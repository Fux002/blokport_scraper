# Market price observations: delivery contract (version 1)

Reference prices seen on competitor sites, for the pricing module. This path never writes to Medusa, the sync ledger or the vendor product path. It records what a site says. Matching, conversion, grading and trust rules belong to the consumer.

## Where

```
s3://<bucket>/<env>/market_prices/<brand>/<run_id>/observations.csv
s3://<bucket>/<env>/market_prices/<brand>/<run_id>/site_status.csv
s3://<bucket>/<env>/market_prices/<brand>/<run_id>/manifest.json
s3://<bucket>/<env>/market_prices/<brand>/latest.json
```

- `run_id` is the run's UTC start, `YYYYMMDDTHHMMSSZ`. Run folders are kept, so they are the price history.
- `manifest.json` has the sha256, row count and S3 version of each file and is written after them. A folder without a manifest is not a delivered run.
- `latest.json` names the newest run with at least one observation (`run_id`, `manifest_key`, `rows`, `contract_version`). A run with no rows is delivered as evidence but does not move it.
- The newest run may hold only some sites: `site_status.csv` says which. For a site missing from it, the consumer looks back through the run folders, and decides how old a price may be.

## observations.csv

UTF-8, comma-separated, `\n` line ends, header row. Empty cell means the site did not say. Sorted by `site_id`, then `listing_key`. Same input gives the same bytes.

| Column | Meaning |
|---|---|
| contract_version | This contract's version |
| brand, site_id | Whose register, which site |
| seller_name, seller_type | From the register. `merchant`, `wholesaler`, `marketplace` or `unknown` |
| seller_country, served_market | From the register, ISO 3166 alpha-2. A site selling into several markets is one register entry per market |
| listing_key | The site's own id for the listing, stable between runs |
| source_url | The listing's page, on the site's own host or a subdomain of it |
| listing_text | The seller's name for the listing, and short description when there is one |
| stated_specs | JSON object: the seller's label to its value, as written |
| grade_text | The seller's grade words, as written, when the register names the label that holds them |
| price_text | The price as the seller shows it |
| price_amount | Exact decimal, only when the listing has one price |
| currency | ISO 4217 code as stated |
| price_status | `priced`, `on_request` or `none`. A range, or zero (made-to-measure), is `none` with `price_text` kept |
| vat_basis | `incl`, `excl` or `unknown` |
| unit_text | The selling unit as written |
| delivery_text, delivery_included | As written; `yes`, `no` or `unknown` |
| min_qty_text | The minimum order as stated |
| availability_text | The stock wording as written |
| provenance | JSON object for vat_basis, unit_text, delivery_text, delivery_included, grade_text, price_amount, currency and min_qty_text: `listing`, `site_statement` (said once for the whole site) or `unknown` |
| observed_at, run_id | When this run started, UTC |
| collector_version | Which collector read it |

No photos are collected. Text cells are the seller's own words: read them as data, and do not open the raw file in a spreadsheet that runs formulas.

## site_status.csv

One row per site in the register, every run.

| status | Meaning |
|---|---|
| ok | Every page the site advertised was read, and every listing on them |
| partial | Some rows delivered: a page failed or listings were malformed. `detail` says why |
| unavailable_blocked | The site refused this client (401, 403, 406, 429, 451, a challenge page). 0 rows, and collection of that site stopped at once. Never worked around |
| unavailable_error | No usable answer. 0 rows |
| disabled_terms | No approved decision, or robots.txt forbids the collection or asks for a slower pace than we can run. 0 rows |

A listing that cannot be read or delivered (for example a cell over 100,000 characters) is counted in `malformed_listings` and left out.

Also: `rows`, `malformed_listings`, `duplicate_listings`, `detail`, `terms_checked_on`, `decision`, `observed_at`, `run_id`, `contract_version`, `brand`, `site_id`.

## Adding a site

One entry in `market_prices/sites/<brand>.yaml`. `decision: approved` is refused without terms URL, date checked, a summary of the terms, who decided and when, the seller's country and the market served. robots.txt is read from the site's root on every run.
