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

"""Shared NCCL output parsing utilities.

Used by k8s_nccl, k8s_nccl_multinode, and slurm_nccl_multinode workloads
to parse NCCL AllReduce benchmark output consistently.
"""

import re
from dataclasses import dataclass

# Regex patterns for NCCL benchmark output.
# The "#" prefix is optional - present in some container output, absent in others.
_RE_AVG_BUS_BW = re.compile(r"#?\s*Avg bus bandwidth\s*:\s*([\d.]+)")
_RE_OUT_OF_BOUNDS = re.compile(r"#?\s*Out of bounds values\s*:\s*(\d+)")
_NCCL_DATA_TYPES = {"float", "half", "double", "int8", "int32", "uint8", "uint32", "int64", "uint64", "bfloat16"}
_BUSBW_COL = 7


@dataclass
class NcclResult:
    """Parsed result of an NCCL benchmark run."""

    success: bool
    avg_bus_bw_gbps: float = 0.0
    max_bus_bw_gbps: float = 0.0
    out_of_bounds: int = -1
    error: str = ""
    output: str = ""


def _parse_max_bus_bw(output: str) -> float:
    """Extract max bus bandwidth from NCCL data table lines.

    Data lines have the format (column indices):
      0:size 1:count 2:type 3:redop 4:root 5:time 6:algbw 7:busbw 8:#wrong ...
    Identified by having a known NCCL data type (e.g. "float") in column 2.
    """
    max_bw = 0.0
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= _BUSBW_COL + 1 and fields[2] in _NCCL_DATA_TYPES:
            try:
                max_bw = max(max_bw, float(fields[_BUSBW_COL]))
            except (ValueError, IndexError):
                pass
    return max_bw


def parse_nccl_output(output: str) -> NcclResult:
    """Parse NCCL AllReduce benchmark output for bandwidth and data integrity.

    Extracts:
    - Average bus bandwidth (GB/s)
    - Maximum bus bandwidth from the data table (GB/s)
    - Out-of-bounds value count (data corruption indicator)

    Args:
        output: Raw stdout/stderr from an NCCL allreduce benchmark run.

    Returns:
        NcclResult with parsed metrics. ``success`` is False if bandwidth
        could not be parsed or data corruption was detected.
    """
    result = NcclResult(success=True, output=output)

    avg_match = _RE_AVG_BUS_BW.search(output)
    if avg_match:
        result.avg_bus_bw_gbps = float(avg_match.group(1))

    result.max_bus_bw_gbps = _parse_max_bus_bw(output)

    oob_match = _RE_OUT_OF_BOUNDS.search(output)
    if oob_match:
        result.out_of_bounds = int(oob_match.group(1))
        if result.out_of_bounds > 0:
            result.success = False
            result.error = f"Data corruption detected: {result.out_of_bounds} out of bounds values"

    if result.avg_bus_bw_gbps == 0 and result.max_bus_bw_gbps == 0:
        result.success = False
        result.error = result.error or "Could not parse bandwidth results from NCCL output"

    return result
