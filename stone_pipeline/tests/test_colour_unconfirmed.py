"""No guessed colour. A product of a variety sold in several colours used to get ONE of them by "first wins" in two
places: reconcile filled the variety's first allowed colour when the vendor gave none (Patagonia Quartzite -> Black),
and normalize kept the first of a vendor's list ('Black and White' -> Black). Now:

1. a vendor colour text naming two or more distinct colours is the pack's multicolour value ('Multicolor');
2. no vendor colour and a variety that allows several: the operator's colour statement for (source, variety) decides;
   without one the colour is UNCONFIRMED: a NEW listing is held, an already-published one stays listed with the colour
   it has (loads never remove) and is queued; curate raises ONE pending_confirm card per (source, variety, type);
3. a variety with ONE allowed colour is unchanged. The backbone's colour LIST is never multicolour evidence.
"""

from __future__ import annotations

import pytest

from stone_pipeline.config.decisions_model import Decisions
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


# --- rule 1: the vendor's own text names several colours ------------------------------------------------------------

def _normalized(ref, raw_color, key="742"):
    row = CanonicalRow(src_site="polonine", surrogate_key=key, raw_color=raw_color)
    normalize.normalize_row(row, normalize.AttributeResolvers.build(ref), ref)
    return row


def test_a_vendor_text_naming_two_colours_is_multicolor(ref):
    row = _reconcile(ref, _normalized(ref, "Black and White"))
    assert row.color_name == "Multicolor" and row.color_method == "multicolor_from_vendor"
    assert row.color_id is not None


def test_the_matcher_sees_mains_colour_rule_1_applies_after_it(ref):
    # The variety matcher (stages/match_variation, unchanged) narrows by row.color_name. normalize leaves exactly
    # main's value there, the first colour named, so which variety a listing is never changes (the golden's polonine
    # 542, Spider Black Quartzite 'Black and White', lost its variety when Multicolor reached the matcher); reconcile,
    # after the matcher, makes it Multicolor.
    row = _normalized(ref, "Black and White", key="542")
    assert (row.color_name, row.color_method) == ("Black", "multi_value")
    assert _reconcile(ref, row).color_name == "Multicolor"


def test_one_colour_named_twice_is_not_multicolor(ref):
    row = _reconcile(ref, _normalized(ref, "White, white", key="743"))
    assert row.color_name == "White"


# --- rule 1 on a variety that does not allow Multicolor yet: the leaf-growth path, a published listing never frozen --

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


def _multicolour_gap(row) -> bool:
    return any(g.suggested_color == "Multicolor" for g in row.tree_gaps)


def test_a_published_multicolour_listing_keeps_its_colour_and_updating_while_the_leaf_waits(ref, patagonia):
    assert "Multicolor" not in patagonia.colors and "Black" in patagonia.colors
    row = _vendor_listed(ref, patagonia, "Black and White")
    validate.validate_row(row, source_code="pol", published=frozenset({"POL-742"}))
    assert not [r for r in row.reject_reasons if r.rule in ("tree_gap", "colour_unconfirmed")]   # never held on it
    assert (row.color_name, row.color_method) == ("Black", "multicolour_leaf_pending") and row.color_id
    assert _multicolour_gap(row)                                     # ...and curate still suggests the leaf


def test_a_new_multicolour_listing_is_held_on_the_leaf_gap_as_usual(ref, patagonia):
    row = _vendor_listed(ref, patagonia, "Black and White")
    validate.validate_row(row, source_code="pol", published=frozenset())
    assert any(r.rule == "tree_gap" for r in row.reject_reasons) and _multicolour_gap(row)


def test_once_the_leaf_is_approved_the_listing_is_multicolor(ref):
    allows = next(v for vs in ref.backbone.by_norm_name.values() for v in vs
                  if "Multicolor" in v.colors and len(v.colors) > 1 and v.finishes and v.qualities)
    row = _vendor_listed(ref, allows, "Black and White")
    assert (row.color_name, row.color_method) == ("Multicolor", "multicolor_from_vendor") and not row.tree_gaps


# --- rule 1 without a multicolour value (review L2): unconfirmed, never a guessed live colour ---------------------

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


# --- rule 2: no vendor colour, a variety sold in several -------------------------------------------------------------

def test_no_colour_and_a_multi_colour_variety_is_unconfirmed(ref, patagonia):
    row = _reconcile(ref, _row(patagonia))
    assert row.color_method == "colour_unconfirmed" and _unconfirmed(row)
    assert row.color_name == patagonia.colors[0]                    # the colour a published listing already carries


def test_a_new_unconfirmed_row_is_held(ref, patagonia):
    row = _reconcile(ref, _row(patagonia))
    validate.validate_row(row, source_code="var", published=frozenset())
    assert any(r.rule == "colour_unconfirmed" for r in row.reject_reasons)


def test_a_published_unconfirmed_row_stays_listed_and_is_queued(ref, patagonia):
    row = _reconcile(ref, _row(patagonia))
    validate.validate_row(row, source_code="var", published=frozenset({"VAR-1056"}))
    assert not any(r.rule == "colour_unconfirmed" for r in row.reject_reasons)
    assert row.color_name == patagonia.colors[0] and _unconfirmed(row)


def test_a_decision_updates_every_row_of_its_source_and_variety(ref, patagonia):
    ref.decisions = Decisions.from_statements(
        [{"source": "varsha", "spelling_norm": "patagonia", "spelling": "Patagonia", "verdict": "is",
          "name": "Patagonia", "stone_type": "Quartzite", "color": "White", "origin_iso": None, "widen": 0,
          "asked_by": "varsha"}],
        exists_as=lambda name, stone_type: True, alias_target=lambda name, stone_type: None)
    one = _reconcile(ref, _row(patagonia, surrogate_key="1056", variety_match_key="Patagonia"))
    two = _reconcile(ref, _row(patagonia, surrogate_key="2230", variety_match_key="Patagonia Quartzite Polished"))
    other = _reconcile(ref, _row(patagonia, src_site="sentas", surrogate_key="9"))
    assert [(r.color_name, r.color_method, _unconfirmed(r)) for r in (one, two)] == [("White", "operator", False)] * 2
    assert other.color_method == "colour_unconfirmed"                # another vendor's listings are its own decision


def _decide(*statements):
    return Decisions.from_statements(
        [{"source": src, "spelling_norm": sp.lower(), "spelling": sp, "verdict": "is", "name": "Patagonia",
          "stone_type": "Quartzite", "color": colour, "origin_iso": None, "widen": 0, "asked_by": src,
          "decided_at": at} for src, sp, colour, at in statements],
        exists_as=lambda name, stone_type: True, alias_target=lambda name, stone_type: None)


def test_the_latest_decision_on_any_spelling_of_the_variety_wins(ref, patagonia):
    ref.decisions = _decide(("varsha", "Patagonia Quartzite", "White", "2026-11-10T09:00:00"),
                            ("varsha", "Patagonia", "Beige", "2026-11-10T10:00:00"))
    row = _reconcile(ref, _row(patagonia, variety_match_key="Patagonia Leather"))   # a spelling never decided
    assert (row.color_name, row.color_method) == ("Beige", "operator")


def test_a_colour_decided_on_a_variety_not_yet_in_the_ledger_is_kept_for_the_vendor(tmp_path, monkeypatch):
    # the variety is not in the ledger yet, so the decide takes the mint path. The first vendor's word
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


# --- the operator's item: ONE per (source, variety), in the existing review queue ---------------------------------

def _cards(ref, patagonia):
    rows = [_reconcile(ref, _row(patagonia, surrogate_key="1217", variety_match_key="Patagonia Quartzite Polished",
                                 raw_image_urls=["https://varsha.example/p/1217.jpg"])),
            _reconcile(ref, _row(patagonia, surrogate_key="1056", raw_image_urls=["https://varsha.example/p/1056.jpg"])),
            _reconcile(ref, _row(patagonia, surrogate_key="2230")),
            _reconcile(ref, _row(patagonia, src_site="sentas", surrogate_key="9"))]
    from stone_pipeline.stages.curate import review
    return review.colour_cards(rows, ref)


def test_one_item_per_source_and_variety_carries_what_the_decision_needs(ref, patagonia):
    from stone_pipeline.config.sources import load_source
    code = load_source("varsha").source_code.upper()
    item = next(c for c in _cards(ref, patagonia) if c["src"] == "varsha")
    assert len(_cards(ref, patagonia)) == 2                            # varsha's and sentas's, never merged
    assert (item["kind"], item["variant"], item["stone_type"], item["color"]) == (
        "colour_unconfirmed", "Patagonia", "Quartzite", "")            # no colour prefilled: never guessed
    assert item["listings"] == [{"source": "varsha", "scraped": "Patagonia"},
                                {"source": "varsha", "scraped": "Patagonia Quartzite Polished"}]
    assert item["skus"] == [f"{code}-1056", f"{code}-1217", f"{code}-2230"] and item["count"] == 3
    assert item["colours"] == [*patagonia.colors, "Multicolor"]
    assert item["image"] == "https://varsha.example/p/1056.jpg"        # the scraped lead of the first listing
    assert item["reason"].startswith("No vendor colour") and item["ref_scope"] == "varsha"


def test_the_items_photo_is_the_published_standardised_main_view(ref, patagonia):
    from stone_pipeline.stages.curate import review
    row = _reconcile(ref, _row(patagonia, raw_image_urls=["https://varsha.example/p/1056.jpg"]))
    row.standard_photo, row.image_keys = True, ["https://cdn.example/standard/main/varsha/abc.jpg"]
    assert review.colour_cards([row], ref)[0]["image"] == "https://cdn.example/standard/main/varsha/abc.jpg"


def test_the_queue_stores_the_item_as_the_review_api_serves_it(ref, patagonia, monkeypatch):
    from stone_pipeline.stages import decisions
    seen = {}
    monkeypatch.setattr(decisions.decisions_store, "replace_pending", lambda kind, rows: seen.setdefault(kind, rows))
    decisions.write_confirm_file(_cards(ref, patagonia))
    stored = {r["ref"]: r["payload"] for r in seen["variety"]}
    assert set(stored) == {"patagonia|quartzite|varsha", "patagonia|quartzite|sentas"}
    item = stored["patagonia|quartzite|varsha"]
    assert item["count"] == 3 and item["colours"][-1] == "Multicolor"
    assert item["listings"] == [{"source": "varsha", "scraped": "Patagonia"},
                                {"source": "varsha", "scraped": "Patagonia Quartzite Polished"}]


def test_one_decision_on_the_item_updates_every_listing_of_its_group(ref, patagonia):
    # the decision is PUT /review/decide {listings: the item's listings, name, type, color}: one statement per
    # listing, all with the one colour; every row of the vendor's variety takes it, whatever its spelling
    item = next(c for c in _cards(ref, patagonia) if c["src"] == "varsha")
    ref.decisions = _decide(*((l["source"], l["scraped"], "Gold", "2026-11-10T11:00:00") for l in item["listings"]))
    rows = [_reconcile(ref, _row(patagonia, surrogate_key=k, variety_match_key=sp))
            for k, sp in (("1056", "Patagonia"), ("1217", "Patagonia Quartzite Polished"), ("2230", "Patagonia"))]
    assert {(r.color_name, r.color_method) for r in rows} == {("Gold", "operator")}
    assert not any(_unconfirmed(r) for r in rows)


# --- rule 3: one allowed colour is unchanged -----------------------------------------------------------------------

def test_a_single_colour_variety_is_unchanged(ref):
    single = next(v for vs in ref.backbone.by_norm_name.values() for v in vs if len(v.colors) == 1)
    row = _reconcile(ref, _row(single))
    assert (row.color_name, row.color_method, row.color_confidence) == (single.colors[0], "from_variety", "high")
    assert not _unconfirmed(row)
