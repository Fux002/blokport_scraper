"""The standardiser's rules, on synthetic images (no model, no GPU): the same canvas on every image, a room that cannot
trace a vendor, real chips kept, flares told from veins, a logo removed only when it can be trusted."""
from pathlib import Path

import cv2
import numpy as np
import pytest

from stone_pipeline.config import settings
from stone_pipeline.config.settings import STANDARDISE as C
from stone_pipeline.standardise import blend, index, kinds, layout, logos, marks, outline, retouch, studio, views


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
    assert list(inspect.signature(layout.room_for).parameters) == ["key", "far"]


def test_an_open_book_stands_in_the_same_hall_seen_from_twice_as_far():
    near, far = layout.room_for("handle-a", False), layout.room_for("handle-a", True)
    assert near.shape == far.shape and not np.array_equal(near, far)
    # the middle of the far view is the near view at half size, about the floor line's centre
    h, w = near.shape[:2]
    small = cv2.resize(near, (w // 2, h // 2), interpolation=cv2.INTER_AREA)
    y0, x0 = int(h * (1 - C.FLOOR_SHARE) / 2), w // 4
    middle = far[y0 + 40:y0 + h // 4, x0 + 40:x0 + w // 4]
    assert np.abs(middle - small[40:h // 4 - y0 + y0, 40:w // 4]).mean() < 12


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


def test_a_tube_reflection_not_fully_at_255_is_a_flare():
    stone = _stone(1000, 1500, base=180, var=4)                     # a white marble lit by strip lights (sen-0c66cf30)
    for y in (150, 230, 310):
        cv2.rectangle(stone, (700, y), (760, y + 40), (250, 250, 250), -1)
    found = marks.flare_mask(stone, np.ones((1000, 1500), np.uint8))
    assert found[170, 730] == 1 and found[250, 730] == 1 and found[330, 730] == 1


def test_every_spot_of_a_row_of_ceiling_lights_is_a_flare():
    stone = _stone(1000, 1500, base=185, var=4)                     # rows of small lamps on white marble (zuc-1e5875ec)
    spots = [(x, y) for y in (80, 160, 240) for x in range(200, 1400, 60)]
    for x, y in spots:
        cv2.ellipse(stone, (x, y), (6, 9), 0, 0, 360, (248, 248, 248), -1)
    found = marks.flare_mask(stone, np.ones((1000, 1500), np.uint8))
    assert all(found[y, x] == 1 for x, y in spots)


def test_clipped_white_veins_that_are_the_stone_s_own_brightest_are_not_flares():
    stone = _stone(1000, 1500, base=60, var=6)                      # a dark marble with a net of white veins (sen-000963c8)
    for i in range(14):
        cv2.line(stone, (0, 40 + 70 * i), (1500, 140 + 60 * i), (249, 249, 249), 9)
    assert marks.flare_mask(stone, np.ones((1000, 1500), np.uint8)).sum() == 0


def test_pale_spots_on_a_black_stone_far_above_it_but_not_clipped_are_not_flares():
    stone = _stone(1000, 1500, base=25, var=4)                      # gold and pale flecks on a black granite (var-5356f4e0)
    for x in (300, 700, 1100):
        cv2.ellipse(stone, (x, 500), (14, 10), 0, 0, 360, (215, 215, 215), -1)
    assert marks.flare_mask(stone, np.ones((1000, 1500), np.uint8)).sum() == 0


def test_a_soft_ceiling_light_not_fully_clipped_is_a_flare():
    stone = _stone(1000, 1500, base=185, var=4)                     # the dimmer lamps of a row (zuc-1e5875ec): 232..244
    cv2.ellipse(stone, (600, 300), (8, 12), 0, 0, 360, (236, 236, 236), -1)
    assert marks.flare_mask(stone, np.ones((1000, 1500), np.uint8))[300, 600] == 1


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


def _sharp_stone(h, w, seed=1, contrast=1.0):
    """A flat slab as a sharp photo shows it: sharp meandering veins (the zero crossings of smooth noise: never straight,
    so no staircase repeats as an enlargement's pattern does) and speckle, uneven across it (real structure, unlike an
    even grain, which cannot be told from noise); contrast < 1: a calm stone."""
    rng = np.random.default_rng(seed)
    field = cv2.GaussianBlur(rng.standard_normal((h, w)).astype(np.float32), (0, 0), 12)
    marks = (np.abs(field) < 0.04 * field.std()).astype(np.float32) * 70.0
    marks += (rng.random((h, w)) < 0.03 * np.linspace(0.1, 1, w)[None, :]) * 50.0     # speckle, denser to the right
    rgb = np.clip(_stone(h, w, seed).astype(np.float32) - contrast * marks[..., None], 0, 255).astype(np.uint8)
    return rgb, np.ones((h, w), np.float32)


@pytest.mark.parametrize("w,h,density", [(1400, 860, 1.0), (1500, 920, 1.0), (2000, 1230, 1.0), (2600, 1600, 1.0),
                                         (4000, 2450, 1.0), (3000, 900, 1.0), (4000, 2450, 0.6), (2600, 1600, 1.4),
                                         (6000, 4000, 1.0)])
def test_a_close_up_never_holds_more_pixels_than_the_photo_has_there(w, h, density):
    box = views.closeup_box(w, h, density)
    if box is not None:
        x, y, cw, ch = box
        (native_w, native_h), (out_w, out_h) = views.closeup_size(cw, density)
        assert native_w <= cw * density * C.CLOSEUP_MAX_ENLARGE and native_h <= ch * density * C.CLOSEUP_MAX_ENLARGE  # 1:1
        assert out_w <= native_w and out_h <= native_h                                                         # never larger
        assert C.CLOSEUP_MIN_WIDTH <= out_w <= C.OUT_W and abs(out_w / out_h - C.OUT_W / C.OUT_H) < 0.01     # the canvas shape
        assert cw <= C.CLOSEUP_MAX_SHARE * w and ch <= C.CLOSEUP_MAX_SHARE * h + 1
        assert x >= 0 and y >= 0 and x + cw <= w and y + ch <= h


def test_a_lower_resolution_slab_gets_a_wider_crop_never_an_enlarged_one():
    big, small = views.closeup_box(4000, 2450), views.closeup_box(2000, 1230)
    assert big[2] == round(C.CLOSEUP_SHARE * 4000)                       # enough pixels: the tightest close-up
    assert small[2] / 2000 > big[2] / 4000                              # fewer pixels: more of the slab ...
    assert views.closeup_size(small[2])[1][0] == small[2]               # ... stored at its own pixels, 1:1


def test_a_close_up_with_more_pixels_than_the_canvas_is_made_smaller_to_it():
    x, y, cw, ch = views.closeup_box(6000, 4000)
    assert cw > C.OUT_W and views.closeup_size(cw) == ((cw, cw * C.OUT_H // C.OUT_W), (C.OUT_W, C.OUT_H))


def test_a_close_up_made_smaller_to_the_canvas_shows_no_false_pattern(monkeypatch):
    # Far more of the photo's pixels than the canvas holds (the crop 0.9 of a 4600 px slab: about 2x), with a grating finer
    # than the stored close-up can show: made smaller properly it averages away; sampled without a filter it folds
    # into broad false bands (moire).
    monkeypatch.setattr(C, "CLOSEUP_SHARE", 0.9)
    stone, _ = _sharp_stone(3000, 4600)
    grating = (40 * np.sin(2 * np.pi * 0.48 * np.arange(4600, dtype=np.float32)))[None, :, None]
    striped = np.clip(stone.astype(np.float32) + grating, 0, 255).astype(np.uint8)
    plain, with_grating = (views.close_up(_frame_slab(layout.fit_working(p)), p) for p in (stone, striped))
    assert plain.shape[1] == C.OUT_W
    broad = lambda img: cv2.GaussianBlur(img.astype(np.float32), (0, 0), 3)
    assert (broad(with_grating) - broad(plain)).std() < 3              # sampled without a filter: 21; area-filtered: 1


def test_a_slab_stretched_by_its_squaring_needs_a_wider_crop():
    plain, stretched = views.closeup_box(4000, 2450), views.closeup_box(4000, 2450, density=0.45)
    assert stretched[2] > plain[2]
    assert views.closeup_size(stretched[2], 0.45)[1][0] <= stretched[2] * 0.45     # never more than the photo's own pixels


def test_the_photos_own_pixels_per_slab_pixel_come_from_the_squaring():
    assert abs(views._density(layout.scaling(0.5, 0.5), (1000, 600)) - 2.0) < 1e-6    # squared smaller: 2 photo px each
    assert abs(views._density(layout.scaling(1.6, 1.0), (1600, 600)) - 1 / 1.6) < 1e-6  # stretched across: fewer
    tilt = cv2.getPerspectiveTransform(np.float32([[0, 0], [999, 0], [999, 599], [0, 599]]),
                                       np.float32([[0, 0], [999, 60], [999, 540], [0, 599]]))     # the far side shortened
    assert views._density(tilt, (1000, 600)) < 1


def test_a_slab_stretched_past_its_pixels_by_a_steep_shot_gets_no_close_up():
    work, _ = _sharp_stone(1300, 2000)
    stretch = layout.scaling(2.0, 1.0)                                  # each photo pixel already two slab pixels wide
    flat = cv2.warpPerspective(work, stretch, (4000, 1300), flags=cv2.INTER_CUBIC)
    assert views.close_up(layout.Flat(flat, np.ones((1300, 4000), np.float32), stretch, work), work) is None


def _jpeg(rgb, q):
    return cv2.imdecode(cv2.imencode(".jpg", rgb, [cv2.IMWRITE_JPEG_QUALITY, q])[1], cv2.IMREAD_UNCHANGED)


def _enlarged(rgb, factor, interp):
    """rgb as a vendor publishes a photo enlarged `factor` times: made smaller, enlarged back."""
    h, w = rgb.shape[:2]
    return cv2.resize(cv2.resize(rgb, (round(w / factor), round(h / factor)), interpolation=cv2.INTER_AREA), (w, h), interpolation=interp)


def _unsharp(rgb):
    return np.clip(rgb * 2.0 - cv2.GaussianBlur(rgb, (0, 0), 1.0).astype(np.float32), 0, 255).astype(np.uint8)


def _pil_unsharp(rgb):
    from PIL import Image, ImageFilter
    return np.asarray(Image.fromarray(rgb).filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=0)))


def _noisy(rgb):
    return np.clip(rgb + np.random.default_rng(0).normal(0, 6, rgb.shape), 0, 255).astype(np.uint8)


@pytest.mark.parametrize("fooled", [lambda rgb: _jpeg(_enlarged(rgb, 2.0, cv2.INTER_CUBIC), 60),
                                    lambda rgb: _unsharp(_jpeg(_enlarged(rgb, 2.0, cv2.INTER_CUBIC), 60)),
                                    lambda rgb: _noisy(_unsharp(_jpeg(_enlarged(rgb, 2.0, cv2.INTER_CUBIC), 60)))],
                         ids=["enlarged 2x + jpeg60", "+ unsharp", "+ noise"])
def test_a_photo_enlarged_then_sharpened_or_noised_gets_no_close_up(fooled):
    sharp, _ = _sharp_stone(1300, 2000)
    assert views.close_up(*_photo_slab_of(sharp)) is not None
    assert views.close_up(*_photo_slab_of(fooled(sharp))) is None


@pytest.mark.parametrize("q", [60, 85])
@pytest.mark.parametrize("sharpened", [False, True], ids=["plain", "unsharp"])
@pytest.mark.parametrize("interp", [cv2.INTER_LINEAR, cv2.INTER_CUBIC], ids=["bilinear", "cubic"])
def test_a_photo_enlarged_twice_over_is_too_soft_to_show_at_1_to_1(interp, sharpened, q):
    sharp, _ = _sharp_stone(1100, 1700)
    assert views.fine_detail(sharp) >= C.CLOSEUP_DETAIL_MIN
    big = _enlarged(sharp, 2.0, interp)
    assert views.fine_detail(_jpeg(_pil_unsharp(big) if sharpened else big, q)) < C.CLOSEUP_DETAIL_MIN


@pytest.mark.parametrize("name,gets_one", [("sharp", True),                 # mar-1a7b77cb: detail 0.98
                                           ("near_bar_sharp", True),        # pol-1eb05952: 0.41, just over the bar
                                           ("near_bar_soft", False)])       # the same photo elsewhere: 0.39, just under it
def test_real_photos_near_the_bar_are_decided_as_their_pixels_say(name, gets_one):
    assert (views.fine_detail(_fixture(name)) >= C.CLOSEUP_DETAIL_MIN) is gets_one


@pytest.mark.parametrize("interp", [cv2.INTER_LINEAR, cv2.INTER_CUBIC], ids=["bilinear", "cubic"])
def test_a_real_sharp_photo_enlarged_twice_over_is_too_soft_to_show_at_1_to_1(interp):
    assert views.fine_detail(_jpeg(_enlarged(_fixture("sharp"), 2.0, interp), 85)) < C.CLOSEUP_DETAIL_MIN


def test_a_sharp_calm_stone_keeps_its_close_up():
    calm, _ = _sharp_stone(1300, 2000, contrast=0.2)
    assert views.close_up(*_photo_slab_of(calm)) is not None


def test_noise_spread_evenly_is_no_detail():
    noise = np.random.default_rng(2).integers(0, 255, (683, 1024, 3), dtype=np.uint8)
    assert views.fine_detail(noise) < 0.1 * C.CLOSEUP_DETAIL_MIN


def test_a_stone_smoothed_to_its_8_bit_steps_or_blank_has_no_detail():
    smoothed = cv2.GaussianBlur(_sharp_stone(1300, 2000)[0], (0, 0), 2.7)
    assert views.fine_detail(smoothed) == 0.0
    assert views.fine_detail(np.full((683, 1024, 3), 128, np.uint8)) == 0.0
    sharp, _ = _sharp_stone(683, 1024)
    assert abs(views.fine_detail(_noisy(sharp)) - views.fine_detail(sharp)) < 0.25 * views.fine_detail(sharp)


def _photo_slab_of(rgb):
    return layout.Flat(rgb, np.ones(rgb.shape[:2], np.float32), np.eye(3), rgb), rgb


def _photo_slab(h, w):
    """(a slab filling a photo no larger than the work copy, the photo): squared as it is."""
    rgb, alpha = _sharp_stone(h, w)
    return layout.Flat(rgb, alpha, np.eye(3), rgb), rgb


def test_a_slab_with_too_few_pixels_gets_no_close_up():
    assert views.closeup_box(1000, 620) is None                         # even its widest crop holds too few of its pixels
    assert views.close_up(*_photo_slab(620, 1000)) is None


def test_a_soft_slab_gets_no_close_up():
    flat, full = _photo_slab(1100, 1700)
    assert views.close_up(flat, full) is not None
    soft = cv2.GaussianBlur(full, (0, 0), 1.5)                          # its fine detail is gone: none is invented
    assert views.close_up(layout.Flat(soft, flat.alpha, flat.homography, soft), soft) is None


def test_a_sharp_slab_gives_a_close_up_at_its_own_pixels():
    x, y, cw, ch = views.closeup_box(1700, 1100)
    _, (out_w, out_h) = views.closeup_size(cw)
    assert views.close_up(*_photo_slab(1100, 1700)).shape == (out_h, out_w, 3) and out_w == cw


def test_a_close_up_is_only_of_real_stone():
    flat, full = _photo_slab(1100, 1700)
    flat.alpha[500:600, 800:900] = 0
    assert views.close_up(flat, full) is None


def test_a_close_up_takes_no_model_only_the_photos_own_pixels():
    import inspect
    assert list(inspect.signature(views.close_up).parameters) == ["flat", "full", "edits"]


def _corners_in_work_copy(q, s):
    """Corners on the full photo, on the work copy s times smaller (pixel centres, as cv2.resize maps them)."""
    return ((q + 0.5) / s - 0.5).astype(np.float32)


def _like(img, other):
    """img at other's size, to compare two close-ups whose boxes differ by a rounded pixel."""
    return cv2.resize(img, other.shape[1::-1], interpolation=cv2.INTER_AREA)


def test_a_close_up_takes_the_slab_from_the_full_resolution_photo(monkeypatch):
    full, _ = _sharp_stone(1950, 3000)                                       # 1.5 x the work copy
    work = layout.fit_working(full)
    q = np.float32([[150, 120], [2850, 180], [2880, 1830], [120, 1800]])
    flat, H = outline._flat(work, _corners_in_work_copy(q, full.shape[1] / work.shape[1]))
    got = views.close_up(layout.Flat(flat, np.ones(flat.shape[:2], np.float32), H, work), full)
    monkeypatch.setattr(C, "WORK_LONG_EDGE", 10_000)                    # the same slab squared on the full photo itself
    big, Hf = outline._flat(full, q)
    want = views.close_up(layout.Flat(big, np.ones(big.shape[:2], np.float32), Hf, full), full)
    blur = lambda img: cv2.GaussianBlur(img.astype(np.float32), (0, 0), 3)
    assert abs(got.shape[1] - want.shape[1]) <= 2 and np.abs(blur(got) - blur(_like(want, got))).mean() < 2


def test_a_close_up_is_lit_as_the_whole_slab_is():
    full, _ = _sharp_stone(1950, 3000)
    full = np.clip(full * np.linspace(0.7, 1.2, 3000, dtype=np.float32)[None, :, None] * (1.05, 1.0, 0.92), 0, 255).astype(np.uint8)
    work = layout.fit_working(full)
    got = views.close_up(_frame_slab(work), full)
    x, y, cw, ch = views.closeup_box(3000, 1950)
    want = marks.even_light(full, np.ones(full.shape[:2], np.uint8))[y:y + ch, x:x + cw]
    blur = lambda img: cv2.GaussianBlur(img.astype(np.float32), (0, 0), 4)
    assert np.abs(blur(got) - blur(_like(want, got))).mean() < 2


def _frame_slab(work):
    """The work copy's whole frame as the slab."""
    return layout.Flat(work, np.ones(work.shape[:2], np.float32), np.eye(3), work)


def test_a_photo_soft_at_1_to_1_but_sharp_made_smaller_gets_its_close_up_from_the_work_copy():
    # Shot at 4000 px and softened (a phone's processing): too soft at 1:1, its 2000 px copy sharp.
    full = cv2.GaussianBlur(_sharp_stone(2600, 4000)[0], (0, 0), 1.0)                # detail 0.09 at 1:1, 0.48 at 2000 px
    flat = _frame_slab(layout.fit_working(full))
    assert views.fine_detail(full[650:1950, 1000:3000]) < C.CLOSEUP_DETAIL_MIN
    got = views.close_up(flat, full)
    assert got is not None
    assert np.array_equal(got, views.close_up(flat, flat.work))           # the work copy's close-up, same rules


def test_a_sharp_full_resolution_photo_keeps_its_full_resolution_close_up():
    full, _ = _sharp_stone(2600, 4000)
    flat = _frame_slab(layout.fit_working(full))
    assert views.close_up(flat, full).shape[1] > views.close_up(flat, flat.work).shape[1]   # more of its own pixels


def test_a_photo_soft_at_both_sizes_gets_no_close_up():
    full = cv2.GaussianBlur(_sharp_stone(1950, 3000)[0], (0, 0), 4)
    assert views.close_up(_frame_slab(layout.fit_working(full)), full) is None


def _labelled(rows, cols):
    """(the photo with a magenta label at rows x cols of its middle, the slab of its work copy with the label erased)."""
    stone, _ = _sharp_stone(1950, 3000)
    full = stone.copy()
    full[rows, cols] = (255, 0, 255)
    return full, _frame_slab(layout.fit_working(stone))


def test_what_the_work_copy_erased_stays_erased_in_the_full_resolution_close_up():
    full, flat = _labelled(slice(960, 990), slice(1470, 1500))         # a small label: well under CLOSEUP_EDIT_MAX of the crop
    out = views.close_up(flat, full)
    assert out is not None
    assert not ((out[..., 0] > 180) & (out[..., 1] < 90) & (out[..., 2] > 180)).any()


def test_a_close_up_mostly_erased_or_filled_is_none():
    full, flat = _labelled(slice(900, 1050), slice(1350, 1650))        # a logo or card: a patch the eraser made up
    assert views.close_up(flat, full) is None


def test_a_close_up_of_a_photo_a_model_cleaned_or_retouched_there_is_none():
    full, _ = _sharp_stone(1950, 3000)
    flat = _frame_slab(layout.fit_working(full))
    assert views.close_up(flat, full) is not None
    assert views.close_up(flat, full, edits=np.ones(full.shape[:2], np.uint8)) is None          # a hosted model's cleaned copy
    fills = np.zeros(full.shape[:2], np.uint8)
    fills[800:1150, 1200:1800] = 1                                                             # a retouch's fill in the middle
    assert views.close_up(flat, full, edits=fills) is None


FIXTURES = Path(__file__).parent / "fixtures" / "closeup"


def _fixture(name):
    """A real photo's crop (its own pixels, 8 px aligned so its JPEG grid stays on its axes), RGB."""
    return cv2.cvtColor(cv2.imread(str(FIXTURES / f"{name}.png")), cv2.COLOR_BGR2RGB)


def test_a_colourful_stone_re_encoded_with_nothing_filled_has_no_fills():
    from stone_pipeline.standardise import photo
    stone = _fixture("varsha_colour_edges")             # var-c7adcb37: the old rule flagged 3.8% of this crop
    recoded = _jpeg(stone, 95)                          # as the retouch stores its copy: q95, colour subsampled 4:2:0
    assert not photo.retouch_fills(recoded, stone).any()


def test_a_retouchs_fills_are_told_from_its_jpeg():
    from stone_pipeline.standardise import photo
    made_from, _ = _sharp_stone(600, 900)
    retouched = made_from.copy()
    retouched[200:300, 300:450] = _stone(100, 150, seed=7, base=90, var=30)    # filled with other stone
    retouched = cv2.imdecode(cv2.imencode(".jpg", retouched, [cv2.IMWRITE_JPEG_QUALITY, 95])[1], cv2.IMREAD_UNCHANGED)
    fills = photo.retouch_fills(retouched, made_from)
    assert fills[210:290, 310:440].all() and fills[:150].sum() == 0 and fills[400:].sum() == 0
    assert not photo.retouch_fills(retouched[50:550, 50:850], made_from).any()   # cropped clear of its marks: nothing filled


class _Esr:
    """Stands in for Real-ESRGAN: 4x, plain cubic."""
    def enhance(self, bgr):
        return cv2.resize(bgr, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)


def test_the_close_up_leaves_every_other_view_as_it_was(monkeypatch):
    from stone_pipeline.standardise import models as standardise_models, photo
    full, alpha = _sharp_stone(1100, 1700)                   # pixels enough for a close-up, still enlarged by the layout
    flats = [layout.Flat(full, alpha, np.eye(3), full)]
    monkeypatch.setattr(kinds, "classify", lambda clip, rgb: ("slab", 0.99))
    monkeypatch.setattr(layout, "flat_slabs", lambda rgb, seg, det, lama, size=None: flats)
    monkeypatch.setattr(blend, "blend", lambda img, solid, stand, pipe, seed: img)
    m = standardise_models.Models(seg=None, det=None, lama=None, esr=_Esr(), clip=None, inpaint=None)
    asked = []                                          # where the start photo is not as shot: asked only for a close-up
    plain = photo.standardise(full, fmt="slab", key="p", models=m, edits=lambda: asked.append("plain"))
    extra = photo.standardise(full, fmt="slab", key="p", models=m, extra_views=True, edits=lambda: asked.append("extra"))
    assert asked == ["extra"]

    def original_gone():
        raise photo.NoCloseUp("its input is missing: scraped/x.jpg")
    unread = photo.standardise(full, fmt="slab", key="p", models=m, extra_views=True, edits=original_gone)
    assert "close" not in unread.images and all(np.array_equal(unread.images[k], plain.images[k]) for k in plain.images)
    slabs = layout.enlarged(flats, m.esr)
    assert slabs[0][0].shape[1] > full.shape[1]
    assert "close" in extra.images and "close" not in plain.images
    for name in ("main", "white", "studio"):
        assert np.array_equal(plain.images[name], extra.images[name])
    assert np.array_equal(extra.images["white"], layout.on_white(slabs))
    assert all(np.array_equal(a, b) for a, b in zip(extra.slab, slabs[0]))
    # The close-only run makes the same close-up from the same photo.
    assert np.array_equal(photo.close_view(full, size=None, models=m), extra.images["close"])



def test_the_close_only_run_takes_the_same_half_of_an_open_book_as_the_run_did(monkeypatch):
    from stone_pipeline.standardise import photo
    flats = [layout.Flat(np.zeros((80, 100, 3), np.uint8), np.ones((80, 100), np.float32), np.eye(3), None) for _ in "lr"]
    flats[0].alpha[:20, :20] = 0                                     # the left half chipped: the right has more stone
    monkeypatch.setattr(layout, "flat_slabs", lambda rgb, *m, size=None: flats)
    monkeypatch.setattr(views, "close_up", lambda flat, full, edits: flat)

    class _Models:
        seg = det = lama = None
    full = np.zeros((60, 90, 3), np.uint8)
    assert photo.close_view(full, size=None, models=_Models()) is flats[1]
    flats[0].alpha[:20, :20] = 1                                     # equal halves: the left, as the run takes it
    assert photo.close_view(full, size=None, models=_Models()) is flats[0]


SHEET = """Product Handle,Product Length,Product Width,Product Category 1,Front Image,Product Image 1
one,3.0,2.0,pcat_01M1GWN1V1E66J1NM1PTQ3ESAK,https://x/prod/products/improved/zucchi/{a}.jpg,https://x/prod/products/improved/zucchi/{b}.jpg
two,,,pcat_01M1GWN1V1E66J1NM1PTQ3ESAK,https://x/prod/products/improved/zucchi/{a}.jpg,
""".format(a="a" * 64, b="b" * 64)
PHOTOS = {"one": [("zucchi", "a" * 64), ("zucchi", "b" * 64)], "two": [("zucchi", "a" * 64)]}


@pytest.fixture
def _active_slab(monkeypatch):
    from dataclasses import replace
    slab = replace(settings.category("slab"), pcat_id="pcat_01M1GWN1V1E66J1NM1PTQ3ESAK")
    monkeypatch.setattr(settings, "_BY_NAME", {slab.name: slab})           # the live registry the helpers read


def test_the_index_links_a_photo_to_its_first_product(_active_slab):
    photos = index.build(SHEET, PHOTOS)
    assert photos["a" * 64]["key"] == "one" and photos["a" * 64]["size"] == (3.0, 2.0) and photos["b" * 64]["format"] == "slab"


def test_an_unknown_category_fails_loud(_active_slab):
    with pytest.raises(ValueError, match="unknown product category"):
        index.build(SHEET.replace("pcat_01M1GWN1V1E66J1NM1PTQ3ESAK", "pcat_new"), PHOTOS)


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


def test_a_marks_own_border_line_does_not_stay_at_the_fills_edge():
    original, clean, mask = _strap_edit()
    original[:, 290:292] = 60                                     # the label's dark border, right at the edge of what was found
    merged, reason = retouch.merge(original, clean, mask, _whole(), _whole(), 1200)
    assert reason == ""
    assert np.abs(merged[:, 290:292].astype(np.float32) - clean[:, 290:292].astype(np.float32)).mean() < 4


def test_the_matte_floor_never_mirrors_the_slab():
    h, w = 900, 1400
    stripes = np.zeros((h, w, 3), np.uint8)
    stripes[:, ::40] = 255                                           # a slab with bold white veins every 40 px
    room = np.full((C.OUT_H, C.OUT_W, 3), 128, np.float32)
    out = layout.compose([(stripes, np.ones((h, w), np.float32))], "k", room=room, stand=False).astype(np.float32)
    bottom = C.OUT_H - int(round(C.FLOOR_SHARE * C.OUT_H))
    floor = out[bottom + 10:, C.OUT_W // 4:3 * C.OUT_W // 4, 0]
    assert floor.std(axis=1).max() < 1.0                             # shaded evenly, no stripe of the slab in it


def test_a_close_up_with_a_fan_of_cards_is_cropped_to_its_clear_stone():
    h, w = 1280, 1707
    fan = np.zeros((h, w), np.uint8); fan[900:1280, 500:1000] = 1      # the cards at the bottom of a close-up, as on Zucchi's
    box = retouch.clear_crop(fan)
    assert box is not None
    x0, y0, x1, y1 = box
    assert not fan[y0:y1, x0:x1].any() and y1 <= 900 - C.CROP_MARGIN * w * 0.9
    assert C.CROP_ASPECT[0] <= (x1 - x0) / (y1 - y0) <= C.CROP_ASPECT[1] + 0.05
    assert (x1 - x0) * (y1 - y0) >= C.CROP_MIN_SHARE * h * w


def test_marks_all_over_leave_no_crop():
    h, w = 1280, 1707
    spread = np.zeros((h, w), np.uint8)
    spread[200:300, :] = 1; spread[700:800, :] = 1; spread[:, 600:700] = 1
    assert retouch.clear_crop(spread) is None


def _black_slab():
    rgb = _stone(600, 900, base=25, var=4)
    return rgb, np.ones((600, 900), np.float32)


def test_a_slab_that_took_in_the_white_slab_behind_its_broken_corner_is_held():
    rgb, alpha = _black_slab()
    corner = np.zeros((600, 900), np.uint8)
    cv2.fillPoly(corner, [np.array([[0, 0], [420, 0], [0, 330]])], 1)
    corner[:25, :] = 1; corner[:, 870:] = 1                              # and on along its top to its far edge, one piece as on sen-00946947
    rgb[corner > 0] = _stone(600, 900, seed=4, base=225, var=6)[corner > 0]
    assert layout.two_stones(rgb, alpha)


def test_white_veins_or_a_two_tone_breccia_are_one_stone():
    rgb, alpha = _black_slab()
    for x in range(60, 900, 140):
        cv2.line(rgb, (x, 0), (x + 200, 600), (235, 235, 230), 9)         # Nero Marquina's white veins
    assert not layout.two_stones(rgb, alpha)
    breccia = _stone(600, 900, base=180, var=10)
    rng = np.random.default_rng(2)
    for _ in range(60):
        cx, cy = rng.integers(0, 900), rng.integers(0, 600)
        cv2.circle(breccia, (int(cx), int(cy)), int(rng.integers(15, 45)), (70, 55, 60), -1)
    assert not layout.two_stones(breccia, alpha)


def _written(stone_rgb):
    rgb = stone_rgb.copy()
    for y in (300, 360, 420):                                       # three lines of marker numbers
        cv2.putText(rgb, "270", (420, y), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (40, 90, 230), 5)
    cv2.putText(rgb, "406-SD", (380, 230), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (40, 90, 230), 5)
    found = np.zeros(rgb.shape[:2], np.uint8); found[185:245, 370:620] = 1   # the detector found only the first line
    return rgb, found


def test_the_rest_of_the_writing_in_the_same_ink_is_found():
    rgb, found = _written(_stone(800, 1200, base=170, var=8))
    more = retouch.same_ink(rgb, found, _whole(), 1200)
    assert more[270:430, 420:520].sum() > 300 and more[:, :350].sum() == 0 and more[500:].sum() == 0


def test_writing_on_a_blue_stone_or_a_white_label_finds_nothing_more():
    blue = np.dstack([np.full((800, 1200), 40), np.full((800, 1200), 90), np.full((800, 1200), 225)]).astype(np.uint8)
    rgb, found = _written(blue)
    assert retouch.same_ink(rgb, found, _whole(), 1200).sum() == 0
    label = _stone(800, 1200, base=170, var=8); label[185:245, 370:620] = 250
    assert retouch.same_ink(label, found, _whole(), 1200).sum() == 0


def test_orange_ink_or_tiny_specks_add_nothing():
    stone = _stone(800, 1200, base=170, var=8)
    stone[300:340, 560:620] = (225, 125, 45)                        # an orange-yellow piece of the stone itself
    rgb = stone.copy()
    cv2.putText(rgb, "406", (380, 230), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (230, 120, 30), 5)   # an orange clamp or crayon
    found = np.zeros(rgb.shape[:2], np.uint8); found[185:245, 370:520] = 1
    assert retouch.same_ink(rgb, found, _whole(), 1200).sum() == 0
    rgb, found = _written(_stone(800, 1200, base=170, var=8))
    rgb[500:506, 450:456] = (40, 90, 230)                            # a blue speck in the stone
    assert retouch.same_ink(rgb, found, _whole(), 1200)[490:515, 440:465].sum() == 0


def test_the_studio_image_is_the_storefront_frame_and_repeatable():
    out = studio.place([_slab(800, 1400)])
    assert out.shape == (C.OUT_H, C.OUT_W, 3)
    assert np.array_equal(out, studio.place([_slab(800, 1400)]))
    assert out[10, 10].min() > 235                                   # the white studio backdrop


def test_the_studio_slab_keeps_its_proportions_and_stands_on_the_feet():
    for h, w in ((800, 1400), (1000, 1300)):
        q = studio.face(w / h)
        assert np.allclose(q[2:], studio.FACE[2:], atol=0.5)        # the same foot line, the clips under it
        assert abs(studio.true_aspect(q) - w / h) < 0.03 * w / h


def test_the_old_slab_is_gone_and_the_clips_stay():
    black = (np.full((800, 1400, 3), 15, np.uint8), np.ones((800, 1400), np.float32))
    out = studio.place([black]).astype(np.float32)
    assert (out.max(axis=2) > 200).any()                            # chrome highlights of the clips in front of it
    face = studio.face(1400 / 800)
    cx, cy = studio.to_frame(face.mean(axis=0), face)
    assert out[int(cy), int(cx)].max() < 40                          # the face is the new stone, nothing of the old slab


def _feet_rows():
    bottom = C.OUT_H - int(round(C.FLOOR_SHARE * C.OUT_H))
    return bottom + 2, bottom + 40


def test_the_showroom_stand_is_two_chrome_feet_with_nothing_between_them():
    grey = np.full((C.OUT_H, C.OUT_W, 3), 128, np.float32)
    with_it = layout.compose([_slab(800, 1400)], "p", room=grey.copy()).astype(np.float32)
    without = layout.compose([_slab(800, 1400)], "p", room=grey.copy(), stand=False).astype(np.float32)
    y0, y1 = _feet_rows()
    changed = np.abs(with_it - without).max(axis=2)[y0:y1] > 20
    runs = np.diff(np.concatenate([[0], changed.any(axis=0).astype(int), [0]]))
    assert (runs == 1).sum() == 2                                    # two feet, no rail joining them
    assert with_it[y0:y1][changed].max() > 200                      # polished chrome catches the light


def test_the_stand_mask_covers_the_chrome_feet():
    grey = np.full((C.OUT_H, C.OUT_W, 3), 128, np.float32)
    with_it = layout.compose([_slab(800, 1400)], "p", room=grey.copy()).astype(np.float32)
    _, stand = layout.masks([_slab(800, 1400)], "p")
    y0, y1 = _feet_rows()
    bright = with_it[y0:y1].max(axis=2) > 200
    assert bright.any() and stand[y0:y1][bright].all()


def test_text_left_around_a_mark_does_not_make_a_clean_fill_look_smeared():
    clean = _stone(800, 1200, base=215, var=1)                       # a plain white marble
    original = clean.copy()
    mask = np.zeros((800, 1200), np.uint8); mask[380:440, 300:420] = 1
    original[385:435, 305:415] = (200, 40, 120)                      # the logo's first word, found
    for y in (410, 460):                                             # the rest of its lettering round it, not found
        for x in range(260, 560, 22):
            if not 300 <= x <= 420 or y > 440:
                cv2.putText(original, "S", (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (90, 90, 90), 2)
    merged, reason = retouch.merge(original, clean, mask, _whole(), _whole(), 1200)
    assert reason == ""


def test_the_far_hall_is_softened_like_a_background_behind_slabs_in_focus():
    from PIL import Image
    raw = np.asarray(Image.open(layout.ROOM_DIR / "far_1.jpg").convert("RGB")).astype(np.float32)
    used = layout._room("far_1.jpg")
    sharp = lambda im: np.abs(cv2.Laplacian(cv2.cvtColor(im.astype(np.uint8), cv2.COLOR_RGB2GRAY), cv2.CV_32F))[:400].mean()
    assert sharp(used) < 0.6 * sharp(raw)


def test_a_floor_under_the_stone_means_a_whole_scene_not_a_close_up():
    from stone_pipeline.standardise import closeup
    h, w = 1000, 1500
    assert closeup.shows_floor([(0, 600, w, h, 0.8), (0, 0, w, h, 0.5)], h)        # open books on the warehouse floor
    assert not closeup.shows_floor([(0, 0, w, 990, 0.8)], h)                       # the detector boxes a close-up's stone as floor
    assert not closeup.shows_floor([], h)


def test_a_lamp_reflection_on_the_stone_is_a_mark_for_the_model_but_not_under_a_strap_or_off_the_stone():
    rgb = _stone(1000, 1500, base=150, var=6)
    for x0 in (300, 1300, 700):                                     # on the stone, past its edge, under a strap
        cv2.rectangle(rgb, (x0, 200), (x0 + 60, 240), (255, 255, 255), -1)
    stone = np.zeros((1000, 1500), np.uint8); stone[100:900, 100:1200] = 1
    straps = np.zeros_like(stone); straps[:, 680:780] = 1
    found = retouch.reflections(rgb, stone, straps)
    assert found[220, 330] == 1 and found[220, 1330] == 0 and found[220, 730] == 0


def test_a_lamp_reflection_never_stops_a_card_fan_being_cropped_away():
    h, w = 1280, 1707
    fan = np.zeros((h, w), np.uint8); fan[900:1280, 500:1000] = 1
    flares = np.zeros_like(fan); flares[100:160, 300:360] = 1; flares[500:560, 1300:1360] = 1   # lamps above the cards
    none, close_up = np.zeros_like(fan), np.ones_like(fan)
    assert retouch.crop_clear_of_wide_marks(none, fan, close_up) is not None
    x0, y0, x1, y1 = retouch.crop_clear_of_wide_marks(none, fan, close_up)
    assert not fan[y0:y1, x0:x1].any()
    assert retouch.crop_clear_of_wide_marks(none, flares, close_up) is None  # reflections alone are filled, not cropped


def test_a_whole_slab_is_never_cropped_to_clear_a_mark():
    h, w = 1500, 2000
    stone = np.zeros((h, w), np.uint8); stone[150:1300, 340:1950] = 1        # the whole slab inside the photo
    beside = np.zeros_like(stone); beside[0:1300, 0:330] = 1                  # a rack beside it taken for a strap (be9e3ec7)
    none = np.zeros_like(stone)
    assert retouch.too_wide_to_fill(beside)
    assert retouch.crop_clear_of_wide_marks(beside, none, stone) is None


def _quad_mask(corners, h=1500, w=2000):
    return cv2.fillConvexPoly(np.zeros((h, w), np.uint8), np.array(corners, np.int32), 1)


def test_a_slab_shot_at_an_angle_is_fitted_up_to_its_far_side_at_two_fifths_of_its_near():
    near, far = 1000, 1000 / 2.3                                     # 1ca6fd5d, adeebb0f: the far side 0.4..0.6 of the near
    angled = _quad_mask([(200, 250), (1800, 250 + (near - far) / 2), (1800, 250 + (near + far) / 2), (200, 1250)])
    assert marks.four_corners(angled) is not None
    far = 1000 / 3.1                                                 # 19ab0177: the far end too soft to show
    steeper = _quad_mask([(200, 250), (1800, 250 + (near - far) / 2), (1800, 250 + (near + far) / 2), (200, 1250)])
    assert marks.four_corners(steeper) is None


def test_a_slab_shot_near_square_on_is_measured_from_its_sides():
    q = np.float32([[200, 250], [1800, 300], [1800, 1200], [200, 1250]])
    assert outline.steep_aspect(q, (3.0, 1.8)) is None


def test_a_steep_shot_takes_the_slab_s_real_proportions():
    q = np.float32([[200, 250], [1400, 450], [1400, 1050], [200, 1250]])     # the right side 0.6 of the left
    assert outline.steep_aspect(q, (2.65, 1.8)) == pytest.approx(2.65 / 1.8)
    assert outline.steep_aspect(q, (1.8, 2.65)) == pytest.approx(2.65 / 1.8)  # a slab stands on its long edge


def test_a_steep_shot_without_a_real_size_is_held():
    q = np.float32([[200, 250], [1400, 450], [1400, 1050], [200, 1250]])
    assert isinstance(outline.steep_aspect(q, None), str)


def test_a_real_size_narrower_than_the_foreshortened_photo_is_held():
    q = np.float32([[100, 250], [1900, 450], [1900, 1050], [100, 1250]])     # seen 2.25 wide, shortened: at least that
    assert isinstance(outline.steep_aspect(q, (2.0, 1.8)), str)


def test_a_steep_slab_is_flattened_at_its_real_proportions_keeping_its_unshortened_height():
    rgb = _stone(1500, 2000)
    q = np.float32([[200, 250], [1400, 450], [1400, 1050], [200, 1250]])
    flat, _ = outline._flat(rgb, q, 1.5)
    _, h = outline._size(q)
    assert flat.shape[0] == int(h) and abs(flat.shape[1] - int(h * 1.5)) <= 1


def test_a_stand_in_front_of_the_slab_bites_a_hollow_out_of_its_outline():
    alpha = np.zeros((800, 1200), np.float32); alpha[20:780, 20:1180] = 1     # the squared slab
    alpha[560:780, 300:760] = 0                                              # the stand cut out of its foot (mar-2864d056)
    assert layout.hollow(alpha) > C.OUTLINE_HOLLOW_MAX


def test_a_chipped_corner_or_rough_edge_is_no_hollow():
    alpha = np.zeros((800, 1200), np.float32); alpha[20:780, 20:1180] = 1
    alpha[20:120, 1080:1180] = np.tril(np.ones((100, 100), np.float32))[:, ::-1]   # a corner broken off
    alpha[300:310, 20:26] = 0                                                       # a small chip in the edge
    assert layout.hollow(alpha) <= C.OUTLINE_HOLLOW_MAX


def test_a_sure_card_must_go_and_a_small_weak_label_may_stay_when_its_fill_is_refused():
    w, h = 2000, 1500
    small = 0.004 * w * h                                              # a sticker or label, as on zuc-156dc3e2 / mar-1c345582
    side = int(small ** 0.5)
    found = [(10, 10, 10 + side, 10 + side, 0.30),                    # small, weak: counts
             (10, 10, 10 + side, 10 + side, 0.20),                    # small, weaker than the label bar: not
             (0, 0, 600, 400, 0.30),                                  # large and weak: not (stone fires there)
             (0, 0, 600, 400, 0.40),                                  # large and sure: counts
             (0, 0, 1000, 1000, 0.90)]                                # larger than any card: the slab itself
    sure, labels = retouch.card_boxes(found, w, h)
    assert sure == [found[3][:4]] and labels == [found[0][:4]]        # a weak small one also fires on clean stone (2026-11-01)


def _two_slabs_one_box(monkeypatch, neighbour):
    """A photo of a slab with (or without) a separate, unmirrored slab beside it, the detector boxing both as one."""
    from stone_pipeline.standardise import marks as proto
    h, w = 1000, 2000
    rgb = np.full((h, w, 3), 90, np.uint8)
    left, right = np.zeros((h, w), np.uint8), np.zeros((h, w), np.uint8)
    left[300:800, 100:900] = 1
    right[300:800, 950:1750] = 1
    rgb[left > 0] = _stone(h, w, seed=1)[left > 0]
    if neighbour:
        rgb[right > 0] = _stone(h, w, seed=7, base=120)[right > 0]
    box = (90.0, 290.0, 1760.0, 810.0)                                 # one box over both places
    monkeypatch.setattr(outline, "detect", lambda det, img, prompt=None, threshold=None: [] if prompt else [(*box, 0.6)])
    monkeypatch.setattr(proto, "embed", lambda seg, img: None)
    def slab_mask(seg, img, b, emb, widest=False):
        m = left if b[0] < 500 else (right if neighbour else np.zeros_like(right))
        return m.copy(), m.copy()
    monkeypatch.setattr(proto, "slab_mask", slab_mask)
    return outline.find_slabs(None, None, rgb)


def test_a_slab_beside_another_in_one_detector_box_is_whole(monkeypatch):
    quads, _, _, _, reason = _two_slabs_one_box(monkeypatch, neighbour=True)      # sen-39212b82
    assert reason == "" and len(quads) == 1 and quads[0][:, 0].max() < 950


def test_a_detector_box_past_the_slab_with_nothing_beside_it_still_holds(monkeypatch):
    quads, _, _, _, reason = _two_slabs_one_box(monkeypatch, neighbour=False)     # only floor beside it: part of it unseen
    assert quads == [] and "continues past" in reason


def _one_slab(monkeypatch, left):
    """A photo of one slab whose left edge is at column `left` (0: it runs off the photo), boxed by the detector."""
    from stone_pipeline.standardise import marks as proto
    h, w = 1000, 2000
    rgb = np.full((h, w, 3), 90, np.uint8)
    slab = np.zeros((h, w), np.uint8)
    slab[300:800, left:1500] = 1
    rgb[slab > 0] = _stone(h, w, seed=3, base=170, var=1)[slab > 0]          # calm stone: no vein reads as an edge
    monkeypatch.setattr(outline, "detect", lambda det, img, prompt=None, threshold=None: [] if prompt else [(float(left), 290.0, 1510.0, 810.0, 0.6)])
    monkeypatch.setattr(proto, "embed", lambda seg, img: None)
    monkeypatch.setattr(proto, "slab_mask", lambda seg, img, b, emb, widest=False: (slab.copy(), slab.copy()))
    return outline.find_slabs(None, None, rgb)


def test_a_slab_framed_tightly_its_edge_a_few_pixels_inside_the_photo_is_whole(monkeypatch):
    quads, _, _, _, reason = _one_slab(monkeypatch, left=10)                       # 0.5% in, as sen-3b0f048c (27 of 4000 px)
    assert reason == "" and len(quads) == 1


def test_a_slab_whose_stone_runs_off_the_photo_is_cut_off(monkeypatch):
    quads, _, _, _, reason = _one_slab(monkeypatch, left=0)                        # as sen-d1023341
    assert quads == [] and "cut off" in reason


def test_the_frame_edges_a_slab_runs_into_are_the_ones_its_stone_reaches():
    m = np.zeros((1000, 2000), np.uint8); m[300:800, 0:1500] = 1
    assert outline.frame_sides(m) == {"left"}
    m[:, :] = 0; m[300:800, 10:1500] = 1
    assert outline.frame_sides(m) == set()


def test_a_whole_slab_framed_tightly_is_never_cropped_either():
    h, w = 1500, 2000
    stone = np.zeros((h, w), np.uint8); stone[150:1300, 8:1950] = 1          # its edge 8 px inside the photo
    beside = np.zeros_like(stone); beside[0:1300, 0:330] = 1
    assert retouch.crop_clear_of_wide_marks(beside, np.zeros_like(stone), stone) is None


# --- the 3D slab of a photo -------------------------------------------------------------------------------------------
def _standardised(monkeypatch, slabs, kind_found=None):
    """photo.standardise on found slabs (or a close-up), the models and the expensive steps stood in: (its result, the
    slabs the close view was made from)."""
    from stone_pipeline.standardise import closeup, photo
    monkeypatch.setattr(kinds, "classify", lambda clip, rgb: ("slab", 0.9))
    flats = None if slabs is None else [layout.Flat(rgb, alpha, np.eye(3), rgb) for rgb, alpha in slabs]
    monkeypatch.setattr(layout, "flat_slabs", lambda rgb, *m, size=None: flats if kind_found is None else "no outline")
    monkeypatch.setattr(layout, "enlarged", lambda flats, esr: slabs)   # the squared slab is its own enlarged one here
    monkeypatch.setattr(closeup, "closeup", lambda rgb, size, *m: kind_found)
    monkeypatch.setattr(photo, "_finished", lambda s, key, models, seed: np.zeros((C.OUT_H, C.OUT_W, 3), np.uint8))
    monkeypatch.setattr(studio, "place", lambda s: np.zeros((C.OUT_H, C.OUT_W, 3), np.uint8))
    closed = []                                                      # the slabs given to the close view, by identity
    monkeypatch.setattr(views, "close_up", lambda flat, full, edits: closed.append((id(flat.rgb), id(flat.alpha))) or np.ones((4, 4, 3), np.uint8))

    class _Models:
        clip, cleaners, esr, seg, det, lama = None, (None, None, None, None), None, None, None, None
    return photo.standardise(np.zeros((60, 90, 3), np.uint8), fmt="slab", key="p", models=_Models(), extra_views=True), closed


def test_each_half_of_an_open_book_is_a_whole_slab_and_the_one_with_more_stone_gets_the_3d_and_the_close_view(monkeypatch):
    left, right = _slab(80, 100), _slab(80, 100)
    left[1][:20, :20] = 0                                            # a chipped corner: less stone in its outline
    res, closed = _standardised(monkeypatch, [left, right])
    assert res.kind == "open book" and res.slab is right
    assert closed == [(id(right[0]), id(right[1]))] and set(res.images) == {"main", "white", "studio", "close"}


def test_of_two_equal_halves_the_left_is_taken(monkeypatch):
    left, right = _slab(80, 100), _slab(80, 100)
    res, closed = _standardised(monkeypatch, [left, right])
    assert res.slab is left and closed == [(id(left[0]), id(left[1]))]


def test_a_single_slab_keeps_its_own_slab_and_close_view(monkeypatch):
    one = _slab(80, 100)
    res, closed = _standardised(monkeypatch, [one])
    assert res.kind == "slab" and res.slab is one and closed == [(id(one[0]), id(one[1]))]
    assert set(res.images) == {"main", "white", "studio", "close"}


def test_a_close_up_shows_only_part_of_a_slab_so_it_has_no_3d_and_no_close_view(monkeypatch):
    res, closed = _standardised(monkeypatch, None, kind_found=[_slab(80, 100)])
    assert res.kind == "close-up" and res.slab == () and closed == []
    assert set(res.images) == {"main", "white"}
