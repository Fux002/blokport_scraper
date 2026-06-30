"""The scraper config store: seed from sources.yaml, the pipeline reads it, and the
enabled flag controls which scrapers run."""

from __future__ import annotations

from stone_pipeline.config import store
from stone_pipeline.config.sources import load_source, load_sources


def _seed_yaml(tmp_path):
    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text(
        "polonine:\n"
        "  adapter: polonine\n"
        "  source_code: pol\n"
        "  vendor: Polonine Stone Co\n"
        "  origin_default: IT\n"
        "  ports_default:\n    - Brindisi\n"
        "  mode: review\n"
        "varsha:\n"
        "  adapter: varsha\n"
        "  source_code: var\n"
        "  vendor: Varsha Stones\n"
        "  watermarked: true\n",
        encoding="utf-8")
    return yaml_path


def test_seed_pipeline_read_and_enable(tmp_path, monkeypatch):
    yaml_path = _seed_yaml(tmp_path)
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))

    # seed the store from the committed yaml
    assert store.seed_from_yaml(yaml_path=yaml_path) == 2

    # the pipeline's load_sources/load_source now read the STORE (config.db exists)
    srcs = load_sources()
    assert set(srcs) == {"polonine", "varsha"}
    assert srcs["polonine"].vendor == "Polonine Stone Co"
    assert load_source("polonine").ports_default == ["Brindisi"]
    assert load_source("varsha").watermarked is True

    # all enabled by default; disabling one removes it from the run set
    assert store.enabled_names() == {"polonine", "varsha"}
    store.set_enabled("varsha", False)
    assert store.enabled_names() == {"polonine"}

    # re-seeding never clobbers a row the admin edited (insert-or-ignore)
    assert store.seed_from_yaml(yaml_path=yaml_path) == 0
    assert store.enabled_names() == {"polonine"}   # varsha stays disabled

    # the admin can edit a setting live
    cfg = load_source("polonine")
    cfg.vendor = "Renamed Company"
    store.upsert_source(cfg)
    assert load_source("polonine").vendor == "Renamed Company"


def test_enabled_names_is_none_without_a_store(tmp_path, monkeypatch):
    # no config store -> enabled_names() is None so callers run everything (pre-store behaviour)
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "absent.db"))
    assert store.enabled_names() is None


def test_config_api_dispatch(tmp_path, monkeypatch):
    from stone_pipeline.config import server

    yaml_path = _seed_yaml(tmp_path)
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    store.seed_from_yaml(yaml_path=yaml_path)

    # list every scraper
    code, body = server.dispatch("GET", ["sources"], None)
    assert code == 200 and {s["source"] for s in body["sources"]} == {"polonine", "varsha"}

    # one scraper
    code, body = server.dispatch("GET", ["sources", "polonine"], None)
    assert code == 200 and body["vendor"] == "Polonine Stone Co"

    # the UI disables a scraper + edits a setting (PUT replaces the row)
    code, body = server.dispatch("PUT", ["sources", "varsha"],
                                 {"enabled": False, "adapter": "varsha", "source_code": "var",
                                  "vendor": "Varsha Stones"})
    assert code == 200 and body["enabled"] is False
    assert store.enabled_names() == {"polonine"}   # varsha no longer runs

    assert server.dispatch("GET", ["sources", "nope"], None)[0] == 404
    assert server.dispatch("PUT", ["sources", "x"], "not-a-dict")[0] == 400
