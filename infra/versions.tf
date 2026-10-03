terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
  # State holds the generated passwords: keep it in an encrypted, private S3 bucket.
  # Fill in and uncomment before the first apply (the bucket is created once, by hand):
  # backend "s3" {
  #   bucket       = "<your-state-bucket>"
  #   key          = "docforge/demo.tfstate"
  #   region       = "ap-south-1"
  #   encrypt      = true
  #   use_lockfile = true
  # }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project = "docforge"
      Stack   = "demo"
    }
  }
}
