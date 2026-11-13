"""The texture generator saves a Phase-1 icon into its Key's own folder: `images/{Key}/{category}.png`, the folder made by
save_image itself. Dev 2026-11-12 (job 8ea6ac51): the :gpu image built before SC #619 had no such mkdir, so every save of
a Phase-1 icon failed with "No such file or directory" and no icon landed. Hermetic: no FAL call, no network; the image
bytes are made here. No em dashes.
"""

from __future__ import annotations

import importlib
import sys
import types
from io import BytesIO

from PIL import Image

KEY = "quartzite__brazilian_white_b2e0a536-7b53-5d1d-8f55-42ac11bf18d9"


def _generator(monkeypatch, tmp_path):
    for name in ("fal_client", "requests"):                 # the GPU image's deps, not CI's: stubbed for the import
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.chdir(tmp_path)                             # the module makes ./images at import
    monkeypatch.delitem(sys.modules, "image_pipeline.genetate_images", raising=False)
    return importlib.import_module("image_pipeline.genetate_images")


def test_a_phase_1_icon_is_saved_into_its_key_s_own_new_folder(monkeypatch, tmp_path):
    gen = _generator(monkeypatch, tmp_path)
    png = BytesIO()
    Image.new("RGB", (1024, 1024), (200, 200, 190)).save(png, format="PNG")
    out = tmp_path / "images" / KEY / f"slab.{gen.OUTPUT_FORMAT}"
    assert not out.parent.exists()
    gen.save_image(png.getvalue(), out)
    assert out.is_file() and not out.with_suffix(out.suffix + ".tmp").exists()
    with Image.open(out) as saved:
        assert saved.format == "PNG" and saved.size == (gen.FINAL_SIZE, gen.FINAL_SIZE)
