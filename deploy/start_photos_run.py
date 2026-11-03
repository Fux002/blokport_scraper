"""The overnight photos run: one press of the admin's Run for the photos stage (POST /config/v1/run {"stage": "photos"}),
made by the environment's EventBridge schedule as a one-off task (infra: the sync_service module's photos_nightly
schedule). The run is the runner's, exactly as the button starts it: the same lock, budget and work window, refused
while another run is under way, and it gives way to the owner's Publish. This only presses and reports.

Env (no flags):
  SCRAPER_CONFIG_URL    the config API's root inside the VPC (http://<service>.<namespace>:8724)
  SCRAPER_CONFIG_TOKEN  its bearer token
Exit: 0 started; 3 not started because a run is under way (named); 2 not configured; 1 any other answer, or none.
"""

from __future__ import annotations

import sys

import httpx

from stone_pipeline.core import env

TIMEOUT_SECONDS = 30       # the route answers at once (the run starts in the background)


def press(post) -> int:
    """POST the photos run through `post(url, json, headers)`; prints the outcome and returns the exit code."""
    try:
        url, token = env.require("CONFIG_URL"), env.require("CONFIG_TOKEN")
    except KeyError as exc:
        print(f"ERROR: not configured: {exc.args[0]}; no run started")
        return 2
    try:
        resp = post(f"{url.rstrip('/')}/config/v1/run", json={"stage": "photos"},
                    headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        print(f"ERROR: the config API did not answer ({type(exc).__name__}: {exc}); no run started")
        return 1
    try:
        rec = resp.json()
    except ValueError:
        rec = {}
    if resp.status_code == 202:
        print(f"==> photos run {rec.get('run_id')} started")
        return 0
    if resp.status_code == 409:
        print(f"==> not started: the {rec.get('stage')} run {rec.get('run_id')} is under way ({rec.get('status')})")
        return 3
    print(f"ERROR: the config API answered {resp.status_code}: {resp.text[:300]}; no run started")
    return 1


def main() -> int:
    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
        return press(client.post)


if __name__ == "__main__":
    sys.exit(main())
