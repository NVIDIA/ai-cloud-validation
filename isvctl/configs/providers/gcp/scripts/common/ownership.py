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

"""Ownership transfer for GCP creates with ambiguous acknowledgements.

Create APIs can commit a resource and then lose the response.  A caller may
claim cleanup ownership only after either receiving the create acknowledgement
or reading back the exact resource with this invocation's marker.  Definite
conflicts never trigger readback or ownership transfer.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from secrets import token_hex
from typing import Any

from google.api_core import exceptions as gax

from common.errors import classify_gcp_error, is_transport_disconnect

INVOCATION_LABEL = "isv-invocation"
INVOCATION_DESCRIPTION_KEY = "isv-invocation"
CREATED_BY_LABEL = "created-by"
CREATED_BY_VALUE = "isvtest"
CREATED_BY_DESCRIPTION = f"{CREATED_BY_LABEL}={CREATED_BY_VALUE}"
DEFAULT_READBACK_ATTEMPTS = 3
DEFAULT_READBACK_BACKOFF_SECONDS = 1.0

_CONFLICT_CLASS_NAMES = {"Aborted", "AlreadyExists", "Conflict"}


def new_invocation_id() -> str:
    """Return a GCP-label-safe invocation discriminator."""
    return token_hex(16)


def labels_with_invocation(labels: dict[str, str] | None, invocation_id: str) -> dict[str, str]:
    """Return ``labels`` with the invocation marker added without dropping ownership labels."""
    return {**(labels or {}), INVOCATION_LABEL: invocation_id}


def description_with_invocation(description: str, invocation_id: str) -> str:
    """Append an invocation marker to a description-only GCP resource."""
    return f"{description} ({INVOCATION_DESCRIPTION_KEY}={invocation_id})"


def has_invocation_label(resource: Any, invocation_id: str) -> bool:
    """Return whether a label-capable resource echoes ``invocation_id``."""
    labels = dict(getattr(resource, "labels", None) or {})
    return labels.get(INVOCATION_LABEL) == invocation_id


def has_invocation_description(resource: Any, invocation_id: str) -> bool:
    """Return whether a description-only resource echoes ``invocation_id``."""
    marker = f"{INVOCATION_DESCRIPTION_KEY}={invocation_id}"
    return marker in str(getattr(resource, "description", "") or "")


def create_may_have_committed(error: Exception) -> bool:
    """Return whether a failed create may have committed server-side."""
    bucket = classify_gcp_error(error)[0]
    if isinstance(error, gax.Conflict) or bucket == "conflict" or type(error).__name__ in _CONFLICT_CLASS_NAMES:
        return False
    if is_transport_disconnect(error):
        return True
    return bucket in {"transient", "api_error", "unknown_error"}


def read_back_owned[T](
    read_back: Callable[[], T],
    owns: Callable[[T], bool],
    *,
    attempts: int = DEFAULT_READBACK_ATTEMPTS,
    backoff_seconds: float = DEFAULT_READBACK_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Boundedly read an exact resource and verify this invocation owns it."""
    for attempt in range(1, attempts + 1):
        try:
            resource = read_back()
        except gax.NotFound:
            resource = None
        except Exception as exc:
            if not create_may_have_committed(exc):
                return False
            resource = None

        if resource is not None:
            return owns(resource)
        if attempt < attempts:
            sleep(backoff_seconds * attempt)
    return False


def submit_owned_create[T](
    submit: Callable[[], T],
    read_back: Callable[[], Any],
    owns: Callable[[Any], bool],
    *,
    on_accepted: Callable[[], None] | None = None,
    readback_attempts: int = DEFAULT_READBACK_ATTEMPTS,
    readback_backoff_seconds: float = DEFAULT_READBACK_BACKOFF_SECONDS,
) -> T:
    """Submit a marked create and transfer cleanup ownership exactly once.

    A successful acknowledgement transfers ownership before the caller waits on
    any asynchronous operation.  An ambiguous error transfers ownership only
    when bounded exact readback echoes the invocation marker, then re-raises the
    original error so the caller's normal failure path performs cleanup.
    """
    try:
        result = submit()
    except Exception as exc:
        if create_may_have_committed(exc) and read_back_owned(
            read_back,
            owns,
            attempts=readback_attempts,
            backoff_seconds=readback_backoff_seconds,
        ):
            if on_accepted is not None:
                on_accepted()
        raise
    if on_accepted is not None:
        on_accepted()
    return result
