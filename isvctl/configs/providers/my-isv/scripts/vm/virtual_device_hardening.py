#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

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
