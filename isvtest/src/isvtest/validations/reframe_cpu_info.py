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

"""CPU information validation check using ReFrame."""

from typing import Any, ClassVar

import reframe as rfm
import reframe.utility.sanity as sn
from reframe.core.builtins import performance_function, run_after, sanity_function


@rfm.simple_test
class CPUInfoCheck(rfm.RunOnlyRegressionTest):
    """Verify CPU information is available and readable."""

    descr = "CPU information check using lscpu"
    valid_systems: ClassVar[list[str]] = ["*"]
    valid_prog_environs: ClassVar[list[str]] = ["*"]
    executable = "/usr/bin/lscpu"

    @run_after("init")
    def set_tags(self) -> None:
        """Set test tags."""
        self.tags = {"system", "cpu", "basic"}

    @sanity_function
    def validate(self) -> Any:
        """Check that Architecture information is present in output."""
        return sn.assert_found(r"Architecture", self.stdout)

    @performance_function("CPU(s)")
    def cpu_nums(self) -> Any:
        """Extract number of CPUs from lscpu output.

        Returns:
            Number of CPUs reported by the system.
        """
        return sn.extractsingle(r"^CPU\(s\):\s+(\d+)", self.stdout, 1, int)
