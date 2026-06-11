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

"""Tests for the AWS stubs' delete_with_retry helper.

The AWS stub scripts live outside the Python package tree and are invoked as
standalone subprocesses at runtime (sys.path manipulated in each script).
The common/errors.py module they share is imported here by path so it can be
unit-tested like normal Python.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError

_COMMON_DIR = Path(__file__).resolve().parents[1] / "configs" / "providers" / "aws" / "scripts"
if str(_COMMON_DIR) not in sys.path:
    sys.path.insert(0, str(_COMMON_DIR))

from common.errors import ALREADY_GONE_CODES, TRANSIENT_AWS_CODES, delete_with_retry  # noqa: E402


def _client_error(code: str, message: str = "error") -> ClientError:
    """Build a ClientError with a specific AWS Error.Code."""
    return ClientError({"Error": {"Code": code, "Message": message}}, "DeleteOp")


class TestDeleteWithRetry:
    """Verify delete_with_retry semantics for finally-block cleanup."""

    def test_succeeds_on_first_attempt(self) -> None:
        """Happy path: single successful call returns True."""
        fn = MagicMock(return_value=None)
        assert delete_with_retry(fn, VpcId="vpc-1", resource_desc="VPC vpc-1") is True
        assert fn.call_count == 1

    def test_already_gone_is_success(self) -> None:
        """InvalidVpcID.NotFound means end state reached - return True."""
        fn = MagicMock(side_effect=_client_error("InvalidVpcID.NotFound"))
        assert delete_with_retry(fn, VpcId="vpc-gone", resource_desc="VPC") is True
        assert fn.call_count == 1

    def test_already_gone_codes_cover_common_resources(self) -> None:
        """Sanity: the frozenset includes the codes that matter most."""
        must_have = {
            "InvalidVpcID.NotFound",
            "InvalidGroup.NotFound",
            "InvalidSubnet.NotFound",
            "InvalidRouteTableID.NotFound",
            "InvalidInstanceID.NotFound",
            "InvalidVolume.NotFound",
            "NoSuchEntity",
        }
        assert must_have <= ALREADY_GONE_CODES

    @patch("common.errors.time.sleep")
    def test_retries_transient_client_error_then_succeeds(self, sleep: MagicMock) -> None:
        """Throttling retries and succeeds on attempt 2 - orphan avoided."""
        fn = MagicMock(side_effect=[_client_error("Throttling"), None])
        assert delete_with_retry(fn, resource_desc="VPC", backoff_seconds=0.0) is True
        assert fn.call_count == 2
        # sleep IS called even with backoff=0 (linear: 0.0 * 1 = 0.0)
        assert sleep.called

    @patch("common.errors.time.sleep")
    def test_exhausts_attempts_on_persistent_transient(self, sleep: MagicMock) -> None:
        """If all attempts hit transient errors, give up and return False."""
        fn = MagicMock(side_effect=_client_error("ServiceUnavailable"))
        assert delete_with_retry(fn, resource_desc="VPC", attempts=3, backoff_seconds=0.0) is False
        assert fn.call_count == 3

    @patch("common.errors.time.sleep")
    def test_retries_botocore_network_error(self, sleep: MagicMock) -> None:
        """RemoteDisconnected-class errors (BotoCoreError) retry just like
        transient client errors."""
        fn = MagicMock(
            side_effect=[
                EndpointConnectionError(endpoint_url="https://ec2.us-west-2.amazonaws.com/"),
                None,
            ]
        )
        assert delete_with_retry(fn, resource_desc="VPC", backoff_seconds=0.0) is True
        assert fn.call_count == 2

    @patch("common.errors.time.sleep")
    def test_gives_up_on_persistent_botocore_error(self, sleep: MagicMock) -> None:
        """All attempts fail with BotoCoreError → False, not a raised exception."""

        class _PersistentBotoError(BotoCoreError):
            """Synthetic persistent botocore error used to exercise retry exhaustion."""

            fmt = "persistent network failure"

        fn = MagicMock(side_effect=_PersistentBotoError())
        assert delete_with_retry(fn, resource_desc="VPC", attempts=2, backoff_seconds=0.0) is False
        assert fn.call_count == 2

    def test_non_transient_client_error_no_retry(self) -> None:
        """Non-transient codes (e.g. DependencyViolation) return False
        immediately - no point spinning, and the caller's finally block
        already accepts best-effort semantics."""
        fn = MagicMock(side_effect=_client_error("DependencyViolation"))
        assert delete_with_retry(fn, resource_desc="VPC", attempts=3, backoff_seconds=0.0) is False
        assert fn.call_count == 1

    def test_never_raises_even_on_unknown_error(self) -> None:
        """Contract: delete_with_retry MUST NOT propagate any exception,
        so finally blocks stay well-formed. Covers ClientError, BotoCoreError,
        and bare Exception (caught as final fallback)."""
        fn = MagicMock(side_effect=_client_error("UnknownWeirdCode"))
        # Non-transient, non-already-gone → logged and False, not raised.
        assert delete_with_retry(fn, resource_desc="VPC", attempts=1) is False

    def test_forwards_args_and_kwargs(self) -> None:
        """Positional + keyword args flow through to the bound method."""
        fn = MagicMock(return_value=None)
        delete_with_retry(fn, "positional-arg", VpcId="vpc-1", extra="value")
        fn.assert_called_once_with("positional-arg", VpcId="vpc-1", extra="value")

    def test_transient_codes_cover_common_aws_throttles(self) -> None:
        """Sanity: the frozenset includes the codes boto3 commonly surfaces."""
        must_have = {"RequestLimitExceeded", "Throttling", "ServiceUnavailable"}
        assert must_have <= TRANSIENT_AWS_CODES


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
