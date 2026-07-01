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

"""NVIDIA GPU utilities for parsing nvidia-smi output.

This module provides shared parsing functions for nvidia-smi output formats
used across various validation tests (Slurm, Kubernetes, bare metal).
"""

import re
from dataclasses import dataclass


@dataclass
class GpuInfo:
    """Information about a single GPU."""

    index: int
    name: str
    uuid: str = ""
    memory_total: str = ""
    driver_version: str = ""
    temperature: int | None = None
    utilization: str = ""


def count_gpus_from_list_output(output: str) -> int:
    """Count GPUs from nvidia-smi -L or --list-gpus output.

    Args:
        output: Output from `nvidia-smi -L` or `nvidia-smi --list-gpus`

    Returns:
        Number of GPUs found in output

    Example output format:
        GPU 0: NVIDIA A100-SXM4-80GB (UUID: GPU-xxx)
        GPU 1: NVIDIA A100-SXM4-80GB (UUID: GPU-yyy)
    """
    return len([line for line in output.split("\n") if line.strip().startswith("GPU ")])


def count_gpus_from_full_output(output: str) -> int:
    """Count GPUs from full nvidia-smi output (table format).

    Args:
        output: Full output from `nvidia-smi` command

    Returns:
        Number of GPUs found in output

    Example pattern matched: "| 0  NVIDIA A100-SXM4-80GB", "| 0  Tesla T4"
    """
    gpu_lines = re.findall(r"\|\s{1,4}\d{1,3}\s{2,}", output, re.MULTILINE)
    return len(gpu_lines)


def parse_gpu_list(output: str) -> list[GpuInfo]:
    """Parse GPU list from nvidia-smi -L output.

    Args:
        output: Output from `nvidia-smi -L`

    Returns:
        List of GpuInfo objects with index, name, and UUID

    Example output format:
        GPU 0: NVIDIA A100-SXM4-80GB (UUID: GPU-12345678-abcd-efgh-ijkl-1234567890ab)
    """
    gpus = []
    pattern = r"GPU (\d+): (.+?)(?:\s*\(UUID: ([^)]+)\))?"

    for line in output.split("\n"):
        line = line.strip()
        if not line or not line.startswith("GPU "):
            continue

        match = re.match(pattern, line)
        if match:
            index = int(match.group(1))
            name = match.group(2).strip()
            uuid = match.group(3) or ""
            gpus.append(GpuInfo(index=index, name=name, uuid=uuid))

    return gpus


def parse_gpu_names_csv(output: str) -> list[str]:
    """Parse GPU names from nvidia-smi CSV output.

    Args:
        output: Output from `nvidia-smi --query-gpu=name --format=csv,noheader`

    Returns:
        List of GPU names
    """
    return [line.strip() for line in output.split("\n") if line.strip()]


def parse_driver_version(output: str) -> str | None:
    """Extract driver version from nvidia-smi output.

    Args:
        output: Full nvidia-smi output or --query-gpu=driver_version output

    Returns:
        Driver version string (e.g., "580.95.05") or None if not found
    """
    # Try full output format first: "Driver Version: 580.95.05"
    match = re.search(r"Driver Version:\s+([\d.]+)", output)
    if match:
        return match.group(1)

    # Try CSV format (single line output from --query-gpu)
    version = output.strip().split("\n")[0].strip()
    if version and re.match(r"^\d+\.\d+", version):
        return version

    return None


def parse_cuda_version(output: str) -> str | None:
    """Extract CUDA version from nvidia-smi output.

    Args:
        output: Full nvidia-smi output

    Returns:
        CUDA version string (e.g., "12.4") or None if not found

    Note: This is the maximum CUDA version supported by the driver,
    not necessarily the installed CUDA toolkit version. Newer drivers
    report ``CUDA UMD Version`` instead of the deprecated ``CUDA Version``
    header; both are accepted.
    """
    match = re.search(r"CUDA(?: UMD)? Version:\s+(\d+\.\d+)", output)
    if match:
        return match.group(1)
    return None


@dataclass
class GpuQueryResult:
    """Result of parsing GPU query CSV output."""

    gpus: list[dict[str, str]]
    malformed_lines: list[tuple[int, str, int]]  # (line_index, raw_line, field_count)


def parse_gpu_query_csv(
    output: str, fields: list[str], *, report_malformed: bool = False
) -> list[dict[str, str]] | GpuQueryResult:
    """Parse nvidia-smi CSV query output.

    Args:
        output: Output from nvidia-smi --query-gpu=field1,field2 --format=csv,noheader
        fields: List of field names corresponding to CSV columns
        report_malformed: If True, returns GpuQueryResult with malformed line info.
                         If False (default), returns just the list of parsed GPUs.

    Returns:
        If report_malformed is False: List of dicts mapping field names to values for each GPU
        If report_malformed is True: GpuQueryResult with both parsed GPUs and malformed lines

    Example:
        parse_gpu_query_csv("NVIDIA GB200, 189471 MiB, 580.95.05", ["name", "memory", "driver"])
        -> [{"name": "NVIDIA GB200", "memory": "189471 MiB", "driver": "580.95.05"}]
    """
    results = []
    malformed = []
    line_index = 0

    for line in output.split("\n"):
        line = line.strip()
        if not line:
            continue

        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= len(fields):
            results.append(dict(zip(fields, parts[: len(fields)], strict=False)))
        else:
            malformed.append((line_index, line, len(parts)))
        line_index += 1

    if report_malformed:
        return GpuQueryResult(gpus=results, malformed_lines=malformed)
    return results


def extract_first_gpu_info(output: str) -> str:
    """Extract first GPU info from any nvidia-smi output format.

    This is a flexible parser that works with multiple output formats:
    - nvidia-smi -L output
    - nvidia-smi --query-gpu CSV output
    - Full nvidia-smi table output

    Args:
        output: Any nvidia-smi output

    Returns:
        First GPU info string found, or empty string if none found
    """
    lines = output.split("\n")

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Check for GPU listing format: "GPU 0: NVIDIA GB200..."
        if line.startswith("GPU ") and ":" in line:
            return line

        # Check for CSV query output containing NVIDIA (all NVIDIA GPUs have this in their name)
        if "NVIDIA" in line:
            # Skip marker lines we might add (like GPU_LIST_START)
            if "GPU_LIST" not in line and "GPU_QUERY" not in line and "===" not in line:
                return line

    return ""


def has_gpu_output(output: str) -> bool:
    """Check if output contains valid GPU information.

    Args:
        output: Any nvidia-smi output

    Returns:
        True if output contains GPU information, False otherwise
    """
    # Check for common indicators of GPU presence
    if "No devices found" in output or "No devices were found" in output:
        return False

    return bool(extract_first_gpu_info(output))


def compare_versions(actual: str, minimum: str) -> bool:
    """Compare version strings (major.minor format).

    Args:
        actual: Actual version string (e.g., "580.95.05")
        minimum: Minimum required version (e.g., "580.00")

    Returns:
        True if actual >= minimum, False otherwise
    """
    try:
        # Handle versions with multiple dots, compare only major.minor
        actual_parts = [int(x.split("-")[0]) for x in actual.split(".")[:2]]
        min_parts = [int(x.split("-")[0]) for x in minimum.split(".")[:2]]

        # Pad with zeros if needed
        while len(actual_parts) < 2:
            actual_parts.append(0)
        while len(min_parts) < 2:
            min_parts.append(0)

        return actual_parts >= min_parts
    except (ValueError, IndexError):
        return False
