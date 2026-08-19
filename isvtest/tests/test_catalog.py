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

from unittest.mock import patch

import pytest

from isvtest.catalog import (
    CATALOG_SCHEMA_VERSION,
    _assert_disjoint_vocabulary,
    build_capability_vocabulary,
    build_catalog,
    build_suite_vocabulary,
    catalog_document,
    get_catalog_version,
)
from isvtest.core.validation import BaseValidation


class ExplicitLabelCatalogCheck(BaseValidation):
    """Catalog fixture whose labels are supplied by the YAML wiring scan."""

    description = "Explicit labels"

    def run(self) -> None:
        """Mark the validation passed."""
        self.set_passed()


class TestCatalogDocument:
    """Tests for capability vocabulary and the versioned catalog envelope."""

    def test_derives_capabilities_from_platform_suites(self) -> None:
        """Only real platform suite keys are declarable capabilities."""
        assert build_capability_vocabulary() == ["bare_metal", "kubernetes", "slurm", "vm"]

    def test_derives_suite_vocabulary_from_plain_suites(self) -> None:
        """Plain suite YAML files are listed separately from platform suites."""
        suites = build_suite_vocabulary()
        assert "iam" in suites
        assert "network_operator" in suites
        assert "network_operator_use_cases" in suites
        assert "storage" in suites
        assert "kubernetes" not in suites
        assert "vm" not in suites

    def test_catalog_document_wraps_entries_with_metadata(self) -> None:
        """The envelope carries schema version, package version, and axis lists."""
        entries = [{"name": "X", "labels": ["iam"]}]
        doc = catalog_document(entries, "1.2.3")
        assert doc["schemaVersion"] == CATALOG_SCHEMA_VERSION
        assert doc["isvTestVersion"] == "1.2.3"
        assert doc["entries"] == entries
        assert doc["capabilities"] == build_capability_vocabulary()
        assert doc["suites"] == build_suite_vocabulary()
        # The axis is named `capabilities`; the former `platforms` spelling is gone.
        assert "platforms" not in doc
        # The label universe is intentionally not summarized at the top level.
        assert "labels" not in doc

    def test_disjoint_vocabulary_accepts_distinct_namespaces(self) -> None:
        """Plain suite names that are not capability words pass the guard."""
        _assert_disjoint_vocabulary(["vm", "kubernetes"], ["storage", "iam", "network"])

    def test_disjoint_vocabulary_rejects_suite_named_after_capability(self) -> None:
        """A plain suite named after any declarable capability is a namespace collision."""
        with pytest.raises(ValueError, match="kubernetes"):
            _assert_disjoint_vocabulary(["vm", "kubernetes"], ["storage", "kubernetes"])

    def test_disjoint_vocabulary_rejects_undeclared_capability_word(self) -> None:
        """Collision is checked against the full reserved set, not just declared platforms."""
        with pytest.raises(ValueError, match="slurm"):
            _assert_disjoint_vocabulary(["vm"], ["slurm"])


class TestBuildCatalog:
    """Tests for build_catalog function."""

    def test_entries_have_suite_contract(self) -> None:
        """Catalog rows expose suite placement and requirement metadata."""
        catalog = build_catalog(released_only=False)
        names = [entry["name"] for entry in catalog]
        assert catalog
        assert len(names) == len(set(names))
        for entry in catalog:
            assert set(entry) == {
                "name",
                "description",
                "labels",
                "test_ids",
                "source",
                "suite",
                "capability",
                "requires",
            }
            assert isinstance(entry["source"], str)
            assert isinstance(entry["requires"], list)
            if entry["capability"]:
                assert entry["requires"] == []
        assert "EastWestNetworkRoceSriovCheck" in names

    def test_extract_checks_supports_direct_dict_category_form(self, tmp_path) -> None:
        """Direct dict category wiring is included in catalog config scans."""
        from isvtest.catalog import _extract_checks_from_config

        config = tmp_path / "direct-dict.yaml"
        config.write_text(
            """\
tests:
  validations:
    direct:
      DirectCheck:
        labels: ["network"]
      EmptyParamsCheck: {}
""",
            encoding="utf-8",
        )

        assert _extract_checks_from_config(config) == ["DirectCheck", "EmptyParamsCheck"]

    def test_extract_check_test_ids_excludes_na_and_blanks(self, tmp_path) -> None:
        """Wiring test_ids are extracted per check, with "N/A"/empty dropped."""
        from isvtest.catalog import _extract_check_test_ids_from_config

        config = tmp_path / "test-ids.yaml"
        config.write_text(
            """\
tests:
  validations:
    sample:
      checks:
        MappedCheck:
          test_id: "SEC07-01"
        GapCheck:
          test_id: "N/A"
        BlankCheck:
          test_id: ""
        NoIdCheck: {}
""",
            encoding="utf-8",
        )

        assert _extract_check_test_ids_from_config(config) == {"MappedCheck": {"SEC07-01"}}

    def test_entries_expose_wired_test_ids(self) -> None:
        """Catalog entries carry the plan ids declared on their wiring."""
        catalog = build_catalog(released_only=False)
        by_name = {e["name"]: e for e in catalog}

        # Every entry has a list-of-strings test_ids and never the "N/A" sentinel.
        for entry in catalog:
            assert isinstance(entry["test_ids"], list)
            assert all(isinstance(tid, str) for tid in entry["test_ids"])
            assert "N/A" not in entry["test_ids"]

        # Single mappings retain their requirement and suite placement.
        assert by_name["MfaEnforcedCheck"]["test_ids"] == ["SEC07-01"]
        assert by_name["MfaEnforcedCheck"]["suite"] == "security"
        assert by_name["MfaEnforcedCheck"]["requires"] == []

    def test_released_only_filters_catalog(self) -> None:
        """Default catalog generation excludes tests not in the release manifest."""
        with patch("isvtest.catalog.load_released_test_filter", return_value={"MfaEnforcedCheck"}):
            catalog = build_catalog()

        assert catalog
        assert all(entry["name"].startswith("MfaEnforcedCheck") for entry in catalog)

    def test_unreleased_env_includes_full_catalog(self) -> None:
        """When the release filter is disabled, default catalog generation includes all tests.

        Composites are the unreleased entries in practice: they are added to the
        release manifest by a release commit, not by the PR that wires them.
        """
        with patch("isvtest.catalog.load_released_test_filter", return_value=None):
            catalog = build_catalog()

        names = {e["name"] for e in catalog}
        assert "MfaEnforcedCheck" in names
        assert "VolumeDeletedCheck" in names

    def test_labels_are_lists_of_strings(self) -> None:
        """Test that labels are lists of strings."""
        catalog = build_catalog()
        for entry in catalog:
            for label in entry["labels"]:
                assert isinstance(label, str)

    def test_catalog_emits_explicit_labels(self) -> None:
        """Per-wiring YAML labels are surfaced as catalog tag metadata."""
        with (
            patch("isvtest.catalog.discover_all_tests", return_value=[ExplicitLabelCatalogCheck]),
            patch(
                "isvtest.catalog._build_suite_map",
                return_value={
                    "ExplicitLabelCatalogCheck": {
                        "suite": "demo",
                        "capability": None,
                        "requires": ["vm", "bare_metal"],
                        "composite": False,
                        "description": "",
                    }
                },
            ),
            patch(
                "isvtest.catalog.build_label_map",
                return_value={"ExplicitLabelCatalogCheck": {"accelerator", "long_running"}},
            ),
            patch("isvtest.catalog.build_test_id_map", return_value={}),
            patch("isvtest.catalog.load_released_test_filter", return_value=None),
        ):
            catalog = build_catalog()

        assert catalog == [
            {
                "name": "ExplicitLabelCatalogCheck",
                "description": "Explicit labels",
                "labels": ["accelerator", "long_running"],
                "test_ids": [],
                "source": __name__,
                "suite": "demo",
                "capability": None,
                "requires": ["vm", "bare_metal"],
            }
        ]

    def test_composite_entry_describes_itself(self) -> None:
        """A composite has no class, so its description comes from the wiring."""
        with (
            patch("isvtest.catalog.discover_all_tests", return_value=[ExplicitLabelCatalogCheck]),
            patch(
                "isvtest.catalog._build_suite_map",
                return_value={
                    "DemoComposedCheck": {
                        "suite": "demo",
                        "capability": None,
                        "requires": [],
                        "composite": True,
                        "description": "Check the demo thing works",
                    }
                },
            ),
            patch("isvtest.catalog.build_label_map", return_value={"DemoComposedCheck": {"demo"}}),
            patch("isvtest.catalog.build_test_id_map", return_value={"DemoComposedCheck": {"SEC07-01"}}),
            patch("isvtest.catalog.load_released_test_filter", return_value=None),
        ):
            catalog = build_catalog()

        assert catalog == [
            {
                "name": "DemoComposedCheck",
                "description": "Check the demo thing works",
                "labels": ["demo"],
                "test_ids": ["SEC07-01"],
                "source": "isvtest.core.composite",
                "suite": "demo",
                "capability": None,
                "requires": [],
            }
        ]

    def test_composite_is_release_gated_by_name(self) -> None:
        """A composite name is not in the manifest, so it ships unreleased."""
        with (
            patch("isvtest.catalog.discover_all_tests", return_value=[ExplicitLabelCatalogCheck]),
            patch(
                "isvtest.catalog._build_suite_map",
                return_value={
                    "DemoComposedCheck": {
                        "suite": "demo",
                        "capability": None,
                        "requires": [],
                        "composite": True,
                        "description": "Check the demo thing works",
                    }
                },
            ),
            patch("isvtest.catalog.build_label_map", return_value={}),
            patch("isvtest.catalog.build_test_id_map", return_value={}),
            patch("isvtest.catalog.load_released_test_filter", return_value={"StepSuccessCheck"}),
        ):
            assert build_catalog() == []

    def test_sources_are_valid_python_paths(self) -> None:
        """Source paths remain useful implementation metadata, not a suite axis."""
        catalog = build_catalog()
        for entry in catalog:
            assert "." in entry["source"]
            assert entry["source"].startswith("isvtest.")


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
