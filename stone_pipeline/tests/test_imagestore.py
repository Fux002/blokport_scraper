"""imagestore.py is the single source of truth for the product-image S3 layout. These lock the pure
path logic that images.py, treat.py, and reprocess_source all rely on (parse, improved_key, repoint)."""

from __future__ import annotations

from stone_pipeline.config.settings import ENV_SEGMENT
from stone_pipeline.io import imagestore


def test_parse_raw_key():
    assert imagestore.parse_raw_key(f"{ENV_SEGMENT}/products/zucchi/abc.jpg") == ("zucchi", "abc.jpg")
    assert imagestore.parse_raw_key(f"{ENV_SEGMENT}/products/scraped/marenostone/x.jpg") == ("marenostone", "x.jpg")
    assert imagestore.parse_raw_key(f"{ENV_SEGMENT}/products/improved/zucchi/abc.jpg") is None   # already treated
    assert imagestore.parse_raw_key(f"{ENV_SEGMENT}/products/_manifest.json") is None            # not an image
    assert imagestore.parse_raw_key(f"{ENV_SEGMENT}/products/zucchi/abc.txt") is None            # not an image


def test_improved_key_is_path_consistent():
    # the treated object key must match the URL the repoint produces, for BOTH raw layouts + any sub-path
    assert imagestore.improved_key(f"{ENV_SEGMENT}/products/zucchi/aa.jpg") == f"{ENV_SEGMENT}/products/improved/zucchi/aa.jpg"
    assert imagestore.improved_key(f"{ENV_SEGMENT}/products/scraped/marenostone/x.jpg") == f"{ENV_SEGMENT}/products/improved/marenostone/x.jpg"
    assert imagestore.improved_key(f"{ENV_SEGMENT}/products/zucchi/sub/a.jpg") == f"{ENV_SEGMENT}/products/improved/zucchi/sub/a.jpg"


def test_raw_to_improved_url_and_prefixes():
    base = "https://bkt.s3.eu-west-1.amazonaws.com"
    assert imagestore.raw_to_improved_url(f"{base}/{ENV_SEGMENT}/products/zucchi/aa.jpg", "zucchi") \
        == f"{base}/{ENV_SEGMENT}/products/improved/zucchi/aa.jpg"
    assert imagestore.raw_to_improved_url(f"{base}/{ENV_SEGMENT}/products/scraped/varsha/y.jpg", "varsha") \
        == f"{base}/{ENV_SEGMENT}/products/improved/varsha/y.jpg"
    # idempotent + never touches another source
    already = f"{base}/{ENV_SEGMENT}/products/improved/zucchi/bb.jpg"
    assert imagestore.raw_to_improved_url(already, "zucchi") == already
    other = f"{base}/{ENV_SEGMENT}/products/marenostone/cc.jpg"
    assert imagestore.raw_to_improved_url(other, "zucchi") == other
    # prefixes + marker are consistent
    assert imagestore.improved_prefix("zucchi") == f"{ENV_SEGMENT}/products/improved/zucchi/"
    assert imagestore.IMPROVED_MARKER in f"{base}/{ENV_SEGMENT}/products/improved/zucchi/aa.jpg"
