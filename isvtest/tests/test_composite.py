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

"""Tests for composite checks."""

from typing import Any

import pytest

import isvtest.core.composite as composite_module
from isvtest.core.composite import CompositeCheck, composed_members, is_composite
from isvtest.core.discovery import discover_all_tests
from isvtest.core.resolution import parse_validations
from isvtest.core.validation import BaseValidation
from isvtest.validations.generic import (
    CrudOperationsCheck,
    FieldExistsCheck,
    FieldValueCheck,
    StepSuccessCheck,
)
from isvtest.validations.iam import IamCredentialAccessCheck


def _config(compose: Any, step_output: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    """Return composite wiring params as resolution would hand them over."""
    return {
        "compose": compose,
        "test_id": "IAM01-01",
        "labels": ["iam"],
        "description": "Check the user exists",
        "step_output": step_output if step_output is not None else {"success": True, "username": "u"},
        **extra,
    }


class TestComposeOnlyAtParseTime:
    """A compose_only check wired under its own name is a config error.

    ``validate_suite_wiring`` enforces this in-tree; an ISV's own config is
    never linted, so config parsing has to reject it too.
    """

    @staticmethod
    def _parse(checks: dict[str, Any]) -> list[Any]:
        return parse_validations({"setup_checks": {"step": "s", "checks": checks}})

    def test_rejects_a_directly_wired_compose_only_check(self) -> None:
        """The entry becomes an invalid-config error naming the fix."""
        entry = self._parse({"StepSuccessCheck": {}})[0]

        assert "compose_only" in entry.params_template["_invalid_config"]
        assert "compose:" in entry.params_template["_invalid_config"]

    def test_rejects_a_directly_wired_compose_only_variant(self) -> None:
        """A variant suffix must not bypass the compose-only restriction."""
        entry = self._parse({"StepSuccessCheck-teardown": {}})[0]

        assert "compose_only" in entry.params_template["_invalid_config"]
        assert "compose:" in entry.params_template["_invalid_config"]

    def test_allows_the_same_check_inside_a_compose_list(self) -> None:
        """Composing it is the supported way to reach a generic check."""
        entry = self._parse({"VmCreatedCheck": {"compose": ["StepSuccessCheck"]}})[0]

        assert entry.name == "VmCreatedCheck"
        assert "_invalid_config" not in entry.params_template

    def test_allows_a_purpose_built_class_wired_directly(self) -> None:
        """Only compose_only checks are restricted."""
        entry = self._parse({"InstanceStateCheck": {"expected_state": "running"}})[0]

        assert "_invalid_config" not in entry.params_template


class TestIsComposite:
    """Tests for recognising composite wiring."""

    def test_detects_compose_key(self) -> None:
        """Params carrying ``compose`` are a composite."""
        assert is_composite({"compose": ["StepSuccessCheck"]}) is True

    def test_plain_check_is_not_composite(self) -> None:
        """Ordinary check params are not a composite."""
        assert is_composite({"fields": ["username"]}) is False
        assert is_composite(None) is False

    def test_empty_compose_is_still_composite(self) -> None:
        """An empty ``compose`` is a composite that fails, not a plain check.

        Silently demoting it to a class lookup would report "validation not
        found" instead of the real authoring mistake.
        """
        assert is_composite({"compose": []}) is True


class TestComposedMembers:
    """Tests for parsing a ``compose`` list."""

    def test_accepts_bare_names_and_mappings(self) -> None:
        """Both member forms parse, with mappings carrying parameters."""
        members = composed_members(["StepSuccessCheck", {"FieldExistsCheck": {"fields": ["a"]}}])

        assert members == [("StepSuccessCheck", {}), ("FieldExistsCheck", {"fields": ["a"]})]

    def test_mapping_without_params_yields_empty_params(self) -> None:
        """``- CheckName:`` with no value parses as a member with no params."""
        assert composed_members([{"StepSuccessCheck": None}]) == [("StepSuccessCheck", {})]

    def test_non_list_yields_no_members(self) -> None:
        """A scalar ``compose`` contributes no members."""
        assert composed_members("StepSuccessCheck") == []


class TestCompositeCheck:
    """Tests for executing a composite."""

    def test_passes_when_every_member_passes(self) -> None:
        """A composite passes only once all members do, and reports each message."""
        result = CompositeCheck(
            config=_config(["StepSuccessCheck", {"FieldExistsCheck": {"fields": ["username"]}}])
        ).execute()

        assert result["passed"] is True
        assert "StepSuccessCheck" in result["output"]
        assert "FieldExistsCheck" in result["output"]

    def test_reports_each_member_as_a_subtest(self) -> None:
        """Members surface individually so a report shows which part ran."""
        composite = CompositeCheck(config=_config(["StepSuccessCheck", {"FieldExistsCheck": {"fields": ["username"]}}]))
        composite.execute()

        assert [(sub["name"], sub["passed"]) for sub in composite._subtest_results] == [
            ("StepSuccessCheck", True),
            ("FieldExistsCheck", True),
        ]

    def test_forwards_member_subtests_with_member_qualified_names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A composite preserves member probes for terminal and JUnit diagnostics."""

        class NestedCheck(BaseValidation):
            def run(self) -> None:
                self.report_subtest("probe-a", True, "probe passed")
                self.report_subtest("probe-b", False, "not applicable", skipped=True)
                self.set_passed("nested check passed")

        monkeypatch.setattr(composite_module, "get_validation_class", lambda name: NestedCheck)
        composite = CompositeCheck(config=_config(["NestedCheck"]))

        result = composite.execute()

        assert result["passed"] is True
        assert [(sub["name"], sub["skipped"]) for sub in result["subtests"]] == [
            ("NestedCheck", False),
            ("NestedCheck/probe-a", False),
            ("NestedCheck/probe-b", True),
        ]

    def test_skipped_member_does_not_skip_or_fail_the_composite(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An inapplicable member is skipped while later members still run."""

        class SkippedCheck(BaseValidation):
            def run(self) -> None:
                pytest.skip("only one rail discovered")

        class PassingCheck(BaseValidation):
            def run(self) -> None:
                self.set_passed("later member passed")

        classes = {"SkippedCheck": SkippedCheck, "PassingCheck": PassingCheck}
        monkeypatch.setattr(composite_module, "get_validation_class", classes.get)
        composite = CompositeCheck(config=_config(["SkippedCheck", "PassingCheck"]))

        result = composite.execute()

        assert result["passed"] is True
        assert [(sub["name"], sub["passed"], sub["skipped"]) for sub in result["subtests"]] == [
            ("SkippedCheck", False, True),
            ("PassingCheck", True, False),
        ]
        assert "SkippedCheck: skipped - only one rail discovered" in result["output"]
        assert "PassingCheck: later member passed" in result["output"]

    def test_fails_naming_the_failing_member(self) -> None:
        """A failing member fails the composite and is named in the error."""
        result = CompositeCheck(
            config=_config(
                ["StepSuccessCheck", {"FieldExistsCheck": {"fields": ["access_key_id"]}}],
                step_output={"success": True, "username": "u"},
            )
        ).execute()

        assert result["passed"] is False
        assert "FieldExistsCheck: Missing fields: access_key_id" in result["error"]

    def test_runs_every_member_after_a_failure(self) -> None:
        """One failure does not hide the others - all members still run."""
        composite = CompositeCheck(
            config=_config(
                ["StepSuccessCheck", {"FieldExistsCheck": {"fields": ["access_key_id"]}}],
                step_output={"success": False, "error": "boom"},
            )
        )
        result = composite.execute()

        assert result["passed"] is False
        assert [sub["name"] for sub in composite._subtest_results] == ["StepSuccessCheck", "FieldExistsCheck"]
        assert "StepSuccessCheck: Step failed: boom" in result["error"]
        assert "FieldExistsCheck: Missing fields: access_key_id" in result["error"]

    def test_fails_on_unknown_member(self) -> None:
        """A member naming no validation class fails rather than being skipped."""
        result = CompositeCheck(config=_config(["NoSuchCheck"])).execute()

        assert result["passed"] is False
        assert "NoSuchCheck: unknown validation class" in result["error"]

    def test_fails_when_compose_is_empty(self) -> None:
        """An empty ``compose`` is an authoring error, not a vacuous pass."""
        result = CompositeCheck(config=_config([])).execute()

        assert result["passed"] is False
        assert "must be a non-empty list" in result["error"]

    def test_fails_when_a_member_is_malformed(self) -> None:
        """A malformed member must fail rather than shrink the composite.

        An out-of-tree config never meets ``validate_suite_wiring``, so dropping
        the item would run fewer checks than the config names and still pass.
        """
        result = CompositeCheck(config=_config(["StepSuccessCheck", ["FieldExistsCheck"]])).execute()

        assert result["passed"] is False
        assert "1 malformed member(s)" in result["error"]

    def test_wiring_metadata_is_not_passed_to_members(self) -> None:
        """``test_id``/``labels``/``description`` describe the composite only.

        ``FieldValueCheck`` reads a ``field`` from config, so leaking the
        composite's own wiring keys into member config could silently change what
        a member checks.
        """
        config = _config(
            [{"FieldValueCheck": {"field": "username", "expected": "u"}}],
            step_output={"success": True, "username": "u", "description": "wrong"},
        )
        composite = CompositeCheck(config=config)
        result = composite.execute()

        assert result["passed"] is True
        assert "username=u" in result["output"]

    def test_composite_params_reach_a_bare_member(self) -> None:
        """Params on the composite reach a member listed without any of its own.

        A composite wrapping a single purpose-built check declares that check's
        params at the composite level, so this forwarding is what makes the
        one-member form work at all.
        """
        result = CompositeCheck(
            config=_config(
                ["FieldExistsCheck"],
                fields=["username"],
            )
        ).execute()

        assert result["passed"] is True
        assert "username" in result["output"]

    def test_member_params_override_shared_config(self) -> None:
        """A member's inline params win over the shared composite config."""
        result = CompositeCheck(
            config=_config(
                [{"FieldExistsCheck": {"fields": ["username"]}}],
                fields=["access_key_id"],
            )
        ).execute()

        assert result["passed"] is True

    def test_skips_with_the_step_reason_when_the_step_skipped(self) -> None:
        """A skipped step skips the composite before any member runs."""
        composite = CompositeCheck(
            config=_config(["StepSuccessCheck"], step_output={"skipped": True, "skip_reason": "not supported"})
        )
        with pytest.raises(pytest.skip.Exception, match="not supported"):
            composite.execute()

        assert composite._subtest_results == []

    def test_generic_checks_are_marked_compose_only(self) -> None:
        """The generic checks describe a mechanism, so they need a composite.

        Dropping the marker would silently let a suite spend a catalog identity
        on ``StepSuccessCheck`` again, which the wiring validator only catches
        while the marker is present.
        """
        for check in (FieldExistsCheck, FieldValueCheck, StepSuccessCheck, CrudOperationsCheck):
            assert check.compose_only is True, f"{check.__name__} must stay compose-only"

    def test_purpose_built_checks_are_not_compose_only(self) -> None:
        """A check whose name already says what it proves is wired directly."""
        assert IamCredentialAccessCheck.compose_only is False

    def test_is_excluded_from_discovery(self) -> None:
        """The runner is machinery: it must never be discovered or catalogued."""
        assert CompositeCheck.__name__ not in {cls.__name__ for cls in discover_all_tests()}
