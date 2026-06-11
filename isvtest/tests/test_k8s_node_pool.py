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

"""Unit tests for ``isvtest.validations.k8s_node_pool``."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from isvtest.core.runners import CommandResult
from isvtest.validations.k8s_node_pool import (
    K8sNodePoolCheck,
    _coerce_mapping,
    _coerce_str_list,
    _coerce_taints,
    _is_node_ready,
    _missing_taints,
)


def _ok(stdout: str = "", stderr: str = "") -> CommandResult:
    """Build a successful ``CommandResult`` (exit_code=0) for mocked commands."""
    return CommandResult(exit_code=0, stdout=stdout, stderr=stderr, duration=0.0)


def _fail(stdout: str = "", stderr: str = "", exit_code: int = 1) -> CommandResult:
    """Build a failing ``CommandResult`` (non-zero exit) for mocked commands."""
    return CommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr, duration=0.0)


def _node(
    name: str,
    *,
    ready: bool = True,
    labels: dict[str, str] | None = None,
    taints: list[dict[str, Any]] | None = None,
    instance_type: str | None = "m6i.large",
) -> dict[str, Any]:
    """Build a minimal Node object matching ``kubectl get nodes -o json``."""
    node_labels = dict(labels or {})
    if instance_type is not None:
        node_labels.setdefault("node.kubernetes.io/instance-type", instance_type)
    return {
        "metadata": {"name": name, "labels": node_labels},
        "spec": {"taints": list(taints or [])},
        "status": {
            "conditions": [
                {"type": "Ready", "status": "True" if ready else "False"},
            ]
        },
    }


def _nodes_payload(nodes: list[dict[str, Any]]) -> str:
    """Serialize ``nodes`` into a ``kubectl get nodes -o json`` payload string."""
    return json.dumps({"items": nodes})


BASE_CONFIG: dict[str, Any] = {
    "label_selector": "eks.amazonaws.com/nodegroup=isv-test-pool",
    "expected_replicas": 1,
    "expected_labels": {"isv.test/pool": "test"},
    "expected_taints": [{"key": "isv.test/dedicated", "value": "test", "effect": "NoSchedule"}],
    "expected_instance_types": ["m6i.large"],
    "node_type": "cpu",
    "wait_timeout": 1,
    "poll_interval": 1,
}


class TestCoerceMapping:
    """Tests for ``_coerce_mapping`` (string/dict -> str->str dict)."""

    def test_none_is_empty(self) -> None:
        assert _coerce_mapping(None, "labels") == {}

    def test_empty_string_is_empty(self) -> None:
        assert _coerce_mapping("", "labels") == {}

    def test_native_dict(self) -> None:
        assert _coerce_mapping({"a": "1", "b": "2"}, "labels") == {"a": "1", "b": "2"}

    def test_json_string(self) -> None:
        assert _coerce_mapping('{"a":"1"}', "labels") == {"a": "1"}

    def test_malformed_json(self) -> None:
        with pytest.raises(ValueError, match="valid JSON"):
            _coerce_mapping("{not json", "labels")

    def test_wrong_shape(self) -> None:
        with pytest.raises(ValueError, match="mapping"):
            _coerce_mapping([1, 2], "labels")

    def test_non_string_leaf(self) -> None:
        with pytest.raises(ValueError, match="str->str"):
            _coerce_mapping({"a": 1}, "labels")


class TestCoerceStrList:
    """Tests for ``_coerce_str_list`` (string/list -> list[str])."""

    def test_none_and_empty(self) -> None:
        assert _coerce_str_list(None, "x") == []
        assert _coerce_str_list("", "x") == []

    def test_native_list(self) -> None:
        assert _coerce_str_list(["a", "b"], "x") == ["a", "b"]

    def test_json_string(self) -> None:
        assert _coerce_str_list('["a","b"]', "x") == ["a", "b"]

    def test_wrong_shape(self) -> None:
        with pytest.raises(ValueError, match="list"):
            _coerce_str_list({"a": "b"}, "x")

    def test_non_string_item(self) -> None:
        with pytest.raises(ValueError, match="strings"):
            _coerce_str_list([1, 2], "x")


class TestCoerceTaints:
    """Tests for ``_coerce_taints`` (string/list -> normalized taint dicts)."""

    def test_none(self) -> None:
        assert _coerce_taints(None) == []

    def test_native(self) -> None:
        v = [{"key": "k", "value": "v", "effect": "NoSchedule"}]
        assert _coerce_taints(v) == [("k", "v", "NoSchedule")]

    def test_missing_value_normalized_to_empty(self) -> None:
        v = [{"key": "k", "effect": "NoSchedule"}]
        assert _coerce_taints(v) == [("k", "", "NoSchedule")]

    def test_null_value_normalized_to_empty(self) -> None:
        v = [{"key": "k", "value": None, "effect": "NoSchedule"}]
        assert _coerce_taints(v) == [("k", "", "NoSchedule")]

    def test_missing_key(self) -> None:
        with pytest.raises(ValueError, match="key"):
            _coerce_taints([{"value": "v", "effect": "NoSchedule"}])

    def test_missing_effect(self) -> None:
        with pytest.raises(ValueError, match="effect"):
            _coerce_taints([{"key": "k", "value": "v"}])

    def test_non_string_value(self) -> None:
        with pytest.raises(ValueError, match="value"):
            _coerce_taints([{"key": "k", "value": 1, "effect": "NoSchedule"}])

    def test_json_string(self) -> None:
        s = '[{"key":"k","value":"v","effect":"NoSchedule"}]'
        assert _coerce_taints(s) == [("k", "v", "NoSchedule")]


class TestMissingTaints:
    """Tests for ``_missing_taints`` (expected vs. actual node taint diff)."""

    def test_empty_expected_skips(self) -> None:
        assert _missing_taints([], [{"key": "k", "value": "v", "effect": "NoSchedule"}]) == []

    def test_all_present(self) -> None:
        expected = [("k", "v", "NoSchedule")]
        actual = [{"key": "k", "value": "v", "effect": "NoSchedule"}]
        assert _missing_taints(expected, actual) == []

    def test_missing_one(self) -> None:
        expected = [("k1", "v", "NoSchedule"), ("k2", "v", "NoSchedule")]
        actual = [{"key": "k1", "value": "v", "effect": "NoSchedule"}]
        assert _missing_taints(expected, actual) == [("k2", "v", "NoSchedule")]

    def test_effect_differs(self) -> None:
        expected = [("k", "v", "NoSchedule")]
        actual = [{"key": "k", "value": "v", "effect": "PreferNoSchedule"}]
        assert _missing_taints(expected, actual) == [("k", "v", "NoSchedule")]


class TestIsNodeReady:
    """Tests for ``_is_node_ready`` (Ready=True condition detection)."""

    def test_ready(self) -> None:
        assert _is_node_ready({"status": {"conditions": [{"type": "Ready", "status": "True"}]}})

    def test_not_ready(self) -> None:
        assert not _is_node_ready({"status": {"conditions": [{"type": "Ready", "status": "False"}]}})

    def test_missing_condition(self) -> None:
        assert not _is_node_ready({"status": {"conditions": []}})

    def test_empty_status(self) -> None:
        assert not _is_node_ready({})


class TestNodePoolCreateHappyPath:
    """Validation succeeds when kubectl shows the expected node pool shape."""

    def _make(self, **overrides: Any) -> K8sNodePoolCheck:
        cfg = {**BASE_CONFIG, **overrides}
        return K8sNodePoolCheck(config=cfg)

    def test_single_node_passes(self) -> None:
        check = self._make()
        node = _node(
            "ip-10-0-0-1",
            labels={"isv.test/pool": "test"},
            taints=[{"key": "isv.test/dedicated", "value": "test", "effect": "NoSchedule"}],
            instance_type="m6i.large",
        )
        with (
            patch.object(check, "run_command", return_value=_ok(stdout=_nodes_payload([node]))),
            patch("isvtest.validations.k8s_node_pool.time.sleep"),
        ):
            check.run()
        assert check.passed, check._error
        assert "1 node(s) Ready" in check._output
        assert "(cpu)" in check._output

    def test_multiple_nodes_pass(self) -> None:
        check = self._make(expected_replicas=3)
        nodes = [
            _node(
                f"node-{i}",
                labels={"isv.test/pool": "test"},
                taints=[{"key": "isv.test/dedicated", "value": "test", "effect": "NoSchedule"}],
            )
            for i in range(3)
        ]
        with (
            patch.object(check, "run_command", return_value=_ok(stdout=_nodes_payload(nodes))),
            patch("isvtest.validations.k8s_node_pool.time.sleep"),
        ):
            check.run()
        assert check.passed
        assert "3 node(s) Ready" in check._output

    def test_extra_labels_still_pass_subset(self) -> None:
        check = self._make()
        node = _node(
            "n",
            labels={"isv.test/pool": "test", "extra": "whatever"},
            taints=[{"key": "isv.test/dedicated", "value": "test", "effect": "NoSchedule"}],
        )
        with (
            patch.object(check, "run_command", return_value=_ok(stdout=_nodes_payload([node]))),
            patch("isvtest.validations.k8s_node_pool.time.sleep"),
        ):
            check.run()
        assert check.passed

    def test_empty_expected_taints_skips_taint_check(self) -> None:
        check = self._make(expected_taints=[])
        # Node has taints not in the expected list; should still pass since check is skipped.
        node = _node(
            "n",
            labels={"isv.test/pool": "test"},
            taints=[{"key": "unrelated", "value": "v", "effect": "NoSchedule"}],
        )
        with (
            patch.object(check, "run_command", return_value=_ok(stdout=_nodes_payload([node]))),
            patch("isvtest.validations.k8s_node_pool.time.sleep"),
        ):
            check.run()
        assert check.passed

    def test_empty_instance_type_list_skips_type_check(self) -> None:
        check = self._make(expected_instance_types=[])
        node = _node(
            "n",
            labels={"isv.test/pool": "test"},
            taints=[{"key": "isv.test/dedicated", "value": "test", "effect": "NoSchedule"}],
            instance_type="whatever.xlarge",
        )
        with (
            patch.object(check, "run_command", return_value=_ok(stdout=_nodes_payload([node]))),
            patch("isvtest.validations.k8s_node_pool.time.sleep"),
        ):
            check.run()
        assert check.passed

    def test_json_string_config_parsed(self) -> None:
        # Step-output path: templated JSON strings instead of native lists/dicts.
        cfg: dict[str, Any] = {
            **BASE_CONFIG,
            "expected_labels": '{"isv.test/pool":"test"}',
            "expected_taints": '[{"key":"isv.test/dedicated","value":"test","effect":"NoSchedule"}]',
            "expected_instance_types": '["m6i.large"]',
        }
        check = K8sNodePoolCheck(config=cfg)
        node = _node(
            "n",
            labels={"isv.test/pool": "test"},
            taints=[{"key": "isv.test/dedicated", "value": "test", "effect": "NoSchedule"}],
        )
        with (
            patch.object(check, "run_command", return_value=_ok(stdout=_nodes_payload([node]))),
            patch("isvtest.validations.k8s_node_pool.time.sleep"),
        ):
            check.run()
        assert check.passed

    def test_expected_zero_replicas_passes_with_empty_list(self) -> None:
        check = self._make(expected_replicas=0)
        with (
            patch.object(check, "run_command", return_value=_ok(stdout=_nodes_payload([]))),
            patch("isvtest.validations.k8s_node_pool.time.sleep"),
        ):
            check.run()
        assert check.passed


class TestNodePoolCreateConvergence:
    """Polling behavior: eventual readiness vs. timeout."""

    def _make(self, **overrides: Any) -> K8sNodePoolCheck:
        cfg = {**BASE_CONFIG, **overrides}
        return K8sNodePoolCheck(config=cfg)

    def test_converges_after_initial_notready(self) -> None:
        check = self._make(wait_timeout=30, poll_interval=1)
        pending = _node(
            "n",
            ready=False,
            labels={"isv.test/pool": "test"},
            taints=[{"key": "isv.test/dedicated", "value": "test", "effect": "NoSchedule"}],
        )
        ready = _node(
            "n",
            ready=True,
            labels={"isv.test/pool": "test"},
            taints=[{"key": "isv.test/dedicated", "value": "test", "effect": "NoSchedule"}],
        )
        responses = iter(
            [
                _ok(stdout=_nodes_payload([])),
                _ok(stdout=_nodes_payload([pending])),
                _ok(stdout=_nodes_payload([ready])),
            ]
        )
        with (
            patch.object(check, "run_command", side_effect=lambda *a, **k: next(responses)),
            patch("isvtest.validations.k8s_node_pool.time.sleep"),
        ):
            check.run()
        assert check.passed

    def test_timeout_when_never_ready(self) -> None:
        check = self._make(wait_timeout=2, poll_interval=1)
        pending = _node("n", ready=False, labels={"isv.test/pool": "test"})
        # Clock: 0.0 (start/deadline=2.0), 0.5 (first iter deadline check), 3.0 (second iter, past deadline).
        clock = iter([0.0, 0.5, 3.0, 3.0, 3.0])
        with (
            patch.object(check, "run_command", return_value=_ok(stdout=_nodes_payload([pending]))),
            patch("isvtest.validations.k8s_node_pool.time.sleep"),
            patch("isvtest.validations.k8s_node_pool.time.monotonic", side_effect=lambda: next(clock, 999.0)),
        ):
            check.run()
        assert not check.passed
        assert "did not converge" in check._error
        assert "0 Ready / 1 total" in check._error

    def test_kubectl_error_propagates_in_timeout_message(self) -> None:
        check = self._make(wait_timeout=1, poll_interval=1)
        clock = iter([0.0, 0.5, 2.0, 2.0, 2.0])
        with (
            patch.object(check, "run_command", return_value=_fail(stderr="Unauthorized")),
            patch("isvtest.validations.k8s_node_pool.time.sleep"),
            patch("isvtest.validations.k8s_node_pool.time.monotonic", side_effect=lambda: next(clock, 999.0)),
        ):
            check.run()
        assert not check.passed
        assert "Unauthorized" in check._error

    def test_malformed_kubectl_json_fails_cleanly(self) -> None:
        check = self._make()
        with (
            patch.object(check, "run_command", return_value=_ok(stdout="{not json")),
            patch("isvtest.validations.k8s_node_pool.time.sleep"),
        ):
            check.run()
        assert not check.passed
        assert "parse kubectl JSON" in check._error


class TestNodePoolCreateAssertionFailures:
    """Once the pool converges, each per-node assertion fails independently."""

    def _make(self, **overrides: Any) -> K8sNodePoolCheck:
        cfg = {**BASE_CONFIG, **overrides}
        return K8sNodePoolCheck(config=cfg)

    def test_missing_label_fails(self) -> None:
        check = self._make()
        node = _node(
            "bad-node",
            labels={},  # missing isv.test/pool
            taints=[{"key": "isv.test/dedicated", "value": "test", "effect": "NoSchedule"}],
        )
        with (
            patch.object(check, "run_command", return_value=_ok(stdout=_nodes_payload([node]))),
            patch("isvtest.validations.k8s_node_pool.time.sleep"),
        ):
            check.run()
        assert not check.passed
        assert "bad-node" in check._error
        assert "missing/incorrect labels" in check._error

    def test_wrong_label_value_fails(self) -> None:
        check = self._make()
        node = _node(
            "n",
            labels={"isv.test/pool": "wrong-value"},
            taints=[{"key": "isv.test/dedicated", "value": "test", "effect": "NoSchedule"}],
        )
        with (
            patch.object(check, "run_command", return_value=_ok(stdout=_nodes_payload([node]))),
            patch("isvtest.validations.k8s_node_pool.time.sleep"),
        ):
            check.run()
        assert not check.passed
        assert "missing/incorrect labels" in check._error

    def test_missing_taint_fails(self) -> None:
        check = self._make()
        node = _node(
            "n",
            labels={"isv.test/pool": "test"},
            taints=[],
        )
        with (
            patch.object(check, "run_command", return_value=_ok(stdout=_nodes_payload([node]))),
            patch("isvtest.validations.k8s_node_pool.time.sleep"),
        ):
            check.run()
        assert not check.passed
        assert "missing taints" in check._error

    def test_taint_effect_mismatch_fails(self) -> None:
        check = self._make()
        node = _node(
            "n",
            labels={"isv.test/pool": "test"},
            taints=[{"key": "isv.test/dedicated", "value": "test", "effect": "PreferNoSchedule"}],
        )
        with (
            patch.object(check, "run_command", return_value=_ok(stdout=_nodes_payload([node]))),
            patch("isvtest.validations.k8s_node_pool.time.sleep"),
        ):
            check.run()
        assert not check.passed
        assert "missing taints" in check._error

    def test_instance_type_mismatch_fails(self) -> None:
        check = self._make()
        node = _node(
            "n",
            labels={"isv.test/pool": "test"},
            taints=[{"key": "isv.test/dedicated", "value": "test", "effect": "NoSchedule"}],
            instance_type="c7g.large",  # not in expected list
        )
        with (
            patch.object(check, "run_command", return_value=_ok(stdout=_nodes_payload([node]))),
            patch("isvtest.validations.k8s_node_pool.time.sleep"),
        ):
            check.run()
        assert not check.passed
        assert "c7g.large" in check._error
        assert "allowlist" in check._error

    def test_multiple_nodes_failure_counts_reflect_reality(self) -> None:
        check = self._make(expected_replicas=2)
        good = _node(
            "good",
            labels={"isv.test/pool": "test"},
            taints=[{"key": "isv.test/dedicated", "value": "test", "effect": "NoSchedule"}],
        )
        bad = _node(
            "bad",
            labels={"isv.test/pool": "wrong"},
            taints=[{"key": "isv.test/dedicated", "value": "test", "effect": "NoSchedule"}],
        )
        with (
            patch.object(check, "run_command", return_value=_ok(stdout=_nodes_payload([good, bad]))),
            patch("isvtest.validations.k8s_node_pool.time.sleep"),
        ):
            check.run()
        assert not check.passed
        assert "1 of 2 node(s)" in check._error
        assert "bad" in check._error


class TestNodePoolCreateBadConfig:
    """Config validation failures should fail fast without polling kubectl."""

    def test_missing_label_selector(self) -> None:
        check = K8sNodePoolCheck(config={"expected_replicas": 1})
        with patch.object(check, "run_command") as mock_run:
            check.run()
        assert not check.passed
        assert "Invalid config" in check._error
        mock_run.assert_not_called()

    def test_empty_label_selector(self) -> None:
        check = K8sNodePoolCheck(config={"label_selector": "  ", "expected_replicas": 1})
        with patch.object(check, "run_command") as mock_run:
            check.run()
        assert not check.passed
        assert "label_selector is empty" in check._error
        mock_run.assert_not_called()

    def test_negative_replicas(self) -> None:
        check = K8sNodePoolCheck(config={"label_selector": "a=b", "expected_replicas": -1})
        with patch.object(check, "run_command") as mock_run:
            check.run()
        assert not check.passed
        assert ">= 0" in check._error
        mock_run.assert_not_called()

    def test_bad_labels_json_fails_fast(self) -> None:
        check = K8sNodePoolCheck(
            config={
                "label_selector": "a=b",
                "expected_replicas": 1,
                "expected_labels": "{not json",
            }
        )
        with patch.object(check, "run_command") as mock_run:
            check.run()
        assert not check.passed
        assert "valid JSON" in check._error
        mock_run.assert_not_called()
