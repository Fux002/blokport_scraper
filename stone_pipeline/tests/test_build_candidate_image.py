"""BUILD-CANDIDATE (its ECR reads use batch-get-image): the build-candidate workflow's two ECR reads run here as
written, against a fake `aws` that answers with the exact batch-get-image shapes (AWS API reference, BatchGetImage
and ImageFailure), so the decision is proved red first: an existing tag and every answer that is not a plain
ImageNotFound for that tag refuse. Also pinned: every `aws ecr` call the workflow makes maps to an action the
deploy role's policy (infra/main.tf) grants. Nothing here reaches AWS or GitHub."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "build-candidate.yml"
INFRA = REPO / "infra" / "main.tf"
SHA = "11da0088a0524214720aa5bdad7b81602a95cdf1"
DIGEST = "sha256:" + "ab" * 32
OTHER_DIGEST = "sha256:" + "cd" * 32
# The workflow's `aws ecr` subcommands and the IAM action each needs. A new call must be added here, against the policy.
ECR_CALL_ACTION = {"batch-get-image": "ecr:BatchGetImage"}

ABSENT = {"images": [], "failures": [
    {"imageId": {"imageTag": SHA}, "failureCode": "ImageNotFound", "failureReason": "Requested image not found"}]}
PRESENT = {"images": [{
    "registryId": "026090543098", "repositoryName": "blokport-scraper",
    "imageId": {"imageDigest": DIGEST, "imageTag": SHA},
    "imageManifest": "{}", "imageManifestMediaType": "application/vnd.docker.distribution.manifest.v2+json"}],
    "failures": []}
ACCESS_DENIED = ("An error occurred (AccessDeniedException) when calling the BatchGetImage operation: User: "
                 "arn:aws:sts::026090543098:assumed-role/blokport-scraper-gha-deploy/x is not authorized")


def _doc():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _step(name):
    return next(s for s in _doc()["jobs"]["build"]["steps"] if s.get("name") == name)


def _run(step_name, tmp_path, *, out, rc=0, pushed=None):
    """Run one step's script as GitHub does (bash -eo pipefail) with a fake `aws` that records its argv."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (tmp_path / "answer").write_text(out if isinstance(out, str) else json.dumps(out), encoding="utf-8")
    fake = bin_dir / "aws"
    fake.write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" > "{tmp_path}/argv"\ncat "{tmp_path}/answer"\nexit {rc}\n')
    fake.chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "SHA": SHA, "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
           **{k: str(v) for k, v in _doc()["env"].items()}}
    if pushed is not None:
        env["PUSHED"] = pushed
    done = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", _step(step_name)["run"]],
                          env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    argv = (tmp_path / "argv").read_text().split("\n") if (tmp_path / "argv").exists() else []
    return done, argv


CHECK = "Refuse a tag that already exists"
READ_BACK = "Read the digest back and print it"


def test_an_absent_tag_is_free_and_the_call_is_batch_get_image_for_that_tag(tmp_path):
    done, argv = _run(CHECK, tmp_path, out=ABSENT)
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"blokport-scraper:{SHA} is free" in done.stdout
    assert argv[:6] == ["ecr", "batch-get-image", "--repository-name", "blokport-scraper", "--image-ids", f"imageTag={SHA}"]


def test_an_existing_tag_refuses(tmp_path):
    done, _ = _run(CHECK, tmp_path, out=PRESENT)
    assert done.returncode != 0 and "already exists" in done.stdout


@pytest.mark.parametrize("code", ["InvalidImageTag", "ImageTagDoesNotMatchDigest", "ImageReferencedByManifestList",
                                  "KmsError", "UpstreamAccessDenied", "ImageInaccessible"])
def test_any_other_failure_code_refuses(code, tmp_path):
    answer = {"images": [], "failures": [{"imageId": {"imageTag": SHA}, "failureCode": code, "failureReason": "x"}]}
    done, _ = _run(CHECK, tmp_path, out=answer)
    assert done.returncode != 0 and "could not tell" in done.stdout


def test_image_not_found_for_another_tag_refuses(tmp_path):
    answer = {"images": [], "failures": [{**ABSENT["failures"][0], "imageId": {"imageTag": "core"}}]}
    done, _ = _run(CHECK, tmp_path, out=answer)
    assert done.returncode != 0 and "could not tell" in done.stdout


def test_an_answer_with_no_failure_and_no_image_refuses(tmp_path):
    done, _ = _run(CHECK, tmp_path, out={"images": [], "failures": []})
    assert done.returncode != 0 and "could not tell" in done.stdout


def test_access_denied_refuses_loud(tmp_path):
    done, _ = _run(CHECK, tmp_path, out=ACCESS_DENIED, rc=254)
    assert done.returncode != 0 and "could not check" in done.stdout and "AccessDeniedException" in done.stdout


def test_an_answer_that_does_not_parse_refuses(tmp_path):
    done, _ = _run(CHECK, tmp_path, out="<html>gateway</html>")
    assert done.returncode != 0 and "does not parse" in done.stdout


def test_the_read_back_prints_the_digest_only_when_ecr_names_the_pushed_one(tmp_path):
    done, argv = _run(READ_BACK, tmp_path, out=PRESENT, pushed=DIGEST)
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"pushed blokport-scraper:{SHA} ({DIGEST})" in done.stdout
    assert DIGEST in (tmp_path / "summary").read_text()
    assert argv[:2] == ["ecr", "batch-get-image"]


@pytest.mark.parametrize("answer,rc", [(PRESENT, 0), (ABSENT, 0), ({"images": [], "failures": []}, 0), (ACCESS_DENIED, 254)])
def test_the_read_back_refuses_anything_but_the_pushed_digest(answer, rc, tmp_path):
    done, _ = _run(READ_BACK, tmp_path, out=answer, rc=rc, pushed=OTHER_DIGEST)
    assert done.returncode != 0 and "refused" in done.stdout


def test_every_ecr_call_maps_to_an_action_the_deploy_role_grants():
    runs = "\n".join(s.get("run", "") for s in _doc()["jobs"]["build"]["steps"])
    calls = set(re.findall(r"\baws ecr ([a-z-]+)", runs))
    assert calls == set(ECR_CALL_ACTION), calls
    policy = INFRA.read_text(encoding="utf-8")
    deploy = policy[policy.index('data "aws_iam_policy_document" "deploy" {'):]
    deploy = deploy[:deploy.index('resource "aws_iam_role_policy" "deploy"')]
    granted = set(re.findall(r'"(ecr:[A-Za-z]+)"', deploy))
    assert {ECR_CALL_ACTION[c] for c in calls} <= granted, granted
    assert "ecr:DescribeImages" not in granted  # why the reads are batch-get-image (run 43155861223)
