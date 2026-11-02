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
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from stone_pipeline.config.settings import S3_BUCKET, STANDARDISE as C
from stone_pipeline.io import imagestore
from stone_pipeline.standardise import cutout

WORKERS = 32
EXCLUDE_PATH = Path(__file__).resolve().parents[1] / "stone_pipeline" / "config" / "standardise_exclude.txt"
_SHA = re.compile(r"^([0-9a-f]{64})\b")
_MADE_FROM = "made-from"                     # the outline's S3 metadata: what its 3D slab was made from (made_from)


def chosen_prefix(root):
    """Where the choice of each listing is kept: <clean tag root>chosen/<listing>.json, one record per listing with a good
    photo."""
    return f"{root}chosen/"


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


class NoChoiceRecords(RuntimeError):
    """No listing has a choice record yet: publishing would empty the folder, so nothing is published or removed."""


def publish(client, root, say=print):
    """Make the published folder match the choice records under `root` (the clean tag's root): {"listings", "copied",
    "made_3d", "removed", "changed"}; changed = the folder is not what it was (a produce must link it again).
    NoChoiceRecords when there are none at all."""
    records = list(_objects(client, chosen_prefix(root)))
    chosen = pool_map(WORKERS, lambda k: json.loads(client.get_object(Bucket=S3_BUCKET, Key=k)["Body"].read()), records)
    if not chosen:
        raise NoChoiceRecords(f"no choice records under {chosen_prefix(root)}: nothing published or removed")
    made = {k: t for prefix in sorted({c["prefix"] for c in chosen}) for k, t in _objects(client, prefix).items()}
    exclude = read_exclude(EXCLUDE_PATH.read_text())
    plan, todo = copies(chosen, made, exclude), slabs(chosen, made, exclude)
    published = {k: t for prefix in [*map(imagestore.standard_prefix, imagestore.STANDARD_VIEWS), imagestore.cutout_prefix()]
                 for k, t in _objects(client, prefix).items()}
    fresh = unpublished(plan, made, published)
    gone = stale(published, [t for _, t in plan] + [k for s in todo for k in cutout_keys(s)])
    stamps = dict(zip(todo, pool_map(WORKERS, lambda s: _stamp(client, s, published), todo)))
    remake = outdated(todo, made, published, stamps)
    pool_map(WORKERS, lambda kv: client.copy_object(Bucket=S3_BUCKET, Key=kv[1], CopySource={"Bucket": S3_BUCKET, "Key": kv[0]},
                                                    ContentType="image/jpeg", MetadataDirective="REPLACE"), fresh)
    pool_map(WORKERS, lambda k: client.delete_object(Bucket=S3_BUCKET, Key=k), gone)
    # each cut-out holds a full-size slab in memory
    made_3d = sum(pool_map(C.CUTOUT_WORKERS, lambda s: _publish_cutout(client, s, todo[s], made[todo[s]]), remake))
    say(f"==> published {len(chosen)} listings' photos: {len(fresh)} of {len(plan)} views copied (the rest unchanged), "
        f"made {made_3d} of {len(remake)} 3D slabs, removed {len(gone)} files no longer chosen")
    return {"listings": len(chosen), "copied": len(fresh), "made_3d": made_3d, "removed": len(gone),
            "changed": bool(fresh or remake or gone)}
