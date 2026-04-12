########################################################################
# Suepercharge MVP — AWS footprint (ingest agent only)
#
# Three services:
#   1. Lambda    — ingest agent
#   2. RDS       — single t4g.micro Postgres, single-AZ
#   3. S3        — raw press releases
#   4. EventBridge Scheduler — 15-minute cron for ingest
#
# Lambda is NOT in VPC. RDS is publicly accessible (dev) with password
# + SSL. When we add creative/campaign agents, add VPC + NAT gateway.
########################################################################

locals {
  name_prefix = "suepercharge-${var.env}"
}

resource "random_id" "bucket_suffix" {
  byte_length = 4
}

########################################################################
# S3 — raw artifacts + generated creative
########################################################################

resource "aws_s3_bucket" "artifacts" {
  bucket        = "${local.name_prefix}-artifacts-${random_id.bucket_suffix.hex}"
  force_destroy = var.env != "prod"
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    id     = "glacier-after-90d"
    status = "Enabled"
    filter {
      prefix = "raw/"
    }
    transition {
      days          = 90
      storage_class = "GLACIER"
    }
  }
}

########################################################################
# Networking — default VPC for RDS only (Lambda is outside VPC)
########################################################################

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

resource "aws_db_subnet_group" "rds" {
  name       = "${local.name_prefix}-rds"
  subnet_ids = data.aws_subnets.default.ids
}

resource "aws_security_group" "rds" {
  name        = "${local.name_prefix}-rds"
  description = "RDS access — publicly accessible for dev (password + SSL)"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "Postgres from anywhere (dev: protected by password + SSL)"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

########################################################################
# RDS — single-AZ t4g.micro Postgres
########################################################################

resource "aws_db_instance" "main" {
  identifier              = "${local.name_prefix}-db"
  engine                  = "postgres"
  engine_version          = "16.4"
  instance_class          = "db.t4g.micro"
  allocated_storage       = 20
  storage_type            = "gp3"
  storage_encrypted       = true
  db_name                 = "suepercharge"
  username                = "postgres"
  password                = var.db_password
  publicly_accessible     = true
  multi_az                = false
  db_subnet_group_name    = aws_db_subnet_group.rds.name
  vpc_security_group_ids  = [aws_security_group.rds.id]
  backup_retention_period = 7
  skip_final_snapshot     = var.env != "prod"
  deletion_protection     = var.env == "prod"
}

locals {
  database_url = "postgresql+psycopg://${aws_db_instance.main.username}:${var.db_password}@${aws_db_instance.main.address}:${aws_db_instance.main.port}/${aws_db_instance.main.db_name}"
}

########################################################################
# IAM for Lambda
########################################################################

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${local.name_prefix}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "lambda_s3" {
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.artifacts.arn}/*"]
  }
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.artifacts.arn]
  }
}

resource "aws_iam_role_policy" "lambda_s3" {
  name   = "${local.name_prefix}-lambda-s3"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda_s3.json
}

########################################################################
# Lambda — ingest agent
########################################################################

resource "aws_lambda_function" "ingest" {
  function_name    = "${local.name_prefix}-ingest"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "agents.ingest.handler"
  timeout          = 300
  memory_size      = 512
  filename         = var.lambda_package_path
  source_code_hash = filebase64sha256(var.lambda_package_path)

  environment {
    variables = {
      DATABASE_URL      = local.database_url
      S3_BUCKET         = aws_s3_bucket.artifacts.bucket
      ANTHROPIC_API_KEY = var.anthropic_api_key
    }
  }
}

########################################################################
# EventBridge Scheduler — ingest every 15 minutes
########################################################################

resource "aws_iam_role" "scheduler" {
  name = "${local.name_prefix}-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "scheduler_invoke" {
  name = "${local.name_prefix}-scheduler-invoke"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.ingest.arn
    }]
  })
}

resource "aws_scheduler_schedule" "ingest" {
  name                         = "${local.name_prefix}-ingest"
  schedule_expression          = "rate(15 minutes)"
  schedule_expression_timezone = "UTC"
  flexible_time_window { mode = "OFF" }

  target {
    arn      = aws_lambda_function.ingest.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ source = "scheduler" })
  }
}

########################################################################
# Outputs
########################################################################

output "s3_bucket" {
  value = aws_s3_bucket.artifacts.bucket
}

output "rds_endpoint" {
  value     = aws_db_instance.main.address
  sensitive = true
}

output "rds_security_group_id" {
  value = aws_security_group.rds.id
}

output "lambda_name" {
  value = aws_lambda_function.ingest.function_name
}
