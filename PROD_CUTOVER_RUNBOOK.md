# Production cutover runbook — Blokport prod scraper standup

One ordered sequence both chats (scraper + Blokport platform/Medusa) work from. Promotion dev→prod is
**config + cross-team prerequisites, never a code edit.** The code fails loud on every value that has no
fallback, so a missed step errors rather than shipping silently. See `TODO_PROD.md` for the checklist form
and `DEV_PROD_PIPELINE.md` for the data-flow story.

**Status (2026-08-25):** prod is NOT provisioned. Nothing exists in prod AWS yet (no state, VPC, cluster,
bucket). The scraper prod stack is fully defined and count-gated on `prod_staging_bucket`; a dev apply never
reads prod state or `/blokport-prod/` params. Launch is gated on the Blokport platform apply (blocked on an
Elastic IP quota increase + real secret values) and then a short scraper apply + texture transplant + verify.
Config now lives in **`infra/brands/blokport.tfvars`** + **`infra/brands/blokport.backend.hcl`** (PR #219); the
old ECR-drift gotcha is RESOLVED — `create_ecr` (default true) owns the shared repo cleanly, no pre-apply cleanup.

## Who does what (the split)

**YOU / Blokport (only you can):** stand up the prod platform (VPC/cluster/state + `blokport-prod-staging` +
`/blokport-prod/` tokens); set up prod Medusa (same attribute vocabulary as dev, sales channel, company,
publishable key, category pcats); send the ids; say "apply"; run the produce + pull loop from the UI.

**ME / command line (all of it):** fill the tfvars/backend from your values; `init` + `plan` (show 0 destroy);
the **one-off texture + base-data transplant** (dev S3 → prod S3, all keys preserved — see Phase 4b); seed +
bootstrap + `seed.verify`; dry-run then real produce; diagnose from S3 throughout. The `apply` and the UI pull
stay YOUR triggers.

**Never copied dev→prod:** `config.db` / `ledger.db` (they carry dev's minted ids + synced state). Prod starts
fresh and mints its own. The base is Id-free; owner ids + attributes come from prod Medusa; only the *textures*
(Key/sha-named, env-agnostic) are transplanted.

---

## Resolved facts (confirmed with the Blokport side)

| Item | Answer | Owner |
|---|---|---|
| Prod platform state | publishes to `blokport/prod/terraform.tfstate` (bucket `blokport-tfstate`) with `vpc_id`, `private_subnet_ids`, `ecs_cluster_arn`, `service_sg_id`, `internal_namespace_id` (+ `ecs_cluster_name`). VPC tagged `blokport-prod-vpc`; private subnets tagged `Tier=private`. **Not applied yet.** | Blokport |
| Prod staging bucket | **`blokport-prod-staging`** (deterministic; media = `blokport-prod-media`). Created by the **platform** `modules/s3` (same controls as dev). Do NOT create it on the scraper side. | Blokport |
| `/blokport-prod/BLOKPORT_SYNC_TOKEN`, `/blokport-prod/BLOKPORT_CONFIG_TOKEN` | Created by the **platform apply**. | Blokport |
| `/blokport-prod/FAL_KEY`, `/blokport-prod/BLOKPORT_SCRAPER_PROXY` | **DONE 2026-08-24** — created as SecureStrings, **same values as dev** (FAL is one hosted-account key; same proxy). Wired via `fal_key_ssm_name` / `scraper_proxy_ssm_name`. | Scraper (us) |
| Prod Medusa ids (`sales_channel_id`, pcat ids, per-source `company_id`s) | Do NOT exist until prod DB is up. Order: platform apply → backend deploy → admin user → `bootstrap.ts` creates sales channel + publishable key → then categories/pcat + per-source company ids in prod admin. Blokport sends the ids after bootstrap. | Blokport → us |
| `from_medusa/production/attributes.csv` | **Auto-publishes** to S3 on any vocab change when `SCRAPER_SYNC_ENABLED=true` (set on prod's first backend task def) — no manual export. `variants_export.csv` follows once products exist. | Blokport (automatic) |
| Prod `/sync/v1` consumer + port_ids | Ships automatically with the backend (prod builds from `main`; `develop→main` brings it). `apply-product.ts` sets origin ports from `port_ids` via `resolvers.existingPortIds(p.port_ids ?? [])` — no country-derived fallback. Review UI present. | Blokport (automatic) |
| Networking | Scraper SG opens ingress **8723 (sync) / 8724 (config)** from the platform `service_sg_id`; Cloud Map resolves `scraper.blokport-prod.internal`. Our `modules/sync_service` wires it. | Scraper module |
| Medusa task-def env | No manual surgery — `SCRAPER_SYNC_URL`, `SCRAPER_CONFIG_URL`, `SCRAPER_SYNC_ENABLED=true` + both tokens are on prod's first task def. | Blokport (automatic) |

**Two decisions (held by us):**
- **FAL key:** same as dev — RESOLVED (done above).
- **Image tag to pin:** pin the **then-current soaked dev `:core-<sha>`** at cutover (see Phase 4) — never the mutable `core` tag, never a sha that will be stale by apply-day. Merge the env-tier guard first if it has landed (see Phase 0).

---

## The ordered sequence

### Phase 0 — Prep (runs in parallel, before the platform apply)
**Blokport:**
- [ ] Elastic IP quota increase (currently 6/5 allocated; each prod stack needs 2 for HA NAT).
- [ ] Gather the real prod secret values the platform apply injects.

**Scraper (us):**
- [x] `/blokport-prod/FAL_KEY` + `/blokport-prod/BLOKPORT_SCRAPER_PROXY` created (same as dev, 2026-08-24).
- [ ] **Merge `fix/env-tier-validation` → `main`** if you want the `BLOKPORT_ENV` allowlist guard (closed-set, raises at import, 13 tests) in the prod image. It is defense-in-depth: our terraform sets `BLOKPORT_ENV=production` deterministically on the prod task def, so the typo risk is already near-zero — but the guard is cheap. If merged, its sha becomes a candidate to pin (after a dev soak). If not merged, ship a soaked pre-guard sha and add the guard in a later routine promotion.
- [x] **ECR drift — RESOLVED (PR #219).** `create_ecr` (default true) count-gates the repo + lifecycle behind a `moved{}` block, and every consumer reads `local.ecr_repo_url/arn`. blokport keeps the existing repo (create_ecr=true); a second brand sets false and references it. No pre-apply cleanup needed; the apply is a pure add.
- [ ] Pre-stage the tfvars values (do NOT set `prod_staging_bucket` yet — that flips `prod_enabled` and the count-gated `platform_prod`/`/blokport-prod/` data sources fail until the platform exists): `fal_key_ssm_name = "/blokport-prod/FAL_KEY"`, `scraper_proxy_ssm_name = "/blokport-prod/BLOKPORT_SCRAPER_PROXY"`, and note the bucket name `blokport-prod-staging`.

### Phase 1 — Blokport platform apply (BLOCKING)
- [ ] Blokport applies the platform stack → creates the prod VPC/cluster, the `blokport-prod-staging` bucket, the sync/config SSM tokens, and publishes `blokport/prod/terraform.tfstate` with the five outputs. **Blocked on the EIP quota + secret values (Phase 0), not on code.**
- Gate: `blokport/prod/terraform.tfstate` present with the outputs, and `/blokport-prod/BLOKPORT_SYNC_TOKEN` + `_CONFIG_TOKEN` exist.

### Phase 2 — Blokport backend deploy
- [ ] `develop → main` merge → prod backend deploys, bringing the `/sync/v1` consumer (port_ids), the review UI, and the first task def (`SCRAPER_SYNC_ENABLED=true` + URLs + tokens).

### Phase 3 — Blokport bootstrap → prod ids
- [ ] Admin user created → `bootstrap.ts` creates the prod **sales channel** + **publishable key**.
- [ ] Categories/pcat + per-source **company ids** configured in the prod admin.
- [ ] **Blokport sends us:** `sales_channel_id`, the Slabs/Blocks/Tiles **pcat ids**, and the per-source **company_id**s.

### Phase 4 — Scraper prod apply (us)
Config lives in `infra/brands/blokport.tfvars` (+ `infra/brands/blokport.backend.hcl` for state). Init with
`terraform init -reconfigure -backend-config=brands/blokport.backend.hcl`, then plan/apply `-var-file=brands/blokport.tfvars`.
- [ ] Set in `infra/brands/blokport.tfvars`:
  - `prod_staging_bucket = "blokport-prod-staging"` (this flips `prod_enabled` and creates ALL prod resources at once — only valid now that Phase 1 is done).
  - `fal_key_ssm_name` / `scraper_proxy_ssm_name` (from Phase 0).
  - `prod_image_tag = "<the :core-<sha> currently deployed AND soaked in dev at this moment>"` — record the exact sha + digest here; pin it, never `core`.
  - `prod_gpu_image_tag = "gpu-<sha>"` (the dev-proven GPU build).
  - `prod_schedule_enabled = false`, `prod_auto_enhance = false`, `prod_auto_texture = false`, `prod_require_enhanced = false` (quiet start).
  - `alert_email = "<ops email>"` — **required before a full apply**, else the apply removes the dev SNS alert the earlier targeted apply created.
- [ ] `terraform plan` — with the platform state present it should CREATE the prod services (`scraper_prod`, `gpu_enhance_prod`, `sync_service_prod`, prod data sources, Batch queue/jobdef). Confirm **0 destroy** and no unexpected ECR churn (see the drift reconcile in Phase 0).
- [ ] `terraform apply` → confirm the prod ECS services (incl. `sync_service_prod`) + the Batch queue/jobdef come up. Confirm the revision-agnostic `batch:SubmitJob` IAM (`:*`) is on the prod roles (mirrors the dev fix).

### Phase 4b — One-off texture + base-data transplant (us, command line)
The variant textures + de-watermark set are **expensive** (GPU + FAL) and **env-agnostic** — every image is named
by its variety **Key** (`{Key}.png`, a deterministic uuid5 from the Id-free variety data) or a content **sha**,
never a Medusa id. So they transplant 1:1 and prod skips the entire GPU pipeline. This needs only the prod bucket
(Phase 1) + our write access to it.
- [ ] **Copy the two texture trees** dev → prod, rewriting the `dev/` → `prod/` segment (keys copied verbatim):
  ```
  aws s3 sync s3://blokport-dev-staging-3e58a6/dev/variations/ s3://blokport-prod-staging/prod/variations/
  aws s3 sync s3://blokport-dev-staging-3e58a6/dev/products/    s3://blokport-prod-staging/prod/products/
  ```
  `dev/variations/` = the ~13.5k variant/type icon textures; `dev/products/` = the de-watermark set + the
  `enhanced/`/`discarded/` **markers**. Copying the markers is what makes prod's produce treat every image as
  **already processed** → auto-enhance submits ZERO GPU jobs (no FAL cost, no GPU hours).
- [ ] Sanity: `aws s3 ls --summarize --recursive` object counts on the prod prefixes ≈ the dev source counts.
- [ ] **Base data (varieties/attributes) is NOT a copy** — it loads from the committed `catalog_source/` (Id-free)
  + prod Medusa's own `from_medusa/production/attributes.csv` at produce time (Phase 5/6). Nothing to move; just
  confirm `production/attributes.csv` has arrived and its `category,value` vocabulary matches dev (only `sourceid`
  differs). A value present in dev but missing in prod Medusa gaps to review, never guesses.

### Phase 5 — Prod runtime config
- [ ] Prod task-def env is set by our terraform: verify `BLOKPORT_ENV=production` (⚠️ must be exactly `production`/`prod`), `BLOKPORT_S3_BUCKET=blokport-prod-staging`, `BLOKPORT_SALES_CHANNEL_ID=<from Phase 3>`, `FAL_KEY` (SSM), `S3_DRY_RUN` (defaults false in prod).
- [ ] In the prod `:4200` config admin: set each source's `company_id` (per-source; a blank one resolves by vendor name on Medusa's side), and add the prod `category,<Cat>,<prod_pcat>` rows (a category is active once its prod pcat is set).
- [ ] `from_medusa/production/attributes.csv` arrives automatically (SCRAPER_SYNC_ENABLED). Bootstrap the prod ledger from `from_medusa/production/*` so ids resolve against the prod backend.

### Phase 6 — Go-live verify (before real traffic)
- [ ] `python -m stone_pipeline.reference.seed verify` → `fixed_point: True` (CI-green on main is the source of truth; the committed seed is a proven fixed point).
- [ ] **Prod dry-run produce** (`BLOKPORT_S3_DRY_RUN=true`): inspect the staged output, and **confirm the variants import matches on live variant SKU and UPDATES rather than minting new Keys** — verify this from the dry-run, not from code reading (Keys carry over dev→prod; do NOT let prod mint new Keys).
- [ ] Real prod produce → products import with the right **company (per source), sales channel, categories, ports, images**.
- [ ] Pull round-trip: `/sync/v1` mints ids and products become visible in prod Medusa.

---

## Gotchas (do not trip on these)
- **Ordering:** setting `prod_staging_bucket` creates the count-gated `platform_prod` remote-state + `/blokport-prod/` data sources; they FAIL if the platform (Phase 1) isn't applied. Platform first, always.
- **ECR drift** — RESOLVED (PR #219, `create_ecr` + `moved{}`); the apply is a pure add, no ECR churn.
- **`alert_email`** must be in tfvars before the full apply, or it removes the dev SNS alert.
- **`BLOKPORT_ENV`** must be exactly `production`/`prod` — a typo runs prod-intended config in the dev namespace *unless* the env-tier guard is merged (Phase 0). Verify the resolved task-def env.
- **Pin a soaked sha**, never the mutable `core`/`gpu` tag — record the exact sha + digest in Phase 4.
- **Same FAL account key** across envs (done); switch to a prod-isolated key only if you want separate FAL billing.

## Non-blocking (track, not launch-blockers)
- Split-type authority: a confidently-wrong resolved type can bind a homonym to the wrong-type variety (input-dependent). Watch `tree_uncovered_variations.csv` / review holds.
- Concurrency edges: an ECS reset can race a running produce; the local-run watcher can wedge the run slot on a non-timeout exception. Low frequency.
- On an S3 outage mid-produce, the image link/gate fail open (narrow window).
- **Pull loop termination (Blokport):** the pull should keep going while **`status.syncing > 0`**, not stop at
  `pending == 0`. Serving LEASES rows to `syncing` (in-flight guard); an un-acked lease is reaped to `dirty`
  and re-served **only on the next pull** (`reap_stale_syncing`, 15-min timeout, no background job). A recurring
  prod pull self-heals this on its next cycle, but a loop that terminates on `pending == 0` alone can leave
  leased-but-unacked rows sitting `syncing` (and their products held) until the next scheduled pull. `status()`
  exposes the `syncing` count for exactly this. Convergence edge, not a correctness bug.
