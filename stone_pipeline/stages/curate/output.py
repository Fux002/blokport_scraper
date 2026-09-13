"""The curation outputs of one produce: the Medusa import DELTA, the backbone additions, and the operator
queues (variety cards, origin cards, leaf suggestions). The full upload file is emit_catalog's."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core import csvio, logfmt
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.matching import projections as proj
from stone_pipeline.stages.curate.context import CurationResult
from stone_pipeline.stages.curate.keys import BRANCHES

log = logfmt.get_logger("curate")

_IMPORT_COLS = ["Key", "Name", "Image", "Aliases", "Volume per kg (m³/kg)"]


def _write_csv(path: Path, cols: list[str], rows: list[dict], sanitize: bool = True) -> None:
    # atomic always. sanitize=True (formula-injection-safe) for operator-opened files; sanitize=False
    # for a MEDUSA IMPORT file, where a leading "'" prepended to a Name/Alias would corrupt the data.
    csvio.write_dicts(path, cols, rows, sanitize=sanitize)


def write_curation(result: CurationResult, rows: list[CanonicalRow]) -> None:
    """Write the catalog curation outputs to the fixed top-level folders:
      to_upload/1_variants_update.csv      the new + alias-update DELTA (incremental upload)
      catalog_source/backbone_additions/   new varieties to append to the backbones
      review/                              the human-decision aids (never uploaded)
    The full upload file (to_upload/1_variants_full.csv) is produced by emit_catalog."""
    p = SETTINGS.paths
    to_upload = p.to_upload_dir
    additions = p.catalog_source_dir / "backbone_additions"
    upload_rows: list[dict] = []
    needs_review: list[dict] = []
    triage: list[dict] = []
    for branch in BRANCHES:
        confirmed = [r for r in result.alias_additions[branch] if r.get("_status") == "confirmed"]
        upload_rows += confirmed + result.new_variants[branch]
        needs_review += [r for r in result.alias_additions[branch] if r.get("_status") != "confirmed"]
        triage += result.new_variants[branch]
        # backbone deltas: the new varieties to append to catalog_source/backbone_*.json. ALWAYS
        # (over)write -- including an empty list -- so a variety that is no longer minted this run
        # (resolved, held, or now code-detected like 'Mgt Onyx') can never PERSIST as a stale
        # addition from a prior run (which would keep feeding the image queue + tree).
        bp = additions / f"{branch}.json"
        bp.parent.mkdir(parents=True, exist_ok=True)
        bp.write_text(json.dumps(result.backbone_new[branch], indent=2, ensure_ascii=False),
                      encoding="utf-8")
    # Drop no-op rows: a variant whose Key is ALREADY in the export and that adds no new alias and
    # no new image is just upsert noise (the mint guard can miss a duplicate-name generic). Keep the
    # update file to the genuine delta -- new variants + real alias/image changes only.
    exp_idx: dict[str, tuple[set, str]] = {}
    ef = SETTINGS.paths.export_file
    if ef.exists():
        with ef.open(encoding="utf-8-sig") as h:
            for r in csv.DictReader(h):
                if (r.get("Key") or "").strip():
                    exp_idx[r["Key"]] = ({proj.norm(a) for a in (r.get("Aliases") or "").split("|") if a.strip()},
                                         (r.get("Image") or "").strip())

    def _is_real_delta(row: dict) -> bool:
        e = exp_idx.get(row["Key"])
        if e is None:
            return True                                    # genuinely new variant (key not in export)
        cur = {proj.norm(a) for a in (row.get("Aliases") or "").split("|") if a.strip()}
        img = (row.get("Image") or "").strip()
        return bool(cur - e[0]) or (bool(img) and img != e[1])   # new alias OR new image

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
    if upload_rows:
        _write_csv(to_upload / "1_variants_update.csv", _IMPORT_COLS, upload_rows, sanitize=False)  # Medusa import
    # the ONLY review file from curate: the decision ledger (uncertain new varieties, confirm
    # true/false). Always (re)written so applied/rejected rows drop out. Advisory sets (uncertain
    # alias spellings, code-like names, images) are logged as counts, not clutter files.
    from stone_pipeline.stages import decisions
    decisions.write_confirm_file(result.pending_confirm)
    # SEPARATE origin queue: rows held because a vendor's primary_origin was not corroborated by the map
    # (origin_needs_confirmation). One entry per (source, variety, type); never mixed into the variety queue
    # above. Always called (empty clears it) so a confirmed origin drops off, like the variety queue.
    decisions.write_origin_confirm_file(rows)
    if result.backbone_updates:   # human-readable audit of the leaf additions (the queue below is the surface)
        _write_csv(additions / "backbone_value_updates.csv", decisions.LEAF_COLUMNS, result.backbone_updates)
    # surface the leaf additions for operator review (:4200) -> approve grows the backbone overlay next run.
    # Always called (empty clears the queue) so a resolved suggestion drops off, like the variety queue.
    decisions.write_backbone_leaf_pending(result.backbone_updates)
    result.counts["uncertain_aliases"] = len(needs_review)
    result.counts["suspicious_names_skipped"] = len(result.suspicious_names)
    log.info("curation written", extra={"extra_fields": result.counts})
