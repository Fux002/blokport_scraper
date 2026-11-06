"""One good photo per listing: the judge's answers are read strictly, a listing's photos are judged in its own order until
one passes, a listing with none gets one photo cleaned by the hosted models in turn, hosted spend never passes its cap,
and every answer is kept so a re-run never pays twice. No network: the hosted calls and S3 are stand-ins."""
from __future__ import annotations

import json
import math

import pytest

from deploy import standardise_select as sel
from stone_pipeline.config.settings import STANDARDISE as C
from stone_pipeline.standardise import clean, judge

A, B, D, E = "a" * 64, "b" * 64, "d" * 64, "e" * 64


@pytest.fixture(autouse=True)
def _judged_by_inputs(monkeypatch):
    """A judge or check request is its inputs here (judge.judging and judge.checking prepare the real one before anything
    is paid), so a fake judge receives (original, result, fmt) and a fake check (original, copy)."""
    monkeypatch.setattr(sel.judge, "judging", lambda original, result, fmt: (original, result, fmt))
    monkeypatch.setattr(sel.judge, "checking", lambda original, copy: (original, copy))

_REAL_PREPARE = clean.prepare
_REAL_JUDGING = judge.judging


@pytest.fixture(autouse=True)
def _prepared_by_name(monkeypatch):
    """An edit's prepared request is its model's name here (clean.prepare builds the real one before anything is paid),
    so a fake submit receives the model's name."""
    monkeypatch.setattr(sel.clean, "prepare", lambda name, encoded: name)
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


def _round(prefix, finished=(), held=(), judged=(), cleaned=(), attempts=(), edited=(), requested=(), slabs=(), checked=None,
           redrawn=()):
    """checked: the whole slabs drawn under the current outline rule (default: every slab); redrawn: those with a
    complete candidate waiting for the judge."""
    import threading
    r = object.__new__(sel.Round)
    r.prefix, r.finished, r.held, r.judged, r.slabs = prefix, set(finished), set(held), set(judged), set(slabs)
    r.checked, r.logo_step, r.noslab = set(slabs) if checked is None else set(checked), set(), set()
    r.redrawn, r.redraw = set(redrawn), sel.Candidates(prefix, [])
    r.cleaned, r.attempts, r.edited, r.requested = set(cleaned), set(attempts), set(edited), set(requested)
    r._locks, r._guard = {}, threading.Lock()
    r.asked = r.redraw.asked = set()
    return r


def _listing(*photos, key="slab-1", fmt="slab", kept=None):
    return {"key": key, "sku": "SEN-1", "source": "sentas", "format": fmt, "photos": list(photos),
            **({"kept": kept} if kept else {})}


def _clean(client, rounds, listing, exclude, spend, owner=None, click=None):
    """The clean step for one listing: its plan (read-only), then paid."""
    plan = sel.clean_plan(client, rounds, listing, exclude, owner, click)
    if plan is not None:
        sel.pay(client, rounds, plan, spend)


def _verdict(v, *kinds):
    return json.dumps({"verdict": v, "problems": [{"kind": k, "where": "x"} for k in kinds], "version": C.JUDGE_VERSION}).encode()


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
    monkeypatch.setattr(sel.judge, "judge", lambda job: asked.append(1) or ({"verdict": "pass" if len(asked) == 2 else "fail",
                                                                                 "problems": [], "version": C.JUDGE_VERSION}, 0.02))
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
    monkeypatch.setattr(sel.judge, "judge", lambda job: asked.append(1) or ({"verdict": "fail", "problems": [], "version": C.JUDGE_VERSION}, 0.0))
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
             | {f"{GPU}judged/{A}.json": _verdict("fail", "not_one_slab"), f"{GPU}judged/{B}.json": _verdict("fail", "leftover_mark")})
    edits = []
    monkeypatch.setattr(sel.clean, "submit", lambda name: edits.append(name) or {"model": name})
    monkeypatch.setattr(sel.clean, "collect", lambda job: f"copy-{job['model']}".encode())
    monkeypatch.setattr(sel.judge, "check_clean", lambda job: ({"passes": job[1] == f"copy-{passes_at}".encode()}, 0.02))
    rounds = [("v9", {"sentas": _round(GPU, finished=(A, B), judged=(A, B))}), ("v9-clean", {"sentas": _round(CL)})]
    return s3, edits, rounds


def test_a_listing_with_no_good_photo_has_its_first_fixable_photo_cleaned_model_by_model(monkeypatch):
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="kontext")
    _clean(s3, rounds, _listing(A, B), set(), sel.Spend(10))
    assert edits == ["nano2", "kontext"]                                       # A is stacked: not worth an edit
    assert s3.objects[f"{CL}cleaned/{B}.jpg"] == b"copy-kontext"
    assert json.loads(s3.objects[f"{CL}attempts/{B}.nano2.json"])["passes"] is False


def test_the_cheapest_and_most_successful_edit_model_is_tried_first():
    # prod 2026-11-02: Nano Banana 2 ($0.12) passed 55% even on photos Kontext ($0.15) had failed, Kontext 31%, FLUX 3 37%
    assert [name for name, _, _ in C.CLEAN_MODELS] == ["nano2", "kontext", "flux3"]


def test_a_paid_edit_whose_check_was_not_made_is_reused(monkeypatch):
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="nano2")
    s3.objects[f"{CL}attempts/{B}.nano2.jpg"] = b"copy-nano2"
    rounds[1][1]["sentas"].edited.add(f"{B}.nano2")
    _clean(s3, rounds, _listing(A, B), set(), sel.Spend(10))
    assert edits == [] and s3.objects[f"{CL}cleaned/{B}.jpg"] == b"copy-nano2"


def test_an_edit_is_reserved_with_its_check_so_the_cap_never_sends_one_it_cannot_check(monkeypatch):
    # owner 2026-11-05: the cleaning has its own cap; a copy paid for and then left unchecked would be money for nothing
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="nano2")
    spend = sel.Spend(C.CLEAN_PRICE_USD["nano2"] + C.JUDGE_PRICE_USD - 0.01)       # covers the edit, not its check too
    with pytest.raises(sel.SpendCapReached):
        _clean(s3, rounds, _listing(A, B), set(), spend)
    assert edits == [] and spend.spent == 0 and spend.reserved == 0 and spend.capped


def test_every_paid_clean_up_call_is_listed_for_the_listing_that_asked_for_it(monkeypatch):
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="kontext")
    spend = sel.Spend(10)
    _clean(s3, rounds, _listing(A, B), set(), spend)
    listed = [(u["model"], u["call"], u["usd"]) for u in spend.uses]
    assert listed == [("nano2", "edit", C.CLEAN_PRICE_USD["nano2"]), ("nano2", "check", 0.02),
                      ("kontext", "edit", C.CLEAN_PRICE_USD["kontext"]), ("kontext", "check", 0.02)]
    assert {(u["source"], u["sha"], u["sku"], u["listing"]) for u in spend.uses} == {("sentas", B, "SEN-1", "slab-1")}
    assert all(u["at"] for u in spend.uses) and spend.spent == pytest.approx(sum(u["usd"] for u in spend.uses))
    assert spend.reserved == pytest.approx(0, abs=1e-9)                       # every reservation settled or released


def test_an_edit_with_no_usable_answer_counts_at_its_price_and_is_never_sent_again(monkeypatch):
    # review of #530: an edit that may have been billed was sent again next run (paid twice); it is a failed attempt
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="kontext")
    sent = []

    def submit(name):
        sent.append(name)
        if name == "nano2":
            raise TimeoutError("no answer")
        return {"model": name}
    monkeypatch.setattr(sel.clean, "submit", submit)
    spend = sel.Spend(10)
    _clean(s3, rounds, _listing(A, B), set(), spend)
    _clean(s3, rounds, _listing(A, B), set(), spend)                                  # a later run: nothing re-sent
    assert sent == ["nano2", "kontext"] and s3.objects[f"{CL}cleaned/{B}.jpg"] == b"copy-kontext"
    assert json.loads(s3.objects[f"{CL}attempts/{B}.nano2.json"])["passes"] is False
    assert [(u["model"], u["call"], u["usd"], u.get("failed")) for u in spend.uses][0] == (
        "nano2", "edit", C.CLEAN_PRICE_USD["nano2"], True)
    assert spend.reserved == pytest.approx(0, abs=1e-9)


def test_an_edit_collected_in_a_later_run_has_its_check_reserved_then(monkeypatch):
    # the run that sent it could not collect it; the next run's check is paid from the cap like any call, and listed
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="nano2")
    s3.objects[f"{CL}attempts/{B}.nano2.request.json"] = json.dumps({"model": "nano2"}).encode()
    rounds[1][1]["sentas"].requested.add(f"{B}.nano2")
    spend = sel.Spend(10)
    _clean(s3, rounds, _listing(A, B), set(), spend)
    assert edits == [] and [(u["call"], u["usd"]) for u in spend.uses] == [("check", 0.02)] and spend.reserved == 0


def test_a_listing_not_fully_judged_is_never_cleaned_and_is_no_problem_of_the_clean_step(monkeypatch):
    # waiting is a normal state: the report counts the listing pending once, the clean step adds no line for it
    monkeypatch.setattr(sel.clean, "submit", lambda *a: pytest.fail("cleaned before judging"))
    rounds = [("v9", {"sentas": _round(GPU, finished=(A,))}), ("v9-clean", {"sentas": _round(CL)})]
    s = _selection(_S3(), [_listing(A)], rounds)
    assert s.plans() == ([], []) and sel.clean_all(s, [], sel.Spend(10), float("inf")) == ([], False)


def test_a_photo_held_by_the_retouch_or_the_retired_logo_step_is_worth_cleaning_one_held_for_its_outline_is_not():
    # "logo:" holds are no longer written, but the retired logo step's stored ones stay cleanable per photo
    s3 = _S3({f"{GPU}held/{A}.json": json.dumps({"reason": "logo: still visible after removal"}).encode(),
              f"{GPU}held/{B}.json": json.dumps({"reason": "slab cut off by the photo edge"}).encode()})
    rnd = _round(GPU, held=(A, B))
    assert sel.fixable(s3, rnd, A, set()) and not sel.fixable(s3, rnd, B, set()) and not sel.fixable(s3, rnd, A, {A})


def test_what_a_cleaned_copy_would_fix_is_named():
    s3 = _S3({f"{GPU}judged/{A}.json": _verdict("fail", "light_reflection", "leftover_mark"),
              f"{GPU}held/{B}.json": json.dumps({"reason": "retouch: a strap the fill could not cover"}).encode()})
    rnd = _round(GPU, finished=(A,), judged=(A,), held=(B,))
    assert sel.fixable(s3, rnd, A, set()) == {"kinds": ["leftover_mark", "light_reflection"]}
    assert sel.fixable(s3, rnd, B, set()) == {"held": "retouch: a strap the fill could not cover"}


def test_a_mark_on_the_stone_is_cleaned_without_asking_anything_else_only_on_the_owner_s_click(monkeypatch):
    monkeypatch.setattr(C, "CLEAN_ENABLED", True)
    # owner 2026-11-05: logos, labels, stickers, tape, straps and film are cleaned automatically; the rest by click
    assert sel.authorised({"kinds": ["leftover_mark"]}, A, None) == "auto"
    assert sel.authorised({"kinds": ["leftover_mark", "light_reflection"]}, A, None) == "auto"   # the edit removes both
    assert sel.authorised({"held": "retouch: a strap"}, A, None) == "auto"
    assert sel.authorised({"held": "logo: still visible"}, A, None) == "auto"
    assert sel.authorised({"kinds": ["light_reflection"]}, A, None) is None
    assert sel.authorised({"kinds": ["bad_fill"]}, A, {"sha": B}) is None                    # the click names another photo
    assert sel.authorised({"kinds": ["scene_reflection"]}, A, {"sha": A}) == "click"


def test_a_reflection_waits_for_the_owner_s_click_then_is_cleaned_and_listed_as_clicked(monkeypatch):
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="nano2")
    s3.objects[f"{GPU}judged/{B}.json"] = _verdict("fail", "light_reflection")
    spend = sel.Spend(10)
    _clean(s3, rounds, _listing(A, B), set(), spend)
    assert edits == [] and spend.uses == []                                            # no click: nothing paid
    _clean(s3, rounds, _listing(A, B), set(), spend, click=_click(B))
    assert edits == ["nano2"] and s3.objects[f"{CL}cleaned/{B}.jpg"] == b"copy-nano2"
    assert {(u["reason"], u["click_at"], tuple(u["kinds"])) for u in spend.uses} == {
        ("click", "2026-11-05T18:00:00Z", ("light_reflection",))}


def test_an_automatic_clean_is_listed_with_what_it_fixes(monkeypatch):
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="nano2")
    spend = sel.Spend(10)
    _clean(s3, rounds, _listing(A, B), set(), spend)
    assert {(u["reason"], tuple(u["kinds"])) for u in spend.uses} == {("auto", ("leftover_mark",))}
    assert all("click_at" not in u for u in spend.uses)


def test_each_listing_is_planned_with_its_own_click(monkeypatch):
    seen = []
    monkeypatch.setattr(sel, "clean_plan", lambda client, rounds, l, exclude, owner, click: seen.append((l["sku"], click)))
    s = _selection(_S3(), [_listing(A, key="one"), {**_listing(B, key="two"), "sku": "SEN-2"}], [])
    s.clicks = {"SEN-2": {"sha": B, "at": "t"}}
    s.plans()
    assert sorted(seen) == [("SEN-1", None), ("SEN-2", {"sha": B, "at": "t"})]


def _click(sha, at="2026-11-05T18:00:00Z", **over):
    return {"sku": "SEN-1", "listing": "slab-1", "source": "sentas", "sha": sha, "at": at, **over}


def _all_models_failed(s3, rounds, sha):
    for name in sel.clean.MODELS:
        s3.objects[f"{CL}attempts/{sha}.{name}.json"] = json.dumps({"passes": False, "clean_version": C.CLEAN_VERSION}).encode()
        rounds[1][1]["sentas"].attempts.add(f"{sha}.{name}")


def test_the_clicked_photo_is_cleaned_before_an_earlier_mark(monkeypatch):
    # review of #525: one photo per listing is cleaned, and an earlier mark photo always took the slot from the click
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="nano2")
    s3.objects[f"{GPU}judged/{A}.json"] = _verdict("fail", "leftover_mark")
    s3.objects[f"{GPU}judged/{B}.json"] = _verdict("fail", "light_reflection")
    spend = sel.Spend(10)
    _clean(s3, rounds, _listing(A, B), set(), spend, click=_click(B))
    assert {u["sha"] for u in spend.uses} == {B} and s3.objects[f"{CL}cleaned/{B}.jpg"] == b"copy-nano2"


def test_a_photo_with_no_model_left_never_takes_the_slot(monkeypatch):
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="nano2")
    s3.objects[f"{GPU}judged/{A}.json"] = _verdict("fail", "leftover_mark")
    s3.objects[f"{GPU}judged/{B}.json"] = _verdict("fail", "leftover_mark")
    _all_models_failed(s3, rounds, A)
    spend = sel.Spend(10)
    _clean(s3, rounds, _listing(A, B), set(), spend)
    assert edits == ["nano2"] and {u["sha"] for u in spend.uses} == {B}


def test_with_the_clean_switch_off_a_mark_waits_and_a_click_is_cleaned(monkeypatch):
    monkeypatch.setattr(C, "CLEAN_ENABLED", False)
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="nano2")                  # B: a mark
    _clean(s3, rounds, _listing(A, B), set(), sel.Spend(10))
    assert edits == []
    spend = sel.Spend(10)
    _clean(s3, rounds, _listing(A, B), set(), spend, click=_click(B))       # a clicked mark: a click always works
    assert edits == ["nano2"] and {u["reason"] for u in spend.uses} == {"click"}
    assert sel.authorised({"kinds": ["leftover_mark"]}, B, _click(B)) == "click"
    assert sel.authorised({"held": "logo: still visible"}, B, None) is None             # switch off: no automatic edit


def test_with_the_clean_switch_on_a_mark_is_cleaned_without_a_click(monkeypatch):
    monkeypatch.setattr(C, "CLEAN_ENABLED", True)
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="nano2")
    _clean(s3, rounds, _listing(A, B), set(), sel.Spend(10))
    assert edits == ["nano2"]


@pytest.mark.parametrize("click, why", [
    (_click(B, at=""), "no time"),
    (_click(B, sku="SEN-9"), "names SKU"),
    (_click(B, listing="slab-9"), "names listing"),
    (_click(D), "not the listing's"),
    (_click(A), "cannot fix"),                                                       # A is stacked: not fixable
    ({"error": "not JSON (bad)"}, "cannot be read"),
])
def test_a_click_that_cannot_be_used_is_never_paid_for_and_the_listing_says_why(monkeypatch, click, why):
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="nano2")
    s3.objects[f"{GPU}judged/{B}.json"] = _verdict("fail", "light_reflection")       # needs a click
    spend = sel.Spend(10)
    _clean(s3, rounds, _listing(A, B), set(), spend, click=click)
    assert edits == [] and spend.uses == []
    issue = sel.click_problem(s3, rounds[0][1]["sentas"], _listing(A, B), click, set())
    assert why in issue
    state = sel.click_state(s3, rounds, _listing(A, B), set(), click, "open")
    assert state == ("blocked", issue)
    assert sel.click_line(click, state) in sel.photo_notes(s3, rounds, _listing(A, B), set(), {}, click, state)
    assert sel.click_line(click, state) == (f"owner's click on {click['sha'][:12]}" if "sha" in click
                                            else "owner's click") + f": blocked: {issue}"


def test_a_click_that_cannot_be_used_is_reported_for_a_pending_listing_too():
    # review of #525: only listings on the review list said so; a listing not fully judged said nothing
    rounds = [("v9", {"sentas": _round(GPU, finished=(A,))}), ("v9-clean", {"sentas": _round(CL)})]   # A not judged
    s = _selection(_S3(), [_listing(A)], rounds)
    s.root, s.clicks = "r/", {"SEN-1": _click(D)}
    out = sel.report(s, {})
    assert out["pending"] == ["slab-1"] and out["click_problems"] == [
        f"slab-1: owner's click on {D[:12]}: blocked: it names a photo that is not the listing's"]


def test_an_edit_paid_before_the_gate_is_always_collected_and_checked_and_nothing_new_is_sent(monkeypatch):
    # review of #525: a request sent before the gate, for a photo that now needs a click, was never collected
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="nano2")
    s3.objects[f"{GPU}judged/{B}.json"] = _verdict("fail", "light_reflection")       # no click: not authorised now
    s3.objects[f"{CL}attempts/{B}.nano2.request.json"] = json.dumps({"model": "nano2"}).encode()
    rounds[1][1]["sentas"].requested.add(f"{B}.nano2")
    spend = sel.Spend(10)
    _clean(s3, rounds, _listing(A, B), set(), spend)
    assert edits == [] and s3.objects[f"{CL}cleaned/{B}.jpg"] == b"copy-nano2"
    assert [(u["reason"], u["call"]) for u in spend.uses] == [("paid", "check")]


def test_a_paid_edit_that_fails_its_check_sends_no_new_one_without_a_reason(monkeypatch):
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="kontext")               # nano2's copy fails its check
    s3.objects[f"{GPU}judged/{B}.json"] = _verdict("fail", "light_reflection")
    s3.objects[f"{CL}attempts/{B}.nano2.request.json"] = json.dumps({"model": "nano2"}).encode()
    rounds[1][1]["sentas"].requested.add(f"{B}.nano2")
    _clean(s3, rounds, _listing(A, B), set(), sel.Spend(10))
    assert edits == []


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

    def get_paginator(self, name):
        objects = self.objects

        class _Pages:
            def paginate(self, Bucket, Prefix):
                return [{"Contents": [{"Key": k} for k in sorted(objects) if k.startswith(Prefix)]}]
        return _Pages()


def test_a_cleaned_copy_goes_through_every_step_a_scraped_photo_does():
    # the hosted edit leaves the straps: the cleaned copy is retouched like any photo
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


def test_a_whole_slab_made_under_the_older_outline_rule_is_never_judged_before_the_gpu_checked_it(monkeypatch):
    s3 = _S3({f"{GPU}{A}.main.jpg": b"m"})
    monkeypatch.setattr(sel.judge, "judge", lambda *a: pytest.fail("judged an outdated result"))
    rounds = [("v9", {"sentas": _round(GPU, finished=(A,), slabs=(A,), checked=())}), ("v9-clean", {"sentas": _round(CL)})]
    assert sel.choose(s3, rounds, _listing(A), set(), spend=sel.Spend(5.0)) == (None, False)   # open until it is checked
    redrawn = _round(GPU, finished=(A,), slabs=(A,), checked=(), redrawn=(A,))       # checked: its redraw not judged yet
    assert sel.choose(s3, [("v9", {"sentas": redrawn}), ("v9-clean", {"sentas": _round(CL)})], _listing(A), set(),
                      spend=sel.Spend(5.0)) == (None, False)


def test_the_gpu_and_the_judge_look_at_the_same_photos_excluded_ones_left_out_first():
    photos = [c * 64 for c in "123456"]
    assert sel.candidates(_listing(*photos), {photos[0]}) == photos[1:1 + C.JUDGE_MAX_PER_LISTING]


def test_a_photo_two_listings_share_is_judged_once_even_in_parallel(monkeypatch):
    import threading
    import time
    s3 = _S3({f"dev/products/scraped/sentas/{A}.jpg": b"o", f"{GPU}{A}.main.jpg": b"m"})
    calls = []
    monkeypatch.setattr(sel.judge, "judge", lambda job: (calls.append(1), time.sleep(0.05))[0] or ({"verdict": "pass", "problems": []}, 0.02))
    rounds = [("v9", {"sentas": _round(GPU, finished=(A,))}), ("v9-clean", {"sentas": _round(CL)})]
    spend = sel.Spend(10)
    threads = [threading.Thread(target=sel.choose, args=(s3, rounds, _listing(A, key=k), set(), spend)) for k in ("one", "two")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(calls) == 1


def test_a_sent_edit_is_paid_even_when_collecting_it_fails_and_a_rerun_collects_it_without_sending_again(monkeypatch):
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="nano2")
    monkeypatch.setattr(sel.clean, "collect", lambda job: (_ for _ in ()).throw(TimeoutError("queue slow")))
    spend = sel.Spend(10)
    with pytest.raises(TimeoutError):
        _clean(s3, rounds, _listing(A, B), set(), spend)
    assert edits == ["nano2"] and spend.spent == pytest.approx(C.CLEAN_PRICE_USD["nano2"])   # counted when sent
    assert f"{CL}attempts/{B}.nano2.request.json" in s3.objects
    monkeypatch.setattr(sel.clean, "collect", lambda job: b"copy-nano2")
    _clean(s3, rounds, _listing(A, B), set(), sel.Spend(10))
    assert edits == ["nano2"] and s3.objects[f"{CL}cleaned/{B}.jpg"] == b"copy-nano2"   # collected, not sent again


def test_a_paid_request_the_queue_no_longer_has_is_a_failed_attempt_never_sent_again(monkeypatch):
    # review of #530: a 404/410 at collect deleted the request, so the next run paid for the same edit again
    import httpx
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="kontext")
    gone = httpx.HTTPStatusError("gone", request=httpx.Request("GET", "https://q"), response=httpx.Response(404))

    def collect(job):
        if job["model"] == "nano2":
            raise gone
        return f"copy-{job['model']}".encode()
    monkeypatch.setattr(sel.clean, "collect", collect)
    spend = sel.Spend(10)
    _clean(s3, rounds, _listing(A, B), set(), spend)
    _clean(s3, rounds, _listing(A, B), set(), spend)
    assert edits == ["nano2", "kontext"] and s3.objects[f"{CL}cleaned/{B}.jpg"] == b"copy-kontext"
    assert "its result could not be found (404)" in json.loads(s3.objects[f"{CL}attempts/{B}.nano2.json"])["problems"]
    assert f"{CL}attempts/{B}.nano2.request.json" in s3.objects                       # kept: what was paid for


def test_a_request_not_finished_yet_is_collected_again_next_run_never_sent_again(monkeypatch):
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="nano2")
    monkeypatch.setattr(sel.clean, "collect", lambda job: (_ for _ in ()).throw(TimeoutError("not done")))
    with pytest.raises(TimeoutError):
        _clean(s3, rounds, _listing(A, B), set(), sel.Spend(10))
    monkeypatch.setattr(sel.clean, "collect", lambda job: b"copy-nano2")
    rounds[1][1]["sentas"].requested.add(f"{B}.nano2")                                 # the stores as read next run
    _clean(s3, rounds, _listing(A, B), set(), sel.Spend(10))
    assert edits == ["nano2"] and s3.objects[f"{CL}cleaned/{B}.jpg"] == b"copy-nano2"


def test_a_send_that_never_reached_the_service_costs_nothing_and_is_tried_again(monkeypatch):
    import httpx
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="nano2")
    down = httpx.ConnectError("no route", request=httpx.Request("POST", "https://q"))
    monkeypatch.setattr(sel.clean, "submit", lambda name: (_ for _ in ()).throw(down))
    spend = sel.Spend(10)
    with pytest.raises(httpx.ConnectError):
        _clean(s3, rounds, _listing(A, B), set(), spend)
    assert spend.spent == 0 and f"{CL}attempts/{B}.nano2.json" not in s3.objects


def test_each_listing_s_choice_is_its_own_record_and_a_run_touches_only_its_listings():
    s3 = _S3({f"{GPU}judged/{A}.json": _verdict("pass"), f"{GPU}judged/{B}.json": _verdict("fail", "not_one_slab"),
              "r/chosen/other.json": b'{"key": "other"}', "r/chosen/two.json": b'{"key": "two"}'})
    s = object.__new__(sel.Selection)
    s.client, s.exclude, s.root, s.owner, s.conflicts, s.clicks = s3, set(), "r/", {}, {}, {}
    s.listings = [_listing(A, key="one"), _listing(B, key="two")]
    s.rounds = [("v9", {"sentas": _round(GPU, finished=(A, B), judged=(A, B))}), ("v9-clean", {"sentas": _round(CL)})]
    result = sel.report(s, {})
    assert [c["key"] for c in result["chosen"]] == ["one"] and [t["listing"] for t in result["tracked"]] == ["two"]
    assert json.loads(s3.objects["r/chosen/one.json"])["sha"] == A                      # chosen: recorded
    assert "r/chosen/two.json" not in s3.objects                                        # now without a good photo
    assert s3.objects["r/chosen/other.json"] == b'{"key": "other"}'                     # outside this run: untouched



def test_the_gpu_list_is_the_judge_s_candidates_when_a_listing_s_first_photo_is_excluded():
    photos = [c * 64 for c in "12345"]
    s = object.__new__(sel.Selection)
    s.client, s.listings, s.exclude, s.owner, s.conflicts = _S3(), [_listing(*photos)], {photos[0]}, {}, {}
    s.sources, s.rounds, s.chosen = ["sentas"], [("v9", {"sentas": _round(GPU)}), ("v9-clean", {"sentas": _round(CL)})], []
    assert s.gpu_todo() == {"sentas": sel.candidates(_listing(*photos), {photos[0]})} == {"sentas": photos[1:4]}


def test_a_stop_cancels_the_listings_that_have_not_started(monkeypatch):
    monkeypatch.setattr(C, "SELECT_WORKERS", 1)
    done = []

    def work(listing):
        done.append(listing["key"])
        if listing["key"] == "l0":
            raise SystemExit("stopped by signal 15")
    with pytest.raises(SystemExit):
        sel._each([_listing(A, key=f"l{i}") for i in range(20)], work, math.inf)
    assert len(done) < 20


@pytest.mark.parametrize("failure", ["refused", "no image"])
def test_an_edit_the_model_failed_for_good_is_a_failed_attempt_and_the_next_model_gets_its_turn(monkeypatch, failure):
    import httpx
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="kontext")
    refused = httpx.HTTPStatusError("422", request=httpx.Request("GET", "https://q"), response=httpx.Response(422))
    error = refused if failure == "refused" else sel.clean.NoImage("finished without an image")

    def collect(job):
        if job["model"] == "nano2":
            raise error
        return f"copy-{job['model']}".encode()
    monkeypatch.setattr(sel.clean, "collect", collect)
    _clean(s3, rounds, _listing(A, B), set(), sel.Spend(10))
    assert edits == ["nano2", "kontext"] and s3.objects[f"{CL}cleaned/{B}.jpg"] == b"copy-kontext"
    assert json.loads(s3.objects[f"{CL}attempts/{B}.nano2.json"])["passes"] is False



def test_no_listing_starts_after_the_work_window():
    started = []
    failures, stopped = sel._each([_listing(A, key=f"l{i}") for i in range(3)], started.append, 10, clock=lambda: 11)
    assert started == [] and failures == [] and not stopped                # not a stop: the listings are simply pending


def test_the_budget_counts_what_earlier_runs_of_the_month_spent():
    spend = sel.Spend(1.0, others=lambda: 0.9)
    with pytest.raises(sel.SpendCapReached):
        spend.reserve(0.14)
    assert spend.capped
    assert not sel.Spend(1.0, others=lambda: 0.5).capped


def test_the_run_s_total_is_kept_after_every_change_a_reserve_counting_as_spent():
    kept = []
    spend = sel.Spend(1.0, save=lambda total, uses: kept.append(total))
    spend.reserve(0.14)
    spend.settle(0.14, 0.02)
    assert kept == [pytest.approx(0.14), pytest.approx(0.02)]


def test_a_halted_spend_lets_no_call_start_and_says_why():
    spend = sel.Spend(10)
    spend.halt("the work window ended")
    with pytest.raises(sel.Halted, match="the work window ended"):
        spend.reserve(0.01)


def test_a_stop_can_halt_the_spend_while_its_lock_is_held():
    import threading
    spend = sel.Spend(10)
    with spend._lock:                                       # a signal arriving while a reserve holds the lock
        t = threading.Thread(target=spend.halt, args=("stopped by signal 15",))
        t.start()
        t.join(timeout=2)
        assert not t.is_alive()
    with pytest.raises(sel.Halted, match="signal 15"):
        spend.reserve(0.01)


def test_the_spend_is_saved_and_the_month_summed_outside_its_lock():
    held = []

    def free():
        got = spend._lock.acquire(blocking=False)           # nobody holds it while S3 is asked
        if got:
            spend._lock.release()
        held.append(not got)
        return 0.0
    spend = sel.Spend(10, save=lambda total, uses: free())
    spend.others = free
    spend.reserve(0.14)
    spend.settle(0.14, 0.02)
    assert held and not any(held)


def test_the_last_total_saved_is_the_latest_even_when_saves_overlap():
    import threading
    saved, gate = [], threading.Event()

    def save(total, uses):
        if not saved:
            gate.wait(0.5)                                  # the first save is slow
        saved.append(total)
    spend = sel.Spend(10, save=save)
    first = threading.Thread(target=spend.reserve, args=(1.0,))
    first.start()
    spend.reserve(2.0)
    gate.set()
    first.join()
    assert saved[-1] == pytest.approx(3.0)


def test_a_stop_cancels_the_pool_s_work_not_yet_started():
    from deploy import standardise_publish as pub
    done = []

    def work(i):
        done.append(i)
        if i == 0:
            raise SystemExit("stopped by signal 15")
        return i
    with pytest.raises(SystemExit):
        pub.pool_map(1, work, range(50))
    assert len(done) < 50


def _judged_setup(monkeypatch, answer):
    s3 = _S3({f"dev/products/scraped/sentas/{A}.jpg": b"o", f"{GPU}{A}.main.jpg": b"m"})
    monkeypatch.setattr(sel.judge, "judge", answer)
    return s3, [("v9", {"sentas": _round(GPU, finished=(A,))}), ("v9-clean", {"sentas": _round(CL)})]


def test_a_judge_call_that_fails_after_it_was_sent_counts_at_its_reserved_price(monkeypatch):
    s3, rounds = _judged_setup(monkeypatch, lambda job: (_ for _ in ()).throw(TimeoutError("read timed out")))
    spend = sel.Spend(10)
    with pytest.raises(TimeoutError):
        sel.choose(s3, rounds, _listing(A), set(), spend)
    assert spend.spent == pytest.approx(C.JUDGE_PRICE_USD) and spend.reserved == 0


def test_an_answer_without_its_cost_counts_at_the_reserved_price_and_says_so(monkeypatch, capsys):
    s3, rounds = _judged_setup(monkeypatch, lambda job: ({"verdict": "pass", "problems": [], "version": C.JUDGE_VERSION}, None))
    spend = sel.Spend(10)
    sel.choose(s3, rounds, _listing(A), set(), spend)
    assert spend.spent == pytest.approx(C.JUDGE_PRICE_USD)
    assert "no usage.cost" in capsys.readouterr().out
    assert json.loads(s3.objects[f"{GPU}judged/{A}.json"])["cost"] == pytest.approx(C.JUDGE_PRICE_USD)


def test_the_judge_answer_s_missing_cost_is_none_never_free(monkeypatch):
    import httpx
    monkeypatch.setenv("FAL_KEY", "k")
    monkeypatch.setattr(judge, "data_uri", lambda *a: "data:")
    answer = httpx.Response(200, json={"output": "{}", "usage": {}}, request=httpx.Request("POST", C.JUDGE_URL))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: answer)
    assert judge.ask(judge.prepare("s", "p", [b"x"])) == ("{}", None)


def test_a_judge_request_that_cannot_be_prepared_costs_nothing_and_is_loud(monkeypatch):
    # review of #540: the judge encoded its images inside the paid call, so an undecodable photo was charged every run
    s3 = _S3({f"dev/products/scraped/sentas/{A}.jpg": b"o", f"{GPU}{A}.main.jpg": b"m"})
    monkeypatch.setattr(sel.judge, "judging", lambda o, r, f: (_ for _ in ()).throw(ValueError("undecodable")))
    monkeypatch.setattr(sel.judge, "judge", lambda job: pytest.fail("asked"))
    rounds = [("v9", {"sentas": _round(GPU, finished=(A,))}), ("v9-clean", {"sentas": _round(CL)})]
    spend = sel.Spend(10)
    with pytest.raises(ValueError):
        sel.choose(s3, rounds, _listing(A), set(), spend)
    assert spend.spent == 0 and spend.reserved == 0


def test_the_real_judge_preparation_fails_before_anything_is_sent(monkeypatch):
    import httpx
    monkeypatch.setenv("FAL_KEY", "key")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("sent"))
    with pytest.raises(Exception):
        _REAL_JUDGING(b"not an image", b"neither", "slab")


def test_the_report_works_listings_in_parallel_and_keeps_their_order(monkeypatch):
    import threading
    import time
    s3 = _S3({f"{GPU}judged/{s}.json": _verdict("pass") for s in (A, B, D, E)})
    s = object.__new__(sel.Selection)
    s.client, s.exclude, s.root, s.owner, s.conflicts, s.clicks = s3, set(), "r/", {}, {}, {}
    s.listings = [_listing(x, key=k) for x, k in ((A, "one"), (B, "two"), (D, "three"), (E, "four"))]
    s.rounds = [("v9", {"sentas": _round(GPU, finished=(A, B, D, E), judged=(A, B, D, E))}), ("v9-clean", {"sentas": _round(CL)})]
    at_once, now, lock = [0], [0], threading.Lock()
    choose = sel.choose

    def slow(*a, **k):
        with lock:
            now[0] += 1
            at_once[0] = max(at_once[0], now[0])
        time.sleep(0.05)
        with lock:
            now[0] -= 1
        return choose(*a, **k)
    monkeypatch.setattr(sel, "choose", slow)
    result = sel.report(s, {})
    assert [c["key"] for c in result["chosen"]] == ["one", "two", "three", "four"] and at_once[0] > 1


def test_an_auth_or_balance_error_from_the_queue_is_loud_never_a_failed_edit(monkeypatch):
    import httpx
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="nano2")
    locked = httpx.HTTPStatusError("403", request=httpx.Request("GET", "https://q"), response=httpx.Response(403))
    monkeypatch.setattr(sel.clean, "collect", lambda job: (_ for _ in ()).throw(locked))
    with pytest.raises(httpx.HTTPStatusError):
        _clean(s3, rounds, _listing(A, B), set(), sel.Spend(10))
    assert f"{CL}attempts/{B}.nano2.json" not in s3.objects and edits == ["nano2"]   # retried next run, not given up


# --- one refusal rule (review 2026-11-06, A2.2) ----------------------------------------------------------------------
# A model that refused the photo at submit (nothing billed) was counted at its list price, paid again by the step's
# second pass, and never let the next model try: phantom spend against the cleaning cap.
def _refused_at_submit(monkeypatch, code, model="nano2"):
    import httpx
    refusal = httpx.HTTPStatusError(str(code), request=httpx.Request("POST", "https://q"), response=httpx.Response(code))
    submitted = []

    def submit(name):
        submitted.append(name)
        if name == model:
            raise refusal
        return {"model": name}
    monkeypatch.setattr(sel.clean, "submit", submit)
    return submitted


def test_an_edit_refused_at_submit_costs_nothing_and_the_next_model_gets_its_turn(monkeypatch):
    s3, _, rounds = _clean_setup(monkeypatch, passes_at="kontext")
    submitted = _refused_at_submit(monkeypatch, 422)
    spend = sel.Spend(10)
    _clean(s3, rounds, _listing(A, B), set(), spend)
    assert submitted == ["nano2", "kontext"] and s3.objects[f"{CL}cleaned/{B}.jpg"] == b"copy-kontext"
    assert json.loads(s3.objects[f"{CL}attempts/{B}.nano2.json"])["passes"] is False         # never sent again
    assert spend.spent == pytest.approx(C.CLEAN_PRICE_USD["kontext"] + 0.02) and spend.reserved == pytest.approx(0, abs=1e-9)
    assert [(u["model"], u["call"], u["usd"], u.get("refused")) for u in spend.uses] == [
        ("nano2", "edit", 0.0, 422), ("kontext", "edit", C.CLEAN_PRICE_USD["kontext"], None), ("kontext", "check", 0.02, None)]


def test_the_step_s_second_pass_never_pays_again_for_a_refused_model(monkeypatch):
    s3, _, rounds = _clean_setup(monkeypatch, passes_at="none")                         # no copy passes its check
    submitted = _refused_at_submit(monkeypatch, 422)
    spend = sel.Spend(10)
    _clean(s3, rounds, _listing(A, B), set(), spend)
    _clean(s3, rounds, _listing(A, B), set(), spend)
    assert submitted == ["nano2", "kontext", "flux3"]                                    # each model asked once


def test_an_auth_or_rate_error_at_submit_is_loud_costs_nothing_and_is_tried_again_next_run(monkeypatch):
    import httpx
    s3, _, rounds = _clean_setup(monkeypatch, passes_at="nano2")
    submitted = _refused_at_submit(monkeypatch, 429)
    spend = sel.Spend(10)
    with pytest.raises(httpx.HTTPStatusError):
        _clean(s3, rounds, _listing(A, B), set(), spend)
    assert submitted == ["nano2"] and f"{CL}attempts/{B}.nano2.json" not in s3.objects
    assert spend.spent == 0 and spend.reserved == pytest.approx(0, abs=1e-9)


def test_a_judge_call_refused_with_a_4xx_costs_nothing(monkeypatch):
    import httpx
    s3 = _S3({f"dev/products/scraped/sentas/{A}.jpg": b"o", f"{GPU}{A}.main.jpg": b"m"})
    refusal = httpx.HTTPStatusError("429", request=httpx.Request("POST", "https://q"), response=httpx.Response(429))
    monkeypatch.setattr(sel.judge, "judge", lambda job: (_ for _ in ()).throw(refusal))
    rounds = [("v9", {"sentas": _round(GPU, finished=(A,))}), ("v9-clean", {"sentas": _round(CL)})]
    spend = sel.Spend(10)
    with pytest.raises(httpx.HTTPStatusError):
        sel.choose(s3, rounds, _listing(A), set(), spend)
    assert spend.spent == 0 and spend.reserved == pytest.approx(0, abs=1e-9)


def test_a_judge_timeout_after_upstream_work_counts_at_its_price(monkeypatch):
    # review of #530: only statuses known to come before any work are free; a 408 (OpenRouter's request timeout) may
    # follow upstream work, so it counts as billed
    import httpx
    s3 = _S3({f"dev/products/scraped/sentas/{A}.jpg": b"o", f"{GPU}{A}.main.jpg": b"m"})
    late = httpx.HTTPStatusError("408", request=httpx.Request("POST", "https://q"), response=httpx.Response(408))
    monkeypatch.setattr(sel.judge, "judge", lambda job: (_ for _ in ()).throw(late))
    rounds = [("v9", {"sentas": _round(GPU, finished=(A,))}), ("v9-clean", {"sentas": _round(CL)})]
    spend = sel.Spend(10)
    with pytest.raises(httpx.HTTPStatusError):
        sel.choose(s3, rounds, _listing(A), set(), spend)
    assert spend.spent == pytest.approx(C.JUDGE_PRICE_USD)


def test_only_the_statuses_known_to_come_before_any_work_are_free():
    import httpx
    def error(code):
        return httpx.HTTPStatusError(str(code), request=httpx.Request("POST", "https://q"), response=httpx.Response(code))
    assert [code for code in (400, 401, 402, 403, 404, 408, 409, 413, 422, 425, 429, 499, 500)
            if sel.refused(error(code))] == list(C.HOSTED_REFUSED_CODES)
    assert sel.refused(TimeoutError("no answer")) is None


def test_a_listing_the_owner_chose_reads_nothing_of_its_click(monkeypatch):
    monkeypatch.setattr(sel, "click_problem", lambda *a: pytest.fail("the click was read"))
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="nano2")
    _clean(s3, rounds, _listing(A, B), set(), sel.Spend(10), owner=("v9", A), click=_click(B))
    assert edits == []


def test_a_photo_that_cannot_be_prepared_for_an_edit_costs_nothing_and_is_loud(monkeypatch):
    # review of #540: an undecodable photo, a missing key or an unknown model failed inside the billed scope, so all
    # three models were burnt as paid and the photo could never be cleaned, even by a click
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="nano2")
    monkeypatch.setattr(sel.clean, "prepare", lambda name, encoded: (_ for _ in ()).throw(ValueError("undecodable")))
    spend = sel.Spend(10)
    with pytest.raises(ValueError):
        _clean(s3, rounds, _listing(A, B), set(), spend)
    assert edits == [] and spend.spent == 0 and spend.reserved == 0 and spend.uses == []
    assert not any("/attempts/" in k for k in s3.objects)


def test_the_real_preparation_fails_before_anything_is_sent(monkeypatch):
    import httpx
    monkeypatch.setenv("FAL_KEY", "key")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("sent"))
    with pytest.raises(Exception):
        _REAL_PREPARE("nano2", b"not an image")
    with pytest.raises(KeyError):
        _REAL_PREPARE("no-such-model", b"o")


def test_a_call_that_may_have_been_billed_still_counts_at_its_price(monkeypatch):
    import httpx
    s3, _, rounds = _clean_setup(monkeypatch, passes_at="nano2")
    broken = httpx.HTTPStatusError("502", request=httpx.Request("POST", "https://q"), response=httpx.Response(502))
    monkeypatch.setattr(sel.clean, "submit", lambda name: (_ for _ in ()).throw(broken))
    spend = sel.Spend(10)
    _clean(s3, rounds, _listing(A, B), set(), spend)
    assert spend.spent == pytest.approx(sum(C.CLEAN_PRICE_USD.values()))               # each model once, never again


# --- one click state (review 2026-11-06, A2.5) -----------------------------------------------------------------------
# A click's state was decided in three places: admitted as finished while its listing awaited an outline recheck (dev
# run 20261106082352439, MAR-P/24/04), "owner's click: clean" on the review list while it waited, and admitted every run
# with no model left.
def _state(s3, rounds, listing, click, owner=None):
    return sel.click_state(s3, rounds, listing, set(), click, sel.listing_state(s3, rounds, listing, set(), owner))


def _reflection_on_a(monkeypatch):
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="nano2")
    s3.objects[f"{GPU}judged/{A}.json"] = _verdict("fail", "light_reflection")
    return s3, edits, rounds


def test_a_click_whose_listing_awaits_an_outline_recheck_is_blocked_never_planned(monkeypatch):
    s3, _, rounds = _reflection_on_a(monkeypatch)
    gpu = rounds[0][1]["sentas"]
    gpu.slabs, gpu.checked = {A}, set()                                               # A: a whole slab, older outline
    state, why = _state(s3, rounds, _listing(A, B), _click(A))
    assert state == "blocked" and "not fully judged" in why
    assert sel.clean_plan(s3, rounds, _listing(A, B), set(), None, _click(A)) is None


def test_a_click_on_a_photo_not_judged_yet_is_blocked_as_pending_not_as_unfixable(monkeypatch):
    s3, _, rounds = _reflection_on_a(monkeypatch)
    rounds[0][1]["sentas"].judged.discard(A)
    state, why = _state(s3, rounds, _listing(A, B), _click(A))
    assert state == "blocked" and "not fully judged" in why


def test_a_click_whose_photo_has_no_model_left_is_blocked_and_never_sent(monkeypatch):
    s3, _, rounds = _reflection_on_a(monkeypatch)
    _all_models_failed(s3, rounds, A)
    assert _state(s3, rounds, _listing(A, B), _click(A)) == ("blocked", sel.NO_MODEL_LEFT)
    plan = sel.clean_plan(s3, rounds, _listing(A, B), set(), None, _click(A))
    assert plan is None or not plan.click


def test_a_click_s_state_is_one_answer_and_the_plan_sends_only_a_ready_one(monkeypatch):
    s3, _, rounds = _reflection_on_a(monkeypatch)
    assert _state(s3, rounds, _listing(A, B), _click(A)) == ("ready", None)
    assert sel.clean_plan(s3, rounds, _listing(A, B), set(), None, _click(A)).click
    assert _state(s3, rounds, _listing(A, B), _click(A), owner=("v9", B))[0] == "blocked"
    rounds[1][1]["sentas"].cleaned.add(A)
    assert _state(s3, rounds, _listing(A, B), _click(A)) == ("cleaned", None)
    assert _state(s3, rounds, _listing(A, B), None) is None


def test_a_click_whose_edits_are_paid_but_not_checked_still_leads_its_plan(monkeypatch):
    # review of #546: the clicked photo's paid edits were only collected, so the plan lost its click priority
    monkeypatch.setattr(C, "CLEAN_ENABLED", True)
    s3, _, rounds = _reflection_on_a(monkeypatch)
    s3.objects[f"{GPU}judged/{B}.json"] = _verdict("fail", "leftover_mark")
    cl = rounds[1][1]["sentas"]
    for name in sel.clean.MODELS:                                                        # A: paid, never checked
        s3.objects[f"{CL}attempts/{A}.{name}.request.json"] = json.dumps({"model": name}).encode()
        cl.requested.add(f"{A}.{name}")
    plan = sel.clean_plan(s3, rounds, _listing(A, B), set(), None, _click(A))
    assert plan.click and plan.collect[0][0] == A and not plan.sends_click           # B, a mark, is its send


def test_the_review_list_says_a_click_waits_and_why(monkeypatch):
    s3, _, rounds = _reflection_on_a(monkeypatch)
    s = _selection(s3, [_listing(A, B)], rounds)
    s.root, s.clicks = "r/", {"SEN-1": _click(A)}
    out = sel.report(s, {"slab-1": "month photo budget reached"})
    assert f"owner's click on {A[:12]}: waiting: month photo budget reached" in out["tracked"][0]["what_happened"]
    assert out["click_problems"] == []                                                # the run names the wait itself


def test_a_blocked_click_is_a_problem_of_the_run(monkeypatch):
    s3, _, rounds = _reflection_on_a(monkeypatch)
    _all_models_failed(s3, rounds, A)
    s = _selection(s3, [_listing(A, B)], rounds)
    s.root, s.clicks = "r/", {"SEN-1": _click(A)}
    assert sel.report(s, {})["click_problems"] == [f"slab-1: owner's click on {A[:12]}: blocked: {sel.NO_MODEL_LEFT}"]


# --- an unparsed verdict is asked again (review 2026-11-06, A2.6) -----------------------------------------------------
def _unparsed(asks):
    return json.dumps({"verdict": "unparsed", "raw": "I think", "version": C.JUDGE_VERSION, "asks": asks}).encode()


def test_an_unparsed_verdict_is_not_final_and_is_asked_again(monkeypatch):
    s3 = _S3({f"{GPU}judged/{A}.json": _unparsed(1), f"dev/products/scraped/sentas/{A}.jpg": b"o", f"{GPU}{A}.main.jpg": b"m"})
    rounds = [("v9", {"sentas": _round(GPU, finished=(A,), judged=(A,))}), ("v9-clean", {"sentas": _round(CL)})]
    assert sel.choose(s3, rounds, _listing(A), set()) == (None, False)                   # pending, not a final fail
    monkeypatch.setattr(sel.judge, "judge", lambda job: ({"verdict": "pass", "problems": [], "version": C.JUDGE_VERSION}, 0.02))
    assert sel.choose(s3, rounds, _listing(A), set(), sel.Spend(10)) == (("v9", A), True)


def test_an_answer_that_never_parses_is_a_named_fail_after_its_asks(monkeypatch):
    # review of #546: a permanently garbled answer was paid again every run and its listing stayed pending for ever
    s3 = _S3({f"{GPU}judged/{A}.json": _unparsed(C.JUDGE_UNPARSED_ASKS - 1),
              f"dev/products/scraped/sentas/{A}.jpg": b"o", f"{GPU}{A}.main.jpg": b"m"})
    asked = []
    monkeypatch.setattr(sel.judge, "judge", lambda o, r, f: asked.append(1) or (
        {"verdict": "unparsed", "raw": "?", "version": C.JUDGE_VERSION}, 0.02))
    rounds = [("v9", {"sentas": _round(GPU, finished=(A,), judged=(A,))}), ("v9-clean", {"sentas": _round(CL)})]
    assert sel.choose(s3, rounds, _listing(A), set(), sel.Spend(10)) == (None, True)      # final: on the review list
    assert asked == [1] and json.loads(s3.objects[f"{GPU}judged/{A}.json"])["asks"] == C.JUDGE_UNPARSED_ASKS
    rounds[0][1]["sentas"].asked.clear()                                                  # a later run
    assert sel.choose(s3, rounds, _listing(A), set(), sel.Spend(10)) == (None, True) and asked == [1]
    assert "the judge's answer could not be read" in sel.photo_notes(s3, rounds, _listing(A), set(), {})


def test_a_photo_is_asked_at_most_once_a_run(monkeypatch):
    s3 = _S3({f"{GPU}judged/{A}.json": _unparsed(0), f"dev/products/scraped/sentas/{A}.jpg": b"o", f"{GPU}{A}.main.jpg": b"m"})
    asked = []
    monkeypatch.setattr(sel.judge, "judge", lambda o, r, f: asked.append(1) or (
        {"verdict": "unparsed", "raw": "?", "version": C.JUDGE_VERSION}, 0.02))
    rounds = [("v9", {"sentas": _round(GPU, finished=(A,), judged=(A,))}), ("v9-clean", {"sentas": _round(CL)})]
    for listing in (_listing(A, key="one"), _listing(A, key="two")):                      # two listings of one stone
        sel.choose(s3, rounds, listing, set(), sel.Spend(10))
    assert asked == [1]


def test_a_selection_keeps_what_it_asked_across_reading_its_rounds_again(monkeypatch):
    first = [("v9", {"sentas": _round(GPU)}), ("v9-clean", {"sentas": _round(CL)})]
    again = [("v9", {"sentas": _round(GPU)}), ("v9-clean", {"sentas": _round(CL)})]
    monkeypatch.setattr(sel, "read_rounds", lambda client, tags, sources: again)
    s = sel.Selection(_S3(), [_listing(A)], "v9", "v9-clean", {}, {}, first, [], {}, set())
    s.rounds[0][1]["sentas"].asked.add(A)
    s.refresh()
    assert A in s.rounds[0][1]["sentas"].asked and A in s.only([], {}).rounds[0][1]["sentas"].asked


# --- the clicked photo is the unit (review 2026-11-06, A2.1) ---------------------------------------------------------
def test_a_clicked_photo_is_cleaned_though_another_photo_of_the_listing_has_a_cleaned_copy(monkeypatch):
    # a copy of another photo (an earlier mark) made the listing count as cleaned: the click was never paid
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="nano2")
    s3.objects[f"{GPU}judged/{A}.json"] = _verdict("fail", "light_reflection")
    rounds[1][1]["sentas"].cleaned.add(B)
    s3.objects[f"{CL}cleaned/{B}.jpg"] = b"older-copy"
    _clean(s3, rounds, _listing(A, B), set(), sel.Spend(10), click=_click(A))
    assert edits == ["nano2"] and s3.objects[f"{CL}cleaned/{A}.jpg"] == b"copy-nano2"


def test_a_clicked_photo_with_its_own_cleaned_copy_is_never_paid_again(monkeypatch):
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="nano2")
    s3.objects[f"{GPU}judged/{A}.json"] = _verdict("fail", "light_reflection")
    rounds[1][1]["sentas"].cleaned.add(A)
    _clean(s3, rounds, _listing(A, B), set(), sel.Spend(10), click=_click(A))
    assert edits == []


def test_without_a_click_any_cleaned_copy_of_the_listing_is_its_one_photo(monkeypatch):
    monkeypatch.setattr(C, "CLEAN_ENABLED", True)
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="nano2")                    # B: a mark
    rounds[1][1]["sentas"].cleaned.add(A)
    _clean(s3, rounds, _listing(A, B), set(), sel.Spend(10))
    assert edits == []



def test_a_fail_under_an_older_stricter_bar_is_judged_again_and_a_pass_stays_a_pass(monkeypatch):
    old_fail = json.dumps({"verdict": "fail", "problems": [{"kind": "leftover_mark"}], "version": 1}).encode()
    old_pass = json.dumps({"verdict": "pass", "problems": [], "version": 1}).encode()
    s3 = _S3({f"{GPU}judged/{A}.json": old_fail, f"{GPU}judged/{B}.json": old_pass,
              f"dev/products/scraped/sentas/{A}.jpg": b"o", f"{GPU}{A}.main.jpg": b"m"})
    asked = []
    monkeypatch.setattr(sel.judge, "judge", lambda job: asked.append(1) or ({"verdict": "pass", "problems": [], "version": C.JUDGE_VERSION}, 0.02))
    rounds = [("v9", {"sentas": _round(GPU, finished=(A, B), judged=(A, B))}), ("v9-clean", {"sentas": _round(CL)})]
    assert sel.choose(s3, rounds, _listing(B), set(), sel.Spend(10)) == (("v9", B), True) and asked == []   # pass stands
    assert sel.choose(s3, rounds, _listing(A), set(), sel.Spend(10)) == (("v9", A), True) and asked == [1]  # judged again
    assert json.loads(s3.objects[f"{GPU}judged/{A}.json"])["version"] == C.JUDGE_VERSION


def test_without_spend_a_fail_under_an_older_bar_is_not_final():
    old_fail = json.dumps({"verdict": "fail", "problems": [{"kind": "leftover_mark"}], "version": 1}).encode()
    rounds = [("v9", {"sentas": _round(GPU, finished=(A,), judged=(A,))}), ("v9-clean", {"sentas": _round(CL)})]
    assert sel.choose(_S3({f"{GPU}judged/{A}.json": old_fail}), rounds, _listing(A), set()) == (None, False)



def test_a_listing_with_an_outdated_fail_is_never_cleaned_before_it_is_judged_again(monkeypatch):
    monkeypatch.setattr(sel.clean, "submit", lambda *a: pytest.fail("cleaned on an outdated verdict"))
    old_fail = json.dumps({"verdict": "fail", "problems": [{"kind": "light_reflection"}], "version": 1}).encode()
    rounds = [("v9", {"sentas": _round(GPU, finished=(A,), judged=(A,))}), ("v9-clean", {"sentas": _round(CL)})]
    _clean(_S3({f"{GPU}judged/{A}.json": old_fail}), rounds, _listing(A), set(), sel.Spend(10))


def test_an_attempt_that_failed_under_an_older_instruction_is_made_again_and_a_pass_stands(monkeypatch):
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="nano2")
    s3.objects[f"{CL}attempts/{B}.nano2.json"] = json.dumps({"passes": False, "clean_version": 1}).encode()
    s3.objects[f"{CL}attempts/{B}.nano2.jpg"] = b"old-copy"
    rounds[1][1]["sentas"].attempts.add(f"{B}.nano2")
    rounds[1][1]["sentas"].edited.add(f"{B}.nano2")
    _clean(s3, rounds, _listing(A, B), set(), sel.Spend(10))
    assert edits == ["nano2"] and s3.objects[f"{CL}cleaned/{B}.jpg"] == b"copy-nano2"   # sent again, new copy used
    assert json.loads(s3.objects[f"{CL}attempts/{B}.nano2.json"])["clean_version"] == C.CLEAN_VERSION


# --- a whole slab before a close-up (a 3D slab for the listing) ---------------------------------------------------------
def _judged_in_turn(monkeypatch, answers):
    """The judge stood in: answers[sha] = "pass" | "fail"; returns the shas it was asked about, in order."""
    asked = []
    monkeypatch.setattr(sel, "_original", lambda client, src, sha: sha.encode())
    monkeypatch.setattr(sel.judge, "judge", lambda job: asked.append(job[0].decode()) or (
        {"verdict": answers[job[0].decode()], "problems": [], "version": C.JUDGE_VERSION}, 0.02))
    return asked


def _results(*shas):
    return {f"{GPU}{s}.main.jpg": b"m" for s in shas}


def test_a_close_up_pass_gives_way_to_the_first_whole_slab_the_judge_passes(monkeypatch):
    asked = _judged_in_turn(monkeypatch, {A: "pass", B: "fail", D: "pass"})
    rounds = [("v9", {"sentas": _round(GPU, finished=(A, B, D), slabs=(B, D))}), ("v9-clean", {"sentas": _round(CL)})]
    s3 = _S3(_results(A, B, D))
    assert sel.choose(s3, rounds, _listing(A, B, D), set(), sel.Spend(10)) == (("v9", D), True)
    assert asked == [A, B, D]


def test_after_a_close_up_pass_only_whole_results_are_judged(monkeypatch):
    asked = _judged_in_turn(monkeypatch, {A: "pass", B: "pass", D: "fail"})
    rounds = [("v9", {"sentas": _round(GPU, finished=(A, B, D), slabs=(D,))}), ("v9-clean", {"sentas": _round(CL)})]
    s3 = _S3(_results(A, B, D))
    assert sel.choose(s3, rounds, _listing(A, B, D), set(), sel.Spend(10)) == (("v9", A), True)
    assert asked == [A, D]                                        # B is another close-up: no better, never judged


def test_a_block_or_tile_takes_its_first_pass_as_before(monkeypatch):
    asked = _judged_in_turn(monkeypatch, {A: "pass", B: "pass"})
    rounds = [("v9", {"sentas": _round(GPU, finished=(A, B))}), ("v9-clean", {"sentas": _round(CL)})]
    s3 = _S3(_results(A, B))
    for fmt in ("block", "tile"):
        assert sel.choose(s3, rounds, _listing(A, B, fmt=fmt), set(), sel.Spend(10)) == (("v9", A), True)
    assert asked == [A]                                           # judged once, stored: B never


def test_a_kept_close_up_stays_when_no_whole_slab_passes_and_a_rerun_costs_nothing(monkeypatch):
    monkeypatch.setattr(sel.judge, "judge", lambda *a: pytest.fail("paid again"))
    s3 = _S3({f"{GPU}judged/{B}.json": _verdict("fail", "not_one_slab")})
    rounds = [("v9", {"sentas": _round(GPU, finished=(A, B), held=(D,), judged=(A, B), slabs=(B,))}),
              ("v9-clean", {"sentas": _round(CL)})]
    spend = sel.Spend(10)
    assert sel.choose(s3, rounds, _listing(A, B, D, kept=("v9", A)), set(), spend) == (("v9", A), True)
    assert spend.spent == 0 and spend.reserved == 0


def test_a_kept_close_up_is_never_given_up_for_a_whole_slab_the_judge_has_not_passed():
    rounds = [("v9", {"sentas": _round(GPU, finished=(A, B), judged=(A,), slabs=(B,))}), ("v9-clean", {"sentas": _round(CL)})]
    assert sel.choose(_S3(), rounds, _listing(A, B, D, kept=("v9", A)), set()) == (("v9", A), False)   # B unjudged, D no result


def test_a_kept_close_up_moves_to_a_whole_slab_that_passes_in_the_cleaned_run(monkeypatch):
    asked = _judged_in_turn(monkeypatch, {B: "pass"})
    monkeypatch.setattr(sel, "_get", lambda client, key: b"m")
    rounds = [("v9", {"sentas": _round(GPU, finished=(A,), judged=(A,))}),
              ("v9-clean", {"sentas": _round(CL, finished=(B,), cleaned=(B,), slabs=(B,))})]
    assert sel.choose(_S3(), rounds, _listing(A, B, kept=("v9", A)), set(), sel.Spend(10)) == (("v9-clean", B), True)
    assert asked == [B]


def test_a_listing_with_a_close_up_pass_is_never_sent_to_a_hosted_edit(monkeypatch):
    monkeypatch.setattr(sel.clean, "submit", lambda *a: pytest.fail("a listing with a good photo was cleaned"))
    s3 = _S3({f"{GPU}judged/{B}.json": _verdict("fail", "light_reflection")})          # fixable, but A already passed
    rounds = [("v9", {"sentas": _round(GPU, finished=(A, B), judged=(A, B), slabs=(B,))}), ("v9-clean", {"sentas": _round(CL)})]
    spend = sel.Spend(10)
    _clean(s3, rounds, _listing(A, B, kept=("v9", A)), set(), spend)
    assert spend.spent == 0


def _reported(objects, listing, rnd):
    s3 = _S3(objects)
    s = object.__new__(sel.Selection)
    s.client, s.exclude, s.root, s.listings, s.owner, s.conflicts, s.clicks = s3, set(), "r/", [listing], {}, {}, {}
    s.rounds = [("v9", {"sentas": rnd}), ("v9-clean", {"sentas": _round(CL)})]
    return s3, sel.report(s, {})


def test_the_report_switches_a_kept_close_up_to_the_whole_slab_that_passed():
    kept = json.dumps({"source": "sentas", "key": "slab-1", "sha": A, "tag": "v9", "prefix": GPU}).encode()
    s3, result = _reported({"r/chosen/slab-1.json": kept, f"{GPU}judged/{B}.json": _verdict("pass")},
                           _listing(A, B, kept=("v9", A)), _round(GPU, finished=(A, B), judged=(A, B), slabs=(B,)))
    assert json.loads(s3.objects["r/chosen/slab-1.json"])["sha"] == B and [c["sha"] for c in result["chosen"]] == [B]


def test_a_kept_close_up_with_nothing_better_is_still_chosen_and_its_record_not_written_again():
    kept = b'{"as": "written before"}'
    s3, result = _reported({"r/chosen/slab-1.json": kept, f"{GPU}judged/{B}.json": _verdict("fail", "not_one_slab")},
                           _listing(A, B, kept=("v9", A)), _round(GPU, finished=(A, B), judged=(A, B), slabs=(B,)))
    assert s3.objects["r/chosen/slab-1.json"] == kept and [c["sha"] for c in result["chosen"]] == [A]
    assert result["tracked"] == [] and result["pending"] == []


# --- open books made before their halves had a slab -------------------------------------------------------------------
class _Written(_Bucket):
    def __init__(self, objects):
        super().__init__(objects)
        self.written = []

    def put_object(self, Bucket, Key, Body, ContentType):
        self.written.append(Key)
        self.objects[Key] = Body


def _open_book(*extra):
    return {f"{GPU}{A}.{v}.jpg": b"" for v in ("main", "white", "studio")} | {k: b"" for k in extra}


def test_an_open_book_without_its_slab_is_not_finished_until_completed_or_tried():
    from deploy import standardise_source as src
    assert not src.finished(_Bucket(_open_book()), GPU, A)
    assert not src.finished(_Bucket(_open_book(f"{GPU}{A}.slab.png")), GPU, A)          # completed: its outline checked once
    assert src.finished(_Bucket(_open_book(f"{GPU}{A}.slab.png", src.checked_key(GPU, A))), GPU, A)
    assert src.finished(_Bucket(_open_book(f"{GPU}noslab/{A}.json")), GPU, A)
    assert src.finished(_Bucket(_open_book(src.logo_step_key(GPU, A))), GPU, A)    # made by the retired logo step
    assert src.finished(_Bucket({f"{GPU}{A}.main.jpg": b"", f"{GPU}{A}.white.jpg": b""}), GPU, A)    # a close-up
    assert src.finished(_Bucket({f"{GPU}held/{A}.json": b""}), GPU, A)
    assert not src.finished(_Bucket({}), GPU, A)


def _views(img, *names):
    return {name: img for name in names}


def _book_result(changed, views=("main", "white", "studio", "close")):
    import numpy as np
    from stone_pipeline.standardise.photo import Result
    img = np.zeros((20, 30, 3), np.uint8)
    return Result(kind="open book", images=_views(img, *views), slab=(img, np.ones((20, 30), np.float32)), chosen=(1, 2),
                  changed=changed)


def test_completing_an_open_book_drawn_the_same_adds_its_close_view_then_its_slab_then_the_note():
    from deploy import standardise_source as src
    s3 = _Written(_open_book())
    assert src.rework(s3, GPU, A, _book_result(changed=False), "complete") == "completed"
    # what was judged stays; the slab last: the result is complete; which half it is, beside it
    assert s3.written == [f"{GPU}{A}.close.jpg", src.slab_record_key(GPU, A), f"{GPU}{A}.slab.png", src.checked_key(GPU, A)]
    assert json.loads(s3.objects[src.slab_record_key(GPU, A)]) == {"index": 1, "of": 2}
    assert json.loads(s3.objects[src.checked_key(GPU, A)]) == {"changed": False} and src.finished(s3, GPU, A)


def test_completing_an_open_book_this_version_draws_differently_draws_a_candidate_for_the_judge():
    from deploy import standardise_source as src
    s3 = _Written(_open_book())
    assert src.rework(s3, GPU, A, _book_result(changed=True), "complete") == "redrawn for the judge"
    assert all(k.startswith(src.candidate_prefix(GPU)) for k in s3.written)          # the result itself untouched
    assert s3.written[-1] == src.candidate_key(GPU, A) and src.finished(s3, GPU, A)  # complete: not run again


def test_a_new_whole_slab_result_records_which_slab_it_is_before_its_slab_and_its_views(monkeypatch):
    import numpy as np
    from stone_pipeline.standardise.photo import Result
    s3 = _Slice({})
    img = np.zeros((20, 30, 3), np.uint8)
    book = lambda *a, **k: Result(kind="open book", images={"main": img, "white": img}, slab=(img, np.ones((20, 30), np.float32)),
                                  chosen=(0, 2))
    assert _completing(monkeypatch, s3, book) == 0
    assert [k for k in s3.written if A in k] == [f"{GPU}{A}.slab.json", f"{GPU}{A}.slab.png", f"{GPU}{A}.white.jpg", f"{GPU}{A}.main.jpg",
                                                 f"{GPU}{C.OUTLINE_CHECKED}{A}.json"]
    assert json.loads(s3.objects[f"{GPU}{C.OUTLINE_CHECKED}{A}.json"]) == {"new": True}
    assert json.loads(s3.objects[f"{GPU}{A}.slab.json"]) == {"index": 0, "of": 2}


@pytest.mark.parametrize("res", ["close-up", "held"])
def test_an_open_book_that_is_no_open_book_on_its_rerun_is_marked_tried_and_never_rerun(res):
    import numpy as np
    from deploy import standardise_source as src
    from stone_pipeline.standardise.photo import Result
    s3 = _Written(_open_book())
    img = np.zeros((20, 30, 3), np.uint8)
    result = Result(kind="close-up", images={"main": img, "white": img}) if res == "close-up" else Result(held="no outline")
    assert src.rework(s3, GPU, A, result, "complete") == "no slab"
    assert s3.written == [f"{GPU}noslab/{A}.json"] and src.finished(s3, GPU, A)


# --- a whole slab made under the older outline rule (the wall in a rounded corner) --------------------------------------
def _whole(*extra):
    return {f"{GPU}{A}.{v}": b"old" for v in ("main.jpg", "white.jpg", "studio.jpg", "close.jpg", "slab.png")} | {k: b"" for k in extra}


def _slab_result(*views):
    import numpy as np
    from stone_pipeline.standardise.photo import Result
    img = np.zeros((20, 30, 3), np.uint8)
    return Result(kind="slab", images=_views(img, *views), slab=(img, np.ones((20, 30), np.float32)), chosen=(0, 1),
                  changed=True)


def test_a_whole_slab_made_under_the_older_outline_rule_is_checked_once():
    from deploy import standardise_source as src
    assert not src.finished(_Bucket(_whole()), GPU, A) and src.again(_Bucket(_whole()), GPU, A) == "recheck"
    assert src.finished(_Bucket(_whole(src.checked_key(GPU, A))), GPU, A)
    assert src.finished(_Bucket(_whole(src.candidate_key(GPU, A))), GPU, A)           # its redraw waits for the judge
    assert not src.finished(_Bucket(_whole(f"{GPU}redraw/{A}.main.jpg")), GPU, A)      # a candidate cut short: again
    assert src.again(_Bucket(_open_book()), GPU, A) == "complete" and src.again(_Bucket({}), GPU, A) is None


def test_a_whole_slab_the_retired_logo_step_made_is_never_rechecked():
    """A re-run would start from the photo with its logo and redraw views no judge saw (logo_step_key)."""
    from deploy import standardise_source as src
    assert src.finished(_Bucket(_whole(src.logo_step_key(GPU, A))), GPU, A)


def test_a_recheck_drawn_the_same_only_notes_it():
    from deploy import standardise_source as src
    from stone_pipeline.standardise.photo import Result
    s3 = _Written(_whole())
    assert src.rework(s3, GPU, A, Result(kind="slab", unchanged=True), "recheck") == "outline unchanged"
    assert s3.written == [src.checked_key(GPU, A)] and json.loads(s3.objects[src.checked_key(GPU, A)]) == {"changed": False}
    assert src.finished(s3, GPU, A)


def test_a_recheck_drawn_differently_draws_a_complete_candidate_and_never_touches_the_result():
    """A separate set under redraw/, named as a result is, which slab it is and the slab first, main last, its
    completion marker last of all; the stored result, its verdict and what is published of it stay as they are."""
    from deploy import standardise_source as src
    s3 = _Slice(_whole(f"{GPU}judged/{A}.json"))
    before = dict(s3.objects)
    assert src.rework(s3, GPU, A, _slab_result("main", "white", "studio", "close"), "recheck") == "redrawn for the judge"
    cand = src.candidate_prefix(GPU)
    assert s3.written == [f"{cand}{A}.slab.json", f"{cand}{A}.slab.png", f"{cand}{A}.white.jpg", f"{cand}{A}.studio.jpg",
                          f"{cand}{A}.close.jpg", f"{cand}{A}.main.jpg", src.candidate_key(GPU, A)]
    assert {k: v for k, v in s3.objects.items() if k in before} == before and src.finished(s3, GPU, A)


def test_every_file_is_encoded_before_the_first_write(monkeypatch):
    from deploy import standardise_source as src
    s3 = _Slice(_whole())
    res = _slab_result("main", "white")
    real = src._encode
    monkeypatch.setattr(src, "_encode", lambda im, quality=92: real(im) if im is not res.images["white"] else (_ for _ in ()).throw(ValueError("could not encode")))
    with pytest.raises(ValueError):
        src.rework(s3, GPU, A, res, "recheck")
    assert s3.written == [] and all(s3.objects[f"{GPU}{A}.{v}"] == b"old" for v in ("main.jpg", "white.jpg", "slab.png"))
    assert not src.finished(s3, GPU, A)                                     # rechecked again


@pytest.mark.parametrize("res", ["held", "close-up"])
def test_a_recheck_that_gives_no_whole_slab_keeps_the_stored_result_and_notes_it_checked(res):
    import numpy as np
    from deploy import standardise_source as src
    from stone_pipeline.standardise.photo import Result
    s3 = _Written(_whole())
    img = np.zeros((20, 30, 3), np.uint8)
    result = Result(held="two stones in one outline") if res == "held" else Result(kind="close-up", images={"main": img})
    assert src.rework(s3, GPU, A, result, "recheck") == "no slab"
    assert s3.written == [src.checked_key(GPU, A)] and json.loads(s3.objects[src.checked_key(GPU, A)])["kept"]
    assert src.finished(s3, GPU, A) and f"{GPU}noslab/{A}.json" not in s3.objects   # never noslab/ over a live result
    assert all(s3.objects[f"{GPU}{A}.{v}"] == b"old" for v in ("main.jpg", "white.jpg", "slab.png"))


def test_a_new_whole_slab_is_noted_as_drawn_under_the_current_outline_rule_and_a_close_up_is_not():
    import numpy as np
    from deploy import standardise_source as src
    from stone_pipeline.standardise.photo import Result
    img = np.zeros((20, 30, 3), np.uint8)
    whole, close = _Written({}), _Written({})
    src.write(whole, GPU, A, Result(kind="slab", images={"main": img, "white": img}, slab=(img, np.ones((20, 30), np.float32)),
                                    chosen=(0, 1)))
    src.write(close, GPU, A, Result(kind="close-up", images={"main": img, "white": img}))
    assert whole.written == [src.slab_record_key(GPU, A), f"{GPU}{A}.slab.png", f"{GPU}{A}.white.jpg", f"{GPU}{A}.main.jpg",
                             src.checked_key(GPU, A)]
    assert json.loads(whole.objects[src.checked_key(GPU, A)]) == {"new": True}
    assert close.written == [f"{GPU}{A}.white.jpg", f"{GPU}{A}.main.jpg"] and src.finished(close, GPU, A)


# --- review D1: a failing completion never holds a judged, published result ---------------------------------------------
class _Slice(_Written):
    """A bucket in a dict as the GPU job sees it: list, get, put, delete; put_object fails for the keys in `failing`."""

    def __init__(self, objects, failing=None):
        super().__init__(objects)
        self.failing = failing or {}

    def put_object(self, Bucket, Key, Body, ContentType):
        if Key in self.failing:
            raise self.failing[Key]
        super().put_object(Bucket, Key, Body, ContentType)

    def delete_object(self, Bucket, Key):
        self.written.append(f"delete {Key}")
        self.objects.pop(Key, None)


def _completing(monkeypatch, s3, standardise):
    """main() over one old open book (A) of slice 0 of sentas, with the models and the photo steps stood in."""
    import numpy as np
    from deploy import standardise_source as src
    monkeypatch.setenv("SRC", "sentas")
    monkeypatch.setenv("RUN_TAG", "v9")
    monkeypatch.setattr(src.boto3, "client", lambda *a, **k: s3)
    monkeypatch.setattr(src, "sheet_photos", lambda client: {A: {"source": "sentas", "key": "k", "format": "slab", "size": None}})
    monkeypatch.setattr(src, "_photo_keys", lambda client, s, photos: [f"dev/products/scraped/sentas/{A}.jpg"])
    monkeypatch.setattr(src.models, "load", lambda need: None)
    monkeypatch.setattr(src, "_get", lambda client, key: b"jpg")
    monkeypatch.setattr(src.photo, "read", lambda data: np.zeros((20, 30, 3), np.uint8))
    monkeypatch.setattr(src.photo, "standardise", standardise)
    return src.main()


def _tried(stored):
    """Where a redo that gave nothing is noted: an open book's completion in noslab/ (main), a recheck in its note, kept
    as it is (never noslab/ over a live result)."""
    from deploy import standardise_source as src
    return (f"{GPU}noslab/{A}.json", "reason") if stored == "open book" else (src.checked_key(GPU, A), "kept")


@pytest.mark.parametrize("stored", ["open book", "older outline"])
def test_a_redo_that_crashes_is_noted_tried_never_held_over_the_published_result(monkeypatch, stored):
    from deploy import standardise_source as src
    s3 = _Slice(_open_book() if stored == "open book" else _whole())

    def crash(*a, **k):
        raise ValueError("the outline went wrong")
    assert _completing(monkeypatch, s3, crash) == 0
    assert f"{GPU}held/{A}.json" not in s3.objects                                   # the judged result stays usable
    key, field = _tried(stored)
    assert "the outline went wrong" in json.loads(s3.objects[key])[field]
    assert src.finished(s3, GPU, A)                                                  # never run again
    assert all(f"{GPU}{A}.{v}.jpg" in s3.objects for v in ("main", "white", "studio"))


@pytest.mark.parametrize("stored", ["open book", "older outline"])
def test_a_redo_that_broke_the_gpu_twice_is_noted_and_never_run_again(monkeypatch, stored):
    from deploy import standardise_source as src
    s3 = _Slice(_open_book(f"{GPU}crashed/{A}.json") if stored == "open book" else _whole(f"{GPU}crashed/{A}.json"))

    def crash(*a, **k):
        raise RuntimeError("CUDA error: an illegal memory access")
    assert _completing(monkeypatch, s3, crash) == 0
    key, field = _tried(stored)
    assert "CUDA" in json.loads(s3.objects[key])[field] and f"{GPU}held/{A}.json" not in s3.objects
    assert src.finished(s3, GPU, A)


def test_a_completion_cut_short_by_a_passing_storage_error_leaves_no_half_and_is_retried(monkeypatch):
    import numpy as np
    from botocore.exceptions import ClientError
    from stone_pipeline.standardise.photo import Result
    slow = ClientError({"Error": {"Code": "SlowDown"}}, "PutObject")
    s3 = _Slice(_open_book(), failing={f"{GPU}{A}.slab.png": slow})
    img = np.zeros((20, 30, 3), np.uint8)
    half = (img, np.ones((20, 30), np.float32))
    book = lambda *a, **k: Result(kind="open book", images={"main": img, "white": img, "studio": img, "close": img}, slab=half,
                                  chosen=(0, 2))
    assert _completing(monkeypatch, s3, book) == 0
    assert f"{GPU}{A}.close.jpg" not in s3.objects                                   # never published half done
    assert f"{GPU}noslab/{A}.json" not in s3.objects and f"{GPU}held/{A}.json" not in s3.objects   # the next run retries


def test_a_completion_cut_short_removes_its_close_view_only_while_the_slab_is_missing():
    from deploy import standardise_source as src
    s3 = _Slice(_open_book(), failing={f"{GPU}{A}.slab.png": TimeoutError("lost")})
    with pytest.raises(TimeoutError):
        src.rework(s3, GPU, A, _book_result(changed=False, views=("close",)), "complete")
    assert s3.written == [f"{GPU}{A}.close.jpg", f"{GPU}{A}.slab.json", f"delete {GPU}{A}.close.jpg"]


# --- the owner's choice (deploy.standardise_decisions) and shared photos -------------------------------------------
def _selection(s3, listings, rounds, owner=None, conflicts=None, exclude=()):
    s = object.__new__(sel.Selection)
    s.client, s.listings, s.exclude, s.sources = s3, listings, set(exclude), ["sentas"]
    s.owner, s.conflicts, s.rounds, s.tags, s.chosen = owner or {}, conflicts or {}, rounds, ("v9", "v9-clean"), []
    s.clicks = {}
    return s


def _owner_record(sha, tag="v9", at="2026-11-02T10:00:00Z"):
    return {"tag": tag, "sha": sha, "at": at, "listing": "slab-1", "decided_on": "slab-1"}


def test_the_owner_s_choice_is_the_listing_s_choice_and_nothing_of_it_is_judged(monkeypatch):
    s3 = _S3({f"{GPU}judged/{A}.json": _verdict("fail", "light_reflection"), f"{GPU}judged/{B}.json": _verdict("fail", "bad_fill")})
    monkeypatch.setattr(sel.judge, "judge", lambda *a: pytest.fail("judged a listing the owner decided"))
    rounds = [("v9", {"sentas": _round(GPU, finished=(A, B, D), judged=(A, B))}), ("v9-clean", {"sentas": _round(CL)})]
    s = _selection(s3, [_listing(D, A, B)], rounds, owner={"SEN-1": _owner_record(B)})
    pick = s.owner_pick(s.listings[0])
    assert pick == ("v9", B) and sel.choose(s3, rounds, s.listings[0], set(), sel.Spend(10), owner=pick) == (("v9", B), True)
    assert s3.objects[f"{GPU}judged/{B}.json"] == _verdict("fail", "bad_fill")         # the judge's verdict untouched


def test_the_owner_s_record_is_checked_every_run():
    rounds = [("v9", {"sentas": _round(GPU, finished=(A, B))}), ("v9-clean", {"sentas": _round(CL)})]
    s = _selection(_S3(), [], rounds, owner={"SEN-1": _owner_record(B)}, exclude={A})
    assert s.owner_pick(_listing(A, B)) == ("v9", B)
    assert s.owner_pick({**_listing(A, B), "key": "rebound-handle"}) == ("v9", B)        # a re-bind keeps the SKU
    assert s.owner_pick(_listing(A)) is None                                             # no longer the listing's photo
    s.exclude = {B}
    assert s.owner_pick(_listing(A, B)) is None                                          # excluded after review
    s.exclude, s.owner = set(), {"SEN-1": _owner_record(D)}
    assert s.owner_pick(_listing(A, D)) is None                                          # its result is gone
    s.owner = {"SEN-1": _owner_record(A, tag="v1")}
    assert s.owner_pick(_listing(A)) is None                                             # not one of the process's tags


def test_only_the_owner_s_own_decision_on_a_listing_assigns_it_a_conflict_photo():
    # a choice that reached the listing through a sibling of its stone (decided_on another listing) never assigns it a
    # photo listings of another stone show: the stones may have changed since (a re-bind)
    rounds = [("v9", {"sentas": _round(GPU, finished=(A,))}), ("v9-clean", {"sentas": _round(CL)})]
    conflicts = {("sentas", A): ("slab-1", "other-stone")}
    reached = {**_owner_record(A), "decided_on": "block-sibling"}
    assert sel.owner_choice(reached, _listing(A), set(), conflicts, rounds) is None
    assert sel.owner_choice(reached, _listing(A), set(), {}, rounds) == ("v9", A)        # no conflict: it stands
    assert sel.owner_choice(_owner_record(A), _listing(A), set(), conflicts, rounds) == ("v9", A)


def test_a_listing_the_owner_decided_gets_no_gpu_work_and_no_storage_read():
    class _NoS3:
        def __getattr__(self, name):
            pytest.fail(f"read storage ({name}) to plan the GPU work")
    photos = [c * 64 for c in "1234"]
    rounds = [("v9", {"sentas": _round(GPU, finished=(photos[1],))}), ("v9-clean", {"sentas": _round(CL)})]
    s = _selection(_NoS3(), [_listing(*photos[:3], key="decided"), {**_listing(photos[3], key="open"), "sku": "SEN-2"}],
                   rounds, owner={"SEN-1": _owner_record(photos[1])})
    assert s.gpu_todo() == {"sentas": [photos[3]]}


def test_a_conflict_photo_is_never_chosen_by_itself_for_any_listing_that_shows_it(monkeypatch):
    s3 = _S3({f"{GPU}judged/{A}.json": _verdict("pass"), f"{GPU}judged/{B}.json": _verdict("pass")})
    rounds = [("v9", {"sentas": _round(GPU, finished=(A, B), judged=(A, B))}), ("v9-clean", {"sentas": _round(CL)})]
    s = _selection(s3, [_listing(A, B)], rounds, conflicts={("sentas", A): ("slab-1", "other-stone")})
    assert s.blocked(s.listings[0]) == {A}
    assert sel.choose(s3, rounds, s.listings[0], s.blocked(s.listings[0])) == (("v9", B), True)
    s.owner = {"SEN-1": _owner_record(A)}                                                # the owner may still assign it
    assert s.owner_pick(s.listings[0]) == ("v9", A)


def test_the_review_list_says_which_photo_is_held_for_a_conflict_and_with_whom():
    s3 = _S3({f"{GPU}judged/{B}.json": _verdict("fail", "other")})
    rounds = [("v9", {"sentas": _round(GPU, finished=(A, B), judged=(B,))}), ("v9-clean", {"sentas": _round(CL)})]
    s = _selection(s3, [_listing(A, B)], rounds, conflicts={("sentas", A): ("other-stone", "slab-1")})
    s.root = "r/"
    tracked = sel.report(s, {})["tracked"]
    assert [t["listing"] for t in tracked] == ["slab-1"]
    assert f"{A[:12]}: photo_conflict: also shown by other-stone" in tracked[0]["what_happened"]


def test_a_listing_the_owner_decided_is_never_cleaned(monkeypatch):
    monkeypatch.setattr(sel.clean, "submit", lambda *a: pytest.fail("cleaned a listing the owner decided"))
    s3, edits, rounds = _clean_setup(monkeypatch, passes_at="nano2")
    _clean(s3, rounds, _listing(A, B), set(), sel.Spend(10), owner=("v9", A))
