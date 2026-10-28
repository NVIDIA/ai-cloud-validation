# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run one GPU Operator self-certification test set as a Kubernetes Job.

The ``gpu-operator-certification`` binary ships in the GPU Operator image. It
runs the test sets named in ``TEST_SETS`` against the current cluster, restores
the original GPU Operator settings after each one, and stores ``results.json``
and ``report.html`` in a results ConfigMap. Its exit code does not reflect the
test outcomes, so only ``results.json`` decides them.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from isvtest.core.k8s import get_kubectl_command, job_terminal_status, render_k8s_manifest

DEFAULT_ARTIFACT_DIR = Path("_output") / "gpu-operator-certification"
_MANIFEST = Path(__file__).resolve().parents[1] / "manifests" / "k8s" / "gpu_operator_certification.yaml"
_JOB_NAME = "gpu-operator-certification"
_RESULTS_CONFIGMAP = "gpu-operator-certification-results"
_POLL_SECONDS = 10
_KUBECTL_TIMEOUT = 300

_logger = logging.getLogger(__name__)


def run_test_set(
    test_set: str,
    *,
    namespace: str,
    image: str,
    env: dict[str, str],
    timeout: int,
    artifact_dir: str | Path = DEFAULT_ARTIFACT_DIR,
) -> dict[str, Any]:
    """Run ``test_set`` alone in the certification Job and return its result.

    ``env`` overrides keys of the manifest's config ConfigMap. The result
    carries ``artifacts`` and either ``result``, the test set's entry from
    ``results.json``, or ``error`` when the run produced none. Every object the
    manifest creates, and the results ConfigMap, is deleted before and after
    the run.
    """
    artifacts_dir = Path(artifact_dir).expanduser().resolve() / test_set
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    for stale in ("results.json", "report.html", "job.log"):
        (artifacts_dir / stale).unlink(missing_ok=True)
    manifest = _render(test_set, namespace=namespace, image=image, env=env)
    # The binary cannot update an existing results ConfigMap, so a leftover one
    # would be read back as this run's results.
    _delete(manifest, namespace)
    try:
        return _run(test_set, manifest, namespace, timeout, artifacts_dir)
    finally:
        _delete(manifest, namespace)


def _render(test_set: str, *, namespace: str, image: str, env: dict[str, str]) -> str:
    """Return the manifest bound to this namespace, image, and test set."""

    def mutate(doc: dict[str, Any]) -> dict[str, Any]:
        metadata = doc.get("metadata") or {}
        if "namespace" in metadata:
            metadata["namespace"] = namespace
        kind = doc.get("kind")
        if kind == "ClusterRoleBinding":
            for subject in doc.get("subjects") or []:
                subject["namespace"] = namespace
        elif kind == "ConfigMap":
            doc["data"] = {**(doc.get("data") or {}), **env, "TEST_SETS": test_set}
        elif kind == "Job":
            doc["spec"]["template"]["spec"]["containers"][0]["image"] = image
        return doc

    return render_k8s_manifest(_MANIFEST, mutate)


def _run(test_set: str, manifest: str, namespace: str, timeout: int, artifacts_dir: Path) -> dict[str, Any]:
    """Apply the manifest, wait for the Job, and read back its results."""
    artifacts: dict[str, str] = {}
    applied = _kubectl(["apply", "-f", "-"], stdin=manifest)
    if applied.returncode != 0:
        return {"artifacts": artifacts, "error": f"failed to apply the certification manifest: {_last_line(applied)}"}

    _logger.info("Waiting up to %ss for GPU Operator certification %s", timeout, test_set)
    status = _wait_for_job(namespace, timeout)
    logs = _kubectl(["logs", f"job/{_JOB_NAME}", "-n", namespace])
    if logs.stdout:
        artifacts["job_log"] = _write(artifacts_dir / "job.log", logs.stdout)
    if status is None:
        return {
            "artifacts": artifacts,
            "error": f"certification Job did not finish within {timeout}s and was stopped; "
            "the GPU Operator settings may not have been restored",
        }
    if status == "Failed":
        return {"artifacts": artifacts, "error": f"certification Job failed: {_last_line(logs)}"}

    stored = _kubectl(["get", "configmap", _RESULTS_CONFIGMAP, "-n", namespace, "-o", "json"])
    if stored.returncode != 0:
        return {"artifacts": artifacts, "error": f"certification Job stored no results: {_last_line(stored)}"}
    try:
        data = json.loads(stored.stdout).get("data") or {}
    except (json.JSONDecodeError, AttributeError):
        return {"artifacts": artifacts, "error": "results ConfigMap is not a JSON object"}
    for key, name in (("results.json", "results_json"), ("report.html", "report_html")):
        if data.get(key):
            artifacts[name] = _write(artifacts_dir / key, data[key])

    try:
        suite = json.loads(data.get("results.json") or "")
    except json.JSONDecodeError as exc:
        return {"artifacts": artifacts, "error": f"results.json is not valid JSON: {exc}"}
    test_sets = suite.get("testSets") if isinstance(suite, dict) else None
    result = next((ts for ts in test_sets or [] if isinstance(ts, dict) and ts.get("id") == test_set), None)
    if result is None:
        log = artifacts.get("job_log", "the Job log")
        return {"artifacts": artifacts, "error": f"results.json has no {test_set} result; see {log}"}
    return {"artifacts": artifacts, "result": result}


def _wait_for_job(namespace: str, timeout: int) -> str | None:
    """Return ``Complete`` or ``Failed`` once the Job finishes, or ``None`` at the deadline."""
    deadline = time.monotonic() + timeout
    while True:
        result = _kubectl(["get", "job", _JOB_NAME, "-n", namespace, "-o", "json"])
        if result.returncode == 0:
            try:
                status = job_terminal_status(json.loads(result.stdout))
            except json.JSONDecodeError:
                status = None
            if status is not None:
                return status
        if time.monotonic() >= deadline:
            return None
        time.sleep(_POLL_SECONDS)


def _delete(manifest: str, namespace: str) -> None:
    """Delete the manifest's objects, the Job's pod, and the results ConfigMap."""
    for args, stdin in (
        (["delete", "-f", "-", "--ignore-not-found", "--cascade=foreground"], manifest),
        (["delete", "configmap", _RESULTS_CONFIGMAP, "-n", namespace, "--ignore-not-found"], None),
    ):
        result = _kubectl(args, stdin=stdin)
        if result.returncode != 0:
            _logger.warning("kubectl %s failed: %s", " ".join(args[:2]), _last_line(result))


def _kubectl(args: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run kubectl and return its result, reporting a timeout as exit code 124."""
    command = [*get_kubectl_command(), *args]
    try:
        return subprocess.run(
            command, input=stdin, capture_output=True, text=True, timeout=_KUBECTL_TIMEOUT, check=False
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, 124, "", f"timed out after {_KUBECTL_TIMEOUT}s")


def _write(path: Path, text: str) -> str:
    """Write retained evidence and return its path."""
    path.write_text(text, encoding="utf-8")
    return str(path)


def _last_line(result: subprocess.CompletedProcess[str]) -> str:
    """Return the last non-empty output line, preferring stderr."""
    for output in (result.stderr, result.stdout):
        lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
        if lines:
            return lines[-1]
    return f"exit code {result.returncode}"
