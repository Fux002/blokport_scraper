"""What a scraped photo shows, read by CLIP: photos that are not one slab in view (a hangar, piles of slabs, a finished
kitchen) are skipped, so every image on the platform has the same feel (the user's rule)."""
import numpy as np
from PIL import Image

from stone_pipeline.config.settings import STANDARDISE as C


def classify(clip, rgb):
    """(the label, its probability) of the best-matching C.PHOTO_KINDS entry."""
    import torch
    model, proc, dev = clip
    labels = list(C.PHOTO_KINDS)
    inputs = proc(text=[C.PHOTO_KINDS[k] for k in labels], images=Image.fromarray(rgb), return_tensors="pt", padding=True).to(dev)
    with torch.no_grad():
        probs = model(**inputs).logits_per_image.softmax(dim=1)[0].cpu().numpy()
    best = int(np.argmax(probs))
    return labels[best], float(probs[best])


def is_skipped(kind, confidence):
    """Skipped only when it is surely a skip kind: an uncertain photo goes on to the outline's own checks."""
    return kind in C.SKIP_KINDS and confidence >= C.SKIP_MIN_CONFIDENCE
