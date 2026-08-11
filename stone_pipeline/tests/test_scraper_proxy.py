"""A declared proxy is applied ONLY on the curl_cffi path (base._cffi -> _resolve_proxy), so a scraper that
sets proxy_capability but runs on plain httpx (use_curl_cffi=False) connects DIRECT with the proxy silently
ignored -- the exact miss that left marenostone blocked despite declaring the proxy. These lock the registry
so it can't recur: every proxied scraper must use curl_cffi, and marenostone specifically is proxied.
"""

from __future__ import annotations

from scrapers.run import REGISTRY


def test_every_scraper_declaring_a_proxy_also_uses_curl_cffi():
    dead = [name for name, cls in REGISTRY.items()
            if getattr(cls, "proxy_capability", "") and not getattr(cls, "use_curl_cffi", False)]
    assert not dead, (f"these scrapers declare proxy_capability but use_curl_cffi=False, so the proxy is "
                      f"DEAD CONFIG (connects direct): {dead}. Set use_curl_cffi=True.")


def test_marenostone_is_proxied_through_curl_cffi():
    cls = REGISTRY["marenostone"]
    assert cls.proxy_capability == "cloudflare_residential"
    assert cls.use_curl_cffi is True, "marenostone must use curl_cffi or its proxy is silently ignored"
