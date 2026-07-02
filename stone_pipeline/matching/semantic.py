"""Semantic suggestion tier (section 5A.2 tier 8).

Sentence-embedding nearest neighbour over canonical plus alias strings, for
cross-language and true-synonym cases that string methods miss (Bianco Carrara
vs Carrara White). Used only as a last suggestion feeding review, never
auto-accept, because it is the least explainable.

The embedder is pluggable. The real one wraps sentence-transformers (lazy import,
config-gated). A deterministic hashing embedder is provided so the tier is
testable offline without the heavy dependency and so the pipeline never hard
-depends on it.
"""

from __future__ import annotations

import math
import zlib
from typing import Callable, Optional, Protocol

from stone_pipeline.core.text import match_key

Vector = list[float]
Embedder = Callable[[list[str]], list[Vector]]


class _STModel(Protocol):
    def encode(self, sentences: list[str]) -> list[Vector]: ...


def hashing_embedder(dim: int = 64) -> Embedder:
    """A tiny deterministic bag-of-character-trigrams embedder. Not as good as a
    real model, but stable, dependency-free, and enough to exercise and test the
    tier. Vectors are L2-normalized so dot product is cosine similarity."""

    def encode(texts: list[str]) -> list[Vector]:
        out: list[Vector] = []
        for text in texts:
            vec = [0.0] * dim
            norm = match_key(text)   # shared canonical key: fold accents + punctuation before trigrams
            padded = f"  {norm}  "
            for i in range(len(padded) - 2):
                tri = padded[i : i + 3]
                # crc32, NOT the builtin hash() -- str hashing is salted per process (PYTHONHASHSEED),
                # which would make the "deterministic" embedder differ run-to-run.
                vec[zlib.crc32(tri.encode("utf-8")) % dim] += 1.0
            length = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / length for v in vec])
        return out

    return encode


def sentence_transformer_embedder(model_name: str = "all-MiniLM-L6-v2") -> Embedder:
    """Lazy wrapper around sentence-transformers; only imported when enabled."""

    from sentence_transformers import SentenceTransformer  # lazy, optional dep

    model = SentenceTransformer(model_name)

    def encode(texts: list[str]) -> list[Vector]:
        return [list(v) for v in model.encode(texts, normalize_embeddings=True)]

    return encode


def _cosine(a: Vector, b: Vector) -> float:
    return sum(x * y for x, y in zip(a, b))


class SemanticSuggester:
    """Nearest-neighbour over candidate surface strings. Returns a suggestion in
    the 0..100 range to sit alongside the other tiers' scores."""

    def __init__(self, names: list[str], cids: list[str], embedder: Embedder):
        self.names = names
        self.cids = cids
        self._vectors = embedder(names) if names else []
        self._embedder = embedder

    def suggest(self, query: str) -> Optional[tuple[str, str, float]]:
        """Return (cid, name, score 0..100) for the nearest candidate, or None."""
        if not self._vectors:
            return None
        qvec = self._embedder([query])[0]
        best_i, best_score = -1, -1.0
        for i, vec in enumerate(self._vectors):
            score = _cosine(qvec, vec)
            if score > best_score:
                best_i, best_score = i, score
        if best_i < 0:
            return None
        return self.cids[best_i], self.names[best_i], round(best_score * 100, 1)
