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

# The application's S3 access: a key for the originals bucket only. The containers cannot
# reach the host's role (IMDS hop limit 1), so the key is all they hold.
resource "aws_iam_user" "app" {
  #checkov:skip=CKV_AWS_273:a machine identity for containers that must not reach the host role
  name = "docforge-demo-app"
}

data "aws_iam_policy_document" "app" {
  statement {
    sid       = "Originals"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.originals.arn}/*"]
  }
}

resource "aws_iam_user_policy" "app" {
  #checkov:skip=CKV_AWS_40:one user, one inline policy scoped to one bucket
  user   = aws_iam_user.app.name
  policy = data.aws_iam_policy_document.app.json
}

resource "aws_iam_access_key" "app" {
  user = aws_iam_user.app.name
}

locals {
  domain   = var.domain != "" ? var.domain : "${replace(aws_eip.demo.public_ip, ".", "-")}.sslip.io"
  registry = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com"
  app_url  = "postgresql+psycopg://docforge_app_user:${random_password.app_login.result}@postgres:5432/docforge"

  settings = {
    # Read by Compose itself: images, domain, the public demo PIN.
    compose = join("\n", [
      "DOMAIN=${local.domain}",
      "APP_IMAGE=${local.registry}/docforge-app:${var.image_tag}",
      "WEB_IMAGE=${local.registry}/docforge-web:${var.image_tag}",
      "DEMO_PIN=${random_integer.demo_pin.result}",
      "",
    ])
    # The API and the worker: never the database owner.
    app = join("\n", [
      "ENVIRONMENT=production",
      "DATABASE_URL=${local.app_url}",
      "MIGRATION_DATABASE_URL=${local.app_url}",
      "S3_ENDPOINT_URL=",
      "S3_REGION=${var.region}",
      "S3_BUCKET=${aws_s3_bucket.originals.id}",
      "AWS_ACCESS_KEY_ID=${aws_iam_access_key.app.id}",
      "AWS_SECRET_ACCESS_KEY=${aws_iam_access_key.app.secret}",
      "WEBHOOK_SIGNING_KEY=${random_password.webhook_key.result}",
      "PIPELINE_FACTORY=docforge.wiring:build_replay_pipelines",
      "GEMINI_API_KEY=",
      "PARSER_OFFLINE=true",
      "PARSER_MAX_RSS_MB=2048",
      "DEMO_PIN=${random_integer.demo_pin.result}",
      "",
    ])
    # Postgres and the one-shot admin service only.
    owner = join("\n", [
      "POSTGRES_PASSWORD=${random_password.postgres.result}",
      "MIGRATION_DATABASE_URL=postgresql+psycopg://docforge:${random_password.postgres.result}@postgres:5432/docforge",
      "",
    ])
  }
}

resource "aws_ssm_parameter" "settings" {
  #checkov:skip=CKV_AWS_337:encrypted with the AWS-managed aws/ssm key; a customer key adds cost
  for_each = local.settings
  name     = "/docforge/demo/${each.key}"
  type     = "SecureString"
  tier     = "Standard"
  value    = each.value
}
