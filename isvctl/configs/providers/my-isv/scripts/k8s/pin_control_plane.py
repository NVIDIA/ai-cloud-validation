#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Pin the Kubernetes control plane to an instance count (K8S27-01) - my-isv template.

TODO: replace the stub below with a call to your control-plane sizing API.

The step has to do two things: request the pin, and report the size the cluster
actually ended up running. Wait for the resize to settle before reporting -
``instance_count`` is what the provider delivered, not what it accepted, and
K8sControlPlaneSizePinnedCheck compares the two.

Providers whose managed offering exposes no control-plane sizing knob should
leave this step out of their config rather than stub it; the check then skips
as ``step_not_configured`` instead of reporting a pin nobody can make.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow importing provider-local helpers from scripts/common/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.stub import emit_stub


def main() -> int:
    """Emit the control-plane pin template result (K8S27-01)."""
    parser = argparse.ArgumentParser(description="Pin the control plane to an instance count (template)")
    parser.add_argument(
        "--instance-count",
        type=int,
        default=3,
        help="Control-plane instance count to pin the cluster to",
    )
    args = parser.parse_args()

    if args.instance_count < 1:
        parser.error("--instance-count must be at least 1")

    return emit_stub(
        "pin_control_plane",
        hint="control-plane sizing API",
        requested_instance_count=args.instance_count,
        instance_count=args.instance_count,
    )


if __name__ == "__main__":
    sys.exit(main())
