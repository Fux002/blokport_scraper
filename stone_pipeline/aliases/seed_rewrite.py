"""P2-10: the ONE rewrite of the committed seed, so the backbone (`catalog_source/backbone_varieties.json`) is the whole
seed store and C3 reads the backbone only. `python -m stone_pipeline.aliases.seed_rewrite`, run by hand from the repo
root; it writes only the files in WRITES.

Per backbone post, in this order (C1's judgement, the SAME one C5 classes `removed` by, cleanup.seed_refusal):
- a variant that is a coded form of its base Name ('Rosal B' for 'Rosal') takes the base Name, listed `renamed`
  (the manager, 11-12): emit's coded-name fold, which C4 deletes, made the base Name the variety's name;
- every value the post already holds (its `aliases`, then its `codes`) is judged: a code goes to `codes`, a value C1
  refuses goes to the difference list as `removed`, anything else stays an alias, in its place;
- every value on the Key's committed base rows (development, then production) whose match_key the post does not
  already hold is judged the same way and joins, sorted by (match_key, value).
A base Key with no post (base-only, SC-3: it gets none) keeps no seed value: each value C1 refuses is `removed`, each
other is listed `no_post` (it leaves the seed when C4 derives the base), and a Key with no such value gets one
`no_post` row with an empty Value, so every one is named (the manager's ruling, 11-12).

The judgement's basis (the manager, 11-12): the variety's own name is its committed base Name (the post's variant
only for a post with no base row); the same-type names are the committed base's (what the ledger mirrors), so the two
bases must agree on every Key's Name and Type; the vocabulary is development's committed attributes.csv.

The difference list (`seed_alias_differences.csv`) is the owner's list and an INPUT as well as the output: a value
removed from a post exists nowhere else afterwards, so its row is kept from the previous list and only new
(Key, match_key) rows join. That makes a second run change nothing, and the output is a pure function of the
committed files (sorted, no set order reaches it), so two PYTHONHASHSEED values give the same bytes. The bases are
read, never written (C4 derives their Aliases). No em dashes.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from stone_pipeline.aliases.cleanup import alias_context, seed_refusal, vocabulary
from stone_pipeline.core.alias_problem import code_matcher
from stone_pipeline.core.text import clean_variety_name, match_key
from stone_pipeline.reference.medusa_files import read_variants_export

# --- config ----------------------------------------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
BACKBONE = Path("catalog_source/backbone_varieties.json")
BASES = (Path("from_medusa/development/variants_export_base.csv"),
         Path("from_medusa/production/variants_export_base.csv"))       # judged in this order
ATTRIBUTES = Path("from_medusa/development/attributes.csv")             # the vocabulary's basis
DIFFERENCES = Path("catalog_source/seed_alias_differences.csv")
WRITES = (BACKBONE, DIFFERENCES)
DIFFERENCE_COLUMNS = ("Key", "Value", "Class", "Reason")
REMOVED = "removed"
NO_POST = "no_post"
NO_POST_VALUE = "base-only Key, no backbone post (SC-3): the value leaves the seed when C4 derives the base"
NO_POST_EMPTY = "base-only Key, no backbone post (SC-3): no value to keep"
RENAMED = "renamed"
RENAMED_REASON = "post variant was a coded form of the base Name; renamed to {name!r}, the Key unchanged"


class BasesDisagree(ValueError):
    """The committed bases name a Key differently: the judgement would depend on which base it read."""


class WrongVocabulary(RuntimeError):
    """The vocabulary would not be development's committed attributes.csv (the stated basis)."""


# --- the rewrite -----------------------------------------------------------------------------------------------------
def render(root: Path = REPO_ROOT) -> dict[Path, bytes]:
    """The bytes of every file in WRITES, from the committed files under `root`. Reads only."""
    _require_vocabulary_basis()
    from stone_pipeline.config.domain import active_pack
    type_words, vocabulary_keys = vocabulary()
    judge = _Judge(_identities(root), type_words, vocabulary_keys, code_matcher(active_pack().name_code_pattern))
    doc = json.loads((root / BACKBONE).read_text(encoding="utf-8"))
    differences = _read_differences(root / DIFFERENCES)
    base_values = _base_values(root)
    posted = {post["key"] for post in doc["posts"] if post.get("key")}
    doc["posts"] = [judge.post(post, base_values.get(post.get("key") or "", []), differences)
                    if post.get("key") else post for post in doc["posts"]]
    for key in sorted(set(base_values) - posted):
        judge.no_post(key, base_values[key], differences)
    return {BACKBONE: (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
            DIFFERENCES: _differences_bytes(differences)}


class _Judge:
    def __init__(self, identities: dict[str, tuple[str, str]], type_words: frozenset[str],
                 vocabulary_keys: frozenset[str], is_code):
        self.identities = identities
        self.names_by_type: dict[str, set[str]] = {}
        for name, type_ in identities.values():
            self.names_by_type.setdefault(match_key(type_), set()).add(match_key(name))
        self.type_words, self.vocabulary_keys, self.is_code = type_words, vocabulary_keys, is_code

    def _context(self, key: str, name: str, type_: str):
        name, type_ = self.identities.get(key, (name, type_))
        return alias_context(name, type_, self.names_by_type, self.type_words, self.vocabulary_keys)

    def post(self, post: dict, base_values: list[str], differences: dict) -> dict:
        """The post with its values judged and the base's missing values joined; `codes` written right after
        `aliases`."""
        key = post["key"]
        post = self._named(post, differences)
        context = self._context(key, post.get("variant") or "", post.get("stone_type") or "")
        held = [*(post.get("aliases") or []), *(post.get("codes") or [])]
        held_keys = {match_key(value) for value in held}
        joining: dict[str, str] = {}                    # one value per match_key: its least spelling
        for value in sorted(base_values, key=lambda v: (match_key(v), v)):
            if match_key(value) not in held_keys:
                joining.setdefault(match_key(value), value)
        aliases: list[str] = []
        codes: list[str] = []
        for value in [*held, *joining.values()]:
            if self.is_code(value):
                codes.append(value)
            elif why := seed_refusal(value, context, self.is_code):
                _note(differences, key, value, REMOVED, why)
            else:
                aliases.append(value)
        out: dict = {}
        for field, value in post.items():
            if field == "codes":
                continue
            out[field] = aliases if field == "aliases" else value
            if field == "aliases":
                out["codes"] = codes
        return out

    def _named(self, post: dict, differences: dict) -> dict:
        """A post whose variant is a CODED form of its base Name ('Rosal B' for 'Rosal': clean_variety_name strips
        the code, and the two differ by match_key) takes the base Name (the manager, 11-12); its Key never moves.
        The coded form stays a seed value through the base's alias, and the rename is listed."""
        key, variant = post["key"], post.get("variant") or ""
        name = self.identities.get(key, (variant, ""))[0]
        coded = match_key(variant) != match_key(name) and (clean_variety_name(variant) or variant).casefold() == \
            name.casefold()
        if not coded:
            return post
        _note(differences, key, variant, RENAMED, RENAMED_REASON.format(name=name))
        return {**post, "variant": name}

    def no_post(self, key: str, values: list[str], differences: dict) -> None:
        name, type_ = self.identities[key]
        context = self._context(key, name, type_)
        kept = False
        for value in sorted(set(values), key=lambda v: (match_key(v), v)):
            if why := seed_refusal(value, context, self.is_code):
                _note(differences, key, value, REMOVED, why)
            else:
                _note(differences, key, value, NO_POST, NO_POST_VALUE)
                kept = True
        if not kept:
            _note(differences, key, "", NO_POST, NO_POST_EMPTY)


def _note(differences: dict, key: str, value: str, cls: str, reason: str) -> None:
    """One row per (Key, match_key(Value), Class); a row already listed (a previous run's) is kept as it is."""
    differences.setdefault((key, match_key(value), cls), (key, value, cls, reason))


# --- the committed files ---------------------------------------------------------------------------------------------
def _identities(root: Path) -> dict[str, tuple[str, str]]:
    """Key -> (Name, Type) from the committed bases, which must agree on every Key they share."""
    identities: dict[str, tuple[str, str]] = {}
    for base in BASES:
        for row in read_variants_export(root / base, require_status=False):
            have = identities.setdefault(row.key, (row.name, row.type))
            if have != (row.name, row.type):
                raise BasesDisagree(f"{base}: Key {row.key!r} is {(row.name, row.type)!r}, another base has {have!r}")
    return identities


def _base_values(root: Path) -> dict[str, list[str]]:
    """Key -> every alias its committed base rows hold, in BASES order then file order."""
    values: dict[str, list[str]] = {}
    for base in BASES:
        for row in read_variants_export(root / base, require_status=False):
            values.setdefault(row.key, []).extend(row.aliases)
    return values


def _require_vocabulary_basis() -> None:
    """The vocabulary is read through the settings (cleanup.vocabulary, as C5 reads it), so they must point at the
    repo's development attributes.csv."""
    from stone_pipeline.config.settings import SETTINGS
    if Path(SETTINGS.paths.attributes_csv).resolve() != (REPO_ROOT / ATTRIBUTES).resolve():
        raise WrongVocabulary(f"the vocabulary would come from {SETTINGS.paths.attributes_csv}, not "
                              f"{REPO_ROOT / ATTRIBUTES}; run with SCRAPER_ENV=development")


def _read_differences(path: Path) -> dict[tuple[str, str, str], tuple[str, str, str, str]]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as handle:
        return {(r["Key"], match_key(r["Value"]), r["Class"]): (r["Key"], r["Value"], r["Class"], r["Reason"])
                for r in csv.DictReader(handle)}


def _differences_bytes(differences: dict) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(DIFFERENCE_COLUMNS)
    writer.writerows(row for _, row in sorted(differences.items(), key=lambda item: (*item[0], item[1][1])))
    return out.getvalue().encode("utf-8")


def main() -> int:
    for path, data in render().items():
        target = REPO_ROOT / path
        changed = not target.exists() or target.read_bytes() != data
        if changed:
            target.write_bytes(data)
        print(f"{path}: {'written' if changed else 'unchanged'} ({len(data)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
