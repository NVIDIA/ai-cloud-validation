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

"""Tests for AWS observability reference scripts."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from botocore.exceptions import ClientError

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
AWS_OBSERVABILITY_SCRIPTS = ISVCTL_ROOT / "configs" / "providers" / "aws" / "scripts" / "observability"


def _client_error(operation_name: str, code: str = "AccessDenied", message: str = "denied") -> ClientError:
    """Create a botocore ClientError for fake AWS client failures."""
    return ClientError({"Error": {"Code": code, "Message": message}}, operation_name)


def _load_script(script_name: str) -> ModuleType:
    """Load an AWS observability script as a module."""
    script_path = AWS_OBSERVABILITY_SCRIPTS / script_name
    spec = importlib.util.spec_from_file_location(script_path.stem, script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[script_path.stem] = module
    spec.loader.exec_module(module)
    return module


class FakeLogsClient:
    """Fake CloudWatch Logs client that records log groups."""

    def __init__(self) -> None:
        """Initialize fake client state."""
        self.created_log_groups: list[str] = []

    def create_log_group(self, *, logGroupName: str) -> None:
        """Record the requested log group creation."""
        self.created_log_groups.append(logGroupName)


class FakeIamClient:
    """Fake IAM client that records role and policy creation."""

    def __init__(self, *, partition: str = "aws") -> None:
        """Initialize fake client state."""
        self.partition = partition
        self.created_roles: list[str] = []
        self.created_role_tags: list[list[dict[str, str]]] = []
        self.policies: list[tuple[str, str]] = []
        self.policy_documents: list[dict[str, Any]] = []

    def create_role(
        self,
        *,
        RoleName: str,
        AssumeRolePolicyDocument: str,
        Tags: list[dict[str, str]],
    ) -> dict[str, Any]:
        """Record role creation and return a role ARN."""
        json.loads(AssumeRolePolicyDocument)
        self.created_roles.append(RoleName)
        self.created_role_tags.append(Tags)
        return {"Role": {"Arn": f"arn:{self.partition}:iam::123456789012:role/{RoleName}"}}

    def put_role_policy(self, *, RoleName: str, PolicyName: str, PolicyDocument: str) -> None:
        """Record inline policy creation."""
        self.policy_documents.append(json.loads(PolicyDocument))
        self.policies.append((RoleName, PolicyName))


class ExistingRoleIamClient(FakeIamClient):
    """Fake IAM client that reports a pre-existing role."""

    def __init__(self, *, role_tags: list[dict[str, str]] | None = None) -> None:
        """Initialize fake client state."""
        super().__init__()
        self.role_tags = role_tags or []

    def create_role(
        self,
        *,
        RoleName: str,
        AssumeRolePolicyDocument: str,
        Tags: list[dict[str, str]],
    ) -> dict[str, Any]:
        """Raise EntityAlreadyExists for role creation."""
        raise _client_error("CreateRole", code="EntityAlreadyExists", message="role exists")

    def get_role(self, *, RoleName: str) -> dict[str, Any]:
        """Return the existing role ARN."""
        return {"Role": {"Arn": f"arn:aws:iam::123456789012:role/{RoleName}"}}

    def list_role_tags(self, *, RoleName: str) -> dict[str, Any]:
        """Return configured role tags."""
        return {"Tags": self.role_tags}


class FailingGetRoleIamClient(ExistingRoleIamClient):
    """Fake IAM client that fails when loading an existing role."""

    def get_role(self, *, RoleName: str) -> dict[str, Any]:
        """Raise an AWS error instead of returning the existing role."""
        raise _client_error("GetRole")


class FakeEc2FlowLogCreateClient:
    """Fake EC2 client that records Flow Log create requests."""

    def __init__(self) -> None:
        """Initialize fake client state."""
        self.requests: list[dict[str, Any]] = []

    def create_flow_logs(self, **kwargs: Any) -> dict[str, Any]:
        """Record Flow Log creation and return a fake ID."""
        self.requests.append(kwargs)
        return {"FlowLogIds": ["fl-123"]}


class FailingEc2FlowLogCreateClient:
    """Fake EC2 client that raises on Flow Log creation."""

    def __init__(self) -> None:
        """Initialize fake client state."""
        self.requests: list[dict[str, Any]] = []

    def create_flow_logs(self, **kwargs: Any) -> dict[str, Any]:
        """Record Flow Log creation and raise an AWS error."""
        self.requests.append(kwargs)
        raise _client_error("CreateFlowLogs")


class FailingPolicyIamClient(FakeIamClient):
    """Fake IAM client that raises when attaching the publish policy."""

    def put_role_policy(self, *, RoleName: str, PolicyName: str, PolicyDocument: str) -> None:
        """Raise an AWS error instead of recording inline policy creation."""
        json.loads(PolicyDocument)
        raise _client_error("PutRolePolicy")


def test_setup_vpc_flow_logs_creates_all_traffic_flow_log() -> None:
    """Setup creates CloudWatch destination, IAM role, and all-traffic Flow Log."""
    script = _load_script("setup_vpc_flow_logs.py")
    ec2 = FakeEc2FlowLogCreateClient()
    logs = FakeLogsClient()
    iam = FakeIamClient()

    result = script.setup_vpc_flow_logs(
        ec2,
        logs,
        iam,
        vpc_id="vpc-123",
        region="us-west-2",
        name="isv-observability",
    )

    assert result["success"] is True
    assert result["platform"] == "observability"
    assert result["test_name"] == "setup_vpc_flow_logs"
    assert result["network_id"] == "vpc-123"
    assert result["flow_log_id"] == "fl-123"
    assert result["traffic_type"] == "ALL"
    assert result["log_group_name"].startswith("/aws/vpc/flowlogs/isv-observability-vpc-123")
    assert ec2.requests[0]["TrafficType"] == "ALL"
    assert ec2.requests[0]["ResourceIds"] == ["vpc-123"]
    assert logs.created_log_groups == [result["log_group_name"]]
    assert iam.created_roles == [result["role_name"]]
    assert {"Key": "CreatedBy", "Value": "isvtest"} in iam.created_role_tags[0]


def test_setup_vpc_flow_logs_demo_mode_skips_aws_clients(monkeypatch: Any, capsys: Any) -> None:
    """Demo mode emits setup evidence without creating AWS clients."""
    monkeypatch.setenv("ISVCTL_DEMO_MODE", "1")
    script = _load_script("setup_vpc_flow_logs.py")
    client_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fail_if_called(*args: Any, **kwargs: Any) -> None:
        client_calls.append((args, kwargs))
        raise AssertionError("boto3.client must not be called in demo mode")

    monkeypatch.setattr(script.boto3, "client", fail_if_called)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "setup_vpc_flow_logs.py",
            "--region",
            "demo-region",
            "--vpc-id",
            "vpc-demo",
            "--name",
            "demo-name",
        ],
    )

    exit_code = script.main()

    result: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert client_calls == []
    assert result["success"] is True
    assert result["platform"] == "observability"
    assert result["test_name"] == "setup_vpc_flow_logs"
    assert result["network_id"] == "vpc-demo"
    assert result["region"] == "demo-region"
    assert result["flow_log_id"]
    assert result["log_destination"] == result["log_group_name"]
    assert result["traffic_type"] == "ALL"


def test_setup_vpc_flow_logs_uses_role_partition_for_publish_policy() -> None:
    """Setup uses the IAM role partition for CloudWatch Logs policy resources."""
    script = _load_script("setup_vpc_flow_logs.py")
    ec2 = FakeEc2FlowLogCreateClient()
    logs = FakeLogsClient()
    iam = FakeIamClient(partition="aws-us-gov")

    result = script.setup_vpc_flow_logs(
        ec2,
        logs,
        iam,
        vpc_id="vpc-123",
        region="us-gov-west-1",
        name="isv-observability",
    )

    assert result["success"] is True
    resources = iam.policy_documents[0]["Statement"][0]["Resource"]
    assert resources[0].startswith("arn:aws-us-gov:logs:us-gov-west-1:123456789012:log-group:")


def test_setup_vpc_flow_logs_refuses_unowned_existing_role() -> None:
    """Setup must not adopt an existing role unless it is tagged as suite-owned."""
    script = _load_script("setup_vpc_flow_logs.py")
    ec2 = FakeEc2FlowLogCreateClient()
    logs = FakeLogsClient()
    iam = ExistingRoleIamClient(role_tags=[{"Key": "CreatedBy", "Value": "someone-else"}])

    result = script.setup_vpc_flow_logs(
        ec2,
        logs,
        iam,
        vpc_id="vpc-123",
        region="us-west-2",
        name="isv-observability",
    )

    assert result["success"] is False
    assert result["error_type"] == "resource_conflict"
    assert "not tagged CreatedBy=isvtest" in result["error"]
    assert ec2.requests == []
    assert iam.policies == []


def test_setup_vpc_flow_logs_existing_role_get_failure_returns_structured_error() -> None:
    """Existing role adoption failures return structured setup output."""
    script = _load_script("setup_vpc_flow_logs.py")
    ec2 = FakeEc2FlowLogCreateClient()
    logs = FakeLogsClient()
    iam = FailingGetRoleIamClient(role_tags=[{"Key": "CreatedBy", "Value": "isvtest"}])

    try:
        result = script.setup_vpc_flow_logs(
            ec2,
            logs,
            iam,
            vpc_id="vpc-123",
            region="us-west-2",
            name="isv-observability",
        )
    except ClientError as e:
        raise AssertionError("setup should return a structured failure result") from e

    assert result["success"] is False
    assert result["flow_log_id"] == ""
    assert result["log_destination"] == result["log_group_name"]
    assert result["error_type"] == "access_denied"
    assert ec2.requests == []
    assert iam.policies == []


def test_setup_vpc_flow_logs_create_failure_preserves_teardown_fields(monkeypatch: Any) -> None:
    """Setup failure after partial creation still emits fields needed by teardown."""
    script = _load_script("setup_vpc_flow_logs.py")
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)
    ec2 = FailingEc2FlowLogCreateClient()
    logs = FakeLogsClient()
    iam = FakeIamClient()

    result = script.setup_vpc_flow_logs(
        ec2,
        logs,
        iam,
        vpc_id="vpc-123",
        region="us-west-2",
        name="isv-observability",
    )

    assert result["success"] is False
    assert result["flow_log_id"] == ""
    assert result["log_destination"] == result["log_group_name"]
    assert result["role_arn"].endswith(f":role/{result['role_name']}")
    assert result["policy_name"] == "isv-observability-publish-flow-logs"
    assert result["error_type"] == "access_denied"
    assert len(ec2.requests) == 5


def test_setup_vpc_flow_logs_policy_failure_returns_partial_teardown_fields() -> None:
    """Policy attachment failure returns structured partial setup output."""
    script = _load_script("setup_vpc_flow_logs.py")
    ec2 = FakeEc2FlowLogCreateClient()
    logs = FakeLogsClient()
    iam = FailingPolicyIamClient()

    try:
        result = script.setup_vpc_flow_logs(
            ec2,
            logs,
            iam,
            vpc_id="vpc-123",
            region="us-west-2",
            name="isv-observability",
        )
    except ClientError as e:
        raise AssertionError("setup should return a structured failure result") from e

    assert result["success"] is False
    assert result["flow_log_id"] == ""
    assert result["log_destination"] == result["log_group_name"]
    assert result["role_arn"].endswith(f":role/{result['role_name']}")
    assert result["error_type"] == "access_denied"
    assert logs.created_log_groups == [result["log_group_name"]]
    assert iam.created_roles == [result["role_name"]]
    assert ec2.requests == []


class FakeEc2FlowLogDescribeClient:
    """Fake EC2 client that returns configured Flow Logs."""

    def __init__(self, flow_logs: list[dict[str, Any]]) -> None:
        """Initialize fake client state."""
        self.flow_logs = flow_logs

    def describe_flow_logs(self, *, Filters: list[dict[str, Any]]) -> dict[str, Any]:
        """Return Flow Logs matching the expected network-id filter."""
        assert Filters == [{"Name": "resource-id", "Values": ["vpc-123"]}]
        return {"FlowLogs": self.flow_logs}


class FakeLogsDescribeClient:
    """Fake CloudWatch Logs client that returns matching log groups."""

    def __init__(self, log_groups: list[dict[str, str]]) -> None:
        """Initialize fake client state."""
        self.log_groups = log_groups

    def describe_log_groups(self, *, logGroupNamePrefix: str) -> dict[str, Any]:
        """Return log groups that start with the requested prefix."""
        return {
            "logGroups": [group for group in self.log_groups if group["logGroupName"].startswith(logGroupNamePrefix)]
        }


class FailingLogsDescribeClient:
    """Fake CloudWatch Logs client that raises for log group lookups."""

    def describe_log_groups(self, *, logGroupNamePrefix: str) -> dict[str, Any]:
        """Raise an AWS API error instead of returning log groups."""
        raise _client_error("DescribeLogGroups", message="raw provider detail")


def test_vpc_flow_logs_aspect_emits_observability_contract() -> None:
    """VPC Flow Logs aspect emits the provider-neutral observability contract."""
    script = _load_script("log_availability_test.py")
    ec2 = FakeEc2FlowLogDescribeClient(
        [
            {
                "FlowLogId": "fl-123",
                "FlowLogStatus": "ACTIVE",
                "ResourceId": "vpc-123",
                "TrafficType": "ALL",
                "LogDestinationType": "cloud-watch-logs",
                "LogGroupName": "/aws/vpc/flowlogs/isv",
            }
        ]
    )
    logs = FakeLogsDescribeClient([{"logGroupName": "/aws/vpc/flowlogs/isv"}])

    result = script.check_vpc_flow_logs(ec2, logs, network_id="vpc-123")

    assert result["success"] is True
    assert result["platform"] == "observability"
    assert result["test_name"] == "vpc_flow_logs"
    assert "network_id" not in result
    assert "flow_log_id" not in result
    assert set(result["tests"]) == {
        "flow_log_endpoint_reachable",
        "flow_logs_configured",
        "traffic_type_all",
        "log_destination_accessible",
    }
    probes = result["tests"]["traffic_type_all"]["probes"]
    assert probes["network_id"] == "vpc-123"
    assert probes["log_destination"] == "/aws/vpc/flowlogs/isv"
    assert probes["traffic_type"] == "ALL"


def test_vpc_flow_logs_aspect_fails_when_traffic_type_is_not_all() -> None:
    """VPC Flow Logs aspect fails when AWS Flow Log traffic type is not ALL."""
    script = _load_script("log_availability_test.py")
    ec2 = FakeEc2FlowLogDescribeClient(
        [
            {
                "FlowLogId": "fl-123",
                "FlowLogStatus": "ACTIVE",
                "ResourceId": "vpc-123",
                "TrafficType": "ACCEPT",
                "LogDestinationType": "cloud-watch-logs",
                "LogGroupName": "/aws/vpc/flowlogs/isv",
            }
        ]
    )
    logs = FakeLogsDescribeClient([{"logGroupName": "/aws/vpc/flowlogs/isv"}])

    result = script.check_vpc_flow_logs(ec2, logs, network_id="vpc-123")

    assert result["success"] is False
    assert result["tests"]["traffic_type_all"]["passed"] is False
    assert "ALL" in result["tests"]["traffic_type_all"]["error"]


def test_vpc_flow_logs_aspect_redacts_log_destination_client_error() -> None:
    """Log destination probe failures return concise generic diagnostics."""
    script = _load_script("log_availability_test.py")
    ec2 = FakeEc2FlowLogDescribeClient(
        [
            {
                "FlowLogId": "fl-123",
                "FlowLogStatus": "ACTIVE",
                "ResourceId": "vpc-123",
                "TrafficType": "ALL",
                "LogDestinationType": "cloud-watch-logs",
                "LogGroupName": "/aws/vpc/flowlogs/isv",
            }
        ]
    )

    result = script.check_vpc_flow_logs(ec2, FailingLogsDescribeClient(), network_id="vpc-123")

    assert result["success"] is False
    failure = result["tests"]["log_destination_accessible"]
    assert failure["passed"] is False
    assert failure["error"] == "AWS API error while checking log destination accessibility"
    assert "raw provider detail" not in json.dumps(result)


def test_vpc_flow_logs_aspect_selects_requested_flow_log_id() -> None:
    """VPC Flow Logs aspect inspects the setup-created Flow Log when requested."""
    script = _load_script("log_availability_test.py")
    ec2 = FakeEc2FlowLogDescribeClient(
        [
            {
                "FlowLogId": "fl-stale",
                "FlowLogStatus": "ACTIVE",
                "ResourceId": "vpc-123",
                "TrafficType": "ACCEPT",
                "LogDestinationType": "cloud-watch-logs",
                "LogGroupName": "/aws/vpc/flowlogs/stale",
            },
            {
                "FlowLogId": "fl-target",
                "FlowLogStatus": "ACTIVE",
                "ResourceId": "vpc-123",
                "TrafficType": "ALL",
                "LogDestinationType": "cloud-watch-logs",
                "LogGroupName": "/aws/vpc/flowlogs/target",
            },
        ]
    )
    logs = FakeLogsDescribeClient([{"logGroupName": "/aws/vpc/flowlogs/target"}])

    result = script.check_vpc_flow_logs(ec2, logs, network_id="vpc-123", flow_log_id="fl-target")

    assert result["success"] is True
    probes = result["tests"]["traffic_type_all"]["probes"]
    assert probes["flow_log_id"] == "fl-target"
    assert probes["log_destination"] == "/aws/vpc/flowlogs/target"


def test_log_availability_main_rejects_non_positive_max_age_minutes(
    monkeypatch: Any,
    capsys: Any,
) -> None:
    """The CLI rejects non-positive host log sampling windows before probing."""
    script = _load_script("log_availability_test.py")
    client_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fail_if_called(*args: Any, **kwargs: Any) -> None:
        client_calls.append((args, kwargs))
        raise AssertionError("boto3.client must not be called for invalid max age")

    monkeypatch.setattr(script.boto3, "client", fail_if_called)
    monkeypatch.setattr(script, "wait_for_ssh", fail_if_called)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "log_availability_test.py",
            "--aspect",
            "host_syslogs",
            "--host",
            "203.0.113.10",
            "--max-age-minutes",
            "0",
        ],
    )

    exit_code = script.main()

    result: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert client_calls == []
    assert result == {
        "success": False,
        "platform": "observability",
        "test_name": "host_syslogs",
        "error": "--max-age-minutes must be greater than 0",
    }


def test_host_syslogs_aspect_emits_observability_contract(monkeypatch: Any) -> None:
    """Host syslog aspect emits the provider-neutral observability contract."""
    script = _load_script("log_availability_test.py")
    monkeypatch.setattr(script, "wait_for_ssh", lambda host, user, key_file, max_attempts=20, interval=10: True)

    def fake_ssh_run(host: str, user: str, key_file: str, cmd: str) -> tuple[int, str, str]:
        """Return a recent journalctl entry."""
        if "journalctl" in cmd:
            return 0, "2026-05-22T13:21:00+0000 host systemd[1]: started\n", ""
        return 0, "", ""

    monkeypatch.setattr(script, "ssh_run", fake_ssh_run)

    result = script.check_host_syslogs(
        host="203.0.113.10",
        ssh_user="ubuntu",
        key_file="/tmp/key.pem",
        max_age_minutes=5,
    )

    assert result["success"] is True
    assert result["platform"] == "observability"
    assert result["test_name"] == "host_syslogs"
    assert "host" not in result
    probes = result["tests"]["entries_recent"]["probes"]
    assert probes["hosts_checked"] == 1
    assert probes["log_source"] == "journalctl"
    assert probes["entry_count"] == 1
    assert probes["latest_timestamp"] == "2026-05-22T13:21:00+0000"


def test_host_syslogs_aspect_fails_when_ssh_is_unavailable(monkeypatch: Any) -> None:
    """Host syslog aspect fails cleanly when SSH is unavailable."""
    script = _load_script("log_availability_test.py")
    monkeypatch.setattr(script, "wait_for_ssh", lambda host, user, key_file, max_attempts=20, interval=10: False)

    result = script.check_host_syslogs(
        host="203.0.113.10",
        ssh_user="ubuntu",
        key_file="/tmp/key.pem",
        max_age_minutes=5,
    )

    assert result["success"] is False
    assert result["tests"]["syslog_endpoint_reachable"]["passed"] is False
    assert "SSH" in result["tests"]["syslog_endpoint_reachable"]["error"]


def test_bmc_sel_logs_aspect_emits_provider_hidden_contract() -> None:
    """AWS BMC SEL observability reports provider-hidden evidence instead of being excluded."""
    script = _load_script("log_availability_test.py")

    result = script.check_bmc_sel_logs(region="us-west-2")

    assert result["success"] is True
    assert result["platform"] == "observability"
    assert result["test_name"] == "bmc_sel_logs"
    assert "bmc_endpoints_checked" not in result
    assert "provider_hidden" not in result
    assert set(result["tests"]) == {
        "sel_log_endpoint_reachable",
        "sel_log_source_present",
        "sel_entries_queryable",
    }
    for subtest in result["tests"].values():
        assert subtest["passed"] is True
        assert subtest["provider_hidden"] is True
        assert subtest["probes"]["bmc_endpoints_checked"] == 0
        assert "provider-owned" in subtest["message"]


def test_bmc_gpu_telemetry_aspect_emits_provider_hidden_contract() -> None:
    """AWS BMC GPU telemetry reports provider-hidden evidence instead of being excluded."""
    script = _load_script("log_availability_test.py")

    result = script.check_bmc_gpu_telemetry(region="us-west-2")

    assert result["success"] is True
    assert result["platform"] == "observability"
    assert result["test_name"] == "bmc_gpu_telemetry"
    assert "bmc_endpoints_checked" not in result
    assert "provider_hidden" not in result
    assert set(result["tests"]) == {
        "telemetry_endpoint_reachable",
        "gpu_metrics_present",
        "host_os_gap_identified",
        "telemetry_samples_recent",
    }
    for subtest in result["tests"].values():
        assert subtest["passed"] is True
        assert subtest["provider_hidden"] is True
        assert subtest["probes"]["bmc_endpoints_checked"] == 0
        assert "provider-owned" in subtest["message"]


class FakeEc2FlowLogDeleteClient:
    """Fake EC2 client that records deleted Flow Logs."""

    def __init__(self) -> None:
        """Initialize fake client state."""
        self.deleted: list[str] = []

    def delete_flow_logs(self, *, FlowLogIds: list[str]) -> dict[str, Any]:
        """Record deleted Flow Log IDs."""
        self.deleted.extend(FlowLogIds)
        return {"Unsuccessful": []}


class FakeLogsDeleteClient:
    """Fake CloudWatch Logs client that records deleted log groups."""

    def __init__(self) -> None:
        """Initialize fake client state."""
        self.deleted: list[str] = []

    def delete_log_group(self, *, logGroupName: str) -> None:
        """Record deleted log group names."""
        self.deleted.append(logGroupName)


class FakeIamDeleteClient:
    """Fake IAM client that records deleted role resources."""

    def __init__(self) -> None:
        """Initialize fake client state."""
        self.deleted_policies: list[tuple[str, str]] = []
        self.deleted_roles: list[str] = []

    def delete_role_policy(self, *, RoleName: str, PolicyName: str) -> None:
        """Record deleted inline role policies."""
        self.deleted_policies.append((RoleName, PolicyName))

    def delete_role(self, *, RoleName: str) -> None:
        """Record deleted IAM roles."""
        self.deleted_roles.append(RoleName)


def test_teardown_vpc_flow_logs_deletes_created_resources() -> None:
    """Teardown deletes Flow Log, log group, policy, and role resources."""
    script = _load_script("teardown_vpc_flow_logs.py")
    ec2 = FakeEc2FlowLogDeleteClient()
    logs = FakeLogsDeleteClient()
    iam = FakeIamDeleteClient()

    result = script.teardown_vpc_flow_logs(
        ec2,
        logs,
        iam,
        flow_log_id="fl-123",
        log_group_name="/aws/vpc/flowlogs/isv",
        role_name="isv-role",
        policy_name="isv-policy",
        skip_destroy=False,
    )

    assert result["success"] is True
    assert ec2.deleted == ["fl-123"]
    assert logs.deleted == ["/aws/vpc/flowlogs/isv"]
    assert iam.deleted_policies == [("isv-role", "isv-policy")]
    assert iam.deleted_roles == ["isv-role"]


class FailingEc2FlowLogDeleteClient(FakeEc2FlowLogDeleteClient):
    """Fake EC2 client that raises on Flow Log deletion."""

    def delete_flow_logs(self, *, FlowLogIds: list[str]) -> dict[str, Any]:
        """Raise a delete failure for the requested Flow Log IDs."""
        self.deleted.extend(FlowLogIds)
        raise _client_error("DeleteFlowLogs")


def test_teardown_vpc_flow_logs_reports_delete_failure_and_continues() -> None:
    """Teardown reports delete failures while continuing best-effort cleanup."""
    script = _load_script("teardown_vpc_flow_logs.py")
    ec2 = FailingEc2FlowLogDeleteClient()
    logs = FakeLogsDeleteClient()
    iam = FakeIamDeleteClient()

    result = script.teardown_vpc_flow_logs(
        ec2,
        logs,
        iam,
        flow_log_id="fl-123",
        log_group_name="/aws/vpc/flowlogs/isv",
        role_name="isv-role",
        policy_name="isv-policy",
        skip_destroy=False,
    )

    assert result["success"] is False
    assert result["resources_destroyed"] is False
    assert result["deleted"]["flow_log_id"] == ""
    assert logs.deleted == ["/aws/vpc/flowlogs/isv"]
    assert iam.deleted_policies == [("isv-role", "isv-policy")]
    assert iam.deleted_roles == ["isv-role"]
    assert result["cleanup_errors"][0]["resource_type"] == "flow_log_id"
    assert result["cleanup_errors"][0]["resource_id"] == "fl-123"
    assert result["cleanup_errors"][0]["error_type"] == "access_denied"
