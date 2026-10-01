# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Verify disposable CSI install ownership, evidence and failure recovery."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from isvctl.config.output_schemas import OUTPUT_SCHEMAS
from jsonschema import validate

SCRIPT = Path(__file__).resolve().parents[2] / "isvctl/configs/providers/shared/csi_driver.py"
SPEC = importlib.util.spec_from_file_location("csi_driver", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class Cluster:
    """Model independently existing objects and API failures at the command boundary."""

    def __init__(self, method: str = "helm") -> None:
        """Create a rendered package with registration and one controller workload."""
        self.method = method
        self.docs = [
            {"apiVersion": "storage.k8s.io/v1", "kind": "CSIDriver", "metadata": {"name": "test.csi"}},
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "controller", "namespace": "probe"},
                "spec": {"replicas": 1},
                "status": {"readyReplicas": 1},
            },
        ]
        self.objects: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.commands: list[list[str]] = []
        self.pvs: list[dict[str, Any]] = []
        self.fail_ready = False
        self.uid = 0

    @staticmethod
    def key(doc: dict[str, Any]) -> tuple[str, str, str]:
        """Return an API identity independent of mutable specs."""
        return doc["kind"], doc["metadata"]["name"], doc["metadata"].get("namespace", "")

    def create(self, doc: dict[str, Any]) -> None:
        """Reject preexisting resources and assign a new server UID."""
        key = self.key(doc)
        if key in self.objects:
            raise RuntimeError("AlreadyExists")
        self.uid += 1
        obj = copy.deepcopy(doc)
        obj["metadata"]["uid"] = str(self.uid)
        self.objects[key] = obj

    def command(self, args: list[str], data: str | None = None) -> str:
        """Implement only the API interactions needed by the installer."""
        self.commands.append(args)
        if args[1] in {"template", "kustomize"}:
            return yaml.safe_dump_all(self.docs)
        if args[1:3] == ["show", "crds"]:
            return ""
        if args[1] == "api-resources":
            return "namespaces v1 false Namespace\ncsidrivers storage.k8s.io/v1 false CSIDriver\ndeployments apps/v1 true Deployment\nstorageclasses storage.k8s.io/v1 false StorageClass"
        if args[1] == "get" and "-f" in args:
            obj = self.objects.get(self.key(yaml.safe_load(data)))
            return json.dumps({"kind": "List", "items": [obj] if obj else []})
        if args[1:3] == ["get", "pv"]:
            return json.dumps({"items": self.pvs})
        if args[1] == "create":
            self.create(yaml.safe_load(data))
            return "created"
        if args[1] == "install":
            for doc in self.docs:
                doc = copy.deepcopy(doc)
                doc["metadata"]["annotations"] = {
                    "meta.helm.sh/release-name": "isvtest-csi",
                    "meta.helm.sh/release-namespace": "probe",
                }
                self.create(doc)
            return "installed"
        if args[1] == "rollout":
            if self.fail_ready:
                raise RuntimeError("Readiness timed out")
            return "ready"
        if args[1:3] == ["get", "deployment/controller"]:
            return json.dumps(self.objects[("Deployment", "controller", "probe")])
        if args[1:3] == ["get", "csidriver"]:
            return json.dumps(self.objects[("CSIDriver", args[3], "")])
        if args[1] == "list":
            return '[{"name": "isvtest-csi"}]'
        if args[1] == "uninstall":
            return "removed"
        if args[1] == "delete":
            self.objects.pop(self.key(yaml.safe_load(data)), None)
            return "deleted"
        raise AssertionError(args)


def installer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cluster: Cluster) -> Any:
    """Bind the operation script to a deterministic API without invoking cloud commands."""
    monkeypatch.setenv("KUBECTL", "kubectl")
    monkeypatch.setenv("HELM", "helm")
    instance = MODULE.CsiInstaller(tmp_path / "state.json")
    monkeypatch.setattr(instance, "command", cluster.command)
    return instance


def config(method: str) -> dict[str, Any]:
    """Return a minimally complete installation configuration."""
    return {
        "method": method,
        "namespace": "probe",
        "source": "/reviewed/package",
        "drivers": ["test.csi"],
        "workloads": ["deployment/controller"],
    }


@pytest.mark.parametrize("method", ["helm", "kustomize"])
def test_installs_checks_readiness_and_removes_owned_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """Both installation methods must create a driver and clean up their exact inventory."""
    cluster = Cluster(method)
    runner = installer(tmp_path, monkeypatch, cluster)
    evidence = runner.install(config(method))
    validate(evidence, OUTPUT_SCHEMAS["generic"])
    assert evidence["success"] and evidence["method"] == method
    assert all(check["passed"] is True for check in evidence["tests"].values())
    state = json.loads(runner.state_path.read_text())
    assert len(state["resources"]) == 3
    assert all(set(obj) == {"apiVersion", "kind", "metadata"} for obj in state["resources"])
    assert runner.state_path.stat().st_mode & 0o777 == 0o600
    runner.remove()
    assert cluster.objects == {}
    assert not runner.state_path.exists()


@pytest.mark.parametrize("method", ["helm", "kustomize"])
def test_existing_driver_is_not_adopted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    """Preflight must reject a collision before creating even a namespace."""
    cluster = Cluster(method)
    cluster.create(cluster.docs[0])
    original = copy.deepcopy(cluster.objects)
    runner = installer(tmp_path, monkeypatch, cluster)
    with pytest.raises(ValueError, match="Refusing to adopt"):
        runner.install(config(method))
    assert cluster.objects == original
    assert not runner.state_path.exists()


@pytest.mark.parametrize("method", ["helm", "kustomize"])
def test_failed_readiness_retains_cleanup_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """Partial setup must remain removable even when readiness fails."""
    cluster = Cluster(method)
    cluster.fail_ready = True
    runner = installer(tmp_path, monkeypatch, cluster)
    with pytest.raises(RuntimeError, match="Readiness"):
        runner.install(config(method))
    runner.remove()
    assert cluster.objects == {}


@pytest.mark.parametrize("fault", ["active-volume", "replaced-resource"])
def test_teardown_refuses_active_or_replaced_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    """Never uninstall a driver with live storage or delete a replacement object."""
    cluster = Cluster()
    runner = installer(tmp_path, monkeypatch, cluster)
    runner.install({**config("helm"), "timeout_s": 0})
    if fault == "active-volume":
        cluster.pvs = [{"spec": {"csi": {"driver": "test.csi"}}}]
    else:
        cluster.objects[("CSIDriver", "test.csi", "")]["metadata"]["uid"] = "replacement"
    with pytest.raises(RuntimeError):
        runner.remove()
    assert not any(args[1] in {"uninstall", "delete"} for args in cluster.commands)
    assert runner.state_path.exists()


@pytest.mark.parametrize("method", ["helm", "kustomize"])
@pytest.mark.parametrize("deleted_at", [0, 2, 3, None])
def test_teardown_waits_for_pvs_until_recorded_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, deleted_at: int | None
) -> None:
    """Wait for asynchronous deletion, ignoring other drivers and retaining state on timeout."""
    cluster = Cluster(method)
    runner = installer(tmp_path, monkeypatch, cluster)
    runner.install({**config(method), "timeout_s": 3})
    runner.timeout = 99  # Teardown must reload the recorded installation timeout.
    original_objects = copy.deepcopy(cluster.objects)
    elapsed = 0.0
    sleeps: list[float] = []
    polls: list[float] = []

    def sleep(seconds: float) -> None:
        """Advance a deterministic clock without delaying the test."""
        nonlocal elapsed
        sleeps.append(seconds)
        elapsed += seconds

    monkeypatch.setattr(MODULE, "time", SimpleNamespace(monotonic=lambda: elapsed, sleep=sleep), raising=False)

    def command(args: list[str], data: str | None = None) -> str:
        """Keep unrelated storage present and finish probe deletion at the specified time."""
        if args[1:3] == ["get", "pv"]:
            polls.append(elapsed)
            cluster.pvs = [{"spec": {"csi": {"driver": "other.csi"}}}, {"spec": {}}]
            if deleted_at is None or elapsed < deleted_at:
                cluster.pvs.append({"spec": {"csi": {"driver": "test.csi"}}})
        if args[1] in {"uninstall", "delete"}:
            assert deleted_at is not None and elapsed >= deleted_at
        return cluster.command(args, data)

    monkeypatch.setattr(runner, "command", command)
    if deleted_at is None:
        with pytest.raises(RuntimeError, match="CSI installation still has PVs"):
            runner.remove()
        assert cluster.objects == original_objects
        assert runner.state_path.exists()
    else:
        runner.remove()
        assert not cluster.objects
        assert not runner.state_path.exists()
    assert elapsed == (deleted_at if deleted_at is not None else 3)
    assert sleeps == ([] if deleted_at == 0 else [2] if deleted_at == 2 else [2, 1])
    assert polls == ([0] if deleted_at == 0 else [0, 2] if deleted_at == 2 else [0, 2, 3])


def test_default_storage_class_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An install must not bind unrelated pending PVCs by changing the cluster default."""
    cluster = Cluster()
    cluster.docs.append(
        {
            "apiVersion": "storage.k8s.io/v1",
            "kind": "StorageClass",
            "metadata": {"name": "unsafe", "annotations": {"storageclass.kubernetes.io/is-default-class": "true"}},
        }
    )
    runner = installer(tmp_path, monkeypatch, cluster)
    with pytest.raises(ValueError, match="default StorageClass"):
        runner.install(config("helm"))
    assert not cluster.objects


def test_zero_replica_workload_is_not_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful rollout command for zero replicas cannot satisfy readiness."""
    cluster = Cluster()
    cluster.docs[1]["spec"]["replicas"] = 0
    runner = installer(tmp_path, monkeypatch, cluster)
    with pytest.raises(RuntimeError, match="no ready replicas"):
        runner.install(config("helm"))
    runner.remove()
    assert not cluster.objects


@pytest.mark.parametrize("method", ["helm", "kustomize"])
def test_create_timeout_can_recover_owned_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """A lost client response after namespace creation must not leak the installation."""
    cluster = Cluster(method)
    runner = installer(tmp_path, monkeypatch, cluster)
    original_command = cluster.command

    def command(args: list[str], data: str | None = None) -> str:
        """Lose just the response to a successful namespace create."""
        value = original_command(args, data)
        if args[1] == "create" and yaml.safe_load(data)["kind"] == "Namespace":
            raise RuntimeError("client timeout after server create")
        return value

    monkeypatch.setattr(runner, "command", command)
    with pytest.raises(RuntimeError, match="client timeout"):
        runner.install(config(method))
    assert json.loads(runner.state_path.read_text())["resources"][0]["kind"] == "Namespace"
    runner.remove()
    assert not cluster.objects


def test_partial_teardown_can_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Successful deletions persist so a later cleanup failure does not lose progress."""
    cluster = Cluster()
    runner = installer(tmp_path, monkeypatch, cluster)
    runner.install(config("kustomize"))
    original_command = cluster.command

    def command(args: list[str], data: str | None = None) -> str:
        """Fail the final namespace deletion once."""
        if args[1] == "delete" and yaml.safe_load(data)["kind"] == "Namespace":
            raise RuntimeError("temporary cleanup error")
        return original_command(args, data)

    monkeypatch.setattr(runner, "command", command)
    with pytest.raises(RuntimeError, match="temporary cleanup"):
        runner.remove()
    assert [obj["kind"] for obj in json.loads(runner.state_path.read_text())["resources"]] == ["Namespace"]
    monkeypatch.setattr(runner, "command", original_command)
    runner.remove()
    assert not cluster.objects


@pytest.mark.parametrize("instance_namespace", ["probe", "another-tenant", "", "owned-cluster"])
def test_crd_teardown_respects_foreign_instances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, instance_namespace: str
) -> None:
    """Only registry objects inside the newly owned namespace may disappear with it."""
    cluster = Cluster()
    runner = installer(tmp_path, monkeypatch, cluster)
    runner.install(config("kustomize"))
    crd = {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "CustomResourceDefinition",
        "metadata": {"name": "registries.test.csi"},
    }
    cluster.create(crd)
    runner.remember(cluster.objects[cluster.key(crd)])
    instance = {
        "apiVersion": "test.csi/v1",
        "kind": "Registry",
        "metadata": {"name": "test", "namespace": instance_namespace},
    }
    if instance_namespace == "owned-cluster":
        instance["metadata"].pop("namespace")
        cluster.create(instance)
        instance = cluster.objects[cluster.key(instance)]
        runner.remember(instance)
    original_command = cluster.command

    def command(args: list[str], data: str | None = None) -> str:
        """Return either owned registry state or foreign CR data."""
        if args[1:3] == ["get", "registries.test.csi"]:
            return json.dumps({"items": [instance]})
        return original_command(args, data)

    monkeypatch.setattr(runner, "command", command)
    if instance_namespace in {"probe", "owned-cluster"}:
        runner.remove()
        assert not cluster.objects
    else:
        with pytest.raises(RuntimeError, match="outside its namespace"):
            runner.remove()
        assert not any(args[1] in {"delete", "uninstall"} for args in cluster.commands)


def test_install_preserves_reviewed_namespace_labels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Namespace policies required by a CSI package must survive fresh namespace creation."""
    cluster = Cluster()
    cluster.docs.insert(
        0,
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": "probe", "labels": {"pod-security.kubernetes.io/enforce": "privileged"}},
        },
    )
    runner = installer(tmp_path, monkeypatch, cluster)
    runner.install(config("kustomize"))
    assert (
        cluster.objects[("Namespace", "probe", "")]["metadata"]["labels"]["pod-security.kubernetes.io/enforce"]
        == "privileged"
    )
    runner.remove()
