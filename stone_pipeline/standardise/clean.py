"""A photo cleaned by a hosted edit model (reflections and marks off, the stone kept), through FAL's queue: submitted, asked
until done, the result fetched. Every failure raises, so the caller records nothing and a re-run tries again."""
import time

import httpx

from stone_pipeline.config.settings import STANDARDISE as C
from stone_pipeline.standardise.judge import data_uri, fal_key

MODELS = {name: (endpoint, extra) for name, endpoint, extra in C.CLEAN_MODELS}


def request(name, encoded):
    """The model's input for this photo."""
    _, extra = MODELS[name]
    image = data_uri(encoded, C.CLEAN_SEND_LONG, 92)
    return {"prompt": C.CLEAN_INSTRUCTION, **({"image_url": image} if name in C.CLEAN_SINGLE_IMAGE else {"image_urls": [image]}), **extra}


def edit(name, encoded):
    """The cleaned photo's bytes from model `name`."""
    endpoint, _ = MODELS[name]
    head = {"Authorization": f"Key {fal_key()}"}
    sub = httpx.post(f"{C.CLEAN_QUEUE_URL}/{endpoint}", json=request(name, encoded), headers=head, timeout=C.CLEAN_TIMEOUT)
    sub.raise_for_status()
    job = sub.json()
    deadline = time.monotonic() + C.CLEAN_TIMEOUT
    while True:
        status = httpx.get(job["status_url"], headers=head, timeout=60)
        status.raise_for_status()
        if status.json()["status"] == "COMPLETED":
            break
        if time.monotonic() > deadline:
            raise TimeoutError(f"{endpoint}: not done after {C.CLEAN_TIMEOUT}s")
        time.sleep(C.CLEAN_POLL)
    res = httpx.get(job["response_url"], headers=head, timeout=60)
    res.raise_for_status()
    img = httpx.get(res.json()["images"][0]["url"], timeout=C.CLEAN_TIMEOUT)
    img.raise_for_status()
    return img.content
