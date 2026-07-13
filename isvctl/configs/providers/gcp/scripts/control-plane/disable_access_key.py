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

"""Deactivate a Cloud Storage HMAC key and confirm the state change by readback.

The AWS reference deactivates the created access key and emits its canonical
``Inactive`` status. Cloud Storage HMAC keys carry a ``state`` field whose
provider value is ``INACTIVE``; state changes can take up to ~3 minutes to
propagate. This step:

  * Loads the HMAC metadata by access id + project (ADC).
  * Sets ``state = INACTIVE`` and calls ``update()``.
  * Polls a metadata ``reload()`` until the provider state is observed as
    ``INACTIVE`` — the canonical ``Inactive`` value is emitted ONLY after that
    readback succeeds, never before.

NOTE: Left UNWIRED in the baseline GCP provider config; re-enable only after a
preflight proves full control over a disposable, run-owned HMAC key.

Usage:
    python3 disable_access_key.py --username sa@proj.iam.gserviceaccount.com \
        --access-key-id GOOG1E... --region us-central1

Output JSON:
{
    "success": true,
    "platform": "control_plane",
    "access_key_id": "GOOG1E...",
    "status": "Inactive"
}
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project
from common.errors import classify_gcp_error, handle_gcp_errors, retry_idempotent
from google.cloud import storage

# Readback poll for the INACTIVE state to propagate (Cloud Storage documents up
# to ~3 min). Bound by a MONOTONIC 180s deadline with a final readback at or
# after it: a fixed 12x15s attempt loop makes its last observation near 165s and
# can falsely report a still-propagating key as not deactivated.
_INACTIVE_DEADLINE_SECONDS = 180
_READBACK_INTERVAL = 15  # seconds between readbacks


@handle_gcp_errors
def main() -> int:
    """Deactivate the HMAC key and emit its canonical status after readback."""
    parser = argparse.ArgumentParser(description="Deactivate a Cloud Storage HMAC key")
    parser.add_argument("--username", default="", help="Service-account email owning the HMAC key")
    parser.add_argument("--access-key-id", required=True, help="Cloud Storage HMAC access id")
    parser.add_argument("--region", default="", help="Accepted for contract parity; no routing effect")
    parser.add_argument("--project", default="", help="GCP project id (falls back to ADC)")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "control_plane",
        "access_key_id": args.access_key_id,
        "status": "",
    }

    project = resolve_project(args.project or None)
    client = storage.Client(project=project)

    meta = retry_idempotent(
        client.get_hmac_key_metadata,
        access_id=args.access_key_id,
        project_id=project,
        op_desc="get_hmac_key_metadata",
    )

    # Persist the deactivation, then confirm it by readback before normalizing.
    # meta.update() sends only {"state": ...} with no etag, so the SDK's default
    # retry (DEFAULT_RETRY_IF_ETAG_IN_JSON) never engages and a single 429/5xx
    # would abort deactivation. Re-persisting the same INACTIVE state is
    # idempotent, so wrap the call in the bounded transient-retry envelope.
    meta.state = meta.INACTIVE_STATE
    retry_idempotent(meta.update, op_desc="hmac update (deactivate)")

    deadline = time.monotonic() + _INACTIVE_DEADLINE_SECONDS
    observed = meta.state
    while True:
        retry_idempotent(meta.reload, op_desc="hmac reload")
        observed = meta.state
        if observed == meta.INACTIVE_STATE:
            break
        # Keep polling until a readback lands at or after the monotonic
        # propagation deadline; only then is a non-INACTIVE state trustworthy.
        if time.monotonic() >= deadline:
            break
        time.sleep(_READBACK_INTERVAL)

    if observed != meta.INACTIVE_STATE:
        result["error"] = classify_gcp_error(
            RuntimeError(f"HMAC key {args.access_key_id} did not reach INACTIVE (observed {observed})")
        )[1]
        print(json.dumps(result, indent=2))
        return 1

    # Normalize the provider INACTIVE state to the contract value only now.
    result["status"] = "Inactive"
    result["success"] = True
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
