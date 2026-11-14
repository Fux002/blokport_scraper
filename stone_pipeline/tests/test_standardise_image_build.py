"""The standardise GPU image is built on CodeBuild from a dispatch with a `sha` input. The sha is checked at both
ends before anything uses it: in the workflow, where the input reaches the check only through env, and again in
the buildspec, before any git command, since StartBuild can be called by anything holding the workflow's role. These
tests run both checks as written; nothing here reaches AWS or GitHub."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "standardise-image.yml"
BUILDSPEC = REPO / "infra" / "standardise_image" / "buildspec.yml"
GOOD = "98648b3e4c1f2a7d0b5e6f8a9c0d1e2f3a4b5c6d"
BAD = ["", GOOD[:39], GOOD + "0", GOOD.upper(), GOOD[:39] + "g", f"{GOOD[:20]};rm -rf {GOOD[:8]}", "main"]


def _job():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["build"]


def _check_step():
    return next(s for s in _job()["steps"] if s.get("name") == "Check the sha")


def _bash(script, env, cwd):
    return subprocess.run(["bash", "-c", script], env={"PATH": "/usr/bin:/bin", **env}, cwd=cwd,
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("sha", BAD)
def test_the_workflow_refuses_a_missing_or_malformed_sha(sha, tmp_path):
    done = _bash(_check_step()["run"], {"SHA": sha}, tmp_path)
    assert done.returncode != 0 and "refused" in done.stdout


def test_the_workflow_takes_a_full_lowercase_sha(tmp_path):
    assert _bash(_check_step()["run"], {"SHA": GOOD}, tmp_path).returncode == 0


def test_the_input_reaches_a_step_only_through_env():
    steps = _job()["steps"]
    assert _check_step()["env"]["SHA"] == "${{ inputs.sha }}"
    assert not any("inputs." in s.get("run", "") or "github.event" in s.get("run", "") for s in steps)


def test_the_job_runs_in_its_own_environment_with_only_an_oidc_token_and_read_access():
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    on = doc.get("on", doc.get(True))                                     # yaml 1.1 reads a bare `on` key as True
    assert set(on) == {"workflow_dispatch"} and on["workflow_dispatch"]["inputs"]["sha"]["required"] is True
    assert _job()["environment"] == "standardise-build"
    assert doc["permissions"] == {"id-token": "write", "contents": "read"}
    assert doc["concurrency"] == {"group": "standardise-image", "cancel-in-progress": False}


@pytest.mark.parametrize("sha", BAD)
def test_the_buildspec_refuses_a_malformed_sha_before_any_git_command(sha, tmp_path):
    spec = yaml.safe_load(BUILDSPEC.read_text(encoding="utf-8"))
    script = "\n".join(spec["phases"]["build"]["commands"])
    done = _bash(script, {"SOURCE_SHA": sha}, tmp_path)
    assert done.returncode != 0 and "refused" in done.stdout and not (tmp_path / "src").exists()
