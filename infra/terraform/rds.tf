# The one instance that holds everything: the ingestion landing tables plus the whole
# bronze/silver/gold medallion.
#
# It is ~82% of the monthly bill, which is why it is a db.t4g.micro and not something
# comfortable. See docs/infra/aws-deployment-plan.md §5 for the trade-off in full.

# The default VPC has ONLY public subnets, so the subnet group is built from them.
resource "aws_db_subnet_group" "aurum" {
  name       = "${var.project}-db"
  subnet_ids = data.aws_subnets.default.ids

  tags = { Name = "${var.project}-db" }
}

# db.t4g.micro has 1 GB of RAM. The silver models run window functions over a 900-day
# lookback per symbol across 503 symbols, which will spill to disk at the default 4 MB
# work_mem. These values do not make it fast; they stop it being pathological.
resource "aws_db_parameter_group" "aurum" {
  name        = "${var.project}-pg17"
  family      = "postgres17"
  description = "Sort/hash memory tuned for the dbt medallion on a 1 GB instance"

  parameter {
    name  = "work_mem"
    value = "16384" # kB
  }

  parameter {
    name  = "maintenance_work_mem"
    value = "65536" # kB — index builds during the backfill
  }
}

resource "aws_db_instance" "aurum" {
  identifier = var.project
  engine     = "postgres"

  # Major version only, deliberately. Pinning an exact minor while
  # auto_minor_version_upgrade is on produces a permanent diff the moment AWS ships the
  # next patch; with "17" the provider compares the prefix and stays quiet. Verified
  # available in us-east-1, where 17.11 is current.
  engine_version = "17"

  # db.t4g.micro + gp3 is orderable for postgres 17 (gp3 minimum is 20 GB, we take 30).
  instance_class = var.db_instance_class

  db_name  = var.project
  username = var.db_username
  password = var.db_password

  # gp3 with autoscaling: pay for 30 GB, grow to 100 GB if the backfill needs it rather
  # than stalling half way through.
  storage_type          = "gp3"
  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = var.db_max_allocated_storage
  storage_encrypted     = true

  db_subnet_group_name   = aws_db_subnet_group.aurum.name
  vpc_security_group_ids = [aws_security_group.data.id]

  # Public IP so the operator can connect from a laptop. Who may actually connect is
  # decided by the `data` security group's 5432 rule, not by this flag.
  publicly_accessible = var.db_publicly_accessible

  parameter_group_name = aws_db_parameter_group.aurum.name

  multi_az                   = false
  backup_retention_period    = 7
  auto_minor_version_upgrade = true
  deletion_protection        = true

  # Without this a change to instance_class waits for the next maintenance window, so
  # `terraform apply` returns "success" and nothing actually resizes. There is no
  # production traffic to protect here — resizes should happen when asked for.
  apply_immediately = true

  # A personal project does not need a final snapshot ceremony on teardown, but
  # deletion_protection above means teardown is deliberate either way.
  skip_final_snapshot = true

  tags = { Name = var.project }
}
