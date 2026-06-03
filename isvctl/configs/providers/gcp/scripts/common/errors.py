# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

"""GCP error classification + retry helpers for VM stubs.

Mirrors providers/aws/scripts/common/errors.py so the structured output
shape stays identical across providers:
    {"success": false, "error_type": "<category>", "error": "<msg>"}

Categories match the AWS provider:
    credentials_missing, credentials_invalid, access_denied,
    api_error, unknown_error.
"""

from __future__ import annotations

import functools
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from google.api_core import exceptions as gax
from google.auth import exceptions as auth_exceptions

logger = logging.getLogger(__name__)

ALREADY_GONE_EXCEPTIONS: tuple[type[Exception], ...] = (gax.NotFound,)

TRANSIENT_EXCEPTIONS: tuple[type[Exception], ...] = (
    gax.ServiceUnavailable,
    gax.InternalServerError,
    gax.GatewayTimeout,
    gax.DeadlineExceeded,
    gax.TooManyRequests,
    gax.Aborted,
    gax.RetryError,
)

# Compute Engine reports the dependency-in-use condition (a resource cannot
# be deleted until its dependents finish draining) as an HTTP 400 ->
# gax.BadRequest (or, on an async DONE-with-errors op surfaced by the
# op-wait helpers, a RuntimeError carrying the same wire code). This is the
# Compute Engine analog of the AWS provider's DependencyViolation, which the
# AWS provider's teardown retries with backoff (see
# providers/aws/scripts/network/teardown.py delete_with_retry). It is
# eventually-consistent — a delete issued right after a dependent's delete
# op reports DONE can still race the backend dependency-graph drain — so it
# MUST be retried, not treated as terminal.
_DEPENDENCY_IN_USE_MARKERS: tuple[str, ...] = (
    "resourceinusebyanotherresource",
    "resource is being used",
    "is already being used",
    "is being used by",
    "resourcenotready",
    "resource is not ready",
)


def _is_dependency_in_use(e: Exception) -> bool:
    """Return True iff ``e`` signals the dependency-in-use (retryable) condition."""
    msg = str(e).lower()
    return any(marker in msg for marker in _DEPENDENCY_IN_USE_MARKERS)


def classify_gcp_error(e: Exception) -> tuple[str, str]:
    """Translate a GCP exception into (error_type, message).

    Categories mirror providers/aws/scripts/common/errors.classify_aws_error.
    Order: credentials_missing (no ADC at all) before credentials_invalid
    (ADC present but rejected) so a missing-setup operator gets the
    setup-pointing message rather than the "invalid credentials" one.
    """
    if isinstance(e, auth_exceptions.DefaultCredentialsError):
        return "credentials_missing", f"GCP credentials missing or not configured: {e}"
    if isinstance(e, gax.Unauthenticated):
        return "credentials_invalid", f"GCP credentials invalid or missing: {e}"
    if isinstance(e, gax.PermissionDenied):
        return "access_denied", f"Access denied: {e}"
    if isinstance(e, gax.NotFound):
        return "api_error", str(e)
    if isinstance(e, gax.GoogleAPICallError):
        return "api_error", str(e)
    if isinstance(e, gax.RetryError):
        return "api_error", str(e)
    return "unknown_error", str(e)


def delete_with_retry(
    fn: Callable[..., Any],
    *args: Any,
    resource_desc: str = "resource",
    attempts: int = 5,
    backoff_seconds: float = 2.0,
    **kwargs: Any,
) -> bool:
    """Call ``fn`` with bounded retry on transient + dependency-in-use errors.

    Never raises. Returns True iff the call succeeded or the resource was
    already gone (NotFound counts as success — the desired terminal state
    is reached). Mirrors providers/aws/scripts/common/errors.delete_with_retry
    so callers can write provider-portable cleanup blocks.

    Two retryable classes (otherwise terminal -> return False):

      * Rate-limit / 5xx / timeout (``TRANSIENT_EXCEPTIONS``).
      * Dependency-in-use (``_is_dependency_in_use``) — the eventually-
        consistent condition where a resource cannot yet be deleted because
        a dependent is still draining after its own delete op reported DONE.
        Surfaces as ``gax.BadRequest`` / ``gax.FailedPrecondition`` (HTTP
        400/412) or, on an async DONE-with-errors op re-raised by the
        op-wait helpers, as a ``RuntimeError`` carrying the same wire code.
        This mirrors the AWS provider retrying ``DependencyViolation``; the
        in-use backoff escalates faster (the drain takes longer than a rate
        limit). Non-in-use 400s stay terminal so simple bad-request deletes
        do not spin.

    The bool return MUST be consumed by the caller and AND-ed into the
    teardown result — helpers that return ``bool`` for batch-cleanup
    safety MUST surface the bool into ``result['success']``.
    """
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            fn(*args, **kwargs)
            return True
        except ALREADY_GONE_EXCEPTIONS:
            return True
        except TRANSIENT_EXCEPTIONS as e:
            if attempt < attempts:
                last_error = e
                delay = backoff_seconds * attempt
                logger.warning(
                    "Transient error deleting %s (attempt %d/%d): %s; retrying in %.1fs",
                    resource_desc, attempt, attempts, e, delay,
                )
                time.sleep(delay)
                continue
            logger.exception("Failed to delete %s after %d attempts", resource_desc, attempts)
            return False
        except (gax.BadRequest, gax.FailedPrecondition) as e:
            # Dependency-in-use (a 400/412) is retryable; any other
            # bad-request is a terminal misuse that retrying cannot fix.
            if _is_dependency_in_use(e) and attempt < attempts:
                last_error = e
                delay = backoff_seconds * (attempt + 1) * 2
                logger.warning(
                    "Dependency-in-use deleting %s (attempt %d/%d): %s; retrying in %.1fs",
                    resource_desc, attempt, attempts, e, delay,
                )
                time.sleep(delay)
                continue
            logger.exception("Non-retryable bad-request deleting %s", resource_desc)
            return False
        except gax.GoogleAPICallError:
            logger.exception("Non-transient API error deleting %s", resource_desc)
            return False
        except Exception as e:
            # The op-wait helpers raise RuntimeError carrying the op error
            # code; a dependency-in-use code surfaces here and is retryable.
            if _is_dependency_in_use(e) and attempt < attempts:
                last_error = e
                delay = backoff_seconds * (attempt + 1) * 2
                logger.warning(
                    "Dependency-in-use (op error) deleting %s (attempt %d/%d): %s; retrying in %.1fs",
                    resource_desc, attempt, attempts, e, delay,
                )
                time.sleep(delay)
                continue
            logger.exception("Unexpected error deleting %s", resource_desc)
            return False

    if last_error is not None:
        logger.error("Exhausted retries deleting %s: %s", resource_desc, last_error)
    return False


def handle_gcp_errors[**P](func: Callable[P, int]) -> Callable[P, int]:
    """Decorator that catches uncaught GCP errors and emits structured JSON.

    Mirrors providers/aws/scripts/common/errors.handle_aws_errors. Scripts
    still print their own JSON and return 0/1; this decorator only handles
    exceptions that escape main (e.g. client construction).
    """

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> int:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            error_type, error_msg = classify_gcp_error(e)
            print(json.dumps(
                {"success": False, "error_type": error_type, "error": error_msg},
                indent=2,
            ))
            return 1

    return wrapper
