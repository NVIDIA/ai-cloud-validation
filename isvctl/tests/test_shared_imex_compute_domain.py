# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the shared SDN17-02 compute-domain reference."""

from __future__ import annotations

import functools
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ISVCTL_ROOT / "configs" / "providers" / "shared" / "network" / "imex_compute_domain_test.py"
NETWORK_SUITE = ISVCTL_ROOT / "configs" / "suites" / "network.yaml"
AWS_CONFIG = ISVCTL_ROOT / "configs" / "providers" / "aws" / "config" / "network.yaml"
MY_ISV_CONFIG = ISVCTL_ROOT / "configs" / "providers" / "my-isv" / "config" / "network.yaml"

DAEMON_CLASS = "compute-domain-daemon.nvidia.com"
CHANNEL_CLASS = "compute-domain-default-channel.nvidia.com"
COMPUTE_DOMAIN_DRIVER = "compute-domain.nvidia.com"


@functools.cache
def _load_script() -> ModuleType:
    """Load the shared compute-domain script as a module for direct testing."""
    spec = importlib.util.spec_from_file_location("test_shared_imex_compute_domain_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _node(
    name: str,
    *,
    clique: str | None = "fabric-1.3",
    gpus: int = 8,
    gpu_label: bool = True,
) -> dict[str, Any]:
    """Return one fake GPU node object as the cluster API reports it."""
    labels: dict[str, str] = {}
    if gpu_label:
        labels["nvidia.com/gpu.present"] = "true"
    if clique is not None:
        labels["nvidia.com/gpu.clique"] = clique
    return {
        "metadata": {"name": name, "labels": labels},
        "status": {"allocatable": {"nvidia.com/gpu": str(gpus)} if gpus else {}},
    }


def _resource_slice(node: str, driver: str = COMPUTE_DOMAIN_DRIVER) -> dict[str, Any]:
    """Return one fake ResourceSlice published by a per-node plugin."""
    return {"metadata": {"name": f"{node}-{driver}"}, "spec": {"driver": driver, "nodeName": node}}


def _cluster(
    *,
    nodes: list[dict[str, Any]] | None = None,
    device_classes: list[str] | None = None,
    slices: list[dict[str, Any]] | None = None,
    compute_domain_crd: bool = True,
) -> dict[str, Any]:
    """Return the canned cluster state a fake kubectl answers from."""
    return {
        "nodes": [_node("gpu-1"), _node("gpu-2")] if nodes is None else nodes,
        "deviceclasses": [DAEMON_CLASS, CHANNEL_CLASS] if device_classes is None else device_classes,
        "resourceslices": [_resource_slice("gpu-1"), _resource_slice("gpu-2")] if slices is None else slices,
        "crd": compute_domain_crd,
    }


def _fake_run(cluster: dict[str, Any], calls: list[tuple[str, ...]] | None = None) -> Any:
    """Return a ``_run`` stub answering the script's reads from ``cluster``."""

    def run(_kubectl: list[str], *args: str) -> subprocess.CompletedProcess[str]:
        """Answer one kubectl read from the canned cluster state."""
        if calls is not None:
            calls.append(args)
        if args[0] == "api-resources":
            stdout = "computedomains.resource.nvidia.com\n" if cluster["crd"] else ""
            return subprocess.CompletedProcess(list(args), 0, stdout=stdout, stderr="")
        resource = args[1].split(".")[0]
        if resource == "deviceclasses":
            items: list[dict[str, Any]] = [{"metadata": {"name": name}} for name in cluster["deviceclasses"]]
        else:
            items = cluster[resource]
        return subprocess.CompletedProcess(list(args), 0, stdout=json.dumps({"items": items}), stderr="")

    return run


def _probe(monkeypatch: pytest.MonkeyPatch, cluster: dict[str, Any]) -> dict[str, Any]:
    """Run the probe against a canned cluster and return its contract."""
    module = _load_script()
    monkeypatch.setattr(module, "_run", _fake_run(cluster))
    return module._probe(["kubectl"])


def test_network_suite_wires_the_check_to_the_step() -> None:
    """SDN17-02 stays a kubernetes-gated check in the canonical network suite."""
    config = yaml.safe_load(NETWORK_SUITE.read_text())
    check = config["tests"]["validations"]["fabric_topology"]["checks"]["ImexComputeDomainCapabilityCheck"]

    assert check["test_id"] == "SDN17-02"
    assert check["step"] == "imex_compute_domain"
    assert check["requires"] == ["kubernetes"]
    assert check["labels"] == ["kubernetes", "min_req", "network"]


@pytest.mark.parametrize(
    ("provider_config", "command"),
    [
        (AWS_CONFIG, "python3 ../../shared/network/imex_compute_domain_test.py"),
        (MY_ISV_CONFIG, "python ../../shared/network/imex_compute_domain_test.py"),
    ],
)
def test_providers_wire_the_shared_reference(provider_config: Path, command: str) -> None:
    """The probe is cluster-API only, so providers run the shared reference as-is."""
    config = yaml.safe_load(provider_config.read_text())
    step = next(item for item in config["commands"]["network"]["steps"] if item["name"] == "imex_compute_domain")

    assert step["command"] == command
    assert step["phase"] == "test"
    assert step["requires"] == ["kubernetes"]
    assert step["output_schema"] == "imex_compute_domain"
    # No args: a node list would let the provider choose what gets tested.
    assert step.get("args", []) == []


def test_healthy_cluster_reports_every_node_published(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DRA cluster with both facts in place reports them per node."""
    result = _probe(monkeypatch, _cluster())

    assert result["success"] is True
    assert result["platform"] == "kubernetes"
    assert result["test_name"] == "imex_compute_domain"
    assert result["device_classes_registered"] is True
    assert result["nodes_checked"] == 2
    assert result["nodes_validated"] == 2
    assert result["nodes"] == [
        {"node_id": "gpu-1", "clique_labelled": True, "compute_domain_resources_published": True},
        {"node_id": "gpu-2", "clique_labelled": True, "compute_domain_resources_published": True},
    ]


def test_missing_compute_domain_crd_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cluster advertising no multi-node NVLink capability is out of scope."""
    result = _probe(monkeypatch, _cluster(compute_domain_crd=False))

    assert result["success"] is True
    assert result["skipped"] is True
    assert "no multi-node NVLink capability" in result["skip_reason"]
    assert result["nodes"] == []


def test_skip_gate_is_a_different_object_from_the_assertion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registered device classes are asserted, not used as the in-scope gate, so
    a cluster missing them fails rather than skipping itself out of the run."""
    result = _probe(monkeypatch, _cluster(device_classes=[]))

    assert result.get("skipped") is not True
    assert result["device_classes_registered"] is False


def test_publication_comes_from_the_published_resource(monkeypatch: pytest.MonkeyPatch) -> None:
    """A node whose plugin published no slice reports no compute-domain resources."""
    result = _probe(monkeypatch, _cluster(slices=[_resource_slice("gpu-1")]))

    assert result["nodes_validated"] == 1
    assert result["nodes"][1] == {
        "node_id": "gpu-2",
        "clique_labelled": True,
        "compute_domain_resources_published": False,
    }


def test_other_drivers_slices_do_not_count_as_publication(monkeypatch: pytest.MonkeyPatch) -> None:
    """The GPU driver's own slices are not compute-domain resources."""
    result = _probe(monkeypatch, _cluster(slices=[_resource_slice("gpu-1", "gpu.nvidia.com")]))

    assert [node["compute_domain_resources_published"] for node in result["nodes"]] == [False, False]


def test_unlabelled_gpu_node_stays_in_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """A GPU node without the clique label is reported, not dropped: the check
    fails it rather than letting the node leave the asserted set."""
    result = _probe(monkeypatch, _cluster(nodes=[_node("gpu-1"), _node("gpu-2", clique=None)]))

    assert [node["node_id"] for node in result["nodes"]] == ["gpu-1", "gpu-2"]
    assert result["nodes"][1]["clique_labelled"] is False
    assert result["nodes_checked"] == 2
    assert result["nodes_validated"] == 1


def test_blank_clique_label_is_not_a_clique(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty label value carries no clique identity."""
    result = _probe(monkeypatch, _cluster(nodes=[_node("gpu-1", clique="  ")]))

    assert result["nodes"][0]["clique_labelled"] is False


def test_non_gpu_nodes_are_out_of_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scope is the cluster's GPU accounting, so CPU nodes are not examined."""
    cpu_node = _node("cpu-1", clique=None, gpus=0, gpu_label=False)
    result = _probe(monkeypatch, _cluster(nodes=[_node("gpu-1"), cpu_node]))

    assert [node["node_id"] for node in result["nodes"]] == ["gpu-1"]


def test_dra_only_gpu_node_is_in_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cluster exposing GPUs only through DRA publishes no extended resource,
    so the feature-discovery label has to count as GPU accounting too."""
    result = _probe(monkeypatch, _cluster(nodes=[_node("gpu-1", gpus=0)]))

    assert [node["node_id"] for node in result["nodes"]] == ["gpu-1"]


def test_no_gpu_nodes_reports_an_empty_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty set is reported rather than skipped: the check fails it, which is
    what stops a cluster with no NVLink nodes from passing vacuously."""
    result = _probe(monkeypatch, _cluster(nodes=[]))

    assert result.get("skipped") is not True
    assert result["nodes"] == []
    assert result["nodes_checked"] == 0


@pytest.mark.parametrize(
    ("device_classes", "expected"),
    [
        ([DAEMON_CLASS, CHANNEL_CLASS], "driver"),
        ([CHANNEL_CLASS], "host"),
        ([], "host"),
    ],
)
def test_ownership_mode_follows_the_daemon_device_class(
    monkeypatch: pytest.MonkeyPatch,
    device_classes: list[str],
    expected: str,
) -> None:
    """The daemon class is what distinguishes driver-owned from host-owned IMEX."""
    result = _probe(monkeypatch, _cluster(device_classes=device_classes))

    assert result["daemon_ownership_mode"] == expected


def test_unrelated_device_classes_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only compute-domain classes count towards the cluster-wide assertion."""
    result = _probe(monkeypatch, _cluster(device_classes=["gpu.nvidia.com", "mig.nvidia.com"]))

    assert result["device_classes_registered"] is False


def test_device_classes_matched_by_role_not_exact_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Class names move with the driver version; the role they play does not."""
    result = _probe(monkeypatch, _cluster(device_classes=["compute-domain-v2-channel.nvidia.com"]))

    assert result["device_classes_registered"] is True
    assert result["daemon_ownership_mode"] == "host"


def test_probe_never_shells_into_a_node(monkeypatch: pytest.MonkeyPatch) -> None:
    """Everything asserted is a cluster object, and an IMEX tooling probe would
    need the command service enabled, which is not the default."""
    calls: list[tuple[str, ...]] = []
    module = _load_script()
    monkeypatch.setattr(module, "_run", _fake_run(_cluster(), calls))

    module._probe(["kubectl"])

    assert calls == [
        ("api-resources", "--api-group=resource.nvidia.com", "-o", "name"),
        ("get", "deviceclasses.resource.k8s.io", "-o", "json"),
        ("get", "resourceslices.resource.k8s.io", "-o", "json"),
        ("get", "nodes", "-o", "json"),
    ]


def test_run_bounds_the_process_and_api_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every kubectl process carries both a subprocess and an API request timeout."""
    module = _load_script()
    observed: dict[str, Any] = {}

    def fake_subprocess_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Capture the bounded command invocation."""
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_subprocess_run)

    module._run(["kubectl"], "get", "nodes")

    assert observed["command"] == ["kubectl", "get", "nodes", "--request-timeout=15s"]
    assert observed["timeout"] == module.DEFAULT_COMMAND_TIMEOUT_SECONDS


def test_unreachable_cluster_fails_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unserved API group is an empty successful listing, so a failed read is
    a cluster the probe could not reach - never one without multi-node NVLink."""
    module = _load_script()
    monkeypatch.setattr(sys, "argv", ["imex_compute_domain_test.py"])
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, stdout="", stderr="connection refused"),
    )

    assert module.main() == 1
    result = json.loads(capsys.readouterr().out)

    assert result["success"] is False
    assert result.get("skipped") is not True
    assert result["error_type"] == "compute_domain_probe_failed"
    assert "connection refused" in result["error"]
    assert result["nodes"] == []


@pytest.mark.parametrize(
    ("stdout", "message"),
    [("not json", "invalid JSON"), (json.dumps({}), "missing items")],
)
def test_malformed_payload_is_a_readable_failure(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    message: str,
) -> None:
    """A response the probe cannot decode fails by name rather than raising."""
    module = _load_script()
    monkeypatch.setattr(
        module,
        "_run",
        lambda *_args: subprocess.CompletedProcess([], 0, stdout=stdout, stderr=""),
    )

    with pytest.raises(module.ComputeDomainProbeError, match=message):
        module._items(["kubectl"], "nodes")


def test_blank_kubectl_override_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A misconfigured KUBECTL fails loudly instead of running the wrong binary."""
    module = _load_script()
    monkeypatch.setenv("KUBECTL", "   ")

    with pytest.raises(module.ComputeDomainProbeError, match="KUBECTL must not be blank"):
        module._kubectl_command()


def test_kubectl_override_is_word_split(monkeypatch: pytest.MonkeyPatch) -> None:
    """A kubectl-compatible CLI prefix is honoured as documented."""
    module = _load_script()
    monkeypatch.setenv("KUBECTL", "oc --context prod")

    assert module._kubectl_command() == ["oc", "--context", "prod"]
