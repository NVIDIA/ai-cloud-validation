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

"""AWS FSx Lustre ``StorageProvider`` shim.

* `health_check` / `get_tenant_quota.hard_limit_bytes`
  via ``servicequotas:GetServiceQuota(ServiceCode="fsx", QuotaCode=...)``.
* `get_tenant_quota.used_bytes` via ``fsx:DescribeFileSystems`` summed
  ``StorageCapacity`` for filesystems matching ``FSX_DEPLOYMENT_TYPE``
  (matches ``storageRequested`` in
  ``nv_storage_controller.go:reconcileStatus``). When ``FSX_QUOTA_CODE``
  overrides the quota code, usage still filters on ``FSX_DEPLOYMENT_TYPE``.
* `list_volumes` via ``fsx:DescribeFileSystems``, one ``Volume`` per
  filesystem (``get_volume`` is served by the SDK base via ``list_volumes``).
* `create_volume` / `delete_volume` are left unimplemented - the
  FSx CSI driver (``fsx.csi.aws.com``) owns volume lifecycle on EKS, so
  the acceptance suite falls back to inventorying via ``list_volumes``.

The shim subclasses ``Implementation`` and is served through
``new_implementation()``: it implements only the surfaces it backs, and the SDK
*detects* which are supported. Which surfaces are *supported* is also declared in
the sibling ``config/storage-provider-manifest.yaml`` (the contract); the
validation suite probes each declared-supported surface at runtime. Surfaces this
shim does NOT back (tenant enumeration, volume lifecycle, directory/user quotas)
are simply left undefined - detected as unimplemented and gated - and the
manifest declares them ``none``.

Tenant = AWS account + region. ``tenant_id`` is resolved from
``sts:GetCallerIdentity`` at construction time (with ``AWS_ACCOUNT_ID``
override for environments without STS access).

Environment variables:
    AWS_REGION              Region the AWS clients target. Falls back to the
                            boto3 session region (AWS_DEFAULT_REGION or the
                            AWS profile's region).
    AWS_PROFILE / AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY [+ AWS_SESSION_TOKEN]
                            Standard boto3 credential chain - no custom logic.
    FSX_DEPLOYMENT_TYPE     Optional. One of PERSISTENT_2 (default),
                            PERSISTENT_1, SCRATCH_1, SCRATCH_2. Chooses
                            which AWS Service Quota code we read. Overrides
                            the manifest's attributes.deployment_type.
    FSX_QUOTA_CODE          Optional override for the deployment-type
                            mapping (e.g. for a quota code we don't know
                            about). Wins over FSX_DEPLOYMENT_TYPE.
    AWS_ACCOUNT_ID          Optional override for the STS-resolved
                            tenant id (useful in CI / restricted envs).

Required IAM actions:
    sts:GetCallerIdentity, servicequotas:GetServiceQuota,
    fsx:DescribeFileSystems

See ``isvctl/configs/providers/aws/scripts/storage/README.md``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

from boto3.session import Session as Boto3Session
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    NoCredentialsError,
    ProfileNotFound,
    TokenRetrievalError,
)
from isvtest.core.storage_provider import (
    API_VERSION,
    AuthenticationError,
    CsiSpec,
    GetTenantQuotaRequest,
    Implementation,
    ListVolumesRequest,
    ListVolumesResponse,
    ProviderProperties,
    StorageApiError,
    StorageProvider,
    TagFilter,
    TenantQuota,
    VersionMetadata,
    Volume,
    VolumeState,
    new_implementation,
)

GIB = 1024**3
FSX_SERVICE_CODE = "fsx"

# Maps FSx Lustre deployment types to the matching AWS Service Quota code.
DEPLOYMENT_TYPE_TO_QUOTA_CODE: dict[str, str] = {
    "PERSISTENT_2": "L-8F1B9C74",
    "PERSISTENT_1": "L-C8640C82",
    "SCRATCH_1": "L-AD2FC696",
    "SCRATCH_2": "L-AD2FC696",
}
DEFAULT_DEPLOYMENT_TYPE = "PERSISTENT_2"

# ClientError codes we surface as AuthenticationError so the acceptance
# suite can distinguish auth failures from other errors.
_AUTH_ERROR_CODES: frozenset[str] = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "UnauthorizedOperation",
        "InvalidClientTokenId",
        "AuthFailure",
        "ExpiredToken",
        "ExpiredTokenException",
        "SignatureDoesNotMatch",
        "InvalidSignatureException",
    }
)

# Map FSx Lifecycle (DescribeFileSystems response) onto the shim's
# VolumeState literal. Anything unknown -> "failed" so the acceptance
# suite's state assertion catches it instead of silently passing.
_LIFECYCLE_TO_STATE: dict[str, VolumeState] = {
    "AVAILABLE": "available",
    "CREATING": "creating",
    "DELETING": "deleting",
    "FAILED": "failed",
    "MISCONFIGURED": "failed",
    "MISCONFIGURED_UNAVAILABLE": "failed",
    "UPDATING": "available",
}


def _resolve_quota_code(deployment_type: str) -> str:
    """Resolve an AWS service quota code by quota name."""
    try:
        return DEPLOYMENT_TYPE_TO_QUOTA_CODE[deployment_type]
    except KeyError as exc:
        valid = ", ".join(sorted(DEPLOYMENT_TYPE_TO_QUOTA_CODE))
        raise StorageApiError(
            f"FSX_DEPLOYMENT_TYPE={deployment_type!r} not recognised; "
            f"expected one of {valid}, or set FSX_QUOTA_CODE explicitly"
        ) from exc


def _classify_client_error(exc: Exception, *, context: str) -> StorageApiError:
    """Convert a boto3 exception into the shim's error taxonomy."""
    if isinstance(exc, NoCredentialsError):
        return AuthenticationError(f"{context}: AWS credentials not found")
    if isinstance(exc, ProfileNotFound):
        return AuthenticationError(f"{context}: AWS profile not found: {exc}")
    if isinstance(exc, TokenRetrievalError):
        return AuthenticationError(f"{context}: AWS credentials expired")
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in _AUTH_ERROR_CODES:
            return AuthenticationError(f"{context}: {code}: {exc}")
        return StorageApiError(f"{context}: {code or 'ClientError'}: {exc}")
    if isinstance(exc, BotoCoreError):
        return StorageApiError(f"{context}: {exc}")
    return StorageApiError(f"{context}: {type(exc).__name__}: {exc}")


def _tag_list_to_dict(tags: Any) -> dict[str, str]:
    """Convert AWS ``[{'Key': ..., 'Value': ...}, ...]`` to a flat dict."""
    if not isinstance(tags, list):
        return {}
    return {t["Key"]: t.get("Value", "") for t in tags if isinstance(t, dict) and "Key" in t}


class AwsFsxLustreApi(Implementation):
    """``StorageProvider`` over AWS FSx Lustre + Service Quotas + STS.

    Single-tenant: each instance is scoped to one AWS account in one
    region. To validate multiple accounts, declare multiple provider
    entries in the manifest (each shim load gets its own AWS clients).
    """

    def __init__(
        self,
        *,
        region: str | None = None,
        deployment_type: str | None = None,
        quota_code: str | None = None,
        account_id: str | None = None,
        attributes: Mapping[str, str] | None = None,
        session: Boto3Session | None = None,
    ) -> None:
        """Initialize the object with its configured dependencies."""
        self._session = session or Boto3Session()
        resolved_region = region or os.environ.get("AWS_REGION") or self._session.region_name
        if not resolved_region:
            raise StorageApiError(
                "No AWS region for the FSx Lustre shim: set AWS_REGION, AWS_DEFAULT_REGION, "
                "or a region in the AWS profile"
            )
        self._region = resolved_region

        resolved_deployment_type = (
            deployment_type
            or os.environ.get("FSX_DEPLOYMENT_TYPE")
            or (attributes or {}).get("deployment_type")
            or DEFAULT_DEPLOYMENT_TYPE
        )
        self._deployment_type = resolved_deployment_type
        self._quota_code = (
            quota_code or os.environ.get("FSX_QUOTA_CODE") or _resolve_quota_code(resolved_deployment_type)
        )

        core = ProviderProperties(
            provider_namespace="aws.amazon.com",
            provider_id="fsx-lustre",
            provider_metadata=VersionMetadata(
                vendor_name="NVIDIA",
                name="AWS FSx Lustre",
                version="0.1.0",
            ),
            sdk_version=API_VERSION,
            storage_type="file",
            storage_protocols=["lustre"],
            attributes={"region": self._region, "deployment_type": self._deployment_type},
        )
        self._core = core

        self._sq = self._session.client("service-quotas", region_name=self._region)
        self._fsx = self._session.client("fsx", region_name=self._region)
        self._sts = self._session.client("sts", region_name=self._region)

        self._default_tenant = account_id or os.environ.get("AWS_ACCOUNT_ID") or self._resolve_tenant_from_sts()

    def _resolve_tenant_from_sts(self) -> str:
        """Resolve the AWS tenant account ID via STS."""
        try:
            identity = self._sts.get_caller_identity()
        except Exception as exc:
            raise _classify_client_error(exc, context="sts:GetCallerIdentity") from exc
        account = identity.get("Account")
        if not account:
            raise StorageApiError("sts:GetCallerIdentity returned no Account")
        return str(account)

    def _resolve_tenant(self, tenant_id: str | None) -> str:
        """Resolve and validate the request tenant for this shim."""
        resolved = tenant_id or self._default_tenant
        if resolved != self._default_tenant:
            raise StorageApiError(
                f"tenant_id={resolved!r} does not match this shim's account "
                f"{self._default_tenant!r}; declare a separate provider entry per account"
            )
        return resolved

    def health_check(self) -> None:
        """Authenticated round-trip via ``GetServiceQuota``."""
        try:
            self._sq.get_service_quota(ServiceCode=FSX_SERVICE_CODE, QuotaCode=self._quota_code)
        except Exception as exc:
            raise _classify_client_error(
                exc,
                context=f"servicequotas:GetServiceQuota(ServiceCode={FSX_SERVICE_CODE}, QuotaCode={self._quota_code})",
            ) from exc

    def get_tenant_quota(self, req: GetTenantQuotaRequest) -> TenantQuota:
        """Hard limit from Service Quotas; used bytes summed from FSx StorageCapacity."""
        resolved = self._resolve_tenant(req.tenant_id)

        try:
            quota_response = self._sq.get_service_quota(ServiceCode=FSX_SERVICE_CODE, QuotaCode=self._quota_code)
        except Exception as exc:
            raise _classify_client_error(
                exc,
                context=f"servicequotas:GetServiceQuota(QuotaCode={self._quota_code})",
            ) from exc

        quota = quota_response.get("Quota", {})
        quota_value_gib = quota.get("Value")
        if quota_value_gib is None:
            raise StorageApiError(f"Service Quota {self._quota_code!r} returned no Value: {quota_response!r}")
        hard_limit_bytes = int(float(quota_value_gib) * GIB)
        quota_name = quota.get("QuotaName") or self._quota_code

        used_bytes = 0
        for fs in self._iter_lustre_filesystems():
            fs_deployment_type = (fs.get("LustreConfiguration") or {}).get("DeploymentType")
            if fs_deployment_type and fs_deployment_type != self._deployment_type:
                continue
            capacity_gib = fs.get("StorageCapacity") or 0
            used_bytes += int(capacity_gib) * GIB

        return TenantQuota(
            tenant_id=resolved,
            hard_limit_bytes=hard_limit_bytes,
            used_bytes=used_bytes,
            name=quota_name,
        )

    def list_volumes(self, req: ListVolumesRequest) -> ListVolumesResponse:
        """Yield one ``Volume`` per FSx Lustre filesystem in the account+region."""
        resolved = self._resolve_tenant(req.tenant_id)
        wanted_ids = set(req.ids) if req.ids else None
        filters = list(req.tag_filters)

        result: list[Volume] = []
        for fs in self._iter_lustre_filesystems():
            volume = self._fs_to_volume(fs, tenant_id=resolved)
            if wanted_ids is not None and volume.id not in wanted_ids:
                continue
            if not all(_tag_matches(volume.tags, f) for f in filters):
                continue
            result.append(volume)
        return ListVolumesResponse(volumes=tuple(result))

    # Volume lifecycle (create_volume / delete_volume) is intentionally NOT
    # implemented: the FSx CSI driver (fsx.csi.aws.com) owns it on EKS. The
    # methods fall back to the base raise (NotSupportedError), the manifest
    # declares volume.create / volume.delete ``none``, and the acceptance suite
    # falls back to inventorying via list_volumes.

    def _iter_lustre_filesystems(self) -> Iterator[dict[str, Any]]:
        """Yield each FSx Lustre filesystem dict, filtering out other file-system types."""
        try:
            paginator = self._fsx.get_paginator("describe_file_systems")
            for page in paginator.paginate():
                for fs in page.get("FileSystems", []):
                    if fs.get("FileSystemType") != "LUSTRE":
                        continue
                    yield fs
        except Exception as exc:
            raise _classify_client_error(exc, context="fsx:DescribeFileSystems") from exc

    def _fs_to_volume(self, fs: dict[str, Any], *, tenant_id: str) -> Volume:
        """Convert an FSx filesystem row to a Volume."""
        fs_id = fs.get("FileSystemId", "")
        tags = _tag_list_to_dict(fs.get("Tags"))
        capacity_gib = int(fs.get("StorageCapacity") or 0)
        lifecycle = str(fs.get("Lifecycle") or "").upper()
        state: VolumeState = _LIFECYCLE_TO_STATE.get(lifecycle, "failed")

        lustre = fs.get("LustreConfiguration") or {}
        deployment_type = lustre.get("DeploymentType") or self._deployment_type
        created_at = fs.get("CreationTime") or datetime.now(UTC)

        return Volume(
            tenant_id=tenant_id,
            id=fs_id,
            size_bytes=capacity_gib * GIB,
            created_at=created_at,
            type="file",
            state=state,
            name=tags.get("Name") or tags.get("CSIVolumeName") or fs_id,
            csi=CsiSpec(
                driver="fsx.csi.aws.com",
                volume_handle=fs_id,
                fs_type="lustre",
            ),
            tier=deployment_type,
            tags=tags,
            attributes={
                "region": self._region,
                "dns_name": str(fs.get("DNSName") or ""),
                "deployment_type": str(deployment_type),
                "cluster_env_tag": tags.get("env", ""),
                "lifecycle": lifecycle,
            },
        )


def _tag_matches(tags: Mapping[str, str], f: TagFilter) -> bool:
    """Return whether a tag filter matches filesystem tags."""
    if f.key not in tags:
        return False
    if not f.values:
        return True
    return tags[f.key] in f.values


def build_api(attributes: Mapping[str, str] | None = None) -> StorageProvider:
    """Entry point isvtest calls. Single hook the provider commits to.

    ``attributes`` is the provider's manifest ``attributes`` block (passed by the
    loader). Composes ``AwsFsxLustreApi`` into a served ``StorageProvider`` via
    ``new_implementation``: capabilities are detected from the overridden methods.
    """
    impl = AwsFsxLustreApi(attributes=attributes)
    return new_implementation(core=impl._core, impl=impl)
