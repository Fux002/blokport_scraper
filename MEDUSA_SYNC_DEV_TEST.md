# Dev sync test: preconditions, the manifest to seed, and the one gap

The scraper-side answers to the dev backend's questions before we run the first
`/sync/v1` dry-run. No em dashes (repo convention).

## The gap that changes step order (read first)

The `/sync/v1` server (`stone_pipeline.ledger.server`) is code-complete and proven
(live HTTP round-trip, byte-identical equivalence, convergent sync loop). But it is
**not deployed as a reachable service yet.** The current dev deploy (`infra/`) is a
**scheduled EventBridge -> Fargate RunTask batch**: it runs `scrape -> run -> catalog
-> upload` and exits. There is no `aws_ecs_service`, no ALB, no published port, and
the ledger it writes lives on the task's ephemeral local disk and dies with the task.

So "confirm the pipeline's `/sync/v1` API is live and serving dev's catalog" cannot be
true until we stand the server up somewhere reachable, pointed at a **persistent,
populated** dev ledger. Two ways:

- **A. Tunnel (fast, for THIS first dry-run).** Run the server locally against the real
  `ledger/development.db`, expose it over a tunnel (cloudflared / ngrok) -> a temporary
  HTTPS URL the dev backend can reach today. Zero infra change. Throwaway.
- **B. Deploy a persistent sync service (productionization).** Add a small always-on
  Fargate **service** (not a scheduled task) on :8723 behind internal service-discovery
  in the dev VPC, with the ledger on durable storage. This is the real end state; it is
  a Terraform add + a decision on ledger persistence (the design says local disk, never
  EFS, so a persistent service needs RDS or a single-writer volume). Do this once the
  loop is proven with A.

Recommendation: **A now to prove the loop, B to make it permanent.**

## Precondition checklist

### Scraper side (mine)
- [ ] Populate the dev ledger with the real catalog: `BLOKPORT_LEDGER_WRITETHROUGH=1`
      run so `ledger/development.db` holds products + combinations + inventory (today it
      has only the id foundation: 24,749 variations, 115 attributes, 0 products).
- [ ] Start `stone_pipeline.ledger.server` (needs `BLOKPORT_SYNC_TOKEN`) against it.
- [ ] Expose it (A or B) and hand over the URL.

### Dev backend side (theirs)
- [ ] `SCRAPER_SYNC_ENABLED=true`, `SCRAPER_SYNC_URL=<the URL>/sync/v1`, `BLOKPORT_SYNC_TOKEN`.
- [ ] `SCRAPER_CONFIG_URL=<config URL>/config/v1`, `BLOKPORT_CONFIG_TOKEN` (for the :4200 admin).
- [ ] Seed the vendors + ports below (attributes already resolve, see the manifest note).
- [ ] Read access to the product images for ingestion (see "Images" below).

### Images: real, but in a PRIVATE bucket
The product `image_urls` are full links into `blokport-dev-staging-3e58a6` (2,765 improved
images, ~815 MB, already staged). The objects EXIST, but the bucket is private, so an
anonymous GET returns 403. Medusa's image-ingestion (download + re-host) needs read access.
Cleanest: grant the dev Medusa task role `s3:GetObject` on
`arn:aws:s3:::blokport-dev-staging-3e58a6/dev/products/*`. Alternatives: presigned URLs, or
making that prefix public-read. Product data (sizes, attributes, vendors) does NOT need this;
only image ingestion does, so it need not block the first size-correction dry-run.

### Secrets
Both `BLOKPORT_SYNC_TOKEN` and `BLOKPORT_CONFIG_TOKEN` are shared secrets: generate once,
store as dev SSM SecureStrings, reference from BOTH the scraper server and the dev backend.
```
python -c "import secrets; print(secrets.token_urlsafe(32))"   # generate each
aws ssm put-parameter --name /blokport-dev/BLOKPORT_SYNC_TOKEN   --type SecureString --value '<token>'
aws ssm put-parameter --name /blokport-dev/BLOKPORT_CONFIG_TOKEN --type SecureString --value '<token>'
```
The token must be byte-identical on both sides or every pull returns 401.

## Resolution manifest: what the scraper will send

The scraper sends agnostic NAMES; the dev Medusa resolves them. Anything unresolved lands
in the review queue. Seed these before the dry-run.

### Vendors / companies  [NEW concept -> the real seeding risk]
Distinct `vendor` across the enabled sources. These MUST exist as companies in dev Medusa:
- **Marenostone** (origin IT)
- **Polonine** (origin IT)
- **Varsha Stones** (origin IN)
- **Zucchi** (origin IT)

### Ports / origins  [check these exist]
- Ports (names, may become UN/LOCODEs later): **Brindisi**, **Civitavecchia**
- Origin countries (ISO-2): **IT**, **IN**

### Attributes  [resolve BY CONSTRUCTION - verify only]
Colors, finishes, qualities, types, and categories are matched against the dev Medusa's
OWN export (`from_medusa/development/attributes.csv`) during the pipeline run, so the
scraper only ever emits a value that is already in that export. As long as that export is
the current dev Medusa state, these resolve with zero review-queue misses:
- **category** (3): Slabs, Blocks, Tiles
- **color** (26): Beige, Black, Blue, Bordeaux, Bronze, Brown, Copper, Cream, Gold, Golden,
  Green, Grey, Ivory, Lilac, Multicolor, Natural, Orange, Orchid, Pink, Purple, Red, Rose,
  Semi, Silver, White, ...
- **finish** (51): Acid Washed, Antique, Brushed, Bush Hammered, Chiseled, Flamed, Honed,
  Polished, Sawn, Tumbled, ... (full set in the export)
- **quality** (4): A, B, C, D
- **type** (31): Agate, Alabaster, Amethyst, Andesite, Basalt, Bluestone, Cantera,
  Conglomerate, Coral Stone, Crystal, Dolomite, Granite, Limestone, Marble, Onyx, Quartz,
  Quartzite, Sandstone, Slate, Travertine, ... (full set in the export)

Type and category are intrinsic to the variation (the identity triple); the product only
chooses color / finish / quality. So a product can never introduce a NEW type/category to
Medusa; it inherits an already-synced variation's.

## The dry-run sequence (matches the backend's plan)
1. Wire dev to the scraper URL + token (backend infra change + redeploy).
2. Confirm `GET /sync/v1/status` returns 200 and shows a non-empty catalog.
3. Catalog sync ONCE with old data in place: upsert-by-SKU corrects slab sizes in place,
   adds new, destroys nothing. Watch products load, attributes/vendors/ports resolve.
4. Delete only the genuinely-stale products the sync did not touch.
5. Verify: sizes correct, quote-only, review queue empty of surprises.
