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

"""Tests for the driver-managed IMEX daemon recovery validation (SDN18-02).

The check allocates a compute domain, kills one daemon inside it, and polls
for recovery, so the cluster it reads has to change over time. Every test here
answers its kubectl calls from a canned cluster that mutates on delete and
settles after a configurable number of polls, against a clock that advances
only when the check sleeps.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from isvtest.core.runners import CommandResult
from isvtest.validations.network import ImexDaemonRecoveryCheck

DOMAIN_UID = "cd-uid-0001"
DRIVER_NAMESPACE = "nvidia-dra-driver-gpu"


def _ok(stdout: str = "", stderr: str = "") -> CommandResult:
    """Return a successful ``CommandResult``."""
    return CommandResult(exit_code=0, stdout=stdout, stderr=stderr, duration=0.0)


def _fail(stdout: str = "", stderr: str = "", exit_code: int = 1) -> CommandResult:
    """Return a failed ``CommandResult``."""
    return CommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr, duration=0.0)


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
    """A canned cluster that answers the check's kubectl calls.

    Models the one thing a static fixture cannot: the domain changing under
    the check. A daemon delete removes that pod, and a replacement appears
    (and rejoins the domain) after the configured number of subsequent polls.
    """

    def __init__(
        self,
        *,
        nodes: tuple[str, ...] = ("gpu-1", "gpu-2"),
        unserved: tuple[str, ...] = (),
        mode: str | None = "driverManaged",
        controller: bool = True,
        ready_after_polls: int = 0,
        reschedule_after_polls: int | None = 0,
        rejoin_after_polls: int = 0,
        replacement_ready: bool = True,
        api_resources: CommandResult | None = None,
        deployments: CommandResult | None = None,
        apply_result: CommandResult | None = None,
        delete_domain_result: CommandResult | None = None,
    ) -> None:
        self.nodes = nodes
        self.unserved = set(unserved)
        self.mode = mode
        self.controller = controller
        self.ready_after_polls = ready_after_polls
        self.reschedule_after_polls = reschedule_after_polls
        self.rejoin_after_polls = rejoin_after_polls
        self.replacement_ready = replacement_ready
        self.api_resources = api_resources
        self.deployments = deployments
        self.apply_result = apply_result
        self.delete_domain_result = delete_domain_result

        self.commands: list[str] = []
        self.allocated = False
        self.claimed = False
        self.released = False
        self._domain_polls = 0
        self._terminated_node: str | None = None
        self._polls_since_terminate = 0

    # -- cluster state ----------------------------------------------------

    def _member_status(self, node: str) -> str:
        """Return the readiness the domain reports for ``node``."""
        if node in self.unserved:
            return "NotReady"
        if self._domain_polls <= self.ready_after_polls:
            return "NotReady"
        if node == self._terminated_node and self._polls_since_terminate <= self.rejoin_after_polls:
            return "NotReady"
        return "Ready"

    def _daemon(self, node: str) -> dict[str, Any] | None:
        """Return the domain's daemon pod on ``node``, or None when it has none."""
        if node in self.unserved:
            return None
        ready = self._domain_polls > self.ready_after_polls
        uid = f"pod-{node}-original"
        if node == self._terminated_node:
            if self.reschedule_after_polls is None:
                return None
            if self._polls_since_terminate <= self.reschedule_after_polls:
                return None
            uid = f"pod-{node}-replacement"
            ready = self.replacement_ready
        return {
            "metadata": {"name": f"daemon-{node}", "namespace": DRIVER_NAMESPACE, "uid": uid},
            "spec": {"nodeName": node},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]},
        }

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
            else:
                self.claimed = True
            return self.apply_result if self.apply_result is not None else _ok()
        if command.startswith("kubectl get computedomains"):
            self._domain_polls += 1
            if self._terminated_node is not None:
                self._polls_since_terminate += 1
            return _ok(self._domain())
        if command.startswith("kubectl get pods"):
            return _ok(self._pods())
        if command.startswith("kubectl delete pod"):
            self._terminated_node = command.split()[3].removeprefix("daemon-")
            self._polls_since_terminate = 0
            return _ok()
        if command.startswith("kubectl delete daemonset"):
            return _ok()
        if command.startswith("kubectl delete computedomains"):
            self.released = True
            return self.delete_domain_result if self.delete_domain_result is not None else _ok()
        raise AssertionError(f"unexpected command: {command}")

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
            {"name": node, "cliqueID": "fabric-1.3", "ipAddress": "10.0.0.1", "status": self._member_status(node)}
            for node in self.nodes
        ]
        return json.dumps({"metadata": {"uid": DOMAIN_UID}, "status": {"nodes": nodes}})

    def _pods(self) -> str:
        """Return the domain's daemon pods as a JSON listing."""
        pods = [pod for pod in (self._daemon(node) for node in self.nodes) if pod is not None]
        return json.dumps({"items": pods})


def _run(cluster: _Cluster, **config: Any) -> ImexDaemonRecoveryCheck:
    """Run the check against ``cluster`` on a clock that never really waits."""
    clock = _Clock()
    check = ImexDaemonRecoveryCheck(config=config)
    with (
        patch("isvtest.validations.network.get_kubectl_base_shell", side_effect=lambda *a: " ".join(("kubectl", *a))),
        patch("isvtest.validations.network.time", clock),
        patch.object(check, "run_command", side_effect=cluster),
    ):
        check.run()
    return check


def _subtests(check: ImexDaemonRecoveryCheck) -> dict[str, bool]:
    """Return each reported subtest name mapped to whether it passed."""
    return {result["name"]: result["passed"] for result in check._subtest_results}


def test_daemon_restored_after_termination_passes() -> None:
    """A driver that puts the daemon back and rejoins the domain passes."""
    check = _run(_Cluster())

    assert check.passed, check.message
    assert "All 2 domain member(s) ran a driver-started daemon on arrival" in check.message
    assert "restored to ready domain membership" in check.message
    assert _subtests(check) == {
        "unaided_presence": True,
        "terminate": True,
        "daemon_rescheduled": True,
        "domain_member": True,
    }


def test_termination_is_a_daemon_delete_not_a_departure() -> None:
    """The node is never cordoned, drained, or taken out of the domain: those
    are deliberate departures a correct controller declines to repair."""
    cluster = _Cluster()
    _run(cluster)

    assert any(command.startswith("kubectl delete pod") for command in cluster.commands)
    assert not any(word in command for command in cluster.commands for word in ("cordon", "drain", "taint"))


def test_nothing_the_check_runs_starts_a_daemon() -> None:
    """Presence is asserted, never arranged. The check creates a domain and
    pods to claim its channels - both tenant actions - and the driver is what
    starts daemons in response. It never creates or starts a daemon itself."""
    cluster = _Cluster()
    _run(cluster)

    created = [c for c in cluster.commands if c.startswith("printf") or " apply " in c or " create " in c]
    assert len(created) == 2
    assert "kind: ComputeDomain" in created[0]
    assert "kind: DaemonSet" in created[1]
    assert "compute-domain-daemon" not in "".join(created)


def test_channel_claims_are_what_give_the_check_a_subject() -> None:
    """The driver places no daemon until a channel claim is prepared: its
    per-domain DaemonSet selects on a node label set during that preparation,
    so an unclaimed domain keeps a DaemonSet of size zero indefinitely."""
    cluster = _Cluster()
    _run(cluster)

    claim = next(c for c in cluster.commands if c.startswith("printf") and "kind: DaemonSet" in c)
    assert "resourceClaimTemplateName: isv-sdn18-02-" in claim
    assert "key: nvidia.com/gpu.clique" in claim
    assert "image: busybox:1.36" in claim


def test_claim_image_is_configurable() -> None:
    """The pods only hold a claim open, so the image is the operator's to pick."""
    cluster = _Cluster()
    _run(cluster, image="registry.k8s.io/e2e-test-images/agnhost:2.47")

    claim = next(c for c in cluster.commands if c.startswith("printf") and "kind: DaemonSet" in c)
    assert "image: registry.k8s.io/e2e-test-images/agnhost:2.47" in claim


def test_invalid_image_is_rejected_before_any_manifest() -> None:
    """An image reference carries no whitespace or quoting."""
    cluster = _Cluster()
    check = _run(cluster, image="busybox:1.36 && rm -rf /")

    assert not check.passed
    assert "must be an image reference" in check.message
    assert not cluster.allocated


def test_missing_compute_domain_crd_skips() -> None:
    """A cluster advertising no multi-node NVLink capability is out of scope."""
    cluster = _Cluster(api_resources=_ok(""))
    with pytest.raises(pytest.skip.Exception, match="no multi-node NVLink capability"):
        _run(cluster)

    assert not cluster.allocated


def test_host_managed_mode_has_no_subject_and_skips() -> None:
    """A driver deferring to an operator-run host daemon owns no daemon here."""
    with pytest.raises(pytest.skip.Exception, match="operator-run host service"):
        _run(_Cluster(mode="hostManaged"))


def test_host_managed_mode_allocates_nothing() -> None:
    """The skip happens before the domain is created, so an out-of-scope
    cluster is never mutated on the way to finding that out."""
    cluster = _Cluster(mode="hostManaged")
    with pytest.raises(pytest.skip.Exception):
        _run(cluster)

    assert not cluster.allocated


def test_absent_daemons_do_not_read_as_host_managed() -> None:
    """A driver-managed cluster whose daemons never came up FAILS. Inferring
    the mode from the absence of a daemon would report this broken deployment
    as a cluster that legitimately never had one."""
    check = _run(_Cluster(unserved=("gpu-1", "gpu-2")), formation_timeout_seconds=10)

    assert not check.passed
    assert "did not come up unaided" in check.message
    assert "indicts the driver deployment" in check.message


def test_undeclared_ownership_mode_fails() -> None:
    """With no controller to declare a mode, the check cannot establish its own
    population, which is a failure rather than a silent skip."""
    check = _run(_Cluster(controller=False))

    assert not check.passed
    assert "Could not establish the IMEX daemon ownership mode" in check.message


def test_unrecognised_ownership_mode_fails() -> None:
    """A mode the check does not understand is not assumed to be the default."""
    check = _run(_Cluster(mode="somethingElse"))

    assert not check.passed
    assert "unrecognised IMEX daemon ownership mode" in check.message


def test_driver_predating_the_mode_setting_is_driver_managed() -> None:
    """A controller declaring no mode comes from a driver version where
    driver-managed is the only lifecycle implemented."""
    check = _run(_Cluster(mode=None))

    assert check.passed, check.message


def test_zero_members_fails_rather_than_passing_vacuously() -> None:
    """A domain that accounts for no nodes has nothing to assert against."""
    check = _run(_Cluster(nodes=()), formation_timeout_seconds=10)

    assert not check.passed
    assert "reported no members" in check.message


def test_member_without_a_daemon_on_arrival_fails() -> None:
    """Not running on arrival is a failure, not a setup step to repair."""
    check = _run(_Cluster(unserved=("gpu-2",)), formation_timeout_seconds=10)

    assert not check.passed
    assert "no daemon on gpu-2" in check.message


def test_daemon_never_rescheduled_indicts_the_controller() -> None:
    """Nothing put back at all is the controller's failure."""
    check = _run(_Cluster(reschedule_after_polls=None), recovery_timeout_seconds=30)

    assert not check.passed
    assert "never rescheduled a daemon onto gpu-1" in check.message
    assert "indicts the controller" in check.message
    assert _subtests(check)["daemon_rescheduled"] is False


def test_rescheduled_but_never_rejoined_indicts_domain_formation() -> None:
    """A pod that runs but never rejoins is the exact defect this test exists
    to catch, so it must not share a message with never being rescheduled."""
    check = _run(_Cluster(rejoin_after_polls=1000), recovery_timeout_seconds=30)

    assert not check.passed
    assert "never became a ready domain member" in check.message
    assert "indicts domain formation rather than the controller" in check.message
    assert _subtests(check) == {
        "unaided_presence": True,
        "terminate": True,
        "daemon_rescheduled": True,
        "domain_member": False,
    }


def test_running_replacement_that_is_not_ready_is_not_a_recovery() -> None:
    """A replacement pod reaching a Running state is not membership."""
    check = _run(_Cluster(replacement_ready=False), recovery_timeout_seconds=30)

    assert not check.passed
    assert "never became a ready domain member" in check.message


def test_recovery_budget_is_derived_from_the_observed_formation() -> None:
    """The bound comes from how long this cluster took to form the domain, not
    from a round number picked in advance."""
    check = _run(_Cluster(ready_after_polls=20, reschedule_after_polls=None))

    assert not check.passed
    # 20 polls at the 5s poll interval, doubled - well past the 60s floor.
    assert "within 200s" in check.message


def test_recovery_budget_floor_applies_to_an_instant_formation() -> None:
    """A domain that formed immediately would otherwise get no budget at all."""
    check = _run(_Cluster(reschedule_after_polls=None))

    assert not check.passed
    assert "within 60s" in check.message


def test_configured_recovery_timeout_overrides_the_derived_one() -> None:
    """An operator with a measured baseline can supply it directly."""
    check = _run(_Cluster(reschedule_after_polls=None), recovery_timeout_seconds=45)

    assert not check.passed
    assert "within 45s" in check.message


def test_terminating_pod_does_not_stand_in_for_its_replacement() -> None:
    """A pod carrying a deletion timestamp is on its way out, so counting it
    would report recovery the moment the check asked for the termination."""
    cluster = _Cluster(reschedule_after_polls=None)
    original = cluster._daemon

    def terminating(node: str) -> dict[str, Any] | None:
        pod = original(node)
        if pod is None and node == cluster._terminated_node:
            return {
                "metadata": {
                    "name": f"daemon-{node}",
                    "namespace": DRIVER_NAMESPACE,
                    "uid": f"pod-{node}-original",
                    "deletionTimestamp": "2026-09-14T12:00:00Z",
                },
                "spec": {"nodeName": node},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            }
        return pod

    with patch.object(cluster, "_daemon", side_effect=terminating):
        check = _run(cluster, recovery_timeout_seconds=30)

    assert not check.passed
    assert "never rescheduled a daemon onto gpu-1" in check.message


def test_domain_is_released_after_a_failure() -> None:
    """Teardown is mandatory, so a failing check still hands the cluster back."""
    cluster = _Cluster(reschedule_after_polls=None)
    check = _run(cluster, recovery_timeout_seconds=30)

    assert not check.passed
    assert cluster.released


def test_claims_are_released_before_the_domain() -> None:
    """No daemon should still be holding a prepared channel when the domain goes."""
    cluster = _Cluster()
    _run(cluster)

    deletes = [c for c in cluster.commands if c.startswith("kubectl delete daemonset") or "delete computedomains" in c]
    assert deletes[0].startswith("kubectl delete daemonset")
    assert "delete computedomains" in deletes[1]


def test_a_failed_allocation_creates_no_claims() -> None:
    """Claims reference a template the controller creates for the domain, so
    there is nothing for them to claim when the domain was never created."""
    cluster = _Cluster(apply_result=_fail(stderr="denied"))
    check = _run(cluster)

    assert not check.passed
    assert not cluster.claimed


def test_release_failure_fails_an_otherwise_passing_check() -> None:
    """A domain left behind changes what the next run observes."""
    check = _run(_Cluster(delete_domain_result=_fail(stderr="forbidden")))

    assert not check.passed
    assert "Could not release what the check created" in check.message


def test_release_failure_does_not_mask_the_real_failure() -> None:
    """The assertion's own message is the more useful one."""
    check = _run(
        _Cluster(reschedule_after_polls=None, delete_domain_result=_fail(stderr="forbidden")),
        recovery_timeout_seconds=30,
    )

    assert not check.passed
    assert "never rescheduled" in check.message


def test_allocation_failure_fails_readably() -> None:
    """A domain the check could not create is named, not raised."""
    check = _run(_Cluster(apply_result=_fail(stderr="admission webhook denied the request")))

    assert not check.passed
    assert "Failed to create compute domain" in check.message
    assert "admission webhook denied the request" in check.message


def test_unreadable_deployments_listing_fails() -> None:
    """A cluster the check cannot read its own population from fails."""
    check = _run(_Cluster(deployments=_fail(stderr="forbidden")))

    assert not check.passed
    assert "forbidden" in check.message


def test_invalid_namespace_is_rejected_before_any_manifest() -> None:
    """A namespace is a DNS-1123 label; anything else never reaches a manifest."""
    cluster = _Cluster()
    check = _run(cluster, namespace="Not A Namespace")

    assert not check.passed
    assert "must be a DNS-1123 label" in check.message
    assert not cluster.allocated
