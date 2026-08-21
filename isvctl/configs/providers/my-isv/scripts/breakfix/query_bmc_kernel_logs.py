#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Query BMC kernel logs (BFX03-03) - my-isv template."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow importing provider-local helpers from scripts/common/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.stub import emit_stub


def main() -> int:
    """Emit the BMC kernel-log query template result (BFX03-03)."""
    parser = argparse.ArgumentParser(description="Query BMC kernel logs (template)")
    parser.add_argument("--region", default="", help="Cloud region")
    _ = parser.parse_args()

    return emit_stub(
        "query_bmc_kernel_logs",
        hint="BMC kernel log query",
        hosts=[
            {
                "host_id": "demo-host-001",
                "kernel_log_available": True,
                "log_source": "/redfish/v1/Managers/BMC/LogServices/Journal/Entries",
                "message_count": 1,
            }
        ],
    )


if __name__ == "__main__":
    sys.exit(main())
