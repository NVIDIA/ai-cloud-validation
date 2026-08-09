#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Emit the documented NICo gap payload for a break-fix requirement.

Several break-fix requirements have no NICo tenant REST surface to exercise:
the mutating BFX01 workflows run through Maestro/repair fixtures, and the
BFX02-02/BFX04-01/BFX05/BFX06 signals are not exposed at all. Each of
those steps emits a structured skip naming the gap rather than a hard failure,
so the suite reports "not available on this platform" instead of "broken".

One script serves all of them: the per-requirement reason and the zeroed
contract fields the validation reads live in ``GAPS`` below, selected by
``--gap``. Add a row here when a new gap is documented; replace the row with a
real query script when NICo ships the API.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from breakfix._common import emit, skip_result

# gap id -> (skip reason, contract fields the bound validation still expects)
GAPS: dict[str, tuple[str, dict[str, Any]]] = {
    "BFX01-02": (
        "Return-node-for-maintenance is a mutating NICo repair workflow requiring lab fixtures (BFX01-02 gap)",
        {"operation": {"requested": False, "accepted": False}},
    ),
    "BFX01-03": (
        "Rack-level maintenance return API is not exposed on NICo tenant REST (BFX01-03 gap)",
        {"operation": {"requested": False, "accepted": False}},
    ),
    "BFX01-05": (
        "Host replacement workflow is mutating and requires dedicated NICo lab fixtures (BFX01-05 gap)",
        {"operation": {"requested": False, "node_removed_from_pool": False}},
    ),
    "BFX02-02": (
        "NICo has no retirement-notice query API (BFX02-02 gap)",
        {"notices_queryable": False, "notices": []},
    ),
    "BFX04-01": (
        "GPUd/Sentinel/Maestro node health agents are not observable via NICo REST (BFX04-01 gap)",
        {"agents_observable": False, "agents": []},
    ),
    "BFX05-01": (
        "Planned maintenance notification channel is not exposed via NICo REST (BFX05-01 gap)",
        {"notification_channel_observable": False, "notifications": []},
    ),
    "BFX06-01": (
        "Immediate failure notification channel is not exposed via NICo REST (BFX06-01 gap)",
        {"notification_channel_observable": False, "notifications": []},
    ),
}


def main() -> int:
    """Emit the skip payload for the requested gap as JSON."""
    parser = argparse.ArgumentParser(description="Emit a documented NICo break-fix gap payload")
    parser.add_argument("--org", required=True)
    parser.add_argument("--site-id", required=True)
    parser.add_argument("--api-base", required=True)
    parser.add_argument("--gap", required=True, choices=sorted(GAPS))
    args = parser.parse_args()

    reason, fields = GAPS[args.gap]
    result = skip_result(args.site_id, reason, gap=args.gap)
    result.update(fields)
    return emit(result)


if __name__ == "__main__":
    sys.exit(main())
