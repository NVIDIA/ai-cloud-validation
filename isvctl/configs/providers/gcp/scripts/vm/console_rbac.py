#!/usr/bin/env python3
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

"""Console RBAC probe for Compute Engine serial console access.

The suite contract requires three subtests:
  1. denied_principal_cannot_access_console
  2. allowed_principal_can_access_console
  3. allowed_principal_is_resource_scoped

GCP has no AWS-style ``simulate_principal_policy`` equivalent for Compute
Engine serial console access (``instances.testIamPermissions`` and
``instances.getSerialPortOutput`` both evaluate the caller). Because the
APIs evaluate the caller rather than a simulated principal, RBAC evidence
must come from REAL probe principals:

  * a denied service account WITHOUT
    ``compute.instances.getSerialPortOutput`` on the target VM,
  * an allowed service account WITH that permission scoped to the target
    VM only,
  * a real second VM where the allowed SA must still be denied.

The default path is SELF-PROVISIONED: this stub creates two temporary
probe service accounts and a second probe VM, grants the caller
``roles/iam.serviceAccountTokenCreator`` on the probe SAs, grants the
allowed SA a minimal serial-output role scoped to the target VM only,
mints short-lived access tokens via the IAMCredentials REST
``generateAccessToken`` endpoint, and probes
``instances.getSerialPortOutput`` as each principal. Cleanup deletes the
temporary SAs and probe VM and removes the IAM bindings with
read-modify-write retry on etag conflicts.

The pre-provisioned env-var path (``GCP_DENIED_PRINCIPAL_SA``,
``GCP_ALLOWED_PRINCIPAL_SA``, ``GCP_OTHER_INSTANCE_ID``) is a FALLBACK
for projects where the operator cannot allow IAM mutation. The fallback
is opt-in via the env vars themselves; otherwise the self-provisioned
path runs.

Compute Engine serial-console RBAC implementation notes:
  * Direct IAMCredentials REST token minting is the stable path. Avoid
    ``google.auth.impersonated_credentials.Credentials`` with local
    authorized-user ADC — its refresh code can call a private
    ``_refresh_token`` member that is a string on authorized-user
    credentials, raising ``TypeError: 'str' object is not callable``.
  * Resolve the ADC caller from tokeninfo when local user ADC has no
    ``service_account_email`` and an empty ``account``.

HTTP 404 on the second VM probe is treated as a FAILURE, not as proof of
RBAC scoping (the resource being missing means IAM enforcement could not
be observed).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import (
    narrow_region_to_zone,
    resolve_project,
    unique_suffix,
    wait_for_zonal_op,
)
from common.errors import handle_gcp_errors

# Serial console permission. The validator's ``restricted_actions`` field
# is populated with this exact permission name so downstream audits can
# correlate the IAM action with the API call.
_CONSOLE_PERMISSION = "compute.instances.getSerialPortOutput"
_RESTRICTED_ACTIONS = (_CONSOLE_PERMISSION,)

# Compute Engine REST endpoints.
_COMPUTE_BASE = "https://compute.googleapis.com/compute/v1"
_IAM_BASE = "https://iam.googleapis.com/v1"
_IAM_CREDENTIALS_BASE = "https://iamcredentials.googleapis.com/v1"
_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"

# Default per-call timeout for raw HTTP calls (seconds). The enclosing
# step timeout is the real deadline; this is just a safeguard against
# the urllib request hanging on a single API call.
_HTTP_TIMEOUT_S = 30

# Minimal predefined role granting serial-port-output read. ``roles/
# compute.viewer`` includes the permission and is broader than necessary,
# but is the acceptable predefined role for this probe (an equivalent
# minimal serial-output custom role would be tighter but requires extra
# provisioning the probe does not need).
_ALLOWED_TARGET_ROLE = "roles/compute.viewer"
_TOKEN_CREATOR_ROLE = "roles/iam.serviceAccountTokenCreator"

# Self-provisioning is the DEFAULT path. The pre-provisioned fallback is
# opt-in by exporting all three env vars.
_DENIED_SA_ENV = "GCP_DENIED_PRINCIPAL_SA"
_ALLOWED_SA_ENV = "GCP_ALLOWED_PRINCIPAL_SA"
_OTHER_INSTANCE_ENV = "GCP_OTHER_INSTANCE_ID"
_OTHER_INSTANCE_ZONE_ENV = "GCP_OTHER_INSTANCE_ZONE"
# Operators that cannot grant IAM mutations to the caller can set this
# to ``"0"`` to force-skip the self-provisioned path even when no fallback
# env vars are supplied; otherwise the stub will try self-provisioning
# and surface an honest failure if any step is denied.
_SELF_PROVISION_ENABLED_ENV = "GCP_SELF_PROVISION_RBAC"


def _http_request(
    method: str,
    url: str,
    token: str,
    *,
    body: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Issue an HTTPS call with a bearer token; return ``(status, body)``.

    Body is the parsed JSON when the response is JSON, or ``{}`` when
    the response is empty / non-JSON. Errors raise ``urllib.error.HTTPError``
    which the caller catches to read the status code.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_S) as response:
            status = response.getcode() or 0
            raw = response.read()
    except urllib.error.HTTPError:
        raise
    parsed: dict[str, Any] = {}
    if raw:
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            parsed = {"raw": raw.decode("utf-8", errors="replace")}
    return status, parsed


def _http_error_body(error: urllib.error.HTTPError) -> dict[str, Any]:
    """Best-effort extract JSON body from an HTTPError."""
    try:
        raw = error.read()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"raw": raw.decode("utf-8", errors="replace")}


def _adc_access_token() -> str:
    """Refresh ADC and return the access token string."""
    import google.auth
    import google.auth.transport.requests
    from google.auth.credentials import Credentials

    raw_creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds: Credentials = raw_creds  # type: ignore[assignment]
    creds.refresh(google.auth.transport.requests.Request())
    token = getattr(creds, "token", None)
    if not isinstance(token, str) or not token:
        msg = "ADC refresh produced no access token"
        raise RuntimeError(msg)
    return token


def _resolve_caller_member(access_token: str) -> str:
    """Resolve the calling principal to a ``user:`` / ``serviceAccount:`` member.

    Local user ADC (``gcloud auth application-default login``) commonly
    has no ``service_account_email`` and an empty ``account`` attribute;
    in that case the only reliable identifier is the tokeninfo endpoint,
    which returns the authenticated email for the refreshed access token.
    """
    import google.auth

    creds, _ = google.auth.default()
    sa_email = getattr(creds, "service_account_email", None)
    if isinstance(sa_email, str) and sa_email:
        return f"serviceAccount:{sa_email}"
    account = getattr(creds, "account", "")
    if isinstance(account, str) and account:
        # gcloud user ADC populates ``account`` with the user email.
        return f"user:{account}"

    # Last resort: probe the tokeninfo endpoint.
    url = f"{_TOKENINFO_URL}?access_token={urllib.parse.quote(access_token)}"
    with urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT_S) as response:
        info = json.loads(response.read().decode("utf-8"))
    email = info.get("email") or info.get("audience") or ""
    if not isinstance(email, str) or not email:
        msg = "could not resolve caller principal from ADC or tokeninfo"
        raise RuntimeError(msg)
    member_type = "serviceAccount" if email.endswith(".gserviceaccount.com") else "user"
    return f"{member_type}:{email}"


def _create_service_account(
    *,
    project: str,
    token: str,
    sa_id: str,
    display_name: str,
) -> str:
    """Create a service account and return its email."""
    url = f"{_IAM_BASE}/projects/{project}/serviceAccounts"
    body = {
        "accountId": sa_id,
        "serviceAccount": {"displayName": display_name},
    }
    _, response = _http_request("POST", url, token, body=body)
    email = response.get("email")
    if not isinstance(email, str) or not email:
        msg = f"create_service_account: response missing email: {response}"
        raise RuntimeError(msg)
    return email


def _delete_service_account(
    *,
    project: str,
    token: str,
    email: str,
    attempts: int = 5,
    backoff: float = 2.0,
) -> bool:
    """Delete a service account; NotFound is success.

    Bounded retry/backoff on transient IAM cleanup failures (HTTP 429 / 5xx
    and socket-level errors) so a single flaky delete call doesn't orphan the
    probe SA into the run namespace — the same transient envelope the
    grant-side read-modify-write retries use. 404 (already gone) is success;
    other 4xx are non-retryable.
    """
    url = f"{_IAM_BASE}/projects/{project}/serviceAccounts/{email}"
    last_error: str | None = None
    for attempt in range(1, attempts + 1):
        try:
            _http_request("DELETE", url, token)
            return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return True
            last_error = f"HTTP {e.code}: {_http_error_body(e)}"
            if e.code == 429 or 500 <= e.code < 600:
                time.sleep(backoff * attempt)
                continue
            print(f"  delete_service_account({email}) {last_error}", file=sys.stderr)
            return False
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_error = f"network error ({type(e).__name__}): {e}"
            time.sleep(backoff * attempt)
            continue
    print(f"  delete_service_account({email}) exhausted: {last_error}", file=sys.stderr)
    return False


def _service_account_resource(project: str, sa_email: str) -> str:
    """REST resource path for an SA (used by SA-resource IAM policy calls)."""
    return f"projects/{project}/serviceAccounts/{sa_email}"


def _is_member_propagation_error(body: dict[str, Any]) -> bool:
    """True if a setIamPolicy 400 body means the member SA has not propagated.

    A freshly-created service account referenced as an IAM *member* is
    eventually consistent: the policy write is rejected with HTTP 400
    ('... does not exist') until the member converges (same window as the
    TokenCreator binding handled in ``_mint_access_token``). Malformed-policy
    400s (unknown member type, unsupported role) carry different text and are
    intentionally NOT matched, so they stay non-retryable. ``body`` is the
    parsed JSON from ``_http_error_body``; serialize it so the match works
    whether the text lands in ``error.message`` or the ``raw`` fallback.
    """
    return "does not exist" in json.dumps(body).lower()


def _modify_iam_policy(
    *,
    get_url: str,
    set_url: str,
    token: str,
    operation: str,
    role: str,
    member: str,
    get_method: str = "POST",
    attempts: int = 5,
    backoff: float = 1.0,
    member_propagation_delay: float = 0.0,
) -> bool:
    """Read-modify-write an IAM policy with etag retry.

    ``operation`` is ``"add"`` or ``"remove"``. Returns True on success.

    ``get_method`` selects the HTTP verb for ``getIamPolicy``. The IAM API
    (service-account resources at ``iam.googleapis.com``) uses
    ``POST {resource}:getIamPolicy`` with a JSON body, while the Compute
    Engine API (instance / disk / zonal resources at
    ``compute.googleapis.com``) uses
    ``GET {resource}/getIamPolicy?optionsRequestedPolicyVersion=3`` and
    rejects POST with HTTP 400. ``setIamPolicy`` is POST on both APIs.
    """
    last_error: str | None = None
    for attempt in range(1, attempts + 1):
        try:
            if get_method == "GET":
                _, policy = _http_request(
                    "GET",
                    f"{get_url}?optionsRequestedPolicyVersion=3",
                    token,
                )
            else:
                _, policy = _http_request(
                    "POST",
                    get_url,
                    token,
                    body={"options": {"requestedPolicyVersion": 3}},
                )
        except urllib.error.HTTPError as e:
            last_error = f"getIamPolicy HTTP {e.code}: {_http_error_body(e)}"
            if e.code in (404, 403):
                # No policy / no permission — caller decides whether this is fatal.
                return False
            if e.code == 429 or 500 <= e.code < 600:
                time.sleep(backoff * attempt)
                continue
            # Other 4xx (malformed request, etc.) — retry will not help.
            return False
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # Transient socket-level failure (connect/read timeout, connection
            # reset, DNS). On Python >= 3.10 a mid-read `socket.timeout` is
            # `TimeoutError` and may NOT be wrapped in `URLError`, so catch
            # both directly to avoid escaping into the outer try.
            last_error = f"getIamPolicy network error ({type(e).__name__}): {e}"
            time.sleep(backoff * attempt)
            continue

        bindings = list(policy.get("bindings", []) or [])
        # Locate or create the role binding.
        target_idx = next((i for i, b in enumerate(bindings) if b.get("role") == role), None)
        if operation == "add":
            if target_idx is None:
                bindings.append({"role": role, "members": [member]})
            else:
                members = list(bindings[target_idx].get("members", []))
                if member not in members:
                    members.append(member)
                bindings[target_idx]["members"] = members
        elif operation == "remove":
            if target_idx is None:
                return True  # already absent
            members = [m for m in bindings[target_idx].get("members", []) if m != member]
            if members:
                bindings[target_idx]["members"] = members
            else:
                bindings.pop(target_idx)
        else:
            msg = f"invalid operation: {operation!r}"
            raise ValueError(msg)

        new_policy = {
            "bindings": bindings,
            "etag": policy.get("etag", ""),
            "version": policy.get("version", 1),
        }
        try:
            _http_request("POST", set_url, token, body={"policy": new_policy})
            return True
        except urllib.error.HTTPError as e:
            body = _http_error_body(e)
            last_error = f"setIamPolicy HTTP {e.code}: {body}"
            # A brand-new SA referenced as a member is eventually consistent;
            # setIamPolicy rejects the binding with HTTP 400 ('... does not
            # exist') until it propagates (~3 min observed). Callers that add a
            # just-created SA opt in via member_propagation_delay and retry the
            # read-modify-write on a flat delay until the member converges.
            if e.code == 400 and member_propagation_delay > 0 and _is_member_propagation_error(body):
                print(
                    f"  iam grant: member {member} not yet propagated; retrying in {member_propagation_delay:.0f}s",
                    file=sys.stderr,
                )
                time.sleep(member_propagation_delay)
                continue
            # 409 stale etag (refresh GET on next iter), 429 rate-limit, and 5xx
            # transient server errors all warrant the read-modify-write retry.
            if e.code in (409, 429) or 500 <= e.code < 600:
                time.sleep(backoff * attempt)
                continue
            print(f"  setIamPolicy non-retryable: {last_error}", file=sys.stderr)
            return False
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # Same socket-level coverage as the getIamPolicy arm — mid-read
            # timeouts can escape URLError on Python >= 3.10.
            last_error = f"setIamPolicy network error ({type(e).__name__}): {e}"
            time.sleep(backoff * attempt)
            continue

    if last_error:
        print(f"  iam_policy_retry exhausted: {last_error}", file=sys.stderr)
    return False


def _grant_token_creator(*, project: str, token: str, sa_email: str, member: str) -> bool:
    """Grant the caller TokenCreator on a probe SA."""
    base = f"{_IAM_BASE}/projects/{project}/serviceAccounts/{sa_email}"
    return _modify_iam_policy(
        get_url=f"{base}:getIamPolicy",
        set_url=f"{base}:setIamPolicy",
        token=token,
        operation="add",
        role=_TOKEN_CREATOR_ROLE,
        member=member,
    )


def _revoke_token_creator(*, project: str, token: str, sa_email: str, member: str) -> bool:
    base = f"{_IAM_BASE}/projects/{project}/serviceAccounts/{sa_email}"
    return _modify_iam_policy(
        get_url=f"{base}:getIamPolicy",
        set_url=f"{base}:setIamPolicy",
        token=token,
        operation="remove",
        role=_TOKEN_CREATOR_ROLE,
        member=member,
    )


def _grant_target_role(
    *,
    project: str,
    zone: str,
    instance: str,
    token: str,
    sa_email: str,
) -> bool:
    """Grant the allowed SA the target-VM serial-output role.

    ``sa_email`` is a probe SA created seconds earlier in this same step.
    A brand-new SA referenced as an IAM member is eventually consistent, so
    the instance setIamPolicy is rejected with HTTP 400 ('... does not exist')
    until the member propagates. Opt into the member-propagation retry (flat
    15s, ~3 min budget — the convergence window already documented on
    ``_mint_access_token``) so the grant lands deterministically instead of
    nondeterministically failing when the SA was minted moments ago.
    """
    base = f"{_COMPUTE_BASE}/projects/{project}/zones/{zone}/instances/{instance}"
    return _modify_iam_policy(
        get_url=f"{base}/getIamPolicy",
        set_url=f"{base}/setIamPolicy",
        token=token,
        operation="add",
        role=_ALLOWED_TARGET_ROLE,
        member=f"serviceAccount:{sa_email}",
        get_method="GET",
        attempts=14,
        member_propagation_delay=15.0,
    )


def _revoke_target_role(
    *,
    project: str,
    zone: str,
    instance: str,
    token: str,
    sa_email: str,
) -> bool:
    base = f"{_COMPUTE_BASE}/projects/{project}/zones/{zone}/instances/{instance}"
    return _modify_iam_policy(
        get_url=f"{base}/getIamPolicy",
        set_url=f"{base}/setIamPolicy",
        token=token,
        operation="remove",
        role=_ALLOWED_TARGET_ROLE,
        member=f"serviceAccount:{sa_email}",
        get_method="GET",
    )


def _mint_access_token(
    *,
    token: str,
    sa_email: str,
    attempts: int = 12,
    delay: float = 15.0,
) -> str:
    """Mint a short-lived access token for ``sa_email`` via IAMCredentials REST.

    TokenCreator IAM bindings on Compute Engine probe service accounts
    are eventually-consistent; observed convergence in this suite has
    required up to ~3 minutes after the binding is granted. Retry on
    HTTP 403 with
    a 12 x 15s budget so the probe doesn't nondeterministically fail
    with ``iam.serviceAccounts.getAccessToken denied`` against bindings
    that converge a few seconds after the call.
    """
    url = f"{_IAM_CREDENTIALS_BASE}/projects/-/serviceAccounts/{sa_email}:generateAccessToken"
    body = {
        "scope": ["https://www.googleapis.com/auth/cloud-platform"],
        "lifetime": "300s",
    }
    last_error: str | None = None
    for attempt in range(1, attempts + 1):
        try:
            _, response = _http_request("POST", url, token, body=body)
        except urllib.error.HTTPError as e:
            last_error = f"HTTP {e.code}: {_http_error_body(e)}"
            if e.code == 403 and attempt < attempts:
                print(
                    f"  generateAccessToken attempt {attempt}/{attempts} HTTP 403 "
                    f"(propagation); retrying in {delay:.0f}s",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            msg = f"generateAccessToken failed: {last_error}"
            raise RuntimeError(msg) from e
        minted = response.get("accessToken")
        if isinstance(minted, str) and minted:
            return minted
        last_error = f"response missing accessToken: {response}"
        if attempt < attempts:
            time.sleep(delay)
            continue
        msg = f"generateAccessToken failed: {last_error}"
        raise RuntimeError(msg)
    msg = f"generateAccessToken failed after {attempts} attempts: {last_error}"
    raise RuntimeError(msg)


def _probe_serial_console(
    *,
    project: str,
    zone: str,
    instance: str,
    access_token: str,
) -> tuple[int, str]:
    """Probe ``getSerialPortOutput`` with ``access_token``.

    Returns ``(http_status, evidence_text)``. Honest signal: the HTTP
    status comes from a real probe (200 = allowed, 403 = denied, 404 =
    diagnostic gap). The evidence text records the response body / error
    detail for audit.
    """
    url = f"{_COMPUTE_BASE}/projects/{project}/zones/{zone}/instances/{instance}/serialPort?port=1"
    try:
        status, response = _http_request("GET", url, access_token)
    except urllib.error.HTTPError as e:
        body = _http_error_body(e)
        message = body.get("error", {}).get("message", "")
        return e.code, f"HTTP {e.code}: {message or body}"
    contents = response.get("contents") or ""
    return status, f"HTTP {status}: contents_length={len(contents)}"


def _submit_probe_vm_insert(
    *,
    project: str,
    zone: str,
    network: str,
    token: str,
    name: str,
) -> tuple[bool, str, str]:
    """Submit the probe VM insert; return ``(ack_ok, op_name, evidence)``.

    The probe VM is e2-micro with the Debian image family — no GPU, no
    NIM, no persistent disk reuse. It exists solely so the allowed-SA
    probe can produce a real HTTP 403 against an instance the SA was
    NOT granted access to.

    Stamp-before-wait split: this function only submits the insert and
    returns the op_name. The caller stamps its probe-VM cleanup tracker
    BEFORE blocking on ``wait_for_zonal_op`` so a wait-side failure
    still has the partial-create name on disk for teardown.
    """
    url = f"{_COMPUTE_BASE}/projects/{project}/zones/{zone}/instances"
    body = {
        "name": name,
        "machineType": f"zones/{zone}/machineTypes/e2-micro",
        "disks": [
            {
                "boot": True,
                "autoDelete": True,
                "initializeParams": {
                    "sourceImage": "projects/debian-cloud/global/images/family/debian-12",
                    "diskType": f"zones/{zone}/diskTypes/pd-balanced",
                    "diskSizeGb": "10",
                },
            }
        ],
        "networkInterfaces": [
            {
                "network": f"projects/{project}/global/networks/{network}",
            }
        ],
        "labels": {
            "createdby": "isvtest",
            "isv_role": "console-rbac-probe",
        },
    }
    try:
        _, op = _http_request("POST", url, token, body=body)
    except urllib.error.HTTPError as e:
        return False, "", f"insert HTTP {e.code}: {_http_error_body(e)}"
    op_name = op.get("name", "")
    if not op_name:
        return False, "", f"insert response missing operation name: {op}"
    return True, op_name, f"probe VM {name} insert accepted in {zone}"


def _wait_probe_vm_insert(
    *,
    project: str,
    zone: str,
    op_name: str,
) -> tuple[bool, str]:
    """Block on the probe VM insert op; return ``(ok, evidence)``."""
    try:
        wait_for_zonal_op(project, zone, op_name, timeout=300)
    except Exception as e:
        return False, f"insert wait failed: {e}"
    return True, "probe VM insert wait done"


def _delete_probe_vm(
    *,
    project: str,
    zone: str,
    token: str,
    name: str,
    attempts: int = 5,
    backoff: float = 2.0,
) -> bool:
    """Delete the probe VM; NotFound counts as success.

    Bounded retry/backoff on transient delete-submit failures (HTTP 429 / 5xx
    and socket-level errors) mirrors the SA-cleanup envelope so a flaky
    Compute Engine call doesn't orphan the probe VM into the run namespace.
    A wait-side failure is NOT retried here (the delete op is already in
    flight); it surfaces as a cleanup error for the next sweep.
    """
    url = f"{_COMPUTE_BASE}/projects/{project}/zones/{zone}/instances/{name}"
    last_error: str | None = None
    for attempt in range(1, attempts + 1):
        try:
            _, op = _http_request("DELETE", url, token)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return True
            last_error = f"HTTP {e.code}: {_http_error_body(e)}"
            if e.code == 429 or 500 <= e.code < 600:
                time.sleep(backoff * attempt)
                continue
            print(f"  delete_probe_vm({name}) {last_error}", file=sys.stderr)
            return False
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_error = f"network error ({type(e).__name__}): {e}"
            time.sleep(backoff * attempt)
            continue
        op_name = op.get("name", "")
        if op_name:
            try:
                wait_for_zonal_op(project, zone, op_name, timeout=180)
            except Exception as e:
                print(f"  delete_probe_vm wait failed: {e}", file=sys.stderr)
                return False
        return True
    print(f"  delete_probe_vm({name}) exhausted: {last_error}", file=sys.stderr)
    return False


def _self_provisioned_probe(
    *,
    project: str,
    zone: str,
    instance: str,
    network: str,
    result: dict[str, Any],
) -> int:
    """Run the self-provisioned RBAC probe.

    Returns 0 ONLY when every subtest passed AND cleanup succeeded on
    every probe resource. Cleanup runs in the ``finally`` block so the
    SAs / VM / IAM bindings created by this run are removed even on
    partial failure; the ``cleanup_errors`` list is then AND-ed into
    ``result['success']`` and the return code, mirroring the AWS oracle
    (providers/aws/scripts/vm/console_rbac.py — cleanup failures flip
    success to False).
    """
    caller_token = _adc_access_token()
    caller_member = _resolve_caller_member(caller_token)
    result["caller"] = caller_member

    # The GCP service-account local-part (segment before ``@<project>.iam.``)
    # is hard-capped at 30 chars. A run-id-only suffix is NOT enough: the
    # run-id alone collapses two distinct logical SAs onto the same name if a
    # transient in-step cleanup of one fails and the next attempt inside the
    # same run hits 409 ALREADY_EXISTS. Fold a per-invocation discriminator
    # (4 hex chars) BETWEEN the static prefix and the run-id suffix so every
    # invocation gets a fresh name, and so the 30-char truncation can never
    # drop the discriminator or the trailing run-id token. The ``isv-`` prefix
    # + trailing run-id suffix still match the external sweep regex
    # ``^isv-.*-<run_id_suffix>$``. Shorter ``-d`` / ``-a`` prefixes (vs
    # ``denied`` / ``allowed``) keep clear headroom under the cap; the human
    # role stays legible via each SA's displayName.
    run_suffix = unique_suffix("rbac", length=8).split("-", 1)[-1]
    invocation_tag = secrets.token_hex(2)  # 4 hex chars, per-invocation
    denied_sa_id = f"isv-rbac-d-{invocation_tag}-{run_suffix}"[:30]
    allowed_sa_id = f"isv-rbac-a-{invocation_tag}-{run_suffix}"[:30]
    probe_vm_name = f"isv-rbac-probe-{invocation_tag}-{run_suffix}"[:62]

    created: dict[str, Any] = {
        "denied_sa": "",
        "allowed_sa": "",
        "probe_vm": "",
        "token_creator_denied": False,
        "token_creator_allowed": False,
        "target_role_allowed": False,
    }
    cleanup_errors: list[str] = []
    subtests_passed = False
    early_failure: str | None = None

    try:
        # 1. Create probe service accounts.
        print(f"Creating denied probe SA {denied_sa_id}...", file=sys.stderr)
        denied_email = _create_service_account(
            project=project,
            token=caller_token,
            sa_id=denied_sa_id,
            display_name="ISV RBAC denied probe",
        )
        created["denied_sa"] = denied_email

        print(f"Creating allowed probe SA {allowed_sa_id}...", file=sys.stderr)
        allowed_email = _create_service_account(
            project=project,
            token=caller_token,
            sa_id=allowed_sa_id,
            display_name="ISV RBAC allowed probe",
        )
        created["allowed_sa"] = allowed_email

        # 2. Grant the caller TokenCreator on both probe SAs so we can
        #    mint access tokens for them.
        created["token_creator_denied"] = _grant_token_creator(
            project=project,
            token=caller_token,
            sa_email=denied_email,
            member=caller_member,
        )
        created["token_creator_allowed"] = _grant_token_creator(
            project=project,
            token=caller_token,
            sa_email=allowed_email,
            member=caller_member,
        )
        if not (created["token_creator_denied"] and created["token_creator_allowed"]):
            early_failure = "could not grant TokenCreator on probe SAs"
            result["error"] = early_failure
            return 1

        # 3. Grant the allowed SA the serial-output role on the TARGET VM
        #    only. The denied SA gets nothing; the allowed SA's binding is
        #    scoped to this one instance so the resource-scope subtest
        #    against the second VM is a genuine deny.
        created["target_role_allowed"] = _grant_target_role(
            project=project,
            zone=zone,
            instance=instance,
            token=caller_token,
            sa_email=allowed_email,
        )
        if not created["target_role_allowed"]:
            early_failure = "could not grant target-VM role to allowed probe SA"
            result["error"] = early_failure
            return 1

        # 4. Submit the second probe VM. The allowed SA was NOT granted
        #    any role on this VM, so the resource-scope subtest is a real
        #    HTTP 403 (or honest failure if 404 — the instance is missing).
        #    Stamp-before-wait: record the probe VM name in the cleanup
        #    tracker IMMEDIATELY after the insert ack so a wait-side
        #    failure still has the partial-create name on disk for the
        #    finally-block teardown.
        ack_ok, probe_op, ack_evidence = _submit_probe_vm_insert(
            project=project,
            zone=zone,
            network=network,
            token=caller_token,
            name=probe_vm_name,
        )
        if not ack_ok:
            early_failure = f"could not create probe VM: {ack_evidence}"
            result["error"] = early_failure
            return 1
        created["probe_vm"] = probe_vm_name
        wait_ok, wait_evidence = _wait_probe_vm_insert(
            project=project,
            zone=zone,
            op_name=probe_op,
        )
        if not wait_ok:
            early_failure = f"probe VM insert wait failed: {wait_evidence}"
            result["error"] = early_failure
            return 1

        # 5. Mint short-lived access tokens (with TokenCreator-propagation
        #    retry budget — see _mint_access_token) and run the three
        #    subtests.
        denied_token = _mint_access_token(token=caller_token, sa_email=denied_email)
        allowed_token = _mint_access_token(token=caller_token, sa_email=allowed_email)

        denied_status, denied_evidence = _probe_serial_console(
            project=project,
            zone=zone,
            instance=instance,
            access_token=denied_token,
        )
        result["tests"]["denied_principal_cannot_access_console"] = {
            "passed": denied_status == 403,
            "principal": f"serviceAccount:{denied_email}",
            "evidence": denied_evidence,
        }

        allowed_status, allowed_evidence = _probe_serial_console(
            project=project,
            zone=zone,
            instance=instance,
            access_token=allowed_token,
        )
        result["tests"]["allowed_principal_can_access_console"] = {
            "passed": allowed_status == 200,
            "principal": f"serviceAccount:{allowed_email}",
            "evidence": allowed_evidence,
        }

        scope_status, scope_evidence = _probe_serial_console(
            project=project,
            zone=zone,
            instance=probe_vm_name,
            access_token=allowed_token,
        )
        # HTTP 404 is NOT proof of scoping — the resource is missing, so
        # IAM enforcement could not be observed. Only 403 counts.
        result["tests"]["allowed_principal_is_resource_scoped"] = {
            "passed": scope_status == 403,
            "principal": f"serviceAccount:{allowed_email}",
            "evidence": f"probe_vm={probe_vm_name}; {scope_evidence}",
        }

        result["access_restricted"] = (
            result["tests"]["denied_principal_cannot_access_console"]["passed"]
            and result["tests"]["allowed_principal_is_resource_scoped"]["passed"]
        )
        subtests_passed = all(t["passed"] for t in result["tests"].values())
        if not subtests_passed:
            result["error"] = "one or more console RBAC subtests failed; see tests.* evidence"
        # Defer the final success/rc computation to the finally block so
        # cleanup failures can flip success to False (matches AWS oracle).
        return 0  # placeholder — finally overrides with the cleanup-AND-ed rc

    except Exception as e:
        # Capture probe-setup errors (SA create/grant HTTP errors, token
        # mint failures, etc.) so the operator sees a structured root
        # cause rather than a generic three-False-subtest failure. The
        # finally block still runs to clean up partial probe resources.
        early_failure = f"{type(e).__name__}: {e}"
        result["error"] = early_failure
        return 1
    finally:
        # Cleanup runs unconditionally so this stub never leaks probe
        # resources / IAM bindings even on partial failure. Each
        # cleanup helper returns bool and is AND-ed into success.
        if created["target_role_allowed"]:
            ok = _revoke_target_role(
                project=project,
                zone=zone,
                instance=instance,
                token=caller_token,
                sa_email=created["allowed_sa"],
            )
            if not ok:
                cleanup_errors.append(f"revoke target role on {instance}")
        if created["probe_vm"]:
            ok = _delete_probe_vm(
                project=project,
                zone=zone,
                token=caller_token,
                name=created["probe_vm"],
            )
            if not ok:
                cleanup_errors.append(f"delete probe VM {created['probe_vm']}")
        for sa_email, created_flag in (
            (created["denied_sa"], created["token_creator_denied"]),
            (created["allowed_sa"], created["token_creator_allowed"]),
        ):
            if not sa_email:
                continue
            if created_flag:
                if not _revoke_token_creator(
                    project=project,
                    token=caller_token,
                    sa_email=sa_email,
                    member=caller_member,
                ):
                    cleanup_errors.append(f"revoke tokenCreator on {sa_email}")
            if not _delete_service_account(project=project, token=caller_token, email=sa_email):
                cleanup_errors.append(f"delete service account {sa_email}")
        if cleanup_errors:
            result["cleanup_errors"] = cleanup_errors
        # Final success/rc: subtests AND cleanup AND no early failure.
        final_success = subtests_passed and not cleanup_errors and early_failure is None
        result["success"] = final_success
        if cleanup_errors and not result.get("error"):
            result["error"] = "console RBAC cleanup failed: " + "; ".join(cleanup_errors)
        elif cleanup_errors and result.get("error"):
            result["error"] = f"{result['error']}; cleanup failed: {'; '.join(cleanup_errors)}"
        return 0 if final_success else 1


def _preprovisioned_probe(
    *,
    project: str,
    zone: str,
    instance: str,
    denied_sa: str,
    allowed_sa: str,
    other_instance: str,
    other_zone: str,
    result: dict[str, Any],
) -> int:
    """Run the pre-provisioned RBAC probe with operator-supplied principals.

    The fallback path for projects where IAM mutation is not allowed.
    Operators must pre-create denied / allowed SAs, grant the caller
    TokenCreator on both, scope the allowed SA's serial-output role to
    the target VM, and create a real ``GCP_OTHER_INSTANCE_ID`` that the
    allowed SA has NOT been granted access to.

    Workflow exceptions (ADC failure, token-mint propagation timeout,
    HTTP errors from getSerialPortOutput) are caught here so the
    contract-shaped result populated by ``main()`` survives — mirrors
    the AWS oracle's try/except around its console RBAC workflow.
    Escaping to ``handle_gcp_errors`` would drop ``platform``,
    ``test_name``, ``rbac_model``, ``access_restricted``, and
    ``tests.*`` from the printed JSON.
    """
    try:
        caller_token = _adc_access_token()
        result["caller"] = _resolve_caller_member(caller_token)
        denied_token = _mint_access_token(token=caller_token, sa_email=denied_sa)
        allowed_token = _mint_access_token(token=caller_token, sa_email=allowed_sa)

        denied_status, denied_evidence = _probe_serial_console(
            project=project,
            zone=zone,
            instance=instance,
            access_token=denied_token,
        )
        result["tests"]["denied_principal_cannot_access_console"] = {
            "passed": denied_status == 403,
            "principal": f"serviceAccount:{denied_sa}",
            "evidence": denied_evidence,
        }

        allowed_status, allowed_evidence = _probe_serial_console(
            project=project,
            zone=zone,
            instance=instance,
            access_token=allowed_token,
        )
        result["tests"]["allowed_principal_can_access_console"] = {
            "passed": allowed_status == 200,
            "principal": f"serviceAccount:{allowed_sa}",
            "evidence": allowed_evidence,
        }

        scope_status, scope_evidence = _probe_serial_console(
            project=project,
            zone=other_zone,
            instance=other_instance,
            access_token=allowed_token,
        )
        result["tests"]["allowed_principal_is_resource_scoped"] = {
            "passed": scope_status == 403,
            "principal": f"serviceAccount:{allowed_sa}",
            "evidence": f"other_instance={other_instance}; {scope_evidence}",
        }

        result["access_restricted"] = (
            result["tests"]["denied_principal_cannot_access_console"]["passed"]
            and result["tests"]["allowed_principal_is_resource_scoped"]["passed"]
        )
        all_passed = all(t["passed"] for t in result["tests"].values())
        result["success"] = all_passed
        if not all_passed:
            result["error"] = "one or more console RBAC subtests failed; see tests.* evidence"
        return 0 if all_passed else 1
    except Exception as e:
        result["success"] = False
        result["access_restricted"] = False
        result["error"] = str(e)
        return 1


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Console RBAC probe (Compute Engine)")
    parser.add_argument("--instance-id", required=True, help="Target instance name")
    parser.add_argument("--region", required=True, help="GCP region or zone")
    parser.add_argument("--zone", default=None, help="GCP zone (overrides region)")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    parser.add_argument(
        "--network",
        default="default",
        help="Network for the probe VM (self-provisioned path)",
    )
    args = parser.parse_args()

    project = resolve_project(args.project)
    zone = args.zone or narrow_region_to_zone(args.region)

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "test_name": "console_rbac",
        "instance_id": args.instance_id,
        "project": project,
        "zone": zone,
        "rbac_model": "gcp-iam",
        "access_restricted": False,
        "restricted_actions": list(_RESTRICTED_ACTIONS),
        "tests": {
            "denied_principal_cannot_access_console": {"passed": False, "principal": "", "evidence": ""},
            "allowed_principal_can_access_console": {"passed": False, "principal": "", "evidence": ""},
            "allowed_principal_is_resource_scoped": {"passed": False, "principal": "", "evidence": ""},
        },
    }

    denied_sa = os.environ.get(_DENIED_SA_ENV, "").strip()
    allowed_sa = os.environ.get(_ALLOWED_SA_ENV, "").strip()
    other_instance = os.environ.get(_OTHER_INSTANCE_ENV, "").strip()
    other_zone = os.environ.get(_OTHER_INSTANCE_ZONE_ENV, "").strip() or zone

    # The pre-provisioned fallback runs only when the operator supplies
    # all three env vars. Otherwise the default self-provisioned path
    # runs (unless explicitly disabled via _SELF_PROVISION_ENABLED_ENV=0).
    if denied_sa and allowed_sa and other_instance:
        result["mode"] = "preprovisioned"
        rc = _preprovisioned_probe(
            project=project,
            zone=zone,
            instance=args.instance_id,
            denied_sa=denied_sa,
            allowed_sa=allowed_sa,
            other_instance=other_instance,
            other_zone=other_zone,
            result=result,
        )
        print(json.dumps(result, indent=2, default=str))
        return rc

    if os.environ.get(_SELF_PROVISION_ENABLED_ENV, "1").strip() in {"0", "false", "no"}:
        # Intentional opt-out via env var. Treat as a clean policy-skip
        # (rc=0, success=True, skipped=True) — same shape as deploy_nim's
        # missing-NGC_API_KEY skip — so the orchestrator's
        # StepSuccessCheck reads this as "step short-circuited cleanly,"
        # not as a failed RBAC probe.
        result["mode"] = "skipped"
        result["skipped"] = True
        result["success"] = True
        result["skip_reason"] = (
            f"{_SELF_PROVISION_ENABLED_ENV} disables the self-provisioned probe and no "
            f"{_DENIED_SA_ENV} / {_ALLOWED_SA_ENV} / {_OTHER_INSTANCE_ENV} fallback was supplied"
        )
        print(json.dumps(result, indent=2, default=str))
        return 0

    result["mode"] = "self_provisioned"
    rc = _self_provisioned_probe(
        project=project,
        zone=zone,
        instance=args.instance_id,
        network=args.network,
        result=result,
    )
    print(json.dumps(result, indent=2, default=str))
    return rc


if __name__ == "__main__":
    sys.exit(main())
