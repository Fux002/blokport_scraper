# New Variant Review & Approval — Implementation Plan

Goal: an operator reviews the new variants a produce surfaces and decides **Mint / Reject /
Alias-to-existing** entirely from the :4200 admin UI — no ECS exec, no S3 poking — with decisions
persisted durably and automatically applied by the next produce. Aliasing must update the target
variety's alias list AND route the scraped product onto that variety.

Prerequisite: the zucchi clean cycle (reset -> produce -> pull -> reconcile) is green. This builds on
that flow; do not start until it proves out.

---

## 1. Where we are today (what already exists)

- `curate.py` classifies every gapped scraped name in strict order: REJECT junk -> RESOLVE to an
  existing variety (auto-alias, confident) -> MINT a new variety -> or HOLD the uncertain ones.
- `review/variants_to_confirm.csv` is the decision ledger: `confirm` = true (mint) / false (reject) /
  blank (pending). Read back at the START of every produce (`decisions.load_confirm_decisions`).
- `review/attributes_to_add.csv`: new colours/finishes/types the operator creates in Medusa, pastes
  the id, next run adopts.
- `state/rejected_varieties.csv` + `state/alias_writeback.csv`: the learned memory (never re-ask).

### The two gaps this plan closes
- **Gap A — no "alias to X" action.** The confirm file only does mint/reject. Declaring "this is
  really an alias of X" today means hand-editing the backbone JSON or `alias_writeback.csv` (and
  knowing X's variation id). No operator surface for it.
- **Gap B — the decision files are ephemeral + unreachable on ECS.** `state_dir` and `review_dir`
  resolve under `/app` (the image); only `/ledger` is EFS. So on ECS the decisions (a) vanish on any
  task restart — the "learning" resets — and (b) can't be edited without exec-ing into the container.

---

## 2. Architecture decisions (agree before building)

- **Persist decision state on EFS.** Env-drive `state_dir` / `review_dir` / `backbone_additions` to
  `/ledger/...` (same pattern as `BLOKPORT_LEDGER_PATH`). Single-host task -> the config server and the
  produce subprocess share the files safely; `decisions.py` already writes atomically.
- **Expose the decision files through the config server API**, edited in the :4200 admin. The UI
  already talks to that server over internal DNS.
- **Aliases are keyed by variety NAME, not Medusa id.** A pending variety has no Medusa id during the
  first cycle, so a name-keyed store works pre- and post-sync. The store feeds BOTH (a) the matcher's
  resolution surface (so the spelling routes to X) AND (b) X's emitted alias list (so X's variation
  payload changes -> re-serves dirty -> Medusa's copy updates on the next pull).
- **Persistence format: EFS CSVs first** (minimal; reuse `decisions.py` helpers). Ledger tables are a
  later refinement if the UI needs richer queries — not required to ship.

---

## 3. SCRAPER SIDE (this chat)

### Phase 1 — Durable decision state on EFS
- Env-drive `SETTINGS.paths.state_dir`, `review_dir`, and `catalog_source/backbone_additions` to an
  EFS root (e.g. `BLOKPORT_STATE_ROOT=/ledger`), defaulting to the repo root on a laptop.
- Create the dirs on boot (config + produce both).
- Verify: write a decision, restart the task, decision still there.
- Note: the produce reads decisions ONCE at curate start; an edit made during a run applies to the
  NEXT run (atomic writes prevent torn reads). Acceptable + document it.

### Phase 2 — The `alias_of` decision (Gap A)
- Extend the confirm-file schema: add `action` (`mint` | `reject` | `alias`) + `alias_of`. Keep
  backward compat (`confirm` true/false still maps to mint/reject).
- `decisions.py`: `load_confirm_decisions()` returns `{variant: {action, alias_of}}`. Add a durable,
  name-keyed `state/confirmed_aliases.csv` (`variety_name, alias_spelling`).
- `curate.py`: when `action == alias`, add the spelling to `confirmed_aliases[alias_of]` and SKIP the
  mint. Next produce: the spelling is an exact surface hit on X -> the product resolves to X's
  variation; X's variation payload gains the alias -> flips dirty -> re-serves.
- Wire `confirmed_aliases` into BOTH `curate.existing_surface` (resolution) AND `emit_catalog`'s
  variety alias column (payload). This is the "update the alias list + product lists as X" guarantee.
- Tests: proposed variant + `alias_of=X` -> next build resolves the product to X, mints nothing, X's
  aliases include the spelling, the review row self-clears.

### Phase 3 — Config server review API (Gap B, scraper half)
- `GET  /config/v1/review/variants` -> pending decisions enriched: `variant, stone_type,
  nearest_existing, model_prob, current_action`.
- `PUT  /config/v1/review/variants/<variant>` -> body `{action: mint|reject|alias, alias_of?}`.
- `GET  /config/v1/review/attributes` -> pending `attributes_to_add` rows.
- `PUT  /config/v1/review/attributes/<value>` -> body `{medusa_id}` (adopt).
- `GET  /config/v1/varieties` -> existing variety names (+ type) for the alias dropdown, read from the
  export / ledger.
- Reuse `decisions.py` read/write helpers; atomic writes; bearer-token auth (existing). Guard: reject
  edits while a run is in flight OR document that they apply next run (mirror the reset/run guard).
- Tests: pure `dispatch` tests per route (no sockets), same style as the existing config server tests.

### Phase 4 — Deploy + live verification
- Rebuild image, redeploy (current 1 vCPU / 8 GB task).
- Live E2E on ECS: produce -> `PUT` a mint, a reject, and an alias via the API -> re-produce ->
  confirm each applied (mint creates a variety, reject drops it forever, alias routes the product).

### Phase 5 — Images for minted variants (dependency, don't forget)
- Minted new variants are HELD out of the upload until they have an S3 image
  (`variants_held_no_image`). So "approve" isn't fully shipped until an image exists.
- Options: (a) wire `FAL_KEY` via the existing `produce_secret_arns` passthrough (same mechanism as
  the proxy) so the produce auto-generates the image; (b) a manual image-upload path in the admin.
  Decide with the user. Until then, approved variants sit imageless-held (not lost).

---

## 4. MEDUSA / OTHER CHAT SIDE

### Admin UI (:4200) — the operator surface
- **New-variants review screen**
  - `GET /config/v1/review/variants` -> table: name, type, nearest existing, model confidence.
  - Per row: `[Mint] [Reject] [Alias to ▼]` where the dropdown is `GET /config/v1/varieties`.
  - Submit -> `PUT /config/v1/review/variants/<variant>` with the chosen action (+ `alias_of`).
- **New-attributes screen**
  - `GET /config/v1/review/attributes` -> list (kind, value, count, suggestion).
  - `[Create in Medusa]` -> create the colour/finish/type in Medusa, capture its id ->
    `PUT /config/v1/review/attributes/<value> { medusa_id }`. (Or manual: operator creates + pastes.)

### Entity handling (mostly automatic via the existing sync)
- **Mint**: no special Medusa action — the next produce mints the variety (pending), the variation
  pull creates it in Medusa (once it has an image, per Phase 5).
- **Alias to X**: X's variation re-serves dirty with the updated alias list -> the pull updates X's
  aliases; the product attaches to X's variation. No manual Medusa step.
- **Reject**: nothing — it's never proposed or served again.

### Decision needed from Medusa
- Attribute creation: does the UI create the attribute in Medusa via API and auto-fill the id, or does
  the operator create it manually and paste? (Auto is nicer; manual is zero backend work.)

---

## 5. Contract to freeze jointly (before coding, like the sync contract)
- The review API shape: the five routes + payloads above. Freeze the field names.
- Attribute creation flow (auto-via-API vs manual paste).
- Image strategy for approved variants (FAL auto-gen vs manual upload).

---

## 6. Ownership split (nothing forgotten)

| # | Item | Owner |
|---|------|-------|
| 1 | EFS-persist state/review dirs (Phase 1) | Scraper |
| 2 | `alias_of` decision + name-keyed alias store + curate/emit wiring (Phase 2) | Scraper |
| 3 | Config review API: variants, attributes, varieties (Phase 3) | Scraper |
| 4 | Rebuild + deploy + live decision round-trip (Phase 4) | Scraper |
| 5 | FAL_KEY wiring OR manual image path for minted variants (Phase 5) | Scraper (infra) + user decision |
| 6 | Admin UI: new-variants review screen (mint/reject/alias + dropdown) | Medusa/other chat |
| 7 | Admin UI: new-attributes screen (+ create-in-Medusa or paste) | Medusa/other chat |
| 8 | Attribute creation in Medusa (API vs manual) | Medusa/other chat |
| 9 | Freeze the review API contract | Joint |
| 10 | End-to-end test: produce -> review -> re-produce -> pull -> reconcile | Joint |

## 7. Rollout order
1. Freeze the API contract (item 9).
2. Scraper Phase 1-3 + deploy (items 1-4).
3. Medusa builds the UI against the live endpoints (items 6-7-8).
4. Decide + wire images (item 5).
5. Joint E2E (item 10): produce -> mint/reject/alias in the UI -> re-produce -> pull -> reconcile.

## 8. Open decisions for the user
- Attribute creation: auto-create in Medusa via API, or manual create + paste id?
- Approved-variant images: wire FAL_KEY for auto-gen, or a manual upload path?
- Persistence: EFS CSVs (recommended, ship fast) vs ledger tables (later, if the UI wants structure)?

---

# PART II — Product closure: every INGESTED row finds a home

Companion goal to the review workflow. This is the broader invariant the review workflow serves.

## 9. The invariant (and its precise meaning)

**Every ingested product is accounted for**: it is in exactly one bucket -- **placed**, **held with a
named reason + a resolution path**, or **explicitly parked as not-sellable-because-X**. The guarantee
is **zero silent / unexplained drops** -- NOT zero rejects (some rows are genuine junk: a 0cm-thickness
typo, a bare supplier code). "Closed" = nothing vanishes unexplained; everything has a bucket and, if
not placed, an action.

### Not "scrape" -- ANY source (the key generalization)
Closure is defined at the **canonical layer**, not at the scrape. The **adapter** is the only
source-specific stage (fetcher + adapter map + config; NO `if source ==` in shared stages -- the source
isolation invariant). A web scrape, an ERP direct feed, a REST API, a CSV upload -- all normalise to the
SAME canonical rows, then flow through the same match -> derive -> emit -> catalog -> ledger. So "every
scraped product finds a home" is really **"every ingested product finds a home,"** and every closure
surface below works identically for every source, present and future. Adding an ERP/API source adds a
fetcher + adapter only; closure, review, and the reconcile scorecard are defined ONCE and apply to all.

## 10. Current state (accounting exists, but dead-ends)
`validate.py` already sorts every row into **emit** (placed) / **review_only** (flagged but shippable) /
**rejects** (hard-fail), and rejects are written out (`emit.write_rejects_csv`). So nothing is truly
lost -- but the rejects file is a dead-end, not a closeable, drive-to-zero loop.

The "no home" bucket = the hard rejects, by reason: `tree_gap` (no variety/type), `dimension_invalid`,
`no_image`, `category_invalid`/`handle` (structural).

## 11. The closure work

### Step 0 -- MEASURE (do first): per-reason reject histogram
Today we only get the total (`rejects: 582`). Instrument `validate.py` to count rejects **by reason**,
logged + surfaced (ledger/status + a per-source breakdown), so closure is measurable and we know which
bucket is biggest. Small change; makes the whole goal quantified instead of vibes.

### Step 1 -- a resolution surface per reason (admin)
- `tree_gap` -> **mint / alias** (Part I) + **type assignment** for uncovered variations
  (`tree_uncovered_variations.csv` `assign_type` already exists as a file -> needs an admin surface).
- `dimension_invalid` -> a **dimension override / correction** surface (likely a big slice -- the zucchi
  run flagged 1,886 dimension issues; some emit flagged, some hard-reject).
- `no_image` -> FAL auto-gen or manual upload (Part I, Phase 5).
- structural (`category`/`handle`) -> rare; a fix path or explicit park.

### Step 2 -- the reconcile scorecard (the "closed" guarantee)
A product-level view, **per source and overall**: `ingested = placed + held(by bucket) +
rejected(by reason)`, buckets sum to the ingest count, every non-placed bucket has an operator action,
and you can drive non-placed toward zero (or explicitly park with a reason). This is the visible proof
that the system is closed.

## 12. Ownership (extends the section 6 table)
| # | Item | Owner |
|---|------|-------|
| 11 | Per-reason reject histogram (step 0) | Scraper |
| 12 | Type-assignment surface (API + UI) | Scraper (API) + Medusa (UI) |
| 13 | Dimension-correction override (mechanism + API + UI) | Scraper + Medusa |
| 14 | Product reconcile scorecard (status endpoint + UI) | Scraper (endpoint) + Medusa (UI) |
| 15 | Keep closure source-agnostic as ERP/API/CSV sources land | Scraper (adapters only) |

## 13. Rollout (after Part I)
0. Reject histogram (measure) -> pick the biggest bucket.
1. Type assignment (closes most of `tree_gap` alongside mint/alias).
2. Dimension correction (the likely-largest remaining bucket).
3. Image resolution.
4. Reconcile scorecard -> drive non-placed to zero.
