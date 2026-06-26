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

"""Verify a service account can obtain credentials and authenticate as itself.

The property under test is "the workload can authenticate as the service
account", not the specific key material. The AWS reference creates a temporary
IAM user, mints a long-lived access key, and proves it via STS
GetCallerIdentity. On GCP the portable equivalent is keyless service-account
impersonation: hardened orgs enforce ``iam.disableServiceAccountKeyCreation``,
so minting a long-lived SA key returns 400 FAILED_PRECONDITION and is not a
portable path.

This script obtains a short-lived OAuth2 access token for a target service
account via IAM Credentials ``generateAccessToken`` (using the run credential's
``roles/iam.serviceAccountTokenCreator`` grant on that SA), then resolves the
identity behind the token from the OAuth2 tokeninfo endpoint and requires it to
match the impersonated SA. The token MUST be minted with the userinfo.email
scope or tokeninfo returns only the numeric ``azp`` and the identity cannot be
confirmed. Nothing is created, so there is no teardown. The access token is
never printed to stdout or stderr.

Usage:
    python3 sa_credential_test.py --region us-central1 \\
        --impersonate-sa sa@proj.iam.gserviceaccount.com --project=proj

Output JSON:
  {
    "success": true,
    "platform": "security",
    "test_name": "sa_credential_test",
    "authenticated": true,
    "credential_type": "oauth2_token",
    "credential_source": "impersonation",
    "identity": "sa@proj.iam.gserviceaccount.com",
    "expires_at": "2026-06-26T12:00:00+00:00"
  }
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project
from common.errors import handle_gcp_errors
from google.cloud import iam_credentials_v1

# Scopes minted into the impersonation token. userinfo.email is REQUIRED so the
# tokeninfo endpoint returns the human-readable SA email (``email``) and not
# just the opaque numeric ``azp``; cloud-platform keeps the token usable for a
# real API call should a caller want to extend the check.
_TOKEN_SCOPES = (
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/cloud-platform",
)
_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
_HTTP_TIMEOUT_S = 30


def _tokeninfo(token: str) -> dict:
    """Fetch OAuth2 tokeninfo for ``token``. Raises on HTTP / transport error."""
    query = urllib.parse.urlencode({"access_token": token})
    request = urllib.request.Request(f"{_TOKENINFO_URL}?{query}", method="GET")
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_S) as response:
        return json.loads(response.read().decode("utf-8"))


@handle_gcp_errors
def main() -> int:
    """Prove SA authentication via impersonation and emit JSON result."""
    parser = argparse.ArgumentParser(description="Service account credential test")
    parser.add_argument("--region", default="")
    parser.add_argument("--project", default="")
    parser.add_argument(
        "--impersonate-sa",
        default="",
        help="Target service account email to impersonate (keyless proof)",
    )
    args = parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "security",
        "test_name": "sa_credential_test",
        "authenticated": False,
        "credential_type": "oauth2_token",
        "credential_source": "impersonation",
        "identity": "",
        "expires_at": None,
    }

    target_sa = args.impersonate_sa.strip()
    if not target_sa:
        result["error"] = (
            "--impersonate-sa is required: set GCP_SECURITY_IMPERSONATION_SA to a "
            "service account the run credential holds roles/iam.serviceAccountTokenCreator on"
        )
        print(json.dumps(result, indent=2))
        return 1

    try:
        # resolve_project documents the active project even though the IAM
        # Credentials resource name uses the wildcard project (projects/-).
        resolve_project(args.project)

        client = iam_credentials_v1.IAMCredentialsClient()
        name = f"projects/-/serviceAccounts/{target_sa}"
        token_response = client.generate_access_token(
            name=name,
            scope=list(_TOKEN_SCOPES),
        )
        access_token = token_response.access_token
        # expire_time is a tz-aware Timestamp -> datetime; record the expiry but
        # never the token itself.
        expire_time = token_response.expire_time
        result["expires_at"] = cast(datetime, expire_time).isoformat() if expire_time else None

        # Resolve the identity behind the token. With the userinfo.email scope
        # tokeninfo returns the SA email in ``email``; ``azp`` is only the
        # numeric client id and cannot confirm the human-readable identity.
        info = _tokeninfo(access_token)
        identity = str(info.get("email") or "")
        result["identity"] = identity

        if identity != target_sa:
            result["error"] = (
                f"token identity {identity!r} does not match impersonated "
                f"service account {target_sa!r} (request the userinfo.email scope)"
            )
        else:
            # Stamp authenticated only once the token's identity is confirmed to
            # be the impersonated SA — a wrong-identity token is not an
            # authenticated success, so the flag must not lead the identity match.
            result["authenticated"] = True
            result["success"] = True
    except urllib.error.URLError as e:
        # tokeninfo transport/HTTP failure: the token may have been minted but
        # its identity could not be confirmed.
        result["error"] = f"tokeninfo lookup failed: {e}"
    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
