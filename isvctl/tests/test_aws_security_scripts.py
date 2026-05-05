# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Tests for AWS security reference scripts."""

from __future__ import annotations

import importlib.util
import io
import json
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.error import HTTPError

import pytest
from botocore.exceptions import ClientError, WaiterError

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
AWS_SECURITY_SCRIPTS = ISVCTL_ROOT / "configs" / "providers" / "aws" / "scripts" / "security"


def _load_security_script(script_name: str) -> ModuleType:
    """Load an AWS security script as a module for direct helper testing."""
    script_path = AWS_SECURITY_SCRIPTS / script_name
    spec = importlib.util.spec_from_file_location(f"test_{script_path.stem}", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _client_error(operation_name: str, code: str = "AccessDenied", message: str = "denied") -> ClientError:
    """Create a botocore ClientError for fake AWS client failures."""
    return ClientError({"Error": {"Code": code, "Message": message}}, operation_name)


class FakeEksClient:
    """Small fake for the EKS client calls used by api_endpoint_test."""

    def __init__(self, cluster_configs: dict[str, dict[str, Any]]) -> None:
        """Store fake cluster configs keyed by cluster name."""
        self.cluster_configs = cluster_configs

    def list_clusters(self) -> dict[str, list[str]]:
        """Return fake cluster names."""
        return {"clusters": list(self.cluster_configs)}

    def describe_cluster(self, name: str) -> dict[str, dict[str, dict[str, Any]]]:
        """Return fake cluster config for a cluster name."""
        return {"cluster": {"resourcesVpcConfig": self.cluster_configs[name]}}


def _patch_eks_client(monkeypatch: pytest.MonkeyPatch, module: ModuleType, eks: FakeEksClient) -> None:
    """Patch boto3.client to return a fake EKS client."""

    def fake_client(service_name: str, region_name: str | None = None) -> FakeEksClient:
        """Return the fake EKS client for EKS requests."""
        assert service_name == "eks"
        assert region_name == "us-west-2"
        return eks

    monkeypatch.setattr(module.boto3, "client", fake_client)


def test_eks_private_check_fails_public_private_cluster_with_world_open_cidr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dual endpoint EKS clusters still fail when public access is world-open."""
    module = _load_security_script("api_endpoint_test.py")
    eks = FakeEksClient(
        {
            "wide-open": {
                "endpointPublicAccess": True,
                "endpointPrivateAccess": True,
                "publicAccessCidrs": ["0.0.0.0/0"],
            }
        }
    )
    _patch_eks_client(monkeypatch, module, eks)

    result = module._check_eks_private("us-west-2")

    assert result["passed"] is False
    assert "open to the internet" in result["error"]


def test_eks_private_check_accepts_dual_endpoint_with_restricted_public_cidr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dual endpoint EKS clusters pass when public CIDRs are restricted."""
    module = _load_security_script("api_endpoint_test.py")
    eks = FakeEksClient(
        {
            "restricted": {
                "endpointPublicAccess": True,
                "endpointPrivateAccess": True,
                "publicAccessCidrs": ["203.0.113.0/24"],
            }
        }
    )
    _patch_eks_client(monkeypatch, module, eks)

    result = module._check_eks_private("us-west-2")

    assert result["passed"] is True


def test_eks_private_check_fails_public_only_cluster_even_with_restricted_cidr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public-only EKS clusters fail even if public CIDRs are restricted."""
    module = _load_security_script("api_endpoint_test.py")
    eks = FakeEksClient(
        {
            "public-only": {
                "endpointPublicAccess": True,
                "endpointPrivateAccess": False,
                "publicAccessCidrs": ["203.0.113.0/24"],
            }
        }
    )
    _patch_eks_client(monkeypatch, module, eks)

    result = module._check_eks_private("us-west-2")

    assert result["passed"] is False
    assert "public-only" in result["error"]


class FakeEc2Paginator:
    """Fake EC2 paginator returning configured pages."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        """Store pages to return from paginate."""
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Return all configured pages and record the pagination filters."""
        self.calls.append(kwargs)
        return self.pages


class FakeBmcEc2:
    """Fake EC2 client for BMC isolation checks."""

    def __init__(self) -> None:
        """Initialize paginated EC2 responses."""
        self.paginators = {
            "describe_route_tables": FakeEc2Paginator(
                [
                    {"RouteTables": [{"RouteTableId": "rtb-first", "Routes": []}]},
                    {
                        "RouteTables": [
                            {
                                "RouteTableId": "rtb-second",
                                "Routes": [{"DestinationCidrBlock": "169.254.0.0/16"}],
                            }
                        ]
                    },
                ]
            ),
            "describe_security_groups": FakeEc2Paginator(
                [
                    {"SecurityGroups": [{"GroupId": "sg-first", "IpPermissionsEgress": []}]},
                    {
                        "SecurityGroups": [
                            {
                                "GroupId": "sg-second",
                                "IpPermissionsEgress": [{"IpRanges": [{"CidrIp": "198.18.0.0/15"}]}],
                            }
                        ]
                    },
                ]
            ),
            "describe_vpcs": FakeEc2Paginator([{"Vpcs": [{"VpcId": "vpc-nondefault"}]}]),
        }

    def get_paginator(self, operation_name: str) -> FakeEc2Paginator:
        """Return a fake paginator for the requested EC2 operation."""
        return self.paginators[operation_name]

    def describe_route_tables(self, **kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        """Return only the first route table page to expose missing pagination."""
        return self.paginators["describe_route_tables"].pages[0]

    def describe_security_groups(self, **kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        """Return only the first security group page to expose missing pagination."""
        return self.paginators["describe_security_groups"].pages[0]

    def describe_vpcs(self, **kwargs: Any) -> dict[str, list[dict[str, str]]]:
        """Return no default VPC for the legacy lookup path."""
        assert kwargs == {"Filters": [{"Name": "is-default", "Values": ["true"]}]}
        return {"Vpcs": []}


class FakeBmcManagementEc2:
    """Fake EC2 client for BMC management-network checks."""

    def __init__(
        self,
        *,
        vpcs: list[dict[str, Any]] | None = None,
        route_tables: list[dict[str, Any]] | None = None,
        network_acls: list[dict[str, Any]] | None = None,
    ) -> None:
        """Initialize paginated EC2 responses."""
        self.paginators = {
            "describe_vpcs": FakeEc2Paginator(
                [
                    {
                        "Vpcs": vpcs
                        if vpcs is not None
                        else [
                            {
                                "VpcId": "vpc-tenant",
                                "CidrBlock": "10.0.0.0/16",
                                "CidrBlockAssociationSet": [{"CidrBlock": "10.0.0.0/16"}],
                            }
                        ]
                    }
                ]
            ),
            "describe_route_tables": FakeEc2Paginator([{"RouteTables": route_tables or []}]),
            "describe_network_acls": FakeEc2Paginator([{"NetworkAcls": network_acls or []}]),
        }

    def get_paginator(self, operation_name: str) -> FakeEc2Paginator:
        """Return a fake paginator for the requested EC2 operation."""
        return self.paginators[operation_name]


def test_bmc_checks_scan_paginated_route_tables_and_security_groups() -> None:
    """BMC route and SG checks inspect every EC2 paginator page."""
    module = _load_security_script("bmc_isolation_test.py")
    ec2 = FakeBmcEc2()

    route_result = module._check_route_tables(ec2, "vpc-nondefault")
    sg_result = module._check_sg_no_bmc_egress(ec2, "vpc-nondefault")

    assert route_result["passed"] is False
    assert "rtb-second" in route_result["error"]
    assert sg_result["passed"] is False
    assert "sg-second" in sg_result["error"]
    assert ec2.paginators["describe_route_tables"].calls == [
        {"Filters": [{"Name": "vpc-id", "Values": ["vpc-nondefault"]}]}
    ]
    assert ec2.paginators["describe_security_groups"].calls == [
        {"Filters": [{"Name": "vpc-id", "Values": ["vpc-nondefault"]}]}
    ]


def test_bmc_main_scans_non_default_vpcs_when_no_vpc_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """BMC validation checks non-default VPCs instead of auto-passing without a default VPC."""
    module = _load_security_script("bmc_isolation_test.py")
    ec2 = FakeBmcEc2()

    def fake_client(service_name: str, **kwargs: Any) -> FakeBmcEc2:
        """Return the fake EC2 client."""
        assert service_name == "ec2"
        return ec2

    monkeypatch.setattr(module.boto3, "client", fake_client)
    monkeypatch.setattr(module.sys, "argv", ["bmc_isolation_test.py", "--region", "us-west-2"])

    exit_code = module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["success"] is False
    assert payload["tests"]["probe_bmc_from_tenant"]["passed"] is False
    assert "vpc-nondefault" in payload["tests"]["probe_bmc_from_tenant"]["error"]
    assert ec2.paginators["describe_vpcs"].calls == [{}]


def test_bmc_management_network_detects_tenant_cidr_overlap() -> None:
    """SEC12-01 fails when tenant VPC CIDRs overlap reserved management ranges."""
    module = _load_security_script("bmc_management_network_test.py")
    vpcs = [
        {
            "VpcId": "vpc-mgmt-overlap",
            "CidrBlock": "198.18.1.0/24",
            "CidrBlockAssociationSet": [{"CidrBlock": "198.18.1.0/24"}],
        }
    ]

    result = module._check_dedicated_management_network(vpcs)

    assert result["passed"] is False
    assert "198.18.1.0/24" in result["error"]


def test_bmc_management_network_detects_management_tag() -> None:
    """SEC12-01 fails when a tenant VPC is tagged as a BMC management network."""
    module = _load_security_script("bmc_management_network_test.py")
    vpcs = [
        {
            "VpcId": "vpc-mgmt-tag",
            "CidrBlock": "10.0.0.0/16",
            "CidrBlockAssociationSet": [{"CidrBlock": "10.0.0.0/16"}],
            "Tags": [{"Key": "Role", "Value": "bmc-network"}],
        }
    ]

    result = module._check_tenant_network_not_management(vpcs)

    assert result["passed"] is False
    assert "vpc-mgmt-tag" in result["error"]


def test_bmc_management_tag_matches_underscore_delimited() -> None:
    """Tag matcher catches underscore-delimited management names like bmc_network."""
    module = _load_security_script("bmc_management_network_test.py")
    vpcs = [
        {
            "VpcId": "vpc-underscore",
            "CidrBlock": "10.0.0.0/16",
            "CidrBlockAssociationSet": [{"CidrBlock": "10.0.0.0/16"}],
            "Tags": [{"Key": "Name", "Value": "bmc_network"}],
        }
    ]

    result = module._check_tenant_network_not_management(vpcs)

    assert result["passed"] is False
    assert "vpc-underscore" in result["error"]


def test_bmc_management_tag_avoids_substring_false_positive() -> None:
    """Tag matcher rejects unrelated identifiers that contain management substrings."""
    module = _load_security_script("bmc_management_network_test.py")
    vpcs = [
        {
            "VpcId": "vpc-tenant",
            "CidrBlock": "10.0.0.0/16",
            "CidrBlockAssociationSet": [{"CidrBlock": "10.0.0.0/16"}],
            "Tags": [{"Key": "Name", "Value": "submarine-bmcollege"}],
        }
    ]

    result = module._check_tenant_network_not_management(vpcs)

    assert result["passed"] is True


def test_bmc_management_network_detects_explicit_management_routes() -> None:
    """SEC12-01 fails when a tenant route table targets part of a management CIDR."""
    module = _load_security_script("bmc_management_network_test.py")
    ec2 = FakeBmcManagementEc2(
        route_tables=[
            {
                "RouteTableId": "rtb-mgmt",
                "Routes": [{"DestinationCidrBlock": "198.18.1.0/24"}],
            }
        ]
    )

    result = module._check_restricted_management_routes(ec2, ["vpc-tenant"])

    assert result["passed"] is False
    assert "rtb-mgmt" in result["error"]


def test_bmc_management_network_detects_management_acl_host_route() -> None:
    """SEC12-01 fails when a NACL explicitly allows a host inside a management range."""
    module = _load_security_script("bmc_management_network_test.py")
    ec2 = FakeBmcManagementEc2(
        network_acls=[
            {
                "NetworkAclId": "acl-mgmt",
                "Entries": [
                    {
                        "RuleAction": "allow",
                        "CidrBlock": "169.254.169.254/32",
                    }
                ],
            }
        ]
    )

    result = module._check_management_acl_enforced(ec2, ["vpc-tenant"])

    assert result["passed"] is False
    assert "acl-mgmt" in result["error"]


def test_bmc_management_network_exempts_default_routes() -> None:
    """SEC12-01 route checks do not treat default internet routes as management routes."""
    module = _load_security_script("bmc_management_network_test.py")
    ec2 = FakeBmcManagementEc2(
        route_tables=[
            {
                "RouteTableId": "rtb-default",
                "Routes": [{"DestinationCidrBlock": "0.0.0.0/0"}],
            }
        ]
    )

    result = module._check_restricted_management_routes(ec2, ["vpc-tenant"])

    assert result["passed"] is True


def test_bmc_management_network_main_emits_sec12_01_contract(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Successful AWS reference run emits the SEC12-01 validation contract."""
    module = _load_security_script("bmc_management_network_test.py")
    ec2 = FakeBmcManagementEc2()

    def fake_client(service_name: str, **kwargs: Any) -> FakeBmcManagementEc2:
        """Return the fake EC2 client."""
        assert service_name == "ec2"
        return ec2

    monkeypatch.setattr(module.boto3, "client", fake_client)
    monkeypatch.setattr(module.sys, "argv", ["bmc_management_network_test.py", "--region", "us-west-2"])

    exit_code = module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["success"] is True
    assert payload["test_name"] == "bmc_management_network"
    assert set(payload["tests"]) == {
        "dedicated_management_network",
        "restricted_management_routes",
        "tenant_network_not_management",
        "management_acl_enforced",
    }


def test_bmc_protocol_security_reports_no_customer_bmc_surface() -> None:
    """AWS BMC protocol check emits all CNP10-01 keys for the no-surface case."""
    module = _load_security_script("bmc_protocol_security_test.py")

    result = module._aws_no_customer_bmc_result("us-west-2")

    assert result["success"] is True
    assert result["bmc_endpoints_tested"] == 0
    assert result["bmc_protocol_surface"] == "none"
    assert set(result["tests"]) == {
        "ipmi_disabled",
        "redfish_tls_enabled",
        "redfish_plain_http_disabled",
        "redfish_authentication_required",
        "redfish_authorization_enforced",
        "redfish_accounting_enabled",
    }
    assert all(test["passed"] is True for test in result["tests"].values())
    assert "do not receive customer-accessible IPMI or Redfish" in result["evidence"]


class FakeStsClient:
    """Small fake for STS GetCallerIdentity."""

    def __init__(self, error: Exception | None = None) -> None:
        """Store an optional error returned by get_caller_identity."""
        self.error = error

    def get_caller_identity(self) -> dict[str, str]:
        """Return a fake caller identity or raise the configured error."""
        if self.error:
            raise self.error
        return {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/test", "UserId": "test"}


def _patch_sts_client(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    sts: FakeStsClient,
    *,
    expected_region: str,
) -> None:
    """Patch boto3.client to return a fake STS client."""

    def fake_client(service_name: str, region_name: str | None = None) -> FakeStsClient:
        """Return the fake STS client for STS requests."""
        assert service_name == "sts"
        assert region_name == expected_region
        return sts

    monkeypatch.setattr(module.boto3, "client", fake_client)


def test_bmc_protocol_security_main_outputs_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AWS BMC protocol script prints the provider-agnostic JSON contract."""
    module = _load_security_script("bmc_protocol_security_test.py")
    monkeypatch.setattr(module.sys, "argv", ["bmc_protocol_security_test.py", "--region", "eu-west-1"])
    _patch_sts_client(monkeypatch, module, FakeStsClient(), expected_region="eu-west-1")

    exit_code = module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["success"] is True
    assert payload["region"] == "eu-west-1"
    assert payload["test_name"] == "bmc_protocol_security"
    assert payload["tests"]["ipmi_disabled"]["passed"] is True


def test_bmc_protocol_security_main_reports_sts_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AWS BMC protocol script fails closed when the STS probe fails."""
    module = _load_security_script("bmc_protocol_security_test.py")
    monkeypatch.setattr(module.sys, "argv", ["bmc_protocol_security_test.py", "--region", "eu-west-1"])
    _patch_sts_client(monkeypatch, module, FakeStsClient(RuntimeError("sts unavailable")), expected_region="eu-west-1")

    exit_code = module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["success"] is False
    assert payload["platform"] == "security"
    assert payload["test_name"] == "bmc_protocol_security"
    assert payload["region"] == "eu-west-1"
    assert "sts unavailable" in payload["evidence"]
    assert all(test["passed"] is False for test in payload["tests"].values())


class FakeBastionEc2:
    """Fake EC2 client for BMC bastion-access checks."""

    def __init__(
        self,
        *,
        security_groups: list[dict[str, Any]] | None = None,
        subnets: list[dict[str, Any]] | None = None,
        route_tables: list[dict[str, Any]] | None = None,
    ) -> None:
        """Initialize paginated EC2 responses."""
        self.paginators = {
            "describe_security_groups": FakeEc2Paginator(
                [{"SecurityGroups": security_groups if security_groups is not None else []}]
            ),
            "describe_subnets": FakeEc2Paginator([{"Subnets": subnets if subnets is not None else []}]),
            "describe_route_tables": FakeEc2Paginator([{"RouteTables": route_tables or []}]),
        }

    def get_paginator(self, operation_name: str) -> FakeEc2Paginator:
        """Return a fake paginator for the requested EC2 operation."""
        return self.paginators[operation_name]


class FakeBastionRouteTablePaginator:
    """Fake route-table paginator that varies responses by route-table association filter."""

    def __init__(
        self,
        *,
        explicit_route_tables: list[dict[str, Any]] | None = None,
        main_route_tables: list[dict[str, Any]] | None = None,
    ) -> None:
        """Store route tables returned for explicit subnet and main associations."""
        self.explicit_route_tables = explicit_route_tables or []
        self.main_route_tables = main_route_tables or []
        self.calls: list[dict[str, Any]] = []

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Return route tables based on the requested association filter."""
        self.calls.append(kwargs)
        filter_names = {filter_config["Name"] for filter_config in kwargs.get("Filters", [])}
        if "association.main" in filter_names:
            return [{"RouteTables": self.main_route_tables}]
        if "association.subnet-id" in filter_names:
            return [{"RouteTables": self.explicit_route_tables}]
        return [{"RouteTables": []}]


class FakeMainRouteBastionEc2:
    """Fake EC2 client for BMC bastion route-table association checks."""

    def __init__(self, paginator: FakeBastionRouteTablePaginator) -> None:
        """Store the route-table paginator."""
        self.route_table_paginator = paginator

    def get_paginator(self, operation_name: str) -> FakeBastionRouteTablePaginator:
        """Return the fake route-table paginator."""
        assert operation_name == "describe_route_tables"
        return self.route_table_paginator


def test_bmc_bastion_access_provider_hidden_when_no_management_resources(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SEC12-03 passes with provider_hidden markers when no BMC resources are tagged."""
    module = _load_security_script("bmc_bastion_access_test.py")
    ec2 = FakeBastionEc2()

    def fake_client(service_name: str, **_: Any) -> FakeBastionEc2:
        """Return the fake EC2 client."""
        assert service_name == "ec2"
        return ec2

    monkeypatch.setattr(module.boto3, "client", fake_client)
    monkeypatch.setattr(module.sys, "argv", ["bmc_bastion_access_test.py", "--region", "us-west-2"])

    exit_code = module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["success"] is True
    assert payload["test_name"] == "bmc_bastion_access"
    for subtest in (
        "bastion_identifiable",
        "management_ingress_via_bastion_only",
        "no_direct_public_route",
        "bastion_hardened",
    ):
        assert payload["tests"][subtest]["passed"] is True
        assert payload["tests"][subtest]["provider_hidden"] is True


def test_bmc_bastion_access_fails_when_bmc_tagged_but_no_bastion(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When a BMC-tagged resource exists but no bastion is tagged, bastion_identifiable fails."""
    module = _load_security_script("bmc_bastion_access_test.py")
    ec2 = FakeBastionEc2(
        security_groups=[
            {
                "GroupId": "sg-bmc",
                "Tags": [{"Key": "Role", "Value": "bmc-network"}],
                "IpPermissions": [],
            },
        ],
    )

    def fake_client(service_name: str, **_: Any) -> FakeBastionEc2:
        """Return the fake EC2 client."""
        assert service_name == "ec2"
        return ec2

    monkeypatch.setattr(module.boto3, "client", fake_client)
    monkeypatch.setattr(module.sys, "argv", ["bmc_bastion_access_test.py", "--region", "us-west-2"])

    exit_code = module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["success"] is False
    assert payload["tests"]["bastion_identifiable"]["passed"] is False
    assert payload["tests"]["bastion_hardened"]["passed"] is False


def test_bmc_bastion_access_detects_world_open_management_ingress() -> None:
    """SEC12-03 fails when a BMC-tagged SG accepts ingress from 0.0.0.0/0 on a management port."""
    module = _load_security_script("bmc_bastion_access_test.py")
    management_sgs = [
        {
            "GroupId": "sg-bmc",
            "Tags": [{"Key": "Role", "Value": "bmc-network"}],
            "IpPermissions": [
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                }
            ],
        }
    ]
    bastion_sgs = [
        {
            "GroupId": "sg-bastion",
            "Tags": [{"Key": "Role", "Value": "bastion"}],
            "IpPermissions": [],
        }
    ]
    bastion_ids = {sg["GroupId"] for sg in bastion_sgs}

    result = module._check_management_ingress_via_bastion_only(management_sgs, bastion_ids)

    assert result["passed"] is False
    assert "sg-bmc" in result["error"]
    assert "public CIDR" in result["error"]


def test_bmc_bastion_access_accepts_bastion_sg_referenced_ingress() -> None:
    """Ingress from the bastion SG (UserIdGroupPairs) is acceptable."""
    module = _load_security_script("bmc_bastion_access_test.py")
    management_sgs = [
        {
            "GroupId": "sg-bmc",
            "Tags": [{"Key": "Role", "Value": "bmc-network"}],
            "IpPermissions": [
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "UserIdGroupPairs": [{"GroupId": "sg-bastion"}],
                }
            ],
        }
    ]
    bastion_ids = {"sg-bastion"}

    result = module._check_management_ingress_via_bastion_only(management_sgs, bastion_ids)

    assert result["passed"] is True


def test_bmc_bastion_access_rejects_explicit_cidr_ingress() -> None:
    """Even a non-public CIDR on a management SG fails; ingress must come via bastion SG ref."""
    module = _load_security_script("bmc_bastion_access_test.py")
    management_sgs = [
        {
            "GroupId": "sg-bmc",
            "Tags": [{"Key": "Role", "Value": "bmc-network"}],
            "IpPermissions": [
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": "10.99.0.0/24"}],
                }
            ],
        }
    ]

    result = module._check_management_ingress_via_bastion_only(management_sgs, set())

    assert result["passed"] is False
    assert "explicit_cidr=True" in result["error"]


def test_bmc_bastion_access_rejects_prefix_list_ingress() -> None:
    """Prefix-list ingress is not bastion SG ingress and must fail SEC12-03."""
    module = _load_security_script("bmc_bastion_access_test.py")
    management_sgs = [
        {
            "GroupId": "sg-bmc",
            "Tags": [{"Key": "Role", "Value": "bmc-network"}],
            "IpPermissions": [
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "PrefixListIds": [{"PrefixListId": "pl-corporate"}],
                }
            ],
        }
    ]

    result = module._check_management_ingress_via_bastion_only(management_sgs, {"sg-bastion"})

    assert result["passed"] is False
    assert "prefix_list=True" in result["error"]


def test_bmc_bastion_access_detects_igw_route_from_management_subnet() -> None:
    """SEC12-03 fails when a BMC-tagged subnet has a 0.0.0.0/0 -> igw route."""
    module = _load_security_script("bmc_bastion_access_test.py")
    management_subnets = [
        {
            "SubnetId": "subnet-bmc",
            "MapPublicIpOnLaunch": False,
            "Tags": [{"Key": "Role", "Value": "bmc-management"}],
        }
    ]
    ec2 = FakeBastionEc2(
        route_tables=[
            {
                "RouteTableId": "rtb-bmc",
                "Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-12345"}],
            }
        ]
    )

    result = module._check_no_direct_public_route(ec2, management_subnets)

    assert result["passed"] is False
    assert "rtb-bmc" in result["error"]


def test_bmc_bastion_access_detects_igw_route_from_main_route_table() -> None:
    """SEC12-03 checks the VPC main route table when a BMC subnet has no explicit association."""
    module = _load_security_script("bmc_bastion_access_test.py")
    management_subnets = [
        {
            "SubnetId": "subnet-bmc",
            "VpcId": "vpc-bmc",
            "MapPublicIpOnLaunch": False,
            "Tags": [{"Key": "Role", "Value": "bmc-management"}],
        }
    ]
    paginator = FakeBastionRouteTablePaginator(
        main_route_tables=[
            {
                "RouteTableId": "rtb-main-public",
                "Associations": [{"Main": True, "RouteTableAssociationId": "rtbassoc-main"}],
                "Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-12345"}],
            }
        ]
    )
    ec2 = FakeMainRouteBastionEc2(paginator)

    result = module._check_no_direct_public_route(ec2, management_subnets)

    assert result["passed"] is False
    assert "rtb-main-public" in result["error"]
    assert paginator.calls == [
        {"Filters": [{"Name": "association.subnet-id", "Values": ["subnet-bmc"]}]},
        {
            "Filters": [
                {"Name": "vpc-id", "Values": ["vpc-bmc"]},
                {"Name": "association.main", "Values": ["true"]},
            ]
        },
    ]


def test_bmc_bastion_access_detects_map_public_ip() -> None:
    """SEC12-03 fails when a BMC-tagged subnet auto-assigns public IPs."""
    module = _load_security_script("bmc_bastion_access_test.py")
    management_subnets = [
        {
            "SubnetId": "subnet-bmc",
            "MapPublicIpOnLaunch": True,
            "Tags": [{"Key": "Role", "Value": "bmc-management"}],
        }
    ]
    ec2 = FakeBastionEc2()

    result = module._check_no_direct_public_route(ec2, management_subnets)

    assert result["passed"] is False
    assert "MapPublicIpOnLaunch" in result["error"]


def test_bmc_bastion_access_detects_world_open_bastion_ssh() -> None:
    """SEC12-03 fails when the bastion SG itself allows SSH from 0.0.0.0/0."""
    module = _load_security_script("bmc_bastion_access_test.py")
    bastion_sgs = [
        {
            "GroupId": "sg-bastion",
            "Tags": [{"Key": "Role", "Value": "bastion"}],
            "IpPermissions": [
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                }
            ],
        }
    ]

    result = module._check_bastion_hardened(bastion_sgs)

    assert result["passed"] is False
    assert "sg-bastion" in result["error"]


def test_bmc_bastion_access_main_emits_sec12_03_contract(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Successful AWS reference run emits the SEC12-03 validation contract."""
    module = _load_security_script("bmc_bastion_access_test.py")
    bastion_sg = {
        "GroupId": "sg-bastion",
        "Tags": [{"Key": "Role", "Value": "jumphost"}],
        "IpPermissions": [
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": "10.0.0.0/16"}],
            }
        ],
    }
    bmc_sg = {
        "GroupId": "sg-bmc",
        "Tags": [{"Key": "Role", "Value": "bmc-network"}],
        "IpPermissions": [
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "UserIdGroupPairs": [{"GroupId": "sg-bastion"}],
            }
        ],
    }
    bmc_subnet = {
        "SubnetId": "subnet-bmc",
        "MapPublicIpOnLaunch": False,
        "Tags": [{"Key": "Role", "Value": "bmc-management"}],
    }
    ec2 = FakeBastionEc2(
        security_groups=[bastion_sg, bmc_sg],
        subnets=[bmc_subnet],
        route_tables=[{"RouteTableId": "rtb-bmc-private", "Routes": []}],
    )

    def fake_client(service_name: str, **_: Any) -> FakeBastionEc2:
        """Return the fake EC2 client."""
        assert service_name == "ec2"
        return ec2

    monkeypatch.setattr(module.boto3, "client", fake_client)
    monkeypatch.setattr(module.sys, "argv", ["bmc_bastion_access_test.py", "--region", "us-west-2"])

    exit_code = module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["success"] is True
    assert payload["test_name"] == "bmc_bastion_access"
    assert set(payload["tests"]) == {
        "bastion_identifiable",
        "management_ingress_via_bastion_only",
        "no_direct_public_route",
        "bastion_hardened",
    }
    for subtest in payload["tests"].values():
        assert subtest["passed"] is True


# --- Audit logging (SEC08-01/02) tests -------------------------------------


def test_audit_logging_includes_cloudtrail_shadow_trails() -> None:
    """Audit logging discovery must include multi-region shadow trails in non-home regions."""
    module = _load_security_script("audit_logging_test.py")

    class FakeCloudTrail:
        def __init__(self) -> None:
            """Track the requested shadow-trail setting."""
            self.include_shadow_trails: bool | None = None
            self.status_names: list[str] = []

        def describe_trails(self, includeShadowTrails: bool) -> dict[str, list[dict[str, Any]]]:
            """Return the fake org trail only when shadow trails are requested."""
            self.include_shadow_trails = includeShadowTrails
            if not includeShadowTrails:
                return {"trailList": []}
            return {
                "trailList": [
                    {
                        "Name": "org-trail",
                        "TrailARN": "arn:aws:cloudtrail:us-east-1:123456789012:trail/org-trail",
                        "IsMultiRegionTrail": True,
                        "S3BucketName": "audit-logs",
                    }
                ]
            }

        def get_trail_status(self, Name: str) -> dict[str, bool]:
            """Report the shadow trail as actively logging."""
            self.status_names.append(Name)
            return {"IsLogging": True}

    cloudtrail = FakeCloudTrail()

    logging_trails, multi_region_logging_trails = module._find_logging_trails(cloudtrail)

    assert cloudtrail.include_shadow_trails is True
    assert len(logging_trails) == 1
    assert len(multi_region_logging_trails) == 1
    assert cloudtrail.status_names == ["arn:aws:cloudtrail:us-east-1:123456789012:trail/org-trail"]


def test_audit_logging_lookup_scans_all_lookup_event_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """A matching CloudTrail event on a later page must be found before the next poll delay."""
    module = _load_security_script("audit_logging_test.py")
    request_id = "request-later-page"
    user_agent_suffix = "isv-sec08-unit"
    target_event = {
        "EventId": "event-target",
        "CloudTrailEvent": json.dumps({"requestID": request_id, "userAgent": f"botocore {user_agent_suffix}"}),
    }

    class FakePagedCloudTrail:
        def __init__(self) -> None:
            """Build five non-matching pages followed by the target event."""
            self.calls: list[dict[str, Any]] = []
            self.pages = [
                {
                    "Events": [
                        {
                            "EventId": f"event-{index}",
                            "CloudTrailEvent": json.dumps({"requestID": f"other-{index}", "userAgent": "botocore"}),
                        }
                    ],
                    "NextToken": f"token-{index + 1}",
                }
                for index in range(5)
            ]
            self.pages.append({"Events": [target_event]})

        def lookup_events(self, **kwargs: Any) -> dict[str, Any]:
            """Return one configured LookupEvents page and verify token sequencing."""
            index = len(self.calls)
            self.calls.append(kwargs)
            if index == 0:
                assert "NextToken" not in kwargs
            else:
                assert kwargs["NextToken"] == f"token-{index}"
            if index >= len(self.pages):
                raise AssertionError("lookup restarted polling before scanning all pages")
            return self.pages[index]

    cloudtrail = FakePagedCloudTrail()
    monkeypatch.setattr(
        module.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(AssertionError("matching event was on a later page")),
    )

    event = module._lookup_management_event(
        cloudtrail,
        request_id=request_id,
        user_agent_suffix=user_agent_suffix,
        call_start=datetime(2026, 5, 5, tzinfo=UTC),
        timeout_seconds=60,
    )

    assert event == target_event
    assert len(cloudtrail.calls) == 6


def test_audit_logging_retention_ignores_unrelated_lifecycle_prefixes() -> None:
    """A short expiration rule for another prefix must not fail CloudTrail log retention."""
    module = _load_security_script("audit_logging_test.py")

    class FakeLifecycleS3:
        def get_bucket_lifecycle_configuration(self, Bucket: str) -> dict[str, list[dict[str, Any]]]:
            """Return both unrelated short retention and applicable long retention rules."""
            assert Bucket == "audit-logs"
            return {
                "Rules": [
                    {
                        "ID": "tmp-short",
                        "Status": "Enabled",
                        "Filter": {"Prefix": "tmp/"},
                        "Expiration": {"Days": 7},
                    },
                    {
                        "ID": "cloudtrail-long",
                        "Status": "Enabled",
                        "Filter": {"Prefix": "AWSLogs/"},
                        "Expiration": {"Days": 90},
                    },
                ]
            }

    retention_result, minimum_retention = module._evaluate_lifecycle_retention(FakeLifecycleS3(), "audit-logs")

    assert retention_result["passed"] is True
    assert minimum_retention == 90


def test_audit_logging_retention_skips_when_lifecycle_access_denied(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing bucket lifecycle permission must skip retention instead of failing SEC08-01."""
    module = _load_security_script("audit_logging_test.py")

    class FakeCloudTrail:
        def describe_trails(self, includeShadowTrails: bool) -> dict[str, list[dict[str, Any]]]:
            """Return one active multi-region trail."""
            assert includeShadowTrails is True
            return {
                "trailList": [
                    {
                        "Name": "org-trail",
                        "TrailARN": "arn:aws:cloudtrail:us-east-1:123456789012:trail/org-trail",
                        "IsMultiRegionTrail": True,
                        "S3BucketName": "audit-logs",
                    }
                ]
            }

        def get_trail_status(self, Name: str) -> dict[str, bool]:
            """Report the trail as actively logging."""
            assert Name == "arn:aws:cloudtrail:us-east-1:123456789012:trail/org-trail"
            return {"IsLogging": True}

    class FakeS3:
        def get_bucket_lifecycle_configuration(self, Bucket: str) -> dict[str, list[dict[str, Any]]]:
            """Deny lifecycle reads for the audit-log bucket."""
            assert Bucket == "audit-logs"
            raise _client_error("GetBucketLifecycleConfiguration", code="AccessDenied")

    def fake_client(service_name: str, **_kwargs: Any) -> Any:
        """Return fake clients used by the audit script."""
        if service_name == "cloudtrail":
            return FakeCloudTrail()
        if service_name == "s3":
            return FakeS3()
        raise AssertionError(service_name)

    def fake_lookup_management_event(*_args: Any, **kwargs: Any) -> dict[str, str]:
        """Return a matched CloudTrail event using the generated user-agent suffix."""
        user_agent_suffix = kwargs["user_agent_suffix"]
        return {
            "EventId": "event-1",
            "CloudTrailEvent": json.dumps(
                {
                    "requestID": "request-1",
                    "eventName": module.EVENT_NAME,
                    "eventTime": datetime.now(UTC).isoformat(),
                    "userIdentity": {"arn": "arn:aws:iam::123456789012:user/test"},
                    "sourceIPAddress": "203.0.113.10",
                    "userAgent": f"botocore {user_agent_suffix}",
                    "awsRegion": "us-west-2",
                    "eventSource": module.EVENT_SOURCE,
                }
            ),
        }

    monkeypatch.setattr(module.boto3, "client", fake_client)
    monkeypatch.setattr(
        module,
        "_emit_management_call",
        lambda _region, _suffix: ("request-1", datetime.now(UTC), datetime.now(UTC)),
    )
    monkeypatch.setattr(module, "_lookup_management_event", fake_lookup_management_event)
    monkeypatch.setattr(module.sys, "argv", ["audit_logging_test.py", "--region", "us-west-2"])

    exit_code = module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["success"] is True
    assert payload["audit_log_retention_skipped"] is True
    assert "cannot inspect lifecycle configuration" in payload["audit_log_retention_skip_reason"]
    assert payload["tests"]["audit_log_entry_found"]["passed"] is True


class FakeIamTags:
    """Small fake for IAM list_user_tags responses."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        """Store responses returned by list_user_tags."""
        self.responses = responses

    def list_user_tags(self, **kwargs: Any) -> dict[str, Any]:
        """Return the next fake list_user_tags response."""
        assert kwargs["UserName"] == "isv-sa-test-1234"
        return self.responses.pop(0)


def test_teardown_only_treats_created_by_isvtest_users_as_owned() -> None:
    """Teardown ownership requires the CreatedBy=isvtest tag."""
    module = _load_security_script("teardown.py")
    iam = FakeIamTags([{"Tags": [{"Key": "CreatedBy", "Value": "isvtest"}], "IsTruncated": False}])

    assert module._user_has_isvtest_tag(iam, "isv-sa-test-1234") is True


def test_teardown_does_not_treat_prefix_only_users_as_owned() -> None:
    """A matching username prefix is not enough to delete a user."""
    module = _load_security_script("teardown.py")
    iam = FakeIamTags([{"Tags": [{"Key": "CreatedBy", "Value": "someone-else"}], "IsTruncated": False}])

    assert module._user_has_isvtest_tag(iam, "isv-sa-test-1234") is False


def test_teardown_checks_paginated_user_tags() -> None:
    """Ownership checks scan paginated IAM user tags."""
    module = _load_security_script("teardown.py")
    iam = FakeIamTags(
        [
            {"Tags": [{"Key": "Name", "Value": "validation"}], "IsTruncated": True, "Marker": "next"},
            {"Tags": [{"Key": "CreatedBy", "Value": "isvtest"}], "IsTruncated": False},
        ]
    )

    assert module._user_has_isvtest_tag(iam, "isv-sa-test-1234") is True


class FakeSaCredentialIam:
    """Fake IAM client for service account credential cleanup tests."""

    def __init__(self, delete_access_key_error: ClientError | None = None) -> None:
        """Configure optional delete_access_key failure."""
        self.delete_access_key_error = delete_access_key_error

    def create_user(self, UserName: str, Tags: list[dict[str, str]]) -> None:
        """Record user creation."""
        assert UserName.startswith("isv-sa-test-")
        assert {"Key": "CreatedBy", "Value": "isvtest"} in Tags

    def create_access_key(self, UserName: str) -> dict[str, dict[str, str]]:
        """Return a fake long-lived access key."""
        assert UserName.startswith("isv-sa-test-")
        return {"AccessKey": {"AccessKeyId": "AKIA_TEST", "SecretAccessKey": "secret"}}

    def delete_access_key(self, UserName: str, AccessKeyId: str) -> None:
        """Optionally fail access key deletion."""
        assert UserName.startswith("isv-sa-test-")
        assert AccessKeyId == "AKIA_TEST"
        if self.delete_access_key_error:
            raise self.delete_access_key_error

    def delete_user(self, UserName: str) -> None:
        """Delete the fake IAM user."""
        assert UserName.startswith("isv-sa-test-")


class FakeSts:
    """Fake STS client for service account credential tests."""

    def get_caller_identity(self) -> dict[str, str]:
        """Return a fake caller identity."""
        return {"Arn": "arn:aws:iam::123456789012:user/isv-sa-test-unit"}


def test_sa_credential_main_fails_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Successful authentication is reported failed when IAM cleanup fails."""
    module = _load_security_script("sa_credential_test.py")
    iam = FakeSaCredentialIam(delete_access_key_error=_client_error("DeleteAccessKey"))
    sts = FakeSts()

    def fake_client(service_name: str, **kwargs: Any) -> FakeSaCredentialIam | FakeSts:
        """Return fake clients for IAM and STS."""
        if service_name == "iam":
            return iam
        if service_name == "sts":
            return sts
        msg = f"unexpected service: {service_name}"
        raise AssertionError(msg)

    monkeypatch.setattr(module.boto3, "client", fake_client)
    monkeypatch.setattr(module.sys, "argv", ["sa_credential_test.py", "--region", "us-west-2"])

    exit_code = module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["success"] is False
    assert payload["authenticated"] is True
    assert "cleanup_errors" in payload
    assert "delete access key AKIA_TEST" in payload["cleanup_errors"][0]


class FakePaginator:
    """Fake paginator for list_users."""

    def paginate(self) -> list[dict[str, list[dict[str, str]]]]:
        """Return one owned test user."""
        return [{"Users": [{"UserName": "isv-sa-test-leftover"}]}]


class FakeTeardownIam:
    """Fake IAM client for teardown cleanup tests."""

    def __init__(self) -> None:
        """Initialize call tracking."""
        self.delete_user_called = False

    def get_paginator(self, operation_name: str) -> FakePaginator:
        """Return a fake list_users paginator."""
        assert operation_name == "list_users"
        return FakePaginator()

    def list_user_tags(self, UserName: str) -> dict[str, Any]:
        """Return ownership tag for the fake user."""
        assert UserName == "isv-sa-test-leftover"
        return {"Tags": [{"Key": "CreatedBy", "Value": "isvtest"}], "IsTruncated": False}

    def list_access_keys(self, UserName: str) -> dict[str, list[dict[str, str]]]:
        """Return one fake access key."""
        assert UserName == "isv-sa-test-leftover"
        return {"AccessKeyMetadata": [{"AccessKeyId": "AKIA_LEFTOVER"}]}

    def delete_access_key(self, UserName: str, AccessKeyId: str) -> None:
        """Fail access key deletion."""
        assert UserName == "isv-sa-test-leftover"
        assert AccessKeyId == "AKIA_LEFTOVER"
        raise _client_error("DeleteAccessKey")

    def list_user_policies(self, UserName: str) -> dict[str, list[str]]:
        """Return no inline policies for the legacy sa_credential test user."""
        assert UserName == "isv-sa-test-leftover"
        return {"PolicyNames": []}

    def delete_user(self, UserName: str) -> None:
        """Fail user deletion after access key deletion failed."""
        assert UserName == "isv-sa-test-leftover"
        self.delete_user_called = True
        raise _client_error("DeleteUser", code="DeleteConflict")


class FakeNoSec11Resources:
    """Trivial AWS client stub used for ec2/kms/s3 when the SEC11 sweep has nothing to find.

    The sweep helpers iterate over describe_* / list_* responses; returning
    empty pages turns each helper into a no-op without exercising it.
    """

    def get_paginator(self, _operation_name: str) -> Any:
        """Return a paginator that yields a single empty page."""

        class _P:
            def paginate(self, **_kwargs: Any) -> list[dict[str, Any]]:
                return [{"Reservations": [], "Volumes": [], "Aliases": [], "Versions": [], "DeleteMarkers": []}]

        return _P()

    def describe_vpcs(self, **_kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        """Return no VPCs."""
        return {"Vpcs": []}

    def list_buckets(self) -> dict[str, list[dict[str, str]]]:
        """Return no buckets."""
        return {"Buckets": []}


def test_teardown_main_fails_when_owned_user_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Teardown reports failure when owned IAM resources cannot be removed."""
    module = _load_security_script("teardown.py")
    iam = FakeTeardownIam()
    no_sec11 = FakeNoSec11Resources()

    def fake_client(service_name: str, **kwargs: Any) -> Any:
        """Return the fake IAM client; trivial empty stub for SEC11 sweep services."""
        if service_name == "iam":
            return iam
        if service_name in {"ec2", "kms", "s3"}:
            return no_sec11
        msg = f"unexpected service: {service_name}"
        raise AssertionError(msg)

    monkeypatch.setattr(module.boto3, "client", fake_client)
    monkeypatch.setattr(module.sys, "argv", ["teardown.py", "--region", "us-west-2"])

    exit_code = module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["success"] is False
    assert payload["resources_cleaned"] == 0
    assert payload["resources_failed"][0]["username"] == "isv-sa-test-leftover"
    # Two failure paths now plus a list_inline_policies success: keep
    # the existing two-failure assertion but allow the inline-policy
    # listing call to succeed silently.
    assert len(payload["resources_failed"][0]["errors"]) == 2
    assert iam.delete_user_called is True


class FakeSec02CleanupIam:
    """Fake IAM client exercising teardown's inline-policy cleanup path for SEC02 users."""

    def __init__(self) -> None:
        """Initialize call tracking."""
        self.deleted_keys: list[tuple[str, str]] = []
        self.deleted_policies: list[tuple[str, str]] = []
        self.deleted_users: list[str] = []
        # Ordered call log so tests can lock in the relative order of
        # delete_user_policy vs delete_user (the inline policy must be
        # detached first or DeleteUser fails with DeleteConflict on AWS).
        self.call_sequence: list[str] = []

    def list_access_keys(self, UserName: str) -> dict[str, list[dict[str, str]]]:
        """Return one fake access key."""
        assert UserName.startswith("isv-sec02-test-")
        self.call_sequence.append(f"list_access_keys:{UserName}")
        return {"AccessKeyMetadata": [{"AccessKeyId": "AKIA_SEC02"}]}

    def delete_access_key(self, UserName: str, AccessKeyId: str) -> None:
        """Record access key deletion."""
        self.deleted_keys.append((UserName, AccessKeyId))
        self.call_sequence.append(f"delete_access_key:{UserName}:{AccessKeyId}")

    def list_user_policies(self, UserName: str) -> dict[str, list[str]]:
        """Return one inline policy attached to the SEC02 user."""
        assert UserName.startswith("isv-sec02-test-")
        self.call_sequence.append(f"list_user_policies:{UserName}")
        return {"PolicyNames": ["isv-sec02-sts-allow"]}

    def delete_user_policy(self, UserName: str, PolicyName: str) -> None:
        """Record inline policy deletion."""
        self.deleted_policies.append((UserName, PolicyName))
        self.call_sequence.append(f"delete_user_policy:{UserName}:{PolicyName}")

    def delete_user(self, UserName: str) -> None:
        """Record user deletion."""
        self.deleted_users.append(UserName)
        self.call_sequence.append(f"delete_user:{UserName}")


def test_teardown_cleanup_owned_user_deletes_inline_policy_for_sec02_users() -> None:
    """`_cleanup_owned_user` must detach inline policies before deleting SEC02 users."""
    module = _load_security_script("teardown.py")
    iam = FakeSec02CleanupIam()

    cleanup_errors = module._cleanup_owned_user(iam, "isv-sec02-test-abcd1234")

    assert cleanup_errors == []
    assert iam.deleted_keys == [("isv-sec02-test-abcd1234", "AKIA_SEC02")]
    assert iam.deleted_policies == [("isv-sec02-test-abcd1234", "isv-sec02-sts-allow")]
    assert iam.deleted_users == ["isv-sec02-test-abcd1234"]
    # Order matters: AWS DeleteUser fails with DeleteConflict if an inline
    # policy is still attached. Lock in policy-before-user.
    policy_idx = iam.call_sequence.index("delete_user_policy:isv-sec02-test-abcd1234:isv-sec02-sts-allow")
    user_idx = iam.call_sequence.index("delete_user:isv-sec02-test-abcd1234")
    assert policy_idx < user_idx


def test_teardown_owned_user_prefixes_cover_security_test_scripts() -> None:
    """The teardown sweep must recognize every owned security-test IAM user prefix."""
    module = _load_security_script("teardown.py")

    assert "isv-sa-test-".startswith("isv-sa-test-")
    assert "isv-sec04-test-foo".startswith(module.OWNED_USER_PREFIXES)
    assert "isv-sec02-test-foo".startswith(module.OWNED_USER_PREFIXES)
    assert "isv-sec11-test-foo".startswith(module.OWNED_USER_PREFIXES)
    assert "isv-sa-test-bar".startswith(module.OWNED_USER_PREFIXES)
    assert not "isv-network-test-baz".startswith(module.OWNED_USER_PREFIXES)


@pytest.fixture(scope="module")
def cert_rotation_module() -> ModuleType:
    """Load the certificate-rotation script as a module."""
    return _load_security_script("cert_rotation_test.py")


class FakeCertRotationEks:
    """Fake EKS client for certificate-rotation tests."""

    def __init__(self, clusters: dict[str, str | dict[str, Any]] | None = None) -> None:
        """Store fake cluster metadata keyed by cluster name."""
        self.clusters = clusters or {}
        self.describe_call_count = 0

    def list_clusters(self, **kwargs: Any) -> dict[str, list[str]]:
        """Return fake EKS cluster names."""
        assert kwargs in ({}, {"nextToken": ""})
        return {"clusters": list(self.clusters)}

    def describe_cluster(self, name: str) -> dict[str, dict[str, Any]]:
        """Return a fake EKS cluster description."""
        self.describe_call_count += 1
        cluster = self.clusters[name]
        if isinstance(cluster, str):
            return {"cluster": {"endpoint": cluster}}
        return {"cluster": cluster}


class PartiallyFailingCertRotationEks(FakeCertRotationEks):
    """Fake EKS client that can list clusters but cannot describe one of them."""

    def describe_cluster(self, name: str) -> dict[str, dict[str, Any]]:
        """Raise AccessDenied for one cluster and return metadata for the rest."""
        if name == "denied":
            raise _client_error("DescribeCluster")
        return super().describe_cluster(name)


def test_cert_rotation_accepts_eks_control_plane_endpoint_certs(
    cert_rotation_module: ModuleType,
) -> None:
    """SEC09-01 emits an explicit provider-hidden skip for EKS endpoint certificates."""
    eks = FakeCertRotationEks({"cluster-a": "https://eks.test.local"})

    result = cert_rotation_module._run_cert_rotation_test(eks, "us-west-2")

    assert result["success"] is True
    assert result["skipped"] is True
    assert result["skip_reason"] == "Managed TLS certificate rotation evidence is provider-hidden"
    assert result["certs_inspected"] == 1
    assert result["auto_rotated"] == 0
    assert result["short_validity"] == 0
    assert result["out_of_policy"] == 0
    assert result["certificates"][0]["source"] == "eks"
    assert result["certificates"][0]["provider_managed"] is True
    assert result["certificates"][0]["rotation_evidence_hidden"] is True
    assert "auto_rotated" not in result["certificates"][0]
    assert all(test["passed"] and test["skipped"] for test in result["tests"].values())


def test_cert_rotation_treats_private_eks_endpoint_certs_as_provider_managed(
    cert_rotation_module: ModuleType,
) -> None:
    """Private-only EKS API endpoint certificates are AWS-managed and not probed."""
    eks = FakeCertRotationEks(
        {
            "private-cluster": {
                "endpoint": "https://private.eks.test.local",
                "resourcesVpcConfig": {
                    "endpointPublicAccess": False,
                    "endpointPrivateAccess": True,
                },
            }
        }
    )

    result = cert_rotation_module._run_cert_rotation_test(eks, "us-west-2")

    record = result["certificates"][0]
    assert result["success"] is True
    assert result["skipped"] is True
    assert result["certs_inspected"] == 1
    assert result["auto_rotated"] == 0
    assert result["short_validity"] == 0
    assert result["out_of_policy"] == 0
    assert "inspection_errors" not in result
    assert record["source"] == "eks"
    assert record["provider_managed"] is True
    assert record["rotation_evidence_hidden"] is True
    assert "out_of_policy" not in record
    assert record["endpoint_private_access"] is True


def test_cert_rotation_reports_inspection_errors_without_policy_fallback(
    cert_rotation_module: ModuleType,
) -> None:
    """Partial EKS inspection failures are reported directly, not evaluated as certificate-policy failures."""
    eks = PartiallyFailingCertRotationEks(
        {
            "cluster-a": "https://eks.test.local",
            "denied": "https://denied.eks.test.local",
        }
    )

    result = cert_rotation_module._run_cert_rotation_test(eks, "us-west-2")

    assert result["success"] is False
    assert "skipped" not in result
    assert result["certs_inspected"] == 1
    assert result["auto_rotated"] == 0
    assert result["short_validity"] == 0
    assert result["out_of_policy"] == 0
    assert len(result["inspection_errors"]) == 1
    assert "denied" in result["inspection_errors"][0]
    assert result["tests"]["cert_inventory_non_empty"]["passed"] is True
    assert result["tests"]["no_certs_out_of_policy"]["passed"] is False
    assert result["tests"]["rotation_evidence_present"]["passed"] is False
    assert "inspection errors prevented" in result["tests"]["rotation_evidence_present"]["error"]


def test_cert_rotation_reports_skipped_when_no_eks_clusters(cert_rotation_module: ModuleType) -> None:
    """SEC09-01 skips cleanly when no EKS control-plane cert inventory exists."""
    result = cert_rotation_module._run_cert_rotation_test(FakeCertRotationEks(), "us-west-2")

    assert result["success"] is True
    assert result["skipped"] is True
    assert result["skip_reason"] == "No managed TLS certificates found on this platform"
    assert result["certs_inspected"] == 0
    assert all(test["passed"] and test["skipped"] for test in result["tests"].values())


def test_cert_rotation_caps_eks_inventory_at_sample_limit(cert_rotation_module: ModuleType) -> None:
    """EKS cluster inspection stops at MAX_CERTS_PER_SOURCE so the step stays within the YAML timeout."""
    limit = cert_rotation_module.MAX_CERTS_PER_SOURCE
    eks = FakeCertRotationEks({f"cluster-{i}": f"https://cluster-{i}.eks.test.local" for i in range(limit + 5)})

    result = cert_rotation_module._run_cert_rotation_test(eks, "us-west-2")

    eks_records = [r for r in result["certificates"] if r["source"] == "eks"]
    assert len(eks_records) == limit
    assert eks.describe_call_count == limit
    assert result["sample_limit_per_source"] == limit


@pytest.fixture(scope="module")
def kms_options_module() -> ModuleType:
    """Load the KMS encryption options script as a module."""
    return _load_security_script("kms_encryption_options_test.py")


class FakeKmsOptionsKms:
    """Fake KMS client for SEC09-02 tests."""

    def __init__(
        self,
        *,
        provider_key_manager: str = "AWS",
        schedule_error: ClientError | None = None,
        describe_errors: dict[str, ClientError] | None = None,
        aliases: list[str] | None = None,
        alias_pages: list[list[str]] | None = None,
    ) -> None:
        """Configure provider key metadata and optional cleanup failure."""
        self.provider_key_manager = provider_key_manager
        self.schedule_error = schedule_error
        self.describe_errors = describe_errors or {}
        self.aliases = aliases if aliases is not None else []
        self.alias_pages = alias_pages
        self.scheduled_deletions: list[dict[str, Any]] = []

    def list_aliases(self) -> dict[str, list[dict[str, str]]]:
        """Return fake KMS aliases."""
        return {"Aliases": [{"AliasName": name} for name in self.aliases]}

    def get_paginator(self, operation_name: str) -> FakeKmsAliasPaginator:
        """Return a fake paginator for alias discovery."""
        assert operation_name == "list_aliases"
        if self.alias_pages is None:
            raise AttributeError("get_paginator")
        return FakeKmsAliasPaginator(self.alias_pages)

    def describe_key(self, KeyId: str) -> dict[str, dict[str, Any]]:
        """Return fake provider or customer key metadata."""
        if KeyId in self.describe_errors:
            raise self.describe_errors[KeyId]
        if KeyId.startswith("alias/aws/"):
            return {
                "KeyMetadata": {
                    "KeyId": "aws-managed-123",
                    "Arn": "arn:aws:kms:us-west-2:123:key/aws-managed-123",
                    "KeyManager": self.provider_key_manager,
                    "KeyState": "Enabled",
                    "KeyUsage": "ENCRYPT_DECRYPT",
                }
            }
        assert KeyId == "cmk-options-123"
        return {
            "KeyMetadata": {
                "KeyId": "cmk-options-123",
                "Arn": "arn:aws:kms:us-west-2:123:key/cmk-options-123",
                "KeyManager": "CUSTOMER",
                "KeyState": "Enabled",
                "KeyUsage": "ENCRYPT_DECRYPT",
            }
        }

    def create_key(self, **kwargs: Any) -> dict[str, dict[str, Any]]:
        """Create a fake temporary CMK."""
        assert kwargs["KeyUsage"] == "ENCRYPT_DECRYPT"
        return self.describe_key(KeyId="cmk-options-123")

    def schedule_key_deletion(self, **kwargs: Any) -> None:
        """Record or fail temporary key deletion."""
        if self.schedule_error:
            raise self.schedule_error
        self.scheduled_deletions.append(kwargs)


class FakeKmsAliasPaginator:
    """Fake KMS alias paginator."""

    def __init__(self, alias_pages: list[list[str]]) -> None:
        """Store pages of alias names."""
        self.alias_pages = alias_pages

    def paginate(self) -> list[dict[str, list[dict[str, str]]]]:
        """Return fake list_aliases pages."""
        return [{"Aliases": [{"AliasName": name} for name in page]} for page in self.alias_pages]


def test_kms_options_creates_customer_key_and_schedules_deletion(kms_options_module: ModuleType) -> None:
    """SEC09-02 passes with AWS-managed and temporary customer-managed keys."""
    kms = FakeKmsOptionsKms()

    result = kms_options_module._run_kms_encryption_options_test(kms, "us-west-2")

    assert result["success"] is True
    assert result["provider_managed_key_id"] == "alias/aws/eks"
    assert result["customer_managed_key_id"] == "cmk-options-123"
    assert kms.scheduled_deletions == [{"KeyId": "cmk-options-123", "PendingWindowInDays": 7}]


def test_kms_options_rejects_provider_alias_that_is_not_aws_managed(kms_options_module: ModuleType) -> None:
    """SEC09-02 fails when the provider-managed alias does not report KeyManager=AWS."""
    kms = FakeKmsOptionsKms(provider_key_manager="CUSTOMER")

    result = kms_options_module._run_kms_encryption_options_test(kms, "us-west-2")

    assert result["success"] is False
    assert result["tests"]["provider_managed_key_available"]["passed"] is False
    assert result["tests"]["customer_managed_key_available"]["passed"] is True
    assert kms.scheduled_deletions == [{"KeyId": "cmk-options-123", "PendingWindowInDays": 7}]


def test_kms_options_skips_when_only_generic_aliases_are_discovered(
    kms_options_module: ModuleType,
) -> None:
    """SEC09-02 skips when only generic AWS-managed aliases are visible.

    Generic AWS-managed service keys are not scoped control-plane evidence, so
    discovery is diagnostic only and must not pass the control.
    """
    kms = FakeKmsOptionsKms(
        describe_errors={
            "alias/aws/eks": _client_error("DescribeKey", code="NotFoundException"),
        },
        aliases=["alias/aws/ebs", "alias/aws/lambda"],
    )

    result = kms_options_module._run_kms_encryption_options_test(kms, "us-west-2")

    assert result["success"] is True
    assert result["skipped"] is True
    assert "only non-control-plane AWS-managed aliases" in result["skip_reason"]
    assert "alias/aws/ebs" in result["skip_reason"]
    assert result["provider_managed_key_id"] == ""
    assert result["customer_managed_key_id"] == ""
    assert kms.scheduled_deletions == []


def test_kms_options_discovers_aws_managed_aliases_across_pages(kms_options_module: ModuleType) -> None:
    """SEC09-02 alias discovery checks every KMS list_aliases page."""
    kms = FakeKmsOptionsKms(alias_pages=[["alias/customer/team-a"], ["alias/aws/s3"]])

    discovered, errors = kms_options_module._discover_aws_managed_aliases(kms)

    assert errors == []
    assert discovered == ["alias/aws/s3"]


def test_kms_options_fails_when_no_aws_managed_alias_exists(kms_options_module: ModuleType) -> None:
    """SEC09-02 fails cleanly when no AWS-managed alias is reachable."""
    kms = FakeKmsOptionsKms(
        describe_errors={
            "alias/aws/eks": _client_error("DescribeKey", code="NotFoundException"),
        },
        aliases=[],
    )

    result = kms_options_module._run_kms_encryption_options_test(kms, "us-west-2")

    assert result["success"] is False
    assert result["tests"]["provider_managed_key_available"]["passed"] is False
    assert "alias/aws/eks" in result["tests"]["provider_managed_key_available"]["error"]


def test_kms_options_fails_when_temporary_key_cleanup_fails(kms_options_module: ModuleType) -> None:
    """SEC09-02 reports cleanup failures because the temporary CMK would leak."""
    kms = FakeKmsOptionsKms(schedule_error=_client_error("ScheduleKeyDeletion"))

    result = kms_options_module._run_kms_encryption_options_test(kms, "us-west-2")

    assert result["success"] is False
    assert "cleanup_errors" in result
    assert "schedule key deletion cmk-options-123" in result["cleanup_errors"][0]
    assert result["tests"]["both_options_supported"]["passed"] is False
    assert "Cleanup failed" in result["tests"]["both_options_supported"]["error"]


@pytest.fixture(scope="module")
def centralized_kms_module() -> ModuleType:
    """Load the centralized KMS script as a module."""
    return _load_security_script("centralized_kms_test.py")


class FakeCentralizedKms:
    """Fake KMS client for SEC09-03 tests."""

    def __init__(self, keys: list[str] | None = None, missing_keys: set[str] | None = None) -> None:
        """Store visible and non-resolving key ids."""
        self.keys = keys if keys is not None else ["key-1"]
        self.missing_keys = missing_keys or set()

    def list_keys(self) -> dict[str, list[dict[str, str]]]:
        """Return fake KMS keys."""
        return {"Keys": [{"KeyId": key_id} for key_id in self.keys]}

    def describe_key(self, KeyId: str) -> dict[str, dict[str, str]]:
        """Resolve fake KMS key ids."""
        if KeyId in self.missing_keys:
            raise _client_error("DescribeKey", code="NotFoundException")
        return {"KeyMetadata": {"KeyId": KeyId, "KeyState": "Enabled"}}


class FailingCentralizedKms(FakeCentralizedKms):
    """Fake KMS client whose key inventory call fails."""

    def list_keys(self) -> dict[str, list[dict[str, str]]]:
        """Raise a fake KMS list_keys failure."""
        raise _client_error("ListKeys")


class FakeCentralizedEc2:
    """Fake EC2 client for SEC09-03 tests."""

    def __init__(self, volumes: list[dict[str, Any]] | None = None) -> None:
        """Store fake EBS volumes."""
        self.volumes = (
            volumes if volumes is not None else [{"VolumeId": "vol-1", "Encrypted": True, "KmsKeyId": "key-1"}]
        )

    def describe_volumes(self, **kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        """Return fake encrypted volumes."""
        assert kwargs == {"Filters": [{"Name": "encrypted", "Values": ["true"]}]}
        return {"Volumes": self.volumes}


class FakeCentralizedEks:
    """Fake EKS client for SEC09-03 tests."""

    def __init__(self, clusters: dict[str, dict[str, Any]] | None = None) -> None:
        """Store fake EKS cluster descriptions."""
        self.clusters = clusters or {}
        self.described: list[str] = []

    def list_clusters(self, **kwargs: Any) -> dict[str, list[str]]:
        """Return fake EKS clusters."""
        assert kwargs in ({}, {"nextToken": ""})
        return {"clusters": list(self.clusters)}

    def describe_cluster(self, name: str) -> dict[str, dict[str, Any]]:
        """Return fake EKS cluster metadata."""
        self.described.append(name)
        return {"cluster": self.clusters[name]}


def test_centralized_kms_accepts_resources_that_resolve_to_kms(centralized_kms_module: ModuleType) -> None:
    """SEC09-03 passes when sampled encrypted resources resolve through KMS."""
    eks = FakeCentralizedEks({"cluster-a": {"encryptionConfig": [{"provider": {"keyArn": "key-1"}}]}})

    result = centralized_kms_module._run_centralized_kms_test(
        FakeCentralizedKms(),
        FakeCentralizedEc2(),
        eks,
        "us-west-2",
    )

    assert result["success"] is True
    assert result["kms_keys_total"] == 1
    assert result["encrypted_resources_inspected"] == 2
    assert result["non_kms_resources"] == 0


def test_centralized_kms_limits_eks_cluster_descriptions(centralized_kms_module: ModuleType) -> None:
    """SEC09-03 caps EKS cluster descriptions even when no clusters expose KMS providers."""
    limit = centralized_kms_module.MAX_RESOURCES_PER_SERVICE
    eks = FakeCentralizedEks({f"cluster-{idx}": {} for idx in range(limit + 5)})
    details: list[str] = []

    inspected = centralized_kms_module._inspect_eks_clusters(eks, FakeCentralizedKms(), details)

    assert inspected == 0
    assert details == []
    assert len(eks.described) == limit


def test_centralized_kms_flags_encrypted_volume_without_kms_key(centralized_kms_module: ModuleType) -> None:
    """SEC09-03 fails when an encrypted volume has no resolvable KMS key id."""
    ec2 = FakeCentralizedEc2([{"VolumeId": "vol-no-key", "Encrypted": True}])

    result = centralized_kms_module._run_centralized_kms_test(
        FakeCentralizedKms(),
        ec2,
        FakeCentralizedEks(),
        "us-west-2",
    )

    assert result["success"] is False
    assert result["non_kms_resources"] == 1
    assert "ec2:vol-no-key" in result["non_kms_details"][0]


def test_centralized_kms_marks_kms_unreachable_when_key_listing_fails(
    centralized_kms_module: ModuleType,
) -> None:
    """SEC09-03 reports KMS reachability failure before inspecting dependent resources."""
    result = centralized_kms_module._run_centralized_kms_test(
        FailingCentralizedKms(),
        FakeCentralizedEc2(),
        FakeCentralizedEks(),
        "us-west-2",
    )

    assert result["success"] is False
    assert result["encrypted_resources_inspected"] == 0
    assert result["tests"]["kms_service_reachable"]["passed"] is False
    assert result["tests"]["kms_keys_present"]["passed"] is False
    assert result["tests"]["all_encrypted_resources_use_kms"]["passed"] is False
    assert "KMS list_keys failed" in result["tests"]["kms_service_reachable"]["error"]


def test_centralized_kms_separates_inspection_errors_from_non_kms_count(
    centralized_kms_module: ModuleType,
) -> None:
    """SEC09-03 surfaces inspection errors without inflating non_kms_resources.

    A read-only AWS permission gap raising ClientError must not be reported
    as 'N encrypted resource(s) not using KMS' in the failure message.
    """

    class RaisingEc2:
        """Fake EC2 client whose service-level describe call raises ClientError."""

        def describe_volumes(self, **kwargs: Any) -> dict[str, list[dict[str, Any]]]:
            """Fake EC2 describe_volumes that always raises AccessDenied."""
            raise _client_error("DescribeVolumes", code="AccessDenied")

    result = centralized_kms_module._run_centralized_kms_test(
        FakeCentralizedKms(),
        RaisingEc2(),
        FakeCentralizedEks(),
        "us-west-2",
    )

    assert result["success"] is False
    assert result["non_kms_resources"] == 0
    assert result["non_kms_details"] == []
    assert len(result["inspection_errors"]) == 1
    assert any("ec2:" in err for err in result["inspection_errors"])
    error_message = result["tests"]["all_encrypted_resources_use_kms"]["error"]
    assert "Inspection errors" in error_message


def test_centralized_kms_requires_visible_kms_keys(centralized_kms_module: ModuleType) -> None:
    """SEC09-03 fails when KMS is reachable but no keys are visible."""
    result = centralized_kms_module._run_centralized_kms_test(
        FakeCentralizedKms(keys=[]),
        FakeCentralizedEc2(volumes=[]),
        FakeCentralizedEks(),
        "us-west-2",
    )

    assert result["success"] is False
    assert result["tests"]["kms_service_reachable"]["passed"] is True
    assert result["tests"]["kms_keys_present"]["passed"] is False


@pytest.fixture(scope="module")
def byok_module() -> ModuleType:
    """Load the customer-managed key script as a module."""
    return _load_security_script("customer_managed_key_test.py")


class FakeByokWaiter:
    """Fake EC2 waiter for encrypted volume tests."""

    def __init__(self, error: Exception | None = None) -> None:
        """Initialize wait call tracking."""
        self.calls: list[dict[str, Any]] = []
        self.error = error

    def wait(self, **kwargs: Any) -> None:
        """Record waiter arguments."""
        self.calls.append(kwargs)
        if self.error:
            raise self.error


class FakeByokKms:
    """Fake KMS client for customer-managed key tests."""

    def __init__(
        self,
        *,
        key_manager: str = "CUSTOMER",
        plaintext_mismatch: bool = False,
        encrypt_error: ClientError | None = None,
    ) -> None:
        """Initialize fake KMS behavior."""
        self.key_metadata = {
            "KeyId": "cmk-123",
            "Arn": "arn:aws:kms:us-west-2:123456789012:key/cmk-123",
            "KeyManager": key_manager,
            "KeyState": "Enabled",
            "KeyUsage": "ENCRYPT_DECRYPT",
        }
        self.plaintext_mismatch = plaintext_mismatch
        self.encrypt_error = encrypt_error
        self.created_keys: list[dict[str, Any]] = []
        self.scheduled_deletions: list[dict[str, Any]] = []

    def create_key(self, **kwargs: Any) -> dict[str, dict[str, Any]]:
        """Create a fake customer-managed key."""
        self.created_keys.append(kwargs)
        return {"KeyMetadata": self.key_metadata}

    def describe_key(self, KeyId: str) -> dict[str, dict[str, Any]]:
        """Return fake KMS key metadata."""
        assert KeyId in {"cmk-123", self.key_metadata["Arn"], "alias/aws/ebs"}
        return {"KeyMetadata": self.key_metadata}

    def encrypt(self, KeyId: str, Plaintext: bytes) -> dict[str, bytes]:
        """Return fake ciphertext or raise the configured error."""
        assert KeyId == "cmk-123"
        if self.encrypt_error:
            raise self.encrypt_error
        return {"CiphertextBlob": b"ciphertext:" + Plaintext}

    def decrypt(self, KeyId: str, CiphertextBlob: bytes) -> dict[str, bytes]:
        """Return the decrypted fake plaintext."""
        assert KeyId == "cmk-123"
        assert CiphertextBlob.startswith(b"ciphertext:")
        if self.plaintext_mismatch:
            return {"Plaintext": b"wrong"}
        return {"Plaintext": CiphertextBlob.removeprefix(b"ciphertext:")}

    def schedule_key_deletion(self, **kwargs: Any) -> None:
        """Record a scheduled key deletion request."""
        self.scheduled_deletions.append(kwargs)


class FakeByokEc2:
    """Fake EC2 client for encrypted EBS volume tests."""

    def __init__(
        self,
        *,
        kms_key_id: str | None = None,
        encrypted: bool = True,
        waiter_error: Exception | None = None,
        describe_error: Exception | None = None,
    ) -> None:
        """Initialize fake EC2 behavior."""
        self.kms_key_id = kms_key_id or "arn:aws:kms:us-west-2:123456789012:key/cmk-123"
        self.encrypted = encrypted
        self.describe_error = describe_error
        self.created_volumes: list[dict[str, Any]] = []
        self.deleted_volumes: list[str] = []
        self.waiter = FakeByokWaiter(waiter_error)

    def describe_availability_zones(self, **kwargs: Any) -> dict[str, list[dict[str, str]]]:
        """Return one available AZ."""
        assert kwargs == {"Filters": [{"Name": "state", "Values": ["available"]}]}
        return {"AvailabilityZones": [{"ZoneName": "us-west-2a", "OptInStatus": "opt-in-not-required"}]}

    def create_volume(self, **kwargs: Any) -> dict[str, Any]:
        """Create a fake encrypted volume."""
        self.created_volumes.append(kwargs)
        return {
            "VolumeId": "vol-byok-123",
            "Encrypted": self.encrypted,
            "KmsKeyId": self.kms_key_id,
        }

    def get_waiter(self, waiter_name: str) -> FakeByokWaiter:
        """Return a fake waiter."""
        assert waiter_name == "volume_available"
        return self.waiter

    def describe_volumes(self, VolumeIds: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Return the fake volume description."""
        assert VolumeIds == ["vol-byok-123"]
        if self.describe_error:
            raise self.describe_error
        return {
            "Volumes": [
                {
                    "VolumeId": "vol-byok-123",
                    "Encrypted": self.encrypted,
                    "KmsKeyId": self.kms_key_id,
                }
            ]
        }

    def delete_volume(self, VolumeId: str) -> None:
        """Record fake volume deletion."""
        self.deleted_volumes.append(VolumeId)


def test_byok_existing_customer_managed_key_path(byok_module: ModuleType) -> None:
    """Existing customer-managed KMS key path passes all SEC09-04 probes."""
    kms = FakeByokKms()
    ec2 = FakeByokEc2()

    result = byok_module._run_customer_managed_key_test(kms, ec2, "us-west-2", "cmk-123")

    assert result["success"] is True
    assert result["key_id"] == "cmk-123"
    assert result["encrypted_resource_id"] == "vol-byok-123"
    assert all(test["passed"] for test in result["tests"].values())
    assert ec2.deleted_volumes == ["vol-byok-123"]
    assert kms.scheduled_deletions == []


def test_byok_rejects_aws_managed_key(byok_module: ModuleType) -> None:
    """AWS-managed KMS keys fail the customer-managed-key contract."""
    kms = FakeByokKms(key_manager="AWS")
    ec2 = FakeByokEc2()

    result = byok_module._run_customer_managed_key_test(kms, ec2, "us-west-2", "alias/aws/ebs")

    assert result["success"] is False
    assert result["tests"]["customer_managed_key_available"]["passed"] is True
    assert result["tests"]["key_manager_is_customer"]["passed"] is False
    assert result["tests"]["provider_managed_key_not_used"]["passed"] is False
    assert ec2.created_volumes == []


def test_byok_encrypt_decrypt_roundtrip_success_and_failure(byok_module: ModuleType) -> None:
    """KMS encrypt/decrypt roundtrip reports success and plaintext mismatch failure."""
    success = byok_module._check_encrypt_decrypt_roundtrip(FakeByokKms(), "cmk-123")
    mismatch = byok_module._check_encrypt_decrypt_roundtrip(FakeByokKms(plaintext_mismatch=True), "cmk-123")
    aws_error = byok_module._check_encrypt_decrypt_roundtrip(
        FakeByokKms(encrypt_error=_client_error("Encrypt")),
        "cmk-123",
    )

    assert success["passed"] is True
    assert mismatch["passed"] is False
    assert "did not match" in mismatch["error"]
    assert aws_error["passed"] is False
    assert "denied" in aws_error["error"]


def test_byok_ebs_volume_kms_key_verification(byok_module: ModuleType) -> None:
    """Encrypted EBS volume verification checks the reported KmsKeyId."""
    key_metadata = FakeByokKms().key_metadata

    success = byok_module._check_resource_encrypted_with_customer_key(FakeByokEc2(), key_metadata, "us-west-2a")
    mismatch = byok_module._check_resource_encrypted_with_customer_key(
        FakeByokEc2(kms_key_id="arn:aws:kms:us-west-2:123456789012:key/other"),
        key_metadata,
        "us-west-2a",
    )

    assert success["passed"] is True
    assert success["volume_id"] == "vol-byok-123"
    assert mismatch["passed"] is False
    assert "unexpected KMS key" in mismatch["error"]


def test_byok_deletes_volume_when_ebs_waiter_fails(byok_module: ModuleType) -> None:
    """EBS waiter failures preserve the volume id so final cleanup can delete it."""
    waiter_error = WaiterError(
        name="VolumeAvailable",
        reason="Max attempts exceeded",
        last_response={"Volumes": [{"VolumeId": "vol-byok-123", "State": "creating"}]},
    )
    kms = FakeByokKms()
    ec2 = FakeByokEc2(waiter_error=waiter_error)

    result = byok_module._run_customer_managed_key_test(kms, ec2, "us-west-2", "cmk-123")

    assert result["success"] is False
    assert result["encrypted_resource_id"] == "vol-byok-123"
    assert result["tests"]["resource_encrypted_with_customer_key"]["passed"] is False
    assert result["tests"]["resource_encrypted_with_customer_key"]["volume_id"] == "vol-byok-123"
    assert ec2.deleted_volumes == ["vol-byok-123"]


def test_byok_deletes_volume_when_ebs_verification_raises_unexpected_error(byok_module: ModuleType) -> None:
    """Unexpected EBS verification errors preserve the volume id for cleanup."""
    kms = FakeByokKms()
    ec2 = FakeByokEc2(describe_error=RuntimeError("describe failed"))

    result = byok_module._run_customer_managed_key_test(kms, ec2, "us-west-2", "cmk-123")

    assert result["success"] is False
    assert result["encrypted_resource_id"] == "vol-byok-123"
    assert result["tests"]["resource_encrypted_with_customer_key"]["volume_id"] == "vol-byok-123"
    assert "describe failed" in result["tests"]["resource_encrypted_with_customer_key"]["error"]
    assert ec2.deleted_volumes == ["vol-byok-123"]


def test_byok_owned_temporary_key_and_volume_are_cleaned_up(byok_module: ModuleType) -> None:
    """Temporary KMS keys are scheduled for deletion and test volumes are deleted."""
    kms = FakeByokKms()
    ec2 = FakeByokEc2()

    result = byok_module._run_customer_managed_key_test(kms, ec2, "us-west-2")

    assert result["success"] is True
    assert kms.created_keys
    assert kms.scheduled_deletions == [{"KeyId": "cmk-123", "PendingWindowInDays": 7}]
    assert ec2.deleted_volumes == ["vol-byok-123"]


@pytest.fixture(scope="module")
def oidc_module() -> ModuleType:
    """Load the OIDC user auth script as a module."""
    return _load_security_script("oidc_user_auth_test.py")


class FakeHttpResponse:
    """Small context-manager response for urllib-based OIDC tests."""

    def __init__(self, payload: dict[str, Any] | None = None, status_code: int = 200) -> None:
        """Store response payload and status code."""
        self.payload = payload or {}
        self.status_code = status_code

    def __enter__(self) -> FakeHttpResponse:
        """Enter response context."""
        return self

    def __exit__(self, *_args: Any) -> None:
        """Exit response context."""
        return None

    def read(self) -> bytes:
        """Return JSON response bytes."""
        return json.dumps(self.payload).encode("utf-8")

    def getcode(self) -> int:
        """Return HTTP status code."""
        return self.status_code


def _make_oidc_fixture(oidc_module: ModuleType) -> dict[str, Any]:
    """Build tokens, discovery metadata, and JWKS for OIDC script tests."""
    from cryptography.hazmat.primitives.asymmetric import rsa as _rsa

    issuer = "https://oidc.test.local/realms/isv"
    audience = "isv-validation"
    target_url = "https://platform.test.local/protected"
    private_key = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kid = "kid-1"
    jwks = {"keys": [oidc_module._public_jwk(private_key.public_key(), kid)]}
    discovery = {
        "issuer": issuer,
        "jwks_uri": f"{issuer}/protocol/openid-connect/certs",
        "response_types_supported": ["code", "id_token"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }
    now = int(oidc_module.time.time())
    base_claims = {
        "iss": issuer,
        "sub": "isv-test-user",
        "aud": audience,
        "iat": now,
        "exp": now + 600,
    }
    return {
        "issuer": issuer,
        "audience": audience,
        "target_url": target_url,
        "discovery": discovery,
        "jwks": jwks,
        "valid_token": oidc_module._sign_jwt(base_claims, private_key, kid),
        "invalid_tokens": {
            "wrong_issuer_rejected": oidc_module._sign_jwt({**base_claims, "iss": f"{issuer}-evil"}, private_key, kid),
            "wrong_audience_rejected": oidc_module._sign_jwt(
                {**base_claims, "aud": "wrong-audience"}, private_key, kid
            ),
            "expired_token_rejected": oidc_module._sign_jwt(
                {**base_claims, "iat": now - 120, "exp": now - 60}, private_key, kid
            ),
            "missing_required_claim_rejected": oidc_module._sign_jwt(
                base_claims, private_key, kid, drop_claims=("sub",)
            ),
        },
    }


def _patch_oidc_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    oidc_module: ModuleType,
    fixture: dict[str, Any],
) -> list[str]:
    """Patch urlopen so OIDC probes exercise fake remote HTTP endpoints."""
    seen_tokens: list[str] = []

    def fake_urlopen(request: Any, timeout: int = 0) -> FakeHttpResponse:
        """Serve discovery/JWKS and enforce target bearer-token behavior."""
        url = request.full_url
        if url == f"{fixture['issuer']}/.well-known/openid-configuration":
            return FakeHttpResponse(fixture["discovery"])
        if url == fixture["discovery"]["jwks_uri"]:
            return FakeHttpResponse(fixture["jwks"])
        if url == fixture["target_url"]:
            auth_header = request.get_header("Authorization", "")
            token = auth_header.removeprefix("Bearer ")
            seen_tokens.append(token)
            if token == fixture["valid_token"]:
                return FakeHttpResponse({}, status_code=200)
            raise HTTPError(url, 401, "Unauthorized", Message(), io.BytesIO(b""))
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(oidc_module, "urlopen", fake_urlopen)
    return seen_tokens


def test_oidc_run_probes_all_pass(oidc_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """All seven OIDC probes pass against configured remote endpoints."""
    fixture = _make_oidc_fixture(oidc_module)
    seen_tokens = _patch_oidc_urlopen(monkeypatch, oidc_module, fixture)

    probes = oidc_module.run_probes(
        fixture["issuer"],
        fixture["audience"],
        fixture["target_url"],
        fixture["valid_token"],
        fixture["invalid_tokens"],
    )

    expected = {
        "valid_token_accepted",
        "bad_signature_rejected",
        "wrong_issuer_rejected",
        "wrong_audience_rejected",
        "expired_token_rejected",
        "missing_required_claim_rejected",
        "discovery_and_jwks_reachable",
    }
    assert set(probes) == expected
    for name, probe in probes.items():
        assert probe["passed"], f"{name} did not pass: {probe}"
    assert len(seen_tokens) == 6


@pytest.mark.parametrize(
    ("jwks", "expected_error"),
    [
        ({"keys": {"kid": "kid-1"}}, "JWKS keys is not a list"),
        ({"keys": []}, "JWKS has no usable RSA keys"),
        ({"keys": ["not-a-key", {"kty": "EC", "kid": "kid-1"}]}, "JWKS has no usable RSA keys"),
        ({"keys": [{"kty": "RSA", "kid": "kid-1"}]}, "JWKS RSA key at index 0 missing required fields: n, e"),
    ],
)
def test_oidc_run_probes_fails_cleanly_for_malformed_jwks(
    oidc_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    jwks: dict[str, Any],
    expected_error: str,
) -> None:
    """Malformed JWKS discovery data fails the discovery probe without raising."""
    fixture = _make_oidc_fixture(oidc_module)
    fixture["jwks"] = jwks
    seen_tokens = _patch_oidc_urlopen(monkeypatch, oidc_module, fixture)

    probes = oidc_module.run_probes(
        fixture["issuer"],
        fixture["audience"],
        fixture["target_url"],
        fixture["valid_token"],
        fixture["invalid_tokens"],
    )

    assert probes["discovery_and_jwks_reachable"]["passed"] is False
    assert probes["discovery_and_jwks_reachable"]["error"] == expected_error
    assert len(seen_tokens) == 0


def test_oidc_run_probes_accepts_jwks_with_non_rsa_entries(
    oidc_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JWKS discovery accepts mixed key sets when at least one RSA key is usable."""
    fixture = _make_oidc_fixture(oidc_module)
    fixture["jwks"]["keys"] = [
        "not-a-key",
        {"kty": "EC", "kid": "kid-1"},
        *fixture["jwks"]["keys"],
    ]
    seen_tokens = _patch_oidc_urlopen(monkeypatch, oidc_module, fixture)

    probes = oidc_module.run_probes(
        fixture["issuer"],
        fixture["audience"],
        fixture["target_url"],
        fixture["valid_token"],
        fixture["invalid_tokens"],
    )

    assert probes["discovery_and_jwks_reachable"]["passed"] is True
    assert all(probe["passed"] for probe in probes.values())
    assert len(seen_tokens) == 6


def test_oidc_run_probes_rejects_malformed_rsa_jwks_even_with_valid_rsa(
    oidc_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed RSA JWKS entries fail discovery even when another RSA key is usable."""
    fixture = _make_oidc_fixture(oidc_module)
    fixture["jwks"]["keys"].append({"kty": "RSA", "kid": "kid-2", "n": "abc"})
    seen_tokens = _patch_oidc_urlopen(monkeypatch, oidc_module, fixture)

    probes = oidc_module.run_probes(
        fixture["issuer"],
        fixture["audience"],
        fixture["target_url"],
        fixture["valid_token"],
        fixture["invalid_tokens"],
    )

    assert probes["discovery_and_jwks_reachable"]["passed"] is False
    assert probes["discovery_and_jwks_reachable"]["error"] == "JWKS RSA key at index 1 missing required fields: e"
    assert len(seen_tokens) == 0


def test_oidc_run_probes_fails_cleanly_for_malformed_discovery_signing_algorithms(
    oidc_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed discovery algorithm metadata fails the discovery probe without raising."""
    fixture = _make_oidc_fixture(oidc_module)
    fixture["discovery"]["id_token_signing_alg_values_supported"] = 123
    seen_tokens = _patch_oidc_urlopen(monkeypatch, oidc_module, fixture)

    probes = oidc_module.run_probes(
        fixture["issuer"],
        fixture["audience"],
        fixture["target_url"],
        fixture["valid_token"],
        fixture["invalid_tokens"],
    )

    assert probes["discovery_and_jwks_reachable"]["passed"] is False
    assert (
        probes["discovery_and_jwks_reachable"]["error"]
        == "discovery id_token_signing_alg_values_supported is not a list"
    )
    assert len(seen_tokens) == 0


def test_oidc_run_probes_rejects_miswired_negative_fixture(
    oidc_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative fixtures must match the specific defect they claim to exercise."""
    fixture = _make_oidc_fixture(oidc_module)
    seen_tokens = _patch_oidc_urlopen(monkeypatch, oidc_module, fixture)
    invalid_tokens = dict(fixture["invalid_tokens"])
    invalid_tokens["wrong_audience_rejected"] = fixture["invalid_tokens"]["expired_token_rejected"]

    probes = oidc_module.run_probes(
        fixture["issuer"],
        fixture["audience"],
        fixture["target_url"],
        fixture["valid_token"],
        invalid_tokens,
    )

    assert probes["wrong_audience_rejected"]["passed"] is False
    assert "wrong_audience_rejected fixture invalid" in probes["wrong_audience_rejected"]["error"]
    assert "audience includes the expected audience" in probes["wrong_audience_rejected"]["error"]
    assert len(seen_tokens) == 5


@pytest.mark.parametrize(
    ("claim_overrides", "drop_claims", "expected_error"),
    [
        ({"iss": "https://issuer.example.com/evil"}, ("sub",), "token also has the wrong issuer"),
        ({"aud": "wrong-audience"}, ("sub",), "token also has the wrong audience"),
        ({"exp": 1_699_999_940}, ("sub",), "token is expired instead"),
        ({}, ("sub", "exp"), "missing required claim: exp"),
    ],
)
def test_oidc_missing_claim_fixture_rejects_additional_defects(
    oidc_module: ModuleType,
    claim_overrides: dict[str, Any],
    drop_claims: tuple[str, ...],
    expected_error: str,
) -> None:
    """Missing-claim fixtures fail locally when they also have unrelated defects."""
    from cryptography.hazmat.primitives.asymmetric import rsa as _rsa

    issuer = "https://issuer.example.com"
    audience = "isv-validation"
    now = 1_700_000_000
    private_key = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwks = {"keys": [oidc_module._public_jwk(private_key.public_key(), "kid-1")]}
    claims = {
        "iss": issuer,
        "sub": "isv-test-user",
        "aud": audience,
        "iat": now,
        "exp": now + 600,
        **claim_overrides,
    }
    token = oidc_module._sign_jwt(claims, private_key, "kid-1", drop_claims=drop_claims)

    fixture_error = oidc_module._validate_negative_fixture(
        "missing_required_claim_rejected",
        token,
        jwks,
        issuer,
        audience,
        now=now,
    )

    assert fixture_error == expected_error


def test_oidc_run_probes_rejects_malformed_negative_fixture(
    oidc_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed negative fixtures fail locally instead of counting as claim coverage."""
    fixture = _make_oidc_fixture(oidc_module)
    seen_tokens = _patch_oidc_urlopen(monkeypatch, oidc_module, fixture)
    invalid_tokens = dict(fixture["invalid_tokens"])
    invalid_tokens["wrong_issuer_rejected"] = "not-a-jwt"

    probes = oidc_module.run_probes(
        fixture["issuer"],
        fixture["audience"],
        fixture["target_url"],
        fixture["valid_token"],
        invalid_tokens,
    )

    assert probes["wrong_issuer_rejected"]["passed"] is False
    assert "wrong_issuer_rejected fixture invalid: malformed token" in probes["wrong_issuer_rejected"]["error"]
    assert len(seen_tokens) == 5


def test_oidc_run_probes_rejects_bad_signature_negative_fixture(
    oidc_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claim-specific negative fixtures must be validly signed before endpoint probing."""
    fixture = _make_oidc_fixture(oidc_module)
    seen_tokens = _patch_oidc_urlopen(monkeypatch, oidc_module, fixture)
    invalid_tokens = dict(fixture["invalid_tokens"])
    invalid_tokens["wrong_audience_rejected"] = oidc_module._tamper_signature(
        fixture["invalid_tokens"]["wrong_audience_rejected"]
    )

    probes = oidc_module.run_probes(
        fixture["issuer"],
        fixture["audience"],
        fixture["target_url"],
        fixture["valid_token"],
        invalid_tokens,
    )

    assert probes["wrong_audience_rejected"]["passed"] is False
    assert "wrong_audience_rejected fixture invalid" in probes["wrong_audience_rejected"]["error"]
    assert "token signature invalid: invalid signature" in probes["wrong_audience_rejected"]["error"]
    assert len(seen_tokens) == 5


def test_oidc_run_probes_fails_when_negative_fixture_missing(
    oidc_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing negative fixture still fails the corresponding probe."""
    fixture = _make_oidc_fixture(oidc_module)
    seen_tokens = _patch_oidc_urlopen(monkeypatch, oidc_module, fixture)
    invalid_tokens = dict(fixture["invalid_tokens"])
    invalid_tokens["expired_token_rejected"] = ""

    probes = oidc_module.run_probes(
        fixture["issuer"],
        fixture["audience"],
        fixture["target_url"],
        fixture["valid_token"],
        invalid_tokens,
    )

    assert probes["expired_token_rejected"] == {"passed": False, "error": "Token not configured"}
    assert len(seen_tokens) == 5


def test_oidc_main_emits_success_json(
    oidc_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """main() emits success=True only when real OIDC inputs are configured."""
    fixture = _make_oidc_fixture(oidc_module)
    _patch_oidc_urlopen(monkeypatch, oidc_module, fixture)
    monkeypatch.setenv("OIDC_VALID_TOKEN", fixture["valid_token"])
    monkeypatch.setenv("OIDC_WRONG_ISSUER_TOKEN", fixture["invalid_tokens"]["wrong_issuer_rejected"])
    monkeypatch.setenv("OIDC_WRONG_AUDIENCE_TOKEN", fixture["invalid_tokens"]["wrong_audience_rejected"])
    monkeypatch.setenv("OIDC_EXPIRED_TOKEN", fixture["invalid_tokens"]["expired_token_rejected"])
    monkeypatch.setenv(
        "OIDC_MISSING_REQUIRED_CLAIM_TOKEN",
        fixture["invalid_tokens"]["missing_required_claim_rejected"],
    )
    monkeypatch.setattr(
        oidc_module.sys,
        "argv",
        [
            "oidc_user_auth_test.py",
            "--region",
            "us-west-2",
            "--issuer-url",
            fixture["issuer"],
            "--audience",
            fixture["audience"],
            "--target-url",
            fixture["target_url"],
        ],
    )

    exit_code = oidc_module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["success"] is True
    assert payload["test_name"] == "oidc_user_auth_test"
    assert payload["platform"] == "security"
    assert payload["target_url"] == fixture["target_url"]
    assert len(payload["tests"]) == 7
    assert all(p["passed"] for p in payload["tests"].values())


def test_oidc_main_emits_skip_when_unconfigured(
    oidc_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """main() emits a structured skip (exit 0) when no OIDC inputs are provided."""
    for env_var in (
        "OIDC_ISSUER_URL",
        "OIDC_AUDIENCE",
        "OIDC_TARGET_URL",
        "OIDC_VALID_TOKEN",
    ):
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setattr(oidc_module.sys, "argv", ["oidc_user_auth_test.py", "--region", "us-west-2"])

    exit_code = oidc_module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["success"] is True
    assert payload["skipped"] is True
    assert payload["endpoints_tested"] == 0
    assert "OIDC validation not configured" in payload["skip_reason"]
    assert payload["tests"] == {}
    assert "error" not in payload


def test_oidc_main_emits_skip_with_inline_empty_config_args(
    oidc_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Inline empty args still produce a structured skip rather than a failure."""
    for env_var in (
        "OIDC_ISSUER_URL",
        "OIDC_AUDIENCE",
        "OIDC_TARGET_URL",
        "OIDC_VALID_TOKEN",
    ):
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setattr(
        oidc_module.sys,
        "argv",
        [
            "oidc_user_auth_test.py",
            "--region",
            "us-west-2",
            "--issuer-url=",
            "--audience=",
            "--target-url=",
        ],
    )

    exit_code = oidc_module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["success"] is True
    assert payload["skipped"] is True
    assert payload["endpoints_tested"] == 0
    assert "OIDC validation not configured" in payload["skip_reason"]
    assert payload["tests"] == {}


def test_oidc_main_skip_resets_endpoints_tested_when_only_target_url_set(
    oidc_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A partially-configured run still reports endpoints_tested=0 on skip."""
    for env_var in (
        "OIDC_ISSUER_URL",
        "OIDC_AUDIENCE",
        "OIDC_TARGET_URL",
        "OIDC_VALID_TOKEN",
    ):
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setattr(
        oidc_module.sys,
        "argv",
        [
            "oidc_user_auth_test.py",
            "--region",
            "us-west-2",
            "--target-url",
            "https://api.example/protected",
        ],
    )

    exit_code = oidc_module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["skipped"] is True
    assert payload["target_url"] == "https://api.example/protected"
    assert payload["endpoints_tested"] == 0


def test_oidc_main_fails_when_probes_fail(
    oidc_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """main() returns non-zero and success=False when probes regress."""
    failing = {
        name: {"passed": False, "error": "forced"}
        for name in (
            "valid_token_accepted",
            "bad_signature_rejected",
            "wrong_issuer_rejected",
            "wrong_audience_rejected",
            "expired_token_rejected",
            "missing_required_claim_rejected",
            "discovery_and_jwks_reachable",
        )
    }
    fixture = _make_oidc_fixture(oidc_module)
    monkeypatch.setattr(oidc_module, "run_probes", lambda *_a, **_kw: failing)
    monkeypatch.setenv("OIDC_VALID_TOKEN", fixture["valid_token"])
    monkeypatch.setattr(
        oidc_module.sys,
        "argv",
        [
            "oidc_user_auth_test.py",
            "--region",
            "us-west-2",
            "--issuer-url",
            fixture["issuer"],
            "--audience",
            fixture["audience"],
            "--target-url",
            fixture["target_url"],
        ],
    )

    exit_code = oidc_module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["success"] is False


def test_oidc_verify_rejects_alg_none(oidc_module: ModuleType) -> None:
    """Verifier must reject alg!=RS256 even when signature bytes are valid."""
    from cryptography.hazmat.primitives.asymmetric import rsa as _rsa

    private_key = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    jwks = {"keys": [oidc_module._public_jwk(public_key, "kid-1")]}

    header = {"alg": "none", "typ": "JWT", "kid": "kid-1"}
    payload = {
        "iss": "iss",
        "sub": "s",
        "aud": "aud",
        "iat": 0,
        "exp": 9999999999,
    }
    b64h = oidc_module._b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    b64p = oidc_module._b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    token = f"{b64h}.{b64p}."

    ok, detail = oidc_module._verify_jwt(token, jwks, "iss", "aud")
    assert not ok
    assert "alg" in detail


def test_oidc_verify_rejects_non_object_jwt_parts(oidc_module: ModuleType) -> None:
    """Verifier must report malformed JWT JSON parts instead of raising AttributeError."""
    valid_header = {"alg": "RS256", "typ": "JWT", "kid": "kid-1"}
    valid_payload = {
        "iss": "iss",
        "sub": "s",
        "aud": "aud",
        "iat": 0,
        "exp": 9999999999,
    }
    non_object_header = oidc_module._b64url_encode(json.dumps(["bad-header"]).encode())
    object_payload = oidc_module._b64url_encode(json.dumps(valid_payload).encode())
    object_header = oidc_module._b64url_encode(json.dumps(valid_header).encode())
    non_object_payload = oidc_module._b64url_encode(json.dumps(["bad-payload"]).encode())

    ok, detail = oidc_module._verify_jwt(f"{non_object_header}.{object_payload}.", {"keys": []}, "iss", "aud")
    assert not ok
    assert "JWT header is not an object: list" in detail

    ok, detail = oidc_module._verify_jwt(f"{object_header}.{non_object_payload}.", {"keys": []}, "iss", "aud")
    assert not ok
    assert "JWT payload is not an object: list" in detail


def test_oidc_verify_normalizes_base64_decode_errors(oidc_module: ModuleType) -> None:
    """Malformed base64url input must surface as verifier decode errors."""
    ok, detail = oidc_module._verify_jwt("a.b.c", {"keys": []}, "iss", "aud")

    assert not ok
    assert "decode error: invalid base64url data:" in detail


def test_oidc_verify_handles_aud_list(oidc_module: ModuleType) -> None:
    """Audience claim may be a list - membership counts as match."""
    from cryptography.hazmat.primitives.asymmetric import rsa as _rsa

    private_key = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwks = {"keys": [oidc_module._public_jwk(private_key.public_key(), "kid-1")]}
    now = 1_700_000_000
    claims = {
        "iss": "iss",
        "sub": "s",
        "aud": ["other", "isv-validation"],
        "iat": now,
        "exp": now + 600,
    }
    token = oidc_module._sign_jwt(claims, private_key, "kid-1")

    ok, _ = oidc_module._verify_jwt(token, jwks, "iss", "isv-validation", now=now)
    assert ok


# ===========================================================================
# Short-lived credentials (SEC02-01) tests
# ===========================================================================


@pytest.fixture(scope="module")
def short_lived_module() -> ModuleType:
    """Load the short-lived credentials script as a module."""
    return _load_security_script("short_lived_credentials_test.py")


class FakeShortLivedIam:
    """Fake IAM client tracking SEC02 user provisioning + cleanup calls."""

    def __init__(
        self,
        *,
        create_user_error: ClientError | None = None,
        put_user_policy_error: ClientError | None = None,
        create_access_key_error: ClientError | None = None,
        delete_user_policy_error: ClientError | None = None,
        delete_access_key_error: ClientError | None = None,
        delete_user_error: ClientError | None = None,
    ) -> None:
        """Configure optional per-call failures."""
        self.create_user_error = create_user_error
        self.put_user_policy_error = put_user_policy_error
        self.create_access_key_error = create_access_key_error
        self.delete_user_policy_error = delete_user_policy_error
        self.delete_access_key_error = delete_access_key_error
        self.delete_user_error = delete_user_error
        self.created_users: list[dict[str, Any]] = []
        self.put_policies: list[dict[str, str]] = []
        self.deleted_policies: list[tuple[str, str]] = []
        self.deleted_keys: list[tuple[str, str]] = []
        self.deleted_users: list[str] = []

    def create_user(self, UserName: str, Tags: list[dict[str, str]]) -> dict[str, dict[str, str]]:
        """Create a fake IAM user, recording the call."""
        if self.create_user_error is not None:
            raise self.create_user_error
        assert UserName.startswith("isv-sec02-test-")
        assert {"Key": "CreatedBy", "Value": "isvtest"} in Tags
        self.created_users.append({"UserName": UserName, "Tags": Tags})
        return {"User": {"UserName": UserName, "Arn": f"arn:aws:iam::123:user/{UserName}"}}

    def put_user_policy(self, UserName: str, PolicyName: str, PolicyDocument: str) -> None:
        """Attach a fake inline policy to the test user."""
        if self.put_user_policy_error is not None:
            raise self.put_user_policy_error
        assert UserName.startswith("isv-sec02-test-")
        self.put_policies.append({"UserName": UserName, "PolicyName": PolicyName, "PolicyDocument": PolicyDocument})

    def create_access_key(self, UserName: str) -> dict[str, dict[str, str]]:
        """Return fake access key material for the test user."""
        if self.create_access_key_error is not None:
            raise self.create_access_key_error
        assert UserName.startswith("isv-sec02-test-")
        return {"AccessKey": {"AccessKeyId": "AKIA_FAKE", "SecretAccessKey": "secret_fake"}}

    def delete_user_policy(self, UserName: str, PolicyName: str) -> None:
        """Detach the inline policy, recording the call."""
        if self.delete_user_policy_error is not None:
            raise self.delete_user_policy_error
        self.deleted_policies.append((UserName, PolicyName))

    def delete_access_key(self, UserName: str, AccessKeyId: str) -> None:
        """Delete the test user's access key, recording the call."""
        if self.delete_access_key_error is not None:
            raise self.delete_access_key_error
        self.deleted_keys.append((UserName, AccessKeyId))

    def delete_user(self, UserName: str) -> None:
        """Delete the test user, recording the call."""
        if self.delete_user_error is not None:
            raise self.delete_user_error
        self.deleted_users.append(UserName)


class FakeShortLivedSts:
    """Fake STS client supporting GetSessionToken / GetFederationToken with optional retry sequencing."""

    def __init__(
        self,
        *,
        session_expiration: Any = None,
        session_errors: list[ClientError] | None = None,
        federation_expiration: Any = None,
        federation_error: ClientError | None = None,
        omit_session_expiration: bool = False,
    ) -> None:
        """Configure fake STS responses, optional retry-error sequence on session, and per-probe expirations."""
        self.session_expiration = session_expiration
        self.session_errors: list[ClientError] = list(session_errors) if session_errors else []
        self.federation_expiration = federation_expiration
        self.federation_error = federation_error
        self.omit_session_expiration = omit_session_expiration
        self.federation_calls: list[dict[str, str]] = []
        self.session_call_count = 0

    def get_session_token(self) -> dict[str, dict[str, Any]]:
        """Return fake GetSessionToken response, popping a queued error on each call."""
        self.session_call_count += 1
        if self.session_errors:
            raise self.session_errors.pop(0)
        creds: dict[str, Any] = {
            "AccessKeyId": "ASIA_FAKE",
            "SecretAccessKey": "secret",
            "SessionToken": "session",
        }
        if not self.omit_session_expiration:
            creds["Expiration"] = self.session_expiration
        return {"Credentials": creds}

    def get_federation_token(self, **kwargs: Any) -> dict[str, dict[str, Any]]:
        """Return fake GetFederationToken response or raise the configured error."""
        self.federation_calls.append(kwargs)
        if self.federation_error is not None:
            raise self.federation_error
        return {
            "Credentials": {
                "AccessKeyId": "ASIA_FED_FAKE",
                "SecretAccessKey": "secret",
                "SessionToken": "session",
                "Expiration": self.federation_expiration,
            },
        }


def _patch_short_lived_clients(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    *,
    iam: FakeShortLivedIam,
    sts: FakeShortLivedSts,
) -> None:
    """Patch boto3.client to return fakes for iam and sts, and zero out the IAM-propagation sleep."""

    def fake_client(service_name: str, **kwargs: Any) -> FakeShortLivedIam | FakeShortLivedSts:
        """Return the matching fake client for iam/sts."""
        if service_name == "iam":
            return iam
        if service_name == "sts":
            return sts
        msg = f"unexpected service: {service_name}"
        raise AssertionError(msg)

    monkeypatch.setattr(module.boto3, "client", fake_client)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)


def _set_short_lived_argv(monkeypatch: pytest.MonkeyPatch, module: ModuleType, *extra_args: str) -> None:
    """Set sys.argv for the short-lived credentials script with optional extra args."""
    monkeypatch.setattr(
        module.sys,
        "argv",
        ["short_lived_credentials_test.py", "--region", "us-west-2", *extra_args],
    )


def test_short_lived_credentials_main_passes_with_bounded_ttls(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    short_lived_module: ModuleType,
) -> None:
    """Both probes pass when STS returns bounded TTLs, and the test user is cleaned up."""

    now = datetime.now(UTC)
    iam = FakeShortLivedIam()
    sts = FakeShortLivedSts(
        session_expiration=now + timedelta(seconds=3600),
        federation_expiration=now + timedelta(seconds=3600),
    )
    _patch_short_lived_clients(monkeypatch, short_lived_module, iam=iam, sts=sts)
    _set_short_lived_argv(monkeypatch, short_lived_module)

    exit_code = short_lived_module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["success"] is True
    assert payload["max_ttl_seconds"] == 43200
    assert payload["node_credential_method"] == "sts:GetSessionToken"
    assert payload["workload_credential_method"] == "sts:GetFederationToken"
    assert 0 < payload["node_credential_ttl_seconds"] <= 3600
    assert 0 < payload["workload_credential_ttl_seconds"] <= 3600
    for probe in payload["tests"].values():
        assert probe["passed"] is True

    assert len(iam.created_users) == 1
    username = iam.created_users[0]["UserName"]
    assert iam.put_policies == [
        {
            "UserName": username,
            "PolicyName": short_lived_module.INLINE_POLICY_NAME,
            "PolicyDocument": short_lived_module.INLINE_STS_POLICY,
        }
    ]
    assert iam.deleted_policies == [(username, short_lived_module.INLINE_POLICY_NAME)]
    assert iam.deleted_keys == [(username, "AKIA_FAKE")]
    assert iam.deleted_users == [username]
    assert len(sts.federation_calls) == 1
    federation_name = sts.federation_calls[0]["Name"]
    assert federation_name.startswith(short_lived_module.WORKLOAD_FEDERATION_PREFIX)
    # Federation Name shares the per-run uuid suffix with the IAM username
    # so CloudTrail events from the same probe correlate.
    assert federation_name.removeprefix(short_lived_module.WORKLOAD_FEDERATION_PREFIX) == username.removeprefix(
        short_lived_module.TEST_USER_PREFIX
    )
    assert sts.federation_calls[0]["Policy"] == short_lived_module.DENY_ALL_POLICY


def test_short_lived_credentials_main_fails_when_node_ttl_exceeds_bound(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    short_lived_module: ModuleType,
) -> None:
    """Node TTL above the configured bound fails the within-bound probe and still cleans up."""

    now = datetime.now(UTC)
    iam = FakeShortLivedIam()
    sts = FakeShortLivedSts(
        session_expiration=now + timedelta(seconds=7200),
        federation_expiration=now + timedelta(seconds=1800),
    )
    _patch_short_lived_clients(monkeypatch, short_lived_module, iam=iam, sts=sts)
    _set_short_lived_argv(monkeypatch, short_lived_module, "--max-ttl-seconds", "3600")

    exit_code = short_lived_module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["success"] is False
    assert payload["tests"]["node_credential_has_expiry"]["passed"] is True
    assert payload["tests"]["node_credential_ttl_within_bound"]["passed"] is False
    assert "outside" in payload["tests"]["node_credential_ttl_within_bound"]["error"]
    assert payload["tests"]["workload_credential_ttl_within_bound"]["passed"] is True
    assert iam.deleted_users  # cleanup still ran on probe failure


def test_short_lived_credentials_main_skips_when_create_user_denied(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    short_lived_module: ModuleType,
) -> None:
    """Orchestrator principal lacking iam:CreateUser yields a clean skip and never probes STS."""
    iam = FakeShortLivedIam(
        create_user_error=_client_error(
            "CreateUser", code="AccessDenied", message="not authorized to perform iam:CreateUser"
        ),
    )
    sts = FakeShortLivedSts()
    _patch_short_lived_clients(monkeypatch, short_lived_module, iam=iam, sts=sts)
    _set_short_lived_argv(monkeypatch, short_lived_module)

    exit_code = short_lived_module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["success"] is True
    assert payload["skipped"] is True
    # Skip reason includes the failing operation name, AWS error code, and
    # the underlying message so operators don't need to re-run with debug
    # to figure out what was denied.
    assert "CreateUser" in payload["skip_reason"]
    assert "AccessDenied" in payload["skip_reason"]
    assert "not authorized to perform iam:CreateUser" in payload["skip_reason"]
    assert payload["tests"] == {}
    assert sts.session_call_count == 0
    assert sts.federation_calls == []
    assert iam.deleted_users == []  # nothing was created


def test_short_lived_credentials_main_cleans_up_when_put_user_policy_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    short_lived_module: ModuleType,
) -> None:
    """Non-skippable failure on PutUserPolicy must still delete the user that was just created."""
    iam = FakeShortLivedIam(
        put_user_policy_error=_client_error("PutUserPolicy", code="LimitExceeded"),
    )
    sts = FakeShortLivedSts()
    _patch_short_lived_clients(monkeypatch, short_lived_module, iam=iam, sts=sts)
    _set_short_lived_argv(monkeypatch, short_lived_module)

    exit_code = short_lived_module.main()

    assert exit_code == 1
    assert len(iam.created_users) == 1
    username = iam.created_users[0]["UserName"]
    assert iam.deleted_users == [username]
    assert sts.session_call_count == 0  # no probes attempted
    assert sts.federation_calls == []


def test_short_lived_credentials_main_cleans_up_when_create_access_key_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    short_lived_module: ModuleType,
) -> None:
    """Non-skippable failure on CreateAccessKey must still detach inline policy and delete the user."""
    iam = FakeShortLivedIam(
        create_access_key_error=_client_error("CreateAccessKey", code="LimitExceeded"),
    )
    sts = FakeShortLivedSts()
    _patch_short_lived_clients(monkeypatch, short_lived_module, iam=iam, sts=sts)
    _set_short_lived_argv(monkeypatch, short_lived_module)

    exit_code = short_lived_module.main()

    assert exit_code == 1
    assert len(iam.created_users) == 1
    username = iam.created_users[0]["UserName"]
    assert iam.deleted_policies == [(username, short_lived_module.INLINE_POLICY_NAME)]
    assert iam.deleted_users == [username]
    assert sts.session_call_count == 0


def test_short_lived_credentials_main_skips_and_cleans_up_when_put_user_policy_denied(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    short_lived_module: ModuleType,
) -> None:
    """Skip path on a partial-setup AccessDenied still deletes the user and surfaces the AWS message."""
    iam = FakeShortLivedIam(
        put_user_policy_error=_client_error(
            "PutUserPolicy",
            code="AccessDenied",
            message="not authorized to perform iam:PutUserPolicy",
        ),
    )
    sts = FakeShortLivedSts()
    _patch_short_lived_clients(monkeypatch, short_lived_module, iam=iam, sts=sts)
    _set_short_lived_argv(monkeypatch, short_lived_module)

    exit_code = short_lived_module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["skipped"] is True
    assert "PutUserPolicy" in payload["skip_reason"]
    assert "AccessDenied" in payload["skip_reason"]
    assert "not authorized to perform iam:PutUserPolicy" in payload["skip_reason"]
    assert len(iam.created_users) == 1
    assert iam.deleted_users == [iam.created_users[0]["UserName"]]


def test_short_lived_credentials_main_fails_when_skip_path_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    short_lived_module: ModuleType,
) -> None:
    """Skip-eligible setup error + cleanup failure must surface as a hard failure, never a clean skip.

    Otherwise we'd report ``skipped: true`` while leaving an IAM user
    behind in the account.
    """
    iam = FakeShortLivedIam(
        put_user_policy_error=_client_error("PutUserPolicy", code="AccessDenied"),
        delete_user_error=_client_error("DeleteUser", code="ServiceUnavailable"),
    )
    sts = FakeShortLivedSts()
    _patch_short_lived_clients(monkeypatch, short_lived_module, iam=iam, sts=sts)
    _set_short_lived_argv(monkeypatch, short_lived_module)

    exit_code = short_lived_module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["success"] is False
    assert payload.get("skipped") is not True
    assert "cleanup_errors" in payload
    assert "setup failed" in payload["error"]
    assert "cleanup failed" in payload["error"]
    assert any("delete user" in err for err in payload["cleanup_errors"])


def test_short_lived_credentials_main_retries_node_probe_on_eventual_consistency(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    short_lived_module: ModuleType,
) -> None:
    """A burst of InvalidClientTokenId errors is retried until STS sees the new key."""

    now = datetime.now(UTC)
    iam = FakeShortLivedIam()
    sts = FakeShortLivedSts(
        session_expiration=now + timedelta(seconds=3600),
        federation_expiration=now + timedelta(seconds=3600),
        session_errors=[
            _client_error("GetSessionToken", code="InvalidClientTokenId"),
            _client_error("GetSessionToken", code="InvalidClientTokenId"),
        ],
    )
    _patch_short_lived_clients(monkeypatch, short_lived_module, iam=iam, sts=sts)
    _set_short_lived_argv(monkeypatch, short_lived_module)

    exit_code = short_lived_module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["success"] is True
    assert sts.session_call_count == 3  # two failures plus one success
    assert iam.deleted_users  # cleanup still ran


def test_short_lived_credentials_main_unhandled_node_error_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    short_lived_module: ModuleType,
) -> None:
    """Non-retryable STS errors on the node probe surface as a failure (not a skip)."""
    iam = FakeShortLivedIam()
    sts = FakeShortLivedSts(
        session_errors=[_client_error("GetSessionToken", code="ServiceUnavailable")],
    )
    _patch_short_lived_clients(monkeypatch, short_lived_module, iam=iam, sts=sts)
    _set_short_lived_argv(monkeypatch, short_lived_module)

    exit_code = short_lived_module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["success"] is False
    assert payload.get("skipped") is not True
    assert iam.deleted_users  # cleanup still ran via the decorator-caught path


def test_short_lived_credentials_main_records_workload_error_and_keeps_node_pass(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    short_lived_module: ModuleType,
) -> None:
    """A workload-side STS error is captured per-probe with op + code + message; node probe still passes."""

    now = datetime.now(UTC)
    iam = FakeShortLivedIam()
    sts = FakeShortLivedSts(
        session_expiration=now + timedelta(seconds=3600),
        federation_error=_client_error(
            "GetFederationToken",
            code="AccessDenied",
            message="not authorized to perform sts:GetFederationToken",
        ),
    )
    _patch_short_lived_clients(monkeypatch, short_lived_module, iam=iam, sts=sts)
    _set_short_lived_argv(monkeypatch, short_lived_module)

    exit_code = short_lived_module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["success"] is False
    assert payload.get("skipped") is not True
    assert payload["tests"]["node_credential_has_expiry"]["passed"] is True
    assert payload["tests"]["node_credential_ttl_within_bound"]["passed"] is True
    assert payload["tests"]["workload_credential_has_expiry"]["passed"] is False
    workload_error = payload["tests"]["workload_credential_has_expiry"]["error"]
    assert "GetFederationToken" in workload_error
    assert "AccessDenied" in workload_error
    assert "not authorized to perform sts:GetFederationToken" in workload_error
    assert iam.deleted_users  # cleanup ran


def test_short_lived_credentials_main_handles_missing_expiration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    short_lived_module: ModuleType,
) -> None:
    """Missing Credentials.Expiration in either response surfaces as a per-probe error."""

    now = datetime.now(UTC)
    iam = FakeShortLivedIam()
    sts = FakeShortLivedSts(
        omit_session_expiration=True,
        federation_expiration=now + timedelta(seconds=1800),
    )
    _patch_short_lived_clients(monkeypatch, short_lived_module, iam=iam, sts=sts)
    _set_short_lived_argv(monkeypatch, short_lived_module)

    exit_code = short_lived_module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["success"] is False
    assert payload["tests"]["node_credential_has_expiry"]["passed"] is False
    assert "Expiration missing" in payload["tests"]["node_credential_has_expiry"]["error"]
    assert payload["tests"]["workload_credential_has_expiry"]["passed"] is True


def test_short_lived_credentials_main_records_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    short_lived_module: ModuleType,
) -> None:
    """Successful probes are reported failed when IAM cleanup fails."""

    now = datetime.now(UTC)
    iam = FakeShortLivedIam(delete_user_error=_client_error("DeleteUser", code="ServiceUnavailable"))
    sts = FakeShortLivedSts(
        session_expiration=now + timedelta(seconds=3600),
        federation_expiration=now + timedelta(seconds=3600),
    )
    _patch_short_lived_clients(monkeypatch, short_lived_module, iam=iam, sts=sts)
    _set_short_lived_argv(monkeypatch, short_lived_module)

    exit_code = short_lived_module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["success"] is False
    assert "cleanup_errors" in payload
    assert any("delete user" in err for err in payload["cleanup_errors"])
    assert "Cleanup failed" in payload["error"]


def test_short_lived_credentials_main_skips_for_non_positive_max_ttl(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    short_lived_module: ModuleType,
) -> None:
    """A non-positive --max-ttl-seconds yields a clean skip rather than a hard fail or any AWS calls."""
    iam = FakeShortLivedIam()
    sts = FakeShortLivedSts()
    _patch_short_lived_clients(monkeypatch, short_lived_module, iam=iam, sts=sts)
    _set_short_lived_argv(monkeypatch, short_lived_module, "--max-ttl-seconds", "0")

    exit_code = short_lived_module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["skipped"] is True
    assert "positive integer" in payload["skip_reason"]
    assert iam.created_users == []
    assert sts.session_call_count == 0


def test_short_lived_credentials_cleanup_handles_partial_setup(
    short_lived_module: ModuleType,
) -> None:
    """_cleanup_test_user is no-op for an unset username and skips inline policy when user_created is False."""
    iam = FakeShortLivedIam()
    assert short_lived_module._cleanup_test_user(iam, None, None, False) == []
    assert short_lived_module._cleanup_test_user(iam, "isv-sec02-test-xyz", None, False) == []
    assert iam.deleted_policies == []
    assert iam.deleted_users == []


# ===========================================================================
# Tenant isolation (SEC11-01) tests
# ===========================================================================


@pytest.fixture(scope="module")
def tenant_isolation_module() -> ModuleType:
    """Load the tenant-isolation script as a module for direct helper testing."""
    return _load_security_script("tenant_isolation_test.py")


def _make_tenant(module: ModuleType, suffix: str, cidr: str) -> Any:
    """Build a populated ``Tenant`` instance for probe tests."""
    return module.Tenant(
        suffix=suffix,
        cidr=cidr,
        vpc_id=f"vpc-{suffix}",
        subnet_id=f"subnet-{suffix}",
        sg_id=f"sg-{suffix}",
        kms_key_id=f"key-{suffix}",
        kms_key_arn=f"arn:aws:kms:us-west-2:111122223333:key/{suffix}",
        s3_bucket=f"isv-sec11-test-{suffix}-abc123",
        instance_id=f"i-{suffix}",
        volume_id=f"vol-{suffix}",
    )


def test_tenant_isolation_scoped_policy_only_grants_own_arns(tenant_isolation_module: ModuleType) -> None:
    """The scoped policy must NOT reference the peer tenant's ARNs.

    This is the security-critical invariant: every cross-tenant deny in
    SEC11-01 hinges on tenant A's policy not granting any access to
    tenant B's resources.
    """
    a = _make_tenant(tenant_isolation_module, "aaaa1111", "10.94.0.0/24")
    b = _make_tenant(tenant_isolation_module, "bbbb2222", "10.95.0.0/24")

    policy = tenant_isolation_module._scoped_policy_document(a)

    assert a.kms_key_arn in policy
    assert a.s3_bucket in policy
    assert a.instance_id in policy
    assert a.volume_id in policy
    assert b.kms_key_arn not in policy
    assert b.s3_bucket not in policy
    assert b.instance_id not in policy
    assert b.volume_id not in policy


def test_tenant_isolation_classify_dry_run_buckets_codes(tenant_isolation_module: ModuleType) -> None:
    """``_classify_dry_run`` distinguishes denied / allowed / other AWS error codes."""
    denied = _client_error("StopInstances", code="UnauthorizedOperation")
    allowed = _client_error("StopInstances", code="DryRunOperation")
    other = _client_error("StopInstances", code="InvalidInstanceID.NotFound")

    assert tenant_isolation_module._classify_dry_run(denied) == "denied"
    assert tenant_isolation_module._classify_dry_run(allowed) == "allowed"
    assert tenant_isolation_module._classify_dry_run(other) == "other"


class FakeOrchestratorEc2:
    """Fake orchestrator-side EC2 client for ``_probe_network_isolation``."""

    def __init__(
        self,
        *,
        peerings: list[dict[str, Any]] | None = None,
        route_tables: list[dict[str, Any]] | None = None,
        tgw_attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        """Configure peering connections, route tables, and TGW attachments to return."""
        self.peerings = peerings or []
        self.route_tables = route_tables or []
        self.tgw_attachments = tgw_attachments or []

    def describe_vpc_peering_connections(self, **_kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        """Return configured peering connections."""
        return {"VpcPeeringConnections": self.peerings}

    def describe_route_tables(self, **_kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        """Return configured route tables."""
        return {"RouteTables": self.route_tables}

    def describe_transit_gateway_vpc_attachments(self, **_kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        """Return configured Transit Gateway VPC attachments."""
        return {"TransitGatewayVpcAttachments": self.tgw_attachments}


def test_probe_network_isolation_passes_when_no_peering_or_shared_route(
    tenant_isolation_module: ModuleType,
) -> None:
    """Pass when no peering connection and no route to tenant B's CIDR exists."""
    a = _make_tenant(tenant_isolation_module, "aaaa1111", "10.94.0.0/24")
    b = _make_tenant(tenant_isolation_module, "bbbb2222", "10.95.0.0/24")
    ec2 = FakeOrchestratorEc2(
        route_tables=[
            {
                "RouteTableId": "rtb-a",
                "Routes": [{"DestinationCidrBlock": "10.94.0.0/24", "GatewayId": "local"}],
            }
        ],
    )

    result = tenant_isolation_module._probe_network_isolation(ec2, a, b)

    assert result["passed"] is True
    assert "No peering" in result["message"]
    assert "transit gateway" in result["message"]


def test_probe_network_isolation_fails_when_shared_tgw_attachment_exists(
    tenant_isolation_module: ModuleType,
) -> None:
    """Fail when a single Transit Gateway is attached to BOTH tenant VPCs."""
    a = _make_tenant(tenant_isolation_module, "aaaa1111", "10.94.0.0/24")
    b = _make_tenant(tenant_isolation_module, "bbbb2222", "10.95.0.0/24")
    ec2 = FakeOrchestratorEc2(
        tgw_attachments=[
            {"TransitGatewayId": "tgw-leak", "VpcId": a.vpc_id, "State": "available"},
            {"TransitGatewayId": "tgw-leak", "VpcId": b.vpc_id, "State": "available"},
        ],
    )

    result = tenant_isolation_module._probe_network_isolation(ec2, a, b)

    assert result["passed"] is False
    assert "Transit Gateway tgw-leak" in result["error"]


def test_probe_network_isolation_passes_when_tgw_attached_to_only_one_vpc(
    tenant_isolation_module: ModuleType,
) -> None:
    """A TGW attached to only one tenant VPC is not a cross-tenant bridge."""
    a = _make_tenant(tenant_isolation_module, "aaaa1111", "10.94.0.0/24")
    b = _make_tenant(tenant_isolation_module, "bbbb2222", "10.95.0.0/24")
    ec2 = FakeOrchestratorEc2(
        tgw_attachments=[
            {"TransitGatewayId": "tgw-only-a", "VpcId": a.vpc_id, "State": "available"},
        ],
    )

    result = tenant_isolation_module._probe_network_isolation(ec2, a, b)

    assert result["passed"] is True


def test_probe_network_isolation_ignores_deleted_tgw_attachment(
    tenant_isolation_module: ModuleType,
) -> None:
    """A TGW attachment in 'deleted' state must not count as a live bridge."""
    a = _make_tenant(tenant_isolation_module, "aaaa1111", "10.94.0.0/24")
    b = _make_tenant(tenant_isolation_module, "bbbb2222", "10.95.0.0/24")
    ec2 = FakeOrchestratorEc2(
        tgw_attachments=[
            {"TransitGatewayId": "tgw-stale", "VpcId": a.vpc_id, "State": "deleted"},
            {"TransitGatewayId": "tgw-stale", "VpcId": b.vpc_id, "State": "deleted"},
        ],
    )

    result = tenant_isolation_module._probe_network_isolation(ec2, a, b)

    assert result["passed"] is True


def test_probe_network_isolation_fails_when_active_peering_exists(
    tenant_isolation_module: ModuleType,
) -> None:
    """Fail when an active VPC peering connection links A and B."""
    a = _make_tenant(tenant_isolation_module, "aaaa1111", "10.94.0.0/24")
    b = _make_tenant(tenant_isolation_module, "bbbb2222", "10.95.0.0/24")
    ec2 = FakeOrchestratorEc2(
        peerings=[{"VpcPeeringConnectionId": "pcx-1", "Status": {"Code": "active"}}],
    )

    result = tenant_isolation_module._probe_network_isolation(ec2, a, b)

    assert result["passed"] is False
    assert "VPC peering exists" in result["error"]


def test_probe_network_isolation_fails_when_route_to_b_cidr_present(
    tenant_isolation_module: ModuleType,
) -> None:
    """Fail when tenant A's route table has any route to tenant B's CIDR."""
    a = _make_tenant(tenant_isolation_module, "aaaa1111", "10.94.0.0/24")
    b = _make_tenant(tenant_isolation_module, "bbbb2222", "10.95.0.0/24")
    ec2 = FakeOrchestratorEc2(
        route_tables=[
            {
                "RouteTableId": "rtb-a",
                "Routes": [{"DestinationCidrBlock": "10.95.0.0/24", "GatewayId": "tgw-leak"}],
            }
        ],
    )

    result = tenant_isolation_module._probe_network_isolation(ec2, a, b)

    assert result["passed"] is False
    assert "Route to tenant B CIDR 10.95.0.0/24" in result["error"]


class FakeTenantClient:
    """Fake AWS service client that raises a configured error on every method."""

    def __init__(self, errors: dict[str, Exception | None]) -> None:
        """Map method-name -> ClientError (or None to silently succeed)."""
        self._errors = errors

    def __getattr__(self, name: str) -> Any:
        """Return a method that raises the configured error or returns ``{}``."""
        err = self._errors.get(name)

        def _call(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            if err is not None:
                raise err
            return {}

        return _call


def _denied_clients(tenant_isolation_module: ModuleType) -> dict[str, Any]:
    """Build per-service fake clients that all return AccessDenied / UnauthorizedOperation."""
    return {
        "ec2": FakeTenantClient(
            {
                "stop_instances": _client_error("StopInstances", code="UnauthorizedOperation"),
                "create_snapshot": _client_error("CreateSnapshot", code="UnauthorizedOperation"),
                "attach_volume": _client_error("AttachVolume", code="UnauthorizedOperation"),
            }
        ),
        "kms": FakeTenantClient({"encrypt": _client_error("Encrypt")}),
        "s3": FakeTenantClient({"get_object": _client_error("GetObject")}),
        "ssm": FakeTenantClient({"start_session": _client_error("StartSession")}),
        "sts": FakeTenantClient({}),
    }


def test_probe_data_isolation_passes_when_kms_and_s3_denied(tenant_isolation_module: ModuleType) -> None:
    """Pass when both kms:Encrypt and s3:GetObject return AccessDenied."""
    b = _make_tenant(tenant_isolation_module, "bbbb2222", "10.95.0.0/24")
    clients = _denied_clients(tenant_isolation_module)

    result = tenant_isolation_module._probe_data_isolation(clients, b)

    assert result["passed"] is True
    probe_names = {p["name"] for p in result["probes"]}
    assert probe_names == {"kms_encrypt_denied", "s3_get_object_denied"}


def test_probe_data_isolation_fails_when_kms_unexpectedly_succeeds(tenant_isolation_module: ModuleType) -> None:
    """Fail when kms:Encrypt returns no error (would mean tenant A could decrypt B's data)."""
    b = _make_tenant(tenant_isolation_module, "bbbb2222", "10.95.0.0/24")
    clients = _denied_clients(tenant_isolation_module)
    # Override kms to silently succeed (the disastrous case).
    clients["kms"] = FakeTenantClient({"encrypt": None})

    result = tenant_isolation_module._probe_data_isolation(clients, b)

    assert result["passed"] is False
    kms_probe = next(p for p in result["probes"] if p["name"] == "kms_encrypt_denied")
    assert kms_probe["passed"] is False


def test_probe_compute_isolation_passes_when_dryrun_denied_and_ssm_denied(
    tenant_isolation_module: ModuleType,
) -> None:
    """Pass when EC2 DryRun returns UnauthorizedOperation and SSM returns AccessDenied."""
    b = _make_tenant(tenant_isolation_module, "bbbb2222", "10.95.0.0/24")
    clients = _denied_clients(tenant_isolation_module)

    result = tenant_isolation_module._probe_compute_isolation(clients, b)

    assert result["passed"] is True
    assert {p["name"] for p in result["probes"]} == {"ec2_stop_instances_denied", "ssm_start_session_denied"}


def test_probe_compute_isolation_fails_when_dryrun_returns_dryrunoperation(
    tenant_isolation_module: ModuleType,
) -> None:
    """Fail when EC2 returns ``DryRunOperation`` (= IAM allowed the call)."""
    b = _make_tenant(tenant_isolation_module, "bbbb2222", "10.95.0.0/24")
    clients = _denied_clients(tenant_isolation_module)
    clients["ec2"] = FakeTenantClient(
        {
            "stop_instances": _client_error("StopInstances", code="DryRunOperation"),
            "create_snapshot": _client_error("CreateSnapshot", code="UnauthorizedOperation"),
            "attach_volume": _client_error("AttachVolume", code="UnauthorizedOperation"),
        }
    )

    result = tenant_isolation_module._probe_compute_isolation(clients, b)

    assert result["passed"] is False
    stop_probe = next(p for p in result["probes"] if p["name"] == "ec2_stop_instances_denied")
    assert stop_probe["code"] == "DryRunOperation"


def test_probe_storage_isolation_passes_when_dryrun_denies_snapshot_and_attach(
    tenant_isolation_module: ModuleType,
) -> None:
    """Pass when CreateSnapshot and AttachVolume DryRun calls both return UnauthorizedOperation."""
    a = _make_tenant(tenant_isolation_module, "aaaa1111", "10.94.0.0/24")
    b = _make_tenant(tenant_isolation_module, "bbbb2222", "10.95.0.0/24")
    clients = _denied_clients(tenant_isolation_module)

    result = tenant_isolation_module._probe_storage_isolation(clients, a, b)

    assert result["passed"] is True
    assert {p["name"] for p in result["probes"]} == {"ec2_create_snapshot_denied", "ec2_attach_volume_denied"}


def test_tenant_isolation_main_cleans_partial_provision_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tenant_isolation_module: ModuleType,
) -> None:
    """A setup failure after partial resource creation still reaches teardown."""

    class FakeUuid:
        """Deterministic UUID stand-in exposing the ``hex`` attribute used by the script."""

        def __init__(self, value: str) -> None:
            self.hex = value

    def fake_client(service_name: str, **_kwargs: Any) -> object:
        """Return an opaque fake client for each AWS service."""
        if service_name in {"ec2", "iam", "kms", "s3"}:
            return object()
        msg = f"unexpected service: {service_name}"
        raise AssertionError(msg)

    cleanup_calls: list[tuple[str, dict[str, bool], str]] = []
    suffixes = iter([FakeUuid("aaaa1111ffffffff"), FakeUuid("bbbb2222ffffffff")])

    def fake_provision_tenant(*, tenant: Any, **_kwargs: Any) -> Any:
        """Simulate a helper failing after creating a resource."""
        tenant.vpc_id = "vpc-partial"
        tenant.created["vpc"] = True
        raise _client_error("CreateUser", code="LimitExceeded", message="setup failed")

    def fake_teardown_tenant(*, tenant: Any, **_kwargs: Any) -> list[str]:
        """Record the tenant ledger passed to cleanup."""
        cleanup_calls.append((tenant.name, dict(tenant.created), tenant.vpc_id))
        return []

    monkeypatch.setattr(tenant_isolation_module.boto3, "client", fake_client)
    monkeypatch.setattr(tenant_isolation_module.uuid, "uuid4", lambda: next(suffixes))
    monkeypatch.setattr(tenant_isolation_module.sys, "argv", ["tenant_isolation_test.py", "--region", "us-west-2"])
    monkeypatch.setattr(tenant_isolation_module, "_get_amazon_linux_ami", lambda _ec2: "ami-test")
    monkeypatch.setattr(tenant_isolation_module, "_provision_tenant", fake_provision_tenant)
    monkeypatch.setattr(tenant_isolation_module, "_teardown_tenant", fake_teardown_tenant)

    exit_code = tenant_isolation_module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["success"] is False
    assert payload.get("skipped") is not True
    assert "setup failed" in payload["error"]
    assert cleanup_calls == [("isv-sec11-test-aaaa1111", {"vpc": True}, "vpc-partial")]


# --- teardown SEC11 sweep helpers ----------------------------------------


class FakeSec11Ec2:
    """Fake EC2 client backing the SEC11 teardown sweep helpers."""

    def __init__(
        self,
        *,
        instances: list[dict[str, Any]] | None = None,
        volumes: list[dict[str, Any]] | None = None,
        vpcs: list[dict[str, Any]] | None = None,
        subnets: list[dict[str, Any]] | None = None,
        sgs: list[dict[str, Any]] | None = None,
    ) -> None:
        """Configure resources returned by describe_*."""
        self.instances = instances or []
        self.volumes = volumes or []
        self.vpcs = vpcs or []
        self.subnets = subnets or []
        self.sgs = sgs or []
        self.terminated: list[str] = []
        self.deleted_volumes: list[str] = []
        self.deleted_vpcs: list[str] = []
        self.deleted_subnets: list[str] = []
        self.deleted_sgs: list[str] = []

    def get_paginator(self, operation_name: str) -> Any:
        """Return a fake paginator that yields a single page from the fixture data."""
        instances = self.instances
        volumes = self.volumes

        class _P:
            def paginate(_self, **_kwargs: Any) -> list[dict[str, Any]]:
                if operation_name == "describe_instances":
                    return [{"Reservations": [{"Instances": instances}]}]
                if operation_name == "describe_volumes":
                    return [{"Volumes": volumes}]
                msg = f"unexpected paginator: {operation_name}"
                raise AssertionError(msg)

        return _P()

    def terminate_instances(self, InstanceIds: list[str]) -> None:
        """Record terminated instance ids."""
        self.terminated.extend(InstanceIds)

    def get_waiter(self, _name: str) -> Any:
        """Return a no-op waiter."""

        class _W:
            def wait(self, **_kwargs: Any) -> None:
                return None

        return _W()

    def delete_volume(self, VolumeId: str) -> None:
        """Record deleted volume id."""
        self.deleted_volumes.append(VolumeId)

    def describe_vpcs(self, **_kwargs: Any) -> dict[str, Any]:
        """Return configured VPCs."""
        return {"Vpcs": self.vpcs}

    def describe_subnets(self, **_kwargs: Any) -> dict[str, Any]:
        """Return configured subnets."""
        return {"Subnets": self.subnets}

    def describe_security_groups(self, **_kwargs: Any) -> dict[str, Any]:
        """Return configured security groups."""
        return {"SecurityGroups": self.sgs}

    def delete_security_group(self, GroupId: str) -> None:
        """Record deleted SG id."""
        self.deleted_sgs.append(GroupId)

    def delete_subnet(self, SubnetId: str) -> None:
        """Record deleted subnet id."""
        self.deleted_subnets.append(SubnetId)

    def delete_vpc(self, VpcId: str) -> None:
        """Record deleted VPC id."""
        self.deleted_vpcs.append(VpcId)


def test_teardown_cleanup_sec11_instances_only_terminates_matching_prefix() -> None:
    """`_cleanup_sec11_instances` skips non-SEC11 instances and ignores terminated ones."""
    module = _load_security_script("teardown.py")
    ec2 = FakeSec11Ec2(
        instances=[
            {
                "InstanceId": "i-keep",
                "State": {"Name": "running"},
                "Tags": [{"Key": "Name", "Value": "production-app"}, {"Key": "CreatedBy", "Value": "isvtest"}],
            },
            {
                "InstanceId": "i-sec11-active",
                "State": {"Name": "running"},
                "Tags": [
                    {"Key": "Name", "Value": "isv-sec11-test-aaaa1111"},
                    {"Key": "CreatedBy", "Value": "isvtest"},
                ],
            },
            {
                "InstanceId": "i-sec11-already-gone",
                "State": {"Name": "terminated"},
                "Tags": [
                    {"Key": "Name", "Value": "isv-sec11-test-bbbb2222"},
                    {"Key": "CreatedBy", "Value": "isvtest"},
                ],
            },
        ]
    )

    errors = module._cleanup_sec11_instances(ec2)

    assert errors == []
    assert ec2.terminated == ["i-sec11-active"]


def test_teardown_cleanup_sec04_ec2_fixtures_by_owned_name_prefix() -> None:
    """The standalone teardown sweep must remove SEC04 EC2 fixtures leaked by a killed test."""
    module = _load_security_script("teardown.py")
    ec2 = FakeSec11Ec2(
        instances=[
            {
                "InstanceId": "i-sec04-active",
                "State": {"Name": "running"},
                "Tags": [
                    {"Key": "Name", "Value": "isv-sec04-test-aaaa1111"},
                    {"Key": "CreatedBy", "Value": "isvtest"},
                ],
            }
        ],
        vpcs=[
            {
                "VpcId": "vpc-sec04",
                "Tags": [
                    {"Key": "Name", "Value": "isv-sec04-test-aaaa1111"},
                    {"Key": "CreatedBy", "Value": "isvtest"},
                ],
            },
            {
                "VpcId": "vpc-keep",
                "Tags": [{"Key": "Name", "Value": "production"}, {"Key": "CreatedBy", "Value": "isvtest"}],
            },
        ],
        subnets=[{"SubnetId": "subnet-sec04"}],
        sgs=[{"GroupId": "sg-sec04", "GroupName": "isv-sec04-test-aaaa1111-sg"}],
    )

    instance_errors = module._cleanup_sec11_instances(ec2)
    vpc_errors = module._cleanup_sec11_vpcs(ec2)

    assert instance_errors == []
    assert vpc_errors == []
    assert ec2.terminated == ["i-sec04-active"]
    assert ec2.deleted_sgs == ["sg-sec04"]
    assert ec2.deleted_subnets == ["subnet-sec04"]
    assert ec2.deleted_vpcs == ["vpc-sec04"]


def test_teardown_cleanup_sec11_buckets_skips_untagged_buckets() -> None:
    """`_cleanup_sec11_buckets` deletes owned SEC04/SEC11 buckets and skips unowned buckets."""
    module = _load_security_script("teardown.py")
    deleted_buckets: list[str] = []

    class FakeS3:
        def list_buckets(self) -> dict[str, list[dict[str, str]]]:
            return {
                "Buckets": [
                    {"Name": "isv-sec04-test-aaaa1111-allowed"},  # tagged
                    {"Name": "isv-sec11-test-aaaa-abcdef"},  # tagged
                    {"Name": "isv-sec11-test-other-zzzzzz"},  # untagged -> skip
                    {"Name": "production-bucket"},  # wrong prefix -> skip
                ]
            }

        def get_bucket_tagging(self, Bucket: str) -> dict[str, list[dict[str, str]]]:
            if Bucket in {"isv-sec04-test-aaaa1111-allowed", "isv-sec11-test-aaaa-abcdef"}:
                return {"TagSet": [{"Key": "CreatedBy", "Value": "isvtest"}]}
            raise _client_error("GetBucketTagging", code="NoSuchTagSet")

        def get_paginator(self, _operation_name: str) -> Any:
            class _P:
                def paginate(_self, **_kwargs: Any) -> list[dict[str, Any]]:
                    return [{"Versions": [], "DeleteMarkers": []}]

            return _P()

        def delete_bucket(self, Bucket: str) -> None:
            deleted_buckets.append(Bucket)

    errors = module._cleanup_sec11_buckets(FakeS3())

    assert errors == []
    assert deleted_buckets == ["isv-sec04-test-aaaa1111-allowed", "isv-sec11-test-aaaa-abcdef"]


def test_teardown_cleanup_sec11_kms_deletes_alias_and_schedules_key_deletion() -> None:
    """`_cleanup_sec11_kms` deletes only ``alias/isv-sec11-test-*`` aliases and schedules their target keys."""
    module = _load_security_script("teardown.py")
    deleted_aliases: list[str] = []
    scheduled_keys: list[str] = []

    class FakeKms:
        def get_paginator(self, _operation_name: str) -> Any:
            class _P:
                def paginate(_self, **_kwargs: Any) -> list[dict[str, Any]]:
                    return [
                        {
                            "Aliases": [
                                {"AliasName": "alias/production-key", "TargetKeyId": "key-prod"},
                                {"AliasName": "alias/isv-sec11-test-aaaa1111", "TargetKeyId": "key-sec11-a"},
                                {"AliasName": "alias/isv-sec11-test-bbbb2222", "TargetKeyId": "key-sec11-b"},
                            ]
                        }
                    ]

            return _P()

        def delete_alias(self, AliasName: str) -> None:
            deleted_aliases.append(AliasName)

        def schedule_key_deletion(self, KeyId: str, PendingWindowInDays: int) -> None:
            assert PendingWindowInDays == 7
            scheduled_keys.append(KeyId)

    errors = module._cleanup_sec11_kms(FakeKms())

    assert errors == []
    assert deleted_aliases == [
        "alias/isv-sec11-test-aaaa1111",
        "alias/isv-sec11-test-bbbb2222",
    ]
    assert scheduled_keys == ["key-sec11-a", "key-sec11-b"]
