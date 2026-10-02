# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Import native Launch Kit JUnit through the standard validation reporting path."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import ClassVar

from isvtest.core.validation import BaseValidation


class LaunchKitConnectivityCheck(BaseValidation):
    """Expose native cases, including fabric skips, to pytest and report uploads."""

    description: ClassVar[str] = "Check the Kubernetes Launch Kit connectivity matrix"

    def run(self) -> None:
        """Import JUnit cases using the same subtest mechanism as conformance tests."""
        output = self.config.get("step_output")
        if not isinstance(output, dict) or output.get("operation") != "validate":
            self.set_failed("Missing Launch Kit validate step_output")
            return
        artifacts = output.get("artifacts") or {}
        report = artifacts.get("validation_junit") if isinstance(artifacts, dict) else None
        try:
            if not isinstance(report, str) or not report:
                raise ValueError("Missing Launch Kit JUnit artifact")
            root = ET.parse(Path(report)).getroot()
            if root.tag != "testsuites":
                raise ValueError("Launch Kit JUnit must have a testsuites root")
            cases = list(root.iter("testcase"))
            durations = [float(case.get("time", "0")) for case in cases]
            if any(not math.isfinite(duration) or duration < 0 for duration in durations):
                raise ValueError("Invalid Launch Kit JUnit testcase duration")
        except (OSError, ET.ParseError, ValueError) as exc:
            self.set_failed(f"{exc}: {output.get('error', '')}")
            return

        failures: list[str] = []
        measured = 0
        for case, duration in zip(cases, durations, strict=True):
            name = case.get("name", "unnamed")
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
                name, passed=not problems, skipped=skipped and not problems, message=message, duration=duration
            )
            if problems:
                failures.append(f"{name}: {message}")
            if case.get("classname") == "network.connectivity" and not skipped:
                measured += 1
        if failures:
            if output.get("error"):
                failures.append(str(output["error"]))
            self.set_failed("Launch Kit validation failed: " + "; ".join(failures))
        elif not output.get("success"):
            self.set_failed(str(output.get("error") or "Launch Kit validate failed"))
        elif not measured:
            self.set_failed("Launch Kit JUnit produced no executed connectivity tests")
        else:
            self.set_passed(f"Launch Kit connectivity passed ({measured} results)")
