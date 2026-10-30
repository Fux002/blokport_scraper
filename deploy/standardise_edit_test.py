"""Which open editing model removes straps, logos and sample cards best (RUN_MODE=standardise-edit-test, on the GPU, dev
only): reads the list at <env>/standardise-test/edit-test.json ([{"src", "sha", "task"}]) and writes each model edit under
<env>/standardise-test/edit-test/<model>/. A comparison for a person to judge: nothing here is published.
"""

from __future__ import annotations

import gc
import io
import json
import sys
import time

import boto3
import torch
from PIL import Image

from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET, S3_REGION, STANDARDISE as C
from stone_pipeline.io import imagestore


def _size(img):
    """The photo's shape at about EDIT_PIXELS, in multiples of 16 as the model needs."""
    k = (C.EDIT_PIXELS / (img.width * img.height)) ** 0.5
    return int(img.width * k) // 16 * 16, int(img.height * k) // 16 * 16


def main() -> int:
    from diffusers import Flux2KleinPipeline
    client = boto3.client("s3", region_name=S3_REGION)
    prefix = f"{ENV_SEGMENT}/{C.TEST_PREFIX}"
    items = json.loads(client.get_object(Bucket=S3_BUCKET, Key=f"{prefix}/edit-test.json")["Body"].read())
    # The machine has 16 GB of memory, and the text encoder and the image model do not fit in it together: the few fixed
    # instructions are encoded once, the encoder freed, and only then is the image model loaded.
    encoder = Flux2KleinPipeline.from_pretrained(C.EDIT_DIR, transformer=None, vae=None, torch_dtype=torch.float16, low_cpu_mem_usage=True).to("cuda")
    embeds = {task: encoder.encode_prompt(prompt=prompt, device="cuda")[0].cpu() for task, prompt in C.EDIT_PROMPTS.items()}
    del encoder
    gc.collect()
    torch.cuda.empty_cache()
    pipe = Flux2KleinPipeline.from_pretrained(C.EDIT_DIR, text_encoder=None, tokenizer=None, torch_dtype=torch.float16, low_cpu_mem_usage=True).to("cuda")
    for item in items:
        started = time.time()
        key = f"{imagestore.scraped_prefix(item['src'])}{item['sha']}.jpg"
        img = Image.open(io.BytesIO(client.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read())).convert("RGB")
        w, h = _size(img)
        out = pipe(prompt_embeds=embeds[item["task"]].to("cuda"), image=img.resize((w, h), Image.LANCZOS), width=w, height=h,
                   num_inference_steps=C.EDIT_STEPS, guidance_scale=C.EDIT_GUIDANCE,
                   generator=torch.Generator("cpu").manual_seed(C.BLEND_SEED)).images[0]
        buf = io.BytesIO()
        out.save(buf, "JPEG", quality=92)
        client.put_object(Bucket=S3_BUCKET, Key=f"{prefix}/edit-test/klein/{item['task']}-{item['src']}-{item['sha'][:12]}.jpg",
                          Body=buf.getvalue(), ContentType="image/jpeg")
        print(f"   {item['task']} {item['src']} {item['sha'][:12]} {w}x{h} {time.time() - started:.1f}s peak GPU {torch.cuda.max_memory_allocated() / 1e9:.1f} GB", flush=True)
    print("==> edit test done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
