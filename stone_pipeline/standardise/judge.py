"""The last check before a photo is published, by a hosted vision model: it sees the original and our result and answers in
JSON. An answer that does not parse is never read as a pass: the photo is simply not published. Calls go through FAL
(FAL_KEY from the environment); an HTTP failure raises, so the caller records nothing and a re-run asks again."""
import base64
import json
import os
import re

import cv2
import httpx
import numpy as np

from stone_pipeline.config.settings import STANDARDISE as C

_OBJECT = re.compile(r"\{.*\}", re.S)


def fal_key():
    key = os.environ.get("FAL_KEY", "").strip()
    if not key:
        raise RuntimeError("FAL_KEY is not set: the judge and the hosted clean-up cannot run")
    return key


def data_uri(encoded, long_side, quality):
    """An encoded image as a JPEG data URI, no longer than long_side; raises ValueError on an undecodable one."""
    img = cv2.imdecode(np.frombuffer(encoded, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("undecodable image")
    s = long_side / max(img.shape[:2])
    if s < 1:
        img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValueError("could not encode")
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()


def _object(text):
    found = _OBJECT.search(text or "")
    try:
        value = json.loads(found.group(0)) if found else None
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def parse_verdict(text):
    """{"verdict": "pass"|"fail", "problems": [...]} from the judge's answer; "unparsed" with the raw text otherwise."""
    value = _object(text)
    if not value or value.get("verdict") not in ("pass", "fail"):
        return {"verdict": "unparsed", "raw": (text or "")[:300]}
    problems = value.get("problems") if isinstance(value.get("problems"), list) else []
    return {"verdict": value["verdict"], "problems": [p for p in problems if isinstance(p, dict)]}


def parse_clean(text):
    """{"clean", "faithful", "problems", "passes"} from the clean check's answer; passes only when both are literally true."""
    value = _object(text) or {}
    clean, faithful = value.get("clean") is True, value.get("faithful") is True
    return {"clean": clean, "faithful": faithful, "problems": str(value.get("problems") or ""), "passes": clean and faithful}


def ask(system, prompt, images):
    """(the model's answer, what the call cost in USD) for these encoded images; the cost is None when the answer does not
    carry it (the caller counts the call at its reserved price, never as free)."""
    body = {"model": C.JUDGE_MODEL, "system_prompt": system, "prompt": prompt, "max_tokens": C.JUDGE_MAX_TOKENS,
            "image_urls": [data_uri(i, C.JUDGE_LONG_SIDE, C.JUDGE_JPEG_QUALITY) for i in images]}
    if C.JUDGE_REASONING:
        body["reasoning"] = True
    r = httpx.post(C.JUDGE_URL, json=body, headers={"Authorization": f"Key {fal_key()}"}, timeout=C.JUDGE_TIMEOUT)
    r.raise_for_status()
    res = r.json()
    cost = (res.get("usage") or {}).get("cost")
    return res["output"], None if cost is None else float(cost)


def judge(original, result, fmt):
    """(verdict dict, cost): is our catalog image `result` of the photo `original` fit to publish? fmt = slab | block | tile."""
    text, cost = ask(C.JUDGE_PROMPT, f"Product format: {fmt.upper()}. Image 1: original. Image 2: catalog image. Judge image 2.",
                     [original, result])
    return {**parse_verdict(text), "model": C.JUDGE_MODEL, "version": C.JUDGE_VERSION}, cost


def current(verdict):
    """Whether a stored verdict still stands: a pass always does, a fail only when made under the current bar."""
    return passed(verdict) or verdict.get("version", 1) >= C.JUDGE_VERSION


def check_clean(original, cleaned):
    """(check dict, cost): did the hosted edit take the reflections and marks off and leave the stone as it was?"""
    text, cost = ask(C.CLEAN_CHECK_PROMPT, "Image 1: original. Image 2: edited copy.", [original, cleaned])
    return {**parse_clean(text), "model": C.JUDGE_MODEL}, cost


def passed(verdict):
    return verdict.get("verdict") == "pass"


def fixable(verdict):
    """Whether every problem the judge saw is one a cleaned copy removes (a reflection, a mark, a fill)."""
    kinds = {p.get("kind") for p in verdict.get("problems", [])}
    return verdict.get("verdict") == "fail" and bool(kinds) and kinds <= set(C.CLEAN_FIXES_KINDS)
