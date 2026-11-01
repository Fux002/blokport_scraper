"""Choose each listing's one good photo (user 2026-11-01: one very good image per listing is enough; it gives every view).
The steps deploy.standardise_run calls in order; hosted calls only, no model runs here. Two run tags take part: the
standardiser's run over the scraped photos and its run over the copies a hosted edit model cleaned. Every answer is
stored next to the image it is about, so a re-run reads it back and never pays twice; one listing's failure is
reported and never stops the others.
  judge   per listing, its finished photos in its own order (the scraped run first, then the cleaned run) go to the
          judge until one passes, at most JUDGE_MAX_PER_LISTING per tag; the verdict is kept at <tag>/<src>/judged/
  clean   per listing with no pass, the first CLEAN_PHOTOS_PER_LISTING photos a cleaned copy can fix (failed only for
          reflections or marks, or held by the retouch or logo step) are edited by each CLEAN_MODELS model in turn until
          a copy passes the clean check; that copy becomes <clean tag>/<src>/cleaned/<sha>.jpg
  report  from the stored answers only: <clean tag>/chosen.json (what is published) and <clean tag>/tracked.csv (every
          listing with no good photo, with what happened to each of its photos)
"""

from __future__ import annotations

import csv
import io
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor

from deploy.standardise_publish import EXCLUDE_PATH, read_exclude
from deploy.standardise_source import round_prefix
from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET, STANDARDISE as C
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
        raise RuntimeError("not every photo looked at has a verdict: the judge step must run first")
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


class Selection:
    """The listings one run works on and what the two run tags hold for them (re-read after each GPU round)."""

    def __init__(self, client, listings, run_tag, clean_tag):
        if not run_tag or not clean_tag or run_tag == clean_tag:
            raise ValueError("two different run tags are required: the scraped photos' run and the cleaned copies' run")
        self.client, self.listings, self.tags = client, listings, (run_tag, clean_tag)
        self.sources = sorted({l["source"] for l in listings})
        self.exclude = read_exclude(EXCLUDE_PATH.read_text())
        self.root = f"{ENV_SEGMENT}/{C.TEST_PREFIX}/{clean_tag}/"
        self.refresh()

    def refresh(self):
        self.rounds = [(tag, {src: Round(self.client, tag, src) for src in self.sources}) for tag in self.tags]

    def gpu_todo(self):
        """{source: shas} the standardiser has not run yet among each listing's first JUDGE_MAX_PER_LISTING photos (the
        judge never looks further)."""
        gpu = self.rounds[0][1]
        return _by_source((l["source"], sha) for l in self.listings for sha in l["photos"][:C.JUDGE_MAX_PER_LISTING]
                          if sha not in self.exclude and sha not in gpu[l["source"]].finished | gpu[l["source"]].held)

    def clean_todo(self):
        """{source: shas} with a cleaned copy the standardiser has not run yet."""
        cl = self.rounds[1][1]
        return _by_source((src, sha) for src in self.sources for sha in cl[src].cleaned
                          if sha not in cl[src].finished | cl[src].held)


def _by_source(pairs):
    out = {}
    for src, sha in sorted(set(pairs)):
        out.setdefault(src, []).append(sha)
    return out


def judge_all(sel, spend):
    """(failures, cap reached): every listing judged until its first pass."""
    return _each(sel.listings, lambda l: choose(sel.client, sel.rounds, l, sel.exclude, spend))


def clean_all(sel, spend):
    """(failures, cap reached): every listing with no pass gets a cleaned copy where one can be made."""
    return _each(sel.listings, lambda l: clean_listing(sel.client, sel.rounds, l, sel.exclude, spend))


def report(sel):
    """{"chosen", "tracked", "pending"}: writes chosen.json (what is published) and tracked.csv (the listings with no
    good photo); pending = listings not fully judged, which are neither."""
    chosen, tracked, pending = [], [], []
    for l in sel.listings:
        picked, complete = choose(sel.client, sel.rounds, l, sel.exclude)
        if picked:
            tag, sha = picked
            chosen.append({"source": l["source"], "key": l["key"], "sha": sha, "tag": tag, "prefix": round_prefix(tag, l["source"])})
        elif complete:
            tracked.append({"source": l["source"], "listing": l["key"], "format": l["format"], "photos": len(l["photos"]),
                            "what_happened": photo_notes(sel.client, sel.rounds, l, sel.exclude)})
        else:
            pending.append(l["key"])
    _put_json(sel.client, f"{sel.root}chosen.json", chosen)
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=["source", "listing", "format", "photos", "what_happened"])
    writer.writeheader()
    writer.writerows(tracked)
    _put(sel.client, f"{sel.root}tracked.csv", out.getvalue().encode(), "text/csv")
    return {"chosen": chosen, "tracked": tracked, "pending": pending}


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
