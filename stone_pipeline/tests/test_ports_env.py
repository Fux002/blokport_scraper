"""Port ids are Medusa ids and differ per environment, so ports.csv is a per-env
from_medusa/<env>/ file. Production must resolve ONLY against its own downloaded file
and never fall back to the dev-id catalog_source master (that ships dev ids into prod,
which Medusa silently drops -> all port_of_origin blank). Dev may fall back.
"""
from pathlib import Path
from types import SimpleNamespace

import stone_pipeline.reference.loaders as loaders
from stone_pipeline.reference.loaders import load_ports


def _settings(primary: Path, fallback: Path):
    return SimpleNamespace(paths=SimpleNamespace(ports_csv=primary, ports_csv_fallback=fallback))


def _write_ports(path: Path) -> None:
    path.write_text(
        "id,name,un_locode,country_iso\n"
        "01TESTPORTID0000000000000,Genoa,ITGOA,IT\n",
        encoding="utf-8",
    )


def test_dev_falls_back_to_master_when_per_env_absent(tmp_path, monkeypatch):
    fallback = tmp_path / "catalog_source_ports.csv"
    _write_ports(fallback)
    monkeypatch.setattr(loaders, "SETTINGS", _settings(tmp_path / "missing.csv", fallback))
    monkeypatch.setattr(loaders, "IS_PRODUCTION", False)
    ports = load_ports()
    assert ports.by_locode.get("ITGOA"), "dev must fall back to the catalog_source master"


def test_prod_refuses_dev_fallback_when_per_env_absent(tmp_path, monkeypatch):
    fallback = tmp_path / "catalog_source_ports.csv"
    _write_ports(fallback)  # a real (dev) master exists...
    monkeypatch.setattr(loaders, "SETTINGS", _settings(tmp_path / "missing.csv", fallback))
    monkeypatch.setattr(loaders, "IS_PRODUCTION", True)
    ports = load_ports()
    # ...but prod must NOT load it: empty, so every row is flagged port_unresolved, never dev ids.
    assert not ports.iso_by_port and not ports.by_locode, "prod loaded the dev-id fallback (regression)"


def test_prod_uses_its_own_per_env_file_when_present(tmp_path, monkeypatch):
    primary = tmp_path / "prod_ports.csv"
    _write_ports(primary)
    monkeypatch.setattr(loaders, "SETTINGS", _settings(primary, tmp_path / "unused_fallback.csv"))
    monkeypatch.setattr(loaders, "IS_PRODUCTION", True)
    ports = load_ports()
    assert ports.by_locode.get("ITGOA") == "01TESTPORTID0000000000000"


def test_explicit_path_is_honored_env_independent(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit.csv"
    _write_ports(explicit)
    monkeypatch.setattr(loaders, "IS_PRODUCTION", True)  # even in prod, an explicit path wins
    ports = load_ports(explicit)
    assert ports.by_locode.get("ITGOA")
