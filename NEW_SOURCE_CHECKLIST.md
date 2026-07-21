# Admission checklist: what a new scraper / data feed must pass before it is allowed in

Every source (a web scraper OR any other data feed) must clear the gates below before it can produce into the
pipeline. This is the contract that keeps the rest of the pipe source-agnostic and bug-free: a source declares
what it offers, proves it against real data, and earns trust in stages. Skipping a gate is how a bug reaches
the catalog. Each item names the real gate/file that enforces it.

## Status note -- tureks is NOT ready

`tureks` is not set up and tested yet. Do NOT scrape or wire it into the pipeline until it clears THIS
checklist. The known open issue (category picked by list order in `parse_categories`) is one of the things the
checklist exists to catch: it must be a DECLARED category rule for the source, proven against a live tureks
response, not an order heuristic. So: build + certify tureks per below FIRST, then (and only then) run it.

## 0. Declare what the source offers (source-isolation invariant)

All source-specific facts are declared at setup, so extraction never guesses and no `if source ==` leaks into
a shared stage. Lock these in on the scraper/adapter class + config:

- **Scraper** (`scrapers/<source>.py`, `ScraperBase`): `source`, `id_field`, `columns`, and either a constant
  `category` OR the per-product format via the adapter's `format_field`. Declare source conventions:
  `dimension_unit` if it parses dimensions (see marenostone), `proxy_capability` if it needs a proxy (Phase 5),
  `acquisition` (`scraper` | `load_frame` | `file_drop`), `use_curl_cffi`.
- **Adapter** (`stone_pipeline/adapters/<source>.py`, `AdapterBase`): `variety_match_key`, `field_map`,
  `required_columns`, `code_prefixes`, `adapter_version`. A non-scrape feed overrides `load_frame`.
- **Config** (`config/sources.yaml`): `source_code` (unique -- SKU provenance), `vendor`, `origin_default`,
  `ports`, `min_expected_rows`, `mode: review` (always start in review).
- Rule: NO order-dependent heuristics and NO silent unit/format guesses. Declare the source's rule; if the
  source genuinely varies, declare that too. (This is the D/E lesson.)

## 1. Golden fixture (offline lock)

A captured real sample (`stone_pipeline/adapters/fixtures/<source>/input.csv`) + its expected canonical
output, so the adapter's behavior is pinned. Every declaration above must reproduce the expected output
EXACTLY. This is what `certify`'s selftest checks; regenerate it deliberately when the source changes.

## 2. Certification -- `python -m stone_pipeline.certify <source>` (runs in CI)

All five must pass (`stone_pipeline/certify.py`):
- **config** -- in sources.yaml with a `source_code` + a valid `mode`.
- **adapter** -- an adapter is registered (auto-discovered).
- **selftest** -- the adapter reproduces its golden fixture byte-for-byte.
- **vocab** -- each mapped attribute's values resolve to the RIGHT vocabulary (catches a finish mapped into
  the colour column -- a swap the selftest alone can't see).
- **contract** -- a source contract is defined.

## 3. Source contract (drift baseline) -- `config/source_contracts.yaml`

`required_columns`, `optional_columns`, `fill_floors` (per-field min non-empty fraction), `value_sets`,
`value_patterns`, `row_baseline`, `adapter_version`. This is what health-drift detection compares against, so
a column vanishing or a fill collapse is caught. Generated from a sample, then reviewed.

## 4. First real scrape -- the input gates must pass

Run the source once and confirm it clears, in order:
- **scrape-completeness**: `>= min_expected_rows`; a truncated fetch calls `mark_incomplete()` so it is never
  treated as authoritative (no wrong delist). (`run.py` floor guard; `scrapers/base.py` complete marker.)
- **>50% adapter-drop guard**: a mis-mapped required field can't silently drop most rows and still "succeed".
- **health gate** (`stages/health.py`): structural / volume / fill / pattern / parse checks against the
  contract + the self-tuning baseline. Must be OK (DEGRADED is investigated, FAILED aborts).
- **ingest gate**: required canonical fields present across the batch.

## 5. Processing + output gates (per row)

- **clean gate**: resolved attribute names are the canonical spelling for their id.
- **magnitude-drift gate** (`stages/magnitude_drift.py`): weight/dimension magnitudes are sane per format
  (catches a unit/scale error -- e.g. mm read as cm). This is the backstop for a wrong `dimension_unit`.
- **process gate**: the Medusa import contract (origin country code, etc.).
- **validate** (`stages/validate.py`): required attribute ids, variation id, no unresolved tree gap, active
  category, unique handle/slug, owner ids (company + sales channel), valid dimensions, and the image rule.
  A row missing any of these is rejected, never emitted half-formed.

## 6. Invariants the source must honor

- **Never guess a value into output** -- an unresolved attribute / unknown unit becomes a review flag or a
  reject, not a fabricated value.
- **Dimensions: capture with the shared helper; fallbacks are the shared toolbox, never per-adapter.** Build
  `raw_dimensions` with `AdapterBase.build_dims(length, height, unit=...)` (never hand-roll the string);
  declare the source's unit via the scraper's `dimension_unit`; thickness rides in `raw_thickness`;
  `AdapterBase.na(...)` blanks an `N/A` sentinel. A dimension that is missing, unparseable, or ambiguous (a
  `MULTI` thickness, a `Free`/cut-to-size length) is filled from `dimension_defaults` in the domain pack
  (`config/domains/<pack>.yaml`) by the shared `derive_dimensions`, every fill flagged
  `FlagCode.dimension_defaulted` -- identical across all sources. A face dimension given as a range takes its
  MAX; a thickness range/`MULTI` takes the standard depth. A real parsed `0` is a data error: kept, never
  defaulted, so validate rejects it. Do NOT add dimension defaults in an adapter; add a category to the pack.
- **A fetch FAILURE holds the row; it is never defaulted.** A value missing because its sub-fetch failed
  (e.g. a rate-limited detail page) is recoverable, so the scraper marks it with
  `self.mark_fetch_failed(row, "dims", ...)` (carried in the reserved `fetch_failed` column, auto-mapped to
  `CanonicalRow.fetch_failed_fields`); `derive` leaves it `None` + flags `dimension_unavailable` and
  `validate` HOLDS the row (`rule="dimension_unavailable"`), which retries on the next scrape -- never
  shipping a fabricated size. This is distinct from a genuine source absence (which defaults, above).
- **Capture raw** (`capture_raw = True`) so no source field is ever lost (mine more later without re-scraping).
- **Deterministic + idempotent** -- the same scrape produces byte-identical emit; safe to re-run.
- **Vendor isolation** -- the source only touches products in `scraper_sync_ref` by SKU, never by company_id.
- **>30% delist refusal** -- a single run can never mass-discontinue a source (a partial scrape is refused,
  not acted on).

## 7. Admission ladder (trust earned in stages) -- Phase 2

- The source starts in `mode: review` -- its output is quarantined for human sign-off, never auto-loaded.
- It becomes `eligible` for auto only after `admission.consistent_runs` consecutive CLEAN runs (health OK, no
  drift) AND certification passing. Promotion to `auto` stays an explicit action (`PUT
  /config/v1/sources/<name>` `mode:auto`); a drift auto-DEMOTES it back to review.
- Watch this per source on the :4200 Diagnostics page (`GET /config/v1/diagnostics/<source>`): the layer
  status strip, the drift banner, and the admission badge.

## 8. Live verification before promotion

One real end-to-end run reviewed by a human: dimensions/categories/attributes resolve correctly, the review
queue is sane, magnitude-drift is OK, no mass-delist, and the emitted product identity is right. Only then
consider promoting review -> auto.

---

### One-line gate summary
declare (0) -> fixture (1) -> `certify` green (2) -> contract (3) -> scrape clears health+ingest (4) ->
clean/magnitude/process/validate (5) -> invariants hold (6) -> N clean runs earn `eligible` (7) -> human
live-verify, then promote (8). A source that fails any gate stays out until it is fixed -- that is the point.
