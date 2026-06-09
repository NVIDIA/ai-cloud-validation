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

"""Tests for the InfiniBand fabric-security validations (SDN04-04, SDN04-05)."""

from __future__ import annotations

from typing import Any

import pytest

from isvtest.validations.infiniband import IbKeysConfiguredCheck, IbTenantIsolationCheck

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _partition(
    *,
    name: str = "turbo-net",
    partition_key: str | None = "0x1",
    tenant_id: str = "tenant-a",
    status: str = "Ready",
) -> dict[str, Any]:
    """Build an InfiniBand partition record."""
    return {
        "name": name,
        "partition_key": partition_key,
        "tenant_id": tenant_id,
        "status": status,
    }


def _isolation_output(
    *,
    success: bool = True,
    partitions: list[dict[str, Any]] | None = None,
    error: str = "",
) -> dict[str, Any]:
    """Build a tenant-isolation step output."""
    if partitions is None:
        partitions = [
            _partition(name="a", partition_key="0x1", tenant_id="tenant-a"),
            _partition(name="b", partition_key="0x2", tenant_id="tenant-b"),
        ]
    return {
        "success": success,
        "platform": "nico",
        "site_id": "test-site-001",
        "partitions_checked": len(partitions),
        "partitions": partitions,
        "error": error,
    }


def _key(configured: bool | None, *, source: str = "nico", detail: str = "ok") -> dict[str, Any]:
    """Build a single key-evidence record."""
    return {"configured": configured, "source": source, "detail": detail}


def _keys_output(
    *,
    success: bool = True,
    keys: dict[str, dict[str, Any]] | None = None,
    partitions_with_pkey: int = 2,
    error: str = "",
) -> dict[str, Any]:
    """Build an IB-keys step output."""
    if keys is None:
        keys = {
            "p_key": _key(True, source="nico", detail="2 partition(s) carry a P_Key"),
            "management_key": _key(True, source="ufm", detail="m_key configured with per-port protection"),
        }
    return {
        "success": success,
        "platform": "nico",
        "site_id": "test-site-001",
        "partitions_with_pkey": partitions_with_pkey,
        "keys": keys,
        "error": error,
    }


# ===========================================================================
# IbTenantIsolationCheck tests (SDN04-04)
# ===========================================================================


class TestIbTenantIsolationCheck:
    """Tests for IbTenantIsolationCheck validation."""

    def test_distinct_tenant_pkeys_pass(self) -> None:
        """Per-tenant, distinct, non-default P_Keys pass."""
        check = IbTenantIsolationCheck(config={"step_output": _isolation_output()})
        check.run()
        assert check._passed is True, check._error
        assert "2 tenant(s)" in check._output
        subtests = {r["name"] for r in check._subtest_results}
        assert {"partition_a", "partition_b"} <= subtests

    def test_step_failure(self) -> None:
        """A failed step is reported with its error detail."""
        check = IbTenantIsolationCheck(config={"step_output": _isolation_output(success=False, error="API timeout")})
        check.run()
        assert check._passed is False
        assert "API timeout" in check._error

    def test_skipped_step(self) -> None:
        """A structured skip (no partitions) skips the validation."""
        check = IbTenantIsolationCheck(
            config={"step_output": {"success": True, "skipped": True, "skip_reason": "No InfiniBand partitions found"}}
        )
        with pytest.raises(pytest.skip.Exception, match="No InfiniBand partitions found"):
            check.run()

    def test_missing_partitions_list(self) -> None:
        """A non-list partitions field fails."""
        output = _isolation_output()
        output["partitions"] = None
        check = IbTenantIsolationCheck(config={"step_output": output})
        check.run()
        assert check._passed is False
        assert "partitions" in check._error

    def test_min_partitions_not_met(self) -> None:
        """Fewer partitions than min_partitions fails."""
        check = IbTenantIsolationCheck(
            config={"step_output": _isolation_output(partitions=[_partition()]), "min_partitions": 2}
        )
        check.run()
        assert check._passed is False
        assert "at least 2" in check._error

    def test_partition_without_pkey_fails(self) -> None:
        """A partition with no allocated P_Key is not isolated."""
        partitions = [_partition(name="a", partition_key=None, tenant_id="tenant-a")]
        check = IbTenantIsolationCheck(config={"step_output": _isolation_output(partitions=partitions)})
        check.run()
        assert check._passed is False
        assert "no P_Key" in check._error
        sub = next(r for r in check._subtest_results if r["name"] == "partition_a")
        assert sub["passed"] is False

    def test_partition_without_tenant_fails(self) -> None:
        """A partition not scoped to a tenant is not an isolation boundary."""
        partitions = [_partition(name="a", partition_key="0x1", tenant_id="")]
        check = IbTenantIsolationCheck(config={"step_output": _isolation_output(partitions=partitions)})
        check.run()
        assert check._passed is False
        assert "not scoped to a tenant" in check._error

    def test_default_partition_pkey_fails(self) -> None:
        """A tenant partition reusing the all-ports default partition fails."""
        partitions = [_partition(name="mgmt", partition_key="0x7fff", tenant_id="tenant-a")]
        check = IbTenantIsolationCheck(config={"step_output": _isolation_output(partitions=partitions)})
        check.run()
        assert check._passed is False
        assert "default all-ports partition" in check._error

    def test_shared_pkey_across_tenants_fails(self) -> None:
        """The same P_Key owned by two tenants is an isolation breach."""
        partitions = [
            _partition(name="a", partition_key="0x5", tenant_id="tenant-a"),
            _partition(name="b", partition_key="0x5", tenant_id="tenant-b"),
        ]
        check = IbTenantIsolationCheck(config={"step_output": _isolation_output(partitions=partitions)})
        check.run()
        assert check._passed is False
        assert "shared across tenants" in check._error
        sub = next(r for r in check._subtest_results if r["name"].startswith("pkey_"))
        assert sub["passed"] is False

    def test_same_tenant_multiple_pkeys_pass(self) -> None:
        """One tenant owning several distinct P_Keys is fine."""
        partitions = [
            _partition(name="a", partition_key="0x1", tenant_id="tenant-a"),
            _partition(name="b", partition_key="0x2", tenant_id="tenant-a"),
        ]
        check = IbTenantIsolationCheck(config={"step_output": _isolation_output(partitions=partitions)})
        check.run()
        assert check._passed is True, check._error
        assert "1 tenant(s)" in check._output

    def test_pkey_hex_decimal_collision_detected(self) -> None:
        """Differently-formatted keys that resolve to the same value collide."""
        partitions = [
            _partition(name="a", partition_key="0x10", tenant_id="tenant-a"),
            _partition(name="b", partition_key="16", tenant_id="tenant-b"),
        ]
        check = IbTenantIsolationCheck(config={"step_output": _isolation_output(partitions=partitions)})
        check.run()
        assert check._passed is False
        assert "shared across tenants" in check._error


# ===========================================================================
# IbKeysConfiguredCheck tests (SDN04-05)
# ===========================================================================


class TestIbKeysConfiguredCheck:
    """Tests for IbKeysConfiguredCheck validation."""

    def test_required_keys_configured_pass(self) -> None:
        """All required keys verified configured + P_Key evidence passes."""
        check = IbKeysConfiguredCheck(
            config={"step_output": _keys_output(), "required_keys": ["p_key", "management_key"]}
        )
        check.run()
        assert check._passed is True, check._error
        assert "2 required InfiniBand key(s) configured" in check._output

    def test_default_required_keys_includes_unverified_skips(self) -> None:
        """With the default 7 required keys, unverified UFM-host keys cause a skip."""
        keys = {
            "p_key": _key(True),
            "management_key": _key(True, source="ufm"),
            "aggregation_management_key": _key(None, source="ufm-host", detail="not exposed"),
            "vendor_specific_key": _key(None, source="ufm-host", detail="not exposed"),
            "congestion_control_key": _key(None, source="ufm-host", detail="not exposed"),
            "node2node_key": _key(None, source="ufm-host", detail="not exposed"),
            "manager2node_key": _key(None, source="ufm-host", detail="not exposed"),
        }
        check = IbKeysConfiguredCheck(config={"step_output": _keys_output(keys=keys)})
        with pytest.raises(pytest.skip.Exception, match="could not observe required key"):
            check.run()

    def test_step_failure(self) -> None:
        """A failed step is reported with its error detail."""
        check = IbKeysConfiguredCheck(config={"step_output": _keys_output(success=False, error="auth error")})
        check.run()
        assert check._passed is False
        assert "auth error" in check._error

    def test_skipped_step(self) -> None:
        """A structured skip (no partitions) skips the validation."""
        check = IbKeysConfiguredCheck(
            config={"step_output": {"success": True, "skipped": True, "skip_reason": "No InfiniBand partitions"}}
        )
        with pytest.raises(pytest.skip.Exception, match="No InfiniBand partitions"):
            check.run()

    def test_required_key_not_configured_fails(self) -> None:
        """A required key explicitly NOT configured fails."""
        keys = {
            "p_key": _key(True),
            "management_key": _key(False, source="ufm", detail="m_key is unset (0)"),
        }
        check = IbKeysConfiguredCheck(
            config={"step_output": _keys_output(keys=keys), "required_keys": ["p_key", "management_key"]}
        )
        check.run()
        assert check._passed is False
        assert "not configured: management_key" in check._error

    def test_unverified_required_key_skips(self) -> None:
        """A required key the script could not observe causes a skip, not a pass."""
        keys = {
            "p_key": _key(True),
            "management_key": _key(None, source="ufm", detail="UFM access not configured"),
        }
        check = IbKeysConfiguredCheck(
            config={"step_output": _keys_output(keys=keys), "required_keys": ["p_key", "management_key"]}
        )
        with pytest.raises(pytest.skip.Exception, match="management_key"):
            check.run()

    def test_missing_keys_object(self) -> None:
        """A non-dict keys field fails."""
        output = _keys_output()
        output["keys"] = None
        check = IbKeysConfiguredCheck(config={"step_output": output})
        check.run()
        assert check._passed is False
        assert "keys" in check._error

    def test_missing_required_key_entry_fails(self) -> None:
        """A required key absent from the keys object fails."""
        keys = {"p_key": _key(True)}
        check = IbKeysConfiguredCheck(
            config={"step_output": _keys_output(keys=keys), "required_keys": ["p_key", "management_key"]}
        )
        check.run()
        assert check._passed is False
        assert "missing required key(s): management_key" in check._error

    def test_no_pkey_evidence_fails(self) -> None:
        """Zero partitions carrying a P_Key fails the concrete evidence check."""
        keys = {"p_key": _key(True), "management_key": _key(True, source="ufm")}
        check = IbKeysConfiguredCheck(
            config={
                "step_output": _keys_output(keys=keys, partitions_with_pkey=0),
                "required_keys": ["p_key", "management_key"],
            }
        )
        check.run()
        assert check._passed is False
        assert "P_Key" in check._error

    def test_missing_partitions_with_pkey_field(self) -> None:
        """A missing integer partitions_with_pkey field fails."""
        output = _keys_output()
        output["partitions_with_pkey"] = None
        check = IbKeysConfiguredCheck(config={"step_output": output, "required_keys": ["p_key"]})
        check.run()
        assert check._passed is False
        assert "partitions_with_pkey" in check._error

    def test_invalid_required_keys(self) -> None:
        """An empty required_keys list is rejected."""
        check = IbKeysConfiguredCheck(config={"step_output": _keys_output(), "required_keys": []})
        check.run()
        assert check._passed is False
        assert "required_keys" in check._error
