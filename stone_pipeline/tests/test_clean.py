"""Housekeeping: `clean` removes superseded scrapes and run folders but ALWAYS keeps the latest
per source, so the pipeline never loses an input and no stale snapshot lingers."""

from __future__ import annotations

from stone_pipeline import clean


def _touch(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x", encoding="utf-8")


def test_clean_keeps_latest_per_source(tmp_path):
    data, out = tmp_path / "data", tmp_path / "out"
    # source a: an old + a newer scrape; source b: a single scrape
    _touch(data / "a" / "20260101_000000" / "products.csv")
    _touch(data / "a" / "20260102_000000" / "products.csv")
    _touch(data / "b" / "20260101_000000" / "products.csv")
    # source a: an old + a newer run folder
    (out / "a_20260101_000000" / "diagnostics").mkdir(parents=True)
    (out / "a_20260102_000000" / "diagnostics").mkdir(parents=True)

    dry = clean.run(dry_run=True, data_dir=data, outputs=out)
    assert dry["superseded_scrapes"] == 1 and dry["superseded_runs"] == 1
    assert (data / "a" / "20260101_000000").exists()        # dry-run removed nothing

    counts = clean.run(data_dir=data, outputs=out)
    assert counts["superseded_scrapes"] == 1 and counts["superseded_runs"] == 1
    assert not (data / "a" / "20260101_000000").exists()    # superseded gone
    assert (data / "a" / "20260102_000000").exists()        # latest kept
    assert (data / "b" / "20260101_000000").exists()        # single scrape untouched
    assert (out / "a_20260102_000000").exists() and not (out / "a_20260101_000000").exists()


def test_clean_prunes_orphan_images_only(tmp_path):
    # an image whose variant Key left the catalog is orphan; one whose Key is still live is kept;
    # an empty catalog prunes NOTHING (never treat a missing catalog as 'all orphaned').
    ip = tmp_path / "image_pipeline"
    for sub in ("images", "to_upload"):
        (ip / sub).mkdir(parents=True)
        (ip / sub / "slab_live_K1.png").write_bytes(b"x")
        (ip / sub / "slab_gone_K9.png").write_bytes(b"x")
    assert clean._orphan_images(ip, set()) == []                        # safety guard
    orphans = clean._orphan_images(ip, {"slab_live_K1"})
    assert {p.name for p in orphans} == {"slab_gone_K9.png"} and len(orphans) == 2
    counts = clean.run(data_dir=tmp_path / "data", outputs=tmp_path / "out",
                       image_pipeline_root=ip, catalog_keys={"slab_live_K1"})
    assert counts["orphan_images"] == 2
    for sub in ("images", "to_upload"):
        assert (ip / sub / "slab_live_K1.png").exists()                 # live kept
        assert not (ip / sub / "slab_gone_K9.png").exists()             # orphan gone
