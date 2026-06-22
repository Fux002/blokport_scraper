# Scraper infrastructure (Terraform)

**ONE deployment** (not a dev/prod pair). A single scheduled Fargate task that runs
inside the Medusa **dev** VPC/cluster. Which environment a *run* targets — the dev
or prod **staging bucket** — is chosen at run time via `BLOKPORT_ENV` /
`BLOKPORT_S3_BUCKET`, not at deploy time. The task role is granted write to both
staging buckets (S3 is regional, not VPC-bound), so the one task can target either.

This is a **sibling stack** to the Medusa platform (`blokport_backend/terraform`):
it does **not** modify it — it reads the cluster name from that stack's remote state
and looks up the VPC/subnets/OIDC provider by name/tag, then creates only the
scraper's own resources.

```
infra/
├── *.tf                 the single stack (backend, providers, variables, main, outputs)
└── modules/scraper/     ECR, scheduled Fargate task, IAM, SG, OIDC deploy role
```

## Creates
- ECR repo `blokport-scraper`
- A Fargate **task definition** (`blokport-scraper`) running `deploy/run_pipeline.sh`,
  with env defaulting to the **dev** target
- An **EventBridge Scheduler** cron (starts **disabled**)
- IAM **task role** (write the dev + prod staging buckets) + **execution role**
- A security group (egress only) in the dev VPC
- A **GitHub OIDC deploy role** (`blokport-scraper-gha-deploy`) for CI

## Reuses (read-only)
The dev VPC + private subnets (`blokport-dev-vpc`, subnets tagged `Tier=private`),
the dev ECS cluster (from the platform's `ecs_cluster_name` remote-state output),
the account GitHub OIDC provider, and the state bucket/lock (under key
`blokport/scraper/terraform.tfstate`).

## The prod bucket is TBD
`prod_staging_bucket` defaults to empty — until the prod S3 bucket is shared, the
task only has dev access. Set it in `terraform.tfvars` later to grant prod access.

## Apply
```bash
cd infra
terraform init
terraform apply                    # schedule starts DISABLED
terraform output deploy_role_arn   # -> repo secret AWS_DEPLOY_ROLE_ARN
```

## Run a job
```bash
# Default (dev target):
aws ecs run-task --cluster blokport-dev --launch-type FARGATE \
  --task-definition blokport-scraper \
  --network-configuration "awsvpcConfiguration={subnets=[<private-subnets>],securityGroups=[<scraper-sg>],assignPublicIp=DISABLED}"

# Target PROD instead (once prod_staging_bucket is set), by overriding env:
#   --overrides '{"containerOverrides":[{"name":"scraper","environment":[
#     {"name":"BLOKPORT_ENV","value":"production"},
#     {"name":"BLOKPORT_S3_BUCKET","value":"<prod-staging-bucket>"}]}]}'
```
`terraform output` gives `private_subnet_ids` and `security_group_id`.
