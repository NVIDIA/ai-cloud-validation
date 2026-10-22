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

"""SEC30-03 allow-rule approval evidence - TEMPLATE.

TODO: Read an effective rule from the deployed policy and fetch its independent
approval record from your change-management system. Never infer approval from
an audit event or copy the live scope into a manufactured approval. Normalize
both scopes without dropping traffic constraints. See the AWS network guide's
allow-rule approval section for the provider-neutral contract.

Demo records below are synthetic and available only with ISVCTL_DEMO_MODE=1.
"""

import json
import os
import sys
from copy import deepcopy
from typing import Any


def main() -> int:
    """Emit a synthetic example in demo mode; fail until the provider is implemented."""
    result: dict[str, Any] = {"success": False, "platform": "network", "test_name": "allow_rule_approval"}
    if os.environ.get("ISVCTL_DEMO_MODE") == "1":
        rule = {
            "rule_id": "demo-rule",
            "policy_id": "demo-policy",
            "owner": "demo-network-team",
            "effective": True,
            "action": "allow",
            "scope": {
                "direction": "ingress",
                "source": "cidr:192.0.2.0/24",
                "destination": "demo-policy",
                "protocol": "tcp",
                "port_range": [443, 443],
            },
        }
        result.update(
            success=True,
            observed_at="2026-01-02T00:00:00Z",
            effective_rule=rule,
            approval={
                "record_id": "demo-approval",
                "source": "demo-only",
                "approver": "demo-reviewer",
                "status": "approved",
                "approved_at": "2026-01-01T00:00:00Z",
                **{key: deepcopy(rule[key]) for key in ("rule_id", "policy_id", "owner", "scope")},
            },
        )
    else:
        result["error"] = "Not implemented - collect a deployed rule and a trusted independent approval record"
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
