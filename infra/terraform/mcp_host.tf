# The MCP server host: one small EC2 instance running the slim `mcp` image.
#
# Access is private. The server speaks streamable-http on the instance's loopback and has no
# authentication of its own, so this group has NO ingress rules and the only way in is an
# SSM Session Manager port-forward, which IAM gates:
#
#   terraform output mcp_port_forward_command
#
# No ALB, NAT, Elastic IP or domain — the public IP is egress-only, for the same reason the
# ECS tasks have one (there is no NAT; see main.tf). Set mcp_enabled = false to tear it down.
# Design: docs/mcp/mcp-server-design.md, docs/infra/aws-deployment-plan.md.

locals {
  mcp_count = var.mcp_enabled ? 1 : 0
}

# Latest Amazon Linux 2023, x86_64 because the image is built linux/amd64. The AMI id is
# ignored after creation (see lifecycle below) so a new AMI release does not replace the box.
data "aws_ssm_parameter" "al2023_x86" {
  count = local.mcp_count
  name  = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

resource "aws_security_group" "mcp" {
  count       = local.mcp_count
  name        = "${var.project}-mcp"
  description = "MCP host: egress only, no ingress (reached by SSM port-forward)"
  vpc_id      = data.aws_vpc.default.id

  egress {
    description = "All outbound: SSM, ECR and RDS"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.project}-mcp" }
}

# --- Credentials -------------------------------------------------------------------
#
# The MCP server connects as the read-only role, never the master user. Terraform stores the
# credentials but does not create the role: infra/sql/mcp_readonly_role.sql is applied once,
# by hand, as the warehouse owner, and var.mcp_password must match the password given there.

resource "aws_ssm_parameter" "mcp_username" {
  count = local.mcp_count
  name  = "/${var.project}/mcp_username"
  type  = "String"
  value = var.mcp_username
}

resource "aws_ssm_parameter" "mcp_password" {
  count = local.mcp_count
  name  = "/${var.project}/mcp_password"
  type  = "SecureString"
  value = var.mcp_password
}

# --- IAM ---------------------------------------------------------------------------

data "aws_iam_policy_document" "ec2_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "mcp" {
  count              = local.mcp_count
  name               = "${var.project}-mcp"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json
}

# Session Manager: the agent registers the instance and carries the port-forward.
resource "aws_iam_role_policy_attachment" "mcp_ssm_core" {
  count      = local.mcp_count
  role       = aws_iam_role.mcp[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "mcp" {
  count = local.mcp_count

  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid       = "PullMcpImage"
    actions   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    resources = [aws_ecr_repository.aurum.arn]
  }

  statement {
    sid     = "ReadMcpCredentials"
    actions = ["ssm:GetParameter"]
    resources = [
      aws_ssm_parameter.mcp_username[0].arn,
      aws_ssm_parameter.mcp_password[0].arn,
    ]
  }

  # SecureString parameters are decrypted with the account's default SSM key.
  statement {
    sid       = "DecryptSecureStrings"
    actions   = ["kms:Decrypt"]
    resources = ["arn:aws:kms:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
  }
}

resource "aws_iam_role_policy" "mcp" {
  count  = local.mcp_count
  name   = "${var.project}-mcp"
  role   = aws_iam_role.mcp[0].id
  policy = data.aws_iam_policy_document.mcp[0].json
}

resource "aws_iam_instance_profile" "mcp" {
  count = local.mcp_count
  name  = "${var.project}-mcp"
  role  = aws_iam_role.mcp[0].name
}

# --- Instance ----------------------------------------------------------------------

resource "aws_instance" "mcp" {
  count                  = local.mcp_count
  ami                    = data.aws_ssm_parameter.al2023_x86[0].value
  instance_type          = var.mcp_instance_type
  subnet_id              = data.aws_subnets.default.ids[0]
  vpc_security_group_ids = [aws_security_group.mcp[0].id]
  iam_instance_profile   = aws_iam_instance_profile.mcp[0].name

  # Egress only: there is no NAT, so the instance needs a public address to reach SSM, ECR
  # and RDS. The security group has no ingress rule, so nothing can connect to it.
  associate_public_ip_address = true

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 20
    encrypted   = true
  }

  user_data = templatefile("${path.module}/mcp_host.tftpl", {
    project  = var.project
    region   = data.aws_region.current.region
    registry = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${data.aws_region.current.region}.amazonaws.com"
    image    = "${aws_ecr_repository.aurum.repository_url}:${var.mcp_image_tag}"
    db_host  = aws_db_instance.aurum.address
    schemas  = var.mcp_schemas
  })

  # A new image tag changes user_data, and the unit reads the tag from it: replacing the box
  # is how a new MCP image rolls out. Cold start is a couple of minutes of downtime.
  user_data_replace_on_change = true

  lifecycle {
    ignore_changes = [ami]

    precondition {
      condition     = var.mcp_image_tag != "latest"
      error_message = "mcp_image_tag cannot be \"latest\": the ECR repository is IMMUTABLE, so that tag is never pushed."
    }
  }

  tags = { Name = "${var.project}-mcp" }
}
