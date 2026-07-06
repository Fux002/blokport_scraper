# Accidental-Complexity / Over-Engineering Audit — blokport_scraper

**Read-only audit.** No code was changed. This is a suggestions map for a later cleanup session.
Every candidate below was traced to WHY it exists (git/blame/comments/callers) before any
recommendation — Chesterton's Fence. "Leave alone" and "not worth touching" are frequent and honest
conclusions; nothing was manufactured.

**Method:** 7 parallel read-only agents, one per subsystem (adapters, stages, matching+curation,
ledger, config, foundation=gates/health/drift/core/reference, orchestration+io). Coverage checked
against the 452-test suite. The pipeline is deterministic (catalog = pure function of export+scrapes),
so the strongest safe verification for any future change is an **output diff**: `produce` before/after
on a frozen input, diff `to_upload/` + ledger + canonical parquet — zero diff = behavior-preserving.

**Headline verdict:** The pipeline is **basically sound**. It is dense but disciplined — nearly every
non-obvious line carries a rationale comment tracing to a real data bug or a stated design invariant.
Total genuinely-removable surface is **~25 lines across 6 tier-1 items**, none of which is a bug or a
friction source. There is **one item with real correctness stakes** (FND-04) that is a *verify-intent*,
not a cleanup. Do not expect a big cleanup here; there isn't one to do.

---

## TIER 1 — Provably dead, safe to remove (verified: no callers + not covered)

These all trace to declarations that were never wired to a reader — several from the one big
`ae5c382` "deploy-ready" commit. Each is a small, deterministic, zero-behavior-change delete.
Recommendation: **bundle into an unrelated edit rather than a standalone PR** — individually low value.

| ID | Location | What | Why it exists | Verify |
|----|----------|------|---------------|--------|
| **LED-1** | `ledger/sync.py:73` | `_SERVABLE = ("pending","dirty")` unused constant | Intended shared "servable states" def; logic evolved into inline SQL predicates `_ELIGIBLE_*` (string literals) and left it behind (commit `6d5862c`). Zero refs anywhere. | `grep _SERVABLE` → def only; output unchanged by construction |
| **MAT-1** | `matching/alias_resolver.py:15` | `import re` unused | Leftover from a refactor that removed the last regex use (`7810f36`). | pyflakes clean; import has no side effects |
| **CFG-1** | `config/settings.py:406-409, :538` | `EmitPolicy.emit_on_review_default` + `Settings.emit` | Superseded by the **per-source** `SourceConfig.emit_on_review` (read at `run.py:300`). Global default never wired. **Best payoff of the tier-1s** — removes a genuine "two `emit_on_review`, only one live" trap. | suite + one frozen produce diff |
| **CFG-2** | `config/settings.py:201` | `Thresholds.derived_accept` | Sibling thresholds are all read live; this one never got a consumer (`ae5c382`, no reader ever added). | suite + frozen diff |
| **CFG-5** | `config/settings.py:103-105` | `Confidence.from_name` classmethod | String→enum parser; zero callers in repo + full history. Enum itself is heavily used — keep it. | suite; nothing depends on it |
| **FND-01** | `reference/loaders.py:33` | Duplicate `log =` shadowed by `:89` | Line 33 (`"reference.loaders"`) added later by `1926719` without noticing the pre-existing `log =` at line 89 (`"reference"`). Line 33 is never observed. | **Note:** deleting line 33 preserves the emitted logger name `stone_pipeline.reference`; deleting line 89 would *change* it — prefer deleting 33 if any log alerting keys on the name |

**ADP-1** (`adapters/selftest.py:63` `run_all()` no callers) is also technically tier-1, but it's a
harmless 2-line public convenience aggregator that reads as legitimate module API — near-zero value
either way. Leave or remove at taste.

---

## TIER 2 — Cosmetic / doc-only (test-covered or trivially safe)

Fix only if you're already in the file. No behavior change.

- **LED-2** — `ledger/bootstrap.py:124-131`: docstring says `attribute_id` "returns None if not seeded **or not synced**", but the SQL applies no state filter. Attributes are never moved out of `synced`, so the code is correct — the docstring lies. **Reword docstring, do NOT add a filter** (it would be dead too).
- **LED-3** — `ledger/snapshot.py:149`: `main()` docstring references "before the volume cutover (EFS)"; the EFS→local-disk+WAL cutover shipped (`bbf35a2`/`1c50d08`). The `save|restore` CLI is still current — only the framing is stale. Trim the clause, optional.
- **FND-02** — `reference/loaders.py:169`: `VariantTable.surface_to_id` is deliberately never populated (documented NOTE: per-row insert over ~24k rows was wasted work; matching uses `matching/index.py` instead). Kept alive only so `tests/test_tiles.py:32` can `.clear()` it. Removable *with* that test line, but it's a zero-cost empty dict with a documenting NOTE — marginal.
- **STG-2** — `stages/emit.py:49`: `_inventory = inventory_for` alias used once; cosmetic naming-symmetry with adjacent `_num`/`_bool` helpers. Self-documenting via the line-48 comment. Leave.
- **ADP-5** — dims reference inconsistency: `marenostone/zucchi/varsha` use `lambda r: _dims(r)` (forward-ref workaround, helper defined below class) vs `polonine` bare `_dimensions` (defined above). Both correct; fixture-guarded. Optional consistency pass only if touching these files.

---

## TIER 3 — Verify intent / needs a characterization test FIRST

- **FND-04 — the one finding with real correctness stakes.** `reference/sync_variants_base.py:26`
  hardcodes `.../from_medusa/development/variants_export_base.csv`, while the rest of the pipeline
  derives the env folder from `ENV_NAME` (`SETTINGS.paths.from_medusa_dir`, "production" if
  `IS_PRODUCTION`). `catalog.run()` calls `sync_variants_base.sync()` unconditionally (`catalog.py:89`),
  so a **production** run would write/read the committed base seed under `.../development/`. Pre-existing
  and internally consistent (predecessor `4c81b8d` already did this; memory says "dev now then promote
  to prod"), so it *may* be intentional for the current phase. **Action: confirm with owner whether the
  base seed should follow `ENV_NAME`.** If yes, swap the literal for `SETTINGS.paths.from_medusa_dir /
  "variants_export_base.csv"` — but add a per-env path-assertion test FIRST (the base is a committed
  source-of-truth; a wrong path corrupts the seed). No test currently references this module.
- **CFG-4 / MAT-2 — `MatchingConfig.semantic_review_floor` (`settings.py:358`) is documented but never
  read.** Its comment ("below this a semantic hit is not even suggested") implies it gates the tier-8
  semantic suggester, but `matching/engine.py:283` gates on the **general** `review_floor` instead. So
  the knob has been aspirational since `ae5c382` and misleads a reader into thinking the semantic tier
  has its own floor. **Decide intent before acting:** either wire it into `VariationEngine` (and choose
  the default to match current behavior — verify via frozen diff with `enable_semantic=True`), or delete
  the field+comment. Only bites when the semantic tier is enabled (off by default). Do NOT silently
  remove — the comment asserts a behavior.
- **ORC-4 — duplicated scrape-completeness predicate.** `run.find_scrape_file` (`run.py:97-112`) and
  `clean._scrape_is_complete` (`clean.py:33-65`) both independently encode "usable iff `products.csv`
  present and `scrape_complete.json` marker not `complete:false`." `clean.py:33` says "**Mirror
  run.find_scrape_file**" — the author duplicated it *on purpose* so `clean` never deletes a folder the
  pipeline still ingests. Load-bearing invariant that could drift if the marker schema changes.
  **Suggestion (low priority, opportunistic):** extract one `scrape_is_complete(folder) -> bool` into a
  shared module; write a characterization test over a fixture tree (complete / incomplete-marker /
  legacy-markerless / no-products.csv) BEFORE extracting, then output-diff a produce run.

---

## TIER 4 — Leave alone (unwired-but-deliberate, or awareness items — DO NOT remove)

These look like dead code but each has a legible, intentional fence. Per the prime directive, none
should be removed on "no callers" alone. A few are **gaps to close, not complexity to cut** — flagged
for owner awareness.

- **LED-4** — `ledger/sync.py:444` `requeue_dead_lettered` (un-quarantine `gap_held` → `dirty`) is a
  designed operator recovery lever with a test but **no HTTP/CLI entry point** (its observers
  `failures()`/`status()` ARE wired via `GET /sync/v1/failures`). *Awareness:* consider wiring a
  `POST /sync/v1/requeue` — do not delete.
- **CFG-3 / MAT-3** — `enable_splink` flag (`settings.py:356`) + `matching/splink_model.py` (tier-7
  residual linkage): the module exists and is test-covered for the *absent* path, but production never
  reads the flag. Its own docstring calls it "the documented integration point; until wired, residual
  stays in review." The live `alias_resolver` already fills the tier-7 role. **Honest note:** this is
  aspirational deferred infra, not accidental dead code — leave it, but worth a roadmap check: "is
  Splink still planned given alias_resolver exists?" If retired, the module + flag + test go together.
  (Contrast `enable_semantic`, which **is** wired live in `stages/match_variation.py:50`.)
- **ORC-1** — `io/medusa_client.py` (`ImportSink`/`CsvImportSink`/`MedusaApiSink`) is unused by the
  production emit path (`stages/emit.py` writes CSVs directly) but is test-pinned (`test_m12_production`)
  and documents itself as the future API-cutover seam. *Awareness:* its premise (push to Medusa) may be
  overtaken by the committed **pull-model ledger sync** — worth a one-line "superseded by ledger pull"
  docstring or eventual deletion **once the team confirms the push path is dead**. Do not act now.
- **STG-1** — `stages/build_tile_backbone.py` has zero runtime callers but is a documented **manual
  bootstrap tool** (README) to regenerate `backbone_tiles.json` from `backbone_slabs.json` when adding
  a mirror category; the running pipeline mirrors tiles via `emit_catalog`/`curate`. Keep; at most add a
  one-line docstring "manual bootstrap only, not run automatically."
- **ORC-2** — `tree.py` / `images.py` are thin (~5-line) operator CLIs for the human-in-the-loop steps
  (post-export re-run-combinations; image inbox intake), documented in RUNBOOK/CATEGORY_GUIDE. Logic
  lives once in `stages/`; these don't duplicate it. Keep.
- **ADP-2/3/4** — `regenerate_fixture` (adapter-onboarding tool), `strip_variety` (retained as the
  documented *rejected-approach* reference in a test that explains why marenostone uses `strip_format`),
  `code_prefixes` escape hatch (unused by all 4 sources but core-level tested, intentional fallback).
  All keep.
- **FND-03** — `gates/report.py:33` `GateReport.messages` is declared but never written/read.
  **Intent undetermined** (plausibly symmetry with `HealthReport.messages`, which IS written) →
  do NOT remove. One-line delete *if* ever confirmed a copy-paste artifact.
- **STG-3** — "safety-net re-resolve" guards (`derive.py:115`, `match_variation.py:115`,
  `reconcile_tree`) that re-run an earlier stage if a field is unset: no-ops in production
  (`run.py` ordering guarantees the earlier stage ran) but keep stages runnable in isolation for unit
  tests. Intentional decoupling, commented as such.
- **STG-4 / decisions.py naming** — CSV-era names (`*_COLUMNS`, `write_confirm_file`) on a config.db
  facade are a *documented compatibility shim* so the storage swap stayed invisible to `curate.py`.
- **ORC-3** — `lifecycle._snapshot_config` swallows a failed S3 snapshot at `log.debug` (best-effort;
  periodic+atexit snapshot is the backstop). Borderline — the only arguable tweak is `debug`→`warning`
  for observability. Judgment call, not accidental complexity.

**Essential domain complexity confirmed sound (do not touch):** the gate framework and both drift
layers (health.py structural + magnitude_drift.py per-format, deliberately separate baselines); the
matching tier resolver + alias_resolver logistic model + projection index (multi-vendor stone naming is
inherently hard); the entire ledger state machine (leases, two-pass tombstone ordering, reset overlay,
dead-lettering, WAL/snapshot, dormant-lane schema headroom); the config two-store split + seed/reconcile/
tombstone dance; all of `core/` (ids/numbers/csvio/text/manifest/logfmt); `reference/loaders.py` (604
lines, no dead loaders, no duplicated parsing); `run.py`'s abort ladder; SSRF guard; the clean
run⊃build⊃{run,catalog,inventory} orchestration layering.

---

## Recommended order of work (if/when you do a cleanup pass)

1. **Verify FND-04 intent** (env path) — the only correctness-relevant item; do this first, add a test.
2. **Decide CFG-4/MAT-2** (`semantic_review_floor`: wire or delete) — resolves a misleading knob.
3. **Bundle the tier-1 deletes** (LED-1, MAT-1, CFG-1, CFG-2, CFG-5, FND-01) into a single hygiene
   commit — run full suite + one frozen produce diff (expect zero output diff).
4. **Fix the stale docstrings** (LED-2, LED-3) opportunistically.
5. Everything else: leave. Optionally wire the two unreachable recovery levers (LED-4 requeue endpoint,
   ORC-1 API sink) as *features*, and confirm the Splink roadmap (CFG-3/MAT-3) — none of these is cleanup.

**Overall:** this codebase does not have meaningful accidental complexity. The intricacy is
domain-inherent and deliberately designed, the comments do real Chesterton's-Fence work, and the test
suite + determinism give a strong safety net. The honest finding is "basically sound — a handful of
one-liners and one env-path question, nothing structural."
