# Stone scrape to Medusa import pipeline — build specification (v3.1)

This document is the complete build contract. It is written to be read end to end by Claude Code,
which should build the pipeline directly from it. It is exhaustive on purpose: every field, every
failure mode, every recovery path, and the production and scale requirements are specified so the
build does not stall on undefined behaviour and does not need redesign to run at scale.

## How to use this document (read first, for the builder)

1. Read sections 0 to 6 fully before writing code. They define the invariants, the data model,
   the canonical schema, the generic Field Resolver, the matching engine, and the reference data.
   The rest of the document depends on them.
2. Build strictly in the milestone order of section 14. Each milestone is independently testable
   and has a Definition of Done; do not start the next until the current one meets it.
3. Discuss the approach for each stage before writing it, then present the files so they can be
   tested immediately. Keep changes small and verifiable.
4. Two ground truths are provided and must be honoured exactly: a real correct upload file (the
   45-column target shape) and the three reference mappings (the attribute id map, the per
   category variation id map with aliases, and the backbone tree that dictates valid
   combinations). Section 6A explains how they fit together. Validate generated output against the
   real upload file.
5. Production from the start. Sections 13A (production and scale) and 14A (testing and acceptance)
   are not optional polish; they are build requirements for a large scale system.

Changes in v3.1 (folded in after validating the plan against real reference data and two real
scrapes):
- Title, Description, Handle, and Slug are generated fields built from resolved attributes, not
  scrape passthrough. They move into Stage 6 and have explicit templates (section 10).
- Variation ids are category scoped. The same variety has a different id as a block and as a slab,
  so the variation reference and the matcher are selected and blocked by branch (sections 5A, 6,
  Stage 4).
- Leaf validity is set membership against the backbone variety (colour in variety.colors, finish
  in variety.finishes, quality in variety.qualities), not an enumerated tuple list (section 6A,
  Stage 5).
- Type, colour, finish, quality are reconciled against the matched variety, which is authoritative;
  the scrape values are inputs and fallbacks (Stage 3, Stage 5).
- Reference data is a live backend export and can lag; gap detection runs against the live set,
  the backbone holds names only and the ids come from the two csv maps (section 6A, 3.2).
- A per source variety match key is declared by the adapter; generic descriptor sources legitimately
  produce many manual gaps (Stage 1, 7A).
- Added production and scale (13A) and testing and acceptance (14A) sections, with per milestone
  Definition of Done in the build order.

---

## 0. Operating principles (binding on all generated code)

1. No `argparse`. All paths, ids, thresholds, and tunables live in a single labelled config
   block in `config/settings.py` and per-source values in `config/sources.yaml`. Never inline.
2. No em dashes in code, comments, generated docs, or emitted text.
3. Deterministic and idempotent. A re-run on the same input plus the same reference data
   produces byte-identical outputs. Any randomness is seeded from a stable key (section 11).
4. Never guess a value into the output. A value below its confidence floor goes to review or
   reject, never to the import CSV. This is a hard requirement.
5. Provenance on every derived value: each non-passthrough field records the method that
   produced it and a confidence. Nothing in the output is unexplained.
6. Fail loud, fail isolated. A bad row never corrupts a good one. A broken source never
   silently emits garbage; it aborts with a health report.
7. Reference data and aliases are living. Every confirmed match and manual fix is written back
   so the same problem never needs solving twice (section 8.4).
8. The template is the schema authority. Column names, order, and required markers are read
   from the live template file at emit time, never hardcoded (the template has already drifted
   from 44 to 45 columns).

---

## 1. Architecture

Many heterogeneous scrapers fan in. One stable emitter fans out. Between them sits one
canonical staging schema. All expensive logic is written once against the canonical shape.
Adding a source is one thin adapter; nothing downstream changes.

```
per-source scrape file (N shapes)
  -> STAGE 0  health and schema-drift gate     did the scrape work? did the site change?
  -> STAGE 1  ingest and adapter                map source columns into canonical rows
  -> STAGE 2  key integrity and dedup           surrogate keys, exact dedup, near-dup flag
  -> STAGE 3  normalize vocabulary              color, finish, type, quality -> attribute id
  -> STAGE 4  resolve variation                 exact, alias, fuzzy, splink, gap queue
  -> STAGE 5  tree reconciliation               confirm the (type,var,finish,color,quality) leaf
  -> STAGE 6  field derivation                  bundle size, dimensions, units, origin, category
  -> STAGE 7  image staging                     download, hash dedup, placeholder block, slot
  -> STAGE 8  apply config constants            company id, sales channel, ports, flags
  -> STAGE 9  validation gate                   hard fails -> reject; soft -> review
  -> STAGE 10 emit                              template-driven CSV plus all side artifacts
```

Five subsystems cut across the stages and are the reason this plan exists:

- **Health and drift** (section 7, Stage 0): detects a failed or changed scrape before any
  data is trusted, and localizes the change for repair.
- **Matching engine** (section 5A): one alias-aware, multi-method resolver shared by variation,
  attribute, and origin matching, so every match uses every reasonable method before giving up.
- **Field Resolver** (section 5): the single generic mechanism for producing every derived or
  matched field, with confidence and a defined fallback ladder, so "the scrape is missing X"
  is handled uniformly.
- **Tree-gap loop** (section 8): when scraped data does not fit the backend tree, it is flagged
  with everything needed to add the missing variant or attribute, then a re-run absorbs it.
- **Adapter framework** (section 7A): adding a source is a declarative, fixture-tested task and
  repairing one after a site change is a localized, verified edit.

Overrides and write-back (section 8.4) close both loops: manual fixes enter through a keyed
overrides file and confirmed matches flow back into reference data, both idempotent.

Every stage emits to at most three sinks: the next stage (clean rows), the review artifact
(soft issues, still potentially emittable), or the reject artifact (hard failures). No stage
silently drops or mutates a value without a provenance and, where relevant, a flag.

---

## 2. Run artifacts (every run produces all of these)

| artifact | content | consumer |
|---|---|---|
| `medusa_import_<source>_<ts>.csv` | rows that passed every gate, template shape | the Medusa import / API |
| `review_<source>_<ts>.csv` | rows that emitted but carry soft flags, with reasons and best-guess values | human review, optional hold |
| `rejects_<source>_<ts>.csv` | rows that cannot emit, with the failing rule(s) | human fix, re-run |
| `tree_gaps_<source>_<ts>.csv` | distinct missing variants and attributes to add to the backend tree | human + backend admin |
| `scrape_health_<source>_<ts>.json` | drift report, status OK / DEGRADED / FAILED, per-field metrics vs baseline | gating, monitoring |
| `run_manifest_<ts>.json` | counts at each stage, config hash, reference-data versions, code version, timings | provenance, debugging |

A run is reproducible from the manifest: it pins the input hash, the reference-data versions,
and the config hash. If any of those change, the manifest records it.

---

## 3. Configuration and static values

Two layers. Global defaults in `config/settings.py`; per-source values in
`config/sources.yaml`. A per-source value overrides the global default.

### 3.1 Static backend constants (the values that do not vary per product)

| key | scope | meaning |
|---|---|---|
| `company_id` | per source | the Medusa account that owns products from this site. Ownership is per source, which is why the same stone from two sites is two products and cross-source dedup is out of scope. |
| `sales_channel_id` | per source | the sales channel products attach to. |
| `port_ids_default` | per source | fallback ports when origin lookup yields none (see 3.3). |
| `cat_blocks_pcat`, `cat_slabs_pcat` | global, env | category ids for the block and slab branches. |
| `visibility` | global | constant, public. |
| `discountable` | global | constant, TRUE. |
| `status` | global | constant, published. |
| `variant_defaults` | global | Variant Title, Manage Inventory (TRUE), Allow Backorder (FALSE), Option 1 Name and Value. |
| `image_base_url` | global, env | public S3 base prefixed to staged keys. |
| `s3` | global, env | bucket, region, key prefix, credentials profile. |

### 3.2 Environment binding and re-seed safety

All backend ids (`company_id`, `sales_channel_id`, `cat_*_pcat`, attribute and tree ids in
reference data) belong to one backend environment. Ids change when the backend is re-seeded.
`config/settings.py` carries an `environment` label and a `backend_id_fingerprint` (a hash of
the live id set). At startup the pipeline checks the fingerprint against the live backend (or a
snapshot) and aborts with a clear message if it has drifted. This is the documented cause of the
earlier "No stock location" and stale-id import failures. A stock location must exist in the
backend and be linked to `sales_channel_id`, or every Manage-Inventory row fails on import; the
startup check asserts this too.

### 3.3 Ports policy (decide once, stated explicitly)

Ports are resolved per product from origin country, not as a flat source constant. The resolver
(section 5) looks up 1 to 2 port ids from `reference/ports.csv` where `country_iso` equals the
resolved `origin_country_code`. `port_ids_default` is used only when origin is unresolved, and
its use raises a review flag so a fixed-port fallback is never silent.

### 3.4 Thresholds (single source of truth, all matching and derivation read these)

| key | meaning | default |
|---|---|---|
| `variation_auto_accept` | score at or above which a fuzzy/splink variation match is accepted | 92 |
| `variation_review_floor` | score band that goes to review rather than gap | 84 to 92 |
| `attribute_fuzzy_floor` | min score for a fuzzy attribute (vocab) match | 90 |
| `derived_accept` | confidence level at or above which a derived field is accepted | medium |
| `health_fill_drop_warn` | per-field fill-rate drop vs baseline that warns | 0.15 |
| `health_fill_drop_fail` | drop that fails the run | 0.40 |
| `health_rowcount_floor` | fraction of baseline row count below which the run fails | 0.50 |

Confidence is a four-level enum, `high > medium > low > none`, with a fixed numeric mapping in
config so thresholds compare uniformly.

---

## 4. Canonical staging schema

One parquet table, one row per cleaned product. Adapters fill `src_` and `raw_`; stages fill the
rest. Every matched or derived field has a sibling `_confidence` and `_method` (or `_source`).

| field | provenance | notes |
|---|---|---|
| `src_site` | config | source id, e.g. ferraz |
| `src_natural_key` | scraped | source unique id; dedup key |
| `surrogate_key` | system | always-present stable id; equals natural key when valid, else minted (section 11) |
| `src_url`, `scrape_timestamp` | scraped | provenance and traceback |
| `raw_name` | scraped | the source product name; may be a named variety or a generic descriptor |
| `variety_match_key` | adapter | the field(s) the adapter declares as the variety name for matching (e.g. polonine `material`); may be empty for generic-descriptor sources |
| `raw_type, raw_color, raw_finish, raw_quality` | scraped | may be empty |
| `raw_format` | scraped | Tile, Slab, Block, or empty |
| `raw_origin` | scraped | usually empty |
| `raw_thickness, raw_dimensions, raw_weight` | scraped | unparsed strings |
| `raw_total_m2, raw_per_slab_m2, raw_slab_count, raw_bundle_size, raw_slabs_array` | scraped | bundle-size inputs, any subset present |
| `raw_image_urls` | scraped | ordered list of source URLs |
| `raw_description` | scraped | often empty; when present, preferred over the generated description |
| `type_id, type_name` | resolved | from the matched variety (authoritative) mapped via attributes; `type_confidence`, `type_method` |
| `color_id, finish_id, quality_id` | resolved | scrape value validated against the variety's allowed set, then mapped to id; each with `_confidence`, `_method` |
| `variation_id, variation_name` | matched | from the branch's variants file; nullable before gap resolution; `variation_confidence`, `variation_method` |
| `category_pcat_id` | derived | from format; Tiles route to Slabs; `category_method` |
| `format_value` | retained | Tile, Slab, or Block, kept so the distinction survives |
| `is_block` | derived | branch flag from format; selects variants file, image slotting, dimension rules |
| `sold_in_bundle, bundle_size` | derived | with `bundle_size_confidence`, `bundle_size_method` |
| `weight, length, width, height` | derived | normalized units; each with `_method` |
| `origin_country_code, origin_city, origin_county` | derived | with `origin_confidence`, `origin_source` |
| `title` | generated | constructed from variety + finish (NOT format -- format is a variant dimension) (section 10.3); raw_name used only as fallback |
| `description` | generated | templated from variety, colour, type, finish, format, origin (section 10.4); raw_description used when present |
| `image_keys, thumbnail_key, oriented_image_keys, product_image_keys` | image stage | staged S3 keys, slotted by branch |
| `company_id, sales_channel_id, port_ids, visibility, discountable` | config | |
| `handle, slug` | generated | slugify(title), namespaced for global uniqueness; built after title |
| `review_flags` | system | list of structured flags, see 9.1 |
| `reject_reasons` | system | list of hard failures, empty if emittable |
| `row_fingerprint` | system | hash of the inputs that determine the output, for idempotency checks |

---

## 5. The Field Resolver (the generic mechanism)

This is the heart of "handle missing and malformed data without bespoke code per field". Every
derived or matched field is produced by a Resolver: an ordered list of Strategies.

```
Resolution = { value, confidence in {high,medium,low,none}, method, evidence }

class Strategy:        # pure function, no side effects
    name: str
    def try(self, row, refs) -> Resolution | None

class FieldResolver:
    field: str
    strategies: list[Strategy]   # ordered, cheapest and most trustworthy first
    accept: confidence           # min confidence to accept silently
    def resolve(self, row, refs) -> ResolvedField:
        results = [s.try(row, refs) for s in self.strategies]
        results = [r for r in results if r is not None]
        best = max(results, key=confidence) if results else Resolution(None, none, "no_strategy", {})
        if best.confidence >= self.accept:
            return ResolvedField(best.value, best, flag=None)
        # below floor: never write to output silently
        return ResolvedField(None or best.value_if_review_allowed, best,
                             flag=ReviewFlag(field, row.raw_inputs, best))
```

Rules that make this generic and safe:

- Strategies are ordered by trust, not just by availability. A high-trust direct field beats a
  low-trust derivation even if both fire.
- A Resolver never invents. If no strategy reaches `accept`, the field is null (or a flagged
  best-guess if the field config permits review emission) and a structured review flag is added.
- Manual overrides are themselves the highest-priority Strategy for every resolver (section 8.4),
  so a human value always wins and is the cleanest way to inject a missing element.
- Every output field maps to exactly one Resolver. Adding a new field, or a new way to fill an
  existing one, means adding a Strategy, not touching stage code. This is the flexibility the
  plan must preserve.

Resolvers used: attribute (type, color, finish, quality), variation, category, origin,
bundle_size, dimension (length, width, height, weight), ports, images-slotting, title,
description, handle. Sections 6A, 10 specify the non-obvious ladders and the generation templates.

---

## 5A. The variation matching engine (use every method before giving up)

Variation resolution is the hardest and highest-value match in the pipeline, and the most
likely to wrongly declare a gap if done naively. It is therefore built as a layered engine, not
a single fuzzy call. The goal is explicit: exhaust every reasonable method to find a home for a
scraped name before routing it to manual assistance. Aliases are first-class and used at every
tier, not just one.

### 5A.1 The alias-aware candidate index

Build one index that flattens, for every variation, all of its known surface forms into lookup
keys, each mapped back to the variation id:

- the canonical variation name,
- every entry in its `Aliases` gazetteer (trade names, supplier spellings, translations),
- and, growing over time, every scraped spelling confirmed by write-back (section 8.4).

For each surface form, precompute and index several normalized projections so different classes
of difference all become exact-lookup hits:

| projection | catches |
|---|---|
| `norm` (casefold, strip punctuation, collapse spaces) | case and punctuation noise |
| `compact` (alnum only, no spaces) | spacing and concatenation (Mont Blanc vs Montblanc) |
| `tokenset` (sorted token tuple) | word-order reversal (Cosmic Black vs Black Cosmic) |
| `deprefixed` (strip inventory prefixes Z, ZB, Z B; strip trailing render or grade tags) | supplier inventory codes |
| `phonetic` (double metaphone per token) | spelling-by-ear typos (Capuccino vs Cappuccino) |

The candidate index is built per category (one over `variants_slabs.csv`, one over
`variants_blocks.csv`), and every tier is blocked first by branch (block or slab), then by
normalized type and colour, so a slab variety never matches a block id and Black Pearl granite
never matches Black Pearl marble.

The index is keyed per projection so a lookup is O(1) per projection. Blocking metadata (parent
type, colour) is stored alongside each candidate for the later tiers.

### 5A.2 Tiered resolution (cheapest and most certain first)

A scraped name runs the tiers in order and stops at the first confident hit. Every tier consults
the alias index, not only the canonical names.

1. **Override** (manual_overrides) -> high.
2. **Exact** on `norm` against canonical names and aliases -> high.
3. **Projection-exact**: `compact`, then `tokenset`, then `deprefixed` exact lookups -> high.
   These are deterministic and safe (they only collapse known, reversible differences) and they
   recover the large class of "very similar" names for free.
4. **Phonetic exact**: double-metaphone match, guarded by a character-similarity floor so it does
   not over-merge -> medium-high.
5. **Fuzzy**: rapidfuzz `token_sort_ratio` and full `ratio` (take the max), never `token_set`
   (which collapses Crystal Frost into Crystal). Block candidates by normalized type and colour so
   only plausible candidates compete. Accept at `variation_auto_accept`; a token-count and length
   guard prevents a short generic candidate from winning -> medium.
6. **Token and n-gram overlap**: Jaccard over word tokens and over character trigrams for
   multi-word names where order and spelling both drift; type/colour blocked -> medium, review
   band only.
7. **Splink** on the residual only, multi-field (type, colour, name tokens) with EM weighting and
   the same blocking. Skip if the residual is too small to calibrate, and route those to review
   instead -> probabilistic, review band.
8. **Semantic (optional, config-gated)**: sentence-embedding nearest neighbour over canonical plus
   alias strings, for cross-language and true synonym cases (Bianco Carrara vs Carrara White) that
   string methods miss. Used only as a last suggestion feeding review, never auto-accept, because
   it is the least explainable -> suggestion only.

### 5A.3 Outcomes

- At or above `variation_auto_accept`: accept, set method to the winning tier, and write the
  scraped spelling back as a new alias (section 8.4) so it is a tier-2 hit next time.
- In the `variation_review_floor` band: route to review with the top one to three candidates and
  their tiers and scores, so a human confirms with one click; confirmation writes back.
- Below the floor, or no candidate at all: route to the tree-gap queue as `missing_variation`,
  carrying the nearest candidate and score so the human can decide add-new versus alias-existing.

Every tier records its method and score on the row, so a match is always explainable and the mix
of methods per run is measurable (and should shift toward tiers 1 to 3 as write-back accumulates).

### 5A.4 Reuse for attributes and origin

The same projection-and-tier approach (without splink and embeddings) backs the attribute
normalization (Stage 3) and origin name lookup (Stage 6). One matching module, three callers, so
improvements to the engine benefit all matched fields.

---

## 6. Reference data the pipeline consumes

All id-bearing reference files are exports of the live backend and can lag it. They are versioned
(a content hash recorded in the manifest) so a run is reproducible against an exact snapshot, but
gap detection and id validity are checked against the live backend set at run start (section 3.2),
not against a possibly stale file.

| file | purpose | status |
|---|---|---|
| `reference/attributes.csv` | category and color, finish, type, quality value to backend id. Columns: `category, value, sourceid`. category in {color, finish, type, quality, category}; the two category rows give the Blocks and Slabs pcat ids | have |
| `reference/variants_slabs.csv` | slab variation id, name, aliases, key. Columns: `Id, Key, Name, Image, Aliases` (Aliases pipe-separated) | have |
| `reference/variants_blocks.csv` | block variation id, name, aliases, key. Same shape. Variation ids are category scoped, so a variety has a different id as a slab and as a block | to supply |
| `reference/backbone.json` | the tree. One record per variety: `variant`, `stone_type`, allowed `color[]`, `finishes[]`, `qualities[]`, `aliases[]`. Holds names only, no ids. Dictates which combinations are valid | have (slabs) |
| `reference/origin_map.csv` | stone or variation name (or pattern) to country, city, county | to supply |
| `reference/ports.csv` | port id, country_iso, size | have |
| `reference/synonyms/*.csv` | per-vocabulary synonym dictionaries, seeded from observed values | to build |
| `reference/units.csv` | unit token to canonical unit and factor (mm,cm,m; m2; in,ft) | to build |
| `reference/placeholder_hashes.csv` | content hashes of known placeholder and watermark images | grows over time |
| `reference/standard_slab_area.csv` | per type or per source default slab area for last-resort bundle derivation | to supply |
| `config/source_contracts.yaml` | per-source expected columns, fill floors, value patterns, row-count baseline, adapter version | to build |
| `state/scrape_baselines.json` | last-good per-source metrics, auto-updated each healthy run | system-managed |
| `state/manual_overrides.csv` | keyed (src_site, surrogate_key) field overrides, human-supplied | grows over time |
| `config/sources.yaml` | per source: adapter, company id, sales channel, ports default | to supply |

---

## 6A. The data model: how the three mappings produce the tree ids

This is the contract for the tree-dependent columns, verified against a real correct upload.
Three files, three jobs:

- **`backbone.json` decides what is valid.** For each variety it lists the `stone_type` and the
  allowed sets of colour, finish, and quality. A combination is valid if, for the matched variety,
  the chosen colour is in `variety.color`, the chosen finish is in `variety.finishes`, and the
  chosen quality is in `variety.qualities`. This is set membership, not an enumerated tuple table.
  The backbone holds names only.
- **`attributes.csv` maps attribute names to ids.** Once the colour, finish, quality, and type
  names are known, their backend ids (STN Color Id, STN Finish Id, STN Quality Id, STN Type Id)
  and the category pcat (Product Category 1) come from this file by name.
- **`variants_<category>.csv` maps a variety name (and aliases) to its variation id.** Selected by
  branch: slabs use `variants_slabs.csv`, blocks use `variants_blocks.csv`. STN Variation Id comes
  from here.

Resolution order for one row:

1. Branch from format to block or slab. This selects the category pcat and which variants file the
   variation match uses.
2. Match the scraped variety name (plus aliases) against the branch's variants file (engine,
   section 5A) to get the variation id. The variety is authoritative: its `stone_type` and its
   allowed colour, finish, and quality sets come from the backbone, not from re-reading the scrape.
3. Choose colour, finish, quality from the scrape, validated to be in the variety's allowed sets
   (snap or gap otherwise, Stage 5). Resolve their names to ids via `attributes.csv`.
4. STN Type Id is the id of the variety's `stone_type`; STN Type Name equals that name.

Validation that this is correct: across a real 200-row upload, every colour, finish, quality, type
id and category resolved through `attributes.csv` with zero errors, Type Name always equalled the
Type Id name, every slab row was a valid backbone leaf by membership, and the slab variation ids
all resolved through the slabs variants file. The only rows not resolvable via the slabs file were
blocks, which require `variants_blocks.csv`. The pipeline must implement exactly this flow.

---

## 7. Stage specifications

### Stage 0: health and schema-drift gate (did the scrape work, did the site change)

Runs before ingest. Compares the incoming file against the source contract and the last-good
baseline. Produces `scrape_health_<source>.json` with status OK, DEGRADED, or FAILED.

Checks:

1. **Structural**: required columns present (from `source_contracts.yaml`). Missing required
   column or unreadable file -> FAILED. New unexpected columns -> DEGRADED warning (adapter may
   need updating).
2. **Volume**: row count vs baseline. Below `health_rowcount_floor` of baseline -> FAILED
   (likely blocked, paginated wrong, or the listing layout changed).
3. **Fill rate**: per-field non-empty fraction vs baseline. A drop past `health_fill_drop_warn`
   -> DEGRADED; past `health_fill_drop_fail` on a required field -> FAILED (a renamed or moved
   field is the classic site-change signature).
4. **Shape**: key fields match expected patterns or value sets (e.g. format in {Tile,Slab,Block},
   price numeric, image URL host). High violation rate -> DEGRADED or FAILED per config.
5. **Parse smoke test**: run the adapter on a sample (e.g. 50 rows); if the adapter raises or
   yields empty canonical rows above a small ceiling -> FAILED.

Behaviour: FAILED aborts the run before any emit and writes the health report only. DEGRADED
continues but stamps every row and the manifest with the warning so downstream review knows the
batch is suspect. OK updates `scrape_baselines.json` with the new metrics (this is how the
baseline self-tunes). The baseline is only updated on OK runs so a degraded scrape never poisons
the reference point.

This is the "smart system indicating the scrape failed" requirement. It is contract plus
baseline-drift, not a single brittle assertion.

**Drift diagnosis for repair**: when the status is DEGRADED or FAILED, the health report does not
just say "something changed". It localizes the change for fast adapter repair: which required
columns disappeared or were renamed (matched by header similarity and by value-shape similarity
to old columns), which fields lost fill, and which value patterns broke, with a few example rows.
This diagnosis is the input to the adapter repair loop (section 8.5), so a site format change
becomes a targeted edit to one adapter rather than a hunt.

### Stage 1: ingest and adapters

Input one source file, output canonical rows with `src_` and `raw_` set. Each adapter subclasses
`AdapterBase`, which defines the canonical schema and asserts the required `src_` fields. Adapter
does only column mapping and light parsing (split a pipe list, pull type after a pipe in a
categories string, strip a trailing token such as polonine's "Granite /", collect image URLs,
ignore known-junk columns such as temmer's global colour menu). No normalization, no matching, no
derivation here, and the adapter does not build title, description, handle, or slug; those are
generated downstream in Stage 6 from resolved attributes. Build `AdapterBase` first, then develi
(small, clean vocab), prove the whole spine on it, then add the rest.

The adapter also declares the `variety_match_key`: which scraped field carries the variety name
for variation matching. Some sources expose a clean named variety (polonine `material`, e.g.
ALPINE, ARABESCATTO EXTRA) and resolve cleanly. Others carry only generic descriptors (marenostone
`name`, e.g. "Cream Marble Tile", "Black Granite Tile") with no named variety; the adapter extracts
the candidate variety by stripping known colour, type, and format tokens, and where nothing
remains the variety is empty. Such rows legitimately resolve to a null variation and route to the
tree-gap queue for manual assistance. This is correct behaviour, not a failure, and it means
result quality varies by source: a named-variety source flows through, a generic-descriptor source
yields many manual gaps.

Per-source field availability is known and drives each adapter (origin absent almost everywhere,
temmer colour is junk, ferraz country fields empty across all rows, polonine quality vocab is
First/High Standard/Premium/Commercial and needs a synonym map, no natural-key duplication in
samples). The adapter map encodes these facts; the source contract encodes the expectations that
Stage 0 checks.

### Stage 2: key integrity and dedup

1. **Surrogate key**: if `src_natural_key` is present and unique, `surrogate_key` equals it. If
   missing or blank, mint a deterministic surrogate from `sha1(src_url or raw_name + ordinal)`.
   marenostone already shipped 7 empty SKUs; never let a blank key flow into handles, image keys,
   or dedup.
2. **Exact dedup** on `surrogate_key`. Keep first by scrape order, record the drop count.
3. **Near-duplicate flag**: same normalized name plus block plus colour gets a `near_duplicate`
   review flag. Never auto-merge.
4. Assert `surrogate_key` uniqueness after this stage; a residual collision is a hard error in
   the run, not a row reject, because it means key minting is wrong.

### Stage 3: normalize controlled vocabulary

Produce `type_id, color_id, finish_id, quality_id` from raw values against the closed vocabulary.
This is normalize-then-lookup, not record linkage. Pipeline per field, run through the attribute
Resolver:

1. Override strategy (manual_overrides) first.
2. Casefold, strip, collapse whitespace, split multi-value (e.g. "Black | Grey" -> first plus a
   `multi_value_color` review note).
3. Synonym dictionary per vocabulary (surface "Leather Finish" -> Leathered; quality "Premium"
   -> defined target; finish "Other" -> none, not an error).
4. Exact match against the canonical list.
5. `rapidfuzz` fallback with `attribute_fuzzy_floor`, using token_sort and full ratio, never
   token_set.
6. Unresolved -> id null, review flag naming field and raw value. Do not guess.

Seed synonym dictionaries from observed per-source values. Day-one known gaps: marenostone
quality Premium and finish Other; temmer has no finish or quality, which is null, not an error.

### Stage 4: resolve variation

Produce `variation_id, name, confidence, method`. Nullable before gap handling because many
scraped products are generic descriptors with no named variety. This stage is the variation
matching engine in section 5A: an alias-aware, multi-method, tiered resolver that exhausts every
reasonable method (exact, projection-exact, phonetic, fuzzy, overlap, splink, optional semantic)
before declaring a gap. It is **category scoped**: the branch (block or slab from Stage 6 format,
or the adapter's format hint) selects `variants_blocks.csv` or `variants_slabs.csv`, because the
same variety has a different variation id as a block and as a slab. The matcher blocks on category,
then type and colour, so Black Pearl granite never matches Black Pearl marble and a slab variety
never matches a block id. The input is `variety_match_key`; aliases participate in every tier.

Outcomes follow 5A.3: auto-accept with alias write-back; review band with the top candidates for
one-click confirmation; below floor or no candidate -> `missing_variation` in the tree-gap queue.
A confirmed match from any non-exact tier or from review appends the scraped spelling to that
variation's aliases (section 8.4), so the next scrape resolves it as an exact alias hit for free
and the matcher gets more consistent every run.

### Stage 5: tree reconciliation (the shoehorn check)

Normalization (Stage 3) and variation (Stage 4) are resolved independently, so nothing yet
guarantees the combination is valid for the matched variety. This stage enforces it, with the
matched variety as the authority.

The matched variety's backbone record gives its `stone_type` and its allowed `color`, `finishes`,
and `qualities` sets. Validity is set membership: the chosen colour must be in the variety's
colours, the chosen finish in its finishes, the chosen quality in its qualities. There is no
enumerated tuple table. Reconcile as follows:

1. Set `type_id`/`type_name` from the variety's `stone_type` (authoritative), overriding any
   independently normalized type, and flag `type_overridden_by_variety` if they differed.
2. If the chosen colour, finish, or quality is not in the variety's allowed set, try to snap to the
   nearest allowed value for the lowest-trust attribute first (quality, then finish), recording the
   snap in `_method` and a `leaf_snapped` review flag.
3. If the variety genuinely has no allowed value matching the required colour or finish, this is a
   **tree gap** of kind `missing_leaf_child`: the variety exists but lacks the needed finish or
   colour. Route to the tree-gap queue with the specific missing child identified.
4. A null variation cannot be validated, so it always routes to the tree-gap queue as
   `missing_variation` for manual assistance. There is no generic catch-all: a product with no
   matched variety is held until a human adds the new variant to the backend tree or confirms it is
   an alias of an existing one. A null variation must never reach the emitter, because the template
   requires `STN Variation Id` and a valid combination.

Only after membership holds are the names mapped to ids (colour, finish, quality, type via
`attributes.csv`; category pcat from the branch), per section 6A.

### Stage 6: field derivation

Run the remaining Resolvers. The non-trivial ones:

- **Category**: from `format_value`. Slab and Tile route to the Slabs pcat; Block routes to the
  Blocks pcat; empty or unknown format -> classify via a rule (presence of thickness and area
  suggests slab) and flag `format_inferred`.
- **Units and dimensions**: parse raw dimension and thickness strings through `units.csv` to
  canonical metres before any area or weight math. Reject silently-wrong units (e.g. a 2000 value
  that is mm not m) via range sanity checks, flagging `dimension_out_of_range`. Apply the
  block/slab dimension rules (section 10.2) only after parsing.
- **Bundle size**: the full ladder in section 10.1. This is the worked example of deriving a
  missing field from related fields (per-slab area and total area).
- **Origin**: order of trust is a populated source location or country field (rare, high) ->
  `origin_map.csv` by name or pattern (medium) -> the matched variation's origin (medium) ->
  none plus review flag. Record `origin_source` and `origin_confidence`. A resolved review writes
  back into `origin_map.csv`.
- **Ports**: from `origin_country_code` against `ports.csv`, 1 to 2 ids; fallback to
  `port_ids_default` only with a flag.
- **Title** (generated, section 10.3): construct from the resolved variety and finish; the FORMAT word
  (Slab/Block/Tile) is NOT in the title -- format is a variant dimension, not product identity
  (e.g. "Walnut Travertine Honed", never "Walnut Travertine Honed Slab"). The format word stays in the
  description prose only. Use `raw_name` only as a fallback when no variety resolved. Built here, after
  variation and attribute resolution, never in the adapter.
- **Description** (generated, section 10.4): if `raw_description` is present and usable, keep it;
  otherwise template from variety, colour, type, finish, format, and origin. Because the template
  reads origin, run after origin resolution.
- **Handle and slug** (generated): `slugify(title)`, namespaced with the source code and surrogate
  key for global uniqueness; identical handle and slug. Built after title.

Ordering within Stage 6: category and units first, then bundle and dimensions and origin, then
title, then description (needs origin), then handle (needs title). The Field Resolver makes each of
these one strategy ladder, so the ordering is a dependency declaration, not bespoke control flow.

### Stage 7: image staging to S3

Input `raw_image_urls`, output staged keys slotted for the branch.

1. Download each image (httpx), skip a failed download with a `image_download_failed` flag.
2. Content hash each image. Dedup identical bytes so they upload once.
3. Block known placeholders and watermark swatches via `placeholder_hashes.csv`; treat a
   placeholder as no image, raise `image_is_placeholder`. This prevents hash-dedup from silently
   pointing hundreds of products at one stock graphic.
4. Upload with a content-addressed, deterministic key:
   `staging/{src_site}/{sha256}.jpg`. No uuid. A re-run re-derives the same key and is a no-op,
   which fixes the idempotency and mix-up risk you flagged: the product to image link lives only
   on the canonical row as an ordered list of (source_url, hash, key), never inferred from the
   filename.
5. **Slot by branch** (the earlier "mixed up" bug): blocks fill Front, Right, Back, Left in order;
   slabs fill Product Image 1..5 in order; thumbnail is the first image either way. Do not put
   block images in the product-image slots or vice versa.
6. A product with zero usable images after this stage gets `no_image` and is held or rejected per
   the validation switch.

### Stage 8: apply config constants

Set `company_id` (the owning Medusa account for the source), `sales_channel_id`, `visibility`,
`discountable`, and the variant defaults from config. Ports are already resolved (Stage 6),
per-origin, not as a flat constant.

### Stage 9: validation gate

Reject a row (write to rejects with reasons) on any hard failure:

- a required attribute id is null (type, colour, finish, quality, per the template required
  markers read live in Stage 10);
- the chosen colour, finish, or quality is not allowed for the matched variety (membership fails), or `variation_id` is null;
- `category_pcat_id` is not the branch's valid category;
- `handle` or `slug` is not globally unique after namespacing;
- `origin_country_code` is not a valid ISO2 when origin is required by config;
- no image staged and images are required by config;
- any FIXED backend id fails the live-environment check (section 3.2).

Soft issues (review flags, no hard failure) emit or hold per the `emit_on_review` config switch.
Validity is membership in the matched variety's allowed colour, finish, and quality sets from the backbone (section 6A), not an enumerated tuple table.

### Stage 10: emit

Read the live template to get the exact column list, order, and required markers. Map each
canonical row onto those columns. Namespace `handle` and `slug` with a short source code plus the
surrogate key so two sources never collide and a re-scrape produces the same handle (which the
later API client must upsert on, not insert). Booleans emit as uppercase TRUE and FALSE. Write
the import CSV plus the review, reject, tree-gap, health, and manifest artifacts. Later milestone:
swap the CSV writer for a direct Medusa API client with upsert semantics; nothing upstream
changes.

---

## 7A. Adapter framework (adding sources and repairing them)

Sources will be added as the system grows, and scraped sites will change format, repeatedly and
without warning. Both must be cheap. The adapter layer is designed so that adding a source is a
small declarative task and repairing one after a site change is a localized, verified edit.

### 7A.1 Adapters are thin and declarative

Most of an adapter is a **field map**, not code: a declarative mapping from source columns (or
simple extraction rules) to canonical `src_` and `raw_` fields, expressed as data in the adapter
plus a small amount of per-source parsing for the few fields that need it (split a pipe list,
pull type after a pipe, drop a known-junk column). `AdapterBase` owns the canonical schema, the
required-field assertions, and all shared parsing helpers, so an adapter author writes only what
is genuinely source-specific. The same declaration also generates the source's entry in
`source_contracts.yaml` (expected columns, required set), keeping the health contract and the
adapter in sync by construction.

### 7A.2 Every adapter ships a golden fixture and self-test

Each adapter has, under `adapters/fixtures/<source>/`, a small committed input sample and the
expected canonical output for it. A single test runner replays every fixture and asserts the
adapter still produces the expected canonical rows. This is what makes both onboarding and repair
safe: a new adapter is "done" when its fixture passes, and a repaired adapter is verified the
instant its fixture passes again. Fixtures are tiny (tens of rows) and are the regression net for
site changes.

### 7A.3 Onboarding a new source (the checklist the framework enforces)

1. Drop a sample scrape under `adapters/fixtures/<source>/input`.
2. Write the field map (mostly declarative) in `adapters/<source>.py` and add the source to
   `config/sources.yaml` (company id, sales channel, default ports) and `sources.yaml` adapter
   binding.
3. Generate the contract and an initial baseline from the sample (the framework does this).
4. Capture the expected canonical output as the fixture and run the self-test.
5. Run the existing spine end to end on the sample. Because every stage after the adapter is
   source-agnostic, nothing downstream is touched. A new source is one adapter plus one config
   block, never a change to a stage.

### 7A.4 Adapter versioning

Each adapter carries a version. The version is stamped on every canonical row and in the manifest,
so a row can always be traced to the adapter logic that produced it, and a site-change repair is a
visible version bump rather than a silent edit.

---

## 8. Exceptions, gaps, and the manual-update loop

### 8.1 Exception taxonomy and handling matrix

Every known failure mode, where it is caught, where it goes, and how it clears. This table is the
checklist that keeps the pipeline from breaking silently.

| condition | detected at | sink | clears by |
|---|---|---|---|
| scrape file unreadable or missing required column | Stage 0 | health FAILED, abort | fix scraper or adapter contract |
| row count collapse | Stage 0 | health FAILED, abort | re-scrape, investigate site change |
| field fill-rate drop on required field | Stage 0 | health FAILED or DEGRADED | update adapter or contract |
| new unexpected column | Stage 0 | health DEGRADED warn | extend adapter, update contract |
| missing or blank natural key | Stage 2 | surrogate minted, info note | none needed (deterministic) |
| near-duplicate products | Stage 2 | review flag | human confirm or ignore |
| unresolved attribute (type/color/finish/quality) | Stage 3 | review or reject | add synonym, re-run |
| variation in review band | Stage 4 | review | human confirm, write-back alias |
| variation below floor (probable missing variant) | Stage 4 | tree-gap queue | add variant to backend + reference, re-run |
| chosen attribute not allowed for variety | Stage 5 | snap+flag, or tree-gap (missing_leaf_child) | add finish/colour to the variety in the tree, re-run |
| null variation | Stage 5 | tree-gap queue (manual) | add variant to tree + reference, or confirm as alias, re-run |
| site format change (columns moved/renamed/lost fill) | Stage 0 | health DEGRADED/FAILED + drift diagnosis | adapter repair loop 8.5: fix field map, pass fixture, bump version, re-run |
| ambiguous or empty format | Stage 6 | inferred+flag | confirm, optional override |
| dimension parse fail or out of range | Stage 6 | review, value null | add unit synonym or override |
| bundle size underivable | Stage 6 | default+flag, or review | supply slab area or override |
| origin unresolved | Stage 6 | none + review flag, default ports flagged | add to origin_map, re-run |
| image download failed | Stage 7 | flag, slot skipped | re-run or override URL |
| image is placeholder | Stage 7 | treated as no image, flag | none, or supply real image |
| no usable image | Stage 7/9 | hold or reject per switch | supply image, re-run |
| handle or slug collision | Stage 9 | reject | fix namespacing (system bug, not data) |
| stale backend id | startup / Stage 9 | abort or reject | refresh ids after re-seed |

### 8.2 The tree-gap queue (the "does not fit, add manually" requirement)

When scraped data cannot be shoehorned into the tree, the row does not silently fail and does not
guess. It produces a tree-gap record with everything a human needs to extend the backend tree:

`tree_gaps` columns: `src_site, surrogate_key, raw_name, normalized_name, suggested_type,
suggested_color, suggested_finish, suggested_quality, gap_kind, nearest_existing, nearest_score,
example_src_url`.

`gap_kind` is one of: `missing_variation` (no variety matched, probably a new variant to add),
`missing_leaf_child` (variation exists but lacks this finish or colour), `missing_attribute`
(a vocabulary value with no id). Gaps are de-duplicated across the batch so the human sees one
row per missing tree entity, not one per product.

### 8.3 The manual-update loop (idempotent re-ingest)

1. A run emits `tree_gaps` and `review`.
2. The human adds the missing variant, leaf child, or attribute to the backend tree and to the
   reference data (or approves the suggested addition through a small helper that writes both).
3. The reference snapshot version bumps.
4. The same scrape is re-run. Previously gapped rows now resolve through the updated reference and
   alias data and emit. Idempotency guarantees untouched rows produce identical output, so a
   re-run is safe and only the newly resolvable rows change state.

### 8.4 Overrides and write-back (closing the loop, both directions)

- **Inbound overrides**: `state/manual_overrides.csv`, keyed by `(src_site, surrogate_key)`,
  carries human-set field values. The override Strategy is the top of every Resolver, so a manual
  value always wins and is the clean way to inject any missing element (a bundle size, an origin,
  a colour) without editing the scrape. Overrides are versioned and logged.
- **Outbound write-back**: a confirmed fuzzy/splink/review variation match appends the scraped
  spelling to that variation's alias list; a resolved origin review writes into `origin_map.csv`;
  an approved attribute synonym writes into the synonym dict. This is what makes the system more
  consistent every run instead of re-reviewing the same names forever. Write-backs are append-only
  and recorded in the manifest.

### 8.5 Adapter repair loop (when a scraped site changes format)

This will happen for sure, so it is a first-class workflow, not an incident.

1. A run hits Stage 0 and the health gate returns DEGRADED or FAILED with a **drift diagnosis**
   (section 7) naming the columns that moved, were renamed, or lost fill, with example rows.
2. The fix is localized to one adapter and its declarative field map; no stage and no other
   adapter is touched, because the canonical schema downstream is unchanged.
3. Update the adapter's golden fixture input to a fresh sample of the new format and adjust the
   expected output, then run the adapter self-test (7A.2) until it passes. Bump the adapter
   version.
4. Update `source_contracts.yaml` and reset that source's baseline (the next OK run re-learns it).
5. Re-run. Idempotency means only the affected source changes; every other source is untouched.

Because the diagnosis localizes the change and the fixture verifies the fix, a format change is a
minutes-scale, low-risk edit, not a pipeline-wide breakage. The health gate also means a silently
broken scrape never emits wrong data in the meantime: it aborts that source and leaves the last
good import in place.

---

## 9. Flags, confidence, and emit policy

### 9.1 Review flag structure

A review flag is structured, not a free string: `{field, code, raw_value, best_guess,
confidence, method, src_url}`. Codes are a closed enum (e.g. `attr_unresolved`,
`variation_review`, `leaf_snapped`, `bundle_default`, `origin_unresolved`, `image_placeholder`,
`near_duplicate`, `format_inferred`, `dimension_out_of_range`, `multi_value`). Closed codes make
the review CSV filterable and the system measurable (how many of each per run, trending down as
reference data improves).

### 9.2 Emit policy

`emit_on_review` switch: when true, rows with soft flags but no hard reject emit into the import
CSV and also appear in review (so they are visible but not blocking); when false, they are held
in review only and excluded from the import CSV. Hard rejects never emit regardless. Default
true for slabs bootstrap, configurable per source.

---

## 10. Worked field derivations

### 10.1 Bundle size resolver (the exemplar)

Slabs are sold in bundles, but the bundle size is frequently not stated. The resolver tries, in
order, and stops at the first result meeting `derived_accept`:

1. `override` (manual_overrides) -> high.
2. `explicit_bundle_size`: `raw_bundle_size` present and a positive integer -> high.
3. `explicit_slab_count`: `raw_slab_count` present -> high.
4. `slabs_array_length`: `raw_slabs_array` present -> length -> high.
5. `area_division`: `raw_total_m2` and a per-slab area (from `raw_per_slab_m2`, or computed from
   parsed slab length times width) both present -> `round(total / per_slab)` -> medium, only if
   the quotient is within a sane band (e.g. 1 to 60) and close to an integer; otherwise drop to
   the next strategy and flag `bundle_ratio_noninteger`.
6. `standard_slab_area`: `raw_total_m2` present, per-slab area absent -> divide by the per-type
   or per-source standard slab area from `standard_slab_area.csv` -> low, flag `bundle_estimated`.
7. `config_default`: configured default bundle size -> low, flag `bundle_default`.

Blocks set `sold_in_bundle = FALSE` and `bundle_size` empty; the resolver short-circuits on
`is_block`. Anything that resolves only at low confidence still emits if `emit_on_review` is true,
but always carries the flag, so a guessed bundle size is never invisible.

### 10.2 Dimension and weight rules

After unit parsing (Stage 6), apply the branch rules: blocks weight 18 to 23, all dims 1.5 to 3.0;
slabs weight 0.225 to 0.350, width fixed 0.2, length and height 1.5 to 3.0. Where the scrape
provides real parsed dimensions (polonine gives length and height in cm plus a thickness for
width; marenostone gives none), prefer them over the synthetic ranges and flag nothing; the ranges
are a fallback Strategy, not the default. Any synthetic value is seeded from the surrogate key so
it is stable across runs.

### 10.3 Title generation

Title is constructed, not the raw scrape name (a real working upload shows "Walnut Travertine
Honed Slab", "Breccia Capraia Split Face Slab"). Resolver order:

1. `override` -> high.
2. `construct`: `"{variety_name} {finish_name} {format_word}"` where `format_word` is Slab, Block,
   or Tile from `format_value`, trimming empty parts -> high. Finish uses the resolved finish name;
   if finish is null, omit it.
3. `raw_name` fallback when no variety resolved (these rows are gapped anyway) -> low.

Titles are title-cased and whitespace-collapsed. The same inputs always produce the same title
(determinism).

### 10.4 Description generation

Description is templated when the scrape has none (polonine has no description column; marenostone
is empty). Resolver order:

1. `override` -> high.
2. `raw_description` when present and non-trivial -> high (passthrough).
3. `template` -> medium. A worked template matching the real upload style:

   `"{variety} is a {color} {type} {origin_clause}. Supplied as a {finish} {format}, it presents
   {surface_phrase}."`

   where `origin_clause` is "extracted in {city}, {country}" when origin resolved, else "natural
   stone"; `surface_phrase` is a short per-finish phrase from a small finish-to-phrase table
   (e.g. honed -> "a smooth matte surface", polished -> "a bright reflective surface"). Missing
   parts are dropped cleanly, never left as empty placeholders. Because the template reads origin
   and finish, it runs after those resolve, and it carries `description_generated` provenance.

A description that can only reach the template tier still emits; it is never blocked, but its
provenance records that it was generated, not scraped.

---

## 11. Determinism and idempotency rules

1. No `uuid` or unseeded random anywhere in keys or values. Image keys are content hashes.
   Synthetic fills use `seed = hash(surrogate_key + field_name)`.
2. `row_fingerprint = hash(ordered inputs that determine the output)`. The emitter can compare
   against the previous run to assert no unexpected drift.
3. Reference data versions and config hash are pinned in the manifest. A run is reproducible only
   against the same versions; changing reference data is an explicit, recorded event.
4. Re-runs upsert, never duplicate. Handles and slugs are stable functions of the surrogate key,
   so the API client (later milestone) upserts on the external id.

---

## 12. Repository layout

```
stone_pipeline/
  config/
    settings.py            global config block, no argparse
    sources.yaml           per source: adapter, company_id, sales_channel, ports_default
    source_contracts.yaml  per source: required columns, fill floors, patterns, row baseline
  reference/
    attributes.csv  variants_slabs.csv  variants_blocks.csv  backbone.json  origin_map.csv  ports.csv
    units.csv  placeholder_hashes.csv  standard_slab_area.csv  synonyms/
  state/
    scrape_baselines.json  manual_overrides.csv
  adapters/
    base.py                canonical schema + AdapterBase + shared parsing
    develi.py marenostone.py tureks.py temmer.py ferraz.py varsha.py
    fixtures/<source>/      committed input sample + expected canonical output, per source
    selftest.py            replays fixtures, asserts canonical output (onboarding + repair gate)
  resolvers/
    base.py                FieldResolver + Strategy
    attribute.py variation.py category.py origin.py bundle.py dimension.py ports.py images_slot.py
  stages/
    health.py ingest.py keys_dedupe.py normalize.py match_variation.py reconcile_tree.py
    derive.py images.py constants.py validate.py emit.py
  matching/
    index.py               alias-aware candidate index with all projections (5A.1)
    projections.py         norm, compact, tokenset, deprefixed, phonetic
    engine.py              tiered resolver shared by variation, attribute, origin (5A)
    fuzzy.py phonetic.py overlap.py splink_model.py semantic.py   per-tier methods
  io/
    staging.py             canonical parquet read/write
    s3.py  download.py
  run.py                   orchestrator, config block at top, runs stages in order
  outputs/
    medusa_import_<source>_<ts>.csv
    review_<source>_<ts>.csv  rejects_<source>_<ts>.csv
    tree_gaps_<source>_<ts>.csv  scrape_health_<source>_<ts>.json  run_manifest_<ts>.json
```

---

## 13. Tech stack

Python 3.11+. polars for table work (preferred for the larger scrapes; pandas acceptable in
glue). rapidfuzz for fuzzy; a double-metaphone library (jellyfish or abydos) for phonetic
matching; splink for the probabilistic residual only; sentence-transformers optional and
config-gated for the semantic suggestion tier (5A.2 step 8). boto3 for S3, httpx for downloads.
parquet for the canonical table between stages. pydantic (or dataclasses) for the canonical row
and the Resolution and flag structures, so the schema is enforced in code.

---

## 13A. Production and scale (build requirements, not polish)

The system must run reliably on large scrapes (tens of thousands of rows per source, many sources,
repeated runs) and as a scheduled production job. These are requirements, designed in from M0.

### 13A.1 Throughput and memory

- Use polars and process per source as a streaming or chunked job; never hold an entire multi-source
  corpus in memory. The canonical parquet between stages is the checkpoint and the unit of work.
- Vectorize the deterministic stages (normalize exact lookups, key minting, dedup) over polars
  columns. The matching engine's tiers 1 to 3 are dictionary lookups and vectorize; only the
  fuzzy and splink residual is per-row, and it runs on the small unresolved subset, blocked, so it
  stays bounded.
- Build the alias index and attribute maps once per run and share them across rows; never rebuild
  per row.

### 13A.2 Concurrency and external calls

- Image download and S3 upload are the slow path. Run them with bounded concurrency (async httpx or
  a thread pool), per-host rate limiting, timeouts, and retry with backoff on transient errors.
  Content-hash dedup (Stage 7) means an image is fetched and uploaded once even across re-runs.
- Treat every network call as fallible and isolated: a failed image is a flag on one row, never a
  crashed run.

### 13A.3 Failure isolation

- Row-level isolation: a malformed row is caught, recorded to rejects with the exception, and the
  batch continues. One bad row never aborts a source.
- Stage-level checkpoints: each stage reads and writes parquet, so a failure resumes from the last
  good checkpoint rather than from the top.
- Source-level isolation: one source failing (health FAILED or an unhandled error) does not affect
  other sources in a multi-source run.

### 13A.4 Observability

- Structured logging (JSON) with the source, stage, surrogate key, and run id on every line.
- Per-stage metrics in the manifest and emitted to logs: rows in and out, counts per review code,
  counts per gap kind, match-method distribution, image fetch successes and failures, timings.
  These are the health signals; a spike in gaps or a collapse in tier-1 matches is the alarm that
  a site changed or the reference data drifted.
- A one-screen run summary at the end (rows emitted, reviewed, rejected, gapped, per source).

### 13A.5 Incremental runs

- Support re-running a source idempotently (the default) and, as an optimization, processing only
  rows whose `row_fingerprint` changed since the last run, by diffing against the previous
  canonical parquet. Unchanged rows reuse prior output, including already-uploaded images.

### 13A.6 Secrets and environments

- S3 credentials and any backend tokens come from the environment or a named profile, never the
  repo. `config/settings.py` carries an `environment` label; the backend id fingerprint check
  (section 3.2) prevents running staging reference data against production ids or vice versa.

### 13A.7 Scheduling

- `run.py` is callable as a scheduled job per source (cron or a workflow runner). It is config
  driven, not interactive, and exits non-zero on a FAILED health gate or an unhandled error so the
  scheduler can alert.

---

## 14. Build order (each milestone independently testable)

Prove the spine on one clean source before scaling out. Discuss approach before each stage,
present files after each change. Each milestone has a Definition of Done (DoD): it is complete only
when its tests pass and its output is verifiable. Do not start the next milestone until the current
DoD is met. The overarching DoD for the whole build is that develi and polonine run front to back
and their emitted CSV validates against the real upload shape and the three reference maps.

- M0: repo skeleton, config block, canonical schema and Resolution/flag types in code (pydantic),
  parquet read and write, manifest writer, structured logging. DoD: a no-op run writes an empty
  manifest and a parquet round-trips.
- M1: reference loaders (attributes, variants_slabs, variants_blocks, backbone, ports, units),
  origin map stub, contract and baseline loaders, and the live backend id fingerprint check.
  DoD: every reference file loads into typed structures; the 6A trace reproduces (an id looked up
  by name, a leaf checked by membership).
- M2: Stage 0 health gate with a synthetic broken file to prove FAILED and DEGRADED paths and the
  drift diagnosis. DoD: a healthy file passes and updates the baseline; a column-dropped file
  FAILS with a localized diagnosis.
- M3: AdapterBase and the adapter framework (declarative field map, fixture self-test, contract
  generation, variety_match_key declaration), then the develi adapter end to end into canonical
  rows with a passing fixture. DoD: develi fixture self-test passes.
- M4: Stage 2 keys and dedup with surrogate minting; Stage 3 normalize with synonyms and the shared
  matching engine (projections plus fuzzy, no splink), review flags. DoD: known synonyms resolve,
  unknowns flag, no guess reaches output.
- M5: FieldResolver base; the variation matching engine 5A tiers 1 to 6 (exact, projection-exact,
  phonetic, fuzzy, overlap) category-scoped with alias write-back; then Stage 5 reconciliation by
  membership with variety-authoritative type, and the tree-gap queue including null-variation-to-
  manual. DoD: a curated set of near-miss names resolves to the right variation, false matches are
  rejected, and a deliberately unknown variety lands in the gap queue.
- M6: Stage 6 derivation: category, units and dimensions, origin, ports, the full bundle-size
  ladder, then title, description, handle generation (sections 10.3, 10.4). DoD: a row with
  slab_count derives bundle size at high confidence; a row without it defaults with a flag; a
  generated title and description match the real upload style.
- M7: Stage 7 images: bounded-concurrency download, hash dedup, placeholder block, content-addressed
  keys, branch slotting. DoD: re-running the stage re-uploads nothing; blocks slot to oriented
  columns, slabs to product image columns.
- M8: Stage 9 validation and reject file; Stage 10 emit, template-driven from the live template,
  handle namespacing, all artifacts. Run develi and polonine front to back and load the result.
  DoD: emitted CSV validates against the real upload (all ids resolve, every row a valid-by-
  membership leaf, no missing required field).
- M9: overrides and write-back (8.4), then the manual-update loop proven by adding one gapped
  variant and re-running. DoD: a previously gapped row emits after the variant is added, untouched
  rows are byte-identical.
- M10: add splink (tier 7) for the variation residual with a calibration guard, and the optional
  config-gated semantic suggestion tier (8) feeding review only.
- M11: remaining adapters (marenostone, tureks, temmer, ferraz, varsha), each reusing the spine
  unchanged. DoD: each adapter's fixture passes; marenostone runs and routes its generic-descriptor
  rows to the gap queue rather than guessing.
- M12: production hardening (13A): incremental runs, concurrency tuning, metrics and run summary,
  scheduling. Then the orchestrator per-source wiring; later, swap CSV emit for the direct Medusa
  API client with upsert.

---

## 14A. Testing and acceptance

Tests are part of each milestone's DoD, not a later phase.

- **Unit tests per Strategy and Resolver**: each matching tier and each field resolver has tests
  with positive and negative cases (the negatives matter most: token_set false positives like
  Crystal Frost to Crystal must be rejected; a generic descriptor must yield null variation).
- **Golden fixtures per adapter** (7A.2): committed input and expected canonical output; the
  self-test replays them. This is the regression net for site changes.
- **End-to-end on a real sample**: develi and polonine run front to back; the emitted CSV is
  validated programmatically against the real upload, asserting every colour, finish, quality,
  type id and category resolves via the attribute map, every emitted row is a valid-by-membership
  leaf, Type Name equals the Type Id name, variation ids resolve via the correct per-category
  variants file, and no required column is empty. This is the same trace that validated v3.1.
- **Determinism test**: run a source twice; assert byte-identical CSV, identical image keys, and no
  new S3 uploads on the second run.
- **Idempotent re-ingest test**: introduce one tree gap, resolve it in reference data, re-run, and
  assert only the formerly gapped row changed state.
- **Property checks**: handles and skus globally unique; booleans uppercase; ports match origin
  country; no value below its confidence floor appears in the import CSV (only in review or
  rejects).

Acceptance for production: a scheduled run on a full-size scrape completes within the resource
budget, the run summary and metrics are emitted, the import CSV loads into the backend (with a
stock location linked to the sales channel), and review, reject, and tree-gap artifacts are
populated and actionable.

---

## 15. Open items to supply (before or during build)

- `reference/origin_map.csv` (name or pattern to country, city, county).
- `reference/backbone.json` (the full tree) and `reference/variants_blocks.csv` (the blocks variation id map); the slabs variants and attribute map are already provided.
- `reference/standard_slab_area.csv` (per type or source) for last-resort bundle derivation.
- `reference/units.csv` and the initial `placeholder_hashes.csv`.
- `config/source_contracts.yaml` baselines per source (or let M2 learn them from the first OK run).
- S3 staging bucket, region, key prefix, credentials profile, public base URL.
- Per-source Medusa company id, sales channel id, default ports.
- Thresholds: the section 3.4 values if the defaults are not wanted.
- Decisions to confirm: Tiles route into Slabs with format retained (assumed yes); `emit_on_review`
  default per source. Decided in v3: a null or unmatched variation always goes to the tree-gap
  queue for manual assistance (no generic catch-all).
