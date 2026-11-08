"""The hidden-listings page's side of the photo process (stone_pipeline.photos.review, served by the config API under
/config/v1/photos): the ledger's hidden listings joined with the review list, the owner's removals, the choice records
and the live clicks, each row with its actions and its photos' images as presigned URLs made afresh on every request
(never stored); the month's spend and paid uses; the owner's click and its withdrawal, through the one decisions writer
(stone_pipeline.photos.decisions.decide), refused while a photos run holds the lock. S3 and the ledger are stand-ins."""
from __future__ import annotations

import io
import json
from contextlib import contextmanager

import pytest
from botocore.exceptions import ClientError

from stone_pipeline.photos import decisions as dec
from stone_pipeline.photos import publish as pub
from stone_pipeline.photos import review as rv
from stone_pipeline.config.control import server
from stone_pipeline.config.settings import ENV_NAME, ENV_SEGMENT
from stone_pipeline.config.standardise_settings import STANDARDISE as C
from stone_pipeline.io import imagestore

A, B, D = "a" * 64, "b" * 64, "d" * 64
_REAL_SHEET = rv.sheet                                          # before the autouse fixture stands it in
RUN = f"{ENV_SEGMENT}/standardise-test/{C.RUN_TAG}/sentas/"
CLN = f"{ENV_SEGMENT}/standardise-test/{C.CLEAN_TAG}/sentas/"
ROOT = f"{ENV_SEGMENT}/standardise-test/{C.CLEAN_TAG}/"
REVIEW = f"{ROOT}{C.REVIEW_FILE}"
AT, LATER = "2026-11-07T10:00:00.000Z", "2026-11-07T11:00:00.000Z"


class _S3:
    """A bucket in a dict, every write recorded; a presigned URL names its key and how many were signed so far."""

    def __init__(self, objects):
        self.objects, self.written, self.signed = dict(objects), [], []

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

    def generate_presigned_url(self, ClientMethod, Params, ExpiresIn):
        assert ClientMethod == "get_object" and ExpiresIn == C.REVIEW_URL_TTL_SECONDS
        self.signed.append(Params["Key"])
        return f"https://signed/{Params['Key']}?n={len(self.signed)}"


def _verdict(v, *kinds):
    return json.dumps({"verdict": v, "problems": [{"kind": k} for k in kinds], "version": C.JUDGE_VERSION}).encode()


def _listing(key, *photos, fmt="slab"):
    return {"key": key, "sku": f"SEN-{key.upper()}", "source": "sentas", "stone": ("v1", key), "format": fmt,
            "photos": list(photos)}


ONE, TWO, TILE = _listing("one", A, B), _listing("two", D), _listing("tile-1", fmt="tile")
GONE, CHOSEN, UNSEEN = _listing("gone"), _listing("chosen"), _listing("unseen")
PHOTO_LISTINGS = [ONE, TWO, GONE, CHOSEN, UNSEEN]


def _bucket(**extra):
    """A and B of listing one failed the judge for a reflection (a click's work), D of listing two too."""
    return _S3({**{f"{RUN}{s}.main.jpg": b"m" for s in (A, B, D)},
                **{f"{RUN}judged/{s}.json": _verdict("fail", "light_reflection") for s in (A, B, D)},
                imagestore.conflicts_key("sentas"): b"{}", **extra})


@pytest.fixture(autouse=True)
def _sheet(monkeypatch):
    """The product sheet's photo and tile listings, and the decisions applier's own reading of them; no lock held."""
    monkeypatch.setattr(rv, "sheet", lambda s3: (PHOTO_LISTINGS, [TILE]))
    monkeypatch.setattr(dec, "photo_listings", lambda s3: PHOTO_LISTINGS)
    monkeypatch.setattr(dec, "image_pipeline_off", lambda: set())

    @contextmanager
    def exclusive(client, owner, on_lost=None):
        yield
    monkeypatch.setattr(dec.proc, "exclusive", exclusive)


def _hidden_in_ledger(monkeypatch, *listings):
    monkeypatch.setattr(rv, "hidden_products", lambda: {l["sku"]: {"listing": l["key"], "source": l["source"]}
                                                       for l in listings})


def _photo(sha, state="fail", fixable=True, left=("nano2", "kontext", "flux3"), copy=None):
    return {"sha": sha, "tag": C.RUN_TAG, "state": state, "kinds": ["light_reflection"], "held": None,
            "fixable": fixable, "auto": False, "clean": {"tried": [], "left": list(left), "copy": copy}}


def _row(listing, reason="awaiting_click", photos=None, click=None):
    return {"listing": listing["key"], "source": "sentas", "format": listing["format"], "reason": reason, "why": None,
            "click": click, "photos": photos if photos is not None else [_photo(s) for s in listing["photos"]]}


# --- GET /photos/hidden -----------------------------------------------------------------------------------------------
def test_each_listing_the_ledger_hides_has_one_row_with_its_reason_from_the_join(monkeypatch):
    _hidden_in_ledger(monkeypatch, ONE, GONE, CHOSEN, UNSEEN, TILE)
    tile_row = {**_row(TILE, "tile_no_stone", photos=[]), "why": "no tile of this size fits"}
    s3 = _bucket(**{REVIEW: json.dumps({"SEN-ONE": _row(ONE), "SEN-TWO": _row(TWO), "SEN-TILE-1": tile_row}).encode(),
                    imagestore.owner_removed_key("SEN-GONE"): json.dumps({"sku": "SEN-GONE", "listing": "gone",
                                                                          "source": "sentas"}).encode(),
                    f"{ROOT}chosen/chosen.json": json.dumps({"key": "chosen", "sha": A}).encode()})
    rows = {r["sku"]: r for r in rv.hidden(s3)["rows"]}
    assert {sku: r["reason"] for sku, r in rows.items()} == {
        "SEN-ONE": "awaiting_click", "SEN-GONE": "owner_removed", "SEN-CHOSEN": "chosen_not_linked",
        "SEN-UNSEEN": "not_worked_yet", "SEN-TILE-1": "tile_no_stone"}             # two: not hidden, not listed
    assert rows["SEN-UNSEEN"] == {"sku": "SEN-UNSEEN", "listing": "unseen", "source": "sentas", "format": "slab",
                                  "reason": "not_worked_yet", "why": None, "click": None, "photos": [],
                                  "actions": ["remove"]}
    assert rows["SEN-TILE-1"]["why"] == "no tile of this size fits" and rows["SEN-TILE-1"]["actions"] == []
    assert rows["SEN-GONE"]["actions"] == [] and rows["SEN-CHOSEN"]["actions"] == ["remove"]
    assert [r["sku"] for r in rv.hidden(s3)["rows"]] == sorted(rows, key=lambda sku: (rows[sku]["source"],
                                                                                     rows[sku]["listing"]))


def test_image_urls_are_signed_afresh_on_every_request_and_never_stored(monkeypatch):
    _hidden_in_ledger(monkeypatch, ONE)
    photos = [_photo(A), _photo(B, state="no_result", copy="fail")]
    stored = json.dumps({"SEN-ONE": _row(ONE, photos=photos)}).encode()
    s3 = _bucket(**{REVIEW: stored})
    first, second = rv.hidden(s3)["rows"][0]["photos"], rv.hidden(s3)["rows"][0]["photos"]
    scraped = imagestore.scraped_prefix("sentas")
    assert first[0]["urls"] == {"original": f"https://signed/{scraped}{A}.jpg?n=1",
                                "result": f"https://signed/{RUN}{A}.main.jpg?n=2", "cleaned": None}
    assert first[1]["urls"] == {"original": f"https://signed/{scraped}{B}.jpg?n=3", "result": None,
                                "cleaned": f"https://signed/{CLN}cleaned/{B}.jpg?n=4"}
    assert second[0]["urls"]["original"].endswith("?n=5")                       # minted again, not cached
    assert s3.objects[REVIEW] == stored and b"http" not in stored and REVIEW not in s3.written


def test_a_row_offers_clean_for_a_photo_a_click_can_still_fix_and_withdraw_for_a_live_click(monkeypatch):
    _hidden_in_ledger(monkeypatch, ONE, TWO)
    one = _row(ONE, photos=[_photo(A, fixable=False, left=()), _photo(B)])
    two = _row(TWO, "clean_failed", photos=[_photo(D, left=())])
    click = pub.click_record("SEN-TWO", "two", "sentas", D, AT)
    s3 = _bucket(**{REVIEW: json.dumps({"SEN-ONE": one, "SEN-TWO": two}).encode(),
                    f"{pub.clicks_prefix(ROOT)}{imagestore.sku_file('SEN-TWO')}": json.dumps(click).encode()})
    rows = {r["sku"]: r for r in rv.hidden(s3)["rows"]}
    assert rows["SEN-ONE"]["actions"] == ["use", "clean", "remove"]
    assert [p["clickable"] for p in rows["SEN-ONE"]["photos"]] == [False, True]
    assert [p["usable"] for p in rows["SEN-ONE"]["photos"]] == [[C.RUN_TAG], [C.RUN_TAG]]
    assert rows["SEN-TWO"]["actions"] == ["use", "withdraw", "remove"] and rows["SEN-TWO"]["photos"][0]["clickable"] is False


def test_a_row_shows_the_live_click_the_run_s_state_while_it_is_the_same_and_queued_once_changed(monkeypatch):
    _hidden_in_ledger(monkeypatch, ONE)
    seen = {"sha": B, "at": AT, "state": "waiting", "why": "cleaning cap reached"}
    clicks = pub.clicks_prefix(ROOT) + imagestore.sku_file("SEN-ONE")
    s3 = _bucket(**{REVIEW: json.dumps({"SEN-ONE": _row(ONE, click=seen)}).encode(),
                    clicks: json.dumps(pub.click_record("SEN-ONE", "one", "sentas", B, AT)).encode()})
    assert rv.hidden(s3)["rows"][0]["click"] == seen
    s3.objects[clicks] = json.dumps(pub.click_record("SEN-ONE", "one", "sentas", A, LATER)).encode()
    assert rv.hidden(s3)["rows"][0]["click"] == {"sha": A, "at": LATER, "state": "queued", "why": None}
    s3.objects[clicks] = b"not json"
    row = rv.hidden(s3)["rows"][0]
    assert row["click"]["state"] == "blocked" and row["click"]["why"].startswith("its file cannot be read")
    assert row["actions"] == [] and not any(p["clickable"] or p["usable"] for p in row["photos"])   # all refused
    del s3.objects[clicks]
    assert rv.hidden(s3)["rows"][0]["click"] is None                            # withdrawn since the run


def _offered(row):
    """The decision documents each action the row offers stands for (what the page sends for it)."""
    base = {"id": row["listing"], "source": row["source"], "at": rv._now()}     # the page sends its own now
    for action in row["actions"]:
        if action == "use":
            yield from ({**base, "action": "use", "sha": p["sha"], "tag": tag} for p in row["photos"] for tag in p["usable"])
        elif action == "clean":
            yield from ({**base, "action": "clean", "sha": p["sha"]} for p in row["photos"] if p["clickable"])
        else:
            yield {**base, "action": action}


def test_no_action_a_row_offers_is_refused_by_the_applier(monkeypatch):
    # manager 2026-11-07: an action must never lead to a 422 for its row; the applier's own rules decide what is offered
    _hidden_in_ledger(monkeypatch, ONE, TWO, GONE, CHOSEN, UNSEEN, TILE)
    one = _row(ONE, photos=[_photo(A, fixable=False, left=()), _photo(B)])
    two = _row(TWO, photos=[_photo(D, state="pass", copy="fail")])
    s3 = _bucket(**{REVIEW: json.dumps({"SEN-ONE": one, "SEN-TWO": two}).encode(),
                    f"{CLN}cleaned/{D}.jpg": b"c", f"{CLN}{D}.main.jpg": b"m",
                    imagestore.owner_removed_key("SEN-GONE"): json.dumps({"sku": "SEN-GONE", "listing": "gone",
                                                                          "source": "sentas", "at": AT}).encode(),
                    f"{pub.clicks_prefix(ROOT)}{imagestore.sku_file('SEN-ONE')}": json.dumps(
                        pub.click_record("SEN-ONE", "one", "sentas", B, AT)).encode()})
    rows = rv.hidden(s3)["rows"]
    docs = [doc for row in rows for doc in _offered(row)]
    assert {d["action"] for d in docs} == {"use", "clean", "remove", "withdraw"}
    assert [p["usable"] for p in next(r for r in rows if r["sku"] == "SEN-TWO")["photos"]] == [[C.RUN_TAG, C.CLEAN_TAG]]
    for doc in docs:
        dec.plan(s3, [doc], PHOTO_LISTINGS, exclude=set())                      # never InvalidDecisions


# --- the hidden list a page at a time (manager 2026-11-07: dev's full list was 48 s and 13 MB in one answer) ----------
def _many(monkeypatch, n):
    """n hidden listings, each with a run's row (awaiting_click on its photo A-like sha), and the bucket."""
    listings = [_listing(f"l{i:02d}", f"{i + 16:x}" * 32) for i in range(n)]
    monkeypatch.setattr(rv, "sheet", lambda s3: (listings, [TILE]))
    monkeypatch.setattr(dec, "photo_listings", lambda s3: listings)
    _hidden_in_ledger(monkeypatch, *listings, TILE, UNSEEN)
    review = {l["sku"]: _row(l) for l in listings}
    review["SEN-TILE-1"] = {**_row(TILE, "tile_no_stone", photos=[]), "why": "no tile fits"}
    s3 = _bucket(**{REVIEW: json.dumps(review).encode(),
                    **{f"{RUN}{l['photos'][0]}.main.jpg": b"m" for l in listings},
                    **{f"{RUN}judged/{l['photos'][0]}.json": _verdict("fail", "light_reflection") for l in listings}})
    return listings, s3


def test_the_list_is_ordered_then_sliced_and_counts_every_hidden_row(monkeypatch):
    listings, s3 = _many(monkeypatch, 7)
    first = rv.hidden(s3, None, 0, 3)
    assert (first["total"], first["offset"], first["limit"]) == (9, 0, 3)
    assert first["counts"] == {"awaiting_click": 7, "not_worked_yet": 1, "tile_no_stone": 1}
    whole = [r["sku"] for r in rv.hidden(s3, None, 0, 100)["rows"]]
    assert whole == sorted(whole, key=lambda sku: next((l["key"] for l in listings + [TILE, UNSEEN] if l["sku"] == sku)))
    pages = [r["sku"] for off in (0, 3, 6) for r in rv.hidden(s3, None, off, 3)["rows"]]
    assert pages == whole and rv.hidden(s3, None, 9, 3)["rows"] == []
    tiles = rv.hidden(s3, "tile_no_stone", 0, 50)
    assert tiles["total"] == 1 and [r["sku"] for r in tiles["rows"]] == ["SEN-TILE-1"]
    assert tiles["counts"] == first["counts"]                            # counts never filtered


def test_only_the_page_s_photos_are_read_and_signed(monkeypatch):
    listings, s3 = _many(monkeypatch, 7)
    page = rv.hidden(s3, None, 2, 2)["rows"]
    on_page = {p["sha"] for r in page for p in r["photos"]}
    assert len(on_page) == 2 and all(any(sha in key for sha in on_page) for key in s3.signed)
    judged = []
    get = s3.get_object
    s3.get_object = lambda Bucket, Key: (judged.append(Key) if "/judged/" in Key else None) or get(Bucket, Key)
    rv.hidden(s3, None, 2, 2)
    assert {k.rsplit("/", 1)[1][:-len(".json")] for k in judged} <= on_page


def test_a_removed_listing_shows_its_photos_and_offers_use_and_clean_the_applier_accepts(monkeypatch):
    # manager ruling (c): a removal is never a dead end on the page; a use or a clean lifts it (the applier's rule)
    _hidden_in_ledger(monkeypatch, ONE)
    s3 = _bucket(**{imagestore.owner_removed_key("SEN-ONE"): json.dumps(
        {"sku": "SEN-ONE", "listing": "one", "source": "sentas", "reason": C.REMOVED_REASON, "at": AT}).encode()})
    row = rv.hidden(s3)["rows"][0]
    assert row["reason"] == "owner_removed" and [p["sha"] for p in row["photos"]] == [A, B]
    assert row["actions"] == ["use", "clean"] and all(p["urls"]["original"] for p in row["photos"])
    docs = list(_offered(row))
    for doc in docs:
        dec.plan(s3, [doc], PHOTO_LISTINGS, exclude=set())               # never InvalidDecisions
    dec.apply(s3, dec.plan(s3, [next(d for d in docs if d["action"] == "use")], PHOTO_LISTINGS, exclude=set()))
    assert imagestore.owner_removed_key("SEN-ONE") not in s3.objects    # the use lifted the removal


def test_the_page_is_asked_for_by_reason_offset_and_limit_validated_at_the_route(monkeypatch):
    seen = []
    monkeypatch.setattr(rv, "client", lambda: "s3")
    monkeypatch.setattr(rv, "hidden", lambda s3, reason, offset, limit: seen.append((reason, offset, limit)) or {})
    assert server.dispatch("GET", ["photos", "hidden"], None)[0] == 200
    assert server.dispatch("GET", ["photos", "hidden"], None, "reason=pending&offset=50&limit=100")[0] == 200
    assert seen == [(None, 0, C.REVIEW_PAGE_DEFAULT), ("pending", 50, 100)]
    for query in ("reason=nope", "reason=", "offset=-1", "offset=x", "limit=0", f"limit={C.REVIEW_PAGE_MAX + 1}",
                  "limit=1.5", "offset=1&offset=2", "reason=pending&reason=pending"):
        code, body = server.dispatch("GET", ["photos", "hidden"], None, query)
        assert code == 400 and query.split("=")[0] in body["error"], query
    assert len(seen) == 2


def test_a_review_list_that_cannot_be_read_is_said_never_served_empty(monkeypatch):
    _hidden_in_ledger(monkeypatch, ONE)
    with pytest.raises(rv.Unavailable, match="review.json"):
        rv.hidden(_bucket(**{REVIEW: b"not json"}))


def test_before_any_full_run_every_hidden_listing_is_not_worked_yet(monkeypatch):
    _hidden_in_ledger(monkeypatch, ONE)
    assert [r["reason"] for r in rv.hidden(_bucket())["rows"]] == ["not_worked_yet"]


def test_the_hidden_products_are_read_from_the_ledger_by_reason_no_photo_with_their_source_names(tmp_path, monkeypatch):
    from stone_pipeline.config import store
    from stone_pipeline.ledger import writethrough
    from stone_pipeline.ledger.db import Ledger
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    store.upsert_row({"source": "sentas", "source_code": "sen", "vendor": "Sentas Marble", "ports": ["Izmir"]})
    path = tmp_path / "dev.ledger"
    monkeypatch.setattr(writethrough, "ledger_path", lambda: path)
    with Ledger.open(path, env=ENV_NAME, backend_id_fingerprint=writethrough.backend_fingerprint()) as lg:
        for sku, handle, reason in (("SEN-1", "one", "no_photo"), ("SEN-2", "two", None), ("SEN-3", "three", "delisted")):
            lg.upsert("product", {"sku": sku, "source": "sen", "handle": handle, "created_at": AT, "updated_at": AT},
                      pk=("sku",))
            lg.upsert("inventory", {"sku": sku, "qty": 0, "reason": reason, "updated_at": AT}, pk=("sku",))
    assert rv.hidden_products() == {"SEN-1": {"listing": "one", "source": "sentas"}}
    path.unlink()
    with pytest.raises(rv.Unavailable, match="ledger"):
        rv.hidden_products()


# --- GET /photos/spend ------------------------------------------------------------------------------------------------
def test_the_month_s_spend_lists_each_run_the_cleaning_uses_and_both_budgets(monkeypatch):
    monkeypatch.setattr(C, "BUDGET_USD", 100.0)
    monkeypatch.setattr(C, "CLEAN_BUDGET_USD", 10.0)
    # the shape select.clean_photo records (Spend.record stamps at); the run is the file it is kept in
    use = {"at": "2026-11-07T10:00:00+00:00", "source": "sentas", "sha": A, "sku": "SEN-ONE", "listing": "one",
           "reason": "click", "kinds": ["light_reflection"], "click_at": AT, "model": "nano2", "call": "edit", "usd": 0.12}
    early = {**use, "at": "2026-11-02T09:00:00+00:00", "call": "check", "usd": 0.02}
    spend, clean = f"{ROOT[:-len(C.CLEAN_TAG) - 1]}{C.SPEND_DIR}/2026-11/", f"{ROOT[:-len(C.CLEAN_TAG) - 1]}{C.CLEAN_SPEND_DIR}/2026-11/"
    s3 = _S3({f"{spend}R1.json": json.dumps({"usd": 3.5, "uses": []}).encode(),
              f"{spend}R2.json": json.dumps({"usd": 1.25, "uses": []}).encode(),
              f"{spend}R3.json": b"not json", f"{spend}R4.json": json.dumps({"uses": []}).encode(),
              f"{clean}R2.json": json.dumps({"usd": 0.12, "uses": [use]}).encode(),
              f"{clean}R1.json": json.dumps({"usd": 0.02, "uses": [early]}).encode(),
              f"{spend[:-8]}2026-10/R0.json": json.dumps({"usd": 50.0, "uses": []}).encode()})
    out = rv.spend(s3, "2026-11")
    assert out["month"] == "2026-11"
    assert out["process"] == {"budget_usd": 100.0, "spent_usd": 4.75,
                              "runs": [{"run_id": "R1", "usd": 3.5}, {"run_id": "R2", "usd": 1.25}],
                              "unreadable": [{"run_id": "R3", "error": out["process"]["unreadable"][0]["error"]},
                                             {"run_id": "R4", "error": "usd is None, not a number"}]}
    assert out["cleaning"]["budget_usd"] == 10.0 and out["cleaning"]["spent_usd"] == 0.14
    assert out["cleaning"]["uses"] == [{"run_id": "R1", **early}, {"run_id": "R2", **use}]
    assert out["cleaning"]["unreadable"] == []


# --- PUT /photos/clicks/{sku} -------------------------------------------------------------------------------------------
def _clicks(s3):
    return pub.read_clicks(s3, ROOT)


def test_a_click_on_a_hidden_listing_s_fixable_photo_is_queued_through_the_one_writer(monkeypatch):
    _hidden_in_ledger(monkeypatch, ONE)
    written = []
    monkeypatch.setattr(rv.decisions, "decide", lambda s3, docs, dry_run, say, sheet, narrow: written.append(
        (docs, dry_run, sheet, narrow)) or dec.plan(s3, docs, sheet, set(), narrow=narrow))
    s3 = _bucket()
    assert rv.click(s3, "SEN-ONE", B, AT) == (200, {"sku": "SEN-ONE", "status": "queued"})
    doc = {"id": "one", "action": "clean", "source": "sentas", "sha": B, "at": AT}
    assert written == [([doc], False, PHOTO_LISTINGS, True)]    # applied once, under the lock, on the sheet read once


def test_a_queued_click_is_the_record_the_photos_run_reads(monkeypatch):
    _hidden_in_ledger(monkeypatch, ONE)
    s3 = _bucket()
    assert rv.click(s3, "SEN-ONE", B, AT)[1]["status"] == "queued"
    assert _clicks(s3) == {"SEN-ONE": pub.click_record("SEN-ONE", "one", "sentas", B, AT)}


def test_a_click_on_a_listing_not_hidden_is_409_and_writes_nothing(monkeypatch):
    _hidden_in_ledger(monkeypatch, TWO)
    s3 = _bucket()
    code, body = rv.click(s3, "SEN-ONE", B, AT)
    assert (code, body["reason"]) == (409, "not_hidden") and s3.written == []
    monkeypatch.setattr(dec, "image_pipeline_off", lambda: {"sentas"})          # never hidden: the applier's 422 first
    code, body = rv.click(s3, "SEN-ONE", B, AT)
    assert code == 422 and "image pipeline is off" in body["error"] and s3.written == []


def test_a_click_on_a_listing_whose_photo_now_passes_is_409_chosen_not_linked(monkeypatch):
    _hidden_in_ledger(monkeypatch, ONE)
    s3 = _bucket(**{f"{RUN}judged/{A}.json": _verdict("pass")})
    code, body = rv.click(s3, "SEN-ONE", B, AT)
    assert (code, body["reason"]) == (409, "chosen_not_linked") and s3.written == []
    s3 = _bucket(**{f"{ROOT}chosen/one.json": json.dumps({"key": "one", "sha": A}).encode()})
    assert rv.click(s3, "SEN-ONE", B, AT)[1]["reason"] == "chosen_not_linked"


def test_a_click_on_a_photo_already_cleaned_is_200_already_cleaned_and_writes_nothing(monkeypatch):
    _hidden_in_ledger(monkeypatch, ONE)
    s3 = _bucket(**{f"{CLN}cleaned/{B}.jpg": b"c"})
    assert rv.click(s3, "SEN-ONE", B, AT) == (200, {"sku": "SEN-ONE", "status": "already_cleaned"})
    assert s3.written == []


def test_a_click_on_a_photo_with_no_model_left_is_200_nothing_left_to_try_and_writes_nothing(monkeypatch):
    _hidden_in_ledger(monkeypatch, ONE)
    failed = json.dumps({"passes": False, "clean_version": C.CLEAN_VERSION}).encode()
    s3 = _bucket(**{f"{CLN}attempts/{B}.{m}.json": failed for m in ("nano2", "kontext", "flux3")})
    assert rv.click(s3, "SEN-ONE", B, AT) == (200, {"sku": "SEN-ONE", "status": "nothing_left_to_try"})
    assert s3.written == []


@pytest.mark.parametrize("case, match", [
    ("unfixable", "a cleaned copy cannot fix"), ("conflict", "conflict photo"), ("excluded", "exclude list"),
    ("off", "image pipeline is off"), ("tile", "not on the product sheet as a photo listing")])
def test_a_click_the_photos_run_could_never_use_is_422_with_the_applier_s_reason(monkeypatch, case, match):
    listing = TILE if case == "tile" else ONE
    _hidden_in_ledger(monkeypatch, listing)
    s3 = _bucket()
    sha = B
    if case == "unfixable":
        s3.objects[f"{RUN}judged/{B}.json"] = _verdict("fail", "not_one_slab")
    elif case == "conflict":
        s3.objects[imagestore.conflicts_key("sentas")] = json.dumps({B: ["one", "other"]}).encode()
        monkeypatch.setattr(dec, "photo_listings", lambda client: [ONE, TWO, _listing("other", B)])
    elif case == "excluded":
        monkeypatch.setattr(dec, "excluded", lambda: {B})
    elif case == "off":
        monkeypatch.setattr(dec, "image_pipeline_off", lambda: {"sentas"})
    code, body = rv.click(s3, listing["sku"], sha, AT)
    assert code == 422 and match in body["error"] and s3.written == []


def test_a_click_while_a_photos_run_holds_the_lock_is_409_naming_the_run(monkeypatch):
    _hidden_in_ledger(monkeypatch, ONE)

    @contextmanager
    def held(client, owner, on_lost=None):
        raise dec.proc.RunActive("held")
        yield
    monkeypatch.setattr(dec.proc, "exclusive", held)
    s3 = _bucket(**{dec.proc.LOCK: json.dumps({"run_id": "20261107T090000Z"}).encode()})
    code, body = rv.click(s3, "SEN-ONE", B, AT)
    assert (code, body["reason"], body["run_id"]) == (409, "run_active", "20261107T090000Z") and s3.written == []


def test_a_click_never_lists_a_whole_run_folder(monkeypatch):
    # review of #555: dev v2/zucchi holds 56,840 keys; a click reads only its listing's photos
    _hidden_in_ledger(monkeypatch, ONE)
    s3 = _bucket()
    listed = []
    pages = s3.get_paginator

    def paginator(name):
        inner = pages(name)

        class _Pages:
            def paginate(self, Bucket, Prefix):
                listed.append(Prefix)
                return inner.paginate(Bucket=Bucket, Prefix=Prefix)
        return _Pages()
    s3.get_paginator = paginator
    assert rv.click(s3, "SEN-ONE", B, AT)[1]["status"] == "queued"
    assert RUN not in listed and CLN not in listed and f"{ROOT}chosen/" not in listed


def test_a_click_reads_only_its_listing_s_candidates_and_the_clicked_photo(monkeypatch):
    # dev SEN-6978 has 114 photos: what the run judges (its first JUDGE_MAX_PER_LISTING) and the click are all a click asks
    many = [f"{i:x}" * 64 for i in range(1, 7)]
    listing = _listing("long", *many)
    monkeypatch.setattr(rv, "sheet", lambda s3: ([ONE, TWO, listing], [TILE]))
    monkeypatch.setattr(dec, "photo_listings", lambda s3: [ONE, TWO, listing])
    _hidden_in_ledger(monkeypatch, listing)
    s3 = _bucket(**{f"{RUN}{s}.main.jpg": b"m" for s in many},
                 **{f"{RUN}judged/{s}.json": _verdict("fail", "light_reflection") for s in many})
    listed = []
    pages = s3.get_paginator

    def paginator(name):
        inner = pages(name)

        class _Pages:
            def paginate(self, Bucket, Prefix):
                listed.append(Prefix)
                return inner.paginate(Bucket=Bucket, Prefix=Prefix)
        return _Pages()
    s3.get_paginator = paginator
    assert rv.click(s3, "SEN-LONG", many[5], AT)[1]["status"] == "queued"
    read = {sha for sha in many if any(sha in prefix for prefix in listed)}
    assert read == set(many[:C.JUDGE_MAX_PER_LISTING]) | {many[5]}


def test_a_click_while_a_decisions_apply_holds_the_lock_says_so(monkeypatch):
    _hidden_in_ledger(monkeypatch, ONE)

    @contextmanager
    def held(client, owner, on_lost=None):
        raise dec.proc.RunActive("held")
        yield
    monkeypatch.setattr(dec.proc, "exclusive", held)
    s3 = _bucket(**{dec.proc.LOCK: json.dumps({"run_id": "decisions-20261107T120000Z"}).encode()})
    code, body = rv.click(s3, "SEN-ONE", B, AT)
    assert code == 409 and "a decisions apply holds the lock" in body["error"] and "photos run" not in body["error"]


def test_a_click_on_an_unknown_sku_is_404(monkeypatch):
    _hidden_in_ledger(monkeypatch, ONE)
    assert rv.click(_bucket(), "SEN-NOWHERE", B, AT)[0] == 404


# --- DELETE /photos/clicks/{sku} ----------------------------------------------------------------------------------------
def test_a_withdraw_removes_the_click_through_the_one_writer_and_a_second_finds_none(monkeypatch):
    _hidden_in_ledger(monkeypatch, ONE)
    s3 = _bucket()
    rv.click(s3, "SEN-ONE", B, AT)
    assert rv.withdraw(s3, "SEN-ONE", LATER) == (200, {"sku": "SEN-ONE", "status": "withdrawn"})
    assert _clicks(s3) == {} and rv.withdraw(s3, "SEN-ONE", LATER) == (200, {"sku": "SEN-ONE", "status": "no_click"})


def test_a_withdraw_older_than_the_click_is_422_and_one_while_a_run_holds_the_lock_is_409(monkeypatch):
    _hidden_in_ledger(monkeypatch, ONE)
    s3 = _bucket()
    rv.click(s3, "SEN-ONE", B, LATER)
    assert rv.withdraw(s3, "SEN-ONE", AT)[0] == 422

    @contextmanager
    def held(client, owner, on_lost=None):
        raise dec.proc.RunActive("held")
        yield
    monkeypatch.setattr(dec.proc, "exclusive", held)
    assert rv.withdraw(s3, "SEN-ONE", "2026-11-07T12:00:00.000Z")[1]["reason"] == "run_active"
    assert rv.withdraw(s3, "SEN-NOWHERE", LATER)[0] == 404


# --- the routes (config API) ----------------------------------------------------------------------------------------------
@pytest.fixture
def _served(monkeypatch):
    calls = []
    monkeypatch.setattr(rv, "client", lambda: "s3")
    monkeypatch.setattr(rv, "hidden", lambda s3, reason, offset, limit: calls.append(("hidden", s3)) or {"rows": []})
    monkeypatch.setattr(rv, "spend", lambda s3, month: calls.append(("spend", month)) or {"month": month})
    monkeypatch.setattr(rv, "click", lambda s3, sku, sha, at: calls.append(("click", sku, sha, at)) or (200, {}))
    monkeypatch.setattr(rv, "withdraw", lambda s3, sku, at: calls.append(("withdraw", sku, at)) or (200, {}))
    return calls


def test_the_photo_routes_reach_the_review_module_with_the_sku_decoded(_served):
    assert server.dispatch("GET", ["photos", "hidden"], None) == (200, {"rows": []})
    assert server.dispatch("PUT", ["photos", "clicks", "MAR-P%2F02%2F16"], {"sha": B, "at": AT})[0] == 200
    assert server.dispatch("DELETE", ["photos", "clicks", "MAR-P%2F02%2F16"], {"at": AT})[0] == 200
    assert server.dispatch("GET", ["photos", "spend"], None, "month=2026-10")[0] == 200
    assert _served == [("hidden", "s3"), ("click", "MAR-P/02/16", B, AT), ("withdraw", "MAR-P/02/16", AT),
                       ("spend", "2026-10")]


def test_the_spend_route_defaults_to_this_month_and_refuses_another_shape(_served):
    from datetime import datetime, timezone
    server.dispatch("GET", ["photos", "spend"], None)
    assert _served == [("spend", datetime.now(timezone.utc).strftime("%Y-%m"))]
    for bad in ("month=2026-13", "month=26-11", "month=2026-1", "month=x"):
        assert server.dispatch("GET", ["photos", "spend"], None, bad)[0] == 400


@pytest.mark.parametrize("body", [None, [], {"sha": B}, {"at": AT}, {"sha": "x", "at": AT}, {"sha": B, "at": "today"},
                                  {"sha": B, "at": AT, "more": 1}])
def test_a_click_with_a_bad_body_is_400_and_reaches_nothing(_served, body):
    assert server.dispatch("PUT", ["photos", "clicks", "SEN-ONE"], body)[0] == 400 and _served == []


def test_a_withdraw_with_a_bad_body_is_400(_served):
    for body in (None, {}, {"at": "today"}, {"at": AT, "sha": B}):
        assert server.dispatch("DELETE", ["photos", "clicks", "SEN-ONE"], body)[0] == 400
    assert _served == []


def test_a_store_that_cannot_be_read_is_503_naming_it(monkeypatch):
    monkeypatch.setattr(rv, "client", lambda: "s3")
    monkeypatch.setattr(rv, "hidden", lambda s3, reason, offset, limit: (_ for _ in ()).throw(
        rv.Unavailable("the ledger cannot be read")))
    assert server.dispatch("GET", ["photos", "hidden"], None) == (503, {"error": "the ledger cannot be read"})


# --- POST /photos/decisions?dry_run= (the page's export, through the one writer) ------------------------------------
def test_a_dry_run_of_the_export_returns_its_plan_and_writes_nothing():
    s3 = _bucket()
    docs = [{"id": "one", "action": "use", "source": "sentas", "sha": A, "tag": C.RUN_TAG, "at": AT},
            {"id": "two", "action": "remove", "source": "sentas", "at": AT}]
    code, body = rv.apply_decisions(s3, docs, dry_run=True)
    assert code == 200 and s3.written == []
    assert {k: body[k] for k in ("dry_run", "applied", "owner_choices", "removals", "cleans", "replaced")} == {
        "dry_run": True, "applied": False, "owner_choices": 1, "removals": 1, "cleans": 0, "replaced": 0}
    assert any("SEN-TWO" in line for line in body["lines"]) and body["lines"][-1].startswith("==> 1 owner choices")


def test_the_export_applied_is_written_through_the_one_writer():
    s3 = _bucket()
    code, body = rv.apply_decisions(s3, [{"id": "one", "action": "clean", "source": "sentas", "sha": B, "at": AT}],
                                    dry_run=False)
    assert (code, body["applied"], body["cleans"]) == (200, True, 1)
    assert _clicks(s3) == {"SEN-ONE": pub.click_record("SEN-ONE", "one", "sentas", B, AT)}


def test_an_export_with_a_bad_decision_is_422_and_writes_nothing():
    s3 = _bucket()
    code, body = rv.apply_decisions(s3, [{"id": "nowhere", "action": "remove", "source": "sentas", "at": AT}],
                                    dry_run=False)
    assert code == 422 and "nowhere" in body["error"] and s3.written == []


def test_an_export_applied_while_a_photos_run_holds_the_lock_is_409_naming_the_run(monkeypatch):
    @contextmanager
    def held(client, owner, on_lost=None):
        raise dec.proc.RunActive("held")
        yield
    monkeypatch.setattr(dec.proc, "exclusive", held)
    s3 = _bucket(**{dec.proc.LOCK: json.dumps({"run_id": "R9"}).encode()})
    code, body = rv.apply_decisions(s3, [{"id": "two", "action": "remove", "source": "sentas", "at": AT}], dry_run=False)
    assert (code, body["reason"], body["run_id"]) == (409, "run_active", "R9") and s3.written == []


def test_the_decisions_route_takes_the_export_and_an_explicit_dry_run(monkeypatch):
    seen = []
    monkeypatch.setattr(rv, "client", lambda: "s3")
    monkeypatch.setattr(rv, "apply_decisions", lambda s3, docs, dry_run: seen.append((docs, dry_run)) or (200, {}))
    docs = [{"id": "two", "action": "remove", "source": "sentas", "at": AT}]
    assert server.dispatch("POST", ["photos", "decisions"], docs, "dry_run=true")[0] == 200
    assert server.dispatch("POST", ["photos", "decisions"], docs, "dry_run=false")[0] == 200
    assert seen == [(docs, True), (docs, False)]
    for query in ("", "dry_run=yes", "dry_run=1"):
        assert server.dispatch("POST", ["photos", "decisions"], docs, query)[0] == 400
    for body in (None, {}, [1], "x"):
        assert server.dispatch("POST", ["photos", "decisions"], body, "dry_run=true")[0] == 400
    assert len(seen) == 2


@pytest.mark.parametrize("bad", [{"id": ["x"]}, {"action": ["use"]}, {"sha": ["x"]}, {"at": 5}, {"source": None}])
def test_a_decision_whose_field_is_not_a_string_is_400_never_a_crash(monkeypatch, bad):
    # review of #556: a list in id, action or sha reached the conflict rule unhashed -> 500
    monkeypatch.setattr(rv, "client", lambda: _bucket())
    doc = {**{"id": "one", "action": "use", "source": "sentas", "sha": A, "tag": C.RUN_TAG, "at": AT}, **bad}
    code, body = server.dispatch("POST", ["photos", "decisions"], [doc], "dry_run=true")
    assert code == 400 and "string" in body["error"]
    with pytest.raises(dec.InvalidDecisions, match="string"):
        dec.read_decisions(json.dumps([doc]))


def test_the_export_is_planned_in_full_never_narrow(monkeypatch):
    # manager 2026-11-07: narrow reads only for a click's single decision; a file of decisions is read whole
    seen = []
    monkeypatch.setattr(rv.decisions, "decide", lambda s3, docs, dry_run, say, sheet, narrow: seen.append((sheet, narrow))
                        or dec.plan(s3, docs, PHOTO_LISTINGS, set()))
    rv.apply_decisions(_bucket(), [{"id": "two", "action": "remove", "source": "sentas", "at": AT}], dry_run=True)
    assert seen == [(None, False)]


# --- the sheet is read again only when it changed (dev: 8.7 MB, read on every request) ------------------------------
class _Sheets:
    """The sheet and its photo list as ETagged objects; every read and look counted."""

    def __init__(self):
        self.etags, self.reads = {"sheet": '"s1"', "photos": '"p1"'}, []

    def head_object(self, Bucket, Key):
        return {"ETag": self.etags["sheet" if Key.endswith(C.SHEET_KEY) else "photos"]}


def test_the_sheet_is_parsed_again_only_when_it_its_photo_list_or_the_categories_changed(monkeypatch):
    s3, raw = _Sheets(), []
    monkeypatch.setattr(rv, "sheet", _REAL_SHEET)              # the real one back, every other stand-in kept (conftest's too)
    monkeypatch.setattr(rv, "_SHEET", {})
    monkeypatch.setattr(rv, "fetch_attributes", lambda client: True)
    monkeypatch.setattr(rv, "sheet_and_photos", lambda client: raw.append(1) or ("csv", {}))
    monkeypatch.setattr(rv.proc, "photo_listings", lambda sheet, photos: [ONE])
    monkeypatch.setattr(rv.proc, "tile_listings", lambda sheet, photos: [TILE])
    categories = [("pcat_slab", "Slab")]
    monkeypatch.setattr(rv, "_categories", lambda: tuple(categories))
    assert rv.sheet(s3) == rv.sheet(s3) == ([ONE], [TILE]) and len(raw) == 1
    s3.etags["photos"] = '"p2"'
    rv.sheet(s3)
    categories.append(("pcat_tile", "Tile"))
    rv.sheet(s3)
    s3.etags["sheet"] = '"s2"'
    rv.sheet(s3)
    assert len(raw) == 4


# --- the query and body edges (review of #555 and #556) ------------------------------------------------------------------
@pytest.mark.parametrize("query", ["limit=²", "limit=١", "offset=١", f"offset={'9' * 5000}", "limit= 5",
                                   "offset=+1"])
def test_a_page_number_that_is_not_plain_ascii_digits_is_400_never_a_crash(monkeypatch, query):
    monkeypatch.setattr(rv, "client", lambda: "s3")
    monkeypatch.setattr(rv, "hidden", lambda *a: pytest.fail("reached"))
    code, body = server.dispatch("GET", ["photos", "hidden"], None, query)
    assert code == 400 and query.split("=")[0] in body["error"]


@pytest.mark.parametrize("query", ["month=", "month=2026-11%0A", "month=٢٠٢٦-11",
                                   "month=2026-10&month=2026-11"])
def test_a_month_that_is_empty_not_ascii_or_repeated_is_400(monkeypatch, query):
    monkeypatch.setattr(rv, "client", lambda: "s3")
    monkeypatch.setattr(rv, "spend", lambda *a: pytest.fail("reached"))
    assert server.dispatch("GET", ["photos", "spend"], None, query)[0] == 400


def test_a_repeated_dry_run_is_400_never_the_last_value(monkeypatch):
    monkeypatch.setattr(rv, "client", lambda: "s3")
    monkeypatch.setattr(rv, "apply_decisions", lambda *a: pytest.fail("reached"))
    code, body = server.dispatch("POST", ["photos", "decisions"], [], "dry_run=true&dry_run=false")
    assert code == 400 and "dry_run" in body["error"]


@pytest.mark.parametrize("body", [{"sha": B + "\n", "at": AT}, {"sha": B, "at": AT + "\n"},
                                  {"sha": B, "at": "٢026-11-07T10:00:00.000Z"}])
def test_a_sha_or_time_with_a_trailing_newline_or_a_non_ascii_digit_is_400(monkeypatch, body):
    monkeypatch.setattr(rv, "client", lambda: "s3")
    monkeypatch.setattr(rv, "click", lambda *a: pytest.fail("reached"))
    assert server.dispatch("PUT", ["photos", "clicks", "SEN-ONE"], body)[0] == 400


def test_a_candidate_decision_is_checked_at_the_millisecond_the_page_would_write(monkeypatch):
    # review of #556: _now() floored to the second ('.000Z'), so an action could be withheld for up to a second
    from datetime import datetime, timezone

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 7, 14, 5, 9, 123456, tzinfo=timezone.utc)
    monkeypatch.setattr(rv, "datetime", _Clock)
    assert rv._now() == "2026-11-07T14:05:09.123Z" and dec.moment(rv._now()) is not None


def test_a_row_names_its_listing_by_the_sheet_s_handle_not_the_one_stored_with_the_run(monkeypatch):
    # review of #556: a variety re-bind moves the handle; review.json kept the old one until the next full run
    _hidden_in_ledger(monkeypatch, ONE)
    s3 = _bucket(**{REVIEW: json.dumps({"SEN-ONE": {**_row(ONE), "listing": "old-handle"}}).encode()})
    row = rv.hidden(s3)["rows"][0]
    assert row["listing"] == "one" and all(d["id"] == "one" for d in _offered(row))


def test_a_removed_row_before_any_conflict_set_is_503_never_a_crash(monkeypatch):
    _hidden_in_ledger(monkeypatch, ONE)
    s3 = _bucket(**{imagestore.owner_removed_key("SEN-ONE"): json.dumps(
        {"sku": "SEN-ONE", "listing": "one", "source": "sentas", "reason": C.REMOVED_REASON, "at": AT}).encode()})
    del s3.objects[imagestore.conflicts_key("sentas")]
    with pytest.raises(rv.Unavailable, match="no conflict set"):
        rv.hidden(s3)


def test_a_click_before_any_conflict_set_is_503_never_a_crash(monkeypatch):
    _hidden_in_ledger(monkeypatch, ONE)
    s3 = _bucket()
    del s3.objects[imagestore.conflicts_key("sentas")]
    with pytest.raises(rv.Unavailable, match="no conflict set"):
        rv.click(s3, "SEN-ONE", B, AT)
    assert s3.written == []


# --- the open #555 follow-ups ---------------------------------------------------------------------------------------------
def test_a_click_on_a_tile_is_refused_by_the_applier_before_anything_else(monkeypatch):
    # review of #555: a hidden tile with a choice record answered 409 chosen_not_linked instead of the promised 422
    _hidden_in_ledger(monkeypatch, TILE)
    s3 = _bucket(**{f"{ROOT}chosen/tile-1.json": json.dumps({"key": "tile-1", "sha": A}).encode()})
    code, body = rv.click(s3, "SEN-TILE-1", B, AT)
    assert code == 422 and "not on the product sheet as a photo listing" in body["error"] and s3.written == []


@pytest.mark.parametrize("change", ["excluded", "pipeline_off"])
def test_a_stale_run_row_never_offers_what_the_applier_now_refuses(monkeypatch, change):
    # review of #555: the row says fixable from the last run; the exclude list or the source's switch changed since
    _hidden_in_ledger(monkeypatch, ONE)
    if change == "excluded":
        monkeypatch.setattr(dec, "excluded", lambda: {B})
    else:
        monkeypatch.setattr(dec, "image_pipeline_off", lambda: {"sentas"})
    s3 = _bucket(**{REVIEW: json.dumps({"SEN-ONE": _row(ONE, photos=[_photo(A, fixable=False, left=()), _photo(B)])}).encode()})
    row = rv.hidden(s3)["rows"][0]
    assert not row["photos"][1]["clickable"] and "clean" not in row["actions"]
    for doc in _offered(row):
        dec.plan(s3, [doc], PHOTO_LISTINGS, exclude=dec.excluded())               # what is offered still applies


def test_a_spend_file_of_the_wrong_shape_is_listed_unreadable_never_a_crash():
    month = f"{ROOT[:-len(C.CLEAN_TAG) - 1]}{C.CLEAN_SPEND_DIR}/2026-11/"
    good = {"at": AT, "source": "sentas", "sha": A, "model": "nano2", "call": "edit", "usd": 0.12}
    s3 = _S3({f"{month}R1.json": json.dumps({"usd": 0.12, "uses": [good]}).encode(),
              f"{month}R2.json": json.dumps({"usd": 1.0, "uses": "all of them"}).encode(),
              f"{month}R3.json": json.dumps({"usd": 1.0, "uses": [{"sha": A}]}).encode()})
    out = rv.spend(s3, "2026-11")["cleaning"]
    assert out["uses"] == [{"run_id": "R1", **good}] and out["spent_usd"] == 0.12
    assert [u["run_id"] for u in out["unreadable"]] == ["R2", "R3"]
