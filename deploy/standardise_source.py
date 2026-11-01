"""Standardise a source's scraped photos (RUN_MODE=standardise), on the GPU: reads <env>/products/scraped/<src>/, writes
finished images + the clean slab under <env>/standardise-test/<src>/, never into improved/ (going live is a later step).
Env: SRC; SURVEY=true only classifies every photo and writes the counts; SAMPLE=<n> takes n photos, the same n on every
run (the lowest hashes); ONLY_KEY=<s3 key of a JSON list of shas> runs just those; SLICE_OFFSET / SLICE_COUNT as in
deploy.reprocess_source. A photo already done is skipped, so a re-run is safe. One bad photo is recorded and never stops
the slice.
"""

from __future__ import annotations

import collections
import json
import os
import sys
import time

import boto3
import cv2
import numpy as np

from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET, S3_REGION, STANDARDISE as C
from stone_pipeline.io import imagestore
from stone_pipeline.standardise import index, kinds, models, outline, photo


def _encode(rgb, quality=92):
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValueError("could not encode")
    return buf.tobytes()


def _slab_png(slab):
    rgb, alpha = slab
    bgra = np.dstack([cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), (alpha.clip(0, 1) * 255).astype(np.uint8)])
    ok, buf = cv2.imencode(".png", bgra)
    if not ok:
        raise ValueError("could not encode")
    return buf.tobytes()


def _get(client, key):
    return client.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()


def _exists(client, prefix):
    return client.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix, MaxKeys=1).get("KeyCount", 0) > 0


def finished(client, out, sha):
    """This photo's run already ended, finished or held: a re-run, or results copied in from another environment's
    run of the same photo, is never worked on again."""
    return _exists(client, f"{out}{sha}.main.jpg") or _exists(client, f"{out}held/{sha}.json")


def photo_source(client, out, key, sha):
    """The photo a run works from: its cleaned copy when a hosted edit made one (deploy.standardise_select: reflections and
    marks off), else the scraped photo. A cleaned copy goes through every step a scraped photo does (the hosted edit
    leaves the straps, which the retouch takes off), except the logo step: it has no logo left."""
    cleaned = f"{out}cleaned/{sha}.jpg"
    return cleaned if _exists(client, cleaned) else key


def start(client, out, key, sha):
    """(the key this photo's standardising starts from, whether it is a cleaned copy, a hold reason or ""): the retouched
    photo first; a refused retouch holds the photo unless only straps and lamp reflections were to come off, which stay
    as shot; else the photo the run works from (photo_source)."""
    source = photo_source(client, out, key, sha)
    if _exists(client, retouched := f"{out}retouched/{sha}.jpg"):
        return retouched, source != key, ""
    if _exists(client, refused := f"{out}retouch_held/{sha}.json"):
        note = json.loads(_get(client, refused))
        if not note["stays_as_shot"]:
            return None, source != key, note["reason"]
    return source, source != key, ""


def _survey(rgb, m):
    """What this photo would become, without the expensive steps."""
    kind, confidence = kinds.classify(m.clip, rgb)
    if kinds.is_skipped(kind, confidence):
        return f"skip: {kind}"
    quads, _, _, _, reason = outline.find_slabs(m.seg, m.det, rgb)
    if not quads:
        return f"no outline: {reason.split(' (')[0]}"
    return "open book" if len(quads) == 2 else "slab"


def round_prefix(tag, src):
    """Where the run tagged `tag` keeps one source's results."""
    return f"{ENV_SEGMENT}/{C.TEST_PREFIX}/{tag + '/' if tag else ''}{src}/"


def run_prefix(src):
    """Where a test run writes: RUN_TAG keeps runs apart, so a new run never skips work done by an older one."""
    return round_prefix(os.environ.get("RUN_TAG", "").strip(), src)


def only(keys, wanted):
    """keys limited to the photos listed in `wanted` (shas), in the run's own order; all of them when there is no list."""
    if wanted is None:
        return keys
    wanted = set(wanted)
    return [k for k in keys if imagestore.sha_from_url(k) in wanted]


def _sheet(client):
    """This environment's product sheet, its categories read from the environment's CURRENT Medusa export first, as the
    produce does (fetch_inputs, then refresh_category_pcats): category ids differ per environment, prod has no committed
    export, and a committed one can lag Medusa. A fetch that fails keeps what is on disk, so a prod run without its export
    fails loud on its first category, never guesses."""
    from deploy import fetch_inputs
    from stone_pipeline.config import settings
    fetch_inputs.fetch_attributes(client)
    settings.refresh_category_pcats()
    return _get(client, f"{ENV_SEGMENT}/{C.SHEET_KEY}").decode("utf-8")


def sheet_photos(client):
    """{sha: {source, key, format, size}} of this environment's product sheet."""
    return index.build(_sheet(client))


def sheet_listings(client):
    """This environment's listings with their photos in order (index.listings)."""
    return index.listings(_sheet(client))


def _photo_keys(client, src, photos):
    """The photos of this run: those of `photos` (sheet_photos) scraped for src, stable order (slices rely on it),
    ONLY_KEY (an S3 JSON list of shas: a run of chosen photos), SAMPLE and the slice applied."""
    sample = int(os.environ.get("SAMPLE", "0"))
    offset, count = int(os.environ.get("SLICE_OFFSET", "0")), int(os.environ.get("SLICE_COUNT", "0"))
    listed = os.environ.get("ONLY_KEY", "").strip()
    keys = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=imagestore.scraped_prefix(src)):
        keys.extend(o["Key"] for o in page.get("Contents", []))
    keys.sort()
    known = only([k for k in keys if imagestore.sha_from_url(k) in photos], json.loads(_get(client, listed)) if listed else None)
    if sample:
        known = known[:sample]
    return known[offset:offset + count] if count else known[offset:]


def main() -> int:
    src = os.environ.get("SRC", "").strip()
    if not src:
        print("ERROR: SRC is required; aborting")
        return 2
    survey = os.environ.get("SURVEY", "false").lower() in ("1", "true", "yes")
    watermarked = os.environ.get("WATERMARKED", "false").lower() in ("1", "true", "yes")
    extra = os.environ.get("EXTRA_VIEWS", "true").lower() in ("1", "true", "yes")
    offset = int(os.environ.get("SLICE_OFFSET", "0"))
    client = boto3.client("s3", region_name=S3_REGION)
    out = run_prefix(src)
    photos = sheet_photos(client)
    sliced = _photo_keys(client, src, photos)
    print(f"==> standardise {src}: {len(sliced)} photos, survey={survey}", flush=True)
    m = models.load(survey_only=survey)
    tally = collections.Counter()
    spent = []
    for i, key in enumerate(sliced):
        sha = imagestore.sha_from_url(key)
        info = photos[sha]
        started = time.time()
        try:
            if not survey and finished(client, out, sha):
                tally["already done"] += 1
                continue
            if survey:
                rgb = photo.decode(_get(client, key))
                tally[_survey(rgb, m) if info["format"] == "slab" else info["format"]] += 1
            else:
                source_key, cleaned, refused = start(client, out, key, sha)
                if refused:
                    client.put_object(Bucket=S3_BUCKET, Key=f"{out}held/{sha}.json", ContentType="application/json",
                                      Body=json.dumps({"reason": refused, "kind": ""}, sort_keys=True).encode())
                    tally["held"] += 1
                    continue
                rgb = photo.decode(_get(client, source_key))
                logo = watermarked and not cleaned             # a cleaned copy has no logo left to take off
                clean = photo.decode(_get(client, f"{imagestore.improved_prefix(src)}{sha}.jpg")) if logo else None
                res = photo.standardise(rgb, fmt=info["format"], key=info["key"], size=tuple(info["size"]) if info["size"] else None,
                                        clean=clean, watermarked=logo, models=m, extra_views=extra)
                if res.held:
                    client.put_object(Bucket=S3_BUCKET, Key=f"{out}held/{sha}.json", ContentType="application/json",
                                      Body=json.dumps({"reason": res.held, "kind": res.kind}, sort_keys=True).encode())
                    tally["held"] += 1
                else:
                    # Written last, so "main" means this photo is complete.
                    for name, img in sorted(res.images.items(), key=lambda kv: kv[0] == "main"):
                        client.put_object(Bucket=S3_BUCKET, Key=f"{out}{sha}.{name}.jpg", Body=_encode(img), ContentType="image/jpeg")
                    if res.slab:
                        client.put_object(Bucket=S3_BUCKET, Key=f"{out}{sha}.slab.png", Body=_slab_png(res.slab), ContentType="image/png")
                    tally[res.kind] += 1
        except Exception as exc:                       # one bad photo never stops the slice; recorded and counted
            tally["failed"] += 1
            print(f"   FAILED {sha[:12]}: {type(exc).__name__}: {exc}", flush=True)
        spent.append(time.time() - started)
        if i % 10 == 0:
            print(f"   [{i + 1}/{len(sliced)}] {dict(tally)} median {np.median(spent):.1f}s per photo", flush=True)
    summary = {"source": src, "survey": survey, "counts": dict(tally), "photos": len(sliced),
               "median_seconds": round(float(np.median(spent)), 1) if spent else None}
    name = "survey" if survey else "run"
    client.put_object(Bucket=S3_BUCKET, Key=f"{out}{name}-{offset}.json", Body=json.dumps(summary, sort_keys=True).encode(), ContentType="application/json")
    print(f"==> {src} done: {json.dumps(summary, sort_keys=True)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
