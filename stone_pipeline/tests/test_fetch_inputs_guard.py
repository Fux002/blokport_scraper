"""The attribute vocabulary is a committed cold-start seed; the S3 fetch must not overwrite it with a
thin/empty Medusa export (mid-reset), or nothing resolves. Guards _would_clobber + the protected set."""

from __future__ import annotations

import boto3
import pytest
from boto3.exceptions import RetriesExceededError
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError

from stone_pipeline.io import medusa_inputs as fetch_inputs
from stone_pipeline.config import settings
from stone_pipeline.reference import sync_variants_base


def test_attributes_is_refused_thin_never_kept_as_a_protected_base():
    # the vocabulary's thin copy is refused by every fetch (_take_attributes; test_attributes_unavailable), so it is
    # not among the files whose local copy is kept
    assert "attributes.csv" not in fetch_inputs._PROTECTED_BASE


def test_variant_exports_are_protected_bases():
    # the live matching reference and the committed full base must not be clobbered by a thin S3 export
    # (Medusa mid-reset): else the matcher starts from a thin set and treats the catalog as new.
    assert "variants_export.csv" in fetch_inputs._PROTECTED_BASE
    assert "variants_export_base.csv" in fetch_inputs._PROTECTED_BASE


def test_would_clobber_blocks_a_thin_overwrite_but_allows_growth():
    assert fetch_inputs._would_clobber(3, 1000)          # thin S3 copy vs full local -> keep local
    assert not fetch_inputs._would_clobber(1200, 1000)   # fuller S3 copy -> take it
    assert not fetch_inputs._would_clobber(600, 1000)    # within tolerance -> normal churn
    assert not fetch_inputs._would_clobber(0, 0)         # no local baseline -> nothing to protect


class _FakeS3:
    """Writes preset CSV bodies to the download target so fetch_attributes runs with no network; `error` is raised
    instead (an S3 error, or anything else)."""

    def __init__(self, body: str | None = None, error: Exception | None = None):
        self._body, self._error = body, error

    def download_file(self, bucket, key, filename):
        from pathlib import Path
        if self._error is not None:
            raise self._error
        Path(filename).write_text(self._body, encoding="utf-8")


_NO_SUCH_KEY = ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")


def _committed(tmp_path, lines: int):
    """A tmp attributes.csv seeded with `lines` colour rows (the committed baseline stand-in)."""
    target = tmp_path / "attributes.csv"
    target.write_text("category,value,sourceid,category_handles\n" + "".join(
        f"color,c{i},id{i},\n" for i in range(lines)), encoding="utf-8")
    return target


def _leftovers(folder):
    return sorted(p.name for p in folder.iterdir() if p.name.endswith(".incoming"))


def test_two_fetches_at_once_each_download_to_their_own_file_and_neither_fails(tmp_path):
    # review of #556: every page GET fetches attributes.csv; two at once shared one .incoming path, so the second
    # replace found the first one's file gone (FileNotFoundError, a 500)
    import threading
    target = _committed(tmp_path, 26)
    live = "category,value,sourceid,category_handles\n" + "".join(f"color,c{i},id{i},\n" for i in range(38))
    both = threading.Barrier(2)

    class _Together(_FakeS3):
        def download_file(self, bucket, key, filename):
            super().download_file(bucket, key, filename)
            both.wait(timeout=5)                                  # both downloaded before either replaces

    out, errors = [], []

    def fetch():
        try:
            fetch_inputs.fetch_attributes(client=_Together(live), target=target)
            out.append("written")
        except Exception as exc:                                 # what the request saw
            errors.append(exc)
    threads = [threading.Thread(target=fetch) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and out == ["written", "written"] and target.read_text().count("color,") == 38
    assert _leftovers(tmp_path) == []


def test_fetch_attributes_writes_a_fuller_live_copy(tmp_path):
    target = _committed(tmp_path, 26)
    live = "category,value,sourceid,category_handles\n" + "".join(f"color,c{i},id{i},\n" for i in range(38))
    fetch_inputs.fetch_attributes(client=_FakeS3(live), target=target)
    assert target.read_text().count("color,") == 38          # replaced with the live vocab
    assert _leftovers(tmp_path) == []                         # temp cleaned up


def test_fetch_attributes_refuses_a_thin_copy_and_leaves_the_local_file(tmp_path):
    # one path with the produce fetch: a thin copy is refused LOUD, never kept quietly as if the local file were fresh
    target = _committed(tmp_path, 100)
    before = target.read_text()
    thin = "category,value,sourceid,category_handles\ncolor,c0,id0,\n"          # 2 lines << 50% of 101
    with pytest.raises(RuntimeError, match="attributes.csv.*thin"):
        fetch_inputs.fetch_attributes(client=_FakeS3(thin), target=target)
    assert target.read_text() == before
    assert _leftovers(tmp_path) == []


@pytest.mark.parametrize("error", [_NO_SUCH_KEY, NoCredentialsError(), EndpointConnectionError(endpoint_url="x"),
                                   RetriesExceededError(last_exception=ConnectionError("body read"))])
def test_fetch_attributes_refuses_on_an_s3_error_and_leaves_the_local_file(tmp_path, error):
    target = _committed(tmp_path, 26)
    before = target.read_text()
    with pytest.raises(RuntimeError, match="attributes.csv"):
        fetch_inputs.fetch_attributes(client=_FakeS3(error=error), target=target)
    assert target.read_text() == before
    assert _leftovers(tmp_path) == []


def test_fetch_attributes_raises_an_unexpected_error(tmp_path):
    # a bug (or a full disk) raises as itself, not as an S3 refusal
    target = _committed(tmp_path, 26)
    with pytest.raises(RuntimeError, match="boom"):
        fetch_inputs.fetch_attributes(client=_FakeS3(error=RuntimeError("boom")), target=target)
    assert _leftovers(tmp_path) == []


def test_only_the_scraper_s_own_variants_are_kept_from_the_export(tmp_path):
    # Medusa's export also lists other vendors' variants (dev 2026-11-01: non-stone test products, np-cattle-...,
    # np-diesel-...): not ours to match, mint or sync, and the ledger refuses them, which aborted the produce
    export = tmp_path / "variants_export.csv"
    # the scraper's own are the export's `owner = scraper` rows (PHASE-1-SPEC D5)
    export.write_text("Id,Key,Name,Aliases,status,Type,owner\n"
                      "v1,marble__carrara_0001,Carrara,,active,Marble,scraper\n"
                      "v2,np-cattle-heifer-limousin-430,Heifer,,active,Cattle,\n"
                      "v3,marble__statuario_0002,Statuario,,active,Marble,scraper\n"
                      "v4,np-diesel-en590-summer,Diesel,,active,Fuel,\n", encoding="utf-8")
    assert fetch_inputs.own_variants(export) == 2
    lines = export.read_text(encoding="utf-8").splitlines()
    assert lines == ["Id,Key,Name,Aliases,status,Type,owner", "v1,marble__carrara_0001,Carrara,,active,Marble,scraper",
                     "v3,marble__statuario_0002,Statuario,,active,Marble,scraper"]
    assert fetch_inputs.own_variants(export) == 0                     # already clean: nothing to drop, same file


def test_both_variant_exports_are_kept_to_the_scraper_s_own_variants():
    assert fetch_inputs._VARIANT_EXPORTS == {"variants_export.csv", "variants_export_base.csv"}


def test_other_vendors_rows_never_make_a_thin_stone_export_look_full(tmp_path, monkeypatch):
    # Medusa mid-reset: few stone rows plus many foreign rows. Counted before filtering it passes the thin-copy guard
    # and replaces the full local export with an almost empty one; filtered first, the local export is kept.
    local = tmp_path / "variants_export.csv"
    header = "Id,Key,Name,Aliases,status,Type,owner\n"
    local.write_text(header + "".join(f"v{i},marble__x_{i:04d},X{i},,active,Marble,scraper\n" for i in range(10)),
                     encoding="utf-8")
    incoming = (header + "v1,marble__x_0001,X1,,active,Marble,scraper\n"
                + "".join(f"n{i},np-thing-{i},Thing {i},,active,Thing,\n" for i in range(30)))

    class _Client:
        def get_paginator(self, name):
            return type("P", (), {"paginate": lambda self, **k: [{"Contents": [{"Key": f"{fetch_inputs.ENV_SEGMENT}/scraper/from_medusa/variants_export.csv"}]}]})()

        def download_file(self, bucket, key, filename):
            open(filename, "w", encoding="utf-8").write(incoming)
    monkeypatch.setattr(boto3, "client", lambda *a, **k: _Client())
    monkeypatch.setattr(fetch_inputs, "SETTINGS", type("S", (), {"paths": type("P", (), {"from_medusa_dir": tmp_path})()})())
    fetch_inputs.fetch_all()
    assert local.read_text(encoding="utf-8").count("marble__x_") == 10       # the full local export stays


def _main_with(tmp_path, monkeypatch, name: str, incoming: str):
    """Run fetch_inputs.fetch_all against a fake S3 holding one object `name` with body `incoming`."""
    class _Client:
        def get_paginator(self, _):
            return type("P", (), {"paginate": lambda self, **k: [{"Contents": [{"Key": f"{fetch_inputs.ENV_SEGMENT}/scraper/from_medusa/{name}"}]}]})()

        def download_file(self, bucket, key, filename):
            open(filename, "w", encoding="utf-8").write(incoming)
    monkeypatch.setattr(boto3, "client", lambda *a, **k: _Client())
    monkeypatch.setattr(fetch_inputs, "SETTINGS", type("S", (), {"paths": type("P", (), {"from_medusa_dir": tmp_path})()})())
    fetch_inputs.fetch_all()


def _committed_base(tmp_path, rows=10):
    (tmp_path / "variants_export_base.csv").write_text(
        "Key,Name,Type\n" + "".join(f"slab_x_{i:04d},X,Marble\n" for i in range(rows)), encoding="utf-8")


def test_with_no_local_export_a_thin_one_with_rows_stops_the_fetch(tmp_path, monkeypatch):
    # after a roll the live export is absent (gitignored); the committed base lists the same variants. A thin S3 export
    # (Medusa mid-reset) must not become the reference, nor be dropped so the run looks like a cold start (tree_build
    # would publish an EMPTY valid-combinations set): the fetch fails, naming the file and both row counts
    _committed_base(tmp_path)
    with pytest.raises(SystemExit, match=r"variants_export\.csv.*1 row.*10 rows"):
        _main_with(tmp_path, monkeypatch, "variants_export.csv", "Id,Key\nv1,slab_x_0001\n")
    assert not (tmp_path / "variants_export.csv").exists()
    assert not (tmp_path / "variants_export.csv.incoming").exists()


def test_with_no_local_export_a_header_only_one_stays_absent(tmp_path, monkeypatch):
    # an export with no rows is the real cold start (Medusa has nothing yet): absent, the run goes on
    _committed_base(tmp_path)
    _main_with(tmp_path, monkeypatch, "variants_export.csv", "Id,Key\n")
    assert not (tmp_path / "variants_export.csv").exists()


def test_with_no_local_export_a_full_one_is_taken(tmp_path, monkeypatch):
    _committed_base(tmp_path)
    full = "Id,Key,Name,Type\n" + "".join(f"v{i},slab_x_{i:04d},X,Marble\n" for i in range(10))   # Name and Type
    _main_with(tmp_path, monkeypatch, "variants_export.csv", full)
    assert (tmp_path / "variants_export.csv").read_text(encoding="utf-8").count("slab_x_") == 10


def test_the_thin_copy_floor_is_one_setting():
    # the S3 fetch and the base write-back guard the same collapse with the same floor
    assert fetch_inputs.THIN_COPY_MIN_FRACTION == settings.THIN_COPY_MIN_FRACTION
    assert sync_variants_base.THIN_COPY_MIN_FRACTION == settings.THIN_COPY_MIN_FRACTION
    assert not hasattr(fetch_inputs, "_MIN_KEEP_FRACTION") and not hasattr(sync_variants_base, "_MIN_KEEP_FRACTION")


# settings parses attributes.csv at import, so a malformed S3 copy written over the good local file would stop every
# later produce at `import settings`, before its own fetch could heal it. The fetch validates before it replaces.
_MALFORMED_ATTRIBUTES = {
    "empty status cell": "category,value,sourceid,status,internal,category_handles\n" + "".join(
        f"color,c{i},id{i},active,false,\n" for i in range(30)) + "color,cx,idx,,false,\n",
    "conflicting duplicate": "category,value,sourceid,category_handles\n" + "".join(
        f"color,c{i},id{i},\n" for i in range(30)) + "color,C0,other,\n",
    "no category_handles column": "category,value,sourceid\n" + "".join(f"color,c{i},id{i}\n" for i in range(30)),
}


def test_fetch_attributes_refuses_a_malformed_copy_and_leaves_the_local_file(tmp_path):
    for problem, body in _MALFORMED_ATTRIBUTES.items():
        target = _committed(tmp_path, 26)
        before = target.read_text()
        with pytest.raises(fetch_inputs.AttributesUnavailable, match="attributes.csv"):
            fetch_inputs.fetch_attributes(client=_FakeS3(body), target=target)
        assert target.read_text() == before, problem
        assert _leftovers(tmp_path) == []


def test_the_produce_fetch_stops_loud_on_a_malformed_attributes_file(tmp_path, monkeypatch):
    for problem, body in _MALFORMED_ATTRIBUTES.items():
        local = _committed(tmp_path, 26)
        before = local.read_text()
        with pytest.raises(fetch_inputs.AttributesUnavailable, match="attributes.csv"):
            _main_with(tmp_path, monkeypatch, "attributes.csv", body)
        assert local.read_text() == before, problem                 # the good local file stays
        assert not local.with_suffix(".csv.incoming").exists()


def test_the_produce_fetch_never_writes_a_malformed_attributes_file_where_none_was(tmp_path, monkeypatch):
    with pytest.raises(fetch_inputs.AttributesUnavailable):
        _main_with(tmp_path, monkeypatch, "attributes.csv", _MALFORMED_ATTRIBUTES["empty status cell"])
    assert [p.name for p in tmp_path.iterdir()] == ["attributes.csv.unavailable"]     # only the recorded reason


def test_the_produce_fetch_takes_a_well_formed_attributes_file(tmp_path, monkeypatch):
    body = "category,value,sourceid,status,internal,category_handles\ncolor,Black,col_1,archived,false,\n"
    _main_with(tmp_path, monkeypatch, "attributes.csv", body)
    assert (tmp_path / "attributes.csv").read_text(encoding="utf-8") == body


def test_the_produce_fetch_stops_loud_on_a_malformed_variants_export(tmp_path, monkeypatch):
    malformed = {
        "empty status cell": "Id,Key,Name,Type,status\nv1,slab_x_0001,X,Marble,active\nv2,slab_x_0002,Y,Marble,\n",
        "no Name column": "Id,Key\nv1,slab_x_0001\nv2,slab_x_0002\n",
    }
    for problem, body in malformed.items():
        local = tmp_path / "variants_export.csv"
        local.write_text("Id,Key,Name\nv1,slab_x_0001,X\n", encoding="utf-8")
        with pytest.raises(ValueError, match="variants_export.csv"):
            _main_with(tmp_path, monkeypatch, "variants_export.csv", body)
        assert local.read_text(encoding="utf-8") == "Id,Key,Name\nv1,slab_x_0001,X\n", problem
        assert not local.with_suffix(".csv.incoming").exists()


def test_the_produce_fetch_takes_a_variants_export_with_status(tmp_path, monkeypatch):
    body = "Id,Key,Name,Type,status\nv1,slab_x_0001,X,Marble,archived\n"
    _main_with(tmp_path, monkeypatch, "variants_export.csv", body)
    assert (tmp_path / "variants_export.csv").read_text(encoding="utf-8") == body
