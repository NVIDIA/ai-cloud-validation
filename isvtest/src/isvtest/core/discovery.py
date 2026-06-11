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

"""Test discovery utilities for validations and ReFrame tests."""

import importlib
import inspect
import logging
import pkgutil
from collections.abc import Generator
from pathlib import Path

import isvtest.validations as validations_pkg
import isvtest.workloads as workloads_pkg
from isvtest.core.validation import BaseValidation

logger = logging.getLogger(__name__)


def discover_tests(
    package_path: str | Path, package_name: str = "isvtest.validations"
) -> Generator[type[BaseValidation], None, None]:
    """Recursively discover BaseValidation subclasses in a package.

    Args:
        package_path: Path to the package directory
        package_name: Python package name (e.g., 'isvtest.validations')

    Yields:
        BaseValidation subclasses
    """
    path = str(package_path)

    for module_info in pkgutil.walk_packages([path], prefix=f"{package_name}."):
        try:
            module = importlib.import_module(module_info.name)

            for name, obj in inspect.getmembers(module):
                if (
                    inspect.isclass(obj)
                    and issubclass(obj, BaseValidation)
                    and obj is not BaseValidation
                    and not inspect.isabstract(obj)
                    and not obj.__dict__.get("_exclude_from_discovery", False)
                ):
                    yield obj

        except ImportError as e:
            logger.warning(f"Failed to import module {module_info.name}: {e}")


def discover_all_tests() -> Generator[type[BaseValidation], None, None]:
    """Discover all BaseValidation subclasses from validations and workloads packages.

    Yields:
        BaseValidation subclasses from both isvtest.validations and isvtest.workloads
    """
    yield from discover_tests(Path(validations_pkg.__file__).parent, "isvtest.validations")
    yield from discover_tests(Path(workloads_pkg.__file__).parent, "isvtest.workloads")


def discover_reframe_tests(
    package_path: str | Path, package_name: str = "isvtest.validations"
) -> Generator[type, None, None]:
    """Recursively discover ReFrame test classes in a package.

    Args:
        package_path: Path to the package directory
        package_name: Python package name (e.g., 'isvtest.validations')

    Yields:
        ReFrame test classes (classes decorated with @rfm.simple_test)
    """
    path = str(package_path)

    for module_info in pkgutil.walk_packages([path], prefix=f"{package_name}."):
        try:
            module = importlib.import_module(module_info.name)

            for name, obj in inspect.getmembers(module):
                if inspect.isclass(obj) and _is_reframe_test(obj):
                    yield obj

        except ImportError as e:
            logger.warning(f"Failed to import module {module_info.name}: {e}")


def _is_reframe_test(cls: type) -> bool:
    """Check if a class is a ReFrame test.

    ReFrame tests are marked with the @rfm.simple_test decorator,
    which adds the _rfm_regression_class_kind attribute to the class.

    Args:
        cls: Class to check

    Returns:
        True if the class is a ReFrame test
    """
    # ReFrame tests have the _rfm_regression_class_kind attribute
    return hasattr(cls, "_rfm_regression_class_kind")
