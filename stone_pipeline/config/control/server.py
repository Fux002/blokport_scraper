"""HTTP surface of the scraper control plane under /config/v1, for the admin UI (Blokport's :4200 admin).

A thin, dependency-free server over the config store, the run trigger and the lifecycle verbs: sources and their
settings, runs, diagnostics, the source and variety lifecycle, the review queues, origins and the vocabularies for
the admin pickers. `_ROUTES` below is the one list of what it serves (an unknown path answers with that list).

Auth is a bearer token (SCRAPER_CONFIG_TOKEN); the server refuses to start without it. Routing is split from
transport (`dispatch` is pure) so it is testable without sockets. Every body is type-checked at this boundary: a
wrongly typed field is a 400 naming it, never a coercion or a 500. SQLite WAL handles the UI (single writer) plus
the pipeline (readers). No em dashes (design principle 2).
"""

from __future__ import annotations

import functools
import hmac
import json
import re
from stone_pipeline.core import env
import signal
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from stone_pipeline.config import store
from stone_pipeline.core import logfmt

log = logfmt.get_logger("config.server")

# Cap an authed request body so a bad/hostile Content-Length can't force an unbounded read into memory.
# Config payloads are small JSON (a source config, a decision); 1 MiB is far above any real one.
_MAX_BODY_BYTES = 1 << 20


def _color_vocab() -> dict[str, str]:
    """norm(colour) -> canonical colour name, from the Medusa attribute vocab. Backs the mint colour
    picker (GET /config/v1/colors) and validates an operator's seed colour (never seed one Medusa lacks).
    Loaded per call (admin low-QPS), mirroring config/varieties.py; empty if attributes.csv is absent."""
    from stone_pipeline.matching import projections as proj
    from stone_pipeline.reference.loaders import load_attributes
    return {proj.norm(c): c for c in load_attributes().canonical_names("color")}


def _type_vocab() -> dict[str, str]:
    """norm(type) -> canonical stone-type name, from the Medusa attribute vocab. Backs the mint type
    picker (GET /config/v1/types) and validates an operator's seed type (never seed one Medusa lacks)."""
    from stone_pipeline.matching import projections as proj
    from stone_pipeline.reference.loaders import load_attributes
    return {proj.norm(t): t for t in load_attributes().canonical_names("type")}


def _assignable_values(category: str, vocab: dict[str, str]) -> list[str]:
    """The sorted canonical values of `vocab` (norm -> canonical) a review PICKER may offer: the full
    attribute vocab minus the RESTRICTED (archived or internal) values. VE2 keeps a restricted value in the
    vocab the decide validator checks against (it is still a known attribute), but never offers it as a
    choice. Dormant until Blokport publishes the status columns: restricted_values is then empty and the
    picker lists the whole vocab, byte-identical to before."""
    from stone_pipeline.reference.loaders import restricted_values
    restricted = restricted_values().get(category, set())
    return sorted(canon for nv, canon in vocab.items() if nv not in restricted)


def _origin_as_card(item: dict) -> dict:
    """An origin confirmation in the variety-card shape: the product bound fine, only its origin could not be
    decided. A vendor-map mismatch says so and prefills the vendor's declared country. A multi-country stone
    without the supplier's home names where the supplier is based (NOT a declared own country, and by
    definition not a documented origin) and prefills nothing: the operator chooses. Every field stays open
    to the operator's statement, like any other card."""
    documented = ", ".join(c.strip() for c in (item.get("map_country") or "").split(",") if c.strip())
    from stone_pipeline.core.schema import ORIGIN_HOLD_MULTI_HOME
    if item.get("hold") == ORIGIN_HOLD_MULTI_HOME:
        reason = (f"Origin not known: supplier based in {item.get('vendor_origin') or '?'}; stone documented in "
                  f"{documented}. Choose the origin, or state what this product is.")
        prefill = ""
    else:
        reason = (f"Origin not corroborated: the vendor declares {item.get('vendor_origin') or '?'}, the "
                  f"stone documents {item.get('map_country') or 'no country'}. Confirm the origin, or "
                  f"state what this product is.")
        prefill = item.get("vendor_origin") or ""
    return {"kind": "origin", "ref": item.get("ref", ""), "variant": item.get("variety", ""),
            "stone_type": item.get("stone_type", ""), "color": "",
            "reason": reason,
            "origin": item.get("current_country") or prefill,
            "nearest_existing": "", "score": "", "model_prob": "",
            "src": item.get("source", ""), "scraped": item.get("scraped", ""),
            "spellings": [item["scraped"]] if item.get("scraped") else [],
            "listings": ([{"source": item.get("source", ""), "scraped": item["scraped"]}]
                         if item.get("scraped") else []),
            "src_url": item.get("src_url", ""), "image": item.get("image", ""), "description": "",
            "sources": item.get("sources"),
            # carry the outcome list_pending computed, so a decided origin card names what it decided
            # (origin | alias) instead of a bare "Decided"; None only when genuinely undecided.
            "current_action": item.get("current_action"),
            "current_alias_of": item.get("current_alias_of"),
            # field parity with a variety card so the SAME editable-name editor renders both: the current
            # decision's name/type/colour/country, so the UI prefills them and can send a restated name
            "current_seed_name": item.get("current_seed_name"),
            "current_seed_type": item.get("current_seed_type"),
            "current_seed_color": item.get("current_seed_color"),
            "current_seed_country": item.get("current_seed_country"),
            "current_country": item.get("current_country"),
            # whether the operator ticked "add to documented origins" (widen) + the country it documents
            "widen": item.get("widen", False),
            "documented_origin": item.get("documented_origin"),
            "decided": item.get("decided", False)}


def _gap_as_card(item: dict) -> dict:
    """A decision the last produce did NOT honour, in the variety-card shape (kind 'decision_gap'): the
    operator restates it (or clears it) from the same card as everything else."""
    listings = [{"source": item["source"], "scraped": item["scraped"]}] if item.get("source") and item.get("scraped") else []
    return {"kind": "decision_gap", "ref": item.get("ref", ""), "variant": item.get("name", ""),
            "stone_type": item.get("stone_type", ""), "color": "", "origin": item.get("origin", ""),
            "reason": item.get("reason", ""), "nearest_existing": "", "score": "", "model_prob": "",
            "src": item.get("source", ""), "scraped": item.get("scraped", ""),
            "spellings": [item["scraped"]] if item.get("scraped") else [], "listings": listings,
            "src_url": "", "image": "", "description": "", "sources": item.get("sources"),
            "current_action": item.get("decision"), "decided": False}


@functools.lru_cache(maxsize=1)
def _country_tables():
    """The country name->ISO table and its valid ISO-2 set, loaded ONCE: the file does not change under a
    running admin server, so origin validation reuses the shared resolver with no per-call CSV read."""
    from stone_pipeline.reference.loaders import load_country_codes
    codes = load_country_codes()
    return codes, frozenset(codes.values())


def _resolve_origin_iso(raw: str) -> str | None:
    """Validate an operator-supplied country to a canonical ISO-3166 alpha-2, or None if it is not a real
    country, through the ONE shared resolver (loaders.resolve_iso: name-first so 'UK' -> GB, then a bare
    valid ISO2) over the cached country table -- the same resolution derive and the matcher use."""
    from stone_pipeline.reference.loaders import resolve_iso
    return resolve_iso(raw, *_country_tables())


def _attach_image_progress(rows: list[dict]) -> None:
    """Enrich each source diagnostic with its `images` block (diagnostics.images_block), counted from the sync ledger
    at serve time; the photo process relinks the products itself when it publishes, so the ledger is current. No block
    when the ledger cannot be read or the source has no product in it (the UI renders "-")."""
    from stone_pipeline.config.control import diagnostics, runner
    from stone_pipeline.config.sources import SourceConfig, load_sources
    counts = diagnostics.ledger_image_counts()
    if counts is None:
        return
    sources = load_sources()
    generating = runner.photos_in_progress()
    for row in rows:
        name = row.get("source")
        if not name:
            continue
        cfg = sources.get(name) or SourceConfig(source=name)
        if (found := counts.get(cfg.source_code)) is not None:
            row["images"] = diagnostics.images_block(found, cfg.image_pipeline, generating, diagnostics.held_for_image(row))


# --- the route table -----------------------------------------------------------------------------------
# One handler per (method, path pattern). A pattern is a tuple of path segments under /config/v1; a segment
# written "{name}" captures that position into the handler's `params`. Matching is first hit per method, so
# a literal pattern must precede a capturing one of the same length. A known path with another method is
# 405 (naming the allowed methods); an unknown path is 404. Every handler is pure: (params, body, query) ->
# (status, json body). Adding a route = one handler + one table row, never a new branch in a dispatcher.


class _BadRequest(ValueError):
    """A body field of the wrong type or shape: dispatch answers 400 with the message, which names the field."""


def _dict(body) -> dict:
    """The JSON object body of a route whose body is optional (absent = {}). Any other JSON value is refused: a list
    or a string read as {} would run the route's widest default (every source) on a malformed request."""
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise _BadRequest("body must be a JSON object")
    return body


def _str(body: dict, field: str) -> str:
    """An optional string field, stripped ('' when absent or null)."""
    value = body.get(field)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _BadRequest(f"{field} must be a string, got {value!r}")
    return value.strip()


def _flag(body: dict, field: str) -> bool:
    """An optional true/false field (false when absent or null). Never bool(): "false" would read as true."""
    value = body.get(field)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise _BadRequest(f"{field} must be true or false, got {value!r}")
    return value


def _str_list(value, field: str) -> list[str]:
    """A non-empty list of non-empty strings, stripped."""
    if not (isinstance(value, list) and value and all(isinstance(v, str) and v.strip() for v in value)):
        raise _BadRequest(f"{field} must be a non-empty list of non-empty strings, got {value!r}")
    return [v.strip() for v in value]


def _sources_of(body) -> list[str] | None:
    """The `sources` scope: None when omitted or null (the route's global scope), else the named sources. An empty
    list is refused: it is neither scope, and reading it as 'every source' turned a UI bug into a global action."""
    sources = _dict(body).get("sources")
    return None if sources is None else _str_list(sources, "sources")


def _durable(handler):
    """A route that records an operator decision in config.db snapshots it to S3 as soon as the write succeeds, by the
    lifecycle verbs' one mechanism (lifecycle.snapshot_config; a no-op off the config server). The periodic save alone
    leaves up to one interval of decisions on the task's disk, lost when the task dies or a roll replaces it first.
    A refused write changed nothing, so it snapshots nothing."""
    @functools.wraps(handler)
    def durable(params, body, query):
        code, payload = handler(params, body, query)
        if 200 <= code < 300:
            from stone_pipeline import lifecycle
            lifecycle.snapshot_config()
        return code, payload
    durable.snapshots_config = True
    return durable


# -- run: the produce trigger ------------------------------------------------------------------------------
def _post_run(params, body, query):
    # Body (all optional): {"sources": ["zucchi", ...], "stage": "scrape"|"catalog"|"all"|..., "close_only": bool,
    # "clicks_only": bool}. sources omitted -> every enabled source; stage omitted -> "all" (scrape -> catalog ->
    # gate); close_only (stage photos only, an operator's action) makes the chosen photos' close-ups again;
    # clicks_only (stage photos only, the hidden-listings page's "run now") works only the listings with a click.
    from stone_pipeline.config.control import runner
    b = _dict(body)
    rec, code = runner.start_run(_sources_of(b), _str(b, "stage") or "all", close_only=b.get("close_only", False),
                                 clicks_only=b.get("clicks_only", False))   # (record, status)
    return code, rec


def _get_run(params, body, query):
    from stone_pipeline.config.control import runner
    return 200, runner.current()


def _get_run_by_id(params, body, query):
    from stone_pipeline.config.control import runner
    rec = runner.get_run(params["run_id"])
    return (200, rec) if rec else (404, {"error": f"no run {params['run_id']!r}"})


# -- diagnostics: per-source pipeline diagnostics for the admin UI (read-only) -----------------------------
def _get_diagnostics(params, body, query):
    from stone_pipeline.config.control import diagnostics
    rows = diagnostics.read_all()
    _attach_image_progress(rows)
    resp = {"diagnostics": rows}
    if (pending := diagnostics.pending_apply()) is not None:
        resp["pending_apply"] = pending      # global "N decisions changed since last produce -> re-produce"
    return 200, resp


def _get_diagnostics_source(params, body, query):
    from stone_pipeline.config.control import diagnostics
    summary = diagnostics.read_source(params["source"])
    if summary:
        _attach_image_progress([summary])
    return (200, summary) if summary else (404, {"error": f"no diagnostics for {params['source']!r}"})


# -- lifecycle verbs --------------------------------------------------------------------------------------
def _post_reset(params, body, query):
    # clean-start the ledger sync state (the coordinated (1)(2)(3) reset, our (1) half). Body (optional):
    #   {"hard": true, "sources": ["zucchi", ...]}   hard also drops scraped products; sources scopes.
    #     Hard and soft resets never touch the hosted product images.
    #   {"pristine": true}                           factory cold start: global hard reset + wipe the
    #     durable operator overlay (variety/leaf/retired decisions) so the next produce is seed-only,
    #     AND the only image wipe in the system (everything under <env>/products/: scraped originals,
    #     re-hosted photos, published standardised views + 3D slabs, manifest). The photo process's stored
    #     results are kept, so its next run republishes the chosen photos without paying again.
    #   {"pristine": true, "keep_images": true}      same, but KEEP the hosted product images (a re-scrape
    #     reuses them and products link their standardised photos at once) -- the test-reset path.
    #   {"pristine": true, "keep_scrape": true}      same, but KEEP the cached scrape (data/ + outputs/)
    #     so the catalog is rebuilt with a Republish All instead of a re-scrape.
    # 409 if a run/serve is active (never reset mid-run); base variant config is never deleted. A GLOBAL
    # reset also clears the config.db review queue + operator-pasted attribute ids (response.config),
    # so the clean start is coherent across both stores; a scoped reset leaves config.db alone.
    from stone_pipeline import lifecycle
    b = _dict(body)
    result, code = lifecycle.reset(_sources_of(b), _flag(b, "hard"), pristine=_flag(b, "pristine"),
                                   keep_images=_flag(b, "keep_images"), keep_scrape=_flag(b, "keep_scrape"))
    return code, result


def _post_curation_rebuild(params, body, query):
    # global incremental curation rebuild (curation state 1 repair): reseed the base FILE from the
    # committed pristine seed + kick a catalog re-derive, WITHOUT a factory reset's ledger/image wipe or
    # re-scrape. Curation is global (a variety belongs to the canonical catalog, not a source), so this
    # is not per-source. 409 if a produce/reset/pull is in flight; returns a summary (base_reseed +
    # rederive run to watch). Blokport also gates the button on 'no pull running'.
    from stone_pipeline import lifecycle
    result, code = lifecycle.rebuild_curation()
    return code, result


def _post_alias_classification(params, body, query):
    # P2-10 C5: the alias classification file and its manifest field, from the live ledger, read-only (409 unless
    # every variation is synced, naming the Keys). The backend operator calls it right after the draining Publish.
    from stone_pipeline import lifecycle
    result, code = lifecycle.alias_classification()
    return code, result


def _post_purge(params, body, query):
    # dead-stock purge: hard-delete the qty-0 (delisted) products. Returns external_ids so Medusa
    # deletes the same set (product + scraper_sync_ref). Guarded; base variations untouched.
    from stone_pipeline import lifecycle
    result, code = lifecycle.purge(_sources_of(body))
    return code, result


def _post_clean(params, body, query):
    # housekeeping for the admin: POST {} prunes superseded scrapes/runs; POST {"sources":[...]}
    # DELETES those sources' raw scraped data (fresh re-scrape). Base config + ledger untouched.
    from stone_pipeline import lifecycle
    result, code = lifecycle.clean(_sources_of(body))
    return code, result


def _post_delist(params, body, query):
    # stage 1 of a vendor removal ('Take offline'): {"sources": ["zucchi", ...]} sets those sources'
    # products to qty 0 (Medusa pulls them out of sale) AND disables them so a scrape never re-stocks
    # an offline vendor. Reversible: re-enable + re-scrape. Guarded (409 if a run/pull is active).
    from stone_pipeline import lifecycle
    result, code = lifecycle.delist(_sources_of(body))
    return code, result


def _post_pause(params, body, query):
    # freeze a source WITHOUT touching its stock: {"sources": ["zucchi", ...]}. Pause stops scraping
    # (lifecycle=paused, enabled 0) but leaves products live & buyable at their last values.
    from stone_pipeline import lifecycle
    result, code = lifecycle.pause(_sources_of(body))
    return code, result


def _post_resume(params, body, query):
    # unfreeze: re-enables (lifecycle=active) so the next run scrapes again. Config-only, no ledger work.
    from stone_pipeline import lifecycle
    result, code = lifecycle.resume(_sources_of(body))
    return code, result


# -- variations: the variety half of the lifecycle (explicit removal + undo) ------------------------------
def _get_variations_minted(params, body, query):
    # the KEY-BEARING list of varieties minted since the committed base (each grouped with its category
    # Keys), so the UI can offer checkbox-select unmint. Needed because unmint is keyed by the variation
    # Key while the mint-review queue is name-keyed candidates.
    from stone_pipeline import lifecycle
    result, code = lifecycle.list_minted_variations()
    return code, result


def _post_variations_unmint(params, body, query):
    # BULK unmint, two modes:
    #   {"keys": [...], "force"?}       unmint the pasted Keys (each block/slab/tile is its own Key)
    #   {"all_minted": true, "force"?}  unmint EVERY variety not in the committed base (scraper finds the
    #                                   Keys itself -- no paste needed; refuses 503 if no base is available)
    from stone_pipeline import lifecycle
    b = _dict(body)
    force = _flag(b, "force")
    if _flag(b, "all_minted"):
        result, code = lifecycle.unmint_all_minted(force=force)
    else:
        result, code = lifecycle.unmint_variations(b.get("keys"), force=force)
    return code, result


def _post_variation_verb(params, body, query):
    #   POST /variations/<key>/retire     {"force": true?}   remove a variety (E11 re-key old side)
    #   POST /variations/<key>/unmint     {"force": true?}   remove it AND clear its mint decision so the
    #     next produce re-surfaces it in the mint queue UNDECIDED (correct a minting mistake). Contrast
    #     retire (permanent, excluded) and reject (never mint); unmint = remove and reconsider.
    #   POST /variations/<key>/un_retire                     reverse it (mirrors source resume)
    #   POST /variations/<key>/not_a_duplicate               cancel a false-positive dup tombstone
    #     (curation state 2): keep the variety + record a durable 'protected' verdict so a future
    #     seed-reconcile never re-drops it. Idempotent + scoped result.
    from stone_pipeline import lifecycle
    key, verb = params["key"], params["verb"]
    if verb in ("retire", "unmint"):
        force = _flag(_dict(body), "force")
        op = lifecycle.retire_variation if verb == "retire" else lifecycle.unmint_variation
        result, code = op(key, force=force)
    elif verb == "un_retire":
        result, code = lifecycle.un_retire(key)
    elif verb == "not_a_duplicate":
        result, code = lifecycle.not_a_duplicate(key)
    else:
        return 404, {"error": "expected POST /config/v1/variations/unmint (bulk) or "
                              "/config/v1/variations/<key>/{retire,unmint,un_retire,not_a_duplicate}"}
    return code, result


# -- review: the new-variant review queue for the :4200 admin ---------------------------------------------
# The produce SURFACES uncertain items; the operator decides here; the NEXT produce APPLIES it (decisions
# are read once at curate start, so an edit takes effect on the following run -- same "applies next run"
# contract as the run guard).
def _get_review_variants(params, body, query):
    # ONE list: the variety cards plus the origin confirmations in the same card shape (kind 'origin'), so
    # the operator reviews everything in one place with one statement (/review/decide).
    from stone_pipeline.config import decisions_store
    cards = (decisions_store.list_pending("variety")
             + [_origin_as_card(o) for o in decisions_store.list_pending("origin")]
             # decisions the last produce did NOT honour (stages.decision_audit): restate or clear
             + [_gap_as_card(g) for g in decisions_store.list_pending("decision_gap")])
    # Decided cards sink to the END, undecided keep their order at the top, so the operator always works
    # the front of one list and never loses their place. A decided card is NOT removed: it stays pending
    # until the next produce binds it, and re-stating over it revises the decision. Stable sort.
    cards.sort(key=lambda c: bool(c.get("decided")))
    for c in cards:
        # "Will bind" / "Will mint" from the server (PHASE-2-SPEC:178), for the name and type the card starts from:
        # the standing decision's, else the scraper's guess (the admin card's own prefill order)
        c["resolution"] = decisions_store.resolution(
            c.get("current_alias_of") or c.get("current_seed_name") or c.get("variant") or "",
            c.get("current_seed_type") or c.get("stone_type") or "")
    decided = sum(1 for c in cards if c.get("decided"))
    return 200, {"variants": cards,
                 # progress signal: the list length cannot move while reviewing, so without a count
                 # there is no way to tell a finished review from an untouched one.
                 "counts": {"total": len(cards), "decided": decided, "undecided": len(cards) - decided}}


def _put_review_variant(params, body, query):
    # {"action":"reject"} is the one explicit action: "not a variety" is not a statement about fields.
    # Everything else is a statement (PUT /review/decide).
    from stone_pipeline.config import decisions_store
    if not isinstance(body, dict):
        return 400, {"error": "body must be a JSON object {action, alias_of?}"}
    # DECODE the path segment before it becomes the decision key. The variety was SURFACED with
    # review_pending.ref = norm(real name) (e.g. 'alpine luxe'); a multi-word name arrives here
    # percent-encoded ('Alpine%20Luxe'), and norm keeps the literal '%20' ('alpine 20luxe'), so a raw
    # segment would store the decision under a key that never matches the pending ref and the UI reads
    # back current_action=null. unquote first, so ref == variant_norm holds by construction.
    # a card ref is `<name>|<type>` for a typed card (stages.decisions.write_confirm_file); the reject is
    # keyed on the NAME (a rejected spelling is rejected for every type), so both the ref and a bare name work
    variant = unquote(params["variant"]).split("|")[0]
    if body.get("action", "") != "reject":
        return 400, {"error": "action must be 'reject'; anything else is a statement: PUT /review/decide"}
    try:
        decisions_store.reject("", variant)              # the legacy card-level reject: global, keyed by the ref
    except decisions_store.InvalidDecision as e:
        return 400, {"error": str(e)}
    return 200, {"variant": variant, "action": "reject"}


def _get_review_attributes(params, body, query):
    from stone_pipeline.config import decisions_store
    return 200, {"attributes": decisions_store.list_pending("attribute")}


def _name_refusal(field: str, value: str) -> tuple[int, dict] | None:
    """The one refusal of a stated name, the backend's rule: 503 while the field kinds are not published, 400 for a
    value with no ASCII form, 400 with `clean` for a value that is not its own clean form. None when it is clean."""
    from stone_pipeline.core.text import UnfoldableName
    from stone_pipeline.reference.loaders import field_kinds
    try:
        cleaned = field_kinds.clean(field, value)
    except field_kinds.FieldKindsError:
        return 503, {"error": "field kinds not published; retry once the backend has published field-kinds.json"}
    except UnfoldableName:
        return 400, {"error": f"{value!r} has no ASCII form; state it in plain letters"}
    if cleaned != value:
        return 400, {"error": f"{value!r} is not in its clean form", "clean": cleaned}
    return None


def _put_review_attribute(params, body, query):
    # {"kind":..., "medusa_id":...} for a pending attribute value (needs a Medusa id)
    from stone_pipeline.config import decisions_store
    from stone_pipeline.reference.loaders import field_kinds
    if not isinstance(body, dict):
        return 400, {"error": "body must be a JSON object {kind, medusa_id}"}
    value = unquote(params["value"])   # same decode-before-key rule as the variety PUT
    kind, medusa_id = _str(body, "kind"), _str(body, "medusa_id")
    # the value becomes the name the products carry, so it is refused unless it is its kind's clean form (W14)
    field = field_kinds.ATTRIBUTE_FIELDS.get(kind.lower())
    if field is None:
        return 400, {"error": f"kind must be one of {sorted(field_kinds.ATTRIBUTE_FIELDS)}"}
    if err := _name_refusal(field, value):
        return err
    # VE2: a restricted (archived or internal) Medusa id must never be pasted onto a pending value -- it would
    # make products unassignable. Dormant until the status columns exist (restricted_ids is then empty).
    from stone_pipeline.reference.loaders import restricted_ids
    if medusa_id and medusa_id in restricted_ids():
        return 400, {"error": f"attribute id {medusa_id!r} is archived or internal in Medusa; "
                              "assign an active value", "restricted": True}
    try:
        decisions_store.set_attribute_id(kind, value, medusa_id)
    except decisions_store.InvalidDecision as e:
        return 400, {"error": str(e)}
    return 200, {"value": value, "kind": body.get("kind"), "medusa_id": body.get("medusa_id")}


def _decide_listings(body) -> list[tuple[str, str]]:
    # the listings a statement applies to: the card's `listings` ([{source, scraped}], a card shared by
    # several vendors), or one `source` with `scraped` as a spelling or a list of spellings
    source = _str(body, "source")
    raw_listings = body.get("listings")
    if raw_listings is None:
        raw_listings = []
    if not (isinstance(raw_listings, list) and all(
            isinstance(l, dict) and isinstance(l.get("source"), str) and isinstance(l.get("scraped"), str)
            for l in raw_listings)):
        raise _BadRequest(f"listings must be a list of {{source, scraped}} string objects, got {raw_listings!r}")
    listings = [(l["source"].strip(), l["scraped"].strip()) for l in raw_listings]
    raw_scraped = body.get("scraped")
    if isinstance(raw_scraped, list):
        if not all(isinstance(s, str) for s in raw_scraped):
            raise _BadRequest(f"scraped must be a spelling or a list of spellings, got {raw_scraped!r}")
        spellings = [s.strip() for s in raw_scraped]
    else:
        spellings = [_str(body, "scraped")]
    listings += [(source, s) for s in spellings if s and source]
    return list(dict.fromkeys(l for l in listings if l[0] and l[1]))


def _delete_review_decide(params, body, query):
    # DELETE {listings}: clear each vendor's statement on the spelling, all or nothing. Refused when a statement
    # the last produce APPLIED bound products of its listing: undone, they may be held on the next produce, and
    # a held product is never written, so it would stay live in Medusa under the old variety with frozen stock.
    # Restating the listing (PUT) re-binds those products in place instead. A statement made after the last
    # applying produce has reached no product yet, so undoing it is always safe. run_log keeps only the newest
    # 50 runs, so "no applying run on record" is ambiguous (never produced, or rolled out by later inventory
    # runs): there the produce output decides, and no output at all means nothing from this host is live.
    from stone_pipeline.config import decisions_store, store
    from stone_pipeline.config.control import resolved
    if not isinstance(body, dict):
        return 400, {"error": "body must be a JSON object {source, scraped, name, type, ...}"}
    listings = _decide_listings(body)
    if not listings:
        return 400, {"error": "listings [{source, scraped}] or source + scraped are required"}
    try:
        decisions_store.check_clearable(listings)          # every listing first: a refused one clears none
    except decisions_store.InvalidDecision as e:
        return 400, {"error": str(e)}
    applied_at = store.last_applied_produce_at()
    blocked = []
    for s, sp in listings:
        decided_at = decisions_store.statement_decided_at(s, sp)
        if decided_at is None or (applied_at is not None and decided_at > applied_at):
            continue
        bound = resolved.bound_products(s, sp)
        if bound is None:
            if applied_at is None:
                continue
            return 409, {"error": f"cannot verify which live products depend on {s} '{sp}': this host has no "
                                  f"produce output. Restate the listing with the correct variety instead."}
        if bound:
            blocked.append({"source": s, "scraped": sp, "products": bound})
    if blocked:
        return 409, {"error": "undo refused: live products are bound by this decision, and removing it would "
                              "leave them listed under the current variety with frozen stock. Restate the "
                              "listing with the correct variety instead.",
                     "blocked": blocked}
    return 200, {"listings": [{"source": s, "scraped": sp} for s, sp in listings],
                 "cleared": [{"statements": n} for n in decisions_store.clear_all(listings)]}   # one transaction


def _resolve_target(name: str, stone_type: str) -> str:
    """The canonical variety a stated name resolves to: itself if it exists as that type, else its alias
    target, else itself (an as-yet-unknown mint name). Used to compare statements on the resolved variety."""
    from stone_pipeline.config import varieties
    if varieties.exists_as(name, stone_type):
        return name
    return varieties.alias_target(name, stone_type) or name


def _all_reconfirm(statements, listings, canonical: str, stone_type: str, color: str, origin: str, widen: bool) -> bool:
    """True iff EVERY listing already carries the IDENTICAL operator statement: the vendor row for (source,
    spelling), else the global ('', spelling) row, resolving to the same variety and matching type, colour,
    origin and widen. An identical statement stores nothing new, so re-stating it (on an archived variety, or
    with a restricted colour the record already carries) is a pure no-op the gate allows. Read from the raw
    statement rows, not the derived Decisions: a bind is folded into bindings there, and its colour is kept
    only at variety level, so the per-listing comparison needs the row. `statements`: decisions_store.statement_rows()."""
    from stone_pipeline.matching import projections as proj
    rows = {(proj.norm(r["source"]), proj.norm(r["spelling_norm"])): r for r in statements}
    want = (proj.norm(canonical), proj.norm(stone_type), proj.norm(color), (origin or "").upper(), bool(widen))
    for source, scraped in listings:
        sp = proj.norm(scraped)
        r = rows.get((proj.norm(source), sp)) or rows.get(("", sp))
        if r is None or r["verdict"] != "is":
            return False
        name_eff = r["name"] or r["spelling"]                   # a non-renamed mint stores no name: the variety is the spelling
        got = (proj.norm(_resolve_target(name_eff, r["stone_type"] or "")), proj.norm(r["stone_type"] or ""),
               proj.norm(r["color"] or ""), (r["origin_iso"] or "").upper(), bool(r["widen"]))
        if got != want:
            return False
    return True


def _variety_carries_colour(canonical: str, stone_type: str, color: str, statements) -> bool:
    """True iff the variety already documents `color`: its backbone SEED colours grown by the config.db leaf
    OVERLAY (the same view the tree is built from), plus any operator-STATED colour. Restating a colour the
    variety already carries is not an assignment, so it passes the colour gate even once that colour is
    restricted. Existing readers only (load_backbone + backbone_leaf_overlay + load_decisions.colours, built from the
    `statements` the gate read)."""
    from stone_pipeline.config import decisions_store
    from stone_pipeline.matching import projections as proj
    from stone_pipeline.reference.loaders import load_backbone
    backbone = load_backbone()
    backbone.apply_leaf_overlay(decisions_store.backbone_leaf_overlay())
    variety = backbone.lookup(canonical, stone_type)
    carried = {proj.norm(c) for c in (variety.colors if variety else [])}
    carried |= {proj.norm(c) for c in decisions_store.load_decisions(rows=statements).colours.get(
        (proj.norm(canonical), proj.norm(stone_type)), [])}
    return proj.norm(color) in carried


def _ve2_decide_gate(listings, name: str, stone_type: str, color: str, origin: str, widen: bool):
    """The VE2 operator gate for PUT /review/decide: refuse a statement that would ASSIGN a restricted
    (archived or internal) value, or BIND onto an archived variety. A value is judged ONLY where the statement
    assigns it, exactly as validate's value_frozen exemption judges a product only on a value it carries:
      * MINT (the target variety does not yet exist): its type AND colour are assigned now, both must be
        assignable.
      * an existing variety: an IDENTICAL re-confirm of the stored statement is a no-op and always allowed;
        otherwise a BIND onto an ARCHIVED variety is refused, and a COLOUR the variety does not already carry
        must be assignable (a bind's carried colour, already documented, is not an assignment).
    Returns an (status, body) error to refuse, or None to allow. Dormant until Blokport publishes the status
    columns: restricted_values is empty and is_archived is False for every variety, so this returns None."""
    from stone_pipeline.config import varieties
    from stone_pipeline.matching import projections as proj
    from stone_pipeline.reference.loaders import restricted_values
    restricted = restricted_values()
    rtypes, rcolors = restricted.get("type", set()), restricted.get("color", set())
    canonical = name if varieties.exists_as(name, stone_type) else varieties.alias_target(name, stone_type)
    if not canonical:                                            # MINT: a brand-new variety
        if proj.norm(stone_type) in rtypes:
            return 400, {"error": f"type {stone_type!r} is archived or internal in Medusa; assign an active type",
                         "restricted": True}
        if color and proj.norm(color) in rcolors:
            return 400, {"error": f"colour {color!r} is archived or internal in Medusa; pick an active colour",
                         "restricted": True}
        return None
    # an existing variety: a BIND or a COLOUR AMEND
    archived = varieties.is_archived(canonical, stone_type)
    colour_restricted = bool(color) and proj.norm(color) in rcolors
    if not archived and not colour_restricted:
        return None                                             # nothing to gate (the dormant path)
    from stone_pipeline.config import decisions_store
    statements = decisions_store.statement_rows()               # read once: the re-confirm and the colour check share it
    if _all_reconfirm(statements, listings, canonical, stone_type, color, origin, widen):
        return None                                             # identical to the stored statement: a no-op
    if archived:
        return 400, {"error": f"'{canonical}' ({stone_type}) is archived in Medusa; bind to an active "
                              "variety, mint as new, or reactivate it in Blokport", "archived": True}
    if colour_restricted and not _variety_carries_colour(canonical, stone_type, color, statements):
        return 400, {"error": f"colour {color!r} is archived or internal in Medusa; pick an active colour",
                     "restricted": True}
    return None


def _put_review_decide(params, body, query):
    # ONE statement, whatever list it comes from (pending card or resolved row): "for vendor <source>, the
    # product scraped as <scraped> is <name>, a <type>, <color>, from <origin>". The backend derives the
    # outcome (bind to the existing variety / mint it / the vendor's origin), see decisions_store.decide.
    #   PUT {source, scraped, name, type, color?, origin?, widen?}
    from stone_pipeline.config import decisions_store
    from stone_pipeline.matching import projections as proj
    if not isinstance(body, dict):
        return 400, {"error": "body must be a JSON object {source, scraped, name, type, ...}"}
    listings = _decide_listings(body)
    if not listings:
        return 400, {"error": "listings [{source, scraped}] or source + scraped are required"}
    name = _str(body, "name")
    raw_type = _str(body, "type")
    widen = _flag(body, "widen")
    if not name or not raw_type:
        return 400, {"error": "name and type are required"}
    from stone_pipeline.reference.loaders import field_kinds
    if err := _name_refusal(field_kinds.VARIATION_NAME, name):     # the stated name is stored as given (W12)
        return err
    vocab = _type_vocab()
    if proj.norm(raw_type) not in vocab:
        return 400, {"error": f"type {raw_type!r} is not a known Medusa stone-type attribute"}
    stone_type = vocab[proj.norm(raw_type)]
    # a colour, when given, must be a real Medusa colour attribute (a minted variety is seeded with it; a
    # value Medusa lacks would null-id every product), stored in its canonical casing
    color = ""
    if (raw_color := _str(body, "color")):
        colors = _color_vocab()
        if proj.norm(raw_color) not in colors:
            return 400, {"error": f"color {raw_color!r} is not a known Medusa colour attribute"}
        color = colors[proj.norm(raw_color)]
    raw_origin = _str(body, "origin")
    origin = _resolve_origin_iso(raw_origin) if raw_origin else ""
    if raw_origin and not origin:
        return 400, {"error": f"origin {raw_origin!r} is not a real ISO-3166 country"}
    from stone_pipeline.phase1.variety_key import new_key
    from stone_pipeline.stages import decisions as _decisions
    if new_key(stone_type, name) in _decisions.load_retired():
        return 409, {"error": f"'{name}' ({stone_type}) is a retired variety; un-retire it first",
                     "retired": True}
    if (refusal := _ve2_decide_gate(listings, name, stone_type, color, origin, widen)) is not None:
        return refusal
    try:
        # one transaction: a listing the store refuses leaves none of the others stored
        outcomes = decisions_store.decide_all(listings, name, stone_type, color, origin, widen)
    except decisions_store.InvalidDecision as e:
        return 400, {"error": str(e)}
    return 200, {**outcomes[0], "listings": [{"source": s, "scraped": sp, "result": o["result"]}
                                             for (s, sp), o in zip(listings, outcomes)],
                 "decided": len(outcomes)}


def _get_review_resolved(params, body, query):
    # GET /review/resolved[?source=&decided=true|false]: the last produce's resolution per product, with
    # the standing decision overlaid (no approval needed)
    from stone_pipeline.config.control import resolved
    qs = parse_qs(query)
    decided = (qs.get("decided") or [None])[0]
    return 200, {"resolved": resolved.list_resolved(
        source=(qs.get("source") or [None])[0] or None,
        decided=None if decided is None else decided.lower() == "true")}


def _get_review_backbone(params, body, query):
    # pending leaf additions (value not yet on a variety)
    from stone_pipeline.config import decisions_store
    return 200, {"backbone": decisions_store.list_pending("backbone_leaf")}


def _get_review_backbone_decided(params, body, query):
    # already-decided leaves (with their refs), so a past verdict can be revised -- e.g. un-approve a
    # spurious approval that a prior run grew into the overlay.
    from stone_pipeline.config import decisions_store
    return 200, {"decided": decisions_store.list_decided_leaves()}


def _post_review_backbone_approve_all(params, body, query):
    # the primary UX: approve every pending leaf suggestion (or only one verdict, e.g. likely_real).
    from stone_pipeline.config import decisions_store
    return 200, {"approved": decisions_store.approve_leaf_pending(_str(_dict(body), "verdict") or None)}


def _put_review_backbone_leaf(params, body, query):
    # {"action":"approve"|"reject"|"clear"}: one verdict (approve/reject also un-approve a DECIDED leaf;
    # clear undoes it)
    from stone_pipeline.config import decisions_store
    if not isinstance(body, dict):
        return 400, {"error": "body must be a JSON object {action}"}
    ref, action = unquote(params["ref"]), _str(body, "action")
    try:
        # a still-pending suggestion decides through the queue; a leaf a prior run already decided (e.g. a
        # spurious approval to undo) has dropped off that queue, so revise it in place by ref. 'clear' only
        # applies to a decided leaf, so it skips the pending path.
        ok = (action != "clear" and decisions_store.decide_leaf_pending(ref, action)) \
            or decisions_store.revise_leaf_decision(ref, action)
    except decisions_store.InvalidDecision as e:
        return 400, {"error": str(e)}
    if not ok:
        return 404, {"error": f"no pending or decided backbone-leaf for ref {ref!r}"}
    return 200, {"ref": ref, "action": action}


# -- learned aliases (P2-10 C6): the AliasesPanel's read, approve/reject and the one delete -------------------------
_ALIAS_ACTIONS = {"approve": "active", "reject": "rejected"}


def _alias_id(params) -> tuple[str, str]:
    from stone_pipeline.aliases import review
    try:
        return review.parse_id(unquote(params["id"]))
    except ValueError as exc:
        raise _BadRequest(str(exc)) from exc


def _get_review_aliases(params, body, query):
    # ?status=proposed|active|rejected narrows the cards; the counts and the cleanup's open items are always whole
    from stone_pipeline.aliases import open_items, review
    from stone_pipeline.config import decisions_store
    status = _query(query, ("status",))["status"]
    if status is not None and status not in decisions_store.LEARNED_STATUSES:
        raise _BadRequest(f"status must be one of {list(decisions_store.LEARNED_STATUSES)}, got {status!r}")
    try:
        return 200, review.review(status)
    except (open_items.OpenItemsUnreadable, review.LedgerUnavailable) as exc:
        return 503, {"error": str(exc)}


def _put_review_alias(params, body, query):
    # {"action":"approve"|"reject"}: approve makes the learned alias a matcher input, reject withdraws it for good
    # (on a proposal or on an approved alias); its norm is never proposed again for that variety. Approve keeps C1's
    # alias boundary at the decision (review.approval_refusal): a refused value is a 400 naming the reason and the row
    # is unchanged. Reject is allowed on anything.
    from stone_pipeline.aliases import review
    from stone_pipeline.config import decisions_store
    key, norm = _alias_id(params)
    if not isinstance(body, dict) or _str(body, "action") not in _ALIAS_ACTIONS:
        return 400, {"error": f"body must be a JSON object {{action}}, action one of {sorted(_ALIAS_ACTIONS)}"}
    status = _ALIAS_ACTIONS[_str(body, "action")]
    row = decisions_store.learned_alias(key, norm)
    if row is None:
        return 404, {"error": f"no learned alias {review.card_id(key, norm)!r}"}
    if status == "active":
        try:
            reason = review.approval_refusal(key, row["value"])
        except review.LedgerUnavailable as exc:
            return 503, {"error": str(exc)}
        if reason is not None:
            return 400, {"error": f"{row['value']!r} cannot be an alias of {key}: {reason}", "reason": reason}
    decisions_store.decide_learned_alias(key, norm, status, decided_by="operator")
    return 200, {"id": review.card_id(key, norm), "status": status}


def _delete_review_alias(params, body, query):
    # the ONLY removal of a learned value; forgotten, not rejected, so the matcher may propose it again
    from stone_pipeline.aliases import review
    from stone_pipeline.config import decisions_store
    key, norm = _alias_id(params)
    if not decisions_store.delete_learned_alias(key, norm):
        return 404, {"error": f"no learned alias {review.card_id(key, norm)!r}"}
    return 200, {"id": review.card_id(key, norm), "deleted": True}


# -- lots: "which slabs are sold?" ---------------------------------------------------------------------------
def _get_review_lots(params, body, query):
    # the lots whose sold slabs the vendor's data does not identify, each with every scanned slab to choose from
    from stone_pipeline.config import decisions_store
    return 200, {"lots": decisions_store.list_pending("lot")}


def _put_review_lot(params, body, query):
    # {"sold": [slab positions]}: the operator's answer for the card's current stock figure
    from stone_pipeline.config import decisions_store
    if not isinstance(body, dict):
        return 400, {"error": "body must be a JSON object {sold: [positions]}"}
    ref = unquote(params["ref"])
    try:
        sold = decisions_store.answer_lot(ref, body.get("sold"))
    except LookupError as e:
        return 404, {"error": str(e)}
    except decisions_store.InvalidDecision as e:
        return 400, {"error": str(e)}
    return 200, {"ref": ref, "sold": sold}


# -- origins: PER-VARIETY origin EDITS (the "edit origins" admin action) ----------------------------------
# The same channel a mint uses for its origin (seed_country), but for ANY variety and holding a country
# LIST. Overlaid onto the origin map on the next produce; the per-vendor gate then picks each vendor's
# country from that list.
def _get_origins(params, body, query):
    from stone_pipeline.config import decisions_store
    return 200, {"origins": decisions_store.list_variety_origins()}


def _get_origins_lookup(params, body, query):
    # ?variety=&stone_type= -> base + edited + effective for one variety
    from stone_pipeline.config import decisions_store
    qs = parse_qs(query)
    variety = (qs.get("variety", [""])[0] or "").strip()
    stone_type = (qs.get("stone_type", [""])[0] or "").strip()
    if not (variety and stone_type):
        return 400, {"error": "lookup needs ?variety=&stone_type="}
    from stone_pipeline.reference import loaders
    rule = loaders.load_origin_map().exact(variety, stone_type)
    base = list(rule.countries) if rule else []
    edit = decisions_store.get_variety_origin(variety, stone_type)
    return 200, {"variety": variety, "stone_type": stone_type, "base_countries": base,
                 "edited_countries": (edit["countries"] if edit else None),
                 "effective_countries": (edit["countries"] if edit else base)}


def _put_origin(params, body, query):
    # {variety, stone_type, countries:[...], city?, county?}
    from stone_pipeline.config import decisions_store
    if not isinstance(body, dict):
        return 400, {"error": "body must be {variety, stone_type, countries:[...]}"}
    variety = _str(body, "variety")
    stone_type = _str(body, "stone_type")
    city, county = _str(body, "city"), _str(body, "county")
    if not (variety and stone_type):
        return 400, {"error": "variety, stone_type and a non-empty countries list are required"}
    from stone_pipeline.reference.loaders import field_kinds
    for field, value in ((field_kinds.VARIATION_NAME, variety), (field_kinds.PRODUCT_ORIGIN_CITY, city),
                         (field_kinds.PRODUCT_ORIGIN_COUNTY, county)):
        if value and (err := _name_refusal(field, value)):     # each becomes a name the products carry (W13)
            return err
    isos = []
    for c in _str_list(body.get("countries"), "countries"):
        if not (iso := _resolve_origin_iso(c)):
            return 400, {"error": f"country {c!r} is not a real ISO-3166 country"}
        isos.append(iso)
    try:
        decisions_store.set_variety_origin(variety, stone_type, ",".join(isos), city, county)
    except decisions_store.InvalidDecision as e:
        return 400, {"error": str(e)}
    return 200, {"variety": variety, "stone_type": stone_type, "countries": isos}


def _delete_origin(params, body, query):
    # revert that variety to the base map
    from stone_pipeline.config import decisions_store
    ref = unquote(params["ref"])
    parts = ref.split("|")
    if len(parts) != 2:
        return 400, {"error": "ref must be <variety_norm>|<stone_type_norm>"}
    removed = decisions_store.delete_variety_origin(parts[0], parts[1])
    return (200, {"ref": ref, "removed": True}) if removed else (404, {"error": f"no origin edit {ref!r}"})


# -- vocabularies for the admin pickers (agnostic: just the lists) -----------------------------------------
def _get_varieties(params, body, query):
    # existing variety names (+ stone_type) from the durable ledger. Optional ?q=<substring> + ?limit=<n>
    # back a type-ahead: the full set is ~25k, so a client can narrow it instead of pulling ~1 MB per open.
    from stone_pipeline.config import varieties
    qs = parse_qs(query)
    q = (qs.get("q", [""])[0] or "").strip() or None
    raw_limit = (qs.get("limit", [""])[0] or "").strip()
    limit = int(raw_limit) if raw_limit.isdigit() else None
    return 200, {"varieties": varieties.list_all(q=q, limit=limit)}


def _get_adapters(params, body, query):
    # the coded adapters available to run (the auto-discovered REGISTRY): the :4200 admin turns the source
    # "adapter" field into a validated dropdown, so it blocks adding a source with no coded adapter (ISS-3).
    from stone_pipeline import adapters
    return 200, {"adapters": sorted(adapters.REGISTRY)}


def _get_colors(params, body, query):
    # the colour vocabulary for the mint colour picker (a colourless variety seeded with a real colour);
    # VE2: restricted (archived/internal) colours are never offered
    return 200, {"colors": _assignable_values("color", _color_vocab())}


def _get_types(params, body, query):
    # the stone-type vocabulary for the mint type picker (a type-less variety is held until it has one);
    # VE2: restricted (archived/internal) types are never offered
    return 200, {"types": _assignable_values("type", _type_vocab())}


# -- sources: the scraper control plane --------------------------------------------------------------------
def _get_sources(params, body, query):
    # enrich each source with what's IN the scraper: the raw scrape (scrape_at + scrape_rows) and how many
    # products reached the ledger (ledger_products) -- so :4200 shows what's there and how old.
    from stone_pipeline.config.control import scrape_status
    return 200, {"sources": scrape_status.enrich(store.list_rows())}


def _get_source(params, body, query):
    row = store.get_row(params["name"])
    return (200, row) if row else (404, {"error": f"no source {params['name']!r}"})


def _put_source(params, body, query):
    if not isinstance(body, dict):
        return 400, {"error": "body must be a JSON object of source settings"}
    name = params["name"]
    stored = store.get_row(name)
    if conflict := _source_state_conflict(name, stored, body):
        return conflict
    # The body updates the fields it names over the stored row, so a field the admin form does not render
    # keeps its value on save instead of resetting to its default. A new source has no stored row and is
    # validated as sent. The path name is authoritative.
    body = {**(stored or {}), **body, "source": name}
    err = _validate_source_put(name, body, stored)
    if err:
        return err
    try:
        store.upsert_row(body)
    except sqlite3.IntegrityError:
        # the DB's UNIQUE(source_code) backstop tripped: a concurrent PUT claimed the same code between the
        # app-level check and this write. Report a clean 400, not a 500.
        return 400, {"error": f"source_code {body.get('source_code', '')!r} is already in use by another source"}
    return 200, store.get_row(name)


def _delete_source(params, body, query):
    # stage 2 of a vendor removal ('Remove permanently'): purge the source's qty-0 products and drop its
    # config row. 409 if the source still has LIVE products (take it offline via /delist first).
    from stone_pipeline import lifecycle
    result, code = lifecycle.remove_source(params["name"])
    return code, result


# -- photos: the hidden-listings page (stone_pipeline.photos.review) ------------------------------------------------------
# The photo process's stores live in S3 and its decisions are written there inside its run lock, so none of these
# touches config.db. A store that cannot be read (the ledger, the review list) is a 503 naming it.
def _photos(call):
    from stone_pipeline.photos import review as rv
    try:
        return call(rv)
    except rv.Unavailable as exc:
        return 503, {"error": str(exc)}


def _photo_body(rv, body, fields):
    try:
        return rv.check_body(body, fields)
    except ValueError as exc:
        raise _BadRequest(str(exc)) from exc


def _query(query, names):
    """{name: its value, or None when absent} of these query parameters (an empty value is a value: ""); one given more
    than once is a 400 naming it, never its last value."""
    qs = parse_qs(query, keep_blank_values=True)
    if repeated := sorted(name for name in names if len(qs.get(name, ())) > 1):
        raise _BadRequest(f"{', '.join(repeated)} given more than once")
    return {name: qs[name][0] if name in qs else None for name in names}


def _page_param(value, name, default, low, high=None):
    """A whole-number query parameter (plain ASCII digits, at most REVIEW_PAGE_DIGITS of them) of at least low (and at
    most high, when given), default when absent; a 400 naming it otherwise."""
    from stone_pipeline.config.standardise_settings import STANDARDISE as C
    if value is None:
        return default
    digits = re.fullmatch(f"[0-9]{{1,{C.REVIEW_PAGE_DIGITS}}}", value)
    if digits is None or int(value) < low or (high is not None and int(value) > high):
        raise _BadRequest(f"{name} must be a whole number from {low}{f' to {high}' if high is not None else ''}, "
                          f"got {value!r}")
    return int(value)


def _get_photos_hidden(params, body, query):
    # ?reason=<one of the review reasons>&offset=<0..>&limit=<1..REVIEW_PAGE_MAX>, each at most once
    from stone_pipeline.config.standardise_settings import STANDARDISE as C

    def answer(rv):
        q = _query(query, ("reason", "offset", "limit"))
        reason = q["reason"]
        if reason is not None and reason not in rv.select.REVIEW_REASONS:
            raise _BadRequest(f"reason must be one of {', '.join(rv.select.REVIEW_REASONS)}, got {reason!r}")
        offset = _page_param(q["offset"], "offset", 0, 0)
        limit = _page_param(q["limit"], "limit", C.REVIEW_PAGE_DEFAULT, 1, C.REVIEW_PAGE_MAX)
        return 200, rv.hidden(rv.client(), reason, offset, limit)
    return _photos(answer)


def _get_photos_spend(params, body, query):
    # ?month=YYYY-MM, this month (UTC) when omitted (an empty or repeated month is a 400)
    from datetime import datetime, timezone

    def answer(rv):
        month = _query(query, ("month",))["month"]
        month = datetime.now(timezone.utc).strftime("%Y-%m") if month is None else month
        if not rv.valid_month(month):
            raise _BadRequest(f"month must be YYYY-MM, got {month!r}")
        return 200, rv.spend(rv.client(), month)
    return _photos(answer)


def _put_photo_click(params, body, query):
    # Body {"sha", "at"}; the SKU percent-encoded in the path (prod SKUs carry slashes)
    def answer(rv):
        b = _photo_body(rv, body, ("sha", "at"))
        return rv.click(rv.client(), unquote(params["sku"]), b["sha"], b["at"])
    return _photos(answer)


def _delete_photo_click(params, body, query):
    # Body {"at"}: when the owner withdrew it (an older withdrawal never undoes a later decision)
    def answer(rv):
        b = _photo_body(rv, body, ("at",))
        return rv.withdraw(rv.client(), unquote(params["sku"]), b["at"])
    return _photos(answer)


def _post_photo_decisions(params, body, query):
    # Body: the decision page's export (a JSON array of decision objects); ?dry_run=true plans only, false applies
    def answer(rv):
        flag = _query(query, ("dry_run",))["dry_run"]
        if flag not in ("true", "false"):
            raise _BadRequest(f"dry_run must be true (plan only) or false (apply), got {flag!r}")
        try:
            docs = rv.decisions.check_docs(body)
        except rv.decisions.InvalidDecisions as exc:
            raise _BadRequest(str(exc)) from exc
        return rv.apply_decisions(rv.client(), docs, flag == "true")
    return _photos(answer)


# _durable: the route records an operator decision in config.db, snapshotted as soon as it succeeds. The lifecycle verbs
# (reset, delist, pause, resume, remove a source) snapshot it themselves.
_ROUTES: tuple[tuple[str, tuple[str, ...], object], ...] = (
    ("POST", ("run",), _post_run),
    ("GET", ("run",), _get_run),
    ("GET", ("run", "{run_id}"), _get_run_by_id),
    ("GET", ("diagnostics",), _get_diagnostics),
    ("GET", ("diagnostics", "{source}"), _get_diagnostics_source),
    ("POST", ("reset",), _post_reset),
    ("POST", ("curation", "rebuild"), _post_curation_rebuild),
    ("POST", ("purge",), _post_purge),
    ("POST", ("aliases", "classification"), _post_alias_classification),
    ("POST", ("clean",), _post_clean),
    ("POST", ("delist",), _post_delist),
    ("POST", ("pause",), _post_pause),
    ("POST", ("resume",), _post_resume),
    ("GET", ("variations", "minted"), _get_variations_minted),
    ("POST", ("variations", "unmint"), _durable(_post_variations_unmint)),
    ("POST", ("variations", "{key}", "{verb}"), _durable(_post_variation_verb)),
    ("GET", ("review", "variants"), _get_review_variants),
    ("PUT", ("review", "variants", "{variant}"), _durable(_put_review_variant)),
    ("GET", ("review", "attributes"), _get_review_attributes),
    ("PUT", ("review", "attributes", "{value}"), _durable(_put_review_attribute)),
    ("PUT", ("review", "decide"), _durable(_put_review_decide)),
    ("DELETE", ("review", "decide"), _durable(_delete_review_decide)),
    ("GET", ("review", "resolved"), _get_review_resolved),
    ("GET", ("review", "backbone"), _get_review_backbone),
    ("GET", ("review", "backbone", "decided"), _get_review_backbone_decided),
    ("POST", ("review", "backbone", "approve_all"), _durable(_post_review_backbone_approve_all)),
    ("PUT", ("review", "backbone", "{ref}"), _durable(_put_review_backbone_leaf)),
    ("GET", ("review", "aliases"), _get_review_aliases),
    ("PUT", ("review", "aliases", "{id}"), _durable(_put_review_alias)),
    ("DELETE", ("review", "aliases", "{id}"), _durable(_delete_review_alias)),
    ("GET", ("review", "lots"), _get_review_lots),
    ("PUT", ("review", "lots", "{ref}"), _durable(_put_review_lot)),
    ("GET", ("origins",), _get_origins),
    ("GET", ("origins", "lookup"), _get_origins_lookup),
    ("PUT", ("origins", "{ref}"), _durable(_put_origin)),
    ("DELETE", ("origins", "{ref}"), _durable(_delete_origin)),
    ("GET", ("varieties",), _get_varieties),
    ("GET", ("adapters",), _get_adapters),
    ("GET", ("colors",), _get_colors),
    ("GET", ("types",), _get_types),
    ("GET", ("sources",), _get_sources),
    ("GET", ("sources", "{name}"), _get_source),
    ("PUT", ("sources", "{name}"), _durable(_put_source)),
    ("DELETE", ("sources", "{name}"), _delete_source),
    ("GET", ("photos", "hidden"), _get_photos_hidden),
    ("GET", ("photos", "spend"), _get_photos_spend),
    ("PUT", ("photos", "clicks", "{sku}"), _put_photo_click),
    ("DELETE", ("photos", "clicks", "{sku}"), _delete_photo_click),
    ("POST", ("photos", "decisions"), _post_photo_decisions),
)

# derived from the table, so the answer to an unknown path can never drift from what is served
_NOT_FOUND = "not found; the routes are: " + ", ".join(
    f"{method} /config/v1/{'/'.join(pattern)}" for method, pattern, _ in _ROUTES)


def _match(pattern: tuple[str, ...], segments: list[str]) -> dict | None:
    if len(pattern) != len(segments):
        return None
    params: dict = {}
    for token, seg in zip(pattern, segments):
        if token.startswith("{"):
            params[token[1:-1]] = seg
        elif token != seg:
            return None
    return params


def dispatch(method: str, segments: list[str], body, query: str = "") -> tuple[int, object]:
    """Route one request through the table. `segments` is the path under /config/v1 (e.g. ['sources'] or
    ['sources', 'polonine']); `query` is the raw URL query string (e.g. 'q=carr&limit=20').
    Pure: returns (status_code, json body)."""
    allowed: list[str] = []
    for route_method, pattern, handler in _ROUTES:
        params = _match(pattern, segments)
        if params is None:
            continue
        if route_method == method:
            try:
                return handler(params, body, query)
            except _BadRequest as exc:
                return 400, {"error": str(exc)}
        allowed.append(route_method)
    if allowed:
        return 405, {"error": f"{method} is not allowed on /config/v1/{'/'.join(segments)}; "
                              f"use {', '.join(dict.fromkeys(allowed))}"}
    return 404, {"error": _NOT_FOUND}


_SOURCE_INT_FIELDS = ("default_bundle_size", "min_expected_rows")
_SOURCE_STR_FIELDS = ("source_code", "vendor", "company_id", "origin_default", "primary_origin",
                      "collection_country", "collection_city")
_SOURCE_BOOL_FIELDS = ("image_pipeline", "emit_on_review")
# The run state POST /pause, /resume and /delist own, as a source that has none yet starts: active.
_SOURCE_STATE_FIELDS = ("enabled", "lifecycle")
_NEW_SOURCE_STATE = {"enabled": True, "lifecycle": "active"}


def _source_state_conflict(name: str, stored: dict | None, body: dict) -> tuple[int, dict] | None:
    """`enabled` and `lifecycle` are set only by POST /pause, /resume and /delist, which keep the two in step (a
    delisted source has enabled 0 so no scrape restocks it). The admin form sends `enabled` back at the value it
    read, so a state field equal to the source's current state is that round trip and is accepted; any other
    value is a stale form or a flip past the verbs (it re-enabled a delisted vendor), refused with 409. A null
    field reads as absent, like every optional field."""
    current = {f: stored[f] for f in _SOURCE_STATE_FIELDS} if stored else _NEW_SOURCE_STATE
    if body.get("enabled") is not None and not isinstance(body["enabled"], bool):
        raise _BadRequest(f"enabled must be true or false, got {body['enabled']!r}")
    changed = [f for f in _SOURCE_STATE_FIELDS if body.get(f) is not None and body[f] != current[f]]
    if not changed:
        return None
    return 409, {"error": f"{', '.join(changed)} is not set by this PUT: source {name!r} is {current['lifecycle']} "
                          f"(enabled {str(current['enabled']).lower()}). Change its state with POST "
                          "/config/v1/pause, /config/v1/resume or /config/v1/delist, and send its current value "
                          "here or omit the field."}


def _ledger_products(source_code: str) -> int:
    """How many products the sync ledger holds under `source_code` (their SKU prefix and product.source). A host
    with no ledger file has produced nothing under any code; a ledger that cannot be read raises (a guard never
    passes on an unread ledger)."""
    from stone_pipeline.ledger import writethrough
    path = writethrough.ledger_path()
    if not path.exists():
        return 0
    with writethrough.connect(path, bind=False) as lg:
        return lg.execute("SELECT COUNT(*) AS n FROM product WHERE source = ?", (source_code,)).fetchone()["n"]


def _validate_source_put(name: str, body: dict, stored: dict | None) -> tuple[int, dict] | None:
    """Guard the admin 'add/edit source' PUT so it can only create a RUNNABLE, non-colliding source.
    A source with no coded adapter can neither run nor be scoped (ISS-3), so it must never become a
    live-looking config row; a duplicate source_code collides SKU provenance for clear/remove (ISS-1).
    Returns an (status, error) tuple to short-circuit, or None when the source is valid; a field of the wrong
    type raises _BadRequest (400 naming it)."""
    from stone_pipeline import adapters
    from stone_pipeline.certify import _VALID_MODES
    if name not in adapters.REGISTRY:
        return 400, {"error": f"no coded adapter for source {name!r}; a runnable source needs a coded "
                     f"adapter (fetcher + adapter map). Known adapters: {sorted(adapters.REGISTRY)}"}
    for field in _SOURCE_STR_FIELDS:
        body[field] = _str(body, field)     # null reads as '' (the column default); the store never sees a None
    for field in _SOURCE_BOOL_FIELDS:
        if field in body and not isinstance(body[field], bool):
            raise _BadRequest(f"{field} must be true or false, got {body[field]!r}")
    # the trust ladder's rungs; the store would keep any string, and certify reads anything else as unconfigured
    if body.get("mode", "review") not in _VALID_MODES:
        raise _BadRequest(f"mode must be one of {', '.join(_VALID_MODES)}, got {body.get('mode')!r}")
    code = _str(body, "source_code")
    if code:
        for row in store.list_rows():
            if row["source"] != name and (row["source_code"] or "").strip().casefold() == code.casefold():
                return 400, {"error": f"source_code {code!r} already used by source {row['source']!r}; "
                             "codes must be unique (they scope SKUs for removal)"}
    # the code is the SKU prefix and the ledger's product.source: changing it under existing products re-creates
    # every one of them under new SKUs and orphans the old ones in Medusa. Compared as the store will keep it
    # (SourceConfig lowercases it and backfills an empty one), so a restated code is not a change.
    from stone_pipeline.config.sources import SourceConfig
    kept = SourceConfig(source=name, source_code=code).source_code
    if stored and stored["source_code"] != kept and (held := _ledger_products(stored["source_code"])):
        return 409, {"error": f"source_code {stored['source_code']!r} is the SKU prefix of {held} product(s) of "
                              f"{name!r} in the ledger; changing it would re-create them all under new SKUs and "
                              "orphan the old ones. Keep the code, or remove the source (delist, then DELETE) "
                              "and add it again under the new one."}
    # Ports are a HARD requirement, set by the admin here (never derived from the seed): Medusa builds a
    # product's shipping ports from the supplier's ports, so a source that ships without them makes Medusa
    # fall back to deriving a whole-country port list. A source must therefore carry at least one port.
    ports = body.get("ports")
    if isinstance(ports, list) and not all(isinstance(p, str) for p in ports):
        raise _BadRequest(f"ports must be a list of port names / LOCODEs (strings), got {ports!r}")
    if not (isinstance(ports, list) and any(p.strip() for p in ports)):
        return 400, {"error": f"source {name!r} requires at least one shipping port: set 'ports' to a "
                     "non-empty list of the supplier's port names / LOCODEs (Medusa derives a product's "
                     "ports from these)."}
    # the collection city is a name the products carry (W25): a stored unclean city is refused on every save until
    # it is re-saved clean, which the release runbook does once for zucchi
    from stone_pipeline.reference.loaders import field_kinds
    city = body["collection_city"]          # stripped above (_SOURCE_STR_FIELDS)
    if city and (err := _name_refusal(field_kinds.PRODUCT_COLLECTION_CITY, city)):
        return err
    # the integer fields are validated HERE, so a malformed value is the client's 400 (it used to reach
    # int() in the store and answer 500)
    for field in _SOURCE_INT_FIELDS:
        if field not in body:
            continue
        raw = body[field]
        try:
            int(str(raw).strip())           # null too: str(None) is no integer
        except ValueError:
            return 400, {"error": f"{field} must be an integer, got {raw!r}"}
    return None


def _expected_token() -> str:
    token = env.getenv("BLOKPORT_CONFIG_TOKEN", "").strip()
    if not token:
        raise SystemExit("SCRAPER_CONFIG_TOKEN is not set; refusing to start the config server")
    return token


class ConfigHandler(BaseHTTPRequestHandler):
    server_version = "scraper-config/1.0"   # brand-neutral banner (one image serves every brand)

    def _respond(self, code: int, payload: object) -> None:
        data = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # the client hung up before the reply landed (a page navigated away, a poller timed out): not an
            # error of ours, and http.server would print a full traceback per hang-up. One line, no trace.
            log.info("client hung up before the reply", extra={"extra_fields": {"path": self.path, "code": code}})

    def _authorized(self) -> bool:
        # constant-time compare so the token can't be recovered by response-timing (L1). Compare on BYTES:
        # hmac.compare_digest raises TypeError on a non-ASCII str, and a client controls the Authorization
        # header value (latin-1 decoded, so a high byte yields a non-ASCII str) -- on str that TypeError
        # escapes _authorized (called before the dispatch try/except) and drops the connection with a
        # traceback instead of a clean 401. utf-8 encoding always succeeds and has no ASCII restriction.
        header = self.headers.get("Authorization", "").encode("utf-8")
        expected = f"Bearer {self.server.expected_token}".encode("utf-8")  # type: ignore[attr-defined]
        return hmac.compare_digest(header, expected)

    def _handle(self, method: str) -> None:
        if not self._authorized():
            return self._respond(401, {"error": "unauthorized"})
        parts = urlsplit(self.path)
        seg = [s for s in parts.path.split("/") if s]
        if len(seg) < 2 or seg[0] != "config" or seg[1] != "v1":
            return self._respond(404, {"error": "not found; expected /config/v1/..."})
        body = None
        # DELETE carries a body too (DELETE /review/decide takes {listings}); reading it only for PUT/POST
        # handed that route no body, so every undo over HTTP was a 400.
        if method in ("PUT", "POST", "DELETE"):
            # A malformed Content-Length ('abc') must be a 400, not an int() ValueError that escapes the
            # handler (this parse sits BEFORE the dispatch try/except) and drops the connection; and the
            # length is client-controlled, so cap it before read() to bound memory.
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._respond(400, {"error": "invalid Content-Length"})
            if length < 0 or length > _MAX_BODY_BYTES:
                return self._respond(400, {"error": f"body too large (max {_MAX_BODY_BYTES} bytes)"})
            try:
                body = json.loads(self.rfile.read(length) or b"null") if length else None
            except json.JSONDecodeError:
                return self._respond(400, {"error": "invalid JSON body"})
        try:
            code, payload = dispatch(method, seg[2:], body, parts.query)
        except Exception:
            log.exception("config request failed", extra={"extra_fields": {"path": self.path}})
            return self._respond(500, {"error": "internal error"})
        self._respond(code, payload)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def log_message(self, *args) -> None:
        # method + path + query + status per request (the token travels in a header, never in the path), so
        # what a UI screen actually asked for, and what it got, can be read from the task log
        log.info("config request", extra={"extra_fields": {
            "client": self.address_string(), "request": self.requestline,
            "status": str(args[1]) if len(args) > 1 else ""}})


def boot(config_db, ledger_path) -> None:
    """The boot sequence, in this ORDER, before anything serves (pure of the HTTP server, so it is tested):
    restore config.db -> seed sources -> reconcile interrupted runs -> await the ledger (the sync server is
    its one restorer) -> artifact trees -> combinations baseline
    -> attribute vocab. A required restore that fails RAISES here, so the task exits non-zero and ECS
    retries: serving on a fresh store would let the periodic save overwrite the real snapshot."""
    from stone_pipeline.ledger import snapshot
    # E14: restore config.db (the durable source lifecycle: pause/delist/enabled) from its S3 snapshot
    # BEFORE seeding, so a redeploy does not lose it and re-seed every source back to active. Restore is a
    # no-op when a local config.db already exists; the seed then only fills a genuinely first-ever run.
    snapshot.restore_config(config_db, required=True)   # durable: fail loud if a present snapshot won't fetch
    # RECONCILE the source list from yaml on EVERY boot (INSERT OR IGNORE, minus removed sources), not only
    # when config.db is absent. A restored partial/stale snapshot must NOT leave configured sources missing
    # (that returned an inconsistent /config/v1/sources); seed re-adds any missing yaml source while keeping
    # each source's restored lifecycle state, and never resurrects a permanently-removed one.
    store.seed_from_yaml()
    # A produce the prior server was mid-run on cannot survive its restart (the in-memory run record and its
    # produce subprocess died with the old process). Any source still stamped `running` in the restored
    # snapshot is stale -- reconcile it to `interrupted` so it reads terminal and is runnable again, else a
    # redeploy (or crash) during a run pins that source `running` forever and the next Run is a no-op.
    if interrupted := store.reconcile_interrupted_runs(config_db):
        log.warning("reconciled interrupted run(s) from a prior restart",
                    extra={"extra_fields": {"sources": interrupted}})
    # C1: the ledger lives on the LOCAL volume both containers share; the sync server is its ONE restorer
    # (S3 snapshot, else bootstrap; each an atomic rename). Wait for it before a produce/reset could create
    # a fresh empty one, and never download a second copy over a ledger that may already be serving.
    snapshot.await_file(ledger_path)
    # Restore the last scrape's artifact trees (outputs_dir + data/) too: catalog/republish consume them
    # off the ephemeral disk, so without this a redeploy wipes the last scrape and they abort with "no
    # source runs". No-op when a local scrape already exists or none has been snapshotted yet.
    snapshot.restore_artifacts()
    # ...and the learned-alias write-back (state/alias_writeback.csv): matcher consistency accumulated over
    # prior runs, on the same ephemeral disk. No-op when a local one already exists or none was snapshotted.
    snapshot.restore_state()
    # E15: refresh the attribute vocab (attributes.csv) from S3 on boot so the review-decision endpoint
    # validates colours/types against Medusa's CURRENT set from container start. Otherwise every deploy
    # reverts the on-disk vocab to the committed baseline (which lags Medusa's newer colours), so a
    # just-added colour is rejected at mint until the next full produce. Best-effort: a fetch failure keeps
    # the committed vocab (a fresh/offline host still validates against a real seed), and a REPUBLISH does
    # not fetch inputs, so without this boot fetch a redeploy leaves the vocab stale.
    from stone_pipeline.io import medusa_inputs
    if medusa_inputs.fetch_attributes():
        log.info("boot: refreshed attribute vocab from S3 (live Medusa colours/types)")
    # The name-field kinds: a boot failure is logged, not fatal. The routes that clean a name read the map per
    # request and answer 503 until a later boot or produce fetch has written a valid one.
    try:
        medusa_inputs.fetch_field_kinds()
        log.info("boot: fetched field-kinds.json")
    except Exception:
        log.warning("boot: field-kinds.json not fetched; the name routes answer 503 until it is", exc_info=True)


def shutdown(config_db, periodic) -> None:
    """SIGTERM path (ECS stops a task with SIGTERM, which never runs atexit): STOP the periodic snapshot
    thread and wait for an in-flight save, THEN save config.db once, then exit 0. Ordered this way so the
    final save never races interpreter teardown (the 'cannot schedule new futures after interpreter
    shutdown' error every roll used to log). No atexit backstop: a second save at teardown was the race. A running run
    gets the SIGTERM first and its grace to clean up (runner.stop_current), inside the container's stopTimeout."""
    from stone_pipeline.config.control import runner
    from stone_pipeline.ledger import snapshot
    runner.stop_current()
    if hasattr(periodic, "stop"):
        periodic.stop()
    else:
        periodic.set()
    snapshot.save_config(config_db)
    raise SystemExit(0)


def serve(host: str | None = None, port: int = 8724) -> None:
    # default 127.0.0.1 (safe on a laptop); ECS sets BLOKPORT_BIND_HOST=0.0.0.0 so peer tasks
    # (Medusa, over the VPC) can reach it. The bearer token still gates every request.
    host = host or env.getenv("BLOKPORT_BIND_HOST", "127.0.0.1")
    # the token first: a server that cannot serve must not restore, seed or reconcile anything before it exits
    token = _expected_token()
    from stone_pipeline.ledger import snapshot, writethrough
    config_db = store.config_db_path()
    boot(config_db, writethrough.ledger_path())
    # keep config.db durable: a periodic snapshot backstop, stopped and flushed by the SIGTERM handler
    # (the lifecycle verbs and the decision routes also snapshot immediately, so a crash or a roll never loses one).
    periodic = snapshot.start_periodic(config_db, key=snapshot.config_key())
    signal.signal(signal.SIGTERM, lambda *_: shutdown(config_db, periodic))
    from stone_pipeline import lifecycle
    lifecycle.enable_config_snapshots()   # now a pause/delist or a decision snapshots immediately (server context only)
    httpd = ThreadingHTTPServer((host, port), ConfigHandler)
    httpd.expected_token = token   # type: ignore[attr-defined]
    log.info("config server listening", extra={"extra_fields": {
        "host": host, "port": port, "db": str(config_db)}})
    httpd.serve_forever()


def main(argv: list[str] | None = None) -> int:
    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
