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

"""Enable VPC Flow Logs for the observability validation (setup phase).

Translates the AWS oracle's ``setup_vpc_flow_logs`` (create a standalone Flow Log
resource + CloudWatch group + IAM role) onto Compute Engine. Documented
divergences:

  * There is NO standalone flow-log resource on Compute Engine — VPC Flow Logs
    are configured on ``Subnetwork.log_config`` and written to the project Cloud
    Logging log ``compute.googleapis.com/vpc_flows``. No flow_log_id / log-group /
    IAM-role is created, so none is emitted.
  * Run-ownership is PER SUBNETWORK. This step patches logging on ONLY the exact
    subnetworks this run created — the forwarded ``--created-subnets`` allowlist —
    never every subnetwork bound to the network. A run-created subnet is patched
    even under an adopted parent network; a subnet this run did not create is
    never mutated.
  * Sampling: ``flow_sampling=1.0`` + ``INCLUDE_ALL_METADATA`` + no export filter,
    covering inbound and outbound sampled flows. "ALL" here means every GCP
    flow-log record remaining after uncontrollable primary sampling is retained
    without secondary sampling or filtering — NOT packet-complete capture and NOT
    a native GCP traffic-type field.

Two DISTINCT lists are emitted so teardown never disables logging this run did
not enable:

    patched_flow_log_subnets  the EXACT subnets this run actively patched (subset
                              of the created-subnets allowlist) — the ONLY list
                              forwarded to teardown_flow_logs.
    flow_log_subnets          the OBSERVED read-back: every subnetwork bound to
                              the network whose live log_config is enabled — a
                              diagnostic superset that can include operator-owned
                              or other-run subnets already logging on an adopted
                              VPC. NEVER forwarded to teardown.

``flow_logs_created`` is true only when this run patches a run-owned subnetwork
(when create_network already left logging enabled, this step is an idempotent
read-back and it stays false).

Emits:
    {
        "success":                  bool,   # all target subnets read back enabled
        "platform":                 "observability",
        "network_id":               str,
        "patched_flow_log_subnets": [str, ...],
        "flow_log_subnets":         [str, ...],
        "flow_logs_created":        bool,
        ...
    }

AWS reference implementation:
    ../../aws/scripts/observability/setup_vpc_flow_logs.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project, short_name
from common.errors import classify_gcp_error, handle_gcp_errors
from common.network import (
    FLOW_LOG_AGGREGATION_INTERVAL,
    FLOW_LOG_FLOW_SAMPLING,
    FLOW_LOG_METADATA,
    get_subnetwork,
    list_subnetworks_for_network,
    patch_subnetwork_flow_logs,
)

_FALSY_SENTINELS = {"", "none", "null", "false"}


def _split_ids(raw: str | None) -> list[str]:
    """Split a comma-separated id arg, dropping falsy sentinels."""
    return [t.strip() for t in (raw or "").split(",") if t.strip() and t.strip().lower() not in _FALSY_SENTINELS]


def _is_fully_enabled(log_config: Any) -> bool:
    """Return True iff log_config reads back EXACTLY as the requested ALL config.

    The requested configuration is enable=true, flow_sampling==1.0 (exact — 1.0
    round-trips a proto FLOAT exactly), an empty export filter, metadata==
    INCLUDE_ALL_METADATA, and aggregation_interval==INTERVAL_5_SEC. Accepting a
    weaker effective configuration (lower sampling, excluded metadata, a coarser
    interval, or an export filter) would let setup pass while the live subnet
    retains materially less than requested, so every dimension is asserted.
    """
    if not getattr(log_config, "enable", False):
        return False
    if float(getattr(log_config, "flow_sampling", 0.0) or 0.0) != FLOW_LOG_FLOW_SAMPLING:
        return False
    if getattr(log_config, "filter_expr", "") or "":
        return False
    if (getattr(log_config, "metadata", "") or "") != FLOW_LOG_METADATA:
        return False
    return (getattr(log_config, "aggregation_interval", "") or "") == FLOW_LOG_AGGREGATION_INTERVAL


@handle_gcp_errors
def main() -> int:
    """Patch VPC Flow Logs onto the run-created subnetworks and emit JSON."""
    parser = argparse.ArgumentParser(description="Enable GCP VPC Flow Logs on the run-created subnetworks")
    parser.add_argument("--region", required=True, help="GCP region containing the target subnetworks")
    parser.add_argument("--network-id", required=True, help="Compute Engine network name the subnets bind to")
    parser.add_argument(
        "--network-created",
        default="false",
        help="Informational only — per-subnet patching is authorized by --created-subnets, not parent ownership",
    )
    parser.add_argument(
        "--created-subnets",
        default="none",
        help="Comma-separated run-created subnetwork names (the exact patch allowlist)",
    )
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    args = parser.parse_args()

    project = resolve_project(args.project)
    network_id = args.network_id
    allowlist = _split_ids(args.created_subnets)

    result: dict[str, Any] = {
        "success": False,
        "platform": "observability",
        "test_name": "enable_vpc_flow_logs",
        "network_id": network_id,
        "patched_flow_log_subnets": [],
        "flow_log_subnets": [],
        "flow_logs_created": False,
        "region": args.region,
    }

    try:
        # The COMPLETE target-network readback set: every subnetwork bound to the
        # target network in this region. Enumerate it up front so confirmation can
        # span the full set (not just the mutation allowlist) and a VERIFIED RESUME
        # — where create_network adopted an already-configured subnet and forwarded
        # an EMPTY created-subnets allowlist — is confirmed by live state instead of
        # being rejected before read-back. MUTATION stays restricted to the
        # allowlist below; an adopted subnet outside it is never patched.
        if not list_subnetworks_for_network(project, args.region, network_id):
            raise RuntimeError(
                f"target network {network_id!r} has no subnetworks in {args.region}; "
                "cannot confirm VPC Flow Logs on an empty target network"
            )

        patched: list[str] = []
        # 1. Patch ONLY the run-created subnets (the allowlist). Verify each is
        # bound to the target network by exact tail match before mutating it, and
        # skip a subnet that is already fully enabled (idempotent read-back — it
        # is not added to patched_flow_log_subnets and does not set flow_logs_created).
        # An empty allowlist patches nothing: a verified resume then relies wholly
        # on the confirmation read-back below, and a disabled adopted subnet is
        # never mutated (it is not on the allowlist).
        # Ownership is recorded via on_accepted — after the synchronous patch is
        # ACCEPTED but before the op-wait — and stamped DIRECTLY into result there,
        # so a wait-side failure still forwards the accepted-but-unconfirmed subnet
        # to teardown_flow_logs (the config change was already submitted); recording
        # it only after the wait would leak a flow-log mutation this run made but
        # does not track.
        for subnet_name in allowlist:
            live = get_subnetwork(project, args.region, subnet_name)
            if short_name(live.network) != network_id:
                raise RuntimeError(
                    f"subnet {subnet_name!r} is bound to {short_name(live.network)!r}, not target network "
                    f"{network_id!r}; refusing to patch a subnet outside the target network"
                )
            if _is_fully_enabled(live.log_config):
                print(f"  {subnet_name}: flow logs already fully enabled; idempotent read-back", file=sys.stderr)
                continue
            print(f"  patching flow logs on run-created subnet {subnet_name}", file=sys.stderr)

            def _mark_patched(name: str = subnet_name) -> None:
                if name not in patched:
                    patched.append(name)
                # Stamp the accepted mutation into result IMMEDIATELY (inside
                # on_accepted, after synchronous acceptance but before the op-wait
                # can raise) so teardown_flow_logs receives the accepted-but-
                # unconfirmed subnet even when the wait fails. Stamping only after
                # the loop would leave result at [] / false on a wait-side failure
                # and leak a flow-log mutation this run made but does not track.
                result["patched_flow_log_subnets"] = patched
                result["flow_logs_created"] = bool(patched)

            patch_subnetwork_flow_logs(project, args.region, subnet_name, enable=True, on_accepted=_mark_patched)

        # Ensure the ownership stamps are set even when nothing was patched (empty
        # allowlist, or every allowlisted subnet was already idempotently enabled).
        result["patched_flow_log_subnets"] = patched
        result["flow_logs_created"] = bool(patched)

        # 2. Read-back confirmation over the COMPLETE target-network set (a fresh
        # LIST after patching), not just the mutation allowlist: success is the AND
        # over every subnetwork bound to the target network. This confirms a
        # verified resume where an adopted subnet is already fully enabled (an empty
        # allowlist still succeeds), and fails loudly when any target subnet —
        # including a disabled adopted one this run must not mutate — is not fully
        # configured. Trust observable state, not the patch ack. The same read-back
        # yields the OBSERVED diagnostic list (every enabled subnet bound to the
        # network — a superset that can include operator-owned / other-run subnets
        # on an adopted VPC) which is NEVER forwarded to teardown (forwarding it
        # would disable logging this run did not enable).
        target_subnets = list_subnetworks_for_network(project, args.region, network_id)
        confirmed = bool(target_subnets)
        observed: list[str] = []
        for sub in target_subnets:
            sub_name = short_name(sub.name)
            if getattr(sub.log_config, "enable", False):
                observed.append(sub_name)
            if not _is_fully_enabled(sub.log_config):
                confirmed = False
                result.setdefault("unconfirmed_subnets", []).append(sub_name)
        result["success"] = confirmed
        if not target_subnets:
            result["error"] = "no target-network subnetworks were returned for flow-log read-back"
        elif not confirmed:
            result["error"] = (
                "one or more target-network subnetworks did not read back with full flow-log configuration"
            )
        result["flow_log_subnets"] = observed

        if result["success"]:
            print(
                f"Enabled VPC Flow Logs: patched {len(patched)} run-owned subnet(s); "
                f"{len(observed)} enabled subnet(s) observed on {network_id}",
                file=sys.stderr,
            )
    except Exception as e:
        error_type, error_msg = classify_gcp_error(e)
        result.setdefault("error_type", error_type)
        result["error"] = error_msg
        result["success"] = False

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
