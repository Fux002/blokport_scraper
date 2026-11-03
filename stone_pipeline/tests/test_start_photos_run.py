"""The overnight press of the photos run: it posts the photos stage with the token, and its exit code says whether a run
started, one was under way, it was not configured, or the API gave another answer or none."""
from __future__ import annotations

import httpx
import pytest

from deploy import start_photos_run as press_mod


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("SCRAPER_CONFIG_URL", "http://scraper.ns:8724/")
    monkeypatch.setenv("SCRAPER_CONFIG_TOKEN", "t0k")


def _answer(status, body=None, text=""):
    calls = []

    def post(url, json, headers):
        calls.append((url, json, headers))
        return httpx.Response(status, json=body) if body is not None else httpx.Response(status, text=text)
    return post, calls


def test_it_posts_the_photos_stage_with_the_token_and_a_started_run_is_0(configured, capsys):
    post, calls = _answer(202, {"run_id": "R1", "status": "queued", "stage": "photos"})
    assert press_mod.press(post) == 0
    assert calls == [("http://scraper.ns:8724/config/v1/run", {"stage": "photos"}, {"Authorization": "Bearer t0k"})]
    assert "photos run R1 started" in capsys.readouterr().out


def test_a_run_under_way_is_3_and_named(configured, capsys):
    post, _ = _answer(409, {"run_id": "R0", "status": "running", "stage": "all"})
    assert press_mod.press(post) == 3
    assert "the all run R0 is under way (running)" in capsys.readouterr().out


def test_a_slot_held_by_an_operation_is_3_with_its_reason(configured, capsys):
    post, _ = _answer(409, {"error": "a reset is in progress; refusing to run"})
    assert press_mod.press(post) == 3
    assert "not started: a reset is in progress; refusing to run" in capsys.readouterr().out


def test_any_other_answer_is_1(configured, capsys):
    post, _ = _answer(500, text="boom")
    assert press_mod.press(post) == 1
    assert "answered 500: boom" in capsys.readouterr().out


def test_no_answer_is_1(configured, capsys):
    def post(url, json, headers):
        raise httpx.ConnectError("refused")
    assert press_mod.press(post) == 1
    assert "did not answer (ConnectError: refused)" in capsys.readouterr().out


@pytest.mark.parametrize("missing", ["CONFIG_URL", "CONFIG_TOKEN"])
def test_unconfigured_is_2_and_posts_nothing(configured, monkeypatch, missing):
    monkeypatch.delenv(f"SCRAPER_{missing}")
    monkeypatch.delenv(f"BLOKPORT_{missing}", raising=False)
    post, calls = _answer(202, {})
    assert press_mod.press(post) == 2 and calls == []
