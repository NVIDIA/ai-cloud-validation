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

"""Tests for the Firebird control-plane and IAM configs and scripts.

Service accounts are faked statefully (``iam_fake.FakeIam``), so one fake
carries an account from creation through rotation to deletion across the real
scripts. Output goes through the real validation classes and the orchestrator's
output schemas; config steps render with the real templating.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import yaml
from isvtest.validations.iam import (
    AccessKeyAuthenticatedCheck,
    AccessKeyCreatedCheck,
    AccessKeyDisabledCheck,
    AccessKeyRejectedCheck,
    TenantInfoCheck,
    TenantListedCheck,
)

from isvctl.redaction import mask_sensitive_args

from .harness import (
    FIREBIRD,
    PROJECT,
    SUITES,
    Route,
    composite,
    config_steps,
    render,
    run,
    schema_errors,
    validate,
)
from .iam_fake import TENANT, FakeIam

USER_ME = {
    "userId": "user.U",
    "tenants": [{"id": TENANT, "name": "acme", "tenantType": "organization", "complianceStatus": "VERIFIED"}],
}


def _checked(step: str, output: dict[str, Any]) -> dict[str, Any]:
    """Assert ``output`` satisfies the output schema of ``step``; return it."""
    assert schema_errors(step, output) == [], output
    return output


def _suite_steps(suite: str) -> set[str]:
    """Return every step name a suite binds a validation to."""
    steps = set()
    for group in yaml.safe_load((SUITES / f"{suite}.yaml").read_text())["tests"]["validations"].values():
        steps.add(group.get("step"))
        steps.update(check.get("step") for check in (group.get("checks") or {}).values())
    return steps - {None}


def _create(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], iam: FakeIam) -> dict[str, Any]:
    """Run create_service_account against ``iam``; return its output."""
    code, out, _ = run(monkeypatch, capsys, "iam/create_service_account.py", iam.routes(), ["--name-prefix", "isv-cp"])
    assert code == 0, out
    return out


def _key_args(created: dict[str, Any]) -> list[str]:
    """Return the credential args the config passes from create_access_key."""
    return [
        f"--service-account-id={created['user_id']}",
        f"--client-id={created['access_key_id']}",
        f"--client-secret={created['secret_access_key']}",
    ]


# ── Config ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("config", "platform", "suite"), [("control-plane", "control_plane", "control-plane"), ("iam", "iam", "iam")]
)
def test_configs_wire_only_suite_steps_setup_first_teardown_last(config: str, platform: str, suite: str) -> None:
    """Every step is a suite step; setup steps come first and teardown steps last."""
    steps = config_steps(config, platform)
    phases = [step.phase for step in steps.values()]

    assert set(steps) <= _suite_steps(suite) | {"check_api", "sweep_leftovers"}
    assert next(iter(steps)) == "sweep_leftovers"
    assert phases == sorted(phases, key=["setup", "test", "teardown"].index)
    assert "labels:" not in (FIREBIRD / "config" / f"{config}.yaml").read_text()


def test_control_plane_leaves_tenant_lifecycle_and_object_storage_unwired() -> None:
    """Gap rows stay unwired so their checks skip as step_not_configured."""
    steps = config_steps("control-plane", "control_plane")

    assert not {"create_tenant", "delete_tenant", "s3_object_lifecycle"} & set(steps)


def test_secret_args_are_masked_in_logs() -> None:
    """Every step that receives the client secret masks it when the command is logged."""
    created = {"user_id": "service-account.1", "access_key_id": "client-1", "secret_access_key": "hunter2"}
    for config, platform, upstream in (
        ("control-plane", "control_plane", "create_access_key"),
        ("iam", "iam", "create_user"),
    ):
        for name, step in config_steps(config, platform).items():
            args = render(config, platform, name, {upstream: created})
            if any("hunter2" in arg for arg in args):
                assert "hunter2" not in mask_sensitive_args(["python3", *args], step.sensitive_args), name


def test_teardowns_delete_the_recorded_account_even_after_a_failed_create() -> None:
    """A create that failed after recording the account still hands its ID to teardown."""
    failed = {"success": False, "user_id": "service-account.1", "access_key_id": "client-1"}

    assert render("control-plane", "control_plane", "delete_access_key", {"create_access_key": failed}) == [
        "--service-account-ids=service-account.1"
    ]
    assert render("iam", "iam", "teardown", {"create_user": failed}) == ["--service-account-ids=service-account.1"]
    assert render("iam", "iam", "teardown", {}) == ["--service-account-ids="]


# ── Access key lifecycle (control-plane) ──────────────────────────────


def test_check_api_reports_the_project_tenant_as_the_account(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CP03-01 passes on /users/me plus the project read; account_id is the tenant."""
    code, out, api = run(monkeypatch, capsys, "control-plane/check_api.py", FakeIam().routes())

    assert code == 0
    assert out["account_id"] == TENANT
    assert api.paths() == ["GET /users/me", f"GET /projects/{PROJECT}"]
    assert composite("control-plane", "ControlPlaneApiHealthCheck", _checked("check_api", out)) == []


def test_access_key_lifecycle_passes_when_rotation_revokes_the_old_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Create -> authenticate -> rotate -> old secret refused -> delete, all through the real checks."""
    iam = FakeIam()
    created = _checked("create_access_key", _create(monkeypatch, capsys, iam))
    assert validate("control-plane", AccessKeyCreatedCheck, created)._passed
    assert iam.roles[(created["user_id"], PROJECT)] == ["VIEWER"]

    code, auth, api = run(monkeypatch, capsys, "iam/test_credentials.py", iam.routes(), _key_args(created))
    assert code == 0, auth
    assert auth["identity_id"] == created["user_id"] and auth["account_id"] == TENANT
    # /users/me and the project are read with the new account's own token.
    assert api.tokens[-2:] == [f"tok:{created['user_id']}"] * 2
    assert validate("control-plane", AccessKeyAuthenticatedCheck, _checked("test_access_key", auth))._passed

    code, disabled, _ = run(
        monkeypatch, capsys, "control-plane/disable_access_key.py", iam.routes(), _key_args(created)
    )
    assert code == 0, disabled
    assert disabled["status"] == "Inactive"
    assert iam.accounts[created["user_id"]]["secrets"][0] not in json.dumps(disabled)  # new secret never emitted
    assert validate("control-plane", AccessKeyDisabledCheck, _checked("disable_access_key", disabled))._passed

    code, rejected, _ = run(
        monkeypatch, capsys, "control-plane/verify_key_rejected.py", iam.routes(), _key_args(created)[1:]
    )
    assert code == 0
    assert rejected["rejected"] is True and rejected["error_code"] == "HTTP 401"
    assert validate("control-plane", AccessKeyRejectedCheck, _checked("verify_key_rejected", rejected))._passed

    args = render("control-plane", "control_plane", "delete_access_key", {"create_access_key": created})
    code, deleted, _ = run(monkeypatch, capsys, "iam/teardown.py", iam.routes(), args)
    assert code == 0
    assert iam.deleted == [created["user_id"]]
    assert composite("control-plane", "AccessKeyDeletedCheck", _checked("delete_access_key", deleted)) == []


def test_old_secret_still_accepted_after_rotation_fails_both_checks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rotation grace period (old secret still valid) must fail CP06-01, not pass it."""
    iam = FakeIam(keep_rotated_secrets=True)
    created = _create(monkeypatch, capsys, iam)

    code, disabled, _ = run(
        monkeypatch, capsys, "control-plane/disable_access_key.py", iam.routes(), _key_args(created)
    )
    assert code == 1
    assert disabled["status"] == "Active"
    assert "rotation policy" in disabled["error"]
    assert not validate("control-plane", AccessKeyDisabledCheck, _checked("disable_access_key", disabled))._passed

    argv = [*_key_args(created)[1:], "--retries", "3", "--wait", "0"]
    before = len(iam.token_requests)
    code, rejected, _ = run(monkeypatch, capsys, "control-plane/verify_key_rejected.py", iam.routes(), argv)
    assert code == 1
    assert rejected["rejected"] is False and rejected["attempts"] == 3
    assert len(iam.token_requests) - before == 3
    assert not validate("control-plane", AccessKeyRejectedCheck, _checked("verify_key_rejected", rejected))._passed


def test_verify_key_rejected_treats_a_server_error_as_inconclusive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 5xx from /auth/token is neither a refusal nor an acceptance: the step fails."""
    argv = ["--client-id=c", "--client-secret=s"]
    code, out, _ = run(monkeypatch, capsys, "control-plane/verify_key_rejected.py", {"POST /auth/token": 503}, argv)

    assert code == 1
    assert out["rejected"] is False
    assert "503" in out["error"]


def test_test_credentials_fails_identity_when_the_token_names_another_account(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The identity probe compares /users/me with the account the step created."""
    iam = FakeIam()
    created = _create(monkeypatch, capsys, iam)
    argv = ["--service-account-id=service-account.other", *_key_args(created)[1:]]

    code, out, _ = run(monkeypatch, capsys, "iam/test_credentials.py", iam.routes(), argv)

    assert code == 1
    assert out["authenticated"] is False
    assert "expected service-account.other" in out["tests"]["identity"]["error"]


def test_test_credentials_retries_a_just_created_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refusal right after creation is retried (credential propagation), then succeeds."""
    iam = FakeIam()
    created = _create(monkeypatch, capsys, iam)
    routes = iam.routes()
    token_route = routes["POST /auth/token"]
    calls = {"n": 0}

    def flaky(body: Any, query: Any) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            return token_route({**body, "clientSecret": "not-yet"}, query)  # type: ignore[operator]
        return token_route(body, query)  # type: ignore[operator]

    routes["POST /auth/token"] = flaky
    code, out, _ = run(monkeypatch, capsys, "iam/test_credentials.py", routes, _key_args(created))

    assert code == 0, out
    assert calls["n"] == 2


# ── Tenants ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(("action", "step"), [("list", "list_tenants"), ("get", "get_tenant")])
def test_tenant_steps_skip_for_a_service_account_caller(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], action: str, step: str
) -> None:
    """When /users/me lists no tenants for a service account, the run skips (schema-valid)."""
    code, out, _ = run(monkeypatch, capsys, "control-plane/tenants.py", FakeIam().routes(), ["--action", action])

    assert code == 0
    assert out["skipped"] is True
    assert "user" in out["skip_reason"]
    _checked(step, out)


def test_tenant_steps_read_the_project_tenant_for_a_user(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A user's /users/me tenants[] must contain the tenant of the run's project."""
    routes = FakeIam(admin_me=USER_ME).routes()

    _, listed, _ = run(monkeypatch, capsys, "control-plane/tenants.py", routes, ["--action", "list"])
    _, info, _ = run(monkeypatch, capsys, "control-plane/tenants.py", routes, ["--action", "get"])

    assert listed["found_target"] is True and listed["count"] == 1
    assert validate("control-plane", TenantListedCheck, _checked("list_tenants", listed))._passed
    assert info["tenant_name"] == "acme" and info["tenant_id"] == TENANT
    assert validate("control-plane", TenantInfoCheck, _checked("get_tenant", info))._passed


@pytest.mark.parametrize("action", ["list", "get"])
def test_tenant_steps_fail_for_a_user_without_tenants(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], action: str
) -> None:
    """Only a service-account caller skips; a user whose tenants[] is empty fails."""
    routes = FakeIam(admin_me={"userId": "user.U", "tenants": []}).routes()

    code, out, _ = run(monkeypatch, capsys, "control-plane/tenants.py", routes, ["--action", action])

    assert code == 1
    assert "skipped" not in out
    assert "not in /users/me tenants" in out["error"]


def test_tenant_list_fails_when_the_project_tenant_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A user whose tenants[] lacks the project's tenant fails CP08-01 instead of passing on a count."""
    me = {"userId": "user.U", "tenants": [{"id": "tenant.other", "name": "other"}]}

    code, out, _ = run(
        monkeypatch, capsys, "control-plane/tenants.py", FakeIam(admin_me=me).routes(), ["--action", "list"]
    )

    assert code == 1
    assert out["found_target"] is False
    assert not validate("control-plane", TenantListedCheck, out)._passed


# ── IAM user lifecycle ────────────────────────────────────────────────


def test_iam_user_lifecycle_passes_through_the_real_checks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """create_user -> test_credentials (identity + project access) -> teardown."""
    iam = FakeIam()
    _, created, _ = run(
        monkeypatch, capsys, "iam/create_service_account.py", iam.routes(), ["--name-prefix", "isv-iam"]
    )
    assert composite("iam", "IamUserCreatedCheck", _checked("create_user", created)) == []
    assert created["username"].startswith("isv-iam-")

    argv = render("iam", "iam", "test_credentials", {"create_user": created})
    code, creds, _ = run(monkeypatch, capsys, "iam/test_credentials.py", iam.routes(), argv)
    assert code == 0, creds
    assert composite("iam", "IamUserAuthenticatedCheck", _checked("test_credentials", creds)) == []
    assert composite("iam", "IamUserApiAccessCheck", creds) == []

    argv = render("iam", "iam", "teardown", {"create_user": created})
    code, deleted, _ = run(monkeypatch, capsys, "iam/teardown.py", iam.routes(), argv)
    assert code == 0 and iam.deleted == [created["user_id"]]
    assert composite("iam", "IamUserDeletedCheck", _checked("teardown", deleted)) == []


def test_access_fails_without_a_project_role(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """IAM03-01 needs the project read to succeed; an account without a role is denied."""
    iam = FakeIam()
    _, created, _ = run(monkeypatch, capsys, "iam/create_service_account.py", iam.routes(), ["--project-role", ""])
    assert iam.roles[(created["user_id"], PROJECT)] == []

    _, creds, _ = run(monkeypatch, capsys, "iam/test_credentials.py", iam.routes(), _key_args(created))

    assert creds["tests"]["identity"]["passed"] is True
    assert creds["tests"]["access"]["passed"] is False
    assert composite("iam", "IamUserApiAccessCheck", creds)


def test_a_failed_role_grant_still_records_the_account_for_teardown(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The account ID is emitted before the grant, and teardown deletes it."""
    iam = FakeIam()
    routes = iam.routes()
    routes["PUT /projects/project.P/service-accounts/service-account.1/roles"] = 400

    code, created, _ = run(monkeypatch, capsys, "iam/create_service_account.py", routes)
    assert code == 1
    assert created["user_id"] == "service-account.1"
    _checked("create_user", created)

    argv = render("iam", "iam", "teardown", {"create_user": created})
    code, _, _ = run(monkeypatch, capsys, "iam/teardown.py", iam.routes(), argv)
    assert code == 0 and iam.deleted == ["service-account.1"]


def _account(sa_id: str, name: str) -> dict[str, Any]:
    """Return a GET /service-accounts/{id} readback (the flat ServiceAccountResponse)."""
    return {"id": sa_id, "displayName": name, "tenantId": TENANT}


def _project(project_id: str, name: str) -> dict[str, Any]:
    """Return a GET /projects/{id} readback."""
    return {"project": {"id": project_id, "name": name, "tenantId": TENANT}}


def test_teardown_attempts_every_resource_and_reports_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One failing delete neither stops the others nor passes the step; 404 counts as gone."""
    routes: dict[str, Route] = {
        "GET /service-accounts/service-account.1": _account("service-account.1", "isv-iam-000001"),
        "GET /service-accounts/service-account.2": _account("service-account.2", "isv-cp-000002"),
        "GET /service-accounts/service-account.3": 404,
        "GET /projects/project.B": _project("project.B", "isv-lp-00000b"),
        "GET /projects/project.C": _project("project.C", "isv-lp-00000c"),
        "DELETE /service-accounts/service-account.1": 500,
        "DELETE /service-accounts/service-account.2": {},
        "DELETE /projects/project.B": {},
        "DELETE /projects/project.C": 404,
    }
    argv = [
        "--service-account-ids=service-account.1,service-account.2,service-account.3",
        "--project-ids=project.B,project.C",
    ]

    code, out, api = run(monkeypatch, capsys, "iam/teardown.py", routes, argv)

    assert code == 1
    assert api.paths("DELETE")[-2:] == ["DELETE /projects/project.B", "DELETE /projects/project.C"]  # after accounts
    assert "DELETE /service-accounts/service-account.3" not in api.paths()  # already gone at the read
    assert out["resources_deleted"] == [
        "service_account:service-account.2",
        "service_account:service-account.3",
        "project:project.B",
        "project:project.C",
    ]
    assert out["resources_failed"][0].startswith("service_account:service-account.1")
    assert composite("iam", "IamUserDeletedCheck", out)


def test_teardown_reports_a_project_delete_that_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only 404 counts as gone for a project; any other error fails the step."""
    routes: dict[str, Route] = {
        "GET /projects/project.B": _project("project.B", "isv-lp-00000b"),
        "DELETE /projects/project.B": 500,
    }

    code, out, api = run(monkeypatch, capsys, "iam/teardown.py", routes, ["--project-ids=project.B"])

    assert code == 1
    assert api.paths("DELETE") == ["DELETE /projects/project.B"]
    assert out["resources_deleted"] == []
    assert out["resources_failed"][0].startswith("project:project.B")


@pytest.mark.parametrize(
    ("argv", "routes"),
    [
        (
            ["--service-account-ids=service-account.admin"],
            {"GET /service-accounts/service-account.admin": _account("service-account.admin", "tenant-admin")},
        ),
        (
            ["--service-account-ids=service-account.look"],
            {"GET /service-accounts/service-account.look": _account("service-account.look", "isv-cp-0a1b2c-x")},
        ),
        (["--project-ids=project.prod"], {"GET /projects/project.prod": _project("project.prod", "production")}),
        (["--project-ids=project.look"], {"GET /projects/project.look": _project("project.look", "isv-lp-0a1b2")}),
        ([f"--project-ids={PROJECT}"], {}),
    ],
    ids=["foreign-account", "lookalike-account", "foreign-project", "lookalike-project", "run-project"],
)
def test_teardown_refuses_resources_not_named_by_the_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str], routes: dict[str, Route]
) -> None:
    """A wrong ID (the admin account, the run's own project, a look-alike name) fails the step and deletes nothing."""
    code, out, api = run(monkeypatch, capsys, "iam/teardown.py", routes, argv)

    assert code == 1 and out["success"] is False
    assert api.paths("DELETE") == []
    assert out["resources_deleted"] == []
    assert "refusing to delete" in out["resources_failed"][0]


def test_teardown_with_nothing_recorded_makes_no_calls(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run whose create never recorded an account tears down nothing, successfully."""
    code, out, api = run(monkeypatch, capsys, "iam/teardown.py", {}, ["--service-account-ids="])

    assert code == 0 and out["success"] is True
    assert api.calls == []
