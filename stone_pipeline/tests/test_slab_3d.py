"""The 3D slab across the boundaries (the "Slab 3D on Blokport" contract): a chosen photo of a whole single slab gets a
cut-out and its outline, published beside the views; a slab product whose lead photo has one carries it as model_3d,
stored in the ledger, part of the change fingerprint, and pulled as {cutout, meta} or null."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import cv2
import numpy as np
import pytest

from stone_pipeline.config.settings import ENV_SEGMENT, STANDARDISE as C
from stone_pipeline.config.sources import load_source
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.io import imagestore
from stone_pipeline.ledger.db import SCHEMA_VERSION, Ledger, now_iso
from stone_pipeline.ledger.populate import populate_products
from stone_pipeline.ledger.sync import ready
from stone_pipeline.stages import emit, images
from stone_pipeline.standardise import cutout

A, B = "a" * 64, "b" * 64
BASE = f"https://bucket.s3.amazonaws.com/{ENV_SEGMENT}/products"
KEY = "slab_marble_calacatta_viola_1"


def _slab_png(w, h, alpha):
    bgr = np.zeros((h, w, 3), np.uint8)
    bgr[:] = (40, 120, 200)                                                 # BGR: an orange stone
    ok, buf = cv2.imencode(".png", np.dstack([bgr, alpha]))
    assert ok
    return buf.tobytes()


def _rect(w, h):
    return np.full((h, w), 255, np.uint8)


# -- the cut-out ---------------------------------------------------------------------------------------------------

def test_a_cut_out_keeps_the_slab_s_alpha_and_proportions_and_is_the_same_every_time():
    png = _slab_png(1500, 1000, _rect(1500, 1000))
    webp, meta = cutout.make(png)
    assert (webp, meta) == cutout.make(png)
    assert cv2.imdecode(np.frombuffer(webp, np.uint8), cv2.IMREAD_UNCHANGED).shape[:2] == (1000, 1500)
    m = json.loads(meta)
    assert (m["version"], m["width_px"], m["height_px"]) == (C.CUTOUT_VERSION, 1500, 1000)
    assert len(m["outline"]) == 4 and all(0 <= u <= 1 and 0 <= v <= 1 for u, v in m["outline"])
    # the stone's colour (RGB 200, 120, 40) mixed with a quarter white
    assert m["edge_color"] == "#d5995d"                                     # 200, 120, 40 -> 213, 153, 93


def test_a_big_slab_is_brought_to_the_contract_s_long_side():
    webp, meta = cutout.make(_slab_png(4096, 2048, _rect(4096, 2048)))
    m = json.loads(meta)
    assert (m["width_px"], m["height_px"]) == (C.CUTOUT_LONG_SIDE, C.CUTOUT_LONG_SIDE // 2)


def test_a_chipped_corner_stays_missing_stone_in_the_outline():
    alpha = _rect(1200, 800)
    cv2.fillPoly(alpha, [np.array([[1200, 0], [900, 0], [1200, 250]])], 0)    # the top-right corner chipped off
    webp, meta = cutout.make(_slab_png(1200, 800, alpha))
    img = cv2.imdecode(np.frombuffer(webp, np.uint8), cv2.IMREAD_UNCHANGED)
    assert img.shape == (800, 1200, 4) and img[5, 1195, 3] == 0 and img[400, 600, 3] == 255   # the chip is transparent
    m = json.loads(meta)
    assert len(m["outline"]) == 5
    assert not any(u > 0.95 and v < 0.05 for u, v in m["outline"])         # no square corner where the chip is


def test_a_ragged_natural_edge_is_simplified_to_the_contract_s_limit():
    alpha = _rect(1600, 1000)
    rng = np.random.default_rng(7)
    for x in range(0, 1600, 8):                                             # a rough top edge, many small bites
        alpha[: int(rng.integers(0, 60)), x:x + 8] = 0
    m = json.loads(cutout.make(_slab_png(1600, 1000, alpha))[1])
    assert 4 <= len(m["outline"]) <= C.CUTOUT_MAX_POINTS


@pytest.mark.parametrize("png", [
    cv2.imencode(".png", np.zeros((50, 80, 3), np.uint8))[1].tobytes(),    # no alpha
    _slab_png(80, 50, np.zeros((50, 80), np.uint8)),                       # no stone
    b"not an image",
])
def test_a_slab_that_is_not_one_is_refused(png):
    with pytest.raises(ValueError):
        cutout.make(png)


# -- publishing ----------------------------------------------------------------------------------------------------

T0 = datetime(2026, 11, 2, tzinfo=timezone.utc)


def test_only_a_chosen_whole_slab_not_excluded_gets_a_3d_slab():
    from deploy import standardise_publish as pub
    run = f"{ENV_SEGMENT}/standardise-test/v2/sentas/"
    made = {f"{run}{A}.main.jpg": T0, f"{run}{A}.slab.png": T0, f"{run}{B}.main.jpg": T0}        # B: an open book
    chosen = [{"source": "sentas", "sha": A, "prefix": run}, {"source": "sentas", "sha": B, "prefix": run}]
    assert pub.slabs(chosen, made) == {("sentas", A): f"{run}{A}.slab.png"}
    assert pub.slabs(chosen, made, exclude={A}) == {}


def test_a_3d_slab_is_made_only_when_missing_half_written_or_older_than_its_slab():
    from deploy import standardise_publish as pub
    later = T0 + timedelta(hours=1)
    todo = {("sentas", A): "run/a.slab.png", ("sentas", B): "run/b.slab.png", ("zucchi", A): "run2/a.slab.png"}
    made = {"run/a.slab.png": T0, "run/b.slab.png": later, "run2/a.slab.png": T0}
    published = {**{k: later for k in pub.cutout_keys(("sentas", A))},          # current
                 **{k: T0 for k in pub.cutout_keys(("sentas", B))},             # older than its slab
                 pub.cutout_keys(("zucchi", A))[0]: later}                      # its outline never written
    assert pub.outdated(todo, made, published) == [("sentas", B), ("zucchi", A)]


def test_a_3d_slab_no_longer_chosen_is_removed_with_the_views():
    from deploy import standardise_publish as pub
    keep, old = pub.cutout_keys(("sentas", A)), pub.cutout_keys(("sentas", B))
    assert pub.stale({k: T0 for k in keep + old}, keep) == sorted(old)


# -- the image stage -----------------------------------------------------------------------------------------------

def _improved(sha):
    return f"{BASE}/improved/sentas/{sha}.png"


VIEWS = {"main": {A, B}, "white": {A, B}, "studio": {A, B}, "close": {A}, imagestore.CUTOUT_SUBDIR: {A}}


def test_a_slab_product_carries_its_lead_photo_s_3d_slab():
    assert images.model_3d([_improved(A), _improved(B)], VIEWS) == f"{BASE}/standard/cutout/sentas/{A}.webp"


def test_no_3d_when_the_lead_photo_has_none_even_if_another_photo_does():
    # B leads (first in the product's order, with a side view) but is an open book: the 3D must show the lead slab
    assert images.model_3d([_improved(B), _improved(A)], VIEWS) is None
    assert images.model_3d([_improved("c" * 64)], VIEWS) is None


def test_only_a_slab_product_gets_a_3d_slab(monkeypatch):
    monkeypatch.setattr(images, "_load_standard_views", lambda cfg: VIEWS)
    monkeypatch.setattr(images, "_link_from_manifest", lambda rows, cfg, stats: None)
    rows = [CanonicalRow(src_site="sentas", surrogate_key=k, format_value=f, image_keys=[_improved(A)])
            for k, f in (("1", "Slab"), ("2", "Block"), ("3", "Tile"))]
    images.run(rows, cfg=type("Cfg", (), {"mode": "passthrough"})())
    assert [r.model_3d_key for r in rows] == [f"{BASE}/standard/cutout/sentas/{A}.webp", None, None]
    assert rows[0].thumbnail_key == f"{BASE}/standard/main/sentas/{A}.jpg"


# -- the ledger and the pull ---------------------------------------------------------------------------------------

def _seed_variation(ledger):
    now = now_iso()
    ledger.upsert("variation", {"key": KEY, "branch": "slab", "type": "Marble", "name": "Calacatta Viola",
                                "aliases": "[]", "image_url": "https://s3/tex.png", "image_sha256": None,
                                "image_model": None, "volume": "", "medusa_id": "V1", "in_full": 1, "payload_hash": "",
                                "state": "synced", "first_seen": now, "last_synced": now,
                                "created_at": now, "updated_at": now}, pk=("key",))


def _row(model_3d_key):
    return CanonicalRow(
        src_site="sentas", surrogate_key="7154-4", color_name="Purple", finish_name="Polished", quality_name="A",
        type_name="Marble", variation_key=KEY, variation_name="Calacatta Viola", title="t", description="d",
        handle="h", slug="h", weight=0.34, length=2.85, width=1.9, height=0.02, company_id="CO",
        sales_channel_id="SC", thumbnail_key="i.jpg", model_3d_key=model_3d_key)


def test_the_ledger_stores_the_3d_slab_and_a_change_re_serves_the_product(tmp_path):
    cfg = load_source("sentas")
    url = f"{BASE}/standard/cutout/sentas/{A}.webp"
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _seed_variation(ledger)
        populate_products(ledger, [_row(None)], cfg)
        sku = emit._sku(_row(None), cfg)
        ledger.execute("UPDATE product SET state = 'synced', medusa_id = 'prod_1' WHERE sku = ?", (sku,))
        populate_products(ledger, [_row(url)], cfg)
        stored = ledger.get("product", "sku", sku)
        assert (stored["model_3d_key"], stored["state"]) == (url, "dirty")      # gained its 3D: re-served


def test_the_pull_payload_carries_both_3d_files_and_null_without_one(tmp_path):
    url = f"{BASE}/standard/cutout/sentas/{A}.webp"
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _seed_variation(ledger)
        now = now_iso()
        for sku, key in (("SEN-7154-4", url), ("SEN-2213", None)):
            ledger.upsert("product", {"sku": sku, "source": "sen", "variation_key": KEY, "state": "pending",
                                      "model_3d_key": key, "created_at": now, "updated_at": now}, pk=("sku",))
        by = {r["external_id"]: r["payload"] for r in ready(ledger, "products")}
        assert by["SEN-7154-4"]["model_3d"] == {"cutout": url, "meta": f"{BASE}/standard/cutout/sentas/{A}.json"}
        assert by["SEN-2213"]["model_3d"] is None


def test_a_v9_ledger_gains_the_3d_column_and_keeps_its_data(tmp_path):
    if sqlite3.sqlite_version_info < (3, 35, 0):
        pytest.skip("ALTER TABLE DROP COLUMN needs SQLite >= 3.35 to simulate the v9 shape")
    p = tmp_path / "mig.ledger"
    with Ledger.open(p, env="development") as lg:
        _seed_variation(lg)
    raw = sqlite3.connect(p)                                                # simulate a v9 ledger
    raw.execute("ALTER TABLE product DROP COLUMN model_3d_key")
    raw.execute("PRAGMA user_version = 9")
    raw.execute("UPDATE ledger_meta SET schema_version = 9")
    raw.commit()
    raw.close()
    with Ledger.open(p, env="development") as lg:
        assert lg.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 10
        assert "model_3d_key" in {r[1] for r in lg.conn.execute("PRAGMA table_info(product)")}
        assert lg.get("variation", "key", KEY)["name"] == "Calacatta Viola"
