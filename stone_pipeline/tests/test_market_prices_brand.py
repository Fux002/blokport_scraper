"""The brand separation of the competitor-price path (W1): one infrastructure, every brand's data apart. Each named rule
has its own tests. Fixtures only; nothing here reaches a site or a bucket."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from market_prices import config
from market_prices.vocabulary import Vocabulary, VocabularyError, load_vocabulary


def _write(directory: Path, name: str, document: object) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")


# --- brand_vocabulary_isolated: a run reads its own brand's vocabulary and no other ------------------------------------

def test_a_run_loads_its_own_brands_vocabulary(tmp_path):
    _write(tmp_path, "wudport", {"brand": "wudport"})
    assert load_vocabulary("wudport", tmp_path) == Vocabulary(brand="wudport")


def test_a_vocabulary_declaring_another_brand_is_refused(tmp_path):
    _write(tmp_path, "wudport", {"brand": "blokport"})
    with pytest.raises(VocabularyError, match="declares brand 'blokport'"):
        load_vocabulary("wudport", tmp_path)


def test_a_run_opens_only_its_own_brands_file(tmp_path, monkeypatch):
    _write(tmp_path, "wudport", {"brand": "wudport"})
    _write(tmp_path, "blokport", {"brand": "blokport"})
    opened: list[Path] = []
    real_read = Path.read_text

    def recording_read(self, *args, **kwargs):
        opened.append(self)
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", recording_read)
    load_vocabulary("wudport", tmp_path)
    assert opened == [tmp_path / "wudport.yaml"]


@pytest.mark.parametrize("brand, content, message", [
    ("../blokport", None, "must match"),
    ("nobody", None, "no scope vocabulary for brand 'nobody'"),
    ("broken", "brand: [", "is not valid YAML"),
    ("shape", "brand: shape\nunknown_field: 1\n", "does not validate"),
])
def test_a_vocabulary_problem_raises(tmp_path, brand, content, message):
    if content is not None:
        (tmp_path / f"{brand}.yaml").write_text(content, encoding="utf-8")
    with pytest.raises(VocabularyError, match=message):
        load_vocabulary(brand, tmp_path)


def test_the_vocabulary_folder_is_the_packages_own():
    assert config.VOCABULARY_DIR == Path(config.__file__).resolve().parent / "vocabulary"
