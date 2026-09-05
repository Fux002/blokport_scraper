"""Per-variety origin edits (the 'edit origins' admin action) -- same channel as a mint's seed_country, for
any variety, holding a country LIST. Store round-trip, the overlay onto the origin map at load, the API, and
the reset wipe. Critically includes the REAL load_all -> derive path (the integration a unit test alone would
miss). Uses the autouse per-test config.db.
"""

from __future__ import annotations

import dataclasses

import pytest

from stone_pipeline.config import decisions_store as ds
from stone_pipeline.config import server
from stone_pipeline.config.sources import load_source
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.reference import loaders
from stone_pipeline.reference.loaders import _norm
from stone_pipeline.stages import derive


def _ref(variety, stype):
    return f"{_norm(variety)}|{_norm(stype)}"


# -- store round-trip ----------------------------------------------------------
def test_set_read_list_roundtrip():
    ds.set_variety_origin("Crystal White", "Granite", "in, ir")            # any casing, comma-list w/ spaces
    assert ds.variety_origins()[(_norm("Crystal White"), _norm("Granite"))] == "IN,IR"
    got = ds.get_variety_origin("Crystal White", "Granite")
    assert got["countries"] == ["IN", "IR"]
    row = ds.list_variety_origins()[0]
    assert row["variety"] == "Crystal White" and row["countries"] == ["IN", "IR"]
    assert row["ref"] == _ref("Crystal White", "Granite")


def test_delete_reverts_to_base():
    ds.set_variety_origin("Crystal White", "Granite", "IR")
    assert ds.delete_variety_origin("Crystal White", "Granite") is True
    assert ds.variety_origins() == {}
    assert ds.get_variety_origin("Crystal White", "Granite") is None


def test_invalid_raises():
    with pytest.raises(ds.InvalidDecision):
        ds.set_variety_origin("Crystal White", "Granite", "")               # no countries


def test_clear_on_pristine_reset():
    ds.set_variety_origin("Crystal White", "Granite", "IR")
    assert ds.clear_variety_origins() == 1
    assert ds.variety_origins() == {}


# -- THE integration: edit -> overlay at load -> membership gate resolves (real path) ------------------
def test_edit_overlays_map_and_each_vendor_resolves_its_country():
    # Base map has Crystal White Granite = IN. Add IR -> [IN, IR]. load_all overlays it; the membership gate
    # then resolves each vendor's own country from the list.
    ds.set_variety_origin("Crystal White", "Granite", "IN,IR")
    ref = loaders.load_all()                                                 # REAL load path, config.db present
    assert set(ref.origin_map.exact("Crystal White", "Granite").countries) == {"IN", "IR"}

    def resolve(vendor, primary):
        cfg = dataclasses.replace(load_source(vendor), primary_origin=primary)
        row = CanonicalRow(src_site=vendor, surrogate_key="1", variation_name="Crystal White",
                           type_name="Granite", color_name="White", type_method="variety_authoritative")
        derive.derive_origin(row, ref, cfg)
        return row.origin_source, row.origin_country_code

    assert resolve("marenostone", "IR") == ("vendor_origin", "IR")          # IR in [IN,IR]
    assert resolve("varsha", "IN") == ("vendor_origin", "IN")               # IN in [IN,IR], same list
    assert resolve("zucchi", "TR")[0] == "origin_needs_confirmation"        # TR not listed -> holds


# -- API -----------------------------------------------------------------------
def test_api_put_get_lookup_delete():
    ref = _ref("Crystal White", "Granite")
    code, body = server.dispatch("PUT", ["origins", ref],
                                 {"variety": "Crystal White", "stone_type": "Granite",
                                  "countries": ["IN", "Iran"]})             # name or ISO accepted
    assert code == 200 and body["countries"] == ["IN", "IR"]

    code, body = server.dispatch("GET", ["origins"], None)
    assert code == 200 and body["origins"][0]["variety"] == "Crystal White"

    code, body = server.dispatch("GET", ["origins", "lookup"], None, "variety=Crystal White&stone_type=Granite")
    assert code == 200 and body["edited_countries"] == ["IN", "IR"]
    assert body["effective_countries"] == ["IN", "IR"]

    code, body = server.dispatch("DELETE", ["origins", ref], None)
    assert code == 200 and body["removed"] is True
    assert ds.variety_origins() == {}


def test_api_put_bad_country_is_400():
    code, _ = server.dispatch("PUT", ["origins", "x|y"],
                              {"variety": "X", "stone_type": "Granite", "countries": ["Notacountry"]})
    assert code == 400


def test_api_delete_unknown_ref_is_404():
    code, _ = server.dispatch("DELETE", ["origins", "no|such"], None)
    assert code == 404
