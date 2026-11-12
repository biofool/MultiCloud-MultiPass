variable "instance_name" {
  description = "Name tag for the EC2 instance and associated resources."
  type        = string
  default     = "cloudmanagement-ai-inference"
}

variable "instance_type" {
  description = "EC2 instance type. Default g6.xlarge (1x L4, 24GB VRAM) — cheapest modern GPU for 7B-13B inference. See docs/ec2-ai-workload-plan.md for pricing."
  type        = string
  default     = "g6.xlarge"
}

variable "project_id" {
  description = "CloudManagement project_id tag value — must match the cost_allocation_tag_key convention so AWS Budgets tracks this instance's spend."
  type        = string
}

variable "cost_allocation_tag_key" {
  description = "AWS user-defined cost allocation tag key — must match the root config's var.cost_allocation_tag_key so this instance's spend is tracked by the same AWS Budgets cost_filter. Defaults to 'project_id'."
  type        = string
  default     = "project_id"
}

variable "ami_id" {
  description = "Override the AMI ID. If empty, uses the latest Ubuntu 22.04 NVIDIA GPU AMI from SSM Parameter Store."
  type        = string
  default     = ""
}

variable "vpc_id" {
  description = "VPC ID for the instance. If empty, uses the default VPC."
  type        = string
  default     = ""
}

variable "subnet_id" {
  description = "Subnet ID for the instance. If empty, uses the first default-VPC subnet."
  type        = string
  default     = ""
}

variable "inference_engine" {
  description = "Which inference engine to run: 'vllm' or 'ollama'."
  type        = string
  default     = "vllm"

  validation {
    condition     = contains(["vllm", "ollama"], var.inference_engine)
    error_message = "inference_engine must be 'vllm' or 'ollama'."
  }
}

variable "inference_port" {
  description = "Port the inference API listens on. 8000 for vLLM, 11434 for Ollama."
  type        = number
  default     = 8000
}

variable "vllm_model" {
  description = "HuggingFace model ID for vLLM (e.g. meta-llama/Llama-3.1-8B-Instruct). Ignored when inference_engine='ollama'."
  type        = string
  default     = "meta-llama/Llama-3.1-8B-Instruct"
}

variable "ollama_model" {
  description = "Ollama model tag (e.g. llama3:8b). Ignored when inference_engine='vllm'."
  type        = string
  default     = "llama3:8b"
}

variable "tensor_parallel_size" {
  description = "Tensor parallel degree for vLLM. 1 for single-GPU instances; increase for multi-GPU."
  type        = number
  default     = 1
}

variable "allowed_cidr" {
  description = "CIDR block allowed to reach the inference API port. Default restricts to VPC-internal; widen for public access."
  type        = string
  default     = "10.0.0.0/8"
}

variable "enable_ssh" {
  description = "Allow inbound SSH (port 22). Prefer SSM Session Manager — only enable for debugging."
  type        = bool
  default     = false
}

variable "ssh_allowed_cidr" {
  description = "CIDR block allowed to reach SSH (port 22). Independent of var.allowed_cidr so widening the API CIDR to 0.0.0.0/0 does NOT also open SSH. Default restricts to private range; set to a specific office/VPN CIDR."
  type        = string
  default     = "10.0.0.0/8"
}

variable "root_volume_size_gb" {
  description = "Root EBS volume size in GB (gp3, encrypted)."
  type        = number
  default     = 50
}

variable "model_volume_size_gb" {
  description = "Model storage EBS volume size in GB. Set to 0 to skip creating a separate model volume."
  type        = number
  default     = 100
}

variable "model_artifacts_bucket" {
  description = "S3 bucket containing pre-downloaded model artifacts. If set, grants the instance read access. Empty = download from HuggingFace/Ollama at boot."
  type        = string
  default     = ""
}

variable "associate_public_ip" {
  description = "Whether to associate a public IP with the instance."
  type        = bool
  default     = true
}

variable "associate_eip" {
  description = "Whether to allocate and associate an Elastic IP for a stable endpoint."
  type        = bool
  default     = false
}

variable "tags" {
  type = map(string)
  default = {
    project = "cloudmanagement"
    phase   = "aws-ec2-ai-inference"
  }
}
