"""Operator dimension overrides (Mas's word, 10 Nov): the EXISTING manual override (state/manual_overrides.csv, keyed
src_site + surrogate_key + field) also takes length, width and height, in metres, with the flat-dims convention
(height = thickness, width = the short face). A set dimension is recorded as `<field>:override` in dimension_method,
and the weight is derived from it.

Every entry of the file carries who confirmed it, when, and from what (confirmed_by, confirmed_at, source); an entry
without them is refused at load, naming its line. A vendor value that later DIFFERS from an override is reported (a
review flag naming both) and never silently taken either way: the override holds until it is removed.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from stone_pipeline.config.settings import ENV_NAME
from stone_pipeline.core.schema import CanonicalRow, FlagCode
from stone_pipeline.io import medusa_inputs
from stone_pipeline.reference import loaders
from stone_pipeline.stages import derive
from stone_pipeline.state import overrides as ov

_HEADER = "src_site,surrogate_key,field,value,confirmed_by,confirmed_at,source\n"
_SIGNED = "fons,2026-11-10,https://vendor.example/p/620"


@pytest.fixture
def ref():
    return loaders.load_all()


def _file(tmp_path, *lines, header=_HEADER):
    path = tmp_path / "manual_overrides.csv"
    path.write_text(header + "".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def _slab(**kw):
    base = dict(src_site="polonine", surrogate_key="620", raw_format="Slab", variation_name="Verde Ubatuba",
                finish_name="Polished", type_name="Granite", color_name="Green", type_method="variety_authoritative")
    base.update(kw)
    return CanonicalRow(**base)


def _derive(ref, row, path):
    ref.overrides = ov.load_overrides(path)
    derive.derive_category(row, ref)
    derive.derive_dimensions(row, ref)
    return row


def _methods(row) -> set[str]:
    return set(row.dimension_method.split(","))


# --- the load -------------------------------------------------------------------------------------------------------

def test_a_signed_entry_loads_with_its_provenance(tmp_path):
    loaded = ov.load_overrides(_file(tmp_path, f"polonine,620,length,3.1,{_SIGNED}"))
    assert loaded.get("polonine", "620", "length") == "3.1"
    assert loaded.provenance[("polonine", "620", "length")] == ov.Provenance(
        confirmed_by="fons", confirmed_at="2026-11-10", source="https://vendor.example/p/620")


@pytest.mark.parametrize("missing", ["confirmed_by", "confirmed_at", "source"])
def test_an_entry_missing_its_provenance_is_refused_and_named(tmp_path, missing):
    cells = {"confirmed_by": "fons", "confirmed_at": "2026-11-10", "source": "office"}
    cells[missing] = ""
    path = _file(tmp_path, "polonine,620,length,3.1," + ",".join(cells[c] for c in
                                                                  ("confirmed_by", "confirmed_at", "source")))
    with pytest.raises(ov.OverridesError, match=rf"line 2.*polonine.*620.*length.*{missing}"):
        ov.load_overrides(path)


def test_a_file_without_the_provenance_columns_is_refused(tmp_path):
    path = _file(tmp_path, "polonine,620,length,3.1", header="src_site,surrogate_key,field,value\n")
    with pytest.raises(ov.OverridesError, match="confirmed_by"):
        ov.load_overrides(path)


def test_an_unparseable_confirmed_at_is_refused(tmp_path):
    with pytest.raises(ov.OverridesError, match="confirmed_at"):
        ov.load_overrides(_file(tmp_path, "polonine,620,length,3.1,fons,last tuesday,office"))


def test_an_empty_value_is_refused_an_override_never_clears_a_field(tmp_path):
    with pytest.raises(ov.OverridesError, match="value"):
        ov.load_overrides(_file(tmp_path, f"polonine,620,length,,{_SIGNED}"))


def test_a_missing_file_is_no_overrides_as_today(tmp_path):
    assert len(ov.load_overrides(tmp_path / "absent.csv")) == 0


def test_an_unreadable_file_fails_loud(tmp_path):
    directory = tmp_path / "manual_overrides.csv"
    directory.mkdir()                                       # the path exists but is not a readable file
    with pytest.raises(OSError):
        ov.load_overrides(directory)


# --- the dimensions -------------------------------------------------------------------------------------------------

def test_a_length_and_width_override_reach_the_canonical_row_as_override(ref, tmp_path):
    path = _file(tmp_path, f"polonine,620,length,3.1,{_SIGNED}", f"polonine,620,width,1.55,{_SIGNED}")
    row = _derive(ref, _slab(raw_thickness="2cm"), path)
    assert (row.length, row.width, row.height) == (3.1, 1.55, 0.02)
    assert {"length:override", "width:override", "height:parsed"} <= _methods(row)


def test_a_height_override_is_the_thickness(ref, tmp_path):
    row = _derive(ref, _slab(raw_dimensions="length=320cm;height=160cm", raw_thickness="2cm"),
                  _file(tmp_path, f"polonine,620,height,0.03,{_SIGNED}"))
    assert (row.length, row.width, row.height) == (3.2, 1.6, 0.03)
    assert "height:override" in _methods(row)


def test_the_weight_is_derived_from_the_overridden_size(ref, tmp_path):
    overridden = _derive(ref, _slab(raw_thickness="2cm"), _file(tmp_path, f"polonine,620,length,3.1,{_SIGNED}",
                                                                 f"polonine,620,width,1.55,{_SIGNED}"))
    scraped = _derive(ref, _slab(raw_dimensions="length=310cm;height=155cm", raw_thickness="2cm"),
                      tmp_path / "absent.csv")
    assert overridden.weight == scraped.weight


def test_a_differing_vendor_value_is_reported_and_the_override_kept(ref, tmp_path):
    row = _derive(ref, _slab(raw_dimensions="length=320cm;height=160cm", raw_thickness="2cm"),
                  _file(tmp_path, f"polonine,620,length,3.1,{_SIGNED}"))
    assert row.length == 3.1 and "length:override" in _methods(row)
    differs = [f for f in row.review_flags if f.code == FlagCode.dimension_override_differs]
    assert [(f.field, f.raw_value, f.best_guess) for f in differs] == [("length", "3.2", "3.1")]


def test_an_equal_vendor_value_reports_the_override_redundant(ref, tmp_path):
    # grade 3: the exception's premise (the vendor is wrong or silent) stopped holding, so the run says remove it
    row = _derive(ref, _slab(raw_dimensions="length=310cm;height=160cm", raw_thickness="2cm"),
                  _file(tmp_path, f"polonine,620,length,3.1,{_SIGNED}"))
    assert not [f for f in row.review_flags if f.code == FlagCode.dimension_override_differs]
    redundant = [f for f in row.review_flags if f.code == FlagCode.dimension_override_redundant]
    assert [(f.field, f.raw_value) for f in redundant] == [("length", "3.1")]
    assert "remove" in redundant[0].best_guess


def test_a_non_positive_or_unparseable_dimension_override_raises(ref, tmp_path):
    for bad in ("0", "-1", "3.1m"):
        with pytest.raises(ValueError, match="length"):
            _derive(ref, _slab(), _file(tmp_path, f"polonine,620,length,{bad},{_SIGNED}"))


# --- the durable home: S3 is the authority, fetched fresh on every produce -----------------------------------------

class _FakeS3:
    """download_file writes `body`, or raises the S3 error `code` (404 = absent)."""
    def __init__(self, body: str | None = None, code: str | None = None):
        self.body, self.code, self.keys = body, code, []

    def download_file(self, bucket, key, filename):
        self.keys.append(key)
        if self.code:
            raise ClientError({"Error": {"Code": self.code, "Message": self.code}}, "GetObject")
        with open(filename, "w", encoding="utf-8") as handle:
            handle.write(self.body)


def test_the_overrides_live_under_the_durable_state_prefix():
    assert medusa_inputs.OVERRIDES_KEY == f"{ENV_NAME}/scraper/state/manual_overrides.csv"


def test_the_fetch_replaces_the_local_file_with_the_s3_copy(tmp_path):
    target = tmp_path / "manual_overrides.csv"
    target.write_text("stale", encoding="utf-8")
    body = _HEADER + f"polonine,620,length,3.1,{_SIGNED}\n"
    client = _FakeS3(body)
    assert medusa_inputs.fetch_overrides(client=client, target=target) is True
    assert target.read_text(encoding="utf-8") == body and client.keys == [medusa_inputs.OVERRIDES_KEY]


def test_no_file_on_s3_is_no_overrides_and_drops_a_stale_local_copy(tmp_path):
    target = tmp_path / "manual_overrides.csv"
    target.write_text(_HEADER + f"polonine,620,length,3.1,{_SIGNED}\n", encoding="utf-8")
    assert medusa_inputs.fetch_overrides(client=_FakeS3(code="NoSuchKey"), target=target) is False
    assert not target.exists() and len(ov.load_overrides(target)) == 0


def test_a_fetch_error_fails_loud_and_keeps_the_local_file(tmp_path):
    target = tmp_path / "manual_overrides.csv"
    target.write_text("kept", encoding="utf-8")
    with pytest.raises(ClientError):
        medusa_inputs.fetch_overrides(client=_FakeS3(code="AccessDenied"), target=target)
    assert target.read_text(encoding="utf-8") == "kept"


def test_a_malformed_s3_copy_is_refused_before_the_swap(tmp_path):
    target = tmp_path / "manual_overrides.csv"
    target.write_text("kept", encoding="utf-8")
    with pytest.raises(ov.OverridesError):
        medusa_inputs.fetch_overrides(client=_FakeS3("src_site,surrogate_key,field,value\npolonine,620,length,3.1\n"),
                                      target=target)
    assert target.read_text(encoding="utf-8") == "kept"


def test_a_produce_stops_when_the_overrides_cannot_be_fetched(monkeypatch):
    from stone_pipeline import produce
    monkeypatch.setattr(medusa_inputs, "fetch_all", lambda: 0)
    monkeypatch.setattr(medusa_inputs, "fetch_field_kinds", lambda: None)
    monkeypatch.setattr(medusa_inputs, "fetch_overrides", lambda: (_ for _ in ()).throw(RuntimeError("denied")))
    with pytest.raises(SystemExit) as stop:
        produce._fetch_inputs()
    assert stop.value.code == 2
