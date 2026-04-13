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

variable "groq_api_key" {
  description = "Groq API key from console.groq.com"
  type        = string
  sensitive   = true
}

variable "lambda_code_hash" {
  description = "Base64-encoded SHA256 of the Lambda zip. Computed from the local file."
  type        = string
  default     = ""
}
