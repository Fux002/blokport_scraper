"""FND-04: the committed base seed (variants_export_base.csv) is a source-of-truth file; it must live
under the env's from_medusa folder (from_medusa/<env>/), like every other Medusa file, so a PRODUCTION
run does not read/write it under the dev folder. Pins the path to the env-derived from_medusa_dir."""

from __future__ import annotations

import csv

from stone_pipeline.config.settings import ENV_NAME, SETTINGS
from stone_pipeline.reference import sync_variants_base


def _write_variants(path, n):
    with path.open("w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=sync_variants_base.COLS)
        w.writeheader()
        for i in range(n):
            w.writerow({c: (f"slab_marble_v{i}_{i:04d}" if c == "Key" else "") for c in sync_variants_base.COLS})


def test_shrink_guard_refuses_a_thin_produce(tmp_path):
    # A produce that collapses to a fraction of the base (a thin live export on a cold start) must NOT
    # overwrite the source of truth -- otherwise the full catalog is silently wiped and a cold start
    # can never rebuild it. The base is left exactly as it was.
    base, full = tmp_path / "base.csv", tmp_path / "full.csv"
    _write_variants(base, 1000)
    _write_variants(full, 3)
    result = sync_variants_base.sync(full_path=full, base_path=base, publish=False)
    assert result["skipped"] and result["guarded"]
    assert sync_variants_base._row_count(base) == 1000


def test_sync_writes_when_the_set_grows_or_holds(tmp_path):
    # normal churn -- growth (or a small retire) -- writes through as before.
    base, full = tmp_path / "base.csv", tmp_path / "full.csv"
    _write_variants(base, 1000)
    _write_variants(full, 1200)
    result = sync_variants_base.sync(full_path=full, base_path=base, publish=False)
    assert not result["skipped"] and result["base_rows"] == 1200
    assert sync_variants_base._row_count(base) == 1200


def test_base_seed_path_follows_the_env_folder():
    # the base seed tracks from_medusa_dir (same mechanism as variants_export.csv / attributes.csv),
    # NOT a hardcoded 'development' -- so dev and prod each seed from their own base.
    assert sync_variants_base.BASE == SETTINGS.paths.from_medusa_dir / "variants_export_base.csv"
    assert sync_variants_base.BASE.parent == SETTINGS.paths.from_medusa_dir
    # and from_medusa_dir is genuinely env-derived (ends in ENV_NAME), so the pin above is per-env,
    # not a fixed literal that would send a prod run to the wrong folder.
    assert SETTINGS.paths.from_medusa_dir.name == ENV_NAME


def test_sync_publishes_base_to_s3_as_single_source_of_truth(tmp_path, monkeypatch):
    # S0: the written base is uploaded to S3 so it is the SINGLE source of truth -- a container's local base
    # can never silently drift from S3 (the divergence that shipped varieties in one category only).
    base, full = tmp_path / "base.csv", tmp_path / "full.csv"
    _write_variants(full, 50)
    calls = []
    class FakeS3:
        def upload_file(self, src, bucket, key):
            calls.append((src, bucket, key))
    monkeypatch.setattr("boto3.client", lambda service, region_name=None: FakeS3())
    sync_variants_base.sync(full_path=full, base_path=base, publish=True)
    assert len(calls) == 1
    src, _bucket, key = calls[0]
    assert src == str(base) and key.endswith("/from_medusa/base.csv")


def test_sync_publish_false_never_touches_s3(tmp_path, monkeypatch):
    base, full = tmp_path / "base.csv", tmp_path / "full.csv"
    _write_variants(full, 50)
    monkeypatch.setattr("boto3.client",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not touch S3")))
    sync_variants_base.sync(full_path=full, base_path=base, publish=False)  # no S3 call
    assert sync_variants_base._row_count(base) == 50


def test_publish_base_is_best_effort_on_failure(tmp_path, monkeypatch):
    # a failed publish must NOT crash the run (the local base is already written); it returns False + logs.
    base = tmp_path / "base.csv"; _write_variants(base, 10)
    monkeypatch.setattr("boto3.client",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no creds")))
    assert sync_variants_base.publish_base_to_s3(base) is False


def test_factory_reset_reseeds_the_base_file_from_the_pristine_seed(tmp_path, monkeypatch):
    # S0: a factory reset resets the live base FILE to the pristine seed (locally + S3), so a cold start
    # truly derives from the seed -- not from the drifted/accumulated base the container had been mutating.
    from stone_pipeline import lifecycle
    seed = tmp_path / "base.seed.csv"
    base = tmp_path / "base.csv"
    _write_variants(seed, 12)                       # the pristine seed (small, clean)
    _write_variants(base, 400)                      # a drifted/accumulated live base (larger)
    published = []
    monkeypatch.setattr("stone_pipeline.reference.sync_variants_base.publish_base_to_s3",
                        lambda p: published.append(str(p)) or True)
    result = lifecycle._reseed_base_from_pristine(seed_path=seed, base_path=base)
    assert result["reseeded"] is True
    assert base.read_text() == seed.read_text()     # base FILE reset to pristine, drift gone
    assert published == [str(base)]                 # and republished to S3 so the cold start reads pristine
