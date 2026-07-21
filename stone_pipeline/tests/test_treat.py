"""treat.py: treat S3 raw product images into improved/ and repoint the manifest, WITHOUT ever
re-downloading from the supplier CDN (which blocks datacenter IPs). Tested against an in-memory S3."""

from __future__ import annotations

import io
import json

from stone_pipeline.config.settings import ENV_SEGMENT
from stone_pipeline.io.image_processing import ProcessResult
from stone_pipeline.stages import treat


class FakeS3:
    """Minimal in-memory S3: list (by prefix), get, put. Records puts for assertions."""

    def __init__(self, objects: dict[str, bytes]):
        self.objects = dict(objects)
        self.puts: list[str] = []

    def get_paginator(self, _op):
        outer = self

        class _P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for k in sorted(outer.objects) if k.startswith(Prefix)]}

        return _P()

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, **_kw):
        self.objects[Key] = Body if isinstance(Body, bytes) else bytes(Body)
        self.puts.append(Key)


class FakeProc:
    def process(self, data, watermarked=False, enhance=True):
        return ProcessResult(data + b"-treated")   # a visible marker that treatment ran


def _url(path: str) -> str:
    return f"https://bkt.s3.eu-west-1.amazonaws.com/{path}"


def test_treat_source_treats_raws_and_repoints_manifest():
    raw = f"{ENV_SEGMENT}/products/zucchi/aa.jpg"
    mkey = f"{ENV_SEGMENT}/products/_manifest.json"
    s3 = FakeS3({
        raw: b"rawbytes",
        mkey: json.dumps({"http://src/a.jpg": _url(f"{ENV_SEGMENT}/products/zucchi/aa.jpg")}).encode(),
    })
    stats = treat.treat_source("zucchi", s3=s3, processor=FakeProc())
    improved = f"{ENV_SEGMENT}/products/improved/zucchi/aa.jpg"
    assert stats.treated == 1 and stats.failed == 0
    assert s3.objects[improved] == b"rawbytes-treated"                 # treated image written
    manifest = json.loads(s3.objects[mkey])
    assert manifest["http://src/a.jpg"] == _url(f"{ENV_SEGMENT}/products/improved/zucchi/aa.jpg")  # repointed
    assert stats.manifest_repointed == 1


def test_idempotent_skips_already_treated():
    raw = f"{ENV_SEGMENT}/products/zucchi/aa.jpg"
    improved = f"{ENV_SEGMENT}/products/improved/zucchi/aa.jpg"
    s3 = FakeS3({raw: b"rawbytes", improved: b"already"})   # improved already exists
    stats = treat.treat_source("zucchi", s3=s3, processor=FakeProc())
    assert stats.treated == 0 and stats.skipped == 1
    assert s3.objects[improved] == b"already"               # untouched


def test_repoint_leaves_other_sources_and_already_improved_alone():
    s3 = FakeS3({f"{ENV_SEGMENT}/products/_manifest.json": json.dumps({
        "u1": _url(f"{ENV_SEGMENT}/products/zucchi/aa.jpg"),            # zucchi raw -> repoint
        "u2": _url(f"{ENV_SEGMENT}/products/improved/zucchi/bb.jpg"),   # zucchi already improved -> leave
        "u3": _url(f"{ENV_SEGMENT}/products/marenostone/cc.jpg"),       # other source -> leave
    }).encode()})
    changed = treat.repoint_manifest(s3, "zucchi")
    assert changed == 1
    m = json.loads(s3.objects[f"{ENV_SEGMENT}/products/_manifest.json"])
    assert "/products/improved/zucchi/aa.jpg" in m["u1"]
    assert "/products/improved/zucchi/bb.jpg" in m["u2"]                # unchanged
    assert "/products/marenostone/cc.jpg" in m["u3"]                    # unchanged
    assert f"{ENV_SEGMENT}/products/_manifest.backup.json" in s3.objects  # backed up


def test_treat_source_never_crashes_on_a_bad_image():
    class Boom:
        def process(self, data, watermarked=False, enhance=True):
            raise RuntimeError("corrupt")
    s3 = FakeS3({f"{ENV_SEGMENT}/products/zucchi/aa.jpg": b"x",
                 f"{ENV_SEGMENT}/products/_manifest.json": b"{}"})
    stats = treat.treat_source("zucchi", s3=s3, processor=Boom())
    assert stats.failed == 1 and stats.errors                          # recorded, not raised


def test_enhance_flag_gates_gpu_upscale_but_always_size_reduces():
    # enhance ON -> GPU upscale path; enhance OFF -> NO upscale but still CPU size-reduced (long edge capped)
    # + re-encoded, and still marked processed. No GPU/torch needed (ESRGAN faked).
    import numpy as np
    import cv2
    from stone_pipeline.io.image_processing import ImageProcessor, ImageProcessingConfig
    proc = ImageProcessor(ImageProcessingConfig(enabled=True))

    class _FakeESR:
        def available(self):
            return True

        def enhance(self, bgr):
            return bgr                      # identity; the point is the flag routes here or not

    proc._esr = _FakeESR()
    big = np.full((3000, 4000, 3), 128, np.uint8)                        # oversized -> must be downscaled
    data = cv2.imencode(".jpg", big)[1].tobytes()

    on = proc.process(data, watermarked=False, enhance=True)
    assert on.enhanced and on.upscaled                                  # upscale path ran

    off = proc.process(data, watermarked=False, enhance=False)
    assert off.enhanced and not off.upscaled                            # processed but NOT upscaled
    out = cv2.imdecode(np.frombuffer(off.data, np.uint8), cv2.IMREAD_COLOR)
    assert max(out.shape[:2]) <= proc.cfg.target_long_edge             # size-reduced (CPU, no GPU)
