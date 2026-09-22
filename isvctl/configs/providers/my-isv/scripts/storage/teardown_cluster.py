#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Release the Kubernetes cluster that setup_cluster acquired."""

import argparse
import json
import os
import sys
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    """Emit the provider-neutral cluster teardown contract."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", default="", help="Kubeconfig emitted by setup_cluster")
    parser.add_argument("--skip-destroy", action="store_true", help="Keep the cluster for cheap reruns")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "kubernetes",
        "resources_deleted": [],
        "message": "",
    }

    if args.skip_destroy:
        result.update({"success": True, "skipped": True, "message": "Teardown skipped (--skip-destroy)"})
        print(json.dumps(result, indent=2))
        return 0

    # TODO: Release whatever setup_cluster acquired. This is deliberately the
    # mirror of that step: if setup_cluster created a cluster, destroy it here;
    # if it reused a long-lived one, a no-op success is the correct answer. The
    # step must still exist either way, so a standalone storage run never leaks
    # a cluster it provisioned.
    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "resources_deleted": ["demo-cluster"],
                "message": "Cluster released",
            }
        )
    else:
        result["error"] = "Not implemented - release the cluster setup_cluster acquired"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
