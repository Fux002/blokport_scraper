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
from botocore.exceptions import BotoCoreError, ClientError

from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET, S3_REGION, STANDARDISE as C
from stone_pipeline.core import env
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


def outdated(client, out, sha):
    """A result made before an open book's halves had their slab: a side view (studio) and no <sha>.slab.png, and not
    tried again before (noslab/). Every result made now that has a side view has its slab, so nothing else matches."""
    return (_exists(client, f"{out}{sha}.studio.jpg") and not _exists(client, f"{out}{sha}.slab.png")
            and not _exists(client, f"{out}noslab/{sha}.json"))


def finished(client, out, sha):
    """This photo's run already ended, finished or held: a re-run, or results copied in from another environment's
    run of the same photo, is never worked on again; only an outdated result is, once (complete)."""
    if _exists(client, f"{out}held/{sha}.json"):
        return True
    return _exists(client, f"{out}{sha}.main.jpg") and not outdated(client, out, sha)


def complete(client, out, sha, res):
    """Complete an outdated open-book result from its re-run: its close view, then its slab (written last: the result is
    complete). What was judged and published (main, white, studio) stays as it is, so its verdict and choice stand. A
    completion cut short takes its close view off again while the slab is missing, so half of one is never published.
    A re-run that is no open book this time adds nothing and is noted in noslab/, so it is never run again."""
    if res.kind == "open book" and res.slab:
        try:
            if "close" in res.images:
                client.put_object(Bucket=S3_BUCKET, Key=f"{out}{sha}.close.jpg", Body=_encode(res.images["close"]),
                                  ContentType="image/jpeg")
            client.put_object(Bucket=S3_BUCKET, Key=f"{out}{sha}.slab.png", Body=_slab_png(res.slab), ContentType="image/png")
        except BaseException:
            if not _exists(client, f"{out}{sha}.slab.png"):
                client.delete_object(Bucket=S3_BUCKET, Key=f"{out}{sha}.close.jpg")
            raise
        return "completed"
    tried(client, out, sha, res.held or f"the re-run is a {res.kind}, no open book")
    return "no slab"


def tried(client, out, sha, reason):
    """Note that an outdated result was tried and gave no slab (noslab/<sha>.json), so it is never run again. Never
    held/: the result is judged and may be published, and a hold would take it from every listing that has it."""
    client.put_object(Bucket=S3_BUCKET, Key=f"{out}noslab/{sha}.json", ContentType="application/json",
                      Body=json.dumps({"reason": reason}, sort_keys=True).encode())


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


def own_input(client, key, read=photo.read):
    """A photo's own input from storage, decoded (read: photo.read or photo.decode); photo.NoCloseUp when it is missing
    or no image, that photo's outcome, not the run's. Any other storage error (denied, throttled) is raised as it is."""
    try:
        data = _get(client, key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            raise photo.NoCloseUp(f"its input is missing: {key}") from exc
        raise
    try:
        return read(data)
    except ValueError as exc:                          # photo.read / decode: the bytes are no image
        raise photo.NoCloseUp(f"its input is no image: {key}") from exc


def start_edits(client, out, key, source_key, cleaned, full):
    """Where the photo a run starts from (`full`, read from source_key) is not as shot (views.close_up's edits): all of a
    hosted model's cleaned copy or a retouch of one, a retouch's fills (photo.retouch_fills against the work copy of the
    scraped photo `key` it was made from), None for the scraped photo itself."""
    if cleaned:
        return np.ones(full.shape[:2], np.uint8)
    if source_key.startswith(f"{out}retouched/"):
        return photo.retouch_fills(full, own_input(client, key, photo.decode))
    return None


def _survey(rgb, m):
    """What this photo would become, without the expensive steps."""
    kind, confidence = kinds.classify(m.clip, rgb)
    if kinds.is_skipped(kind, confidence):
        return f"skip: {kind}"
    quads, _, _, _, reason = outline.find_slabs(m.seg, m.det, rgb)
    if not quads:
        return f"no outline: {reason.split(' (')[0]}"
    return "open book" if len(quads) == 2 else "slab"


_GPU_ERRORS = ("cuda", "cudnn", "cublas", "cufft")     # a broken GPU fails every photo after it, in any of its libraries
_PASSING_STORAGE = ("SlowDown", "Throttling", "ThrottlingException", "RequestTimeout", "ServiceUnavailable", "InternalError")


def gpu_error(exc):
    return type(exc).__name__ == "OutOfMemoryError" or any(word in str(exc).lower() for word in _GPU_ERRORS)


def passing_storage_error(exc):
    """A storage error that passes (throttling, a timeout, a 5xx, a connection error); a missing or forbidden object is
    no passing error: it is the photo's own input that is wrong."""
    if isinstance(exc, BotoCoreError):
        return True
    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {})
        return err.get("Code") in _PASSING_STORAGE or exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) >= 500
    return False


def after_failure(exc, in_a_row, crashed_before):
    """What a slice does after a photo failed: "hold" it (the photo itself cannot be standardised: its listing moves on,
    the tracked list shows why), "retry" it (a passing storage error: no record, the next run does it) or "stop" the
    slice (the job fails loud and the next run retries its photos). A GPU error stops the slice, as it breaks the GPU
    for every photo after it, unless the same photo crashed it before (then it is the photo: held). Passing storage
    errors stop the slice after SLICE_MAX_STORAGE_FAILURES in a row. in_a_row counts this one."""
    if gpu_error(exc):
        return "hold" if crashed_before else "stop"
    if passing_storage_error(exc):
        return "stop" if in_a_row >= C.SLICE_MAX_STORAGE_FAILURES else "retry"
    return "hold"


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


def _photo_list(client):
    """This environment's photo list (STANDARDISE.PHOTOS_KEY, beside the sheet: each listing's own photos in its order).
    Missing or malformed, it fails loud: the sheet's image columns are never read instead (once a listing is published
    they carry only its chosen photo's views)."""
    key = f"{ENV_SEGMENT}/{C.PHOTOS_KEY}"
    try:
        text = _get(client, key).decode("utf-8")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            raise RuntimeError(f"no photo list at s3://{S3_BUCKET}/{key}: a full produce writes it beside the sheet") from exc
        raise
    try:
        return index.parse_photos(text)
    except ValueError as exc:
        raise RuntimeError(f"the photo list s3://{S3_BUCKET}/{key} is malformed: {exc}") from exc


def sheet_photos(client):
    """{sha: {source, key, format, size}} of this environment's product sheet and its photo list."""
    return index.build(_sheet(client), _photo_list(client))


def sheet_and_photos(client):
    """(this environment's product sheet text, its photo list): what the photo process reads, for its listings
    (index.listings) and for publish (the tile listings)."""
    return _sheet(client), _photo_list(client)


def scraped_keys(client, src):
    """Every scraped photo key of src, in a stable order (slices rely on it)."""
    keys = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=imagestore.scraped_prefix(src)):
        keys.extend(o["Key"] for o in page.get("Contents", []))
    return sorted(keys)


def listed_shas(client):
    """The shas ONLY_KEY lists (an S3 JSON list of shas: a run of chosen photos), or None when it is not set."""
    listed = os.environ.get("ONLY_KEY", "").strip()
    return json.loads(_get(client, listed)) if listed else None


def _photo_keys(client, src, photos):
    """The photos of this run: those of `photos` (sheet_photos) scraped for src, stable order (slices rely on it),
    ONLY_KEY, SAMPLE and the slice applied."""
    sample = int(os.environ.get("SAMPLE", "0"))
    offset, count = int(os.environ.get("SLICE_OFFSET", "0")), int(os.environ.get("SLICE_COUNT", "0"))
    known = only([k for k in scraped_keys(client, src) if imagestore.sha_from_url(k) in photos], listed_shas(client))
    if sample:
        known = known[:sample]
    return known[offset:offset + count] if count else known[offset:]


def main() -> int:
    src = os.environ.get("SRC", "").strip()
    if not src:
        print("ERROR: SRC is required; aborting")
        return 2
    survey = os.environ.get("SURVEY", "false").lower() in ("1", "true", "yes")
    try:
        watermarked = env.job_flag("WATERMARKED")
    except ValueError as exc:
        print(f"ERROR: {exc}; aborting")
        return 2
    extra = os.environ.get("EXTRA_VIEWS", "true").lower() in ("1", "true", "yes")
    offset = int(os.environ.get("SLICE_OFFSET", "0"))
    client = boto3.client("s3", region_name=S3_REGION)
    out = run_prefix(src)
    photos = sheet_photos(client)
    sliced = _photo_keys(client, src, photos)
    print(f"==> standardise {src}: {len(sliced)} photos, survey={survey}", flush=True)
    m = models.load("survey" if survey else "all")
    tally = collections.Counter()
    spent = []
    storage_in_a_row = 0
    for i, key in enumerate(sliced):
        sha = imagestore.sha_from_url(key)
        info = photos[sha]
        started = time.time()
        redo = False                                   # this photo is an outdated result being completed
        try:
            if not survey and finished(client, out, sha):
                tally["already done"] += 1
                storage_in_a_row = 0
                continue
            if survey:
                rgb = photo.decode(_get(client, key))
                tally[_survey(rgb, m) if info["format"] == "slab" else info["format"]] += 1
            else:
                redo = _exists(client, f"{out}{sha}.main.jpg")     # made, yet not finished: outdated, to complete
                source_key, cleaned, refused = start(client, out, key, sha)
                if refused:
                    if redo:
                        tally[complete(client, out, sha, photo.Result(held=refused))] += 1
                    else:
                        client.put_object(Bucket=S3_BUCKET, Key=f"{out}held/{sha}.json", ContentType="application/json",
                                          Body=json.dumps({"reason": refused, "kind": ""}, sort_keys=True).encode())
                        tally["held"] += 1
                    storage_in_a_row = 0
                    continue
                full = photo.read(_get(client, source_key))
                logo = watermarked and not cleaned             # a cleaned copy has no logo left to take off
                clean = photo.decode(_get(client, f"{imagestore.improved_prefix(src)}{sha}.jpg")) if logo else None
                res = photo.standardise(full, fmt=info["format"], key=info["key"], size=tuple(info["size"]) if info["size"] else None,
                                        clean=clean, watermarked=logo, models=m, extra_views=extra,
                                        edits=lambda: start_edits(client, out, key, source_key, cleaned, full))
                if redo:
                    tally[complete(client, out, sha, res)] += 1
                elif res.held:
                    client.put_object(Bucket=S3_BUCKET, Key=f"{out}held/{sha}.json", ContentType="application/json",
                                      Body=json.dumps({"reason": res.held, "kind": res.kind}, sort_keys=True).encode())
                    tally["held"] += 1
                else:
                    if res.slab:
                        client.put_object(Bucket=S3_BUCKET, Key=f"{out}{sha}.slab.png", Body=_slab_png(res.slab), ContentType="image/png")
                    # Written last, so "main" means this photo is complete.
                    for name, img in sorted(res.images.items(), key=lambda kv: kv[0] == "main"):
                        client.put_object(Bucket=S3_BUCKET, Key=f"{out}{sha}.{name}.jpg", Body=_encode(img), ContentType="image/jpeg")
                    tally[res.kind] += 1
            storage_in_a_row = 0
        except Exception as exc:                       # one bad photo never stops the slice (after_failure)
            tally["failed"] += 1
            storage_in_a_row = storage_in_a_row + 1 if passing_storage_error(exc) else 0
            crashed = f"{out}crashed/{sha}.json"
            action = after_failure(exc, storage_in_a_row, not survey and gpu_error(exc) and _exists(client, crashed))
            print(f"   FAILED {sha[:12]} ({action}): {type(exc).__name__}: {exc}", flush=True)
            if action == "stop":
                if gpu_error(exc) and not survey:      # marked, so the same photo crashing it again is held, not retried
                    client.put_object(Bucket=S3_BUCKET, Key=crashed, ContentType="application/json",
                                      Body=json.dumps({"error": f"{type(exc).__name__}: {exc}"[:500]}).encode())
                print(f"==> {src}: slice stopped, the next run retries its photos", flush=True)
                return 1
            if action == "hold" and not survey:
                why = f"error: {type(exc).__name__}: {exc}"[:500]
                if redo:                               # a judged result is never held over: tried, not run again
                    tried(client, out, sha, why)
                else:
                    client.put_object(Bucket=S3_BUCKET, Key=f"{out}held/{sha}.json", ContentType="application/json",
                                      Body=json.dumps({"reason": why, "kind": ""}).encode())
        spent.append(time.time() - started)
        if i % 10 == 0:
            print(f"   [{i + 1}/{len(sliced)}] {dict(tally)} median {np.median(spent):.1f}s per photo", flush=True)
    summary = {"source": src, "survey": survey, "counts": dict(tally), "photos": len(sliced),
               "median_seconds": round(float(np.median(spent)), 1) if spent else None}
    name = "survey" if survey else "run"
    part = os.path.basename(os.environ.get("ONLY_KEY", "")).removesuffix(".json") or str(offset)    # one summary per slice
    client.put_object(Bucket=S3_BUCKET, Key=f"{out}{name}-{part}.json", Body=json.dumps(summary, sort_keys=True).encode(), ContentType="application/json")
    print(f"==> {src} done: {json.dumps(summary, sort_keys=True)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
