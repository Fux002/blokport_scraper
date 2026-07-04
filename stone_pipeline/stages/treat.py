"""Treat scraped product images from the S3 raws into improved/ and repoint the imageproc manifest.

The supplier image CDNs block datacenter (AWS/ECS) IPs, so images are NEVER re-downloaded from the
supplier here. The raws are already on S3 (the scrape uploaded them); this reads THOSE, runs the
de-watermark (flagged sources only) + enhance + upscale + compress chain, writes each to
products/improved/<src>/<hash>.jpg, and rewrites the manifest so a passthrough produce links the
treated version. It therefore runs correctly from ANY host (ECS or laptop) -- no CDN dependency.

Idempotent (an image whose improved/ object already exists is skipped), concurrent, and never crashes
on a single bad image (the ImageProcessor returns the original bytes on any failure). No em dashes
(design principle 2).

    python -m stone_pipeline.stages.treat                 # every registered source
    python -m stone_pipeline.stages.treat --sources zucchi
    python -m stone_pipeline.stages.treat --overwrite     # re-treat even already-improved images
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET, S3_REGION
from stone_pipeline.core import logfmt
from stone_pipeline.io.image_processing import ImageProcessingConfig, ImageProcessor

log = logfmt.get_logger("treat")

_MANIFEST_KEY = f"{ENV_SEGMENT}/products/_manifest.json"
_BACKUP_KEY = f"{ENV_SEGMENT}/products/_manifest.backup.json"
_IMG_EXT = (".jpg", ".jpeg", ".png")


@dataclass
class TreatStats:
    treated: int = 0
    skipped: int = 0                                  # already treated -> idempotent no-op
    failed: int = 0
    manifest_repointed: int = 0
    errors: list[str] = field(default_factory=list)


def parse_raw_key(key: str) -> tuple[str, str] | None:
    """(source, filename) from a raw products key, or None if it is already improved, the manifest, or
    not a product image. Handles both layouts: products/<src>/<f> (s3 mode) and
    products/scraped/<src>/<f> (keep_scraped)."""
    if not key.lower().endswith(_IMG_EXT):
        return None
    parts = key.split("/")
    if "products" not in parts:
        return None
    rest = parts[parts.index("products") + 1:]
    if not rest or rest[0] == "improved":            # already treated
        return None
    if rest[0] == "scraped" and len(rest) >= 3:
        return rest[1], rest[-1]
    if len(rest) >= 2:
        return rest[0], rest[-1]
    return None


def _improved_key(source: str, filename: str) -> str:
    return f"{ENV_SEGMENT}/products/improved/{source}/{filename}"


def _is_watermarked(source: str) -> bool:
    try:
        from stone_pipeline.config.sources import load_sources
        return bool(getattr(load_sources().get(source), "watermarked", False))
    except Exception:
        return False


def _list(s3, prefix: str) -> list[str]:
    keys: list[str] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix):
        keys += [o["Key"] for o in page.get("Contents", [])]
    return keys


def _load_manifest(s3) -> dict:
    try:
        return json.loads(s3.get_object(Bucket=S3_BUCKET, Key=_MANIFEST_KEY)["Body"].read())
    except Exception:
        return {}


def repoint_manifest(s3, source: str) -> int:
    """Rewrite the manifest so `source` entries point at products/improved/<src>/ instead of the raw
    location. Backs up the pre-change manifest. Returns how many entries changed. Idempotent: entries
    already improved are left alone."""
    manifest = _load_manifest(s3)
    if not manifest:
        return 0
    backup = json.dumps(manifest).encode("utf-8")    # snapshot BEFORE modifying
    already = f"/products/improved/{source}/"
    markers = (f"/products/{source}/", f"/products/scraped/{source}/")
    changed = 0
    for k, v in list(manifest.items()):
        if not isinstance(v, str) or already in v:
            continue
        for marker in markers:
            if marker in v:
                manifest[k] = v.replace(marker, already)
                changed += 1
                break
    if changed:
        s3.put_object(Bucket=S3_BUCKET, Key=_BACKUP_KEY, Body=backup)
        s3.put_object(Bucket=S3_BUCKET, Key=_MANIFEST_KEY,
                      Body=json.dumps(manifest).encode("utf-8"), ContentType="application/json")
    return changed


def treat_source(source: str, *, s3=None, processor=None, concurrency: int = 24,
                 overwrite: bool = False) -> TreatStats:
    """Treat one source's raw S3 images into improved/ and repoint the manifest. `s3`/`processor` are
    injectable for tests. Idempotent unless `overwrite`."""
    import boto3
    s3 = s3 or boto3.client("s3", region_name=S3_REGION)
    processor = processor or ImageProcessor(ImageProcessingConfig(enabled=True))
    watermarked = _is_watermarked(source)
    stats = TreatStats()

    raw_keys = [k for pre in (f"{ENV_SEGMENT}/products/{source}/", f"{ENV_SEGMENT}/products/scraped/{source}/")
                for k in _list(s3, pre) if parse_raw_key(k)]
    existing = set() if overwrite else set(_list(s3, f"{ENV_SEGMENT}/products/improved/{source}/"))

    def _one(key: str):
        src, name = parse_raw_key(key)                       # raw_keys are pre-filtered, so never None
        dst = _improved_key(src, name)
        if not overwrite and dst in existing:
            return ("skip", key)
        try:
            data = s3.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
            res = processor.process(data, watermarked=watermarked)
            s3.put_object(Bucket=S3_BUCKET, Key=dst, Body=res.data, ContentType="image/jpeg")
            return ("ok", key)
        except Exception as exc:                             # a single bad image never fails the source
            return ("err", f"{key}: {exc}")

    if raw_keys:
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            for status, info in pool.map(_one, raw_keys):
                if status == "ok":
                    stats.treated += 1
                elif status == "skip":
                    stats.skipped += 1
                else:
                    stats.failed += 1
                    stats.errors.append(info)

    stats.manifest_repointed = repoint_manifest(s3, source)
    log.info("treated source images", extra={"extra_fields": {
        "source": source, "watermarked": watermarked, "treated": stats.treated, "skipped": stats.skipped,
        "failed": stats.failed, "manifest_repointed": stats.manifest_repointed}})
    return stats


def treat(sources: list[str] | None = None, *, overwrite: bool = False) -> dict[str, TreatStats]:
    """Treat the given sources (or every registered source) from their S3 raws. Returns per-source stats."""
    from stone_pipeline import adapters as registry
    todo = sorted(set(sources) & set(registry.REGISTRY)) if sources else sorted(registry.REGISTRY)
    return {s: treat_source(s, overwrite=overwrite) for s in todo}


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    overwrite = "--overwrite" in argv
    raw = next((argv[i + 1] for i, a in enumerate(argv) if a == "--sources" and i + 1 < len(argv)), None)
    sources = [s.strip() for s in raw.split(",") if s.strip()] if raw else None
    results = treat(sources, overwrite=overwrite)
    for src, st in results.items():
        print(f"  {src:14} treated={st.treated} skipped={st.skipped} failed={st.failed} "
              f"manifest_repointed={st.manifest_repointed}")
    total_failed = sum(st.failed for st in results.values())
    return 1 if total_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
