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

"""Pytest driver for ReFrame tests configured in YAML."""

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from isvtest.config.loader import ConfigLoader
from isvtest.core.discovery import discover_reframe_tests


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Generate tests for ReFrame test classes configured in YAML."""
    if "reframe_test_class" not in metafunc.fixturenames:
        return

    # Discover ReFrame tests in validations/ and workloads/
    validations_dir = Path(__file__).parent.parent / "validations"
    workloads_dir = Path(__file__).parent.parent / "workloads"

    test_classes = []
    if validations_dir.exists():
        test_classes.extend(list(discover_reframe_tests(validations_dir, "isvtest.validations")))
    if workloads_dir.exists():
        test_classes.extend(list(discover_reframe_tests(workloads_dir, "isvtest.workloads")))

    if not test_classes:
        return

    # Load configuration if available
    enabled_reframe_tests = {}
    filtering_enabled = False
    show_skipped = False

    try:
        config_file_arg = metafunc.config.getoption("--config", default=None)

        if config_file_arg:
            filtering_enabled = True
            loader = ConfigLoader()
            cluster_config = loader.load_cluster_config(config_file=config_file_arg)

            # Get reframe tests from config
            validations = cluster_config.get("validations", {}) or {}
            reframe_section = validations.get("reframe", []) or []

            # Parse reframe section (list of dicts with test names as keys)
            for item in reframe_section:
                if isinstance(item, dict):
                    for test_name, test_config in item.items():
                        enabled_reframe_tests[test_name] = test_config or {}

            # Check if we should show skipped tests
            show_skipped = cluster_config.get("settings", {}).get("show_skipped_tests", False)

    except (ImportError, FileNotFoundError, ValueError, AttributeError, OSError):
        pass

    # Create parameters
    params = []
    ids = []

    for cls in test_classes:
        test_is_enabled = cls.__name__ in enabled_reframe_tests

        # If filtering is enabled and test not in config
        if filtering_enabled and not test_is_enabled:
            if not show_skipped:
                # Don't collect this test at all
                continue
            # Otherwise, collect it but mark as skipped

        # Get config for this test (empty if not filtering or not enabled)
        test_config = enabled_reframe_tests.get(cls.__name__, {})

        # Get module path for the test
        module_file = sys.modules[cls.__module__].__file__
        if module_file:
            module_path = Path(module_file)
        else:
            continue

        # Create param with skip marker if needed
        param_value = (cls, module_path, test_config)
        if filtering_enabled and not test_is_enabled and show_skipped:
            # Mark as skipped
            params.append(pytest.param(*param_value, marks=pytest.mark.skip(reason="Not configured in config YAML")))
        else:
            params.append(param_value)

        ids.append(cls.__name__)

    # Parametrize the test function
    if params:
        metafunc.parametrize("reframe_test_class,module_path,test_config", params, ids=ids)
    else:
        # If no ReFrame tests are configured/found, we must still parametrize arguments
        # to avoid "fixture not found" errors. We use a specific ID that will be
        # filtered out by pytest_collection_modifyitems in conftest.py.
        metafunc.parametrize(
            "reframe_test_class,module_path,test_config",
            [(None, None, {})],
            ids=["NO_REFRAME_TESTS"],
        )


def test_reframe(reframe_test_class: type, module_path: Path, test_config: dict[str, Any]) -> None:
    """Run a ReFrame test using the reframe CLI.

    Args:
        reframe_test_class: The ReFrame test class
        module_path: Path to the module containing the test
        test_config: Configuration dict for the test (currently unused but available for future)
    """
    # Find reframe executable (should be in same venv as current Python)
    reframe_cmd = shutil.which("reframe")
    if not reframe_cmd:
        # Try in the same directory as the Python executable
        python_dir = Path(sys.executable).parent
        reframe_cmd = str(python_dir / "reframe")
        if not Path(reframe_cmd).exists():
            pytest.skip("reframe command not found in PATH or venv")

    # Run reframe with the specific test
    cmd = [
        reframe_cmd,
        "-c",
        str(module_path),
        "-R",  # Recursive search
        "-r",  # Run tests
        "-n",
        reframe_test_class.__name__,  # Run only this specific test
    ]

    # TODO: In the future, we could pass test_config as ReFrame variables
    # For now, ReFrame tests use their default configurations

    result = subprocess.run(cmd, capture_output=True, text=True)

    # Check if test passed
    assert result.returncode == 0, f"ReFrame test failed:\n{result.stdout}\n{result.stderr}"
