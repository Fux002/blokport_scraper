"""Stage 7: image staging (section 7 Stage 7, section 13A.2).

Download each image, content-hash the bytes, dedup identical bytes, block known
placeholders by content hash, then store via the configured backend with a
content-addressed key. Slot by branch: blocks fill Front, Right, Back, Left;
slabs fill Product Image 1..N (N = SETTINGS.images.product_image_slots); thumbnail
is the first image either way. A re-run re-derives the same key and re-uploads nothing.

mode (config) selects the path:
  passthrough  use source URLs directly; no download, no storage backend.
  local        download + store under the local staging directory.
  s3           download + upload to the staging bucket.

When cfg.processing.enabled (local/s3 only), each freshly-fetched image is
enhanced/de-watermarked once before re-host (see io/image_processing.py): the
improved image goes to <prefix>/improved/, the raw download optionally to
<prefix>/scraped/, and the emit links to the improved one.

The fetcher is injectable so tests run offline with deterministic bytes.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Callable, Optional

from stone_pipeline.config.settings import SETTINGS, Confidence
from stone_pipeline.core import logfmt
from stone_pipeline.core.schema import CanonicalRow, FlagCode, ReviewFlag
from stone_pipeline.io import download as dl
from stone_pipeline.io import imagestore
from stone_pipeline.io import storage

log = logfmt.get_logger("images")

_BLOCK_SLOTS = 4  # Front, Right, Back, Left
# slab/tile product-image cap; shared with emit via settings so the columns can't drift
_SLAB_SLOTS = SETTINGS.images.product_image_slots

Fetcher = Callable[[str], Optional[bytes]]


def _load_placeholder_hashes() -> set[str]:
    path = SETTINGS.paths.placeholder_hashes_csv
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return {r["sha256"].strip() for r in csv.DictReader(handle) if r.get("sha256", "").strip()}


def _build_backend(cfg) -> Optional[storage.StorageBackend]:
    if cfg.mode == "local":
        return storage.LocalStorageBackend(cfg.local_staging_dir, cfg.public_base)
    if cfg.mode == "s3":
        s3 = SETTINGS.s3
        # Guard against a production run still pointing at the dev bucket (the
        # default until BLOKPORT_S3_BUCKET is set on the prod deployment).
        from stone_pipeline.config.settings import IS_PRODUCTION, _DEV_S3_BUCKET
        if IS_PRODUCTION and s3.bucket == _DEV_S3_BUCKET:
            raise RuntimeError(
                "production run targeting the DEV S3 bucket -- refusing to write/stamp prod images "
                f"into {_DEV_S3_BUCKET}. Set BLOKPORT_S3_BUCKET to the prod bucket.")
        return storage.S3StorageBackend(
            bucket=s3.bucket, region=s3.region, key_prefix=s3.staging_prefix,
            public_base=cfg.public_base, profile=s3.credentials_profile, dry_run=s3.dry_run,
        )
    return None  # passthrough


@dataclass
class ImageStats:
    staged: int = 0
    no_image: int = 0
    placeholders: int = 0
    download_failed: int = 0
    bytes_uploaded: int = 0
    processed: int = 0  # source images enhanced/de-watermarked before re-host


_MANIFEST_KEY = "_manifest.json"


def _load_manifest(backend) -> dict[str, str]:
    """Persisted source_url -> public_url map (cross-run idempotency on the URL).
    A URL processed in a prior scrape is reused as-is — never re-downloaded or
    re-processed — so repeated scrapes of the same products never duplicate an
    already-processed image, even if the supplier re-encodes the bytes."""
    try:
        raw = backend.get(_MANIFEST_KEY)
    except Exception:
        raw = None
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_manifest(backend, manifest: dict[str, str]) -> None:
    try:
        backend.put(_MANIFEST_KEY, json.dumps(manifest, sort_keys=True).encode("utf-8"),
                    content_type="application/json", overwrite=True)
    except Exception as exc:
        log.warning("could not persist image manifest", extra={"extra_fields": {"error": str(exc)}})


def _watermarked_sources() -> set[str]:
    """Source names flagged `watermarked: true` in sources.yaml."""
    from stone_pipeline.config.sources import load_sources

    return {name for name, cfg in load_sources().items() if getattr(cfg, "watermarked", False)}


def _write_preview(rows: list[dict]) -> None:
    """Append a source -> processed audit row so a human can eyeball results
    before they go live (de-watermarking is detect-then-inpaint, not perfect)."""
    if not rows:
        return
    path = SETTINGS.paths.workspace_root / "images" / "reports" / "processed_preview.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    cols = ["src_site", "source_url", "processed_url", "watermarked", "dewatermarked",
            "enhanced", "upscaled"]
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=cols)
        if new:
            writer.writeheader()
        writer.writerows(rows)


def _slot_row(row: CanonicalRow, public_urls: list[str]) -> None:
    row.image_keys = public_urls
    row.thumbnail_key = public_urls[0] if public_urls else None
    if row.is_block:
        # a block's first images fill the oriented faces (Front/Right/Back/Left); ANY MORE go into
        # the product/"other" images rather than being dropped -- use every image the scrape gave.
        row.oriented_image_keys = public_urls[:_BLOCK_SLOTS]
        row.product_image_keys = public_urls[_BLOCK_SLOTS:_BLOCK_SLOTS + _SLAB_SLOTS]
    else:
        row.product_image_keys = public_urls[:_SLAB_SLOTS]
        row.oriented_image_keys = []
    if not public_urls:
        row.add_flag(ReviewFlag(field="images", code=FlagCode.no_image,
                                confidence=Confidence.none, method="zero_usable", src_url=row.src_url))


def _readonly_manifest() -> dict[str, str]:
    """Read the imageproc manifest (source_url -> IMPROVED S3 url) straight from S3, read-only, so
    even a no-download passthrough run links products to the upscaled/de-watermarked images and
    NEVER to a raw supplier url. Best-effort: {} if S3/boto3 is unreachable (then source urls are
    kept so a fully-offline run still works)."""
    try:
        import boto3
        s3 = SETTINGS.s3
        client = boto3.Session(profile_name=s3.credentials_profile or None, region_name=s3.region).client("s3")
        obj = client.get_object(Bucket=s3.bucket, Key=imagestore.MANIFEST_KEY)
        data = json.loads(obj["Body"].read().decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.warning("imageproc manifest unreachable; passthrough keeps source urls",
                    extra={"extra_fields": {"error": str(exc)}})
        return {}


def run(rows: list[CanonicalRow], fetch: Optional[Fetcher] = None, cfg=None) -> ImageStats:
    cfg = cfg or SETTINGS.images
    placeholders = _load_placeholder_hashes()
    stats = ImageStats()

    if cfg.mode == "passthrough":
        # Link product images ONLY to their TREATED (improved) S3 version via the imageproc manifest.
        # Two things are HELD, never linked: a source url the manifest doesn't know, AND a manifest
        # entry that still points at an UNtreated upload (a source staged to S3 before enhancement ran).
        # So the upload only ever carries enhanced/upscaled/compressed images -- and a later produce
        # re-links each held image the moment its improved/ version lands in the manifest.
        manifest = _readonly_manifest()
        if not manifest:
            log.warning("passthrough: imageproc manifest empty/unreachable -- product images are HELD, "
                        "never linked to raw source urls. Run the image stage in s3 mode or restore S3 "
                        "access to populate the manifest.")
        improved_marker = imagestore.IMPROVED_MARKER              # a treated image lives under improved/
        held_untreated = 0
        for row in rows:
            srcs = [u for u in dict.fromkeys(row.raw_image_urls or []) if u and u.strip()]
            urls = []
            for u in srcs:
                mapped = manifest.get(u)
                if mapped and improved_marker in mapped:
                    urls.append(mapped)                          # treated -> link it
                elif mapped:
                    held_untreated += 1                          # on S3 but not enhanced yet -> hold
            _slot_row(row, urls)
            if urls:
                stats.staged += 1
            else:
                stats.no_image += 1
        log.info("images done (passthrough -> improved S3 only)", extra={"extra_fields": {
            "staged": stats.staged, "no_image": stats.no_image,
            "manifest_entries": len(manifest), "held_untreated": held_untreated}})
        return stats

    backend = _build_backend(cfg)
    fetch = fetch or dl.httpx_fetcher(timeout=cfg.timeout, retries=cfg.retries)

    # optional faithful enhancement / de-watermark before re-host (off by default)
    processor = None
    watermarked_sources: set[str] = set()
    if getattr(cfg, "processing", None) and cfg.processing.enabled:
        from stone_pipeline.io.image_processing import ImageProcessor

        processor = ImageProcessor(cfg.processing)
        watermarked_sources = _watermarked_sources()
    preview: list[dict] = []

    # cross-run idempotency on the SOURCE URL: a URL processed in a prior scrape is
    # reused from the manifest and skipped entirely (no re-download, no re-process,
    # no re-upload), so repeated scrapes of the same products can never duplicate an
    # already-processed image. Content-hash dedup (below) still applies to NEW urls.
    manifest = _load_manifest(backend)
    manifest_dirty = False

    # gather all distinct urls + url->site in one pass (O(1) lookups in the loop)
    all_urls: list[str] = []
    url_to_site: dict[str, str] = {}
    for row in rows:
        for u in (row.raw_image_urls or []):
            if u and u.strip():
                all_urls.append(u)
                url_to_site.setdefault(u, row.src_site)

    # known urls reuse their stored public url; only NEW urls are fetched
    url_to_public: dict[str, Optional[str]] = {}
    new_urls: list[str] = []
    for u in dict.fromkeys(all_urls):
        if u in manifest:
            url_to_public[u] = manifest[u]
        else:
            new_urls.append(u)
    # validation cap: process only N new images per run, so de-watermark/enhance
    # output can be eyeballed on a sample before a full run. 0 = no cap.
    sample_limit = int(os.environ.get("BLOKPORT_IMAGE_SAMPLE_LIMIT", "0") or 0)
    if sample_limit > 0:
        new_urls = new_urls[:sample_limit]
    fetched = dl.fetch_many(new_urls, fetch, concurrency=cfg.concurrency)

    # content-address: bytes hash -> public url (so identical bytes upload once)
    hash_to_public: dict[str, str] = {}
    for url, data in fetched.items():
        if data is None:
            url_to_public[url] = None
            stats.download_failed += 1
            continue
        # hash the SOURCE bytes: keys/dedup/placeholder checks stay stable on the
        # source even though the stored bytes are processed (so re-runs are no-ops).
        digest = hashlib.sha256(data).hexdigest()
        if digest in placeholders:
            url_to_public[url] = None
            stats.placeholders += 1
            continue
        if digest in hash_to_public:
            url_to_public[url] = hash_to_public[digest]
            manifest[url] = hash_to_public[digest]
            manifest_dirty = True
            continue
        src_site = url_to_site.get(url, "unknown")
        ck = storage.content_key(src_site, digest)
        # When processing runs, the improved image lives in its own subfolder and
        # the raw scraped copy in a sibling folder; Medusa points at the improved
        # one (the URL we return). Without processing, the image stays at the root.
        dest_key = f"{imagestore.IMPROVED_SUBDIR}/{ck}" if processor is not None else ck
        # store via backend (idempotent; re-run re-derives same key, no re-upload).
        # If it already exists, reuse the URL and skip processing entirely — each
        # source image is only ever enhanced/de-watermarked once.
        if backend.exists(dest_key):
            public = backend.url_for(dest_key)
        else:
            out = data
            if processor is not None:
                pr = processor.process(data, watermarked=src_site in watermarked_sources)
                out = pr.data
                stats.processed += 1
                # keep the raw download in the sibling scraped/ folder (same filename)
                if cfg.processing.keep_scraped and out is not data:
                    skey = f"{imagestore.SCRAPED_SUBDIR}/{ck}"
                    if not backend.exists(skey):
                        backend.put(skey, data)
                        stats.bytes_uploaded += len(data)
                if cfg.processing.write_preview:
                    preview.append({
                        "src_site": src_site, "source_url": url,
                        "processed_url": backend.url_for(dest_key),
                        "watermarked": src_site in watermarked_sources,
                        "dewatermarked": pr.dewatermarked, "enhanced": pr.enhanced,
                        "upscaled": pr.upscaled})
            public = backend.put(dest_key, out)
            stats.bytes_uploaded += len(out)
        hash_to_public[digest] = public
        url_to_public[url] = public
        manifest[url] = public
        manifest_dirty = True

    for row in rows:
        public_urls: list[str] = []
        for url in (row.raw_image_urls or []):
            if not url or not url.strip():
                continue
            public = url_to_public.get(url)
            if public is None:
                if url in fetched and fetched[url] is None:
                    row.add_flag(ReviewFlag(field="images", code=FlagCode.image_download_failed,
                                            raw_value=url, confidence=Confidence.low,
                                            method="download", src_url=row.src_url))
                continue
            if public not in public_urls:
                public_urls.append(public)
        _slot_row(row, public_urls)
        if public_urls:
            stats.staged += 1
        else:
            stats.no_image += 1

    if manifest_dirty:
        _save_manifest(backend, manifest)
    if processor is not None and cfg.processing.write_preview:
        _write_preview(preview)

    log.info("images done", extra={"extra_fields": {
        "mode": cfg.mode, "staged": stats.staged, "no_image": stats.no_image,
        "download_failed": stats.download_failed, "placeholders": stats.placeholders,
        "bytes_uploaded": stats.bytes_uploaded, "processed": stats.processed}})
    return stats
