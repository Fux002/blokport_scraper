# Multi-brand / multi-product launch runbook

One codebase + one dev. Each **brand** is its own **production deployment** (own Medusa backend + storefront,
own S3 bucket, own domain pack). The **product type** (stone / wood / lime) is a **setup choice** — the domain
pack — not code. Today: Blokport (stone). Next: Wudport (wood), Calcport (lime).

## What is now product- and brand-agnostic (this branch)

The software no longer assumes stone or Blokport. Chosen at deploy time by env var, no code edit:

- **`DOMAIN_PACK`** selects the product model — vocabulary (attributes/type set), categories,
  dimensions, finishes, density, name-cleaning, colour classification — from `config/domains/<pack>.yaml`.
  `stone` (default) reproduces the historical output exactly; `wood` ships as a fill-in skeleton.
- **`BRAND`** names the store and is cross-checked against the bucket in prod (a bucket mis-set to
  another brand's store fails loud). Required in prod.
- **`BLOKPORT_SALES_CHANNEL_ID`** is the brand's storefront (one per deployment).
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
3. **Smoke run (the gate)** — a ~10-row `DOMAIN_PACK=<pack>` scrape→derive→emit in a **separate dev
   data plane** (own bucket/config.db/ledger), eyeballed for right categories, un-mangled names, right
   density/dimensions. Code review does not prove a material works; a clean smoke run does.
4. **Root Terraform stack per brand** — see below; not yet parameterized.

## The remaining infra work (root stack per brand)

The `scraper`/`sync_service`/`gpu_enhance` **modules** are brand-parameterized. The **root** `infra/main.tf`
is still hardwired to the Blokport platform (names `blokport-*`, VPC `blokport-<env>-vpc`, state key
`blokport/<env>/terraform.tfstate`, SSM `/blokport-<env>/…`). A new brand needs **its own root stack**:

- its own platform stack (VPC / cluster / state key `<brand>/<env>/…` / SSM namespace `/<brand>-<env>/…`),
- its own `<brand>-<env>-staging` bucket + `gpu_enhance` instance,
- the shared ECR image (`blokport-scraper`) is domain-agnostic — the pack is chosen at runtime, so all brands
  promote the SAME image tag.

This is the one piece that needs a real `terraform plan` against live state before apply. (Do NOT apply from
here — this branch changes no live infrastructure. Every module change is additive: a `plan` on the existing
Blokport stack should show only task-def revisions adding `BRAND`/`DOMAIN_PACK`/`SALES_CHANNEL_ID`
with their current effective values, and no destroy/recreate.)

## Per-brand launch checklist (Wudport / Calcport)

For each brand X (wood→Wudport, lime→Calcport):

**A. Author (code + data)**
1. `config/domains/<pack>.yaml` — real categories/finishes/density/dimensions.
2. `catalog_source/backbone_<category>.json` per category; `reference/type_density.<pack>.csv`.
3. `config/sources.yaml` — the brand's source(s) (fetcher/adapter per source, auto-discovered).

**B. Prove**
4. Smoke run with `DOMAIN_PACK=<pack>` in a scratch dev data plane → eyeball. Fix until clean.

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
12. Task-def env is set by TF: `BLOKPORT_ENV=production`, `BLOKPORT_S3_BUCKET=<brand>-prod-staging`,
    `BRAND=<brand>`, `DOMAIN_PACK=<pack>`, `BLOKPORT_SALES_CHANNEL_ID=<channel>`, `FAL_KEY`.
13. `python -m stone_pipeline.config.store seed`; bootstrap the ledger from `from_medusa/production/*`;
    `python -m stone_pipeline.reference.seed verify` → `fixed_point: True`.
14. Dry-run produce (`BLOKPORT_S3_DRY_RUN=true`) → inspect → real produce → verify company/channel/categories/
    density/images → pull round-trip mints ids.

## Blokport (stone) prod

Code-ready; its only blocker is external (the Blokport prod platform stack + `/blokport-prod` SSM tokens per
`TODO_PROD.md`). Once those exist, flip `prod_staging_bucket` in tfvars, set `BRAND=blokport`, and
follow steps C–E with `domain_pack=stone`.
