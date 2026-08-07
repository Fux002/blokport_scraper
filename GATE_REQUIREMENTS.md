# Gate Requirement Blueprint (seam-contract view)

Status: DRAFT for review. The working checklist for making each pipeline seam
robust. Organizing principle (confirmed): **a gate confirms that step N's OUTPUT
satisfies step N+1's INPUT, and surfaces any failure in that seam's own diagnostic**
— so a missing input is caught where it is introduced, not steps downstream.

### Progress
- [x] derive stock ladder: count -> area-division -> undetermined; single owner (was step 1).
- [x] **marenostone stock derived from other data** (Ready Stock SQM -> pieces via face area). The
      "derive if possible" case: stock is now a real number, not undetermined. Scraper reuses the
      existing spec-table parse; `raw_stock_m2` kept separate from `raw_total_m2` so bundle_size is safe.
- [ ] Seam A: keys_dedupe collision flag. (Global ingest value-checks rejected: would abort sparse
      sources; per-source completeness already covered by HEALTH fill_floors.)
- [ ] Seam C: stock hold at PROCESS (undetermined -> held); docstring fix; origin authority; title/ports.
- [ ] Seam B: reconcile type-authority downgrade flag.
- [ ] Seam D + G7: hard rules per model; one shared requirement declaration.

We clean each step's internal logic AND close its seam's holes, one seam at a time,
each a small tested increment. This doc is the "overall image" — updated as we go so
nothing is forgotten.

## The seam ladder (run.py order)

```
scrape → adapt → [HEALTH] → [INGEST]
       → dedupe → format → normalize → match → reconcile → [CLEAN]
       → derive → images → constants → [PROCESS]
       → validate (Stage 9) → emit → ledger → Medusa
```

Severity: **HARD** = row rejected (never emits); **SOFT** = flagged, still emits;
**report** = batch diagnostic only. Principle: each handoff requirement is owned and
surfaced at ONE seam, at the severity its failure warrants; downstream may re-use the
result but does not silently re-discover it.

---

## SEAM 0 — scrape OUTPUT → [HEALTH] → adapt INPUT

- **scrape outputs:** a raw CSV per source.
- **adapt needs:** the columns each adapter's `field_map` reads.
- **HEALTH confirms:** required columns present, fill-floor, value patterns, rowcount vs
  baseline, adapter smoke test. Per source. FAILED aborts.
- **HOLES:**
  - H0.1 — `required_columns` per source drift from the true downstream need: **dimensions
    columns required by NO source** though dims are HARD later; **quality optional for
    marenostone** though `quality_id` is HARD; **stock inconsistent** (pol/varsha require a
    count, zucchi/marenostone do not).
- **Internal cleanup:** none needed in health.py itself (the 5 checks are sound); the fix is
  the per-source declaration content + making it the single source (see Seam A).
- **Diagnostic:** `scrape_health_<source>.json` (drift items). Good; just incomplete inputs.

## SEAM A — adapt OUTPUT → [INGEST] → clean INPUT

- **adapt outputs:** whatever `raw_*` the adapter mapped.
- **clean needs (dedupe/format/normalize/match/reconcile):** unique identity basis,
  `raw_name`, `raw_format`, `raw_type/color/finish/quality`, `variety_match_key`.
- **INGEST confirms:** `raw_name`|`variety_match_key`, `dimensions_source`, `image_source`
  — all SOFT.
- **HOLES:**
  - HA.1 — confirms the WRONG step's inputs: `dimensions_source`/`image_source` are needed by
    *derive/images* (Seam C), not by clean. Early-presence is fine, but ownership belongs at
    Seam C; here it muddies the contract.
  - HA.2 — does NOT confirm identity uniqueness → distinct-product collision **dropped
    log-only** (keys_dedupe.py:78), no ReviewFlag. [G5]
  - HA.3 — does NOT confirm `raw_format` → block/tile silently filed as slab (flagged
    `format_unresolved` only).
  - HA.4 — does NOT confirm `variety_match_key` → surfaces as a `missing_variation` gap much
    later at match.
  - HA.5 — does NOT confirm `raw_type/color/finish/quality` → empty becomes a null id,
    rejected 5 steps later at validate (`required_id_null`), diagnostic points at validate not
    the scrape.
  - HA.6 — does NOT confirm a stock raw-signal (the input derive_inventory needs). [G6]
- **Internal cleanup:**
  - keys_dedupe: emit a ReviewFlag on a distinct-product collision drop (not log-only).
  - format_resolve / mint: already clean (verified) — leave.
- **Diagnostic:** INGEST GateReport (SOFT, report-only). Present but under-covers the handoff.

## SEAM B — clean OUTPUT → [CLEAN] → derive INPUT

- **clean outputs:** resolved `type/color/finish/quality` ids+names, `variation_id/key/name`,
  `format_value`, `tree_gaps`.
- **derive needs:** `format_value`, `type_name`, `variation_name`, ids passed through.
- **CLEAN confirms:** canonical attribute casing (SOFT flag); tree-gap (report-only).
- **HOLES:**
  - HB.1 — CLEAN *sees* the tree-gap but does not hold; enforcement deferred to validate
    (Seam D). The diagnostic exists at the right seam but the ownership is 4 steps away.
  - HB.2 — `variation_key` with no known type-slug → silent `type_name_fallback` downgrade
    (reconcile), not surfaced; downstream origin must not trust it but nothing flags it.
- **Internal cleanup:** reconcile — surface the type-authority downgrade as a flag.
- **Diagnostic:** CLEAN GateReport. Casing good; tree-gap report-only by design.

## SEAM C — derive/images/constants OUTPUT → [PROCESS] → emit INPUT

- **derive/images/constants outputs:** `category_pcat_id`, `length/width/height`, `weight`,
  `inventory_quantity`, `origin_country_code`, `port_ids`, `title`, `description`,
  `handle/slug`, `image_keys`, `company_id/sales_channel_id`.
- **emit needs:** all of the above as valid Medusa columns (see appendix cross-check).
- **PROCESS confirms:** `origin_country_code` non-empty (HARD). That is 1 of ~10.
- **HOLES:**
  - HC.1 — **stock:** `inventory_quantity` determined is not confirmed. `stock_undetermined`
    is flagged (derive, done) but never held. [G1] — the original problem.
  - HC.2 — `title` emitted, can be "", confirmed nowhere. [G3]
  - HC.3 — `port_ids` emitted, defaulted+flag, confirmed nowhere (planned emit-guard). [#14]
  - HC.4 — dims/category/handle/owner/image are produced here but confirmed at Seam D, not C.
    (Acceptable if Seam D is the chosen owner — but then PROCESS is redundant with validate for
    origin; pick one, see model note.)
- **Internal cleanup:** derive_inventory — DONE (ladder + area-division + single owner). Fix
  the docstring overclaim (it says validate holds; validate does not yet). weight is implicitly
  covered by dims (weight computes whenever dims are valid) — note, no separate check needed.
- **Diagnostic:** PROCESS GateReport (advisory, non-aborting) + review flags.

## SEAM D — validate (Stage 9) OUTPUT → emit

- **validate confirms (18 HARD):** attribute ids, tree-gap, category active, handle/slug +
  uniqueness, owner ids, image (config-gated), dimensions (invalid/defaulted/unavailable).
- **HOLES:**
  - HD.1 — stock (`inventory_quantity`) not checked. [G1]
  - HD.2 — origin NOT re-checked (only Seam C) → the final authority is not self-sufficient. [G2]
  - HD.3 — `title`, `weight`, `port_ids`, `surrogate_key` not checked.
- **Internal cleanup:** validate is clean and well-structured; the fix is adding the missing
  hard rules (stock, and origin if we consolidate authority here).
- **Diagnostic:** `products_rejects.csv` + review split. Good.

---

## The pattern of the holes (the actual diagnosis)

The robustness check leaks in three consistent ways:
1. **Checked at the wrong seam** — INGEST checks dims/image (needed at Seam C); CLEAN sees the
   tree-gap but validate enforces it.
2. **Confirmed nowhere** — stock, title, port_ids reach emit with no gate.
3. **Confirmed too late** — a missing `raw_type` becomes a null id surfaced only at validate,
   far from the adapter seam where it was actually absent.

The fix: **each seam owns and surfaces its own handoff**; ONE seam owns each requirement at the
right severity; and the three "required" idioms (source_contracts columns / ingest invariants /
validate rejects) read from ONE declaration so they can't drift. [G7]

## Model note (single-owner)

Confirmed direction: each requirement is owned and surfaced at exactly one seam. Open sub-choice
per requirement — where a HARD emittability reject lives when the input is available early but
consumed late (e.g. `raw_type`): reject early at Seam A, or diagnose-soft early + hard-reject at
the consuming seam. Decide per requirement as we reach each seam; the registry records the pick.

---

## Appendix 1 — field registry (completeness proof, forward + backward)

(Retained from the field-indexed view: every `raw_*` input traced forward, every emit column
traced backward. See git history for the full 21-row table; the seam contracts above are derived
from it. Requirements by #: 1 identity, 2 variety-name, 3 match-key, 4 format, 5 variation,
6 type, 7 color, 8 finish, 9 quality, 10 dimensions, 11 weight, 12 stock, 13 origin, 14 ports,
15 category, 16 handle, 17 owner, 18 image, 19 title, 20 description, 21 bundle.)

## Appendix 2 — output completeness cross-check

Every emit `COLUMN_MAP` column maps to a requirement # or a backend constant; no orphan columns.
Constants (no per-row requirement): Product Status, Variant Title, Manage Inventory, Allow
Backorder, Option 1 Name/Value, STN Popular, Visibility, Discountable, Specification File Ids.

## Edge-case catalog (do-not-forget)

1. Three-way missing distinction (dims-proven, stock must mirror): fetch-failed→held-transient;
   absent-but-derivable→derive+flag; absent-underivable→hold; determined-incl-0→ships.
2. Explicit 0 is trusted (stock; a 0 slab-count is not a fall-through).
3. Inactive category (tiles until pcat) → held, not rejected-forever.
4. Terminal imageless publish (`no_publishable_image`) must NOT be rejected.
5. Type authority affects origin (`origin_type_unverified`).
6. Case-folded SKU collapse (match Medusa upload casing).
7. Format in mint basis — slab/block same name must not collapse.
8. Block/tile need an explicit tag — structural inference only yields slab.
9. Attribute fill sources differ — color from variety (reconcile), finish/quality last-resort.
10. Idempotent mint — url/name basis not ordinal, byte-stable re-scrape.
11. **PROTECTED INVARIANT — (type, variant) atomic identity.** A variation's identity is the atomic pair
    (type, variety-name), always unique; every variant has exactly ONE type; no type-less and no multi-type
    variant (Aqua Blue gneiss != Aqua Blue granite = two variations). Type authority = the variation Key's
    type slug (`type_slug_from_key`), never name/alias tokens. Match on (type+variety); unresolved -> ONE
    review list, never a guessed/minted type. reconcile `_bound_type` `type_name_fallback` is correct (keeps
    the type value, marks provenance non-authoritative so origin skips homonym lookups) -- leave it. This
    finally works after days of correction and is FRAGILE: do NOT touch type/variation resolution, matching,
    reconcile, curate, or `variety_identity` without explicit sign-off AND full type/variation test coverage.
    Stock/inventory work is orthogonal (only `inventory_quantity`).

## Implementation order (seam by seam)

- Seam A: ingest owns the clean-input handoff (identity+format+match_key+vocab+stock-signal,
  right severity); keys_dedupe collision flag; move dims/image ownership to Seam C.
- Seam B: reconcile type-authority downgrade flag.
- Seam C: stock hold (G1); origin authority decision (G2); title/port_ids (G3/#14); docstring fix.
- Seam D: add the hard rules the chosen model assigns here.
- Cross-cutting: one shared requirement declaration (G7) feeding health + ingest + validate.
