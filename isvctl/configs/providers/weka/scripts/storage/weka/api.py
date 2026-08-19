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

"""WEKA ``StorageProvider`` shim.

Calls the WEKA cluster REST API (``/api/v2``). The shim subclasses
``Implementation`` and is served through ``new_implementation()``: it implements
only the surfaces it backs, and the SDK *detects* which are supported. Which
surfaces are *supported* is also declared in the sibling
``config/storage-provider-manifest.yaml`` (the contract); the validation suite
probes each declared-supported surface at runtime and fails if it raises
``NotSupportedError``.

* ``health_check`` — authenticated GET ``/fileSystems`` (validates credentials).
* ``list_tenants`` / ``get_tenant`` — the single configured organization.
* ``get_tenant_quota`` / ``list_tenant_quotas`` — filesystem ``total_budget`` /
  ``used_total`` for ``WEKA_FILESYSTEM``, or directory-quota aggregation when
  ``WEKA_STORAGE_PATH`` is set.
* ``list_volumes`` / ``get_volume`` — one ``Volume`` per WEKA filesystem,
  using the CSI handle ``weka/v2/<filesystem>``. Volume lifecycle is owned by
  the driver; ``create_volume`` / ``delete_volume`` are left to the base method
  (which raises ``NotSupportedError``) and the manifest declares them ``none``.
* ``{list,get,set,delete}_directory_quota`` — directory-tree quotas via the
  documented REST API (``resolvePath`` + ``PUT`` / ``PATCH`` / ``DELETE`` on
  ``/fileSystems/<uid>/quota/<inode>``). ``id_assignment`` is ``backend`` (WEKA
  mints the ``quota_id``), so ``set`` requires a volume-relative ``path``.
  Creating a quota on a *non-empty* directory over REST needs a Data Services
  container on the backend; without one WEKA restricts ``PUT`` to empty
  directories (``PATCH`` / ``DELETE`` on an existing quota are unaffected). The
  shim surfaces that backend rule verbatim.
* ``{list,get,set,delete}_user_quota`` — per-UID quotas via
  ``/fileSystems/<uid>/quota/user``, the REST surface WEKA introduced in
  **v5.1.26**. Arguments travel as query values rather than a JSON body, and
  ``GET`` paginates via ``next_token``. Older clusters answer the route with a
  404 naming the route, which the shim reports as ``NotSupportedError`` (a
  capability gap) rather than ``NotFoundError`` (a missing quota).

WEKA addresses one UID per call and has no filesystem-wide default-user slot, so
``defaultUserSlot`` is advertised ``false`` and a request with ``user=None`` is
refused with ``NotSupportedError``. A hard limit of ``0`` means unlimited on the
backend and is surfaced as an absent limit, not a zero-byte allowance. User
quotas are accepted only for filesystem-backed (``weka/v2``) volumes: WEKA scopes
them to a whole filesystem, so applying one to a directory-backed volume would
also hit every other PVC sharing that filesystem.

Two backend prerequisites apply before the limits take effect: filesystems
created before WEKA 5.1.20 need a one-time ``weka fs quota enable-users`` (which
requires a Data Services container), and enabling user quotas on an existing
filesystem starts a background ``QUOTA_COLORING`` pass that stamps existing files
with their UID — quotas are not enforced on pre-existing data until it finishes.

Tenant scoping
--------------
``tenant_id`` maps to the WEKA organization name (``WEKA_ORGANIZATION``,
default ``Root``). On clusters without multi-tenancy this is the sole
isolation boundary exposed to the shim. To validate multiple tenants, declare
one provider entry per organization in the manifest.

Environment variables
---------------------
WEKA_ENDPOINTS             Comma-separated ``host:port`` list (preferred;
                          matches the CSI secret ``endpoints`` field).
WEKA_ENDPOINT              Single ``host:port`` fallback when
                          ``WEKA_ENDPOINTS`` is unset.
WEKA_SCHEME                URL scheme (default ``https``).
WEKA_USERNAME              Required. Cluster username. All surfaces work with
                          an account with quota-management permissions.
WEKA_PASSWORD              Required. Cluster password.
WEKA_ORGANIZATION          Optional. Organization / tenant name for login
                          (default ``Root``).
WEKA_FILESYSTEM            Required. Filesystem used for tenant quota
                          (matches StorageClass ``filesystemName``, e.g.
                          ``test_fs``). Volume directory quotas use the
                          filesystem named by their ``weka/v2`` handle.
WEKA_STORAGE_PATH          Optional. Directory path prefix within the
                          filesystem for tenant-quota aggregation.
WEKA_INSECURE_SKIP_VERIFY  Optional. ``1`` / ``true`` to disable TLS
                          certificate verification (dev/test only).

See ``scripts/storage/README.md`` (sibling directory).
"""

from __future__ import annotations

import contextlib
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
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
    NotFoundError,
    NotSupportedError,
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

# Maximum pages for the paginated /fileSystems/<uid>/quota response (defensive
# cap mirroring the VAST shim).
_MAX_PAGES = 1000

# HTTP status codes that indicate authentication / authorisation failures.
_AUTH_STATUS_CODES: frozenset[int] = frozenset({401, 403})

# Map WEKA filesystem ``status`` -> shim VolumeState (``weka/v2`` volumes).
_FS_STATE_MAP: dict[str, VolumeState] = {
    "ready": "available",
    "creating": "creating",
    "removing": "deleting",
}

# CSI volume handle for a filesystem-per-PVC volume: ``weka/v2/<filesystem>``.
_FS_HANDLE_PREFIX = "weka/v2/"

# WEKA emits directory-quota ids as ``DIR:0x…:0``; a volume named by one is
# directory-backed and shares its filesystem with other PVCs.
_DIR_V1_ID_PREFIX = "DIR:"

# First WEKA release carrying /fileSystems/<uid>/quota/user. Named in the error
# so an operator on an older cluster sees the upgrade path.
_MIN_USER_QUOTA_RELEASE = "5.1.26"


def _fs_name_from_volume_id(volume_id: str) -> str:
    """Return the filesystem name from a ``weka/v2`` handle or bare name."""
    if not volume_id:
        return ""
    if volume_id.startswith(_FS_HANDLE_PREFIX):
        return volume_id[len(_FS_HANDLE_PREFIX) :].strip("/")
    return "" if "/" in volume_id else volume_id


def _build_ssl_context(insecure: bool) -> ssl.SSLContext:
    """Build the TLS context for provider API calls."""
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class WekaApi(Implementation):
    """``StorageProvider`` over the WEKA cluster REST API.

    Single-tenant per instance: the shim is scoped to one WEKA organization +
    one filesystem (optionally narrowed to a ``storage_path`` prefix). To
    validate multiple tenants or filesystems, declare separate provider entries
    in the manifest.
    """

    def __init__(
        self,
        *,
        endpoints: str | None = None,
        endpoint: str | None = None,
        scheme: str | None = None,
        username: str | None = None,
        password: str | None = None,
        organization: str | None = None,
        filesystem: str | None = None,
        storage_path: str | None = None,
        insecure_skip_verify: bool | None = None,
    ) -> None:
        """Initialize the object with its configured dependencies."""
        raw_endpoints = endpoints or os.environ.get("WEKA_ENDPOINTS", "")
        if not raw_endpoints:
            raw_endpoints = endpoint or os.environ.get("WEKA_ENDPOINT", "")
        if not raw_endpoints:
            raise StorageApiError("WEKA_ENDPOINTS or WEKA_ENDPOINT must be set for the WEKA shim")
        self._endpoint_hosts = [h.strip() for h in raw_endpoints.split(",") if h.strip()]
        self._scheme = (scheme or os.environ.get("WEKA_SCHEME", "https")).rstrip(":/")

        self._username = username or os.environ.get("WEKA_USERNAME", "")
        self._password = password or os.environ.get("WEKA_PASSWORD", "")
        if not self._username or not self._password:
            raise StorageApiError("WEKA_USERNAME and WEKA_PASSWORD must be set for the WEKA shim")

        self._organization = organization if organization is not None else os.environ.get("WEKA_ORGANIZATION", "Root")

        self._filesystem = filesystem or os.environ.get("WEKA_FILESYSTEM", "")
        if not self._filesystem:
            raise StorageApiError("WEKA_FILESYSTEM must be set (env var or constructor arg) for the WEKA shim")

        raw_path = storage_path if storage_path is not None else os.environ.get("WEKA_STORAGE_PATH", "")
        self._storage_path = ("/" + raw_path.strip("/")) if raw_path else ""

        if insecure_skip_verify is not None:
            skip = insecure_skip_verify
        else:
            val = os.environ.get("WEKA_INSECURE_SKIP_VERIFY", "")
            skip = val.lower() in ("1", "true", "yes")
        self._ssl_ctx = _build_ssl_context(skip)

        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expires_at: float = 0.0
        self._active_base_url: str | None = None

        # Identity + capability core (mirrors ProviderProperties). provider
        # version is "0.1.0" (the shim's own version, semver) and MUST stay in
        # sync with the manifest's provider.version. The backend version is an
        # opaque vendor passthrough ("unknown" - the live cluster build is not
        # surfaced by the REST API today).
        core = ProviderProperties(
            provider_namespace="weka.io",
            provider_id="shared-fs",
            provider_metadata=VersionMetadata(
                vendor_name="NVIDIA",
                name="WEKA shared filesystem",
                version="0.1.0",
            ),
            sdk_version=API_VERSION,
            storage_type="file",
            storage_protocols=["wekafs"],
            backend_metadata=VersionMetadata(
                vendor_name="WekaIO",
                vendor_docs="https://docs.weka.io/weka-filesystems-and-object-stores/quota-management",
                name="WEKA",
                version="unknown",
            ),
            attributes={
                "filesystem": self._filesystem,
                "storage_path": self._storage_path,
            },
        )
        self._core = core

    # ------------------------------------------------------------------
    # StorageProvider: discovery / tenant / volume
    # ------------------------------------------------------------------

    def health_check(self) -> None:
        """Authenticated GET ``/fileSystems`` — validates credentials."""
        self._request("GET", "/api/v2/fileSystems")

    def get_tenant_quota(self, req: GetTenantQuotaRequest) -> TenantQuota:
        """Return overall storage utilization for the configured organization.

        With ``WEKA_STORAGE_PATH`` unset, the target filesystem's
        ``total_budget`` / ``used_total`` is the tenant ceiling. When set, the
        ceiling and usage are aggregated from the directory quotas rooted at
        that path (VAST-style: parent hard limit or sum of children).
        """
        resolved = self._resolve_tenant(req.tenant_id)
        if self._storage_path:
            return self._tenant_quota_from_directory_quotas(resolved)
        return self._tenant_quota_from_filesystem(resolved)

    def list_volumes(self, req: ListVolumesRequest) -> ListVolumesResponse:
        """Return one ``weka/v2`` volume per filesystem in the organization."""
        resolved = self._resolve_tenant(req.tenant_id)
        wanted_ids = set(req.ids) if req.ids else None
        filters = list(req.tag_filters)

        result: list[Volume] = []
        for fs in self._filesystems():
            vol = self._filesystem_to_volume(fs, tenant_id=resolved)
            if wanted_ids is not None and vol.id not in wanted_ids:
                continue
            if filters:
                # WEKA filesystems have no tag concept; tag filters never match.
                continue
            result.append(vol)
        return ListVolumesResponse(volumes=tuple(result))

    def get_volume(self, req: GetVolumeRequest) -> Volume:
        """Return a filesystem volume by ``weka/v2`` handle or bare name."""
        resolved = self._resolve_tenant(req.tenant_id)
        fs = self._filesystem_for_volume(req.volume_id)
        if fs is None:
            raise NotFoundError(f"volume {req.volume_id!r} not found")
        return self._filesystem_to_volume(fs, tenant_id=resolved)

    # Volume lifecycle (create_volume / delete_volume) is intentionally NOT
    # implemented: the WEKA CSI driver (csi.weka.io) owns it. The methods fall
    # back to the base raise (NotSupportedError) and the manifest declares
    # volume.create / volume.delete ``none``.

    def capability_qualifiers(self, caps: ImplementationCapabilities) -> None:
        """Refine the composed capabilities with WEKA's semantic facts."""
        # WEKA quotas are byte-only (no inode enforcement); directory-quota ids
        # are backend-minted (``quota_id``), so ``set`` needs a path; and the
        # shim is scoped to a single organization.
        caps.quota().set_inodes(False)
        caps.quota().directory().set_id_assignment("backend")
        # WEKA's user quota API addresses one uid per call, with no
        # filesystem-wide default-user slot.
        caps.quota().user().set_default_user_slot(False)
        caps.tenant().set_multi_tenant(False)

    # ------------------------------------------------------------------
    # StorageProvider: tenant enumeration
    # ------------------------------------------------------------------

    def list_tenants(self, req: ListTenantsRequest) -> ListTenantsResponse:
        """Return the single organization this shim is scoped to."""
        tenant = Tenant(id=self._organization, name=self._organization or "Root")
        if not req.ids or self._organization in req.ids:
            return ListTenantsResponse(tenants=(tenant,))
        return ListTenantsResponse(tenants=())

    def list_tenant_quotas(self, req: ListTenantQuotasRequest) -> ListTenantQuotasResponse:
        """Return the sole organization's quota as a one-element list."""
        quota = self.get_tenant_quota(GetTenantQuotaRequest(tenant_id=req.tenant_id))
        return ListTenantQuotasResponse(tenant_quotas=(quota,))

    # ------------------------------------------------------------------
    # StorageProvider: directory quotas (documented REST API)
    # ------------------------------------------------------------------

    def list_directory_quotas(self, req: ListDirectoryQuotasRequest) -> ListDirectoryQuotasResponse:
        """Return directory quotas below the volume's filesystem root."""
        tenant_id = self._resolve_tenant(req.tenant_id)
        fs_uid = self._filesystem_uid_for_volume(req.volume_id)
        out: list[DirectoryQuota] = []
        for q in self._list_directory_quotas(fs_uid=fs_uid):
            q_path = _norm_path(q.get("path") or "")
            if not q_path:
                continue
            out.append(self._directory_quota(q, tenant_id, req.volume_id, q_path.lstrip("/")))
        return ListDirectoryQuotasResponse(directory_quotas=tuple(out))

    def set_directory_quota(self, req: SetDirectoryQuotaRequest) -> DirectoryQuota:
        """Create or update a directory-tree quota under the volume."""
        quota = req.quota
        if quota.path is None:
            raise ValidationError("directory quota path is required (id_assignment=backend)")
        tenant_id = self._resolve_tenant(quota.tenant_id)
        fs_uid = self._filesystem_uid_for_volume(quota.volume_id)
        abs_path = _norm_path(quota.path)
        if not abs_path:
            # Root path is the CSI capacity quota on the volume itself.
            raise ValidationError(f"directory quota path must name a subdirectory of the volume, got {quota.path!r}")
        hard = _hard_bytes(quota.hard)

        existing = self._find_directory_quota_by_path(fs_uid, abs_path)
        if existing is not None:
            stored = self._patch_directory_quota(fs_uid, _quota_inode_id(existing), hard)
        else:
            inode = self._resolve_inode(fs_uid, abs_path)
            stored = self._put_directory_quota(fs_uid, inode, hard)
        if not stored.get("path"):
            stored["path"] = abs_path
        return self._directory_quota(
            stored, tenant_id, quota.volume_id, _norm_path(stored.get("path") or "").lstrip("/")
        )

    def delete_directory_quota(self, req: DeleteDirectoryQuotaRequest) -> None:
        """Remove a directory-tree quota (idempotent — missing is a no-op)."""
        if req.path is None and req.id is None:
            raise ValidationError("delete_directory_quota requires at least one of path or id")
        if req.path is not None and not _norm_path(req.path):
            raise ValidationError("directory quota path must name a subdirectory of the volume")
        self._resolve_tenant(req.tenant_id)
        fs_uid = self._filesystem_uid_for_volume(req.volume_id)

        target: dict[str, Any] | None = None
        if req.path is not None:
            target = self._find_directory_quota_by_path(fs_uid, _norm_path(req.path))
        else:
            for q in self._list_directory_quotas(fs_uid=fs_uid):
                if _quota_id(q) != req.id:
                    continue
                if not _norm_path(q.get("path") or ""):
                    continue
                target = q
                break
        if target is None:
            return
        if req.path is not None and req.id is not None and _quota_id(target) != req.id:
            raise ConflictError(
                f"directory quota key mismatch: path resolved to id {_quota_id(target)!r}, requested id {req.id!r}"
            )
        self._delete_directory_quota(fs_uid, _quota_inode_id(target))

    # ------------------------------------------------------------------
    # StorageProvider: user quotas (WEKA 5.1.26+ REST API)
    # ------------------------------------------------------------------

    def list_user_quotas(self, req: ListUserQuotasRequest) -> ListUserQuotasResponse:
        """Enumerate the per-UID quotas on the volume's filesystem."""
        tenant_id = self._resolve_tenant(req.tenant_id)
        fs_uid = self._filesystem_uid_for_user_quota(req.volume_id)
        rows = self._list_user_quota_rows(fs_uid)
        return ListUserQuotasResponse(
            user_quotas=tuple(self._user_quota(row, tenant_id, req.volume_id) for row in rows)
        )

    def get_user_quota(self, req: GetUserQuotaRequest) -> UserQuota:
        """Look up one UID's quota on the volume's filesystem."""
        if req.user is None:
            raise _default_user_slot_unsupported()
        tenant_id = self._resolve_tenant(req.tenant_id)
        fs_uid = self._filesystem_uid_for_user_quota(req.volume_id)
        uid = _parse_user_uid(req.user)
        # WEKA exposes no per-uid read; the list endpoint is the only source.
        for row in self._list_user_quota_rows(fs_uid):
            if _row_uid(row) == uid:
                return self._user_quota(row, tenant_id, req.volume_id)
        raise NotFoundError(f"user quota for uid {uid} not found on volume {req.volume_id!r}")

    def set_user_quota(self, req: SetUserQuotaRequest) -> UserQuota:
        """Upsert one UID's hard byte limit on the volume's filesystem."""
        quota = req.quota
        if quota.user is None:
            raise _default_user_slot_unsupported()
        resolved = self._resolve_tenant(quota.tenant_id)
        fs_uid = self._filesystem_uid_for_user_quota(quota.volume_id)
        uid = _parse_user_uid(quota.user)
        # Both limits are sent unconditionally: WEKA reads 0 as unlimited, so an
        # explicit zero is how a limit gets cleared.
        self._user_quota_request(
            "POST",
            fs_uid,
            params={
                "user_id": str(uid),
                "hard_limit_bytes": str(max(_hard_bytes(quota.hard), 0)),
                "soft_limit_bytes": "0",
            },
        )
        return self.get_user_quota(GetUserQuotaRequest(tenant_id=resolved, volume_id=quota.volume_id, user=quota.user))

    def delete_user_quota(self, req: DeleteUserQuotaRequest) -> None:
        """Remove one UID's quota (no-op if already absent)."""
        if req.user is None:
            raise _default_user_slot_unsupported()
        self._resolve_tenant(req.tenant_id)
        fs_uid = self._filesystem_uid_for_user_quota(req.volume_id)
        uid = _parse_user_uid(req.user)
        try:
            self._user_quota_request("DELETE", fs_uid, params={"user_id": str(uid)})
        except NotFoundError:
            return

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_tenant(self, tenant_id: str | None) -> str:
        """Resolve and validate the request tenant for this shim."""
        resolved = tenant_id if tenant_id is not None else self._organization
        if not resolved:
            raise StorageApiError("tenant_id is required (no default organization configured)")
        if tenant_id is not None and tenant_id != self._organization:
            raise NotFoundError(
                f"tenant_id={tenant_id!r} does not match this shim's configured organization "
                f"{self._organization!r}; declare a separate provider entry per tenant"
            )
        return resolved

    def _base_urls(self) -> list[str]:
        """Return candidate WEKA API base URLs."""
        return [f"{self._scheme}://{host.rstrip('/')}" for host in self._endpoint_hosts]

    def _ensure_token(self) -> None:
        """Ensure a usable access token is available."""
        if self._access_token and time.monotonic() < self._token_expires_at - 30:
            return
        if self._refresh_token:
            try:
                self._refresh_access_token()
                return
            except AuthenticationError:
                pass
        self._login()

    def _login(self) -> None:
        """Authenticate to WEKA and store returned tokens."""
        body: dict[str, str] = {
            "username": self._username,
            "password": self._password,
        }
        if self._organization:
            body["org"] = self._organization

        last_error: Exception | None = None
        for base_url in self._base_urls():
            try:
                payload = self._raw_request(
                    "POST",
                    "/api/v2/login",
                    body=body,
                    base_url=base_url,
                    auth=False,
                )
                data = self._unwrap(payload)
                self._store_tokens(data, base_url=base_url)
                return
            except AuthenticationError:
                raise
            except StorageApiError as exc:
                last_error = exc
        raise StorageApiError(f"WEKA login failed against all endpoints: {last_error}") from last_error

    def _refresh_access_token(self) -> None:
        """Refresh the WEKA access token using the refresh token."""
        if not self._refresh_token or not self._active_base_url:
            raise AuthenticationError("no refresh token available")
        payload = self._raw_request(
            "POST",
            "/api/v2/login/refresh",
            body={"refresh_token": self._refresh_token},
            base_url=self._active_base_url,
            auth=False,
        )
        data = self._unwrap(payload)
        self._store_tokens(data, base_url=self._active_base_url)

    def _store_tokens(self, payload: Any, *, base_url: str) -> None:
        """Store WEKA authentication tokens and expiry metadata."""
        if not isinstance(payload, dict):
            raise StorageApiError(f"unexpected login response type: {type(payload)!r}")
        access = payload.get("access_token")
        if not access:
            raise StorageApiError("login response missing access_token")
        self._access_token = str(access)
        refresh = payload.get("refresh_token")
        self._refresh_token = str(refresh) if refresh else None
        expires_in = int(payload.get("expires_in") or 300)
        self._token_expires_at = time.monotonic() + expires_in
        self._active_base_url = base_url

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        """Send an authenticated provider API request."""
        envelope = self._request_envelope(method, path, body=body, params=params)
        return self._unwrap(envelope)

    def _request_envelope(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        """Send a WEKA request and return the response envelope."""
        self._ensure_token()
        assert self._active_base_url is not None
        try:
            payload = self._raw_request(
                method,
                path,
                body=body,
                params=params,
                base_url=self._active_base_url,
                auth=True,
            )
        except AuthenticationError:
            self._access_token = None
            self._ensure_token()
            payload = self._raw_request(
                method,
                path,
                body=body,
                params=params,
                base_url=self._active_base_url,
                auth=True,
            )
        if isinstance(payload, dict):
            return payload
        return {"data": payload}

    def _raw_request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        base_url: str,
        auth: bool,
    ) -> Any:
        """Perform one raw HTTP request to the provider API."""
        query = urllib.parse.urlencode(params) if params else ""
        url = base_url + path + (f"?{query}" if query else "")
        data = json.dumps(body).encode() if body is not None else None
        headers: dict[str, str] = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if auth:
            if not self._access_token:
                raise AuthenticationError("missing access token")
            headers["Authorization"] = f"Bearer {self._access_token}"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            if exc.code in _AUTH_STATUS_CODES:
                raise AuthenticationError(f"WEKA {method} {path}: HTTP {exc.code} {exc.reason}") from exc
            body_text = ""
            try:
                body_text = exc.read().decode(errors="replace").strip()
            except Exception:
                pass
            if exc.code == 404 and _is_unknown_route(body_text):
                raise NotSupportedError(
                    f"WEKA {method} {path}: route not implemented on this cluster release: {body_text}"
                ) from exc
            if exc.code == 404:
                raise NotFoundError(f"WEKA {method} {path}: HTTP 404 {exc.reason}") from exc
            raise StorageApiError(f"WEKA {method} {path}: HTTP {exc.code}: {body_text or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise StorageApiError(f"WEKA {method} {path}: {exc.reason}") from exc

    @staticmethod
    def _unwrap(payload: Any) -> Any:
        """Return the data payload from a provider response envelope."""
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    def _filesystems(self) -> list[dict[str, Any]]:
        """Return filesystem rows from the WEKA API."""
        filesystems = self._request("GET", "/api/v2/fileSystems")
        if not isinstance(filesystems, list):
            raise StorageApiError(f"unexpected /fileSystems response type: {type(filesystems)!r}")
        return filesystems

    def _get_filesystem(self) -> dict[str, Any]:
        """Return the configured WEKA filesystem row."""
        for fs in self._filesystems():
            if str(fs.get("name") or "") == self._filesystem:
                return fs
        raise StorageApiError(f"filesystem {self._filesystem!r} not found on WEKA cluster")

    def _filesystem_for_volume(self, volume_id: str) -> dict[str, Any] | None:
        """The filesystem a ``weka/v2`` volume id names, if the cluster has one."""
        name = _fs_name_from_volume_id(volume_id)
        if not name:
            return None
        for fs in self._filesystems():
            if str(fs.get("name") or "") == name:
                return fs
        return None

    def _filesystem_uid_for_volume(self, volume_id: str) -> str:
        """Return the filesystem uid for a ``weka/v2`` volume."""
        fs = self._filesystem_for_volume(volume_id)
        if fs is None:
            raise NotFoundError(f"volume {volume_id!r} not found")
        uid = str(fs.get("uid") or "")
        if not uid:
            raise StorageApiError(f"filesystem {fs.get('name')!r} missing uid")
        return uid

    def _filesystem_to_volume(self, fs: dict[str, Any], *, tenant_id: str) -> Volume:
        """Map a WEKA filesystem to the ``Volume`` a ``weka/v2`` PVC exposes."""
        name = str(fs.get("name") or "")
        handle = _FS_HANDLE_PREFIX + name
        total = int(fs.get("total_budget") or fs.get("ssd_budget") or 0)
        used = int(fs.get("used_total") or 0)
        state: VolumeState = _FS_STATE_MAP.get(str(fs.get("status") or "").lower(), "failed")

        return Volume(
            tenant_id=tenant_id,
            # CSI handle (not WEKA uid) so get_volume / quota calls round-trip.
            id=handle,
            size_bytes=total,
            created_at=datetime.now(UTC),
            type="file",
            state=state,
            name=name,
            csi=CsiSpec(driver="csi.weka.io", volume_handle=handle, fs_type="wekafs"),
            used_bytes=used,
            available_bytes=max(0, total - used),
            attributes={
                "path": "",
                "filesystem": name,
                "uid": str(fs.get("uid") or ""),
            },
        )

    def _tenant_quota_from_filesystem(self, tenant_id: str) -> TenantQuota:
        """Build tenant quota from the configured filesystem."""
        fs = self._get_filesystem()
        hard_limit_bytes = int(fs.get("total_budget") or fs.get("ssd_budget") or 0)
        used_bytes = int(fs.get("used_total") or 0)
        name = str(fs.get("name") or self._filesystem)
        return TenantQuota(
            tenant_id=tenant_id,
            hard_limit_bytes=hard_limit_bytes,
            used_bytes=used_bytes,
            name=name,
        )

    def _tenant_quota_from_directory_quotas(self, tenant_id: str) -> TenantQuota:
        """Build tenant quota from directory quota rows."""
        quotas = self._list_directory_quotas(include_root=True)
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
            hard_limit_bytes = int(parent.get("hard_limit_bytes") or 0)
            name = str(parent.get("path") or storage_path)
        else:
            hard_limit_bytes = sum(int(q.get("hard_limit_bytes") or 0) for q in children)
            name = f"WEKA {storage_path}"

        used_bytes = sum(int(q.get("total_bytes") or 0) for q in children)
        return TenantQuota(
            tenant_id=tenant_id,
            hard_limit_bytes=hard_limit_bytes,
            used_bytes=used_bytes,
            name=name,
        )

    def _list_directory_quotas(self, *, fs_uid: str | None = None, include_root: bool = False) -> list[dict[str, Any]]:
        """Paginated directory quotas for ``fs_uid`` (default: ``WEKA_FILESYSTEM``).

        When ``fs_uid`` is omitted, apply ``WEKA_STORAGE_PATH`` filtering.
        Callers that pass ``fs_uid`` already scope by the volume root.
        """
        scoped_to_volume = fs_uid is not None
        if fs_uid is None:
            fs_uid = self._filesystem_uid()

        all_quotas: list[dict[str, Any]] = []
        params: dict[str, str] | None = None
        for _ in range(_MAX_PAGES):
            envelope = self._request_envelope(
                "GET",
                f"/api/v2/fileSystems/{fs_uid}/quota",
                params=params,
            )
            page = envelope.get("data")
            if page is None:
                page = envelope.get("quotas") or envelope.get("results")
                if page is None:
                    raise StorageApiError(
                        f"unexpected /fileSystems/{fs_uid}/quota response shape: keys={sorted(envelope)}"
                    )
            if isinstance(page, dict):
                page = [page]
            if not isinstance(page, list):
                break
            all_quotas.extend(page)

            next_token = envelope.get("next_token")
            if not next_token or (params is not None and str(next_token) == params.get("next_token")):
                break
            params = {"next_token": str(next_token)}

        if scoped_to_volume:
            return all_quotas
        return self._filter_quotas(all_quotas, include_root=include_root)

    def _filter_quotas(self, quotas: list[dict[str, Any]], *, include_root: bool) -> list[dict[str, Any]]:
        """Filter quota rows to the configured storage path."""
        if not self._storage_path:
            return quotas

        storage_path = self._storage_path.rstrip("/")
        filtered: list[dict[str, Any]] = []
        for q in quotas:
            path_norm = (q.get("path") or "").rstrip("/")
            if path_norm == storage_path and include_root:
                filtered.append(q)
            elif path_norm.startswith(storage_path + "/"):
                filtered.append(q)
        return filtered

    def _directory_quota(self, q: dict[str, Any], tenant_id: str, volume_id: str, rel_path: str) -> DirectoryQuota:
        """Convert a WEKA quota row to a DirectoryQuota."""
        hard = int(q.get("hard_limit_bytes") or 0)
        return DirectoryQuota(
            tenant_id=tenant_id,
            volume_id=volume_id,
            path=rel_path,
            id=_quota_id(q),
            hard=QuotaLimits(bytes=hard if hard > 0 else None),
            usage=QuotaUsage(bytes=int(q.get("total_bytes") or 0)),
        )

    # --- directory-quota REST helpers -----------------------------------

    def _filesystem_uid(self) -> str:
        """Return the configured filesystem UID."""
        fs = self._get_filesystem()
        uid = str(fs.get("uid") or "")
        if not uid:
            raise StorageApiError(f"filesystem {self._filesystem!r} missing uid")
        return uid

    def _resolve_inode(self, fs_uid: str, quota_path: str) -> str:
        """Resolve an absolute filesystem path to its inode id (for a new quota)."""
        data = self._request(
            "GET",
            f"/api/v2/fileSystems/{urllib.parse.quote(fs_uid)}/resolvePath",
            params={"path": _norm_path(quota_path)},
        )
        inode = str(data.get("inode_id") or "") if isinstance(data, dict) else ""
        if not inode:
            raise NotFoundError(f"path {quota_path!r} not found on WEKA filesystem")
        return inode

    def _find_directory_quota_by_path(self, fs_uid: str, abs_path: str) -> dict[str, Any] | None:
        """Find a directory quota row by absolute path."""
        want = _norm_path(abs_path)
        for q in self._list_directory_quotas(fs_uid=fs_uid):
            if _norm_path(q.get("path") or "") == want:
                return q
        return None

    def _quota_path(self, fs_uid: str, inode: str) -> str:
        """Build the WEKA directory quota endpoint path."""
        return f"/api/v2/fileSystems/{urllib.parse.quote(fs_uid)}/quota/{urllib.parse.quote(inode)}"

    def _put_directory_quota(self, fs_uid: str, inode: str, hard: int) -> dict[str, Any]:
        """Create a WEKA directory quota for an inode."""
        # PUT creates the quota; WEKA requires the full limit tuple even when
        # only a hard limit is configured (0 keeps soft/grace unlimited).
        data = self._request(
            "PUT",
            self._quota_path(fs_uid, inode),
            body={"hard_limit_bytes": hard, "soft_limit_bytes": 0, "grace_seconds": 0},
        )
        return data if isinstance(data, dict) else {}

    def _patch_directory_quota(self, fs_uid: str, inode: str, hard: int) -> dict[str, Any]:
        """Update a WEKA directory quota for an inode."""
        data = self._request(
            "PATCH",
            self._quota_path(fs_uid, inode),
            body={"hard_limit_bytes": hard},
        )
        return data if isinstance(data, dict) else {}

    def _delete_directory_quota(self, fs_uid: str, inode: str) -> None:
        """Delete a WEKA directory quota for an inode."""
        self._request("DELETE", self._quota_path(fs_uid, inode))

    # --- user-quota REST helpers ----------------------------------------

    def _filesystem_uid_for_user_quota(self, volume_id: str) -> str:
        """The filesystem uid a user quota may be written to.

        WEKA scopes user quotas to a whole filesystem, so a directory-backed
        volume is refused: its quota would also bind every other PVC sharing
        that filesystem.
        """
        if volume_id.strip().startswith(_DIR_V1_ID_PREFIX):
            raise NotSupportedError(
                "WEKA user quotas require a filesystem-backed (weka/v2) volume, one filesystem per PVC; "
                f"refusing directory-backed volume {volume_id!r}"
            )
        return self._filesystem_uid_for_volume(volume_id)

    def _user_quota_request(self, method: str, fs_uid: str, *, params: dict[str, str] | None = None) -> Any:
        """Send a WEKA user quota request with release mapping."""
        with _user_quota_route():
            return self._request(method, _user_quota_path(fs_uid), params=params)

    def _list_user_quota_rows(self, fs_uid: str) -> list[dict[str, Any]]:
        """Every user-quota row on ``fs_uid``, following ``next_token``."""
        rows: list[dict[str, Any]] = []
        params: dict[str, str] | None = None
        for _ in range(_MAX_PAGES):
            with _user_quota_route():
                envelope = self._request_envelope("GET", _user_quota_path(fs_uid), params=params)
            page = envelope.get("data")
            if isinstance(page, dict):
                page = [page]
            if not isinstance(page, list):
                break
            rows.extend(row for row in page if isinstance(row, dict))

            next_token = envelope.get("next_token")
            # next_token is a number in the body but must go back as a query
            # value; the final page omits it.
            if not next_token or (params is not None and str(next_token) == params.get("next_token")):
                break
            params = {"next_token": str(next_token)}
        return rows

    def _user_quota(self, row: dict[str, Any], tenant_id: str, volume_id: str) -> UserQuota:
        """Convert a WEKA user quota row to a UserQuota."""
        # WEKA reports 0 as unlimited rather than as a zero-byte allowance.
        hard = int(row.get("hard_limit_bytes") or 0)
        return UserQuota(
            tenant_id=tenant_id,
            volume_id=volume_id,
            user=str(_row_uid(row)),
            hard=QuotaLimits(bytes=hard if hard > 0 else None),
            usage=QuotaUsage(bytes=int(row.get("total_bytes") or 0)),
        )


# ----------------------------------------------------------------------
# Module-level path / id / quota helpers
# ----------------------------------------------------------------------


def _norm_path(p: str) -> str:
    """Normalize to a leading-slash absolute path (``""`` for empty / root)."""
    raw = (p or "").strip("/")
    return "/" + raw if raw else ""


def _quota_id(q: dict[str, Any]) -> str:
    """Return the stable quota identifier from a backend row."""
    qid = str(q.get("quota_id") or "").strip()
    if qid:
        return qid
    return str(q.get("inode_id") or "").strip()


def _quota_inode_id(q: dict[str, Any]) -> str:
    """The decimal inode id WEKA quota endpoints key on (falls back to quota_id)."""
    inode = str(q.get("inode_id") or "").strip()
    if inode:
        return inode
    return _quota_id(q)


def _hard_bytes(limits: QuotaLimits | None) -> int:
    """Return the hard byte limit, or zero for unlimited."""
    if limits is None or limits.bytes is None:
        return 0
    return int(limits.bytes)


def _parse_user_uid(user: str) -> int:
    """WEKA's ``user_id`` is numeric; usernames have no REST equivalent."""
    try:
        return int(user.strip())
    except ValueError:
        raise ValidationError(f"WEKA user subject must be a numeric uid, got {user!r}") from None


def _row_uid(row: dict[str, Any]) -> int:
    """Return the numeric UID from a WEKA user-quota row.

    The 5.1.26 vendor shape carries ``uid_or_gid``. Observed 5.1.31 rows omit
    that field and encode the uid in ``quota_id`` as ``USER:<uid>`` instead.
    """
    raw = row.get("uid_or_gid")
    if raw is not None and raw != "":
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    qid = str(row.get("quota_id") or "").strip()
    if qid.upper().startswith("USER:"):
        try:
            return int(qid.split(":", 1)[1])
        except ValueError:
            pass
    raise ValidationError(f"WEKA user-quota row missing uid (quota_id={qid!r}, uid_or_gid={raw!r})")


def _user_quota_path(fs_uid: str) -> str:
    """Build the WEKA user quota endpoint path."""
    return f"/api/v2/fileSystems/{urllib.parse.quote(fs_uid)}/quota/user"


@contextlib.contextmanager
def _user_quota_route() -> Iterator[None]:
    """Name the release that carries these endpoints when the route is absent."""
    try:
        yield
    except NotSupportedError as exc:
        raise NotSupportedError(f"WEKA user quotas require WEKA {_MIN_USER_QUOTA_RELEASE} or later: {exc}") from exc


def _default_user_slot_unsupported() -> NotSupportedError:
    """Build the default-user-slot unsupported error."""
    return NotSupportedError(
        "WEKA user quotas address a specific uid; the default-user slot (user=None) has no WEKA equivalent"
    )


def _is_unknown_route(message: str) -> bool:
    """Whether a 404 body names a missing *route* rather than a missing object.

    WEKA answers a route it does not implement with 404 and a body like
    ``{"message":"Route GET - /api/v2/… does not exist"}``, whereas a missing
    object reads ``{"message":"uid: '…' not found"}``. An absent API surface is
    a capability gap, so the two must not collapse together.
    """
    return '"Route ' in message and "does not exist" in message


def build_api() -> StorageProvider:
    """Entry point isvtest calls. Single hook the provider commits to.

    Composes ``WekaApi`` into a served ``StorageProvider`` via
    ``new_implementation``: capabilities are detected from the overridden
    methods. WEKA resolves its own default tenant internally, so no
    ``default_tenant`` wrapping is applied here.
    """
    impl = WekaApi()
    return new_implementation(core=impl._core, impl=impl)
