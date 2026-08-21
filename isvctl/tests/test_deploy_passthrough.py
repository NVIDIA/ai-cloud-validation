# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for what a deploy forwards to the remote test run."""

import pytest
from isvtest.release_manifest import INCLUDE_UNRELEASED_ENV

from isvctl.cli.deploy import _pytest_passthrough, _remote_env_assignments


def test_passthrough_carries_the_separator() -> None:
    """Without `--`, `test run` reads a bare pytest flag as an unknown isvctl option."""
    assert _pytest_passthrough(["-v", "-s", "-k", "K8sNodeReadyCheck"]) == "-- -v -s -k K8sNodeReadyCheck"


def test_passthrough_is_empty_without_args() -> None:
    """A deploy with no pytest args leaves no dangling separator on the command line."""
    assert _pytest_passthrough([]) == ""


def test_passthrough_quotes_a_multi_word_expression() -> None:
    """The remote shell must receive one -k argument, not three words."""
    assert _pytest_passthrough(["-k", "A or B"]) == "-- -k 'A or B'"


def test_release_gate_reaches_the_remote_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without this the remote run silently skips every unreleased check."""
    monkeypatch.delenv("NGC_NIM_API_KEY", raising=False)
    monkeypatch.setenv("NGC_API_KEY", "secret key")
    monkeypatch.setenv(INCLUDE_UNRELEASED_ENV, "1")

    assert _remote_env_assignments() == f"NGC_API_KEY='secret key' {INCLUDE_UNRELEASED_ENV}=1"


def test_ngc_key_alias_is_forwarded_under_the_canonical_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """The target reads NGC_API_KEY, whichever name it was supplied under here."""
    monkeypatch.delenv("NGC_API_KEY", raising=False)
    monkeypatch.delenv(INCLUDE_UNRELEASED_ENV, raising=False)
    monkeypatch.setenv("NGC_NIM_API_KEY", "nim-key")

    assert _remote_env_assignments() == "NGC_API_KEY=nim-key"


def test_nothing_is_forwarded_when_nothing_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty assignment list must not leave a stray token on the command line."""
    for name in (
        "NGC_API_KEY",
        "NGC_NIM_API_KEY",
        INCLUDE_UNRELEASED_ENV,
        "ISVTEST_BREAKFIX_ALLOW_MUTATION",
        "ISVTEST_BREAKFIX_NODE",
    ):
        monkeypatch.delenv(name, raising=False)

    assert _remote_env_assignments() == ""


def test_breakfix_mutation_controls_are_forwarded_and_quoted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remote focused runs need the same explicit consent and node target."""
    monkeypatch.delenv("NGC_API_KEY", raising=False)
    monkeypatch.delenv("NGC_NIM_API_KEY", raising=False)
    monkeypatch.delenv(INCLUDE_UNRELEASED_ENV, raising=False)
    monkeypatch.setenv("ISVTEST_BREAKFIX_ALLOW_MUTATION", "1")
    monkeypatch.setenv("ISVTEST_BREAKFIX_NODE", "dedicated node")

    assert _remote_env_assignments() == "ISVTEST_BREAKFIX_ALLOW_MUTATION=1 ISVTEST_BREAKFIX_NODE='dedicated node'"
