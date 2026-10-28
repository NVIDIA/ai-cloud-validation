# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""One self-certification test set run as a Job against a fake kubectl."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from isvtest.validations.k8s_gpu_operator_certification import runner

pytestmark = pytest.mark.unit

_TS1000 = {"id": "TS1000", "status": "passed", "tests": [{"name": "operand-verification", "status": "passed"}]}


class _FakeKubectl:
    """Answer the runner's kubectl calls the way a cluster running the Job would."""

    def __init__(self, *, apply_rc: int = 0, job_status: str | None = "Complete", results: dict | None = None):
        self.apply_rc = apply_rc
        self.job_status = job_status
        self.results = {"results.json": json.dumps({"testSets": [_TS1000]}), "report.html": "<html/>"}
        if results is not None:
            self.results = results
        self.logs = "=== TestSet [TS1000]: Default Settings ===\nerror: creating k8s clients: no config"
        self.calls: list[list[str]] = []
        self.applied = ""

    def __call__(self, args: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[0] == "apply":
            self.applied = stdin or ""
            return _done(self.apply_rc, stderr="" if self.apply_rc == 0 else "error: forbidden")
        if args[:2] == ["get", "job"]:
            conditions = [{"type": self.job_status, "status": "True"}] if self.job_status else []
            return _done(0, json.dumps({"status": {"conditions": conditions}}))
        if args[0] == "logs":
            return _done(0, self.logs)
        if args[:2] == ["get", "configmap"]:
            if not self.results:
                return _done(1, stderr='Error from server (NotFound): configmaps "results" not found')
            return _done(0, json.dumps({"data": self.results}))
        return _done(0)


def _done(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


@pytest.fixture
def kubectl(monkeypatch: pytest.MonkeyPatch) -> _FakeKubectl:
    fake = _FakeKubectl()
    monkeypatch.setattr(runner, "_kubectl", fake)
    monkeypatch.setattr(runner, "_POLL_SECONDS", 0)
    return fake


def _run(tmp_path: Path, test_set: str = "TS1000", timeout: int = 60) -> dict:
    return runner.run_test_set(
        test_set,
        namespace="gpu-ns",
        image="ghcr.io/nvidia/gpu-operator:main-latest",
        env={"TARGET_DRIVER_VERSION": "580.126.16"},
        timeout=timeout,
        artifact_dir=tmp_path,
    )


def test_manifest_is_bound_to_namespace_image_and_test_set(tmp_path: Path, kubectl: _FakeKubectl) -> None:
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
    output = _run(tmp_path)

    assert output["result"] == _TS1000
    artifacts = output["artifacts"]
    assert json.loads(Path(artifacts["results_json"]).read_text())["testSets"] == [_TS1000]
    assert Path(artifacts["report_html"]).read_text() == "<html/>"
    assert "Default Settings" in Path(artifacts["job_log"]).read_text()
    assert Path(artifacts["results_json"]).parent == tmp_path / "TS1000"


def test_cleans_up_before_and_after_the_run(tmp_path: Path, kubectl: _FakeKubectl) -> None:
    _run(tmp_path)

    verbs = [call[0] for call in kubectl.calls]
    assert verbs[:3] == ["delete", "delete", "apply"]
    assert verbs[-2:] == ["delete", "delete"]
    deleted_configmaps = [call for call in kubectl.calls if call[:2] == ["delete", "configmap"]]
    assert len(deleted_configmaps) == 2
    assert all(runner._RESULTS_CONFIGMAP in call for call in deleted_configmaps)


def test_stale_evidence_never_stands_in(tmp_path: Path, kubectl: _FakeKubectl) -> None:
    stale = tmp_path / "TS1000" / "results.json"
    stale.parent.mkdir()
    stale.write_text("{}")
    kubectl.apply_rc = 1

    output = _run(tmp_path)

    assert output["error"] == "failed to apply the certification manifest: error: forbidden"
    assert not stale.exists()


def test_failed_job_reports_its_last_log_line(tmp_path: Path, kubectl: _FakeKubectl) -> None:
    kubectl.job_status = "Failed"

    output = _run(tmp_path)

    assert output["error"] == "certification Job failed: error: creating k8s clients: no config"
    assert "job_log" in output["artifacts"]


def test_unfinished_job_is_stopped(tmp_path: Path, kubectl: _FakeKubectl) -> None:
    kubectl.job_status = None

    output = _run(tmp_path, timeout=0)

    assert "did not finish within 0s" in output["error"]
    assert "may not have been restored" in output["error"]
    assert kubectl.calls[-1][:2] == ["delete", "configmap"]


def test_missing_results_configmap_fails(tmp_path: Path, kubectl: _FakeKubectl) -> None:
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
    kubectl.results = {"results.json": "{broken"}

    assert _run(tmp_path)["error"].startswith("results.json is not valid JSON")
