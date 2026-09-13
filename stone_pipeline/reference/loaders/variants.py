"""The existing-variety file (variants_export.csv, else the Id-free base): one table per category, indexed by
variation id, with junk / deleted / retired / dup-loser rows excluded by the same survivor rule emit uses."""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field
from pathlib import Path

from stone_pipeline.core.text import looks_code_shaped
from stone_pipeline.reference.loaders.common import env, log
from stone_pipeline.reference.loaders.identity import survivor_of, variety_identity


@dataclass
class Variant:
    variation_id: str
    key: str
    name: str
    image: str
    aliases: list[str]


@dataclass
class VariantTable:
    branch: str  # 'slab' or 'block'
    by_id: dict[str, Variant] = field(default_factory=dict)

    def all_ids(self) -> list[str]:
        return list(self.by_id.keys())


def _delete_keys() -> set[str]:
    """Keys flagged in variants_to_delete -- excluded from matching so a junk/phantom variant on its
    way out can't intercept a product match or a fold-in (e.g. a phantom 'White Super ES' must not
    capture the scrape now that it folds into 'Super White')."""
    p = env().SETTINGS.paths.review_dir / "variants_to_delete.csv"
    if not p.exists():
        return set()
    return {(r.get("Key") or "").strip() for r in csv.DictReader(p.open(encoding="utf-8-sig"))
            if (r.get("Key") or "").strip()}


@dataclass
class ExistingVariants:
    """The existing-variety file read ONCE: one table per branch (split by Key prefix), the file it came
    from and the content hash of the bytes actually read (the provenance the manifest records)."""
    tables: dict[str, VariantTable]
    source: Path
    content_hash: str


def _read_variant_records(path: Path) -> tuple[list[dict], str]:
    """One read of the file: the parsed rows and the same digest content_hash() would give."""
    data = path.read_bytes()
    records = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline="")))
    return records, hashlib.sha256(data).hexdigest()[:16]


def load_existing_variants(path: Path, branches: list[str]) -> ExistingVariants:
    """Every branch's variants table from ONE read of the combined file: a row goes to the table whose
    branch is its Key's leading token. Retired/deleted/bare-code/dup-loser exclusion is computed once over
    the whole file, so every branch applies the same survivor rule."""
    path = Path(path)
    if not path.exists():
        log.warning(f"variants file absent: {path}")
        return ExistingVariants({b: VariantTable(branch=b) for b in branches}, path, "absent")
    records, sha = _read_variant_records(path)
    tables = {b: VariantTable(branch=b) for b in branches}

    def _table_for(key: str) -> VariantTable | None:
        return tables.get(key.split("_", 1)[0].casefold())
    _fill_tables(records, _table_for)
    return ExistingVariants(tables, path, sha)


def load_variants(path: Path, branch: str, key_prefix: str | None = None) -> VariantTable:
    """One branch's variants table. key_prefix filters rows by their Key's leading token; None takes every
    row (a single-branch file). load_all uses load_existing_variants (one read for every branch)."""
    table = VariantTable(branch=branch)
    path = Path(path)
    if not path.exists():
        log.warning(f"variants file absent for branch {branch}: {path}")
        return table
    records, _ = _read_variant_records(path)
    prefix = (key_prefix or "").casefold()
    _fill_tables(records, lambda key: table if key.casefold().startswith(prefix) else None)
    return table


def _fill_tables(records: list[dict], table_for) -> None:
    """Index each eligible record into the table `table_for(key)` picks (None = not this table)."""
    # CRITICAL: a RETIRED variety must not be a resolution target here either. The matcher stamps a
    # product's variation_key from THIS reference (built off the lagging Medusa export, which still lists a
    # retired-but-not-yet-deleted variety), and the catalog-side surface exclusion runs too late to undo
    # that stamp. Excluding retired keys at the matcher (like delete_keys) is what actually stops a product
    # re-linking onto a retiring Key (which would FK-fail the eventual ack-done). One durable source: config.db.
    from stone_pipeline.stages import decisions
    delete_keys = env()._delete_keys() | decisions.load_retired()
    # DEDUP the reference to survivors only: the live Medusa export can still list a re-key OLD SIDE that
    # is being deleted. If the matcher can stamp that Key onto a product, the product re-links onto a
    # variety emit will have collapsed away (an orphan / FK-fail on ack). Exclude every non-survivor of a
    # variety_identity group here, using the SAME survivor rule emit uses, so the matcher and emit agree on
    # exactly one Key per variety. Homonyms (different type) are separate identities and all survive.
    _seed = env().pristine_seed_keys()
    # Pick the survivor over ELIGIBLE keys only. survivor_of is blind to the exclusion predicates below
    # (bare_code / delete_keys), so if it picked an INELIGIBLE key (e.g. a re-key OLD SIDE that
    # is in delete_keys) every eligible key of that identity would be dropped as a dup_loser AND the chosen
    # survivor dropped by its own exclusion -- the whole variety vanishes from the matcher reference, so a
    # scrape for it matches nothing and MINTS A DUPLICATE (the exact orphan dup-losing was meant to prevent).
    # Excluding ineligible keys before grouping guarantees a good loser is promoted whenever one exists.
    def _eligible(key: str, name: str) -> bool:
        return not (looks_code_shaped(name) == "bare_code" or key in delete_keys)
    _by_identity: dict = {}
    for rec in records:
        k = (rec.get("Key") or "").strip()
        nm = (rec.get("Name") or "").strip()
        if k and _eligible(k, nm):
            _by_identity.setdefault(variety_identity(k, nm), []).append(k)
    dup_losers = {k for keys in _by_identity.values() if len(keys) > 1
                  for k in keys if k != survivor_of(keys, _seed)}
    delete_keys = delete_keys | dup_losers
    for record in records:
        name = (record.get("Name") or "").strip()
        key = (record.get("Key") or "").strip()
        # The Id-free base is the matcher's fallback source when the live export is absent (post factory
        # reset -- see load_all). It has no Id column, so use the Key as the variation_id: Keys are unique,
        # so every base variety indexes distinctly instead of colliding on an empty id, and the matcher
        # recognizes it rather than minting a duplicate. Products link by Key (match_variation._key_for);
        # the real Medusa id fills in on the next pull. The live export (with an Id) is unaffected.
        vid = (record.get("Id") or "").strip() or key
        if not vid or not name:
            continue
        # Never match products to a JUNK existing variant -- a bare-code one ('Mgt','Gs', a supplier
        # code/brand abbreviation wrongly minted) OR a re-key OLD SIDE (dup_losers above). A variety's
        # stone TYPE is authoritative (assigned from the Key at review), so it is NOT re-judged against
        # the name here: a variety is matchable by its assigned type, never hidden on a name-vs-type guess.
        if looks_code_shaped(name) == "bare_code" or key in delete_keys:
            continue
        table = table_for(key)
        if table is None:
            continue
        aliases_raw = (record.get("Aliases") or "").strip()
        aliases = [a.strip() for a in aliases_raw.split("|") if a.strip()] if aliases_raw else []
        table.by_id[vid] = Variant(
            variation_id=vid, key=key, name=name,
            image=(record.get("Image") or "").strip(), aliases=aliases,
        )
