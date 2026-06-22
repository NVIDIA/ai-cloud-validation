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

"""Centralized redaction utilities for secret masking.

Single source of truth for all secret redaction throughout isvctl:

- mask_sensitive_args   - masks CLI argument values in logged commands
- redact_dict           - recursively redacts sensitive keys in dicts/JSON
- filter_env            - removes sensitive environment variables from context
- redact_text           - redacts sensitive key/value patterns in free text
- redact_junit_xml_tree - sanitizes an ElementTree of JUnit XML in-place
"""

import re
import shlex
import xml.etree.ElementTree as ET
from typing import Any

REDACTED = "***"

# ---------------------------------------------------------------------------
# CLI argument masking (used by step_executor and commands)
# ---------------------------------------------------------------------------

SENSITIVE_ARG_PATTERNS: list[str] = [
    r"--secret[-_]?access[-_]?key",
    r"--access[-_]?key[-_]?id",
    r"--password",
    r"--token",
    r"--api[-_]?key",
    r"--private[-_]?key",
    r"--secret",
    r"--credential",
    r"--auth",
    r"--connection[-_]?string",
    r"--account[-_]?key",
    r"--subscription[-_]?key",
]

_SENSITIVE_ARG_RE = re.compile(
    "|".join(SENSITIVE_ARG_PATTERNS),
    re.IGNORECASE,
)


def mask_sensitive_args(
    cmd_parts: list[str],
    extra_patterns: list[str] | None = None,
) -> str:
    """Mask sensitive arguments in a command string for safe logging.

    Handles both --flag value and --flag=value forms.

    Args:
        cmd_parts: List of command parts.
        extra_patterns: Additional patterns to mask (from step config sensitive_args).

    Returns:
        Command string with sensitive values replaced by ***.
    """
    if extra_patterns:
        combined = SENSITIVE_ARG_PATTERNS + [re.escape(p) for p in extra_patterns]
        pattern = re.compile("|".join(combined), re.IGNORECASE)
    else:
        pattern = _SENSITIVE_ARG_RE

    masked: list[str] = []
    skip_next = False

    for i, part in enumerate(cmd_parts):
        if skip_next:
            masked.append(REDACTED)
            skip_next = False
            continue

        if "=" in part:
            key, _, _value = part.partition("=")
            if pattern.search(key):
                masked.append(f"{key}={REDACTED}")
            else:
                masked.append(part)
        elif pattern.search(part):
            masked.append(part)
            if i + 1 < len(cmd_parts) and not cmd_parts[i + 1].startswith("-"):
                skip_next = True
        else:
            masked.append(part)

    return " ".join(shlex.quote(p) for p in masked)


# ---------------------------------------------------------------------------
# Dict-key redaction (for step outputs, config dumps, phase details)
# ---------------------------------------------------------------------------

SENSITIVE_KEY_PATTERNS: list[str] = [
    # AWS
    r"secret[-_]?access[-_]?key",
    r"access[-_]?key[-_]?id",
    r"secret[-_]?key",
    # Azure
    r"account[-_]?key",
    r"subscription[-_]?key",
    r"connection[-_]?string",
    r"sas[-_]?token",
    r"signing[-_]?key",
    # General / multi-cloud
    r"password",
    r"api[-_]?key",
    r"private[-_]?key",
    r"auth[-_]?token",
    r"session[-_]?token",
    r"client[-_]?secret",
    r"credential[-_]?secret",
    r"access[-_]?token",
    r"refresh[-_]?token",
    r"bearer[-_]?token",
]

_SENSITIVE_KEY_RE = re.compile(
    "|".join(SENSITIVE_KEY_PATTERNS),
    re.IGNORECASE,
)


def is_sensitive_key(key: str) -> bool:
    """Return True if key looks like it holds a secret value."""
    return bool(_SENSITIVE_KEY_RE.search(key))


def redact_dict(data: Any) -> Any:
    """Recursively redact values whose keys match sensitive patterns.

    Non-dict/list values are returned unchanged. None passes through
    safely so callers do not need a guard.
    """
    if data is None:
        return None
    if isinstance(data, dict):
        return {k: REDACTED if is_sensitive_key(k) else redact_dict(v) for k, v in data.items()}
    if isinstance(data, list):
        return [redact_dict(item) for item in data]
    return data


# ---------------------------------------------------------------------------
# Environment-variable filtering (for Jinja2 context)
# ---------------------------------------------------------------------------

SENSITIVE_ENV_VARS: frozenset[str] = frozenset(
    {
        # AWS
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        # Azure
        "AZURE_CLIENT_SECRET",
        "AZURE_STORAGE_KEY",
        "AZURE_SUBSCRIPTION_KEY",
        # GCP
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        # NVIDIA
        "NGC_API_KEY",
        "NGC_NIM_API_KEY",
        # NICo
        "NICO_BEARER_TOKEN",
        "NICO_CLIENT_SECRET",
        # UFM (InfiniBand fabric manager)
        "UFM_TOKEN",
        # ISV
        "ISV_CLIENT_SECRET",
    }
)

SENSITIVE_ENV_SUFFIXES: tuple[str, ...] = (
    "_SECRET",
    "_PASSWORD",
    "_API_KEY",
    "_SECRET_KEY",
    "_SECRET_ACCESS_KEY",
    "_PRIVATE_KEY",
    "_ACCOUNT_KEY",
    "_CONNECTION_STRING",
)


def is_secret_env_var(name: str) -> bool:
    """Return True if an environment variable name holds a secret value.

    A variable is secret if it appears in the explicit deny-list or its name
    ends with a sensitive suffix. This is the single rule used both to filter
    secrets out of the Jinja2 context and to route values written by
    `isvctl configure` into the permission-locked secrets file.
    """
    return name in SENSITIVE_ENV_VARS or any(name.upper().endswith(s) for s in SENSITIVE_ENV_SUFFIXES)


def filter_env(env: dict[str, str]) -> dict[str, str]:
    """Return a copy of env with known-sensitive variables removed.

    Variables are excluded if they appear in the explicit deny-list or
    their name ends with a sensitive suffix.
    """
    return {k: v for k, v in env.items() if not is_secret_env_var(k)}


# ---------------------------------------------------------------------------
# Free-text redaction (for JUnit XML, log files, error messages)
# ---------------------------------------------------------------------------

_KEY_ALT = "|".join(SENSITIVE_KEY_PATTERNS)

# Allow prefixed/suffixed key names (e.g. NGC_API_KEY, AWS_SECRET_ACCESS_KEY)
# so that both exact keys and longer variants are caught.
_JSON_DOUBLE_QUOTE_RE = re.compile(
    rf'"([\w-]*(?:{_KEY_ALT})[\w-]*)"\s*:\s*"[^"]*"',
    re.IGNORECASE,
)
_JSON_SINGLE_QUOTE_RE = re.compile(
    rf"'([\w-]*(?:{_KEY_ALT})[\w-]*)'\s*:\s*'[^']*'",
    re.IGNORECASE,
)
_KEY_VALUE_RE = re.compile(
    rf"\b([\w-]*(?:{_KEY_ALT})[\w-]*)\s*=\s*\S+",
    re.IGNORECASE,
)


def redact_text(text: str) -> str:
    """Redact sensitive key/value pairs found in free-form text.

    Handles JSON ("key": "value"), single-quoted variants (Python repr),
    and key=value patterns. Matches both exact keys (api_key) and
    prefixed variants (NGC_API_KEY, AWS_SECRET_ACCESS_KEY).
    """
    result = _JSON_DOUBLE_QUOTE_RE.sub(rf'"\1": "{REDACTED}"', text)
    result = _JSON_SINGLE_QUOTE_RE.sub(rf"'\1': '{REDACTED}'", result)
    result = _KEY_VALUE_RE.sub(rf"\1={REDACTED}", result)
    return result


# ---------------------------------------------------------------------------
# JUnit XML sanitization
# ---------------------------------------------------------------------------

_JUNIT_TEXT_TAGS = frozenset({"failure", "error", "system-out", "system-err"})
_JUNIT_MSG_TAGS = frozenset({"failure", "error"})


def redact_junit_xml_tree(root: ET.Element) -> None:
    """Redact sensitive values from a JUnit XML ElementTree in-place.

    Processes <failure>, <error>, <system-out>, and <system-err> elements,
    applying redact_text to their text content and message attributes.
    """
    for element in root.iter():
        if element.tag in _JUNIT_TEXT_TAGS and element.text:
            element.text = redact_text(element.text)
        if element.tag in _JUNIT_MSG_TAGS:
            msg = element.get("message")
            if msg:
                element.set("message", redact_text(msg))
