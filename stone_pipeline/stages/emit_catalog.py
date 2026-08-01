"""Emit the complete variant upload file -- one deterministic, isolated step.

The catalog is a pure function of two inputs that NEVER alias:
  - the immutable Medusa export (existing variants, download-only), and
  - this run's scraped additions (new variants + confirmed alias updates).

This stage assembles the full file the user uploads, so nothing is hand-merged or
stamped in place and re-running yields an identical file:

  existing export            ─┐
  1_variants_update.csv       ├─►  1_variants_full.csv    (existing ∪ new ∪ union-fill,
  union gap-fill (all cats)  ─┘                            Image + Volume stamped)
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from stone_pipeline.config.settings import CATEGORIES, SETTINGS, active_categories, category, category_for_key
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


def _s3_access_denied(exc) -> bool:
    """True for an S3 PERMISSION error (an IAM misconfig to fail loud on), vs a transient/network error or
    a missing client (degrade). Read structurally off the botocore ClientError response, so no hard botocore
    import is needed and a non-ClientError (e.g. ImportError) is simply not a permission error."""
    resp = getattr(exc, "response", None)
    code = resp.get("Error", {}).get("Code", "") if isinstance(resp, dict) else ""
    return code in ("AccessDenied", "AllAccessDisabled", "InvalidAccessKeyId",
                    "SignatureDoesNotMatch", "AuthorizationHeaderMalformed", "403")


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
    except Exception as exc:
        # AccessDenied is an IAM MISCONFIG, not "S3 unreachable". Swallowing it returned None, so the caller
        # fell back to the has_export_image/backed heuristic and advertised {Key}.png for objects it never
        # confirmed exist -> Medusa 404s. Fail loud on a permission error; degrade only on transient/network.
        if _s3_access_denied(exc):
            log.error("S3 AccessDenied listing variation images; refusing to guess image links (would 404). "
                      "Fix the task IAM.", extra={"extra_fields": {"error": str(exc)}})
            raise
        log.warning("S3 unreachable listing variation images; image gate falls back to the heuristic",
                    extra={"extra_fields": {"error": str(exc)}})
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


def _posts_of(data) -> list[dict]:
    """The posts of a backbone JSON, tolerant of either committed shape (a bare list OR {"posts": [...]}),
    matching how reference.loaders reads the same files -- so a shape drift degrades a mirror, not crashes."""
    return data.get("posts", data if isinstance(data, list) else []) if isinstance(data, dict) else (
        data if isinstance(data, list) else [])


def _uniform_rows(by_key: dict[str, dict]) -> list[dict]:
    """Category uniformity by UNION: a variety belongs to the STONE, not a format, so every variety present
    in ANY active fan-out category must exist in ALL of them -- no category is a master. Collect the union
    of varieties (identified by (type, clean name)) across every fan-out category, then for each fan-out
    category emit the rows it is MISSING, with a deterministic gen_key and the variety's Name/Aliases.

    Presence is tested by (type, name), NOT by Key: the base carries LEGACY uuid4 keys (from the original
    Medusa export) while gen_key mints uuid5, so a Key match would treat every existing variety as missing
    and duplicate all of them under a second key. Checking the identity means an existing variety -- under
    any key form -- is left untouched (additive; images preserved) and only a genuinely-absent category
    gets a fresh row. This makes the base a fixed point (every category is carried forward) and means a
    block-only variety is added to slab and tile, and a NEWLY activated category is fully backfilled the
    first time it emits, with no code change (the future-category requirement). Derived from the CLEANED
    base rows, never the raw backbone, so cleaned names/aliases are what propagate."""
    from stone_pipeline.reference.loaders import type_slug_from_key
    from stone_pipeline.stages.curate import gen_key
    from stone_pipeline.matching import projections as proj
    # Read the RUNTIME registry (active_categories), never the frozen CATEGORIES tuple: a category's
    # active flag depends on its Medusa pcat, which is refreshed from the on-disk export after import
    # (refresh_category_pcats). Iterating CATEGORIES here would gate the union-fill on the stale import
    # snapshot, so when block/tile pcats were absent at import the fan-out silently dropped to slab-only
    # and a genuinely-new variety was minted in one category only. curate.active_branches() already reads
    # the runtime registry; this makes the emit safety net agree with it.
    fanout = [c.name for c in active_categories() if c.fan_out]
    fanout_set = set(fanout)
    present: dict[str, set[tuple[str, str]]] = {b: set() for b in fanout}
    # canonical variety -> (type_slug, Name, Aliases). First-seen wins, but a row that carries aliases is
    # preferred so the union never loses aliases to an alias-less duplicate in another category.
    varieties: dict[tuple[str, str], tuple[str, str, str]] = {}
    for k, s in by_key.items():
        br = k.split("_", 1)[0]
        if br not in fanout_set:
            continue
        ts = type_slug_from_key(k)
        ident = (proj.norm(ts), proj.norm(s.get("Name", "")))
        present[br].add(ident)
        cand = (ts, s.get("Name", ""), s.get("Aliases", ""))
        cur = varieties.get(ident)
        if cur is None or (not cur[2] and cand[2]):
            varieties[ident] = cand
    out: dict[str, dict] = {}
    skipped: dict[tuple, list] = {}   # ident -> [(branch, reason)]: a category that could NOT be filled
    for br in fanout:
        for ident, (ts, disp, aliases) in varieties.items():
            if ident in present[br]:
                continue                          # category already carries this variety (any key form)
            mk = gen_key(br, ts, disp)
            if mk in by_key:                      # a row with this exact key already exists in the input
                skipped.setdefault(ident, []).append((br, "key_already_in_by_key"))
                continue
            if mk in out:                         # already queued this run (defensive dedup)
                skipped.setdefault(ident, []).append((br, "key_already_queued"))
                continue
            out[mk] = {"Key": mk, "Name": disp, "Image": "",
                       "Aliases": aliases, "Volume per kg (m³/kg)": ""}
    # DIAGNOSTIC (pure logging, no behaviour change): log the EXACT per-category fill decision for any
    # variety whose NAME is present in some-but-not-all fan-out categories (the real slab-only symptom).
    # Keyed on PRESENCE only (not on `out`), so a fill that lands in `out` cannot mask the case. For each
    # missing category: was it FILLED (queued in out), skipped because the deterministic key already exists
    # in by_key, or not reached at all -- plus, for a filled row, whether that key is ALSO in by_key (which
    # means build()'s `filled = [... if key not in by_key]` will DISCARD the fill). This is the line that
    # ends the guessing: it states, in the run's own log, why block/tile were not created.
    name_nonuniform: dict[str, dict] = {}
    for ident, (ts, disp, aliases) in varieties.items():
        present_in = [b for b in fanout if ident in present[b]]
        if not (0 < len(present_in) < len(fanout)):
            continue
        miss = {}
        for b in fanout:
            if b in present_in:
                continue
            mk = gen_key(b, ts, disp)
            if mk in out:
                miss[b] = "filled_AND_in_by_key(discarded)" if mk in by_key else "filled"
            elif mk in by_key:
                miss[b] = "skip:key_in_by_key"
            else:
                miss[b] = "not_reached"
        name_nonuniform[f"{disp}|{ident[0]}"] = {"present_in": sorted(present_in), "missing": miss}
    if name_nonuniform:
        log.warning("uniform-fill: name-non-uniform varieties + per-category decision",
                    extra={"extra_fields": {"fanout": fanout, "count": len(name_nonuniform),
                                            "detail": dict(list(name_nonuniform.items())[:25])}})
    return list(out.values())


def build(existing_path: Path | None = None, image_keys: set[str] | None = None) -> Path:
    """Assemble to_upload/1_variants_full.csv from the committed base (the complete
    source-of-truth variant set) + this run's delta (to_upload/1_variants_update.csv).

    image_keys: the set of variant Keys that actually have a {Key}.png on S3. When provided (or
    resolvable from S3), a variant advertises its image link IFF its image exists -- so the file
    can never point Medusa at a missing image. Pass an explicit set in tests; None resolves it from
    S3, and a None result there (S3 unreachable) falls back to the prior heuristic."""
    # Seed from the BASE (committed, complete, Id-free source of truth), never the live export: a cold
    # start has a thin/empty live export (Medusa emptied), but the catalog must still rebuild the whole
    # set and grow from there. The live export is unioned only as a safety net, so a variety Medusa minted
    # that is not yet in the base is never dropped (base should already contain the live set).
    existing_path = Path(existing_path or SETTINGS.paths.variants_export_base_csv)
    by_key = {r["Key"]: r for r in _rows(existing_path)}      # base = complete catalog (read-only)
    order = list(by_key)
    live = SETTINGS.paths.export_file
    if existing_path != live:
        for r in _rows(live):
            if r["Key"] not in by_key:
                by_key[r["Key"]] = r
                order.append(r["Key"])
    n_existing = len(order)
    for r in _rows(SETTINGS.paths.to_upload_dir / "1_variants_update.csv"):   # new + alias delta
        if r["Key"] in by_key:
            by_key[r["Key"]].update(r)                        # alias update onto existing row
        else:
            by_key[r["Key"]] = r
            order.append(r["Key"])                            # genuinely new variant
    filled = [m for m in _uniform_rows(by_key) if m["Key"] not in by_key]  # union-fill missing categories
    rows = [by_key[k] for k in order] + filled
    # DIAGNOSTIC (pure logging): snapshot each variety's per-category coverage after every filter, so a
    # slab-only outcome names the EXACT stage that removed its other categories (input drift vs a specific
    # filter) instead of us inferring it. Identity = the branch-independent Key core (type+name slug).
    _stages: list[tuple[str, set]] = []
    def _snap(label: str) -> None:
        s = set()
        for r in rows:
            head = r["Key"].rsplit("_", 1)[0]
            if "_" in head:
                b, core = head.split("_", 1)
                s.add((b, core))
        _stages.append((label, s))
    _snap("assembled")
    # E10: a retired variety (the operator's explicit removal / un-retire memory) never re-enters the
    # upload -- so a produce does not re-serve it, and the ledger row stays 'retiring' until Medusa acks.
    from stone_pipeline.stages import decisions
    retired = decisions.load_retired()
    if retired:
        rows = [r for r in rows if r["Key"] not in retired]
    _snap("retired")
    # Only clean, correctly-typed variety names reach Medusa. Drop the two junk classes the rest of the
    # pipeline already excludes from matching and lists for deletion (loaders/catalog): code-like names
    # (a stale 'Z Astoria') and MIS-TYPED variants whose name carries a stone-type word that disagrees
    # with the Key's type ('Agata White' -- an agate -- keyed as semi_precious_stone). The same rule
    # applies here so the union-fill can never carry a mistyped variety into another category: one notion
    # of "a real variety" everywhere, so a cold start stays uniform (the correctly-typed variety survives).
    from stone_pipeline.reference import loaders
    rows = [r for r in rows
            if not looks_like_artifact(r["Name"]) and not loaders.is_mistyped_variant(r["Key"], r["Name"])]
    _snap("artifact_mistyped")
    rows = _consolidate(rows)                                 # 'Rosal C/T/B' -> one 'Rosal' + aliases
    _snap("consolidate")

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

    # THE dedup gate: collapse every (branch,type,name) that resolves to more than one Key to a single
    # survivor before write. 1_variants_full feeds BOTH the committed base (base := full) AND the ledger,
    # so gating uniqueness here makes the whole catalog a fixed point: a duplicate variety cannot ship
    # no matter how it entered (dirty base, stale live-export union, a re-key old side still in Medusa).
    # Keyed on (branch, type, name) so genuine multi-type homonyms (Aqua Blue as gneiss/granite/...) are
    # preserved. This is the one place the pipeline enforces variety identity.
    rows = loaders.collapse_to_survivors(rows)
    _snap("collapse")
    # Report any variety left in some-but-not-all fan-out categories, naming the filter stage that dropped
    # each missing category (the first snapshot where it went absent after having been present).
    _fanout = {c.name for c in active_categories() if c.fan_out}
    _cov: dict = {}
    for _b, _core in _stages[-1][1]:
        _cov.setdefault(_core, set()).add(_b)
    _nonuniform = {c: brs for c, brs in _cov.items() if 0 < len(brs) < len(_fanout)}
    if _nonuniform:
        _detail = {}
        for _core, _brs in list(_nonuniform.items())[:20]:
            _miss = {}
            for _mb in sorted(_fanout - _brs):
                _dropped_by, _seen = "not_in_input", False
                for _lbl, _s in _stages:
                    if (_mb, _core) in _s:
                        _seen = True
                    elif _seen:
                        _dropped_by = _lbl        # was present, gone at this stage -> this filter dropped it
                        break
                _miss[_mb] = _dropped_by
            _detail[_core] = {"have": sorted(_brs), "dropped_by": _miss}
        log.warning("catalog left non-uniform varieties", extra={"extra_fields": {
            "fanout": sorted(_fanout), "non_uniform_count": len(_nonuniform), "detail": _detail}})

    path = SETTINGS.paths.to_upload_dir / "1_variants_full.csv"
    # atomic, NOT sanitized: 1_variants_full is the Medusa import; a leading "'" prepended to a
    # Name/Alias would corrupt the catalog data. (Operator review of scraped names is the sanitized
    # review files' job, not this machine-consumed deliverable.)
    csvio.write_dicts(path, _COLS, rows, sanitize=False)
    # count GENUINELY new rows from the FINAL set (after artifact-filter + consolidation), not from
    # the pre-filter `order` -- else dropped/folded rows inflate the 'new' metric.
    existing_keys = set(order[:n_existing])
    filled_keys = {m["Key"] for m in filled}
    new_count = sum(1 for r in rows if r["Key"] not in existing_keys and r["Key"] not in filled_keys)
    log.info("variants_full emitted", extra={"extra_fields": {
        "rows": len(rows), "existing": n_existing,
        "new": new_count, "union_filled": len(filled)}})
    return path
