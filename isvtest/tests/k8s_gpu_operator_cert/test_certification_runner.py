# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""One self-certification test set run as a Job against a fake kubectl."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from isvtest.core import k8s
from isvtest.validations.k8s_gpu_operator_cert import runner

pytestmark = pytest.mark.unit

_TS1000 = {"id": "TS1000", "status": "passed", "tests": [{"name": "operand-verification", "status": "passed"}]}


class _FakeKubectl:
    """Answer the runner's kubectl calls the way a cluster running the Job would."""

    def __init__(self) -> None:
        """Start as a cluster where the Job completes and stores TS1000's results."""
        self.apply_rc = 0
        self.job_status: str | None = "Complete"
        self.results = {"results.json": json.dumps({"testSets": [_TS1000]}), "report.html": "<html/>"}
        self.logs = "=== TestSet [TS1000]: Default Settings ===\nerror: creating k8s clients: no config"
        self.logs_rc = 0
        self.delete_rcs: list[int] = []
        self.namespace_rc = 0
        self.existing: list[dict] = []
        self.calls: list[list[str]] = []
        self.applied = ""

    def __call__(self, args: list[str], *, stdin: str | None = None, **_: object) -> subprocess.CompletedProcess[str]:
        """Record the kubectl call and answer it from the configured cluster state."""
        self.calls.append(args)
        if args[:2] == ["get", "namespace"]:
            return _done(self.namespace_rc, stderr='Error from server (NotFound): namespaces "gpu-ns" not found')
        if args[:2] == ["get", "-f"]:
            return _done(0, json.dumps({"kind": "List", "items": self.existing}) if self.existing else "")
        if args[0] == "apply":
            self.applied = stdin or ""
            return _done(self.apply_rc, stderr="" if self.apply_rc == 0 else "error: forbidden")
        if args[:2] == ["get", "job"]:
            conditions = [{"type": self.job_status, "status": "True"}] if self.job_status else []
            return _done(0, json.dumps({"status": {"conditions": conditions}}))
        if args[0] == "logs":
            if self.logs_rc:
                return _done(self.logs_rc, stderr="error: pods for job/gpu-operator-certification not found")
            return _done(0, self.logs)
        if args[0] == "delete":
            rc = self.delete_rcs.pop(0) if self.delete_rcs else 0
            return _done(rc, stderr="error: timed out after 300s" if rc else "")
        if args[:2] == ["get", "configmap"]:
            if not self.results:
                return _done(1, stderr='Error from server (NotFound): configmaps "results" not found')
            return _done(0, json.dumps({"data": self.results}))
        return _done(0)


def _done(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    """Build a finished kubectl result."""
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


@pytest.fixture
def kubectl(monkeypatch: pytest.MonkeyPatch) -> _FakeKubectl:
    """Route the runner's and the shared helpers' kubectl calls to a fake."""
    fake = _FakeKubectl()
    monkeypatch.setattr(runner, "run_kubectl", fake)
    monkeypatch.setattr(k8s, "run_kubectl", fake)
    return fake


def _run(tmp_path: Path, test_set: str = "TS1000", timeout: int = 60) -> dict:
    """Run one test set against the fake, keeping evidence under ``tmp_path``."""
    return runner.run_test_set(
        test_set,
        namespace="gpu-ns",
        image="ghcr.io/nvidia/gpu-operator:main-latest",
        env={"TARGET_DRIVER_VERSION": "580.126.16"},
        timeout=timeout,
        artifact_dir=tmp_path,
    )


def test_manifest_is_bound_to_namespace_image_and_test_set(tmp_path: Path, kubectl: _FakeKubectl) -> None:
    """The applied manifest carries the namespace, image, test set, and inputs."""
    _run(tmp_path, test_set="TS1003")

    docs = {doc["kind"]: doc for doc in yaml.safe_load_all(kubectl.applied)}
    for kind in ("ServiceAccount", "ConfigMap", "Job"):
        assert docs[kind]["metadata"]["namespace"] == "gpu-ns"
    assert docs["ClusterRoleBinding"]["subjects"][0]["namespace"] == "gpu-ns"
    assert docs["Job"]["spec"]["template"]["spec"]["containers"][0]["image"] == (
        "ghcr.io/nvidia/gpu-operator:main-latest"
    )
    data = docs["ConfigMap"]["data"]
    assert data["TEST_SETS"] == "TS1003"
    assert data["TARGET_DRIVER_VERSION"] == "580.126.16"
    assert data["TEST_TIMEOUT"] == "15m"


def test_returns_the_test_set_and_keeps_evidence(tmp_path: Path, kubectl: _FakeKubectl) -> None:
    """The test set's result is returned and the log, results, and report are kept."""
    output = _run(tmp_path)

    assert output["result"] == _TS1000
    evidence = tmp_path / "TS1000"
    assert json.loads((evidence / "results.json").read_text())["testSets"] == [_TS1000]
    assert (evidence / "report.html").read_text() == "<html/>"
    assert "Default Settings" in (evidence / "job.log").read_text()


def test_cleans_up_before_and_after_the_run(tmp_path: Path, kubectl: _FakeKubectl) -> None:
    """The objects and the results ConfigMap are deleted around the apply."""
    _run(tmp_path)

    verbs = [call[0] for call in kubectl.calls]
    assert verbs[:5] == ["get", "get", "delete", "delete", "apply"]
    assert verbs[-2:] == ["delete", "delete"]
    deleted_configmaps = [call for call in kubectl.calls if call[:2] == ["delete", "configmap"]]
    assert len(deleted_configmaps) == 2
    assert all(runner._RESULTS_CONFIGMAP in call for call in deleted_configmaps)


def test_missing_namespace_touches_nothing(tmp_path: Path, kubectl: _FakeKubectl) -> None:
    """A missing namespace stops the run with a hint before anything is changed."""
    kubectl.namespace_rc = 1

    output = _run(tmp_path)

    assert output["error"].startswith("GPU Operator namespace 'gpu-ns' is not available (Error from server")
    assert "K8S_GPU_OPERATOR_NAMESPACE" in output["error"]
    assert [call[0] for call in kubectl.calls] == ["get"]


def test_helm_owned_objects_are_left_alone(tmp_path: Path, kubectl: _FakeKubectl) -> None:
    """Objects of a Helm release stop the run with the uninstall command."""
    kubectl.existing = [
        {"kind": "ClusterRole", "metadata": {"name": "gpu-operator-certification"}},
        {
            "kind": "Job",
            "metadata": {
                "name": "gpu-operator-certification",
                "annotations": {
                    "meta.helm.sh/release-name": "self-cert",
                    "meta.helm.sh/release-namespace": "gpu-operator",
                },
            },
        },
    ]

    output = _run(tmp_path)

    assert "Helm release 'self-cert' owns the existing certification objects" in output["error"]
    assert output["error"].endswith("helm -n gpu-operator uninstall self-cert")
    assert [call[0] for call in kubectl.calls] == ["get", "get"]


def test_stale_evidence_never_stands_in(tmp_path: Path, kubectl: _FakeKubectl) -> None:
    """Evidence from an earlier run is removed even when this run fails early."""
    stale = tmp_path / "TS1000" / "results.json"
    stale.parent.mkdir()
    stale.write_text("{}")
    kubectl.apply_rc = 1

    output = _run(tmp_path)

    assert output["error"] == "failed to apply the certification manifest: error: forbidden"
    assert not stale.exists()


def test_failed_job_reports_its_last_log_line(tmp_path: Path, kubectl: _FakeKubectl) -> None:
    """A failed Job reports the last line of its log and keeps the log."""
    kubectl.job_status = "Failed"

    output = _run(tmp_path)

    assert output["error"] == "certification Job failed: error: creating k8s clients: no config"
    assert (tmp_path / "TS1000" / "job.log").exists()


def test_failed_job_with_unreadable_log_says_both(tmp_path: Path, kubectl: _FakeKubectl) -> None:
    """A log retrieval error must not pass for the Job's own failure reason."""
    kubectl.job_status = "Failed"
    kubectl.logs_rc = 1

    output = _run(tmp_path)

    assert output["error"] == (
        "certification Job failed; its log could not be read: error: pods for job/gpu-operator-certification not found"
    )


def test_failed_cleanup_before_the_run_stops_it(tmp_path: Path, kubectl: _FakeKubectl) -> None:
    """A results ConfigMap that cannot be removed would be read back as this run's results."""
    kubectl.delete_rcs = [0, 1]

    output = _run(tmp_path)

    assert output["error"] == (
        "failed to remove leftover certification objects: kubectl delete configmap: error: timed out after 300s"
    )
    assert "apply" not in [call[0] for call in kubectl.calls]


def test_failed_cleanup_after_the_run_fails_it(tmp_path: Path, kubectl: _FakeKubectl) -> None:
    """Privileged RBAC left behind fails the check; both deletions are still attempted."""
    kubectl.delete_rcs = [0, 0, 1, 0]

    output = _run(tmp_path)

    assert output == {
        "error": "failed to remove the certification objects after the run: "
        "kubectl delete -f: error: timed out after 300s"
    }
    assert [call[0] for call in kubectl.calls][-2:] == ["delete", "delete"]


def test_failed_cleanup_keeps_the_run_error(tmp_path: Path, kubectl: _FakeKubectl) -> None:
    """A cleanup failure is added to the run's own error, not swapped for it."""
    kubectl.job_status = "Failed"
    kubectl.delete_rcs = [0, 0, 0, 1]

    output = _run(tmp_path)

    assert output["error"] == (
        "certification Job failed: error: creating k8s clients: no config; "
        "failed to remove the certification objects after the run: "
        "kubectl delete configmap: error: timed out after 300s"
    )


def test_unfinished_job_is_stopped(tmp_path: Path, kubectl: _FakeKubectl) -> None:
    """A Job still running at the timeout is removed, with a restore warning."""
    kubectl.job_status = None

    output = _run(tmp_path, timeout=0)

    assert "did not finish within 0s" in output["error"]
    assert "may not have been restored" in output["error"]
    assert kubectl.calls[-1][:2] == ["delete", "configmap"]


def test_missing_results_configmap_fails(tmp_path: Path, kubectl: _FakeKubectl) -> None:
    """A completed Job without a results ConfigMap fails."""
    kubectl.results = {}

    output = _run(tmp_path)

    assert output["error"].startswith("certification Job stored no results: Error from server (NotFound)")


@pytest.mark.parametrize(
    "results_json",
    [json.dumps({"testSets": []}), json.dumps({"testSets": [{**_TS1000, "id": "TS1001"}]}), json.dumps([])],
)
def test_results_without_this_test_set_fail(tmp_path: Path, kubectl: _FakeKubectl, results_json: str) -> None:
    """Capturing the original settings failed, or the binary ran something else."""
    kubectl.results = {"results.json": results_json}

    output = _run(tmp_path)

    assert output["error"].startswith("results.json has no TS1000 result; see ")


def test_malformed_results_json_fails(tmp_path: Path, kubectl: _FakeKubectl) -> None:
    """A results.json that is not valid JSON fails."""
    kubectl.results = {"results.json": "{broken"}

    assert _run(tmp_path)["error"].startswith("results.json is not valid JSON")
