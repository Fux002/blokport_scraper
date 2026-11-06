# Deploying the scraper on AWS

Environment variables use the neutral `SCRAPER_` prefix (infra sets these); the legacy `BLOKPORT_` prefix is still read as a fallback, `SCRAPER_` winning when both are set (`stone_pipeline/core/env.py`).

**TWO deployments from ONE image** — a dedicated **dev** task and a dedicated
**prod** task. The dev task runs in the Medusa **dev** VPC/cluster (`blokport-dev`)
and writes only the **dev** staging bucket; the prod task runs in the **prod**
VPC/cluster (`blokport-prod`) and writes only the **prod** staging bucket. Each
task is **hard-wired** to its environment (`SCRAPER_ENV` is fixed, no runtime
toggle), and each task's IAM role is **scoped to its own bucket only** — so a dev
run physically cannot write prod, and vice versa. The scraper is decoupled from
Medusa: it writes cleaned/enhanced product images to that env's private staging
bucket and pushes the CSVs to S3; Medusa's own import then reads the staging links.

**Why the two can't get mixed up** (the guarantees, strongest first):
1. **Per-bucket IAM** — `scraper-dev`'s task role can write *only* the dev bucket,
   `scraper-prod`'s *only* the prod bucket. A misconfigured env var can't cross over.
2. **No runtime toggle** — `SCRAPER_ENV` is fixed per task (prod can never default
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
  (adds the CPU torch enhance stack) and `gpu` (CUDA torch, the AWS Batch enhancer) targets.
- `deploy/run_pipeline.sh`: the image entrypoint for the GPU Batch jobs and the ad-hoc task; it dispatches on
  `RUN_MODE` and refuses an unset mode or `pipeline` (exit 2). The produce runs in the sync service, not here.
- `infra/` — Terraform: one stack, shared ECR + CI role, and `modules/scraper`
  instantiated twice (`scraper_dev`, `scraper_prod`); see `infra/README.md`.
- `.github/workflows/` — `ci.yml` (pytest) + `deploy.yml` (OIDC → build → push to ECR).

## Config is env-driven (fixed per task)
Each task has its environment **baked in** by Terraform — `scraper-dev` runs
`SCRAPER_ENV=development` + the dev bucket, `scraper-prod` runs
`SCRAPER_ENV=production` + the prod bucket (both with `SCRAPER_IMAGE_MODE=s3`,
`SCRAPER_IMAGE_PROCESSING=true`). There is **no runtime flip** anymore: the two are
separate task definitions in separate clusters, each scoped by IAM to its own
bucket. Images land at `s3://<staging>/<env>/products/improved/<source>/<hash>.jpg`
and the product CSV links to that full https URL. See `config/settings.py` +
`DEV_PROD_PIPELINE.md`. A produce is started from the admin's Run (`POST /config/v1/run` on the sync
service). The ad-hoc task definition (`blokport-scraper-<env>`) runs `deploy/run_pipeline.sh`, which needs a
`RUN_MODE` (unset exits 2); none of its modes runs on the `:core` image that task uses (they need the GPU
images' torch and models and run as AWS Batch jobs), so it is not a way to produce.

## Sequencing guarantee: images are ready before products load
The image re-host runs **synchronously inside the produce, before the product CSV exists**, so a product
can never be loaded referencing an unprocessed or not-yet-uploaded image (layout: `stone_pipeline/io/imagestore.py`):

```
produce (sync service, POST /config/v1/run)
  -> Stage 7: download -> size-reduce -> UPLOAD to s3://<staging>/<env>/products/improved/<src>/<hash>.jpg
              (the raw original to <env>/products/scraped/<src>/<hash>.jpg, the photo process's input)
  -> only THEN emit the product CSV, with image URLs pointing at the uploaded files
  -> catalog -> bundle to_upload/
photos stage (after a produce when SCRAPER_STANDARDISE_AUTO is on, or the admin's Run for photos)
  -> publishes the standardised photos to <env>/products/standard/<view>/<src>/<hash>.jpg; a product of a
     source whose image pipeline is on links its standardised photo once published
[separate, later, Blokport] -> import the CSV into Medusa
```

The produce **stages** the CSV + images and stops; importing into Medusa is a separate step, so "processing
completes before load" holds by construction. A product that has no standardised photo yet is served qty 0
with reason `no_photo` when `SCRAPER_REQUIRE_STANDARD_PHOTO` is on (the production default), until its photo
is published.

**INVARIANT, do not break:** this only holds when the real run has `SCRAPER_S3_DRY_RUN=false` **and**
`SCRAPER_IMAGE_MODE=s3`. With `dry_run=true` or `mode=passthrough`, Stage 7 *derives* image URLs **without
uploading**, so the CSV would link to objects that don't exist. Those are the safe DEV/no-network defaults;
never let them leak into a real upload run. The sync service's task definition sets both
(`infra/modules/sync_service/main.tf`, the config container, which runs the produce).

## First deploy
```bash
# 1. Infra (one stack)
cd infra
terraform init && terraform apply          # creates the dev task definition + sync service
terraform output deploy_role_arn           # copy for step 2

# 2. CI auth: GitHub repo Settings → Secrets and variables → Actions →
#    add repo secret  AWS_DEPLOY_ROLE_ARN = <the output above>

# 3. First image: merge to `main`. When CI on main is green, the Deploy workflow builds
#    main's head and pushes blokport-scraper:core + :<sha>, then rolls the dev sync service.

# 4. Wait for the dev sync service to be stable on it, then start a produce from the admin's Run
#    (POST /config/v1/run on the sync service). There is no produce schedule: a produce is started by hand.
aws ecs wait services-stable --cluster "$(terraform output -raw dev_cluster_name)" \
  --services blokport-scraper-svc-development
```

With `prod_staging_bucket` empty (the default), **only the dev task is created** — the
prod instance is live (stood up 2026-09-02; pinned by `prod_image_tag` in `infra/brands/blokport.tfvars`).

## Rolling a dev deploy (the mutable `:core` gotcha -- now automatic)
Dev tracks the **mutable** `:core` tag, and the running service's task def points at `:core`. So a merge
to `main` rebuilds and pushes a NEW `:core` image, but the task def does not change -- ECS would see "no
change" and never restart the running task, leaving the new code in ECR but **not live**.

The Deploy workflow now closes this automatically: after the `:core` push, the **"Roll dev service"**
step (`.github/workflows/deploy.yml`) runs on the Deploy that CI's completion on `main` starts, and only when it
built main's head (never on a manual dispatch). It runs `update-service --force-new-deployment`
and `wait services-stable`, so a green Deploy means the task ACTUALLY rolled, not just that the image was
pushed. This is safe: `config.db` (sources + review decisions) and the ledger restore from their S3
snapshots on restart, so nothing is lost. The CI deploy role is granted `ecs:UpdateService` (scoped to
ONLY the dev scraper service) + `ecs:DescribeServices` in `infra/main.tf`.

If you ever need to roll by hand (e.g. a manual `imageproc`/`gpu` dispatch, which deliberately does NOT
roll the app task):
```bash
aws ecs update-service --cluster blokport-dev-cluster \
  --service blokport-scraper-svc-development --force-new-deployment
aws ecs wait services-stable --cluster blokport-dev-cluster --service blokport-scraper-svc-development
# verify: the running task's image digest must match :core in ECR
aws ecr describe-images --repository-name blokport-scraper \
  --image-ids imageTag=core --query 'imageDetails[0].imageDigest' --output text
```

## Promoting an image to prod (pinning)
Prod is its own stack in the prod cluster, stood up 2026-09-02. It runs only immutable, dev-proven tags, set in
`infra/brands/blokport.tfvars` (`variables.tf` refuses a mutable or empty tag while prod is enabled):

- `prod_image_tag = "<sha>"`: the sync service image, the 40-hex git-sha tag the Deploy workflow pushed.
- `prod_gpu_image_tag = "gpu-<sha>"`: the texture job's GPU image (a Deploy dispatch with `build_gpu`).
- `prod_standardise_image_tag = "standardise-pin-<sha12>"`: the photo process's GPU image
  (`deploy/build_standardise_image.sh` pushes it under that pin tag; see STANDARDISE_PROCESS.md).

**Pinning = adding the image's pin tag in ECR, before the apply.** The ECR lifecycle (`infra/main.tf`) keeps
pinned images by their pin tag, in rules that rank above every build and churn rule, so no build or churn
rule can expire a pinned image; each kind keeps its newest pins (the current one and its rollbacks):

| Pinned image | Pin tag to add | Kept |
|---|---|---|
| sync service (`<sha>`) | `prod-<sha12>` | newest 20 |
| texture GPU (`gpu-<sha>`) | `prod-gpu-<sha12>` | newest 5 |
| photo process GPU | `standardise-pin-<sha12>` (pushed under it) | newest 20 |

`<sha12>` is the first 12 hex digits of the image's commit sha, the form every pin tag uses (the current prod
image carries `prod-d11da5b62972`).

The images are OCI indexes: add a tag by re-putting the image's EXACT manifest (boto3 `batch_get_image` with
`acceptedMediaTypes` including `application/vnd.oci.image.index.v1+json`, then `put_image` with that
`imageManifest`, its `imageManifestMediaType`, the new `imageTag` and the source `imageDigest`), and check that
the new tag's digest equals the source tag's. Re-serialising the manifest (the CLI's `--output text` round
trip) yields a different digest, a tag on an image nothing runs.

Then apply the prod modules the change touches with the brand's tfvars, e.g.
`terraform apply -var-file=brands/blokport.tfvars -target=module.sync_service_prod -target=module.gpu_enhance_prod`.

The plan checks the pins (`infra/main.tf`, `data.aws_ecr_image.prod_service` / `prod_gpu`): each prod image is looked
up by its configured tag and must carry `prod-<sha12>` / `prod-gpu-<sha12>` on that same image, or the plan fails
with "is not pinned: add the tag ... to that same image". The prod modules take their tags through these lookups,
so a targeted plan of a prod module runs the check too. The standardise image needs no check: its tfvars name its
pin tag itself.

## Standardised photos
Supplier logos and watermarks are taken off per photo by the standardised-photo process
(STANDARDISE_PROCESS.md: the judge finds them, the hosted clean step removes them). A source opts
out of the process with its `image_pipeline` switch in the admin.

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
Fargate (the sync service, always on; ad-hoc tasks pay per run) + S3 + ECR + CloudWatch
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

### S3 layout: two DELIBERATE prefixes per environment (regenerable vs durable)

Each environment uses **two** top-level S3 prefixes, on purpose, split by lifecycle. This is a
designed boundary, not an accident of naming — each prefix is owned by a different subsystem:

```
dev/          (ENV_SEGMENT = dev|prod)  -- REGENERABLE data + the Blokport/Medusa integration surface
  products/          hosted product images (scraped/ improved/ standard/) -- wiped by a factory reset unless keep_images
  variations/        variant textures (kept across a factory reset)
  scraper/
    from_medusa/     INPUT  the Medusa export the matcher reads
    to_upload/       OUTPUT the upload set Blokport imports
    review/          OUTPUT look-before-upload files
    texture_queues/  transient texture-generation work queues
  A produce rebuilds ALL of this; a factory reset may wipe dev/products/. Nothing here is irreplaceable.

development/  (ENV_NAME = development|production)  -- DURABLE state that MUST survive a cold task
  scraper/
    config/config.db    source lifecycle (pause/delist) + the operator overlay (curation decisions)
    ledger/<env>.db     sync state (what Medusa already has)
    artifacts/*.tar.gz  data/ + outputs/ backups for a fast cold-start restore
  A produce does NOT regenerate this; the snapshot layer (ledger/snapshot.py) owns it, restoring on a cold task.
```

**Rule of thumb:** if a produce can rebuild it, or Blokport/Medusa read it, it goes under `ENV_SEGMENT`
(`dev/`|`prod/`). If losing it loses real state (decisions, sync history), it goes under `ENV_NAME`
(`development/`|`production/`). Prod mirrors this exactly, in the SEPARATE prod bucket. The images
(`products/`, `variations/`) are siblings of `scraper/` under `ENV_SEGMENT`, not inside it.

The run wires both ends automatically:
- **start:** `deploy/fetch_inputs.py` downloads `from_medusa/` from S3 into the task.
- **end:** `deploy/upload_artifacts.py` uploads `to_upload/` + `review/` back to S3.

So you **maintain the export** by dropping a fresh Medusa export into
`<env>/scraper/from_medusa/` after each Medusa import (or seed it once — already done
for dev). If it's absent, the matcher just treats every variant as new. Auto-fetching
it straight from the Medusa admin API is a later option (the `MedusaApiSink` stub exists).
```
