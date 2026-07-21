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

from isvtest.catalog import (
    CATALOG_SCHEMA_VERSION,
    build_axis_taxonomy,
    build_catalog,
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


class TestAxisTaxonomy:
    """Tests for the platform/module axis taxonomy and the catalog envelope."""

    def test_derives_platform_and_module_axes_from_suites(self) -> None:
        """Platforms come from platform suites; modules from module suites."""
        platforms, modules = build_axis_taxonomy()
        assert platforms == ["bare_metal", "foundational", "kubernetes", "slurm", "vm"]
        assert modules == [
            "control_plane",
            "iam",
            "image_registry",
            "network",
            "observability",
            "security",
            "storage",
        ]

    def test_catalog_document_wraps_entries_with_metadata(self) -> None:
        """The envelope carries schema version, package version, and both axes."""
        entries = [{"name": "X", "labels": ["iam"]}]
        doc = catalog_document(entries, "1.2.3")
        assert doc["schemaVersion"] == CATALOG_SCHEMA_VERSION
        assert doc["isvTestVersion"] == "1.2.3"
        assert doc["entries"] == entries
        assert doc["platforms"] == ["bare_metal", "foundational", "kubernetes", "slurm", "vm"]
        assert "iam" in doc["modules"]
        # The label universe is intentionally not summarized at the top level.
        assert "labels" not in doc


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
            assert "test_ids" in entry
            assert "platforms" in entry
            assert "modules" in entry
            assert "module" not in entry
            assert "markers" not in entry

    def test_entries_have_correct_types(self) -> None:
        """Test that entry values have the correct types."""
        catalog = build_catalog()
        for entry in catalog:
            assert isinstance(entry["name"], str)
            assert isinstance(entry["description"], str)
            assert isinstance(entry["labels"], list)
            assert isinstance(entry["platforms"], list)
            assert isinstance(entry["modules"], list)

    def test_no_duplicate_names(self) -> None:
        """Test that there are no duplicate test names in the catalog."""
        catalog = build_catalog()
        names = [e["name"] for e in catalog]
        assert len(names) == len(set(names))

    def test_known_tests_present(self) -> None:
        """Test that some known validation tests appear in the catalog."""
        catalog = build_catalog()
        names = {e["name"] for e in catalog}
        assert "StepSuccessCheck-iam_teardown" in names
        assert "FieldExistsCheck-iam_setup" in names
        # Generic plumbing classes are wired only under variant names, so no
        # bare entry (it would carry no wiring of its own).
        assert "StepSuccessCheck" not in names
        assert "FieldExistsCheck" not in names

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

        # Per-wiring names: the bm and vm GpuCheck wirings are separate tests,
        # each carrying only its own suite's plan id.
        assert by_name["MfaEnforcedCheck"]["test_ids"] == ["SEC07-01"]
        assert by_name["GpuCheck-bm_gpu"]["test_ids"] == ["BMAAS08-01"]
        assert by_name["GpuCheck-vm_gpu"]["test_ids"] == ["VMAAS06-01"]

    def test_variant_test_ids_stay_on_the_variant(self) -> None:
        """A variant's wired test_id stays on the variant; no bare-base entry exists."""
        catalog = build_catalog(released_only=False)
        by_name = {e["name"]: e for e in catalog}

        assert by_name["StepSuccessCheck-delete_tenant"]["test_ids"] == ["CP10-01"]
        assert "StepSuccessCheck" not in by_name

    def test_released_only_filters_catalog(self) -> None:
        """Default catalog generation excludes tests not in the release manifest.

        The manifest lists validation classes; variant wirings of a released
        class are released with it (mirrors the runtime gating).
        """
        with patch("isvtest.catalog.load_released_test_filter", return_value={"StepSuccessCheck"}):
            catalog = build_catalog()

        names = {e["name"] for e in catalog}
        assert names
        assert all(name.startswith("StepSuccessCheck-") for name in names)
        assert "StepSuccessCheck-iam_teardown" in names

    def test_unreleased_env_includes_full_catalog(self) -> None:
        """When the release filter is disabled, default catalog generation includes all tests."""
        with patch("isvtest.catalog.load_released_test_filter", return_value=None):
            catalog = build_catalog()

        names = {e["name"] for e in catalog}
        assert "StepSuccessCheck-iam_teardown" in names
        assert "FieldExistsCheck-iam_setup" in names

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
            patch("isvtest.catalog._build_axis_maps", return_value=({}, {})),
            patch("isvtest.catalog._all_wired_names", return_value=set()),
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
                "platforms": [],
                "modules": [],
            }
        ]

    def test_module_suite_checks_use_modules_axis(self) -> None:
        """Checks wired in a module suite land on modules; their platforms come
        only from the declared ``platforms:`` field (foundational for iam /
        control_plane / image_registry / network)."""
        catalog = build_catalog(released_only=False)
        by_name = {e["name"]: e for e in catalog}
        entry = by_name["AccessKeyAuthenticatedCheck"]
        assert entry["modules"] == ["control_plane"]
        assert entry["platforms"] == ["foundational"]

    def test_foundational_module_checks_declare_foundational(self) -> None:
        """Module checks run once (foundational) or inside a platform session.

        Every module-suite check either carries platforms ["foundational"]
        (self-contained, once per lab) or positively declares the real
        column(s) whose platform session it runs inside (e.g. the storage
        suite's kubernetes CSI checks).
        """
        catalog = build_catalog(released_only=False)
        foundational_modules = {
            "iam",
            "control_plane",
            "image_registry",
            "network",
            "observability",
            "security",
            "storage",
        }
        session_columns = {"bare_metal", "kubernetes", "slurm", "vm"}
        for entry in catalog:
            if foundational_modules & set(entry["modules"]):
                platforms = set(entry["platforms"])
                assert platforms == {"foundational"} or platforms <= session_columns, (
                    f"{entry['name']}: expected ['foundational'] or a real-column "
                    f"session declaration, got {entry['platforms']}"
                )

    def test_module_suite_platforms_declaration_sets_platform_axis(self, tmp_path) -> None:
        """A module-suite check's platforms: declaration is its platform placement."""
        from isvtest.catalog import _build_axis_maps

        suite = tmp_path / "security.yaml"
        suite.write_text(
            """\
tests:
  module: security
  validations:
    capacity:
      checks:
        RestrictedCheck:
          labels: ["security"]
          platforms: ["vm", "bare_metal"]
        UnrestrictedCheck:
          labels: ["security"]
""",
            encoding="utf-8",
        )

        platform_map, module_map = _build_axis_maps(tmp_path)

        assert platform_map == {"RestrictedCheck": {"bare_metal", "vm"}}
        assert module_map == {"RestrictedCheck": {"security"}, "UnrestrictedCheck": {"security"}}

    def test_cap04_checks_are_foundational(self) -> None:
        """The self-contained CAP04 checks run once with the security module."""
        catalog = build_catalog(released_only=False)
        by_name = {e["name"]: e for e in catalog}
        for name in ("CapacityReservationGroupingCheck", "CapacityTopologyBlockAtomicAllocationCheck"):
            entry = by_name[name]
            assert entry["platforms"] == ["foundational"]
            assert entry["modules"] == ["security"]
            assert "bare_metal" not in entry["labels"]

    def test_platform_suite_checks_use_platforms_axis(self) -> None:
        """Checks wired in a platform suite land on platforms, not modules."""
        catalog = build_catalog(released_only=False)
        by_name = {e["name"]: e for e in catalog}
        assert by_name["GpuCheck-bm_gpu"]["platforms"] == ["bare_metal"]
        assert by_name["GpuCheck-bm_gpu"]["modules"] == []
        assert by_name["GpuCheck-vm_gpu"]["platforms"] == ["vm"]
        assert by_name["GpuCheck-vm_gpu"]["modules"] == []

    def test_suite_membership_overrides_label_axis_inference(self) -> None:
        """Regression: trait labels must not add extra axis ownership.

        A check can carry labels like ``("security", "network")`` for pytest
        filtering AND appear in a single module suite YAML (e.g.
        ``security.yaml``). Suite membership is the source of truth - otherwise
        the UI shows phantom axis badges.

        DO NOT add per-check asserts to this test. It is a property test
        that already covers every check in the catalog. If a new validation
        breaks the invariant, the failure message names it.
        """
        from isvtest.catalog import _build_axis_maps, _find_configs_dir

        configs_dir = _find_configs_dir()
        assert configs_dir is not None, "isvctl/configs/ not found"

        suite_platforms, suite_modules = _build_axis_maps()
        platform_axis, module_axis = build_axis_taxonomy()
        platform_axis_set = set(platform_axis)
        module_axis_set = set(module_axis)

        for entry in build_catalog(released_only=False):
            name = entry["name"]
            in_suite = name in suite_platforms or name in suite_modules
            if not in_suite:
                continue
            expected_platforms = sorted(suite_platforms.get(name, set()))
            expected_modules = sorted(suite_modules.get(name, set()))
            label_platforms = sorted({label for label in entry["labels"] if label in platform_axis_set})
            label_modules = sorted({label for label in entry["labels"] if label in module_axis_set})
            phantom_platforms = sorted(set(label_platforms) - set(expected_platforms) & set(entry["platforms"]))
            phantom_modules = sorted(set(label_modules) - set(expected_modules) & set(entry["modules"]))
            assert not phantom_platforms, (
                f"{name}: label-derived platforms {phantom_platforms} leaked into catalog; "
                f"expected exactly {expected_platforms}, got {entry['platforms']}"
            )
            assert not phantom_modules, (
                f"{name}: label-derived modules {phantom_modules} leaked into catalog; "
                f"expected exactly {expected_modules}, got {entry['modules']}"
            )
            assert entry["platforms"] == expected_platforms, (
                f"{name}: platforms should equal suite assignment {expected_platforms}, got {entry['platforms']}"
            )
            assert entry["modules"] == expected_modules, (
                f"{name}: modules should equal suite assignment {expected_modules}, got {entry['modules']}"
            )

    def test_observability_label_infers_module_for_unlisted_checks(self) -> None:
        """Checks labelled with `observability` are tagged on modules when not in any suite."""

        class ObservabilityLabelledCheck(BaseValidation):
            description = "Observability check labelled but not in any suite"

            def run(self) -> None:
                self.set_passed()

        ObservabilityLabelledCheck.__module__ = "isvtest.validations.fake"

        with (
            patch("isvtest.catalog.discover_all_tests", return_value=[ObservabilityLabelledCheck]),
            patch("isvtest.catalog._build_axis_maps", return_value=({}, {})),
            patch("isvtest.catalog._all_wired_names", return_value=set()),
            patch(
                "isvtest.catalog.build_label_map",
                return_value={"ObservabilityLabelledCheck": {"observability"}},
            ),
            patch("isvtest.catalog.build_test_id_map", return_value={}),
            patch("isvtest.catalog.load_released_test_filter", return_value=None),
        ):
            catalog = build_catalog()

        assert catalog == [
            {
                "name": "ObservabilityLabelledCheck",
                "description": "Observability check labelled but not in any suite",
                "labels": ["observability"],
                "test_ids": [],
                "platforms": [],
                "modules": ["observability"],
            }
        ]

    def test_platform_labels_do_not_infer_platform_axis(self) -> None:
        """Platform-axis labels on an unwired check imply no platform placement.

        Platform placement comes only from platform-suite placement or a
        declared ``platforms:`` field, never from labels.
        """

        class BareMetalLabelledCheck(BaseValidation):
            description = "Check labelled bare_metal but not in any suite"

            def run(self) -> None:
                self.set_passed()

        BareMetalLabelledCheck.__module__ = "isvtest.validations.fake"

        with (
            patch("isvtest.catalog.discover_all_tests", return_value=[BareMetalLabelledCheck]),
            patch("isvtest.catalog._build_axis_maps", return_value=({}, {})),
            patch("isvtest.catalog._all_wired_names", return_value=set()),
            patch(
                "isvtest.catalog.build_label_map",
                return_value={"BareMetalLabelledCheck": {"bare_metal", "gpu"}},
            ),
            patch("isvtest.catalog.build_test_id_map", return_value={}),
            patch("isvtest.catalog.load_released_test_filter", return_value=None),
        ):
            catalog = build_catalog()

        (entry,) = catalog
        assert entry["platforms"] == []
        assert entry["modules"] == []


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
