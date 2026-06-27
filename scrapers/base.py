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
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx

from stone_pipeline.io.ssrf import url_allowed

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
_MAX_REDIRECTS = 5


class _SSRFBlocked(Exception):
    """A fetch target resolved to a non-public host (or non-http(s) scheme) — never retried."""

VALID_FORMATS = ("slab", "block", "tile")
_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119 Safari/537.36",
]


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

    # tunables
    timeout: float = 30.0
    max_retries: int = 5
    backoff_base: float = 2.0
    image_delay: tuple[float, float] = (0.3, 0.9)

    # capture the full source object as raw_json on every row, so no available
    # field is ever lost (the pipeline can mine more later without re-scraping).
    # Set False for very large scrapes where the raw blob bloats the CSV.
    capture_raw: bool = True

    # always-present columns the base adds to every row (one consistent naming for
    # every scraper, so the pipeline adapters all read the same columns)
    BASE_COLUMNS = ["format", "image_count", "image_urls", "image_filenames_local",
                    "scrape_timestamp", "raw_json"]

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
        self.log = self._make_logger()

    # --- HTTP ----------------------------------------------------------------
    def _cffi(self):
        """Lazily build a curl_cffi session (Chrome TLS impersonation), routed through
        BLOKPORT_SCRAPER_PROXY when set. Cloudflare blocks datacenter IPs (the AWS NAT
        egress), so the Cloudflare-fronted sources need a residential proxy from AWS;
        locally the env var is unset and it connects directly."""
        if getattr(self, "_cffi_session", None) is None:
            try:
                from curl_cffi import requests as cffi_requests
            except ImportError as exc:
                raise RuntimeError(
                    "this scraper needs curl_cffi (Cloudflare bypass). "
                    "Install: pip3 install curl_cffi"
                ) from exc
            opts: dict = {"impersonate": self.impersonate}
            proxy = os.environ.get("BLOKPORT_SCRAPER_PROXY", "").strip()
            if proxy:
                opts["proxies"] = {"http": proxy, "https": proxy}
                self.log.info("routing %s through proxy", self.source)
            self._cffi_session = cffi_requests.Session(**opts)
        return self._cffi_session

    def _request(self, method: str, url: str, **kwargs):
        """HTTP with retry, backoff, a rotating UA, and 429 handling. Routes
        through curl_cffi when use_curl_cffi is set (Cloudflare-fronted sites)."""
        headers = {"User-Agent": random.choice(_USER_AGENTS), **kwargs.pop("headers", {})}
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
                    if r.status_code == 429:
                        time.sleep(self.backoff_base ** attempt * 5)
                        break  # retry from the top
                    r.raise_for_status()
                    break
                else:
                    last_exc = RuntimeError(f"too many redirects: {url}")
            except _SSRFBlocked:
                raise  # permanent — never retry a blocked target
            except Exception as exc:  # transient: back off and retry
                last_exc = exc
                time.sleep(self.backoff_base ** attempt)
        self.record_failure("http", method=method, url=url, error=str(last_exc))
        raise RuntimeError(f"{method} failed after {self.max_retries} tries: {url}")

    def get(self, url: str, **kwargs) -> httpx.Response:
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
                r = self.get(url)
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

    def record_failure(self, kind: str, **details) -> None:
        self._failures.append({"kind": kind, **details})

    def _write_csv(self, rows: list[dict]) -> None:
        cols = list(dict.fromkeys([*self.columns, *self.BASE_COLUMNS]))
        with self.products_csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

    def _write_failures(self) -> None:
        if not self._failures:
            return
        keys = sorted({k for f in self._failures for k in f})
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
