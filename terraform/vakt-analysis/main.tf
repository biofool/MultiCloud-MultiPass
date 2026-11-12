###############################################################################
# vakt analysis box (Security/code_review). Over-provisioned GPU host for
# running vakt scans in AWS instead of eng-rtx-a5000.
#
# SSH only from operator_cidr. Destroy when analysis is done:
#   tofu -chdir=terraform/vakt-analysis destroy
#
# Credentials: AWS_PROFILE=ai-pentest-test (or standard env vars).
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
  type    = string
  default = "us-east-1"
}

variable "instance_type" {
  type    = string
  default = "g6e.2xlarge"
}

variable "availability_zone" {
  type    = string
  default = "us-east-1b"
}

# Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04) 20261110.
variable "ami_id" {
  type    = string
  default = "ami-020971c2de8d4cbca"
}

variable "operator_cidr" {
  description = "CIDR allowed to SSH in (operator public IP /32)."
  type        = string
}

variable "ssh_public_key_path" {
  type    = string
  default = "~/.ssh/id_ed25519.pub"
}

variable "root_volume_size_gb" {
  type    = number
  default = 200
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      project = "vakt-analysis"
      owner   = "kkron"
    }
  }
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnet" "default_az" {
  vpc_id            = data.aws_vpc.default.id
  availability_zone = var.availability_zone
  default_for_az    = true
}

resource "aws_key_pair" "operator" {
  key_name_prefix = "vakt-analysis-"
  public_key      = file(pathexpand(var.ssh_public_key_path))
}

resource "aws_security_group" "vakt" {
  name_prefix = "vakt-analysis-"
  description = "vakt analysis: SSH from operator only"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "SSH from operator"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.operator_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "vakt" {
  ami                         = var.ami_id
  instance_type               = var.instance_type
  subnet_id                   = data.aws_subnet.default_az.id
  key_name                    = aws_key_pair.operator.key_name
  vpc_security_group_ids      = [aws_security_group.vakt.id]
  associate_public_ip_address = true

  metadata_options {
    http_tokens = "required"
  }

  root_block_device {
    volume_size = var.root_volume_size_gb
    volume_type = "gp3"
    encrypted   = true
  }

  # vakt install and `hf auth login` stay manual: they need the operator's HF token.
  user_data = <<-EOF
    #!/bin/bash
    apt-get update -y && apt-get install -y libopenblas0
  EOF

  tags = {
    Name = "vakt-analysis"
  }

  # The provider retries InsufficientInstanceCapacity until timeout; fail fast
  # so launch.sh can try the next AZ/type.
  timeouts {
    create = "4m"
  }
}

output "instance_id" {
  value = aws_instance.vakt.id
}

output "instance_type" {
  value = aws_instance.vakt.instance_type
}

output "public_ip" {
  value = aws_instance.vakt.public_ip
}
