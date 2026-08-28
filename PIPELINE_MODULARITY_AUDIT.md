# Pipeline Modular-Integrity Audit — Handoff

**Purpose.** Deep audit of the stone-scraper pipeline (scrape → clean → transform → load) for modular integrity: stage boundary enforcement, contract clarity, function responsibility, plus edge cases and concrete failure scenarios. Written to be handed to another engineer/chat to investigate and fix.

**How to read this.** Findings are grouped by layer and ranked by severity. Each finding gives: exact mechanism (file:line), triggering input, downstream failure, minimal reproduction, and fix direction. Read §0 first — it frames every other finding. Read §7 ("what is correct — do not touch") before changing anything, so correct-by-design code isn't "fixed."

**Method.** One broad audit pass per layer + four deep-dive investigations (inventory, transform coupling, scrape derivations, systemic contract risks). All file:line refs verified against the working tree at audit time.

---

## 0. Cross-cutting architecture (frames everything)

The pipeline threads **one mutable `CanonicalRow`** (`stone_pipeline/core/schema.py:125`, **89 fields**, pydantic `BaseModel`, not frozen) through 12 stages. Each stage `run(rows, ref, …) -> StageMetric` **mutates the rows in place** and returns only *diagnostics*, never the transformed data. Stage order is hard-coded in `stone_pipeline/run.py` (~265-382):

```
health → ingest → keys_dedupe → format_resolve → normalize → match_variation
       → reconcile_tree → derive → images → constants → validate → emit
```

**There are no typed per-stage input/output contracts and no per-stage boundary validation.** Pydantic validates only at construction; the sole runtime gate is Stage 9 `validate`. So "does the next stage's input match the previous stage's output" is enforced by **convention** (which of the 89 fields each stage writes), not by types or checks. This is a pragmatic "progressive enrichment of a god-object" design — it works today, but every finding below is an instance of its structural weaknesses:

- A stage *can* read a field before its writer ran (silent `None`/default).
- A field can be written by more than one stage (last-writer-wins, no assertion).
- List fields (`review_flags`/`reject_reasons`/`tree_gaps`) **accumulate** on any re-application.
- "Authority" for a value = whoever wrote last, not an explicit marker.

---

## 1. SYSTEMIC findings (structural, apply pipeline-wide)

### S1 — Field ownership has multi-writer collisions with no enforcement
Intended writers: adapter (`src_*`,`raw_*`,`variety_match_key`); keys_dedupe (`surrogate_key`); format_resolve (`format_value`,`is_block`); normalize→reconcile (`type/color/finish/quality _*`); match (`variation_*`); derive (`category_*`,dims,`weight`,`bundle_*`,`origin_*`,`port_ids`,`title/description/handle/slug`); images (`*image*`); constants (`company_id`,`sales_channel_id`,`visibility`,`discountable`); product_state (`product_status`,`product_changed`).

**Multi-writer fields (collisions):**
| Field | Writer 1 | Writer 2 (later, wins) | Risk |
|---|---|---|---|
| `format_value` | format_resolve `_set` (`format_resolve.py:65`) | match fallback (`match_variation.py:118`) + derive safety-net (`derive.py:107`) | 3 setters; a `branch_of`/`resolve_format` change must touch all 3 or they diverge |
| `is_block` | format_resolve (`format_resolve.py:66`) | match (`match_variation.py:120`) | default `False` → read-before-write silently treats a block as a slab |
| `type_name`/`_id`/`_confidence`/`_method` | normalize (`normalize.py:107,150`) | reconcile `_apply_type` (`reconcile_tree.py:188`) | **split type authority** — provisional value visible between the two stages |
| `color_name`/`quality_name`/`finish_name` (+ids) | normalize | reconcile fill+snap (`reconcile_tree.py:54,219`) | reconcile fills-when-null and fuzzy-snaps (see T3) |
| `color_id`/`finish_id`/`quality_id`/`type_id` | normalize | reconcile `_finalize_ids` re-maps all 4 (`reconcile_tree.py:258`) | correct only while `ref` identical across stages |
| `category_pcat_id`/`category_method` | reconcile `_finalize_ids` (`reconcile_tree.py:262`) | derive `derive_category` (`derive.py:109`) | **dead redundant write** (see T2) |

**Read-before-write hazard:** `is_block` defaults `False`; any stage reading it before format_resolve/match sees a silent `False`. `keys_dedupe` is the *only* reader that guards against this (`keys_dedupe.py:92-96`, deliberately reads `raw_format` instead). A newly-inserted early stage would not know to.

### S2 — Re-run is NOT idempotent at the stage layer (list fields accumulate)
Process re-run is safe **only because `run_source` rebuilds `rows` fresh from `adapter.adapt(frame)` every call** (`run.py:266`). There is no in-process guard. If a stage (or the pipeline) is ever re-applied to the *same* `rows` object:
- **Scalars converge** (every write is a replace; reconcile early-returns on already-set values — genuinely idempotent).
- **Lists accumulate** — `add_flag`/`add_reject`/`add_gap` are unconditional `list.append` (`schema.py:247-254`). A second pass **doubles** `review_flags` (format_resolve/normalize/derive re-add), **doubles** `reject_reasons` (validate), and **doubles** `tree_gaps` from match `_gap` (`match_variation.py:272`, *unguarded* — reconcile's gap *is* guarded at `reconcile_tree.py:93`). Routing stays correct (`is_emittable` checks `len==0`), and gap CSV dedupes at write (`run.py:78`), but `products_review.csv` / `review_code_counts` would double.
- **Regression vector:** any future caching/retry layer that re-invokes a stage on an already-processed row multiplies flags/rejects/gaps. No idempotency key on the appends.
- **Preserve this fix:** match defers learned aliases to the persisted write-back (`match_variation.py:205-210`) instead of mutating the live index mid-run — a prior order-dependent-confidence bug. Don't "optimize" it back.

### S3 — validate is a whitelist of known failure shapes, not a schema check
The only loud structural check is the surrogate-uniqueness assert (`keys_dedupe.py:114`, raises `KeyCollisionError`/crashes). Every other boundary degrades to a flag/reject. `validate` (`validate.py:44-93`) re-reads specific fields+flags. A **new field or a new silent-degrade path is invisible to it** until someone adds a rule. See §3 for the shipped-but-wrong list.

### S4 — Order fragility: cross-stage dependencies enforced only by call order + (some stale) comments
No declared dependency graph, no preconditions. Reorders produce *plausible wrong values*, not crashes. Notable hidden dependencies:
1. **normalize/format_resolve order is documented BACKWARDS** — `normalize.py:156-159` says "normalize runs BEFORE format_resolve, so format_value isn't set yet"; actual order is the reverse (`run.py:307` then `:312`), and the code works *because* format_value **is** already set. A maintainer trusting the comment could reorder and silently drop the block Raw-finish default. **(Also filed as C1.)**
2. normalize must precede match (match blocks on `type_name`/`color_name`, `match_variation.py:196`).
3. match must precede reconcile (else every row gaps `missing_variation`, whole batch rejected silently).
4. **reconcile must precede derive** — derive computes weight/density, title, description from `type_name`; if derive ran on normalize's provisional type, freight weight uses the wrong stone's density, no error. (Runtime face of T1.)
5. `derive_category`'s safety-net re-resolve *masks* a reconcile skip (redundant writer hides the break).
6. constants/images must precede validate; classify must precede the emit split (`run.py:404`); `row_fingerprint` (`run.py:363`) must follow derive/constants/images or change-detection hashes stale inputs.

---

## 2. LOAD layer

### L1 — [HIGH] Inventory quantity is computed at load time in 6 places, never a finished field
`inventory_quantity` is **not a `CanonicalRow` field**; it exists only as a persisted ledger column. Every consumer re-derives it from raw fields at write time.

**Resolver** (`product_state.py:71-83`): first-positive of `raw_slab_count` → `bundle_size` (the **derived** int, not `raw_bundle_size`) → `raw_inventory_quantity`; else literal `"0"`. Positive = `parse_number(...) is not None and int(n) > 0`.

**Six call sites:** `emit.py:82` (`Variant Inventory Quantity`), `emit.py:178` (`write_inventory_csv`), `populate.py:188`, `populate.py:228`, `render.py:91`, `render.py:135`. **Latent 7th, divergent:** `io/medusa_client.py:88` uses a *different* formula `row.bundle_size or 1` (ignores slab_count, floors to 1 not 0) — currently unwired but will ship split-brain the day the API sink is enabled.

**Two inputs are dead:** no adapter/scraper ever sets `raw_inventory_quantity` or `raw_bundle_size` (only `raw_slab_count`, in polonine/varsha/zucchi). Effective live formula: `raw_slab_count(if positive) else bundle_size else "0"`.

**Concrete bugs / edge cases:**
| # | Input | Result | Failure |
|---|---|---|---|
| E1 | **Block**, no `raw_slab_count` (normal) | `"0"` | **Every block ships out-of-stock** — indistinguishable from delisted; blocks effectively unsellable via CSV |
| E2 | **Slab, `raw_slab_count="0"`** | `"6"` | derive rejects `"0"` (`derive.py:299` `.isdigit()` on `>0`? no — falls to config default 6) → **oversell**: 0 available published as 6 |
| E3 | Slab, `raw_slab_count="twelve"` | `"6"` | falls to bundle_size=6; **`stock_unparseable` flag DEAD for slabs** (`_stock_is_unparseable` sees derived `bundle_size="6"` which parses, `product_state.py:89`) |
| E4 | Block, `"twelve"` | `"0"` | here the flag *does* fire (bundle_size None) — inconsistent slab-vs-block |
| E5 | Slab, `raw_slab_count="1,000"` | `"1000"` but `STN Bundle Size="6"` | **row-internal disagreement**: derive gates on `.isdigit()` (strict, `derive.py:299`) → default 6; `inventory_for` uses `parse_number` (locale) → 1000 |
| E6 | Slab, `"1,5"` | `"1"` | silent truncation |
| E7 | Slab, `"9999…"` (1e20) | huge int | possible Medusa integer overflow / import reject |
| E8 | Slab, `"-5"` | `"6"` | negative → default |
| E10 | Slab on a source with no `raw_slab_count` map | `"6"` every run | **stock pinned at config default forever** → inventory-refresh clock is a no-op for these sources |
| E11 | Discontinued (SKU absent) | `"0"` via delist path | same value as E1 blocks → a live block and a delisted product both read 0 |

`validate` has **no stock gate** — E2/E3 emit normally.

**Root cause:** inventory has no owning stage/field, and candidate #2 conflates "bundle multiplier" with "stock level."

**Fix direction:** (1) add `CanonicalRow.inventory_quantity: Optional[int]` + `inventory_method`/`_confidence`; (2) derive it once in Stage 6 after `derive_bundle_size`, separating stock from bundle size, using `parse_number` consistently, treating literal `"0"` as trusted-0 (fix E2/E9); (3) all writers (emit/populate/render/medusa_client) *read* the field, delete `inventory_for` (kills the 7-way split incl. medusa's `or 1`); (4) add a `validate` stock gate + fix `_stock_is_unparseable` to inspect **raw** inputs not derived bundle_size; range-check E7; decide block policy for E1 (blocks are singletons — explicit 1, not accidental 0). Note: true stock tracking requires an adapter to actually scrape it (`raw_inventory_quantity` is never set today — upstream gap).

**Key files:** `product_state.py:71-91,101-130`, `core/numbers.py:16-51`, `derive.py:277-323`, `emit.py:49,82,98,165-190`, `populate.py:188,220-241`, `render.py:56-92,117-143`, `io/medusa_client.py:88`, `validate.py:44-93`, `schema.py`.

### L2 — [MED-HIGH] Ledger re-implements type resolution/matching
`populate.py:106-137` `fill_variation_types` parses the variation Key slug, normalizes via `match_key`, and longest-match resolves against the seeded type vocab — its own comment says it duplicates "the same recovery tree_build uses." Derive/matching logic in the load layer, and it also mutates sync state (`synced→dirty`). Source-of-truth split. **Fix:** call the shared resolver, don't re-implement.

### L3 — [MED] emit silently defaults business fields from config
`emit.py:66` `discountable = r.discountable or SETTINGS.backend.discountable`; `emit.py:94` `visibility = r.visibility or SETTINGS.backend.visibility`. Computes a fallback at emit time, hiding an upstream gap — violates the "never guess into output" invariant. **Fix:** apply+stamp these upstream (constants/derive), emit only maps.

### L4 — [MED, borderline] payload_hash field-selection is business policy in the load layer
`populate.py:30,189-199` hand-picks which fields define "dirty" and owns a contract-version bump. Defensible as ledger-owned dirty detection, but the *field selection* is a business decision duplicated away from the emit column map. Note for maintainers: this is the mechanism by which origin/image changes re-sync — see the separate image-content-sha gap (image replaced in place at same URL does NOT re-dirty, because the hash uses the URL not the bytes).

### L5 — [LOW] branch derived by parsing the Key at populate (`populate.py:53-54`); SKU derived at emit and duplicated byte-identically (`emit.py:44-45` `_sku` vs `product_state.py:67-69` `sku_for`) — factor to one.

**Clean (no violation):** `validate` is a proper boundary gate (no business-value mutation). The template-driven `COLUMN_MAP` + `read_template_columns` is the ideal pattern. **Port/origin is serialized, not computed** at load — the PR #64 quarry-country fan-out fix held (`emit.py:83-85,93`, `sync.py:264-268`). Keep these.

---

## 3. TRANSFORM layer

> Correction to an earlier assumption: **derive is NEVER skipped.** Inventory-only runs (`inventory.py:39`) still execute the full `_run_source` through `derive.run` (`run.py:330`); `inventory_only` only gates the magnitude gate, image stage, and canonical/product writes.

### T1 — [HIGH] `derive_origin` resolves the WRONG homonym's origin when type isn't variation-authoritative
`derive_origin` keys the supplier-override and origin_map lookups on `row.type_name` (`derive.py:420,430`), both **strict `(name, stone_type)` with no type-blind fallback** (`loaders.py:591-597,687-688` — note: any belief that origin_map has a type-blind fallback is **stale**; shipped code is strict). `type_name` is only trustworthy because `reconcile._apply_type` (`reconcile_tree.py:172-191`) overwrote a name-derived type — **but that override is guarded by `if not row.variation_id: return` (`reconcile_tree.py:108`)** and by whether the Key yields a recognized type slug.

**Failure states:**
- **State 1 — no `variation_id`** (unmatched): reconcile returns before `_apply_type`; `type_name` = normalize's guess (incl. `name_explicit`, `normalize.py:144-152`). Origin resolves on the wrong/guessed type → strict miss → supplier_default. Row is gapped/rejected anyway → **low real impact** but wasted lookup + misleading origin on a doomed row.
- **State 2 — `variation_id` present but `variation_key` carries no recognized type slug** (`_bound_type` falls back to `type_name`, `reconcile_tree.py:159-169`): reconcile **re-stamps the name-derived type as `variety_authoritative`, high confidence** — misleading provenance. `derive_origin` uses it. **If the name is a genuine homonym in origin_map** (e.g. `Aqua Blue` = gneiss/granite/marble/onyx, `loaders.py:71-72`; `Azul White` quartzite vs onyx), it resolves the **wrong stone's origin row** → wrong `origin_country_code`/city, stamped `origin_source="origin_map"`, **no mismatch flag**. This is a confidently-wrong shipped origin — and it's exactly the homonym class the just-shipped multi-country origin feature targets.
- **States 3 (`type_overridden_by_variety`) and 4 (variety record absent) are CORRECT** — don't "fix" them.

**Repro (State 2):** matched row, `variation_key` post-branch segment is a type slug not in `_type_slugs()`, name `"Azul White"`, origin_map has both `(Azul White, Onyx)→X` and `(Azul White, Quartzite)→Y`, normalize set `type_name="Quartzite"` from the name word. Actual: origin = Y at high/low confidence, no flag. Expected: origin from the *bound* variety's type.

**Fix direction:** give `_apply_type` a distinct `type_method` when it falls back (State 2) instead of always stamping `variety_authoritative`, and have `derive_origin` refuse the curated lookups (drop to supplier_default + flag) unless the type is genuinely variation-authoritative. Files: `reconcile_tree.py`, `derive.py`, `normalize.py`, `loaders.py`.

### T2 — [MED] Category dual-owner is a dead store (latent, not active)
`reconcile._finalize_ids` writes `category_pcat_id`/`category_method="branch"` (`reconcile_tree.py:262`); `derive_category` unconditionally overwrites (`derive.py:109-110`, `category_method="format:…"`). **The reconcile write is never read** — every consumer runs after derive. Both compute the identical `category_pcat_for_branch(branch_of(row), ref)` value (input `format_value` is frozen after format_resolve), so **no value disagreement today** — only the provenance string differs (and `category_method` is never read for logic). **Verdict:** not a bug now; latent double-owner hazard if `derive_category`'s dormant format re-resolve ever fires. **Fix:** delete the category write from `_finalize_ids` (single owner = derive).

### T3 — [MED] reconcile fuzzy-snap can invert an identity-bearing finish
`_reconcile_attribute(snappable=True)` for finish/quality snaps to the nearest allowed value at `score >= 80` (`reconcile_tree.py:215-217`), `_nearest_allowed` uses `max(fuzz.ratio, token_sort_ratio)` with strict `>` tie-break (`:67-74`). Finish is explicitly *identity-bearing and not filled from the variety* (`reconcile_tree.py:37-39`) — yet it is snappable. Measured: `"Unpolished"→"Polished"` = **88.9** (semantic inversion!), `"Satin"→"Sain"` 88.9, `"Leather"→"Leathered"` 87.5. Threshold is inclusive at 80 — exactly the confusable band. No floor distinguishes "typo of allowed" from "real different finish this variety doesn't offer," so a genuine catalog gap is masked (should route `missing_leaf_child`). Empty-allowed short-circuits safely.

**Repro:** variety `finishes=["Polished","Honed"]`, scrape finish `"Unpolished"` → ships as **Polished**, `finish_method="snapped(89)"`, medium `leaf_snapped` flag, `ok=True`. **Fix:** raise the finish floor (or make it per-vocab), add an antonym/negation guard (`un-`/`semi-` can't snap to bare form), or treat finish like color (`snappable=False`) and gap non-members; refuse to snap on a near-tie between two allowed values.

**Lower-severity transform:** match re-derives `format_value` + writes `is_block` (derive-owned) (`match_variation.py:118-122`, documented unit-test fallback); derive re-runs `resolve_format` safety-net (`derive.py:107`); **derive loads CSVs directly** (`_standard_areas`,`_type_densities`, `derive.py:326-368`) via `SETTINGS.paths`, bypassing the `ref` injection every other table uses — inconsistent + harder to test; `derive_dimensions` bundles 4+ concerns incl. cm→m correction + nested weight (`derive.py:212-273`); `derive_category` name understates its `format_value` side effect.

---

## 4. CLEAN layer

### C1 — [MED] normalize ordering comment is inverted + dead fallback
`normalize.py:157-159` documents the stage order backwards (see S4.1). The `raw_format` arm of `fmt = (row.format_value or row.raw_format or …)` is unreachable because format_resolve always sets `format_value` first. Behavior correct by accident; the comment will mislead a maintainer into a reorder. **Fix:** correct the comment; optionally drop the dead arm.

### C2 — [MED] Split type authority inside a pure clean stage
`normalize.py:139-152` re-decides `type_name`/`type_id` from a stone-type word in the variety **name** (`explicit_type_word`, loads reference vocab), reaching into `variety_match_key` (a matching-layer field). This is a second type-authority path competing with match/reconcile — and (per T1) it *survives to `derive_origin` in States 1 & 2*, driving the wrong-homonym origin. **Fix:** consolidate type authority; at minimum ensure this provisional type carries a low-trust marker so origin can gate on it.

### C3 — [LOW/MED] Business-default injection in the clean layer
`normalize.py:157-176` injects the block→Raw finish default and last-resort finish/quality — supplying a missing business value, not cleaning a raw one. Config-driven, low-confidence, flagged (`block_default_raw`,`attr_last_resort`) — acceptable pragmatism, but note it crosses "normalize raw" into "supply default."

**Clean/acceptable:** `keys_dedupe` writes only `surrogate_key`+flags, asserts uniqueness (good); minor nit — it imports `proj.norm` from the `matching` package for a plain norm alias (`keys_dedupe.py:17,97`), prefer `core.text.match_key`. `format_resolve` is a category-derive **deliberately** hoisted into the clean layer (branch selects the Stage-4 variants file) — documented, reference reads confined to the override ladder.

---

## 5. SCRAPE layer

The governing rule (`adapters/base.py:9-11`): *"adapter does only column mapping and light parsing. No normalization, no matching, no derivation."* Violations cross that self-declared line.

### SC1 — [HIGH-for-layer] Zucchi per-piece weight derived in the adapter; its claimed safety net doesn't exist
`adapters/zucchi.py:61-73` `_per_piece_kg` = `round(net/count,3)` → `raw_weight`. Derive's `_derive_weight` (`derive.py:114-129`) trusts a present `raw_weight` (`method weight:parsed`) and only falls back to `volume×density` when blank — so the adapter value **always wins**.
- Div-by-zero/bad-input edges are **safe** (guards return `""` → derive fallback).
- **Defect 1 — the docstring's safety net is false:** it claims "the health gate bounds `weight_kg_net`+`slab_count`"; in `config/source_contracts.yaml` those are `optional_columns` with **no min/max/range/value bounds** at all. Nothing aborts on drift.
- **Defect 2 — silent mis-scale:** if `weight_kg_net` is ever per-piece (source variant), every row is under-weighted by `slab_count`×, `method weight:parsed`, no flag → wrong freight.
- **Defect 3 — locale mismatch:** adapter uses bare `float()`; derive uses `parse_number` (locale-aware). A EU-formatted `weight_kg_net="3.000,5"` → adapter `float()` raises → `raw_weight=""` → derive silently substitutes a **density estimate** even though the real weight was parseable.

**Fix:** move the per-bundle→per-piece division into derive (through `parse_number`, range-checkable), or at minimum parse with `parse_number` in the adapter and add real numeric bounds in `source_contracts.yaml`.

### SC2 — [HIGH-for-layer] Adapters pre-canonicalize `raw_color`/`raw_type`, killing normalize's fuzzy tier
`adapters/zucchi.py:33,53-58` and `adapters/varsha.py:53-54` set `raw_color`/`raw_type` to **canonical vocab** via `tokens.extract_color`/`recognize_type`. `raw_*` are supposed to be *raw candidates* for Stage-3 to resolve. normalize then re-runs its ladder on the already-canonical string, so the **exact tier short-circuits and synonym/fuzzy never fire**.
- **Lossy pre-filter:** `extract_color`/`recognize_type` return `""` for anything not already in `{canonical ∪ synonyms}` (`tokens.py:85-90,114-123`). So a color/type the adapter can't canonicalize is **collapsed to blank before normalize sees the original text** → normalize's `attribute_fuzzy_floor` (`normalize.py:41`) is **dead for color on zucchi/varsha**. A near-miss spelling or multi-word color that fuzzy would have caught is silently dropped.
- **Staleness:** the adapter freezes a specific canonical spelling (`"Grey"`); after an attribute rename (`Grey→Gray`) a replay hits normalize's exact tier and misses, where a raw token (`"Preto"`) would re-resolve via synonym every run.
- **Homonym mis-pick decided in the adapter:** `extract_color` picks the first color word by position (`tokens.py:114-123`) — `"Black Forest"` (green marble) → `raw_color="Black"`, and normalize just confirms it.
- Type path is partly rescued by normalize's name-override + reconcile authority, so adapter type-canonicalization is mostly *redundant*; color is *harmful*.

**Fix:** adapters emit the **raw text fragment** (source field or sliced name substring) as `raw_color`/`raw_type`; let normalize own all vocab resolution.

### SC3 — [MED, data-dependent] Batch-wide name cleaning strips legitimate 2-char leads
`adapters/base.py:174-182` scans the whole batch and `detect_code_prefixes` (`core/text.py:188-199`) discovers lead tokens where `looks_codey(tok)` (True for **any `len≤2`**, `core/text.py:179-185`) **and** fanout `≥2`. So `["El Dorado","El Capitan"]` → `"el"` discovered → `clean_variety_name` strips it from **all** rows → `"Dorado"`,`"Capitan"`. Same for `St`, `La`, `Di`, `Mt`. This **contradicts the function's own docstring** ("'El Dorado','Mt Blanc' untouched" — true only at fanout 1). Genuine cross-contamination: a second `El*` product corrupts the first's match key → wrong/failed backbone match + wrong minted name. Also: `_lead_codes` is singleton state only reset when the column is present (`base.py:180`) — latent cross-run contamination if `adapt()` sees a frame without the column. Also: trailing lone-letter grade strip (`core/text.py:228-233`) applies to **all** sources, and a newly-scraped variety not yet in the backbone whose name legitimately ends in a single non-`I` letter loses it.

**Repro:** frame `variety_match_key = ["El Dorado","El Capitan"]` → both become `"Dorado"`,`"Capitan"`. **Fix:** exclude short pronounceable real-word leads from `looks_codey` (a 2-char token with a vowel isn't code-shaped; require no-vowel or a digit); reset `_lead_codes` unconditionally at the top of `adapt()`.

### SC4 — [MED, maintainability] Marenostone dimension columns are misnamed, convention split across two files
Fetcher `scrapers/marenostone.py:176-178` maps HTML `Width`(short face)→column `dimensions_height` and `Thickness`(depth)→column `dimensions_width`; adapter `adapters/marenostone.py:51-56` consumes with matching comments. End-to-end consistent, but **the column literally named `dimensions_width` holds thickness**, and the only source of truth is a pair of comments in two files. An independent edit of either — or "fixing" the apparent mismatch from the CSV header — silently swaps thickness and a face → wrong area/volume → wrong weight+pricing. Plus: `_dims_from_html` captures `out["height"]` (`:93`) that `parse_product` never reads — **dead capture**; if the source ever emits a real Height it's silently dropped. Unit is attached at fetch time and adapter passes `unit=""` (`:55`) — another 2-comment cross-file contract (edit to `unit="cm"` → `"140cmcm"` → parse fail → defaulted dim).

**Repro:** rename CSV column `dimensions_width→dimensions_thickness` in the scraper without updating `adapters/marenostone.py:52` → `raw_thickness` blank, thickness lost, freight defaults. **Fix:** name columns for what they carry (`dimensions_thickness`) or remap to canonical names once at a single boundary; delete or wire the dead `height` capture; assert the unit contract in one place.

**Contract note (scrape):** a fetcher writes with `extrasaction="ignore"` and does **not** validate its own output columns (`base.py:383-388`); the adapter output is validated one stage downstream (Stage 0 health + ingest gate, `run.py:251-297`), not at the fetcher→adapter handoff — deliberate but worth knowing.

---

## 6. Prioritized remediation plan

| Pri | Finding | Why | Blast radius |
|---|---|---|---|
| 1 | **L1 inventory-at-load** | active correctness bugs (blocks=0, 0-slab→6 oversell, static stock, row-internal disagreement, 7-way split) | every product's stock |
| 2 | **T1 derive_origin homonym** | confidently-wrong shipped origin on a homonym; undermines the new multi-country feature | homonym varieties across suppliers |
| 3 | **SC1 zucchi weight** | wrong freight weight, unbounded, false safety-net claim | all zucchi weights + any EU-formatted weight |
| 4 | **T3 finish snap inversion** | "Unpolished"→"Polished" ships wrong attribute | any finish not in a variety's set within 80-89 similarity |
| 5 | **SC2 adapter pre-canonicalize** | dead fuzzy tier → silently dropped colors | zucchi/varsha color coverage |
| 6 | **L2/L3 ledger type resolution + emit defaults** | source-of-truth split; hidden defaults | sync correctness |
| 7 | **SC3 batch name cleaning** | data-dependent name corruption | any batch with ≥2 short-lead names |
| 8 | **T2 category dead-store, C1 stale comment, S1-S4 structural** | latent hazards / doc bugs | future refactors |

None are on fire except L1 (blocks unsellable / oversell) and T1 (wrong origin on homonyms). The rest are latent, data-dependent, or maintainability.

---

## 7. What is CORRECT — do NOT "fix"

- `validate` is a proper boundary gate — no changes needed.
- Template-driven emit `COLUMN_MAP` + `read_template_columns` — the ideal pattern; keep.
- **Port/origin serialization at load** (not computed) — PR #64 fix; correct.
- reconcile's `type_overridden_by_variety` (State 3) and variety-absent (State 4) origin paths — correct.
- reconcile is genuinely idempotent (scalar convergence); the alias-writeback deferral (`match_variation.py:205`) fixes a real order-dependence — don't revert.
- keys_dedupe's deliberate `raw_format`-not-`is_block` read (`keys_dedupe.py:92`) — the one place read-before-write was correctly avoided.
- `format_resolve` living in the clean layer — a documented, deliberate hoist.

---

## 8. Reproduction test index (minimal)
- **L1:** parametrize `inventory_for` + `bundle_size` + `stock_unparseable` over `[("0",slab),("twelve",slab),(None,block),("1,000",slab)]`.
- **T1:** matched row, `variation_key` with unrecognized type slug, homonym name in origin_map both types, normalize-set type; assert origin comes from bound type.
- **T3:** variety `finishes=["Polished","Honed"]`, scrape `"Unpolished"`; assert NOT snapped to Polished.
- **SC1:** zucchi row `weight_kg_net="3.000,5"`,`slab_count="7"`; assert real weight, not density fallback.
- **SC2:** zucchi/varsha product with a valid-but-not-in-vocab or near-miss color; assert normalize fuzzy fires (it won't today).
- **SC3:** frame `["El Dorado","El Capitan"]`; assert match keys preserved.
- **S2:** run a stage twice on the same rows; assert `review_flags`/`reject_reasons` don't double.

---

## 9. Key file index
- Orchestration: `stone_pipeline/run.py`
- Schema: `stone_pipeline/core/schema.py` (CanonicalRow, 89 fields)
- Scrape: `scrapers/*.py`, `stone_pipeline/adapters/{base,zucchi,varsha,marenostone,polonine,tokens}.py`, `config/source_contracts.yaml`
- Clean: `stone_pipeline/stages/{keys_dedupe,format_resolve,normalize}.py`, `core/text.py`, `core/numbers.py`
- Transform: `stone_pipeline/stages/{match_variation,reconcile_tree,derive,constants}.py`, `reference/loaders.py`
- Load: `stone_pipeline/stages/{validate,emit,product_state}.py`, `stone_pipeline/ledger/{populate,render,sync}.py`, `io/medusa_client.py`
