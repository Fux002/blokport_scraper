"""RED PROOF ONLY for #614 (carrier PR, closed unmerged after its run). Review L1, L2 and the matcher pin of #614's
test_colour_unconfirmed.py, copied byte for byte, run against MAIN's code so CI shows them red there.
"""

from __future__ import annotations

import pytest

from stone_pipeline.core.schema import CanonicalRow, FlagCode
from stone_pipeline.reference import loaders
from stone_pipeline.stages import normalize, reconcile_tree, validate


@pytest.fixture
def ref():
    return loaders.load_all()


@pytest.fixture
def patagonia(ref):
    variety = ref.backbone.lookup("Patagonia", stone_type="Quartzite")
    assert variety is not None and len(variety.colors) > 1          # sold in several colours
    return variety


def _row(variety, **kw):
    base = dict(src_site="varsha", surrogate_key="1056", variation_id="vid", variation_name=variety.variant,
                variety_match_key=variety.variant, type_name=variety.stone_type, color_name=None,
                finish_name=variety.finishes[0], quality_name=variety.qualities[0])
    base.update(kw)
    return CanonicalRow(**base)


def _reconcile(ref, row):
    reconcile_tree.reconcile_row(row, ref, reconcile_tree.ReconcileStats())
    return row


def _unconfirmed(row) -> bool:
    return any(f.code == FlagCode.colour_unconfirmed for f in row.review_flags)


def _normalized(ref, raw_color, key="742"):
    row = CanonicalRow(src_site="polonine", surrogate_key=key, raw_color=raw_color)
    normalize.normalize_row(row, normalize.AttributeResolvers.build(ref), ref)
    return row


def test_the_matcher_sees_mains_colour_rule_1_applies_after_it(ref):
    # The variety matcher (stages/match_variation, unchanged) narrows by row.color_name. normalize leaves exactly
    # main's value there, the first colour named, so which variety a listing is never changes (the golden's polonine
    # 542, Spider Black Quartzite 'Black and White', lost its variety when Multicolor reached the matcher); reconcile,
    # after the matcher, makes it Multicolor.
    row = _normalized(ref, "Black and White", key="542")
    assert (row.color_name, row.color_method) == ("Black", "multi_value")
    assert _reconcile(ref, row).color_name == "Multicolor"


def _vendor_listed(ref, variety, raw_color, **kw):
    """A row as the pipeline builds it: normalize reads the vendor's colour, the matcher binds the variety, then
    reconcile checks it against the variety's colours."""
    row = CanonicalRow(src_site="polonine", surrogate_key="742", raw_color=raw_color)
    normalize.normalize_row(row, normalize.AttributeResolvers.build(ref), ref)
    for field, value in dict(variation_id="vid", variation_name=variety.variant, variety_match_key=variety.variant,
                             type_name=variety.stone_type, finish_name=variety.finishes[0],
                             quality_name=variety.qualities[0], **kw).items():
        setattr(row, field, value)
    return _reconcile(ref, row)


@pytest.fixture
def no_multicolour(monkeypatch):
    import dataclasses
    from stone_pipeline.config.domain import active_pack
    monkeypatch.setattr(reconcile_tree, "active_pack", lambda: dataclasses.replace(active_pack(), multicolour_value=None),
                        raising=False)


def test_without_a_multicolour_value_a_published_listing_keeps_its_first_named_colour(ref, patagonia, no_multicolour):
    row = _vendor_listed(ref, patagonia, "Black and White")
    assert (row.color_name, row.color_method) == ("Black", "colour_unconfirmed") and _unconfirmed(row)
    assert not any(f.code == FlagCode.attr_unresolved and f.field == "color" for f in row.review_flags)
    validate.validate_row(row, source_code="pol", published=frozenset({"POL-742"}))
    assert not any(r.rule == "colour_unconfirmed" for r in row.reject_reasons)


def test_without_a_multicolour_value_a_new_listing_is_held(ref, patagonia, no_multicolour):
    row = _vendor_listed(ref, patagonia, "Black and White")
    validate.validate_row(row, source_code="pol", published=frozenset())
    assert any(r.rule == "colour_unconfirmed" for r in row.reject_reasons)


def test_without_a_multicolour_value_even_a_single_colour_variety_is_unconfirmed(ref, no_multicolour):
    single = next(v for vs in ref.backbone.by_norm_name.values() for v in vs
                  if len(v.colors) == 1 and v.finishes and v.qualities)
    row = _vendor_listed(ref, single, "Black and White")
    assert row.color_method == "colour_unconfirmed" and _unconfirmed(row)


def test_a_colour_decided_on_a_variety_not_yet_in_the_ledger_is_kept_for_the_vendor(tmp_path, monkeypatch):
    # review L1: the variety is not in the ledger yet, so the decide takes the mint path. The first vendor's word
    # defines the spelling for everyone (a GLOBAL statement); a second vendor's restates that meaning (nothing new for
    # the variety). Each vendor's colour must still reach its own listings.
    from stone_pipeline.config import decisions_store
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    lookups = dict(exists_as=lambda name, stone_type: False, alias_target=lambda name, stone_type: None)
    decisions_store.decide_all([("varsha", "Nova Blue")], "Nova Blue", "Quartzite", "Blue",
                               clean=lambda spelling, stone_type: spelling, **lookups)
    decisions_store.decide_all([("sentas", "Nova Blue")], "Nova Blue", "Quartzite", "Green",
                               clean=lambda spelling, stone_type: spelling, **lookups)
    decided = decisions_store.load_decisions(rows=decisions_store.statement_rows(), **lookups)
    assert [decided.listing_colour(v, "Nova Blue", "Quartzite") for v in ("varsha", "sentas")] == ["Blue", "Green"]
