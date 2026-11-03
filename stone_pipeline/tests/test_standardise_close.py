"""The close-only run (RUN_MODE=standardise-close): only the chosen photos of a whole single slab get their close-up made
again; a photo that gets none, or has none for a reason of its own (a missing or broken input among them), loses its
run's old one (publish then takes the published one down) and its record says why; every photo is done once per
close-up version; the list a slice works through never shifts; retries and GPU crashes count once per job, not per
Batch attempt; anything that is not the photo's own outcome changes nothing and stops the slice loud, and no photo is
ever left out of a run that ends well. No network: S3 is a stand-in."""
from __future__ import annotations

import io
import json

import cv2
import numpy as np
import pytest
from botocore.exceptions import ClientError

from deploy import standardise_close as close
from stone_pipeline.config.settings import STANDARDISE as C
from stone_pipeline.standardise import photo

A, B, D = "a" * 64, "b" * 64, "d" * 64
RUN, CLEAN = "dev/standardise-test/v9/sentas/", "dev/standardise-test/v9-clean/sentas/"
JOB, NEXT_JOB = "job-1", "job-2"


def _error(code):
    return ClientError({"Error": {"Code": code}}, "S3")


class _S3:
    """A bucket in a dict. failing = {key: exception} raised when that key is written or deleted, `times` times (every
    time when None)."""

    def __init__(self, objects=None, failing=None, times=None):
        self.objects, self.failing, self.times = dict(objects or {}), dict(failing or {}), times

    def _fail(self, key):
        if key in self.failing and self.times != 0:
            if self.times is not None:
                self.times -= 1
            raise self.failing[key]

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _error("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType):
        self._fail(Key)
        self.objects[Key] = Body

    def delete_object(self, Bucket, Key):
        self._fail(Key)
        self.objects.pop(Key, None)

    def get_paginator(self, _name):
        objects = self.objects

        class _Pages:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k, "ETag": "\"e\""} for k in sorted(objects) if k.startswith(Prefix)]}
        return _Pages()


def _record(sha, prefix=RUN, source="sentas"):
    return {"source": source, "key": f"slab-{sha[0]}", "sha": sha, "tag": prefix.split("/")[2], "prefix": prefix}


def _done(s3, record):
    return json.loads(s3.objects[close.done_key(record)])


def _slab(*shas, prefix=RUN):
    return {f"{prefix}{s}.slab.png" for s in shas}


def _remakes(monkeypatch, outcomes):
    """remake gives, per sha, an image, None, or raises the exception given."""
    def remake(client, record, photos, m, made):
        out = outcomes[record["sha"]]
        if isinstance(out, BaseException):
            raise out
        return out
    monkeypatch.setattr(close, "remake", remake)


def _redo(s3, records, made, job=JOB):
    return close.redo(s3, records, made, {}, None, job)


IMAGE = np.full((C.OUT_H, C.OUT_W, 3), 128, np.uint8)


# --- which photos -----------------------------------------------------------------------------------------------------
def test_the_list_is_every_chosen_photo_of_the_source_once():
    records = [_record(B), _record(A), _record(D, CLEAN), _record(A), _record(D, source="varsha")]
    assert [(r["prefix"], r["sha"]) for r in close.listed(records, "sentas")] == [(RUN, A), (RUN, B), (CLEAN, D)]
    assert [r["sha"] for r in close.listed(records, "sentas", wanted=[D])] == [D]


def test_the_list_does_not_shift_when_the_run_removes_close_ups(monkeypatch):
    records = [_record(A), _record(B), _record(D)]
    s3 = _S3({f"{RUN}{s}.close.jpg": b"old" for s in (A, B, D)})
    before = close.listed(records, "sentas")
    _remakes(monkeypatch, {A: None, B: None, D: None})
    _redo(s3, before, _slab(A, B, D))
    assert close.listed(records, "sentas") == before                   # built on the choice alone, never on run state


# --- outcomes -----------------------------------------------------------------------------------------------------------
def test_a_new_close_up_replaces_the_runs_old_one(monkeypatch):
    s3 = _S3({f"{RUN}{A}.close.jpg": b"old"})
    _remakes(monkeypatch, {A: IMAGE})
    assert _redo(s3, [_record(A)], _slab(A)) == {"close-up": 1}
    assert s3.objects[f"{RUN}{A}.close.jpg"] != b"old" and _done(s3, _record(A)) == {"close": True, "reason": ""}


def test_a_photo_that_now_gets_no_close_up_loses_its_old_one(monkeypatch):
    s3 = _S3({f"{RUN}{A}.close.jpg": b"old"})
    _remakes(monkeypatch, {A: None})
    assert _redo(s3, [_record(A)], _slab(A)) == {"no close-up": 1}
    assert f"{RUN}{A}.close.jpg" not in s3.objects and _done(s3, _record(A)) == {"close": False, "reason": close.NO_CLOSE_UP}


def test_a_chosen_photo_its_run_made_no_whole_single_slab_of_has_no_close_up():
    s3 = _S3({f"{RUN}{B}.close.jpg": b"old"})                          # a close-up left without its slab (an early run)
    assert _redo(s3, [_record(B)], set()) == {"no close-up": 1}
    assert f"{RUN}{B}.close.jpg" not in s3.objects and _done(s3, _record(B))["reason"] == close.NO_SLAB


def test_a_photo_done_under_this_close_up_version_is_skipped(monkeypatch):
    _remakes(monkeypatch, {})
    assert _redo(_S3(), [_record(A)], _slab(A) | {close.done_key(_record(A))}) == {"already done": 1}
    assert f"v{C.CLOSEUP_VERSION}" in close.done_key(_record(A))


def test_a_photo_with_no_close_up_for_a_reason_of_its_own_loses_it_and_says_why(monkeypatch):
    s3 = _S3({f"{RUN}{A}.close.jpg": b"old"})
    _remakes(monkeypatch, {A: photo.NoCloseUp("not one whole slab now: no outline")})
    assert _redo(s3, [_record(A)], _slab(A)) == {"none of its own": 1}
    assert f"{RUN}{A}.close.jpg" not in s3.objects
    assert _done(s3, _record(A)) == {"close": False, "reason": "not one whole slab now: no outline"}


@pytest.mark.parametrize("error", [TypeError("bug"), cv2.error("cv2 failed"), MemoryError(), _error("AccessDenied")],
                         ids=["TypeError", "cv2.error", "MemoryError", "AccessDenied"])
def test_anything_else_stops_the_slice_and_changes_nothing(monkeypatch, error):
    s3 = _S3({f"{RUN}{A}.close.jpg": b"old", f"{RUN}{B}.close.jpg": b"old"})
    _remakes(monkeypatch, {A: error, B: IMAGE})
    assert _redo(s3, [_record(A), _record(B)], _slab(A, B)) == {"stopped": 1}
    assert s3.objects == {f"{RUN}{A}.close.jpg": b"old", f"{RUN}{B}.close.jpg": b"old"}


def test_a_stopped_slice_keeps_the_counts_it_wrote(monkeypatch):
    _remakes(monkeypatch, {A: IMAGE, B: TypeError("bug")})
    assert _redo(_S3(), [_record(A), _record(B)], _slab(A, B)) == {"close-up": 1, "stopped": 1}


# --- storage: retried, counted once per job ---------------------------------------------------------------------------
def _retries(s3, record):
    return json.loads(s3.objects[close.retries_key(record)])


def test_a_passing_storage_error_is_retried_once_per_job_up_to_the_cap(monkeypatch):
    s3 = _S3({f"{RUN}{A}.close.jpg": b"old"})
    _remakes(monkeypatch, {A: _error("SlowDown")})
    made = lambda: _slab(A) | {k for k in s3.objects if "/retries/" in k}
    assert _redo(s3, [_record(A)], made()) == {"retry": 1} and _retries(s3, _record(A)) == {"runs": 1, "job": JOB}
    assert _redo(s3, [_record(A)], made()) == {"retry": 1} and _retries(s3, _record(A)) == {"runs": 1, "job": JOB}   # Batch's 2nd attempt
    for n in range(2, C.CLOSEUP_RETRY_MAX):
        assert _redo(s3, [_record(A)], made(), job=f"job-{n}") == {"retry": 1}
        assert _retries(s3, _record(A)) == {"runs": n, "job": f"job-{n}"}
    assert s3.objects[f"{RUN}{A}.close.jpg"] == b"old" and close.done_key(_record(A)) not in s3.objects
    assert _redo(s3, [_record(A)], made(), job="job-last") == {"none of its own": 1}                       # it persisted
    assert f"{RUN}{A}.close.jpg" not in s3.objects and "SlowDown" in _done(s3, _record(A))["reason"]


def test_a_store_that_fails_in_passing_is_retried_and_otherwise_stops(monkeypatch):
    _remakes(monkeypatch, {A: IMAGE})
    slow = _S3(failing={f"{RUN}{A}.close.jpg": _error("SlowDown")})
    assert _redo(slow, [_record(A)], _slab(A)) == {"retry": 1}
    denied = _S3(failing={f"{RUN}{A}.close.jpg": _error("AccessDenied")})
    assert _redo(denied, [_record(A)], _slab(A)) == {"stopped": 1}
    assert close.done_key(_record(A)) not in denied.objects


def test_a_retry_record_that_cannot_be_written_stops_the_slice(monkeypatch):
    _remakes(monkeypatch, {A: _error("SlowDown")})
    s3 = _S3(failing={close.retries_key(_record(A)): _error("SlowDown")})
    assert _redo(s3, [_record(A)], _slab(A)) == {"stopped": 1}


def test_a_photo_held_at_the_end_is_written_even_when_its_first_write_fails(monkeypatch):
    # Its write fails once in passing at the retry cap: it is held again, and written, never dropped from a run ending 0.
    s3 = _S3({close.retries_key(_record(A)): json.dumps({"runs": C.CLOSEUP_RETRY_MAX - 1, "job": "job-0"}).encode()},
             failing={f"{RUN}{A}.close.jpg": _error("SlowDown")}, times=1)
    _remakes(monkeypatch, {A: photo.NoCloseUp("not one whole slab now: no outline")})
    tally = _redo(s3, [_record(A)], _slab(A) | {close.retries_key(_record(A))})
    assert close.done_key(_record(A)) in s3.objects and tally == {"none of its own": 1}


# --- a broken GPU: counted once per job ---------------------------------------------------------------------------------
def test_a_broken_gpu_stops_the_slice_and_the_same_photo_breaking_it_in_another_job_has_none(monkeypatch):
    s3 = _S3({f"{RUN}{A}.close.jpg": b"old"})
    _remakes(monkeypatch, {A: RuntimeError("CUDA error: an illegal memory access"), B: IMAGE})
    assert _redo(s3, [_record(A), _record(B)], _slab(A, B)) == {"stopped": 1}
    assert json.loads(s3.objects[close.crashed_key(_record(A))])["job"] == JOB
    made = _slab(A, B) | {close.crashed_key(_record(A))}
    assert _redo(s3, [_record(A)], made) == {"stopped": 1}                       # Batch's 2nd attempt, a bad host: no hold
    assert s3.objects[f"{RUN}{A}.close.jpg"] == b"old"
    assert _redo(s3, [_record(A)], made, job=NEXT_JOB) == {"none of its own": 1}  # another job: it is the photo
    assert f"{RUN}{A}.close.jpg" not in s3.objects


def test_a_crash_marker_that_cannot_be_written_still_stops_the_slice(monkeypatch):
    _remakes(monkeypatch, {A: RuntimeError("CUDA error: an illegal memory access")})
    s3 = _S3(failing={close.crashed_key(_record(A)): _error("SlowDown")})
    assert _redo(s3, [_record(A)], _slab(A)) == {"stopped": 1}


# --- many in a row --------------------------------------------------------------------------------------------------------
def test_many_photos_in_a_row_with_the_same_reason_stop_the_slice_unwritten(monkeypatch):
    shas = [f"{i:064x}" for i in range(C.CLOSEUP_HOLD_STREAK_MAX)]
    _remakes(monkeypatch, {s: photo.NoCloseUp("not one whole slab now: no outline") for s in shas})
    s3 = _S3({f"{RUN}{s}.close.jpg": b"old" for s in shas})
    assert _redo(s3, [_record(s) for s in shas], _slab(*shas)) == {"stopped": 1}
    assert all(s3.objects[f"{RUN}{s}.close.jpg"] == b"old" for s in shas) and not any("close_v" in k for k in s3.objects)


def test_a_few_photos_in_a_row_with_the_same_reason_are_written(monkeypatch):
    shas = [f"{i:064x}" for i in range(C.CLOSEUP_HOLD_STREAK_MAX - 1)]
    _remakes(monkeypatch, {s: photo.NoCloseUp("not one whole slab now: no outline") for s in shas})
    s3 = _S3()
    assert _redo(s3, [_record(s) for s in shas], _slab(*shas)) == {"none of its own": len(shas)}
    assert all(close.done_key(_record(s)) in s3.objects for s in shas)


def test_photos_off_the_product_sheet_never_stop_the_slice(monkeypatch):
    shas = [f"{i:064x}" for i in range(C.CLOSEUP_HOLD_STREAK_MAX + 5)]           # delisted products: an outcome, en masse
    _remakes(monkeypatch, {s: photo.NoCloseUp(close.OFF_SHEET) for s in shas})
    s3 = _S3()
    assert _redo(s3, [_record(s) for s in shas], _slab(*shas)) == {"none of its own": len(shas)}


def test_each_slice_keeps_its_own_summary():
    assert close.summary_key("r/", "sentas", "lists/x-0.json") != close.summary_key("r/", "sentas", "lists/x-1.json")
    assert close.summary_key("r/", "sentas", "") != close.summary_key("r/", "varsha", "")


# --- one photo --------------------------------------------------------------------------------------------------------
def test_the_run_makes_each_photos_close_up_from_the_photo_its_run_started_from(monkeypatch):
    seen = []
    monkeypatch.setattr(close, "start", lambda client, out, key, sha: (f"{out}retouched/{sha}.jpg", False, ""))
    monkeypatch.setattr(close, "own_input", lambda client, key, read=photo.read: key)
    monkeypatch.setattr(close, "start_edits", lambda client, out, key, source_key, cleaned, full: f"fills of {source_key}")

    def close_view(full, *, size, models, edits=None, chosen, saved):
        seen.append((full, size, edits, chosen, saved))
        return None
    monkeypatch.setattr(close.photo, "close_view", close_view)
    assert close.remake(_S3(), _record(A), {A: {"size": [3.0, 1.8]}}, None, _slab(A)) is None
    # no record (made before one was kept): decided against the slab its run saved (own_input stood in: its key)
    assert seen == [(f"{RUN}retouched/{A}.jpg", (3.0, 1.8), f"fills of {RUN}retouched/{A}.jpg", None, f"{RUN}{A}.slab.png")]


def test_a_saved_slab_reads_back_as_it_was_written():
    from deploy import standardise_source as src
    rgb = np.random.default_rng(1).integers(0, 256, (20, 30, 3), dtype=np.uint8)
    alpha = np.linspace(0, 1, 600, dtype=np.float32).reshape(20, 30)
    got_rgb, got_alpha = src.read_slab(src._slab_png((rgb, alpha)))
    assert np.array_equal(got_rgb, rgb) and np.abs(got_alpha - alpha).max() <= 1 / 255 + 1e-6   # written truncated to 8 bits
    with pytest.raises(ValueError):
        src.read_slab(b"not a png")


def test_an_unrecorded_result_whose_saved_slab_is_missing_has_no_close_up_of_its_own(monkeypatch):
    monkeypatch.setattr(close, "start", lambda client, out, key, sha: (f"{out}{sha}.jpg", False, ""))
    monkeypatch.setattr(close, "own_input", lambda client, key, read=photo.read: (_ for _ in ()).throw(photo.NoCloseUp(f"its input is missing: {key}")) if key.endswith(".png") else key)
    monkeypatch.setattr(close, "start_edits", lambda *a: None)
    with pytest.raises(photo.NoCloseUp, match="slab.png"):
        close.remake(_S3(), _record(A), {A: {"size": None}}, None, _slab(A))


def test_the_run_takes_the_slab_its_run_recorded(monkeypatch):
    seen = []
    monkeypatch.setattr(close, "start", lambda client, out, key, sha: (f"{out}{sha}.jpg", False, ""))
    monkeypatch.setattr(close, "own_input", lambda client, key, read=photo.read: key)
    monkeypatch.setattr(close, "start_edits", lambda *a: None)
    monkeypatch.setattr(close.photo, "close_view", lambda full, *, chosen, saved, **k: seen.append((chosen, saved)))
    s3 = _S3({f"{RUN}{A}.slab.json": json.dumps({"index": 1, "of": 2}).encode()})
    close.remake(s3, _record(A), {A: {"size": None}}, None, _slab(A) | {f"{RUN}{A}.slab.json"})
    assert seen == [((1, 2), None)]


@pytest.mark.parametrize("started,photos,why", [((None, False, "retouch: refused"), {A: {"size": None}}, "refused"),
                                                ((f"{RUN}{A}.jpg", False, ""), {}, "product sheet")])
def test_a_photo_its_run_refused_or_off_the_sheet_has_none_of_its_own(monkeypatch, started, photos, why):
    monkeypatch.setattr(close, "start", lambda client, out, key, sha: started)
    with pytest.raises(photo.NoCloseUp, match=why):
        close.remake(_S3(), _record(A), photos, None, _slab(A))


def _jpg(rgb):
    return cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tobytes()


def test_a_photos_missing_or_broken_input_is_its_own_outcome_and_a_denied_one_is_not():
    from deploy import standardise_source as runner
    key = f"dev/products/scraped/sentas/{A}.jpg"
    with pytest.raises(photo.NoCloseUp, match="missing"):
        runner.own_input(_S3(), key)
    with pytest.raises(photo.NoCloseUp, match="no image"):
        runner.own_input(_S3({key: b"not a jpeg"}), key)

    class _Denied(_S3):
        def get_object(self, Bucket, Key):
            raise _error("AccessDenied")
    with pytest.raises(ClientError):
        runner.own_input(_Denied(), key)
    shot = np.random.default_rng(3).integers(60, 200, (400, 600, 3), dtype=np.uint8)
    assert np.array_equal(runner.own_input(_S3({key: _jpg(shot)}), key).shape, shot.shape)


def test_what_of_the_start_photo_is_not_as_shot():
    from deploy import standardise_source as runner
    scraped = np.random.default_rng(3).integers(60, 200, (400, 600, 3), dtype=np.uint8)
    key = f"dev/products/scraped/sentas/{A}.jpg"
    s3 = _S3({key: _jpg(scraped)})
    shot = runner.photo.read(s3.objects[key])
    assert runner.start_edits(s3, RUN, key, key, False, shot) is None                         # the photo as shot
    assert runner.start_edits(s3, RUN, key, f"{CLEAN}cleaned/{A}.jpg", True, shot).all()    # a hosted model's copy
    retouched = shot.copy()
    retouched[100:200, 200:300] = 128                                                         # a retouch's fill
    fills = runner.start_edits(s3, RUN, key, f"{RUN}retouched/{A}.jpg", False, retouched)
    assert fills[110:190, 210:290].all() and fills[:80].sum() == 0
    with pytest.raises(photo.NoCloseUp, match="missing"):                                     # its original is gone
        runner.start_edits(_S3(), RUN, key, f"{RUN}retouched/{A}.jpg", False, retouched)


# --- a result the retired logo step made (deploy.mark_logo_step_results) -----------------------------------------------
def test_a_photo_the_retired_logo_step_made_is_never_remade_whatever_the_close_up_rule(monkeypatch):
    # its close-up would be made from the photo with the logo, unjudged: it is never remade and its close.jpg stays
    from deploy import standardise_source as src
    marked, plain = _record(A), _record(B)
    s3 = _S3({src.logo_step_key(RUN, A): b"{}", f"{RUN}{A}.close.jpg": b"old close-up"})
    assert close.undone(s3, [marked, plain]) == [plain]
    _remakes(monkeypatch, {B: IMAGE})                                  # A is not in it: remaking A would raise
    tally = _redo(s3, [marked, plain], _slab(A, B) | {src.logo_step_key(RUN, A)})
    assert tally == {"made by the retired logo step": 1, "close-up": 1}
    assert s3.objects[f"{RUN}{A}.close.jpg"] == b"old close-up" and close.done_key(marked) not in s3.objects
    assert close.done_key(plain) in s3.objects                         # an unmarked photo is remade
    monkeypatch.setattr(C, "CLOSEUP_VERSION", C.CLOSEUP_VERSION + 1)  # a new close-up rule redoes every photo ...
    assert close.undone(s3, [marked, plain]) == [plain]                # ... but this one
    _remakes(monkeypatch, {B: IMAGE})
    assert _redo(s3, [marked, plain], _slab(A, B) | {src.logo_step_key(RUN, A)})["made by the retired logo step"] == 1
