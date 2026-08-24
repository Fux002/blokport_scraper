# Per-brand deployment config

One codebase, one shared image, one Terraform root. A brand is a **tfvars file**, not
a code change. `brand` selects identity + bucket + SSM namespace + state key; `domain_pack`
selects the product type (stone/wood/lime); `dev_enabled` / `prod_staging_bucket` decide
which stacks actually stand up.

## AWS footprint (intended steady state)

| Brand | Pack | Dev | Prod | AWS resources today |
|-------|------|-----|------|---------------------|
| **blokport** | stone | ✅ the one shared dev | ✅ the one active prod (once `prod_staging_bucket` is filled) | dev live; prod applies when filled |
| **wudport** | wood | ❌ (`dev_enabled=false`) | ⏸ standby (`prod_staging_bucket=""`) | **none** until launch |
| **calcport** | lime | ❌ (`dev_enabled=false`) | ⏸ standby (`prod_staging_bucket=""`) | **none** until launch |

So the running footprint is **dev + one prod (blokport)**; wudport/calcport are declared and
ready but consume **zero** AWS until their `*.tfvars` are filled and applied.

## Standing a brand up

Each brand has its **own** Terraform state (a backend block can't take a variable, so the
key is passed at init):

```bash
cd infra
terraform init -reconfigure \
  -backend-config="bucket=<brand>-tfstate" \
  -backend-config="key=<brand>/scraper/terraform.tfstate"
terraform plan  -var-file=brands/<brand>.tfvars   # MUST show no destroy/recreate on an existing stack
terraform apply -var-file=brands/<brand>.tfvars
```

- **blokport** → `brands/blokport.tfvars` (dev is already applied; fill the prod block to add prod).
- **wudport / calcport** → `brands/<brand>.tfvars`. As written they apply **nothing** (standby).
  Filling the `FILL` values launches **prod only** (no dev).

## Before a non-blokport brand can actually produce

Standing up the stack is the easy half. A wood/lime brand also needs (real onboarding):

- its **domain pack** filled out (`config/domains/wood.yaml` / `lime.yaml` are skeletons),
- its **own supplier scrapers + adapters** (a source = fetcher + adapter + config entry),
- its **platform** (VPC / cluster / `<brand>/<env>/terraform.tfstate`) and `<brand>-tfstate` bucket,
- its prod **SSM** params (`/<brand>-prod/FAL_KEY`, proxy, tokens) + a `<brand>-prod-staging-*` bucket.

Runtime env vars for a deployment are injected by Terraform from these tfvars; the full list
(and how to run locally) is in the repo-root `.env.template`.
