"""The durable best-model marker of the variant textures. Only a truly absent marker reads as empty (a first run);
one that exists but cannot be read raises, and is never rewritten: read as empty, the next save would wipe it and
the upgrade drip would pay again for every texture already made by the best model. Two texture jobs can run at once
(the dispatch is fire-and-forget and the GPU pool holds two), so a mark is a conditional write: a job whose read was
overtaken by the other's write merges again, and no job's Keys are ever lost."""

from __future__ import annotations

import json
import types

import pytest
from botocore.exceptions import EndpointConnectionError

from stone_pipeline import refresh_images as ri
from stone_pipeline.tests._marker_s3 import MarkerS3, client_error

# icon pair ids (`{Key}/{category}`), what the best-model marker holds (spec section 4)
P0 = "granite__absolute_black_7f3c0b6e-1111-5222-8333-944455556666/slab"
P1 = "granite__absolute_black_7f3c0b6e-1111-5222-8333-944455556666/block"
P2 = "granite__absolute_black_7f3c0b6e-1111-5222-8333-944455556666/tile"
P3 = "marble__carrara_00000000-1111-5222-8333-944455556666/slab"
PA = "marble__carrara_00000000-1111-5222-8333-944455556666/block"
PB = "marble__carrara_00000000-1111-5222-8333-944455556666/tile"


@pytest.fixture
def s3_on(monkeypatch):
    monkeypatch.setattr(ri, "SETTINGS", types.SimpleNamespace(
        s3=types.SimpleNamespace(bucket="b", dry_run=False),
        images=types.SimpleNamespace(marker_write_attempts=3)))


def test_the_marker_reads_its_keys():
    assert ri.load_refreshed(MarkerS3([P1, P2])) == {P1, P2}


def test_only_an_absent_marker_reads_as_empty():
    assert ri.load_refreshed(MarkerS3()) == set()                                   # a first run


@pytest.mark.parametrize("s3", [
    MarkerS3(error=client_error("AccessDenied")),
    MarkerS3(error=EndpointConnectionError(endpoint_url="https://s3")),
    MarkerS3(body=b"not json at all"),
    MarkerS3(body=json.dumps({"not": "a list"}).encode()),
], ids=["denied", "unreachable", "corrupt", "not-a-list"])
def test_a_marker_that_exists_but_cannot_be_read_raises(s3):
    with pytest.raises(ri.MarkerUnreadable):
        ri.load_refreshed(s3)


def test_an_unreadable_marker_is_never_rewritten(s3_on):
    s3 = MarkerS3(error=client_error("AccessDenied"))
    assert ri.mark_best_model([P3], client=s3) is False
    assert s3.puts == []                                                            # not wiped to {P3}


def test_marking_merges_into_the_existing_marker(s3_on):
    s3 = MarkerS3([P1])
    assert ri.mark_best_model([P2], client=s3) is True
    assert s3.puts == [[P1, P2]]


@pytest.mark.parametrize("start", [None, [P0]], ids=["first-run", "existing"])
def test_two_interleaved_jobs_both_keep_their_keys(s3_on, start):
    # job A reads the marker, job B marks in full, then A writes: A's write was overtaken, so it merges again
    s3 = MarkerS3(start)
    s3.after_read = lambda: ri.mark_best_model([PB], client=s3)
    assert ri.mark_best_model([PA], client=s3) is True
    assert s3.keys() == set(start or []) | {PA, PB}


@pytest.mark.parametrize("code", ["PreconditionFailed", "ConditionalRequestConflict"])
def test_a_write_that_never_wins_gives_up_after_the_set_attempts(s3_on, code):
    s3 = MarkerS3([P0], refuse=code)
    reads = []
    get = s3.get_object
    s3.get_object = lambda **kw: reads.append(1) or get(**kw)
    assert ri.mark_best_model([PA], client=s3) is False                            # the job reports it as partial
    assert len(reads) == 3 and s3.keys() == {P0}


def test_a_write_refused_for_another_reason_is_not_retried(s3_on):
    s3 = MarkerS3([P0], refuse="AccessDenied")
    assert ri.mark_best_model([PA], client=s3) is False
    assert s3.keys() == {P0}


def test_marking_writes_nothing_under_dry_run(monkeypatch):
    monkeypatch.setattr(ri, "SETTINGS", types.SimpleNamespace(
        s3=types.SimpleNamespace(bucket="b", dry_run=True), images=types.SimpleNamespace(marker_write_attempts=3)))
    s3 = MarkerS3([P0])
    assert ri.mark_best_model([PA], client=s3) is False
    assert s3.puts == []


def test_marking_is_a_noop_without_s3(monkeypatch, s3_on):
    monkeypatch.setattr(ri, "_s3_client", lambda: None)
    assert ri.mark_best_model([PA]) is False
