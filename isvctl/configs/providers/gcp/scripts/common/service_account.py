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

"""Shared service-account lifecycle + absence-proof helpers for GCP stubs.

Two families of stub consume this module:

  * firewall-scoping stubs, which need a distinct owned identity for a probe VM
    (the original motivating case, described below), and
  * control-plane HMAC lifecycle stubs (``control-plane/create_access_key.py`` and
    ``control-plane/delete_access_key.py``), which self-create and tear down the
    service account that owns a Cloud Storage HMAC key and rely on the same
    absence-proof (``service_account_absent``) helper for idempotent teardown.

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
from google.auth import exceptions as auth_exceptions
from google.cloud import iam_admin_v1
from google.iam.v1 import iam_policy_pb2, policy_pb2

from common.errors import TRANSIENT_EXCEPTIONS, is_transport_disconnect, retry_idempotent
from common.network import insert_instance

# IAM propagation budget: a freshly-created serviceAccountUser binding is not
# effective on instances.insert immediately; GCE returns permission-denied /
# actAs-not-yet-effective for up to ~3 minutes after the binding is set.
IAM_PROPAGATION_ATTEMPTS = 12
IAM_PROPAGATION_DELAY = 15  # seconds -> 180s budget

# Bounded retry budget for the service-account delete (mirrors the transient
# handling in common.errors.delete_with_retry).
_SA_DELETE_ATTEMPTS = 5
_SA_DELETE_BACKOFF = 2.0  # seconds, multiplied by the attempt number

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


def create_service_account_resource(project: str, account_id: str, *, display_name: str) -> iam_admin_v1.ServiceAccount:
    """Create a test-owned service account and return the provider ``ServiceAccount``.

    Returns the resource the API returns so callers can populate identity fields
    from server-created evidence (``email`` and the resource ``name``) instead of
    reconstructing them locally. Callers that only need the address use
    :func:`create_service_account`, which wraps this and returns ``.email``.
    """
    iam = iam_admin_v1.IAMClient()
    sa = iam_admin_v1.ServiceAccount()
    sa.display_name = display_name
    return iam.create_service_account(
        name=f"projects/{project}",
        account_id=account_id,
        service_account=sa,
    )


def create_service_account(project: str, account_id: str, *, display_name: str) -> str:
    """Create a test-owned service account and return its server-populated email."""
    return create_service_account_resource(project, account_id, display_name=display_name).email


def _list_service_account_emails(project: str) -> list[str]:
    """Materialize the FULL paginated service-account listing for ``project``.

    ``list_service_accounts`` returns a lazy pager: iterating it fetches later
    pages on demand, so a transient failure on one of those deferred page
    fetches escapes the installed SDK's partial default list retry. Forcing the
    complete ``list(...)`` materialization here — under ``retry_idempotent`` in
    the caller — is what lets the full set of transient errors (429 /
    ServiceUnavailable / InternalServerError / DeadlineExceeded) be retried on
    EVERY page fetch, not just the first request.
    """
    iam = iam_admin_v1.IAMClient()
    return [acct.email for acct in iam.list_service_accounts(name=f"projects/{project}")]


def service_account_absent(project: str, sa_email: str) -> bool | None:
    """Return whether ``sa_email`` is genuinely absent from ``project``'s SA list.

    A GCP service-account get/delete can return ``PermissionDenied`` 403 for BOTH
    a denied caller and an already-absent SA (existence hiding), so a 403 alone is
    not proof the SA is gone. A project-scoped ``list_service_accounts`` is a
    trustworthy absence signal: the SA is genuinely deleted iff its email is not
    present in the list.

    The whole paginated listing is materialized inside ``retry_idempotent`` so a
    transient (429 / 5xx / timeout / transport disconnect) on ANY page fetch is
    retried with bounded backoff rather than converting a recoverable blip into a
    spurious inconclusive result. Three unreadable-list dispositions outlast that
    budget and MUST all collapse to ``None`` rather than escape, because the two
    callers (``create_access_key`` rollback, ``delete_access_key`` teardown)
    consume the tri-state and never catch these themselves:

      * a terminal non-transient list failure, or a typed transient that outlasts
        the retry budget, surfaces as ``gax.GoogleAPICallError``;
      * an ADC credential-refresh failure surfaces as
        ``google.auth.exceptions.RefreshError`` — either raised immediately by
        ``retry_idempotent`` (non-retryable) or after its transient budget is
        exhausted (retryable) — and is NOT a ``GoogleAPICallError``;
      * a raw transport disconnect (``RemoteDisconnected`` / urllib3
        ``ProtocolError``, possibly re-wrapped) that outlasts the single
        transport retry is likewise NOT a ``GoogleAPICallError``.

    All three leave the list genuinely unreadable, so they yield ``None``. Every
    other exception — programming errors and unrelated failures — propagates
    unchanged.

    Returns ``True`` (confirmed absent), ``False`` (still present -> a delete that
    reported success was really a denial), or ``None`` when the list itself is not
    readable (inconclusive — the caller must fall back to the delete-call result
    rather than treat this as proof either way).
    """
    try:
        emails = retry_idempotent(
            _list_service_account_emails,
            project,
            op_desc="iam.list_service_accounts (absence proof)",
        )
    except gax.GoogleAPICallError:
        return None
    except auth_exceptions.RefreshError:
        # An exhausted (retryable) or immediately-raised (non-retryable) ADC
        # credential-refresh failure means the list could not be read at all —
        # inconclusive, not proof of absence. RefreshError is not a
        # GoogleAPICallError, so it would otherwise escape the arm above.
        return None
    except Exception as e:
        # A raw transport disconnect (RemoteDisconnected / urllib3 ProtocolError,
        # possibly re-wrapped) that outlasts retry_idempotent's single transport
        # retry is not a google.api_core type either, so it lands here. It too
        # leaves the list unreadable -> inconclusive. Everything else (programming
        # errors, unrelated failures) re-raises so genuine bugs stay loud.
        if is_transport_disconnect(e):
            return None
        raise
    return sa_email not in emails


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

    Returns True when the SA was deleted now OR is already absent. An absent GCP
    service account does NOT surface as ``NotFound``: a deleted/absent SA returns
    ``PermissionDenied`` 403 ("...denied on resource (or it may not exist)") on
    BOTH get and delete. Because this SA is test-owned — the run created it and so
    holds ``iam.serviceAccounts.delete`` — that 403 cannot be a real permission
    loss; it is the existence-hiding shape of the already-gone terminal state (for
    example a concurrent owner sweep sharing this RUN_ID removed it first), so it
    is folded into success. Only a documented transient (rate-limit / 5xx /
    timeout) that persists past the retry budget returns False, so the caller can
    fold the genuine leak into ``cleanup_errors`` / overall ``success`` rather than
    silently orphaning a project-level SA. Replaces the generic
    ``common.errors.delete_with_retry`` envelope, which classifies the
    existence-hiding 403 as a terminal failure and would report a swept SA as a
    leak.
    """
    iam = iam_admin_v1.IAMClient()
    name = f"projects/-/serviceAccounts/{sa_email}"
    for attempt in range(1, _SA_DELETE_ATTEMPTS + 1):
        try:
            iam.delete_service_account(name=name)
            return True
        except gax.NotFound:
            return True
        except (gax.PermissionDenied, gax.Forbidden):
            # Existence-hiding 403 on a test-owned SA == already gone.
            return True
        except TRANSIENT_EXCEPTIONS:
            if attempt < _SA_DELETE_ATTEMPTS:
                time.sleep(_SA_DELETE_BACKOFF * attempt)
                continue
            return False
        except gax.GoogleAPICallError:
            return False
    return False


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
