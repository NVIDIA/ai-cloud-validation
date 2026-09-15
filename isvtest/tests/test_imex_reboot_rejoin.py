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

"""Tests for ImexRebootRejoinCheck (SDN20-01)."""

from __future__ import annotations

from typing import Any

import pytest

from isvtest.validations.network import ImexRebootRejoinCheck


def _reboot_output(post: dict[str, Any] | None = None, **top: Any) -> dict[str, Any]:
    """Build a step_output dict for IMEX reboot rejoin tests."""
    step_output: dict[str, Any] = {
        "success": True,
        "platform": "network",
        "node_id": "node-a",
        "persistence_configured": True,
        "reboot_confirmed": True,
        "uptime_seconds": 94,
        "post_reboot": {
            "service_ready": True,
            "domain_member": True,
            "elapsed_seconds": 61,
            "intervention_required": False,
        },
    }
    if post is not None:
        step_output["post_reboot"] = {**step_output["post_reboot"], **post}
    step_output.update(top)
    return {"step_output": step_output}


class TestImexRebootRejoinCheck:
    """Tests for ImexRebootRejoinCheck validation (SDN20-01)."""

    def test_all_passed(self) -> None:
        """A confirmed reboot with IMEX back and rejoined unassisted passes."""
        result = ImexRebootRejoinCheck(config=_reboot_output()).execute()
        assert result["passed"] is True
        assert "rejoined its domain unassisted after 61s" in result["output"]

    def test_unconfirmed_reboot_fails_first(self) -> None:
        """A run that cannot prove the node went down proves nothing about what
        happens when it comes back, so this is checked before anything else."""
        config = _reboot_output(reboot_confirmed=False)
        result = ImexRebootRejoinCheck(config=config).execute()
        assert result["passed"] is False
        assert "not affirmatively confirmed" in result["error"]
        assert "reachability is not evidence" in result["error"]

    def test_unconfirmed_reboot_fails_even_when_everything_else_looks_healthy(self) -> None:
        """A node that never rebooted has IMEX running and is a domain member -
        exactly the state that would pass if the reboot were inferred from
        reachability. It must still fail."""
        config = _reboot_output(reboot_confirmed=False)
        assert config["step_output"]["post_reboot"]["service_ready"] is True
        assert config["step_output"]["post_reboot"]["domain_member"] is True
        result = ImexRebootRejoinCheck(config=config).execute()
        assert result["passed"] is False
        assert "not affirmatively confirmed" in result["error"]

    def test_service_not_back_fails(self) -> None:
        """IMEX not returning after boot fails, with elapsed time reported."""
        config = _reboot_output({"service_ready": False})
        result = ImexRebootRejoinCheck(config=config).execute()
        assert result["passed"] is False
        assert "did not come back after the reboot within 61s" in result["error"]

    def test_not_enabled_at_boot_fails_even_when_everything_else_is_healthy(self) -> None:
        """The strict pass condition, asserted affirmatively rather than only as
        an explanation for a service that failed to return.

        A node running IMEX while not enabled at boot may only be up because
        something else started it, so healthy post-reboot evidence must not be
        enough to pass.
        """
        config = _reboot_output(persistence_configured=False)
        assert config["step_output"]["post_reboot"]["service_ready"] is True
        assert config["step_output"]["post_reboot"]["domain_member"] is True
        result = ImexRebootRejoinCheck(config=config).execute()
        assert result["passed"] is False
        assert "not configured to start at boot" in result["error"]
        assert "provider obligation" in result["error"]

    def test_not_enabled_at_boot_fails_when_service_also_absent(self) -> None:
        """The same verdict when the service did not come back either."""
        config = _reboot_output({"service_ready": False}, persistence_configured=False)
        result = ImexRebootRejoinCheck(config=config).execute()
        assert result["passed"] is False
        assert "not configured to start at boot" in result["error"]

    def test_service_back_but_not_rejoined_fails(self) -> None:
        """Coming back without rejoining the domain is a distinct failure."""
        config = _reboot_output({"domain_member": False})
        result = ImexRebootRejoinCheck(config=config).execute()
        assert result["passed"] is False
        assert "had not rejoined its domain after 61s" in result["error"]

    def test_intervention_required_fails(self) -> None:
        """A return that needed a human is not an unassisted return."""
        config = _reboot_output({"intervention_required": True})
        result = ImexRebootRejoinCheck(config=config).execute()
        assert result["passed"] is False
        assert "needed intervention" in result["error"]

    def test_rejoin_beyond_bound_fails(self) -> None:
        """Rejoining far too late fails when the wiring supplies a bound, so a
        platform that technically recovers but takes far too long is visible."""
        config = _reboot_output({"elapsed_seconds": 1200})
        config["rejoin_timeout_seconds"] = 900
        result = ImexRebootRejoinCheck(config=config).execute()
        assert result["passed"] is False
        assert "beyond the 900s bound" in result["error"]

    def test_rejoin_within_bound_passes(self) -> None:
        """A slow but in-bound rejoin still passes."""
        config = _reboot_output({"elapsed_seconds": 800})
        config["rejoin_timeout_seconds"] = 900
        assert ImexRebootRejoinCheck(config=config).execute()["passed"] is True

    @pytest.mark.parametrize("uptime", ["ninety", None, True, -1])
    def test_invalid_uptime_rejected(self, uptime: Any) -> None:
        """uptime_seconds must be a real non-negative number."""
        result = ImexRebootRejoinCheck(config=_reboot_output(uptime_seconds=uptime)).execute()
        assert result["passed"] is False
        assert "uptime_seconds" in result["error"]

    @pytest.mark.parametrize("elapsed", ["sixty", None, True, -1])
    def test_invalid_elapsed_rejected(self, elapsed: Any) -> None:
        """elapsed_seconds must be a real non-negative number."""
        result = ImexRebootRejoinCheck(config=_reboot_output({"elapsed_seconds": elapsed})).execute()
        assert result["passed"] is False
        assert "elapsed_seconds" in result["error"]

    def test_missing_node_id_rejected(self) -> None:
        """The report has to name the node that was rebooted."""
        result = ImexRebootRejoinCheck(config=_reboot_output(node_id="")).execute()
        assert result["passed"] is False
        assert "node_id" in result["error"]

    def test_malformed_post_reboot_rejected(self) -> None:
        """A non-object post_reboot value is rejected rather than raising."""
        result = ImexRebootRejoinCheck(
            config={
                "step_output": {"node_id": "n", "reboot_confirmed": True, "uptime_seconds": 1, "post_reboot": "oops"}
            }
        ).execute()
        assert result["passed"] is False
        assert "`post_reboot`" in result["error"]

    def test_skipped_payload_skips_instead_of_failing(self) -> None:
        """An unconfigured run skips rather than failing the whole network run."""
        config = _reboot_output(skipped=True, skip_reason="IMEX reboot node not configured for this run")
        with pytest.raises(pytest.skip.Exception, match="not configured"):
            ImexRebootRejoinCheck(config=config).execute()

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_timing_rejected(self, bad: float) -> None:
        """NaN slips through every obvious guard: json.loads accepts the JSON
        literal NaN, jsonschema treats it as a valid number whose `minimum` does
        not reject it, and both `nan < 0` and `nan > bound` are False - so a NaN
        duration would satisfy a range check and a timeout check alike."""
        config = _reboot_output({"elapsed_seconds": bad})
        config["rejoin_timeout_seconds"] = 900
        result = ImexRebootRejoinCheck(config=config).execute()
        assert result["passed"] is False
        assert "elapsed_seconds" in result["error"]

    @pytest.mark.parametrize("bad", [float("nan"), float("inf")])
    def test_non_finite_uptime_rejected(self, bad: float) -> None:
        """Same guard on the uptime evidence."""
        result = ImexRebootRejoinCheck(config=_reboot_output(uptime_seconds=bad)).execute()
        assert result["passed"] is False
        assert "uptime_seconds" in result["error"]
