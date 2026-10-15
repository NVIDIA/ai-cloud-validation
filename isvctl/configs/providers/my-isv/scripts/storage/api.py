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

"""Storage shim template (copy and fill in for your backend).

Each method has a ``TODO`` block - swap in your backend's SDK / REST / CLI
calls and return the appropriate dataclass. The ``ISVCTL_DEMO_MODE=1`` gate
returns dummy data so a demo acceptance run passes end-to-end before the real
backend is wired up (see the demo command in ``README.md``).

The shim subclasses ``Implementation`` and is composed by ``new_implementation()``
in ``build_api()``. You declare identity once as a static ``ProviderProperties``
*core*, then implement ONLY the surfaces your backend serves. Which capabilities
are supported is *detected* from the methods you override; ``new_implementation``
gates everything else. This is the key rule of the detection model:

  **To say "not supported", simply do NOT define the method** - inherit the base.
  Do NOT ship a stub that raises: an overridden method reads as *supported* and
  fails the suite when probed.

The sibling ``config/overlays/storage-provider-manifest.yaml`` is the contract the
validation suite probes: every capability it marks ``supported`` must answer,
and a surface you did not implement must be declared ``none``. Keep the two in
sync - detection and the manifest must agree.

Authoring checklist:

  1. Replace the ``_CORE`` identity declaration with your backend's values.
  2. Replace ``MyStorageApi.__init__`` with your backend connection setup.
  3. Fill in ``health_check`` with an authenticated round-trip.
  4. Fill in ``get_tenant_quota`` and ``list_volumes``.
  5. To back a surface your backend offers (volume create/delete, directory- or
     user-quota CRUD, tenant enumeration): ADD the method, declare any L2
     semantics in ``capability_qualifiers()``, and flip the matching manifest
     entry to ``native``. To NOT offer one, leave the method undefined and
     declare it ``none`` (the managed-K8s default is that the CSI driver owns
     volume lifecycle, so ``create_volume`` / ``delete_volume`` are omitted here
     and the suite falls back to ``list_volumes``).
  6. Keep ``config/overlays/storage-provider-manifest.yaml`` in sync: every capability it
     marks ``native``/``default`` must be implemented (the suite enforces it).
"""

from __future__ import annotations

import os

from isvtest.core.storage_provider import (
    API_VERSION,
    GetTenantQuotaRequest,
    Implementation,
    ImplementationCapabilities,
    ListVolumesRequest,
    ListVolumesResponse,
    ProviderProperties,
    StorageProvider,
    TenantQuota,
    VersionMetadata,
    Volume,
    new_implementation,
)

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

# ----------------------------------------------------------------------
# TODO: Replace the identity fields with your backend's values.
#
# provider_namespace + provider_id form the registration key
# <namespace>/<id>; provider_metadata.version MUST be semver.
# backend_metadata is the storage system you front (version is an
# opaque vendor passthrough). Capabilities are NOT declared here - they
# are detected from which methods MyStorageApi overrides.
# ----------------------------------------------------------------------
_CORE = ProviderProperties(
    provider_namespace="my-isv.example.com",
    provider_id="my-isv-shared-fs",
    provider_metadata=VersionMetadata(
        vendor_name="My ISV",
        name="My ISV Shared FS",
        version="0.1.0",
    ),
    sdk_version=API_VERSION,
    storage_type="file",
    storage_protocols=["nfsv4"],
)


class MyStorageApi(Implementation):
    """Replace the TODO blocks with real backend calls.

    Overrides only the surfaces my-isv backs: ``health_check``,
    ``get_tenant_quota``, ``list_volumes`` (and, by base delegation,
    ``get_volume``). Everything else is left undefined -> detected as
    unsupported -> gated by ``new_implementation``.
    """

    def __init__(self) -> None:
        # ----------------------------------------------------------------------
        # TODO: Replace this block with your backend connection setup
        #
        # 1. Read endpoint + credentials from env / a sidecar config file
        #    (e.g. /etc/shim/config.yaml, mounted from the per-provider
        #    ConfigMap referenced by providers[].shim.configmap).
        # 2. Build any SDK client / HTTP session you need.
        # ----------------------------------------------------------------------
        if DEMO_MODE:
            self._demo_volumes: dict[str, Volume] = {}
            self._demo_hard_limit_bytes = 100 * 1024**3

    def health_check(self) -> None:
        """Authenticated round-trip to the backend management API."""
        # ----------------------------------------------------------------------
        # TODO: Issue an authenticated GET against your management API
        # (e.g. a /healthz or /version endpoint). Raise AuthenticationError
        # on a 401/403; return None on success.
        # ----------------------------------------------------------------------
        if DEMO_MODE:
            return
        raise NotImplementedError(
            "Not implemented - replace with your platform's authenticated round-trip "
            "(raise AuthenticationError on a 401/403)"
        )

    def get_tenant_quota(self, req: GetTenantQuotaRequest) -> TenantQuota:
        """Overall storage utilization for the tenant.

        ``req.tenant_id`` is already resolved to the configured default (see
        ``build_api``'s ``default_tenant``) by the time it reaches here.
        """
        resolved = req.tenant_id
        # ----------------------------------------------------------------------
        # TODO: Call your tenant-quota API for `resolved` and return a
        # TenantQuota(tenant_id, hard_limit_bytes, used_bytes, name).
        # ----------------------------------------------------------------------
        if DEMO_MODE:
            used = sum(v.size_bytes for v in self._demo_volumes.values() if v.tenant_id == resolved)
            return TenantQuota(
                tenant_id=resolved,
                hard_limit_bytes=self._demo_hard_limit_bytes,
                used_bytes=used,
                name="my-isv demo tenant",
            )
        raise NotImplementedError("Not implemented - replace with your platform's tenant-quota query logic")

    def list_volumes(self, req: ListVolumesRequest) -> ListVolumesResponse:
        """Return volumes in the tenant, optionally filtered."""
        resolved = req.tenant_id
        wanted_ids = set(req.ids) if req.ids else None
        filters = list(req.tag_filters)
        # ----------------------------------------------------------------------
        # TODO: Query your backend for volumes in `resolved` tenant, honoring
        # `wanted_ids` and ANDing every TagFilter in `filters`. Keep the
        # listing scoped to the selected tenant - never leak foreign tenants.
        # ----------------------------------------------------------------------
        if DEMO_MODE:
            result: list[Volume] = []
            for vol in self._demo_volumes.values():
                if vol.tenant_id != resolved:
                    continue
                if wanted_ids is not None and vol.id not in wanted_ids:
                    continue
                if not all(f.key in vol.tags and (not f.values or vol.tags[f.key] in f.values) for f in filters):
                    continue
                result.append(vol)
            return ListVolumesResponse(volumes=tuple(result))
        raise NotImplementedError("Not implemented - replace with your platform's volume-list logic")

    # ----------------------------------------------------------------------
    # Optional L2 semantics hook.
    #
    # Declare semantic facts a caller needs to behave correctly on a surface
    # you back (NOT whether it is supported - that is detected). Amend the typed
    # view in place; a fact set on a group inherits to its leaves, e.g.:
    #
    #   from isvtest.core.storage_provider import QUAL_ID_ASSIGNMENT
    #   caps.quota().directory().set_id_assignment("backend").set_inodes(True)
    #
    # `set_state(...)` on a node narrows its runtime availability
    # ("unavailable" / "disabled") but can never enable an unimplemented surface.
    # ----------------------------------------------------------------------
    def capability_qualifiers(self, caps: ImplementationCapabilities) -> None:
        return None


def build_api() -> StorageProvider:
    """Entry point isvtest calls. Single hook the provider commits to.

    Composes ``MyStorageApi`` into a served ``StorageProvider`` via
    ``new_implementation``: capabilities are detected from the overridden
    methods, and ``default_tenant`` (from ``STORAGE_TENANT_ID``) is injected into
    any request that leaves ``tenant_id`` unset - so the impl always sees a
    resolved tenant. That env var is the knob that actually selects the tenant.

    The manifest's ``tenant_id`` field does NOT configure the shim; it is a
    cross-check assertion. ``StorageProviderApiCheck`` fails if
    ``get_tenant_quota().tenant_id`` disagrees with the manifest value, so keep
    ``STORAGE_TENANT_ID`` (or the fallback below) in sync with the manifest's
    ``tenant_id``.
    """
    default_tenant = os.environ.get("STORAGE_TENANT_ID", "my-isv-tenant")
    return new_implementation(core=_CORE, impl=MyStorageApi(), default_tenant=default_tenant)
