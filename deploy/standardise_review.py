"""The hidden-listings page's side of the photo process (design 2026-11-05, steps 4 and 5): what the config API serves
under /config/v1/photos. Blokport's page renders each row's `actions`; an empty list is a read-only row.

  hidden    the listings the ledger hides for want of a standardised photo (inventory reason no_photo), one row each:
            the owner's removal (owner_removed), a choice record not linked yet (chosen_not_linked), the full photos
            run's row (review.json: deploy.standardise_select.review_row, the tiles publish could not render), or
            none of these (not_worked_yet); the owner's live click; each photo's images as presigned GET URLs made
            afresh on every request (STANDARDISE.REVIEW_URL_TTL_SECONDS), never stored.
  spend     the month's process budget and cleaning cap, each run's spend, and the cleaning cap's paid uses.
  click     the owner's click to clean one photo, through the one decisions writer (deploy.standardise_decisions:
            checked, then applied inside the photo process's run lock).
  withdraw  the click withdrawn, through the same writer.

A store that cannot be read (the ledger, review.json) is Unavailable: said, never served as an empty list.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from collections import Counter
from datetime import datetime, timezone

import boto3
from botocore.config import Config

from deploy import standardise_decisions as decisions
from deploy import standardise_run as proc
from deploy import standardise_select as select
from deploy.standardise_publish import aws_config, chosen_prefix, pool_map, read_conflicts, read_records
from deploy.fetch_inputs import fetch_attributes
from deploy.standardise_source import round_prefix, sheet_and_photos
from stone_pipeline.config import settings
from stone_pipeline.config.settings import ENV_NAME, S3_BUCKET, S3_REGION, STANDARDISE as C
from stone_pipeline.core import logfmt
from stone_pipeline.io import imagestore

log = logfmt.get_logger("photos.review")

ROOT = decisions.ROOT
ACTIONS = ("use", "clean", "withdraw", "remove")      # the page's contract, in the order the page lists them
CLICKABLE = ("awaiting_click", "awaiting_auto", "owner_removed")   # the reasons a click may still change
RESULT_STATES = ("unchecked", "not_judged", "unparsed", "outdated", "pass", "fail")   # a result image exists
QUEUED = "queued"                                    # a click stored since the last full run: no run has read it yet
_SHA = re.compile(r"^[0-9a-f]{64}$")
_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class Unavailable(RuntimeError):
    """A store the answer depends on cannot be read: the ledger, or the review list."""


def client():
    """The photo routes' S3 client: the process's configuration, with a connection per worker (STORE_WORKERS reads at
    once; botocore's default pool of 10 made them queue and reconnect)."""
    return boto3.client("s3", region_name=S3_REGION,
                        config=aws_config().merge(Config(max_pool_connections=C.STORE_WORKERS)))


_SHEET = {}                                            # {"key", "value"}: the sheet as last parsed (sheet)
_SHEET_LOCK = threading.Lock()


def _categories():
    """The live category registry the sheet's formats are read by (index.listings), as a comparable value."""
    return tuple(sorted((c.pcat_id, c.name) for c in settings.active_categories()))


def _etag(s3, key):
    return s3.head_object(Bucket=S3_BUCKET, Key=key)["ETag"]


def sheet(s3):
    """(photo listings, tile listings) of the product sheet, by the photo process's own split (standardise_run): the
    photo listings are the ones decisions are about, a tile listing is rendered from its variety's slab stone. Parsed
    again only when the sheet, its photo list (their ETags) or the live categories changed; else the last parse is
    served (dev's sheet is 8.7 MB and changes once a produce; its readers never change it). A write between the look
    and the read only costs one more read on the next request."""
    fetch_attributes(s3)
    settings.refresh_category_pcats()
    key = (_etag(s3, f"{settings.ENV_SEGMENT}/{C.SHEET_KEY}"), _etag(s3, f"{settings.ENV_SEGMENT}/{C.PHOTOS_KEY}"),
           _categories())
    with _SHEET_LOCK:
        if _SHEET.get("key") == key:
            return _SHEET["value"]
    raw = sheet_and_photos(s3)
    value = (proc.photo_listings(*raw), proc.tile_listings(*raw))
    with _SHEET_LOCK:
        _SHEET.update(key=key, value=value)
    return value


# --- what the ledger hides --------------------------------------------------------------------------------------------
def hidden_products():
    """{SKU: {"listing": its handle, "source": its source's name}} of the products the ledger hides for want of a
    standardised photo (inventory reason no_photo); a source no longer configured is named None. Unavailable when there
    is no ledger or it cannot be read."""
    from stone_pipeline.config.sources import load_sources
    from stone_pipeline.ledger import writethrough
    from stone_pipeline.ledger.db import Ledger, LedgerError
    path = writethrough.ledger_path()
    if not path.exists():
        raise Unavailable(f"there is no sync ledger at {path}: no hidden listing can be named")
    try:
        with Ledger.open(path, env=ENV_NAME) as ledger:
            rows = ledger.execute("SELECT p.sku AS sku, p.handle AS handle, p.source AS code FROM product p"
                                  " JOIN inventory i ON i.sku = p.sku WHERE i.reason = 'no_photo'").fetchall()
    except (sqlite3.Error, LedgerError) as exc:
        raise Unavailable(f"the sync ledger cannot be read: {exc}") from exc
    names = {cfg.source_code: name for name, cfg in load_sources().items()}
    return {r["sku"]: {"listing": r["handle"], "source": names.get(r["code"])} for r in rows}


# --- GET /photos/hidden -------------------------------------------------------------------------------------------------
def read_review(s3):
    """{SKU: row} of the photos runs' review list (standardise_run.read_review); {} before any. Unavailable when it
    cannot be read."""
    try:
        return proc.read_review(s3, ROOT)
    except proc.ReviewUnreadable as exc:
        raise Unavailable(str(exc)) from exc


def live_click(record, seen):
    """The row's click as the clicks store holds it now: the run's view of it (`seen`, review_row) while it is the same
    click, QUEUED once the owner clicked again since, blocked when its file cannot be read, None when withdrawn."""
    if record is None:
        return None
    if "error" in record:
        return {"sha": None, "at": None, "state": "blocked", "why": f"its file cannot be read: {record['error']}"}
    if seen is not None and (seen["sha"], seen["at"]) == (record["sha"], record["at"]):
        return seen
    return {"sha": record["sha"], "at": record["at"], "state": QUEUED, "why": None}


def chosen_listings(s3):
    """The listing keys that have a choice record (<root>chosen/<listing>.json), from the keys alone: whether one exists
    is all the page needs, so no record is read."""
    prefix = chosen_prefix(ROOT)
    return {o["Key"][len(prefix):-len(".json")]
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix)
            for o in page.get("Contents", []) if o["Key"].endswith(".json")}


def _url(s3, key):
    return s3.generate_presigned_url("get_object", Params={"Bucket": S3_BUCKET, "Key": key},
                                     ExpiresIn=C.REVIEW_URL_TTL_SECONDS)


def _now():
    """Now as the page writes a decision's time (toISOString, UTC): the time a candidate decision is checked at."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _accepted(ctx, doc):
    return not decisions.refusals(ctx, doc)


def served_photo(s3, ctx, row, photo, at):
    """A review photo as served. clickable: its listing's reason still lets a click help, a cleaned copy could fix it,
    a model is left, and the applier would store the click; usable: the run tags whose result of it the applier would
    take as the owner's choice ("use"), asked only where the stored state has a result (results are never taken away,
    so this never offers one that is not there). Its images signed now: the scraped photo, its result when one exists,
    its cleaned copy when one was made."""
    sha, source = photo["sha"], row["source"]
    doc = {"id": row["listing"], "source": source, "sha": sha, "at": at}
    clickable = (row["reason"] in CLICKABLE and photo["fixable"] and bool(photo["clean"]["left"])
                 and _accepted(ctx, {**doc, "action": "clean"}))
    stored = {C.RUN_TAG: photo["state"] in RESULT_STATES, C.CLEAN_TAG: photo["clean"]["copy"] in RESULT_STATES}
    return {**photo, "clickable": clickable,
            "usable": [tag for tag in decisions.TAGS if stored[tag] and _accepted(ctx, {**doc, "action": "use", "tag": tag})],
            "urls": {"original": _url(s3, f"{imagestore.scraped_prefix(source)}{sha}.jpg"),
                     "result": (_url(s3, f"{round_prefix(photo['tag'], source)}{sha}.main.jpg")
                                if photo["state"] in RESULT_STATES else None),
                     "cleaned": (_url(s3, f"{round_prefix(C.CLEAN_TAG, source)}cleaned/{sha}.jpg")
                                 if photo["clean"]["copy"] is not None else None)}}


def row_actions(ctx, row, photos, click, at):
    """What the owner can do on the row, each only when the decisions applier would accept it (refusals), so an offered
    action never meets a 422: "use" while a photo has a usable result, "clean" while a photo is clickable, "withdraw"
    while a click is stored, "remove" on a photo listing not removed already."""
    doc = {"id": row["listing"], "source": row["source"], "at": at}
    offered = {"use": any(p["usable"] for p in photos), "clean": any(p["clickable"] for p in photos),
               "withdraw": click is not None and _accepted(ctx, {**doc, "action": "withdraw"}),
               "remove": row["reason"] != "owner_removed" and _accepted(ctx, {**doc, "action": "remove"})}
    return [action for action in ACTIONS if offered[action]]


def _joined(found, listing, reason):
    """A row only the join can give: the listing as the sheet has it (the ledger's handle when it left the sheet)."""
    return {"listing": listing["key"] if listing else found["listing"], "source": found["source"],
            "format": listing["format"] if listing else None, "reason": reason, "why": None, "click": None, "photos": []}


def _bases(s3, products, by_sku):
    """[(SKU, its row as stored, its listing or None)] of every listing the ledger hides, in the page's order (source,
    listing, SKU), from review.json and the keys of the removals and choice records alone (no record read, nothing per
    row): the owner's removal first, then a choice record (chosen, the next relink links it), then the last runs' row,
    else not worked yet."""
    review, removed, chosen = read_review(s3), proc.removed_listings(s3), chosen_listings(s3)
    out = []
    for sku, found in products.items():
        listing = by_sku.get(sku)
        if sku in removed:
            row = _joined(found, listing, "owner_removed")
        elif (listing["key"] if listing else found["listing"]) in chosen:
            row = _joined(found, listing, "chosen_not_linked")
        elif sku in review:
            row = review[sku]
        else:
            row = _joined(found, listing, "not_worked_yet")
        out.append((sku, row, listing))
    return sorted(out, key=lambda item: (item[1]["source"] or "", item[1]["listing"] or "", item[0]))


def _removed_photos(s3, page, exclude):
    """{SKU: its photos} of the removed listings on the page (manager ruling (c): a removal is never a dead end): its
    candidates (select.candidates: the photos the process judges, so the only ones with a result to use or clean),
    each as a run's row has it (select.review_photo), over what the run folders hold of them alone."""
    removed = [(sku, listing) for sku, row, listing in page if row["reason"] == "owner_removed" and listing is not None]
    if not removed:
        return {}
    conflicts = read_conflicts(s3, {l["source"] for _, l in removed})
    blocked = {sku: exclude | {s for s in l["photos"] if (l["source"], s) in conflicts} for sku, l in removed}
    asked = {sku: select.candidates(l, blocked[sku]) for sku, l in removed}
    by_source = {}
    for sku, l in removed:
        by_source.setdefault(l["source"], set()).update(asked[sku])
    # one source at a time, each Round listing its photos' prefixes STORE_WORKERS at once (never pools in pools)
    rounds = [(tag, {src: select.Round(s3, tag, src, shas) for src, shas in sorted(by_source.items())})
              for tag in (C.RUN_TAG, C.CLEAN_TAG)]
    return {sku: [select.review_photo(s3, rounds, l, sha, exclude, blocked[sku]) for sha in asked[sku]] for sku, l in removed}


def _results(s3, rows):
    """{(tag, source): select.Results} of what the page asks the applier about: a result to use (the stored state has
    one) and, of a photo a click may clean (fixable with a model left), its verdict and hold, read in parallel; nothing
    else is read (served_photo asks exactly these)."""
    wanted = {}
    for row in rows:
        for p in row["photos"]:
            run = wanted.setdefault((C.RUN_TAG, row["source"]), (set(), set()))
            if p["state"] in RESULT_STATES:
                run[0].add(p["sha"])
            if p["fixable"] and p["clean"]["left"]:
                run[1].add(p["sha"])
            if p["clean"]["copy"] in RESULT_STATES:
                wanted.setdefault((C.CLEAN_TAG, row["source"]), (set(), set()))[0].add(p["sha"])
    # one (tag, source) at a time, each listing its photos' prefixes STORE_WORKERS at once (never pools in pools)
    return {pair: select.Results(s3, *pair, shas, verdicts) for pair, (shas, verdicts) in sorted(wanted.items())}


def hidden(s3, reason=None, offset=0, limit=C.REVIEW_PAGE_DEFAULT):
    """One page of the listings the ledger hides: {"total" (rows of `reason`, or all), "counts" ({reason: rows} over
    every hidden row), "offset", "limit", "rows"}. Every row is placed and counted from the stored rows and keys alone
    (_bases), in a stable order applied before the page is cut; only the page's rows are then worked: the removed ones'
    photos (_removed_photos), the stored decisions of their listings alone and the results their photos are asked
    about (decisions.context keys and results), the applier asked about each action (served_photo, row_actions), the
    live click, the images signed: a few rows at a time."""
    products = hidden_products()
    photo_listings, tiles = sheet(s3)
    by_sku = {l["sku"]: l for l in photo_listings + tiles}
    bases = _bases(s3, products, by_sku)
    counts = Counter(row["reason"] for _, row, _ in bases)
    picked = [item for item in bases if reason is None or item[1]["reason"] == reason]
    page = picked[offset:offset + limit]
    exclude = decisions.excluded()
    removed_photos = _removed_photos(s3, page, exclude)
    rows = [(sku, {**row, "photos": removed_photos[sku]} if sku in removed_photos else row) for sku, row, _ in page]
    results = _results(s3, [row for _, row in rows])
    try:
        ctx = decisions.context(s3, photo_listings, exclude, keys=[row["listing"] for _, row in rows],
                                results=lambda tag, src: results.get((tag, src)) or select.Results(s3, tag, src, (), ()))
    except decisions.InvalidDecisions as exc:
        raise Unavailable(str(exc)) from exc
    stores, at = ctx.stores, _now()
    clicks = {**stores.clicks, **{sku: {"error": why} for sku, items in stores.unreadable.items()
                                  for what, _, why in items if what == "click"}}

    def served(item):
        sku, row = item
        photos = [served_photo(s3, ctx, row, p, at) for p in row["photos"]]
        click = live_click(clicks.get(sku), row["click"])
        return {"sku": sku, **row, "click": click, "photos": photos, "actions": row_actions(ctx, row, photos, click, at)}
    return {"total": len(picked), "counts": dict(sorted(counts.items())), "offset": offset, "limit": limit,
            "rows": pool_map(C.STORE_WORKERS, served, rows)}


# --- GET /photos/spend --------------------------------------------------------------------------------------------------
def valid_month(month):
    return isinstance(month, str) and bool(_MONTH.match(month))


def _month(s3, directory, month):
    """(runs, uses, unreadable) of one budget's spend files for the month (each run's own <run id>.json, {"usd",
    "uses"}): each use named by its run (the file's name), oldest first; a file without a number for usd is unreadable
    too."""
    found, broken = read_records(s3, proc.spend_prefix(directory, month), lambda key: key.rsplit("/", 1)[1][:-len(".json")])
    for run_id, record in list(found.items()):
        if not isinstance(record.get("usd"), (int, float)) or isinstance(record["usd"], bool):
            broken[run_id] = f"usd is {record.get('usd')!r}, not a number"
            del found[run_id]
    runs = [{"run_id": run_id, "usd": record["usd"]} for run_id, record in sorted(found.items())]
    uses = sorted(({"run_id": run_id, **use} for run_id, record in found.items() for use in record.get("uses", [])),
                  key=lambda u: (u["at"], u["run_id"], u["sha"], u["model"], u["call"]))
    return runs, uses, [{"run_id": run_id, "error": why} for run_id, why in sorted(broken.items())]


def spend(s3, month):
    """The month's photo spend: the process budget (GPU and judge) and the cleaning cap (edits and their checks), each
    with its budget, what its runs spent, each run, and the files that cannot be read (named, left out of the sum); the
    cleaning cap's paid uses too, oldest first."""
    out = {"month": month}
    for name, directory, budget in (("process", C.SPEND_DIR, C.BUDGET_USD), ("cleaning", C.CLEAN_SPEND_DIR,
                                                                             C.CLEAN_BUDGET_USD)):
        runs, uses, broken = _month(s3, directory, month)
        out[name] = {"budget_usd": budget, "spent_usd": round(sum(r["usd"] for r in runs), 6), "runs": runs,
                     **({"uses": uses} if name == "cleaning" else {}), "unreadable": broken}
    return out


# --- PUT and DELETE /photos/clicks/{sku} ----------------------------------------------------------------------------------
def check_body(body, fields):
    """The body's fields when it is exactly `fields`, each a string: sha a photo's sha256, at an ISO UTC timestamp.
    ValueError names what is wrong."""
    if not isinstance(body, dict) or set(body) != set(fields):
        raise ValueError(f"the body is exactly {{{', '.join(fields)}}}")
    if "sha" in fields and not (isinstance(body["sha"], str) and _SHA.match(body["sha"])):
        raise ValueError(f"sha {body['sha']!r} is not a photo's sha256")
    if decisions.moment(body["at"]) is None:
        raise ValueError(f"at {body['at']!r} is not an ISO UTC timestamp (2026-11-07T10:00:00.000Z)")
    return body


def _say(line):
    log.info("photo decision", extra={"extra_fields": {"line": line}})


def run_active(s3, exc):
    """The 409 while the run lock is held, naming its holder: a decisions apply ("decisions-...") or a photos run."""
    holder = proc.lock_holder(s3)
    who = "a decisions apply" if (holder or "").startswith("decisions-") else "a photos run"
    return 409, {"reason": "run_active", "run_id": holder,
                 "error": f"{who} holds the lock ({holder or exc}): try again after it"}


def _decide(s3, docs, dry_run=False, say=_say, sheet=None, narrow=False):
    """(Plan, None) of the decisions planned, and unless dry_run applied (decisions.decide: on `sheet` when the caller
    read it, narrow for a click's one decision), or (None, the refusal): 422 when one is invalid, 409 while the lock is
    held (run_active)."""
    try:
        return decisions.decide(s3, docs, dry_run=dry_run, say=say, sheet=sheet, narrow=narrow), None
    except decisions.InvalidDecisions as exc:
        return None, (422, {"reason": "refused", "error": str(exc)})
    except proc.RunActive as exc:
        return None, run_active(s3, exc)


def _listing(s3, sku):
    """(the listing with this SKU or None, the sheet's photo listings): the sheet read once per request."""
    photo_listings, tiles = sheet(s3)
    return next((l for l in photo_listings + tiles if l["sku"] == sku), None), photo_listings


def _has_choice(s3, key):
    """Whether this listing has a choice record (<root>chosen/<listing>.json): its own key listed, nothing read."""
    wanted = f"{chosen_prefix(ROOT)}{key}.json"
    return any(o["Key"] == wanted for page in s3.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=wanted)
               for o in page.get("Contents", []))


def _unknown(sku):
    return 404, {"error": f"no listing on the product sheet has SKU {sku!r}"}


def click(s3, sku, sha, at):
    """The owner's click to clean photo `sha` of the listing with this SKU: (status, body). A listing the ledger does
    not hide: 422 when the decisions applier refuses the click anyway (a dry run), else 409 not_hidden. A hidden one:
    409 chosen_not_linked when it has a choice record or a photo of it now passes (the next relink or run shows it);
    200 already_cleaned or nothing_left_to_try with nothing written (click_state: its copy exists, or no model is left);
    else the decisions applier, once, inside the run lock: 422 with its reason (a tile, a source with the process off,
    an excluded or conflict photo, one a cleaned copy cannot fix, an older decision), 409 run_active while a photos run
    holds the lock, or 200 queued."""
    listing, photo_listings = _listing(s3, sku)
    if listing is None:
        return _unknown(sku)
    doc = {"id": listing["key"], "action": "clean", "source": listing["source"], "sha": sha, "at": at}
    if sku not in hidden_products():
        _, refused = _decide(s3, [doc], dry_run=True, sheet=photo_listings, narrow=True)
        if refused is not None and refused[0] == 422:
            return refused
        return 409, {"reason": "not_hidden", "error": f"{sku} is not hidden for want of a photo"}
    src = listing["source"]
    if _has_choice(s3, listing["key"]):
        return 409, {"reason": "chosen_not_linked", "error": f"{sku} has a chosen photo; the next relink shows it"}
    conflicts = read_conflicts(s3, {src})
    blocked = decisions.excluded() | {s for s in listing["photos"] if (src, s) in conflicts}
    # what the run folders hold of the photos the two states ask about (select.Round shas), never a whole folder: the
    # listing's candidates (listing_state judges only those) and the clicked photo (click_state)
    asked = set(select.candidates(listing, blocked)) | {sha}
    rounds = [(tag, {src: select.Round(s3, tag, src, asked)}) for tag in (C.RUN_TAG, C.CLEAN_TAG)]
    state = select.listing_state(s3, rounds, listing, blocked, None)    # a clean lifts the owner's choice (decisions)
    record = {"sku": sku, "listing": listing["key"], "source": src, "sha": sha, "at": at}
    found = select.click_state(s3, rounds, listing, blocked, record, state)
    if found[0] == "done":
        return 409, {"reason": "chosen_not_linked", "error": f"a photo of {sku} passes; the next photos run links it"}
    if found[0] == "cleaned":
        return 200, {"sku": sku, "status": "already_cleaned"}
    if found == ("blocked", select.NO_MODEL_LEFT):
        return 200, {"sku": sku, "status": "nothing_left_to_try"}
    _, refused = _decide(s3, [doc], sheet=photo_listings, narrow=True)
    return refused or (200, {"sku": sku, "status": QUEUED})


def withdraw(s3, sku, at):
    """The owner's click on the listing with this SKU withdrawn, through the decisions applier: (status, body). 200
    withdrawn, or no_click when none is stored; 422 when older than the decision stored; 409 run_active."""
    listing, photo_listings = _listing(s3, sku)
    if listing is None:
        return _unknown(sku)
    plan, refused = _decide(s3, [{"id": listing["key"], "action": "withdraw", "source": listing["source"], "at": at}],
                            sheet=photo_listings, narrow=True)
    return refused or (200, {"sku": sku, "status": "withdrawn" if plan.deletions else "no_click"})


# --- POST /photos/decisions --------------------------------------------------------------------------------------------
def apply_decisions(s3, docs, dry_run):
    """The page's export (decision documents) planned, and unless dry_run applied, by the decisions applier: (status,
    body). 200 {dry_run, applied, owner_choices, removals, cleans, replaced (records a later decision replaces), lines
    (the plan as the CLI prints it)}; 422 with the applier's reasons (nothing written); 409 run_active."""
    lines = []
    plan, refused = _decide(s3, docs, dry_run=dry_run, say=lines.append)
    if refused:
        return refused
    return 200, {"dry_run": dry_run, "applied": not dry_run, "owner_choices": len(plan.owner),
                 "removals": len(plan.removals), "cleans": len(plan.clicks), "replaced": len(plan.deletions),
                 "lines": lines}
