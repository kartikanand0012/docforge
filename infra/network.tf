data "aws_availability_zones" "available" {
  #checkov:skip=CKV_AWS_394:only the first zone is used, once, for the single subnet
  state = "available"
}

resource "aws_vpc" "demo" {
  #checkov:skip=CKV2_AWS_11:a one-host demo VPC; flow logs add cost and a log pipeline
  cidr_block           = "10.20.0.0/16"
  enable_dns_hostnames = true
  tags                 = { Name = "docforge-demo" }
}

resource "aws_internet_gateway" "demo" {
  vpc_id = aws_vpc.demo.id
}

resource "aws_subnet" "public" {
  vpc_id            = aws_vpc.demo.id
  cidr_block        = "10.20.1.0/24"
  availability_zone = data.aws_availability_zones.available.names[0]
  tags              = { Name = "docforge-demo-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.demo.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.demo.id
  }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# The default security group allows nothing.
resource "aws_default_security_group" "demo" {
  vpc_id = aws_vpc.demo.id
}

# HTTPS (and HTTP for the certificate challenge and the redirect) only. No SSH: the host is
# reached through SSM Session Manager.
resource "aws_security_group" "web" {
  #checkov:skip=CKV_AWS_260:port 80 for the certificate challenge and the redirect to HTTPS
  #checkov:skip=CKV_AWS_382:the host pulls images and packages and calls AWS APIs
  name        = "docforge-demo-web"
  description = "HTTP and HTTPS to Caddy"
  vpc_id      = aws_vpc.demo.id

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "HTTP, redirected to HTTPS; certificate challenges"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    description = "Images, packages, AWS APIs, certificates"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
