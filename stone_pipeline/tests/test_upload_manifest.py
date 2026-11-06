"""The deliverables manifest contract: content-hash entries, changed-only uploads, each deliverable's S3 version,
side files outside the manifest, manifest written last."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from botocore.exceptions import ClientError

from deploy import upload_artifacts as ua
from stone_pipeline.config.settings import UPLOAD_SIDE_FILES


class _S3:
    """A fake versioned S3: keeps every version of an object, checks a put against its ChecksumSHA256 like S3, and
    records every call in order."""

    def __init__(self, versioned=True):
        self.versions: dict[str, dict[str, bytes]] = {}   # key -> {version_id: bytes}
        self.objects: dict[str, bytes] = {}                # key -> the current bytes
        self.calls: list[tuple[str, str]] = []
        self.versioned = versioned

    def get_object(self, Bucket, Key, VersionId=None):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": _Body(self.versions[Key][VersionId] if VersionId else self.objects[Key])}

    def upload_file(self, filename, bucket, key):
        self._store(key, open(filename, "rb").read())
        self.calls.append(("side", key))

    def put_object(self, Bucket, Key, Body, ContentType=None, ChecksumSHA256=None):
        data = Body if isinstance(Body, bytes) else Body.read()
        if ChecksumSHA256 is not None:
            assert base64.b64encode(hashlib.sha256(data).digest()).decode() == ChecksumSHA256
        version = self._store(Key, data)
        self.calls.append(("put", Key))
        return {"VersionId": version} if self.versioned else {}

    def _store(self, key, data):
        self.objects[key] = data
        history = self.versions.setdefault(key, {})
        version = f"v{len(history) + 1}"
        history[version] = data
        return version


class _Body:
    def __init__(self, data): self._d = data
    def read(self): return self._d


def _write(dirpath, name, text):
    (dirpath / name).write_text(text, encoding="utf-8")


def test_file_entry_counts_csv_records_not_lines(tmp_path):
    _write(tmp_path, "x.csv", 'a,b\n1,"two\nlines"\n3,4\n')
    e = ua.file_entry(tmp_path / "x.csv")
    assert e["rows"] == 2                       # a quoted newline is not a row
    assert e["bytes"] == (tmp_path / "x.csv").stat().st_size
    assert len(e["sha256"]) == 64
    _write(tmp_path, "n.json", "{}")
    assert "rows" not in ua.file_entry(tmp_path / "n.json")


def test_an_empty_line_is_not_a_record_and_a_record_of_empty_fields_is(tmp_path):
    _write(tmp_path, "x.csv", "a,b,c,d\n1,2,3,4\n\n,,,\n\n")
    assert ua.file_entry(tmp_path / "x.csv")["rows"] == 2
    _write(tmp_path, "h.csv", "\na,b\n")                     # a blank line before the header is not the header
    assert ua.file_entry(tmp_path / "h.csv")["rows"] == 0
    _write(tmp_path, "e.csv", "")
    assert ua.file_entry(tmp_path / "e.csv")["rows"] == 0


def test_first_publish_uploads_everything_then_writes_the_manifest_last(tmp_path):
    s3 = _S3()
    _write(tmp_path, "2_valid_combinations.csv", "h\n1\n2\n")
    _write(tmp_path, "3_products_all.csv", "h\n1\n")
    out = ua.publish_deliverables(s3, "b", tmp_path, "prod/scraper/to_upload", run_id="r1")
    assert out["uploaded"] == ["2_valid_combinations.csv", "3_products_all.csv"] and out["unchanged"] == []
    assert s3.calls[-1] == ("put", "prod/scraper/to_upload/manifest.json")         # manifest LAST
    m = json.loads(s3.objects["prod/scraper/to_upload/manifest.json"])
    assert m["contract"] == "v1" and m["run_id"] == "r1" and m["produced_at"].endswith("Z")
    assert m["files"]["2_valid_combinations.csv"]["rows"] == 2
    assert set(m["files"]) == {"2_valid_combinations.csv", "3_products_all.csv"}


def test_unchanged_content_is_not_re_uploaded_but_the_manifest_is_rewritten(tmp_path):
    s3 = _S3()
    _write(tmp_path, "2_valid_combinations.csv", "h\n1\n2\n")
    _write(tmp_path, "3_products_all.csv", "h\n1\n")
    first = ua.publish_deliverables(s3, "b", tmp_path, "p", run_id="r1")
    s3.calls.clear()
    _write(tmp_path, "3_products_all.csv", "h\n1\n9\n")                              # one file changes
    out = ua.publish_deliverables(s3, "b", tmp_path, "p", run_id="r2")
    assert out["uploaded"] == ["3_products_all.csv"] and out["unchanged"] == ["2_valid_combinations.csv"]
    assert s3.calls == [("put", "p/3_products_all.csv"), ("put", "p/manifest.json")]
    m = json.loads(s3.objects["p/manifest.json"])
    assert m["run_id"] == "r2"
    assert m["files"]["2_valid_combinations.csv"] == first["manifest"]["files"]["2_valid_combinations.csv"]
    # a produce that changes nothing: no upload at all, manifest rewritten with the same hashes
    s3.calls.clear()
    out = ua.publish_deliverables(s3, "b", tmp_path, "p", run_id="r3")
    assert out["uploaded"] == [] and s3.calls == [("put", "p/manifest.json")]
    assert json.loads(s3.objects["p/manifest.json"])["files"] == m["files"]


def test_the_manifest_names_the_version_holding_the_hashed_bytes_while_a_publish_overwrites_them(tmp_path):
    s3 = _S3()
    _write(tmp_path, "3_products_all.csv", "h\n1\n")
    ua.publish_deliverables(s3, "b", tmp_path, "p", run_id="r1")
    published = json.loads(s3.objects["p/manifest.json"])["files"]["3_products_all.csv"]
    _write(tmp_path, "3_products_all.csv", "h\n1\n9\n")
    s3.put_object("b", "p/3_products_all.csv", (tmp_path / "3_products_all.csv").read_bytes())  # mid-publish
    by_version = s3.get_object("b", "p/3_products_all.csv", VersionId=published["version_id"])["Body"].read()
    assert hashlib.sha256(by_version).hexdigest() == published["sha256"]                    # still consistent
    assert hashlib.sha256(s3.objects["p/3_products_all.csv"]).hexdigest() != published["sha256"]   # fixed name moved


def test_an_entry_from_before_the_version_id_is_uploaded_once_to_gain_it(tmp_path):
    s3 = _S3()
    _write(tmp_path, "3_products_all.csv", "h\n1\n")
    entry = ua.file_entry(tmp_path / "3_products_all.csv")
    s3.put_object("b", "p/manifest.json", json.dumps({"files": {"3_products_all.csv": entry}}).encode())
    out = ua.publish_deliverables(s3, "b", tmp_path, "p", run_id="r1")
    assert out["uploaded"] == ["3_products_all.csv"]
    assert json.loads(s3.objects["p/manifest.json"])["files"]["3_products_all.csv"]["version_id"]


def test_an_unversioned_bucket_fails_loud_before_the_manifest(tmp_path):
    s3 = _S3(versioned=False)
    _write(tmp_path, "3_products_all.csv", "h\n1\n")
    with pytest.raises(RuntimeError, match="versioning"):
        ua.publish_deliverables(s3, "b", tmp_path, "p", run_id="r1")
    assert "p/manifest.json" not in s3.objects


def test_side_files_are_uploaded_but_not_in_the_manifest(tmp_path):
    assert UPLOAD_SIDE_FILES == {"SYNC_STEPS.md", "standardise_photo_list.json", "variant_image_shas.json"}
    s3 = _S3()
    _write(tmp_path, "3_products_all.csv", "h\n1\n")
    for name in UPLOAD_SIDE_FILES:
        _write(tmp_path, name, "{}")
    out = ua.publish_deliverables(s3, "b", tmp_path, "p", run_id="r1")
    assert sorted(out["side"]) == sorted(UPLOAD_SIDE_FILES)
    assert set(json.loads(s3.objects["p/manifest.json"])["files"]) == {"3_products_all.csv"}
    assert all(f"p/{name}" in s3.objects for name in UPLOAD_SIDE_FILES)                 # the photo process reads it


def test_a_manifest_read_error_other_than_missing_raises(tmp_path):
    class _Broken(_S3):
        def get_object(self, Bucket, Key, VersionId=None):
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")
    _write(tmp_path, "a.csv", "h\n1\n")
    with pytest.raises(ClientError):
        ua.publish_deliverables(_Broken(), "b", tmp_path, "p", run_id=None)
