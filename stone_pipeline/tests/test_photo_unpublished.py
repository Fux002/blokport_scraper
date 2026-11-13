"""A product never serves a photo the photos side has not published (dev MAR-P/03/06, 11-09).

The produce linked MAR-P/03/06 to standardised photo c041fed8 and left the row dirty; the next produce no longer emitted
it (the vendor dropped it); four minutes later the photos publish removed c041fed8, rightly, since the listing had left
the sheet. The ledger kept serving the frozen dirty row, and the backend could never copy its photos.

Two fixes, one per side of that disagreement:
- ROOT (populate.withdraw_unsent): a product this run no longer produces never serves its unsent change. Never sent: the
  row goes. Held by Medusa: the change is withdrawn (synced, hash blank, failures cleared), the row stays and the delist
  owns it (stock 0 through populate_discontinued), so a later return re-serves as dirty.
- GUARD (populate.mark_unpublished_photos): every produce records, for every product of the source, which standardised
  files it links that are not in the published set (imagestore.published_prefixes, the folders publish keeps). A
  product with any is never served (sync._ELIGIBLE_PRODUCT). It is a content fact, not a sync state, so a requeue or a
  reset can never revive it; only a produce that finds the files published clears it.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from stone_pipeline.config.settings import ENV_NAME, ENV_SEGMENT
from stone_pipeline.config.sources import load_source
from stone_pipeline.core.schema import CanonicalRow, OutputMode
from stone_pipeline.io import imagestore
from stone_pipeline.ledger import sync, writethrough
from stone_pipeline.ledger.db import SCHEMA_VERSION, Ledger
from stone_pipeline.tests._ledger_icons import SLAB_PCAT, add_icon, seed_slab_category
from stone_pipeline.stages import images

_OLD = "2026-11-04T12:12:47+00:00"
_SHA = "c041fed8d1cf8da7f2978da668607e3b6119f93ebcefeefaa83eaeb4c0f1d5f8"
_BASE = "https://bucket.s3.eu-west-1.amazonaws.com/"
_VARIATION = "marble__abadeh_amvaj_1"
_FILES = [imagestore.standard_key("marenostone", _SHA, view) for view in ("main", "white", "studio")]
_CUTOUT = imagestore.cutout_key("marenostone", _SHA, "webp")
PUBLISHED = frozenset([*_FILES, _CUTOUT])
_SCRAPED = f"{_BASE}{ENV_SEGMENT}/products/improved/marenostone/{'a' * 64}.jpg"


def _open(path) -> Ledger:
    """The ledger as record_source opens it (this deployment's fingerprint), with the one synced variety."""
    ledger = Ledger.open(path, env=ENV_NAME, backend_id_fingerprint=writethrough.backend_fingerprint())
    if ledger.get("variation", "key", _VARIATION) is None:
        ledger.execute(
            "INSERT INTO variation (key, type, name, alias_rows, codes, in_full, payload_hash, state, "
            "medusa_id, first_seen, last_synced, created_at, updated_at) "
            "VALUES (?, 'Marble', 'Abadeh Amvaj', '[]', '[]', 1, '', 'synced', 'V', ?, ?, ?, ?)",
            (_VARIATION, _OLD, _OLD, _OLD, _OLD))
        add_icon(ledger, _VARIATION)                 # phase 1: a product serves once its variety has its category's icon
        seed_slab_category(ledger)
    return ledger


def _held_row(path, sku, *, state="dirty", in_medusa=True, photos="standard"):
    """A product the ledger already holds, as an earlier produce left it."""
    if photos == "standard":
        urls = [_BASE + k for k in _FILES]
        thumb, gallery, cutout = urls[0], urls, _BASE + _CUTOUT
    else:
        thumb, gallery, cutout = _SCRAPED, [_SCRAPED], None
    with _open(path) as ledger:
        ledger.execute(
            "INSERT INTO product (sku, source, variation_key, category, state, medusa_id, last_synced, payload_hash, "
            "sync_attempts, sync_error, retry_after, thumbnail_key, product_image_keys, oriented_image_keys, "
            "model_3d_key, created_at, updated_at) VALUES (?, 'mar', ?, 'slabs', ?, ?, ?, 'h-old', 1, 'not copied', "
            "?, ?, ?, '[]', ?, ?, ?)",
            (sku, _VARIATION, state, "prod_1" if in_medusa else None, _OLD if in_medusa else None, _OLD, thumb,
             json.dumps(gallery), cutout, _OLD, _OLD))


def _emitted(surrogate, photos="standard"):
    """A row this run emits, its photos linked by the image stage."""
    urls = [_BASE + k for k in _FILES] if photos == "standard" else [_SCRAPED]
    return CanonicalRow(src_site="marenostone", surrogate_key=surrogate, variation_key=_VARIATION,
                        handle=f"h-{surrogate.lower()}", title="Abadeh Amvaj", inventory_quantity=5,
                        thumbnail_key=urls[0], product_image_keys=urls, image_keys=urls,
                        model_3d_key=_BASE + _CUTOUT if photos == "standard" else None,
                        standard_version="etag-1", standard_photo=True, category_pcat_id=SLAB_PCAT)


@pytest.fixture
def ledger_path(tmp_path, monkeypatch):
    monkeypatch.delenv("BLOKPORT_LEDGER_WRITETHROUGH", raising=False)
    monkeypatch.setenv("SCRAPER_LEDGER_WRITETHROUGH", "1")
    path = tmp_path / "dev.ledger"
    _open(path).close()
    return path


def _record(path, rows, *, published=PUBLISHED, discontinued=()):
    assert writethrough.record_source(rows, list(discontinued), load_source("marenostone"), mode=OutputMode.FULL,
                                      published=published, path=path)


def _served(path) -> list[str]:
    with _open(path) as ledger:
        return [i["external_id"] for i in sync.ready(ledger, "products")]


def _row(path, sku):
    with _open(path) as ledger:
        row = ledger.get("product", "sku", sku)
        return dict(row) if row else None


# --- the published set: one notion, shared with publish ---------------------------------------------------------------

def test_the_published_set_is_the_folders_publish_keeps():
    assert imagestore.published_prefixes() == tuple(
        imagestore.standard_prefix(f) for f in (*imagestore.STANDARD_VIEWS, imagestore.CUTOUT_SUBDIR,
                                                imagestore.TILE_SUBDIR))


def test_a_linked_url_maps_to_its_object_key_only_under_the_standard_folders():
    assert imagestore.standard_object_key(_BASE + _FILES[0]) == _FILES[0]
    assert imagestore.standard_object_key(_BASE + _CUTOUT) == _CUTOUT
    assert imagestore.standard_object_key(_SCRAPED) is None
    assert imagestore.standard_object_key("") is None


def test_the_produce_lists_each_published_folder_once(monkeypatch):
    listed = []
    monkeypatch.setattr(images, "_list_objects", lambda prefix: (listed.append(prefix), {f"{prefix}x.jpg": "e"})[1])
    keys = images.published_keys(SimpleNamespace(mode="s3"))
    assert listed == list(imagestore.published_prefixes())
    assert keys == frozenset(f"{p}x.jpg" for p in imagestore.published_prefixes())


def test_local_mode_has_nothing_published_and_lists_nothing(monkeypatch):
    monkeypatch.setattr(images, "_list_objects", lambda prefix: pytest.fail("local mode must not list S3"))
    assert images.published_keys(SimpleNamespace(mode="local")) == frozenset()


def test_an_unreadable_published_set_raises(monkeypatch):
    def unreadable(prefix):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "ListObjectsV2")
    monkeypatch.setattr(images, "_list_objects", unreadable)
    with pytest.raises(ClientError):
        images.published_keys(SimpleNamespace(mode="s3"))


def test_an_unreadable_published_set_fails_the_source_run_and_records_nothing(tmp_path, monkeypatch, scrape_data_dir):
    from stone_pipeline.run import run_source
    monkeypatch.delenv("BLOKPORT_LEDGER_WRITETHROUGH", raising=False)
    monkeypatch.setenv("SCRAPER_LEDGER_WRITETHROUGH", "1")
    calls: list[int] = []
    monkeypatch.setattr(writethrough, "record_source", lambda *a, **k: (calls.append(1), True)[1])

    def unreadable(*a, **k):
        raise EndpointConnectionError(endpoint_url="https://s3.eu-west-1.amazonaws.com")
    monkeypatch.setattr(images, "published_keys", unreadable)
    with pytest.raises(SystemExit):
        run_source("marenostone", outputs_dir=tmp_path, state_dir=tmp_path, data_dir=scrape_data_dir)
    assert calls == [], "a run that cannot read the published set must not record the ledger"


# --- ROOT: a product the run no longer produces never serves its unsent change ---------------------------------------

def test_a_dropped_dirty_product_medusa_holds_is_withdrawn_kept_and_delisted(ledger_path):
    _held_row(ledger_path, "MAR-P/03/06")
    _record(ledger_path, [], discontinued=[("MAR-P/03/06", "h-p-03-06")])
    row = _row(ledger_path, "MAR-P/03/06")
    assert row is not None, "never delete a product Medusa holds: the delist owns it"
    assert (row["state"], row["payload_hash"], row["sync_attempts"], row["sync_error"], row["retry_after"]) == (
        "synced", "", 0, None, None)
    assert row["medusa_id"] == "prod_1"
    assert _served(ledger_path) == []
    with _open(ledger_path) as ledger:
        assert ledger.get("inventory", "sku", "MAR-P/03/06")["qty"] == 0          # stock 0: the discontinued path
        assert ledger.execute("SELECT COUNT(*) FROM removed").fetchone()[0] == 0  # never the removed lane


def test_the_withdrawn_product_re_emitted_with_its_old_content_is_served(ledger_path):
    # the unsent change is REAL content: the produce wrote it (its payload_hash H), Medusa holds an older version. The
    # withdrawal must blank H: the ledger no longer holds what Medusa acked, so the same content coming back is a change
    # to serve. Had it kept H, the identical re-emit would match, stay synced and never reach Medusa.
    _record(ledger_path, [_emitted("P/03/06")])
    with _open(ledger_path) as ledger:
        ledger.execute("UPDATE product SET state = 'dirty', medusa_id = 'prod_1', last_synced = ? "
                       "WHERE sku = 'MAR-P/03/06'", (_OLD,))
        unsent = ledger.get("product", "sku", "MAR-P/03/06")["payload_hash"]
    assert unsent
    _record(ledger_path, [], discontinued=[("MAR-P/03/06", "h-p-03-06")])          # dropped: the change is withdrawn
    _record(ledger_path, [_emitted("P/03/06")])                                     # the identical content returns
    row = _row(ledger_path, "MAR-P/03/06")
    assert (row["payload_hash"], row["state"]) == (unsent, "dirty")
    assert _served(ledger_path) == ["MAR-P/03/06"]


def test_a_leased_product_the_run_no_longer_produces_is_untouched(ledger_path):
    _held_row(ledger_path, "MAR-P/03/06", state="syncing")
    before = _row(ledger_path, "MAR-P/03/06")
    _record(ledger_path, [])
    after = _row(ledger_path, "MAR-P/03/06")
    assert (after["state"], after["payload_hash"]) == (before["state"], before["payload_hash"]) == ("syncing", "h-old")


def test_a_never_sent_product_the_run_no_longer_produces_is_still_removed(ledger_path):
    _held_row(ledger_path, "MAR-P/03/07", state="pending", in_medusa=False)
    _record(ledger_path, [])
    assert _row(ledger_path, "MAR-P/03/07") is None


# --- GUARD: a product linking an unpublished standardised file is never served --------------------------------------

def test_a_product_linking_an_unpublished_file_is_excluded_and_named(ledger_path, monkeypatch):
    warned: list[tuple[str, dict]] = []
    monkeypatch.setattr(writethrough.log, "warning", lambda msg, *a, **k: warned.append((msg, k)))
    _record(ledger_path, [_emitted("P/03/01")], published=frozenset())             # published, then removed by publish
    row = _row(ledger_path, "MAR-P/03/01")
    assert row["state"] == "pending" and json.loads(row["photo_unpublished"]) == sorted([*_FILES, _CUTOUT])
    assert _served(ledger_path) == []
    assert any("MAR-P/03/01" in json.dumps(k.get("extra", {})) for _, k in warned), "the produce names the SKU"
    with _open(ledger_path) as ledger:
        assert sync.status(ledger)["product_photos"] == {"unpublished": 1}
        held = [f for f in sync.failures(ledger) if f["external_id"] == "MAR-P/03/01"]
    assert len(held) == 1 and held[0]["type"] == "products" and held[0]["state"] == "photo_unpublished"
    assert held[0]["error"] == f"photo not published: 4 files ({sorted([*_FILES, _CUTOUT])[0]})"


def test_a_dropped_product_medusa_holds_with_a_removed_file_is_withdrawn_and_marked(ledger_path):
    _held_row(ledger_path, "MAR-P/03/06")
    _record(ledger_path, [], published=frozenset(), discontinued=[("MAR-P/03/06", "h-p-03-06")])
    row = _row(ledger_path, "MAR-P/03/06")
    assert row["state"] == "synced" and json.loads(row["photo_unpublished"]) == sorted([*_FILES, _CUTOUT])
    assert _served(ledger_path) == []


def test_the_exclusion_survives_a_requeue_and_a_soft_reset(ledger_path):
    _record(ledger_path, [_emitted("P/03/01")], published=frozenset())
    with _open(ledger_path) as ledger:
        ledger.execute("UPDATE product SET state = 'gap_held' WHERE sku = 'MAR-P/03/01'")
        sync.requeue_dead_lettered(ledger)
        assert ledger.get("product", "sku", "MAR-P/03/01")["state"] == "dirty"
        assert sync.count_ready(ledger, "products") == 0                           # a requeue does not revive it
        sync.reset_sync_state(ledger)
        row = ledger.get("product", "sku", "MAR-P/03/01")
        assert row["state"] == "pending" and row["photo_unpublished"]
        assert sync.count_ready(ledger, "products") == 0                           # nor does a reset


def test_an_emitted_product_whose_files_are_published_is_untouched(ledger_path):
    _record(ledger_path, [_emitted("P/03/01")])
    assert _row(ledger_path, "MAR-P/03/01")["photo_unpublished"] is None
    assert _served(ledger_path) == ["MAR-P/03/01"]


def test_scraped_photos_are_out_of_scope(ledger_path):
    _record(ledger_path, [_emitted("P/03/01", photos="scraped")], published=frozenset())
    assert _row(ledger_path, "MAR-P/03/01")["photo_unpublished"] is None
    assert _served(ledger_path) == ["MAR-P/03/01"]


def test_the_fact_clears_when_the_files_are_published_again(ledger_path):
    _record(ledger_path, [_emitted("P/03/01")], published=frozenset())
    assert _served(ledger_path) == []
    _record(ledger_path, [_emitted("P/03/01")])
    assert _row(ledger_path, "MAR-P/03/01")["photo_unpublished"] is None
    assert _served(ledger_path) == ["MAR-P/03/01"]


def test_the_fact_is_not_served_content(ledger_path):
    _record(ledger_path, [_emitted("P/03/01")])
    hashed = _row(ledger_path, "MAR-P/03/01")["payload_hash"]
    _record(ledger_path, [_emitted("P/03/01")], published=frozenset())
    assert _row(ledger_path, "MAR-P/03/01")["payload_hash"] == hashed


def test_a_phase_1_v15_ledger_gains_photo_unpublished_in_place_and_keeps_its_data(tmp_path):
    # SC-4's store migration writes v15 stores (dev's .v2 ledger among them): the v16 column is added in place,
    # never by the store migration again, and the row and the ledger's own version follow
    path = tmp_path / "mig.ledger"
    with Ledger.open(path, env="development") as ledger:
        ledger.execute("INSERT INTO product (sku, source, state, created_at, updated_at) "
                       "VALUES ('MAR-1', 'mar', 'synced', 'x', 'x')")
    raw = sqlite3.connect(path)                                                     # simulate a v15 ledger
    raw.execute("ALTER TABLE product DROP COLUMN photo_unpublished")
    raw.execute("PRAGMA user_version = 15")
    raw.execute("UPDATE ledger_meta SET schema_version = 15")
    raw.commit()
    raw.close()
    for _ in range(2):                                                              # the second open is a no-op
        with Ledger.open(path, env="development") as ledger:
            # v16 is this column's own version; a later column moves SCHEMA_VERSION on, never this step
            assert ledger.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 16
            assert ledger.get("ledger_meta", "id", 1)["schema_version"] == SCHEMA_VERSION
            row = ledger.get("product", "sku", "MAR-1")
            assert row["photo_unpublished"] is None and row["state"] == "synced"