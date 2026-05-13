# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Unit tests for ``isvtest.validations.k8s_api_network_acl``."""

from __future__ import annotations

import json
from typing import Any

import pytest

from isvtest.core.runners import CommandResult
from isvtest.utils.checks import truncate
from isvtest.validations.k8s_api_network_acl import K8sApiNetworkAclCheck


def _ok(stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(exit_code=0, stdout=stdout, stderr=stderr, duration=0.0)


def _fail(stdout: str = "", stderr: str = "", exit_code: int = 1) -> CommandResult:
    return CommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr, duration=0.0)


_DEFAULT_KUBECTL_SERVER = "https://api.example.com:6443"


def _config_view_json(server: str = _DEFAULT_KUBECTL_SERVER) -> str:
    """Return a minimal ``kubectl config view --minify -o json`` payload."""
    return json.dumps({"clusters": [{"cluster": {"server": server}}]})


def _minimal_config(**overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "commands": {
            "unauthorized_probe": "ssh ext-host curl --max-time 5 https://api.example.com:6443/healthz",
        },
    }
    cfg.update(overrides)
    return cfg


def _classify(cmd: str) -> str:
    if " config view " in cmd:
        return "config_view"
    if " get --raw " in cmd:
        return "authorized"
    if cmd.startswith("ssh ext-host"):
        return "unauthorized"
    return "unknown"


def _make_fake(
    *,
    auth_result: CommandResult | None = None,
    unauth_result: CommandResult | None = None,
    config_view_result: CommandResult | None = None,
    calls: list[str] | None = None,
):
    """Build a fake ``run_command`` that classifies by command shape."""
    auth = auth_result if auth_result is not None else _ok(stdout="ok")
    unauth = unauth_result if unauth_result is not None else _fail(stderr="connection timed out")
    cv = config_view_result if config_view_result is not None else _ok(stdout=_config_view_json())

    def fake(cmd: str, *_: Any, **__: Any) -> CommandResult:
        if calls is not None:
            calls.append(cmd)
        kind = _classify(cmd)
        if kind == "authorized":
            return auth
        if kind == "unauthorized":
            return unauth
        if kind == "config_view":
            return cv
        raise AssertionError(f"unexpected {cmd}")

    return fake


class TestInputValidation:
    def test_unconfigured_probe_skips(self) -> None:
        check = K8sApiNetworkAclCheck(config={})

        with pytest.raises(pytest.skip.Exception) as exc_info:
            check.execute()

        assert "network ACL probe is not configured" in str(exc_info.value)

    def test_non_integer_probe_timeout_rejected(self) -> None:
        check = K8sApiNetworkAclCheck(config=_minimal_config(probe_timeout_s="ten"))
        check.run()
        assert not check.passed
        assert "`probe_timeout_s` must be an integer" in check.message

    def test_bool_probe_timeout_rejected(self) -> None:
        check = K8sApiNetworkAclCheck(config=_minimal_config(probe_timeout_s=True))
        check.run()
        assert not check.passed
        assert "must be an integer, got bool" in check.message

    def test_invalid_api_health_path_rejected(self) -> None:
        check = K8sApiNetworkAclCheck(config=_minimal_config(api_health_path="readyz"))
        check.run()
        assert not check.passed
        assert "`api_health_path` must be an absolute API path string" in check.message

    def test_invalid_commands_rejected(self) -> None:
        check = K8sApiNetworkAclCheck(config={"commands": ["ssh ext-host curl"]})
        check.run()
        assert not check.passed
        assert "`commands` must be a mapping" in check.message

    def test_empty_authorized_probe_rejected(self) -> None:
        cfg = _minimal_config()
        cfg["commands"]["authorized_probe"] = "  "
        check = K8sApiNetworkAclCheck(config=cfg)
        check.run()
        assert not check.passed
        assert "`commands.authorized_probe` must be a non-empty string" in check.message

    def test_non_string_command_value_rejected(self) -> None:
        check = K8sApiNetworkAclCheck(config={"commands": {"authorized_probe": ["a", "b"]}})
        check.run()
        assert not check.passed
        assert "`commands.authorized_probe` must be a string, got list" in check.message

    @pytest.mark.parametrize(
        ("key", "value", "type_name"),
        [
            ("authorized_probe", False, "bool"),
            ("authorized_probe", 0, "int"),
            ("authorized_probe", [], "list"),
            ("unauthorized_probe", False, "bool"),
            ("unauthorized_probe", 0, "int"),
            ("unauthorized_probe", [], "list"),
        ],
    )
    def test_falsey_non_string_command_value_rejected(self, key: str, value: Any, type_name: str) -> None:
        cfg = _minimal_config()
        cfg["commands"][key] = value
        check = K8sApiNetworkAclCheck(config=cfg)

        try:
            check.run()
        except pytest.skip.Exception as exc:
            pytest.fail(f"invalid command value skipped instead of failing: {exc}")

        assert not check.passed
        assert f"`commands.{key}` must be a string, got {type_name}" in check.message

    def test_non_string_api_endpoint_rejected(self) -> None:
        check = K8sApiNetworkAclCheck(config=_minimal_config(api_endpoint=123))
        check.run()
        assert not check.passed
        assert "`api_endpoint` must be a string" in check.message

    def test_api_endpoint_without_scheme_rejected(self) -> None:
        check = K8sApiNetworkAclCheck(config=_minimal_config(api_endpoint="api.example.com:6443"))
        check.run()
        assert not check.passed
        assert "must start with 'https://'" in check.message

    def test_api_endpoint_with_http_scheme_rejected(self) -> None:
        """Kubernetes API is HTTPS-only; reject `http://` rather than letting
        it slip through and trigger a confusing consistency mismatch later.
        """
        check = K8sApiNetworkAclCheck(config=_minimal_config(api_endpoint="http://api.example.com:6443"))
        check.run()
        assert not check.passed
        assert "must start with 'https://'" in check.message
        assert "HTTPS-only" in check.message

    def test_api_endpoint_without_host_rejected(self) -> None:
        check = K8sApiNetworkAclCheck(config=_minimal_config(api_endpoint="https://"))
        check.run()
        assert not check.passed
        assert "must include a host" in check.message

    def test_api_endpoint_with_invalid_port_rejected(self) -> None:
        check = K8sApiNetworkAclCheck(config=_minimal_config(api_endpoint="https://api.example.com:notaport"))
        check.run()
        assert not check.passed
        assert "`api_endpoint` must include a valid HTTPS scheme, host, and port" in check.message

    def test_empty_api_endpoint_treated_as_unset(self) -> None:
        check = K8sApiNetworkAclCheck(config=_minimal_config(api_endpoint="   "))
        check.run_command = _make_fake()  # type: ignore[assignment]
        check.run()
        assert check.passed, check.message

    def test_non_bool_expect_separate_rejected(self) -> None:
        check = K8sApiNetworkAclCheck(config=_minimal_config(expect_separate_endpoints="yes"))
        check.run()
        assert not check.passed
        assert "`expect_separate_endpoints` must be a boolean" in check.message


class TestAuthorizedProbe:
    def test_authorized_probe_uses_kubectl_for_reviewed_cluster(self) -> None:
        check = K8sApiNetworkAclCheck(config=_minimal_config())
        calls: list[str] = []
        check.run_command = _make_fake(calls=calls)  # type: ignore[assignment]
        check.run()
        assert check.passed, check.message
        assert calls[0].endswith(" get --raw /readyz")

    def test_api_health_path_overrides_default_authorized_probe_path(self) -> None:
        check = K8sApiNetworkAclCheck(config=_minimal_config(api_health_path="/livez"))
        calls: list[str] = []
        check.run_command = _make_fake(calls=calls)  # type: ignore[assignment]
        check.run()
        assert check.passed, check.message
        assert calls[0].endswith(" get --raw /livez")

    def test_authorized_probe_command_override(self) -> None:
        cfg = _minimal_config()
        cfg["commands"]["authorized_probe"] = "custom-kubectl get --raw /healthz"
        check = K8sApiNetworkAclCheck(config=cfg)
        calls: list[str] = []

        def fake(cmd: str, *a: Any, **kw: Any) -> CommandResult:
            calls.append(cmd)
            if cmd == "custom-kubectl get --raw /healthz":
                return _ok(stdout="ok")
            if _classify(cmd) == "unauthorized":
                return _fail(stderr="connection timed out")
            raise AssertionError(f"unexpected {cmd}")

        check.run_command = fake  # type: ignore[assignment]
        check.run()
        assert check.passed, check.message
        assert calls[0] == "custom-kubectl get --raw /healthz"

    def test_custom_authorized_probe_skips_kubectl_url_derivation(self) -> None:
        """When the user overrides authorized_probe, we cannot infer its target
        from kubeconfig - so don't run `kubectl config view` and don't include
        a kubectl URL in the result message.
        """
        cfg = _minimal_config()
        cfg["commands"]["authorized_probe"] = "custom-cmd"
        check = K8sApiNetworkAclCheck(config=cfg)
        calls: list[str] = []

        def fake(cmd: str, *a: Any, **kw: Any) -> CommandResult:
            calls.append(cmd)
            if cmd == "custom-cmd":
                return _ok(stdout="ok")
            if _classify(cmd) == "unauthorized":
                return _fail(stderr="connection timed out")
            raise AssertionError(f"unexpected {cmd}")

        check.run_command = fake  # type: ignore[assignment]
        check.run()
        assert check.passed, check.message
        assert not any(_classify(c) == "config_view" for c in calls)
        assert "authorized target (kubectl)" not in check.message

    def test_authorized_failure_aborts_with_baseline_message(self) -> None:
        """A failing authorized probe means the API is unreachable even from
        the configured cluster context. We cannot then assert anything about
        ACLs because a failing unauthorized probe could equally mean "ACL
        works" or "API dead".
        """
        check = K8sApiNetworkAclCheck(config=_minimal_config())
        calls: list[str] = []
        check.run_command = _make_fake(  # type: ignore[assignment]
            auth_result=_fail(stderr="Unable to connect to the server"),
            calls=calls,
        )
        check.run()
        assert not check.passed
        assert "Authorized probe failed" in check.message
        assert "could" in check.message and "mean" in check.message
        assert not any(_classify(c) == "unauthorized" for c in calls)

    def test_authorized_probe_runs_before_unauthorized(self) -> None:
        """Ordering matters: the baseline check must run before the
        unauthorized probe, otherwise a slow/long-timeout unauthorized probe
        wastes time on a check that was already doomed.
        """
        check = K8sApiNetworkAclCheck(config=_minimal_config())
        order: list[str] = []

        def fake(cmd: str, *a: Any, **kw: Any) -> CommandResult:
            kind = _classify(cmd)
            if kind == "authorized":
                order.append("auth")
                return _ok(stdout="ok")
            if kind == "config_view":
                return _ok(stdout=_config_view_json())
            if kind == "unauthorized":
                order.append("unauth")
                return _fail(stderr="timed out")
            raise AssertionError(f"unexpected {cmd}")

        check.run_command = fake  # type: ignore[assignment]
        check.run()
        assert check.passed, check.message
        assert order == ["auth", "unauth"]


class TestUnauthorizedProbe:
    def test_passes_when_unauthorized_probe_fails(self) -> None:
        check = K8sApiNetworkAclCheck(config=_minimal_config())
        check.run_command = _make_fake(  # type: ignore[assignment]
            unauth_result=_fail(stderr="connection refused", exit_code=7),
        )
        check.run()
        assert check.passed, check.message
        assert "network ACL verified" in check.message
        assert "exit=7" in check.message

    def test_passes_when_unauthorized_probe_times_out(self) -> None:
        """A timeout surfaces as a non-zero exit from the runner and counts as
        a valid "blocked" outcome.
        """
        check = K8sApiNetworkAclCheck(config=_minimal_config())
        check.run_command = _make_fake(  # type: ignore[assignment]
            unauth_result=_fail(stderr="", exit_code=124),
        )
        check.run()
        assert check.passed, check.message

    def test_fails_when_unauthorized_probe_succeeds(self) -> None:
        check = K8sApiNetworkAclCheck(config=_minimal_config())
        check.run_command = _make_fake(  # type: ignore[assignment]
            unauth_result=_ok(stdout='{"healthy": true}'),
        )
        check.run()
        assert not check.passed
        assert "Unauthorized probe unexpectedly succeeded" in check.message
        assert "no network ACL is in place" in check.message

    def test_fails_when_unauthorized_probe_command_not_found(self) -> None:
        """Exit 127 must not be mistaken for an enforced ACL."""
        check = K8sApiNetworkAclCheck(config=_minimal_config())
        check.run_command = _make_fake(  # type: ignore[assignment]
            unauth_result=_fail(stderr="ssh: command not found", exit_code=127),
        )
        check.run()
        assert not check.passed
        assert "could not execute" in check.message
        assert "ssh: command not found" in check.message

    def test_fails_when_unauthorized_probe_not_executable(self) -> None:
        """Exit 126 must fail loudly too."""
        check = K8sApiNetworkAclCheck(config=_minimal_config())
        check.run_command = _make_fake(  # type: ignore[assignment]
            unauth_result=_fail(stderr="permission denied", exit_code=126),
        )
        check.run()
        assert not check.passed
        assert "could not execute" in check.message

    def test_probe_timeout_forwarded_to_run_command(self) -> None:
        """The configured ``probe_timeout_s`` must reach the runner so a hung
        unauthorized probe cannot freeze the check indefinitely.
        """
        check = K8sApiNetworkAclCheck(config=_minimal_config(probe_timeout_s=3))
        seen_timeouts: dict[str, int] = {}

        def fake(cmd: str, *a: Any, **kw: Any) -> CommandResult:
            timeout = kw.get("timeout") if "timeout" in kw else (a[0] if a else None)
            kind = _classify(cmd)
            if timeout is not None:
                seen_timeouts[kind] = timeout
            if kind == "authorized":
                return _ok(stdout="ok")
            if kind == "config_view":
                return _ok(stdout=_config_view_json())
            if kind == "unauthorized":
                return _fail(stderr="timed out", exit_code=124)
            raise AssertionError(f"unexpected {cmd}")

        check.run_command = fake  # type: ignore[assignment]
        check.run()
        assert check.passed, check.message
        assert seen_timeouts.get("authorized") == 3
        assert seen_timeouts.get("unauthorized") == 3
        assert seen_timeouts.get("config_view") == 3


class TestEndpointVisibility:
    def test_pass_message_includes_kubectl_target(self) -> None:
        check = K8sApiNetworkAclCheck(config=_minimal_config())
        check.run_command = _make_fake()  # type: ignore[assignment]
        check.run()
        assert check.passed, check.message
        assert "authorized target (kubectl): https://api.example.com:6443" in check.message

    def test_pass_message_includes_api_endpoint(self) -> None:
        check = K8sApiNetworkAclCheck(
            config=_minimal_config(api_endpoint="https://api.example.com:6443"),
        )
        check.run_command = _make_fake()  # type: ignore[assignment]
        check.run()
        assert check.passed, check.message
        assert "configured api_endpoint: https://api.example.com:6443" in check.message

    def test_unauthorized_succeeded_message_includes_targets(self) -> None:
        check = K8sApiNetworkAclCheck(
            config=_minimal_config(api_endpoint="https://api.example.com:6443"),
        )
        check.run_command = _make_fake(  # type: ignore[assignment]
            unauth_result=_ok(stdout="leaked"),
        )
        check.run()
        assert not check.passed
        assert "authorized target (kubectl)" in check.message
        assert "configured api_endpoint" in check.message

    def test_kubectl_url_derivation_failure_does_not_break_check(self) -> None:
        """`kubectl config view` failing is informational only - the check
        still completes and the kubectl line is simply omitted.
        """
        check = K8sApiNetworkAclCheck(config=_minimal_config())
        check.run_command = _make_fake(  # type: ignore[assignment]
            config_view_result=_fail(stderr="boom"),
        )
        check.run()
        assert check.passed, check.message
        assert "authorized target (kubectl)" not in check.message

    def test_kubectl_url_invalid_json_does_not_break_check(self) -> None:
        check = K8sApiNetworkAclCheck(config=_minimal_config())
        check.run_command = _make_fake(  # type: ignore[assignment]
            config_view_result=_ok(stdout="not-json"),
        )
        check.run()
        assert check.passed, check.message
        assert "authorized target (kubectl)" not in check.message

    @pytest.mark.parametrize(
        "payload",
        [
            {"clusters": {}},
            {"clusters": []},
            {"clusters": ["not-a-cluster"]},
        ],
    )
    def test_kubectl_url_malformed_clusters_do_not_break_check(self, payload: dict[str, Any]) -> None:
        check = K8sApiNetworkAclCheck(config=_minimal_config())
        check.run_command = _make_fake(  # type: ignore[assignment]
            config_view_result=_ok(stdout=json.dumps(payload)),
        )
        check.run()
        assert check.passed, check.message
        assert "authorized target (kubectl)" not in check.message


class TestEndpointConsistency:
    def test_unauth_probe_must_reference_api_endpoint_origin(self) -> None:
        cfg = _minimal_config(api_endpoint="https://api.example.com:6443")
        cfg["commands"]["unauthorized_probe"] = "ssh ext-host curl https://192.0.2.1:6443/healthz"
        check = K8sApiNetworkAclCheck(config=cfg)
        check.run()
        assert not check.passed
        assert "does not reference the configured `api_endpoint` origin 'https://api.example.com:6443'" in check.message
        assert "expect_separate_endpoints: true" in check.message

    def test_unauth_probe_origin_match_is_case_insensitive(self) -> None:
        cfg = _minimal_config(api_endpoint="https://API.example.com:6443")
        cfg["commands"]["unauthorized_probe"] = "ssh ext-host curl https://api.example.com:6443/healthz"
        check = K8sApiNetworkAclCheck(config=cfg)
        check.run_command = _make_fake()  # type: ignore[assignment]
        check.run()
        assert check.passed, check.message

    def test_unauth_probe_https_default_port_matches_explicit_443(self) -> None:
        cfg = _minimal_config(api_endpoint="https://api.example.com:443")
        cfg["commands"]["unauthorized_probe"] = "ssh ext-host curl https://api.example.com/healthz"
        check = K8sApiNetworkAclCheck(config=cfg)
        check.run_command = _make_fake(  # type: ignore[assignment]
            config_view_result=_ok(stdout=_config_view_json("https://api.example.com")),
        )
        check.run()
        assert check.passed, check.message

    def test_unauth_probe_port_mismatch_is_rejected(self) -> None:
        cfg = _minimal_config(api_endpoint="https://api.example.com:6443")
        cfg["commands"]["unauthorized_probe"] = "ssh ext-host curl https://api.example.com:443/healthz"
        check = K8sApiNetworkAclCheck(config=cfg)
        check.run()
        assert not check.passed
        assert "does not reference the configured `api_endpoint` origin 'https://api.example.com:6443'" in check.message

    def test_unauth_probe_scheme_mismatch_is_rejected(self) -> None:
        cfg = _minimal_config(api_endpoint="https://api.example.com:6443")
        cfg["commands"]["unauthorized_probe"] = "ssh ext-host curl http://api.example.com:6443/healthz"
        check = K8sApiNetworkAclCheck(config=cfg)
        check.run()
        assert not check.passed
        assert "does not reference the configured `api_endpoint` origin 'https://api.example.com:6443'" in check.message

    def test_unauth_probe_partial_host_substring_does_not_match(self) -> None:
        """A probe targeting `my-api.example.com` must NOT satisfy a check
        configured for `api.example.com` - the old substring match would have
        accepted it and silently let a wrong-target probe pass.
        """
        cfg = _minimal_config(api_endpoint="https://api.example.com:6443")
        cfg["commands"]["unauthorized_probe"] = "ssh ext-host curl --max-time 5 https://my-api.example.com:6443/healthz"
        check = K8sApiNetworkAclCheck(config=cfg)
        check.run()
        assert not check.passed
        assert "does not reference the configured `api_endpoint` origin 'https://api.example.com:6443'" in check.message

    def test_unauth_probe_host_in_ssh_user_does_not_match(self) -> None:
        """If the configured host only appears as part of an SSH `user@host`
        argument and not as the URL host being probed, the URL-aware match
        rejects it - SSHing to a host named after the API doesn't probe the API.
        """
        cfg = _minimal_config(api_endpoint="https://api.example.com:6443")
        cfg["commands"]["unauthorized_probe"] = "ssh user@api.example.com.lab whoami"
        check = K8sApiNetworkAclCheck(config=cfg)
        check.run()
        assert not check.passed
        assert "does not reference the configured `api_endpoint` origin" in check.message

    def test_unauth_substring_check_skipped_when_expect_separate(self) -> None:
        cfg = _minimal_config(
            api_endpoint="https://api.example.com:6443",
            expect_separate_endpoints=True,
        )
        cfg["commands"]["unauthorized_probe"] = "ssh ext-host curl https://public.example.com/healthz"
        check = K8sApiNetworkAclCheck(config=cfg)
        check.run_command = _make_fake()  # type: ignore[assignment]
        check.run()
        assert check.passed, check.message

    def test_kubectl_url_must_match_api_endpoint(self) -> None:
        check = K8sApiNetworkAclCheck(
            config=_minimal_config(api_endpoint="https://api.example.com:6443"),
        )
        calls: list[str] = []
        check.run_command = _make_fake(  # type: ignore[assignment]
            config_view_result=_ok(stdout=_config_view_json("https://other.example.com:6443")),
            calls=calls,
        )
        check.run()
        assert not check.passed
        assert "does not match the configured endpoint" in check.message
        assert "expect_separate_endpoints: true" in check.message
        # Unauth probe must NOT have run when consistency check fails.
        assert not any(_classify(c) == "unauthorized" for c in calls)

    def test_kubectl_url_port_mismatch_is_treated_as_mismatch(self) -> None:
        check = K8sApiNetworkAclCheck(
            config=_minimal_config(api_endpoint="https://api.example.com:6443"),
        )
        check.run_command = _make_fake(  # type: ignore[assignment]
            config_view_result=_ok(stdout=_config_view_json("https://api.example.com:443")),
        )
        check.run()
        assert not check.passed
        assert "does not match the configured endpoint" in check.message

    def test_kubectl_url_default_https_port_matches_explicit_443(self) -> None:
        cfg = _minimal_config(api_endpoint="https://api.example.com:443")
        cfg["commands"]["unauthorized_probe"] = "ssh ext-host curl https://api.example.com/healthz"
        check = K8sApiNetworkAclCheck(config=cfg)
        check.run_command = _make_fake(  # type: ignore[assignment]
            config_view_result=_ok(stdout=_config_view_json("https://api.example.com")),
        )
        check.run()
        assert check.passed, check.message

    def test_kubectl_consistency_skipped_when_expect_separate(self) -> None:
        check = K8sApiNetworkAclCheck(
            config=_minimal_config(
                api_endpoint="https://api.example.com:6443",
                expect_separate_endpoints=True,
            ),
        )
        check.run_command = _make_fake(  # type: ignore[assignment]
            config_view_result=_ok(stdout=_config_view_json("https://private.example.com:6443")),
        )
        check.run()
        assert check.passed, check.message

    def test_kubectl_consistency_skipped_when_derivation_fails(self) -> None:
        """If kubectl URL can't be derived, we don't have enough info to
        enforce - degrade to visibility-only behavior, don't false-fail.
        """
        check = K8sApiNetworkAclCheck(
            config=_minimal_config(api_endpoint="https://api.example.com:6443"),
        )
        check.run_command = _make_fake(  # type: ignore[assignment]
            config_view_result=_fail(stderr="boom"),
        )
        check.run()
        assert check.passed, check.message

    def test_kubectl_url_empty_stdout_treated_as_undeterminable(self) -> None:
        """`kubectl config view` returning empty stdout (zero exit) must be
        treated identically to a failed lookup - we don't have a server URL
        to compare against, so consistency is degraded rather than enforced.
        """
        check = K8sApiNetworkAclCheck(
            config=_minimal_config(api_endpoint="https://api.example.com:6443"),
        )
        check.run_command = _make_fake(  # type: ignore[assignment]
            config_view_result=_ok(stdout="   "),
        )
        check.run()
        assert check.passed, check.message
        assert "authorized target (kubectl)" not in check.message

    def test_expect_separate_with_no_api_endpoint_is_noop(self) -> None:
        """`expect_separate_endpoints: true` without `api_endpoint` set has
        nothing to skip - both consistency checks are already inert because
        `api_endpoint` is None. Pin the behavior so a future refactor doesn't
        accidentally make the flag fail-closed when there is nothing to compare.
        """
        check = K8sApiNetworkAclCheck(config=_minimal_config(expect_separate_endpoints=True))
        check.run_command = _make_fake()  # type: ignore[assignment]
        check.run()
        assert check.passed, check.message


class TestUnauthorizedProbeMessageTargets:
    def test_could_not_execute_message_includes_targets(self) -> None:
        """126/127 failure messages must include the target context that pass
        and unauth-succeeded messages already surface, so a debugging operator
        sees which endpoint the broken probe was supposed to reach.
        """
        check = K8sApiNetworkAclCheck(
            config=_minimal_config(api_endpoint="https://api.example.com:6443"),
        )
        check.run_command = _make_fake(  # type: ignore[assignment]
            unauth_result=_fail(stderr="ssh: command not found", exit_code=127),
        )
        check.run()
        assert not check.passed
        assert "could not execute" in check.message
        assert "authorized target (kubectl)" in check.message
        assert "configured api_endpoint" in check.message


class TestTruncate:
    def test_short_text_returned_unchanged(self) -> None:
        assert truncate("short") == "short"

    def test_long_text_ellipsized_at_limit(self) -> None:
        long = "a" * 120
        out = truncate(long, limit=80)
        assert len(out) == 80
        assert out.endswith("...")
