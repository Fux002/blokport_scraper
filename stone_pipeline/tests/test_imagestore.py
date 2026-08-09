"""imagestore.py is the single source of truth for the product-image S3 layout. These lock the pure
path logic images.py and reprocess_source rely on (the raw/scraped/improved prefixes + the improved
marker). sha_from_url is exercised in test_image_discard."""

from __future__ import annotations

from stone_pipeline.config.settings import ENV_SEGMENT
from stone_pipeline.io import imagestore


def test_prefixes_and_improved_marker_are_consistent():
    assert imagestore.raw_prefix("zucchi") == f"{ENV_SEGMENT}/products/zucchi/"
    assert imagestore.scraped_prefix("marenostone") == f"{ENV_SEGMENT}/products/scraped/marenostone/"
    assert imagestore.improved_prefix("zucchi") == f"{ENV_SEGMENT}/products/improved/zucchi/"
    # the substring the product-link path tests to tell a TREATED url from a raw one
    assert imagestore.IMPROVED_MARKER in f"https://bkt/{ENV_SEGMENT}/products/improved/zucchi/aa.jpg"
    assert imagestore.IMPROVED_MARKER not in f"https://bkt/{ENV_SEGMENT}/products/zucchi/aa.jpg"
