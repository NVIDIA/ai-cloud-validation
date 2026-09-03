# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Assertions over unmodified Kubernetes Launch Kit command output.

Cluster interaction and Launch Kit command execution stay in the provider.
These checks interpret the real discover and validate documents and expose
resource or matrix rows as pytest subtests for actionable reporting.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from isvtest.core.validation import BaseValidation

_CONNECTIVITY_FAMILIES: dict[str, set[int]] = {
    "icmp": {0, 1},
    "rping": {2, 3},
    "ib_write_bw": {4, 5},
    "gpudirect_dmabuf": {6, 7},
}

_PROFILE_NETWORK_KINDS: dict[tuple[str, str], str] = {
    ("ethernet", "sriov"): "SriovNetwork",
    ("infiniband", "sriov"): "SriovIBNetwork",
    ("ethernet", "rdma_shared"): "MacvlanNetwork",
    ("infiniband", "rdma_shared"): "IPoIBNetwork",
    ("ethernet", "host_device"): "HostDeviceNetwork",
    ("infiniband", "host_device"): "HostDeviceNetwork",
}


def _object(value: Any) -> dict[str, Any]:
    """Return ``value`` as a JSON object or an empty object."""
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    """Return ``value`` as a list or an empty list."""
    return value if isinstance(value, list) else []


def _profile_network_kind(profile: dict[str, Any]) -> str | None:
    """Return the expected secondary-network resource for a resolved profile."""
    fabric = profile.get("fabric")
    deployment = profile.get("deployment")
    if not isinstance(fabric, str) or not isinstance(deployment, str):
        return None
    return _PROFILE_NETWORK_KINDS.get((fabric, deployment))


class _LaunchKitCheck(BaseValidation):
    """Shared parsing and subtest reporting for Launch Kit checks."""

    _exclude_from_discovery: ClassVar[bool] = True

    def _step_output(self, operation: str | None = None) -> dict[str, Any] | None:
        """Return the bound provider envelope and validate its operation."""
        output = self.config.get("step_output")
        if not isinstance(output, dict):
            self.set_failed("Missing Launch Kit step_output")
            return None
        if operation is not None and output.get("operation") != operation:
            self.set_failed(f"Expected Launch Kit operation {operation!r}, got {output.get('operation')!r}")
            return None
        return output

    def _configured_output(self, key: str) -> dict[str, Any]:
        """Decode another step envelope passed through validation configuration."""
        value = self.config.get(key)
        if isinstance(value, dict):
            return value
        if not isinstance(value, str) or not value:
            return {}
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _documents(self, output: dict[str, Any]) -> list[dict[str, Any]]:
        """Return only object documents from a provider envelope."""
        return [document for document in _list(output.get("documents")) if isinstance(document, dict)]

    def _profile(self) -> dict[str, Any] | None:
        """Return the resolved profile from the real discover JSONResult."""
        discover = self._configured_output("discover_output")
        documents = self._documents(discover)
        profile = documents[0].get("profile") if documents else None
        if not isinstance(profile, dict):
            self.set_failed("Launch Kit discover output has no resolved profile")
            return None
        return profile

    def _static_document(self, output: dict[str, Any]) -> dict[str, Any] | None:
        """Find the manifest/version validation document."""
        for document in self._documents(output):
            if {"versionCheck", "manifests", "summary"}.issubset(document):
                return document
        provider_error = output.get("error")
        suffix = f": {provider_error}" if isinstance(provider_error, str) and provider_error else ""
        self.set_failed(f"Launch Kit validate output has no static validation document{suffix}")
        return None

    def _connectivity(self, output: dict[str, Any]) -> dict[str, Any] | None:
        """Find the source-bound connectivity matrix document."""
        for document in self._documents(output):
            connectivity = document.get("connectivity")
            if isinstance(connectivity, dict):
                return connectivity
        provider_error = output.get("error")
        suffix = f": {provider_error}" if isinstance(provider_error, str) and provider_error else ""
        self.set_failed(f"Launch Kit validate output has no connectivity matrix{suffix}")
        return None

    def _finish_probes(self, title: str, probes: list[dict[str, Any]]) -> None:
        """Report every probe and aggregate its failures after the final row."""
        if not probes:
            if not self._error:
                self.set_failed(f"{title} produced no probes")
            return
        failures: list[str] = []
        for index, probe in enumerate(probes, start=1):
            name = str(probe.get("name") or f"probe-{index}")
            passed = probe.get("passed") is True
            skipped = probe.get("skipped") is True
            message = str(probe.get("message") or probe.get("error") or "")
            self.report_subtest(name, passed=passed, skipped=skipped, message=message)
            if not passed and not skipped:
                failures.append(f"{name}: {message or 'failed without a diagnostic'}")
        if failures:
            self.set_failed(f"{title} failed: {'; '.join(failures)}")
            return
        self.set_passed(f"{title} passed ({len(probes)} probes)")

    def _manifest_probes(
        self,
        output: dict[str, Any],
        *,
        required_kinds: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Build probes from Launch Kit manifest validation rows."""
        static = self._static_document(output)
        if static is None:
            return []
        manifests = [item for item in _list(static.get("manifests")) if isinstance(item, dict)]
        if required_kinds is not None:
            manifests = [item for item in manifests if item.get("Kind") in required_kinds]
        probes = []
        for item in manifests:
            kind = str(item.get("Kind") or "unknown-kind")
            namespace = str(item.get("Namespace") or "cluster")
            name = str(item.get("Name") or "unknown")
            passed = item.get("State") == "success" and item.get("Missing") is not True
            probes.append(
                {
                    "name": f"{kind}/{namespace}/{name}",
                    "passed": passed,
                    "message": str(item.get("Reason") or item.get("Detail") or item.get("State") or ""),
                }
            )
        return probes

    def _matrix_probes(self, output: dict[str, Any], families: set[str]) -> list[dict[str, Any]]:
        """Build one informative subtest for every selected connectivity row."""
        connectivity = self._connectivity(output)
        if connectivity is None:
            return []
        probes: list[dict[str, Any]] = []
        for row in _list(connectivity.get("PingResults")):
            if not isinstance(row, dict):
                continue
            test = _object(row.get("Test"))
            kind = test.get("Kind")
            explicit_family = row.get("Family")
            family = explicit_family if isinstance(explicit_family, str) else None
            if family not in _CONNECTIVITY_FAMILIES:
                family = next(
                    (name for name, family_kinds in _CONNECTIVITY_FAMILIES.items() if kind in family_kinds),
                    None,
                )
            if family not in families:
                continue
            source = str(test.get("SrcNode") or test.get("SrcPod") or "unknown-source")
            destination = str(test.get("DstNode") or test.get("DstPod") or "unknown-destination")
            source_rail = str(test.get("SrcRail") or test.get("Rail") or "unknown-rail")
            destination_rail = str(test.get("DstRail") or test.get("Rail") or "unknown-rail")
            expectation = str(row.get("Expectation") or test.get("Expectation") or "required")
            passed = row.get("OK") is True
            stderr = str(row.get("Stderr") or "").strip()
            error = str(row.get("Error") or "").strip()
            bandwidth = row.get("BandwidthGbps")
            minimum = row.get("MinBandwidthGbps")
            details = [f"expectation={expectation}", f"observedOK={row.get('ObservedOK')}"]
            if family in {"ib_write_bw", "gpudirect_dmabuf"}:
                details.extend([f"bandwidthGbps={bandwidth}", f"minimumGbps={minimum}"])
            if family == "gpudirect_dmabuf":
                source_gpu = test.get("SrcGPUIndex")
                destination_gpu = test.get("DstGPUIndex")
                valid_gpu_indices = all(
                    isinstance(index, int) and not isinstance(index, bool) and index >= 0
                    for index in (source_gpu, destination_gpu)
                )
                passed = passed and valid_gpu_indices
                details.append(f"gpuIndices={source_gpu}->{destination_gpu}")
                source_gpu_pci = test.get("SrcGPUPCIAddress")
                destination_gpu_pci = test.get("DstGPUPCIAddress")
                if source_gpu_pci:
                    details.append(f"sourceGpuPci={source_gpu_pci}")
                if destination_gpu_pci:
                    details.append(f"destinationGpuPci={destination_gpu_pci}")
                if not valid_gpu_indices:
                    details.append("invalid or missing endpoint GPU index")
            if stderr:
                details.append(f"stderr={stderr}")
            if error and error != stderr:
                details.append(f"error={error}")
            probes.append(
                {
                    "name": f"{family}/{source}->{destination}/{source_rail}->{destination_rail}",
                    "passed": passed,
                    "message": ", ".join(details),
                    "source_rail": source_rail,
                    "destination_rail": destination_rail,
                }
            )
        return probes

    def _kind_coverage_probes(
        self,
        output: dict[str, Any],
        expected_kinds: set[str],
    ) -> list[dict[str, Any]]:
        """Require every applicable manifest kind to appear in Launch Kit output."""
        static = self._static_document(output)
        if static is None:
            return []
        observed = {
            str(item.get("Kind"))
            for item in _list(static.get("manifests"))
            if isinstance(item, dict) and item.get("Kind")
        }
        return [
            {
                "name": f"kind-coverage/{kind}",
                "passed": kind in observed,
                "message": f"observed kinds: {', '.join(sorted(observed)) or '(none)'}",
            }
            for kind in sorted(expected_kinds)
        ]


class LaunchKitKubernetesPrerequisiteCheck(_LaunchKitCheck):
    """Require a reachable Kubernetes API and a non-empty Ready-node inventory."""

    description: ClassVar[str] = "Check Kubernetes prerequisites before Launch Kit execution"

    def run(self) -> None:
        """Report every provider preflight probe."""
        output = self._configured_output("preflight_output") or self._step_output("kubernetes-preflight")
        if output is None:
            return
        probes = [probe for probe in _list(output.get("checks")) if isinstance(probe, dict)]
        self._finish_probes("Kubernetes prerequisite", probes)


class LaunchKitTopologyDiscoveryCheck(_LaunchKitCheck):
    """Validate that Launch Kit completed discovery and resolved a profile."""

    description: ClassVar[str] = "Check cluster topology discovery with Kubernetes Launch Kit"

    def run(self) -> None:
        """Check the real discover JSONResult without inventing topology fields."""
        output = self._configured_output("discover_output") or self._step_output("discover")
        if output is None:
            return
        documents = self._documents(output)
        document = documents[0] if len(documents) == 1 else {}
        profile = _object(document.get("profile"))
        probes = [
            {
                "name": "discover-command",
                "passed": output.get("success") is True and document.get("success") is True,
                "message": str(output.get("error") or f"phase={document.get('phase')!r}"),
            },
            {
                "name": "resolved-profile",
                "passed": bool(profile.get("fabric") and profile.get("deployment")),
                "message": f"fabric={profile.get('fabric')}, deployment={profile.get('deployment')}",
            },
        ]
        self._finish_probes("Launch Kit topology discovery", probes)


class LaunchKitDeploymentHealthCheck(_LaunchKitCheck):
    """Validate the Network Operator release and every generated resource."""

    description: ClassVar[str] = "Check Network Operator deployment health with Kubernetes Launch Kit"

    def run(self) -> None:
        """Report version and manifest readiness independently of connectivity."""
        output = self._step_output("validate")
        if output is None:
            return
        static = self._static_document(output)
        if static is None:
            return
        version = _object(static.get("versionCheck"))
        summary = _object(static.get("summary"))
        version_skipped = version.get("Skipped") is True
        probes = [
            {
                "name": "launch-kit-validate-command",
                "passed": output.get("success") is True and output.get("exit_code") == 0,
                "message": str(
                    output.get("error") or f"success={output.get('success')!r}, exitCode={output.get('exit_code')!r}"
                ),
            },
            {
                "name": "network-operator-version",
                "passed": not version_skipped and version.get("Match") is True,
                "skipped": version_skipped,
                "message": (
                    str(version.get("Reason"))
                    if version_skipped
                    else (
                        f"selected={version.get('SelectedRelease')}, expected={version.get('ExpectedVersion')}, "
                        f"deployed={_object(version.get('DeployedRelease')).get('ChartVersion')}"
                    )
                ),
            },
            {
                "name": "static-summary",
                "passed": summary.get("success") is True,
                "message": (
                    f"success={summary.get('successManifests')}/{summary.get('totalManifests')}, "
                    f"errors={summary.get('errorManifests')}, missing={summary.get('missingManifests')}"
                ),
            },
            {
                "name": "manifest-inventory",
                "passed": bool(_list(static.get("manifests"))),
                "message": f"rows={len(_list(static.get('manifests')))}",
            },
            *self._manifest_probes(output),
        ]
        self._finish_probes("Network Operator deployment health", probes)


class LaunchKitSriovReadinessCheck(_LaunchKitCheck):
    """Validate SR-IOV policies and secondary-network resources."""

    description: ClassVar[str] = "Check SR-IOV Network RDMA readiness with Kubernetes Launch Kit"

    def run(self) -> None:
        """Check applicable validated resources for an SR-IOV profile."""
        output = self._step_output("validate")
        if output is None:
            return
        profile = self._profile()
        if profile is None:
            return
        if profile.get("deployment") != "sriov":
            pytest.skip(f"selected Launch Kit deployment is {profile.get('deployment')}, not sriov")
        fabric = profile.get("fabric")
        network_kind = "SriovIBNetwork" if fabric == "infiniband" else "SriovNetwork"
        expected_kinds = {"SriovNetworkNodePolicy", network_kind}
        probes = self._kind_coverage_probes(output, expected_kinds)
        probes.extend(
            self._manifest_probes(
                output,
                required_kinds=expected_kinds,
            )
        )
        self._finish_probes("SR-IOV readiness", probes)


class LaunchKitRdmaConnectivityCheck(_LaunchKitCheck):
    """Validate every rping matrix result."""

    description: ClassVar[str] = "Check pod-to-pod RDMA connectivity with Kubernetes Launch Kit"

    def run(self) -> None:
        """Report all same-rail and cross-rail RDMA-CM observations."""
        output = self._step_output("validate")
        if output is not None:
            self._finish_probes("RDMA-CM connectivity", self._matrix_probes(output, {"rping"}))


class LaunchKitRoceCheck(_LaunchKitCheck):
    """Validate the selected Ethernet/RoCE profile resources."""

    description: ClassVar[str] = "Check RoCE secondary networking with Kubernetes Launch Kit"

    def run(self) -> None:
        """Skip non-Ethernet profiles and report the selected network resources."""
        output = self._step_output("validate")
        if output is None:
            return
        profile = self._profile()
        if profile is None:
            return
        if profile.get("fabric") != "ethernet":
            pytest.skip(f"selected Launch Kit fabric is {profile.get('fabric')}, not ethernet")
        network_kind = _profile_network_kind(profile)
        if network_kind is None:
            self.set_failed(f"Launch Kit returned an unsupported profile: {profile}")
            return
        probes = self._kind_coverage_probes(output, {network_kind})
        probes.extend(self._manifest_probes(output, required_kinds={network_kind}))
        self._finish_probes("Ethernet/RoCE profile", probes)


class LaunchKitInfiniBandCheck(_LaunchKitCheck):
    """Validate the selected InfiniBand profile resources."""

    description: ClassVar[str] = "Check InfiniBand networking with Kubernetes Launch Kit"

    def run(self) -> None:
        """Skip non-IB profiles and report IB network resources."""
        output = self._step_output("validate")
        if output is None:
            return
        profile = self._profile()
        if profile is None:
            return
        if profile.get("fabric") != "infiniband":
            pytest.skip(f"selected Launch Kit fabric is {profile.get('fabric')}, not infiniband")
        network_kind = _profile_network_kind(profile)
        if network_kind is None:
            self.set_failed(f"Launch Kit returned an unsupported profile: {profile}")
            return
        probes = self._kind_coverage_probes(output, {network_kind})
        probes.extend(self._manifest_probes(output, required_kinds={network_kind}))
        self._finish_probes("InfiniBand profile", probes)


class LaunchKitHostDeviceCheck(_LaunchKitCheck):
    """Validate an applicable host-device profile."""

    description: ClassVar[str] = "Check host-device networking with Kubernetes Launch Kit"

    def run(self) -> None:
        """Skip other deployment types and report HostDeviceNetwork rows."""
        output = self._step_output("validate")
        if output is None:
            return
        profile = self._profile()
        if profile is None:
            return
        if profile.get("deployment") != "host_device":
            pytest.skip(f"selected Launch Kit deployment is {profile.get('deployment')}, not host_device")
        probes = self._kind_coverage_probes(output, {"HostDeviceNetwork"})
        probes.extend(self._manifest_probes(output, required_kinds={"HostDeviceNetwork"}))
        self._finish_probes(
            "host-device networking",
            probes,
        )


class LaunchKitSecondaryNetworkCheck(_LaunchKitCheck):
    """Validate secondary-network resources and test DaemonSet readiness."""

    description: ClassVar[str] = "Check secondary-network and IPAM readiness with Kubernetes Launch Kit"

    def run(self) -> None:
        """Report network/IPPool manifests and test-pod rollout state."""
        output = self._step_output("validate")
        if output is None:
            return
        profile = self._profile()
        if profile is None:
            return
        network_kind = _profile_network_kind(profile)
        if network_kind is None:
            self.set_failed(f"Launch Kit returned an unsupported profile: {profile}")
            return
        network_kinds = {"SriovNetwork", "SriovIBNetwork", "MacvlanNetwork", "IPoIBNetwork", "HostDeviceNetwork"}
        probes = self._kind_coverage_probes(output, {"IPPool", network_kind})
        probes.extend(
            self._manifest_probes(
                output,
                required_kinds={"IPPool", *network_kinds},
            )
        )
        static = self._static_document(output)
        if static is None:
            return
        observed_network_kinds = {
            str(item.get("Kind"))
            for item in _list(static.get("manifests"))
            if isinstance(item, dict) and item.get("Kind") in network_kinds
        }
        probes.append(
            {
                "name": "kind-coverage/secondary-network",
                "passed": bool(observed_network_kinds),
                "message": f"observed kinds: {', '.join(sorted(observed_network_kinds)) or '(none)'}",
            }
        )
        connectivity = self._connectivity(output)
        if connectivity is None:
            return
        for daemonset in _list(connectivity.get("DaemonSets")):
            if not isinstance(daemonset, dict):
                continue
            ref = _object(daemonset.get("Ref"))
            rollout = _object(daemonset.get("Rollout"))
            desired = rollout.get("Desired")
            ready = rollout.get("Ready")
            not_ready = rollout.get("NotReady")
            valid_counts = all(type(value) is int and value >= 0 for value in (desired, ready, not_ready))
            rollout_detail = f"ready={ready}/{desired}, notReady={not_ready}"
            if not valid_counts:
                rollout_detail += ", invalid or missing integer rollout counts"
            probes.append(
                {
                    "name": f"DaemonSet/{ref.get('Namespace')}/{ref.get('Name')}",
                    "passed": valid_counts and desired > 0 and ready == desired and not_ready == 0,
                    "message": rollout_detail,
                }
            )
        self._finish_probes("secondary-network readiness", probes)


class LaunchKitRdmaSharedCheck(_LaunchKitCheck):
    """Validate an applicable RDMA Shared profile."""

    description: ClassVar[str] = "Check RDMA Shared networking with Kubernetes Launch Kit"

    def run(self) -> None:
        """Skip other profiles and report Macvlan or IPoIB resources."""
        output = self._step_output("validate")
        if output is None:
            return
        profile = self._profile()
        if profile is None:
            return
        if profile.get("deployment") != "rdma_shared":
            pytest.skip(f"selected Launch Kit deployment is {profile.get('deployment')}, not rdma_shared")
        network_kind = _profile_network_kind(profile)
        if network_kind is None:
            self.set_failed(f"Launch Kit returned an unsupported profile: {profile}")
            return
        probes = self._kind_coverage_probes(output, {network_kind})
        probes.extend(self._manifest_probes(output, required_kinds={network_kind}))
        self._finish_probes(
            "RDMA Shared networking",
            probes,
        )


class LaunchKitIcmpConnectivityCheck(_LaunchKitCheck):
    """Validate every source-bound ICMP matrix result."""

    description: ClassVar[str] = "Check source-bound ICMP connectivity with Kubernetes Launch Kit"

    def run(self) -> None:
        """Report all same-rail and expected-isolation ICMP observations."""
        output = self._step_output("validate")
        if output is not None:
            self._finish_probes("source-bound ICMP", self._matrix_probes(output, {"icmp"}))


class LaunchKitRdmaBandwidthCheck(_LaunchKitCheck):
    """Validate every ib_write_bw matrix result and its Launch Kit threshold."""

    description: ClassVar[str] = "Check RDMA bandwidth with Kubernetes Launch Kit"

    def run(self) -> None:
        """Use the observed and minimum bandwidth emitted by Launch Kit."""
        output = self._step_output("validate")
        if output is not None:
            self._finish_probes("RDMA bandwidth", self._matrix_probes(output, {"ib_write_bw"}))


class LaunchKitGpuDirectRdmaCheck(_LaunchKitCheck):
    """Validate every Launch Kit GPUDirect DMA-BUF bandwidth result."""

    description: ClassVar[str] = "Check GPUDirect RDMA DMA-BUF bandwidth with Kubernetes Launch Kit"

    def run(self) -> None:
        """Report endpoint GPU topology and Launch Kit's bandwidth verdict."""
        output = self._step_output("validate")
        if output is None:
            return
        probes = self._matrix_probes(output, {"gpudirect_dmabuf"})
        if not probes:
            if self._error:
                return
            pytest.skip(
                "Launch Kit emitted no gpudirect_dmabuf results; validation.gpuDirect is disabled "
                "or ib_write_bw is not selected"
            )
        self._finish_probes("GPUDirect RDMA DMA-BUF bandwidth", probes)


class LaunchKitMultirailCheck(_LaunchKitCheck):
    """Validate same-rail reachability and expected cross-rail isolation."""

    description: ClassVar[str] = "Check multi-rail connectivity behavior with Kubernetes Launch Kit"

    def run(self) -> None:
        """Require same-rail and cross-rail coverage when multiple rails exist."""
        output = self._step_output("validate")
        if output is None:
            return
        profile = self._profile()
        if profile is None:
            return
        multirail = profile.get("multirail")
        if multirail not in {True, "true"}:
            pytest.skip(f"selected Launch Kit profile is not multirail: {multirail!r}")
        probes = self._matrix_probes(output, set(_CONNECTIVITY_FAMILIES))
        rails = {rail for probe in probes for rail in (probe["source_rail"], probe["destination_rail"])}
        if len(rails) == 1 and "unknown-rail" not in rails:
            pytest.skip(f"Launch Kit connectivity matrix contains only one rail: {next(iter(rails))}")
        same_rail = [probe for probe in probes if probe["source_rail"] == probe["destination_rail"]]
        cross_rail = [probe for probe in probes if probe["source_rail"] != probe["destination_rail"]]
        probes.extend(
            [
                {"name": "same-rail-coverage", "passed": bool(same_rail), "message": f"rows={len(same_rail)}"},
                {"name": "cross-rail-coverage", "passed": bool(cross_rail), "message": f"rows={len(cross_rail)}"},
            ]
        )
        self._finish_probes("multi-rail behavior", probes)


def _artifact_paths(value: Any) -> list[Path]:
    """Recursively collect paths from provider artifact mappings."""
    if isinstance(value, dict):
        paths: list[Path] = []
        for child in value.values():
            paths.extend(_artifact_paths(child))
        return paths
    if isinstance(value, list):
        paths = []
        for child in value:
            paths.extend(_artifact_paths(child))
        return paths
    if isinstance(value, str) and value:
        return [Path(value)]
    return []


def _evidence_path(value: str, output: dict[str, Any]) -> Path:
    """Resolve a Launch Kit-emitted path against its command working directory."""
    path = Path(value)
    if path.is_absolute():
        return path
    working_directory = output.get("working_directory")
    if isinstance(working_directory, str) and working_directory:
        return Path(working_directory) / path
    return path


class LaunchKitEvidenceCaptureCheck(_LaunchKitCheck):
    """Validate raw command logs and the Launch Kit HTML report."""

    description: ClassVar[str] = "Check Launch Kit evidence capture"

    def run(self) -> None:
        """Require fresh evidence for every real workflow command."""
        outputs = {
            "verify": self._configured_output("verify_output"),
            "preflight": self._configured_output("preflight_output"),
            "discover": self._configured_output("discover_output"),
            "generate": self._configured_output("generate_output"),
        }
        deploy = self._configured_output("deploy_output")
        if deploy:
            outputs["deploy"] = deploy
        outputs["validate"] = self._step_output("validate") or {}
        prepare = self._configured_output("prepare_output")
        if prepare:
            outputs = {"prepare": prepare, **outputs}
        probes: list[dict[str, Any]] = []
        for operation, output in outputs.items():
            paths = _artifact_paths(output.get("artifacts"))
            existing = [path for path in paths if path.is_file()]
            probes.append(
                {
                    "name": f"{operation}-artifacts",
                    "passed": bool(paths) and len(existing) == len(paths),
                    "message": f"found {len(existing)}/{len(paths)} files",
                }
            )
        generated_paths = [
            _evidence_path(path, outputs["generate"])
            for document in self._documents(outputs["generate"])
            for path in _list(document.get("generatedFiles"))
            if isinstance(path, str)
        ]
        probes.append(
            {
                "name": "generated-files",
                "passed": bool(generated_paths) and all(path.is_file() for path in generated_paths),
                "message": (
                    f"found {sum(path.is_file() for path in generated_paths)}/{len(generated_paths)} generated files"
                ),
            }
        )
        validate = outputs["validate"]
        report_paths = [
            _evidence_path(document["reportPath"], validate)
            for document in self._documents(validate)
            if isinstance(document.get("reportPath"), str)
        ]
        probes.append(
            {
                "name": "launch-kit-html-report",
                "passed": bool(report_paths) and all(path.is_file() for path in report_paths),
                "message": ", ".join(str(path) for path in report_paths) or "no reportPath document",
            }
        )
        self._finish_probes("Launch Kit evidence capture", probes)
