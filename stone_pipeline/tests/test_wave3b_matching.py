"""Wave 3b: the matcher and normalizer never substitute a value the input did not carry.

Three silent substitutions: a branch without an engine matched against ANOTHER category's variants (a slab
id on a block row, high confidence, unflagged); a manual attribute override that does not resolve shipped
id=None at confidence high with no review flag (a required_id_null reject with nothing in review); and the
matcher re-derived the format branch from the raw tag when the format stage had not run (a second, test-only
canonicalisation path inside production code).
"""

from __future__ import annotations

import pytest

from stone_pipeline.core.schema import CanonicalRow, FlagCode
from stone_pipeline.reference import loaders
from stone_pipeline.stages import match_variation, normalize
from stone_pipeline.state.overrides import Overrides


@pytest.fixture(scope="module")
def ref():
    return loaders.load_all()


def test_a_branch_without_an_engine_never_matches_another_category(ref):
    stage = match_variation.VariationStage.build(ref)
    stage.engines.pop("block")
    row = CanonicalRow(src_site="polonine", surrogate_key="1", raw_name="Arabescato", format_value="Block",
                       variety_match_key="arabescato", raw_format="Block")
    with pytest.raises(KeyError):                       # isolated per row by the run's row guard
        stage.resolve_row(row)
    assert row.variation_id is None                     # never the slab Arabescato


def test_an_override_that_does_not_resolve_is_flagged_not_shipped_at_high_confidence(ref):
    ref.overrides = Overrides(by_key={("polonine", "1"): {"color_name": "Not A Colour"}})
    resolvers = normalize.AttributeResolvers.build(ref)
    row = CanonicalRow(src_site="polonine", surrogate_key="1", raw_color="Black")
    normalize.normalize_row(row, resolvers, ref)
    ref.overrides = None
    assert row.color_id is None
    assert row.color_method == "override"
    assert row.color_confidence != "high"
    assert any(f.code == FlagCode.attr_unresolved and f.field == "color" for f in row.review_flags)


def test_the_matcher_does_not_rederive_the_format_branch(ref):
    stage = match_variation.VariationStage.build(ref)
    row = CanonicalRow(src_site="polonine", surrogate_key="2", raw_name="Arabescato",
                       variety_match_key="arabescato", raw_format="Block")
    stage.resolve_row(row)
    assert not row.format_value                         # the format stage owns this field
    assert row.is_block is False                        # unresolved format = the default branch
