"""The standardised-photo process (STANDARDISE_PROCESS.md), with AWS stand-ins: it picks exactly the listings without a
valid chosen photo, continues in the tags the published results came from, keeps one hosted budget per month across
runs, publishes only what changed, links the new photos in the same run only when the published folder changed, and runs
after a produce on the owner's button as the runner's photos stage."""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys

import pytest
from botocore.exceptions import ClientError

from deploy import standardise_close as close
from deploy import standardise_publish as pub
from deploy import standardise_run as proc
from deploy import standardise_select as select
from stone_pipeline.config import runner
from stone_pipeline.config.settings import ENV_SEGMENT, STANDARDISE as C
from stone_pipeline.io import imagestore
from stone_pipeline.ledger import writethrough

A, B, D, E = "a" * 64, "b" * 64, "d" * 64, "e" * 64
RUN = f"{ENV_SEGMENT}/standardise-test/{C.RUN_TAG}/sentas/"
ROOT = f"{ENV_SEGMENT}/standardise-test/{C.CLEAN_TAG}/"
NO_TILES = "Product Handle,Product Category 1\n"            # a product sheet without tile listings


class _S3:
    """A bucket in a dict: ETags as S3 gives a single-part object (the md5 of its bytes, kept by a server-side copy),
    listing, and every write recorded."""

    def __init__(self, objects=None):
        self.objects, self.written = {}, []
        for key, body in (objects or {}).items():
            self._store(key, body)

    def _store(self, key, body, metadata=None):
        self.objects[key] = {"Body": body, "ETag": f'"{hashlib.md5(body).hexdigest()}"', "Metadata": metadata or {}}

    def put_object(self, Bucket, Key, Body, ContentType=None, Metadata=None, **condition):
        self.written.append(Key)
        self._store(Key, Body, Metadata)
        return {"ETag": self.objects[Key]["ETag"]}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key]["Body"]), "ETag": self.objects[Key]["ETag"]}

    def head_object(self, Bucket, Key):
        return {"ETag": self.objects[Key]["ETag"], "Metadata": self.objects[Key]["Metadata"]}

    def copy_object(self, Bucket, Key, CopySource, ContentType, MetadataDirective):
        self.written.append(Key)
        self.objects[Key] = dict(self.objects[CopySource["Key"]], Metadata={})

    def delete_object(self, Bucket, Key, **condition):
        self.written.append(Key)
        self.objects.pop(Key, None)

    def list_objects_v2(self, Bucket, Prefix, MaxKeys):
        return {"KeyCount": min(MaxKeys, sum(k.startswith(Prefix) for k in self.objects))}

    def get_paginator(self, name):
        objects = self.objects

        class _Pages:
            def paginate(self, Bucket, Prefix, Delimiter=None):
                keys = [k for k in sorted(objects) if k.startswith(Prefix)]
                if Delimiter:
                    folders = sorted({Prefix + k[len(Prefix):].split(Delimiter)[0] + Delimiter for k in keys
                                      if Delimiter in k[len(Prefix):]})
                    return [{"CommonPrefixes": [{"Prefix": f} for f in folders],
                             "Contents": [{"Key": k, "ETag": objects[k]["ETag"]} for k in keys if Delimiter not in k[len(Prefix):]]}]
                return [{"Contents": [{"Key": k, "ETag": objects[k]["ETag"]} for k in keys]}]
        return _Pages()


def _listing(key, *photos, fmt="slab", stone=("v1", "f1")):
    return {"key": key, "sku": f"SEN-{key.upper()}", "source": "sentas", "stone": stone, "format": fmt,
            "photos": list(photos)}


def _record(key, sha, prefix=RUN):
    return json.dumps({"source": "sentas", "key": key, "sha": sha, "tag": C.RUN_TAG, "prefix": prefix}).encode()


def _rec(sha):
    return {"sha": sha, "tag": C.RUN_TAG, "prefix": RUN}


# --- what a run works on ----------------------------------------------------------------------------------------------
def test_the_process_picks_exactly_the_listings_without_a_valid_chosen_photo():
    listings = [_listing("done", A, B), _listing("new", D), _listing("moved", E), _listing("excluded", B)]
    records = {"done": _rec(A), "moved": _rec(D), "excluded": _rec(B), "gone": _rec(E)}
    assert [l["key"] for l in proc.todo(listings, records, {B}, {f"{RUN}{A}.slab.png"}, removed=set(), conflicts={},
                                        picks={})] == ["new", "moved", "excluded"]


def test_a_slab_listing_whose_chosen_photo_has_no_3d_slab_is_picked_again_with_its_choice_kept():
    listings = [_listing("whole", A, B), _listing("close-up", D, E), _listing("block", E, fmt="block"),
                _listing("tile", E, fmt="tile")]
    records = {"whole": _rec(A), "close-up": _rec(D), "block": _rec(E), "tile": _rec(E)}
    found = proc.todo(listings, records, set(), {f"{RUN}{A}.slab.png"}, removed=set(), conflicts={}, picks={})
    assert found == [{**_listing("close-up", D, E), "kept": (C.RUN_TAG, D)}]          # a block or tile has no 3D slab


def _decided(exclude=(), conflicts=None, picks=None):
    return proc.Decisions(set(exclude), {}, {}, conflicts or {}, [], picks or {}, {}, {})


def test_what_the_live_listings_show_is_refreshed_and_nothing_else():
    """Each listing on the sheet's choice: its owner choice that applies, else its valid record; a listing that left the
    sheet, an excluded or a conflict photo chosen by itself, and a listing the owner removed are not among them."""
    listings = [_listing("on", A), _listing("new", D), _listing("excluded", B), _listing("owned", A, E),
                _listing("conflict", D), _listing("removed", E)]
    records = {"on": _rec(A), "gone": _rec(B), "excluded": _rec(B), "owned": _rec(A), "conflict": _rec(D), "removed": _rec(E)}
    decided = _decided(exclude={B}, conflicts={("sentas", D): ("conflict", "other")}, picks={"owned": (C.CLEAN_TAG, E)})
    assert proc.live_choices(listings, records, {"SEN-REMOVED"}, decided) == sorted(
        [(C.RUN_TAG, "sentas", A), (C.CLEAN_TAG, "sentas", E)])


def test_a_source_whose_image_pipeline_is_off_has_nothing_refreshed(monkeypatch):
    monkeypatch.setattr(proc, "image_pipeline_off", lambda: {"sentas"})
    assert proc.live_choices([_listing("on", A)], {"on": _rec(A)}, set(), _decided()) == []


def test_which_chosen_photos_have_a_3d_slab_is_read_from_the_runs_the_records_point_at():
    other = f"{ENV_SEGMENT}/standardise-test/{C.CLEAN_TAG}/sentas/"
    s3 = _S3({f"{RUN}{A}.slab.png": b"p", f"{RUN}{A}.main.jpg": b"m", f"{RUN}held/{B}.json": b"{}",
              f"{other}{B}.slab.png": b"p", f"{ENV_SEGMENT}/standardise-test/v1/sentas/{D}.slab.png": b"p"})
    records = {"one": {"sha": A, "prefix": RUN}, "two": {"sha": B, "prefix": other}}
    assert proc.whole_slabs(s3, records) == {f"{RUN}{A}.slab.png", f"{other}{B}.slab.png"}


def _owned(sha, tag=C.RUN_TAG, listing="mine"):
    return {"tag": tag, "sha": sha, "at": "2026-11-02T10:00:00Z", "listing": listing, "decided_on": listing}


def test_a_choice_the_owner_overruled_or_a_conflict_photo_chosen_by_itself_is_worked_again():
    listings = [_listing("redecided", A, B), _listing("agreed", A, B), _listing("conflict", D), _listing("assigned", D)]
    records = {k: _rec(sha) for k, sha in (("redecided", A), ("agreed", B), ("conflict", D), ("assigned", D))}
    picks = {"redecided": (C.RUN_TAG, B), "agreed": (C.RUN_TAG, B), "assigned": (C.RUN_TAG, D)}
    conflicts = {("sentas", D): ("assigned", "conflict")}
    found = proc.todo(listings, records, set(), set(), removed=set(), conflicts=conflicts, picks=picks)
    assert [l["key"] for l in found] == ["redecided", "conflict"]


def test_a_conflict_photo_is_assigned_to_the_one_listing_whose_valid_owner_choice_names_it():
    listings = [_listing("mine", D, A), _listing("partner", D, stone=("v2", "f1")), _listing("other", B),
                _listing("stale", B, stone=("v3", "f1"))]
    conflicts = {("sentas", D): ("mine", "partner"), ("sentas", B): ("other", "stale")}
    assigned, problems = proc.assignments(listings, {"mine": (C.RUN_TAG, D)}, conflicts)
    assert assigned == {("sentas", D): ["SEN-MINE"], ("sentas", B): []} and problems == []


def test_a_conflict_photo_two_listings_claim_is_linked_to_neither_and_said():
    listings = [_listing("mine", D), _listing("partner", D, stone=("v2", "f1"))]
    conflicts = {("sentas", D): ("mine", "partner")}
    assigned, problems = proc.assignments(listings, {"mine": (C.RUN_TAG, D), "partner": (C.RUN_TAG, D)}, conflicts)
    assert assigned == {("sentas", D): []}
    assert problems == [f"photo {D[:12]} ({C.CONFLICT_REASON}) is the owner's choice for mine, partner: linked to none "
                        "until one decision is withdrawn"]


def test_a_listing_the_owner_removed_is_never_worked_on_whatever_its_choice():
    listings = [_listing("new", D), _listing("removed", E), _listing("removed-chosen", A)]
    records = {"removed-chosen": _rec(A)}
    assert [l["key"] for l in proc.todo(listings, records, set(), set(), removed={"SEN-REMOVED", "SEN-REMOVED-CHOSEN"},
                                        conflicts={}, picks={})] == ["new"]


def test_the_owner_s_removals_are_read_by_sku_from_their_own_store():
    # prod SKUs carry slashes, spaces and plus signs (MAR-P/02/16): each still names exactly one record, in one folder
    skus = {"SEN-1", "MAR-P/02/16", "ZUC-A B+C.2"}
    s3 = _S3({**{imagestore.owner_removed_key(sku): b"{}" for sku in skus}, f"{ROOT}chosen/x.json": b"{}"})
    prefix = imagestore.owner_removed_prefix()
    assert prefix == f"{ENV_SEGMENT}/standardise-test/removed/" and proc.removed_listings(s3) == skus
    assert all("/" not in key[len(prefix):] for key in s3.objects if key.startswith(prefix))


def test_the_choice_records_are_read_from_the_clean_root():
    s3 = _S3({f"{ROOT}chosen/one.json": _record("one", A), f"{ROOT}tracked.csv": b"source\n", f"{ROOT}sentas/x.json": b"{}"})
    assert proc.choices(s3, ROOT) == {"one": json.loads(_record("one", A))}


def test_the_process_continues_in_the_tags_the_published_results_came_from():
    assert C.RUN_TAG == "v2" and C.CLEAN_TAGS == {"dev": "v2-clean", "prod": "v2-clean2"}
    assert C.CLEAN_TAG == C.CLEAN_TAGS[ENV_SEGMENT]


# --- one budget per month ---------------------------------------------------------------------------------------------
def _spent(s3, month, run_id, usd):
    s3._store(f"{proc.spend_prefix(C.SPEND_DIR, month)}{run_id}.json", json.dumps({"usd": usd}).encode())


def test_the_month_s_budget_counts_every_run_of_the_month(monkeypatch):
    monkeypatch.setattr(C, "BUDGET_USD", 1.0)
    s3 = _S3()
    _spent(s3, "2026-11", "20261101T000000Z", 0.5)
    _spent(s3, "2026-11", "20261101T060000Z", 0.4)
    spend = proc.month_spend(s3, "20261102T120000Z", C.BUDGET_USD, C.SPEND_DIR)
    with pytest.raises(select.SpendCapReached):
        spend.reserve(C.JUDGE_PRICE_USD)                                     # 0.90 + 0.14 would pass 1.00
    assert spend.capped


def test_a_new_month_has_its_whole_budget(monkeypatch):
    monkeypatch.setattr(C, "BUDGET_USD", 1.0)
    s3 = _S3()
    _spent(s3, "2026-10", "20261031T230000Z", 1.0)
    proc.month_spend(s3, "20261101T000100Z", C.BUDGET_USD, C.SPEND_DIR).reserve(0.5)


def test_each_run_keeps_only_its_own_spend_a_reserve_counting_until_it_is_settled(monkeypatch):
    monkeypatch.setattr(C, "BUDGET_USD", 10.0)
    run_id, other = "20261102T120000Z", "20261101T000000Z"
    s3 = _S3()
    _spent(s3, "2026-11", other, 2.0)
    spend = proc.month_spend(s3, run_id, C.BUDGET_USD, C.SPEND_DIR)
    own = f"{proc.spend_prefix(C.SPEND_DIR, '2026-11')}{run_id}.json"
    spend.reserve(0.14)
    assert json.loads(s3.objects[own]["Body"])["usd"] == pytest.approx(0.14)        # a run that dies now still counts it
    spend.settle(0.14, 0.02)
    assert json.loads(s3.objects[own]["Body"])["usd"] == pytest.approx(0.02)
    assert json.loads(s3.objects[f"{proc.spend_prefix(C.SPEND_DIR, '2026-11')}{other}.json"]["Body"])["usd"] == 2.0   # never rewritten


def test_the_month_is_summed_again_when_a_call_is_settled(monkeypatch):
    monkeypatch.setattr(C, "BUDGET_USD", 3.0)
    s3 = _S3()
    spend = proc.month_spend(s3, "20261102T120000Z", C.BUDGET_USD, C.SPEND_DIR)
    _spent(s3, "2026-11", "20261102T110000Z", 2.9)                           # written by another run since
    spend.reserve(0.05)
    spend.settle(0.05, 0.05)
    assert spend.before == pytest.approx(2.9)
    with pytest.raises(select.SpendCapReached):
        spend.reserve(0.1)


def test_two_budgets_keep_their_runs_apart():
    # The process's budget and any other monthly budget (the cleaning cap) each sum only their own directory's runs.
    s3 = _S3()
    _spent(s3, "2026-11", "20261101T000000Z", 0.9)
    other = proc.month_spend(s3, "20261102T120000Z", 1.0, "another-spend")
    assert other.before == 0.0
    other.reserve(0.5)
    assert proc.month_spend(s3, "20261102T130000Z", 1.0, C.SPEND_DIR).before == pytest.approx(0.9)
    assert f"{proc.spend_prefix('another-spend', '2026-11')}20261102T120000Z.json" in s3.objects


def test_each_run_s_spend_file_lists_its_paid_calls():
    s3 = _S3()
    spend = proc.month_spend(s3, "20261102T120000Z", 10.0, C.CLEAN_SPEND_DIR)
    spend.reserve(0.5)
    spend.settle(0.5, 0.12)
    spend.record({"call": "edit", "usd": 0.12})
    stored = json.loads(s3.objects[f"{proc.spend_prefix(C.CLEAN_SPEND_DIR, '2026-11')}20261102T120000Z.json"]["Body"])
    assert stored["usd"] == pytest.approx(0.12) and [u["call"] for u in stored["uses"]] == ["edit"]


def test_without_a_cleaning_cap_no_edit_can_be_paid_and_the_run_says_so_once(monkeypatch, capsys):
    monkeypatch.setattr(C, "CLEAN_BUDGET_USD", None)
    assert proc.cleaning_cap(_S3(), "20261102T120000Z", close_only=False) is None
    assert capsys.readouterr().out.count("SCRAPER_STANDARDISE_CLEAN_BUDGET_USD unset") == 1


def test_the_cleaning_cap_is_its_own_monthly_budget_and_a_close_only_run_has_none(monkeypatch):
    monkeypatch.setattr(C, "CLEAN_BUDGET_USD", 30.0)
    s3 = _S3()
    _spent(s3, "2026-11", "20261101T000000Z", 99.0)                      # the process budget's runs never count here
    cap = proc.cleaning_cap(s3, "20261102T120000Z", close_only=False)
    assert cap.budget == 30.0 and cap.before == 0.0
    assert cap.cap_error is select.CleanCapReached                         # reaching it ends only the clean step
    with pytest.raises(select.CleanCapReached):
        cap.reserve(31.0)
    assert proc.cleaning_cap(s3, "20261102T120000Z", close_only=True) is None


def test_the_summary_carries_the_cleaning_cap_s_spend():
    cap = select.Spend(30.0, others=lambda: 4.0)
    cap.reserve(0.26)
    cap.settle(0.26, 0.14)
    cap.record({"call": "edit", "usd": 0.12})
    assert proc.clean_summary(cap) == {"clean_run_spent_usd": 0.14, "clean_month_spent_usd": 4.14, "clean_budget_usd": 30.0,
                                       "clean_budget_reached": False, "clean_uses": 1}
    assert set(proc.clean_summary(None).values()) == {None}


def test_a_click_is_written_in_the_one_shape_every_run_reads():
    root = f"{proc.ROOT}{C.CLEAN_TAG}/"
    s3 = _S3()
    pub.write_click(s3, root, "MAR-P/06/22", "slab-1", "marenostone", A, "t")
    assert pub.read_clicks(s3, root) == {"MAR-P/06/22": {"sku": "MAR-P/06/22", "listing": "slab-1",
                                                        "source": "marenostone", "sha": A, "at": "t"}}


def test_an_unreadable_click_or_choice_record_fails_only_its_own_listing(capsys):
    root = f"{proc.ROOT}{C.CLEAN_TAG}/"
    s3 = _S3({f"{pub.clicks_prefix(root)}{imagestore.sku_file('SEN-BAD')}": b"{not json",
              f"{pub.owner_prefix(root)}{imagestore.sku_file('SEN-BAD')}": b"[1, 2]"})
    pub.write_click(s3, root, "SEN-OK", "ok", "sentas", A, "t")
    clicks = pub.read_clicks(s3, root)
    assert clicks["SEN-OK"]["sha"] == A and "not JSON" in clicks["SEN-BAD"]["error"]
    owner, unreadable = proc.owner_records(s3, root)
    assert owner == {} and unreadable == {"SEN-BAD": "not a JSON object"}
    assert capsys.readouterr().out.count("SEN-BAD") == 2                              # each named loud with its key


@pytest.mark.parametrize("close_only", [False, True])
def test_a_listing_whose_choice_record_cannot_be_read_stays_exactly_as_published(monkeypatch, close_only):
    # review of #525: its conflict photo dropped out of the assignments, so publish unpublished it and removed the file
    import time
    s3 = _S3({f"{RUN}{D}.main.jpg": b"m", f"{RUN}{D}.white.jpg": b"w", f"{ROOT}chosen/mine.json": _record("mine", D),
              imagestore.conflicts_key("sentas"): json.dumps({D: ["mine", "partner"]}).encode()})
    pub.publish(s3, ROOT, NO_TILES, {}, say=lambda m: None, assigned={("sentas", D): ["SEN-MINE"]})   # as published
    s3._store(pub.owner_key(ROOT, "SEN-MINE"), b"{broken")
    published = {k: v for k, v in s3.objects.items() if k.startswith(("dev/products/", "prod/products/"))}
    assert imagestore.assigned_key("sentas", D) in published
    monkeypatch.setattr(proc, "sheet_and_photos", lambda client: (NO_TILES, {}))
    monkeypatch.setattr(proc, "photo_listings", lambda sheet, photos: [_listing("mine", D), _listing("partner", D)])
    monkeypatch.setattr(proc.gpu, "clear_leftovers", lambda batch, run_id: [])
    monkeypatch.setattr(proc, "run", lambda batch, sel, spend, clean, run_id, deadline, say=print: (
        [], {"chosen": [], "tracked": [], "pending": [], "click_problems": []}))
    proc.process(s3, None, select.Spend(5.0), None, "R1", time.monotonic() + 3600, say=lambda m: None,
                 close_only=close_only)
    assert {k: v for k, v in s3.objects.items() if k.startswith(("dev/products/", "prod/products/"))} == published
    assert json.loads(s3.objects[f"{ROOT}chosen/mine.json"]["Body"])["sha"] == D
    summary = json.loads(s3.objects[f"{ROOT}{'close-summary' if close_only else 'summary'}.json"]["Body"])
    assert any(p.startswith("mine: the owner's choice record cannot be read") for p in summary["problem_list"])


def test_a_listing_whose_choice_record_cannot_be_read_is_left_as_it_is_and_named(monkeypatch):
    s3 = _S3({pub.owner_key(ROOT, "SEN-MINE"): b"{broken", imagestore.conflicts_key("sentas"): b"{}"})
    monkeypatch.setattr(proc, "sheet_and_photos", lambda client: (NO_TILES, {}))
    monkeypatch.setattr(proc, "photo_listings", lambda sheet, photos: [_listing("mine", D)])
    monkeypatch.setattr(proc.gpu, "clear_leftovers", lambda batch, run_id: [])
    worked = []

    def run(batch, sel, spend, clean, run_id, deadline, say=print):
        worked.extend(l["key"] for l in sel.listings)
        return [], {"chosen": [], "tracked": [], "pending": [], "click_problems": []}
    monkeypatch.setattr(proc, "run", run)
    monkeypatch.setattr(proc.publish, "publish", lambda s3, root, sheet, photos, say, assigned: {"changed": False})
    import time
    proc.process(s3, None, select.Spend(5.0), None, "R1", time.monotonic() + 3600, say=lambda m: None)
    stored = json.loads(s3.objects[f"{ROOT}summary.json"]["Body"])
    assert worked == [] and any("mine" in p and "cannot be read" in p for p in stored["problem_list"])


def _configured(monkeypatch):
    for name, value in (("SCRAPER_GPU_QUEUE", "q"), ("SCRAPER_STANDARDISE_JOBDEF", "jd"), ("FAL_KEY", "k")):
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(C, "BUDGET_USD", 50.0)
    monkeypatch.setattr(proc, "RUN_TIMEOUT_SECONDS", C.WORK_SECONDS + C.FINISH_SECONDS + 1)


def test_without_a_budget_the_process_refuses_to_start(monkeypatch):
    _configured(monkeypatch)
    monkeypatch.setattr(C, "BUDGET_USD", None)
    assert proc.unconfigured() == ["SCRAPER_STANDARDISE_BUDGET_USD"]
    assert proc.main() == 2


def test_the_gpu_target_and_the_hosted_key_must_be_configured(monkeypatch):
    _configured(monkeypatch)
    for name in ("SCRAPER_GPU_QUEUE", "BLOKPORT_GPU_QUEUE", "SCRAPER_STANDARDISE_JOBDEF", "BLOKPORT_STANDARDISE_JOBDEF", "FAL_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert proc.unconfigured() == ["SCRAPER_GPU_QUEUE", "SCRAPER_STANDARDISE_JOBDEF", "FAL_KEY"]


def test_the_work_window_and_the_finish_must_fit_the_runner_s_ceiling(monkeypatch):
    _configured(monkeypatch)
    assert proc.unconfigured() == []
    monkeypatch.setattr(proc, "RUN_TIMEOUT_SECONDS", C.WORK_SECONDS + C.FINISH_SECONDS)
    assert proc.unconfigured() == [f"SCRAPER_STANDARDISE_WORK_SECONDS ({C.WORK_SECONDS}) + FINISH_SECONDS ({C.FINISH_SECONDS}) "
                                   f"under SCRAPER_RUN_TIMEOUT_SECONDS ({C.WORK_SECONDS + C.FINISH_SECONDS})"]


def test_the_work_window_must_leave_the_gpu_rounds_their_minimum_after_the_judge_s_share(monkeypatch):
    _configured(monkeypatch)
    monkeypatch.setattr(C, "WORK_SECONDS", C.JUDGE_WINDOW_SECONDS + C.GPU_MIN_WINDOW_SECONDS - 1)
    assert proc.unconfigured() == [f"SCRAPER_STANDARDISE_WORK_SECONDS ({C.WORK_SECONDS}) at least JUDGE_WINDOW_SECONDS "
                                   f"({C.JUDGE_WINDOW_SECONDS}) + GPU_MIN_WINDOW_SECONDS ({C.GPU_MIN_WINDOW_SECONDS})"]


def test_a_close_only_run_has_no_judge_so_no_judge_share_is_asked_of_its_window(monkeypatch):
    _configured(monkeypatch)
    monkeypatch.setattr(C, "WORK_SECONDS", C.GPU_MIN_WINDOW_SECONDS)
    assert proc.unconfigured(close_only=True) == []


# --- publish: only what changed ---------------------------------------------------------------------------------------
def _published_bucket():
    return _S3({f"{RUN}{A}.main.jpg": b"m", f"{RUN}{A}.white.jpg": b"w", f"{ROOT}chosen/one.json": _record("one", A)})


def test_the_stored_conflict_sets_of_every_source_are_read_as_one():
    s3 = _S3({imagestore.conflicts_key("sentas"): json.dumps({D: ["mine", "partner"]}).encode(),
              imagestore.conflicts_key("zucchi"): b"{}"})
    assert pub.read_conflicts(s3, ["sentas", "zucchi"]) == {("sentas", D): ("mine", "partner")}


def test_a_source_without_a_stored_conflict_set_is_refused_never_read_as_no_conflicts():
    s3 = _S3({imagestore.conflicts_key("sentas"): b"{}"})
    with pytest.raises(pub.NoConflictSet, match="no conflict set yet for zucchi: run a produce first"):
        pub.read_conflicts(s3, ["sentas", "zucchi"])


def test_the_photo_run_refuses_to_start_without_the_conflict_set(monkeypatch, capsys):
    monkeypatch.setattr(proc, "process", lambda *a, **k: (_ for _ in ()).throw(pub.NoConflictSet("no conflict set yet for "
                                                                                                  "sentas: run a produce first")))
    rc, relinks = _main(monkeypatch, keep_process=True)
    assert rc == 2 and relinks == [] and "no conflict set yet for sentas" in capsys.readouterr().out


def test_publish_copies_only_what_changed_and_says_whether_the_folder_changed():
    s3 = _published_bucket()
    first = pub.publish(s3, ROOT, NO_TILES, {}, say=lambda m: None, assigned={})
    assert first["copied"] == 2 and first["changed"]
    assert s3.objects[imagestore.standard_key("sentas", A, "main")]["Body"] == b"m"
    s3.written.clear()
    second = pub.publish(s3, ROOT, NO_TILES, {}, say=lambda m: None, assigned={})
    assert not second["changed"] and s3.written == []                      # nothing rewritten: no new versions, no re-serve


def test_a_view_its_run_made_again_is_published_again():
    s3 = _published_bucket()
    pub.publish(s3, ROOT, NO_TILES, {}, say=lambda m: None, assigned={})
    s3._store(f"{RUN}{A}.main.jpg", b"m2")
    again = pub.publish(s3, ROOT, NO_TILES, {}, say=lambda m: None, assigned={})
    assert again["copied"] == 1 and again["changed"]
    assert s3.objects[imagestore.standard_key("sentas", A, "main")]["Body"] == b"m2"


def test_a_view_no_longer_chosen_is_removed_and_that_is_a_change():
    s3 = _published_bucket()
    s3._store(imagestore.standard_key("sentas", B, "main"), b"old")
    out = pub.publish(s3, ROOT, NO_TILES, {}, say=lambda m: None, assigned={})
    assert out["removed"] == 1 and imagestore.standard_key("sentas", B, "main") not in s3.objects


def test_3d_slabs_are_cut_out_a_few_at_a_time_on_the_small_service(monkeypatch):
    s3 = _published_bucket()
    s3._store(f"{RUN}{A}.slab.png", b"png")
    pools = []
    real = pub.ThreadPoolExecutor
    monkeypatch.setattr(pub, "ThreadPoolExecutor", lambda n: pools.append(n) or real(n))
    monkeypatch.setattr(pub, "_publish_cutout", lambda client, s, key, etag: True)
    assert pub.publish(s3, ROOT, NO_TILES, {}, say=lambda m: None, assigned={})["made_3d"] == 1
    assert C.CUTOUT_WORKERS in pools and C.CUTOUT_WORKERS <= 4


def _slab_png(corner=False):
    """A whole slab's PNG; corner: its top-right corner transparent (a rounded corner shown as it is)."""
    import cv2
    import numpy as np
    alpha = np.full((40, 60), 255, np.uint8)
    if corner:
        alpha[:10, 50:] = 0
    ok, buf = cv2.imencode(".png", np.dstack([np.full((40, 60, 3), 120, np.uint8), alpha]))
    assert ok
    return buf.tobytes()


def test_a_choice_moved_from_a_close_up_to_a_whole_slab_publishes_its_views_and_its_3d_slab_and_drops_the_old(monkeypatch):
    s3 = _S3({f"{RUN}{A}.main.jpg": b"ma", f"{RUN}{A}.white.jpg": b"wa", f"{ROOT}chosen/one.json": _record("one", A),
              **{f"{RUN}{B}.{v}.jpg": f"{v}b".encode() for v in imagestore.STANDARD_VIEWS}, f"{RUN}{B}.slab.png": _slab_png()})
    first = pub.publish(s3, ROOT, NO_TILES, {}, say=lambda m: None, assigned={})
    assert first["copied"] == 2 and first["made_3d"] == 0                              # the close-up: two views, no 3D
    s3._store(f"{ROOT}chosen/one.json", _record("one", B))
    moved = pub.publish(s3, ROOT, NO_TILES, {}, say=lambda m: None, assigned={})
    assert moved["changed"] and moved["copied"] == 4 and moved["made_3d"] == 1 and moved["removed"] == 2
    assert {k for k in s3.objects if "/products/standard/" in k} == (
        {imagestore.standard_key("sentas", B, v) for v in imagestore.STANDARD_VIEWS}
        | {imagestore.cutout_key("sentas", B, ext) for ext in ("webp", "json")})


def test_a_conflict_photo_is_published_only_when_the_owner_assigned_it_and_says_to_whom(monkeypatch):
    s3 = _published_bucket()
    out = pub.publish(s3, ROOT, NO_TILES, {}, say=lambda m: None, assigned={("sentas", A): []})
    assert imagestore.standard_key("sentas", A, "main") not in s3.objects and out["listings"] == 0
    out = pub.publish(s3, ROOT, NO_TILES, {}, say=lambda m: None, assigned={("sentas", A): ["SEN-MINE"]})
    assert imagestore.standard_key("sentas", A, "main") in s3.objects and out["changed"]
    assert json.loads(s3.objects[imagestore.assigned_key("sentas", A)]["Body"]) == ["SEN-MINE"]
    s3.written.clear()
    assert not pub.publish(s3, ROOT, NO_TILES, {}, say=lambda m: None, assigned={("sentas", A): ["SEN-MINE"]})["changed"]
    assert s3.written == []
    out = pub.publish(s3, ROOT, NO_TILES, {}, say=lambda m: None, assigned={})                 # no longer a conflict
    assert imagestore.assigned_key("sentas", A) not in s3.objects and out["changed"]


class _Crash(Exception):
    pass


def test_a_conflict_photo_s_assignment_is_written_before_its_views_so_a_stop_between_links_nobody_else(monkeypatch):
    s3 = _published_bucket()
    copy = s3.copy_object
    monkeypatch.setattr(s3, "copy_object", lambda **kw: (_ for _ in ()).throw(_Crash("stopped mid-publish")))
    with pytest.raises(_Crash):
        pub.publish(s3, ROOT, NO_TILES, {}, say=lambda m: None, assigned={("sentas", A): ["SEN-MINE"]})
    assert json.loads(s3.objects[imagestore.assigned_key("sentas", A)]["Body"]) == ["SEN-MINE"]
    assert imagestore.standard_key("sentas", A, "main") not in s3.objects                # no view without its file
    monkeypatch.setattr(s3, "copy_object", copy)
    pub.publish(s3, ROOT, NO_TILES, {}, say=lambda m: None, assigned={("sentas", A): ["SEN-MINE"]})
    assert imagestore.standard_key("sentas", A, "main") in s3.objects


def test_a_stale_assignment_is_removed_only_after_the_views_it_covers(monkeypatch):
    s3 = _published_bucket()
    pub.publish(s3, ROOT, NO_TILES, {}, say=lambda m: None, assigned={("sentas", A): ["SEN-MINE"]})
    s3.objects.pop(f"{ROOT}chosen/one.json")
    s3._store(f"{ROOT}chosen/two.json", _record("two", B))
    s3._store(f"{RUN}{B}.main.jpg", b"m")
    delete = s3.delete_object

    def crash_on_views(Bucket, Key, **condition):
        if "/standard/main/" in Key:
            raise _Crash("stopped mid-publish")
        return delete(Bucket=Bucket, Key=Key, **condition)
    monkeypatch.setattr(s3, "delete_object", crash_on_views)
    with pytest.raises(_Crash):
        pub.publish(s3, ROOT, NO_TILES, {}, say=lambda m: None, assigned={("sentas", A): []})
    assert imagestore.standard_key("sentas", A, "main") in s3.objects                    # the view is still there ...
    assert imagestore.assigned_key("sentas", A) in s3.objects                            # ... and still only its owner's


def test_publish_without_any_choice_record_is_refused_with_its_own_error():
    with pytest.raises(pub.NoChoiceRecords):
        pub.publish(_S3({f"{RUN}{A}.main.jpg": b"m"}), ROOT, NO_TILES, {}, say=lambda m: None, assigned={})


# --- the process end to end -------------------------------------------------------------------------------------------
def test_the_process_works_the_picked_listings_then_publishes_and_writes_its_summary(monkeypatch):
    s3 = _S3({f"{ROOT}chosen/done.json": _record("done", A), f"{RUN}{A}.main.jpg": b"m", f"{RUN}{A}.slab.png": _slab_png(),
              imagestore.owner_removed_key("SEN-GONE"): b"{}", imagestore.conflicts_key("sentas"): b"{}"})
    monkeypatch.setattr(proc, "sheet_and_photos", lambda client: (NO_TILES, {}))
    monkeypatch.setattr(proc, "photo_listings", lambda sheet, photos: [_listing("done", A), _listing("new", D),
                                                                       _listing("gone", E)])
    cleared, worked = [], []
    monkeypatch.setattr(proc.gpu, "clear_leftovers", lambda batch, run_id: cleared.append(run_id) or [])

    def run(batch, sel, spend, clean, run_id, deadline, say=print):
        worked.extend(l["key"] for l in sel.listings)
        return ["sentas d: no result after the GPU round"], {"chosen": [], "tracked": [], "pending": ["new"], "click_problems": []}
    monkeypatch.setattr(proc, "run", run)
    import time
    summary = proc.process(s3, None, select.Spend(5.0, others=lambda: 1.0), None, "R1", time.monotonic() + 3600, say=lambda m: None)
    assert cleared == ["R1"] and worked == ["new"]                       # an earlier run's jobs are stopped first
    assert summary["listings"] == 1 and summary["pending"] == 1 and summary["problems"] == 1 and summary["removed"] == 1
    assert summary["published"]["changed"] and summary["month_spent_usd"] == 1.0
    stored = json.loads(s3.objects[f"{ROOT}summary.json"]["Body"])
    assert stored["pending_listings"] == ["new"] and stored["problem_list"] == ["sentas d: no result after the GPU round"]


def test_the_run_hands_the_live_listings_choices_to_the_gpu_so_an_older_outline_is_rechecked(monkeypatch):
    s3 = _S3({f"{ROOT}chosen/done.json": _record("done", A), f"{RUN}{A}.main.jpg": b"m", f"{RUN}{A}.slab.png": _slab_png(),
              f"{ROOT}chosen/gone.json": _record("gone", B), f"{RUN}{B}.main.jpg": b"m", f"{RUN}{B}.slab.png": _slab_png(),
              imagestore.conflicts_key("sentas"): b"{}"})
    monkeypatch.setattr(proc, "sheet_and_photos", lambda client: (NO_TILES, {}))
    monkeypatch.setattr(proc, "photo_listings", lambda sheet, photos: [_listing("done", A)])     # "gone" left the sheet
    monkeypatch.setattr(proc.gpu, "clear_leftovers", lambda batch, run_id: [])
    seen = {}

    def run(batch, sel, spend, clean, run_id, deadline, say=print):
        seen.update(listings=sel.listings, todo=sel.gpu_todo())
        return [], {"chosen": [], "tracked": [], "pending": [], "click_problems": []}
    monkeypatch.setattr(proc, "run", run)
    import time
    proc.process(s3, None, select.Spend(5.0), None, "R1", time.monotonic() + 3600, say=lambda m: None)
    assert seen == {"listings": [], "todo": {"sentas": [A]}}         # its listing not picked; its photo checked once


def test_the_run_reads_the_owner_s_choices_by_sku_and_the_conflicts_the_produce_stored(monkeypatch):
    # the produce's conflict set (classified over the emitted products with every photo each has), never the sheet's own
    # reading: here the sheet would call the two listings siblings
    s3 = _S3({pub.owner_key(ROOT, "SEN-MINE"): json.dumps(_owned(D)).encode(), f"{RUN}{D}.main.jpg": b"m",
              imagestore.conflicts_key("sentas"): json.dumps({D: ["mine", "partner"]}).encode()})
    monkeypatch.setattr(proc, "sheet_and_photos", lambda client: (NO_TILES, {}))
    monkeypatch.setattr(proc, "photo_listings", lambda sheet, photos: [_listing("mine", D), _listing("partner", D)])
    monkeypatch.setattr(proc.gpu, "clear_leftovers", lambda batch, run_id: [])
    seen, handed = {}, {}

    def run(batch, sel, spend, clean, run_id, deadline, say=print):
        seen.update(owner=sel.owner, conflicts=sel.conflicts)
        return [], {"chosen": [], "tracked": [], "pending": [], "click_problems": []}
    monkeypatch.setattr(proc, "run", run)
    monkeypatch.setattr(proc.publish, "publish",
                        lambda s3, root, sheet, photos, say, assigned: handed.update(assigned) or {"changed": False})
    import time
    proc.process(s3, None, select.Spend(5.0), None, "R1", time.monotonic() + 3600, say=lambda m: None)
    assert seen["owner"] == {"SEN-MINE": _owned(D)} and seen["conflicts"] == {("sentas", D): ("mine", "partner")}
    assert handed == {("sentas", D): ["SEN-MINE"]}


def test_no_hosted_call_starts_after_the_work_window(monkeypatch):
    import time
    s3 = _S3({f"{ROOT}chosen/done.json": _record("done", A), f"{RUN}{A}.main.jpg": b"m",
              imagestore.conflicts_key("sentas"): b"{}"})
    monkeypatch.setattr(proc, "sheet_and_photos", lambda client: (NO_TILES, {}))
    monkeypatch.setattr(proc, "photo_listings", lambda sheet, photos: [_listing("new", D)])
    monkeypatch.setattr(proc.gpu, "clear_leftovers", lambda batch, run_id: [])
    seen = []

    def run(batch, sel, spend, clean, run_id, deadline, say=print):
        time.sleep(0.2)                                                     # the window (0.05 s) ends while working
        with pytest.raises(select.Halted, match="work window"):
            spend.reserve(0.01)
        seen.append(1)
        return [], {"chosen": [], "tracked": [], "pending": [], "click_problems": []}
    monkeypatch.setattr(proc, "run", run)
    proc.process(s3, None, select.Spend(5.0), None, "R1", time.monotonic() + 0.05, say=lambda m: None)
    assert seen == [1]


def test_a_close_up_choice_moves_to_a_whole_slab_once_the_judge_passes_one_and_a_rerun_costs_nothing(monkeypatch):
    """The pick, the GPU list, the judge, the hosted edit and the report over stored results: a listing whose chosen photo
    (A) is a close-up gets its whole slab (B) judged, the choice moves to it, and nothing is paid twice."""
    scraped = f"{ENV_SEGMENT}/products/scraped/sentas/"
    s3 = _S3({f"{ROOT}chosen/one.json": _record("one", A), f"{RUN}{A}.main.jpg": b"ma", f"{RUN}judged/{A}.json":
              json.dumps({"verdict": "pass", "problems": [], "version": C.JUDGE_VERSION}).encode(),
              f"{RUN}{B}.main.jpg": b"mb", f"{RUN}{B}.slab.png": b"p", f"{RUN}{C.OUTLINE_CHECKED}{B}.json": b"{}",
              f"{RUN}held/{D}.json": b'{"reason": "x"}', f"{scraped}{B}.jpg": b"ob"})
    asked = []
    monkeypatch.setattr(select.judge, "judge", lambda o, r, f: asked.append(r) or (
        {"verdict": "pass", "problems": [], "version": C.JUDGE_VERSION}, 0.02))
    monkeypatch.setattr(select.clean, "submit", lambda *a: pytest.fail("a listing with a good photo was cleaned"))

    def picked():
        records = proc.choices(s3, ROOT)
        return proc.todo([_listing("one", A, B, D)], records, set(), proc.whole_slabs(s3, records), set(), {}, {})
    found = picked()
    assert [l["kept"] for l in found] == [(C.RUN_TAG, A)]
    sel = select.Selection(s3, found, C.RUN_TAG, C.CLEAN_TAG, {}, {},
                           select.read_rounds(s3, (C.RUN_TAG, C.CLEAN_TAG), ["sentas"]), [], {})
    assert sel.gpu_todo() == {}                                     # every candidate has its result: no GPU
    spend = select.Spend(5.0)
    select.judge_all(sel, spend, float("inf"))
    select.clean_all(sel, spend, float("inf"))
    assert asked == [b"mb"] and spend.spent == pytest.approx(0.02)
    result = select.report(sel)
    assert [c["sha"] for c in result["chosen"]] == [B]
    assert json.loads(s3.objects[f"{ROOT}chosen/one.json"]["Body"])["sha"] == B
    assert picked() == []                                           # a whole slab now: never picked again


def test_a_close_up_choice_with_no_whole_slab_left_to_try_costs_nothing_again(monkeypatch):
    s3 = _S3({f"{ROOT}chosen/one.json": _record("one", A), f"{RUN}{A}.main.jpg": b"ma",
              f"{RUN}{B}.main.jpg": b"mb", f"{RUN}{B}.slab.png": b"p", f"{RUN}{C.OUTLINE_CHECKED}{B}.json": b"{}",
              f"{RUN}judged/{B}.json": json.dumps({"verdict": "fail", "problems": [{"kind": "light_reflection"}],
                                                   "version": C.JUDGE_VERSION}).encode()})
    monkeypatch.setattr(select.judge, "judge", lambda *a: pytest.fail("paid again"))
    monkeypatch.setattr(select.clean, "submit", lambda *a: pytest.fail("a listing with a good photo was cleaned"))
    records = proc.choices(s3, ROOT)
    sel = select.Selection(s3, proc.todo([_listing("one", A, B)], records, set(), proc.whole_slabs(s3, records), set(), {}, {}),
                           C.RUN_TAG, C.CLEAN_TAG, {}, {}, select.read_rounds(s3, (C.RUN_TAG, C.CLEAN_TAG), ["sentas"]), [], {})
    spend = select.Spend(5.0)
    assert sel.gpu_todo() == {}
    assert select.judge_all(sel, spend, float("inf")) == ([], False)
    assert select.clean_all(sel, spend, float("inf")) == ([], False)
    s3.written.clear()
    assert [c["sha"] for c in select.report(sel)["chosen"]] == [A]
    assert spend.spent == 0 and s3.written == [f"{ROOT}tracked.csv"]   # the kept record is not written again


def test_an_open_book_chosen_before_its_halves_had_a_slab_gets_its_3d_without_a_new_verdict_or_record(monkeypatch):
    s3 = _S3({f"{ROOT}chosen/one.json": _record("one", A), **{f"{RUN}{A}.{v}.jpg": v.encode() for v in ("main", "white", "studio")},
              f"{RUN}judged/{A}.json": json.dumps({"verdict": "pass", "problems": [], "version": C.JUDGE_VERSION}).encode()})
    monkeypatch.setattr(select.judge, "judge", lambda *a: pytest.fail("judged again"))
    records = proc.choices(s3, ROOT)
    found = proc.todo([_listing("one", A)], records, set(), proc.whole_slabs(s3, records), set(), {}, {})
    sel = select.Selection(s3, found, C.RUN_TAG, C.CLEAN_TAG, {}, {},
                           select.read_rounds(s3, (C.RUN_TAG, C.CLEAN_TAG), ["sentas"]), [], {})
    assert sel.gpu_todo() == {"sentas": [A]}                        # outdated: the GPU completes it
    s3._store(f"{RUN}{A}.close.jpg", b"close")                     # what the GPU job adds (standardise_source.rework)
    s3._store(f"{RUN}{A}.slab.png", _slab_png())
    s3._store(f"{RUN}{C.OUTLINE_CHECKED}{A}.json", b'{"changed": false}')
    sel.refresh()
    spend = select.Spend(5.0)
    select.judge_all(sel, spend, float("inf"))
    s3.written.clear()
    assert [c["sha"] for c in select.report(sel)["chosen"]] == [A] and spend.spent == 0
    assert f"{ROOT}chosen/one.json" not in s3.written                # the same choice: its record stays
    out = pub.publish(s3, ROOT, NO_TILES, {}, say=lambda m: None, assigned={})
    assert out["made_3d"] == 1 and imagestore.standard_key("sentas", A, "close") in s3.objects


# --- a recheck never writes over a result: a candidate, judged, promoted or discarded ------------------------------------
def _redrawn():
    """What the GPU job's recheck makes of a slab this version draws differently: other views, a corner of its slab off."""
    import cv2
    import numpy as np
    from stone_pipeline.standardise.photo import Result
    img = np.full((40, 60, 3), 200, np.uint8)
    slab = cv2.cvtColor(cv2.imdecode(np.frombuffer(_slab_png(corner=True), np.uint8), cv2.IMREAD_UNCHANGED), cv2.COLOR_BGRA2RGBA)
    return Result(kind="slab", images={"main": img, "white": img, "studio": img}, slab=(slab[..., :3], slab[..., 3] / 255.0),
                  chosen=(0, 1), changed=True)


def _older_whole_slab_chosen(owner):
    """A whole slab chosen for listing "one" under the older outline rule, by the judge or by the owner, published as it
    is: (bucket, the owner's records {SKU: record})."""
    s3 = _S3({f"{ROOT}chosen/one.json": _record("one", A), f"{RUN}{A}.slab.png": _slab_png(),
              **{f"{RUN}{A}.{v}.jpg": b"old " + v.encode() for v in ("main", "white", "studio", "close")},
              f"{RUN}judged/{A}.json": json.dumps({"verdict": "pass", "problems": [], "version": C.JUDGE_VERSION}).encode(),
              f"{ENV_SEGMENT}/products/scraped/sentas/{A}.jpg": b"photo"})
    pub.publish(s3, ROOT, NO_TILES, {}, say=lambda m: None, assigned={})
    return s3, ({"SEN-ONE": _owned(A, listing="one")} if owner else {})


def _state(s3):
    """Every object's bytes: what a run changed."""
    return {k: v["Body"] for k, v in s3.objects.items()}


def _photos_run(monkeypatch, s3, owner, redraw=None, verdict=None):
    """One photos run over listing "one" (photos_round's selection, proc.run's steps, then publish), its GPU job stood in:
    each photo it sends is worked on with `redraw` (a photo.Result) through standardise_source.rework, as the job does; the
    judge answers `verdict` ("pass" or "fail"). Unexpected work fails the test. Returns (photos sent, judge calls)."""
    from deploy import standardise_source as src
    sent, asked = [], []

    def run_round(batch, client, tag, todo, run_id, deadline, spend, work=None):
        for source, shas in todo.items():
            for sha in shas:
                out = src.round_prefix(tag, source)
                assert redraw is not None, f"sent {sha[:12]} to the GPU"
                sent.append(sha)
                src.rework(client, out, sha, redraw, src.again(client, out, sha))
        return []

    def judged(original, result, fmt):
        assert verdict is not None, "a judge call"
        asked.append(result)
        return {"verdict": verdict, "problems": [], "version": C.JUDGE_VERSION}, 0.02
    monkeypatch.setattr(proc.gpu, "run_round", run_round)
    monkeypatch.setattr(select.judge, "judge", judged)
    listings, records = [_listing("one", A)], proc.choices(s3, ROOT)
    rounds = select.read_rounds(s3, (C.RUN_TAG, C.CLEAN_TAG), ["sentas"])
    picks = {l["key"]: pick for l in listings
             if (pick := select.owner_choice(owner.get(l["sku"]), l, set(), {}, rounds)) is not None}
    decided = proc.Decisions(set(), owner, {}, {}, rounds, picks, {}, records)
    found = proc.todo(listings, records, set(), proc.whole_slabs(s3, records), set(), {}, picks)
    sel = select.Selection(s3, found, C.RUN_TAG, C.CLEAN_TAG, owner, {}, rounds,
                           proc.live_choices(listings, records, set(), decided), {})
    proc.run(None, sel, select.Spend(5.0), None, "R1", float("inf"), say=lambda m: None)
    pub.publish(s3, ROOT, NO_TILES, {}, say=lambda m: None, assigned={})
    return sent, asked


def _published(s3):
    return {k: v for k, v in _state(s3).items() if k.startswith(f"{ENV_SEGMENT}/products/standard/")}


@pytest.mark.parametrize("owner", [False, True])
def test_a_redraw_the_judge_passes_is_promoted_then_published_and_a_second_run_is_a_no_op(monkeypatch, owner):
    from deploy import standardise_source as src
    s3, owned = _older_whole_slab_chosen(owner)
    before = _published(s3)
    sent, asked = _photos_run(monkeypatch, s3, owned, _redrawn(), "pass")
    assert sent == [A] and len(asked) == 1 and asked[0] != b"old main"              # the candidate was judged
    white = imagestore.standard_key("sentas", A, "white")
    assert s3.objects[f"{RUN}{A}.white.jpg"]["Body"] != b"old white"                 # promoted over the result
    assert s3.objects[white]["Body"] == s3.objects[f"{RUN}{A}.white.jpg"]["Body"] != before[white]
    assert f"{RUN}{A}.close.jpg" not in s3.objects and imagestore.standard_key("sentas", A, "close") not in s3.objects
    assert json.loads(s3.objects[src.checked_key(RUN, A)]["Body"]) == {"changed": True}
    assert "cost" in json.loads(s3.objects[f"{RUN}judged/{A}.json"]["Body"])          # the redraw's verdict
    assert not any(k.startswith(src.candidate_prefix(RUN)) for k in s3.objects)
    after = _state(s3)
    assert _photos_run(monkeypatch, s3, owned) == ([], []) and _state(s3) == after


@pytest.mark.parametrize("owner", [False, True])
def test_a_redraw_the_judge_fails_is_discarded_and_everything_live_stays_and_a_second_run_is_a_no_op(monkeypatch, owner):
    """The result, its verdict, the choice and what is published stay exactly as they were; it is never redrawn or
    judged again under this rule."""
    from deploy import standardise_source as src
    s3, owned = _older_whole_slab_chosen(owner)
    before = _state(s3)
    sent, asked = _photos_run(monkeypatch, s3, owned, _redrawn(), "fail")
    assert sent == [A] and len(asked) == 1
    after = _state(s3)
    note = json.loads(after.pop(src.checked_key(RUN, A)))
    assert note["changed"] is False and "judge" in note["kept"]
    assert {k: v for k, v in after.items() if k != f"{ROOT}tracked.csv"} == before
    after = _state(s3)
    assert _photos_run(monkeypatch, s3, owned) == ([], []) and _state(s3) == after


class _Killed(BaseException):
    """The process killed (nothing catches it, as nothing catches a SIGKILL)."""


def _cut_at(s3, n):
    """s3 whose n-th write into the run folders (put, copy, delete) kills the run there: a list of the writes."""
    seen = []
    for name in ("put_object", "copy_object", "delete_object"):
        real = getattr(s3, name)

        def cut(*a, _real=real, **k):
            if "/standardise-test/" in k.get("Key", ""):
                seen.append(k["Key"])
                if len(seen) == n:
                    raise _Killed(k["Key"])
            return _real(*a, **k)
        setattr(s3, name, cut)
    return seen


@pytest.mark.parametrize("verdict", ["pass", "fail"])
def test_a_run_cut_short_at_any_write_is_finished_cleanly_by_the_next(monkeypatch, verdict):
    """Killed at each write in turn (the candidate, its marker, the judge's verdict, the promotion or the discard, the
    note, the removal): what is published never changes before the next run, which ends exactly where an uninterrupted
    run ends, and never pays the judge again for a verdict already stored."""
    from deploy import standardise_source as src
    s3, owned = _older_whole_slab_chosen(False)
    seen = _cut_at(s3, 0)                              # counts every write, fails none
    _photos_run(monkeypatch, s3, owned, _redrawn(), verdict)
    reference = _state(s3)
    assert len(seen) > 8
    for n in range(1, len(seen) + 1):
        s3, _ = _older_whole_slab_chosen(False)
        published = _published(s3)
        real = {name: getattr(s3, name) for name in ("put_object", "copy_object", "delete_object")}
        _cut_at(s3, n)
        with pytest.raises(_Killed):
            _photos_run(monkeypatch, s3, owned, _redrawn(), verdict)
        assert _published(s3) == published, seen[n - 1]
        judged = f"{src.candidate_prefix(RUN)}judged/{A}.json" in s3.objects
        for name, fn in real.items():
            setattr(s3, name, fn)
        _, asked = _photos_run(monkeypatch, s3, owned, _redrawn(), verdict)
        assert _state(s3) == reference, seen[n - 1]
        assert not (judged and asked), seen[n - 1]


def _killed_at(s3, method, key):
    """s3 whose `method` call on `key` kills the run there."""
    real = getattr(s3, method)

    def cut(*a, **k):
        if k["Key"] == key:
            raise _Killed(key)
        return real(*a, **k)
    setattr(s3, method, cut)
    return lambda: setattr(s3, method, real)


def _process(monkeypatch, s3, mode, redraw=None, verdict=None):
    """proc.process over listing "one", its GPU job stood in as in _photos_run: mode "photos", "halted" (a photos run
    stopped in its first round: the run lock taken over, so nothing is settled after a GPU round) or "close-only" (every
    close-up made already). Returns the photos sent to the GPU."""
    from deploy import standardise_source as src
    import time
    sent = []

    def run_round(batch, client, tag, todo, run_id, deadline, spend, work=None):
        if mode == "halted":
            raise select.Halted("the run lock was taken over")
        for source, shas in todo.items():
            for sha in shas:
                assert redraw is not None, f"sent {sha[:12]} to the GPU"
                out = src.round_prefix(tag, source)
                sent.append(sha)
                src.rework(client, out, sha, redraw, src.again(client, out, sha))
        return []

    def judged(original, result, fmt):
        assert verdict is not None, "a paid call"
        return {"verdict": verdict, "problems": [], "version": C.JUDGE_VERSION}, 0.02
    monkeypatch.setattr(proc.gpu, "clear_leftovers", lambda batch, run_id: [])
    monkeypatch.setattr(proc.gpu, "run_round", run_round)
    monkeypatch.setattr(select.judge, "judge", judged)
    monkeypatch.setattr(proc, "sheet_and_photos", lambda client: (NO_TILES, {}))
    monkeypatch.setattr(proc, "photo_listings", lambda sheet, photos: [_listing("one", A)])
    monkeypatch.setattr(close, "undone", lambda client, records: [])
    spend = select.Spend(5.0)
    if mode == "halted":
        spend.halt("the run lock was taken over")
    proc.process(s3, None, spend, None, "R2", time.monotonic() + 3600, say=lambda m: None, close_only=mode == "close-only")
    return sent


def _promotion_cut_short(monkeypatch):
    """A chosen slab whose redraw passed and whose promotion was killed half way: its slab and its white and studio views
    copied over the result, its main view not, the old close view still there, no note."""
    from deploy import standardise_source as src
    s3, owned = _older_whole_slab_chosen(False)
    s3._store(imagestore.conflicts_key("sentas"), b"{}")
    before = _published(s3)
    restore = _killed_at(s3, "copy_object", f"{RUN}{A}.main.jpg")
    with pytest.raises(_Killed):
        _photos_run(monkeypatch, s3, owned, _redrawn(), "pass")
    restore()
    assert _published(s3) == before and src.pending(s3, RUN, A)
    assert s3.objects[f"{RUN}{A}.white.jpg"]["Body"] != b"old white" and s3.objects[f"{RUN}{A}.main.jpg"]["Body"] == b"old main"
    return s3


def _published_whole(s3):
    """Whether every published view of A is its run's, and all of them new: never old and new side by side."""
    views = [v for v in imagestore.STANDARD_VIEWS if imagestore.standard_key("sentas", A, v) in s3.objects]
    return views and all(s3.objects[imagestore.standard_key("sentas", A, v)]["Body"] == s3.objects[f"{RUN}{A}.{v}.jpg"]["Body"]
                         != b"old " + v.encode() for v in views)


@pytest.mark.parametrize("mode", ["halted", "close-only"])
def test_a_promotion_cut_short_is_finished_before_either_kind_of_run_publishes(monkeypatch, mode):
    """Killed half way through its copies: the next run, a photos run stopped in its first round or a close-only run
    (neither settles after a GPU round), finishes the promotion first, with no paid call, so a half-new set is never
    published."""
    from deploy import standardise_source as src
    s3 = _promotion_cut_short(monkeypatch)
    assert _process(monkeypatch, s3, mode) == []
    assert not src.pending(s3, RUN, A) and _published_whole(s3)
    assert imagestore.standard_key("sentas", A, "close") not in s3.objects                # the redraw makes none


def test_a_promotion_cut_short_then_a_later_rule_is_finished_under_its_own_rule_then_checked_whole(monkeypatch):
    """The candidate is settled under the rule its marker names: promoted (its stored pass) and noted under that rule,
    then the photo is due under the later rule and checked whole; never old and new views published side by side."""
    from deploy import standardise_source as src
    from stone_pipeline.standardise.photo import Result
    s3 = _promotion_cut_short(monkeypatch)
    monkeypatch.setattr(C, "OUTLINE_CHECKED", "outline-v3/")
    assert _process(monkeypatch, s3, "photos", Result(kind="slab", unchanged=True)) == [A]
    assert json.loads(s3.objects[f"{RUN}outline-v2/{A}.json"]["Body"]) == {"changed": True}
    assert json.loads(s3.objects[f"{RUN}outline-v3/{A}.json"]["Body"]) == {"changed": False}
    assert not src.pending(s3, RUN, A) and _published_whole(s3)


@pytest.mark.parametrize("verdict", ["pass", "fail"])
def test_a_candidate_drawn_under_an_older_rule_is_settled_under_it_unjudged_and_its_slab_checked_again(monkeypatch, verdict):
    """A candidate pending when a later rule is deployed: never judged (no verdict of it), so it is discarded and noted
    under the rule it was drawn under; the photo is due under the new rule and is checked whole."""
    from deploy import standardise_source as src
    s3, owned = _older_whole_slab_chosen(False)
    assert src.rework(s3, RUN, A, _redrawn(), "recheck") == "redrawn for the judge"     # under outline-v2
    monkeypatch.setattr(C, "OUTLINE_CHECKED", "outline-v3/")
    before = _state(s3)
    assert _photos_run(monkeypatch, s3, owned) == ([], [])                               # settled, nothing judged
    after = _state(s3)
    note = json.loads(after.pop(f"{RUN}outline-v2/{A}.json"))
    assert note["changed"] is False and "never judged" in note["kept"]
    assert {k: v for k, v in after.items() if k != f"{ROOT}tracked.csv"} == \
        {k: v for k, v in before.items() if not k.startswith(src.candidate_prefix(RUN))}
    sent, asked = _photos_run(monkeypatch, s3, owned, _redrawn(), verdict)
    assert sent == [A] and len(asked) == 1                                              # checked under outline-v3
    assert f"{RUN}outline-v3/{A}.json" in s3.objects and _photos_run(monkeypatch, s3, owned) == ([], [])


def test_a_removal_cut_short_after_a_pass_then_a_later_rule_never_stalls_the_runs(monkeypatch):
    """Its note (outline-v2) written, its candidate half removed: under a later rule it is removed, never promoted
    again from files already gone, and the photo is checked under the new rule."""
    from deploy import standardise_source as src
    s3, owned = _older_whole_slab_chosen(False)
    restore = _killed_at(s3, "delete_object", f"{src.candidate_prefix(RUN)}{A}.white.jpg")
    with pytest.raises(_Killed):
        _photos_run(monkeypatch, s3, owned, _redrawn(), "pass")
    restore()
    assert json.loads(s3.objects[src.checked_key(RUN, A)]["Body"]) == {"changed": True} and src.pending(s3, RUN, A)
    monkeypatch.setattr(C, "OUTLINE_CHECKED", "outline-v3/")
    assert _photos_run(monkeypatch, s3, owned) == ([], []) and not src.pending(s3, RUN, A)
    sent, _ = _photos_run(monkeypatch, s3, owned, _redrawn(), "pass")
    assert sent == [A] and _photos_run(monkeypatch, s3, owned) == ([], [])


@pytest.mark.parametrize("bump", [False, True])
def test_a_recheck_with_no_whole_slab_notes_it_kept_and_never_loops(monkeypatch, bump):
    """Also after a candidate cut short (its leftovers removed), and under a later rule (a new note folder)."""
    from deploy import standardise_source as src
    from stone_pipeline.standardise.photo import Result
    s3, owned = _older_whole_slab_chosen(False)
    s3._store(f"{src.candidate_prefix(RUN)}{A}.slab.png", b"half")                    # a candidate cut short
    if bump:
        _photos_run(monkeypatch, s3, owned, _redrawn(), "fail")
        monkeypatch.setattr(C, "OUTLINE_CHECKED", "outline-v3/")
    before = _published(s3)
    sent, asked = _photos_run(monkeypatch, s3, owned, Result(held="two stones in one outline"))
    assert sent == [A] and asked == [] and _published(s3) == before
    assert json.loads(s3.objects[src.checked_key(RUN, A)]["Body"])["kept"] == "two stones in one outline"
    assert not any(k.startswith(src.candidate_prefix(RUN)) for k in s3.objects)
    assert f"{RUN}noslab/{A}.json" not in s3.objects                                   # never noslab/ over a live result
    after = _state(s3)
    assert _photos_run(monkeypatch, s3, owned) == ([], []) and _state(s3) == after


def test_a_close_only_run_makes_the_close_ups_again_in_the_same_process_then_publishes(monkeypatch):
    import time
    s3 = _S3({f"{ROOT}chosen/one.json": _record("one", A), f"{ROOT}chosen/two.json": _record("two", B),
              f"{RUN}{A}.main.jpg": b"m", f"{RUN}{B}.main.jpg": b"m"})
    monkeypatch.setattr(proc.gpu, "clear_leftovers", lambda batch, run_id: [])
    rounds = []
    monkeypatch.setattr(proc.gpu, "run_round", lambda batch, client, tag, todo, run_id, deadline, spend, work:
                        rounds.append((tag, todo, run_id, work)) or [])
    monkeypatch.setattr(proc, "sheet_and_photos", lambda client: (NO_TILES, {}))
    monkeypatch.setattr(proc.select, "Selection", lambda *a, **k: pytest.fail("a close-only run picks no listings"))
    summary = proc.process(s3, None, select.Spend(5.0), None, "R1", time.monotonic() + 3600, say=lambda m: None, close_only=True)
    assert rounds == [(C.CLEAN_TAG, {"sentas": [A, B]}, "R1", proc.gpu.CLOSE_ROUND)]
    assert summary["close_only"] and summary["photos"] == 2 and summary["to_do"] == 2 and summary["published"]["changed"]
    assert f"{ROOT}close-summary.json" in s3.objects and f"{ROOT}summary.json" not in s3.objects   # the photos run's stays


def test_a_close_only_run_slices_only_the_photos_whose_close_up_is_not_made_yet(monkeypatch):
    import time
    from deploy import standardise_close as close
    other = f"{ENV_SEGMENT}/standardise-test/{C.CLEAN_TAG}/sentas/"
    s3 = _S3({f"{ROOT}chosen/one.json": _record("one", A), f"{ROOT}chosen/two.json": _record("two", B),
              f"{ROOT}chosen/three.json": _record("three", D), f"{ROOT}chosen/four.json": _record("four", D, other),
              f"{RUN}close_v{C.CLOSEUP_VERSION}/{A}.json": b"{}",                      # A done in an earlier run
              f"{RUN}close_v{C.CLOSEUP_VERSION}/retries/{B}.json": b"{}",              # B only left to a retry: not done
              f"{RUN}close_v{C.CLOSEUP_VERSION - 1}/{B}.json": b"{}",                  # done under an older rule
              f"{RUN}close_v{C.CLOSEUP_VERSION}/{D}.json": b"{}"})                     # D done in one folder, not the other
    assert [(r["sha"], r["prefix"]) for r in close.undone(s3, close.listed(close.chosen(s3, ROOT), "sentas"))] == [
        (B, RUN), (D, other)]
    monkeypatch.setattr(proc.gpu, "clear_leftovers", lambda batch, run_id: [])
    rounds = []
    monkeypatch.setattr(proc.gpu, "run_round", lambda batch, client, tag, todo, run_id, deadline, spend, work:
                        rounds.append(todo) or [])
    monkeypatch.setattr(proc, "sheet_and_photos", lambda client: (NO_TILES, {}))
    summary = proc.process(s3, None, select.Spend(5.0), None, "R1", time.monotonic() + 3600, say=lambda m: None, close_only=True)
    assert rounds == [{"sentas": [B, D]}] and summary["photos"] == 3 and summary["to_do"] == 2


def test_a_close_only_run_with_every_close_up_made_starts_no_gpu_job(monkeypatch):
    import time
    s3 = _S3({f"{ROOT}chosen/one.json": _record("one", A), f"{RUN}close_v{C.CLOSEUP_VERSION}/{A}.json": b"{}"})
    monkeypatch.setattr(proc.gpu, "clear_leftovers", lambda batch, run_id: [])
    monkeypatch.setattr(proc.gpu, "run_round", lambda *a, **k: pytest.fail("a GPU round for photos already done"))
    monkeypatch.setattr(proc, "sheet_and_photos", lambda client: (NO_TILES, {}))
    summary = proc.process(s3, None, select.Spend(5.0), None, "R1", time.monotonic() + 3600, say=lambda m: None, close_only=True)
    assert summary["photos"] == 1 and summary["to_do"] == 0 and summary["problems"] == 0


def test_a_close_only_run_publishes_only_the_conflict_photos_the_owner_assigned(monkeypatch):
    # it republishes the same choices, so it decides the conflict photos as a photos run does: D, shown by two stones'
    # listings, only for the listing the owner gave it to
    import time
    s3 = _S3({pub.owner_key(ROOT, "SEN-MINE"): json.dumps(_owned(D)).encode(), f"{RUN}{D}.main.jpg": b"m",
              imagestore.conflicts_key("sentas"): json.dumps({D: ["mine", "partner"]}).encode()})
    monkeypatch.setattr(proc, "sheet_and_photos", lambda client: (NO_TILES, {}))
    monkeypatch.setattr(proc, "photo_listings", lambda sheet, photos: [_listing("mine", D), _listing("partner", D)])
    monkeypatch.setattr(proc.gpu, "clear_leftovers", lambda batch, run_id: [])
    monkeypatch.setattr(proc.select, "Selection", lambda *a, **k: pytest.fail("a close-only run picks no listings"))
    handed = {}
    monkeypatch.setattr(proc.publish, "publish",
                        lambda s3, root, sheet, photos, say, assigned: handed.update(assigned) or {"changed": False})
    proc.process(s3, None, select.Spend(5.0), None, "R1", time.monotonic() + 3600, say=lambda m: None, close_only=True)
    assert handed == {("sentas", D): ["SEN-MINE"]}


def test_a_close_only_run_needs_no_hosted_key(monkeypatch):
    _configured(monkeypatch)
    monkeypatch.delenv("FAL_KEY", raising=False)
    assert proc.unconfigured(close_only=True) == [] and proc.unconfigured() == ["FAL_KEY"]


def test_a_close_only_run_holds_the_same_lock_named_by_its_run_id(monkeypatch):
    seen = {}
    monkeypatch.setenv("SCRAPER_RUN_ID", "20261102120000123")
    monkeypatch.setenv("SCRAPER_STANDARDISE_CLOSE_ONLY", "true")
    real = proc.exclusive
    monkeypatch.setattr(proc, "exclusive", lambda s3, owner, on_lost: seen.update(owner=owner) or real(s3, owner, on_lost))
    _main(monkeypatch)
    monkeypatch.setattr(proc, "process", lambda s3, batch, spend, run_id, deadline, say=print, close_only=False:
                        seen.update(close_only=close_only) or
                        {"problems": 0, "pending": 0, "budget_reached": False, "published": {"changed": False}})
    proc.main()
    assert seen["owner"]["close_only"] and seen["owner"]["run_id"] == "20261102120000123" and seen["close_only"]


class _Clients:
    def __init__(self, s3):
        self.s3 = s3

    def __call__(self, name, **kw):
        return self.s3 if name == "s3" else object()


def _main(monkeypatch, tmp_path=None, changed=True, capped=False, problems=0, relink_rc=0, writethrough_on=True,
          keep_process=False, steps=None):
    """main() with the process stubbed (unless the test stubbed it, keep_process): what it returns and the relinks it
    made. steps, when given, records the relink and the pool's last idle setting in the order they happen."""
    steps = [] if steps is None else steps
    monkeypatch.setattr(proc.gpu, "set_idle", lambda batch: steps.append("idle"))
    monkeypatch.setattr(proc, "unconfigured", lambda close_only=False: [])
    monkeypatch.setattr(proc, "stop_on_signal", lambda *spends: None)
    monkeypatch.setattr(proc.boto3, "client", _Clients(_S3()))
    monkeypatch.setattr(C, "BUDGET_USD", 10.0)
    monkeypatch.setattr(writethrough, "enabled", lambda: writethrough_on)
    if tmp_path is not None:
        monkeypatch.setenv("SCRAPER_RUN_SUMMARY", str(tmp_path / "summary.json"))
    else:
        monkeypatch.delenv("SCRAPER_RUN_SUMMARY", raising=False)
    relinks = []
    monkeypatch.setattr(proc, "relink", lambda: relinks.append(1) or steps.append("relink") or relink_rc)

    def process(s3, batch, spend, clean, run_id, deadline, say=print, close_only=False):
        spend.capped = capped
        return {"problems": problems, "pending": 0, "budget_reached": capped, "published": {"changed": changed}}
    if not keep_process:
        monkeypatch.setattr(proc, "process", process)
    return proc.main(), relinks


def test_the_new_photos_are_linked_in_the_same_run_when_the_folder_changed(monkeypatch):
    assert _main(monkeypatch) == (0, [1])


def test_no_relink_when_nothing_changed(monkeypatch):
    assert _main(monkeypatch, changed=False) == (0, [])


def test_no_relink_where_there_is_no_ledger_to_link_into(monkeypatch):
    assert _main(monkeypatch, writethrough_on=False) == (0, [])


def test_a_run_at_the_budget_or_with_problems_still_succeeds_its_summary_says_so(monkeypatch, tmp_path):
    assert _main(monkeypatch, tmp_path, capped=True, problems=3) == (0, [1])
    handed = json.loads((tmp_path / "summary.json").read_text())
    assert handed["budget_reached"] and handed["problems"] == 3 and handed["relinked"]


@pytest.mark.parametrize("changed, relink_rc, want", [(True, 0, ["relink", "idle"]), (True, 2, ["relink", "idle"]),
                                                       (False, 0, ["idle"])])
def test_the_pool_is_set_back_to_idle_at_the_run_s_end_after_the_relink(monkeypatch, changed, relink_rc, want):
    # a ceiling lowered while the round's instances still ran stays at what they held (prod 24 and 12, dev 32 on 3-4 Nov)
    steps = []
    _main(monkeypatch, changed=changed, relink_rc=relink_rc, steps=steps)
    assert steps == want


def test_a_pool_that_cannot_be_set_back_is_reported_and_the_run_s_outcome_stands(monkeypatch, capsys):
    def refused(batch):
        raise ClientError({"Error": {"Code": "ServerException", "Message": "busy"}}, "UpdateComputeEnvironment")
    _main(monkeypatch, steps=[])
    monkeypatch.setattr(proc.gpu, "set_idle", refused)
    assert proc.main() == 0
    assert "ERROR: the GPU pool's ceiling could not be set back to idle" in capsys.readouterr().out


def test_a_failed_relink_fails_the_run(monkeypatch):
    assert _main(monkeypatch, relink_rc=2) == (2, [1])


def test_the_process_is_named_by_the_runner_s_run_id_in_its_lock_and_its_jobs(monkeypatch):
    seen = {}
    monkeypatch.setenv("SCRAPER_RUN_ID", "20261102120000123")
    real = proc.exclusive
    monkeypatch.setattr(proc, "exclusive", lambda s3, owner, on_lost: seen.update(owner=owner) or real(s3, owner, on_lost))
    _main(monkeypatch)
    monkeypatch.setattr(proc, "process", lambda s3, batch, spend, clean, run_id, deadline, say=print, close_only=False: seen.update(run_id=run_id) or
                        {"problems": 0, "pending": 0, "budget_reached": False, "published": {"changed": False}})
    proc.main()
    assert seen["owner"]["run_id"] == "20261102120000123" and seen["run_id"] == "20261102120000123"


def test_a_second_process_refuses_to_start_while_one_runs(monkeypatch):
    monkeypatch.setattr(proc, "exclusive", lambda s3, owner, on_lost: (_ for _ in ()).throw(proc.RunActive("held")))
    assert _main(monkeypatch)[0] == 4


def _reaping(monkeypatch, lock_body):
    s3 = _S3({proc.LOCK: lock_body})
    monkeypatch.setattr(proc.boto3, "client", _Clients(s3))
    done = []
    monkeypatch.setattr(proc.gpu, "stop_run", lambda batch, run_id: done.append(("stopped", run_id)) or ["job1"])
    monkeypatch.setattr(proc.gpu, "set_idle", lambda batch: done.append("idle"))
    monkeypatch.setattr(proc.gpu, "clear_leftovers", lambda *a: pytest.fail("touched another run's jobs"))
    return s3, done


def test_reaping_a_killed_run_stops_its_jobs_and_removes_its_own_lock(monkeypatch):
    s3, done = _reaping(monkeypatch, b'{"run_id": "R7", "tags": ["v2", "v2-clean"]}')
    proc.reap("R7")
    assert done == [("stopped", "R7"), "idle"] and proc.LOCK not in s3.objects


def test_reaping_never_touches_another_process_s_lock_or_ceiling(monkeypatch):
    # a rolling deploy's other task, or a process started by hand, holds the lock and the raised ceiling
    s3, done = _reaping(monkeypatch, b'{"run_id": "R8"}')
    proc.reap("R7")
    assert done == [("stopped", "R7")] and proc.LOCK in s3.objects                 # only the killed run's own jobs


def test_the_relink_is_the_produce_s_republish_in_this_process_with_its_progress_noise_dropped(monkeypatch, capsys):
    from stone_pipeline import produce
    seen = []

    def republish(argv):
        seen.append(argv)
        print("image 1 of 4,313")
        return 0
    monkeypatch.setattr(produce, "main", republish)
    assert proc.relink() == 0
    assert seen == [["--stage", "republish"]] and "image 1" not in capsys.readouterr().out


# --- the trigger: the runner's photos stage ---------------------------------------------------------------------------
def test_the_photos_run_follows_only_a_full_produce_never_the_publish_button_s_republish(monkeypatch):
    from stone_pipeline.config import settings
    monkeypatch.setattr(settings.STANDARDISE, "AUTO", True)   # the class the runner reads at call time (a test reloads settings)
    started = []
    monkeypatch.setattr(runner, "start_run", lambda sources=None, stage="all", launch=None: started.append(stage) or ({}, 202))
    for stage, status in (("all", "succeeded"), ("republish", "succeeded"), ("catalog", "succeeded"), ("all", "failed"),
                          ("scrape", "succeeded"), ("inventory", "succeeded"), ("photos", "succeeded")):
        runner._follow_up({"run_id": "r", "stage": stage, "status": status})
    assert started == ["photos"]


def test_no_photos_run_follows_while_the_process_is_off(monkeypatch):
    from stone_pipeline.config import settings
    monkeypatch.setattr(settings.STANDARDISE, "AUTO", False)
    monkeypatch.setattr(runner, "start_run", lambda **kw: pytest.fail("photos started while off"))
    runner._follow_up({"run_id": "r", "stage": "all", "status": "succeeded"})


def test_the_photos_stage_is_one_run_over_the_whole_environment(monkeypatch):
    monkeypatch.setattr(runner, "_stamp_last_run", lambda *a, **k: None)
    launched = []
    rec, code = runner.start_run(None, "photos", launch=lambda r: launched.append(r) or r.update(status="succeeded"))
    assert code == 202 and rec["stage"] == "photos" and rec["sources"] == [] and launched
    assert runner._build_command(launched[0]) == [sys.executable, "-u", "-m", "deploy.standardise_run"]
    rec, code = runner.start_run(["sentas"], "photos", launch=lambda r: pytest.fail("launched"))
    assert code == 400



def test_a_close_only_run_is_a_photos_run_the_operator_starts_through_the_runner(monkeypatch):
    from stone_pipeline.config import server
    monkeypatch.setattr(runner, "_stamp_last_run", lambda *a, **k: None)
    launched = []
    monkeypatch.setattr(runner, "_launch_local", lambda r: launched.append(r) or r.update(status="succeeded"))
    code, rec = server.dispatch("POST", ["run"], {"stage": "photos", "close_only": True})
    assert code == 202 and rec["stage"] == "photos" and rec["close_only"] is True and launched[0]["close_only"] is True
    code, rec = server.dispatch("POST", ["run"], {"stage": "photos"})
    assert code == 202 and rec["close_only"] is False


def test_close_only_on_any_other_stage_or_not_a_boolean_is_refused(monkeypatch):
    from stone_pipeline.config import server
    monkeypatch.setattr(runner, "_launch_local", lambda r: pytest.fail("launched"))
    for body in ({"stage": "all", "close_only": True}, {"close_only": True}, {"stage": "republish", "close_only": True},
                 {"stage": "photos", "close_only": "true"}, {"stage": "photos", "close_only": 1}):
        code, rec = server.dispatch("POST", ["run"], body)
        assert code == 400 and "close_only" in rec["error"], body
    assert runner.start_run(stage="all", close_only=False, launch=lambda r: r.update(status="succeeded"))[1] == 202


class _Proc:
    stderr = None

    def __init__(self, lines, rc=0):
        self.stdout, self.rc = iter(lines), rc

    def wait(self):
        return self.rc

    def kill(self):
        pass


def _watch_quietly(monkeypatch):
    calls = []
    for name in ("_stamp_last_run", "_persist_run", "_follow_up"):
        monkeypatch.setattr(runner, name, lambda *a, **k: None)
    monkeypatch.setattr(runner, "_capture_counts", lambda: pytest.fail("a photos run reports its own counts"))
    for name in ("_persist_diagnostics", "_evaluate_admission"):
        monkeypatch.setattr(runner, name, lambda n=name: calls.append(n))
    return calls


def test_a_photos_run_s_record_carries_its_summary_and_its_report_is_streamed(monkeypatch, capsys, tmp_path):
    calls = _watch_quietly(monkeypatch)
    monkeypatch.setattr(runner.tempfile, "gettempdir", lambda: str(tmp_path))
    rec = {"run_id": "r", "stage": "photos"}
    summary = {"chosen": 3, "review": 1, "pending": 2, "run_spent_usd": 1.5, "budget_reached": True}
    runner._summary_path(rec).write_text(json.dumps(summary))
    runner._watch_local(rec, _Proc(["==> 3 of 6 listings with a good photo\n"]))
    assert rec["status"] == "succeeded" and rec["counts"] == summary and calls == []
    assert not runner._summary_path(rec).exists()
    assert "3 of 6 listings" in capsys.readouterr().err


def test_a_photos_run_that_ended_without_a_summary_has_no_counts(monkeypatch, tmp_path):
    _watch_quietly(monkeypatch)
    monkeypatch.setattr(runner.tempfile, "gettempdir", lambda: str(tmp_path))
    rec = {"run_id": "r", "stage": "photos"}
    runner._watch_local(rec, _Proc(["Traceback\n"], rc=1))
    assert rec["status"] == "failed" and rec["counts"] is None


def test_the_photo_process_s_stdout_is_its_report_and_its_summary_comes_back_in_a_file(monkeypatch, tmp_path):
    seen = {}

    def _popen(cmd, env=None, **kw):
        seen.update(kw, env=env)
        return type("P", (), {"poll": lambda self: None})()
    monkeypatch.setattr(subprocess, "Popen", _popen)
    monkeypatch.setattr(runner.threading, "Thread", lambda target, args, daemon: type("T", (), {"start": lambda self: None})())
    monkeypatch.setattr(runner.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(runner, "_child", None)          # no watcher runs here to clear the launched child
    monkeypatch.setattr(runner, "_watcher", None)
    rec = {"run_id": "r", "stage": "photos", "status": "queued", "close_only": False}
    runner._launch_local(rec)
    assert seen["stdout"] == subprocess.PIPE and seen["stderr"] == subprocess.STDOUT
    assert seen["env"]["SCRAPER_RUN_SUMMARY"] == str(runner._summary_path(rec))
    assert seen["env"]["SCRAPER_STANDARDISE_CLOSE_ONLY"] == "false"
    runner._launch_local({"run_id": "r2", "stage": "all", "status": "queued", "close_only": False})
    assert seen["stdout"] == subprocess.DEVNULL and seen["stderr"] == subprocess.PIPE
    assert "SCRAPER_RUN_SUMMARY" not in seen["env"]


def test_only_a_close_only_run_gets_the_close_only_switch_whatever_the_service_s_own_env_says(monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, env=None, **kw: seen.append(env) or type("P", (), {})())
    monkeypatch.setattr(runner.threading, "Thread", lambda target, args, daemon: type("T", (), {"start": lambda self: None})())
    monkeypatch.setattr(runner, "_child", None)
    monkeypatch.setattr(runner, "_watcher", None)
    monkeypatch.setenv("SCRAPER_STANDARDISE_CLOSE_ONLY", "true")              # set on the service by mistake
    monkeypatch.setenv("BLOKPORT_STANDARDISE_CLOSE_ONLY", "true")
    runner._launch_local({"run_id": "r", "stage": "photos", "status": "queued", "close_only": False})
    runner._launch_local({"run_id": "r2", "stage": "photos", "status": "queued", "close_only": True})
    assert [e["SCRAPER_STANDARDISE_CLOSE_ONLY"] for e in seen] == ["false", "true"]   # SCRAPER_ wins over BLOKPORT_


# --- a clean stop -----------------------------------------------------------------------------------------------------
class _Child:
    def __init__(self, ends):
        self.ends, self.signals, self.waited = ends, [], None

    def poll(self):
        return None

    def terminate(self):
        self.signals.append("TERM")

    def wait(self, timeout=None):
        self.waited = timeout
        if not self.ends:
            raise subprocess.TimeoutExpired("x", timeout)
        return 0


def _child_of(monkeypatch, child, stage="photos"):
    run = {"run_id": "r", "stage": stage}
    monkeypatch.setattr(runner, "_child", child)
    monkeypatch.setattr(runner, "_child_run", run)
    monkeypatch.setattr(runner, "_watcher", None)
    return run


def test_a_stopping_server_passes_sigterm_to_the_running_child_and_waits_for_it(monkeypatch):
    child = _Child(ends=True)
    run = _child_of(monkeypatch, child)
    monkeypatch.setattr(runner, "_stopping", False)
    monkeypatch.setattr(runner, "RUN_STOP_GRACE_SECONDS", 5)
    runner.stop_current(clock=lambda: 0)
    assert child.signals == ["TERM"] and child.waited == 5 and run["signalled"]


def test_a_stopping_server_kills_a_late_child_but_leaves_its_clearing_to_the_next_run(monkeypatch):
    # the server's last save comes first: the next photos run's clear_leftovers and stale-lock takeover clear it
    child = _Child(ends=False)
    child.kill = lambda: child.signals.append("KILL")
    _child_of(monkeypatch, child)
    monkeypatch.setattr(runner, "_stopping", False)
    monkeypatch.setattr(runner, "_reap_photos", lambda *a: pytest.fail("cleared during the server's stop"))
    runner.stop_current()                                                  # logged, never raised
    assert child.signals == ["TERM", "KILL"]


def test_a_photos_run_killed_while_giving_way_has_what_it_left_cleared_by_its_run_id(monkeypatch):
    child = _Child(ends=False)
    child.kill = lambda: child.signals.append("KILL")
    run = _child_of(monkeypatch, child)
    reaped = []
    monkeypatch.setattr(runner, "_reap_photos", lambda run_id, deadline, clock: reaped.append((run_id, deadline)))
    runner._end_child(100.0, clock=lambda: 0, reap=True)
    assert child.signals == ["TERM", "KILL"] and reaped == [(run["run_id"], 100.0)]


def test_a_killed_produce_leaves_nothing_of_the_photo_process_to_clear(monkeypatch):
    child = _Child(ends=False)
    child.kill = lambda: child.signals.append("KILL")
    _child_of(monkeypatch, child, stage="all")
    monkeypatch.setattr(runner, "_reap_photos", lambda *a: pytest.fail("cleared a photo run that was not running"))
    runner._end_child(100.0, clock=lambda: 0, reap=True)


def test_clearing_a_killed_photos_run_never_outlasts_the_deadline(monkeypatch):
    import threading
    import time
    from deploy import standardise_run
    release, seen = threading.Event(), []
    monkeypatch.setattr(standardise_run, "reap", lambda run_id: seen.append(run_id) or release.wait(5))
    started = time.monotonic()
    runner._reap_photos("R7", started + 0.1, time.monotonic)
    assert time.monotonic() - started < 1 and seen == ["R7"]                # it goes on alone; the launch is not held
    release.set()


def test_one_deadline_bounds_the_child_s_grace_and_the_watcher_s_join(monkeypatch):
    child = _Child(ends=True)
    _child_of(monkeypatch, child)
    monkeypatch.setattr(runner, "_stopping", False)
    monkeypatch.setattr(runner, "RUN_STOP_GRACE_SECONDS", 80)
    joined = []
    monkeypatch.setattr(runner, "_watcher", type("W", (), {"join": lambda self, timeout: joined.append(timeout)})())
    times = iter([0, 0, 30])                                                # the child took 30 s to end
    runner.stop_current(clock=lambda: next(times))
    assert child.waited == 80 and joined == [50]


def test_a_run_marked_to_give_way_before_it_was_launched_never_starts(monkeypatch):
    # the gap: the slot is claimed for photos (queued), the request to give way comes before its launch
    for name in ("_stamp_last_run", "_persist_run"):
        monkeypatch.setattr(runner, name, lambda *a, **k: None)
    monkeypatch.setattr(runner, "_resolve_sources", lambda requested: sorted(requested or ["polonine"]))
    monkeypatch.setattr(runner, "_child", None)
    monkeypatch.setattr(runner, "_watcher", None)
    monkeypatch.setattr(runner, "_stopping", False)
    pending, launched = [], []
    photos, code = runner.start_run(None, "photos", launch=pending.append)          # claimed, launch still to come
    publish, code = runner.start_run(None, "republish", launch=lambda r: launched.append(r["run_id"]))
    try:
        assert publish["run_id"] != photos["run_id"]                                # claimed in the same millisecond
        assert code == 202 and runner._runs[photos["run_id"]]["preempted_by"]      # marked with the claim
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("a run that gave way was launched"))
        runner._launch_local(pending[0])                                            # its launch comes late
        import time
        for _ in range(100):
            if launched:
                break
            time.sleep(0.02)
        assert launched == [publish["run_id"]]
        assert runner.get_run(photos["run_id"])["status"] == "stopped"
    finally:
        runner._runs[publish["run_id"]]["status"] = "succeeded"                      # free the slot for later tests


def test_no_run_is_launched_once_the_server_is_stopping(monkeypatch):
    monkeypatch.setattr(runner, "_stopping", True)
    monkeypatch.setattr(runner, "_persist_run", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_stamp_last_run", lambda *a, **k: None)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("launched while the server stops"))
    rec = {"run_id": "r", "stage": "republish", "status": "queued"}
    runner._launch_local(rec)
    assert rec["status"] == "failed" and "stopping" in rec["error"]


def test_a_photos_run_that_ended_on_its_own_keeps_its_status_though_marked_to_give_way(monkeypatch, tmp_path):
    _watch_quietly(monkeypatch)
    monkeypatch.setattr(runner.tempfile, "gettempdir", lambda: str(tmp_path))
    rec = {"run_id": "r", "stage": "photos", "preempted_by": "republish run x"}     # marked, never signalled
    runner._watch_local(rec, _Proc(["==> done\n"], rc=0))
    assert rec["status"] == "succeeded"


def test_a_stopping_server_starts_no_follow_up_run(monkeypatch):
    from stone_pipeline.config import settings
    monkeypatch.setattr(settings.STANDARDISE, "AUTO", True)
    monkeypatch.setattr(runner, "_stopping", True)
    monkeypatch.setattr(runner, "start_run", lambda **kw: pytest.fail("a run started while the server stops"))
    runner._follow_up({"run_id": "r", "stage": "all", "status": "succeeded"})


class _Running:
    """A photos process that runs until it gets SIGTERM, then cleans up and exits."""

    def __init__(self):
        import threading
        self.stopped, self.signals, self.stderr = threading.Event(), [], None

    @property
    def stdout(self):
        yield "==> standardise process R1\n"
        self.stopped.wait(5)

    def poll(self):
        return -15 if self.stopped.is_set() else None

    def terminate(self):
        self.signals.append("TERM")
        self.stopped.set()

    def wait(self, timeout=None):
        self.stopped.wait(timeout if timeout is not None else 5)
        return -15

    def kill(self):
        self.signals.append("KILL")
        self.stopped.set()


def test_the_owner_s_publish_stops_a_photos_run_cleanly_and_runs_at_once(monkeypatch, tmp_path):
    import time
    for name in ("_stamp_last_run", "_persist_run", "_persist_diagnostics", "_evaluate_admission"):
        monkeypatch.setattr(runner, name, lambda *a, **k: None)
    monkeypatch.setattr(runner, "_capture_counts", lambda: None)
    monkeypatch.setattr(runner.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(runner, "_resolve_sources", lambda requested: sorted(requested or ["polonine"]))
    monkeypatch.setattr(runner, "_child", None)
    monkeypatch.setattr(runner, "_watcher", None)
    child = _Running()
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: child)
    photos, code = runner.start_run(None, "photos")
    assert code == 202
    for _ in range(100):                                                            # the photos run is under way
        if runner.get_run(photos["run_id"])["status"] == "running":
            break
        time.sleep(0.01)
    launched = []
    publish, code = runner.start_run(None, "republish", launch=lambda r: launched.append(r["run_id"]))
    assert code == 202 and publish["stage"] == "republish"
    try:
        for _ in range(100):
            if launched:
                break
            time.sleep(0.05)
        assert child.signals == ["TERM"] and launched == [publish["run_id"]]       # the photos run ended first
        record = runner.get_run(photos["run_id"])
        assert record["status"] == "stopped" and "republish" in record["error"]
    finally:
        runner._runs[publish["run_id"]]["status"] = "succeeded"                     # free the slot for later tests


def test_a_second_photos_run_or_a_produce_over_a_produce_is_still_refused(monkeypatch):
    monkeypatch.setattr(runner, "_stamp_last_run", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_resolve_sources", lambda requested: sorted(requested or ["polonine"]))
    held = []
    rec, code = runner.start_run(None, "photos", launch=lambda r: held.append(r))
    try:
        assert runner.start_run(None, "photos", launch=lambda r: pytest.fail("ran twice"))[1] == 409
    finally:
        held[0]["status"] = "succeeded"
    rec, code = runner.start_run(None, "republish", launch=lambda r: held.append(r))
    try:
        assert runner.start_run(None, "all", launch=lambda r: pytest.fail("ran twice"))[1] == 409
    finally:
        held[1]["status"] = "succeeded"


def test_the_server_s_sigterm_stops_the_run_before_its_last_config_save(monkeypatch):
    from stone_pipeline.config import server
    from stone_pipeline.ledger import snapshot
    order = []
    monkeypatch.setattr(runner, "stop_current", lambda: order.append("stop run"))
    monkeypatch.setattr(snapshot, "save_config", lambda db: order.append("save"))
    with pytest.raises(SystemExit):
        server.shutdown("config.db", type("P", (), {"stop": lambda self: order.append("periodic")})())
    assert order == ["stop run", "periodic", "save"]
