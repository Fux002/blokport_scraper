"""One good photo per listing: the judge's answers are read strictly, a listing's photos are judged in its own order until
one passes, a listing with none gets one photo cleaned by the hosted models in turn, hosted spend never passes its cap,
and every answer is kept so a re-run never pays twice. No network: the hosted calls and S3 are stand-ins."""
from __future__ import annotations

import json

import pytest

from deploy import standardise_select as sel
from stone_pipeline.config.settings import STANDARDISE as C
from stone_pipeline.standardise import clean, judge

A, B, D, E = "a" * 64, "b" * 64, "d" * 64, "e" * 64
GPU, CL = "dev/standardise-test/v9/sentas/", "dev/standardise-test/v9-clean/sentas/"


class _S3:
    """A bucket in a dict: what the selection reads and writes."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})

    def get_object(self, Bucket, Key):
        import io
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = Body

    def copy_object(self, Bucket, Key, CopySource, ContentType, MetadataDirective):
        self.objects[Key] = self.objects[CopySource["Key"]]

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)


def _round(prefix, finished=(), held=(), judged=(), cleaned=(), attempts=(), edited=(), requested=()):
    import threading
    r = object.__new__(sel.Round)
    r.prefix, r.finished, r.held, r.judged = prefix, set(finished), set(held), set(judged)
    r.cleaned, r.attempts, r.edited, r.requested = set(cleaned), set(attempts), set(edited), set(requested)
    r._locks, r._guard = {}, threading.Lock()
    return r


def _listing(*photos, key="slab-1"):
    return {"key": key, "source": "sentas", "format": "slab", "photos": list(photos)}


def _verdict(v, *kinds):
    return json.dumps({"verdict": v, "problems": [{"kind": k, "where": "x"} for k in kinds]}).encode()


# --- the judge's answers -------------------------------------------------------------------------------------------
def test_a_verdict_is_read_from_the_json_in_the_answer():
    assert judge.parse_verdict('```json\n{"verdict": "pass", "problems": []}\n```') == {"verdict": "pass", "problems": []}
    v = judge.parse_verdict('Looking at it: {"verdict": "fail", "problems": [{"kind": "bad_fill", "where": "top"}]}')
    assert v["verdict"] == "fail" and v["problems"][0]["kind"] == "bad_fill"


@pytest.mark.parametrize("text", ["", "it looks fine", '{"verdict": "ok"}', '{"verdict": "pass"', "[1, 2]"])
def test_an_answer_that_does_not_parse_is_never_a_pass(text):
    v = judge.parse_verdict(text)
    assert v["verdict"] == "unparsed" and not judge.passed(v)


def test_a_clean_copy_passes_only_when_both_answers_are_literally_true():
    assert judge.parse_clean('{"clean": true, "faithful": true, "problems": ""}')["passes"]
    assert not judge.parse_clean('{"clean": true, "faithful": false}')["passes"]
    assert not judge.parse_clean('{"clean": "true", "faithful": "true"}')["passes"]
    assert not judge.parse_clean("no json")["passes"]


def test_only_reflections_marks_and_fills_are_worth_a_cleaned_copy():
    def v(*kinds):
        return {"verdict": "fail", "problems": [{"kind": k} for k in kinds]}
    assert judge.fixable(v("light_reflection", "leftover_mark"))
    assert not judge.fixable(v("light_reflection", "not_one_slab"))        # stacked slabs stay stacked when cleaned
    assert not judge.fixable(v())                                          # a fail with no reason given
    assert not judge.fixable({"verdict": "pass", "problems": []})


def test_the_judge_needs_its_key_and_says_so(monkeypatch):
    monkeypatch.delenv("FAL_KEY", raising=False)
    with pytest.raises(RuntimeError, match="FAL_KEY"):
        judge.fal_key()


def test_each_edit_model_gets_the_photo_in_the_field_it_takes():
    import cv2
    import numpy as np
    photo = cv2.imencode(".jpg", np.zeros((40, 60, 3), np.uint8))[1].tobytes()
    single = clean.request("kontext", photo)
    listed = clean.request("nano2", photo)
    assert single["image_url"].startswith("data:image/jpeg;base64,") and "image_urls" not in single
    assert len(listed["image_urls"]) == 1 and "image_url" not in listed
    assert single["prompt"] == listed["prompt"] == C.CLEAN_INSTRUCTION


# --- the choice per listing ----------------------------------------------------------------------------------------
def test_the_first_photo_the_judge_passes_is_chosen_and_judging_stops_there(monkeypatch):
    s3 = _S3({f"dev/products/scraped/sentas/{s}.jpg": b"o" for s in (A, B, D)} | {f"{GPU}{s}.main.jpg": b"m" for s in (A, B, D)})
    asked = []
    monkeypatch.setattr(sel.judge, "judge", lambda o, r, f: asked.append(1) or ({"verdict": "pass" if len(asked) == 2 else "fail",
                                                                                 "problems": []}, 0.02))
    rounds = [("v9", {"sentas": _round(GPU, finished=(A, B, D))}), ("v9-clean", {"sentas": _round(CL)})]
    picked, complete = sel.choose(s3, rounds, _listing(A, B, D), set(), sel.Spend(10))
    assert picked == ("v9", B) and complete and len(asked) == 2                          # D is never judged
    assert json.loads(s3.objects[f"{GPU}judged/{B}.json"])["verdict"] == "pass"           # kept for the re-run


def test_a_stored_verdict_is_read_back_never_bought_again(monkeypatch):
    s3 = _S3({f"{GPU}judged/{A}.json": _verdict("pass")})
    monkeypatch.setattr(sel.judge, "judge", lambda *a: pytest.fail("judged twice"))
    rounds = [("v9", {"sentas": _round(GPU, finished=(A,), judged=(A,))}), ("v9-clean", {"sentas": _round(CL)})]
    assert sel.choose(s3, rounds, _listing(A), set(), sel.Spend(10)) == (("v9", A), True)


def test_at_most_the_set_number_of_photos_is_judged_per_listing(monkeypatch):
    photos = [c * 64 for c in "123456"]
    s3 = _S3({f"dev/products/scraped/sentas/{s}.jpg": b"o" for s in photos} | {f"{GPU}{s}.main.jpg": b"m" for s in photos})
    asked = []
    monkeypatch.setattr(sel.judge, "judge", lambda o, r, f: asked.append(1) or ({"verdict": "fail", "problems": []}, 0.0))
    rounds = [("v9", {"sentas": _round(GPU, finished=photos)}), ("v9-clean", {"sentas": _round(CL)})]
    assert sel.choose(s3, rounds, _listing(*photos), set(), sel.Spend(10)) == (None, True)
    assert len(asked) == C.JUDGE_MAX_PER_LISTING


def test_an_excluded_photo_is_never_chosen_and_a_cleaned_copy_counts_after_the_gpu_run():
    s3 = _S3({f"{GPU}judged/{B}.json": _verdict("fail", "light_reflection"), f"{CL}judged/{B}.json": _verdict("pass")})
    rounds = [("v9", {"sentas": _round(GPU, finished=(A, B), judged=(B,))}),
              ("v9-clean", {"sentas": _round(CL, finished=(B,), judged=(B,), cleaned=(B,))})]
    assert sel.choose(s3, rounds, _listing(A, B), {A}) == (("v9-clean", B), True)


def test_without_spend_a_photo_not_judged_yet_leaves_the_listing_incomplete():
    rounds = [("v9", {"sentas": _round(GPU, finished=(A,))}), ("v9-clean", {"sentas": _round(CL)})]
    assert sel.choose(_S3(), rounds, _listing(A), set()) == (None, False)


# --- the hosted clean-up -------------------------------------------------------------------------------------------
def _clean_setup(monkeypatch, passes_at):
    s3 = _S3({f"dev/products/scraped/sentas/{s}.jpg": b"o" for s in (A, B)}
             | {f"{GPU}judged/{A}.json": _verdict("fail", "not_one_slab"), f"{GPU}judged/{B}.json": _verdict("fail", "light_reflection")})
    edits = []
    monkeypatch.setattr(sel.clean, "submit", lambda name, img: edits.append(name) or {"model": name})
    monkeypatch.setattr(sel.clean, "collect", lambda job: f"copy-{job['model']}".encode())
    monkeypatch.setattr(sel.judge, "check_clean", lambda o, c: ({"passes": c == f"copy-{passes_at}".encode()}, 0.02))
    rounds = [("v9", {"sentas": _round(GPU, finished=(A, B), judged=(A, B))}), ("v9-clean", {"sentas": _round(CL)})]
    return s3, edits, rounds


def test_a_listing_with_no_good_photo_has_its_first_fixable_photo_cleaned_model_by_model(monkeypatch):
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="nano2")
    sel.clean_listing(s3, rounds, _listing(A, B), set(), sel.Spend(10))
    assert edits == ["kontext", "nano2"]                                       # A is stacked: not worth an edit
    assert s3.objects[f"{CL}cleaned/{B}.jpg"] == b"copy-nano2"
    assert json.loads(s3.objects[f"{CL}attempts/{B}.kontext.json"])["passes"] is False


def test_a_paid_edit_whose_check_was_not_made_is_reused(monkeypatch):
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="kontext")
    s3.objects[f"{CL}attempts/{B}.kontext.jpg"] = b"copy-kontext"
    rounds[1][1]["sentas"].edited.add(f"{B}.kontext")
    sel.clean_listing(s3, rounds, _listing(A, B), set(), sel.Spend(10))
    assert edits == [] and s3.objects[f"{CL}cleaned/{B}.jpg"] == b"copy-kontext"


def test_a_listing_not_fully_judged_is_never_cleaned(monkeypatch):
    monkeypatch.setattr(sel.clean, "submit", lambda *a: pytest.fail("cleaned before judging"))
    rounds = [("v9", {"sentas": _round(GPU, finished=(A,))}), ("v9-clean", {"sentas": _round(CL)})]
    with pytest.raises(RuntimeError, match="nothing is cleaned before they do"):
        sel.clean_listing(_S3(), rounds, _listing(A), set(), sel.Spend(10))


def test_a_photo_held_by_the_retouch_or_logo_step_is_worth_cleaning_one_held_for_its_outline_is_not():
    s3 = _S3({f"{GPU}held/{A}.json": json.dumps({"reason": "logo: still visible after removal"}).encode(),
              f"{GPU}held/{B}.json": json.dumps({"reason": "slab cut off by the photo edge"}).encode()})
    rnd = _round(GPU, held=(A, B))
    assert sel.fixable(s3, rnd, A, set()) and not sel.fixable(s3, rnd, B, set()) and not sel.fixable(s3, rnd, A, {A})


def test_spend_never_passes_the_cap_even_with_calls_in_flight():
    spend = sel.Spend(0.10)
    spend.reserve(0.06)
    with pytest.raises(sel.SpendCapReached):
        spend.reserve(0.06)                                   # 0.12 with the one still in flight
    spend.settle(0.06, 0.02)
    spend.reserve(0.06)
    assert spend.spent == pytest.approx(0.02)


# --- the standardiser's input and the publish ----------------------------------------------------------------------
class _Bucket:
    def __init__(self, objects):
        self.objects = objects

    def list_objects_v2(self, Bucket, Prefix, MaxKeys):
        return {"KeyCount": sum(k.startswith(Prefix) for k in self.objects)}

    def get_object(self, Bucket, Key):
        import io
        return {"Body": io.BytesIO(self.objects[Key])}


def test_a_cleaned_copy_goes_through_every_step_a_scraped_photo_does():
    # the hosted edit leaves the straps: the cleaned copy is retouched like any photo, only its logo step is skipped
    from deploy import standardise_source as src
    key = f"dev/products/scraped/sentas/{A}.jpg"
    cleaned, retouched = f"{CL}cleaned/{A}.jpg", f"{CL}retouched/{A}.jpg"
    held = json.dumps({"reason": "retouch: too large", "stays_as_shot": False}).encode()
    as_shot = json.dumps({"reason": "retouch: refused", "stays_as_shot": True}).encode()
    assert src.photo_source(_Bucket({cleaned: b""}), CL, key, A) == cleaned
    assert src.photo_source(_Bucket({}), CL, key, A) == key
    assert src.start(_Bucket({cleaned: b"", retouched: b""}), CL, key, A) == (retouched, True, "")
    assert src.start(_Bucket({cleaned: b""}), CL, key, A) == (cleaned, True, "")
    assert src.start(_Bucket({retouched: b""}), CL, key, A) == (retouched, False, "")
    assert src.start(_Bucket({f"{CL}retouch_held/{A}.json": held}), CL, key, A) == (None, False, "retouch: too large")
    assert src.start(_Bucket({f"{CL}retouch_held/{A}.json": as_shot}), CL, key, A) == (key, False, "")


def test_a_photo_with_a_cleaned_copy_is_still_retouched():
    from deploy import standardise_retouch as rt
    assert not rt.retouched(_Bucket({f"{CL}cleaned/{A}.jpg": b""}), CL, A)



# --- review fixes -------------------------------------------------------------------------------------------------------
def test_a_photo_with_no_standardiser_result_leaves_the_listing_open_not_without_a_good_photo():
    s3 = _S3({f"{GPU}judged/{B}.json": _verdict("fail", "light_reflection")})
    rounds = [("v9", {"sentas": _round(GPU, finished=(B,), judged=(B,))}), ("v9-clean", {"sentas": _round(CL)})]
    assert sel.choose(s3, rounds, _listing(A, B), set()) == (None, False)          # A: its GPU job left no result


def test_the_gpu_and_the_judge_look_at_the_same_photos_excluded_ones_left_out_first():
    photos = [c * 64 for c in "123456"]
    assert sel.candidates(_listing(*photos), {photos[0]}) == photos[1:1 + C.JUDGE_MAX_PER_LISTING]


def test_a_photo_two_listings_share_is_judged_once_even_in_parallel(monkeypatch):
    import threading
    import time
    s3 = _S3({f"dev/products/scraped/sentas/{A}.jpg": b"o", f"{GPU}{A}.main.jpg": b"m"})
    calls = []
    monkeypatch.setattr(sel.judge, "judge", lambda o, r, f: (calls.append(1), time.sleep(0.05))[0] or ({"verdict": "pass", "problems": []}, 0.02))
    rounds = [("v9", {"sentas": _round(GPU, finished=(A,))}), ("v9-clean", {"sentas": _round(CL)})]
    spend = sel.Spend(10)
    threads = [threading.Thread(target=sel.choose, args=(s3, rounds, _listing(A, key=k), set(), spend)) for k in ("one", "two")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(calls) == 1


def test_a_sent_edit_is_paid_even_when_collecting_it_fails_and_a_rerun_collects_it_without_sending_again(monkeypatch):
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="kontext")
    monkeypatch.setattr(sel.clean, "collect", lambda job: (_ for _ in ()).throw(TimeoutError("queue slow")))
    spend = sel.Spend(10)
    with pytest.raises(TimeoutError):
        sel.clean_listing(s3, rounds, _listing(A, B), set(), spend)
    assert edits == ["kontext"] and spend.spent == pytest.approx(C.CLEAN_PRICE_USD["kontext"])   # counted when sent
    assert f"{CL}attempts/{B}.kontext.request.json" in s3.objects
    monkeypatch.setattr(sel.clean, "collect", lambda job: b"copy-kontext")
    sel.clean_listing(s3, rounds, _listing(A, B), set(), sel.Spend(10))
    assert edits == ["kontext"] and s3.objects[f"{CL}cleaned/{B}.jpg"] == b"copy-kontext"   # collected, not sent again


def test_a_request_the_queue_no_longer_has_is_forgotten_so_the_next_run_sends_it_again(monkeypatch):
    import httpx
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="kontext")
    gone = httpx.HTTPStatusError("gone", request=httpx.Request("GET", "https://q"), response=httpx.Response(404))
    monkeypatch.setattr(sel.clean, "collect", lambda job: (_ for _ in ()).throw(gone))
    with pytest.raises(httpx.HTTPStatusError):
        sel.clean_listing(s3, rounds, _listing(A, B), set(), sel.Spend(10))
    assert f"{CL}attempts/{B}.kontext.request.json" not in s3.objects


def test_each_listing_s_choice_is_its_own_record_and_a_run_touches_only_its_listings():
    s3 = _S3({f"{GPU}judged/{A}.json": _verdict("pass"), f"{GPU}judged/{B}.json": _verdict("fail", "not_one_slab"),
              "r/chosen/other.json": b'{"key": "other"}', "r/chosen/two.json": b'{"key": "two"}'})
    s = object.__new__(sel.Selection)
    s.client, s.exclude, s.root = s3, set(), "r/"
    s.listings = [_listing(A, key="one"), _listing(B, key="two")]
    s.rounds = [("v9", {"sentas": _round(GPU, finished=(A, B), judged=(A, B))}), ("v9-clean", {"sentas": _round(CL)})]
    result = sel.report(s)
    assert [c["key"] for c in result["chosen"]] == ["one"] and [t["listing"] for t in result["tracked"]] == ["two"]
    assert json.loads(s3.objects["r/chosen/one.json"])["sha"] == A                      # chosen: recorded
    assert "r/chosen/two.json" not in s3.objects                                        # now without a good photo
    assert s3.objects["r/chosen/other.json"] == b'{"key": "other"}'                     # outside this run: untouched
