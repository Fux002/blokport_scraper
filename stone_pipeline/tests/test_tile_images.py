"""Tile listings' images (owner 2026-11-02): rendered from the same variety's judge-passed, published slab stone, never
guessed. A tile listing without a published slab of its variety AND finish gets nothing; a slab cut-out with a
crosshatch or at the wrong proportions is never used; one slab per variety, never a pool; every tile leads with the
laid tiles (main), however few distinct faces its slab gives; every view at the listing's own size (a tile's size, never a defaulted one); the 3D
tile at its listed length and proportions, CUTOUT_LONG_SIDE px; the tile step
renders exactly when its inputs change and writes byte-identical files; the photo process never takes a tile listing,
and the image stage links a tile product to the set named after it."""
from __future__ import annotations

import hashlib
import io
import json
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest
from botocore.exceptions import ClientError

from stone_pipeline.photos import publish as pub
from stone_pipeline.photos import process as proc
from stone_pipeline.config import categories as registry
from stone_pipeline.config.settings import ENV_SEGMENT
from stone_pipeline.config.standardise_settings import STANDARDISE as C
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.io import imagestore
from stone_pipeline.stages import images
from stone_pipeline.standardise import tile_pattern as P
from stone_pipeline.standardise import tile_plan, tile_stone, tile_views

FIXTURES = Path(__file__).parent / "fixtures" / "tiles"
A, B, D, E = "a" * 64, "b" * 64, "d" * 64, "e" * 64
POLISHED, FLAMED = "fin_polished", "fin_flamed"
GRANITE, MARBLE = "type_granite", "type_marble"
ROOT = f"{ENV_SEGMENT}/standardise-test/{C.CLEAN_TAG}/"
RUN = f"{ENV_SEGMENT}/standardise-test/{C.RUN_TAG}/marenostone/"


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """Draw at canvas size (no supersampling): the same code, a quarter of the time."""
    monkeypatch.setattr(C, "TILE_SUPERSAMPLE", 1)


# --- real cut-outs: the crosshatch check -----------------------------------------------------------------------------
def _fixture(name):
    return tile_stone.decode((FIXTURES / name).read_bytes())


def test_the_tiger_black_slab_with_a_mesh_over_its_face_is_never_cut_into_tiles():
    # a crop of the real published cut-out prod/products/standard/cutout/marenostone/805cf5ac...webp, read from prod S3
    # 2026-11-02 (the whole file scores 17.6)
    rgba = _fixture("crosshatch_805cf5ac.webp")
    assert tile_stone.grid_score(rgba) > C.TILE_GRID_MAX
    with pytest.raises(tile_stone.Refused, match="crosshatch"):
        tile_stone.from_cutout(rgba, 1.0, 1.0)


@pytest.mark.parametrize("name", ["linear_7984ade4.webp", "grooved_1d37abc7.webp"])
def test_stone_that_runs_one_way_is_not_a_crosshatch(name):
    # real prod cut-outs: a linear marble (veins along one axis) and a grooved finish (regular ridges, one axis)
    assert tile_stone.grid_score(_fixture(name)) < C.TILE_GRID_MAX / 2


def _stone_rgba(w, h, seed=1, tint=(170, 160, 150)):
    """A synthetic slab cut-out: smooth random veining in a tint, fully opaque."""
    rng = np.random.default_rng(seed)
    noise = cv2.GaussianBlur(rng.normal(0, 1, (h, w)).astype(np.float32), (0, 0), 6)
    grey = np.clip(noise / (noise.std() + 1e-6) * 18, -50, 50)
    rgb = np.clip(np.float32(tint)[None, None] + grey[..., None], 0, 255).astype(np.uint8)
    return np.dstack([rgb, np.full((h, w), 255, np.uint8)])


def test_a_cut_out_not_at_the_slab_s_listed_proportions_is_never_used():
    with pytest.raises(tile_stone.Refused, match="proportions"):
        tile_stone.from_cutout(_stone_rgba(800, 400), 2.4, 1.6)        # 2:1 cut-out for a 3:2 slab: its scale is unknown
    assert tile_stone.from_cutout(_stone_rgba(780, 520), 2.4, 1.6).ppm == pytest.approx(325, rel=1e-3)


def test_an_upright_cut_out_is_turned_so_x_runs_along_the_slab_s_length():
    stone = tile_stone.from_cutout(_stone_rgba(520, 780), 2.4, 1.6)
    assert stone.rgb.shape[:2] == (520, 780)


# --- the faces ---------------------------------------------------------------------------------------------------------
def _stone(w=900, h=600, slab=(3.0, 2.0)):
    return tile_stone.from_cutout(_stone_rgba(w, h), *slab)


def _on_stone(cuts):
    """(height, width) of a face as it lies on the stone."""
    return (cuts.length_px, cuts.width_px) if cuts.across else (cuts.width_px, cuts.length_px)


def test_faces_are_whole_non_overlapping_crops_of_valid_stone():
    stone = _stone()                                                   # 2.94 x 1.94 m usable
    cuts = tile_stone.cuts(stone, 0.6, 0.4)
    h, w = _on_stone(cuts)
    taken = np.zeros(stone.valid.shape, int)
    for x, y in cuts.origins:
        taken[y:y + h, x:x + w] += 1
    assert taken.max() == 1 and stone.valid[taken > 0].all()
    assert cuts.across and len(cuts.origins) == 7 * 3                  # across 7 x 3 = 21 beats along 4 x 4 = 16


def test_a_long_plank_is_cut_across_the_slab_when_that_gives_more_faces():
    stone = _stone()                                                   # 3.0 x 2.0 m
    cuts = tile_stone.cuts(stone, 1.45, 0.35)                          # along: 2 x 5 = 10; across: 8 x 1 = 8
    assert not cuts.across and len(cuts.origins) == 10
    cuts = tile_stone.cuts(stone, 1.9, 0.3)                            # along: 1 x 6 = 6; across: 9 x 1 = 9
    assert cuts.across and len(cuts.origins) == 9


def test_a_face_is_the_stone_s_own_pixels_turned_never_mirrored():
    stone = _stone()
    along = tile_stone.cuts(stone, 1.45, 0.35)
    assert not along.across
    x, y = along.origins[0]
    assert np.array_equal(tile_stone.face(stone, along, 0), stone.rgb[y:y + along.width_px, x:x + along.length_px])
    across = tile_stone.cuts(stone, 1.9, 0.3)
    x, y = across.origins[0]
    crop = stone.rgb[y:y + across.length_px, x:x + across.width_px]
    assert np.array_equal(tile_stone.face(stone, across, 0), np.rot90(crop))


# --- the laying --------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("size", [(0.6, 0.6), (0.6, 0.4), (1.45, 0.35)])
def test_every_floor_point_lies_in_exactly_one_tile(size):
    p = P.for_tile(*size)
    rng = np.random.default_rng(0)
    x, y = rng.uniform(-5, 5, 20000), rng.uniform(-5, 5, 20000)
    i, j, u, v = P.locate(p, x, y)
    assert (u >= 0).all() and (u <= p.length).all() and (v >= 0).all() and (v <= p.width).all()
    back_x, back_y = i * p.a[0] + j * p.b[0] + u, j * p.b[1] + v
    assert np.allclose(back_x, x) and np.allclose(back_y, y)


@pytest.mark.parametrize("size,faces", [((0.6, 0.6), 8), ((0.6, 0.4), 6), ((0.6, 0.4), 17)])
def test_no_tile_shares_its_face_with_a_neighbour(size, faces):
    p = P.for_tile(*size)
    i, j = np.meshgrid(np.arange(-20, 20), np.arange(-20, 20))
    index, _ = P.assign(p, i, j, faces, (-20, 19))
    assert (index[:, 1:] != index[:, :-1]).all()                       # along a row
    assert (index[1:, :] != index[:-1, :]).all()                       # the tile below (straight) or below-left (bond)
    if p.b[0]:
        assert (index[1:, :-1] != index[:-1, 1:]).all()               # half-bond: the other tile it rests on


def test_a_tile_gets_the_same_face_whatever_part_of_the_floor_is_drawn():
    p = P.for_tile(0.6, 0.4)
    i, j = np.meshgrid(np.arange(-5, 5), np.arange(-5, 5))
    whole, _ = P.assign(p, i, j, 9, (-5, 4))
    top, _ = P.assign(p, i[:5], j[:5], 9, (-5, 4))
    assert np.array_equal(whole[:5], top)


# --- the 3D tile (Blokport's viewer) -----------------------------------------------------------------------------------
def _tile(stone, long_m, short_m, thick=0.02):
    cuts = tile_stone.cuts(stone, long_m, short_m)
    return tile_views.Tile(tuple(tile_stone.faces(stone, cuts)), tile_stone.hero(stone, cuts), stone.ppm, long_m, short_m,
                           thick)


@pytest.mark.parametrize("stored,shape", [((0.6, 0.4), (1365, 2048)), ((0.4, 0.6), (2048, 1365)),
                                          ((0.6, 0.8), (2048, 1536)), ((0.6, 0.6), (2048, 2048))])
def test_the_3d_tile_runs_along_its_stored_length_at_its_proportions_and_the_viewer_s_size(stored, shape):
    stone = _stone()
    tile = _tile(stone, max(stored), min(stored))
    webp, meta = tile_views.model(tile_views.oriented(tile, *stored), stone.ppm)
    img = tile_stone.decode(webp)
    assert img.shape[:2] == shape                                      # horizontal axis = the stored length
    assert abs(img.shape[1] / img.shape[0] - stored[0] / stored[1]) < 1e-3
    assert max(img.shape[:2]) >= C.CUTOUT_LONG_SIDE
    m = json.loads(meta)
    assert (m["width_px"], m["height_px"]) == (shape[1], shape[0])
    assert sorted(map(tuple, m["outline"])) == [(0, 0), (0, 1), (1, 0), (1, 1)]
    assert m["px_per_mm"] == round(stone.ppm / 1000, 4)                # the stone's real resolution, not the resampled


# --- every view at the listing's own size (the real prod tile sizes) ---------------------------------------------------
REAL_SIZES = [(0.6, 0.4, 0.02), (0.6, 0.6, 0.02), (1.45, 0.35, 0.03), (1.2, 1.2, 0.018)]
RED = (230.0, 20.0, 20.0)


def _red_tile(size):
    """A tile of the listed size whose faces are a flat red at 300 px/m (a 3 x 2 m stone): what is red in a view is
    face, so its drawn shape can be measured."""
    length, width, thick = size
    long_m, short_m = max(length, width), min(length, width)
    face = np.empty((int(round(short_m * 300)), int(round(long_m * 300)), 3), np.float32)
    face[:] = RED
    return tile_views.Tile((face, face.copy(), face.copy()), 0, 300.0, long_m, short_m, thick)


def _red_box(rgb):
    ys, xs = np.nonzero((rgb[:, :, 0] > 150) & (rgb[:, :, 1] < 90) & (rgb[:, :, 2] < 90))
    return xs.min(), xs.max() + 1, ys.min(), ys.max() + 1


@pytest.mark.parametrize("size", REAL_SIZES)
def test_the_white_view_shows_the_tile_at_its_listed_proportions(size):
    length, width, _ = size
    tile = _red_tile(size)
    face = tile_views.oriented(tile, length, width)
    assert abs(face.shape[1] / face.shape[0] / (length / width) - 1) < 0.01  # the face the white and studio views draw
    x0, x1, y0, y1 = _red_box(tile_views.single(face))
    assert abs((x1 - x0) / (y1 - y0) / (length / width) - 1) < 0.01


def _drawn(to_px, quad):
    """The mask of a world quad as the studio view draws it (at the canvas's size)."""
    mask = np.zeros((C.OUT_H, C.OUT_W), np.uint8)
    cv2.fillPoly(mask, [np.int32(np.round(to_px(quad)))], 1)
    return mask > 0


@pytest.mark.parametrize("size", REAL_SIZES)
def test_the_studio_view_shows_the_listed_face_and_its_thickness_where_the_camera_puts_l_w_h(size):
    length, width, thick = size
    face = tile_views.oriented(_red_tile(size), length, width)
    rgb = tile_views.side(face, size).astype(np.float32)
    to_px = tile_views.side_projection(face, size, 1)                    # the same camera at the canvas's size
    red = (rgb[:, :, 0] > 120) & (rgb[:, :, 1] < 90) & (rgb[:, :, 2] < 90)
    top = rgb[:, :, 0] > 0.9 * RED[0]                                  # the face in full light; the sawn edges in less
    want_face = _drawn(to_px, [(0, width, thick), (length, width, thick), (length, 0, thick), (0, 0, thick)])
    assert (want_face & red & top).sum() / (want_face | (red & top)).sum() > 0.95   # the drawn face is the L x W one
    want_edge = _drawn(to_px, [(0, 0, thick), (length, 0, thick), (length, 0, 0), (0, 0, 0)]) & ~want_face
    near_light = C.TILE_SIDE_EDGE_LIGHT[1] * RED[0]                     # the near sawn edge's red, told from the side's
    near = red & (np.abs(rgb[:, :, 0] - near_light) < 12)
    # the drawn near edge is the projection of the true thickness H: its area within 30% (it is a few px high), and
    # twice the thickness would double it
    assert abs(near.sum() / want_edge.sum() - 1) < 0.3


@pytest.mark.parametrize("size", REAL_SIZES)
def test_the_laid_tiles_are_l_by_w_at_the_stated_scale(size):
    length, width, _ = size
    tile = _red_tile(size)
    scale = tile_views.close_scale(tile)                              # px per metre of floor
    assert scale / tile.ppm <= C.TILE_CLOSE_MAX_UPSCALE + 1e-9          # real pixels enlarged at most 2x
    rgb = tile_views.close(tile).astype(np.float32)
    joint = ~((rgb[:, :, 0] > 150) & (rgb[:, :, 1] < 90))             # grout: anything that is not the red face
    for along, step in ((joint.mean(axis=0) > 0.5, tile.long_m), (joint.mean(axis=1) > 0.5, tile.short_m)):
        lines = np.nonzero(along)[0]
        starts = lines[np.r_[True, np.diff(lines) > 1]]               # one start per joint line
        if len(starts) >= 2:                                         # the joint lines are a tile plus a joint apart
            assert np.allclose(np.diff(starts), (step + C.TILE_JOINT_M) * scale, atol=1.5)


@pytest.mark.parametrize("size", REAL_SIZES)
def test_the_3d_tile_of_a_real_size_runs_along_its_length_at_its_proportions(size):
    length, width, _ = size
    tile = _red_tile(size)
    img = tile_stone.decode(tile_views.model(tile_views.oriented(tile, length, width), tile.ppm)[0])
    assert abs(img.shape[1] / img.shape[0] / (length / width) - 1) < 0.01 and max(img.shape[:2]) == C.CUTOUT_LONG_SIDE


@pytest.mark.parametrize("size,ok", [((0.6, 0.4, 0.02), True), ((1.45, 0.35, 0.03), True), ((1.2, 1.2, 0.018), True),
                                     ((3.0, 1.8, 0.02), False), ((0.6, 0.6, 0.2), False), ((0.6, 0.05, 0.02), False),
                                     ((0.6, 0.6, 0.005), False)])
def test_only_a_tile_s_size_is_rendered(size, ok):
    assert tile_plan.tile_size(size) is ok


# --- the plan: never a guess -------------------------------------------------------------------------------------------
_COLUMNS = ("Product Handle,Product Category 1,Product Length,Product Width,Product Height,STN Variation Id,STN Type Id,"
            "STN Finish Id,Product Image 1,Variant Sku")
_IMG = "https://b.s3.amazonaws.com/{env}/products/improved/{src}/{sha}.jpg"


@pytest.fixture
def categories(monkeypatch):
    by = {n: replace(c, pcat_id=f"pcat_{n}") for n, c in registry._BY_NAME.items()}
    monkeypatch.setattr(registry, "_BY_NAME", by)


def _sheet(*rows):
    """(the product sheet's text, its photo list) of rows: (handle, format, L, W, H, variation id, type id, finish id,
    source, photo sha)."""
    lines, photos = [_COLUMNS], {}
    for h, fmt, L, W, H, var, typ, fin, src, sha in rows:
        lines.append(f"{h},pcat_{fmt},{L},{W},{H},{var},{typ},{fin},{_IMG.format(env=ENV_SEGMENT, src=src, sha=sha)},"
                     f"SKU-{h.upper()}")
        photos[h] = [(src, sha)]
    return "\n".join(lines) + "\n", photos


NAMES = {"v_tb_slab": "Tiger Black", "v_tb_tile": "Tiger Black", "v_tbm_slab": "Tiger Black", "v_nb_tile": "Navy Blue",
         "v_rb_slab": "Royal Beige", "v_rb_tile": "Royal Beige"}
VARIANTS = "Id,Key,Name\n" + "".join(f"{k},key_{k},{v}\n" for k, v in NAMES.items())
ATTRIBUTES = f"category,value,sourceid\nfinish,Polished,{POLISHED}\nfinish,Flamed,{FLAMED}\n"


def _products(sheet):
    return tile_plan.products(*sheet, tile_plan.variety_names(VARIANTS))


def test_a_flamed_tile_with_only_polished_slabs_gets_no_stone(categories):
    # Tiger Black Flamed: the variety's slabs are polished; a polished face would show the wrong surface
    everything = _products(_sheet(("tb-slab", "slab", 2.4, 1.6, 0.02, "v_tb_slab", GRANITE, POLISHED, "marenostone", A),
                                  ("tb-flamed", "tile", 0.6, 0.6, 0.02, "v_tb_tile", GRANITE, FLAMED, "marenostone", B)))
    tile = tile_plan.tiles(everything)[0]
    assert tile_plan.no_stone(tile, everything, {POLISHED, FLAMED}) == "no slab of this variety in this finish"


def test_a_tile_whose_variety_has_no_slab_at_all_gets_no_stone(categories):
    everything = _products(_sheet(("nb-honed", "tile", 0.6, 0.4, 0.018, "v_nb_tile", GRANITE, FLAMED, "marenostone", B)))
    assert tile_plan.no_stone(tile_plan.tiles(everything)[0], everything, {FLAMED}) == "no slab of this variety"


def test_the_same_name_of_another_stone_type_is_another_variety(categories):
    everything = _products(_sheet(("tbm-slab", "slab", 2.4, 1.6, 0.02, "v_tbm_slab", MARBLE, POLISHED, "marenostone", A),
                                  ("tb-tile", "tile", 0.6, 0.4, 0.02, "v_tb_tile", GRANITE, POLISHED, "marenostone", B)))
    assert tile_plan.no_stone(tile_plan.tiles(everything)[0], everything, {POLISHED}) == "no slab of this variety"


def _slab(listing, source, area_key):
    return tile_plan.Slab(listing, source, area_key[-1] * 64, (2.4, 1.6), area_key, "e")


def test_slabs_are_tried_the_tile_s_own_vendor_s_first_then_the_most_stone_and_never_a_refused_one():
    own_small, own_big, other_bigger = _slab("s1", "marenostone", "k1"), _slab("s2", "marenostone", "k2"), _slab("s3", "sentas", "k3")
    areas = {"k1": 2.0, "k2": 4.0, "k3": 9.0}
    assert tile_plan.ranked([own_small, other_bigger, own_big], "marenostone", areas) == [own_big, own_small, other_bigger]
    assert tile_plan.ranked([other_bigger], "marenostone", areas) == [other_bigger]      # any vendor's when it has none
    assert tile_plan.ranked([own_small, other_bigger], "marenostone", {"k3": 9.0}) == [other_bigger]   # own refused
    assert tile_plan.ranked([own_small], "marenostone", {}) == []                         # every slab refused


@pytest.mark.parametrize("size, faces", [((1.2, 1.2, 0.02), 2), ((2.0, 1.5, 0.02), 1)])   # straight, half-bond
def test_every_tile_leads_with_the_laid_floor_and_has_the_single_tile_and_studio_as_extras_however_few_its_faces(size, faces):
    # owner 2026-11-04: every tile's catalog image is the laid floor; the single tile on white, the studio view and the
    # 3D tile are extras. These sizes fit only once or twice on this slab's stone: the floor repeats those faces, turned.
    stone = _stone()
    product = tile_plan.Product("big", "marenostone", "tile", ("x", "y"), POLISHED, size)
    slab = tile_plan.Slab("s", "marenostone", A, (3.0, 2.0), "k", "e")
    cuts = tile_plan.faces_of(product, stone)
    assert len(cuts.origins) == faces
    files = tile_plan.render(product, slab, stone, cuts)
    sha = imagestore.listing_sha("big")
    main, white, studio = (files[imagestore.standard_key("marenostone", sha, v)][0] for v in ("main", "white", "studio"))
    assert list(files) == tile_plan.keys(product)
    assert main != white and main != studio                                          # main is the floor, not one tile


def test_a_listing_whose_size_is_no_tile_s_gets_no_stone(categories):
    everything = _products(_sheet(("rb-slab", "slab", 2.4, 1.6, 0.02, "v_rb_slab", MARBLE, POLISHED, "marenostone", A),
                                  ("rb-tile", "tile", 2.4, 1.6, 0.02, "v_rb_tile", MARBLE, POLISHED, "marenostone", B)))
    assert tile_plan.no_stone(tile_plan.tiles(everything)[0], everything, {POLISHED}).startswith("not a tile's size")


def test_a_tile_s_stamp_changes_exactly_with_its_slab_its_content_its_size_its_finish_and_the_renderer(monkeypatch):
    p = tile_plan.Product("t", "marenostone", "tile", ("x", "y"), POLISHED, (0.6, 0.4, 0.02))
    s = tile_plan.Slab("s", "marenostone", A, (2.4, 1.6), "k", "e1")
    base = tile_plan.stamp(p, s)
    assert tile_plan.stamp(p, s) == base
    assert tile_plan.stamp(p, replace(s, etag="e2")) != base
    assert tile_plan.stamp(p, replace(s, cutout="k2")) != base
    assert tile_plan.stamp(replace(p, size=(0.6, 0.6, 0.02)), s) != base
    assert tile_plan.stamp(replace(p, finish="f2"), s) != base
    monkeypatch.setattr(C, "TILE_RENDER_VERSION", C.TILE_RENDER_VERSION + 1)
    assert tile_plan.stamp(p, s) != base


# --- the tile step on a bucket -----------------------------------------------------------------------------------------
class _S3:
    def __init__(self, objects=None):
        self.objects, self.written = {}, []
        for k, v in (objects or {}).items():
            self._store(k, v)

    def _store(self, key, body, metadata=None):
        self.objects[key] = {"Body": body, "ETag": f'"{hashlib.md5(body).hexdigest()}"', "Metadata": metadata or {}}

    def put_object(self, Bucket, Key, Body, ContentType=None, Metadata=None):
        self.written.append(("put", Key))
        self._store(Key, Body, Metadata)

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key]["Body"])}

    def head_object(self, Bucket, Key):
        return {"ETag": self.objects[Key]["ETag"], "Metadata": self.objects[Key]["Metadata"]}

    def copy_object(self, Bucket, Key, CopySource, ContentType, MetadataDirective):
        self.written.append(("copy", Key))
        self.objects[Key] = dict(self.objects[CopySource["Key"]])

    def delete_object(self, Bucket, Key):
        self.written.append(("delete", Key))
        self.objects.pop(Key, None)

    def get_paginator(self, name):
        objects = self.objects

        class _Pages:
            def paginate(self, Bucket, Prefix, Delimiter=None):
                keys = [k for k in sorted(objects) if k.startswith(Prefix)]
                if Delimiter:                          # the folders right under Prefix, and the keys there
                    folders = sorted({Prefix + k[len(Prefix):].split(Delimiter)[0] + Delimiter for k in keys
                                      if Delimiter in k[len(Prefix):]})
                    return [{"CommonPrefixes": [{"Prefix": f} for f in folders],
                             "Contents": [{"Key": k, "ETag": objects[k]["ETag"]} for k in keys if Delimiter not in k[len(Prefix):]]}]
                return [{"Contents": [{"Key": k, "ETag": objects[k]["ETag"]} for k in keys]}]
        return _Pages()


SHEET_ROWS = (("rb-slab", "slab", 2.4, 1.6, 0.02, "v_rb_slab", MARBLE, POLISHED, "marenostone", A),
              ("tb-slab", "slab", 2.3, 1.5, 0.02, "v_tb_slab", GRANITE, POLISHED, "marenostone", D),
              ("rb-tile", "tile", 0.6, 0.4, 0.02, "v_rb_tile", MARBLE, POLISHED, "marenostone", B),
              ("tb-flamed", "tile", 0.6, 0.6, 0.02, "v_tb_tile", GRANITE, FLAMED, "marenostone", D))   # Tiger Black Flamed


def _png(rgba):
    ok, buf = cv2.imencode(".png", cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA))
    assert ok
    return buf.tobytes()


def _bucket(slab_rgba):
    """The slab rb-slab chosen (its run saved the clean slab: publish makes its 3D cut-out); the tile listing rb-tile
    chosen before tiles were rendered (its own photo B published); the Medusa exports."""
    std = {imagestore.standard_key("marenostone", s, "main"): b"m" + s.encode() for s in (A, B)}
    return _S3({
        f"{ROOT}chosen/rb-slab.json": json.dumps({"key": "rb-slab", "source": "marenostone", "sha": A, "prefix": RUN}).encode(),
        f"{ROOT}chosen/rb-tile.json": json.dumps({"key": "rb-tile", "source": "marenostone", "sha": B, "prefix": RUN}).encode(),
        f"{RUN}{A}.main.jpg": b"m" + A.encode(), f"{RUN}{B}.main.jpg": b"m" + B.encode(), f"{RUN}{A}.slab.png": _png(slab_rgba),
        f"{ENV_SEGMENT}/{C.VARIANTS_KEY}": VARIANTS.encode(), f"{ENV_SEGMENT}/{C.ATTRIBUTES_KEY}": ATTRIBUTES.encode(),
        **std})


def _publish(s3):
    return pub.publish(s3, ROOT, *_sheet(*SHEET_ROWS), say=lambda m: None, assigned={}, held=frozenset())


def test_a_tile_listing_is_shown_from_its_variety_s_slab_stone_not_its_own_photo(categories, monkeypatch):
    # the slab's cut-out is kept as it is (its run saved no slab.png here): only the tile step works
    s3 = _bucket(_stone_rgba(960, 640))
    out = _publish(s3)
    sha = imagestore.listing_sha("rb-tile")
    files = tile_plan.keys(tile_plan.Product("rb-tile", "marenostone", "tile", None, POLISHED, None))
    assert all(k in s3.objects for k in files)
    record = json.loads(s3.objects[imagestore.tile_record_key("marenostone", sha)]["Body"])
    assert record["stone"]["sha"] == A and record["faces"] > 0
    assert imagestore.standard_key("marenostone", B, "main") not in s3.objects   # the tile's photo is no longer published
    assert imagestore.standard_key("marenostone", A, "main") in s3.objects       # the slab's still is
    puts = [k for op, k in s3.written if op == "put" and sha in k]
    assert puts[-2:] == [files[0], files[-1]]                                    # main, then the record, last
    assert out["tiles"] == {"listings": 2, "shown": 1, "rendered": 1, "hidden": 1, "failed": 0, "failed_listings": [],
                            "unknown_variations": 0}
    assert out["tiles_hidden"] == {"tb-flamed": "no slab of this variety in this finish"}
    assert out["changed"]


def test_the_tile_step_renders_again_exactly_when_an_input_changed(categories):
    s3 = _bucket(_stone_rgba(960, 640))
    _publish(s3)
    s3.written.clear()
    again = _publish(s3)
    assert s3.written == [] and not again["changed"]                             # nothing changed: nothing written
    s3._store(f"{RUN}{A}.slab.png", _png(_stone_rgba(960, 640, seed=2)))         # the slab's run made its slab again
    out = _publish(s3)
    assert out["made_3d"] == 1 and out["tiles"]["rendered"] == 1 and out["changed"]


def test_a_tile_renders_byte_identical_every_time():
    stone = _stone()
    product = tile_plan.Product("t", "marenostone", "tile", ("x", "y"), POLISHED, (0.6, 0.4, 0.02))
    slab = tile_plan.Slab("s", "marenostone", A, (3.0, 2.0), "k", "e")
    cuts = tile_plan.faces_of(product, stone)
    first = tile_plan.render(product, slab, stone, cuts)
    assert tile_plan.render(product, slab, stone, cuts) == first
    assert list(first) == tile_plan.keys(product)


def test_a_slab_with_a_crosshatch_leaves_its_tiles_hidden(categories):
    mesh = cv2.resize(_fixture("crosshatch_805cf5ac.webp"), (960, 640), interpolation=cv2.INTER_NEAREST)
    s3 = _bucket(mesh)
    out = _publish(s3)
    assert "crosshatch" in out["tiles_hidden"]["rb-tile"]
    assert not any(imagestore.listing_sha("rb-tile") in k for k in s3.objects)


def test_tile_files_no_longer_wanted_are_removed(categories):
    s3 = _bucket(_stone_rgba(960, 640))
    _publish(s3)
    del s3.objects[f"{RUN}{A}.slab.png"]                                          # the slab's photo is no whole slab now
    out = _publish(s3)
    assert out["tiles_hidden"]["rb-tile"] == "no published slab stone (3D cut-out) of this variety in this finish"
    assert not any(imagestore.listing_sha("rb-tile") in k for k in s3.objects)


def _two_slab_bucket(first, second):
    """Royal Beige with two chosen slabs (A, E) whose cut-outs publish makes from their clean slabs."""
    s3 = _bucket(first)
    s3._store(f"{ROOT}chosen/rb-slab2.json",
              json.dumps({"key": "rb-slab2", "source": "marenostone", "sha": E, "prefix": RUN}).encode())
    s3._store(f"{RUN}{E}.main.jpg", b"m" + E.encode())
    s3._store(f"{RUN}{E}.slab.png", _png(second))
    return s3


TWO_SLABS = SHEET_ROWS + (("rb-slab2", "slab", 2.4, 1.6, 0.02, "v_rb_slab", MARBLE, POLISHED, "marenostone", E),)


def test_a_tile_too_big_for_the_first_slab_is_cut_from_the_next(categories):
    # A: the most stone (2.4 x 1.6 m), but cut in four by two gaps, so no 1.5 x 1.1 m tile fits on it; E: a smaller
    # (2.0 x 1.4 m) whole slab
    cut_in_four = _stone_rgba(960, 640)
    cut_in_four[:, 300:310, 3] = 0
    cut_in_four[300:310, :, 3] = 0
    s3 = _two_slab_bucket(cut_in_four, _stone_rgba(960, 672, seed=3))
    rows = (SHEET_ROWS[0], SHEET_ROWS[1], ("rb-tile", "tile", 1.5, 1.1, 0.02, "v_rb_tile", MARBLE, POLISHED, "marenostone", B),
            ("rb-slab2", "slab", 2.0, 1.4, 0.02, "v_rb_slab", MARBLE, POLISHED, "marenostone", E))
    out = pub.publish(s3, ROOT, *_sheet(*rows), say=lambda m: None, assigned={}, held=frozenset())
    record = json.loads(s3.objects[imagestore.tile_record_key("marenostone", imagestore.listing_sha("rb-tile"))]["Body"])
    assert out["tiles"]["shown"] == 1 and record["stone"]["sha"] == E


def test_publish_with_only_tile_listings_chosen_is_refused_and_removes_nothing(categories):
    s3 = _bucket(_stone_rgba(960, 640))
    del s3.objects[f"{ROOT}chosen/rb-slab.json"]                                  # only the tile listing's record is left
    before = dict(s3.objects)
    with pytest.raises(pub.NoChoiceRecords):
        _publish(s3)
    assert s3.objects == before


def _rerender_breaks(s3, monkeypatch, error):
    """A first publish renders rb-tile; then the slab's view and stone change (the tile must render again) and the
    renderer raises `error`."""
    _publish(s3)
    s3._store(f"{RUN}{A}.main.jpg", b"new view of the slab")
    s3._store(f"{RUN}{A}.slab.png", _png(_stone_rgba(960, 640, seed=5)))

    def broken(*args):
        raise error
    monkeypatch.setattr(tile_plan, "render", broken)


def test_a_tile_step_raises_nothing_deleted_slabs_published_problem_listed(categories, monkeypatch):
    s3 = _bucket(_stone_rgba(960, 640))
    _rerender_breaks(s3, monkeypatch, OSError("disk full"))
    tile_files = {k for k in s3.objects if imagestore.listing_sha("rb-tile") in k}
    out = _publish(s3)
    assert tile_files <= set(s3.objects)                                           # its stone is still A: it keeps them
    assert s3.objects[imagestore.standard_key("marenostone", A, "main")]["Body"] == b"new view of the slab"
    assert out["tiles_problems"] == {"rb-tile": "OSError: disk full"}
    assert out["tiles"]["failed"] == 1 and out["tiles"]["failed_listings"] == ["rb-tile"]   # in the run record


def test_an_unexpected_error_in_a_tile_step_fails_the_run_before_anything_is_removed(categories, monkeypatch):
    s3 = _bucket(_stone_rgba(960, 640))
    _rerender_breaks(s3, monkeypatch, RuntimeError("a bug"))
    s3._store(imagestore.standard_key("marenostone", D, "main"), b"no longer chosen")
    with pytest.raises(RuntimeError, match="a bug"):
        _publish(s3)
    assert imagestore.standard_key("marenostone", D, "main") in s3.objects        # nothing removed


def test_a_failed_tile_whose_stone_is_no_longer_its_to_cut_loses_its_files(categories, monkeypatch):
    s3 = _two_slab_bucket(_stone_rgba(960, 640), _stone_rgba(960, 672, seed=3))
    rows = SHEET_ROWS + (("rb-slab2", "slab", 2.0, 1.4, 0.02, "v_rb_slab", MARBLE, POLISHED, "marenostone", E),)
    pub.publish(s3, ROOT, *_sheet(*rows), say=lambda m: None, assigned={}, held=frozenset())
    record = imagestore.tile_record_key("marenostone", imagestore.listing_sha("rb-tile"))
    assert json.loads(s3.objects[record]["Body"])["stone"]["sha"] == A
    mesh = cv2.resize(_fixture("crosshatch_805cf5ac.webp"), (960, 640), interpolation=cv2.INTER_NEAREST)
    s3._store(f"{RUN}{A}.slab.png", _png(mesh))                                   # A is refused now: the tile's stone is E

    def broken(*args):
        raise OSError("disk full")
    monkeypatch.setattr(tile_plan, "render", broken)
    out = pub.publish(s3, ROOT, *_sheet(*rows), say=lambda m: None, assigned={}, held=frozenset())
    assert out["tiles"]["failed_listings"] == ["rb-tile"]
    assert not any(imagestore.listing_sha("rb-tile") in k for k in s3.objects)     # hidden until it renders again


class _GetOutage(_S3):
    """The bucket lists, but reading under the given prefixes fails (S3 erroring, not a missing key)."""

    def __init__(self, objects, failing):
        super().__init__()
        self.objects, self.failing = objects, failing

    def get_object(self, Bucket, Key):
        if Key.startswith(self.failing):
            raise ClientError({"Error": {"Code": "InternalError"}}, "GetObject")
        return super().get_object(Bucket, Key)


def test_an_s3_read_outage_keeps_every_tile_s_files(categories):
    s3 = _bucket(_stone_rgba(960, 640))
    _publish(s3)
    tile_files = {k for k in s3.objects if imagestore.listing_sha("rb-tile") in k}
    out_of_reach = (imagestore.cutout_prefix(), imagestore.standard_prefix(imagestore.TILE_SUBDIR))
    outage = _GetOutage(s3.objects, out_of_reach)
    out = _publish(outage)
    assert tile_files <= set(outage.objects) and out["removed"] == 0             # unknown is not gone: nothing deleted
    assert out["tiles"]["failed_listings"] == ["rb-tile"]


def test_a_failed_tile_whose_record_is_missing_loses_its_files(categories, monkeypatch):
    s3 = _bucket(_stone_rgba(960, 640))
    _publish(s3)
    del s3.objects[imagestore.tile_record_key("marenostone", imagestore.listing_sha("rb-tile"))]   # NoSuchKey

    def broken(*args):
        raise OSError("disk full")
    monkeypatch.setattr(tile_plan, "render", broken)
    out = _publish(s3)
    assert out["tiles"]["failed_listings"] == ["rb-tile"]
    assert not any(imagestore.listing_sha("rb-tile") in k for k in s3.objects)


def test_a_corrupt_tile_record_means_out_of_date_the_tile_renders_again(categories):
    s3 = _bucket(_stone_rgba(960, 640))
    _publish(s3)
    record = imagestore.tile_record_key("marenostone", imagestore.listing_sha("rb-tile"))
    s3._store(record, b"{not json")
    out = _publish(s3)
    assert out["tiles"]["rendered"] == 1 and out["tiles"]["failed"] == 0
    assert out["tiles_problems"] == {"rb-tile": "its record could not be read: rendered again"}
    assert json.loads(s3.objects[record]["Body"])["stone"]["sha"] == A


def test_only_one_slab_s_stone_is_decoded_at_a_time(categories, monkeypatch):
    import weakref
    s3 = _two_slab_bucket(_stone_rgba(960, 640), _stone_rgba(960, 640, seed=3))
    live, most = [], []
    real = pub._stone

    def counted(client, slab):
        most.append(sum(ref() is not None for ref in live))                        # stones alive when one more is decoded
        stone = real(client, slab)
        live.append(weakref.ref(stone))
        return stone
    monkeypatch.setattr(pub, "_stone", counted)
    out = pub.publish(s3, ROOT, *_sheet(*TWO_SLABS), say=lambda m: None, assigned={}, held=frozenset())
    assert out["tiles"]["rendered"] == 1
    assert len(most) == 3 and max(most) == 0                                       # two checks, then the pick again


def test_a_tile_s_problem_is_on_the_run_s_problem_list(monkeypatch):
    import time
    from stone_pipeline.photos import select as select
    s3 = _S3()
    monkeypatch.setattr(proc, "sheet_and_photos", lambda client: ("", {}))
    monkeypatch.setattr(proc, "photo_listings", lambda sheet, photos: [])
    monkeypatch.setattr(proc.gpu, "clear_leftovers", lambda batch, run_id: [])
    monkeypatch.setattr(proc, "choices", lambda client, root: ({}, {}))
    # the real shapes: run's report always carries the review rows (select.report), and publish's hidden tiles are tile
    # listings of the same sheet (proc.tile_listings), so the review list can name each one
    monkeypatch.setattr(proc, "run", lambda *a, **k: ([], {"chosen": [], "tracked": [], "pending": [], "click_problems": [],
                                                           "review": {}}))
    monkeypatch.setattr(proc, "tile_listings", lambda sheet, photos: [{"key": "x", "sku": "MAR-X", "source": "marenostone",
                                                                       "format": "tile", "photos": []}])
    monkeypatch.setattr(proc.publish, "publish", lambda *a, **k: {"changed": True, "tiles_hidden": {"x": "no slab"},
                                                             "tiles_problems": {"t": "RuntimeError: boom"}})
    summary = proc.process(s3, None, select.Spend(5.0), None, "R1", time.monotonic() + 60, say=lambda m: None)
    kept = json.loads(s3.objects[f"{ROOT}summary.json"]["Body"])
    assert summary["problems"] == 1 and kept["problem_list"] == ["tile t: RuntimeError: boom"]
    assert kept["tiles_hidden"] == {"x": "no slab"}


def test_the_photo_process_never_takes_a_tile_listing(categories):
    assert [l["key"] for l in proc.photo_listings(*_sheet(*SHEET_ROWS))] == ["rb-slab", "tb-slab"]


def test_a_tile_listing_s_old_choice_is_never_sent_to_the_gpu_again(categories):
    """Its photo is no stone of its own any more (it is rendered from its variety's slab): an older outline of it is never
    rechecked."""
    records = {h: {"key": h, "sha": sha, "source": "marenostone", "tag": "v2"} for h, sha in (("rb-slab", A), ("rb-tile", B))}
    decided = proc.Decisions(set(), {}, {}, {}, [], {}, {}, {})
    live = proc.live_choices(proc.photo_listings(*_sheet(*SHEET_ROWS)), records, set(), decided)
    assert live == [("v2", "marenostone", A)]


# --- the image stage ---------------------------------------------------------------------------------------------------
PUBLIC = f"https://bucket.s3.amazonaws.com/{ENV_SEGMENT}/products/"


def _row(handle, fmt="Tile"):
    return CanonicalRow(src_site="marenostone", surrogate_key=handle, handle=handle, format_value=fmt,
                        image_keys=[f"{PUBLIC}improved/marenostone/{B}.jpg"])


def test_a_tile_product_links_the_set_named_after_it_and_its_3d_tile(monkeypatch):
    own = imagestore.listing_sha("rb-tile")
    views = {"main": {own: "m"}, "white": {own: "w"}, "studio": {own: "s"},
             imagestore.CUTOUT_SUBDIR: {own: "t+j"}}
    monkeypatch.setattr(images, "_load_standard_views", lambda cfg: views)
    monkeypatch.setattr(images, "_load_owner_removed", lambda cfg: set())
    monkeypatch.setattr(images, "_load_assigned", lambda cfg: {})
    monkeypatch.setattr(images, "_link_from_manifest", lambda rows, cfg, stats: None)
    rows = [_row("rb-tile"), _row("tb-flamed")]
    cfg = type("Cfg", (), {"mode": "passthrough", "public_base": PUBLIC})()
    images.run(rows, cfg=cfg)
    images.link_standard(rows, rows, cfg=cfg, image_pipeline=True)
    shown, hidden = rows
    assert shown.standard_photo is True and hidden.standard_photo is False      # no set: hidden by the no-photo rule
    assert shown.thumbnail_key == f"{PUBLIC}standard/main/marenostone/{own}.jpg"
    assert shown.model_3d_key == f"{PUBLIC}standard/cutout/marenostone/{own}.webp"
    assert shown.standard_version == "m w s t+j"


def _run(rows, views, monkeypatch):
    monkeypatch.setattr(images, "_load_standard_views", lambda cfg: views)
    monkeypatch.setattr(images, "_load_owner_removed", lambda cfg: set())
    monkeypatch.setattr(images, "_load_assigned", lambda cfg: {})
    monkeypatch.setattr(images, "_link_from_manifest", lambda rows, cfg, stats: None)
    cfg = type("Cfg", (), {"mode": "passthrough", "public_base": PUBLIC})()
    images.run(rows, cfg=cfg)
    images.link_standard(rows, rows, cfg=cfg, image_pipeline=True)


def test_a_tile_whose_size_was_filled_from_the_pack_s_defaults_is_never_shown(monkeypatch):
    # 60 x 60 x 2 is the pack's tile default and a common real size: only the row's provenance tells them apart
    from stone_pipeline.config.settings import Confidence
    from stone_pipeline.core.schema import FlagCode, ReviewFlag
    own = imagestore.listing_sha("rb-tile")
    views = {"main": {own: "m"}, "studio": {own: "s"}}
    real, defaulted = _row("rb-tile"), _row("rb-tile")
    defaulted.add_flag(ReviewFlag(field="width", code=FlagCode.dimension_defaulted, confidence=Confidence.low,
                                  method="pack_default"))
    _run([real, defaulted], views, monkeypatch)
    assert real.standard_photo is True and defaulted.standard_photo is False


def test_a_tile_without_its_own_set_never_takes_its_photo_s_set(monkeypatch):
    # a hidden Tiger Black Flamed tile sharing photo B with a polished slab: it must not go live with that slab's views
    views = {v: {B: v} for v in imagestore.STANDARD_VIEWS} | {imagestore.CUTOUT_SUBDIR: {B: "t+j"}}
    row = _row("tb-flamed")
    _run([row], views, monkeypatch)
    assert row.standard_photo is False and row.model_3d_key is None and row.standard_version is None
    assert row.thumbnail_key == f"{PUBLIC}improved/marenostone/{B}.jpg"         # its own photo, hidden


def test_any_other_product_ignores_a_set_named_after_its_listing(monkeypatch):
    # a handle that moved from tile to slab: the slab is shown from its own photo, never the old tile render
    own = imagestore.listing_sha("rb-tile")
    views = {"main": {own: "m0", B: "m"}, "studio": {own: "s0", B: "s"}, imagestore.CUTOUT_SUBDIR: {own: "t+j"}}
    slab = _row("rb-tile", fmt="Slab")
    _run([slab], views, monkeypatch)
    assert slab.thumbnail_key == f"{PUBLIC}standard/main/marenostone/{B}.jpg" and slab.model_3d_key is None
    alone = _row("rb-tile", fmt="Slab")
    _run([alone], {"main": {own: "m0"}}, monkeypatch)
    assert alone.standard_photo is False


def test_a_listing_s_name_is_shaped_like_a_photo_s_and_apart_from_every_photo_s():
    sha = imagestore.listing_sha("rb-tile")
    assert len(sha) == 64 and imagestore.sha_from_url(imagestore.standard_key("x", sha, "main")) == sha
    assert sha != hashlib.sha256(b"rb-tile").hexdigest()
