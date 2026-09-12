"""HTTP surface for the scraper config store, for the admin UI (e.g. on :4200).

A thin, dependency-free reference server over the config store, so the UI can list
scrapers, toggle which run, and edit per-scraper settings live:

    GET  /config/v1/sources                list every scraper (enabled + settings)
    GET  /config/v1/sources/<name>         one scraper
    PUT  /config/v1/sources/<name>         create or update one (body = the settings)

Auth is a bearer token (BLOKPORT_CONFIG_TOKEN); the server refuses to start without
it. Routing is split from transport (`dispatch` is pure) so it is testable without
sockets. SQLite WAL handles the UI (single writer) plus the pipeline (readers). No
em dashes (design principle 2).
"""

from __future__ import annotations

import hmac
import json
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


def _origin_as_card(item: dict) -> dict:
    """An origin confirmation in the variety-card shape: the product bound fine, only its origin could not be
    corroborated, so the card says so and prefills the vendor's declared country. Every field stays open to
    the operator's statement, like any other card."""
    return {"kind": "origin", "ref": item.get("ref", ""), "variant": item.get("variety", ""),
            "stone_type": item.get("stone_type", ""), "color": "",
            "reason": (f"Origin not corroborated: the vendor declares {item.get('vendor_origin') or '?'}, the "
                       f"stone documents {item.get('map_country') or 'no country'}. Confirm the origin, or "
                       f"state what this product is."),
            "origin": item.get("current_country") or item.get("vendor_origin") or "",
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


def _country_iso(raw: str) -> str | None:
    """Resolve a country NAME or ISO2 to a canonical ISO-3166 alpha-2, or None if it is not a real country.
    Name-first (so 'UK' -> GB) then a bare valid ISO2 -- mirrors derive._to_iso. Validates the operator's
    mint origin so a garbage country is never stored. Loaded per call (admin low-QPS)."""
    from stone_pipeline.core.text import match_key
    from stone_pipeline.reference.loaders import load_country_codes
    v = (raw or "").strip()
    if not v:
        return None
    codes = load_country_codes()
    if hit := codes.get(match_key(v)):
        return hit
    if len(v) == 2 and v.isalpha() and v.upper() in set(codes.values()):
        return v.upper()
    return None


def _attach_image_progress(rows: list[dict]) -> None:
    """Enrich each source diagnostic with a LIVE `images` block, computed at serve time from the S3 markers
    (deliberately NOT the produce-time stages.json snapshot: de-watermark + upscale run async for tens of
    minutes after the produce, so the UI's 30s poll must see numbers that actually move). Only meaningful
    when images are hosted on S3; a source with no scraped images gets no block (the UI renders "-").

    Best-effort and isolated: a single S3 client is reused across sources, and any listing failure omits the
    images block rather than failing the whole diagnostics response -- the endpoint's primary job is pipeline
    health, and the UI already treats a missing block as "-"."""
    from stone_pipeline.config.settings import S3_REGION, SETTINGS
    if SETTINGS.images.mode != "s3":
        return
    try:
        import boto3
        client = boto3.client("s3", region_name=S3_REGION)
    except Exception:
        log.warning("live image-progress: S3 client unavailable; omitting images blocks", exc_info=True)
        return
    from deploy.enhance_trigger import image_progress
    from stone_pipeline.config import diagnostics
    for row in rows:                          # isolate per source: one source's S3 hiccup never drops the rest
        source = row.get("source")
        if not source:
            continue
        try:
            block = image_progress(client, source)
        except Exception:
            log.warning("live image-progress failed for %s; omitting its images block",
                        source, exc_info=True)
            continue
        if block is not None:
            # held_for_image: products the LAST produce held for no_image (their texture was not ready at emit).
            # Paired with this LIVE block (ready/total/generating) so the admin can render the reconciliation
            # the frozen per-run 'Produced' count cannot give on its own: "N held for image, M of T textures
            # ready -> Republish". Per-run (drops to 0 on the next republish as they emit); read from the same
            # row's images-stage diagnostic, so no extra I/O.
            block["held_for_image"] = diagnostics.held_for_image(row)
            row["images"] = block


# --- the route table -----------------------------------------------------------------------------------
# One handler per (method, path pattern). A pattern is a tuple of path segments under /config/v1; a segment
# written "{name}" captures that position into the handler's `params`. Matching is first hit per method, so
# a literal pattern must precede a capturing one of the same length. A known path with another method is
# 405 (naming the allowed methods); an unknown path is 404. Every handler is pure: (params, body, query) ->
# (status, json body). Adding a route = one handler + one table row, never a new branch in a dispatcher.


def _dict(body) -> dict:
    return body if isinstance(body, dict) else {}


def _sources_of(body):
    return body.get("sources") if isinstance(body, dict) else None


# -- run: the produce trigger ------------------------------------------------------------------------------
def _post_run(params, body, query):
    # Body (all optional): {"sources": ["zucchi", ...], "stage": "scrape"|"catalog"|"all"}
    # sources omitted -> every enabled source; stage omitted -> "all" (scrape -> catalog -> gate).
    from stone_pipeline.config import runner
    rec, code = runner.start_run(_sources_of(body), _dict(body).get("stage") or "all")   # (record, status)
    return code, rec


def _get_run(params, body, query):
    from stone_pipeline.config import runner
    return 200, runner.current()


def _get_run_by_id(params, body, query):
    from stone_pipeline.config import runner
    rec = runner.get_run(params["run_id"])
    return (200, rec) if rec else (404, {"error": f"no run {params['run_id']!r}"})


# -- diagnostics: per-source pipeline diagnostics for the admin UI (read-only) -----------------------------
def _get_diagnostics(params, body, query):
    from stone_pipeline.config import diagnostics
    rows = diagnostics.read_all()
    _attach_image_progress(rows)
    return 200, {"diagnostics": rows}


def _get_diagnostics_source(params, body, query):
    from stone_pipeline.config import diagnostics
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
    #     AND the only image wipe in the system (hosted product images + enhanced markers + manifest).
    #   {"pristine": true, "keep_images": true}      same, but KEEP the hosted product images + enhanced
    #     markers (a re-scrape reuses them, no GPU/FAL rebuild) -- the test-reset path.
    #   {"pristine": true, "keep_scrape": true}      same, but KEEP the cached scrape (data/ + outputs/)
    #     so the catalog is rebuilt with a Republish All instead of a re-scrape.
    # 409 if a run/serve is active (never reset mid-run); base variant config is never deleted. A GLOBAL
    # reset also clears the config.db review queue + operator-pasted attribute ids (response.config),
    # so the clean start is coherent across both stores; a scoped reset leaves config.db alone.
    from stone_pipeline import lifecycle
    b = _dict(body)
    result, code = lifecycle.reset(_sources_of(body), bool(b.get("hard")), pristine=bool(b.get("pristine")),
                                   keep_images=bool(b.get("keep_images")), keep_scrape=bool(b.get("keep_scrape")))
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
    # (lifecycle=paused, disabled) but leaves products live & buyable at their last values.
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
    force = bool(b.get("force"))
    if b.get("all_minted"):
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
        force = bool(_dict(body).get("force"))
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
    variant = unquote(params["variant"])
    if body.get("action", "") != "reject":
        return 400, {"error": "action must be 'reject'; anything else is a statement: PUT /review/decide"}
    try:
        decisions_store.set_variety_decision(variant, "reject")
    except decisions_store.InvalidDecision as e:
        return 400, {"error": str(e)}
    return 200, {"variant": variant, "action": "reject"}


def _get_review_attributes(params, body, query):
    from stone_pipeline.config import decisions_store
    return 200, {"attributes": decisions_store.list_pending("attribute")}


def _put_review_attribute(params, body, query):
    # {"kind":..., "medusa_id":...} for a pending attribute value (needs a Medusa id)
    from stone_pipeline.config import decisions_store
    if not isinstance(body, dict):
        return 400, {"error": "body must be a JSON object {kind, medusa_id}"}
    value = unquote(params["value"])   # same decode-before-key rule as the variety PUT
    try:
        decisions_store.set_attribute_id(body.get("kind", ""), value, body.get("medusa_id", ""))
    except decisions_store.InvalidDecision as e:
        return 400, {"error": str(e)}
    return 200, {"value": value, "kind": body.get("kind"), "medusa_id": body.get("medusa_id")}


def _decide_listings(body) -> list[tuple[str, str]]:
    # the listings a statement applies to: the card's `listings` ([{source, scraped}], a card shared by
    # several vendors), or one `source` with `scraped` as a spelling or a list of spellings
    source = (body.get("source") or "").strip()
    raw_scraped = body.get("scraped") or ""
    listings = [(str(l.get("source") or "").strip(), str(l.get("scraped") or "").strip())
                for l in (body.get("listings") or []) if isinstance(l, dict)]
    listings += [(source, s.strip()) for s in (raw_scraped if isinstance(raw_scraped, list) else [raw_scraped])
                 if isinstance(s, str) and s.strip() and source]
    return list(dict.fromkeys(l for l in listings if l[0] and l[1]))


def _delete_review_decide(params, body, query):
    # DELETE {source, scraped}: clear that vendor's decisions for the spelling
    from stone_pipeline.config import decisions_store
    if not isinstance(body, dict):
        return 400, {"error": "body must be a JSON object {source, scraped, name, type, ...}"}
    listings = _decide_listings(body)
    if not listings:
        return 400, {"error": "listings [{source, scraped}] or source + scraped are required"}
    return 200, {"listings": [{"source": s, "scraped": sp} for s, sp in listings],
                 "cleared": [decisions_store.clear_decisions(s, sp) for s, sp in listings]}


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
    name = (body.get("name") or "").strip()
    raw_type = (body.get("type") or "").strip()
    if not name or not raw_type:
        return 400, {"error": "name and type are required"}
    vocab = _type_vocab()
    if proj.norm(raw_type) not in vocab:
        return 400, {"error": f"type {raw_type!r} is not a known Medusa stone-type attribute"}
    stone_type = vocab[proj.norm(raw_type)]
    # a colour, when given, must be a real Medusa colour attribute (a minted variety is seeded with it; a
    # value Medusa lacks would null-id every product), stored in its canonical casing
    color = ""
    if (raw_color := (body.get("color") or "").strip()):
        colors = _color_vocab()
        if proj.norm(raw_color) not in colors:
            return 400, {"error": f"color {raw_color!r} is not a known Medusa colour attribute"}
        color = colors[proj.norm(raw_color)]
    raw_origin = (body.get("origin") or "").strip()
    origin = _country_iso(raw_origin) if raw_origin else ""
    if raw_origin and not origin:
        return 400, {"error": f"origin {raw_origin!r} is not a real ISO-3166 country"}
    from stone_pipeline.stages.curate import active_branches, gen_key
    from stone_pipeline.stages import decisions as _decisions
    retired = _decisions.load_retired()
    if any(gen_key(b, stone_type, name) in retired for b in active_branches()):
        return 409, {"error": f"'{name}' ({stone_type}) is a retired variety; un-retire it first",
                     "retired": True}
    try:
        outcomes = [decisions_store.decide(s, sp, name, stone_type, color, origin, bool(body.get("widen")))
                    for s, sp in listings]
    except decisions_store.InvalidDecision as e:
        return 400, {"error": str(e)}
    return 200, {**outcomes[0], "listings": [{"source": s, "scraped": sp, "result": o["result"]}
                                             for (s, sp), o in zip(listings, outcomes)],
                 "decided": len(outcomes)}


def _get_review_resolved(params, body, query):
    # GET /review/resolved[?source=&decided=true|false]: the last produce's resolution per product, with
    # the standing decision overlaid (no approval needed)
    from stone_pipeline.config import resolved
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
    return 200, {"approved": decisions_store.approve_leaf_pending(_dict(body).get("verdict"))}


def _put_review_backbone_leaf(params, body, query):
    # {"action":"approve"|"reject"|"clear"}: one verdict (approve/reject also un-approve a DECIDED leaf;
    # clear undoes it)
    from stone_pipeline.config import decisions_store
    if not isinstance(body, dict):
        return 400, {"error": "body must be a JSON object {action}"}
    ref, action = unquote(params["ref"]), body.get("action", "")
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
    variety = (body.get("variety") or "").strip()
    stone_type = (body.get("stone_type") or "").strip()
    raw = body.get("countries") or []
    if not (variety and stone_type and raw):
        return 400, {"error": "variety, stone_type and a non-empty countries list are required"}
    isos = []
    for c in raw:
        if not (iso := _country_iso((c or "").strip())):
            return 400, {"error": f"country {c!r} is not a real ISO-3166 country"}
        isos.append(iso)
    try:
        decisions_store.set_variety_origin(variety, stone_type, ",".join(isos),
                                           (body.get("city") or "").strip(), (body.get("county") or "").strip())
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
    # the colour vocabulary for the mint colour picker (a colourless variety seeded with a real colour)
    return 200, {"colors": sorted(_color_vocab().values())}


def _get_types(params, body, query):
    # the stone-type vocabulary for the mint type picker (a type-less variety is held until it has one)
    return 200, {"types": sorted(_type_vocab().values())}


# -- sources: the scraper control plane --------------------------------------------------------------------
def _get_sources(params, body, query):
    # enrich each source with what's IN the scraper: the raw scrape (scrape_at + scrape_rows) and how many
    # products reached the ledger (ledger_products) -- so :4200 shows what's there and how old.
    from stone_pipeline.config import scrape_status
    return 200, {"sources": scrape_status.enrich(store.list_rows())}


def _get_source(params, body, query):
    row = store.get_row(params["name"])
    return (200, row) if row else (404, {"error": f"no source {params['name']!r}"})


def _put_source(params, body, query):
    if not isinstance(body, dict):
        return 400, {"error": "body must be a JSON object of source settings"}
    name = params["name"]
    body = {**body, "source": name}   # the path name is authoritative
    err = _validate_source_put(name, body)
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


_ROUTES: tuple[tuple[str, tuple[str, ...], object], ...] = (
    ("POST", ("run",), _post_run),
    ("GET", ("run",), _get_run),
    ("GET", ("run", "{run_id}"), _get_run_by_id),
    ("GET", ("diagnostics",), _get_diagnostics),
    ("GET", ("diagnostics", "{source}"), _get_diagnostics_source),
    ("POST", ("reset",), _post_reset),
    ("POST", ("curation", "rebuild"), _post_curation_rebuild),
    ("POST", ("purge",), _post_purge),
    ("POST", ("clean",), _post_clean),
    ("POST", ("delist",), _post_delist),
    ("POST", ("pause",), _post_pause),
    ("POST", ("resume",), _post_resume),
    ("GET", ("variations", "minted"), _get_variations_minted),
    ("POST", ("variations", "unmint"), _post_variations_unmint),
    ("POST", ("variations", "{key}", "{verb}"), _post_variation_verb),
    ("GET", ("review", "variants"), _get_review_variants),
    ("PUT", ("review", "variants", "{variant}"), _put_review_variant),
    ("GET", ("review", "attributes"), _get_review_attributes),
    ("PUT", ("review", "attributes", "{value}"), _put_review_attribute),
    ("PUT", ("review", "decide"), _put_review_decide),
    ("DELETE", ("review", "decide"), _delete_review_decide),
    ("GET", ("review", "resolved"), _get_review_resolved),
    ("GET", ("review", "backbone"), _get_review_backbone),
    ("GET", ("review", "backbone", "decided"), _get_review_backbone_decided),
    ("POST", ("review", "backbone", "approve_all"), _post_review_backbone_approve_all),
    ("PUT", ("review", "backbone", "{ref}"), _put_review_backbone_leaf),
    ("GET", ("origins",), _get_origins),
    ("GET", ("origins", "lookup"), _get_origins_lookup),
    ("PUT", ("origins", "{ref}"), _put_origin),
    ("DELETE", ("origins", "{ref}"), _delete_origin),
    ("GET", ("varieties",), _get_varieties),
    ("GET", ("adapters",), _get_adapters),
    ("GET", ("colors",), _get_colors),
    ("GET", ("types",), _get_types),
    ("GET", ("sources",), _get_sources),
    ("GET", ("sources", "{name}"), _get_source),
    ("PUT", ("sources", "{name}"), _put_source),
    ("DELETE", ("sources", "{name}"), _delete_source),
)

_NOT_FOUND = ("not found; expected /config/v1/sources[/<name>], /run, /reset, /purge, /delist, /pause, "
              "/resume, /clean, /review/<kind>, /varieties, /adapters, /colors, /types or "
              "/variations/<key>/{retire,un_retire}")


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
            return handler(params, body, query)
        allowed.append(route_method)
    if allowed:
        return 405, {"error": f"{method} is not allowed on /config/v1/{'/'.join(segments)}; "
                              f"use {', '.join(dict.fromkeys(allowed))}"}
    return 404, {"error": _NOT_FOUND}


_SOURCE_INT_FIELDS = ("default_bundle_size", "min_expected_rows")


def _validate_source_put(name: str, body: dict) -> tuple[int, dict] | None:
    """Guard the admin 'add/edit source' PUT so it can only create a RUNNABLE, non-colliding source.
    A source with no coded adapter can neither run nor be scoped (ISS-3), so it must never become a
    live-looking config row; a duplicate source_code collides SKU provenance for clear/remove (ISS-1).
    Returns an (status, error) tuple to short-circuit, or None when the source is valid."""
    from stone_pipeline import adapters
    if name not in adapters.REGISTRY:
        return 400, {"error": f"no coded adapter for source {name!r}; a runnable source needs a coded "
                     f"adapter (fetcher + adapter map). Known adapters: {sorted(adapters.REGISTRY)}"}
    code = (body.get("source_code") or "").strip()
    if code:
        for row in store.list_rows():
            if row["source"] != name and (row["source_code"] or "").strip().casefold() == code.casefold():
                return 400, {"error": f"source_code {code!r} already used by source {row['source']!r}; "
                             "codes must be unique (they scope SKUs for removal)"}
    # Ports are a HARD requirement, set by the admin here (never derived from the seed): Medusa builds a
    # product's shipping ports from the supplier's ports, so a source that ships without them makes Medusa
    # fall back to deriving a whole-country port list. A source must therefore carry at least one port.
    ports = body.get("ports")
    if not (isinstance(ports, list) and any(str(p).strip() for p in ports)):
        return 400, {"error": f"source {name!r} requires at least one shipping port: set 'ports' to a "
                     "non-empty list of the supplier's port names / LOCODEs (Medusa derives a product's "
                     "ports from these)."}
    # the integer fields are validated HERE, so a malformed value is the client's 400 (it used to reach
    # int() in the store and answer 500)
    for field in _SOURCE_INT_FIELDS:
        raw = body.get(field)
        if raw is None:
            continue
        try:
            int(str(raw).strip())
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
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

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
        if method in ("PUT", "POST"):
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
    restore config.db -> seed sources -> reconcile interrupted runs -> restore the ledger -> the two-level
    backfill (needs the ledger) -> artifact trees -> combinations baseline -> attribute vocab. A required
    restore that fails RAISES here, so the task exits non-zero and ECS retries: serving on a fresh store
    would let the periodic save overwrite the real snapshot."""
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
    # C1: restore the LOCAL-disk ledger from its S3 snapshot before any produce/reset could create a
    # fresh empty one over it. Idempotent + shared-volume-safe (skips if the sync server already did it).
    snapshot.restore(ledger_path, required=True)   # durable: fail loud if present-but-unfetchable
    # TWO LEVELS data backfill, once: needs the ledger (variety lookups), so it runs here and not in the store
    # migration, which fires on the first config.db open above, before the ledger is back.
    from stone_pipeline.config import decisions_store
    if (moved := decisions_store.backfill_levels()) is not None:
        log.warning("boot: two-level backfill applied", extra={"extra_fields": {"moved": moved}})
    # Restore the last scrape's artifact trees (outputs_dir + data/) too: catalog/republish consume them
    # off the ephemeral disk, so without this a redeploy wipes the last scrape and they abort with "no
    # source runs". No-op when a local scrape already exists or none has been snapshotted yet.
    snapshot.restore_artifacts()
    # ...and the last published big-list combinations file, which tree_build diffs against to emit only the
    # NEW combinations ("build on it"). Without it a cold task re-emits the whole ~2M set as the delta.
    snapshot.restore_combinations_baseline()
    # E15: refresh the attribute vocab (attributes.csv) from S3 on boot so the review-decision endpoint
    # validates colours/types against Medusa's CURRENT set from container start. Otherwise every deploy
    # reverts the on-disk vocab to the committed baseline (which lags Medusa's newer colours), so a
    # just-added colour is rejected at mint until the next full produce. Best-effort: a fetch failure keeps
    # the committed vocab (a fresh/offline host still validates against a real seed), and a REPUBLISH does
    # not fetch inputs, so without this boot fetch a redeploy leaves the vocab stale.
    from deploy import fetch_inputs
    if fetch_inputs.fetch_attributes():
        log.info("boot: refreshed attribute vocab from S3 (live Medusa colours/types)")


def shutdown(config_db, periodic) -> None:
    """SIGTERM path (ECS stops a task with SIGTERM, which never runs atexit): STOP the periodic snapshot
    thread and wait for an in-flight save, THEN save config.db once, then exit 0. Ordered this way so the
    final save never races interpreter teardown (the 'cannot schedule new futures after interpreter
    shutdown' error every roll used to log). No atexit backstop: a second save at teardown was the race."""
    from stone_pipeline.ledger import snapshot
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
    from stone_pipeline.ledger import snapshot, writethrough
    config_db = store.config_db_path()
    boot(config_db, writethrough.ledger_path())
    # keep config.db durable: a periodic snapshot backstop, stopped and flushed by the SIGTERM handler
    # (the lifecycle verbs also snapshot immediately after a pause/delist so a crash never loses one).
    periodic = snapshot.start_periodic(config_db, key=snapshot.config_key())
    signal.signal(signal.SIGTERM, lambda *_: shutdown(config_db, periodic))
    from stone_pipeline import lifecycle
    lifecycle.enable_config_snapshots()   # now a pause/delist also snapshots immediately (server context only)
    httpd = ThreadingHTTPServer((host, port), ConfigHandler)
    httpd.expected_token = _expected_token()   # type: ignore[attr-defined]
    log.info("config server listening", extra={"extra_fields": {
        "host": host, "port": port, "db": str(config_db)}})
    httpd.serve_forever()


def main(argv: list[str] | None = None) -> int:
    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
