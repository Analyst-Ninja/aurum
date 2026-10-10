# GitHub Actions OIDC — the identity `.github/workflows/terraform.yml`'s apply job assumes.
#
# The alternative is a long-lived AWS access key pair sitting in repository secrets. OIDC
# hands the runner a short-lived STS session instead, scoped by a trust policy that names
# exactly one repository and one branch, so a leaked GitHub token buys nothing outside a
# `main` workflow run.
#
# Lives in its own root (own state key) so `terraform destroy` on the main stack can never
# delete the role the destroy workflow is running as. Applied once by bootstrap.yml or `make tf-bootstrap`.

# AWS ships GitHub's CA in its own trust store, so `thumbprint_list` is optional here and
# deliberately omitted — a pinned thumbprint becomes an outage the day GitHub rotates its
# certificate.
#
# An account can hold only one provider per URL. If another repository already created it,
# this apply fails with EntityAlreadyExists; import the existing one rather than deleting it:
#   terraform import aws_iam_openid_connect_provider.github \
#     arn:aws:iam::<account>:oidc-provider/token.actions.githubusercontent.com
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "github_actions_assume_role" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    # `aud` proves the token was minted for STS and not for some other consumer.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # `sub` is the whole security boundary. Exact StringEquals values, never StringLike
    # with a wildcard: `repo:<owner>/<repo>:*` would let a pull request from a fork branch
    # assume an AdministratorAccess role.
    #
    # Two subjects, because GitHub changes the claim for a job that declares `environment:`
    # from `ref:refs/heads/main` to `environment:<name>`. The destroy workflow uses the
    # `destroy` environment (required reviewers), so without the second value its token is
    # rejected with "Not authorized to perform sts:AssumeRoleWithWebIdentity". Restrict the
    # environment to the main branch in Settings -> Environments so it cannot be used from
    # another branch.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:${var.github_repository}:ref:refs/heads/main",
        "repo:${var.github_repository}:environment:destroy",
      ]
    }
  }
}

resource "aws_iam_role" "github_actions" {
  name               = "${var.project}-github-actions"
  description        = "Assumed by the Terraform apply job on pushes to main. See .github/workflows/terraform.yml."
  assume_role_policy = data.aws_iam_policy_document.github_actions_assume_role.json
}

# AdministratorAccess, on purpose.
#
# This configuration manages its own IAM roles and inline policies (aws_iam_role.execution,
# .task, .sfn, .scheduler), so any policy broad enough to run `terraform apply` already
# grants iam:* — and iam:* is self-escalating. A hand-curated allow-list would therefore be
# no narrower in practice, while breaking every apply that introduces a new resource type.
# The real containment is the `sub` condition above: only a workflow run on main can get here.
resource "aws_iam_role_policy_attachment" "github_actions_admin" {
  role       = aws_iam_role.github_actions.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}
