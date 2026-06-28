# Deploying the scraper on AWS

**TWO deployments from ONE image** — a dedicated **dev** task and a dedicated
**prod** task. The dev task runs in the Medusa **dev** VPC/cluster (`blokport-dev`)
and writes only the **dev** staging bucket; the prod task runs in the **prod**
VPC/cluster (`blokport-prod`) and writes only the **prod** staging bucket. Each
task is **hard-wired** to its environment (`BLOKPORT_ENV` is fixed, no runtime
toggle), and each task's IAM role is **scoped to its own bucket only** — so a dev
run physically cannot write prod, and vice versa. The scraper is decoupled from
Medusa: it writes cleaned/enhanced product images to that env's private staging
bucket and pushes the CSVs to S3; Medusa's own import then reads the staging links.

**Why the two can't get mixed up** (the guarantees, strongest first):
1. **Per-bucket IAM** — `scraper-dev`'s task role can write *only* the dev bucket,
   `scraper-prod`'s *only* the prod bucket. A misconfigured env var can't cross over.
2. **No runtime toggle** — `BLOKPORT_ENV` is fixed per task (prod can never default
   to dev). S3 keys are namespaced `dev/…` vs `prod/…`, Medusa ids are per-env.
3. **Separate VPC/cluster** — the prod task runs in the prod platform, not dev.
4. **Promote the SAME image** — both tasks pull from one ECR repo. Use an immutable
   **git-sha tag** and set `prod_image_tag` to the dev-proven sha (do NOT leave both
   on the mutable `core` tag, or pushing a new dev image silently moves prod too).

> **Standing prod up** (the `scraper_prod` instance is created only when these are set):
> 1. Set `prod_staging_bucket` to the prod bucket name (you provide it).
> 2. Ensure the **prod platform stack exists** — `blokport-prod-vpc` (tagged
>    `Tier=private` subnets) and remote state at `blokport/prod/terraform.tfstate`
>    exposing `ecs_cluster_name`. The prod task reads these via `prod_home_env`
>    (default `prod`). If the prod Medusa platform isn't stood up yet, the prod
>    task can't be created — leave `prod_staging_bucket` empty until it is.
> 3. `terraform apply` then `terraform plan` to confirm only the prod resources are added.

```
EventBridge (cron) ─▶ Fargate task: scrape ─▶ pipeline (Stage 7 stages images to S3)
                                       └▶ catalog ─▶ push to_upload/*.csv to S3
                                                       │
   you: download CSVs ─▶ import into Medusa ─▶ refresh variants export ─▶ `tree`
```

## Pieces (all in this repo)
- `Dockerfile` — `core` (scrape+pipeline+CPU image enhancement) and `imageproc`
  (adds de-watermark torch stack) targets.
- `deploy/run_pipeline.sh` — container entrypoint (`scrape → run → catalog → upload`).
- `infra/` — Terraform: one stack, shared ECR + CI role, and `modules/scraper`
  instantiated twice (`scraper_dev`, `scraper_prod`); see `infra/README.md`.
- `.github/workflows/` — `ci.yml` (pytest) + `deploy.yml` (OIDC → build → push to ECR).

## Config is env-driven (fixed per task)
Each task has its environment **baked in** by Terraform — `scraper-dev` runs
`BLOKPORT_ENV=development` + the dev bucket, `scraper-prod` runs
`BLOKPORT_ENV=production` + the prod bucket (both with `BLOKPORT_IMAGE_MODE=s3`,
`BLOKPORT_IMAGE_PROCESSING=true`). There is **no runtime flip** anymore: the two are
separate task definitions in separate clusters, each scoped by IAM to its own
bucket. Images land at `s3://<staging>/<env>/products/improved/<source>/<hash>.jpg`
and the product CSV links to that full https URL. See `config/settings.py` +
`DEV_PROD_PIPELINE.md`. To run one by hand:
`aws ecs run-task --cluster <env_cluster> --task-definition blokport-scraper-<env>`.

## Sequencing guarantee: images are ready before products load
Image cleanup/upscale runs **synchronously inside the run, before the product CSV
exists** — there is no async path, so a product can never be loaded referencing an
unprocessed or not-yet-uploaded image:

```
run all → Stage 7: download → process (clean / upscale / de-watermark) → UPLOAD to S3
        → only THEN emit the product CSV, with image URLs pointing at the uploaded files
catalog → bundle to_upload/
[separate, later, you] → import the CSV into Medusa   ← you trigger this; the task does not
```

The scheduled task **stages** the CSV + images and stops; importing into Medusa is a
separate step you run afterward (and after reviewing `images/reports/processed_preview.csv`),
so "processing completes before load" holds by construction.

**INVARIANT — do not break:** this only holds when the real run has
`BLOKPORT_S3_DRY_RUN=false` **and** `BLOKPORT_IMAGE_MODE=s3`. With `dry_run=true` or
`mode=passthrough`, Stage 7 *derives* image URLs **without uploading**, so the CSV would
link to objects that don't exist. Those are the safe DEV/no-network defaults — never let
them leak into a real upload run. The Terraform task sets both correctly
(`infra/modules/scraper/main.tf`); verify them if you run the task by hand.

## First deploy
```bash
# 1. Infra (one stack)
cd infra
terraform init && terraform apply          # schedule starts DISABLED
terraform output deploy_role_arn           # copy for step 2

# 2. CI auth: GitHub repo Settings → Secrets and variables → Actions →
#    add repo secret  AWS_DEPLOY_ROLE_ARN = <the output above>

# 3. First image: push to `main` (or run the Deploy workflow). CI builds the
#    `core` image and pushes blokport-scraper:core.

# 4. Run the DEV task once manually and watch the logs
aws ecs run-task --cluster blokport-dev --launch-type FARGATE \
  --task-definition blokport-scraper-development \
  --network-configuration "awsvpcConfiguration={subnets=[$(terraform output -json dev_private_subnet_ids | jq -r 'join(\",\")')],securityGroups=[$(terraform output -raw dev_security_group_id)],assignPublicIp=DISABLED}"

# 5. When happy, enable the DEV cron:
terraform apply -var dev_schedule_enabled=true
```

With `prod_staging_bucket` empty (the default), **only the dev task is created** — the
prod instance is count-gated to zero, so nothing prod-side exists yet.

## Standing PROD up LATER (after dev is proven AND the prod Medusa platform exists)
Prod is a **separate task in the prod cluster** (`blokport-scraper-production`), not a
runtime override of dev. It needs the prod platform stack (`blokport-prod-vpc`,
`blokport/prod/terraform.tfstate`) and the prod bucket:
```bash
cd infra
# promote the SAME dev-proven image (a git sha), do NOT rebuild for prod:
terraform apply \
  -var prod_staging_bucket=blokport-prod-staging-<hex> \
  -var prod_image_tag=<the dev-proven git sha>
aws ecs run-task --cluster blokport-prod --launch-type FARGATE \
  --task-definition blokport-scraper-production \
  --network-configuration "awsvpcConfiguration={subnets=[$(terraform output -json prod_private_subnet_ids | jq -r 'join(\",\")')],securityGroups=[$(terraform output -raw prod_security_group_id)],assignPublicIp=DISABLED}"
# enable the prod cron when proven:
terraform apply -var prod_staging_bucket=... -var prod_schedule_enabled=true
```

## Enabling de-watermarking (varsha) later
Default `core` = enhancement + Lanczos upscale (CPU). To also strip supplier
watermarks: set `image_tag = "imageproc"` + raise `memory` (~8192), and have CI
build/push the `imageproc` target. Runs on CPU (no GPU); review
`images/reports/processed_preview.csv`.

## Adding & certifying a source (scraper or any product connection)
Every source — a web scraper or any other product data connection — is onboarded
the same way and must **certify** before it's trusted to auto-load:

1. Connect: a web scraper subclasses `ScraperBase` (copy `scrapers/_template.py`);
   a non-scraper connection (partner API, CSV/Excel drop, push feed) just needs to
   yield raw rows in its column shape.
2. Map: add an adapter (`stone_pipeline/adapters/<source>.py`, copy `_standalone_template.py`)
   that maps those raw columns to the canonical schema, plus a golden fixture.
3. Configure: add a `sources.yaml` entry. It starts at **`mode: review`** (quarantined).
4. Certify: `python -m stone_pipeline.certify <source>` until green (config, adapter,
   golden-fixture selftest, contract). CI runs `certify all` on every push, so a
   regression in any source fails the build.
5. Promote: once it's run clean and you've signed off, set `mode: auto` in `sources.yaml`.

A `review` source always stages its output for human sign-off; only `auto` sources
load automatically when the Medusa auto-link is eventually enabled. So full
auto-linking stays safe: new or unproven sources are quarantined by default and can
never silently push bad data live.

## Residential proxy for the Cloudflare-fronted scrapers (varsha etc.)
Cloudflare blocks datacenter IPs, so the Cloudflare-fronted sources (the SlabWare
tenants — varsha, polonine, ferraz, brumagran) fail from the AWS NAT egress even
though they work from a residential IP locally. Route just those through a
**residential proxy**:

1. Get a residential proxy URL (`http://user:pass@host:port`) from a provider.
2. Store it as an SSM SecureString, e.g.:
   `aws ssm put-parameter --name /blokport-dev/SCRAPER_PROXY --type SecureString --value '<url>'`
3. Wire it: `cd infra && terraform apply -var scraper_proxy_ssm_name=/blokport-dev/SCRAPER_PROXY`
   (injects `BLOKPORT_SCRAPER_PROXY` into the task).

`ScraperBase` routes its **curl_cffi** session (only the Cloudflare sources use it)
through `BLOKPORT_SCRAPER_PROXY` when set; unset = direct connection (local default).
The clean sources (marenostone, zucchi) never touch the proxy, so bandwidth cost stays tiny.

## Cost (cheapest working)
Pay-per-run Fargate (a scheduled batch, idle otherwise) + S3 + ECR + CloudWatch
logs ≈ **a few dollars/month**. No NAT/ALB/GPU added — reuses the platform's network.

## Where the import/export files live on S3
The Fargate task is ephemeral, so the scraper's CSV files live in the env's staging
bucket under a single canonical home — `s3://<staging-bucket>/<env>/scraper/`:

```
<env>/scraper/from_medusa/   INPUT  — the Medusa export the matcher reads
                                      (variants_export.csv, attributes.csv)
<env>/scraper/to_upload/     OUTPUT — produced upload set (variants,
                                      2_valid_combinations.csv, products CSVs)
<env>/scraper/review/        OUTPUT — look-before-upload files
```

(dev = `blokport-dev-staging-3e58a6/dev/scraper/…`; prod adds the same tree under
`prod/scraper/…` in the prod bucket once it exists.) Local per-env folders
(`to_upload/<env>/`, `from_medusa/<env>/`) map 1:1 onto these.

The run wires both ends automatically:
- **start:** `deploy/fetch_inputs.py` downloads `from_medusa/` from S3 into the task.
- **end:** `deploy/upload_artifacts.py` uploads `to_upload/` + `review/` back to S3.

So you **maintain the export** by dropping a fresh Medusa export into
`<env>/scraper/from_medusa/` after each Medusa import (or seed it once — already done
for dev). If it's absent, the matcher just treats every variant as new. Auto-fetching
it straight from the Medusa admin API is a later option (the `MedusaApiSink` stub exists).
```
