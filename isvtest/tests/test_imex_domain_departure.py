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

"""Tests for the deliberate node departure validation (SDN19-02).

The check shrinks a compute domain, watches the departing daemon stop, asks a
surviving daemon what it now reports about that node, and puts it back. Every
test here answers its kubectl calls from a canned cluster that reacts to the
shrink and to the restore, against a clock that advances only when the check
sleeps.
"""

from __future__ import annotations

import json
import shlex
from typing import Any
from unittest.mock import patch

import pytest

from isvtest.core.runners import CommandResult
from isvtest.validations.network import ImexDomainDepartureCheck

DOMAIN_UID = "cd-uid-0002"
DRIVER_NAMESPACE = "nvidia-dra-driver-gpu"


def _ok(stdout: str = "", stderr: str = "") -> CommandResult:
    """Return a successful ``CommandResult``."""
    return CommandResult(exit_code=0, stdout=stdout, stderr=stderr, duration=0.0)


def _fail(stdout: str = "", stderr: str = "", exit_code: int = 1) -> CommandResult:
    """Return a failed ``CommandResult``."""
    return CommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr, duration=0.0)


def _address(node: str) -> str:
    """Return the canned address the domain publishes for ``node``."""
    return f"10.0.0.{node.rsplit('-', 1)[-1]}"


class _Clock:
    """A monotonic clock that only advances when the check sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        """Return the current time."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Advance the clock by ``seconds`` without waiting."""
        self.now += seconds


class _Cluster:
    """A canned cluster that reacts to a compute domain being shrunk.

    Models what a static fixture cannot: withdrawing one node's channel claim
    stops that node's daemon, the surviving daemon's report of its peers
    catches up some polls later, and reinstating the claim brings the node
    back. Timings, how the daemon went, and what the surviving daemon says are
    all knobs, because each is a defect the check has to tell apart.
    """

    def __init__(
        self,
        *,
        nodes: tuple[str, ...] = ("gpu-1", "gpu-2"),
        mode: str | None = "driverManaged",
        controller: bool = True,
        addressed: bool = True,
        departure_polls: int = 1,
        notice_polls: int = 0,
        peer_still_connected: bool = False,
        notice_as_invalid: bool = False,
        exit_code: int = 0,
        baseline_restarts: int = 0,
        shutdown_restarts: int = 0,
        daemon_never_stops: bool = False,
        reschedule_after_departure: bool = False,
        broken_survivor: str | None = None,
        keep_departed_member: bool = False,
        domain_state: str = "UP",
        observer_status: str = "READY",
        restore_polls: int = 0,
        api_resources: CommandResult | None = None,
        deployments: CommandResult | None = None,
        apply_result: CommandResult | None = None,
        patch_result: CommandResult | None = None,
        restore_patch_result: CommandResult | None = None,
        exec_result: CommandResult | None = None,
        delete_domain_result: CommandResult | None = None,
    ) -> None:
        self.nodes = nodes
        self.mode = mode
        self.controller = controller
        self.addressed = addressed
        self.departure_polls = departure_polls
        self.notice_polls = notice_polls
        self.peer_still_connected = peer_still_connected
        self.notice_as_invalid = notice_as_invalid
        self.exit_code = exit_code
        self.baseline_restarts = baseline_restarts
        self.shutdown_restarts = shutdown_restarts
        self.daemon_never_stops = daemon_never_stops
        self.reschedule_after_departure = reschedule_after_departure
        self.broken_survivor = broken_survivor
        self.keep_departed_member = keep_departed_member
        self.domain_state = domain_state
        self.observer_status = observer_status
        self.restore_polls = restore_polls
        self.api_resources = api_resources
        self.deployments = deployments
        self.apply_result = apply_result
        self.patch_result = patch_result
        self.restore_patch_result = restore_patch_result
        self.exec_result = exec_result
        self.delete_domain_result = delete_domain_result

        self.commands: list[str] = []
        self.patches: list[dict[str, Any]] = []
        self.allocated = False
        self.released = False
        self.excluded: str | None = None
        self._polls = 0
        self._shrunk_at: int | None = None
        self._restored_at: int | None = None

    # -- cluster state ----------------------------------------------------

    @property
    def _since_shrink(self) -> int:
        """Return how many domain reads have happened since the shrink."""
        return 0 if self._shrunk_at is None else self._polls - self._shrunk_at

    @property
    def _since_restore(self) -> int:
        """Return how many domain reads have happened since the restore."""
        return 0 if self._restored_at is None else self._polls - self._restored_at

    @property
    def _departed(self) -> str | None:
        """Return the node currently out of the domain, if any."""
        if self._shrunk_at is None or self._restored_at is not None:
            return None
        return self.excluded

    def _target_daemon_state(self, node: str) -> str:
        """Return what the departing node's daemon is doing: running/going/gone."""
        if self._shrunk_at is None:
            return "running"
        if self._restored_at is not None:
            return "running" if self._since_restore > self.restore_polls else "gone"
        if self.daemon_never_stops:
            return "running"
        if self._since_shrink <= self.departure_polls:
            return "going"
        # A driver that puts one back does so after the first had gone, which
        # is a different fault from one that never stopped it at all.
        if self.reschedule_after_departure and self._since_shrink > self.departure_polls + 1:
            return "running"
        return "gone"

    def _member_status(self, node: str) -> str:
        """Return the readiness the domain reports for ``node``."""
        if node == self.broken_survivor and self._departed is not None:
            return "NotReady"
        if node == self.excluded and self._departed is not None:
            return "Ready"
        return "Ready"

    def _members(self) -> list[str]:
        """Return the nodes the domain still accounts for."""
        departed = self._departed
        if departed is None or self.keep_departed_member:
            return list(self.nodes)
        if self._target_daemon_state(departed) == "gone":
            return [node for node in self.nodes if node != departed]
        return list(self.nodes)

    def _daemon(self, node: str) -> dict[str, Any] | None:
        """Return the domain's daemon pod on ``node``, or None when it has none."""
        if node != self.excluded or self._shrunk_at is None:
            return _pod(node, ready=True, restarts=self.baseline_restarts)

        state = self._target_daemon_state(node)
        if state == "gone":
            return None
        if state == "going":
            return _pod(
                node,
                ready=False,
                restarts=self.baseline_restarts + self.shutdown_restarts,
                terminating=True,
                exit_code=self.exit_code,
            )
        return _pod(node, ready=True, restarts=self.baseline_restarts, uid_suffix="replacement")

    # -- kubectl dispatch -------------------------------------------------

    def __call__(self, command: str, **_: Any) -> CommandResult:
        """Answer one kubectl invocation."""
        self.commands.append(command)

        if "api-resources" in command:
            return self.api_resources if self.api_resources is not None else _ok("computedomains.resource.nvidia.com\n")
        if command.startswith("kubectl get deployments"):
            return self.deployments if self.deployments is not None else _ok(self._deployments())
        if command.startswith("printf"):
            if "kind: ComputeDomain" in command:
                self.allocated = True
            return self.apply_result if self.apply_result is not None else _ok()
        if command.startswith("kubectl patch daemonset"):
            return self._patch(command)
        if command.startswith("kubectl get computedomains"):
            self._polls += 1
            return _ok(self._domain())
        if command.startswith("kubectl get pods"):
            return _ok(self._pods())
        if command.startswith("kubectl exec"):
            return self.exec_result if self.exec_result is not None else _ok(self._imex_report())
        if command.startswith("kubectl delete daemonset"):
            return _ok()
        if command.startswith("kubectl delete computedomains"):
            self.released = True
            return self.delete_domain_result if self.delete_domain_result is not None else _ok()
        raise AssertionError(f"unexpected command: {command}")

    def _patch(self, command: str) -> CommandResult:
        """Apply a claim-scope patch, recording which nodes it now selects."""
        payload = json.loads(shlex.split(command)[-1])
        self.patches.append(payload)
        term = payload["spec"]["template"]["spec"]["affinity"]["nodeAffinity"][
            "requiredDuringSchedulingIgnoredDuringExecution"
        ]["nodeSelectorTerms"][0]
        fields = term.get("matchFields")
        if fields:
            if self.patch_result is not None:
                return self.patch_result
            self.excluded = fields[0]["values"][0]
            self._shrunk_at = self._polls
            return _ok()
        if self.restore_patch_result is not None:
            return self.restore_patch_result
        self._restored_at = self._polls
        return _ok()

    def _deployments(self) -> str:
        """Return the deployment listing that declares the ownership mode, as JSON."""
        if not self.controller:
            return json.dumps({"items": []})
        env = [{"name": "IMEX_MODE", "value": self.mode}] if self.mode is not None else []
        container = {"name": "compute-domain", "command": ["compute-domain-controller", "-v", "6"], "env": env}
        deployment = {
            "metadata": {"name": "dra-driver-controller", "namespace": DRIVER_NAMESPACE},
            "spec": {"template": {"spec": {"containers": [container]}}},
        }
        return json.dumps({"items": [deployment]})

    def _domain(self) -> str:
        """Return the compute domain and its per-node membership, as JSON."""
        nodes = [
            {
                "name": node,
                "cliqueID": "fabric-1.3",
                **({"ipAddress": _address(node)} if self.addressed else {}),
                "status": self._member_status(node),
            }
            for node in self._members()
        ]
        return json.dumps({"metadata": {"uid": DOMAIN_UID}, "status": {"nodes": nodes}})

    def _pods(self) -> str:
        """Return the domain's daemon pods as a JSON listing."""
        pods = [pod for pod in (self._daemon(node) for node in self.nodes) if pod is not None]
        return json.dumps({"items": pods})

    def _imex_report(self) -> str:
        """Return a surviving daemon's own report of the domain, as JSON.

        Shaped like `nvidia-imex-ctl -N -j -H`: an entry per configured member,
        each carrying its own `connections` map keyed by address.
        """
        departed = self._departed
        connected = [node for node in self.nodes if node != departed]
        if departed is not None and (self.peer_still_connected or self._since_shrink <= self._notice_at):
            connected.append(departed)

        entries: dict[str, Any] = {}
        for index, node in enumerate(self.nodes):
            peers = {
                str(peer_index): {"host": _address(peer), "status": "CONNECTED", "changed": True}
                for peer_index, peer in enumerate(connected)
            }
            if departed is not None and self.notice_as_invalid and departed not in connected:
                peers[str(len(peers))] = {"host": _address(departed), "status": "INVALID", "changed": True}
            entries[str(index)] = {
                "status": self.observer_status if node != departed else "UNAVAILABLE",
                "host": _address(node),
                "hostName": node,
                "connections": peers,
            }
        return json.dumps({"nodes": entries, "timestamp": "9/15/2026 00:00:00.000", "status": self.domain_state})

    @property
    def _notice_at(self) -> int:
        """Return the poll after the shrink at which peers stop seeing the node."""
        return self.departure_polls + self.notice_polls


def _pod(
    node: str,
    *,
    ready: bool,
    restarts: int,
    terminating: bool = False,
    exit_code: int = 0,
    uid_suffix: str = "original",
) -> dict[str, Any]:
    """Return one daemon pod as the cluster would report it."""
    metadata: dict[str, Any] = {
        "name": f"daemon-{node}",
        "namespace": DRIVER_NAMESPACE,
        "uid": f"pod-{node}-{uid_suffix}",
    }
    if terminating:
        metadata["deletionTimestamp"] = "2026-09-15T12:00:00Z"
    status: dict[str, Any] = {
        "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
        "containerStatuses": [{"name": "imex", "restartCount": restarts, "state": {}}],
    }
    if terminating:
        status["containerStatuses"][0]["state"] = {
            "terminated": {"exitCode": exit_code, "reason": "Error" if exit_code else "Completed"}
        }
    return {"metadata": metadata, "spec": {"nodeName": node}, "status": status}


def _kubectl(*args: str) -> str:
    """Compose a kubectl command, shell-quoting each part as the real builder does.

    The quoting matters here: the shrink passes a JSON patch as one argument,
    and a fake that joined on spaces would make a document the check never
    sends look like a dozen separate flags.
    """
    return " ".join(shlex.quote(part) for part in ("kubectl", *args))


def _run(cluster: _Cluster, **config: Any) -> ImexDomainDepartureCheck:
    """Run the check against ``cluster`` on a clock that never really waits."""
    clock = _Clock()
    check = ImexDomainDepartureCheck(config=config)
    with (
        patch("isvtest.validations.network.get_kubectl_base_shell", side_effect=_kubectl),
        patch("isvtest.validations.network.time", clock),
        patch.object(check, "run_command", side_effect=cluster),
    ):
        check.run()
    return check


def _subtests(check: ImexDomainDepartureCheck) -> dict[str, bool]:
    """Return each reported subtest name mapped to whether it passed."""
    return {result["name"]: result["passed"] for result in check._subtest_results}


def test_departure_observed_by_a_surviving_peer_passes() -> None:
    """The whole property: a clean exit, a peer that noticed, a domain that held."""
    check = _run(_Cluster())

    assert check.passed, check.message
    assert "gpu-1 left the compute domain cleanly" in check.message
    assert "gpu-2 reported it unavailable" in check.message
    assert "still operational among the surviving members" in check.message
    assert _subtests(check) == {
        "departure": True,
        "clean_exit": True,
        "departure_upheld": True,
        "peer_convergence": True,
        "surviving_members_operational": True,
        "restore": True,
    }


def test_departure_is_a_domain_shrink_not_a_drain() -> None:
    """A drain leaves DaemonSet pods running, so it would report a departure
    that never happened. The removal is a withdrawn channel claim instead."""
    cluster = _Cluster()
    _run(cluster)

    assert any(command.startswith("kubectl patch daemonset") for command in cluster.commands)
    assert not any(word in command for command in cluster.commands for word in ("drain", "cordon", "taint"))
    assert not any(command.startswith("kubectl delete pod") for command in cluster.commands)


def test_the_shrink_excludes_by_node_name_not_by_hostname_label() -> None:
    """The node's name is its identity; the hostname label is a convention a
    platform is free to set to something else."""
    cluster = _Cluster()
    _run(cluster)

    term = cluster.patches[0]["spec"]["template"]["spec"]["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]["nodeSelectorTerms"][0]
    assert term["matchFields"] == [{"key": "metadata.name", "operator": "NotIn", "values": ["gpu-1"]}]
    assert term["matchExpressions"] == [{"key": "nvidia.com/gpu.clique", "operator": "Exists"}]


def test_what_peers_observed_is_read_from_a_surviving_daemon() -> None:
    """The observation comes from asking a surviving daemon, not from reading
    the domain's own record of the node that left."""
    cluster = _Cluster()
    _run(cluster)

    execs = [command for command in cluster.commands if command.startswith("kubectl exec")]
    assert execs
    assert execs[0].endswith("daemon-gpu-2 -- nvidia-imex-ctl -N -j -H")


def test_a_domain_still_listing_the_departed_node_is_not_a_failure() -> None:
    """Stale bookkeeping in the controller is not what this check measures: the
    property is that the surviving members noticed."""
    check = _run(_Cluster(keep_departed_member=True))

    assert check.passed, check.message


def test_a_peer_that_never_notices_fails_as_stale_membership() -> None:
    """The defect this check exists to catch: survivors still believe a node
    that is gone is connected."""
    check = _run(_Cluster(peer_still_connected=True), convergence_timeout_seconds=30)

    assert not check.passed
    assert "still reported the departed node gpu-1 as available" in check.message
    assert "stale" in check.message
    assert _subtests(check)["peer_convergence"] is False


def test_a_collapsed_domain_fails_as_fragile_not_as_stale() -> None:
    """Losing the domain along with the node is a different fault from peers
    failing to notice, and must not share its message."""
    check = _run(_Cluster(nodes=("gpu-1", "gpu-2", "gpu-3"), broken_survivor="gpu-3"))

    assert not check.passed
    assert "did not leave the compute domain operational among its surviving members" in check.message
    assert "gpu-3" in check.message
    assert "fragile domain rather than peers failing to notice" in check.message
    assert _subtests(check)["surviving_members_operational"] is False


def test_a_daemon_put_back_on_the_departed_node_fails() -> None:
    """A deliberate departure is not the driver's to repair, so a daemon
    reappearing there is the removal being ignored."""
    check = _run(_Cluster(reschedule_after_departure=True))

    assert not check.passed
    assert "placed a daemon back on gpu-1" in check.message
    assert "ignored rather than a recovery" in check.message
    assert _subtests(check)["departure_upheld"] is False


def test_a_daemon_that_exits_with_an_error_fails() -> None:
    """The departing daemon has to stop cleanly, not merely stop."""
    check = _run(_Cluster(exit_code=1))

    assert not check.passed
    assert "did not exit cleanly" in check.message
    assert "exit code 1" in check.message
    assert _subtests(check)["clean_exit"] is False


def test_a_force_killed_daemon_names_the_expired_grace_period() -> None:
    """SIGKILL only happens once the grace period has run out, which is the
    signature of a daemon that had to be forced rather than asked."""
    check = _run(_Cluster(exit_code=137))

    assert not check.passed
    assert "exit code 137" in check.message
    assert "grace period expired" in check.message


def test_a_daemon_that_exits_on_sigterm_is_clean() -> None:
    """143 is the graceful path for a process that installs no signal handler,
    so treating it as an error would fail healthy clusters."""
    check = _run(_Cluster(exit_code=143))

    assert check.passed, check.message


def test_a_daemon_restarting_on_the_way_out_is_not_a_clean_exit() -> None:
    """Being left restarting is one of the two unclean shutdowns named."""
    check = _run(_Cluster(shutdown_restarts=2))

    assert not check.passed
    assert "restarted 2 more time(s) while shutting down" in check.message


def test_restarts_from_before_the_departure_are_not_held_against_it() -> None:
    """A daemon that restarted earlier in the cluster's life says nothing about
    how it handled the removal, so the count is taken against a baseline."""
    check = _run(_Cluster(baseline_restarts=3))

    assert check.passed, check.message


def test_a_departure_that_never_takes_effect_fails() -> None:
    """Nothing can be said about what peers observed while the departing node
    is still serving the domain, so the departure is confirmed first."""
    check = _run(_Cluster(departure_polls=10**6), convergence_timeout_seconds=30)

    assert not check.passed
    assert "did not take it out of compute domain" in check.message
    assert "still shutting down" in check.message
    assert _subtests(check)["departure"] is False


def test_a_daemon_left_running_names_the_driver() -> None:
    """A driver that ignores the withdrawn claim outright reads differently
    from one that is slow to finish the shutdown."""
    check = _run(_Cluster(daemon_never_stops=True), convergence_timeout_seconds=30)

    assert not check.passed
    assert "the driver left its daemon running there" in check.message


def test_a_single_member_domain_fails_as_too_small() -> None:
    """With no surviving peer there is nobody to observe the departure, so the
    check would otherwise pass with nothing asserted."""
    check = _run(_Cluster(nodes=("gpu-1",)))

    assert not check.passed
    assert "formed with 1 member(s)" in check.message
    assert "too small" in check.message


def test_a_domain_publishing_no_address_fails_readably() -> None:
    """Without an address there is no way to recognise the departed node in a
    surviving daemon's report of its peers."""
    check = _run(_Cluster(addressed=False))

    assert not check.passed
    assert "publishes no address for gpu-1, gpu-2" in check.message


def test_an_unreadable_peer_view_fails_rather_than_passing() -> None:
    """A view that could not be read is a check that never made its
    observation, not a departure nobody objected to."""
    check = _run(_Cluster(exec_result=_fail(stderr="container not found")), convergence_timeout_seconds=30)

    assert not check.passed
    assert "Could not establish what gpu-2 reports about the departed node gpu-1" in check.message
    assert "container not found" in check.message


def test_a_peer_reporting_the_node_invalid_reads_as_unavailable() -> None:
    """The assertion is over a normalized value, so a peer map that keeps the
    departed node with a dead connection still counts as having noticed."""
    check = _run(_Cluster(notice_as_invalid=True))

    assert check.passed, check.message


def test_a_surviving_daemon_reporting_its_domain_down_fails() -> None:
    """Noticing the departure is not enough: the domain has to keep working
    among the members that remain, as those members see it."""
    check = _run(_Cluster(domain_state="DOWN"), convergence_timeout_seconds=30)

    assert not check.passed
    assert "its own view of the domain was 'down'" in check.message
    assert "expected to stay operational" in check.message


def test_a_peer_map_from_an_unready_daemon_is_not_evidence() -> None:
    """An empty peer map on an unhealthy daemon says nothing about the peer, so
    it must not be read as that daemon reporting the node gone."""
    check = _run(_Cluster(observer_status="UNAVAILABLE"), convergence_timeout_seconds=30)

    assert not check.passed
    assert "Could not establish what gpu-2 reports" in check.message


def test_the_node_is_put_back_after_a_successful_departure() -> None:
    """The check degrades the domain deliberately, so it hands it back whole."""
    cluster = _Cluster()
    check = _run(cluster)

    assert check.passed, check.message
    assert len(cluster.patches) == 2
    restore = cluster.patches[1]["spec"]["template"]["spec"]["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]["nodeSelectorTerms"][0]
    assert "matchFields" not in restore
    assert _subtests(check)["restore"] is True


def test_the_node_is_put_back_even_when_the_assertion_failed() -> None:
    """Restoration is mandatory rather than best-effort."""
    cluster = _Cluster(peer_still_connected=True)
    _run(cluster, convergence_timeout_seconds=30)

    assert len(cluster.patches) == 2
    assert "matchFields" not in str(cluster.patches[1])


def test_a_node_that_cannot_be_put_back_fails_an_otherwise_passing_check() -> None:
    """A degraded domain left behind changes what the next run observes."""
    check = _run(_Cluster(restore_polls=10**6))

    assert not check.passed
    assert "Could not restore gpu-1 to the domain the check removed it from" in check.message
    assert _subtests(check)["restore"] is False


def test_a_failed_restore_does_not_mask_the_real_failure() -> None:
    """The assertion's own message is the more useful one."""
    check = _run(_Cluster(peer_still_connected=True, restore_polls=10**6), convergence_timeout_seconds=30)

    assert not check.passed
    assert "stale" in check.message


def test_a_restore_that_cannot_be_requested_is_reported() -> None:
    """Reinstating the claim is the first half of putting the node back."""
    check = _run(_Cluster(restore_patch_result=_fail(stderr="forbidden")))

    assert not check.passed
    assert "the channel claim could not be reinstated" in check.message
    assert "forbidden" in check.message


def test_a_shrink_that_cannot_be_requested_fails_readably() -> None:
    """A departure the check could not ask for is named, not raised."""
    check = _run(_Cluster(patch_result=_fail(stderr="admission webhook denied the request")))

    assert not check.passed
    assert "Failed to remove gpu-1 from compute domain" in check.message
    assert "admission webhook denied the request" in check.message


def test_the_domain_is_released_after_a_failure() -> None:
    """Teardown still hands the cluster back when the assertion failed."""
    cluster = _Cluster(peer_still_connected=True)
    check = _run(cluster, convergence_timeout_seconds=30)

    assert not check.passed
    assert cluster.released


def test_the_claims_are_released_before_the_domain() -> None:
    """No daemon should still be holding a prepared channel when the domain goes."""
    cluster = _Cluster()
    _run(cluster)

    deletes = [c for c in cluster.commands if c.startswith("kubectl delete daemonset") or "delete computedomains" in c]
    assert deletes[0].startswith("kubectl delete daemonset")
    assert "delete computedomains" in deletes[1]


def test_the_convergence_budget_is_derived_from_the_observed_formation() -> None:
    """The bound comes from how long this cluster took to form the domain, not
    from a round number picked in advance."""
    cluster = _Cluster(peer_still_connected=True, departure_polls=0)
    check = _run(cluster, formation_timeout_seconds=600)

    assert not check.passed
    # The domain formed on the first poll, so the floor applies.
    assert "60s after it left the domain" in check.message


def test_a_configured_convergence_timeout_overrides_the_derived_one() -> None:
    """An operator with a measured baseline can supply it directly."""
    check = _run(_Cluster(peer_still_connected=True), convergence_timeout_seconds=45)

    assert not check.passed
    assert "45s after it left the domain" in check.message


def test_a_missing_compute_domain_crd_skips() -> None:
    """A cluster advertising no multi-node NVLink capability is out of scope."""
    cluster = _Cluster(api_resources=_ok(""))
    with pytest.raises(pytest.skip.Exception, match="no multi-node NVLink capability"):
        _run(cluster)

    assert not cluster.allocated


def test_host_managed_mode_has_no_subject_and_skips() -> None:
    """Where the driver owns no daemon, removing a node from a domain stops
    nothing it owns, and the cluster is never mutated on the way to finding
    that out."""
    cluster = _Cluster(mode="hostManaged")
    with pytest.raises(pytest.skip.Exception, match="operator-run host service"):
        _run(cluster)

    assert not cluster.allocated
    assert not cluster.patches


def test_an_undeclared_ownership_mode_fails() -> None:
    """With no controller to declare a mode, the check cannot establish its own
    population, which is a failure rather than a silent skip."""
    check = _run(_Cluster(controller=False))

    assert not check.passed
    assert "Could not establish the IMEX daemon ownership mode" in check.message


def test_a_failed_allocation_never_shrinks_anything() -> None:
    """There is no domain to take a node out of."""
    cluster = _Cluster(apply_result=_fail(stderr="denied"))
    check = _run(cluster)

    assert not check.passed
    assert not cluster.patches
