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
  "vendor": "Polonine Stone Co",   // the COMPANY this scraper's products belong to (agnostic name)
  "company_id": "",                // Medusa company id for this source. ENV-SPECIFIC (dev != prod),
                                   // so keep dev ids in dev's config. Empty = Medusa resolves by vendor name.
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

## Frontend (:4200) integration: what to build

A single "Scrapers" admin screen that talks to the config API above. Concretely:

1. **Connect (server-side).** The config API is at the config server (default
   `http://<scraper-host>:8724`, put a real base URL behind your proxy) and needs
   `Authorization: Bearer <BLOKPORT_CONFIG_TOKEN>`. Call it from the `:4200` **backend**,
   never the browser: the browser talks to your `:4200` server, which holds the token and
   proxies to the config API. Do not ship the token to the client.

2. **List view.** `GET /config/v1/sources` -> render a table of scrapers, one row each:
   name, `enabled` (a toggle), `vendor`, `source_code`, `mode`, `origin_default`.
   **Guarantee:** each element of `sources` is the FULL `<source>` object (same shape as
   `GET /sources/<name>`, same serializer), so Edit and toggle can PUT straight from the
   list row without a second fetch. The list is never summarized.

3. **Enable / disable.** The toggle does `PUT /config/v1/sources/<name>` with the full
   object and `enabled` flipped. A disabled scraper stops running on the next `run all`,
   no deploy.

4. **Edit settings.** An edit form per scraper for the `<source>` fields (below). Save =
   `PUT /config/v1/sources/<name>` with the FULL object. `PUT` REPLACES the row, so send
   every field back (prefill from the `GET`, edit, submit the whole thing) or a missing
   field falls to its default.

5. **Add a scraper.** Same `PUT` to a name that does not exist yet. Require `adapter`,
   `source_code` (unique across scrapers), and `vendor` at minimum.

6. **Validation to enforce in the form:** `source_code` unique and short (it is the SKU
   prefix and the delist scope); `mode` in {`review`, `auto`}; `origin_default` an ISO-2
   country; `ports` a list of names or UN/LOCODEs; `vendor` the company name (not an id).

The full `<source>` object schema and the endpoint table are above. That is the entire
contract: three endpoints, one object, bearer auth. Nothing on the pipeline side changes
when you build the UI; it already reads the same store.
