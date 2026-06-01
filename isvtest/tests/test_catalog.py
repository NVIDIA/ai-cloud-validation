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

"""Tests for the catalog module."""

from typing import ClassVar
from unittest.mock import patch

from isvtest.catalog import build_catalog, get_catalog_version
from isvtest.core.validation import BaseValidation


class ExplicitLabelCatalogCheck(BaseValidation):
    """Catalog fixture with explicit labels."""

    description = "Explicit labels"
    labels: ClassVar[tuple[str, ...]] = ("accelerator", "long_running")

    def run(self) -> None:
        """Mark the validation passed."""
        self.set_passed()


class TestBuildCatalog:
    """Tests for build_catalog function."""

    def test_returns_list_of_dicts(self) -> None:
        """Test that build_catalog returns a list of dicts."""
        catalog = build_catalog()
        assert isinstance(catalog, list)
        assert len(catalog) > 0
        for entry in catalog:
            assert isinstance(entry, dict)

    def test_entries_have_required_keys(self) -> None:
        """Test that each entry has the required keys."""
        catalog = build_catalog()
        for entry in catalog:
            assert "name" in entry
            assert "description" in entry
            assert "labels" in entry
            assert "module" in entry
            assert "test_ids" in entry
            assert "markers" not in entry

    def test_entries_have_correct_types(self) -> None:
        """Test that entry values have the correct types."""
        catalog = build_catalog()
        for entry in catalog:
            assert isinstance(entry["name"], str)
            assert isinstance(entry["description"], str)
            assert isinstance(entry["labels"], list)
            assert isinstance(entry["module"], str)
            assert isinstance(entry["test_ids"], list)

    def test_no_duplicate_names(self) -> None:
        """Test that there are no duplicate test names in the catalog."""
        catalog = build_catalog()
        names = [e["name"] for e in catalog]
        assert len(names) == len(set(names))

    def test_known_tests_present(self) -> None:
        """Test that some known validation tests appear in the catalog."""
        catalog = build_catalog()
        names = {e["name"] for e in catalog}
        assert "StepSuccessCheck" in names
        assert "FieldExistsCheck" in names

    def test_released_only_filters_catalog(self) -> None:
        """Default catalog generation excludes tests not in the release manifest."""
        with patch("isvtest.catalog.load_released_test_filter", return_value={"StepSuccessCheck"}):
            catalog = build_catalog()

        assert {e["name"] for e in catalog} == {"StepSuccessCheck"}

    def test_unreleased_env_includes_full_catalog(self) -> None:
        """When the release filter is disabled, default catalog generation includes all tests."""
        with patch("isvtest.catalog.load_released_test_filter", return_value=None):
            catalog = build_catalog()

        names = {e["name"] for e in catalog}
        assert "StepSuccessCheck" in names
        assert "FieldExistsCheck" in names

    def test_labels_are_lists_of_strings(self) -> None:
        """Test that labels are lists of strings."""
        catalog = build_catalog()
        for entry in catalog:
            for label in entry["labels"]:
                assert isinstance(label, str)

    def test_catalog_emits_explicit_labels(self) -> None:
        """Explicit class labels are the only source of catalog tag metadata."""
        with (
            patch("isvtest.catalog.discover_all_tests", return_value=[ExplicitLabelCatalogCheck]),
            patch("isvtest.catalog._build_platform_map", return_value={}),
            patch("isvtest.catalog.load_released_test_filter", return_value=None),
        ):
            catalog = build_catalog()

        assert catalog == [
            {
                "name": "ExplicitLabelCatalogCheck",
                "description": "Explicit labels",
                "labels": ["accelerator", "long_running"],
                "module": __name__,
                "platforms": [],
                "test_ids": [],
            }
        ]

    def test_modules_are_valid_python_paths(self) -> None:
        """Test that module paths look like valid Python module paths."""
        catalog = build_catalog()
        for entry in catalog:
            assert "." in entry["module"]
            assert entry["module"].startswith("isvtest.")

    def test_suite_membership_overrides_label_platforms(self) -> None:
        """Regression: trait labels must not add extra platform ownership.

        A check can carry labels like ``("security", "network")`` for pytest
        filtering AND appear in a single suite YAML (e.g. ``security.yaml``).
        ``_build_platform_map`` must use the suite as the source of truth and
        skip label-derived platform inference in that case - otherwise the
        UI shows phantom platform badges.

        DO NOT add per-check asserts to this test. It is a property test
        that already covers every check in the catalog. If a new validation
        breaks the invariant, the failure message names it.
        """
        from isvtest.catalog import (
            LABEL_TO_PLATFORM,
            PLATFORM_CONFIGS,
            _extract_checks_from_config,
            _find_configs_dir,
        )

        configs_dir = _find_configs_dir()
        assert configs_dir is not None, "isvctl/configs/ not found"

        suite_platforms: dict[str, set[str]] = {}
        for platform, files in PLATFORM_CONFIGS.items():
            for relpath in files:
                for name in _extract_checks_from_config(configs_dir / relpath):
                    suite_platforms.setdefault(name, set()).add(platform)

        for entry in build_catalog(released_only=False):
            name = entry["name"]
            if name not in suite_platforms:
                continue
            label_platforms = {LABEL_TO_PLATFORM[label] for label in entry["labels"] if label in LABEL_TO_PLATFORM}
            expected = suite_platforms[name]
            actual = set(entry["platforms"])
            phantom = (label_platforms - expected) & actual
            assert not phantom, (
                f"{name}: label-derived platforms {sorted(phantom)} leaked "
                f"into catalog; expected exactly {sorted(expected)}, "
                f"got {sorted(actual)}"
            )
            assert actual == expected, (
                f"{name}: platforms should equal suite assignment {sorted(expected)}, got {sorted(actual)}"
            )

    def test_observability_label_infers_platform_for_unlisted_checks(self) -> None:
        """Checks labelled with `observability` are tagged OBSERVABILITY when not in any suite."""

        class ObservabilityLabelledCheck(BaseValidation):
            description = "Observability check labelled but not in any suite"
            labels: ClassVar[tuple[str, ...]] = ("observability",)

            def run(self) -> None:
                self.set_passed()

        ObservabilityLabelledCheck.__module__ = "isvtest.validations.fake"

        with (
            patch("isvtest.catalog.discover_all_tests", return_value=[ObservabilityLabelledCheck]),
            patch("isvtest.catalog._build_platform_map", return_value={}),
            patch("isvtest.catalog.load_released_test_filter", return_value=None),
        ):
            catalog = build_catalog()

        assert catalog == [
            {
                "name": "ObservabilityLabelledCheck",
                "description": "Observability check labelled but not in any suite",
                "labels": ["observability"],
                "module": "isvtest.validations.fake",
                "platforms": ["OBSERVABILITY"],
                "test_ids": [],
            }
        ]


class TestGetCatalogVersion:
    """Tests for get_catalog_version function."""

    def test_returns_string(self) -> None:
        """Test that get_catalog_version returns a string."""
        version = get_catalog_version()
        assert isinstance(version, str)
        assert len(version) > 0

    def test_returns_dev_when_not_installed(self) -> None:
        """Test that 'dev' is returned when package is not installed."""
        from importlib.metadata import PackageNotFoundError

        with patch(
            "isvreporter.version.version",
            side_effect=PackageNotFoundError("isvtest"),
        ):
            version = get_catalog_version()
            assert version == "dev"
