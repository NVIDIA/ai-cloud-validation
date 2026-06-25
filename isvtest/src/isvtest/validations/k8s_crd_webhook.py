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

"""Validate Kubernetes CRD admission and custom-resource webhook behavior."""

from __future__ import annotations

import base64
import datetime as dt
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from isvtest.core.k8s import KubectlParseError, get_kubectl_base_shell, get_kubectl_command, parse_kubectl_json
from isvtest.core.validation import BaseValidation

_MANIFEST_PATH = Path(__file__).parent / "manifests" / "k8s" / "crd_webhook.yaml"
_ROLE_ANNOTATION = "isvtest.nvidia.com/crd-webhook-role"
_LABEL_KEY = "isvtest-crd-webhook"
_DEFAULT_IMAGE = "registry.k8s.io/e2e-test-images/agnhost:2.47"
_SERVICE_PORT = 443
_CONTAINER_PORT = 8444
_API_VERSION = "v1"
_KIND = "CrdWebhookWidget"
_PLURAL = "crdwebhookwidgets"
_SINGULAR = "crdwebhookwidget"


@dataclass(frozen=True)
class _CertBundle:
    """PEM certificate material encoded for Kubernetes Secret and webhook fields."""

    ca_bundle: str
    tls_crt: str
    tls_key: str


@dataclass(frozen=True)
class _RuntimeNames:
    """Names generated for one ephemeral CRD webhook validation run."""

    suffix: str
    namespace: str
    secret: str
    service: str
    deployment: str
    crd_name: str
    crd_group: str
    deny_crd_name: str
    deny_crd_group: str
    crd_validator: str
    cr_mutator: str
    cr_validator: str
    resource: str


class K8sCrdWebhookCheck(BaseValidation):
    """Verify Kubernetes CRDs plus validating and mutating admission webhooks.

    The check deploys the upstream ``agnhost webhook`` server in an ephemeral
    namespace, registers admission webhooks, and then verifies:

    * CRD creation is denied when the CRD has the disallowed label.
    * A valid namespaced custom resource definition can be established.
    * A custom resource is mutated with ``data.mutation-stage-1=yes``.
    * A custom resource with disallowed data is rejected.

    Config keys:
        image: agnhost image containing the ``webhook`` subcommand.
        namespace_prefix: Prefix for the ephemeral namespace.
        wait_timeout: Timeout for waits and admission propagation retries.
        poll_interval: Delay between admission propagation retry attempts.
    """

    description: ClassVar[str] = (
        "Create a CRD and verify CRD admission rejection, custom-resource mutation, and custom-resource validation."
    )
    timeout: ClassVar[int] = 300

    def run(self) -> None:
        """Create webhook resources, verify admission behavior, and clean up."""
        wait_timeout = self._parse_positive_int("wait_timeout", default=180)
        poll_interval = self._parse_positive_int("poll_interval", default=2)
        if wait_timeout is None or poll_interval is None:
            return

        self._wait_timeout = wait_timeout
        self._poll_interval = poll_interval
        self._image = str(self.config.get("image") or _DEFAULT_IMAGE)
        self._kubectl_parts = get_kubectl_command()
        self._kubectl_base = get_kubectl_base_shell()
        self._names = _make_names(str(self.config.get("namespace_prefix", "isvtest-crd-webhook")))
        service_dns_names = _service_dns_names(self._names.service, self._names.namespace)
        self._certs = _generate_cert_bundle(service_dns_names)

        try:
            if not self._create_namespace():
                return
            if not self._apply_or_fail(self._render_roles({"secret", "service", "deployment"}), "webhook server"):
                return
            if not self._wait_for_webhook_deployment():
                return
            if not self._apply_or_fail(self._render_roles({"crd-validator"}), "CRD validating webhook"):
                return
            if not self._expect_apply_rejected(
                self._render_crd(disallowed=True),
                "the crd contains unwanted label",
                "CRD validating webhook did not reject the disallowed CRD",
                f"{self._kubectl_base} delete crd {shlex.quote(self._names.deny_crd_name)} --ignore-not-found=true",
            ):
                return
            if not self._apply_or_fail(self._render_crd(disallowed=False), "custom resource definition"):
                return
            if not self._wait_for_crd():
                return
            if not self._apply_or_fail(
                self._render_roles({"cr-mutator", "cr-validator"}),
                "custom resource webhooks",
            ):
                return
            if not self._verify_custom_resource_mutation():
                return
            if not self._expect_apply_rejected(
                self._render_custom_resource("rejected", {"webhook-e2e-test": "webhook-disallow"}),
                "the custom resource contains unwanted data",
                "Custom resource validating webhook did not reject disallowed data",
                self._delete_custom_resource_command("rejected"),
            ):
                return

            self.set_passed("CRD admission and custom resource webhook mutation/validation verified")
        finally:
            self._cleanup()

    def _create_namespace(self) -> bool:
        """Create and label the ephemeral namespace used by namespaced webhook resources."""
        namespace = shlex.quote(self._names.namespace)
        result = self.run_command(f"{self._kubectl_base} create namespace {namespace}")
        if result.exit_code != 0:
            self.set_failed(f"Failed to create namespace {self._names.namespace}: {_command_detail(result)}")
            return False

        label = shlex.quote(f"{_LABEL_KEY}={self._names.suffix}")
        label_result = self.run_command(f"{self._kubectl_base} label namespace {namespace} {label} --overwrite")
        if label_result.exit_code != 0:
            self.set_failed(f"Failed to label namespace {self._names.namespace}: {_command_detail(label_result)}")
            return False
        return True

    def _wait_for_webhook_deployment(self) -> bool:
        """Wait for the agnhost webhook Deployment to become Available."""
        cmd = (
            f"{self._kubectl_base} wait --for=condition=Available "
            f"--timeout={self._wait_timeout}s deployment/{shlex.quote(self._names.deployment)} "
            f"-n {shlex.quote(self._names.namespace)}"
        )
        result = self.run_command(cmd, timeout=self._wait_timeout + 30)
        if result.exit_code != 0:
            self.set_failed(f"Webhook deployment did not become Available: {_command_detail(result)}")
            return False
        return True

    def _wait_for_crd(self) -> bool:
        """Wait for the custom resource definition to become Established."""
        cmd = (
            f"{self._kubectl_base} wait --for=condition=Established "
            f"--timeout={self._wait_timeout}s crd/{shlex.quote(self._names.crd_name)}"
        )
        result = self.run_command(cmd, timeout=self._wait_timeout + 30)
        if result.exit_code != 0:
            self.set_failed(f"CustomResourceDefinition did not become Established: {_command_detail(result)}")
            return False
        return True

    def _verify_custom_resource_mutation(self) -> bool:
        """Create a custom resource and verify the mutating webhook added stage-1 data."""
        manifest = self._render_custom_resource("mutated", {"mutation-start": "yes"})
        last_detail = ""
        attempts = self._attempts()
        deadline = time.monotonic() + self._wait_timeout
        for attempt in range(attempts):
            proc = self._run_apply(manifest, timeout=self._apply_timeout(deadline))
            if proc.returncode != 0:
                last_detail = _process_detail(proc)
            else:
                result = self.run_command(
                    f"{self._kubectl_base} get {shlex.quote(self._names.resource)} mutated "
                    f"-n {shlex.quote(self._names.namespace)} -o json"
                )
                if result.exit_code != 0:
                    last_detail = _command_detail(result)
                else:
                    try:
                        payload = parse_kubectl_json(result, "mutated custom resource")
                    except KubectlParseError as exc:
                        last_detail = str(exc)
                    else:
                        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                        if data.get("mutation-stage-1") == "yes":
                            return True
                        last_detail = f"observed data={data!r}"
                self._cleanup_command(self._delete_custom_resource_command("mutated"), "mutated custom resource")

            if attempt < attempts - 1:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(float(self._poll_interval), remaining))

        self.set_failed(f"Custom resource mutation did not add data.mutation-stage-1=yes: {last_detail}")
        return False

    def _expect_apply_rejected(
        self,
        manifest: str,
        expected: str,
        failure_message: str,
        cleanup_cmd: str,
    ) -> bool:
        """Run ``kubectl apply`` until the admission webhook rejects with ``expected``."""
        last_detail = ""
        attempts = self._attempts()
        deadline = time.monotonic() + self._wait_timeout
        for attempt in range(attempts):
            proc = self._run_apply(manifest, timeout=self._apply_timeout(deadline))
            detail = _process_detail(proc)
            if proc.returncode != 0:
                if expected in detail:
                    return True
                last_detail = detail
            else:
                last_detail = "kubectl apply unexpectedly succeeded"
                self._cleanup_command(cleanup_cmd, "unexpectedly admitted resource")

            if attempt < attempts - 1:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(float(self._poll_interval), remaining))

        self.set_failed(f"{failure_message}: {last_detail}")
        return False

    def _attempts(self) -> int:
        """Return the number of admission propagation attempts to make."""
        return max(1, (self._wait_timeout + self._poll_interval - 1) // self._poll_interval)

    def _apply_timeout(self, deadline: float) -> float:
        """Return a per-apply timeout bounded by the remaining retry budget."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 0.1
        return min(float(self.timeout), max(0.1, remaining))

    def _apply_or_fail(self, manifest: str, label: str) -> bool:
        """Apply a manifest and mark the validation failed on kubectl errors."""
        proc = self._run_apply(manifest)
        if proc.returncode != 0:
            self.set_failed(f"kubectl apply failed for {label}: {_process_detail(proc)}")
            return False
        return True

    def _run_apply(self, manifest: str, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        """Run ``kubectl apply -f -`` for a rendered manifest string."""
        try:
            return subprocess.run(
                self._kubectl_parts + ["apply", "-f", "-"],
                input=manifest,
                capture_output=True,
                text=True,
                timeout=self.timeout if timeout is None else timeout,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else "kubectl apply timed out"
            return subprocess.CompletedProcess(self._kubectl_parts + ["apply", "-f", "-"], 124, stdout, stderr)
        except Exception as exc:
            return subprocess.CompletedProcess(
                self._kubectl_parts + ["apply", "-f", "-"],
                1,
                "",
                f"kubectl apply raised: {exc}",
            )

    def _render_roles(self, roles: set[str]) -> str:
        """Render all manifest documents whose role annotation is in ``roles``."""
        docs = [self._mutate_doc(doc) for doc in _load_template_docs() if _doc_role(doc) in roles]
        return yaml.safe_dump_all(docs, sort_keys=False)

    def _render_crd(self, *, disallowed: bool) -> str:
        """Render the CRD document, optionally with the disallowed webhook label."""
        crd = next(doc for doc in _load_template_docs() if _doc_role(doc) == "crd")
        return yaml.safe_dump(self._mutate_crd_doc(crd, disallowed=disallowed), sort_keys=False)

    def _render_custom_resource(self, name: str, data: dict[str, str]) -> str:
        """Render a namespaced custom resource instance for the generated CRD."""
        doc: dict[str, Any] = {
            "apiVersion": f"{self._names.crd_group}/{_API_VERSION}",
            "kind": _KIND,
            "metadata": {
                "name": name,
                "namespace": self._names.namespace,
                "labels": {_LABEL_KEY: self._names.suffix},
            },
            "data": data,
        }
        return yaml.safe_dump(doc, sort_keys=False)

    def _mutate_doc(self, doc: dict[str, Any]) -> dict[str, Any]:
        """Patch a template document with runtime values."""
        role = _doc_role(doc)
        if role == "secret":
            return self._mutate_secret_doc(doc)
        if role == "service":
            return self._mutate_service_doc(doc)
        if role == "deployment":
            return self._mutate_deployment_doc(doc)
        if role == "crd":
            return self._mutate_crd_doc(doc, disallowed=False)
        if role == "crd-validator":
            return self._mutate_webhook_doc(doc, self._names.crd_validator)
        if role == "cr-mutator":
            return self._mutate_custom_resource_webhook_doc(doc, self._names.cr_mutator)
        if role == "cr-validator":
            return self._mutate_custom_resource_webhook_doc(doc, self._names.cr_validator)
        msg = f"Unsupported CRD webhook manifest role: {role}"
        raise ValueError(msg)

    def _mutate_secret_doc(self, doc: dict[str, Any]) -> dict[str, Any]:
        """Patch the TLS Secret document."""
        metadata = _metadata(doc)
        metadata["name"] = self._names.secret
        metadata["namespace"] = self._names.namespace
        metadata.setdefault("labels", {})[_LABEL_KEY] = self._names.suffix
        doc["data"] = {"tls.crt": self._certs.tls_crt, "tls.key": self._certs.tls_key}
        return doc

    def _mutate_service_doc(self, doc: dict[str, Any]) -> dict[str, Any]:
        """Patch the webhook Service document."""
        metadata = _metadata(doc)
        metadata["name"] = self._names.service
        metadata["namespace"] = self._names.namespace
        metadata.setdefault("labels", {})[_LABEL_KEY] = self._names.suffix
        doc["spec"]["selector"] = {"app": self._names.deployment}
        doc["spec"]["ports"][0]["port"] = _SERVICE_PORT
        doc["spec"]["ports"][0]["targetPort"] = "https"
        return doc

    def _mutate_deployment_doc(self, doc: dict[str, Any]) -> dict[str, Any]:
        """Patch the agnhost webhook Deployment document."""
        metadata = _metadata(doc)
        metadata["name"] = self._names.deployment
        metadata["namespace"] = self._names.namespace
        metadata.setdefault("labels", {})[_LABEL_KEY] = self._names.suffix
        selector_labels = {"app": self._names.deployment}
        doc["spec"]["selector"]["matchLabels"] = selector_labels
        doc["spec"]["template"]["metadata"]["labels"] = {**selector_labels, _LABEL_KEY: self._names.suffix}
        container = doc["spec"]["template"]["spec"]["containers"][0]
        container["image"] = self._image
        container["args"] = [
            "webhook",
            "--tls-cert-file=/webhook.local.config/certificates/tls.crt",
            "--tls-private-key-file=/webhook.local.config/certificates/tls.key",
            f"--port={_CONTAINER_PORT}",
        ]
        doc["spec"]["template"]["spec"]["volumes"][0]["secret"]["secretName"] = self._names.secret
        return doc

    def _mutate_crd_doc(self, doc: dict[str, Any], *, disallowed: bool) -> dict[str, Any]:
        """Patch the CustomResourceDefinition document."""
        metadata = _metadata(doc)
        group = self._names.deny_crd_group if disallowed else self._names.crd_group
        crd_name = self._names.deny_crd_name if disallowed else self._names.crd_name
        metadata["name"] = crd_name
        labels = metadata.setdefault("labels", {})
        labels[_LABEL_KEY] = self._names.suffix
        if disallowed:
            labels["webhook-e2e-test"] = "webhook-disallow"
        else:
            labels.pop("webhook-e2e-test", None)
        doc["spec"]["group"] = group
        doc["spec"]["names"]["plural"] = _PLURAL
        doc["spec"]["names"]["singular"] = _SINGULAR
        doc["spec"]["names"]["kind"] = _KIND
        doc["spec"]["names"]["listKind"] = f"{_KIND}List"
        return doc

    def _mutate_webhook_doc(self, doc: dict[str, Any], name: str) -> dict[str, Any]:
        """Patch common webhook configuration fields."""
        _metadata(doc)["name"] = name
        for webhook in doc["webhooks"]:
            webhook["clientConfig"]["caBundle"] = self._certs.ca_bundle
            service = webhook["clientConfig"]["service"]
            service["namespace"] = self._names.namespace
            service["name"] = self._names.service
            service["port"] = _SERVICE_PORT
            if "objectSelector" in webhook:
                webhook["objectSelector"]["matchLabels"] = {_LABEL_KEY: self._names.suffix}
            if "namespaceSelector" in webhook:
                webhook["namespaceSelector"]["matchLabels"] = {_LABEL_KEY: self._names.suffix}
        return doc

    def _mutate_custom_resource_webhook_doc(self, doc: dict[str, Any], name: str) -> dict[str, Any]:
        """Patch webhook rules for the generated custom resource."""
        doc = self._mutate_webhook_doc(doc, name)
        for webhook in doc["webhooks"]:
            for rule in webhook["rules"]:
                rule["apiGroups"] = [self._names.crd_group]
                rule["apiVersions"] = [_API_VERSION]
                rule["resources"] = [_PLURAL]
        return doc

    def _delete_custom_resource_command(self, name: str) -> str:
        """Build a best-effort delete command for a generated custom resource."""
        return (
            f"{self._kubectl_base} delete {shlex.quote(self._names.resource)} {shlex.quote(name)} "
            f"-n {shlex.quote(self._names.namespace)} --ignore-not-found=true"
        )

    def _cleanup(self) -> None:
        """Best-effort cleanup for cluster-scoped webhooks, CRDs, and the namespace."""
        if not hasattr(self, "_names") or not hasattr(self, "_kubectl_base"):
            return
        commands = [
            (
                f"{self._kubectl_base} delete validatingwebhookconfiguration "
                f"{shlex.quote(self._names.crd_validator)} --ignore-not-found=true",
                "CRD validating webhook",
            ),
            (
                f"{self._kubectl_base} delete validatingwebhookconfiguration "
                f"{shlex.quote(self._names.cr_validator)} --ignore-not-found=true",
                "custom resource validating webhook",
            ),
            (
                f"{self._kubectl_base} delete mutatingwebhookconfiguration "
                f"{shlex.quote(self._names.cr_mutator)} --ignore-not-found=true",
                "custom resource mutating webhook",
            ),
            (
                f"{self._kubectl_base} delete crd {shlex.quote(self._names.crd_name)} --ignore-not-found=true",
                "custom resource definition",
            ),
            (
                f"{self._kubectl_base} delete crd {shlex.quote(self._names.deny_crd_name)} --ignore-not-found=true",
                "disallowed custom resource definition",
            ),
            (
                f"{self._kubectl_base} delete namespace {shlex.quote(self._names.namespace)} "
                "--wait=false --ignore-not-found=true",
                "namespace",
            ),
        ]
        for cmd, label in commands:
            self._cleanup_command(cmd, label)

    def _cleanup_command(self, cmd: str, label: str) -> None:
        """Run one best-effort cleanup command without changing validation state."""
        try:
            result = self.run_command(cmd)
        except Exception as exc:
            self.log.warning("%s cleanup raised: %s", label, exc)
            return
        if result.exit_code != 0:
            self.log.warning("%s cleanup failed: %s", label, _command_detail(result))


def _load_template_docs() -> list[dict[str, Any]]:
    """Load the CRD webhook manifest template as a list of YAML documents."""
    return [doc for doc in yaml.safe_load_all(_MANIFEST_PATH.read_text()) if isinstance(doc, dict)]


def _doc_role(doc: dict[str, Any]) -> str:
    """Return the CRD webhook role annotation for a template document."""
    return str((_metadata(doc).get("annotations") or {}).get(_ROLE_ANNOTATION, ""))


def _metadata(doc: dict[str, Any]) -> dict[str, Any]:
    """Return a document's metadata dictionary, creating it if needed."""
    metadata = doc.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        msg = f"Kubernetes document metadata must be an object, got {type(metadata).__name__}"
        raise TypeError(msg)
    return metadata


def _make_names(namespace_prefix: str) -> _RuntimeNames:
    """Generate names unique enough for one validation run."""
    suffix = uuid.uuid4().hex[:8]
    namespace = f"{namespace_prefix}-{suffix}"
    group = f"crd-webhook-{suffix}.isvtest.nvidia.com"
    deny_group = f"crd-webhook-deny-{suffix}.isvtest.nvidia.com"
    return _RuntimeNames(
        suffix=suffix,
        namespace=namespace,
        secret="crd-webhook-certs",
        service="crd-webhook",
        deployment="crd-webhook",
        crd_name=f"{_PLURAL}.{group}",
        crd_group=group,
        deny_crd_name=f"{_PLURAL}.{deny_group}",
        deny_crd_group=deny_group,
        crd_validator=f"crd-webhook-{suffix}-crd-validator",
        cr_mutator=f"crd-webhook-{suffix}-cr-mutator",
        cr_validator=f"crd-webhook-{suffix}-cr-validator",
        resource=f"{_PLURAL}.{group}",
    )


def _service_dns_names(service: str, namespace: str) -> list[str]:
    """Return DNS SANs accepted for the webhook Service certificate."""
    return [
        service,
        f"{service}.{namespace}",
        f"{service}.{namespace}.svc",
        f"{service}.{namespace}.svc.cluster.local",
    ]


def _generate_cert_bundle(dns_names: list[str]) -> _CertBundle:
    """Generate a CA and server certificate for the webhook Service DNS names."""
    now = dt.datetime.now(dt.UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "isvtest-crd-webhook-ca")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns_names[2])])
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name) for name in dns_names]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    ca_pem = ca_cert.public_bytes(serialization.Encoding.PEM)
    cert_pem = server_cert.public_bytes(serialization.Encoding.PEM)
    key_pem = server_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    return _CertBundle(
        ca_bundle=_b64(ca_pem),
        tls_crt=_b64(cert_pem),
        tls_key=_b64(key_pem),
    )


def _b64(data: bytes) -> str:
    """Return base64-encoded text for Kubernetes byte fields."""
    return base64.b64encode(data).decode("ascii")


def _process_detail(proc: subprocess.CompletedProcess[str]) -> str:
    """Return the most useful stderr/stdout detail from a completed process."""
    return (proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip()


def _command_detail(result: Any) -> str:
    """Return the most useful stderr/stdout detail from a command result."""
    return (result.stderr or result.stdout or f"exit code {result.exit_code}").strip()
