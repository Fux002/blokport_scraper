"""Emit the complete variant upload file -- one deterministic, isolated step.

The catalog is a pure function of two inputs that NEVER alias:
  - the immutable Medusa export (existing variants, download-only), and
  - this run's scraped additions (new variants + confirmed alias updates).

This stage assembles the full file the user uploads, so nothing is hand-merged or
stamped in place and re-running yields an identical file:

  existing export            ─┐
  1_variants_update.csv       ├─►  1_variants_full.csv    (existing ∪ new ∪ mirror,
  mirror backbones (tiles)   ─┘                            Image + Volume stamped)
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from stone_pipeline.config.settings import CATEGORIES, SETTINGS, category, category_for_key
from stone_pipeline.core import csvio, logfmt
from stone_pipeline.core.text import (
    clean_alias_list,
    clean_variety_name,
    looks_like_artifact,
    split_bracket_alias,
    title_case,
)

log = logfmt.get_logger("emit_catalog")

_COLS = ["Key", "Name", "Image", "Aliases", "Volume per kg (m³/kg)"]


def _s3_variation_keys():
    """Set of variant Keys that have a {Key}.png in <env>/variations/ on S3, via ONE list call --
    or None when S3 is genuinely unreachable (no boto3/creds, e.g. CI/local), so the caller falls
    back. READ-ONLY, so it runs even under s3.dry_run (dry_run only suppresses WRITES; variant images
    are uploaded out-of-band by the image_pipeline). This is the authority for whether a variant may
    advertise its image -- a Key with no object must NOT point Medusa at a 404."""
    from stone_pipeline.config.settings import ENV_SEGMENT
    s3 = SETTINGS.s3
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        return None
    try:
        # bounded timeouts + capped retries so a network stall can't hang the build (boto3's default
        # has no connect-timeout cap); standard retry mode for transient throttling.
        cfg = Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 3, "mode": "standard"})
        client = boto3.Session(profile_name=s3.credentials_profile or None,
                               region_name=s3.region).client("s3", config=cfg)
        prefix = f"{ENV_SEGMENT}/variations/"
        keys: set[str] = set()
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=s3.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                name = obj["Key"][len(prefix):]
                if name.endswith(".png"):
                    keys.add(name[:-4])
        return keys
    except Exception:
        return None


def _consolidate(rows: list[dict]) -> list[dict]:
    """Fold ONLY variants whose name carries a trailing grade code into their base variety, so a
    graded family becomes ONE searchable variety with the originals as aliases ('Rosal C'/'Rosal
    T'/'Rosal B' -> 'Rosal'). Every other variant passes through UNTOUCHED -- distinct same-name
    variants are NOT deduped and real names ('Rosal Codacal Favor', 'Agata Black') never merge,
    because only a row whose clean_variety_name DIFFERS from its name (a code was stripped) is
    folded. If a base with the clean name already exists it absorbs the code as an alias; otherwise
    the first coded row is renamed to the base (its Key survives the in-place Medusa rename)."""
    coded, passthrough = [], []
    for r in rows:
        clean = clean_variety_name(r["Name"]) or r["Name"]
        (coded if clean.casefold() != r["Name"].casefold() else passthrough).append((clean, r))
    out = [r for _, r in passthrough]
    base_by: dict[tuple, dict] = {}
    for r in out:                                                      # existing real bases to fold into
        cat = category_for_key(r["Key"])
        base_by.setdefault(((cat.label if cat else ""), r["Name"].casefold()), r)
    for clean, r in coded:
        cat = category_for_key(r["Key"])
        gkey = ((cat.label if cat else ""), clean.casefold())
        target = base_by.get(gkey)
        new_aliases = {r["Name"].strip()} | {a.strip() for a in (r.get("Aliases") or "").split("|") if a.strip()}
        if target is None:                                            # no base yet -> this row becomes it
            nr = dict(r); nr["Name"] = clean
            nr["Aliases"] = "|".join(sorted(a for a in new_aliases if a.casefold() != clean.casefold()))
            base_by[gkey] = nr
            out.append(nr)
        else:                                                         # fold the code into the base
            cur = {a.strip() for a in (target.get("Aliases") or "").split("|") if a.strip()}
            target["Aliases"] = "|".join(sorted((cur | new_aliases) - {""}
                                                - {a for a in (cur | new_aliases) if a.casefold() == clean.casefold()}))
            target["Image"] = target.get("Image") or r.get("Image") or ""
    return out


def _image_link(key: str, has_export_image: bool, backed: bool,
                s3_keys: set[str] | None, base: str) -> str:
    """The variant's Image cell. When S3 is known (`s3_keys` not None) a variant advertises its
    link IFF the {Key}.png exists -- never a 404. When S3 is unreachable, fall back to the prior
    heuristic (already-imaged OR product-backed) so CI/local output is unchanged."""
    if s3_keys is not None:
        return f"{base}{key}.png" if key in s3_keys else ""
    return f"{base}{key}.png" if (has_export_image or backed) else ""


def _rows(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    with Path(path).open(encoding="utf-8-sig") as h:
        return [{c: (r.get(c) or "") for c in _COLS}
                for r in csv.DictReader(h) if (r.get("Key") or "").strip()]


def _mirror_rows(by_key: dict[str, dict]) -> list[dict]:
    """One variant row per source variety for each active mirror category (tiles
    mirror slabs). The mirror's deterministic Key carries the source variant's
    Name and Aliases, so a tile stays identical to its slab."""
    # keyed by the mirror Key so a Key is emitted ONCE even when two source varieties share the same
    # (stone_type, variant) and both resolve to the same mirror post -- first source wins. A list-append
    # here produced duplicate tile Keys (the downstream dedup only checks against existing variants,
    # not among mirror rows).
    out: dict[str, dict] = {}
    for cat in CATEGORIES:
        if not (cat.mirror_of and cat.active):
            continue
        src = category(cat.mirror_of)
        src_posts = json.loads(src.backbone_path.read_text(encoding="utf-8-sig"))["posts"]
        mir_posts = json.loads(cat.backbone_path.read_text(encoding="utf-8-sig"))["posts"]
        # join slab<->tile on variety identity (type, variant), NOT positional zip: the two
        # backbones can drift in order and a zip would silently mis-pair varieties.
        mir_by = {(mp.get("stone_type"), mp.get("variant")): mp for mp in mir_posts}
        for sp in src_posts:
            s = by_key.get(sp.get("key"))
            mp = mir_by.get((sp.get("stone_type"), sp.get("variant")))
            if s and mp and mp["key"] not in out:
                out[mp["key"]] = {"Key": mp["key"], "Name": s["Name"], "Image": "",
                                  "Aliases": s["Aliases"], "Volume per kg (m³/kg)": ""}
    return list(out.values())


def build(existing_path: Path | None = None, image_keys: set[str] | None = None) -> Path:
    """Assemble to_upload/1_variants_full.csv from the immutable export + this run's
    delta (to_upload/1_variants_update.csv).

    image_keys: the set of variant Keys that actually have a {Key}.png on S3. When provided (or
    resolvable from S3), a variant advertises its image link IFF its image exists -- so the file
    can never point Medusa at a missing image. Pass an explicit set in tests; None resolves it from
    S3, and a None result there (S3 unreachable) falls back to the prior heuristic."""
    existing_path = Path(existing_path or SETTINGS.paths.export_file)
    by_key = {r["Key"]: r for r in _rows(existing_path)}      # existing (read-only)
    order = list(by_key)
    n_existing = len(order)
    for r in _rows(SETTINGS.paths.to_upload_dir / "1_variants_update.csv"):   # new + alias delta
        if r["Key"] in by_key:
            by_key[r["Key"]].update(r)                        # alias update onto existing row
        else:
            by_key[r["Key"]] = r
            order.append(r["Key"])                            # genuinely new variant
    mirror = [m for m in _mirror_rows(by_key) if m["Key"] not in by_key]  # tiles for existing varieties
    rows = [by_key[k] for k in order] + mirror
    # E10: a retired variety (the operator's explicit removal / un-retire memory) never re-enters the
    # upload -- so a produce does not re-serve it, and the ledger row stays 'retiring' until Medusa acks.
    from stone_pipeline.stages import decisions
    retired = decisions.load_retired()
    if retired:
        rows = [r for r in rows if r["Key"] not in retired]
    # keep code-like names (e.g. a stale 'Z Astoria' still in the export) OUT of the upload
    # file -- only clean variety names ever reach Medusa. Their varieties are to be deleted there.
    rows = [r for r in rows if not looks_like_artifact(r["Name"])]
    rows = _consolidate(rows)                                 # 'Rosal C/T/B' -> one 'Rosal' + aliases

    from stone_pipeline.stages.image_prompts import product_backed_keys
    backed = product_backed_keys()                            # variants a product references
    base = SETTINGS.curation.variant_image_base               # dev/prod switch via env
    s3_keys = image_keys if image_keys is not None else _s3_variation_keys()
    for r in rows:
        cat = category_for_key(r["Key"])
        # advertise the image link only when the image really exists (see _image_link): a
        # product-backed variant whose image was never generated -- or whose Key drifted in a
        # catalog rename -- stays BLANK instead of pointing Medusa at a 404.
        r["Image"] = _image_link(r["Key"], bool((r.get("Image") or "").strip()),
                                  r["Key"] in backed, s3_keys, base)
        r["Volume per kg (m³/kg)"] = cat.volume_per_kg if cat else ""
        # Move a '(alternative)' out of the Name into the alias list ('Black Galaxy (Star Galaxy)' ->
        # Name 'Black Galaxy' + alias 'Star Galaxy'), then normalize the whole alias list: split
        # comma-joined blobs into separate aliases, drop 'market:' context prefixes / wrapping brackets
        # / junk, drop any alias equal to the Name, title-case, and de-duplicate. The Key is unchanged
        # (a Name/alias correction, not a re-key), and matching improves because both the clean name and
        # each alternative spelling now resolve.
        clean_name, name_bracket_aliases = split_bracket_alias(r["Name"])
        raw_aliases = [a for a in r["Aliases"].split("|") if a.strip()] + name_bracket_aliases
        r["Name"] = title_case(clean_name)               # consistent casing for the whole catalog
        r["Aliases"] = "|".join(clean_alias_list(clean_name, raw_aliases))

    path = SETTINGS.paths.to_upload_dir / "1_variants_full.csv"
    # atomic, NOT sanitized: 1_variants_full is the Medusa import; a leading "'" prepended to a
    # Name/Alias would corrupt the catalog data. (Operator review of scraped names is the sanitized
    # review files' job, not this machine-consumed deliverable.)
    csvio.write_dicts(path, _COLS, rows, sanitize=False)
    # count GENUINELY new rows from the FINAL set (after artifact-filter + consolidation), not from
    # the pre-filter `order` -- else dropped/folded rows inflate the 'new' metric.
    existing_keys = set(order[:n_existing])
    mirror_keys = {m["Key"] for m in mirror}
    new_count = sum(1 for r in rows if r["Key"] not in existing_keys and r["Key"] not in mirror_keys)
    log.info("variants_full emitted", extra={"extra_fields": {
        "rows": len(rows), "existing": n_existing,
        "new": new_count, "mirror": len(mirror)}})
    return path
