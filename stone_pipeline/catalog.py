"""Catalog sync CLI: consolidate the new variants, aliases, backbone entries, and
images from EVERY source run into one shared catalog folder, with a sync
checklist. The catalog (variants/backbone/tree/images) is shared across sources;
products are per-source. Run this after scraping, before importing products.

    python -m stone_pipeline.build        # scrape + catalog + combinations + consistency gate
    # (or the stages individually: `run all` then `catalog` -- catalog now builds the combinations
    #  itself and runs the consistency gate, so `tree` is no longer a separate manual step.)
    # then follow to_upload/SYNC_STEPS.md

It reads every run's diagnostics/canonical.parquet, so it reflects all sources at
once and de-duplicates a variant that several suppliers carry.
"""

from __future__ import annotations

import csv
import json
import shutil
import sys
from pathlib import Path

from stone_pipeline import prepare_variant_images
from stone_pipeline.aliases.cleanup import FILE_NAME as ALIAS_CLASSIFICATION_FILE
from stone_pipeline.config.settings import SETTINGS, VARIANTS_EXPORT_STATUS_REQUIRED
from stone_pipeline.config.standardise_settings import STANDARDISE
from stone_pipeline.adapters.tokens import explicit_type_word
from stone_pipeline.core import csvio, logfmt
from stone_pipeline.core.layout import write_sync_md
from stone_pipeline.core.text import looks_code_shaped
from stone_pipeline.io import staging
from stone_pipeline.io import storage
from stone_pipeline.reference import loaders
from stone_pipeline.reference.loaders import field_kinds
from stone_pipeline.reference.medusa_files import ExportVariation, read_variants_export
from stone_pipeline.stages import curate, emit, emit_catalog, tree_build
from stone_pipeline.standardise import index

log = logfmt.get_logger("catalog")


def _is_complete_run(run_dir: Path) -> bool:
    """A run is COMPLETE only once it has emitted its canonical.parquet. That file is written solely on a
    successful emit (run.py); every abort path (scrape-floor / health / gate / ingest) writes diagnostics
    but NO parquet. So the parquet is the authoritative completeness marker at the catalog layer."""
    return (run_dir / "diagnostics" / "canonical.parquet").exists()


def latest_run_dirs(outputs_root: Path) -> list[Path]:
    """The newest COMPLETE run folder per source under outputs_root. A run id is `<source>_<date>_<time>`,
    so the lexically-greatest folder is the newest scrape -- but an ABORTED newer run (which writes
    diagnostics but no canonical.parquet) must NOT supersede the last good run: consolidation would drop
    the source entirely and prune/clean would delete its last recoverable snapshot in favour of an empty
    newer folder (a source silently vanishing from the catalog + its data destroyed). So completeness, not
    lexical recency alone, picks the authoritative folder per source. Older + aborted folders are stale.
    This keeps the catalog idempotent and leftover-proof AND crash/abort-safe."""
    latest: dict[str, Path] = {}
    for d in sorted(Path(outputs_root).glob("*_*_*")):   # <source>_<YYYYMMDD>_<HHMMSS>, sorted asc
        if d.is_dir() and _is_complete_run(d):
            latest[d.name.rsplit("_", 2)[0]] = d          # newest COMPLETE wins
    return list(latest.values())


def find_canonical(outputs_root: Path) -> list[Path]:
    return [d / "diagnostics" / "canonical.parquet" for d in latest_run_dirs(outputs_root)]


# The consistency gate's OWN exit code. produce reconciles a failure to exit 0 only when it carries this code
# AND the ledger explains it (new varieties held for their first pull); any other non-zero rc (a write-through
# abort, a scrape floor, a stall) stays fatal no matter what the consistency errors look like.
CONSISTENCY_GATE_RC = 4


def _consistency_gate() -> None:
    """Deterministic consistency gate: abort with CONSISTENCY_GATE_RC when the upload set is internally
    inconsistent (stale/out-of-order combinations or products vs the current export). Warnings never abort."""
    errors, warnings = verify_consistency()
    for w in warnings:
        log.warning("consistency warning", extra={"extra_fields": {"warning": w}})
    if errors:
        for e in errors:
            log.error("consistency gate FAILED", extra={"extra_fields": {"error": e}})
        raise SystemExit(CONSISTENCY_GATE_RC)


def _record_catalog() -> None:
    """Reflect the produced variations onto the ledger (flag-gated). The ledger is what Medusa pulls, so a
    failed write-through is a failed produce: exit non-zero rather than report a clean success whose
    variations will never sync."""
    from stone_pipeline.ledger import writethrough
    if writethrough.enabled() and not writethrough.record_catalog():
        raise SystemExit("ledger catalog write-through FAILED: the produced variations are NOT in the ledger "
                         "and will not sync to Medusa; re-run the produce")


def run(outputs_root: Path | None = None) -> Path:
    field_kinds.require_present()   # every name is written by the map; fail loud up front, before any row is touched
    outputs_root = Path(outputs_root or SETTINGS.paths.outputs_dir)
    parquets = find_canonical(outputs_root)
    if not parquets:
        raise SystemExit(f"no source runs found under {outputs_root} (run `python -m stone_pipeline.run all` first)")

    rows = []
    sources: set[str] = set()
    for p in parquets:
        src_rows = staging.read_canonical(p)
        rows.extend(src_rows)
        sources.update(r.src_site for r in src_rows)   # union ALL src_sites, not just the first row's

    ref = loaders.load_all()
    result = curate.run(rows, ref)                # -> outputs/<env>/curate_delta.csv (emit publishes the update file), review/, backbone_additions/
    # HEAL base<->backbone drift: a variety minted in a prior run dropped out of the per-run additions file
    # (which must stay per-run for the texture queue), so its products flag no_backbone_record and skip on
    # color_id with nothing reviewable. Persist those varieties' membership durably (config.db overlay,
    # statement colour first) so the NEXT produce resolves them. New this run are already covered by curate.
    from stone_pipeline.stages import decisions as _decisions
    _decisions.heal_drifted_membership(rows)
    products = collect_products(outputs_root)     # -> to_upload/3_products_*.csv (per source + combined)
    emit_catalog.build()                          # -> to_upload/1_variants_full.csv -- AFTER products, so its
                                                  #    product-backed image stamping reads THIS run's products
                                                  #    (their variation ids), never a stale prior set
    aliases_dropped = emit_catalog.alias_collisions_dropped()   # -> review list; the summary states the count
    inventory = collect_inventory(outputs_root)   # -> to_upload/4_inventory_update.csv (existing stock only)
    discontinued = collect_discontinued(outputs_root)  # -> review/4 delete-loop report (stock-0'd in inventory)
    collect_lot_cards(outputs_root)                    # -> the "which slabs are sold?" review queue
    sync = write_sync_md(counts=result.counts, sources=sorted(set(sources)),
                         products=products, inventory=inventory, discontinued=discontinued,
                         aliases_dropped=aliases_dropped)
    images_queued = prepare_variant_images.queue_new_textures()   # new variants -> the texture job (auto)
    # Give each colour-less variety a real colour read from its REAL PRODUCT IMAGE (the hosted
    # product photo, keyed by variation_key), then propagate to mirrors, so a no-colour source like
    # varsha/zucchi never nulls colour_id; 'Natural' only if no photo. NOT the generated variant icon --
    # an icon can be a stale/placeholder render that diverges from the actual stone.
    from stone_pipeline.stages import variety_color
    _additions = [SETTINGS.paths.backbone_additions_file]   # this run's new varieties, coloured in place
    _merged = [SETTINGS.paths.backbone_json]                # read-only: a new variety inherits a known colour
    product_images: dict[str, str] = {}                # variation_key -> its product photo (first wins)
    for r in rows:
        if r.variation_key and r.image_keys and r.variation_key not in product_images:
            product_images[r.variation_key] = r.image_keys[0]
    color_stats = variety_color.fill_colors(backbone_paths=_additions, reference_paths=_merged,
                                            product_images=product_images)
    write_types_outside_pack()                     # the types left out of the pack's vocabulary, listed
    to_delete = write_variants_to_delete()         # surface junk variants (bare-code + mis-typed) for deletion
    migrated = migrate_retyped_variant_images(ref) # a re-typed variant keeps its image at its new Key
    dropped = drop_deleted_variants()              # drop to-delete junk; imageless NEW records are KEPT now
    # ONE list: the committed seed of truth is exactly the freshly-gated full upload (base == full).
    from stone_pipeline.reference import sync_variants_base
    sync_variants_base.sync()
    sync_variants_base.write_deliverable()        # -> to_upload/variants_export_base.csv: the base just settled
    pruned = prune_superseded_runs(outputs_root)  # leave only the latest run folder per source
    # Build the valid combinations HERE, in the same step as the variants/products, so they are
    # always derived from the SAME export. They can never go stale by someone forgetting to run
    # `tree` separately after the export changed (which silently shipped stale combinations before).
    tree_build.run()                              # -> to_upload/2_valid_combinations.csv
    log.info("catalog consolidated", extra={"extra_fields": {
        "sources": sorted(set(sources)), **result.counts,
        "products": sum(products.values()), "inventory": inventory,
        "discontinued": discontinued, "aliases_dropped": aliases_dropped, "pruned_stale_runs": pruned,
        "images_queued": images_queued, "variant_colors": color_stats,
        "variants_dropped_deleted": dropped, "variants_to_delete": to_delete,
        "retyped_images_migrated": migrated}})
    # Reflect the produced 1_variants_full onto the ledger variation table (flag-gated). This runs
    # BEFORE the consistency gate on purpose: in the pull model the ledger is the source of truth Medusa
    # pulls, and a produce that mints new varieties trips the CSV gate ("not in the current export")
    # even though those variations are valid and simply awaiting their first pull. The ledger must carry
    # them regardless; the sync engine gates their products on the variation being synced, and produce
    # reconciles the gate against the ledger (held vs fatal). Inert unless write-through is enabled.
    _record_catalog()

    # Deterministic consistency gate: fail loudly if the upload set is internally inconsistent
    # (stale/out-of-order combinations or products vs the current export) -- no manual/AI check. In the
    # pull model produce reconciles this against the ledger: new-variety failures are the expected
    # two-pass checkpoint (held), a genuine structural fault stays fatal.
    _consistency_gate()

    return sync


def _s3_object_copier():
    """Return a callable (source object key, target object key) -> bool that copies one object within the env's
    staging bucket, or None when S3 is unreachable. A re-typed variety is the SAME stone, so its existing icons are
    copied to the new Key rather than regenerated."""
    from stone_pipeline.config.settings import SETTINGS as _S
    s3 = _S.s3
    try:
        client = storage.aws_client("s3", config=storage.BOUNDED, profile=s3.credentials_profile)
    except Exception:
        return None

    def copy(source: str, target: str) -> bool:
        try:
            client.copy_object(Bucket=s3.bucket, Key=target, CopySource={"Bucket": s3.bucket, "Key": source})
            return True
        except Exception:
            return False
    return copy


def copy_variety_icons(old: str, new: str, existing: dict[str, dict[str, str]], copier) -> int:
    """Copy each category icon of `old` that `new` does not have yet (`existing`: the S3 listing, {Key: {category
    name: ETag}}), pair by pair through io.variety_icons. Returns the number copied."""
    from stone_pipeline.io import variety_icons
    have = existing.get(new, {})
    return sum(1 for name in sorted(existing.get(old, {})) if name not in have
               and copier(variety_icons.icon_key(old, name), variety_icons.icon_key(new, name)))


def migrate_retyped_variant_images(ref, existing=None, copier=None) -> int:
    """When a variety is re-typed (its mis-typed Key is flagged in variants_to_delete and its NAME resolves to a
    different, correct type), copy its S3 icons, each category's, from the old Key to the new correct Key, so the
    re-typed variety keeps its icons instead of being held imageless out of the upload. Idempotent (a pair the new
    Key already has is skipped); no-op when S3 is unreachable, so the local sandbox is unchanged and AWS does it
    in-build."""
    dele = SETTINGS.paths.review_dir / "variants_to_delete.csv"
    if not dele.exists():
        return 0
    if existing is None:
        from stone_pipeline.stages.emit_catalog import _s3_icon_pairs
        existing = _s3_icon_pairs()
    copier = copier if copier is not None else _s3_object_copier()
    if existing is None or copier is None:
        return 0
    from stone_pipeline.adapters.tokens import explicit_type_word
    from stone_pipeline.phase1.variety_key import new_key
    copied = 0
    for r in csv.DictReader(dele.open(encoding="utf-8-sig")):
        old = (r.get("Key") or "").strip()
        name = (r.get("Name") or "").strip()
        if not old or not name:
            continue
        looked = ref.attributes.resolve_id("type", explicit_type_word(name) or name)
        if not looked:
            continue                                   # can't derive the correct type from the name
        new = new_key(looked[0], name)
        if new != old:
            copied += copy_variety_icons(old, new, existing, copier)
    if copied:
        log.info("migrated re-typed variant images to their new S3 Keys",
                 extra={"extra_fields": {"copied": copied}})
    return copied


def drop_deleted_variants(to_upload: Path | None = None, delete_file: Path | None = None) -> int:
    """Drop variants flagged in variants_to_delete from BOTH upload files (1_variants_update +
    1_variants_full), so the full/seed file never re-imports junk on its way out. Returns the count dropped.

    A new variant is NO LONGER held for a missing image. Every variety is prepared in Medusa for ALL
    categories: a fan-out record carries a BLANK Image (emit stamps {Key}.png only when the object exists,
    so no 404) until its texture is generated. A texture is generated per-category only when a PRODUCT for
    that category appears, and a product only LISTS once its texture is on S3 (the sync's H2 product gate).
    So the catalog gates PRODUCTS on the image, never variant RECORDS -- every variety is a consistent full
    set of category records, imaged per-category with its products. (Blokport placeholders an imageless
    category record.) The old image-hold made minted varieties inconsistently category-partial."""
    to_upload = Path(to_upload or SETTINGS.paths.to_upload_dir)
    upd = to_upload / "1_variants_update.csv"
    if not upd.exists():
        return 0
    dele = Path(delete_file or SETTINGS.paths.review_dir / "variants_to_delete.csv")
    drop = ({(r.get("Key") or "").strip() for r in csv.DictReader(dele.open(encoding="utf-8-sig"))
             if (r.get("Key") or "").strip()} if dele.exists() else set())
    if not drop:
        return 0
    for fname in ("1_variants_update.csv", "1_variants_full.csv"):
        p = to_upload / fname
        if not p.exists():
            continue
        with p.open(encoding="utf-8-sig", newline="") as h:
            rows = list(csv.reader(h))
        if not rows:
            continue                                   # truncated/empty -> skip, never crash the run
        header, body = rows[0], [r for r in rows[1:] if r and r[0] not in drop]
        csvio.atomic_write(p, lambda h, _rows=[header, *body]: csv.writer(h).writerows(_rows))
    log.info("excluded to-delete variants from upload files", extra={"extra_fields": {"excluded": len(drop)}})
    return len(drop)


def prune_superseded_runs(outputs_root: Path) -> int:
    """Remove run folders that a newer run for the SAME source has superseded, so outputs/ keeps
    exactly one (current) snapshot per source and stale data can't accumulate. The latest run per
    source is always kept; the dedicated _inventory/ tree (no <source>_<date>_<time> shape at this
    level) is untouched. Returns the number of folders removed."""
    keep = {d.resolve() for d in latest_run_dirs(outputs_root)}
    removed = 0
    for d in Path(outputs_root).glob("*_*_*"):
        if d.is_dir() and d.resolve() not in keep:
            shutil.rmtree(d)
            removed += 1
    return removed


def collect_products(outputs_root: Path) -> dict[str, int]:
    """Gather each source's product import CSV into to_upload/: one file per source
    (3_products_<source>.csv) plus a combined all-sources file (3_products_all.csv),
    same Medusa import schema. So the products are next to the variants and tree."""
    to_upload = SETTINGS.paths.to_upload_dir
    to_upload.mkdir(parents=True, exist_ok=True)
    # prune stale per-source files first so a source that no longer runs cannot leave a
    # 3_products_<gone>.csv lingering in the upload set (3_products_all is rewritten below).
    for old in to_upload.glob("3_products_*.csv"):
        old.unlink()
    (to_upload / STANDARDISE.PHOTOS_FILE).unlink(missing_ok=True)
    # the alias classification describes the ledger at the moment it was written (aliases.cleanup): a produce moves
    # the ledger on, so the next publish must not carry it (the backend refuses a missing file, never a stale one)
    (to_upload / ALIAS_CLASSIFICATION_FILE).unlink(missing_ok=True)
    (to_upload / STANDARDISE.DEFAULTED_FILE).unlink(missing_ok=True)
    header: list[str] | None = None
    all_rows: list[list[str]] = []
    photos: dict[str, list] = {}
    defaulted: set[str] = set()
    without_photos: list[str] = []
    without_defaulted: list[str] = []
    counts: dict[str, int] = {}
    for run_dir in latest_run_dirs(outputs_root):          # newest run per source only
        src_csv = run_dir / "4_products_import" / "medusa_import.csv"
        if not src_csv.exists():
            continue
        source = run_dir.name.rsplit("_", 2)[0]            # <source>_<date>_<time> -> <source>
        with src_csv.open(encoding="utf-8-sig", newline="") as h:
            rows = list(csv.reader(h))
        if not rows:
            continue
        header_row, body = rows[0], rows[1:]
        # every source emits the SAME template header (emit.read_template_columns); refuse to merge
        # if one differs, or the combined 3_products_all.csv would silently mis-align columns.
        if header is None:
            header = header_row
        elif header_row != header:
            raise RuntimeError(
                f"collect_products: source '{source}' product header differs from earlier sources -- "
                "a column-schema mismatch would mis-align 3_products_all.csv; refusing to merge")
        # the photo list beside the sheet (emit.write_photos_json). A run made before it existed (a catalog-only run
        # or a scoped produce over restored outputs, a source whose newer scrape aborted) never blocks the catalog:
        # its source is named loud and left out of the list; the photo process is the gate (it stops on a sheet
        # listing the list does not have) until a produce runs that source again.
        own = src_csv.parent / STANDARDISE.PHOTOS_FILE
        if own.exists():
            for handle, refs in json.loads(own.read_text(encoding="utf-8")).items():
                if photos.setdefault(handle, refs) != refs:
                    raise RuntimeError(f"collect_products: product handle {handle!r} has two different photo lists")
            # Written with the photo list by the same emit (emit.write_photos_json). A run whose photo list predates it
            # (an older producer's) has none: none of its sizes is listed until a produce runs that source again,
            # named loud with the sources missing a photo list. Remover: drop this fallback once every source's latest
            # run is from a producer that writes it (after the first full produce on both envs).
            listed = own.parent / STANDARDISE.DEFAULTED_FILE
            if listed.exists():
                defaulted.update(index.parse_defaulted(listed.read_text(encoding="utf-8")))
            else:
                without_defaulted.append(source)
        else:
            without_photos.append(source)
        counts[source] = len(body)
        # atomic (a half-written upload file would be read by verify_consistency right after); NOT
        # sanitized -- this is the Medusa IMPORT file, where a leading "'" would corrupt the data.
        csvio.write_rows(to_upload / f"3_products_{source}.csv", header_row, body)
        all_rows += body
    if header is not None:
        csvio.write_rows(to_upload / "3_products_all.csv", header, all_rows)
        body_json = json.dumps(photos, sort_keys=True, separators=(",", ":"))
        csvio.atomic_write(to_upload / STANDARDISE.PHOTOS_FILE, lambda handle: handle.write(body_json))
        listed_json = json.dumps(sorted(defaulted), separators=(",", ":"))
        csvio.atomic_write(to_upload / STANDARDISE.DEFAULTED_FILE, lambda handle: handle.write(listed_json))
    if without_defaulted:
        log.warning("defaulted sizes incomplete: these sources' latest runs predate the list, so none of their sizes is "
                    "held as defaulted until a produce runs them again: " + ", ".join(sorted(without_defaulted)),
                    extra={"extra_fields": {"sources_without_defaulted_sizes": sorted(without_defaulted)}})
    if without_photos:
        log.warning("photo list incomplete: these sources' latest runs have none, so the photo process stops until a "
                    "produce runs them again: " + ", ".join(sorted(without_photos)),
                    extra={"extra_fields": {"sources_without_photo_list": sorted(without_photos)}})
    return counts


def consolidate_inventory(inv_csvs: list[Path], to_upload: Path | None = None) -> int:
    """Merge the given per-run inventory_update.csv files into ONE deliverable:
    to_upload/<env>/4_inventory_update.csv. Deduped by Variant Sku (globally unique), last run
    wins -- so re-running against the same Medusa baseline never double-lists a SKU. Medusa's
    INVENTORY importer loads it WITHOUT a product re-import or any image work. Always written
    (header-only when nothing changed) so the deliverable is predictable. Returns SKU count."""
    to_upload = Path(to_upload or SETTINGS.paths.to_upload_dir)
    to_upload.mkdir(parents=True, exist_ok=True)
    by_sku: dict[str, list[str]] = {}
    for src_csv in inv_csvs:
        if not src_csv.exists():
            continue
        with src_csv.open(encoding="utf-8-sig", newline="") as h:
            rows = list(csv.reader(h))
        for r in rows[1:]:          # skip header
            if r and r[0].strip():
                by_sku[r[0].strip().upper()] = r   # Variant Sku is column 0
    # atomic, NOT sanitized -- Medusa import file.
    csvio.write_rows(to_upload / "4_inventory_update.csv", emit.INVENTORY_COLUMNS, list(by_sku.values()))
    return len(by_sku)


def collect_inventory(outputs_root: Path) -> int:
    """Catalog path: consolidate the latest run per source's inventory delta."""
    return consolidate_inventory(
        [d / "4_products_import" / "inventory_update.csv" for d in latest_run_dirs(outputs_root)])


def collect_lot_cards(outputs_root: Path) -> int:
    """Replace the "which slabs are sold?" review queue with the cards of the latest run per source
    (review/lot_cards.json, written by every run). Returns the number of cards."""
    from stone_pipeline.config import decisions_store
    cards: list[dict] = []
    for d in latest_run_dirs(outputs_root):
        p = d / "review" / "lot_cards.json"
        if p.exists():
            cards.extend(json.loads(p.read_text(encoding="utf-8")))
    decisions_store.replace_pending("lot", cards)
    return len(cards)


def collect_discontinued(outputs_root: Path) -> int:
    """Consolidate the latest run per source's delete-loop report into one review file:
    review/<env>/products_discontinued.csv (deduped by SKU). These are products the suppliers no
    longer carry -- already set to stock 0 by the inventory update, listed here for optional delete.
    Always written (header-only when none) so the file is a predictable, auditable deliverable."""
    review = SETTINGS.paths.review_dir
    review.mkdir(parents=True, exist_ok=True)
    by_sku: dict[str, list[str]] = {}
    for d in latest_run_dirs(outputs_root):
        p = d / "review" / "products_discontinued.csv"
        if not p.exists():
            continue
        with p.open(encoding="utf-8-sig", newline="") as h:
            rows = list(csv.reader(h))
        for r in rows[1:]:
            if r and r[0].strip():
                by_sku[r[0].strip().upper()] = r
    # operator-opened review file built from scraped product rows -> sanitize each cell + atomic.
    csvio.write_rows(review / "products_discontinued.csv", emit.DISCONTINUED_COLUMNS,
                     [[csvio.safe_cell(c) for c in r] for r in by_sku.values()])
    return len(by_sku)


def write_types_outside_pack() -> int:
    """List every type the pack's vocabulary leaves out (never guessed in) to review/<env>/types_outside_pack.csv:
    its handles and why (no stone-category combination yet, or only other categories). Always written, header-only
    when none, so a type that has entered the pack drops off. Returns the count."""
    excluded = loaders.types_outside_pack(SETTINGS.paths.attributes_csv)
    out = SETTINGS.paths.review_dir / "types_outside_pack.csv"
    csvio.write_dicts(out, ["type", "category_handles", "reason"], excluded, sanitize=True)
    if excluded:
        log.warning("types left out of the pack's vocabulary (no stone-category combination)", extra={"extra_fields": {
            "count": len(excluded), "file": str(out), "types": [t["type"] for t in excluded]}})
    return len(excluded)


def write_variants_to_delete() -> int:
    """Surface JUNK existing variants to review/<env>/variants_to_delete.csv for deletion from Medusa:
    BARE-CODE ones ('Mgt','Gs',... supplier codes wrongly minted) and MIS-TYPED ones ('Azul White
    Quartzite' keyed under type Onyx). Both are excluded from matching (loaders), so their products
    re-gap and (for mis-typed) re-mint under the correct type; deleting them in Medusa + re-exporting
    removes them everywhere. Their <env>/variations/{Key}.png on S3 should be deleted too."""
    exp = SETTINGS.paths.export_file
    if not exp.exists():
        return 0
    rows = []
    by_key: dict[str, ExportVariation] = {}
    for r in read_variants_export(exp, require_status=VARIANTS_EXPORT_STATUS_REQUIRED):
        name, key = r.name, r.key
        if key:
            by_key[key] = r
        reason = ""
        if name and looks_code_shaped(name) == "bare_code":
            reason = "bare_code: supplier code/brand abbreviation, not a variety"
        elif name and loaders.is_mistyped_variant(key, name):
            reason = (f"mistyped: name says '{explicit_type_word(name)}' but keyed as type "
                      f"'{loaders.type_slug_from_key(key)}'")
        if reason:
            rows.append({"Key": key, "Name": name, "Id": r.id, "reason": reason})
    out = SETTINGS.paths.review_dir / "variants_to_delete.csv"
    # Preserve a previously-flagged variant that auto-detection can't re-derive (e.g. a single-token
    # name like 'Sodalita' that isn't an explicit type word) AS LONG AS it is still in the export.
    # Once it is actually deleted in Medusa (gone from the export) it drops off here automatically.
    auto_keys = {r["Key"] for r in rows}
    if out.exists():
        for prev in csv.DictReader(out.open(encoding="utf-8-sig")):
            pk = (prev.get("Key") or "").strip()
            if pk and pk in by_key and pk not in auto_keys:
                src = by_key[pk]
                rows.append({"Key": pk, "Name": src.name or (prev.get("Name") or "").strip(),
                             "Id": src.id or (prev.get("Id") or "").strip(),
                             "reason": prev.get("reason") or "flagged for deletion"})
    out.parent.mkdir(parents=True, exist_ok=True)
    # operator-opened delete list carrying scraped Names -> sanitize against formula injection + atomic.
    csvio.write_dicts(out, ["Key", "Name", "Id", "reason"],
                      sorted(rows, key=lambda r: r["Key"]), sanitize=True)
    if rows:
        log.warning("junk variants in the export -- delete in Medusa + S3",
                    extra={"extra_fields": {"count": len(rows), "file": str(out),
                                            "names": sorted({r["Name"] for r in rows})}})
    return len(rows)


def _consistency_errors(export_ids: set[str], combo_ids: set[str], prod_ids: set[str],
                        inv_skus: set[str], known_skus: set[str],
                        prod_tuples: set[tuple[str, ...]] = frozenset(),
                        combo_tuples: set[tuple[str, ...]] = frozenset(),
                        ) -> tuple[list[str], list[str]]:
    """Pure set-arithmetic core of the consistency gate (testable without files). Every product and
    combination variation id must exist in the export; every inventory SKU must exist in Medusa; and
    every product's FULL (type,variation,finish,colour,quality) tuple must be an allowed combination."""
    if not export_ids:
        return (["variants_export.csv is missing or has no Ids -- cannot verify"], [])
    errors: list[str] = []
    warnings: list[str] = []
    if combo_ids - export_ids:
        errors.append(f"{len(combo_ids - export_ids)} combination variation ids are NOT in the current "
                      "export (stale combinations -- rebuild against the refreshed export)")
    if prod_ids - export_ids:
        errors.append(f"{len(prod_ids - export_ids)} product variation ids are NOT in the current export "
                      "(stale products -- rerun run all + catalog)")
    if prod_ids - combo_ids:
        # HARD: a product whose variation has no valid-combination row ships UNPRICEABLE in Medusa.
        # With the finish->Raw fallback every typed variation gets a combination, so this firing means
        # a genuinely uncovered variation (no resolvable type) is being emitted -- it must be assigned
        # a type (review/tree_uncovered_variations.csv) or held, not silently shipped.
        errors.append(f"{len(prod_ids - combo_ids)} product variations have NO valid-combination row "
                      "(unpriceable -- assign a type in tree_uncovered_variations.csv or hold them)")
    # Beyond variation-level coverage: the product's FULL (type,variation,finish,colour,quality) tuple
    # must itself be an allowed combination. The finish->Raw fallback guarantees every typed variation
    # gets SOME combination row, so the variation-level check above passes even when a product ships a
    # type/colour the tree never allowed for that variation (e.g. a product served on Quartzite while
    # the allowed set has the variety only under Marble). That disagreement ships UNPRICEABLE and is the
    # exact "backbone gap" that drafts the product downstream -- catch it here, structurally.
    if combo_tuples:
        orphan = prod_tuples - combo_tuples
        if orphan:
            errors.append(f"{len(orphan)} product combinations (type/colour/finish/quality) are NOT in "
                          "valid_combinations (the product feed disagrees with the allowed set -- the "
                          "product's type/colour was never allowed for its variation; fix the source "
                          "type/colour or add the combination)")
    # Inventory updates may ONLY target products that exist in Medusa -- never push stock for a product
    # that was never imported (an imageless product held out of the catalog, or a stray SKU). Only
    # checked once a Medusa product export is present (known_skus non-empty).
    if known_skus and (inv_skus - known_skus):
        errors.append(f"{len(inv_skus - known_skus)} inventory SKUs are NOT in the Medusa product export "
                      "(refusing to update stock for products that don't exist)")
    return (errors, warnings)


def verify_consistency() -> tuple[list[str], list[str]]:
    """Deterministic cross-artifact consistency gate -- no heuristics, no sampling. Every product and
    every valid-combination row must reference a variation id that EXISTS in the current export; every
    inventory SKU must exist in Medusa; every product's variation should have a valid combination. This
    catches stale or out-of-order artifacts (the classic failure: combinations not rebuilt after the
    export changed) before they ship, so correctness is structural, not something a human/AI eyeballs.
    Returns (errors, warnings); a non-empty errors list means the upload set is inconsistent."""
    p = SETTINGS.paths

    def _ids(path: Path, col: str) -> set[str]:
        try:
            with path.open(encoding="utf-8-sig", newline="") as h:
                return {(r.get(col) or "").strip() for r in csv.DictReader(h) if (r.get(col) or "").strip()}
        except FileNotFoundError:
            return set()

    def _export_ids(path: Path) -> set[str]:
        if not path.exists():
            return set()
        return {v.id for v in read_variants_export(path, require_status=VARIANTS_EXPORT_STATUS_REQUIRED) if v.id}

    # Full-combination tuples, in ONE canonical field order (type, variation, finish, colour, quality),
    # so the product feed's STN ids and the valid-combinations columns compare directly. Category is
    # pinned by variation_id (Key prefix), so it is redundant in the tuple. Only complete tuples (every
    # component present) are compared -- an incomplete row is a missing-id case other gates already cover.
    def _tuples(path: Path, cols: tuple[str, ...]) -> set[tuple[str, ...]]:
        try:
            with path.open(encoding="utf-8-sig", newline="") as h:
                reader = csv.DictReader(h)
                # Fail loud on a renamed/dropped column: a silent all(vals)==False would drop every row
                # and no-op the tuple gate, masking a real mismatch. Only checked when the file has a
                # header (an empty file legitimately yields no tuples).
                if reader.fieldnames and (missing := [c for c in cols if c not in reader.fieldnames]):
                    raise ValueError(f"{path.name} is missing expected column(s) {missing} -- the tuple "
                                     "consistency gate cannot run against a changed schema")
                out: set[tuple[str, ...]] = set()
                for r in reader:
                    vals = tuple((r.get(c) or "").strip() for c in cols)
                    if all(vals):
                        out.add(vals)
                return out
        except FileNotFoundError:
            return set()

    combo_cols = ("type_id", "variation_id", "finish_id", "color_id", "quality_id")
    prod_cols = ("STN Type Id", "STN Variation Id", "STN Finish Id", "STN Color Id", "STN Quality Id")

    from stone_pipeline.stages.product_state import load_known_products
    known = load_known_products()
    errors, warnings = _consistency_errors(
        export_ids=_export_ids(p.variants_export_csv),
        combo_ids=_ids(p.to_upload_dir / "2_valid_combinations.csv", "variation_id"),
        prod_ids=_ids(p.to_upload_dir / "3_products_all.csv", "STN Variation Id"),
        inv_skus={s.upper() for s in _ids(p.to_upload_dir / "4_inventory_update.csv", "Variant Sku")},
        known_skus=set(known.by_sku),
        prod_tuples=_tuples(p.to_upload_dir / "3_products_all.csv", prod_cols),
        combo_tuples=_tuples(p.to_upload_dir / "2_valid_combinations.csv", combo_cols),
    )
    # Variety-identity uniqueness: emit collapses every (branch,type,name) to one survivor, so the emitted
    # full set must have NO identity appearing under two Keys. A failure here means a duplicate variety
    # would ship -- fail the whole produce loud rather than let it through (the bug this gate closes).
    errors += _identity_dup_errors(p.to_upload_dir / "1_variants_full.csv")
    return errors, warnings


def _identity_dup_errors(full_path: Path) -> list[str]:
    """Every (branch, type, name) in 1_variants_full must resolve to exactly ONE Key (emit's dedup gate)."""
    import collections
    from stone_pipeline.reference.loaders import variety_identity
    try:
        with full_path.open(encoding="utf-8-sig", newline="") as h:
            groups = collections.defaultdict(list)
            for r in csv.DictReader(h):
                if (r.get("Key") or "").strip():
                    groups[variety_identity(r["Key"], r.get("Name") or "")].append(r["Key"])
    except FileNotFoundError:
        return []
    return [f"duplicate variety {ident} under {len(keys)} Keys: {keys}"
            for ident, keys in groups.items() if len(keys) > 1]


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    outputs_root = Path(argv[0]) if argv else SETTINGS.paths.outputs_dir
    steps = run(outputs_root)
    print(f"upload checklist: {steps}")
    print(f"  variants:  {steps.parent / '1_variants_full.csv'}  (or 1_variants_update.csv)")
    print(f"  products:  {steps.parent / '3_products_all.csv'}")
    print(f"  inventory: {steps.parent / '4_inventory_update.csv'}  (existing products' stock only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
