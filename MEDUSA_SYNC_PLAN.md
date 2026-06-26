# Medusa sync — plan (target design, not yet built)

Goal: a **bulletproof, automated** link from the pipeline to Medusa that removes the
manual CSV import + export-download round-trips. Design only — keep the file-based
flow while the scrapers are still being tested.

## The root friction (why imports/exports exist today)
Combinations and products need Medusa **variant IDs**, and those IDs only exist
*after* a manual variant CSV import + export download. That round-trip forces the
two-pass dance (upload variants → refresh export → build combinations) and is the
fragile part. Remove the round-trip and the manual steps disappear.

## The principle: Keys are the identity; Medusa resolves them server-side
Three ideas, together, eliminate imports/exports:

1. **Address everything by a stable Key, never by Medusa's internal ID.** The pipeline
   already mints deterministic Keys (`{branch}_{type}_{name}_{uuid}`; `uuid5` tree
   nodes). Medusa stores each variant/attribute with that Key as its `external_id`,
   so every entity is addressable by Key from both sides.
2. **Push via the Admin API, upsert-by-Key (no CSV import).** The pipeline creates-or-
   updates by Key — idempotent, so re-runs never duplicate. (Flesh out the existing
   `io/medusa_client.py:MedusaApiSink` stub.)
3. **Medusa resolves Key→ID server-side (no export download).** Combinations and
   products are pushed *referencing Keys*; Medusa swaps Keys→IDs internally when it
   stores them. The client never needs the ULIDs, so there's nothing to download.

Result: "upload variants → refresh export → build combinations" collapses into **one
idempotent push** the pipeline does itself.

## The flow it becomes
```
scrape → produce catalog (all Key-addressed) → ONE atomic sync to Medusa:
   variants upsert-by-Key  →  combinations + products pushed by Key (resolved server-side)
   → Medusa reconciles in a transaction. Re-run anytime; idempotent.
```
No imports, no exports, no two passes — and no "new variant sneaks in between upload
and combinations" window, because it's a single push.

## Ownership (cross-repo — coordinate both chats)
| Scraper side (this repo) | Medusa side (backend repo) |
|---|---|
| Flesh out `MedusaApiSink` (Admin API client) | Store Key as `external_id` on variants/attributes |
| Push Key-addressed payloads | Upsert-by-Key endpoints (variants, products) |
| Sync orchestration + ordering | Server-side Key→ID resolution for combinations |
| Dry-run / diff before apply | One atomic sync endpoint (transactional) + orphan policy |

## Safety (what makes it "bulletproof", not just automated)
- **Trust-gate** (already built): only `mode: auto` certified sources sync
  automatically; new/unproven sources stage for review. Adding scrapers can't push
  bad data live.
- **Dry-run first**: the sync previews the diff (create/update/orphan) before changing
  anything; approve, then apply. Graduate to auto per source.
- **Atomic / transactional**: a partial failure rolls back — Medusa never half-synced.
- **Idempotent everywhere**: upsert-by-Key + the image manifest mean re-running is
  always safe and a no-op when nothing changed.

## Sequencing (stay safe while still testing)
1. Now → keep testing the scrapers with the file flow (it works; don't change it).
2. Add scrapers freely behind the `certify` gate — the Medusa link is source-agnostic,
   so new sources need no special wiring.
3. When ready → build the API sync behind dry-run + per-source `mode: auto`; prove it
   on one source, then flip the rest. The file export stays as a fallback until trusted.

## First concrete step when you move on it
Agree the **Medusa-side endpoints** with the backend chat: upsert-by-Key (variants,
products) + server-side Key→ID resolution for combinations + one atomic sync endpoint.
Everything else is scraper-side. Write the endpoint spec (inputs/outputs, Key
semantics, idempotency contract) as the shared target.
