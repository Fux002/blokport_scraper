# Adding a new source (scraper → adapter)

The pipeline ingests any source that lands a `data/<source>/<timestamp>/products.csv`
and has an **adapter** mapping that CSV to the canonical schema. Adapters are
**auto-discovered** — drop the file in and it registers itself.

## Steps

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
