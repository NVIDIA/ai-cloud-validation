# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for Kubernetes Launch Kit result interpretation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from isvtest.core.validation import BaseValidation
from isvtest.validations.k8s_launch_kit.checks import (
    LaunchKitDeploymentHealthCheck,
    LaunchKitEvidenceCaptureCheck,
    LaunchKitGpuDirectRdmaCheck,
    LaunchKitHostDeviceCheck,
    LaunchKitIcmpConnectivityCheck,
    LaunchKitInfiniBandCheck,
    LaunchKitKubernetesPrerequisiteCheck,
    LaunchKitMultirailCheck,
    LaunchKitRdmaBandwidthCheck,
    LaunchKitRdmaConnectivityCheck,
    LaunchKitRdmaSharedCheck,
    LaunchKitRoceCheck,
    LaunchKitSecondaryNetworkCheck,
    LaunchKitSriovReadinessCheck,
    LaunchKitTopologyDiscoveryCheck,
)

pytestmark = pytest.mark.unit

_NETWORK_KIND = {
    ("ethernet", "sriov"): "SriovNetwork",
    ("infiniband", "sriov"): "SriovIBNetwork",
    ("ethernet", "rdma_shared"): "MacvlanNetwork",
    ("infiniband", "rdma_shared"): "IPoIBNetwork",
    ("ethernet", "host_device"): "HostDeviceNetwork",
    ("infiniband", "host_device"): "HostDeviceNetwork",
}


def _discover(
    fabric: str = "ethernet",
    deployment: str = "sriov",
    *,
    multirail: bool = True,
) -> dict[str, Any]:
    """Build the provider envelope around a real discover-shaped document."""
    return {
        "success": True,
        "platform": "kubernetes",
        "operation": "discover",
        "exit_code": 0,
        "documents": [
            {
                "success": True,
                "phase": "discover",
                "profile": {
                    "fabric": fabric,
                    "deployment": deployment,
                    "multirail": "true" if multirail else "false",
                },
                "deployed": False,
                "messages": [],
            }
        ],
        "artifacts": {},
    }


def _manifest(kind: str, *, state: str = "success") -> dict[str, Any]:
    """Build one exported manifest validation row."""
    return {
        "Kind": kind,
        "APIVersion": "example.nvidia.com/v1",
        "Name": f"mock-{kind.lower()}",
        "Namespace": "default",
        "State": state,
        "Reason": "resource exists and is Ready" if state == "success" else "rollout has 1 unavailable pod",
        "Found": True,
        "Missing": False,
    }


def _matrix_row(kind: int, *, same_rail: bool, passed: bool = True) -> dict[str, Any]:
    """Build one exported connectivity row with source and destination detail."""
    destination_rail = "rail-0" if same_rail else "rail-1"
    family = "icmp" if kind < 2 else "rping" if kind < 4 else "ib_write_bw" if kind < 6 else "gpudirect_dmabuf"
    bandwidth_family = family in {"ib_write_bw", "gpudirect_dmabuf"}
    return {
        "Test": {
            "Kind": kind,
            "SrcNode": "worker-a",
            "DstNode": "worker-b",
            "SrcRail": "rail-0",
            "DstRail": destination_rail,
            "Expectation": "required" if same_rail else "forbidden",
            **(
                {
                    "SrcGPUIndex": 2,
                    "DstGPUIndex": 5,
                    "SrcGPUPCIAddress": "0000:41:00.0",
                    "DstGPUPCIAddress": "0000:71:00.0",
                }
                if family == "gpudirect_dmabuf"
                else {}
            ),
        },
        "Family": family,
        "OK": passed,
        "ObservedOK": same_rail if passed else False,
        "Expectation": "required" if same_rail else "forbidden",
        "BandwidthGbps": 187.6 if bandwidth_family and passed else 42.5,
        "MinBandwidthGbps": 100.0 if bandwidth_family else 0.0,
        "Stderr": "" if passed else f"{family}: connection refused on rail-0",
        **({"Error": f"{family} validation failed"} if not passed else {}),
    }


def _validate(
    fabric: str = "ethernet",
    deployment: str = "sriov",
    *,
    failed_kind: str | None = None,
    failed_family: str | None = None,
    include_sriov_policy: bool = True,
) -> dict[str, Any]:
    """Build a validate transport envelope with static and matrix documents."""
    network_kind = _NETWORK_KIND[(fabric, deployment)]
    kinds = ["NicClusterPolicy", "NicNodePolicy", "IPPool"]
    if deployment == "sriov" and include_sriov_policy:
        kinds.append("SriovNetworkNodePolicy")
    kinds.append(network_kind)
    manifests = [_manifest(kind, state="error" if kind == failed_kind else "success") for kind in kinds]
    rows: list[dict[str, Any]] = []
    for family, pair in {
        "icmp": (0, 1),
        "rping": (2, 3),
        "ib_write_bw": (4, 5),
        "gpudirect_dmabuf": (6, 7),
    }.items():
        rows.append(_matrix_row(pair[0], same_rail=True, passed=family != failed_family))
        rows.append(_matrix_row(pair[1], same_rail=False))
    failed_manifests = sum(item["State"] != "success" for item in manifests)
    failed_rows = sum(row["OK"] is not True for row in rows)
    return {
        "success": failed_rows == 0,
        "platform": "kubernetes",
        "operation": "validate",
        "exit_code": 0 if failed_rows == 0 else 4,
        "documents": [
            {
                "versionCheck": {
                    "Skipped": False,
                    "SelectedRelease": "26.4",
                    "ExpectedVersion": "v26.4.1",
                    "DeployedRelease": {"ChartVersion": "26.4.1"},
                    "Match": True,
                },
                "manifests": manifests,
                "presetDeviations": [],
                "summary": {
                    "totalManifests": len(manifests),
                    "successManifests": len(manifests) - failed_manifests,
                    "errorManifests": failed_manifests,
                    "missingManifests": 0,
                    "success": failed_manifests == 0,
                },
            },
            {
                "connectivity": {
                    "DaemonSets": [
                        {
                            "Ref": {"Namespace": "default", "Name": "l8k-network-test"},
                            "Rollout": {"Desired": 2, "Ready": 2, "NotReady": 0},
                        }
                    ],
                    "PingResults": rows,
                    "Summary": {"TotalTests": len(rows), "Failed": failed_rows},
                }
            },
        ],
        "artifacts": {},
        **({"error": "one or more connectivity rows failed"} if failed_rows else {}),
    }


def _config(
    output: dict[str, Any],
    *,
    discover: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Bind a command envelope and optional earlier step outputs to a check."""
    config = {"step_output": output, **extra}
    if discover is not None:
        config["discover_output"] = json.dumps(discover)
    return config


def test_kubernetes_prerequisite_reports_every_probe() -> None:
    """A failed prerequisite retains successful checks and its remediation detail."""
    output = {
        "success": False,
        "platform": "kubernetes",
        "operation": "kubernetes-preflight",
        "checks": [
            {"name": "api-version", "passed": True, "message": "server v1.34.1"},
            {"name": "nodes", "passed": False, "message": "Forbidden: cannot list nodes"},
            {"name": "non-empty-cluster", "passed": False, "message": "cluster contains no nodes"},
        ],
    }

    result = LaunchKitKubernetesPrerequisiteCheck(config=_config(output)).execute()

    assert result["passed"] is False
    assert [probe["name"] for probe in result["subtests"]] == ["api-version", "nodes", "non-empty-cluster"]
    assert "Forbidden: cannot list nodes" in result["error"]


def test_topology_discovery_uses_the_real_profile_document() -> None:
    """Discovery succeeds only when l8k resolves both fabric and deployment."""
    result = LaunchKitTopologyDiscoveryCheck(config=_config(_discover())).execute()

    assert result["passed"] is True
    assert [probe["name"] for probe in result["subtests"]] == ["discover-command", "resolved-profile"]


def test_deployment_health_reports_all_resources_before_failing() -> None:
    """One unhealthy manifest is named without hiding later manifest rows."""
    output = _validate(failed_kind="SriovNetworkNodePolicy")

    result = LaunchKitDeploymentHealthCheck(config=_config(output)).execute()

    assert result["passed"] is False
    names = [probe["name"] for probe in result["subtests"]]
    assert "SriovNetworkNodePolicy/default/mock-sriovnetworknodepolicy" in names
    assert names[-1] == "SriovNetwork/default/mock-sriovnetwork"
    assert "rollout has 1 unavailable pod" in result["error"]


def test_deployment_health_honors_the_launch_kit_exit_verdict() -> None:
    """A validate-level drift failure cannot be hidden by green manifest rows."""
    output = _validate()
    output["success"] = False
    output["exit_code"] = 4
    output["error"] = "l8k validate exited with code 4: component versions diverge"

    result = LaunchKitDeploymentHealthCheck(config=_config(output)).execute()

    assert result["passed"] is False
    assert result["subtests"][0]["name"] == "launch-kit-validate-command"
    assert "component versions diverge" in result["error"]


def test_deployment_health_allows_an_unconfigured_version_expectation() -> None:
    """An optional Launch Kit version check is reported as skipped, not failed."""
    output = _validate()
    output["documents"][0]["versionCheck"] = {
        "Skipped": True,
        "Reason": "cluster config has no selectedRelease",
    }

    result = LaunchKitDeploymentHealthCheck(config=_config(output)).execute()

    assert result["passed"] is True
    assert result["subtests"][1] == {
        "name": "network-operator-version",
        "passed": False,
        "skipped": True,
        "message": "cluster config has no selectedRelease",
        "duration": None,
    }


def test_sriov_readiness_requires_policy_and_network_kinds() -> None:
    """A non-vacuous SR-IOV result requires both policy and attachment resources."""
    discover = _discover("infiniband", "sriov")
    output = _validate("infiniband", "sriov", include_sriov_policy=False)

    result = LaunchKitSriovReadinessCheck(config=_config(output, discover=discover)).execute()

    assert result["passed"] is False
    assert "kind-coverage/SriovNetworkNodePolicy" in result["error"]
    assert any(probe["name"].startswith("SriovIBNetwork/") for probe in result["subtests"])


@pytest.mark.parametrize(
    ("check_class", "fabric", "deployment", "expected_kind"),
    [
        (LaunchKitRoceCheck, "ethernet", "sriov", "SriovNetwork"),
        (LaunchKitInfiniBandCheck, "infiniband", "sriov", "SriovIBNetwork"),
        (LaunchKitHostDeviceCheck, "ethernet", "host_device", "HostDeviceNetwork"),
        (LaunchKitRdmaSharedCheck, "infiniband", "rdma_shared", "IPoIBNetwork"),
    ],
)
def test_profile_checks_require_the_applicable_network_kind(
    check_class: type[BaseValidation],
    fabric: str,
    deployment: str,
    expected_kind: str,
) -> None:
    """Profile-specific checks select the exact resource implied by discover."""
    result = check_class(
        config=_config(_validate(fabric, deployment), discover=_discover(fabric, deployment))
    ).execute()

    assert result["passed"] is True
    names = [probe["name"] for probe in result["subtests"]]
    assert names[0] == f"kind-coverage/{expected_kind}"
    assert len(names) == len(set(names))


def test_non_applicable_profile_is_skipped() -> None:
    """An individually selectable check is skipped when the selected profile does not apply."""
    check = LaunchKitInfiniBandCheck(config=_config(_validate(), discover=_discover()))

    with pytest.raises(pytest.skip.Exception, match="not infiniband"):
        check.execute()


@pytest.mark.parametrize(
    ("check_class", "family"),
    [
        (LaunchKitIcmpConnectivityCheck, "icmp"),
        (LaunchKitRdmaConnectivityCheck, "rping"),
        (LaunchKitRdmaBandwidthCheck, "ib_write_bw"),
        (LaunchKitGpuDirectRdmaCheck, "gpudirect_dmabuf"),
    ],
)
def test_connectivity_checks_create_source_bound_subtests(
    check_class: type[BaseValidation],
    family: str,
) -> None:
    """Each matrix row becomes an independently named report item."""
    result = check_class(config=_config(_validate())).execute()

    assert result["passed"] is True
    assert [probe["name"] for probe in result["subtests"]] == [
        f"{family}/worker-a->worker-b/rail-0->rail-0",
        f"{family}/worker-a->worker-b/rail-0->rail-1",
    ]


def test_connectivity_failure_preserves_stderr_and_bandwidth() -> None:
    """A bandwidth failure includes endpoints, rails, observation, and threshold."""
    result = LaunchKitRdmaBandwidthCheck(config=_config(_validate(failed_family="ib_write_bw"))).execute()

    assert result["passed"] is False
    assert len(result["subtests"]) == 2
    assert "bandwidthGbps=42.5" in result["error"]
    assert "minimumGbps=100.0" in result["error"]
    assert "connection refused on rail-0" in result["error"]


def test_gpudirect_failure_preserves_endpoint_gpu_and_bandwidth_evidence() -> None:
    """A DMA-BUF failure identifies both endpoint GPUs and the failed threshold."""
    result = LaunchKitGpuDirectRdmaCheck(config=_config(_validate(failed_family="gpudirect_dmabuf"))).execute()

    assert result["passed"] is False
    assert "gpuIndices=2->5" in result["error"]
    assert "sourceGpuPci=0000:41:00.0" in result["error"]
    assert "destinationGpuPci=0000:71:00.0" in result["error"]
    assert "bandwidthGbps=42.5" in result["error"]
    assert "minimumGbps=100.0" in result["error"]
    assert "error=gpudirect_dmabuf validation failed" in result["error"]


def test_gpudirect_prefers_the_exported_family_contract() -> None:
    """The stable Family field selects GPUDirect even if numeric kinds evolve."""
    output = _validate()
    rows = output["documents"][1]["connectivity"]["PingResults"]
    gpudirect_rows = [row for row in rows if row["Family"] == "gpudirect_dmabuf"]
    for row in gpudirect_rows:
        row["Test"]["Kind"] = 999
    output["documents"][1]["connectivity"]["PingResults"] = gpudirect_rows

    result = LaunchKitGpuDirectRdmaCheck(config=_config(output)).execute()

    assert result["passed"] is True
    assert len(result["subtests"]) == 2


def test_gpudirect_rejects_missing_endpoint_gpu_indices() -> None:
    """A green row without explicit endpoint GPU topology is not accepted."""
    output = _validate()
    row = next(
        row for row in output["documents"][1]["connectivity"]["PingResults"] if row["Family"] == "gpudirect_dmabuf"
    )
    row["Test"].pop("DstGPUIndex")

    result = LaunchKitGpuDirectRdmaCheck(config=_config(output)).execute()

    assert result["passed"] is False
    assert "invalid or missing endpoint GPU index" in result["error"]


def test_gpudirect_is_skipped_when_launch_kit_does_not_emit_the_family() -> None:
    """A discovery-disabled GPUDirect family is inapplicable, not failed."""
    output = _validate()
    output["documents"][1]["connectivity"]["PingResults"] = [
        row for row in output["documents"][1]["connectivity"]["PingResults"] if row["Family"] != "gpudirect_dmabuf"
    ]
    check = LaunchKitGpuDirectRdmaCheck(config=_config(output))

    with pytest.raises(pytest.skip.Exception, match=r"validation\.gpuDirect is disabled"):
        check.execute()


def test_secondary_network_requires_ipam_network_and_ready_test_pods() -> None:
    """Secondary-network coverage combines static resources with workload readiness."""
    discover = _discover("ethernet", "rdma_shared")
    result = LaunchKitSecondaryNetworkCheck(
        config=_config(_validate("ethernet", "rdma_shared"), discover=discover)
    ).execute()

    assert result["passed"] is True
    names = {probe["name"] for probe in result["subtests"]}
    assert {"kind-coverage/IPPool", "kind-coverage/MacvlanNetwork", "DaemonSet/default/l8k-network-test"} <= names


@pytest.mark.parametrize(
    "rollout",
    [
        {},
        {"Desired": 2, "Ready": 2},
        {"Desired": 0, "Ready": 0, "NotReady": 0},
        {"Desired": True, "Ready": True, "NotReady": 0},
    ],
)
def test_secondary_network_rejects_incomplete_or_empty_daemonset_rollout(rollout: dict[str, Any]) -> None:
    """Missing, invalid, or zero-sized rollout counts cannot pass vacuously."""
    discover = _discover("ethernet", "rdma_shared")
    output = _validate("ethernet", "rdma_shared")
    output["documents"][1]["connectivity"]["DaemonSets"][0]["Rollout"] = rollout

    result = LaunchKitSecondaryNetworkCheck(config=_config(output, discover=discover)).execute()

    assert result["passed"] is False
    rollout_probe = next(probe for probe in result["subtests"] if probe["name"].startswith("DaemonSet/"))
    assert rollout_probe["passed"] is False


def test_multirail_requires_same_and_cross_rail_coverage() -> None:
    """Multi-rail validation distinguishes same-rail reachability from isolation rows."""
    result = LaunchKitMultirailCheck(config=_config(_validate(), discover=_discover())).execute()

    assert result["passed"] is True
    assert result["subtests"][-2]["name"] == "same-rail-coverage"
    assert result["subtests"][-1]["name"] == "cross-rail-coverage"


def test_multirail_is_skipped_when_matrix_contains_one_rail() -> None:
    """A single-rail topology is inapplicable rather than a coverage failure."""
    output = _validate()
    connectivity = output["documents"][1]["connectivity"]
    connectivity["PingResults"] = [
        row for row in connectivity["PingResults"] if row["Test"]["SrcRail"] == row["Test"]["DstRail"]
    ]
    check = LaunchKitMultirailCheck(config=_config(output, discover=_discover()))

    with pytest.raises(pytest.skip.Exception, match="only one rail: rail-0"):
        check.execute()

    assert check._subtest_results == []


def test_structured_launch_kit_error_is_actionable() -> None:
    """A failed l8k invocation surfaces its structured error when documents are absent."""
    output = {
        "success": False,
        "platform": "kubernetes",
        "operation": "validate",
        "documents": [{"error": {"message": "failed to create Kubernetes client"}}],
        "error": "failed to create Kubernetes client; verify kubeconfig access",
    }

    result = LaunchKitDeploymentHealthCheck(config=_config(output)).execute()

    assert result["passed"] is False
    assert "failed to create Kubernetes client; verify kubeconfig access" in result["error"]


@pytest.mark.parametrize(("include_deploy", "expected_count"), [(True, 9), (False, 8)])
def test_evidence_check_requires_selected_command_artifacts_and_html_report(
    tmp_path: Path,
    include_deploy: bool,
    expected_count: int,
) -> None:
    """Evidence supports both lifecycle-owning and validation-only workflows."""
    outputs: dict[str, dict[str, Any]] = {}
    operations = ["prepare", "verify", "preflight", "discover", "generate", "validate"]
    if include_deploy:
        operations.insert(-1, "deploy")
    for operation in operations:
        artifact = tmp_path / f"{operation}.log"
        artifact.write_text(f"{operation} evidence\n", encoding="utf-8")
        outputs[operation] = {
            "success": True,
            "platform": "kubernetes",
            "operation": "kubernetes-preflight" if operation == "preflight" else operation,
            "working_directory": str(tmp_path),
            "artifacts": {"stderr": str(artifact)},
            "documents": [],
        }
    generated = tmp_path / "generated" / "network-operator.yaml"
    generated.parent.mkdir()
    generated.write_text("kind: NicClusterPolicy\n", encoding="utf-8")
    outputs["generate"]["documents"] = [{"generatedFiles": ["generated/network-operator.yaml"]}]
    report = tmp_path / "k8s-launch-kit-validation-report.html"
    report.write_text("<html>passed</html>\n", encoding="utf-8")
    outputs["validate"]["documents"] = [{"reportPath": str(report)}]

    extra_outputs = {
        "prepare_output": json.dumps(outputs["prepare"]),
        "verify_output": json.dumps(outputs["verify"]),
        "preflight_output": json.dumps(outputs["preflight"]),
        "discover_output": json.dumps(outputs["discover"]),
        "generate_output": json.dumps(outputs["generate"]),
    }
    if include_deploy:
        extra_outputs["deploy_output"] = json.dumps(outputs["deploy"])
    config = _config(outputs["validate"], **extra_outputs)
    result = LaunchKitEvidenceCaptureCheck(config=config).execute()

    assert result["passed"] is True
    assert len(result["subtests"]) == expected_count
