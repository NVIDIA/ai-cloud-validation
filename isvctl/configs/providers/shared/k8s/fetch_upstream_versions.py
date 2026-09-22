#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Report the upstream Kubernetes release cycles the K8S02 checks compare against.

K8S02 is a statement about a provider's offering relative to upstream: the three
most recent minors have to be offered, a new minor within 4-6 weeks of its
upstream release, and the control plane kept patched. None of that can be
settled from inside a cluster, and asking the provider for it would let it grade
its own homework, so the reference half is read from upstream here and the
bound validations compare the two.

The index is fetched by a step rather than by the validations because network
egress is a side effect, and the step/validation split keeps I/O out of the
assertions. A run that cannot reach the index fails this step - wire it
``continue_on_failure: true`` so the checks skip on the empty cycle list rather
than the phase stopping on an upstream outage.

``--source`` accepts a ``file://`` URL as well as an ``https://`` one, so an
air-gapped run can point at a mirrored copy of the same JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

# endoflife.date tracks Kubernetes from the upstream release data and answers
# every field the K8S02 checks need in one request: the minor, when it shipped,
# its newest patch, and when that patch shipped.
DEFAULT_SOURCE = "https://endoflife.date/api/kubernetes.json"
DEFAULT_TIMEOUT_SECONDS = 30


def fetch_cycles(source: str, timeout: int) -> list[dict[str, str]]:
    """Return the upstream release cycles from ``source``.

    Cycles that do not name a minor are dropped; the remaining fields are
    passed through as strings so a missing one reads as absent rather than
    ``None`` on the other side of the JSON contract. Ordering is left to the
    consumer, which cannot trust it from here and sorts for itself.
    """
    with urllib.request.urlopen(source, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if not isinstance(payload, list):
        raise ValueError(f"expected a JSON array of release cycles, got {type(payload).__name__}")

    return [cycle for cycle in (_cycle(entry) for entry in payload) if cycle]


def _cycle(entry: Any) -> dict[str, str] | None:
    """Return one release cycle in the contract's shape, or ``None`` if unusable."""
    if not isinstance(entry, dict):
        return None
    minor = str(entry.get("cycle") or "").strip()
    if not minor:
        return None
    cycle = {"minor": minor}
    for field, key in (
        ("released", "releaseDate"),
        ("latest_patch", "latest"),
        ("latest_patch_released", "latestReleaseDate"),
    ):
        value = str(entry.get(key) or "").strip()
        if value:
            cycle[field] = value
    return cycle


def main() -> int:
    """Fetch the upstream release index and print it as the step's JSON result."""
    parser = argparse.ArgumentParser(description="Report the upstream Kubernetes release cycles")
    parser.add_argument(
        "--source",
        default=DEFAULT_SOURCE,
        help=f"URL of the upstream release index; file:// is accepted for a mirror (default: {DEFAULT_SOURCE})",
    )
    args = parser.parse_args()

    result: dict[str, Any] = {"success": True, "platform": "kubernetes", "cycles_json": "[]"}
    try:
        cycles = fetch_cycles(args.source, DEFAULT_TIMEOUT_SECONDS)
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        result["success"] = False
        result["error"] = f"Could not read the upstream Kubernetes release index from {args.source}: {exc}"
    else:
        if cycles:
            result["cycles_json"] = json.dumps(cycles)
        else:
            result["success"] = False
            result["error"] = f"Upstream Kubernetes release index at {args.source} named no release cycles"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
