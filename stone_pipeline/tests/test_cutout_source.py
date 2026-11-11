"""What a 3D slab's pixels are made from, in its meta (Mas, 2026-11-10, for architect renders): a new 3D slab carries the
exact source_long_px (the stone's long side before the layout enlarged it) and upscaled (whether it did); one published
before its run recorded that carries only the bound source_long_px_max (the long side of the photo its run started from),
added by a meta-only republish (stone_pipeline.photos.cutout_meta) that never touches the cut-out or what it was made from.
A value that cannot be determined is absent, never false or 0."""
from __future__ import annotations

import io
import json

import cv2
import numpy as np
from botocore.exceptions import ClientError
from PIL import Image

from stone_pipeline.config.settings import ENV_SEGMENT
from stone_pipeline.config.standardise_settings import STANDARDISE as C
from stone_pipeline.io import imagestore
from stone_pipeline.standardise import cutout, layout

A, B = "a" * 64, "b" * 64
RUN = f"{ENV_SEGMENT}/standardise-test/v9/sentas/"
SLAB = f"{RUN}{A}.slab.png"


def _flat(h, w):
    return layout.Flat(np.zeros((h, w, 3), np.uint8), np.ones((h, w), np.float32), np.eye(3), None)


def _png(w, h):
    ok, buf = cv2.imencode(".png", np.dstack([np.full((h, w, 3), 120, np.uint8), np.full((h, w), 255, np.uint8)]))
    assert ok
    return buf.tobytes()


def _jpeg(w, h):
    out = io.BytesIO()
    Image.new("RGB", (w, h), (90, 90, 90)).save(out, "JPEG")
    return out.getvalue()


class _S3:
    """A bucket in a dict, with each object's ETag and metadata: what publish and the meta-only republish read and write."""

    def __init__(self, objects):
        self.objects = {k: (v, {}) if isinstance(v, bytes) else v for k, v in objects.items()}
        self.puts = []

    def _etag(self, key):
        return f'"e-{len(self.objects[key][0])}"'

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        body, meta = self.objects[Key]
        return {"Body": io.BytesIO(body), "Metadata": dict(meta), "ETag": self._etag(Key)}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"Metadata": dict(self.objects[Key][1]), "ETag": self._etag(Key)}

    def list_objects_v2(self, Bucket, Prefix, MaxKeys=1000):
        keys = [k for k in sorted(self.objects) if k.startswith(Prefix)][:MaxKeys]
        return {"KeyCount": len(keys), "Contents": [{"Key": k, "ETag": self._etag(k)} for k in keys]}

    def get_paginator(self, name):
        s3 = self

        class _Pages:
            def paginate(self, Bucket, Prefix):
                return [s3.list_objects_v2(Bucket, Prefix)]
        return _Pages()

    def put_object(self, Bucket, Key, Body, ContentType, Metadata=None):
        self.puts.append((Key, ContentType, Metadata))
        self.objects[Key] = (Body, dict(Metadata or {}))

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)


# -- the layout records what the slab it draws is made from ---------------------------------------------------------

def test_a_slab_the_layout_enlarges_is_recorded_with_its_own_long_side_and_upscaled():
    assert layout.source_of([_flat(600, 1000)], 0) == {"source_long_px": 1000, "upscaled": True}


def test_a_slab_wide_enough_is_drawn_as_it_is_and_recorded_not_upscaled():
    assert layout.source_of([_flat(1300, 2400)], 0) == {"source_long_px": 2400, "upscaled": False}


def test_an_open_book_half_is_recorded_as_drawn_with_its_seam_trimmed_before_enlarging():
    half = 900 - max(1, int(C.JOINT_TRIM * 900))
    assert layout.source_of([_flat(1200, 900), _flat(1200, 900)], 1) == {"source_long_px": 1200, "upscaled": True}
    assert layout.source_of([_flat(400, 900), _flat(400, 900)], 0) == {"source_long_px": half, "upscaled": True}
    assert layout.source_of([_flat(400, 1000), _flat(400, 1000)], 0)["upscaled"] is False   # 2000 px wide: drawn as is


def test_a_drawn_width_of_the_layout_s_own_span_is_reached_both_enlarged_and_as_shot():
    # Why an existing 3D slab is never marked upscaled (photos.cutout_meta): a slab narrower than the span is enlarged TO
    # it, one exactly that wide is drawn as it is, and both are drawn the same width, so its slab.png cannot tell them apart.
    span = round(C.OUT_W * (1 - 2 * C.MARGIN))
    narrow, native = [_flat(600, 1000)], [_flat(1100, span)]
    assert layout.drawn_alphas(narrow)[0].shape[1] == layout.drawn_alphas(native)[0].shape[1] == span
    assert layout.source_of(narrow, 0)["upscaled"] and not layout.source_of(native, 0)["upscaled"]


def test_the_run_records_what_its_3d_slab_is_made_from_beside_which_slab_it_is():
    from stone_pipeline.photos import source as src
    from stone_pipeline.standardise.photo import Result
    img = np.zeros((20, 30, 3), np.uint8)
    res = Result(kind="slab", images={"main": img}, slab=(img, np.ones((20, 30), np.float32)), chosen=(0, 1),
                 source={"source_long_px": 30, "upscaled": True})
    files = {name: body for name, body, _ in src._files(res)}
    assert json.loads(files["slab.json"]) == {"index": 0, "of": 1, "source_long_px": 30, "upscaled": True}


# -- the cut-out carries it ----------------------------------------------------------------------------------------

def test_the_cut_out_meta_carries_what_it_is_made_from_and_the_cut_out_itself_is_the_same():
    png = _png(300, 200)
    webp, meta = cutout.make(png, source_long_px=300, upscaled=True)
    assert webp == cutout.make(png)[0]
    assert json.loads(meta) == json.loads(cutout.make(png)[1]) | {"source_long_px": 300, "upscaled": True}


def test_a_new_3d_slab_takes_the_exact_values_its_run_recorded_and_no_bound():
    from stone_pipeline.photos import publish as pub
    s3 = _S3({SLAB: _png(300, 200), f"{RUN}{A}.slab.json": json.dumps(
        {"index": 0, "of": 1, "source_long_px": 280, "upscaled": True}).encode()})
    assert pub._publish_cutout(s3, ("sentas", A), SLAB, '"e1"', {})
    meta = json.loads(s3.objects[pub.cutout_keys(("sentas", A))[1]][0])
    assert (meta["source_long_px"], meta["upscaled"]) == (280, True) and "source_long_px_max" not in meta


def test_a_3d_slab_of_a_run_that_recorded_nothing_takes_the_bound_from_the_photo_the_run_started_from():
    from stone_pipeline.photos import publish as pub
    scraped = f"{ENV_SEGMENT}/products/scraped/sentas/{A}.jpg"
    base = {SLAB: _png(300, 200), f"{RUN}{A}.slab.json": json.dumps({"index": 0, "of": 1}).encode(), scraped: _jpeg(1600, 900)}
    for extra, long_side in (({}, 1600), ({f"{RUN}retouched/{A}.jpg": _jpeg(1500, 1000)}, 1500)):
        s3 = _S3(base | extra)
        assert pub._publish_cutout(s3, ("sentas", A), SLAB, '"e1"', {A: scraped})
        meta = json.loads(s3.objects[pub.cutout_keys(("sentas", A))[1]][0])
        assert meta["source_long_px_max"] == long_side and not {"source_long_px", "upscaled"} & set(meta)


def test_a_3d_slab_whose_source_photo_is_gone_has_no_bound_at_all():
    from stone_pipeline.photos import publish as pub
    s3 = _S3({SLAB: _png(300, 200)})
    assert pub._publish_cutout(s3, ("sentas", A), SLAB, '"e1"', {})
    meta = json.loads(s3.objects[pub.cutout_keys(("sentas", A))[1]][0])
    assert not {"source_long_px", "upscaled", "source_long_px_max"} & set(meta)


# -- the meta-only republish of the 3D slabs published before -----------------------------------------------------

def _published(slab_body=None, record=None, photo=True, stamped=True):
    """A bucket with one published 3D slab of A (and a tile's, no stamp), as publish wrote them before this change."""
    from stone_pipeline.photos import publish as pub
    s3 = _S3({SLAB: slab_body or _png(300, 200)})
    if record is not None:
        s3.objects[f"{RUN}{A}.slab.json"] = (json.dumps(record).encode(), {})
    if photo:
        s3.objects[f"{ENV_SEGMENT}/products/scraped/sentas/{A}.jpg"] = (_jpeg(2000, 1333), {})
    webp, meta = cutout.make(_png(300, 200))
    stamp = {"made-from": pub.made_from(SLAB, s3.head_object("b", SLAB)["ETag"] if stamped else '"other"')}
    webp_key, meta_key = pub.cutout_keys(("sentas", A))
    s3.objects[webp_key], s3.objects[meta_key] = (webp, {}), (meta, stamp)
    tile_webp, tile_meta = cutout.encode(np.dstack([np.zeros((20, 40, 3), np.uint8), np.full((20, 40), 255, np.uint8)]),
                                         px_per_mm=0.8)
    s3.objects[imagestore.cutout_key("sentas", B, "webp")] = (tile_webp, {})
    s3.objects[imagestore.cutout_key("sentas", B, "json")] = (tile_meta, {})
    return s3, webp_key, meta_key, stamp


def test_the_meta_only_republish_adds_the_bound_and_never_touches_the_cut_out_or_its_stamp():
    from stone_pipeline.photos import cutout_meta
    s3, webp_key, meta_key, stamp = _published(record={"index": 0, "of": 1})
    webp_before, meta_before = s3.objects[webp_key], json.loads(s3.objects[meta_key][0])
    out = cutout_meta.republish(s3)
    assert s3.puts == [(meta_key, "application/json", stamp)]                 # the outline only, its stamp as it was
    assert s3.objects[webp_key] == webp_before
    assert json.loads(s3.objects[meta_key][0]) == meta_before | {"source_long_px_max": 2000}
    assert out["added"] == 1 and out["tile"] == 1
    s3.puts.clear()
    assert cutout_meta.republish(s3)["unchanged"] == 1 and s3.puts == []        # a second run changes nothing


def test_the_meta_only_republish_never_marks_an_existing_3d_slab_upscaled_nor_writes_a_bound_as_exact():
    from stone_pipeline.photos import cutout_meta
    s3, _, meta_key, _ = _published(record={"index": 0, "of": 1})
    cutout_meta.republish(s3)
    assert not {"source_long_px", "upscaled"} & set(json.loads(s3.objects[meta_key][0]))


def test_the_meta_only_republish_leaves_a_3d_slab_publish_will_make_again_and_one_it_cannot_measure():
    from stone_pipeline.photos import cutout_meta
    s3, _, _, _ = _published(record={"index": 0, "of": 1}, stamped=False)      # its slab was made again since
    assert cutout_meta.republish(s3)["outdated"] == 1 and s3.puts == []
    s3, _, _, _ = _published(record={"index": 0, "of": 1}, photo=False)       # the photo its run started from is gone
    assert cutout_meta.republish(s3)["unchanged"] == 1 and s3.puts == []


# -- review of #604 (Scraper clean, 2026-11-10) --------------------------------------------------------------------

def test_a_corrupt_run_record_fails_loud_and_keeps_the_3d_slab():
    # M1: an unreadable <sha>.slab.json is not "no slab": it must never delete the product's 3D slab
    import pytest
    from stone_pipeline.photos import publish as pub
    s3 = _S3({SLAB: _png(300, 200), f"{RUN}{A}.slab.json": b"{not json"})
    webp_key, meta_key = pub.cutout_keys(("sentas", A))
    s3.objects[webp_key], s3.objects[meta_key] = (b"old webp", {}), (b"{}", {"made-from": "x"})
    with pytest.raises(ValueError):
        pub._publish_cutout(s3, ("sentas", A), SLAB, '"e1"', {})
    assert s3.objects[webp_key] == (b"old webp", {}) and s3.objects[meta_key] == (b"{}", {"made-from": "x"})


def test_the_meta_only_republish_touches_only_its_scope():
    # L1: every refreshed outline re-serves its product once (standard_version), so a run names what it refreshes
    from stone_pipeline.photos import cutout_meta
    s3, _, meta_key, _ = _published(record={"index": 0, "of": 1})
    assert cutout_meta.republish(s3, scope=frozenset({"c" * 64})) == dict.fromkeys(cutout_meta.OUTCOMES, 0)
    assert s3.puts == []
    assert cutout_meta.republish(s3, scope=frozenset({A}))["added"] == 1 and [p[0] for p in s3.puts] == [meta_key]


def test_the_meta_only_republish_needs_a_scope_and_never_all_on_dev(monkeypatch):
    import pytest
    from stone_pipeline.photos import cutout_meta
    monkeypatch.setattr(cutout_meta, "IS_PRODUCTION", False)
    for value in ("", "  ", "all"):
        monkeypatch.setenv("CUTOUT_META_SCOPE", value)
        with pytest.raises(SystemExit):
            cutout_meta.scope_from_env()
    monkeypatch.setenv("CUTOUT_META_SCOPE", f"{A}, {B}")
    assert cutout_meta.scope_from_env() == frozenset({A, B})
    monkeypatch.setattr(cutout_meta, "IS_PRODUCTION", True)
    monkeypatch.setenv("CUTOUT_META_SCOPE", "all")
    assert cutout_meta.scope_from_env() is None
    monkeypatch.setenv("CUTOUT_META_SCOPE", "")
    with pytest.raises(SystemExit):
        cutout_meta.scope_from_env()
