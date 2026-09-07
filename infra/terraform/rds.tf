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

  # 96 MB, up from 16 MB. Both parameters below are `dynamic`, so neither needs a reboot.
  #
  # 16 MB was sized for the original 1 GB db.t4g.micro (see this group's description, which
  # is stale). On db.m7g.large it made every window function in the gold layer spill to
  # temp files: the 2026-09-06 build sustained ~119 MB/s against a 125 MiB/s gp3 ceiling
  # with a disk queue depth of 13-63, moving ~430 GB of I/O to produce ~20 GB of tables,
  # and finished by running the volume out of space entirely.
  #
  # Sizing is bounded by the worst case, not the typical one. work_mem is allocated PER
  # SORT/HASH NODE, per process — and two multipliers compound it:
  #   * max_parallel_workers_per_gather = 2, so a session is leader + 2 workers, each
  #     with its own allocation
  #   * hash_mem_multiplier = 2, so hash nodes get twice this value
  # At dbt threads: 4 that is 4 x 3 x 2 = 24x for a single concurrent hash node, or
  # ~2.3 GB here, against roughly 5 GB left once shared_buffers (1.85 GB) is taken. That
  # is the reason this is not the ~192 MB a naive "RAM / threads" division suggests.
  #
  # Raise it only alongside RAM, and lower it if dbt threads goes above 4.
  parameter {
    name  = "work_mem"
    value = "96608" # kB = 96 MB
  }

  parameter {
    name  = "maintenance_work_mem"
    value = "524288" # kB — index builds during the backfill
  }

  # 1.1, not the 4 the default assumes. 4 encodes a seek penalty for spinning rust; gp3 is
  # SSD, where a random page costs barely more than a sequential one. At 4 the planner
  # systematically over-values sequential scans and under-uses indexes.
  parameter {
    name  = "random_page_cost"
    value = "1.1"
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
