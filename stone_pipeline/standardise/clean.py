"""A photo cleaned by a hosted edit model (straps, reflections and marks off, the stone kept), through FAL's queue: submitted
(the request is the caller's to store, the edit being paid from then on), asked until done, the result fetched. Every
failure raises."""
import time

import httpx

from stone_pipeline.config.standardise_settings import STANDARDISE as C
from stone_pipeline.standardise.judge import data_uri, fal_key

MODELS = {name: (endpoint, extra) for name, endpoint, extra in C.CLEAN_MODELS}


class NoImage(RuntimeError):
    """The queue finished the request without an image: the model failed this photo for good."""


def request(name, encoded):
    """The model's input for this photo."""
    _, extra = MODELS[name]
    image = data_uri(encoded, C.CLEAN_SEND_LONG, C.JPEG_QUALITY)
    return {"prompt": C.CLEAN_INSTRUCTION, **({"image_url": image} if name in C.CLEAN_SINGLE_IMAGE else {"image_urls": [image]}), **extra}


def prepare(name, encoded):
    """Everything sending the photo to model `name` needs, made before anything is paid: the model's endpoint, its input
    (the photo encoded) and the queue's headers (the key). A photo that cannot be encoded, an unknown model or a missing
    key fails here: loud, and free."""
    endpoint, _ = MODELS[name]
    return {"endpoint": endpoint, "body": request(name, encoded), "headers": _head()}


def submit(prepared):
    """Send a prepared edit (prepare); the queued request ({"endpoint", "status_url", "response_url"}), to store at once:
    from here on the edit is paid, and a re-run collects it instead of sending it again."""
    sub = httpx.post(f"{C.CLEAN_QUEUE_URL}/{prepared['endpoint']}", json=prepared["body"], headers=prepared["headers"],
                     timeout=C.CLEAN_TIMEOUT)
    sub.raise_for_status()
    job = sub.json()
    return {"endpoint": prepared["endpoint"], "status_url": job["status_url"], "response_url": job["response_url"]}


def collect(job):
    """The cleaned photo's bytes of a stored request, once the queue has finished it."""
    deadline = time.monotonic() + C.CLEAN_TIMEOUT
    while True:
        status = httpx.get(job["status_url"], headers=_head(), timeout=C.CLEAN_COLLECT_TIMEOUT)
        status.raise_for_status()
        if status.json()["status"] == "COMPLETED":
            break
        if time.monotonic() > deadline:
            raise TimeoutError(f"{job['endpoint']}: not done after {C.CLEAN_TIMEOUT}s")
        time.sleep(C.CLEAN_POLL)
    res = httpx.get(job["response_url"], headers=_head(), timeout=C.CLEAN_COLLECT_TIMEOUT)
    res.raise_for_status()
    images = res.json().get("images") or []
    if not images or not images[0].get("url"):
        raise NoImage(f"{job['endpoint']}: finished without an image")
    img = httpx.get(images[0]["url"], timeout=C.CLEAN_TIMEOUT)
    img.raise_for_status()
    return img.content


def _head():
    return {"Authorization": f"Key {fal_key()}"}
