"""Apply the owner's decisions on the hidden listings (the decision page's `decisions` collection, one document per
listing, exported as a JSON array of {id: <listing handle>, action, source, sha, tag, at}, a clean without the tag) to
the photo process's own stores, so the next photos run acts on them (STANDARDISE_PROCESS.md, "The owner's decisions").
Every record is keyed by the listing's SKU, which a variety re-bind never moves (the handle it does):
  use     the owner's photo choice record, <clean tag root>owner/<SKU>.json = {tag, sha, at, listing, decided_on}: the
          process chooses that result for the listing before anything else and judges nothing of it, while the photo is
          one of the listing's and not excluded. A photo belongs to a stone and finish: the choice also reaches every
          listing of the same stone that shows the photo (slabs of one block, tile sizes), each with its own record
          (decided_on names the listing the owner decided on); a listing with its own decision keeps it. A photo that
          listings of another variety or finish show too (a conflict) is assigned to that one listing only.
  remove  a removal record, <env>/standardise-test/removed/<SKU>.json: the process never works on the listing again and
          the image stage links nothing standardised to it, even a sibling's published photo, so it stays hidden (reason
          no_photo). Its owner choice and its choice record go. Nothing is deleted from the catalog: Blokport deletes the product.
  clean   the owner's click, <clean tag root>clicks/<SKU>.json = {sku, listing, source, sha, at}, stored and read only
          by publish.write_click and read_clicks: the next photos run sends that photo to the hosted clean step first,
          before its other work, while the month's budget can finish it (else it waits: run.admit), whose
          gate (select.authorised) allows a click whatever kind of fix it needs and whatever the clean switch says; the
          cleaned copy is judged, chosen on a pass, and on a fail the listing stays on the review list (its stored
          attempts are read back, never paid again). Only for the listing decided on: a cleaned copy is the photo's, so
          a listing of the same stone showing it finds it itself.
  withdraw the owner's click withdrawn (the hidden-listings page's DELETE /photos/clicks): its click file goes, nothing
          else; a listing without a click is left as it is.
The judge's verdicts are never touched. The owner's latest decision on a listing wins, and each replaces the other two:
a "use" lifts its removal and its click; a "remove" drops its choice and its click; a "clean" lifts its removal and its
owner choice with the choice record made from it (else that photo stays the listing's valid choice and the run never
reaches the click). A decision older than the one already stored for its listing is refused, so an old export applied
again never undoes a later decision.
Every decision is checked first (the listing is on the product sheet, the action is known, exactly the page's fields,
`at` an ISO UTC timestamp, the source is the listing's, the photo one of the listing's and not excluded, the tag's result
exists, for a clean the source's image pipeline is on, the photo is no conflict photo and the run could ever use the click (select.click_problem: its record
is the listing's, and no current verdict or hold says a cleaned copy cannot fix the photo; one not judged yet is
accepted, the run reports it blocked until it is), not older than what is stored, no stored decision of the listing that
cannot be read); one bad decision refuses the whole file and nothing is written. The stores are read through the photo
process's own readers, so a file that cannot be read is named with its key and never crashes the applier: a listing
with one is never decided over (its decision is unknown), never reached by a sibling's use, and a conflict photo is
not given while a partner's stored choice cannot be read. Only what differs from the stores is written, so applying
the same file again writes nothing. The plan is read and written inside the photo process's run lock; a write that
fails half way is completed by running again.
Env: SCRAPER_ENV (required, explicitly: the environment's bucket is printed first), DECISIONS_FILE (the export),
DRY_RUN (true prints the plan and writes nothing, no lock taken; false applies).
Exit 0 applied (or planned); 2 not configured, or the file or a decision is invalid; 4 a photos run holds the lock.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import boto3

from deploy import standardise_run as proc
from deploy import standardise_select as select
from deploy.standardise_publish import (NoConflictSet, aws_config, chosen_prefix, click_record, clicks_prefix, excluded,
                                       owner_key, owner_prefix, read_choices, read_clicks, read_conflicts,
                                       records_by_sku, write_click)
from deploy.standardise_source import sheet_and_photos
from stone_pipeline.config.settings import ENV_NAME, S3_BUCKET, S3_REGION, STANDARDISE as C
from stone_pipeline.config.sources import image_pipeline_off
from stone_pipeline.io import imagestore
from stone_pipeline.standardise import index

# The page's document shape per action (its contract, not a setting): exactly these fields. A clean names no tag: the
# clean step always edits the scraped photo whose standardised result failed (select.clean_plan).
FIELDS = {"use": {"id", "action", "source", "sha", "tag", "at"}, "remove": {"id", "action", "source", "at"},
          "clean": {"id", "action", "source", "sha", "at"}, "withdraw": {"id", "action", "source", "at"}}
TAGS = (C.RUN_TAG, C.CLEAN_TAG)                        # the process's tags, the only results a choice can name
_SHA = re.compile(r"^[0-9a-f]{64}$")
_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")   # what the page writes: toISOString(), UTC
_FLAG = {"true": True, "false": False}
ROOT = f"{proc.ROOT}{C.CLEAN_TAG}/"


class InvalidDecisions(ValueError):
    """The export or one of its decisions is invalid: nothing is applied."""


@dataclass
class Plan:
    """What applying the decisions changes; only what differs from the stores."""
    owner: dict[str, dict] = field(default_factory=dict)          # owner choice record key -> record to write
    removals: dict[str, dict] = field(default_factory=dict)       # removal record key -> record to write
    clicks: dict[str, dict] = field(default_factory=dict)         # SKU -> click record to write (write_click)
    deletions: list[str] = field(default_factory=list)            # records a later decision replaces
    notes: list[str] = field(default_factory=list)                # what reaches whom, and what is left as it is

    def empty(self):
        return not (self.owner or self.removals or self.clicks or self.deletions)


def read_decisions(text):
    """The export's documents: a JSON array of objects."""
    try:
        docs = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidDecisions(f"the export is not JSON: {exc}") from exc
    if not isinstance(docs, list) or not all(isinstance(d, dict) for d in docs):
        raise InvalidDecisions("the export must be a JSON array of decision objects")
    return docs


def moment(at):
    """A decision's time (an ISO UTC timestamp, ending Z) as a datetime; None when it is not one."""
    if not isinstance(at, str) or not _AT.match(at):
        return None
    try:
        return datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return None


def _shape(doc, listings):
    """The errors in one document's own fields and its listing; [] when it can be checked against the stores."""
    action, key = doc.get("action"), doc.get("id")
    if action not in FIELDS:
        return [f"unknown action {action!r} (one of {', '.join(FIELDS)})"]
    if set(doc) != FIELDS[action]:
        return [f"a {action} has exactly the fields {sorted(FIELDS[action])}, this one {sorted(doc)}"]
    if not all(isinstance(doc[k], str) and doc[k] for k in FIELDS[action]):
        return ["every field must be a non-empty string"]
    if moment(doc["at"]) is None:
        return [f"at {doc['at']!r} is not an ISO UTC timestamp (2026-11-02T10:00:00.000Z)"]
    listing = listings.get(key)
    if listing is None:
        return ["not on the product sheet as a photo listing (a tile listing is rendered, never decided here)"]
    if doc["source"] != listing["source"]:
        return [f"source {doc['source']!r} is not the listing's ({listing['source']!r})"]
    if action in ("remove", "withdraw"):
        return []
    if not _SHA.match(doc["sha"]):
        return [f"sha {doc['sha']!r} is not a photo's sha256"]
    if action == "use" and doc["tag"] not in TAGS:
        return [f"tag {doc['tag']!r} is not the process's ({' or '.join(TAGS)})"]
    if doc["sha"] not in listing["photos"]:
        return [f"photo {doc['sha'][:12]} is not one of the listing's photos"]
    return []


def _keys(s3, prefix):
    return {o["Key"] for page in s3.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix)
            for o in page.get("Contents", [])}


class _Stores:
    """The decisions already stored, read once through the photo process's own readers (standardise_run.owner_records,
    publish.records_by_sku, publish.read_choices, publish.read_clicks), so a file that cannot be read fails only its
    listing: each SKU's owner choice, removal and click, and its listing's choice record; `unreadable` = {SKU: [(what, key, why)]} of the files that cannot be read, whose
    listing is never decided over (its decision is unknown); and the click files' keys by SKU, so a later decision
    removes the file there."""

    def __init__(self, s3, listings):
        self.s3 = s3
        self.owner_keys, self.removal_keys = _keys(s3, owner_prefix(ROOT)), _keys(s3, imagestore.owner_removed_prefix())
        self.record_keys = _keys(s3, chosen_prefix(ROOT))
        self.records, record_bad = read_choices(s3, ROOT)                 # by listing key
        self.click_keys = {imagestore.sku_of(k, clicks_prefix(ROOT)): k for k in _keys(s3, clicks_prefix(ROOT))}
        self.stored = self.owner_keys | self.removal_keys | self.record_keys
        (self.owners, owner_bad), clicks = proc.owner_records(s3, ROOT), read_clicks(s3, ROOT)
        self.removals, removal_bad = records_by_sku(s3, imagestore.owner_removed_prefix())
        self.clicks = {sku: r for sku, r in clicks.items() if "error" not in r}
        self.unreadable = {}
        for what, bad, key in (("owner choice", owner_bad, lambda sku: owner_key(ROOT, sku)),
                               ("removal", removal_bad, imagestore.owner_removed_key),
                               ("click", {sku: r["error"] for sku, r in clicks.items() if "error" in r},
                                lambda sku: self.click_keys[sku])):
            for sku, why in bad.items():
                self.unreadable.setdefault(sku, []).append((what, key(sku), why))
        for key, why in record_bad.items():          # a choice record names its listing; the stores go by SKU
            if key in listings:
                self.unreadable.setdefault(listings[key]["sku"], []).append(
                    ("choice record", f"{chosen_prefix(ROOT)}{key}.json", why))

    def chosen(self, listing):
        """A listing's choice record (the run's own, by listing key), None without one (or when it cannot be read: the
        listing is then unreadable, see unreadable_of)."""
        return self.records.get(listing["key"])

    def owner(self, sku):
        return self.owners.get(sku)

    def removal(self, sku):
        return self.removals.get(sku)

    def click(self, sku):
        return self.clicks.get(sku)

    def owner_unreadable(self, sku):
        """The key of a SKU's owner choice that cannot be read, None when it can (or there is none)."""
        return next((key for what, key, _ in self.unreadable.get(sku, ()) if what == "owner choice"), None)

    def latest(self, sku):
        """The time of the latest decision stored for a SKU, None without one."""
        times = [moment(r["at"]) for r in (self.owner(sku), self.removal(sku), self.click(sku)) if r is not None]
        return max(times) if times else None

    def live(self, listing, exclude, conflicts):
        """Whether a listing has a valid choice of its own (the process's own test, standardise_run.valid_choice, without
        an owner choice): a decision on a sibling never re-chooses it."""
        return proc.valid_choice(listing, self.chosen(listing), exclude, conflicts, {})

    def own_decision(self, listing):
        """Whether the owner decided on this listing itself: a removal, a click, or a choice made on it (not reached
        through another listing of its stone); a stored decision that cannot be read counts (it is unknown, so it
        stands)."""
        sku, choice = listing["sku"], self.owner(listing["sku"])
        return (sku in self.unreadable or self.removal(sku) is not None or self.click(sku) is not None
                or (choice is not None and choice["decided_on"] == choice["listing"]))


def _check(docs, listings, rounds, stores, exclude, shared, off):
    """Every decision checked; InvalidDecisions names each bad one. off: the sources whose image pipeline is off."""
    errors, seen = [], set()
    for i, doc in enumerate(docs):
        key = doc.get("id")
        found = _shape(doc, listings)
        if not found and key in seen:
            found = ["decided more than once in this file"]
        elif not found and doc["action"] == "clean" and doc["source"] in off:
            found = [f"source {doc['source']!r}: its image pipeline is off, the photos run never works on it"]
        elif not found and "sha" in doc and doc["sha"] in exclude:
            found = [f"photo {doc['sha'][:12]} is on the exclude list"]
        elif not found and doc["action"] == "use" and doc["sha"] not in rounds(doc["tag"], doc["source"]).finished:
            found = [f"photo {doc['sha'][:12]} has no result under {doc['tag']}"]
        elif not found and doc["action"] == "clean" and (
                unfit := _unusable(doc, listings[key], shared, rounds(C.RUN_TAG, doc["source"]), exclude, stores.s3)):
            found = unfit
        elif not found and (broken := stores.unreadable.get(listings[key]["sku"])):
            found = [f"its stored {what} cannot be read ({why}): it has no time to order this decision against; look "
                     f"at {path} first" for what, path, why in broken]
        elif not found and (stored := stores.latest(listings[key]["sku"])) is not None and moment(doc["at"]) < stored:
            found = [f"older than the decision already stored for it ({stored.isoformat()}): an old export"]
        if not found:
            seen.add(key)
        errors += [f"#{i} {key}: {why}" for why in found]
    errors += _one_listing_per_conflict(docs, listings, shared, stores)
    if errors:
        raise InvalidDecisions("\n".join(errors))


def _click(listing, doc):
    """The click a "clean" decision stores for its listing (publish.click_record, the one shape of a click)."""
    return click_record(sku=listing["sku"], listing=listing["key"], source=listing["source"], sha=doc["sha"],
                        at=doc["at"])


def _unusable(doc, listing, shared, rnd, exclude, s3):
    """Why the photos run would never use this clean, [] when it might: a conflict photo is never chosen by itself
    (Selection.blocked), and otherwise the run's own rule (select.click_problem: the click's record, or a current
    verdict or hold a cleaned copy cannot fix; a photo not judged yet is accepted)."""
    group = shared.get((doc["source"], doc["sha"]))
    if group is not None and group.kind == "conflict":
        return [f"photo {doc['sha'][:12]} is a conflict photo, never chosen by itself: assign it with a use"]
    problem = select.click_problem(s3, rnd, listing, _click(listing, doc), exclude)
    return [f"the photos run would not use this clean: {problem}"] if problem else []


def _one_listing_per_conflict(docs, listings, shared, stores):
    """The errors of a "use" giving a conflict photo (listings of several stones show it) to a second listing: it is
    one listing's, by the owner's own decision, until that decision is withdrawn (a removal, or another photo chosen for
    it in this file). Decisions are taken by listing key, so the same file names the same holder."""
    decided = {d.get("id") for d in docs if d.get("action") != "withdraw"}   # a withdraw releases no photo it holds
    holders = {}
    for doc in sorted((d for d in docs if d.get("action") == "use" and d.get("id") in listings), key=lambda d: d["id"]):
        photo = (listings[doc["id"]]["source"], doc["sha"])
        group = shared.get(photo)
        if group is None or group.kind != "conflict":
            continue
        if photo in holders:
            yield f"{doc['id']}: photo {doc['sha'][:12]} is a conflict photo also chosen for {holders[photo]} in this file"
            continue
        others = [k for k in group.listings if k != doc["id"] and k not in decided and k in listings]
        unknown = [(k, path) for k in others if (path := stores.owner_unreadable(listings[k]["sku"]))]
        if unknown:
            yield from (f"{doc['id']}: photo {doc['sha'][:12]} is a conflict photo and {k}'s stored owner choice "
                        f"cannot be read, so whether it holds the photo is unknown; look at {path} first"
                        for k, path in unknown)
            continue
        stored = [k for k in others if (r := stores.owner(listings[k]["sku"])) is not None and r["sha"] == doc["sha"]
                  and r["decided_on"] == r["listing"]]
        if stored:
            yield (f"{doc['id']}: photo {doc['sha'][:12]} is a conflict photo already the owner's choice for "
                   f"{', '.join(stored)}; withdraw that first")
            continue
        holders[photo] = doc["id"]


def _rounds(s3):
    """Round(tag, src), each listed once."""
    seen = {}

    def get(tag, src):
        if (tag, src) not in seen:
            seen[(tag, src)] = select.Round(s3, tag, src)
        return seen[(tag, src)]
    return get


def _choices(docs, listings, shared, stores, exclude, notes):
    """{SKU: the owner choice record it should have} from the "use" decisions: each decided listing's own, then the
    listings of the same stone each photo reaches. A listing reached by several takes the latest decision (then the
    first deciding listing by key); one with its own decision, in this file or stored, keeps it."""
    decided = {listings[d["id"]]["sku"] for d in docs if d["action"] != "withdraw"}   # a withdraw decides no photo
    conflicts = {photo: group.listings for photo, group in shared.items() if group.kind == "conflict"}
    out = {}
    for doc in sorted((d for d in docs if d["action"] == "use"), key=lambda d: d["id"]):
        listing = listings[doc["id"]]
        out[listing["sku"]] = {"tag": doc["tag"], "sha": doc["sha"], "at": doc["at"], "listing": listing["key"],
                               "decided_on": listing["key"]}
        group = shared.get((listing["source"], doc["sha"]))
        if group is None:
            continue
        others = [k for k in group.listings if k != listing["key"]]
        if group.kind == "conflict":
            notes.append(f"{listing['key']}: photo {doc['sha'][:12]} conflict: also shown by {', '.join(others)} "
                         "(another variety or finish), not assigned to it")
            continue
        for key in others:
            sibling = listings[key]
            if sibling["sku"] in decided or stores.own_decision(sibling):
                notes.append(f"{key}: same stone as {listing['key']}, not reached: its own decision stands")
                continue
            if stores.live(sibling, exclude, conflicts):
                notes.append(f"{key}: same stone as {listing['key']}, not reached: it is live with its own photo")
                continue
            stored = stores.owner(sibling["sku"])
            current = out.get(sibling["sku"])
            if stored is not None and moment(stored["at"]) > moment(doc["at"]):
                notes.append(f"{key}: same stone as {listing['key']}, not reached: a later decision stands")
                continue
            if current is None or moment(doc["at"]) > moment(current["at"]):
                out[sibling["sku"]] = {"tag": doc["tag"], "sha": doc["sha"], "at": doc["at"], "listing": key,
                                       "decided_on": listing["key"]}
    return out


def plan(s3, docs, sheet, exclude):
    """The changes the decisions make (Plan), every decision checked first: InvalidDecisions names each bad one. Reads
    only; the same decisions against the same stores give the same plan, whatever their order in the file."""
    listings = {l["key"]: l for l in sheet}
    stores = _Stores(s3, listings)
    try:                                              # the produce's conflict set, the sheet's sibling groups
        shared = index.groups(sheet, read_conflicts(s3, {l["source"] for l in sheet}))
    except NoConflictSet as exc:
        raise InvalidDecisions(str(exc)) from exc
    _check(docs, listings, _rounds(s3), stores, exclude, shared, image_pipeline_off())
    out = Plan()
    for sku, record in sorted(_choices(docs, listings, shared, stores, exclude, out.notes).items()):
        if stores.owner(sku) != record:
            out.owner[owner_key(ROOT, sku)] = record
            if record["decided_on"] != record["listing"]:
                out.notes.append(f"owner choice {sku} ({record['listing']}): same stone as {record['decided_on']}")
        if stores.removal(sku) is not None:
            out.deletions.append(imagestore.owner_removed_key(sku))
        if sku in stores.click_keys:
            out.deletions.append(stores.click_keys[sku])
    for doc in sorted((d for d in docs if d["action"] == "remove"), key=lambda d: d["id"]):
        listing = listings[doc["id"]]
        record = {"sku": listing["sku"], "listing": listing["key"], "source": listing["source"],
                  "reason": C.REMOVED_REASON, "at": doc["at"]}
        if stores.removal(listing["sku"]) != record:
            out.removals[imagestore.owner_removed_key(listing["sku"])] = record
        out.deletions += [k for k in (owner_key(ROOT, listing["sku"]), f"{chosen_prefix(ROOT)}{listing['key']}.json")
                          if k in stores.stored]
        if listing["sku"] in stores.click_keys:
            out.deletions.append(stores.click_keys[listing["sku"]])
    for doc in sorted((d for d in docs if d["action"] == "clean"), key=lambda d: d["id"]):
        listing = listings[doc["id"]]
        record = _click(listing, doc)
        if stores.click(listing["sku"]) != record:
            out.clicks[listing["sku"]] = record
        lifted = [imagestore.owner_removed_key(listing["sku"])]
        if owner_key(ROOT, listing["sku"]) in stores.owner_keys:     # the owner's choice, with the record made from it
            lifted += [owner_key(ROOT, listing["sku"]), f"{chosen_prefix(ROOT)}{listing['key']}.json"]
        out.deletions += [k for k in lifted if k in stores.stored]
    for doc in sorted((d for d in docs if d["action"] == "withdraw"), key=lambda d: d["id"]):
        if (sku := listings[doc["id"]]["sku"]) in stores.click_keys:
            out.deletions.append(stores.click_keys[sku])
    return out


def photo_listings(s3):
    """The listings decisions are about: the photo process's own (standardise_run.photo_listings, from the sheet and its
    photo list), never a tile listing (rendered from its variety's slab stone at publish)."""
    return proc.photo_listings(*sheet_and_photos(s3))


def describe(p, say=print):
    """The plan, one line per change, then what reaches whom."""
    for key, record in sorted(p.owner.items()):
        say(f"   owner choice {imagestore.sku_of(key, owner_prefix(ROOT))} ({record['listing']}): "
            f"{record['tag']} {record['sha'][:12]}")
    for key, record in sorted(p.removals.items()):
        say(f"   removed {record['sku']} ({record['listing']}): {key}")
    for record in sorted(p.clicks.values(), key=lambda r: r["sku"]):
        say(f"   clean {record['sku']} ({record['listing']}): {record['sha'][:12]}")
    for key in sorted(p.deletions):
        say(f"   replaced by the owner's later decision: {key}")
    for note in p.notes:
        say(f"   {note}")
    say(f"==> {len(p.owner)} owner choices, {len(p.removals)} removals, {len(p.clicks)} cleans, "
        f"{len(p.deletions)} records replaced"
        + ("; nothing to change" if p.empty() else ""))


def apply(s3, p):
    """Write the plan: the new records first (a click through publish.write_click), then what they replace."""
    for key, record in sorted({**p.owner, **p.removals}.items()):
        s3.put_object(Bucket=S3_BUCKET, Key=key, Body=json.dumps(record, sort_keys=True).encode(),
                      ContentType="application/json")
    for sku, record in sorted(p.clicks.items()):
        write_click(s3, ROOT, **record)
    for key in sorted(p.deletions):
        s3.delete_object(Bucket=S3_BUCKET, Key=key)


def decide(s3, docs, dry_run, say=print):
    """The Plan of the decisions (described through `say`), and unless dry_run applied inside the photo process's run
    lock (planned again there, so it is written against the stores as they are under the lock). The one way decisions
    reach the stores: this CLI and the config API's photo routes both come here. InvalidDecisions names each bad
    decision, RunActive says a photos run holds the lock; either way nothing is written."""
    if dry_run:
        p = plan(s3, docs, photo_listings(s3), excluded())
        describe(p, say)
        return p
    run_id = "decisions-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with proc.exclusive(s3, {"run_id": run_id, "tags": list(TAGS)}):
        p = plan(s3, docs, photo_listings(s3), excluded())
        describe(p, say)
        apply(s3, p)
    return p


def main() -> int:
    if not os.environ.get("SCRAPER_ENV", "").strip():
        print("ERROR: SCRAPER_ENV must be set explicitly (development or production): it picks the bucket written to")
        return 2
    print(f"==> environment {ENV_NAME}, bucket {S3_BUCKET}")
    dry = _FLAG.get(os.environ.get("DRY_RUN", "").strip().lower())
    if dry is None:
        print("ERROR: DRY_RUN must be true (print the plan) or false (apply); nothing done")
        return 2
    path = os.environ.get("DECISIONS_FILE", "").strip()
    if not path:
        print("ERROR: DECISIONS_FILE (the decision page's export) is required; nothing done")
        return 2
    try:
        docs = read_decisions(Path(path).read_text())
    except (OSError, InvalidDecisions) as exc:
        print(f"ERROR: {exc}; nothing applied")
        return 2
    s3 = boto3.client("s3", region_name=S3_REGION, config=aws_config())
    print(f"==> {len(docs)} owner decisions from {path}")
    try:
        decide(s3, docs, dry)
        if dry:
            print("==> DRY_RUN: nothing written")
            return 0
    except InvalidDecisions as exc:
        print(f"ERROR: invalid decisions, nothing applied:\n{exc}")
        return 2
    except proc.RunActive as exc:
        print(f"ERROR: {exc}; apply after the photos run")
        return 4
    print("==> applied: the next photos run (POST /config/v1/run {\"stage\": \"photos\"}) cleans, chooses, publishes "
          "and links them")
    return 0


if __name__ == "__main__":
    sys.exit(main())
