variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "env" {
  description = "Environment name (dev/prod)"
  type        = string
  default     = "dev"
}

variable "db_password" {
  description = "RDS master password. Pass via TF_VAR_db_password or tfvars."
  type        = string
  sensitive   = true
}

# Lambda env secrets. In prod, migrate these to Secrets Manager; for MVP
# they live in Lambda env vars to keep the service count small (per plan §2).
variable "anthropic_api_key" {
  type      = string
  sensitive = true
}

variable "ideogram_api_key" {
  type      = string
  sensitive = true
  default   = ""
}

variable "arcads_api_key" {
  type      = string
  sensitive = true
  default   = ""
}

variable "meta_app_id" {
  type    = string
  default = ""
}

variable "meta_app_secret" {
  type      = string
  sensitive = true
  default   = ""
}

variable "meta_access_token" {
  type      = string
  sensitive = true
  default   = ""
}

variable "meta_ad_account_id" {
  type    = string
  default = ""
}

variable "meta_page_id" {
  type    = string
  default = ""
}

variable "meta_webhook_verify_token" {
  type      = string
  sensitive = true
  default   = ""
}

variable "slack_bot_token" {
  type      = string
  sensitive = true
  default   = ""
}

variable "slack_channel_id" {
  type    = string
  default = ""
}

variable "slack_signing_secret" {
  type      = string
  sensitive = true
  default   = ""
}

variable "lambda_package_path" {
  description = "Path to the zipped Lambda deployment package (built by scripts/build_lambda.sh)."
  type        = string
  default     = "../../lambda-packages/suepercharge.zip"
}
