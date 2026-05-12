# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Tests for dynamic pytest validation generation."""

from typing import Any
from unittest.mock import patch

from isvtest.config.constants import RESOLVED_ENTRIES_FLAG
from isvtest.core.validation import BaseValidation
from isvtest.tests import test_validations as validation_tests


class ReleasedValidation(BaseValidation):
    """Released validation used by pytest generation tests."""

    def run(self) -> None:
        """Pass the test validation."""
        self.set_passed()


class UnreleasedValidation(BaseValidation):
    """Unreleased validation used by pytest generation tests."""

    def run(self) -> None:
        """Pass the test validation."""
        self.set_passed()


class FakeConfig:
    """Minimal pytest config object for generation tests."""

    def getoption(self, name: str, default: Any = None) -> Any:
        """Return configured pytest option values."""
        if name == "--config":
            return "config.yaml"
        if name == "--inventory":
            return None
        return default


class FakeMetafunc:
    """Minimal pytest metafunc object for generation tests."""

    def __init__(self) -> None:
        self.fixturenames = ["validation_class", "validation_config", "validation_name"]
        self.config = FakeConfig()
        self.argnames: str | None = None
        self.argvalues: list[Any] | None = None
        self.ids: list[str] | None = None

    def parametrize(self, argnames: str, argvalues: list[Any], ids: list[str]) -> None:
        """Capture parametrized arguments."""
        self.argnames = argnames
        self.argvalues = argvalues
        self.ids = ids


class FakeLoader:
    """Minimal ConfigLoader replacement for synthesized-name generation tests."""

    def load_cluster_config(self, config_file: str, inventory_path: str | None = None) -> dict[str, Any]:
        """Return a cluster config with validation filtering enabled."""
        return {
            "inventory": {"instance_id": "i-abc"},
            "settings": {},
            "validations": {"vm": {}},
        }

    def get_all_validations(
        self,
        config: dict[str, Any],
        categories: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Return transformed validations with one synthesized key and one unreleased check."""
        return {
            "ReleasedValidation-start_checks": {"expected": True, "_category": "start_checks"},
            "UnreleasedValidation": {},
        }


class LiteralVariantLoader(FakeLoader):
    """ConfigLoader replacement for literal configured variants."""

    def get_all_validations(
        self,
        config: dict[str, Any],
        categories: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Return a literal configured variant without internal category metadata."""
        return {"ReleasedValidation-experimental": {"expected": True}}


class ShowSkippedLiteralVariantLoader(LiteralVariantLoader):
    """ConfigLoader replacement that enables skipped-test reporting."""

    def load_cluster_config(self, config_file: str, inventory_path: str | None = None) -> dict[str, Any]:
        """Return a cluster config with show_skipped_tests enabled."""
        config = super().load_cluster_config(config_file=config_file, inventory_path=inventory_path)
        config["settings"] = {"show_skipped_tests": True}
        return config


class ResolvedEntriesLoader(FakeLoader):
    """ConfigLoader replacement for orchestrator-resolved temp configs."""

    def load_cluster_config(self, config_file: str, inventory_path: str | None = None) -> dict[str, Any]:
        """Return a config whose validation entries were resolved upstream."""
        config = super().load_cluster_config(config_file=config_file, inventory_path=inventory_path)
        config[RESOLVED_ENTRIES_FLAG] = True
        config["settings"] = {"show_skipped_tests": True}
        return config


def test_release_filter_allows_synthesized_key_when_base_is_released() -> None:
    """Synthesized duplicate keys are kept when their base class is released."""
    metafunc = FakeMetafunc()

    with (
        patch.object(validation_tests, "ConfigLoader", FakeLoader),
        patch.object(validation_tests, "discover_all_tests", return_value=[ReleasedValidation, UnreleasedValidation]),
        patch.object(validation_tests, "load_released_test_filter", return_value={"ReleasedValidation"}),
    ):
        validation_tests.pytest_generate_tests(metafunc)

    assert metafunc.ids == ["ReleasedValidation-start_checks"]


def test_unreleased_configured_variant_is_not_labeled_not_configured() -> None:
    """Configured variants skipped by release gating are not re-added as unconfigured skips."""
    metafunc = FakeMetafunc()

    with (
        patch.object(validation_tests, "ConfigLoader", ShowSkippedLiteralVariantLoader),
        patch.object(validation_tests, "discover_all_tests", return_value=[ReleasedValidation]),
        patch.object(validation_tests, "load_released_test_filter", return_value={"ReleasedValidation"}),
    ):
        validation_tests.pytest_generate_tests(metafunc)

    assert metafunc.ids == ["NO_VALIDATIONS"]


def test_resolved_entries_bypass_pytest_release_filter_and_show_skipped() -> None:
    """Resolved temp configs execute exactly the entries already selected upstream."""
    metafunc = FakeMetafunc()

    with (
        patch.object(validation_tests, "ConfigLoader", ResolvedEntriesLoader),
        patch.object(validation_tests, "discover_all_tests", return_value=[ReleasedValidation, UnreleasedValidation]),
        patch.object(validation_tests, "load_released_test_filter", return_value={"ReleasedValidation"}),
    ):
        validation_tests.pytest_generate_tests(metafunc)

    assert metafunc.ids == ["ReleasedValidation-start_checks", "UnreleasedValidation"]


def test_release_filter_allows_synthesized_key_when_full_key_is_released() -> None:
    """Synthesized duplicate keys are kept when their full key is released."""
    metafunc = FakeMetafunc()

    with (
        patch.object(validation_tests, "ConfigLoader", FakeLoader),
        patch.object(validation_tests, "discover_all_tests", return_value=[ReleasedValidation, UnreleasedValidation]),
        patch.object(validation_tests, "load_released_test_filter", return_value={"ReleasedValidation-start_checks"}),
    ):
        validation_tests.pytest_generate_tests(metafunc)

    assert metafunc.ids == ["ReleasedValidation-start_checks"]
    assert metafunc.argvalues is not None
    parameter_values = metafunc.argvalues[0].values
    assert parameter_values[0] is ReleasedValidation
    assert parameter_values[1] == {
        "expected": True,
        "_category": "start_checks",
        "inventory": {"instance_id": "i-abc"},
    }
    assert parameter_values[2] == "ReleasedValidation-start_checks"


def test_release_filter_skips_unreleased_literal_variant() -> None:
    """Literal configured variants must be released by their full key."""
    metafunc = FakeMetafunc()

    with (
        patch.object(validation_tests, "ConfigLoader", LiteralVariantLoader),
        patch.object(validation_tests, "discover_all_tests", return_value=[ReleasedValidation]),
        patch.object(validation_tests, "load_released_test_filter", return_value={"ReleasedValidation"}),
    ):
        validation_tests.pytest_generate_tests(metafunc)

    assert metafunc.ids == ["NO_VALIDATIONS"]
