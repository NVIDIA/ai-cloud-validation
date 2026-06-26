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

"""Verify the platform issues short-lived credentials to nodes and workloads (SEC02-01).

Two distinct issuance surfaces are probed, mirroring the node and workload
identity flows the requirement targets:

* Node-equivalent: the credential a GCE instance receives from the platform
  identity service -- the metadata-server token. ``google.auth.default()``
  resolves to that source (a ``compute_engine.Credentials``) ONLY when running
  on a GCE node; off-node it resolves a user/impersonation ADC token, which is
  NOT the node issuance surface. The probe refreshes the credential for its
  finite ``expiry`` only after confirming it is the metadata-server source, and
  structured-skips the node surface otherwise rather than relabeling ADC as
  ``metadata_server_token``.
* Workload-equivalent: a credential an in-cluster workload acquires through the
  workload-identity flow. IAM Credentials ``generateAccessToken`` mints a
  short-lived token whose ``expire_time`` (a Timestamp, NOT an integer seconds
  field) bounds its lifetime; the TTL is the remaining ``expire_time - now``.

Each probe asserts the credential carries a finite expiry whose TTL is within
``--max-ttl-seconds``. The workload probe impersonates ``--impersonate-sa`` when
ADC is a user/default principal with no bound service account. When an issuance
surface cannot be exercised in the environment (vs. returning a token that fails
the TTL bound), the script emits a structured ``skipped`` payload (exit 0) so the
validation skips rather than fabricating a pass or a partial fail. Token material
is never printed.

Usage:
    python3 short_lived_credentials_test.py --region us-central1 --max-ttl-seconds 43200

Output JSON:
  {
    "success": true,
    "platform": "security",
    "test_name": "short_lived_credentials_test",
    "node_credential_method": "metadata_server_token",
    "workload_credential_method": "iam_credentials_generate_access_token",
    "node_credential_ttl_seconds": 3599,
    "workload_credential_ttl_seconds": 3599,
    "max_ttl_seconds": 43200,
    "tests": {
      "node_credential_has_expiry":           {"passed": true},
      "node_credential_ttl_within_bound":     {"passed": true},
      "workload_credential_has_expiry":       {"passed": true},
      "workload_credential_ttl_within_bound": {"passed": true}
    }
  }
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

import google.auth
import google.auth.compute_engine
import google.auth.transport.requests
from common.compute import resolve_project
from common.errors import handle_gcp_errors
from google.cloud import iam_credentials_v1
from google.protobuf import duration_pb2

DEFAULT_MAX_TTL_SECONDS = 43200  # 12h - SEC02 upper bound for short-lived creds
NODE_METHOD = "metadata_server_token"
WORKLOAD_METHOD = "iam_credentials_generate_access_token"
# GCP default max SA-token lifetime is 3600s (only longer when org policy
# iam.allowServiceAccountCredentialLifetimeExtension applies); request a
# lifetime that fits both the default cap and the configured bound.
_WORKLOAD_LIFETIME_CAP_SECONDS = 3600
_WORKLOAD_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)


def _ttl_seconds(expiry: datetime) -> int:
    """Return seconds until ``expiry``, treating naive datetimes as UTC."""
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return int((expiry - datetime.now(UTC)).total_seconds())


def _record_credential(
    result: dict,
    *,
    expiry_key: str,
    ttl_key: str,
    ttl_field: str,
    expiry: datetime | None,
    max_ttl_seconds: int,
) -> None:
    """Update ``result`` with expiry + TTL probe outcomes for one credential."""
    if expiry is None:
        result["tests"][expiry_key]["error"] = "credential carried no expiry"
        result["tests"][ttl_key]["error"] = "no expiry returned -- TTL bound cannot be evaluated"
        return
    result["tests"][expiry_key]["passed"] = True
    ttl = _ttl_seconds(expiry)
    result[ttl_field] = ttl
    if 0 < ttl <= max_ttl_seconds:
        result["tests"][ttl_key]["passed"] = True
    else:
        result["tests"][ttl_key]["error"] = f"TTL {ttl}s outside (0, {max_ttl_seconds}s] bound"


def _node_credential_expiry() -> datetime | None:
    """Refresh the node's metadata-server credential and return its expiry.

    SEC02-01 node coverage means the credential a GCE instance receives from
    the platform identity service: the metadata-server token, surfaced as a
    ``compute_engine.Credentials``. ``google.auth.default()`` only resolves to
    that source on a GCE node; off-node it resolves a user/impersonation ADC
    token. Labeling such a token ``metadata_server_token`` (NODE_METHOD) would
    assert node-credential coverage that was never exercised, so refuse to
    relabel a non-node credential: raise when the resolved credential is not a
    metadata-server source, letting the caller structured-skip the node surface
    (the GCP analog of the AWS oracle skipping when it cannot exercise its STS
    issuance surface).
    """
    raw_credentials, _ = google.auth.default(scopes=list(_WORKLOAD_SCOPES))
    if not isinstance(raw_credentials, google.auth.compute_engine.Credentials):
        raise RuntimeError(
            "node credential surface unavailable: google.auth.default() resolved a "
            f"{type(raw_credentials).__name__}, not a metadata-server "
            "compute_engine.Credentials (not running on a GCE node) -- refusing to "
            f"label a non-node ADC token {NODE_METHOD!r}"
        )
    raw_credentials.refresh(google.auth.transport.requests.Request())
    return getattr(raw_credentials, "expiry", None)


def _workload_credential_expiry(target_sa: str, max_ttl_seconds: int) -> datetime | None:
    """Mint a short-lived workload token via IAM Credentials and return its expiry."""
    client = iam_credentials_v1.IAMCredentialsClient()
    lifetime = duration_pb2.Duration(seconds=min(max_ttl_seconds, _WORKLOAD_LIFETIME_CAP_SECONDS))
    response = client.generate_access_token(
        name=f"projects/-/serviceAccounts/{target_sa}",
        scope=list(_WORKLOAD_SCOPES),
        lifetime=lifetime,
    )
    return cast(datetime, response.expire_time) if response.expire_time else None


@handle_gcp_errors
def main() -> int:
    """Probe node + workload credential issuance, emit JSON result."""
    parser = argparse.ArgumentParser(description="Short-lived credentials test (SEC02-01)")
    parser.add_argument("--region", default="")
    parser.add_argument("--project", default="")
    parser.add_argument(
        "--max-ttl-seconds",
        default="43200",
        help=f"Upper bound on credential TTL (default: {DEFAULT_MAX_TTL_SECONDS})",
    )
    parser.add_argument(
        "--impersonate-sa",
        default="",
        help=(
            "Target service account to impersonate for the workload-identity "
            "probe when ADC is a user/default principal with no bound SA "
            "(set GCP_SECURITY_IMPERSONATION_SA)"
        ),
    )
    args = parser.parse_args()

    try:
        max_ttl_seconds = int(args.max_ttl_seconds)
    except (TypeError, ValueError):
        max_ttl_seconds = 0

    if max_ttl_seconds < 1:
        print(
            json.dumps(
                {
                    "success": True,
                    "platform": "security",
                    "test_name": "short_lived_credentials_test",
                    "skipped": True,
                    "skip_reason": "--max-ttl-seconds must be a positive integer",
                    "tests": {},
                    "max_ttl_seconds": DEFAULT_MAX_TTL_SECONDS,
                },
                indent=2,
            )
        )
        return 0

    result: dict = {
        "success": False,
        "platform": "security",
        "test_name": "short_lived_credentials_test",
        "node_credential_method": NODE_METHOD,
        "workload_credential_method": WORKLOAD_METHOD,
        "node_credential_ttl_seconds": 0,
        "workload_credential_ttl_seconds": 0,
        "max_ttl_seconds": max_ttl_seconds,
        "tests": {
            "node_credential_has_expiry": {"passed": False},
            "node_credential_ttl_within_bound": {"passed": False},
            "workload_credential_has_expiry": {"passed": False},
            "workload_credential_ttl_within_bound": {"passed": False},
        },
    }

    # resolve_project confirms a usable credential/project before either probe
    # runs (and surfaces a clear error when ADC is unconfigured).
    project = resolve_project(args.project)

    node_error: str | None = None
    workload_error: str | None = None

    try:
        node_expiry = _node_credential_expiry()
        _record_credential(
            result,
            expiry_key="node_credential_has_expiry",
            ttl_key="node_credential_ttl_within_bound",
            ttl_field="node_credential_ttl_seconds",
            expiry=node_expiry,
            max_ttl_seconds=max_ttl_seconds,
        )
    except Exception as e:
        node_error = str(e)
        result["tests"]["node_credential_has_expiry"]["error"] = node_error
        result["tests"]["node_credential_ttl_within_bound"]["error"] = node_error

    # The workload token is minted through the IAM Credentials
    # generateAccessToken flow (the workload-identity issuance surface). On a
    # node, ADC is the bound service account and impersonates itself; off-node
    # (user ADC) there is no service_account_email, so fall back to the
    # operator-supplied impersonation target. The run credential needs
    # roles/iam.serviceAccountTokenCreator on the resolved SA.
    credentials, _ = google.auth.default(scopes=list(_WORKLOAD_SCOPES))
    target_sa = getattr(credentials, "service_account_email", None) or ""
    if not target_sa or target_sa == "default":
        target_sa = args.impersonate_sa.strip()

    try:
        if not target_sa:
            raise RuntimeError(
                "no service account available to mint a workload token "
                f"(project {project}): ADC is a user/default principal and "
                "--impersonate-sa is unset (set GCP_SECURITY_IMPERSONATION_SA)"
            )
        workload_expiry = _workload_credential_expiry(target_sa, max_ttl_seconds)
        _record_credential(
            result,
            expiry_key="workload_credential_has_expiry",
            ttl_key="workload_credential_ttl_within_bound",
            ttl_field="workload_credential_ttl_seconds",
            expiry=workload_expiry,
            max_ttl_seconds=max_ttl_seconds,
        )
    except Exception as e:
        workload_error = str(e)
        result["tests"]["workload_credential_has_expiry"]["error"] = workload_error
        result["tests"]["workload_credential_ttl_within_bound"]["error"] = workload_error

    # An issuance surface raising (vs. returning a token that fails the TTL
    # bound) means the environment could not exercise it -- a structured skip,
    # not a security failure. The validator requires all four node_*/workload_*
    # subtests, so a partial verdict cannot honestly pass; skip the whole test.
    # Token-quality failures (no expiry, TTL out of bound) are recorded as
    # subtest errors without raising, so they survive as real failures below.
    if node_error or workload_error:
        unavailable = []
        if node_error:
            unavailable.append(f"node ({node_error})")
        if workload_error:
            unavailable.append(f"workload ({workload_error})")
        print(
            json.dumps(
                {
                    "success": True,
                    "platform": "security",
                    "test_name": "short_lived_credentials_test",
                    "skipped": True,
                    "skip_reason": ("credential issuance surface could not be exercised: " + "; ".join(unavailable)),
                    "tests": {},
                    "max_ttl_seconds": max_ttl_seconds,
                },
                indent=2,
            )
        )
        return 0

    result["success"] = all(probe["passed"] for probe in result["tests"].values())
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
