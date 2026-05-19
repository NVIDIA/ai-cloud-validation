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

"""CRUD an OS install configuration (e.g., iPXE config, Carbide profile).

Provider-agnostic template - replace the TODO section with your platform's
install configuration management API calls.

This script is SELF-CONTAINED: it creates a config, reads it back, updates
it, and deletes it, reporting pass/fail for each operation.

Required JSON output:
{
    "success":     bool    - true if all CRUD operations passed,
    "platform":    str     - "image_registry",
    "config_id":   str     - identifier of the created config,
    "config_name": str     - human-readable config name,
    "operations": {
        "create": {"passed": bool},
        "read":   {"passed": bool},
        "update": {"passed": bool},
        "delete": {"passed": bool}
    },
    "error":       str     - (optional) error message, present when success is false
}

Usage:
    python crud_install_config.py --region <region>

AWS reference implementation:
    ../aws/image-registry/upload_image.py (similar pattern)
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """CRUD OS install configuration and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="CRUD OS install configuration")
    parser.add_argument("--region", required=True, help="Cloud region / availability zone")
    _args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "image_registry",
        "config_id": "",
        "config_name": "",
        "operations": {
            "create": {"passed": False},
            "read": {"passed": False},
            "update": {"passed": False},
            "delete": {"passed": False},
        },
    }

    # ╔══════════════════════════════════════════════════════════════════╗
    # ║  TODO: Replace this block with your platform's implementation    ║
    # ║                                                                  ║
    # ║  1. CREATE an OS install configuration                           ║
    # ║     config = create_install_config(                              ║
    # ║         name="isvtest-config", region=args.region,               ║
    # ║         boot_image="ubuntu-24.04", boot_method="ipxe",           ║
    # ║     )                                                            ║
    # ║     result["config_id"] = config.id                              ║
    # ║     result["config_name"] = config.name                          ║
    # ║     result["operations"]["create"]["passed"] = True              ║
    # ║                                                                  ║
    # ║  2. READ the config back                                         ║
    # ║     fetched = get_install_config(config.id)                      ║
    # ║     assert fetched.name == config.name                           ║
    # ║     result["operations"]["read"]["passed"] = True                ║
    # ║                                                                  ║
    # ║  3. UPDATE the config                                            ║
    # ║     update_install_config(config.id, description="updated")      ║
    # ║     result["operations"]["update"]["passed"] = True              ║
    # ║                                                                  ║
    # ║  4. DELETE the config                                            ║
    # ║     delete_install_config(config.id)                             ║
    # ║     result["operations"]["delete"]["passed"] = True              ║
    # ║                                                                  ║
    # ║  5. Set result["success"] = True if all operations passed        ║
    # ╚══════════════════════════════════════════════════════════════════╝

    if DEMO_MODE:
        result["config_id"] = "dummy-config-0001"
        result["config_name"] = "dummy-install-config"
        result["operations"] = {
            "create": {"passed": True},
            "read": {"passed": True},
            "update": {"passed": True},
            "delete": {"passed": True},
        }
        result["success"] = True
    else:
        result["error"] = "Not implemented - replace with your platform's install config CRUD logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
