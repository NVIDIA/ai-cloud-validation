# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Tests for orchestrator loop."""

import xml.etree.ElementTree as ET
from pathlib import Path

from isvtest.core.resolution import (
    ErrorReason,
    ResolvedEntry,
    SkipReason,
    State,
    ValidationEntry,
)

from isvctl.config.schema import PlatformCommands, RunConfig, StepConfig, ValidationConfig
from isvctl.orchestrator.context import Context
from isvctl.orchestrator.loop import (
    Orchestrator,
    Phase,
    _entries_missing_from_junit,
    _merge_junit_xmls,
    _write_terminal_junit_xml,
)
from isvctl.orchestrator.step_executor import (
    MissingStepRefError,
    StepExecutor,
    _find_missing_step_path,
    _format_stderr_excerpt,
)

_INVENTORY_SCRIPT = (
    '#!/bin/bash\necho \'{"success": true, "platform": "kubernetes", '
    '"cluster_name": "test", "node_count": 1, "kubernetes": {"node_count": 1}}\'\n'
)

_OK_SCRIPT = "#!/bin/bash\necho '{\"success\": true}'\n"

_NOISY_FAILING_SCRIPT = """#!/bin/sh
i=0
while [ "$i" -le 104 ]; do
  echo "stderr line $i" >&2
  i=$((i + 1))
done
echo "AWS_SECRET_ACCESS_KEY=super-secret" >&2
exit 7
"""


def _write_script(tmp_path: Path, name: str, content: str) -> str:
    """Create an executable script file under tmp_path and return its path.

    Args:
        tmp_path: Temporary directory where the script is written.
        name: Script filename to create.
        content: Script contents to write.

    Returns:
        String path to the created script.

    Side effects:
        Writes the file and sets executable mode 0o755.
    """
    path = tmp_path / name
    path.write_text(content)
    path.chmod(0o755)
    return str(path)


class TestOrchestrator:
    """Tests for Orchestrator class."""

    def test_detect_platform_from_config(self) -> None:
        """Test platform detection from test config."""
        config = RunConfig(
            commands={
                "kubernetes": PlatformCommands(
                    steps=[StepConfig(name="setup", command="echo", args=["test"], phase="setup")]
                )
            },
            tests=ValidationConfig(platform="kubernetes"),
        )
        orchestrator = Orchestrator(config)

        platform = orchestrator._detect_platform()
        assert platform == "kubernetes"

    def test_run_setup_phase_success(self, tmp_path: Path) -> None:
        """Test successful setup phase execution."""
        # Must match the "cluster" schema: success, platform, cluster_name, node_count (at root)
        script_path = _write_script(
            tmp_path,
            "setup_cluster.sh",
            """#!/bin/bash
cat << 'EOF'
{"success": true, "platform": "kubernetes", "cluster_name": "test-cluster", "node_count": 4, "kubernetes": {"node_count": 4}}
EOF
""",
        )

        config = RunConfig(
            commands={
                "kubernetes": PlatformCommands(
                    steps=[
                        StepConfig(name="setup_cluster", command=script_path, phase="setup"),
                    ]
                )
            },
            tests=ValidationConfig(platform="kubernetes"),
        )
        orchestrator = Orchestrator(config)

        result = orchestrator.run(phases=[Phase.SETUP])

        assert result.success
        assert len(result.phases) == 1
        assert result.phases[0].phase == Phase.SETUP
        assert result.phases[0].success
        assert result.inventory is not None
        assert "setup_cluster" in result.inventory

    def test_run_setup_phase_command_failure(self) -> None:
        """Test setup phase with command failure."""
        config = RunConfig(
            commands={
                "kubernetes": PlatformCommands(
                    steps=[
                        StepConfig(name="failing_setup", command="false", phase="setup"),
                    ]
                )
            },
            tests=ValidationConfig(platform="kubernetes"),
        )
        orchestrator = Orchestrator(config)

        result = orchestrator.run(phases=[Phase.SETUP])

        assert not result.success
        assert len(result.phases) == 1
        assert not result.phases[0].success
        assert "failed" in result.phases[0].message.lower()

    def test_run_skip_setup_phase(self) -> None:
        """Test skipping setup phase (platform-level skip)."""
        config = RunConfig(
            commands={"kubernetes": PlatformCommands(skip=True)},
            tests=ValidationConfig(platform="kubernetes"),
        )
        orchestrator = Orchestrator(config)

        result = orchestrator.run(phases=[Phase.SETUP])

        # When platform is skipped, no steps are returned which results in "No steps defined"
        assert not result.success
        assert "No steps defined" in result.phases[0].message

    def test_run_test_phase_requires_steps(self) -> None:
        """Test that test phase with no steps fails gracefully."""
        config = RunConfig(
            commands={
                "kubernetes": PlatformCommands(
                    steps=[]  # No steps defined
                )
            },
            tests=ValidationConfig(platform="kubernetes", cluster_name="test"),
        )
        orchestrator = Orchestrator(config)

        result = orchestrator.run(phases=[Phase.TEST])

        assert not result.success
        assert "No steps defined" in result.phases[0].message

    def test_run_test_phase_with_mocked_pytest_skip(self) -> None:
        """Skip complex test phase mocking - covered by integration tests."""
        pass

    def test_run_teardown_phase(self) -> None:
        """Test teardown phase execution when only teardown is requested.

        Covers the use case where setup ran in a previous invocation (e.g., with
        AWS_SKIP_TEARDOWN) and now the user explicitly runs ``--phase teardown``
        to clean up resources.
        """
        config = RunConfig(
            commands={
                "kubernetes": PlatformCommands(
                    steps=[
                        StepConfig(name="cleanup", command="echo", args=["cleanup"], phase="teardown"),
                    ]
                )
            },
            tests=ValidationConfig(platform="kubernetes"),
        )
        orchestrator = Orchestrator(config)

        result = orchestrator.run(phases=[Phase.TEARDOWN])

        assert result.success
        assert len(result.phases) == 1
        assert result.phases[0].phase == Phase.TEARDOWN
        assert result.phases[0].success
        assert "SKIPPED" not in result.phases[0].message, "teardown must actually run, not be skipped"
        step_names = [s["name"] for s in result.phases[0].details["steps"]]
        assert "cleanup" in step_names, "teardown step must have executed"

    def test_run_all_phases_with_failure(self) -> None:
        """Test that teardown runs even after setup failure."""
        config = RunConfig(
            commands={
                "kubernetes": PlatformCommands(
                    steps=[
                        StepConfig(name="failing_setup", command="false", phase="setup"),
                        StepConfig(name="cleanup", command="echo", args=["cleanup"], phase="teardown"),
                    ]
                )
            },
            tests=ValidationConfig(platform="kubernetes"),
        )
        orchestrator = Orchestrator(config)

        result = orchestrator.run(teardown_on_failure=True)

        # Overall should fail due to setup
        assert not result.success
        # But teardown should still have run
        teardown_phases = [p for p in result.phases if p.phase == Phase.TEARDOWN]
        assert len(teardown_phases) == 1
        assert teardown_phases[0].success

    def test_platform_detection_missing(self) -> None:
        """Test error when platform cannot be detected."""
        config = RunConfig(
            commands={},
            tests=ValidationConfig(),  # No platform specified
        )
        orchestrator = Orchestrator(config)

        result = orchestrator.run()

        assert not result.success
        assert "Cannot determine platform" in result.phases[0].message

    def test_teardown_runs_when_setup_validation_fails(self, tmp_path: Path) -> None:
        """Teardown must run when setup steps succeed but setup validations fail.

        Regression test for issue where validation failures in setup caused
        teardown to be skipped, leaking cloud resources.
        """
        setup_script = _write_script(tmp_path, "setup.sh", _INVENTORY_SCRIPT)

        config = RunConfig(
            commands={
                "kubernetes": PlatformCommands(
                    steps=[
                        StepConfig(name="setup_cluster", command=setup_script, phase="setup"),
                        StepConfig(name="cleanup", command="echo", args=["done"], phase="teardown"),
                    ]
                )
            },
            tests=ValidationConfig(
                platform="kubernetes",
                validations={
                    "setup_checks": {
                        "step": "setup_cluster",
                        "checks": {
                            "FieldExistsCheck": {
                                "field": "missing_field",
                            }
                        },
                    }
                },
            ),
        )
        orchestrator = Orchestrator(config)

        result = orchestrator.run(teardown_on_failure=True)

        assert not result.success
        teardown_phases = [p for p in result.phases if p.phase == Phase.TEARDOWN]
        assert len(teardown_phases) == 1, "teardown phase must run even when setup validations fail"
        teardown_step_names = [s["name"] for s in teardown_phases[0].details["steps"]]
        assert "cleanup" in teardown_step_names, "teardown step must have executed"

    def test_teardown_skipped_when_setup_steps_did_not_run(self) -> None:
        """Teardown must be skipped when no setup steps executed."""
        config = RunConfig(
            commands={
                "kubernetes": PlatformCommands(
                    phases=["setup", "teardown"],
                    steps=[
                        StepConfig(name="setup_cluster", command="echo", args=["hi"], phase="setup", skip=True),
                        StepConfig(name="cleanup", command="echo", args=["bye"], phase="teardown"),
                    ],
                )
            },
            tests=ValidationConfig(platform="kubernetes"),
        )
        orchestrator = Orchestrator(config)
        result = orchestrator.run(teardown_on_failure=True)

        teardown_phases = [p for p in result.phases if p.phase == Phase.TEARDOWN]
        assert len(teardown_phases) == 1
        assert "SKIPPED" in teardown_phases[0].message
        assert "setup steps did not run" in teardown_phases[0].message

    def test_teardown_continues_after_step_failure(self, tmp_path: Path) -> None:
        """All teardown steps must run even if an earlier teardown step fails.

        Regression test for issue where the first failing teardown step caused
        remaining teardown steps to be skipped (e.g., VM not deleted after
        NIM teardown failed).
        """
        setup_script = _write_script(tmp_path, "setup.sh", _INVENTORY_SCRIPT)
        teardown_ok_script = _write_script(tmp_path, "teardown_ok.sh", _OK_SCRIPT)

        config = RunConfig(
            commands={
                "kubernetes": PlatformCommands(
                    steps=[
                        StepConfig(name="setup_cluster", command=setup_script, phase="setup"),
                        StepConfig(name="teardown_nim", command="false", phase="teardown"),
                        StepConfig(name="teardown_vm", command=teardown_ok_script, phase="teardown"),
                    ]
                )
            },
            tests=ValidationConfig(platform="kubernetes"),
        )
        orchestrator = Orchestrator(config)
        result = orchestrator.run(teardown_on_failure=True)

        teardown_phases = [p for p in result.phases if p.phase == Phase.TEARDOWN]
        assert len(teardown_phases) == 1

        step_names = [s["name"] for s in teardown_phases[0].details["steps"]]
        assert "teardown_nim" in step_names, "first teardown step must be recorded"
        assert "teardown_vm" in step_names, "second teardown step must run despite first failure"

    def test_validation_without_step_output_is_reported_as_skipped(self) -> None:
        """A configured validation whose step produced no JSON is skipped visibly."""
        config = RunConfig(
            commands={
                "kubernetes": PlatformCommands(
                    phases=["test"],
                    steps=[
                        StepConfig(name="probe", command="true", phase="test"),
                    ],
                )
            },
            tests=ValidationConfig(
                platform="kubernetes",
                validations={
                    "probe_checks": {
                        "step": "probe",
                        "checks": {"StepSuccessCheck": {}},
                    },
                },
            ),
        )
        orchestrator = Orchestrator(config)

        result = orchestrator.run(phases=[Phase.TEST])

        assert result.success
        validations = result.phases[0].details["validations"]
        assert validations == [
            {
                "name": "StepSuccessCheck",
                "passed": True,
                "skipped": True,
                "message": "step 'probe' did not produce output",
                "category": "probe_checks",
                "state": "skipped",
                "skip_reason": "step_no_output",
                "error_reason": None,
            }
        ]

    def test_validation_template_error_is_reported_as_error(self, tmp_path: Path) -> None:
        """Validation parameter render failures are terminal validation errors."""
        ok_script = _write_script(tmp_path, "ok.sh", _OK_SCRIPT)
        config = RunConfig(
            commands={
                "kubernetes": PlatformCommands(
                    phases=["test"],
                    steps=[
                        StepConfig(name="probe", command=ok_script, phase="test"),
                    ],
                )
            },
            tests=ValidationConfig(
                platform="kubernetes",
                validations={
                    "probe_checks": {
                        "checks": {
                            "FieldExistsCheck": {
                                "field": "{{ missing.value }}",
                            }
                        },
                    },
                },
            ),
        )
        orchestrator = Orchestrator(config)

        result = orchestrator.run(phases=[Phase.TEST])

        assert not result.success
        validation = result.phases[0].details["validations"][0]
        assert validation["name"] == "FieldExistsCheck"
        assert validation["passed"] is False
        assert validation["skipped"] is False
        assert validation["state"] == "error"
        assert validation["error_reason"] == "template_render_failed"
        assert "failed to render validation parameters" in validation["message"]

    def test_preresolved_skip_and_error_are_written_to_junit(self, tmp_path: Path) -> None:
        """Merged JUnit includes terminal entries that never went through pytest."""
        ok_script = _write_script(tmp_path, "ok.sh", _OK_SCRIPT)
        junit_path = tmp_path / "junit.xml"
        config = RunConfig(
            commands={
                "kubernetes": PlatformCommands(
                    phases=["test"],
                    steps=[
                        StepConfig(name="no_json", command="true", phase="test"),
                        StepConfig(name="probe", command=ok_script, phase="test"),
                    ],
                )
            },
            tests=ValidationConfig(
                platform="kubernetes",
                validations={
                    "skip_checks": {
                        "step": "no_json",
                        "checks": {"StepSuccessCheck": {}},
                    },
                    "error_checks": {
                        "checks": {
                            "FieldExistsCheck": {
                                "field": "{{ missing.value }}",
                            }
                        },
                    },
                },
            ),
        )
        orchestrator = Orchestrator(config)

        orchestrator.run(phases=[Phase.TEST], junitxml=str(junit_path))

        root = ET.parse(junit_path).getroot()
        cases = {case.attrib["name"]: case for case in root.iter("testcase")}
        assert "StepSuccessCheck" in cases
        assert cases["StepSuccessCheck"].find("skipped") is not None
        assert cases["StepSuccessCheck"].find("skipped").attrib["type"] == "step_no_output"
        assert "FieldExistsCheck" in cases
        assert cases["FieldExistsCheck"].find("error") is not None
        assert cases["FieldExistsCheck"].find("error").attrib["type"] == "template_render_failed"


class TestTeardownOnlyPhase:
    """Tests for running teardown as the only requested phase.

    Covers the workflow where setup ran in a prior invocation (e.g., with
    AWS_SKIP_TEARDOWN set) and the user later runs ``--phase teardown`` to
    clean up resources from that earlier run.
    """

    def test_teardown_only_runs_without_setup(self) -> None:
        """Teardown must execute when it is the only requested phase.

        When a user explicitly requests ``--phase teardown``, it should run
        regardless of whether setup ran in this invocation.
        """
        config = RunConfig(
            commands={
                "kubernetes": PlatformCommands(
                    steps=[
                        StepConfig(name="setup_cluster", command="echo", args=["hi"], phase="setup"),
                        StepConfig(name="cleanup", command="echo", args=["bye"], phase="teardown"),
                    ]
                )
            },
            tests=ValidationConfig(platform="kubernetes"),
        )
        orchestrator = Orchestrator(config)
        result = orchestrator.run(phases=[Phase.TEARDOWN])

        assert result.success
        teardown_phases = [p for p in result.phases if p.phase == Phase.TEARDOWN]
        assert len(teardown_phases) == 1
        assert teardown_phases[0].success
        assert "SKIPPED" not in teardown_phases[0].message
        step_names = [s["name"] for s in teardown_phases[0].details["steps"]]
        assert "cleanup" in step_names

    def test_full_lifecycle_skips_teardown_when_setup_is_all_skip_placeholders(self) -> None:
        """skip:true setup placeholders must not falsely satisfy the teardown gate.

        ``execute_steps`` records placeholder StepResults for ``skip: true`` steps,
        so a naive ``step_results.steps`` truthy-check would let teardown run even
        though no setup command actually executed. The gate must look at the
        configured steps' skip flags, not just the result list.
        """
        config = RunConfig(
            commands={
                "kubernetes": PlatformCommands(
                    steps=[
                        StepConfig(name="setup_cluster", command="echo", args=["hi"], phase="setup", skip=True),
                        StepConfig(name="cleanup", command="echo", args=["bye"], phase="teardown"),
                    ]
                )
            },
            tests=ValidationConfig(platform="kubernetes"),
        )
        orchestrator = Orchestrator(config)
        result = orchestrator.run(phases=[Phase.SETUP, Phase.TEARDOWN])

        teardown_phases = [p for p in result.phases if p.phase == Phase.TEARDOWN]
        assert len(teardown_phases) == 1
        assert "SKIPPED" in teardown_phases[0].message
        assert "setup steps did not run" in teardown_phases[0].message

    def test_teardown_only_does_not_run_setup_steps(self) -> None:
        """When only teardown is requested, setup steps must not execute."""
        config = RunConfig(
            commands={
                "kubernetes": PlatformCommands(
                    steps=[
                        StepConfig(name="setup_cluster", command="echo", args=["created"], phase="setup"),
                        StepConfig(name="cleanup", command="echo", args=["deleted"], phase="teardown"),
                    ]
                )
            },
            tests=ValidationConfig(platform="kubernetes"),
        )
        orchestrator = Orchestrator(config)
        result = orchestrator.run(phases=[Phase.TEARDOWN])

        setup_phases = [p for p in result.phases if p.phase == Phase.SETUP]
        assert len(setup_phases) == 0, "setup phase must not appear when only teardown is requested"

    def test_teardown_only_best_effort_continues_past_failures(self, tmp_path: Path) -> None:
        """Teardown-only run must use best-effort so all cleanup steps execute."""
        ok_script = _write_script(tmp_path, "teardown_ok.sh", _OK_SCRIPT)

        config = RunConfig(
            commands={
                "kubernetes": PlatformCommands(
                    steps=[
                        StepConfig(name="teardown_nim", command="false", phase="teardown"),
                        StepConfig(name="teardown_vm", command=ok_script, phase="teardown"),
                    ]
                )
            },
            tests=ValidationConfig(platform="kubernetes"),
        )
        orchestrator = Orchestrator(config)
        result = orchestrator.run(phases=[Phase.TEARDOWN])

        teardown_phases = [p for p in result.phases if p.phase == Phase.TEARDOWN]
        assert len(teardown_phases) == 1
        step_names = [s["name"] for s in teardown_phases[0].details["steps"]]
        assert "teardown_nim" in step_names, "failing teardown step must be recorded"
        assert "teardown_vm" in step_names, "second teardown step must run despite first failure"

    def test_teardown_still_skipped_when_setup_requested_but_did_not_run(self) -> None:
        """When both setup and teardown are requested, teardown is still gated on setup execution.

        This ensures the existing safety guard stays in place for full lifecycle runs.
        """
        config = RunConfig(
            commands={
                "kubernetes": PlatformCommands(
                    phases=["setup", "teardown"],
                    steps=[
                        StepConfig(name="setup_cluster", command="echo", args=["hi"], phase="setup", skip=True),
                        StepConfig(name="cleanup", command="echo", args=["bye"], phase="teardown"),
                    ],
                )
            },
            tests=ValidationConfig(platform="kubernetes"),
        )
        orchestrator = Orchestrator(config)
        result = orchestrator.run(phases=[Phase.SETUP, Phase.TEARDOWN], teardown_on_failure=True)

        teardown_phases = [p for p in result.phases if p.phase == Phase.TEARDOWN]
        assert len(teardown_phases) == 1
        assert "SKIPPED" in teardown_phases[0].message
        assert "setup steps did not run" in teardown_phases[0].message


class TestStepExecutorBestEffort:
    """Tests for StepExecutor best_effort parameter."""

    def test_best_effort_false_stops_on_failure(self) -> None:
        """Without best_effort, execution stops after the first failing step."""
        executor = StepExecutor()
        context = Context(RunConfig())
        steps = [
            StepConfig(name="fail_step", command="false", phase="teardown"),
            StepConfig(name="ok_step", command="echo", args=["hi"], phase="teardown"),
        ]

        results = executor.execute_steps(steps, context, best_effort=False)

        assert not results.success
        executed = [s.name for s in results.steps]
        assert executed == ["fail_step"], "second step must NOT run when best_effort is False"

    def test_best_effort_true_continues_on_failure(self) -> None:
        """With best_effort, all steps execute even when one fails."""
        executor = StepExecutor()
        context = Context(RunConfig())
        steps = [
            StepConfig(name="fail_step", command="false", phase="teardown"),
            StepConfig(name="ok_step", command="echo", args=["hi"], phase="teardown"),
        ]

        results = executor.execute_steps(steps, context, best_effort=True)

        assert not results.success
        executed = [s.name for s in results.steps]
        assert executed == ["fail_step", "ok_step"], "second step must run when best_effort is True"
        assert not results.steps[0].success
        assert results.steps[1].success

    def test_best_effort_respects_continue_on_failure(self) -> None:
        """Steps with continue_on_failure=True continue regardless of best_effort."""
        executor = StepExecutor()
        context = Context(RunConfig())
        steps = [
            StepConfig(name="fail_step", command="false", phase="setup", continue_on_failure=True),
            StepConfig(name="ok_step", command="echo", args=["hi"], phase="setup"),
        ]

        results = executor.execute_steps(steps, context, best_effort=False)

        executed = [s.name for s in results.steps]
        assert executed == ["fail_step", "ok_step"]


_NOISY_FAILING_SCRIPT = """#!/bin/sh
i=0
while [ "$i" -le 104 ]; do
  echo "stderr line $i" >&2
  i=$((i + 1))
done
echo "AWS_SECRET_ACCESS_KEY=super-secret" >&2
exit 7
"""


class TestStderrExcerpt:
    """Tests for failed-step stderr excerpts."""

    def test_short_excerpt_preserves_lines_and_redacts_secrets(self) -> None:
        """Short stderr is preserved while sensitive key/value pairs are masked."""
        excerpt = _format_stderr_excerpt(
            "first line\nAWS_SECRET_ACCESS_KEY=super-secret\nlast line\n",
            head_lines=2,
            tail_lines=2,
        )

        assert "first line" in excerpt
        assert "last line" in excerpt
        assert "AWS_SECRET_ACCESS_KEY=***" in excerpt
        assert "super-secret" not in excerpt

    def test_long_excerpt_keeps_head_and_tail_lines(self) -> None:
        """Long stderr keeps early context and trailing root-cause lines."""
        stderr = "\n".join(f"stderr line {i}" for i in range(10))

        excerpt = _format_stderr_excerpt(stderr, head_lines=2, tail_lines=3)

        assert "stderr line 0" in excerpt
        assert "stderr line 1" in excerpt
        assert "stderr line 2" not in excerpt
        assert "stderr line 6" not in excerpt
        assert "stderr line 7" in excerpt
        assert "stderr line 9" in excerpt
        assert "omitted 5 lines" in excerpt

    def test_failed_step_error_uses_redacted_head_tail_stderr(self, tmp_path: Path) -> None:
        """Failed command summaries use the redacted line-based stderr excerpt."""
        script_path = tmp_path / "noisy_fail.sh"
        script_path.write_text(_NOISY_FAILING_SCRIPT)
        script_path.chmod(0o755)

        executor = StepExecutor()
        context = Context(RunConfig())
        steps = [StepConfig(name="fail_step", command=str(script_path), phase="setup")]

        results = executor.execute_steps(steps, context)

        error = results.steps[0].error or ""
        assert "Command exited with code 7" in error
        assert "stderr line 0" in error
        assert "stderr line 20" not in error
        assert "stderr line 104" in error
        assert "AWS_SECRET_ACCESS_KEY=***" in error
        assert "super-secret" not in error
        assert "stderr truncated" in error


class TestMissingStepRefDetection:
    """Tests for detecting undefined {{steps.X.Y}} references in step args."""

    def test_find_missing_step_path_returns_none_when_resolved(self) -> None:
        """Fully-resolved {{steps.X.Y}} refs return None (no missing path)."""
        steps_data = {"create_network": {"network_id": "vpc-abc123"}}
        arg = "{{steps.create_network.network_id}}"
        assert _find_missing_step_path(arg, steps_data) is None

    def test_find_missing_step_path_detects_missing_step(self) -> None:
        """Return the full path when the top-level step name is absent."""
        arg = "{{steps.create_network.network_id}}"
        assert _find_missing_step_path(arg, {}) == "create_network.network_id"

    def test_find_missing_step_path_detects_missing_field(self) -> None:
        """Return the full path when a leaf field is missing from step output."""
        steps_data = {"create_network": {"other_field": "x"}}
        arg = "{{steps.create_network.network_id}}"
        assert _find_missing_step_path(arg, steps_data) == "create_network.network_id"

    def test_find_missing_step_path_detects_empty_leaf(self) -> None:
        """Treat an empty-string leaf as missing (would render to '')."""
        steps_data = {"create_network": {"network_id": ""}}
        arg = "{{steps.create_network.network_id}}"
        assert _find_missing_step_path(arg, steps_data) == "create_network.network_id"

    def test_teardown_step_skipped_when_setup_step_missing(self) -> None:
        """Teardown with empty {{steps.create_network.network_id}} is skipped,
        not invoked with a stripped-out required arg."""
        executor = StepExecutor()
        context = Context(RunConfig())
        # No "create_network" step output - simulates a failed setup step.
        steps = [
            StepConfig(
                name="teardown_network",
                command="echo",
                args=["--vpc-id", "{{steps.create_network.network_id}}"],
                phase="teardown",
            ),
            StepConfig(
                name="cleanup",
                command="echo",
                args=["done"],
                phase="teardown",
            ),
        ]

        results = executor.execute_steps(steps, context, best_effort=True)

        assert not results.steps[0].success
        assert "missing step reference" in (results.steps[0].error or "").lower()
        assert results.steps[0].exit_code == -1
        # Remaining teardown steps still run under best_effort.
        assert [s.name for s in results.steps] == ["teardown_network", "cleanup"]
        assert results.steps[1].success

    def test_default_filter_suppresses_missing_ref_error(self) -> None:
        """Args with `| default(...)` are allowed to render empty silently."""
        executor = StepExecutor()
        context = Context(RunConfig())
        steps = [
            StepConfig(
                name="optional_flag",
                command="echo",
                args=["--region", "{{steps.create_network.region | default('')}}"],
                phase="teardown",
            ),
        ]

        results = executor.execute_steps(steps, context, best_effort=True)

        # The empty arg is filtered out; the step runs successfully with just
        # "--region" stripped (matching previous behavior for explicit defaults).
        assert results.steps[0].success

    def test_inline_empty_template_value_keeps_flag_value_pair(self) -> None:
        """Empty optional values stay attached when YAML uses ``--flag={{value}}``."""
        executor = StepExecutor()
        config = RunConfig(
            tests=ValidationConfig(
                settings={
                    "oidc_issuer_url": "",
                    "oidc_audience": "",
                    "oidc_target_url": "",
                }
            )
        )
        context = Context(config)

        rendered = executor._render_args(
            [
                "--issuer-url={{oidc_issuer_url}}",
                "--audience={{oidc_audience}}",
                "--target-url={{oidc_target_url}}",
            ],
            context,
        )

        assert rendered == ["--issuer-url=", "--audience=", "--target-url="]

    def test_missing_ref_raised_from_render_args_directly(self) -> None:
        """_render_args raises MissingStepRefError for bare references."""
        import pytest

        executor = StepExecutor()
        context = Context(RunConfig())
        args = ["--vpc-id", "{{steps.create_network.network_id}}"]

        with pytest.raises(MissingStepRefError) as exc_info:
            executor._render_args(args, context)

        assert exc_info.value.missing_path == "create_network.network_id"


class TestWriteTerminalJunitXml:
    """JUnit stub generation for entries pre-resolved before pytest."""

    @staticmethod
    def _entry(
        name: str,
        category: str,
        *,
        state: State,
        skip_reason: SkipReason | None = None,
        error_reason: ErrorReason | None = None,
        message: str = "",
    ) -> ResolvedEntry:
        return ResolvedEntry(
            entry=ValidationEntry(name=name, category=category, params_template={}),
            state=state,
            skip_reason=skip_reason,
            error_reason=error_reason,
            message=message,
        )

    def test_emits_skipped_and_error_testcases(self, tmp_path: Path) -> None:
        """Each pre-resolved entry produces one testcase with the right child element."""
        entries = [
            self._entry(
                "K8sNodePoolCheck",
                "cluster",
                state=State.SKIPPED,
                skip_reason=SkipReason.STEP_NOT_CONFIGURED,
                message="step 'create_test_node_pool' is not configured for this run",
            ),
            self._entry(
                "BrokenCheck",
                "network",
                state=State.ERROR,
                error_reason=ErrorReason.TEMPLATE_RENDER_FAILED,
                message="missing.value is undefined",
            ),
        ]
        output_path = tmp_path / "terminal.xml"

        _write_terminal_junit_xml(entries, output_path, "kubernetes/setup/resolved")

        tree = ET.parse(output_path)
        suite = tree.getroot()
        assert suite.tag == "testsuite"
        assert suite.get("name") == "kubernetes/setup/resolved"
        assert suite.get("tests") == "2"
        assert suite.get("errors") == "1"
        assert suite.get("skipped") == "1"

        cases = suite.findall("testcase")
        assert [c.get("name") for c in cases] == ["K8sNodePoolCheck", "BrokenCheck"]
        # classname intentionally not set: dashboards that prefix the testcase
        # name with classname (e.g., "category.TestName") would otherwise turn
        # the YAML grouping concept into user-facing noise.
        assert [c.get("classname") for c in cases] == [None, None]

        skipped = cases[0].find("skipped")
        assert skipped is not None
        assert skipped.get("type") == SkipReason.STEP_NOT_CONFIGURED.value
        assert skipped.get("message") == "step 'create_test_node_pool' is not configured for this run"
        # Body intentionally empty for skipped: avoids a misleading "Stack Trace"
        # panel on dashboards that render <skipped>'s body content as a trace.
        assert not skipped.text

        error = cases[1].find("error")
        assert error is not None
        assert error.get("type") == ErrorReason.TEMPLATE_RENDER_FAILED.value
        assert error.get("message") == "missing.value is undefined"
        assert error.text == "missing.value is undefined"

    def test_empty_entry_list_produces_zero_count_suite(self, tmp_path: Path) -> None:
        """An empty entry list still writes a valid empty testsuite."""
        output_path = tmp_path / "empty.xml"

        _write_terminal_junit_xml([], output_path, "kubernetes/teardown/resolved")

        suite = ET.parse(output_path).getroot()
        assert suite.get("tests") == "0"
        assert suite.findall("testcase") == []


class TestMergeJunitXmls:
    """Per-phase XMLs are concatenated; same-named suites fold into one."""

    @staticmethod
    def _write_suite(
        path: Path,
        name: str,
        cases: list[tuple[str, str | None]],
        *,
        time: str = "0.000",
        skipped: int = 0,
        errors: int = 0,
        failures: int = 0,
    ) -> None:
        """Write a minimal <testsuite> XML; ``cases`` is ``[(name, child_tag_or_None), ...]``."""
        suite = ET.Element(
            "testsuite",
            attrib={
                "name": name,
                "tests": str(len(cases)),
                "failures": str(failures),
                "errors": str(errors),
                "skipped": str(skipped),
                "time": time,
            },
        )
        for case_name, child_tag in cases:
            case = ET.SubElement(suite, "testcase", attrib={"name": case_name, "classname": "cluster"})
            if child_tag:
                ET.SubElement(case, child_tag, attrib={"message": "x"})
        ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)

    def test_distinct_suite_names_are_kept_separate(self, tmp_path: Path) -> None:
        """Suites with different names produce separate <testsuite> children."""
        a = tmp_path / "a.xml"
        b = tmp_path / "b.xml"
        self._write_suite(a, "kubernetes/setup", [("A", None)])
        self._write_suite(b, "kubernetes/teardown", [("B", None)])
        out = tmp_path / "merged.xml"

        _merge_junit_xmls([a, b], out)

        root = ET.parse(out).getroot()
        suites = root.findall("testsuite")
        assert [s.get("name") for s in suites] == ["kubernetes/setup", "kubernetes/teardown"]

    def test_same_named_suites_are_folded(self, tmp_path: Path) -> None:
        """Stub + pytest output for the same phase merge into a single suite."""
        pytest_xml = tmp_path / "pytest.xml"
        stub_xml = tmp_path / "stub.xml"
        self._write_suite(pytest_xml, "kubernetes/test", [("Ran", None)], time="1.500")
        self._write_suite(
            stub_xml,
            "kubernetes/test",
            [("PreSkipped", "skipped"), ("PreError", "error")],
            time="0.250",
            skipped=1,
            errors=1,
        )
        out = tmp_path / "merged.xml"

        _merge_junit_xmls([pytest_xml, stub_xml], out)

        root = ET.parse(out).getroot()
        suites = root.findall("testsuite")
        assert len(suites) == 1
        suite = suites[0]
        assert suite.get("name") == "kubernetes/test"
        assert {case.get("name") for case in suite.findall("testcase")} == {"Ran", "PreSkipped", "PreError"}
        assert suite.get("tests") == "3"
        assert suite.get("skipped") == "1"
        assert suite.get("errors") == "1"
        assert suite.get("failures") == "0"
        assert suite.get("time") == "1.750"

    def test_skips_unparseable_phase_files(self, tmp_path: Path) -> None:
        """A corrupt per-phase XML is logged and skipped, not fatal."""
        good = tmp_path / "good.xml"
        bad = tmp_path / "bad.xml"
        self._write_suite(good, "kubernetes/test", [("OK", None)])
        bad.write_text("<not-valid")
        out = tmp_path / "merged.xml"

        _merge_junit_xmls([good, bad], out)

        root = ET.parse(out).getroot()
        suites = root.findall("testsuite")
        assert [s.get("name") for s in suites] == ["kubernetes/test"]
        assert {case.get("name") for case in suites[0].findall("testcase")} == {"OK"}

    def test_name_order_interleaves_stub_and_pytest_testcases(self, tmp_path: Path) -> None:
        """With name_order, skipped stubs slot in beside pytest results per YAML order."""
        stub_xml = tmp_path / "stub.xml"
        pytest_xml = tmp_path / "pytest.xml"
        # Stub gets the marker-excluded entry that lives third in the YAML.
        self._write_suite(stub_xml, "kubernetes/test", [("MidSkipped", "skipped")], skipped=1)
        # Pytest emits the two ready entries that flank it; subtest also present.
        self._write_suite(
            pytest_xml,
            "kubernetes/test",
            [("First", None), ("First::sub", None), ("Last", None)],
        )
        out = tmp_path / "merged.xml"

        _merge_junit_xmls(
            [stub_xml, pytest_xml],
            out,
            name_order=["First", "MidSkipped", "Last"],
        )

        suite = ET.parse(out).getroot().find("testsuite")
        names = [case.get("name") for case in suite.findall("testcase")]
        assert names == ["First", "First::sub", "MidSkipped", "Last"]

    def test_name_order_keeps_unknown_testcases_at_end(self, tmp_path: Path) -> None:
        """Testcases not mentioned in name_order land after the known ones."""
        xml = tmp_path / "phase.xml"
        self._write_suite(xml, "kubernetes/test", [("Unknown", None), ("Known", None)])
        out = tmp_path / "merged.xml"

        _merge_junit_xmls([xml], out, name_order=["Known"])

        suite = ET.parse(out).getroot().find("testsuite")
        assert [case.get("name") for case in suite.findall("testcase")] == ["Known", "Unknown"]


class TestEntriesMissingFromJunit:
    """Diff helper that finds executed entries pytest didn't write to XML."""

    @staticmethod
    def _entry(name: str) -> ResolvedEntry:
        return ResolvedEntry(
            entry=ValidationEntry(name=name, category="cluster", params_template={}),
            state=State.SKIPPED,
            skip_reason=SkipReason.EXCLUDED,
            message="excluded by pytest -k/-m filter",
        )

    @staticmethod
    def _write_junit(path: Path, names: list[str]) -> None:
        suite = ET.Element("testsuite", attrib={"name": "test"})
        for name in names:
            ET.SubElement(suite, "testcase", attrib={"name": name})
        ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)

    def test_returns_executed_entries_not_in_junit(self, tmp_path: Path) -> None:
        """Entries deselected by pytest -k surface here so they can be stubbed."""
        junit = tmp_path / "junit.xml"
        self._write_junit(junit, ["RanCheck"])

        missing = _entries_missing_from_junit(
            [self._entry("RanCheck"), self._entry("DeselectedCheck")],
            junit,
        )

        assert [m.entry.name for m in missing] == ["DeselectedCheck"]

    def test_empty_when_all_executed_entries_are_in_junit(self, tmp_path: Path) -> None:
        """No stub needed when pytest wrote every executed entry."""
        junit = tmp_path / "junit.xml"
        self._write_junit(junit, ["A", "B"])

        assert _entries_missing_from_junit([self._entry("A"), self._entry("B")], junit) == []

    def test_returns_all_entries_when_junit_missing(self, tmp_path: Path) -> None:
        """If pytest never produced an XML, treat every executed entry as missing."""
        missing = _entries_missing_from_junit(
            [self._entry("Solo")],
            tmp_path / "does-not-exist.xml",
        )
        assert [m.entry.name for m in missing] == ["Solo"]

    def test_returns_all_entries_when_junit_unparseable(self, tmp_path: Path) -> None:
        """A corrupt pytest XML doesn't suppress entries from the merged report."""
        junit = tmp_path / "broken.xml"
        junit.write_text("<not-valid-xml")

        missing = _entries_missing_from_junit([self._entry("Solo")], junit)
        assert [m.entry.name for m in missing] == ["Solo"]

    def test_returns_all_entries_when_junit_path_is_none(self) -> None:
        """A None path (pytest skipped because no ready entries) returns the full list."""
        assert _entries_missing_from_junit([self._entry("A")], None) == [self._entry("A")]
