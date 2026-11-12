"""Table 572 step 2, the SC half (S2, C5, C6): the scraper's REAL readers run on byte copies of the backend's goldens
(BE-SCRAPER-INTERFACE.md section H; BE branch feat/572-interface-contract). BE's real writers made the goldens, so this
tests the scraper against what the backend actually emits. A change to the schema, a golden or a reader is a PAIRED PR.

Two levels of strictness (34 with the scraper manager, 12 Nov; the owner's "a missing or extra column fails"):
- THIS TEST: each golden's header is exactly the schema's columns, in order; a missing or extra one fails.
- THE PRODUCTION READERS: a missing REQUIRED column refuses by name; an extra column is read by header and ignored, so
  a writer ships an additive column first and the reader adopts it after (569 `category_handles`, 577 `handle`). The
  test pins both edges on every reader: its golden plus an extra column reads the same rows, and dropping each schema
  column refuses by name exactly when the schema marks it `required` (the reader's required set equals the schema's).
No em dashes."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from pathlib import Path

import pytest

from stone_pipeline.config import settings
from stone_pipeline.reference.loaders import field_kinds
from stone_pipeline.reference.loaders.ports import load_ports
from stone_pipeline.reference.medusa_files import read_attributes, read_variants_export
from stone_pipeline.stages.product_state import load_known_products

# --- config ----------------------------------------------------------------------------------------------------------
FIXTURES = Path(__file__).parent / "fixtures" / "be_interface"
SCHEMA_FILE = FIXTURES / "be-scraper-interface.json"
GOLDEN_DIR = FIXTURES / "golden"
META_FILE = GOLDEN_DIR / "golden.meta.json"
GOLDEN_KEY_PREFIX = "be/"                       # the schema's goldens_sha256 keys name BE's goldens as be/<file>
CSV_FILES = ("T1", "T3", "T4", "T5")            # the BE->SC files with a header; T2 is JSON
EXTRA_COLUMN = "zz_planted_extra"               # an additive column no writer has yet
FIELD_KINDS_BRANDS = ("blokport", "wudport")


def _schema() -> dict:
    return json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _columns(touchpoint: str) -> list[dict]:
    return _schema()["files"][touchpoint]["columns"]


def _golden(touchpoint: str) -> Path:
    return GOLDEN_DIR / _schema()["files"][touchpoint]["golden"]


# --- the readers, each called as production calls it -------------------------------------------------------------
def _read(touchpoint: str, path: Path):
    """The touchpoint's REAL production reader on `path`, with production's settings, as a comparable value. T1 and
    T3 are read as the fetch's intake gate reads a downloaded file (io/medusa_inputs.py _READERS)."""
    if touchpoint == "T1":
        return read_attributes(path, require_status=settings.ATTRIBUTES_STATUS_REQUIRED,
                               require_category_handles=True)
    if touchpoint == "T3":
        return read_variants_export(path, require_status=settings.VARIANTS_EXPORT_STATUS_REQUIRED)
    if touchpoint == "T4":
        return load_known_products(path).by_sku
    if touchpoint == "T5":
        return load_ports(path)
    raise KeyError(touchpoint)


def _rewrite(src: Path, dest: Path, *, drop: str | None = None, add: str | None = None) -> Path:
    """`src`'s rows written to `dest` without the column `drop`, or with the column `add` (cells 'x') appended."""
    with src.open(encoding="utf-8", newline="") as h:
        rows = list(csv.DictReader(h))
        h.seek(0)
        header = next(csv.reader(h))
    header = [c for c in header if c != drop] + ([add] if add else [])
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=header, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    writer.writerows({**r, **({add: "x"} if add else {})} for r in rows)
    dest.write_text(out.getvalue(), encoding="utf-8")
    return dest


# --- C5: every copy is the one the schema names, and the schema is the one the goldens were made with -----------------
def test_c5_every_golden_copy_has_the_schema_s_sha256_and_no_golden_is_extra_or_missing():
    expected = _schema()["goldens_sha256"]
    copies = {f"{GOLDEN_KEY_PREFIX}{p.name}": _sha(p) for p in GOLDEN_DIR.iterdir() if p != META_FILE}
    assert copies == expected


def test_c5_the_schema_copy_is_the_one_the_goldens_were_made_with():
    assert _sha(SCHEMA_FILE) == json.loads(META_FILE.read_text(encoding="utf-8"))["schema_sha256"]


# --- S2: the owner's rule, exact headers, on the goldens -------------------------------------------------------------
@pytest.mark.parametrize("touchpoint", CSV_FILES)
def test_each_golden_header_is_exactly_the_schema_s_columns_in_order(touchpoint):
    with _golden(touchpoint).open(encoding="utf-8", newline="") as h:
        header = next(csv.reader(h))
    assert header == [c["name"] for c in _columns(touchpoint)]


@pytest.mark.parametrize("brand", FIELD_KINDS_BRANDS)
def test_each_field_kinds_golden_has_exactly_the_schema_s_keys_and_sorted_fields(brand):
    doc = json.loads((GOLDEN_DIR / f"field-kinds.{brand}.json").read_text(encoding="utf-8"))
    shape = _schema()["files"]["T2"]["json_shape"]
    assert list(doc) == shape["keys"] and doc["brand"] == brand
    assert list(doc["fields"]) == sorted(doc["fields"])


# --- S2: the real readers on the goldens ----------------------------------------------------------------------------
@pytest.mark.parametrize("touchpoint", CSV_FILES)
def test_the_real_reader_parses_its_golden(touchpoint):
    with _golden(touchpoint).open(encoding="utf-8", newline="") as h:
        rows = list(csv.DictReader(h))
    parsed = _read(touchpoint, _golden(touchpoint))
    assert rows and parsed
    if touchpoint == "T5":
        assert sorted(parsed.all_ids()) == sorted(r["id"] for r in rows)
    else:
        assert len(parsed) == len(rows)


@pytest.mark.parametrize("brand", FIELD_KINDS_BRANDS)
def test_the_real_field_kinds_parser_reads_each_brand_s_golden(monkeypatch, brand):
    path = GOLDEN_DIR / f"field-kinds.{brand}.json"
    monkeypatch.setattr(field_kinds, "BRAND", brand)
    assert field_kinds.parse(path.read_bytes(), path) == json.loads(path.read_text(encoding="utf-8"))["fields"]


# --- the production readers' two edges (34's condition) ---------------------------------------------------------------
@pytest.mark.parametrize("touchpoint", CSV_FILES)
def test_an_extra_column_reads_the_same_rows(tmp_path, touchpoint):
    planted = _rewrite(_golden(touchpoint), tmp_path / "planted.csv", add=EXTRA_COLUMN)
    assert _read(touchpoint, planted) == _read(touchpoint, _golden(touchpoint))


@pytest.mark.parametrize("touchpoint,column", [(t, c["name"]) for t in CSV_FILES for c in _columns(t)])
def test_dropping_a_column_refuses_by_name_exactly_when_the_schema_requires_it(tmp_path, touchpoint, column):
    required = next(c for c in _columns(touchpoint) if c["name"] == column).get("required") is True
    dropped = _rewrite(_golden(touchpoint), tmp_path / "dropped.csv", drop=column)
    if required:
        with pytest.raises(ValueError, match=re.escape(column)):
            _read(touchpoint, dropped)
    else:
        assert _read(touchpoint, dropped)


@pytest.mark.parametrize("brand", FIELD_KINDS_BRANDS)
def test_field_kinds_an_extra_key_reads_the_same_and_each_schema_key_is_required_by_name(monkeypatch, brand):
    path = GOLDEN_DIR / f"field-kinds.{brand}.json"
    monkeypatch.setattr(field_kinds, "BRAND", brand)
    doc = json.loads(path.read_text(encoding="utf-8"))
    planted = json.dumps({**doc, EXTRA_COLUMN: "x"}).encode("utf-8")
    assert field_kinds.parse(planted, "planted") == doc["fields"]
    for key in _schema()["files"]["T2"]["json_shape"]["keys"]:
        dropped = json.dumps({k: v for k, v in doc.items() if k != key}).encode("utf-8")
        with pytest.raises(field_kinds.FieldKindsError, match=re.escape(key)):
            field_kinds.parse(dropped, "dropped")
