"""The competitor-price path's live pieces, on stubs only: the HTTP transport (driven through httpx's MockTransport, so
no request leaves the process), the run entry, the dev pilot and the RUN_MODE dispatch. Nothing here reaches a site."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from market_prices import config, pilot, run
from market_prices.fetching import Response, TransportError, UnexpectedResponse
from market_prices.register import Register, RegisterError
from market_prices.transport import LiveTransport
from stone_pipeline.io import ssrf

MOMENT = datetime(2026, 11, 8, 12, 30, 5, tzinfo=timezone.utc)
BASE = "https://shop.example"
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def public_hosts(monkeypatch):
    """Example hosts count as public and any host named internal does not: the SSRF check runs, without DNS."""
    checked: list[str] = []

    def allowed(url: str) -> bool:
        checked.append(url)
        return "internal" not in url
    monkeypatch.setattr(ssrf, "url_allowed", allowed)
    return checked


class _Server:
    """httpx's in-process MockTransport answering from a script; records every request it saw."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        answer = self.answers[str(request.url)]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _transport(answers: dict) -> tuple[LiveTransport, _Server]:
    server = _Server(answers)
    return LiveTransport(http_transport=httpx.MockTransport(server)), server


# --- the live transport ------------------------------------------------------------------------------------------------
def test_one_get_with_our_honest_name_and_the_answer_as_it_came():
    transport, server = _transport({f"{BASE}/a": httpx.Response(200, text="hello", headers={"X-WP-TotalPages": "3"})})
    with transport:
        answer = transport.get(f"{BASE}/a")
    assert (answer.status, answer.text, answer.header("x-wp-totalpages")) == (200, "hello", "3")
    assert [(r.method, r.headers["user-agent"]) for r in server.seen] == [("GET", config.USER_AGENT)]


def test_a_refusal_comes_back_as_an_answer_once_never_retried():
    transport, server = _transport({f"{BASE}/a": httpx.Response(429, text="slow down")})
    with transport:
        assert transport.get(f"{BASE}/a").status == 429
    assert len(server.seen) == 1


def test_every_redirect_hop_is_checked_and_followed(public_hosts):
    transport, server = _transport({
        f"{BASE}/a": httpx.Response(301, headers={"location": "/b"}),
        f"{BASE}/b": httpx.Response(302, headers={"location": "https://cdn.shop.example/c"}),
        "https://cdn.shop.example/c": httpx.Response(200, text="final")})
    with transport:
        assert transport.get(f"{BASE}/a").text == "final"
    assert public_hosts == [str(r.url) for r in server.seen] == [f"{BASE}/a", f"{BASE}/b", "https://cdn.shop.example/c"]


def test_a_redirect_to_a_non_public_host_is_never_requested():
    transport, server = _transport({f"{BASE}/a": httpx.Response(302, headers={"location": "http://internal.example/x"})})
    with transport, pytest.raises(UnexpectedResponse, match="internal.example"):
        transport.get(f"{BASE}/a")
    assert [str(r.url) for r in server.seen] == [f"{BASE}/a"]


def test_a_redirect_loop_is_capped():
    transport, server = _transport({f"{BASE}/a": httpx.Response(301, headers={"location": "/a"})})
    with transport, pytest.raises(UnexpectedResponse, match="too many redirects"):
        transport.get(f"{BASE}/a")
    assert len(server.seen) == ssrf.MAX_REDIRECTS + 1


def test_an_over_size_body_is_unexpected(monkeypatch):
    monkeypatch.setattr(config, "MAX_RESPONSE_BYTES", 10)
    # chunks, so no Content-Length is declared: the cap is met while the body streams in
    transport, _ = _transport({f"{BASE}/a": httpx.Response(200, content=iter([b"x" * 6, b"x" * 5]))})
    with transport, pytest.raises(UnexpectedResponse, match="over 10 bytes"):
        transport.get(f"{BASE}/a")


def test_a_body_at_the_cap_is_read(monkeypatch):
    monkeypatch.setattr(config, "MAX_RESPONSE_BYTES", 10)
    transport, _ = _transport({f"{BASE}/a": httpx.Response(200, content=b"x" * 10)})
    with transport:
        assert transport.get(f"{BASE}/a").text == "x" * 10


def test_a_declared_over_size_body_is_refused_unread(monkeypatch):
    monkeypatch.setattr(config, "MAX_RESPONSE_BYTES", 10)

    def body():
        raise AssertionError("the body was read")
        yield b""   # a generator, read only if the transport iterates it
    transport, _ = _transport({f"{BASE}/a": httpx.Response(200, headers={"content-length": "999"}, content=body())})
    with transport, pytest.raises(UnexpectedResponse, match="declares 999 bytes"):
        transport.get(f"{BASE}/a")


@pytest.mark.parametrize("failure", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"),
                                     httpx.RemoteProtocolError("cut")])
def test_no_answer_is_a_transport_error_and_is_not_retried(failure):
    transport, server = _transport({f"{BASE}/a": failure})
    with transport, pytest.raises(TransportError):
        transport.get(f"{BASE}/a")
    assert len(server.seen) == 1


def test_the_client_never_follows_redirects_itself_nor_reads_proxy_settings(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal.example:3128")
    with LiveTransport() as transport:
        client = transport._client
        assert client.follow_redirects is False and client.trust_env is False
        assert client.timeout.read == config.FETCH_TIMEOUT_SECONDS


def test_a_body_that_trickles_past_the_deadline_is_unexpected(monkeypatch):
    # each read is inside httpx's per-operation timeout, yet the request as a whole is held too long
    monkeypatch.setattr(config, "FETCH_DEADLINE_SECONDS", 10.0)
    ticks = iter(range(0, 1000, 4))                     # every clock read is 4 s later

    def trickle():
        for _ in range(10):
            yield b"x"
    server = _Server({f"{BASE}/a": httpx.Response(200, content=trickle())})
    transport = LiveTransport(http_transport=httpx.MockTransport(server), clock=lambda: float(next(ticks)))
    with transport, pytest.raises(UnexpectedResponse, match="deadline"):
        transport.get(f"{BASE}/a")


def test_a_start_url_that_is_not_public_says_so():
    transport, server = _transport({})
    with transport, pytest.raises(UnexpectedResponse, match="internal.example/x is not a public"):
        transport.get("http://internal.example/x")
    assert server.seen == []


def test_the_body_is_decoded_by_its_declared_charset():
    transport, _ = _transport({f"{BASE}/a": httpx.Response(
        200, content="Prijs € 5".encode("cp1252"), headers={"content-type": "text/html; charset=windows-1252"})})
    with transport:
        assert transport.get(f"{BASE}/a").text == "Prijs € 5"


# --- the run entry -----------------------------------------------------------------------------------------------------
from stone_pipeline.tests.test_market_prices import (   # noqa: E402  (the delivery fixtures, shared with the core tests)
    _S3, _listing, _page, _register, _shop, _site,
)


def _no_pause(_seconds: float) -> None:
    return None


class _Refuses:
    """A transport that fails the test if it is asked for anything."""

    def get(self, url: str) -> Response:
        raise AssertionError(f"requested {url}")


def test_a_register_with_no_approved_site_runs_cleanly_and_publishes_only_evidence(tmp_path):
    s3 = _S3()
    pending = Register.model_validate({"brand": "blokport", "sites": [_site(decision="pending")]})
    outcome = run.run_register(pending, _Refuses(), s3, "bucket", tmp_path, MOMENT, dry_run=False, pause=_no_pause)
    assert [status.status.value for status in outcome.result.statuses] == ["disabled_terms"]
    prefix = "dev/market_prices/blokport"
    assert sorted(s3.objects) == [f"{prefix}/20261108T123005Z/manifest.json",
                                  f"{prefix}/20261108T123005Z/observations.csv",
                                  f"{prefix}/20261108T123005Z/site_status.csv"]
    assert outcome.published["manifest"]["files"]["observations.csv"]["rows"] == 0
    assert outcome.published["latest_moved"] is False


@pytest.mark.parametrize("brand", ["blokport", "wudport"])
def test_the_shipped_registers_approve_no_site_yet_so_a_run_requests_nothing(brand, tmp_path):
    outcome = run.run_register(run.load_register(brand), _Refuses(), _S3(), "bucket", tmp_path, MOMENT, dry_run=True,
                               pause=_no_pause)
    # every listed site is skipped for want of approval (wudport lists none yet), and nothing is requested
    assert {status.status.value for status in outcome.result.statuses} <= {"disabled_terms"}
    assert outcome.result.observations == ()


def test_an_approved_site_is_collected_published_and_moves_latest(tmp_path):
    s3 = _S3()
    outcome = run.run_register(_register(), _shop(_page([_listing()])), s3, "bucket", tmp_path, MOMENT,
                               dry_run=False, pause=_no_pause)
    assert outcome.published["latest_moved"] is True
    assert outcome.run_dir == tmp_path / "blokport" / "20261108T123005Z"
    assert json.loads(s3.objects["dev/market_prices/blokport/latest.json"])["rows"] == 1


def test_the_s3_dry_run_writes_the_folder_and_uploads_nothing(tmp_path):
    s3 = _S3()
    outcome = run.run_register(_register(), _shop(_page([_listing()])), s3, "bucket", tmp_path, MOMENT,
                               dry_run=True, pause=_no_pause)
    assert outcome.published is None and s3.order == []
    assert (outcome.run_dir / config.OBSERVATIONS_FILE).is_file()


@pytest.mark.parametrize("variable", ["SCRAPER_MARKET_PRICES_BRAND", "BLOKPORT_MARKET_PRICES_BRAND"])
def test_main_runs_the_run_inputs_brand_not_the_deployments_and_publishes_under_it(variable, monkeypatch, tmp_path):
    # this deployment is blokport's (the dev default); the run input names wudport, so wudport's register is run and
    # its keys land under wudport's prefix in this deployment's bucket: the one shared runner
    loaded: list[str] = []
    s3 = _S3()

    def load(brand: str) -> Register:
        loaded.append(brand)
        return Register.model_validate({"brand": brand, "sites": [_site()]})
    monkeypatch.setenv(variable, "wudport")
    monkeypatch.setattr(run, "_dry_run", lambda: False)
    monkeypatch.setattr(run, "_s3_client", lambda: s3)
    monkeypatch.setattr(run, "_runs_root", lambda: tmp_path)
    monkeypatch.setattr(run, "load_register", load)
    monkeypatch.setattr(run, "LiveTransport", lambda: _Context(_shop(_page([_listing()]))))
    monkeypatch.setattr(run, "_now", lambda: MOMENT)
    monkeypatch.setattr(run.time, "sleep", lambda _seconds: None)
    run.main()
    assert loaded == ["wudport"]
    assert "dev/market_prices/wudport/latest.json" in s3.objects
    assert not any(key.startswith("dev/market_prices/blokport/") for key in s3.objects)


def test_a_run_without_the_brand_input_stops_before_any_register_or_request(monkeypatch):
    monkeypatch.setattr(run, "load_register", lambda brand: pytest.fail(f"the {brand} register was read"))
    monkeypatch.setattr(run, "LiveTransport", lambda: pytest.fail("a transport was built"))
    with pytest.raises(SystemExit, match="SCRAPER_MARKET_PRICES_BRAND"):
        run.main()


def test_an_unknown_run_brand_stops_the_run_before_any_request(monkeypatch):
    monkeypatch.setenv("SCRAPER_MARKET_PRICES_BRAND", "no_such_brand")
    monkeypatch.setattr(run, "LiveTransport", lambda: pytest.fail("a transport was built"))
    with pytest.raises(RegisterError, match="no site register for brand 'no_such_brand'"):
        run.main()


class _Context:
    def __init__(self, transport):
        self.transport = transport

    def __enter__(self):
        return self.transport

    def __exit__(self, *exc_info):
        return None


# --- the dev pilot (its site choice: tests/test_market_prices_brand.py, the register's pilot_site) ---------------------

def test_the_pilot_refuses_to_run_outside_development(monkeypatch):
    monkeypatch.setattr(pilot, "IS_PRODUCTION", True)
    monkeypatch.setattr(pilot, "pilot_register", lambda *args: pytest.fail("the register was read"))
    with pytest.raises(SystemExit, match="development"):
        pilot.main()


def test_the_pilot_summary_names_status_rows_malformed_and_the_first_rows(tmp_path):
    listings = [_listing(n) for n in range(1, 8)] + [{"id": 99}]          # 7 good listings and one without a name
    outcome = run.run_register(_register(), _shop(_page(listings)), _S3(), "bucket", tmp_path, MOMENT,
                               dry_run=True, pause=_no_pause)
    text = run.summary(outcome, sample=config.PILOT_SAMPLE_ROWS)
    assert "shop: partial, 7 rows, 1 malformed" in text
    shown = [line for line in text.splitlines() if line.startswith("  ")]
    assert len(shown) == config.PILOT_SAMPLE_ROWS and "Sill & step 1" in shown[0] and "21.25 EUR" in shown[0]
    assert "published: no (S3 dry run)" in text


def test_the_pilot_runs_the_run_inputs_brand_not_the_deployments(monkeypatch, tmp_path):
    # this deployment is blokport's (the dev default); the run input names wudport, so wudport's pilot site is run
    piloted: list[str] = []

    def cut(brand: str) -> Register:
        piloted.append(brand)
        return Register.model_validate({"brand": brand, "sites": [_site()]})
    monkeypatch.setenv("SCRAPER_MARKET_PRICES_BRAND", "wudport")
    monkeypatch.setattr(pilot, "pilot_register", cut)
    monkeypatch.setattr(pilot.run, "_dry_run", lambda: True)
    monkeypatch.setattr(pilot.run, "_runs_root", lambda: tmp_path)
    monkeypatch.setattr(pilot.run, "LiveTransport", lambda: _Context(_shop(_page([_listing()]))))
    monkeypatch.setattr(pilot.run, "_now", lambda: MOMENT)
    monkeypatch.setattr(pilot.run.time, "sleep", lambda _seconds: None)
    pilot.main()
    assert piloted == ["wudport"]


def test_a_pilot_without_the_brand_input_stops_before_any_register_or_request(monkeypatch):
    monkeypatch.setattr(pilot, "pilot_register", lambda brand: pytest.fail(f"the {brand} register was read"))
    monkeypatch.setattr(pilot.run, "LiveTransport", lambda: pytest.fail("a transport was built"))
    with pytest.raises(SystemExit, match="SCRAPER_MARKET_PRICES_BRAND"):
        pilot.main()


def test_the_pilot_publishes_to_the_deployments_bucket(monkeypatch, tmp_path, capsys):
    s3 = _S3()
    monkeypatch.setenv("SCRAPER_MARKET_PRICES_BRAND", "blokport")
    monkeypatch.setattr(pilot, "pilot_register", lambda brand: _register())
    monkeypatch.setattr(pilot.run, "_dry_run", lambda: False)
    monkeypatch.setattr(pilot.run, "_s3_client", lambda: s3)
    monkeypatch.setattr(pilot.run, "_runs_root", lambda: tmp_path)
    monkeypatch.setattr(pilot.run, "LiveTransport", lambda: _Context(_shop(_page([_listing()]))))
    monkeypatch.setattr(pilot.run, "_now", lambda: MOMENT)
    monkeypatch.setattr(pilot.run.time, "sleep", lambda _seconds: None)
    pilot.main()
    assert "dev/market_prices/blokport/latest.json" in s3.objects
    assert "shop: ok, 1 rows, 0 malformed" in capsys.readouterr().out


# --- RUN_MODE ----------------------------------------------------------------------------------------------------------
def test_run_mode_market_prices_runs_the_run_entry(tmp_path):
    fake = tmp_path / "python"
    fake.write_text('#!/bin/sh\necho "python $*"\n')
    fake.chmod(0o755)
    done = subprocess.run(["bash", str(REPO / "deploy" / "run_pipeline.sh")], capture_output=True, text=True,
                          env={"PATH": f"{tmp_path}:/usr/bin:/bin", "RUN_MODE": "market-prices"}, timeout=30)
    assert done.returncode == 0 and done.stdout.strip().splitlines()[-1] == "python -m market_prices.run"


def test_an_unknown_run_mode_names_market_prices_among_the_modes():
    done = subprocess.run(["bash", str(REPO / "deploy" / "run_pipeline.sh")], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "RUN_MODE": "nope"}, timeout=30)
    assert done.returncode == 2 and "market-prices" in done.stderr
