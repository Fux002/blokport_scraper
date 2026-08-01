"""T3-5: the identity-dedup survivor must be chosen among ELIGIBLE keys only.

survivor_of is blind to the exclusion predicates (bare_code / mistyped / delete_keys). If it picked an
INELIGIBLE key (a re-key OLD SIDE listed in variants_to_delete), every eligible key of that identity was
dropped as a dup_loser AND the chosen survivor dropped by its own exclusion -- the whole variety vanished
from the matcher reference, so a scrape for it matched nothing and minted a DUPLICATE.
"""

from __future__ import annotations

import csv

from stone_pipeline.reference import loaders


def _variants_csv(tmp_path, rows):        # rows: (Id, Key, Name)
    p = tmp_path / "variants.csv"
    with p.open("w", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        w.writerow(["Id", "Key", "Name", "Image", "Aliases", "Volume per kg (m³/kg)"])
        for vid, key, nm in rows:
            w.writerow([vid, key, nm, "", "", ""])
    return p


def test_rekey_old_side_survivor_does_not_strand_the_new_key(tmp_path, monkeypatch):
    old_key = "slab_marble_super_white_0001"     # re-key OLD SIDE, in variants_to_delete, lexically smaller
    new_key = "slab_marble_super_white_0002"     # the good NEW key (same identity: same type + name)
    monkeypatch.setattr(loaders, "_delete_keys", lambda: {old_key})
    monkeypatch.setattr("stone_pipeline.stages.decisions.load_retired", lambda: set())
    monkeypatch.setattr(loaders, "pristine_seed_keys", lambda: frozenset())   # neither key is seeded

    path = _variants_csv(tmp_path, [("id_old", old_key, "Super White"),
                                    ("id_new", new_key, "Super White")])
    table = loaders.load_variants(path, branch="slab")

    # the good new key survives (its product can resolve) and the deleted old side is excluded
    assert "id_new" in table.by_id, "the eligible new key must survive, not vanish with the deleted old side"
    assert table.by_id["id_new"].key == new_key
    assert "id_old" not in table.by_id


def test_normal_duplicate_still_collapses_to_one_survivor(tmp_path, monkeypatch):
    # regression: two eligible duplicates still collapse to exactly one (lexically-smaller slug survives)
    monkeypatch.setattr(loaders, "_delete_keys", lambda: set())
    monkeypatch.setattr("stone_pipeline.stages.decisions.load_retired", lambda: set())
    monkeypatch.setattr(loaders, "pristine_seed_keys", lambda: frozenset())
    path = _variants_csv(tmp_path, [("id_a", "slab_marble_carrara_0001", "Carrara"),
                                    ("id_b", "slab_marble_carrara_0002", "Carrara")])
    table = loaders.load_variants(path, branch="slab")
    kept = [vid for vid in ("id_a", "id_b") if vid in table.by_id]
    assert kept == ["id_a"]      # exactly one survivor, the deterministic pick
