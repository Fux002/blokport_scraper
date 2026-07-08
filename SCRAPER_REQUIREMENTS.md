# Scraper Requirements Contract

The single source of truth for what a scraper (or any product data connection) must
produce, what happens when a field is absent, and how to add and certify a new
source. If you are adding a scraper, this is the checklist. No em dashes (repo
convention).

A scraper yields raw rows in its own column shape; an adapter
(`stone_pipeline/adapters/<source>.py`) maps those columns to the canonical schema.
This document is about the canonical fields the adapter must be able to fill, not
the raw column names (those are per source).

---

## 1. Field disposition table (the contract)

Every field Medusa needs has a declared **disposition**. This is what the pipeline
actually does today (deployed), field by field.

| Canonical field | Disposition | If the source does not provide it |
| --- | --- | --- |
| **length / width / height** | **REQUIRED** | Product is **REJECTED**. Sizes are never fabricated: a stone with no real size breaks area/volume pricing and freight. (~99-100% of scraped rows carry dims, so this drops only the genuinely sizeless.) |
| **weight** | **DERIVABLE** | Derived from real dims x per-type density: `weight_kg = length x width x height x density[type]` (`reference/type_density.csv`, kg/m3). A real physical weight, not a synthetic. Unlisted type falls back to Marble; every derived weight is flagged `weight_derived`. |
| **type** | **REQUIRED (resolve)** | Resolved against the closed vocabulary; an explicit type word in the variety name overrides a wrong supplier tag. Unresolvable -> **REJECTED**. |
| **color** | **REQUIRED (resolve)** | Resolved, else inherited from the matched variety (whose color is classified from its texture; `Natural` floor). A brand-new colourless variety with no texture yet can still reject until the variety is coloured. |
| **finish** | **RESOLVE then DEFAULTED** | Resolved; else a configured, flagged last-resort default: block -> `Raw`, slab -> `Polished`, tile -> `Honed` (`settings.LAST_RESORT_FINISH`). Never rejects for a missing finish. |
| **quality** | **RESOLVE then DEFAULTED** | Resolved; else the configured, flagged last-resort default `A` (`settings.LAST_RESORT_QUALITY`). Never rejects for a missing quality. |
| **origin (country/city/county)** | **DEFAULTED** | `origin_map` (variety -> country, exact or geographic pattern, flagged) -> supplier `origin_default` (flagged) -> **REJECTED** only if a source set no `origin_default`. |
| **images (thumbnail / gallery / oriented)** | **HOLD-GATE** | The product is **held** (not shipped) until at least one image and the variety texture exist. Not rejected, held. |
| **bundle size** | **DEFAULTED** | Ladder: explicit count -> slab-array -> area division -> standard area -> source `default_bundle_size` (flagged). Always filled. Blocks are not bundled. |
| **title / description / handle / slug** | **DERIVED** | Constructed from variety + finish + origin. Handle is namespaced `slug-sourcecode-surrogate` (globally unique). |
| **company_id / sales_channel / visibility / discountable / status / ports** | **CONFIG** | From `settings.py` (env) or the per-source config. Always present. |

Dispositions in one line:
- **REQUIRED**: must be scraped or resolvable, else the product is rejected. Never fabricated.
- **DERIVABLE**: may be absent; a documented rule fills it from other required fields.
- **DEFAULTED**: may be absent; a configured, flagged fallback fills it (last resort).
- **HOLD-GATE**: may be absent; the product waits rather than ships incomplete.
- **DERIVED / CONFIG**: never comes from the source.

---

## 2. Per-source status (are the current scrapers correct?)

Verified against live scrape data. "Correct" means the source provides every
REQUIRED field; a missing DERIVABLE/DEFAULTED field is expected and handled.

| Source | Platform | dims | weight | color | finish | quality | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **zucchi** | Salesforce | 100% | 100% scraped | (inherited) | 100% | 100% | correct |
| **polonine** | SlabWare | 99% | none (derived) | 99% | ~95% | 100% | correct (weight derived) |
| **varsha** | SlabWare | 100% | none (derived) | 0% (colourless) | ~97% | 100% | correct (weight derived; colour from variety) |
| **marenostone** | direct | 100% | none (derived) | (inherited) | ~93% | 99% | correct (weight derived) |

Key point on weight: **only zucchi's source carries weight.** SlabWare (polonine,
varsha) is an **area-based** slab system that has no weight field, and marenostone's
source returns weight empty. This is not a scraper bug: there is nothing to pull, so
weight is **derived** from the (required, real) dimensions and the per-type density.
The scrapers are therefore correct under this contract.

One item to confirm on a live run: that SlabWare truly exposes no weight field (this
was inferred from the fields the scraper reads, not a full raw API dump). If a
SlabWare source does expose weight, add it to the adapter (a scraped weight always
wins over a derived one).

The finish/quality blanks (a few products per source) no longer drop: an
unresolvable finish or quality now gets a configured, flagged last-resort default
(`settings.LAST_RESORT_FINISH` / `LAST_RESORT_QUALITY`), so the product ships and is
queued for correction rather than being rejected.

---

## 3. How a scraper must behave

- **Yield raw rows in a stable column shape.** A web scraper subclasses `ScraperBase`
  (copy `scrapers/_template.py`); a non-scraper connection (partner API, CSV/Excel
  drop, push feed) just yields rows in its own columns.
- **Map to canonical in an adapter** (`stone_pipeline/adapters/<source>.py`, copy
  `_standalone_template.py`). The adapter fills `raw_*` fields; the pipeline resolves
  and derives the rest.
- **Provide the REQUIRED fields** (dimensions, and resolvable type/color/finish/
  quality). Weight, origin, bundle, and images are DERIVABLE/DEFAULTED/HOLD, so a
  source that lacks them is still valid.
- **Source isolation.** All per-source quirks live in the fetcher and the adapter.
  The shared stages (clean, match, derive, emit) never branch on which source a row
  came from, so one source can never change how another is handled.
- **Cloudflare-fronted sources** (the SlabWare tenants) route their session through
  the residential proxy (`BLOKPORT_SCRAPER_PROXY`); the clean sources do not.

---

## 4. How to add a source

1. **Connect**: a `ScraperBase` scraper, or any connection that yields raw rows.
2. **Map**: an adapter mapping raw columns to canonical, plus a golden fixture
   (`stone_pipeline/adapters/fixtures/<source>/{input.csv,expected.json}`).
3. **Configure**: a `sources.yaml` entry. Set `source_code`, `vendor`, `company_id`
   (or leave blank for the env default), and crucially `origin_default` (the
   supplier country, so origin never rejects). Starts at `mode: review` (quarantined).
4. **Certify**: `python -m stone_pipeline.certify <source>` until green (config,
   adapter, golden-fixture selftest, contract). CI runs `certify all` on every push,
   so a regression in any source fails the build.
5. **Promote**: once it runs clean and you have signed off, set `mode: auto`.

A `review` source stages its output for human sign-off; only `auto` sources load
automatically. New or unproven sources are quarantined by default and can never
silently push bad data live.

---

## 5. How "correct" is enforced (not just documented)

- **certify** (`stone_pipeline.certify`): re-runs each source's adapter against its
  saved golden fixture. CI runs it on every push, so a change that breaks a source's
  mapping is caught before it runs for real.
- **validate** (`stages/validate.py`): hard-rejects a product missing a REQUIRED id
  (type/color/finish/quality/variation) or a REQUIRED dimension. These never reach
  the import.
- **the boundary gates** (`gates/`): the ingest / clean / process contracts check
  field presence and canonical casing at each stage boundary and escalate a systemic
  failure (a whole batch failing one rule) rather than dropping rows silently.

---

## 6. Open items

- **Confirm SlabWare has no weight** against a full raw API response (see section 2).
  Inferred from the fields the scraper reads; if a SlabWare source does expose weight,
  add it to the adapter (a scraped weight always wins over a derived one).

Everything else in this contract is built and live: dimensions required, weight
derived from dims x per-type density, finish/quality last-resort defaults, origin
supplier fallback, images held. Colour is inherited from the variety (texture
classification, `Natural` floor), so it needs no separate default.
