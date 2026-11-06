# Superseded docs

Point-in-time plans, audits and contracts kept for history. They describe how
things were, not how they work now. For current behaviour read the root design
docs (HOW_THE_SCRAPER_WORKS.md, PIPELINE_OVERVIEW.md, DEV_PROD_PIPELINE.md,
SYNC_LEDGER_DESIGN.md, STANDARDISE_PROCESS.md, DEPLOY.md) and CLAUDE.md.

| file | what it was | status / replaced by |
|---|---|---|
| stone_pipeline_plan_v3_1.md | the original v3.1 build specification | shipped; see the root design docs + the code |
| FULL_CODEBASE_REVIEW.md | early architecture + implementation review | historical record |
| AUDIT_2026-07-31.md | audit and fix plan (2026-07-31) | historical; its fixes shipped |
| FIX_TRACKER.md | remediation tracker for the 2026-07-31 audit + the slab-only root cause | historical; one open item F10 (a single scrape-completeness contract + recheck at catalog) still pending |
| BUGS_FOUND.md | whole-pipeline adversarial bug audit | historical; listed bugs fixed |
| OVERENGINEERING_AUDIT.md | accidental-complexity / over-engineering audit | historical record |
| PIPELINE_MODULARITY_AUDIT.md | pipeline modular-integrity audit handoff | historical; module gates (gates/) shipped |
| ROBUSTNESS_PLAN.md | plan to bring each layer in line with its contract | historical; delivered through the gates + audit waves |
| MATERIAL_AGNOSTIC_REVIEW_2026-08-14.md | material/brand-agnostic refactor review (2026-08-14) | shipped; see DOMAIN_PACK.md, MULTI_BRAND_LAUNCH.md |
| GATE_REQUIREMENTS.md | module-gate seam-contract blueprint | shipped as gates/; see PIPELINE_OVERVIEW.md |
| GPU_ENHANCE_PLAN.md | Real-ESRGAN GPU image-enhancement plan | replaced by the standardised-photo process (STANDARDISE_PROCESS.md) |
| IMAGE_DISCARD_CONTRACT.md | CLIP non-stone image-discard contract | superseded 2026-11-02; non-slab photos are held by the standardiser (STANDARDISE_PROCESS.md) |
| IMAGE_DISCARD_PRODUCER.md | CLIP non-stone image-discard producer notes | superseded 2026-11-02 (STANDARDISE_PROCESS.md) |
| MEDUSA_SYNC_PLAN.md | target-design plan for Medusa sync (not yet built at the time) | built and live; see SYNC_LEDGER_DESIGN.md |
| MEDUSA_SYNC_DEV_TEST.md | scraper-side preconditions + manifest for the first dev sync test | historical; sync is live (SYNC_LEDGER_DESIGN.md) |
| REVIEW_API_CONTRACT.md | frozen new-variant review API contract (scraper to the :4200 admin) | superseded by the one-statement review (REVIEW_DECISIONS.md) + FRONTEND_API.md |
| NEW_VARIANT_REVIEW_PLAN.md | the old mint / reject / alias review model | superseded by one statement per listing (REVIEW_DECISIONS.md) |
| VENDOR_REMOVAL.md | vendor removal and re-add notes | superseded by the entity lifecycle (ENTITY_LIFECYCLE.md) |
| PROD_CUTOVER_RUNBOOK.md | Blokport prod scraper standup cutover runbook | historical record (prod live 2026-09-02); see DEPLOY.md |
| do_do.md | deferred-items scratch list | historical record |
