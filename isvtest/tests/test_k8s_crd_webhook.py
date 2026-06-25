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

"""Unit tests for ``isvtest.validations.k8s_crd_webhook``."""

from __future__ import annotations

import json
import subprocess
from base64 import b64decode
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import yaml
from cryptography import x509

from isvtest.core.runners import CommandResult
from isvtest.validations.k8s_crd_webhook import K8sCrdWebhookCheck


def _ok(stdout: str = "", stderr: str = "") -> CommandResult:
    """Return a successful ``CommandResult`` with the given stdout/stderr."""
    return CommandResult(exit_code=0, stdout=stdout, stderr=stderr, duration=0.0)


def _fail(stdout: str = "", stderr: str = "", exit_code: int = 1) -> CommandResult:
    """Return a failing ``CommandResult`` with the given output and exit code."""
    return CommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr, duration=0.0)


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    """Return a ``subprocess.CompletedProcess`` shaped like ``kubectl apply``."""
    return subprocess.CompletedProcess(
        args=["kubectl", "apply", "-f", "-"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _docs(manifest: str) -> list[dict[str, Any]]:
    """Parse the Kubernetes docs sent to ``kubectl apply -f -``."""
    return [doc for doc in yaml.safe_load_all(manifest) if isinstance(doc, dict)]


def _custom_resource_json(*, mutated: bool = True) -> str:
    """Return a custom resource payload as read back from kubectl."""
    data = {"mutation-start": "yes"}
    if mutated:
        data["mutation-stage-1"] = "yes"
    return json.dumps({"data": data})


def _patch_kubectl() -> Any:
    """Patch kubectl command discovery for deterministic unit tests."""
    return patch.multiple(
        "isvtest.validations.k8s_crd_webhook",
        get_kubectl_command=lambda: ["kubectl"],
        get_kubectl_base_shell=lambda *args: " ".join(("kubectl", *args)) if args else "kubectl",
    )


def _run_check(
    *,
    apply_func: Callable[..., subprocess.CompletedProcess[str]],
    run_command: Callable[..., CommandResult],
    config: dict[str, Any] | None = None,
) -> K8sCrdWebhookCheck:
    """Run ``K8sCrdWebhookCheck`` with kubectl and sleep mocked out."""
    check = K8sCrdWebhookCheck(
        config={
            "image": "registry.k8s.io/e2e-test-images/agnhost:2.47",
            "namespace_prefix": "isvtest-crd-webhook",
            "wait_timeout": 1,
            "poll_interval": 1,
            **(config or {}),
        }
    )
    with (
        _patch_kubectl(),
        patch("isvtest.validations.k8s_crd_webhook.subprocess.run", side_effect=apply_func),
        patch("isvtest.validations.k8s_crd_webhook.time.sleep"),
        patch.object(check, "run_command", side_effect=run_command),
    ):
        check.run()
    return check


def test_crd_webhook_check_passes_with_expected_manifests_and_probes() -> None:
    """Pass path applies webhook infrastructure, verifies mutation and rejection, and cleans up."""
    applied_docs: list[dict[str, Any]] = []
    admission_apply_timeouts: list[float] = []
    commands: list[str] = []

    def fake_apply(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert args == ["kubectl", "apply", "-f", "-"]
        docs = _docs(kwargs["input"])
        applied_docs.extend(docs)
        if any(
            (doc.get("metadata") or {}).get("labels", {}).get("webhook-e2e-test") == "webhook-disallow"
            or doc.get("kind") == "CrdWebhookWidget"
            for doc in docs
        ):
            admission_apply_timeouts.append(kwargs["timeout"])
        if any(
            (doc.get("metadata") or {}).get("labels", {}).get("webhook-e2e-test") == "webhook-disallow" for doc in docs
        ):
            return _completed(returncode=1, stderr="the crd contains unwanted label")
        if any((doc.get("data") or {}).get("webhook-e2e-test") == "webhook-disallow" for doc in docs):
            return _completed(returncode=1, stderr="the custom resource contains unwanted data")
        return _completed()

    def fake_run_command(cmd: str, *_args: Any, **_kwargs: Any) -> CommandResult:
        commands.append(cmd)
        if " get " in cmd and " -o json" in cmd:
            return _ok(stdout=_custom_resource_json())
        return _ok()

    check = _run_check(apply_func=fake_apply, run_command=fake_run_command)

    assert check.passed
    assert "CRD admission and custom resource webhook mutation/validation verified" in check.message
    kinds = {doc["kind"] for doc in applied_docs}
    assert {
        "Secret",
        "Service",
        "Deployment",
        "CustomResourceDefinition",
        "ValidatingWebhookConfiguration",
        "MutatingWebhookConfiguration",
    } <= kinds

    deployments = [doc for doc in applied_docs if doc["kind"] == "Deployment"]
    secret = next(doc for doc in applied_docs if doc["kind"] == "Secret")
    service = next(doc for doc in applied_docs if doc["kind"] == "Service")
    valid_crd = next(
        doc
        for doc in applied_docs
        if doc["kind"] == "CustomResourceDefinition"
        and (doc.get("metadata") or {}).get("labels", {}).get("webhook-e2e-test") != "webhook-disallow"
    )
    container = deployments[0]["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "registry.k8s.io/e2e-test-images/agnhost:2.47"
    assert "webhook" in container["args"]
    assert {"name": "https", "containerPort": 8444, "protocol": "TCP"} in container["ports"]
    assert service["spec"]["ports"][0]["targetPort"] == "https"

    ca_bundle = next(
        webhook["clientConfig"]["caBundle"]
        for doc in applied_docs
        if doc["kind"] == "ValidatingWebhookConfiguration"
        for webhook in doc["webhooks"]
    )
    ca_cert = x509.load_pem_x509_certificate(b64decode(ca_bundle))
    server_cert = x509.load_pem_x509_certificate(b64decode(secret["data"]["tls.crt"]))
    san_values = server_cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
        x509.DNSName
    )
    assert server_cert.issuer == ca_cert.subject
    assert f"{service['metadata']['name']}.{service['metadata']['namespace']}.svc" in san_values

    webhook_paths = {
        webhook["clientConfig"]["service"]["path"]
        for doc in applied_docs
        if doc["kind"] in {"ValidatingWebhookConfiguration", "MutatingWebhookConfiguration"}
        for webhook in doc["webhooks"]
    }
    assert {"/crd", "/custom-resource", "/mutating-custom-resource"} <= webhook_paths
    assert all(
        webhook["clientConfig"]["caBundle"]
        for doc in applied_docs
        if doc["kind"] in {"ValidatingWebhookConfiguration", "MutatingWebhookConfiguration"}
        for webhook in doc["webhooks"]
    )
    assert all(
        webhook["clientConfig"]["service"]["name"] == service["metadata"]["name"]
        and webhook["clientConfig"]["service"]["namespace"] == service["metadata"]["namespace"]
        for doc in applied_docs
        if doc["kind"] in {"ValidatingWebhookConfiguration", "MutatingWebhookConfiguration"}
        for webhook in doc["webhooks"]
    )
    label_value = service["metadata"]["labels"]["isvtest-crd-webhook"]
    assert deployments[0]["spec"]["template"]["metadata"]["labels"]["isvtest-crd-webhook"] == label_value
    assert valid_crd["metadata"]["labels"]["isvtest-crd-webhook"] == label_value
    assert all(
        (webhook.get("objectSelector") or webhook.get("namespaceSelector"))["matchLabels"]["isvtest-crd-webhook"]
        == label_value
        for doc in applied_docs
        if doc["kind"] in {"ValidatingWebhookConfiguration", "MutatingWebhookConfiguration"}
        for webhook in doc["webhooks"]
    )
    assert all(
        rule["apiGroups"] == [valid_crd["spec"]["group"]]
        and rule["resources"] == [valid_crd["spec"]["names"]["plural"]]
        for doc in applied_docs
        if doc["kind"] in {"ValidatingWebhookConfiguration", "MutatingWebhookConfiguration"}
        for webhook in doc["webhooks"]
        if webhook["clientConfig"]["service"]["path"] in {"/custom-resource", "/mutating-custom-resource"}
        for rule in webhook["rules"]
    )
    assert admission_apply_timeouts
    assert max(admission_apply_timeouts) <= 1
    assert any(" wait --for=condition=Available " in cmd for cmd in commands)
    assert any(" wait --for=condition=Established " in cmd for cmd in commands)
    assert any(" delete namespace " in cmd for cmd in commands)


def test_fails_when_crd_rejection_is_not_enforced() -> None:
    """The check fails if the CRD validating webhook allows a disallowed CRD."""

    def fake_apply(_args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return _completed()

    def fake_run_command(cmd: str, *_args: Any, **_kwargs: Any) -> CommandResult:
        if " get " in cmd and " -o json" in cmd:
            return _ok(stdout=_custom_resource_json())
        return _ok()

    check = _run_check(apply_func=fake_apply, run_command=fake_run_command)

    assert not check.passed
    assert "CRD validating webhook did not reject the disallowed CRD" in check.message


def test_fails_when_custom_resource_mutation_is_missing() -> None:
    """The check fails when the mutating webhook does not add ``mutation-stage-1``."""

    def fake_apply(_args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        docs = _docs(kwargs["input"])
        if any(
            (doc.get("metadata") or {}).get("labels", {}).get("webhook-e2e-test") == "webhook-disallow" for doc in docs
        ):
            return _completed(returncode=1, stderr="the crd contains unwanted label")
        if any((doc.get("data") or {}).get("webhook-e2e-test") == "webhook-disallow" for doc in docs):
            return _completed(returncode=1, stderr="the custom resource contains unwanted data")
        return _completed()

    def fake_run_command(cmd: str, *_args: Any, **_kwargs: Any) -> CommandResult:
        if " get " in cmd and " -o json" in cmd:
            return _ok(stdout=_custom_resource_json(mutated=False))
        return _ok()

    check = _run_check(apply_func=fake_apply, run_command=fake_run_command)

    assert not check.passed
    assert "Custom resource mutation did not add data.mutation-stage-1=yes" in check.message


def test_fails_when_custom_resource_rejection_is_not_enforced() -> None:
    """The check fails if the custom-resource validating webhook allows disallowed data."""

    def fake_apply(_args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        docs = _docs(kwargs["input"])
        if any(
            (doc.get("metadata") or {}).get("labels", {}).get("webhook-e2e-test") == "webhook-disallow" for doc in docs
        ):
            return _completed(returncode=1, stderr="the crd contains unwanted label")
        return _completed()

    def fake_run_command(cmd: str, *_args: Any, **_kwargs: Any) -> CommandResult:
        if " get " in cmd and " -o json" in cmd:
            return _ok(stdout=_custom_resource_json())
        return _ok()

    check = _run_check(apply_func=fake_apply, run_command=fake_run_command)

    assert not check.passed
    assert "Custom resource validating webhook did not reject disallowed data" in check.message


def test_cleanup_failures_do_not_override_success() -> None:
    """Best-effort cleanup failures must not replace the validation result."""

    def fake_apply(_args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        docs = _docs(kwargs["input"])
        if any(
            (doc.get("metadata") or {}).get("labels", {}).get("webhook-e2e-test") == "webhook-disallow" for doc in docs
        ):
            return _completed(returncode=1, stderr="the crd contains unwanted label")
        if any((doc.get("data") or {}).get("webhook-e2e-test") == "webhook-disallow" for doc in docs):
            return _completed(returncode=1, stderr="the custom resource contains unwanted data")
        return _completed()

    def fake_run_command(cmd: str, *_args: Any, **_kwargs: Any) -> CommandResult:
        if " get " in cmd and " -o json" in cmd:
            return _ok(stdout=_custom_resource_json())
        if " delete " in cmd:
            return _fail(stderr="cleanup failed")
        return _ok()

    check = _run_check(apply_func=fake_apply, run_command=fake_run_command)

    assert check.passed
    assert check._error == ""
