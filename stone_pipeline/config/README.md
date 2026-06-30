# Scraper config store and admin API

The durable control plane for the scrapers: which scrapers exist, which are enabled
(run), and their per-scraper settings. It is a small SQLite database (`config.db`),
edited live by an admin UI and read by the pipeline. This is CONFIG, kept separate
from the per-env sync ledger (which is regenerated state).

No em dashes in this document (repo convention).

## Where it lives

- `config.db` at the repo root (override with `BLOKPORT_CONFIG_DB`). SQLite in WAL mode:
  many readers (the pipeline) plus one writer (the admin UI), no contention, no Postgres.
- `sources.yaml` is the committed **seed**. The DB is the source of truth once seeded.
- The settings are **agnostic** (names/refs, no Medusa ids), so ONE config DB serves both
  dev and prod.

## CLI (ops)

```
python -m stone_pipeline.config.store seed    # create/refresh from sources.yaml (never clobbers an edit)
python -m stone_pipeline.config.store list    # show every scraper + on/off + vendor/code/mode
```

`seed` is insert-or-ignore: it only adds scrapers that are not already in the DB, so a
re-seed after the UI has edited rows is safe.

## How the pipeline uses it

- `load_sources()` / `load_source(name)` read the DB when `config.db` exists, else the YAML.
- `run all` runs **only enabled** scrapers. Set a scraper `enabled = false` and it stops
  running, no code change or deploy.

## Admin API (for the UI, e.g. on :4200)

Run it:

```
BLOKPORT_CONFIG_TOKEN=<secret> python -m stone_pipeline.config.server   # listens on 127.0.0.1:8724
```

Every request needs `Authorization: Bearer <BLOKPORT_CONFIG_TOKEN>`. JSON in, JSON out.

| Method | Path | Purpose | Body / response |
|---|---|---|---|
| `GET` | `/config/v1/sources` | list every scraper | `{ "sources": [ <source>, ... ] }` |
| `GET` | `/config/v1/sources/<name>` | one scraper | `<source>` or `404` |
| `PUT` | `/config/v1/sources/<name>` | create or update one | body: `<source>` -> the saved `<source>` |

`PUT` replaces the row, so send the full object (a missing field falls to its default).
To add a new scraper, `PUT` a name that does not exist yet. To disable one, `PUT` it with
`"enabled": false` (or include it in any edit).

### The `<source>` object

```json
{
  "source": "polonine",            // the scraper name (the URL <name>)
  "enabled": true,                 // does it run
  "schedule": null,                // optional: how often / when (free text)
  "adapter": "polonine",           // which adapter parses it
  "source_code": "pol",            // SKU prefix and delist scope; keep unique
  "vendor": "Polonine Stone Co",   // the COMPANY this scraper's products belong to (agnostic)
  "origin_default": "IT",          // supplier ISO-2 country (origin fallback)
  "ports": ["Brindisi"],           // origin ports (names or UN/LOCODEs)
  "mode": "review",                // "review" (quarantine) or "auto" (load live)
  "watermarked": false,            // source burns a watermark into its photos
  "emit_on_review": true,
  "default_bundle_size": 6,
  "min_expected_rows": 75          // catastrophic-scrape floor (abort below this)
}
```

`vendor` is the agnostic company reference (no Medusa id); the Medusa sync resolves it to
the marketplace company. `source_code` must stay unique across scrapers (it scopes SKUs and
delisting).

## Example

```
# list
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8724/config/v1/sources

# disable a scraper
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"enabled": false, "adapter": "zucchi", "source_code": "zuc", "vendor": "Zucchi"}' \
  http://127.0.0.1:8724/config/v1/sources/zucchi

# add a new scraper
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"adapter": "newco", "source_code": "new", "vendor": "New Stone Co", "origin_default": "TR"}' \
  http://127.0.0.1:8724/config/v1/sources/newco
```
