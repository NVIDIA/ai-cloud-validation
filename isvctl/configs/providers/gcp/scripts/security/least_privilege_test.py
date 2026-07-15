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

"""Verify least-privilege IAM policy dimensions and minimal-role denial (SEC04-01/02).

This GCP implementation provisions a scoped fixture in the operator project and
probes it as the AWS reference does, translated to GCP primitives:

  * A custom IAM role (``IAMClient.create_role``) whose ``included_permissions``
    grant only ``storage.objects.list`` / ``storage.buckets.get`` — the minimal
    set the allowed probe needs.
  * A test service account (the principal that receives the scoped grant).
  * One probe Cloud Storage bucket (the single allowed resource).
  * A conditional IAM binding of the custom role to the service account, scoped
    by an IAM Condition CEL expression that references ``resource.name`` (the
    resource dimension), ``request.time`` (a temporal bound), and
    ``'<access-level>' in request.auth.access_levels`` — the network dimension.
    GCP IAM Conditions have no source-IP attribute, so the AWS ``aws:SourceIp``
    grant maps to an Access Context Manager access level supplied by the
    operator via ``--access-level``.

The least-privilege dimensions named by SEC04-01 are then evidenced:

  * user-based: only the test service account receives the scoped grant.
  * resource-based: only the one bucket is named in the binding condition, and
    an out-of-scope bucket read is denied.
  * network-based: the binding condition carries the access-level restriction.
  * allowed action: an in-scope Cloud Storage list succeeds.

SEC04-02 is checked by confirming out-of-scope compute, storage, and network
calls raise ``PermissionDenied`` (403). Cloud Storage hides a bucket from a
zero-grant principal as ``NotFound`` (404), so the storage deny probe accepts a
404 as a valid deny alongside a 403.

When ``--access-level`` is empty (the network dimension cannot be expressed) or
the orchestrator principal cannot create the fixture, the script emits a
structured ``skipped`` payload (exit 0) so the validations skip rather than
fabricate a pass. Created resources are cleaned up in a ``finally`` block.

Usage:
    python3 least_privilege_test.py --region us-central1 --project my-project \\
        --access-level accessPolicies/123/accessLevels/operator_cidr

Output JSON:
{
    "success": true,
    "platform": "security",
    "test_name": "least_privilege_test",
    "test_identity": "isv-sec04-lp-...@my-project.iam.gserviceaccount.com",
    "allowed_resource": "isv-sec04-lp-...",
    "allowed_source_cidr": "accessPolicies/123/accessLevels/operator_cidr",
    "role_created": true,
    "sa_created": true,
    "bucket_created": true,
    "tests": {
        "policy_dimensions_user_based": {"passed": true},
        "policy_dimensions_resource_based": {"passed": true, "probes": [...]},
        "policy_dimensions_network_based": {"passed": true, "probes": [...]},
        "policy_dimensions_allowed_action_succeeds": {"passed": true},
        "out_of_scope_compute_denied": {"passed": true, "probes": [...]},
        "out_of_scope_storage_denied": {"passed": true, "probes": [...]},
        "out_of_scope_network_denied": {"passed": true, "probes": [...]}
    }
}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from secrets import token_hex
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

import google.auth
import google.auth.impersonated_credentials
from common.compute import resolve_project, unique_suffix
from common.errors import delete_with_retry, handle_gcp_errors, modify_iam_policy_with_retry
from common.iam_policy import (
    ensure_unconditional_binding_member,
    remove_binding_members,
    service_account_member_email,
)
from common.ownership import (
    CREATED_BY_DESCRIPTION,
    CREATED_BY_LABEL,
    CREATED_BY_VALUE,
    description_with_invocation,
    has_invocation_description,
    has_invocation_label,
    labels_with_invocation,
    new_invocation_id,
    submit_owned_create,
)
from common.result import preserve_success_after_cleanup
from common.service_account import create_service_account, delete_service_account, resolve_principal_member
from google.api_core import exceptions as gax
from google.api_core import retry as gax_retry
from google.cloud import compute_v1, iam_admin_v1, storage
from google.cloud.iam_admin_v1 import types as iam_types

# Scopes the impersonated probe token carries so the scoped SA can exercise
# Cloud Storage / Compute APIs (and be denied where it lacks the permission).
_PROBE_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)
# IAM-propagation budget: a fresh role binding / tokenCreator grant takes a
# short while to become effective. Bounded retry inside the step timeout.
_PROPAGATION_ATTEMPTS = 8
_PROPAGATION_DELAY_SECONDS = 10
# Cloud Storage's create helper defaults to retrying 5xx/transport failures,
# which can turn commit-then-error into a final 409 and hide ownership recovery.
_NO_CREATE_RETRY = gax_retry.Retry(predicate=lambda _exc: False)

TEST_NAME = "least_privilege_test"

# Custom-role ids may not contain hyphens, so the run-id hex is joined to an
# underscore-only stem (the bucket / SA names below keep the hyphenated form).
_ROLE_ID_PREFIX = "isv_sec04_"
_SA_BASE = "isv-sec04-lp"
_BUCKET_BASE = "isv-sec04-lp"
_BINDING_CONDITION_TITLE = "isv-sec04-least-privilege"
_BINDING_CONDITION_DESCRIPTION = f"SEC04 least-privilege scoped grant ({CREATED_BY_DESCRIPTION})."
_CUSTOM_ROLE_DESCRIPTION = f"Scoped role for SEC04 least-privilege validation ({CREATED_BY_DESCRIPTION})."

# The minimal permission set the allowed probe (Cloud Storage list) needs. The
# role is deliberately narrow so it cannot satisfy any out-of-scope probe.
_ROLE_PERMISSIONS = ("storage.objects.list", "storage.buckets.get")

# Setup exceptions that mean "this principal cannot provision the fixture" — a
# structured skip, not a fabricated pass.
_SKIPPABLE_SETUP = (gax.PermissionDenied, gax.Forbidden)


def _run_id_hex() -> str:
    """Return the suite RUN_ID (hex-safe stem) or a fresh discriminator."""
    sid = os.environ.get("RUN_ID") or os.environ.get("LS_RUN_ID") or ""
    cleaned = "".join(ch for ch in sid if ch.isalnum()).lower()
    return cleaned[:8] if cleaned else token_hex(4)


def _service_account_id() -> str:
    """Return a <=30-char fixture ID with discriminator then run suffix."""
    return unique_suffix(f"{_SA_BASE}-{token_hex(2)}")


def _skipped_result(reason: str) -> dict[str, Any]:
    """Return a structured top-level skip payload honored by both validators."""
    return {
        "success": True,
        "platform": "security",
        "test_name": TEST_NAME,
        "skipped": True,
        "skip_reason": reason,
        "test_identity": "",
        "allowed_resource": "",
        "allowed_source_cidr": "",
        "tests": {},
    }


def _create_bucket(
    storage_client: storage.Client,
    name: str,
    location: str,
    *,
    on_accepted: Callable[[], None] | None = None,
) -> None:
    """Create a pre-labelled bucket and reconcile an ambiguous acknowledgement."""
    invocation_id = new_invocation_id()
    bucket = storage_client.bucket(name)
    bucket.labels = labels_with_invocation({CREATED_BY_LABEL: CREATED_BY_VALUE}, invocation_id)
    submit_owned_create(
        lambda: storage_client.create_bucket(bucket, location=location, retry=_NO_CREATE_RETRY),
        lambda: storage_client.get_bucket(name),
        lambda resource: has_invocation_label(resource, invocation_id),
        on_accepted=on_accepted,
    )


def _condition_expression(allowed_bucket: str, access_level: str) -> str:
    """Build the IAM Condition CEL covering the three least-privilege dimensions.

    * resource dimension: ``resource.name`` is pinned to the one allowed bucket.
    * temporal bound: ``request.time`` keeps the grant time-scoped.
    * network dimension: ``'<access-level>' in request.auth.access_levels`` is
      the GCP analog of the AWS ``aws:SourceIp`` condition (IAM Conditions have
      no source-IP attribute, so the operator's source restriction is expressed
      as an Access Context Manager access level).
    """
    resource_suffix = f"buckets/{allowed_bucket}"
    return (
        f'resource.name.endsWith("{resource_suffix}") '
        f'&& request.time < timestamp("2099-01-01T00:00:00Z") '
        f"&& '{access_level}' in request.auth.access_levels"
    )


def _condition_has_dimensions(expression: str, allowed_bucket: str, access_level: str) -> tuple[bool, bool]:
    """Return (resource_scoped, network_scoped) by inspecting the condition CEL."""
    resource_scoped = f"buckets/{allowed_bucket}" in expression
    network_scoped = f"'{access_level}' in request.auth.access_levels" in expression
    return resource_scoped, network_scoped


def _create_custom_role(
    iam_client: iam_admin_v1.IAMClient,
    project: str,
    role_id: str,
    *,
    on_accepted: Callable[[], None] | None = None,
) -> str:
    """Create the scoped custom role with exact ambiguous-ack reconciliation."""
    invocation_id = new_invocation_id()
    role_name = f"projects/{project}/roles/{role_id}"
    role = iam_types.Role(
        title="ISV SEC04 least-privilege probe role",
        description=description_with_invocation(_CUSTOM_ROLE_DESCRIPTION, invocation_id),
        included_permissions=list(_ROLE_PERMISSIONS),
        stage=iam_types.Role.RoleLaunchStage.GA,
    )
    request = iam_types.CreateRoleRequest(parent=f"projects/{project}", role_id=role_id, role=role)
    created = submit_owned_create(
        lambda: iam_client.create_role(request=request, retry=_NO_CREATE_RETRY),
        lambda: iam_client.get_role(request=iam_types.GetRoleRequest(name=role_name)),
        lambda resource: has_invocation_description(resource, invocation_id),
        on_accepted=on_accepted,
    )
    return created.name


def _bind_role_with_condition(
    project: str,
    sa_email: str,
    role_name: str,
    expression: str,
    *,
    on_write_attempt: Callable[[], None] | None = None,
) -> None:
    """Bind the scoped role to the test SA on the allowed bucket with an IAM Condition.

    The binding is attached at the project IAM policy with a CEL condition so
    the grant only applies to the one allowed bucket and only when the operator
    access level is satisfied — the user/resource/network dimensions in one
    conditional binding.
    """
    # Imported lazily so the module import stays valid in environments that
    # vendor resource-manager separately from the rest of the google.cloud SDK.
    from google.cloud import resourcemanager_v3
    from google.iam.v1 import iam_policy_pb2, options_pb2, policy_pb2
    from google.type import expr_pb2

    resource = f"projects/{project}"
    binding = policy_pb2.Binding(
        role=role_name,
        members=[f"serviceAccount:{sa_email}"],
        condition=expr_pb2.Expr(
            title=_BINDING_CONDITION_TITLE,
            description=_BINDING_CONDITION_DESCRIPTION,
            expression=expression,
        ),
    )
    projects = resourcemanager_v3.ProjectsClient()

    def _read() -> Any:
        return projects.get_iam_policy(
            request=iam_policy_pb2.GetIamPolicyRequest(
                resource=resource,
                options=options_pb2.GetPolicyOptions(requested_policy_version=3),
            )
        )

    def _write(policy: Any) -> Any:
        policy.version = 3
        if on_write_attempt is not None:
            on_write_attempt()
        return projects.set_iam_policy(request=iam_policy_pb2.SetIamPolicyRequest(resource=resource, policy=policy))

    def _append_once(policy: Any) -> bool:
        if any(_binding_matches(item, sa_email, role_name, expression) for item in policy.bindings):
            return False
        policy.bindings.append(binding)
        return True

    modify_iam_policy_with_retry(
        _read,
        _write,
        _append_once,
        resource_desc=f"project {project}",
    )


def _binding_matches(binding: Any, sa_email: str, role_name: str, expression: str) -> bool:
    """Return whether a project binding is exactly the SEC04 fixture grant."""
    condition = getattr(binding, "condition", None)
    return (
        binding.role == role_name
        and f"serviceAccount:{sa_email}" in binding.members
        and getattr(condition, "title", "") == _BINDING_CONDITION_TITLE
        and getattr(condition, "description", "") == _BINDING_CONDITION_DESCRIPTION
        and getattr(condition, "expression", "") == expression
    )


def _remove_role_binding(project: str, sa_email: str, role_name: str, expression: str) -> None:
    """Remove only this fixture's project binding using fresh etags on retry."""
    from google.cloud import resourcemanager_v3
    from google.iam.v1 import iam_policy_pb2, options_pb2

    projects = resourcemanager_v3.ProjectsClient()
    resource = f"projects/{project}"

    def _read() -> Any:
        return projects.get_iam_policy(
            request=iam_policy_pb2.GetIamPolicyRequest(
                resource=resource,
                options=options_pb2.GetPolicyOptions(requested_policy_version=3),
            )
        )

    def _write(policy: Any) -> Any:
        policy.version = 3
        return projects.set_iam_policy(request=iam_policy_pb2.SetIamPolicyRequest(resource=resource, policy=policy))

    def _remove(policy: Any) -> bool:
        removed = remove_binding_members(
            policy,
            binding_matches=lambda binding: _binding_matches(binding, sa_email, role_name, expression),
            member_matches=lambda candidate: service_account_member_email(candidate) == sa_email,
        )
        return removed > 0

    modify_iam_policy_with_retry(
        _read,
        _write,
        _remove,
        resource_desc=f"project {project}",
    )


def _grant_token_creator(sa_email: str, member: str) -> None:
    """Grant ``member`` roles/iam.serviceAccountTokenCreator on the test SA.

    The run principal must be allowed to mint short-lived tokens for the test SA
    so the out-of-scope probes can run AS that least-privileged identity (the
    orchestrator credential is broadly privileged and would not be denied).
    """
    from google.iam.v1 import iam_policy_pb2

    iam_client = iam_admin_v1.IAMClient()
    resource = f"projects/-/serviceAccounts/{sa_email}"

    def _read() -> Any:
        return iam_client.get_iam_policy(request=iam_policy_pb2.GetIamPolicyRequest(resource=resource))

    def _write(policy: Any) -> Any:
        return iam_client.set_iam_policy(request=iam_policy_pb2.SetIamPolicyRequest(resource=resource, policy=policy))

    def _ensure(policy: Any) -> bool:
        return ensure_unconditional_binding_member(policy, "roles/iam.serviceAccountTokenCreator", member)

    modify_iam_policy_with_retry(_read, _write, _ensure, resource_desc=f"service account {sa_email}")


def _impersonated_credentials(sa_email: str) -> Any:
    """Build short-lived impersonated credentials for the scoped test SA."""
    source, _ = google.auth.default(scopes=list(_PROBE_SCOPES))
    return google.auth.impersonated_credentials.Credentials(
        source_credentials=source,
        target_principal=sa_email,
        target_scopes=list(_PROBE_SCOPES),
        lifetime=600,
    )


def _probe_allowed_with_retry(storage_client: Any, allowed_bucket: str) -> dict[str, Any]:
    """List the allowed bucket as the scoped SA, retrying while the grant propagates."""
    last_error = ""
    for attempt in range(_PROPAGATION_ATTEMPTS):
        try:
            list(storage_client.list_blobs(allowed_bucket, max_results=1))
        except (gax.PermissionDenied, gax.Forbidden, gax.NotFound) as exc:
            # The scoped grant or the SA's view of the bucket may not have
            # propagated yet; retry within the budget.
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < _PROPAGATION_ATTEMPTS - 1:
                time.sleep(_PROPAGATION_DELAY_SECONDS)
                continue
        except Exception as exc:
            return {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
        else:
            return {"passed": True, "message": "in-scope Cloud Storage list succeeded on the allowed bucket"}
    return {"passed": False, "error": f"allowed scoped-resource list did not succeed: {last_error}"}


def _is_denied(exc: Exception) -> bool:
    """Return True when the exception represents an authorization deny (403)."""
    return isinstance(exc, gax.PermissionDenied | gax.Forbidden)


def _is_hidden_or_denied(exc: Exception) -> bool:
    """Return True for a 403 deny OR a 404 hide.

    Cloud Storage returns ``NotFound`` (404) rather than ``PermissionDenied``
    (403) to a principal with zero grants on a bucket, hiding its existence. A
    404 is therefore a valid out-of-scope storage deny.
    """
    return _is_denied(exc) or isinstance(exc, gax.NotFound)


def _expect_denied(name: str, fn: Callable[[], Any], *, deny_predicate: Callable[[Exception], bool]) -> dict[str, Any]:
    """Run one probe and return passed=True only when it is denied.

    A successful call means the minimal grant was broader than intended (a
    SEC04-02 failure); any other error is an inconclusive probe (passed=False
    with the error recorded).
    """
    try:
        fn()
    except Exception as exc:  # classify, never crash the probe
        if deny_predicate(exc):
            return {"name": name, "passed": True, "code": type(exc).__name__}
        return {"name": name, "passed": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"name": name, "passed": False, "error": "out-of-scope action unexpectedly succeeded"}


def _aggregate(probes: list[dict[str, Any]], *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Aggregate per-probe outcomes into a validation test envelope with evidence."""
    passed = bool(probes) and all(p.get("passed") for p in probes)
    out: dict[str, Any] = {"passed": passed, "probes": probes}
    if extra:
        out.update(extra)
    if not passed:
        out["error"] = "; ".join(
            p.get("error") or f"{p['name']} returned {p.get('code', 'unknown')}" for p in probes if not p.get("passed")
        )
    return out


class _SkipSignal(Exception):
    """Internal sentinel: a structured skip payload was already printed."""


def _cleanup(
    project: str,
    role_name: str,
    sa_email: str,
    buckets_created: list[str],
    binding_expression: str,
    binding_write_attempted: bool,
    result: dict[str, Any],
) -> None:
    """Best-effort delete of the project binding and fixture resources."""
    cleanup_errors: list[str] = []

    if project and role_name and sa_email and binding_expression and binding_write_attempted:
        try:
            _remove_role_binding(project, sa_email, role_name, binding_expression)
        except Exception as exc:
            cleanup_errors.append(f"remove project binding {role_name}: {type(exc).__name__}")

    if (
        result.get("role_created")
        and role_name
        and not delete_with_retry(
            iam_admin_v1.IAMClient().delete_role,
            request=iam_types.DeleteRoleRequest(name=role_name),
            resource_desc=f"custom role {role_name}",
        )
    ):
        cleanup_errors.append(f"delete role {role_name}")

    if result.get("sa_created") and sa_email and not delete_service_account(sa_email, project=project):
        cleanup_errors.append(f"delete service account {sa_email}")

    if buckets_created and project:
        client = storage.Client(project=project)
        for name in buckets_created:
            try:
                client.bucket(name).delete(force=True)
            except gax.NotFound:
                pass
            except Exception as exc:
                cleanup_errors.append(f"delete bucket {name}: {type(exc).__name__}")

    if cleanup_errors:
        result["cleanup_errors"] = cleanup_errors


@handle_gcp_errors
def main() -> int:
    """Provision the SEC04 fixture, run positive and negative probes, emit JSON."""
    parser = argparse.ArgumentParser(description="Least-privilege policy and minimal-role enforcement test")
    parser.add_argument("--region", default="")
    parser.add_argument("--project", default="")
    parser.add_argument("--skip-destroy", action="store_true", help="Preserve run-owned fixtures for later teardown")
    parser.add_argument(
        "--access-level",
        default="",
        help="Access Context Manager access-level resource path (the aws:SourceIp analog).",
    )
    args = parser.parse_args()

    access_level = args.access_level.strip()
    if not access_level:
        print(
            json.dumps(
                _skipped_result(
                    "no --access-level supplied; the network dimension requires an Access Context "
                    "Manager access level (set GCP_SECURITY_ACCESS_LEVEL)"
                ),
                indent=2,
            )
        )
        return 0

    run_hex = _run_id_hex()
    role_id = f"{_ROLE_ID_PREFIX}{run_hex}"
    # The SA local-part is capped at 30 chars.  Keep the per-invocation
    # discriminator before the canonical run suffix so both same-run retries
    # and the suite's terminal-suffix cleanup remain reliable.
    sa_account_id = _service_account_id()
    allowed_bucket = unique_suffix(_BUCKET_BASE)
    denied_bucket = unique_suffix(f"{_BUCKET_BASE}-deny")

    result: dict = {
        "success": False,
        "platform": "security",
        "test_name": TEST_NAME,
        "test_identity": "",
        "allowed_resource": allowed_bucket,
        "allowed_source_cidr": access_level,
        "role_created": False,
        "binding_created": False,
        "sa_created": False,
        "bucket_created": False,
        "tests": {
            "policy_dimensions_user_based": {"passed": False},
            "policy_dimensions_resource_based": {"passed": False},
            "policy_dimensions_network_based": {"passed": False},
            "policy_dimensions_allowed_action_succeeds": {"passed": False},
            "out_of_scope_compute_denied": {"passed": False},
            "out_of_scope_storage_denied": {"passed": False},
            "out_of_scope_network_denied": {"passed": False},
        },
    }

    project = ""
    role_name = ""
    sa_email = ""
    buckets_created: list[str] = []
    binding_expression = ""
    binding_write_attempted = False
    skip_payload: dict[str, Any] | None = None

    try:
        project = resolve_project(args.project)
        location = args.region or "us-central1"
        iam_client = iam_admin_v1.IAMClient()
        storage_client = storage.Client(project=project)

        try:
            role_name = f"projects/{project}/roles/{role_id}"
            role_name = _create_custom_role(
                iam_client,
                project,
                role_id,
                on_accepted=lambda: result.update(role_created=True),
            )

            sa_email = f"{sa_account_id}@{project}.iam.gserviceaccount.com"
            sa_email = create_service_account(
                project,
                sa_account_id,
                display_name="ISV SEC04 least-privilege probe",
                description=CREATED_BY_DESCRIPTION,
                on_accepted=lambda: result.update(sa_created=True),
            )
            result["test_identity"] = sa_email

            # Labels are part of each create request.  The callback records the
            # bucket after a normal acknowledgement or invocation-marker
            # readback, so the finally block can clean up commit-then-error
            # outcomes without adopting a same-name foreign bucket.
            def _record_bucket(name: str) -> None:
                buckets_created.append(name)
                result["bucket_created"] = True

            _create_bucket(
                storage_client,
                allowed_bucket,
                location,
                on_accepted=lambda: _record_bucket(allowed_bucket),
            )
            _create_bucket(
                storage_client,
                denied_bucket,
                location,
                on_accepted=lambda: _record_bucket(denied_bucket),
            )

            # Complete fixture provisioning inside the setup permission
            # boundary. Track the transition into SET separately from building
            # the expression: a denied GET cannot have created a binding, while
            # any attempted SET needs exact readback during cleanup because its
            # acknowledgement may be ambiguous.
            binding_expression = _condition_expression(allowed_bucket, access_level)

            def _record_binding_write_attempt() -> None:
                nonlocal binding_write_attempted
                binding_write_attempted = True

            _bind_role_with_condition(
                project,
                sa_email,
                role_name,
                binding_expression,
                on_write_attempt=_record_binding_write_attempt,
            )
            result["binding_created"] = True
            _grant_token_creator(sa_email, resolve_principal_member())
        except _SKIPPABLE_SETUP as exc:
            # Defer the skip emit: anything created so far is cleaned up in the
            # finally block first, and the skip is only honored when that cleanup
            # left nothing behind (see the post-finally gate). A missing-permission
            # setup is a structured skip, not a fabricated pass.
            skip_payload = _skipped_result(f"cannot provision SEC04 fixture: {exc}")
            raise _SkipSignal from exc

        resource_scoped, network_scoped = _condition_has_dimensions(binding_expression, allowed_bucket, access_level)

        # Build clients authenticated as the scoped test SA.
        sa_creds = _impersonated_credentials(sa_email)
        sa_storage = storage.Client(project=project, credentials=sa_creds)
        sa_compute = compute_v1.InstancesClient(credentials=sa_creds)
        sa_networks = compute_v1.NetworksClient(credentials=sa_creds)

        # Allowed action: an in-scope Cloud Storage list on the one allowed
        # bucket succeeds (the scoped role grants exactly this), retrying while
        # the fresh grant propagates.
        result["tests"]["policy_dimensions_allowed_action_succeeds"] = _probe_allowed_with_retry(
            sa_storage, allowed_bucket
        )
        allowed_ok = bool(result["tests"]["policy_dimensions_allowed_action_succeeds"].get("passed"))

        # user-based: the allowed action succeeded AS the dedicated test SA (the
        # only principal the scoped role is bound to).
        result["tests"]["policy_dimensions_user_based"] = {
            "passed": allowed_ok and bool(sa_email),
            "message": "scoped role bound only to the dedicated test service account",
        }

        # SEC04-02 out-of-scope deny probes, run as the scoped SA. The role
        # grants none of these permissions, so each must be denied. The storage
        # probe accepts a 404 hide alongside a 403 deny.
        compute_probes = [
            _expect_denied(
                "compute_get_denied",
                lambda: sa_compute.get(project=project, zone=f"{location}-a", instance=denied_bucket),
                deny_predicate=_is_denied,
            ),
        ]
        storage_probes = [
            _expect_denied(
                "storage_get_unscoped_bucket_denied",
                lambda: list(sa_storage.list_blobs(denied_bucket, max_results=1)),
                deny_predicate=_is_hidden_or_denied,
            ),
        ]
        network_probes = [
            _expect_denied(
                "network_list_denied",
                lambda: list(sa_networks.list(project=project)),
                deny_predicate=_is_denied,
            ),
        ]
        result["tests"]["out_of_scope_compute_denied"] = _aggregate(compute_probes)
        result["tests"]["out_of_scope_storage_denied"] = _aggregate(storage_probes)
        result["tests"]["out_of_scope_network_denied"] = _aggregate(network_probes)

        # resource-based: the in-scope action succeeded AND the unscoped bucket
        # was denied — the binding condition pins exactly the one allowed bucket.
        result["tests"]["policy_dimensions_resource_based"] = {
            "passed": allowed_ok and bool(storage_probes[0].get("passed")) and resource_scoped,
            "message": "allowed scoped bucket; denied the unscoped bucket",
            "probes": [
                {"name": "condition_resource_pinned", "passed": resource_scoped, "resource": allowed_bucket},
                storage_probes[0],
            ],
        }
        # network-based: the in-scope action succeeded AND the binding condition
        # carries the access-level restriction (the GCP source-restriction
        # dimension). Gating on both mirrors the AWS oracle's
        # ``allowed_passed and source_condition_matches`` conjunction — the
        # access-level scoping only proves least-privilege if the allowed action
        # it scopes actually went through.
        result["tests"]["policy_dimensions_network_based"] = {
            "passed": allowed_ok and network_scoped,
            "message": (
                "allowed action succeeded under the access-level-restricted binding condition"
                if allowed_ok and network_scoped
                else "network dimension requires both the allowed action and the access-level restriction"
            ),
            "probes": [
                {"name": "allowed_action_succeeds", "passed": allowed_ok},
                {"name": "condition_access_level_present", "passed": network_scoped, "access_level": access_level},
            ],
        }
        result["success"] = all(t.get("passed") for t in result["tests"].values())
    except _SkipSignal:
        # The skip payload is emitted after the finally block, gated on cleanup
        # leaving nothing behind (see the post-finally check).
        pass
    except Exception as e:
        result["error"] = str(e)
        result["success"] = False
    finally:
        if args.skip_destroy:
            result["cleanup_skipped"] = True
        else:
            _cleanup(
                project,
                role_name,
                sa_email,
                buckets_created,
                binding_expression,
                binding_write_attempted,
                result,
            )

    # A structured skip wins only when cleanup left nothing behind, so a cleanup
    # failure on the skip path surfaces as a real failure (carrying
    # cleanup_errors) instead of a clean-looking skip that hides a leak.
    if skip_payload is not None and not result.get("cleanup_errors"):
        print(json.dumps(skip_payload, indent=2))
        return 0

    preserve_success_after_cleanup(result)
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
