# Scraper infrastructure (Terraform)

**TWO deployments from one image** — a dedicated **dev** task (runs in the Medusa
`blokport-dev` VPC/cluster, writes only the dev staging bucket) and a dedicated
**prod** task (runs in `blokport-prod`, writes only the prod bucket). Each task is
hard-wired to its environment (`BLOKPORT_ENV` fixed, no runtime toggle) and its IAM
role is **scoped to its own bucket only**, so the two environments cannot mix. The
prod task is **count-gated**: it is created only once `prod_staging_bucket` is set.

This is a **sibling stack** to the Medusa platform (`blokport_backend/terraform`):
it does **not** modify it — it reads each env's cluster name from that env's remote
state and looks up the VPC/subnets/OIDC provider by name/tag, then creates only the
scraper's own resources.

```
infra/
├── *.tf                 root: shared ECR + GitHub OIDC deploy role + two module instances
└── modules/scraper/     ONE env: scheduled Fargate task, IAM (its bucket only), SG, schedule
```

## Creates
- **Shared:** ECR repo `blokport-scraper`; GitHub OIDC deploy role
  `blokport-scraper-gha-deploy` (CI builds + pushes the one image).
- **Per env** (`-development` / `-production` suffix), via `module.scraper_dev` and
  `module.scraper_prod`:
  - a Fargate **task definition** (`blokport-scraper-<env>`) running `deploy/run_pipeline.sh`,
    with `BLOKPORT_ENV` fixed to that env;
  - an **EventBridge Scheduler** cron (starts **disabled**);
  - IAM **task role scoped to that env's staging bucket ONLY** + an execution role;
  - a security group (egress only) in that env's VPC.

## Reuses (read-only, per env)
The env's VPC + private subnets (`blokport-<env>-vpc`, subnets tagged `Tier=private`),
the env's ECS cluster (from that platform's `ecs_cluster_name` remote-state output),
the account GitHub OIDC provider, and the state bucket/lock (this stack's state at
`blokport/scraper/terraform.tfstate`).

## Dev-only for now; prod is cloned later
`prod_staging_bucket` defaults to empty → `module.scraper_prod` has `count = 0`, so a
`terraform apply` today creates **only the dev task** (plus the shared ECR + CI role).
Stand prod up later by setting `prod_staging_bucket` (and ensuring the `blokport-prod`
platform stack exists) — see `DEPLOY.md`.

## Apply (dev)
```bash
cd infra
terraform init
terraform apply                        # only dev is created; schedule starts DISABLED
terraform output deploy_role_arn       # -> repo secret AWS_DEPLOY_ROLE_ARN
```

## Run the dev job
```bash
aws ecs run-task --cluster blokport-dev --launch-type FARGATE \
  --task-definition blokport-scraper-development \
  --network-configuration "awsvpcConfiguration={subnets=[<dev-private-subnets>],securityGroups=[<dev-scraper-sg>],assignPublicIp=DISABLED}"
```
`terraform output` gives `dev_private_subnet_ids` and `dev_security_group_id` (and the
`prod_*` equivalents once prod is enabled).
