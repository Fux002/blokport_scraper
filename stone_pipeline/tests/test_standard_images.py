"""Products link to their standardised photos when a published set exists: the showroom image first (the catalog
thumbnail), then the white, side and close-up views of that photo, then each further photo's white view; a product
with no standardised photo keeps the images it had."""
from __future__ import annotations

from stone_pipeline.config.settings import ENV_SEGMENT
from stone_pipeline.io import imagestore
from stone_pipeline.stages import images

A, B, C_ = "a" * 64, "b" * 64, "c" * 64
BASE = f"https://bucket.s3.amazonaws.com/{ENV_SEGMENT}/products"


def _improved(sha, src="sentas"):
    return f"{BASE}/improved/{src}/{sha}.png"


def _std(view, sha, src="sentas"):
    return f"{BASE}/standard/{view}/{src}/{sha}.jpg"


def test_the_standard_key_carries_the_sha_like_every_image_key():
    key = imagestore.standard_key("sentas", A, "main")
    assert key == f"{ENV_SEGMENT}/products/standard/main/sentas/{A}.jpg"
    assert imagestore.sha_from_url(key) == A


def test_a_standardised_product_shows_the_showroom_first_then_its_gallery():
    views = {"main": {A, B}, "white": {A, B}, "studio": {A, B}, "close": {A}}
    out = images.standardised([_improved(A), _improved(B)], views)
    assert out == [_std("main", A), _std("white", A), _std("studio", A), _std("close", A), _std("white", B)]


def test_an_unstandardised_photo_is_dropped_when_others_are_standardised():
    views = {"main": {B}, "white": {B}, "studio": set(), "close": set()}
    out = images.standardised([_improved(A), _improved(B)], views)
    assert out == [_std("main", B), _std("white", B)]


def test_a_product_with_no_standardised_photo_keeps_its_images():
    urls = [_improved(A), _improved(C_)]
    assert images.standardised(urls, {"main": {B}, "white": set(), "studio": set(), "close": set()}) == urls


def test_a_further_photo_without_a_white_view_shows_its_main_image():
    views = {"main": {A, B}, "white": {A}, "studio": set(), "close": set()}       # B is a block: one image only
    out = images.standardised([_improved(A, "zucchi"), _improved(B, "zucchi")], views)
    assert out == [_std("main", A, "zucchi"), _std("white", A, "zucchi"), _std("main", B, "zucchi")]


def test_a_finished_run_is_published_view_by_view_and_nothing_else():
    from deploy import standardise_publish as pub
    run = f"{ENV_SEGMENT}/standardise-test/full/sentas/"
    keys = [f"{run}{A}.main.jpg", f"{run}{A}.white.jpg", f"{run}{A}.studio.jpg", f"{run}{A}.close.jpg", f"{run}{A}.slab.png",
            f"{run}held/{B}.json", f"{run}retouched/{A}.jpg", f"{run}{B}.angle_left.jpg"]
    assert pub.copies("sentas", run, keys) == sorted(
        (f"{run}{A}.{v}.jpg", imagestore.standard_key("sentas", A, v)) for v in imagestore.STANDARD_VIEWS)
