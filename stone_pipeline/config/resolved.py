"""The RESOLVED list: what the last produce decided for every product it BOUND, per vendor -- the variety,
the type, the colour, the origin, and HOW each was decided -- with the operator's standing decisions
overlaid. No approval is needed: the next produce reproduces exactly this. A row can be adjusted through
the same statement the pending list uses (decisions_store.decide); until the next produce the row shows
that decision as pending. A product the produce could NOT bind is not here: it is a pending card, and the
two lists never show the same product.

Source: the newest run's canonical staging per source (outputs/<env>/<source>/<run>/diagnostics/
canonical.parquet), which the container restores on boot with the rest of the artifact tree.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from stone_pipeline.catalog import find_canonical
from stone_pipeline.config import decisions_store
import json

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.matching import projections as proj

_COLUMNS = ["src_site", "surrogate_key", "raw_name", "variety_match_key", "variation_key", "variation_name",
            "variation_method", "type_name", "type_method", "color_name", "origin_country_code",
            "origin_source", "src_url", "raw_image_urls", "image_keys"]


def _norm(s: str | None) -> str:
    return proj.norm(s or "")


def _first_image(rec: dict) -> str:
    """First usable image URL for a resolved row, from the SAME fields the pending card uses. The parquet
    stores these list fields as JSON strings (io.staging.JSON_FIELDS), so parse them; prefer the supplier
    photo (raw_image_urls, survives Medusa churn), fall back to the processed image_keys. Tolerant of a bad
    value -> no image, never a crash."""
    for col in ("raw_image_urls", "image_keys"):
        raw = rec.get(col)
        if not raw:
            continue
        try:
            urls = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            urls = [raw] if isinstance(raw, str) else []
        if isinstance(urls, list):
            for u in urls:
                if u:
                    return u
        elif isinstance(urls, str) and urls:
            return urls
    return ""


def _row(rec: dict, scoped: dict, origins: dict, actions: dict, owiden: dict | None = None) -> dict:
    source = rec["src_site"] or ""
    scraped = rec["variety_match_key"] or rec["raw_name"] or ""
    name = rec["variation_name"] or ""
    stone_type = rec["type_name"] or ""
    alias = scoped.get((_norm(source), _norm(scraped)))
    mint = actions.get(_norm(scraped))
    decision = None
    if alias:
        decision = {"kind": "bound", "name": alias[0], "stone_type": alias[1] or None}
    elif mint and mint["action"] == "mint":
        decision = {"kind": "minted", "name": mint.get("seed_name") or scraped,
                    "stone_type": mint.get("seed_type"), "color": mint.get("seed_color"),
                    "origin": mint.get("seed_country")}
    elif mint and mint["action"] == "reject":
        decision = {"kind": "rejected"}
    # the vendor's origin is keyed by the variety the product WILL bind to: the decided target when there is
    # one, else the one the last produce bound
    target = (decision or {}).get("name") or name, (decision or {}).get("stone_type") or stone_type
    origin = origins.get((_norm(source), _norm(target[0]), _norm(target[1])))
    if origin:
        decision = {**(decision or {"kind": "origin"}), "origin": origin}
    # WIDEN ("added to the stone's documented origins"): the OPERATOR's checkbox on THIS decision, per-record
    # from origin_widen, keyed on (source, decided target, type). NOT variety-level membership (which would
    # show widen on every sibling decision that binds the same variety).
    doc_iso = (owiden or {}).get((_norm(source), _norm(target[0]), _norm(target[1])))
    return {
        "ref": f"{_norm(source)}|{_norm(scraped)}",
        "source": source, "scraped": scraped, "surrogate_key": rec["surrogate_key"] or "",
        "name": name, "variation_key": rec["variation_key"] or "",
        "stone_type": stone_type, "color": rec["color_name"] or "",
        "origin": rec["origin_country_code"] or "",
        "resolved_by": {"variety": rec["variation_method"] or "", "type": rec["type_method"] or "",
                        "origin": rec["origin_source"] or ""},
        "decision": decision,
        "src_url": rec["src_url"] or "",
        # Resolved rows never carried a scraper image, so they depended entirely on a Medusa SKU fallback;
        # when that stops matching, the images vanish. Supply the image from the SAME field the pending
        # card uses (the supplier photo, which survives Medusa product churn), image_keys as a fallback.
        "image": _first_image(rec),
        "widen": doc_iso is not None,
        "documented_origin": doc_iso,
    }


def _read_canonical(f) -> "pl.DataFrame":
    """Read the resolved columns from one canonical parquet, tolerant of a file that predates the image
    columns (raw_image_urls/image_keys were added later): request only the columns the file has, then
    backfill any missing one as empty so every frame shares _COLUMNS' schema for the concat."""
    present = set(pl.scan_parquet(f).collect_schema().names())
    df = pl.read_parquet(f, columns=[c for c in _COLUMNS if c in present])
    missing = [c for c in _COLUMNS if c not in present]
    if missing:
        df = df.with_columns([pl.lit("").alias(c) for c in missing])
    return df.select(_COLUMNS)


def list_resolved(source: str | None = None, decided: bool | None = None,
                  outputs_dir: Path | None = None) -> list[dict]:
    """Every product of the last produce (optionally one vendor), newest run per source, with its
    resolution and the operator's standing decision. `decided` filters to rows with (True) or without
    (False) an operator decision. Deterministic order: source, scraped spelling, surrogate key."""
    files = [f for f in find_canonical(outputs_dir or SETTINGS.paths.outputs_dir) if f.exists()]
    if not files:
        return []
    frame = pl.concat([_read_canonical(f) for f in files], how="vertical_relaxed")
    # RESOLVED means bound: a product the last produce could not bind belongs to the pending list (its card,
    # with any decision made on it as the card's current action), never to both lists at once
    frame = frame.filter(pl.col("variation_key").is_not_null() & (pl.col("variation_key") != ""))
    if source:
        frame = frame.filter(pl.col("src_site") == source)
    scoped = decisions_store.scoped_aliases()
    origins = decisions_store.origin_decisions()
    actions = decisions_store.variety_actions()
    owiden = decisions_store.origin_widen()   # PER-DECISION widen: {(nsrc, vnorm, tnorm): iso}
    rows = [_row(rec, scoped, origins, actions, owiden) for rec in frame.to_dicts()]
    if decided is not None:
        rows = [r for r in rows if (r["decision"] is not None) == decided]
    rows.sort(key=lambda r: (r["source"], _norm(r["scraped"]), r["surrogate_key"]))
    return rows
