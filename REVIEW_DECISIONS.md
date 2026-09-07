# Review decisions: one statement, two lists

The operator never chooses "mint", "alias" or "set origin". They state what a vendor's product **is**:
name, stone type, colour, origin. The backend derives the outcome from how that statement compares with
what exists. Valid combinations, attribute ids and textures are outside this model.

## The statement

`PUT /config/v1/review/decide`

```json
{"source": "marenostone", "scraped": "Amazon Green Granite",
 "name": "Golden Lightning", "type": "Granite", "color": "Green", "origin": "IR", "widen": false}
```

`scraped` is the spelling the decision is keyed on (a card's `scraped`, or its whole `spellings` list).
`name` and `type` are required; `color`, `origin`, `widen` are optional. `type` must be a Medusa stone
type, `origin` an ISO-3166 country (code or name). Nothing is stored unless everything validates.

| name + type | result | stored for the vendor |
|---|---|---|
| an existing variety | `bound`: the product binds to it on the next produce | scoped alias, plus the origin as a supplier override |
| no such variety | `minted`: the variety is created with these attributes | mint (global variety), scoped alias when the name differs from the spelling, plus the origin |
| a retired variety | `409`, nothing stored | un-retire it first |

A statement replaces the vendor's previous one for that spelling. It never touches another vendor, and it
never changes the variety's documented origins unless `widen` is true, which adds the origin to the stone's
list for every vendor's origin gate.

`DELETE /config/v1/review/decide` with `{"source", "scraped"}` removes exactly what the statement stored;
the next produce resolves the product on its own again.

The previous per-action bodies on `/review/variants/<v>` (`mint`, `alias`, `reject`) and the origin-card
bodies on `/review/origins/<ref>` keep working. `reject` stays an explicit action: "not a variety" is not
a statement about fields.

## Pending: a decision is required

`GET /config/v1/review/variants` (and, until the UI switches, `GET /config/v1/review/origins`).

Every card carries `src`, `scraped`, `spellings` (every listing behind the card), `variant` (the cleaned
identity), `stone_type`, `color`, `reason`, `nearest_existing`, `src_url`, `image`, `description`. The
reason says why the card exists; it never limits what may be changed.

## Resolved: no decision required

`GET /config/v1/review/resolved?source=<vendor>&decided=true|false`

One row per product of the last produce: `scraped`, `name`, `variation_key`, `stone_type`, `color`,
`origin`, `resolved_by` (`variety`: the matcher tier, `type`: the type authority, `origin`: the origin
rung), and `decision`, the operator's standing statement for that vendor and spelling, or null. The next
produce reproduces exactly these rows; a row with a `decision` shows the last outcome until then. The same
statement adjusts a row; `DELETE` clears it.

## What a decision touches

- **Vendor alias** (`scoped_alias`): vendor + spelling -> variety. Applied by the matcher's override tier,
  before any alias or similarity lookup, for that vendor only.
- **Vendor origin** (`origin_decision`): vendor + variety + type -> country. Read by derive at its top curated
  rung, above the origin map and the vendor gate, for that vendor's products only.
- **Mint** (`variety_decision`): spelling -> the variety to create, with the vendor that asked for it. The
  variety itself is global by nature; only the spelling's binding is scoped.
- **Documented origins** (`variety_origin`, only with `widen`): the stone's country list, every vendor.

All of it lives in `config.db`, snapshotted every five minutes and restored on boot. Republish, redeploy,
soft and hard reset keep it. Only the pristine factory reset clears it.
