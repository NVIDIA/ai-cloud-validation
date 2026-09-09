# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for what a deploy forwards to the remote test run."""

import pytest
from isvreporter.version import BUILD_REF_ENV

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


def test_source_reference_reaches_the_remote_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """The archive excludes .git, so source identity must be forwarded first."""
    monkeypatch.delenv("NGC_NIM_API_KEY", raising=False)
    monkeypatch.setenv("NGC_API_KEY", "secret key")
    monkeypatch.setenv(BUILD_REF_ENV, "v1.2.3-2-gabc1234")

    assert _remote_env_assignments() == "NGC_API_KEY='secret key' ISVTEST_BUILD_REF=v1.2.3-2-gabc1234"


def test_ngc_key_alias_is_forwarded_under_the_canonical_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """The target reads NGC_API_KEY, whichever name it was supplied under here."""
    monkeypatch.delenv("NGC_API_KEY", raising=False)
    monkeypatch.delenv(BUILD_REF_ENV, raising=False)
    monkeypatch.setenv("NGC_NIM_API_KEY", "nim-key")
    monkeypatch.setattr("isvctl.cli.deploy.build_ref", lambda: None)

    assert _remote_env_assignments() == "NGC_API_KEY=nim-key"


def test_nothing_is_forwarded_when_nothing_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty assignment list must not leave a stray token on the command line."""
    for name in ("NGC_API_KEY", "NGC_NIM_API_KEY", BUILD_REF_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("isvctl.cli.deploy.build_ref", lambda: None)

    assert _remote_env_assignments() == ""
