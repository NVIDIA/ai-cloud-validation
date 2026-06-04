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

"""Shared service-account lifecycle helpers for GCP firewall-scoping stubs.

Proving that a firewall does NOT select a sibling VM requires that sibling to
carry a DISTINCT, NON-EMPTY service account. The proto-plus ``compute_v1`` REST
client serializes ``service_accounts=[]`` identically to an unset field, so an
empty list collapses to the shared default Compute service account and reads as
a fake-pass (see ``common.network.build_probe_instance`` and the gcp/network
``sg_service_scoping`` divergence). To give the negative observation a genuinely
independent identity, these helpers:

  * self-create a test-owned service account (``create_service_account``),
  * grant the operator ADC principal ``roles/iam.serviceAccountUser`` on it so
    the VM-attach succeeds (``resolve_principal_member`` /
    ``bind_service_account_user``),
  * insert the VM, retrying while the fresh ``actAs`` binding propagates
    (``insert_instance_with_iam_propagation``), and
  * delete the SA on cleanup (``delete_service_account``).

This module is the canonical home for the pattern. ``sg_scoping_test.py`` still
carries an equivalent private copy (service scope); it can migrate to these
helpers in a follow-up without behavior change.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, cast

import google.auth
import google.auth.credentials
import google.auth.transport.requests
from google.api_core import exceptions as gax
from google.cloud import iam_admin_v1
from google.iam.v1 import iam_policy_pb2, policy_pb2

from common.errors import delete_with_retry
from common.network import insert_instance

# IAM propagation budget: a freshly-created serviceAccountUser binding is not
# effective on instances.insert immediately; GCE returns permission-denied /
# actAs-not-yet-effective for up to ~3 minutes after the binding is set.
IAM_PROPAGATION_ATTEMPTS = 12
IAM_PROPAGATION_DELAY = 15  # seconds -> 180s budget

# OAuth2 tokeninfo endpoint used to resolve the ADC principal email when
# GCP_TEST_SA_EMAIL is not supplied by the operator.
_TOKENINFO_URL = "https://www.googleapis.com/oauth2/v1/tokeninfo"


def resolve_principal_member() -> str:
    """Resolve the principal that must be granted ``serviceAccountUser`` on a new SA.

    Prefers the operator-pinned ``GCP_TEST_SA_EMAIL`` (a USER email — the
    principal that will act-as the created SA). Otherwise refresh ADC and read
    the OAuth2 tokeninfo endpoint for the active principal's email. Returns the
    IAM member string (``user:`` or ``serviceAccount:`` prefixed).
    """
    pinned = os.environ.get("GCP_TEST_SA_EMAIL", "").strip()
    if pinned:
        return pinned if ":" in pinned else f"user:{pinned}"

    raw_creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds = cast(google.auth.credentials.Credentials, raw_creds)
    auth_req = google.auth.transport.requests.Request()
    creds.refresh(auth_req)

    # Service-account ADC exposes the email directly; user ADC does not, so
    # fall back to the tokeninfo endpoint.
    sa_email = getattr(creds, "service_account_email", None)
    if sa_email and sa_email != "default":
        return f"serviceAccount:{sa_email}"

    resp = auth_req(url=f"{_TOKENINFO_URL}?access_token={creds.token}", method="GET")
    info = json.loads(resp.data.decode("utf-8") if isinstance(resp.data, bytes) else resp.data)
    email = info.get("email")
    if not email:
        raise RuntimeError(
            "could not resolve ADC principal email from tokeninfo; set GCP_TEST_SA_EMAIL to the operator principal"
        )
    prefix = "serviceAccount:" if email.endswith(".gserviceaccount.com") else "user:"
    return f"{prefix}{email}"


def create_service_account(project: str, account_id: str, *, display_name: str) -> str:
    """Create a test-owned service account and return its email."""
    iam = iam_admin_v1.IAMClient()
    sa = iam_admin_v1.ServiceAccount()
    sa.display_name = display_name
    iam.create_service_account(
        name=f"projects/{project}",
        account_id=account_id,
        service_account=sa,
    )
    return f"{account_id}@{project}.iam.gserviceaccount.com"


def bind_service_account_user(sa_email: str, member: str) -> None:
    """Grant ``member`` roles/iam.serviceAccountUser on the SA so VM-attach succeeds."""
    iam = iam_admin_v1.IAMClient()
    binding = policy_pb2.Binding(role="roles/iam.serviceAccountUser", members=[member])
    policy = policy_pb2.Policy(bindings=[binding])
    request = iam_policy_pb2.SetIamPolicyRequest(
        resource=f"projects/-/serviceAccounts/{sa_email}",
        policy=policy,
    )
    iam.set_iam_policy(request=request)


def delete_service_account(sa_email: str) -> bool:
    """Delete the test-owned SA with bounded retry; return True iff it is gone.

    Returns True when the SA was deleted now OR is already absent (NotFound is
    the desired terminal state — the eventual-consistency window is absorbed by
    the retry). Returns False only when a documented transient IAM failure
    (rate-limit / 5xx / timeout) persists past the retry budget, so the caller
    can fold the genuine leak into ``cleanup_errors`` / ``tests.cleanup.passed``
    / overall ``success`` rather than silently orphaning a project-level SA.
    Wraps the canonical GCP cleanup envelope (``common.errors.delete_with_retry``)
    so SA cleanup matches every other cloud-delete in these stubs and the GCP VM
    ``console_rbac`` bool contract.
    """
    iam = iam_admin_v1.IAMClient()
    return delete_with_retry(
        iam.delete_service_account,
        name=f"projects/-/serviceAccounts/{sa_email}",
        resource_desc=f"service account {sa_email}",
    )


def insert_instance_with_iam_propagation(project: str, zone: str, instance: Any) -> None:
    """Insert an instance, retrying while a fresh ``actAs`` binding propagates.

    A just-created serviceAccountUser binding is not effective on
    instances.insert immediately; GCE returns permission-denied /
    actAs-not-yet-effective for up to ~3 minutes. Retry within the propagation
    budget; re-raise any non-permission error immediately.
    """
    last_err: Exception | None = None
    for attempt in range(1, IAM_PROPAGATION_ATTEMPTS + 1):
        try:
            insert_instance(project, zone, instance)
            return
        except gax.PermissionDenied as e:
            last_err = e
        except (gax.Forbidden, gax.BadRequest) as e:
            # actAs-not-yet-effective sometimes surfaces as 400/403 with an
            # "iam.serviceAccounts.actAs" message rather than PermissionDenied.
            if "actas" not in str(e).lower() and "serviceaccount" not in str(e).lower():
                raise
            last_err = e
        if attempt < IAM_PROPAGATION_ATTEMPTS:
            print(
                f"  IAM actAs not yet effective (attempt {attempt}/{IAM_PROPAGATION_ATTEMPTS}); "
                f"sleeping {IAM_PROPAGATION_DELAY}s",
                file=sys.stderr,
            )
            time.sleep(IAM_PROPAGATION_DELAY)
    raise RuntimeError(
        f"IAM actAs binding did not propagate within {IAM_PROPAGATION_ATTEMPTS * IAM_PROPAGATION_DELAY}s: {last_err}"
    ) from last_err
