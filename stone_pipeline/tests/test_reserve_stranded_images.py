"""TEMPORARY one-off backfill test -- REMOVE with reserve.py + the endpoint after the backfill.

reserve_stranded_images bumps ONLY imaged + synced variants that Medusa's export shows imageless.
"""

from __future__ import annotations

import csv

from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.ledger.reserve import reserve_stranded_images


def _var(ledger, key, *, state, image_url="https://s3/tex.png", image_sha256="abc123"):
    now = now_iso()
    ledger.upsert("variation", {"key": key, "branch": "slab", "type": "Marble", "name": "x",
                                "aliases": "[]", "image_url": image_url, "image_sha256": image_sha256,
                                "image_model": None, "volume": "", "medusa_id": None,
                                "in_full": 1, "payload_hash": "", "state": state,
                                "first_seen": now, "last_synced": None,
                                "created_at": now, "updated_at": now}, pk=("key",))


def _export(tmp_path, rows):
    p = tmp_path / "variants_export.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["Key", "Image"])
        w.writeheader()
        for key, image in rows:
            w.writerow({"Key": key, "Image": image})
    return p


def test_reserve_bumps_only_stranded_imaged_synced_and_is_idempotent(tmp_path):
    export = _export(tmp_path, [
        ("slab_stranded_1", ""),            # imaged+synced, Medusa Image EMPTY   -> reserve
        ("slab_stranded_2", "   "),         # blank counts as empty               -> reserve
        ("slab_ok_1", "https://img.png"),   # Medusa HAS the image                -> leave
        ("slab_dirty_1", ""),               # already dirty (not synced)          -> leave
        ("slab_noimg_1", ""),               # no image in the ledger              -> leave
    ])
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _var(ledger, "slab_stranded_1", state="synced")
        _var(ledger, "slab_stranded_2", state="synced")
        _var(ledger, "slab_ok_1", state="synced")
        _var(ledger, "slab_dirty_1", state="dirty")
        _var(ledger, "slab_noimg_1", state="synced", image_url="", image_sha256=None)

        result = reserve_stranded_images(ledger, export_path=export)
        assert result["reserved"] == 2 and result["export_present"] is True

        states = {r["key"]: r["state"] for r in ledger.execute("SELECT key, state FROM variation")}
        assert states["slab_stranded_1"] == "dirty"
        assert states["slab_stranded_2"] == "dirty"
        assert states["slab_ok_1"] == "synced"      # Medusa has it -> untouched
        assert states["slab_dirty_1"] == "dirty"    # already dirty, not re-selected
        assert states["slab_noimg_1"] == "synced"   # not imaged -> untouched

        # idempotent: the reserved ones are now dirty (not synced), so a second run finds nothing new
        assert reserve_stranded_images(ledger, export_path=export)["reserved"] == 0


def test_reserve_no_export_is_a_safe_noop(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _var(ledger, "slab_imaged_1", state="synced")
        result = reserve_stranded_images(ledger, export_path=tmp_path / "missing.csv")
        assert result["reserved"] == 0 and result["export_present"] is False
        assert next(iter(ledger.execute("SELECT state FROM variation")))["state"] == "synced"
