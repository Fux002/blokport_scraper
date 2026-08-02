"""Variant image freshness: a texture that (re)generates at the stable {Key}.png URL must re-serve.

The variation payload_hash keys on the image URL, which never changes ({Key}.png). Emit writes the
image's S3 ETag into a variant_image_shas.json sidecar; populate folds it into the hash. So a texture
whose BYTES change (the one-time best-model refresh, or a first appearance) flips the variation dirty
and re-serves -- which a URL-only hash silently missed, leaving Medusa on a stale/blank image forever.
"""

from __future__ import annotations

import csv
import json

from stone_pipeline.ledger.db import Ledger
from stone_pipeline.ledger.populate import populate_variations_full
from stone_pipeline.stages.emit_catalog import _COLS

KEY = "slab_marble_arabescato_1124daeb"


def _write_full(d, rows):
    p = d / "1_variants_full.csv"
    with p.open("w", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        w.writerow(_COLS)
        for r in rows:
            w.writerow([r.get(c, "") for c in _COLS])
    return p


def _write_shas(d, shas):
    (d / "variant_image_shas.json").write_text(json.dumps(shas), encoding="utf-8")


def _state(ledger, key):
    row = ledger.execute("SELECT state, image_sha256 FROM variation WHERE key = ?", (key,)).fetchone()
    return (row["state"], row["image_sha256"])


def test_regenerated_texture_reserves_a_synced_variation(tmp_path):
    # An imaged variation is synced (Medusa has it). Its texture is regenerated in place: same {Key}.png
    # URL, new bytes (a new ETag). The variation MUST flip dirty so Medusa re-fetches the new image.
    full = _write_full(tmp_path, [{"Key": KEY, "Name": "Arabescato",
                                   "Image": f"https://s3/dev/variations/{KEY}.png"}])
    _write_shas(tmp_path, {KEY: "etag-v1"})
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        populate_variations_full(ledger, full)
        ledger.execute("UPDATE variation SET state = 'synced' WHERE key = ?", (KEY,))   # Medusa acked v1

        _write_shas(tmp_path, {KEY: "etag-v2"})            # texture regenerated: new bytes, SAME url
        populate_variations_full(ledger, full)
        assert _state(ledger, KEY) == ("dirty", "etag-v2")  # re-serves the new image


def test_appearing_texture_reserves_and_stable_texture_does_not_churn(tmp_path):
    full = _write_full(tmp_path, [{"Key": KEY, "Name": "Arabescato",
                                   "Image": f"https://s3/dev/variations/{KEY}.png"}])
    _write_shas(tmp_path, {KEY: "etag-v1"})
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        populate_variations_full(ledger, full)
        ledger.execute("UPDATE variation SET state = 'synced' WHERE key = ?", (KEY,))

        # same csv + same sha on a re-run is a no-op: an unchanged texture must NOT re-serve.
        populate_variations_full(ledger, full)
        assert _state(ledger, KEY)[0] == "synced"


def test_missing_sidecar_is_tolerated(tmp_path):
    # CI / S3 unreachable: no sidecar. Populate must still work; the fingerprint is simply absent.
    full = _write_full(tmp_path, [{"Key": KEY, "Name": "Arabescato", "Image": ""}])
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        n = populate_variations_full(ledger, full)
        assert n == 1
        assert _state(ledger, KEY) == ("pending", None)
