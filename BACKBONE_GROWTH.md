# Backbone growth: how the tree learns new leaf values

The backbone is an old, hand-maintained base. It is deliberately narrow: every variety carries an
allowed SET of colours, finishes and qualities, and a scraped product is only emitted if each of its
values is in that set (`Backbone.is_valid_leaf`). When a real product arrives with a value the variety
does not allow yet (a grade-B slab under an A-only variety, a finish the variety has never carried), it
is correctly held back. The tree is meant to GROW to absorb these, and this is how.

There are two distinct growth paths. Do not confuse them.

| The new value is... | Path | Because |
|---------------------|------|---------|
| already in Medusa (quality B/C/D, a finish/colour the vocab has) but not on this variety | **backbone-leaf loop** (this document) | nothing to create in Medusa; the variety's allowed set just needs widening |
| brand new to Medusa (a finish the attribute vocabulary has never had) | **attribute-id loop** (`attributes_to_add.csv` -> create in Medusa -> paste id) | it is a real Medusa entity that must be created before anything can reference it |

## The clean base is never mutated

The committed seed (`catalog_source/backbone_*.json`) is the pristine base and is **never written by the
pipeline**. Growth lives as a separate, reversible OVERLAY in `config.db` (which is snapshotted and
restored, so approvals survive an ECS restart). At load time the effective backbone = seed + overlay,
merged in memory only. Drop the overlay (or reject the row) and the pristine tree is back. So the base
can always be called back to, and an approval can never corrupt it.

## The loop

```
SURFACE            REVIEW / APPROVE (:4200)      OVERLAY (config.db)        APPLY                RELEASE
produce writes  -> operator bulk-approves     -> approved row becomes    -> load_all merges    -> Medusa's
a suggestion       the confident ones            a leaf-decision            it onto the seed      next pull
                   (likely_real), eyeballs                                  (is_valid_leaf         ingests
                   the rest (verify_match)                                  now passes)
```

1. **Surface.** Each produce, `curate` finds every product blocked only because its (already-in-Medusa)
   value is missing from the matched variety's set. It writes the suggestion to
   `catalog_source/backbone_additions/backbone_value_updates.csv` (human-readable audit) and into the
   review queue (`review_pending`, kind `backbone_leaf`). Each suggestion carries the variety, its
   `stone_type` (so a granite approval never grows a same-named quartzite), the attribute, the value, and
   a `verdict` (`likely_real` for exact/projection matches, else `verify_match`).

2. **Review.** The `:4200` admin serves the queue:
   - `GET  /config/v1/review/backbone` -- the pending suggestions (with `current_action` if decided between runs).
   - `POST /config/v1/review/backbone/approve_all` `{ "verdict"?: "likely_real" }` -- **bulk approve** the
     confident ones in one click (or all, if `verdict` is omitted). Only ever acts on undecided rows.
   - `PUT  /config/v1/review/backbone/<ref>` `{ "action": "approve" | "reject" }` -- one verdict.
     `<ref>` is `variety_norm|stone_type_norm|attribute|value_norm`.

3. **Overlay.** An `approve` writes an `approve` row into `backbone_leaf_decision`; a `reject` records
   "never propose again". `decisions_store.backbone_leaf_overlay()` returns only the approved additions.

4. **Apply.** `load_all` calls `Backbone.apply_leaf_overlay(...)`: it widens each variety's in-memory
   allowed set with its approved values (additive, idempotent, seed file untouched). The next time the
   pipeline runs, `is_valid_leaf` passes for those products and they emit.

5. **Release.** Run the **`republish`** stage. It re-runs the pipeline + catalog against the LAST scrape
   with no supplier re-fetch, so the products the approval just made valid flow into the ledger. Medusa
   ingests them on its next pull (Medusa's own schedule; the scraper never triggers it).

## Why `republish` exists

The blocked products are already scraped; releasing them must not re-scrape. But of the run stages,
`catalog` only re-consolidates already-emitted outputs (it does not re-run the reconcile/validate that
releases parked rows), while `scrape`/`all` re-run the pipeline but force a costly supplier fetch (HTTP +
de-watermark). `republish` is the missing cheap path: at the build level it is identical to `all` (build
never fetches -- that is produce's job), and produce simply skips the live scrape for it. So:

> **approve (bulk) -> run `republish` once (seconds, no re-scrape) -> Medusa's next pull lists them.**

`republish` is a run stage like the others, so it is also a dropdown entry on the `:4200` trigger.

## What is agnostic

Nothing here is per-variety or per-source. Any variety, any of the three leaf vocabularies, any current
or future feed inherits the loop with zero configuration. The suggestion generation, the overlay, and the
`republish` stage all operate on the canonical layer, so a new scraper gets backbone growth for free.

## Guarantees (locked by `tests/test_backbone_growth.py`)

- The committed seed JSON is byte-identical after growth (the base is never mutated).
- Growth is reversible (reject clears it; dropping the decision rows restores the pristine tree).
- Same-named varieties are disambiguated by `stone_type` (no fuzzy fallback).
- A decided suggestion drops off the queue on the next produce; bulk-approve never re-flips a decision.
- `republish` re-runs pipeline + catalog and never re-scrapes; `runner.STAGES` and `build.STAGES` cannot drift.
