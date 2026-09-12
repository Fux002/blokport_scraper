"""F-5: the pack's disambiguator was validated but never consumed. The reason it cannot be consumed today is
the row schema: CanonicalRow carries <attribute>_name / <attribute>_id fields for exactly type, color,
finish and quality, and the Key builder, reconcile and curate implement the type attribute by name. So a
pack cannot declare another attribute set or identity attribute without a row-schema change. The loader
says so, loudly, instead of accepting a declaration the pipeline would silently ignore."""

from __future__ import annotations

import pytest
import yaml

from stone_pipeline.config import domain
from stone_pipeline.core.schema import CanonicalRow


def _stone():
    return yaml.safe_load(domain._pack_path("stone").read_text(encoding="utf-8"))


def test_an_attribute_the_row_schema_does_not_carry_is_refused_naming_the_limit():
    data = _stone()
    data["attributes"] = ["material", "color", "size"]
    data["disambiguator"], data["leaf_attributes"] = "material", ["color", "size"]
    with pytest.raises(ValueError, match="material.*row schema"):
        domain._validate_shape("stone", domain._pack_path("stone"), data)


def test_the_identity_attribute_must_be_type_until_the_row_schema_is_generic():
    data = _stone()
    data["disambiguator"], data["leaf_attributes"] = "color", ["type", "finish", "quality"]
    with pytest.raises(ValueError, match="disambiguator.*type"):
        domain._validate_shape("stone", domain._pack_path("stone"), data)


def test_the_loaders_attribute_list_is_the_row_models():
    fields = set(CanonicalRow.model_fields)
    row_attributes = {f[:-5] for f in fields if f.endswith("_name") and f"{f[:-5]}_id" in fields}
    assert row_attributes - {"variation"} == set(domain.ROW_ATTRIBUTES)   # variation is the variety, not an attribute
