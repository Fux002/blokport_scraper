"""Standardise a source's scraped photos (RUN_MODE=standardise), on the GPU: reads <env>/products/scraped/<src>/, writes
finished images + the clean slab under <env>/standardise-test/<src>/, never into improved/ (going live is a later step).
Env: SRC; SURVEY=true only classifies every photo and writes the counts; SAMPLE=<n> takes n photos, the same n on every
run (the lowest hashes); ONLY_KEY=<s3 key of a JSON list of shas> runs just those; SLICE_OFFSET / SLICE_COUNT run that
contiguous slice of the source's photos. A photo already done is skipped, so a re-run is safe. One bad photo is recorded and never stops
the slice.
"""

from __future__ import annotations

import collections
import json
import os
import sys
import time

import cv2
import numpy as np
from botocore.exceptions import BotoCoreError, ClientError

from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET
from stone_pipeline.config.standardise_settings import STANDARDISE as C
from stone_pipeline.io import imagestore
from stone_pipeline.io.storage import BOTOCORE_DEFAULTS, aws_client
from stone_pipeline.standardise import index, kinds, models, outline, photo


def _encode(rgb, quality=C.JPEG_QUALITY):
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


def read_slab(data):
    """(RGB, alpha 0..1) of a saved <sha>.slab.png (_slab_png); ValueError when it is no slab PNG."""
    bgra = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    if bgra is None or bgra.ndim != 3 or bgra.shape[2] != 4:
        raise ValueError("no slab PNG: an image with an alpha channel")
    return cv2.cvtColor(bgra[..., :3], cv2.COLOR_BGR2RGB), bgra[..., 3].astype(np.float32) / 255


def slab_record_key(out, sha):
    """Where it is recorded which of the slabs found in a photo its <sha>.slab.png is (photo.Result.chosen): what the
    close-only run makes its close-up of."""
    return f"{out}{sha}.slab.json"


LOGO_STEP = "logo-step/"


def logo_step_key(out, sha):
    """The marker of a result the retired logo step made (stone_pipeline.photos.mark_logo_step, once at that release's deploy):
    a re-run would start from the photo with its logo and add views no judge saw, so such a result is never completed
    (outdated) and its close-up never made again (stone_pipeline.photos.close), whatever the close-up rule. Per photo:
    data, never a source setting."""
    return f"{out}{LOGO_STEP}{sha}.json"


def _get(client, key):
    return client.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()


def _exists(client, prefix):
    return client.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix, MaxKeys=1).get("KeyCount", 0) > 0


def outdated(client, out, sha):
    """A result made before an open book's halves had their slab: a side view (studio) and no <sha>.slab.png, not tried
    again before (noslab/), not made by the retired logo step (logo_step_key), and not settled by an outline recheck
    (its note: a redraw the judge failed keeps it as it is) nor waiting for the judge (pending). Every result made now
    that has a side view has its slab, so nothing else matches."""
    return (_exists(client, f"{out}{sha}.studio.jpg") and not _exists(client, f"{out}{sha}.slab.png")
            and not _exists(client, f"{out}noslab/{sha}.json") and not _exists(client, logo_step_key(out, sha))
            and not _exists(client, checked_key(out, sha)) and not pending(client, out, sha))


def checked_key(out, sha, rule=None):
    """The note that this photo's whole slab is drawn under an outline rule (its folder: `rule`, the current one,
    C.OUTLINE_CHECKED, when not given)."""
    return f"{out}{rule or C.OUTLINE_CHECKED}{sha}.json"


REDRAW = "redraw/"
# The rules whose candidates the judge decides now (undecided, select.settle_one): the outline recheck's and a scoped
# run's re-standardise (C.RESTANDARDISED). A candidate under any other rule was drawn under an earlier one.
JUDGED_RULES = (C.OUTLINE_CHECKED, C.RESTANDARDISED)


def candidate_prefix(out):
    """Where a recheck draws a result this version draws differently (a candidate) instead of over the result: a folder
    named as a run folder is, <sha>.<file> and judged/<sha>.json, so the judge reads it as it reads a result."""
    return f"{out}{REDRAW}"


def candidate_key(out, sha):
    """A candidate's completion marker, written last: without it the candidate is cut short, never judged."""
    return f"{candidate_prefix(out)}{sha}.done.json"


def pending(client, out, sha):
    """A complete candidate of this photo waits for the judge (stone_pipeline.photos.select.settle)."""
    return _exists(client, candidate_key(out, sha))


def older_outline(client, out, sha):
    """A whole slab made under the outline rule before C.OUTLINE_CHECKED: its slab, no note of the current rule and no
    complete candidate waiting for the judge, not made by the retired logo step (logo_step_key: a re-run would start
    from the photo with its logo and redraw views no judge saw)."""
    return (_exists(client, f"{out}{sha}.slab.png") and not _exists(client, checked_key(out, sha))
            and not pending(client, out, sha) and not _exists(client, logo_step_key(out, sha)))


def made(client, out, sha):
    """This photo's run already ended once, with a result or held, whether or not that result is worked on again (an
    outdated one, a recheck): the input it was made from (start) is fixed from then on."""
    return _exists(client, f"{out}{sha}.main.jpg") or _exists(client, f"{out}held/{sha}.json")


def finished(client, out, sha):
    """This photo's run already ended, finished or held: a re-run, or results copied in from another environment's
    run of the same photo, is never worked on again; only an outdated result is, once (complete), and a whole slab made
    under an older outline rule, once (recheck)."""
    if _exists(client, f"{out}held/{sha}.json"):
        return True
    return (_exists(client, f"{out}{sha}.main.jpg") and not outdated(client, out, sha)
            and not older_outline(client, out, sha))


def restandardise_due(client, out, sha):
    """Whether a photo on a scoped run's re-standardise list (RESTANDARDISE_KEY) is still to be re-standardised: it has
    a result, no note of C.RESTANDARDISED (written when its redraw is settled or gave no slab) and no complete candidate
    waiting for the judge. So it is re-done once, and a run cut short before its candidate was complete re-does it."""
    return (_exists(client, f"{out}{sha}.main.jpg") and not noted(client, out, sha, C.RESTANDARDISED)
            and not pending(client, out, sha))


def again(client, out, sha):
    """How a stored result that is not finished is worked on again: "complete" (an open book made before its halves had a
    slab) or "recheck" (a whole slab made under an older outline rule); None for a photo without a result."""
    if not _exists(client, f"{out}{sha}.main.jpg"):
        return None
    return "complete" if outdated(client, out, sha) else "recheck"


def _files(res):
    """[(name, bytes, content type)] of a result's files, every one encoded before anything is written: for a whole slab,
    which of the slabs found it is and what it is made from (slab.json: slab_record_key; photo.Result.source) and then
    the slab, marking a whole slab; its views, main last (main means the photo is complete)."""
    files = []
    if res.slab:
        record = {"index": res.chosen[0], "of": res.chosen[1], **res.source}
        files = [("slab.json", json.dumps(record, sort_keys=True).encode(), "application/json"),
                 ("slab.png", _slab_png(res.slab), "image/png")]
    return files + [(f"{name}.jpg", _encode(img), "image/jpeg")
                    for name, img in sorted(res.images.items(), key=lambda kv: kv[0] == "main")]


def _put(client, out, sha, files):
    for name, body, kind in files:
        client.put_object(Bucket=S3_BUCKET, Key=f"{out}{sha}.{name}", Body=body, ContentType=kind)


def _views_not_in(client, out, sha, names):
    """The photo's stored views that `names` (its new files) do not have: a stale close view would be published with the
    new ones."""
    return [f"{out}{sha}.{view}.jpg" for view in imagestore.STANDARD_VIEWS
            if f"{view}.jpg" not in names and _exists(client, f"{out}{sha}.{view}.jpg")]


def write(client, out, sha, res):
    """A new result written whole: its files (_files), the views an earlier result of the photo had and this one does
    not make removed, then, for a whole slab, the note {"new": true}: it is drawn under the current outline rule
    (written last: missing, the slab is checked once more, never wrong)."""
    files = _files(res)
    _put(client, out, sha, files)
    for key in _views_not_in(client, out, sha, {name for name, _, _ in files}):
        client.delete_object(Bucket=S3_BUCKET, Key=key)
    if res.slab:
        _note(client, out, sha, {"new": True})


def _note(client, out, sha, note, rule=None):
    client.put_object(Bucket=S3_BUCKET, Key=checked_key(out, sha, rule), ContentType="application/json",
                      Body=json.dumps(note, sort_keys=True).encode())


def _keys(client, prefix):
    return sorted(o["Key"] for page in client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix)
                  for o in page.get("Contents", []))


def _candidate_files(client, out, sha):
    """The keys of the photo's candidate: its files and its verdict, not its completion marker."""
    verdict = f"{candidate_prefix(out)}judged/{sha}.json"
    return [k for k in _keys(client, f"{candidate_prefix(out)}{sha}.") if k != candidate_key(out, sha)] + \
        ([verdict] if _exists(client, verdict) else [])


def clear_candidate(client, out, sha):
    """A candidate cut short (no completion marker: a photo with a complete one is not worked on again) ignored and
    removed before the photo is worked on again."""
    for key in _candidate_files(client, out, sha):
        client.delete_object(Bucket=S3_BUCKET, Key=key)


def draw_candidate(client, out, sha, res, rule=C.OUTLINE_CHECKED):
    """A result this version draws differently written as a candidate (candidate_prefix), never over the result: every
    file encoded first, then written as a result's are, then its completion marker, last, naming its files and the rule
    it was drawn under (`rule`: the outline recheck's, or a scoped run's re-standardise)."""
    files = _files(res)
    _put(client, candidate_prefix(out), sha, files)
    marker = {"files": [[name, kind] for name, _, kind in files], "rule": rule}
    client.put_object(Bucket=S3_BUCKET, Key=candidate_key(out, sha), ContentType="application/json",
                      Body=json.dumps(marker, sort_keys=True).encode())


def candidate_rule(client, out, sha):
    """The outline rule a complete candidate was drawn under (its marker): it is settled under that rule."""
    return json.loads(_get(client, candidate_key(out, sha)))["rule"]


def noted(client, out, sha, rule):
    """Whether this photo's result has the note of `rule` (checked_key)."""
    return _exists(client, checked_key(out, sha, rule))


def undecided(client, out, sha):
    """Whether the judge still decides this complete candidate: drawn under the current rule and not settled yet (no
    note of its rule). One drawn under an earlier rule is never judged: it is settled by its stored verdict alone."""
    rule = candidate_rule(client, out, sha)
    return rule in JUDGED_RULES and not noted(client, out, sha, rule)


def promote(client, out, sha, rule):
    """A candidate the judge passed made the result, safe to cut short and run again (each step is the same copy or
    removal again): its files copied over the result's (which slab it is and the slab, its views, main last), the views
    it does not make removed, its verdict copied over the result's, the note {"changed": true} of the rule it was drawn
    under, then the candidate removed (remove_candidate). Publish copies the new files by their ETag."""
    cand = candidate_prefix(out)
    files = json.loads(_get(client, candidate_key(out, sha)))["files"]
    for name, kind in files:
        _copy(client, f"{cand}{sha}.{name}", f"{out}{sha}.{name}", kind)
    for key in _views_not_in(client, out, sha, {name for name, _ in files}):
        client.delete_object(Bucket=S3_BUCKET, Key=key)
    _copy(client, f"{cand}judged/{sha}.json", f"{out}judged/{sha}.json", "application/json")
    _note(client, out, sha, {"changed": True}, rule)
    remove_candidate(client, out, sha)


def _copy(client, source, target, kind):
    client.copy_object(Bucket=S3_BUCKET, Key=target, CopySource={"Bucket": S3_BUCKET, "Key": source}, ContentType=kind,
                       MetadataDirective="REPLACE")


def discard(client, out, sha, reason, rule):
    """A candidate set aside: the note of the rule it was drawn under that the result is kept as it is ({"changed":
    false, "kept": why}: never redrawn or judged again under that rule), then the candidate removed. The result, its
    verdict, its choice and what is published of it are never touched."""
    _note(client, out, sha, {"changed": False, "kept": reason}, rule)
    remove_candidate(client, out, sha)


def remove_candidate(client, out, sha):
    """A settled candidate (the note written) removed: its files and verdict, its completion marker last, so a removal
    cut short is finished by the next settle."""
    for key in _candidate_files(client, out, sha) + [candidate_key(out, sha)]:
        client.delete_object(Bucket=S3_BUCKET, Key=key)


def rework(client, out, sha, res, how):
    """A stored result worked on again (how: again's "complete" or "recheck", or "restandardise": a scoped run's listed
    photo, restandardise_due), one path for all, a candidate cut short before cleared first (clear_candidate):
      - drawn the same by this version (a recheck stops before drawing): only the note, {"changed": false};
      - no whole slab on the re-run (held, a close-up, or a completion that is no open book this time): tried;
      - drawn differently (res.changed: the wall in a corner, the sawn edge beside a broken corner), or re-standardised
        whatever the outline says (the fix it is for may be one drawn_differently cannot see): a candidate
        (draw_candidate, under C.RESTANDARDISED for a re-standardise), never over the result: the judge scores it as
        it scores a new result, and it is promoted or discarded (stone_pipeline.photos.select.settle);
      - a completion drawn the same: what was judged (main, white, studio) stays; its close view, then which slab it is
        and its slab (the result complete), then the note {"changed": false}. Cut short, the close view comes off again
        while the slab is missing, so half of one is never published."""
    clear_candidate(client, out, sha)
    if res.unchanged:
        _note(client, out, sha, {"changed": False})
        return "outline unchanged"
    if res.held or not res.slab or (how == "complete" and res.kind != "open book"):
        wanted = "open book" if how == "complete" else "whole slab"
        tried(client, out, sha, how, res.held or f"the re-run is a {res.kind}, no {wanted}")
        return "no slab"
    if how == "restandardise":
        draw_candidate(client, out, sha, res, C.RESTANDARDISED)
        return "redrawn for the judge"
    if res.changed:
        draw_candidate(client, out, sha, res)
        return "redrawn for the judge"
    by_name = {f[0]: f for f in _files(res)}
    files = [by_name[n] for n in ("close.jpg", "slab.json", "slab.png") if n in by_name]   # the slab last: complete
    try:
        _put(client, out, sha, files)
    except BaseException:
        if not _exists(client, f"{out}{sha}.slab.png"):
            client.delete_object(Bucket=S3_BUCKET, Key=f"{out}{sha}.close.jpg")
        raise
    _note(client, out, sha, {"changed": False})
    return "completed"


def tried(client, out, sha, how, reason):
    """Note that a result worked on again gave no slab, or failed, so it is never run again and stays as it is: a
    completion in noslab/<sha>.json, a recheck in its note ({"changed": false, "kept": why}: never noslab/ over a live
    result), a re-standardise in the note of its rule (C.RESTANDARDISED). Never held/: the result is judged and may be
    published, and a hold would take it from every listing that has it."""
    if how in ("recheck", "restandardise"):
        rule = C.RESTANDARDISED if how == "restandardise" else None
        _note(client, out, sha, {"changed": False, "kept": reason}, rule)
        return
    client.put_object(Bucket=S3_BUCKET, Key=f"{out}noslab/{sha}.json", ContentType="application/json",
                      Body=json.dumps({"reason": reason}, sort_keys=True).encode())


def photo_source(client, out, key, sha):
    """The photo a run works from: its cleaned copy when a hosted edit made one (stone_pipeline.photos.select: reflections,
    marks and logos off), else the scraped photo. A cleaned copy goes through every step a scraped photo does (the hosted
    edit leaves the straps, which the retouch takes off)."""
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
    from stone_pipeline.io import medusa_inputs
    from stone_pipeline.config import categories
    medusa_inputs.fetch_attributes(client)
    categories.refresh_category_pcats()
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


def _defaulted_sizes(client):
    """The handles whose length or width the produce defaulted (STANDARDISE.DEFAULTED_KEY, beside the photo list), read
    once per job. Present but unreadable, it fails loud. Absent means none defaulted ONLY because the producer before #605
    wrote no such file (grade 2). Remover: drop the absent branch once both envs' to_upload come from a produce that
    writes it (the first full produce on dev and prod after #605 merges)."""
    key = f"{ENV_SEGMENT}/{C.DEFAULTED_KEY}"
    try:
        text = _get(client, key).decode("utf-8")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return frozenset()
        raise
    try:
        return index.parse_defaulted(text)
    except ValueError as exc:
        raise RuntimeError(f"the defaulted-sizes list s3://{S3_BUCKET}/{key} is malformed: {exc}") from exc


def sheet_photos(client):
    """{sha: {source, key, format, size}} of this environment's product sheet, its photo list and its defaulted sizes (a
    defaulted size reads None: unknown)."""
    return index.build(_sheet(client), _photo_list(client), _defaulted_sizes(client))


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


def listed_shas(client, name="ONLY_KEY"):
    """The shas the S3 JSON list named by env `name` holds (ONLY_KEY: a slice of photos; RESTANDARDISE_KEY: those of
    the slice to re-standardise, batch.submit), or None when it is not set."""
    listed = os.environ.get(name, "").strip()
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
    extra = os.environ.get("EXTRA_VIEWS", "true").lower() in ("1", "true", "yes")
    offset = int(os.environ.get("SLICE_OFFSET", "0"))
    client = aws_client("s3", config=BOTOCORE_DEFAULTS)
    out = run_prefix(src)
    photos = sheet_photos(client)
    sliced = _photo_keys(client, src, photos)
    forced = set(listed_shas(client, "RESTANDARDISE_KEY") or ())   # a scoped run's photos to re-standardise once
    print(f"==> standardise {src}: {len(sliced)} photos, survey={survey}", flush=True)
    m = models.load("survey" if survey else "all")
    tally = collections.Counter()
    spent = []
    storage_in_a_row = 0
    for i, key in enumerate(sliced):
        sha = imagestore.sha_from_url(key)
        info = photos[sha]
        started = time.time()
        redo = None                                    # complete or recheck: a result there, worked on again
        try:
            restandardise = not survey and sha in forced and restandardise_due(client, out, sha)
            if not survey and not restandardise and finished(client, out, sha):
                tally["already done"] += 1
                storage_in_a_row = 0
                continue
            if survey:
                rgb = photo.decode(_get(client, key))
                tally[_survey(rgb, m) if info["format"] == "slab" else info["format"]] += 1
            else:
                redo = "restandardise" if restandardise else again(client, out, sha)
                source_key, cleaned, refused = start(client, out, key, sha)
                if refused:
                    if redo:
                        tally[rework(client, out, sha, photo.Result(held=refused), redo)] += 1
                    else:
                        client.put_object(Bucket=S3_BUCKET, Key=f"{out}held/{sha}.json", ContentType="application/json",
                                          Body=json.dumps({"reason": refused, "kind": ""}, sort_keys=True).encode())
                        tally["held"] += 1
                    storage_in_a_row = 0
                    continue
                full = photo.read(_get(client, source_key))
                res = photo.standardise(full, fmt=info["format"], key=info["key"], size=tuple(info["size"]) if info["size"] else None,
                                        models=m, extra_views=extra,
                                        edits=lambda: start_edits(client, out, key, source_key, cleaned, full), redo=redo)
                if redo:
                    tally[rework(client, out, sha, res, redo)] += 1
                elif res.held:
                    client.put_object(Bucket=S3_BUCKET, Key=f"{out}held/{sha}.json", ContentType="application/json",
                                      Body=json.dumps({"reason": res.held, "kind": res.kind}, sort_keys=True).encode())
                    tally["held"] += 1
                else:
                    write(client, out, sha, res)
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
                    clear_candidate(client, out, sha)
                    tried(client, out, sha, redo, why)
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
