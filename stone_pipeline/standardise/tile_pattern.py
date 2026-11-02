"""Laying patterns as lattices: every floor point (metres) is mapped to the tile it lies on and its place on that tile.

A pattern is a lattice (two translation vectors) of slots, each a tile plus its joint ((L + j) x (W + j)), the tile's
length along x: straight (one slot per cell) for a square, half-bond (rows offset by half a tile) for a rectangle.
"""
from dataclasses import dataclass

import numpy as np

from stone_pipeline.config.settings import STANDARDISE as C

_SLACK = 1e-9                      # metres: a point on a shared edge may round to just outside both slots


@dataclass(frozen=True)
class Pattern:
    a: tuple               # lattice vectors (metres)
    b: tuple
    length: float          # slot length: the tile's length plus the joint
    width: float           # slot width: the tile's width plus the joint


def for_tile(length_m, width_m):
    """The pattern a tile is laid in: straight for a square, half-bond for a rectangle (length >= width)."""
    if length_m < width_m:
        raise ValueError("a pattern lays a tile with its length along x: length must not be below width")
    L, W = length_m + C.TILE_JOINT_M, width_m + C.TILE_JOINT_M
    square = abs(length_m - width_m) < 1e-9
    return Pattern((L, 0.0), (0.0 if square else L / 2, W), L, W)


def locate(p, x, y):
    """(i, j, u, v) for floor points x, y (metres, arrays): the lattice cell (i, j) and the point's place in its slot
    along the tile's length (u) and width (v). Every point lies in exactly one slot."""
    inv = np.linalg.inv(np.array([[p.a[0], p.b[0]], [p.a[1], p.b[1]]]))
    t = inv[1, 0] * x + inv[1, 1] * y
    j = np.floor(t).astype(np.int64)                    # rows are exact: b has no x share beyond the row's offset
    dx = x - j * p.b[0]
    i = np.floor((dx + _SLACK) / p.a[0]).astype(np.int64)
    u = np.clip(dx - i * p.a[0], 0, p.length)
    v = np.clip(y - j * p.b[1], 0, p.width)
    return i, j, u, v


def _mix(*ints):
    """A deterministic integer hash of lattice coordinates (vectorised, wraps in uint64)."""
    h = np.full(np.shape(ints[0]), np.uint64(C.TILE_ROW_SEED), np.uint64)
    for n in ints:
        h ^= np.asarray(n).astype(np.int64).astype(np.uint64) + np.uint64(0x9E3779B97F4A7C15) + (h << np.uint64(6)) + (h >> np.uint64(2))
        h *= np.uint64(0xBF58476D1CE4E5B9)
    return h ^ (h >> np.uint64(31))


def rows(p, y_min, y_max):
    """(first, last) row of the lattice over floor y from y_min to y_max (metres): the rows a drawing spans."""
    return int(np.floor(y_min / p.b[1])), int(np.floor(y_max / p.b[1]))


def assign(p, i, j, n_faces, row_span):
    """(face index, quarter turns) per tile (i, j). Along a row the face steps by TILE_CUT_STEP (coprime to the face
    count, so neighbours differ); each row starts at a hashed offset from the row below, never the same face as either
    tile it rests on (half-bond: (i, j) and (i + 1, j)) when the face count allows. The offsets are summed from the
    first row of row_span (rows), so a drawing made in bands assigns every tile the same face. Turns: 0 or 2 for a
    rectangle, 0 to 3 for a square (a real layer turns tiles; never mirrors them)."""
    square = p.b[0] == 0.0 and abs(p.length - p.width) < 1e-9
    with np.errstate(over="ignore"):
        step = C.TILE_CUT_STEP % n_faces if np.gcd(C.TILE_CUT_STEP, n_faces) == 1 else 1 % n_faces
        below = {0, step} if p.b[0] else {0}
        jumps = np.array([d for d in range(1, n_faces) if d not in below] or list(range(1, n_faces)) or [0], np.int64)
        first, last = row_span
        span = np.arange(first, last + 1)
        offset = np.cumsum(jumps[(_mix(span, np.full_like(span, 7)) % np.uint64(len(jumps))).astype(np.int64)])
        index = (i * step + offset[j - first]) % n_faces
        turns = (_mix(i, j) >> np.uint64(5)).astype(np.int64) % (4 if square else 2)
    return index, turns * (1 if square else 2)
