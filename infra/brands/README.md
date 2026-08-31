# Per-brand deployment config

One codebase, one shared image, one Terraform root. A brand is a **tfvars file**, not
a code change. `brand` selects identity + bucket + SSM namespace + state key; `domain_pack`
selects the product type (stone/wood/lime); `dev_enabled` / `prod_staging_bucket` decide
which stacks actually stand up; `create_ecr` decides repo ownership.

## Adding a brand = two files + init, ZERO code edits

Every knob a new brand needs is a variable, so onboarding never touches a `.tf` resource:

1. **`brands/<brand>.tfvars`** — `brand`, `domain_pack`, `create_ecr = false` (references the shared
   image repo instead of creating it), `dev_enabled = false` (prod-only) or `true`, the prod owner ids +
   bucket + SSM names. Copy an existing file and fill it.
2. **`brands/<brand>.backend.hcl`** — the brand's own state bucket/key/lock (a backend block can't take a
   variable, so state is separated at `init` time, not in code).
3. **`terraform init -reconfigure -backend-config=brands/<brand>.backend.hcl`** then
   **`terraform plan/apply -var-file=brands/<brand>.tfvars`**.

That's the whole infra story. What is NOT declarative (because it is genuine product work, not config):
the brand's **domain pack** (`config/domains/<pack>.yaml`), its **source scrapers + adapters**, and a
**smoke run** to prove the material — see `MULTI_BRAND_LAUNCH.md`. The platform (VPC/cluster/state bucket)
must also exist first. But the scraper stack itself is pure tfvars.

## AWS footprint (intended steady state)

| Brand | Pack | Dev | Prod | AWS resources today |
|-------|------|-----|------|---------------------|
| **blokport** | stone | ✅ the one shared dev | ✅ the one active prod (once `prod_staging_bucket` is filled) | dev live; prod applies when filled |
| **wudport** | wood | ❌ (`dev_enabled=false`) | ⏸ standby (`prod_staging_bucket=""`) | **none** until launch |
| **calcport** | lime | ❌ (`dev_enabled=false`) | ⏸ standby (`prod_staging_bucket=""`) | **none** until launch |

So the running footprint is **dev + one prod (blokport)**; wudport/calcport are declared and
ready but consume **zero** AWS until their `*.tfvars` are filled and applied.

## Standing a brand up

> **Two different state locations — do not conflate them.**
> - **This stack's own state** is per brand (`brands/<brand>.backend.hcl`), passed at init.
> - **The PLATFORM's state**, which this stack only *reads* (`platform_state_bucket`), is NOT per brand:
>   every brand's platform state lives in the single `blokport-tfstate` bucket, separated by key
>   `<brand>/<env>/terraform.tfstate`. wudport-dev is already at
>   `blokport-tfstate/wudport/dev/terraform.tfstate`. Pointing `platform_state_bucket` at a
>   `<brand>-tfstate` bucket that does not exist fails the remote-state read at plan time.

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
