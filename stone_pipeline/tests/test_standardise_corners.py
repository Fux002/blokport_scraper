"""Rounded and broken slab corners on real photos (prod, 2026-11-02 audit): the wall the photo shows in such a corner is
never drawn as stone, a corner broken off keeps its true shape (never filled with stone), and a slab with square corners
or a broken corner already drawn true is drawn exactly as before.

Each fixture is a real photo the standardiser worked from (prod run folder, the retouched or cleaned copy it used), at
640 px, with SAM's own masks of it (the slab mask, what SAM counted as stone, the straps, the text marks) dumped on the
dev GPU (corners trial) and scaled with it; the slab's quad fitted on the scaled mask by marks.four_corners, as
find_slabs does. The models are stood in: SAM and the detector by those masks, LaMa by OpenCV's inpainting,
Real-ESRGAN by a cubic resize; everything after them is the real code. Listings: notch_top_right afyon-white-polished-
sen-1325, notch_top_left afyon-sugar-white-honed-sen-1151, broken_top_corners_7074 afyon-white-honed-sen-7074-10,
broken_already_true_7074 afyon-white-honed-sen-7074-2, square_calacatta_gold calacatta-gold-polished-sen-7034-9,
square_bianco_impala bianco-impala-polished-zuc-bd00204766."""
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from stone_pipeline.standardise import layout, marks, outline, studio

FIXTURES = Path(__file__).parent / "fixtures" / "standardise_corners"
CORNER = 0.04                        # the corner patch checked: this share of the squared slab's height, square
DARK = 120                           # a pixel darker than this (mean of R, G, B) on a pale slab's white view is the wall


def _fixture(name):
    d = np.load(FIXTURES / f"{name}.npz")
    rgb = cv2.cvtColor(cv2.imread(str(FIXTURES / f"{name}.jpg")), cv2.COLOR_BGR2RGB)
    return rgb, d


class _Esr:
    @staticmethod
    def enhance(bgr):
        return cv2.resize(bgr, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)


def _lama(image, mask):
    return Image.fromarray(cv2.inpaint(np.asarray(image), np.asarray(mask), 5, cv2.INPAINT_TELEA))


def _patch(monkeypatch, quads, mask, raw, straps, text):
    """The outline step's models stood in by masks: SAM and the detector (find_slabs), the text reader (mark_mask)."""
    monkeypatch.setattr(outline, "find_slabs", lambda seg, det, img: (quads, mask, raw, straps, ""))
    monkeypatch.setattr(marks, "mark_mask", lambda img, on=None: text.copy())


def _found(monkeypatch, name):
    """(the fixture's photo, its outline step: layout.slab_outline)."""
    rgb, d = _fixture(name)
    _patch(monkeypatch, [q for q in d["quads"]], d["mask"], d["raw"], d["straps"], d["text"])
    return rgb, layout.slab_outline(rgb, None, None, tuple(d["size"]) if d["size"].size else None)


def _squared(monkeypatch, name):
    """(the outline step, the slabs as drawn: layout.flats_from, then layout.enlarged) of a fixture."""
    rgb, found = _found(monkeypatch, name)
    return found, layout.enlarged(layout.flats_from(rgb, found, _lama), _Esr())


def _older_rule(found):
    """The outline under the rule before OUTLINE_CHECKED: every erased mark grown inside the fitted corners."""
    return found.seen | ((found.cards | found.objects) & found.inside)


def _corner(a, which):
    k = max(4, int(CORNER * a.shape[0]))
    rows = slice(0, k) if which[0] == "t" else slice(a.shape[0] - k, a.shape[0])
    cols = slice(0, k) if which[1] == "l" else slice(a.shape[1] - k, a.shape[1])
    return a[rows, cols]


def _white_corner(slabs, which):
    """The white view's pixels in the slab's corner: the canvas's patch where that corner of the squared slab lands."""
    rgb, cover = layout._white_slabs(slabs)
    img = layout.shadowed_on_white(rgb, cover)
    ys, xs = np.nonzero(cover > 0.5)
    box = (ys.min(), ys.max(), xs.min(), xs.max())
    k = max(4, int(CORNER * (box[1] - box[0])))
    y = box[0] if which[0] == "t" else box[1] - k
    x = box[2] if which[1] == "l" else box[3] - k
    return img[y:y + k, x:x + k]


def _edge(a):
    """The studio's sawn edge rows of a squared slab (studio.place takes its last columns)."""
    return studio.edge_rows(a)


def _top_rows(a):
    return slice(0, max(4, int(CORNER * a.shape[0])))


# --- the wall in a rounded or broken corner is gone ---------------------------------------------------------------------
@pytest.mark.parametrize("name, corners", [("notch_top_right", ("tr",)), ("notch_top_left", ("tl",)),
                                           ("broken_top_corners_7074", ("tl", "tr"))])
def test_the_wall_in_a_top_corner_is_never_drawn(monkeypatch, name, corners):
    found, slabs = _squared(monkeypatch, name)
    (rgb, a), = slabs
    for which in corners:
        assert _corner(a, which).mean() < 0.25, which                  # the corner is transparent: no stone, no wall
        assert (_white_corner(slabs, which).mean(axis=2) < DARK).mean() < 0.01, which   # white there on the white view
    if "tr" in corners:
        assert _edge(a)[_top_rows(a)].max() == 0                       # no sawn edge up past the corner in the studio
    assert layout.drawn_differently(found)                             # so a result made before it is drawn again


def test_a_broken_corner_keeps_its_true_shape_and_is_never_filled_with_stone(monkeypatch):
    """7074: the block's slabs are broken off at their top corners; the outline is what SAM saw as stone there."""
    _, found = _found(monkeypatch, "broken_top_corners_7074")
    wall = layout.wall_in_corners(found.cards, found.objects, found.inside, found.stone)
    assert wall.any() and not (found.outline() & wall).any()           # where the older rule drew the wall as stone
    assert not (found.outline() & (1 - found.stone) & (1 - found.cards) & (1 - found.seen)).any()


# --- square corners stay exactly as they were; a broken corner already drawn true keeps its outline -----------------
@pytest.mark.parametrize("name", ["square_calacatta_gold", "square_bianco_impala"])
def test_a_slab_with_square_corners_is_drawn_exactly_as_before_and_never_drawn_again(monkeypatch, name):
    """Byte for byte: the outline is the older rule's, the studio's sawn edge the full strip as before, and everything
    else is the same code on the same input; so a recheck stops before drawing anything."""
    found, slabs = _squared(monkeypatch, name)
    assert np.array_equal(found.outline(), _older_rule(found))
    assert all((_edge(a) == 1).all() for _, a in slabs)
    assert not layout.drawn_differently(found)


def test_a_broken_corner_already_drawn_true_keeps_its_outline_and_only_the_wall_beside_it_in_the_studio_goes(monkeypatch):
    """7074: a slab of the block whose broken top-right corner the older rule already left out. Its outline, so its
    white and showroom views and 3D slab, are as before; the studio drew its sawn edge up past the break from the wall
    there, which goes, so a recheck draws it again."""
    found, slabs = _squared(monkeypatch, "broken_already_true_7074")
    assert np.array_equal(found.outline(), _older_rule(found))
    (_, a), = slabs
    assert _corner(a, "tr").mean() < 0.25 and _edge(a)[_top_rows(a)].max() == 0
    assert layout.drawn_differently(found)


# --- an open book's inner corners and a label on a corner (synthetic photos) -------------------------------------------
def _synthetic(monkeypatch, body, quads, text=None):
    """layout.flat_slabs, then layout.enlarged, on a pale stone `body` over a dark wall, SAM's masks stood in by body
    itself."""
    rgb = np.full(body.shape + (3,), 45, np.uint8)
    rng = np.random.default_rng(3)
    rgb[body > 0] = (205 + rng.integers(-12, 12, (int(body.sum()), 1))).clip(0, 255).astype(np.uint8)
    _patch(monkeypatch, quads, body.copy(), body.copy(), np.zeros_like(body), np.zeros_like(body) if text is None else text)
    return layout.enlarged(layout.flat_slabs(rgb, None, None, _lama), _Esr())


def test_an_open_book_s_inner_corners_show_no_wall(monkeypatch):
    body = np.zeros((600, 1200), np.uint8)
    body[50:550, 50:1150] = 1
    body[50:130, 520:680] = 0                          # where the halves meet at the top, both rounded (radius 80)
    cv2.circle(body, (520, 130), 80, 1, -1)
    cv2.circle(body, (680, 130), 80, 1, -1)
    body[:50] = 0
    quads = [np.float32([[50, 50], [599, 50], [599, 549], [50, 549]]), np.float32([[600, 50], [1149, 50], [1149, 549], [600, 549]])]
    (l, la), (r, ra) = _synthetic(monkeypatch, body, quads)
    assert _corner(la, "tr").mean() < 0.25 and _corner(ra, "tl").mean() < 0.25
    assert _corner(la, "tl").mean() > 0.9 and _corner(ra, "tr").mean() > 0.9   # the outer corners are square stone


def test_a_label_on_a_corner_is_filled_whole(monkeypatch):
    body = np.zeros((600, 900), np.uint8)
    body[50:550, 50:850] = 1
    body[50:140, 730:850] = 0                          # a printed label on the top-right corner: SAM left it out
    text = np.zeros_like(body)
    text[50:140, 730:850] = 1                          # and the text reader found it
    (rgb, a), = _synthetic(monkeypatch, body, [np.float32([[50, 50], [849, 50], [849, 549], [50, 549]])], text)
    assert _corner(a, "tr").mean() > 0.9               # its place is stone: the label sat on the slab


# --- an object or a card on a square corner ------------------------------------------------------------------------------
def _square_with_corner_mark(monkeypatch, read_as_text):
    """The outline step of a square pale slab with a 50 x 60 mark on its top-right corner that SAM did not count as stone:
    a clip (an object) or, read_as_text, a printed card."""
    body = np.zeros((600, 900), np.uint8)
    body[50:550, 50:850] = 1
    body[50:110, 800:850] = 0                          # the mark, over the corner
    text = np.zeros_like(body)
    if read_as_text:
        text[50:110, 800:850] = 1
    rgb = np.full(body.shape + (3,), 45, np.uint8)
    rgb[body > 0] = 205
    _patch(monkeypatch, [np.float32([[50, 50], [849, 50], [849, 549], [50, 549]])], body.copy(), body.copy(),
           np.zeros_like(body), text)
    return layout.slab_outline(rgb, None, None)


def test_a_clip_on_a_square_corner_is_drawn_differently_so_its_recheck_redraws_it_for_the_judge(monkeypatch):
    """The stone's hull cuts the corner under the clip diagonally: this version draws it otherwise than the older rule, so
    a recheck redraws it, and the redraw is published only once the judge passes it (stone_pipeline.photos.source.rework,
    test_standardise_process)."""
    found = _square_with_corner_mark(monkeypatch, read_as_text=False)
    assert layout.wall_in_corners(found.cards, found.objects, found.inside, found.stone).sum() > 1000
    assert layout.drawn_differently(found)


def test_a_card_on_a_square_corner_is_filled_whole_and_drawn_as_before(monkeypatch):
    found = _square_with_corner_mark(monkeypatch, read_as_text=True)
    assert not layout.wall_in_corners(found.cards, found.objects, found.inside, found.stone).any()
    assert not layout.drawn_differently(found)
