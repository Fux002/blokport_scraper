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

## observations.csv

UTF-8, comma-separated, `\n` line ends, header row. Empty cell means the site did not say. Sorted by `site_id`, then `listing_key`. Same input gives the same bytes.

| Column | Meaning |
|---|---|
| contract_version | This contract's version |
| brand, site_id | Whose register, which site |
| seller_name, seller_type | From the register. `merchant`, `wholesaler`, `marketplace` or `unknown` |
| listing_key | The site's own id for the listing, stable between runs |
| source_url | The listing's page, always on the site's own host |
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
| provenance | JSON object per field: `listing`, `site_statement` (said once for the whole site) or `unknown` |
| observed_at, run_id | When this run started, UTC |
| collector_version | Which collector read it |

No photos are collected. Text cells are the seller's own words: read them as data, and do not open the raw file in a spreadsheet that runs formulas.

## site_status.csv

One row per site in the register, every run.

| status | Meaning |
|---|---|
| ok | All listings read |
| partial | Some rows delivered: `detail` says why |
| unavailable_blocked | The site refused this client (401, 403, 429, a challenge page). 0 rows, and collection of that site stopped at once. Never worked around |
| unavailable_error | No usable answer. 0 rows |
| disabled_terms | No approved decision, or robots.txt forbids it. No page fetched |

Also: `rows`, `malformed_listings`, `duplicate_listings`, `detail`, `terms_checked_on`, `decision`, `observed_at`, `run_id`, `contract_version`, `brand`, `site_id`.

## Adding a site

One entry in `market_prices/sites/<brand>.yaml`. `decision: approved` is refused without terms URL, robots URL, date checked, a summary of the terms, who decided and when.
