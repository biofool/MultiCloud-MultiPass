output "instance_id" {
  description = "EC2 instance ID."
  value       = aws_instance.inference.id
}

output "instance_public_ip" {
  description = "Public IP of the instance (if associated)."
  value       = aws_instance.inference.public_ip
}

output "instance_private_ip" {
  description = "Private IP of the instance."
  value       = aws_instance.inference.private_ip
}

output "elastic_ip" {
  description = "Elastic IP address (if allocated)."
  value       = length(aws_eip.inference) > 0 ? aws_eip.inference[0].public_ip : ""
}

output "inference_endpoint" {
  description = "Full inference API URL."
  value       = "http://${length(aws_eip.inference) > 0 ? aws_eip.inference[0].public_ip : aws_instance.inference.public_ip}:${var.inference_port}"
}

output "security_group_id" {
  value = aws_security_group.inference.id
}

output "iam_role_arn" {
  value = aws_iam_role.inference.arn
}

output "model_volume_id" {
  description = "EBS model volume ID (empty if no separate model volume)."
  value       = length(aws_ebs_volume.models) > 0 ? aws_ebs_volume.models[0].id : ""
}
