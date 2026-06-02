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

"""Bare metal CUDA validations."""

from typing import ClassVar

from isvtest.core.nvidia import compare_versions, parse_cuda_version
from isvtest.core.validation import BaseValidation


class BmCudaVersion(BaseValidation):
    """Verify CUDA version is reported by nvidia-smi."""

    description: ClassVar[str] = "Query CUDA version from nvidia-smi (driver-reported max CUDA version)"
    timeout: ClassVar[int] = 30
    labels: ClassVar[tuple[str, ...]] = ("bare_metal",)

    def run(self) -> None:
        result = self.run_command("nvidia-smi")

        if result.exit_code != 0:
            self.set_failed(f"nvidia-smi failed: {result.stderr}")
            return

        # Parse CUDA version using shared parser
        cuda_version = parse_cuda_version(result.stdout)

        if not cuda_version:
            self.set_failed("CUDA version not found in nvidia-smi output")
            return

        # Check against minimum version if configured
        min_version = self.config.get("min_version")
        if min_version:
            if not compare_versions(cuda_version, min_version):
                self.set_failed(f"CUDA version {cuda_version} is below minimum required {min_version}")
                return

        self.set_passed(f"CUDA version: {cuda_version}")
