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

"""Auto-apply unit marker to all tests in tests/ directory."""

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Register the unit marker to avoid warnings."""
    config.addinivalue_line("markers", "unit: Unit tests for library code")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Automatically add 'unit' marker to all tests in tests/ directory.

    Only matches isvtest/tests/, not isvtest/src/isvtest/tests/
    """
    for item in items:
        path_str = str(item.fspath)
        # Only mark tests that are in isvtest/tests/ (not in src/isvtest/tests/)
        if "/tests/" in path_str and "/src/isvtest/tests/" not in path_str:
            item.add_marker(pytest.mark.unit)
