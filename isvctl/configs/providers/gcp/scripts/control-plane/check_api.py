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

"""Check GCP control-plane API connectivity and health.

Translates the AWS reference ``check_api`` onto GCP. The AWS oracle authenticates
with STS, records the account id, then probes each configured service with a
read-only call; access-denied still proves endpoint reachability. On GCP:

  * Application Default Credentials resolve both the credential and the project
    id (``google.auth.default()``); the resolved project id is the ``account_id``.
  * Each configured service is probed with one lightweight read-only API call
    (Cloud Storage ``list_buckets``, IAM Admin ``list_service_accounts``,
    Resource Manager ``get_project``). A ``PermissionDenied`` still proves the
    endpoint is reachable and authenticated, so it counts as ``passed`` with a
    visible note (mirroring the oracle's access-denied handling).
  * Top-level ``success`` is gated on ADC resolving AND the authenticated Cloud
    Storage probe succeeding (the object-storage endpoint is the DATASVC-XX-01
    "authenticated endpoint" surface); individual service results stay visible.

Usage:
    python3 check_api.py --region us-central1 --services storage,iam,resourcemanager

Output JSON:
{
    "success": true,
    "platform": "control_plane",
    "region": "us-central1",
    "account_id": "my-project",
    "tests": {
        "storage": {"passed": true, "latency_ms": 123.4},
        "iam": {"passed": true, "latency_ms": 89.1},
        "resourcemanager": {"passed": true, "latency_ms": 45.2}
    }
}
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project
from common.errors import classify_gcp_error, handle_gcp_errors, retry_idempotent
from google.api_core import exceptions as gax
from google.cloud import iam_admin_v1, resourcemanager_v3, storage

# The authenticated object-storage probe gates top-level success (the
# DATASVC-XX-01 authenticated-endpoint surface). Keep it in the service list so
# an operator can drop the others without losing the success signal.
_STORAGE_SERVICE = "storage"

# classify_gcp_error prefixes every probe error with a "[bucket=<name>] " token
# (the shared google.api_core disposition). Recover that bucket for the
# top-level error_type so the orchestration summary keeps the disposition.
_BUCKET_TOKEN_RE = re.compile(r"^\[bucket=([^\]]+)\]")


def _bucket_token(message: str) -> str:
    """Return the classified disposition bucket embedded in ``message``.

    ``classify_gcp_error`` renders ``[bucket=<name>] <detail>``; extract
    ``<name>`` so it can populate the structured ``error_type``. Falls back to
    ``api_unreachable`` when the message carries no token (e.g. a plain
    "probe did not pass" placeholder).
    """
    match = _BUCKET_TOKEN_RE.match(message.strip())
    return match.group(1) if match else "api_unreachable"


def _probe_service(service: str, project: str) -> dict[str, Any]:
    """Run one read-only API call for ``service`` and report a reachability result.

    ``passed`` is derived from the real API result: a successful call passes; a
    ``PermissionDenied`` (403) still proves the endpoint is reachable and the
    request was authenticated, so it passes with a visible note (the oracle's
    access-denied-is-reachable rule). Only a credential / transport / server
    failure fails the probe, with the classified ``[bucket=...]`` error visible.
    """
    result: dict[str, Any] = {"passed": False}
    start = time.time()
    try:
        if service == _STORAGE_SERVICE:
            client = storage.Client(project=project)
            # Consume one item so the lazy iterator issues a real authenticated call.
            retry_idempotent(
                lambda: next(iter(client.list_buckets(max_results=1)), None),
                op_desc="storage.list_buckets",
            )
        elif service == "iam":
            client = iam_admin_v1.IAMClient()
            retry_idempotent(
                lambda: next(iter(client.list_service_accounts(name=f"projects/{project}")), None),
                op_desc="iam.list_service_accounts",
            )
        elif service in ("resourcemanager", "resource-manager", "resourcemanager_v3"):
            client = resourcemanager_v3.ProjectsClient()
            retry_idempotent(
                client.get_project,
                name=f"projects/{project}",
                op_desc="resourcemanager.get_project",
            )
        else:
            result["error"] = f"unknown service '{service}'"
            return result
        result["passed"] = True
        result["latency_ms"] = round((time.time() - start) * 1000, 2)
    except (gax.PermissionDenied, gax.Forbidden) as e:
        # 403 == authenticated but not authorized -> endpoint proven reachable.
        # gRPC clients (IAM, Resource Manager) raise PermissionDenied; the HTTP/JSON
        # Cloud Storage client maps a 403 to the superclass Forbidden, which a
        # PermissionDenied-only handler misses. Catch both (Forbidden covers the
        # gRPC subclass too) without broadening to any other error class.
        result["passed"] = True
        result["latency_ms"] = round((time.time() - start) * 1000, 2)
        result["note"] = f"API reachable (access denied): {e}"
    except Exception as e:  # classify and keep the probe result visible
        _bucket, message = classify_gcp_error(e)
        result["error"] = message
    return result


@handle_gcp_errors
def main() -> int:
    """Probe GCP control-plane API health and print a structured JSON result."""
    parser = argparse.ArgumentParser(description="Check GCP control-plane API health")
    parser.add_argument("--region", default="", help="Cloud Storage bucket location (contract parity)")
    parser.add_argument("--services", default="storage,iam,resourcemanager", help="Comma-separated GCP services")
    parser.add_argument("--project", default="", help="GCP project id (falls back to ADC)")
    args = parser.parse_args()

    services = [s.strip() for s in args.services.split(",") if s.strip()]

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "region": args.region,
        "account_id": "",
        "tests": {},
    }

    # Resolve credentials + project id centrally (ADC). A resolution failure is a
    # structured credential error, not an uncaught crash.
    try:
        project = resolve_project(args.project or None)
    except Exception as e:  # surface as structured credential error
        _bucket, message = classify_gcp_error(e)
        result["error"] = message
        print(json.dumps(result, indent=2))
        return 1

    result["account_id"] = project

    tests: dict[str, Any] = {}
    for service in services:
        tests[service] = _probe_service(service, project)
    result["tests"] = tests

    passed = sum(1 for t in tests.values() if t.get("passed"))
    result["summary"] = f"{passed}/{len(tests)} services reachable"

    # Success is gated on the authenticated Cloud Storage probe (the object-store
    # endpoint). If storage was not requested, fall back to all requested probes
    # passing so success still reflects a real authenticated result.
    if _STORAGE_SERVICE in tests:
        result["success"] = bool(tests[_STORAGE_SERVICE].get("passed"))
    else:
        result["success"] = bool(tests) and all(t.get("passed") for t in tests.values())

    # Surface the gating probe's failure reason as a top-level structured error.
    # The orchestrator's failure summary echoes a top-level `error`/`error_type`
    # (see step_executor._parse_output usage) but only reaches into the JSON's
    # top level -- a per-service `tests.<svc>.error` stays hidden, leaving the
    # operator with a bare "Command exited with code 1". Mirror the
    # resolve_project failure path (which already sets a top-level error) so any
    # failed probe reports WHY. Prefer the success-gating storage probe, then the
    # first other failing probe; reuse the probe's classified [bucket=...] token
    # as error_type so the disposition survives into the summary line.
    if not result["success"]:
        gating = tests.get(_STORAGE_SERVICE)
        if gating is not None and not gating.get("passed"):
            failing_svc, failing = _STORAGE_SERVICE, gating
        else:
            failing_svc, failing = next(
                ((svc, t) for svc, t in tests.items() if not t.get("passed")),
                ("", {}),
            )
        detail = failing.get("error") or "probe did not pass (no error detail)"
        result["error"] = f"check_api {failing_svc} probe failed: {detail}" if failing_svc else detail
        result["error_type"] = _bucket_token(detail)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
