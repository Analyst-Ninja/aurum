#!/usr/bin/env bash
# One entry point for Terraform, shared by the GitHub workflows and the Makefile so a
# local deploy and a CI deploy run exactly the same steps.
#
#   scripts/deploy.sh bootstrap   state bucket + GitHub OIDC role (once per account)
#   scripts/deploy.sh plan        terraform plan of the main stack, no image build
#   scripts/deploy.sh apply       ECR -> image push -> full apply (safe on a cold account)
#   scripts/deploy.sh destroy     flip deletion guards, then destroy the main stack
#
# Needs AWS credentials in the environment and Terraform >= 1.10. Secrets come from TF_VAR_*
# (CI) or from the gitignored infra/terraform/terraform.tfvars (local).
# destroy also honours FINAL_SNAPSHOT=<identifier> to keep an RDS snapshot.
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
TF_DIR="$REPO_ROOT/infra/terraform"
REGION=${AWS_REGION:-us-east-1}
ECR_REPO=aurum
STATE_BUCKET=aurum-tfstate-851459781998   # must match backend "s3" in versions.tf

account=$(aws sts get-caller-identity --query Account --output text)
registry="$account.dkr.ecr.$REGION.amazonaws.com"

# Short SHA of the last commit that touched anything the image contains, not HEAD, so an
# infra-only commit resolves to the tag already deployed. Same rule as the old workflow step.
resolve_tag() {
  local commit
  commit=$(git -C "$REPO_ROOT" log -1 --format=%H -- pyproject.toml uv.lock src docker main.py)
  git -C "$REPO_ROOT" rev-parse --short "${commit:-HEAD}"
}

ensure_state_bucket() {
  if [ "$account" != "851459781998" ]; then
    echo "Account $account does not match the bucket pinned in versions.tf" >&2
    exit 1
  fi
  if aws s3api head-bucket --bucket "$STATE_BUCKET" 2>/dev/null; then
    return
  fi
  echo "Creating state bucket $STATE_BUCKET"
  aws s3api create-bucket --bucket "$STATE_BUCKET" --region "$REGION"
  aws s3api put-bucket-versioning --bucket "$STATE_BUCKET" \
    --versioning-configuration Status=Enabled
  aws s3api put-public-access-block --bucket "$STATE_BUCKET" \
    --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
}

# Pushes $1 unless the tag exists (the repo is IMMUTABLE, and this makes a re-run idempotent).
# $2 is an optional docker build target.
push_image() {
  local tag=$1 target=${2:-}
  if aws ecr describe-images --repository-name "$ECR_REPO" --image-ids imageTag="$tag" >/dev/null 2>&1; then
    echo "$registry/$ECR_REPO:$tag already exists, skipping the build."
    return
  fi
  aws ecr get-login-password --region "$REGION" \
    | docker login --username AWS --password-stdin "$registry"
  # --platform linux/amd64: Fargate refuses arm64, and it is a no-op on the CI runner.
  docker build --platform linux/amd64 ${target:+--target "$target"} \
    -f "$REPO_ROOT/docker/aurum.Dockerfile" -t "$registry/$ECR_REPO:$tag" "$REPO_ROOT"
  docker push "$registry/$ECR_REPO:$tag"
}

# Variables with no default and no tfvars entry still need a value at plan and destroy time.
export_tags() {
  tag=$(resolve_tag)
  export TF_VAR_image_tag=$tag TF_VAR_mcp_image_tag=mcp-$tag
}

# An empty TF_VAR_x counts as set to Terraform and overrides the default, so fail in CI
# rather than let a missing secret wipe a resource (see the SNS incident in terraform.yml).
check_secrets() {
  [ "${CI:-}" = "true" ] || return 0
  local missing=0 v
  for v in TF_VAR_db_password TF_VAR_sec_user_agent TF_VAR_alert_email TF_VAR_mcp_password; do
    if [ -z "${!v:-}" ]; then
      echo "::error::$v is unset or empty"
      missing=1
    fi
  done
  [ "$missing" -eq 0 ] || exit 1
}

cmd_bootstrap() {
  ensure_state_bucket
  cd "$TF_DIR/bootstrap"
  terraform init -input=false
  # Adopt what already exists instead of failing with EntityAlreadyExists: an account holds
  # one OIDC provider per URL, and the role may predate this root (it used to live in the main
  # stack's state).
  in_state() { terraform state list 2>/dev/null | grep -qx "$1"; }
  if ! in_state aws_iam_openid_connect_provider.github; then
    arn=$(aws iam list-open-id-connect-providers --query \
      "OpenIDConnectProviderList[?ends_with(Arn, 'token.actions.githubusercontent.com')].Arn | [0]" \
      --output text)
    if [ -n "$arn" ] && [ "$arn" != "None" ]; then
      terraform import aws_iam_openid_connect_provider.github "$arn"
    fi
  fi
  if ! in_state aws_iam_role.github_actions \
     && aws iam get-role --role-name aurum-github-actions >/dev/null 2>&1; then
    terraform import aws_iam_role.github_actions aurum-github-actions
  fi
  if ! in_state aws_iam_role_policy_attachment.github_actions_admin \
     && aws iam get-role --role-name aurum-github-actions >/dev/null 2>&1; then
    terraform import aws_iam_role_policy_attachment.github_actions_admin \
      aurum-github-actions/arn:aws:iam::aws:policy/AdministratorAccess
  fi
  terraform apply -auto-approve -input=false
  echo "Role: $(terraform output -raw github_actions_role_arn)"
}

cmd_plan() {
  check_secrets
  export_tags
  cd "$TF_DIR"
  terraform init -input=false
  terraform plan -input=false
}

cmd_apply() {
  check_secrets
  export_tags
  cd "$TF_DIR"
  terraform init -input=false
  # The repo has to exist before an image can be pushed, and the image has to exist before
  # the MCP host boots (it pulls at user_data time).
  if ! aws ecr describe-repositories --repository-names "$ECR_REPO" >/dev/null 2>&1; then
    terraform apply -auto-approve -input=false \
      -target=aws_ecr_repository.aurum -target=aws_ecr_lifecycle_policy.keep_last_three
  fi
  push_image "$TF_VAR_image_tag"
  push_image "$TF_VAR_mcp_image_tag" mcp
  terraform apply -auto-approve -input=false
}

cmd_destroy() {
  check_secrets
  export_tags
  cd "$TF_DIR"
  terraform init -input=false
  export TF_VAR_allow_destroy=true
  [ -z "${FINAL_SNAPSHOT:-}" ] || export TF_VAR_final_snapshot_identifier=$FINAL_SNAPSHOT
  # Flip deletion protection / force-delete in state first; destroy alone would hit the old values.
  terraform apply -auto-approve -input=false \
    -target=aws_db_instance.aurum \
    -target=aws_ecr_repository.aurum \
    -target=aws_s3_bucket.artifacts
  terraform destroy -auto-approve -input=false
}

case "${1:-}" in
  bootstrap) cmd_bootstrap ;;
  plan)      cmd_plan ;;
  apply)     cmd_apply ;;
  destroy)   cmd_destroy ;;
  *) echo "usage: $0 {bootstrap|plan|apply|destroy}" >&2; exit 2 ;;
esac
