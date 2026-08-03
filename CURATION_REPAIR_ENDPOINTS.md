# Curation-repair endpoints (retire the `{pristine}` factory reset in prod)

Three incremental, prod-safe repair endpoints that fix the last scraper-side states that previously
required a full factory reset. All are **idempotent** and return a **scoped summary** of what they acted on.

**Two planes** (matching the existing split): A and B are **config plane** (`/config/v1/*`, bearer
`BLOKPORT_CONFIG_TOKEN`, via `configFetch`). C is **sync plane** (`/sync/v1/*`, bearer `BLOKPORT_SYNC_TOKEN`,
via the scraper-sync module client) — it sits next to `requeue` and reads from the same `/sync/v1/failures`
list, because it is the exact opposite of requeue on the same dead-letter object.

| State that used to need `{pristine}` | Plane | Endpoint | Blokport action |
|---|---|---|---|
| 1. Curation base-file corruption / drift | config | `POST /config/v1/curation/rebuild` | "Rebuild curation" (Maintenance) |
| 2. Duplicate-variety tombstone (false positive) | config | `POST /config/v1/variations/<key>/not_a_duplicate` | "Not a duplicate" on a tombstoned variety |
| 3. Dead-letter that structurally re-rejects | **sync** | `POST /sync/v1/abandon` | "Abandon" per-row in the Sync-failures table |

---

## A. Global curation rebuild — `POST /config/v1/curation/rebuild`

Reseeds the base FILE from the committed pristine seed (fixes base-file corruption/drift), then dispatches
a **catalog re-derive** so every source is re-curated from the clean base — **without** a factory reset's
ledger wipe, image wipe, or re-scrape.

**Global, not per-source, by design.** Curation is canonical: a variety belongs to the catalog, not to a
source (several sources can scrape the same variety), so there is no per-source curation slice to rebuild.
A per-source *re-curation* already exists (a scoped `POST /config/v1/run {sources, stage:"catalog"}`); this
endpoint rebuilds the **shared base** that a scoped run reads but never regenerates.

**Body:** none.

**Response 200:**
```json
{
  "rebuilt": true,
  "base_reseed": { "reseeded": true, "...": "..." },
  "rederive": { "run_id": "…", "sources": ["marenostone","polonine","varsha","zucchi"], "...": "…" },
  "rederive_status": 202,
  "scope": "global",
  "pristine_retired": true,
  "note": "base reseeded from the committed seed; catalog re-derive dispatched (watch rederive.run_id)…"
}
```
Show the operator: base reseeded ✓, sources being re-curated (`rederive.sources`), and a link to the run
(`rederive.run_id`, pollable at `GET /config/v1/run/<run_id>`).

**Status codes:** `200` dispatched · `409` a produce/reset/rebuild is already in flight, **or a pull is
mid-serve** · `409` (with `rebuilt:false`) no committed pristine seed to rebuild from (local dev only).

**Guard ownership (the one race worth blocking — a global re-derive vs an in-flight pull):**
- **Scraper owns** serialization against produces/resets/other rebuilds **and** best-effort refusal (`409`)
  if it detects an active pull lease. It takes the exclusive lock before touching the base file.
- **Blokport owns** gating the button on "no pull running" — only Medusa knows when its own pull is active,
  and the scraper cannot pause an external pull. Please keep that gate; the scraper `409` is the backstop.

---

## B. "Not a duplicate" — `POST /config/v1/variations/<key>/not_a_duplicate`

Cancels a dedup/reconcile tombstone that would delete a variety the operator confirms is distinct, and
records a **durable protection** so a future seed-reconcile never re-drops it. Restores the variety to
serving if it was held `retiring`.

**Body:** none. `<key>` is the variation Key.

**Response 200:**
```json
{ "key": "slab_marble_aqua_blue_…", "tombstone_cleared": 1, "restored": true, "known": true, "protected": true }
```
`tombstone_cleared` = pending tombstones dropped (0 on a repeat — idempotent). `protected: true` = the
durable "not a duplicate" verdict is recorded.

**Status codes:** `200` acted (or idempotent no-op) · `404` unknown key **and** no pending tombstone to
cancel · `409` a pull is mid-serve.

Sibling ops on the same path: `POST …/retire` (remove a variety), `POST …/un_retire` (undo a retire).
`not_a_duplicate` differs from `un_retire`: it does not re-enable re-minting, and it records the durable
protection.

---

## C. Abandon a dead-letter — `POST /sync/v1/abandon` (SYNC plane)

**Plane: sync** (`BLOKPORT_SYNC_TOKEN`, the scraper-sync module client — add an `abandon()` method to
`ScraperSyncClient` next to `requeue`). It is the **exact opposite of `requeue`** and keys off the **same
`{type, external_id}`** a `GET /sync/v1/failures` row already carries — so a per-row "Abandon" button passes
the row's identifier straight through. **Same object, same plane, no id threading needed.**

Drops one structurally-re-rejecting dead-letter to a **terminal** marker (`abandoned_at`): it stops
re-serving, **Requeue no longer resurrects it**, and it stays **auditable** — no blind delete. A later
`reset` is the un-abandon escape hatch (re-serves it for one more attempt). Serve-safe (touches only a
non-served dead-letter row), so — like `requeue` — it takes no in-flight-pull lock.

**Body:**
```json
{ "type": "variations" | "products" | "removed", "external_id": "<variation Key | product SKU | tombstone id>" }
```

**Response 200:**
```json
{ "type": "variations", "external_id": "slab_marble_bad_…", "abandoned": true, "was": "gap_held" }
```
Idempotent repeat: `{ "abandoned": true, "already": true }`.

**Status codes:** `200` abandoned (or already) · `400` missing body or unknown `type` · `404` unknown
`external_id` · `409` the id is not actually dead-lettered (still live/serving — refused, `error` explains).

**Rendering the abandoned state:** `GET /sync/v1/failures` now returns a `state` and an `abandoned` flag per
item (backward compatible — new fields alongside the existing `type`/`external_id`/`attempts`/`error`):
```json
{ "type":"variations", "external_id":"…", "state":"abandoned", "abandoned":true,
  "attempts":5, "error":"…", "updated_at":"…" }
```
Active dead-letters read `state:"gap_held"`/`"dead"` with `abandoned:false`; abandoned ones read
`state:"abandoned"` with `abandoned:true`. Style them distinctly in the Sync-failures view.

---

## Notes
- All three are **safe to call twice** and report what they acted on.
- A `pristine` factory reset still clears these overlays (protection verdicts, abandoned markers) for a true
  seed-only cold start — so `{pristine}` remains the nuclear option, just no longer *required* in prod.
- Combined with Blokport's graceful-shutdown / auto-resume, production recovers from every state
  incrementally; a factory reset is never needed to recover.
