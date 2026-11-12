"""The alias stores read from where they live, for aliases.surfaces (P2-10 C3): the seed from the backbone's posts, the
operator's renames from config.db's `statement` table, the ACTIVE learned aliases from config.db's `learned_alias` (C2),
and the backend's hand rows from the export's AdminAliases and AdminCodes columns. The matcher (reference.loaders) and
alias_target (config.varieties) both read through here, so they can never see two different alias sets. No em dashes."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from stone_pipeline.aliases.surfaces import OperatorAlias, Stores
from stone_pipeline.core.alias_problem import code_matcher
from stone_pipeline.core.text import match_key

log = logging.getLogger("stone_pipeline.aliases")


def is_code() -> Callable[[str], bool]:
    """The active brand's code test (its pack's name_code_pattern; None means the brand has no codes)."""
    from stone_pipeline.config.domain import active_pack
    return code_matcher(active_pack().name_code_pattern)


def export_file() -> Path:
    """The backend's variants export, whose AdminAliases and AdminCodes are the hand rows."""
    from stone_pipeline.config.settings import SETTINGS
    return SETTINGS.paths.export_file


def export_version() -> tuple[str, int, int] | None:
    """The export's (path, size, mtime), or None when it is absent: what a cache keyed on the export compares, never
    a parse (reference.medusa_files.read_variants_export is the one reader)."""
    path = export_file()
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (str(path), stat.st_size, stat.st_mtime_ns)


def seed_of(backbone) -> dict[str, tuple[Sequence[str], Sequence[str]]]:
    """Key -> (aliases, codes) of every keyed backbone post: the seed's one store of record."""
    return {key: (tuple(v.aliases), tuple(v.codes)) for key, v in backbone.by_key.items()}


def operator_alias(spelling: str, stated_type: str) -> str:
    """THE value an operator rename makes an alias of: the stated spelling's variety identity under the stated type
    (clean_variety: format and type words dropped). Curate's mint (minting.py) and this store both take it from here,
    so a renamed mint's own spelling and the operator store's alias can never differ."""
    from stone_pipeline.adapters.tokens import clean_variety
    return clean_variety(spelling, stated_type)


def operator_aliases(rows: Iterable[Mapping]) -> list[OperatorAlias]:
    """The operator's aliases in `statement` rows: a GLOBAL (vendor '') `is` statement whose stated name is not its
    alias value (operator_alias), so a mint's own spelling lives on in this store after its run. A vendor's binding
    is match-only (the matcher's override tier), never an alias of the variety."""
    out = []
    for r in rows:
        name, spelling = (r.get("name") or "").strip(), (r.get("spelling") or r.get("spelling_norm") or "").strip()
        if (r.get("source") or "").strip() or r.get("verdict") != "is" or not name or not spelling:
            continue
        stone_type = (r.get("stone_type") or "").strip()
        value = operator_alias(spelling, stone_type)
        if match_key(name) != match_key(value):
            out.append(OperatorAlias(name, stone_type, value))
    return out


# The seed and the export's hand rows, each kept for the one file version it was read from: alias_target rebuilds on
# every config.db write (a decision), and re-parsing the 12,156-post backbone and the export each time would put that
# parse on every decision. One entry each, replaced when the file changes.
_SEED: dict = {}
_ADMIN: dict = {}


def _backbone_version() -> tuple | None:
    from stone_pipeline.config.settings import SETTINGS
    path = SETTINGS.paths.backbone_json
    if not path.exists():
        return None                      # load_backbone raises the named error
    stat = path.stat()
    return (str(path), stat.st_size, stat.st_mtime_ns)


def _seed(backbone) -> Mapping[str, tuple[Sequence[str], Sequence[str]]]:
    """The seed of `backbone`, or, with none, of the backbone file: parsed once per file version, and a backbone the
    caller parsed from that very file (load_all's) is reused, never parsed twice."""
    from stone_pipeline.reference.loaders.backbone import load_backbone
    version = _backbone_version()
    if backbone is None and version is not None and version in _SEED:
        return _SEED[version]
    if backbone is None:
        backbone = load_backbone()
    seed = seed_of(backbone)
    if version is not None and backbone.version == version:
        _SEED.clear()
        _SEED[version] = seed
    return seed


def export_admin_rows() -> dict[str, tuple[Sequence[str], Sequence[str]]]:
    """Key -> (AdminAliases, AdminCodes) of the backend's export, read once per export version; {} with no export."""
    version = export_version()
    if version is None:
        return {}
    if version not in _ADMIN:
        from stone_pipeline.reference.medusa_files import read_variants_export
        _ADMIN.clear()
        _ADMIN[version] = admin_of(read_variants_export(export_file(), require_status=False))
    return _ADMIN[version]


def scraper_stores(backbone=None) -> Stores:
    """The scraper's own stores (seed, operator, active learned); the hand rows come with the export a caller reads.
    The statements are read only when config.db exists, and the learned reader creates no store either (a read never
    creates it). A proposed or rejected learned alias is no surface."""
    from stone_pipeline.config import decisions_store, store
    rows = decisions_store.statement_rows() if store.config_db_path().exists() else []
    return Stores(seed=_seed(backbone),
                  operator=operator_aliases(rows),
                  learned=[(r["identity_key"], r["value"]) for r in decisions_store.active_learned_aliases()])


def admin_of(records) -> dict[str, tuple[Sequence[str], Sequence[str]]]:
    """Key -> (AdminAliases, AdminCodes) of export records."""
    return {r.key: (r.admin_aliases, r.admin_codes) for r in records if r.key}


def log_unbound(unbound: Sequence[OperatorAlias]) -> None:
    """An operator alias naming no ONE variety is skipped, never bound to a guess, and said."""
    if unbound:
        log.warning("operator aliases that name no one variety are not surfaces", extra={"extra_fields": {
            "count": len(unbound), "aliases": [(o.spelling, o.name, o.stone_type) for o in unbound[:20]]}})
