"""The ONE builder of curate's existing-variety index for tests: varieties as (key, name, aliases[, archived]) tuples,
indexed exactly as curate.existing.load_existing indexes the export (by (norm name, norm Key type) and by norm
name). A variety's Type is its Key's type slug read as a name, which is right for every one-word test type."""
from __future__ import annotations

from stone_pipeline.matching import projections as proj
from stone_pipeline.reference.loaders import type_slug_from_key
from stone_pipeline.stages.curate import ImportFile


def existing_index(*varieties: tuple) -> ImportFile:
    imp = ImportFile(path=None)
    for key, name, aliases, *archived in varieties:
        slug = type_slug_from_key(key)
        v = {"Key": key, "Name": name, "Type": slug.replace("_", " ").title(), "Aliases": aliases,
             "type": proj.norm(slug), "archived": bool(archived and archived[0])}
        imp.varieties.append(v)
        imp.by_name_type[(proj.norm(name), v["type"])] = v
        imp.by_name[proj.norm(name)] = v
    return imp
