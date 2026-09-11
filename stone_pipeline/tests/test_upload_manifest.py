"""The deliverables manifest contract: content-hash entries, changed-only uploads, manifest written last."""

from __future__ import annotations

import json

from botocore.exceptions import ClientError

from deploy import upload_artifacts as ua


class _S3:
    """A fake S3: remembers objects by key and records every call in order."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": _Body(self.objects[Key])}

    def upload_file(self, filename, bucket, key):
        self.objects[key] = open(filename, "rb").read()
        self.calls.append(("upload", key))

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = Body
        self.calls.append(("put", Key))


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
    assert s3.calls == [("upload", "p/3_products_all.csv"), ("put", "p/manifest.json")]
    m = json.loads(s3.objects["p/manifest.json"])
    assert m["run_id"] == "r2"
    assert m["files"]["2_valid_combinations.csv"] == first["manifest"]["files"]["2_valid_combinations.csv"]
    # a produce that changes nothing: no upload at all, manifest rewritten with the same hashes
    s3.calls.clear()
    out = ua.publish_deliverables(s3, "b", tmp_path, "p", run_id="r3")
    assert out["uploaded"] == [] and s3.calls == [("put", "p/manifest.json")]
    assert json.loads(s3.objects["p/manifest.json"])["files"] == m["files"]


def test_a_manifest_read_error_other_than_missing_raises(tmp_path):
    class _Broken(_S3):
        def get_object(self, Bucket, Key):
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")
    _write(tmp_path, "a.csv", "h\n1\n")
    try:
        ua.publish_deliverables(_Broken(), "b", tmp_path, "p", run_id=None)
    except ClientError:
        return
    raise AssertionError("an unreadable manifest must fail loud, never publish against an unknown state")
