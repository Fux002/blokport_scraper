"""The texture job definition's retry rules (infra/modules/gpu_enhance): only an infrastructure failure, where no
FAL was paid, is retried; any exit of the job itself (EXIT_PARTIAL, EXIT_CANNOT_RUN, a crash) is final. Rules match
in order and Batch retries when none matches, so the order and the closing catch-all are the contract."""

from __future__ import annotations

import re
from pathlib import Path

_MAIN_TF = Path(__file__).resolve().parents[2] / "infra" / "modules" / "gpu_enhance" / "main.tf"


def _texture_retry_rules() -> list[tuple[str, str, str]]:
    """(action, field, pattern) of each evaluate_on_exit block of the texture job definition, in file order."""
    tf = _MAIN_TF.read_text(encoding="utf-8")
    jobdef = tf[tf.index('resource "aws_batch_job_definition" "this"'):]
    jobdef = jobdef[:jobdef.index("\nresource ")]
    retry = jobdef[jobdef.index("retry_strategy {"):jobdef.index("timeout {")]
    rules = []
    for block in re.findall(r"evaluate_on_exit \{(.*?)\}", retry, flags=re.S):
        fields = dict(re.findall(r"(\w+)\s*=\s*\"([^\"]*)\"", block))
        action = fields.pop("action")
        [(field, pattern)] = fields.items()
        rules.append((action, field, pattern))
    return rules


def test_only_infrastructure_failures_retry_and_every_other_exit_is_final():
    assert _texture_retry_rules() == [
        ("RETRY", "on_status_reason", "Host EC2*"),
        ("RETRY", "on_status_reason", "Task failed to start*"),
        ("RETRY", "on_reason", "CannotPullContainerError*"),
        ("RETRY", "on_reason", "ResourceInitializationError*"),
        ("EXIT", "on_reason", "*"),                      # last: anything else (our exit codes, a crash) is final
    ]
