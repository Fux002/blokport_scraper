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
size-reduced once before re-host: the hosted image goes to <prefix>/improved/,
the raw download optionally to <prefix>/scraped/ (the photo process's input), and
the emit links to the hosted one. A product of a source whose image pipeline is on
then links its published standardised photo when it has one.

The fetcher is injectable so tests run offline with deterministic bytes.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from stone_pipeline.core import env
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Optional

import cv2
import numpy as np

from stone_pipeline.config.settings import SETTINGS, Confidence
from stone_pipeline.config.standardise_settings import STANDARDISE
from stone_pipeline.core import logfmt
from stone_pipeline.core.schema import CanonicalRow, FlagCode, ReviewFlag
from stone_pipeline.io import download as dl
from stone_pipeline.io import imagestore
from stone_pipeline.io import storage
from stone_pipeline.standardise import cutout

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
        s3 = SETTINGS.s3            # never prod on the dev bucket: settings refuses that at import
        return storage.S3StorageBackend(
            bucket=s3.bucket, key_prefix=s3.staging_prefix,
            public_base=cfg.public_base, profile=s3.credentials_profile, dry_run=s3.dry_run,
        )
    return None  # passthrough


@dataclass
class ImageStats:
    staged: int = 0
    no_image: int = 0
    # subset of no_image with NO usable source url at all: a permanent reject (nothing to link, ever), NOT
    # republishable. no_image - no_image_source = the transient set a republish can still emit.
    no_image_source: int = 0
    placeholders: int = 0
    download_failed: int = 0
    bytes_uploaded: int = 0
    processed: int = 0  # source images size-reduced before re-host
    size_reduce_failed: int = 0  # photos the size-reduce could not handle (degenerate shape): held, retried next run


_MANIFEST_KEY = "_manifest.json"


def _load_manifest(backend) -> dict[str, str]:
    """Persisted source_url -> public_url map (cross-run idempotency on the URL).
    A URL processed in a prior scrape is reused as-is -- never re-downloaded or
    re-processed -- so repeated scrapes of the same products never duplicate an
    already-processed image, even if the supplier re-encodes the bytes.

    This map is rewritten with overwrite=True at the end of the run, so a silent
    empty read here would WIPE the whole cross-run idempotency map. `backend.get`
    returns None only for a genuine absence (fresh env) and RAISES on any real S3
    error, and a corrupt manifest raises here -- both fail loud rather than wipe."""
    raw = backend.get(_MANIFEST_KEY)
    if not raw:
        return {}                                    # genuinely absent -> fresh env, start empty
    data = json.loads(raw.decode("utf-8"))           # corrupt manifest fails loud, never silently wiped
    if not isinstance(data, dict):
        raise ValueError(f"image manifest {_MANIFEST_KEY} is not a JSON object")
    return {str(k): str(v) for k, v in data.items()}


def _save_manifest(backend, manifest: dict[str, str]) -> None:
    """Persist the source_url -> hosted-url manifest. Best-effort ONLY for a genuine persist failure
    (S3/network/local-disk): the manifest is recoverable -- the next run re-derives it and content-addressed
    images are not re-uploaded (backend.exists short-circuits), so a transient write failure must not fail
    the produce. A programming error (a non-serializable manifest, a broken import) is NOT recoverable and
    fails loud rather than being masked."""
    payload = json.dumps(manifest, sort_keys=True).encode("utf-8")   # a non-serializable manifest -> fail loud
    try:
        backend.put(_MANIFEST_KEY, payload, content_type="application/json", overwrite=True)
    except OSError as exc:                              # local-disk write failure: recoverable, best-effort
        log.error("could not persist image manifest (best-effort; next run re-derives it)",
                  extra={"extra_fields": {"error": str(exc)}})
    except Exception as exc:                            # S3 path: swallow ONLY a boto/S3 error, else fail loud
        import botocore.exceptions as be
        if not isinstance(exc, (be.BotoCoreError, be.ClientError)):
            raise
        log.error("could not persist image manifest (best-effort; next run re-derives it)",
                  extra={"extra_fields": {"error": str(exc)}})


def _hosted_object_exists(backend, hosted_url: str) -> bool:
    """Does a manifest entry's stored hosted URL still point at a real object? `url_for(key)` is
    `public_base + key`, so stripping the base yields the exact key `exists()` checks. This closes the
    manifest-vs-object gap: an EXTERNAL delete (e.g. a Blokport clear) removes the object but not this
    scraper's manifest, so a blind reuse would re-link a product to a dead URL and never re-upload. dry-run
    derives URLs with no network (`exists()` is always False there), so trust the manifest in dry-run; a URL
    not under this backend's base cannot be verified, so treat it as absent and re-fetch."""
    if getattr(backend, "dry_run", False):
        return True
    base = getattr(backend, "public_base", "")
    if not base or not hosted_url.startswith(base):
        return False
    return backend.exists(hosted_url[len(base):])


def _list_etags(prefix: str) -> dict[str, str]:
    """{content sha256: ETag} of the object keys under an S3 pool prefix. Raises on any S3 failure."""
    return _by_sha(_list_objects(prefix))


def _list_objects(prefix: str) -> dict[str, str]:
    """{key: ETag} under an S3 prefix. Raises on any S3 failure: the callers decide the policy."""
    s3 = SETTINGS.s3
    client = storage.aws_client("s3", config=storage.BOTOCORE_DEFAULTS, profile=s3.credentials_profile)
    return {obj["Key"]: obj["ETag"].strip('"')
            for page in client.get_paginator("list_objects_v2").paginate(Bucket=s3.bucket, Prefix=prefix)
            for obj in page.get("Contents", [])}


def _by_sha(objects: dict[str, str], suffix: str = "") -> dict[str, str]:
    """{content sha256: ETag} of the keys ending in `suffix` that carry a sha."""
    return {sha: etag for key, etag in objects.items() if key.endswith(suffix) and (sha := imagestore.sha_from_url(key))}


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
    if not public_urls:   # no linked image: a transient no_image, retried next scrape
        row.add_flag(ReviewFlag(field="images", code=FlagCode.no_image,
                                confidence=Confidence.none, method="zero_usable", src_url=row.src_url))


def _readonly_manifest() -> dict[str, str]:
    """Read the imageproc manifest (source_url -> IMPROVED S3 url) straight from S3, read-only, so
    even a no-download passthrough run links products to the re-hosted images and NEVER to a raw
    supplier url. Best-effort: {} if S3/boto3 is unreachable (then source urls are
    kept so a fully-offline run still works)."""
    try:
        s3 = SETTINGS.s3
        client = storage.aws_client("s3", config=storage.BOTOCORE_DEFAULTS, profile=s3.credentials_profile)
        obj = client.get_object(Bucket=s3.bucket, Key=imagestore.MANIFEST_KEY)
        data = json.loads(obj["Body"].read().decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.warning("imageproc manifest unreachable; passthrough keeps source urls",
                    extra={"extra_fields": {"error": str(exc)}})
        return {}


def run(rows: list[CanonicalRow], fetch: Optional[Fetcher] = None, cfg=None) -> ImageStats:
    """Stage 7. passthrough links products to their re-hosted S3 images through the read-only imageproc
    manifest; local/s3 fetch NEW source images, size-reduce them (when enabled) and host them, then link.
    Either way a row ends in _finish_row: linked or held (transient, retried next produce). The standardised
    photos are linked once validation has said which products are emitted (link_standard)."""
    cfg = cfg or SETTINGS.images
    stats = ImageStats()
    if cfg.mode == "passthrough":
        _link_from_manifest(rows, cfg, stats)
    else:
        _stage_and_link(rows, fetch, cfg, stats)
    return stats


def link_standard(rows: list[CanonicalRow], emitted: list[CanonicalRow], cfg=None, *,
                  image_pipeline: bool,
                  mismatches: Optional[list[dict]] = None) -> Optional[dict[str, dict[str, list[str]]]]:
    """After validation: every product linked to its standardised photo, from the published pool, and the conflict set
    {source: {sha: [handles]}} of the emitted products (index.shared_photos, each with every photo it has: the products
    on the sheet, nothing validation rejected), returned to be stored (save_conflicts) as the one set the photo process
    and the review page read. A conflict photo is linked only to the product its assignment file names. None when the
    pool cannot be read (nothing is stored then; the products keep their images and their listed state). The rows are one
    source's; `image_pipeline` is its switch as the produce loaded it (one value for the whole run). Off: every product
    keeps its hosted scraped photos (standard_photo stays None; populate never hides it), and the photo list and the
    conflict set are made as for any source (the photo process refuses a source on the sheet without them). A product
    carries its lead's 3D slab only from its own photo and at its own measured size (_no_3d); each emitted product
    refused one is added to `mismatches` (the source's diagnostics, photo_scale_mismatch) when given."""
    cfg = cfg or SETTINGS.images
    views = _load_standard_views(cfg)
    removed, assigned = _load_owner_removed(cfg), _load_assigned(cfg)
    # A pool unread, or with nothing published (local mode, before any publish), or the owner's removals or the conflict
    # assignments unread, cannot tell which products have a standardised photo: this run then never changes whether a
    # product is listed.
    known = bool(views and views.get("main")) and removed is not None and assigned is not None
    by_source = {**{r.src_site: {} for r in rows}, **_conflicts(emitted)}   # every source of the run, even one with none
    conflicts = {(src, sha) for src, photos in by_source.items() for sha in photos}
    removed, assigned = removed or set(), assigned or {}
    skus = _skus(rows) if removed or conflicts else {}
    outlines: dict[tuple[str, str, str], tuple[float, float]] = {}
    shared = _lot_shared(rows)
    misfits: list[tuple[CanonicalRow, dict]] = []
    for row in rows:
        row.own_photo_keys = list(row.image_keys)      # the photo list (emit.write_photos_json): before the views replace them
        if not image_pipeline:                         # off: it keeps the hosted scraped photos
            continue
        seen = _seen_by(row, skus.get(id(row)), views or {}, removed, conflicts, assigned)
        lead = listing_lead(row, seen, cfg) if _is_tile(row) else photo_lead(row.image_keys, seen)
        row.standard_photo = lead is not None if known else None
        if row.image_keys:
            # read from the product's own photos, before they are replaced by the standardised views
            row.model_3d_key = model_3d(seen, lead) if _is_3d_format(row) else None
            if row.model_3d_key and (misfit := _no_3d(row, lead, seen, outlines, shared)) is not None:
                row.model_3d_key = None
                misfits.append((row, misfit))
            row.standard_version = standard_version(seen, lead, with_3d=bool(row.model_3d_key))
            _slot_row(row, standardised(row.image_keys, seen, lead))
    if mismatches is not None:
        on_sheet = {id(r) for r in emitted}
        reported = [(r, m) for r, m in misfits if id(r) in on_sheet]
        sku_of = _skus([r for r, _ in reported])
        mismatches.extend({**m, "sku": sku_of[id(r)]} for r, m in reported)
    return None if views is None else by_source


def _no_3d(row: CanonicalRow, lead: Lead, seen: dict[str, dict[str, str]],
           outlines: dict[tuple[str, str, str], tuple[float, float]],
           shared: dict[str, set[str]]) -> Optional[dict]:
    """None when a product may carry its lead's 3D slab: a lot piece shown from its own photo, never its lot's shared
    one (Mas: no 3D until its own photo passes), and its length and width measured (inside the category's band, not
    filled from the pack's defaults) with the cut-out at their proportions (cutout.aspect_misfit, the one rule
    Blokport's renders measure too: the cut-out's width is the listing's length). Else why not, a photo_scale_mismatch
    row of the source's diagnostics: the judge's reason for a lot piece, both figures (and the band) for a size.
    outlines: each outline's px size by (source, sha, ETag), read once a run; shared: the lots' shared photos."""
    _, src, sha = lead
    if sha in shared.get(row.lot_code or "", ()):
        return {"listing": row.handle, "reason": "lot_shared_photo", "lead": sha,
                "why": _own_photo_state(row, shared[row.lot_code])}
    where = (src, sha, seen[imagestore.CUTOUT_SUBDIR][sha])
    if where not in outlines:
        outlines[where] = _outline_size(src, sha)
    w_px, h_px = outlines[where]
    found = {"listing": row.handle, "length_m": row.length, "width_m": row.width,
             "cutout_px": [round(w_px), round(h_px)]}
    if not (row.length and row.width and row.length > 0 and row.width > 0):
        return {**found, "reason": "size_missing", "listing_aspect": None, "cutout_aspect": round(w_px / h_px, 3),
                "fit_pct": None}
    found |= {"listing_aspect": round(row.length / row.width, 3), "cutout_aspect": round(w_px / h_px, 3),
              "fit_pct": round(cutout.aspect_off(w_px, h_px, row.length, row.width), 2)}
    band = {f.field: f.detail for f in row.review_flags
            if f.code == FlagCode.dimension_out_of_range and f.field in ("length", "width")}
    if band:                                            # derive's range check: no size of its category (Mas)
        return {**found, "reason": "size_out_of_band", "band": band}
    if _defaulted_size(row, ("length", "width")):
        return {**found, "reason": "size_defaulted"}
    if cutout.aspect_misfit(w_px, h_px, row.length, row.width) is not None:
        return {**found, "reason": "photo_scale_mismatch"}
    return None


def _lot_shared(rows: list[CanonicalRow]) -> dict[str, set[str]]:
    """{lot code: the photos two or more of its pieces show}: the lot's shared (bundle) photos. Each piece shows its own
    photos first, then the bundle's, never another piece's own (stages.lots._split), so a piece's other photos are its
    own. A lot of one piece has none: its photos cannot be told apart and count as its own."""
    shows: dict[str, Counter] = {}
    for row in rows:
        if row.lot_code:
            shows.setdefault(row.lot_code, Counter()).update({sha for _, sha in _shown(row)})
    return {lot: {sha for sha, n in seen.items() if n > 1} for lot, seen in shows.items()}


def _own_photo_state(row: CanonicalRow, shared: set[str]) -> str:
    """Why a lot piece is not shown from its own photo (its first photo not shared in its lot): the judge's reason when
    it failed, as the photo process's latest run of it recorded (the clean run's, else the standardiser run's)."""
    own = next(((src, sha) for src, sha in _shown(row) if sha not in shared), None)
    if own is None:
        return "no own photo"
    record = _judged(*own)
    if record is None:
        return "own photo not judged"
    if record.get("verdict") != "fail":
        return f"own photo {record.get('verdict')}"
    return "own photo failed: " + "; ".join(f"{p.get('kind')}: {p.get('where')}" for p in record.get("problems") or [])


def _judged(src: str, sha: str) -> Optional[dict]:
    """The judge's record of one photo in the clean run, else in the standardiser run (CLEAN_TAG, RUN_TAG); None when
    neither judged it. Any other failure to read it raises: the produce fails loud for the source."""
    from botocore.exceptions import BotoCoreError, ClientError

    from stone_pipeline.photos.source import round_prefix
    for tag in (STANDARDISE.CLEAN_TAG, STANDARDISE.RUN_TAG):
        key = f"{round_prefix(tag, src)}judged/{sha}.json"
        try:
            record = json.loads(_get_object(key))
        except ClientError as exc:
            if storage.s3_error_is_missing(exc):
                continue
            raise RuntimeError(f"the judge's record {key} cannot be read: {exc}") from exc
        except (BotoCoreError, ValueError) as exc:          # ValueError: not JSON
            raise RuntimeError(f"the judge's record {key} cannot be read: {exc}") from exc
        if not isinstance(record, dict):
            raise RuntimeError(f"the judge's record {key} is not a JSON object")
        return record
    return None


def _outline_size(src: str, sha: str) -> tuple[float, float]:
    """A published 3D slab's outline size in px (cutout.outline_size). Raises when it cannot be read or is not one: the
    produce fails loud for the source, never a silent "no 3D"."""
    from botocore.exceptions import BotoCoreError, ClientError

    key = imagestore.cutout_key(src, sha, "json")
    try:
        return cutout.outline_size(json.loads(_get_object(key)))
    except (BotoCoreError, ClientError, ValueError) as exc:     # ValueError: not JSON, or not an outline
        raise RuntimeError(f"the 3D slab's outline {key} cannot be read: {exc}") from exc


def save_conflicts(by_source: dict[str, dict[str, list[str]]]) -> None:
    """Store the produce's conflict set, one file per source (imagestore.conflicts_key), each whole so a conflict that is
    gone is gone, and only when it changed (the bucket is versioned: an identical write is a new version). Only a run
    that writes the bucket stores it: s3 image mode without the s3 dry run, as the service's produce runs; a laptop run
    (local or passthrough images, or the dry run) never overwrites an environment's set from its own data. Raises on any
    S3 failure: the photo process must never work from a set this produce did not store."""
    if SETTINGS.images.mode != "s3" or SETTINGS.s3.dry_run:
        log.info("conflict set not stored: this run does not write the bucket", extra={"extra_fields": {
            "image_mode": SETTINGS.images.mode, "s3_dry_run": SETTINGS.s3.dry_run, "sources": sorted(by_source)}})
        return
    for source, photos in sorted(by_source.items()):
        key, body = imagestore.conflicts_key(source), json.dumps(photos, sort_keys=True).encode()
        if _stored_object(key) != body:
            _put_object(key, body)


def _skus(rows: list[CanonicalRow]) -> dict[int, str]:
    """{id(row): its product's SKU} (the key of the owner's removals and of the conflict assignments)."""
    from stone_pipeline.config.sources import load_source
    from stone_pipeline.stages import emit

    cfgs = {src: load_source(src) for src in {r.src_site for r in rows}}
    return {id(r): emit._sku(r, cfgs[r.src_site]) for r in rows}


def _seen_by(row: CanonicalRow, sku: Optional[str], views: dict[str, dict[str, str]], removed: set[str],
             conflicts: set[tuple[str, str]], assigned: dict[tuple[str, str], set[str]]) -> dict[str, dict[str, str]]:
    """The published views as one product sees them, the one place that decides. A product the owner removed sees none:
    no standardised thumbnail, gallery, 3D or version is linked to it, even a photo published for a sibling of its stone,
    and with the pool known it has no standardised photo (hidden under the no_photo rule). A photo products of several
    stones show (conflicts) is a product's only when its assignment file names it (stone_pipeline.photos.publish); without
    a file, nobody's."""
    if sku in removed:
        return {}
    return _without(views, {sha for src, sha in _shown(row)
                            if (src, sha) in conflicts and sku not in assigned.get((src, sha), ())})


def _shown(row: CanonicalRow) -> list[tuple[str, str]]:
    """(source, sha) of each hosted photo of a product, in its order, once each."""
    return list(dict.fromkeys((m.group(1), m.group(2)) for u in row.image_keys if (m := _HOSTED.search(u))))


def _conflicts(emitted: list[CanonicalRow]) -> dict[str, dict[str, list[str]]]:
    """{source: {sha: [handles]}} of the photos emitted products of several stones show (index.shared_photos, each
    product with every photo it has), with every source of the emitted products present (an empty set clears an old
    one). A tile product never counts: it is shown only from the set rendered for itself (listing_lead), never a photo,
    and it never enters the photo process (standardise_run.photo_listings)."""
    from stone_pipeline.standardise import index

    listings = [{"key": r.handle, "source": shown[0][0], "stone": (r.variation_id or "", r.finish_id or ""),
                 "photos": [sha for _, sha in shown]}
                for r in emitted if not _is_tile(r) and (shown := _shown(r))]
    out = {l["source"]: {} for l in listings}
    for (src, sha), shared in index.shared_photos(listings).items():
        if shared.kind == "conflict":
            out[src][sha] = list(shared.listings)
    return out


def _without(views: dict[str, dict[str, str]], shas: set[str]) -> dict[str, dict[str, str]]:
    """The published views as one product sees them: without the photos in `shas`."""
    if not shas:
        return views
    return {view: {sha: etag for sha, etag in published.items() if sha not in shas} for view, published in views.items()}


_HOSTED = re.compile(r"/products/(?:[a-z]+/)?([a-z_]+)/([0-9a-f]{64})\.[a-z0-9]+$")
Lead = tuple[str, str, str]                            # (products base url, source, name) of a published set


def listing_lead(row: CanonicalRow, views: dict[str, set[str]], cfg) -> Optional[Lead]:
    """A tile product's lead: only the set rendered for the listing itself from its variety's slab stone
    (stone_pipeline.photos.publish.publish_tiles names it after the listing's handle, imagestore.listing_sha); None when
    there is none. Never one of the row's photos: a tile without its own set stays hidden rather than borrow a photo it
    shares with a slab (that slab's stone, possibly in another finish). None too when a dimension of the row was filled
    from the pack's defaults: the rendered tile would show a size the vendor never gave (the sheet carries only the
    numbers, and the tile default 60 x 60 x 2 is a common real size, so publish cannot tell; this stage can)."""
    if not row.handle or _defaulted_size(row):
        return None
    sha = imagestore.listing_sha(row.handle)
    return (cfg.public_base.rstrip("/"), row.src_site, sha) if sha in views.get("main", ()) else None


def photo_lead(urls: list[str], views: dict[str, set[str]]) -> Optional[Lead]:
    """Any other product's lead: its first published photo that shows a whole slab or an open book (it has a side view),
    else its first published photo; None without one. Only photos the judge passed are published, one chosen per
    listing. A set named after a listing is never a photo's, so it is never found here."""
    found = []
    for url in urls:
        m = _HOSTED.search(url)
        if m and m.group(2) in views.get("main", ()):
            found.append((url[:m.start()] + "/products", m.group(1), m.group(2)))
    if not found:
        return None
    return next((f for f in found if f[2] in views.get("studio", ())), found[0])


def standardised(urls: list[str], views: dict[str, set[str]], lead: Optional[Lead]) -> list[str]:
    """A product's images: its lead's standardised views when it has a lead (user 2026-11-01: one very good image per
    listing is enough, shown in every view): the showroom image is the catalog thumbnail, then the white, side and
    close-up views, whichever it has (a block: its showroom image only). The product's other photos are left out, their
    warehouse would show next to the showroom; a product without a lead keeps the images it had (`urls`)."""
    if lead is None:
        return urls
    base, src, sha = lead
    return [f"{base}/{imagestore.STANDARD_SUBDIR}/{view}/{src}/{sha}.jpg"
            for view in imagestore.STANDARD_VIEWS if sha in views.get(view, ())]


def model_3d(views: dict[str, set[str]], lead: Optional[Lead]) -> Optional[str]:
    """The 3D model's cut-out url of a product: its lead's (the stone the catalog thumbnail shows), when the lead has a
    published cut-out (a whole slab: a single slab or one half of an open book; a rendered tile); None otherwise (a
    close-up, no lead)."""
    if lead is None or lead[2] not in views.get(imagestore.CUTOUT_SUBDIR, ()):
        return None
    base, src, sha = lead
    return f"{base}/{imagestore.STANDARD_SUBDIR}/{imagestore.CUTOUT_SUBDIR}/{src}/{sha}.webp"


def standard_version(views: dict[str, dict[str, str]], lead: Optional[Lead], with_3d: bool) -> Optional[str]:
    """The ETags of the files a product is served from its lead (its views, its 3D model when it has one), None without
    one. A file is re-made in place under the same name (a listing's choice moved to another run of the photo, a tile
    rendered again), so the names alone would never re-serve the product with it."""
    if lead is None:
        return None
    sha = lead[2]
    kinds = [*imagestore.STANDARD_VIEWS, *([imagestore.CUTOUT_SUBDIR] if with_3d else [])]
    return " ".join(views[k][sha] for k in kinds if sha in views.get(k, ()))


def _defaulted_size(row: CanonicalRow, fields: tuple[str, ...] = ("length", "width", "height")) -> bool:
    return any(f.code == FlagCode.dimension_defaulted and f.field in fields for f in row.review_flags)


def _is_tile(row: CanonicalRow) -> bool:
    return (row.format_value or "").casefold() == STANDARDISE.TILE_FORMAT


def _is_3d_format(row: CanonicalRow) -> bool:
    return (row.format_value or "").casefold() in (STANDARDISE.CUTOUT_FORMAT, STANDARDISE.TILE_FORMAT)


def _load_standard_views(cfg) -> Optional[dict[str, dict[str, str]]]:
    """{view: {sha: ETag}} of the published standardised photos, and of their 3D slabs under CUTOUT_SUBDIR (the pair's
    two ETags, once both files are there: the outline is written last). Empty in local mode or when none are published.
    None when the pool cannot be read: the products keep the images they had this run (today's images, never a guess),
    it is logged, and the next produce links the standardised set."""
    if getattr(cfg, "mode", None) == "local":
        return {}
    try:
        views = {view: _list_etags(imagestore.standard_prefix(view)) for view in imagestore.STANDARD_VIEWS}
        cutouts = _list_objects(imagestore.cutout_prefix())
        webp, meta = (_by_sha(cutouts, ext) for ext in (".webp", ".json"))
        views[imagestore.CUTOUT_SUBDIR] = {sha: f"{webp[sha]}+{meta[sha]}" for sha in webp.keys() & meta.keys()}
        return views
    except Exception as exc:
        log.warning("standardised photos unreachable; products keep their images this run",
                    extra={"extra_fields": {"error": str(exc)}})
        return None


def _load_owner_removed(cfg) -> Optional[set[str]]:
    """The SKUs of the products the owner removed (imagestore.owner_removed_prefix, stone_pipeline.photos.decisions): none in
    local mode. None when storage cannot be read: like an unread photo pool, the run then cannot tell. Any other error is
    a bug and fails loud: it must never quietly freeze which products are listed."""
    from botocore.exceptions import BotoCoreError, ClientError

    if getattr(cfg, "mode", None) == "local":
        return set()
    try:
        return imagestore.owner_removed_skus(_list_objects(imagestore.owner_removed_prefix()))
    except (BotoCoreError, ClientError) as exc:
        log.warning("the owner's removed listings unreadable; products keep their listed state this run",
                    extra={"extra_fields": {"error": str(exc)}})
        return None


def _load_assigned(cfg) -> Optional[dict[tuple[str, str], set[str]]]:
    """{(source, sha): SKUs} of the published photos listings of several stones show, with the products the owner assigned
    each to (stone_pipeline.photos.publish): none in local mode. None when storage cannot be read (the run cannot tell); any
    other error fails loud."""
    from botocore.exceptions import BotoCoreError, ClientError

    if getattr(cfg, "mode", None) == "local":
        return {}
    try:
        return {photo: set(json.loads(_get_object(key))) for key in sorted(_list_objects(imagestore.assigned_prefix()))
                if (photo := imagestore.assigned_photo(key))}
    except (BotoCoreError, ClientError) as exc:
        log.warning("the conflict photos' assignments unreadable; products keep their listed state this run",
                    extra={"extra_fields": {"error": str(exc)}})
        return None


def _put_object(key: str, body: bytes) -> None:
    """Write one JSON object. Raises on any S3 failure: the callers decide the policy."""
    s3 = SETTINGS.s3
    client = storage.aws_client("s3", config=storage.BOTOCORE_DEFAULTS, profile=s3.credentials_profile)
    client.put_object(Bucket=s3.bucket, Key=key, Body=body, ContentType="application/json")


def _stored_object(key: str) -> Optional[bytes]:
    """One object's bytes, None when it does not exist. Raises on any other S3 failure."""
    try:
        return _get_object(key)
    except Exception as exc:
        if storage.s3_error_is_missing(exc):
            return None
        raise


def _get_object(key: str) -> bytes:
    """One object's bytes. Raises on any S3 failure: the callers decide the policy."""
    s3 = SETTINGS.s3
    client = storage.aws_client("s3", config=storage.BOTOCORE_DEFAULTS, profile=s3.credentials_profile)
    return client.get_object(Bucket=s3.bucket, Key=key)["Body"].read()


def _finish_row(row: CanonicalRow, urls: list[str], srcs: int, stats: ImageStats) -> None:
    """Slot a row's linked images and count it."""
    _slot_row(row, urls)
    if urls:
        stats.staged += 1
    else:
        stats.no_image += 1
        if srcs == 0:                             # no usable source url -> permanent, not republishable
            stats.no_image_source += 1


def _source_urls(row: CanonicalRow) -> list[str]:
    return [u for u in dict.fromkeys(row.raw_image_urls or []) if u and u.strip()]


# --- passthrough: link to the treated images the imageproc manifest knows -----------------------------------
def _link_from_manifest(rows: list[CanonicalRow], cfg, stats: ImageStats) -> None:
    # Link product images ONLY to their re-hosted (improved) S3 version via the imageproc manifest. Two things are
    # HELD, never linked: a source url the manifest doesn't know, AND a manifest entry that still points at a raw upload
    # outside improved/. A later produce re-links each held image the moment its improved/ version is in the manifest.
    manifest = _readonly_manifest()
    if not manifest:
        log.warning("passthrough: imageproc manifest empty/unreachable -- product images are HELD, "
                    "never linked to raw source urls. Run the image stage in s3 mode or restore S3 "
                    "access to populate the manifest.")
    held_raw = 0
    for row in rows:
        srcs = _source_urls(row)
        urls: list[str] = []
        for u in srcs:
            mapped = manifest.get(u)
            if mapped and imagestore.IMPROVED_MARKER in mapped:
                urls.append(mapped)
            elif mapped:
                held_raw += 1                                # on S3 but not re-hosted into improved/ -> hold
        _finish_row(row, urls, len(srcs), stats)
    log.info("images done (passthrough -> improved S3 only)", extra={"extra_fields": {
        "staged": stats.staged, "no_image": stats.no_image, "no_image_source": stats.no_image_source,
        "manifest_entries": len(manifest), "held_raw": held_raw}})


# --- local / s3: fetch, size-reduce, host, link ---------------------------------------------------------------------------
def _size_reduce(data: bytes, cfg) -> bytes:
    """The photo with its long edge capped at cfg.target_long_edge (downscale only, never enlarged: more pixels add bytes,
    not detail) and re-encoded as JPEG. Bytes that are not a decodable raster (an svg) are hosted as they came."""
    bgr = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if bgr is None:
        return data
    h, w = bgr.shape[:2]
    if max(h, w) > cfg.target_long_edge:
        scale = cfg.target_long_edge / max(h, w)
        bgr = cv2.resize(bgr, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
    ok, enc = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, cfg.jpeg_quality])
    if not ok:
        raise ValueError("could not encode the size-reduced photo as JPEG")
    return enc.tobytes()


def _url_sites(rows: list[CanonicalRow]) -> dict[str, str]:
    """Every distinct source url -> the site that scraped it (first wins), in one pass."""
    url_to_site: dict[str, str] = {}
    for row in rows:
        for u in (row.raw_image_urls or []):
            if u and u.strip():
                url_to_site.setdefault(u, row.src_site)
    return url_to_site


def _reuse_hosted(manifest: dict, backend, urls) -> tuple[dict[str, Optional[str]], list[str], int]:
    """Cross-run idempotency on the SOURCE URL: a url processed in a prior scrape reuses its hosted public
    url and is skipped entirely (no re-download, no re-process, no re-upload). A manifest hit is
    authoritative ONLY if its object still exists: an external delete (a Blokport clear) removes the object
    but not the manifest, so a blind reuse would re-link a product to a dead URL and never re-upload. A stale
    entry is dropped (the url re-fetches + re-hosts) and the manifest self-heals. Returns (url -> public for
    the reused ones, the NEW urls, stale entries pruned)."""
    url_to_public: dict[str, Optional[str]] = {}
    new_urls: list[str] = []
    stale_pruned = 0
    for u in urls:
        hosted = manifest.get(u)
        if hosted and _hosted_object_exists(backend, hosted):
            url_to_public[u] = hosted
        else:
            if u in manifest:                      # entry points at a since-deleted object -> drop + re-host
                del manifest[u]
                stale_pruned += 1
            new_urls.append(u)
    return url_to_public, new_urls, stale_pruned


def _sample(new_urls: list[str]) -> list[str]:
    # validation cap: host only N new images per run, so a sample can be eyeballed before a full run. 0 = no cap.
    sample_limit = int(env.getenv("BLOKPORT_IMAGE_SAMPLE_LIMIT", "0") or 0)
    return new_urls[:sample_limit] if sample_limit > 0 else new_urls


class _Stager:
    """Hosts one NEW source image per call: content-addressed dedup, placeholder drop, reuse of an already hosted
    object, else the raw bytes at the content key or (cfg.processing.enabled) the size-reduced photo under improved/
    with the raw original kept under scraped/. Returns the public url, or None for a placeholder, a failed download or
    a photo the size-reduce could not handle (held like a failed download, so one bad photo never stops the run)."""

    def __init__(self, cfg, backend, manifest: dict, url_to_site: dict[str, str], stats: ImageStats):
        self.cfg, self.backend, self.manifest = cfg, backend, manifest
        self.url_to_site, self.stats = url_to_site, stats
        self.placeholders = _load_placeholder_hashes()
        self.hash_to_public: dict[str, str] = {}      # content-address: bytes hash -> public url (upload once)
        self.manifest_dirty = False

    def stage(self, url: str, data: Optional[bytes]) -> Optional[str]:
        if data is None:
            self.stats.download_failed += 1
            return None
        # hash the SOURCE bytes: keys/dedup/placeholder checks stay stable on the source even though the
        # stored bytes are size-reduced (so re-runs are no-ops).
        digest = hashlib.sha256(data).hexdigest()
        if digest in self.placeholders:
            self.stats.placeholders += 1
            return None
        if digest in self.hash_to_public:
            return self._remember(url, self.hash_to_public[digest])
        ck = storage.content_key(self.url_to_site.get(url, "unknown"), digest)
        processing = self.cfg.processing.enabled
        # With processing the hosted photo lives in improved/ and the raw original in its sibling scraped/; Medusa
        # points at the improved one. Without processing, the raw bytes stay at the root.
        dest_key = f"{imagestore.IMPROVED_SUBDIR}/{ck}" if processing else ck
        if self.backend.exists(dest_key):
            public = self.backend.url_for(dest_key)          # hosted by this run or a prior one: reused as it is
        else:
            if processing:
                try:
                    body = _size_reduce(data, self.cfg.processing)
                except (cv2.error, ValueError) as exc:     # a degenerate photo (e.g. 1 x 5000 px): this photo only
                    self.stats.size_reduce_failed += 1
                    log.error("photo could not be size-reduced; held this run",
                              extra={"extra_fields": {"url": url, "error": str(exc)}})
                    return None
                self.stats.processed += 1
                self._keep_scraped(ck, data)
            else:
                body = data
            public = self.backend.put(dest_key, body)
            self.stats.bytes_uploaded += len(body)
        self.hash_to_public[digest] = public
        return self._remember(url, public)

    def _keep_scraped(self, ck: str, data: bytes) -> None:
        """The raw original under scraped/ (keep_scraped): the photo process's input. Idempotent."""
        skey = f"{imagestore.SCRAPED_SUBDIR}/{ck}"
        if self.cfg.processing.keep_scraped and not self.backend.exists(skey):
            self.backend.put(skey, data)
            self.stats.bytes_uploaded += len(data)

    def _remember(self, url: str, public: str) -> str:
        self.manifest[url] = public
        self.manifest_dirty = True
        return public


def _link_hosted(rows: list[CanonicalRow], url_to_public: dict[str, Optional[str]], fetched: dict,
                 cfg, stats: ImageStats) -> None:
    for row in rows:
        public_urls: list[str] = []
        srcs = 0
        for url in (row.raw_image_urls or []):
            if not url or not url.strip():
                continue
            srcs += 1
            public = url_to_public.get(url)
            if public is None:
                # download failed / placeholder-blocked -> transient, retried next produce
                if url in fetched and fetched[url] is None:
                    row.add_flag(ReviewFlag(field="images", code=FlagCode.image_download_failed,
                                            raw_value=url, confidence=Confidence.low,
                                            method="download", src_url=row.src_url))
                continue
            if public not in public_urls:
                public_urls.append(public)
        _finish_row(row, public_urls, srcs, stats)


def _stage_and_link(rows: list[CanonicalRow], fetch: Optional[Fetcher], cfg, stats: ImageStats) -> None:
    backend = _build_backend(cfg)
    fetch = fetch or dl.httpx_fetcher(timeout=cfg.timeout, retries=cfg.retries)
    manifest = _load_manifest(backend)
    url_to_site = _url_sites(rows)
    url_to_public, new_urls, stale_pruned = _reuse_hosted(manifest, backend, url_to_site)
    fetched = dl.fetch_many(_sample(new_urls), fetch, concurrency=cfg.concurrency)
    stager = _Stager(cfg, backend, manifest, url_to_site, stats)
    for url, data in fetched.items():
        url_to_public[url] = stager.stage(url, data)
    _link_hosted(rows, url_to_public, fetched, cfg, stats)
    if stager.manifest_dirty or stale_pruned:
        _save_manifest(backend, manifest)
    log.info("images done", extra={"extra_fields": {
        "mode": cfg.mode, "staged": stats.staged, "no_image": stats.no_image,
        "no_image_source": stats.no_image_source,
        "download_failed": stats.download_failed, "placeholders": stats.placeholders,
        "bytes_uploaded": stats.bytes_uploaded, "processed": stats.processed,
        "size_reduce_failed": stats.size_reduce_failed,
        "stale_manifest_pruned": stale_pruned}})
