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

"""Kubernetes version support and control-plane patching checks (K8S02).

K8S02 asks three things of a managed Kubernetes offering: that it support the
three most recent upstream minors, that a new minor appear within 4-6 weeks of
its upstream release, and that control-plane security patching be automated.
None of that is visible from inside a cluster, so both checks read a provider
step for the claim and compare it against two things the provider does not
control - the upstream release index, fetched by its own step, and the version
the API server itself reports.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, ClassVar

import pytest

from isvtest.core.k8s import command_detail, get_kubectl_base_shell, parse_server_version
from isvtest.core.validation import BaseValidation

# How many of the most recent upstream minors have to be supported. Three is
# K8S02's "three most recent minor releases", i.e. a cluster may drift to N-2.
# Configurable per suite via ``supported_minor_count`` because the requirement
# has been written both ways.
DEFAULT_SUPPORTED_MINOR_COUNT = 3

# K8S02 allows 4-6 weeks for a new minor to appear; the check holds providers
# to the outer edge of that range so the grace is not itself the finding.
DEFAULT_AVAILABILITY_GRACE_DAYS = 42

# How far behind the newest upstream patch a control plane may sit before the
# patching automation is not keeping up with it.
DEFAULT_MAX_PATCH_LAG_DAYS = 30

# Leading ``X.Y`` or ``X.Y.Z`` of a version, ignoring a ``v`` prefix and any
# pre-release or build metadata that follows.
_VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?")


@dataclass(frozen=True)
class UpstreamCycle:
    """One upstream Kubernetes minor, as the release index describes it."""

    minor: tuple[int, int]
    released: date | None
    latest_patch: tuple[int, int, int] | None
    latest_patch_released: date | None

    @property
    def label(self) -> str:
        """Return the minor as ``X.Y``."""
        return _render_minor(self.minor)

    @property
    def latest_patch_label(self) -> str:
        """Return the newest patch as ``X.Y.Z``; only meaningful when ``latest_patch`` is set."""
        return "" if self.latest_patch is None else _render_patch(self.latest_patch)


class K8sSupportedMinorVersionsCheck(BaseValidation):
    """Verify the provider offers the three most recent upstream Kubernetes minors.

    The catalogue of versions a tenant can create or upgrade to is a property of
    the provider's control plane API, not of any one cluster, so the bound step
    reports it and this check compares it against the upstream release index.

    A minor released within ``availability_grace_days`` is not required yet -
    that is the 4-6 week window K8S02 allows for a new release to land - so a
    provider is only faulted for minors it has had time to pick up.

    The window is ``supported_minor_count`` minors wide - three by K8S02's
    wording, so a cluster may sit at N-2.

    The cluster under test is held to the same window. A catalogue listing a
    minor is not the same as running one, and a cloud certified on a minor
    upstream has already moved past does not evidence the support requirement -
    so a cluster outside the window fails even when the provider's catalogue is
    complete.

    Taking the catalogue at face value would also let a provider report an
    offering other than the one it runs, so it is corroborated against that
    same cluster: a catalogue that omits the in-window minor the tenant is
    demonstrably running is describing something else. That is what stops a
    provider from taking the grace window for a minor it is already serving.

    Step output:
        offered_versions: Kubernetes versions the provider offers, as ``X.Y`` or
            ``X.Y.Z``, with or without a leading ``v``.
    """

    description: ClassVar[str] = "Verify the provider offers the three most recent upstream Kubernetes minor releases."

    def run(self) -> None:
        """Compare the reported version catalogue against the upstream maintenance window."""
        step_output = _step_output(self, "Kubernetes version support")
        if step_output is None:
            return

        offered_raw = step_output.get("offered_versions")
        if not isinstance(offered_raw, list) or not offered_raw:
            self.set_failed(
                "Step output must report offered_versions as a non-empty list of Kubernetes versions, got "
                f"{offered_raw!r}"
            )
            return

        offered = {minor for minor in (_parse_minor(version) for version in offered_raw) if minor}
        if not offered:
            self.set_failed(f"No entry in offered_versions parses as a Kubernetes version: {offered_raw!r}")
            return

        minor_count = self._parse_positive_int("supported_minor_count", default=DEFAULT_SUPPORTED_MINOR_COUNT)
        if minor_count is None:
            return

        grace_days = self._parse_positive_int("availability_grace_days", default=DEFAULT_AVAILABILITY_GRACE_DAYS)
        if grace_days is None:
            return

        cycles = _upstream_cycles(self)
        window = cycles[:minor_count]

        today = datetime.now(UTC).date()
        missing: list[str] = []
        awaited: list[str] = []
        for cycle in window:
            if cycle.minor in offered:
                continue
            age = (today - cycle.released).days if cycle.released else None
            suffix = "" if age is None else f" (released {age} day(s) ago)"
            if age is not None and age <= grace_days:
                awaited.append(f"{cycle.label}{suffix}")
            else:
                missing.append(f"{cycle.label}{suffix}")

        if missing:
            self.set_failed(
                f"Provider does not offer {len(missing)} of the {len(window)} most recent upstream minor(s): "
                f"{', '.join(missing)} - offered: {_render_minors(offered)}"
            )
            return

        cluster_note = self._verify_cluster_version(offered, cycles, minor_count, grace_days)
        if cluster_note is None:
            return

        covered = [cycle.label for cycle in window if cycle.minor in offered]
        message = f"Provider offers the upstream maintenance window ({', '.join(covered)})"
        if awaited:
            message += f"; still within the {grace_days}-day availability window for {', '.join(awaited)}"
        if cluster_note:
            message += f"; {cluster_note}"
        self.set_passed(message)

    def _verify_cluster_version(
        self,
        offered: set[tuple[int, int]],
        cycles: list[UpstreamCycle],
        minor_count: int,
        grace_days: int,
    ) -> str | None:
        """Return a note for the message, or ``None`` when the cluster does not satisfy the requirement.

        How far behind the cluster is comes from its position in the upstream
        list: index 0 is the newest minor, so anything at or past
        ``minor_count`` is outside the window. A cloud is certified on the
        version it is certified on, and a cluster upstream has moved past does
        not evidence support for the window even when the provider's catalogue
        lists it.

        Falling out of the window is not instantaneous fault, though. A cluster
        sitting at the window's floor is pushed out the moment a new minor
        ships, through no act of its own, so it gets the same number of days to
        move as the provider gets to publish a new minor. The clock runs from
        the release of the minor whose arrival displaced it - at index 3 that
        is the newest minor, at index 4 the one before it - so the runway is
        granted once, not renewed by every subsequent release.

        An in-window cluster is also checked against the catalogue, which is
        what stops a provider from taking the availability grace for a minor it
        is demonstrably already serving.

        Marks the check failed and returns ``None`` when the cluster does not
        hold, or when the server version cannot be read - without it only the
        provider's own report is left, which is what this exists to avoid.
        """
        server = _server_version(self)
        if server is None:
            return None

        server_minor = server[:2]
        behind = next((index for index, cycle in enumerate(cycles) if cycle.minor == server_minor), None)
        if behind is None:
            self.set_failed(f"Cluster runs {_render_patch(server)}, a minor the upstream release index does not carry")
            return None

        if behind >= minor_count:
            return self._verify_displaced_cluster(server, cycles, behind, minor_count, grace_days)

        if server_minor not in offered:
            self.set_failed(
                f"Reported catalogue omits {_render_minor(server_minor)}, the in-window minor this cluster runs "
                f"({_render_patch(server)}), so it does not describe this offering - offered: {_render_minors(offered)}"
            )
            return None

        return ""

    def _verify_displaced_cluster(
        self,
        server: tuple[int, int, int],
        cycles: list[UpstreamCycle],
        behind: int,
        minor_count: int,
        grace_days: int,
    ) -> str | None:
        """Return a note when a cluster outside the window is still inside its runway to move."""
        window = ", ".join(cycle.label for cycle in cycles[:minor_count])
        displaced_by = cycles[behind - minor_count]
        age = (datetime.now(UTC).date() - displaced_by.released).days if displaced_by.released else None

        if age is not None and age <= grace_days:
            return (
                f"cluster runs {_render_patch(server)}, pushed out of the window {age} day(s) ago by "
                f"{displaced_by.label} and still inside the {grace_days}-day window to move"
            )

        since = "" if age is None else f" {age} day(s) ago"
        self.set_failed(
            f"Cluster runs {_render_patch(server)}, {behind} minor(s) behind upstream {cycles[0].label} and "
            f"outside the {minor_count} most recent ({window}) since {displaced_by.label} released{since}"
        )
        return None


class K8sAutomatedControlPlanePatchingCheck(BaseValidation):
    """Verify control-plane security patching is automated and has kept the cluster current.

    Whether patching is automated is a property of the provider's configuration,
    so the bound step reports it. A provider asserting its own compliance proves
    nothing on its own, so the claim is only accepted when the control plane
    shows the automation has acted: the API server's own version has to be at
    the newest patch upstream publishes for its minor, or no further behind it
    than ``max_patch_lag_days``.

    The version the API server reports is the ground truth throughout; the
    provider's ``current_version`` only sets an additional floor. That is
    one-sided on purpose - a provider naming a patch it does not deliver is
    caught, and a provider naming a low one gains nothing, because the
    substantive comparison is always against upstream. It also lets a provider
    report the patch it ships for the minor rather than a per-cluster reading,
    which is all some managed APIs expose, and still be held to delivering it.

    A control plane on a minor upstream no longer patches fails outright - there
    are no security patches arriving for it, automated or otherwise. Keeping the
    cluster on a supported minor in the first place is K8S02-01's subject.

    Downtime during patching is deliberately out of scope here; K8S09-01 covers
    control-plane updates without application downtime.

    Step output:
        automated_patching_enabled: Whether the provider patches the control
            plane without tenant action.
        current_version: Patch version the provider holds this control plane's
            minor at.
    """

    description: ClassVar[str] = (
        "Verify control-plane security patching is automated and the control plane runs a current upstream patch."
    )

    def run(self) -> None:
        """Corroborate the reported patching automation against the live control-plane version."""
        step_output = _step_output(self, "control-plane patching")
        if step_output is None:
            return

        enabled = step_output.get("automated_patching_enabled")
        if enabled is not True:
            self.set_failed(
                "Provider reports control-plane security patching is not automated "
                f"(automated_patching_enabled={enabled!r})"
            )
            return

        reported = _parse_patch(step_output.get("current_version"))
        if reported is None:
            self.set_failed(
                "Step output must report current_version as an X.Y.Z Kubernetes version, got "
                f"{step_output.get('current_version')!r}"
            )
            return

        server = _server_version(self)
        if server is None:
            return

        if server < reported:
            self.set_failed(
                f"Control plane has not received the provider's own current patch: the provider reports "
                f"{_render_patch(reported)} for this minor and the API server reports {_render_patch(server)}"
            )
            return

        cycle = next((cycle for cycle in _upstream_cycles(self) if cycle.minor == server[:2]), None)
        if cycle is None:
            self.set_failed(
                f"Control plane runs {_render_patch(server)}, a minor the upstream release index no longer "
                "carries, so no patching automation is keeping it secure"
            )
            return

        if cycle.latest_patch is None:
            pytest.skip(
                f"Upstream release index names no latest patch for {cycle.label}, so the control plane's "
                "patch level cannot be placed against it"
            )

        if server >= cycle.latest_patch:
            self.set_passed(
                f"Control-plane patching is automated and has landed {_render_patch(server)}, the newest "
                f"upstream patch for {cycle.label}"
            )
            return

        if cycle.latest_patch_released is None:
            pytest.skip(
                f"Upstream release index names no release date for {cycle.latest_patch_label}, so how far "
                f"behind it the control plane sits cannot be measured"
            )

        max_lag_days = self._parse_positive_int("max_patch_lag_days", default=DEFAULT_MAX_PATCH_LAG_DAYS)
        if max_lag_days is None:
            return

        lag_days = (datetime.now(UTC).date() - cycle.latest_patch_released).days
        if lag_days > max_lag_days:
            self.set_failed(
                f"Control plane runs {_render_patch(server)} while upstream published "
                f"{cycle.latest_patch_label} {lag_days} day(s) ago, more than the {max_lag_days}-day "
                "allowance - the patching automation is not keeping the control plane current"
            )
            return

        self.set_passed(
            f"Control-plane patching is automated; {_render_patch(server)} trails "
            f"{cycle.latest_patch_label} published {lag_days} day(s) ago, within the {max_lag_days}-day allowance"
        )


def _step_output(check: BaseValidation, subject: str) -> dict[str, Any] | None:
    """Return the bound step's output, or ``None`` after marking ``check`` failed."""
    step_output = check.config.get("step_output")
    if not isinstance(step_output, dict):
        check.set_failed(f"Missing step_output for {subject} validation")
        return None
    if step_output.get("success") is False:
        check.set_failed(str(step_output.get("error") or step_output.get("message") or f"{subject} step failed"))
        return None
    return step_output


def _upstream_cycles(check: BaseValidation) -> list[UpstreamCycle]:
    """Return the upstream release cycles newest minor first, skipping the check if absent.

    The cycles arrive as the JSON string the upstream index step emits, because
    step output reaches validation config through Jinja2 rendering. An empty
    list means that step is unwired or could not reach the index, and there is
    no reference to compare the provider against - an honest skip, not a pass.
    """
    raw = check.config.get("upstream_cycles")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else []
        except json.JSONDecodeError as exc:
            pytest.skip(f"Upstream Kubernetes release index is not valid JSON: {exc}")

    if not isinstance(raw, list) or not raw:
        pytest.skip(
            "Upstream Kubernetes release index is unavailable; wire the fetch_upstream_k8s_versions step so "
            "the offering can be compared against upstream"
        )

    cycles = [cycle for cycle in (_cycle(entry) for entry in raw) if cycle]
    if not cycles:
        pytest.skip(f"Upstream Kubernetes release index named no usable release cycles: {raw!r}")

    cycles.sort(key=lambda cycle: cycle.minor, reverse=True)
    return cycles


def _cycle(entry: Any) -> UpstreamCycle | None:
    """Return one release cycle, or ``None`` if the entry does not name a minor."""
    if not isinstance(entry, dict):
        return None
    label = str(entry.get("minor") or "").strip()
    minor = _parse_minor(label)
    if minor is None:
        return None
    return UpstreamCycle(
        minor=minor,
        released=_parse_date(entry.get("released")),
        latest_patch=_parse_patch(entry.get("latest_patch")),
        latest_patch_released=_parse_date(entry.get("latest_patch_released")),
    )


def _server_version(check: BaseValidation) -> tuple[int, int, int] | None:
    """Return the API server's version, or ``None`` after marking ``check`` failed."""
    result = check.run_command(get_kubectl_base_shell("version", "-o", "json"))
    if result.exit_code != 0:
        check.set_failed(f"Could not read the Kubernetes server version: {command_detail(result)}")
        return None

    server = _parse_patch(parse_server_version(result.stdout))
    if server is None:
        check.set_failed("kubectl reported no parseable Kubernetes server version")
        return None
    return server


def _parse_minor(value: Any) -> tuple[int, int] | None:
    """Return the ``(major, minor)`` of an ``X.Y`` or ``X.Y.Z`` version, or ``None``."""
    match = _version_match(value)
    return (int(match[1]), int(match[2])) if match else None


def _parse_patch(value: Any) -> tuple[int, int, int] | None:
    """Return the ``(major, minor, patch)`` of an ``X.Y.Z`` version, or ``None``."""
    match = _version_match(value)
    return (int(match[1]), int(match[2]), int(match[3])) if match and match[3] else None


def _version_match(value: Any) -> re.Match[str] | None:
    """Return the version match for a version string, or ``None``.

    Accepts an optional ``v`` prefix and ignores any pre-release or build
    metadata a provider appends (``1.34.1+isv.2``), so a version that names a
    real release is not rejected for how it is decorated.
    """
    if not isinstance(value, str):
        return None
    return _VERSION_PATTERN.match(value.strip())


def _parse_date(value: Any) -> date | None:
    """Return an ISO ``YYYY-MM-DD`` date, or ``None`` if it is absent or malformed."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _render_minor(minor: tuple[int, int]) -> str:
    """Return a ``X.Y`` minor as text."""
    return f"{minor[0]}.{minor[1]}"


def _render_patch(version: tuple[int, int, int]) -> str:
    """Return an ``X.Y.Z`` version as text."""
    return f"{version[0]}.{version[1]}.{version[2]}"


def _render_minors(minors: set[tuple[int, int]]) -> str:
    """Return a set of minors as a newest-first comma-separated list."""
    return ", ".join(_render_minor(minor) for minor in sorted(minors, reverse=True))
