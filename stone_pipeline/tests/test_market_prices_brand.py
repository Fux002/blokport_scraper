"""The brand separation of the competitor-price path (W1): one infrastructure, every brand's data apart. Each named rule
has its own tests. Fixtures only; nothing here reaches a site or a bucket."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from botocore.exceptions import ClientError
from pydantic import ValidationError

from market_prices import config
from market_prices import brand as rules
from market_prices.collect import RunResult, write_run
from market_prices.pilot import pilot_register
from market_prices.publish import publish_run
from market_prices.register import RegisterError, load_register
from market_prices.schema import SiteRunStatus, SiteStatus, Source
from market_prices.vocabulary import Vocabulary, VocabularyError, load_vocabulary
from stone_pipeline.config.settings import ENV_SEGMENT
from stone_pipeline.io.deliverables import publish_deliverables


def _write(directory: Path, name: str, document: object) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")


# --- brand_vocabulary_isolated: a run reads its own brand's vocabulary and no other ------------------------------------

def test_a_run_loads_its_own_brands_vocabulary(tmp_path):
    _write(tmp_path, "wudport", {"brand": "wudport", "scope": "all"})
    assert load_vocabulary("wudport", tmp_path) == Vocabulary(brand="wudport", scope="all")


def test_a_vocabulary_declaring_another_brand_is_refused(tmp_path):
    _write(tmp_path, "wudport", {"brand": "blokport", "scope": "all"})
    with pytest.raises(VocabularyError, match="declares brand 'blokport'"):
        load_vocabulary("wudport", tmp_path)


def test_a_run_opens_only_its_own_brands_file(tmp_path, monkeypatch):
    _write(tmp_path, "wudport", {"brand": "wudport", "scope": "all"})
    _write(tmp_path, "blokport", {"brand": "blokport", "scope": "all"})
    opened: list[Path] = []
    real_read = Path.read_text

    def recording_read(self, *args, **kwargs):
        opened.append(self)
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", recording_read)
    load_vocabulary("wudport", tmp_path)
    assert opened == [tmp_path / "wudport.yaml"]


@pytest.mark.parametrize("brand, content, message", [
    ("../blokport", None, "must match"),
    ("nobody", None, "no scope vocabulary for brand 'nobody'"),
    ("broken", "brand: [", "is not valid YAML"),
    ("shape", "brand: shape\nscope: all\nunknown_field: 1\n", "does not validate"),
])
def test_a_vocabulary_problem_raises(tmp_path, brand, content, message):
    if content is not None:
        (tmp_path / f"{brand}.yaml").write_text(content, encoding="utf-8")
    with pytest.raises(VocabularyError, match=message):
        load_vocabulary(brand, tmp_path)


def test_the_vocabulary_folder_is_the_packages_own():
    assert config.VOCABULARY_DIR == Path(config.__file__).resolve().parent / "vocabulary"


# --- shared fixtures for the rules below --------------------------------------------------------------------------------

MOMENT = datetime(2026, 11, 8, 12, 30, 5, tzinfo=timezone.utc)
RUN_ID = "20261108T123005Z"


def _row(brand="blokport", site_id="shop", host="shop.example", **overrides) -> dict:
    """A pending register row: it needs no terms, so it isolates the brand rules from the terms gate."""
    row = {"brand": brand, "site_id": site_id, "seller_name": "Shop", "seller_type": "merchant",
           "platform": "woocommerce_store", "base_url": f"https://{host}", "terms": {}, "decision": "pending"}
    return {**row, **overrides}


def _approved(**overrides) -> dict:
    return _row(**{"seller_country": "BE", "served_market": "BE", "decided_by": "owner", "decided_on": "2026-11-08",
                   "terms": {"terms_url": "https://shop.example/terms", "checked_on": "2026-11-02",
                             "summary": "No clause on copying."},
                   "decision": "approved", **overrides})


def _register_file(directory: Path, brand: str, *rows: dict, **top) -> None:
    _write(directory, brand, {"brand": brand, "sites": list(rows), **top})


def _status(brand="blokport", site_id="shop") -> SiteStatus:
    return SiteStatus(contract_version=config.CONTRACT_VERSION, source=Source.site, brand=brand, site_id=site_id,
                      status=SiteRunStatus.disabled_terms, rows=0, malformed_listings=0, duplicate_listings=0,
                      detail="decision is pending", terms_checked_on=None, decision="pending",
                      observed_at="2026-11-08T12:30:05Z", run_id=RUN_ID)


class _Body:
    def __init__(self, data: bytes):
        self.data = data

    def read(self) -> bytes:
        return self.data


class _S3:
    """An in-memory bucket that records every key it is asked for, read or written."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.versions: dict[str, str] = {}
        self.keys: list[str] = []

    def get_object(self, Bucket, Key):
        self.keys.append(Key)
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": _Body(self.objects[Key])}

    def head_object(self, Bucket, Key):
        self.keys.append(Key)
        return {"VersionId": self.versions[Key]}

    def put_object(self, Bucket, Key, Body, ContentType=None, ChecksumSHA256=None):
        self.keys.append(Key)
        self.objects[Key] = Body if isinstance(Body, bytes) else Body.read()
        self.versions[Key] = f"v{len(self.keys)}"
        return {"VersionId": self.versions[Key]}


def _published_run(tmp_path: Path, brand="blokport") -> _S3:
    s3 = _S3()
    result = RunResult(brand=brand, run_id=RUN_ID, observations=(), statuses=(_status(brand),))
    publish_run(s3, "bucket", brand, write_run(result, tmp_path))
    return s3


# --- brand_required: every record carries the run's brand ---------------------------------------------------------------

def test_a_register_row_carries_its_registers_brand(tmp_path):
    _register_file(tmp_path, "blokport", _row())
    assert load_register("blokport", tmp_path).sites[0].brand == "blokport"


@pytest.mark.parametrize("row", [
    {k: v for k, v in _row().items() if k != "brand"},      # no brand
    _row(brand=""),                                          # empty brand
    _row(brand="wudport"),                                   # another brand's row
])
def test_a_register_row_without_the_registers_brand_is_refused(tmp_path, row):
    _register_file(tmp_path, "blokport", row)
    with pytest.raises(RegisterError, match="brand"):
        load_register("blokport", tmp_path)


def test_every_shipped_register_row_carries_its_files_brand():
    for path in sorted(config.SITES_DIR.glob("*.yaml")):
        register = load_register(path.stem)
        assert {site.brand for site in register.sites} <= {path.stem}


@pytest.mark.parametrize("brand", ["", "Blok Port", "../x"])
def test_a_row_with_an_empty_or_malformed_brand_does_not_validate(brand):
    with pytest.raises(ValidationError):
        _status(brand=brand)


def test_write_run_refuses_a_row_of_another_brand(tmp_path):
    result = RunResult(brand="blokport", run_id=RUN_ID, observations=(), statuses=(_status("wudport"),))
    with pytest.raises(rules.BrandError, match="wudport"):
        write_run(result, tmp_path)
    assert not (tmp_path / "blokport").exists()


def test_the_published_manifest_carries_the_runs_brand(tmp_path):
    s3 = _published_run(tmp_path)
    assert json.loads(s3.objects[f"{ENV_SEGMENT}/market_prices/blokport/{RUN_ID}/manifest.json"])["brand"] == "blokport"


def test_the_shared_manifest_carries_the_brand_it_is_given(tmp_path):
    (tmp_path / "a.csv").write_text("x\n1\n", encoding="utf-8")
    s3 = _S3()
    out = publish_deliverables(s3, "bucket", tmp_path, "p", "r1", brand="calcport")
    assert out["manifest"]["brand"] == "calcport"
    assert json.loads(s3.objects["p/manifest.json"])["brand"] == "calcport"


# --- one_register_per_site: a site belongs to one brand unless both declare it shared ---------------------------------

def test_one_host_in_two_registers_undeclared_is_refused(tmp_path):
    _register_file(tmp_path, "blokport", _row(site_id="shop"))
    _register_file(tmp_path, "wudport", _row(brand="wudport", site_id="other_id"))
    with pytest.raises(RegisterError, match="shop.example"):
        load_register("blokport", tmp_path)


def test_one_site_id_in_two_registers_undeclared_is_refused(tmp_path):
    _register_file(tmp_path, "blokport", _row(site_id="shop", host="a.example"))
    _register_file(tmp_path, "wudport", _row(brand="wudport", site_id="shop", host="b.example"))
    with pytest.raises(RegisterError, match="shop"):
        load_register("wudport", tmp_path)


def test_a_site_both_registers_declare_shared_loads(tmp_path):
    _register_file(tmp_path, "blokport", _row(shared_with=["wudport"]))
    _register_file(tmp_path, "wudport", _row(brand="wudport", shared_with=["blokport"]))
    assert load_register("blokport", tmp_path).sites[0].shared_with == ("wudport",)
    assert load_register("wudport", tmp_path).sites[0].shared_with == ("blokport",)


def test_a_site_only_one_register_declares_shared_is_refused(tmp_path):
    _register_file(tmp_path, "blokport", _row(shared_with=["wudport"]))
    _register_file(tmp_path, "wudport", _row(brand="wudport"))
    with pytest.raises(RegisterError, match="shop.example"):
        load_register("blokport", tmp_path)


# --- brand_scoped_output: every key is under <env>/market_prices/<run brand>/ ---------------------------------------------

def test_the_output_key_is_under_the_runs_brand():
    assert rules.output_key("wudport", RUN_ID, "observations.csv") == \
        f"{ENV_SEGMENT}/market_prices/wudport/{RUN_ID}/observations.csv"
    assert rules.output_key("wudport") == f"{ENV_SEGMENT}/market_prices/wudport"


@pytest.mark.parametrize("brand, parts", [
    ("../x", ()), ("wudport", ("",)), ("wudport", ("a/b",)), ("wudport", ("..",)), ("wudport", (".hidden",)),
])
def test_an_output_key_that_could_leave_the_brand_is_refused(brand, parts):
    with pytest.raises(rules.BrandError):
        rules.output_key(brand, *parts)


def test_a_key_outside_the_runs_brand_is_refused():
    rules.require_brand_key("wudport", rules.output_key("wudport", "latest.json"))
    for key in (rules.output_key("blokport", "latest.json"), f"{ENV_SEGMENT}/market_prices/wudportx/latest.json",
                f"{ENV_SEGMENT}/scraper/to_upload/manifest.json"):
        with pytest.raises(rules.BrandError):
            rules.require_brand_key("wudport", key)


def test_a_publish_touches_only_its_brands_keys(tmp_path):
    s3 = _published_run(tmp_path, "wudport")
    assert s3.keys and all(key.startswith(rules.output_key("wudport") + "/") for key in s3.keys)


# --- shared_code_brand_free: no brand name in the shared code; a brand's facts live in its own files -------------------

def _brand_names() -> set[str]:
    repo = Path(config.__file__).resolve().parents[1]
    return {p.stem for p in config.SITES_DIR.glob("*.yaml")} | {p.stem for p in (repo / "infra" / "brands").glob("*.tfvars")}


def test_the_shared_code_holds_no_brand_name():
    package = Path(config.__file__).resolve().parent
    pattern = re.compile(r"\b(" + "|".join(sorted(_brand_names())) + r")\b", re.IGNORECASE)
    hits = [f"{path.relative_to(package)}:{n}: {line.strip()}"
            for path in sorted(package.rglob("*.py"))
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if pattern.search(line)]
    assert hits == []


def test_the_brand_names_cover_every_brand():
    assert {"blokport", "wudport", "calcport"} <= _brand_names()


def test_the_pilot_runs_the_site_its_register_names(tmp_path):
    _register_file(tmp_path, "blokport", _approved(), _row(site_id="other", host="other.example"), pilot_site="shop")
    assert [site.site_id for site in pilot_register("blokport", tmp_path).sites] == ["shop"]


@pytest.mark.parametrize("pilot, message", [(None, "names no pilot_site"), ("other", "not approved")])
def test_a_pilot_without_an_approved_named_site_is_refused(tmp_path, pilot, message):
    top = {} if pilot is None else {"pilot_site": pilot}
    _register_file(tmp_path, "blokport", _approved(), _row(site_id="other", host="other.example"), **top)
    with pytest.raises(RegisterError, match=message):
        pilot_register("blokport", tmp_path)


def test_a_pilot_site_the_register_does_not_hold_is_refused(tmp_path):
    _register_file(tmp_path, "blokport", _approved(), pilot_site="nowhere")
    with pytest.raises(RegisterError, match="nowhere"):
        load_register("blokport", tmp_path)
