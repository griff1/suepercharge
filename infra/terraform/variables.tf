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

variable "gemini_api_key" {
  description = "Google Gemini API key from aistudio.google.com"
  type        = string
  sensitive   = true
}

variable "lambda_package_path" {
  description = "Path to the zipped Lambda deployment package (built by scripts/build_lambda.sh)."
  type        = string
  default     = "../../lambda-packages/suepercharge.zip"
}
