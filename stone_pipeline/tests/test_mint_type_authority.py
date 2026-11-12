"""Minting type-authority: a variety's stone TYPE (taken from its Key, the authority) governs matching and
alias routing, so a same-name variety of a different stone is never collapsed into one.

The known risk (memory: classification-type-authority-review / curate-type-aware-existing-index, PR #45):
split type authority let cross-type products ship -- a name that exists as stone A getting merged with the
same name scraped as stone B. These tests pin the fix at its two load-bearing points:
  1. load_existing indexes the existing catalog by (name, TYPE), so two same-name different-type varieties
     coexist and are addressable separately (name-only lookup is an explicit typeless fallback).
  2. A confirmed non-exact match attaches its scraped spelling to the (name, Key-TYPE) owner -- the Granite
     variety, never the same-name Marble one.
"""

from __future__ import annotations

import csv


from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.config.decisions_model import Decisions
from stone_pipeline.reference import loaders
from stone_pipeline.stages import curate
from stone_pipeline.stages.curate import ImportFile
from stone_pipeline.tests._existing_index import existing_index

# Arabescato is single-type Marble in the real backbone; we introduce a synthetic Granite twin to model the
# same-name-different-stone collision that is the crux of the cross-type bug.
MARBLE_KEY = "marble__arabescato_A"
GRANITE_KEY = "granite__arabescato_B"


def _slab_imports():
    """A minimal existing catalog: two same-name ('Arabescato') varieties of different stones. Granite first,
    MARBLE last, so the name-only fallback (by_name, last-wins) resolves to MARBLE: the keyed path must still
    land on Granite, so if the owner ever regressed to a name-only lookup the alias would wrongly hit Marble and
    the test would fail. Discriminating, not correct-by-luck."""
    return existing_index((GRANITE_KEY, "Arabescato", ""), (MARBLE_KEY, "Arabescato", ""))


def test_load_existing_keys_same_name_different_type_separately(tmp_path, monkeypatch):
    exp = tmp_path / "variants_export.csv"
    with exp.open("w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=["Id", "Key", "Name", "AdminAliases", "AdminCodes", "Type"])   # P2-7
        w.writeheader()
        w.writerow({"Id": "1", "Key": MARBLE_KEY, "Name": "Arabescato", "Type": "Marble"})
        w.writerow({"Id": "2", "Key": GRANITE_KEY, "Name": "Arabescato", "Type": "Granite"})
    monkeypatch.setattr(curate.existing, "existing_varieties_file", lambda: exp)

    imp = curate.load_existing()
    # both stones survive under one name, addressed by (name, TYPE) -- not collapsed to a single variety
    assert imp.by_name_type[("arabescato", "marble")]["Key"] == MARBLE_KEY
    assert imp.by_name_type[("arabescato", "granite")]["Key"] == GRANITE_KEY
    assert len(imp.varieties) == 2
    # the type is taken from the Key (the authority), never guessed from the name
    assert {v["type"] for v in imp.varieties} == {"marble", "granite"}


def test_non_exact_match_aliases_onto_the_matched_type_never_the_same_name_other_type(monkeypatch):
    # A scrape fuzzy-matched to the GRANITE Arabescato (variation_key carries its type) must attach its
    # spelling to the Granite variety, never bleed onto the same-name Marble one.
    monkeypatch.setattr(curate, "load_existing", _slab_imports)
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    ref = loaders.load_all()

    row = CanonicalRow(src_site="polonine", surrogate_key="a1",
                       variety_match_key="Arabesk",           # the scraped spelling to alias
                       variation_id="v1", variation_key=GRANITE_KEY,
                       variation_name="Arabescato", variation_method="fuzzy")   # a learned guess -> alias path
    result = curate.build_curation([row], ref)

    slab_aliases = result.learned_proposals
    on_granite = [a for a in slab_aliases if a["identity_key"] == GRANITE_KEY and a["value"] == "Arabesk"]
    on_marble = [a for a in slab_aliases if a["identity_key"] == MARBLE_KEY]
    assert on_granite, f"spelling should alias onto the Granite variety, got {slab_aliases}"
    assert not on_marble, f"spelling must NOT touch the same-name Marble variety, got {on_marble}"


def _gap_row(name: str) -> CanonicalRow:
    """A type-less scrape whose name matches an existing variety but gaps as missing_variation (the matcher
    could not pick a type). Drives curate's gap-classification loop."""
    from stone_pipeline.core.schema import GapKind, TreeGap
    g = TreeGap(src_site="polonine", surrogate_key="g1", raw_name=name,
                normalized_name=curate.proj.norm(name), gap_kind=GapKind.missing_variation)
    return CanonicalRow(src_site="polonine", surrogate_key="g1", variety_match_key=name,
                        variation_id=None, variation_key=None, variation_name=None,
                        variation_method="exact_name_ambiguous", tree_gaps=[g])




def test_typeless_scrape_holds_for_type_even_when_name_has_one_existing_type(tmp_path, monkeypatch):
    # A type the scrape did not make clear is set via REVIEW -- even when the name exists under exactly ONE
    # type. The code no longer auto-completes it to that single type (that was the code guessing the type).
    existing = existing_index(("marble__solo_stone_1", "Solo Stone", ""))
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    monkeypatch.setattr(curate, "load_existing", lambda: existing)
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    ref = loaders.load_all()

    from stone_pipeline.core.schema import GapKind, TreeGap
    g = TreeGap(src_site="polonine", surrogate_key="s1", raw_name="Solo Stone",
                normalized_name="solo stone", gap_kind=GapKind.missing_variation)
    row = CanonicalRow(src_site="polonine", surrogate_key="s1", variety_match_key="Solo Stone",
                       variation_id=None, variation_key=None, variation_name=None,
                       variation_method="exact_name_ambiguous", tree_gaps=[g])
    result = curate.build_curation([row], ref)
    assert any(p["variant"].lower() == "solo stone" for p in result.pending_confirm), "type-less must hold for a type"
    assert not result.learned_proposals, "must NOT auto-complete to the single existing type"


def test_alias_family_surfaces_to_review_not_a_hidden_side_file(tmp_path, monkeypatch):
    # A type-less scrape whose surface is an ALIAS of SEVERAL different-named varieties is an uncertain
    # identity -> it must appear on the review list (pick which / mint new), never be filed away silently.
    # two DIFFERENT varieties (Alpha, Beta) that both carry 'Shared Surface' as an alias
    existing = existing_index(("marble__alpha_1", "Alpha", "Shared Surface"), ("granite__beta_2", "Beta", "Shared Surface"))
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    monkeypatch.setattr(curate, "load_existing", lambda: existing)
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    ref = loaders.load_all()

    from stone_pipeline.core.schema import GapKind, TreeGap
    g = TreeGap(src_site="polonine", surrogate_key="f1", raw_name="Shared Surface",
                normalized_name="shared surface", gap_kind=GapKind.missing_variation)
    row = CanonicalRow(src_site="polonine", surrogate_key="f1", variety_match_key="Shared Surface",
                       variation_id=None, variation_key=None, variation_name=None,
                       variation_method="exact_ambiguous", tree_gaps=[g])
    result = curate.build_curation([row], ref)
    p = next((p for p in result.pending_confirm if p["variant"].lower() == "shared surface"), None)
    assert p is not None, "alias-family identity must reach the review list"
    assert "Alpha" in p["reason"] and "Beta" in p["reason"], "review must name the candidate varieties"


def test_typeless_match_on_multi_type_name_holds_never_picks_an_arbitrary_stone(monkeypatch):
    # A match that carries NO stable Key (keyless -- e.g. a forced override at a stale id, or an operator
    # alias-by-name) on a name that exists as SEVERAL stones must NOT silently attach to an arbitrary one.
    # _by_name_owner refuses the ambiguous name, so no alias is emitted on either variety (the caller HOLDs).
    monkeypatch.setattr(curate, "load_existing", _slab_imports)
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    ref = loaders.load_all()

    def keyless(raw_type: str) -> CanonicalRow:
        return CanonicalRow(src_site="polonine", surrogate_key="a2", variety_match_key="Arabesk",
                            raw_type=raw_type,
                            variation_id="v1", variation_key=None,     # keyless -> name-only resolution
                            variation_name="Arabescato", variation_method="fuzzy")

    def aliased(result) -> set[str]:
        return {a["identity_key"] for a in result.learned_proposals
                if a["identity_key"] in (MARBLE_KEY, GRANITE_KEY) and a["value"] == "Arabesk"}

    # the row DOES reach the name-only routing: with its own type it is aliased onto that type's variety
    assert aliased(curate.build_curation([keyless("Granite")], ref)) == {GRANITE_KEY}
    # type-less, the multi-type name is HELD: no alias on either stone
    assert aliased(curate.build_curation([keyless("")], ref)) == set(), \
        "multi-type name must not resolve to an arbitrary stone"


def _minted_any(res) -> bool:
    return bool(res.new_variants)






VERDE_SCURO_KEY = "onyx__verde_scuro_A"
VERDE_ONYX_SCURO_KEY = "onyx__verde_onyx_scuro_B"


def _verde_imports():
    """Two SAME-TYPE (onyx) varieties: one literally NAMED 'Verde Scuro', and 'Verde Onyx Scuro' which lists
    'Verde Scuro' as an ALIAS (listed FIRST). So the surface 'verde scuro' resolves to BOTH owners."""
    return existing_index((VERDE_ONYX_SCURO_KEY, "Verde Onyx Scuro", "Verde Scuro"), (VERDE_SCURO_KEY, "Verde Scuro", ""))


def test_same_type_alias_prefers_exact_name_owner_over_an_alias_owner(monkeypatch):
    # SEV-3: PHASE-4a same-type routing used sorted(owners)[0]. 'verde onyx scuro' sorts BEFORE 'verde
    # scuro', so a typed-onyx 'Verde Scuro' scrape would alias onto 'Verde Onyx Scuro' (which merely lists
    # 'Verde Scuro' as a spelling) instead of the variety literally named 'Verde Scuro'. Prefer the exact-
    # name owner.
    monkeypatch.setattr(curate, "load_existing", _verde_imports)
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    ref = loaders.load_all()

    from stone_pipeline.core.schema import GapKind, TreeGap
    # 'Verde Scuro Onyx' cleans to the shared surface 'verde scuro' (both owners), but is neither variety's name nor an
    # alias either carries, so the arm's choice of owner shows as the Key its proposal lands on (P2-10 C2)
    g = TreeGap(src_site="polonine", surrogate_key="v1", raw_name="Verde Scuro Onyx",
                normalized_name="verde scuro onyx", gap_kind=GapKind.missing_variation)
    row = CanonicalRow(src_site="polonine", surrogate_key="v1", variety_match_key="Verde Scuro Onyx",
                       raw_type="Onyx", variation_method="exact_name_ambiguous", tree_gaps=[g])
    res = curate.build_curation([row], ref)

    on_exact = [a for a in res.learned_proposals if a["identity_key"] == VERDE_SCURO_KEY]
    on_alias_owner = [a for a in res.learned_proposals if a["identity_key"] == VERDE_ONYX_SCURO_KEY]
    assert on_exact, f"must alias onto the exact-name 'Verde Scuro', got {res.learned_proposals}"
    assert not on_alias_owner, "must NOT alias onto 'Verde Onyx Scuro' (it only carries the name as a spelling)"


def _empty_imports() -> ImportFile:
    return ImportFile(path=None)


def _typed_gap_row(name: str, raw_type: str) -> CanonicalRow:
    """A gapped scrape that carries a stone TYPE (as a supplier does when the type is in the product name,
    e.g. 'Lumiere Crystal'), so variety_identity sees a non-blank scrape type."""
    from stone_pipeline.core.schema import GapKind, TreeGap
    g = TreeGap(src_site="zucchi", surrogate_key="t1", raw_name=name,
                normalized_name=curate.proj.norm(name), gap_kind=GapKind.missing_variation)
    return CanonicalRow(src_site="zucchi", surrogate_key="t1", variety_match_key=name,
                        raw_type=raw_type, variation_method="exact_name_ambiguous", tree_gaps=[g])


def test_operator_mint_type_overrides_the_scraped_type(tmp_path, monkeypatch):
    # THE requirement: the operator picks a type in review and it must WIN over the type the scraper derived
    # from the product name. 'Lumiere' scraped as Crystal, operator mints it as Agate -> the minted variety
    # is Agate, never Crystal. (The bug: seed_type only filled a BLANK type, so the scrape's Crystal won.)
    from stone_pipeline.stages import decisions
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    monkeypatch.setattr(curate, "load_existing", _empty_imports)
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    monkeypatch.setattr(decisions, "load_decisions",            # operator minted it ... as Agate
                        lambda: Decisions.from_legacy({("", "lumiere"): {"action": "mint", "seed_type": "Agate"}}))
    ref = loaders.load_all()

    res = curate.build_curation([_typed_gap_row("Lumiere", "Crystal")], ref)
    minted = [r["Key"] for r in res.new_variants]
    assert any(k.startswith("agate__") for k in minted), f"must mint as the operator-picked Agate, got {minted}"
    assert not any(k.startswith("crystal__") for k in minted), f"must NOT mint as the scraped Crystal type, got {minted}"


def test_operator_mint_type_wins_even_when_the_scrape_type_matches_an_existing_variety(tmp_path, monkeypatch):
    # Arabescato exists as Marble + Granite. A scrape typed MARBLE (an existing type) that the operator minted
    # as Agate must mint a NEW Agate variety -- the operator's choice wins even over an existing same-type
    # variety; it does NOT silently alias onto the Marble one (which is what happened when the scrape type won).
    from stone_pipeline.stages import decisions
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    monkeypatch.setattr(curate, "load_existing", _slab_imports)
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    monkeypatch.setattr(decisions, "load_decisions",
                        lambda: Decisions.from_legacy({("", "arabescato"): {"action": "mint", "seed_type": "Agate"}}))
    ref = loaders.load_all()

    res = curate.build_curation([_typed_gap_row("Arabescato", "Marble")], ref)
    minted = [r["Key"] for r in res.new_variants]
    assert any(k.startswith("agate__") for k in minted), f"operator's Agate must mint, got {minted}"
    on_marble = [a for a in res.learned_proposals if a["identity_key"] == MARBLE_KEY]
    assert not on_marble, "must NOT alias onto the existing Marble variety when the operator picked Agate"


def test_operator_confirmed_mint_mints_even_when_similar_to_an_existing_name(tmp_path, monkeypatch):
    # The products-side of #174: an operator-CONFIRMED mint ("yes" + a chosen type) must MINT the new
    # variety, EVEN when its name is similar to an existing one -- the similarity/nearest arms must not
    # alias or re-review it away (the "Alpine Luxe near Alpine got silently dropped" bug). The confirmed
    # mint is honoured ONCE at the top (3c-bis), before any heuristic can override it.
    from stone_pipeline.stages import decisions
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    monkeypatch.setattr(curate, "load_existing", _slab_imports)   # existing 'Arabescato'
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    ref = loaders.load_all()

    def minted(res):
        return {(p.get("Name") or "").lower() for p in res.new_variants}

    # (a) UNDECIDED 'Arabescato Royal' (near existing 'Arabescato') -> HELD for review, never auto-minted
    monkeypatch.setattr(decisions, "load_decisions", Decisions.empty)
    undecided = curate.build_curation([_gap_row("Arabescato Royal")], ref)
    assert "arabescato royal" not in minted(undecided), "an UNDECIDED new variety must not auto-mint"

    # (b) operator confirmed the mint as Granite -> it MINTS, despite the name similarity, and is NOT held
    monkeypatch.setattr(decisions, "load_decisions",
                        lambda: Decisions.from_legacy({("", "arabescato royal"): {"action": "mint", "seed_type": "Granite"}}))
    confirmed = curate.build_curation([_gap_row("Arabescato Royal")], ref)
    assert "arabescato royal" in minted(confirmed), "a CONFIRMED mint must mint even when similar to an existing name"
    assert not any(p.get("variant", "").lower() == "arabescato royal" for p in confirmed.pending_confirm), \
        "a confirmed mint must not also be re-held for review"

    # (c) operator rejected -> not minted, not held
    monkeypatch.setattr(decisions, "load_decisions",
                        lambda: Decisions.from_legacy({("", "arabescato royal"): {"action": "reject"}}))
    rejected = curate.build_curation([_gap_row("Arabescato Royal")], ref)
    assert "arabescato royal" not in minted(rejected), "a rejected variety must not mint"
