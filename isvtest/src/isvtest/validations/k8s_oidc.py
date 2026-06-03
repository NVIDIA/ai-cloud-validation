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
from collections.abc import Iterable
from typing import ClassVar
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


class K8sOidcIssuerCheck(BaseValidation):
    """Validate the cluster's OIDC discovery endpoint for workload identity federation."""

    description: ClassVar[str] = (
        "Verify the cluster exposes a valid OIDC Issuer endpoint for workload identity federation."
    )

    def run(self) -> None:
        """Fetch the OIDC discovery document and validate its shape and issuer URL."""
        required_fields_config = self.config.get("required_fields", DEFAULT_REQUIRED_FIELDS)
        error_msg = "Invalid 'required_fields' config: expected a string or iterable of non-empty strings."
        if isinstance(required_fields_config, str):
            field = required_fields_config.strip()
            if not field:
                self.set_failed(error_msg)
                return
            required_fields = [field]
        elif isinstance(required_fields_config, Iterable):
            required_fields = []
            for field in required_fields_config:
                if not isinstance(field, str):
                    self.set_failed(error_msg)
                    return

                normalized_field = field.strip()
                if not normalized_field:
                    self.set_failed(error_msg)
                    return
                required_fields.append(normalized_field)
        else:
            self.set_failed(error_msg)
            return

        kubectl_parts = get_kubectl_command()
        kubectl_base = " ".join(shlex.quote(part) for part in kubectl_parts)

        cmd = f"{kubectl_base} get --raw /.well-known/openid-configuration"
        result = self.run_command(cmd)

        if result.exit_code != 0:
            self.set_failed(f"Failed to query OIDC discovery endpoint: {result.stderr}")
            return

        try:
            oidc_config = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            self.set_failed(f"Failed to parse OIDC discovery response as JSON: {e}")
            return

        if not isinstance(oidc_config, dict):
            self.set_failed("OIDC discovery response must be a JSON object.")
            return

        missing_fields = [f for f in required_fields if f not in oidc_config]
        if missing_fields:
            self.set_failed(f"OIDC discovery response missing required fields: {', '.join(missing_fields)}")
            return

        issuer = oidc_config.get("issuer")
        if not isinstance(issuer, str):
            self.set_failed(f"OIDC issuer is not a valid HTTPS URL: {issuer}")
            return

        parsed = urlsplit(issuer)
        if parsed.scheme != "https" or not parsed.netloc:
            self.set_failed(f"OIDC issuer is not a valid HTTPS URL: {issuer}")
            return

        self.log.info(f"OIDC issuer: {issuer}")
        self.set_passed(f"OIDC discovery endpoint is valid with issuer: {issuer}")
