"""End-to-end LEDGER scenario tests -- the invariants that must hold after every step so the
pipeline stays independent, idempotent, and un-corruptible: cold start, hot re-produce, a
churning Medusa id, adding a scraper, and a broken/partial sync that restarts.

These are ledger-level (fast, isolated) -- they drive populate + sync + simulate with synthetic
canonical rows that carry the STABLE variation_key (as match_variation now stamps it), so a
regression in the product<->variation link, the convergent state, or add-a-scraper isolation
fails here loudly. The heavier real-pipeline run (scrape/match/emit) is covered by the m-series
and ledger equivalence tests.
"""

from __future__ import annotations

from stone_pipeline.config.sources import load_source
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.ledger import simulate, sync
from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.ledger.populate import populate_inventory, populate_products


def _variety(lg: Ledger, key: str, medusa_id: str, state: str = "synced") -> None:
    """A servable variation: typed, textured, in the produced set."""
    now = now_iso()
    lg.upsert("variation", {
        "key": key, "branch": key.split("_", 1)[0], "type": "Marble", "name": key.rsplit("_", 1)[0],
        "aliases": "[]", "image_url": "https://s3/tex.png", "image_sha256": None, "image_model": None,
        "volume": "", "medusa_id": medusa_id, "in_full": 1, "payload_hash": key, "state": state,
        "first_seen": now, "last_synced": None, "created_at": now, "updated_at": now}, pk=("key",))


def _row(surrogate: str, variation_key: str, color: str = "Black", qty: str = "5") -> CanonicalRow:
    """A canonical product row carrying the STABLE variation_key (as match stamps it)."""
    return CanonicalRow(
        src_site="x", surrogate_key=surrogate, variation_key=variation_key,
        variation_id="EXPORT-" + variation_key,   # the churn-prone id; must NOT be the link
        color_name=color, finish_name="Polished", quality_name="A", type_name="Marble",
        title=f"{variation_key} {color}", description="A stone.", handle=f"h-{surrogate}",
        slug=f"h-{surrogate}", weight=0.3, length=2.5, width=0.02, height=1.7,
        origin_country_code="IT", bundle_size=6, raw_slab_count=qty, product_image_keys=["i.jpg"])


def _snapshot(lg: Ledger, source: str) -> dict:
    return {r["sku"]: (r["state"], r["medusa_id"], r["variation_key"], r["payload_hash"])
            for r in lg.execute("SELECT * FROM product WHERE source = ?", (source,))}


def _no_orphans(lg: Ledger) -> int:
    return lg.execute("SELECT COUNT(*) n FROM product p LEFT JOIN variation v ON v.key = p.variation_key "
                      "WHERE v.key IS NULL").fetchone()["n"]


# --- COLD START ---------------------------------------------------------------

def test_cold_start_converges_with_invariants(tmp_path):
    cfg = load_source("polonine")
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        for i in range(3):
            _variety(lg, f"slab_marble_v{i}_0001", medusa_id=f"V{i}", state="synced")
        rows = [_row(f"A{i}", f"slab_marble_v{i}_0001") for i in range(3)]
        populate_products(lg, rows, cfg)
        populate_inventory(lg, rows, cfg)

        assert _no_orphans(lg) == 0                                   # every product resolves
        assert lg.counts("product") == {"pending": 3}
        assert sync.count_ready(lg, "products") == 3                  # variations already synced -> servable
        rep = simulate.simulate_sync(lg)                              # (count_ready PEEKS; does not lease)
        assert rep["converged"] and lg.counts("product") == {"synced": 3}
        assert sync.count_ready(lg, "products") == 0 and sync.count_ready(lg, "inventory") == 0   # fixed point


# --- HOT RE-PRODUCE (idempotent / convergent) --------------------------------

def test_hot_reproduce_is_a_fixed_point(tmp_path):
    cfg = load_source("polonine")
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        _variety(lg, "slab_marble_x_0001", medusa_id="V1")
        rows = [_row("A1", "slab_marble_x_0001")]
        populate_products(lg, rows, cfg)
        simulate.simulate_sync(lg)                                    # product synced with an id
        before = _snapshot(lg, "pol")
        assert list(before.values())[0][0] == "synced"

        populate_products(lg, rows, cfg)                             # HOT re-produce, unchanged
        populate_products(lg, rows, cfg)                             # and again
        assert _snapshot(lg, "pol") == before                        # nothing moved: same state/id/key/hash
        assert _no_orphans(lg) == 0


def test_content_change_re_serves_only_the_changed_product(tmp_path):
    cfg = load_source("polonine")
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        _variety(lg, "slab_marble_x_0001", medusa_id="V1")
        _variety(lg, "slab_marble_y_0001", medusa_id="V2")
        rows = [_row("A1", "slab_marble_x_0001"), _row("A2", "slab_marble_y_0001")]
        populate_products(lg, rows, cfg)
        simulate.simulate_sync(lg)
        # change ONE product's content
        changed = [rows[0].model_copy(update={"title": "New Title"}), rows[1]]
        populate_products(lg, changed, cfg)
        states = {r["sku"]: r["state"] for r in lg.execute("SELECT sku, state FROM product")}
        assert sum(s == "dirty" for s in states.values()) == 1       # only the changed one re-serves
        assert sum(s == "synced" for s in states.values()) == 1
        # medusa_id preserved on the dirtied one
        assert all(r["medusa_id"] for r in lg.execute("SELECT medusa_id FROM product"))


# --- MEDUSA_ID CHURN (the D1 root) -------------------------------------------

def test_medusa_id_churn_never_orphans(tmp_path):
    cfg = load_source("polonine")
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        _variety(lg, "slab_marble_x_0001", medusa_id="V1")
        rows = [_row("A1", "slab_marble_x_0001")]
        populate_products(lg, rows, cfg)
        simulate.simulate_sync(lg)
        # Medusa re-mints the variation id (clean-start re-sync) -- the export id in the row is now stale
        lg.execute("UPDATE variation SET medusa_id = 'REMINT-1' WHERE key = 'slab_marble_x_0001'")
        populate_products(lg, rows, cfg)                             # re-produce
        assert _no_orphans(lg) == 0                                  # link held via the stable Key
        assert _snapshot(lg, "pol")["POL-A1"][0] == "synced"        # and stayed synced


# --- ADD A SCRAPER -----------------------------------------------------------

def test_add_a_scraper_leaves_earlier_ones_untouched(tmp_path):
    pol, mar = load_source("polonine"), load_source("marenostone")
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        _variety(lg, "slab_marble_x_0001", medusa_id="V1")           # shared backbone
        _variety(lg, "slab_marble_y_0001", medusa_id="V2")
        populate_products(lg, [_row("A1", "slab_marble_x_0001")], pol)
        simulate.simulate_sync(lg)                                    # polonine synced (ids churned)
        pol_before = _snapshot(lg, "pol")

        # ADD marenostone -- references the SAME shared variety + a synced one
        populate_products(lg, [_row("B1", "slab_marble_x_0001"), _row("B2", "slab_marble_y_0001")], mar)

        assert _snapshot(lg, "pol") == pol_before                    # ISOLATION: polonine byte-identical
        assert _no_orphans(lg) == 0                                  # marenostone products resolve
        mar_states = {r["sku"]: r["state"] for r in lg.execute("SELECT sku, state FROM product WHERE source='mar'")}
        assert set(mar_states.values()) == {"pending"} and len(mar_states) == 2
        # no SKU collision across sources; prefixes distinct
        skus = [r["sku"] for r in lg.execute("SELECT sku FROM product")]
        assert len(skus) == len(set(skus)) and any(s.startswith("POL-") for s in skus) and any(s.startswith("MAR-") for s in skus)
        # syncing again serves ONLY the new (marenostone) products
        served = {i["external_id"] for i in sync.ready(lg, "products")}
        assert served == {"MAR-B1", "MAR-B2"}


# --- BROKEN / PARTIAL SYNC, THEN RESTART -------------------------------------

def test_partial_ack_then_restart_serves_only_the_unacked(tmp_path):
    cfg = load_source("polonine")
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        for i in range(4):
            _variety(lg, f"slab_marble_v{i}_0001", medusa_id=f"V{i}")
        populate_products(lg, [_row(f"A{i}", f"slab_marble_v{i}_0001") for i in range(4)], cfg)
        served = [i["external_id"] for i in sync.ready(lg, "products")]   # LEASES all four -> 'syncing'
        assert len(served) == 4
        assert lg.counts("product") == {"syncing": 4}                     # in-flight, not yet acked
        # ack only HALF (a crash / interruption after two): those settle to synced, the other two
        # stay leased ('syncing') -- a second overlapping pull would NOT re-serve them (D5 guard).
        for sku in served[:2]:
            sync.ack(lg, "products", sku, medusa_id="M-" + sku, status_="synced")
        assert {i["external_id"] for i in sync.ready(lg, "products")} == set(), \
            "leased-but-unacked rows must not double-serve to a concurrent pull"
        # RESTART after the lease expires: age the in-flight rows past the lease window, so the next
        # pull reaps them back to servable and re-serves EXACTLY the two un-acked, none of the acked.
        lg.execute("UPDATE product SET updated_at = '2000-01-01T00:00:00+00:00' WHERE state = 'syncing'")
        reserved = {i["external_id"] for i in sync.ready(lg, "products")}
        assert reserved == set(served[2:])
        assert all(lg.get("product", "sku", s)["state"] == "synced" for s in served[:2])
