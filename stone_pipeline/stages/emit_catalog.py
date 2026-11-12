"""Emit the complete variant upload file -- one deterministic, isolated step.

The catalog is a pure function of two inputs that NEVER alias:
  - the immutable Medusa export (existing variants, download-only), and
  - this run's scraped additions (new variants + confirmed alias updates).

This stage assembles the full file the user uploads, so nothing is hand-merged or
stamped in place and re-running yields an identical file:

  committed base + export    ─┐
  curate's delta             ─┴─►  1_variants_full.csv    (one row per variety, names cleaned)
                                    └─►  1_variants_update.csv  (the full rows the delta names)

Beside them it writes the icon side file (variant_image_shas.json): each produced variety's icon pairs listed on S3,
which the ledger serves as the variety's category_images (PHASE-1-SPEC section 4).
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from stone_pipeline.config.settings import IS_PRODUCTION, SETTINGS
from stone_pipeline.core import csvio, logfmt
from stone_pipeline.core.text import (
    UnfoldableName,
    clean_alias_list,
    clean_variety_name,
    looks_like_artifact,
    split_bracket_alias,
)
from stone_pipeline.io import storage
from stone_pipeline.io.variety_icons import ICON_SIDECAR, VARIATIONS_PREFIX, icon_pair
from stone_pipeline.phase1.variety_key import variety_key_type
from stone_pipeline.reference import alias_collisions
from stone_pipeline.reference.loaders import field_kinds, variants_status_required
from stone_pipeline.reference.medusa_files import VARIETY_FILE_COLUMNS, read_variants_export

log = logfmt.get_logger("emit_catalog")


def _alias_display(text: str) -> str:
    return field_kinds.display(field_kinds.VARIATION_ALIAS_VALUE, text)


def _write_names(row: dict) -> list[str]:
    """Write a row's Name and Aliases by their fields' rule (field-kinds.json); returns the aliases dropped for
    having no ASCII form. A '(alternative)' moves out of the Name into the alias list ('Black Galaxy (Star
    Galaxy)' -> Name 'Black Galaxy' + alias 'Star Galaxy'). The Key is unchanged (a Name/alias correction, not a
    re-key), and matching improves because both the clean name and each alternative spelling resolve."""
    bare_name, name_bracket_aliases = split_bracket_alias(row["Name"])
    raw_aliases = [a for a in row["Aliases"].split("|") if a.strip()] + name_bracket_aliases
    try:
        row["Name"] = field_kinds.display(field_kinds.VARIATION_NAME, bare_name)
    except UnfoldableName as exc:
        # dropping the row would delete the variety (base := full), so a name with no ASCII form stops emit
        raise UnfoldableName(f"{row['Key']}: variety name {bare_name!r} has no ASCII form") from exc
    kept, dropped = clean_alias_list(bare_name, raw_aliases, _alias_display)
    row["Aliases"] = "|".join(kept)
    return dropped


def update_rows(full_rows: list[dict], delta: list[dict]) -> list[dict]:
    """The rows of the full file whose Key the delta names, in full-file order; a delta row the full gates dropped
    (retired, artifact, consolidated, collapsed) is not published."""
    wanted = {r["Key"] for r in delta}
    return [r for r in full_rows if r["Key"] in wanted]


def _report_alias_drops(drops: list[tuple[str, str]]) -> None:
    """Every alias dropped for having no ASCII form (owner decision O2), listed for the operator, sorted."""
    path = SETTINGS.paths.review_dir / "aliases_dropped.csv"
    csvio.write_dicts(path, ["Key", "alias"], [{"Key": k, "alias": a} for k, a in sorted(drops)], sanitize=True)
    if drops:
        log.info("aliases dropped for having no ASCII form", extra={"extra_fields": {
            "count": len(drops), "report": str(path)}})


_COLS = VARIETY_FILE_COLUMNS
ALIAS_COLLISIONS_REVIEW = "alias_collisions_dropped.csv"     # review list of aliases dropped at emit (Mas, 11-12)
# Each run's list under its own UTC stamp: the base keeps only the emitted aliases (base := 1_variants_full), so a
# dropped alias is gone for good and the current list is overwritten by the next run (Scraper L2). The review upload
# mirrors this folder to S3 and never deletes, so every run's list stays there.
ALIAS_COLLISIONS_RUNS = "alias_collisions_dropped_runs"
_ALIAS_COLLISION_FIELDS = ["Key", "variety", "alias", "collides_with", "collides_with_key"]


def _run_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _drop_alias_collisions(rows: list[dict]) -> list[dict]:
    """An alias equal to another live variety's name of the same type (an operator mint equal to a seed alias) would
    bind one stone's listing to another (Mas, 11-11): it is DROPPED from its row, logged, and returned for the review
    list. The produce never stops on it; CI holds the committed vocabulary at 0."""
    collisions = alias_collisions.find(
        alias_collisions.Variety(r["Key"], r["Type"], r["Name"], tuple(a for a in r["Aliases"].split("|") if a))
        for r in rows)
    gone: dict[str, set[str]] = {}
    for c in collisions:
        gone.setdefault(c.key, set()).add(c.alias)
    for r in rows:
        if r["Key"] in gone:
            r["Aliases"] = "|".join(a for a in r["Aliases"].split("|") if a and a not in gone[r["Key"]])
    listed = [{"Key": c.key, "variety": f"{c.variety[0]} {c.variety[1]}", "alias": c.alias,
               "collides_with": f"{c.collides_with[0]} {c.collides_with[1]}", "collides_with_key": c.other_key}
              for c in collisions]
    if listed:
        log.warning("aliases dropped: each names another variety of the same type", extra={"extra_fields": {
            "count": len(listed), "report": ALIAS_COLLISIONS_REVIEW, "aliases": listed[:20]}})
    return listed


def _list_alias_collisions(listed: list[dict]) -> None:
    """The review list of this run's dropped aliases (the run summary counts it), and, when any dropped, its
    per-run copy."""
    review = SETTINGS.paths.review_dir
    csvio.write_dicts(review / ALIAS_COLLISIONS_REVIEW, _ALIAS_COLLISION_FIELDS, listed, sanitize=True)
    if listed:
        csvio.write_dicts(review / ALIAS_COLLISIONS_RUNS / f"{_run_stamp()}.csv", _ALIAS_COLLISION_FIELDS, listed,
                          sanitize=True)


def alias_collisions_dropped() -> int:
    """How many aliases the last produce build dropped as collisions, read from the review list every such build
    writes, so the run summary states the count of the very list the operator opens."""
    with (SETTINGS.paths.review_dir / ALIAS_COLLISIONS_REVIEW).open(encoding="utf-8-sig", newline="") as h:
        return sum(1 for _ in csv.DictReader(h))


def _s3_access_denied(exc) -> bool:
    """True for an S3 PERMISSION error (an IAM misconfig to fail loud on), vs a transient/network error or
    a missing client (degrade). Read structurally off the botocore ClientError response, so no hard botocore
    import is needed and a non-ClientError (e.g. ImportError) is simply not a permission error."""
    resp = getattr(exc, "response", None)
    code = resp.get("Error", {}).get("Code", "") if isinstance(resp, dict) else ""
    return code in ("AccessDenied", "AllAccessDisabled", "InvalidAccessKeyId",
                    "SignatureDoesNotMatch", "AuthorizationHeaderMalformed", "403")


def _s3_icon_pairs() -> dict[str, dict[str, str]] | None:
    """{Key: {category name: ETag}} for every variety icon in <env>/variations/ (PHASE-1-SPEC section 4), via ONE
    paginated listing. Any other object under the prefix (an icon of the old flat layout, a marker file) is counted
    and logged, never read as an icon. READ-ONLY, so it runs even under s3.dry_run.

    None only OUTSIDE production when S3 cannot be reached (no boto3 or credentials: CI, local), and the ledger then
    keeps the icons it holds. In production a failed listing raises, because category_images are never sent from a
    fallback (spec section 4). A permission error is an IAM misconfig and raises everywhere."""
    s3 = SETTINGS.s3
    pairs: dict[str, dict[str, str]] = {}
    other = 0
    try:
        # bounded timeouts + capped retries so a network stall can't hang the build
        client = storage.aws_client("s3", config=storage.BOUNDED, profile=s3.credentials_profile)
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=s3.bucket, Prefix=VARIATIONS_PREFIX):
            for obj in page.get("Contents", []):
                pair = icon_pair(obj["Key"])
                if pair is None:
                    other += 1
                    continue
                key, cat = pair
                pairs.setdefault(key, {})[cat.name] = (obj.get("ETag") or "").strip('"')
    except ImportError as exc:                    # boto3 is not installed here (a lean local environment)
        if IS_PRODUCTION:
            raise
        log.warning("boto3 is not installed, so variety icons are not listed; the ledger keeps its icons",
                    extra={"extra_fields": {"error": str(exc)}})
        return None
    except Exception as exc:
        if IS_PRODUCTION or _s3_access_denied(exc):
            log.error("listing the variety icons on S3 failed; refusing to serve icons from a guess",
                      extra={"extra_fields": {"error": str(exc)}})
            raise
        log.warning("S3 unreachable listing the variety icons; the ledger keeps its icons",
                    extra={"extra_fields": {"error": str(exc)}})
        return None
    if other:
        log.info("objects under the variations prefix that are not icon pairs",
                 extra={"extra_fields": {"count": other}})
    return pairs


def _consolidate(rows: list[dict]) -> list[dict]:
    """Fold ONLY variants whose name carries a trailing grade code into their base variety, so a
    graded family becomes ONE searchable variety with the originals as aliases ('Rosal C'/'Rosal
    T'/'Rosal B' -> 'Rosal'). Every other variant passes through UNTOUCHED -- distinct same-name
    variants are NOT deduped and real names ('Rosal Codacal Favor', 'Agata Black') never merge,
    because only a row whose clean_variety_name DIFFERS from its name (a code was stripped) is
    folded. If a base of the same type with the clean name already exists it absorbs the code as an
    alias; otherwise the first coded row is renamed to the base (its Key survives the in-place Medusa
    rename). A fold never crosses types: a variety's identity is (type, name)."""
    coded, passthrough = [], []
    for r in rows:
        clean = clean_variety_name(r["Name"]) or r["Name"]
        (coded if clean.casefold() != r["Name"].casefold() else passthrough).append((clean, r))
    out = [r for _, r in passthrough]
    base_by: dict[tuple, dict] = {}
    for r in out:                                                      # existing real bases to fold into
        base_by.setdefault((variety_key_type(r["Key"]), r["Name"].casefold()), r)
    for clean, r in coded:
        gkey = (variety_key_type(r["Key"]), clean.casefold())
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
    return out


def _rows(path: Path) -> list[dict]:
    """The _COLS rows with a Key of a variants file: the base, Medusa's live export or this produce's update
    delta, which share the shape."""
    path = Path(path)
    if not path.exists():
        return []
    return [dict(zip(_COLS, (v.key, v.name, v.type, "|".join(v.aliases))))
            for v in read_variants_export(path, require_status=variants_status_required(path)) if v.key]


def build(existing_path: Path | None = None, icons: dict[str, dict[str, str]] | None = None,
          *, seed_only: bool = False, out_path: Path | None = None) -> Path:
    """Assemble to_upload/1_variants_full.csv from the committed base (the complete source-of-truth variety set) +
    this run's delta (curate's outputs/<env>/curate_delta.csv), and to_upload/1_variants_update.csv as the subset of
    the full rows the delta names (one cleaning point). One row per variety (type, name): a variety has no category.

    icons: {Key: {category name: ETag}}, the icon pairs on S3. Pass it in tests; None lists S3 (_s3_icon_pairs).
    The produced varieties' pairs go to the side file the ledger serves from; when S3 is not listed (outside
    production only) the side file is left as it is.

    seed_only: rebuild HERMETICALLY from the committed base alone -- no live Medusa export, no update delta, no
    retired-decision filter, no S3. This is the cold-start seed projection (the fixed point `seed.verify` checks):
    the deterministic transforms only, so the result depends solely on the committed seed. out_path: write here
    instead of the standard artifact (so a seed_only verify never clobbers a real 1_variants_full.csv)."""
    # Seed from the BASE (committed, complete, Id-free source of truth), never the live export: a cold
    # start has a thin/empty live export (Medusa emptied), but the catalog must still rebuild the whole
    # set and grow from there. The live export is unioned only as a safety net, so a variety Medusa minted
    # that is not yet in the base is never dropped (base should already contain the live set).
    existing_path = Path(existing_path or SETTINGS.paths.variants_export_base_csv)
    by_key = {r["Key"]: r for r in _rows(existing_path)}      # base = complete catalog (read-only)
    order = list(by_key)
    live = SETTINGS.paths.export_file
    if not seed_only and existing_path != live:              # seed_only: no live-export union (hermetic)
        for r in _rows(live):
            if r["Key"] not in by_key:
                by_key[r["Key"]] = r
                order.append(r["Key"])
    n_existing = len(order)
    delta = [] if seed_only else _rows(SETTINGS.paths.curate_delta_csv)              # curate's new + alias delta
    for r in delta:
        if r["Key"] in by_key:
            by_key[r["Key"]].update(r)                        # alias update onto existing row
        else:
            by_key[r["Key"]] = r
            order.append(r["Key"])                            # genuinely new variant
    rows = [by_key[k] for k in order]
    # E10: a retired variety (the operator's explicit removal / un-retire memory) never re-enters the
    # upload -- so a produce does not re-serve it, and the ledger row stays 'retiring' until Medusa acks.
    from stone_pipeline.stages import decisions
    retired = set() if seed_only else decisions.load_retired()   # seed_only: no config.db retired filter
    # Only clean, correctly-typed variety names reach Medusa. Drop the two junk classes the rest of the
    # pipeline already excludes from matching and lists for deletion (loaders/catalog): code-like names
    # (a stale 'Z Astoria'). NOTE: a variety's stone TYPE is authoritative -- it is assigned from the Key
    # at review (mint/alias), never re-judged against the name -- so emit NEVER drops a variety on a
    # name-vs-type disagreement. That silent, catalog-wide, permanent delete (base := 1_variants_full +
    # in_full=0) could remove a valid combination on type-vocabulary drift. A genuinely wrong-typed base
    # entry is surfaced for review by seed.verify / the consistency report instead, never deleted here.
    from stone_pipeline.reference import loaders
    rows = [r for r in rows if not looks_like_artifact(r["Name"])]
    rows = _consolidate(rows)                                 # 'Rosal C/T/B' -> one 'Rosal' + aliases
    alias_drops: list[tuple[str, str]] = []
    for r in rows:
        alias_drops.extend((r["Key"], alias) for alias in _write_names(r))
    _report_alias_drops(alias_drops)

    # THE dedup gate: collapse every (type, name) that resolves to more than one Key to a single survivor
    # before write. 1_variants_full feeds BOTH the committed base (base := full) AND the ledger, so gating
    # uniqueness here makes the whole catalog a fixed point: a duplicate variety cannot ship no matter how it
    # entered (dirty base, stale live-export union). This is the one place the pipeline enforces variety identity.
    rows = loaders.collapse_to_survivors(rows)
    dropped_aliases = _drop_alias_collisions(rows)
    if not seed_only:                     # seed.verify's projection never rewrites the produce's list (Scraper L1)
        _list_alias_collisions(dropped_aliases)

    path = Path(out_path) if out_path else (SETTINGS.paths.to_upload_dir / "1_variants_full.csv")
    # atomic, NOT sanitized: 1_variants_full is the Medusa import; a leading "'" prepended to a
    # Name/Alias would corrupt the catalog data. (Operator review of scraped names is the sanitized
    # review files' job, not this machine-consumed deliverable.)
    csvio.write_dicts(path, _COLS, rows, sanitize=False)
    if not seed_only:
        # the incremental upload: the full file's rows this run's delta names, the SAME cleaned row objects in
        # full-file order, so the two upload files can never disagree on a Name or alias
        csvio.write_dicts(path.parent / "1_variants_update.csv", _COLS, update_rows(rows, delta), sanitize=False)
        listed = icons if icons is not None else _s3_icon_pairs()
        if listed is not None:
            # every produced variety's pairs, sorted, so the same listing writes the same bytes; a variety with no
            # pair is absent, which the ledger reads as "no icon" (the listing is the icons' authority)
            produced = {r["Key"] for r in rows}
            pairs = {k: dict(sorted(v.items())) for k, v in sorted(listed.items()) if k in produced}
            (path.parent / ICON_SIDECAR).write_text(json.dumps(pairs, ensure_ascii=False), encoding="utf-8")
    existing_keys = set(order[:n_existing])
    log.info("variants_full emitted", extra={"extra_fields": {
        "rows": len(rows), "existing": n_existing,
        "new": sum(1 for r in rows if r["Key"] not in existing_keys)}})
    return path
