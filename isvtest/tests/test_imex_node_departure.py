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

"""Tests for ImexNodeDepartureCheck (SDN19-01)."""

from __future__ import annotations

from typing import Any

import pytest

from isvtest.validations.network import ImexNodeDepartureCheck


def _departure_output(ops: dict[str, Any] | None = None, **top: Any) -> dict[str, Any]:
    """Build a step_output dict for IMEX node departure tests."""
    operations: dict[str, Any] = {
        "stop": {"requested": True, "clean_exit": True},
        "peer_convergence": {
            "observed_from": "node-b",
            "target_reported": "unavailable",
            "elapsed_seconds": 8,
            "surviving_members_operational": True,
        },
        "restore": {"restored_to": "active", "domain_member": True},
    }
    for key, value in (ops or {}).items():
        if isinstance(value, dict) and isinstance(operations.get(key), dict):
            operations[key] = {**operations[key], **value}
        else:
            operations[key] = value
    step_output: dict[str, Any] = {
        "success": True,
        "platform": "network",
        "target_node": "node-a",
        "operations": operations,
    }
    step_output.update(top)
    return {"step_output": step_output}


class TestImexNodeDepartureCheck:
    """Tests for ImexNodeDepartureCheck validation (SDN19-01)."""

    def test_all_passed(self) -> None:
        """A clean stop that peers noticed, with the domain surviving, passes."""
        result = ImexNodeDepartureCheck(config=_departure_output()).execute()
        assert result["passed"] is True
        assert "reported it unavailable after 8s" in result["output"]

    def test_stop_never_requested_fails(self) -> None:
        """Without a deliberate stop there is no departure to observe."""
        result = ImexNodeDepartureCheck(config=_departure_output({"stop": {"requested": False}})).execute()
        assert result["passed"] is False
        assert "never requested" in result["error"]

    def test_unclean_shutdown_fails(self) -> None:
        """The node must shut down cleanly, not merely stop."""
        result = ImexNodeDepartureCheck(config=_departure_output({"stop": {"clean_exit": False}})).execute()
        assert result["passed"] is False
        assert "did not shut down cleanly" in result["error"]

    def test_peers_never_noticed_reported_as_stale_membership(self) -> None:
        """A peer still reporting the target available is stale membership, and
        must be named as such rather than blamed on a fragile domain."""
        config = _departure_output({"peer_convergence": {"target_reported": "available"}})
        result = ImexNodeDepartureCheck(config=config).execute()
        assert result["passed"] is False
        assert "stale membership" in result["error"]
        assert "still reports it as available" in result["error"]

    def test_unknown_peer_view_also_fails_as_stale_membership(self) -> None:
        """A peer that cannot say is equally a convergence failure."""
        config = _departure_output({"peer_convergence": {"target_reported": "unknown"}})
        result = ImexNodeDepartureCheck(config=config).execute()
        assert result["passed"] is False
        assert "cannot say whether it is available" in result["error"]

    def test_domain_collapse_reported_distinctly_from_stale_membership(self) -> None:
        """Peers noticing but the domain falling over is a different defect - a
        fragile domain - and must not be reported as stale membership."""
        config = _departure_output({"peer_convergence": {"surviving_members_operational": False}})
        result = ImexNodeDepartureCheck(config=config).execute()
        assert result["passed"] is False
        assert "collapsed the domain" in result["error"]
        assert "stale membership" not in result["error"]

    @pytest.mark.parametrize("reported", ["down", "UNAVAILABLE", "", None, True])
    def test_unnormalized_peer_state_rejected(self, reported: Any) -> None:
        """The provider must normalize; the validation never string-matches raw
        vendor output, so anything outside the enum is rejected."""
        config = _departure_output({"peer_convergence": {"target_reported": reported}})
        result = ImexNodeDepartureCheck(config=config).execute()
        assert result["passed"] is False
        assert "target_reported" in result["error"]

    def test_convergence_beyond_bound_fails(self) -> None:
        """Noticing far too late fails when the wiring supplies a bound."""
        config = _departure_output({"peer_convergence": {"elapsed_seconds": 120}})
        config["convergence_timeout_seconds"] = 60
        result = ImexNodeDepartureCheck(config=config).execute()
        assert result["passed"] is False
        assert "beyond the 60s convergence bound" in result["error"]

    def test_convergence_within_bound_passes(self) -> None:
        """Noticing inside the bound passes."""
        config = _departure_output({"peer_convergence": {"elapsed_seconds": 5}})
        config["convergence_timeout_seconds"] = 60
        assert ImexNodeDepartureCheck(config=config).execute()["passed"] is True

    @pytest.mark.parametrize("elapsed", ["eight", None, True, -1])
    def test_invalid_elapsed_rejected(self, elapsed: Any) -> None:
        """elapsed_seconds must be a real non-negative number."""
        config = _departure_output({"peer_convergence": {"elapsed_seconds": elapsed}})
        result = ImexNodeDepartureCheck(config=config).execute()
        assert result["passed"] is False
        assert "elapsed_seconds" in result["error"]

    def test_missing_observer_rejected(self) -> None:
        """The observation has to name the surviving node it came from."""
        config = _departure_output({"peer_convergence": {"observed_from": ""}})
        result = ImexNodeDepartureCheck(config=config).execute()
        assert result["passed"] is False
        assert "observed_from" in result["error"]

    def test_missing_restore_evidence_fails(self) -> None:
        """Restoration is mandatory here, not best-effort: this check leaves the
        domain a member short, so passing without evidence it was put back would
        hide the damage."""
        config = _departure_output()
        del config["step_output"]["operations"]["restore"]
        result = ImexNodeDepartureCheck(config=config).execute()
        assert result["passed"] is False
        assert "no restoration evidence" in result["error"]

    def test_failed_restore_fails(self) -> None:
        """A node left inactive means the domain is still a member short."""
        config = _departure_output({"restore": {"restored_to": "failed"}})
        result = ImexNodeDepartureCheck(config=config).execute()
        assert result["passed"] is False
        assert "still a member short" in result["error"]

    def test_restore_losing_domain_membership_fails(self) -> None:
        """Restarting the service but not rejoining the domain is not restored."""
        config = _departure_output({"restore": {"restored_to": "active", "domain_member": False}})
        result = ImexNodeDepartureCheck(config=config).execute()
        assert result["passed"] is False
        assert "not an operational domain member" in result["error"]

    def test_malformed_operations_rejected(self) -> None:
        """A non-object operations value is rejected rather than raising."""
        result = ImexNodeDepartureCheck(config={"step_output": {"target_node": "n", "operations": "oops"}}).execute()
        assert result["passed"] is False
        assert "`operations`" in result["error"]

    def test_skipped_payload_skips_instead_of_failing(self) -> None:
        """An unconfigured run skips rather than failing the whole network run."""
        config = _departure_output(
            skipped=True, skip_reason="IMEX departure not configured for this run (no target set)"
        )
        with pytest.raises(pytest.skip.Exception, match="not configured"):
            ImexNodeDepartureCheck(config=config).execute()
