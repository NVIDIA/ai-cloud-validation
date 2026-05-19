# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Shared AWS error classification utilities.

Provides consistent error type classification for all AWS scripts.
"""

import functools
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    NoCredentialsError,
    ProfileNotFound,
    TokenRetrievalError,
)

logger = logging.getLogger(__name__)

# AWS error codes that indicate the resource is already gone - treat as success
# in cleanup paths since the desired end state (resource absent) is reached.
ALREADY_GONE_CODES: frozenset[str] = frozenset(
    {
        "InvalidVpcID.NotFound",
        "InvalidGroup.NotFound",
        "InvalidGroupId.NotFound",
        "InvalidSubnet.NotFound",
        "InvalidSubnetID.NotFound",
        "InvalidRouteTableID.NotFound",
        "InvalidInternetGatewayID.NotFound",
        "InvalidInstanceID.NotFound",
        "InvalidNetworkAclID.NotFound",
        "InvalidVpcPeeringConnectionID.NotFound",
        "InvalidAllocationID.NotFound",
        "InvalidAssociationID.NotFound",
        "NoSuchEntity",
        "NoSuchBucket",
    }
)

# Transient AWS error codes - safe to retry after backoff.
TRANSIENT_AWS_CODES: frozenset[str] = frozenset(
    {
        "RequestLimitExceeded",
        "Throttling",
        "ThrottlingException",
        "RequestThrottledException",
        "TooManyRequestsException",
        "ServiceUnavailable",
        "InternalError",
        "InternalFailure",
        "RequestTimeout",
        "RequestTimeoutException",
    }
)


def classify_aws_error(e: Exception) -> tuple[str, str]:
    """Classify AWS error into error_type and message.

    Returns:
        Tuple of (error_type, error_message) where error_type is one of:
        - credentials_missing: No AWS credentials configured
        - credentials_expired: Token/session expired
        - credentials_invalid: Invalid signature or keys
        - profile_not_found: AWS profile doesn't exist
        - access_denied: Valid creds but insufficient permissions
        - aws_error: Other AWS API errors
        - unknown_error: Non-AWS exceptions
    """
    if isinstance(e, NoCredentialsError):
        return "credentials_missing", "AWS credentials not found"
    if isinstance(e, ProfileNotFound):
        return "profile_not_found", f"AWS profile not found: {e}"
    if isinstance(e, TokenRetrievalError):
        return "credentials_expired", "AWS credentials expired - please refresh"
    if isinstance(e, ClientError):
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("ExpiredToken", "ExpiredTokenException"):
            return "credentials_expired", "AWS credentials expired - please refresh"
        if code in ("InvalidSignatureException", "SignatureDoesNotMatch"):
            return "credentials_invalid", "AWS credentials invalid or expired"
        if code in ("InvalidClientTokenId", "AuthFailure"):
            return "credentials_invalid", "AWS credentials are invalid"
        if code == "AccessDenied":
            return "access_denied", f"Access denied: {e}"
        return "aws_error", str(e)
    if isinstance(e, BotoCoreError):
        return "aws_error", str(e)
    return "unknown_error", str(e)


def delete_with_retry(
    fn: Callable[..., Any],
    *args: Any,
    resource_desc: str = "resource",
    attempts: int = 3,
    backoff_seconds: float = 2.0,
    transient_codes: frozenset[str] = TRANSIENT_AWS_CODES,
    **kwargs: Any,
) -> bool:
    """Call ``fn(*args, **kwargs)`` with retry on transient AWS errors.

    Designed for best-effort cleanup in ``finally`` blocks where a single-
    attempt delete leaks resources whenever a transient error fires
    (e.g. throttling, endpoint connection resets, service unavailable).

    Already-gone errors (``Invalid*.NotFound`` etc.) count as success - the
    desired end state is reached. Non-transient errors are logged and
    return ``False`` without retry.

    Args:
        fn: The bound boto3 method to call (e.g. ``ec2.delete_vpc``).
        *args: Positional args forwarded to ``fn``.
        resource_desc: Human-readable description for logs (e.g. ``"VPC vpc-abc"``).
        attempts: Total attempts including the first (default 3).
        backoff_seconds: Linear backoff base; delay = backoff * attempt (default 2.0s).
        transient_codes: AWS ``Error.Code`` values treated as retryable.
        **kwargs: Keyword args forwarded to ``fn``.

    Returns:
        True if the call succeeded or the resource was already gone;
        False if all attempts exhausted or a non-transient error was raised.
        Never raises - failures are logged by the caller, which is expected to
        reason about the returned ``False`` (e.g., record as orphan for later cleanup).
    """
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            fn(*args, **kwargs)
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ALREADY_GONE_CODES:
                return True
            if code in transient_codes and attempt < attempts:
                last_error = e
                delay = backoff_seconds * attempt
                logger.warning(
                    "Transient error deleting %s (attempt %d/%d, code=%s): %s; retrying in %.1fs",
                    resource_desc,
                    attempt,
                    attempts,
                    code,
                    e,
                    delay,
                )
                time.sleep(delay)
                continue
            logger.exception("Failed to delete %s (code=%s)", resource_desc, code)
            return False
        except BotoCoreError as e:
            # Network-level boto errors (EndpointConnectionError, ConnectionClosedError, etc.)
            if attempt < attempts:
                last_error = e
                delay = backoff_seconds * attempt
                logger.warning(
                    "Transient network error deleting %s (attempt %d/%d): %s; retrying in %.1fs",
                    resource_desc,
                    attempt,
                    attempts,
                    e,
                    delay,
                )
                time.sleep(delay)
                continue
            logger.exception("Failed to delete %s after %d attempts", resource_desc, attempts)
            return False
        except Exception:
            # Contract: this helper never raises (callers use it in finally
            # blocks, where a propagating exception would skip sibling cleanup).
            logger.exception("Unexpected error deleting %s", resource_desc)
            return False

    if last_error is not None:
        logger.error("Exhausted retries deleting %s: %s", resource_desc, last_error)
    return False


def handle_aws_errors[**P](func: Callable[P, int]) -> Callable[P, int]:
    """Decorator that catches AWS errors and outputs structured JSON.

    Scripts still print their own JSON and return 0/1.
    This decorator only catches uncaught exceptions (like boto3.client() failing).

    Usage:
        @handle_aws_errors
        def main() -> int:
            # ... do work, print JSON ...
            return 0 if success else 1

        if __name__ == "__main__":
            sys.exit(main())
    """

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> int:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            error_type, error_msg = classify_aws_error(e)
            print(json.dumps({"success": False, "error_type": error_type, "error": error_msg}, indent=2))
            return 1

    return wrapper
