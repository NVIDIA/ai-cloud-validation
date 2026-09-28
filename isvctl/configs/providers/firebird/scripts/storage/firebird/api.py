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

"""Firebird ``StorageProvider`` shim over the Firebird Filesystem API.

Drives ``StorageProviderApiCheck`` (manifest: ``config/storage-provider-manifest.yaml``).
A volume is a Firebird filesystem (``/projects/{p}/storage/fs``), which the API
provisions in the tenant's organization on the parallel filesystem:

* ``health_check``      ``GET .../storage/fs`` (list filesystems)
* ``get_tenant_quota``  ``GET .../storage/fs/status``: the tenant organization's
                        total quota and allocation; the tenant is the project's
* ``list_volumes``      the project's filesystems (``get_volume`` rides on it, so
                        the shim never reads the per-filesystem response, which
                        carries a client login token)
* ``create_volume``     ``POST .../storage/fs``, waits for the Operation and a
                        READY readback; the name is ``isv-fs-`` plus the request's
                        name (``isvtest-<run_id>-<provider>`` -> ``isv-fs-<run_id>-<provider>``),
                        so the storage config's teardown finds it by the run prefix
* ``delete_volume``     ``DELETE .../storage/fs/{id}``, waits until it is gone;
                        refuses any filesystem not named ``isv-fs-...``

Directory and user quotas, tenant listing, and mount instructions are not
backed. Endpoint, project, and credentials come from the ``FIREBIRD_*``
environment variables (see ``common/firebird_client.py``).

The Firebird helpers are loaded by path under fixed module names, not as
``common.*``: this module runs inside the validation process, where ``common``
may be another provider's package.
"""

from __future__ import annotations

import importlib.util
import math
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from isvtest.core.storage_provider import (
    API_VERSION,
    AuthenticationError,
    CreateVolumeRequest,
    DeleteVolumeRequest,
    GetTenantQuotaRequest,
    Implementation,
    ListVolumesRequest,
    ListVolumesResponse,
    NotFoundError,
    NotSupportedError,
    ProviderProperties,
    QuotaExceededError,
    StorageApiError,
    StorageProvider,
    TenantQuota,
    ValidationError,
    VersionMetadata,
    Volume,
    VolumeState,
    new_implementation,
)

_COMMON = Path(__file__).resolve().parents[2] / "common"
CLIENT_MODULE = "_firebird_storage_shim_client"
FILESYSTEMS_MODULE = "_firebird_storage_shim_filesystems"
WAIT_SECONDS = 600
PROVIDER_VERSION = "0.1.0"  # keep in sync with the manifest's provider.version

# Firebird filesystem state -> StorageProvider volume state. Unknown states map
# to "failed" so the check notices them.
_STATES: dict[str, VolumeState] = {
    "PROVISIONING": "creating",
    "READY": "available",
    "UPDATING": "available",
    "ERROR": "failed",
    "DELETING": "deleting",
}
_REQUEST_NAME = re.compile(r"^isvtest-")
_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def _load(name: str, filename: str) -> ModuleType:
    """Load ``common/<filename>`` as module ``name`` (reusing an already loaded one)."""
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, _COMMON / filename)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {_COMMON / filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_fb = _load(CLIENT_MODULE, "firebird_client.py")
_fs = _load(FILESYSTEMS_MODULE, "filesystems.py")


def volume_name(requested: str | None) -> str:
    """Return the filesystem name for a requested volume name (``isv-fs-`` + the request's name)."""
    base = _NAME_CHARS.sub("-", _REQUEST_NAME.sub("", requested or "")).strip("-")
    if not base:
        base = f"{_fs.new_run_id()}-api"
    return (_fs.FS_PREFIX + base)[: _fs.NAME_MAX]


def _translate(error: Exception) -> StorageApiError:
    """Map an API error onto the StorageProvider error taxonomy."""
    status = int(getattr(error, "status", 0) or 0)
    if status in (401, 403):
        return AuthenticationError(str(error))
    if status == 404:
        return NotFoundError(str(error))
    if status == 429:
        return QuotaExceededError(str(error))
    if status == 400:
        return ValidationError(str(error))
    return StorageApiError(str(error))


class FirebirdFilesystemApi(Implementation):
    """``StorageProvider`` over one Firebird project's filesystems."""

    def __init__(self, client: Any = None) -> None:
        """Use ``client``, or a ``FirebirdClient`` configured from the environment."""
        self._client = client or _fb.FirebirdClient()
        self._tenant_id = ""
        self._core = ProviderProperties(
            provider_namespace="firebird.ai",
            provider_id="firebird-fs",
            provider_metadata=VersionMetadata(
                vendor_name="Firebird",
                name="Firebird shared filesystem",
                version=PROVIDER_VERSION,
            ),
            sdk_version=API_VERSION,
            storage_type="file",
            storage_protocols=["wekafs"],
            attributes={"project_id": self._client.project_id},
        )

    def backend_metadata(self) -> VersionMetadata | None:
        """The backend behind the Filesystem API: the parallel filesystem."""
        return VersionMetadata(vendor_name="WekaIO", name="WEKA", version="unknown")

    def _tenant(self) -> str:
        """Return the tenant the project belongs to (read once)."""
        if not self._tenant_id:
            try:
                self._tenant_id = str(self._client.get_project().get("tenantId", ""))
            except Exception as e:
                raise _translate(e) from e
        return self._tenant_id

    def health_check(self) -> None:
        """List the project's filesystems; an unreachable API or refused token is an AuthenticationError."""
        try:
            _fs.list_filesystems(self._client)
        except Exception as e:
            if int(getattr(e, "status", 0) or 0) in (0, 401, 403):
                raise AuthenticationError(f"Filesystem API not usable: {e}") from e
            raise _translate(e) from e

    def get_tenant_quota(self, req: GetTenantQuotaRequest) -> TenantQuota:
        """The tenant organization's quota and allocation (``/storage/fs/status``), in bytes."""
        tenant = self._tenant()
        if req.tenant_id is not None and req.tenant_id != tenant:
            raise NotFoundError(f"tenant {req.tenant_id!r} not found (this project belongs to {tenant!r})")
        try:
            total_gib, allocated_gib = _fs.storage_status(self._client)
        except Exception as e:
            raise _translate(e) from e
        return TenantQuota(
            tenant_id=tenant,
            hard_limit_bytes=total_gib * _fs.GIB_BYTES,
            used_bytes=allocated_gib * _fs.GIB_BYTES,
            name="Weka organization",
        )

    def list_volumes(self, req: ListVolumesRequest) -> ListVolumesResponse:
        """The project's filesystems as volumes (filesystems carry no tags, so a tag filter matches none)."""
        tenant = self._tenant()
        if req.tenant_id is not None and req.tenant_id != tenant:
            raise NotFoundError(f"tenant {req.tenant_id!r} not found (this project belongs to {tenant!r})")
        if req.tag_filters:
            return ListVolumesResponse(volumes=())
        try:
            items = _fs.list_filesystems(self._client)
        except Exception as e:
            raise _translate(e) from e
        wanted = set(req.ids)
        volumes = tuple(self._volume(fs, tenant) for fs in items if not wanted or fs.get("id") in wanted)
        return ListVolumesResponse(volumes=volumes)

    def create_volume(self, req: CreateVolumeRequest) -> Volume:
        """Create a filesystem of at least ``size_bytes`` (whole GiB, 1 GiB minimum) and wait until READY."""
        if req.volume_type != "file":
            raise ValidationError(f"Firebird filesystems are file volumes, not {req.volume_type!r}")
        if req.tier:
            raise NotSupportedError("Firebird filesystems have no tiers")
        tenant = self._tenant()
        size_gib = max(1, math.ceil(req.size_bytes / _fs.GIB_BYTES))
        created: list[str] = []
        try:
            fs = _fs.create_ready(self._client, volume_name(req.name), size_gib, WAIT_SECONDS, created)
        except Exception as e:
            # Leave nothing behind: a create that failed after the API accepted it is deleted here.
            _fs.delete_all(self._client, created, WAIT_SECONDS)
            raise _translate(e) from e
        return self._volume(fs, tenant)

    def delete_volume(self, req: DeleteVolumeRequest) -> None:
        """Delete one of this provider's filesystems and wait until it is gone (already gone: no-op)."""
        try:
            _fs.delete_filesystem(self._client, req.volume_id, WAIT_SECONDS)
        except _fs.RefusedDeleteError as e:
            raise ValidationError(str(e)) from e
        except Exception as e:
            raise _translate(e) from e

    @staticmethod
    def _volume(fs: dict[str, Any], tenant: str) -> Volume:
        """Map a filesystem resource onto a ``Volume``."""
        gib = _fs.capacity_gib(fs)
        return Volume(
            tenant_id=tenant,
            id=str(fs.get("id", "")),
            size_bytes=(gib or 0) * _fs.GIB_BYTES,
            created_at=_fb.parse_timestamp(fs.get("createdAt")) or datetime.now(UTC),
            type="file",
            state=_STATES.get(str(fs.get("state", "")), "failed"),
            name=fs.get("name"),
        )


def build_api() -> StorageProvider:
    """Entry point isvtest calls: compose ``FirebirdFilesystemApi`` into a served ``StorageProvider``."""
    impl = FirebirdFilesystemApi()
    return new_implementation(core=impl._core, impl=impl)
