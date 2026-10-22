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

"""AWS evidence collection and scaffold tests for allow-rule approval."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber
from isvtest.validations import AllowRuleApprovalCheck

from isvctl.config.output_schemas import validate_output

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "configs/providers/aws/scripts/network/allow_rule_approval_test.py"
TEMPLATE = ROOT / "configs/providers/my-isv/scripts/network/allow_rule_approval_test.py"
RULE_ID = "sgr-0123456789abcdef0"
GROUP_ID = "sg-0123456789abcdef0"
POLICY = f"aws:us-east-2:123456789012:security-group/{GROUP_ID}"


@pytest.fixture
def collector() -> ModuleType:
    """Load the actual script without running its command-line entry point."""
    spec = importlib.util.spec_from_file_location("allow_rule_collector", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def aws_rule(**updates: Any) -> dict[str, Any]:
    """Return a real-shaped AWS rule with explicit ownership metadata."""
    rule = {
        "SecurityGroupRuleId": RULE_ID,
        "GroupId": GROUP_ID,
        "GroupOwnerId": "123456789012",
        "IsEgress": False,
        "IpProtocol": "6",
        "FromPort": 443,
        "ToPort": 443,
        "CidrIpv4": "192.0.2.0/24",
        "Tags": [{"Key": "Owner", "Value": "network-team"}],
    }
    rule.update(updates)
    return rule


def stub_rule(stub: Stubber, rule: dict[str, Any], active: bool = True, paginated: bool = False) -> None:
    """Stub read-only AWS calls and require exact rule and policy selection."""
    stub.add_response(
        "describe_security_group_rules", {"SecurityGroupRules": [rule]}, {"SecurityGroupRuleIds": [RULE_ID]}
    )
    filters = {"Filters": [{"Name": "group-id", "Values": [GROUP_ID]}, {"Name": "status", "Values": ["in-use"]}]}
    if paginated:
        stub.add_response("describe_network_interfaces", {"NetworkInterfaces": [], "NextToken": "next"}, filters)
        filters = {**filters, "NextToken": "next"}
    stub.add_response(
        "describe_network_interfaces",
        {
            "NetworkInterfaces": [
                {"NetworkInterfaceId": "eni-0123456789abcdef0", "Status": "in-use", "Groups": [{"GroupId": GROUP_ID}]}
            ]
            if active
            else []
        },
        filters,
    )


def ec2_client() -> Any:
    """Build a client with dummy credentials; Stubber prevents network requests."""
    return boto3.client("ec2", region_name="us-east-2", aws_access_key_id="testing", aws_secret_access_key="testing")


@pytest.mark.parametrize("egress", [False, True])
def test_live_scope_direction_and_pagination(collector: ModuleType, egress: bool) -> None:
    """Normalize every traffic dimension and find attached policies beyond the first page."""
    client = ec2_client()
    with Stubber(client) as stub:
        stub_rule(stub, aws_rule(IsEgress=egress), paginated=True)
        result = collector.collect_rule(client, RULE_ID, "us-east-2", "Owner")
        stub.assert_no_pending_responses()
    assert result == {
        "rule_id": RULE_ID,
        "policy_id": POLICY,
        "owner": "network-team",
        "effective": True,
        "action": "allow",
        "scope": {
            "direction": "egress" if egress else "ingress",
            "protocol": "tcp",
            "port_range": [443, 443],
            "source": POLICY if egress else "cidr:192.0.2.0/24",
            "destination": "cidr:192.0.2.0/24" if egress else POLICY,
        },
    }


def test_unattached_rule_rejected(collector: ModuleType) -> None:
    """A standalone security-group definition does not establish an effective rule."""
    client = ec2_client()
    with Stubber(client) as stub:
        stub_rule(stub, aws_rule(), active=False)
        with pytest.raises(ValueError, match="in-use"):
            collector.collect_rule(client, RULE_ID, "us-east-2", "Owner")


def test_missing_owner_is_not_inferred(collector: ModuleType) -> None:
    """Neither descriptions nor an approval export supplies a missing live-rule owner."""
    client = ec2_client()
    with Stubber(client) as stub:
        stub_rule(stub, aws_rule(Tags=[], Description="Owner: network-team"))
        assert collector.collect_rule(client, RULE_ID, "us-east-2", "Owner")["owner"] == ""


def test_prefix_list_membership_not_assumed(collector: ModuleType) -> None:
    """A mutable prefix-list identifier cannot stand in for approved address scope."""
    client = ec2_client()
    rule = aws_rule(PrefixListId="pl-0123456789abcdef0")
    del rule["CidrIpv4"]
    with Stubber(client) as stub:
        stub_rule(stub, rule)
        with pytest.raises(ValueError, match="membership"):
            collector.collect_rule(client, RULE_ID, "us-east-2", "Owner")


def test_cli_preserves_unapproved_record(
    collector: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Successful collection must not convert a pending record to an approval."""
    approval = {"record_id": "CHANGE-1", "status": "pending"}
    file = tmp_path / "approval.json"
    file.write_text(json.dumps(approval))
    client = ec2_client()
    monkeypatch.setattr(collector.boto3, "client", lambda *a, **kw: client)
    monkeypatch.setattr(
        sys, "argv", [str(SCRIPT), "--region", "us-east-2", "--rule-id", RULE_ID, "--approval-file", str(file)]
    )
    with Stubber(client) as stub:
        stub_rule(stub, aws_rule())
        assert collector.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["approval"] == approval
    assert validate_output(output, "allow_rule_approval")[0]
    assert AllowRuleApprovalCheck(config={"step_output": output}).execute()["passed"] is False


def test_cli_missing_configuration_fails(
    collector: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing approval source cannot result in a passing or skipped collection."""
    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])
    assert collector.main() == 1
    output = json.loads(capsys.readouterr().out)
    assert output["success"] is False
    assert "--approval-file" in output["error"]


@pytest.mark.parametrize("demo", ["0", "1"])
def test_template_requires_real_implementation(demo: str) -> None:
    """Synthetic approvals are available only in explicit demo mode."""
    result = subprocess.run(
        [sys.executable, str(TEMPLATE)],
        capture_output=True,
        text=True,
        env={**os.environ, "ISVCTL_DEMO_MODE": demo},
        check=False,
    )
    output = json.loads(result.stdout)
    assert result.returncode == (0 if demo == "1" else 1)
    assert AllowRuleApprovalCheck(config={"step_output": output}).execute()["passed"] is (demo == "1")
