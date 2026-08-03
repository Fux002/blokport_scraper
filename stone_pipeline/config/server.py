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

import atexit
import hmac
import json
import os
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


def dispatch(method: str, segments: list[str], body, query: str = "") -> tuple[int, object]:
    """Route one request. `segments` is the path under /config/v1 (e.g. ['sources']
    or ['sources', 'polonine']); `query` is the raw URL query string (e.g. 'q=carr&limit=20').
    Pure: returns (status_code, json body)."""
    if segments and segments[0] == "run":
        # the 'produce' trigger: kick a scrape/catalog into the ledger. Body (all optional):
        #   {"sources": ["zucchi", ...], "stage": "scrape"|"catalog"|"all"}
        # sources omitted -> every enabled source; stage omitted -> "all" (scrape -> catalog -> gate).
        from stone_pipeline.config import runner
        if len(segments) == 1:
            if method == "POST":
                srcs = body.get("sources") if isinstance(body, dict) else None
                stage = body.get("stage") if isinstance(body, dict) else None
                rec, code = runner.start_run(srcs, stage or "all")   # (record, status)
                return code, rec                                     # dispatch returns (status, body)
            if method == "GET":
                return 200, runner.current()
            return 405, {"error": "POST /config/v1/run to trigger, GET for the current run"}
        if len(segments) == 2 and method == "GET":     # /run/<run_id>
            rec = runner.get_run(segments[1])
            return (200, rec) if rec else (404, {"error": f"no run {segments[1]!r}"})
        return 404, {"error": "expected /config/v1/run or /config/v1/run/<run_id>"}
    if segments and segments[0] == "diagnostics":
        # per-source pipeline diagnostics for the admin UI (read-only): which layer degraded + what
        # drifted in the source, so a silent format change is visible.
        #   GET /config/v1/diagnostics            -> [{source, run_id, health, gates, stages, drift, ...}, ...]
        #   GET /config/v1/diagnostics/<source>   -> that source's latest summary (404 if it never produced)
        from stone_pipeline.config import diagnostics
        if method != "GET":
            return 405, {"error": "GET /config/v1/diagnostics[/<source>]"}
        if len(segments) == 1:
            return 200, {"diagnostics": diagnostics.read_all()}
        if len(segments) == 2:
            summary = diagnostics.read_source(segments[1])
            return (200, summary) if summary else (404, {"error": f"no diagnostics for {segments[1]!r}"})
        return 404, {"error": "expected /config/v1/diagnostics or /config/v1/diagnostics/<source>"}
    if segments and segments[0] == "reset":
        # clean-start the ledger sync state (the coordinated ①②③ reset, our ① half). Body (optional):
        #   {"hard": true, "sources": ["zucchi", ...]}   hard also drops scraped products; sources scopes.
        #   {"pristine": true}                           factory cold start: global hard reset + wipe the
        #     durable operator overlay (variety/leaf/retired decisions) so the next produce is seed-only.
        # 409 if a run/serve is active (never reset mid-run); base variant config is never deleted. A GLOBAL
        # reset also clears the config.db review queue + operator-pasted attribute ids (response.config),
        # so the clean start is coherent across both stores; a scoped reset leaves config.db alone.
        from stone_pipeline import lifecycle
        if method == "POST":
            srcs = body.get("sources") if isinstance(body, dict) else None
            hard = bool(body.get("hard")) if isinstance(body, dict) else False
            pristine = bool(body.get("pristine")) if isinstance(body, dict) else False
            result, code = lifecycle.reset(srcs, hard, pristine=pristine)
            return code, result
        return 405, {"error": "POST /config/v1/reset to reset the ledger"}
    if segments and segments[0] == "curation":
        # global incremental curation rebuild (curation state 1 repair): reseed the base FILE from the
        # committed pristine seed + kick a catalog re-derive, WITHOUT a factory reset's ledger/image wipe or
        # re-scrape. Curation is global (a variety belongs to the canonical catalog, not a source), so this
        # is not per-source. 409 if a produce/reset/pull is in flight; returns a summary (base_reseed +
        # rederive run to watch). Blokport also gates the button on 'no pull running'.
        from stone_pipeline import lifecycle
        if len(segments) == 2 and segments[1] == "rebuild" and method == "POST":
            result, code = lifecycle.rebuild_curation()
            return code, result
        return 405, {"error": "POST /config/v1/curation/rebuild to reseed the base + re-derive the catalog"}
    if segments and segments[0] == "purge":
        # dead-stock purge: hard-delete the qty-0 (delisted) products. Returns external_ids so Medusa
        # deletes the same set (product + scraper_sync_ref). Guarded; base variations untouched.
        from stone_pipeline import lifecycle
        if method == "POST":
            srcs = body.get("sources") if isinstance(body, dict) else None
            result, code = lifecycle.purge(srcs)
            return code, result
        return 405, {"error": "POST /config/v1/purge to hard-delete qty-0 products"}
    if segments and segments[0] == "clean":
        # housekeeping for the admin: POST {} prunes superseded scrapes/runs; POST {"sources":[...]}
        # DELETES those sources' raw scraped data (fresh re-scrape). Base config + ledger untouched.
        from stone_pipeline import lifecycle
        if method == "POST":
            srcs = body.get("sources") if isinstance(body, dict) else None
            result, code = lifecycle.clean(srcs)
            return code, result
        return 405, {"error": "POST /config/v1/clean to prune (or delete a source's scraped data)"}
    if segments and segments[0] == "delist":
        # stage 1 of a vendor removal ('Take offline'): {"sources": ["zucchi", ...]} sets those sources'
        # products to qty 0 (Medusa pulls them out of sale) AND disables them so a scrape never re-stocks
        # an offline vendor. Reversible: re-enable + re-scrape. Guarded (409 if a run/pull is active).
        from stone_pipeline import lifecycle
        if method == "POST":
            srcs = body.get("sources") if isinstance(body, dict) else None
            result, code = lifecycle.delist(srcs)
            return code, result
        return 405, {"error": "POST /config/v1/delist to take sources offline (qty 0 + disable)"}
    if segments and segments[0] in ("pause", "resume"):
        # freeze / unfreeze a source WITHOUT touching its stock: {"sources": ["zucchi", ...]}. Pause stops
        # scraping (lifecycle=paused, disabled) but leaves products live & buyable at their last values;
        # resume re-enables (lifecycle=active) so the next run scrapes again. Config-only, no ledger work.
        from stone_pipeline import lifecycle
        if method == "POST":
            srcs = body.get("sources") if isinstance(body, dict) else None
            verb = lifecycle.pause if segments[0] == "pause" else lifecycle.resume
            result, code = verb(srcs)
            return code, result
        return 405, {"error": f"POST /config/v1/{segments[0]} with a sources list"}
    if segments and segments[0] == "variations":
        # variation lifecycle (the variety half): explicit removal + undo.
        #   POST /config/v1/variations/<key>/retire     {"force": true?}   remove a variety (E11 re-key old side)
        #   POST /config/v1/variations/<key>/un_retire                     reverse it (mirrors source resume)
        #   POST /config/v1/variations/<key>/not_a_duplicate               cancel a false-positive dup tombstone
        #     (curation state 2): keep the variety + record a durable 'protected' verdict so a future
        #     seed-reconcile never re-drops it. Idempotent + scoped result.
        from stone_pipeline import lifecycle
        if len(segments) == 3 and method == "POST" and segments[2] in ("retire", "un_retire", "not_a_duplicate"):
            key = segments[1]
            if segments[2] == "retire":
                force = bool(body.get("force")) if isinstance(body, dict) else False
                result, code = lifecycle.retire_variation(key, force=force)
            elif segments[2] == "un_retire":
                result, code = lifecycle.un_retire(key)
            else:
                result, code = lifecycle.not_a_duplicate(key)
            return code, result
        return 404, {"error": "expected POST /config/v1/variations/<key>/{retire,un_retire,not_a_duplicate}"}
    if segments and segments[0] == "review":
        # the new-variant review queue for the :4200 admin. The produce SURFACES uncertain items; the
        # operator decides here; the NEXT produce APPLIES it (decisions are read once at curate start, so
        # an edit takes effect on the following run -- same "applies next run" contract as the run guard).
        #   GET /config/v1/review/variants               pending varieties (+ current_action)
        #   PUT /config/v1/review/variants/<variant>     {"action":"mint"|"reject"|"alias","alias_of"?,"type"?}
        #     for alias, "type" is the TARGET's stone type (which of a multi-type name to alias into)
        #   GET /config/v1/review/attributes             pending attribute values (need a Medusa id)
        #   PUT /config/v1/review/attributes/<value>     {"kind":..., "medusa_id":...}
        #   GET  /config/v1/review/backbone              pending leaf additions (value not yet on a variety)
        #   GET  /config/v1/review/backbone/decided      already-decided leaves (+ ref), to revise a verdict
        #   POST /config/v1/review/backbone/approve_all  {"verdict"?: "likely_real"}  bulk-approve
        #   PUT  /config/v1/review/backbone/<ref>        {"action":"approve"|"reject"|"clear"}  one verdict
        #                                                (approve/reject also un-approve a DECIDED leaf; clear undoes it)
        from stone_pipeline.config import decisions_store, varieties
        if len(segments) == 2 and segments[1] == "variants" and method == "GET":
            return 200, {"variants": decisions_store.list_pending("variety")}
        if len(segments) == 3 and segments[1] == "variants" and method == "PUT":
            if not isinstance(body, dict):
                return 400, {"error": "body must be a JSON object {action, alias_of?}"}
            # DECODE the path segment before it becomes the decision key. The variety was SURFACED with
            # review_pending.ref = norm(real name) (e.g. 'alpine luxe'); a multi-word name arrives here
            # percent-encoded ('Alpine%20Luxe'), and norm keeps the literal '%20' ('alpine 20luxe'), so a
            # raw segment would store the decision under a key that never matches the pending ref and the
            # UI reads back current_action=null. unquote first, so ref == variant_norm holds by construction
            # (mirrors the backbone PUT).
            variant = unquote(segments[2])
            action = body.get("action", "")
            alias_of = body.get("alias_of")
            # domain guard (server's job, not the store's): an alias MUST target a real variety, else it
            # would resolve to nothing at produce time -- a silent no-op. Reject it loudly here instead.
            if action == "alias" and not varieties.exists(alias_of or ""):
                return 400, {"error": f"alias_of {alias_of!r} is not an existing variety"}
            # an optional mint colour / stone type must be a real Medusa attribute, else the variety
            # would be seeded with a value that null-ids every product -- same loud guard as the alias
            # target. A type-less variety is HELD until the operator assigns a type here.
            from stone_pipeline.matching import projections as proj
            seed_color = None
            if (raw_color := (body.get("color") or "").strip()):
                vocab = _color_vocab()
                if proj.norm(raw_color) not in vocab:
                    return 400, {"error": f"color {raw_color!r} is not a known Medusa colour attribute"}
                seed_color = vocab[proj.norm(raw_color)]   # store the canonical casing
            seed_type = None
            if (raw_type := (body.get("type") or "").strip()):
                vocab = _type_vocab()
                if proj.norm(raw_type) not in vocab:
                    return 400, {"error": f"type {raw_type!r} is not a known Medusa stone-type attribute"}
                seed_type = vocab[proj.norm(raw_type)]
            # the mint ORIGIN: a real ISO country (name or code). Required at mint is enforced in the
            # :4200 UI so nothing breaks here before it ships the field; when a country IS sent it must be
            # a real one (never store garbage). A minted country overlays origin_map as a confirmed rule.
            seed_country = None
            if (raw_country := (body.get("country") or "").strip()):
                if not (seed_country := _country_iso(raw_country)):
                    return 400, {"error": f"country {raw_country!r} is not a real ISO-3166 country"}
            try:
                decisions_store.set_variety_decision(variant, action, alias_of, seed_color=seed_color,
                                                     seed_type=seed_type, seed_country=seed_country)
            except decisions_store.InvalidDecision as e:
                return 400, {"error": str(e)}
            return 200, {"variant": variant, "action": action, "alias_of": alias_of,
                         "seed_color": seed_color, "seed_type": seed_type, "seed_country": seed_country}
        if len(segments) == 2 and segments[1] == "attributes" and method == "GET":
            return 200, {"attributes": decisions_store.list_pending("attribute")}
        if len(segments) == 3 and segments[1] == "attributes" and method == "PUT":
            if not isinstance(body, dict):
                return 400, {"error": "body must be a JSON object {kind, medusa_id}"}
            value = unquote(segments[2])   # same decode-before-key rule as the variety PUT (see above)
            try:
                decisions_store.set_attribute_id(body.get("kind", ""), value, body.get("medusa_id", ""))
            except decisions_store.InvalidDecision as e:
                return 400, {"error": str(e)}
            return 200, {"value": value, "kind": body.get("kind"), "medusa_id": body.get("medusa_id")}
        if len(segments) == 2 and segments[1] == "backbone" and method == "GET":
            return 200, {"backbone": decisions_store.list_pending("backbone_leaf")}
        if len(segments) == 3 and segments[1] == "backbone" and segments[2] == "decided" and method == "GET":
            # already-decided leaves (with their refs), so a past verdict can be revised -- e.g. un-approve
            # a spurious approval that a prior run grew into the overlay.
            return 200, {"decided": decisions_store.list_decided_leaves()}
        if len(segments) == 3 and segments[1] == "backbone" and segments[2] == "approve_all" and method == "POST":
            # the primary UX: approve every pending leaf suggestion (or only one verdict, e.g. likely_real).
            verdict = body.get("verdict") if isinstance(body, dict) else None
            return 200, {"approved": decisions_store.approve_leaf_pending(verdict)}
        if len(segments) == 3 and segments[1] == "backbone" and method == "PUT":
            if not isinstance(body, dict):
                return 400, {"error": "body must be a JSON object {action}"}
            ref, action = unquote(segments[2]), body.get("action", "")
            try:
                # a still-pending suggestion decides through the queue; a leaf a prior run already decided
                # (e.g. a spurious approval to undo) has dropped off that queue, so revise it in place by
                # ref. 'clear' only applies to a decided leaf, so it skips the pending path.
                ok = (action != "clear" and decisions_store.decide_leaf_pending(ref, action)) \
                    or decisions_store.revise_leaf_decision(ref, action)
            except decisions_store.InvalidDecision as e:
                return 400, {"error": str(e)}
            if not ok:
                return 404, {"error": f"no pending or decided backbone-leaf for ref {ref!r}"}
            return 200, {"ref": ref, "action": action}
        return 404, {"error": "expected GET/PUT/POST /config/v1/review/{variants,attributes,backbone}[/<ref>]"}
    if segments and segments[0] == "varieties":
        # existing variety names (+ stone_type) for the review alias-to dropdown, from the durable ledger.
        # Optional ?q=<substring> + ?limit=<n> back a type-ahead: the full set is ~25k, so a client can
        # narrow it instead of pulling ~1 MB on every open.
        if method == "GET":
            from stone_pipeline.config import varieties
            params = parse_qs(query)
            q = (params.get("q", [""])[0] or "").strip() or None
            raw_limit = (params.get("limit", [""])[0] or "").strip()
            limit = int(raw_limit) if raw_limit.isdigit() else None
            return 200, {"varieties": varieties.list_all(q=q, limit=limit)}
        return 405, {"error": "GET /config/v1/varieties[?q=&limit=]"}
    if segments and segments[0] == "adapters":
        # the coded adapters available to run (the auto-discovered REGISTRY). The :4200 admin turns the
        # source "adapter" field into a validated dropdown from this, so it can proactively block adding a
        # source with no coded adapter (ISS-3) instead of relying on the PUT 400. Agnostic: just the list.
        if method == "GET":
            from stone_pipeline import adapters
            return 200, {"adapters": sorted(adapters.REGISTRY)}
        return 405, {"error": "GET /config/v1/adapters"}
    if segments and segments[0] == "colors":
        # the colour vocabulary for the new-variety mint colour picker: seed a colourless variety with a
        # real Medusa colour instead of the generic 'Natural'. Agnostic: just the canonical names.
        if method == "GET":
            return 200, {"colors": sorted(_color_vocab().values())}
        return 405, {"error": "GET /config/v1/colors"}
    if segments and segments[0] == "types":
        # the stone-type vocabulary for the new-variety mint type picker: assign a type to a type-less
        # variety (held until it has one). Agnostic: just the canonical names.
        if method == "GET":
            return 200, {"types": sorted(_type_vocab().values())}
        return 405, {"error": "GET /config/v1/types"}
    if not segments or segments[0] != "sources":
        return 404, {"error": "not found; expected /config/v1/sources[/<name>], /run, /reset, /purge, "
                     "/delist, /pause, /resume, /clean, /review/<kind>, /varieties, /adapters, /colors, "
                     "/types or /variations/<key>/{retire,un_retire}"}
    if len(segments) == 1:
        if method == "GET":
            # enrich each source with what's IN the scraper: the raw scrape (scrape_at + scrape_rows)
            # and how many products reached the ledger (ledger_products) -- so :4200 shows what's there
            # and how old, not just the last run.
            from stone_pipeline.config import scrape_status
            return 200, {"sources": scrape_status.enrich(store.list_rows())}
        return 405, {"error": "use PUT /config/v1/sources/<name> to create"}
    name = segments[1]
    if method == "GET":
        row = store.get_row(name)
        return (200, row) if row else (404, {"error": f"no source {name!r}"})
    if method == "PUT":
        if not isinstance(body, dict):
            return 400, {"error": "body must be a JSON object of source settings"}
        body = {**body, "source": name}   # the path name is authoritative
        err = _validate_source_put(name, body)
        if err:
            return err
        try:
            store.upsert_row(body)
        except sqlite3.IntegrityError:
            # the DB's UNIQUE(source_code) backstop tripped: a concurrent PUT claimed the same code between
            # the app-level check and this write. Report a clean 400, not a 500.
            return 400, {"error": f"source_code {body.get('source_code', '')!r} is already in use by another source"}
        return 200, store.get_row(name)
    if method == "DELETE":
        # stage 2 of a vendor removal ('Remove permanently'): purge the source's qty-0 products and drop
        # its config row. 409 if the source still has LIVE products (take it offline via /delist first).
        from stone_pipeline import lifecycle
        result, code = lifecycle.remove_source(name)
        return code, result
    return 405, {"error": f"method {method} not allowed on a source"}


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
    return None


def _expected_token() -> str:
    token = os.environ.get("BLOKPORT_CONFIG_TOKEN", "").strip()
    if not token:
        raise SystemExit("BLOKPORT_CONFIG_TOKEN is not set; refusing to start the config server")
    return token


class ConfigHandler(BaseHTTPRequestHandler):
    server_version = "blokport-config/1.0"

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
        log.info("config request", extra={"extra_fields": {"client": self.address_string()}})


def serve(host: str | None = None, port: int = 8724) -> None:
    # default 127.0.0.1 (safe on a laptop); ECS sets BLOKPORT_BIND_HOST=0.0.0.0 so peer tasks
    # (Medusa, over the VPC) can reach it. The bearer token still gates every request.
    host = host or os.environ.get("BLOKPORT_BIND_HOST", "127.0.0.1")
    from stone_pipeline.ledger import snapshot, writethrough
    # E14: restore config.db (the durable source lifecycle: pause/delist/enabled) from its S3 snapshot
    # BEFORE seeding, so a redeploy does not lose it and re-seed every source back to active. Restore is a
    # no-op when a local config.db already exists; the seed then only fills a genuinely first-ever run.
    config_db = store.config_db_path()
    snapshot.restore_config(config_db)
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
    snapshot.restore(writethrough.ledger_path())
    # Restore the last scrape's artifact trees (outputs_dir + data/) too: catalog/republish consume them
    # off the ephemeral disk, so without this a redeploy wipes the last scrape and they abort with "no
    # source runs". No-op when a local scrape already exists or none has been snapshotted yet.
    snapshot.restore_artifacts()
    # ...and the last published big-list combinations file, which tree_build diffs against to emit only the
    # NEW combinations ("build on it"). Without it a cold task re-emits the whole ~2M set as the delta.
    snapshot.restore_combinations_baseline()
    # keep config.db durable: a periodic snapshot backstop + a best-effort snapshot on clean shutdown
    # (the lifecycle verbs also snapshot immediately after a pause/delist so a crash never loses one).
    snapshot.start_periodic(config_db, key=snapshot.config_key())
    # snapshot config.db on stop. ECS stops a task with SIGTERM, which by default terminates WITHOUT
    # running atexit -- so a SIGTERM handler is REQUIRED (mirrors the sync server), or a pause/delist set
    # in the last snapshot window is lost on redeploy. atexit is kept as the clean-exit backstop.
    def _snapshot_on_term(*_):
        snapshot.save_config(config_db)
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _snapshot_on_term)
    atexit.register(lambda: snapshot.save_config(config_db))
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
