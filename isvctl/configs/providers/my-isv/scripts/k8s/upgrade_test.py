#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""K8S08/09/10 provider adapter template; never upgrade a cluster in demo mode.

TODO: implement the selected operation using your provider's upgrade API and
independent application/PDB observers. See docs/references/kubernetes-upgrades.md for the contract,
experiment, timeout and cleanup requirements. Demo records are illustrative only.
"""

import argparse
import json
import os
from pathlib import Path


def main() -> int:
    """Emit explicit not-implemented failure unless the caller selects demo mode."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", required=True, choices=("managed_upgrade_policy", "control_plane_upgrade", "node_pool_upgrade")
    )
    args = parser.parse_args()
    result = {"success": False, "platform": "kubernetes", "test_name": args.check}
    if os.environ.get("ISVCTL_DEMO_MODE") == "1":
        examples = Path(__file__).with_name("upgrade-example.json")
        result = json.loads(examples.read_text())[args.check]
    else:
        result["error"] = "Not implemented - replace with your provider's upgrade operation and independent observers"
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
