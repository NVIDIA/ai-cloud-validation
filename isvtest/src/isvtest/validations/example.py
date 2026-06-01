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

from typing import ClassVar

from isvtest.core.validation import UNMAPPED, BaseValidation


class ExampleCheck(BaseValidation):
    """Example check demonstrating the BaseValidation pattern."""

    description = "An example check that verifies echo works."
    test_ids: ClassVar[tuple[str, ...]] = (UNMAPPED,)
    catalog_exclude: ClassVar[bool] = True

    def run(self) -> None:
        result = self.run_command("echo 'hello world'")

        if result.exit_code != 0:
            self.set_failed(f"Command failed with exit code {result.exit_code}")
            return

        if "hello world" not in result.stdout:
            self.set_failed(f"Unexpected output: {result.stdout}")
            return

        self.set_passed("Echo command worked as expected")


class SecondExampleCheck(BaseValidation):
    """Second example check demonstrating the BaseValidation pattern."""

    description = "An example check that verifies echo works."
    test_ids: ClassVar[tuple[str, ...]] = (UNMAPPED,)
    catalog_exclude: ClassVar[bool] = True

    def run(self) -> None:
        result = self.run_command("echo 'another example'")

        if result.exit_code != 0:
            self.set_failed(f"Command failed with exit code {result.exit_code}")
            return

        if "another example" not in result.stdout:
            self.set_failed(f"Unexpected output: {result.stdout}")
            return

        self.set_passed("Echo command worked as expected")
