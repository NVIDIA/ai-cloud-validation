# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Tests for validation module."""

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from isvtest.core.runners import CommandResult
from isvtest.core.validation import BaseValidation
from isvtest.tests.test_validations import (
    _validation_results,
    clear_validation_results,
)
from isvtest.tests.test_validations import (
    test_validation as run_validation_entry_point,
)
from isvtest.validations.instance import (
    InstanceListCheck,
    InstancePowerCycleCheck,
    InstanceStartCheck,
    InstanceStopCheck,
    InstanceTagCheck,
)
from isvtest.validations.k8s_metrics import K8sApiServerMetricsCheck
from isvtest.validations.network import (
    BackendSwitchFabricCheck,
    ByoipCheck,
    FloatingIpCheck,
    LocalizedDnsCheck,
    NvlinkDomainCheck,
    StablePrivateIpCheck,
    VpcPeeringCheck,
)
from isvtest.validations.nim import NimHealthCheck, NimInferenceCheck, NimModelCheck
from isvtest.validations.security import ConsoleRbacCheck


class ConcreteValidation(BaseValidation):
    """Concrete implementation for testing."""

    description = "Test validation"
    timeout = 30

    def run(self) -> None:
        """Simple run implementation."""
        self.set_passed("Test passed")


class FailingValidation(BaseValidation):
    """Validation that always fails."""

    def run(self) -> None:
        """Fail the validation."""
        self.set_failed("Test failed", "Error output")


class ExceptionValidation(BaseValidation):
    """Validation that raises an exception."""

    def run(self) -> None:
        """Raise an exception."""
        raise RuntimeError("Unexpected error")


class TestBaseValidation:
    """Tests for BaseValidation class."""

    def test_init_with_defaults(self) -> None:
        """Test initialization with default values."""
        validation = ConcreteValidation()
        assert validation.name == "ConcreteValidation"
        assert validation.config == {}
        assert validation._passed is False
        assert validation._output == ""
        assert validation._error == ""

    def test_init_with_config(self) -> None:
        """Test initialization with custom config."""
        config = {"key": "value", "nested": {"inner": 42}}
        validation = ConcreteValidation(config=config)
        assert validation.config == config

    def test_set_passed(self) -> None:
        """Test set_passed method."""
        validation = ConcreteValidation()
        validation.set_passed("Success message")

        assert validation._passed is True
        assert validation._output == "Success message"

    def test_set_passed_without_message(self) -> None:
        """Test set_passed without message."""
        validation = ConcreteValidation()
        validation.set_passed()

        assert validation._passed is True
        assert validation._output == ""

    def test_set_failed(self) -> None:
        """Test set_failed method."""
        validation = ConcreteValidation()
        validation.set_failed("Error message", "Error output")

        assert validation._passed is False
        assert validation._error == "Error message"
        assert validation._output == "Error output"

    def test_set_failed_without_output(self) -> None:
        """Test set_failed without output."""
        validation = ConcreteValidation()
        validation.set_failed("Error message")

        assert validation._passed is False
        assert validation._error == "Error message"
        assert validation._output == ""

    def test_execute_returns_result_dict(self) -> None:
        """Test that execute returns a result dictionary."""
        validation = ConcreteValidation()
        result = validation.execute()

        assert isinstance(result, dict)
        assert result["name"] == "ConcreteValidation"
        assert result["passed"] is True
        assert result["output"] == "Test passed"
        assert result["error"] == ""
        assert result["description"] == "Test validation"
        assert "duration" in result
        assert result["duration"] >= 0

    def test_execute_with_failed_validation(self) -> None:
        """Test execute with a failing validation."""
        validation = FailingValidation()
        result = validation.execute()

        assert result["passed"] is False
        assert result["error"] == "Test failed"
        assert result["output"] == "Error output"

    def test_execute_catches_exceptions(self) -> None:
        """Test that execute catches exceptions from run()."""
        validation = ExceptionValidation()
        result = validation.execute()

        assert result["passed"] is False
        assert "Unexpected error" in result["error"]
        assert result["error_reason"] == "runtime_exception"

    def test_run_command(self) -> None:
        """Test run_command method."""
        mock_runner = MagicMock()
        mock_runner.run.return_value = CommandResult(
            exit_code=0,
            stdout="command output",
            stderr="",
            duration=0.5,
        )

        validation = ConcreteValidation(runner=mock_runner)
        result = validation.run_command("echo hello")

        mock_runner.run.assert_called_once_with("echo hello", timeout=30)
        assert result.exit_code == 0
        assert result.stdout == "command output"

    def test_run_command_with_custom_timeout(self) -> None:
        """Test run_command with custom timeout."""
        mock_runner = MagicMock()
        mock_runner.run.return_value = CommandResult(
            exit_code=0,
            stdout="",
            stderr="",
            duration=0.1,
        )

        validation = ConcreteValidation(runner=mock_runner)
        validation.run_command("slow command", timeout=120)

        mock_runner.run.assert_called_once_with("slow command", timeout=120)

    def test_run_command_appends_to_results(self) -> None:
        """Test that run_command appends results to _results list."""
        mock_runner = MagicMock()
        mock_result = CommandResult(exit_code=0, stdout="", stderr="", duration=0.1)
        mock_runner.run.return_value = mock_result

        validation = ConcreteValidation(runner=mock_runner)
        validation.run_command("cmd1")
        validation.run_command("cmd2")

        assert len(validation._results) == 2

    def test_class_attributes(self) -> None:
        """Test that class attributes are accessible."""
        assert ConcreteValidation.description == "Test validation"
        assert ConcreteValidation.timeout == 30

    def test_logger_is_created(self) -> None:
        """Test that a logger is created for the validation."""
        validation = ConcreteValidation()
        assert validation.log is not None
        assert validation.log.name == "ConcreteValidation"


class TestInstanceListCheck:
    """Tests for InstanceListCheck validation."""

    def _make_instance(
        self,
        instance_id: str = "i-abc123",
        state: str = "running",
        vpc_id: str = "vpc-111",
    ) -> dict:
        return {
            "instance_id": instance_id,
            "instance_type": "g5.xlarge",
            "state": state,
            "public_ip": "54.0.0.1",
            "private_ip": "10.0.0.1",
            "vpc_id": vpc_id,
        }

    def test_valid_list(self) -> None:
        """Test passing with a valid instance list."""
        v = InstanceListCheck(
            config={
                "step_output": {
                    "instances": [self._make_instance()],
                    "count": 1,
                },
            }
        )
        result = v.execute()
        assert result["passed"] is True
        assert "Listed 1 instance(s)" in result["output"]

    def test_found_target(self) -> None:
        """Test passing when target instance is found."""
        v = InstanceListCheck(
            config={
                "step_output": {
                    "instances": [self._make_instance(instance_id="i-target")],
                    "count": 1,
                    "found_target": True,
                    "target_instance": "i-target",
                },
            }
        )
        result = v.execute()
        assert result["passed"] is True
        assert "i-target" in result["output"]
        assert "found" in result["output"]

    def test_empty_list(self) -> None:
        """Test failure with an empty instance list."""
        v = InstanceListCheck(
            config={
                "step_output": {
                    "instances": [],
                    "count": 0,
                },
            }
        )
        result = v.execute()
        assert result["passed"] is False
        assert "at least 1" in result["error"]

    def test_missing_instances_key(self) -> None:
        """Test failure when instances key is missing."""
        v = InstanceListCheck(
            config={
                "step_output": {
                    "count": 0,
                },
            }
        )
        result = v.execute()
        assert result["passed"] is False
        assert "No 'instances' key" in result["error"]

    def test_target_not_found(self) -> None:
        """Test failure when target instance is not in the list."""
        v = InstanceListCheck(
            config={
                "step_output": {
                    "instances": [self._make_instance(instance_id="i-other")],
                    "count": 1,
                    "found_target": False,
                    "target_instance": "i-target",
                },
            }
        )
        result = v.execute()
        assert result["passed"] is False
        assert "i-target" in result["error"]
        assert "not found" in result["error"]

    def test_missing_required_fields(self) -> None:
        """Test failure when an instance is missing required fields."""
        v = InstanceListCheck(
            config={
                "step_output": {
                    "instances": [{"instance_type": "g5.xlarge"}],
                    "count": 1,
                },
            }
        )
        result = v.execute()
        assert result["passed"] is False
        assert "missing required field" in result["error"]

    def test_custom_min_count(self) -> None:
        """Test failure when instance count is below custom min_count."""
        v = InstanceListCheck(
            config={
                "step_output": {
                    "instances": [self._make_instance()],
                    "count": 1,
                },
                "min_count": 3,
            }
        )
        result = v.execute()
        assert result["passed"] is False
        assert "at least 3" in result["error"]

    def test_custom_min_count_satisfied(self) -> None:
        """Test passing when custom min_count is satisfied."""
        instances = [self._make_instance(instance_id=f"i-{i}") for i in range(3)]
        v = InstanceListCheck(
            config={
                "step_output": {
                    "instances": instances,
                    "count": 3,
                },
                "min_count": 3,
            }
        )
        result = v.execute()
        assert result["passed"] is True


class TestInstanceStopCheck:
    """Tests for InstanceStopCheck validation."""

    def test_stopped_successfully(self) -> None:
        v = InstanceStopCheck(
            config={
                "step_output": {
                    "instance_id": "i-abc123",
                    "stop_initiated": True,
                    "state": "stopped",
                },
            }
        )
        result = v.execute()
        assert result["passed"] is True
        assert "i-abc123" in result["output"]
        assert "stopped" in result["output"]

    def test_missing_instance_id(self) -> None:
        v = InstanceStopCheck(config={"step_output": {"stop_initiated": True, "state": "stopped"}})
        result = v.execute()
        assert result["passed"] is False
        assert "instance_id" in result["error"]

    def test_stop_not_initiated(self) -> None:
        v = InstanceStopCheck(
            config={
                "step_output": {
                    "instance_id": "i-abc123",
                    "stop_initiated": False,
                    "state": "stopped",
                },
            }
        )
        result = v.execute()
        assert result["passed"] is False
        assert "not initiated" in result["error"]

    def test_wrong_state(self) -> None:
        v = InstanceStopCheck(
            config={
                "step_output": {
                    "instance_id": "i-abc123",
                    "stop_initiated": True,
                    "state": "running",
                },
            }
        )
        result = v.execute()
        assert result["passed"] is False
        assert "stopped" in result["error"]
        assert "running" in result["error"]


class TestInstanceStartCheck:
    """Tests for InstanceStartCheck validation."""

    def test_started_successfully(self) -> None:
        v = InstanceStartCheck(
            config={
                "step_output": {
                    "instance_id": "i-abc123",
                    "start_initiated": True,
                    "state": "running",
                    "ssh_ready": True,
                },
            }
        )
        result = v.execute()
        assert result["passed"] is True
        assert "i-abc123" in result["output"]
        assert "running" in result["output"]

    def test_missing_instance_id(self) -> None:
        v = InstanceStartCheck(config={"step_output": {"start_initiated": True, "state": "running", "ssh_ready": True}})
        result = v.execute()
        assert result["passed"] is False
        assert "instance_id" in result["error"]

    def test_start_not_initiated(self) -> None:
        v = InstanceStartCheck(
            config={
                "step_output": {
                    "instance_id": "i-abc123",
                    "start_initiated": False,
                    "state": "running",
                    "ssh_ready": True,
                },
            }
        )
        result = v.execute()
        assert result["passed"] is False
        assert "not initiated" in result["error"]

    def test_wrong_state(self) -> None:
        v = InstanceStartCheck(
            config={
                "step_output": {
                    "instance_id": "i-abc123",
                    "start_initiated": True,
                    "state": "stopped",
                    "ssh_ready": True,
                },
            }
        )
        result = v.execute()
        assert result["passed"] is False
        assert "stopped" in result["error"]

    def test_ssh_not_ready(self) -> None:
        v = InstanceStartCheck(
            config={
                "step_output": {
                    "instance_id": "i-abc123",
                    "start_initiated": True,
                    "state": "running",
                    "ssh_ready": False,
                },
            }
        )
        result = v.execute()
        assert result["passed"] is False
        assert "SSH" in result["error"]


class TestInstanceTagCheck:
    """Tests for InstanceTagCheck validation."""

    def test_tags_present(self) -> None:
        v = InstanceTagCheck(
            config={
                "step_output": {
                    "instance_id": "i-abc123",
                    "tags": {"Name": "isv-test-gpu", "CreatedBy": "isvtest"},
                    "tag_count": 2,
                },
            }
        )
        result = v.execute()
        assert result["passed"] is True
        assert "i-abc123" in result["output"]
        assert "2" in result["output"]

    def test_required_keys_present(self) -> None:
        v = InstanceTagCheck(
            config={
                "step_output": {
                    "instance_id": "i-abc123",
                    "tags": {"Name": "isv-test-gpu", "CreatedBy": "isvtest"},
                    "tag_count": 2,
                },
                "required_keys": ["Name", "CreatedBy"],
            }
        )
        result = v.execute()
        assert result["passed"] is True

    def test_required_key_missing(self) -> None:
        v = InstanceTagCheck(
            config={
                "step_output": {
                    "instance_id": "i-abc123",
                    "tags": {"Name": "isv-test-gpu"},
                    "tag_count": 1,
                },
                "required_keys": ["Name", "CreatedBy"],
            }
        )
        result = v.execute()
        assert result["passed"] is False
        assert "CreatedBy" in result["error"]

    def test_no_tags(self) -> None:
        v = InstanceTagCheck(
            config={
                "step_output": {
                    "instance_id": "i-abc123",
                    "tags": {},
                    "tag_count": 0,
                },
            }
        )
        result = v.execute()
        assert result["passed"] is False
        assert "no tags" in result["error"].lower()

    def test_missing_instance_id(self) -> None:
        v = InstanceTagCheck(config={"step_output": {"tags": {"Name": "test"}, "tag_count": 1}})
        result = v.execute()
        assert result["passed"] is False
        assert "instance_id" in result["error"]

    def test_missing_tags_key(self) -> None:
        v = InstanceTagCheck(config={"step_output": {"instance_id": "i-abc123"}})
        result = v.execute()
        assert result["passed"] is False
        assert "tags" in result["error"]


class TestInstancePowerCycleCheck:
    """Tests for InstancePowerCycleCheck validation."""

    def test_power_cycle_success(self) -> None:
        v = InstancePowerCycleCheck(
            config={
                "step_output": {
                    "instance_id": "i-abc123",
                    "power_cycle_initiated": True,
                    "power_was_off": True,
                    "state": "running",
                    "ssh_ready": True,
                    "recovery_seconds": 180,
                },
            }
        )
        result = v.execute()
        assert result["passed"] is True
        assert "i-abc123" in result["output"]
        assert "recovery=180s" in result["output"]

    def test_missing_instance_id(self) -> None:
        v = InstancePowerCycleCheck(config={"step_output": {"power_cycle_initiated": True, "state": "running"}})
        result = v.execute()
        assert result["passed"] is False
        assert "instance_id" in result["error"]

    def test_power_cycle_not_initiated(self) -> None:
        v = InstancePowerCycleCheck(
            config={
                "step_output": {
                    "instance_id": "i-abc123",
                    "power_cycle_initiated": False,
                    "power_was_off": True,
                    "state": "running",
                    "ssh_ready": True,
                },
            }
        )
        result = v.execute()
        assert result["passed"] is False
        assert "not initiated" in result["error"]

    def test_power_was_not_off(self) -> None:
        v = InstancePowerCycleCheck(
            config={
                "step_output": {
                    "instance_id": "i-abc123",
                    "power_cycle_initiated": True,
                    "power_was_off": False,
                    "state": "running",
                    "ssh_ready": True,
                },
            }
        )
        result = v.execute()
        assert result["passed"] is False
        assert "powered-off" in result["error"]

    def test_not_running_after_cycle(self) -> None:
        v = InstancePowerCycleCheck(
            config={
                "step_output": {
                    "instance_id": "i-abc123",
                    "power_cycle_initiated": True,
                    "power_was_off": True,
                    "state": "stopped",
                    "ssh_ready": False,
                },
            }
        )
        result = v.execute()
        assert result["passed"] is False
        assert "stopped" in result["error"]

    def test_ssh_not_ready(self) -> None:
        v = InstancePowerCycleCheck(
            config={
                "step_output": {
                    "instance_id": "i-abc123",
                    "power_cycle_initiated": True,
                    "power_was_off": True,
                    "state": "running",
                    "ssh_ready": False,
                },
            }
        )
        result = v.execute()
        assert result["passed"] is False
        assert "SSH" in result["error"]

    def test_recovery_too_slow(self) -> None:
        v = InstancePowerCycleCheck(
            config={
                "step_output": {
                    "instance_id": "i-abc123",
                    "power_cycle_initiated": True,
                    "power_was_off": True,
                    "state": "running",
                    "ssh_ready": True,
                    "recovery_seconds": 1200,
                },
                "max_recovery_time": 900,
            }
        )
        result = v.execute()
        assert result["passed"] is False
        assert "1200s" in result["error"]
        assert "900s" in result["error"]

    def test_success_without_recovery_time(self) -> None:
        v = InstancePowerCycleCheck(
            config={
                "step_output": {
                    "instance_id": "i-abc123",
                    "power_cycle_initiated": True,
                    "power_was_off": True,
                    "state": "running",
                    "ssh_ready": True,
                },
            }
        )
        result = v.execute()
        assert result["passed"] is True
        assert "i-abc123" in result["output"]


def _console_rbac_config(step_output: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Build a minimal ConsoleRbacCheck config."""
    output: dict[str, Any] = {
        "success": True,
        "platform": "vm",
        "test_name": "console_rbac",
        "instance_id": "i-abc123",
        "rbac_model": "aws-iam",
        "access_restricted": True,
        "restricted_actions": ["ec2-instance-connect:SendSerialConsoleSSHPublicKey"],
        "tests": {
            "denied_principal_cannot_access_console": {"passed": True},
            "allowed_principal_can_access_console": {"passed": True},
            "allowed_principal_is_resource_scoped": {"passed": True},
        },
    }
    if step_output:
        output.update(step_output)
    return {"step_output": output}


class TestConsoleRbacCheck:
    """Tests for ConsoleRbacCheck validation."""

    def test_all_required_fields_and_subtests_pass(self) -> None:
        """Console RBAC passes when all required proof fields are present."""
        v = ConsoleRbacCheck(config=_console_rbac_config())
        result = v.execute()

        assert result["passed"] is True
        assert "i-abc123" in result["output"]
        assert "aws-iam" in result["output"]

    @pytest.mark.parametrize("access_restricted", [None, False])
    def test_access_restricted_must_be_true(self, access_restricted: bool | None) -> None:
        """Console RBAC fails when access_restricted is missing or false."""
        v = ConsoleRbacCheck(config=_console_rbac_config({"access_restricted": access_restricted}))
        result = v.execute()

        assert result["passed"] is False
        assert "access_restricted" in result["error"]

    def test_restricted_actions_must_be_non_empty(self) -> None:
        """Console RBAC fails when no restricted action is reported."""
        v = ConsoleRbacCheck(config=_console_rbac_config({"restricted_actions": []}))
        result = v.execute()

        assert result["passed"] is False
        assert "restricted console actions" in result["error"]

    @pytest.mark.parametrize(
        ("tests", "expected_error"),
        [
            (
                {
                    "allowed_principal_can_access_console": {"passed": True},
                    "allowed_principal_is_resource_scoped": {"passed": True},
                },
                "denied_principal_cannot_access_console",
            ),
            (
                {
                    "denied_principal_cannot_access_console": {"passed": True},
                    "allowed_principal_can_access_console": {"passed": False, "error": "denied"},
                    "allowed_principal_is_resource_scoped": {"passed": True},
                },
                "allowed_principal_can_access_console",
            ),
        ],
    )
    def test_required_subtests_must_pass(self, tests: dict[str, Any], expected_error: str) -> None:
        """Console RBAC fails when a required subtest is missing or failed."""
        v = ConsoleRbacCheck(config=_console_rbac_config({"tests": tests}))
        result = v.execute()

        assert result["passed"] is False
        assert expected_error in result["error"]


def _mock_ssh_run(responses: dict[str, tuple[int, str, str]]):
    """Create a mock run_ssh_command that returns canned responses by substring match."""

    def _run(ssh: MagicMock, command: str) -> tuple[int, str, str]:
        for pattern, response in responses.items():
            if pattern in command:
                return response
        return (1, "", "unknown command")

    return _run


def _nim_config(extra: dict | None = None) -> dict:
    """Build a minimal NIM validation config with SSH details."""
    cfg: dict = {
        "step_output": {
            "success": True,
            "host": "10.0.0.1",
            "key_file": "/tmp/test.pem",
            "ssh_user": "ubuntu",
            "port": 8000,
        },
    }
    if extra:
        cfg.update(extra)
    return cfg


class TestNimHealthCheck:
    """Tests for NimHealthCheck validation."""

    def test_skipped_when_nim_not_deployed(self) -> None:
        """Test skip when deploy_nim was skipped."""
        import pytest

        v = NimHealthCheck(
            config={
                "step_output": {
                    "skipped": True,
                    "skip_reason": "NGC_API_KEY not set",
                },
            }
        )
        with pytest.raises(pytest.skip.Exception, match="NGC_API_KEY"):
            v.execute()

    @patch("isvtest.validations.nim.get_ssh_client")
    @patch("isvtest.validations.nim.run_ssh_command")
    def test_healthy(self, mock_run: MagicMock, mock_ssh: MagicMock) -> None:
        """Test passing when health endpoint returns OK."""
        mock_ssh.return_value = MagicMock()
        mock_run.return_value = (0, "\n0", "")

        v = NimHealthCheck(config=_nim_config())
        result = v.execute()
        assert result["passed"] is True
        assert "health check passed" in result["output"]

    @patch("isvtest.validations.nim.get_ssh_client")
    @patch("isvtest.validations.nim.run_ssh_command")
    def test_unhealthy(self, mock_run: MagicMock, mock_ssh: MagicMock) -> None:
        """Test failure when health endpoint is not ready."""
        mock_ssh.return_value = MagicMock()
        mock_run.return_value = (1, "1", "")

        v = NimHealthCheck(config=_nim_config())
        result = v.execute()
        assert result["passed"] is False
        assert "not ready" in result["error"]

    def test_missing_host(self) -> None:
        """Test failure when host is missing but step succeeded."""
        v = NimHealthCheck(config={"step_output": {"success": True}})
        result = v.execute()
        assert result["passed"] is False
        assert "Missing host" in result["error"]

    def test_skipped_when_step_failed(self) -> None:
        """Test skip when deploy_nim step output is empty (timed out)."""
        import pytest

        v = NimHealthCheck(config={"step_output": {}})
        with pytest.raises(pytest.skip.Exception, match="did not succeed"):
            v.execute()


class TestNimInferenceCheck:
    """Tests for NimInferenceCheck validation."""

    @patch("isvtest.validations.nim.get_ssh_client")
    @patch("isvtest.validations.nim.run_ssh_command")
    def test_successful_inference(self, mock_run: MagicMock, mock_ssh: MagicMock) -> None:
        """Test passing with valid inference response."""
        mock_ssh.return_value = MagicMock()

        models_response = json.dumps({"data": [{"id": "meta/llama-3.2-3b-instruct"}]})
        inference_response = json.dumps(
            {
                "choices": [{"message": {"content": "CUDA is a platform..."}, "finish_reason": "stop"}],
                "usage": {"completion_tokens": 10, "prompt_tokens": 5, "total_tokens": 15},
            }
        )

        mock_run.side_effect = _mock_ssh_run(
            {
                "/v1/models": (0, models_response, ""),
                "/v1/chat/completions": (0, inference_response, ""),
            }
        )

        v = NimInferenceCheck(config=_nim_config())
        result = v.execute()
        assert result["passed"] is True
        assert "inference OK" in result["output"]

    @patch("isvtest.validations.nim.get_ssh_client")
    @patch("isvtest.validations.nim.run_ssh_command")
    def test_empty_choices(self, mock_run: MagicMock, mock_ssh: MagicMock) -> None:
        """Test failure when response has no choices."""
        mock_ssh.return_value = MagicMock()

        mock_run.side_effect = _mock_ssh_run(
            {
                "/v1/models": (0, json.dumps({"data": [{"id": "test-model"}]}), ""),
                "/v1/chat/completions": (0, json.dumps({"choices": []}), ""),
            }
        )

        v = NimInferenceCheck(config=_nim_config())
        result = v.execute()
        assert result["passed"] is False
        assert "No choices" in result["error"]

    @patch("isvtest.validations.nim.get_ssh_client")
    @patch("isvtest.validations.nim.run_ssh_command")
    def test_no_model_detected(self, mock_run: MagicMock, mock_ssh: MagicMock) -> None:
        """Test failure when no model can be detected."""
        mock_ssh.return_value = MagicMock()
        mock_run.return_value = (1, "", "error")

        v = NimInferenceCheck(config=_nim_config())
        result = v.execute()
        assert result["passed"] is False
        assert "Could not determine model" in result["error"]

    @patch("isvtest.validations.nim.get_ssh_client")
    @patch("isvtest.validations.nim.run_ssh_command")
    def test_model_from_config(self, mock_run: MagicMock, mock_ssh: MagicMock) -> None:
        """Test that model can be specified directly in config."""
        mock_ssh.return_value = MagicMock()

        inference_response = json.dumps(
            {
                "choices": [{"message": {"content": "response"}, "finish_reason": "stop"}],
            }
        )

        mock_run.side_effect = _mock_ssh_run(
            {
                "/v1/chat/completions": (0, inference_response, ""),
            }
        )

        v = NimInferenceCheck(config=_nim_config({"model": "my-model"}))
        result = v.execute()
        assert result["passed"] is True

    @patch("isvtest.validations.nim.get_ssh_client")
    @patch("isvtest.validations.nim.run_ssh_command")
    def test_invalid_json_response(self, mock_run: MagicMock, mock_ssh: MagicMock) -> None:
        """Test failure when inference returns invalid JSON."""
        mock_ssh.return_value = MagicMock()

        mock_run.side_effect = _mock_ssh_run(
            {
                "/v1/models": (0, json.dumps({"data": [{"id": "test-model"}]}), ""),
                "/v1/chat/completions": (0, "not json", ""),
            }
        )

        v = NimInferenceCheck(config=_nim_config())
        result = v.execute()
        assert result["passed"] is False
        assert "Invalid JSON" in result["error"]


class TestNimModelCheck:
    """Tests for NimModelCheck validation."""

    @patch("isvtest.validations.nim.get_ssh_client")
    @patch("isvtest.validations.nim.run_ssh_command")
    def test_models_returned(self, mock_run: MagicMock, mock_ssh: MagicMock) -> None:
        """Test passing when models are returned."""
        mock_ssh.return_value = MagicMock()
        mock_run.return_value = (
            0,
            json.dumps({"data": [{"id": "meta/llama-3.2-3b-instruct"}]}),
            "",
        )

        v = NimModelCheck(config=_nim_config())
        result = v.execute()
        assert result["passed"] is True
        assert "llama" in result["output"]

    @patch("isvtest.validations.nim.get_ssh_client")
    @patch("isvtest.validations.nim.run_ssh_command")
    def test_no_models(self, mock_run: MagicMock, mock_ssh: MagicMock) -> None:
        """Test failure when no models are returned."""
        mock_ssh.return_value = MagicMock()
        mock_run.return_value = (0, json.dumps({"data": []}), "")

        v = NimModelCheck(config=_nim_config())
        result = v.execute()
        assert result["passed"] is False
        assert "No models" in result["error"]

    @patch("isvtest.validations.nim.get_ssh_client")
    @patch("isvtest.validations.nim.run_ssh_command")
    def test_expected_model_found(self, mock_run: MagicMock, mock_ssh: MagicMock) -> None:
        """Test passing when expected model is found."""
        mock_ssh.return_value = MagicMock()
        mock_run.return_value = (
            0,
            json.dumps({"data": [{"id": "meta/llama-3.2-3b-instruct"}]}),
            "",
        )

        v = NimModelCheck(config=_nim_config({"expected_model": "llama"}))
        result = v.execute()
        assert result["passed"] is True

    @patch("isvtest.validations.nim.get_ssh_client")
    @patch("isvtest.validations.nim.run_ssh_command")
    def test_expected_model_not_found(self, mock_run: MagicMock, mock_ssh: MagicMock) -> None:
        """Test failure when expected model is not found."""
        mock_ssh.return_value = MagicMock()
        mock_run.return_value = (
            0,
            json.dumps({"data": [{"id": "meta/llama-3.2-3b-instruct"}]}),
            "",
        )

        v = NimModelCheck(config=_nim_config({"expected_model": "mistral"}))
        result = v.execute()
        assert result["passed"] is False
        assert "expected_model" in result["error"]

    @patch("isvtest.validations.nim.get_ssh_client")
    @patch("isvtest.validations.nim.run_ssh_command")
    def test_request_failed(self, mock_run: MagicMock, mock_ssh: MagicMock) -> None:
        """Test failure when models endpoint is unreachable."""
        mock_ssh.return_value = MagicMock()
        mock_run.return_value = (1, "", "connection refused")

        v = NimModelCheck(config=_nim_config())
        result = v.execute()
        assert result["passed"] is False
        assert "failed" in result["error"]


def _sdn_step_output(tests: dict) -> dict:
    """Build a step_output dict for SDN tests."""
    return {"step_output": {"success": True, "platform": "network", "tests": tests}}


class TestByoipCheck:
    """Tests for ByoipCheck validation."""

    def test_all_passed(self) -> None:
        tests = {
            "custom_cidr_create": {"passed": True, "vpc_id": "vpc-aaa", "cidr": "100.64.0.0/16"},
            "custom_cidr_verify": {"passed": True},
            "standard_cidr_create": {"passed": True},
            "no_conflict": {"passed": True},
            "custom_cidr_subnet": {"passed": True, "subnet_id": "subnet-aaa"},
        }
        v = ByoipCheck(config=_sdn_step_output(tests))
        result = v.execute()
        assert result["passed"] is True
        assert "100.64.0.0/16" in result["output"]

    def test_custom_cidr_failed(self) -> None:
        tests = {
            "custom_cidr_create": {"passed": False, "error": "CIDR rejected"},
            "custom_cidr_verify": {"passed": False},
            "standard_cidr_create": {"passed": False},
            "no_conflict": {"passed": False},
            "custom_cidr_subnet": {"passed": False},
        }
        v = ByoipCheck(config=_sdn_step_output(tests))
        result = v.execute()
        assert result["passed"] is False
        assert "custom_cidr_create" in result["error"]

    def test_empty_tests(self) -> None:
        v = ByoipCheck(config={"step_output": {}})
        result = v.execute()
        assert result["passed"] is False
        assert "tests" in result["error"]


class TestStablePrivateIpCheck:
    """Tests for StablePrivateIpCheck validation."""

    def test_ip_stable(self) -> None:
        tests = {
            "create_instance": {"passed": True, "instance_id": "i-xxx"},
            "record_ip": {"passed": True, "private_ip": "10.91.1.5"},
            "stop_instance": {"passed": True},
            "start_instance": {"passed": True},
            "ip_unchanged": {"passed": True, "ip_before": "10.91.1.5", "ip_after": "10.91.1.5"},
        }
        v = StablePrivateIpCheck(config=_sdn_step_output(tests))
        result = v.execute()
        assert result["passed"] is True
        assert "10.91.1.5" in result["output"]

    def test_ip_changed(self) -> None:
        tests = {
            "create_instance": {"passed": True},
            "record_ip": {"passed": True, "private_ip": "10.91.1.5"},
            "stop_instance": {"passed": True},
            "start_instance": {"passed": True},
            "ip_unchanged": {"passed": False, "error": "IP changed from 10.91.1.5 to 10.91.1.99"},
        }
        v = StablePrivateIpCheck(config=_sdn_step_output(tests))
        result = v.execute()
        assert result["passed"] is False
        assert "ip_unchanged" in result["error"]

    def test_empty_tests(self) -> None:
        v = StablePrivateIpCheck(config={"step_output": {}})
        result = v.execute()
        assert result["passed"] is False


class TestFloatingIpCheck:
    """Tests for FloatingIpCheck validation."""

    def test_fast_switch(self) -> None:
        tests = {
            "allocate_eip": {"passed": True, "allocation_id": "eipalloc-xxx", "public_ip": "54.1.2.3"},
            "associate_to_a": {"passed": True},
            "verify_on_a": {"passed": True},
            "reassociate_to_b": {"passed": True, "switch_seconds": 1.78},
            "verify_on_b": {"passed": True},
            "verify_not_on_a": {"passed": True},
        }
        v = FloatingIpCheck(config=_sdn_step_output(tests))
        result = v.execute()
        assert result["passed"] is True
        assert "1.78" in result["output"]

    def test_slow_switch(self) -> None:
        tests = {
            "allocate_eip": {"passed": True, "public_ip": "54.1.2.3"},
            "associate_to_a": {"passed": True},
            "verify_on_a": {"passed": True},
            "reassociate_to_b": {"passed": True, "switch_seconds": 15.0},
            "verify_on_b": {"passed": True},
            "verify_not_on_a": {"passed": True},
        }
        v = FloatingIpCheck(config={**_sdn_step_output(tests), "max_switch_seconds": 10})
        result = v.execute()
        assert result["passed"] is False
        assert "15.0" in result["error"]

    def test_eip_not_removed(self) -> None:
        tests = {
            "allocate_eip": {"passed": True, "public_ip": "54.1.2.3"},
            "associate_to_a": {"passed": True},
            "verify_on_a": {"passed": True},
            "reassociate_to_b": {"passed": True, "switch_seconds": 2.0},
            "verify_on_b": {"passed": True},
            "verify_not_on_a": {"passed": False, "error": "EIP still on instance A"},
        }
        v = FloatingIpCheck(config=_sdn_step_output(tests))
        result = v.execute()
        assert result["passed"] is False
        assert "verify_not_on_a" in result["error"]

    def test_empty_tests(self) -> None:
        v = FloatingIpCheck(config={"step_output": {}})
        result = v.execute()
        assert result["passed"] is False


class TestLocalizedDnsCheck:
    """Tests for LocalizedDnsCheck validation."""

    def test_all_passed(self) -> None:
        tests = {
            "create_vpc_with_dns": {"passed": True, "vpc_id": "vpc-xxx"},
            "create_hosted_zone": {"passed": True, "zone_id": "/hostedzone/Zxxx"},
            "create_dns_record": {"passed": True, "fqdn": "storage.internal.isv.test"},
            "verify_dns_settings": {"passed": True},
            "resolve_record": {"passed": True, "resolved_ip": "10.89.1.100"},
        }
        v = LocalizedDnsCheck(config=_sdn_step_output(tests))
        result = v.execute()
        assert result["passed"] is True
        assert "storage.internal.isv.test" in result["output"]
        assert "10.89.1.100" in result["output"]

    def test_resolve_failed(self) -> None:
        tests = {
            "create_vpc_with_dns": {"passed": True},
            "create_hosted_zone": {"passed": True},
            "create_dns_record": {"passed": True, "fqdn": "storage.internal.isv.test"},
            "verify_dns_settings": {"passed": True},
            "resolve_record": {"passed": False, "error": "Record not found"},
        }
        v = LocalizedDnsCheck(config=_sdn_step_output(tests))
        result = v.execute()
        assert result["passed"] is False
        assert "resolve_record" in result["error"]

    def test_empty_tests(self) -> None:
        v = LocalizedDnsCheck(config={"step_output": {}})
        result = v.execute()
        assert result["passed"] is False


class TestVpcPeeringCheck:
    """Tests for VpcPeeringCheck validation."""

    def test_peering_active(self) -> None:
        tests = {
            "create_vpc_a": {"passed": True, "vpc_id": "vpc-aaa"},
            "create_vpc_b": {"passed": True, "vpc_id": "vpc-bbb"},
            "create_peering": {"passed": True, "peering_id": "pcx-xxx"},
            "accept_peering": {"passed": True},
            "add_routes": {"passed": True},
            "peering_active": {"passed": True, "status": "active"},
        }
        config = _sdn_step_output(tests)
        config["step_output"]["vpc_a"] = {"id": "vpc-aaa", "cidr": "10.88.0.0/16"}
        config["step_output"]["vpc_b"] = {"id": "vpc-bbb", "cidr": "10.87.0.0/16"}
        v = VpcPeeringCheck(config=config)
        result = v.execute()
        assert result["passed"] is True
        assert "vpc-aaa" in result["output"]
        assert "vpc-bbb" in result["output"]

    def test_peering_failed(self) -> None:
        tests = {
            "create_vpc_a": {"passed": True},
            "create_vpc_b": {"passed": True},
            "create_peering": {"passed": True},
            "accept_peering": {"passed": False, "error": "Timeout waiting for active"},
            "add_routes": {"passed": False},
            "peering_active": {"passed": False},
        }
        v = VpcPeeringCheck(config=_sdn_step_output(tests))
        result = v.execute()
        assert result["passed"] is False
        assert "accept_peering" in result["error"]

    def test_empty_tests(self) -> None:
        v = VpcPeeringCheck(config={"step_output": {}})
        result = v.execute()
        assert result["passed"] is False


def _backend_switch_fabric_output(
    fabric: dict[str, Any] | None = None,
    tests: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a step_output dict for backend switch fabric tests."""
    return {
        "step_output": {
            "success": True,
            "platform": "network",
            "node_id": "compute-node-1",
            "fabric": fabric
            if fabric is not None
            else {
                "leaf_switch_ids": ["leaf-1"],
                "spine_switch_ids": ["spine-1"],
                "core_switch_ids": ["core-1"],
            },
            "tests": tests
            if tests is not None
            else {
                "node_resolved": {"passed": True},
                "leaf_switch_ids_present": {"passed": True},
                "spine_switch_ids_present": {"passed": True},
                "core_switch_ids_present": {"passed": True},
            },
        }
    }


class TestBackendSwitchFabricCheck:
    """Tests for BackendSwitchFabricCheck validation."""

    def test_all_passed(self) -> None:
        """Validate backend switch fabric output when all required checks pass."""
        v = BackendSwitchFabricCheck(config=_backend_switch_fabric_output())
        result = v.execute()
        assert result["passed"] is True
        assert "compute-node-1" in result["output"]
        assert "leaf" in result["output"]

    def test_missing_node_id(self) -> None:
        """Reject backend switch fabric output that omits the node identifier."""
        config = _backend_switch_fabric_output()
        config["step_output"]["node_id"] = ""
        v = BackendSwitchFabricCheck(config=config)
        result = v.execute()
        assert result["passed"] is False
        assert "node_id" in result["error"]

    @pytest.mark.parametrize(
        "field_name",
        ["leaf_switch_ids", "spine_switch_ids", "core_switch_ids"],
    )
    def test_empty_switch_id_collection(self, field_name: str) -> None:
        """Reject backend switch fabric output with any empty switch ID collection."""
        fabric = {
            "leaf_switch_ids": ["leaf-1"],
            "spine_switch_ids": ["spine-1"],
            "core_switch_ids": ["core-1"],
        }
        fabric[field_name] = []
        v = BackendSwitchFabricCheck(config=_backend_switch_fabric_output(fabric=fabric))
        result = v.execute()
        assert result["passed"] is False
        assert field_name in result["error"]

    def test_failed_required_subtest(self) -> None:
        """Reject backend switch fabric output when a required subtest fails."""
        tests = {
            "node_resolved": {"passed": True},
            "leaf_switch_ids_present": {"passed": False, "error": "leaf unavailable"},
            "spine_switch_ids_present": {"passed": True},
            "core_switch_ids_present": {"passed": True},
        }
        v = BackendSwitchFabricCheck(config=_backend_switch_fabric_output(tests=tests))
        result = v.execute()
        assert result["passed"] is False
        assert "leaf_switch_ids_present" in result["error"]


def _nvlink_domain_output(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a step_output dict for NVLink domain tests."""
    step_output = {
        "success": True,
        "platform": "network",
        "node_id": "compute-node-1",
        "nvlink_supported": True,
        "nvlink_domain_id": "domain-1",
        "tests": {
            "node_resolved": {"passed": True},
            "nvlink_support_detected": {"passed": True},
            "nvlink_domain_id_present": {"passed": True},
        },
    }
    if extra:
        step_output.update(extra)
    return {"step_output": step_output}


class TestNvlinkDomainCheck:
    """Tests for NvlinkDomainCheck validation."""

    def test_all_passed(self) -> None:
        """Validate NVLink domain output when all required checks pass."""
        v = NvlinkDomainCheck(config=_nvlink_domain_output())
        result = v.execute()
        assert result["passed"] is True
        assert "domain-1" in result["output"]

    def test_unsupported_node_skips(self) -> None:
        """Skip NVLink domain validation when the node does not support NVLink."""
        config = _nvlink_domain_output(
            {
                "nvlink_supported": False,
                "nvlink_domain_id": "",
            }
        )
        v = NvlinkDomainCheck(config=config)
        with pytest.raises(pytest.skip.Exception, match="NVLink not supported"):
            v.execute()

    def test_missing_domain_id_when_supported(self) -> None:
        """Reject supported NVLink output when the domain identifier is missing."""
        config = _nvlink_domain_output({"nvlink_domain_id": ""})
        v = NvlinkDomainCheck(config=config)
        result = v.execute()
        assert result["passed"] is False
        assert "nvlink_domain_id" in result["error"]

    def test_failed_domain_subtest_when_supported(self) -> None:
        """Reject supported NVLink output when the domain subtest fails."""
        config = _nvlink_domain_output(
            {
                "tests": {
                    "node_resolved": {"passed": True},
                    "nvlink_support_detected": {"passed": True},
                    "nvlink_domain_id_present": {"passed": False, "error": "domain missing"},
                }
            }
        )
        v = NvlinkDomainCheck(config=config)
        result = v.execute()
        assert result["passed"] is False
        assert "nvlink_domain_id_present" in result["error"]


class TestValidationResultCapture:
    """Tests that test_validation() captures results in _validation_results.

    Exercises the orchestration integration path: skipped, passed, and failed
    validations must all appear in the in-memory results list so the
    ORCHESTRATION RESULTS summary can display them.
    """

    def setup_method(self) -> None:
        clear_validation_results()

    def test_skipped_validation_captured(self) -> None:
        """Skipped validations must appear in _validation_results with skipped=True."""
        config = {
            "step_output": {"skipped": True, "skip_reason": "NGC_API_KEY not set"},
            "_category": "nim",
        }
        subtests = MagicMock()

        with pytest.raises(pytest.skip.Exception):
            run_validation_entry_point(NimHealthCheck, config, "NimHealthCheck", subtests)

        assert len(_validation_results) == 1
        r = _validation_results[0]
        assert r["name"] == "NimHealthCheck"
        assert r["skipped"] is True
        assert r["passed"] is True
        assert r["category"] == "nim"
        assert "NGC_API_KEY" in r["message"]

    def test_passed_validation_captured(self) -> None:
        """Passed validations must appear with skipped=False."""
        config = {"_category": "test_cat"}
        subtests = MagicMock()

        run_validation_entry_point(ConcreteValidation, config, "ConcreteValidation", subtests)

        assert len(_validation_results) == 1
        r = _validation_results[0]
        assert r["name"] == "ConcreteValidation"
        assert r["skipped"] is False
        assert r["passed"] is True

    def test_failed_validation_captured(self) -> None:
        """Failed validations must appear with passed=False."""
        config = {"_category": "test_cat"}
        subtests = MagicMock()

        with pytest.raises(AssertionError):
            run_validation_entry_point(FailingValidation, config, "FailingValidation", subtests)

        assert len(_validation_results) == 1
        r = _validation_results[0]
        assert r["name"] == "FailingValidation"
        assert r["skipped"] is False
        assert r["passed"] is False


class TestCloudInitCheckMetadataHeaders:
    """Tests for CloudInitCheck metadata_headers parameter."""

    @patch("isvtest.validations.host.get_ssh_client")
    @patch("isvtest.validations.host.run_ssh_command")
    @patch("isvtest.validations.host.get_ssh_config")
    def test_no_headers_uses_default_curl(
        self, mock_ssh_cfg: MagicMock, mock_run: MagicMock, mock_ssh: MagicMock
    ) -> None:
        """Without metadata_headers, curl command must not contain -H flags."""
        from isvtest.validations.host import CloudInitCheck

        mock_ssh_cfg.return_value = {"ssh_host": "10.0.0.1", "ssh_user": "ubuntu", "ssh_key_path": "/tmp/k.pem"}
        mock_ssh.return_value = MagicMock()
        mock_run.return_value = (0, "200", "")

        captured_cmds: list[str] = []

        def _capture(ssh: MagicMock, cmd: str) -> tuple[int, str, str]:
            captured_cmds.append(cmd)
            if "cloud-init" in cmd:
                return (0, "status: done", "")
            return (0, "200", "")

        mock_run.side_effect = _capture

        v = CloudInitCheck(config={})
        v.execute()

        curl_cmds = [c for c in captured_cmds if "curl" in c]
        assert len(curl_cmds) == 1
        assert "-H" not in curl_cmds[0]
        assert "169.254.169.254" in curl_cmds[0]

    @patch("isvtest.validations.host.get_ssh_client")
    @patch("isvtest.validations.host.run_ssh_command")
    @patch("isvtest.validations.host.get_ssh_config")
    def test_gcp_headers_included_in_curl(
        self, mock_ssh_cfg: MagicMock, mock_run: MagicMock, mock_ssh: MagicMock
    ) -> None:
        """With metadata_headers set, curl must include -H flags for each header."""
        from isvtest.validations.host import CloudInitCheck

        mock_ssh_cfg.return_value = {"ssh_host": "10.0.0.1", "ssh_user": "ubuntu", "ssh_key_path": "/tmp/k.pem"}
        mock_ssh.return_value = MagicMock()

        captured_cmds: list[str] = []

        def _capture(ssh: MagicMock, cmd: str) -> tuple[int, str, str]:
            captured_cmds.append(cmd)
            if "cloud-init" in cmd:
                return (0, "status: done", "")
            return (0, "200", "")

        mock_run.side_effect = _capture

        v = CloudInitCheck(
            config={
                "metadata_url": "http://metadata.google.internal/computeMetadata/v1/",
                "metadata_headers": {"Metadata-Flavor": "Google"},
            }
        )
        v.execute()

        curl_cmds = [c for c in captured_cmds if "curl" in c]
        assert len(curl_cmds) == 1
        assert "-H 'Metadata-Flavor: Google'" in curl_cmds[0]
        assert "metadata.google.internal" in curl_cmds[0]

    @patch("isvtest.validations.host.get_ssh_client")
    @patch("isvtest.validations.host.run_ssh_command")
    @patch("isvtest.validations.host.get_ssh_config")
    def test_multiple_headers(self, mock_ssh_cfg: MagicMock, mock_run: MagicMock, mock_ssh: MagicMock) -> None:
        """Multiple metadata_headers entries must each produce a -H flag."""
        from isvtest.validations.host import CloudInitCheck

        mock_ssh_cfg.return_value = {"ssh_host": "10.0.0.1", "ssh_user": "ubuntu", "ssh_key_path": "/tmp/k.pem"}
        mock_ssh.return_value = MagicMock()

        captured_cmds: list[str] = []

        def _capture(ssh: MagicMock, cmd: str) -> tuple[int, str, str]:
            captured_cmds.append(cmd)
            if "cloud-init" in cmd:
                return (0, "status: done", "")
            return (0, "200", "")

        mock_run.side_effect = _capture

        v = CloudInitCheck(
            config={
                "metadata_headers": {"X-Custom": "value1", "X-Other": "value2"},
            }
        )
        v.execute()

        curl_cmds = [c for c in captured_cmds if "curl" in c]
        assert len(curl_cmds) == 1
        assert "-H 'X-Custom: value1'" in curl_cmds[0]
        assert "-H 'X-Other: value2'" in curl_cmds[0]


SAMPLE_APISERVER_METRICS = """\
# HELP apiserver_request_total Counter of apiserver requests broken out for each verb, dry run value, group, version, resource, scope, component, and HTTP response code.
# TYPE apiserver_request_total counter
apiserver_request_total{code="200",component="apiserver",group="",resource="pods",verb="GET"} 1234
apiserver_request_total{code="201",component="apiserver",group="",resource="pods",verb="POST"} 56
# HELP apiserver_request_duration_seconds Response latency distribution in seconds for each verb, dry run value, group, version, resource, subresource, scope, and component.
# TYPE apiserver_request_duration_seconds histogram
apiserver_request_duration_seconds_bucket{component="apiserver",verb="GET",le="0.1"} 500
apiserver_request_duration_seconds_count{component="apiserver",verb="GET"} 1000
# HELP process_cpu_seconds_total Total user and system CPU time spent in seconds.
# TYPE process_cpu_seconds_total counter
process_cpu_seconds_total 42.5
"""


class TestK8sApiServerMetricsCheck:
    """Tests for K8sApiServerMetricsCheck validation."""

    def test_successful_metrics_response(self) -> None:
        """Valid Prometheus payload with default expected metrics passes."""
        mock_runner = MagicMock()
        mock_runner.run.return_value = CommandResult(
            exit_code=0, stdout=SAMPLE_APISERVER_METRICS, stderr="", duration=0.1
        )
        validation = K8sApiServerMetricsCheck(runner=mock_runner, config={})
        result = validation.execute()
        assert result["passed"] is True
        assert "Prometheus format" in result["output"]

    def test_missing_expected_metrics(self) -> None:
        """Payload missing the default metrics fails with both names listed."""
        payload = (
            "# HELP process_cpu_seconds_total Total user and system CPU time.\n"
            "# TYPE process_cpu_seconds_total counter\n"
            "process_cpu_seconds_total 42.5\n"
        )
        mock_runner = MagicMock()
        mock_runner.run.return_value = CommandResult(exit_code=0, stdout=payload, stderr="", duration=0.1)
        validation = K8sApiServerMetricsCheck(runner=mock_runner, config={})
        result = validation.execute()
        assert result["passed"] is False
        assert "apiserver_request_total" in result["error"]
        assert "apiserver_request_duration_seconds" in result["error"]

    def test_empty_response(self) -> None:
        """An empty 200 response is reported explicitly."""
        mock_runner = MagicMock()
        mock_runner.run.return_value = CommandResult(exit_code=0, stdout="", stderr="", duration=0.1)
        validation = K8sApiServerMetricsCheck(runner=mock_runner, config={})
        result = validation.execute()
        assert result["passed"] is False
        assert "empty response" in result["error"]

    def test_kubectl_command_failure_hints_rbac(self) -> None:
        """Non-zero kubectl exit surfaces stderr and hints at RBAC."""
        mock_runner = MagicMock()
        mock_runner.run.return_value = CommandResult(
            exit_code=1,
            stdout="",
            stderr="Error from server (Forbidden): ...",
            duration=0.1,
        )
        validation = K8sApiServerMetricsCheck(runner=mock_runner, config={})
        result = validation.execute()
        assert result["passed"] is False
        assert "Failed to query" in result["error"]
        assert "RBAC" in result["error"]
        assert "/metrics" in result["error"]

    def test_custom_expected_metrics(self) -> None:
        """Overriding expected_metrics with a present name passes."""
        mock_runner = MagicMock()
        mock_runner.run.return_value = CommandResult(
            exit_code=0, stdout=SAMPLE_APISERVER_METRICS, stderr="", duration=0.1
        )
        validation = K8sApiServerMetricsCheck(
            runner=mock_runner, config={"expected_metrics": ["process_cpu_seconds_total"]}
        )
        result = validation.execute()
        assert result["passed"] is True

    def test_custom_expected_metrics_missing(self) -> None:
        """Overriding expected_metrics with an absent name fails and names it."""
        mock_runner = MagicMock()
        mock_runner.run.return_value = CommandResult(
            exit_code=0, stdout=SAMPLE_APISERVER_METRICS, stderr="", duration=0.1
        )
        validation = K8sApiServerMetricsCheck(
            runner=mock_runner, config={"expected_metrics": ["nonexistent_metric_foobar"]}
        )
        result = validation.execute()
        assert result["passed"] is False
        assert "nonexistent_metric_foobar" in result["error"]

    def test_not_prometheus_format(self) -> None:
        """Non-Prometheus payload (e.g. Status JSON) fails with clear message."""
        mock_runner = MagicMock()
        mock_runner.run.return_value = CommandResult(
            exit_code=0,
            stdout='{"kind": "Status", "apiVersion": "v1"}',
            stderr="",
            duration=0.1,
        )
        validation = K8sApiServerMetricsCheck(runner=mock_runner, config={})
        result = validation.execute()
        assert result["passed"] is False
        assert "not in Prometheus" in result["error"]

    def test_expected_metrics_must_be_a_list(self) -> None:
        """Non-list expected_metrics config fails fast with a typed message."""
        mock_runner = MagicMock()
        validation = K8sApiServerMetricsCheck(
            runner=mock_runner, config={"expected_metrics": "apiserver_request_total"}
        )
        result = validation.execute()
        assert result["passed"] is False
        assert "expected_metrics" in result["error"]
        assert "list" in result["error"]
        mock_runner.run.assert_not_called()

    def test_expected_metrics_rejects_non_string_elements(self) -> None:
        """List with non-string or empty elements fails fast without running kubectl."""
        mock_runner = MagicMock()
        validation = K8sApiServerMetricsCheck(
            runner=mock_runner, config={"expected_metrics": ["apiserver_request_total", 123]}
        )
        result = validation.execute()
        assert result["passed"] is False
        assert "expected_metrics" in result["error"]
        mock_runner.run.assert_not_called()

        validation = K8sApiServerMetricsCheck(
            runner=mock_runner, config={"expected_metrics": ["apiserver_request_total", ""]}
        )
        result = validation.execute()
        assert result["passed"] is False
        assert "expected_metrics" in result["error"]
        mock_runner.run.assert_not_called()
