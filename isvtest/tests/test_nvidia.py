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

"""Tests for NVIDIA parsing helpers."""

from isvtest.core.nvidia import parse_cuda_version


class TestParseCudaVersion:
    """Tests for parse_cuda_version()."""

    def test_legacy_cuda_version_header(self) -> None:
        header = "| NVIDIA-SMI 550.54.15    Driver Version: 550.54.15    CUDA Version: 12.4     |"
        assert parse_cuda_version(header) == "12.4"

    def test_cuda_umd_version_header(self) -> None:
        header = "| NVIDIA-SMI 610.47    KMD Version: 610.47    CUDA UMD Version: 13.3     |"
        assert parse_cuda_version(header) == "13.3"

    def test_prefers_first_match_when_both_present(self) -> None:
        output = "CUDA Version: 12.4\nCUDA UMD Version: 13.3"
        assert parse_cuda_version(output) == "12.4"

    def test_returns_none_when_missing(self) -> None:
        assert parse_cuda_version("Driver Version: 550.54.15") is None
