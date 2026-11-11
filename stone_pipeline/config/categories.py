"""The category registry: the SINGLE source of truth for categories.

Built at import from the active product-domain pack (config/domains/<pack>.yaml) and this env's Medusa export
(from_medusa/<env>/attributes.csv, for the pcat ids), so SCRAPER_DOMAIN_PACK and the env tier must be set before the
first import. It imports settings, never the reverse, so a settings import alone never builds the registry. The helpers read the module globals that refresh_category_pcats
rebinds, so every name lives here and is read from here: there is no copy in settings. The settings values it uses
(the env's from_medusa folder and the attributes status flag) are read through
the settings module at call time, as they were when this lived there."""

from __future__ import annotations

from dataclasses import dataclass, replace

from stone_pipeline.config import settings
from stone_pipeline.config.domain import active_pack
from stone_pipeline.core import env
from stone_pipeline.reference.medusa_files import read_attributes


@dataclass(frozen=True)
class Category:
    """One product category. Adding a category = ONE entry here (plus its data
    files); every stage derives the category set from this list, so a misspelled
    or unwired category fails loudly instead of silently becoming a slab.

    The behaviour flags let a category opt out of the stone-variety model: slab/
    block/tile are FORMS of one stone variety (shared vocabulary, fan-out, texture
    -swap images), whereas e.g. accessories would set shares_variety_vocab=False,
    fan_out=False, base_image="" (own vocabulary, own backbone, real photos)."""

    name: str                   # canonical lowercase; also the variant Key prefix
    plural: str                 # inbox folder + tree-build group key
    label: str                  # Medusa category name / backbone "category" value
    pcat_id: str                # Medusa product-category id ("" until created)
    base_image: str             # fal.ai base for texture generation ("" = real photo)
    shares_variety_vocab: bool  # shares the stone-variety vocabulary (slab/block/tile)
    fan_out: bool               # a new variety is also created in this category
    volume_per_kg: str = ""     # static "Volume per kg (m³/kg)" written into every variant
    pcat_env_var: str | None = None  # env override for pcat_id (bootstrap); kept so a post-fetch refresh
                                     # re-derives pcat_id the SAME way construction did (see refresh_category_pcats)
    bulk_form: bool = False     # the uncut/solid form (block for stone): drives row.is_block + the block-scale
                                # dimension/freight branch. A domain with no such form leaves this unset everywhere.
    default_form: bool = False  # the form a row falls back to when its format is unresolved; also the finish and
                                # dimension fallback source. Exactly one category declares it (pack-validated).
    finishes: tuple[str, ...] = ()        # its own finish set (D7: block's 18); () for the default form
    finishes_from: str | None = None      # or the category whose finish set it takes (D9: tile -> slab)

    @property
    def active(self) -> bool:
        """Live once its Medusa category id exists."""
        return bool(self.pcat_id)

    @property
    def handle(self) -> str:
        """The category's immutable Medusa HANDLE, the one identifier the sync wire and the ledger carry (PHASE-1-SPEC
        D11): the pack's plural, never the renameable label."""
        return self.plural


# DEV/PROD SEGREGATION: a category's Medusa pcat id is INSTANCE-SPECIFIC (the dev and prod
# Medusa assign different ids), so it must come from the env's own download, never a hardcoded
# value. Read it from from_medusa/<env>/attributes.csv (the same file the reference loads) so a
# prod run uses prod pcats and a dev run uses dev pcats -- the ids can never cross. The literals
# below are only a bootstrap fallback for an env whose attributes.csv isn't downloaded yet.
def _env_category_pcats() -> dict[str, str]:
    path = settings._FROM_MEDUSA / "attributes.csv"
    if not path.exists():
        return {}
    return {a.value: a.sourceid for a in read_attributes(path, require_status=settings.ATTRIBUTES_STATUS_REQUIRED)
            if a.category == "category" and a.sourceid}


_ENV_PCATS = _env_category_pcats()  # {'Slabs': pcat, 'Blocks': pcat, 'Tiles': pcat} for THIS env


def _pcat(label: str, env_var: str | None = None) -> str:
    """A category's Medusa pcat for THIS env, sourced ONLY from the env's Medusa export
    (from_medusa/<env>/attributes.csv) -- never hardcoded. An explicit env-var override is allowed.
    A category absent from the export stays "" (inactive, emits nothing), so dev and prod ids can
    never cross and no stale literal can leak."""
    if env_var and env.getenv(env_var):
        return env.getenv(env_var)
    return _ENV_PCATS.get(label, "")

# The category list is declared by the active product-domain pack (config/domains/<pack>.yaml). Adding a
# category = ADD AN ENTRY THERE; every category-derived constant in the pipeline recomputes from this tuple
# at import. The Medusa pcat id stays env-derived (_pcat, from the env's attributes.csv), never in the pack,
# so dev/prod ids never cross. `_BY_NAME` below is the runtime index the helpers read (activation via the
# env pcat, and tests, can flip a category). The stone pack reproduces the historical slab/block/tile tuple
# exactly. Full recipe: CATEGORY_GUIDE.md.
def _build_categories() -> "tuple[Category, ...]":
    return tuple(
        Category(
            c["name"], c["plural"], c["label"],
            _pcat(c["label"], c.get("pcat_env_var")), c["base_image"],
            shares_variety_vocab=c["shares_variety_vocab"], fan_out=c["fan_out"],
            volume_per_kg=c.get("volume_per_kg", ""),
            pcat_env_var=c.get("pcat_env_var"),
            bulk_form=c.get("bulk_form", False), default_form=c.get("default_form", False),
            finishes=tuple(c.get("finishes") or ()), finishes_from=c.get("finishes_from"))
        for c in active_pack().categories
    )


CATEGORIES: tuple[Category, ...] = _build_categories()

_BY_NAME = {c.name: c for c in CATEGORIES}
_BY_LABEL_CF = {c.label.casefold(): c for c in CATEGORIES}  # plural label, casefolded


def category(name: str) -> "Category | None":
    # accept the singular name ('slab') OR the plural label ('Slabs') so a scraper that tags
    # its format in the plural ('Blocks'/'Tiles') still resolves the explicit tag instead of
    # silently falling through to the slab default.
    key = (name or "").strip().casefold()
    return _BY_NAME.get(key) or _BY_LABEL_CF.get(key)


def category_by_handle(handle: str) -> "Category | None":
    """The category whose handle (D11) is exactly `handle`, or None."""
    return next((c for c in _BY_NAME.values() if c.handle == handle), None)


def default_form_name() -> str:
    """The category a row falls back to when its format is unresolved (also the finish/dimension fallback
    source). Declared by the pack's `default_form: true`; fail loud if the pack declares none (a pack that
    passes _validate_shape always has exactly one, so this raise is only a defence against a bypassed load)."""
    for c in _BY_NAME.values():
        if c.default_form:
            return c.name
    raise RuntimeError("active domain pack declares no default_form category")


def bulk_form_name() -> "str | None":
    """The uncut/solid form's category name (block for stone), or None when the domain has no such form.
    Drives row.is_block and the block-scale dimension/freight branch."""
    for c in _BY_NAME.values():
        if c.bulk_form:
            return c.name
    return None


def active_categories() -> tuple[Category, ...]:
    # iterate _BY_NAME (the runtime registry) so tests can flip a category's pcat
    return tuple(c for c in _BY_NAME.values() if c.active)


def refresh_category_pcats() -> None:
    """Re-read the category pcats from the from_medusa/<env>/attributes.csv now on disk and rebuild the
    runtime registry (_BY_NAME et al., which the helpers read).

    _ENV_PCATS is read ONCE at import, as a bootstrap from whatever export is on disk then. But a produce
    downloads a FRESH attributes.csv AFTER import (produce._fetch_inputs), so a running task whose local
    export changed meaning -- a new Medusa pcat id, or a corrected category label -- would otherwise keep
    gating on the stale import snapshot: every row rejected category_invalid (validate) and new-variety
    fan-out silently disabled (curate), both off the frozen active_categories(). Call this once the fresh
    export is on disk so the registry reflects the live pcats. Each category's pcat_id is re-derived the
    SAME way construction did (_pcat honours its env override), so the refresh is a faithful recompute,
    not a second code path. Idempotent: a no-op when the export is unchanged."""
    global _ENV_PCATS, _BY_NAME, _BY_LABEL_CF
    _ENV_PCATS = _env_category_pcats()
    rebuilt = tuple(replace(c, pcat_id=_pcat(c.label, c.pcat_env_var)) for c in _BY_NAME.values())
    _BY_NAME = {c.name: c for c in rebuilt}
    _BY_LABEL_CF = {c.label.casefold(): c for c in rebuilt}
