# Archived and internal values and varieties (VE2)

Medusa gains two facts the scraper must respect: an attribute value (a type, colour, finish, quality, or
category) or a variety can be **archived**, and an attribute value can be **internal**. Neither is a value
the catalog may hand to buyers any more. This document is the end-to-end contract: what the two facts mean,
how Blokport publishes them, how the scraper freezes on them, and the guarantee that nothing changes until
Blokport turns them on.

One rule runs through every stage: a restricted value is never **assigned** into new output, but a product
or variety that **already carries** it keeps flowing. The scraper freezes forward, never rewrites the past.

## 1. The two facts

- **archived**: the value or variety is retired in Medusa. It must not be assigned to anything new. Existing
  products that carry it stay listed (Blokport, not the scraper, decides their fate).
- **internal**: an attribute value Medusa keeps for its own use and never exposes. Same treatment as
  archived for the scraper's purposes: not assignable.

The single predicate is **assignable = `status == "active"` and not `internal`** (`AttributeValue.assignable`
in `stone_pipeline/reference/medusa_files.py`). A **variety** has only a status, so its predicate is
`archived` (`ExportVariation.archived == (status == "archived")`).

## 2. The input contract (what Blokport publishes)

Two Medusa exports gain columns:

| file | new columns | read as |
|---|---|---|
| `from_medusa/<env>/attributes.csv` | `status` (`active`\|`archived`), `internal` (`true`\|`false`) | per value; `assignable = active and not internal` |
| Medusa's `variants_export.csv` | `status` (`active`\|`archived`) | per variety; `archived = status == "archived"` |

The readers are tolerant (`read_attributes`, `read_variants_export`). While a column is absent, every value
reads **active and not internal** and every variety reads **active**, so the feature is dormant. Only the
scraper's own base file (`variants_export_base.csv`) never carries the status column; it is always read
tolerant.

## 3. Rollout: expand, publish, flip

The change ships in three steps so prod is never caught between shapes:

1. **Expand (done, this release).** Every stage reads the columns tolerantly and behaves identically whether
   or not they are present. Nothing is restricted yet.
2. **Blokport publishes.** Medusa's prod release adds the columns to both exports and starts marking values
   and varieties archived or internal.
3. **Flip (step C, after Blokport's prod release).** Two flags in `stone_pipeline/config/settings.py` flip to
   `True`:
   - `ATTRIBUTES_STATUS_REQUIRED` -- a missing status/internal column on the real `attributes.csv` download
     then fails loud instead of defaulting to active.
   - `VARIANTS_EXPORT_STATUS_REQUIRED` -- the same for the variants export's status column.

   The requirement is path-aware (`attributes_status_required` / `variants_status_required`): it binds only
   the real Medusa download, never an internal ledger dump of the same shape, so the flip touches exactly the
   two files Blokport publishes.

## 4. The ledger archived mirror and the lane freeze

The ledger `variation` table carries an `archived` column, mirrored from the export each produce
(`stone_pipeline/config/varieties.py`, `ledger/render.py`). It is a write-through mirror of Medusa's truth,
not an independent source.

An archived variety's sync lane is **frozen**: the scraper does not re-push it, and the lane reports it
frozen rather than dirty. A republish of an unchanged catalog therefore writes a manifest byte-identical to
the previous run and leaves the ledger with zero dirty rows (the dormancy proof used on dev for PR3-PR5).

## 5. value_frozen: holding scraped products on a restricted value (validate)

The `value_frozen` rule in `stone_pipeline/stages/validate.py` holds a scraped product whose resolved value
is restricted, across **every** dimension a product carries: `color`, `finish`, `quality`, `type`,
`category`, and `variation_key` (the variety itself, archived). A held product goes to the review queue; it
never emits with a restricted value.

The exemption mirrors reality: a product is **not** held on a value it already carries in Medusa. The rule
reads the ledger's acked products (`ledger/existing.acked_products`, keyed by SKU to the set of
`(category, norm(value))` it already carries) and lets a product through on a value an acked product of the
same SKU already has. So Medusa's existing listings keep flowing; only a **new** assignment of a restricted
value is held.

## 6. Curate: the three freeze cards

Curate surfaces a restricted assignment through the one existing review path (`pending_confirm`), never a
silent drop and never a guessed default. Three card kinds (see REVIEW_DECISIONS.md for the queue shape):

- **`archived_type`** -- a genuinely new variety would mint under an archived or internal stone type. Only a
  variety that would actually mint is held; an existing variety of that type re-enters emit every produce and
  is left alone. Reason text:
  > This variety's stone type is archived or internal in Medusa, so it cannot be minted under it. Assign an
  > active type in Review, or reactivate the type in Blokport.

- **`archived_value`** -- a mint drops restricted colours and qualities; if the drop empties a **required**
  dimension (no active colour, or no active quality, left), the variety is held rather than minted with a
  guessed default. A non-empty remainder mints with what survives. The card's colour field is left **empty**
  (prefilling the restricted value would only hold again). Reason text (colour / quality variants):
  > The only colour(s) this variety would mint with (X) is/are archived or internal in Medusa. Pick an active
  > colour in Review, or reactivate it in Blokport.
  > The only quality/qualities this variety would mint with (X) is/are archived or internal in Medusa.
  > Reactivate it in Blokport, or the supplier's listing changes.

- **`archived_variety`** -- a gapped scrape resolves (by surface or fuzzy alias) onto an archived variety.
  The alias row is still recorded (reactivating the variety auto-binds the listing and clears the card next
  produce), but a new alias onto a frozen variety never reaches the export the matcher reads, so without a
  card the listing would stay a tree gap forever. The card carries the owner's type so it keeps its own
  `name|type` ref. Reason text:
  > This listing matches <Variety>, which is archived in Medusa. Bind it to an active variety or mint it as
  > new in Review, or reactivate <Variety> in Blokport.

The decision audit suppresses the "mint not applied" gap only for the exact `(source, spelling)` a VE2 card
holds (a global statement matches any vendor), so a held mint is one review entry, not a duplicate gap.

## 7. Combinations keep-set (tree_build)

`build_combinations` fans out finishes x colours x qualities per variety. The VE2 filter keeps a combination
when its every attribute is **assignable**, UNION the tuples that are actually in use:

```
keep_set   = emitted product tuples (this run)  UNION  ledger-acked product tuples
combinations = assignable(fan-out)  UNION  (fan-out INTERSECT keep_set)
```

So a speculative fan-out combination on a restricted value drops, but a combination a real product sits on
(emitted this run, or live in Medusa and acked) is kept, because Blokport replaces the whole combination set
and must not lose a live product's row. An archived variety's combinations are counted **frozen**, not
uncovered (an archived variety with no colour source would otherwise read as an uncovered gap). A product
tuple that was never a fan-out tuple (for example the product asserts a different type than the variety's
Key) is NOT added (the intersection, not a plain union), and is reported; an acked tuple that cannot be
rebuilt is counted, not dropped silently.

## 8. Operator API: the review gate (config server)

The review API never offers, assigns, or binds a restricted value
(`stone_pipeline/config/server.py`):

- **Pickers.** `GET /config/v1/colors` and `/types` list **assignable** values only. The decide validator
  still sees the full vocab (a restricted value is a known attribute, just not offered) and applies its own
  gate below. There is no quality or finish picker; `GET /review/attributes` serves pending (not-yet-in-
  Medusa) values, which are never restricted.
- **`PUT /review/decide`.** A value is judged only where the statement would **assign** it:
  - a **mint** (the target variety does not yet exist) must use an assignable type and colour, else `400`;
  - an **identical re-confirm** of the stored statement for every listing is a no-op and always allowed;
  - otherwise a **bind onto an archived variety** is `400`, and a **colour** the variety does not already
    carry (its backbone seed grown by the config.db leaf overlay, plus any stated colour) must be assignable.
    A bind's carried type/colour are not assignments and are not checked.
  The refusals reuse the existing `{"error": ...}` response shape (`"restricted"` / `"archived"` flags), so
  there is no change to the Blokport-facing response contract.
- **`PUT /review/attributes/{value}`.** A paste of a restricted Medusa id is refused `400` (it would make
  products unassignable).

## 9. Two by-design caveats

- **Variety archived check is per variety, not per format.** `is_archived(name, type)` is true only when
  **every** format Key of the variety (slab, block, tile) is archived; if one format is still active a listing
  can bind to it. So a variety with its slab Key archived but its block Key active is not treated as archived
  by the review gate. A scraped slab product that then resolves to the archived slab Key is still held by
  `value_frozen` at validate (shown in the held breakdown, not as a card). This is not a dead end: the product
  is surfaced, just on the per-product path rather than the per-variety one. A Key present in the ledger but
  not yet in the Medusa export is never mirrored archived.
- **The review gate reads the ledger mirror, which lags Blokport.** `decide` reads the ledger's archived
  state, which is refreshed each produce fetch, so between a Blokport archive and the next produce the gate
  may not yet see it. The next produce then holds the affected products at validate, so the catalog
  self-corrects; the gate is a fast fail for the operator, not the source of truth.

## 10. Dormancy guarantee

Until Blokport publishes the columns (and step C flips the flags), every piece above is inert:
`restricted_values` / `restricted_ids` are empty, `is_archived` is false for every variety, and the readers
default every value active. Pickers list the whole vocab, the decide gate returns nothing, `value_frozen`
holds nothing, the combinations filter drops nothing, and a republish is byte-identical. The feature turns on
only when Medusa's data says a value or variety is archived or internal.
