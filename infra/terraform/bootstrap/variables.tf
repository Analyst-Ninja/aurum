variable "region" {
  description = "AWS region for the bootstrap resources."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Name prefix for the role."
  type        = string
  default     = "aurum"
}

variable "github_repository" {
  description = "The owner/repo the GitHub Actions OIDC trust policy is scoped to. Only workflow runs on this repository's main branch can assume aws_iam_role.github_actions."
  type        = string
  default     = "Analyst-Ninja/aurum"
}
