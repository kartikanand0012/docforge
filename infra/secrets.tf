# The stack's settings as one SecureString, generated here. The values are in Terraform state,
# which is why state must live in an encrypted, private bucket (versions.tf).
resource "random_password" "postgres" {
  length  = 32
  special = false
}

resource "random_password" "app_login" {
  length  = 32
  special = false
}

resource "random_password" "webhook_key" {
  length  = 48
  special = false
}

resource "random_integer" "demo_pin" {
  min = 100000
  max = 999999
}

locals {
  domain   = var.domain != "" ? var.domain : "${replace(aws_eip.demo.public_ip, ".", "-")}.sslip.io"
  registry = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com"
  env_file = join("\n", [
    "ENVIRONMENT=production",
    "DOMAIN=${local.domain}",
    "APP_IMAGE=${local.registry}/docforge-app:${var.image_tag}",
    "WEB_IMAGE=${local.registry}/docforge-web:${var.image_tag}",
    "POSTGRES_PASSWORD=${random_password.postgres.result}",
    "MIGRATION_DATABASE_URL=postgresql+psycopg://docforge:${random_password.postgres.result}@postgres:5432/docforge",
    "DATABASE_URL=postgresql+psycopg://docforge_app_user:${random_password.app_login.result}@postgres:5432/docforge",
    "S3_ENDPOINT_URL=",
    "S3_REGION=${var.region}",
    "S3_BUCKET=${aws_s3_bucket.originals.id}",
    "WEBHOOK_SIGNING_KEY=${random_password.webhook_key.result}",
    "PIPELINE_FACTORY=docforge.wiring:build_replay_pipelines",
    "GEMINI_API_KEY=",
    "DEMO_PIN=${random_integer.demo_pin.result}",
    "PARSER_OFFLINE=true",
    "",
  ])
}

resource "aws_ssm_parameter" "env" {
  #checkov:skip=CKV_AWS_337:encrypted with the AWS-managed aws/ssm key; a customer key adds cost
  name  = "/docforge/demo/env"
  type  = "SecureString"
  tier  = "Standard"
  value = local.env_file
}
