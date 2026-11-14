"""Supplier prices: a source's price pass becomes one write-once run under <env>/supplier_prices/<brand>/<run_id>/,
prices.csv in the contract's columns and its manifest last. A price is what BlokPort pays that vendor, so it never
rides in market_prices, and every row names the variant by the SKU the product emit serves."""

from __future__ import annotations

import csv
import json

import pytest

STAMP = "20261114_164823"
RUN = "20261114T164823Z"
URL = "https://polonine.slabware.com/Bundle.aspx?id={}"
COLUMNS = ["contract_version", "run_id", "brand", "vendor_key", "vendor_sku", "listing_text", "price_text",
           "price_amount", "price_status", "currency", "vat_basis", "unit_text", "observed_at", "source_url",
           "provenance"]


def _row(product_id="151", price1="US$ 59.00<span>/Sq.Mt.</span>", material="Andes"):
    return {"product_id": product_id, "material": material, "stone_type": "Quartzite/", "finish": "Polished",
            "quality": "", "thickness": "2cm", "block": "", "bundle_no": "", "average_size": "", "price1": price1,
            "price2": "", "detail_url": URL.format(product_id)}


def _folder(tmp_path, rows, prices="ok", source="polonine", stamp=STAMP):
    from scrapers.polonine import PRICE_COLUMNS
    folder = tmp_path / "data" / source / stamp
    folder.mkdir(parents=True)
    if rows:
        with (folder / "prices.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=PRICE_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
    (folder / "prices_complete.json").write_text(json.dumps({"prices": prices, "rows": len(rows)}), encoding="utf-8")
    return folder


def _write(tmp_path, rows, **kw):
    from supplier_prices.writer import write_run
    return write_run(_folder(tmp_path, rows, **kw), tmp_path / "out", "blokport")


def _read(written):
    with (written.run_dir / "prices.csv").open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return reader.fieldnames, list(reader)


def test_a_priced_row_is_written_in_the_contract_columns(tmp_path):
    written = _write(tmp_path, [_row()])
    header, rows = _read(written)
    assert written.run_dir == tmp_path / "out" / "blokport" / RUN
    assert header == COLUMNS
    row = rows[0]
    assert json.loads(row.pop("provenance")) == {"price_field": "price1", "scrape": STAMP}
    assert row == {"contract_version": "1", "run_id": RUN, "brand": "blokport", "vendor_key": "polonine",
                   "vendor_sku": "POL-151", "listing_text": "Andes", "price_text": "US$ 59.00 /Sq.Mt.",
                   "price_amount": "59.00", "price_status": "priced", "currency": "USD", "vat_basis": "excl",
                   "unit_text": "Sq.Mt.", "observed_at": "2026-11-14T16:48:23Z", "source_url": URL.format("151")}
    assert written.vendor == {"vendor_key": "polonine", "vendor_name": "Polonine"}
    assert (written.rows, written.left_out, written.duplicates) == (1, 0, 0)


def test_the_vendor_sku_is_the_sku_the_product_emit_builds(tmp_path):
    from stone_pipeline import adapters
    from stone_pipeline.config.sources import load_source
    from stone_pipeline.stages.product_state import sku_from
    raw = _row(product_id="s/08/27")
    _, rows = _read(_write(tmp_path, [raw]))
    natural = adapters.REGISTRY["polonine"].adapt_record(raw).src_natural_key
    assert rows[0]["vendor_sku"] == sku_from(load_source("polonine").source_code, natural) == "POL-S/08/27"


def test_a_grouped_amount_is_written_as_an_exact_decimal(tmp_path):
    _, rows = _read(_write(tmp_path, [_row(price1="US$ 1,234.50 /Sq.Mt.")]))
    assert (rows[0]["price_amount"], rows[0]["price_status"]) == ("1234.50", "priced")


def test_call_for_price_is_on_request_with_no_amount(tmp_path):
    _, rows = _read(_write(tmp_path, [_row(price1="Call for Price")]))
    row = rows[0]
    assert (row["price_status"], row["price_text"], row["price_amount"], row["currency"], row["unit_text"]) == \
        ("on_request", "Call for Price", "", "", "")


@pytest.mark.parametrize("stated, shown, note", [
    ("R$ 59.00 /Sq.Mt.", "R$ 59.00 /Sq.Mt.", "currency symbol not declared"),
    ("about fifty dollars", "about fifty dollars", "price not read"),
    ("US$ 0.00 /Sq.Mt.", "US$ 0.00 /Sq.Mt.", "price not read"),
    ("", "", "no price shown"),
])
def test_a_price_that_cannot_be_read_stays_priced_with_no_amount_and_the_reason(tmp_path, stated, shown, note):
    _, rows = _read(_write(tmp_path, [_row(price1=stated)]))
    row = rows[0]
    assert (row["price_status"], row["price_text"], row["price_amount"], row["currency"], row["unit_text"]) == \
        ("priced", shown, "", "", "")
    assert json.loads(row["provenance"])["note"] == note


def test_a_row_with_no_natural_key_is_left_out_never_minted(tmp_path):
    written = _write(tmp_path, [_row(product_id=""), _row(product_id="152")])
    _, rows = _read(written)
    assert [r["vendor_sku"] for r in rows] == ["POL-152"]
    assert (written.rows, written.left_out) == (1, 1)


def test_a_repeated_sku_keeps_the_first_row_as_the_product_path_does(tmp_path):
    written = _write(tmp_path, [_row(product_id="a1", price1="US$ 10.00 /Sq.Mt."),
                                _row(product_id="A1", price1="US$ 20.00 /Sq.Mt.")])
    _, rows = _read(written)
    assert [(r["vendor_sku"], r["price_amount"]) for r in rows] == [("POL-A1", "10.00")]
    assert (written.rows, written.duplicates) == (1, 1)


def test_the_same_pass_writes_the_same_bytes_sorted_by_sku(tmp_path):
    from supplier_prices.writer import write_run
    folder = _folder(tmp_path, [_row(product_id="9"), _row(product_id="10")])
    first = write_run(folder, tmp_path / "a", "blokport")
    second = write_run(folder, tmp_path / "b", "blokport")
    assert (first.run_dir / "prices.csv").read_bytes() == (second.run_dir / "prices.csv").read_bytes()
    assert [r["vendor_sku"] for r in _read(first)[1]] == ["POL-10", "POL-9"]


def test_a_pass_that_read_no_prices_makes_no_run(tmp_path):
    from supplier_prices.writer import NoPrices, write_run
    folder = _folder(tmp_path, [], prices="login refused (12 characters)")
    with pytest.raises(NoPrices, match="login refused"):
        write_run(folder, tmp_path / "out", "blokport")
    assert not (tmp_path / "out").exists()


def test_a_source_whose_adapter_declares_no_price_field_is_refused(tmp_path):
    from supplier_prices.writer import write_run
    folder = _folder(tmp_path, [_row()], source="marenostone")
    with pytest.raises(ValueError, match="supplier_price_field"):
        write_run(folder, tmp_path / "out", "blokport")


# -- publish: write-once, files then the manifest -------------------------------------------------------------------

class _Body:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data


class _S3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.versions: dict[str, str] = {}
        self.order: list[str] = []

    def get_object(self, Bucket, Key):
        from botocore.exceptions import ClientError
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": _Body(self.objects[Key])}

    def head_object(self, Bucket, Key):
        return {"VersionId": self.versions[Key]}

    def put_object(self, Bucket, Key, Body, ContentType=None, ChecksumSHA256=None):
        self.objects[Key] = Body if isinstance(Body, bytes) else Body.read()
        self.versions[Key] = f"v{len(self.order)}"
        self.order.append(Key)
        return {"VersionId": self.versions[Key]}


def test_a_run_is_published_prices_then_its_manifest(tmp_path):
    from supplier_prices.publish import publish_run
    written = _write(tmp_path, [_row(), _row(product_id="152", price1="Call for Price")])
    s3 = _S3()
    publish_run(s3, "bucket", "blokport", written)
    prefix = f"dev/supplier_prices/blokport/{RUN}"
    assert s3.order == [f"{prefix}/prices.csv", f"{prefix}/manifest.json"]
    manifest = json.loads(s3.objects[f"{prefix}/manifest.json"])
    assert (manifest["contract"], manifest["contract_version"], manifest["brand"], manifest["run_id"]) == \
        ("supplier_prices", "1", "blokport", RUN)
    assert manifest["vendor"] == {"vendor_key": "polonine", "vendor_name": "Polonine"}
    assert manifest["produced_at"]
    entry = manifest["files"]["prices.csv"]
    assert (entry["rows"], entry["version_id"]) == (2, s3.versions[f"{prefix}/prices.csv"])
    assert set(manifest["files"]) == {"prices.csv"}


def test_a_published_run_is_never_published_again(tmp_path):
    from supplier_prices.publish import RunAlreadyPublished, publish_run
    written = _write(tmp_path, [_row()])
    s3 = _S3()
    publish_run(s3, "bucket", "blokport", written)
    before = list(s3.order)
    with pytest.raises(RunAlreadyPublished):
        publish_run(s3, "bucket", "blokport", written)
    assert s3.order == before


# -- delivery from a produce's own scrape -------------------------------------------------------------------------

def test_deliver_publishes_each_scraped_source_with_a_price_pass_and_isolates_a_failed_pass(tmp_path, caplog):
    from supplier_prices import run
    _folder(tmp_path, [_row()])
    _folder(tmp_path, [], prices="login refused (3 characters)", source="polonine", stamp="20261114_170000")
    (tmp_path / "data" / "zucchi" / STAMP).mkdir(parents=True)          # no price pass: not a supplier price source
    s3 = _S3()
    with caplog.at_level("ERROR"):
        delivered = run.deliver(None, "20261114_160000", data_dir=tmp_path / "data", out_root=tmp_path / "out",
                                client=s3, bucket="bucket", brand="blokport")
    assert delivered == []                       # the newest pass read no prices, so nothing older stands in for it
    assert "login refused" in caplog.text
    assert s3.order == []


def test_deliver_takes_the_newest_pass_of_this_produce(tmp_path):
    from supplier_prices import run
    _folder(tmp_path, [_row()], stamp="20261114_150000")               # before this produce: never delivered
    _folder(tmp_path, [_row(product_id="152")])
    s3 = _S3()
    delivered = run.deliver(["polonine"], "20261114_160000", data_dir=tmp_path / "data", out_root=tmp_path / "out",
                            client=s3, bucket="bucket", brand="blokport")
    assert [(d["source"], d["run_id"], d["rows"]) for d in delivered] == [("polonine", RUN, 1)]
    assert s3.order[-1] == f"dev/supplier_prices/blokport/{RUN}/manifest.json"


def test_a_supplier_price_failure_never_fails_the_product_produce(monkeypatch, caplog):
    from botocore.exceptions import ClientError
    from stone_pipeline import produce
    from supplier_prices import run

    def broken(*a, **k):
        raise ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")

    monkeypatch.setattr(run, "deliver", broken)
    with caplog.at_level("ERROR"):
        produce._emit_supplier_prices(["polonine"], "20261114_160000")
    assert "supplier prices not delivered" in caplog.text and "AccessDenied" in caplog.text
