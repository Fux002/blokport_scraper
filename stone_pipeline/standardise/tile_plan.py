"""Which slab stone each tile listing of the product sheet is rendered from, and the files it gets (a tile listing is
shown from its variety's judge-passed, published slab stone, from any vendor's slab of that variety).

Never a guess: the stone is a slab listing's published 3D cut-out of the same variety (stone type and name) AND the
same finish. A tile listing without one gets no files (it stays hidden by the no-photo rule), with the reason.

One slab per variety and finish, never a pool: two slabs of one variety can be different lots or a mislabelled stone,
and colour and texture statistics cannot tell that from two photos lit differently (on prod: Pietra Grey
8160c6, the same stone shot darker, is 17 L* from its pool; 902c4b, a brown quartzite, under 1). A floor cut from
one slab is one real stone, as a tile order from one lot is. The slabs are tried in one order: the tile's own vendor's
first (the stone it ships), then the most usable stone (square metres), then the cut-out key; a listing takes the first
on which a tile of its size fits, so the sizes of a variety show the same slab wherever they fit.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Optional

from stone_pipeline.config.settings import ATTRIBUTES_STATUS_REQUIRED, VARIANTS_EXPORT_STATUS_REQUIRED
from stone_pipeline.config.standardise_settings import STANDARDISE as C
from stone_pipeline.io import imagestore
from stone_pipeline.reference.medusa_files import read_attributes, read_variants_export
from stone_pipeline.standardise import index, tile_stone, tile_views


@dataclass(frozen=True)
class Product:
    key: str                       # the listing (Product Handle)
    source: str
    fmt: str
    variety: Optional[tuple]       # (stone type id, variety name); None when its variation is unknown
    finish: str                    # finish id
    size: Optional[tuple]          # (length, width, height) in metres as stored; None when not all positive


@dataclass(frozen=True)
class Slab:
    """A slab listing's published stone."""
    listing: str
    source: str
    sha: str                       # its chosen photo
    size: tuple                    # (length, width) in metres
    cutout: str                    # the published cut-out's key ...
    etag: str                      # ... and content


def _size(row):
    try:
        size = tuple(float(row[c]) for c in ("Product Length", "Product Width", "Product Height"))
    except (KeyError, TypeError, ValueError):
        return None
    return size if all(v > 0 for v in size) else None


def variety_names(variants_csv: str) -> dict:
    """{Medusa variation id: variety name} of the variations export text."""
    return {v.id: v.name for v in read_variants_export(variants_csv, require_status=VARIANTS_EXPORT_STATUS_REQUIRED)
            if v.id and v.name}


def finish_ids(attributes_csv: str) -> set:
    """The finish ids of the attributes.csv text."""
    return {a.sourceid for a in read_attributes(attributes_csv, require_status=ATTRIBUTES_STATUS_REQUIRED)
            if a.category == "finish"}


def products(sheet_text: str, photos: dict, names: dict) -> list:
    """Every product of the sheet with a photo in the photo list (its source is its photos'), in sheet order."""
    out = []
    for row, fmt, shown in index.rows(sheet_text, photos):
        if not shown:
            continue
        name = names.get(row["STN Variation Id"])
        out.append(Product(row["Product Handle"], shown[0][0], fmt, (row["STN Type Id"], name) if name else None,
                           row["STN Finish Id"], _size(row)))
    return out


def tiles(all_products: list) -> list:
    return [p for p in all_products if p.fmt == C.TILE_FORMAT]


def slabs(all_products: list, chosen: dict, cutouts: dict) -> dict:
    """{(variety, finish): [Slab]} of the slab listings whose chosen photo has a published 3D cut-out (both its files),
    each list sorted. chosen = {listing: choice record}; cutouts = {key: ETag} of the published cut-out files."""
    out = {}
    for p in all_products:
        record = chosen.get(p.key)
        if p.fmt != C.CUTOUT_FORMAT or record is None or p.variety is None or p.size is None:
            continue
        webp, meta = (imagestore.cutout_key(record["source"], record["sha"], ext) for ext in ("webp", "json"))
        if webp in cutouts and meta in cutouts:
            out.setdefault((p.variety, p.finish), []).append(
                Slab(p.key, record["source"], record["sha"], p.size[:2], webp, cutouts[webp].strip('"')))
    return {k: sorted(v, key=lambda s: (s.cutout, s.listing)) for k, v in out.items()}


def no_stone(product: Product, all_products: list, finishes: set) -> str:
    """Why a tile listing has no slab stone of its variety and finish ("" when it may have one)."""
    if product.size is None:
        return "no size: length, width and height must all be positive"
    if not tile_size(product.size):
        length, width, thick = product.size
        return (f"not a tile's size: {length:g} x {width:g} x {thick:g} m (sides {C.TILE_SIDE_RANGE_M}, thickness "
                f"{C.TILE_THICKNESS_RANGE_M} m)")
    if product.variety is None:
        return "its variation is not in the variations export"
    if product.finish not in finishes:
        return f"unknown finish {product.finish!r}"
    same = [p for p in all_products if p.fmt == C.CUTOUT_FORMAT and p.variety == product.variety]
    if not same:
        return "no slab of this variety"
    if not any(p.finish == product.finish for p in same):
        return "no slab of this variety in this finish"
    return ""


def tile_size(size: tuple) -> bool:
    """Whether a listing's (length, width, height) is a tile's (TILE_SIDE_RANGE_M, TILE_THICKNESS_RANGE_M)."""
    length, width, thick = size
    (lo, hi), (t_lo, t_hi) = C.TILE_SIDE_RANGE_M, C.TILE_THICKNESS_RANGE_M
    return lo <= length <= hi and lo <= width <= hi and t_lo <= thick <= t_hi


def ranked(candidates: list, source: str, areas: dict) -> list:
    """The slabs a tile listing may be cut from, in the order they are tried: those whose stone was not refused
    (areas = {cut-out key: usable m2}), the tile's own vendor's first, then the most usable stone, then the cut-out key
    (deterministic)."""
    usable = [s for s in candidates if s.cutout in areas]
    return sorted(usable, key=lambda s: (s.source != source, -areas[s.cutout], s.cutout))


def stamp(product: Product, slab: Slab) -> str:
    """What a tile listing's files are made from: the slab's cut-out and its content, the listing's size and finish, and
    the renderer's version. Any change re-renders them; nothing else does."""
    made = {"renderer": C.TILE_RENDER_VERSION, "listing": product.key, "stone": slab.cutout, "etag": slab.etag,
            "size": [round(v, 6) for v in product.size], "finish": product.finish}
    return hashlib.sha256(json.dumps(made, sort_keys=True).encode()).hexdigest()


# Every tile listing's views: main, its catalog image, = the laid tiles from above at
# true scale, also when its slab gives only a few distinct faces (the floor then repeats them, turned, as a layer from
# one lot does); white = one tile face-on; studio = one tile at an angle (its thickness); the 3D tile beside them. The
# close slot is never used: the main view is the close-up.
VIEWS = ("main", "white", "studio")


def keys(product: Product) -> list:
    """Every file of a rendered tile listing, the record last (written last: it means the set is complete)."""
    sha = imagestore.listing_sha(product.key)
    return ([imagestore.standard_key(product.source, sha, v) for v in VIEWS]
            + [imagestore.cutout_key(product.source, sha, ext) for ext in ("webp", "json")]
            + [imagestore.tile_record_key(product.source, sha)])


def faces_of(product: Product, stone: tile_stone.Stone) -> tile_stone.Cuts:
    """The distinct faces of this listing's tile on the stone. Refused when not one fits."""
    length, width, _ = product.size
    cuts = tile_stone.cuts(stone, max(length, width), min(length, width))
    if not cuts.origins:
        raise tile_stone.Refused(f"no {length:g} x {width:g} m tile fits on the slab's usable stone")
    return cuts


def render(product: Product, slab: Slab, stone: tile_stone.Stone, cuts: tile_stone.Cuts) -> dict:
    """{key: (bytes, content type)} of a tile listing's files (keys order), its record last: what they were made from."""
    length, width, thick = product.size
    faces = tile_stone.faces(stone, cuts)
    tile = tile_views.Tile(tuple(faces), tile_stone.hero(stone, cuts), stone.ppm, max(length, width), min(length, width),
                           thick)
    face = tile_views.oriented(tile, length, width)
    drawn = {"main": tile_views.close(tile), "white": tile_views.single(face), "studio": tile_views.side(face, product.size)}
    webp, meta = tile_views.model(face, stone.ppm)
    record = {"listing": product.key, "stamp": stamp(product, slab), "renderer": C.TILE_RENDER_VERSION,
              "stone": {"listing": slab.listing, "source": slab.source, "sha": slab.sha, "cutout": slab.cutout,
                        "etag": slab.etag, "px_per_mm": round(stone.ppm / 1000, 4)},
              "size_m": list(product.size), "faces": len(faces), "views": list(VIEWS)}
    blobs = [(tile_views.encode_jpeg(drawn[v]), "image/jpeg") for v in VIEWS]
    blobs += [(webp, "image/webp"), (meta, "application/json"),
              (json.dumps(record, sort_keys=True).encode(), "application/json")]
    return dict(zip(keys(product), blobs))

