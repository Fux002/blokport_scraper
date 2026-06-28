#!/usr/bin/env python3
"""
Batch image-to-image generator for fal.ai — FLUX.2 [klein] 4B Base (edit).

Converts a single base slab image into N stone variants defined in prompts_to_generate.json.

Key facts (verified against fal.ai docs):
  - Endpoint:   fal-ai/flux-2/klein/4b/base/edit   (image-to-image)
  - Cost:       $0.009 / megapixel of input + output
                512x512 edit = ~1 MP in + 1 MP out = ~$0.018 per image
                3000 images  = ~$54 (upper bound)
  - Param name: image_urls  (a LIST). There is NO 'strength' on this endpoint.
  - image_size: an object {"width": W, "height": H}, NOT a string.

Designed for a 3000-image run:
  - Resumable:   skips any output that already exists, so re-running continues.
  - Retries:     each image retried with exponential backoff.
  - Concurrent:  ThreadPoolExecutor — much faster than sequential.
  - Logged:      failures written to failures.jsonl for easy re-runs.

SETUP
  pip install fal-client requests pillow
  export FAL_KEY="<id>:<secret>"  (or from SSM), set MODEL (klein/turbo/dev/pro/max), then run.
"""

import os

# ============================================================================
#  FAL_KEY comes from the environment (never hardcoded — this file is committed).
#  Locally:   export FAL_KEY="<id>:<secret>"   before running.
#  On AWS:    injected from SSM SecureString /blokport-<env>/FAL_KEY by the task.
#  A previously hardcoded key was removed from source history — rotate it in the
#  fal.ai dashboard if it was ever committed.
# ============================================================================

import re
import json
import time
import threading
from io import BytesIO
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import fal_client
from PIL import Image

# ------------------------------------------------------------------ CONFIG ---

# The base image. Can be:
#   - a local file (it will be uploaded to fal once and the URL reused), or
#   - an https URL already hosted on fal.media.
# The base image. Set BASE_IMAGE to a local file to upload it once, or leave it
# as None to use a URL already hosted on fal.media (no upload needed).
BASE_IMAGE = None                     # using the hosted URL below
BASE_IMAGE_URL = "https://v3b.fal.media/files/b/0a9ef2cf/Mfbt9fWxn_4taQ9Pe-wvs_base_slab_3.jpg"

PROMPTS_FILE = os.environ.get("PROMPTS_FILE", "prompts_to_generate.json")  # override for targeted runs
OUTPUT_DIR = Path("./images")               # writes {Key}.png (output_name = variant Key)

# --- MODEL SWITCH -----------------------------------------------------------
# Pick which FLUX.2 tier to run. All share the same prompt + image_urls
# interface, so the preservation prompt / seed / batch harness work unchanged.
#
#   "klein" — smallest/cheapest, loosest preservation (what you started on)
#   "turbo" — FLUX.2 [dev] at turbo speed, cheap
#   "dev"   — FLUX.2 [dev], a real fidelity step up from klein
#   "pro"   — FLUX.2 [pro], production tier
#   "max"   — FLUX.2 [max], the flagship you used on Runway (best, priciest)
#
# approx_per_image is for a ~512/1024 edit, used only for the cost estimate.
# flux_knobs = True means the model accepts guidance_scale/steps/acceleration;
# pro and max are zero-config and reject those, so we don't send them.
MODEL = "max"   # flagship FLUX.2 [max] — best fidelity, used for all new images

MODELS = {
    "klein": {"endpoint": "fal-ai/flux-2/klein/4b/base/edit", "flux_knobs": True,  "max_steps": 50, "approx_per_image": 0.018},
    "turbo": {"endpoint": "fal-ai/flux-2/turbo/edit",         "flux_knobs": True,  "max_steps": 8,  "approx_per_image": 0.016},
    "dev":   {"endpoint": "fal-ai/flux-2/edit",               "flux_knobs": True,  "max_steps": 50, "approx_per_image": 0.024},
    "pro":   {"endpoint": "fal-ai/flux-2-pro/edit",           "flux_knobs": False, "max_steps": 0,  "approx_per_image": 0.030},
    "max":   {"endpoint": "fal-ai/flux-2-max/edit",           "flux_knobs": False, "max_steps": 0,  "approx_per_image": 0.100},
}

# Which slice of prompts_to_generate.json to process.
START_INDEX = 0
COUNT = None                            # process the whole prompts file

# --- OUTPUT SIZE ------------------------------------------------------------
# IMPORTANT: we edit at the input image's NATIVE resolution (best fidelity —
# forcing the model to a small canvas makes it recompose) and then downscale
# locally. Output below 1 MP is billed as 1 MP anyway, so native editing is
# the SAME price as 512 but cleaner. FINAL_SIZE = the square size we save at.
FINAL_SIZE = 512                       # set to None to keep native resolution
OUTPUT_FORMAT = "png"                  # REQUIRED png: the de-background step + the variant
                                       # Image URLs ({Key}.png) are png-only — do not change.

# Throughput / reliability.
MAX_WORKERS = 12                        # raise to 8-12 if your fal plan allows it
MAX_RETRIES = 4
BASE_BACKOFF = 3                       # seconds; doubles each retry
HEARTBEAT_SECONDS = 20                 # how often to print "still alive" status

# Diffusion knobs — used ONLY by klein/turbo/dev (pro & max ignore them).
NUM_INFERENCE_STEPS = 28              # lower (e.g. 20) = faster, slightly less detail
ACCELERATION = "regular"             # "none" = max quality | "regular" | "high" = fastest
ENABLE_SAFETY_CHECKER = False        # stone textures are harmless; avoids false flags

# --- PROMPT HANDLING --------------------------------------------------------
# Your prompts_to_generate.json keeps the stone surface clean (no glare/reflections/etc.),
# which we KEEP. We only drop the few phrases that fight the composition
# (flat view / no perspective / no background) — those are what flattened the
# image and turned the background white. Everything else is carried through.
# Set USE_PRESERVATION_PROMPT = False to send the raw prompt verbatim instead.
USE_PRESERVATION_PROMPT = True

# Directive phrases to remove because they destroy the composition.
# Add "no shadows" or "flat even lighting" here if the slab's grounding shadow
# or studio lighting disappears and you want them back.
DROP_PHRASES = [
    "straight-on flat view",
    "flat view",
    "no perspective",
    "no background",
]

# Per-category preservation prompt: each category's base photo has a different
# form (slabs stand on display stands; blocks/tiles do NOT), so the template must
# match or the edit hallucinates the wrong shape (e.g. a stand onto a block).
# Picked by the variant Key prefix (slab_/block_/tile_).
PROMPT_TEMPLATES = {
    "slab": (
        "Change only the stone material of this slab to {stone}. This is a "
        "texture-only edit: keep the slab in the exact same position, size and "
        "angle within the frame — do not move, rotate, rescale, crop, zoom or "
        "reframe it. Preserve the identical slab shape, the same three-quarter "
        "viewing angle, the two metal display stands, the plain white background "
        "with a soft shadow beneath the slab, "
        "and the same framing and perspective. Replace only the stone's surface "
        "texture, veining, pattern and colour, leaving all geometry unchanged. "
        "{directives}"
    ),
    "block": (
        "Change only the stone material of this stone block to {stone}. This is a "
        "texture-only edit: keep the block in the exact same position, size and "
        "angle within the frame — do not move, rotate, rescale, crop, zoom or "
        "reframe it. Preserve the identical solid rectangular block shape, the "
        "same viewing angle, the plain white background with a soft shadow beneath "
        "the block, and the same framing and perspective. Do NOT add any display "
        "stands, easels or supports — the block sits directly on the surface. "
        "Replace only the stone's surface texture, veining, pattern and colour, "
        "leaving all geometry unchanged. {directives}"
    ),
    "tile": (
        "Change only the stone material of this tile to {stone}. This is a "
        "texture-only edit: keep the tile in the exact same position, size and "
        "angle within the frame — do not move, rotate, rescale, crop, zoom or "
        "reframe it. Preserve the identical flat tile shape, the same viewing "
        "angle, the plain white background with a soft shadow beneath the tile, "
        "and the same framing and perspective. Do NOT add any display stands or "
        "supports. Replace only the stone's surface texture, veining, pattern and "
        "colour, leaving all geometry unchanged. {directives}"
    ),
}

# Some varieties are named after a fruit, a painting, a person, etc. (e.g. 'Mona Lisa',
# 'Banana', 'Picasso'). Without a guard the model renders the LITERAL subject (the
# painting) onto the slab. This pins every edit to natural stone only. It is appended
# to the {stone} value so it sits right where the name is introduced in each template.
STONE_ONLY_GUARD = (
    " — a natural stone variety (this is only the quarry trade name). Render a "
    "realistic natural stone surface only: mineral grain, crystalline veining and "
    "polished rock. Do NOT depict any object, person, face, animal, fruit, plant, "
    "painting, logo, scene or text the name might literally suggest"
)

GUIDANCE_SCALE = 5.0                  # raise to ~6-7 if it still drifts; lower if too rigid

# Fixed seed = identical framing/stands/lighting across ALL images (uniform catalog).
# Only the stone texture changes between outputs. Set SEED = None for random each time.
SEED = 42

# -----------------------------------------------------------------------------

OUTPUT_DIR.mkdir(exist_ok=True)
_print_lock = threading.Lock()
_fail_lock = threading.Lock()
FAILURES_FILE = Path("failures.jsonl")


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def record_failure(item: dict, error: str) -> None:
    with _fail_lock:
        with open(FAILURES_FILE, "a") as f:
            f.write(json.dumps({"item": item, "error": error}) + "\n")


def detect_base_size(base_url: str):
    """Read the base image's pixel dimensions, rounded down to a multiple of 32
    (required by FLUX.2 custom sizes). Used to lock pro/max output to the input."""
    try:
        if BASE_IMAGE and Path(BASE_IMAGE).is_file():
            with Image.open(BASE_IMAGE) as im:
                w, h = im.size
        else:
            data = requests.get(base_url, timeout=60).content
            with Image.open(BytesIO(data)) as im:
                w, h = im.size
        w, h = (w // 32) * 32, (h // 32) * 32
        return (max(w, 32), max(h, 32))
    except Exception as e:
        log(f"⚠️  Could not detect base size ({e}); falling back to model default.")
        return None


def build_prompt(raw_prompt: str, category: str = "slab") -> str:
    """Keep the stone name AND the surface directives, dropping only the phrases
    that break the composition. The category (slab/block/tile) picks the template.

    'Transform this attached image into Agate: Agata Black. No shadows, no light
     glare, ..., straight-on flat view, no background, no perspective, ...'
      -> stone      = 'Agate: Agata Black'
      -> directives = 'No shadows, no light glare, no reflections, no highlights,
                       flat even lighting, no text, no watermark'  (breakers removed)
    """
    if not USE_PRESERVATION_PROMPT:
        return raw_prompt

    after = raw_prompt.split("into ", 1)[-1]

    # Anchor on the directive block so stone names containing '. ' aren't truncated.
    low = after.lower()
    idx = low.find("no shadows")
    if idx == -1:
        idx = low.find("no light glare")
    if idx == -1:
        # No recognisable directive block — fall back to the first sentence.
        parts = after.split(". ", 1)
        stone = parts[0].strip().rstrip(".")
        directives = parts[1].strip() if len(parts) > 1 else ""
    else:
        stone = after[:idx].strip().rstrip(". ")
        directives = after[idx:].strip()

    stone = re.sub(r"\s+", " ", stone) + STONE_ONLY_GUARD   # keep it natural stone, not the literal name
    directives = clean_directives(directives)
    template = PROMPT_TEMPLATES.get(category, PROMPT_TEMPLATES["slab"])
    return template.format(stone=stone, directives=directives).strip()


def clean_directives(directives: str) -> str:
    """Drop composition-breaking phrases, keep the rest (glare/reflections/etc.)."""
    if not directives:
        return ""
    drop = [d.lower() for d in DROP_PHRASES]
    kept = []
    for phrase in directives.rstrip(".").split(","):
        p = phrase.strip()
        if p and not any(d in p.lower() for d in drop):
            kept.append(p)
    return (", ".join(kept) + ".") if kept else ""


def build_arguments(raw_prompt: str, base_url: str, base_size=None, category: str = "slab") -> dict:
    """Build the request payload, adapted to the selected model tier.

    - We do NOT force a small image_size: the model edits at the input's native
      resolution (best fidelity) and we downscale locally afterwards.
    - klein/turbo/dev accept diffusion knobs; pro/max are zero-config and reject
      guidance_scale/num_inference_steps/acceleration, so we omit them there.
    """
    cfg = MODELS[MODEL]
    args = {
        "prompt": build_prompt(raw_prompt, category),
        "image_urls": [base_url],
        "num_images": 1,
        "output_format": OUTPUT_FORMAT,
        "enable_safety_checker": ENABLE_SAFETY_CHECKER,
    }
    if SEED is not None:
        args["seed"] = SEED

    if cfg["flux_knobs"]:
        # klein / turbo / dev — diffusion model knobs. image_size omitted so the
        # output matches the input resolution (the schema's default behaviour).
        # Clamp steps to the model's ceiling (turbo maxes out at 8).
        args["guidance_scale"] = GUIDANCE_SCALE
        args["num_inference_steps"] = min(NUM_INFERENCE_STEPS, cfg["max_steps"])
        args["acceleration"] = ACCELERATION
    else:
        # pro / max — zero-config. Lock the output canvas to the input's EXACT
        # pixel size: instruction-edit models re-place/re-angle the subject when
        # the canvas changes, and "auto" can snap to a different size bucket.
        # Matching dimensions keeps it a pixel-aligned, texture-only edit.
        if base_size:
            args["image_size"] = {"width": base_size[0], "height": base_size[1]}
        elif MODEL == "max":
            args["image_size"] = "auto"
    return args


def save_image(content: bytes, out_path: Path) -> None:
    """Downscale to FINAL_SIZE locally (clean Lanczos) and save, or save as-is. Atomic: write a .tmp
    then os.replace, so a process killed mid-save never leaves a truncated {Key}.png that the resume
    scan (out_path.exists()) would then skip forever, shipping a corrupt image."""
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    if not FINAL_SIZE:
        tmp.write_bytes(content)
    else:
        img = Image.open(BytesIO(content))
        img = img.resize((FINAL_SIZE, FINAL_SIZE), Image.LANCZOS)
        if OUTPUT_FORMAT in ("jpeg", "jpg") and img.mode != "RGB":
            img = img.convert("RGB")
        img.save(tmp)
    os.replace(tmp, out_path)


def generate_one(item: dict, base_sizes: dict) -> bool:
    """Generate and save a single image. Returns True on success. Each item carries
    its own base_image_url (slab base vs block base), so block variants edit the
    block photo and slab variants the slab photo."""
    prompt = item.get("prompt")
    name = item.get("output_name", "stone")
    out_path = OUTPUT_DIR / f"{name}.{OUTPUT_FORMAT}"

    # Resume: skip if already done.
    if out_path.exists():
        return True

    if not prompt:
        record_failure(item, "missing prompt")
        return False

    base_url = item.get("base_image_url") or BASE_IMAGE_URL
    category = (item.get("output_name") or "").split("_", 1)[0] or "slab"
    arguments = build_arguments(prompt, base_url, base_sizes.get(base_url), category)
    endpoint = MODELS[MODEL]["endpoint"]

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = fal_client.subscribe(endpoint, arguments=arguments)
            images = result.get("images") or []
            if not images:
                raise ValueError(f"no images in response: {list(result.keys())}")

            img_url = images[0]["url"] if isinstance(images[0], dict) else images[0]
            resp = requests.get(img_url, timeout=60)
            resp.raise_for_status()
            save_image(resp.content, out_path)
            return True

        except Exception as e:
            msg = str(e)
            # Validation / bad-request errors are deterministic — retrying wastes
            # time and money. Fail immediately so you see the real problem.
            # Specific, deterministic parameter rejections only. A bare 'validation' substring was too
            # broad -- a transient gateway/proxy error whose body merely mentions 'validation' would be
            # permanently failed without a retry.
            non_retryable = (
                "less_than_equal" in msg or "greater_than_equal" in msg
                or "unprocessable" in msg.lower() or "422" in msg
            )
            if non_retryable:
                log(f"❌ {name}: parameter rejected by {MODEL} — {msg}")
                record_failure(item, msg)
                return False
            if attempt == MAX_RETRIES:
                log(f"❌ {name}: failed after {MAX_RETRIES} attempts — {e}")
                record_failure(item, msg)
                return False
            wait = BASE_BACKOFF * (2 ** (attempt - 1))
            log(f"⚠️  {name}: attempt {attempt} failed ({e}); retrying in {wait}s")
            time.sleep(wait)
    return False


def main() -> None:
    key = os.environ.get("FAL_KEY", "")
    if not key or key == "PASTE-YOUR-ROTATED-KEY-HERE":
        raise SystemExit("❌ FAL_KEY not set. Export it first: export FAL_KEY=\"<id>:<secret>\" "
                         "(on AWS it is injected from SSM /blokport-<env>/FAL_KEY).")

    # start each run with a fresh failure log, else it accumulates stale (since-resolved) entries and
    # the 're-run to retry' guidance reads old failures.
    FAILURES_FILE.unlink(missing_ok=True)

    with open(PROMPTS_FILE) as f:
        all_items = json.load(f)

    end = len(all_items) if COUNT is None else START_INDEX + COUNT
    items = all_items[START_INDEX:end]

    cfg = MODELS[MODEL]
    size_label = f"{FINAL_SIZE}x{FINAL_SIZE}" if FINAL_SIZE else "native"

    # Cost estimate (upper bound), per the selected model tier.
    est_cost = len(items) * cfg["approx_per_image"]
    log("-" * 60)
    log(f"🧠 Model           : FLUX.2 [{MODEL}]  ({cfg['endpoint']})")
    log(f"📊 Prompts in file : {len(all_items)}")
    log(f"🎯 This run        : {len(items)}  (index {START_INDEX}..{end - 1})")
    log(f"🖼  Output size      : {size_label} {OUTPUT_FORMAT}  (edited at native res)")
    log(f"💸 Est. cost        : ~${est_cost:,.2f}  (≈${cfg['approx_per_image']:.3f}/image)")
    log(f"⚙️  Workers          : {MAX_WORKERS}")
    log("-" * 60)

    # Each prompt item carries its own base_image_url (slab vs block). Detect the
    # pixel size of each distinct base once, for pro/max canvas locking.
    distinct_bases = sorted({it.get("base_image_url") or BASE_IMAGE_URL for it in items})
    base_sizes = {u: detect_base_size(u) for u in distinct_bases}
    log(f"🖼  Base images      : {len(distinct_bases)} (one per category)")
    for u, s in base_sizes.items():
        log(f"     {u.rsplit('/', 1)[-1]} -> {s}")

    # --- RESUME SCAN: skip images that already exist in OUTPUT_DIR -----------
    existing = sorted(p.name for p in OUTPUT_DIR.glob(f"*.{OUTPUT_FORMAT}"))
    todo = [it for it in items
            if not (OUTPUT_DIR / f"{it.get('output_name','stone')}.{OUTPUT_FORMAT}").exists()]
    already = len(items) - len(todo)
    log(f"📂 Output folder    : {OUTPUT_DIR.resolve()}")
    log(f"🔁 Already on disk  : {len(existing)} file(s)  →  skipping {already} of this run")
    log(f"🆕 To generate      : {len(todo)}")
    log("-" * 60)

    if not todo:
        log("✅ Nothing to do — every requested image already exists in the output "
            "folder. Delete files (or change OUTPUT_DIR) to regenerate.")
        return

    log(f"🚀 Generating {len(todo)} images with {MAX_WORKERS} workers...")
    log(f"   (Max can take 20-60s per image — a heartbeat prints every "
        f"{HEARTBEAT_SECONDS}s so you always know it's alive)\n")

    done = 0
    failed = 0
    started = time.time()
    stats = {"completed": 0, "last": time.time()}
    stats_lock = threading.Lock()

    # --- HEARTBEAT: prove the run is alive during long silent gaps -----------
    stop_beat = threading.Event()

    def heartbeat():
        while not stop_beat.wait(HEARTBEAT_SECONDS):
            with stats_lock:
                comp = stats["completed"]
                quiet = time.time() - stats["last"]
            inflight = min(MAX_WORKERS, len(todo) - comp)
            log(f"   ⏳ working… {comp}/{len(todo)} done, ~{inflight} in flight, "
                f"{quiet:.0f}s since last result")

    beat = threading.Thread(target=heartbeat, daemon=True)
    beat.start()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(generate_one, it, base_sizes): it for it in todo}
        for fut in as_completed(futures):
            it = futures[fut]
            name = it.get("output_name", "stone")
            ok = fut.result()
            if ok:
                done += 1
            else:
                failed += 1
            total = done + failed
            with stats_lock:
                stats["completed"] = total
                stats["last"] = time.time()
            elapsed = time.time() - started
            rate = total / max(elapsed, 1)
            eta = (len(todo) - total) / max(rate, 0.01)
            mark = "✅" if ok else "❌"
            log(f"{mark} [{total}/{len(todo)}] {name}   "
                f"({rate*60:.1f}/min, ETA {eta/60:.1f} min)")

    stop_beat.set()

    log("\n" + "=" * 60)
    log(f"✅ Done: {done} succeeded, {failed} failed.")
    if failed:
        log(f"   Failed items logged to {FAILURES_FILE} — re-run this script to retry them.")


if __name__ == "__main__":
    main()