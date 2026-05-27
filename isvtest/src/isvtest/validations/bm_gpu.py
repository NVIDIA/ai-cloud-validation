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

"""Bare metal GPU health validations."""

from typing import ClassVar

from isvtest.core.nvidia import GpuQueryResult, parse_gpu_names_csv, parse_gpu_query_csv
from isvtest.core.validation import BaseValidation


class BmGpuDetection(BaseValidation):
    """Verify GPUs are detected by the system."""

    description: ClassVar[str] = "Verify at least one GPU is detected via nvidia-smi"
    timeout: ClassVar[int] = 30
    labels: ClassVar[tuple[str, ...]] = ("bare_metal", "gpu")

    def run(self) -> None:
        """Query nvidia-smi and verify at least one GPU is detected."""
        result = self.run_command("nvidia-smi --query-gpu=name --format=csv,noheader")

        if result.exit_code != 0:
            self.set_failed(f"Failed to query GPUs: {result.stderr}")
            return

        gpus = parse_gpu_names_csv(result.stdout)
        if not gpus:
            self.set_failed("No GPUs detected")
            return

        # Check against expected count if configured
        expected_count = self.config.get("expected_count")
        if expected_count is not None:
            try:
                expected_count = int(expected_count)
            except (TypeError, ValueError):
                self.set_failed(f"Invalid expected_count config value: {expected_count!r}")
                return
            if len(gpus) != expected_count:
                self.set_failed(f"Expected {expected_count} GPUs, found {len(gpus)}")
                return

        gpu_list = "\n  - ".join(gpus)
        self.set_passed(f"Found {len(gpus)} GPU(s):\n  - {gpu_list}")


class BmGpuHealth(BaseValidation):
    """Check GPU health status using nvidia-smi metrics."""

    description: ClassVar[str] = "Query GPU health metrics (temperature, utilization)"
    timeout: ClassVar[int] = 30
    labels: ClassVar[tuple[str, ...]] = ("bare_metal", "gpu")

    def run(self) -> None:
        """Query GPU health metrics and validate temperature/utilization values."""
        result = self.run_command("nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu --format=csv,noheader")

        if result.exit_code != 0:
            self.set_failed(f"Failed to query GPU health: {result.stderr}")
            return

        query_result = parse_gpu_query_csv(result.stdout, ["name", "temperature", "utilization"], report_malformed=True)
        assert isinstance(query_result, GpuQueryResult)  # Type narrowing for mypy

        if not query_result.gpus and not query_result.malformed_lines:
            self.set_failed("No GPUs found for health check")
            return

        # Validate each GPU has valid metrics
        issues = []

        # Normalize max_temperature config once (outside the loop)
        max_temp_raw = self.config.get("max_temperature", 85)
        try:
            max_temp = int(max_temp_raw)
        except (TypeError, ValueError):
            self.set_failed(f"Invalid max_temperature config value: {max_temp_raw!r}")
            return

        # Report malformed lines (fewer fields than expected)
        for line_idx, raw_line, field_count in query_result.malformed_lines:
            issues.append(f"GPU {line_idx}: Expected 3 metrics (name,temp,util), got {field_count}")

        for i, gpu in enumerate(query_result.gpus):
            name = gpu.get("name", f"GPU {i}")
            temp_str = gpu.get("temperature", "")
            util_str = gpu.get("utilization", "")

            # Validate temperature (should be numeric)
            try:
                temp = int(temp_str)
                if temp > max_temp:
                    issues.append(f"GPU {i} ({name}): Temperature {temp}°C exceeds maximum {max_temp}°C")
            except ValueError:
                issues.append(f"GPU {i} ({name}): Invalid temperature value '{temp_str}'")

            # Validate utilization format
            if not util_str.endswith("%") and util_str.strip() != "N/A":
                try:
                    int(util_str)
                except ValueError:
                    issues.append(f"GPU {i} ({name}): Invalid utilization value '{util_str}'")

        if issues:
            self.set_failed("\n".join(issues))
            return

        self.set_passed(f"All {len(query_result.gpus)} GPU(s) have valid health metrics")


class BmGpuComputeCapability(BaseValidation):
    """Verify GPU compute capability can be queried."""

    description: ClassVar[str] = "Query GPU compute capability (e.g., 8.0, 9.0)"
    timeout: ClassVar[int] = 30
    labels: ClassVar[tuple[str, ...]] = ("bare_metal", "gpu")

    def run(self) -> None:
        """Query and validate GPU compute capability format."""
        result = self.run_command("nvidia-smi --query-gpu=compute_cap --format=csv,noheader")

        if result.exit_code != 0:
            self.set_failed(f"Failed to query compute capability: {result.stderr}")
            return

        compute_cap = result.stdout.strip().split("\n")[0] if result.stdout.strip() else ""
        if not compute_cap:
            self.set_failed("GPU compute capability not available")
            return

        # Validate format (should be X.Y)
        parts = compute_cap.split(".")
        if len(parts) != 2:
            self.set_failed(f"Invalid compute capability format: {compute_cap}")
            return

        try:
            int(parts[0])
            int(parts[1])
        except ValueError:
            self.set_failed(f"Invalid compute capability format: {compute_cap}")
            return

        # Check against minimum if configured
        min_capability = self.config.get("min_capability")
        if min_capability:
            try:
                actual_parts = [int(x) for x in compute_cap.split(".")]
                min_parts = [int(x) for x in str(min_capability).split(".")]
                if actual_parts < min_parts:
                    self.set_failed(f"Compute capability {compute_cap} is below minimum required {min_capability}")
                    return
            except (ValueError, IndexError):
                self.set_failed("Failed to compare compute capabilities")
                return

        self.set_passed(f"GPU compute capability: {compute_cap}")
