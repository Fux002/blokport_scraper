# Pipeline bug audit findings

A whole-pipeline adversarial audit (excluding the image pipeline) surfaced the issues below. The FIXED ones
are done + verified. The DEFERRED ones are real but were not fixed in this pass because they need live
supplier data to verify or touch a critical/scraper path that should not be changed blind; each lists the
exact fix so you can apply it deliberately.

## Fixed + verified

- **product_state inventory-change false positive (determinism)** `stages/product_state.py`. `new_inv` was
  normalized (`str(int(parse_number))`) but `old_inv` was the raw export string, so `'10.0'`/`'1,000'`/`' 10 '`
  read as a phantom change every run -- spurious inventory deltas + non-byte-identical re-runs. Fixed:
  normalize `old_inv` the same way, preserving a real `0`.
- **tureks silent catalog truncation -> wrong delist** `scrapers/tureks.py`. Fetched only page 1 of
  `per_page=100` and marked the run COMPLETE on overflow, so rows 101+ could be discontinued/stock-0. Fixed:
  `mark_incomplete()` on `X-WP-TotalPages > 1`, so a truncated scrape is never authoritative (fails loud;
  add pagination to restore authority).
- **ledger synced-ack un-retires a retiring row** `ledger/sync.py`. The synced `UPDATE` matched on primary
  key only, so a stray/out-of-order synced ack flipped a `retiring` variation back to `synced` while its
  tombstone still served `/removed`. Fixed: `AND state != 'retiring'` on the synced update (ack from
  pending/syncing/dirty stays allowed by design; only retiring is protected) + a regression test.
- **gap worklist collapses distinct-type gaps** `run.py` `_gap_rows`. The dedup key omitted
  `suggested_type`/`suggested_quality`, so two distinct tree gaps differing only by type/quality collapsed to
  the first. Fixed: include both in the key.
- **ledger serve stranded a leased page on one corrupt JSON cell** `ledger/sync.py`. `json.loads(...)` in
  the row-build was not per-row isolated, so one malformed cell 500'd the whole pull. Fixed: `_isolate`
  builds each row in a try/except -- a bad row is dead-lettered to `gap_held` (surfaces in /failures) and
  dropped from the page, the good rows still serve. + a regression test.
- **CSV formula injection behind leading whitespace** `core/csvio.py` `safe_cell`. `' =cmd'` (a formula
  leader after whitespace) slipped past the first-char check. Fixed: also neutralize when the stripped value
  starts with `=`/`+`/`-`/`@`. + a test.
- **blank owner/sales-channel emitted an empty required cell** `stages/validate.py`. The non-CLI emit paths
  (ledger render/writethrough, medusa_client) bypassed the `run.main` prod guard. Fixed: validate now rejects
  a row with a blank `company_id`/`sales_channel_id` (`owner_missing`) -- the row-level gate every emit path
  shares. Safe: dev always has the owner via its defaults, so it only bites a misconfigured prod. + a test.
- **ledger bootstrap coerced messy stock to 0** `ledger/bootstrap.py`. `int(inv) if inv.isdigit() else 0`
  turned `'10.0'`/`'1,000'` into a phantom out-of-stock 0 (then a spurious delta). Fixed: `parse_number`
  reads them correctly. (populate.py was already fine -- `inventory_for` normalizes before the check.)

## Scraper-contract layer (declared at the source, not inline patches)

These are properties of *what each source offers*, DECLARED at scraper setup and used by extraction (never
guessed inline). See NEW_SOURCE_CHECKLIST.md.

- **marenostone dimension unit -- FIXED** `scrapers/marenostone.py`. Verified against 264 real scraped rows
  (all cm). Now declares `dimension_unit = "cm"` (the locked-in source convention); `_dims_from_html` uses
  the declared unit instead of a hardcoded `else 'cm'`, and a non-numeric value (`'Free'` free-length slabs)
  keeps its raw text instead of becoming a bogus `'Freecm'` (so it rejects as "no real size"). + tests;
  certify marenostone green.
- **tureks order-dependent category -- BLOCKED on scraper setup** `scrapers/tureks.py` `parse_categories`
  picks the FIRST non-`slabs` category as the material (category ORDER decides variety vs type). Not fixed
  because **tureks is not set up/tested yet** -- it must be built + certified per NEW_SOURCE_CHECKLIST.md
  first, with the category rule DECLARED from a live tureks response (which level is material/type vs
  variety), not an order heuristic. Do not scrape tureks until it clears the checklist.
- **zucchi first-page vs `contagemProds=0`** `scrapers/zucchi.py`. Trusts the count over the actual page
  content. Declare the source's rule (trust content) from a live zucchi response; needs the live sample.

## Low / latent (documented, low impact)

- `ledger/populate.py` / `bootstrap.py` `int(x) if str(x).isdigit() else 0` coerces negatives/`'12 '`/decimals
  to `0` (an unintended out-of-stock signal); prefer `parse_number`.
- `core/numbers.py` `parse_number`: a single `.` is treated as a decimal, so a supplier using `.` as a
  thousands separator (`'1.200'` meaning 1200) mis-reads as `1.2`. Documented tradeoff; per-source override
  if such a supplier appears.
- `scrapers` slabware join helpers turn a legitimate numeric `0` into `""` (`str(c.get(k,'') or '')`), masking
  a real zero that should reject downstream.

## Verified SOUND (checked, no change needed)

Number decimal/thousands disambiguation, range midpoint parsing, unknown-unit rejection, the matching-engine
determinism (batch-order independence, tied->review, colour-conflict guard, phonetic floor, short-generic
guard), surrogate minting (format in the basis), `match_key` normalization, `reconcile_tree`
type-authoritative + snap-with-flag, `origin_map` word-boundary lookup, CSV emit template alignment + atomic
write + formula sanitization on operator files only, the run-level scrape-floor / >50%-drop / >30%-delist /
prod-owner guards, and the ledger happy-path lease/ack idempotency + product-ahead-of-variation guards.
