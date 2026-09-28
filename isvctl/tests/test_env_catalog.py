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

"""Tests for the shared environment-variable catalog."""

import pytest

from isvctl.config.env_catalog import (
    ENV_VARS,
    PROVIDER_GROUPS,
    EnvVar,
    Requirement,
    env_to_section_key,
    section_key_to_env,
    vars_for_provider,
)
from isvctl.redaction import is_secret_env_var


def test_catalog_is_non_empty_and_well_formed() -> None:
    assert ENV_VARS
    for var in ENV_VARS:
        assert isinstance(var, EnvVar)
        assert var.name
        assert var.group
        assert isinstance(var.requirement, Requirement)
        assert var.hint


def test_var_names_are_unique() -> None:
    names = [var.name for var in ENV_VARS]
    assert len(names) == len(set(names))


def test_provider_groups() -> None:
    assert PROVIDER_GROUPS["nico"] == "NICo"
    assert PROVIDER_GROUPS["aws"] == "AWS"
    assert PROVIDER_GROUPS["firebird"] == "Firebird"


def test_vars_for_provider_none_returns_all() -> None:
    assert vars_for_provider(None) == list(ENV_VARS)


def test_vars_for_provider_nico_scopes_to_group() -> None:
    nico_vars = vars_for_provider("nico")
    assert nico_vars
    assert all(var.group == "NICo" for var in nico_vars)
    names = {var.name for var in nico_vars}
    assert "NICO_API_BASE" in names
    assert "NICO_API_NAME" in names
    assert "NICO_CLIENT_SECRET" in names
    assert "AWS_REGION" not in names


def test_vars_for_provider_firebird_scopes_to_group() -> None:
    firebird_vars = vars_for_provider("firebird")
    assert all(var.group == "Firebird" for var in firebird_vars)
    names = {var.name for var in firebird_vars}
    assert {
        "FIREBIRD_API_BASE",
        "FIREBIRD_PROJECT_ID",
        "FIREBIRD_CLIENT_ID",
        "FIREBIRD_CLIENT_SECRET",
        "FIREBIRD_BEARER_TOKEN",
        "FIREBIRD_SSH_USER",
    } <= names
    assert "NICO_API_BASE" not in names


def test_firebird_vars_map_to_the_firebird_section() -> None:
    assert env_to_section_key("FIREBIRD_API_BASE") == ("firebird", "api_base")
    assert section_key_to_env("firebird", "client_secret") == "FIREBIRD_CLIENT_SECRET"


@pytest.mark.parametrize(
    ("name", "secret"),
    [
        ("FIREBIRD_CLIENT_SECRET", True),
        ("FIREBIRD_BEARER_TOKEN", True),
        ("FIREBIRD_CLIENT_ID", False),
        ("FIREBIRD_API_BASE", False),
        ("FIREBIRD_PROJECT_ID", False),
    ],
)
def test_firebird_credentials_route_to_secrets(name: str, secret: bool) -> None:
    """The client secret and bearer token go to secrets.yml; everything else to config.yml."""
    assert is_secret_env_var(name) is secret


def test_vars_for_provider_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown provider"):
        vars_for_provider("gcp")


def test_flags_group_is_not_persistable() -> None:
    flags = [var for var in ENV_VARS if var.group == "Flags"]
    assert flags
    assert all(not var.persistable for var in flags)
    # Specific per-run toggles that must never be persisted.
    names = {var.name for var in flags}
    assert {"KUBECTL", "ISVCTL_DEMO_MODE", "AWS_SKIP_TEARDOWN"} <= names


def test_non_flag_vars_are_persistable() -> None:
    for var in ENV_VARS:
        if var.group != "Flags":
            assert var.persistable, var.name
