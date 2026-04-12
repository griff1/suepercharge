# Terraform — Suepercharge MVP infra

Four AWS resource groups: Lambda (×4), RDS Postgres, S3, EventBridge Scheduler.

## Prereqs

- Terraform >= 1.6
- AWS credentials with permission to create the above (a `PowerUserAccess` IAM user is fine for MVP)
- A built Lambda zip at `../../lambda-packages/suepercharge.zip` — see `scripts/build_lambda.sh`

## Usage

```sh
cd infra/terraform
terraform init
terraform plan -var-file=dev.tfvars
terraform apply -var-file=dev.tfvars
```

`dev.tfvars` should set every `sensitive` variable in `variables.tf`. Never commit it.

## What gets created

- `suepercharge-dev-db` — RDS t4g.micro single-AZ, default VPC
- `suepercharge-dev-artifacts-<suffix>` — S3 bucket, versioned, encrypted, private
- `suepercharge-dev-{ingest,creative,campaign,webhook}` — 4 Lambdas
- `suepercharge-dev-{ingest,creative,campaign}` schedules — EventBridge Scheduler rules
- A public Lambda Function URL for the Meta lead webhook

## After first apply

1. Copy `meta_webhook_url` output into the Meta App's lead webhook config.
2. Run migrations from a machine with DB connectivity:
   ```sh
   DATABASE_URL="<rds url>" uv run alembic upgrade head
   ```
   (The RDS instance is in the default VPC with no public access, so run this from a bastion, EC2, or via `aws ssm start-session` tunnel.)

## Upgrade path when revenue justifies it

- Move secrets from Lambda env → Secrets Manager
- Add multi-AZ to RDS
- Put Lambdas behind a private VPC endpoint
- Add CloudWatch alarms on Lambda errors + RDS CPU
