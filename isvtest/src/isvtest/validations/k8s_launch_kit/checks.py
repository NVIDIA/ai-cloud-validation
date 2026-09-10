# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Connectivity assertions over unmodified Kubernetes Launch Kit output."""

from __future__ import annotations

from typing import Any, ClassVar

from isvtest.core.validation import BaseValidation

_FAMILY_BY_KIND = {
    0: "icmp",
    1: "icmp",
    2: "rping",
    3: "rping",
    4: "ib_write_bw",
    5: "ib_write_bw",
    6: "gpudirect_dmabuf",
    7: "gpudirect_dmabuf",
}


def _mapping(value: Any) -> dict[str, Any]:
    """Return ``value`` as a mapping or an empty mapping."""
    return value if isinstance(value, dict) else {}


def _sequence(value: Any) -> list[Any]:
    """Return ``value`` as a sequence or an empty sequence."""
    return value if isinstance(value, list) else []


class LaunchKitConnectivityCheck(BaseValidation):
    """Report every connectivity result emitted by ``l8k validate``."""

    description: ClassVar[str] = "Check the Kubernetes Launch Kit connectivity matrix"

    def _connectivity(self) -> dict[str, Any] | None:
        """Find the connectivity document in the bound provider output."""
        output = self.config.get("step_output")
        if not isinstance(output, dict):
            self.set_failed("Missing Launch Kit step_output")
            return None
        if output.get("operation") != "validate":
            self.set_failed(f"Expected Launch Kit operation 'validate', got {output.get('operation')!r}")
            return None
        for document in _sequence(output.get("documents")):
            connectivity = _mapping(document).get("connectivity")
            if isinstance(connectivity, dict):
                return connectivity
        provider_error = output.get("error")
        suffix = f": {provider_error}" if isinstance(provider_error, str) and provider_error else ""
        self.set_failed(f"Launch Kit validate output has no connectivity matrix{suffix}")
        return None

    @staticmethod
    def _probe(row: dict[str, Any], index: int) -> dict[str, Any]:
        """Convert one Launch Kit matrix row into one informative subtest."""
        test = _mapping(row.get("Test"))
        explicit_family = row.get("Family")
        family = explicit_family if isinstance(explicit_family, str) and explicit_family else None
        if family is None:
            family = _FAMILY_BY_KIND.get(test.get("Kind"), f"kind-{test.get('Kind', 'unknown')}")

        source = str(test.get("SrcNode") or test.get("SrcPod") or "unknown-source")
        destination = str(test.get("DstNode") or test.get("DstPod") or "unknown-destination")
        source_rail = str(test.get("SrcRail") or test.get("Rail") or "unknown-rail")
        destination_rail = str(test.get("DstRail") or test.get("Rail") or "unknown-rail")
        expectation = str(row.get("Expectation") or test.get("Expectation") or "required")
        details = [f"expectation={expectation}", f"observedOK={row.get('ObservedOK')}"]

        bandwidth = row.get("BandwidthGbps")
        minimum = row.get("MinBandwidthGbps")
        if bandwidth is not None or minimum is not None:
            details.extend([f"bandwidthGbps={bandwidth}", f"minimumGbps={minimum}"])

        source_gpu = test.get("SrcGPUIndex")
        destination_gpu = test.get("DstGPUIndex")
        if source_gpu is not None or destination_gpu is not None:
            details.append(f"gpuIndices={source_gpu}->{destination_gpu}")
        source_gpu_pci = test.get("SrcGPUPCIAddress")
        destination_gpu_pci = test.get("DstGPUPCIAddress")
        if source_gpu_pci:
            details.append(f"sourceGpuPci={source_gpu_pci}")
        if destination_gpu_pci:
            details.append(f"destinationGpuPci={destination_gpu_pci}")

        stderr = str(row.get("Stderr") or "").strip()
        error = str(row.get("Error") or "").strip()
        if stderr:
            details.append(f"stderr={stderr}")
        if error and error != stderr:
            details.append(f"error={error}")

        return {
            "name": f"{family}/{source}->{destination}/{source_rail}->{destination_rail}",
            "passed": row.get("OK") is True,
            "message": ", ".join(details) or f"matrix row {index}",
        }

    def run(self) -> None:
        """Expose the complete Launch Kit connectivity matrix as subtests."""
        connectivity = self._connectivity()
        if connectivity is None:
            return
        probes = [
            self._probe(row, index)
            for index, row in enumerate(_sequence(connectivity.get("PingResults")), start=1)
            if isinstance(row, dict)
        ]
        if not probes:
            self.set_failed("Launch Kit connectivity matrix produced no results")
            return

        failures: list[str] = []
        for probe in probes:
            self.report_subtest(
                probe["name"],
                passed=probe["passed"],
                message=probe["message"],
            )
            if not probe["passed"]:
                failures.append(f"{probe['name']}: {probe['message']}")
        if failures:
            self.set_failed("Launch Kit connectivity failed: " + "; ".join(failures))
            return
        self.set_passed(f"Launch Kit connectivity passed ({len(probes)} results)")
