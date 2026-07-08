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

"""Catalog of environment variables isvctl knows about.

Single source of truth for which env vars matter, how strictly each is needed,
and which provider group it belongs to. Both `isvctl doctor` (which validates
presence) and `isvctl configure` (which persists values) consume this table so
they never drift.
"""

from dataclasses import dataclass
from enum import StrEnum


class Requirement(StrEnum):
    """How strictly an env var is needed."""

    REQUIRED = "required"  # missing → FAIL
    RECOMMENDED = "recommended"  # missing → WARN
    OPTIONAL = "optional"  # missing → SKIP (informational only)


@dataclass(frozen=True)
class EnvVar:
    """One environment variable in the catalog."""

    name: str
    group: str
    requirement: Requirement
    hint: str
    # Per-run toggles (the "Flags" group) are intentionally NOT persistable:
    # they belong on the command line or in an explicit export each time, not in
    # config.yml. `doctor` still reports them; `configure` skips them and the
    # loader rejects them.
    persistable: bool = True


# Variable table — single source of truth for which env vars isvctl knows
# about and how it classifies them. Keep grouped for stable rendering order.
ENV_VARS: tuple[EnvVar, ...] = (
    # ISV Lab Service
    EnvVar(
        "ISV_SERVICE_ENDPOINT",
        "ISV Lab Service",
        Requirement.RECOMMENDED,
        "needed to upload results to ISV Lab Service",
    ),
    EnvVar(
        "ISV_SSA_ISSUER",
        "ISV Lab Service",
        Requirement.RECOMMENDED,
        "needed for SSA auth against ISV Lab Service",
    ),
    EnvVar(
        "ISV_CLIENT_ID",
        "ISV Lab Service",
        Requirement.RECOMMENDED,
        "needed to authenticate result uploads",
    ),
    EnvVar(
        "ISV_CLIENT_SECRET",
        "ISV Lab Service",
        Requirement.RECOMMENDED,
        "needed to authenticate result uploads",
    ),
    # NGC
    EnvVar(
        "NGC_API_KEY",
        "NGC",
        Requirement.RECOMMENDED,
        "needed for NIM workloads and the NGC container registry",
    ),
    EnvVar(
        "NGC_NIM_API_KEY",
        "NGC",
        Requirement.OPTIONAL,
        "alternative to NGC_API_KEY for NIM workloads",
    ),
    # AWS — informational only. Static keys are just one of several credential
    # sources boto3 accepts; `--provider aws` runs `_check_aws_provider` which
    # validates the whole chain instead of demanding these specific vars.
    EnvVar(
        "AWS_ACCESS_KEY_ID",
        "AWS",
        Requirement.OPTIONAL,
        "one way to supply AWS credentials (see also AWS_PROFILE / SSO)",
    ),
    EnvVar(
        "AWS_SECRET_ACCESS_KEY",
        "AWS",
        Requirement.OPTIONAL,
        "one way to supply AWS credentials (see also AWS_PROFILE / SSO)",
    ),
    EnvVar(
        "AWS_REGION",
        "AWS",
        Requirement.OPTIONAL,
        "AWS region; may also come from AWS_DEFAULT_REGION or ~/.aws/config",
    ),
    # Flags — informational only, and never persisted (per-run toggles).
    EnvVar(
        "KUBECTL",
        "Flags",
        Requirement.OPTIONAL,
        "override the kubectl command (POSIX shlex split)",
        persistable=False,
    ),
    EnvVar(
        "ISVCTL_DEMO_MODE",
        "Flags",
        Requirement.OPTIONAL,
        "set to '1' to use my-isv demo stubs",
        persistable=False,
    ),
    EnvVar(
        "ISVTEST_INCLUDE_UNRELEASED",
        "Flags",
        Requirement.OPTIONAL,
        "include unreleased validations",
        persistable=False,
    ),
    EnvVar(
        "AWS_SKIP_TEARDOWN",
        "Flags",
        Requirement.OPTIONAL,
        "skip AWS teardown phase",
        persistable=False,
    ),
    # NICo — optional by default; --provider nico runs strict provider-specific
    # checks for the same variables.
    EnvVar(
        "NICO_API_BASE",
        "NICo",
        Requirement.OPTIONAL,
        "NICo API base URL",
    ),
    EnvVar(
        "NICO_API_NAME",
        "NICo",
        Requirement.OPTIONAL,
        "NICo API path segment (carbide or nico); defaults to carbide",
    ),
    EnvVar(
        "NICO_ORGANIZATION",
        "NICo",
        Requirement.OPTIONAL,
        "NICo organization name used in the API path",
    ),
    EnvVar(
        "NICO_SITE_ID",
        "NICo",
        Requirement.OPTIONAL,
        "Forge site UUID for NICo hardware checks",
    ),
    EnvVar(
        "NICO_BEARER_TOKEN",
        "NICo",
        Requirement.OPTIONAL,
        "local NICo bearer token for API authentication",
    ),
    EnvVar(
        "NICO_SSA_ISSUER",
        "NICo",
        Requirement.OPTIONAL,
        "SSA issuer URL for NICo client_credentials auth",
    ),
    EnvVar(
        "NICO_CLIENT_ID",
        "NICo",
        Requirement.OPTIONAL,
        "OIDC client ID for NICo client_credentials auth",
    ),
    EnvVar(
        "NICO_CLIENT_SECRET",
        "NICo",
        Requirement.OPTIONAL,
        "OIDC client secret for NICo client_credentials auth",
    ),
    EnvVar(
        "NICO_OIDC_SCOPE",
        "NICo",
        Requirement.OPTIONAL,
        "optional OIDC scope for NICo client_credentials auth",
    ),
)


# Maps a provider name (as passed to `--provider`) to its catalog group, so
# `isvctl configure --provider nico` and `isvctl doctor --provider nico` scope
# to the same variables.
PROVIDER_GROUPS: dict[str, str] = {
    "aws": "AWS",
    "nico": "NICo",
}


@dataclass(frozen=True)
class Section:
    """A provider-namespaced section in the persisted config files.

    `name` is the YAML key (e.g. ``nico``); `group` is the catalog group it
    holds; `env_prefix` is the common prefix shared by every env var in that
    group, used to translate between ``nico.api_base`` and ``NICO_API_BASE``.
    """

    name: str
    group: str
    env_prefix: str


# On-disk config.yml / secrets.yml are organized into these provider-namespaced
# sections instead of a flat env map. Each persistable catalog group maps to one
# section; the "Flags" group has none (flags are never persisted).
SECTIONS: tuple[Section, ...] = (
    Section("isv_lab_service", "ISV Lab Service", "ISV_"),
    Section("ngc", "NGC", "NGC_"),
    Section("aws", "AWS", "AWS_"),
    Section("nico", "NICo", "NICO_"),
)

_GROUP_TO_SECTION: dict[str, Section] = {s.group: s for s in SECTIONS}
_SECTION_BY_NAME: dict[str, Section] = {s.name: s for s in SECTIONS}


def _build_section_maps() -> tuple[dict[str, tuple[str, str]], dict[tuple[str, str], str]]:
    """Build the env-name <-> (section, key) translation tables from the catalog.

    Validates the catalog invariant that every persistable var belongs to a
    section whose prefix it carries — so a drifting catalog fails loudly here.
    """
    env_to_sk: dict[str, tuple[str, str]] = {}
    sk_to_env: dict[tuple[str, str], str] = {}
    for var in ENV_VARS:
        section = _GROUP_TO_SECTION.get(var.group)
        if section is None:
            if var.persistable:
                raise ValueError(f"persistable var {var.name!r} has no section for group {var.group!r}")
            continue
        if not var.name.startswith(section.env_prefix):
            raise ValueError(f"{var.name!r} does not start with section prefix {section.env_prefix!r}")
        key = var.name[len(section.env_prefix) :].lower()
        env_to_sk[var.name] = (section.name, key)
        sk_to_env[(section.name, key)] = var.name
    return env_to_sk, sk_to_env


_ENV_TO_SECTION_KEY, _SECTION_KEY_TO_ENV = _build_section_maps()


def is_known_section(name: str) -> bool:
    """Return True if `name` is a valid top-level section key."""
    return name in _SECTION_BY_NAME


def env_to_section_key(name: str) -> tuple[str, str]:
    """Translate an env var name to its ``(section, key)`` pair."""
    return _ENV_TO_SECTION_KEY[name]


def section_key_to_env(section: str, key: str) -> str | None:
    """Translate a ``(section, key)`` pair to its env var name, or None."""
    return _SECTION_KEY_TO_ENV.get((section, key))


def vars_for_provider(provider: str | None) -> list[EnvVar]:
    """Return catalog vars for a provider group, or all vars when None.

    Args:
        provider: A provider name from `PROVIDER_GROUPS`, or None for every var.

    Raises:
        ValueError: If `provider` is not a known provider name.
    """
    if provider is None:
        return list(ENV_VARS)
    group = PROVIDER_GROUPS.get(provider)
    if group is None:
        valid = ", ".join(sorted(PROVIDER_GROUPS))
        raise ValueError(f"unknown provider '{provider}'. Valid: {valid}")
    return [var for var in ENV_VARS if var.group == group]
