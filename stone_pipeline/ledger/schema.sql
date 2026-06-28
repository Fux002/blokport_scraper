-- Sync ledger schema (Phase 1).
--
-- This is the data model from SYNC_LEDGER_DESIGN.md section 4, expressed as DDL.
-- One database PER ENVIRONMENT (a dev ledger and a prod ledger); the env is never
-- mixed. Target for Phase 1 is SQLite on the task's LOCAL disk with an S3 snapshot
-- (never EFS, see design section 12 / M4). The DDL is written to lift cleanly to
-- RDS Postgres later: only TEXT/INTEGER/REAL, CHECK constraints instead of native
-- enums, ISO8601 TEXT timestamps, and JSON stored as TEXT. No em dashes (design
-- principle 2).
--
-- Refinements vs section 4 (DDL forced these to be precise; flagged for review):
--   1. Env guard: section 4 says "all tables carry env." Realized here as a single
--      `ledger_meta` row holding the env + fingerprint, asserted by the DAL at every
--      connection open. Same guarantee (a misrouted connection aborts), without an
--      env column duplicated on every row. One DB per env makes per-row env moot.
--   2. `variation.image_url` stores the live TEXTURE url directly (the CSV Image
--      cell; empty when no texture exists), because a variant texture is
--      Key-addressed (`{Key}.png`), not content-hash addressed. `image_sha256` is
--      reserved for a future content-addressed texture lane. Scraped PHOTOS, which
--      are content-hash addressed, still derive their live url from `image.live_key`
--      (one source of truth for that lane) and link via `product.image_shas`.
--   3. `combination.category` and `combination.type` store the canonical NAMES (the
--      design calls the category dimension "category_pcat"). Names are re-seed safe;
--      the pcat id is resolved at the boundary. combo_key hashes the name tuple.
--   4. `gap.id` and `sync_run.id` are DAL-generated TEXT ids (ULID-style), not
--      auto-increment, to stay dialect-neutral.
--
-- payload_hash inputs are documented per table (design section 4 / M3). The DAL
-- computes payload_hash and combo_key with a stable canonical serialization so the
-- same desired state always hashes the same (determinism).

PRAGMA foreign_keys = ON;   -- SQLite: enforce the FK declarations below.

-- ---------------------------------------------------------------------------
-- ledger_meta: the env guard (one row). The DAL asserts RunContext.env == env
-- and the fingerprint at every open, so a dev process can never write a prod
-- ledger or vice versa (design section 2 cross-env guard).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ledger_meta (
    id                     INTEGER PRIMARY KEY CHECK (id = 1),   -- single row
    env                    TEXT    NOT NULL CHECK (env IN ('development', 'production')),
    backend_id_fingerprint TEXT,                                 -- hash of the live id set
    schema_version         INTEGER NOT NULL,
    created_at             TEXT    NOT NULL,
    updated_at             TEXT    NOT NULL
);

-- ---------------------------------------------------------------------------
-- variation: one row per canonical variety, addressed by the deterministic Key
-- (Medusa external_id). A type or canonical-name correction is a NEW Key (a
-- re-key, design section 5C), not an update of this row.
--
-- payload_hash covers: (branch, type, name, sorted(aliases), image_url, volume)
--   where image_url is the live url derived from the linked texture image.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS variation (
    key           TEXT PRIMARY KEY,                    -- {branch}_{slug(type)}_{slug(name)}_{uuid5}
    branch        TEXT NOT NULL CHECK (branch IN ('slab', 'block', 'tile')),
    type          TEXT NOT NULL,                       -- canonical stone type name
    name          TEXT NOT NULL,                       -- canonical variety name
    aliases       TEXT,                                -- JSON array of strings
    image_url     TEXT,                                -- live texture url (the CSV Image cell); empty if none
    image_sha256  TEXT,                                -- reserved: content-hash if texture is ever content-addressed
    image_model   TEXT,                                -- generator of the current texture, null if none
    volume        TEXT,                                -- "Volume per kg (m3/kg)", kept as text for exactness
    medusa_id     TEXT,                                -- null until synced
    payload_hash  TEXT,
    state         TEXT NOT NULL DEFAULT 'pending'
                  CHECK (state IN ('pending','dirty','syncing','synced','needs_resync','gap_held','retiring')),
    first_seen    TEXT NOT NULL,
    last_synced   TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_variation_serve ON variation (state, created_at, key);
CREATE INDEX IF NOT EXISTS idx_variation_branch ON variation (branch);

-- ---------------------------------------------------------------------------
-- attribute: the controlled vocabulary. Existing values are refreshed from
-- Medusa; NEW values discovered by the pipeline start `pending` and HOLD their
-- dependents until synced (design C3). Keyed by (category, value).
-- No payload_hash: an attribute is identified by its (category, value); it has no
-- other mutable content to diff.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS attribute (
    category    TEXT NOT NULL CHECK (category IN ('color','finish','quality','type','category')),
    value       TEXT NOT NULL,                         -- canonical value (exact spelling Medusa resolves on)
    medusa_id   TEXT,                                  -- null until synced
    state       TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending','dirty','syncing','synced','needs_resync')),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (category, value)
);
CREATE INDEX IF NOT EXISTS idx_attribute_serve ON attribute (state, created_at, category, value);

-- ---------------------------------------------------------------------------
-- combination: a priceable tuple. category and type ARE dimensions (design C1),
-- so combo_key hashes the full tuple and they are stored explicitly. Matches
-- 2_valid_combinations.csv (product_category_id, type_id, variation_id, finish_id,
-- color_id, quality_id), resolved to ids by name at the boundary.
--
-- combo_key = hash(category, type, variation_key, color, finish, quality)
-- payload_hash covers the same tuple (a combination has no other content).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS combination (
    combo_key      TEXT PRIMARY KEY,
    category       TEXT NOT NULL,                       -- canonical category name (design "category_pcat")
    type           TEXT NOT NULL,                       -- canonical type name
    variation_key  TEXT NOT NULL REFERENCES variation (key),
    color          TEXT NOT NULL,
    finish         TEXT NOT NULL,
    quality        TEXT NOT NULL,
    medusa_id      TEXT,                                -- null until synced
    payload_hash   TEXT,
    state          TEXT NOT NULL DEFAULT 'pending'
                   CHECK (state IN ('pending','dirty','syncing','synced','needs_resync','gap_held','retiring')),
    last_synced    TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_combination_serve ON combination (state, created_at, combo_key);
CREATE INDEX IF NOT EXISTS idx_combination_variation ON combination (variation_key);

-- ---------------------------------------------------------------------------
-- product: a supplier's concrete offering, keyed by SKU ({source_code}-{surrogate}
-- uppercased). SKU is stable across a variation re-key, so a product re-points
-- onto the new variation_key rather than being recreated (design section 5C).
--
-- payload_hash covers: (variation_key, color, finish, quality, title, description,
--   handle, weight, length, width, height, origin_country_code, ordered(image urls),
--   company_id, sales_channel_id, category, bundle_size, ports).
-- ---------------------------------------------------------------------------
--
-- Two groups of columns. The DESIGN fields (name-based: color/finish/quality/
-- category/type as names, variation_key) are the pull-payload shape; their ids are
-- resolved from the attribute/variation tables at render time. The PRESENTATION
-- fields below them exist to reproduce the legacy 55-column import CSV exactly
-- during the parallel migration (the equivalence test), and can be dropped once
-- that CSV is retired. surrogate_key reproduces the SKU; the image columns preserve
-- the branch-specific slotting (oriented for blocks, product images for slabs).
CREATE TABLE IF NOT EXISTS product (
    sku                  TEXT PRIMARY KEY,
    source               TEXT NOT NULL,                -- source_code
    surrogate_key        TEXT,                          -- reproduces the SKU via emit._sku
    variation_key        TEXT NOT NULL REFERENCES variation (key),
    -- design fields (names; ids resolved at render)
    color                TEXT,
    finish               TEXT,
    quality              TEXT,
    type                 TEXT,                          -- the product's stone type name (-> type_id, type_name)
    category             TEXT,                          -- canonical category name (-> category_pcat_id)
    -- presentation fields (reproduce the legacy import CSV)
    title                TEXT,
    description          TEXT,
    handle               TEXT,
    slug                 TEXT,
    weight               REAL,                          -- dims as REAL so emit._num(:g) reproduces exactly
    length               REAL,
    width                REAL,
    height               REAL,
    origin_country_code  TEXT,
    origin_city          TEXT,
    origin_county        TEXT,
    thumbnail_key        TEXT,
    oriented_image_keys  TEXT,                          -- JSON array, ordered (blocks: Front/Right/Back/Left)
    product_image_keys   TEXT,                          -- JSON array, ordered (slabs: Product Image 1..N)
    company_id           TEXT,
    sales_channel_id     TEXT,
    ports                TEXT,                          -- JSON array of port ids
    visibility           TEXT,                          -- null -> render the config constant
    discountable         TEXT,                          -- null -> render the config constant
    sold_in_bundle       INTEGER,                       -- 0/1
    bundle_size          INTEGER,
    inventory_quantity   TEXT,                          -- the resolved stock string (emit.inventory_for)
    medusa_id            TEXT,                          -- null until synced
    payload_hash         TEXT,
    state                TEXT NOT NULL DEFAULT 'pending'
                         CHECK (state IN ('pending','dirty','syncing','synced','needs_resync','gap_held','retiring')),
    last_synced          TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_product_serve ON product (state, created_at, sku);
CREATE INDEX IF NOT EXISTS idx_product_variation ON product (variation_key);
CREATE INDEX IF NOT EXISTS idx_product_source ON product (source);

-- ---------------------------------------------------------------------------
-- inventory: stock per SKU. No state/payload_hash: the qty vs last_synced_qty
-- compare drives what is served (design section 7). qty 0 is the reversible
-- delist for a discontinued product.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inventory (
    sku              TEXT PRIMARY KEY REFERENCES product (sku),
    qty              INTEGER NOT NULL,
    last_synced_qty  INTEGER,                           -- null until first inventory sync
    updated_at       TEXT NOT NULL
);
-- served when qty IS DISTINCT FROM last_synced_qty; this index supports that scan.
CREATE INDEX IF NOT EXISTS idx_inventory_delta ON inventory (last_synced_qty, qty);

-- ---------------------------------------------------------------------------
-- image: one row per unique image content (sha256). Two lanes, two gates
-- (design H1): a variation gates on its variant_texture being promoted, a product
-- on its scraped_photo set being promoted. The live url is read from live_key.
-- A persistently failing generation goes `failed` and escalates its variation
-- (design H2).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS image (
    sha256       TEXT PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('scraped_photo', 'variant_texture')),
    source_url   TEXT,
    staging_key  TEXT,
    live_key     TEXT,                                  -- null until promoted
    state        TEXT NOT NULL DEFAULT 'staged'
                 CHECK (state IN ('staged','generating','promoted','failed')),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_image_state ON image (state);
CREATE INDEX IF NOT EXISTS idx_image_kind ON image (kind);

-- ---------------------------------------------------------------------------
-- gap: the durable, deduped queue of things a human must resolve (the per-run
-- tree_gaps.csv plus the review queues). An open gap HOLDS its dependents. Deduped
-- across runs by (kind, name).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gap (
    id                TEXT PRIMARY KEY,                 -- DAL-generated ULID-style id
    kind              TEXT NOT NULL
                      CHECK (kind IN ('missing_variation','missing_leaf_child','missing_attribute','image_generation_failed')),
    name              TEXT NOT NULL,
    nearest_existing  TEXT,
    nearest_score     REAL,
    example_url       TEXT,
    state             TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'resolved')),
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (kind, name)
);
CREATE INDEX IF NOT EXISTS idx_gap_state ON gap (state);

-- ---------------------------------------------------------------------------
-- sync_run: the audit ledger, one row per reconcile pass. `counts` is a JSON blob
-- of per-type / per-state tallies for GET /sync/status and the run summary.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sync_run (
    id           TEXT PRIMARY KEY,                      -- DAL-generated run id
    env          TEXT NOT NULL CHECK (env IN ('development', 'production')),
    fingerprint  TEXT,
    started      TEXT NOT NULL,
    finished     TEXT,
    status       TEXT,
    counts       TEXT,                                  -- JSON
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sync_run_started ON sync_run (started);
