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
import shutil
import subprocess
from pathlib import Path
from typing import Any

from isvtest.core.k8s import (
    KubectlParseError,
    command_detail,
    parse_kubectl_json,
    render_k8s_manifest,
    run_kubectl,
    wait_for_job_completion,
)

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
    carries either ``result``, the test set's entry from ``results.json``, or
    ``error`` when the run produced none; the Job log, ``results.json``, and
    ``report.html`` are kept under ``artifact_dir/<test_set>``. Every object the
    manifest creates, and the results ConfigMap, is deleted before and after
    the run, unless the namespace is missing or a Helm release owns them.
    """
    artifacts_dir = Path(artifact_dir).expanduser().resolve() / test_set
    shutil.rmtree(artifacts_dir, ignore_errors=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    manifest = _render(test_set, namespace=namespace, image=image, env=env)
    if error := _preflight(manifest, namespace):
        return {"error": error}
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
        kind = doc.get("kind")
        if kind not in ("ClusterRole", "ClusterRoleBinding"):
            doc.setdefault("metadata", {})["namespace"] = namespace
        if kind == "ClusterRoleBinding":
            for subject in doc.get("subjects") or []:
                subject["namespace"] = namespace
        elif kind == "ConfigMap":
            doc["data"] = {**(doc.get("data") or {}), **env, "TEST_SETS": test_set}
        elif kind == "Job":
            doc["spec"]["template"]["spec"]["containers"][0]["image"] = image
        return doc

    return render_k8s_manifest(_MANIFEST, mutate)


def _preflight(manifest: str, namespace: str) -> str | None:
    """Return why the manifest must not be applied, or ``None`` when it can be."""
    found = _kubectl(["get", "namespace", namespace])
    if found.returncode != 0:
        return (
            f"GPU Operator namespace {namespace!r} is not available ({_last_line(found)}); "
            "set K8S_GPU_OPERATOR_NAMESPACE to the namespace the GPU Operator runs in"
        )
    existing = _kubectl(["get", "-f", "-", "--ignore-not-found", "-o", "json"], stdin=manifest)
    if existing.returncode != 0:
        return f"failed to look up existing certification objects: {_last_line(existing)}"
    try:
        # Empty output means nothing exists; invalid output must fail closed.
        listed = parse_kubectl_json(existing, "existing certification objects") if existing.stdout.strip() else {}
    except KubectlParseError as exc:
        return str(exc)
    items = (listed.get("items") or []) if listed.get("kind") == "List" else [listed]
    for item in items:
        annotations = (item.get("metadata") or {}).get("annotations") or {}
        if release := annotations.get("meta.helm.sh/release-name"):
            release_namespace = annotations.get("meta.helm.sh/release-namespace") or namespace
            return (
                f"Helm release {release!r} owns the existing certification objects, which this check would "
                f"replace and delete; remove it first: helm -n {release_namespace} uninstall {release}"
            )
    return None


def _run(test_set: str, manifest: str, namespace: str, timeout: int, artifacts_dir: Path) -> dict[str, Any]:
    """Apply the manifest, wait for the Job, and read back its results."""
    applied = _kubectl(["apply", "-f", "-"], stdin=manifest)
    if applied.returncode != 0:
        return {"error": f"failed to apply the certification manifest: {_last_line(applied)}"}

    _logger.info("Waiting up to %ss for GPU Operator certification %s", timeout, test_set)
    finished, status = wait_for_job_completion(_JOB_NAME, namespace, timeout, poll_interval=_POLL_SECONDS)
    logs = _kubectl(["logs", f"job/{_JOB_NAME}", "-n", namespace])
    job_log = artifacts_dir / "job.log"
    if logs.stdout:
        job_log.write_text(logs.stdout, encoding="utf-8")
    if not finished:
        return {
            "error": f"certification Job did not finish within {timeout}s and was stopped; "
            "the GPU Operator settings may not have been restored"
        }
    if status == "Failed":
        return {"error": f"certification Job failed: {_last_line(logs)}"}

    stored = _kubectl(["get", "configmap", _RESULTS_CONFIGMAP, "-n", namespace, "-o", "json"])
    if stored.returncode != 0:
        return {"error": f"certification Job stored no results: {_last_line(stored)}"}
    try:
        data = parse_kubectl_json(stored, "results ConfigMap").get("data") or {}
    except KubectlParseError as exc:
        return {"error": str(exc)}
    for key in ("results.json", "report.html"):
        if data.get(key):
            (artifacts_dir / key).write_text(data[key], encoding="utf-8")

    try:
        suite = json.loads(data.get("results.json") or "")
    except json.JSONDecodeError as exc:
        return {"error": f"results.json is not valid JSON: {exc}"}
    test_sets = suite.get("testSets") if isinstance(suite, dict) else None
    result = next((ts for ts in test_sets or [] if isinstance(ts, dict) and ts.get("id") == test_set), None)
    if result is None:
        log = job_log if job_log.exists() else "the Job log"
        return {"error": f"results.json has no {test_set} result; see {log}"}
    return {"result": result}


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
    """Run kubectl with the runner's timeout; a timeout reports exit code 124."""
    return run_kubectl(args, timeout=_KUBECTL_TIMEOUT, stdin=stdin)


def _last_line(result: subprocess.CompletedProcess[str]) -> str:
    """Return the last line of kubectl's most informative output."""
    return command_detail(result).splitlines()[-1]
