# Market price observations: delivery contract (version 4)

Reference prices seen on competitor sites, and read from the competitor price sheets the owner supplies, for the pricing module. This path never writes to Medusa, the sync ledger or the vendor product path. It records what a site or a sheet says. Matching, conversion, grading and trust rules belong to the consumer.

Versions: a consumer refuses a contract_version it does not know. Version 2 adds `source` (`site` or `file`) as the second column of both files, and `seller_is_our_supplier` and `supplier_key` after `served_market` in observations.csv; every other column is as in version 1. Version 3 adds nine columns to observations.csv right after `stated_specs` (`thickness_mm`, `finish`, `material_type`, `origin_iso_2`, `thickness_text`, `finish_text`, `material_text`, `origin_text`, `spec_labels`; see "Specs") and four keys to `provenance`; every other column, and site_status.csv's columns, are as in version 2. Version 3 replaces version 2 outright: no version 2 run was ever delivered, so no consumer reads both. Version 4 adds two columns to observations.csv right after `price_amount` (`regular_price_text`, `regular_price_amount`; Mas's sale rule, 14 Nov) and a `register.json` file to every run folder (see "register.json"); site_status.csv is as in version 3. Version 4 replaces version 3 outright, at one cutover after the owner's sheets have loaded under version 3; the consumer reads one version.

The observations.csv header, version 4, in order:

```
contract_version,source,brand,site_id,seller_name,seller_type,seller_country,served_market,seller_is_our_supplier,supplier_key,listing_key,source_url,listing_text,stated_specs,thickness_mm,finish,material_type,origin_iso_2,thickness_text,finish_text,material_text,origin_text,spec_labels,grade_text,price_text,price_amount,regular_price_text,regular_price_amount,currency,price_status,vat_basis,unit_text,delivery_text,delivery_included,min_qty_text,availability_text,provenance,observed_at,run_id,collector_version
```

## Where

```
s3://<bucket>/<env>/market_prices/<brand>/<run_id>/observations.csv
s3://<bucket>/<env>/market_prices/<brand>/<run_id>/site_status.csv
s3://<bucket>/<env>/market_prices/<brand>/<run_id>/register.json
s3://<bucket>/<env>/market_prices/<brand>/<run_id>/manifest.json
s3://<bucket>/<env>/market_prices/<brand>/latest.json
```

- `run_id` is the run's UTC start, `YYYYMMDDTHHMMSSZ`. Run folders are kept, so they are the price history.
- `manifest.json` has the run's `brand` and the sha256, row count and S3 version of each file (`register.json` among them), and is written after them. A folder without a manifest is not a delivered run.
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
| stated_specs | JSON object: the seller's label to its value, as written, for every stated value not read into a column (a label mapped to a spec column below is in that column, not here) |
| thickness_mm | The stated thickness in millimetres: an exact decimal, `^\d+(\.\d+)?$`, no trailing zeros (`20`, `12.5`, never `20.0`). Empty when not stated |
| finish | The brand vocabulary's canonical finish word for the stated finish (for example `Polished`). Empty when not stated |
| material_type | The brand vocabulary's canonical material word for the stated material: for blokport the seed's stone type name exactly (for example `Granite`), for wudport its material word (for example `Plywood`). Empty when not stated |
| origin_iso_2 | The stated origin country, ISO 3166 alpha-2, lower case, `^[a-z]{2}$`. Lower case on purpose: the pricing module keys every country that way, while `seller_country` and `served_market` stay as the register writes them (upper case). Empty when not stated |
| thickness_text, finish_text, material_text, origin_text | Each spec as the seller wrote it. Empty when not stated |
| spec_labels | JSON object: each stated spec column (`thickness_mm`, `finish`, `material_type`, `origin_iso_2`) to the seller's label or sheet header it was read from, for example `{"finish":"Afwerking","thickness_mm":"Dikte"}`. A column not stated has no key |
| grade_text | The seller's grade words, as written, when the register names the label that holds them |
| price_text | The price as the seller shows it: the CURRENT price |
| price_amount | Exact decimal, only when the listing has one price: the CURRENT price |
| regular_price_text | The regular price the seller shows beside a current one (a struck "was" price, or a regular price the source states separately), as written. Empty when the listing shows one price |
| regular_price_amount | Its exact decimal, read as the current price is. Empty when `regular_price_text` is empty or is not one price. A listing is on sale exactly when `regular_price_text` is set: there is no separate flag |
| currency | ISO 4217 code as stated |
| price_status | `priced`, `on_request` or `none`. A range, or zero (made-to-measure), is `none` with `price_text` kept |
| vat_basis | `incl`, `excl` or `unknown` |
| unit_text | The selling unit as written |
| delivery_text, delivery_included | As written; `yes`, `no` or `unknown` |
| min_qty_text | The minimum order as stated |
| availability_text | The stock wording as written |
| provenance | JSON object for vat_basis, unit_text, delivery_text, delivery_included, grade_text, price_amount, currency, min_qty_text, thickness_mm, finish, material_type and origin_iso_2: `listing`, `site_statement` (said once for the whole site) or `unknown`. The four spec columns are `listing` when stated and `unknown` when empty. A listing that states its own VAT basis (schema.org `valueAddedTaxIncluded`) gives `vat_basis` provenance `listing`, over any site statement, and a listing that states its own selling unit (a `declared_html` unit source) gives `unit_text` provenance `listing` the same way; site_status `detail` counts the listings where the two differ |
| observed_at, run_id | When this run started, UTC |
| collector_version | Which collector read it |

A row with `seller_is_our_supplier` = `true` is still a public price, the same universe price as any competitor's, labelled as the owner's supplier. The consumer shows a supplier's list prices as their own line, apart from the competitor comparison, and never as the buy price: the owner's buy price comes only from his own load.

No photos are collected. Text cells are the seller's own words: read them as data, and do not open the raw file in a spreadsheet that runs formulas.

## Specs

A listing's thickness, finish, material and origin are delivered as normalised columns, so the consumer matches on them and never parses `stated_specs` or `listing_key`. Each source maps its own labels in its own config, as it maps price, VAT and unit:

- **A site:** its register row's `spec_labels` names the labels (a WooCommerce attribute name, a schema.org `additionalProperty` name) that state each spec, compared case and spacing aside, and the unit a bare thickness number is in. A site whose row has no `spec_labels` yet delivers the four columns empty: the labels come from pilot evidence, never guessed.
- **A sheet:** its layout's `specs` names the column that states each spec, and the unit of a bare thickness number (a header `Thickness (cm)` declares cm).
- **Words:** the brand's vocabulary (`market_prices/vocabulary/<brand>.yaml`) maps each stated finish and material to its canonical word (`finishes`, `materials`: canonical to the words that mean it, in any language). A stated value matches a word whole, case, accents and punctuation aside, never a part of it. Its `unstated` words (for example `Not specified`) mean the seller stated nothing: the column is empty and `detail` counts them as `stated as unstated xN`.
- **Countries:** one brand-neutral table, `market_prices/countries.yaml`: each ISO 3166 alpha-2 code with its alpha-3 code and its names. A stated code matches as ISO writes it (upper case, `BE`, `BEL`), a name case and accents aside; the column is delivered in lower case (`be`).
- **Thickness:** one number (a dot or a comma as the decimal mark), with `mm` or `cm` or bare. A stated unit wins over the declared one; cm is multiplied by 10 exactly.

A stated value that has no honest mapping refuses its row, which is counted in `malformed_listings` with its reason and never delivered with a guess:

| Reason in `detail` | When |
|---|---|
| `thickness '<value>' is not one number in mm or cm` | A range, two numbers, a unit other than mm or cm, or a bare number with no declared unit |
| `finish '<value>' is not in <brand>'s finishes` | The finish is neither a finish word nor an unstated word |
| `material '<value>' is not in <brand>'s materials` | The material is neither a material word nor an unstated word |
| `origin '<value>' is not in the country table` | The origin is neither a code nor a name in the table, nor an unstated word |
| `<column>: two labels state different values '<a>' / '<b>'` | A site states one spec under two of its labels with different values |

## register.json

Every run folder carries the brand's register as it stood when the run started: one row per APPROVED site in `market_prices/sites/<brand>.yaml` and per competitor declared in `market_prices/sheet_layouts/<brand>.yaml`, whatever the run read. The consumer upserts its sources from it by `source_key`; it never needs a writer of its own. UTF-8 JSON:

```json
{"brand": "blokport", "contract_version": "4", "run_id": "20261114T150000Z", "sources": [
  {"brand": "blokport", "source_key": "cereser_carrara", "source": "file", "seller_name": "Cereser Listino Carrara",
   "seller_type": "wholesaler", "country_iso_2": "it", "served_market": "EU", "price_basis": "net",
   "net_discount": "0.2", "net_discount_stated_by": "owner", "vat_rate": null, "vat_rate_stated_by": null,
   "seller_is_our_supplier": false,
   "supplier_key": null, "decided_by": "owner (supplied workbook) via Mas", "decided_on": "2026-11-13"}]}
```

`sources` is sorted by `source_key`, and every row has exactly these keys (an empty value is `null`):

| Key | Meaning |
|---|---|
| brand | The register's brand |
| source_key | The site's `site_id` or the competitor's `competitor_id` (the two are kept apart at load); the `site_id` of its observations |
| source | `site` (a register site) or `file` (a sheet competitor), as in observations.csv |
| seller_name, seller_type | As its observations carry them |
| country_iso_2 | The seller's country, ISO 3166 alpha-2 in LOWER case (`it`), `^[a-z]{2}$`: the pricing module keys countries that way (as `origin_iso_2`); its observations' `seller_country` stays as the register writes it (upper case) |
| served_market | As its observations carry it, as the register writes it (`EU` included) |
| price_basis | `list` or `net`: whether its prices are list prices or net prices. Declared on every approved site and every competitor; never defaulted |
| net_discount | For a `net` source only (else `null`): the exact decimal in [0, 1) that its net prices are below list (`0.2`: list = net / (1 - 0.2)) |
| net_discount_stated_by | `site` (the site's own pages or terms state it) or `owner` (the owner's statement); `null` exactly when `net_discount` is |
| vat_rate | The VAT rate as an exact decimal (`0.2`), only when the site or the owner states it; else `null`, never inferred |
| vat_rate_stated_by | `site` (the site's own pages or terms state it) or `owner` (the owner's statement); `null` exactly when `vat_rate` is |
| seller_is_our_supplier, supplier_key | As its observations carry them |
| decided_by, decided_on | Who approved the source and when (`YYYY-MM-DD`): a site's register decision, a competitor's declared decision |

## site_status.csv

One row per site in the register, every run.

| status | Meaning |
|---|---|
| ok | Every page the site advertised was read, and every listing on them |
| partial | Some rows delivered: a page failed, a cap was reached, or listings were malformed. `detail` says why, naming the most common reasons listings were skipped with their counts |
| unavailable_blocked | The site refused this client (401, 403, 406, 429, 451, a challenge page). 0 rows, and collection of that site stopped at once. Never worked around |
| unavailable_error | No usable answer. 0 rows |
| disabled_terms | No approved decision. No page fetched, 0 rows |
| disallowed_robots | robots.txt forbids the collection or asks for a slower pace than we can run. 0 rows |

A listing that cannot be read or delivered (for example a cell over 100,000 characters, or a product page with no schema.org Product) is counted in `malformed_listings` and left out; `detail` names the reasons, as `<reason> x<count>`, the most common first and the rest summed as `other reasons x<count>`.

A listing outside the run brand's scope (its vocabulary, `market_prices/vocabulary/<brand>.yaml`) is left out and counted in `detail` as `listings out of scope xN`. A site read whole stays `ok`, including when no listing on it is in scope.

Also: `rows`, `malformed_listings`, `duplicate_listings`, `detail`, `terms_checked_on`, `decision`, `observed_at`, `run_id`, `contract_version`, `brand`, `site_id`.

## Running

- **A run:** `RUN_MODE=market-prices` on the ad-hoc scraper task runs `python -m market_prices.run` for the brand named by the required run input `SCRAPER_MARKET_PRICES_BRAND` (set as a container override; never the deployment's own brand, so one deployment's task runs every brand's register), in that deployment's env and bucket, under `<env>/market_prices/<brand>/`. A run without it stops before any request; an unknown brand (no `sites/<brand>.yaml`) is refused the same way. It loads the brand's register, collects every site in it, writes the run folder, then publishes it as above. A register that does not validate stops the run before any request. Only an approved site is requested at all, so a register with none still runs and publishes `site_status.csv` as evidence, leaving `latest.json` where it is. Under the S3 dry run (`SCRAPER_S3_DRY_RUN`) the folder is written locally and nothing is uploaded.
- **Collectors:** a site names its platform. `woocommerce_store` reads the shop's public Store API. `schema_org_product` reads each product page's schema.org Product/Offer structured data (JSON-LD), reached through the entry points the register names (see "Adding a site"): one listing per product, or per variant of a ProductGroup. Only what the structured data states is read: a price is `priced` only as one exact decimal in schema.org's form (a dot, no grouping), and a span, a zero or any other form is `none` with `price_text` kept as stated. Offers stating prices in several currencies are not one price either: `none`, every stated price in `price_text` (`10 EUR | 10 GBP`), `currency` empty, and `detail` counts them (`offers state several currencies x<n>`); the listing is still delivered. `listing_key` is the product's `sku`, else its `productID`, else its page's path; a variant shares its page, so without a `sku` or `productID` it is keyed by its own `@id` or `url` on the site, and one with no identity of its own is a malformed listing (`variant has no id of its own`), never merged into another. A site that states no structured price (prices loaded by script, or a browser needed) gives no rows, and `detail` says why; it is never worked around. `declared_html` reads a site whose pages state their facts in their HTML, where its register row DECLARES them (see "Adding a site"): one record (an element) is one listing, so a product page's per-thickness rows are one listing each. What the page shows is what is read: the price is the price element's text as shown, its amount read in the site's declared number notation; a listing that shows no price is `none` with its words kept, whatever a script or an analytics attribute holds. Struck text (`<del>`, `<s>`, `<strike>`) is never the price: the current price is, and the struck "was" price is `regular_price_text` (and `regular_price_amount`) when the row declares a `regular_price` source for it. `woocommerce_store` fills the regular price from the Store API's own `regular_price` when it differs from the price; `schema_org_product` and the sheets leave it empty, as their sources state no regular price apart from the price.
- **How it fetches:** plain HTTP under our own name (`USER_AGENT` in `config.py`): no browser disguise, no proxy, no retry. Every redirect hop must be a public http(s) address, and a body over `MAX_RESPONSE_BYTES` is refused.
- **The dev pilot:** `python -m market_prices.pilot` runs ONE approved site of ONE brand (the same required run input `SCRAPER_MARKET_PRICES_BRAND`), that brand register's own `pilot_site` (`market_prices/sites/<brand>.yaml`), against development's bucket and prints a summary with the first rows. It refuses any other environment, a register naming no pilot site, and a pilot site the register has not approved, before a single request.

## Price sheets (`source` = `file`)

The owner puts a competitor's price sheet (`.csv` in UTF-8, or `.xlsx`) in `s3://<bucket>/<env>/market_prices/<brand>/intake/`. RUN_MODE=market-prices-intake, for the brand named by the same required run input `SCRAPER_MARKET_PRICES_BRAND`, reads each file once, by its sha256, and delivers it as its own run folder, published like a crawl run:

- `run_id` and `observed_at` are when S3 received the file (its LastModified), not a price date. A date the sheet states is in `stated_specs` as written.
- `site_id` is the competitor's id from the layout file, and the seller fields are that competitor's. `site_status.csv` has one row per competitor in the file; its `decision` is `supplied` and `terms_checked_on` is empty.
- `source_url` is `s3://<bucket>/<intake key>?versionId=<version>#<sheet>!R<row>` (`#R<row>` for a csv): the exact file version and row.
- `stated_specs` holds every column the layout does not read into an observation field or a spec column, header to cell as written. The identity columns (the column naming the competitor, every `listing_key` part) are kept there too, unless the column is read into a field (a product name that is both a key part and `listing_text` is in `listing_text`; a thickness column the layout's `specs` names is in `thickness_mm` and `thickness_text`), so nothing is ever parsed out of `listing_key`.
- `provenance` `site_statement` means the sheet's layout states it once for every row (for example its currency or VAT basis).
- `s3://<bucket>/<env>/market_prices/<brand>/intake_absorbed.csv` records each absorbed file: `sha256`, `key`, `version_id`, `layout_id`, `run_id`, sorted by `sha256`.

A file is refused, named, and left in the intake (never half-read) when it is not `.csv`/`.xlsx`, is over a size cap, matches no layout's exact header row, or names a competitor the layout file does not declare. A row that cannot be read is counted in `malformed_listings`.

## Adding a competitor sheet

One `competitors` entry (id, the names the sheet uses, seller type, country, market, its own `site_domain` (a bare host), and `supplier_key` when the competitor is one of the owner's suppliers; the flag is written in the layout file and checked both ways against the product scraper's suppliers at load, as for a site) and one `layouts` entry in `market_prices/sheet_layouts/<brand>.yaml`. A layout names the sheet (xlsx), the header row and its exact headers, where the competitor's name is (a cell, a column, or the file name), the key, text, price and optional columns, the spec columns (`specs`, see "Specs"), the number separators, the currency symbols it accepts and what the sheet states once. A sheet that names several competitors, each stating its price in its own column, names those columns per competitor (`price_by_competitor`, competitor id to column) instead of one `price`: each row takes its own competitor's column, every other stated column (another competitor's price among them) is kept in `stated_specs`, and a row naming a competitor the layout gives no price column refuses the file. A competitor named by a column is matched only by its declared names: where that column states the source document a row was transcribed from (the owner's workbook tabs), each competitor's names include that document's file name, so a sheet from a new source document refuses the file until the layout gains the name. Two layouts with the same headers are refused, and a competitor id may not also be a site id.

## Adding a site

One entry in `market_prices/sites/<brand>.yaml`, carrying `brand: <brand>` (a row of another brand, or none, is refused). A site_id or host already in another brand's register is refused unless both rows name each other in `shared_with: [<brand>]`. A site that is one of the owner's suppliers carries `supplier_key`, the product scraper's source id for it (`scrapers/<source>.py`). At load the register is checked against every supplier the product scraper knows (each fetcher's `site_domain`): an unknown key, a key whose domain is not the site's, and a supplier's domain without the key are all refused. The domains are compared host to host, case and a leading `www.` dropped on both sides: the site's host must be the supplier's domain or a subdomain of it (never a shared parent such as `slabware.com`, which hosts several sellers). `decision: approved` is refused without terms URL, date checked, a summary of the terms, who decided and when, the seller's country and the market served. A site's `spec_labels` (see "Specs") names the labels that state its specs, taken from its listings as fetched. robots.txt is read from the site's root on every run.

An approved `schema_org_product` site needs `discovery` (another platform refuses it; a site never requested, such as a blocked one registered unapproved, may leave it out, so nothing about it is invented), and takes no `query`:

```yaml
discovery:
  sitemaps: ["/sitemap-products.xml"]        # paths on base_url; a sitemap index (and a gzipped file) is followed
  listing_pages: ["/sheet-materials/plywood"]  # paths; their links and rel="next" pages are followed
  product_path: "^/p/[^/]+/?$"                # regular expression on a URL's path that marks a product page
```

A `declared_html` site also declares where its facts sit (`html`), each value a source inside one record: exactly one of `json` ([attribute, dotted.path] into a JSON-valued attribute of the record or an element under it), `attr` (an attribute of the record, or of the first element under it with `class`), `text` (the text of the record or of that element; with `struck`, only its struck text, for the `regular_price` field alone) or `label` (the value that element pairs with the label: a th/td row, a dt/dd pair, or a label element followed by its text, the label matched whole, case, colon and whitespace aside), optionally narrowed by a `pattern` with one capture group. A class lookup for current text skips struck subtrees. The price source is always visible `text`: a JSON or attribute value (an analytics or data attribute) is never the price, only identity or a spec. A currency source must read an ISO 4217 code, else the listing keeps its price with no currency and `detail` counts `currency '<value>' is not an ISO 4217 code`; a declared currency symbol counts only whole, not joined to a letter (so `R$` is never `$`, and an undeclared `CA$` is no currency), and a price text holding two declared currencies gives none (`price text holds more than one declared currency symbol`).

```yaml
html:
  read: listing_pages                        # or product_pages: the pages discovery reaches
  record: {class: items-loop__item}          # or {attribute: data-...}; `tag` narrows a class
  fields:
    listing_key:  {json: [data-ms-tracking-item, item_id]}
    listing_text: {json: [data-ms-tracking-item, item_name]}
    url:          {class: stretched-link, attr: href}       # absent: the page's own URL
    price:        {class: items-loop__item-price, text: true, pattern: "\\$\\s*([\\d,]+\\.\\d{2})"}
    unit:         {class: sale-unit, text: true, pattern: "per\\s+(\\S+)"}
    currency_symbols: {"$": USD}                            # or currency: a source of the code
    on_request: [Contact us for pricing]                    # price texts meaning on request, as the page states them
    regular_price: {class: price, text: true, struck: true} # the struck "was" price: regular_price_text / _amount
  number: {decimal: ".", thousands: ","}                    # the sheet layouts' notation model
  specs:
    Material:      {class: specs, label: Material}
```

A record without its `listing_key` or `listing_text` (or where their pattern does not match), a JSON attribute that does not parse, a `url` off the site, or a product page with no declared record is a malformed listing, counted with its reason. A spec or an optional field not held, or not in its declared form, is simply not stated. Material is read as stated and never filtered by a register pattern: v3's vocabulary refuses an unmapped material.

At least one sitemap or listing page. The entry points are the in-scope part of the site (for example its sheet-material categories), never its whole catalogue: nothing is guessed from a link before a page is fetched. Product pages are read in sorted order, each only where robots.txt allows it (a forbidden one is skipped unrequested and counted in `malformed_listings`, `detail` reason `robots.txt disallows the product page`; a query is never dropped to reach an allowed URL); a page that answers with an error is its listing's problem, while a block or no answer ends the site. Caps (`SCHEMA_ORG_*` in `config.py`) bound the sitemaps, listing pages and product pages of one site; reaching one is reported in `detail`.
