"""FND-04: the committed base seed (variants_export_base.csv) is a source-of-truth file; it must live
under the env's from_medusa folder (from_medusa/<env>/), like every other Medusa file, so a PRODUCTION
run does not read/write it under the dev folder. Pins the path to the env-derived from_medusa_dir."""

from __future__ import annotations

from stone_pipeline.config.settings import ENV_NAME, SETTINGS
from stone_pipeline.reference import sync_variants_base


def test_base_seed_path_follows_the_env_folder():
    # the base seed tracks from_medusa_dir (same mechanism as variants_export.csv / attributes.csv),
    # NOT a hardcoded 'development' -- so dev and prod each seed from their own base.
    assert sync_variants_base.BASE == SETTINGS.paths.from_medusa_dir / "variants_export_base.csv"
    assert sync_variants_base.BASE.parent == SETTINGS.paths.from_medusa_dir
    # and from_medusa_dir is genuinely env-derived (ends in ENV_NAME), so the pin above is per-env,
    # not a fixed literal that would send a prod run to the wrong folder.
    assert SETTINGS.paths.from_medusa_dir.name == ENV_NAME
