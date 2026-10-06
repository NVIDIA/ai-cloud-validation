# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Map Kubernetes Launch Kit's native JUnit suites onto static catalog tests.

``l8k validate --junit-path`` writes a ``network/validation`` suite of
deployment-state cases and one ``<Family>-<fabric>`` suite per connectivity
family, for the configured fabric only. Each catalog test reports one native
suite, with its cases as subtests. All of them share one Launch Kit run against
the current cluster (see :mod:`.runner`); with no inputs configured they skip.
``LaunchKitSosreport`` reports that run's diagnostic collection outside the
catalog.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import ClassVar

import pytest

from isvtest.core.validation import BaseValidation
from isvtest.validations.k8s_launch_kit.runner import run_launch_kit

_FABRICS = ("ethernet", "infiniband")


class _LaunchKitCheck(BaseValidation):
    """A check that reads the shared Launch Kit run."""

    _exclude_from_discovery: ClassVar[bool] = True

    def _launch_kit_output(self) -> dict:
        """Return the shared Launch Kit result, skipping when no inputs are configured."""
        user_config = str(self.config.get("user_config") or "")
        deployment_files = str(self.config.get("deployment_files") or "")
        if not user_config and not deployment_files:
            pytest.skip(
                "Launch Kit inputs not configured: set tests.settings.k8s_launch_kit.user_config "
                "and tests.settings.k8s_launch_kit.deployment_files"
            )
        return run_launch_kit(user_config=user_config, deployment_files=deployment_files)


class _LaunchKitSuiteCheck(_LaunchKitCheck):
    """Report one native Launch Kit JUnit suite as this test's subtests."""

    _exclude_from_discovery: ClassVar[bool] = True

    def _suite_name(self) -> str:
        raise NotImplementedError

    def _missing_suite(self, name: str, root: ET.Element, output: dict) -> None:
        raise NotImplementedError

    def _unexplained_failure(self, root: ET.Element, output: dict) -> str | None:
        """Return a failure to report when every native case of this suite passed."""
        return None

    def run(self) -> None:
        output = self._launch_kit_output()
        if output.get("skip_reason"):
            pytest.skip(output["skip_reason"])
        artifacts = output.get("artifacts") or {}
        report = artifacts.get("validation_junit") if isinstance(artifacts, dict) else None
        try:
            name = self._suite_name()
            if not isinstance(report, str) or not report:
                raise ValueError("Missing Launch Kit JUnit artifact")
            root = ET.parse(Path(report)).getroot()
            if root.tag != "testsuites":
                raise ValueError("Launch Kit JUnit must have a testsuites root")
            suite = next((s for s in root.iter("testsuite") if s.get("name") == name), None)
            cases = [] if suite is None else suite.findall("testcase")
            durations = [float(case.get("time", "0")) for case in cases]
            if any(not math.isfinite(duration) or duration < 0 for duration in durations):
                raise ValueError("Invalid Launch Kit JUnit testcase duration")
        except (OSError, ET.ParseError, ValueError) as exc:
            # The run's own error is the cause; the unusable report is its symptom.
            error = output.get("error")
            self.set_failed(f"{error} ({exc})" if error else str(exc))
            return
        if suite is None:
            self._missing_suite(name, root, output)
            return

        failures: list[str] = []
        skip_reasons: list[str] = []
        executed = 0
        for case, duration in zip(cases, durations, strict=True):
            # Native case names repeat their suite name, which is already this test's name.
            case_name = case.get("name", "unnamed").removeprefix(f"{name}::")
            skipped = case.find("skipped") is not None
            problems = [*case.findall("failure"), *case.findall("error")]
            messages = [
                text
                for element in [
                    *problems,
                    *case.findall("skipped"),
                    *case.findall("system-out"),
                    *case.findall("system-err"),
                ]
                for text in (element.get("message"), element.text)
                if text
            ]
            message = "\n".join(messages)
            self.report_subtest(
                case_name, passed=not problems, skipped=skipped and not problems, message=message, duration=duration
            )
            if problems:
                failures.append(f"{case_name}: {message}")
            elif skipped:
                skip_reasons.append(message)
            else:
                executed += 1
        if failures:
            self.set_failed(f"{name} failed: " + "; ".join(failures))
        elif unexplained := self._unexplained_failure(root, output):
            self.set_failed(unexplained)
        elif not executed:
            pytest.skip("; ".join(dict.fromkeys(reason for reason in skip_reasons if reason)) or f"{name} not executed")
        else:
            self.set_passed(f"{name} passed ({executed} results)")


class _ConnectivityFamilyCheck(_LaunchKitSuiteCheck):
    """One connectivity family on one fabric, selected by the ``fabric`` parameter."""

    _exclude_from_discovery: ClassVar[bool] = True

    def _suite_name(self) -> str:
        fabric = self.config.get("fabric")
        if fabric not in _FABRICS:
            raise ValueError(f"fabric must be one of {', '.join(_FABRICS)}, got {fabric!r}")
        return f"{type(self).__name__}-{fabric}"

    def _missing_suite(self, name: str, root: ET.Element, output: dict) -> None:
        fabric = self.config["fabric"]
        other = next(f for f in _FABRICS if f != fabric)
        if any(suite.get("name") == f"{type(self).__name__}-{other}" for suite in root.iter("testsuite")):
            pytest.skip(f"Cluster fabric is not configured for this fabric type: {fabric}")
        pytest.skip(f"Launch Kit reported no {name} results")


class K8sEastWestNetworkICMPPing(_ConnectivityFamilyCheck):
    """Layer 3 (ICMP) connectivity between secondary-network pods."""

    description: ClassVar[str] = "Verify ICMP connectivity between secondary-network pods on every rail"


class K8sEastWestNetworkRDMAPing(_ConnectivityFamilyCheck):
    """Pod-to-pod RDMA-CM (rping) connectivity."""

    description: ClassVar[str] = "Verify pod-to-pod RDMA-CM (rping) connectivity on every rail"


class K8sEastWestNetworkIBWriteBandwidth(_ConnectivityFamilyCheck):
    """Pod-to-pod RDMA bandwidth (ib_write_bw) against the reference minimum."""

    description: ClassVar[str] = "Verify pod-to-pod RDMA bandwidth (ib_write_bw) meets the reference minimum"


class K8sEastWestNetworkDMABufBandwidth(_ConnectivityFamilyCheck):
    """GPUDirect RDMA (DMA-BUF) bandwidth between GPU-enabled pods."""

    description: ClassVar[str] = (
        "Verify GPUDirect RDMA (DMA-BUF) bandwidth between GPU pods meets the reference minimum"
    )


class K8sNetworkOperatorDeployment(_LaunchKitSuiteCheck):
    """Network Operator release, components, Helm values, manifests, and topology presets."""

    description: ClassVar[str] = "Verify the Network Operator deployment matches the Launch Kit reference configuration"

    def _suite_name(self) -> str:
        return "network/validation"

    def _missing_suite(self, name: str, root: ET.Element, output: dict) -> None:
        if unexplained := self._unexplained_failure(root, output):
            self.set_failed(unexplained)
            return
        pytest.skip(f"Launch Kit reported no {name} results")

    def _unexplained_failure(self, root: ET.Element, output: dict) -> str | None:
        # A failed l8k run that no native case explains (for example a coverage
        # policy) must not leave every catalog test green.
        if output.get("success"):
            return None
        if any(case.find("failure") is not None or case.find("error") is not None for case in root.iter("testcase")):
            return None
        return str(output.get("error") or "Launch Kit validate failed")


class LaunchKitSosreport(_LaunchKitCheck):
    """``l8k sosreport`` collected after the shared validate run."""

    description: ClassVar[str] = "Collect Network Operator diagnostics with l8k sosreport"
    # Diagnostic evidence, not a catalog test. Its failure fails the run without
    # changing any Network Operator result.
    catalog_exclude: ClassVar[bool] = True

    def run(self) -> None:
        sosreport = self._launch_kit_output().get("sosreport") or {}
        if sosreport.get("skip_reason"):
            pytest.skip(sosreport["skip_reason"])
        if sosreport.get("success"):
            collected = (sosreport.get("artifacts") or {}).get("sosreport")
            self.set_passed(f"sosreport collected: {collected}" if collected else "sosreport collected")
        else:
            self.set_failed(f"sosreport collection failed: {sosreport.get('error') or 'no result'}")
