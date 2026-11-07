"""C5 pin: ONE country resolver. loaders.resolve_iso is the single implementation; derive and the matcher
reach it through reference.to_iso, and config/server reaches the same answer. This pins, over the real
country vocabulary (country_codes.csv names + codes, origin_map.csv country_iso) plus adversarial inputs,
that all three paths agree for every input -- so collapsing derive._to_iso onto reference.to_iso (A) and
folding server._country_iso onto the shared resolver (B) is behaviour-unchanged.
"""

from __future__ import annotations

import csv

import pytest

from stone_pipeline.config import server
from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.reference import loaders
from stone_pipeline.reference.loaders import resolve_iso

ADVERSARIAL = ["", " ", "UK", "uk", "XX", "EN", "in", "Gb", "India", "BRASIL", "Z", "GBR", "turkey", "TR"]


@pytest.fixture(scope="module")
def ref():
    return loaders.load_all()


def _country_code_rows() -> list[tuple[str, str]]:
    with SETTINGS.paths.country_codes_csv.open(encoding="utf-8-sig", newline="") as h:
        return [(r["name"], r["iso2"]) for r in csv.DictReader(h) if r.get("name")]


def _origin_map_tokens() -> list[str]:
    p = SETTINGS.paths.origin_map_csv
    if not p.exists():
        return []
    toks: list[str] = []
    with p.open(encoding="utf-8-sig", newline="") as h:
        for r in csv.DictReader(h):
            for tok in (r.get("country_iso") or "").split(","):
                if tok.strip():
                    toks.append(tok.strip())
    return toks


def _vocabulary() -> list[str]:
    seen: dict[str, None] = {}
    for name, iso2 in _country_code_rows():
        seen[name] = None
        seen[iso2] = None
    for tok in _origin_map_tokens():
        seen[tok] = None
    for v in ADVERSARIAL:
        seen[v] = None
    return list(seen)


def test_all_three_resolver_paths_agree_over_the_real_vocabulary(ref):
    # reference.to_iso (the one binder), the pre-C5 derive._to_iso binding (resolve_iso with the ref's
    # fields), and server._country_iso must give the identical answer for every real and adversarial input.
    vocab = _vocabulary()
    assert len(vocab) > 100                                   # the real country table is loaded, not empty
    for v in vocab:
        new = ref.to_iso(v)
        old_derive = resolve_iso(v, ref.country_codes, ref.valid_iso_codes)
        srv = server._country_iso(v)
        assert new == old_derive == srv, f"resolver disagreement on {v!r}: {new!r} / {old_derive!r} / {srv!r}"


def test_every_country_name_resolves_to_its_listed_code(ref):
    # the country table's own contract: each name resolves to its iso2, and each iso2 passes through.
    for name, iso2 in _country_code_rows():
        assert ref.to_iso(name) == iso2
        assert ref.to_iso(iso2) == iso2


def test_behaviour_anchors(ref):
    assert ref.to_iso("India") == "IN"
    assert ref.to_iso("UK") == "GB"                           # alias via the name table, not a literal 'UK'
    assert ref.to_iso("XX") is None                           # a bogus 2-letter token is rejected
    assert ref.to_iso("") is None and ref.to_iso("  ") is None
