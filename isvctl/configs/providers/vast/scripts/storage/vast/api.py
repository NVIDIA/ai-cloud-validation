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

"""VAST Data ``StorageProvider`` shim.

Calls the VAST VMS REST API. The shim subclasses ``Implementation`` and is
served through ``new_implementation()``: it implements only the surfaces it
backs, and the SDK *detects* which are supported. Which surfaces are *supported*
is also declared in the sibling ``config/overlays/storage-provider-manifest.yaml`` (the
contract); the validation suite probes each declared-supported surface at
runtime and fails if it raises ``NotSupportedError``. Surfaces this shim does not
back (volume lifecycle) are left undefined - detected as unimplemented and gated.
L2 semantics are declared via ``capability_qualifiers()``.

* ``health_check`` — authenticated GET ``/api/quotas/`` (validates credentials).
* ``list_tenants`` / ``get_tenant`` — the single configured tenant
  (``VAST_TENANT``, empty = VMS default).
* ``get_tenant_quota`` / ``list_tenant_quotas`` — aggregate ``hard_limit`` /
  ``used_effective_capacity`` across all directory quotas under
  ``VAST_STORAGE_PATH``.
* ``list_volumes`` — one ``Volume`` per directory quota under
  ``VAST_STORAGE_PATH``.  Volume lifecycle is owned by the VAST CSI driver
  (``csi.vastdata.com`` / ``scd.vastdata.com``); ``create_volume`` /
  ``delete_volume`` are advertised ``unimplemented`` and the acceptance suite
  falls back to inventorying existing CSI-provisioned volumes.

Quota model
-----------
The directory- and user-quota surfaces wrap the VMS quota endpoints:

* A **volume** is a VAST directory quota (a ``/api/quotas/`` row) that is a
  child of ``VAST_STORAGE_PATH``; ``volume.id`` is the VAST quota id.
* **Directory quotas** are the same ``/api/quotas/`` rows. Per the
  StorageProvider contract their ``path`` is **volume-relative**: the shim
  joins it under the volume's absolute export path (``_join_vol_path``) for VMS
  calls and returns volume-relative paths (``_rel_under``). Rows are keyed on
  the backend numeric ``id`` (``id_assignment=backend``).
  ``list_directory_quotas(volume_id)`` returns the quota tree rooted at the
  volume's path (the volume's own quota and any nested quotas); ``set`` PATCHes
  an existing row or POSTs a new one; ``delete`` removes it.
* **User quotas** attach to a volume's directory quota via VAST's
  ``quota_system_id`` (== the volume/quota id). The default-user slot
  (``user=None``) maps to the directory quota's ``default_user_quota``; per-user
  overrides map to ``/api/userquotas/`` rows. The backend infers the identifier
  kind from the value (numeric -> ``uid``, otherwise -> ``username``).

Tenant scoping
--------------
``tenant_id`` maps to the VAST tenant **name** sent as the
``X-Tenant-Name`` HTTP header.  The default is read from ``VAST_TENANT``
(empty string = omit the header and use the VMS default tenant).  To
validate multiple tenants, declare one provider entry per tenant in the
manifest.

Environment variables
---------------------
VAST_ENDPOINT             Required. VMS hostname or URL (e.g. ``vms.example.com``
                          or ``https://vms.example.com``).
VAST_TOKEN                API token (``Authorization: Api-Token <token>``).
                          Either ``VAST_TOKEN`` or ``VAST_USERNAME`` +
                          ``VAST_PASSWORD`` must be set.
VAST_USERNAME             VMS username for basic auth.
VAST_PASSWORD             VMS password for basic auth.
VAST_TENANT               Optional. VAST tenant name (``X-Tenant-Name`` header).
                          Empty = VMS default tenant.
VAST_STORAGE_PATH         Required. Root export path the shim is scoped to
                          (matches StorageClass ``root_export`` parameter,
                          e.g. ``/exports/k8s``). Quotas are filtered to this
                          path and its children.
VAST_VIP_POOL             Optional. VIP pool FQDN or name used to build the
                          NFS ``MountSpec.source`` for each volume.
VAST_INSECURE_SKIP_VERIFY Optional. Set to ``1`` / ``true`` to disable TLS
                          certificate verification (dev/test only).

See ``scripts/storage/README.md`` (sibling directory).
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
import warnings
from base64 import b64encode
from datetime import UTC, datetime
from typing import Any

from isvtest.core.storage_provider import (
    API_VERSION,
    AuthenticationError,
    ConflictError,
    CsiSpec,
    DeleteDirectoryQuotaRequest,
    DeleteUserQuotaRequest,
    DirectoryQuota,
    GetDirectoryQuotaRequest,
    GetTenantQuotaRequest,
    GetUserQuotaRequest,
    GetVolumeRequest,
    Implementation,
    ImplementationCapabilities,
    ListDirectoryQuotasRequest,
    ListDirectoryQuotasResponse,
    ListTenantQuotasRequest,
    ListTenantQuotasResponse,
    ListTenantsRequest,
    ListTenantsResponse,
    ListUserQuotasRequest,
    ListUserQuotasResponse,
    ListVolumesRequest,
    ListVolumesResponse,
    MountSpec,
    NotFoundError,
    ProviderProperties,
    QuotaLimits,
    QuotaUsage,
    SetDirectoryQuotaRequest,
    SetUserQuotaRequest,
    StorageApiError,
    StorageProvider,
    Tenant,
    TenantQuota,
    UserQuota,
    ValidationError,
    VersionMetadata,
    Volume,
    VolumeState,
    new_implementation,
)

# Maximum pages for paginated /api/quotas/ and /api/userquotas/ responses
# (defensive cap).
_MAX_PAGES = 1000

# Map VAST quota ``state`` field -> shim VolumeState.
# VAST states are "ok", "exceeded" (hard limit breached), and implementation-
# defined variants. Anything unrecognised falls through to "failed" so the
# validation catches it rather than silently passing.
_QUOTA_STATE_MAP: dict[str, VolumeState] = {
    "ok": "available",
    "exceeded": "available",  # still usable; hard limit enforced by VAST
    "softlimit": "available",
}

# HTTP status codes that indicate authentication / authorisation failures.
_AUTH_STATUS_CODES: frozenset[int] = frozenset({401, 403})

# Surfaces this shim backs (declared ``native`` in the manifest, probed by the
# acceptance suite):
#   * tenant.list/get/getQuota/listQuotas - the single configured tenant
#   * volume.list/get   - one Volume per directory quota (get via the SDK base)
#   * quota.directory.* - /api/quotas/ CRUD (list/get/set/delete)
#   * quota.user.*      - /api/userquotas/ + the directory quota default-user slot
# ``get_*`` surfaces ride their ``list_*`` sibling through the SDK base, so only
# list/set/delete are overridden where needed. Volume lifecycle (create/delete)
# is owned by the VAST CSI driver, so those methods are left unimplemented
# (absent) - the base raises NotSupportedError, the manifest declares them
# ``none``, and the acceptance suite falls back to inventorying existing
# CSI-provisioned volumes.


def _build_ssl_context(insecure: bool) -> ssl.SSLContext:
    """Build the TLS context for provider API calls."""
    ctx = ssl.create_default_context()
    if insecure:
        warnings.warn(
            "VAST_INSECURE_SKIP_VERIFY is enabled: TLS certificate verification is "
            "disabled and credentials may be exposed to MITM attacks. Dev/test only.",
            stacklevel=2,
        )
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _norm_path(path: str) -> str:
    """Normalise a VAST export path to a leading-slash, no-trailing-slash form."""
    return "/" + path.strip("/")


def _join_vol_path(root: str, rel: str) -> str:
    """Join a volume-relative directory-quota ``rel`` path under the volume ``root``.

    Directory-quota paths in the StorageProvider contract are volume-relative
    (``DirectoryQuota.path``); VAST addresses quotas by absolute VMS path, so
    the shim joins the two. ``rel`` empty addresses the volume's own directory.
    """
    rel = (rel or "").strip("/")
    base = _norm_path(root).rstrip("/")
    if not rel:
        return base or "/"
    return f"{base}/{rel}" if base else f"/{rel}"


def _rel_under(root: str, abs_path: str) -> str:
    """Return ``abs_path`` expressed relative to the volume ``root`` (``""`` for root)."""
    an = _norm_path(abs_path)
    rn = _norm_path(root)
    if an == rn:
        return ""
    prefix = rn.rstrip("/") + "/"
    return an[len(prefix) :] if an.startswith(prefix) else an.lstrip("/")


def _volume_matches_quota(q: dict[str, Any], volume_id: str) -> bool:
    """True when ``volume_id`` identifies quota ``q`` by VMS id, path, or CSI handle.

    Accepts the backend numeric id, the absolute VMS path, or a CSI
    ``volumeHandle`` that embeds the export path (the VAST CSI driver keys
    volumes on their export path), mirroring the WEKA shim's resolver.
    """
    if not volume_id:
        return False
    if str(q.get("id") or "") == volume_id:
        return True
    # Test the raw path before normalising: _norm_path("") becomes "/", which
    # would otherwise match almost any absolute volume_id / CSI handle.
    raw_path = (q.get("path") or "").strip("/")
    if not raw_path:
        return False
    qpath = _norm_path(raw_path)
    if qpath == _norm_path(volume_id):
        return True
    return volume_id.endswith(qpath) or qpath in volume_id


def _identifier_type(user: str) -> str:
    """Infer VAST ``identifier_type`` from a user value (numeric -> uid)."""
    return "uid" if user.isdigit() else "username"


class VastApi(Implementation):
    """``StorageProvider`` over the VAST VMS REST API.

    Single-tenant per instance: the shim is scoped to one VAST tenant + one
    ``storage_path`` prefix.  To validate multiple tenants or storage paths,
    declare separate provider entries in the manifest.
    """

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        tenant: str | None = None,
        storage_path: str | None = None,
        vip_pool: str | None = None,
        insecure_skip_verify: bool | None = None,
    ) -> None:
        """Initialize the object with its configured dependencies."""
        raw_endpoint = endpoint or os.environ.get("VAST_ENDPOINT", "")
        if not raw_endpoint:
            raise StorageApiError("VAST_ENDPOINT must be set (env var or constructor arg) for the VAST shim")
        self._base_url = self._normalise_endpoint(raw_endpoint)

        self._token = token or os.environ.get("VAST_TOKEN", "")
        self._username = username or os.environ.get("VAST_USERNAME", "")
        self._password = password or os.environ.get("VAST_PASSWORD", "")
        if not self._token and not self._username:
            raise StorageApiError("VAST_TOKEN or VAST_USERNAME + VAST_PASSWORD must be set for the VAST shim")
        if self._username and not self._password:
            raise StorageApiError("VAST_PASSWORD must be set when VAST_USERNAME is used for authentication")

        self._tenant = tenant if tenant is not None else os.environ.get("VAST_TENANT", "")

        raw_path = storage_path or os.environ.get("VAST_STORAGE_PATH", "")
        if not raw_path:
            raise StorageApiError("VAST_STORAGE_PATH must be set (env var or constructor arg) for the VAST shim")
        self._storage_path = _norm_path(raw_path)

        self._vip_pool = vip_pool or os.environ.get("VAST_VIP_POOL", "")

        if insecure_skip_verify is not None:
            skip = insecure_skip_verify
        else:
            val = os.environ.get("VAST_INSECURE_SKIP_VERIFY", "")
            skip = val.lower() in ("1", "true", "yes")
        self._ssl_ctx = _build_ssl_context(skip)

        # Identity + capability core (mirrors ProviderProperties). provider
        # version is "0.1.0" until a future revision queries VMS at construction;
        # it MUST stay in sync with the manifest's provider.version. The backend
        # version is an opaque vendor passthrough ("unknown" until queried).
        core = ProviderProperties(
            provider_namespace="vastdata.com",
            provider_id="vast-nfs",
            provider_metadata=VersionMetadata(
                vendor_name="NVIDIA",
                name="VAST NFS",
                version="0.1.0",
            ),
            sdk_version=API_VERSION,
            storage_type="file",
            storage_protocols=["nfsv4"],
            backend_metadata=VersionMetadata(
                vendor_name="VAST Data",
                vendor_docs="https://kb.vastdata.com/documentation/docs/overview-of-quotas-1",
                name="VAST",
                version="unknown",
            ),
            attributes={"storage_path": self._storage_path},
        )
        self._core = core

    # ------------------------------------------------------------------
    # Capability narrowing hooks
    # ------------------------------------------------------------------

    def capability_qualifiers(self, caps: ImplementationCapabilities) -> None:
        """L2 semantic facts for VAST (qualifier keys documented in api.py).

        VAST mints directory-quota ids (path is the natural key), accounts
        nested overlapping subjects against the most-restrictive cap, enforces
        inode limits, and exposes the fs-wide default-user slot. The shim is
        scoped to a single tenant. A fact set on a group inherits to its leaves.
        """
        caps.quota().directory().set_id_assignment("backend").set_accounting("nested").set_inodes(True)
        caps.quota().user().set_default_user_slot(True).set_inodes(True)
        caps.tenant().set_multi_tenant(False)

    # ------------------------------------------------------------------
    # StorageProvider: discovery / tenant / volume
    # ------------------------------------------------------------------

    def health_check(self) -> None:
        """Authenticated GET ``/api/quotas/`` — validates credentials."""
        self._request("GET", "/api/quotas/")

    def list_tenants(self, req: ListTenantsRequest) -> ListTenantsResponse:
        """Return the single tenant this shim is scoped to."""
        tenant = Tenant(id=self._tenant, name=self._tenant or "default")
        if not req.ids or self._tenant in req.ids:
            return ListTenantsResponse(tenants=(tenant,))
        return ListTenantsResponse(tenants=())

    def list_tenant_quotas(self, req: ListTenantQuotasRequest) -> ListTenantQuotasResponse:
        """Return the sole tenant's quota as a one-element list."""
        quota = self.get_tenant_quota(GetTenantQuotaRequest(tenant_id=req.tenant_id))
        return ListTenantQuotasResponse(tenant_quotas=(quota,))

    def get_tenant_quota(self, req: GetTenantQuotaRequest) -> TenantQuota:
        """Aggregate hard limit and used capacity for all quotas under ``storage_path``.

        * If a quota exists at exactly ``storage_path``, its ``hard_limit``
          is the tenant capacity ceiling and its ``used_effective_capacity``
          is the reported usage (covers the common single-root-quota layout).
        * Otherwise the ceiling and usage are the sum of child-quota hard
          limits / ``used_effective_capacity``.
        """
        resolved = self._resolve_tenant(req.tenant_id)
        quotas = self._list_quotas_all()
        storage_path = self._storage_path.rstrip("/")

        parent: dict[str, Any] | None = None
        children: list[dict[str, Any]] = []
        for q in quotas:
            path_norm = (q.get("path") or "").rstrip("/")
            if path_norm == storage_path:
                parent = q
            elif path_norm.startswith(storage_path + "/"):
                children.append(q)

        if parent is not None:
            hard_limit_bytes = int(parent.get("hard_limit") or 0)
            name = str(parent.get("name") or storage_path)
            used_bytes = int(parent.get("used_effective_capacity") or 0)
        else:
            hard_limit_bytes = sum(int(q.get("hard_limit") or 0) for q in children)
            name = f"VAST {storage_path}"
            used_bytes = sum(int(q.get("used_effective_capacity") or 0) for q in children)

        return TenantQuota(
            tenant_id=resolved,
            hard_limit_bytes=hard_limit_bytes,
            used_bytes=used_bytes,
            name=name,
        )

    def list_volumes(self, req: ListVolumesRequest) -> ListVolumesResponse:
        """Yield one ``Volume`` per directory quota that is a child of ``storage_path``."""
        resolved = self._resolve_tenant(req.tenant_id)
        wanted_ids = set(req.ids) if req.ids else None
        filters = list(req.tag_filters)

        result: list[Volume] = []
        for q in self._list_quotas_children():
            vol = self._quota_to_volume(q, tenant_id=resolved)
            if wanted_ids is not None and vol.id not in wanted_ids:
                continue
            if filters:
                # VAST quotas have no tag concept; tag filters never match.
                continue
            result.append(vol)
        return ListVolumesResponse(volumes=tuple(result))

    def get_volume(self, req: GetVolumeRequest) -> Volume:
        """Return one volume by VAST id, absolute path, or CSI volume handle."""
        resolved = self._resolve_tenant(req.tenant_id)
        q = self._volume_quota(req.volume_id)
        return self._quota_to_volume(q, tenant_id=resolved)

    # Volume lifecycle (create_volume / delete_volume) is intentionally NOT
    # implemented: the VAST CSI driver (csi.vastdata.com / scd.vastdata.com) owns
    # it. The methods fall back to the base raise (NotSupportedError) and the
    # manifest declares volume.create / volume.delete ``none``.

    # ------------------------------------------------------------------
    # StorageProvider: directory quotas
    # ------------------------------------------------------------------

    def list_directory_quotas(self, req: ListDirectoryQuotasRequest) -> ListDirectoryQuotasResponse:
        """Return the VAST quota tree rooted at ``req.volume_id``'s path (volume-relative)."""
        resolved = self._resolve_tenant(req.tenant_id)
        root = _norm_path(self._volume_quota(req.volume_id).get("path") or "")
        result = [
            self._quota_to_directory_quota(q, tenant_id=resolved, volume_id=req.volume_id, root=root)
            for q in self._list_quotas_all()
            if self._is_at_or_under(q.get("path") or "", root)
        ]
        return ListDirectoryQuotasResponse(directory_quotas=tuple(result))

    def get_directory_quota(self, req: GetDirectoryQuotaRequest) -> DirectoryQuota:
        """Lookup a directory quota by volume-relative ``path`` and/or VAST ``id``."""
        if req.path is None and req.id is None:
            raise ValidationError("get_directory_quota requires at least one of `path` or `id`")
        resolved = self._resolve_tenant(req.tenant_id)
        root = _norm_path(self._volume_quota(req.volume_id).get("path") or "")

        # VAST mints quota ids (id_assignment="backend"), so path is the natural
        # key. ``path`` is volume-relative and joined under the volume root.
        abs_path = _join_vol_path(root, req.path) if req.path is not None else None
        if abs_path is not None:
            q = self._get_quota_by_path(abs_path)
        else:
            # /api/quotas/<id>/ is cluster-wide: a row outside the volume root is
            # not this volume's quota (list_directory_quotas would not yield it).
            q = self._get_quota_by_id(req.id)  # type: ignore[arg-type]
            if q is not None and not self._is_at_or_under(q.get("path") or "", root):
                q = None
        if q is None:
            raise NotFoundError(
                f"directory quota not found (volume_id={req.volume_id!r}, path={req.path!r}, id={req.id!r})"
            )
        if req.id is not None and str(q.get("id")) != str(req.id):
            raise ConflictError(
                f"directory quota lookup mismatch: requested path={req.path!r}, "
                f"id={req.id!r}; found path={q.get('path')!r}, id={q.get('id')!r}"
            )
        if abs_path is not None and (q.get("path") or "").rstrip("/") != abs_path.rstrip("/"):
            raise ConflictError(
                f"directory quota lookup mismatch: requested path={req.path!r}, "
                f"id={req.id!r}; found path={q.get('path')!r}, id={q.get('id')!r}"
            )
        return self._quota_to_directory_quota(q, tenant_id=resolved, volume_id=req.volume_id, root=root)

    def set_directory_quota(self, req: SetDirectoryQuotaRequest) -> DirectoryQuota:
        """Upsert a directory-tree quota (``hard.bytes`` / ``hard.inodes``).

        ``quota.path`` is volume-relative (VAST assigns the quota id) and joined
        under the volume's export path. An existing row at that absolute path is
        PATCHed; otherwise a new ``/api/quotas/`` row is created.
        ``quota.hard=None`` clears both caps on an existing row.
        """
        quota = req.quota
        resolved = self._resolve_tenant(quota.tenant_id)
        if quota.path is None:
            raise ValidationError("set_directory_quota requires `quota.path` (VAST id_assignment='backend')")
        root = _norm_path(self._volume_quota(quota.volume_id).get("path") or "")
        abs_path = _join_vol_path(root, quota.path)

        body: dict[str, Any] = self._hard_to_body(quota.hard)
        existing = self._get_quota_by_path(abs_path)
        if existing is None:
            body["path"] = abs_path
            # VMS requires a ``name`` on create; default to the directory's leaf
            # (truncated to VMS's 64-char limit) when the caller supplies none.
            body["name"] = quota.attributes.get("name") or abs_path.rstrip("/").rsplit("/", 1)[-1][:64]
            stored = self._request("POST", "/api/quotas/", body=body)
        else:
            stored = self._request("PATCH", f"/api/quotas/{existing['id']}/", body=body)
        return self._quota_to_directory_quota(stored, tenant_id=resolved, volume_id=quota.volume_id, root=root)

    def delete_directory_quota(self, req: DeleteDirectoryQuotaRequest) -> None:
        """Remove a directory-tree quota (no-op if already absent)."""
        if req.path is None and req.id is None:
            raise ValidationError("delete_directory_quota requires at least one of `path` or `id`")
        self._resolve_tenant(req.tenant_id)
        root = _norm_path(self._volume_quota(req.volume_id).get("path") or "")

        if req.path is not None:
            q = self._get_quota_by_path(_join_vol_path(root, req.path))
        else:
            # A cluster-wide id hit outside the volume root belongs to another
            # volume; treat it as absent rather than deleting it.
            q = self._get_quota_by_id(req.id)  # type: ignore[arg-type]
            if q is not None and not self._is_at_or_under(q.get("path") or "", root):
                q = None
        if q is None:
            return
        if req.id is not None and str(q.get("id")) != str(req.id):
            raise ConflictError(
                f"directory quota lookup mismatch: requested path={req.path!r}, "
                f"id={req.id!r}; found path={q.get('path')!r}, id={q.get('id')!r}"
            )
        self._request("DELETE", f"/api/quotas/{q['id']}/", expect_json=False)

    # ------------------------------------------------------------------
    # StorageProvider: user quotas
    # ------------------------------------------------------------------

    def list_user_quotas(self, req: ListUserQuotasRequest) -> ListUserQuotasResponse:
        """Enumerate user quotas attached to ``req.volume_id`` (default slot + overrides)."""
        resolved = self._resolve_tenant(req.tenant_id)
        volume = self._get_quota_by_id(req.volume_id)
        if volume is None:
            raise NotFoundError(f"volume {req.volume_id!r} not found")

        result: list[UserQuota] = []
        default = volume.get("default_user_quota") or {}
        if default.get("hard_limit") is not None:
            result.append(
                UserQuota(
                    tenant_id=resolved,
                    volume_id=req.volume_id,
                    user=None,
                    hard=self._limits_from(default.get("hard_limit"), default.get("hard_limit_inodes")),
                )
            )
        for row in self._list_user_quota_rows(req.volume_id):
            entity = row.get("entity") or {}
            if entity.get("is_group"):
                continue
            result.append(
                UserQuota(
                    tenant_id=resolved,
                    volume_id=req.volume_id,
                    user=str(entity.get("identifier")),
                    hard=self._limits_from(row.get("hard_limit"), row.get("hard_limit_inodes")),
                    usage=QuotaUsage(
                        bytes=int(row.get("used_capacity") or 0),
                        inodes=int(row["used_inodes"]) if row.get("used_inodes") is not None else None,
                    ),
                )
            )
        return ListUserQuotasResponse(user_quotas=tuple(result))

    def set_user_quota(self, req: SetUserQuotaRequest) -> UserQuota:
        """Upsert a per-user (or default-user slot) quota on a volume.

        ``quota.user=None`` enables user quotas on the directory quota and sets
        its ``default_user_quota.hard_limit``. A non-None user POSTs an override
        to ``/api/userquotas/`` (enabling user quotas on the parent first).
        """
        quota = req.quota
        resolved = self._resolve_tenant(quota.tenant_id)
        volume_id_int = self._volume_id_int(quota.volume_id)
        hard_bytes = quota.hard.bytes if quota.hard else None
        hard_inodes = quota.hard.inodes if quota.hard else None

        if quota.user is None:
            if hard_bytes is None:
                raise ValidationError("set_user_quota for the default-user slot requires hard.bytes")
            self._request(
                "PATCH",
                f"/api/quotas/{volume_id_int}/",
                body={
                    "is_user_quota": True,
                    "default_user_quota": {
                        "hard_limit": hard_bytes,
                        "hard_limit_inodes": hard_inodes,
                    },
                },
            )
        else:
            self._ensure_user_quota_enabled(volume_id_int)
            self._request(
                "POST",
                "/api/userquotas/",
                body={
                    "quota_id": volume_id_int,
                    "identifier": quota.user,
                    "identifier_type": _identifier_type(quota.user),
                    "hard_limit": hard_bytes or 0,
                    "hard_limit_inodes": hard_inodes,
                    "soft_limit": 0,
                    "is_group": False,
                },
                expect_json=False,
            )
        return self.get_user_quota(GetUserQuotaRequest(tenant_id=resolved, volume_id=quota.volume_id, user=quota.user))

    def delete_user_quota(self, req: DeleteUserQuotaRequest) -> None:
        """Remove a user quota record (no-op if already absent)."""
        self._resolve_tenant(req.tenant_id)
        volume_id_int = self._volume_id_int(req.volume_id)

        if req.user is None:
            # Clear the fs-wide default-user slot.
            self._request(
                "PATCH",
                f"/api/quotas/{volume_id_int}/",
                body={"default_user_quota": {"hard_limit": None, "hard_limit_inodes": None}},
            )
            return
        for row in self._list_user_quota_rows(req.volume_id):
            entity = row.get("entity") or {}
            if entity.get("is_group"):
                continue
            if str(entity.get("identifier")) == req.user:
                self._request("DELETE", f"/api/userquotas/{row['id']}/", expect_json=False)
                return

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_endpoint(endpoint: str) -> str:
        """Normalize and validate the VAST API endpoint URL."""
        endpoint = endpoint.strip()
        if endpoint.startswith("http://"):
            raise StorageApiError("VAST_ENDPOINT must use https://")
        if endpoint.startswith("https://"):
            return endpoint.rstrip("/")
        return "https://" + endpoint.rstrip("/")

    def _resolve_tenant(self, tenant_id: str | None) -> str:
        """Resolve and validate the request tenant for this shim."""
        resolved = tenant_id if tenant_id is not None else self._tenant
        if tenant_id is not None and tenant_id != self._tenant:
            raise StorageApiError(
                f"tenant_id={tenant_id!r} does not match this shim's configured tenant "
                f"{self._tenant!r}; declare a separate provider entry per tenant"
            )
        return resolved

    @staticmethod
    def _volume_id_int(volume_id: str) -> int:
        """Parse a VAST volume identifier as an integer quota ID."""
        try:
            return int(volume_id)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"volume_id {volume_id!r} is not a VAST quota id") from exc

    def _auth_headers(self) -> dict[str, str]:
        """Build authentication and tenant headers for VAST requests."""
        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Api-Token {self._token}"
        else:
            creds = b64encode(f"{self._username}:{self._password}".encode()).decode()
            headers["Authorization"] = f"Basic {creds}"
        if self._tenant:
            headers["X-Tenant-Name"] = self._tenant
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        not_found_ok: bool = False,
        expect_json: bool = True,
    ) -> Any:
        """Send an authenticated provider API request."""
        url = self._base_url + path
        data = json.dumps(body).encode() if body is not None else None
        headers = {**self._auth_headers(), "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=30) as resp:
                raw = resp.read()
                if not expect_json:
                    return None
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            if not_found_ok and exc.code == 404:
                return None
            if exc.code in _AUTH_STATUS_CODES:
                raise AuthenticationError(f"VAST VMS {method} {path}: HTTP {exc.code} {exc.reason}") from exc
            raise StorageApiError(f"VAST VMS {method} {path}: HTTP {exc.code} {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise StorageApiError(f"VAST VMS {method} {path}: {exc.reason}") from exc

    def _list_quotas_all(self) -> list[dict[str, Any]]:
        """Fetch all quotas from ``/api/quotas/``, handling pagination."""
        return self._paginate("/api/quotas/")

    def _paginate(self, start_path: str) -> list[dict[str, Any]]:
        """Walk a Django-REST-style paginated collection (``results`` + ``next``)."""
        url = start_path
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _ in range(_MAX_PAGES):
            data = self._request("GET", url)
            if isinstance(data, list):
                items.extend(data)
                break
            if isinstance(data, dict) and "results" in data:
                items.extend(data["results"])
                next_url = data.get("next") or ""
                if not next_url:
                    break
                next_path = self._extract_path(next_url)
                if not next_path or next_path in seen:
                    break
                seen.add(next_path)
                url = next_path
            else:
                break
        return items

    def _list_quotas_children(self) -> list[dict[str, Any]]:
        """Return quotas that are strict children of ``storage_path`` (not the root itself)."""
        storage_path = self._storage_path.rstrip("/")
        return [q for q in self._list_quotas_all() if (q.get("path") or "").rstrip("/").startswith(storage_path + "/")]

    def _list_user_quota_rows(self, volume_id: str) -> list[dict[str, Any]]:
        """Fetch ``/api/userquotas/?quota_system_id=<id>`` rows, handling pagination."""
        quota_system_id = self._volume_id_int(volume_id)
        return self._paginate(f"/api/userquotas/?quota_system_id={quota_system_id}")

    def _get_quota_by_id(self, quota_id: str) -> dict[str, Any] | None:
        """GET ``/api/quotas/<id>/``; returns None when the id is unknown (404)."""
        quota_id_int = self._volume_id_int(quota_id)
        return self._request("GET", f"/api/quotas/{quota_id_int}/", not_found_ok=True)

    def _get_quota_by_path(self, path: str) -> dict[str, Any] | None:
        """GET ``/api/quotas/?path=<path>`` and return the exact-path match, else None."""
        target = _norm_path(path)
        query = "/api/quotas/?path=" + urllib.parse.quote(target, safe="")
        data = self._request("GET", query)
        rows = data.get("results", []) if isinstance(data, dict) else (data or [])
        for q in rows:
            if (q.get("path") or "").rstrip("/") == target.rstrip("/"):
                return q
        return None

    def _volume_quota(self, volume_id: str) -> dict[str, Any]:
        """Resolve the directory quota backing ``volume_id`` (VMS id, path, or CSI handle).

        Accepts the numeric VAST quota id, an absolute export path, or the PV's
        CSI ``volumeHandle``. The VAST CSI driver names each volume's directory
        after its handle under the root export (``<storage_path>/<handle>``), so
        a non-path, non-numeric id is resolved by that convention. Raises
        ``NotFoundError`` when nothing matches.
        """
        if volume_id and volume_id.isdigit():
            q = self._get_quota_by_id(volume_id)
            if q is not None:
                return q
        if volume_id.startswith("/"):
            q = self._get_quota_by_path(volume_id)
            if q is not None:
                return q
        elif volume_id:
            # VAST CSI volume handle -> leaf directory under the root export.
            q = self._get_quota_by_path(_join_vol_path(self._storage_path, volume_id))
            if q is not None:
                return q
        for q in self._list_quotas_all():
            if _volume_matches_quota(q, volume_id):
                return q
        raise NotFoundError(f"volume {volume_id!r} not found")

    def _ensure_user_quota_enabled(self, volume_id_int: int) -> None:
        """Enable user quotas on the directory quota if not already enabled."""
        quota = self._request("GET", f"/api/quotas/{volume_id_int}/", not_found_ok=True)
        if quota is None:
            raise NotFoundError(f"volume {volume_id_int!r} not found")
        if not quota.get("is_user_quota"):
            self._request("PATCH", f"/api/quotas/{volume_id_int}/", body={"is_user_quota": True})

    @staticmethod
    def _is_at_or_under(path: str, root: str) -> bool:
        """Return whether a path is at or below a root path."""
        path_norm = path.rstrip("/")
        root_norm = root.rstrip("/")
        return path_norm == root_norm or path_norm.startswith(root_norm + "/")

    @staticmethod
    def _hard_to_body(hard: QuotaLimits | None) -> dict[str, Any]:
        """Translate ``QuotaLimits`` to the VAST quota PATCH/POST body.

        ``hard=None`` clears both caps; a per-dimension ``None`` clears that
        dimension (VAST treats null / 0 as unlimited).
        """
        if hard is None:
            return {"hard_limit": None, "hard_limit_inodes": None}
        return {"hard_limit": hard.bytes, "hard_limit_inodes": hard.inodes}

    @staticmethod
    def _limits_from(hard_limit: Any, hard_limit_inodes: Any) -> QuotaLimits | None:
        """Convert VAST hard-limit values to QuotaLimits."""
        bytes_value = int(hard_limit) if hard_limit else None
        inodes_value = int(hard_limit_inodes) if hard_limit_inodes else None
        if bytes_value is None and inodes_value is None:
            return None
        return QuotaLimits(bytes=bytes_value, inodes=inodes_value)

    def _quota_to_directory_quota(
        self, q: dict[str, Any], *, tenant_id: str, volume_id: str, root: str
    ) -> DirectoryQuota:
        """Convert a VAST quota row to a DirectoryQuota."""
        usage = QuotaUsage(
            bytes=int(q.get("used_effective_capacity") or q.get("used_capacity") or 0),
            inodes=int(q["used_inodes"]) if q.get("used_inodes") is not None else None,
        )
        return DirectoryQuota(
            tenant_id=tenant_id,
            volume_id=volume_id,
            path=_rel_under(root, q.get("path") or ""),
            id=str(q.get("id")),
            hard=self._limits_from(q.get("hard_limit"), q.get("hard_limit_inodes")),
            usage=usage,
        )

    def _quota_to_volume(self, q: dict[str, Any], *, tenant_id: str) -> Volume:
        """Convert a VAST quota row to a Volume."""
        quota_id = str(q.get("id", ""))
        path = str(q.get("path") or "")
        name = str(q.get("name") or path)
        hard_limit = int(q.get("hard_limit") or 0)
        used_eff = int(q.get("used_effective_capacity") or 0)
        state_raw = str(q.get("state") or "").lower()
        state: VolumeState = _QUOTA_STATE_MAP.get(state_raw, "failed")

        csi = CsiSpec(
            driver="scd.vastdata.com",
            volume_handle=path,
            fs_type="nfs",
        )

        mount: MountSpec | None = None
        if self._vip_pool:
            mount = MountSpec(
                fs_type="nfs",
                source=f"{self._vip_pool}:{path}",
                options="vers=4.1",
            )

        return Volume(
            tenant_id=tenant_id,
            id=quota_id,
            size_bytes=hard_limit,
            created_at=datetime.now(UTC),
            type="file",
            state=state,
            name=name,
            mount=mount,
            csi=csi,
            used_bytes=used_eff,
            available_bytes=max(0, hard_limit - used_eff),
            attributes={
                "path": path,
                "storage_path": self._storage_path,
                "vip_pool": self._vip_pool,
            },
        )

    def _extract_path(self, url: str) -> str:
        """Extract the path+query portion from an absolute URL returned by the paginator."""
        url = url.strip()
        if url.startswith("http://") or url.startswith("https://"):
            parsed = urllib.parse.urlparse(url)
            path = parsed.path
            if parsed.query:
                path = path + "?" + parsed.query
            return path
        return url


def build_api() -> StorageProvider:
    """Entry point isvtest calls. Single hook the provider commits to.

    Composes ``VastApi`` into a served ``StorageProvider`` via
    ``new_implementation``: capabilities are detected from the overridden
    methods. VAST resolves its own default tenant internally, so no
    ``default_tenant`` wrapping is applied here.
    """
    impl = VastApi()
    return new_implementation(core=impl._core, impl=impl)
