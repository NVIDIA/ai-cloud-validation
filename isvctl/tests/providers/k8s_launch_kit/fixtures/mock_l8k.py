#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Executable Launch Kit test double for provider unit tests.

Values in this file are fixed mock output, not AI Cloud Validation defaults.
The provider passes only real l8k arguments and receives the same distinct
stdout forms used by version, schema, discover, generate, deploy, validate, and clean.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

_TIMESTAMP = "2026-08-05T08:27:43Z"
_SOURCE_COMMIT = "db32e4b98170"
_FIXTURE = Path(__file__).with_name("launch_kit_scenarios.json")
_VALUE_FLAGS: dict[str, set[str]] = {
    "version": {"--output"},
    "discover": {
        "--kubeconfig",
        "--save-cluster-config",
        "--user-config",
        "--fabric",
        "--deployment-type",
        "--multirail",
        "--node-selector",
        "--network-operator-release",
        "--output",
    },
    "generate": {
        "--user-config",
        "--save-deployment-files",
        "--network-operator-namespace",
        "--output",
    },
    "deploy": {
        "--kubeconfig",
        "--user-config",
        "--deployment-files",
        "--network-operator-namespace",
        "--deploy-timeout",
        "--output",
    },
    "validate": {
        "--kubeconfig",
        "--user-config",
        "--deployment-files",
        "--network-operator-namespace",
        "--connectivity",
        "--connectivity-timeout",
        "--validation-mode",
        "--validation-checks",
        "--rdma-rping-iterations",
        "--rdma-ib-write-size",
        "--rdma-ib-write-min-bandwidth-gbps",
        "--wait",
        "--report-path",
        "--output",
    },
    "clean": {
        "--kubeconfig",
        "--user-config",
        "--network-operator-namespace",
        "--keep-helm-chart",
        "--output",
    },
}
_BOOLEAN_FLAGS: dict[str, set[str]] = {
    "clean": {"--keep-helm-chart"},
}


def _load_fixture() -> dict[str, Any]:
    """Load the pinned mock contract and scenario definitions."""
    value = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Launch Kit fixture must contain an object")
    return value


def _parse_flags(command: str, argv: list[str]) -> dict[str, str]:
    """Parse the real flag subset exercised by the provider tests."""
    supported = _VALUE_FLAGS[command]
    values: dict[str, str] = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        if not token.startswith("--"):
            raise ValueError(f"unexpected positional argument: {token}")
        if "=" in token:
            flag, value = token.split("=", 1)
        else:
            flag = token
            if flag in _BOOLEAN_FLAGS.get(command, set()):
                value = "true"
            else:
                index += 1
                if index >= len(argv):
                    raise ValueError(f"missing value for {flag}")
                value = argv[index]
        if flag not in supported:
            raise ValueError(f"unknown flag for l8k {command}: {flag}")
        values[flag] = value
        index += 1
    return values


def _emit(value: dict[str, Any], *, pretty: bool = False) -> None:
    """Write one JSON document to stdout."""
    print(json.dumps(value, indent=2 if pretty else None))


def _message(level: str, message: str) -> dict[str, str]:
    """Build one ui.LogEntry-compatible record."""
    return {"level": level, "message": message, "timestamp": _TIMESTAMP}


def _profile(scenario: dict[str, Any]) -> dict[str, str]:
    """Return the profile fields emitted by Launch Kit."""
    return {
        "deployment": str(scenario["deployment"]),
        "fabric": str(scenario["fabric"]),
        "ignoreARP": "false",
        "multirail": "true",
        "routing": "destination-based",
    }


def _json_result(
    phase: str,
    *,
    profile: dict[str, str] | None = None,
    generated_files: list[str] | None = None,
) -> dict[str, Any]:
    """Build a successful ui.JSONResult-compatible object."""
    value: dict[str, Any] = {
        "success": True,
        "phase": phase,
        "deployed": False,
        "messages": [
            _message("info", f"Running {phase}"),
            _message("success", "Workflow completed successfully"),
        ],
    }
    if profile is not None:
        value["profile"] = profile
    if generated_files:
        value["generatedFiles"] = generated_files
    return value


def _structured_error(command: str, message: str) -> tuple[dict[str, Any], int]:
    """Build the JSON error emitted by a failed Launch Kit command."""
    category = "cluster" if command == "discover" else "deployment" if command == "deploy" else "validation"
    exit_code = 3 if category == "cluster" else 4 if category == "deployment" else 2
    return {
        "success": False,
        "phase": "",
        "deployed": False,
        "error": {
            "code": f"{category.upper()}_ERROR",
            "message": message,
            "category": category,
            "transient": category == "cluster",
            "suggestion": "Inspect the preserved Launch Kit logs and correct the reported condition",
        },
        "messages": None,
    }, exit_code


def _scenario_for_profile(fabric: str, deployment: str) -> tuple[str, dict[str, Any]]:
    """Find fixture data for one explicit profile."""
    scenarios = _load_fixture().get("scenarios")
    if not isinstance(scenarios, dict):
        raise ValueError("Launch Kit fixture has no scenarios map")
    for name, scenario in scenarios.items():
        if isinstance(scenario, dict) and scenario.get("fabric") == fabric and scenario.get("deployment") == deployment:
            return str(name), scenario
    raise ValueError(f"unsupported mock profile: fabric={fabric!r}, deployment={deployment!r}")


def _load_cluster_config(path: Path) -> dict[str, Any]:
    """Load a cluster configuration produced by the mock discovery command."""
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a YAML object")
    return value


def _scenario_from_config(flags: dict[str, str]) -> tuple[str, dict[str, Any]]:
    """Resolve a scenario from the persisted Launch Kit profile."""
    raw = flags.get("--user-config")
    if not raw:
        deployment = Path(flags.get("--deployment-files", "deployment"))
        raw = str(deployment.parent / "cluster-config.yaml")
    config = _load_cluster_config(Path(raw))
    profile = config.get("profile")
    if not isinstance(profile, dict):
        raise ValueError("cluster config has no profile")
    return _scenario_for_profile(str(profile.get("fabric", "")), str(profile.get("deployment", "")))


def _cluster_config_yaml(scenario: dict[str, Any]) -> str:
    """Render representative output from l8k discovery."""
    return f"""# Generated by `l8k discover`.
networkOperator:
  selectedRelease: "26.4"
  version: v26.4.1
  namespace: nvidia-network-operator
validation:
  gpuDirect:
    enabled: true
    gpuResourceType: nvidia.com/gpu
  connectivity: true
  mode: strict
  checks: [icmp, rping, ib_write_bw]
  rdma:
    rpingIterations: 5
    ibWriteSize: 65536
    ibWriteMinBandwidthGbps: 100
profile:
  fabric: {scenario["fabric"]}
  deployment: {scenario["deployment"]}
  multirail: true
  routing: destination-based
clusterConfig:
  - identifier: mock-group
    machineType: mock-vm
    gpuType: NVIDIA-H100-80GB-HBM3
    workerNodes: [worker-a, worker-b]
    nodeSelector:
      feature.node.kubernetes.io/pci-15b3.present: "true"
    pfs:
      - networkInterface: ens5f0np0
        pciAddress: "0000:17:00.0"
        rdmaDevice: mlx5_0
        traffic: east-west
        rail: 0
        connectedGPU: GPU0
        connectedGPUPCIAddress: "0000:41:00.0"
      - networkInterface: ens6f0np0
        pciAddress: "0000:31:00.0"
        rdmaDevice: mlx5_1
        traffic: east-west
        rail: 1
        connectedGPU: GPU1
        connectedGPUPCIAddress: "0000:71:00.0"
"""


def _resource_name(kind: str, rail: int) -> str:
    """Return a deterministic resource name for generated mock manifests."""
    prefix = {
        "IPPool": "nv-ipam-pool",
        "SriovNetwork": "sriov-network",
        "SriovIBNetwork": "sriov-ib-network",
        "MacvlanNetwork": "macvlan-network",
        "IPoIBNetwork": "ipoib-network",
        "HostDeviceNetwork": "hostdev-network",
        "SriovNetworkNodePolicy": "sriov-policy",
        "NicNodePolicy": "nic-node-policy",
    }.get(kind, kind.lower())
    return f"{prefix}-rail-{rail}-mock-group"


def _manifest_specs(scenario: dict[str, Any]) -> list[tuple[str, str, str, int]]:
    """Return generated API/kind/file/count tuples for one profile."""
    specs = [
        ("mellanox.com/v1alpha1", "NicClusterPolicy", "10-nic-cluster-policy", 1),
        ("configuration.net.nvidia.com/v1alpha1", "NicNodePolicy", "20-nic-node-policy", 1),
        ("nv-ipam.nvidia.com/v1alpha1", "IPPool", "30-ip-pool", 2),
    ]
    if scenario.get("requires_sriov"):
        specs.append(("sriovnetwork.openshift.io/v1", "SriovNetworkNodePolicy", "40-sriov-policy", 2))
    specs.append(
        (
            str(scenario["network_api_version"]),
            str(scenario["network_kind"]),
            "50-secondary-network",
            2,
        )
    )
    return specs


def _manifest_yaml(api_version: str, kind: str, count: int) -> str:
    """Render a valid multi-document mock manifest."""
    documents: list[str] = []
    for rail in range(count):
        name = "nic-cluster-policy" if kind == "NicClusterPolicy" else _resource_name(kind, rail)
        namespace = "" if kind in {"NicClusterPolicy", "NicNodePolicy"} else "  namespace: default\n"
        documents.append(
            f"apiVersion: {api_version}\nkind: {kind}\nmetadata:\n  name: {name}\n{namespace}spec:\n  mock: true\n"
        )
    return "---\n".join(documents)


def _write_generated_files(root: Path, scenario: dict[str, Any]) -> list[str]:
    """Materialize profile manifests and return their absolute paths."""
    manifest_dir = root / "network-operator"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    generated: list[Path] = []
    values = manifest_dir / "values.yaml"
    values.write_text("operator:\n  namespace: nvidia-network-operator\n", encoding="utf-8")
    generated.append(values)
    for api_version, kind, stem, count in _manifest_specs(scenario):
        path = manifest_dir / f"{stem}.yaml"
        path.write_text(_manifest_yaml(api_version, kind, count), encoding="utf-8")
        generated.append(path)
    example = manifest_dir / "60-example-daemonset-mock-group.yaml"
    example.write_text(
        "apiVersion: apps/v1\nkind: DaemonSet\nmetadata:\n  name: l8k-network-test\n  namespace: default\n"
        "spec:\n"
        "  selector:\n"
        "    matchLabels:\n"
        "      app: l8k-network-test\n"
        "  template:\n"
        "    metadata:\n"
        "      labels:\n"
        "        app: l8k-network-test\n"
        "    spec:\n"
        "      containers:\n"
        "        - name: test-container\n"
        "          image: nvcr.io/nvidia/doca/doca:3.3.0-full-rt-host\n"
        "          command: [/bin/bash, -c, sleep infinity]\n"
        "          resources:\n"
        "            requests:\n"
        "              nvidia.com/gpu: '2'\n"
        "            limits:\n"
        "              nvidia.com/gpu: '2'\n"
        "        - name: netshoot\n"
        "          image: nicolaka/netshoot:latest\n"
        "          command: [/bin/bash, -c, sleep infinity]\n",
        encoding="utf-8",
    )
    generated.append(example)
    return [str(path.resolve()) for path in generated]


def _manifest_results(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the exported Launch Kit manifest-validation results."""
    results: list[dict[str, Any]] = []
    for api_version, kind, stem, count in _manifest_specs(scenario):
        for rail in range(count):
            name = "nic-cluster-policy" if kind == "NicClusterPolicy" else _resource_name(kind, rail)
            reason = "resource exists and is Ready"
            results.append(
                {
                    "Kind": kind,
                    "APIVersion": api_version,
                    "Name": name,
                    "Namespace": "" if kind in {"NicClusterPolicy", "NicNodePolicy"} else "default",
                    "SourceFile": f"{stem}.yaml",
                    "State": "success",
                    "Reason": reason,
                    "Details": {},
                    "Found": True,
                    "Missing": False,
                    "Detail": reason,
                }
            )
    return results


def _ping_result(
    kind: int,
    src_node: str,
    dst_node: str,
    src_rail: str,
    dst_rail: str,
    fail_family: str | None,
) -> dict[str, Any]:
    """Build one exported connectivity matrix row."""
    family = "icmp" if kind < 2 else "rping" if kind < 4 else "ib_write_bw" if kind < 6 else "gpudirect_dmabuf"
    bandwidth_family = family in {"ib_write_bw", "gpudirect_dmabuf"}
    cross_rail = src_rail != dst_rail
    expectation = "forbidden" if cross_rail else "required"
    observed_ok = not cross_rail
    ok = True
    stderr = ""
    stdout = ""
    bandwidth = 0.0
    if family == "icmp":
        stdout = "1 packets transmitted, 1 received" if observed_ok else ""
        stderr = "Network is unreachable" if cross_rail else ""
    elif family == "rping":
        stdout = "client DISCONNECT EVENT" if observed_ok else ""
        stderr = "rping: connection timed out" if cross_rail else ""
    elif observed_ok:
        bandwidth = 191.25 if family == "gpudirect_dmabuf" else 187.6
        stdout = f"65536 5000 {bandwidth:.2f} {bandwidth:.2f} 0.3578"
    else:
        stderr = "ib_write_bw: failed to connect"

    if fail_family == family and not cross_rail and src_node == "worker-a" and src_rail == "rail-0":
        ok = False
        observed_ok = False
        if bandwidth_family:
            bandwidth = 42.5
            stderr = "observed bandwidth 42.5 Gbps below minimum 100 Gbps"
        else:
            stderr = f"{family}: connection refused"

    test = {
        "Kind": kind,
        "SrcPod": f"network-test-{src_node}",
        "DstPod": f"network-test-{dst_node}",
        "SrcNode": src_node,
        "DstNode": dst_node,
        "Rail": src_rail if not cross_rail else f"{src_rail}→{dst_rail}",
        "SrcIP": "192.168.128.10" if src_node == "worker-a" else "192.168.128.11",
        "DstIP": "192.168.128.11" if dst_node == "worker-b" else "192.168.128.10",
        "SrcRail": src_rail,
        "DstRail": dst_rail,
        "SrcIface": "net1" if src_rail == "rail-0" else "net2",
        "DstIface": "net1" if dst_rail == "rail-0" else "net2",
        "SrcRDMADev": "mlx5_0" if src_rail == "rail-0" else "mlx5_1",
        "DstRDMADev": "mlx5_0" if dst_rail == "rail-0" else "mlx5_1",
        "Expectation": expectation,
    }
    if family == "gpudirect_dmabuf":
        test.update(
            {
                "SrcGPUIndex": 0 if src_rail == "rail-0" else 1,
                "DstGPUIndex": 0 if dst_rail == "rail-0" else 1,
                "SrcGPUPCIAddress": "0000:41:00.0" if src_rail == "rail-0" else "0000:71:00.0",
                "DstGPUPCIAddress": "0000:41:00.0" if dst_rail == "rail-0" else "0000:71:00.0",
            }
        )
    return {
        "Test": test,
        "Family": family,
        "OK": ok,
        "ObservedOK": observed_ok,
        "Expectation": expectation,
        "Route": {"OK": not cross_rail},
        "BandwidthGbps": bandwidth,
        "MsgRateMpps": 0.3578 if bandwidth else 0.0,
        "MinBandwidthGbps": 100.0 if bandwidth_family else 0.0,
        "Stdout": stdout,
        "Stderr": stderr,
        **({"Error": stderr} if not ok else {}),
    }


def _connectivity_result(scenario_name: str, fail_family: str | None) -> dict[str, Any]:
    """Build a strict two-node, two-rail matrix."""
    rails = ["rail-0", "rail-1"]
    rows: list[dict[str, Any]] = []
    for kind in range(8):
        for src_node, dst_node in (("worker-a", "worker-b"), ("worker-b", "worker-a")):
            pairs = [(rail, rail) for rail in rails] if kind % 2 == 0 else [(rails[0], rails[1]), (rails[1], rails[0])]
            for src_rail, dst_rail in pairs:
                rows.append(_ping_result(kind, src_node, dst_node, src_rail, dst_rail, fail_family))
    failed = sum(row["OK"] is not True for row in rows)
    return {
        "DaemonSets": [
            {
                "Ref": {
                    "Namespace": "default",
                    "Name": f"l8k-network-test-{scenario_name}",
                    "Container": "test-container",
                    "RDMAContainer": "test-container",
                    "ICMPContainer": "netshoot",
                    "SourceFile": "60-example-daemonset-mock-group.yaml",
                },
                "Rollout": {"Desired": 2, "Updated": 2, "Available": 2, "Ready": 2, "NotReady": 0},
                "PodCount": 2,
            }
        ],
        "PingResults": rows,
        "Skipped": None,
        "Summary": {"TotalTests": len(rows), "Passed": len(rows) - failed, "Failed": failed},
    }


def _failure(command: str) -> tuple[bool, str | None]:
    """Resolve optional failure injection as ``command[:family]``."""
    parts = os.environ.get("L8K_MOCK_FAIL", "").split(":")
    if not parts or parts[0] != command:
        return False, None
    return len(parts) == 1, parts[1] if len(parts) > 1 else None


def _run_version(flags: dict[str, str]) -> int:
    """Mock ``l8k version``."""
    if flags.get("--output") == "json":
        _emit({"version": "v0.1.0-mock", "gitCommit": _SOURCE_COMMIT, "buildDate": _TIMESTAMP}, pretty=True)
    else:
        print("l8k v0.1.0-mock")
    return 0


def _run_schema() -> int:
    """Mock ``l8k schema`` with Launch Kit-owned capabilities and defaults."""
    fixture = _load_fixture()
    _emit(
        {
            "version": "v0.1.0-mock",
            "description": "CLI tool for deploying NVIDIA cloud-native networking solutions on Kubernetes",
            "commands": {
                command: {"description": f"Mock l8k {command}", "example": f"l8k {command}"}
                for command in ("discover", "generate", "deploy", "validate", "clean", "schema")
            },
            "phases": ["discover", "generate", "deploy"],
            "fabrics": sorted({str(value["fabric"]) for value in fixture["scenarios"].values()}),
            "deploymentTypes": sorted({str(value["deployment"]) for value in fixture["scenarios"].values()}),
            "outputFormats": ["text", "json"],
            "supportedNetworkOperatorReleases": ["26.4"],
            "exitCodes": {"0": "success", "2": "validation_error", "3": "cluster_error", "4": "deployment_error"},
            "flags": {
                "--node-selector": {
                    "type": "string",
                    "default": "feature.node.kubernetes.io/pci-15b3.present=true",
                    "description": "Node selector",
                },
                "--validation-mode": {
                    "type": "string",
                    "default": "inherit from validation.mode",
                    "description": "Validation mode",
                },
                "--validation-checks": {
                    "type": "[]string",
                    "default": "inherit from validation.checks",
                    "description": (
                        "Comma-separated checks: icmp, rping, ib_write_bw. "
                        "Enabled GPUDirect DMA-BUF validation follows ib_write_bw."
                    ),
                },
            },
        },
        pretty=True,
    )
    return 0


def _run_discover(flags: dict[str, str]) -> int:
    """Mock ``l8k discover``."""
    fail, _ = _failure("discover")
    if fail:
        result, exit_code = _structured_error("discover", "cluster discovery failed")
        _emit(result, pretty=True)
        print("Error: cluster discovery failed", file=sys.stderr)
        return exit_code
    _, scenario = _scenario_for_profile(flags.get("--fabric", ""), flags.get("--deployment-type", ""))
    path = Path(flags.get("--save-cluster-config", "cluster-config.yaml"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_cluster_config_yaml(scenario), encoding="utf-8")
    _emit(_json_result("discover", profile=_profile(scenario)), pretty=True)
    print(f"[success] Configuration saved: {path}", file=sys.stderr)
    return 0


def _run_generate(flags: dict[str, str]) -> int:
    """Mock ``l8k generate``."""
    fail, _ = _failure("generate")
    if fail:
        result, exit_code = _structured_error("generate", "deployment file generation failed")
        _emit(result, pretty=True)
        return exit_code
    _, scenario = _scenario_from_config(flags)
    root = Path(flags.get("--save-deployment-files", "deployment"))
    files = _write_generated_files(root, scenario)
    _emit(_json_result("generate", profile=_profile(scenario), generated_files=files), pretty=True)
    print(f"[success] Generated {len(files)} files", file=sys.stderr)
    return 0


def _run_deploy(flags: dict[str, str]) -> int:
    """Mock standalone ``l8k deploy`` including empty success stdout."""
    fail, _ = _failure("deploy")
    if fail:
        result, exit_code = _structured_error("deploy", "deployment failed")
        _emit(result, pretty=True)
        print("Error: deployment failed", file=sys.stderr)
        return exit_code
    _scenario_from_config(flags)
    print(f"[success] Deployment completed from {flags.get('--deployment-files', 'deployment')}", file=sys.stderr)
    return 0


def _run_validate(flags: dict[str, str]) -> int:
    """Mock the current three-document ``l8k validate`` JSON stream."""
    fail, family = _failure("validate")
    if fail:
        result, exit_code = _structured_error("validate", "failed to create Kubernetes client")
        _emit(result)
        print("Error: failed to create Kubernetes client", file=sys.stderr)
        return exit_code
    scenario_name, scenario = _scenario_from_config(flags)
    manifests = _manifest_results(scenario)
    static = {
        "versionCheck": {
            "Skipped": False,
            "Reason": "",
            "SelectedRelease": "26.4",
            "ExpectedVersion": "v26.4.1",
            "DeployedRelease": {
                "Name": "network-operator",
                "Namespace": "nvidia-network-operator",
                "ChartName": "network-operator",
                "ChartVersion": "26.4.1",
                "AppVersion": "v26.4.1",
                "Revision": 1,
                "Status": "deployed",
            },
            "Match": True,
        },
        "manifests": manifests,
        "presetDeviations": [],
        "summary": {
            "totalManifests": len(manifests),
            "successManifests": len(manifests),
            "inProgress": 0,
            "errorManifests": 0,
            "missingManifests": 0,
            "versionMatch": True,
            "deviationGroups": 0,
            "success": True,
        },
    }
    connectivity = _connectivity_result(scenario_name, family)
    deployment = Path(flags.get("--deployment-files", "deployment"))
    report = Path(flags.get("--report-path", str(deployment / "k8s-launch-kit-validation-report.html"))).resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    verdict = "FAILED" if connectivity["Summary"]["Failed"] else "PASSED"
    report.write_text(f"<!doctype html><html><body><h1>VALIDATION {verdict}</h1></body></html>\n", encoding="utf-8")
    _emit(static)
    _emit({"connectivity": connectivity})
    _emit({"reportPath": str(report)})
    print(f"HTML report written to {report}", file=sys.stderr)
    return 4 if connectivity["Summary"]["Failed"] else 0


def _run_clean(flags: dict[str, str]) -> int:
    """Mock the current one-document ``l8k clean`` JSON result."""
    fail, _ = _failure("clean")
    if fail:
        result, exit_code = _structured_error("clean", "Network Operator cleanup failed")
        _emit(result, pretty=True)
        print("Error: Network Operator cleanup failed", file=sys.stderr)
        return exit_code
    keep_helm = flags.get("--keep-helm-chart", "false").lower() == "true"
    _emit(
        {
            "success": True,
            "phase": "clean",
            "deployed": False,
            "cleanup": {
                "namespace": flags.get("--network-operator-namespace", "nvidia-network-operator"),
                "customResourcesDeleted": 12,
                "helmReleaseRemoved": not keep_helm,
                "keepHelmChart": keep_helm,
            },
            "messages": [],
        },
        pretty=True,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Execute one mocked Launch Kit command."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("mock l8k expects a command", file=sys.stderr)
        return 2
    command = args[0]
    try:
        if command == "schema":
            if len(args) != 1:
                raise ValueError("l8k schema accepts no arguments")
            return _run_schema()
        if command not in _VALUE_FLAGS:
            raise ValueError(f"unknown l8k command: {command}")
        flags = _parse_flags(command, args[1:])
        if command in {"discover", "generate", "deploy", "validate", "clean"} and flags.get("--output") != "json":
            raise ValueError("mock workflow commands require --output json")
        if command == "version":
            return _run_version(flags)
        if command == "discover":
            return _run_discover(flags)
        if command == "generate":
            return _run_generate(flags)
        if command == "deploy":
            return _run_deploy(flags)
        if command == "validate":
            return _run_validate(flags)
        return _run_clean(flags)
    except (KeyError, OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        result, exit_code = _structured_error(command, str(exc))
        _emit(result, pretty=command != "validate")
        print(f"Error: {exc}", file=sys.stderr)
        return exit_code


if __name__ == "__main__":
    sys.exit(main())
