# Fix Tracker — remediation of the 2026-07-31 audit + slab-only root cause

Source of truth for outstanding fixes so NONE is forgotten. Each fix: isolated branch off `main`,
proper investigation (read source + callers + edge cases before writing), `pytest -q` green, named repro
test added, verify after. One fix per branch (another chat is active in this repo).

Status key: TODO / INVESTIGATING / IN-PR (#) / MERGED / DEPLOYED / DEFERRED

## Slab-only input-model (my proven root cause — deploy layer, NOT the seed file)
- [x] **S0 — Scratch-disk input divergence** — IN-PR `fix/S0-base-s3-authority`. Root cause: the base was written
  locally each produce (`base := 1_variants_full`) but NEVER uploaded to `from_medusa/`, so `fetch_inputs` had
  nothing to pull and a long-lived container's local base accumulated/drifted while a fresh one started baked
  → 4 varieties shipped slab-only. Fix (no seed-DATA edit): (1) `sync_variants_base.publish_base_to_s3` uploads
  the written base to `<env>/scraper/from_medusa/variants_export_base.csv` each produce, so `fetch_inputs` pulls
  the SAME base every run (its `_would_clobber` thin-guard already blocks a thin S3 copy) — S3 is now the single
  source of truth; (2) factory reset `_reseed_base_from_pristine` resets the live base FILE (local + S3) to the
  Docker-baked pristine `.seed.csv`, so a factory reset is a true cold start from the seed. 7 repro tests; reset
  test proves no committed-file mutation. Bootstrap is clean (first post-deploy produce finds no S3 base → baked
  → publishes → converges). See memory `slab-only-root-cause-scratch-divergence`.

## TIER 1 — Critical (audit AUDIT_2026-07-31.md)
- [x] **F1 — inventory computed at load, 6+ sites** [LIVE BUG] — MERGED+DEPLOYED dev #121 (single field derived Stage 6, 7 readers switched, inventory_for deleted, E2/E3 repro tests, suite green). Block policy: same as slabs.
- [x] **F2 — derive_origin resolves wrong homonym's origin** [LIVE BUG] — MERGED+DEPLOYED dev (gate curated lookups on variety_authoritative type; State-2 stamps type_name_fallback; origin_type_unverified flag; repro tests; suite green).
- [x] **F3 — ledger product ACK lost-update** — MERGED+DEPLOYED dev (F3+F4 together; payload_hash round-trip; opt-in, backward compat; repro tests). Blokport: echo payload_hash.
- [x] **F4 — ledger inventory ACK snaps to current not served qty** — MERGED+DEPLOYED dev (with F3; quantity round-trip). Blokport: echo quantity.
- [x] **F5 — FAL cost breaker under-counts** — MERGED+DEPLOYED dev #126 (billed_mp counts every generation incl retries/failures; repro test).
- [x] **F6 — magnitude-drift baseline merge per-field** — MERGED+DEPLOYED dev (deep-merge inner field dict; repro test).
- [x] **F7 — batch name-strip corrupts short leading words** — MERGED+DEPLOYED dev (looks_codey vowel+known-lead rule; unconditional _lead_codes reset; repro test).

## TIER 2 — Systemic roots
- [~] **F8 — cross-process shared-state** — atomic_write unique-temp MERGED #129. REMAINING under verification: S3 manifest CAS, config.db UNIQUE, baselines lock, upsert txn (only fix the PROVEN-concurrent ones).
- [~] **F9 — audit every `except Exception` vs fail-loud** — certify.py:84 + writethrough:110 MERGED #128. REMAINING under verification: run.py:422 inventory-export, S3 AccessDenied helpers, schema extra=forbid.
- [ ] **F10 — one scrape-completeness contract + completeness recheck at catalog** — TODO.
- [ ] **F11 — gate-abort contract (clean/process FAILED should abort or fix doc)** — TODO.

## TIER 3 — Correctness mediums (sequence by exposure)
- [ ] Fuzzy match auto-written-back → permanent EXACT/HIGH alias (match_variation.py:209)
- [ ] Weight parser dimension-blind (derive.py:119)
- [ ] No per-row exception isolation in match/reconcile/derive (derive.py:620)
- [ ] Retired varieties still get combinations (tree_build.py:399) — test retired KEYS not vid
- [ ] Variety vanishes when dedup survivor independently excluded (loaders.py:300)
- [ ] De-watermark inpaints pink/red veining (image_processing.py:170)
- [ ] Served product type/category from product row while gate checks variation (sync.py:254)
- [ ] inventory_for prefers bundle_size over explicit raw_inventory_quantity (folds into F1)
- [ ] _env_int crashes on `--3` (settings.py:76); non-ASCII Authorization→TypeError not 401 (server.py:375); migration parity hand-mirrored (store.py:74)

## TIER 4 — Long tail (42 lows)
- [ ] See FULL_CODEBASE_REVIEW.md "LOW findings". Sequence after Tiers 1-3.

## F13 — FAL breaker global not per-process (with F5)
- [ ] reprocess_source.py:109 + no ceiling on Stage-7 :core path (images.py:411)

## Do NOT regress (verified correct-by-design) — see AUDIT_2026-07-31.md bottom list
SSRF revalidation; parameterized SQL + constant-time token; content-addressing on source bytes; HOLD-never-guess
for freight/pricing/images; type authority pinned to Key; single match_key + one dedup rule; SQLite backup snapshots;
template-is-schema-authority; Medusa CSVs NOT formula-sanitized; no-live-index alias write-back; verify_consistency
on full tuples; 30%-delist guard + scrape-floor abort; validate.py three-way dimension policy.
