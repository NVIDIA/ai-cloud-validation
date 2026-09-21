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

"""Tests for the Kubernetes version support and control-plane patching checks (K8S02)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from isvtest.core.runners import CommandResult
from isvtest.validations.k8s_versions import (
    K8sAutomatedControlPlanePatchingCheck,
    K8sSupportedMinorVersionsCheck,
)

VERSION_COMMAND = "kubectl version -o json"


def _days_ago(days: int) -> str:
    """Return the ISO date ``days`` before today, as the upstream index formats it."""
    return (datetime.now(UTC).date() - timedelta(days=days)).isoformat()


def _cycle(minor: str, *, released: int, latest_patch: str, patch_released: int) -> dict[str, str]:
    """Return one upstream release cycle, dating it relative to today."""
    return {
        "minor": minor,
        "released": _days_ago(released),
        "latest_patch": latest_patch,
        "latest_patch_released": _days_ago(patch_released),
    }


def _cycles(*overrides: dict[str, str]) -> str:
    """Return the upstream release index as the fetch step encodes it.

    Defaults to a settled window - three minors all released long enough ago
    that the availability grace does not apply - so a test that cares about
    the grace states it.
    """
    cycles = list(overrides) or [
        _cycle("1.34", released=120, latest_patch="1.34.1", patch_released=10),
        _cycle("1.33", released=240, latest_patch="1.33.5", patch_released=10),
        _cycle("1.32", released=360, latest_patch="1.32.9", patch_released=10),
        _cycle("1.31", released=480, latest_patch="1.31.12", patch_released=10),
    ]
    return json.dumps(cycles)


def _server(version: str) -> CommandResult:
    """Return a ``kubectl version`` result reporting ``version`` as the server version."""
    payload = json.dumps({"serverVersion": {"gitVersion": version}})
    return CommandResult(exit_code=0, stdout=payload, stderr="", duration=0.0)


def _run(check: Any, version: CommandResult | None = None) -> list[str]:
    """Run ``check``, answering its ``kubectl version`` probe, and return the commands it ran."""
    answer = version if version is not None else _server("v1.34.1")
    with (
        patch(
            "isvtest.validations.k8s_versions.get_kubectl_base_shell",
            side_effect=lambda *args: " ".join(("kubectl", *args)),
        ),
        patch.object(check, "run_command", side_effect=lambda command, **_: answer) as mock_run,
    ):
        check.run()
    return [call[0][0] for call in mock_run.call_args_list]


# ---------------------------------------------------------------------------
# K8S02-01 - supported minor versions
# ---------------------------------------------------------------------------


def _offered(*versions: str, **overrides: Any) -> dict[str, Any]:
    """Return a version-catalogue step output offering ``versions``."""
    output: dict[str, Any] = {
        "success": True,
        "platform": "kubernetes",
        "offered_versions": list(versions) or ["1.34.1", "1.33.5", "1.32.9"],
    }
    output.update(overrides)
    return output


def _minors_check(step_output: Any = None, cycles: str | None = None, **config: Any) -> K8sSupportedMinorVersionsCheck:
    """Return a K8S02-01 check wired to ``step_output`` and the upstream index."""
    return K8sSupportedMinorVersionsCheck(
        config={
            "step_output": _offered() if step_output is None else step_output,
            "upstream_cycles": _cycles() if cycles is None else cycles,
            **config,
        }
    )


def test_passes_when_the_catalogue_covers_the_maintenance_window() -> None:
    """Offering the three most recent upstream minors satisfies K8S02-01."""
    check = _minors_check()
    commands = _run(check)

    assert check.passed, check.message
    assert "1.34, 1.33, 1.32" in check.message
    assert commands == [VERSION_COMMAND]


def test_passes_when_versions_carry_a_v_prefix_or_no_patch() -> None:
    """The catalogue is read by minor, however a provider spells its versions."""
    check = _minors_check(_offered("v1.34", "v1.33.5", "1.32"))
    _run(check)

    assert check.passed, check.message


def test_fails_when_a_settled_minor_is_missing_from_the_catalogue() -> None:
    """A minor upstream released long ago and still unoffered is the K8S02-01 failure."""
    check = _minors_check(_offered("1.34.1", "1.33.5"))
    _run(check)

    assert not check.passed
    assert "1 of the 3 most recent upstream minor(s): 1.32" in check.message


def test_passes_when_the_newest_minor_is_still_inside_the_grace_window() -> None:
    """A minor released days ago is not yet required - K8S02 allows 4-6 weeks."""
    cycles = _cycles(
        _cycle("1.35", released=10, latest_patch="1.35.0", patch_released=10),
        _cycle("1.34", released=120, latest_patch="1.34.1", patch_released=10),
        _cycle("1.33", released=240, latest_patch="1.33.5", patch_released=10),
    )
    check = _minors_check(_offered("1.34.1", "1.33.5"), cycles=cycles)
    _run(check)

    assert check.passed, check.message
    assert "still within the 42-day availability window for 1.35" in check.message


def test_fails_when_the_newest_minor_has_outlived_the_grace_window() -> None:
    """Past the availability window an unoffered minor is no longer excused."""
    cycles = _cycles(
        _cycle("1.35", released=60, latest_patch="1.35.2", patch_released=10),
        _cycle("1.34", released=180, latest_patch="1.34.1", patch_released=10),
        _cycle("1.33", released=300, latest_patch="1.33.5", patch_released=10),
    )
    check = _minors_check(_offered("1.34.1", "1.33.5"), cycles=cycles)
    _run(check)

    assert not check.passed
    assert "1.35 (released 60 day(s) ago)" in check.message


def test_fails_when_the_catalogue_omits_the_in_window_minor_the_cluster_runs() -> None:
    """A provider already serving a minor cannot take the grace window for not offering it."""
    cycles = _cycles(
        _cycle("1.34", released=10, latest_patch="1.34.1", patch_released=10),
        _cycle("1.33", released=240, latest_patch="1.33.5", patch_released=10),
        _cycle("1.32", released=360, latest_patch="1.32.9", patch_released=10),
    )
    check = _minors_check(_offered("1.33.5", "1.32.9"), cycles=cycles)
    _run(check)

    assert not check.passed
    assert "omits 1.34, the in-window minor this cluster runs (1.34.1)" in check.message


def test_fails_when_the_cluster_fell_out_of_the_window_long_ago() -> None:
    """A complete catalogue does not excuse certifying on a minor upstream has moved past."""
    check = _minors_check()
    _run(check, version=_server("v1.31.12"))

    assert not check.passed
    assert "Cluster runs 1.31.12, 3 minor(s) behind upstream 1.34" in check.message
    assert "outside the 3 most recent (1.34, 1.33, 1.32) since 1.34 released 120 day(s) ago" in check.message


def test_passes_when_a_new_minor_has_only_just_pushed_the_cluster_out() -> None:
    """A cluster at the window floor gets runway to move when the next minor ships."""
    cycles = _cycles(
        _cycle("1.35", released=5, latest_patch="1.35.0", patch_released=5),
        _cycle("1.34", released=120, latest_patch="1.34.1", patch_released=10),
        _cycle("1.33", released=240, latest_patch="1.33.5", patch_released=10),
        _cycle("1.32", released=360, latest_patch="1.32.9", patch_released=10),
    )
    offered = _offered("1.35.0", "1.34.1", "1.33.5", "1.32.9")
    check = _minors_check(offered, cycles=cycles)
    _run(check, version=_server("v1.32.9"))

    assert check.passed, check.message
    assert "pushed out of the window 5 day(s) ago by 1.35" in check.message
    assert "still inside the 42-day window to move" in check.message


def test_supported_minor_count_narrows_the_window() -> None:
    """The same N-2 cluster passes a 3-wide window and fails a 2-wide one."""
    at_n_minus_2 = _server("v1.32.9")

    allowed = _minors_check()
    _run(allowed, version=at_n_minus_2)
    assert allowed.passed, allowed.message

    narrowed = _minors_check(supported_minor_count=2)
    _run(narrowed, version=at_n_minus_2)
    assert not narrowed.passed
    assert "Cluster runs 1.32.9, 2 minor(s) behind upstream 1.34" in narrowed.message
    assert "outside the 2 most recent (1.34, 1.33)" in narrowed.message


def test_the_runway_is_not_renewed_by_a_later_release() -> None:
    """Two minors past the floor the clock runs from the older displacing release, not the newest.

    A brand-new minor at the top must not hand fresh runway to a cluster that
    already blew the window when the previous minor shipped.
    """
    cycles = _cycles(
        _cycle("1.36", released=5, latest_patch="1.36.0", patch_released=5),
        _cycle("1.35", released=130, latest_patch="1.35.0", patch_released=5),
        _cycle("1.34", released=250, latest_patch="1.34.1", patch_released=10),
        _cycle("1.33", released=370, latest_patch="1.33.5", patch_released=10),
        _cycle("1.32", released=490, latest_patch="1.32.9", patch_released=10),
    )
    offered = _offered("1.36.0", "1.35.0", "1.34.1", "1.33.5", "1.32.9")
    check = _minors_check(offered, cycles=cycles)
    _run(check, version=_server("v1.32.9"))

    assert not check.passed
    assert "Cluster runs 1.32.9, 4 minor(s) behind upstream 1.36" in check.message
    assert "since 1.35 released 130 day(s) ago" in check.message


def test_fails_when_the_server_version_cannot_be_read() -> None:
    """Without the independent half only the provider's own report is left."""
    unreadable = CommandResult(exit_code=1, stdout="", stderr="connection refused", duration=0.0)
    check = _minors_check()
    _run(check, version=unreadable)

    assert not check.passed
    assert "Could not read the Kubernetes server version" in check.message


@pytest.mark.parametrize("offered_versions", [[], "1.34.1", ["not-a-version"]])
def test_fails_when_the_catalogue_is_missing_or_unparseable(offered_versions: Any) -> None:
    """A step that reports no usable catalogue fails rather than passing vacuously."""
    check = _minors_check(_offered(offered_versions=offered_versions))
    _run(check)

    assert not check.passed


def test_fails_when_the_catalogue_step_failed() -> None:
    """A failed provider step is reported, not worked around."""
    check = _minors_check({"success": False, "error": "catalogue API returned 503"})
    _run(check)

    assert not check.passed
    assert "catalogue API returned 503" in check.message


def test_skips_when_the_upstream_index_is_unavailable_for_the_catalogue() -> None:
    """With no upstream reference there is nothing to compare against - skip, never pass."""
    with pytest.raises(BaseException, match="Upstream Kubernetes release index is unavailable"):
        _run(_minors_check(cycles=""))


# ---------------------------------------------------------------------------
# K8S02-02 - automated control-plane patching
# ---------------------------------------------------------------------------


def _patching(**overrides: Any) -> dict[str, Any]:
    """Return a control-plane patching step output claiming automation is on."""
    output: dict[str, Any] = {
        "success": True,
        "platform": "kubernetes",
        "automated_patching_enabled": True,
        "current_version": "1.34.1",
    }
    output.update(overrides)
    return output


def _patching_check(
    step_output: Any = None,
    cycles: str | None = None,
    **config: Any,
) -> K8sAutomatedControlPlanePatchingCheck:
    """Return a K8S02-02 check wired to ``step_output`` and the upstream index."""
    return K8sAutomatedControlPlanePatchingCheck(
        config={
            "step_output": _patching() if step_output is None else step_output,
            "upstream_cycles": _cycles() if cycles is None else cycles,
            **config,
        }
    )


def test_passes_when_the_control_plane_runs_the_newest_upstream_patch() -> None:
    """Automation that has landed the newest patch is the clean K8S02-02 pass."""
    check = _patching_check()
    commands = _run(check)

    assert check.passed, check.message
    assert "newest upstream patch for 1.34" in check.message
    assert commands == [VERSION_COMMAND]


def test_passes_when_the_control_plane_is_ahead_of_the_indexed_patch() -> None:
    """A control plane past the indexed patch is current, not behind it."""
    check = _patching_check(_patching(current_version="1.34.3"))
    _run(check, version=_server("v1.34.3"))

    assert check.passed, check.message


def test_passes_when_a_recent_patch_has_not_landed_yet() -> None:
    """A patch published days ago is inside the allowance, so automation is not faulted."""
    cycles = _cycles(_cycle("1.34", released=120, latest_patch="1.34.2", patch_released=5))
    check = _patching_check(cycles=cycles)
    _run(check)

    assert check.passed, check.message
    assert "trails 1.34.2 published 5 day(s) ago" in check.message


def test_fails_when_the_control_plane_has_missed_a_patch_past_the_allowance() -> None:
    """Sitting behind a long-published patch is automation that is not keeping up."""
    cycles = _cycles(_cycle("1.34", released=200, latest_patch="1.34.2", patch_released=90))
    check = _patching_check(cycles=cycles)
    _run(check)

    assert not check.passed
    assert "upstream published 1.34.2 90 day(s) ago" in check.message
    assert "30-day allowance" in check.message


def test_fails_when_the_provider_reports_patching_is_not_automated() -> None:
    """The requirement is automation, so a manual process fails outright."""
    check = _patching_check(_patching(automated_patching_enabled=False))
    commands = _run(check)

    assert not check.passed
    assert "not automated" in check.message
    assert commands == []


def test_fails_when_the_control_plane_is_behind_the_provider_s_own_patch() -> None:
    """A patch the provider names but has not delivered to this cluster is a real finding."""
    check = _patching_check(_patching(current_version="1.34.9"))
    _run(check)

    assert not check.passed
    assert "has not received the provider's own current patch" in check.message
    assert "reports 1.34.9 for this minor and the API server reports 1.34.1" in check.message


def test_passes_when_the_api_server_is_ahead_of_the_reported_patch() -> None:
    """The provider's figure is only a floor; the API server's version is the ground truth."""
    cycles = _cycles(_cycle("1.34", released=120, latest_patch="1.34.1", patch_released=10))
    check = _patching_check(_patching(current_version="1.34.0"), cycles=cycles)
    _run(check)

    assert check.passed, check.message


def test_fails_when_the_control_plane_runs_a_minor_upstream_no_longer_patches() -> None:
    """No upstream patches exist for a retired minor, so nothing can be landing."""
    cycles = _cycles(_cycle("1.34", released=120, latest_patch="1.34.1", patch_released=10))
    check = _patching_check(_patching(current_version="1.28.4"), cycles=cycles)
    _run(check, version=_server("v1.28.4"))

    assert not check.passed
    assert "the upstream release index no longer carries" in check.message


def test_fails_when_the_reported_version_is_not_a_patch_version() -> None:
    """The comparison needs a patch level, so a bare minor is a broken contract."""
    check = _patching_check(_patching(current_version="1.34"))
    _run(check)

    assert not check.passed
    assert "must report current_version as an X.Y.Z" in check.message


def test_skips_when_the_index_cannot_date_the_newest_patch() -> None:
    """An index missing the release date cannot measure lag - skip rather than pass."""
    cycles = json.dumps([{"minor": "1.34", "latest_patch": "1.34.2"}])
    with pytest.raises(BaseException, match="names no release date"):
        _run(_patching_check(cycles=cycles))


def test_skips_when_the_upstream_index_is_unavailable_for_patching() -> None:
    """The automation claim is never accepted on the provider's word alone."""
    with pytest.raises(BaseException, match="Upstream Kubernetes release index is unavailable"):
        _run(_patching_check(cycles=""))
