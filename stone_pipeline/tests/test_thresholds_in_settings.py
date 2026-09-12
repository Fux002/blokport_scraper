"""Wave 5b: every live threshold is a settings field; no dead settings field survives.

The audit found live thresholds inline in stages and matching while their settings counterparts were dead
(never read), plus settings fields nothing reads. Tunables live in ONE config block (settings.py); a stage
reads its field, never a private constant.
"""

from __future__ import annotations

import pytest

from stone_pipeline.config import settings
from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.matching import alias_resolver, engine
from stone_pipeline.stages import format_resolve, magnitude_drift, reconcile_tree
from stone_pipeline import run as run_mod


@pytest.mark.parametrize("module,constant,field,value", [
    (magnitude_drift, "_WARN_FACTOR", "magnitude_warn_factor", 4.0),
    (magnitude_drift, "_FAIL_FACTOR", "magnitude_fail_factor", 10.0),
    (magnitude_drift, "_MIN_SAMPLE", "magnitude_min_sample", 8),
    (engine, "_PHONETIC_CHAR_FLOOR", "phonetic_char_floor", 85.0),
    (reconcile_tree, "_LEAF_SNAP_FLOOR", "leaf_snap_floor", 80.0),
    (format_resolve, "_BULK_DEPTH_FRACTION", "bulk_depth_fraction", 0.3),
    (format_resolve, "_DEFAULT_DEPTH_MULTIPLE", "default_depth_multiple", 3.0),
    (run_mod, "_ADAPT_DROP_ABORT", "adapt_drop_abort_fraction", 0.5),
    (run_mod, "_DELIST_MAX_FRACTION", "delist_max_fraction", 0.30),
])
def test_a_live_threshold_is_a_settings_field_not_a_module_constant(module, constant, field, value):
    assert not hasattr(module, constant)
    assert getattr(SETTINGS.thresholds, field) == value


def test_the_alias_resolver_floors_are_curation_settings():
    assert not hasattr(alias_resolver, "_CHAR_ALIAS_FLOOR") and not hasattr(alias_resolver, "_CHAR_REVIEW_FLOOR")
    assert SETTINGS.curation.alias_char_floor == 0.94
    assert SETTINGS.curation.alias_char_review_floor == 0.88


def test_dead_settings_fields_are_gone():
    assert not hasattr(SETTINGS.matching, "semantic_review_floor")
    assert not hasattr(SETTINGS.curation, "alias_model_hi") and not hasattr(SETTINGS.curation, "alias_model_lo")
    for f in ("crop_pad", "crop_min_side", "crop_snap"):
        assert not hasattr(SETTINGS.images.processing, f)
    assert not hasattr(SETTINGS.paths, "repo_root")
    assert not hasattr(SETTINGS.paths, "origin_map_csv_fallback")
    assert not hasattr(settings, "category_by_label")
