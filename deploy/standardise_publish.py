"""Publish the chosen photos (deploy.standardise_select: one record per listing under <CLEAN_TAG>/chosen/, together the
complete choice whatever runs made them): each listing's one good photo, its views
(showroom, white, side view, close-up) copied from the run that made it into <env>/products/standard/<view>/<src>/<sha>.jpg,
where the image stage links products to them. A chosen photo of a whole slab (a single slab or one half of an open book:
its run saved <sha>.slab.png) also gets its 3D slab, made here from that slab (stone_pipeline.standardise.cutout) into
<env>/products/standard/cutout/<src>/<sha>.webp + .json. The outline carries what it was made from (the slab's key and
ETag, CUTOUT_VERSION), so a 3D slab is made again exactly when that changed: a listing's choice moved to another run of
the same photo, a later run re-made the slab, or the cut-out itself changed.
The folder is made to match the choice exactly: a view or 3D slab no longer chosen is removed, so a photo the judge or a
person turned down never stays live. Only a view that is missing or other than its run's is copied (server-side, same
bytes) and a cut-out is deterministic, so a re-run is safe and changes nothing. A photo on the reviewed exclude list
(EXCLUDE_PATH, one sha per line) is never published, whatever chose it. No choice records at all is refused: the folder
would be emptied. The last step of the photo process (deploy.standardise_run), inside its lock.
A tile listing is never shown from its own photos (owner 2026-11-02: warehouse racks): its views and 3D tile are rendered
here from its variety's published slab stone (stone_pipeline.standardise.tile_plan), once the slabs' 3D cut-outs are
made, under the listing's own name (imagestore.listing_sha), with a record of what they were made from written last
(<env>/products/standard/tile/<src>/<listing>.json). They are rendered again exactly when that changed: the slab chosen or
its content, the listing's size or finish, or TILE_RENDER_VERSION. A tile listing without such stone gets no files.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
from botocore.exceptions import BotoCoreError, ClientError

from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET, STANDARDISE as C
from stone_pipeline.io import imagestore
from stone_pipeline.standardise import cutout, tile_plan, tile_stone

WORKERS = 32
EXCLUDE_PATH = Path(__file__).resolve().parents[1] / "stone_pipeline" / "config" / "standardise_exclude.txt"
_SHA = re.compile(r"^([0-9a-f]{64})\b")
_MADE_FROM = "made-from"                     # the outline's S3 metadata: what its 3D slab was made from (made_from)


def chosen_prefix(root):
    """Where the choice of each listing is kept: <clean tag root>chosen/<listing>.json, one record per listing with a good
    photo."""
    return f"{root}chosen/"


def owner_prefix(root):
    """Where the owner's photo choice of each listing is kept (deploy.standardise_decisions): <clean tag root>owner/<SKU>
    .json, one record per listing by SKU (a variety re-bind moves the handle, never the SKU)."""
    return f"{root}{C.OWNER_DIR}/"


def owner_key(root, sku):
    return f"{owner_prefix(root)}{imagestore.sku_file(sku)}"


def clicks_prefix(root):
    """Where the owner's clicks to clean a photo are kept: <clean tag root>clicks/<SKU>.json, one per listing by SKU."""
    return f"{root}{C.CLICKS_DIR}/"


class NoConflictSet(RuntimeError):
    """A source on the sheet has no stored conflict set: no produce has stored it yet (every produce stores one per
    source, an empty one too), so nothing can tell which of its photos are conflicts."""


def read_conflicts(client, sources):
    """{(source, sha): listing handles} of the produce's stored conflict sets (imagestore.conflicts_key): the one set the
    photo process, the owner's decisions and the review page work with. NoConflictSet when one of `sources` (the sheet's)
    has none: never read as "no conflicts"."""
    keys = sorted(k for k in _objects(client, imagestore.conflicts_prefix()) if imagestore.conflicts_source(k))
    if missing := sorted(set(sources) - {imagestore.conflicts_source(k) for k in keys}):
        raise NoConflictSet(f"no conflict set yet for {', '.join(missing)}: run a produce first")
    sets = pool_map(WORKERS, lambda k: json.loads(client.get_object(Bucket=S3_BUCKET, Key=k)["Body"].read()), keys)
    return {(imagestore.conflicts_source(k), sha): tuple(handles)
            for k, photos in zip(keys, sets) for sha, handles in sorted(photos.items())}


def read_exclude(text):
    """The shas on the exclude list: the first word of each line, comments and blank lines ignored."""
    return {m.group(1) for line in text.splitlines() if (m := _SHA.match(line.strip()))}


def copies(chosen, made, exclude=frozenset()):
    """[(run key, standard key)] for every view the run made of each chosen photo not on the exclude list, sorted.
    made = the run keys that exist."""
    out = set()
    for c in chosen:
        if c["sha"] in exclude:
            continue
        for view in imagestore.STANDARD_VIEWS:
            key = f"{c['prefix']}{c['sha']}.{view}.jpg"
            if key in made:
                out.add((key, imagestore.standard_key(c["source"], c["sha"], view)))
    return sorted(out)


def slabs(chosen, made, exclude=frozenset()):
    """{(source, sha): its run's slab key} for every chosen photo not on the exclude list whose run saved a whole slab:
    the photos that get a 3D slab."""
    return {(c["source"], c["sha"]): key for c in chosen
            if c["sha"] not in exclude and (key := f"{c['prefix']}{c['sha']}.slab.png") in made}


def cutout_keys(slab):
    """The two published files of one photo's 3D slab, the outline last (written last: it means the pair is complete)."""
    return [imagestore.cutout_key(*slab, "webp"), imagestore.cutout_key(*slab, "json")]


def made_from(slab_key, etag):
    """What a 3D slab is made from: the slab's key and content (ETag) and the cut-out's version."""
    return f"{slab_key} {etag.strip(chr(34))} v{C.CUTOUT_VERSION}"


def outdated(todo, made, published, stamps):
    """The (source, sha) of `todo` whose 3D slab is missing, half written or made from something else than the slab now
    chosen, sorted. made, published = {key: ETag}; stamps = {(source, sha): what its published outline was made from}."""
    return sorted(s for s, slab in todo.items()
                  if any(k not in published for k in cutout_keys(s)) or stamps.get(s) != made_from(slab, made[slab]))


def unpublished(plan, made, published):
    """The copies of `plan` whose published file is missing or other than the run's (ETag: a server-side copy keeps it).
    An unchanged view is never written again: the versioned bucket would keep every rewrite, and nothing changes for the
    produce. made, published = {key: ETag}."""
    return [(src, dst) for src, dst in plan if published.get(dst) != made[src]]


def stale(published, keep):
    """The published keys the choice no longer has: removed, so the folder matches the choice."""
    return sorted(set(published) - set(keep))


def _objects(client, prefix):
    """{key: ETag} under a prefix."""
    return {o["Key"]: o["ETag"] for page in client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix)
            for o in page.get("Contents", [])}


def _stamp(client, slab, published):
    """What the published outline of one photo's 3D slab was made from, None when it is not published."""
    meta_key = cutout_keys(slab)[1]
    if meta_key not in published:
        return None
    return client.head_object(Bucket=S3_BUCKET, Key=meta_key)["Metadata"].get(_MADE_FROM)


def _publish_cutout(client, slab, slab_key, etag):
    """Make and write one photo's 3D slab. The outline goes first and comes back last, so a pair cut short never looks
    complete. A slab that is not one (ValueError) is reported and that photo's 3D slab removed: that product simply has
    no 3D, the run goes on."""
    webp_key, meta_key = cutout_keys(slab)
    client.delete_object(Bucket=S3_BUCKET, Key=meta_key)
    try:
        webp, meta = cutout.make(client.get_object(Bucket=S3_BUCKET, Key=slab_key)["Body"].read())
    except ValueError as exc:
        print(f"   NO 3D {slab[0]}/{slab[1][:12]}: {exc}", flush=True)
        client.delete_object(Bucket=S3_BUCKET, Key=webp_key)
        return False
    client.put_object(Bucket=S3_BUCKET, Key=webp_key, Body=webp, ContentType="image/webp")
    client.put_object(Bucket=S3_BUCKET, Key=meta_key, Body=meta, ContentType="application/json",
                      Metadata={_MADE_FROM: made_from(slab_key, etag)})
    return True


def pool_map(workers, work, items):
    """[work(item)] in the items' order, `workers` at a time. On any exception (a SIGTERM's SystemExit included) the
    items not started are cancelled at once and the exception goes on, so a stopping run releases its lock within its
    grace instead of finishing the whole list first."""
    pool = ThreadPoolExecutor(workers)
    try:
        out = list(pool.map(work, items))
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    return out


def _text(client, key):
    return client.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read().decode("utf-8-sig")


def _stone(client, slab):
    """The stone of a slab's published cut-out. tile_stone.Refused when it is not used (the reason)."""
    rgba = tile_stone.decode(client.get_object(Bucket=S3_BUCKET, Key=slab.cutout)["Body"].read())
    return tile_stone.from_cutout(rgba, *slab.size)


def _check(client, slab):
    """The usable stone (square metres) of a slab's published cut-out, or the tile_stone.Refused saying why it is not
    used. The decoded stone is dropped: only the slab a tile is cut from is decoded again."""
    try:
        return tile_stone.usable_area(_stone(client, slab))
    except tile_stone.Refused as exc:
        return exc


# What one tile listing's step may meet that is its alone (a cut-out or record that cannot be read or decoded, an image
# that cannot be encoded): the listing is set aside and the run goes on. Anything else is a bug and fails the run.
_TILE_ERRORS = (ClientError, BotoCoreError, cv2.error, ValueError, OSError)


_MISSING = ("NoSuchKey", "404", "NotFound")


def _record(client, key):
    """A tile listing's record (what its files were made from), None when it is missing or is not one (corrupt): the
    record is our own file, written last, so either means the set is not known to be complete. Any other read error
    (S3 failing) is raised: then the record's state is unknown, which is not the same as gone."""
    try:
        body = client.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in _MISSING:
            return None
        raise
    try:
        record = json.loads(body.decode("utf-8-sig"))
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _tile_current(client, files, published, stamp):
    """(whether a tile listing's files are all published and its record says they were made from `stamp`, a note when
    its record could not be read: the set is then out of date and is rendered again)."""
    if any(k not in published for k in files):
        return False, ""
    try:
        record = _record(client, files[-1])
    except (ClientError, BotoCoreError):
        record = None
    if record is None:
        return False, "its record could not be read: rendered again"
    return record.get("stamp") == stamp, ""


def _publish_tile(client, product, slab, stone, cuts):
    """Render and write one tile listing's files. Its record and main view go first and come back last, so a set cut
    short never looks complete (the image stage links a listing by its main view; this step by its record)."""
    files = tile_plan.render(product, slab, stone, cuts)
    record, main = list(files)[-1], list(files)[0]
    for key in (record, main):
        client.delete_object(Bucket=S3_BUCKET, Key=key)
    for key in [k for k in files if k not in (record, main)] + [main, record]:
        body, kind = files[key]
        client.put_object(Bucket=S3_BUCKET, Key=key, Body=body, ContentType=kind)


class _OneStone:
    """The decoded stone of one slab at a time (a slab is tens of MB): asking for another drops the one held first."""

    def __init__(self, client):
        self.client, self.slab, self.stone = client, None, None

    def drop(self):
        self.slab, self.stone = None, None

    def __call__(self, slab):
        if slab != self.slab:
            self.drop()
            self.stone, self.slab = _stone(self.client, slab), slab
        return self.stone


def _tile(client, product, ranked, stone_of, published):
    """One tile listing: (its files, whether they were rendered now, a note), or (None, False, why it has none). Its
    slab is the first of `ranked` on which a tile of its size fits."""
    misfits = []
    for slab in ranked:
        stone = stone_of(slab)
        try:
            cuts = tile_plan.faces_of(product, stone)
        except tile_stone.Refused as exc:
            misfits.append(f"{slab.source}/{slab.sha[:12]}: {exc}")
            continue
        files = tile_plan.keys(product)
        current, note = _tile_current(client, files, published, tile_plan.stamp(product, slab))
        if current:
            return files, False, ""
        _publish_tile(client, product, slab, stone, cuts)
        return files, True, note
    return None, False, "; ".join(misfits)


def _still_its_stone(client, product, published, slabs):
    """The files a tile listing whose step failed keeps: those it has, while its record says they were cut from one of
    `slabs` (the slabs of its variety and finish it may be cut from now), or while its record cannot be read for an S3
    failure (unknown is not gone: an S3 outage must never remove every tile); none when its record is missing, corrupt
    or names another stone, so it hides until it renders again."""
    own = imagestore.listing_sha(product.key)
    current = [k for k in published if own in k]
    try:
        record = _record(client, imagestore.tile_record_key(product.source, own))
    except (ClientError, BotoCoreError):
        return current
    stone = (record or {}).get("stone")
    if not isinstance(stone, dict) or stone.get("cutout") not in {s.cutout for s in slabs}:
        return []
    return current


def publish_tiles(client, sheet, photos, chosen, cutouts, published, say=print):
    """Render the tile listings of the sheet from their slab stone: {"keep", "listings", "shown", "rendered", "hidden",
    "problems", "unknown_variations"}. sheet, photos = the product sheet's text and its photo list (index.rows); chosen
    = {listing: choice record} of the photo listings; cutouts = {key: ETag} of the slabs' published cut-outs;
    published = {key: ETag} under the standard folder. keep: every file a shown tile
    listing has, and the files a tile listing whose step failed (_TILE_ERRORS) already had while their stone is still
    one it may be cut from (the run goes on); hidden: {listing: why it gets none}; problems: {listing: what its step
    met: an error, or a record it could not read (rendered again)}; failed: the listings whose step failed. One
    (variety, finish) at a time and one decoded slab at a time. Listing the bucket is the caller's: a failed listing
    stops publish before any removal; any other error fails the run."""
    tiles = tile_plan.tiles(tile_plan.products(sheet, photos, {}))
    if not tiles:
        return {"keep": [], "listings": 0, "shown": 0, "rendered": 0, "hidden": {}, "problems": {}, "failed": [],
                "unknown_variations": 0}
    names = tile_plan.variety_names(_text(client, f"{ENV_SEGMENT}/{C.VARIANTS_KEY}"))
    attributes = _text(client, f"{ENV_SEGMENT}/{C.ATTRIBUTES_KEY}")
    finishes = tile_plan.finish_ids(attributes)
    everything = tile_plan.products(sheet, photos, names)
    unknown = sum(p.variety is None and p.fmt in (C.TILE_FORMAT, C.CUTOUT_FORMAT) for p in everything)
    if unknown:
        say(f"==> the variations export {'is empty' if not names else 'is stale'}: {unknown} tile and slab products' "
            f"variations are not in it, so they are matched to no stone ({ENV_SEGMENT}/{C.VARIANTS_KEY})")
    stone_of = tile_plan.slabs(everything, chosen, cutouts)
    hidden, problems, keep, rendered, groups = {}, {}, [], 0, {}
    for t in tile_plan.tiles(everything):
        why = tile_plan.no_stone(t, everything, finishes)
        if not why and not stone_of.get((t.variety, t.finish)):
            why = "no published slab stone (3D cut-out) of this variety in this finish"
        if why:
            hidden[t.key] = why
        else:
            groups.setdefault((t.variety, t.finish), []).append(t)

    failed = set()

    def fail(t, exc, slabs):
        # this listing's alone: it keeps the files it has while their stone is still one of `slabs`, and is listed
        problems[t.key] = f"{type(exc).__name__}: {exc}"
        failed.add(t.key)
        keep.extend(_still_its_stone(client, t, published, slabs))

    one = _OneStone(client)
    for group in sorted(groups):
        one.drop()                                     # the checks below decode each slab of the group in turn
        try:
            checks = {s: _check(client, s) for s in stone_of[group]}
        except _TILE_ERRORS as exc:
            # a cut-out of the group cannot be read: no ranking, so its tiles keep files cut from any of its slabs
            for t in groups[group]:
                fail(t, exc, stone_of[group])
            continue
        areas = {s.cutout: v for s, v in checks.items() if not isinstance(v, tile_stone.Refused)}
        refused = "; ".join(f"{s.source}/{s.sha[:12]}: {v}" for s, v in checks.items() if isinstance(v, tile_stone.Refused))
        ranked = {t: tile_plan.ranked(stone_of[group], t.source, areas) for t in groups[group]}
        for t in sorted(groups[group], key=lambda t: ([s.cutout for s in ranked[t]], t.key)):
            if not ranked[t]:
                hidden[t.key] = f"every slab of this variety and finish was refused: {refused}"
                continue
            try:
                files, now, note = _tile(client, t, ranked[t], one, published)
            except _TILE_ERRORS as exc:
                fail(t, exc, ranked[t])
                continue
            if files is None:
                hidden[t.key] = f"no tile of this size fits on any usable slab: {note}"
                continue
            if note:
                problems[t.key] = note
            keep += files
            rendered += now
    for key in sorted(hidden):
        say(f"   NO TILE IMAGES {key}: {hidden[key]}")
    for key in sorted(problems):
        say(f"   TILE {'FAILED' if key in failed else 'NOTE'} {key}: {problems[key]}")
    shown = len(tiles) - len(hidden) - len(failed)
    say(f"==> tiles: {shown} of {len(tiles)} listings shown from slab stone, {rendered} rendered (the rest unchanged), "
        f"{len(hidden)} without stone, {len(failed)} failed")
    return {"keep": keep, "listings": len(tiles), "shown": shown, "rendered": rendered, "hidden": hidden,
            "problems": problems, "failed": sorted(failed), "unknown_variations": unknown}


class NoChoiceRecords(RuntimeError):
    """No listing has a choice record yet: publishing would empty the folder, so nothing is published or removed."""


def assignment_changes(assigned, published, current):
    """(writes {key: body}, removals [key]) that make the assignment files match `assigned` for the published photos:
    {(source, sha): SKUs} of every conflict photo (index.shared_photos) with the SKUs the owner assigned it to. A file
    is kept only for a published photo assigned to someone; current = {key: its stored body}."""
    wanted = {imagestore.assigned_key(*photo): json.dumps(sorted(skus)).encode() for photo, skus in assigned.items()
              if skus and imagestore.standard_key(*photo, "main") in published}
    writes = {k: body for k, body in sorted(wanted.items()) if current.get(k) != body}
    return writes, sorted(set(current) - set(wanted))


def publish(client, root, sheet, photos, say=print, *, assigned):
    """Make the published folder match the choice records under `root` (the clean tag's root) and the tile listings of
    `sheet` (the product sheet's text, with its photo list `photos`): {"listings", "copied", "made_3d", "removed", "tiles" (counts), "tiles_hidden"
    ({listing: why it has no images}), "tiles_problems" ({listing: the error its tile step met}), "changed"}; changed =
    the folder is not what it was (a produce must link it again). NoChoiceRecords when no photo listing has one (the
    folder would be emptied). assigned = {(source, sha): SKUs} of every photo listings of several stones show (the
    produce's conflict set): one is published only when the owner assigned it to someone, with a file naming who, so the
    produce links it for those products only (a record that names it otherwise is a choice made before it was a
    conflict: not published)."""
    records = list(_objects(client, chosen_prefix(root)))
    chosen = pool_map(WORKERS, lambda k: json.loads(client.get_object(Bucket=S3_BUCKET, Key=k)["Body"].read()), records)
    if not chosen:
        raise NoChoiceRecords(f"no choice records under {chosen_prefix(root)}: nothing published or removed")
    # a tile listing chosen before tiles were rendered is shown from its stone now, never from its own photo
    tile_listings = {t.key for t in tile_plan.tiles(tile_plan.products(sheet, photos, {}))}
    chosen = [c for c in chosen if c["key"] not in tile_listings]
    if not chosen:
        raise NoChoiceRecords(f"no choice records of photo listings under {chosen_prefix(root)}: nothing published or "
                              f"removed")
    chosen = [c for c in chosen if assigned.get((c["source"], c["sha"]), True)]
    made = {k: t for prefix in sorted({c["prefix"] for c in chosen}) for k, t in _objects(client, prefix).items()}
    exclude = read_exclude(EXCLUDE_PATH.read_text())
    plan, todo = copies(chosen, made, exclude), slabs(chosen, made, exclude)
    folders = [*map(imagestore.standard_prefix, (*imagestore.STANDARD_VIEWS, imagestore.CUTOUT_SUBDIR, imagestore.TILE_SUBDIR))]
    published = {k: t for prefix in folders for k, t in _objects(client, prefix).items()}
    fresh = unpublished(plan, made, published)
    stamps = dict(zip(todo, pool_map(WORKERS, lambda s: _stamp(client, s, published), todo)))
    remake = outdated(todo, made, published, stamps)
    files = list(_objects(client, imagestore.assigned_prefix()))
    current = dict(zip(files, pool_map(WORKERS, lambda k: client.get_object(Bucket=S3_BUCKET, Key=k)["Body"].read(), files)))
    writes, unassigned = assignment_changes(assigned, {t for _, t in plan}, current)
    # A conflict photo's assignment file goes up before its views, and a stale one comes down after the views it covered:
    # a publish stopped half way never leaves a conflict photo's view without the file that says whose it is.
    for key, body in writes.items():
        client.put_object(Bucket=S3_BUCKET, Key=key, Body=body, ContentType="application/json")
    pool_map(WORKERS, lambda kv: client.copy_object(Bucket=S3_BUCKET, Key=kv[1], CopySource={"Bucket": S3_BUCKET, "Key": kv[0]},
                                                    ContentType="image/jpeg", MetadataDirective="REPLACE"), fresh)
    # each cut-out holds a full-size slab in memory
    made_3d = sum(pool_map(C.CUTOUT_WORKERS, lambda s: _publish_cutout(client, s, todo[s], made[todo[s]]), remake))
    slab_3d = {k for s in todo for k in cutout_keys(s)}
    cutouts = {k: t for k, t in _objects(client, imagestore.cutout_prefix()).items() if k in slab_3d}
    tiles = publish_tiles(client, sheet, photos, {c["key"]: c for c in chosen}, cutouts, published, say)
    gone = stale(published, [t for _, t in plan] + sorted(slab_3d) + tiles["keep"])
    pool_map(WORKERS, lambda k: client.delete_object(Bucket=S3_BUCKET, Key=k), gone)
    for key in unassigned:                             # only once the views it covered are gone
        client.delete_object(Bucket=S3_BUCKET, Key=key)
    say(f"==> published {len(chosen)} listings' photos: {len(fresh)} of {len(plan)} views copied (the rest unchanged), "
        f"made {made_3d} of {len(remake)} 3D slabs, removed {len(gone)} files no longer chosen; "
        f"{len(writes)} conflict photos assigned, {len(unassigned)} assignments removed")
    counts = ({k: tiles[k] for k in ("listings", "shown", "rendered", "unknown_variations")}
              | {"hidden": len(tiles["hidden"]), "failed": len(tiles["failed"]),
                 "failed_listings": tiles["failed"][:C.TILE_FAILED_LISTED]})
    return {"listings": len(chosen), "copied": len(fresh), "made_3d": made_3d, "removed": len(gone), "tiles": counts,
            "tiles_hidden": tiles["hidden"], "tiles_problems": tiles["problems"],
            "changed": bool(fresh or remake or gone or tiles["rendered"] or writes or unassigned)}
