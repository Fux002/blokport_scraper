"""P2-10: the ONE rewrite of the committed seed, so the backbone (`catalog_source/backbone_varieties.json`) is the whole
seed store and C3 reads the backbone only. `python -m stone_pipeline.aliases.seed_rewrite`, run by hand from the repo
root; it writes only LIVE_ADDITIONS and the files in WRITES.

The bases it reads are the two committed ones plus LIVE_ADDITIONS: what dev's LIVE base has grown since the committed
snapshot (new Keys whole, new aliases on committed Keys), cut by main() from one pinned S3 version (read-only, its
sha256 checked) and committed, so `render` reads committed files only and CI reproduces it. Re-run before the window
with the VersionId and sha256 bumped, so dev's changes since are in.

Per backbone post, in this order (C1's judgement, the SAME one C5 classes `removed` by, cleanup.seed_refusal):
- FIRST, the source fixes (Mas, 13 Nov; FIXES): a seed value the table names, wherever it is (the post's own, a
  base's, a live addition's), is replaced by the table's terms, or dropped when it names none, and listed `fixed`
  with the table's reason, so no base or live addition brings it back; a fix that names no seed value (and was
  not listed `fixed` before) refuses (StaleFix);
- a variant that is a coded form of its base Name ('Rosal B' for 'Rosal') takes the base Name, listed `renamed`
  (the manager, 11-12): emit's coded-name fold, which C4 deletes, made the base Name the variety's name;
- every value the post already holds (its `aliases`, then its `codes`) is judged: a code goes to `codes`, a value C1
  refuses goes to the difference list as `removed`, anything else stays an alias, in its place;
- every value on the Key's committed base rows (development, then production) whose match_key the post does not
  already hold is judged the same way and joins, sorted by (match_key, value).
A base Key with no post (base-only, SC-3: it gets none) keeps no seed value: each value C1 refuses is `removed`, each
other is listed `no_post` (it leaves the seed when C4 derives the base), and a Key with no such value gets one
`no_post` row with an empty Value, so every one is named (the manager's ruling, 11-12).

The judgement's basis (the manager, 11-12), built by C2's one context builder (aliases.context.build_contexts): the
variety's own name is its base Name (the post's variant only for a post with no base row); the same-type names are the
COMMITTED bases' only, since the seed is shared by both envs (a dev-only mint never refuses a seed value; SC #638's
gate enforces that collision per env at emit); every base must agree on every Key's Name and Type; the vocabulary is
the live names of development's committed attributes.csv.

The difference list (`seed_alias_differences.csv`) is the owner's list and an INPUT as well as the output: a value
removed from a post exists nowhere else afterwards, so its row is kept from the previous list and only new
(Key, match_key) rows join. That makes a second run change nothing, and the output is a pure function of the
committed files (sorted, no set order reaches it), so two PYTHONHASHSEED values give the same bytes. The bases are
read, never written (C4 derives their Aliases). No em dashes.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

from stone_pipeline.aliases.cleanup import seed_refusal
from stone_pipeline.aliases.context import build_contexts
from stone_pipeline.core.alias_problem import AliasContext, code_matcher
from stone_pipeline.core.text import clean_variety_name, match_key
from stone_pipeline.reference.medusa_files import VARIETY_FILE_COLUMNS, ExportVariation, read_variants_export

# --- config ----------------------------------------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
BACKBONE = Path("catalog_source/backbone_varieties.json")
COMMITTED_BASES = (Path("from_medusa/development/variants_export_base.csv"),
                   Path("from_medusa/production/variants_export_base.csv"))
# What dev's LIVE base adds to the committed bases (reviewer 2's L2 on C3, the manager 11-12), read once by main()
# from this exact S3 version. Prod's live base is pre-phase-1 (old Keys): its pass is at prod's P2-12, on an R26 word.
LIVE_ADDITIONS = Path("catalog_source/seed_live_additions_development.csv")
LIVE_BASE_BUCKET = "blokport-dev-staging-3e58a6"
LIVE_BASE_KEY = "dev/scraper/from_medusa/variants_export_base.csv"
LIVE_BASE_VERSION_ID = "rBPreKupBFRCx0rrcPsSBe04bFiZr1ht"
LIVE_BASE_SHA256 = "464056207a34a2ae64f00ac5b68b92e02f80324304237d1f4fb94ba88cceaef7"
BASES = (*COMMITTED_BASES, LIVE_ADDITIONS)                               # judged in this order
ATTRIBUTES = Path("from_medusa/development/attributes.csv")             # the vocabulary's basis
DIFFERENCES = Path("catalog_source/seed_alias_differences.csv")
WRITES = (BACKBONE, DIFFERENCES)                                        # render's files (main() also writes LIVE_ADDITIONS)
DIFFERENCE_COLUMNS = ("Key", "Value", "Class", "Reason")
REMOVED = "removed"
NO_POST = "no_post"
NO_POST_VALUE = "base-only Key, no backbone post (SC-3): the value leaves the seed when C4 derives the base"
NO_POST_EMPTY = "base-only Key, no backbone post (SC-3): no value to keep"
RENAMED = "renamed"
RENAMED_REASON = "post variant was a coded form of the base Name; renamed to {name!r}, the Key unchanged"
# The source fixes (Mas, 13 Nov): a sentence fragment, a concatenation or mojibake in the seed and what it becomes.
FIXES = Path("catalog_source/seed_alias_fixes.csv")
FIX_COLUMNS = ("Key", "Value", "Fix", "Reason")
FIX_SEPARATOR = "|"                                       # the alias separator, so a term can never hold one
FIXED = "fixed"


class BasesDisagree(ValueError):
    """The committed bases name a Key differently: the judgement would depend on which base it read."""


class WrongVocabulary(RuntimeError):
    """The vocabulary would not be development's committed attributes.csv (the stated basis)."""


class BadFix(ValueError):
    """A row of the fixes table is malformed or repeats a (Key, value) another row already fixes."""


class StaleFix(ValueError):
    """A fix names a value no seed store holds: the table no longer describes the seed."""


class LiveBaseMismatch(RuntimeError):
    """The pinned S3 version of dev's live base does not hash to LIVE_BASE_SHA256."""


# --- the rewrite -----------------------------------------------------------------------------------------------------
def render(root: Path = REPO_ROOT) -> dict[Path, bytes]:
    """The bytes of every file in WRITES, from the committed files under `root`. Reads only."""
    _require_vocabulary_basis()
    from stone_pipeline.config.domain import active_pack
    identities = _identities(root, BASES)
    committed = _identities(root, COMMITTED_BASES)
    doc = json.loads((root / BACKBONE).read_text(encoding="utf-8"))
    others = {key: name_type for key, name_type in identities.items() if key not in committed}
    others.update({post["key"]: (post.get("variant") or "", post.get("stone_type") or "")
                   for post in doc["posts"] if post.get("key") and post["key"] not in identities})
    fixes = _read_fixes(root / FIXES)
    judge = _Judge(identities, _contexts(committed, others), code_matcher(active_pack().name_code_pattern), fixes)
    differences = _read_differences(root / DIFFERENCES)
    listed_fixed = {(key, norm) for key, norm, cls in differences if cls == FIXED}
    base_values = _base_values(root)
    posted = {post["key"] for post in doc["posts"] if post.get("key")}
    doc["posts"] = [judge.post(post, base_values.get(post.get("key") or "", []), differences)
                    if post.get("key") else post for post in doc["posts"]]
    for key in sorted(set(base_values) - posted):
        judge.no_post(key, base_values[key], differences)
    if stale := sorted(set(fixes) - judge.used - listed_fixed):
        raise StaleFix(f"{FIXES}: {len(stale)} fix(es) name no seed value: "
                       + "; ".join(f"{key} {fixes[(key, norm)][0]!r}" for key, norm in stale))
    return {BACKBONE: (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
            DIFFERENCES: _differences_bytes(differences)}


def _contexts(committed: dict[str, tuple[str, str]],
              others: dict[str, tuple[str, str]]) -> dict[str, AliasContext]:
    """Every Key's alias-check context, from C2's one builder (aliases.context.build_contexts, the builder C5 judges
    with). The seed is shared by both envs, so the same-type names are the COMMITTED bases' only, never one env's live
    mints (the manager, 11-12; a collision with a dev-only variety is enforced per env at emit, SC #638). A Key the
    committed bases lack (a live-only Key, or a post with no base row) is judged under its own name among those names:
    each is built with only itself added, so it never widens another Key's same-type set."""
    base = [(key, type_, name) for key, (name, type_) in sorted(committed.items())]
    contexts = build_contexts(base)
    for key, (name, type_) in sorted(others.items()):
        contexts[key] = build_contexts([*base, (key, type_, name)])[key]
    return contexts


class _Judge:
    def __init__(self, identities: dict[str, tuple[str, str]], contexts: dict[str, AliasContext], is_code,
                 fixes: dict[tuple[str, str], tuple[str, tuple[str, ...], str]]):
        self.identities, self.contexts, self.is_code, self.fixes = identities, contexts, is_code, fixes
        self.used: set[tuple[str, str]] = set()

    def _fixed(self, key: str, values: list[str], differences: dict) -> list[str]:
        """`values` with each fixed one replaced in place by its terms (none for a drop), each fix used listed `fixed`.
        A term joins only when no other value or earlier term has its match_key; every other value is unchanged."""
        seen = {match_key(value) for value in values if (key, match_key(value)) not in self.fixes}
        out: list[str] = []
        for value in values:
            fix = self.fixes.get((key, match_key(value)))
            if fix is None:
                out.append(value)
                continue
            self.used.add((key, match_key(value)))
            _, terms, reason = fix
            _note(differences, key, value, FIXED, f"{reason} (replaced by {' | '.join(terms)})" if terms else reason)
            for term in terms:
                if match_key(term) not in seen:
                    seen.add(match_key(term))
                    out.append(term)
        return out

    def post(self, post: dict, base_values: list[str], differences: dict) -> dict:
        """The post with its values fixed and judged and the base's missing values joined; `codes` written right after
        `aliases`."""
        key = post["key"]
        post = self._named(post, differences)
        context = self.contexts[key]
        held = self._fixed(key, [*(post.get("aliases") or []), *(post.get("codes") or [])], differences)
        held_keys = {match_key(value) for value in held}
        joining: dict[str, str] = {}                    # one value per match_key: its least spelling
        for value in sorted(self._fixed(key, base_values, differences), key=lambda v: (match_key(v), v)):
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
        context = self.contexts[key]
        kept = False
        for value in sorted(set(self._fixed(key, values, differences)), key=lambda v: (match_key(v), v)):
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
def _identities(root: Path, bases: tuple[Path, ...]) -> dict[str, tuple[str, str]]:
    """Key -> (Name, Type) from `bases`, which must agree on every Key they share."""
    identities: dict[str, tuple[str, str]] = {}
    for base in bases:
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


# --- dev's live base -------------------------------------------------------------------------------------------------
def live_additions(live_text: str, committed: list[ExportVariation]) -> str:
    """What a live base adds to the committed rows, in the base's columns, sorted by Key: a Key the committed rows
    lack, whole; a committed Key, with only the aliases whose match_key it does not already hold (left out when there
    is none). A live row that names or types a committed Key differently refuses (BasesDisagree)."""
    identities = {row.key: (row.name, row.type) for row in committed}
    held: dict[str, set[str]] = {}
    for row in committed:
        held.setdefault(row.key, set()).update(match_key(a) for a in row.aliases)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(VARIETY_FILE_COLUMNS)
    for row in sorted(read_variants_export(live_text, require_status=False, name=LIVE_BASE_KEY), key=lambda r: r.key):
        have = identities.setdefault(row.key, (row.name, row.type))
        if have != (row.name, row.type):
            raise BasesDisagree(f"{LIVE_BASE_KEY}: Key {row.key!r} is {(row.name, row.type)!r}, committed {have!r}")
        new = held.get(row.key)
        adds = [a for a in row.aliases if new is None or match_key(a) not in new]
        if new is None or adds:
            writer.writerow([row.key, row.name, row.type, "|".join(adds)])
        held.setdefault(row.key, set()).update(match_key(a) for a in row.aliases)
    return out.getvalue()


def snapshot_live_base(root: Path = REPO_ROOT) -> bytes:
    """LIVE_ADDITIONS' bytes from the pinned S3 version of dev's live base (read-only; refuses on a hash mismatch)."""
    from stone_pipeline.io.storage import BOTOCORE_DEFAULTS, aws_client
    body = aws_client("s3", config=BOTOCORE_DEFAULTS).get_object(
        Bucket=LIVE_BASE_BUCKET, Key=LIVE_BASE_KEY, VersionId=LIVE_BASE_VERSION_ID)["Body"].read()
    if (digest := hashlib.sha256(body).hexdigest()) != LIVE_BASE_SHA256:
        raise LiveBaseMismatch(f"{LIVE_BASE_KEY} {LIVE_BASE_VERSION_ID} hashes to {digest}, not {LIVE_BASE_SHA256}")
    committed = [row for base in COMMITTED_BASES for row in read_variants_export(root / base, require_status=False)]
    return live_additions(body.decode("utf-8-sig"), committed).encode("utf-8")


def _require_vocabulary_basis() -> None:
    """The vocabulary is read through the settings (cleanup.vocabulary, as C5 reads it), so they must point at the
    repo's development attributes.csv."""
    from stone_pipeline.config.settings import SETTINGS
    if Path(SETTINGS.paths.attributes_csv).resolve() != (REPO_ROOT / ATTRIBUTES).resolve():
        raise WrongVocabulary(f"the vocabulary would come from {SETTINGS.paths.attributes_csv}, not "
                              f"{REPO_ROOT / ATTRIBUTES}; run with SCRAPER_ENV=development")


def _read_fixes(path: Path) -> dict[tuple[str, str], tuple[str, tuple[str, ...], str]]:
    """(Key, match_key(Value)) -> (Value, the replacement terms, Reason). Refuses (BadFix) a file whose columns are not
    FIX_COLUMNS, a row missing its Key, Value or Reason, an empty term, or a second row for one (Key, value)."""
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != FIX_COLUMNS:
            raise BadFix(f"{path}: columns {reader.fieldnames}, expected {list(FIX_COLUMNS)}")
        fixes: dict[tuple[str, str], tuple[str, tuple[str, ...], str]] = {}
        for row in reader:
            key, value, fix, reason = (row[c] for c in FIX_COLUMNS)
            terms = tuple(term.strip() for term in fix.split(FIX_SEPARATOR)) if fix else ()
            if not (key and match_key(value) and reason) or any(not match_key(term) for term in terms):
                raise BadFix(f"{path}: row {row!r} needs a Key, a Value, a Reason and no empty term")
            if (key, match_key(value)) in fixes:
                raise BadFix(f"{path}: {key} {value!r} is fixed twice")
            fixes[(key, match_key(value))] = (value, terms, reason)
    return fixes


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


def _write(path: Path, data: bytes) -> None:
    target = REPO_ROOT / path
    changed = not target.exists() or target.read_bytes() != data
    if changed:
        target.write_bytes(data)
    print(f"{path}: {'written' if changed else 'unchanged'} ({len(data)} bytes)")


def main() -> int:
    """The live snapshot first (render reads it), then the seed."""
    _write(LIVE_ADDITIONS, snapshot_live_base())
    for path, data in render().items():
        _write(path, data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
