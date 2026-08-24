"""ScraperBase: the shared framework for site scrapers.

A scraper subclass writes ONLY the site-specific part (how to list products and
how to parse one). The base owns everything generic and easy to get wrong:

  - output layout (item 1):  data/<source>/<timestamp>/  per source, per run, so
    different sites never share a folder.
  - image download + naming (item 1):  images/<source>_<product_id>_<idx>.ext,
    source-namespaced so two sites that reuse a product id never collide.
  - format declaration (item 3):  every row carries a `format` of slab/block/tile,
    either a per-scraper constant or set per product in parse_product.
  - products.csv, scrape.log, failures.csv, a run summary.
  - retrying HTTP with backoff and a rotating user agent.

This mirrors the pipeline's AdapterBase: ScraperBase produces the per-source
products.csv that an Adapter then maps into the canonical pipeline.
"""

from __future__ import annotations

import csv
import logging
from stone_pipeline.core import env
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx

from stone_pipeline.io.ssrf import url_allowed

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
_MAX_REDIRECTS = 5


class _SSRFBlocked(Exception):
    """A fetch target resolved to a non-public host (or non-http(s) scheme) -- never retried."""

VALID_FORMATS = ("slab", "block", "tile")
# A pool of current desktop UAs; ONE is picked per scraper INSTANCE (per run), not per request, so a
# session looks like a single human browser rather than a client whose UA flips on every hop.
_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]
# Upper bound on a server-provided Retry-After wait, so a hostile/misconfigured header can never park a
# scrape for hours. A real rate-limit cooldown is well under this.
_RETRY_AFTER_MAX = 300.0


class ScraperBase:
    # subclass sets these
    source: str = ""
    category: Optional[str] = None          # constant format if every product is one kind
    columns: list[str] = []                 # the site-specific CSV columns (no image/format cols)
    id_field: str = "product_id"            # the row column that uniquely ids a product
                                            # (used to name its images); e.g. "bundle_id"
    # By default DON'T download images: the upload file uses the source URLs
    # directly (simpler, no re-hosting, no local-file mixup). Set True only when a
    # site needs local copies (then images are namespaced + organised per source).
    download_images_enabled: bool = False
    # Cloudflare-fronted sites (e.g. SlabWare tenants) challenge plain httpx; set
    # use_curl_cffi to route HTTP through curl_cffi with a Chrome TLS fingerprint.
    # Lazy-imported so the module imports fine without curl_cffi installed.
    use_curl_cffi: bool = False
    impersonate: str = "chrome120"
    # proxy_capability is the toolbox way (config/proxies.yaml): a scraper names the KIND of proxy it needs
    # (e.g. "cloudflare_residential") and the registry resolves it to a proxy + its secret. Adding/swapping a
    # proxy is a config edit, not a code change. Empty = no proxy from the toolbox.
    proxy_capability: str = ""
    # needs_proxy is the LEGACY single-proxy flag (BLOKPORT_SCRAPER_PROXY), still honoured as a fallback when
    # no proxy_capability is set. Separate from use_curl_cffi: some Cloudflare tenants accept the Chrome TLS
    # fingerprint from a datacenter IP (varsha -> curl_cffi alone works), others block it and demand a
    # residential one (polonine). The residential proxy is metered per-GB, so it is used ONLY by a scraper
    # that asks for it. Images never use it (their hosts serve datacenter IPs; see io/download.py).
    needs_proxy: bool = False

    # tunables
    timeout: float = 30.0
    max_retries: int = 5
    backoff_base: float = 2.0
    image_delay: tuple[float, float] = (0.3, 0.9)
    # PREVENTIVE inter-request delay (random seconds in [lo, hi]) paid once before each non-image request,
    # so page/detail GETs are spaced out and do not PROVOKE a 429 in the first place (the 429 backoff below
    # is only reactive, after the block already happened). A generous default: correctness over speed -- a
    # scrape may run for hours. A source that still rate-limits raises this; set (0, 0) to disable.
    request_delay: tuple[float, float] = (1.0, 2.5)
    # DECLARED source convention (source-isolation): the unit a source renders dimensions in when a value
    # carries none of its own (e.g. marenostone renders cm). Read by a scraper's dimension extractor so it
    # never guesses a unit; "" means values already carry their unit. First-class here so a new scraper
    # declares it rather than reinventing a per-scraper constant.
    dimension_unit: str = ""

    # capture the full source object as raw_json on every row, so no available
    # field is ever lost (the pipeline can mine more later without re-scraping).
    # Set False for very large scrapes where the raw blob bloats the CSV.
    capture_raw: bool = True

    # Reserved column that carries a per-row fetch-failure signal from the scraper INTO the pipeline: a
    # pipe-list of field-groups whose sub-fetch failed for THIS product (e.g. "dims" when a detail-page
    # fetch was rate-limited). The adapter maps it to CanonicalRow.fetch_failed_fields, and derive then
    # HOLDS the row (never defaults a recoverable-but-missing value) instead of shipping a fabricated one.
    FETCH_FAILED_COL = "fetch_failed"

    # always-present columns the base adds to every row (one consistent naming for
    # every scraper, so the pipeline adapters all read the same columns)
    BASE_COLUMNS = ["format", "image_count", "image_urls", "image_filenames_local",
                    "scrape_timestamp", "raw_json", FETCH_FAILED_COL]

    def __init__(self, data_dir: Path | None = None):
        if not self.source:
            raise ValueError("scraper must set `source`")
        self.timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        root = Path(data_dir or DATA_DIR)
        self.run_dir = root / self.source / self.timestamp
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.images_dir = self.run_dir / "images"
        if self.download_images_enabled:  # only when actually downloading
            self.images_dir.mkdir(parents=True, exist_ok=True)
        self.products_csv = self.run_dir / "products.csv"
        self.failures_csv = self.run_dir / "failures.csv"
        self.complete_marker = self.run_dir / "scrape_complete.json"
        self._failures: list[dict] = []
        # Set True by a scraper that knows it terminated early (e.g. pagination stopped before the
        # advertised total). The completion marker then records the scrape as INCOMPLETE so the
        # pipeline refuses to treat this truncated folder as the authoritative latest scrape.
        self._incomplete = False
        # follow_redirects=False: _request follows hops manually so the SSRF guard
        # validates every one (a public source must not 30x into an internal host).
        self._client = httpx.Client(timeout=self.timeout, follow_redirects=False)
        # one stable UA for the whole run (a real browser does not change UA mid-session)
        self._ua = random.choice(_USER_AGENTS)
        self.log = self._make_logger()
        # Fail LOUD on a dead proxy declaration: the proxy is wired ONLY into the curl_cffi session
        # (_cffi -> _resolve_proxy). A scraper that declares proxy_capability but runs on plain httpx
        # (use_curl_cffi=False) would connect DIRECT with NO warning -- the exact silent miss that left
        # marenostone blocked. Surface it at init so it can never be dead config again.
        if self.proxy_capability and not self.use_curl_cffi:
            self.log.warning("%s declares proxy_capability=%r but use_curl_cffi is False -- the proxy is "
                             "applied ONLY on the curl_cffi path, so it will NOT be used. Set "
                             "use_curl_cffi=True to actually route through it.",
                             self.source, self.proxy_capability)

    # --- HTTP ----------------------------------------------------------------
    def _resolve_proxy(self) -> Optional[str]:
        """The proxy URL for this scraper, or None (connect direct). Toolbox first: a declared
        proxy_capability resolves via config/proxies.yaml to a proxy + its secret; a capability that
        resolves to no secret logs LOUD (a source known to need a proxy would otherwise silently run direct
        into a block). Legacy fallback: needs_proxy uses BLOKPORT_SCRAPER_PROXY. Images never call this."""
        if self.proxy_capability:
            from stone_pipeline.config.proxies import proxy_url_for_capability
            url, name = proxy_url_for_capability(self.proxy_capability)
            if url:
                self.log.info("routing %s through %s (%s)", self.source, name, self.proxy_capability)
                return url
            self.log.warning("%s declares proxy_capability=%r but its proxy/secret is unset; connecting "
                             "DIRECT (likely to be blocked)", self.source, self.proxy_capability)
            return None
        if self.needs_proxy:
            url = env.getenv("BLOKPORT_SCRAPER_PROXY", "").strip()
            if url:
                self.log.info("routing %s through the residential proxy (legacy needs_proxy)", self.source)
                return url
        return None

    def _cffi(self):
        """Lazily build a curl_cffi session (Chrome TLS impersonation), routed through the proxy this
        scraper resolves (see _resolve_proxy) or direct. Locally the proxy secret is unset, so everything
        connects direct."""
        if getattr(self, "_cffi_session", None) is None:
            try:
                from curl_cffi import requests as cffi_requests
            except ImportError as exc:
                raise RuntimeError(
                    "this scraper needs curl_cffi (Cloudflare bypass). "
                    "Install: pip3 install curl_cffi"
                ) from exc
            opts: dict = {"impersonate": self.impersonate}
            proxy = self._resolve_proxy()
            if proxy:
                opts["proxies"] = {"http": proxy, "https": proxy}
            self._cffi_session = cffi_requests.Session(**opts)
        return self._cffi_session

    def _retry_after_seconds(self, header: str | None, attempt: int) -> float:
        """Seconds to wait after a 429, honoring Retry-After in BOTH forms (delta-seconds and an HTTP-date),
        clamped to _RETRY_AFTER_MAX; falls back to exponential backoff when the header is absent/unparseable."""
        fallback = self.backoff_base ** attempt * 5
        if not header:
            return fallback
        header = header.strip()
        try:
            return min(float(header), _RETRY_AFTER_MAX)          # delta-seconds form
        except ValueError:
            pass
        try:                                                      # HTTP-date form
            delta = (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds()
            return min(max(delta, 0.0), _RETRY_AFTER_MAX) if delta > 0 else fallback
        except (TypeError, ValueError):
            return fallback

    def _request(self, method: str, url: str, throttle: bool = True, allow_missing: bool = False, **kwargs):
        """HTTP with a preventive throttle, retry, backoff, a per-session UA, and 429 handling. Routes
        through curl_cffi when use_curl_cffi is set (Cloudflare-fronted sites). `throttle=False` skips the
        preventive delay (used by image downloads, which pace themselves via image_delay).

        `allow_missing=True` returns None on a definitive 404 without retrying, so a paginator can tell
        'past the last page' (a legitimate stop) from a transient failure (which still retries then raises).
        Every other non-200 status is treated as a retryable error exactly as before."""
        # Preventive spacing, ONCE per logical request (not per redirect hop, not per retry): keep page/detail
        # GETs from provoking a 429. Reactive 429/transient backoffs below still apply on top when needed.
        if throttle and self.request_delay[1] > 0:
            time.sleep(random.uniform(*self.request_delay))
        headers = {"User-Agent": self._ua, **kwargs.pop("headers", {})}
        if self.use_curl_cffi and "content" in kwargs:  # httpx 'content' -> curl_cffi 'data'
            kwargs["data"] = kwargs.pop("content")
        last_exc = None
        for attempt in range(self.max_retries):
            try:
                current = url
                for _ in range(_MAX_REDIRECTS + 1):
                    # SSRF guard: validate every hop (entry + each redirect target)
                    if not url_allowed(current):
                        self.record_failure("ssrf_blocked", method=method, url=current,
                                             error="non-public host or non-http(s) scheme")
                        raise _SSRFBlocked(current)
                    if self.use_curl_cffi:
                        r = self._cffi().request(method, current, headers=headers,
                                                 allow_redirects=False, **kwargs)
                    else:
                        r = self._client.request(method, current, headers=headers, **kwargs)
                    if r.status_code in (301, 302, 303, 307, 308):
                        loc = r.headers.get("location")
                        if not loc:
                            break
                        current = str(httpx.URL(current).join(loc))  # resolve relative -> revalidate
                        continue
                    if r.status_code == 200:
                        return r
                    if allow_missing and r.status_code == 404:
                        return None   # definitive absence -> caller's end-of-list, not a transient error
                    if r.status_code == 429:
                        # record WHY (else the final raise reports last_exc=None for a pure-429 run) and
                        # honor the server's Retry-After (delta-seconds OR HTTP-date), else exponential backoff.
                        last_exc = RuntimeError(f"rate limited (HTTP 429): {url}")
                        time.sleep(self._retry_after_seconds(r.headers.get("Retry-After"), attempt))
                        break  # retry from the top
                    r.raise_for_status()
                    break
                else:
                    last_exc = RuntimeError(f"too many redirects: {url}")
            except _SSRFBlocked:
                raise  # permanent -- never retry a blocked target
            except Exception as exc:  # transient: back off and retry
                last_exc = exc
                time.sleep(self.backoff_base ** attempt)
        self.record_failure("http", method=method, url=url, error=str(last_exc))
        raise RuntimeError(f"{method} failed after {self.max_retries} tries: {url}")

    def get(self, url: str, **kwargs) -> Optional[httpx.Response]:
        # Returns an httpx.Response, or None only when allow_missing=True and the server gave a 404.
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self._request("POST", url, **kwargs)

    # --- images (item 1) -----------------------------------------------------
    def download_images(self, product_id: str, urls: list[str]) -> list[str]:
        """Download every image into the per-source folder with a source-namespaced
        name. Returns the local filenames. A failed image is recorded and skipped,
        never crashing the run."""
        saved: list[str] = []
        seen: set[str] = set()
        idx = 0
        for url in urls:
            url = (url or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            idx += 1
            ext = self._ext(url)
            filename = f"{self.source}_{self._safe(product_id)}_{idx}{ext}"
            path = self.images_dir / filename
            if path.exists() and path.stat().st_size > 0:
                saved.append(filename)
                continue
            try:
                r = self.get(url, throttle=False)   # images pace themselves via image_delay below
                path.write_bytes(r.content)
                saved.append(filename)
            except Exception as exc:
                self.record_failure("image", product_id=product_id, url=url, error=str(exc))
            time.sleep(random.uniform(*self.image_delay))
        return saved

    # --- the orchestration ---------------------------------------------------
    def run(self) -> Path:
        self.log.info("scraper start: %s -> %s", self.source, self.run_dir)
        import json
        rows: list[dict] = []
        for raw in self.list_products():
            try:
                row = self.parse_product(raw)
            except Exception as exc:
                self.record_failure("parse", error=str(exc))
                continue
            if not row:
                continue
            if self.capture_raw and "raw_json" not in row:
                try:
                    row["raw_json"] = json.dumps(raw, ensure_ascii=False, default=str)
                except Exception:
                    row["raw_json"] = ""
            fmt = self._resolve_format(row)
            image_urls = row.pop("image_urls", []) or []
            # default: keep the source URLs, do not download (the upload file links
            # straight to the source). Opt in with download_images_enabled.
            files = self.download_images(row.get(self.id_field, ""), image_urls) \
                if self.download_images_enabled else []
            row.update({
                "format": fmt,
                "image_count": len(image_urls),
                "image_urls": " | ".join(image_urls),               # source links
                "image_filenames_local": " | ".join(files),         # empty unless downloaded
                "scrape_timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            })
            rows.append(row)
        self._write_csv(rows)
        self._write_failures()
        self._summary(rows)
        # A 0-row scrape is almost never a genuinely empty catalog (a blocked/empty first page, a total
        # block) -- mark it INCOMPLETE so the pipeline keeps the prior good folder instead of treating
        # the wipe as authoritative and delisting the whole source. (A mid-pagination truncation that
        # still yielded rows is signalled by the scraper itself via mark_incomplete -- e.g. a list-page
        # fetch that exhausted retries -- and bounded downstream by the >30% delist guard.)
        if not self._incomplete and not rows:
            self.mark_incomplete("zero rows scraped -- treating as a failed run, not an empty catalog")
        # Written ONLY after a clean finish: a crash mid-scrape leaves no marker, so the pipeline
        # falls back to the prior good folder instead of ingesting a half-written products.csv.
        self.complete_marker.write_text(
            json.dumps({"rows": len(rows), "complete": not self._incomplete,
                        "timestamp": self.timestamp}),
            encoding="utf-8")
        return self.products_csv

    def mark_incomplete(self, reason: str = "") -> None:
        """A scraper calls this when it knows it stopped short (e.g. pagination broke before the
        advertised total) so the run is recorded as truncated and not ingested as authoritative."""
        self._incomplete = True
        self.log.warning("scrape marked INCOMPLETE: %s", reason or "(no reason given)")

    # --- the site-specific bits (subclass implements these) ------------------
    def list_products(self) -> Iterable[Any]:
        raise NotImplementedError

    def parse_product(self, raw: Any) -> Optional[dict]:
        """Return a row dict including an `image_urls` list and, when the format is
        per-product, a `format` value. Return None to skip."""
        raise NotImplementedError

    # --- helpers -------------------------------------------------------------
    def _resolve_format(self, row: dict) -> str:
        fmt = (row.get("format") or self.category or "").strip().lower()
        if fmt not in VALID_FORMATS:
            self.record_failure("format", product_id=row.get("product_id"), got=fmt)
            self.log.warning("unknown format %r for product %s", fmt, row.get("product_id"))
        return fmt

    # id-like kwargs a caller may pass; the first present one becomes the stable `ref` triage column.
    _REF_KEYS = ("id", "product_id", "bundle_id", "slug", "sku")

    def record_failure(self, kind: str, **details) -> None:
        """Record a failure with a STABLE, triageable schema: always `kind`, a derived `ref` (whatever
        id-like field the caller passed, so every row is traceable), `url`, and `error` -- then any extra
        keys. Callers keep passing whatever kwargs they like; the core columns are always present + ordered
        (see _write_failures), so failures.csv does not shift shape run to run."""
        ref = next((str(details[k]) for k in self._REF_KEYS if details.get(k)), "")
        self._failures.append({**details, "kind": kind, "ref": ref,
                               "url": str(details.get("url", "")), "error": str(details.get("error", ""))})

    def mark_fetch_failed(self, row: dict, group: str, **details) -> None:
        """A sub-fetch for THIS product failed recoverably (e.g. a rate-limited detail page): mark the row
        so the pipeline HOLDS it for retry instead of defaulting the missing value, AND audit it. Marking
        both in one call keeps the carried signal and the audit trail in sync. `group` is the field-group
        (e.g. "dims"); appended to the reserved column as a deduped pipe-list."""
        existing = [g for g in (row.get(self.FETCH_FAILED_COL) or "").split("|") if g]
        if group not in existing:
            existing.append(group)
            self.record_failure(group, **details)   # audit once per (row, group)
        row[self.FETCH_FAILED_COL] = "|".join(existing)

    def _write_csv(self, rows: list[dict]) -> None:
        cols = list(dict.fromkeys([*self.columns, *self.BASE_COLUMNS]))
        with self.products_csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

    def _write_failures(self) -> None:
        if not self._failures:
            return
        # stable column ORDER: the core schema first, then any extra keys sorted -- so failures.csv is
        # predictable and every row leads with kind/ref/url/error for triage.
        core = ["kind", "ref", "url", "error"]
        keys = core + sorted({k for f in self._failures for k in f} - set(core))
        with self.failures_csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(self._failures)

    def _summary(self, rows: list[dict]) -> None:
        fmts: dict[str, int] = {}
        for r in rows:
            fmts[r["format"]] = fmts.get(r["format"], 0) + 1
        imgs = sum(int(r.get("image_count") or 0) for r in rows)
        self.log.info("done: %d products, %d images, formats=%s, %d failures -> %s",
                      len(rows), imgs, fmts, len(self._failures), self.products_csv)

    def _make_logger(self) -> logging.Logger:
        logger = logging.getLogger(f"scraper.{self.source}")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        fh = logging.FileHandler(self.run_dir / "scrape.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger.addHandler(fh)
        logger.addHandler(sh)
        return logger

    @staticmethod
    def _safe(value: Any) -> str:
        return "".join(c if c.isalnum() else "-" for c in str(value or "")).strip("-") or "x"

    @staticmethod
    def _ext(url: str) -> str:
        last = url.rsplit("/", 1)[-1]
        if "." in last:
            cand = "." + last.rsplit(".", 1)[1].split("?")[0].lower()
            if len(cand) <= 5:
                return cand
        return ".jpg"
