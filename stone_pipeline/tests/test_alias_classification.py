"""P2-10 C5: the alias classification file (plan P2-10-SCRAPER-PLAN.md section 5; table 249 rev 3 "Definitions").

The file names every served Key and pending tombstone with its current acked set, one row per (Key, match_key(Value)),
and gives each value one class: code, operator, removed, seed, learned, or the one `Key,,none` row for a Key with no
value. It is written only when every variation is synced; a served value no store owns refuses. The difference list
names every removed and refused value for the owner. POST /config/v1/aliases/classification publishes it with its
manifest entry and the `alias_classification` field, read-only on the ledger and config.db.
Fixtures only."""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from stone_pipeline.aliases import cleanup
from stone_pipeline.core import text
from stone_pipeline.core.alias_problem import code_matcher
from stone_pipeline.core.text import match_key
from stone_pipeline.ledger.db import Ledger, now_iso

REPO = Path(__file__).resolve().parents[2]
ABS = "granite__absolute_black_7f3c0b6e-1111-5222-8333-944455556666"
KASH = "granite__kashmir_white_7f3c0b6e-2222-5222-8333-944455556666"
CARR = "marble__carrara_7f3c0b6e-3333-5222-8333-944455556666"


def _served(aliases, computed=True):
    """The v18 served set (P2-10 C4) of `aliases`: a code-shaped value in `codes`, every other one an alias_rows row
    (its method does not class it: the stores do). Both NULL when not computed yet."""
    if not computed:
        return None, None
    is_code = code_matcher(r"[Gg]\d+[A-Za-z]?")
    return (json.dumps([{"value": v, "method": "seed"} for v in aliases if not is_code(v)], ensure_ascii=False),
            json.dumps([v for v in aliases if is_code(v)], ensure_ascii=False))


def _variation(ledger, key, name, type_, aliases=(), state="synced", abandoned=False, computed=True):
    now = now_iso()
    alias_rows, codes = _served(aliases, computed)
    ledger.upsert("variation", {"key": key, "type": type_, "name": name, "alias_rows": alias_rows, "codes": codes,
                                "medusa_id": "V-" + key[:6], "in_full": 1, "payload_hash": "h", "state": state,
                                "abandoned_at": now if abandoned else None, "first_seen": now, "last_synced": now,
                                "created_at": now, "updated_at": now}, pk=("key",))


def _tombstone(ledger, key):
    now = now_iso()
    ledger.upsert("removed", {"external_id": key, "kind": "variation", "source": None, "reason": "variation_removed",
                              "state": "pending", "sync_attempts": 0, "created_at": now, "updated_at": now},
                  pk=("external_id",))


def _stores(seed=None, operator=None, learned=None, pattern=r"[Gg]\d+[A-Za-z]?"):
    return cleanup.Stores(seed={k: frozenset(match_key(v) for v in vs) for k, vs in (seed or {}).items()},
                          operator={(match_key(n), match_key(t)): frozenset(match_key(v) for v in vs)
                                    for (n, t), vs in (operator or {}).items()},
                          learned={k: frozenset(match_key(v) for v in vs) for k, vs in (learned or {}).items()},
                          is_code=code_matcher(pattern))


def _rows(classification):
    return [tuple(r) for r in classification.rows]


@pytest.fixture(autouse=True)
def _vocabulary(monkeypatch):
    """C2's one context builder (aliases.context), on a fixed vocabulary instead of this env's attributes export."""
    from stone_pipeline.aliases import context
    monkeypatch.setattr(context, "_clear_type_words", lambda: frozenset({"granite", "marble"}))
    monkeypatch.setattr(context, "vocabulary_keys", lambda: frozenset({"black", "white", "polished", "granite",
                                                                       "marble"}))


@pytest.fixture
def ledger(tmp_path):
    with Ledger.open(tmp_path / "l.db", env="development") as lg:
        yield lg


# --- the precondition --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("state, abandoned", [("dirty", False), ("syncing", False), ("gap_held", False),
                                              ("pending", False), ("synced", True)])
def test_the_file_is_refused_unless_every_variation_is_synced_naming_the_keys(ledger, state, abandoned):
    _variation(ledger, ABS, "Absolute Black", "Granite")
    _variation(ledger, KASH, "Kashmir White", "Granite", state=state, abandoned=abandoned)
    with pytest.raises(cleanup.NotAllSynced) as refused:
        cleanup.classify(ledger, _stores())
    assert refused.value.keys == [KASH] and KASH in str(refused.value)


def test_the_file_is_refused_while_a_served_set_is_not_computed_naming_the_keys(ledger):
    # P2-10 C4 (ledger v18): a row's alias_rows/codes are NULL until a produce computes them; the file would not
    # describe Medusa, so it refuses as it does for an unsynced row (the same 409 naming the Keys)
    _variation(ledger, ABS, "Absolute Black", "Granite", aliases=["Nero Assoluto"])
    _variation(ledger, KASH, "Kashmir White", "Granite", computed=False)
    with pytest.raises(cleanup.NotAllComputed, match=KASH) as raised:
        cleanup.classify(ledger, _stores(seed={ABS: ["Nero Assoluto"]}))
    assert raised.value.keys == [KASH] and isinstance(raised.value, cleanup.NotAllSynced)


def test_the_v18_served_set_gives_the_pre_c4_file_bytes(ledger):
    # the 249 rev 3 contract is unchanged by C4: the same served values, read from alias_rows + codes instead of the
    # dropped aliases column, give the same bytes the pre-C4 ledger gave (SC #642's expectations, kept verbatim)
    _variation(ledger, KASH, "Kashmir White", "Granite", aliases=["Kashmir, Bianco"])
    _variation(ledger, ABS, "Absolute Black", "Granite", aliases=["G654", "Shanxi Black", "Nero Assoluto"])
    _variation(ledger, CARR, "Carrara", "Marble")
    stores = _stores(seed={ABS: ["Shanxi Black", "Nero Assoluto"], KASH: ["Kashmir, Bianco"]})
    assert cleanup.csv_bytes(cleanup.classify(ledger, stores)) == (
        f"Key,Value,Class\n{ABS},G654,code\n{ABS},Nero Assoluto,seed\n{ABS},Shanxi Black,seed\n"
        f'{KASH},"Kashmir, Bianco",seed\n{CARR},,none\n').encode("utf-8")


# --- the classes -------------------------------------------------------------------------------------------------------
def test_a_key_with_no_value_is_the_one_none_row(ledger):
    _variation(ledger, ABS, "Absolute Black", "Granite")
    assert _rows(cleanup.classify(ledger, _stores())) == [(ABS, "", "none")]


def test_operator_beats_seed_beats_learned(ledger):
    _variation(ledger, ABS, "Absolute Black", "Granite", aliases=["Nero Assoluto", "Shanxi Black", "Jet Black"])
    stores = _stores(seed={ABS: ["Nero Assoluto", "Shanxi Black"]}, learned={ABS: ["Shanxi Black", "Jet Black"]},
                     operator={("Absolute Black", "Granite"): ["Nero Assoluto"]})
    assert _rows(cleanup.classify(ledger, stores)) == [
        (ABS, "Jet Black", "learned"), (ABS, "Nero Assoluto", "operator"), (ABS, "Shanxi Black", "seed")]


def test_an_operator_alias_stated_without_a_type_names_the_variety_by_its_name(ledger):
    _variation(ledger, ABS, "Absolute Black", "Granite", aliases=["Nero Assoluto"])
    stores = _stores(operator={("Absolute Black", ""): ["Nero Assoluto"]})
    assert _rows(cleanup.classify(ledger, stores)) == [(ABS, "Nero Assoluto", "operator")]


def test_a_code_shaped_value_is_a_code_whoever_owns_it(ledger):
    _variation(ledger, ABS, "Absolute Black", "Granite", aliases=["G654", "Nero Assoluto"])
    stores = _stores(seed={ABS: ["Nero Assoluto"]}, operator={("Absolute Black", "Granite"): ["G654"]})
    assert _rows(cleanup.classify(ledger, stores)) == [(ABS, "G654", "code"), (ABS, "Nero Assoluto", "seed")]


def test_a_seed_or_learned_value_the_alias_check_refuses_is_removed_an_operator_one_is_kept(ledger):
    # own_name for the seed one, bare_vocabulary_word for the learned one; an operator's row is never changed
    _variation(ledger, ABS, "Absolute Black", "Granite", aliases=["Absolute  black", "Black", "White"])
    stores = _stores(seed={ABS: ["Absolute Black"]}, learned={ABS: ["Black"]},
                     operator={("Absolute Black", "Granite"): ["White"]})
    result = cleanup.classify(ledger, stores)
    assert _rows(result) == [(ABS, "Absolute  black", "removed"), (ABS, "Black", "removed"), (ABS, "White", "operator")]
    assert [(k, v, c, r) for k, v, c, r in result.differences] == [
        (ABS, "Absolute  black", "removed", "own_name"), (ABS, "Black", "removed", "bare_vocabulary_word")]


def test_a_same_type_name_is_judged_against_the_other_live_varieties(ledger):
    _variation(ledger, ABS, "Absolute Black", "Granite", aliases=["Kashmir White"])
    _variation(ledger, KASH, "Kashmir White", "Granite")
    stores = _stores(seed={ABS: ["Kashmir White"]})
    assert (ABS, "Kashmir White", "removed") in _rows(cleanup.classify(ledger, stores))


def test_a_served_value_no_store_owns_refuses_and_is_listed(ledger):
    _variation(ledger, ABS, "Absolute Black", "Granite", aliases=["Nero Assoluto", "Mystery Stone"])
    result = cleanup.classify(ledger, _stores(seed={ABS: ["Nero Assoluto"]}))
    assert [d for d in result.differences if d[2] == "refused"] == [(ABS, "Mystery Stone", "refused", "no owner store")]
    with pytest.raises(cleanup.Unclassifiable, match="Mystery Stone"):
        cleanup.require_whole(result)


def test_a_value_with_an_empty_key_refuses(ledger):
    _variation(ledger, ABS, "Absolute Black", "Granite", aliases=["---"])
    result = cleanup.classify(ledger, _stores(seed={ABS: ["---"]}))
    assert [d[2:] for d in result.differences] == [("refused", "empty key")]


def test_one_row_per_key_and_key_of_the_value(ledger):
    _variation(ledger, ABS, "Absolute Black", "Granite", aliases=["Nero Assoluto", "nero-assoluto", "NERO ASSOLUTO"])
    rows = _rows(cleanup.classify(ledger, _stores(seed={ABS: ["Nero Assoluto"]})))
    assert rows == [(ABS, "NERO ASSOLUTO", "seed")]                   # one row, the least spelling (deterministic)


# --- the Keys: served rows and pending tombstones ------------------------------------------------------------------------
def test_a_retiring_variety_with_a_pending_tombstone_carries_its_last_set(ledger):
    _variation(ledger, ABS, "Absolute Black", "Granite", aliases=["Nero Assoluto"])
    _variation(ledger, CARR, "Carrara", "Marble", aliases=["Bianco Carrara"], state="retiring")
    _tombstone(ledger, CARR)
    result = cleanup.classify(ledger, _stores(seed={ABS: ["Nero Assoluto"], CARR: ["Bianco Carrara"]}))
    assert (CARR, "Bianco Carrara", "seed") in _rows(result)
    assert (result.variations, result.tombstoned) == (1, 1)


def test_a_pending_tombstone_with_no_row_left_refuses(ledger):
    _variation(ledger, ABS, "Absolute Black", "Granite")
    _tombstone(ledger, CARR)
    with pytest.raises(cleanup.Unclassifiable, match=CARR):
        cleanup.require_whole(cleanup.classify(ledger, _stores()))


# --- the file ----------------------------------------------------------------------------------------------------------
def test_the_file_is_utf8_no_bom_lf_minimal_quoting_sorted_by_key_then_value_key(ledger):
    _variation(ledger, KASH, "Kashmir White", "Granite", aliases=["Kashmir, Bianco"])
    _variation(ledger, ABS, "Absolute Black", "Granite", aliases=["Shanxi Black", "Nero Assoluto"])
    stores = _stores(seed={ABS: ["Shanxi Black", "Nero Assoluto"], KASH: ["Kashmir, Bianco"]})
    body = cleanup.csv_bytes(cleanup.classify(ledger, stores))
    assert not body.startswith(b"\xef\xbb\xbf") and b"\r" not in body
    assert body.decode("utf-8") == (f"Key,Value,Class\n{ABS},Nero Assoluto,seed\n{ABS},Shanxi Black,seed\n"
                                    f'{KASH},"Kashmir, Bianco",seed\n')


def test_the_manifest_field_is_exact(ledger):
    _variation(ledger, ABS, "Absolute Black", "Granite")
    _variation(ledger, CARR, "Carrara", "Marble", state="retiring")
    _tombstone(ledger, CARR)
    result = cleanup.classify(ledger, _stores())
    assert cleanup.manifest_field(result, "2026-11-13T10:00:00Z") == {
        "complete": True, "basis": "all variations synced", "keys": "every served Key and pending tombstone",
        "values": "current acked set", "variations": 1, "tombstoned": 1, "generated_at": "2026-11-13T10:00:00Z"}
    named = {r[0] for r in result.rows}
    assert len(named) == result.variations + result.tombstoned


def test_two_hash_seeds_give_byte_identical_files(tmp_path):
    path = tmp_path / "l.db"
    with Ledger.open(path, env="development") as lg:
        _variation(lg, ABS, "Absolute Black", "Granite", aliases=["Shanxi Black", "Nero Assoluto", "G654"])
        _variation(lg, KASH, "Kashmir White", "Granite", aliases=["Bianco Kashmir"])
    script = (
        "import sys, json\n"
        "from stone_pipeline.tests.test_alias_classification import _stores, ABS, KASH\n"
        "from stone_pipeline.aliases import cleanup\n"
        "from stone_pipeline.ledger.db import Ledger\n"
        "with Ledger.open(sys.argv[1], env='development') as lg:\n"
        "    r = cleanup.classify(lg, _stores(seed={ABS: ['Shanxi Black', 'Nero Assoluto'], KASH: ['Bianco Kashmir']}))\n"
        "sys.stdout.buffer.write(cleanup.csv_bytes(r) + cleanup.differences_bytes(r))\n")
    outs = [subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True, check=True, cwd=REPO,
                           env={**os.environ, "PYTHONHASHSEED": seed}).stdout for seed in ("1", "2")]
    assert outs[0] == outs[1] and outs[0]


def test_a_second_run_changes_nothing_and_reads_the_ledger_only(ledger):
    _variation(ledger, ABS, "Absolute Black", "Granite", aliases=["Nero Assoluto"])
    before = [dict(r) for r in ledger.execute("SELECT * FROM variation")]
    stores = _stores(seed={ABS: ["Nero Assoluto"]})
    first, second = cleanup.classify(ledger, stores), cleanup.classify(ledger, stores)
    assert cleanup.csv_bytes(first) == cleanup.csv_bytes(second)
    assert [dict(r) for r in ledger.execute("SELECT * FROM variation")] == before


def test_the_difference_list_names_each_removed_and_refused_value():
    result = cleanup.Classification(rows=(), variations=0, tombstoned=0, differences=(
        (ABS, "Black", "removed", "bare_vocabulary_word"), (ABS, "Mystery", "refused", "no owner store")))
    assert cleanup.differences_bytes(result).decode("utf-8") == (
        f"Key,Value,Class,Reason\n{ABS},Black,removed,bare_vocabulary_word\n{ABS},Mystery,refused,no owner store\n")


# --- the stores --------------------------------------------------------------------------------------------------------
def test_the_stores_are_the_base_the_backbone_the_global_renames_and_the_active_learned_rows(ledger, tmp_path,
                                                                                           monkeypatch):
    from stone_pipeline.config import decisions_store
    base = tmp_path / "base.csv"
    base.write_text(f"Key,Name,Type,Aliases\n{ABS},Absolute Black,Granite,Nero Assoluto|Shanxi Black\n",
                    encoding="utf-8")
    backbone = tmp_path / "backbone_varieties.json"
    backbone.write_text(json.dumps({"posts": [{"key": ABS, "variant": "Absolute Black", "stone_type": "Granite",
                                               "aliases": ["Black Galaxy"]}]}), encoding="utf-8")
    additions = tmp_path / "varieties.json"
    additions.write_text(json.dumps([{"key": KASH, "variant": "Kashmir White", "stone_type": "Granite",
                                      "aliases": ["Bianco Kashmir"], "categories": ["slab"]},
                                     {"key": "", "variant": "Avocatus", "stone_type": "", "aliases": ["Avo"],
                                      "categories": ["slab"]}]), encoding="utf-8")
    monkeypatch.setattr(cleanup, "_seed_files", lambda: (base, [backbone, additions]))
    monkeypatch.setattr(decisions_store, "statement_rows", lambda: [
        {"source": "", "spelling_norm": "nero assoluto", "spelling": "Nero Assoluto", "verdict": "is",
         "name": "Absolute Black", "stone_type": "Granite"},
        {"source": "", "spelling_norm": "absolute black", "spelling": "Absolute Black", "verdict": "is",
         "name": "Absolute Black", "stone_type": "Granite"},                       # not renamed: no alias
        {"source": "pol", "spelling_norm": "abs blk", "spelling": "ABS BLK", "verdict": "is",
         "name": "Absolute Black", "stone_type": "Granite"},                       # a vendor binding: match-only
        {"source": "", "spelling_norm": "jet", "spelling": "Jet", "verdict": "reject", "name": None,
         "stone_type": None}])
    monkeypatch.setattr(decisions_store, "active_learned_aliases", lambda: [
        {"identity_key": ABS, "value": "Jet Black", "norm": "jet black", "method": "phonetic", "source": "pol",
         "status": "active", "first_seen": "", "decided_by": "migration", "decided_at": ""}], raising=False)
    stores = cleanup.load_stores()
    assert stores.seed == {ABS: frozenset({"nero assoluto", "shanxi black", "black galaxy"}),
                           KASH: frozenset({"bianco kashmir"})}
    assert stores.operator == {("absolute black", "granite"): frozenset({"nero assoluto"})}
    assert stores.learned == {ABS: frozenset({"jet black"})}


# --- the route ----------------------------------------------------------------------------------------------------------
class _S3:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.versions: dict[str, int] = {}

    def get_object(self, Bucket, Key):
        from botocore.exceptions import ClientError
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def head_object(self, Bucket, Key):
        return {"VersionId": f"v{self.versions.get(Key, 0)}"}

    def put_object(self, Bucket, Key, Body, ContentType=None, ChecksumSHA256=None):
        self.objects[Key] = Body if isinstance(Body, bytes) else Body.read()
        self.versions[Key] = self.versions.get(Key, 0) + 1
        return {"VersionId": f"v{self.versions[Key]}"}


@pytest.fixture
def service(tmp_path, monkeypatch):
    """The sync service's view: a live ledger, a to_upload folder already published by a produce, a bucket."""
    from stone_pipeline.io import deliverables
    path = tmp_path / "dev.ledger"
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(path))
    with Ledger.open(path, env="development") as lg:
        _variation(lg, ABS, "Absolute Black", "Granite", aliases=["Nero Assoluto"])
    up = tmp_path / "to_upload"
    up.mkdir()
    (up / "1_variants_full.csv").write_text("Key,Name\n", encoding="utf-8")
    (up / "SYNC_STEPS.md").write_text("steps\n", encoding="utf-8")         # a side file the route must carry too
    s3 = _S3()
    monkeypatch.setattr(cleanup, "_to_upload_dir", lambda: up)
    monkeypatch.setattr(cleanup, "_review_dir", lambda: tmp_path / "review")
    monkeypatch.setattr(cleanup, "_export_file", lambda: tmp_path / "no_export.csv")
    monkeypatch.setattr(cleanup, "_s3_client", lambda: s3)
    monkeypatch.setattr(cleanup, "load_stores", lambda: _stores(seed={ABS: ["Nero Assoluto"]}))
    deliverables.publish_deliverables(s3, "bucket", up, "dev/scraper/to_upload", "R1")
    return {"path": path, "up": up, "s3": s3, "prefix": "dev/scraper/to_upload"}


def _manifest(service):
    return json.loads(service["s3"].objects[f"{service['prefix']}/manifest.json"])


def test_the_route_publishes_the_file_and_the_field_and_keeps_the_other_entries(service, monkeypatch):
    from stone_pipeline import lifecycle
    monkeypatch.setattr(cleanup, "_bucket_prefix", lambda: ("bucket", service["prefix"]))
    body, code = lifecycle.alias_classification()
    manifest = _manifest(service)
    assert code == 200 and set(manifest["files"]) == {"1_variants_full.csv", cleanup.FILE_NAME}
    assert manifest["run_id"] == "R1"                                    # the produce's run id stays
    field = manifest[cleanup.MANIFEST_FIELD]
    assert (field["variations"], field["tombstoned"], field["complete"]) == (1, 0, True)
    assert body["generated_at"] == field["generated_at"]
    assert service["s3"].objects[f"{service['prefix']}/{cleanup.FILE_NAME}"] == \
        f"Key,Value,Class\n{ABS},Nero Assoluto,seed\n".encode()


def test_an_empty_local_to_upload_keeps_every_published_entry_and_adds_the_file(service, monkeypatch):
    # after a dev roll the task's to_upload is empty until the next produce: the route merges into the PUBLISHED
    # manifest and never reads (or writes) the local folder, so no produce deliverable drops out
    from stone_pipeline import lifecycle
    monkeypatch.setattr(cleanup, "_bucket_prefix", lambda: ("bucket", service["prefix"]))
    before = _manifest(service)
    for path in service["up"].iterdir():
        path.unlink()
    body, code = lifecycle.alias_classification()
    after = _manifest(service)
    assert code == 200
    assert {k: v for k, v in after.items() if k not in ("files", cleanup.MANIFEST_FIELD)} == \
        {k: v for k, v in before.items() if k != "files"}          # run_id, produced_at, side_files, contract, env
    assert {n: e for n, e in after["files"].items() if n != cleanup.FILE_NAME} == before["files"]
    assert after["files"][cleanup.FILE_NAME]["sha256"] == body["sha256"]
    assert list(service["up"].iterdir()) == []


@pytest.mark.parametrize("published, code", [(None, 409), (b"not json", 500),
                                             (b'{"contract": "v0", "files": {}}', 500)])
def test_a_missing_or_unreadable_published_manifest_refuses_and_publishes_nothing(service, monkeypatch, published,
                                                                                 code):
    # never a manifest of one: the route only adds to what a produce published
    from stone_pipeline import lifecycle
    monkeypatch.setattr(cleanup, "_bucket_prefix", lambda: ("bucket", service["prefix"]))
    key = f"{service['prefix']}/manifest.json"
    if published is None:
        del service["s3"].objects[key]
    else:
        service["s3"].objects[key] = published
    before = dict(service["s3"].objects)
    body, got = lifecycle.alias_classification()
    assert got == code and key in body["error"] and service["s3"].objects == before


def test_a_second_call_on_an_unchanged_ledger_is_byte_identical_but_generated_at(service, monkeypatch):
    from stone_pipeline import lifecycle
    monkeypatch.setattr(cleanup, "_bucket_prefix", lambda: ("bucket", service["prefix"]))
    times = iter(["2026-11-12T14:00:00Z", "2026-11-12T14:05:00Z"])
    monkeypatch.setattr(cleanup, "_now", lambda: next(times))
    manifest_key = f"{service['prefix']}/manifest.json"
    file_key = f"{service['prefix']}/{cleanup.FILE_NAME}"
    lifecycle.alias_classification()
    first, uploads = service["s3"].objects[manifest_key], service["s3"].versions[file_key]
    lifecycle.alias_classification()
    second = service["s3"].objects[manifest_key]
    assert second != first
    assert second == first.replace(b"2026-11-12T14:00:00Z", b"2026-11-12T14:05:00Z")
    assert service["s3"].versions[file_key] == uploads                  # unchanged bytes are not uploaded again


def test_the_route_refuses_while_a_variation_is_not_synced_and_publishes_nothing(service, monkeypatch):
    from stone_pipeline import lifecycle
    monkeypatch.setattr(cleanup, "_bucket_prefix", lambda: ("bucket", service["prefix"]))
    with Ledger.open(service["path"], env="development") as lg:
        _variation(lg, KASH, "Kashmir White", "Granite", state="dirty")
    before = dict(service["s3"].objects)
    body, code = lifecycle.alias_classification()
    assert code == 409 and body["keys"] == [KASH] and service["s3"].objects == before


def test_the_route_never_writes_the_ledger(service, monkeypatch):
    from stone_pipeline import lifecycle
    monkeypatch.setattr(cleanup, "_bucket_prefix", lambda: ("bucket", service["prefix"]))
    with Ledger.open(service["path"], env="development") as lg:
        before = {t: [dict(r) for r in lg.execute(f"SELECT * FROM {t}")] for t in ("variation", "removed", "ledger_meta")}
    lifecycle.alias_classification()
    with Ledger.open(service["path"], env="development") as lg:
        assert {t: [dict(r) for r in lg.execute(f"SELECT * FROM {t}")] for t in before} == before


def test_the_route_is_dispatched(monkeypatch):
    from stone_pipeline import lifecycle
    from stone_pipeline.config.control.server import dispatch
    monkeypatch.setattr(lifecycle, "alias_classification", lambda: ({"rows": 1}, 200))
    assert dispatch("POST", ["aliases", "classification"], None) == (200, {"rows": 1})
    assert dispatch("GET", ["aliases", "classification"], None)[0] == 405


def test_a_produce_drops_a_stale_classification_file(tmp_path, monkeypatch):
    # a produce moves the ledger on, so the file no longer describes it: the next publish must not carry it
    import dataclasses
    from stone_pipeline import catalog
    from stone_pipeline.config.settings import SETTINGS
    up = tmp_path / "to_upload"
    up.mkdir()
    (up / cleanup.FILE_NAME).write_text("Key,Value,Class\n", encoding="utf-8")
    monkeypatch.setattr(catalog, "SETTINGS", dataclasses.replace(
        SETTINGS, paths=dataclasses.replace(SETTINGS.paths, to_upload_dir=up)))
    (tmp_path / "outputs").mkdir()
    catalog.collect_products(tmp_path / "outputs")
    assert not (up / cleanup.FILE_NAME).exists()


def test_csv_reads_back_as_written(ledger):
    _variation(ledger, ABS, "Absolute Black", "Granite", aliases=['Say "Nero"'])
    body = cleanup.csv_bytes(cleanup.classify(ledger, _stores(seed={ABS: ['Say "Nero"']})))
    assert list(csv.reader(io.StringIO(body.decode("utf-8")))) == [["Key", "Value", "Class"],
                                                                   [ABS, 'Say "Nero"', "seed"]]


# --- the one `removed` judgement, shared with the seed rewrite (Scraper seed, 11-12) --------------------------------------
def test_seed_refusal_is_the_c1_reason_and_a_code_is_never_a_refusal():
    from stone_pipeline.aliases.context import build_contexts
    is_code = code_matcher(r"[Gg]\d+[A-Za-z]?")
    context = build_contexts([(ABS, "Granite", "Absolute Black"), (KASH, "Granite", "Kashmir White")])[ABS]
    assert cleanup.seed_refusal("G654", context, is_code) is None              # a code moves to `codes`, never removed
    assert cleanup.seed_refusal("Black", context, is_code) == "bare_vocabulary_word"
    assert cleanup.seed_refusal("Kashmir White", context, is_code) == "same_type_name"
    assert cleanup.seed_refusal("Nero Assoluto", context, is_code) is None


def test_c5_judges_with_c2_s_builder_over_the_live_varieties(ledger):
    from stone_pipeline.aliases.context import build_contexts
    _variation(ledger, ABS, "Absolute Black", "Granite", aliases=["Black", "Kashmir White", "Nero Assoluto", "G654"])
    _variation(ledger, KASH, "Kashmir White", "Granite")
    _variation(ledger, CARR, "Carrara", "Marble", state="retiring")
    _tombstone(ledger, CARR)
    stores = _stores(seed={ABS: ["Black", "Kashmir White", "Nero Assoluto", "G654"]})
    removed = {(k, v): why for k, v, cls, why in cleanup.classify(ledger, stores).differences if cls == "removed"}
    context = build_contexts([(ABS, "Granite", "Absolute Black"), (KASH, "Granite", "Kashmir White")])[ABS]
    expected = {(ABS, v): why for v in ("Black", "Kashmir White", "Nero Assoluto", "G654")
                if (why := cleanup.seed_refusal(v, context, stores.is_code))}
    assert removed == expected == {(ABS, "Black"): "bare_vocabulary_word", (ABS, "Kashmir White"): "same_type_name"}


# --- the open review items (alias_cleanup_open.csv; agreed with Scraper C6, 11-12) ---------------------------------------
def test_the_open_items_file_is_header_only_when_empty_and_sorted():
    from stone_pipeline.aliases import open_items
    assert open_items.open_items_bytes([]) == b"kind,Key,Value,reason\n"
    body = open_items.open_items_bytes([("digit_non_code", KASH, "Bianco 2000", "digit_not_code"),
                                        ("admin_refused", ABS, "Black", "bare_vocabulary_word"),
                                        ("digit_non_code", ABS, "Nero 3", "digit_not_code")])
    assert body.decode("utf-8") == ("kind,Key,Value,reason\n"
                                    f"admin_refused,{ABS},Black,bare_vocabulary_word\n"
                                    f"digit_non_code,{ABS},Nero 3,digit_not_code\n"
                                    f"digit_non_code,{KASH},Bianco 2000,digit_not_code\n")
    with pytest.raises(ValueError, match="kind"):
        open_items.open_items_bytes([("other", ABS, "x", "y")])


def test_the_open_items_read_back_and_a_bad_header_is_named(tmp_path):
    from stone_pipeline.aliases import open_items
    path = tmp_path / open_items.OPEN_FILE
    assert open_items.read_open_items(path) is None                          # absent
    path.write_bytes(open_items.open_items_bytes([("digit_non_code", ABS, "Nero 3", "digit_not_code")]))
    assert open_items.read_open_items(path) == [
        {"kind": "digit_non_code", "key": ABS, "value": "Nero 3", "reason": "digit_not_code"}]
    path.write_text("kind,Key,Value\n", encoding="utf-8")
    with pytest.raises(open_items.OpenItemsUnreadable, match=open_items.OPEN_FILE):
        open_items.read_open_items(path)


def test_a_digit_bearing_alias_that_is_not_a_code_is_an_open_item(ledger):
    _variation(ledger, ABS, "Absolute Black", "Granite", aliases=["Nero 3", "G654", "Shanxi Black", "Black"])
    _variation(ledger, KASH, "Kashmir White", "Granite", aliases=["Bianco 2000"])
    stores = _stores(seed={ABS: ["Shanxi Black", "Black"], KASH: ["Bianco 2000"]},
                     operator={("Absolute Black", "Granite"): ["Nero 3"]})
    assert cleanup.digit_items(cleanup.classify(ledger, stores)) == [
        ("digit_non_code", ABS, "Nero 3", "digit_not_code"), ("digit_non_code", KASH, "Bianco 2000", "digit_not_code")]


def test_an_admin_alias_failing_the_boundary_is_an_open_item(tmp_path):
    export = tmp_path / "variants_export.csv"
    export.write_text("Id,Key,Name,Type,Aliases,AdminAliases,AdminCodes\n"
                      f"v1,{ABS},Absolute Black,Granite,,Black|Nero Assoluto|G602,\n"
                      f"v2,{KASH},Kashmir White,Granite,,Kashmir White Granite,\n", encoding="utf-8")
    items = cleanup.admin_items(export, code_matcher(r"[Gg]\d+[A-Za-z]?"))
    assert items == [("admin_refused", ABS, "Black", "bare_vocabulary_word"), ("admin_refused", ABS, "G602", "is_code"),
                     ("admin_refused", KASH, "Kashmir White Granite", "own_name_type_word")]


def test_an_export_without_admin_aliases_gives_no_admin_items_and_says_so(tmp_path, caplog):
    export = tmp_path / "variants_export.csv"
    export.write_text(f"Id,Key,Name,Type,Aliases\nv1,{ABS},Absolute Black,Granite,Black\n", encoding="utf-8")
    assert cleanup.admin_items(export, code_matcher(None)) is None
    assert "AdminAliases" in caplog.text


def test_the_route_writes_the_open_items_and_reports_the_admin_rows(service, monkeypatch, tmp_path):
    from stone_pipeline import lifecycle
    from stone_pipeline.aliases import open_items
    monkeypatch.setattr(cleanup, "_bucket_prefix", lambda: ("bucket", service["prefix"]))
    monkeypatch.setattr(cleanup, "_export_file", lambda: tmp_path / "absent_export.csv")
    with Ledger.open(service["path"], env="development") as lg:
        _variation(lg, KASH, "Kashmir White", "Granite", aliases=["Bianco 2000"])
    monkeypatch.setattr(cleanup, "load_stores", lambda: _stores(seed={ABS: ["Nero Assoluto"], KASH: ["Bianco 2000"]}))
    body, code = lifecycle.alias_classification()
    assert code == 200 and (body["open_items"], body["admin_rows"]) == (1, "absent")
    assert open_items.read_open_items(tmp_path / "review" / open_items.OPEN_FILE) == [
        {"kind": "digit_non_code", "key": KASH, "value": "Bianco 2000", "reason": "digit_not_code"}]


def test_a_non_latin_value_is_classed_on_its_own_row_never_merged_with_its_latin_part(ledger):
    # Mas, 13 Nov: values key by alias_key. match_key reads a pure-CJK value as empty (the whole file refused) and a
    # mixed one as its Latin part (merged into that row); alias_key keeps the script, so each is its own row
    alias_key = getattr(text, "alias_key", match_key)
    values = ["Wratza 维纳多", "Wratza", "巴比伦金"]
    _variation(ledger, ABS, "Absolute Black", "Granite", aliases=values)
    stores = cleanup.Stores(seed={ABS: frozenset(alias_key(v) for v in values)}, operator={}, learned={},
                            is_code=code_matcher(r"[Gg]\d+[A-Za-z]?"))
    result = cleanup.classify(ledger, stores)
    assert [d for d in result.differences if d[2] == "refused"] == []
    assert _rows(result) == [(ABS, "Wratza", "seed"), (ABS, "Wratza 维纳多", "seed"),
                             (ABS, "巴比伦金", "seed")]
