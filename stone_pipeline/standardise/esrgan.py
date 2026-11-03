"""Real-ESRGAN, the standardiser's upscaler (moved here from the retired enhancer's io/image_processing)."""

from __future__ import annotations

from typing import Optional

import cv2
import numpy as np

from stone_pipeline.core import logfmt

log = logfmt.get_logger("standardise.esrgan")


class ESRGAN:
    """Real-ESRGAN learned super-resolution (clean + sharpen + 4x), loaded via spandrel: the standardiser's upscaler
    (layout.upscale). Lazy: available() loads it once and says whether it loaded; the loader (models._esrgan) fails loud
    when it did not. Runs on CUDA (deploy), MPS (local Mac), or CPU. Large images are tiled to bound GPU memory."""

    def __init__(self, model_name: str, weights_path: str, tile: int):
        self.model_name = model_name
        self.weights_path = weights_path
        self.tile = tile
        self._ok: Optional[bool] = None
        self._model = None
        self._device = None
        self._scale = 4

    def _resolve_weights(self) -> str:
        if self.weights_path:
            return self.weights_path
        from stone_pipeline.config.settings import REPO_ROOT
        return str(REPO_ROOT.parent / "models" / f"{self.model_name}.pth")

    def available(self) -> bool:
        if self._ok is not None:
            return self._ok
        try:
            import torch
            from spandrel import ImageModelDescriptor, ModelLoader

            self._device = ("cuda" if torch.cuda.is_available()
                            else "mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()
                            else "cpu")
            model = ModelLoader().load_from_file(self._resolve_weights())
            if not isinstance(model, ImageModelDescriptor):
                raise RuntimeError("loaded file is not a single-image upscaling model")
            self._model = model.to(self._device).eval()
            self._scale = model.scale
            self._ok = True
        except Exception as exc:  # deps/weights absent: reported here, the loader fails loud on it
            log.warning("ESRGAN unavailable", extra={"extra_fields": {"error": str(exc)}})
            self._ok = False
        return self._ok

    def _infer_tiled(self, t):
        import torch

        s = self._scale
        _, c, h, w = t.shape
        if self.tile <= 0 or (h <= self.tile and w <= self.tile):
            return self._model(t)
        ov, out_s = 32, s
        step = self.tile - ov
        out = torch.zeros(1, c, h * out_s, w * out_s, device=self._device)
        wt = torch.zeros(1, 1, h * out_s, w * out_s, device=self._device)
        for y in range(0, h, step):
            for x in range(0, w, step):
                y2, x2 = min(y + self.tile, h), min(x + self.tile, w)
                y1, x1 = max(y2 - self.tile, 0), max(x2 - self.tile, 0)
                o = self._model(t[:, :, y1:y2, x1:x2])
                out[:, :, y1 * out_s:y2 * out_s, x1 * out_s:x2 * out_s] += o
                wt[:, :, y1 * out_s:y2 * out_s, x1 * out_s:x2 * out_s] += 1
        return out / wt.clamp(min=1)

    def enhance(self, bgr):
        """BGR -> BGR, cleaned + sharpened + upscaled by the learned model."""
        import torch

        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        t = torch.from_numpy(rgb).permute(2, 0, 1).unsqueeze(0).to(self._device)
        with torch.no_grad():
            out = self._infer_tiled(t)
        arr = out.squeeze(0).clamp(0, 1).permute(1, 2, 0).cpu().numpy()
        return cv2.cvtColor((arr * 255.0 + 0.5).astype(np.uint8), cv2.COLOR_RGB2BGR)
