"""The outline diagnostic (stone_pipeline.photos.diag) on synthetic photos, models stubbed: it records what the real
find_slabs saw, puts the real steps back after, and writes only under the diag prefix."""
from __future__ import annotations

import json

import cv2
import numpy as np
import boto3
import pytest

from stone_pipeline.photos import diag as diag
from stone_pipeline.photos import source as source
from stone_pipeline.config.standardise_settings import STANDARDISE as C
from stone_pipeline.standardise import marks, models, outline, photo


_SEG = (None, None, "cpu")                                                 # SAM is never called: slab_mask is stubbed


def _photo(monkeypatch, left):
    """A photo of one slab whose left edge is at column `left` (0: its stone runs off the photo), boxed by the detector."""
    h, w = 1000, 2000
    rng = np.random.default_rng(3)
    stone = np.clip(170 + cv2.GaussianBlur(rng.standard_normal((h, w)).astype(np.float32), (0, 0), 3) * 3, 0, 255).astype(np.uint8)
    rgb = np.full((h, w, 3), 90, np.uint8)
    slab = np.zeros((h, w), np.uint8)
    slab[300:800, left:1500] = 1
    rgb[slab > 0] = np.dstack([stone] * 3)[slab > 0]
    post = (1200.0, 250.0, 1240.0, 850.0, 0.21)                               # a post the strap step looks at (and rejects: no mask)
    monkeypatch.setattr(outline, "detect", lambda det, img, prompt=None, threshold=None: [post] if prompt else [(float(left), 290.0, 1510.0, 810.0, 0.6)])
    monkeypatch.setattr(marks, "embed", lambda seg, img: None)
    monkeypatch.setattr(marks, "slab_mask", lambda seg, img, b, emb, widest=False: ((slab.copy(),) * 2 if b[2] - b[0] > 100 else (np.zeros_like(slab),) * 2))
    return rgb, slab


def test_a_cut_off_slab_is_recorded_with_the_edge_it_runs_into_and_its_missing_trim(monkeypatch):
    rgb, slab = _photo(monkeypatch, left=0)
    record, images = diag.diagnose(_SEG, None, rgb, (3.0, 1.8))
    assert "cut off" in record["reason"] and record["kind"] == "" and record["quads"] == []
    assert record["frame_sides"][0] == ["left"] and record["box"] == [0.0, 290.0, 1510.0, 810.0]
    rows, columns = record["ends"]
    assert rows["sides"] == ["top", "bottom"] and rows["at_frame"] == [False, False] and rows["trim"] == [0.0, 1.0]
    assert columns["sides"] == ["left", "right"] and columns["at_frame"] == [True, False] and columns["trim"] is None
    assert len(columns["profile"]) == 1023 and set(columns["candidates"]) == {"left", "right"}
    assert np.array_equal(images["mask"], slab) and len(record["outline"]) == 4
    assert record["strap_detections"] == [[1200.0, 250.0, 1240.0, 850.0, 0.21]] and record["straps_px"] == 0
    json.dumps(record)                                                     # the record is plain JSON


def test_a_whole_slab_is_recorded_as_found(monkeypatch):
    rgb, _ = _photo(monkeypatch, left=300)
    record, _ = diag.diagnose(_SEG, None, rgb, None)
    assert record["reason"] == "" and record["kind"] == "slab" and len(record["quads"]) == 1
    assert record["frame_sides"][0] == [] and record["outline_whole"] == [True] and record["steep"] == [None]


@pytest.mark.parametrize("left", [0, 300])
def test_observing_the_outline_step_never_changes_what_it_finds(monkeypatch, left):
    rgb, _ = _photo(monkeypatch, left=left)                                  # a cut-off slab and a whole one
    quads, mask, raw, straps, reason = outline.find_slabs(None, None, rgb)
    record, _ = diag.diagnose(_SEG, None, rgb, None)
    assert record["reason"] == reason and len(record["quads"]) == len(quads)
    assert all(np.allclose(np.float32(r), q, atol=0.05) for r, q in zip(record["quads"], quads))


class _Tensor:
    """Just enough of a torch tensor (CI has no torch) for what SAM hands back: its masks and predicted IoU."""

    def __init__(self, a):
        self.a = np.asarray(a)

    def __getitem__(self, i):
        return _Tensor(self.a[i])

    def float(self):
        return _Tensor(self.a.astype(np.float32))

    def cpu(self):
        return self

    def flatten(self, start):
        return _Tensor(self.a.reshape(*self.a.shape[:start], -1))

    def sum(self, dim):
        return _Tensor(self.a.sum(dim))

    def argmax(self):
        return int(self.a.argmax())

    def tolist(self):
        return self.a.tolist()

    def numpy(self):
        return self.a


class _SamOutput:
    def __init__(self, scores):
        self.iou_scores, self.pred_masks = _Tensor([[scores]]), None


def _sam_model(answers):
    """(sam, proc, device) answering a box prompt with answers[box] = (masks [3, h, w], scores [3]); any other box: no
    stone. The processor keeps the last prompt so SAM and the post-processing answer for it."""
    last = {}

    class Proc:
        class image_processor:
            @staticmethod
            def post_process_masks(pred, original, reshaped):
                return [_Tensor(answers.get(last["box"], answers[None])[0][None])]

        def __call__(self, image, input_boxes=None, return_tensors=None):
            last["box"] = tuple(input_boxes[0][0])
            return {"input_boxes": None, "original_sizes": None, "reshaped_input_sizes": None}

    def sam(image_embeddings=None, input_boxes=None):
        return _SamOutput(answers.get(last["box"], answers[None])[1])
    return sam, Proc(), "cpu"


def _sam_slab_mask(model, rgb, box, emb, widest=False):
    """marks.slab_mask's own calls to SAM (box prompt, SAM, post-processing, the pick) without torch, which CI lacks;
    its trimming is the real one's."""
    sam, proc, _ = model
    proc(None, input_boxes=[[[float(v) for v in box]]], return_tensors="pt")
    out = sam(image_embeddings=emb, input_boxes=None)
    masks = proc.image_processor.post_process_masks(out.pred_masks, None, None)[0][0]
    pick = masks.flatten(1).sum(1).argmax() if widest else out.iou_scores.cpu()[0][0].argmax()
    raw = marks._largest(masks[pick].numpy().astype(np.uint8))
    return raw.copy(), raw


def _proposals_photo(monkeypatch):
    """A whole slab whose box SAM answers with three proposals: the slab (most confident), the slab with a band of wrap
    above it (the largest), and a part of it. Any other box: nothing."""
    h, w = 1000, 2000
    rng = np.random.default_rng(3)
    stone = np.clip(170 + cv2.GaussianBlur(rng.standard_normal((h, w)).astype(np.float32), (0, 0), 3) * 3, 0, 255).astype(np.uint8)
    rgb = np.full((h, w, 3), 90, np.uint8)
    slab, wrapped, part = (np.zeros((h, w), np.uint8) for _ in range(3))
    slab[300:800, 300:1500] = 1
    wrapped[260:800, 300:1500] = 1
    part[350:750, 600:1200] = 1
    rgb[slab > 0] = np.dstack([stone] * 3)[slab > 0]
    box = (290.0, 290.0, 1510.0, 810.0)
    monkeypatch.setattr(outline, "detect", lambda det, img, prompt=None, threshold=None:
                        [(1200.0, 250.0, 1240.0, 850.0, 0.21)] if prompt else [(*box, 0.6)])
    monkeypatch.setattr(marks, "embed", lambda seg, img: None)
    monkeypatch.setattr(marks, "slab_mask", _sam_slab_mask)
    masks, scores = np.stack([slab, wrapped, part]), np.float32([0.97, 0.9, 0.8])
    model = _sam_model({box: (masks, scores), None: (np.zeros((3, h, w), np.uint8), np.float32([0.5, 0.4, 0.3]))})
    return rgb, box, model, masks


def test_sams_proposals_for_the_slab_s_box_are_recorded_with_its_stone_and_straps(monkeypatch):
    rgb, box, model, masks = _proposals_photo(monkeypatch)
    record, images = diag.diagnose(model, None, rgb, None)
    slab = [p for p in record["proposals"] if p["box"] == list(box)]
    assert slab == [{"box": list(box), "scores": [0.97, 0.9, 0.8], "areas": [int(m.sum()) for m in masks]}]
    assert record["picked"] == 0 and len(record["proposals"]) > 1     # the strap and neighbour probes' boxes are recorded too
    assert all(np.array_equal(images[f"proposal-{n}"], masks[n]) for n in range(3))
    assert np.array_equal(images["raw"], masks[0]) and not images["straps"].any() and "mask" in images
    json.dumps(record)


def test_recording_the_proposals_leaves_the_outline_step_byte_identical(monkeypatch):
    rgb, _, model, _ = _proposals_photo(monkeypatch)
    plain = outline.find_slabs(model, None, rgb)
    seen = {}
    real = outline.find_slabs
    monkeypatch.setattr(outline, "find_slabs", lambda *a: seen.setdefault("out", real(*a)))
    diag.diagnose(model, None, rgb, None)                                # find_slabs on SAM as diagnose wraps it
    quads, mask, raw, straps, reason = plain
    assert reason == seen["out"][4] == "" and len(quads) == len(seen["out"][0]) == 1
    assert all(a.tobytes() == b.tobytes() for a, b in zip([*quads, mask, raw, straps], [*seen["out"][0], *seen["out"][1:4]]))


def test_the_real_steps_are_put_back_even_when_the_outline_step_fails(monkeypatch):
    def straps(*args):
        raise RuntimeError("boom")
    monkeypatch.setattr(marks, "embed", lambda seg, img: None)
    monkeypatch.setattr(outline, "detect", lambda *a, **k: [(0.0, 0.0, 1000.0, 1000.0, 0.9)])
    monkeypatch.setattr(outline, "_straps", straps)
    real = {name: getattr(module, name) for module, name in diag._OBSERVED}
    with pytest.raises(RuntimeError, match="boom"):
        diag.diagnose(_SEG, None, np.zeros((1000, 1000, 3), np.uint8), None)
    assert {name: getattr(module, name) for module, name in diag._OBSERVED} == real


class _S3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = Body


def test_a_run_writes_only_the_diag_files_of_the_listed_photos_and_fails_loud_on_a_missing_one(monkeypatch):
    rgb, _ = _photo(monkeypatch, left=0)
    a, b, gone = "a" * 64, "b" * 64, "c" * 64
    s3 = _S3()
    monkeypatch.setenv("SRC", "sentas")
    monkeypatch.setenv("ONLY_KEY", "list.json")
    monkeypatch.setattr(boto3, "client", lambda *a, **k: s3)
    monkeypatch.setattr(source, "listed_shas", lambda client: [a, gone])
    monkeypatch.setattr(source, "scraped_keys", lambda client, src: [f"p/scraped/sentas/{a}.jpg", f"p/scraped/sentas/{b}.jpg"])
    monkeypatch.setattr(source, "sheet_photos", lambda client: {a: {"size": [3.0, 1.8]}})
    monkeypatch.setattr(source, "start", lambda client, out, key, sha: (key, False, ""))
    monkeypatch.setattr(source, "_get", lambda client, key: b"jpeg")
    monkeypatch.setattr(photo, "decode", lambda data: rgb)
    from unittest import mock
    # A fake with the wrong signature hid that the job called load(survey_only=True), a TypeError;
    # an autospec takes exactly what the real load takes
    load = mock.create_autospec(models.load, return_value=models.Models(_SEG, None, None, None, None, None))
    monkeypatch.setattr(models, "load", load)
    assert diag.main() == 1                                                  # the missing photo fails the job
    load.assert_called_once_with("survey")
    assert sorted(s3.objects) == [f"{diag.diag_prefix()}{a}{ext}"
                                  for ext in (".json", ".mask.png", ".overlay.jpg", ".raw.png", ".straps.png")]
    record = json.loads(s3.objects[f"{diag.diag_prefix()}{a}.json"])
    assert record["sha"] == a and record["size"] == [3.0, 1.8] and record["photo"].endswith(f"{a}.jpg")
    over = cv2.imdecode(np.frombuffer(s3.objects[f"{diag.diag_prefix()}{a}.overlay.jpg"], np.uint8), cv2.IMREAD_COLOR)
    assert max(over.shape[:2]) == C.DIAG_LONG_EDGE


def test_a_run_without_its_list_is_refused(monkeypatch):
    monkeypatch.setenv("SRC", "sentas")
    monkeypatch.delenv("ONLY_KEY", raising=False)
    assert diag.main() == 2


def test_a_tagged_run_writes_under_its_own_folder_and_a_bad_tag_is_refused(monkeypatch):
    monkeypatch.setenv("DIAG_TAG", "branch-1")
    assert diag.diag_prefix().endswith(f"/{C.TEST_PREFIX}/{C.DIAG_DIR}/branch-1/")
    monkeypatch.setenv("DIAG_TAG", "../prod")
    with pytest.raises(ValueError, match="DIAG_TAG"):
        diag.diag_prefix()
