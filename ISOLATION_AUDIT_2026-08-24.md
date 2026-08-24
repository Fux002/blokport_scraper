# Multi-brand / multi-material isolation audit — 2026-08-24

**Question audited:** can one brand's data cross into another brand's store, or one material's vocabulary/varieties into another material's deployment — i.e. can you *truly* set up per-brand and per-material with **no mixing**?

**Method:** three independent deep audits (brand isolation, material isolation, deployment/infra), run in isolated git worktrees, read-only. Findings are file:line-verified against the current tree.

---

## Verdict

**The runtime / code / vocabulary / physics layer is genuinely brand- and material-parameterized — a lot of real work is done.** But **true isolation is not yet airtight**, on three fronts:

1. **Data-plane namespacing is shared-by-directory** (isolated only by filename/convention), and it already has a **concrete collision** (lime reuses stone's `backbone_blocks.json`). No structural guard makes a cross fail loud.
2. **The Medusa binding is unguarded** — `sales_channel_id` / per-source `company_id` can point at the wrong brand's store with the *correct* bucket, and nothing flags it.
3. **The Terraform root is single-prod** — you cannot stand up a second brand's prod without name collisions, shared state, and un-overridden defaults.

The model is sound (one deployment = one brand + one material + one bucket + one env). What's missing is the *guard rails and namespacing* that make that separation structural instead of operator-dependent.

## What is genuinely done (do NOT redo — this corrects earlier reviews)
The heavy code generalization is largely complete:
- **Categories (old GAP A): CLOSED.** `format_resolve` / `derive` drive off `active_pack().categories` (`default_form_name()`/`bulk_form_name()`/`category()`/`CATEGORIES`) — no `slab/block/tile` literals in branch logic. Wood (plank/beam/panel) and lime (block/bag) work.
- **Name heuristics (old GAP F): CLOSED, pack-gated.** Granite-code is `active_pack().name_code_pattern`; trailing lone-letter grade is gated on `active_pack().trailing_grade_letters`. Wood/lime set both off, so stone's rules don't touch their names.
- **Density / area: pack-namespaced.** `type_density.<pack>.csv`, `standard_slab_area.<pack>.csv`; falls back to `pack.default_density` (wood 700 / lime 1600 / stone 2700), never the stone file.
- **Colour classifier (old GAP B): opt-out gated** via `active_pack().classify_texture_color` (wood/lime set it false).
- **`wood.yaml` + `lime.yaml` exist**; `active_pack()` is a `maxsize-1` singleton keyed on the env var → one process = one material, no in-process mixing.
- **Runtime brand:** `BRAND` + `BLOKPORT_DOMAIN_PACK` are runtime task env (image stays agnostic); a `BRAND↔bucket` fail-loud guard exists; ECR + deploy role are shared-and-safe.

So the vocabulary/physics plane is isolated. The remaining risk is in the **data plane, the Medusa binding, and the infra root** — all below.

---

## Layer 1 — BRAND isolation

| # | Surface | file:line | Status | Sev |
|---|---------|-----------|--------|-----|
| B1 | **`sales_channel_id` + per-source `company_id` are NOT brand-checked** | `settings.py:140-148`, `stages/constants.py:24-28`, `ledger/populate.py:203-219`, `ledger/sync.py:256-260` | **SILENTLY CROSSES** | **HIGH** |
| B2 | Brand↔bucket guard is a **substring** check, prod-only | `settings.py:116-127` | guarded, fragile | MED |
| B3 | Durable state (ledger, `config.db`, snapshots) keyed by **ENV only, not brand** | `ledger/snapshot.py:44-57`, `config/store.py:56-59`, `ledger/db.py:174-205` | isolated **only by bucket** | MED |
| B4 | `STN ` custom-field prefix + dev-default bucket/brand hardcoded | `stages/emit.py:83-99`, `settings.py:100,117` | brand-coupled, fails safe | LOW |

**B1 (the real crossing path).** The only brand guard checks the *bucket*. A deployment can have the correct bucket (guard passes) but a wrong `SCRAPER_SALES_CHANNEL_ID` or a per-source `company_id` (entered in `:4200`, **not** in IaC) pointing at another brand's Medusa → products are stamped with brand A's owner ids while living in brand B's bucket/ledger. **Nothing cross-checks brand↔company↔sales_channel↔Medusa-export.** Worse: `constants.py:33-34` flags only a *blank* owner (DEGRADED, non-fatal); a *wrong-but-present* owner id is not flagged at all. Mitigator: the `from_medusa` export rides the (correct) bucket, so *it* is brand-correct when the bucket is — but `sales_channel_id`/`company_id` are free-floating env/config, so they don't.

**B2.** `BRAND in S3_BUCKET.lower()` is containment, not equality. Safe *today* only because `blokport`/`wudport`/`calcport` don't nest as substrings. A future brand that nests (`blokport` vs `blokport2`, or a short slug like `wud`/`calc`) would pass the check against the wrong bucket. It's a naming-hygiene guarantee, not a structural one, and it's skipped entirely in dev.

**B3.** Snapshot/ledger/config S3 keys are `{ENV_NAME}/scraper/...` — brand-blind. Two prod brands both `production` produce **byte-identical** durable keys; they don't collide *only because the bucket differs*. The ledger env-guard (`ledger_meta`) asserts env, not brand — so restoring the wrong brand's ledger/config snapshot into a deployment would be accepted silently (and `config.db` now carries `company_id`, an env/brand-specific Medusa id, despite its "env-agnostic" premise).

---

## Layer 2 — MATERIAL isolation

| # | Surface | file:line | Isolated per-material? | Sev |
|---|---------|-----------|------------------------|-----|
| M1 | **`catalog_source/backbone_*.json` shared dir, filename-only — lime reuses stone's `backbone_blocks.json`** | `settings.py:581-582`, `stone.yaml:27` vs `lime.yaml:25` | **NO — concrete collision** | **HIGH** |
| M2 | `reference/synonyms/*.csv` are fixed **shared stone files** every pack reads | `settings.py:171`, `loaders.py:567-582,208-211` | **SHARED** | MED |
| M3 | `attributes.csv` + committed seed `variants_export_base.csv` are **env-namespaced, not material** | `settings.py:58,193,201` | per-env only | MED |
| M4 | `classify_texture_color` **defaults `True`** → a pack that forgets to opt out inherits the stone classifier + is forced onto stone colours | `domain.py:77`, `variety_color.py:196`, `loaders.py:780-788` | fail-loud coupling | MED |

**M1 (the concrete collision).** `catalog_source/` is one shared directory; backbone isolation is *only* by the pack picking distinct filenames. Stone's block form and **lime's block form both declare `backbone_blocks.json`.** A lime deployment running against a `catalog_source/` that still holds stone's committed `backbone_blocks.json` (a cold start before lime's data lands, or a shared checkout) silently loads **stone block varieties** into the lime catalog — `load_backbone()` doesn't fail loud because the file exists and parses. Wood avoids this only by luck of distinct names.

**M2.** `synonyms/{color,finish,quality,type}.csv` are the fixed stone files, read by *every* pack. For wood/lime rows they usually no-op, but any name collision resolves to a stone canonical silently, and `_type_slugs()` folds stone synonyms into type resolution for all materials.

**M3.** `attributes.csv` and the committed seed are keyed on `development`/`production` only. Safe across *separate* deployments (separate filesystems + Medusa), unsafe in a shared workspace or on a cold start before the material's own files land. The committed seed is stone, so a wood/lime cold start reads whatever base sits in that env dir — nothing asserts the base matches the active pack.

**M4.** The pack default `classify_texture_color: True` means a *new* material pack that forgets to set it false inherits the stone HSV classifier and is force-required to carry all 13 stone colours in its Medusa (`_assert_pack_defaults_resolve`). And because synonyms are shared stone files, that same load-time assertion checks stone synonym targets (e.g. "Sodalite Syenite") against the material's own Medusa vocabulary — a fail-loud coupling of every material to stone's colour/synonym vocab (wood/lime dodge the colours by opting out, but not the shared-synonym assertion).

---

## Layer 3 — Deployment / infra (the biggest structural gap)

| # | Layer | file:line | Supports N brands? | Sev |
|---|-------|-----------|--------------------|-----|
| I1 | Root prod is **single-prod** (`count 0/1` on one singular `prod_staging_bucket`, no `for_each`) | `infra/main.tf:39,173-190,225-239,336-364`; `variables.tf:12-47` | **NO** | **HIGH** |
| I2 | Resource names keyed on `target_env` only, literal `blokport-` → `wudport-prod` collides with `blokport-prod` | `modules/scraper/main.tf:24`, `gpu_enhance/main.tf:10`, `sync_service/main.tf:13` | **NO** | HIGH |
| I3 | **Root never passes `brand`/`domain_pack`/`sales_channel_id` to the prod module calls** → prod deploys as blokport/stone defaults | `infra/main.tf:173-190,336-364` (inputs plumbed in modules, not set at root) | **NO** | HIGH |
| I4 | One shared tfstate key + one lock (no workspaces) | `infra/backend.tf:6` | **NO** | HIGH |
| I5 | Hardcoded `blokport/prod` platform remote-state read | `infra/main.tf:310-318` | **NO** | HIGH |
| I6 | Single `/blokport-prod/*` SSM namespace | `infra/main.tf:19-26,320-328` | **NO** | MED |
| I7 | Cloud Map name `scraper` collides **if** brands share one prod platform VPC | `modules/sync_service/variables.tf:57-61`, `main.tf:284,351` | CONDITIONAL | MED |
| — | ECR (agnostic image, semantic-tag-protected), deploy role (scoped), runtime image env, durable-state-by-bucket | `infra/main.tf:44,51-140`; `modules/scraper/main.tf:199-200` | **YES (safe)** | — |

**Bottom line for infra:** standing up `wudport-prod` alongside `blokport-prod` is **real infra work, not a tfvars flip.** It needs: (a) per-brand state (a `terraform workspace` per brand or a per-brand backend key), (b) a **`${brand}` component in every module `local.name`** (the `blokport-` literal is baked), (c) the root to actually **set `brand`/`domain_pack`/`sales_channel_id`** on the prod calls (plumbed into the modules, never set), (d) a per-brand `platform_<brand>-prod` remote-state source + `home_env`, (e) per-brand SSM namespaces, and (f) a distinct prod platform VPC/namespace per brand (else the Cloud Map `scraper` name collides).

---

## The common root, and the structural fix

Every non-trivial finding is the same shape: **isolation is keyed by ENV (dev/prod) and by one operator-set value (the bucket), not by BRAND or by MATERIAL.** The physics plane got proper per-pack namespacing (`type_density.<pack>.csv`); the *data plane, durable state, and infra* did not. The clean fix is to extend that same namespacing everywhere:

1. **Pack-namespace `catalog_source/` and `reference/synonyms/`** the way `type_density`/`standard_slab_area` already are — e.g. `catalog_source/<pack>/backbone_*.json`, `reference/synonyms/<pack>/*.csv`. Closes M1 (the lime collision), M2, and part of M3/M4. Make a missing material file **fail loud**, not silently fall back to stone.
2. **Brand-namespace the durable S3 prefixes and the ledger guard** — put `BRAND` in the `{ENV}/scraper/...` keys and add a `brand` field to `ledger_meta` (assert it on open, like the env guard). Closes B3 and gives B2 a structural second line.
3. **Replace the substring bucket guard with an equality/registry check + a brand↔sales_channel↔company assertion.** A deployment should refuse to run unless bucket, `sales_channel_id`, and the source `company_id`s all belong to the declared `BRAND`. Closes B1 (the HIGH) and B2.
4. **Parameterize the infra root per brand** — `for_each` over a brands map (or one workspace/state per brand), a `${brand}` prefix in every `local.name`, pass `brand`/`domain_pack`/`sales_channel_id` to the prod calls, and per-brand platform-state + SSM. Closes I1–I7.

---

## Edge-case catalog (what actually triggers a cross)
- **Cold start before a material's own `catalog_source/` lands** → loads the committed **stone** backbone/seed (M1/M3). The lime `backbone_blocks.json` name-collision makes this silent for lime specifically.
- **Shared checkout / shared workspace** running two materials or two brands → env-keyed paths collide (M3, B3).
- **Correct bucket, wrong owner ids** (copied a task def, changed the bucket, forgot `sales_channel_id` / per-source `company_id`) → products written to the wrong brand's Medusa, unflagged (B1).
- **A new brand whose slug nests in another's bucket name** → the substring guard passes against the wrong bucket (B2).
- **A new material pack that forgets `classify_texture_color: false`** → inherits the stone colour classifier + is forced to carry stone colours (M4).
- **Restoring a `config.db`/ledger snapshot into the wrong deployment** → accepted (env matches), carries the wrong brand's `company_id` (B3).
- **A non-stone Medusa template without the `STN ` custom-field prefix** → those fields emit blank on the legacy CSV path (B4; live sync path is safe).

---

## To *truly* run N brands × N materials — checklist
Data/code (this repo):
- [ ] Pack-namespace `catalog_source/` (backbones) + `reference/synonyms/`; fail loud on a missing material file (M1, M2).
- [ ] Make `attributes.csv` + the committed seed material-aware (or assert the loaded base matches the active pack) (M3).
- [ ] Flip `classify_texture_color` default to `False` (opt-in, not opt-out) so a new pack can't silently inherit the stone classifier (M4).
- [ ] Brand-namespace the durable S3 prefixes + add a `brand` field to the ledger/config guard (B3).
- [ ] Replace `BRAND in S3_BUCKET` with equality + a brand↔sales_channel↔company assertion (B1, B2).
- [ ] Make the `STN ` custom-field prefix pack/brand-configurable if any non-stone Medusa uses a different one (B4).

Infra (per new brand):
- [ ] Per-brand state (workspace or backend key) (I4).
- [ ] `${brand}` component in every module `local.name`; drop the `blokport-` literal (I2).
- [ ] Root passes `brand`/`domain_pack`/`sales_channel_id` to the prod module calls (I3).
- [ ] Per-brand `platform_<brand>-prod` remote-state + `home_env` + distinct platform VPC/namespace (I5, I7).
- [ ] Per-brand SSM namespaces `/<brand>-prod/*` + per-brand `*_ssm_name` (I6).
- [ ] A `for_each`/brands-map root or one root instance per brand (I1).

Gate (per new brand/material): a smoke run in that brand's/material's own bucket + Medusa, confirming right categories, un-mangled names, right density, correct owner ids, and — critically — that it loaded **its own** backbone/vocab, not stone's.

---

## Priority
1. **M1 (lime `backbone_blocks.json` collision)** — a concrete, silent cross today; fix the backbone namespacing first.
2. **B1 (owner-id crossing)** — the unguarded path that writes to the wrong brand's store.
3. **I1–I5 (infra single-prod)** — required before a *second* brand's prod can exist at all.
4. B2/B3/M2/M3/M4 — the structural hardening that turns "works if the operator is careful" into "fails loud if they're not."

**Net:** you built the hard part (packs, pack-driven categories/names/density, the singleton, the runtime brand env). What remains to make it *truly* mix-proof is namespacing the data plane and durable state by brand/material, adding the owner-id and equality guards, and parameterizing the infra root — after which each new brand/material is data + a smoke run.
