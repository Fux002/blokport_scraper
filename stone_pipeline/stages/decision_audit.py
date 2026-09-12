"""Did the produce DO what the operator decided? The pipeline says so itself, every run.

A decision the produce silently ignores is the worst failure this review can have: the operator sees a card
disappear and assumes the catalog changed, while nothing did (the 2026-09-09 case: eleven mints keyed on a
spelling with a type word were never read). So after each produce every stored decision is checked against
the outcome:

  * a MINT is applied once its variety (the corrected name, else the spelling, under its type) exists;
  * a vendor ALIAS is applied once that vendor's listings of the spelling bind to the target;
  * a vendor ORIGIN is applied once that vendor's products bound to the variety carry it at the operator rung;
  * a REJECT is applied once no card carries the spelling.

Two-pass is not a gap: a target created THIS produce binds its products on the next one, so an alias or an
origin whose variety is new this run is "pending", not "not applied". A decision with no listing in this
produce (the vendor dropped it) is neither applied nor a gap, and is left alone.

Pure: takes the rows and the decision maps, returns the gaps. curate.run writes them as pending cards of kind
`decision_gap` so they surface in the one review list, and counts them in the run's summary.
"""

from __future__ import annotations

from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.matching import projections as proj

OPERATOR_ORIGIN_RUNG = "supplier_override"


def _norm(s: str | None) -> str:
    return proj.norm(s or "")


def _gap(kind: str, source: str, scraped: str, name: str, stone_type: str, origin: str, reason: str) -> dict:
    return {"kind": "decision_gap", "decision": kind, "source": source, "scraped": scraped, "name": name,
            "stone_type": stone_type, "origin": origin, "reason": reason}


def audit(rows: list[CanonicalRow], existing: set[tuple[str, str]], created: set[tuple[str, str]],
          mints: dict[str, dict], scoped: dict[tuple[str, str], tuple[str, str]],
          origins: dict[tuple[str, str, str], str], pending_spellings: set[str]) -> list[dict]:
    """The decisions this produce did NOT honour. `existing` and `created` are (norm name, norm type) sets of
    the varieties in the reference and the ones minted this run; `mints` is decisions_store.variety_actions();
    `scoped` / `origins` are the vendor alias and origin maps; `pending_spellings` are the norm scraped spellings
    still carried by a pending variety card."""
    gaps: list[dict] = []
    by_listing: dict[tuple[str, str], list[CanonicalRow]] = {}
    for r in rows:
        by_listing.setdefault((_norm(r.src_site), _norm(r.variety_match_key or r.raw_name)), []).append(r)

    for (_, spelling), dec in mints.items():
        if dec["action"] == "mint":
            name = dec.get("seed_name") or dec.get("spelling") or spelling
            target = (_norm(name), _norm(dec.get("seed_type")))
            if target not in existing and target not in created:
                gaps.append(_gap("mint", dec.get("source") or "", spelling, name, dec.get("seed_type") or "",
                                 dec.get("seed_country") or "",
                                 f"Mint not applied: no variety '{name}' ({dec.get('seed_type')}) exists after this produce."))
        elif dec["action"] == "reject" and spelling in pending_spellings:
            gaps.append(_gap("reject", "", spelling, spelling, "", "",
                             "Reject not applied: the spelling still has a pending card."))

    for (source, spelling), (target, target_type) in scoped.items():
        listings = by_listing.get((source, spelling), [])
        if not listings:
            continue                                                  # no listing this produce: nothing to judge
        ttype = _norm(target_type) or _norm(listings[0].type_name)
        if (_norm(target), ttype) in created:
            continue                                                  # minted this run; binds on the next produce
        if any(_norm(r.variation_name) != _norm(target) for r in listings):
            observed = ", ".join(sorted({r.variation_name or r.variation_method or "unbound" for r in listings}))
            gaps.append(_gap("alias", source, spelling, target, target_type or "", "",
                             f"Alias not applied: {len(listings)} listing(s) resolved to {observed}, not '{target}'."))

    by_product: dict[tuple[str, str, str], list[CanonicalRow]] = {}
    for r in rows:
        by_product.setdefault((_norm(r.src_site), _norm(r.variation_name), _norm(r.type_name)), []).append(r)
    for (source, variety, stone_type), iso in origins.items():
        products = by_product.get((source, variety, stone_type), [])
        if not products or (variety, stone_type) in created:
            continue
        off = [r for r in products if (r.origin_country_code or "") != iso or r.origin_source != OPERATOR_ORIGIN_RUNG]
        if off:
            observed = ", ".join(sorted({f"{r.origin_country_code or '-'}/{r.origin_source or '-'}" for r in off}))
            gaps.append(_gap("origin", source, "", variety, stone_type, iso,
                             f"Origin not applied: {len(off)} product(s) carry {observed}, not {iso} at the operator rung."))
    return gaps
