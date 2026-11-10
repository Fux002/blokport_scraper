"""The EXISTING varieties: one read of the file the matcher's candidate index is built from, indexed by (normalized
Name, normalized stone TYPE). A variety is one record per (type, name) whatever category its products sit in, so
there is one index, not one per category. Curate decides alias-vs-new and dedups against it."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from stone_pipeline.matching import projections as proj
from stone_pipeline.reference.loaders import existing_varieties_file, type_slug_from_key, variants_status_required
from stone_pipeline.reference.medusa_files import read_variants_export


@dataclass
class ImportFile:
    path: Path | None
    # A variety's identity is (name, TYPE), not name alone: the SAME name legitimately exists as several
    # stones ('Aqua Blue' is a gneiss AND a granite AND a marble AND an onyx). Keying by name alone
    # collapsed them to one arbitrary type (last row wins), so a type-less scrape was silently tied to the
    # wrong type and the mint-dedup went blind to the hidden types. Keep every variety and index by
    # (norm name, norm type); `type` on each variety is derived from its Key (the authority).
    varieties: list[dict] = field(default_factory=list)              # each: {Key,Name,Type,Aliases,type,archived}
    by_name_type: dict[tuple[str, str], dict] = field(default_factory=dict)  # (norm name, norm type) -> variety
    by_name: dict[str, dict] = field(default_factory=dict)           # norm name -> variety (name-only lookup)


def load_existing() -> ImportFile:
    """The EXISTING varieties from ONE read of the file the matcher's candidate index is built from
    (`existing_varieties_file`: the live Medusa export, else the committed base), indexed by (normalized Name,
    normalized stone TYPE). Used to decide alias-vs-new and to dedup. Neither file is written by the pipeline,
    so the catalog is a pure function of (export + scrapes): re-running yields the identical output. A missing
    file raises: an empty existing index would mint every known variety again. A Key not of the variety-Key form
    raises too (type_slug_from_key): its type is never guessed."""
    path = existing_varieties_file()
    imp = ImportFile(path=path)
    for r in read_variants_export(path, require_status=variants_status_required(path)):
        if not r.name:
            continue
        v = {
            "Key": r.key,
            "Name": r.name,
            "Type": r.type,                                     # the canonical type NAME a variety row carries
            "Aliases": "|".join(r.aliases),
            "type": proj.norm(type_slug_from_key(r.key)),       # the identity's stone type, from its Key
            # VE2 PR5: the export's archived flag. An archived variety STAYS in the index (a scrape still
            # recognizes it, no re-mint); classify uses the flag to surface a card when a gapped scrape
            # would need a NEW alias onto a frozen variety (which the lane never serves, so it would
            # otherwise stay a tree gap forever). False in the expand phase, so the index is unchanged.
            "archived": r.archived,
        }
        imp.varieties.append(v)
        imp.by_name_type[(proj.norm(r.name), v["type"])] = v
        imp.by_name[proj.norm(r.name)] = v   # name-only lookup (a review suggestion has no type)
    return imp


def alias_list(raw: str) -> list[str]:
    return [a.strip() for a in (raw or "").split("|") if a.strip()]
