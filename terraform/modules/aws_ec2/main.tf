###############################################################################
# EC2 GPU inference instance for vLLM/Ollama AI workloads.
#
# Provisions a single GPU EC2 instance with Docker + NVIDIA Container Toolkit
# pre-installed via user_data, serving an LLM via an OpenAI-compatible API.
# Uses an IAM instance profile (not static keys) for runtime AWS access;
# the GSM-stored AWS keys are only for CloudManagement's management code
# (see aws_session.py).
#
# Gated by var.enable in the root config — existing Lambda-only deployments
# are unaffected when this module is not invoked.
###############################################################################

# Default AMI lookup via SSM Parameter Store. Only read when var.ami_id is
# empty. The path below did not resolve in us-east-1 (2026-11-05), so pin
# var.ami_id to a verified image for now. Keeping the lookup conditional stops
# an unresolvable default from failing the plan for every caller.
data "aws_ssm_parameter" "gpu_ami" {
  count = var.ami_id == "" ? 1 : 0
  name  = "/aws/service/canonical/ubuntu/server/22.04/pvt/amd64/nvidia_gpu"
}

# Default VPC subnet — using the default VPC avoids creating a full VPC
# for a single inference instance. Override with var.subnet_id for a
# custom VPC deployment.
data "aws_subnets" "default" {
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
  filter {
    name   = "vpc-id"
    values = [var.vpc_id != "" ? var.vpc_id : data.aws_vpc.default.id]
  }
}

data "aws_vpc" "default" {
  default = var.vpc_id == ""
  id      = var.vpc_id != "" ? var.vpc_id : null
}

###############################################################################
# Security group — inbound on the inference API port only
###############################################################################

resource "aws_security_group" "inference" {
  name        = "${var.instance_name}-sg"
  description = "Inference API access for ${var.instance_name}"
  vpc_id      = var.vpc_id != "" ? var.vpc_id : data.aws_vpc.default.id

  tags = var.tags
}

resource "aws_vpc_security_group_ingress_rule" "api" {
  security_group_id = aws_security_group.inference.id
  description       = "Inference API port (${var.inference_port})"
  from_port         = var.inference_port
  to_port           = var.inference_port
  ip_protocol       = "tcp"
  cidr_ipv4         = var.allowed_cidr
}

resource "aws_vpc_security_group_ingress_rule" "ssh" {
  count             = var.enable_ssh ? 1 : 0
  security_group_id = aws_security_group.inference.id
  description       = "SSH (prefer SSM Session Manager instead)"
  from_port         = 22
  to_port           = 22
  ip_protocol       = "tcp"
  cidr_ipv4         = var.ssh_allowed_cidr
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.inference.id
  description       = "All outbound (model downloads, API calls)"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

###############################################################################
# IAM instance profile — SSM managed-instance + CloudWatch agent
###############################################################################

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "inference" {
  name               = "${var.instance_name}-role"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
  tags               = var.tags
}

# SSM managed-instance core — enables Session Manager (SSH-less access)
# and SSM Run Command for remote management.
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.inference.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# CloudWatch agent — log shipping from the inference container.
resource "aws_iam_role_policy_attachment" "cloudwatch" {
  role       = aws_iam_role.inference.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

# S3 read access for model artifacts bucket (if configured) — scoped to
# the specific bucket ARN, never s3:GetObject on "*".
data "aws_iam_policy_document" "s3_model_read" {
  count = var.model_artifacts_bucket != "" ? 1 : 0
  statement {
    actions = ["s3:GetObject", "s3:ListBucket"]
    resources = [
      "arn:aws:s3:::${var.model_artifacts_bucket}",
      "arn:aws:s3:::${var.model_artifacts_bucket}/*",
    ]
  }
}

resource "aws_iam_policy" "s3_model_read" {
  count  = var.model_artifacts_bucket != "" ? 1 : 0
  name   = "${var.instance_name}-s3-model-read"
  policy = data.aws_iam_policy_document.s3_model_read[0].json
}

resource "aws_iam_role_policy_attachment" "s3_model_read" {
  count      = var.model_artifacts_bucket != "" ? 1 : 0
  role       = aws_iam_role.inference.name
  policy_arn = aws_iam_policy.s3_model_read[0].arn
}

resource "aws_iam_instance_profile" "inference" {
  name = "${var.instance_name}-profile"
  role = aws_iam_role.inference.name
  tags = var.tags
}

###############################################################################
# EBS volumes — root + model storage
###############################################################################

# Root volume is part of the aws_instance resource (see root_block_device
# below). The model storage volume is a separate EBS volume mounted at
# /models for downloaded model weights, keeping them persistent across
# instance stop/start if the root volume is replaced.
#
# AZ is derived from the instance itself (aws_instance.inference.availability_zone)
# rather than var.availability_zone — an EBS volume MUST be in the same AZ as
# the instance it attaches to, and the instance's AZ is determined by its
# subnet. Deriving the volume AZ from the instance avoids the mismatch that
# would occur if var.availability_zone and the subnet's AZ disagreed.
resource "aws_ebs_volume" "models" {
  count             = var.model_volume_size_gb > 0 ? 1 : 0
  availability_zone = aws_instance.inference.availability_zone
  size              = var.model_volume_size_gb
  type              = "gp3"
  encrypted         = true
  tags              = var.tags
}

resource "aws_volume_attachment" "models" {
  count       = var.model_volume_size_gb > 0 ? 1 : 0
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.models[0].id
  instance_id = aws_instance.inference.id
}

###############################################################################
# EC2 instance — GPU inference
###############################################################################

resource "aws_instance" "inference" {
  ami                  = var.ami_id != "" ? var.ami_id : data.aws_ssm_parameter.gpu_ami[0].value
  instance_type        = var.instance_type
  subnet_id            = var.subnet_id != "" ? var.subnet_id : data.aws_subnets.default.ids[0]
  iam_instance_profile = aws_iam_instance_profile.inference.name

  vpc_security_group_ids = [aws_security_group.inference.id]

  associate_public_ip_address = var.associate_public_ip

  # Require IMDSv2 — instance-role credentials stay available but SSRF
  # against the metadata endpoint via plain GET (IMDSv1) is blocked.
  metadata_options {
    http_tokens = "required"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = var.root_volume_size_gb
    encrypted   = true
    tags        = var.tags
  }

  user_data = templatefile("${path.module}/user_data.sh", {
    inference_engine     = var.inference_engine
    vllm_model           = var.vllm_model
    vllm_port            = var.inference_port
    vllm_tensor_parallel = var.tensor_parallel_size
    ollama_model         = var.ollama_model
    ollama_port          = var.inference_port
    model_volume_size_gb = var.model_volume_size_gb
  })

  # Cost allocation tag — uses var.cost_allocation_tag_key (the same key
  # the root config's aws_budgets_budget resources filter on) so this
  # instance's spend is tracked under its project_id in Cost Explorer and
  # AWS Budgets. The tag key is configurable to stay in lockstep with the
  # root var.cost_allocation_tag_key.
  tags = merge(var.tags, {
    "${var.cost_allocation_tag_key}" = var.project_id
    Name                             = var.instance_name
  })

  # GPU instances can take several minutes for first-boot driver setup.
  # Don't let a slow user_data cause a create-timeout.
  instance_initiated_shutdown_behavior = "stop"

  # No lifecycle.ignore_changes on the cost-allocation tag — a Terraform-side
  # project_id change should propagate. If the tag is edited out-of-band in
  # the AWS console, the next apply will reconcile it back to var.project_id,
  # which is the desired behavior for cost-tracking integrity.
}

###############################################################################
# Elastic IP (optional) — stable endpoint for the inference API
###############################################################################

resource "aws_eip" "inference" {
  count    = var.associate_eip ? 1 : 0
  instance = aws_instance.inference.id
  tags     = var.tags
}
