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

"""Tests for AWS network reference scripts."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from botocore.exceptions import ClientError

ISVCTL_ROOT = Path(__file__).resolve().parents[3]
AWS_NETWORK_SCRIPTS = ISVCTL_ROOT / "configs" / "providers" / "aws" / "scripts" / "network"
MY_ISV_NETWORK_SCRIPTS = ISVCTL_ROOT / "configs" / "providers" / "my-isv" / "scripts" / "network"
STABLE_EGRESS_TEST_NAMES = {"create_instance", "probe_egress_ip", "egress_ip_stable"}
STABLE_EGRESS_TOP_LEVEL_KEYS = {"success", "platform", "test_name", "tests"}
STABLE_EGRESS_TEST_RESULT_KEYS = {"passed", "message", "probes"}


def _load_network_script(script_name: str) -> ModuleType:
    """Load an AWS network script as a module for direct helper testing."""
    script_path = AWS_NETWORK_SCRIPTS / script_name
    spec = importlib.util.spec_from_file_location(f"test_{script_path.stem}", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _client_error(operation_name: str, code: str = "AccessDenied", message: str = "denied") -> ClientError:
    """Create a botocore ClientError for fake AWS client failures."""
    return ClientError({"Error": {"Code": code, "Message": message}}, operation_name)


def test_create_vpc_owned_tag_specification() -> None:
    """Network fixtures must receive ownership tags in their create API call."""
    module = _load_network_script("create_vpc.py")

    assert module.owned_tag_specification("vpc", "isv-shared-vpc-12345678") == [
        {
            "ResourceType": "vpc",
            "Tags": [
                {"Key": "Name", "Value": "isv-shared-vpc-12345678"},
                {"Key": "CreatedBy", "Value": "isvtest"},
            ],
        }
    ]


def _assert_stable_egress_contract(result: dict[str, Any]) -> None:
    """Assert stable egress scripts emit the minimal provider JSON contract."""
    assert set(result) == STABLE_EGRESS_TOP_LEVEL_KEYS
    assert result["success"] is True
    assert result["platform"] == "network"
    assert result["test_name"] == "stable_egress_ip"
    assert set(result["tests"]) == STABLE_EGRESS_TEST_NAMES
    for test_result in result["tests"].values():
        assert set(test_result) <= STABLE_EGRESS_TEST_RESULT_KEYS
        assert isinstance(test_result["passed"], bool)


class FakeServiceScopingEc2:
    """Fake EC2 client covering the calls used by test_service_scoping."""

    def __init__(
        self,
        endpoint_eni_ids: list[str] | None = None,
        delete_endpoint_error: ClientError | None = None,
        *,
        endpoint_deleted_after_delete: bool = True,
        delete_endpoint_unsuccessful: list[dict[str, Any]] | None = None,
        subnet_dependency_failures: int = 0,
        sg_dependency_failures: int = 0,
    ) -> None:
        """Configure ENIs returned by the endpoint and optional delete failure."""
        self.endpoint_eni_ids = endpoint_eni_ids if endpoint_eni_ids is not None else ["eni-endpoint-1"]
        self.delete_endpoint_error = delete_endpoint_error
        self.endpoint_deleted_after_delete = endpoint_deleted_after_delete
        self.delete_endpoint_unsuccessful = delete_endpoint_unsuccessful or []
        self.subnet_dependency_failures = subnet_dependency_failures
        self.sg_dependency_failures = sg_dependency_failures
        self.delete_subnet_attempts = 0
        self.delete_sg_attempts = 0
        self.created_sg_ingress: list[dict[str, Any]] = []
        self.deleted_endpoints: list[str] = []
        self.deleted_subnets: list[str] = []
        self.deleted_sgs: list[str] = []
        self.deleted_enis: list[str] = []

    def create_subnet(self, VpcId: str, CidrBlock: str, AvailabilityZone: str) -> dict[str, Any]:
        """Return a fake subnet."""
        return {"Subnet": {"SubnetId": "subnet-aaa", "VpcId": VpcId, "CidrBlock": CidrBlock}}

    def create_security_group(
        self,
        GroupName: str,
        Description: str,
        VpcId: str,
        TagSpecifications: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Return a fake SG ID."""
        return {"GroupId": "sg-svc"}

    def authorize_security_group_ingress(self, GroupId: str, IpPermissions: list[dict[str, Any]]) -> dict[str, Any]:
        """Record the SG rule that was authorized."""
        self.created_sg_ingress.append({"GroupId": GroupId, "IpPermissions": IpPermissions})
        return {}

    def create_vpc_endpoint(
        self,
        VpcId: str,
        ServiceName: str,
        VpcEndpointType: str,
        SubnetIds: list[str],
        SecurityGroupIds: list[str],
        PrivateDnsEnabled: bool,
        TagSpecifications: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Return a fake VPC interface endpoint."""
        assert VpcEndpointType == "Interface"
        assert ServiceName.startswith("com.amazonaws.")
        assert PrivateDnsEnabled is False
        return {"VpcEndpoint": {"VpcEndpointId": "vpce-svc"}}

    def create_network_interface(self, SubnetId: str, **kwargs: Any) -> dict[str, Any]:
        """Return a fake unrelated ENI without an SG."""
        return {"NetworkInterface": {"NetworkInterfaceId": "eni-other"}}

    def describe_vpc_endpoints(self, VpcEndpointIds: list[str]) -> dict[str, Any]:
        """Report the endpoint with its ENI IDs (or absence after deletion)."""
        if VpcEndpointIds[0] in self.deleted_endpoints and self.endpoint_deleted_after_delete:
            return {"VpcEndpoints": []}
        return {
            "VpcEndpoints": [
                {
                    "VpcEndpointId": VpcEndpointIds[0],
                    "NetworkInterfaceIds": list(self.endpoint_eni_ids),
                    "State": "available",
                }
            ]
        }

    def describe_network_interfaces(self, NetworkInterfaceIds: list[str]) -> dict[str, Any]:
        """Report SG attachment: SG attached to endpoint ENIs, none on the unrelated ENI."""
        nics = []
        for nic_id in NetworkInterfaceIds:
            if nic_id in self.endpoint_eni_ids:
                nics.append({"NetworkInterfaceId": nic_id, "Groups": [{"GroupId": "sg-svc"}]})
            else:
                nics.append({"NetworkInterfaceId": nic_id, "Groups": []})
        return {"NetworkInterfaces": nics}

    def delete_vpc_endpoints(self, VpcEndpointIds: list[str]) -> dict[str, Any]:
        """Delete the endpoint, optionally raising a configured error."""
        if self.delete_endpoint_error:
            raise self.delete_endpoint_error
        self.deleted_endpoints.extend(VpcEndpointIds)
        return {"Unsuccessful": self.delete_endpoint_unsuccessful}

    def delete_network_interface(self, NetworkInterfaceId: str) -> None:
        """Delete a fake ENI."""
        self.deleted_enis.append(NetworkInterfaceId)

    def delete_subnet(self, SubnetId: str) -> None:
        """Delete a fake subnet."""
        self.delete_subnet_attempts += 1
        if self.delete_subnet_attempts <= self.subnet_dependency_failures:
            raise _client_error("DeleteSubnet", "DependencyViolation", "subnet has dependencies")
        self.deleted_subnets.append(SubnetId)

    def delete_security_group(self, GroupId: str) -> None:
        """Delete a fake SG."""
        self.delete_sg_attempts += 1
        if self.delete_sg_attempts <= self.sg_dependency_failures:
            raise _client_error("DeleteSecurityGroup", "DependencyViolation", "SG has dependencies")
        self.deleted_sgs.append(GroupId)


def test_service_scoping_happy_path_attaches_sg_only_to_endpoint_eni(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SG must attach to the endpoint's ENIs and not to the unrelated ENI."""
    module = _load_network_script("sg_scoping_test.py")
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    ec2 = FakeServiceScopingEc2(endpoint_eni_ids=["eni-endpoint-1", "eni-endpoint-2"])

    result = module.test_service_scoping(ec2, "vpc-test", "us-west-2a", "us-west-2")

    assert result["create_sg"]["passed"] is True
    assert result["apply_service_rule"]["passed"] is True
    assert result["service_endpoint_allowed"]["passed"] is True
    assert result["other_endpoint_blocked"]["passed"] is True
    assert result["cleanup"]["passed"] is True
    assert ec2.created_sg_ingress[0]["IpPermissions"][0]["FromPort"] == 443
    assert ec2.created_sg_ingress[0]["IpPermissions"][0]["ToPort"] == 443
    assert ec2.deleted_endpoints == ["vpce-svc"]
    assert ec2.deleted_enis == ["eni-other"]
    assert ec2.deleted_subnets == ["subnet-aaa"]
    assert ec2.deleted_sgs == ["sg-svc"]


def test_service_scoping_records_cleanup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed VPC endpoint deletion is reported via the cleanup result."""
    module = _load_network_script("sg_scoping_test.py")
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    ec2 = FakeServiceScopingEc2(
        endpoint_eni_ids=["eni-endpoint-1"],
        delete_endpoint_error=_client_error("DeleteVpcEndpoints"),
    )

    result = module.test_service_scoping(ec2, "vpc-test", "us-west-2a", "us-west-2")

    assert result["service_endpoint_allowed"]["passed"] is True
    assert result["cleanup"]["passed"] is False
    assert "delete VPC endpoint vpce-svc" in result["cleanup"]["error"]


def test_service_scoping_records_endpoint_delete_unsuccessful(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unsuccessful delete_vpc_endpoints entries should fail cleanup."""
    module = _load_network_script("sg_scoping_test.py")
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    ec2 = FakeServiceScopingEc2(
        endpoint_eni_ids=["eni-endpoint-1"],
        delete_endpoint_unsuccessful=[{"ResourceId": "vpce-svc", "Error": {"Code": "UnauthorizedOperation"}}],
    )

    result = module.test_service_scoping(ec2, "vpc-test", "us-west-2a", "us-west-2")

    assert result["cleanup"]["passed"] is False
    assert "delete_vpc_endpoints reported unsuccessful entries" in result["cleanup"]["error"]
    assert ec2.deleted_enis == ["eni-other"]
    assert ec2.deleted_subnets == ["subnet-aaa"]
    assert ec2.deleted_sgs == ["sg-svc"]


def test_service_scoping_records_endpoint_wait_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Endpoint deletion wait timeouts should be the visible cleanup cause."""
    module = _load_network_script("sg_scoping_test.py")
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    ec2 = FakeServiceScopingEc2(
        endpoint_eni_ids=["eni-endpoint-1"],
        endpoint_deleted_after_delete=False,
    )

    result = module.test_service_scoping(ec2, "vpc-test", "us-west-2a", "us-west-2")

    assert result["cleanup"]["passed"] is False
    assert result["cleanup"]["error"].startswith("delete VPC endpoint vpce-svc: Timed out waiting")
    assert ec2.deleted_enis == ["eni-other"]
    assert ec2.deleted_subnets == ["subnet-aaa"]
    assert ec2.deleted_sgs == ["sg-svc"]


def test_service_scoping_retries_dependency_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Subnet and SG cleanup should retry brief dependency lag after endpoint deletion."""
    module = _load_network_script("sg_scoping_test.py")
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    ec2 = FakeServiceScopingEc2(
        endpoint_eni_ids=["eni-endpoint-1"],
        subnet_dependency_failures=2,
        sg_dependency_failures=1,
    )

    result = module.test_service_scoping(ec2, "vpc-test", "us-west-2a", "us-west-2")

    assert result["cleanup"]["passed"] is True
    assert ec2.delete_subnet_attempts == 3
    assert ec2.delete_sg_attempts == 2
    assert ec2.deleted_subnets == ["subnet-aaa"]
    assert ec2.deleted_sgs == ["sg-svc"]


class FakePortSecurityEc2:
    """Fake EC2 client for port security policy tests."""

    def __init__(
        self,
        *,
        target_rules: list[dict[str, Any]] | None = None,
        other_rules: list[dict[str, Any]] | None = None,
    ) -> None:
        """Configure observed rules for target and unrelated interfaces."""
        self.target_rules = target_rules
        self.other_rules = other_rules if other_rules is not None else []
        self.created_ingress: list[dict[str, Any]] = []
        self.deleted_enis: list[str] = []
        self.deleted_subnets: list[str] = []
        self.deleted_sgs: list[str] = []

    def create_subnet(self, VpcId: str, CidrBlock: str, AvailabilityZone: str) -> dict[str, Any]:
        """Return a fake subnet."""
        return {"Subnet": {"SubnetId": "subnet-port", "VpcId": VpcId, "CidrBlock": CidrBlock}}

    def create_security_group(
        self,
        GroupName: str,
        Description: str,
        VpcId: str,
        TagSpecifications: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Return a fake security group."""
        assert "port security" in Description
        return {"GroupId": "sg-port"}

    def authorize_security_group_ingress(self, GroupId: str, IpPermissions: list[dict[str, Any]]) -> dict[str, Any]:
        """Record the configured ingress policy."""
        self.created_ingress.append({"GroupId": GroupId, "IpPermissions": IpPermissions})
        return {}

    def create_network_interface(self, SubnetId: str, **kwargs: Any) -> dict[str, Any]:
        """Return fake ENIs, with the first one attached to the SG."""
        if kwargs.get("Groups"):
            return {"NetworkInterface": {"NetworkInterfaceId": "eni-target"}}
        return {"NetworkInterface": {"NetworkInterfaceId": "eni-other"}}

    def describe_security_groups(self, GroupIds: list[str]) -> dict[str, Any]:
        """Return configured SG rules."""
        assert GroupIds == ["sg-port"]
        rules = self.target_rules
        if rules is None:
            rules = self.created_ingress[0]["IpPermissions"]
        return {"SecurityGroups": [{"GroupId": "sg-port", "IpPermissions": rules}]}

    def describe_network_interfaces(self, NetworkInterfaceIds: list[str]) -> dict[str, Any]:
        """Return target and unrelated ENI SG attachments."""
        interfaces = []
        for eni_id in NetworkInterfaceIds:
            if eni_id == "eni-target":
                interfaces.append({"NetworkInterfaceId": eni_id, "Groups": [{"GroupId": "sg-port"}]})
            else:
                interfaces.append({"NetworkInterfaceId": eni_id, "Groups": []})
        return {"NetworkInterfaces": interfaces}

    def delete_network_interface(self, NetworkInterfaceId: str) -> None:
        """Delete a fake ENI."""
        self.deleted_enis.append(NetworkInterfaceId)

    def delete_subnet(self, SubnetId: str) -> None:
        """Delete a fake subnet."""
        self.deleted_subnets.append(SubnetId)

    def delete_security_group(self, GroupId: str) -> None:
        """Delete a fake security group."""
        self.deleted_sgs.append(GroupId)


def test_port_security_policy_happy_path_applies_single_port_to_target_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The AWS port security probe should allow only the configured port on the target ENI."""
    module = _load_network_script("sg_port_security_policy.py")
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    ec2 = FakePortSecurityEc2()

    result = module.test_port_security_policy(ec2, "vpc-test", "us-west-2a", allowed_port=8443)

    assert result["create_virtual_interface"]["passed"] is True
    assert result["apply_port_policy"]["passed"] is True
    assert result["allowed_port_permitted"]["passed"] is True
    assert result["unlisted_port_blocked"]["passed"] is True
    assert result["other_interface_unaffected"]["passed"] is True
    assert result["cleanup"]["passed"] is True
    rule = ec2.created_ingress[0]["IpPermissions"][0]
    assert rule["FromPort"] == 8443
    assert rule["ToPort"] == 8443
    assert ec2.deleted_enis == ["eni-target", "eni-other"]
    assert ec2.deleted_subnets == ["subnet-port"]
    assert ec2.deleted_sgs == ["sg-port"]


def test_port_security_policy_fails_when_unlisted_port_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wider observed ingress range must fail the unlisted-port check."""
    module = _load_network_script("sg_port_security_policy.py")
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    ec2 = FakePortSecurityEc2(
        target_rules=[
            {
                "IpProtocol": "tcp",
                "FromPort": 8443,
                "ToPort": 8444,
                "IpRanges": [{"CidrIp": "10.0.0.0/8"}],
            }
        ]
    )

    result = module.test_port_security_policy(ec2, "vpc-test", "us-west-2a", allowed_port=8443)

    assert result["allowed_port_permitted"]["passed"] is True
    assert result["unlisted_port_blocked"]["passed"] is False
    assert "8444" in result["unlisted_port_blocked"]["error"]
    assert result["cleanup"]["passed"] is True


def test_port_security_policy_fails_when_policy_leaks_to_other_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The custom SG must not attach to the unrelated virtual interface."""
    module = _load_network_script("sg_port_security_policy.py")
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    ec2 = FakePortSecurityEc2()

    def leaked_describe_network_interfaces(NetworkInterfaceIds: list[str]) -> dict[str, Any]:
        """Return AWS-shaped ENI descriptions with sg-port attached to every ENI."""
        return {
            "NetworkInterfaces": [
                {"NetworkInterfaceId": eni_id, "Groups": [{"GroupId": "sg-port"}]} for eni_id in NetworkInterfaceIds
            ]
        }

    ec2.describe_network_interfaces = leaked_describe_network_interfaces  # type: ignore[method-assign]

    result = module.test_port_security_policy(ec2, "vpc-test", "us-west-2a", allowed_port=8443)

    assert result["other_interface_unaffected"]["passed"] is False
    assert "leaked" in result["other_interface_unaffected"]["error"]
    assert result["cleanup"]["passed"] is True


def test_port_security_policy_main_emits_full_contract_on_vpc_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """VPC bootstrap failure should still emit every port-security subtest key."""
    module = _load_network_script("sg_port_security_policy.py")
    fake_ec2 = object()
    cleaned_vpcs: list[str] = []

    monkeypatch.setattr(module.boto3, "client", lambda service, region_name: fake_ec2)
    monkeypatch.setattr(
        module,
        "create_test_vpc",
        lambda ec2, cidr, name: {"passed": False, "vpc_id": "vpc-partial", "error": "quota exceeded"},
    )
    monkeypatch.setattr(module, "cleanup_vpc_resources", lambda ec2, vpc_id: cleaned_vpcs.append(vpc_id))
    monkeypatch.setattr(sys, "argv", ["sg_port_security_policy.py", "--region", "us-west-2"])

    exit_code = module.main()

    assert exit_code == 1
    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert payload["error"] == "VPC creation failed: quota exceeded"
    assert set(payload["tests"]) == set(module.PORT_SECURITY_TEST_NAMES)
    assert all(test["passed"] is False for test in payload["tests"].values())
    assert cleaned_vpcs == ["vpc-partial"]


def test_port_security_policy_main_emits_full_contract_on_az_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Post-VPC failures (e.g. no available AZ) should still emit every subtest key."""
    module = _load_network_script("sg_port_security_policy.py")
    fake_ec2 = object()
    cleaned_vpcs: list[str] = []

    monkeypatch.setattr(module.boto3, "client", lambda service, region_name: fake_ec2)
    monkeypatch.setattr(
        module,
        "create_test_vpc",
        lambda ec2, cidr, name: {"passed": True, "vpc_id": "vpc-ok"},
    )

    def raise_no_az(ec2: Any, region: str) -> str:
        raise ValueError(f"No available AZ found in region {region}")

    monkeypatch.setattr(module, "_get_az", raise_no_az)
    monkeypatch.setattr(module, "cleanup_vpc_resources", lambda ec2, vpc_id: cleaned_vpcs.append(vpc_id))
    monkeypatch.setattr(sys, "argv", ["sg_port_security_policy.py", "--region", "us-west-2"])

    exit_code = module.main()

    assert exit_code == 1
    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert "No available AZ" in payload["error"]
    assert set(payload["tests"]) == set(module.PORT_SECURITY_TEST_NAMES)
    assert all(test["passed"] is False for test in payload["tests"].values())
    assert cleaned_vpcs == ["vpc-ok"]


class FakeEndpointDeletionWaitEc2:
    """Fake EC2 client for endpoint deletion polling."""

    def __init__(self, error: ClientError) -> None:
        """Configure the error raised by describe_vpc_endpoints."""
        self.error = error

    def describe_vpc_endpoints(self, VpcEndpointIds: list[str]) -> dict[str, Any]:
        """Raise the configured describe error."""
        raise self.error


def test_wait_for_endpoint_deletion_treats_not_found_as_success() -> None:
    """AWS NotFound during endpoint deletion means the endpoint is already gone."""
    module = _load_network_script("sg_scoping_test.py")
    ec2 = FakeEndpointDeletionWaitEc2(
        _client_error("DescribeVpcEndpoints", "InvalidVpcEndpointId.NotFound", "endpoint not found")
    )

    module._wait_for_endpoint_deletion(ec2, "vpce-svc", attempts=1, delay=0)


def test_wait_for_endpoint_deletion_reraises_unexpected_client_error() -> None:
    """Unexpected describe errors should still fail cleanup."""
    module = _load_network_script("sg_scoping_test.py")
    ec2 = FakeEndpointDeletionWaitEc2(_client_error("DescribeVpcEndpoints", "RequestLimitExceeded", "throttled"))

    with pytest.raises(ClientError):
        module._wait_for_endpoint_deletion(ec2, "vpce-svc", attempts=1, delay=0)


def test_connectivity_ping_result_uses_shared_ssm_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connectivity ping results should be adapted from the shared SSM runner."""
    module = _load_network_script("test_connectivity.py")
    calls: list[tuple[Any, str, str]] = []

    def fake_run_ssm_command(ssm: Any, instance_id: str, command: str) -> tuple[bool, str]:
        calls.append((ssm, instance_id, command))
        return True, "rtt min/avg/max/mdev = 0.100/0.250/0.300/0.010 ms"

    monkeypatch.setattr(module, "run_ssm_command", fake_run_ssm_command)

    result = module.ping_result_via_ssm("ssm-client", "i-source", "10.0.2.10")

    assert calls == [("ssm-client", "i-source", "ping -c 3 -W 2 10.0.2.10")]
    assert result == {"passed": True, "latency_ms": 0.25}


def test_traffic_iam_profile_result_uses_shared_instance_profile_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Traffic IAM setup should adapt the shared SSM instance profile helper."""
    module = _load_network_script("traffic_test.py")
    calls: list[tuple[Any, str]] = []

    def fake_create_ssm_instance_profile(iam: Any, description: str) -> tuple[str, str]:
        calls.append((iam, description))
        return "role-name", "profile-name"

    monkeypatch.setattr(module, "create_ssm_instance_profile", fake_create_ssm_instance_profile)

    result = module.ssm_instance_profile_result("iam-client")

    assert calls == [("iam-client", "Temporary role for traffic testing")]
    assert result == {
        "passed": True,
        "role_name": "role-name",
        "profile_name": "profile-name",
        "message": "Created IAM profile profile-name",
    }


def test_traffic_ping_result_uses_shared_ssm_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """Traffic ping checks should be adapted from the shared SSM runner."""
    module = _load_network_script("traffic_test.py")
    calls: list[tuple[Any, str, str]] = []

    def fake_run_ssm_command(ssm: Any, instance_id: str, command: str) -> tuple[bool, str]:
        calls.append((ssm, instance_id, command))
        return True, "rtt min/avg/max/mdev = 1.000/1.500/2.000/0.100 ms"

    monkeypatch.setattr(module, "run_ssm_command", fake_run_ssm_command)

    result = module.ping_result_via_ssm("ssm-client", "i-source", "10.0.1.20", expect_success=True)

    assert calls == [("ssm-client", "i-source", "ping -c 3 -W 2 10.0.1.20")]
    assert result == {
        "passed": True,
        "latency_ms": 1.5,
        "message": "Ping succeeded (latency: 1.5ms)",
    }


def test_traffic_ssm_ready_result_uses_shared_wait_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """Traffic SSM readiness should adapt the shared readiness helper."""
    module = _load_network_script("traffic_test.py")
    calls: list[tuple[Any, str, int]] = []

    def fake_wait_ssm_ready(ssm: Any, instance_id: str, timeout: int = 180) -> bool:
        calls.append((ssm, instance_id, timeout))
        return True

    monkeypatch.setattr(module, "wait_ssm_ready", fake_wait_ssm_ready)

    result = module.ssm_ready_result("ssm-client", "i-source")

    assert calls == [("ssm-client", "i-source", 180)]
    assert result == {"passed": True, "message": "SSM agent online"}


class FakeNeverDeletedEndpointEc2:
    """Fake EC2 client that keeps reporting the endpoint as present."""

    def describe_vpc_endpoints(self, VpcEndpointIds: list[str]) -> dict[str, Any]:
        """Report a still-present endpoint."""
        return {
            "VpcEndpoints": [
                {
                    "VpcEndpointId": VpcEndpointIds[0],
                    "NetworkInterfaceIds": ["eni-endpoint-1"],
                    "State": "deleting",
                }
            ]
        }


def test_wait_for_endpoint_deletion_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Polling exhaustion must surface as a timeout instead of returning success."""
    module = _load_network_script("sg_scoping_test.py")
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    ec2 = FakeNeverDeletedEndpointEc2()

    with pytest.raises(TimeoutError, match="Timed out waiting for VPC endpoint vpce-svc deletion"):
        module._wait_for_endpoint_deletion(ec2, "vpce-svc", attempts=2, delay=0)


class FakeSdnLoggingEc2:
    """Fake EC2 client for SDN logging script tests."""

    def __init__(
        self,
        flow_logs: list[dict[str, Any]] | None = None,
        flow_log_error: ClientError | None = None,
        delete_sg_error: ClientError | None = None,
        instances: list[dict[str, Any]] | None = None,
        network_interfaces: list[dict[str, Any]] | None = None,
        authorize_error: ClientError | None = None,
        revoke_error: ClientError | None = None,
    ) -> None:
        """Configure Flow Logs, metric resources, and optional SG failures."""
        self.flow_logs = flow_logs or []
        self.flow_log_error = flow_log_error
        self.delete_sg_error = delete_sg_error
        self.instances = instances or []
        self.network_interfaces = network_interfaces or []
        self.authorize_error = authorize_error
        self.revoke_error = revoke_error
        self.authorized_rules: list[dict[str, Any]] = []
        self.revoked_rules: list[dict[str, Any]] = []
        self.deleted_sgs: list[str] = []

    def describe_flow_logs(self, Filters: list[dict[str, Any]]) -> dict[str, Any]:
        """Return configured VPC Flow Logs."""
        assert Filters[0]["Name"] == "resource-id"
        if self.flow_log_error:
            raise self.flow_log_error
        return {"FlowLogs": list(self.flow_logs)}

    def describe_instances(self, Filters: list[dict[str, Any]]) -> dict[str, Any]:
        """Return configured target VPC instances for metric scoping."""
        assert {"Name": "vpc-id", "Values": ["vpc-test"]} in Filters
        return {"Reservations": [{"Instances": list(self.instances)}] if self.instances else []}

    def describe_network_interfaces(self, Filters: list[dict[str, Any]]) -> dict[str, Any]:
        """Return configured target VPC network interfaces for metric scoping."""
        assert Filters == [{"Name": "vpc-id", "Values": ["vpc-test"]}]
        return {"NetworkInterfaces": list(self.network_interfaces)}

    def create_security_group(
        self,
        GroupName: str,
        Description: str,
        VpcId: str,
        TagSpecifications: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Return a fake audit probe security group."""
        assert GroupName.startswith("isv-sdn-audit-")
        assert Description == "ISV SDN09 audit trail probe"
        assert VpcId == "vpc-test"
        assert TagSpecifications
        return {"GroupId": "sg-audit"}

    def authorize_security_group_ingress(self, GroupId: str, IpPermissions: list[dict[str, Any]]) -> dict[str, Any]:
        """Record authorized ingress rules."""
        if self.authorize_error:
            raise self.authorize_error
        self.authorized_rules.append({"GroupId": GroupId, "IpPermissions": IpPermissions})
        return {}

    def revoke_security_group_ingress(self, GroupId: str, IpPermissions: list[dict[str, Any]]) -> dict[str, Any]:
        """Record revoked ingress rules."""
        if self.revoke_error:
            raise self.revoke_error
        self.revoked_rules.append({"GroupId": GroupId, "IpPermissions": IpPermissions})
        return {}

    def delete_security_group(self, GroupId: str) -> dict[str, Any]:
        """Delete a fake security group, optionally raising a configured error."""
        if self.delete_sg_error:
            raise self.delete_sg_error
        self.deleted_sgs.append(GroupId)
        return {}


class FakePolicyPropagationEc2:
    """Fake EC2 client for SDN02-08 policy propagation timing tests."""

    def __init__(
        self,
        *,
        rule_visible_after: int = 1,
        rule_removed_after: int = 1,
        delete_sg_error: ClientError | None = None,
    ) -> None:
        """Configure poll thresholds and optional delete-security-group failure."""
        self.rule_visible_after = rule_visible_after
        self.rule_removed_after = rule_removed_after
        self.delete_sg_error = delete_sg_error
        self.authorized = False
        self.revoked = False
        self.describe_calls = 0
        self.revoked_describe_calls = 0
        self.deleted_sgs: list[str] = []

    def create_security_group(
        self,
        GroupName: str,
        Description: str,
        VpcId: str,
        TagSpecifications: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Return a fake policy probe security group."""
        assert GroupName.startswith("isv-sdn-policy-propagation-")
        assert Description == "ISV policy propagation probe"
        assert VpcId == "vpc-test"
        assert TagSpecifications
        return {"GroupId": "sg-probe"}

    def authorize_security_group_ingress(self, GroupId: str, IpPermissions: list[dict[str, Any]]) -> dict[str, Any]:
        """Record that the probe rule was added."""
        assert GroupId == "sg-probe"
        self.authorized = True
        return {}

    def revoke_security_group_ingress(self, GroupId: str, IpPermissions: list[dict[str, Any]]) -> dict[str, Any]:
        """Record that the probe rule was revoked."""
        assert GroupId == "sg-probe"
        self.revoked = True
        return {}

    def describe_security_groups(self, GroupIds: list[str]) -> dict[str, Any]:
        """Return SG permissions according to configured propagation lag."""
        assert GroupIds == ["sg-probe"]
        self.describe_calls += 1
        if self.revoked:
            self.revoked_describe_calls += 1
            visible = self.revoked_describe_calls < self.rule_removed_after
        elif self.authorized:
            visible = self.describe_calls >= self.rule_visible_after
        else:
            visible = False
        return {
            "SecurityGroups": [
                {
                    "GroupId": "sg-probe",
                    "IpPermissions": [
                        {
                            "IpProtocol": "tcp",
                            "FromPort": 443,
                            "ToPort": 443,
                            "IpRanges": [{"CidrIp": "10.0.0.0/8"}],
                        }
                    ]
                    if visible
                    else [],
                }
            ]
        }

    def delete_security_group(self, GroupId: str) -> dict[str, Any]:
        """Delete a fake security group, optionally raising a configured error."""
        if self.delete_sg_error:
            raise self.delete_sg_error
        self.deleted_sgs.append(GroupId)
        return {}


class FakeHealth:
    """Fake AWS Health client for SDN hardware-fault logging tests."""

    def __init__(self, events: list[dict[str, Any]] | None = None, error: ClientError | None = None) -> None:
        """Configure events or a describe_events error."""
        self.events = events or []
        self.error = error

    def describe_events(self, **kwargs: Any) -> dict[str, Any]:
        """Return configured Health events."""
        assert "EC2" in kwargs["filter"]["services"]
        if self.error:
            raise self.error
        return {"events": list(self.events)}


class FakeCloudWatch:
    """Fake CloudWatch client for latency/performance telemetry tests."""

    def __init__(
        self,
        metrics: list[dict[str, Any]] | None = None,
        datapoints: list[dict[str, Any]] | None = None,
        list_error: ClientError | None = None,
    ) -> None:
        """Configure metrics, datapoints, and optional list_metrics failure."""
        self.metrics = metrics or []
        self.datapoints = datapoints or []
        self.list_error = list_error
        self.list_metric_dimensions: list[list[dict[str, str]] | None] = []

    def list_metrics(
        self,
        Namespace: str,
        MetricName: str,
        Dimensions: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Return configured metrics."""
        assert Namespace == "AWS/EC2"
        assert MetricName == "NetworkPacketsIn"
        self.list_metric_dimensions.append(Dimensions)
        if self.list_error:
            raise self.list_error
        if not Dimensions:
            return {"Metrics": list(self.metrics)}

        requested = {(dimension["Name"], dimension["Value"]) for dimension in Dimensions}
        scoped_metrics = []
        for metric in self.metrics:
            metric_dimensions = {(dimension["Name"], dimension["Value"]) for dimension in metric.get("Dimensions", [])}
            if requested <= metric_dimensions:
                scoped_metrics.append(metric)
        return {"Metrics": scoped_metrics}

    def get_metric_statistics(
        self,
        Namespace: str,
        MetricName: str,
        Dimensions: list[dict[str, str]],
        StartTime: Any,
        EndTime: Any,
        Period: int,
        Statistics: list[str],
    ) -> dict[str, Any]:
        """Return configured datapoints."""
        assert Namespace == "AWS/EC2"
        assert MetricName == "NetworkPacketsIn"
        assert Period == 60
        assert Statistics == ["Sum"]
        assert StartTime < EndTime
        assert Dimensions
        return {"Datapoints": list(self.datapoints)}


class FakeLogs:
    """Fake CloudWatch Logs client for VPC Flow Log samples."""

    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        """Configure log events."""
        self.events = events or []
        self.calls: list[str] = []

    def filter_log_events(
        self,
        logGroupName: str,
        startTime: int,
        endTime: int,
        limit: int,
    ) -> dict[str, Any]:
        """Return configured Flow Log events."""
        assert logGroupName == "/aws/vpc/flow-logs"
        assert startTime < endTime
        assert limit == 10
        self.calls.append(logGroupName)
        return {"events": list(self.events)}


class FakeCloudTrail:
    """Fake CloudTrail client for audit trail tests."""

    def __init__(
        self,
        events: list[dict[str, Any]] | None = None,
        error: ClientError | None = None,
        event_batches: list[list[dict[str, Any]]] | None = None,
    ) -> None:
        """Configure parsed CloudTrail events or a lookup error."""
        self.events = events or []
        self.error = error
        self.event_batches = event_batches
        self.lookup_calls = 0

    def lookup_events(
        self,
        LookupAttributes: list[dict[str, str]],
        StartTime: Any,
        EndTime: Any,
    ) -> dict[str, Any]:
        """Return configured events as CloudTrail LookupEvents entries."""
        assert LookupAttributes == [{"AttributeKey": "ResourceName", "AttributeValue": "sg-audit"}]
        assert StartTime < EndTime
        if self.error:
            raise self.error
        events = self.events
        if self.event_batches is not None:
            batch_index = min(self.lookup_calls, len(self.event_batches) - 1)
            events = self.event_batches[batch_index]
        self.lookup_calls += 1
        return {"Events": [{"CloudTrailEvent": json.dumps(event)} for event in events]}


def _active_flow_log() -> dict[str, Any]:
    """Return a fake active VPC Flow Log."""
    return {
        "FlowLogId": "fl-123",
        "FlowLogStatus": "ACTIVE",
        "LogDestinationType": "cloud-watch-logs",
        "LogGroupName": "/aws/vpc/flow-logs",
        "LogDestination": "arn:aws:logs:us-west-2:123456789012:log-group:/aws/vpc/flow-logs",
    }


def _active_s3_flow_log() -> dict[str, Any]:
    """Return a fake active S3-backed VPC Flow Log."""
    return {
        "FlowLogId": "fl-s3",
        "FlowLogStatus": "ACTIVE",
        "LogDestinationType": "s3",
        "LogDestination": "arn:aws:s3:::isv-flow-logs",
    }


@pytest.mark.parametrize(
    ("aspect", "step_name"),
    [
        ("hardware_faults", "sdn_hardware_fault_logging"),
        ("latency_perf", "sdn_latency_perf_logging"),
        ("audit_trail", "sdn_filter_audit_trail"),
    ],
)
def test_aws_sdn_logging_result_test_names_match_suite_steps(aspect: str, step_name: str) -> None:
    """AWS SDN logging output names must match suite step IDs."""
    module = _load_network_script("sdn_logging_test.py")

    result = module._base_result(aspect, "vpc-test", "us-west-2")

    assert result["test_name"] == step_name


@pytest.mark.parametrize(
    ("aspect", "step_name"),
    [
        ("hardware_faults", "sdn_hardware_fault_logging"),
        ("latency_perf", "sdn_latency_perf_logging"),
        ("audit_trail", "sdn_filter_audit_trail"),
    ],
)
def test_my_isv_sdn_logging_demo_test_names_match_suite_steps(aspect: str, step_name: str) -> None:
    """my-isv SDN logging template output names must match suite step IDs."""
    script = MY_ISV_NETWORK_SCRIPTS / "sdn_logging_test.py"
    env = os.environ | {"ISVCTL_DEMO_MODE": "1"}

    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(script),
                "--region",
                "demo-region",
                "--vpc-id",
                "vpc-demo",
                "--aspect",
                aspect,
            ],
            capture_output=True,
            env=env,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"{script} timed out after {exc.timeout} seconds\nstdout: {exc.stdout!r}\nstderr: {exc.stderr!r}")

    assert completed.returncode == 0, completed.stderr
    result: dict[str, Any] = json.loads(completed.stdout)
    assert result["test_name"] == step_name


def test_my_isv_port_security_policy_demo_output_contract() -> None:
    """my-isv port security template demo output must satisfy the validation contract."""
    script = MY_ISV_NETWORK_SCRIPTS / "sg_port_security_policy.py"
    env = os.environ | {"ISVCTL_DEMO_MODE": "1"}

    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(script),
                "--region",
                "demo-region",
                "--allowed-port",
                "8443",
            ],
            capture_output=True,
            env=env,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"{script} timed out after {exc.timeout} seconds\nstdout: {exc.stdout!r}\nstderr: {exc.stderr!r}")

    assert completed.returncode == 0, completed.stderr
    result: dict[str, Any] = json.loads(completed.stdout)
    assert result["success"] is True
    assert result["test_name"] == "sg_port_security_policy"
    assert set(result["tests"]) == {
        "create_virtual_interface",
        "apply_port_policy",
        "allowed_port_permitted",
        "unlisted_port_blocked",
        "other_interface_unaffected",
        "cleanup",
    }
    assert all(test["passed"] for test in result["tests"].values())


def test_sdn_hardware_fault_logging_happy_path() -> None:
    """Hardware-fault logging passes with Flow Logs and queryable Health events."""
    module = _load_network_script("sdn_logging_test.py")
    ec2 = FakeSdnLoggingEc2(flow_logs=[_active_flow_log()])
    health = FakeHealth(
        events=[
            {
                "arn": "arn:aws:health:global::event/EC2/test",
                "service": "EC2",
                "eventTypeCategory": "issue",
                "startTime": "2026-05-05T00:00:00Z",
            }
        ]
    )

    result = module.check_hardware_fault_logging(ec2, health, "vpc-test", "us-west-2")

    assert result["success"] is True
    assert result["tests"]["logging_endpoint_reachable"]["passed"] is True
    assert result["tests"]["fault_event_source_queryable"]["passed"] is True
    assert result["tests"]["event_schema_valid"]["passed"] is True
    assert result["log_destination"].endswith("/aws/vpc/flow-logs")
    assert result["recent_event_count"] == 1


def test_sdn_hardware_fault_logging_marks_health_subscription_provider_hidden() -> None:
    """AWS Health subscription gating should not fail the hardware-fault check."""
    module = _load_network_script("sdn_logging_test.py")
    ec2 = FakeSdnLoggingEc2(flow_logs=[_active_flow_log()])
    health = FakeHealth(
        error=_client_error("DescribeEvents", "SubscriptionRequiredException", "AWS Health subscription required")
    )

    result = module.check_hardware_fault_logging(ec2, health, "vpc-test", "us-west-2")

    assert result["success"] is True
    assert result["tests"]["fault_event_source_queryable"]["provider_hidden"] is True
    assert result["tests"]["event_schema_valid"]["provider_hidden"] is True
    assert result["recent_event_count"] == 0


def test_sdn_hardware_fault_logging_marks_absent_flow_logs_provider_hidden() -> None:
    """Hardware-fault logging does not fail the AWS suite when Flow Logs are not configured."""
    module = _load_network_script("sdn_logging_test.py")
    ec2 = FakeSdnLoggingEc2(flow_logs=[])
    health = FakeHealth(events=[])

    result = module.check_hardware_fault_logging(ec2, health, "vpc-test", "us-west-2")

    assert result["success"] is True
    assert result["log_destination"] == "aws-vpc-flow-logs:not-configured"
    assert result["tests"]["log_destination_configured"]["provider_hidden"] is True


def test_sdn_hardware_fault_logging_fails_destination_when_flow_log_query_fails() -> None:
    """Hardware-fault logging must not report absent Flow Logs when the query failed."""
    module = _load_network_script("sdn_logging_test.py")
    ec2 = FakeSdnLoggingEc2(flow_log_error=_client_error("DescribeFlowLogs", "UnauthorizedOperation", "denied"))
    health = FakeHealth(events=[])

    result = module.check_hardware_fault_logging(ec2, health, "vpc-test", "us-west-2")

    log_destination = result["tests"]["log_destination_configured"]
    assert result["success"] is False
    assert result["log_destination"] == "aws-vpc-flow-logs:unknown"
    assert log_destination["passed"] is False
    assert "Unable to inspect VPC Flow Logs" in log_destination["error"]
    assert log_destination["flow_log_query"]["passed"] is False
    assert "provider_hidden" not in log_destination


def test_sdn_hardware_fault_logging_fails_event_schema_when_health_query_fails() -> None:
    """A non-hidden Health query failure must not be reported as a passing schema check."""
    module = _load_network_script("sdn_logging_test.py")
    ec2 = FakeSdnLoggingEc2(flow_logs=[_active_flow_log()])
    health = FakeHealth(error=_client_error("DescribeEvents", "UnauthorizedOperation", "denied"))

    result = module.check_hardware_fault_logging(ec2, health, "vpc-test", "us-west-2")

    event_schema_valid = result["tests"]["event_schema_valid"]
    assert result["success"] is False
    assert result["tests"]["fault_event_source_queryable"]["passed"] is False
    assert event_schema_valid["passed"] is False
    assert "provider_hidden" not in event_schema_valid
    assert event_schema_valid["health_query"]["passed"] is False


def test_sdn_latency_perf_logging_happy_path_with_cloudwatch_datapoint() -> None:
    """Latency/performance logging passes when CloudWatch has recent packet datapoints."""
    module = _load_network_script("sdn_logging_test.py")
    ec2 = FakeSdnLoggingEc2(
        flow_logs=[],
        instances=[
            {
                "InstanceId": "i-probe",
                "NetworkInterfaces": [{"NetworkInterfaceId": "eni-probe"}],
            }
        ],
        network_interfaces=[{"NetworkInterfaceId": "eni-probe"}],
    )
    cloudwatch = FakeCloudWatch(
        metrics=[
            {
                "MetricName": "NetworkPacketsIn",
                "Dimensions": [{"Name": "InstanceId", "Value": "i-probe"}],
            }
        ],
        datapoints=[{"Sum": 42.0}],
    )
    logs = FakeLogs(events=[])

    result = module.check_latency_perf_logging(
        ec2,
        cloudwatch,
        logs,
        "vpc-test",
        "us-west-2",
        sample_window_seconds=60,
    )

    assert result["success"] is True
    assert result["telemetry_namespace"] == "AWS/EC2"
    assert result["probe_resource_id"] == "i-probe"
    assert [{"Name": "InstanceId", "Value": "i-probe"}] in cloudwatch.list_metric_dimensions
    assert result["tests"]["performance_metric_present"]["provider_hidden"] is True
    assert result["tests"]["samples_recent"]["passed"] is True


def test_sdn_logging_list_packet_metrics_paginates_and_filters() -> None:
    """CloudWatch packet metric discovery must include all list_metrics pages."""
    module = _load_network_script("sdn_logging_test.py")
    calls: list[dict[str, Any]] = []
    dimensions = [{"Name": "InstanceId", "Value": "i-probe"}]
    pages = [
        {
            "Metrics": [
                {
                    "MetricName": "NetworkPacketsIn",
                    "Dimensions": dimensions,
                }
            ],
            "NextToken": "page-2",
        },
        {
            "Metrics": [
                {
                    "MetricName": "NetworkBytesIn",
                    "Dimensions": dimensions,
                },
                {
                    "MetricName": "NetworkPacketsIn",
                    "Dimensions": [{"Name": "InstanceId", "Value": "i-next"}],
                },
            ]
        },
    ]

    class PagedCloudWatch:
        """Fake CloudWatch client returning a tokenized list_metrics response."""

        def list_metrics(self, **kwargs: Any) -> dict[str, Any]:
            """Return the next configured metrics page."""
            calls.append(dict(kwargs))
            return pages[len(calls) - 1]

    metrics = module._list_packet_metrics(PagedCloudWatch(), dimensions)

    assert calls == [
        {
            "Namespace": "AWS/EC2",
            "MetricName": "NetworkPacketsIn",
            "Dimensions": dimensions,
        },
        {
            "Namespace": "AWS/EC2",
            "MetricName": "NetworkPacketsIn",
            "Dimensions": dimensions,
            "NextToken": "page-2",
        },
    ]
    assert metrics == [
        {
            "Namespace": "AWS/EC2",
            "MetricName": "NetworkPacketsIn",
            "Dimensions": dimensions,
        },
        {
            "Namespace": "AWS/EC2",
            "MetricName": "NetworkPacketsIn",
            "Dimensions": [{"Name": "InstanceId", "Value": "i-next"}],
        },
    ]


def test_sdn_latency_perf_logging_cloudwatch_metrics_pass_when_flow_log_query_fails() -> None:
    """CloudWatch packet metrics independently satisfy packet telemetry."""
    module = _load_network_script("sdn_logging_test.py")
    ec2 = FakeSdnLoggingEc2(
        flow_log_error=_client_error("DescribeFlowLogs", "UnauthorizedOperation", "denied"),
        instances=[
            {
                "InstanceId": "i-probe",
                "NetworkInterfaces": [{"NetworkInterfaceId": "eni-probe"}],
            }
        ],
        network_interfaces=[{"NetworkInterfaceId": "eni-probe"}],
    )
    cloudwatch = FakeCloudWatch(
        metrics=[
            {
                "MetricName": "NetworkPacketsIn",
                "Dimensions": [{"Name": "InstanceId", "Value": "i-probe"}],
            }
        ],
        datapoints=[{"Sum": 42.0}],
    )
    logs = FakeLogs(events=[])

    result = module.check_latency_perf_logging(
        ec2,
        cloudwatch,
        logs,
        "vpc-test",
        "us-west-2",
        sample_window_seconds=60,
    )

    assert result["success"] is True
    assert result["tests"]["packet_metric_present"]["passed"] is True
    assert result["tests"]["samples_recent"]["passed"] is True
    assert result["telemetry_namespace"] == "AWS/EC2"
    assert result["probe_resource_id"] == "i-probe"


def test_sdn_latency_perf_logging_ignores_account_metrics_when_target_vpc_has_no_resources() -> None:
    """Account-wide EC2 metrics must not count as target VPC telemetry."""
    module = _load_network_script("sdn_logging_test.py")
    ec2 = FakeSdnLoggingEc2(flow_logs=[])
    cloudwatch = FakeCloudWatch(
        metrics=[
            {
                "MetricName": "NetworkPacketsIn",
                "Dimensions": [{"Name": "InstanceId", "Value": "i-unrelated"}],
            }
        ],
        datapoints=[{"Sum": 42.0}],
    )
    logs = FakeLogs(events=[])

    result = module.check_latency_perf_logging(
        ec2,
        cloudwatch,
        logs,
        "vpc-test",
        "us-west-2",
        sample_window_seconds=60,
    )

    assert result["success"] is True
    assert result["telemetry_namespace"] == "provider-hidden"
    assert result["probe_resource_id"] == "vpc-test"
    assert result["tests"]["packet_metric_present"]["provider_hidden"] is True
    assert result["tests"]["samples_recent"]["provider_hidden"] is True


def test_sdn_latency_perf_logging_fails_packet_metric_when_flow_log_query_fails() -> None:
    """A Flow Logs query failure must not be hidden as absent target telemetry."""
    module = _load_network_script("sdn_logging_test.py")
    ec2 = FakeSdnLoggingEc2(flow_log_error=_client_error("DescribeFlowLogs", "UnauthorizedOperation", "denied"))
    cloudwatch = FakeCloudWatch(metrics=[], datapoints=[])
    logs = FakeLogs(events=[])

    result = module.check_latency_perf_logging(
        ec2,
        cloudwatch,
        logs,
        "vpc-test",
        "us-west-2",
        sample_window_seconds=60,
    )

    packet_metric = result["tests"]["packet_metric_present"]
    assert result["success"] is False
    assert packet_metric["passed"] is False
    assert "Unable to verify VPC Flow Logs" in packet_metric["error"]
    assert "UnauthorizedOperation" in packet_metric["flow_log_error"]
    assert packet_metric["flow_log_query"]["passed"] is False
    assert "provider_hidden" not in packet_metric


def test_sdn_latency_perf_logging_rejects_unrelated_cloudwatch_metrics_for_target_resources() -> None:
    """Metrics for instances outside the target VPC must not satisfy packet telemetry."""
    module = _load_network_script("sdn_logging_test.py")
    ec2 = FakeSdnLoggingEc2(flow_logs=[], instances=[{"InstanceId": "i-probe"}])
    cloudwatch = FakeCloudWatch(
        metrics=[
            {
                "MetricName": "NetworkPacketsIn",
                "Dimensions": [{"Name": "InstanceId", "Value": "i-unrelated"}],
            }
        ],
        datapoints=[{"Sum": 42.0}],
    )
    logs = FakeLogs(events=[])

    result = module.check_latency_perf_logging(
        ec2,
        cloudwatch,
        logs,
        "vpc-test",
        "us-west-2",
        sample_window_seconds=60,
    )

    assert result["success"] is False
    assert result["tests"]["packet_metric_present"]["passed"] is False
    assert "No target-VPC CloudWatch packet metric" in result["tests"]["packet_metric_present"]["error"]


def test_sdn_latency_perf_logging_fails_without_recent_samples() -> None:
    """Latency/performance logging fails when no packet telemetry sample is recent."""
    module = _load_network_script("sdn_logging_test.py")
    ec2 = FakeSdnLoggingEc2(
        flow_logs=[_active_flow_log()],
        instances=[{"InstanceId": "i-probe"}],
    )
    cloudwatch = FakeCloudWatch(
        metrics=[
            {
                "MetricName": "NetworkPacketsIn",
                "Dimensions": [{"Name": "InstanceId", "Value": "i-probe"}],
            }
        ],
        datapoints=[],
    )
    logs = FakeLogs(events=[])

    result = module.check_latency_perf_logging(
        ec2,
        cloudwatch,
        logs,
        "vpc-test",
        "us-west-2",
        sample_window_seconds=60,
    )

    assert result["success"] is False
    assert result["tests"]["packet_metric_present"]["passed"] is True
    assert result["tests"]["samples_recent"]["passed"] is False
    assert "No recent packet telemetry samples" in result["tests"]["samples_recent"]["error"]


def test_sdn_latency_perf_logging_uses_recent_flow_log_samples() -> None:
    """Flow Log records satisfy the recent sample requirement when metrics are absent."""
    module = _load_network_script("sdn_logging_test.py")
    ec2 = FakeSdnLoggingEc2(flow_logs=[_active_flow_log()])
    cloudwatch = FakeCloudWatch(metrics=[], datapoints=[])
    logs = FakeLogs(events=[{"message": "2 123 eni-1 10.0.0.1 10.0.0.2 443 443 6 1 52 1 2 ACCEPT OK"}])

    result = module.check_latency_perf_logging(
        ec2,
        cloudwatch,
        logs,
        "vpc-test",
        "us-west-2",
        sample_window_seconds=60,
    )

    assert result["success"] is True
    assert result["telemetry_namespace"] == "AWS/VPCFlowLogs"
    assert result["probe_resource_id"] == "vpc-test"
    assert result["tests"]["samples_recent"]["sample_count"] == 1


def test_sdn_latency_perf_logging_marks_s3_flow_log_samples_provider_hidden() -> None:
    """S3-backed Flow Logs are valid telemetry but cannot be sampled through CloudWatch Logs."""
    module = _load_network_script("sdn_logging_test.py")
    ec2 = FakeSdnLoggingEc2(flow_logs=[_active_s3_flow_log()])
    cloudwatch = FakeCloudWatch(metrics=[], datapoints=[])
    logs = FakeLogs(events=[])

    result = module.check_latency_perf_logging(
        ec2,
        cloudwatch,
        logs,
        "vpc-test",
        "us-west-2",
        sample_window_seconds=60,
    )

    samples_recent = result["tests"]["samples_recent"]
    assert result["success"] is True
    assert result["telemetry_namespace"] == "AWS/VPCFlowLogs"
    assert result["probe_resource_id"] == "vpc-test"
    assert result["tests"]["packet_metric_present"]["passed"] is True
    assert samples_recent["provider_hidden"] is True
    assert "s3" in samples_recent["message"]
    assert samples_recent["flow_log_destinations"] == ["arn:aws:s3:::isv-flow-logs"]
    assert logs.calls == []


def test_sdn_audit_trail_logging_happy_path() -> None:
    """Audit trail logging passes when CloudTrail has the SG rule lifecycle."""
    module = _load_network_script("sdn_logging_test.py")
    ec2 = FakeSdnLoggingEc2()
    cloudtrail = FakeCloudTrail(
        events=[
            module._audit_event("AuthorizeSecurityGroupIngress", "sg-audit"),
            module._audit_event("RevokeSecurityGroupIngress", "sg-audit"),
            module._audit_event("AuthorizeSecurityGroupIngress", "sg-audit"),
            module._audit_event("RevokeSecurityGroupIngress", "sg-audit"),
            module._audit_event("DeleteSecurityGroup", "sg-audit"),
        ]
    )

    result = module.check_audit_trail_logging(
        ec2,
        cloudtrail,
        "vpc-test",
        "us-west-2",
        timeout_seconds=0,
        poll_seconds=0,
    )

    assert result["success"] is True
    assert result["target_rule_id"] == "sg-audit"
    assert result["tests"]["create_rule_logged"]["passed"] is True
    assert result["tests"]["modify_rule_logged"]["passed"] is True
    assert result["tests"]["delete_rule_logged"]["passed"] is True
    assert result["tests"]["cleanup"]["passed"] is True
    assert len(ec2.authorized_rules) == 2
    assert len(ec2.revoked_rules) == 2
    assert ec2.deleted_sgs == ["sg-audit"]


def test_sdn_audit_trail_logging_polls_until_full_lifecycle_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CloudTrail polling must not stop after only the first audit event appears."""
    module = _load_network_script("sdn_logging_test.py")
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    ec2 = FakeSdnLoggingEc2()
    cloudtrail = FakeCloudTrail(
        event_batches=[
            [module._audit_event("AuthorizeSecurityGroupIngress", "sg-audit")],
            [
                module._audit_event("AuthorizeSecurityGroupIngress", "sg-audit"),
                module._audit_event("RevokeSecurityGroupIngress", "sg-audit"),
                module._audit_event("AuthorizeSecurityGroupIngress", "sg-audit"),
                module._audit_event("RevokeSecurityGroupIngress", "sg-audit"),
                module._audit_event("DeleteSecurityGroup", "sg-audit"),
            ],
        ]
    )

    result = module.check_audit_trail_logging(
        ec2,
        cloudtrail,
        "vpc-test",
        "us-west-2",
        timeout_seconds=1,
        poll_seconds=0,
    )

    assert result["success"] is True
    assert cloudtrail.lookup_calls == 2
    assert result["tests"]["audit_endpoint_reachable"]["passed"] is True


def test_sdn_audit_trail_logging_fails_on_cloudtrail_propagation_timeout() -> None:
    """Missing CloudTrail events should fail with a propagation timeout marker."""
    module = _load_network_script("sdn_logging_test.py")
    ec2 = FakeSdnLoggingEc2()
    cloudtrail = FakeCloudTrail(events=[])

    result = module.check_audit_trail_logging(
        ec2,
        cloudtrail,
        "vpc-test",
        "us-west-2",
        timeout_seconds=0,
        poll_seconds=0,
    )

    assert result["success"] is False
    assert result["tests"]["audit_endpoint_reachable"]["passed"] is False
    assert result["tests"]["audit_endpoint_reachable"]["propagation_timeout"] is True
    assert result["tests"]["create_rule_logged"]["passed"] is False


def test_sdn_audit_trail_logging_cleans_up_probe_after_partial_create_failure() -> None:
    """If mutation fails after SG creation, the audit probe SG must still be deleted."""
    module = _load_network_script("sdn_logging_test.py")
    ec2 = FakeSdnLoggingEc2(authorize_error=_client_error("AuthorizeSecurityGroupIngress", "InvalidPermission"))
    cloudtrail = FakeCloudTrail(events=[])

    result = module.check_audit_trail_logging(
        ec2,
        cloudtrail,
        "vpc-test",
        "us-west-2",
        timeout_seconds=0,
        poll_seconds=0,
    )

    assert result["success"] is False
    assert result["target_rule_id"] == "sg-audit"
    assert result["tests"]["cleanup"]["passed"] is True
    assert ec2.deleted_sgs == ["sg-audit"]


def test_sdn_audit_trail_logging_ignores_create_security_group_event_without_group_id() -> None:
    """CreateSecurityGroup events lack groupId in requestParameters and must not fail required-fields."""
    module = _load_network_script("sdn_logging_test.py")
    ec2 = FakeSdnLoggingEc2()
    create_event = {
        "eventName": "CreateSecurityGroup",
        "userIdentity": {"type": "AssumedRole", "arn": "arn:aws:sts::123456789012:assumed-role/isv/test"},
        "eventTime": datetime.now(UTC).isoformat(),
        "requestParameters": {"groupName": "isv-sdn-audit", "vpcId": "vpc-test"},
    }
    cloudtrail = FakeCloudTrail(
        events=[
            create_event,
            module._audit_event("AuthorizeSecurityGroupIngress", "sg-audit"),
            module._audit_event("RevokeSecurityGroupIngress", "sg-audit"),
            module._audit_event("AuthorizeSecurityGroupIngress", "sg-audit"),
            module._audit_event("RevokeSecurityGroupIngress", "sg-audit"),
        ]
    )

    result = module.check_audit_trail_logging(
        ec2,
        cloudtrail,
        "vpc-test",
        "us-west-2",
        timeout_seconds=0,
        poll_seconds=0,
    )

    assert result["success"] is True
    assert result["tests"]["audit_event_has_required_fields"]["passed"] is True


def test_sdn_audit_trail_logging_records_cleanup_failure() -> None:
    """Audit probe cleanup failures must be visible in the cleanup subtest."""
    module = _load_network_script("sdn_logging_test.py")
    ec2 = FakeSdnLoggingEc2(delete_sg_error=_client_error("DeleteSecurityGroup", "DependencyViolation", "in use"))
    cloudtrail = FakeCloudTrail(
        events=[
            module._audit_event("AuthorizeSecurityGroupIngress", "sg-audit"),
            module._audit_event("RevokeSecurityGroupIngress", "sg-audit"),
            module._audit_event("AuthorizeSecurityGroupIngress", "sg-audit"),
            module._audit_event("RevokeSecurityGroupIngress", "sg-audit"),
            module._audit_event("DeleteSecurityGroup", "sg-audit"),
        ]
    )

    result = module.check_audit_trail_logging(
        ec2,
        cloudtrail,
        "vpc-test",
        "us-west-2",
        timeout_seconds=0,
        poll_seconds=0,
    )

    assert result["success"] is False
    assert result["tests"]["cleanup"]["passed"] is False
    assert "Failed to delete audit probe security group sg-audit" in result["tests"]["cleanup"]["error"]


def test_sdn_policy_propagation_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Policy propagation passes when add/remove observations stay within the limit."""
    module = _load_network_script("sg_policy_propagation_test.py")
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    ec2 = FakePolicyPropagationEc2(rule_visible_after=2, rule_removed_after=2)

    result = module.check_policy_propagation(
        ec2,
        "vpc-test",
        "us-west-2",
        max_propagation_seconds=10,
        poll_seconds=0,
    )

    assert result["success"] is True
    assert result["test_name"] == "sg_policy_propagation"
    assert result["tests"]["rule_observed"]["passed"] is True
    assert result["tests"]["removal_observed"]["passed"] is True
    assert result["target_rule_id"] == "sg-probe"
    assert result["add_observed_seconds"] <= 10
    assert result["remove_observed_seconds"] <= 10
    assert ec2.deleted_sgs == ["sg-probe"]


def test_sdn_policy_propagation_times_out_waiting_for_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rule that is never observed must fail with a propagation timeout."""
    module = _load_network_script("sg_policy_propagation_test.py")
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    ec2 = FakePolicyPropagationEc2(rule_visible_after=999)

    result = module.check_policy_propagation(
        ec2,
        "vpc-test",
        "us-west-2",
        max_propagation_seconds=0,
        poll_seconds=0,
    )

    assert result["success"] is False
    assert result["tests"]["rule_observed"]["passed"] is False
    assert result["tests"]["rule_observed"]["propagation_timeout"] is True
    assert result["tests"]["cleanup"]["passed"] is True


def test_sdn_policy_propagation_records_cleanup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cleanup errors must be surfaced and make the overall result fail."""
    module = _load_network_script("sg_policy_propagation_test.py")
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    ec2 = FakePolicyPropagationEc2(delete_sg_error=_client_error("DeleteSecurityGroup", "DependencyViolation", "busy"))

    result = module.check_policy_propagation(
        ec2,
        "vpc-test",
        "us-west-2",
        max_propagation_seconds=10,
        poll_seconds=0,
    )

    assert result["success"] is False
    assert result["tests"]["cleanup"]["passed"] is False
    assert result["tests"]["cleanup"]["error"] == "Probe rule cleanup failed"


def test_my_isv_policy_propagation_demo_test_name_matches_suite_step() -> None:
    """my-isv SDN02-08 template output name must match the suite step ID."""
    script = MY_ISV_NETWORK_SCRIPTS / "sg_policy_propagation_test.py"
    env = os.environ | {"ISVCTL_DEMO_MODE": "1"}

    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(script),
                "--region",
                "demo-region",
                "--vpc-id",
                "vpc-demo",
            ],
            capture_output=True,
            env=env,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"{script} timed out after {exc.timeout} seconds\nstdout: {exc.stdout!r}\nstderr: {exc.stderr!r}")

    assert completed.returncode == 0, completed.stderr
    result: dict[str, Any] = json.loads(completed.stdout)
    assert result["test_name"] == "sg_policy_propagation"
    assert result["success"] is True


def test_my_isv_storage_l3_routing_demo_reports_directed_full_mesh() -> None:
    """my-isv storage L3 demo output should report directed host pairs."""
    script = MY_ISV_NETWORK_SCRIPTS / "storage_l3_routing_test.py"
    env = os.environ | {"ISVCTL_DEMO_MODE": "1"}

    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(script),
                "--region",
                "demo-region",
                "--hosts",
                "3",
            ],
            capture_output=True,
            env=env,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"{script} timed out after {exc.timeout} seconds\nstdout: {exc.stdout!r}\nstderr: {exc.stderr!r}")

    assert completed.returncode == 0, completed.stderr
    payload: dict[str, Any] = json.loads(completed.stdout)
    assert payload["success"] is True
    assert payload["test_name"] == "storage_l3_routing"
    assert payload["tests"]["distinct_subnets"]["subnet_count"] == 2
    assert payload["tests"]["all_to_all_reachable"]["pairs_tested"] == 6
    assert payload["tests"]["all_to_all_reachable"]["pairs_reachable"] == 6
    assert payload["tests"]["cross_subnet_routing"]["pairs_tested"] == 4
    assert payload["tests"]["cross_subnet_routing"]["pairs_reachable"] == 4
    assert payload["tests"]["no_gateway_hop"]["pairs_tested"] == 4
    assert payload["tests"]["no_gateway_hop"]["pairs_direct"] == 4


def test_my_isv_storage_l3_routing_rejects_too_few_hosts() -> None:
    """my-isv storage L3 demo should reject host counts below the contract minimum."""
    script = MY_ISV_NETWORK_SCRIPTS / "storage_l3_routing_test.py"
    env = os.environ | {"ISVCTL_DEMO_MODE": "1"}

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--region",
            "demo-region",
            "--hosts",
            "2",
        ],
        capture_output=True,
        env=env,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 2
    assert "--hosts must be >= 3" in completed.stderr


class FakeStableEgressEc2:
    """Fake EC2 client for stable egress IP main-path tests."""

    def __init__(self) -> None:
        """Track subnet creation without reaching AWS."""
        self.created_subnet_cidrs: list[str] = []

    def describe_availability_zones(self, Filters: list[dict[str, Any]]) -> dict[str, Any]:
        """Return one available AZ."""
        assert Filters == [{"Name": "state", "Values": ["available"]}]
        return {"AvailabilityZones": [{"ZoneName": "us-west-2a"}]}

    def create_subnet(self, VpcId: str, CidrBlock: str, AvailabilityZone: str) -> dict[str, Any]:
        """Record the subnet CIDR used by main."""
        assert VpcId == "vpc-egress"
        assert AvailabilityZone == "us-west-2a"
        self.created_subnet_cidrs.append(CidrBlock)
        return {"Subnet": {"SubnetId": "subnet-egress"}}

    def terminate_instances(self, InstanceIds: list[str]) -> dict[str, Any]:
        """No-op terminate for cleanup."""
        assert InstanceIds == ["i-egress"]
        return {}

    def delete_key_pair(self, KeyName: str) -> dict[str, Any]:
        """No-op key cleanup."""
        assert KeyName.startswith("isv-stable-egress-ip-")
        return {}

    def delete_security_group(self, GroupId: str) -> dict[str, Any]:
        """No-op security group cleanup."""
        assert GroupId == "sg-egress"
        return {}

    def delete_subnet(self, SubnetId: str) -> dict[str, Any]:
        """No-op subnet cleanup."""
        assert SubnetId == "subnet-egress"
        return {}

    def get_waiter(self, _name: str) -> Any:
        """Return a waiter with a no-op wait method."""
        return type("FakeWaiter", (), {"wait": lambda self, **kwargs: None})()


class FakeStorageL3Ec2:
    """Fake EC2 client for storage L3 routing main-path tests."""

    def __init__(self) -> None:
        """Track endpoint operations and expose a local VPC route."""
        self.endpoint_requests: list[dict[str, Any]] = []
        self.deleted_endpoints: list[str] = []

    def create_vpc_endpoint(
        self,
        VpcId: str,
        ServiceName: str,
        VpcEndpointType: str,
        SubnetIds: list[str],
        SecurityGroupIds: list[str],
        PrivateDnsEnabled: bool,
        TagSpecifications: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Record a VPC endpoint creation request."""
        self.endpoint_requests.append(
            {
                "VpcId": VpcId,
                "ServiceName": ServiceName,
                "VpcEndpointType": VpcEndpointType,
                "SubnetIds": SubnetIds,
                "SecurityGroupIds": SecurityGroupIds,
                "PrivateDnsEnabled": PrivateDnsEnabled,
                "TagSpecifications": TagSpecifications,
            }
        )
        service = ServiceName.rsplit(".", maxsplit=1)[-1]
        return {"VpcEndpoint": {"VpcEndpointId": f"vpce-{service}"}}

    def describe_route_tables(self, Filters: list[dict[str, Any]]) -> dict[str, Any]:
        """Return the VPC main route table with the VPC-local route."""
        assert Filters == [{"Name": "vpc-id", "Values": ["vpc-storage"]}]
        return {
            "RouteTables": [
                {
                    "RouteTableId": "rtb-main",
                    "Associations": [{"Main": True}],
                    "Routes": [
                        {
                            "DestinationCidrBlock": "10.86.0.0/16",
                            "GatewayId": "local",
                            "State": "active",
                        }
                    ],
                }
            ]
        }

    def terminate_instances(self, InstanceIds: list[str]) -> dict[str, Any]:
        """No-op instance cleanup."""
        assert InstanceIds == ["i-a", "i-b", "i-c"]
        return {}

    def delete_vpc_endpoints(self, VpcEndpointIds: list[str]) -> dict[str, Any]:
        """Record endpoint cleanup."""
        self.deleted_endpoints.extend(VpcEndpointIds)
        return {"Unsuccessful": []}

    def describe_vpc_endpoints(self, VpcEndpointIds: list[str]) -> dict[str, Any]:
        """Report endpoints as available until deletion has been requested."""
        return {
            "VpcEndpoints": [
                {"VpcEndpointId": endpoint_id, "State": "available"}
                for endpoint_id in VpcEndpointIds
                if endpoint_id not in self.deleted_endpoints
            ]
        }

    def get_waiter(self, _name: str) -> Any:
        """Return a waiter with a no-op wait method."""
        return type("FakeWaiter", (), {"wait": lambda self, **kwargs: None})()


class FakeStorageL3SubnetEc2:
    """Fake EC2 client for storage L3 subnet creation tests."""

    def __init__(self) -> None:
        """Track subnet creation requests."""
        self.create_subnet_calls: list[dict[str, str]] = []

    def describe_availability_zones(self, Filters: list[dict[str, Any]]) -> dict[str, Any]:
        """Return two available AZs."""
        assert Filters == [{"Name": "state", "Values": ["available"]}]
        return {"AvailabilityZones": [{"ZoneName": "us-west-2a"}, {"ZoneName": "us-west-2b"}]}

    def create_subnet(self, VpcId: str, CidrBlock: str, AvailabilityZone: str) -> dict[str, Any]:
        """Record subnet creation requests."""
        self.create_subnet_calls.append(
            {
                "VpcId": VpcId,
                "CidrBlock": CidrBlock,
                "AvailabilityZone": AvailabilityZone,
            }
        )
        return {"Subnet": {"SubnetId": f"subnet-storage-{len(self.create_subnet_calls)}"}}

    def create_tags(self, Resources: list[str], Tags: list[dict[str, str]]) -> dict[str, Any]:
        """No-op tag creation."""
        return {}


def test_storage_l3_create_subnets_rejects_invalid_cidr() -> None:
    """Storage L3 subnet creation should report invalid CIDR input clearly."""
    module = _load_network_script("storage_l3_routing_test.py")

    with pytest.raises(RuntimeError, match="Invalid CIDR 'not-a-cidr'"):
        module.create_subnets(object(), "vpc-storage", "not-a-cidr", "suffix", [])


def test_storage_l3_create_subnets_rejects_cidr_without_two_24_subnets() -> None:
    """Storage L3 subnet creation should validate CIDR capacity before AWS calls."""
    module = _load_network_script("storage_l3_routing_test.py")
    ec2 = FakeStorageL3SubnetEc2()

    with pytest.raises(RuntimeError, match="CIDR 10\\.86\\.0\\.0/24 cannot provide two /24 subnets"):
        module.create_subnets(ec2, "vpc-storage", "10.86.0.0/24", "suffix", [])

    assert ec2.create_subnet_calls == []


def test_storage_l3_main_rejects_too_few_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """AWS storage L3 should reject host counts below the contract minimum before AWS calls."""
    module = _load_network_script("storage_l3_routing_test.py")
    monkeypatch.setattr(sys, "argv", ["storage_l3_routing_test.py", "--region", "us-west-2", "--hosts", "2"])
    monkeypatch.setattr(module.boto3, "client", lambda service, region_name: pytest.fail("unexpected AWS client"))

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert exc.value.code == 2


def test_storage_l3_main_cleans_partial_vpc_when_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed VPC bootstrap that returns a VPC ID should still clean up that VPC."""
    module = _load_network_script("storage_l3_routing_test.py")
    fake_ec2 = object()
    cleaned_vpcs: list[tuple[str, list[str] | None, list[str] | None]] = []

    def fake_boto3_client(service: str, region_name: str) -> Any:
        assert region_name == "us-west-2"
        return fake_ec2 if service == "ec2" else object()

    def fake_cleanup_vpc_resources(
        ec2: Any,
        vpc_id: str,
        subnet_ids: list[str] | None = None,
        sg_ids: list[str] | None = None,
    ) -> None:
        cleaned_vpcs.append((vpc_id, subnet_ids, sg_ids))

    monkeypatch.setattr(module.boto3, "client", fake_boto3_client)
    monkeypatch.setattr(module, "get_amazon_linux_ami", lambda ec2: "ami-storage")
    monkeypatch.setattr(
        module,
        "create_test_vpc",
        lambda ec2, cidr, name, *, enable_dns=False: {
            "passed": False,
            "vpc_id": "vpc-partial",
            "error": "quota exceeded",
        },
    )
    monkeypatch.setattr(module, "cleanup_vpc_resources", fake_cleanup_vpc_resources)
    monkeypatch.setattr(sys, "argv", ["storage_l3_routing_test.py", "--region", "us-west-2"])

    exit_code = module.main()

    assert exit_code == 1
    assert cleaned_vpcs == [("vpc-partial", [], None)]


def _patch_storage_l3_main(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    fake_ec2: FakeStorageL3Ec2,
    run_ssm_command: Any,
) -> list[bool]:
    """Patch storage L3 main dependencies and return observed DNS flags."""
    create_test_vpc_dns_flags: list[bool] = []

    def fake_boto3_client(service: str, region_name: str) -> Any:
        assert region_name == "us-west-2"
        return fake_ec2 if service == "ec2" else object()

    def fake_create_test_vpc(ec2: Any, cidr: str, name: str, *, enable_dns: bool = False) -> dict[str, Any]:
        create_test_vpc_dns_flags.append(enable_dns)
        return {"passed": True, "vpc_id": "vpc-storage"}

    def fake_create_subnets(
        ec2: Any, vpc_id: str, cidr: str, suffix: str, created_ids: list[str]
    ) -> list[dict[str, Any]]:
        created_ids.extend(["subnet-a", "subnet-b"])
        return [
            {"subnet_id": "subnet-a", "cidr": "10.86.1.0/24", "az": "us-west-2a"},
            {"subnet_id": "subnet-b", "cidr": "10.86.2.0/24", "az": "us-west-2b"},
        ]

    def fake_launch_hosts(
        ec2: Any,
        ami: str,
        subnets: list[dict[str, Any]],
        sg_id: str,
        profile: str,
        count: int,
        created_ids: list[str],
    ) -> list[dict[str, Any]]:
        created_ids.extend(["i-a", "i-b", "i-c"])
        return [
            {"instance_id": "i-a", "subnet_id": "subnet-a", "private_ip": "10.86.1.10"},
            {"instance_id": "i-b", "subnet_id": "subnet-b", "private_ip": "10.86.2.10"},
            {"instance_id": "i-c", "subnet_id": "subnet-a", "private_ip": "10.86.1.11"},
        ]

    monkeypatch.setattr(module.boto3, "client", fake_boto3_client)
    monkeypatch.setattr(module, "get_amazon_linux_ami", lambda ec2: "ami-storage")
    monkeypatch.setattr(module, "create_test_vpc", fake_create_test_vpc)
    monkeypatch.setattr(module, "create_subnets", fake_create_subnets)
    monkeypatch.setattr(module, "create_intra_vpc_sg", lambda ec2, vpc_id, vpc_cidr, suffix: "sg-storage")
    monkeypatch.setattr(module, "create_ssm_instance_profile", lambda iam, description: ("role", "profile"))
    monkeypatch.setattr(module, "launch_hosts", fake_launch_hosts)
    monkeypatch.setattr(module, "wait_ssm_ready_all", lambda ssm, instance_ids: [])
    monkeypatch.setattr(module, "run_ssm_command", run_ssm_command)
    monkeypatch.setattr(module, "cleanup_vpc_resources", lambda ec2, vpc_id, subnet_ids, sg_ids: None)
    monkeypatch.setattr(module, "delete_ssm_instance_profile", lambda iam, role_name, profile_name: None)
    monkeypatch.setattr(sys, "argv", ["storage_l3_routing_test.py", "--region", "us-west-2"])
    return create_test_vpc_dns_flags


def test_storage_l3_main_creates_ssm_private_endpoints_before_waiting_for_ssm(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Storage L3 main must make isolated subnets able to reach SSM before SSM polling."""
    module = _load_network_script("storage_l3_routing_test.py")
    fake_ec2 = FakeStorageL3Ec2()
    events: list[str] = []
    original_create_ssm_vpc_endpoints = module.create_ssm_vpc_endpoints

    def fake_create_ssm_vpc_endpoints(
        ec2: Any, vpc_id: str, subnet_ids: list[str], sg_id: str, region: str, suffix: str
    ) -> list[str]:
        events.append("ssm_endpoints")
        return original_create_ssm_vpc_endpoints(ec2, vpc_id, subnet_ids, sg_id, region, suffix)

    def fake_wait_ssm_ready_all(ssm: Any, instance_ids: list[str]) -> list[str]:
        events.append("ssm_ready")
        return []

    def fake_run_ssm_command(ssm: Any, instance_id: str, command: str) -> tuple[bool, str]:
        if command.startswith("ip route get "):
            return True, "10.86.2.10 dev eth0 src 10.86.1.10"
        return True, ""

    create_test_vpc_dns_flags = _patch_storage_l3_main(module, monkeypatch, fake_ec2, fake_run_ssm_command)
    monkeypatch.setattr(module, "wait_ssm_ready_all", fake_wait_ssm_ready_all)
    monkeypatch.setattr(module, "create_ssm_vpc_endpoints", fake_create_ssm_vpc_endpoints)

    exit_code = module.main()

    assert exit_code == 0
    assert create_test_vpc_dns_flags == [True]
    assert events[0] == "ssm_endpoints"
    assert {request["ServiceName"] for request in fake_ec2.endpoint_requests} == {
        "com.amazonaws.us-west-2.ssm",
        "com.amazonaws.us-west-2.ssmmessages",
        "com.amazonaws.us-west-2.ec2messages",
    }
    assert all(request["PrivateDnsEnabled"] is True for request in fake_ec2.endpoint_requests)
    assert all(request["SubnetIds"] == ["subnet-a", "subnet-b"] for request in fake_ec2.endpoint_requests)
    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert payload["success"] is True


def test_storage_l3_main_accepts_ec2_cross_subnet_routes_via_subnet_router(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """EC2 guest routes may show a subnet router while the effective VPC route is still local."""
    module = _load_network_script("storage_l3_routing_test.py")
    fake_ec2 = FakeStorageL3Ec2()
    ssm_commands: list[str] = []

    def fake_run_ssm_command(ssm: Any, instance_id: str, command: str) -> tuple[bool, str]:
        ssm_commands.append(command)
        if command.startswith("ip route get "):
            return True, "10.86.2.10 via 10.86.1.1 dev eth0 src 10.86.1.10"
        return True, ""

    _patch_storage_l3_main(module, monkeypatch, fake_ec2, fake_run_ssm_command)

    exit_code = module.main()

    assert exit_code == 0
    assert all(command.startswith("ping ") for command in ssm_commands)
    assert len(ssm_commands) == 6
    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert payload["tests"]["all_to_all_reachable"] == {
        "passed": True,
        "pairs_tested": 6,
        "pairs_reachable": 6,
    }
    assert payload["tests"]["cross_subnet_routing"] == {
        "passed": True,
        "pairs_tested": 4,
        "pairs_reachable": 4,
    }
    assert payload["tests"]["no_gateway_hop"] == {
        "passed": True,
        "pairs_tested": 4,
        "pairs_direct": 4,
    }


def test_storage_l3_main_rejects_reachable_cross_subnet_pairs_via_transit_gateway(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reachability must not hide a more-specific prohibited gateway route."""
    module = _load_network_script("storage_l3_routing_test.py")

    class FakeGatewayStorageL3Ec2(FakeStorageL3Ec2):
        """Expose local routing generally but a more-specific gateway route from subnet A."""

        def describe_route_tables(self, Filters: list[dict[str, Any]]) -> dict[str, Any]:
            """Return a selected transit-gateway route for subnet-A to subnet-B traffic."""
            assert Filters == [{"Name": "vpc-id", "Values": ["vpc-storage"]}]
            return {
                "RouteTables": [
                    {
                        "RouteTableId": "rtb-subnet-a",
                        "Associations": [{"SubnetId": "subnet-a"}],
                        "Routes": [
                            {
                                "DestinationCidrBlock": "10.86.0.0/16",
                                "GatewayId": "local",
                                "State": "active",
                            },
                            {
                                "DestinationCidrBlock": "10.86.2.0/24",
                                "TransitGatewayId": "tgw-prohibited",
                                "State": "active",
                            },
                        ],
                    },
                    {
                        "RouteTableId": "rtb-main",
                        "Associations": [{"Main": True}],
                        "Routes": [
                            {
                                "DestinationCidrBlock": "10.86.0.0/16",
                                "GatewayId": "local",
                                "State": "active",
                            }
                        ],
                    },
                ]
            }

    fake_ec2 = FakeGatewayStorageL3Ec2()

    def fake_run_ssm_command(ssm: Any, instance_id: str, command: str) -> tuple[bool, str]:
        return True, ""

    _patch_storage_l3_main(module, monkeypatch, fake_ec2, fake_run_ssm_command)

    exit_code = module.main()

    assert exit_code == 1
    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert payload["tests"]["all_to_all_reachable"] == {
        "passed": True,
        "pairs_tested": 6,
        "pairs_reachable": 6,
    }
    assert payload["tests"]["cross_subnet_routing"] == {
        "passed": True,
        "pairs_tested": 4,
        "pairs_reachable": 4,
    }
    assert payload["tests"]["no_gateway_hop"]["passed"] is False
    assert payload["tests"]["no_gateway_hop"]["pairs_tested"] == 4
    assert payload["tests"]["no_gateway_hop"]["pairs_direct"] == 2
    assert "TransitGatewayId=tgw-prohibited" in payload["tests"]["no_gateway_hop"]["error"]
    assert payload["success"] is False


def test_storage_l3_main_records_cleanup_failure_without_losing_result(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AWS storage L3 cleanup failures should not suppress the script JSON result."""
    module = _load_network_script("storage_l3_routing_test.py")
    fake_ec2 = FakeStorageL3Ec2()

    def fake_run_ssm_command(ssm: Any, instance_id: str, command: str) -> tuple[bool, str]:
        return True, ""

    def fail_cleanup(ec2: Any, vpc_id: str, subnet_ids: list[str], sg_ids: list[str]) -> None:
        raise RuntimeError("cleanup boom")

    _patch_storage_l3_main(module, monkeypatch, fake_ec2, fake_run_ssm_command)
    monkeypatch.setattr(module, "cleanup_vpc_resources", fail_cleanup)

    exit_code = module.main()

    assert exit_code == 1
    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert payload["test_name"] == "storage_l3_routing"
    assert payload["success"] is False
    assert payload["cleanup"] is False
    assert payload["cleanup_errors"] == ["vpc cleanup failed: cleanup boom"]
    assert payload["error"] == "Cleanup failed: vpc cleanup failed: cleanup boom"


def test_stable_egress_main_uses_cidr_parser_and_emits_minimal_contract(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AWS stable egress output should stay minimal and subnet CIDR derivation should be CIDR-aware."""
    module = _load_network_script("stable_egress_ip_test.py")
    fake_ec2 = FakeStableEgressEc2()

    monkeypatch.setattr(module.boto3, "client", lambda service, region_name: fake_ec2)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        module,
        "create_test_vpc",
        lambda ec2, cidr, name: {"passed": True, "vpc_id": "vpc-egress"},
    )
    monkeypatch.setattr(module, "create_internet_routing", lambda ec2, vpc_id, subnet_id, name, routing: None)
    monkeypatch.setattr(module, "create_security_group", lambda ec2, vpc_id, name, description: "sg-egress")
    monkeypatch.setattr(module, "create_key_pair", lambda ec2, key_name: "/tmp/isv-missing-egress-key.pem")
    monkeypatch.setattr(
        module,
        "launch_instance",
        lambda ec2, subnet_id, sg_id, key_name, name: {
            "passed": True,
            "instance_id": "i-egress",
            "public_ip": "198.51.100.10",
            "message": "Launched instance i-egress with public IP 198.51.100.10",
        },
    )
    monkeypatch.setattr(
        module,
        "probe_egress_ip",
        lambda public_ip, key_file, endpoint, probes, interval_seconds, ssh_user: {
            "passed": True,
            "ips": ["203.0.113.20"] * probes,
            "endpoint": endpoint,
            "probes": probes,
            "message": f"Collected {probes} egress IP probes from {endpoint}",
        },
    )
    monkeypatch.setattr(module, "delete_with_retry", lambda func, **kwargs: True)
    monkeypatch.setattr(module, "delete_vpc", lambda ec2, vpc_id: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stable_egress_ip_test.py",
            "--region",
            "us-west-2",
            "--cidr",
            "10.88.16.0/20",
            "--probes",
            "2",
        ],
    )

    exit_code = module.main()

    assert exit_code == 0
    assert fake_ec2.created_subnet_cidrs == ["10.88.16.0/24"]
    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    _assert_stable_egress_contract(payload)
    assert payload["tests"]["probe_egress_ip"]["probes"] == 2


def test_my_isv_stable_egress_demo_emits_minimal_contract() -> None:
    """my-isv stable egress demo output should model the provider-neutral contract."""
    script = MY_ISV_NETWORK_SCRIPTS / "stable_egress_ip_test.py"
    env = os.environ | {"ISVCTL_DEMO_MODE": "1"}

    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(script),
                "--region",
                "demo-region",
                "--probes",
                "2",
            ],
            capture_output=True,
            env=env,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"{script} timed out after {exc.timeout} seconds\nstdout: {exc.stdout!r}\nstderr: {exc.stderr!r}")

    assert completed.returncode == 0, completed.stderr
    payload: dict[str, Any] = json.loads(completed.stdout)
    _assert_stable_egress_contract(payload)
    assert payload["tests"]["probe_egress_ip"]["probes"] == 2


def _imex_up_payload(node_a_id: str, node_b_id: str, *, hostnames: bool = False) -> str:
    """Build a real-shaped `nvidia-imex-ctl -N -j -H` UP-domain JSON payload for two nodes.

    Schema confirmed live against `nvidia-imex-ctl -N -j -H` (2026-09-04):
    every configured member gets an entry (not just the queried node), each
    with its own `connections` map keyed by "host" (IP only - never a
    hostname, even when the node's own top-level entry has "hostName" set).
    """
    node_a_host, node_a_name = ("10.0.0.1", node_a_id) if hostnames else (node_a_id, "gpu-node-a")
    node_b_host, node_b_name = ("10.0.0.2", node_b_id) if hostnames else (node_b_id, "gpu-node-b")
    return json.dumps(
        {
            "nodes": {
                "0": {
                    "status": "READY",
                    "host": node_a_host,
                    "hostName": node_a_name,
                    "connections": {
                        "0": {"host": node_a_host, "status": "CONNECTED", "changed": True},
                        "1": {"host": node_b_host, "status": "CONNECTED", "changed": True},
                    },
                },
                "1": {
                    "status": "READY",
                    "host": node_b_host,
                    "hostName": node_b_name,
                    "connections": {
                        "0": {"host": node_a_host, "status": "CONNECTED", "changed": True},
                        "1": {"host": node_b_host, "status": "CONNECTED", "changed": True},
                    },
                },
            },
            "timestamp": "9/4/2026 00:00:00.000",
            "status": "UP",
        }
    )


def test_imex_parse_matches_queried_ip() -> None:
    """--node-ids given as IPs: own node found by `host`, peers reported as IPs."""
    module = _load_network_script("imex_domain_test.py")
    payload = _imex_up_payload("10.0.0.1", "10.0.0.2")

    domain_state, own_status, peers = module._parse_imex_ctl_json(payload, "10.0.0.1")

    assert domain_state == "UP"
    assert own_status == "READY"
    assert peers == ["10.0.0.2"]


def test_imex_parse_matches_queried_hostname() -> None:
    """--node-ids given as hostnames must still resolve the local node and report
    peers as hostnames, not the underlying IPs nvidia-imex-ctl reports in
    `connections` - regression test for a CodeRabbit-flagged bug where hostname
    -configured domains matched nothing and silently reported zero peers."""
    module = _load_network_script("imex_domain_test.py")
    payload = _imex_up_payload("gpu-node-a", "gpu-node-b", hostnames=True)

    domain_state, own_status, peers = module._parse_imex_ctl_json(payload, "gpu-node-a")

    assert domain_state == "UP"
    assert own_status == "READY"
    assert peers == ["gpu-node-b"]


def test_imex_parse_down_domain_reports_no_peers() -> None:
    """A DOWN domain (real payload shape from a version-mismatched daemon) reports
    the domain state but no peers, rather than crashing or fabricating connectivity."""
    module = _load_network_script("imex_domain_test.py")
    payload = json.dumps(
        {
            "nodes": {
                "1": {
                    "status": "UNAVAILABLE",
                    "host": "10.0.0.2",
                    "connections": {
                        "0": {"host": "10.0.0.1", "status": "INVALID", "changed": False},
                        "1": {"host": "10.0.0.2", "status": "INVALID", "changed": False},
                    },
                    "hostName": "N/A",
                },
                "0": {
                    "status": "UNAVAILABLE",
                    "host": "10.0.0.1",
                    "connections": {
                        "1": {"host": "10.0.0.2", "status": "INVALID", "changed": False},
                        "0": {"host": "10.0.0.1", "status": "INVALID", "changed": False},
                    },
                    "hostName": "gpu-node-a",
                },
            },
            "timestamp": "9/4/2026 00:00:00.000",
            "status": "DOWN",
        }
    )

    domain_state, own_status, peers = module._parse_imex_ctl_json(payload, "10.0.0.1")

    assert domain_state == "DOWN"
    # A down daemon reports itself UNAVAILABLE, which maps to service_state
    # "inactive" and domain_member false in the emitted contract.
    assert own_status == "UNAVAILABLE"
    assert module._service_state(own_status) == "inactive"
    assert peers == []


@pytest.mark.parametrize("malformed_payload", ["[]", "null", '{"nodes": []}', '{"nodes": "oops"}'])
def test_imex_parse_rejects_malformed_payload_shapes(malformed_payload: str) -> None:
    """A decoded payload that isn't the expected object shape (list, null, or a
    `nodes` value that isn't an object) must raise ValueError, not AttributeError -
    CodeRabbit regression: query_node only caught JSONDecodeError, so an
    AttributeError from `data.get(...)` on a non-dict payload would have escaped
    ThreadPoolExecutor.map and crashed main() instead of emitting structured JSON."""
    module = _load_network_script("imex_domain_test.py")

    with pytest.raises(ValueError):
        module._parse_imex_ctl_json(malformed_payload, "10.0.0.1")


def test_imex_query_node_reports_malformed_payload_as_query_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """query_node must convert a zero-exit SSH command returning `[]` into a
    per-node ok=False error, not raise - so main()'s ThreadPoolExecutor.map
    still yields structured JSON for every node instead of crashing."""
    module = _load_network_script("imex_domain_test.py")
    monkeypatch.setattr(module, "run_remote", lambda h, *a, **k: {"host": h, "ok": True, "stdout": "[]"})

    result = module.query_node("10.0.0.1", "ubuntu", "/tmp/key.pem", 30)

    assert result["ok"] is False
    assert "could not parse" in result["error"]


def _load_common_module(module_name: str) -> ModuleType:
    """Load a provider-local helper from aws/scripts/common as a module.

    The helper imports its siblings as ``common.*``, exactly as the scripts do,
    so the scripts directory has to be importable first - otherwise this only
    works by accident after some other test has loaded a script.
    """
    scripts_root = ISVCTL_ROOT / "configs" / "providers" / "aws" / "scripts"
    if str(scripts_root) not in sys.path:
        sys.path.insert(0, str(scripts_root))
    script_path = scripts_root / "common" / module_name
    spec = importlib.util.spec_from_file_location(f"test_common_{script_path.stem}", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The node sweep, the CLI options and the under-configured gate are shared by
# both IMEX checks (SDN17-01 and SDN21-01), so they are tested once here rather
# than duplicated per script.


def test_imex_sweep_returns_one_result_per_host() -> None:
    """Every requested host must appear in the result map, keyed by host."""
    imex = _load_common_module("imex.py")

    results = imex.sweep_nodes(["h1", "h2"], lambda host: {"host": host, "ok": True}, deadline=90)

    assert set(results) == {"h1", "h2"}
    assert all(r["ok"] for r in results.values())


def test_imex_sweep_reports_unfinished_hosts_on_deadline() -> None:
    """Hosts that do not answer within the overall deadline must come back as
    timed-out errors, so the caller still emits its full JSON contract instead
    of the orchestrator killing the process at its step timeout with no output."""
    imex = _load_common_module("imex.py")

    def _query(host: str) -> dict[str, Any]:
        if host == "slow":
            time.sleep(5)
        return {"host": host, "ok": True}

    results = imex.sweep_nodes(["fast", "slow"], _query, deadline=1, action="probe")

    assert results["fast"]["ok"] is True
    assert results["slow"]["ok"] is False
    assert "probe did not complete within the 1s deadline" in results["slow"]["error"]


def test_imex_sweep_caps_concurrency() -> None:
    """Concurrency stays capped so a large fleet does not fan out into one ssh
    process per node."""
    imex = _load_common_module("imex.py")
    lock = threading.Lock()
    live = 0
    peak = 0

    def _query(host: str) -> dict[str, Any]:
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.01)
        with lock:
            live -= 1
        return {"host": host, "ok": True}

    hosts = [f"h{n}" for n in range(40)]
    results = imex.sweep_nodes(hosts, _query, deadline=90)

    assert len(results) == len(hosts)
    assert peak <= imex.MAX_PARALLEL_QUERIES


def test_imex_sweep_handles_empty_host_list() -> None:
    """An empty sweep must not raise (ThreadPoolExecutor rejects max_workers=0)."""
    imex = _load_common_module("imex.py")

    assert imex.sweep_nodes([], lambda host: {"host": host, "ok": True}, deadline=90) == {}


@pytest.mark.parametrize(
    ("node_ids", "key_file", "expected"),
    [
        pytest.param([], "", "skip", id="nothing-configured"),
        pytest.param([], "/tmp/k.pem", "skip", id="key-only"),
        pytest.param(["n1"], "", "error", id="nodes-without-key"),
        pytest.param(["n1"], "/tmp/k.pem", None, id="fully-configured"),
    ],
)
def test_imex_config_gate(node_ids: list[str], key_file: str, expected: str | None) -> None:
    """An unconfigured run skips so it cannot break unrelated network runs, but a
    partially configured one stays a hard error - that means someone aimed the
    check at a cluster and got it wrong, which should not pass silently."""
    imex = _load_common_module("imex.py")

    gate = imex.config_gate(node_ids, key_file, subject="IMEX domain")

    if expected is None:
        assert gate is None
    elif expected == "skip":
        assert "skip_reason" in gate
        assert "not configured" in gate["skip_reason"]
    else:
        assert "error" in gate


def test_imex_parse_node_ids_trims_and_drops_blanks() -> None:
    """Node ID parsing is shared, so both checks accept the same spellings."""
    imex = _load_common_module("imex.py")

    assert imex.parse_node_ids(" n1 , ,n2,") == ["n1", "n2"]


def _run_imex_script(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the AWS imex_domain_test.py script with a clean env (no AWS_IMEX_* set)."""
    script = AWS_NETWORK_SCRIPTS / "imex_domain_test.py"
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        check=False,
        timeout=30,
    )


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["--region", "us-west-2"], id="nothing-configured"),
        pytest.param(["--region", "us-west-2", "--key-file", "/tmp/key.pem"], id="key-only"),
    ],
)
def test_imex_skips_when_domain_not_configured(args: list[str]) -> None:
    """SDN21-01 needs a pre-existing multi-node IMEX cluster, which a normal AWS
    network run does not provision. When the run is not pointed at one, the step
    must skip cleanly (exit 0 + skipped payload) instead of failing the whole
    network run - CodeRabbit regression: the step is wired unconditionally, so
    exiting 1 here broke every AWS network run not specifically testing IMEX."""
    completed = _run_imex_script(*args)

    assert completed.returncode == 0, completed.stderr
    payload: dict[str, Any] = json.loads(completed.stdout)
    assert payload["success"] is True
    assert payload["skipped"] is True
    assert "not configured" in payload["skip_reason"]


def test_imex_skipped_payload_matches_output_schema() -> None:
    """The skipped payload must still satisfy the wired imex_domain schema, or the
    orchestrator flags a schema failure on an intentionally-skipped step."""
    from isvctl.config.output_schemas import validate_output

    completed = _run_imex_script("--region", "us-west-2")
    payload: dict[str, Any] = json.loads(completed.stdout)

    is_valid, errors = validate_output(payload, "imex_domain")
    assert is_valid, errors


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["--node-ids", "node-a", "--key-file", "/tmp/key.pem"], id="single-node"),
        pytest.param(["--node-ids", "node-a,node-b"], id="no-key-file"),
    ],
)
def test_imex_partial_configuration_still_fails(args: list[str]) -> None:
    """A partially configured run means someone pointed this at a cluster and got
    it wrong - that must stay a hard error rather than silently skipping."""
    completed = _run_imex_script("--region", "us-west-2", *args)

    assert completed.returncode == 1
    payload: dict[str, Any] = json.loads(completed.stdout)
    assert payload["success"] is False
    assert payload.get("skipped") is not True
    assert payload["error"]


def test_imex_emits_sdn21_step_output_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """The script must emit the SDN21-01 contract shape from the issue: a nested
    `domain` object plus a `nodes` array of per-node reports, not a flat payload."""
    module = _load_network_script("imex_domain_test.py")
    payload = _imex_up_payload("10.0.0.1", "10.0.0.2")
    monkeypatch.setattr(module, "run_remote", lambda h, *a, **k: {"host": h, "ok": True, "stdout": payload})
    monkeypatch.setattr(
        sys,
        "argv",
        ["imex_domain_test.py", "--region", "us-west-2", "--node-ids", "10.0.0.1,10.0.0.2", "--key-file", "/tmp/k.pem"],
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = module.main()
    emitted: dict[str, Any] = json.loads(buf.getvalue())

    assert rc == 0
    assert emitted["domain"]["domain_id"]
    assert emitted["domain"]["state"] == "up"
    assert emitted["domain"]["expected_members"] == ["10.0.0.1", "10.0.0.2"]
    assert emitted["domain"]["fully_connected"] is True
    assert emitted["nodes_checked"] == 2
    assert emitted["nodes_validated"] == 2
    assert [n["node_id"] for n in emitted["nodes"]] == ["10.0.0.1", "10.0.0.2"]
    for node in emitted["nodes"]:
        assert node["service_state"] == "active"
        assert node["domain_member"] is True
        assert node["peers_reachable"]
    # no leftovers from the old hand-rolled shape
    assert "reachability" not in emitted
    assert "members" not in emitted


def _imex_probe_output(daemon: str = "yes", ctl: str = "yes", load: str = "loaded", boot: str = "disabled") -> str:
    """Build probe stdout in the shape the remote command emits."""
    return f"DAEMON={daemon}\nCTL={ctl}\nLOAD={load}\nBOOT={boot}\n"


def test_imex_service_parses_real_systemd_shape() -> None:
    """Parse the probe output shape a healthy host emits. LoadState/UnitFileState
    values are the real ones observed on a live host with nvidia-imex installed."""
    module = _load_network_script("imex_service_test.py")

    parsed = module._parse_probe(_imex_probe_output())

    assert parsed["service_present"] is True
    assert parsed["control_tooling_present"] is True
    assert parsed["service_registration"] == "loaded"
    assert parsed["boot_disposition"] == "disabled"


@pytest.mark.parametrize(
    ("load_state", "expected"),
    [
        pytest.param("loaded", "loaded", id="loaded"),
        pytest.param("masked", "masked", id="masked"),
        pytest.param("not-found", "not_found", id="systemd-hyphen-normalized"),
        pytest.param("", "error", id="no-answer-from-manager"),
        pytest.param("bogus", "error", id="unrecognized"),
    ],
)
def test_imex_service_normalizes_load_state(load_state: str, expected: str) -> None:
    """systemd LoadState maps onto the contract's normalized enum. Querying the
    manager (rather than the filesystem) is what lets absent be told apart from
    masked - a boolean would collapse the two."""
    module = _load_network_script("imex_service_test.py")

    assert module._parse_probe(_imex_probe_output(load=load_state))["service_registration"] == expected


@pytest.mark.parametrize(
    ("unit_file_state", "expected"),
    [
        pytest.param("enabled", "enabled", id="enabled"),
        pytest.param("disabled", "disabled", id="disabled"),
        pytest.param("static", "static", id="static"),
        pytest.param("", "none", id="empty"),
        pytest.param("indirect", "unknown", id="unrecognized"),
        pytest.param("masked", "none", id="masked-has-no-boot-value"),
    ],
)
def test_imex_service_normalizes_boot_disposition(unit_file_state: str, expected: str) -> None:
    """Boot disposition is evidence only, but still normalized to the contract enum."""
    module = _load_network_script("imex_service_test.py")

    assert module._parse_probe(_imex_probe_output(boot=unit_file_state))["boot_disposition"] == expected


def test_imex_service_control_tooling_present_but_not_invocable() -> None:
    """The requirement is that the control tool is *invocable*, so a binary that
    exists but fails to run is not counted as present."""
    module = _load_network_script("imex_service_test.py")

    parsed = module._parse_probe(_imex_probe_output(ctl="present_not_invocable"))

    assert parsed["control_tooling_present"] is False


def test_imex_service_probe_targets_roles_not_package_names() -> None:
    """The daemon ships branch-versioned and the control tool is a binary inside
    that package, so the probe must look for the artifacts by role and query the
    service manager - never `dpkg`/`rpm` on a fixed package name."""
    module = _load_network_script("imex_service_test.py")

    probe = module._probe_command("nvidia-imex.service", "nvidia-imex", "nvidia-imex-ctl")

    assert "command -v nvidia-imex" in probe
    assert "command -v nvidia-imex-ctl" in probe
    assert "systemctl show nvidia-imex.service -p LoadState" in probe
    assert "dpkg" not in probe and "rpm" not in probe
    assert "/usr/lib/systemd" not in probe  # query the manager, not the filesystem


def test_imex_service_unreachable_node_stays_in_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """A node that cannot be reached must not silently drop out of the asserted
    set - it stays in scope and is reported as an error registration."""
    module = _load_network_script("imex_service_test.py")
    monkeypatch.setattr(
        module, "run_remote", lambda h, *a, **k: {"host": h, "ok": False, "error": "connection refused"}
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["imex_service_test.py", "--region", "us-west-2", "--node-ids", "n1", "--key-file", "/tmp/k.pem"],
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = module.main()
    emitted: dict[str, Any] = json.loads(buf.getvalue())

    assert rc == 1
    assert emitted["nodes_checked"] == 1
    assert emitted["nodes_validated"] == 0
    node = emitted["nodes"][0]
    assert node["in_nvlink_allocation"] is True
    assert node["service_registration"] == "error"


def test_imex_service_emits_sdn17_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """The script emits the SDN17-01 contract shape from the issue."""
    module = _load_network_script("imex_service_test.py")
    monkeypatch.setattr(
        module, "run_remote", lambda h, *a, **k: {"host": h, "ok": True, "stdout": _imex_probe_output()}
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["imex_service_test.py", "--region", "us-west-2", "--node-ids", "n1,n2", "--key-file", "/tmp/k.pem"],
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = module.main()
    emitted: dict[str, Any] = json.loads(buf.getvalue())

    assert rc == 0
    assert emitted["nodes_checked"] == 2
    assert emitted["nodes_validated"] == 2
    for node in emitted["nodes"]:
        assert node["in_nvlink_allocation"] is True
        assert node["service_present"] is True
        assert node["control_tooling_present"] is True
        assert node["service_registration"] == "loaded"
        assert node["boot_disposition"] == "disabled"


def test_imex_service_skips_when_not_configured() -> None:
    """An unconfigured run skips cleanly instead of failing unrelated network runs."""
    script = AWS_NETWORK_SCRIPTS / "imex_service_test.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--region", "us-west-2"],
        capture_output=True,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    payload: dict[str, Any] = json.loads(completed.stdout)
    assert payload["skipped"] is True
    assert "not configured" in payload["skip_reason"]


# --- SDN18-01: IMEX resilience -------------------------------------------------


def test_imex_resilience_arrival_probe_reads_manager_not_filesystem() -> None:
    """Arrival state is read from the service manager, and the domain-membership
    probe is a plain grep - it runs inside a shell substitution over SSH, where
    piping through a remote interpreter is a reliable source of quoting breakage."""
    module = _load_network_script("imex_resilience_test.py")

    probe = module._arrival_command("nvidia-imex.service", "nvidia-imex")

    assert "systemctl is-active nvidia-imex.service" in probe
    assert "systemctl show nvidia-imex.service -p UnitFileState" in probe
    assert "systemctl show nvidia-imex.service -p Restart" in probe
    assert "nvidia-imex-ctl -N -j" in probe
    assert "python3 -c" not in probe


@pytest.mark.parametrize(
    ("unit_file_state", "expected"),
    [
        pytest.param("enabled", True, id="enabled"),
        pytest.param("enabled-runtime", False, id="enabled-runtime-lives-under-run-and-is-erased-by-reboot"),
        pytest.param("static", False, id="static-does-not-prove-the-boot-target-pulls-it-in"),
        pytest.param("disabled", False, id="disabled"),
        pytest.param("masked", False, id="masked"),
        pytest.param("", False, id="unreported"),
    ],
)
def test_imex_resilience_boot_persistence_readback(unit_file_state: str, expected: bool) -> None:
    """Boot persistence is read back from the manager's own enablement state."""
    module = _load_network_script("imex_resilience_test.py")

    assert module._boot_persistence_configured(unit_file_state) is expected


def test_imex_resilience_kills_rather_than_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stimulus must be a kill. A correct supervisor deliberately does not
    restart a graceful stop, so `systemctl stop` would fail good nodes."""
    module = _load_network_script("imex_resilience_test.py")
    issued: list[str] = []

    def _remote(host: str, user: str, key_file: str, command: str, timeout: int) -> dict[str, Any]:
        """Fake a healthy node: running on arrival, then replaced after the kill."""
        issued.append(command)
        if "ACTIVE=" in command and "PID=" in command:
            return {
                "host": host,
                "ok": True,
                "stdout": "ACTIVE=active\nPID=123\nBOOT=enabled\nRESTART=always\nMEMBER=yes\n",
            }
        if "pkill" in command:
            return {"host": host, "ok": True, "stdout": ""}
        return {"host": host, "ok": True, "stdout": "PID=456\nMEMBER=yes\nACTIVE=active\n"}

    monkeypatch.setattr(module, "run_remote", _remote)
    monkeypatch.setattr(
        sys,
        "argv",
        ["imex_resilience_test.py", "--region", "r", "--node-ids", "n1", "--key-file", "/tmp/k.pem"],
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = module.main()
    emitted: dict[str, Any] = json.loads(buf.getvalue())

    assert rc == 0
    assert any("pkill -9" in c for c in issued), "expected an outright kill"
    assert not any("systemctl stop" in c for c in issued), "a graceful stop is the wrong stimulus"
    assert emitted["operations"]["terminate"] == {"method": "kill", "confirmed": True}
    assert emitted["operations"]["unaided_presence"]["started_by_test"] is False
    assert emitted["operations"]["recovery"]["domain_member"] is True
    assert emitted["operations"]["restore"]["restored_to"] == "active"


def test_imex_resilience_never_starts_a_stopped_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """A node that arrives not running must fail without being repaired - starting
    it would destroy the very property under test, and it indicts provisioning."""
    module = _load_network_script("imex_resilience_test.py")
    issued: list[str] = []

    def _remote(host: str, user: str, key_file: str, command: str, timeout: int) -> dict[str, Any]:
        """Fake a node that arrives with IMEX already stopped."""
        issued.append(command)
        return {"host": host, "ok": True, "stdout": "ACTIVE=inactive\nPID=\nBOOT=enabled\nRESTART=no\nMEMBER=no\n"}

    monkeypatch.setattr(module, "run_remote", _remote)
    monkeypatch.setattr(
        sys,
        "argv",
        ["imex_resilience_test.py", "--region", "r", "--node-ids", "n1", "--key-file", "/tmp/k.pem"],
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = module.main()
    emitted: dict[str, Any] = json.loads(buf.getvalue())

    assert rc == 1
    assert not any("systemctl start" in c or "systemctl restart" in c for c in issued)
    assert not any("pkill" in c for c in issued), "must not kill a service that was already down"
    assert emitted["operations"]["unaided_presence"]["running_on_arrival"] is False


def test_imex_resilience_reports_elapsed_when_node_never_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a node with no restart policy the daemon never comes back. Elapsed time
    is still reported, and prior state is restored despite the failure."""
    module = _load_network_script("imex_resilience_test.py")

    def _remote(host: str, user: str, key_file: str, command: str, timeout: int) -> dict[str, Any]:
        """Fake an unsupervised node: killed daemon never returns."""
        if "ACTIVE=" in command and "PID=" in command and "BOOT=" in command:
            return {
                "host": host,
                "ok": True,
                "stdout": "ACTIVE=active\nPID=123\nBOOT=enabled\nRESTART=no\nMEMBER=yes\n",
            }
        if "pkill" in command:
            return {"host": host, "ok": True, "stdout": ""}
        if "systemctl restart" in command:
            return {"host": host, "ok": True, "stdout": "ACTIVE=active\nMEMBER=yes\n"}
        return {"host": host, "ok": True, "stdout": "PID=\nMEMBER=no\n"}  # never recovers

    monkeypatch.setattr(module, "run_remote", _remote)
    monkeypatch.setattr(module, "RECOVERY_POLL_SECONDS", 0)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "imex_resilience_test.py",
            "--region",
            "r",
            "--node-ids",
            "n1",
            "--key-file",
            "/tmp/k.pem",
            "--recovery-timeout",
            "1",
        ],
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = module.main()
    emitted: dict[str, Any] = json.loads(buf.getvalue())

    assert rc == 1
    recovery = emitted["operations"]["recovery"]
    assert recovery["domain_member"] is False
    assert recovery["elapsed_seconds"] >= 0
    assert recovery["operator_intervention"] is False
    assert "restart policy: no" in emitted["error"]
    # Destructive check: prior state must be put back even on failure.
    assert emitted["operations"]["restore"]["restored_to"] == "active"


def test_imex_resilience_skips_when_not_configured() -> None:
    """An unconfigured run skips cleanly instead of failing unrelated network runs."""
    script = AWS_NETWORK_SCRIPTS / "imex_resilience_test.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--region", "us-west-2"],
        capture_output=True,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    payload: dict[str, Any] = json.loads(completed.stdout)
    assert payload["skipped"] is True
    assert "not configured" in payload["skip_reason"]


def test_imex_resilience_accepts_fast_supervisor_replacing_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """A supervisor fast enough to replace the daemon inside the post-kill settle
    window must count as confirmed-killed and recovered, not as a failed kill.

    Regression: treating any live PID as proof the kill failed rejected exactly
    the well-supervised nodes this check exists to pass.
    """
    module = _load_network_script("imex_resilience_test.py")

    def _remote(host: str, user: str, key_file: str, command: str, timeout: int) -> dict[str, Any]:
        """Fake a supervisor that has already replaced the process post-kill."""
        if "BOOT=" in command:
            return {
                "host": host,
                "ok": True,
                "stdout": "ACTIVE=active\nPID=111\nBOOT=enabled\nRESTART=always\nMEMBER=yes\n",
            }
        if "pkill" in command:
            # New PID already present in the settle window, domain already back.
            return {"host": host, "ok": True, "stdout": "PID=222\nMEMBER=yes\n"}
        return {"host": host, "ok": True, "stdout": "ACTIVE=active\nMEMBER=yes\n"}

    monkeypatch.setattr(module, "run_remote", _remote)
    monkeypatch.setattr(
        sys,
        "argv",
        ["imex_resilience_test.py", "--region", "r", "--node-ids", "n1", "--key-file", "/tmp/k.pem"],
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = module.main()
    emitted: dict[str, Any] = json.loads(buf.getvalue())

    assert rc == 0
    assert emitted["operations"]["terminate"]["confirmed"] is True
    assert emitted["operations"]["recovery"]["domain_member"] is True


def test_imex_resilience_rejects_kill_that_left_original_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the SAME pid is still running, the kill genuinely did not land."""
    module = _load_network_script("imex_resilience_test.py")

    def _remote(host: str, user: str, key_file: str, command: str, timeout: int) -> dict[str, Any]:
        """Fake a daemon that survived the kill with its original pid."""
        if "BOOT=" in command:
            return {
                "host": host,
                "ok": True,
                "stdout": "ACTIVE=active\nPID=111\nBOOT=enabled\nRESTART=no\nMEMBER=yes\n",
            }
        return {"host": host, "ok": True, "stdout": "PID=111\nMEMBER=yes\n"}

    monkeypatch.setattr(module, "run_remote", _remote)
    monkeypatch.setattr(
        sys,
        "argv",
        ["imex_resilience_test.py", "--region", "r", "--node-ids", "n1", "--key-file", "/tmp/k.pem"],
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = module.main()
    emitted: dict[str, Any] = json.loads(buf.getvalue())

    assert rc == 1
    assert emitted["operations"]["terminate"]["confirmed"] is False
    assert "could not confirm" in emitted["error"]


def test_imex_resilience_times_recovery_from_before_the_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    """Elapsed is measured from the kill request, so the round trip and settle
    are inside the window rather than silently extending the recovery bound."""
    module = _load_network_script("imex_resilience_test.py")
    clock = {"t": 0.0}

    def _remote(host: str, user: str, key_file: str, command: str, timeout: int) -> dict[str, Any]:
        """Fake a node whose kill round trip burns 5s before recovery is observed."""
        if "BOOT=" in command:
            return {
                "host": host,
                "ok": True,
                "stdout": "ACTIVE=active\nPID=111\nBOOT=enabled\nRESTART=always\nMEMBER=yes\n",
            }
        if "pkill" in command:
            clock["t"] += 5.0  # SSH round trip + the remote settle sleep
            return {"host": host, "ok": True, "stdout": "PID=222\nMEMBER=yes\n"}
        return {"host": host, "ok": True, "stdout": "ACTIVE=active\nMEMBER=yes\n"}

    monkeypatch.setattr(module, "run_remote", _remote)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(
        sys,
        "argv",
        ["imex_resilience_test.py", "--region", "r", "--node-ids", "n1", "--key-file", "/tmp/k.pem"],
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        module.main()
    emitted: dict[str, Any] = json.loads(buf.getvalue())

    # Would be 0.0 if timing started after the kill returned.
    assert emitted["operations"]["recovery"]["elapsed_seconds"] == 5.0


def test_imex_resilience_restores_even_when_termination_unconfirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`run_remote` can fail after the remote shell already ran pkill - an SSH
    disconnect or command timeout. Bailing out there would leave the daemon down,
    so the destructive check must still restore before reporting the failure.
    """
    module = _load_network_script("imex_resilience_test.py")
    issued: list[str] = []

    def _remote(host: str, user: str, key_file: str, command: str, timeout: int) -> dict[str, Any]:
        """Fake an SSH failure on the kill round trip, after pkill may have landed."""
        issued.append(command)
        if "BOOT=" in command:
            return {
                "host": host,
                "ok": True,
                "stdout": "ACTIVE=active\nPID=111\nBOOT=enabled\nRESTART=always\nMEMBER=yes\n",
            }
        if "pkill" in command:
            return {"host": host, "ok": False, "error": "connection closed by remote host"}
        return {"host": host, "ok": True, "stdout": "ACTIVE=active\nMEMBER=yes\n"}

    monkeypatch.setattr(module, "run_remote", _remote)
    monkeypatch.setattr(
        sys,
        "argv",
        ["imex_resilience_test.py", "--region", "r", "--node-ids", "n1", "--key-file", "/tmp/k.pem"],
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = module.main()
    emitted: dict[str, Any] = json.loads(buf.getvalue())

    assert rc == 1
    assert emitted["operations"]["terminate"]["confirmed"] is False
    # The node must not be abandoned in whatever state the failed kill left it.
    assert any("systemctl restart" in c for c in issued), "expected restoration despite the failed kill"
    assert emitted["operations"]["restore"]["restored_to"] == "active"
    assert emitted["operations"]["restore"]["domain_member"] is True


def test_imex_resilience_refuses_multiple_nodes() -> None:
    """This check kills a daemon, so it must never silently pick one of several
    nodes to do that to - the node is named deliberately, one per run."""
    script = AWS_NETWORK_SCRIPTS / "imex_resilience_test.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--region", "r", "--node-ids", "n1,n2", "--key-file", "/tmp/k.pem"],
        capture_output=True,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 1
    payload: dict[str, Any] = json.loads(completed.stdout)
    assert "exactly one node" in payload["error"]


def test_imex_resilience_rejects_recovery_observed_after_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe can itself take up to the SSH timeout, so a result landing after
    the bound must not be accepted - that would report success past the very
    deadline the bound exists to enforce."""
    module = _load_network_script("imex_resilience_test.py")
    clock = {"t": 0.0}

    def _remote(host: str, user: str, key_file: str, command: str, timeout: int) -> dict[str, Any]:
        """Fake a recovery probe that only returns well after the deadline."""
        if "BOOT=" in command:
            return {
                "host": host,
                "ok": True,
                "stdout": "ACTIVE=active\nPID=111\nBOOT=enabled\nRESTART=always\nMEMBER=yes\n",
            }
        if "pkill" in command:
            return {"host": host, "ok": True, "stdout": "PID=\nMEMBER=no\n"}
        if "systemctl restart" in command:
            return {"host": host, "ok": True, "stdout": "ACTIVE=active\nMEMBER=yes\n"}
        clock["t"] += 90.0  # probe overruns the 10s bound before answering
        return {"host": host, "ok": True, "stdout": "PID=222\nMEMBER=yes\n"}

    monkeypatch.setattr(module, "run_remote", _remote)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "imex_resilience_test.py",
            "--region",
            "r",
            "--node-ids",
            "n1",
            "--key-file",
            "/tmp/k.pem",
            "--recovery-timeout",
            "10",
        ],
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = module.main()
    emitted: dict[str, Any] = json.loads(buf.getvalue())

    assert rc == 1
    assert emitted["operations"]["recovery"]["domain_member"] is False
    assert emitted["operations"]["recovery"]["elapsed_seconds"] > 10


# --- SDN19-01: deliberate node departure ---------------------------------------


def _imex_domain_payload(
    target_conn: str = "CONNECTED",
    *,
    domain_status: str = "DEGRADED",
    observer_status: str = "READY",
) -> str:
    """Build a `nvidia-imex-ctl -N -j` payload shaped like real observed output.

    Defaults mirror a live healthy 2-node AWS domain: the domain reports
    DEGRADED even while both members are up, and each node reports its peer
    UNAVAILABLE while their connections are CONNECTED. Those two quirks are why
    availability is read from the observer's own connections map, and why
    survivor health is read from the observer's own status rather than the
    domain-level one.
    """
    return json.dumps(
        {
            "nodes": {
                "0": {
                    "status": observer_status,
                    "host": "10.0.0.2",
                    "hostName": "node-b",
                    "connections": {
                        "0": {"host": "10.0.0.2", "status": "CONNECTED"},
                        "1": {"host": "10.0.0.1", "status": target_conn},
                    },
                },
                "1": {"status": "UNAVAILABLE", "host": "10.0.0.1", "hostName": "node-a", "connections": {}},
            },
            "status": domain_status,
        }
    )


def _target_ready_payload() -> str:
    """A payload in which the target node reports itself an operational member."""
    return json.dumps(
        {
            "nodes": {
                "0": {"status": "READY", "host": "10.0.0.1", "hostName": "node-a", "connections": {}},
                "1": {"status": "READY", "host": "10.0.0.2", "hostName": "node-b", "connections": {}},
            },
            "status": "DEGRADED",
        }
    )


def test_imex_departure_reads_availability_from_observer_connections() -> None:
    """Availability comes from the observer's own connection to the target.

    The peer's node-level status is not usable: on a live 2-node domain each node
    reported its peer UNAVAILABLE while both daemons were up and connected, so
    matching on it would report the target gone before it was ever stopped.
    """
    module = _load_network_script("imex_departure_test.py")

    connected = _imex_domain_payload("CONNECTED")
    assert module._peer_view(connected, "10.0.0.2", "10.0.0.1") == "available"

    gone = _imex_domain_payload("RECOVERING")
    assert module._peer_view(gone, "10.0.0.2", "10.0.0.1") == "unavailable"


def test_imex_departure_peer_view_resolves_target_by_hostname() -> None:
    """The target may be named by hostname while connections carry only IPs."""
    module = _load_network_script("imex_departure_test.py")

    payload = _imex_domain_payload("RECOVERING")
    assert module._peer_view(payload, "node-b", "node-a") == "unavailable"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("not json", id="unparseable"),
        pytest.param("[]", id="not-an-object"),
        pytest.param('{"nodes": "oops"}', id="nodes-not-an-object"),
        pytest.param('{"nodes": {}}', id="observer-absent"),
    ],
)
def test_imex_departure_peer_view_unknown_on_unusable_output(payload: str) -> None:
    """Output that cannot answer the question maps to `unknown`, never to a
    guess in either direction."""
    module = _load_network_script("imex_departure_test.py")

    assert module._peer_view(payload, "10.0.0.2", "10.0.0.1") == "unknown"


@pytest.mark.parametrize(
    ("target_conn", "expected"),
    [
        pytest.param("CONNECTED", "available", id="connected"),
        pytest.param("RECOVERING", "unavailable", id="recovering-observed-live-after-a-stop"),
        pytest.param("INVALID", "unavailable", id="invalid"),
        pytest.param("NEVER", "unavailable", id="never-connected"),
    ],
)
def test_imex_departure_peer_view_maps_connection_states(target_conn: str, expected: str) -> None:
    """Only CONNECTED is available. A deliberate stop was observed live to move
    the observer's link to RECOVERING, which persists rather than settling to
    INVALID, so anything other than CONNECTED counts as gone."""
    module = _load_network_script("imex_departure_test.py")

    payload = _imex_domain_payload(target_conn)
    assert module._peer_view(payload, "10.0.0.2", "10.0.0.1") == expected


def test_imex_departure_survivors_operational_ignores_domain_status() -> None:
    """Survivor health must not be read from the domain-level status.

    Observed live: a healthy 2-node domain reports DEGRADED, and still reports
    DEGRADED after a member departs - so that field cannot tell "a node left"
    from "the domain fell over", which is the distinction being asserted.
    """
    module = _load_network_script("imex_departure_test.py")

    # Departed target, domain DEGRADED throughout - survivors are still fine.
    healthy = _imex_domain_payload("RECOVERING", domain_status="DEGRADED", observer_status="READY")
    assert module._survivors_operational(healthy, "10.0.0.2", "10.0.0.1") is True

    # Same DEGRADED domain status, but the observer itself is no longer ready.
    collapsed = _imex_domain_payload("RECOVERING", domain_status="DEGRADED", observer_status="UNAVAILABLE")
    assert module._survivors_operational(collapsed, "10.0.0.2", "10.0.0.1") is False


def test_imex_departure_survivors_operational_requires_other_members_connected() -> None:
    """A survivor that lost its link to another *surviving* member is not
    operational, even though the departed node is expected to be gone."""
    module = _load_network_script("imex_departure_test.py")

    payload = json.dumps(
        {
            "nodes": {
                "0": {
                    "status": "READY",
                    "host": "10.0.0.2",
                    "hostName": "node-b",
                    "connections": {
                        "0": {"host": "10.0.0.2", "status": "CONNECTED"},
                        "1": {"host": "10.0.0.1", "status": "RECOVERING"},  # departed, expected
                        "2": {"host": "10.0.0.3", "status": "INVALID"},  # another survivor, not expected
                    },
                },
                "1": {"status": "UNAVAILABLE", "host": "10.0.0.1", "hostName": "node-a", "connections": {}},
                "2": {"status": "READY", "host": "10.0.0.3", "hostName": "node-c", "connections": {}},
            },
            "status": "DEGRADED",
        }
    )
    assert module._survivors_operational(payload, "10.0.0.2", "10.0.0.1") is False


def test_imex_departure_stops_gracefully_never_kills(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stimulus is a graceful stop. A kill is the opposite test (SDN18-01),
    where a supervisor is expected to bring the daemon back."""
    module = _load_network_script("imex_departure_test.py")
    issued: list[str] = []

    def _remote(host: str, user: str, key_file: str, command: str, timeout: int) -> dict[str, Any]:
        """Fake a clean stop followed by the observer noticing the departure."""
        issued.append(command)
        if "systemctl stop" in command:
            return {"host": host, "ok": True, "stdout": "ACTIVE=inactive\n"}
        if "systemctl start" in command:
            return {"host": host, "ok": True, "stdout": ""}
        if "ACTIVE=" in command and "OUT=" in command:
            # Target sample: active and a member both before the stop and after
            # restoration. `_imex_domain_payload` reports node-a READY when it
            # is the one being asked about.
            return {"host": host, "ok": True, "stdout": f"ACTIVE=active\nOUT={_target_ready_payload()}\n"}
        return {"host": host, "ok": True, "stdout": _imex_domain_payload("RECOVERING")}

    monkeypatch.setattr(module, "run_remote", _remote)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "imex_departure_test.py",
            "--region",
            "r",
            "--target-node",
            "10.0.0.1",
            "--observer-node",
            "10.0.0.2",
            "--key-file",
            "/tmp/k.pem",
        ],
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = module.main()
    emitted: dict[str, Any] = json.loads(buf.getvalue())

    assert rc == 0
    assert any("systemctl stop" in c for c in issued)
    assert not any("pkill" in c or "kill -9" in c for c in issued), "a kill is the wrong stimulus here"
    assert emitted["operations"]["stop"] == {"requested": True, "clean_exit": True}
    assert emitted["operations"]["peer_convergence"]["target_reported"] == "unavailable"
    assert emitted["operations"]["restore"]["restored_to"] == "active"


def test_imex_departure_restores_even_when_stop_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restoration is mandatory on every path that may have stopped the service,
    so a stop that did not land cleanly still puts the node back."""
    module = _load_network_script("imex_departure_test.py")
    issued: list[str] = []

    def _remote(host: str, user: str, key_file: str, command: str, timeout: int) -> dict[str, Any]:
        """Fake a stop that leaves the service in an unexpected state."""
        issued.append(command)
        if "systemctl stop" in command:
            return {"host": host, "ok": True, "stdout": "ACTIVE=failed\n"}
        if "systemctl start" in command:
            return {"host": host, "ok": True, "stdout": ""}
        return {"host": host, "ok": True, "stdout": f"ACTIVE=active\nOUT={_target_ready_payload()}\n"}

    monkeypatch.setattr(module, "run_remote", _remote)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "imex_departure_test.py",
            "--region",
            "r",
            "--target-node",
            "10.0.0.1",
            "--observer-node",
            "10.0.0.2",
            "--key-file",
            "/tmp/k.pem",
        ],
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = module.main()
    emitted: dict[str, Any] = json.loads(buf.getvalue())

    assert rc == 1
    assert emitted["operations"]["stop"]["clean_exit"] is False
    assert any("systemctl start" in c for c in issued), "expected restoration despite the failed stop"
    assert emitted["operations"]["restore"]["restored_to"] == "active"


def test_imex_departure_requires_both_ends_named() -> None:
    """This stops a service, so neither the target nor the observer is inferred."""
    script = AWS_NETWORK_SCRIPTS / "imex_departure_test.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--region", "r", "--target-node", "n1", "--key-file", "/tmp/k.pem"],
        capture_output=True,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 1
    assert "must both name a node" in json.loads(completed.stdout)["error"]


def test_imex_departure_rejects_observing_from_the_stopped_node() -> None:
    """The observer has to be a survivor, not the node being stopped."""
    script = AWS_NETWORK_SCRIPTS / "imex_departure_test.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--region",
            "r",
            "--target-node",
            "n1",
            "--observer-node",
            "n1",
            "--key-file",
            "/tmp/k.pem",
        ],
        capture_output=True,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 1
    assert "must be a surviving member" in json.loads(completed.stdout)["error"]


def test_imex_departure_skips_when_not_configured() -> None:
    """An unconfigured run skips cleanly instead of failing unrelated network runs."""
    script = AWS_NETWORK_SCRIPTS / "imex_departure_test.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--region", "r"],
        capture_output=True,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    payload: dict[str, Any] = json.loads(completed.stdout)
    assert payload["skipped"] is True
    assert "not configured" in payload["skip_reason"]


def test_imex_departure_rejects_ambiguous_node_lists() -> None:
    """Silently taking the first of several would aim a destructive stop at an
    ambiguous node, so both ends must name exactly one."""
    script = AWS_NETWORK_SCRIPTS / "imex_departure_test.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--region",
            "r",
            "--target-node",
            "n1,n2",
            "--observer-node",
            "n3",
            "--key-file",
            "/tmp/k.pem",
        ],
        capture_output=True,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 1
    assert "exactly one node" in json.loads(completed.stdout)["error"]


def test_imex_departure_refuses_target_that_was_already_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    """An already-inactive service satisfies "inactive" after the stop, so the run
    could report a departure it never caused - and starting it afterwards would
    not be restoring its prior state either."""
    module = _load_network_script("imex_departure_test.py")
    issued: list[str] = []

    def _remote(host: str, user: str, key_file: str, command: str, timeout: int) -> dict[str, Any]:
        """Fake a target that is already stopped on arrival."""
        issued.append(command)
        return {"host": host, "ok": True, "stdout": "ACTIVE=inactive\nOUT=\n"}

    monkeypatch.setattr(module, "run_remote", _remote)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "imex_departure_test.py",
            "--region",
            "r",
            "--target-node",
            "10.0.0.1",
            "--observer-node",
            "10.0.0.2",
            "--key-file",
            "/tmp/k.pem",
        ],
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = module.main()
    emitted: dict[str, Any] = json.loads(buf.getvalue())

    assert rc == 1
    assert emitted["operations"]["prior_state"]["service_state"] == "inactive"
    assert not any("systemctl stop" in c for c in issued), "must not stop a service that was already down"
    assert "no departure to cause" in emitted["error"]


def test_imex_departure_polls_restoration_rather_than_sampling_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rejoining the domain is not instantaneous. A single early sample would
    report a restoration that did in fact succeed as having failed."""
    module = _load_network_script("imex_departure_test.py")
    samples = {"n": 0}

    def _remote(host: str, user: str, key_file: str, command: str, timeout: int) -> dict[str, Any]:
        """Fake a target that only rejoins the domain on the third sample."""
        if "systemctl stop" in command:
            return {"host": host, "ok": True, "stdout": "ACTIVE=inactive\n"}
        if "systemctl start" in command:
            return {"host": host, "ok": True, "stdout": ""}
        if "ACTIVE=" in command and "OUT=" in command:
            samples["n"] += 1
            if samples["n"] == 1:  # prior-state probe: healthy
                return {"host": host, "ok": True, "stdout": f"ACTIVE=active\nOUT={_target_ready_payload()}\n"}
            if samples["n"] < 4:  # still coming back
                return {"host": host, "ok": True, "stdout": "ACTIVE=activating\nOUT=\n"}
            return {"host": host, "ok": True, "stdout": f"ACTIVE=active\nOUT={_target_ready_payload()}\n"}
        return {"host": host, "ok": True, "stdout": _imex_domain_payload("RECOVERING")}

    monkeypatch.setattr(module, "run_remote", _remote)
    monkeypatch.setattr(module, "RESTORE_POLL_SECONDS", 0)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "imex_departure_test.py",
            "--region",
            "r",
            "--target-node",
            "10.0.0.1",
            "--observer-node",
            "10.0.0.2",
            "--key-file",
            "/tmp/k.pem",
        ],
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = module.main()
    emitted: dict[str, Any] = json.loads(buf.getvalue())

    assert rc == 0
    assert emitted["operations"]["restore"] == {"restored_to": "active", "domain_member": True}


def test_imex_departure_failed_restoration_fails_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restoration gates reported success: this check deliberately degrades the
    domain, so converging peers are not enough if the node was left down."""
    module = _load_network_script("imex_departure_test.py")
    samples = {"n": 0}

    def _remote(host: str, user: str, key_file: str, command: str, timeout: int) -> dict[str, Any]:
        """Fake a target that never comes back after the stop."""
        if "systemctl stop" in command:
            return {"host": host, "ok": True, "stdout": "ACTIVE=inactive\n"}
        if "systemctl start" in command:
            return {"host": host, "ok": True, "stdout": ""}
        if "ACTIVE=" in command and "OUT=" in command:
            samples["n"] += 1
            if samples["n"] == 1:
                return {"host": host, "ok": True, "stdout": f"ACTIVE=active\nOUT={_target_ready_payload()}\n"}
            return {"host": host, "ok": True, "stdout": "ACTIVE=failed\nOUT=\n"}
        return {"host": host, "ok": True, "stdout": _imex_domain_payload("RECOVERING")}

    monkeypatch.setattr(module, "run_remote", _remote)
    monkeypatch.setattr(module, "RESTORE_POLL_SECONDS", 0)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "imex_departure_test.py",
            "--region",
            "r",
            "--target-node",
            "10.0.0.1",
            "--observer-node",
            "10.0.0.2",
            "--key-file",
            "/tmp/k.pem",
            "--restore-timeout",
            "1",
        ],
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = module.main()
    emitted: dict[str, Any] = json.loads(buf.getvalue())

    assert rc == 1
    assert emitted["operations"]["peer_convergence"]["target_reported"] == "unavailable"
    assert emitted["success"] is False
    assert "was left" in emitted["error"]


# --- SDN20-01: reboot rejoin ---------------------------------------------------


def _reboot_state(uptime: str, boot_id: str, active: str = "active", enabled: str = "enabled") -> dict[str, str]:
    """Build a parsed state sample as the reboot probe returns it."""
    return {"UPTIME": uptime, "BOOTID": boot_id, "ACTIVE": active, "ENABLED": enabled, "OUT": ""}


@pytest.mark.parametrize(
    ("before", "after", "expected", "why"),
    [
        pytest.param(
            _reboot_state("3600.0", "aaa"),
            _reboot_state("94.0", "bbb"),
            True,
            "uptime back and boot id changed",
            id="rebooted",
        ),
        pytest.param(
            _reboot_state("3600.0", ""),
            _reboot_state("94.0", ""),
            True,
            "uptime went backwards",
            id="uptime-only",
        ),
        pytest.param(
            _reboot_state("3600.0", "aaa"),
            _reboot_state("3700.0", "bbb"),
            True,
            "boot id changed",
            id="boot-id-only",
        ),
        pytest.param(
            _reboot_state("3600.0", "aaa"),
            _reboot_state("3700.0", "aaa"),
            False,
            "node never went down",
            id="never-rebooted",
        ),
        pytest.param(
            _reboot_state("3600.0", "aaa"),
            {},
            False,
            "no post-reboot sample",
            id="unreadable",
        ),
    ],
)
def test_imex_reboot_confirmation_requires_positive_evidence(
    before: dict[str, str], after: dict[str, str], expected: bool, why: str
) -> None:
    """A reboot is confirmed only by uptime going backwards or the boot id
    changing. Reachability proves nothing: a node that never went down answers
    SSH too, and must not be accepted as having rebooted."""
    module = _load_network_script("imex_reboot_test.py")

    assert module.reboot_confirmed(before, after) is expected, why


def test_imex_reboot_unconfirmed_reboot_fails_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A node that answers but never restarted must fail, even though the
    service is running and it is a domain member - that is exactly the state a
    reachability-based check would wrongly pass."""
    module = _load_network_script("imex_reboot_test.py")
    healthy = (
        "UPTIME=3600.0\nBOOTID=same\nACTIVE=active\nENABLED=enabled\n"
        'OUT={"nodes":{"0":{"status":"READY","host":"10.0.0.1","hostName":"n"}},"status":"UP"}\n'
    )

    def _remote(host: str, user: str, key_file: str, command: str, timeout: int) -> dict[str, Any]:
        """Fake a node whose uptime and boot id never change."""
        return {"host": host, "ok": True, "stdout": healthy}

    monkeypatch.setattr(module, "run_remote", _remote)
    monkeypatch.setattr(module, "POLL_SECONDS", 0)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "imex_reboot_test.py",
            "--region",
            "r",
            "--node-ids",
            "10.0.0.1",
            # Bounded: the script now keeps polling for restart evidence rather
            # than stopping at the first readable sample, so an unbounded run
            # against a node that never reboots would wait out the full default.
            "--key-file",
            "/tmp/k.pem",
            "--ssh-return-timeout",
            "1",
        ],
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = module.main()
    emitted: dict[str, Any] = json.loads(buf.getvalue())

    assert rc == 1
    assert emitted["reboot_confirmed"] is False
    assert "Reachability alone is not evidence" in emitted["error"]


def test_imex_reboot_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A confirmed reboot with IMEX returning unassisted passes, and the script
    never starts the service itself."""
    module = _load_network_script("imex_reboot_test.py")
    issued: list[str] = []
    calls = {"n": 0}
    member = '{"nodes":{"0":{"status":"READY","host":"10.0.0.1","hostName":"n"}},"status":"UP"}'

    def _remote(host: str, user: str, key_file: str, command: str, timeout: int) -> dict[str, Any]:
        """Fake a node that reboots and brings IMEX back on its own."""
        issued.append(command)
        if "reboot" in command:
            return {"host": host, "ok": False, "error": "connection closed"}
        calls["n"] += 1
        if calls["n"] == 1:  # pre-reboot
            return {
                "host": host,
                "ok": True,
                "stdout": f"UPTIME=3600.0\nBOOTID=old\nACTIVE=active\nENABLED=enabled\nOUT={member}\n",
            }
        return {
            "host": host,
            "ok": True,
            "stdout": f"UPTIME=94.0\nBOOTID=new\nACTIVE=active\nENABLED=enabled\nOUT={member}\n",
        }

    monkeypatch.setattr(module, "run_remote", _remote)
    monkeypatch.setattr(module, "POLL_SECONDS", 0)
    monkeypatch.setattr(
        sys,
        "argv",
        ["imex_reboot_test.py", "--region", "r", "--node-ids", "10.0.0.1", "--key-file", "/tmp/k.pem"],
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = module.main()
    emitted: dict[str, Any] = json.loads(buf.getvalue())

    assert rc == 0
    assert emitted["reboot_confirmed"] is True
    assert emitted["persistence_configured"] is True
    assert emitted["post_reboot"]["service_ready"] is True
    assert emitted["post_reboot"]["domain_member"] is True
    assert emitted["post_reboot"]["intervention_required"] is False
    assert not any("systemctl start" in c for c in issued), "the return must be unassisted"


def test_imex_reboot_reports_elapsed_when_imex_never_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    """A node that comes back without IMEX fails with elapsed time reported, and
    names the not-enabled-at-boot case when that is the cause."""
    module = _load_network_script("imex_reboot_test.py")
    calls = {"n": 0}

    member = '{"nodes":{"0":{"status":"READY","host":"10.0.0.1","hostName":"n"}},"status":"UP"}'

    def _remote(host: str, user: str, key_file: str, command: str, timeout: int) -> dict[str, Any]:
        """Fake a healthy member that reboots but never brings IMEX back."""
        if "reboot" in command:
            return {"host": host, "ok": False, "error": "connection closed"}
        calls["n"] += 1
        if calls["n"] == 1:  # pre-reboot: an active member, as the guard requires
            return {
                "host": host,
                "ok": True,
                "stdout": f"UPTIME=3600.0\nBOOTID=old\nACTIVE=active\nENABLED=disabled\nOUT={member}\n",
            }
        return {
            "host": host,
            "ok": True,
            "stdout": "UPTIME=94.0\nBOOTID=new\nACTIVE=inactive\nENABLED=disabled\nOUT=\n",
        }

    monkeypatch.setattr(module, "run_remote", _remote)
    monkeypatch.setattr(module, "POLL_SECONDS", 0)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "imex_reboot_test.py",
            "--region",
            "r",
            "--node-ids",
            "10.0.0.1",
            "--key-file",
            "/tmp/k.pem",
            "--rejoin-timeout",
            "1",
        ],
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = module.main()
    emitted: dict[str, Any] = json.loads(buf.getvalue())

    assert rc == 1
    assert emitted["reboot_confirmed"] is True
    assert emitted["persistence_configured"] is False
    assert emitted["post_reboot"]["service_ready"] is False
    assert emitted["post_reboot"]["elapsed_seconds"] >= 0
    assert "not enabled at boot" in emitted["error"]


@pytest.mark.parametrize(
    ("payload", "host", "expected"),
    [
        pytest.param('{"nodes":{"0":{"status":"READY","host":"10.0.0.1"}}}', "10.0.0.1", "member", id="ready"),
        pytest.param(
            '{"nodes":{"0":{"status":"UNAVAILABLE","host":"10.0.0.1"}}}', "10.0.0.1", "not_ready", id="not-ready"
        ),
        pytest.param(
            '{"nodes":{"0":{"status":"READY","host":"10.0.0.1"}}}',
            "203.0.113.5",
            "absent",
            id="identity-the-domain-does-not-know",
        ),
        pytest.param("not json", "10.0.0.1", "absent", id="unparseable"),
    ],
)
def test_imex_reboot_membership_separates_absent_from_not_ready(payload: str, host: str, expected: str) -> None:
    """A node missing from the payload is usually an identity mismatch, not a
    failed rejoin - nodes_config.cfg may list a private address while the check
    was pointed at a public one. Observed for real on AWS, where the node was
    READY under its private IP while the run named its public one."""
    module = _load_network_script("imex_reboot_test.py")

    assert module.membership(payload, host) == expected


def test_imex_reboot_identity_mismatch_reported_as_such(monkeypatch: pytest.MonkeyPatch) -> None:
    """When IMEX is running but the domain does not know the node by the given
    identity, say so rather than reporting a rejoin that never had a chance."""
    module = _load_network_script("imex_reboot_test.py")
    calls = {"n": 0}
    other = '{"nodes":{"0":{"status":"READY","host":"172.31.0.9","hostName":"priv"}},"status":"UP"}'

    def _remote(host: str, user: str, key_file: str, command: str, timeout: int) -> dict[str, Any]:
        """Fake a healthy node the domain knows by a different address."""
        if "reboot" in command:
            return {"host": host, "ok": False, "error": "connection closed"}
        calls["n"] += 1
        if calls["n"] == 1:  # pre-reboot: known by the queried identity
            known = '{"nodes":{"0":{"status":"READY","host":"203.0.113.5","hostName":"pub"}},"status":"UP"}'
            return {
                "host": host,
                "ok": True,
                "stdout": f"UPTIME=3600.0\nBOOTID=old\nACTIVE=active\nENABLED=enabled\nOUT={known}\n",
            }
        return {
            "host": host,
            "ok": True,
            "stdout": f"UPTIME=94.0\nBOOTID=new\nACTIVE=active\nENABLED=enabled\nOUT={other}\n",
        }

    monkeypatch.setattr(module, "run_remote", _remote)
    monkeypatch.setattr(module, "POLL_SECONDS", 0)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "imex_reboot_test.py",
            "--region",
            "r",
            "--node-ids",
            "203.0.113.5",
            "--key-file",
            "/tmp/k.pem",
            "--rejoin-timeout",
            "1",
        ],
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = module.main()
    emitted: dict[str, Any] = json.loads(buf.getvalue())

    assert rc == 1
    assert emitted["reboot_confirmed"] is True
    assert "does not know this node by that identity" in emitted["error"]
    assert "nodes_config.cfg" in emitted["error"]


def test_imex_reboot_refuses_multiple_nodes() -> None:
    """This reboots a node, so it must never silently pick one of several."""
    script = AWS_NETWORK_SCRIPTS / "imex_reboot_test.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--region", "r", "--node-ids", "n1,n2", "--key-file", "/tmp/k.pem"],
        capture_output=True,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 1
    assert "exactly one node" in json.loads(completed.stdout)["error"]


def test_imex_reboot_skips_when_not_configured() -> None:
    """An unconfigured run skips cleanly instead of failing unrelated network runs."""
    script = AWS_NETWORK_SCRIPTS / "imex_reboot_test.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--region", "r"],
        capture_output=True,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    payload: dict[str, Any] = json.loads(completed.stdout)
    assert payload["skipped"] is True
    assert "not configured" in payload["skip_reason"]


def test_imex_reboot_probe_does_not_truncate_the_parsed_payload() -> None:
    """The domain JSON is parsed, so it must not be cut. A large domain's payload
    truncated mid-structure is invalid JSON, which reads as "node absent" and
    reports a false identity mismatch."""
    module = _load_network_script("imex_reboot_test.py")

    probe = module._state_command("nvidia-imex.service")

    assert "nvidia-imex-ctl" in probe
    assert "head -c" not in probe


def test_imex_reboot_not_enabled_at_boot_fails_despite_healthy_return(monkeypatch: pytest.MonkeyPatch) -> None:
    """Boot persistence is part of the pass condition, not just an explanation
    for a service that failed to return: a node running IMEX while not enabled
    at boot may only be up because something else started it."""
    module = _load_network_script("imex_reboot_test.py")
    calls = {"n": 0}
    member = '{"nodes":{"0":{"status":"READY","host":"10.0.0.1","hostName":"n"}},"status":"UP"}'

    def _remote(host: str, user: str, key_file: str, command: str, timeout: int) -> dict[str, Any]:
        """Fake a healthy return on a node that is not enabled at boot."""
        if "reboot" in command:
            return {"host": host, "ok": False, "error": "connection closed"}
        calls["n"] += 1
        uptime = "3600.0" if calls["n"] == 1 else "94.0"
        boot = "old" if calls["n"] == 1 else "new"
        return {
            "host": host,
            "ok": True,
            "stdout": f"UPTIME={uptime}\nBOOTID={boot}\nACTIVE=active\nENABLED=disabled\nOUT={member}\n",
        }

    monkeypatch.setattr(module, "run_remote", _remote)
    monkeypatch.setattr(module, "POLL_SECONDS", 0)
    monkeypatch.setattr(
        sys,
        "argv",
        ["imex_reboot_test.py", "--region", "r", "--node-ids", "10.0.0.1", "--key-file", "/tmp/k.pem"],
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = module.main()
    emitted: dict[str, Any] = json.loads(buf.getvalue())

    assert rc == 1
    assert emitted["persistence_configured"] is False
    assert emitted["post_reboot"]["service_ready"] is True
    assert emitted["post_reboot"]["domain_member"] is True
    assert emitted["success"] is False
    assert "not enabled at boot" in emitted["error"]


def test_imex_reboot_elapsed_excludes_downtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """elapsed_seconds is boot-to-domain-membership, so the reboot downtime must
    not inflate it or eat into the rejoin bound."""
    module = _load_network_script("imex_reboot_test.py")
    clock = {"t": 0.0}
    calls = {"n": 0}
    member = '{"nodes":{"0":{"status":"READY","host":"10.0.0.1","hostName":"n"}},"status":"UP"}'

    def _remote(host: str, user: str, key_file: str, command: str, timeout: int) -> dict[str, Any]:
        """Fake 300s of downtime before the node answers again."""
        if "reboot" in command:
            clock["t"] += 300.0
            return {"host": host, "ok": False, "error": "connection closed"}
        calls["n"] += 1
        uptime = "3600.0" if calls["n"] == 1 else "94.0"
        boot = "old" if calls["n"] == 1 else "new"
        return {
            "host": host,
            "ok": True,
            "stdout": f"UPTIME={uptime}\nBOOTID={boot}\nACTIVE=active\nENABLED=enabled\nOUT={member}\n",
        }

    monkeypatch.setattr(module, "run_remote", _remote)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["imex_reboot_test.py", "--region", "r", "--node-ids", "10.0.0.1", "--key-file", "/tmp/k.pem"],
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = module.main()
    emitted: dict[str, Any] = json.loads(buf.getvalue())

    assert rc == 0
    # Would be >=300 if the timer had started before the reboot request.
    assert emitted["post_reboot"]["elapsed_seconds"] < 300


@pytest.mark.parametrize("node_ids", ["", "n1,n2"])
def test_imex_reboot_template_requires_exactly_one_node(node_ids: str) -> None:
    """The my-isv template must not report success for an empty selection, nor
    silently reboot the first of several."""
    script = MY_ISV_NETWORK_SCRIPTS / "imex_reboot_test.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--region", "r", "--node-ids", node_ids],
        capture_output=True,
        env={"PATH": os.environ.get("PATH", ""), "ISVCTL_DEMO_MODE": "1"},
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 1
    assert "exactly one node" in json.loads(completed.stdout)["error"]


@pytest.mark.parametrize(
    ("active", "payload", "why"),
    [
        pytest.param("inactive", '{"nodes":{"0":{"status":"READY","host":"10.0.0.1"}}}', "service down", id="inactive"),
        pytest.param(
            "active", '{"nodes":{"0":{"status":"UNAVAILABLE","host":"10.0.0.1"}}}', "not ready", id="not-ready"
        ),
        pytest.param("active", '{"nodes":{}}', "not in the domain", id="absent"),
    ],
)
def test_imex_reboot_refuses_a_target_that_was_not_a_member(
    monkeypatch: pytest.MonkeyPatch, active: str, payload: str, why: str
) -> None:
    """SDN20-01 reboots a *member* and watches it rejoin. A node that was not one
    beforehand would be joining rather than rejoining, and could report success
    for a property never demonstrated - so refuse before doing anything
    destructive."""
    module = _load_network_script("imex_reboot_test.py")
    issued: list[str] = []

    def _remote(host: str, user: str, key_file: str, command: str, timeout: int) -> dict[str, Any]:
        """Fake a node that is not an operational member before the reboot."""
        issued.append(command)
        return {
            "host": host,
            "ok": True,
            "stdout": f"UPTIME=3600.0\nBOOTID=old\nACTIVE={active}\nENABLED=enabled\nOUT={payload}\n",
        }

    monkeypatch.setattr(module, "run_remote", _remote)
    monkeypatch.setattr(module, "POLL_SECONDS", 0)
    monkeypatch.setattr(
        sys,
        "argv",
        ["imex_reboot_test.py", "--region", "r", "--node-ids", "10.0.0.1", "--key-file", "/tmp/k.pem"],
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = module.main()
    emitted: dict[str, Any] = json.loads(buf.getvalue())

    assert rc == 1, why
    assert not any("reboot" in c for c in issued), "must not reboot a node that was not a member"
    assert "not an active domain member before the reboot" in emitted["error"]
    assert emitted["prior_state"]["domain_member"] is False or emitted["prior_state"]["service_state"] != "active"


def test_imex_reboot_waits_for_restart_evidence_not_first_readable_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`systemctl reboot` returns once the reboot is enqueued, so the node can
    still answer for a while afterwards - on bare metal, longer than a poll
    interval. Stopping at the first readable sample would capture pre-reboot
    uptime and report "not confirmed", which looks exactly like the defect this
    check exists to catch.
    """
    module = _load_network_script("imex_reboot_test.py")
    calls = {"n": 0}
    member = '{"nodes":{"0":{"status":"READY","host":"10.0.0.1","hostName":"n"}},"status":"UP"}'

    def _remote(host: str, user: str, key_file: str, command: str, timeout: int) -> dict[str, Any]:
        """Fake a node that keeps answering with pre-reboot values, then restarts."""
        if "reboot" in command:
            return {"host": host, "ok": True, "stdout": ""}
        calls["n"] += 1
        if calls["n"] <= 3:  # pre-reboot guard, then two stale post-enqueue samples
            return {
                "host": host,
                "ok": True,
                "stdout": f"UPTIME=3600.0\nBOOTID=old\nACTIVE=active\nENABLED=enabled\nOUT={member}\n",
            }
        return {
            "host": host,
            "ok": True,
            "stdout": f"UPTIME=94.0\nBOOTID=new\nACTIVE=active\nENABLED=enabled\nOUT={member}\n",
        }

    monkeypatch.setattr(module, "run_remote", _remote)
    monkeypatch.setattr(module, "POLL_SECONDS", 0)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "imex_reboot_test.py",
            "--region",
            "r",
            "--node-ids",
            "10.0.0.1",
            "--key-file",
            "/tmp/k.pem",
            "--ssh-return-timeout",
            "60",
        ],
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = module.main()
    emitted: dict[str, Any] = json.loads(buf.getvalue())

    # Would be a spurious "not confirmed" failure if the loop stopped early.
    assert rc == 0
    assert emitted["reboot_confirmed"] is True
    assert emitted["uptime_seconds"] == 94.0


def test_imex_reboot_uses_the_confirming_sample_as_first_rejoin_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sample that confirmed the reboot came from the same probe and already
    carries service state and membership. Discarding it would let a node that
    had already recovered report a false rejoin failure - and wait out the whole
    timeout doing it - if every later probe failed.
    """
    module = _load_network_script("imex_reboot_test.py")
    calls = {"n": 0}
    member = '{"nodes":{"0":{"status":"READY","host":"10.0.0.1","hostName":"n"}},"status":"UP"}'

    def _remote(host: str, user: str, key_file: str, command: str, timeout: int) -> dict[str, Any]:
        """Fake a node fully recovered at confirmation time, then unreachable."""
        if "reboot" in command:
            return {"host": host, "ok": True, "stdout": ""}
        calls["n"] += 1
        if calls["n"] == 1:  # pre-reboot member
            return {
                "host": host,
                "ok": True,
                "stdout": f"UPTIME=3600.0\nBOOTID=old\nACTIVE=active\nENABLED=enabled\nOUT={member}\n",
            }
        if calls["n"] == 2:  # confirms the reboot AND is already fully back
            return {
                "host": host,
                "ok": True,
                "stdout": f"UPTIME=94.0\nBOOTID=new\nACTIVE=active\nENABLED=enabled\nOUT={member}\n",
            }
        return {"host": host, "ok": False, "error": "unreachable"}

    monkeypatch.setattr(module, "run_remote", _remote)
    monkeypatch.setattr(module, "POLL_SECONDS", 0)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "imex_reboot_test.py",
            "--region",
            "r",
            "--node-ids",
            "10.0.0.1",
            "--key-file",
            "/tmp/k.pem",
            "--ssh-return-timeout",
            "60",
            "--rejoin-timeout",
            "60",
        ],
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = module.main()
    emitted: dict[str, Any] = json.loads(buf.getvalue())

    assert rc == 0
    assert emitted["post_reboot"]["service_ready"] is True
    assert emitted["post_reboot"]["domain_member"] is True


def test_imex_reboot_not_enabled_message_states_the_cause_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The boot-persistence suffix explains a failure the detail has not already
    attributed, so it must not repeat a detail that is itself about boot
    persistence - which previously read '... not enabled at boot after 61.0s (it
    was not enabled at boot)'."""
    module = _load_network_script("imex_reboot_test.py")
    calls = {"n": 0}
    member = '{"nodes":{"0":{"status":"READY","host":"10.0.0.1","hostName":"n"}},"status":"UP"}'

    def _remote(host: str, user: str, key_file: str, command: str, timeout: int) -> dict[str, Any]:
        """Fake a healthy return on a node that is not enabled at boot."""
        if "reboot" in command:
            return {"host": host, "ok": True, "stdout": ""}
        calls["n"] += 1
        uptime = "3600.0" if calls["n"] == 1 else "94.0"
        boot = "old" if calls["n"] == 1 else "new"
        return {
            "host": host,
            "ok": True,
            "stdout": f"UPTIME={uptime}\nBOOTID={boot}\nACTIVE=active\nENABLED=disabled\nOUT={member}\n",
        }

    monkeypatch.setattr(module, "run_remote", _remote)
    monkeypatch.setattr(module, "POLL_SECONDS", 0)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "imex_reboot_test.py",
            "--region",
            "r",
            "--node-ids",
            "10.0.0.1",
            "--key-file",
            "/tmp/k.pem",
            "--ssh-return-timeout",
            "60",
        ],
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = module.main()
    emitted: dict[str, Any] = json.loads(buf.getvalue())

    assert rc == 1
    assert emitted["error"].count("enabled at boot") == 1, emitted["error"]
