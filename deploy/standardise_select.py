"""Choose each listing's one good photo (user 2026-11-01: one very good image per listing is enough; it gives every view).
Hosted calls only, no model runs here. Two run tags take part: RUN_TAG, the standardiser's run over the scraped photos,
and CLEAN_TAG, its run over the copies a hosted edit model cleaned. Every answer is stored next to the image it is about,
so a re-run reads it back and never pays twice; one listing's failure is reported and never stops the others.
PHASE:
  judge   per listing, its finished photos in its own order (RUN_TAG first, then CLEAN_TAG) go to the judge until one
          passes, at most JUDGE_MAX_PER_LISTING per tag; the verdict is kept at <tag>/<src>/judged/<sha>.json
  clean   per listing with no pass, the first photo a cleaned copy can fix (failed only for reflections or marks, or held
          by the retouch or logo step) is edited by each CLEAN_MODELS model in turn until a copy passes the clean check;
          that copy becomes <CLEAN_TAG>/<src>/cleaned/<sha>.jpg, and the photos still to standardise are listed in
          <CLEAN_TAG>/todo.json (the GPU run's ONLY_KEY)
  report  from the stored answers only: <CLEAN_TAG>/chosen.json (what is published) and <CLEAN_TAG>/tracked.csv (every
          listing with no good photo, with what happened to each of its photos)
Env: PHASE, RUN_TAG, CLEAN_TAG, SRC (space-separated sources); LISTINGS_KEY (optional) = S3 key of a JSON list of listing
keys to work on; FAL_KEY. Hosted spend stops loud at SPEND_CAP_USD (exit 3).
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import boto3

from deploy.standardise_publish import EXCLUDE_PATH, read_exclude
from deploy.standardise_source import round_prefix, sheet_listings
from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET, S3_REGION, STANDARDISE as C
from stone_pipeline.io import imagestore
from stone_pipeline.standardise import clean, judge

_SHA = re.compile(r"[0-9a-f]{64}")


class SpendCapReached(RuntimeError):
    pass


class Spend:
    """Hosted spend of this run: a call's price is reserved before it is made and settled to what it cost, so parallel
    calls can never together pass the cap."""

    def __init__(self, cap):
        self.cap, self.spent, self.reserved = cap, 0.0, 0.0
        self._lock = threading.Lock()

    def reserve(self, usd):
        with self._lock:
            if self.spent + self.reserved + usd > self.cap:
                raise SpendCapReached(f"hosted spend would pass the cap of ${self.cap:.2f} (spent ${self.spent:.2f})")
            self.reserved += usd

    def settle(self, reserved, cost):
        with self._lock:
            self.reserved -= reserved
            self.spent += cost


class Round:
    """What one run tag holds for one source, its keys listed once: finished photos, held ones, verdicts, cleaned copies
    and clean attempts ("<sha>.<model>": its check, and its edited copy)."""

    def __init__(self, client, tag, src):
        self.prefix = round_prefix(tag, src)
        keys = [o["Key"][len(self.prefix):] for page in client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=self.prefix)
                for o in page.get("Contents", [])]
        self.finished = {k[:64] for k in keys if k.endswith(".main.jpg") and "/" not in k}
        self.held = _shas(keys, "held/")
        self.judged = _shas(keys, "judged/")
        self.cleaned = _shas(keys, "cleaned/")
        self.attempts = {k[len("attempts/"):-len(".json")] for k in keys if k.startswith("attempts/") and k.endswith(".json")}
        self.edited = {k[len("attempts/"):-len(".jpg")] for k in keys if k.startswith("attempts/") and k.endswith(".jpg")}


def _shas(keys, folder):
    return {m.group(0) for k in keys if k.startswith(folder) and (m := _SHA.match(k[len(folder):]))}


def _get(client, key):
    return client.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()


def _get_json(client, key):
    return json.loads(_get(client, key))


def _put(client, key, body, content_type):
    client.put_object(Bucket=S3_BUCKET, Key=key, Body=body, ContentType=content_type)


def _put_json(client, key, value):
    _put(client, key, json.dumps(value, sort_keys=True).encode(), "application/json")


def _original(client, src, sha):
    return _get(client, f"{imagestore.scraped_prefix(src)}{sha}.jpg")


def verdict(client, rnd, src, sha, fmt, spend=None):
    """The judge's stored verdict on this tag's result for the photo; judged now when there is none and spend is given,
    None when there is none and none may be bought."""
    key = f"{rnd.prefix}judged/{sha}.json"
    if sha in rnd.judged:
        return _get_json(client, key)
    if spend is None:
        return None
    spend.reserve(C.JUDGE_PRICE_USD)
    cost = 0.0
    try:
        found, cost = judge.judge(_original(client, src, sha), _get(client, f"{rnd.prefix}{sha}.main.jpg"), fmt)
    finally:
        spend.settle(C.JUDGE_PRICE_USD, cost)
    _put_json(client, key, {**found, "cost": cost})
    rnd.judged.add(sha)
    return found


def choose(client, rounds, listing, exclude, spend=None):
    """((tag, sha) of the listing's photo to publish or None, whether every photo looked at has a verdict): its finished
    photos in its own order, the standardiser's run first and then the cleaned copies' run, the first the judge passes.
    At most JUDGE_MAX_PER_LISTING per tag are looked at: one good photo is enough, more would only cost."""
    src = listing["source"]
    complete = True
    for tag, rnd in rounds:
        looked = 0
        for sha in listing["photos"]:
            if looked == C.JUDGE_MAX_PER_LISTING:
                break
            if sha in exclude or sha not in rnd[src].finished:
                continue
            looked += 1
            found = verdict(client, rnd[src], src, sha, listing["format"], spend)
            if found is None:
                complete = False
            elif judge.passed(found):
                return (tag, sha), True
    return None, complete


def fixable(client, rnd, sha, exclude):
    """Whether a cleaned copy of this photo could pass: the judge failed its result only for reflections, marks or fills,
    or the retouch or logo step held it. A photo held for its outline, or never judged, is not worth an edit."""
    if sha in exclude:
        return False
    if sha in rnd.finished:
        return sha in rnd.judged and judge.fixable(_get_json(client, f"{rnd.prefix}judged/{sha}.json"))
    if sha in rnd.held:
        return _get_json(client, f"{rnd.prefix}held/{sha}.json")["reason"].startswith(C.CLEAN_FIXES_HELD)
    return False


def clean_photo(client, cl, src, sha, spend):
    """Each model in turn until a cleaned copy passes the clean check; that copy becomes cleaned/<sha>.jpg. Returns
    whether one passed. Each attempt (the copy and its check) is kept, so a re-run goes on where this one stopped."""
    original = _original(client, src, sha)
    for name in clean.MODELS:
        att = f"{cl.prefix}attempts/{sha}.{name}"
        if f"{sha}.{name}" in cl.attempts:
            check = _get_json(client, f"{att}.json")
        else:
            if f"{sha}.{name}" in cl.edited:           # paid for before, its check not made: the copy is reused
                copy, price = _get(client, f"{att}.jpg"), 0.0
            else:
                spend.reserve(C.CLEAN_PRICE_USD[name])
                price = 0.0
                try:
                    copy = clean.edit(name, original)
                    price = C.CLEAN_PRICE_USD[name]
                finally:
                    spend.settle(C.CLEAN_PRICE_USD[name], price)
                _put(client, f"{att}.jpg", copy, "image/jpeg")
                cl.edited.add(f"{sha}.{name}")
            spend.reserve(C.JUDGE_PRICE_USD)
            judged = 0.0
            try:
                check, judged = judge.check_clean(original, copy)
            finally:
                spend.settle(C.JUDGE_PRICE_USD, judged)
            _put_json(client, f"{att}.json", {**check, "edit_model": name, "cost": price + judged})
            cl.attempts.add(f"{sha}.{name}")
        if check["passes"]:
            client.copy_object(Bucket=S3_BUCKET, Key=f"{cl.prefix}cleaned/{sha}.jpg", CopySource={"Bucket": S3_BUCKET, "Key": f"{att}.jpg"},
                               ContentType="image/jpeg", MetadataDirective="REPLACE")
            cl.cleaned.add(sha)
            return True
    return False


def clean_listing(client, rounds, listing, exclude, spend):
    """A listing with no good photo: its first CLEAN_PHOTOS_PER_LISTING fixable photos are cleaned (one is enough)."""
    picked, complete = choose(client, rounds, listing, exclude)
    if picked is not None:
        return
    if not complete:
        raise RuntimeError("not every photo looked at has a verdict: run PHASE=judge first")
    (_, gpu), (_, cl) = rounds
    src = listing["source"]
    candidates = [sha for sha in listing["photos"] if fixable(client, gpu[src], sha, exclude)][:C.CLEAN_PHOTOS_PER_LISTING]
    for sha in candidates:
        if sha in cl[src].cleaned or clean_photo(client, cl[src], src, sha, spend):
            return


def photo_notes(client, rounds, listing, exclude):
    """What happened to each of a listing's photos, for a person reading the tracked list."""
    notes = []
    src = listing["source"]
    for sha in listing["photos"]:
        parts = []
        for tag, rnd in rounds:
            r = rnd[src]
            if sha in exclude:
                parts.append("excluded after review")
                break
            if sha in r.judged:
                v = _get_json(client, f"{r.prefix}judged/{sha}.json")
                kinds = ", ".join(sorted({p.get("kind", "?") for p in v.get("problems", [])}))
                parts.append(f"{tag}: judge {v['verdict']}" + (f" ({kinds})" if kinds else ""))
            elif sha in r.finished:
                parts.append(f"{tag}: finished, not judged")
            elif sha in r.held:
                parts.append(f"{tag}: held: {_get_json(client, f'{r.prefix}held/{sha}.json')['reason']}")
            tried = [a for a in sorted(r.attempts) if a.startswith(sha)]
            if tried:
                parts.append("cleaned by " + ", ".join(a.split(".", 1)[1] for a in tried) + ("; a copy passed" if sha in r.cleaned else "; none passed"))
        notes.append(f"{sha[:12]}: " + ("; ".join(parts) or "no result"))
    return " | ".join(notes)


def _listings(client):
    wanted = os.environ.get("LISTINGS_KEY", "").strip()
    sources = set(os.environ["SRC"].split())
    keep = set(json.loads(_get(client, wanted))) if wanted else None
    return [l for l in sheet_listings(client) if l["source"] in sources and (keep is None or l["key"] in keep)]


def _each(listings, work):
    """work(listing) on every listing, WORKERS at a time: (failures, cap reached). A failure is reported and the rest go
    on; reaching the spend cap stops what has not started."""
    stop = threading.Event()
    failures = []

    def one(listing):
        if stop.is_set():
            return
        try:
            work(listing)
        except SpendCapReached as exc:
            stop.set()
            failures.append((listing["key"], str(exc)))
        except Exception as exc:                       # one listing never stops the others; the re-run retries it
            failures.append((listing["key"], f"{type(exc).__name__}: {exc}"))
    with ThreadPoolExecutor(C.SELECT_WORKERS) as pool:
        list(pool.map(one, listings))
    return failures, stop.is_set()


def main() -> int:
    phase = os.environ.get("PHASE", "").strip()
    run_tag, clean_tag = os.environ.get("RUN_TAG", "").strip(), os.environ.get("CLEAN_TAG", "").strip()
    if phase not in ("judge", "clean", "report") or not run_tag or not clean_tag or run_tag == clean_tag or not os.environ.get("SRC", "").strip():
        print("ERROR: PHASE (judge|clean|report), RUN_TAG, CLEAN_TAG (a different tag) and SRC are required; aborting")
        return 2
    client = boto3.client("s3", region_name=S3_REGION)
    exclude = read_exclude(EXCLUDE_PATH.read_text())
    listings = _listings(client)
    sources = sorted({l["source"] for l in listings})
    rounds = [(tag, {src: Round(client, tag, src) for src in sources}) for tag in (run_tag, clean_tag)]
    root = f"{ENV_SEGMENT}/{C.TEST_PREFIX}/{clean_tag}/"
    spend = Spend(C.SPEND_CAP_USD)
    print(f"==> {phase}: {len(listings)} listings of {', '.join(sources)}; {run_tag} then {clean_tag}", flush=True)
    if phase == "judge":
        failures, capped = _each(listings, lambda l: choose(client, rounds, l, exclude, spend))
    elif phase == "clean":
        failures, capped = _each(listings, lambda l: clean_listing(client, rounds, l, exclude, spend))
        todo = sorted(sha for src in sources for sha in rounds[1][1][src].cleaned
                      if sha not in rounds[1][1][src].finished and sha not in rounds[1][1][src].held)
        _put_json(client, f"{root}todo.json", todo)
        print(f"==> {len(todo)} cleaned photos to standardise: ONLY_KEY={root}todo.json RUN_TAG={clean_tag}", flush=True)
    else:
        failures, capped = [], False
        chosen, tracked, pending = [], [], []
        for l in listings:
            picked, complete = choose(client, rounds, l, exclude)
            if picked:
                tag, sha = picked
                chosen.append({"source": l["source"], "key": l["key"], "sha": sha, "tag": tag, "prefix": round_prefix(tag, l["source"])})
            elif not complete:
                pending.append(l["key"])
            else:
                tracked.append({"source": l["source"], "listing": l["key"], "format": l["format"], "photos": len(l["photos"]),
                                "what_happened": photo_notes(client, rounds, l, exclude)})
        _put_json(client, f"{root}chosen.json", chosen)
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=["source", "listing", "format", "photos", "what_happened"])
        writer.writeheader()
        writer.writerows(tracked)
        _put(client, f"{root}tracked.csv", out.getvalue().encode(), "text/csv")
        by_tag = {tag: sum(c["tag"] == tag for c in chosen) for tag in (run_tag, clean_tag)}
        print(f"==> {len(chosen)} listings with a good photo {by_tag}, {len(tracked)} without (s3://{S3_BUCKET}/{root}tracked.csv)", flush=True)
        if pending:
            failures = [(key, "not judged yet: run PHASE=judge") for key in pending]
    print(f"==> hosted spend this run ${spend.spent:.2f}", flush=True)
    for key, why in failures:
        print(f"   FAILED {key}: {why}", flush=True)
    if capped:
        print(f"==> STOPPED at the spend cap (${C.SPEND_CAP_USD:.2f}); raise it in settings to go on", flush=True)
        return 3
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
