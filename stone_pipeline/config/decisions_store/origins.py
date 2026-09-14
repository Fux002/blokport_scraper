"""Per-variety origin edits (the "edit origins" admin action; same channel as a mint's seed_country, but for
any variety and holding a LIST of countries). Keyed by (normalized variety, normalized type). Overlaid onto
the origin MAP at load, so the variety resolves against these countries and the per-vendor gate picks from
the list."""

from __future__ import annotations

from contextlib import closing

from stone_pipeline.config import store

from ._common import InvalidDecision, _norm, _now


def set_variety_origin(variety: str, stone_type: str, country_iso: str, city: str = "", county: str = "") -> None:
    """Upsert the operator-edited origin COUNTRIES for a (variety, type) -- a comma-list ("IN,IR"). Keyed by
    (normalized variety, normalized type). Overlaid onto the origin MAP at load, so the variety resolves
    against these countries (and the per-vendor gate picks from the list). The caller validates each ISO."""
    v_norm, t_norm = _norm(variety), _norm(stone_type)
    isos = [c.strip().upper() for c in (country_iso or "").split(",") if c.strip()]
    if not v_norm or not t_norm or not isos:
        raise InvalidDecision("variety origin edit requires variety, stone_type and at least one country_iso")
    with closing(store.open_store()) as conn:
        conn.execute(
            "INSERT INTO variety_origin (variant_norm, stone_type_norm, variant_display, country_iso, "
            "city, county, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(variant_norm, stone_type_norm) DO UPDATE SET "
            "variant_display = excluded.variant_display, country_iso = excluded.country_iso, "
            "city = excluded.city, county = excluded.county, decided_at = excluded.decided_at",
            (v_norm, t_norm, variety.strip(), ",".join(isos), city.strip(), county.strip(), _now()))
        conn.commit()


def delete_variety_origin(variety: str, stone_type: str) -> bool:
    """Remove one variety origin edit (revert that (variety, type) to the base map). Returns True if a row
    was removed."""
    with closing(store.open_store()) as conn:
        n = conn.execute("DELETE FROM variety_origin WHERE variant_norm = ? AND stone_type_norm = ?",
                         (_norm(variety), _norm(stone_type))).rowcount
        conn.commit()
        return n > 0


def variety_origins() -> dict[tuple[str, str], str]:
    """(normalized variety, normalized type) -> comma-list ISO. Overlaid onto the origin map at load
    (apply_origin_overlay), AFTER the mint seed_country overlay so an explicit edit wins. Empty for a fresh
    store."""
    with closing(store.read_store()) as conn:
        return {(r["variant_norm"], r["stone_type_norm"]): r["country_iso"]
                for r in conn.execute("SELECT variant_norm, stone_type_norm, country_iso FROM variety_origin")}


def list_variety_origins() -> list[dict]:
    """Every variety origin edit as a display row, for the admin list."""
    with closing(store.read_store()) as conn:
        return [{"ref": f"{r['variant_norm']}|{r['stone_type_norm']}", "variety": r["variant_display"],
                 "stone_type": r["stone_type_norm"], "countries": r["country_iso"].split(","),
                 "city": r["city"], "county": r["county"]}
                for r in conn.execute(
                    "SELECT variant_norm, stone_type_norm, variant_display, country_iso, city, county "
                    "FROM variety_origin ORDER BY variant_display")]


def get_variety_origin(variety: str, stone_type: str) -> dict | None:
    """The stored edit for one (variety, type), or None if unedited (the base map applies)."""
    with closing(store.read_store()) as conn:
        r = conn.execute("SELECT variant_display, country_iso, city, county FROM variety_origin "
                         "WHERE variant_norm = ? AND stone_type_norm = ?",
                         (_norm(variety), _norm(stone_type))).fetchone()
    if not r:
        return None
    return {"variety": r["variant_display"], "stone_type": _norm(stone_type),
            "countries": r["country_iso"].split(","), "city": r["city"], "county": r["county"]}


def clear_variety_origins() -> int:
    """Drop EVERY per-variety origin edit. PRISTINE reset ONLY (a factory reset returns to the pure base, so
    the operator overlay is wiped). Returns rows deleted."""
    with closing(store.open_store()) as conn:
        n = conn.execute("DELETE FROM variety_origin").rowcount
        conn.commit()
        return n
