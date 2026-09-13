# Decision model: one statement table, one read

Status: DESIGN, awaiting approval. No code yet.

## 1. Where we are

An operator decision is "for vendor S, the listing scraped as X is variety N of type T (colour C, origin O,
widen W)", or "X is not a variety" (reject). Since #331 the mint side of that is keyed correctly:
`variety_decision` has `PRIMARY KEY (source, variant_norm)`, `''` is the global meaning of a spelling and a
vendor is its own stone beside it, and `decide()` derives the level. That was the structural flaw; it is
fixed.

What remains is accretion around it. One statement is written into up to three tables and read back through
more than a dozen projections:

| written by `decide()` | table | why it exists today |
|---|---|---|
| the mint (global or vendor) | `variety_decision` (source, spelling) | the decision |
| the vendor binding | `scoped_alias` (source, spelling) -> (name, type) | the matcher's override tier; duplicates what a vendor-level decision already implies |
| the vendor origin | `origin_decision` (source, name, type) -> iso, widen | derive's supplier-override tier; keyed by the RESULT not the listing |
| the reject (legacy route) | `variety_decision` (`''`, cleaned name) action=reject | `PUT /review/variants/<ref>`; keyed by the card ref, not the listing |

Read side, per produce:

| reader | accessors consulted |
|---|---|
| `curate/context.py` | `confirm_map`, `rejected_names`, `variety_seed_colors`, `variety_seed_types`, `variety_seed_names`, `variety_seed_scopes` (six maps), each looked up through `decided()`, which tries four keys per row |
| `match_variation` | `ref.scoped_aliases`, `ref.variety_seed_types` |
| `loaders/reference_data.load_all` | `variety_seed_country_rules`, `variety_origins`, `origin_widen`, `origin_decisions`, `scoped_aliases`, `variety_seed_types`, `backbone_leaf_overlay` |
| `config/resolved.py`, `stages/decision_audit.py` | `variety_actions`, `scoped_aliases`, `origin_decisions`, `origin_widen` |

Compensations that exist only because of the above: `_drop_global_reject` (a statement must out-rank a
reject stored under a different key), `_foreign_scope` in curate (a vendor decision must not decide another
vendor's row), the scope bit in the dedup identity, `clear_decisions` deleting from three tables, and the
`backfill_levels` boot migration (still live, its job done).

The decision store is 974 lines and 47 functions; 18 test files call it at 170 sites.

## 2. Target

### 2.1 One table

```
statement (
  source          TEXT NOT NULL,          -- '' = the global meaning of the spelling
  spelling_norm   TEXT NOT NULL,          -- the scraped spelling a listing is keyed on
  spelling        TEXT NOT NULL,          -- display form
  verdict         TEXT NOT NULL CHECK (verdict IN ('is', 'reject')),
  name            TEXT,                   -- verdict 'is': the variety the listing IS
  stone_type      TEXT,                   -- canonical Medusa type (validated by the server)
  color           TEXT,                   -- optional mint seed
  origin_iso      TEXT,                   -- optional: this vendor's origin for the variety
  widen           INTEGER NOT NULL DEFAULT 0,
  asked_by        TEXT NOT NULL DEFAULT '',   -- the vendor whose card produced a global statement
  decided_at      TEXT NOT NULL,
  PRIMARY KEY (source, spelling_norm)
)
```

Invariants:
- A statement is about a listing (source, spelling). Nothing is keyed by the result.
- Whether a statement MINTS or BINDS is not stored; it is derived at read time from what exists, exactly as
  `decide()` derives it today, so a statement stays correct when the catalog changes under it (a stated
  name that later exists binds instead of minting a duplicate).
- The two-level rule is unchanged: a statement on an undefined spelling with `source=''` defines the global
  meaning; a vendor statement that names a different stone is that vendor's own.
- `variety_origin` (operator "edit origins" of a variety, a per-variety fact, not a listing statement),
  `backbone_leaf_decision`, `attribute_decision`, `protected_variation`, `review_pending` stay as they are.

### 2.2 One derived object, one read per row

```python
@dataclass(frozen=True)
class Decisions:
    """Every statement, loaded once per produce, with the derived views the stages consume."""
    statements: dict[tuple[str, str], Statement]          # (source, spelling_norm)

    def for_listing(self, source, spelling, clean) -> RowDecision | None
    # vendor statement on the spelling, else on the cleaned identity, else the global one on either.
    # RowDecision(verdict, name, stone_type, color, origin_iso, widen, level: 'vendor'|'global', renamed: bool)

    def scoped_bindings(self) -> dict[(source, spelling_norm), (name, stone_type)]   # matcher override tier
    def mint_seed_types(self) -> dict[(source, spelling_norm), stone_type]           # matcher, curate
    def vendor_origins(self) -> dict[(source, name_norm, type_norm), iso]            # derive's supplier tier
    def widened(self) -> dict[(source, name_norm, type_norm), iso]                   # origin map union
    def mint_origin_rules(self) -> dict[(name_norm, type_norm), iso]                 # origin map overlay
```

`Decisions` is built by `decisions_store.load_decisions()` (one SELECT) and carried on `ReferenceData`
(`ref.decisions`), so curate, the matcher, derive, the resolved view and the audit all read the same object.
Curate's context drops its six maps and `_foreign_scope`; `classify`, `minting` and `variety_identity` call
`c.ref.decisions.for_listing(row.src_site, spelling, clean)` once and read fields off the result. The dedup
identity keeps `(type, clean, level)` with `level` from the same call.

### 2.3 One write API

```python
decide(source, spelling, name, stone_type, color="", origin="", widen=False) -> Outcome   # verdict 'is'
reject(source, spelling) -> None                                                            # verdict 'reject'
clear(source, spelling) -> int
clear_for_variety(name, stone_type) -> int      # unmint: every statement whose derived result is that variety
clear_all() -> int                              # pristine reset
```

`decide()` keeps its derivation and validation; it writes ONE row. Because vendor bindings and origins are
derived from the same row, there is nothing to keep consistent and `clear` is one DELETE.

Reject becomes a listing statement like everything else. A reject from a card that carries several
listings is written once per listing; a card with no listings (legacy shape) writes the global row
(`source=''`). The last statement on a listing wins, so `_drop_global_reject` disappears.

### 2.4 Routes

| today | after |
|---|---|
| `PUT /review/decide` {listings, name, type, color?, origin?, widen?} | unchanged |
| `PUT /review/variants/<ref>` {"action":"reject"} | `PUT /review/decide` {listings, verdict:"reject"}; old route kept for one release returning the same shape, then removed |
| `DELETE /review/decide` {listings} | unchanged |
| `GET /review/variants`, `GET /review/resolved` | unchanged contract; `current_*` fields come from `Decisions` |

Blokport UI impact: the reject button sends a statement with `verdict: "reject"` and the card's listings.
One line in their client. The old route stays until they switch.

## 3. Consumers and their change

| consumer | change |
|---|---|
| `stages/curate/context.py` | `Curation` gets `decisions: Decisions` (from `ref`); the six maps, `decided()`, `_foreign_scope()` and `level()` go; `variety_identity`, `classify`, `_reject_or_decide`, `mint`, `emit_new_variants` read one `RowDecision` |
| `stages/match_variation.py` | `ref.scoped_aliases` -> `ref.decisions.scoped_bindings()`, `ref.variety_seed_types` -> `ref.decisions.mint_seed_types()` |
| `reference/loaders/reference_data.py` | `load_all` builds `ref.decisions = decisions_store.load_decisions()` (guarded on the store existing, as today); the origin overlays take `mint_origin_rules()`, `vendor_origins()`, `widened()` from it; `variety_origin` overlay unchanged |
| `stages/derive*` (origin tiers) | supplier-override tier reads `ref.decisions.vendor_origins()` (today via `origin_overrides.apply_overlay(origin_decisions())`) |
| `config/resolved.py`, `stages/decision_audit.py` | take a `Decisions` instead of three maps |
| `config/server.py` | `decide` route unchanged; reject route delegates to `reject()`; `backfill_levels` boot hook replaced by the one-time migration below |
| `lifecycle.py` | unmint -> `clear_for_variety(name, type)`; pristine reset -> `clear_all()` |
| `stages/decisions.py` (the façade over the store) | `load_confirm_decisions`, `load_rejected`, `load_variety_seed_*` removed; one `load_decisions()` |

## 4. Migration

Boot-time, idempotent, in `store._migrate`, the same slot `backfill_levels` uses today (after the ledger
restore, before the servers accept requests):

1. Create `statement`. If it already has rows, stop (already migrated).
2. `variety_decision` action=mint -> one statement per row: `verdict='is'`, `name = seed_name or spelling`,
   `stone_type = seed_type`, `color = seed_color`, `origin_iso = seed_country`, `source`, `asked_by`.
3. `variety_decision` action=reject -> `verdict='reject'` on `(source, spelling)`.
4. `scoped_alias` rows with no statement at `(source, spelling)` -> `verdict='is'`, `name = alias_of`,
   `stone_type = seed_type`. (Rows that duplicate a vendor mint are dropped: the mint row already implies
   the binding.)
5. `origin_decision` -> for each `(source, name, type)`, the vendor's statement whose derived result is that
   `(name, type)` gets `origin_iso` and `widen`. One that matches no statement (an origin confirmed on a
   variety the vendor's listings bind to without a statement, the origin-queue case) becomes a statement
   `(source, spelling = name)` with `name`, `type`, `origin_iso`, `widen`: "this vendor's `name` is `name`,
   from `origin`", which is what the confirmation meant.
6. Record `statement_migration` in the `migration` table with the row counts moved, and log them.
7. Legacy tables are renamed `*_legacy` and dropped in the release after next, once a prod produce has run
   on the new table and the audit shows zero decision gaps.

Verification before cutting consumers over (PR A below): a dual-read check that for the current prod
`config.db` snapshot every legacy accessor equals the corresponding `Decisions` view. This runs as a test
against a copied snapshot, not in production.

## 5. Deletion list

Store: `variety_actions`, `variety_seed_colors`, `variety_seed_types`, `variety_seed_names`,
`variety_seed_scopes`, `variety_seed_countries`, `variety_seed_country_rules`, `confirm_map`,
`rejected_names`, `set_variety_decision`, `_lookups`/`_spelling_means`/`_global_meaning` (fold into
`Decisions` derivation), `backfill_levels`, `_drop_global_reject`, `clear_decisions`, `set_origin_decision`,
`origin_widen`, `origin_decisions`, `clear_origin_decisions`, `set_scoped_alias`, `scoped_aliases`,
`clear_scoped_aliases`, `clear_variety_decisions`, `clear_variety_decision`, `scope_key`.
Tables: `variety_decision`, `scoped_alias`, `origin_decision` (step 7).
Curate: `decided`, `_foreign_scope`, `level`, the six context fields.
Façade: the six `load_*` accessors in `stages/decisions.py`.
Server: the legacy reject route (after Blokport switches).

Estimated: store 974 -> about 450 lines; curate context 199 -> about 140.

## 6. Tests

Behaviour tests stay and are the guard: `test_mint_rename_two_produce`, `test_review_decide`,
`test_mint_type_authority`, `test_operator_type_fallback`, `test_collision_cards`, `test_clean_name_retry`,
`test_origin_decision_flow`, the matcher override tests. Their store calls move from the deleted accessors
to `decide()`/`reject()`/`load_decisions()`; assertions on derived views use `Decisions` methods.
New: `test_decisions_object.py` (derivation rules, two levels, rename, reject supersession, unmint clear),
`test_statement_migration.py` (each legacy shape -> statement, idempotence, the dual-read equality on a
fixture snapshot), one end-to-end statement -> produce -> bind test per level.

## 7. Sequence

Three PRs, each green on the full suite and deployed dev then prod before the next. Read side first: it
carries zero data risk and removes the six maps and the four-key lookup immediately; the table follows.

- **A. One read.** `Decisions` and `load_decisions()`, built from the EXISTING tables (a pure projection),
  carried on `ReferenceData`; every consumer (curate, matcher, loaders overlays, resolved view, audit) reads
  it and nothing else. `decided()`, `_foreign_scope()`, `level()` and the six context maps go. Behaviour
  tests unchanged and green; one produce on dev, then prod, with zero decision gaps.
- **B. One table.** `statement`, the writers (`decide()`, `reject()`, `clear*`), the boot-time migration and
  the dual-read test (legacy projection == statement projection on the prod snapshot); `load_decisions()`
  switches source. The legacy reject route delegates.
- **C. Delete.** Legacy tables renamed, dead accessors and compensations removed, route removed once
  Blokport has switched.

## 8. Decisions needed from you

1. **Reject scope.** Per listing (each vendor on the card), with the global form kept for legacy cards, as
   designed. The alternative is "a reject is always global", which is today's behaviour.
2. **Origin-queue confirmations without a statement** migrate as "the vendor's `name` is `name`" (section 4,
   step 5). Alternative: keep `origin_decision` as a separate table. Recommended: migrate.
3. **Blokport coordination.** The reject route change is one client line; the old route stays until they
   confirm. Say when to brief them.
