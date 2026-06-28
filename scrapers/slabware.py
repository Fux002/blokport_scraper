"""Shared helpers for the SlabWare-backed tenants (ferraz, varsha, …).

Several suppliers run the SAME SlabWare backend, so the response shapes and the photo/price/status
parsing are identical -- only the origin (`base`) and the per-tenant constants differ. These pure
helpers + the response regexes live here so a fix lands ONCE instead of being copy-pasted per tenant.
Each tenant imports them (aliasing to its local names) and binds its own `base`.
"""

from __future__ import annotations

import re

DISPLAY_RE = re.compile(r"class='([^']+)'", re.IGNORECASE)
_LOGIN_STUB_RE = re.compile(r'<\s*a\s|login\s*for\s*price', re.IGNORECASE)
# SlabWare's standard image paths; pass overrides to build_image_urls() if a tenant ever differs.
IMAGE_PATH_FULL = "/backendGranite/cadastros/Bundles/fotos/{bundle_id}/{filename}"
IMAGE_PATH_THUMB = "/backendGranite/cadastros/Bundles/fotos/{bundle_id}/{stem}_crop_555x300{ext}"


def slab_get(d, key, default=''):
    v = d.get(key)
    if v is None or v == '':
        return default
    return v


def parse_display_status(html):
    """The displayProduct field is a tiny HTML span. Extract a clean status."""
    if not html:
        return ''
    m = DISPLAY_RE.search(html)
    if not m:
        return ''
    classes = m.group(1).lower()
    if 'recommended-product' in classes and 'display: block' in html.lower():
        return 'recommended'
    if 'new-product' in classes and 'display: block' in html.lower():
        return 'new'
    return ''


def clean_price(value):
    """Return '' if the value is an HTML 'Login for Price' stub, else as-is."""
    if not value:
        return ''
    s = str(value)
    if _LOGIN_STUB_RE.search(s):
        return ''
    return s


def join_slabs(chapas, key):
    """Pipe-join a single field across all slabs, e.g. all widths."""
    if not chapas:
        return ''
    return ' | '.join(str(c.get(key, '') or '') for c in chapas)


def join_photos(fotos, base):
    """Convert an array of photo paths to a list of full source URLs (`base` = the tenant origin)."""
    out = []
    for path in fotos or []:
        if not path:
            continue
        if path.startswith('http'):
            out.append(path)
        elif path.startswith('/'):
            out.append(base + path)
        else:
            out.append(base + '/' + path)
    return out


def build_image_urls(bundle_id, filename, base, full_tmpl=IMAGE_PATH_FULL, thumb_tmpl=IMAGE_PATH_THUMB):
    """Return (full_url, thumb_url) for a bundle's primary photo."""
    if not bundle_id or not filename:
        return ('', '')
    if '.' in filename:
        stem, _dot, ext = filename.rpartition('.')
        ext = '.' + ext
    else:
        stem, ext = filename, '.jpg'
    full = base + full_tmpl.format(bundle_id=bundle_id, filename=filename)
    thumb = base + thumb_tmpl.format(bundle_id=bundle_id, stem=stem, ext=ext)
    return (full, thumb)
