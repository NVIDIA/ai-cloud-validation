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

"""Tests for providers/gcp/scripts/common/service_account.service_account_absent.

The tri-state absence proof feeds two HMAC-lifecycle callers
(``create_access_key`` rollback, ``delete_access_key`` teardown) that consume
``True``/``False``/``None`` and never catch listing exceptions themselves. The
helper materializes the paginated SA list under ``retry_idempotent``, whose
re-raise surface is wider than ``google.api_core`` — it can surface an exhausted
(or non-retryable) ADC ``RefreshError`` and a raw transport disconnect that both
sit OUTSIDE ``GoogleAPICallError``. These tests pin that every unreadable-list
disposition collapses to ``None`` (inconclusive) while genuine programming
errors still propagate.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from google.api_core import exceptions as gax
from google.auth import exceptions as auth_exceptions

# The gcp provider scripts import their shared helpers as the ``common`` package;
# put the provider scripts root on sys.path so ``common.service_account`` (and its
# transitive ``common.errors`` / ``common.network`` imports) resolve.
_SCRIPTS_ROOT = Path(__file__).resolve().parents[2] / "isvctl" / "configs" / "providers" / "gcp" / "scripts"
if str(_SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_ROOT))

from common import service_account as sa  # noqa: E402


class _RemoteDisconnected(Exception):
    """Stand-in whose class NAME matches the transport-disconnect classifier.

    ``common.errors.is_transport_disconnect`` recognizes a raw transport drop by
    class name (``RemoteDisconnected`` / ``ProtocolError``) so it need not
    hard-import urllib3 / requests. Naming this double accordingly makes it a
    genuine transport-disconnect for the helper under test.
    """


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize retry backoff so exhaustion paths run instantly."""
    monkeypatch.setattr("common.errors.time.sleep", lambda *_a, **_k: None)


def _patch_list(monkeypatch: pytest.MonkeyPatch, side_effect):  # type: ignore[no-untyped-def]
    """Replace the SA-list primitive with a counting fake driving ``side_effect``.

    ``side_effect`` is either a return value (list of emails) or an exception
    instance to raise. Returns a one-element list holding the call count so a
    test can assert the retry budget was actually consumed.
    """
    calls = [0]

    def _fake(_project: str) -> list[str]:
        calls[0] += 1
        if isinstance(side_effect, BaseException):
            raise side_effect
        return side_effect

    monkeypatch.setattr(sa, "_list_service_account_emails", _fake)
    return calls


def test_returns_true_when_email_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_list(monkeypatch, ["other@x.iam.gserviceaccount.com"])
    assert sa.service_account_absent("proj", "gone@x.iam.gserviceaccount.com") is True


def test_returns_false_when_email_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_list(monkeypatch, ["still@x.iam.gserviceaccount.com"])
    assert sa.service_account_absent("proj", "still@x.iam.gserviceaccount.com") is False


def test_none_on_terminal_google_api_call_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A terminal (non-transient) GoogleAPICallError re-raised by retry_idempotent.
    _patch_list(monkeypatch, gax.PermissionDenied("list denied"))
    assert sa.service_account_absent("proj", "who@x.iam.gserviceaccount.com") is None


def test_none_on_exhausted_retryable_refresh_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Retryable ADC refresh failure: retry_idempotent retries the transient budget
    # (1 + 3) then re-raises the RefreshError, which is NOT a GoogleAPICallError.
    calls = _patch_list(monkeypatch, auth_exceptions.RefreshError("token refresh failed", retryable=True))
    assert sa.service_account_absent("proj", "who@x.iam.gserviceaccount.com") is None
    assert calls[0] == 4  # initial attempt + transient_retries=3


def test_none_on_nonretryable_refresh_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Non-retryable refresh failure: retry_idempotent re-raises immediately.
    calls = _patch_list(monkeypatch, auth_exceptions.RefreshError("credentials expired"))
    assert sa.service_account_absent("proj", "who@x.iam.gserviceaccount.com") is None
    assert calls[0] == 1


def test_none_on_exhausted_transport_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    # A raw transport disconnect that outlasts retry_idempotent's single transport
    # retry is not a google.api_core type; the helper must still yield None.
    calls = _patch_list(monkeypatch, _RemoteDisconnected("Remote end closed connection without response"))
    assert sa.service_account_absent("proj", "who@x.iam.gserviceaccount.com") is None
    assert calls[0] == 2  # initial attempt + retries=1


def test_reraises_unrelated_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    # A programming / unrelated error is neither transient nor a transport drop,
    # so it must escape rather than masquerade as an inconclusive absence proof.
    _patch_list(monkeypatch, ValueError("bug in caller"))
    with pytest.raises(ValueError, match="bug in caller"):
        sa.service_account_absent("proj", "who@x.iam.gserviceaccount.com")
