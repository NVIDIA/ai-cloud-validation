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

"""``StorageProviderApiCheck`` - StorageProvider API contract conformity.

Drives the tenant-level subset of the ``StorageProvider`` shim contract against
each provider declared in the manifest: health-check / auth, volume
provisioning, and tenant quota visibility. Maps to acceptance tests
N-019/N-020/N-021. Subtests are namespaced by API area so this fixture can
later be split into discovery/volume/quota-specific siblings without
disturbing how callers select it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import ClassVar

from isvtest.core.storage import (
    ManifestError,
    Provider,
    load_provider_registry,
)
from isvtest.core.storage_provider import (
    CAP_DIRECTORY_QUOTA_DELETE,
    CAP_DIRECTORY_QUOTA_GET,
    CAP_DIRECTORY_QUOTA_LIST,
    CAP_DIRECTORY_QUOTA_SET,
    CAP_TENANT_GET,
    CAP_TENANT_GET_QUOTA,
    CAP_TENANT_LIST,
    CAP_TENANT_LIST_QUOTAS,
    CAP_USER_QUOTA_DELETE,
    CAP_USER_QUOTA_GET,
    CAP_USER_QUOTA_LIST,
    CAP_USER_QUOTA_SET,
    CAP_VOLUME_CREATE,
    CAP_VOLUME_GET,
    CAP_VOLUME_LIST,
    AuthenticationError,
    CreateVolumeRequest,
    DeleteDirectoryQuotaRequest,
    DeleteUserQuotaRequest,
    DeleteVolumeRequest,
    DirectoryQuota,
    GetDirectoryQuotaRequest,
    GetTenantQuotaRequest,
    GetTenantRequest,
    GetUserQuotaRequest,
    GetVolumeRequest,
    ListDirectoryQuotasRequest,
    ListTenantQuotasRequest,
    ListTenantsRequest,
    ListUserQuotasRequest,
    ListVolumesRequest,
    NotSupportedError,
    SetDirectoryQuotaRequest,
    SetUserQuotaRequest,
    StorageProvider,
    UserQuota,
)
from isvtest.core.validation import BaseValidation

# A deliberately nonexistent identifier used by the capability probes below. A correct
# shim rejects it (NotFoundError / ValidationError / ...) before mutating
# anything, whereas a stub for an unimplemented surface raises the
# not-implemented sentinel (NotSupportedError / NotImplementedError) regardless
# of input. Only the sentinel is a contract violation under a ``supported`` claim.
_PROBE_ID = "__isvtest_capability_probe__"
_PROBE_PATH = "/__isvtest_capability_probe__"

# How to invoke each capability so the manifest-consistency subtest can confirm a
# surface the manifest declares ``supported`` actually answers (does not raise
# the not-implemented sentinel). Reads use a sentinel resource id; writes use a
# sentinel tenant so the call is refused before committing. volume.create /
# volume.delete are exercised end-to-end (with cleanup) in volume-provisioning -
# the only place those mutating calls are made - so they are not probed here.
_CAPABILITY_PROBES: dict[str, Callable[[StorageProvider], object]] = {
    CAP_TENANT_LIST: lambda api: api.list_tenants(ListTenantsRequest()),
    CAP_TENANT_GET: lambda api: api.get_tenant(GetTenantRequest()),
    CAP_TENANT_GET_QUOTA: lambda api: api.get_tenant_quota(GetTenantQuotaRequest()),
    CAP_TENANT_LIST_QUOTAS: lambda api: api.list_tenant_quotas(ListTenantQuotasRequest()),
    CAP_VOLUME_LIST: lambda api: api.list_volumes(ListVolumesRequest()),
    CAP_VOLUME_GET: lambda api: api.get_volume(GetVolumeRequest(volume_id=_PROBE_ID)),
    CAP_DIRECTORY_QUOTA_LIST: lambda api: api.list_directory_quotas(ListDirectoryQuotasRequest(volume_id=_PROBE_ID)),
    CAP_DIRECTORY_QUOTA_GET: lambda api: api.get_directory_quota(
        GetDirectoryQuotaRequest(volume_id=_PROBE_ID, path=_PROBE_PATH)
    ),
    CAP_DIRECTORY_QUOTA_SET: lambda api: api.set_directory_quota(
        SetDirectoryQuotaRequest(DirectoryQuota(tenant_id=_PROBE_ID, volume_id=_PROBE_ID, path=_PROBE_PATH))
    ),
    CAP_DIRECTORY_QUOTA_DELETE: lambda api: api.delete_directory_quota(
        DeleteDirectoryQuotaRequest(volume_id=_PROBE_ID, path=_PROBE_PATH)
    ),
    CAP_USER_QUOTA_LIST: lambda api: api.list_user_quotas(ListUserQuotasRequest(volume_id=_PROBE_ID)),
    CAP_USER_QUOTA_GET: lambda api: api.get_user_quota(GetUserQuotaRequest(volume_id=_PROBE_ID, user=_PROBE_ID)),
    CAP_USER_QUOTA_SET: lambda api: api.set_user_quota(
        SetUserQuotaRequest(UserQuota(tenant_id=_PROBE_ID, volume_id=_PROBE_ID, user=_PROBE_ID))
    ),
    CAP_USER_QUOTA_DELETE: lambda api: api.delete_user_quota(
        DeleteUserQuotaRequest(volume_id=_PROBE_ID, user=_PROBE_ID)
    ),
}


class StorageProviderApiCheck(BaseValidation):
    """Validate the ``StorageProvider`` API contract per provider.

    Iterates every provider in the manifest that declares a ``shim:``
    block and reports four namespaced subtests per provider:

    * ``manifest-consistency[<name>]`` - the manifest is a contract: its
      declared identity (``type`` / ``provider.protocols`` /
      ``provider.version``) must match the running shim's ``properties()``, and
      every capability it declares ``supported`` (the ``native`` / ``default`` /
      ``none`` block lowered to ``cap_id -> supported?``) must actually answer
      when probed - a surface that raises ``NotSupportedError`` /
      ``NotImplementedError`` under a ``supported`` claim fails. Omitted claims
      and ``none`` claims are not checked. ``volume.create`` / ``volume.delete``
      are verified in volume-provisioning (the only place those calls are made).
    * ``api-authentication[<name>]`` - ``health_check()``
    * ``volume-provisioning[<name>]`` - ``create_volume`` + ``delete_volume``
      Falls back to ``list_volumes`` and reports skipped when the
      shim raises ``NotSupportedError`` (managed-K8s providers whose CSI driver
      owns provisioning) - unless the ``volume.create`` capability declares
      otherwise, in which case the mismatch fails.
    * ``tenant-quota[<name>]`` - ``get_tenant_quota`` with non-zero
      ``hard_limit_bytes`` (and ``tenant_id`` matching the manifest's
      ``tenant_id`` when declared)

    Skipped (passed) when the manifest is unset, missing providers, or
    contains no providers with a shim block.

    Config keys (with defaults):
        manifest_path: Path to the provider manifest YAML. In K8s mode
            this is the mounted ConfigMap file; in bare-metal mode it's
            a path on the management host. Empty -> the check is skipped.
        volume_size_bytes: Size requested by the volume
            (default: 1 GiB).
    """

    description: ClassVar[str] = "Validate StorageProvider API contract"
    timeout: ClassVar[int] = 300
    labels: ClassVar[tuple[str, ...]] = ("storage", "storage_provider_api")

    def run(self) -> None:
        """Load the manifest and drive per provider."""
        try:
            providers = load_provider_registry(self.config)
        except ManifestError as exc:
            self.set_failed(f"Failed to load provider manifest: {exc}")
            return

        if not providers:
            self.set_skipped("Skipped: no provider manifest configured (manifest_path unset). ")
            return

        shim_providers = [p for p in providers if p.has_shim]
        if not shim_providers:
            rest_only = [p.name for p in providers if p.shim_kind == "rest"]
            csi_only = [p.name for p in providers if p.shim_kind is None]
            note: list[str] = []
            if rest_only:
                note.append(f"REST shims skipped: {', '.join(sorted(rest_only))}")
            if csi_only:
                note.append(f"CSI-only providers (no management API to test) skipped: {', '.join(sorted(csi_only))}")
            self.set_passed(
                "Skipped: no provider in the manifest declares a Python `shim:` block; " + "; ".join(note)
                if note
                else "Skipped: no provider in the manifest declares a Python `shim:` block."
            )
            return

        run_id = str(self.config.get("run_id") or uuid.uuid4().hex[:12])
        volume_size_bytes = int(self.config.get("volume_size_bytes") or (1 << 30))

        any_failed = False
        for provider in shim_providers:
            if not self._exercise_provider(provider, run_id=run_id, volume_size_bytes=volume_size_bytes):
                any_failed = True

        if any_failed:
            self.set_failed("One or more provider shim subtests failed; see subtest details")
        else:
            self.set_passed(f"Storage shim verified for {', '.join(sorted(p.name for p in shim_providers))} ")

    def _exercise_provider(
        self,
        provider: Provider,
        *,
        run_id: str,
        volume_size_bytes: int,
    ) -> bool:
        """Drive the storage management API against one provider. Return True when all subtests pass."""
        tag = provider.name
        api = provider.api
        assert api is not None  # has_shim filter above guarantees this
        ok = True

        # manifest <-> shim contract: declared identity / capabilities must
        # match the running shim's properties() (and safe read-only probes).
        if not self._exercise_manifest_consistency(provider, tag=tag):
            ok = False

        # authentication / reachability.
        try:
            api.health_check()
            self.report_subtest(
                f"api-authentication[{tag}]",
                passed=True,
                message="health_check() ok",
            )
        except AuthenticationError as exc:
            self.report_subtest(
                f"api-authentication[{tag}]",
                passed=False,
                message=f"health_check() raised AuthenticationError: {exc}",
            )
            # Skip downstream subtests for this provider - they all need a
            # working auth surface. Other providers may still pass.
            self.report_subtest(
                f"volume-provisioning[{tag}]",
                passed=True,
                skipped=True,
                message="api-authentication failed; volume-provisioning skipped",
            )
            self.report_subtest(
                f"tenant-quota[{tag}]",
                passed=True,
                skipped=True,
                message="api-authentication failed; tenant-quota skipped",
            )
            return False
        except Exception as exc:
            self.report_subtest(
                f"api-authentication[{tag}]",
                passed=False,
                message=f"health_check() raised {type(exc).__name__}: {exc}",
            )
            ok = False

        # volume provisioning + cleanup.
        if not self._exercise_volume_provisioning(
            api,
            tag=tag,
            run_id=run_id,
            volume_size_bytes=volume_size_bytes,
            volume_type=provider.volume_type,
            declared_create=provider.expected_capabilities.get(CAP_VOLUME_CREATE),
        ):
            ok = False

        # tenant quota visible with non-zero hard limit.
        if not self._exercise_tenant_quota(api, tag=tag, declared_tenant_id=provider.tenant_id):
            ok = False

        return ok

    def _exercise_manifest_consistency(self, provider: Provider, *, tag: str) -> bool:
        """Assert the manifest's declared identity/capabilities match the shim.

        The manifest is a contract: a customer cannot declare values the running
        shim does not back. Identity is cross-checked against ``properties()``;
        every capability the manifest declares ``supported`` is probed at runtime
        and fails if the shim raises ``NotSupportedError`` / ``NotImplementedError``
        (any other error means the sentinel probe input was rejected, not the
        surface). Claims the manifest omits, and ``none`` claims, are not checked.
        ``create_volume`` / ``delete_volume`` are enforced in the
        volume-provisioning subtest (the only place those calls are made).
        """
        api = provider.api
        assert api is not None
        try:
            props = api.properties()
        except Exception as exc:
            self.report_subtest(
                f"manifest-consistency[{tag}]",
                passed=False,
                message=f"properties() raised {type(exc).__name__}: {exc}",
            )
            return False

        mismatches: list[str] = []

        # Storage type is always known (manifest type / provider.type).
        if props.storage_type != provider.volume_type:
            mismatches.append(
                f"storage_type: manifest={provider.volume_type!r} != shim.properties()={props.storage_type!r}"
            )

        # Wire protocols: every protocol the manifest declares must be advertised.
        if provider.storage_protocols:
            declared = {p.lower() for p in provider.storage_protocols}
            advertised = {p.lower() for p in props.storage_protocols}
            missing = declared - advertised
            if missing:
                mismatches.append(
                    f"storage_protocols: manifest declares {sorted(declared)} but shim "
                    f"advertises {sorted(advertised)} (missing {sorted(missing)})"
                )

        shim_version = props.provider_metadata.version if props.provider_metadata else None
        if provider.provider_version is not None and str(provider.provider_version) != str(shim_version):
            mismatches.append(f"provider.version: manifest={provider.provider_version!r} != shim={shim_version!r}")

        # Capabilities: every surface the manifest declares ``supported`` must
        # actually answer. Probe it live - a stub that raises the not-implemented
        # sentinel under a ``supported`` claim is a contract violation (any other
        # error means the sentinel probe input was rejected, not the surface).
        for cap_id, probe in _CAPABILITY_PROBES.items():
            if not provider.expected_capabilities.get(cap_id):
                continue  # only enforce surfaces the manifest claims supported
            try:
                probe(api)
            except (NotSupportedError, NotImplementedError) as exc:
                mismatches.append(
                    f"{cap_id}: manifest declares supported but the shim raised "
                    f"{type(exc).__name__} when probed ({exc})"
                )
            except Exception:
                # Non-sentinel error -> the surface is wired up; the deliberately
                # the sentinel probe input was rejected, which is the expected outcome.
                pass

        if mismatches:
            self.report_subtest(
                f"manifest-consistency[{tag}]",
                passed=False,
                message="manifest disagrees with shim: " + "; ".join(mismatches),
            )
            return False

        self.report_subtest(
            f"manifest-consistency[{tag}]",
            passed=True,
            message=(
                f"manifest matches shim (type={props.storage_type}, "
                f"protocols={list(props.storage_protocols)}, version={shim_version})"
            ),
        )
        return True

    def _exercise_volume_provisioning(
        self,
        api: StorageProvider,
        *,
        tag: str,
        run_id: str,
        volume_size_bytes: int,
        volume_type: str,
        declared_create: bool | None = None,
    ) -> bool:
        """create + delete with NotSupportedError fallback to list_volumes.

        When the manifest declares the ``volume.create`` capability, the
        observed behavior must match: declaring ``true`` and getting
        ``NotSupportedError`` (or declaring ``false`` and succeeding) is a
        contract violation rather than the silent CSI-owns-lifecycle skip.
        """
        volume_name = f"isvtest-{run_id}-{tag}"
        tags = {
            "isvtest-run-id": run_id,
            "provider": tag,
            "test-case": "N-020",
        }

        try:
            volume = api.create_volume(
                CreateVolumeRequest(
                    size_bytes=volume_size_bytes,
                    volume_type=volume_type,  # type: ignore[arg-type]
                    name=volume_name,
                    tags=tags,
                )
            )
        except NotSupportedError:
            if declared_create:
                self.report_subtest(
                    f"volume-provisioning[{tag}]",
                    passed=False,
                    message=(
                        "manifest volume.create=true but create_volume raised NotSupportedError (contract violation)"
                    ),
                )
                return False
            # Managed-K8s provider: CSI handles provisioning. Fall back
            # to asserting at least one CSI-provisioned volume is visible
            # (fallback path).
            try:
                existing = list(api.list_volumes(ListVolumesRequest()).volumes)
            except Exception as exc:
                self.report_subtest(
                    f"volume-provisioning[{tag}]",
                    passed=False,
                    message=(f"create_volume not implemented and list_volumes() raised {type(exc).__name__}: {exc}"),
                )
                return False
            self.report_subtest(
                f"volume-provisioning[{tag}]",
                passed=True,
                skipped=True,
                message=(
                    f"create_volume not implemented; observed {len(existing)} "
                    f"CSI-provisioned volume(s) via list_volumes()"
                ),
            )
            return True
        except Exception as exc:
            self.report_subtest(
                f"volume-provisioning[{tag}]",
                passed=False,
                message=f"create_volume raised {type(exc).__name__}: {exc}",
            )
            return False

        try:
            if declared_create is False:
                self.report_subtest(
                    f"volume-provisioning[{tag}]",
                    passed=False,
                    message=(
                        "manifest volume.create=false but create_volume "
                        f"succeeded (returned volume {volume.id}; contract violation)"
                    ),
                )
                return False
            if volume.state not in ("creating", "available"):
                self.report_subtest(
                    f"volume-provisioning[{tag}]",
                    passed=False,
                    message=(
                        f"create_volume returned volume {volume.id} in unexpected state "
                        f"{volume.state!r} (expected creating/available)"
                    ),
                )
                return False

            if volume.mount is not None:
                access = f"mount={volume.mount.source}"
            elif volume.csi is not None:
                access = f"csi={volume.csi.driver}#{volume.csi.volume_handle}"
            else:
                access = "access=n/a"
            self.report_subtest(
                f"volume-provisioning[{tag}]",
                passed=True,
                message=(
                    f"created volume {volume.id} (state={volume.state}, {access}, size_bytes={volume.size_bytes})"
                ),
            )
            return True
        finally:
            try:
                api.delete_volume(DeleteVolumeRequest(volume_id=volume.id))
            except NotSupportedError:
                self.log.warning(
                    "Provider %r implements create_volume but not delete_volume; "
                    "volume %s left behind (tags include isvtest-run-id=%s for orphan sweep)",
                    tag,
                    volume.id,
                    run_id,
                )
            except Exception as exc:
                self.log.warning(
                    "Provider %r delete_volume(%s) raised %s: %s",
                    tag,
                    volume.id,
                    type(exc).__name__,
                    exc,
                )

    def _exercise_tenant_quota(self, api: StorageProvider, *, tag: str, declared_tenant_id: str | None = None) -> bool:
        """tenant quota exists with non-zero hard limit (and matches declared tenant_id)."""
        try:
            quota = api.get_tenant_quota(GetTenantQuotaRequest())
        except Exception as exc:
            self.report_subtest(
                f"tenant-quota[{tag}]",
                passed=False,
                message=f"get_tenant_quota() raised {type(exc).__name__}: {exc}",
            )
            return False

        if declared_tenant_id and quota.tenant_id != declared_tenant_id:
            self.report_subtest(
                f"tenant-quota[{tag}]",
                passed=False,
                message=(
                    f"manifest tenant_id={declared_tenant_id!r} != "
                    f"get_tenant_quota().tenant_id={quota.tenant_id!r} (contract violation)"
                ),
            )
            return False

        if quota.hard_limit_bytes <= 0:
            self.report_subtest(
                f"tenant-quota[{tag}]",
                passed=False,
                message=(
                    f"get_tenant_quota() returned hard_limit_bytes={quota.hard_limit_bytes} "
                    f"for tenant {quota.tenant_id!r} (expected > 0)"
                ),
            )
            return False

        self.report_subtest(
            f"tenant-quota[{tag}]",
            passed=True,
            message=(
                f"tenant {quota.tenant_id!r} hard_limit_bytes={quota.hard_limit_bytes}, used_bytes={quota.used_bytes}"
            ),
        )
        return True
