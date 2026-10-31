"""The standardiser's rules, on synthetic images (no model, no GPU): the same canvas on every image, a room that cannot
trace a vendor, real chips kept, flares told from veins, a logo removed only when it can be trusted."""
import cv2
import numpy as np
import pytest

from stone_pipeline.config import settings
from stone_pipeline.config.settings import STANDARDISE as C
from stone_pipeline.standardise import blend, index, kinds, layout, logos, marks, retouch, views


def _stone(h, w, seed=1, base=170, var=18):
    rng = np.random.default_rng(seed)
    noise = cv2.GaussianBlur(rng.standard_normal((h, w)).astype(np.float32), (0, 0), 3) * var * 3
    grey = np.clip(base + noise, 0, 255).astype(np.uint8)
    return np.dstack([grey, grey, np.clip(grey.astype(np.int16) - 8, 0, 255).astype(np.uint8)])


def _slab(h, w):
    return _stone(h, w), np.ones((h, w), np.float32)


@pytest.mark.parametrize("h,w", [(800, 1400), (1000, 1000), (500, 1900), (1200, 900)])
def test_every_slab_shape_gives_the_same_canvas(h, w):
    assert layout.compose([_slab(h, w)], "p").shape == (C.OUT_H, C.OUT_W, 3)


def test_an_open_book_gives_the_same_canvas():
    assert layout.compose([_slab(800, 1000), _slab(800, 1000)], "p").shape == (C.OUT_H, C.OUT_W, 3)


def test_the_canvas_is_the_storefronts_three_to_two():
    assert abs(C.OUT_W / C.OUT_H - 3 / 2) < 0.001


def test_the_same_input_gives_the_same_image():
    a = layout.compose([_slab(800, 1400)], "p")
    assert np.array_equal(a, layout.compose([_slab(800, 1400)], "p"))


def test_the_room_comes_from_the_product_alone():
    assert np.array_equal(layout.room_for("handle-a", False), layout.room_for("handle-a", False))
    seen = {hash(layout.room_for(f"handle-{i}", False).tobytes()) for i in range(40)}
    assert len(seen) == C.ROOM_COUNT


def test_room_for_takes_no_vendor_or_origin():
    import inspect
    assert list(inspect.signature(layout.room_for).parameters) == ["key", "wide"]


def test_a_neighbours_strip_along_a_side_is_cut():
    stone = _stone(800, 1200, base=150, var=4)
    stone[:, :20] = (95, 125, 110)                     # a neighbour's grey-green side inside the outline
    alpha = layout.trim_edges(stone, np.ones((800, 1200), np.float32))
    assert alpha[400, 5] == 0 and alpha[400, 100] == 1


def test_a_short_vein_at_the_edge_leaves_no_nick():
    stone = _stone(800, 1200, base=150, var=4)
    stone[380:420, :24] = (70, 40, 40)                 # a dark vein meeting the left edge
    alpha = layout.trim_edges(stone, np.ones((800, 1200), np.float32))
    assert alpha[400, 3] == 1


def test_plain_stone_loses_nothing_at_its_edges():
    alpha = layout.trim_edges(_stone(800, 1200, base=150, var=4), np.ones((800, 1200), np.float32))
    assert alpha.mean() > 0.995


def test_a_labels_square_bite_is_filled_but_a_strip_along_a_side_is_not():
    alpha = np.ones((800, 1200), np.float32)
    alpha[300:340, 1170:] = 0                          # a bite in the right edge
    alpha[:, :6] = 0                                   # a strip along the whole left edge
    filled = layout._fill_bites(alpha)
    assert filled[320, 1190] == 1 and filled[400, 2] == 0


def test_a_card_erased_on_the_slabs_edge_leaves_no_bite():
    outline = np.ones((800, 1200), np.uint8)
    outline[:60, 500:800] = 0                          # SAM saw the card on the top edge as not stone
    inside = np.ones((800, 1200), np.uint8)
    marks = np.zeros((800, 1200), np.uint8)
    marks[:70, 490:810] = 1                            # the card, erased and filled with stone
    assert layout.outline_with_erased(outline, marks, inside)[30, 650] == 1


def test_a_mark_outside_the_slab_does_not_grow_it():
    outline = np.zeros((800, 1200), np.uint8)
    outline[100:700, 100:1100] = 1
    inside = outline.copy()
    marks = np.zeros((800, 1200), np.uint8)
    marks[20:60, 300:500] = 1                          # a painted code on the floor
    assert layout.outline_with_erased(outline, marks, inside)[40, 400] == 0


def test_a_real_corner_chip_stays():
    alpha = np.ones((800, 1200), np.float32)
    cv2.fillConvexPoly(alpha, np.array([[0, 700], [100, 799], [0, 799]]), 0)
    assert layout._fill_bites(alpha)[790, 5] == 0


def _canvas_masks():
    solid = np.zeros((C.OUT_H, C.OUT_W), np.uint8)
    solid[100:1100, 200:1800] = 1
    stand = np.zeros_like(solid)
    stand[1080:1150, 400:470] = 1
    return solid, stand


def test_the_repaint_never_touches_the_stone_or_the_stand():
    solid, stand = _canvas_masks()
    k = blend.weights(solid | stand, stand)
    assert k[600, 1000] == 0 and k[1120, 430] == 0


def test_the_repaint_fades_out_across_the_floor():
    solid, stand = _canvas_masks()
    k = blend.weights(solid | stand, stand)
    assert k[600, 1800 + 2] > 0.5 and k[600, 1800 + C.BAND_OUT + 10] == 0


def test_a_chip_in_the_slab_is_not_repainted():
    solid, stand = _canvas_masks()
    solid[600:680, 1760:1800] = 0                      # a chip in the right edge
    k = blend.weights(solid | stand, stand)
    assert k[640, 1790] < 0.3


def test_a_blown_compact_reflection_is_a_flare_and_a_white_vein_is_not():
    stone = _stone(1000, 1500, base=150, var=6)
    cv2.rectangle(stone, (300, 200), (360, 240), (255, 255, 255), -1)
    cv2.line(stone, (700, 100), (1200, 900), (250, 250, 250), 2)
    found = marks.flare_mask(stone, np.ones((1000, 1500), np.uint8))
    assert found[220, 330] == 1 and found[500, 950] == 0


def test_many_white_pieces_are_the_stone_not_flares():
    stone = _stone(1000, 1500, base=150, var=6)
    for x in range(100, 1400, 90):
        cv2.rectangle(stone, (x, 300), (x + 40, 340), (255, 255, 255), -1)
    assert marks.flare_mask(stone, np.ones((1000, 1500), np.uint8)).sum() == 0


def _stamped():
    clean = _stone(600, 1024, seed=3, base=175, var=12)
    raw = clean.copy()
    cv2.putText(raw, "LOGO", (400, 320), cv2.FONT_HERSHEY_SIMPLEX, 2.5, (235, 40, 130), 8)
    return raw, clean


def test_a_logo_is_removed_and_the_rest_stays_the_originals():
    raw, clean = _stamped()
    out, reason = logos.remove(raw, clean)
    assert reason == "" and logos._pink_left(out) <= C.LOGO_MAX_LEFT and logos._pink_left(raw) > C.LOGO_MAX_LEFT
    assert np.array_equal(out[:60], raw[:60])


def test_a_copy_that_does_not_line_up_is_held_never_guessed():
    raw, _ = _stamped()
    out, reason = logos.remove(raw, _stone(600, 1024, seed=9, base=90, var=30))
    assert out is None and reason.startswith("logo:")


@pytest.mark.parametrize("kind,conf,skipped", [("pile", 0.9, True), ("hangar", 0.7, True), ("interior", 0.8, True),
                                               ("pile", 0.4, False), ("slab", 0.99, False), ("close-up", 0.9, False)])
def test_only_a_sure_pile_hangar_or_interior_is_skipped(kind, conf, skipped):
    assert kinds.is_skipped(kind, conf) is skipped


def test_an_angled_view_keeps_the_canvas_and_the_floor_line():
    img, solid, stand = views.angled([_slab(800, 1400)], "p", "left")
    assert img.shape == (C.OUT_H, C.OUT_W, 3)
    bottom = np.where(solid[:, C.OUT_W // 2])[0].max()
    assert abs(bottom - C.OUT_H * (1 - C.FLOOR_SHARE)) < 0.12 * C.OUT_H


def test_a_close_up_is_only_of_real_stone():
    slab, alpha = _slab(800, 1400)
    alpha[350:450, 650:750] = 0
    assert views.close_up(slab, alpha, None) is None


SHEET = """Product Handle,Product Length,Product Width,Product Category 1,Front Image,Product Image 1
one,3.0,2.0,pcat_01M1GWN1V1E66J1NM1PTQ3ESAK,https://x/prod/products/improved/zucchi/{a}.jpg,https://x/prod/products/improved/zucchi/{b}.jpg
two,,,pcat_01M1GWN1V1E66J1NM1PTQ3ESAK,https://x/prod/products/improved/zucchi/{a}.jpg,
""".format(a="a" * 64, b="b" * 64)


@pytest.fixture
def _active_slab(monkeypatch):
    from dataclasses import replace
    slab = settings.category("slab")
    monkeypatch.setattr(settings, "CATEGORIES", (replace(slab, pcat_id="pcat_01M1GWN1V1E66J1NM1PTQ3ESAK"),))


def test_the_index_links_a_photo_to_its_first_product(_active_slab):
    photos = index.build(SHEET)
    assert photos["a" * 64]["key"] == "one" and photos["a" * 64]["size"] == (3.0, 2.0) and photos["b" * 64]["format"] == "slab"


def test_an_unknown_category_fails_loud(_active_slab):
    with pytest.raises(ValueError, match="unknown product category"):
        index.build(SHEET.replace("pcat_01M1GWN1V1E66J1NM1PTQ3ESAK", "pcat_new"))


def _whole(shape=(800, 1200)):
    return np.ones(shape, np.uint8)


def _strap_edit():
    stone = _stone(800, 1200, base=160, var=8)
    with_strap = stone.copy()
    with_strap[:, 300:360] = (120, 80, 50)
    mask = np.zeros((800, 1200), np.uint8)
    mask[:, 290:370] = 1
    return with_strap, stone, mask


def test_a_clean_edit_is_taken_only_inside_the_found_area():
    original, edit, mask = _strap_edit()
    original[100:140, 700:760] = (10, 200, 10)         # outside the mask: must stay the real photo
    merged, reason = retouch.merge(original, edit, mask, _whole(), _whole(), 1200)
    assert reason == ""
    assert np.array_equal(merged[120, 730], original[120, 730]) and merged[400, 330, 0] > 130


def test_a_repainted_stone_is_refused():
    original, _, mask = _strap_edit()
    repainted = _stone(800, 1200, seed=5, base=60, var=20)
    merged, reason = retouch.merge(original, repainted, mask, _whole(), _whole(), 1200)
    assert merged is None and "recoloured" in reason


def test_an_invented_busy_fill_is_refused():
    original, edit, mask = _strap_edit()
    rng = np.random.default_rng(1)
    edit[:, 290:370] = rng.integers(0, 255, (800, 80, 3), dtype=np.uint8)
    merged, reason = retouch.merge(original, edit, mask, _whole(), _whole(), 1200)
    assert merged is None and "does not match" in reason


def test_a_fill_brighter_than_the_stone_leaves_no_band():
    original, edit, mask = _strap_edit()
    edit[:, 290:370] = np.clip(edit[:, 290:370].astype(np.int16) + 25, 0, 255).astype(np.uint8)
    merged, reason = retouch.merge(original, edit, mask, _whole(), _whole(), 1200)
    assert reason == ""
    inside, beside = merged[:, 300:360].astype(np.float32).mean(), merged[:, 240:280].astype(np.float32).mean()
    assert abs(inside - beside) < 4


def test_an_even_drift_of_the_whole_edit_is_not_a_repaint():
    original, edit, mask = _strap_edit()
    drifted = np.clip(edit.astype(np.int16) + 15, 0, 255).astype(np.uint8)
    merged, reason = retouch.merge(original, drifted, mask, _whole(), _whole(), 1200)
    assert reason == "" and abs(merged[:, 300:360].astype(np.float32).mean() - merged[:, 240:280].astype(np.float32).mean()) < 4


def test_a_smeared_flat_fill_is_refused():
    rng = np.random.default_rng(3)
    original = np.clip(rng.normal(150, 30, (800, 1200, 3)), 0, 255).astype(np.uint8)
    mask = np.zeros((800, 1200), np.uint8); mask[:, 290:370] = 1
    edit = original.copy()
    edit[:, 290:370] = original.reshape(-1, 3).mean(axis=0).astype(np.uint8)
    merged, reason = retouch.merge(original, edit, mask, _whole(), _whole(), 1200)
    assert merged is None and "does not match" in reason


def test_each_mark_gets_its_own_close_crop():
    mask = np.zeros((1200, 1800), np.uint8)
    mask[100:1100, 200:260] = 1
    mask[100:1100, 1500:1560] = 1
    boxes = retouch.regions(mask)
    assert len(boxes) == 2
    for x0, y0, x1, y1 in boxes:
        assert (x1 - x0) * (y1 - y0) < 0.6 * mask.size and (y1 - y0) <= C.RETOUCH_MAX_ASPECT * (x1 - x0)
        assert mask[y0:y1, x0:x1].any() and not mask[y0:y1, x0:x1].all()
    assert sum(int(mask[b[1]:b[3], b[0]:b[2]].sum()) for b in boxes) == int(mask.sum())


def test_the_model_sees_multiples_of_16_and_small_marks_larger():
    w, h = retouch.model_size(400, 400)
    assert w % 16 == 0 and h % 16 == 0 and w > 400 and w <= C.RETOUCH_MAX_SCALE * 400
    w, h = retouch.model_size(3000, 2000)
    assert w % 16 == 0 and h % 16 == 0 and w * h <= C.EDIT_PIXELS


def test_only_the_crops_go_to_the_model_and_the_rest_stays_the_photo():
    original, clean, mask = _strap_edit()
    seen = []
    x0, y0, x1, y1 = retouch.regions(mask)[0]

    def edit(crop, seed):
        seen.append(crop.shape)
        return clean[y0:y1, x0:x1]

    out, reason = retouch.retouch(original, mask, _whole(), _whole(), edit)
    assert reason == "" and seen == [(y1 - y0, x1 - x0, 3)]
    assert np.array_equal(out[:, :x0], original[:, :x0]) and out[400, 330, 0] > 130


def test_a_refused_try_is_tried_again_with_the_next_seed():
    original, clean, mask = _strap_edit()
    x0, y0, x1, y1 = retouch.regions(mask)[0]
    seeds = []

    def edit(crop, seed):                              # the first draw recolours the stone, the second is faithful
        seeds.append(seed)
        return np.zeros_like(crop) if len(seeds) == 1 else clean[y0:y1, x0:x1]

    out, reason = retouch.retouch(original, mask, _whole(), _whole(), edit)
    assert reason == "" and seeds == list(C.RETOUCH_SEEDS[:2]) and out[400, 330, 0] > 130


def test_a_mark_refused_on_every_seed_refuses_the_photo():
    original, _, mask = _strap_edit()
    seeds = []
    out, reason = retouch.retouch(original, mask, _whole(), _whole(), lambda crop, seed: seeds.append(seed) or np.zeros_like(crop))
    assert out is None and reason and seeds == list(C.RETOUCH_SEEDS)


def _with_box(stone):                                           # the slab as straps count it: the stone and the detector's box
    slab = retouch._slab(stone)
    slab[100:700, 100:1100] = 1
    return slab


def test_a_mark_in_a_notch_of_the_slab_counts_as_on_it():
    stone = np.zeros((800, 1200), np.uint8); stone[100:700, 100:1100] = 1
    stone[500:700, 500:800] = 0                               # the photo's rack stands in front of the slab
    rack = np.zeros_like(stone); rack[500:700, 500:800] = 1
    assert retouch.keep_on_slab(rack, retouch._slab(stone), C.CARD_ON_SLAB).any()


def test_a_strap_off_the_slab_is_no_strap_but_one_sticking_out_above_it_is():
    stone = np.zeros((800, 1200), np.uint8); stone[100:700, 100:1100] = 1
    beside = np.zeros_like(stone); beside[50:750, 1120:1180] = 1     # the edge of the next slab in the row
    across = np.zeros_like(stone); across[20:700, 300:360] = 1       # a strap, its top above the slab
    assert not retouch.keep_on_slab(beside, _with_box(stone), C.STRAP_ON_SLAB).any()
    assert retouch.keep_on_slab(across, _with_box(stone), C.STRAP_ON_SLAB).sum() == across.sum()


def test_a_strap_at_the_slabs_edge_counts_when_the_stone_beyond_it_was_missed():
    stone = np.zeros((800, 1200), np.uint8); stone[100:700, 260:1100] = 1   # SAM lost the strip of stone left of the strap
    strap = np.zeros_like(stone); strap[20:700, 200:260] = 1
    assert retouch.keep_on_slab(strap, _with_box(stone), C.STRAP_ON_SLAB).sum() == strap.sum()


@pytest.mark.parametrize("h,w", [(800, 1400), (500, 1900), (1200, 900)])
def test_on_white_is_the_same_canvas_with_white_corners(h, w):
    out = layout.on_white([_slab(h, w)])
    assert out.shape == (C.OUT_H, C.OUT_W, 3) and out[2, 2].min() > 250
    assert layout.on_white([_slab(h, w)], turn="left").shape == (C.OUT_H, C.OUT_W, 3)


def test_a_changed_background_is_not_a_changed_stone():
    original, edit, mask = _strap_edit()
    edit[:150] = (30, 60, 200)                         # the model repainted the warehouse behind the slab
    stone = _whole(); stone[:200] = 0
    merged, reason = retouch.merge(original, edit, mask & stone, stone, retouch._slab(stone), 1200)
    assert reason == "" and merged is not None


def test_a_mark_off_the_slab_is_not_a_mark():
    stone = np.zeros((800, 1200), np.uint8); stone[100:700, 100:1100] = 1
    on = np.zeros_like(stone); on[300:340, 500:600] = 1
    off = np.zeros_like(stone); off[720:780, 200:400] = 1      # a painted code on the floor
    assert retouch.keep_on_slab(on, retouch._slab(stone), C.CARD_ON_SLAB).any() and not retouch.keep_on_slab(off, retouch._slab(stone), C.CARD_ON_SLAB).any()


def test_the_model_also_clearing_an_unmarked_logo_is_no_repaint():
    original, edit, mask = _strap_edit()
    original[100:220, 600:900] = (200, 40, 120)        # a vendor logo nobody marked: the model takes it off too, it is never pasted
    merged, reason = retouch.merge(original, edit, mask, _whole(), _whole(), 1200)
    assert reason == "" and np.array_equal(merged[150, 700], original[150, 700])


def test_a_strong_even_recolouring_is_refused():
    original, edit, mask = _strap_edit()
    recoloured = np.clip(edit.astype(np.int16) + (30, 30, 10), 0, 255).astype(np.uint8)
    merged, reason = retouch.merge(original, recoloured, mask, _whole(), _whole(), 1200)
    assert merged is None and "recoloured" in reason


def test_a_label_on_the_slabs_top_edge_is_judged_against_the_stone_only():
    original, edit, mask = _strap_edit()
    stone = _whole(); stone[:200] = 0
    original[:200], edit[:200] = (20, 20, 25), (20, 20, 25)   # the dark hall above the slab
    label = np.zeros_like(mask); label[150:300, 500:640] = 1
    original[150:300, 500:640] = 250
    merged, reason = retouch.merge(original, edit, label, stone, retouch._slab(stone), 1200)
    assert reason == "" and merged[260, 570].max() < 200


def test_a_fill_matching_the_models_repaint_but_not_the_real_stone_is_refused():
    original, _, mask = _strap_edit()
    original = _stone(800, 1200, base=160, var=1)                 # a plain grey slab
    rng = np.random.default_rng(9)
    busy = np.clip(rng.normal(160, 40, (800, 1200, 3)), 0, 255).astype(np.uint8)   # the model drew a busy one in its place
    merged, reason = retouch.merge(original, busy, mask, _whole(), _whole(), 1200)
    assert merged is None


def test_a_mark_too_wide_to_fill_is_refused_before_the_model_runs():
    original = _stone(800, 1200, base=160, var=8)
    fan = np.zeros((800, 1200), np.uint8); fan[300:650, 400:800] = 1   # a fan of sample cards on a close-up
    calls = []
    out, reason = retouch.retouch(original, fan, _whole(), _whole(), lambda crop, seed: calls.append(crop) or crop)
    assert out is None and "too large" in reason and not calls


def _stand():
    clean = _stone(800, 1200, base=170, var=6)
    shot = clean.copy()
    shot[500:530, 300:900] = (70, 70, 75)                       # the stand's top bar
    shot[530:560, 300:900] = (shot[530:560, 300:900] * 0.75).astype(np.uint8)   # its shadow on the stone
    bar = np.zeros((800, 1200), np.uint8); bar[495:535, 295:905] = 1
    return shot, clean, bar


def test_the_shadow_a_stand_casts_comes_off_with_it():
    shot, clean, bar = _stand()
    merged, reason = retouch.merge(shot, clean, bar, _whole(), _whole(), 1200)
    assert reason == ""
    assert abs(merged[550, 600].astype(np.float32).mean() - clean[550, 600].astype(np.float32).mean()) < 6


def test_a_dark_thing_beside_a_mark_that_changes_colour_is_no_shadow():
    shot, clean, bar = _stand()
    shot[530:560, 300:900] = (40, 40, 140)                      # a blue sticker the detector missed, touching the bar
    merged, _ = retouch.merge(shot, clean, bar, _whole(), _whole(), 1200)
    assert merged is None or np.array_equal(merged[550, 600], shot[550, 600])


def test_a_fill_shaded_where_nothing_measures_its_edge_takes_the_stones_tone():
    original, edit, _ = _strap_edit()
    stone = _whole(); stone[:200] = 0
    original[:200], edit[:200] = (20, 20, 25), (20, 20, 25)        # the hall above the slab
    label = np.zeros((800, 1200), np.uint8); label[150:300, 500:640] = 1
    original[150:300, 500:640] = 250
    shade = np.linspace(-30, 0, 100)[:, None, None]                 # the model drew the label's area darker toward the top
    edit[200:300, 500:640] = np.clip(edit[200:300, 500:640] + shade, 0, 255).astype(np.uint8)
    merged, reason = retouch.merge(original, edit, label, stone, retouch._slab(stone), 1200)
    assert reason == ""
    tone = lambda im, ys, xs: im[ys, xs].astype(np.float32).mean()
    assert abs(tone(merged, slice(200, 250), slice(510, 630)) - tone(original, slice(200, 250), slice(660, 720))) < 5
    assert abs(tone(merged, slice(200, 300), slice(510, 630)) - tone(original, slice(200, 300), slice(660, 720))) < 3


def test_a_strap_where_the_stone_beyond_it_was_lost_takes_the_stones_tone():
    clean = _stone(800, 1200, base=150, var=8)
    original = clean.copy(); original[:, 200:260] = (120, 80, 50)
    stone = np.zeros((800, 1200), np.uint8); stone[:, 260:] = 1    # the segmenter lost the strip left of the strap
    strap = np.zeros_like(stone); strap[:, 195:265] = 1
    bluer = np.clip(clean.astype(np.int16) + (-4, -2, 8), 0, 255).astype(np.uint8)   # the model drew the stone a little bluer
    merged, reason = retouch.merge(original, bluer, strap, stone, _whole(), 1200)
    assert reason == ""
    assert np.abs(merged[:, 210:250].reshape(-1, 3).mean(0) - clean[:, 210:250].reshape(-1, 3).mean(0)).max() < 2


def test_a_shadows_soft_edge_comes_off_too():
    shot, clean, bar = _stand()
    shot[560:568, 300:900] = (clean[560:568, 300:900] * 0.9).astype(np.uint8)   # the shadow's fringe, within SHADOW_REACH
    merged, reason = retouch.merge(shot, clean, bar, _whole(), _whole(), 1200)
    assert reason == ""
    assert abs(merged[564, 600].astype(np.float32).mean() - clean[564, 600].astype(np.float32).mean()) < 3
