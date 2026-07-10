"""Proxy toolbox (Phase 5): a config-driven registry of proxies a scraper picks from by CAPABILITY.

The single-proxy env var (BLOKPORT_SCRAPER_PROXY gated by a per-scraper `needs_proxy` bool) becomes a named
registry: a scraper declares `proxy_capability`, and the registry maps it to a proxy definition whose URL is
read from the named env var (injected per env from SSM). Adding/removing/swapping a proxy is an edit to
config/proxies.yaml -- no scraper or code change. Fails loud on a malformed registry; a capability that no
proxy serves, or whose secret is unset, resolves to no proxy (the caller decides whether that is fatal).
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

_REGISTRY_PATH = Path(__file__).with_name("proxies.yaml")


@dataclass(frozen=True)
class ProxyDef:
    name: str
    provider: str
    type: str                       # residential | datacenter | rotating
    url_env: str                    # env var (from SSM) holding the proxy URL
    regions: tuple[str, ...]
    for_capabilities: frozenset[str]


def load_proxies(path: str | Path | None = None) -> dict[str, ProxyDef]:
    """Load the proxy registry, name -> ProxyDef. Fails loud on a malformed file (a proxy entry missing a
    required field), never a silent empty registry that would drop a needed proxy."""
    path = Path(path or _REGISTRY_PATH)
    if not path.exists():
        return {}                                    # no toolbox configured -> scrapers fall back to legacy
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, ProxyDef] = {}
    for name, spec in (data.get("proxies") or {}).items():
        missing = [k for k in ("provider", "type", "url_env", "for_capabilities") if k not in spec]
        if missing:
            raise ValueError(f"proxy {name!r} in {path} is missing required keys: {missing}")
        out[name] = ProxyDef(
            name=name, provider=spec["provider"], type=spec["type"], url_env=spec["url_env"],
            regions=tuple(spec.get("regions") or []),
            for_capabilities=frozenset(spec["for_capabilities"]))
    return out


@functools.lru_cache(maxsize=1)
def registry() -> dict[str, ProxyDef]:
    return load_proxies()


def proxy_for_capability(capability: str) -> ProxyDef | None:
    """The first registered proxy that serves `capability`, or None if none does."""
    if not capability:
        return None
    for proxy in registry().values():
        if capability in proxy.for_capabilities:
            return proxy
    return None


def proxy_url_for_capability(capability: str) -> tuple[str | None, str | None]:
    """(url, proxy_name) for `capability` -- the proxy's URL read from its env var. (None, name) when the
    proxy is registered but its secret is unset (so the caller can warn); (None, None) when no proxy serves
    the capability at all."""
    proxy = proxy_for_capability(capability)
    if proxy is None:
        return None, None
    url = os.environ.get(proxy.url_env, "").strip()
    return (url or None), proxy.name
