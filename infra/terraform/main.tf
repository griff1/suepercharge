########################################################################
# Suepercharge MVP — AWS footprint
#
# Four services (per plan §2):
#   1. Lambda    — 3 agents + 1 Meta lead webhook handler
#   2. RDS       — single t4g.micro Postgres, single-AZ
#   3. S3        — raw press releases + generated creative assets
#   4. EventBridge Scheduler — cron triggers for each agent
#
# We deliberately do NOT provision Secrets Manager, API Gateway, VPC
# endpoints, DynamoDB, or Step Functions. Upgrade when revenue justifies it.
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
# Networking — default VPC + default subnets for RDS (MVP simplicity)
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
  description = "Allow Lambda -> RDS 5432"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_security_group" "lambda" {
  name        = "${local.name_prefix}-lambda"
  description = "Lambda egress to RDS + internet (via NAT in default VPC)"
  vpc_id      = data.aws_vpc.default.id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group_rule" "rds_from_lambda" {
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = aws_security_group.rds.id
  source_security_group_id = aws_security_group.lambda.id
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
  publicly_accessible     = false
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
# IAM for Lambdas
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

resource "aws_iam_role_policy_attachment" "lambda_vpc" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
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
# Lambda — shared deployment package, four functions
########################################################################

locals {
  common_env = {
    DATABASE_URL              = local.database_url
    S3_BUCKET                 = aws_s3_bucket.artifacts.bucket
    ANTHROPIC_API_KEY         = var.anthropic_api_key
    IDEOGRAM_API_KEY          = var.ideogram_api_key
    ARCADS_API_KEY            = var.arcads_api_key
    META_APP_ID               = var.meta_app_id
    META_APP_SECRET           = var.meta_app_secret
    META_ACCESS_TOKEN         = var.meta_access_token
    META_AD_ACCOUNT_ID        = var.meta_ad_account_id
    META_PAGE_ID              = var.meta_page_id
    META_WEBHOOK_VERIFY_TOKEN = var.meta_webhook_verify_token
    SLACK_BOT_TOKEN           = var.slack_bot_token
    SLACK_CHANNEL_ID          = var.slack_channel_id
    SLACK_SIGNING_SECRET      = var.slack_signing_secret
  }

  # Each function => (handler, timeout_s, memory_mb)
  functions = {
    ingest = {
      handler = "agents.ingest.handler"
      timeout = 300
      memory  = 512
    }
    creative = {
      handler = "agents.creative.handler"
      timeout = 600
      memory  = 1024
    }
    campaign = {
      handler = "agents.campaign.handler"
      timeout = 300
      memory  = 512
    }
    webhook = {
      handler = "agents.campaign.webhook_handler"
      timeout = 30
      memory  = 256
    }
  }
}

resource "aws_lambda_function" "agents" {
  for_each = local.functions

  function_name    = "${local.name_prefix}-${each.key}"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = each.value.handler
  timeout          = each.value.timeout
  memory_size      = each.value.memory
  filename         = var.lambda_package_path
  source_code_hash = filebase64sha256(var.lambda_package_path)

  vpc_config {
    subnet_ids         = data.aws_subnets.default.ids
    security_group_ids = [aws_security_group.lambda.id]
  }

  environment {
    variables = local.common_env
  }
}

# Public HTTPS endpoint for the Meta lead webhook. Function URL = zero-config
# alternative to API Gateway, per plan §2.
resource "aws_lambda_function_url" "webhook" {
  function_name      = aws_lambda_function.agents["webhook"].function_name
  authorization_type = "NONE"
}

########################################################################
# EventBridge Scheduler — cron triggers
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
      Resource = [for f in aws_lambda_function.agents : f.arn if f.function_name != aws_lambda_function.agents["webhook"].function_name]
    }]
  })
}

locals {
  schedules = {
    ingest   = "rate(15 minutes)"
    creative = "rate(5 minutes)"
    campaign = "rate(15 minutes)"
  }
}

resource "aws_scheduler_schedule" "agents" {
  for_each = local.schedules

  name                         = "${local.name_prefix}-${each.key}"
  schedule_expression          = each.value
  schedule_expression_timezone = "UTC"
  flexible_time_window { mode = "OFF" }

  target {
    arn      = aws_lambda_function.agents[each.key].arn
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

output "meta_webhook_url" {
  description = "Public HTTPS URL to register with Meta as the lead webhook callback."
  value       = aws_lambda_function_url.webhook.function_url
}

output "lambda_names" {
  value = { for k, f in aws_lambda_function.agents : k => f.function_name }
}
