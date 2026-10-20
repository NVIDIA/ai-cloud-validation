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

"""DPU host-management denial evidence template for SEC47-02.

Replace this template with probes for the operator's required_probes inventory.
Run each approved probe as the privileged tenant on the host, targeting the DPU
management interface. Record the actual execution context and effective UID.
Only classify an observed policy/access rejection as denied; missing tools,
unreachable devices, timeouts, and invalid command syntax are execution errors.
Do not infer denial from a security-mode query or a nonzero exit code alone.

Select probes and disposable targets with the operator: an unexpectedly allowed
management operation could alter device state. This template executes none.
"""

import json
import os


def main() -> int:
    """Emit demo evidence only when explicitly enabled; otherwise fail closed."""
    if os.environ.get("ISVCTL_DEMO_MODE") != "1":
        print(
            json.dumps(
                {
                    "success": False,
                    "error": "Not implemented - collect privileged host DPU management denial probes for your platform",
                    "probes": {},
                }
            )
        )
        return 1

    # TODO: Replace these demo records with observed results, including failures.
    probes = {
        f"demo-host/demo-dpu/{path}": {
            "executed": True,
            "execution_context": "host",
            "effective_uid": 0,
            "outcome": "denied",
            "denial_evidence": "DEMO ONLY: DPU policy rejected the host management operation",
        }
        for path in ("device_configuration", "firmware_update")
    }
    print(json.dumps({"success": True, "platform": "security", "test_name": "dpu_host_management", "probes": probes}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
