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

"""Verify the Kubernetes API endpoint is protected by network access controls."""

from __future__ import annotations

import re
from typing import Any, ClassVar
from urllib.parse import urlsplit

import pytest

from isvtest.core.k8s import KubectlParseError, get_kubectl_base_shell, parse_kubectl_json
from isvtest.core.validation import BaseValidation
from isvtest.utils.checks import truncate

_DEFAULT_API_HEALTH_PATH = "/readyz"
_DEFAULT_PROBE_TIMEOUT_S = 10
_DEFAULT_HTTP_PORTS = {"http": 80, "https": 443}


def _normalized_http_origin(url: str) -> tuple[str, str, int] | None:
    """Return normalized ``(scheme, host, port)`` for an HTTP(S) URL."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in _DEFAULT_HTTP_PORTS or not host:
        return None
    try:
        port = parts.port if parts.port is not None else _DEFAULT_HTTP_PORTS[scheme]
    except ValueError:
        return None
    return scheme, host, port


def _format_origin(origin: tuple[str, str, int]) -> str:
    """Render a normalized HTTP(S) origin for diagnostics."""
    scheme, host, port = origin
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{scheme}://{host}:{port}"


class K8sApiNetworkAclCheck(BaseValidation):
    """Verify the reviewed cluster's Kubernetes API endpoint enforces network ACLs."""

    description: ClassVar[str] = "Verify the Kubernetes API endpoint is protected by network access controls."
    timeout: ClassVar[int] = 120
    labels: ClassVar[tuple[str, ...]] = ("kubernetes",)

    def run(self) -> None:
        """Execute the authorized baseline probe and unauthorized probe flow."""
        cfg = self._parse_config()
        if cfg is None:
            return

        using_default_auth_probe = cfg["authorized_probe_cmd"] is None
        authorized_probe_cmd = cfg["authorized_probe_cmd"] or get_kubectl_base_shell(
            "get",
            "--raw",
            cfg["api_health_path"],
        )
        if not self._run_authorized_probe(authorized_probe_cmd, cfg["probe_timeout_s"]):
            return

        # Only meaningful when the default kubectl-based auth probe was used;
        # a custom authorized_probe may not even use kubectl, so its target
        # cannot be inferred from kubeconfig.
        kubectl_server_url = (
            self._derive_kubectl_server_url(cfg["probe_timeout_s"]) if using_default_auth_probe else None
        )

        if not self._enforce_endpoint_consistency(
            api_endpoint=cfg["api_endpoint"],
            kubectl_server_url=kubectl_server_url,
            expect_separate=cfg["expect_separate_endpoints"],
        ):
            return

        self._run_unauthorized_probe(
            unauthorized_probe_cmd=cfg["unauthorized_probe_cmd"],
            probe_timeout_s=cfg["probe_timeout_s"],
            api_endpoint=cfg["api_endpoint"],
            kubectl_server_url=kubectl_server_url,
        )

    def _parse_config(self) -> dict[str, Any] | None:
        """Validate and normalize check configuration, or ``None`` after calling ``set_failed``."""
        raw_commands = self.config.get("commands", {})
        if not isinstance(raw_commands, dict):
            self.set_failed(
                f"`commands` must be a mapping, got {type(raw_commands).__name__}: "
                f"{raw_commands!r}. Example: `commands: {{unauthorized_probe: 'ssh external-host curl ...'}}`."
            )
            return None

        commands: dict[str, str] = {}
        for key, value in raw_commands.items():
            if value is None:
                continue
            if not isinstance(value, str):
                self.set_failed(f"`commands.{key}` must be a string, got {type(value).__name__}: {value!r}.")
                return None
            if not value:
                continue
            commands[str(key)] = value

        authorized = commands.get("authorized_probe")
        if authorized is not None and not authorized.strip():
            self.set_failed("`commands.authorized_probe` must be a non-empty string when set.")
            return None

        unauthorized = commands.get("unauthorized_probe")
        if unauthorized is None or not unauthorized.strip():
            pytest.skip(
                "Kubernetes API network ACL probe is not configured; provide "
                "`commands.unauthorized_probe` with a shell command expected "
                "to FAIL or time out because the source network is not allow-listed, e.g. "
                "`ssh external-host curl --max-time 5 https://<endpoint>:6443/healthz`)."
            )

        probe_timeout_s = self._parse_positive_int("probe_timeout_s", default=_DEFAULT_PROBE_TIMEOUT_S)
        if probe_timeout_s is None:
            return None

        api_health_path = self.config.get("api_health_path", _DEFAULT_API_HEALTH_PATH)
        if not isinstance(api_health_path, str) or not api_health_path.startswith("/"):
            self.set_failed(
                f"`api_health_path` must be an absolute API path string, got "
                f"{type(api_health_path).__name__}: {api_health_path!r}"
            )
            return None

        api_endpoint, ok = self._parse_api_endpoint()
        if not ok:
            return None

        expect_separate, ok = self._parse_expect_separate()
        if not ok:
            return None

        if (
            api_endpoint
            and not expect_separate
            and not self._unauthorized_targets_api_endpoint(
                unauthorized=unauthorized,
                api_endpoint=api_endpoint,
            )
        ):
            return None

        return {
            "authorized_probe_cmd": authorized,
            "unauthorized_probe_cmd": unauthorized,
            "probe_timeout_s": probe_timeout_s,
            "api_health_path": api_health_path,
            "api_endpoint": api_endpoint,
            "expect_separate_endpoints": expect_separate,
        }

    def _parse_api_endpoint(self) -> tuple[str | None, bool]:
        """Validate optional ``api_endpoint`` config.

        Returns ``(value_or_none, ok)``. ``ok`` is ``False`` only when the
        field is present but malformed; in that case ``set_failed`` has
        already been called.
        """
        raw = self.config.get("api_endpoint")
        if raw is None:
            return None, True
        if not isinstance(raw, str):
            self.set_failed(f"`api_endpoint` must be a string, got {type(raw).__name__}: {raw!r}")
            return None, False
        value = raw.strip()
        if not value:
            return None, True
        if not value.startswith("https://"):
            self.set_failed(f"`api_endpoint` must start with 'https://' (Kubernetes API is HTTPS-only), got {value!r}")
            return None, False
        if not urlsplit(value).hostname:
            self.set_failed(f"`api_endpoint` must include a host, got {value!r}")
            return None, False
        if _normalized_http_origin(value) is None:
            self.set_failed(f"`api_endpoint` must include a valid HTTPS scheme, host, and port, got {value!r}")
            return None, False
        return value, True

    def _parse_expect_separate(self) -> tuple[bool, bool]:
        """Validate optional ``expect_separate_endpoints`` flag.

        Returns ``(value, ok)``. Defaults to ``False`` (consistency enforced).
        """
        raw = self.config.get("expect_separate_endpoints", False)
        if isinstance(raw, bool):
            return raw, True
        self.set_failed(f"`expect_separate_endpoints` must be a boolean, got {type(raw).__name__}: {raw!r}")
        return False, False

    def _unauthorized_targets_api_endpoint(self, *, unauthorized: str, api_endpoint: str) -> bool:
        """Check the unauth probe references the configured ``api_endpoint`` origin.

        A typo'd or stale unauth target (e.g. an unrouted IP) trivially
        fails to connect and would otherwise be misread as "ACL enforced".
        We extract every ``http(s)://...`` URL from the probe string and
        compare its normalized scheme/host/port to the configured
        ``api_endpoint`` origin - matching at origin boundaries rather than
        substring avoids false positives like ``my-api.test`` matching
        ``api.test``, an SSH user happening to contain the host string, or a
        probe hitting the right host but wrong API port.
        """
        api_origin = _normalized_http_origin(api_endpoint)
        if api_origin is None:
            return True
        probe_urls = re.findall(r"https?://[^\s'\"<>]+", unauthorized)
        for url in probe_urls:
            if _normalized_http_origin(url) == api_origin:
                return True
        api_origin_text = _format_origin(api_origin)
        self.set_failed(
            f"`commands.unauthorized_probe` does not reference the configured "
            f"`api_endpoint` origin {api_origin_text!r}: probe is {truncate(unauthorized)!r}. "
            f"A probe that targets a different scheme, host, or port can fail "
            f"trivially (DNS, TLS, unrouted IP) and be misread as "
            f"'ACL enforced'. Either change the unauth probe to target {api_endpoint!r}, "
            f"or set `expect_separate_endpoints: true` if the probe is intentionally "
            f"hitting a different (e.g. public) hostname."
        )
        return False

    def _enforce_endpoint_consistency(
        self,
        *,
        api_endpoint: str | None,
        kubectl_server_url: str | None,
        expect_separate: bool,
    ) -> bool:
        """Verify the auth-probe target and the configured ``api_endpoint`` agree.

        Returns ``True`` when consistency holds, when one side is unknown,
        or when the user has acknowledged intentional separation. Returns
        ``False`` after ``set_failed`` when both targets are known and
        differ - the unauth probe result against a different endpoint than
        the auth probe baselined would not be interpretable.
        """
        if expect_separate or not api_endpoint or not kubectl_server_url:
            return True
        api_origin = _normalized_http_origin(api_endpoint)
        k_origin = _normalized_http_origin(kubectl_server_url)
        if api_origin is None or k_origin is None:
            return True
        if api_origin == k_origin:
            return True
        self.set_failed(
            f"Authorized probe targets {kubectl_server_url!r} (kubectl), but "
            f"`api_endpoint` is {api_endpoint!r}. The auth baseline does not "
            f"match the configured endpoint, so the unauthorized probe result "
            f"would not be meaningful. Either align the kubeconfig with "
            f"`api_endpoint`, or set `expect_separate_endpoints: true` if the "
            f"auth path is intentionally different (e.g. private link)."
        )
        return False

    def _derive_kubectl_server_url(self, probe_timeout_s: int) -> str | None:
        """Best-effort: extract the API server URL kubectl is currently pointed at.

        Used for consistency checks and reviewer-visible reporting. Returns
        ``None`` on failure - this is informational and must never fail the
        check on its own; the auth probe already established that kubectl
        works.
        """
        cmd = get_kubectl_base_shell("config", "view", "--minify", "-o", "json")
        result = self.run_command(cmd, timeout=probe_timeout_s)
        if result.exit_code != 0:
            return None
        try:
            payload = parse_kubectl_json(result, "kubectl config view")
        except KubectlParseError as exc:
            self.log.warning("Failed to parse kubectl config JSON: %s", exc)
            return None
        clusters = payload.get("clusters")
        if not isinstance(clusters, list) or not clusters or not isinstance(clusters[0], dict):
            return None
        server = ((clusters[0].get("cluster") or {}).get("server")) or ""
        return server.strip() or None

    def _run_authorized_probe(self, authorized_probe_cmd: str, probe_timeout_s: int) -> bool:
        """Run the authorized baseline probe and report failure on non-zero exit.

        Returns ``True`` if the probe succeeded, or ``False`` after calling
        ``set_failed`` - a failing baseline makes the unauthorized-probe result
        ambiguous, so the check must stop before interpreting it.
        """
        result = self.run_command(authorized_probe_cmd, timeout=probe_timeout_s)
        if result.exit_code != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.exit_code}"
            snippet = truncate(authorized_probe_cmd)
            self.set_failed(
                f"Authorized probe failed (cmd: {snippet}): {detail}. A failing "
                f"baseline makes the unauthorized-probe result unreliable (could "
                f"mean 'ACL works' OR 'API is down'). Fix cluster access first, "
                f"then re-run."
            )
            return False
        return True

    def _run_unauthorized_probe(
        self,
        *,
        unauthorized_probe_cmd: str,
        probe_timeout_s: int,
        api_endpoint: str | None,
        kubectl_server_url: str | None,
    ) -> None:
        """Run the unauthorized probe and set the final pass/fail verdict.

        A non-zero exit (connection blocked or timeout) means the ACL is
        enforced; a zero exit means the endpoint is reachable from a source
        that should be blocked and the check fails.
        """
        result = self.run_command(unauthorized_probe_cmd, timeout=probe_timeout_s)
        snippet = truncate(unauthorized_probe_cmd)
        targets = self._format_targets(api_endpoint, kubectl_server_url)

        # 126/127 are shell conventions for "not executable" / "command not
        # found". Treating them as an ACL-enforced pass would hide a broken
        # probe and yield false assurance.
        if result.exit_code in (126, 127):
            detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.exit_code}"
            self.set_failed(
                f"Unauthorized probe could not execute (cmd: {snippet}): "
                f"{detail}. Fix the probe tooling/command and re-run.{targets}"
            )
            return

        if result.exit_code == 0:
            preview = result.stdout.strip() or result.stderr.strip() or "(no output)"
            preview = truncate(preview, limit=120)
            self.set_failed(
                f"Unauthorized probe unexpectedly succeeded: the API endpoint "
                f"is reachable from a source that should be "
                f"blocked, so no network ACL is in place. Probe cmd: {snippet}. "
                f"Probe output: {preview!r}.{targets}"
            )
            return

        self.set_passed(
            f"API endpoint blocked the unauthorized probe (exit={result.exit_code}) "
            f"and served the authorized probe - network ACL verified.{targets}"
        )

    @staticmethod
    def _format_targets(api_endpoint: str | None, kubectl_server_url: str | None) -> str:
        """Render the auth/unauth probe targets for inclusion in result messages."""
        parts: list[str] = []
        if kubectl_server_url:
            parts.append(f"authorized target (kubectl): {kubectl_server_url}")
        if api_endpoint:
            parts.append(f"configured api_endpoint: {api_endpoint}")
        if not parts:
            return ""
        return " (" + "; ".join(parts) + ")"
