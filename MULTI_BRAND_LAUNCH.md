# Multi-brand / multi-product launch runbook

One codebase + **one shared dev** (blokport). Each **brand** is its own **production deployment** (own Medusa
backend + storefront, own S3 bucket, own domain pack). The other brands are **prod-only** (no dev of their
own). The **product type** (stone / wood / lime) is a **setup choice** — the domain pack — not code. Today:
Blokport (stone). Next: Wudport (wood), Calcport (lime).

> **The declarative layer.** A brand is a **tfvars file**, not a code change:
> - `infra/brands/<brand>.tfvars` — one file per brand (blokport = dev + prod; wudport/calcport = prod-only,
>   standby with zero AWS until filled). See `infra/brands/README.md` for the launch matrix + `init`/`apply`.
> - `.env.template` (repo root) — every runtime env var, `SCRAPER_*` names, for local runs + as the checklist.
>
> Intended AWS footprint: **dev + one prod (blokport)**; wudport/calcport are declared and ready but consume
> **zero** AWS until their tfvars are filled.

## What is now product- and brand-agnostic (this branch)

The software no longer assumes stone or Blokport. Chosen at deploy time by env var, no code edit:

- **`SCRAPER_DOMAIN_PACK`** selects the product model — vocabulary (attributes/type set), categories,
  dimensions, finishes, density, name-cleaning, colour classification — from `config/domains/<pack>.yaml`.
  `stone` (default) reproduces the historical output exactly; `wood` ships as a fill-in skeleton.
- **`SCRAPER_BRAND`** names the store and is cross-checked against the bucket in prod (a bucket mis-set to
  another brand's store fails loud). Required in prod.
- **`SCRAPER_SALES_CHANNEL_ID`** is the brand's storefront (one per deployment).
- Per-material **density** is pack-namespaced (`reference/type_density.<pack>.csv`); a pack with no file uses
  its `default_density`, so wood never inherits stone's 2700.

The Terraform `scraper` + `sync_service` modules take `brand` / `domain_pack` / `sales_channel_id` inputs
(defaults `blokport` / `stone` / `""`, so the existing stack is unchanged).

## Still required before a non-stone brand goes live

1. **Author the pack** — fill `config/domains/wood.yaml` (or `lime.yaml`) with the brand's real categories,
   finishes, density, and fallback dimensions. The skeleton loads and validates; the VALUES are yours.
2. **Reference data** — the brand's `attributes.csv` (from its Medusa: type=species, color, finish, quality),
   `backbone_<category>.json` per category in `catalog_source/`, and `reference/type_density.<pack>.csv` for
   per-type densities. Novel varieties absent from the backbone legitimately gap to review.
3. **Smoke run (the gate)** — a ~10-row `SCRAPER_DOMAIN_PACK=<pack>` scrape→derive→emit in a **separate dev
   data plane** (own bucket/config.db/ledger), eyeballed for right categories, un-mangled names, right
   density/dimensions. Code review does not prove a material works; a clean smoke run does.
4. **Stand up the brand's root stack** — the root is now brand-parameterized (below).

## Standing up a brand's root stack (now parameterized)

The whole root (`infra/`) is parameterized by a `brand` variable (default `blokport`, so the existing stack
renders byte-identical). Every resource name, tag, SSM namespace, and platform-state key derives from
`var.brand`; the modules already take `brand`/`domain_pack`/`sales_channel_id`. A second brand is the **same
code**, a separate state:

1. **Init with the brand's own backend key** — a backend block can't take a variable, so:
   `terraform init -backend-config="key=<brand>/scraper/terraform.tfstate"` (reuse the platform state bucket
   or set your own). This is the one thing that keeps two brands' state apart.
2. **Set the brand tfvars:** `brand = "<brand>"`, `domain_pack = "<pack>"`, `platform_state_bucket` +
   `prod_home_env` for that brand's platform, `dev_staging_bucket`/`prod_staging_bucket`, `prod_sales_channel_id`,
   the brand's `*_ssm_name`s, `alert_email`.
3. **Shared ECR:** the image is brand-agnostic (pack chosen at runtime). blokport's stack **creates** the
   `blokport-scraper` repo; a second brand should **reference** it (change `aws_ecr_repository.this` to the
   existing `data "aws_ecr_repository" "scraper"` and drop the create), so all brands promote the SAME tag.
4. **Platform:** the brand's own VPC (`<brand>-<env>-vpc`), cluster, and `<brand>/<env>/terraform.tfstate`
   platform state must exist — the modules resolve them from `var.brand` + `platform_state_bucket`.

Still `terraform plan`-gated before apply (nothing here touches live infra). A `plan` on the **existing
blokport stack** must show **no destroy/recreate** — only the additive task-def env vars
(`SCRAPER_BRAND`/`SCRAPER_DOMAIN_PACK`/`SCRAPER_SALES_CHANNEL_ID`) with their current effective values, because
`brand` defaults to `blokport` and every rendered string is unchanged.

## Per-brand launch checklist (Wudport / Calcport)

For each brand X (wood→Wudport, lime→Calcport):

**A. Author (code + data)**
1. `config/domains/<pack>.yaml` — real categories/finishes/density/dimensions.
2. `catalog_source/backbone_<category>.json` per category; `reference/type_density.<pack>.csv`.
3. `config/sources.yaml` — the brand's source(s) (fetcher/adapter per source, auto-discovered).

**B. Prove**
4. Smoke run with `SCRAPER_DOMAIN_PACK=<pack>` in a scratch dev data plane → eyeball. Fix until clean.

**C. Medusa (that brand's backend)**
5. Create the brand's company, sales channel, publishable key, and attribute values.
6. Turn on `SCRAPER_SYNC_ENABLED` so it auto-publishes `from_medusa/production/attributes.csv`.
7. Note the `sales_channel_id`, category pcat ids, per-source company ids.

**D. Infra (its own root stack)**
8. Stand up the brand platform stack → publishes `<brand>/prod/terraform.tfstate` + creates
   `<brand>-prod-staging` + `/<brand>-prod/{BLOKPORT_SYNC_TOKEN,BLOKPORT_CONFIG_TOKEN}`.
9. Create `/<brand>-prod/{FAL_KEY,BLOKPORT_SCRAPER_PROXY}`.
10. Instantiate `scraper` + `sync_service` + `gpu_enhance` for the brand-prod with
    `brand=<brand>`, `domain_pack=<pack>`, `sales_channel_id=<its channel>`,
    `staging_bucket=<brand>-prod-staging`, the shared ECR image, pinned tags.
11. `terraform plan` (confirm 0 destroy) → `apply`.

**E. Cutover**
12. Task-def env is set by TF: `SCRAPER_ENV=production`, `SCRAPER_S3_BUCKET=<brand>-prod-staging`,
    `SCRAPER_BRAND=<brand>`, `SCRAPER_DOMAIN_PACK=<pack>`, `SCRAPER_SALES_CHANNEL_ID=<channel>`, `FAL_KEY`.
13. `python -m stone_pipeline.config.store seed`; bootstrap the ledger from `from_medusa/production/*`;
    `python -m stone_pipeline.reference.seed verify` → `fixed_point: True`.
14. Dry-run produce (`BLOKPORT_S3_DRY_RUN=true`) → inspect → real produce → verify company/channel/categories/
    density/images → pull round-trip mints ids.

## Blokport (stone) prod

Code-ready; its only blocker is external (the Blokport prod platform stack + `/blokport-prod` SSM tokens per
`TODO_PROD.md`). Once those exist, flip `prod_staging_bucket` in tfvars, set `SCRAPER_BRAND=blokport`, and
follow steps C–E with `domain_pack=stone`.
