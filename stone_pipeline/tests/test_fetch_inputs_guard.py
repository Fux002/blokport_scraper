"""The attribute vocabulary is a committed cold-start seed; the S3 fetch must not overwrite it with a
thin/empty Medusa export (mid-reset), or nothing resolves. Guards _would_clobber + the protected set."""

from __future__ import annotations

from deploy import fetch_inputs


def test_attributes_is_a_protected_base():
    assert "attributes.csv" in fetch_inputs._PROTECTED_BASE


def test_variant_exports_are_protected_bases():
    # the live matching reference and the committed full base must not be clobbered by a thin S3 export
    # (Medusa mid-reset): else the matcher starts from a thin set and treats the catalog as new.
    assert "variants_export.csv" in fetch_inputs._PROTECTED_BASE
    assert "variants_export_base.csv" in fetch_inputs._PROTECTED_BASE


def test_would_clobber_blocks_a_thin_overwrite_but_allows_growth():
    assert fetch_inputs._would_clobber(3, 1000)          # thin S3 copy vs full local -> keep local
    assert not fetch_inputs._would_clobber(1200, 1000)   # fuller S3 copy -> take it
    assert not fetch_inputs._would_clobber(600, 1000)    # within tolerance -> normal churn
    assert not fetch_inputs._would_clobber(0, 0)         # no local baseline -> nothing to protect
