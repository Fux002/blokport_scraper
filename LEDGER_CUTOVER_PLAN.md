# Ledger cutover: source the catalog's variation ids from the ledger, not the fetched export

## Why (the bug this kills by construction)

`variants_export.csv` (the Medusa export in `from_medusa/<env>/`) is the id source the catalog reads:
`match_variation` assigns each scraped product a `variation_id` from it, `tree_build` builds the
combination set from it, and `emit_catalog` writes `1_variants` from it. That file is published by Blokport
and fetched by the scraper (`produce._fetch_inputs`).

When Medusa rebuilds variations on a cold start (deletes + recreates them with **new** ids), the acks push
the new ids into the **ledger** — but nobody re-publishes `variants_export.csv`. So the export goes stale
and the whole catalog emits **dead variation ids** (this is exactly the 2026-07-08 incident: the ledger held
the current `01KWW2*` ids, but the catalog shipped the 06-22 `01KVNZ*` ids from the stale export, and
Medusa rejected every combination on import).

The `attributes.csv` half of this is already immune — Blokport auto-publishes it on every vocab change. The
variation-id half is not, because the pull-model contract assumed "ids flow back via the ack" (true for the
**ledger**, false for the **export CSV** the catalog reads). This plan closes that gap **by construction**:
the catalog reads the acked ledger, so a rebuild can never stale it and no export re-publish is ever needed.

Non-goal: this is **NOT** a storage migration (SQLite stays SQLite; no Postgres). It only changes the *data
source* the catalog reads for variation ids.

## What already exists (so this is wiring + gating, not new machinery)

- `ledger/render.py`: `_dump_variations_export(ledger, path)` renders the ledger's `variation` table into the
  exact `variants_export.csv` shape (`Id, Key, Name, Image, Aliases, Volume`); `_dump_attributes` likewise
  for `attributes.csv`. `render_combinations` / `render_variants_full` / `render_products` already build the
  three `to_upload` artifacts from the ledger by feeding those dumps into the **same** builders the export
  path uses (`tree_build.build_combinations` etc.) — so identical inputs in, identical outputs out.
- `ledger/verify.py`: the **Phase-1 cutover gate**. Renders each artifact from the ledger and compares to the
  CSV the export path produced (variants/products by content-set, combinations byte-identical). Exits non-zero
  on any mismatch, so it can gate a build.
- The ledger is kept current by the pull acks (confirmed live: 24,693 variations carry current `01KWW2*`
  medusa_ids) and seeded at bootstrap from the export (`ledger/bootstrap.py`).

The only thing missing is that the **production** produce still reads the fetched `from_medusa` CSVs instead
of rendering them from the ledger.

## Design

One seam, flag-gated, verify-fenced. In `produce`, after `_fetch_inputs`, when the ledger source is enabled:
regenerate the local `variants_export.csv` (and `attributes.csv`) **from the ledger** via the existing
`_dump_variations_export` / `_dump_attributes`, overwriting the fetched copy. Then the entire existing
pipeline (`match_variation`, `tree_build`, `emit_catalog`) runs unchanged and reads the ledger's current ids.

This is the smallest correct cutover: it does not touch any stage's logic, only the *provenance* of the id
map. It reuses the render path that `verify.py` already proves equivalent, so the gate is already written.

```
# produce, sketch (behind BLOKPORT_LEDGER_SOURCE)
_fetch_inputs()                       # still pulls attributes for the initial/bootstrap case
if ledger_source_enabled():
    with Ledger.open(...) as lg:
        render._dump_variations_export(lg, SETTINGS.paths.export_file)   # ledger -> variants_export.csv
        render._dump_attributes(lg, SETTINGS.paths.attributes_csv)       # ledger -> attributes.csv
# ... unchanged: scrape -> pipeline -> catalog, now reading the ledger-sourced export
```

Bootstrap stays as-is (empty ledger is still seeded from a real export once); steady state flips to
ledger-sourced. The circular "ledger seeded from export, export rendered from ledger" is resolved by order:
seed once at bootstrap, render-from-ledger every produce thereafter.

## Phases

- **Phase 0 — shadow (done).** Renderers + `verify.py` exist; ledger current via acks. No behaviour change.
- **Phase 1 — dual-run + gate (this plan).** Add `BLOKPORT_LEDGER_SOURCE` (default off). When on, render the
  export/attributes from the ledger before the pipeline. Run `verify.py` at the end of the produce as a
  non-fatal check first (log mismatches), then as a **hard gate** once a few real produces show zero drift.
- **Phase 2 — flip default + retire the export fetch.** Default `BLOKPORT_LEDGER_SOURCE` on. `_fetch_inputs`
  no longer needs `variants_export.csv` (the ledger is the source); Blokport **stops publishing it** and can
  delete the assumed-but-never-built variants-export publisher entirely. `attributes.csv` fetch stays (it's
  Blokport's authoritative vocab, already auto-published) unless/until it too is proven ledger-equivalent.
- **Phase 3 (optional) — full ledger-rendered catalog.** Render `1_variants` / `3_products` /
  `2_valid_combinations` directly from the ledger (`render_*`) instead of via the scrape+export path. This
  needs write-through (`BLOKPORT_LEDGER_WRITETHROUGH`) on so the ledger holds products/combinations, and is a
  larger change — **not required** to fix the id-staleness (Phase 1/2 already do that). List separately.

## Risks + safety

- **Ledger completeness.** The cutover trusts the ledger's `variation` table as the id truth. Guard: `verify.py`
  as a hard gate (Phase 1) catches any variation the export has but the ledger lacks, before it can ship. Also
  assert the ledger variation count is within tolerance of the last export before rendering, else fall back to
  the fetched export and log loudly (no silent divergence).
- **Ack lag.** If Medusa rebuilds but the acks have not landed yet, the ledger is briefly behind. Same failure
  mode as today (stale ids) but self-heals on the next produce after the acks arrive — and the consistency
  gate already holds new/unsynced variations, so nothing dead ships.
- **Reversibility.** Flag-gated and default-off through Phase 1; a bad render falls back to the fetched export.

## Definition of done

- `BLOKPORT_LEDGER_SOURCE=on` produce renders the export from the ledger, the catalog emits **current** ids
  without any `variants_export.csv` re-publish, and `verify.py` passes as a hard gate.
- A simulated rebuild (bump the ledger's medusa_ids, do NOT touch the export) still produces current-id
  combinations — proving the staleness is gone by construction.
- Blokport confirms it no longer needs to publish `variants_export.csv` (Phase 2).

## Explicitly out of scope

SQLite -> Postgres (not this). Attribute-vocab sourcing (already auto-published by Blokport). The Phase-3
full ledger-rendered catalog (separate, write-through-dependent).
