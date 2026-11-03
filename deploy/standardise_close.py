"""Make the close-ups of the chosen photos again (RUN_MODE=standardise-close), on the GPU, without standardising them again;
submitted by deploy.standardise_run with CLOSE_ONLY=true, one job per slice of a source's chosen photos.

For each chosen photo of SRC (deploy.standardise_select's records under <CLEAN_TAG>/chosen/), narrowed to the slice's
fixed list (ONLY_KEY): a photo its run made a whole slab of (<sha>.slab.png: a single slab, or an open book's half,
which <sha>.slab.json records; a result made before that was recorded is decided against that slab.png) goes, from the photo
its run started from, through only what the close-up needs (logo, outline, detector, eraser: photo.close_view; no
upscaler, inpainting, judge or hosted edit). The new close-up replaces <prefix><sha>.close.jpg in the run's own folder; the file is deleted
when the photo gets none: by the close-up rule, for a reason of its own (photo.NoCloseUp: its run refused it, it is off
the product sheet, one of its own inputs is missing or no image, it no longer gives the slabs its run found, its logo
cannot be trusted), because a storage error on it persisted over CLOSEUP_RETRY_MAX jobs, or because its run made no whole single
slab of it. Each photo done is recorded (with the reason when it has none) under <prefix>close_v<CLOSEUP_VERSION>/, so
a re-run goes on where it stopped and a new close-up rule does every photo again; deploy.standardise_publish then
publishes it, or takes the published one down.

What is not the photo's own outcome changes nothing: a passing storage error leaves the photo to the next job (counted
once per job, whatever Batch's retries), a broken GPU stops the slice (a photo that breaks it in two jobs has none), any
other error stops the slice loud, and so do CLOSEUP_HOLD_STREAK_MAX photos in a row with no close-up for one kind of
reason (a cause outside the photos, not those photos; none of them is written; photos off the product sheet are a
legitimate outcome en masse and never count). The bookkeeping that bounds this (retry records, crash markers) stops the
slice when it cannot be read or written. Exit 1 when photos are left to the next run.
Env: CLEAN_TAG, SRC, WATERMARKED (required: env.job_flag), ONLY_KEY (an S3 JSON list of shas); AWS_BATCH_JOB_ID names
the job (the same over Batch's attempts at it).
"""

from __future__ import annotations

import collections
import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

import boto3

from deploy.standardise_publish import _objects, chosen_prefix
from deploy.standardise_source import (_encode, _get, gpu_error, own_input, passing_storage_error, read_slab,
                                       sheet_photos, slab_record_key, start, start_edits)
from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET, S3_REGION, STANDARDISE as C
from stone_pipeline.core import env
from stone_pipeline.io import imagestore
from stone_pipeline.standardise import models, photo

WORKERS = 32                 # parallel reads of the choice records
PROGRESS_EVERY = 25          # photos between progress lines
NO_CLOSE_UP = "the close-up rule gives this photo none"
NO_SLAB = "its run made no whole single slab of it"
OFF_SHEET = "the photo is no longer on the product sheet"


class _Stop(Exception):
    """The slice stops: what failed is no outcome of a photo."""


def done_key(record):
    """Where it is recorded that this photo's close-up was made again under the current close-up rule."""
    return f"{record['prefix']}close_v{C.CLOSEUP_VERSION}/{record['sha']}.json"


def crashed_key(record):
    """Where it is recorded that this photo broke the GPU, and in which job."""
    return f"{record['prefix']}close_v{C.CLOSEUP_VERSION}/crashed/{record['sha']}.json"


def retries_key(record):
    """How many jobs a passing storage error has left this photo to the next one, and the last of them."""
    return f"{record['prefix']}close_v{C.CLOSEUP_VERSION}/retries/{record['sha']}.json"


def summary_key(root, src, only):
    """One slice's summary: its source and its photo list, so no two slices write the same one."""
    return f"{root}close-v{C.CLOSEUP_VERSION}-{src}-{os.path.basename(only).removesuffix('.json') or 'all'}.json"


def chosen(client, root):
    """Every choice record under the tag root."""
    with ThreadPoolExecutor(WORKERS) as pool:
        return list(pool.map(lambda k: json.loads(_get(client, k)), sorted(_objects(client, chosen_prefix(root)))))


def undone(client, records):
    """The records whose close-up is not made yet under the current rule (no done_key): one listing per run folder."""
    done = {k for prefix in sorted({r["prefix"] for r in records})
            for k in _objects(client, f"{prefix}close_v{C.CLOSEUP_VERSION}/")}
    return [r for r in records if done_key(r) not in done]


def listed(records, src, wanted=None):
    """The chosen photos of `src`, each once (listings can share a photo), in a stable order, in `wanted` (shas) when
    given: built on the choice alone, which this run never changes, so a list never shifts under it."""
    keep = None if wanted is None else set(wanted)
    found = {(r["prefix"], r["sha"]): r for r in records if r["source"] == src and (keep is None or r["sha"] in keep)}
    return sorted(found.values(), key=lambda r: (r["sha"], r["prefix"]))


def remake(client, record, photos, m, watermarked, made):
    """The new close-up of one chosen photo, or None when it gets none; NoCloseUp for a reason of its own. Of the slab its
    run recorded (slab_record_key, in `made`: the run folder's keys); a result made before that was recorded is decided
    against the <sha>.slab.png it saved (photo.run_slab)."""
    src, sha, out = record["source"], record["sha"], record["prefix"]
    key = f"{imagestore.scraped_prefix(src)}{sha}.jpg"
    source_key, cleaned, refused = start(client, out, key, sha)
    if refused:
        raise photo.NoCloseUp(f"its run refused the photo: {refused}")
    if sha not in photos:
        raise photo.NoCloseUp(OFF_SHEET)
    size = photos[sha]["size"]
    logo = watermarked and not cleaned                 # a cleaned copy has no logo left to take off
    clean = own_input(client, f"{imagestore.improved_prefix(src)}{sha}.jpg", photo.decode) if logo else None
    full = own_input(client, source_key)
    chosen = saved = None
    if (slab_key := slab_record_key(out, sha)) in made:
        recorded = json.loads(_get(client, slab_key))
        chosen = (recorded["index"], recorded["of"])
    else:
        saved = own_input(client, f"{out}{sha}.slab.png", read_slab)
    return photo.close_view(full, size=tuple(size) if size else None, clean=clean, watermarked=logo, models=m,
                            edits=start_edits(client, out, key, source_key, cleaned, full), chosen=chosen, saved=saved)


def store(client, record, image, reason=""):
    """The run's close-up of this photo replaced, or deleted when it has none (reason: why); then the record that it is
    done (written last, so a photo stopped half way is done again)."""
    key = f"{record['prefix']}{record['sha']}.close.jpg"
    if image is None:
        client.delete_object(Bucket=S3_BUCKET, Key=key)
    else:
        client.put_object(Bucket=S3_BUCKET, Key=key, Body=_encode(image), ContentType="image/jpeg")
    client.put_object(Bucket=S3_BUCKET, Key=done_key(record), ContentType="application/json",
                      Body=json.dumps({"close": image is not None, "reason": reason}, sort_keys=True).encode())


class _Slice:
    """One job's pass over its slice: the outcomes; the photos in a row with no close-up for one kind of reason, written
    once a different outcome follows (CLOSEUP_HOLD_STREAK_MAX of them stop the slice unwritten); the passing storage
    errors in a row (SLICE_MAX_STORAGE_FAILURES stop it). job: this job's id, so Batch's retry of it counts nothing
    twice."""

    def __init__(self, client, made, job):
        self.client, self.made, self.job = client, made, job
        self.tally = collections.Counter()
        self.streak = []                               # [(record, reason)]
        self.storage_in_a_row = 0

    def _book(self, what, act):
        """Bookkeeping that bounds the retries (retry records, crash markers): the slice stops when it fails."""
        try:
            return act()
        except Exception as exc:                       # classified: without its record the run cannot go on safely
            raise _Stop(f"{what} failed: {type(exc).__name__}: {exc}") from exc

    def _record_of(self, key):
        return self._book(f"reading {key}", lambda: json.loads(_get(self.client, key))) if key in self.made else None

    def write(self, record, image, reason, outcome):
        """Store one photo's outcome; a passing storage error leaves it to the next job, any other stops the slice."""
        try:
            store(self.client, record, image, reason)
        except Exception as exc:                       # classified: passing -> the next job, anything else -> stop
            self.storage_error(record, exc)
            return
        self.storage_in_a_row = 0
        self.tally[outcome] += 1

    def flush(self):
        """Write the photos held in a row; one held again while writing (its write failed at the retry cap) is written in
        the next round, so none is ever left out."""
        while self.streak:
            held, self.streak = self.streak, []
            for record, reason in held:
                self.write(record, None, reason, "none of its own")

    def none_of_its_own(self, record, reason):
        if reason == OFF_SHEET:                        # delisted products: a legitimate outcome en masse, never a streak
            self.flush()
            self.write(record, None, reason, "none of its own")
            return
        if self.streak and self.streak[0][1].split(":")[0] != reason.split(":")[0]:
            self.flush()
        self.streak.append((record, reason))
        if len(self.streak) >= C.CLOSEUP_HOLD_STREAK_MAX:
            raise _Stop(f"{len(self.streak)} photos in a row with no close-up: {reason.split(':')[0]}")

    def storage_error(self, record, exc):
        """A storage error on one photo: the next job tries it again, up to CLOSEUP_RETRY_MAX jobs (another attempt of this
        job counts nothing more); not a passing one stops the slice."""
        if not passing_storage_error(exc):
            raise _Stop(f"{type(exc).__name__}: {exc}") from exc
        self.storage_in_a_row += 1
        if self.storage_in_a_row >= C.SLICE_MAX_STORAGE_FAILURES:
            raise _Stop(f"{self.storage_in_a_row} storage errors in a row: {exc}") from exc
        before = self._record_of(retries_key(record))
        runs = 1 if before is None else before["runs"] + (before["job"] != self.job)
        if runs >= C.CLOSEUP_RETRY_MAX:
            self.none_of_its_own(record, f"storage kept failing over {runs} jobs: {type(exc).__name__}: {exc}"[:500])
            return
        body = json.dumps({"runs": runs, "job": self.job}, sort_keys=True).encode()
        self._book("the retry record", lambda: self.client.put_object(Bucket=S3_BUCKET, Key=retries_key(record), Body=body,
                                                                      ContentType="application/json"))
        self.tally["retry"] += 1

    def broken_gpu(self, record, exc):
        """A broken GPU stops the slice; the photo that broke it in an earlier job too is what breaks it: it has none. A
        second attempt of this job (Batch's retry, maybe the same bad host) counts nothing more."""
        before = self._record_of(crashed_key(record))
        if before is not None and before["job"] != self.job:
            self.none_of_its_own(record, f"it broke the GPU in two jobs: {type(exc).__name__}: {exc}"[:500])
            return
        body = json.dumps({"error": f"{type(exc).__name__}: {exc}"[:500], "job": self.job}, sort_keys=True).encode()
        self._book("the crash marker", lambda: self.client.put_object(Bucket=S3_BUCKET, Key=crashed_key(record), Body=body,
                                                                      ContentType="application/json"))
        raise _Stop(f"broken GPU: {type(exc).__name__}: {exc}") from exc

    def one(self, record, photos, m, watermarked):
        if f"{record['prefix']}{record['sha']}.slab.png" not in self.made:
            self.flush()
            self.write(record, None, NO_SLAB, "no close-up")
            return
        try:
            image = remake(self.client, record, photos, m, watermarked, self.made)
        except photo.NoCloseUp as exc:
            self.none_of_its_own(record, str(exc)[:500])
            return
        except Exception as exc:                       # classified: a broken GPU, storage, else the slice stops
            if gpu_error(exc):
                self.broken_gpu(record, exc)
            else:
                self.storage_error(record, exc)
            return
        self.flush()
        self.write(record, image, "" if image is not None else NO_CLOSE_UP, "close-up" if image is not None else "no close-up")


def redo(client, records, made, photos, m, watermarked, job):
    """Every listed photo's close-up made again, those done already skipped: {outcome: count}; with "stopped" when the
    slice stopped (what it had not written stays as it was)."""
    run = _Slice(client, made, job)
    try:
        for i, record in enumerate(records):
            if done_key(record) in made:
                run.tally["already done"] += 1
                continue
            run.one(record, photos, m, watermarked)
            if i % PROGRESS_EVERY == 0:
                print(f"   [{i + 1}/{len(records)}] {dict(run.tally)}", flush=True)
        run.flush()
    except _Stop as stop:
        print(f"==> slice stopped: {stop}", flush=True)
        run.tally["stopped"] += 1
    return dict(run.tally)


def main() -> int:
    tag, src = os.environ.get("CLEAN_TAG", "").strip(), os.environ.get("SRC", "").strip()
    if not tag or not src:
        print("ERROR: CLEAN_TAG and SRC are required; aborting")
        return 2
    try:
        watermarked = env.job_flag("WATERMARKED")
    except ValueError as exc:
        print(f"ERROR: {exc}; aborting")
        return 2
    only = os.environ.get("ONLY_KEY", "").strip()
    job = os.environ.get("AWS_BATCH_JOB_ID", "").strip() or f"local-{uuid.uuid4()}"     # outside Batch: one job per run
    client = boto3.client("s3", region_name=S3_REGION)
    root = f"{ENV_SEGMENT}/{C.TEST_PREFIX}/{tag}/"
    records = chosen(client, root)
    if not records:
        print(f"ERROR: no choice records under {chosen_prefix(root)}; aborting")
        return 2
    whole = listed(records, src, json.loads(_get(client, only)) if only else None)
    made = {k for prefix in sorted({r["prefix"] for r in whole}) for k in _objects(client, prefix)}
    todo = sum(done_key(r) not in made for r in whole)
    print(f"==> close-ups v{C.CLOSEUP_VERSION} of {tag}/{src} (job {job}): {len(whole)} photos, {todo} to do", flush=True)
    tally = redo(client, whole, made, sheet_photos(client), models.load("close"), watermarked, job) if todo else {"already done": len(whole)}
    summary = {"tag": tag, "source": src, "version": C.CLOSEUP_VERSION, "job": job, "photos": len(whole), "counts": tally}
    client.put_object(Bucket=S3_BUCKET, Key=summary_key(root, src, only), Body=json.dumps(summary, sort_keys=True).encode(),
                      ContentType="application/json")
    print(f"==> done: {json.dumps(summary, sort_keys=True)}", flush=True)
    return 1 if tally.get("retry") or tally.get("stopped") else 0


if __name__ == "__main__":
    sys.exit(main())
