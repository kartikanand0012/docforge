data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

resource "aws_eip" "demo" {
  domain = "vpc"
}

resource "aws_instance" "demo" {
  #checkov:skip=CKV_AWS_88:a single public demo host; only 80 and 443 are open, no SSH
  ami                         = data.aws_ssm_parameter.al2023_arm64.value
  instance_type               = var.instance_type
  subnet_id                   = aws_subnet.public.id
  vpc_security_group_ids      = [aws_security_group.web.id]
  iam_instance_profile        = aws_iam_instance_profile.host.name
  associate_public_ip_address = true
  monitoring                  = true
  ebs_optimized               = true

  metadata_options {
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 1          # containers cannot reach the host role's credentials
    http_endpoint               = "enabled"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 40
    encrypted   = true
  }

  # Fetch the deploy directory from the bucket and bring the stack up.
  user_data                   = <<-EOT
    #!/bin/bash
    set -euo pipefail
    mkdir -p /opt/docforge
    aws s3 cp --recursive s3://${aws_s3_bucket.deploy.id}/ /opt/docforge/
    chmod +x /opt/docforge/*.sh
    /opt/docforge/bootstrap.sh
  EOT
  user_data_replace_on_change = false

  tags = { Name = "docforge-demo" }

  lifecycle {
    ignore_changes = [ami] # a new AMI is a deliberate rebuild, not every plan
  }
}

resource "aws_eip_association" "demo" {
  instance_id   = aws_instance.demo.id
  allocation_id = aws_eip.demo.id
}

resource "aws_route53_record" "demo" {
  count   = var.route53_zone_id != "" && var.domain != "" ? 1 : 0
  zone_id = var.route53_zone_id
  name    = var.domain
  type    = "A"
  ttl     = 300
  records = [aws_eip.demo.public_ip]
}
