"""The photo list (STANDARDISE.PHOTOS_FILE): each listing's own photos in its order, written by the produce beside the
product sheet and read by the photo process and its GPU job instead of the sheet's image columns, which a publish
replaces with the chosen photo's views. A missing or malformed list, or a listing missing from it, fails loud."""
from __future__ import annotations

import json

import numpy as np
import pytest
from botocore.exceptions import ClientError

from stone_pipeline.config.standardise_settings import STANDARDISE as C
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.standardise import index

A, B = "a" * 64, "b" * 64
HOSTED = "https://b.s3.amazonaws.com/dev/products/{}/sentas/{}.jpg"


# --- reading it -------------------------------------------------------------------------------------------------------
def _slab_category(monkeypatch, pcat="pcat_S"):
    from dataclasses import replace
    from stone_pipeline.config import categories
    slab = replace(categories.category("slab"), pcat_id=pcat)
    monkeypatch.setattr(categories, "_BY_NAME", {**categories._BY_NAME, slab.name: slab})


def test_a_listing_missing_from_the_photo_list_fails_loud_never_falls_back_to_the_sheet(monkeypatch):
    _slab_category(monkeypatch)
    sheet = f"Product Handle,Product Category 1,Product Image 1\none,pcat_S,{HOSTED.format('improved', A)}\n"
    with pytest.raises(ValueError, match="not in the photo list"):
        index.listings(sheet, {})


@pytest.mark.parametrize("text", ["", "[]", '{"one": "x"}', '{"one": [["sentas"]]}', '{"one": [["sentas", "abc"]]}',
                                  '{"one": [["Sentas!", "%s"]]}' % A, '{"one": [[1, "%s"]]}' % A, '{"": []}'])
def test_a_malformed_photo_list_fails_loud(text):
    with pytest.raises(ValueError):
        index.parse_photos(text)


def test_a_photo_list_reads_back_as_written():
    assert index.parse_photos(json.dumps({"one": [["sentas", A], ["zucchi", B]], "none": []})) == \
        {"one": [("sentas", A), ("zucchi", B)], "none": []}


def test_a_missing_photo_list_fails_loud(monkeypatch):
    from stone_pipeline.photos import source as src

    def get(client, key):
        raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
    monkeypatch.setattr(src, "_get", get)
    monkeypatch.setattr(src, "_sheet", lambda client: "Product Handle,Product Category 1\n")
    with pytest.raises(RuntimeError, match="photo list"):
        src.sheet_and_photos(object())
    with pytest.raises(RuntimeError, match="photo list"):
        src.sheet_photos(object())


# --- the produce writing it -------------------------------------------------------------------------------------------
def test_each_product_s_own_photos_once_each_in_its_order():
    urls = [HOSTED.format("improved", B), HOSTED.format("scraped", A), HOSTED.format("improved", B), "https://x/y.jpg"]
    assert index.photo_refs(urls) == [["sentas", B], ["sentas", A]]


def test_the_image_stage_keeps_each_product_s_own_photos_before_the_standardised_views_replace_them(monkeypatch):
    from stone_pipeline.stages import images
    own = [HOSTED.format("improved", A), HOSTED.format("improved", B)]
    monkeypatch.setattr(images, "_load_standard_views", lambda cfg: {"main": {A: "m"}, "white": {A: "w"}})
    monkeypatch.setattr(images, "_link_from_manifest", lambda rows, cfg, stats: None)
    row = CanonicalRow(src_site="sentas", surrogate_key="1", format_value="Slab", image_keys=list(own))
    cfg = type("Cfg", (), {"mode": "passthrough"})()
    images.run([row], cfg=cfg)
    images.link_standard([row], [row], cfg=cfg, image_pipeline=True)
    assert [u.split("/products/")[1] for u in row.image_keys] == [f"standard/main/sentas/{A}.jpg", f"standard/white/sentas/{A}.jpg"]
    assert row.own_photo_keys == own


def test_the_own_photos_survive_the_canonical_parquet(tmp_path):
    from stone_pipeline.io import staging
    row = CanonicalRow(src_site="sentas", surrogate_key="1", own_photo_keys=[HOSTED.format("improved", A)])
    path = staging.write_canonical([row], tmp_path / "c.parquet")
    assert staging.read_canonical(path)[0].own_photo_keys == row.own_photo_keys


def _emitted(handle, *shas):
    return CanonicalRow(src_site="sentas", surrogate_key=handle, handle=handle,
                        own_photo_keys=[HOSTED.format("improved", s) for s in shas])


def test_the_emit_writes_the_photo_list_sorted_and_the_same_bytes_on_a_rerun(tmp_path):
    from stone_pipeline.stages import emit
    path = tmp_path / C.PHOTOS_FILE
    emit.write_photos_json([_emitted("two", A), _emitted("one", B, A), _emitted("none")], path)
    first = path.read_bytes()
    assert json.loads(first) == {"none": [], "one": [["sentas", B], ["sentas", A]], "two": [["sentas", A]]}
    assert list(json.loads(first)) == ["none", "one", "two"]
    emit.write_photos_json([_emitted("one", B, A), _emitted("none"), _emitted("two", A)], path)
    assert path.read_bytes() == first


def test_two_products_with_one_handle_and_different_photos_fail_loud(tmp_path):
    from stone_pipeline.stages import emit
    with pytest.raises(ValueError, match="'one'"):
        emit.write_photos_json([_emitted("one", A), _emitted("one", B)], tmp_path / C.PHOTOS_FILE)


def _run_dir(root, source, handles):
    d = root / f"{source}_20261102_120000"
    (d / "diagnostics").mkdir(parents=True)
    (d / "diagnostics" / "canonical.parquet").write_bytes(b"x")                          # a complete run
    (d / "4_products_import").mkdir()
    (d / "4_products_import" / "medusa_import.csv").write_text("Product Handle\n" + "".join(f"{h}\n" for h in handles))
    (d / "4_products_import" / C.PHOTOS_FILE).write_text(json.dumps({h: [[source, A]] for h in handles}))
    return d


def _to_upload(monkeypatch, path):
    from types import SimpleNamespace
    from stone_pipeline import catalog
    monkeypatch.setattr(catalog, "SETTINGS", SimpleNamespace(paths=SimpleNamespace(to_upload_dir=path)))


def test_the_catalog_gathers_the_photo_lists_into_one_beside_the_product_sheet(tmp_path, monkeypatch):
    from stone_pipeline import catalog
    _to_upload(monkeypatch, tmp_path / "to_upload")
    _run_dir(tmp_path / "out", "zucchi", ["z1"])
    _run_dir(tmp_path / "out", "sentas", ["s2", "s1"])
    catalog.collect_products(tmp_path / "out")
    merged = (tmp_path / "to_upload" / C.PHOTOS_FILE).read_bytes()
    assert json.loads(merged) == {"s1": [["sentas", A]], "s2": [["sentas", A]], "z1": [["zucchi", A]]}
    assert list(json.loads(merged)) == ["s1", "s2", "z1"]
    assert (tmp_path / "to_upload" / "3_products_all.csv").exists()
    catalog.collect_products(tmp_path / "out")
    assert (tmp_path / "to_upload" / C.PHOTOS_FILE).read_bytes() == merged                # byte-identical re-run
    assert C.PHOTOS_KEY.rsplit("/", 1) == [C.SHEET_KEY.rsplit("/", 1)[0], C.PHOTOS_FILE]   # beside the sheet on S3


def test_a_run_without_its_photo_list_never_blocks_the_catalog_and_is_named_loud(tmp_path, monkeypatch, caplog):
    # a catalog-only run after the deploy, a scoped produce, a source whose scrape aborted: the other sources' sheet and
    # photo list are still published; the photo process is the gate (a sheet listing missing from the list fails it)
    import logging
    from stone_pipeline import catalog
    _to_upload(monkeypatch, tmp_path / "to_upload")
    old = _run_dir(tmp_path / "out", "zucchi", ["z1"])
    (old / "4_products_import" / C.PHOTOS_FILE).unlink()
    _run_dir(tmp_path / "out", "sentas", ["s1"])
    with caplog.at_level(logging.WARNING):
        counts = catalog.collect_products(tmp_path / "out")
    assert counts == {"sentas": 1, "zucchi": 1}
    assert json.loads((tmp_path / "to_upload" / C.PHOTOS_FILE).read_text()) == {"s1": [["sentas", A]]}
    assert any("zucchi" in r.getMessage() and r.levelno >= logging.WARNING for r in caplog.records)


def test_the_photo_list_lives_outside_the_product_sheets_names():
    assert C.PHOTOS_FILE == "standardise_photo_list.json" and not C.PHOTOS_FILE.startswith("3_products_")


def test_a_reset_removes_the_photo_list_with_the_product_sheets_locally_and_on_s3(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import boto3
    from stone_pipeline import lifecycle
    from stone_pipeline.config import settings
    up = tmp_path / "to_upload"
    up.mkdir()
    for name in ("3_products_all.csv", C.PHOTOS_FILE, "1_variants_full.csv"):
        (up / name).write_text("x")
    monkeypatch.setattr(settings, "SETTINGS", SimpleNamespace(paths=SimpleNamespace(to_upload_dir=up)))
    deleted = []

    class _Bucket:
        def list_objects_v2(self, Bucket, Prefix):
            return {"Contents": [{"Key": f"{Prefix}{n}"} for n in ("3_products_all.csv", C.PHOTOS_FILE, "1_variants_full.csv")]}

        def delete_objects(self, Bucket, Delete):
            deleted.extend(o["Key"].rsplit("/", 1)[-1] for o in Delete["Objects"])
    monkeypatch.setattr(boto3, "client", lambda *a, **k: _Bucket())
    out = lifecycle._prune_stale_product_exports()
    assert sorted(p.name for p in up.iterdir()) == ["1_variants_full.csv"] and out["local"] == 2
    assert sorted(deleted) == sorted(["3_products_all.csv", C.PHOTOS_FILE]) and out["s3"] == 2


def test_one_handle_in_two_sources_stops_the_catalog(tmp_path, monkeypatch):
    from stone_pipeline import catalog
    _to_upload(monkeypatch, tmp_path / "to_upload")
    _run_dir(tmp_path / "out", "zucchi", ["same"])
    _run_dir(tmp_path / "out", "sentas", ["same"])
    with pytest.raises(RuntimeError, match="'same'"):
        catalog.collect_products(tmp_path / "out")


# --- the defaulted sizes beside it (STANDARDISE.DEFAULTED_FILE): a size the produce filled from the pack's defaults is
# never squared to, so a keystone on such a listing is held, not drawn at a guessed size (#605 review M1) -------------
def _defaulted(handle, *fields):
    from stone_pipeline.core.schema import FlagCode, ReviewFlag
    row = _emitted(handle, A)
    row.review_flags = [ReviewFlag(field=f, code=FlagCode.dimension_defaulted) for f in fields]
    return row


def test_the_emit_lists_the_handles_whose_length_or_width_was_defaulted_from_the_photo_list_s_rows(tmp_path):
    from stone_pipeline.stages import emit
    rows = [_defaulted("w", "width"), _defaulted("l", "length"), _defaulted("h", "height"), _emitted("real", A),
            _defaulted("l", "length")]
    emit.write_photos_json(rows, tmp_path / C.PHOTOS_FILE)
    first = (tmp_path / C.DEFAULTED_FILE).read_bytes()
    assert json.loads(first) == ["l", "w"]                     # the footprint only: a defaulted thickness squares nothing
    emit.write_photos_json(list(reversed(rows)), tmp_path / C.PHOTOS_FILE)
    assert (tmp_path / C.DEFAULTED_FILE).read_bytes() == first
    assert C.DEFAULTED_KEY.rsplit("/", 1) == [C.PHOTOS_KEY.rsplit("/", 1)[0], C.DEFAULTED_FILE]


def test_the_catalog_gathers_the_defaulted_sizes_beside_the_photo_list(tmp_path, monkeypatch):
    from stone_pipeline import catalog
    _to_upload(monkeypatch, tmp_path / "to_upload")
    z = _run_dir(tmp_path / "out", "zucchi", ["z1", "z2"])
    (z / "4_products_import" / C.DEFAULTED_FILE).write_text(json.dumps(["z2"]))
    s = _run_dir(tmp_path / "out", "sentas", ["s1"])
    (s / "4_products_import" / C.DEFAULTED_FILE).write_text(json.dumps(["s1"]))
    catalog.collect_products(tmp_path / "out")
    assert (tmp_path / "to_upload" / C.DEFAULTED_FILE).read_text() == json.dumps(["s1", "z2"], separators=(",", ":"))


def test_a_defaulted_size_reads_as_unknown_so_a_keystone_on_it_is_held(monkeypatch):
    from stone_pipeline.standardise import outline
    _slab_category(monkeypatch)
    sheet = "Product Handle,Product Category 1,Product Length,Product Width\nreal,pcat_S,2.85,1.85\nguess,pcat_S,3.3,2.0\n"
    found = index.build(sheet, {"real": [("sentas", A)], "guess": [("sentas", B)]}, frozenset({"guess"}))
    assert found[A]["size"] == (2.85, 1.85) and found[B]["size"] is None
    q = np.float32([[4.0, 14.0], [1128.3, 36.0], [1050.4, 610.9], [97.0, 626.9]])   # POL-736's keystone, 1.18
    assert isinstance(outline.steep_aspect(q, found[B]["size"]), str)


@pytest.mark.parametrize("text", ["", "{}", '"x"', "[1]", '[""]', '["b", "a"]', '["a", "a"]'])
def test_a_malformed_defaulted_list_fails_loud(text):
    with pytest.raises(ValueError):
        index.parse_defaulted(text)


def test_the_job_reads_the_defaulted_sizes_and_a_present_but_unreadable_file_fails_loud(monkeypatch):
    from stone_pipeline.photos import source as src
    files = {f"{src.ENV_SEGMENT}/{C.PHOTOS_KEY}": json.dumps({"one": [["sentas", A]]}).encode(),
             f"{src.ENV_SEGMENT}/{C.DEFAULTED_KEY}": b'["one"]'}

    def get(client, key):
        if key not in files:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return files[key]
    _slab_category(monkeypatch)
    monkeypatch.setattr(src, "_get", get)
    monkeypatch.setattr(src, "_sheet", lambda client: "Product Handle,Product Category 1,Product Length,Product Width\n"
                                                      "one,pcat_S,3.3,2.0\n")
    assert src.sheet_photos(object())[A]["size"] is None
    files[f"{src.ENV_SEGMENT}/{C.DEFAULTED_KEY}"] = b"not json"
    with pytest.raises(RuntimeError, match="defaulted"):
        src.sheet_photos(object())
    del files[f"{src.ENV_SEGMENT}/{C.DEFAULTED_KEY}"]                 # a producer from before the file: none defaulted
    assert src.sheet_photos(object())[A]["size"] == (3.3, 2.0)
