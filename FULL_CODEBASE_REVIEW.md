# Full Codebase Review — Architecture & Implementation

**Scope.** Application code only (scrapers, adapters, core, pipeline stages, matching/reference, ledger/sync, image pipeline, config/admin, orchestration). Excludes infra/deploy (Terraform/Docker) and the test suite, per request.

**Method.** 11 subsystems each got a deep architecture + implementation review at high effort (correctness, security, error handling, concurrency, performance, determinism, dead code). Every **high/critical** finding was then hit by 2 independent skeptics prompted to *refute* it; a finding only survives if not refuted. **31 agents, 0 errors, ~1.8M tokens.**

**Result of verification.** Surviving: **0 critical, 5 high, 40 medium, 42 low.** 1 high finding was refuted and dropped; 1 (temmer truncation) was downgraded high→medium by the verifiers. High/critical findings below carry a `[VERIFIED n/2]` tag; medium/low were not adversarially verified (treat as "likely, needs a quick confirm").

---

## Overall verdict

This is a **genuinely well-architected, senior-level codebase.** The reviewers independently praised, across subsystems: strong determinism discipline (sha-based ids, seeded RNG, atomic writes, crc32-not-`hash()`), correct security fundamentals (per-hop SSRF revalidation on redirects, parameterized SQL everywhere, constant-time token compare, secrets env-only), a single-normalizer identity contract that provably keeps index and vocab in sync, and real correctness mechanisms — **HOLD-never-guess** (never fabricate a freight/pricing basis), **content-addressing on source bytes**, **type-authority pinned to the Key** (not to product assertions), and a complete row-level validation gate.

It is **not** clean-to-a-fault, though. The findings cluster into a **small number of recurring, systemic patterns** — not scattered one-offs, and not "bad code." They are the specific seams where a well-built *single-process* design meets a *multi-process deployment* and adversarial inputs:

1. **Cross-process concurrency (the dominant systemic risk).** The code repeatedly assumes a single sequential writer, but the deployment runs overlapping processes — a scheduled Fargate produce, a manual/laptop `build`, and on-demand GPU Batch jobs — against the same S3 manifest, the same baseline JSON files, the same ledger, and the same `config.db`. Every one of those shared-state read-modify-writes is last-writer-wins with no CAS/lock. This is the root of two of the five high findings and ~8 mediums.
2. **Silent-degrade against a stated fail-loud ethos.** The project's own invariant is "fail loud, never guess." Yet several boundaries swallow exceptions or degrade silently: `certify` coerces any evaluation error to PASS, `record_catalog` swallows write-through failures, S3 helpers collapse AccessDenied into a benign heuristic, an inventory-only run with a missing export reports success, the schema silently drops misnamed fields (already losing a live `ReviewFlag detail=`), and non-Latin names collapse to one colliding empty key.
3. **The "authoritative scrape" completeness contract is enforced inconsistently.** Whether a scrape is complete-enough to be treated as latest (and therefore to *delist* what it omits) is decided differently by each scraper and not re-checked at catalog time — so a truncated scrape can silently delist real inventory.
4. **Safety breakers that don't fully hold.** The FAL cost breaker is per-process and blind to retries/failures; the magnitude-drift gate has a per-field blind spot that can let a 1000× unit error through; the only universal net is the 30%-delist guard.

None of these make the system "unsafe today" in the common path — most are latent, edge-triggered, or masked by a downstream guard. But they are real, they share roots, and fixing the roots (a shared-state locking strategy; an audit of every `except Exception` against fail-loud; one completeness contract in the base) would resolve the majority.

---

## The 5 verified HIGH findings (fix first)

### H1 — Ledger product ACK marks a row synced after a concurrent write-through re-dirtied it (lost update) `[VERIFIED 2/2]`
`ledger/sync.py:415`. The synced-flip gates only on `state != 'retiring'`; it does **not** require the row to still be `syncing`, nor compare the served `payload_hash` against the current one (the hash is served but never checked back). `populate_products` runs in a *different process*, not behind `_lock_and_check_in_flight`, and re-dirties on a content change.
**Failure:** pull serves product P (content A, leased `syncing`); a produce rewrites P to content B (→`dirty`, hash B stored); Medusa applies A and acks; ack flips P→`synced` with hash B stored, so P never re-serves. **Medusa holds A forever; ledger believes B is live.**

### H2 — Ledger inventory ACK snaps stock to the CURRENT qty, not the served qty `[VERIFIED 2/2]`
`ledger/sync.py:384`. `ack('inventory')` does `SET last_synced_qty = qty`; the ack carries no quantity to check. Inventory rows are never leased, and `populate_inventory` can move qty between serve and ack.
**Failure:** serve sku=X at qty 5; a produce moves qty→8 before the ack; Medusa applies 5 and acks; ack sets `last_synced_qty=8=qty`, so the delta vanishes. **Medusa has 5, ledger says synced-at-8; the qty-8 change never ships → oversell/understock.**
*(H1+H2 share one root: the ACK is authoritative over the row's current contents, not the version served. Fix together — round-trip the served `payload_hash`/qty and only flip synced if it still matches.)*

### H3 — FAL cost accounting ignores retries and failed/blank generations → breaker under-counts `[VERIFIED 2/2]`
`io/image_processing.py:231`. `billed_mp` is set once for a *successful* fill and is `0` on `failed=True`, but `_fal_fill` retries up to 3× and a blank/refused output triggers another billed generation. Reprocess sums `billed_mp*price` as its only cost signal.
**Failure:** FAL degrades / returns safety-refused blanks; every image is generated 3× (billed 3×) then reports `billed_mp=0`; `fal_cost` stays $0; the breaker never trips; **the run silently bills for thousands of generations producing nothing.**

### H4 — Magnitude-drift baseline merge is per-format not per-field → a field can silently lose its drift guard `[VERIFIED 2/2]`
`stages/magnitude_drift.py:142`. On an OK run, `merged = dict(baseline); merged.update(current)` — `dict.update` replaces the *entire inner* `{field→median}` for any present format. If a format is present but one field is absent this run (e.g. all weights null), that field's baselined median is discarded.
**Failure:** baseline `slab={weight:0.3,length:3.0}`; Run A omits weights → merged `slab` loses `weight`; Run B ships weights with a kg↔tonne bug at 300 (1000×) → `weight` isn't in baseline, the FAIL-factor check never runs, status OK, 300 re-seeded as "good." **The exact 1000× unit rescale the gate exists to stop ships uncaught.**

### H5 — Batch-discovered code-stripping corrupts legitimate short leading words `[VERIFIED 2/2]`
`core/text.py:197` (via `adapters/base.py:180`). `looks_codey` treats *every* 2-letter leading token as a code; when it fans out over ≥2 varieties in a batch, `clean_variety_name` strips it from all rows — contradicting the function's own docstring ("`La Perla`, `El Dorado` untouched").
**Failure:** a batch with `St Laurent` + `St Tropez` → `st` becomes a lead-code → both `variety_match_key`s truncate to `Laurent`/`Tropez` → wrong match or a spurious mint under a corrupted name. (Also filed in the modularity audit; adversarially confirmed here.)

---

## Significant MEDIUM findings (grouped by root cause)

### Cross-process concurrency (single-writer assumption vs overlapping deploy processes)
- **S3 image manifest is a whole-file overwrite, no CAS** — `stages/images.py:105` and `stages/treat.py:82` both rewrite it. A produce that loaded before a concurrent `treat` write silently reverts all of treat's repoints → products re-link to stale/raw URLs, invisibly.
- **Baselines + alias_writeback are read-modify-write, no lock** — `state/writeback.py:54`, `magnitude_drift` save, scrape baselines. Overlapping produce/build clobber each other's per-source entries → a source's drift baseline silently regresses.
- **`source_code` uniqueness is a racy app-check, no DB UNIQUE** — `config/server.py:347`; two concurrent PUTs create duplicate source_codes → one source's delist scopes the other's products and pollutes the 30%-cap denominator.
- **`config/store.upsert_row` splits an atomic write across two transactions** — `store.py:268`; a crash between them leaves a source row present but still tombstoned.

### Silent-degrade vs fail-loud
- **`certify` vocab check swallows all exceptions → returns PASS** — `certify.py:84`. The CI trust gate that guards review→auto promotion coerces any evaluation error (including one caused *by* the mis-mapped column it's meant to catch) to a passing "skipped". **A swapped attribute column can be green-lit to auto.**
- **`record_catalog` swallows write-through failures** — `ledger/writethrough.py:110` (contrast `record_source` which surfaces them). A raise mid-produce means new/updated varieties never reach the variation table, produce reports success, and their products stall forever on the `v.state='synced'` gate with no signal.
- **Catalog has no scrape-completeness guard** `[VERIFIED 2/2 medium]` — `catalog.py:40`. `latest_run_dirs` picks the lexically-newest run folder with no completeness check (unlike `run.find_scrape_file`). An aborted scrape leaves an empty newer folder; catalog drops the source from the upload set **and `prune_superseded_runs` deletes the last-good folder** → a source silently vanishes and its recoverable data is destroyed.
- **Inventory-only run with a missing export is a silent success** — `run.py:422`. Absent `from_medusa` export → no `inventory_update.csv` → header-only deliverable → exits 0 printing "0 changed". A real infra failure is indistinguishable from "nothing changed"; **stock movements silently dropped.**
- **S3 boundary helpers swallow AccessDenied** — `emit_catalog.py:63`, `variety_color.py:146`, `treat.py:47`. A real IAM misconfig collapses into a benign heuristic → can advertise `{Key}.png` links for images that 404, or ship a watermarked source un-de-watermarked.
- **Schema silently ignores unknown/misnamed fields** — `core/schema.py` (Pydantic default `extra='ignore'`). **Already live:** `ReviewFlag(..., detail=...)` at `product_state.py:107` — `ReviewFlag` has no `detail` field, so every `stock_unparseable` flag silently loses its explanation.
- **Only the ingest gate aborts on FAILED** — `run.py:371`. `gates/__init__` documents "a FAILED gate aborts before emit," but clean/process gate FAILED status is recorded and discarded. A whole-batch origin regression → PROCESS gate FAILED, yet the run emits the ~10% survivors and reports completed.

### The "authoritative scrape" completeness contract
- **temmer treats any fetch error as end-of-pagination** — `scrapers/temmer.py:171`; a transient 502 on page 3 truncates the category and records COMPLETE → downstream delists the omitted products (bounded by the 30% guard).
- **Offset scrapers treat an empty-200 as end-of-catalog, no total cross-check** — `varsha.py:146`, `ferraz`, `polonine`. A backend load-shed empty page mid-catalog ends pagination COMPLETE.
- **No fractional scrape-floor** — `base.py:327`. Only `>0` rows; a run where 40% of per-product detail fetches failed ships hollow rows (blank dims/colour/photos) as COMPLETE.
- **`REGISTRY` built eagerly at import** — `scrapers/run.py:50`; one malformed scraper module breaks the whole runner, defeating source isolation.

### Correctness / data-integrity
- **Fuzzy match auto-written-back becomes a permanent EXACT/HIGH alias** — `match_variation.py:209`. A machine-accepted fuzzy hit (medium+flag) is persisted as a "confirmed" spelling and resolves at EXACT/high with no review next run — latching a false positive until the id churns.
- **Weight parser is dimension-blind** — `derive.py:119`; `_parse_measure` reuses the length parser and `Units.convert` ignores `entry.dimension`. A bare tonne value `20` → 20 kg (1000× too light) drives freight pricing; only sometimes range-flagged.
- **No per-row exception isolation in match/reconcile/derive** — `derive.py:620` et al.; one row that raises (e.g. `int('²')` — `isdigit()` True, `int()` raises) drops the **entire source batch**, violating the "one dead row never crashes the run" invariant.
- **Retired varieties still get valid combinations (Key vs Id space)** `[VERIFIED 2/2 medium]` — `tree_build.py:399`; `exclude_ids` is loaded with variety **Keys** but tested against export **Ids**, so the exclusion is inert — retired varieties are re-priced until Medusa deletes them.
- **A variety can vanish from the matcher when its dedup survivor is independently excluded** — `loaders.py:300`; if the chosen survivor Key is retired/mistyped-excluded, no loser is promoted → the stone matches nothing → a duplicate can be minted for a variety that already exists.
- **De-watermark locator inpaints real pink/red veining** — `image_processing.py:170`; a naturally pink stone exceeds the pink-ink threshold, so FAL hallucinates a fill over actual veining and ships it marked "cleaned."
- **Served product type/category read from product row while gate checks the variation** — `sync.py:254`; a product populated while untyped keeps `p.type=''` (not in the hash, never re-dirties), so a typed variety can serve a product with empty type/category to Medusa.
- **`inventory_for` prefers derived `bundle_size` over explicit `raw_inventory_quantity`** — `product_state.py:78`; publishes the packaging size as stock. (Ties into the modularity audit's inventory finding.)

### Config / runtime robustness
- **`_env_int` crashes config import on `--3`-style input** — `settings.py:76`; `lstrip('-')` passes `isdigit()` but `int('--3')` raises, aborting *every* entrypoint at import despite a documented fail-soft.
- **Non-ASCII `Authorization` header → unhandled TypeError (not 401)** — `config/server.py:375`; `hmac.compare_digest` on non-ASCII str raises before the try/except → traceback flood, broken response.
- **Migration parity is hand-mirrored with no safety net** — `store.py:74`; a column added to `_SCHEMA` but forgotten in `_migrate` KeyErrors every pre-existing `config.db` at read time.

---

## LOW findings (42) — compact list
Grouped; each is `file` + one-line. These are latent/edge/maintainability.

**Determinism/dead-code:** non-deterministic surface ordering from `set()` into the index (`index.py:124`); dead `surface_to_id` field kept for a test (`loaders.py:255`); dead product-type accumulation in combinations (`tree_build.py:260`); dead value-shape rename diagnosis, only header-similarity runs (`health.py:156`); superseded `MedusaApiSink` push path retained referencing the flat image list (`medusa_client.py:50`); stale inverted ordering comment in normalize (`normalize.py:156`).

**Parsing/edge:** `parse_number` drops leading-dot decimals `.5`→5.0 (`numbers.py:13`); dash-joined compounds never resolve (`engine.py:127`); tile→slab dimension bucket via plural fallback (`derive.py:135`); non-Latin names → empty identity key collision (`text.py:57`); trailing lone-letter grade strip mangles new varieties (`text.py:228`); origin_map drops type-less single-origin rows (`loaders.py:659`); `origin_city` copied from rule while country may differ (`derive.py:436`).

**Concurrency/atomicity:** `atomic_write` fixed `.tmp` name unsafe under concurrent writers (`csvio.py:37`); `smoke_count` mutates shared singleton `_lead_codes` (`base.py:214`); process-global `_TYPE_SLUGS` never invalidated across envs (`loaders.py:36`).

**Ledger:** global reset nulls attribute ids with no restore lane (`sync.py:597`); reset doesn't clear stale tombstones → can delete a variety it meant to preserve (`sync.py:582`); GET `/sync/<type>` mutates (leases) → a retried/truncated GET strands a page for 900s (`sync.py:196`); discontinued product with NULL medusa_id re-serves a qty-0 delist indefinitely (`populate.py:259`).

**Scrapers/adapters:** brumagran bypasses base HTTP (no SSRF/throttle, unbounded Retry-After) (`brumagran.py:214`); offset pagination skip/dup over live inventory (`varsha.py:148`); zucchi trusts `contagemProds`, under-report truncates COMPLETE (`zucchi.py:101`); polonine status reports hidden badges (`polonine.py:72`); onboarding fill-floor 0.0 disables a required column's health check (`contracts.py:84`); `raw_name` vs cleaned `variety_match_key` divergence on recovery path (`base.py:154`).

**Config/health:** unhandled ValueError on malformed Content-Length + unbounded body read (`server.py:387`); `set_attribute_id` accepts any `kind` unchecked (`decisions_store.py:214`); `BLOKPORT_ENV` not allowlisted — a typo runs prod in dev namespace (`settings.py:39`); `_migrate` re-runs ~15 DDL/PRAGMA per connection (`store.py:77`); health thresholds are inline magic numbers (`health.py:218`); volume check guards collapse only, never explosion (`health.py:171`); near-dup key conflates tile with slab (`keys_dedupe.py:96`); manual attribute override writes non-canonical name / silent null id (`normalize.py:86`).

**Image/emit:** FAL breaker per-process → concurrent windows multiply the ceiling ~10× (`reprocess_source.py:109`); no FAL ceiling on the Stage-7 `:core` path (`images.py:411`); self-heal issues one S3 HEAD per known image every run, O(catalog) (`images.py:353`); GPU-deferral HOLD depends on `keep_scraped` (defaults False, coupling unenforced) (`images.py:419`); FAL upload uses deprecated `tempfile.mktemp` (`image_processing.py:242`); PNG/WebP stored as `.jpg` with `image/jpeg` type (`images.py:401`); no template-column guard, header drift emits silent blanks (`emit.py:126`); `product_changed` skips blank export stock / fires on junk (`product_state.py:124`); `adopt_attribute_ids` appends CRLF into an LF committed file (`decisions.py:189`); fuzzy colour-set rebuilt per candidate (`engine.py:40`); `_read_backbone` KeyErrors on shape drift (`tree_build.py:63`); override variation_id accepted cross-branch (`match_variation.py:167`); write-back keyed by churn-prone id evaporates on re-mint (`match_variation.py:210`); build/produce flag parsing consumes a following flag as a value (`build.py:51`); `clean` counts as removed even when rmtree fails (`clean.py:123`); `run_id` constant-timestamp fallback collapses distinct scrapes (`run.py:67`).

---

## Per-subsystem architecture assessment (one line each)

- **scrapers** — Clean template-method (base owns all the dangerous generic work). Fault lines are the inconsistent completeness contract and eager-import registry, not the happy path.
- **adapters** — Clean declarative fan-in; vocab recognition is genuinely well-boundaried. Two leaks: adapter-side numeric derivation and the batch-wide name cleaning (H5).
- **core** — Solid foundational value layer, excellent determinism/CSV-injection discipline. The "boundary validation" and "one canonical key" contracts are weaker than the docstrings claim (silent field drop; non-Latin collapse).
- **clean-stages** — High cohesion, source-isolation holds, the two drift gates are a nice symmetric pair — but both have blind spots (H4; growth never guarded) and shared-file races.
- **transform-stages** — A clean trust-ordered ladder; type authority is the strongest part. Risks concentrate in the write-back feedback loop and silent-fill paths.
- **emit-catalog** — Strong spines (template-authority, single dedup rule, Key-pinned combination types). Risks in the seams: Key/Id space confusion, no template-column guard, broad `except`.
- **matching-reference** — Cohesive, one-normalizer discipline is real. Cost lives entirely in the un-indexed fuzzy fallback; a couple of silent-degrade loaders.
- **ledger** — Well-bounded transport-free state store, sound lease/reap design, injection-safe, page-consistent snapshots. The dominant risk is ACK-authoritative-over-current-contents across processes (H1/H2).
- **image-pipeline** — Well-layered, `imagestore` is a real single-source-of-truth, content-addressing is correct, HOLD-never-publish is consistent, SSRF done properly. Risks cluster in cost + concurrency (H3, manifest race, per-process breaker).
- **config-admin** — Well-layered control plane, careful prod/dev safety, correct secret handling, no injection surface. Risks in migration fragility, racy uniqueness, thin transport error handling.
- **orchestration** — Clean two-tier design, the delist/floor guards and `verify_consistency` are exactly right. Biggest weakness: catalog's completeness-blind "newest folder" selection + prune.

---

## Remediation roadmap

**Tier 1 — fix now (the 5 high + their shared roots):**
1. **Ledger ACK correctness (H1+H2):** round-trip the served `payload_hash` (products) and served qty (inventory); only flip `synced` if the current value still matches what was served. This is the single highest-value fix.
2. **FAL cost accounting (H3):** count every submitted generation (including retries and failed/blank) toward `billed_mp`/`fal_cost`; make the breaker global (see Tier 2 concurrency) not per-process.
3. **Magnitude gate per-field merge (H4):** deep-merge per field, never drop a baselined field's median because it was absent one run.
4. **Batch name-strip (H5):** exclude short *pronounceable* leads (2-char with a vowel) from `looks_codey`; reset `_lead_codes` unconditionally.

**Tier 2 — the systemic roots (resolve many mediums at once):**
5. **Cross-process shared-state strategy:** the manifest, baselines, alias_writeback, config uniqueness, and the ledger all assume one writer. Pick a discipline (S3 CAS/If-Match + ETag for the manifest; a DB UNIQUE index for source_code; a lock or single-writer invariant for baselines) — this closes ~6 mediums + 4 lows.
6. **Audit every `except Exception` against fail-loud:** certify, record_catalog, the S3 helpers, inventory-only-missing-export, catalog-completeness — each should distinguish "genuinely unreachable" from "misconfigured" and surface the latter.
7. **One scrape-completeness contract in the base** + a fractional floor + a completeness re-check at catalog time (mirror `find_scrape_file`) before prune deletes the last-good folder.

**Tier 3 — the rest:** the correctness mediums (fuzzy auto-writeback, weight dimension-blindness, per-row isolation, retired-combinations Key/Id, matcher survivor-vanish, pink-stone inpaint) and the long tail of lows, sequenced by exposure.

---

## What is genuinely strong (do not regress)
Per-hop SSRF revalidation; parameterized SQL + constant-time token compare + env-only secrets; content-addressing on source bytes; HOLD-never-guess for freight/pricing/images; type authority pinned to the Key; the single `match_key` normalizer + one dedup rule shared by matcher/emit/ledger; SQLite backup-API snapshots; the template-is-schema-authority emit; `verify_consistency` on full (type,variation,finish,colour,quality) tuples; the 30%-delist guard + scrape-floor abort; the deliberate no-live-index-alias-writeback (batch-order determinism). These are the load-bearing correctness/security decisions and they are sound.

---
*Companion document: `PIPELINE_MODULARITY_AUDIT.md` (stage-boundary/contract audit + the inventory-at-load and origin-homonym deep-dives).*
