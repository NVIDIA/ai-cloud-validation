# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Exact, retry-safe IAM lifecycle for Compute Engine use of a CMEK key."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from google.cloud import kms_v1, resourcemanager_v3
from google.iam.v1 import iam_policy_pb2, options_pb2, policy_pb2

from common.errors import modify_iam_policy_with_retry

KMS_ENCRYPTER_ROLE = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
_GCE_AGENT_TEMPLATE = "service-{number}@compute-system.iam.gserviceaccount.com"


class _PolicyAlreadyDesired(Exception):
    """Stop a read-modify-write retry after readback proves the desired state."""


def compute_service_agent_member(
    project: str,
    *,
    projects_client: resourcemanager_v3.ProjectsClient | None = None,
) -> str:
    """Return the IAM member for ``project``'s Compute Engine service agent."""
    client = projects_client or resourcemanager_v3.ProjectsClient()
    resource = client.get_project(name=f"projects/{project}")
    project_number = resource.name.split("/", 1)[1]
    email = _GCE_AGENT_TEMPLATE.format(number=project_number)
    return f"serviceAccount:{email}"


def _is_unconditional(binding: Any) -> bool:
    """Return whether a binding has no IAM condition."""
    condition = getattr(binding, "condition", None)
    return not any(str(getattr(condition, field, "") or "") for field in ("title", "description", "expression"))


def _read_policy(client: kms_v1.KeyManagementServiceClient, key_name: str) -> Any:
    return client.get_iam_policy(
        request=iam_policy_pb2.GetIamPolicyRequest(
            resource=key_name,
            options=options_pb2.GetPolicyOptions(requested_policy_version=3),
        )
    )


def _write_policy(client: kms_v1.KeyManagementServiceClient, key_name: str, policy: Any) -> Any:
    policy.version = 3
    return client.set_iam_policy(request=iam_policy_pb2.SetIamPolicyRequest(resource=key_name, policy=policy))


def _clone_binding(binding: Any) -> Any:
    """Clone a live protobuf binding or an intent-harness message double."""
    clone = policy_pb2.Binding()
    copy_from = getattr(clone, "CopyFrom", None)
    if callable(copy_from):
        copy_from(binding)
        return clone
    return copy.deepcopy(binding)


def _replace_repeated(owner: Any, field: str, values: list[Any]) -> None:
    """Replace a repeated field on protobufs and intent-harness message doubles."""
    clear_field = getattr(owner, "ClearField", None)
    if callable(clear_field):
        clear_field(field)
    else:
        getattr(owner, field).clear()
    getattr(owner, field).extend(values)


def ensure_kms_role_member(
    client: kms_v1.KeyManagementServiceClient,
    key_name: str,
    member: str,
    *,
    on_added: Callable[[], None] | None = None,
) -> bool:
    """Ensure the exact unconditional KMS role member, returning whether this call added it.

    ``on_added`` runs before the first SET attempt. That monotonic ownership
    signal survives a commit-then-response-loss retry, allowing the caller to
    roll the grant back even when the original SET outcome was ambiguous.
    """
    attempted_add = False

    def _mutate(policy: Any) -> None:
        nonlocal attempted_add
        role_binding = None
        for binding in policy.bindings:
            if binding.role != KMS_ENCRYPTER_ROLE or not _is_unconditional(binding):
                continue
            role_binding = binding
            if member in binding.members:
                raise _PolicyAlreadyDesired

        if not attempted_add:
            attempted_add = True
            if on_added is not None:
                on_added()
        if role_binding is None:
            policy.bindings.append(policy_pb2.Binding(role=KMS_ENCRYPTER_ROLE, members=[member]))
        else:
            role_binding.members.append(member)

    try:
        modify_iam_policy_with_retry(
            lambda: _read_policy(client, key_name),
            lambda policy: _write_policy(client, key_name, policy),
            _mutate,
            resource_desc=f"CryptoKey {key_name}",
        )
    except _PolicyAlreadyDesired:
        pass
    return attempted_add


def remove_kms_role_member(
    client: kms_v1.KeyManagementServiceClient,
    key_name: str,
    member: str,
) -> bool:
    """Remove only the exact unconditional KMS role member, preserving all else."""
    attempted_remove = False

    def _mutate(policy: Any) -> None:
        nonlocal attempted_remove
        retained: list[policy_pb2.Binding] = []
        removed = False
        for binding in policy.bindings:
            clone = _clone_binding(binding)
            if binding.role == KMS_ENCRYPTER_ROLE and _is_unconditional(binding) and member in binding.members:
                _replace_repeated(clone, "members", [existing for existing in binding.members if existing != member])
                removed = True
            if clone.members:
                retained.append(clone)

        if not removed:
            raise _PolicyAlreadyDesired
        attempted_remove = True
        _replace_repeated(policy, "bindings", retained)

    try:
        modify_iam_policy_with_retry(
            lambda: _read_policy(client, key_name),
            lambda policy: _write_policy(client, key_name, policy),
            _mutate,
            resource_desc=f"CryptoKey {key_name}",
        )
    except _PolicyAlreadyDesired:
        pass
    return attempted_remove
