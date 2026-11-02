"""imagestore.py is the single source of truth for the product-image S3 layout. These lock the pure
path logic the image stage and the photo process rely on (the raw/scraped/improved prefixes, the improved
marker, the sha every hosted key carries)."""

from __future__ import annotations

from stone_pipeline.config.settings import ENV_SEGMENT
from stone_pipeline.io import imagestore


def test_prefixes_and_improved_marker_are_consistent():
    assert imagestore.raw_prefix("zucchi") == f"{ENV_SEGMENT}/products/zucchi/"
    assert imagestore.scraped_prefix("marenostone") == f"{ENV_SEGMENT}/products/scraped/marenostone/"
    # the substring the product-link path tests to tell a TREATED url from a raw one
    assert imagestore.IMPROVED_MARKER in f"https://bkt/{ENV_SEGMENT}/products/improved/zucchi/aa.jpg"
    assert imagestore.IMPROVED_MARKER not in f"https://bkt/{ENV_SEGMENT}/products/zucchi/aa.jpg"


def test_sha_from_url():
    h = "a" * 64
    assert imagestore.sha_from_url(f"https://s3/dev/products/improved/x/{h}.jpg") == h
    assert imagestore.sha_from_url(f"{ENV_SEGMENT}/products/standard/main/x/{h}.jpg") == h
    assert imagestore.sha_from_url("https://s3/dev/products/improved/x/logo.png") is None
    assert imagestore.sha_from_url("") is None
