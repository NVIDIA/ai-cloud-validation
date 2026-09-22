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

import json
import shlex
import urllib.error
import urllib.request
from collections.abc import Iterable
from typing import Any, ClassVar
from urllib.parse import urlsplit

from isvtest.core.k8s import get_kubectl_command
from isvtest.core.validation import BaseValidation

DEFAULT_REQUIRED_FIELDS = [
    "issuer",
    "jwks_uri",
    "response_types_supported",
    "subject_types_supported",
    "id_token_signing_alg_values_supported",
]

DISCOVERY_PATH = "/.well-known/openid-configuration"
DEFAULT_HTTP_TIMEOUT = 10


class K8sOidcIssuerCheck(BaseValidation):
    """Validate the cluster's OIDC discovery endpoint for workload identity federation."""

    description: ClassVar[str] = (
        "Verify the cluster exposes a valid OIDC Issuer endpoint for workload identity federation."
    )

    def run(self) -> None:
        """Fetch the OIDC discovery document anonymously and dereference its JWKS endpoint."""
        required_fields = self._parse_required_fields()
        if required_fields is None:
            return

        timeout = self._parse_positive_int("http_timeout", default=DEFAULT_HTTP_TIMEOUT)
        if timeout is None:
            return

        issuer = self._resolve_issuer_url()
        if issuer is None:
            return

        if not _is_https_url(issuer):
            self.set_failed(f"OIDC issuer is not a valid HTTPS URL: {issuer}")
            return

        discovery_url = issuer.rstrip("/") + DISCOVERY_PATH
        oidc_config = self._fetch_json_anonymously(discovery_url, "OIDC discovery document", timeout)
        if oidc_config is None:
            return

        missing_fields = [f for f in required_fields if f not in oidc_config]
        if missing_fields:
            self.set_failed(f"OIDC discovery response missing required fields: {', '.join(missing_fields)}")
            return

        advertised_issuer = oidc_config.get("issuer")
        if advertised_issuer != issuer.rstrip("/"):
            self.set_failed(
                f"OIDC discovery document advertises issuer {advertised_issuer!r}, "
                f"which does not match the endpoint it was served from: {issuer.rstrip('/')!r}"
            )
            return

        jwks_uri = oidc_config.get("jwks_uri")
        if not _is_https_url(jwks_uri):
            self.set_failed(f"OIDC jwks_uri is not a valid HTTPS URL: {jwks_uri}")
            return

        jwks = self._fetch_json_anonymously(jwks_uri, "JWKS document", timeout)
        if jwks is None:
            return

        keys = jwks.get("keys")
        if not isinstance(keys, list) or not keys:
            self.set_failed(f"JWKS document at {jwks_uri} contains no signing keys")
            return

        self.log.info(f"OIDC issuer: {issuer}")
        self.set_passed(
            f"OIDC discovery endpoint is valid and anonymously reachable at {issuer}, "
            f"serving {len(keys)} signing key(s) from {jwks_uri}"
        )

    def _parse_required_fields(self) -> list[str] | None:
        """Normalize the ``required_fields`` config into a list, or fail."""
        required_fields_config = self.config.get("required_fields", DEFAULT_REQUIRED_FIELDS)
        error_msg = "Invalid 'required_fields' config: expected a string or iterable of non-empty strings."

        if isinstance(required_fields_config, str):
            field = required_fields_config.strip()
            if not field:
                self.set_failed(error_msg)
                return None
            return [field]

        if isinstance(required_fields_config, Iterable):
            required_fields = []
            for field in required_fields_config:
                if not isinstance(field, str):
                    self.set_failed(error_msg)
                    return None
                normalized_field = field.strip()
                if not normalized_field:
                    self.set_failed(error_msg)
                    return None
                required_fields.append(normalized_field)
            return required_fields

        self.set_failed(error_msg)
        return None

    def _resolve_issuer_url(self) -> str | None:
        """Return the issuer URL to probe, from config or from the API server.

        kubectl is only used to learn *which* URL to test. The requirement is
        about anonymous external reachability, so the URL is proven separately.
        """
        configured = self.config.get("issuer_url")
        if isinstance(configured, str) and configured.strip():
            return configured.strip()

        kubectl_base = " ".join(shlex.quote(part) for part in get_kubectl_command())
        result = self.run_command(f"{kubectl_base} get --raw {DISCOVERY_PATH}")
        if result.exit_code != 0:
            self.set_failed(f"Failed to query OIDC discovery endpoint: {result.stderr}")
            return None

        try:
            cluster_view = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            self.set_failed(f"Failed to parse OIDC discovery response as JSON: {e}")
            return None

        if not isinstance(cluster_view, dict):
            self.set_failed("OIDC discovery response must be a JSON object.")
            return None

        issuer = cluster_view.get("issuer")
        if not isinstance(issuer, str) or not issuer.strip():
            self.set_failed(f"OIDC issuer is not a valid HTTPS URL: {issuer}")
            return None
        return issuer.strip()

    def _fetch_json_anonymously(self, url: str, label: str, timeout: int) -> dict[str, Any] | None:
        """GET ``url`` with no credentials and return the parsed JSON object, or fail.

        No Authorization header, kubeconfig or client certificate is attached:
        an unauthenticated caller on the public internet is exactly the actor
        the requirement describes.
        """
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as e:
            self.set_failed(f"Anonymous fetch of {label} at {url} returned HTTP {e.code} {e.reason}")
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            self.set_failed(f"{label} at {url} is not anonymously reachable: {e}")
            return None

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            self.set_failed(f"Failed to parse {label} as JSON: {e}")
            return None

        if not isinstance(payload, dict):
            self.set_failed(f"{label} must be a JSON object.")
            return None
        return payload


def _is_https_url(value: object) -> bool:
    """Return True when ``value`` is an HTTPS URL with a host."""
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return parsed.scheme == "https" and bool(parsed.netloc)
