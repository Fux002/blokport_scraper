"""The attribute vocabulary is a committed cold-start seed; the S3 fetch must not overwrite it with a
thin/empty Medusa export (mid-reset), or nothing resolves. Guards _would_clobber + the protected set."""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError

from deploy import fetch_inputs
from stone_pipeline.config import settings
from stone_pipeline.reference import sync_variants_base


def test_attributes_is_a_protected_base():
    assert "attributes.csv" in fetch_inputs._PROTECTED_BASE


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
    target.write_text("category,value,sourceid\n" + "".join(
        f"color,c{i},id{i}\n" for i in range(lines)), encoding="utf-8")
    return target


def test_fetch_attributes_writes_a_fuller_live_copy(tmp_path):
    target = _committed(tmp_path, 26)
    live = "category,value,sourceid\n" + "".join(f"color,c{i},id{i}\n" for i in range(38))
    assert fetch_inputs.fetch_attributes(client=_FakeS3(live), target=target) is True
    assert target.read_text().count("color,") == 38          # replaced with the live vocab
    assert not target.with_suffix(".csv.incoming").exists()   # temp cleaned up


def test_fetch_attributes_keeps_committed_when_s3_is_thin(tmp_path):
    target = _committed(tmp_path, 100)
    thin = "category,value,sourceid\ncolor,c0,id0\n"          # 2 lines << 50% of 101 -> floor keeps committed
    assert fetch_inputs.fetch_attributes(client=_FakeS3(thin), target=target) is False
    assert target.read_text().count("color,") == 100          # committed seed preserved
    assert not target.with_suffix(".csv.incoming").exists()


@pytest.mark.parametrize("error", [_NO_SUCH_KEY, NoCredentialsError(), EndpointConnectionError(endpoint_url="x")])
def test_fetch_attributes_keeps_the_committed_vocab_on_an_s3_error_and_says_so(tmp_path, monkeypatch, error):
    errors: list[str] = []
    monkeypatch.setattr(fetch_inputs.log, "error", lambda msg, *a, **k: errors.append(msg))
    target = _committed(tmp_path, 26)
    before = target.read_text()
    assert fetch_inputs.fetch_attributes(client=_FakeS3(error=error), target=target) is False   # kept
    assert target.read_text() == before
    assert not target.with_suffix(".csv.incoming").exists()
    assert len(errors) == 1 and "attributes.csv" in errors[0]                  # loud, not silent


def test_fetch_attributes_raises_an_unexpected_error(tmp_path):
    # only an S3 failure keeps the committed vocab; a bug (or a full disk) is not hidden as "kept"
    target = _committed(tmp_path, 26)
    with pytest.raises(RuntimeError, match="boom"):
        fetch_inputs.fetch_attributes(client=_FakeS3(error=RuntimeError("boom")), target=target)
    assert not target.with_suffix(".csv.incoming").exists()


def test_only_the_scraper_s_own_variants_are_kept_from_the_export(tmp_path):
    # Medusa's export also lists other vendors' variants (dev 2026-11-01: non-stone test products, np-cattle-...,
    # np-diesel-...): not ours to match, mint or sync, and the ledger refuses them, which aborted the produce
    export = tmp_path / "variants_export.csv"
    export.write_text("Id,Key,Name,Image,Aliases\n"
                      "v1,slab_marble_carrara_0001,Carrara,,\n"
                      "v2,np-cattle-heifer-limousin-430,Heifer,,\n"
                      "v3,tile_marble_carrara_0001,Carrara,,\n"
                      "v4,np-diesel-en590-summer,Diesel,,\n", encoding="utf-8")
    assert fetch_inputs.own_variants(export) == 2
    lines = export.read_text(encoding="utf-8").splitlines()
    assert lines == ["Id,Key,Name,Image,Aliases", "v1,slab_marble_carrara_0001,Carrara,,", "v3,tile_marble_carrara_0001,Carrara,,"]
    assert fetch_inputs.own_variants(export) == 0                     # already clean: nothing to drop, same file


def test_both_variant_exports_are_kept_to_the_scraper_s_own_variants():
    assert fetch_inputs._VARIANT_EXPORTS == {"variants_export.csv", "variants_export_base.csv"}


def test_other_vendors_rows_never_make_a_thin_stone_export_look_full(tmp_path, monkeypatch):
    # Medusa mid-reset: few stone rows plus many foreign rows. Counted before filtering it passes the thin-copy guard
    # and replaces the full local export with an almost empty one; filtered first, the local export is kept.
    local = tmp_path / "variants_export.csv"
    local.write_text("Id,Key\n" + "".join(f"v{i},slab_x_{i:04d}\n" for i in range(10)), encoding="utf-8")
    incoming = "Id,Key\nv1,slab_x_0001\n" + "".join(f"n{i},np-thing-{i}\n" for i in range(30))

    class _Client:
        def get_paginator(self, name):
            return type("P", (), {"paginate": lambda self, **k: [{"Contents": [{"Key": f"{fetch_inputs.ENV_SEGMENT}/scraper/from_medusa/variants_export.csv"}]}]})()

        def download_file(self, bucket, key, filename):
            open(filename, "w", encoding="utf-8").write(incoming)
    monkeypatch.setattr(fetch_inputs.boto3, "client", lambda *a, **k: _Client())
    monkeypatch.setattr(fetch_inputs, "SETTINGS", type("S", (), {"paths": type("P", (), {"from_medusa_dir": tmp_path})()})())
    fetch_inputs.main()
    assert local.read_text(encoding="utf-8").count("slab_x_") == 10          # the full local export stays


def _main_with(tmp_path, monkeypatch, name: str, incoming: str):
    """Run fetch_inputs.main against a fake S3 holding one object `name` with body `incoming`."""
    class _Client:
        def get_paginator(self, _):
            return type("P", (), {"paginate": lambda self, **k: [{"Contents": [{"Key": f"{fetch_inputs.ENV_SEGMENT}/scraper/from_medusa/{name}"}]}]})()

        def download_file(self, bucket, key, filename):
            open(filename, "w", encoding="utf-8").write(incoming)
    monkeypatch.setattr(fetch_inputs.boto3, "client", lambda *a, **k: _Client())
    monkeypatch.setattr(fetch_inputs, "SETTINGS", type("S", (), {"paths": type("P", (), {"from_medusa_dir": tmp_path})()})())
    fetch_inputs.main()


def test_with_no_local_export_a_thin_one_is_measured_against_the_committed_base(tmp_path, monkeypatch):
    # after a roll the live export is absent (gitignored); the committed base is in the image and lists the same
    # variants, so a thin S3 export (Medusa mid-reset) is refused instead of becoming the matcher's reference
    (tmp_path / "variants_export_base.csv").write_text(
        "Key,Name\n" + "".join(f"slab_x_{i:04d},X\n" for i in range(10)), encoding="utf-8")
    _main_with(tmp_path, monkeypatch, "variants_export.csv", "Id,Key\nv1,slab_x_0001\n")
    assert not (tmp_path / "variants_export.csv").exists()          # absent: the matcher falls back to the base


def test_with_no_local_export_a_full_one_is_taken(tmp_path, monkeypatch):
    (tmp_path / "variants_export_base.csv").write_text(
        "Key,Name\n" + "".join(f"slab_x_{i:04d},X\n" for i in range(10)), encoding="utf-8")
    full = "Id,Key\n" + "".join(f"v{i},slab_x_{i:04d}\n" for i in range(10))
    _main_with(tmp_path, monkeypatch, "variants_export.csv", full)
    assert (tmp_path / "variants_export.csv").read_text(encoding="utf-8").count("slab_x_") == 10


def test_the_thin_copy_floor_is_one_setting():
    # the S3 fetch and the base write-back guard the same collapse with the same floor
    assert fetch_inputs.THIN_COPY_MIN_FRACTION == settings.THIN_COPY_MIN_FRACTION
    assert sync_variants_base.THIN_COPY_MIN_FRACTION == settings.THIN_COPY_MIN_FRACTION
    assert not hasattr(fetch_inputs, "_MIN_KEEP_FRACTION") and not hasattr(sync_variants_base, "_MIN_KEEP_FRACTION")
