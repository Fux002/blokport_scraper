"""The existing-variety file (variants_export.csv, else the Id-free base): ONE table of varieties (phase 1: a
variety is one record per (type, name) and has no category), indexed by variation id, with junk / deleted / retired /
dup-loser rows excluded by the same survivor rule emit uses. Every category that shares the stone-variety vocabulary
matches against this one table. A Key of the old prefixed form is refused (the one parser, phase1.variety_key)."""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from stone_pipeline.config.settings import VARIANTS_EXPORT_STATUS_REQUIRED
from stone_pipeline.core.text import looks_code_shaped
from stone_pipeline.phase1.variety_key import variety_key_type
from stone_pipeline.reference.loaders.common import env, log
from stone_pipeline.reference.loaders.identity import survivor_of, variety_identity
from stone_pipeline.reference.medusa_files import ExportVariation, read_variants_export


@dataclass
class Variant:
    variation_id: str
    key: str
    name: str
    aliases: list[str]
    # VE2: the export marks this variety archived. It STAYS in the matcher (recognition keeps the full
    # vocabulary), but the matcher must not LEARN a new alias onto it (match_variation write-back).
    archived: bool = False


@dataclass
class VariantTable:
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
    """The existing-variety file read ONCE: the ONE variety table under each category that shares the
    stone-variety vocabulary (the same object for each), the file it came from and the content hash of the bytes
    actually read (the provenance the manifest records)."""
    tables: dict[str, VariantTable]
    source: Path
    content_hash: str


def variants_status_required(path: Path) -> bool:
    """Whether reading `path` requires the status column: only Medusa's live export carries it. The existing-variety
    file can also be the scraper's Id-free base, which never has the column, so the base is always read with
    require_status False."""
    return VARIANTS_EXPORT_STATUS_REQUIRED and Path(path) == env().SETTINGS.paths.export_file


def _read_variant_records(path: Path) -> tuple[list[ExportVariation], str]:
    """One read of the file: the parsed rows and the same digest content_hash() would give."""
    data = path.read_bytes()
    records = read_variants_export(data.decode("utf-8-sig"), require_status=variants_status_required(path),
                                   name=str(path))
    return records, hashlib.sha256(data).hexdigest()[:16]


def load_existing_variants(path: Path, categories: list[str]) -> ExistingVariants:
    """The one variety table from ONE read of the file, under each of `categories`: a variety belongs to the stone,
    not a format, so every category's engine matches against every variety, under the same Key and id."""
    path = Path(path)
    if not path.exists():
        log.warning(f"variants file absent: {path}")
        table, sha = VariantTable(), "absent"
    else:
        records, sha = _read_variant_records(path)
        table = _table(records)
    return ExistingVariants({c: table for c in categories}, path, sha)


def load_variants(path: Path) -> VariantTable:
    """The one variety table of a file (an absent file is an empty table)."""
    path = Path(path)
    if not path.exists():
        log.warning(f"variants file absent: {path}")
        return VariantTable()
    records, _ = _read_variant_records(path)
    return _table(records)


def _table(records: list[ExportVariation]) -> VariantTable:
    """The table of every eligible record. Every record's Key is read by the one parser first, an excluded row's
    too: a Key it refuses (an old prefixed Key) raises, never read as some category's or skipped silently."""
    for record in records:
        if record.key:
            variety_key_type(record.key)
    table = VariantTable()
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
        if rec.key and _eligible(rec.key, rec.name):
            _by_identity.setdefault(variety_identity(rec.key, rec.name), []).append(rec.key)
    dup_losers = {k for keys in _by_identity.values() if len(keys) > 1
                  for k in keys if k != survivor_of(keys, _seed)}
    delete_keys = delete_keys | dup_losers
    for record in records:
        name, key = record.name, record.key
        # The Id-free base is the matcher's fallback source when the live export is absent (post factory
        # reset -- see load_all). It has no Id column, so use the Key as the variation_id: Keys are unique,
        # so every base variety indexes distinctly instead of colliding on an empty id, and the matcher
        # recognizes it rather than minting a duplicate. Products link by Key (match_variation._key_for);
        # the real Medusa id fills in on the next pull. The live export (with an Id) is unaffected.
        vid = record.id or key
        if not vid or not name:
            continue
        # Never match products to a JUNK existing variant -- a bare-code one ('Mgt','Gs', a supplier
        # code/brand abbreviation wrongly minted) OR a re-key OLD SIDE (dup_losers above). A variety's
        # stone TYPE is authoritative (assigned from the Key at review), so it is NOT re-judged against
        # the name here: a variety is matchable by its assigned type, never hidden on a name-vs-type guess.
        if looks_code_shaped(name) == "bare_code" or key in delete_keys:
            continue
        table.by_id[vid] = Variant(
            variation_id=vid, key=key, name=name, aliases=list(record.aliases),
            archived=record.archived,
        )
    return table
