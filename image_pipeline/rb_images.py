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
# -----------------------------------------------------------------------------


def make_output_name(filename):
    """Keep the name (it is already the variant Key) and force a .png extension.
    The generator wrote {Key}.png, so the background-removed file stays {Key}.png
    and matches the variant's Image URL dev/variations/{Key}.png exactly. (The old
    flow inserted '_rb_' here; that legacy rb naming has been retired.)
    """
    return Path(filename).stem + ".png"


def initialize_ben2_model():
    """Initialize the BEN2 model for high-quality background removal."""
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"🚀 Initializing BEN2 model on {device} (first run may take a moment)...")
    model = BEN_Base.from_pretrained("PramaLLC/BEN2")
    model = model.to(device).eval()
    print("✅ BEN2 model ready for high-quality background removal!\n")
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
        return

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
            foreground.save(output_path, format="PNG")
            done += 1
            print(f"✨ → {output_path.name}\n")
        except Exception as e:
            failed += 1
            print(f"❌ Error processing {filename}: {e}\n")

    print("=" * 50)
    print(f"✅ Complete: {done} processed, {skipped} skipped, {failed} failed.")


if __name__ == "__main__":
    process_images()