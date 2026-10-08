#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Prove a workload uses its ServiceAccount's IAM identity (K8S16-01) - my-isv template.

TODO: replace the stub below with a probe that runs inside a real pod:

1. Create a temporary platform IAM identity that only the pod's
   ServiceAccount can assume, granting access to one resource.
2. Launch a pod under that ServiceAccount without passing it any controller
   credentials, so it must use the platform-injected identity.
3. From the pod, report ``tests.identity`` (it assumed exactly that identity),
   ``tests.allowed_access`` (it reached the allowed resource), and
   ``tests.out_of_scope_denied`` (an authorization error, not a network error,
   for a resource outside its policy).
4. Remove every fixture; report anything left behind in ``cleanup_errors``.

Reference implementation: ../../../aws/scripts/eks/service_account_iam.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow importing provider-local helpers from scripts/common/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.stub import DEMO_MODE, emit_stub


def main() -> int:
    """Emit the ServiceAccount IAM template result (K8S16-01)."""
    return emit_stub(
        "service_account_iam",
        hint="in-pod ServiceAccount IAM probe",
        tests={probe: {"passed": DEMO_MODE} for probe in ("identity", "allowed_access", "out_of_scope_denied")},
    )


if __name__ == "__main__":
    sys.exit(main())
