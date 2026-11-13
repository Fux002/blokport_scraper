"""A failed attributes.csv fetch at the config server's boot is RECORDED, never fatal (the manager's ruling on SC #663,
12 Nov). With a thin S3 copy (Medusa mid-reset) or none (a new environment before BE's first export) a refusing boot
would crash-loop and take the whole config API down, /reset and /run included. So the boot records why; the routes
that read the type and colour vocabulary (decide, GET /types and /colors, the photos pages) answer 503 naming the
cause; every other route serves; the produce's own fetch still refuses loud; and a later successful fetch (the boot's,
a photos page's or the produce's) clears it. Hermetic: a tmp attributes.csv and a stand-in S3. No em dashes.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from stone_pipeline.config import store
from stone_pipeline.config.control import server
from stone_pipeline.io import medusa_inputs

HEADER = "category,value,sourceid,category_handles\n"
FULL = HEADER + "".join(f"color,c{i},id{i},\n" for i in range(100))
FULLER = HEADER + "".join(f"color,c{i},id{i},\n" for i in range(110))
THIN = HEADER + "color,c0,id0,\n"                                   # 2 lines against 101: Medusa mid-reset
NO_SUCH_KEY = ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
DECIDE = {"source": "zucchi", "scraped": "Honey Onyx Slab", "name": "Honey Onyx", "type": "Onyx"}


class _S3:
    """attributes.csv on a stand-in S3: `body` is written to the download target, or `error` raised."""

    def __init__(self, body: str | None = None, error: Exception | None = None):
        self._body, self._error = body, error

    def download_file(self, bucket, key, filename):
        if self._error is not None:
            raise self._error
        Path(filename).write_text(self._body, encoding="utf-8")


@pytest.fixture
def attributes(tmp_path, monkeypatch) -> Path:
    """The environment's attributes.csv, a full local copy, wherever medusa_inputs fetches and checks it."""
    path = tmp_path / "from_medusa" / "attributes.csv"
    path.parent.mkdir()
    path.write_text(FULL, encoding="utf-8")
    monkeypatch.setattr(medusa_inputs, "SETTINGS", SimpleNamespace(
        paths=SimpleNamespace(attributes_csv=path, from_medusa_dir=path.parent)))
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    return path


def _boot(monkeypatch, tmp_path, s3: _S3) -> None:
    """server.boot with every restore a no-op and attributes.csv fetched from `s3`."""
    from stone_pipeline.ledger import snapshot
    for step in ("restore_config", "await_file", "restore_artifacts"):
        monkeypatch.setattr(snapshot, step, lambda *a, **k: None)
    monkeypatch.setattr(store, "seed_from_yaml", lambda *a, **k: None)
    monkeypatch.setattr(store, "reconcile_interrupted_runs", lambda *a, **k: [])
    monkeypatch.setattr(medusa_inputs, "fetch_field_kinds", lambda *a, **k: None)
    monkeypatch.setattr(medusa_inputs, "aws_client", lambda *a, **k: s3)
    server.boot(tmp_path / "config.db", tmp_path / "ledger.db")


def _unavailable(code_body, *words: str) -> bool:
    code, body = code_body
    return code == 503 and body["error"].startswith("attributes.csv unavailable: ") and all(
        w in body["error"] for w in words)


def test_a_boot_with_a_thin_s3_copy_serves_and_the_vocabulary_routes_answer_503_naming_why(
        attributes, tmp_path, monkeypatch):
    before = attributes.read_text(encoding="utf-8")
    _boot(monkeypatch, tmp_path, _S3(THIN))                                # recorded, not raised
    assert attributes.read_text(encoding="utf-8") == before               # the thin copy never replaced it
    assert server.dispatch("GET", ["run"], None)[0] == 200                # the run status serves
    assert server.dispatch("GET", ["adapters"], None)[0] == 200
    assert _unavailable(server.dispatch("PUT", ["review", "decide"], dict(DECIDE)), "thin")
    assert _unavailable(server.dispatch("GET", ["types"], None), "thin")
    assert _unavailable(server.dispatch("GET", ["colors"], None), "thin")


def test_a_boot_in_a_new_environment_before_be_s_first_export_serves(attributes, tmp_path, monkeypatch):
    attributes.unlink()                                                   # no committed copy (prod), no export yet
    _boot(monkeypatch, tmp_path, _S3(error=NO_SUCH_KEY))
    assert server.dispatch("GET", ["run"], None)[0] == 200
    assert _unavailable(server.dispatch("PUT", ["review", "decide"], dict(DECIDE)), "could not fetch")


def test_a_boot_with_a_malformed_copy_serves_and_names_it(attributes, tmp_path, monkeypatch):
    no_column = "category,value,sourceid\n" + "".join(f"color,c{i},id{i}\n" for i in range(100))
    _boot(monkeypatch, tmp_path, _S3(no_column))
    assert _unavailable(server.dispatch("GET", ["types"], None), "category_handles")


def test_every_read_of_attributes_csv_on_the_server_answers_503_while_its_fetch_stands_refused(
        attributes, tmp_path, monkeypatch):
    # reviewer 2's LOW: the restricted (archived or internal) values and ids come from the same file; the id-paste route
    # reads nothing else of it, so it served the stale file's restrictions
    _boot(monkeypatch, tmp_path, _S3(THIN))
    assert _unavailable(server.dispatch("PUT", ["review", "attributes", "Grey"],
                                        {"kind": "color", "medusa_id": "col_grey"}), "thin")


def test_a_photos_page_whose_fetch_is_refused_answers_503_naming_why(attributes, monkeypatch):
    from stone_pipeline.photos import review as rv
    monkeypatch.setattr(rv, "client", lambda: _S3(THIN))
    monkeypatch.setattr(rv, "hidden", lambda s3, *a: rv.fetch_attributes(s3))   # the page's own fetch, first
    assert _unavailable(server.dispatch("GET", ["photos", "hidden"], None), "thin")


def test_a_later_successful_fetch_clears_it(attributes, tmp_path, monkeypatch):
    _boot(monkeypatch, tmp_path, _S3(THIN))
    assert server.dispatch("GET", ["types"], None)[0] == 503
    medusa_inputs.fetch_attributes(client=_S3(FULLER))                   # a photos page's fetch, or the next boot's
    assert attributes.read_text(encoding="utf-8") == FULLER
    assert server.dispatch("GET", ["types"], None)[0] == 200


class _Listing(_S3):
    """The produce's view of S3: the from_medusa/ prefix lists attributes.csv alone."""

    def get_paginator(self, _):
        key = f"{medusa_inputs.ENV_SEGMENT}/scraper/from_medusa/attributes.csv"
        return SimpleNamespace(paginate=lambda **k: [{"Contents": [{"Key": key}]}])


def test_the_produce_s_successful_fetch_clears_it(attributes, tmp_path, monkeypatch):
    _boot(monkeypatch, tmp_path, _S3(THIN))
    monkeypatch.setattr(medusa_inputs, "aws_client", lambda *a, **k: _Listing(FULLER))
    medusa_inputs.fetch_all()
    assert server.dispatch("GET", ["types"], None)[0] == 200


def test_every_fetch_outside_the_boot_still_refuses_loud(attributes):
    # one refusing path (34's rev 2): the photos run and a photos page raise; only the BOOT records instead of raising
    with pytest.raises(medusa_inputs.AttributesUnavailable, match="thin"):
        medusa_inputs.fetch_attributes(client=_S3(THIN))


def test_the_produce_s_fetch_refuses_a_thin_copy_loud_and_leaves_the_local_file(attributes, monkeypatch):
    # the manager's ruling (G10's attributes half): a produce never runs on the local vocab during a Medusa reset
    before = attributes.read_text(encoding="utf-8")
    monkeypatch.setattr(medusa_inputs, "aws_client", lambda *a, **k: _Listing(THIN))
    with pytest.raises(medusa_inputs.AttributesUnavailable, match=r"thin \(2 lines against 101 in the local copy\)"):
        medusa_inputs.fetch_all()
    assert attributes.read_text(encoding="utf-8") == before
    assert sorted(p.name for p in attributes.parent.iterdir()) == ["attributes.csv", "attributes.csv.unavailable"]
    assert _unavailable(server.dispatch("GET", ["types"], None), "thin")          # and the server says so


@pytest.mark.parametrize("production", [True, False])
def test_a_refused_attributes_fetch_stops_the_produce_in_every_environment(monkeypatch, production):
    # produce._fetch_inputs keeps development best-effort for an S3 outage, but a refused vocabulary is no outage. The
    # later fetches succeed here, so the stop can only be the refusal's own (each is fatal by itself: test_field_kinds,
    # test_dimension_overrides), and none of them is reached.
    from stone_pipeline import produce
    from stone_pipeline.config import categories
    reached = []

    def refused():
        raise medusa_inputs.AttributesUnavailable("the S3 copy looks thin (2 lines against 101 in the local copy)")
    monkeypatch.setattr(medusa_inputs, "fetch_all", refused)
    monkeypatch.setattr(medusa_inputs, "fetch_field_kinds", lambda: reached.append("field_kinds"))
    monkeypatch.setattr(medusa_inputs, "fetch_overrides", lambda: reached.append("overrides") or False)
    monkeypatch.setattr(categories, "refresh_category_pcats", lambda: reached.append("pcats"))
    monkeypatch.setattr(produce, "IS_PRODUCTION", production, raising=False)
    with pytest.raises(SystemExit) as stop:
        produce._fetch_inputs()
    assert stop.value.code == 2 and reached == []
