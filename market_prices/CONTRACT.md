# Market price observations: delivery contract (version 2)

Reference prices seen on competitor sites, and read from the competitor price sheets the owner supplies, for the pricing module. This path never writes to Medusa, the sync ledger or the vendor product path. It records what a site or a sheet says. Matching, conversion, grading and trust rules belong to the consumer.

Versions: a consumer refuses a contract_version it does not know. Version 2 adds `source` (`site` or `file`) as the second column of both files, and `seller_is_our_supplier` and `supplier_key` after `served_market` in observations.csv; every other column is as in version 1.

## Where

```
s3://<bucket>/<env>/market_prices/<brand>/<run_id>/observations.csv
s3://<bucket>/<env>/market_prices/<brand>/<run_id>/site_status.csv
s3://<bucket>/<env>/market_prices/<brand>/<run_id>/manifest.json
s3://<bucket>/<env>/market_prices/<brand>/latest.json
```

- `run_id` is the run's UTC start, `YYYYMMDDTHHMMSSZ`. Run folders are kept, so they are the price history.
- `manifest.json` has the run's `brand` and the sha256, row count and S3 version of each file, and is written after them. A folder without a manifest is not a delivered run.
- `latest.json` names the newest run with at least one observation (`brand`, `run_id`, `manifest_key`, `rows`, `contract_version`). A run with no rows is delivered as evidence but does not move it.
- Brands never mix: every row, manifest and pointer carries the run's brand, every key is under `<env>/market_prices/<brand>/`, and a site sits in one brand's register unless both registers declare it shared (`shared_with`). The rules are named in `market_prices/brand.py`.
- The newest run may hold only some sites: `site_status.csv` says which. For a site missing from it, the consumer looks back through the run folders, and decides how old a price may be.

## observations.csv

UTF-8, comma-separated, `\n` line ends, header row. Empty cell means the site did not say. Sorted by `site_id`, then `listing_key`. Same input gives the same bytes.

| Column | Meaning |
|---|---|
| contract_version | This contract's version |
| source | `site`: read from a site in the register. `file`: read from a supplied price sheet (see "Price sheets") |
| brand, site_id | Whose register, which site |
| seller_name, seller_type | From the register. `merchant`, `wholesaler`, `marketplace` or `unknown` |
| seller_country, served_market | From the register, ISO 3166 alpha-2. A site selling into several markets is one register entry per market |
| seller_is_our_supplier, supplier_key | `true` when this seller is also one of the owner's suppliers, with `supplier_key` the product scraper's source id for it (empty when `false`). Copied from the row's seller: written in the register and checked both ways against the product scraper's suppliers at load |
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

A row with `seller_is_our_supplier` = `true` is still a public price, the same universe price as any competitor's, labelled as the owner's supplier. The consumer shows a supplier's list prices as their own line, apart from the competitor comparison, and never as the buy price: the owner's buy price comes only from his own load.

No photos are collected. Text cells are the seller's own words: read them as data, and do not open the raw file in a spreadsheet that runs formulas.

## site_status.csv

One row per site in the register, every run.

| status | Meaning |
|---|---|
| ok | Every page the site advertised was read, and every listing on them |
| partial | Some rows delivered: a page failed or listings were malformed. `detail` says why |
| unavailable_blocked | The site refused this client (401, 403, 406, 429, 451, a challenge page). 0 rows, and collection of that site stopped at once. Never worked around |
| unavailable_error | No usable answer. 0 rows |
| disabled_terms | No approved decision. No page fetched, 0 rows |
| disallowed_robots | robots.txt forbids the collection or asks for a slower pace than we can run. 0 rows |

A listing that cannot be read or delivered (for example a cell over 100,000 characters) is counted in `malformed_listings` and left out.

A listing outside the run brand's scope (its vocabulary, `market_prices/vocabulary/<brand>.yaml`) is left out and counted in `detail` as `listings out of scope xN`. A site read whole stays `ok`, including when no listing on it is in scope.

Also: `rows`, `malformed_listings`, `duplicate_listings`, `detail`, `terms_checked_on`, `decision`, `observed_at`, `run_id`, `contract_version`, `brand`, `site_id`.

## Running

- **A run:** `RUN_MODE=market-prices` on the ad-hoc scraper task runs `python -m market_prices.run` for that deployment's brand (`SCRAPER_BRAND`), env and bucket. It loads the brand's register, collects every site in it, writes the run folder, then publishes it as above. A register that does not validate stops the run before any request. Only an approved site is requested at all, so a register with none still runs and publishes `site_status.csv` as evidence, leaving `latest.json` where it is. Under the S3 dry run (`SCRAPER_S3_DRY_RUN`) the folder is written locally and nothing is uploaded.
- **How it fetches:** plain HTTP under our own name (`USER_AGENT` in `config.py`): no browser disguise, no proxy, no retry. Every redirect hop must be a public http(s) address, and a body over `MAX_RESPONSE_BYTES` is refused.
- **The dev pilot:** `python -m market_prices.pilot` runs ONE approved site of ONE brand, the register's own `pilot_site` (`market_prices/sites/<brand>.yaml`), against development's bucket and prints a summary with the first rows. It refuses any other environment, a register naming no pilot site, and a pilot site the register has not approved, before a single request.

## Price sheets (`source` = `file`)

The owner puts a competitor's price sheet (`.csv` in UTF-8, or `.xlsx`) in `s3://<bucket>/<env>/market_prices/<brand>/intake/`. RUN_MODE=market-prices-intake reads each file once, by its sha256, and delivers it as its own run folder, published like a crawl run:

- `run_id` and `observed_at` are when S3 received the file (its LastModified), not a price date. A date the sheet states is in `stated_specs` as written.
- `site_id` is the competitor's id from the layout file, and the seller fields are that competitor's. `site_status.csv` has one row per competitor in the file; its `decision` is `supplied` and `terms_checked_on` is empty.
- `source_url` is `s3://<bucket>/<intake key>?versionId=<version>#<sheet>!R<row>` (`#R<row>` for a csv): the exact file version and row.
- `stated_specs` holds every column the layout does not map, header to cell as written.
- `provenance` `site_statement` means the sheet's layout states it once for every row (for example its currency or VAT basis).
- `s3://<bucket>/<env>/market_prices/<brand>/intake_absorbed.csv` records each absorbed file: `sha256`, `key`, `version_id`, `layout_id`, `run_id`, sorted by `sha256`.

A file is refused, named, and left in the intake (never half-read) when it is not `.csv`/`.xlsx`, is over a size cap, matches no layout's exact header row, or names a competitor the layout file does not declare. A row that cannot be read is counted in `malformed_listings`.

## Adding a competitor sheet

One `competitors` entry (id, the names the sheet uses, seller type, country, market, its own `site_domain` (a bare host), and `supplier_key` when the competitor is one of the owner's suppliers; the flag is written in the layout file and checked both ways against the product scraper's suppliers at load, as for a site) and one `layouts` entry in `market_prices/sheet_layouts/<brand>.yaml`. A layout names the sheet (xlsx), the header row and its exact headers, where the competitor's name is (a cell, a column, or the file name), the key, text, price and optional columns, the number separators, the currency symbols it accepts and what the sheet states once. Two layouts with the same headers are refused, and a competitor id may not also be a site id.

## Adding a site

One entry in `market_prices/sites/<brand>.yaml`, carrying `brand: <brand>` (a row of another brand, or none, is refused). A site_id or host already in another brand's register is refused unless both rows name each other in `shared_with: [<brand>]`. A site that is one of the owner's suppliers carries `supplier_key`, the product scraper's source id for it (`scrapers/<source>.py`). At load the register is checked against every supplier the product scraper knows (each fetcher's `site_domain`): an unknown key, a key whose domain is not the site's, and a supplier's domain without the key are all refused. The domains are compared host to host, case and a leading `www.` dropped on both sides: the site's host must be the supplier's domain or a subdomain of it (never a shared parent such as `slabware.com`, which hosts several sellers). `decision: approved` is refused without terms URL, date checked, a summary of the terms, who decided and when, the seller's country and the market served. robots.txt is read from the site's root on every run.
