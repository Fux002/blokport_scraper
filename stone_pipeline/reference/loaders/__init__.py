"""Reference data loaders (section 6, section 6A).

Three files do three jobs (section 6A):
  - backbone.json decides what is valid (per variety: stone_type and the allowed
    colour, finish, quality sets). Names only, no ids.
  - attributes.csv maps attribute names to backend ids (colour, finish, quality,
    type, and the two category pcat ids).
  - variants_<category>.csv maps a variety name (plus aliases) to its variation
    id, selected by branch (slabs vs blocks).

Every id-bearing file is a live backend export and can lag (section 6). A
content hash of each is recorded so a run is reproducible against a snapshot.
The loaders return typed structures; gap detection and id validity are checked
against the live set at run start (the fingerprint check below).

The package, one module per reference:
  common          the shared name normalization, the logger, the facade hook
  identity        variety identity (type slug from Key, dedup identity, survivor rule, collapse)
  attributes      attributes.csv -> ids
  variants        the existing-variety file -> per-category variant tables
  backbone        backbone.json -> allowed sets per variety
  ports           the environment's Medusa port ids
  units           unit tokens -> factors
  synonyms        raw spelling -> canonical value, per vocabulary
  origins         origin map, per-supplier overrides, country codes, the ONE country resolver
  reference_data  ReferenceData, load_all, existing_varieties_file, the pack-defaults check

This facade is the package's public surface and its seam: environment-dependent state (SETTINGS,
IS_PRODUCTION) and the cached helpers (load_synonyms, pristine_seed_keys, _delete_keys) are read by the
loaders THROUGH this module at call time, so replacing them here (a test, an operator harness) replaces
them for every loader."""

from __future__ import annotations

from stone_pipeline.config.settings import IS_PRODUCTION, SETTINGS
from stone_pipeline.core.manifest import content_hash
from stone_pipeline.reference.loaders.attributes import VOCAB_CATEGORIES, Attributes, load_attributes
from stone_pipeline.reference.loaders.backbone import Backbone, BackboneVariety, load_backbone
from stone_pipeline.reference.loaders.common import log, norm as _norm
from stone_pipeline.reference.loaders.identity import (collapse_to_survivors, is_mistyped_variant,
                                                       pristine_seed_keys, survivor_of, type_slug_from_key,
                                                       variety_identity)
from stone_pipeline.reference.loaders.origins import (OriginMap, OriginOverrides, OriginRule,
                                                      load_country_codes, load_origin_map,
                                                      load_origin_overrides, resolve_iso)
from stone_pipeline.reference.loaders.ports import Ports, load_ports
from stone_pipeline.reference.loaders.reference_data import (ReferenceData,
                                                             assert_pack_defaults_resolve as _assert_pack_defaults_resolve,
                                                             existing_varieties_file, load_all)
from stone_pipeline.reference.loaders.synonyms import load_synonyms
from stone_pipeline.reference.loaders.units import UnitEntry, Units, load_units
from stone_pipeline.reference.loaders.variants import (ExistingVariants, Variant, VariantTable,
                                                       _delete_keys, load_existing_variants, load_variants)

__all__ = [
    "IS_PRODUCTION", "SETTINGS", "content_hash", "log",
    "Attributes", "VOCAB_CATEGORIES", "load_attributes",
    "Backbone", "BackboneVariety", "load_backbone",
    "collapse_to_survivors", "is_mistyped_variant", "pristine_seed_keys", "survivor_of", "type_slug_from_key",
    "variety_identity",
    "OriginMap", "OriginOverrides", "OriginRule", "load_country_codes", "load_origin_map",
    "load_origin_overrides", "resolve_iso",
    "Ports", "load_ports",
    "ReferenceData", "existing_varieties_file", "load_all",
    "load_synonyms",
    "UnitEntry", "Units", "load_units",
    "ExistingVariants", "Variant", "VariantTable", "load_existing_variants", "load_variants",
    "_assert_pack_defaults_resolve", "_delete_keys", "_norm",
]
