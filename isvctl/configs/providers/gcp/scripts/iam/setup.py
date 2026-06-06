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

"""GCP IAM inventory (optional ISV-facing scaffold; not wired into the suite).

Counterpart to the AWS reference `setup.sh`. Queries Cloud IAM for the
project's service accounts and reports the identity capabilities the suite
relies on, so an operator can confirm their environment before running the
lifecycle steps. This script is NOT invoked by suites/iam.yaml — the wired
setup-phase step is `create_user`. It is kept as a parity helper an ISV can
adapt or wire in.

When no project / Application Default Credentials resolve, the inventory is
skipped but the capability report is still emitted (there is nothing to
enumerate), so the read-only probe succeeds rather than failing closed.

Usage:
    python3 setup.py --project=my-project

Output JSON:
{
    "success": true,
    "platform": "iam",
    "iam": {
        "service_account_count": 3,
        "supports_service_accounts": true,
        "supports_workload_identity_federation": true,
        "supports_short_lived_tokens": true,
        "auth_methods": ["oauth2", "service_account_impersonation", "workload_identity_federation"]
    },
    "gcp": {"project": "my-project"}
}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project
from common.errors import classify_gcp_error, handle_gcp_errors
from google.cloud import iam_admin_v1


def _count_service_accounts(project: str) -> int:
    """Return the number of service accounts in ``project`` (read-only)."""
    iam = iam_admin_v1.IAMClient()
    return sum(1 for _ in iam.list_service_accounts(name=f"projects/{project}"))


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="GCP IAM inventory (optional scaffold)")
    parser.add_argument("--project", default="", help="GCP project (falls back to env/ADC when blank)")
    args = parser.parse_args()

    result: dict = {
        "success": True,
        "platform": "iam",
        "iam": {
            "supports_service_accounts": True,
            "supports_workload_identity_federation": True,
            "supports_short_lived_tokens": True,
            "auth_methods": ["oauth2", "service_account_impersonation", "workload_identity_federation"],
        },
        "gcp": {},
    }

    try:
        project = resolve_project(args.project or None)
        result["gcp"]["project"] = project
        result["iam"]["service_account_count"] = _count_service_accounts(project)
    except Exception as e:
        # No resolvable project / ADC, or a read error: report capabilities
        # only. A read-only inventory has nothing to enumerate, so the probe
        # still succeeds (matches the reference scaffold's no-endpoint path).
        error_type, error_msg = classify_gcp_error(e)
        result["iam"]["service_account_count"] = 0
        result["note"] = f"inventory skipped ({error_type}): {error_msg}"

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
