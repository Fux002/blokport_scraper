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


def seed_of(backbone) -> dict[str, tuple[Sequence[str], Sequence[str]]]:
    """Key -> (aliases, codes) of every keyed backbone post: the seed's one store of record."""
    return {key: (tuple(v.aliases), tuple(v.codes)) for key, v in backbone.by_key.items()}


def operator_aliases(rows: Iterable[Mapping]) -> list[OperatorAlias]:
    """The operator's aliases in `statement` rows: a GLOBAL (vendor '') `is` statement whose stated name is not the
    spelling. The alias is the spelling's variety identity (clean_variety under the stated type, format and type
    words dropped), the very value curate attaches to a renamed mint (context.variety_identity, minting.py), so a
    mint's own spelling lives on in this store after its run. A vendor's binding is match-only (the matcher's
    override tier), never an alias of the variety."""
    from stone_pipeline.adapters.tokens import clean_variety
    out = []
    for r in rows:
        name, spelling = (r.get("name") or "").strip(), (r.get("spelling") or r.get("spelling_norm") or "").strip()
        if (r.get("source") or "").strip() or r.get("verdict") != "is" or not name or not spelling:
            continue
        stone_type = (r.get("stone_type") or "").strip()
        clean = clean_variety(spelling, stone_type)
        if match_key(name) != match_key(clean):
            out.append(OperatorAlias(name, stone_type, clean))
    return out


def scraper_stores(backbone=None) -> Stores:
    """The scraper's own stores (seed, operator, active learned); the hand rows come with the export a caller reads.
    The statements are read only when config.db exists, and the learned reader creates no store either (a read never
    creates it). A proposed or rejected learned alias is no surface."""
    from stone_pipeline.config import decisions_store, store
    from stone_pipeline.reference.loaders.backbone import load_backbone
    rows = decisions_store.statement_rows() if store.config_db_path().exists() else []
    return Stores(seed=seed_of(backbone if backbone is not None else load_backbone()),
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
