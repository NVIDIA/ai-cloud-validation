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

"""Tests for the compute-domain reboot rejoin validation (SDN20-02).

The check allocates a compute domain, reboots one of its nodes through a
provider-supplied command, and watches four stages of the recovery. Every test
here answers its kubectl calls from a canned cluster that reacts to that reboot
on a clock the check itself advances, so each stage can be placed at a chosen
number of seconds after the node went down.
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Any
from unittest.mock import patch

import pytest

from isvtest.core.runners import CommandResult
from isvtest.validations.network import ImexDomainRebootRejoinCheck

DOMAIN_UID = "cd-uid-0003"
DRIVER_NAMESPACE = "nvidia-dra-driver-gpu"
CLIQUE = "fabric-1.3"
REBOOT_COMMAND = "reboot-node --node {node}"
OLD_BOOT_ID = "boot-before-0001"
NEW_BOOT_ID = "boot-after-0002"
OLD_DISCOVERY = "1700000000"
NEW_DISCOVERY = "1700009999"

#: The stages the check reports, in the order it reaches them.
STAGES = (
    "reboot_confirmed",
    "node_registered",
    "clique_label_republished",
    "daemon_rescheduled",
    "domain_ready",
)


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
    """A canned cluster whose node goes down and comes back in stages.

    Every knob below is a number of seconds after the reboot was requested, so
    a test can hold one stage back without touching the others - which is the
    whole point of the check reporting them separately. The clock is shared
    with the check, so those seconds are the ones the check itself measures.
    """

    def __init__(
        self,
        clock: _Clock,
        *,
        nodes: tuple[str, ...] = ("gpu-1", "gpu-2"),
        mode: str | None = "driverManaged",
        controller: bool = True,
        boot_id_at: float | None = 10,
        registered_at: float = 10,
        clique_at: float = 10,
        daemon_at: float = 15,
        domain_at: float = 20,
        discovery_at: float | None = 10,
        clique_after_reboot: str | None = CLIQUE,
        boot_id_before: str = OLD_BOOT_ID,
        clique_before: str | None = CLIQUE,
        discovery_before: str = OLD_DISCOVERY,
        node_unreadable_until: float = 0,
        api_resources: CommandResult | None = None,
        deployments: CommandResult | None = None,
        reboot_result: CommandResult | None = None,
        formed: bool = True,
    ) -> None:
        self.clock = clock
        self.nodes = nodes
        self.mode = mode
        self.controller = controller
        self.boot_id_at = boot_id_at
        self.registered_at = registered_at
        self.clique_at = clique_at
        self.daemon_at = daemon_at
        self.domain_at = domain_at
        self.discovery_at = discovery_at
        self.clique_after_reboot = clique_after_reboot
        self.boot_id_before = boot_id_before
        self.clique_before = clique_before
        self.discovery_before = discovery_before
        self.node_unreadable_until = node_unreadable_until
        self.api_resources = api_resources
        self.deployments = deployments
        self.reboot_result = reboot_result
        self.formed = formed

        self.commands: list[str] = []
        self.reboots: list[str] = []
        self.allocated = False
        self.released = False
        self._reboot_at: float | None = None

    # -- cluster state ----------------------------------------------------

    @property
    def target(self) -> str:
        """Return the node the check will reboot."""
        return min(self.nodes)

    @property
    def _since_reboot(self) -> float | None:
        """Return how long ago the reboot was requested, or None before it was."""
        return None if self._reboot_at is None else self.clock.now - self._reboot_at

    def _reached(self, stage_at: float) -> bool:
        """Return whether ``stage_at`` seconds have passed since the reboot."""
        since = self._since_reboot
        return since is not None and since >= stage_at

    def _boot_id(self) -> str:
        """Return the boot identity the node currently publishes."""
        if self.boot_id_at is None or not self._reached(self.boot_id_at):
            return self.boot_id_before
        return NEW_BOOT_ID

    def _node(self) -> dict[str, Any]:
        """Return the rebooted node as the cluster reports it right now."""
        down = self._since_reboot is not None and not self._reached(self.registered_at)
        if self._since_reboot is None:
            clique = self.clique_before
        elif self._reached(self.clique_at):
            clique = self.clique_after_reboot
        else:
            clique = None
        labels = {} if clique is None else {"nvidia.com/gpu.clique": clique}
        if self.discovery_before:
            fresh = self.discovery_at is not None and self._reached(self.discovery_at)
            labels["nvidia.com/gfd.timestamp"] = NEW_DISCOVERY if fresh else self.discovery_before
        return {
            "metadata": {"name": self.target, "labels": labels},
            "spec": {"unschedulable": down},
            "status": {
                "nodeInfo": {"bootID": self._boot_id()},
                "conditions": [{"type": "Ready", "status": "False" if down else "True"}],
            },
        }

    def _member_status(self, node: str) -> str:
        """Return the readiness the domain reports for ``node``."""
        if node != self.target or self._since_reboot is None:
            return "Ready" if self.formed else "NotReady"
        return "Ready" if self._reached(self.domain_at) else "NotReady"

    def _daemon(self, node: str) -> dict[str, Any] | None:
        """Return the domain's daemon pod on ``node``, or None when it has none."""
        if node != self.target or self._since_reboot is None:
            return _pod(node, ready=self.formed)
        return _pod(node, ready=True) if self._reached(self.daemon_at) else None

    # -- kubectl dispatch -------------------------------------------------

    def __call__(self, command: str, **_: Any) -> CommandResult:
        """Answer one invocation, kubectl or the provider's reboot command."""
        self.commands.append(command)

        if "api-resources" in command:
            return self.api_resources if self.api_resources is not None else _ok("computedomains.resource.nvidia.com\n")
        if command.startswith("kubectl get deployments"):
            return self.deployments if self.deployments is not None else _ok(self._deployments())
        if command.startswith("printf"):
            if "kind: ComputeDomain" in command:
                self.allocated = True
            return _ok()
        if command.startswith("kubectl get node "):
            return self._node_read()
        if command.startswith("kubectl get computedomains"):
            return _ok(self._domain())
        if command.startswith("kubectl get pods"):
            return _ok(self._pods())
        if command.startswith("kubectl delete daemonset"):
            return _ok()
        if command.startswith("kubectl delete computedomains"):
            self.released = True
            return _ok()
        if command.startswith("reboot-node"):
            return self._reboot(command)
        raise AssertionError(f"unexpected command: {command}")

    def _reboot(self, command: str) -> CommandResult:
        """Take the node down, unless the provider's mechanism refuses."""
        self.reboots.append(command)
        if self.reboot_result is not None:
            return self.reboot_result
        self._reboot_at = self.clock.now
        return _ok()

    def _node_read(self) -> CommandResult:
        """Answer one node read, or refuse while the node is deregistered."""
        since = self._since_reboot
        if since is not None and since < self.node_unreadable_until:
            return _fail(stderr=f'Error from server (NotFound): nodes "{self.target}" not found')
        return _ok(json.dumps(self._node()))

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
                "cliqueID": CLIQUE,
                "index": index,
                "status": self._member_status(node),
            }
            for index, node in enumerate(self.nodes)
        ]
        return json.dumps({"metadata": {"uid": DOMAIN_UID}, "status": {"nodes": nodes}})

    def _pods(self) -> str:
        """Return the domain's daemon pods as a JSON listing."""
        pods = [pod for pod in (self._daemon(node) for node in self.nodes) if pod is not None]
        return json.dumps({"items": pods})


def _pod(node: str, *, ready: bool) -> dict[str, Any]:
    """Return one daemon pod as the cluster would report it."""
    return {
        "metadata": {"name": f"daemon-{node}", "namespace": DRIVER_NAMESPACE, "uid": f"pod-{node}"},
        "spec": {"nodeName": node},
        "status": {
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            "containerStatuses": [{"name": "imex", "restartCount": 0, "state": {}}],
        },
    }


def _kubectl(*args: str) -> str:
    """Compose a kubectl command, shell-quoting each part as the real builder does."""
    return " ".join(shlex.quote(part) for part in ("kubectl", *args))


def _run(cluster: _Cluster, **config: Any) -> ImexDomainRebootRejoinCheck:
    """Run the check against ``cluster`` on the clock it shares with it."""
    config.setdefault("reboot_command", REBOOT_COMMAND)
    check = ImexDomainRebootRejoinCheck(config=config)
    with (
        patch("isvtest.validations.network.get_kubectl_base_shell", side_effect=_kubectl),
        patch("isvtest.validations.network.time", cluster.clock),
        patch.object(check, "run_command", side_effect=cluster),
    ):
        check.run()
    return check


def _cluster(**kwargs: Any) -> _Cluster:
    """Return a canned cluster on a fresh clock."""
    return _Cluster(_Clock(), **kwargs)


def _subtests(check: ImexDomainRebootRejoinCheck) -> dict[str, bool]:
    """Return each reported subtest name mapped to whether it passed."""
    return {result["name"]: result["passed"] for result in check._subtest_results}


def _durations(check: ImexDomainRebootRejoinCheck) -> dict[str, float | None]:
    """Return each reported subtest name mapped to its elapsed time."""
    return {result["name"]: result["duration"] for result in check._subtest_results}


def test_a_node_that_reboots_and_rejoins_passes() -> None:
    """The whole property: the node came back, kept its clique, got its daemon
    back, and the domain was ready again - with nothing done to help."""
    check = _run(_cluster())

    assert check.passed, check.message
    assert "gpu-1 rebooted and rejoined a ready compute domain for the same workload 20s later" in check.message
    assert "with no intervention" in check.message
    assert _subtests(check) == dict.fromkeys(STAGES, True)


def test_every_stage_is_reported_with_its_own_elapsed_time() -> None:
    """A platform that recovers correctly but far too slowly has to stay
    visible, and a stage that lagged has to be attributable."""
    check = _run(_cluster(boot_id_at=5, registered_at=25, clique_at=40, daemon_at=55, domain_at=70))

    assert check.passed, check.message
    assert _durations(check) == {
        "reboot_confirmed": 5,
        "node_registered": 25,
        "clique_label_republished": 40,
        "daemon_rescheduled": 55,
        "domain_ready": 70,
    }
    assert "reboot_confirmed 5s, node_registered 25s" in check.message


def test_a_node_that_never_went_down_fails_rather_than_passing_instantly() -> None:
    """The defect a weaker reading would miss entirely: nothing rebooted, and
    every other stage is trivially satisfied by a node that stayed up."""
    check = _run(_cluster(boot_id_at=None), rejoin_timeout_seconds=30)

    assert not check.passed
    assert "the reboot was never affirmatively confirmed" in check.message
    assert "reachability is not evidence" in check.message
    assert _subtests(check) == {"reboot_confirmed": False}


def test_no_stage_is_timed_from_before_the_boot_identity_changed() -> None:
    """While the node is going down the cluster still describes the boot that
    is ending, so a stage read then would be the old boot's state."""
    check = _run(_cluster(boot_id_at=60, registered_at=0, clique_at=0, daemon_at=0, domain_at=0))

    assert check.passed, check.message
    assert set(_durations(check).values()) == {60}


def test_a_clique_label_that_never_returns_indicts_whatever_publishes_it() -> None:
    """The domain cannot form around an unlabelled node, and the label is not
    the domain's to publish."""
    check = _run(_cluster(clique_after_reboot=None), rejoin_timeout_seconds=60)

    assert not check.passed
    assert "its NVLink clique label was never republished" in check.message
    assert "indicts whatever publishes the label" in check.message
    assert _subtests(check)["clique_label_republished"] is False


def test_a_clique_label_left_over_from_before_the_reboot_does_not_count() -> None:
    """Labels live on the node object, so the old one is still there the moment
    the node comes back. Counting it credits the cluster for a conclusion
    feature discovery has not reached: here it never re-runs, and the label
    standing untouched is exactly what a node dropped from its clique looks
    like until something restates it."""
    cluster = _cluster(clique_at=0, discovery_at=None)

    check = _run(cluster, rejoin_timeout_seconds=60)

    assert not check.passed
    assert "its NVLink clique label was never republished" in check.message
    assert _subtests(check)["clique_label_republished"] is False


def test_a_clique_label_restated_after_the_reboot_counts() -> None:
    """The label surviving the reboot is not held against a cluster that then
    re-runs discovery and stands by it."""
    check = _run(_cluster(clique_at=0, discovery_at=10))

    assert check.passed
    assert _subtests(check)["clique_label_republished"] is True


def test_a_cluster_that_stamps_no_discovery_time_is_taken_at_its_word() -> None:
    """Nothing dates the label there, and failing a cluster for a signal it
    never claimed to publish would report the wrong fault."""
    check = _run(_cluster(clique_at=0, discovery_at=None, discovery_before=""))

    assert check.passed
    assert _subtests(check)["clique_label_republished"] is True


def test_a_node_that_returns_in_a_different_clique_fails_for_that_reason() -> None:
    """Coming back in another NVLink partition is a different fault from the
    label never returning: the workload's domain is scoped to a clique."""
    check = _run(_cluster(clique_after_reboot="fabric-9.9"), rejoin_timeout_seconds=60)

    assert not check.passed
    assert "came back in NVLink clique fabric-9.9 rather than the fabric-1.3 it left" in check.message
    assert _subtests(check)["clique_label_republished"] is False


def test_a_node_left_cordoned_has_not_re_registered() -> None:
    """A node that comes back unschedulable is present but of no use to the
    workload, which is what this stage is about."""
    check = _run(_cluster(registered_at=10_000), rejoin_timeout_seconds=60)

    assert not check.passed
    assert "never came back as a ready, schedulable node" in check.message
    assert _subtests(check)["node_registered"] is False


def test_a_daemon_never_put_back_indicts_the_driver() -> None:
    """The node recovered; the domain's daemon did not follow it back."""
    check = _run(_cluster(daemon_at=10_000), rejoin_timeout_seconds=60)

    assert not check.passed
    assert "the domain's daemon was never rescheduled onto it" in check.message
    assert "indicts the driver rather than the node" in check.message
    assert _subtests(check)["daemon_rescheduled"] is False


def test_a_domain_that_never_re_forms_fails_after_the_daemon_came_back() -> None:
    """A daemon running while the domain never calls the node a ready member is
    exactly the split this check reports separately."""
    check = _run(_cluster(domain_at=10_000), rejoin_timeout_seconds=60)

    assert not check.passed
    assert "no ready compute domain re-formed for the same workload" in check.message
    assert _subtests(check) == {
        "reboot_confirmed": True,
        "node_registered": True,
        "clique_label_republished": True,
        "daemon_rescheduled": True,
        "domain_ready": False,
    }


def test_a_domain_left_broken_on_another_member_is_not_ready() -> None:
    """The workload is served by the domain, so a member still broken in the
    rebooted node's wake is not a domain that re-formed."""
    cluster = _cluster(nodes=("gpu-1", "gpu-2", "gpu-3"))
    original = cluster._member_status

    def broken_peer(node: str) -> str:
        return "NotReady" if node == "gpu-3" and cluster._since_reboot is not None else original(node)

    with patch.object(cluster, "_member_status", side_effect=broken_peer):
        check = _run(cluster, rejoin_timeout_seconds=60)

    assert not check.passed
    assert "no ready compute domain re-formed for the same workload" in check.message


def test_a_node_deregistered_while_it_is_down_is_not_a_failure() -> None:
    """Some platforms drop the node object while the node is away; the
    surrounding budget is what decides whether that matters."""
    check = _run(_cluster(node_unreadable_until=30, registered_at=30, daemon_at=35, domain_at=40))

    assert check.passed, check.message
    assert _durations(check)["reboot_confirmed"] == 30


def test_a_reboot_the_provider_refuses_fails_naming_the_mechanism() -> None:
    """Nothing can be asserted about a recovery from a reboot that never
    happened, and the mechanism is the provider's."""
    check = _run(_cluster(reboot_result=_fail(stderr="InstanceNotFound")))

    assert not check.passed
    assert "Failed to request an out-of-band reboot of gpu-1" in check.message
    assert "InstanceNotFound" in check.message
    assert _subtests(check) == {}


def test_the_node_name_is_shell_quoted_into_the_providers_command() -> None:
    """A platform whose node names carry shell metacharacters must not be able
    to turn the reboot into a command the check never meant to run."""
    cluster = _cluster(nodes=("gpu-1; rm -rf /", "gpu-2"))
    _run(cluster)

    assert cluster.reboots == ["reboot-node --node 'gpu-1; rm -rf /'"]


def test_a_reboot_command_that_does_not_name_a_node_is_rejected_before_allocating() -> None:
    """A command with no placeholder would reboot whatever it was hard-coded
    to, which need not be the node the check is asserting about."""
    cluster = _cluster()
    check = _run(cluster, reboot_command="reboot-node --node gpu-7")

    assert not check.passed
    assert "must carry {node}" in check.message
    assert not cluster.allocated


def test_a_cluster_with_no_reboot_mechanism_skips_before_allocating() -> None:
    """The cluster API cannot restart a node, so without a provider mechanism
    there is nothing to observe rather than something that failed."""
    cluster = _cluster()
    with pytest.raises(pytest.skip.Exception, match="No out-of-band reboot mechanism is configured"):
        _run(cluster, reboot_command="")

    assert not cluster.allocated


def test_a_single_member_domain_is_too_small_to_validate_the_property() -> None:
    """Rebooting the only member re-forms the whole domain from cold, which the
    requirement rules out as a substitute."""
    cluster = _cluster(nodes=("gpu-1",))
    check = _run(cluster)

    assert not check.passed
    assert "re-forming the whole domain from cold" in check.message
    assert not cluster.reboots


def test_a_node_publishing_no_boot_identity_is_never_rebooted() -> None:
    """A reboot that could not be confirmed afterwards is not worth causing."""
    cluster = _cluster(boot_id_before="")
    check = _run(cluster)

    assert not check.passed
    assert "publishes no boot identity" in check.message
    assert not cluster.reboots


def test_an_unlabelled_node_is_never_rebooted() -> None:
    """A label republished afterwards could not be told from one that was never
    there, so the baseline has to exist first."""
    cluster = _cluster(clique_before=None)
    check = _run(cluster)

    assert not check.passed
    assert "carries no NVLink clique label before the reboot" in check.message
    assert not cluster.reboots


def test_nothing_is_done_to_the_node_between_the_reboot_and_the_recovery() -> None:
    """Any manual step is a FAIL even where the end state is correct, so the
    check does nothing but read while it waits."""
    cluster = _cluster()
    _run(cluster)

    after_reboot = cluster.commands[cluster.commands.index(cluster.reboots[0]) + 1 :]
    mutations = [
        command
        for command in after_reboot
        if not command.startswith("kubectl get") and not command.startswith("kubectl delete")
    ]
    assert mutations == []
    assert not any(
        command.startswith(f"kubectl {verb}")
        for command in cluster.commands
        for verb in ("label", "uncordon", "cordon", "patch", "rollout", "annotate")
    )


def test_the_domain_and_its_claims_are_released_afterwards() -> None:
    """The check allocated them, so it hands them back however the run went."""
    cluster = _cluster(domain_at=10_000)
    check = _run(cluster, rejoin_timeout_seconds=30)

    assert not check.passed
    assert cluster.released
    assert any(command.startswith("kubectl delete daemonset") for command in cluster.commands)


def test_the_claims_are_held_open_for_longer_than_the_reboot_is_waited_out() -> None:
    """A claim that lapsed while the node was down would leave the check
    asserting against a domain nothing was asking for any more."""
    cluster = _cluster()
    _run(cluster, formation_timeout_seconds=300, rejoin_timeout_seconds=900)

    claims = next(command for command in cluster.commands if "kind: DaemonSet" in command)
    hold = int(re.search(r"sleep (\d+)", claims).group(1))
    assert hold > 300 + 900


def test_a_domain_that_never_forms_fails_before_anything_is_rebooted() -> None:
    """A daemon missing on arrival indicts the driver deployment, and is not a
    node this check should go and reboot."""
    cluster = _cluster(formed=False)
    check = _run(cluster, formation_timeout_seconds=30)

    assert not check.passed
    assert "did not come up unaided within 30s" in check.message
    assert not cluster.reboots


def test_a_host_managed_cluster_skips() -> None:
    """Where the driver defers the daemon to an operator-run host service there
    is no per-domain daemon for a rebooted node to get back."""
    with pytest.raises(pytest.skip.Exception, match="operator-run host service"):
        _run(_cluster(mode="hostManaged"))


def test_a_cluster_without_the_drivers_crd_skips() -> None:
    """A cluster advertising no multi-node NVLink through the driver is out of
    scope rather than failing."""
    with pytest.raises(pytest.skip.Exception, match="advertises no multi-node NVLink"):
        _run(_cluster(api_resources=_ok("resourceclaims.resource.k8s.io\n")))


def test_the_rejoin_budget_is_configurable() -> None:
    """Bounded but generous by default, since bare-metal reboots take minutes -
    but a fleet that knows its own numbers can say so."""
    check = _run(_cluster(domain_at=10_000), rejoin_timeout_seconds=45)

    assert not check.passed
    assert "did not rejoin a ready compute domain within 45s" in check.message
