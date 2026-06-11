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

"""Virtual device hardening validation - TEMPLATE.

This script must prove that USB redirection, clipboard sharing, and
unnecessary virtual devices are disabled or absent for the target VM.

Required JSON output fields:
  {
    "success": true,
    "platform": "vm",
    "test_name": "virtual_device_hardening",
    "tests": {
      "usb_devices_disabled": {"passed": true},
      "clipboard_disabled": {"passed": true},
      "unnecessary_virtual_devices_absent": {"passed": true}
    }
  }

Usage:
    python virtual_device_hardening.py --instance-id <id> --region <region>

Reference implementation: ../../../aws/scripts/vm/virtual_device_hardening.py
"""

import argparse
import json
import os
import sys
from typing import Any

# ISVCTL_DEMO_MODE=1 enables demo-success output (used by `make demo-test`).
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def _demo_result() -> dict[str, Any]:
    """Return a passing demo payload for the virtual-device hardening contract."""
    return {
        "success": True,
        "platform": "vm",
        "test_name": "virtual_device_hardening",
        "tests": {
            "usb_devices_disabled": {
                "passed": True,
                "probes": ["demo_no_usb_redirection"],
            },
            "clipboard_disabled": {
                "passed": True,
                "probes": ["demo_no_shared_clipboard"],
            },
            "unnecessary_virtual_devices_absent": {
                "passed": True,
                "probes": ["demo_no_unnecessary_virtual_devices"],
            },
        },
    }


def _not_implemented_result() -> dict[str, Any]:
    """Return a failing payload until the ISV provider implementation is supplied."""
    return {
        "success": False,
        "platform": "vm",
        "test_name": "virtual_device_hardening",
        "tests": {
            "usb_devices_disabled": {"passed": False},
            "clipboard_disabled": {"passed": False},
            "unnecessary_virtual_devices_absent": {"passed": False},
        },
        "error": "Not implemented - replace with your platform's virtual device hardening validation",
    }


def main() -> int:
    """Validate virtual-device hardening and emit structured JSON result."""
    parser = argparse.ArgumentParser(description="Virtual device hardening validation (template)")
    parser.add_argument("--instance-id", required=True, help="Instance ID")
    parser.add_argument("--region", required=True, help="Cloud region")
    parser.add_argument("--public-ip", default="", help="Optional SSH host for guest probes")
    parser.add_argument("--key-file", default="", help="Optional SSH private key path for guest probes")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    parser.parse_args()

    # TODO: Replace this block with your platform's virtual-device hardening checks.
    #
    # Recommended evidence:
    #   1. Prove the VM service exposes no tenant USB attach/redirection surface.
    #   2. Prove the VM service exposes no shared clipboard channel.
    #   3. Probe the guest, when SSH is available, for USB controllers/devices,
    #      SPICE/QXL/VDI clipboard agents, and floppy/CD-ROM/audio/tablet devices.
    #
    # Populate all required `tests` entries with {"passed": true} only when
    # your provider evidence supports that claim.

    result = _demo_result() if DEMO_MODE else _not_implemented_result()
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
