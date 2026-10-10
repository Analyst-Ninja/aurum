output "github_actions_role_arn" {
  description = "Role the Terraform workflows assume via OIDC. Store as the repository variable AWS_ROLE_ARN."
  value       = aws_iam_role.github_actions.arn
}
