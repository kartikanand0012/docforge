output "url" {
  value = "https://${local.domain}"
}

output "instance_id" {
  description = "For `aws ssm start-session --target <id>`."
  value       = aws_instance.demo.id
}

output "ecr_repositories" {
  value = { for name, repo in aws_ecr_repository.app : name => repo.repository_url }
}

output "deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE_ARN secret in GitHub."
  value       = aws_iam_role.deploy.arn
}

output "bucket" {
  value = aws_s3_bucket.originals.id
}

output "demo_pin" {
  description = "Shown on the demo's sign-in page; public by design (synthetic data)."
  value       = random_integer.demo_pin.result
}
