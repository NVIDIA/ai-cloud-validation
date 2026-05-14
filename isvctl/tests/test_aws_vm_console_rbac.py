# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Tests for the AWS VM console RBAC reference script."""

from __future__ import annotations

import json
from types import ModuleType
from typing import Any

from botocore.exceptions import ClientError

from .conftest import load_vm_script


class FakeSts:
    """Fake STS client for account ID resolution."""

    def __init__(
        self,
        account_id: str = "123456789012",
        partition: str = "aws",
        include_arn: bool = True,
    ) -> None:
        """Initialize the fake identity response."""
        self.account_id = account_id
        self.partition = partition
        self.include_arn = include_arn

    def get_caller_identity(self) -> dict[str, str]:
        """Return a fake caller identity."""
        identity = {"Account": self.account_id}
        if self.include_arn:
            identity["Arn"] = f"arn:{self.partition}:sts::{self.account_id}:assumed-role/TestRole/session"
        return identity


class FakeEc2:
    """Fake EC2 client for serial console access status."""

    def __init__(self, serial_access_enabled: bool = True) -> None:
        """Initialize the fake serial console access status."""
        self.serial_access_enabled = serial_access_enabled

    def get_serial_console_access_status(self) -> dict[str, bool]:
        """Return fake EC2 serial console access status."""
        return {"SerialConsoleAccessEnabled": self.serial_access_enabled}


def _client_error(operation: str, code: str = "TestFailure") -> ClientError:
    """Build a botocore ClientError for fake IAM failures."""
    return ClientError({"Error": {"Code": code, "Message": f"{operation} failed"}}, operation)


class FakeConsoleRbacIam:
    """Fake IAM client for console RBAC principal policy simulation."""

    def __init__(
        self,
        empty_results: bool = False,
        force_allowed_policy_resource: str | None = None,
        fail_delete_user: bool = False,
    ) -> None:
        """Initialize fake IAM state and optional failure modes."""
        self.empty_results = empty_results
        self.force_allowed_policy_resource = force_allowed_policy_resource
        self.fail_delete_user = fail_delete_user
        self.simulation_calls: list[dict[str, Any]] = []
        self.create_user_calls: list[dict[str, Any]] = []
        self.put_user_policy_calls: list[dict[str, Any]] = []
        self.deleted_users: list[str] = []
        self.deleted_policies: list[dict[str, str]] = []
        self.users: dict[str, dict[str, Any]] = {}

    def create_user(self, UserName: str, Tags: list[dict[str, str]]) -> dict[str, dict[str, str]]:
        """Create a fake IAM user."""
        arn = f"arn:aws:iam::123456789012:user/{UserName}"
        self.create_user_calls.append({"UserName": UserName, "Tags": Tags})
        self.users[UserName] = {"Arn": arn, "Tags": Tags, "Policies": {}}
        return {"User": {"UserName": UserName, "Arn": arn}}

    def put_user_policy(self, UserName: str, PolicyName: str, PolicyDocument: str) -> None:
        """Attach a fake inline policy to a user."""
        policy = json.loads(PolicyDocument)
        if self.force_allowed_policy_resource:
            for statement in policy.get("Statement", []):
                if statement.get("Action") == "ec2-instance-connect:SendSerialConsoleSSHPublicKey":
                    statement["Resource"] = self.force_allowed_policy_resource
            PolicyDocument = json.dumps(policy)

        self.put_user_policy_calls.append(
            {
                "UserName": UserName,
                "PolicyName": PolicyName,
                "PolicyDocument": PolicyDocument,
            }
        )
        self.users[UserName]["Policies"][PolicyName] = PolicyDocument

    def list_user_policies(self, UserName: str) -> dict[str, list[str]]:
        """List fake inline policy names for a user."""
        return {"PolicyNames": list(self.users[UserName]["Policies"])}

    def delete_user_policy(self, UserName: str, PolicyName: str) -> None:
        """Delete a fake inline policy from a user."""
        self.deleted_policies.append({"UserName": UserName, "PolicyName": PolicyName})
        self.users[UserName]["Policies"].pop(PolicyName, None)

    def delete_user(self, UserName: str) -> None:
        """Delete a fake IAM user."""
        if self.fail_delete_user:
            raise _client_error("DeleteUser")
        if self.users[UserName]["Policies"]:
            raise _client_error("DeleteUser", code="DeleteConflict")
        self.deleted_users.append(UserName)
        self.users.pop(UserName, None)

    def simulate_principal_policy(
        self,
        PolicySourceArn: str,
        ActionNames: list[str],
        ResourceArns: list[str],
    ) -> dict[str, list[dict[str, str]]]:
        """Evaluate a fake user's inline policies against one action and resource."""
        self.simulation_calls.append(
            {
                "PolicySourceArn": PolicySourceArn,
                "ActionNames": ActionNames,
                "ResourceArns": ResourceArns,
            }
        )
        if self.empty_results:
            return {"EvaluationResults": []}

        action = ActionNames[0]
        resource = ResourceArns[0]
        decision = "implicitDeny"

        for user in self.users.values():
            if user["Arn"] != PolicySourceArn:
                continue
            for policy_document in user["Policies"].values():
                policy = json.loads(policy_document)
                for statement in policy.get("Statement", []):
                    actions = statement.get("Action", [])
                    resources = statement.get("Resource", [])
                    if isinstance(actions, str):
                        actions = [actions]
                    if isinstance(resources, str):
                        resources = [resources]
                    action_matches = action in actions or "*" in actions
                    resource_matches = resource in resources or "*" in resources
                    if statement.get("Effect") == "Allow" and action_matches and resource_matches:
                        decision = "allowed"

        return {
            "EvaluationResults": [{"EvalActionName": action, "EvalResourceName": resource, "EvalDecision": decision}]
        }


def _run_fake_console_rbac(
    module: ModuleType,
    iam: FakeConsoleRbacIam | None = None,
    sts: FakeSts | None = None,
    ec2: FakeEc2 | None = None,
    instance_id: str = "i-0123456789abcdef0",
    region: str = "us-west-2",
) -> tuple[dict[str, Any], FakeConsoleRbacIam]:
    """Run the console RBAC helper with fake AWS clients."""
    fake_iam = iam or FakeConsoleRbacIam()
    fake_sts = sts or FakeSts()
    fake_ec2 = ec2 or FakeEc2()
    result = module._run_console_rbac_check(fake_iam, fake_sts, fake_ec2, instance_id, region)
    return result, fake_iam


def test_denied_principal_without_policy_is_denied() -> None:
    """A policy without console rights produces a passing denied subtest."""
    module = load_vm_script("console_rbac.py")
    result, _iam = _run_fake_console_rbac(module)

    subtest = result["tests"]["denied_principal_cannot_access_console"]
    assert subtest["passed"] is True
    assert subtest["decision"] == "implicitDeny"
    assert subtest["principal"].startswith("arn:aws:iam::123456789012:user/isv-console-rbac-test-denied-")


def test_allowed_principal_with_scoped_policy_is_allowed() -> None:
    """Allowed principal with a target-instance policy is allowed."""
    module = load_vm_script("console_rbac.py")
    result, _iam = _run_fake_console_rbac(module)

    subtest = result["tests"]["allowed_principal_can_access_console"]
    assert subtest["passed"] is True
    assert subtest["decision"] == "allowed"
    assert subtest["principal"].startswith("arn:aws:iam::123456789012:user/isv-console-rbac-test-allowed-")
    assert subtest["serial_access_enabled"] is True
    assert subtest["resource"] == "arn:aws:ec2:us-west-2:123456789012:instance/i-0123456789abcdef0"


def test_console_rbac_skips_when_serial_console_access_is_disabled() -> None:
    """Account-level serial console disablement makes console RBAC not applicable."""
    module = load_vm_script("console_rbac.py")
    result, _iam = _run_fake_console_rbac(module, ec2=FakeEc2(serial_access_enabled=False))

    assert result["success"] is True
    assert result["skipped"] is True
    assert result["serial_access_enabled"] is False
    assert result["skip_reason"] == "EC2 serial console access is disabled for this account or region"
    assert all(test["passed"] and test["skipped"] for test in result["tests"].values())


def test_serial_console_disabled_skip_avoids_temporary_iam_users() -> None:
    """Serial-console-disabled environments skip before creating IAM users."""
    module = load_vm_script("console_rbac.py")
    result, iam = _run_fake_console_rbac(module, ec2=FakeEc2(serial_access_enabled=False))

    assert result["skipped"] is True
    assert iam.create_user_calls == []
    assert iam.put_user_policy_calls == []
    assert iam.simulation_calls == []


def test_allowed_principal_is_denied_for_other_instance() -> None:
    """Allowed principal remains denied for an unscoped instance ARN."""
    module = load_vm_script("console_rbac.py")
    result, _iam = _run_fake_console_rbac(module)

    subtest = result["tests"]["allowed_principal_is_resource_scoped"]
    assert subtest["passed"] is True
    assert subtest["decision"] == "implicitDeny"
    assert subtest["resource"] == "arn:aws:ec2:us-west-2:123456789012:instance/i-00000000000000000"
    assert result["access_restricted"] is True
    assert result["success"] is True


def test_unscoped_attached_policy_fails_resource_scoping() -> None:
    """An actual unscoped inline policy on the allowed principal fails the check."""
    module = load_vm_script("console_rbac.py")
    iam = FakeConsoleRbacIam(force_allowed_policy_resource="*")
    result, _iam = _run_fake_console_rbac(module, iam=iam)

    subtest = result["tests"]["allowed_principal_is_resource_scoped"]
    assert result["success"] is False
    assert result["access_restricted"] is False
    assert subtest["passed"] is False
    assert subtest["decision"] == "allowed"
    assert len(iam.simulation_calls) == 3
    assert all("PolicySourceArn" in call for call in iam.simulation_calls)


def test_instance_arn_uses_partition_from_sts_identity() -> None:
    """Simulated instance ARNs use the caller's AWS partition."""
    module = load_vm_script("console_rbac.py")
    result, _iam = _run_fake_console_rbac(
        module,
        sts=FakeSts(partition="aws-us-gov"),
        region="us-gov-west-1",
    )

    subtest = result["tests"]["allowed_principal_can_access_console"]
    assert subtest["passed"] is True
    assert subtest["resource"] == "arn:aws-us-gov:ec2:us-gov-west-1:123456789012:instance/i-0123456789abcdef0"


def test_instance_arn_partition_falls_back_to_region_metadata() -> None:
    """Simulated instance ARNs use region metadata when STS omits the caller ARN."""
    module = load_vm_script("console_rbac.py")
    result, _iam = _run_fake_console_rbac(
        module,
        sts=FakeSts(include_arn=False),
        region="cn-north-1",
    )

    subtest = result["tests"]["allowed_principal_can_access_console"]
    assert subtest["passed"] is True
    assert subtest["resource"] == "arn:aws-cn:ec2:cn-north-1:123456789012:instance/i-0123456789abcdef0"


def test_empty_iam_simulation_results_mark_result_failed() -> None:
    """Empty IAM simulation output is reported as a failed script result."""
    module = load_vm_script("console_rbac.py")
    iam = FakeConsoleRbacIam(empty_results=True)
    result, _iam = _run_fake_console_rbac(module, iam=iam)

    assert result["success"] is False
    assert result["access_restricted"] is False
    assert result["error"] == "IAM simulation returned no evaluation results"


def test_temporary_users_are_tagged_and_cleaned_up() -> None:
    """Temporary IAM users are owned with tags and deleted after simulation."""
    module = load_vm_script("console_rbac.py")
    result, iam = _run_fake_console_rbac(module)

    assert result["success"] is True
    assert len(iam.create_user_calls) == 2
    assert all({"Key": "CreatedBy", "Value": "isvtest"} in call["Tags"] for call in iam.create_user_calls)
    assert len(iam.deleted_users) == 2
    assert iam.users == {}


def test_cleanup_failure_marks_result_failed() -> None:
    """Owned cleanup failures fail the script result."""
    module = load_vm_script("console_rbac.py")
    result, _iam = _run_fake_console_rbac(module, iam=FakeConsoleRbacIam(fail_delete_user=True))

    assert result["success"] is False
    assert "cleanup_errors" in result
    assert "Cleanup failed" in result["error"]
