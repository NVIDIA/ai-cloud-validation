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

"""Tests for AWS common/ec2.py verified-reuse guards.

``create_key_pair`` and ``create_security_group`` previously accepted any
existing-by-name resource as a success without verifying the shape matched
what the caller asked for. These tests verify that reuse is now gated on
the suite's ownership tag and the documented invariants (description /
SSH ingress rule).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

_COMMON_DIR = Path(__file__).resolve().parents[3] / "configs" / "providers" / "aws" / "scripts"
if str(_COMMON_DIR) not in sys.path:
    sys.path.insert(0, str(_COMMON_DIR))

from common.ec2 import (  # noqa: E402
    create_key_pair,
    create_security_group,
    sanitize_key_name,
    wait_for_public_ip,
)


def _client_error(code: str, message: str = "error") -> ClientError:
    """Build a ClientError with a specific AWS Error.Code."""
    return ClientError({"Error": {"Code": code, "Message": message}}, "op")


_ISV_TAGS = [{"Key": "Name", "Value": "k1"}, {"Key": "CreatedBy", "Value": "isvtest"}]
_OTHER_TAGS = [{"Key": "Name", "Value": "k1"}, {"Key": "CreatedBy", "Value": "someone-else"}]
_NO_TAGS: list[dict[str, str]] = []


class TestCreateKeyPairVerifiedReuse:
    """create_key_pair must refuse to adopt keys it didn't create."""

    def test_reuses_existing_key_when_tagged_and_file_present(self, tmp_path: Path) -> None:
        """Verified reuse: AWS key has suite tag + local PEM exists."""
        key_file = tmp_path / "k1.pem"
        key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nstub\n-----END RSA PRIVATE KEY-----")
        ec2 = MagicMock()
        ec2.describe_key_pairs.return_value = {"KeyPairs": [{"KeyName": "k1", "Tags": _ISV_TAGS}]}

        result = create_key_pair(ec2, "k1", key_dir=tmp_path)

        assert result == str(key_file)
        ec2.create_key_pair.assert_not_called()
        ec2.delete_key_pair.assert_not_called()

    def test_raises_when_existing_key_missing_isv_tag(self, tmp_path: Path) -> None:
        """Refuse to adopt a key some other caller created."""
        (tmp_path / "k1.pem").write_text("stub")
        ec2 = MagicMock()
        ec2.describe_key_pairs.return_value = {"KeyPairs": [{"KeyName": "k1", "Tags": _OTHER_TAGS}]}

        with pytest.raises(RuntimeError, match="not tagged CreatedBy=isvtest"):
            create_key_pair(ec2, "k1", key_dir=tmp_path)
        ec2.create_key_pair.assert_not_called()
        ec2.delete_key_pair.assert_not_called()

    def test_raises_when_existing_key_has_no_tags(self, tmp_path: Path) -> None:
        """Missing tag block is treated the same as wrong tag."""
        (tmp_path / "k1.pem").write_text("stub")
        ec2 = MagicMock()
        ec2.describe_key_pairs.return_value = {"KeyPairs": [{"KeyName": "k1", "Tags": _NO_TAGS}]}

        with pytest.raises(RuntimeError, match="not tagged"):
            create_key_pair(ec2, "k1", key_dir=tmp_path)

    def test_recreates_when_tagged_but_local_file_missing(self, tmp_path: Path) -> None:
        """Tag matches but PEM missing - delete and recreate (ours, unrecoverable)."""
        ec2 = MagicMock()
        ec2.describe_key_pairs.return_value = {"KeyPairs": [{"KeyName": "k1", "Tags": _ISV_TAGS}]}
        ec2.create_key_pair.return_value = {
            "KeyMaterial": "-----BEGIN RSA PRIVATE KEY-----\nnew\n-----END RSA PRIVATE KEY-----"
        }

        result = create_key_pair(ec2, "k1", key_dir=tmp_path)

        ec2.delete_key_pair.assert_called_once_with(KeyName="k1")
        ec2.create_key_pair.assert_called_once()
        assert Path(result).exists()

    def test_creates_fresh_when_no_existing_key(self, tmp_path: Path) -> None:
        """No prior key on AWS → normal create path."""
        ec2 = MagicMock()
        ec2.describe_key_pairs.side_effect = _client_error("InvalidKeyPair.NotFound")
        ec2.create_key_pair.return_value = {
            "KeyMaterial": "-----BEGIN RSA PRIVATE KEY-----\nfresh\n-----END RSA PRIVATE KEY-----"
        }

        result = create_key_pair(ec2, "k1", key_dir=tmp_path)

        ec2.create_key_pair.assert_called_once()
        assert Path(result).exists()


_EXPECTED_DESC = "ISV validation security group"
_EXPECTED_INGRESS = [
    {
        "IpProtocol": "tcp",
        "FromPort": 22,
        "ToPort": 22,
        "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSH"}],
    }
]


class TestCreateSecurityGroupVerifiedReuse:
    """create_security_group must refuse to adopt SGs with the wrong shape."""

    def test_creates_fresh_when_no_duplicate(self) -> None:
        """No existing SG → normal create path."""
        ec2 = MagicMock()
        ec2.create_security_group.return_value = {"GroupId": "sg-new"}

        assert create_security_group(ec2, "vpc-1", "sg1") == "sg-new"
        ec2.authorize_security_group_ingress.assert_called_once()

    def test_reuses_verified_existing_sg(self) -> None:
        """Verified reuse: tag + description + SSH rule all match."""
        ec2 = MagicMock()
        ec2.create_security_group.side_effect = _client_error("InvalidGroup.Duplicate")
        ec2.describe_security_groups.return_value = {
            "SecurityGroups": [
                {
                    "GroupId": "sg-existing",
                    "Description": _EXPECTED_DESC,
                    "Tags": _ISV_TAGS,
                    "IpPermissions": _EXPECTED_INGRESS,
                }
            ]
        }

        assert create_security_group(ec2, "vpc-1", "sg1") == "sg-existing"
        # Did NOT authorize ingress on reuse - the rule already exists.
        ec2.authorize_security_group_ingress.assert_not_called()

    def test_raises_on_duplicate_missing_isv_tag(self) -> None:
        """Reject reuse when the SG lacks the suite's ownership tag."""
        ec2 = MagicMock()
        ec2.create_security_group.side_effect = _client_error("InvalidGroup.Duplicate")
        ec2.describe_security_groups.return_value = {
            "SecurityGroups": [
                {
                    "GroupId": "sg-foreign",
                    "Description": _EXPECTED_DESC,
                    "Tags": _OTHER_TAGS,
                    "IpPermissions": _EXPECTED_INGRESS,
                }
            ]
        }
        with pytest.raises(RuntimeError, match="not tagged CreatedBy=isvtest"):
            create_security_group(ec2, "vpc-1", "sg1")

    def test_raises_on_duplicate_with_wrong_description(self) -> None:
        """Reject reuse when the description doesn't match the caller's."""
        ec2 = MagicMock()
        ec2.create_security_group.side_effect = _client_error("InvalidGroup.Duplicate")
        ec2.describe_security_groups.return_value = {
            "SecurityGroups": [
                {
                    "GroupId": "sg-desc-mismatch",
                    "Description": "something else",
                    "Tags": _ISV_TAGS,
                    "IpPermissions": _EXPECTED_INGRESS,
                }
            ]
        }
        with pytest.raises(RuntimeError, match="description differs"):
            create_security_group(ec2, "vpc-1", "sg1", description=_EXPECTED_DESC)

    def test_raises_on_duplicate_missing_ssh_rule(self) -> None:
        """Reject reuse when the required SSH ingress rule is absent."""
        ec2 = MagicMock()
        ec2.create_security_group.side_effect = _client_error("InvalidGroup.Duplicate")
        ec2.describe_security_groups.return_value = {
            "SecurityGroups": [
                {
                    "GroupId": "sg-no-ssh",
                    "Description": _EXPECTED_DESC,
                    "Tags": _ISV_TAGS,
                    "IpPermissions": [
                        {
                            "IpProtocol": "tcp",
                            "FromPort": 443,
                            "ToPort": 443,
                            "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                        }
                    ],
                }
            ]
        }
        with pytest.raises(RuntimeError, match="missing the required SSH ingress"):
            create_security_group(ec2, "vpc-1", "sg1")

    def test_raises_on_duplicate_but_describe_empty(self) -> None:
        """API race: duplicate error but describe returns nothing - raise the
        original ClientError rather than swallow silently."""
        ec2 = MagicMock()
        ec2.create_security_group.side_effect = _client_error("InvalidGroup.Duplicate")
        ec2.describe_security_groups.return_value = {"SecurityGroups": []}
        with pytest.raises(ClientError):
            create_security_group(ec2, "vpc-1", "sg1")

    def test_propagates_non_duplicate_client_error(self) -> None:
        """Non-duplicate errors propagate unchanged."""
        ec2 = MagicMock()
        ec2.create_security_group.side_effect = _client_error("AccessDenied")
        with pytest.raises(ClientError):
            create_security_group(ec2, "vpc-1", "sg1")


class TestSanitizeKeyName:
    """sanitize_key_name - prevent path traversal when
    ``key_name`` is composed into a filesystem path like /tmp/<name>.pem."""

    @pytest.mark.parametrize(
        "name",
        [
            "isv-test",
            "isv_test",
            "isv-test-v1.2",
            "a",
            "A1B2C3",
            "x" * 255,
        ],
    )
    def test_accepts_safe_names(self, name: str) -> None:
        """Letters, digits, dot, dash, underscore are all allowed."""
        assert sanitize_key_name(name) == name

    @pytest.mark.parametrize(
        "name",
        [
            "../etc/passwd",
            "foo/bar",
            "foo bar",
            "name;rm -rf /",
            "name$(whoami)",
            "",
            "x" * 256,  # over the length cap
            "foo\nbar",
        ],
    )
    def test_rejects_unsafe_names(self, name: str) -> None:
        """Anything outside [A-Za-z0-9_.-] or over length is rejected."""
        with pytest.raises(ValueError, match="invalid key name"):
            sanitize_key_name(name)


class TestWaitForPublicIp:
    """wait_for_public_ip - always poll describe_instances,
    never fall back to a pre-stop value."""

    def test_returns_fresh_ip_on_first_poll(self) -> None:
        """Happy path: describe immediately returns a PublicIpAddress."""
        ec2 = MagicMock()
        ec2.describe_instances.return_value = {"Reservations": [{"Instances": [{"PublicIpAddress": "34.1.2.3"}]}]}
        assert wait_for_public_ip(ec2, "i-1", timeout=1, interval=0) == "34.1.2.3"

    def test_polls_until_non_null(self) -> None:
        """First poll returns null, second returns the new IP."""
        ec2 = MagicMock()
        ec2.describe_instances.side_effect = [
            {"Reservations": [{"Instances": [{"PublicIpAddress": None}]}]},
            {"Reservations": [{"Instances": [{"PublicIpAddress": "34.9.9.9"}]}]},
        ]
        assert wait_for_public_ip(ec2, "i-1", timeout=10, interval=0) == "34.9.9.9"
        assert ec2.describe_instances.call_count == 2

    def test_returns_none_on_timeout(self) -> None:
        """No IP after the deadline → return None (caller decides next move)."""
        ec2 = MagicMock()
        ec2.describe_instances.return_value = {"Reservations": [{"Instances": [{"PublicIpAddress": None}]}]}
        assert wait_for_public_ip(ec2, "i-1", timeout=0, interval=0) is None

    def test_tolerates_transient_describe_errors(self) -> None:
        """A ClientError on one poll doesn't abort the loop."""
        ec2 = MagicMock()
        ec2.describe_instances.side_effect = [
            _client_error("Throttling"),
            {"Reservations": [{"Instances": [{"PublicIpAddress": "34.5.5.5"}]}]},
        ]
        assert wait_for_public_ip(ec2, "i-1", timeout=10, interval=0) == "34.5.5.5"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
