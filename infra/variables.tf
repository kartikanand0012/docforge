variable "region" {
  description = "AWS region for everything."
  type        = string
  default     = "ap-south-1"
}

variable "instance_type" {
  description = "The demo host (arm64). 8 GB holds the stack; the OCR path needs more for long scans."
  type        = string
  default     = "t4g.large"
}

variable "domain" {
  description = "The demo's domain, e.g. demo.example.com. Empty: <elastic ip>.sslip.io."
  type        = string
  default     = ""
}

variable "route53_zone_id" {
  description = "Hosted zone for `domain`, to create its A record. Empty: point the domain yourself."
  type        = string
  default     = ""
}

variable "alert_email" {
  description = "Where alarms and budget warnings go."
  type        = string
}

variable "monthly_budget_usd" {
  description = "A warning at 80% and at 100% of this, forecast and actual."
  type        = number
  default     = 50
}

variable "image_tag" {
  description = "The image tag (git sha) the host runs; CI pushes it to ECR."
  type        = string
}

variable "originals_expire_days" {
  description = "Uploaded originals are deleted after this many days (the demo is re-seeded nightly)."
  type        = number
  default     = 7
}

variable "github_repository" {
  description = "owner/name of the repository whose main branch may push images and deploy."
  type        = string
  default     = "kartikanand0012/docforge"
}
