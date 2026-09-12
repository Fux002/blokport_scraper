# Review decisions: one statement, two lists

> Card refs: a variety card's `ref` is `<normalised name>|<normalised type>` (a type-less card: the bare name); an origin card's is `<source>|<variety>|<type>`. Treat refs as opaque keys; the reject `PUT /config/v1/review/variants/<ref>` accepts a card ref or a bare name and keys the reject on the name.

The operator never chooses "mint", "alias" or "set origin". They state what a vendor's product **is**:
name, stone type, colour, origin. The backend derives the outcome from how that statement compares with
what exists. Valid combinations, attribute ids and textures are outside this model.

## The statement

`PUT /config/v1/review/decide`

```json
{"source": "marenostone", "scraped": "Amazon Green Granite",
 "name": "Golden Lightning", "type": "Granite", "color": "Green", "origin": "IR", "widen": false}
```

The listings a statement applies to are the card's `listings` (`[{"source", "scraped"}]`, one per vendor
and spelling behind the card); send them as `listings`. `source` + `scraped` (a spelling or a list) is the
single-vendor form. `name` and `type` are required; `color`, `origin`, `widen` are optional. `type` must be
a Medusa stone type, `origin` an ISO-3166 country (code or name). Nothing is stored unless everything
validates. The response lists each listing with its `result`.

| name + type | result | stored for the vendor |
|---|---|---|
| an existing variety | `bound`: the product binds to it on the next produce | scoped alias, plus the origin as a supplier override |
| no such variety, spelling undefined | `minted`, `level: global`: the variety is created and the spelling MEANS it for every vendor | global mint; a renamed spelling becomes the variety's alias for everyone, plus the origin |
| no such variety, spelling already means another stone | `minted`, `level: vendor`: the variety is created for this vendor only | vendor mint + scoped alias (other vendors keep the spelling's meaning), plus the origin |
| a retired variety | `409`, nothing stored | un-retire it first |

A statement replaces the vendor's previous one for that spelling. It never touches another vendor, and it
never changes the variety's documented origins unless `widen` is true, which adds the origin to the stone's
list for every vendor's origin gate.

**Two levels.** A spelling means ONE variety for every vendor: the global mint on it, or the existing variety
it already resolves to (by name or alias, on the spelling and on its cleaned form). The first mint on an
undefined spelling defines that meaning ("Tropical Green IS Tropical Green Bahia", for everyone). A later
statement that names the same stone stores nothing. One that names a DIFFERENT new stone is that vendor's own
mint, kept beside the global meaning and bound by its vendor alias; no other vendor moves. `DELETE` on a
vendor-level mint removes it and the vendor falls back to the global meaning; a global mint is cleared only by
un-minting the variety.

`DELETE /config/v1/review/decide` with `{"source", "scraped"}` removes exactly what the statement stored;
the next produce resolves the product on its own again.

`reject` stays the one explicit action, `PUT /review/variants/<v>` with `{"action": "reject"}`: "not a
variety" is not a statement about fields. The previous `mint` and `alias` bodies and the
`/review/origins` routes were removed once the UI switched (2026-09-08); a `mint` or `alias` body is
answered with 400 pointing at `/review/decide`.

## Pending: a decision is required

`GET /config/v1/review/variants` is the one list: the variety cards plus the origin confirmations in the
same card shape.

Every card carries `kind`, `src`, `scraped`, `listings` (`[{source, scraped}]`, every listing behind the
card, what a statement is sent with), `spellings` (the same, flat, for display), `variant` (the
cleaned identity), `stone_type`, `color`, `origin` (origin cards: the vendor's declared country, prefilled),
`reason`, `nearest_existing`, `src_url`, `image`, `description`. The reason says why the card exists; it
never limits what may be stated.

| kind | why the card exists | prefilled |
|---|---|---|
| `new` | the name exists under no type | name, type, colour as read |
| `similar` | close to an existing variety, not the same spelling | name, type; the nearest in evidence |
| `collision` | one spelling owned by several same-type varieties | name, type; the owners in the reason |
| `no_type` | no stone type could be read, or the name and the site's tag name two real stones | name; the existing types in the reason |
| `new_type` | the name exists, but under other types than the scrape's | name, the scrape's type; existing types in the reason |
| `alias_target` | an alias decision points at a multi-type name | name; the types in the reason |
| `code` | the name looks like a supplier code | name |
| `retired` | the variety was retired; un-retire or leave | name, type |
| `origin` | bound fine, the vendor's country is not in the stone's documented list | name, type, the vendor's origin |
| `decision_gap` | a decision the last produce did NOT honour (mint whose variety does not exist, alias whose listings bound elsewhere, origin not at the operator rung, reject whose card remains); `current_action` names the decision | name, type, origin as decided; restate it, or clear it with DELETE |

A name whose title and site tag name two different stones that both exist (Azul White: onyx and quartzite)
is no longer typed from the title: the type stays open, the matcher's origin rung binds the stone the
vendor's country corroborates, and without that evidence the product holds as `no_type`.

## Resolved: no decision required

`GET /config/v1/review/resolved?source=<vendor>&decided=true|false`

One row per product the last produce BOUND (an unbound product is a pending card; a product is never in both lists): `scraped`, `name`, `variation_key`, `stone_type`, `color`,
`origin`, `resolved_by` (`variety`: the matcher tier, `type`: the type authority, `origin`: the origin
rung), and `decision`, the operator's standing statement for that vendor and spelling, or null. The next
produce reproduces exactly these rows; a row with a `decision` shows the last outcome until then. The same
statement adjusts a row; `DELETE` clears it.

## What a decision touches

- **Vendor alias** (`scoped_alias`): vendor + spelling -> variety. Applied by the matcher's override tier,
  before any alias or similarity lookup, for that vendor only.
- **Vendor origin** (`origin_decision`): vendor + variety + type -> country. Read by derive at its top curated
  rung, above the origin map and the vendor gate, for that vendor's products only.
- **Mint** (`variety_decision`, keyed vendor + spelling): the global level (vendor `''`, `asked_by` = who
  stated it) is what the spelling means for everyone; a vendor level is that vendor's own stone beside it.
  Every reader resolves a listing vendor-first, then global.
- **Documented origins**: `widen` is recorded on the decision (`origin_decision.widen`) and at load the
  country is ADDED to the stone's documented list, a union with what the map already documents, never a
  replacement. `variety_origin` is the admin's explicit list edit only (it sets the list).

All of it lives in `config.db`, snapshotted every five minutes and restored on boot. Republish, redeploy,
soft and hard reset keep it. Only the pristine factory reset clears it.
