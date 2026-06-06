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

"""Verify a GCP service-account impersonation token authenticates (IAM test_credentials).

The AWS reference builds a boto3 session from the created access key and proves
it authenticates via STS GetCallerIdentity. The GCP analog of "these
credentials work" for a short-lived impersonation token is:

  1. Call the OAuth2 tokeninfo endpoint for the token. Require
     ``tokeninfo.azp == --credential-id`` (the service account unique_id
     emitted as create_user.access_key_id) and ``expires_in > 0``. This proves
     the token is live AND belongs to the expected identity.
  2. Build google.oauth2.credentials.Credentials(token=...) and call
     IAMClient.get_service_account for the expected service account. Success is
     authenticated access; PermissionDenied still proves the token
     authenticated but lacks that specific permission (limited permissions are
     not a failure). Unauthenticated / token expiry / transport errors are
     credential failures.

``account_id`` is derived from the project segment of the service-account email
(or the --project argument), preserving the AWS-shaped field name the suite's
``credentials`` validation requires.

The access token must never be printed to stderr or diagnostic logs.

Usage:
    python3 test_credentials.py --username sa@proj.iam.gserviceaccount.com \\
        --credential-id 1234567890 --credential-secret ya29... --project=proj

Output JSON:
{
    "success": true,
    "platform": "iam",
    "account_id": "my-project",
    "identity_id": "1234567890",
    "tests": {
        "identity": {"passed": true, "identity": "1234567890", "expires_in": 599},
        "access": {"passed": true, "note": "iam_get_self_ok"}
    }
}
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

import google.oauth2.credentials
from common.errors import TRANSIENT_EXCEPTIONS, handle_gcp_errors
from google.api_core import exceptions as gax
from google.cloud import iam_admin_v1

_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
_HTTP_TIMEOUT_S = 30

# tokeninfo / IAM-read retry budget within the 120s step timeout. The
# tokenCreator binding already propagated during create_user (it minted the
# token), so this is a short hedge against transient backend errors, not the
# full propagation budget.
_TOKENINFO_MAX_RETRIES = 4
_TOKENINFO_RETRY_DELAY_SECONDS = 5
_READ_MAX_RETRIES = 6
_READ_RETRY_DELAY_SECONDS = 10

# IAM self-read shapes worth retrying: the service account not yet visible
# (NotFound) or a transient backend error. Authenticated-but-limited
# (PermissionDenied) and Unauthenticated are terminal and handled explicitly.
_RETRYABLE_READ: tuple[type[Exception], ...] = (gax.NotFound, *TRANSIENT_EXCEPTIONS)


def _project_from_email(email: str) -> str:
    """Derive the project ID from a ``<id>@<project>.iam.gserviceaccount.com`` email."""
    if "@" in email:
        host = email.split("@", 1)[1]
        if host.endswith(".iam.gserviceaccount.com"):
            return host.split(".", 1)[0]
    return email


def _tokeninfo(token: str) -> dict:
    """Fetch OAuth2 tokeninfo for ``token``. Raises on HTTP / transport error."""
    query = urllib.parse.urlencode({"access_token": token})
    request = urllib.request.Request(f"{_TOKENINFO_URL}?{query}", method="GET")
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_S) as response:
        return json.loads(response.read().decode("utf-8"))


def _tokeninfo_with_retry(token: str) -> dict:
    """Call tokeninfo, retrying only on transport / 5xx errors (4xx is terminal)."""
    last_error: str | None = None
    for attempt in range(1, _TOKENINFO_MAX_RETRIES + 1):
        try:
            return _tokeninfo(token)
        except urllib.error.HTTPError as e:
            # 4xx (e.g. 400 invalid_token) is a real credential failure; only
            # 5xx is transient and worth retrying.
            if e.code < 500 or attempt >= _TOKENINFO_MAX_RETRIES:
                raise
            last_error = f"HTTP {e.code}"
        except urllib.error.URLError as e:
            if attempt >= _TOKENINFO_MAX_RETRIES:
                raise
            last_error = str(e)
        time.sleep(_TOKENINFO_RETRY_DELAY_SECONDS)
    msg = f"tokeninfo did not succeed: {last_error}"
    raise RuntimeError(msg)


def _iam_self_read(token: str, sa_email: str) -> dict:
    """Probe IAM get_service_account with the minted token.

    Returns a ``tests.access``-shaped dict. ``passed`` is True when the token
    authenticates (a successful read OR PermissionDenied, which still proves
    authentication with limited permissions) and False for Unauthenticated,
    transport errors, or exhausted transient retries.
    """
    credentials = google.oauth2.credentials.Credentials(token=token)
    client = iam_admin_v1.IAMClient(credentials=credentials)
    name = f"projects/-/serviceAccounts/{sa_email}"

    last_error: str | None = None
    for attempt in range(1, _READ_MAX_RETRIES + 1):
        try:
            client.get_service_account(name=name)
            return {"passed": True, "note": "iam_get_self_ok"}
        except gax.PermissionDenied:
            # Authenticated but lacks iam.serviceAccounts.get — the token is
            # still valid; limited permissions are not a credential failure.
            return {"passed": True, "note": "permission_denied_expected"}
        except gax.Unauthenticated as e:
            return {"passed": False, "note": "unauthenticated", "error": str(e)}
        except _RETRYABLE_READ as e:
            last_error = f"{type(e).__name__}: {e}"
            if attempt < _READ_MAX_RETRIES:
                time.sleep(_READ_RETRY_DELAY_SECONDS)
                continue
            return {"passed": False, "note": "transient_exhausted", "error": last_error}
        except Exception as e:
            return {"passed": False, "note": "transport_error", "error": str(e)}
    return {"passed": False, "note": "transient_exhausted", "error": last_error}


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a GCP service-account impersonation token")
    parser.add_argument("--username", required=True, help="Service account email from create_user")
    parser.add_argument("--credential-id", required=True, help="Service account unique_id (== tokeninfo.azp)")
    parser.add_argument("--credential-secret", required=True, help="Short-lived OAuth2 access token")
    parser.add_argument("--project", default="", help="GCP project; falls back to the SA email's project")
    args = parser.parse_args()

    account_id = args.project.strip() or _project_from_email(args.username)
    result: dict = {
        "success": False,
        "platform": "iam",
        "account_id": account_id,
        "tests": {},
    }

    # 1. Prove the token is live for the expected identity via tokeninfo.
    try:
        info = _tokeninfo_with_retry(args.credential_secret)
    except (urllib.error.URLError, RuntimeError) as e:
        result["error_type"] = "credentials_invalid"
        result["error"] = f"tokeninfo failed: {e}"
        result["tests"]["identity"] = {"passed": False}
        print(json.dumps(result, indent=2))
        return 1

    azp = str(info.get("azp") or info.get("sub") or "")
    expires_in = int(info.get("expires_in") or 0)
    if azp != args.credential_id:
        result["error_type"] = "credentials_invalid"
        result["error"] = f"tokeninfo azp {azp!r} != expected credential id {args.credential_id!r}"
        result["tests"]["identity"] = {"passed": False, "identity": azp}
        print(json.dumps(result, indent=2))
        return 1
    if expires_in <= 0:
        result["error_type"] = "credentials_expired"
        result["error"] = "access token has expired (expires_in <= 0)"
        result["tests"]["identity"] = {"passed": False, "expires_in": expires_in}
        print(json.dumps(result, indent=2))
        return 1
    result["identity_id"] = azp
    result["tests"]["identity"] = {"passed": True, "identity": azp, "expires_in": expires_in}

    # 2. Authenticated self-read with the minted token.
    result["tests"]["access"] = _iam_self_read(args.credential_secret, args.username)

    result["success"] = result["tests"]["identity"]["passed"] and result["tests"]["access"]["passed"]
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
