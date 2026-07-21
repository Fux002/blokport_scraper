# Adding a new source (scraper → adapter)

The pipeline ingests any source that lands a `data/<source>/<timestamp>/products.csv`
and has an **adapter** mapping that CSV to the canonical schema. Adapters are
**auto-discovered** — drop the file in and it registers itself.

> **Before a source is allowed to produce, it MUST clear every gate in
> [`NEW_SOURCE_CHECKLIST.md`](../../NEW_SOURCE_CHECKLIST.md).** The steps below are the mechanical build;
> the checklist is the full admission contract (declare conventions → fixture → certify → source contract →
> health/ingest → clean/magnitude/process/validate → invariants → admission ladder → human live-verify). A
> source that fails any gate stays in `mode: review` (or out) until fixed. Follow the checklist end to end.

## Steps

**0. Declare what the source offers** (source-isolation invariant): every source-specific fact is a
declaration on the scraper/adapter/config, never an inline guess or an order-dependent heuristic. Beyond
`source`/`columns`/`format`, declare the source's conventions -- e.g. `dimension_unit` if it parses dimensions
(see `marenostone.py`), `proxy_capability` if it needs a proxy, its acquisition type. If the source's rule is
ambiguous (a unit a label omits, which category level is the material), DECLARE the rule from a live sample;
do not decide it at extraction time. See NEW_SOURCE_CHECKLIST.md section 0.

**Dimensions capture only; fallbacks are shared.** An adapter's job is to hand `derive` the raw values it can
find (`raw_dimensions`, `raw_thickness`); it must NOT invent or default a size. Build `raw_dimensions` with
the ONE shared helper `AdapterBase.build_dims(length, height, unit=...)` (never hand-roll `length=..;height=..`);
`unit="m"` if the source's values carry no unit, `unit=""` if they already do (declare the source's unit via the
scraper's `dimension_unit`); `AdapterBase.na(...)` blanks an `N/A` sentinel. Thickness (the depth) rides in
`raw_thickness`. Missing / unparseable / ambiguous dimensions (a `MULTI` thickness, a `Free` cut-to-size length,
an `A to B` range) are resolved once, for every source, by the shared `stages/derive.derive_dimensions` from the
`dimension_defaults` toolbox in the domain pack (`config/domains/<pack>.yaml`), always provenance-flagged
(`FlagCode.dimension_defaulted`). Tune a size or add a category THERE, never in an adapter.

**A FETCH failure holds the row; it is never defaulted.** A genuine source absence defaults (above); a value
missing because its SUB-FETCH failed (e.g. a rate-limited detail page) is recoverable, so it must be HELD for
retry, not shipped with a fabricated size. The scraper marks it -- `self.mark_fetch_failed(row, "dims", ...)` --
which the base carries in the reserved `fetch_failed` column; `AdapterBase` auto-maps it to
`CanonicalRow.fetch_failed_fields` (zero per-adapter work), `derive` leaves the dim `None` + flags
`dimension_unavailable`, and `validate` holds the row (a fresh scrape retries it). See NEW_SOURCE_CHECKLIST.md
section 6.

1. **Scraper** — `scrapers/<source>.py`: copy `scrapers/_template.py`, subclass `ScraperBase`,
   set `source`/`columns`/`id_field` and a per-product `format` (`slab`/`block`/`tile`),
   implement `list_products()` + `parse_product()` (return an `image_urls` list). Register it
   in `scrapers/run.py` `REGISTRY`. Run `python -m scrapers.<source>` → writes the CSV.

2. **Adapter** — `stone_pipeline/adapters/<source>.py`: copy `_template.py`, set `source`,
   `adapter_version`, `variety_match_key`, `required_columns`, `field_map`. End the file with
   `ADAPTER = <Source>Adapter()`. **That's the only registration** — it is auto-discovered into
   `adapters.REGISTRY`; no list to edit.
   - Map the variety from the scraper's name column; the base **strips supplier-code junk**
     automatically (lone-letter / alphanumeric leading codes). For an exotic prefix (e.g. varsha's
     `Z`/`ZB`) add one regex to `code_prefixes = (...)` — see `varsha.py`.
   - Set `format_field = "format"` to wire the scraper's format tag (no `raw_format` boilerplate
     needed). Use `generic_descriptor = True` for sources whose name is a colour+type descriptor.

3. **Config** — add a `<source>:` block to `config/sources.yaml` (adapter, source_code,
   company/sales-channel ids, `ports_default`, `origin_default`).

4. **Fixture** — put a small `adapters/fixtures/<source>/input.csv`, then
   `python -c "from stone_pipeline.adapters import selftest as s; s.regenerate_fixture('<source>', s.fixture_dir('<source>')/'input.csv')"`.
   **Open the generated `expected.json` and eyeball it** — the self-test checks stability, not
   correctness, so the first snapshot must be verified by hand.

5. **Run** — `python -m stone_pipeline.run <source>`, then `python -m stone_pipeline.catalog`
   and `python -m stone_pipeline.tree`.

## What's checked for you
- The adapter is auto-registered, and `test_fixture_selftest_passes` is parametrized over
  `REGISTRY`, so the new source is tested automatically once it has a fixture.
- `test_every_adapter_has_a_fixture_and_config` fails if an adapter is missing its fixture or its
  `sources.yaml` entry — catching a half-wired source before release.
- `python -m stone_pipeline.run <source>` raises a clear error if the source has no adapter.
- **Certify's `vocab` check** (`python -m stone_pipeline.certify <source>`) catches a **swapped
  attribute column** — if your `raw_finish` values resolve as colours (etc.), it fails. The
  self-test alone can't catch this (the golden file is made by the same adapter), so this is the
  real correctness gate. It skips an attribute with <5 values or values that resolve nowhere (novel
  varieties never false-trip it).
- **`source_code` must be unique** across sources (`test_source_codes_are_unique_across_sources`) —
  a duplicate/typo'd code would alias another source's SKUs and delist scope.
- **A >50% adapt-time row drop aborts the run** — a mis-mapped `required_canonical` can't silently
  discard most of a batch and "succeed" on the survivors.
- The **format tag is plural-tolerant**: `"Slabs"`/`"Blocks"`/`"Tiles"` resolve the same as the
  singular, so a plural scraper tag still routes correctly instead of defaulting to slab.

> The hand-eyeball of `expected.json` (step 4) still matters, but the `vocab` check now backstops
> the most damaging mistake (a column swap) so it can't reach live data on `mode: auto`.

That's it: **adapter + config + fixture**, no manual registry edits, and the name-cleanup +
matching + tree stages all apply unchanged.

## Non-file data sources (API / DB / partner feed)

A scraper writes `data/<source>/<ts>/products.csv` and the adapter maps it. A source with **no
scrape file** needs one extra method: override **`load_frame`** to fetch its records and return
`(frame, timestamp_token, origin_label)`. The frame must be in the same column shape your
`field_map` reads; everything downstream (adapt → stages → emit → catalog) is identical and never
knows where the rows came from.

Copy `stone_pipeline/adapters/_api_template.py`, fill in `_fetch()` + `field_map`, end with
`ADAPTER = <Source>Adapter()`. No core code changes — the pipeline is source-agnostic.

```python
class ApiSourceAdapter(AdapterBase):
    source = "myapi"
    field_map = { "src_natural_key": lambda r: AdapterBase.clean(r.get("product_id")), ... }
    def load_frame(self, scrape_path=None):
        records = requests.get(API).json()["products"]      # or a DB query
        return pl.DataFrame(records), "20260101_000000", "myapi://live"
ADAPTER = ApiSourceAdapter()
```
