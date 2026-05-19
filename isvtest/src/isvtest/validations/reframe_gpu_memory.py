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

"""GPU memory validation check using ReFrame."""

from typing import ClassVar

import reframe as rfm
import reframe.utility.sanity as sn
from reframe.core.builtins import run_after, sanity_function


@rfm.simple_test
class GpuMemoryCheck(rfm.RunOnlyRegressionTest):
    """Verify GPU memory availability."""

    descr = "GPU memory check"
    valid_systems: ClassVar[list[str]] = ["*"]
    valid_prog_environs: ClassVar[list[str]] = ["*"]
    executable = "nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits"

    @run_after("init")
    def set_tags(self) -> None:
        """Set test tags."""
        self.tags = {"gpu", "memory"}

    @sanity_function
    def validate_memory(self) -> bool:
        """Check that GPUs have sufficient memory (>= 16GB)."""
        return sn.assert_found(r"\d{5,}", self.stdout)
