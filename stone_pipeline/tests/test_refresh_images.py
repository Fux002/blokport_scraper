"""The durable best-model marker of the variant textures: tolerant of an absent or corrupt marker, and a no-op
without S3."""

from __future__ import annotations

import json

from stone_pipeline import refresh_images as ri


def test_parse_marker_is_tolerant():
    assert ri.parse_marker(json.dumps(["k1", "k2"])) == {"k1", "k2"}
    assert ri.parse_marker(b"") == set()                        # no marker yet -> first run
    assert ri.parse_marker("not json at all") == set()          # corrupt -> eligible, not a crash
    assert ri.parse_marker(json.dumps({"not": "a list"})) == set()


def test_save_refreshed_is_a_noop_without_s3(monkeypatch):
    # no client -> nothing persisted (a dry run / CI never writes a marker), reported as False
    assert ri.save_refreshed({"k1"}, client=None) is False
    assert ri.load_refreshed(client=None) == set()
