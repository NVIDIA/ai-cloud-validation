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

"""Tests for issue #665: K8sLoadBalancerCheck, K8sCoreDnsForwardingCheck, K8sCidrRangesCheck."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from isvtest.core.runners import CommandResult
from isvtest.validations.k8s_networking import (
    _PROBE_ANSWER_IP,
    K8sCidrRangesCheck,
    K8sCoreDnsForwardingCheck,
    K8sLoadBalancerCheck,
)


def _ok(stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(exit_code=0, stdout=stdout, stderr=stderr, duration=0.0)


def _fail(stdout: str = "", stderr: str = "", exit_code: int = 1) -> CommandResult:
    return CommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr, duration=0.0)


def _ok_proc() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _svc_json(ingress: list[dict[str, str]]) -> str:
    return json.dumps({"status": {"loadBalancer": {"ingress": ingress}}})


# --------------------------- K8S29: LoadBalancer ---------------------------
class TestLoadBalancer:
    def test_external_lb_passes_when_ingress_populated(self) -> None:
        check = K8sLoadBalancerCheck(config={})

        def fake_run(cmd: str, timeout=None, display_cmd=None) -> CommandResult:
            if "get svc" in cmd:
                return _ok(stdout=_svc_json([{"ip": "203.0.113.9"}]))
            return _ok()

        with (
            patch.object(check, "run_command", side_effect=fake_run),
            patch("isvtest.validations.k8s_networking.subprocess.run", return_value=_ok_proc()),
        ):
            check.run()

        assert check.passed
        names = {s["name"] for s in check._subtest_results}
        assert names == {"external", "internal", "static_ip"}
        assert {s["name"] for s in check._subtest_results if s.get("skipped")} == {"internal", "static_ip"}

    def test_static_ip_mismatch_fails(self) -> None:
        check = K8sLoadBalancerCheck(config={"static_ip": "203.0.113.100"})

        def fake_run(cmd: str, timeout=None, display_cmd=None) -> CommandResult:
            if "get svc" in cmd:
                return _ok(stdout=_svc_json([{"ip": "203.0.113.9"}]))  # not the requested IP
            return _ok()

        with (
            patch.object(check, "run_command", side_effect=fake_run),
            patch("isvtest.validations.k8s_networking.subprocess.run", return_value=_ok_proc()),
        ):
            check.run()

        assert not check.passed

    def test_invalid_annotations_json_fails_fast(self) -> None:
        check = K8sLoadBalancerCheck(config={"annotations": "{not json"})
        with patch.object(check, "run_command") as mock_run:
            check.run()
        mock_run.assert_not_called()
        assert not check.passed

    def test_namespace_create_failure_fails(self) -> None:
        check = K8sLoadBalancerCheck(config={})
        with patch.object(check, "run_command", return_value=_fail(stderr="forbidden")):
            check.run()
        assert not check.passed


# --------------------------- K8S30: CoreDNS forwarding ---------------------------
class TestCoreDnsForwarding:
    def test_missing_configmap_skips_cleanly(self) -> None:
        check = K8sCoreDnsForwardingCheck(config={})
        with patch.object(
            check,
            "run_command",
            return_value=_fail(stderr='Error from server (NotFound): configmaps "coredns" not found'),
        ):
            check.run()
        assert check.passed

    def _fake_run(self, resolves: bool):
        def fake_run(cmd: str, timeout=None, display_cmd=None) -> CommandResult:
            if "get configmap" in cmd:
                return _ok(stdout=json.dumps({"data": {"Corefile": ".:53 {\n forward . /etc/resolv.conf\n}\n"}}))
            if "get svc fake-resolver" in cmd:
                return _ok(stdout="10.96.5.5")
            if "exec" in cmd and "nslookup" in cmd:
                return _ok(stdout=f"Address: {_PROBE_ANSWER_IP}\n") if resolves else _fail(stdout="NXDOMAIN")
            return _ok()

        return fake_run

    def test_successful_forwarding_passes(self) -> None:
        check = K8sCoreDnsForwardingCheck(config={"propagation_timeout_s": 5, "poll_interval_s": 1})
        with (
            patch.object(check, "run_command", side_effect=self._fake_run(resolves=True)),
            patch("isvtest.validations.k8s_networking.subprocess.run", return_value=_ok_proc()),
        ):
            check.run()
        assert check.passed

    def test_never_resolves_fails(self) -> None:
        check = K8sCoreDnsForwardingCheck(config={"propagation_timeout_s": 2, "poll_interval_s": 1})
        with (
            patch.object(check, "run_command", side_effect=self._fake_run(resolves=False)),
            patch("isvtest.validations.k8s_networking.subprocess.run", return_value=_ok_proc()),
            patch("isvtest.validations.k8s_networking.time.sleep"),
        ):
            check.run()
        assert not check.passed


# --------------------------- K8S31: CIDR ranges ---------------------------
class TestCidrRanges:
    def test_skips_when_nothing_configured(self) -> None:
        check = K8sCidrRangesCheck(config={})
        with patch.object(check, "run_command") as mock_run:
            check.run()
        mock_run.assert_not_called()
        assert check.passed

    def test_service_cidr_in_range_passes(self) -> None:
        check = K8sCidrRangesCheck(config={"expected_service_cidr": "10.96.0.0/12"})
        payload = json.dumps({"spec": {"clusterIP": "10.96.0.1"}})
        with patch.object(check, "run_command", return_value=_ok(stdout=payload)):
            check.run()
        assert check.passed

    def test_service_cidr_out_of_range_fails(self) -> None:
        check = K8sCidrRangesCheck(config={"expected_service_cidr": "10.96.0.0/12"})
        payload = json.dumps({"spec": {"clusterIP": "192.168.1.1"}})
        with patch.object(check, "run_command", return_value=_ok(stdout=payload)):
            check.run()
        assert not check.passed

    def test_pod_and_node_cidr_pass_together(self) -> None:
        check = K8sCidrRangesCheck(
            config={"expected_pod_cidr": "10.244.0.0/16", "expected_node_cidr": "192.168.0.0/16"}
        )
        nodes = json.dumps(
            {
                "items": [
                    {
                        "metadata": {"name": "n1"},
                        "spec": {"podCIDR": "10.244.1.0/24"},
                        "status": {"addresses": [{"type": "InternalIP", "address": "192.168.1.5"}]},
                    }
                ]
            }
        )
        with patch.object(check, "run_command", return_value=_ok(stdout=nodes)):
            check.run()
        assert check.passed

    def test_invalid_cidr_fails_fast(self) -> None:
        check = K8sCidrRangesCheck(config={"expected_service_cidr": "garbage"})
        with patch.object(check, "run_command") as mock_run:
            check.run()
        mock_run.assert_not_called()
        assert not check.passed
