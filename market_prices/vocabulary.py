"""Each brand's scope vocabulary: market_prices/vocabulary/<brand>.yaml, read only by its own brand's run.

load_vocabulary owns where the file lives and whose it is (brand.load_brand_document): a run reads its own brand's file
and no other. What a vocabulary holds, and how a listing is matched against it, are the scope fields of Vocabulary:

- `scope: all` keeps every listing the collector reads (declared, never a default);
- `scope: words` keeps a listing whose text holds a word of a category in its market's languages.

The one question a collector asks is Vocabulary.in_scope(listing_text, market), after the fetch, for every platform.
Text and words compare after core.text.match_key (case, accents and punctuation do not count), and a word may sit
inside a longer word, so a compound ("Birkenmultiplex", "bjorkplywood") is kept.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, PrivateAttr, StringConstraints, field_validator, model_validator

from market_prices import config
from market_prices.brand import SLUG, load_brand_document
from stone_pipeline.core.text import match_key

Language = Annotated[str, StringConstraints(pattern=r"^[a-z]{2}$")]          # ISO 639-1, the listing's language
Market = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]            # ISO 3166-1 alpha-2: a served_market
Word = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class VocabularyError(Exception):
    """The vocabulary cannot be used. Nothing is scoped on a vocabulary that does not validate."""


class Scope(str, Enum):
    all = "all"
    words = "words"


class Category(BaseModel):
    """One kind of in-scope listing, in the sellers' own words per language. An empty list is a stated gap."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    category: str
    words: dict[Language, tuple[Word, ...]]

    @field_validator("category")
    @classmethod
    def _category_is_a_slug(cls, value: str) -> str:
        if not SLUG.fullmatch(value):
            raise ValueError(f"category {value!r} must match {SLUG.pattern}")
        return value

    @model_validator(mode="after")
    def _words_compare_once(self) -> "Category":
        for language, words in self.words.items():
            keys = [match_key(word) for word in words]
            if empty := [word for word, key in zip(words, keys) if not key]:
                raise ValueError(f"{self.category} [{language}]: {empty} has no letters or digits to compare")
            if repeated := sorted({word for word, key in zip(words, keys) if keys.count(key) > 1}):
                raise ValueError(f"{self.category} [{language}]: {repeated} repeat one word")
        return self


class Vocabulary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    brand: str
    scope: Scope
    languages_by_market: dict[Market, tuple[Language, ...]] = {}
    categories: tuple[Category, ...] = ()
    # market -> the compared words of every category in its languages, built once at load
    _words_by_market: dict[str, tuple[str, ...]] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _a_whole_declaration(self) -> "Vocabulary":
        if self.scope is Scope.all:
            if self.languages_by_market or self.categories:
                raise ValueError("scope all keeps every listing: it declares no languages_by_market and no categories")
            return self
        if not self.languages_by_market or not self.categories:
            raise ValueError("scope words needs languages_by_market and categories")
        if empty := sorted(market for market, languages in self.languages_by_market.items() if not languages):
            raise ValueError(f"markets {empty} name no language")
        names = [category.category for category in self.categories]
        if repeated := sorted({name for name in names if names.count(name) > 1}):
            raise ValueError(f"categories {repeated} are declared twice")
        languages = {language for named in self.languages_by_market.values() for language in named}
        for category in self.categories:
            if missing := sorted(languages - set(category.words)):
                raise ValueError(f"{category.category} names no words for {missing} (an empty list states a gap)")
            if unused := sorted(set(category.words) - languages):
                raise ValueError(f"{category.category} has words for {unused}, a language no market uses")
        # built only once the declaration is whole (every category names every language)
        self._words_by_market = {
            market: tuple(sorted({match_key(word) for category in self.categories for language in named
                                  for word in category.words[language]}))
            for market, named in self.languages_by_market.items()}
        return self

    def covers(self, market: str) -> bool:
        """Whether in_scope can answer for `market`: every market under scope all, a declared one under scope words."""
        return self.scope is Scope.all or market in self._words_by_market

    def in_scope(self, listing_text: str, market: str) -> bool:
        """Whether the run keeps this listing: always under scope all; under scope words, when the listing's text
        holds a word of a category in `market`'s languages. A market the vocabulary does not declare raises."""
        if self.scope is Scope.all:
            return True
        words = self._words_by_market.get(market)
        if words is None:
            raise VocabularyError(f"market {market!r} is not in {self.brand}'s scope vocabulary "
                                  f"(declared: {sorted(self.languages_by_market)})")
        text = match_key(listing_text or "")
        return any(word in text for word in words)


def load_vocabulary(run_brand: str, vocabulary_dir: Path = config.VOCABULARY_DIR) -> Vocabulary:
    """The validated vocabulary of the run's brand. Any problem raises VocabularyError."""
    return load_brand_document(vocabulary_dir, run_brand, Vocabulary, VocabularyError, "scope vocabulary")
