# New-Variant Review API - Frozen Contract (scraper -> :4200 admin)

The scraper now exposes the new-variant review queue and the operator's decisions over the existing
config server (`:8724`, same bearer token as every other config route, `BLOKPORT_CONFIG_TOKEN`). This is
the contract the Blokport admin UI consumes. It is stable; changes go through the same freeze-first
process as the sync contract.

Scope of THIS delivery (scraper side, shipped):
- Durable decisions in config.db (snapshotted + restored, so they survive an ECS task restart).
- Three actions per uncertain variety: `mint`, `reject`, `alias` (alias-to-existing).
- New-attribute id capture.
- The endpoints below.

What Blokport builds: the two admin screens (variants review, attributes) + the alias-to dropdown.

---

## Model (what the operator is deciding)

A produce run classifies every scraped variety name; the ones it cannot decide confidently are SURFACED
as `pending`. The operator resolves each, and the **NEXT produce applies it** (decisions are read once at
the start of curation). So the flow is: produce -> review (these endpoints) -> re-produce (`POST
/config/v1/run`) -> pull. A decision without a re-produce does nothing; a re-pull without a decision does
nothing.

Actions:
- `mint`  -> it is a real new variety; the next produce mints it.
- `reject`-> never propose it again (durable "no" memory).
- `alias` -> it is really a spelling of an existing variety `alias_of`; the product routes onto that
             variety and the spelling is added to that variety's alias list (which re-serves it dirty, so
             Medusa's copy updates on the next pull). `alias_of` MUST be an existing variety name (the API
             rejects an unknown target with 400 - see `GET /varieties` for the valid set).

---

## Endpoints

All paths are under `/config/v1`. Auth: `Authorization: Bearer <BLOKPORT_CONFIG_TOKEN>`. Bodies + responses
are JSON.

### 1. GET /config/v1/review/variants
The pending varieties awaiting a decision.

```json
{
  "variants": [
    {
      "variant": "Zucchi Blue X",       // the scraped spelling (this is the <variant> path key below)
      "stone_type": "granite",
      "color": "blue",
      "nearest_existing": "Azul X",      // the closest existing variety the matcher found (may be blank)
      "score": 0.72,                     // fuzzy score (informational)
      "model_prob": 0.61,                // alias-model probability (informational)
      "sources": ["zucchi"],             // which source(s) proposed this spelling (provenance; may be [])
      "current_action": null,            // the decision already recorded, if any: mint|reject|alias|null
      "current_alias_of": null           // the alias target, if current_action == "alias"
    }
  ]
}
```
`current_action` lets the UI show a decision made between runs (applied on the next produce). A decided
item stays in the queue until the next produce re-surfaces the (now smaller) pending set.

### 2. PUT /config/v1/review/variants/<variant>
Record one decision. `<variant>` is the exact `variant` string from the GET.

Body:
```json
{ "action": "mint" | "reject" | "alias", "alias_of": "Bianco Carrara" }
```
`alias_of` is required only when `action == "alias"`.

Responses:
- `200 {"variant": "...", "action": "...", "alias_of": "..."}` - recorded.
- `400 {"error": "..."}` - bad action, alias with no/unknown target, or a non-object body.

### 3. GET /config/v1/review/attributes
New colour/finish/type/quality VALUES that need a Medusa id before the variety using them can complete.

```json
{
  "attributes": [
    { "kind": "finish", "value": "Leathered", "count": 12,
      "suggested_value": "", "sources": [] }
  ]
}
```

### 4. PUT /config/v1/review/attributes/<value>
Record the Medusa id the operator created for a value. `<value>` is the `value` from the GET.

Body:
```json
{ "kind": "finish", "medusa_id": "pcol_01J..." }
```
Responses: `200` on success; `400` if `kind` or `medusa_id` is missing.

### 5. GET /config/v1/varieties
The existing variety names for the alias-to dropdown. This is EXACTLY the set an `alias_of` may target
(validated by endpoint 2), so the dropdown never offers an invalid target.

```json
{ "varieties": [ { "name": "Bianco Carrara", "stone_type": "Marble" }, ... ] }
```

---

## UI notes (Blokport side)

- **Variants review screen**: table from endpoint 1 (name, type, nearest existing, confidence, sources).
  Per row: `[Mint] [Reject] [Alias to v]` where the dropdown is endpoint 5; submit via endpoint 2. Show
  `current_action` as the row's current state.
- **Attributes screen**: table from endpoint 3; per row `[Create in Medusa]` (or paste) -> capture the id
  -> endpoint 4.
- **Apply**: after deciding, trigger `POST /config/v1/run` (the existing produce button) to apply, then
  pull. Optionally show a pending-count badge from endpoint 1.
- Nothing in Medusa needs a bespoke consumer for review: `mint` flows through the existing variation
  pull, `alias` re-serves the target variety dirty (its alias list changed), `reject` simply never
  appears. No new sync lane.

## Guarantees / edges the scraper side handles
- Decisions are durable (config.db snapshot), so they survive a task restart - no lost learning.
- An `alias_of` that is not a real variety is refused at decision time (400), never a silent no-op.
- The pending queue is rewritten each produce from the current uncertain set; a resolved item drops off.
- Provenance (`sources`) is carried so a newly-added source's batch can be reviewed as a group. (The
  producing side populates it as source coverage lands; absent -> `[]`.)
