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

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from isvtest.catalog import (
    CATALOG_SCHEMA_VERSION,
    _assert_disjoint_vocabulary,
    build_capability_vocabulary,
    build_catalog,
    build_suite_vocabulary,
    catalog_digest,
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
        catalog = build_catalog()
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
        catalog = build_catalog()
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

    def test_catalog_always_includes_the_complete_checkout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Obsolete release-gating environment values cannot hide wired tests."""
        monkeypatch.setenv("ISVTEST_INCLUDE_UNRELEASED", "0")
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
            assert get_catalog_version() == "dev"

    def test_the_checkout_never_changes_the_catalog_version(self) -> None:
        """The catalog version is the release number, drift or no drift.

        Whether the build has moved past that release is a separate fact, and
        it is settled by :func:`catalog_digest` comparing the checks this build
        holds against the ones the release published - not by decorating the
        version string, which every consumer is entitled to read plainly.
        """
        from isvreporter.version import describe_checkout

        describe_checkout.cache_clear()
        try:
            with patch("isvreporter.version._repository_root", return_value=Path("/repo")):
                with patch(
                    "subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        args=[], returncode=0, stdout="v0.9.0-3-g08339c7\n", stderr=""
                    ),
                ):
                    with patch("isvreporter.version.version", return_value="0.9.0"):
                        assert get_catalog_version() == "0.9.0"
        finally:
            describe_checkout.cache_clear()


class TestCatalogDocumentDigest:
    """The envelope carries the run's complete catalog identity."""

    def test_the_document_carries_the_digest_of_its_own_entries(self) -> None:
        """So the saved artifact shows what the service will compare against.

        Diagnosing a run called off-release means reading the digest somewhere;
        computing it only in passing on the way to the upload left the operator
        with nothing on disk to look at.
        """
        entries = [{"name": "GpuCheck"}]
        document = catalog_document(entries, "1.2.3", isv_test_build_ref="v1.2.3-0-gabc1234")
        assert document["catalogDigest"] == catalog_digest(document)
        assert document["isvTestBuildRef"] == "v1.2.3-0-gabc1234"

    def test_the_document_digest_is_what_the_reporter_sends(self) -> None:
        """One number, read from one place, rather than two that ought to agree."""
        from isvctl.reporting import _catalog_digest_of

        document = catalog_document([{"name": "GpuCheck"}], "1.2.3")
        assert _catalog_digest_of(document) == document["catalogDigest"]

    def test_a_document_without_a_digest_reports_none(self) -> None:
        """An older artifact without a recorded digest is unverified."""
        from isvctl.reporting import _catalog_digest_of

        assert _catalog_digest_of({"entries": []}) is None
        assert _catalog_digest_of(None) is None


class TestCatalogDigest:
    """Canonical hashing of the complete public catalog contract."""

    def _document(self) -> dict[str, object]:
        """Return a representative catalog contract."""
        return {
            "schemaVersion": 2,
            "isvTestVersion": "1.2.3",
            "isvTestBuildRef": "v1.2.3-0-gabc1234",
            "capabilities": ["vm", "kubernetes"],
            "suites": ["storage"],
            "entries": [
                {
                    "name": "GpuCheck",
                    "description": "Checks GPUs",
                    "labels": ["gpu", "fast"],
                    "source": "isvtest.validations.gpu",
                    "suite": "storage",
                    "capability": None,
                    "requires": ["vm", "bare_metal"],
                    "test_ids": ["GPU01-01"],
                }
            ],
        }

    def test_set_and_entry_order_do_not_matter(self) -> None:
        """Discovery and set-like list order are not part of catalog identity."""
        one = self._document()
        other = self._document()
        other["capabilities"] = ["kubernetes", "vm", "vm"]
        entry = dict(one["entries"][0])  # type: ignore[index]
        entry["labels"] = ["fast", "gpu", "gpu"]
        entry["requires"] = ["bare_metal", "vm"]
        second = {
            "name": "CpuCheck",
            "description": "Checks CPUs",
            "labels": ["cpu"],
            "source": "isvtest.validations.cpu",
            "suite": "storage",
            "capability": None,
            "requires": [],
            "test_ids": ["CPU01-01"],
        }
        one["entries"].append(second)  # type: ignore[union-attr]
        other["entries"] = [second, entry]
        assert catalog_digest(one) == catalog_digest(other)

    @pytest.mark.parametrize(
        ("field", "replacement"),
        [
            ("schemaVersion", 3),
            ("capabilities", ["kubernetes"]),
            ("suites", ["storage", "network"]),
        ],
    )
    def test_every_public_envelope_field_changes_the_digest(self, field: str, replacement: object) -> None:
        """Schema and both catalog vocabularies belong to compatibility."""
        before = self._document()
        after = self._document()
        after[field] = replacement
        assert catalog_digest(before) != catalog_digest(after)

    def test_repeated_generation_is_stable(self) -> None:
        """The same contract always produces the same identity."""
        document = self._document()
        assert catalog_digest(document) == catalog_digest(document)

    @pytest.mark.parametrize(
        ("field", "replacement"),
        [
            ("name", "RenamedCheck"),
            ("description", "New description"),
            ("labels", ["slow"]),
            ("capability", "kubernetes"),
            ("suite", "network"),
            ("requires", ["kubernetes"]),
            ("test_ids", ["GPU02-01"]),
        ],
    )
    def test_every_public_entry_field_changes_the_digest(self, field: str, replacement: object) -> None:
        """Every public entry field belongs to catalog compatibility."""
        before = self._document()
        after = self._document()
        after["entries"][0][field] = replacement  # type: ignore[index]
        assert catalog_digest(before) != catalog_digest(after)

    def test_excluded_provenance_fields_do_not_change_the_digest(self) -> None:
        """Version, source ref, digest, and local module paths are not contract fields."""
        one = self._document()
        two = self._document()
        two["isvTestVersion"] = "9.9.9"
        two["isvTestBuildRef"] = "v9.9.9-0-gfffffff"
        two["catalogDigest"] = "sha256:" + "0" * 64
        two["entries"][0]["source"] = "somewhere.else"  # type: ignore[index]
        two["entries"][0]["introducedInVersion"] = "0.1.0"  # type: ignore[index]
        assert catalog_digest(one) == catalog_digest(two)

    def test_is_the_shape_the_service_column_holds(self) -> None:
        digest = catalog_digest(self._document())
        assert digest.startswith("sha256:")
        assert len(digest) == 71
