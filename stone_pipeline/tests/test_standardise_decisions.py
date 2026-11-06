"""The owner's decisions on the hidden listings (deploy.standardise_decisions), with an S3 stand-in: a "use" becomes the
owner's choice record of the listing (by SKU), extended to the listings of the same stone that show the photo and never
to a listing of another stone; a "remove" becomes a removal record by SKU; a "clean" becomes the owner's click record by
SKU, written and read only by publish.write_click and read_clicks. The judge's verdicts are never touched. Every
decision is checked against the product sheet, the stored results and the stored decisions before anything is written;
a bad or out-of-date one refuses the whole file. Applying twice changes nothing, a dry run writes nothing, nothing is
applied while a photos run holds the lock, and nothing runs without an explicit environment."""
from __future__ import annotations

import io
import json
from contextlib import contextmanager

import pytest
from botocore.exceptions import ClientError

from deploy import standardise_decisions as dec
from deploy import standardise_publish as pub
from deploy import standardise_select as select
from stone_pipeline.config.settings import ENV_SEGMENT, STANDARDISE as C
from stone_pipeline.io import imagestore

A, B, D, E, F = "a" * 64, "b" * 64, "d" * 64, "e" * 64, "f" * 64
RUN = f"{ENV_SEGMENT}/standardise-test/{C.RUN_TAG}/sentas/"
CLN = f"{ENV_SEGMENT}/standardise-test/{C.CLEAN_TAG}/sentas/"
ROOT = f"{ENV_SEGMENT}/standardise-test/{C.CLEAN_TAG}/"
CHOSEN = f"{ROOT}chosen/"
AT, LATER = "2026-11-02T10:00:00.000Z", "2026-11-03T08:00:00.000Z"


class _S3:
    """A bucket in a dict, every write recorded."""

    def __init__(self, objects):
        self.objects, self.written = dict(objects), []

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.written.append(Key)
        self.objects[Key] = Body

    def delete_object(self, Bucket, Key):
        self.written.append(Key)
        self.objects.pop(Key, None)

    def get_paginator(self, name):
        objects = self.objects

        class _Pages:
            def paginate(self, Bucket, Prefix):
                return [{"Contents": [{"Key": k, "ETag": "e"} for k in sorted(objects) if k.startswith(Prefix)]}]
        return _Pages()


def _verdict(v, *kinds):
    return json.dumps({"verdict": v, "problems": [{"kind": k} for k in kinds], "version": C.JUDGE_VERSION}).encode()


def _listing(key, *photos, stone=("v1", "f1")):
    return {"key": key, "sku": f"SEN-{key.upper()}", "source": "sentas", "stone": stone, "format": "slab",
            "photos": list(photos)}


# one, two and three are three stones; block-1/block-2 are slabs of one block (same stone) sharing F; leathered and
# polished are two finishes of one variety sharing E
LISTINGS = [_listing("one", A, B, stone=("v1", "f1")), _listing("two", D, stone=("v2", "f1")),
            _listing("three", "c" * 64, stone=("v3", "f1")),
            _listing("block-1", F, stone=("v4", "f1")), _listing("block-2", F, stone=("v4", "f1")),
            _listing("leathered", E, stone=("v5", "lea")), _listing("polished", E, stone=("v5", "pol"))]


def _bucket(conflicts=None):
    """Results for A, B, D, E, F under the run tag (all failed the judge), D's cleaned copy too; three has none. The
    produce's stored conflict set: E (leathered and polished), unless `conflicts` says otherwise."""
    conflicts = {E: ["leathered", "polished"]} if conflicts is None else conflicts
    return _S3({**{f"{RUN}{s}.main.jpg": b"m" for s in (A, B, D, E, F)},
                imagestore.conflicts_key("sentas"): json.dumps(conflicts).encode(),
                **{f"{RUN}judged/{s}.json": _verdict("fail", "light_reflection") for s in (A, B, D, E, F)},
                f"{CLN}cleaned/{D}.jpg": b"c", f"{CLN}{D}.main.jpg": b"m", f"{CLN}judged/{D}.json": _verdict("fail", "bad_fill")})


def _use(key, sha, tag=C.RUN_TAG, at=AT, **extra):
    return {"id": key, "action": "use", "source": "sentas", "sha": sha, "tag": tag, "at": at, **extra}


def _remove(key, at=AT, **extra):
    return {"id": key, "action": "remove", "source": "sentas", "at": at, **extra}


def _clean(key, sha, at=AT, **extra):
    return {"id": key, "action": "clean", "source": "sentas", "sha": sha, "at": at, **extra}


def _apply(s3, decisions, listings=LISTINGS):
    dec.apply(s3, dec.plan(s3, decisions, listings, exclude=set()))


def _owner(s3, sku):
    key = pub.owner_key(ROOT, sku)
    return json.loads(s3.objects[key]) if key in s3.objects else None


def _click(s3, sku):
    return pub.read_clicks(s3, ROOT).get(sku)


def _judged(s3):
    return {k: v for k, v in s3.objects.items() if "/judged/" in k}


# --- what a decision becomes ------------------------------------------------------------------------------------------
def test_a_use_is_the_listing_s_owner_record_by_sku_and_the_judge_s_verdicts_are_untouched():
    s3 = _bucket()
    judged = _judged(s3)
    _apply(s3, [_use("one", B), _use("two", D, tag=C.CLEAN_TAG)])
    assert _owner(s3, "SEN-ONE") == {"tag": C.RUN_TAG, "sha": B, "at": AT, "listing": "one", "decided_on": "one"}
    assert _owner(s3, "SEN-TWO")["tag"] == C.CLEAN_TAG and _judged(s3) == judged


def test_the_owner_s_later_decision_replaces_the_earlier_one():
    s3 = _bucket()
    _apply(s3, [_use("one", A)])
    _apply(s3, [_use("one", B, at=LATER)])
    assert _owner(s3, "SEN-ONE")["sha"] == B
    rounds = [(tag, {"sentas": select.Round(s3, tag, "sentas")}) for tag in (C.RUN_TAG, C.CLEAN_TAG)]
    s = object.__new__(select.Selection)
    s.exclude, s.owner, s.rounds, s.conflicts = set(), {"SEN-ONE": _owner(s3, "SEN-ONE")}, rounds, {}
    assert s.owner_pick(LISTINGS[0]) == (C.RUN_TAG, B)


def test_a_removal_is_recorded_by_sku_and_replaces_the_owner_s_choice_and_the_listing_s_choice():
    s3 = _bucket()
    s3.objects[f"{CHOSEN}one.json"] = json.dumps({"key": "one", "sha": A, "tag": C.RUN_TAG}).encode()
    _apply(s3, [_use("one", A)])
    _apply(s3, [_remove("one", at=LATER)])
    assert json.loads(s3.objects[imagestore.owner_removed_key("SEN-ONE")]) == {
        "sku": "SEN-ONE", "listing": "one", "source": "sentas", "reason": C.REMOVED_REASON, "at": LATER}
    assert _owner(s3, "SEN-ONE") is None and f"{CHOSEN}one.json" not in s3.objects


def test_a_later_use_lifts_a_removal():
    s3 = _bucket()
    _apply(s3, [_remove("one")])
    _apply(s3, [_use("one", B, at=LATER)])
    assert imagestore.owner_removed_key("SEN-ONE") not in s3.objects and _owner(s3, "SEN-ONE")["sha"] == B


def test_a_clean_is_the_listing_s_click_record_by_sku_the_one_the_photos_run_reads():
    s3 = _bucket()
    judged = _judged(s3)
    _apply(s3, [_clean("one", B)])
    record = {"sku": "SEN-ONE", "listing": "one", "source": "sentas", "sha": B, "at": AT}
    assert pub.read_clicks(s3, ROOT) == {"SEN-ONE": record} and _judged(s3) == judged
    assert select.click_problem(s3, select.Round(s3, C.RUN_TAG, "sentas"), LISTINGS[0], record, set()) is None
    assert select.authorised({"kinds": ["light_reflection"]}, B, record) == "click"      # the run's own gate takes it


def test_a_clean_is_stored_only_through_the_one_click_writer(monkeypatch):
    s3, written = _bucket(), []
    monkeypatch.setattr(dec, "write_click", lambda client, root, **record: written.append((client, root, record)))
    _apply(s3, [_clean("one", B)])
    assert written == [(s3, ROOT, {"sku": "SEN-ONE", "listing": "one", "source": "sentas", "sha": B, "at": AT})]


def test_a_decision_on_a_listing_whose_stored_click_cannot_be_read_is_refused():
    # it has no time to order the new decision against: an operator looks at the file first
    s3 = _bucket()
    _apply(s3, [_clean("one", B)])
    s3.objects[next(k for k in s3.objects if "/clicks/" in k)] = b"not json"
    before, s3.written = dict(s3.objects), []
    with pytest.raises(dec.InvalidDecisions, match="its stored click cannot be read"):
        dec.plan(s3, [_use("one", A, at=LATER)], LISTINGS, exclude=set())
    assert s3.objects == before


def test_a_clean_lifts_the_owner_s_choice_and_the_choice_record_made_from_it():
    # else the owner's earlier photo stays the listing's valid choice and the run never reaches the click
    s3 = _bucket()
    _apply(s3, [_use("one", A)])
    s3.objects[f"{CHOSEN}one.json"] = json.dumps({"key": "one", "sha": A, "tag": C.RUN_TAG}).encode()
    _apply(s3, [_clean("one", B, at=LATER)])
    assert _owner(s3, "SEN-ONE") is None and f"{CHOSEN}one.json" not in s3.objects and _click(s3, "SEN-ONE")["sha"] == B


def test_a_clean_lifts_a_removal():
    s3 = _bucket()
    _apply(s3, [_remove("one")])
    _apply(s3, [_clean("one", B, at=LATER)])
    assert imagestore.owner_removed_key("SEN-ONE") not in s3.objects and _click(s3, "SEN-ONE")["sha"] == B


@pytest.mark.parametrize("later", [_use("one", A, at=LATER), _remove("one", at=LATER)])
def test_a_later_use_or_removal_withdraws_the_click(later):
    s3 = _bucket()
    _apply(s3, [_clean("one", B)])
    _apply(s3, [later])
    assert _click(s3, "SEN-ONE") is None


def test_a_later_clean_replaces_the_earlier_one():
    s3 = _bucket()
    _apply(s3, [_clean("one", A)])
    _apply(s3, [_clean("one", B, at=LATER)])
    assert _click(s3, "SEN-ONE") == {"sku": "SEN-ONE", "listing": "one", "source": "sentas", "sha": B, "at": LATER}


@pytest.mark.parametrize("stored, older", [(_clean("one", B, at=LATER), _use("one", A)),
                                          (_use("one", A, at=LATER), _clean("one", B)),
                                          (_clean("one", B, at=LATER), _remove("one")),
                                          (_remove("one", at=LATER), _clean("one", B))])
def test_a_decision_older_than_a_stored_clean_or_than_what_a_clean_replaces_is_refused(stored, older):
    s3 = _bucket()
    _apply(s3, [stored])
    before, s3.written = dict(s3.objects), []
    with pytest.raises(dec.InvalidDecisions, match="older than the decision already stored"):
        dec.plan(s3, [older], LISTINGS, exclude=set())
    assert s3.objects == before


def test_a_use_never_reaches_a_sibling_the_owner_asked_to_clean():
    s3 = _bucket()
    _apply(s3, [_clean("block-2", F)])
    p = dec.plan(s3, [_use("block-1", F, at=LATER)], LISTINGS, exclude=set())
    assert list(p.owner) == [pub.owner_key(ROOT, "SEN-BLOCK-1")]
    assert "block-2: same stone as block-1, not reached: its own decision stands" in p.notes


def test_applying_the_same_clean_again_writes_nothing_also_after_the_run_chose_a_photo():
    s3 = _bucket()
    decisions = [_clean("one", B), _clean("block-1", F)]
    _apply(s3, [_use("one", A)])
    s3.objects[f"{CHOSEN}one.json"] = json.dumps({"key": "one", "sha": A, "tag": C.RUN_TAG}).encode()
    _apply(s3, [{**d, "at": LATER} for d in decisions])
    s3.objects[f"{CHOSEN}one.json"] = json.dumps({"key": "one", "sha": B, "tag": C.CLEAN_TAG}).encode()  # its pass
    before, s3.written = dict(s3.objects), []
    assert dec.plan(s3, [{**d, "at": LATER} for d in decisions], LISTINGS, exclude=set()).empty()
    _apply(s3, [{**d, "at": LATER} for d in decisions])
    assert s3.written == [] and s3.objects == before


@pytest.mark.parametrize("store", ["owner", "removed", "chosen"])
def test_a_decision_on_a_listing_whose_stored_choice_or_removal_cannot_be_read_is_refused(store):
    # read through the photo process's isolated readers (publish.read_records), never a bare read that crashes here
    s3 = _bucket()
    key = {"owner": pub.owner_key(ROOT, "SEN-ONE"), "removed": imagestore.owner_removed_key("SEN-ONE"),
           "chosen": f"{pub.chosen_prefix(ROOT)}one.json"}[store]
    s3.objects[key] = b"{broken"
    before, s3.written = dict(s3.objects), []
    with pytest.raises(dec.InvalidDecisions, match=f"cannot be read .*look at {key} first"):
        dec.plan(s3, [_clean("one", B, at=LATER)], LISTINGS, exclude=set())
    assert s3.objects == before


def test_a_use_never_reaches_a_sibling_whose_stored_choice_cannot_be_read():
    s3 = _bucket()
    s3.objects[pub.owner_key(ROOT, "SEN-BLOCK-2")] = b"[1, 2]"
    p = dec.plan(s3, [_use("block-1", F)], LISTINGS, exclude=set())
    assert list(p.owner) == [pub.owner_key(ROOT, "SEN-BLOCK-1")]
    assert "block-2: same stone as block-1, not reached: its own decision stands" in p.notes


def test_a_use_never_reaches_a_sibling_whose_choice_record_cannot_be_read():
    # review of #548: a sibling's corrupt choice record crashed the whole apply (a bare JSON read)
    s3 = _bucket()
    s3.objects[f"{pub.chosen_prefix(ROOT)}block-2.json"] = b"{broken"
    p = dec.plan(s3, [_use("block-1", F)], LISTINGS, exclude=set())
    assert list(p.owner) == [pub.owner_key(ROOT, "SEN-BLOCK-1")]
    assert "block-2: same stone as block-1, not reached: its own decision stands" in p.notes


def test_a_conflict_photo_is_refused_while_a_partner_s_stored_choice_cannot_be_read():
    # the partner may hold it (the photos run leaves such a listing exactly as published): never a second holder
    s3 = _bucket()
    s3.objects[pub.owner_key(ROOT, "SEN-LEATHERED")] = b"{broken"
    with pytest.raises(dec.InvalidDecisions, match="leathered's stored owner choice cannot be read"):
        dec.plan(s3, [_use("polished", E)], LISTINGS, exclude=set())


def test_a_removal_of_a_listing_whose_sku_has_slashes_is_one_record():
    s3 = _bucket()
    _apply(s3, [_remove("p")], [{**_listing("p", A), "sku": "MAR-P/02/16"}])
    assert s3.written == [imagestore.owner_removed_key("MAR-P/02/16")]
    assert imagestore.owner_removed_skus(s3.objects) == {"MAR-P/02/16"}


def test_applying_the_same_decisions_again_changes_nothing():
    s3 = _bucket()
    decisions = [_use("one", B), _use("two", D, tag=C.CLEAN_TAG), _remove("three"), _use("block-1", F)]
    _apply(s3, decisions)
    before, s3.written = dict(s3.objects), []
    assert dec.plan(s3, decisions, LISTINGS, exclude=set()).empty()
    _apply(s3, decisions)
    assert s3.written == [] and s3.objects == before


# --- shared photos: the same stone, or a conflict -------------------------------------------------------------------------
def test_a_use_extends_to_the_listings_of_the_same_stone_that_show_the_photo():
    s3 = _bucket()
    p = dec.plan(s3, [_use("block-1", F)], LISTINGS, exclude=set())
    assert sorted(p.owner) == [pub.owner_key(ROOT, "SEN-BLOCK-1"), pub.owner_key(ROOT, "SEN-BLOCK-2")]
    dec.apply(s3, p)
    assert _owner(s3, "SEN-BLOCK-2") == {"tag": C.RUN_TAG, "sha": F, "at": AT, "listing": "block-2", "decided_on": "block-1"}


def test_a_sibling_s_own_decision_wins_over_the_extension():
    s3 = _bucket()
    _apply(s3, [_remove("block-2", at=LATER)])
    _apply(s3, [_use("block-1", F)])                                   # older than block-2's own removal: not extended
    assert _owner(s3, "SEN-BLOCK-2") is None and imagestore.owner_removed_key("SEN-BLOCK-2") in s3.objects
    s3 = _bucket()
    _apply(s3, [_use("block-1", F), _remove("block-2")])               # decided in the same file: its own decision
    assert _owner(s3, "SEN-BLOCK-2") is None and imagestore.owner_removed_key("SEN-BLOCK-2") in s3.objects


def test_a_use_never_reaches_a_sibling_live_with_its_own_photo():
    G = "9" * 64
    listings = [_listing("block-1", F, stone=("v4", "f1")), _listing("block-2", G, F, stone=("v4", "f1")),
                _listing("block-3", F, stone=("v4", "f1"))]
    s3 = _bucket()
    s3.objects[f"{RUN}{G}.main.jpg"] = b"m"
    s3.objects[f"{CHOSEN}block-2.json"] = json.dumps({"key": "block-2", "sha": G, "tag": C.RUN_TAG}).encode()
    p = dec.plan(s3, [_use("block-1", F)], listings, exclude=set())
    assert sorted(p.owner) == [pub.owner_key(ROOT, "SEN-BLOCK-1"), pub.owner_key(ROOT, "SEN-BLOCK-3")]
    assert "block-2: same stone as block-1, not reached: it is live with its own photo" in p.notes


def test_a_conflict_photo_is_given_to_one_listing_only():
    s3 = _bucket()
    _apply(s3, [_use("leathered", E)])
    with pytest.raises(dec.InvalidDecisions, match="already the owner's choice for leathered"):
        dec.plan(s3, [_use("polished", E, at=LATER)], LISTINGS, exclude=set())
    with pytest.raises(dec.InvalidDecisions, match="also chosen for leathered in this file"):
        dec.plan(_bucket(), [_use("leathered", E), _use("polished", E)], LISTINGS, exclude=set())
    _apply(s3, [_remove("leathered", at=LATER)])                                    # withdrawn: free for another
    _apply(s3, [_use("polished", E, at=LATER)])
    assert _owner(s3, "SEN-POLISHED")["sha"] == E


def test_the_produce_s_conflict_set_decides_never_the_sheet_s_reading():
    # the sheet shows block-1 and block-2 as one stone, but the produce found F conflicting (a live product of another
    # stone shows it besides its lead): no extension, and a second listing is refused
    s3 = _bucket(conflicts={F: ["block-1", "block-2", "live-other"]})
    p = dec.plan(s3, [_use("block-1", F)], LISTINGS, exclude=set())
    assert list(p.owner) == [pub.owner_key(ROOT, "SEN-BLOCK-1")]
    assert any("conflict: also shown by block-2, live-other" in n for n in p.notes)
    dec.apply(s3, p)
    with pytest.raises(dec.InvalidDecisions, match="already the owner's choice for block-1"):
        dec.plan(s3, [_use("block-2", F, at=LATER)], LISTINGS, exclude=set())


def test_a_conflict_photo_is_assigned_to_the_one_listing_never_its_partners():
    s3 = _bucket()
    _apply(s3, [_use("leathered", E)])
    assert _owner(s3, "SEN-LEATHERED")["sha"] == E and _owner(s3, "SEN-POLISHED") is None


def test_a_dry_run_names_every_listing_a_use_reaches_and_every_conflict_partner(monkeypatch, tmp_path, capsys):
    rc, s3, _ = _main(monkeypatch, tmp_path, [_use("block-1", F), _use("leathered", E)], dry="true")
    out = capsys.readouterr().out
    assert rc == 0 and s3.written == []
    assert "SEN-BLOCK-2 (block-2): same stone as block-1" in out
    assert "conflict: also shown by polished (another variety or finish), not assigned to it" in out


# --- bad input is refused whole -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("bad, why", [
    (_use("nowhere", A), "not on the product sheet"),
    ({**_use("one", A), "action": "keep"}, "unknown action"),
    (_use("one", A, source="zucchi"), "source"),
    (_use("one", D), "not one of the listing's photos"),
    (_use("one", A, tag="v1"), "tag"),
    (_use("three", "c" * 64), "no result"),
    (_use("one", A, tag=C.CLEAN_TAG), "no result"),
    (_remove("one", sha=A), "fields"),
    ({k: v for k, v in _use("one", A).items() if k != "at"}, "fields"),
    (_use("one", "not-a-sha"), "sha"),
    (_use("one", A, at="yesterday"), "UTC timestamp"),
    (_use("one", A, at="2026-11-02T10:00:00+02:00"), "UTC timestamp"),
    (_clean("one", B, tag=C.RUN_TAG), "fields"),
    (_clean("one", D), "not one of the listing's photos"),
    (_clean("one", "not-a-sha"), "sha"),
    (_clean("one", B, source="zucchi"), "source"),
    (_clean("leathered", E), "conflict photo"),
])
def test_a_bad_decision_refuses_the_whole_file_and_nothing_is_written(bad, why):
    s3 = _bucket()
    with pytest.raises(dec.InvalidDecisions) as refused:
        dec.plan(s3, [_use("two", D), bad], LISTINGS, exclude=set())
    assert why in str(refused.value) and s3.written == []


def test_a_listing_decided_twice_in_one_file_is_refused():
    with pytest.raises(dec.InvalidDecisions, match="more than once"):
        dec.plan(_bucket(), [_use("one", A), _remove("one")], LISTINGS, exclude=set())


def test_an_excluded_photo_is_refused():
    with pytest.raises(dec.InvalidDecisions, match="on the exclude list"):
        dec.plan(_bucket(), [_use("one", A)], LISTINGS, exclude={A})
    with pytest.raises(dec.InvalidDecisions, match="on the exclude list"):
        dec.plan(_bucket(), [_clean("one", A)], LISTINGS, exclude={A})


def test_a_clean_of_a_photo_not_judged_yet_is_accepted_and_the_run_reports_it_pending():
    # review of #546: the applier refused it as "cannot fix" while the run called the same photo pending; one rule now
    s3 = _bucket()
    s3.objects[f"{RUN}judged/{B}.json"] = json.dumps({"verdict": "unparsed", "raw": "?", "version": C.JUDGE_VERSION,
                                                       "asks": 1}).encode()
    p = dec.plan(s3, [_clean("three", "c" * 64), _clean("one", B)], LISTINGS, exclude=set())
    assert not p.empty()


def test_a_clean_of_a_photo_the_judge_passed_is_refused():
    s3 = _bucket()
    s3.objects[f"{RUN}judged/{B}.json"] = _verdict("pass")
    with pytest.raises(dec.InvalidDecisions, match="a cleaned copy cannot fix"):
        dec.plan(s3, [_clean("one", B)], LISTINGS, exclude=set())


def test_an_old_file_applied_again_never_undoes_a_later_decision():
    s3 = _bucket()
    old = [_remove("one")]
    _apply(s3, old)
    _apply(s3, [_use("one", B, at=LATER)])
    before, s3.written = dict(s3.objects), []
    with pytest.raises(dec.InvalidDecisions, match="older than the decision already stored"):
        dec.plan(s3, old, LISTINGS, exclude=set())
    assert s3.objects == before


def test_the_export_must_be_a_list_of_decisions():
    for text in ("{}", "not json", '[1]'):
        with pytest.raises(dec.InvalidDecisions):
            dec.read_decisions(text)
    assert dec.read_decisions(json.dumps([_remove("three")])) == [_remove("three")]


# --- running it -----------------------------------------------------------------------------------------------------------
def _main(monkeypatch, tmp_path, decisions, dry="false", lock=None, env="production"):
    s3 = _bucket()
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps(decisions))
    monkeypatch.setenv("DECISIONS_FILE", str(path))
    if dry is None:
        monkeypatch.delenv("DRY_RUN", raising=False)
    else:
        monkeypatch.setenv("DRY_RUN", dry)
    if env is None:
        monkeypatch.delenv("SCRAPER_ENV", raising=False)
    else:
        monkeypatch.setenv("SCRAPER_ENV", env)
    monkeypatch.setattr(dec.boto3, "client", lambda *a, **k: s3)
    monkeypatch.setattr(dec, "photo_listings", lambda client: LISTINGS)
    held = []

    @contextmanager
    def exclusive(client, owner, on_lost=None):
        if lock:
            raise lock
        held.append(True)
        yield
        held.append(False)
    monkeypatch.setattr(dec.proc, "exclusive", exclusive)
    return dec.main(), s3, held


def test_a_dry_run_prints_the_environment_and_the_plan_and_writes_nothing(monkeypatch, tmp_path, capsys):
    rc, s3, held = _main(monkeypatch, tmp_path, [_use("one", B), _remove("three"), _clean("two", D)], dry="true")
    out = capsys.readouterr().out
    assert rc == 0 and s3.written == [] and held == []
    assert out.startswith(f"==> environment {dec.ENV_NAME}, bucket {dec.S3_BUCKET}")
    assert "owner choice" in out and "SEN-THREE" in out and "DRY_RUN" in out
    assert f"clean SEN-TWO (two): {D[:12]}" in out and "1 cleans" in out


def test_the_decisions_are_applied_inside_the_run_lock(monkeypatch, tmp_path):
    rc, s3, held = _main(monkeypatch, tmp_path, [_use("one", B), _remove("three")])
    assert rc == 0 and held == [True, False]
    assert pub.owner_key(ROOT, "SEN-ONE") in s3.written and imagestore.owner_removed_key("SEN-THREE") in s3.written


def test_nothing_is_applied_while_a_photos_run_holds_the_lock(monkeypatch, tmp_path):
    rc, s3, _ = _main(monkeypatch, tmp_path, [_use("one", B)], lock=dec.proc.RunActive("held"))
    assert rc == 4 and s3.written == []


def test_a_bad_file_exits_2_and_writes_nothing(monkeypatch, tmp_path, capsys):
    rc, s3, _ = _main(monkeypatch, tmp_path, [_use("one", B), _use("nowhere", A)])
    assert rc == 2 and s3.written == [] and "nowhere" in capsys.readouterr().out


@pytest.mark.parametrize("dry", [None, "", "yes", "maybe", "0"])
def test_dry_run_must_be_true_or_false(monkeypatch, tmp_path, dry):
    rc, s3, held = _main(monkeypatch, tmp_path, [_use("one", B)], dry=dry)
    assert rc == 2 and s3.written == [] and held == []


def test_no_decision_is_planned_before_the_produce_stored_the_conflict_set(monkeypatch, tmp_path, capsys):
    s3 = _bucket()
    del s3.objects[imagestore.conflicts_key("sentas")]
    with pytest.raises(dec.InvalidDecisions, match="no conflict set yet for sentas: run a produce first"):
        dec.plan(s3, [_use("one", B)], LISTINGS, exclude=set())
    assert s3.written == []


def test_nothing_runs_without_an_explicit_environment(monkeypatch, tmp_path, capsys):
    rc, s3, held = _main(monkeypatch, tmp_path, [_use("one", B)], env=None)
    assert rc == 2 and s3.written == [] and held == [] and "SCRAPER_ENV" in capsys.readouterr().out
