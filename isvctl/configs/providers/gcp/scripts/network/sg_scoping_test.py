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

"""Test security-group / firewall rule scoping at workload, node, subnet, or service level.

Documented divergences from the AWS provider
(``providers/aws/scripts/network/sg_scoping_test.py``):

  * workload / node: AWS attaches SGs per-ENI. Compute Engine firewalls do
    NOT attach to a NIC — they scope by ``target_tags``. We create a
    tag-scoped INGRESS firewall and TWO VMs in the same network where only
    one carries the tag, then confirm via two INDEPENDENT ``instances.get``
    reads that the firewall's tag is present on the target VM and absent on
    the other. Workload and node collapse to the same tag/instance boundary.
  * subnet: AWS uses per-subnet NACLs. Compute Engine has no NACL and no
    per-subnet attachment. We approximate with a firewall whose
    ``source_ranges`` are constrained to subnet A's CIDR, then assert via
    ``firewalls.get`` that A's CIDR is present and B's CIDR is absent.
  * service: AWS scopes an SG to a VPC interface-endpoint's ENIs. Compute
    Engine has no VPC-endpoint analog; the honest equivalent is
    ``target_service_accounts`` (service-identity scoping — NOT
    ``network_tags``, which is a different firewall feature). GCE firewalls
    expose no "applied to instance X" readback field — applicability is
    implicit at packet time based on a VM's ``service_accounts`` matching the
    rule's ``target_service_accounts``. We therefore self-create a service
    account in-test, bind the operator principal to
    ``roles/iam.serviceAccountUser`` so VM-attach succeeds, wait for IAM
    propagation, then launch TWO VMs — (a) with the firewall's target SA
    attached, (b) with a SEPARATE, distinct non-target SA attached. The
    'other' VM uses a real distinct identity rather than
    ``service_accounts=[]`` because the proto-plus REST client serializes an
    empty list identically to an unset field (verified), so an empty list
    cannot reliably express a no-SA / distinct-SA state. The two scoping
    booleans derive from TWO INDEPENDENT VM observations, not from a single
    firewall-config readback.

Usage:
    python sg_scoping_test.py --region us-central1 --zone us-central1-a --scope workload
    python sg_scoping_test.py --region us-central1 --zone us-central1-a --scope node
    python sg_scoping_test.py --region us-central1 --zone us-central1-a --scope subnet
    python sg_scoping_test.py --region us-central1 --zone us-central1-a --scope service
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

import google.auth
import google.auth.credentials
import google.auth.transport.requests
from common.compute import (
    get_instance,
    narrow_region_to_zone,
    resolve_project,
    unique_suffix,
    zone_to_region,
)
from common.errors import classify_gcp_error, delete_with_retry, handle_gcp_errors
from common.network import (
    build_firewall,
    build_probe_instance,
    carve_subnet_cidrs,
    delete_firewall,
    delete_instance,
    delete_network,
    delete_subnetwork,
    get_firewall,
    insert_firewall,
    insert_instance,
    insert_network,
    insert_subnetwork,
    make_allowed,
)
from google.api_core import exceptions as gax
from google.cloud import iam_admin_v1
from google.iam.v1 import iam_policy_pb2, policy_pb2

# Aggregate the test carves its (per-scope) subnets from.
TEST_CIDR = "10.91.0.0/16"

# IAM propagation budget for the service scope: a freshly-created SA's
# actAs binding is not effective on instances.insert immediately. Retry
# the VM create against permission-denied / actAs-not-yet-effective.
IAM_PROPAGATION_ATTEMPTS = 12
IAM_PROPAGATION_DELAY = 15  # seconds -> 180s budget

# OAuth2 tokeninfo endpoint used to resolve the ADC principal email when
# GCP_TEST_SA_EMAIL is not supplied by the operator.
_TOKENINFO_URL = "https://www.googleapis.com/oauth2/v1/tokeninfo"


# --------------------------------------------------------------------- #
# Shared network scaffolding                                            #
# --------------------------------------------------------------------- #


# Non-admin TCP port used purely to give each scoping firewall a non-empty
# allowed[] (empty allowed[] is rejected with HTTP 400). These tests verify
# firewall SCOPING — by target_tags, target_service_accounts, or subnet CIDR
# in source_ranges — via instances.get / firewalls.get read-backs, NOT by
# making a connection (every probe VM is launched with external_ip=False).
# SSH/RDP are never exercised, so the rule deliberately avoids the admin ports
# tcp/22 and tcp/3389 (whose ingress is governed by NETWORK_FIREWALL_TRUST_IP);
# a non-admin port keeps the rule honest about what it does and what it does not.
_PROBE_PORT = "8080"


def _probe_allowed() -> list[Any]:
    """Return a one-entry non-admin tcp allow list (empty allowed[] is rejected with HTTP 400)."""
    return [make_allowed("tcp", [_PROBE_PORT])]


# --------------------------------------------------------------------- #
# workload / node scope (tag-scoped firewall + two VM observations)     #
# --------------------------------------------------------------------- #


def _drive_tag_scope(project: str, zone: str, scope: str) -> dict[str, Any]:
    """Verify a tag-scoped firewall applies only to the tagged VM (workload/node)."""
    region = zone_to_region(zone)
    apply_key = f"apply_{scope}_rule"
    allowed_key = "workload_allowed" if scope == "workload" else "target_node_allowed"
    blocked_key = "other_workload_blocked" if scope == "workload" else "other_node_blocked"
    tests: dict[str, Any] = {}

    network_name = unique_suffix(f"isv-sgscope-{scope}")
    subnet_name = unique_suffix(f"isv-sgscope-{scope}-subnet")
    firewall_name = unique_suffix(f"isv-sgscope-{scope}-fw")
    target_vm = unique_suffix(f"isv-sgscope-{scope}-target")
    other_vm = unique_suffix(f"isv-sgscope-{scope}-other")
    test_tag = unique_suffix(f"isv-sgscope-{scope}-tag")[:62]

    network_created = False
    created_instances: list[str] = []
    firewall_created = False
    subnet_created = False

    try:
        # Stamp each *_created tracker BEFORE its insert helper: insert_* runs
        # _wait_or_rollback, which on a failed op-wait + failed rollback raises
        # PartialCreateError with the resource possibly leaked. The finally
        # cleanup gates on the tracker, so it must be True before the call for a
        # partial create to still reach cleanup (delete on a never-created
        # resource is a harmless NotFound no-op). Mirrors create_vpc/byoip_test.
        network_created = True
        insert_network(project, network_name)

        subnet_cidr = carve_subnet_cidrs(TEST_CIDR, 1)[0]
        subnet_created = True
        insert_subnetwork(project, region, subnet_name, network_name, subnet_cidr)

        # Tag-scoped INGRESS firewall — applies only to instances carrying
        # test_tag (Compute Engine firewalls scope by target_tags, not NIC).
        fw = build_firewall(
            firewall_name,
            network_name,
            project,
            direction="INGRESS",
            allowed=_probe_allowed(),
            source_ranges=["0.0.0.0/0"],
            target_tags=[test_tag],
        )
        firewall_created = True
        insert_firewall(project, fw)
        tests["create_sg"] = {"passed": True}

        # Target VM carries the tag; other VM does not (no SA needed for
        # tag scoping — leave service_accounts unset).
        target_inst = build_probe_instance(
            project=project,
            zone=zone,
            name=target_vm,
            network_name=network_name,
            subnet_name=subnet_name,
            external_ip=False,
            network_tags=[test_tag],
        )
        created_instances.append(target_vm)
        insert_instance(project, zone, target_inst)

        other_inst = build_probe_instance(
            project=project,
            zone=zone,
            name=other_vm,
            network_name=network_name,
            subnet_name=subnet_name,
            external_ip=False,
            network_tags=None,
        )
        created_instances.append(other_vm)
        insert_instance(project, zone, other_inst)
        tests[apply_key] = {"passed": True}

        # TWO INDEPENDENT reads — one per VM. Derive each boolean from the
        # tag set actually present on that instance.
        target_tags = set(get_instance(project, zone, target_vm).tags.items or ())
        other_tags = set(get_instance(project, zone, other_vm).tags.items or ())

        tests[allowed_key] = {
            "passed": test_tag in target_tags,
            "message": f"firewall target tag {test_tag!r} present on target VM",
        }
        tests[blocked_key] = {
            "passed": test_tag not in other_tags,
            "message": f"firewall target tag {test_tag!r} absent on other VM (scoped correctly)",
        }
    finally:
        cleanup_errors = _cleanup(
            project,
            zone=zone,
            region=region,
            instances=created_instances,
            firewalls=[firewall_name] if firewall_created else [],
            subnets=[subnet_name] if subnet_created else [],
            networks=[network_name] if network_created else [],
        )
        tests["cleanup"] = _cleanup_result(cleanup_errors)

    return tests


# --------------------------------------------------------------------- #
# subnet scope (CIDR-constrained firewall stand-in)                     #
# --------------------------------------------------------------------- #


def _drive_subnet(project: str, zone: str) -> dict[str, Any]:
    """Verify a firewall's source_ranges are scoped to subnet A's CIDR, not B's."""
    region = zone_to_region(zone)
    tests: dict[str, Any] = {}

    network_name = unique_suffix("isv-sgscope-subnet")
    subnet_a = unique_suffix("isv-sgscope-subnet-a")
    subnet_b = unique_suffix("isv-sgscope-subnet-b")
    firewall_name = unique_suffix("isv-sgscope-subnet-fw")

    network_created = False
    created_subnets: list[str] = []
    firewall_created = False

    try:
        # Stamp/record each tracker BEFORE its insert helper: insert_* runs
        # _wait_or_rollback, which on a failed op-wait + failed rollback raises
        # PartialCreateError with the resource possibly leaked. Cleanup gates on
        # the tracker, so it must be set before the call for a partial create to
        # still reach cleanup (delete on a never-created resource is a harmless
        # NotFound no-op). Mirrors create_vpc/byoip_test.
        network_created = True
        insert_network(project, network_name)

        cidr_a, cidr_b = carve_subnet_cidrs(TEST_CIDR, 2)
        tests["create_sg"] = {"passed": True, "message": "CIDR-constrained firewall stand-in"}

        created_subnets.append(subnet_a)
        insert_subnetwork(project, region, subnet_a, network_name, cidr_a)
        created_subnets.append(subnet_b)
        insert_subnetwork(project, region, subnet_b, network_name, cidr_b)

        # Firewall scoped to subnet A's CIDR only — no NACL/per-subnet
        # attachment exists on Compute Engine, so source-range constraint
        # is the honest approximation of subnet-level scoping.
        fw = build_firewall(
            firewall_name,
            network_name,
            project,
            direction="INGRESS",
            allowed=_probe_allowed(),
            source_ranges=[cidr_a],
        )
        firewall_created = True
        insert_firewall(project, fw)
        tests["apply_subnet_rule"] = {"passed": True}

        # Read back the firewall and derive both booleans from its real
        # source_ranges.
        ranges = set(get_firewall(project, firewall_name).source_ranges or ())
        tests["subnet_allowed"] = {
            "passed": cidr_a in ranges,
            "message": "firewall sourceRanges contain subnet A CIDR",
        }
        tests["other_subnet_blocked"] = {
            "passed": cidr_b not in ranges,
            "message": "firewall sourceRanges do not contain subnet B CIDR",
        }
    finally:
        cleanup_errors = _cleanup(
            project,
            zone=zone,
            region=region,
            instances=[],
            firewalls=[firewall_name] if firewall_created else [],
            subnets=created_subnets,
            networks=[network_name] if network_created else [],
        )
        tests["cleanup"] = _cleanup_result(cleanup_errors)

    return tests


# --------------------------------------------------------------------- #
# service scope (targetServiceAccounts + self-created SA + two VMs)     #
# --------------------------------------------------------------------- #


def _resolve_principal_member() -> str:
    """Resolve the principal that must be granted ``serviceAccountUser`` on the new SA.

    Prefers the operator-pinned ``GCP_TEST_SA_EMAIL`` (a USER email — the
    principal that will act-as the created SA). Otherwise refresh ADC and
    read the OAuth2 tokeninfo endpoint for the active principal's email.
    Returns the IAM member string (``user:`` or ``serviceAccount:`` prefixed).
    """
    pinned = os.environ.get("GCP_TEST_SA_EMAIL", "").strip()
    if pinned:
        return pinned if ":" in pinned else f"user:{pinned}"

    raw_creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds = cast(google.auth.credentials.Credentials, raw_creds)
    auth_req = google.auth.transport.requests.Request()
    creds.refresh(auth_req)

    # Service-account ADC exposes the email directly; user ADC does not, so
    # fall back to the tokeninfo endpoint.
    sa_email = getattr(creds, "service_account_email", None)
    if sa_email and sa_email != "default":
        return f"serviceAccount:{sa_email}"

    resp = auth_req(url=f"{_TOKENINFO_URL}?access_token={creds.token}", method="GET")
    info = json.loads(resp.data.decode("utf-8") if isinstance(resp.data, bytes) else resp.data)
    email = info.get("email")
    if not email:
        raise RuntimeError(
            "could not resolve ADC principal email from tokeninfo; set GCP_TEST_SA_EMAIL to the operator principal"
        )
    prefix = "serviceAccount:" if email.endswith(".gserviceaccount.com") else "user:"
    return f"{prefix}{email}"


def _create_target_sa(project: str, account_id: str, *, display_name: str) -> str:
    """Create a test-owned service account and return its email."""
    iam = iam_admin_v1.IAMClient()
    sa = iam_admin_v1.ServiceAccount()
    sa.display_name = display_name
    iam.create_service_account(
        name=f"projects/{project}",
        account_id=account_id,
        service_account=sa,
    )
    return f"{account_id}@{project}.iam.gserviceaccount.com"


def _bind_service_account_user(sa_email: str, member: str) -> None:
    """Grant ``member`` roles/iam.serviceAccountUser on the new SA so VM-attach succeeds."""
    iam = iam_admin_v1.IAMClient()
    binding = policy_pb2.Binding(role="roles/iam.serviceAccountUser", members=[member])
    policy = policy_pb2.Policy(bindings=[binding])
    request = iam_policy_pb2.SetIamPolicyRequest(
        resource=f"projects/-/serviceAccounts/{sa_email}",
        policy=policy,
    )
    iam.set_iam_policy(request=request)


def _delete_target_sa(sa_email: str) -> bool:
    """Delete the test-owned SA with bounded retry; return True iff it is gone.

    NotFound / already-absent counts as success (the eventual-consistency
    window is absorbed by the retry). Returns False only when a documented
    transient IAM failure (rate-limit / 5xx / timeout) persists past the retry
    budget, so the caller can fold the genuine leak into the cleanup error
    list. Wraps ``common.errors.delete_with_retry`` — the canonical GCP cleanup
    envelope used for every other delete here.
    """
    iam = iam_admin_v1.IAMClient()
    return delete_with_retry(
        iam.delete_service_account,
        name=f"projects/-/serviceAccounts/{sa_email}",
        resource_desc=f"service account {sa_email}",
    )


def _insert_instance_with_iam_propagation(
    project: str,
    zone: str,
    instance: Any,
) -> None:
    """Insert an instance, retrying while a fresh actAs binding propagates.

    A just-created serviceAccountUser binding is not effective on
    instances.insert immediately; GCE returns permission-denied /
    actAs-not-yet-effective for up to ~3 minutes. Retry within the
    propagation budget; re-raise any non-permission error immediately.
    """
    last_err: Exception | None = None
    for attempt in range(1, IAM_PROPAGATION_ATTEMPTS + 1):
        try:
            insert_instance(project, zone, instance)
            return
        except gax.PermissionDenied as e:
            last_err = e
        except (gax.Forbidden, gax.BadRequest) as e:
            # actAs-not-yet-effective sometimes surfaces as 400/403 with an
            # "iam.serviceAccounts.actAs" message rather than PermissionDenied.
            if "actas" not in str(e).lower() and "serviceaccount" not in str(e).lower():
                raise
            last_err = e
        if attempt < IAM_PROPAGATION_ATTEMPTS:
            print(
                f"  IAM actAs not yet effective (attempt {attempt}/{IAM_PROPAGATION_ATTEMPTS}); "
                f"sleeping {IAM_PROPAGATION_DELAY}s",
                file=sys.stderr,
            )
            time.sleep(IAM_PROPAGATION_DELAY)
    raise RuntimeError(
        f"IAM actAs binding did not propagate within {IAM_PROPAGATION_ATTEMPTS * IAM_PROPAGATION_DELAY}s: {last_err}"
    ) from last_err


def _drive_service(project: str, zone: str) -> dict[str, Any]:
    """Verify a targetServiceAccounts firewall applies only to the VM with the matching SA."""
    region = zone_to_region(zone)
    tests: dict[str, Any] = {}

    network_name = unique_suffix("isv-sgscope-service")
    subnet_name = unique_suffix("isv-sgscope-service-subnet")
    firewall_name = unique_suffix("isv-sgscope-service-fw")
    allowed_vm = unique_suffix("isv-sgscope-service-allowed")
    other_vm = unique_suffix("isv-sgscope-service-other")
    # account_id: <=30 chars, lowercase alnum + hyphen, starts with a letter.
    # RUN_ID alone is shared across a run, so a same-run retry after a
    # delayed/failed SA delete would reuse the same account_id and hit
    # ALREADY_EXISTS. Fold a fresh per-invocation token in BEFORE the run
    # suffix so each invocation gets distinct IDs while staying within 30
    # chars ("isv-sgs-svc-" 12 + 4 token + "-" + 6 run = 23).
    invocation_disc = uuid.uuid4().hex[:4]
    target_account_id = unique_suffix(f"isv-sgs-svc-{invocation_disc}", length=6)[:30]
    other_account_id = unique_suffix(f"isv-sgs-oth-{invocation_disc}", length=6)[:30]

    # Stamp the SA trackers BEFORE any subsequent op (async-create discipline).
    sa_email: str | None = None
    other_sa_email: str | None = None
    network_created = False
    created_instances: list[str] = []
    firewall_created = False
    subnet_created = False

    try:
        # Stamp each *_created tracker BEFORE its insert helper: insert_* runs
        # _wait_or_rollback, which on a failed op-wait + failed rollback raises
        # PartialCreateError with the resource possibly leaked. The finally
        # cleanup gates on the tracker, so it must be True before the call for a
        # partial create to still reach cleanup (delete on a never-created
        # resource is a harmless NotFound no-op). Mirrors create_vpc/byoip_test.
        network_created = True
        insert_network(project, network_name)

        subnet_cidr = carve_subnet_cidrs(TEST_CIDR, 1)[0]
        subnet_created = True
        insert_subnetwork(project, region, subnet_name, network_name, subnet_cidr)

        # 1. Self-create TWO distinct test-owned SAs: the firewall's target
        # SA (attached to the 'allowed' VM) and a separate non-target SA
        # (attached to the 'other' VM). The 'other' VM must carry a real,
        # DISTINCT identity rather than service_accounts=[] — an empty list
        # is serialized identically to "unset" by the proto-plus REST client
        # (verified), so it cannot reliably express a no-SA / distinct-SA
        # state. A second created SA makes the negative observation rest on a
        # genuinely independent service identity. Stamp trackers first.
        sa_email = f"{target_account_id}@{project}.iam.gserviceaccount.com"
        other_sa_email = f"{other_account_id}@{project}.iam.gserviceaccount.com"
        _create_target_sa(project, target_account_id, display_name="ISV sg_service_scoping target SA")
        _create_target_sa(project, other_account_id, display_name="ISV sg_service_scoping non-target SA")

        # 2. Bind the operator principal to serviceAccountUser on BOTH new SAs
        # so each VM-attach succeeds. Binding both up front lets the two IAM
        # propagation windows overlap (one VM-create budget, not two).
        member = _resolve_principal_member()
        _bind_service_account_user(sa_email, member)
        _bind_service_account_user(other_sa_email, member)

        # 3. targetServiceAccounts firewall (service-identity scoping — NOT
        # network_tags). Applies implicitly to VMs whose SA matches sa_email.
        fw = build_firewall(
            firewall_name,
            network_name,
            project,
            direction="INGRESS",
            allowed=_probe_allowed(),
            source_ranges=["0.0.0.0/0"],
            target_service_accounts=[sa_email],
        )
        firewall_created = True
        insert_firewall(project, fw)
        tests["create_sg"] = {"passed": True}
        tests["apply_service_rule"] = {"passed": True, "target_service_account": sa_email}

        # 4. Allowed VM carries the target SA; retry while IAM propagates.
        allowed_inst = build_probe_instance(
            project=project,
            zone=zone,
            name=allowed_vm,
            network_name=network_name,
            subnet_name=subnet_name,
            external_ip=False,
            service_accounts=[sa_email],
        )
        created_instances.append(allowed_vm)
        _insert_instance_with_iam_propagation(project, zone, allowed_inst)

        # 5. Other VM carries the DISTINCT non-target SA (other_sa_email) — a
        # real, reliably-serialized identity that the firewall does NOT
        # target, so the rule does not apply to this VM.
        other_inst = build_probe_instance(
            project=project,
            zone=zone,
            name=other_vm,
            network_name=network_name,
            subnet_name=subnet_name,
            external_ip=False,
            service_accounts=[other_sa_email],
        )
        created_instances.append(other_vm)
        _insert_instance_with_iam_propagation(project, zone, other_inst)

        # TWO INDEPENDENT observations, one per VM.
        fw_sas = set(get_firewall(project, firewall_name).target_service_accounts or ())
        allowed_sas = {sa.email for sa in (get_instance(project, zone, allowed_vm).service_accounts or ())}
        other_sas = {sa.email for sa in (get_instance(project, zone, other_vm).service_accounts or ())}

        tests["service_endpoint_allowed"] = {
            "passed": fw_sas == {sa_email} and sa_email in allowed_sas,
            "message": "firewall targetServiceAccounts matches and allowed VM carries the target SA",
        }
        tests["other_endpoint_blocked"] = {
            "passed": sa_email not in other_sas and other_sa_email in other_sas,
            "message": "other VM carries a distinct non-target SA (firewall does not apply)",
        }
    finally:
        # Cleanup ordering: VMs first (so the SAs are detachable), then the
        # firewall, then both SAs, then subnet + network.
        sa_cleanup = [e for e in (sa_email, other_sa_email) if e]
        cleanup_errors = _cleanup(
            project,
            zone=zone,
            region=region,
            instances=created_instances,
            firewalls=[firewall_name] if firewall_created else [],
            subnets=[subnet_name] if subnet_created else [],
            networks=[network_name] if network_created else [],
            service_accounts=sa_cleanup,
        )
        tests["cleanup"] = _cleanup_result(cleanup_errors)

    return tests


# --------------------------------------------------------------------- #
# Cleanup                                                               #
# --------------------------------------------------------------------- #


def _cleanup(
    project: str,
    *,
    zone: str,
    region: str,
    instances: list[str],
    firewalls: list[str],
    subnets: list[str],
    networks: list[str],
    service_accounts: list[str] | None = None,
) -> list[str]:
    """Best-effort teardown of every created resource. Returns a list of error strings.

    Dependency order: instances -> firewalls -> service accounts -> subnets
    -> networks. SA deletion retries the eventual-consistency / transient IAM
    window (NotFound counts as gone); a genuine post-retry leak is recorded as
    a cleanup error so the step cannot report a clean cleanup while leaking.
    """
    errors: list[str] = []

    for name in instances:
        if not delete_with_retry(delete_instance, project, zone, name, resource_desc=f"instance {name}"):
            errors.append(f"delete instance {name}")

    for name in firewalls:
        if not delete_with_retry(delete_firewall, project, name, resource_desc=f"firewall {name}"):
            errors.append(f"delete firewall {name}")

    for sa_email in service_accounts or ():
        if not _delete_target_sa(sa_email):
            errors.append(f"delete service account {sa_email}")

    for name in subnets:
        if not delete_with_retry(delete_subnetwork, project, region, name, resource_desc=f"subnetwork {name}"):
            errors.append(f"delete subnetwork {name}")

    for name in networks:
        if not delete_with_retry(delete_network, project, name, resource_desc=f"network {name}"):
            errors.append(f"delete network {name}")

    return errors


def _cleanup_result(errors: list[str]) -> dict[str, Any]:
    """Build the ``cleanup`` subtest entry from accumulated error strings."""
    entry: dict[str, Any] = {"passed": not errors}
    if errors:
        entry["error"] = "; ".join(errors)
    return entry


@handle_gcp_errors
def main() -> int:
    """Run the SG/firewall scoping test for the chosen scope and emit JSON."""
    parser = argparse.ArgumentParser(description="Test firewall rule scoping levels (GCP)")
    parser.add_argument("--region", required=True, help="GCP region")
    parser.add_argument("--zone", default=None, help="GCP zone (narrowed from --region if absent)")
    parser.add_argument(
        "--scope",
        required=True,
        choices=["workload", "node", "subnet", "service"],
        help="Scoping level to test",
    )
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    args = parser.parse_args()

    project = resolve_project(args.project)
    zone = args.zone or narrow_region_to_zone(args.region)

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": f"sg_{args.scope}_scoping",
        "scope": args.scope,
        "tests": {},
    }

    try:
        if args.scope in ("workload", "node"):
            result["tests"] = _drive_tag_scope(project, zone, args.scope)
        elif args.scope == "subnet":
            result["tests"] = _drive_subnet(project, zone)
        else:
            result["tests"] = _drive_service(project, zone)
        result["success"] = bool(result["tests"]) and all(t.get("passed") for t in result["tests"].values())
    except Exception as e:
        error_type, error_msg = classify_gcp_error(e)
        result["error_type"] = error_type
        result["error"] = error_msg

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
