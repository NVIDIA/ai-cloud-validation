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

"""Time security-policy propagation on Compute Engine (step ``sg_policy_propagation``).

Translates the AWS provider's ``sg_policy_propagation_test`` to Compute Engine.
Adds a probe firewall rule to the shared network and times how long until it is
observable, then removes it and times until it is gone, asserting both are
within ``--max-propagation-seconds`` (suite default 10, provider-neutral).

Documented divergences from the AWS provider:

  * Compute Engine has no security-group describe; the analog is
    ``FirewallsClient.get`` on the probe firewall. The firewall control plane is
    asynchronous — the insert Operation reaching DONE does not guarantee the
    rule is observable yet, so propagation is timed by polling ``get`` until the
    probe rule's EXACT expected shape — tcp/443, the probe source range, and the
    probe target tag — is observable (add) and until ``get`` returns NotFound
    (remove). Polling on the full shape (mirroring the AWS oracle's
    ``_permission_present``, which confirms protocol + port + expected CIDR)
    rather than on any non-empty ``allowed[]`` keeps a broader or different
    firewall from faking propagation success.
  * A GCE firewall cannot carry an empty ``allowed[]`` (HTTP 400), so "revoke"
    is modeled as ``FirewallsClient.delete`` (mirrors the sg_crud delete +
    NotFound pattern), not as an in-place rule emptying.
  * The probe rule allows tcp/443 (NOT an admin port) from a documentation test
    range and is tag-scoped to a probe tag, so it neither touches the SSH/RDP
    ingress guardrail (tcp/22 + tcp/3389) nor broadly applies to real VMs.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project, unique_suffix
from common.errors import delete_with_retry, handle_gcp_errors
from common.network import (
    build_firewall,
    delete_firewall,
    get_firewall,
    insert_firewall,
    make_allowed,
)
from google.api_core import exceptions as gax

TEST_NAME = "sg_policy_propagation"
TEST_NAMES = ("create_probe_rule", "rule_observed", "revoke_probe_rule", "removal_observed", "cleanup")

# Probe firewall shape. tcp/443 is not an admin port (the SSH/RDP ingress
# guardrail covers tcp/22 + tcp/3389 only); the source is RFC 5737 TEST-NET-1, a
# documentation range that cannot route to real hosts, and the rule is tag-scoped
# to a probe tag so it never broadly applies.
_PROBE_PORT = "443"
_PROBE_SOURCE = "192.0.2.0/24"
_PROBE_TAG = "isv-prop-probe"
# Poll generously past the threshold so a genuinely slow propagation is RECORDED
# (and then fails the timing gate below) rather than masked by a tight poll
# deadline that would fail rule_observed for the wrong reason. The recorded
# add/remove seconds are gated against --max-propagation-seconds, so a
# late-but-eventual transition still sets the subtest passed=false and overall
# success=false. Fits the step's 180s budget.
_POLL_TIMEOUT_S = 60.0
_POLL_INTERVAL_S = 0.5


def _probe_rule_observable(fw: Any) -> bool:
    """True iff ``fw`` matches the EXACT probe shape: tcp/443, source range, target tag.

    Mirrors the AWS oracle's ``_permission_present`` (protocol + port + expected
    CIDR) rather than treating any non-empty ``allowed[]`` as propagated.
    Compute Engine additionally tag-scopes the rule, so the probe tag is
    verified too. Source ranges and target tags are checked for set equality
    against the exact values the probe was created with, so a broader or
    different firewall cannot fake propagation success.
    """
    has_tcp_port = any(
        entry.I_p_protocol.lower() == "tcp" and _PROBE_PORT in list(entry.ports or ()) for entry in (fw.allowed or ())
    )
    return has_tcp_port and set(fw.source_ranges or ()) == {_PROBE_SOURCE} and set(fw.target_tags or ()) == {_PROBE_TAG}


def _poll_until_visible(project: str, name: str, timeout: float) -> float:
    """Poll ``get`` until the probe rule's EXACT expected shape is visible; return wall-clock seconds."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            fw = get_firewall(project, name)
            if _probe_rule_observable(fw):
                return time.monotonic() - start
        except gax.NotFound:
            pass
        time.sleep(_POLL_INTERVAL_S)
    raise RuntimeError(f"probe firewall {name!r} not observable with expected shape via get() within {timeout}s")


def _poll_until_gone(project: str, name: str, timeout: float) -> float:
    """Poll ``get`` until the probe rule returns NotFound; return wall-clock seconds."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            get_firewall(project, name)
        except gax.NotFound:
            return time.monotonic() - start
        time.sleep(_POLL_INTERVAL_S)
    raise RuntimeError(f"probe firewall {name!r} still observable via get() {timeout}s after delete")


def _threshold_subtest(observed: float, threshold: float, what: str) -> dict[str, Any]:
    """Pass only if ``observed`` propagation time is within the suite threshold.

    The diagnostic poll (``_POLL_TIMEOUT_S``) runs well past ``threshold`` so a
    slow-but-eventual transition is recorded rather than masked, but exceeding
    ``threshold`` fails the subtest (and hence overall ``success``). Mirrors the
    AWS oracle, which fails ``rule_observed`` / ``removal_observed`` when the
    transition is not observable within ``max_propagation_seconds``.
    """
    if observed <= threshold:
        return {"passed": True}
    return {
        "passed": False,
        "error": f"{what} took {observed:.2f}s, over the {threshold:.2f}s propagation threshold",
    }


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Time security-policy propagation (GCP)")
    parser.add_argument("--region", required=True, help="GCP region (informational; firewalls are global)")
    parser.add_argument("--vpc-id", required=True, help="Shared network short name to bind the probe rule to")
    parser.add_argument(
        "--max-propagation-seconds",
        type=float,
        default=10.0,
        help="Provider-neutral propagation threshold (suite-supplied); add and remove must each be within it",
    )
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    args = parser.parse_args()

    project = resolve_project(args.project)
    fw_name = unique_suffix("isv-prop-fw")

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": TEST_NAME,
        "tests": {name: {"passed": False} for name in TEST_NAMES},
        "target_rule_id": None,
        "add_observed_seconds": None,
        "remove_observed_seconds": None,
        "max_propagation_seconds": args.max_propagation_seconds,
    }

    fw_created = False
    fw_deleted = False

    try:
        # create_probe_rule — insert a probe firewall on the shared network.
        # insert_firewall waits for the global op to reach DONE. Stamp the
        # tracker BEFORE the wait so a partial create still reaches cleanup.
        probe = build_firewall(
            fw_name,
            args.vpc_id,
            project,
            direction="INGRESS",
            allowed=[make_allowed("tcp", [_PROBE_PORT])],
            source_ranges=[_PROBE_SOURCE],
            target_tags=[_PROBE_TAG],
        )
        fw_created = True
        insert_firewall(project, probe)
        result["target_rule_id"] = fw_name
        result["tests"]["create_probe_rule"] = {"passed": True}

        # rule_observed — time from insert-op-DONE to first visible via get().
        # Gate the recorded time on the suite threshold (not the diagnostic poll
        # window): a slow-but-eventual add fails the subtest + overall success.
        add_seconds = _poll_until_visible(project, fw_name, _POLL_TIMEOUT_S)
        result["add_observed_seconds"] = round(add_seconds, 3)
        result["tests"]["rule_observed"] = _threshold_subtest(
            add_seconds, args.max_propagation_seconds, "probe rule add"
        )

        # revoke_probe_rule — model "revoke" as delete (empty allowed[] is 400).
        # delete_firewall waits for the delete op to reach DONE, but op-DONE
        # does NOT prove get() already returns NotFound (same async lag as
        # insert), so the firewall is NOT yet confirmed gone here. Do NOT stamp
        # fw_deleted now: only the _poll_until_gone NotFound below proves
        # removal, and stamping early would let a slow/partial delete skip the
        # finally re-delete and still report cleanup passed.
        delete_firewall(project, fw_name)
        result["tests"]["revoke_probe_rule"] = {"passed": True}

        # removal_observed — time from delete to get() returning NotFound.
        # Gate the recorded time on the suite threshold, same as rule_observed.
        remove_seconds = _poll_until_gone(project, fw_name, _POLL_TIMEOUT_S)
        # Removal is now confirmed observable (get() returned NotFound), so mark
        # the firewall deleted and let finally skip the idempotent re-delete. If
        # _poll_until_gone raised, fw_deleted stays False and finally re-attempts
        # deletion via delete_with_retry — cleanup fails if it cannot complete.
        fw_deleted = True
        result["remove_observed_seconds"] = round(remove_seconds, 3)
        result["tests"]["removal_observed"] = _threshold_subtest(
            remove_seconds, args.max_propagation_seconds, "probe rule removal"
        )

    except Exception as e:
        result.setdefault("error", str(e))
        result["success"] = False
    finally:
        # cleanup — ensure the probe rule is gone on success AND failure.
        # NotFound inside delete_with_retry is idempotent success.
        cleanup_ok = True
        if fw_created and not fw_deleted:
            cleanup_ok = delete_with_retry(delete_firewall, project, fw_name, resource_desc=f"firewall {fw_name}")
        result["tests"]["cleanup"] = {"passed": cleanup_ok}
        if not cleanup_ok:
            result.setdefault("cleanup_errors", []).append(f"firewall {fw_name}")
        # Recompute overall success inside finally (oracle shape): every subtest
        # — including the timing gates and cleanup — must pass.
        result["success"] = all(t.get("passed", False) for t in result["tests"].values())

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
