"""The GPU reprocess drives EVERY scraped image to a terminal state so the pipeline converges: a completing
image is published (improved + enhanced marker = READY); one that cannot complete after bounded retries is
written to the discarded pool as a terminal HELD state (never left markerless to re-serve / spin the
`generating` indicator forever). These tests pin enhance_one() with the processor + S3 client mocked.
"""

from __future__ import annotations

import json

from deploy import reprocess_source as rs
from stone_pipeline.io import imagestore
from stone_pipeline.io.image_processing import ProcessResult

SHA = "a" * 64
SRC = "varsha"
DST = imagestore.improved_prefix(SRC)
NAME = f"{SHA}.jpg"


class FakeProc:
    """Returns a queued ProcessResult per process() call (last one repeats)."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def process(self, data, *, watermarked, enhance):
        r = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return r


class FakeClient:
    def __init__(self):
        self.puts = {}   # Key -> Body

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.puts[Key] = Body


def _complete():
    return ProcessResult(b"img", dewatermarked=True, enhanced=True, upscaled=True, billed_mp=1)


def _incomplete():                      # upscale failed -> is_complete(enhance) is False
    return ProcessResult(b"img", dewatermarked=True, enhanced=True, upscaled=False, billed_mp=1)


def _run(proc, client, max_attempts=2):
    return rs.enhance_one(client, proc, SRC, NAME, SHA, DST, b"raw",
                          watermarked=True, enhance=True, price=0.05, max_attempts=max_attempts)


def test_publishes_when_complete():
    proc, client = FakeProc([_complete()]), FakeClient()
    outcome, was_dw, cost = _run(proc, client)
    assert outcome == "enhanced" and was_dw is True
    assert f"{DST}{NAME}" in client.puts                       # improved image
    assert imagestore.enhanced_key(SRC, SHA) in client.puts     # ready marker
    assert imagestore.discarded_key(SRC, SHA) not in client.puts
    assert proc.calls == 1                                       # no needless retry on success


def test_terminal_held_after_retries():
    proc, client = FakeProc([_incomplete()]), FakeClient()     # never completes
    outcome, was_dw, cost = _run(proc, client, max_attempts=2)
    assert outcome == "held" and was_dw is False
    assert proc.calls == 2                                       # bounded retries exhausted
    assert imagestore.enhanced_key(SRC, SHA) not in client.puts  # never marked ready
    assert f"{DST}{NAME}" not in client.puts                     # never published
    marker_key = imagestore.discarded_key(SRC, SHA)
    assert marker_key in client.puts                             # terminal HELD marker in the discarded pool
    body = json.loads(client.puts[marker_key])
    assert body["reason"] == "dewatermark_unrecoverable" and body["attempts"] == 2
    assert cost == 2 * 1 * 0.05                                  # billed per attempt


def test_retry_recovers():
    proc, client = FakeProc([_incomplete(), _complete()]), FakeClient()
    outcome, _, _ = _run(proc, client, max_attempts=2)
    assert outcome == "enhanced"
    assert proc.calls == 2
    assert imagestore.enhanced_key(SRC, SHA) in client.puts
    assert imagestore.discarded_key(SRC, SHA) not in client.puts
