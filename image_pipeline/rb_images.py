import os
from pathlib import Path
from PIL import Image
import torch
from ben2 import BEN_Base

# ------------------------------------------------------------------ CONFIG ---
INPUT_DIR = "./images"
OUTPUT_DIR = "./to_upload"     # background-removed {Key}.png, ready for S3 dev/variations/
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
SKIP_EXISTING = True          # skip images already done (resume support)
BEN2_REPO = "PramaLLC/BEN2"
# PIN the exact model revision: from_pretrained defaults to 'main', which can move and silently pull a
# different model (non-deterministic output + a surprise runtime download). The :gpu/:imageproc images bake
# THIS revision, so pinning it here makes the load hit the baked snapshot -- no runtime fetch, identical
# output for new textures AND the quality-refresh (both go through this loader).
BEN2_REVISION = "e48a20765fb421d19dcdb0bf3cc61e802ca5ec8f"
# -----------------------------------------------------------------------------


def make_output_name(filename):
    """Keep the name (it is already the variant Key) and force a .png extension.
    The generator wrote {Key}.png, so the background-removed file stays {Key}.png
    and matches the variant's Image URL dev/variations/{Key}.png exactly. (The old
    flow inserted '_rb_' here; that legacy rb naming has been retired.)
    """
    return Path(filename).stem + ".png"


def initialize_ben2_model():
    """Initialize the BEN2 model for high-quality background removal.

    Fail loud, never silently degrade: when BLOKPORT_REQUIRE_CUDA is set (the :gpu texture job, which was
    dispatched specifically for a GPU) but CUDA is absent, abort -- running BEN2 on CPU here is a multi-minute
    per-image stall that looks like a hang and burns a GPU box doing nothing. The CPU :imageproc refresh path
    does NOT set the flag, so CPU stays a valid device there. Model load hits the baked snapshot (HF offline
    in the image), so this is fast and never reaches the network."""
    cuda = torch.cuda.is_available()
    if os.environ.get("BLOKPORT_REQUIRE_CUDA") and not cuda:
        raise RuntimeError(
            "BLOKPORT_REQUIRE_CUDA is set but torch.cuda.is_available() is False: this GPU texture job has no "
            "usable CUDA device. Aborting loud rather than silently running BEN2 on CPU.")
    device = torch.device("cuda" if cuda else "cpu")
    print(f"🚀 Initializing BEN2 model on {device} (cuda_available={cuda})...", flush=True)
    model = BEN_Base.from_pretrained(BEN2_REPO, revision=BEN2_REVISION)
    model = model.to(device).eval()
    print("✅ BEN2 model ready for high-quality background removal!\n", flush=True)
    return model, device


def remove_background_ben2(model, image_path):
    """Remove background using BEN2 with refinement for best edge quality."""
    image = Image.open(image_path).convert("RGB")
    foreground = model.inference(image, refine_foreground=True)
    return foreground


def process_images():
    """Read every image in INPUT_DIR, remove its background, write to OUTPUT_DIR."""
    input_dir = Path(INPUT_DIR)
    output_dir = Path(OUTPUT_DIR)
    input_dir.mkdir(exist_ok=True)
    output_dir.mkdir(exist_ok=True)

    files = sorted(f for f in os.listdir(input_dir)
                   if Path(f).suffix.lower() in IMAGE_EXTENSIONS)

    if not files:
        print(f"❌ No images found in '{input_dir}'.")
        return 0

    print(f"📂 Input  : {input_dir.resolve()}")
    print(f"📂 Output : {output_dir.resolve()}")
    print(f"🖼  Found {len(files)} image(s) to process.\n")

    model, device = initialize_ben2_model()

    done = 0
    failed = 0
    skipped = 0

    for i, filename in enumerate(files, 1):
        input_path = input_dir / filename
        output_path = output_dir / make_output_name(filename)

        if SKIP_EXISTING and output_path.exists():
            skipped += 1
            print(f"⏭  [{i}/{len(files)}] {output_path.name} already exists, skipping")
            continue

        try:
            print(f"🎨 [{i}/{len(files)}] {filename}")
            foreground = remove_background_ben2(model, input_path)
            # write-then-rename so an interruption never leaves a truncated {Key}.png that
            # SKIP_EXISTING would then skip forever (shipping a corrupt image).
            tmp = output_path.with_suffix(".png.tmp")
            foreground.save(tmp, format="PNG")
            tmp.replace(output_path)
            done += 1
            print(f"✨ → {output_path.name}\n")
        except Exception as e:
            failed += 1
            print(f"❌ Error processing {filename}: {e}\n")

    print("=" * 50)
    print(f"✅ Complete: {done} processed, {skipped} skipped, {failed} failed.", flush=True)
    return failed


if __name__ == "__main__":
    # exit non-zero when any image failed so the caller's failure accounting is truthful (a partial BEN2
    # failure must flag those variants HELD, not look like a clean run).
    import sys
    sys.exit(1 if process_images() else 0)