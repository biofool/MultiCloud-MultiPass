###############################################################################
# Test deployment root for the EC2 GPU inference module (ai-pentest-eval).
#
# Bounded test: one g6.xlarge, no model volume, inference API not open to the
# internet. Destroy when the test is done.
#
# Credentials: AWS provider reads the standard env vars (AWS_ACCESS_KEY_ID /
# AWS_SECRET_ACCESS_KEY). Do not commit a tfvars file with keys.
###############################################################################

terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

variable "region" {
  description = "AWS region for the test deployment."
  type        = string
  default     = "us-east-1"
}

provider "aws" {
  region = var.region
}

module "ai_inference" {
  source = "../modules/aws_ec2"

  instance_name    = "ai-pentest-eval-test"
  instance_type    = "g6.xlarge"
  # Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04), owner amazon,
  # built 2026-11-03. Ships NVIDIA drivers, as user_data.sh assumes. The SSM
  # path the module uses by default does not exist in us-east-1.
  ami_id           = "ami-0846639209ba40084"
  # us-east-1a returned InsufficientInstanceCapacity for g6.xlarge on 2026-11-05.
  # Pin to the default subnet in us-east-1b, where the type is offered.
  subnet_id        = "subnet-0cd7f452d4ba152ff"
  project_id       = "ai-pentest-eval-test"
  inference_engine = "vllm"
  vllm_model       = "meta-llama/Llama-3.1-8B-Instruct"

  # Test scope: no separate model volume, no EIP, no SSH.
  # Root must be at least the AMI snapshot size (75 GB); 50 GB failed at RunInstances.
  root_volume_size_gb  = 100
  model_volume_size_gb = 0
  associate_eip        = false
  enable_ssh           = false

  tags = {
    project = "ai-pentest-eval"
    phase   = "test-deployment"
  }
}

output "instance_id" {
  value = module.ai_inference.instance_id
}

output "instance_public_ip" {
  value = module.ai_inference.instance_public_ip
}

output "instance_private_ip" {
  value = module.ai_inference.instance_private_ip
}
