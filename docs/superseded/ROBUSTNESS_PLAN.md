# Robustness plan: bring every layer in line with its contract

## 0. Goal and diagnosis

Make the pipeline robust on the messy multi-source data it ingests: it does exactly what it is
supposed to, and when it cannot, it HOLDS for review rather than guessing.

The architecture is already right: a layered pipeline (adapt -> normalize -> match -> reconcile ->
derive -> curate/tree -> emit) with conformance gates at the boundaries, governed by three stated
rules -- clean at the layer where the info first exists, REUSE before MINT, and when uncertain HOLD
(never guess). A three-part audit of the EXISTING code found those rules are violated in a
consistent pattern in almost every layer. Robustness is therefore not new machinery; it is enforcing
the rules the design already declares, in one place per concern, with a gate per invariant.

The recurring root is one thing: **guess-instead-of-HOLD**. It is what produces the reported
empty-type and cross-type symptoms, and it appears in match, normalize, curate, and tree_build.

## 1. The invariants (cross-cutting; each becomes an enforced rule)

These are the rules from CLAUDE.md and the pipeline's own contracts, restated as invariants the code
must satisfy. Each fix in section 3 maps to one or more of these.

- **I1 Never guess; HOLD when uncertain.** An unresolved required value is flagged / routed to a
  review (confirm or uncovered/assign_type) queue, never fabricated to clear a gate.
  Violated in: match (block), normalize (last-resort), curate (empty-type mint, fallback colour/quality),
  tree_build (modal colour, invented type).
- **I2 Vocabulary and policy sourced from one live place, never hardcoded.** Every list of attribute
  values / descriptor words / defaults comes from `reference/` or `attributes.csv`.
  Violated in: `_AMBIGUOUS_TYPE_WORDS`, `_NEGATION_WORDS`, `_DEFAULT_FINISHES`, quality `"A"`, `"raw"`,
  `_PREFIX_CATEGORY`, the `+"s"` pluralization.
- **I3 Deterministic and idempotent.** Same input -> same Key and same result, independent of what
  else is in the batch.
  Violated in: batch-discovered code-prefix stripping; first-wins ordering in consolidate/mirror.
- **I4 Every boundary invariant is a gate; one authority per invariant.** Importability and mint
  validity are gate contracts, not logic scattered across files.
  Violated in: no gate covers the variety-mint/catalog-emit boundary; importability split between the
  PROCESS gate and `validate.REQUIRED_ID_FIELDS`.
- **I5 Fail loud.** No catch-all that swallows a real failure as a silent drop or fallback.
  Violated in: adapter `except Exception` -> warn-and-drop; emit_catalog S3 `except Exception` -> heuristic.
- **I6 No magic numbers in logic.** Thresholds live in `SETTINGS`.
  Violated in: matcher floors `85.0`, fuzzy `90`, snap `80`, `_CHAR_REVIEW_FLOOR 0.90`, `min_fanout`.
- **I7 One logic path per concern.** No duplicated or stacked logic over one datum.
  Violated in: two variety-name cleaners; two importability checks; `attr_unresolved` + `attr_last_resort`
  on the same field.
- **I8 Never cross stone type or colour silently.** Type/colour is identity-bearing; a match, alias, or
  fold across it must HOLD, not merge.
  Violated in: match block no-ops on empty query type; surface-alias keyed without type; consolidate folds
  without type.

## 2. Flagship: the empty-type / cross-type chain (your reported complaints, root-caused)

Both symptoms are one failure that cascades across four layers, each guessing instead of holding:

1. **match** (`engine.py:294`): the type block is guarded `if nt and ...`, so a query with no type
   admits every candidate type and can auto-accept a cross-type nearest (granite -> Black Marble).
   Because it resolves to a wrong-type variety, the variety never reaches the HOLD path below.
2. **reconcile** (`reconcile_tree.py:116-130`): a resolved variety with no type / no backbone record is
   passed through as a soft flag, not routed to the uncovered/assign_type HOLD the contract describes.
3. **curate** (`curate.py:281-292, 467-475`): `variety_identity` can yield `stone_type=""`, and PHASE 5
   MINTs it -- `gen_key` drops the empty slug, producing a type-less Key. No `if not stone_type: HOLD`.
4. **tree_build** (`tree_build.py:243-290`): the type-less variety gets a type invented from a word in
   its Name and a colour set to the catalogue's modal colour, so it becomes priceable and ships.
5. **gate**: nothing validates the mint. `validate.py` only sees product rows, never curate's minted
   varieties, so the empty-type variety reaches Medusa.

Conformant fix (each layer does its part; HOLD-and-hand-off):
- match: fail closed on unknown query type -- cap an unverified/cross-type hit at `review_floor`
  (route to review), mark it `fuzzy_type_unverified`, never auto-accept. Same for colour.
- reconcile: type-less / no-backbone -> the uncovered/assign_type HOLD contract (its enforcing home).
- curate: never MINT an empty stone type -> `pending_confirm` HOLD with a clear reason.
- tree_build: stop inventing type and colour; a variation lacking them goes uncovered.
- gate: a NEW mint contract HARD-rejects an empty-type variety before write (defence in depth).

Result: a type-less variety is HELD for the operator to `assign_type`, never guessed, never shipped.
This is the correct, design-conformant replacement for the source-dominant guess that was reverted.

## 3. Per-layer plan

For each layer: contract, the messy-data cases it must own, the existing violations to bring in line
(audit citations), the conformant fix, and the gate that enforces it.

### Layer 1 -- Adapters (`adapters/`)
- **Contract:** map columns -> `raw_*` faithfully; per-source quirks isolated; no inference; never guess.
- **Messy cases:** colour in a name (zucchi PT slot), prose that is not colour, trade-name colour,
  garbage numerics (European decimal comma), placeholder values (`"N/A"`), missing type.
- **Violations to fix:** colour-scan picks first colour by position and can take a trade-name colour
  silently (`tokens.py:91-100`, no flag); `except Exception` swallows code bugs as dropped rows
  (`base.py:149`); `_per_piece_kg` returns `""` on bad numerics with no flag (`zucchi.py:76`); varsha
  passes `"N/A"` straight through (`varsha.py:43`); batch-discovered code stripping is non-deterministic
  (`base.py:141` + `text.py:174`); hardcoded `_GRANITE_CODE`, `min_fanout`, `looks_codey` sizes.
- **Fix:** colour recovery HOLDS on ambiguity (>1 colour word -> no `raw_color`, set an ambiguous
  marker for Stage 3 to review); narrow the adapter except to parse/validation errors + a drop-rate
  ceiling that fails loud; distinguish "no data" from "bad data" in numerics (flag the bad); give varsha
  the `_na` cleaner; make code stripping deterministic (freeze prefixes in `state/` or per-source config).
- **Gate:** ingest gate reports missing raw fields and escalates systemic gaps.

### Layer 2 -- normalize (`stages/normalize.py`, `matching/engine.py: VocabResolver`)
- **Contract:** resolve to canonical id via synonym -> exact -> fuzzy -> unresolved(HOLD); never accept
  an ambiguous descriptor as a type; a weak match HOLDS, it does not silently resolve.
- **Messy cases:** ambiguous type words (Crystal/Quartz), compounds (Polished + Leather), low-cardinality
  vocab typos (quality `AA`/`A+`), mis-tagged type (name-over-tag), a whole-value weak-fuzzy that should
  yield to a compound.
- **Violations to fix:** last-resort fabricates finish/quality (`normalize.py:59-71,165`) -- guess, not
  HOLD, and auto-emits for `mode: auto`; block-default `Raw` reads `raw_format` before format is resolved
  (`normalize.py:154`); the ambiguous-type guard lives only in the name scanner, so a synonym
  `crystal->type` bypasses it (`engine.py:85`); fuzzy auto-accepts at a single global floor `90` with no
  review flag; per-part fuzzy in `resolve_multi` is unguarded.
- **Fix:** last-resort becomes a HOLD for `mode: auto` (or per-source opt-in) and the contract text is
  reconciled with `settings.py`; move the block-finish default to after format_resolve on a resolved
  format; enforce the ambiguous-descriptor guard inside `VocabResolver` for the type vocab, sourced live
  (I2); per-vocab fuzzy floors (I6) and HOLD (flag) any fuzzy hit for low-cardinality closed vocabs;
  restrict `resolve_multi` per-part to exact/synonym.
- **Gate:** clean gate (canonical-casing invariant).

### Layer 3 -- match_variation (`stages/match_variation.py`, `matching/engine.py`, `index.py`, `alias_resolver.py`)
- **Contract:** match within branch, block by type AND colour, never cross-type; uncertain -> review/gap.
- **Violations to fix:** the flagship block-fails-open (`engine.py:294,296,177-186,279,200`); `_engine`
  borrows a foreign category index on unknown branch (`match_variation.py:93`); magic floors `85.0`,
  `_CHAR_REVIEW_FLOOR 0.90`.
- **Fix:** the block fails closed and returns a tri-state (ok / blocked / unverified); an unverified
  cross-type/colour hit is capped at `review_floor` and marked `*_type_unverified`; an unknown branch
  HOLDs as an unsupported-branch gap instead of borrowing; floors to `SETTINGS.matching` (I6).
- **Gate:** the tier blocking itself; the review routing.

### Layer 4 -- reconcile_tree (`stages/reconcile_tree.py`) -- home of the empty-type HOLD
- **Contract:** attrs in the variety's allowed sets; a variety with no resolvable type is surfaced
  UNCOVERED via `assign_type` (HOLD), never guessed; colour is identity-bearing (never snapped).
- **Violations to fix:** type-less / no-backbone rows pass through as flags, not held (`:116-130`);
  `_fill_missing_from_variety` picks `allowed[0]` as "primary" -- order-dependent guess (`:56-63`); snap
  floor `80` magic (`:198`).
- **Fix:** route a resolved-but-typeless / no-backbone row to the uncovered/assign_type HOLD; replace
  the `allowed[0]` "primary" with an explicit backbone primary or leave null + flag; floor to `SETTINGS`.
- **Gate:** clean gate (unresolved-tree-gap report).

### Layer 5 -- curate (`stages/curate.py`)
- **Contract:** CANONICALISE -> DEDUP -> REJECT/HOLD -> REUSE -> MINT; REUSE before MINT; HOLD when
  uncertain; outputs canonical.
- **Violations to fix:** MINT with empty `stone_type` (`:281-292,467`); `_FALLBACK_COLOR="Natural"`
  guess with a contradicting comment (`:164,562`); quality `"A"` magic (`:425,472`); hardcoded
  `_DEFAULT_FINISHES` (`:156`); two cleaners (`_clean_variety` vs `clean_variety_name`) risking
  Key<->Name divergence; cross-type surface auto-alias -- `existing_surface` keyed without type
  (`:248-254,407-410`); `except FileNotFoundError` leaves `json.loads` unguarded (`:524`).
- **Fix:** never MINT an empty type -> `pending_confirm` HOLD; drop the colour/quality guesses (HOLD or
  source from `ref.attributes`, justified); `_DEFAULT_FINISHES` from `ref.attributes.by_category["finish"]`
  (I2); collapse to one cleaner used at mint and consolidate (I7); key `existing_surface` on
  `(norm(type), norm(name))` and route a cross-type name collision to review, never auto-alias (I8);
  guard `json.loads` (`JSONDecodeError`) and surface the bad file (I5).
- **Gate:** the new mint gate (Layer 7).

### Layer 6 -- tree_build + emit_catalog (`stages/tree_build.py`, `stages/emit_catalog.py`)
- **Contract:** build the combination set + variant files; an unresolvable type is reported uncovered,
  never silently dropped or invented; outputs canonical/consistent.
- **Violations to fix:** `_resolve_type` invents a type from a Name word (`:187-192`) and
  `default_color`/`default_qual` price a colourless variation with the modal colour (`:243-290`);
  `_raw_finish` fabricates `{"raw"}` (`:207`); `_consolidate` folds by clean-name without type
  (`emit_catalog.py:67-99`); S3 catch-all (`emit_catalog.py:63`) can advertise a 404; `_PREFIX_CATEGORY`
  and `+"s"` hardcoded (`:34,275`); `assign_type` typo loops uncovered forever with no re-flag (`:139`).
- **Fix:** remove the type/colour/finish fabrication -- once curate HOLDs (Layer 5) the type-less variety
  never reaches here; gate `_consolidate` folding on same Key-derived type (I8); narrow the S3 except to
  the specific botocore errors and let a real fault fail loud (I5); use the category registry for
  prefix/plural (I2); re-surface an unresolvable `assign_type` with a distinct reason so the operator
  sees the typo.
- **Gate:** the new mint gate.

### Layer 7 -- gates + validate (`gates/definitions.py`, `gates/contract.py`, `gates/runner.py`, `stages/validate.py`)
- **Contract:** each boundary invariant is a gate; Medusa-breaking rows hard-reject with a precise reason.
- **Violations to fix (the missing gate):** no contract covers the variety-mint/catalog-emit boundary,
  so mint validity is enforced nowhere; importability is split between the PROCESS gate and
  `validate.REQUIRED_ID_FIELDS`; `_nonempty` numeric trap documented but shipped (`contract.py:37`);
  `category_invalid` recomputes per row and depends on the import-frozen pcats (`validate.py:42`);
  `dimension_invalid` breaks on the first bad dim (`:52`).
- **Fix:** add a MINT ModuleContract with HARD `mint_missing_type` (`bool(post["stone_type"])`) and
  `mint_key_shape` (Key carries a non-empty type slug), applied to `backbone_new`/`new_variants` before
  `write_curation` (I4, and the defence-in-depth for the flagship); move `REQUIRED_ID_FIELDS` into one
  importability authority; give any numeric required field an explicit predicate; hoist `valid_pcat` out
  of the per-row check and refresh the registry after fetch; collect all invalid dimensions before
  rejecting.

## 4. Sequenced rollout

One layer at a time. Each step: agree the specific change, implement small and clean, add tests, and
validate on a REAL 4-source produce (not only fixtures) before the next step. No step ships with a
known gap.

1. **Foundation (I2 + I6):** source the ambiguous-type / negation / default-finish lists from
   `reference/`; move matcher and snap floors into `SETTINGS`. Low-risk, removes the drift, unblocks the
   type work. Validate: the vocab lists load identically; existing tests green.
2. **The flagship chain (I1 + I8 + I4):** match fail-closed -> reconcile uncovered handoff -> curate no
   empty-type MINT -> tree_build stop inventing -> new MINT gate. Ship as its own reviewed change with a
   real produce that shows type-less varieties HELD in `tree_uncovered_variations.csv`, not shipped.
3. **Cross-type merges (I8):** type-aware `existing_surface` and `_consolidate`. Real produce: no
   cross-type aliases in the backbone.
4. **Determinism (I3):** batch-independent code stripping; stable ordering. Validate: same product ->
   same Key across a partial vs full scrape.
5. **Fail-loud (I5):** narrow the adapter and S3 excepts; add the drop-rate ceiling.
6. **One-path (I7):** collapse to one cleaner and one importability authority.

Each step is a separate, small, agreed, tested change. The flagship (step 2) is the one that fixes the
reported empty-type and cross-type complaints, at the layers that own them, HOLD-not-guess.
