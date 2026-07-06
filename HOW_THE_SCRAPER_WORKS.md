# How the Blokport Scraper Works

A plain-language, layer-by-layer picture of how messy supplier data becomes a clean,
correct product catalogue in the web shop, and how we keep it correct at every step.
Written to be readable without a technical background, while showing the real
engineering that makes it dependable.

---

## In one sentence

We read raw product data from many stone suppliers (each messy and inconsistent in its
own way), pass it down an assembly line where every station does one job and an
inspector checks the result before it moves on, work out which real stone variety each
product actually is, and hand the finished catalogue to the web shop, which takes the
changes at its own pace and confirms each one.

---

## The big picture: an assembly line with an inspector at every station

```
   SUPPLIER WEBSITES  (each messy and different: odd names, missing fields, watermarked photos)
   polonine · varsha · zucchi · marenostone · ...
        |
        v
 +--------------------------------------------------------------+
 | 1. INGEST    Read the raw rows. Each supplier has its OWN     |
 |              translator, so one supplier's quirks stay with   |
 |              that supplier and never leak into another.       |
 +--------------------------------------------------------------+
        |  [check] every row carries the fields the next step needs
        v
 +--------------------------------------------------------------+
 | 2. CLEAN     Standardise the words: colour, finish, stone     |
 |              type, quality, all mapped to ONE fixed vocabulary.|
 +--------------------------------------------------------------+
        |  [check] an unknown word is FLAGGED for a human, never guessed
        v
 +--------------------------------------------------------------+
 | 3. MATCH     Work out WHICH stone variety this is, even when  |
 |              the supplier spelled it differently or oddly.    |
 +--------------------------------------------------------------+
        |  [check] no confident match -> goes to a review queue, not the shop
        v
 +--------------------------------------------------------------+
 | 4. ENRICH    Add the rest: country of origin, size/weight,    |
 |              and images (downloaded, cleaned, upscaled, hosted).|
 +--------------------------------------------------------------+
        |  [check] a product with no proper image is HELD back, not shipped
        v
 +--------------------------------------------------------------+
 | 5. ASSEMBLE  Build the finished catalogue: the varieties, the |
 |              sellable products, the valid combinations, stock. |
 +--------------------------------------------------------------+
        |  [check] the WHOLE catalogue is cross-checked for consistency
        v
 +--------------------------------------------------------------+
 | 6. LEDGER    The single source of truth: what SHOULD be in the |
 |              shop, and what the shop already has.              |
 +--------------------------------------------------------------+
        |  <=>  the shop PULLS the changes when ready, and confirms each one
        v
   MEDUSA WEB SHOP   (products live for customers)
```

The important idea: **nothing moves to the next station until the inspector for that
station says it is correct.** A bad row is stopped where it is created, not discovered
later on the shop shelf.

---

## The layers, and why they cannot interfere with each other

Each layer has one responsibility. They are deliberately kept apart so a change or a
fault in one cannot spill into another.

```
  PER-SUPPLIER  (isolated)            SHARED  (supplier-agnostic)
  --------------------------          -----------------------------------
  Fetcher   reads that one site       Clean / Match / Enrich / Assemble
  Adapter   translates its columns    never ask "which supplier is this?"
            into our common shape      -> they treat every product the same
```

- **One supplier, one translator.** Adding, changing, or removing a supplier touches
  only its fetcher and its adapter. The shared stages downstream never contain
  supplier-specific logic, so a new or broken supplier cannot quietly change how
  another supplier's products are handled.
- **New suppliers start quarantined.** A new source is "review only" until it has
  passed its own certification, so it can never silently push unproven data live.
- **Removing a supplier is clean and reversible.** A supplier can be taken offline
  (its products go out of stock, reversibly) and later fully removed, without touching
  any other supplier's products.

---

## How we find the real product in messy data (the hard part)

Suppliers name the same stone in wildly different ways. The same variety might appear
as `Black Galaxy`, `Star Galaxy`, `Black Galaxy (Star Galaxy)`, or under a local market
name. Getting this right is what stops the shop filling up with duplicates and mislabels.

```
   Raw supplier name:  "CRYSTAL WHITE GRANITE SLAB"
        |
        |  strip the format word (slab/block/tile) and the type word when safe
        v
   "Crystal White"     <- the candidate variety name
        |
        |  look it up against KNOWN varieties + all their alternative names (aliases)
        v
   +---------------------------------------------------------------+
   |  exact name?      -> matched, high confidence                  |
   |  known alias?     -> matched (e.g. "Star Galaxy" -> Black Galaxy)|
   |  close spelling?  -> fuzzy match, only above a strict threshold |
   |  nothing solid?   -> NOT guessed. Sent to a human review queue  |
   +---------------------------------------------------------------+
```

- **Every variety has a stable identity.** Once recognised, a variety keeps the same
  internal key, so the same stone from three different suppliers becomes ONE catalogue
  entry, not three.
- **Alternative names are captured, not lost.** A bracketed local name like
  `(Star Galaxy)` is moved out of the display name and kept as an alias, so a future
  supplier who uses that name still matches the right variety.
- **We never guess.** A colour, finish, or variety we cannot resolve with confidence is
  flagged for a person to decide, rather than invented. This is why the catalogue stays
  trustworthy even as the incoming data stays messy.
- **The mess is actively cleaned.** Junk, duplicate, and malformed alternative names are
  split apart, de-duplicated, and tidied so the matching keeps improving over time.

---

## How the shop stays in sync (Medusa)

The scraper never reaches into the shop and writes to it. Instead it keeps a **ledger**
(a precise to-do list) of what the shop should contain, and the shop reads from it on its
own schedule and confirms each change. This is what makes the sync safe and unattended.

```
   SCRAPER (the ledger = source of truth)          MEDUSA WEB SHOP
   ------------------------------------            ---------------------
   "these varieties are ready to add"   ---->      creates them, sends back their ids
   "these products are ready"           ---->      creates them, sends back their ids
   "this stock level changed"           ---->      updates the quantity
   "this product was removed"           ---->      deletes it, confirms it is gone
                                        <----  the shop's confirmation is the "done" signal
```

- **The shop pulls; the scraper never pushes.** If the shop is busy or down, the work
  simply waits in the ledger until the next pull. Nothing is lost, nothing is forced.
- **Correct order is guaranteed.** A product is only offered to the shop once the
  variety it belongs to exists and its image is ready, so the shop can never receive a
  product that references something it does not have yet.
- **Everything is confirmed.** For each item the shop mints an id and sends it back; only
  then is that item marked "in sync." Re-running only ever moves what has actually changed.
- **Out of stock vs removed are different, on purpose.** A discontinued item goes to
  quantity zero (still listed, reversible). A removed supplier's products are properly
  deleted, and the shop confirms each deletion.

---

## How we know it is correct at every step

Robustness here is not a claim, it is built in and checked automatically:

- **An inspector at every boundary.** Between each layer, a "gate" checks that the output
  meets a written contract (for example: every product has an origin, a size, and a
  resolved type). A row that fails is held or sent to review, not passed on. If a whole
  batch fails one rule, that signals a systemic problem and the run stops loudly rather
  than shipping bad data.
- **Golden-record tests per supplier.** Each supplier has a saved "known-good" sample;
  every change is automatically checked against it, so a tweak that breaks one supplier's
  reading is caught before it ever runs for real.
- **A whole-catalogue consistency check.** Before anything is handed over, the finished
  catalogue is cross-checked as a set: no duplicates, no missing pieces, stock and
  combinations all agree with the current products. If they do not, it fails loudly.
- **The source of truth is protected.** The ledger is safeguarded against interference
  (it refuses risky changes while the shop is mid-read) and is backed up automatically,
  so a restart never loses what the shop has already confirmed.
- **Images are ready before products.** Photo cleaning and hosting finish inside the run,
  before the product is offered, so a product can never appear referencing a missing image.

---

## Why this is more robust than it looks

Every hard part of "messy supplier data to a clean shop" has been given its own
isolated, checked stage:

- Suppliers cannot interfere with each other.
- Unknowns are flagged for people, never guessed.
- The same stone from many suppliers becomes one clean entry.
- Nothing reaches the shop until it is complete and consistent.
- The shop syncs at its own pace and confirms everything, unattended.

The result is a system that stays accurate and trustworthy even while the data flowing
into it stays as messy as the real world always is.
