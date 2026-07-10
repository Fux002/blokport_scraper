"""Phase 5: the proxy toolbox. A scraper names the KIND of proxy it needs (a capability); the registry
(config/proxies.yaml) resolves it to a proxy + its secret. Adding/removing/swapping a proxy is a config
edit -- no code change. Fails loud on a malformed registry."""

from __future__ import annotations

import pytest

from stone_pipeline.config import proxies


def test_stone_registry_loads():
    reg = proxies.load_proxies()                       # the real config/proxies.yaml
    assert "residential_soax" in reg
    p = reg["residential_soax"]
    assert p.type == "residential" and p.url_env == "BLOKPORT_SCRAPER_PROXY"
    assert "cloudflare_residential" in p.for_capabilities


def test_capability_resolution_reads_the_secret(monkeypatch):
    monkeypatch.setenv("BLOKPORT_SCRAPER_PROXY", "http://u:p@proxy:1337")
    url, name = proxies.proxy_url_for_capability("cloudflare_residential")
    assert url == "http://u:p@proxy:1337" and name == "residential_soax"


def test_capability_with_unset_secret_names_the_proxy_but_no_url(monkeypatch):
    monkeypatch.delenv("BLOKPORT_SCRAPER_PROXY", raising=False)
    url, name = proxies.proxy_url_for_capability("cloudflare_residential")
    assert url is None and name == "residential_soax"   # so the caller can warn (source needs it, secret unset)


def test_unknown_or_empty_capability_resolves_to_nothing():
    assert proxies.proxy_url_for_capability("nonexistent") == (None, None)
    assert proxies.proxy_url_for_capability("") == (None, None)


def test_malformed_registry_fails_loud(tmp_path):
    bad = tmp_path / "proxies.yaml"
    bad.write_text("proxies:\n  broken: {provider: X}\n", encoding="utf-8")   # missing type/url_env/for_capabilities
    with pytest.raises(ValueError, match="missing required keys"):
        proxies.load_proxies(bad)


def test_adding_a_second_proxy_is_config_only(tmp_path):
    # the toolbox promise: a new proxy (e.g. a US datacenter provider) is added by editing the yaml, no code.
    cfg = tmp_path / "proxies.yaml"
    cfg.write_text(
        "proxies:\n"
        "  dc_us: {provider: BrightData, type: datacenter, url_env: BLOKPORT_PROXY_DC_US, "
        "regions: [US], for_capabilities: [us_datacenter]}\n", encoding="utf-8")
    reg = proxies.load_proxies(cfg)
    assert reg["dc_us"].url_env == "BLOKPORT_PROXY_DC_US" and "us_datacenter" in reg["dc_us"].for_capabilities
