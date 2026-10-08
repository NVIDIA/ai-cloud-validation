#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Executable Launch Kit test double for the Network Operator check tests.

Values in this file are fixed mock output, not AI Cloud Validation defaults.
The runner passes only real l8k arguments and receives the same distinct
stdout forms used by validate and sosreport.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import yaml

_FIXTURE = Path(__file__).with_name("launch_kit_scenarios.json")
_VALUE_FLAGS: dict[str, set[str]] = {
    "validate": {
        "--junit-path",
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
    "sosreport": {
        "--kubeconfig",
        "--output-dir",
        "--output",
    },
}


def _load_fixture() -> dict[str, Any]:
    """Load the pinned mock contract and scenario definitions."""
    value = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Launch Kit fixture must contain an object")
    return value


def _parse_flags(command: str, argv: list[str]) -> dict[str, str]:
    """Parse the real flag subset exercised by the runner tests."""
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


def _structured_error(message: str) -> tuple[dict[str, Any], int]:
    """Build the JSON error emitted by a failed Launch Kit command."""
    return {
        "success": False,
        "phase": "",
        "deployed": False,
        "error": {
            "code": "VALIDATION_ERROR",
            "message": message,
            "category": "validation",
            "transient": False,
            "suggestion": "Inspect the preserved Launch Kit logs and correct the reported condition",
        },
        "messages": None,
    }, 2


def _scenario_for_profile(fabric: str, deployment: str) -> tuple[str, dict[str, Any]]:
    """Find fixture data for one explicit profile."""
    scenarios = _load_fixture().get("scenarios")
    if not isinstance(scenarios, dict):
        raise ValueError("Launch Kit fixture has no scenarios map")
    for name, scenario in scenarios.items():
        if isinstance(scenario, dict) and scenario.get("fabric") == fabric and scenario.get("deployment") == deployment:
            return str(name), scenario
    raise ValueError(f"unsupported mock profile: fabric={fabric!r}, deployment={deployment!r}")


def _scenario_from_config(flags: dict[str, str]) -> tuple[str, dict[str, Any]]:
    """Resolve a scenario from the profile in the supplied Launch Kit user config."""
    raw = flags.get("--user-config")
    if not raw:
        raise ValueError("--user-config is required")
    config = yaml.safe_load(Path(raw).read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError(f"{raw} must contain a YAML object")
    profile = config.get("profile")
    if not isinstance(profile, dict):
        raise ValueError("cluster config has no profile")
    return _scenario_for_profile(str(profile.get("fabric", "")), str(profile.get("deployment", "")))


def _resource_name(kind: str, rail: int) -> str:
    """Return a deterministic resource name for mock manifest results."""
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
    """Return the API/kind/file/count tuples of one profile's manifests."""
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


def _run_validate(flags: dict[str, str]) -> int:
    """Mock the current three-document ``l8k validate`` JSON stream."""
    fail, family = _failure("validate")
    if fail:
        result, exit_code = _structured_error("failed to create Kubernetes client")
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
    if junit_path := flags.get("--junit-path"):
        root = ET.Element("testsuites", name="l8k validation tests")
        static_cases = [
            ("NetworkOperatorVersion", static["versionCheck"]),
            *((f"{row['Kind']}/{row['Namespace']}/{row['Name']}", row) for row in manifests),
        ]
        static_suite = ET.SubElement(
            root,
            "testsuite",
            name="network/validation",
            tests=str(len(static_cases)),
            failures="0",
            errors="0",
            skipped="0",
            time="0.010",
        )
        for case_name, evidence in static_cases:
            case = ET.SubElement(static_suite, "testcase", name=case_name, classname="network.validation")
            ET.SubElement(case, "system-out").text = json.dumps(evidence)
        families = {
            "icmp": "ICMPPing",
            "rping": "RDMAPing",
            "ib_write_bw": "IBWriteBandwidth",
            "gpudirect_dmabuf": "DMABufBandwidth",
        }
        for family, suffix in families.items():
            name = f"K8sEastWestNetwork{suffix}-{scenario['fabric']}"
            rows = [row for row in connectivity["PingResults"] if row["Family"] == family]
            suite = ET.SubElement(
                root,
                "testsuite",
                name=name,
                tests=str(len(rows) or 1),
                failures=str(sum(not row["OK"] for row in rows)),
                errors="0",
                skipped="0" if rows else "1",
                time="0.018",
            )
            if not rows:
                case = ET.SubElement(suite, "testcase", name=name, classname="network.connectivity", time="0.000")
                ET.SubElement(case, "skipped", message="Check is not enabled")
            for index, row in enumerate(rows):
                case = ET.SubElement(
                    suite, "testcase", name=f"{name}::probe-{index}", classname="network.connectivity", time="0.000"
                )
                ET.SubElement(case, "system-out").text = json.dumps(row)
                if not row["OK"]:
                    ET.SubElement(case, "failure", message=row["Error"]).text = row["Stderr"]
        ET.ElementTree(root).write(junit_path, encoding="utf-8", xml_declaration=True)
    _emit(static)
    _emit({"connectivity": connectivity})
    _emit({"reportPath": str(report)})
    print(f"HTML report written to {report}", file=sys.stderr)
    return 4 if connectivity["Summary"]["Failed"] else 0


def _run_sosreport(flags: dict[str, str]) -> int:
    """Mock the current text-streaming ``l8k sosreport`` command."""
    output_dir = Path(flags.get("--output-dir", "./sosreport")).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "collection-errors.log").write_text("", encoding="utf-8")
    print("Collecting sosreport from cluster...")
    print(f"  Output:     {output_dir}")
    fail, _ = _failure("sosreport")
    if fail:
        print("Error: sosreport collection failed", file=sys.stderr)
        return 3
    # Like the Network Operator helper: archive next to the directory, then remove it.
    archive = output_dir.with_name(f"{output_dir.name}.tar.gz")
    archive.write_text("mock Network Operator diagnostic archive\n", encoding="utf-8")
    shutil.rmtree(output_dir)
    print(f"\nSosreport collected: {output_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Execute one mocked Launch Kit command."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("mock l8k expects a command", file=sys.stderr)
        return 2
    command = args[0]
    try:
        if command not in _VALUE_FLAGS:
            raise ValueError(f"unknown l8k command: {command}")
        flags = _parse_flags(command, args[1:])
        if command == "sosreport":
            return _run_sosreport(flags)
        if flags.get("--output") != "json":
            raise ValueError("mock validate requires --output json")
        return _run_validate(flags)
    except (KeyError, OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        result, exit_code = _structured_error(str(exc))
        _emit(result, pretty=command != "validate")
        print(f"Error: {exc}", file=sys.stderr)
        return exit_code


if __name__ == "__main__":
    sys.exit(main())
