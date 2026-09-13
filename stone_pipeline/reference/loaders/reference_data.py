"""The bundle of all reference data, its version pinning, and load_all: the ONE place the reference is
assembled and the operator overlays (config.db) are applied in memory."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from stone_pipeline.config.settings import CATEGORIES
from stone_pipeline.core.manifest import content_hash
from stone_pipeline.reference.loaders.attributes import VOCAB_CATEGORIES, Attributes, load_attributes
from stone_pipeline.reference.loaders.backbone import Backbone, load_backbone
from stone_pipeline.reference.loaders.common import env, log, norm
from stone_pipeline.reference.loaders.identity import type_slug_from_key
from stone_pipeline.reference.loaders.origins import (OriginMap, OriginOverrides, load_country_codes,
                                                      load_origin_map, load_origin_overrides, resolve_iso)
from stone_pipeline.reference.loaders.ports import Ports, load_ports
from stone_pipeline.reference.loaders.units import Units, load_units
from stone_pipeline.reference.loaders.variants import VariantTable, load_existing_variants


@dataclass
class ReferenceData:
    attributes: Attributes
    # category name -> variant table, for categories that share the stone-variety
    # vocabulary (slab/block/tile). One per registry entry; the named accessors
    # below are kept for existing callers/tests.
    variants: dict[str, VariantTable]
    backbone: Backbone
    ports: Ports
    units: Units
    origin_map: OriginMap
    country_codes: dict[str, str]   # country name (normalized) -> ISO-2
    synonyms: dict[str, dict[str, str]]
    versions: dict[str, str]
    origin_overrides: OriginOverrides = field(default_factory=OriginOverrides)  # per-supplier origin picks
    overrides: object = None  # state.overrides.Overrides; lazy import to avoid cycle
    # norm(cleaned variety name) -> operator-chosen canonical stone type, for MINT decisions (config.db).
    # The matcher consults this so a product whose scraped type matches no existing variety of its name binds
    # to the operator-minted (name, type) instead of gapping -- the operator's authority reaching the PRODUCT,
    # not just the variety. Folded in at load (below), mirroring the seed_country origin overlay.
    variety_seed_types: dict[tuple[str, str], str] = field(default_factory=dict)   # scope_key -> type
    # (norm source, norm scraped spelling) -> (target variety name, target stone type or ''): the operator's
    # VENDOR-SCOPED alias decisions ('for marenostone, Amazon Green Granite is Golden Lightning'). Applied by
    # the matcher's override tier for that vendor only; a global alias goes the ordinary alias route.
    scoped_aliases: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict)
    # norm(variety name) -> the stone types it exists under, built once on first use from the variant tables
    name_types: dict[str, set[str]] | None = None

    def variety_types(self, name: str) -> set[str]:
        """The normalized stone types an existing variety NAME is known under ('azul white' -> {'onyx',
        'quartzite'}); empty for an unknown name. Identity is (type, name), so a name under several types is
        several varieties: the callers that must not guess between them ask here."""
        if self.name_types is None:
            index: dict[str, set[str]] = {}
            for table in self.variants.values():
                for v in table.by_id.values():
                    index.setdefault(norm(v.name), set()).add(norm(type_slug_from_key(v.key).replace("_", " ")))
            self.name_types = index
        return self.name_types.get(norm(name), set())

    @cached_property
    def valid_iso_codes(self) -> frozenset:
        """The real ISO-3166 alpha-2 codes we know (the values of country_codes), so a bare 2-letter
        scraped token can be VALIDATED rather than blindly trusted (rejects 'XX'; lets 'UK' resolve
        to GB via the name/alias path instead of passing through as a bogus code)."""
        return frozenset(self.country_codes.values())

    def to_iso(self, value: str) -> str | None:
        """The product-facing country resolver: see resolve_iso."""
        return resolve_iso(value, self.country_codes, self.valid_iso_codes)

    # A specific category's variant table is `self.variants[<category name>]` (keyed by the active pack's
    # category names) -- no per-product-type accessor, so nothing here assumes a 'slab'/'block'/'tile' pack.


def assert_pack_defaults_resolve(ref: ReferenceData) -> None:
    """Fail LOUD if a domain-pack default VALUE (or a colour classify() can emit) is not a real Medusa value
    in attributes.csv. Field NAMES are the Medusa contract; the VALUES must exist in Medusa's vocabulary --
    so a pack/hardcoded default that Medusa renamed or removed is caught here at load, not shipped downstream
    as an unresolvable null id. Reuses the same resolve_id every stage uses."""
    from stone_pipeline.config.domain import active_pack
    from stone_pipeline.stages.variety_color import CLASSIFIABLE_COLORS
    pack = active_pack()
    checks: list[tuple[str, str]] = (
        [("finish", f) for f in pack.default_finishes]
        + [("finish", f) for f in pack.last_resort_finishes.values()]
        + [("finish", pack.block_finish), ("quality", pack.last_resort_quality),
           ("color", pack.fallback_color)])
    # the classify() palette only needs to resolve in Medusa if the domain actually classifies texture colour
    if pack.classify_texture_color:
        checks += [("color", c) for c in CLASSIFIABLE_COLORS]
    # every SYNONYM's canonical target must also be a real Medusa value: a synonym 'Grigio'->'Gray' while
    # attributes.csv has only 'Grey' otherwise resolves to a null id PER PRODUCT (a quiet per-row reject
    # for every product routing through it) instead of one loud config error here at load. Skip the 'none'
    # sentinel (a deliberate resolve-to-no-id). Symmetric with the pack-default checks above.
    for vocab in pack.attributes:
        checks += [(vocab, c) for c in env().load_synonyms(vocab).values()
                   if c and c.strip().casefold() != "none"]
    missing = sorted({(v, val) for v, val in checks if val and not ref.attributes.resolve_id(v, val)})
    if missing:
        raise ValueError("pack default OR synonym-target values absent from attributes.csv (a value Medusa "
                         "does not have): " + ", ".join(f"{v}={val!r}" for v, val in missing))


def existing_varieties_file() -> Path:
    """The ONE file every existing-variety index is built from (the matcher's candidate index AND curate's
    alias-vs-new index): the live Medusa export when it exists, else the committed Id-free base.

    The base is a CORRECT source for both readers, not a degraded fallback: neither reads the Medusa Id. They
    use Key, Name, Aliases, Image and Volume and derive the type from the Key, all of which the base carries,
    and the base is the canonical full variety union while the live export lags it after a factory reset
    (lifecycle._prune_stale_medusa_export deletes the export; Blokport re-exports only after the next pull).
    Reading the base in that window keeps every existing variety known, so nothing is minted twice. Only
    these two readers use it: emit, tree build and the catalog gate need Medusa ids and keep reading the live
    export alone. Neither file present is an error, never an empty index."""
    paths = env().SETTINGS.paths
    for path in (paths.export_file, paths.variants_export_base_csv):
        if path.exists():
            return path
    raise FileNotFoundError(
        f"no existing-variety file: neither {paths.export_file} nor {paths.variants_export_base_csv} exists")


def load_all() -> ReferenceData:
    from stone_pipeline.state.overrides import load_overrides
    from stone_pipeline.config import decisions_store, settings, store

    # load_all is a READ path. The operator overlays below live in config.db, but merely READING them via
    # open_store() CREATES the file if it is absent -- which then flips load_source (config store vs yaml) for
    # the rest of the process and is wrong for a pure read. Guard every decisions_store read on the store
    # already existing: in prod it always does (control plane, snapshotted + restored on boot), so the overlays
    # always apply; when it is absent (tests, a fresh checkout) there are no decisions to overlay anyway, so
    # skipping is equivalent -- minus the surprise file. See the origin-map overlay note below.
    _have_config_db = store.config_db_path().exists()

    paths = env().SETTINGS.paths
    is_production = env().IS_PRODUCTION
    # Realign the category registry with the export NOW on disk, BEFORE any stage reads category
    # activation. The pcats are read once at import (bootstrap), which for a run whose export landed after
    # import (a cold start, or any non-produce entrypoint like catalog/build/republish that never fetched)
    # leaves block/tile inactive on the frozen snapshot -- silently disabling new-variety fan-out into
    # those categories. load_all is the single point every pipeline stage passes through with the export on
    # disk, so refreshing here makes EVERY path (not just produce) reflect the live pcats. Idempotent.
    settings.refresh_category_pcats()
    # The effective backbone = the committed seed grown by the operator-approved leaf overlay (config.db).
    # The seed file is never mutated; the overlay is applied in memory, here, in the one place ref is built.
    backbone = load_backbone()
    if _have_config_db:
        backbone.apply_leaf_overlay(decisions_store.backbone_leaf_overlay())
    # products link by Key, not id, so the matcher's candidate index may come from the Id-free base while the
    # live export is absent; the real Medusa id fills in on the pull.
    # ONE combined export for every category, read ONCE and split by Key prefix into a per-category index,
    # one per registry entry that shares the stone-variety vocabulary.
    existing = load_existing_variants(existing_varieties_file(),
                                      [c.name for c in CATEGORIES if c.shares_variety_vocab])
    ref = ReferenceData(
        attributes=load_attributes(),
        variants=existing.tables,
        backbone=backbone,
        ports=load_ports(),
        units=load_units(),
        origin_map=load_origin_map(),
        origin_overrides=load_origin_overrides(),
        country_codes=load_country_codes(),
        synonyms={v: env().load_synonyms(v) for v in VOCAB_CATEGORIES},
        overrides=load_overrides(),
        versions={
            "attributes": content_hash(paths.attributes_csv),
            # the file the matcher ACTUALLY read (the live export, else the committed base) and its hash
            "variants_source": existing.source.name,
            "variants_export": existing.content_hash,
            "backbone": content_hash(paths.backbone_json),
            "ports": content_hash(
                paths.ports_csv if paths.ports_csv.exists()
                # prod never provenances the dev-id fallback (load_ports refuses it): hash the absent
                # primary ("absent") so the version reflects what actually resolved.
                else (paths.ports_csv if is_production else paths.ports_csv_fallback)
            ),
            "units": content_hash(paths.units_csv),
            "origin_map": content_hash(paths.origin_map_csv),
        },
    )
    # The effective origin map = the curated CSV grown by operator-minted origins (variety_decision
    # seed_type + seed_country), overlaid in memory here -- the one place ref is built, mirroring the leaf
    # overlay. Type-scoped, so a mint under one type never clobbers a homonym's origin under another.
    # Operator overlays from config.db (guarded on the store already existing -- see the note in load_all):
    #   * seed_country grows the per-variety origin MAP (type-scoped, so a mint under one type never clobbers
    #     a homonym's origin under another);
    #   * origin CONFIRMATIONS from the separate origin review queue grow the per-vendor origin OVERRIDES, so a
    #     confirmed (source, variety, type) origin resolves at derive's supplier_override tier and is never
    #     re-asked;
    #   * operator MINT types (canonical-gated, keyed by norm(clean variety name) exactly as decisions_store +
    #     curate key them) let the matcher bind a product to an operator-minted (name, type) whose type the
    #     scrape did not carry -- the mint decision reaching the product, not only the variety.
    ref.variety_seed_types = {}
    ref.scoped_aliases = {}
    if _have_config_db:
        ref.scoped_aliases = decisions_store.scoped_aliases()
        ref.origin_map.apply_origin_overlay(decisions_store.variety_seed_country_rules())
        # Operator "edit origins" edits win over both the CSV base and a mint's seed_country: applied LAST,
        # they set the variety's origin country LIST (the per-vendor gate then picks from it). Same overlay
        # machinery as the mint origin above, keyed by (name, type), country_iso may be a comma-list.
        ref.origin_map.apply_origin_overlay(decisions_store.variety_origins())
        # a widened decision ADDS its country to the stone's documented list (union), after every list edit
        ref.origin_map.widen(decisions_store.origin_widen())
        ref.origin_overrides.apply_overlay(decisions_store.origin_decisions())
        _valid_types = {norm(t) for t in ref.attributes.canonical_names("type")}
        ref.variety_seed_types = {n: t for n, t in decisions_store.variety_seed_types().items()
                                  if norm(t) in _valid_types}
    env()._assert_pack_defaults_resolve(ref)   # a pack default value not in Medusa's vocabulary fails loud here
    log.info(
        "reference loaded",
        extra={
            "extra_fields": {
                "attributes_colors": len(ref.attributes.by_category.get("color", {})),
                # the default-form variant count (pack primary category); NOT literally slab, so a non-stone
                # pack whose default form is not named 'slab' does not KeyError building this log line.
                "variants_default_form": (
                    len(ref.variants[settings.default_form_name()].by_id)
                    if settings.default_form_name() in ref.variants else 0),
                "backbone_varieties": len(ref.backbone),
            }
        },
    )
    return ref
