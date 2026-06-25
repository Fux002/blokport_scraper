"""Orchestrator (section 12 run.py).

Config driven, not interactive (section 13A.7). Runs the stages in order for one
source and exits non-zero on a FAILED health gate or an unhandled error so a
scheduler can alert. This file grows one stage per milestone. At M5 it runs the
spine Stage 0 to Stage 5 (health, ingest, keys/dedup, normalize, variation,
reconcile) and writes the canonical parquet, the tree-gap queue, the health
report, and the manifest. Stages 6 to 10 (derivation, images, emit) land in the
later milestones.
"""

from __future__ import annotations

import csv
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

from stone_pipeline import adapters as adapter_registry  # adapter_registry.REGISTRY (auto-discovered)
from stone_pipeline.adapters.base import AdapterBase, read_scrape_csv
from stone_pipeline.config import contracts
from stone_pipeline.config.settings import COMPANY_ID, IS_PRODUCTION, SALES_CHANNEL_ID, SETTINGS
from stone_pipeline.config.sources import load_source
from stone_pipeline.core import ids as ids_mod
from stone_pipeline.core import logfmt
from stone_pipeline.core.layout import RunLayout, write_steps_md
from stone_pipeline.core.manifest import Manifest, StageMetric, content_hash
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.io import staging
from stone_pipeline.reference import loaders
from stone_pipeline.reference.fingerprint import check_fingerprint
from stone_pipeline.state.writeback import WriteBack
from stone_pipeline.stages import (
    constants,
    derive,
    emit,
    format_resolve,
    health,
    images,
    keys_dedupe,
    match_variation,
    normalize,
    product_state,
    reconcile_tree,
    validate,
)

log = logfmt.get_logger("run")


def make_run_id(source: str, scrape_ts: str) -> str:
    token = scrape_ts.replace(":", "").replace("-", "").replace(" ", "_")
    return f"{source}_{token}"


def _scrape_ts(frame) -> str:
    if "scrape_timestamp" in frame.columns and frame.height:
        value = frame.get_column("scrape_timestamp").drop_nulls().head(1).to_list()
        if value:
            return str(value[0])
    return "00000000_000000"


def _gap_rows(rows: list[CanonicalRow]) -> list[dict]:
    """De-duplicate gaps across the batch so the human sees one row per missing
    tree entity, not one per product (section 8.2)."""
    seen: dict[tuple, dict] = {}
    for row in rows:
        for gap in row.tree_gaps:
            key = (gap.gap_kind, gap.normalized_name, gap.suggested_color, gap.suggested_finish)
            if key not in seen:
                seen[key] = gap.model_dump(mode="json")
    return list(seen.values())


def _write_gap_queue(rows: list[CanonicalRow], path: Path) -> int:
    gaps = _gap_rows(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "src_site", "surrogate_key", "raw_name", "normalized_name", "suggested_type",
        "suggested_color", "suggested_finish", "suggested_quality", "gap_kind",
        "nearest_existing", "nearest_score", "example_src_url",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for gap in gaps:
            writer.writerow({c: gap.get(c, "") for c in columns})
    return len(gaps)


def find_scrape_file(source: str) -> Optional[Path]:
    """The latest live scrape for a source at data/<source>/<timestamp>/products.csv
    (written by the scrapers). Returns None if the source has never been scraped."""
    live = sorted(SETTINGS.paths.data_dir.glob(f"{source}/*/products.csv"))
    return live[-1] if live else None


def run_source(
    source: str, scrape_path: Optional[Path] = None, outputs_dir: Optional[Path] = None,
    state_dir: Optional[Path] = None, inventory_only: bool = False,
) -> Manifest:
    # inventory_only: a lightweight stock refresh for EXISTING products. Runs the same scrape ->
    # match -> classify path, then emits ONLY inventory_update.csv (Variant Sku + Inventory
    # Quantity). Skips the image stage and the product-import/canonical writes entirely, so it
    # never re-imports products or regenerates images. See stone_pipeline.inventory.
    adapter: AdapterBase = adapter_registry.REGISTRY.get(source)
    if adapter is None:
        raise SystemExit(
            f"source '{source}' has no adapter. Add stone_pipeline/adapters/{source}.py ending "
            f"in `ADAPTER = ...` (auto-registered). Adapters available: {sorted(adapter_registry.REGISTRY)}"
        )
    outputs_dir = Path(outputs_dir or SETTINGS.paths.outputs_dir)
    # write-back persists to state (production); tests pass a temp state_dir so a
    # run never pollutes the repo's learned aliases
    state_dir = Path(state_dir or SETTINGS.paths.state_dir)
    writeback_path = state_dir / "alias_writeback.csv"

    # INGEST is pluggable: a non-file source (API/DB/feed) overrides adapter.load_frame to hand back
    # its own frame; everything below is identical. The default is the scraper CSV.
    custom = adapter.load_frame(scrape_path)
    if custom is not None:
        frame, ts_token, origin = custom
    else:
        scrape_path = scrape_path or find_scrape_file(source)
        if scrape_path is None:
            raise FileNotFoundError(f"no scrape file found for source {source}")
        frame = read_scrape_csv(scrape_path)
        ts_token, origin = _scrape_ts(frame), scrape_path.name
    run_id = make_run_id(source, ts_token)
    layout = RunLayout.for_run(outputs_dir, run_id).ensure()
    run_log = logfmt.bind(log, run_id=run_id, source=source)
    run_log.info(f"run start on {origin} ({frame.height} rows)")

    ref = loaders.load_all()
    check_fingerprint(ref)                          # warns/raises on a reference drift; return unused

    manifest = Manifest(
        run_id=run_id,
        source=source,
        code_version=SETTINGS.code_version,
        environment=SETTINGS.environment,
        input_path=origin,
        input_hash=content_hash(scrape_path) if scrape_path else "",
        reference_versions=ref.versions,
    )
    manifest.totals["backend_fingerprint_ok"] = 1

    # Stage 0: health
    contract = contracts.load_contract(source) or adapter.generate_contract(frame)
    baseline = contracts.load_baselines().get(source)
    report = health.run_health(frame, contract, baseline, smoke_test=adapter.smoke_count)
    report.write(layout.diagnostics / "health.json")
    manifest.health_status = report.status
    if report.status == health.FAILED:
        run_log.error("health FAILED; aborting before emit")
        manifest.write(layout.diagnostics / "manifest.json")
        write_steps_md(layout, source=source, run_id=run_id, health=report.status, counts={})
        raise SystemExit(2)
    health.update_baseline_if_ok(report, contract, health.now_iso())
    degraded = report.status == health.DEGRADED

    # Stage 1: ingest
    rows = adapter.adapt(frame)
    for row in rows:
        row.degraded = degraded
    manifest.add_stage(StageMetric(stage="ingest", rows_in=frame.height, rows_out=len(rows)))

    # Stage 2: keys and dedup
    dd = keys_dedupe.run(rows)
    rows = dd.rows
    manifest.add_stage(StageMetric(stage="keys_dedupe", rows_in=len(dd.rows) + dd.dropped_exact,
                                   rows_out=len(rows), extra={"minted": dd.minted, "near_dup": dd.near_duplicates}))

    # Format resolution: decide block / slab / tile before variation matching,
    # because the branch selects the variants file (section 7 Stage 4)
    fmt_stats = format_resolve.run(rows, ref)
    manifest.add_stage(StageMetric(stage="format", rows_in=len(rows), rows_out=len(rows),
                                   extra={"by_value": fmt_stats.by_value, "unresolved": fmt_stats.unresolved}))

    # Stage 3: normalize
    normalize.run(rows, ref)
    # Stage 4: variation (collects alias write-back for persistence at run end)
    writeback = WriteBack()
    match_variation.run(rows, ref, writeback=writeback, writeback_path=writeback_path,
                        generic_descriptor=adapter.generic_descriptor)
    # Stage 5: reconcile tree
    stats = reconcile_tree.run(rows, ref)
    manifest.add_stage(StageMetric(stage="reconcile", rows_in=len(rows), rows_out=len(rows),
                                   gapped=stats.missing_variation + stats.missing_leaf,
                                   extra={"validated": stats.validated, "snapped": stats.snapped}))

    # Stage 6: derivation
    source_cfg = load_source(source)
    derive.run(rows, ref, source_cfg)
    # Stage 7: images  (skipped for an inventory-only refresh — stock never touches images)
    img_stats = images.ImageStats() if inventory_only else images.run(rows)
    # Stage 8: constants
    constants.run(rows, source_cfg)

    # row fingerprint: hash of the inputs that determine the output (section 11.2),
    # the basis for incremental runs (section 13A.5) and drift checks
    for row in rows:
        row.row_fingerprint = ids_mod.row_fingerprint([
            row.surrogate_key, row.variation_id, row.color_id, row.finish_id,
            row.quality_id, row.type_id, row.title, row.bundle_size, "|".join(row.image_keys),
        ])
    # Stage 9: validation
    validation = validate.run(rows, emit_on_review=source_cfg.emit_on_review,
                              require_images=False if inventory_only else SETTINGS.images.require_images)
    manifest.add_stage(StageMetric(stage="validate", rows_in=len(rows), rows_out=len(validation.emit),
                                   rejected=len(validation.rejects), reviewed=len(validation.review_only),
                                   extra={"images_staged": img_stats.staged}))

    # metrics
    manifest.match_method_distribution = dict(Counter(r.variation_method for r in rows))
    manifest.review_code_counts = dict(Counter(str(f.code) for r in rows for f in r.review_flags))
    manifest.gap_kind_counts = dict(Counter(str(g.gap_kind) for r in rows for g in r.tree_gaps))
    manifest.totals.update(
        rows=len(rows),
        variation_resolved=sum(1 for r in rows if r.variation_id),
        validated=stats.validated,
        emitted=len(validation.emit),
        rejected=len(validation.rejects),
    )

    # Item 4: tag emitted products new vs existing (by SKU) against the Medusa
    # product export, and flag inventory changes (feeds item 5).
    known = product_state.load_known_products()
    pstats = product_state.classify(validation.emit, source_cfg, known)
    manifest.totals.update(products_new=pstats.new, products_existing=pstats.existing,
                           inventory_changed=pstats.inventory_changed)

    # Stage 10: emit into the clean per-run layout (outputs/<run_id>/...)
    existing_rows = [r for r in validation.emit if r.product_status == "existing"]
    # item 5: the inventory-only delta — existing products whose supplier stock moved. Shared by
    # both modes: it is the SOLE output of an inventory refresh and one output of a full run.
    changed = [r for r in existing_rows if r.product_changed] if known else []
    # delete loop: products in Medusa (this source) that the latest scrape dropped -> stock-0 delist + report
    discontinued = product_state.discontinued(rows, source_cfg, known)
    # SAFETY: a partial scrape (truncated Cloudflare page, transient block) passes the DEGRADED
    # health gate yet would delist every product it failed to fetch. Refuse to mass-delist: if a
    # single run would discontinue >30% of this source's known products, drop the delist and flag
    # it loudly -- almost certainly an incomplete scrape, not a real bulk discontinuation.
    _src_prefix = f"{source_cfg.source_code}-".upper()
    _src_known = sum(1 for sku in known.by_sku if sku.startswith(_src_prefix)) if known else 0
    if _src_known and len(discontinued) > 0.30 * _src_known:
        run_log.warning("delist refused: a single run would discontinue too much of the catalog "
                        "(likely a partial scrape) -- keeping products listed",
                        extra={"extra_fields": {"would_delist": len(discontinued), "source_known": _src_known,
                                                "fraction": round(len(discontinued) / _src_known, 3)}})
        discontinued = []
    if known:  # always (re)write so a stale delta from a prior run (same scrape -> same folder) can't linger
        emit.write_inventory_csv(changed, source_cfg, layout.products_import / "inventory_update.csv",
                                 discontinued=tuple(discontinued))
        emit.write_discontinued_csv(discontinued, layout.review / "products_discontinued.csv")
    manifest.totals["products_discontinued"] = len(discontinued)
    gap_count = 0
    # rejects are an audit invariant: write them in BOTH modes so a rejected row never vanishes
    # without a per-row record (inventory_only still runs validate above).
    emit.write_rejects_csv(validation.rejects, layout.review / "products_rejects.csv")
    if inventory_only:
        run_log.info("inventory-only: wrote stock delta + rejects, skipped products/images/canonical",
                     extra={"extra_fields": {"existing": len(existing_rows), "changed": len(changed),
                                             "rejected": len(validation.rejects)}})
    else:
        columns = emit.read_template_columns()
        emit.write_import_csv(validation.emit, source_cfg, layout.products_import / "medusa_import.csv", columns)
        new_rows = [r for r in validation.emit if r.product_status == "new"]
        if known:  # only split when we know what exists
            emit.write_import_csv(new_rows, source_cfg, layout.products_import / "medusa_import_new.csv", columns)
            emit.write_import_csv(existing_rows, source_cfg, layout.products_import / "medusa_import_existing.csv", columns)
        emit.write_review_csv(validation.review_only + validation.emit, layout.review / "products_review.csv")
        staging.write_canonical(rows, layout.diagnostics / "canonical.parquet")
        gap_count = _write_gap_queue(rows, layout.review / "tree_gaps.csv")
    # NOTE: variants/backbone/tree/images are SHARED across sources and are
    # consolidated by `python -m stone_pipeline.catalog` (reads every run's
    # canonical.parquet), not written per source. This folder is products only.
    written = writeback.flush(writeback_path)
    if written:
        manifest.write_backs.append(f"alias_writeback:{written}")
    manifest.write(layout.diagnostics / "manifest.json")
    report.write(layout.diagnostics / "health.json")
    # the per-source product checklist
    write_steps_md(layout, source=source, run_id=run_id, health=report.status,
                   counts=manifest.totals)

    run_log.info(
        "run done",
        extra={"extra_fields": {
            "rows": len(rows),
            "variation_resolved": manifest.totals["variation_resolved"],
            "emitted": len(validation.emit),
            "rejected": len(validation.rejects),
            "distinct_gaps": gap_count,
            "health": report.status,
        }},
    )
    return manifest


def print_summary(manifest: Manifest) -> None:
    """One-screen run summary (section 13A.4)."""
    t = manifest.totals
    lines = [
        "",
        f"  source            {manifest.source}",
        f"  health            {manifest.health_status}",
        f"  rows in           {t.get('rows', 0)}",
        f"  emitted           {t.get('emitted', 0)}",
        f"  rejected          {t.get('rejected', 0)}",
        f"  variation matched {t.get('variation_resolved', 0)}",
        f"  distinct gaps     {sum(manifest.gap_kind_counts.values())}",
        f"  match methods     {manifest.match_method_distribution}",
        f"  review codes      {manifest.review_code_counts}",
        f"  write-backs       {manifest.write_backs}",
        "",
    ]
    print("\n".join(lines))


def run_all(sources: Optional[list[str]] = None, outputs_dir: Optional[Path] = None,
            state_dir: Optional[Path] = None, inventory_only: bool = False) -> dict[str, Manifest]:
    """Multi-source run with source-level isolation (section 13A.3): one source
    failing does not affect the others."""
    sources = sources or list(adapter_registry.REGISTRY.keys())
    results: dict[str, Manifest] = {}
    for source in sources:
        try:
            results[source] = run_source(source, outputs_dir=outputs_dir, state_dir=state_dir,
                                         inventory_only=inventory_only)
        except SystemExit:
            log.error(f"{source} aborted on health gate; continuing other sources")
        except Exception:
            log.exception(f"{source} failed; continuing other sources")
    return results


def main(argv: Optional[list[str]] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    target = argv[0] if argv else SETTINGS.spine_source
    # Fail fast in production if the owner ids are unset: settings returns "" (never a dev id) and
    # nothing downstream rejects a blank company/sales-channel, so without this guard a prod run
    # would emit a "valid" import of unowned, channel-less (invisible) products.
    if IS_PRODUCTION and not (SALES_CHANNEL_ID and COMPANY_ID):
        log.error("production run requires BLOKPORT_SALES_CHANNEL_ID and BLOKPORT_COMPANY_ID "
                  "(refusing to emit unowned, channel-less products)")
        return 1
    try:
        if target == "all":
            # only sources that actually HAVE a scrape this run are expected to produce output; a
            # registered adapter with no scrape file is absent, not failed (don't alert on it).
            requested = [s for s in adapter_registry.REGISTRY if find_scrape_file(s)]
            results = run_all(requested)
            for manifest in results.values():
                print_summary(manifest)
            # exit non-zero if a source that HAD data failed to produce output (run_all isolates a
            # failing source by omitting it from results) -- so a scheduler is alerted, not misled.
            missing = [s for s in requested if s not in results]
            if missing:
                log.error("run all: source(s) with data failed to produce output",
                          extra={"extra_fields": {"failed": missing}})
                return 1
            return 0
        manifest = run_source(target)
        print_summary(manifest)
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception:
        log.exception("run failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
