#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Small kubectl-compatible test double for Launch Kit prerequisite checks."""

from __future__ import annotations

import json
import os
import sys


def main(argv: list[str] | None = None) -> int:
    """Return Kubernetes version or Ready-node JSON for the requested command."""
    args = list(sys.argv[1:] if argv is None else argv)
    if os.environ.get("L8K_MOCK_KUBERNETES_FAIL") == "1":
        print("Unable to connect to the server: connection refused", file=sys.stderr)
        return 1
    expected_kubeconfig = os.environ.get("L8K_MOCK_EXPECT_KUBECONFIG")
    if expected_kubeconfig and os.environ.get("KUBECONFIG") != expected_kubeconfig:
        print(
            f"expected KUBECONFIG={expected_kubeconfig!r}, got {os.environ.get('KUBECONFIG')!r}",
            file=sys.stderr,
        )
        return 1
    if "version" in args:
        print(
            json.dumps(
                {
                    "clientVersion": {"gitVersion": "v1.34.1"},
                    "serverVersion": {"gitVersion": "v1.34.1"},
                }
            )
        )
        return 0
    if "get" in args and "nodes" in args:
        print(
            json.dumps(
                {
                    "apiVersion": "v1",
                    "items": [
                        {
                            "metadata": {"name": "worker-a"},
                            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                        },
                        {
                            "metadata": {"name": "worker-b"},
                            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                        },
                    ],
                }
            )
        )
        return 0
    print(f"mock kubectl does not support: {' '.join(args)}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
