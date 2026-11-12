"""The curation outputs of one produce: the Medusa import DELTA, the backbone additions, and the operator
queues (variety cards, origin cards, leaf suggestions). The full upload file is emit_catalog's."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from stone_pipeline.config import decisions_store
from stone_pipeline.config.settings import SETTINGS, VARIANTS_EXPORT_STATUS_REQUIRED
from stone_pipeline.core import csvio, logfmt
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.matching import projections as proj
from stone_pipeline.reference.medusa_files import VARIETY_FILE_COLUMNS, read_variants_export
from stone_pipeline.stages.curate.context import CurationResult

log = logfmt.get_logger("curate")

_IMPORT_COLS = VARIETY_FILE_COLUMNS


def _write_csv(path: Path, cols: list[str], rows: list[dict], sanitize: bool = True) -> None:
    # atomic always. sanitize=True (formula-injection-safe) for operator-opened files; sanitize=False
    # for a MEDUSA IMPORT file, where a leading "'" prepended to a Name/Alias would corrupt the data.
    csvio.write_dicts(path, cols, rows, sanitize=sanitize)


def write_curation(result: CurationResult, rows: list[CanonicalRow]) -> None:
    """Record curate's learned-alias proposals in config.db (P2-10 C2: a guess is a proposal, never an import row),
    then write the curation files."""
    result.counts["learned_proposed"] = decisions_store.propose_learned_aliases(result.learned_proposals)
    _write_files(result, rows)


def _write_files(result: CurationResult, rows: list[CanonicalRow]) -> None:
    """Write the catalog curation outputs to the fixed top-level folders:
      outputs/<env>/curate_delta.csv       the new-variety DELTA, raw (emit publishes it cleaned)
      catalog_source/backbone_additions/   new varieties to append to the backbones
      review/                              the human-decision aids (never uploaded)
    Both upload files (to_upload/1_variants_full.csv and 1_variants_update.csv) are produced by emit_catalog."""
    p = SETTINGS.paths
    additions = p.backbone_additions_dir
    upload_rows = list(result.new_variants)
    # the backbone delta: this run's new varieties, one post each. ALWAYS (over)written, an empty list
    # included, so a variety that is no longer minted this run (resolved, held, or now code-detected like
    # 'Mgt Onyx') can never PERSIST as a stale addition from a prior run (which would keep feeding the image
    # queue + tree).
    p.backbone_additions_file.parent.mkdir(parents=True, exist_ok=True)
    p.backbone_additions_file.write_text(json.dumps(result.backbone_new, indent=2, ensure_ascii=False),
                                         encoding="utf-8")
    # Drop no-op rows: a variant whose Key is ALREADY in the export and that adds no new alias is just upsert
    # noise (the mint guard can miss a duplicate-name generic). Keep the update file to the genuine delta: a new
    # variant, or a mint row whose Key the export already holds only when it adds an alias (a variety file carries
    # no image since SC-4 2b).
    exp_idx: dict[str, set] = {}
    ef = SETTINGS.paths.export_file
    if ef.exists():
        for r in read_variants_export(ef, require_status=VARIANTS_EXPORT_STATUS_REQUIRED):
            if r.key:
                exp_idx[r.key] = {proj.norm(a) for a in r.aliases}

    def _is_real_delta(row: dict) -> bool:
        e = exp_idx.get(row["Key"])
        if e is None:
            return True                                    # genuinely new variant (key not in export)
        cur = {proj.norm(a) for a in (row.get("Aliases") or "").split("|") if a.strip()}
        return bool(cur - e)                                # a new alias

    # never offer a variant that is flagged for deletion (mis-typed/junk) for (re)loading
    _del = SETTINGS.paths.review_dir / "variants_to_delete.csv"
    _delete_keys: set[str] = set()
    if _del.exists():
        with _del.open(encoding="utf-8-sig") as _handle:   # close the handle (was leaked in a comprehension)
            _delete_keys = {(r.get("Key") or "").strip() for r in csv.DictReader(_handle)}
    before = len(upload_rows)
    upload_rows = [r for r in upload_rows if _is_real_delta(r) and r["Key"] not in _delete_keys]
    if before != len(upload_rows):
        log.info("update delta pruned", extra={"extra_fields": {"dropped_noops": before - len(upload_rows)}})
    # ALWAYS written, header-only when empty: emit_catalog reads this RAW delta as THIS run's delta and
    # publishes the cleaned 1_variants_update.csv from it, so skipping an empty write would leave a previous
    # run's delta in place to be applied again.
    _write_csv(p.curate_delta_csv, _IMPORT_COLS, upload_rows, sanitize=False)
    # the ONLY review file from curate: the decision ledger (uncertain new varieties, confirm
    # true/false). Always (re)written so applied/rejected rows drop out. Advisory sets (uncertain
    # alias spellings, code-like names, images) are logged as counts, not clutter files.
    from stone_pipeline.stages import decisions
    decisions.write_confirm_file(result.pending_confirm)
    # SEPARATE origin queue: rows held because their origin is not known for the vendor (origin_needs_confirmation:
    # a primary_origin the map does not corroborate, or several map countries none of them the supplier's
    # home). One entry per (source, variety, type); never mixed into the variety queue
    # above. Always called (empty clears it) so a confirmed origin drops off, like the variety queue.
    decisions.write_origin_confirm_file(rows)
    if result.backbone_updates:   # human-readable audit of the leaf additions (the queue below is the surface)
        _write_csv(additions / "backbone_value_updates.csv", decisions.LEAF_COLUMNS, result.backbone_updates)
    # surface the leaf additions for operator review (:4200) -> approve grows the backbone overlay next run.
    # Always called (empty clears the queue) so a resolved suggestion drops off, like the variety queue.
    decisions.write_backbone_leaf_pending(result.backbone_updates)
    # DURABLE membership for THIS run's mints: persist each new variety's own colour/finish/quality as an
    # approved leaf decision, so it survives past this produce. The backbone_additions files above stay
    # per-run (the texture queue reads them for new-this-run varieties); durability lives in the overlay,
    # which reconcile's apply_leaf_overlay turns into a record -- closing the base<->backbone drift.
    decisions.write_minted_membership(result.backbone_new)
    result.counts["suspicious_names_skipped"] = len(result.suspicious_names)
    log.info("curation written", extra={"extra_fields": result.counts})
