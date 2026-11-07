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

import json
import re
import sqlite3

import boto3
from botocore.exceptions import ClientError

from deploy import standardise_decisions as decisions
from deploy import standardise_run as proc
from deploy import standardise_select as select
from deploy.standardise_publish import (MISSING, aws_config, chosen_prefix, excluded, read_clicks, read_conflicts,
                                       read_records)
from deploy.standardise_source import round_prefix, sheet_and_photos
from stone_pipeline.config.settings import ENV_NAME, S3_BUCKET, S3_REGION, STANDARDISE as C
from stone_pipeline.core import logfmt
from stone_pipeline.io import imagestore
from stone_pipeline.standardise import index

log = logfmt.get_logger("photos.review")

ROOT = decisions.ROOT
ACTIONS = ("clean", "withdraw")                      # the page's contract: PUT and DELETE /photos/clicks/{sku}
CLICKABLE = ("awaiting_click", "awaiting_auto")      # the reasons a click may still change
RESULT_STATES = ("unchecked", "not_judged", "unparsed", "outdated", "pass", "fail")   # a result image exists
QUEUED = "queued"                                    # a click stored since the last full run: no run has read it yet
_SHA = re.compile(r"^[0-9a-f]{64}$")
_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class Unavailable(RuntimeError):
    """A store the answer depends on cannot be read: the ledger, or the review list."""


def client():
    return boto3.client("s3", region_name=S3_REGION, config=aws_config())


def sheet_listings(s3):
    """Every listing of the product sheet (index.listings), tiles too: what a SKU names."""
    return index.listings(*sheet_and_photos(s3))


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
    """{SKU: row} of the last full photos run's review list; {} before any (no file). Unavailable when unreadable."""
    try:
        body = s3.get_object(Bucket=S3_BUCKET, Key=f"{ROOT}{C.REVIEW_FILE}")["Body"].read()
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in MISSING:
            return {}
        raise
    try:
        rows = json.loads(body)
    except json.JSONDecodeError as exc:
        raise Unavailable(f"the review list {ROOT}{C.REVIEW_FILE} cannot be read: {exc}") from exc
    if not isinstance(rows, dict):
        raise Unavailable(f"the review list {ROOT}{C.REVIEW_FILE} is not an object of rows")
    return rows


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


def served_photo(s3, source, photo, reason):
    """A review photo as served: whether a click may clean it (its listing's reason still lets a click help, a cleaned
    copy could fix it and a model is left) and its images signed now: the scraped photo, its result when one exists,
    its cleaned copy when one was made."""
    sha = photo["sha"]
    return {**photo,
            "clickable": reason in CLICKABLE and photo["fixable"] and bool(photo["clean"]["left"]),
            "urls": {"original": _url(s3, f"{imagestore.scraped_prefix(source)}{sha}.jpg"),
                     "result": (_url(s3, f"{round_prefix(photo['tag'], source)}{sha}.main.jpg")
                                if photo["state"] in RESULT_STATES else None),
                     "cleaned": (_url(s3, f"{round_prefix(C.CLEAN_TAG, source)}cleaned/{sha}.jpg")
                                 if photo["clean"]["copy"] is not None else None)}}


def _joined(found, reason):
    return {"listing": found["listing"], "source": found["source"], "format": None, "reason": reason, "why": None,
            "click": None, "photos": []}


def hidden(s3):
    """{"rows": [...]} of every listing the ledger hides, by source then listing: the owner's removal first, then a
    choice record (chosen, the next relink links it), then the last full run's row, else not worked yet; each with the
    live click (live_click), its photos served (served_photo) and its actions: "clean" while a photo is clickable,
    "withdraw" while a click is stored."""
    products = hidden_products()
    review, removed = read_review(s3), proc.removed_listings(s3)
    records, clicks = chosen_listings(s3), read_clicks(s3, ROOT)
    rows = []
    for sku, found in products.items():
        if sku in removed:
            row = _joined(found, "owner_removed")
        elif found["listing"] in records:
            row = _joined(found, "chosen_not_linked")
        elif sku in review:
            row = review[sku]
        else:
            row = _joined(found, "not_worked_yet")
        photos = [served_photo(s3, row["source"], p, row["reason"]) for p in row["photos"]]
        click = live_click(clicks.get(sku), row["click"])
        actions = [a for a, on in (("clean", any(p["clickable"] for p in photos)), ("withdraw", click is not None)) if on]
        rows.append({"sku": sku, **row, "click": click, "photos": photos, "actions": actions})
    return {"rows": sorted(rows, key=lambda r: (r["source"] or "", r["listing"] or "", r["sku"]))}


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


def _decide(s3, doc):
    """(Plan, None) of the one decision applied, or (None, the refusal): 422 when invalid, 409 while a run holds the
    lock."""
    try:
        return decisions.decide(s3, [doc], dry_run=False, say=_say), None
    except decisions.InvalidDecisions as exc:
        return None, (422, {"reason": "refused", "error": str(exc)})
    except proc.RunActive as exc:
        return None, (409, {"reason": "run_active", "run_id": proc.lock_holder(s3),
                            "error": f"a photos run holds the lock ({exc}): try again after it"})


def _listing(s3, sku):
    return next((l for l in sheet_listings(s3) if l["sku"] == sku), None)


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
    listing = _listing(s3, sku)
    if listing is None:
        return _unknown(sku)
    doc = {"id": listing["key"], "action": "clean", "source": listing["source"], "sha": sha, "at": at}
    if sku not in hidden_products():
        try:
            decisions.decide(s3, [doc], dry_run=True, say=lambda line: None)
        except decisions.InvalidDecisions as exc:
            return 422, {"reason": "refused", "error": str(exc)}
        return 409, {"reason": "not_hidden", "error": f"{sku} is not hidden for want of a photo"}
    src = listing["source"]
    if listing["key"] in chosen_listings(s3):
        return 409, {"reason": "chosen_not_linked", "error": f"{sku} has a chosen photo; the next relink shows it"}
    rounds = select.read_rounds(s3, (C.RUN_TAG, C.CLEAN_TAG), [src])
    conflicts = read_conflicts(s3, {src})
    blocked = excluded() | {s for s in listing["photos"] if (src, s) in conflicts}
    state = select.listing_state(s3, rounds, listing, blocked, None)    # a clean lifts the owner's choice (decisions)
    record = {"sku": sku, "listing": listing["key"], "source": src, "sha": sha, "at": at}
    found = select.click_state(s3, rounds, listing, blocked, record, state)
    if found[0] == "done":
        return 409, {"reason": "chosen_not_linked", "error": f"a photo of {sku} passes; the next photos run links it"}
    if found[0] == "cleaned":
        return 200, {"sku": sku, "status": "already_cleaned"}
    if found == ("blocked", select.NO_MODEL_LEFT):
        return 200, {"sku": sku, "status": "nothing_left_to_try"}
    _, refused = _decide(s3, doc)
    return refused or (200, {"sku": sku, "status": QUEUED})


def withdraw(s3, sku, at):
    """The owner's click on the listing with this SKU withdrawn, through the decisions applier: (status, body). 200
    withdrawn, or no_click when none is stored; 422 when older than the decision stored; 409 run_active."""
    listing = _listing(s3, sku)
    if listing is None:
        return _unknown(sku)
    plan, refused = _decide(s3, {"id": listing["key"], "action": "withdraw", "source": listing["source"], "at": at})
    return refused or (200, {"sku": sku, "status": "withdrawn" if plan.deletions else "no_click"})
