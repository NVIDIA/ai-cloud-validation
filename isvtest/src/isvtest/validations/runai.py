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

"""Run:AI compatibility kit validations.

Assertions over the JSON contract emitted by the shared
``run_compatibility_kit.py`` step script, which runs the Run:AI compatibility
kit container against the target Kubernetes cluster.
"""

from typing import ClassVar

from isvtest.core.validation import BaseValidation, check_required_tests


class RunAICompatibilityCheck(BaseValidation):
    """Validate the Run:AI compatibility kit suite passed on the cluster.

    Config:
        step_output: Output of the run_compatibility_kit step

    Step output:
        tests.compatibility.passed: Overall kit verdict (no failed/broken tests)
        tests.compatibility.message: Pass/fail/skip counts summary
        tests.compatibility.error: Failure detail incl. failed test names
    """

    description: ClassVar[str] = "Check the Run:AI compatibility kit test suite passed on the cluster"

    def run(self) -> None:
        if not check_required_tests(self, ["compatibility"], "Run:AI compatibility failed"):
            return
        compatibility = self.config.get("step_output", {}).get("tests", {}).get("compatibility", {})
        self.set_passed(compatibility.get("message") or "Run:AI compatibility passed")
