"""Each brand's scope vocabulary: market_prices/vocabulary/<brand>.yaml, read only by its own brand's run.

load_vocabulary owns where the file lives and whose it is (brand.load_brand_document): a run reads its own brand's file
and no other. What a vocabulary holds, and how a listing is matched against it, are the scope fields of Vocabulary.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from market_prices import config
from market_prices.brand import load_brand_document


class VocabularyError(Exception):
    """The vocabulary cannot be used. Nothing is scoped on a vocabulary that does not validate."""


class Vocabulary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    brand: str


def load_vocabulary(run_brand: str, vocabulary_dir: Path = config.VOCABULARY_DIR) -> Vocabulary:
    """The validated vocabulary of the run's brand. Any problem raises VocabularyError."""
    return load_brand_document(vocabulary_dir, run_brand, Vocabulary, VocabularyError, "scope vocabulary")
