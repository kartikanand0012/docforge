data "aws_caller_identity" "current" {}

# Uploaded originals. Private, encrypted, versioned, and deleted after a few days: the demo's
# documents are synthetic and re-seeded every night.
resource "aws_s3_bucket" "originals" {
  #checkov:skip=CKV_AWS_145:SSE-S3 (AES256); a customer key adds cost for synthetic demo data
  #checkov:skip=CKV_AWS_18:demo bucket; access logging would need a second bucket
  #checkov:skip=CKV_AWS_144:demo data is re-seeded nightly; no cross-region replica needed
  #checkov:skip=CKV2_AWS_62:nothing consumes object events
  bucket_prefix = "docforge-demo-"
  force_destroy = false
}

resource "aws_s3_bucket_public_access_block" "originals" {
  bucket                  = aws_s3_bucket.originals.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "originals" {
  bucket = aws_s3_bucket.originals.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "originals" {
  bucket = aws_s3_bucket.originals.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Old versions and abandoned uploads do not accumulate.
resource "aws_s3_bucket_lifecycle_configuration" "originals" {
  bucket = aws_s3_bucket.originals.id
  rule {
    id     = "tidy"
    status = "Enabled"
    filter {}
    expiration {
      days = var.originals_expire_days
    }
    noncurrent_version_expiration {
      noncurrent_days = 1
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_bucket_versioning" "originals" {
  bucket = aws_s3_bucket.originals.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Only TLS connections.
resource "aws_s3_bucket_policy" "originals" {
  bucket = aws_s3_bucket.originals.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "TlsOnly"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.originals.arn, "${aws_s3_bucket.originals.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

# The host's deploy directory, uploaded with each apply, in a bucket of its own that the host
# can only read: it runs these files as root.
resource "aws_s3_bucket" "deploy" {
  #checkov:skip=CKV_AWS_145:SSE-S3 (AES256); these are the repository's own deploy files
  #checkov:skip=CKV_AWS_18:written only by Terraform; access logging would need another bucket
  #checkov:skip=CKV_AWS_144:the files are in the repository; no replica needed
  #checkov:skip=CKV2_AWS_62:nothing consumes object events
  #checkov:skip=CKV2_AWS_61:a handful of small files, replaced on each apply
  bucket_prefix = "docforge-demo-deploy-"
  force_destroy = true # only copies of files in the repository
}

resource "aws_s3_bucket_public_access_block" "deploy" {
  bucket                  = aws_s3_bucket.deploy.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "deploy" {
  bucket = aws_s3_bucket.deploy.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "deploy" {
  bucket = aws_s3_bucket.deploy.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "deploy" {
  bucket = aws_s3_bucket.deploy.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_policy" "deploy" {
  bucket = aws_s3_bucket.deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "TlsOnly"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.deploy.arn, "${aws_s3_bucket.deploy.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_s3_object" "deploy" {
  for_each = toset([
    "compose.yml", "Caddyfile", "bootstrap.sh", "reset-demo.sh", "ops-check.sh",
    "systemd/docforge-reset.service", "systemd/docforge-reset.timer",
    "systemd/docforge-ops.service", "systemd/docforge-ops.timer",
  ])
  bucket = aws_s3_bucket.deploy.id
  key    = each.value
  source = "${path.module}/../deploy/${each.value}"
  etag   = filemd5("${path.module}/../deploy/${each.value}")
}

resource "aws_ecr_repository" "app" {
  for_each = toset(["docforge-app", "docforge-web"])
  #checkov:skip=CKV_AWS_136:AES256 at rest; a customer key adds cost for public images
  name                 = each.value
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  for_each   = aws_ecr_repository.app
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 10 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}
