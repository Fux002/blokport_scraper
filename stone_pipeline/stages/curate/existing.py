"""The EXISTING varieties: one read of the file the matcher's candidate index is built from, indexed per
category by (normalized Name, normalized stone TYPE). Curate decides alias-vs-new and dedups against it."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from stone_pipeline.matching import projections as proj
from stone_pipeline.reference.loaders import existing_varieties_file, type_slug_from_key
from stone_pipeline.stages.curate.keys import BRANCHES


@dataclass
class ImportFile:
    branch: str
    path: Path | None
    # A variety's identity is (name, TYPE), not name alone: the SAME name legitimately exists as several
    # stones ('Aqua Blue' is a gneiss AND a granite AND a marble AND an onyx). Keying by name alone
    # collapsed them to one arbitrary type (last row wins), so a type-less scrape was silently tied to the
    # wrong type and the mint-dedup went blind to the hidden types. Keep every variety and index by
    # (norm name, norm type); `type` on each variety is derived from its Key (the authority).
    varieties: list[dict] = field(default_factory=list)              # each: {Key,Name,Image,Aliases,Volume,type}
    by_name_type: dict[tuple[str, str], dict] = field(default_factory=dict)  # (norm name, norm type) -> variety
    by_name: dict[str, dict] = field(default_factory=dict)           # norm name -> variety (name-only lookup)


def load_all_existing() -> dict[str, ImportFile]:
    """The EXISTING variants of every category from ONE read of the file the matcher's candidate index is
    built from (`existing_varieties_file`: the live Medusa export, else the committed base), each indexed by
    (normalized Name, normalized stone TYPE). Used to decide alias-vs-new and to dedup. Neither file is written
    by the pipeline, so the catalog is a pure function of (export + scrapes) -- re-running yields the identical
    output. A missing file raises: an empty existing index would mint every known variety again."""
    path = existing_varieties_file()
    imports = {b: ImportFile(branch=b, path=path) for b in BRANCHES}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for r in csv.DictReader(handle):
            name = (r.get("Name") or "").strip()
            key = (r.get("Key") or "").strip()
            imp = imports.get(key.split("_", 1)[0].casefold())     # one combined file; the branch is the Key prefix
            if name and imp is not None:
                v = {
                    "Key": key,
                    "Name": name,
                    "Image": (r.get("Image") or "").strip(),
                    "Aliases": (r.get("Aliases") or "").strip(),
                    "Volume": (r.get("Volume per kg (m³/kg)") or "").strip(),
                    "type": proj.norm(type_slug_from_key(key)),   # the variety's stone type, from its Key
                }
                imp.varieties.append(v)
                imp.by_name_type[(proj.norm(name), v["type"])] = v
                imp.by_name[proj.norm(name)] = v   # name-only lookup (a review suggestion has no type)
    return imports


def load_existing(branch: str) -> ImportFile:
    """One branch's view of load_all_existing (single-branch callers)."""
    return load_all_existing()[branch]


def alias_list(raw: str) -> list[str]:
    return [a.strip() for a in (raw or "").split("|") if a.strip()]
