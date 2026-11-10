"""One-off: bring every name the scraper COMMITS to its field's fixed point (report 21 step 4; plan section 3).

Run once from the repo root, committed together with its output:

    python -m stone_pipeline.reference.clean_committed_names

The kinds come from the test fixture of field-kinds.json (no runtime copy exists; plan M-4). The base files are
cleaned by emit's own per-row writer, so the base is exactly what emit produces and seed.verify proves it. Each file is
rewritten in its own byte form (line ending, final newline), so only the changed cells differ. It refuses, and writes
NOTHING, when a variety name has no ASCII form, a tile Key would move, two rows of one category and type would share a
cleaned Name, or a second pass would change anything. A second run changes 0 bytes.
"""

from __future__ import annotations

import csv
import io
import json
from functools import partial
from pathlib import Path

from stone_pipeline.core.text import clean_alias_list
from stone_pipeline.phase1.variety_key import new_key
from stone_pipeline.reference.loaders import field_kinds as fk
from stone_pipeline.reference.loaders.identity import type_slug_from_key

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "stone_pipeline" / "tests" / "fixtures" / "field-kinds.json"
BASES = ["from_medusa/development/variants_export_base.csv", "from_medusa/production/variants_export_base.csv"]
# the ONE variety backbone and the ONE committed additions file (spec rev 8 section 3)
BACKBONES = ["catalog_source/backbone_varieties.json", "catalog_source/backbone_additions/varieties.json"]
VARIETY_BACKBONE = "catalog_source/backbone_varieties.json"
# (file, {column: (field, rule)}): rule "display" for a variety Name (title-cased as emit writes it), "clean" for a
# place, port or vocabulary name (case kept)
TABLES = [
    ("catalog_source/origin_map.csv", {"variety": (fk.VARIATION_NAME, "display"), "stone_type": (fk.TYPE_NAME, "clean"),
                                       "city": (fk.PRODUCT_ORIGIN_CITY, "clean"),
                                       "county": (fk.PRODUCT_ORIGIN_COUNTY, "clean")}),
    ("catalog_source/ports.csv", {"name": (fk.PORT_NAME, "clean"), "region_name": (fk.PORT_REGION_NAME, "clean")}),
    ("catalog_source/backbone_additions/backbone_value_updates.csv", {"variety": (fk.VARIATION_NAME, "display")}),
    # a synonym's canonical value is Medusa's vocabulary spelling: cleaned, never re-cased
    ("stone_pipeline/reference/synonyms/color.csv", {"canonical": (fk.COLOR_NAME, "clean")}),
    ("stone_pipeline/reference/synonyms/finish.csv", {"canonical": (fk.FINISH_NAME, "clean")}),
    ("stone_pipeline/reference/synonyms/type.csv", {"canonical": (fk.TYPE_NAME, "clean")}),
    ("stone_pipeline/reference/synonyms/quality.csv", {"canonical": (fk.QUALITY_NAME, "clean")}),
]
_POST_FIELDS = {"stone_type": fk.TYPE_NAME, "category": fk.PRODUCT_CATEGORY_NAME}
_POST_LISTS = {"color": fk.COLOR_NAME, "finishes": fk.FINISH_NAME, "qualities": fk.QUALITY_NAME}


class Refused(RuntimeError):
    """The cleanup would change identity or is not a fixed point: nothing is written."""


def _rule(field: str, rule: str):
    return partial(fk.display if rule == "display" else fk.clean, field)


# --- CSV ----------------------------------------------------------------------------------------------------------
def _read_csv(text: str) -> tuple[list[str], list[dict]]:
    reader = csv.DictReader(io.StringIO(text, newline=""))
    return list(reader.fieldnames or []), list(reader)


def _write_csv(header: list[str], rows: list[dict], like: str) -> str:
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=header, lineterminator="\r\n" if "\r\n" in like else "\n")
    writer.writeheader()
    writer.writerows(rows)
    out = buf.getvalue()
    return out if like.endswith("\n") else out.rstrip("\r\n")


def clean_base(text: str) -> str:
    from stone_pipeline.stages import emit_catalog
    header, rows = _read_csv(text)
    seen: dict[tuple[str, str], str] = {}
    for row in rows:
        emit_catalog._write_names(row)              # emit's own rule: raises for a Name with no ASCII form
        ident = (type_slug_from_key(row["Key"]), row["Name"].casefold())   # a variety: (type, name), no category
        if ident in seen and seen[ident] != row["Key"]:
            raise Refused(f"{row['Key']} and {seen[ident]} would share the cleaned Name {row['Name']!r}")
        seen[ident] = row["Key"]
    return _write_csv(header, rows, text)


def clean_table(text: str, columns: dict[str, tuple[str, str]]) -> str:
    header, rows = _read_csv(text)
    for row in rows:
        for column, (field, rule) in columns.items():
            if row.get(column):
                row[column] = _rule(field, rule)(row[column])
    return _write_csv(header, rows, text)


# --- JSON backbones -------------------------------------------------------------------------------------------------
def clean_backbone(text: str) -> str:
    """variant cleaned with its case kept and no bracket split (its key never moves); aliases by the alias rule;
    leaf values (type, category, colours, finishes, qualities) are Medusa's vocabulary spellings, cleaned with their
    case kept, never re-cased ('Brushed and Tumbled'). merged_from is NOT cleaned: it is the audit record of what a
    merge removed."""
    doc = json.loads(text)
    posts = doc["posts"] if isinstance(doc, dict) else doc
    alias = partial(fk.display, fk.VARIATION_ALIAS_VALUE)
    for post in posts:
        post["variant"] = fk.clean(fk.VARIATION_NAME, post["variant"])
        for key, field in _POST_FIELDS.items():
            if post.get(key):
                post[key] = fk.clean(field, post[key])
        for key, field in _POST_LISTS.items():
            if post.get(key):
                post[key] = list(dict.fromkeys(fk.clean(field, v) for v in post[key]))
        if "aliases" in post:
            post["aliases"], _ = clean_alias_list(post["variant"], post["aliases"], alias)
    return json.dumps(doc, indent=2, ensure_ascii=False) + ("\n" if text.endswith("\n") else "")


def _check_keys_stay(before: dict[str, str], after: dict[str, str]) -> None:
    """No Key moves: every variety post's deterministic Key (the one rule, over its type and name) is the same
    before and after cleaning. Compared before/after, so a post already missing on main is not counted."""
    old, new = (json.loads(t[VARIETY_BACKBONE])["posts"] for t in (before, after))
    moved = [o["variant"] for o, n in zip(old, new)
             if new_key(o["stone_type"], o["variant"]) != new_key(n["stone_type"], n["variant"])]
    if moved:
        raise Refused(f"{len(moved)} Keys would move, first {moved[:5]}")


def plan() -> dict[str, str]:
    """{path: new text} for every file whose text changes; raises Refused, writing nothing."""
    fk._path = lambda: FIXTURE                     # the fixture is the only committed copy of the map
    # bytes decoded as they are: read_text would translate CRLF to LF and every CRLF file would change
    texts = {p: (ROOT / p).read_bytes().decode("utf-8") for p in [*BASES, *BACKBONES, *(t for t, _ in TABLES)]}
    cleaners = {**{p: clean_base for p in BASES}, **{p: clean_backbone for p in BACKBONES},
                **{t: partial(clean_table, columns=c) for t, c in TABLES}}
    out = {p: cleaners[p](texts[p]) for p in texts}
    _check_keys_stay(texts, out)
    unstable = [p for p in out if cleaners[p](out[p]) != out[p]]
    if unstable:
        raise Refused(f"not a fixed point after one pass: {unstable}")
    return {p: t for p, t in out.items() if t != texts[p]}


def main() -> int:
    changes = plan()
    for path, text in changes.items():
        (ROOT / path).write_bytes(text.encode("utf-8"))
    print(f"cleaned {len(changes)} file(s): {sorted(changes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
