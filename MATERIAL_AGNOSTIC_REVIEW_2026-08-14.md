# Material- & Brand-Agnostic Refactor — Review & Correction Plan (2026-08-14)

**For:** the chat that built the material-agnostic (domain-pack) + brand-agnostic work.
**What this is:** an independent review of that refactor — what's correct (don't redo it), and a **detailed, line-level plan** for the few remaining gaps that stop a second material (wood/lime) from running clean. No code was changed; this is the map.

**Design principle being checked (your stated intent):** the data model — `category → type → variant → color → finish → quality → …` — is **the same for every brand/material**; only the **values differ**, and those values are **picked up from what Medusa exposes** (`attributes.csv`). This review confirms where the code honors that and pinpoints the spots that don't.

**Review method:** 3 independent deep-review agents (abstraction correctness/regression; multi-material + brand readiness; the commit series #182–#193 + the uncommitted `store.py`/`lifecycle.py`), plus the test suite. `test_domains.py` 9/9 green.

---

## 0. THE ACCEPTANCE GATE — a wood smoke run (read this first)

**Nothing here proves wood works. Code review does not.** The only trustworthy gate is the same discipline this project holds everywhere: **a smoke run of a tiny wood pack — a handful of real wood rows through `scrape → derive → emit` — with the output eyeballed for sanity.** Until that dry run exists and is clean, "we did A–F and it works" is a **claim, not a proof.**

The smoke run must check, on the emitted output:
- **Right categories** — wood rows land in wood's categories (not silently bucketed into `slab`).
- **Names not mangled** — the `core/text.py` heuristics (granite-code, loose-number, trailing lone-letter grade strip) did NOT corrupt wood variety names (see GAP F — this is the concrete reason it would not run clean today).
- **Dimensions + density right** — geometry maps to wood's form and weight uses wood density, not marble's 2700 (GAP C).
- **Nothing wrongly held/rejected** — no wood row falls to a stone-tuned default, gets held, or rejected because a stone assumption fired.

**Build the smallest possible `wood.yaml` + a ~10-row wood fixture and run it end to end. If that's clean, it's usable. If it doesn't exist, it isn't done.** Every gap below is a prediction the smoke run will confirm or refute — GAP A and GAP F are the two most likely to fail it.

---

## 1. What is CORRECT — do not redo

- **No stone regression. The externalization is byte-for-byte faithful.** Every value moved into `config/domains/stone.yaml` reproduces the old hardcoded constant exactly — verified against the pre-refactor commit: `default_finishes`, `fallback_color: Natural`, `dimension_ranges`, `last_resort_finishes`/`last_resort_quality`, `block_finish: Raw`, `ambiguous_type_words`, `generic_descriptors` (23 words), `generic_material_word: stone`, `finish_phrases`. The three values that differ from *ancient* literals (finish phrases, block defaults 2.80→2.64, stock 999→per-category) are separate, intentional, test-guarded commits (#39/#180/#186), not silent drift from this refactor.
- **The commit series #182–#193 is correct and safe** — #188 deterministic alias resolver is *strictly safer* (mis-binds → mint/review, never a wrong product), #184 block-from-depth thresholds are sound, the #182/#183/#186 stock ladder can't oversell, #189 factory-reset re-derive is idempotent, #192 cleaning funnel is display-only. The `store.py`/`lifecycle.py` changes (clear diagnostics on global reset; prune stale exports on hard reset) are correct.
- **The identity vocabulary is fully Medusa-sourced — your principle is honored here.** Scraped `color/finish/type/quality` are resolved against `attributes.csv` via the single `VocabResolver` using Medusa's synonyms. The resolver never invents a value and never hardcodes the allowed set. `_assert_pack_defaults_resolve` (loaders.py) even verifies every pack *default* actually exists in Medusa's vocabulary at load. **This part needs nothing.**
- **Brand-agnosticism is essentially achieved — config only, no code coupling.** Destination company/storefront is per-source `vendor` + `company_id` (`config/sources.py:26,31`), sent agnostically in the sync payload (`ledger/sync.py:256`, `populate.py:176`); bucket / sales-channel / company are env-driven with prod fail-loud guards. The `BLOKPORT_` env prefixes and `"blokport-…"` strings are resource/identity names, **not** stone coupling. A different brand needs **no code change** — set `BLOKPORT_S3_BUCKET` / `SALES_CHANNEL_ID` / `COMPANY_ID` and the `vendor` per source.
- **Leaf attributes = color/finish/quality being fixed is CORRECT** under "same data model for all." An earlier draft flagged "wood needs species/grade" — disregard that: if a material's extra dimension (e.g. grade) exists as an attribute in Medusa, the resolver handles it the same way. Only add a *new attribute name* if Medusa genuinely models one for that material.

---

## 2. The gaps to fix (detailed) — the spots that DON'T honor "values from the pack/Medusa"

These are the only things standing between "stone runs" and "wood runs." All are small.

### GAP A — Category VALUES (`slab`/`block`/`tile`) are hardcoded despite the pack declaring `categories`
This is the main "make it data-driven" work. Category is universal, but its *values* are per-material (wood might be `board`/`panel`/`beam`). The pack declares `categories`, but literals still leak in, so a pack with different category names KeyErrors or mis-buckets.

**Exact sites to make pack-driven:**
| File:line | Current | Problem for a non-`slab/block/tile` pack |
|---|---|---|
| `stages/format_resolve.py:94-95` | literal `ranges["slab"]["width"]`, `ranges["block"]["width"]` | KeyError if those category names don't exist |
| `stages/derive.py:135-140` (`_dimension_category`) | hardcodes `"block"/"tile"/"slab"` | any other category silently buckets as `slab` |
| `stages/derive.py:219` | `dimension_defaults[category]` | KeyError if the category isn't in the map |
| `stages/derive.py:259` | `dimension_ranges[category]` | KeyError |
| `stages/derive.py:426` | `in_stock_fallback_qty[_dimension_category(row)]` | KeyError |
| `stages/derive.py:132` (`_FACE_DIMS`) | `("length","height")`, width==thickness, mm-thickness clause, `is_block` short-circuit | geometry assumes slab/block/tile physical form |
| `stages/normalize.py:139-176` | `resolve_id("type", …)`, `fmt == "block"`, `.get("slab")` literals | type-correction + last-resort/block logic assume the disambiguator is literally `type` and the forms are slab/block |
| `ledger/populate.py:67` | `("slab","block","tile")` branch tuple | a pack category loses its ledger branch |
| `ledger/bootstrap.py:26` | `("slab","block","tile")` | same |

**Fix direction:** drive all of these from `active_pack().categories` (each category already carries name/plural/label/backbone_filename/base_image/flags/volume in the pack). Replace the branch tuples with the pack's category-name set; make `_dimension_category` map from the pack's category list, not literals; index `dimension_ranges`/`defaults`/`in_stock_fallback_qty` by the resolved pack category with a **loud** miss (not a KeyError). The geometry (`_FACE_DIMS`, thickness) is the one place a genuinely different *form* (non-slab) needs thought — for wood-as-boards it's still L×W×thickness so it likely maps; flag it if a material has a fundamentally different geometry.

### GAP B — The image colour classifier has a hardcoded palette + a load-gate (the only place a colour VALUE isn't Medusa-derived)
`stages/variety_color.py:28-65` — `classify()` + `CLASSIFIABLE_COLORS` (13 colours: Black/Grey/White/Cream/Red/Brown/Gold/Beige/…), HSV thresholds tuned "natural stone is mostly low-saturation." And `reference/loaders.py:788` (`_assert_pack_defaults_resolve`) **requires those 13 colours to exist in `attributes.csv`** at load.

**Two facts to decide on:**
1. It's a **computer-vision classifier** — a fixed output label space is inherent (you can't classify pixels into arbitrary Medusa labels without retraining). So this "hardcode" is structural, not sloppy.
2. It's a **fallback** — it only fires when no colour was scraped or in the name. So live impact is bounded. **But the load assertion is a hard gate:** a material whose Medusa colour vocabulary doesn't include those 13 labels **fails loud at startup** (a safety net — never silently wrong — but a blocker).

**Fix direction (pick one):**
- **Simplest:** make `CLASSIFIABLE_COLORS` a pack field (the classifier's label set becomes per-material), and the HSV thresholds a pack field too. The load assertion then checks the *pack's* colour set against Medusa. Works if the material's colours are a colour-family palette (wood: brown/beige/red/grey do apply).
- **If a material's "colour" is really a species/tone** not derivable from HSV, the honest answer is: **disable the image-colour fallback for that pack** (a pack flag `classify_texture_color: false`) and rely on scraped/named colour + the Medusa-resolved value. Don't force a stone-tuned CV model onto wood tones.

### GAP C — Weight densities are a stone physics table (not a Medusa value)
`stages/derive.py:462-483` (`_type_density`) reads `reference/type_density.csv` (kg/m³ per stone type) and `standard_slab_area.csv`, defaulting to Marble/2700. This is **physics data, outside your "values from Medusa" rule** (Medusa doesn't carry density), so it's a genuine per-material data need, not a code bug.

**Fix direction:** either add per-material density files at pack-relative paths (a pack field `density_table` / `area_table`), or move the density map into the pack yaml keyed by type. For wood/lime the numbers are very different (wood ~500–900, marble ~2700), so weight-from-dimensions will be materially wrong until this is supplied. Blocks are `is_block` short-circuited so the impact is on slabs/boards.

### GAP D — `_validate_shape` doesn't cross-validate the pack (a bad pack passes then KeyErrors deep in a stage)
`config/domain.py` `_validate_shape` catches many malformed shapes loudly, but misses the cross-checks that would make GAP A safe:
- **V1:** no check that **every category name has an entry in `dimension_ranges`, `dimension_defaults`, AND `in_stock_fallback_qty`.** A category present in `categories` but absent from those maps KeyErrors at `derive.py:219/259/426`. Add this cross-check so a wood pack fails at *load* with a named message, not deep in `derive`.
- **V2:** `disambiguator` is not validated to be a member of `attributes`, and `leaf_attributes` is not validated to equal `attributes − disambiguator`. A mismatch mis-drives the Key and `decisions_store._LEAF_ATTRIBUTES`.
- **V3 (minor):** `finish_phrases` given as a YAML list raises a raw unnamed `ValueError` at load line 139 (loud but not the nice named message); an empty `default_finishes` silently yields fewer finishes.

**Fix direction:** add V1 + V2 to `_validate_shape`. Cheap, and turns every GAP-A KeyError into a clear load-time "wood.yaml: category 'board' missing from dimension_ranges."

### GAP E — Nothing namespaces the pack ON DISK, so two materials can't share one deployment
`BLOKPORT_DOMAIN_PACK` selects a pack but does not namespace any path. Every reference/ground-truth file is one shared path: `from_medusa/<env>/attributes.csv` (one type/color/finish/quality + pcat namespace, `settings.py:173`, `loaders.py:222`), `catalog_source/backbone_*.json`, `reference/synonyms/*.csv` (`loaders.py:567`), `origin_map.csv` (column literally `stone_type`). The S3 layout (`dev/products/`, `dev/variations/`) and the ledger DB path are keyed by **env only** — no material or brand dimension.

**This is a design decision, not a bug:** the real unit is **one deployment = one material pack + one env + one S3 namespace + one ledger**, with brand/company selectable per-source *inside* it. Wood-on-Wudport = a **separate deployment**. Just make that explicit and intended, or (bigger change) add a material/brand path segment if you want co-tenancy. Recommend: keep separate deployments; document it.

### GAP F — `core/text.py` name heuristics are validated against the STONE corpus and will mangle wood names  ⟵ the concrete reason it won't run clean yet
This was missing from the first pass and it's the one most likely to fail the smoke run. `core/text.py` cleans/rejects variety names with **shape heuristics tuned and explicitly validated on the stone naming corpus.** They are not brand/material-parameterized, and for wood they will corrupt real names or fail to clean wood-specific junk:

| File:line | Heuristic | Why it's stone-specific / breaks wood |
|---|---|---|
| `core/text.py:161,172` | `_GRANITE_CODE = [Gg]\d+[A-Za-z]?` — preserve granite codes `G682`/`G032` as real names | A stone convention. Wood has no `G###` names; irrelevant at best, and it encodes "the ONE number a name may keep is a granite code." |
| `core/text.py` `_is_number_code` + step 4 | strip loose/series numbers anywhere (`"883 Black"→"Black"`, `"Marjan No. 426"→"Marjan"`) | Assumes numbers in a stone name are supplier codes. A wood product whose real name legitimately contains a number (a dimension code, a collection number that IS the name) is silently truncated. |
| `core/text.py:240-244` | strip **one trailing lone-letter grade** (`"Rosal C"→"Rosal"`), keep Roman `I` | The justification is a literal check: *"no backbone variety ends in a lone non-`I` letter"* — **validated against the STONE backbone.** Wood varieties may legitimately end in a letter (a grade/series that is part of the trade name); they'd be truncated and mis-merged. |
| `core/text.py:186-289` | `looks_codey` (len≤2), `detect_code_prefixes` batch fanout, `looks_like_artifact` | Stone-tuned junk-shape detection (already flagged separately for corrupting `St Laurent`→`Laurent`). The artifact/mint-refusal shapes are calibrated to stone code conventions, not wood's. |

**Fix direction:** make the naming heuristics **corpus/pack-aware**, not hardcoded to stone conventions — e.g. the "no variety ends in a lone letter" and "keep only granite `G###` numbers" rules should be validated against the **active pack's** backbone/naming rules (or made pack-configurable: a per-pack set of `name_code_patterns` / `keep_number_patterns` / `strip_trailing_grade` toggle), so a wood pack declares its own naming shape rather than inheriting stone's. **The smoke run's "names not mangled" check is exactly the test for this gap.**

### Note — latent test-procedure hazard (not a prod bug)
Several consumers bind pack values into **module-level constants at import** (`normalize.VOCAB_FIELDS:32`, `loaders.VOCAB_CATEGORIES:179`, `decisions_store._LEAF_ATTRIBUTES:39`, `settings.CATEGORIES:612`, `tokens.FORMAT_TOKENS:95`, `derive._FORMAT_WORD:621`). `active_pack.cache_clear()` does **not** rebind these. Fine in production (one pack per process, chosen at startup), but the `domain.py` docstring tells tests that switch `BLOKPORT_DOMAIN_PACK` to just call `cache_clear()`, which is insufficient — an in-process pack switch needs a fresh process. Fix the docstring, or provide a real `reload_pack()` that rebinds, so a future wood test doesn't get a false pass.

---

## 3. Onboarding checklist — to actually run a new material (wood)

Two distinct tracks. **Do not treat the data plane as a flag** — it's the larger, real part of onboarding.

**Track 1 — the DATA PLANE (real onboarding work, per material, not a toggle):**
1. `config/domains/wood.yaml` — same attribute model (category/type/variant/color/finish/quality) with wood's category **names and values**; must pass V1/V2 once added.
2. Wood **`attributes.csv`** (the Medusa vocabulary for wood: its types/colours/finishes/qualities + category pcat ids) — authored from Medusa, the source of truth for values.
3. Wood **`backbone_*.json`** varieties + wood **`synonyms/*.csv`** — a genuine catalogue-authoring effort (the known wood varieties and their alias spellings), not a config change.
4. Wood **density/area** data (GAP C) — real per-material physics data, else weight is marble-wrong.
5. All of the above in a **separate env / S3 namespace / `config.db` / ledger** (GAP E) so wood never collides with stone.

This track is the bulk of the effort and it is **material data authoring**, comparable to onboarding a new supplier's worth of ground truth. Budget it as such.

**Track 2 — the CODE (generalization, once):**
6. GAP A — pack-drive the category literals (`slab/block/tile` → `active_pack().categories`).
7. GAP F — make the `core/text.py` name heuristics pack/corpus-aware (or wood names get mangled — the granite/number/lone-letter rules).
8. GAP D — add the two pack validators (categories ↔ the three maps; `disambiguator ∈ attributes`).
9. GAP B — decide the colour-classifier story (pack-ify the palette, or a `classify_texture_color:false` pack flag).
10. GAP E — document the one-deployment-per-material model.

**Track 3 — THE GATE (before calling it usable):**
11. **Wood smoke run (§0).** Build the smallest `wood.yaml` + a ~10-row wood fixture, run `scrape → derive → emit`, and eyeball the output: right categories, names intact, dimensions/density right, nothing wrongly held. **This is the acceptance test. Until it's clean, wood is not usable — no matter how many of 6–10 are done.**

Effort: GAP A + D + F are the code bulk (mechanical replacements + validators + making the name rules pack-aware). GAP B is a small decision. GAP C and the data plane (Track 1) are the *real* time sink — genuine material-data authoring, not flags.

---

## 4. Test / process notes (so the other chat isn't misled)
- **Full-suite red herrings:** running the whole suite shows the seed fixed-point test and 2 reset tests red. These are **pre-existing local flakiness** — seed data is mid-edit, and the reset/ledger/`config.db` state pollutes across the suite. **Both reset tests pass in isolation**, and the changes are correct. Not regressions from this work.
- **Git-state incident during this review:** the working `store.py`/`lifecycle.py` got committed (`2b24490`) + pushed to `origin/reset-clear-stale-diagnostics` and HEAD briefly moved to `main`. It's been restored to the branch and nothing is lost; noted only so the branch history makes sense.

---

## 5. Bottom line
The refactor is **the right foundation** — stone is untouched, brand is config-only, and the identity vocabulary is genuinely Medusa-sourced exactly as intended. But **A–E is not "and it works"** — it's a claim until a **wood smoke run** (§0) proves it. Two things stand between the current state and a clean dry run:

- **GAP A** (category literals hardcoded) and **GAP F** (`core/text.py` name heuristics validated against the stone corpus — the granite/number/lone-letter rules that will mangle wood names). These are the two that will actually fail the smoke run first.
- The **data plane** (wood `attributes.csv` + `backbone` + `synonyms` + density) is **real onboarding**, not a flag — budget it as material-data authoring.

So the honest close: it's the right architecture and there is **no rewrite** needed — generalize the category literals (A), make the name heuristics pack-aware (F), add the two validators (D), decide the colour classifier (B), author the wood data plane + density (C + Track 1), keep materials as separate deployments (E) — **and then gate the whole thing on a clean wood smoke run before anyone calls it usable.** Until that run exists, treat "wood works" as unproven.
