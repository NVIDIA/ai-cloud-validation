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

"""Tests for the redaction module.

Verifies that secrets are correctly redacted across all output formats:
CLI arguments, dict keys, environment variables, free text, and JUnit XML.
"""

import xml.etree.ElementTree as ET

from isvctl.redaction import (
    REDACTED,
    filter_env,
    is_secret_env_var,
    is_sensitive_key,
    mask_sensitive_args,
    redact_dict,
    redact_junit_xml_tree,
    redact_text,
)

# ---------------------------------------------------------------------------
# mask_sensitive_args
# ---------------------------------------------------------------------------


class TestMaskSensitiveArgs:
    """Tests for CLI argument masking."""

    def test_masks_secret_access_key_flag_value(self) -> None:
        parts = ["aws", "--secret-access-key", "wJalrXUtnFEMI/EXAMPLEKEY"]
        result = mask_sensitive_args(parts)
        assert "wJalrXUtnFEMI" not in result
        assert REDACTED in result

    def test_masks_access_key_id(self) -> None:
        parts = ["cmd", "--access-key-id", "AKIAIOSFODNN7EXAMPLE"]
        result = mask_sensitive_args(parts)
        assert "AKIAIOSFODNN7EXAMPLE" not in result

    def test_masks_connection_string(self) -> None:
        parts = ["cmd", "--connection-string", "Server=tcp:myserver.database.windows.net"]
        result = mask_sensitive_args(parts)
        assert "myserver.database" not in result

    def test_masks_password_equals_format(self) -> None:
        parts = ["cmd", "--password=hunter2"]
        result = mask_sensitive_args(parts)
        assert "hunter2" not in result
        assert f"--password={REDACTED}" in result

    def test_masks_api_key(self) -> None:
        parts = ["cmd", "--api-key", "sk-abc123"]
        result = mask_sensitive_args(parts)
        assert "sk-abc123" not in result

    def test_preserves_non_sensitive_args(self) -> None:
        parts = ["cmd", "--region", "us-west-2", "--nodes", "4"]
        result = mask_sensitive_args(parts)
        assert "us-west-2" in result
        assert "4" in result

    def test_extra_patterns_from_config(self) -> None:
        parts = ["cmd", "--my-custom-secret", "top-secret"]
        result = mask_sensitive_args(parts, extra_patterns=["--my-custom-secret"])
        assert "top-secret" not in result

    def test_token_flag(self) -> None:
        parts = ["cmd", "--token", "eyJhbGciOiJSUzI1NiJ9"]
        result = mask_sensitive_args(parts)
        assert "eyJhbGciOiJSUzI1NiJ9" not in result


# ---------------------------------------------------------------------------
# is_sensitive_key / redact_dict
# ---------------------------------------------------------------------------


class TestIsSensitiveKey:
    """Tests for dict key sensitivity detection."""

    def test_aws_keys(self) -> None:
        assert is_sensitive_key("secret_access_key")
        assert is_sensitive_key("access_key_id")
        assert is_sensitive_key("AWS_SECRET_ACCESS_KEY")
        assert is_sensitive_key("AWS_ACCESS_KEY_ID")

    def test_azure_keys(self) -> None:
        assert is_sensitive_key("account_key")
        assert is_sensitive_key("subscription_key")
        assert is_sensitive_key("connection_string")
        assert is_sensitive_key("sas_token")
        assert is_sensitive_key("signing_key")
        assert is_sensitive_key("AZURE_STORAGE_ACCOUNT_KEY")

    def test_general_keys(self) -> None:
        assert is_sensitive_key("password")
        assert is_sensitive_key("api_key")
        assert is_sensitive_key("private_key")
        assert is_sensitive_key("auth_token")
        assert is_sensitive_key("client_secret")

    def test_prefixed_keys(self) -> None:
        assert is_sensitive_key("NGC_API_KEY")
        assert is_sensitive_key("IAM_API_KEY")
        assert is_sensitive_key("GOOGLE_API_KEY")
        assert is_sensitive_key("user_password")
        assert is_sensitive_key("db_password")

    def test_non_sensitive_keys(self) -> None:
        assert not is_sensitive_key("cluster_name")
        assert not is_sensitive_key("region")
        assert not is_sensitive_key("node_count")
        assert not is_sensitive_key("success")
        assert not is_sensitive_key("platform")


class TestRedactDict:
    """Tests for recursive dict redaction."""

    def test_redacts_aws_credentials(self) -> None:
        data = {
            "success": True,
            "access_key_id": "AKIAIOSFODNN7EXAMPLE",
            "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        }
        result = redact_dict(data)
        assert result["success"] is True
        assert result["access_key_id"] == REDACTED
        assert result["secret_access_key"] == REDACTED

    def test_redacts_azure_credentials(self) -> None:
        data = {
            "account_key": "base64storagekey==",
            "connection_string": "DefaultEndpointsProtocol=https;AccountName=...",
            "resource_group": "my-rg",
        }
        result = redact_dict(data)
        assert result["account_key"] == REDACTED
        assert result["connection_string"] == REDACTED
        assert result["resource_group"] == "my-rg"

    def test_redacts_nested_dicts(self) -> None:
        data = {"outer": {"credentials": {"api_key": "secret123", "name": "test"}}}
        result = redact_dict(data)
        assert result["outer"]["credentials"]["api_key"] == REDACTED
        assert result["outer"]["credentials"]["name"] == "test"

    def test_redacts_in_lists(self) -> None:
        data = [{"password": "abc"}, {"name": "safe"}]
        result = redact_dict(data)
        assert result[0]["password"] == REDACTED
        assert result[1]["name"] == "safe"

    def test_none_passthrough(self) -> None:
        assert redact_dict(None) is None

    def test_non_string_values_redacted(self) -> None:
        """Sensitive keys are redacted regardless of value type."""
        data = {"password": 12345, "auth_token": True, "api_key": None}
        result = redact_dict(data)
        assert result["password"] == REDACTED
        assert result["auth_token"] == REDACTED
        assert result["api_key"] == REDACTED

    def test_real_create_user_output(self) -> None:
        """Simulate actual create_user.py script output."""
        data = {
            "success": True,
            "platform": "iam",
            "username": "test-user",
            "access_key_id": "AKIAIOSFODNN7EXAMPLE",
            "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        }
        result = redact_dict(data)
        assert result["username"] == "test-user"
        assert result["access_key_id"] == REDACTED
        assert result["secret_access_key"] == REDACTED


# ---------------------------------------------------------------------------
# filter_env
# ---------------------------------------------------------------------------


class TestFilterEnv:
    """Tests for environment variable filtering."""

    def test_removes_aws_secrets(self) -> None:
        env = {
            "HOME": "/home/user",
            "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
            "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI",
            "AWS_SESSION_TOKEN": "token123",
            "AWS_REGION": "us-west-2",
            "PATH": "/usr/bin",
        }
        result = filter_env(env)
        assert "HOME" in result
        assert "PATH" in result
        assert "AWS_REGION" in result
        assert "AWS_ACCESS_KEY_ID" not in result
        assert "AWS_SECRET_ACCESS_KEY" not in result
        assert "AWS_SESSION_TOKEN" not in result

    def test_removes_azure_secrets(self) -> None:
        env = {
            "AZURE_CLIENT_SECRET": "secret",
            "AZURE_STORAGE_KEY": "key",
            "AZURE_SUBSCRIPTION_KEY": "subkey",
            "AZURE_TENANT_ID": "tenant-id",
        }
        result = filter_env(env)
        assert "AZURE_CLIENT_SECRET" not in result
        assert "AZURE_STORAGE_KEY" not in result
        assert "AZURE_SUBSCRIPTION_KEY" not in result
        assert "AZURE_TENANT_ID" in result

    def test_removes_nvidia_secrets(self) -> None:
        env = {"NGC_API_KEY": "nvapi-abc123", "NGC_NIM_API_KEY": "nvapi-def456"}
        result = filter_env(env)
        assert "NGC_API_KEY" not in result
        assert "NGC_NIM_API_KEY" not in result

    def test_removes_suffix_matches(self) -> None:
        env = {
            "MY_CUSTOM_SECRET": "value",
            "DB_PASSWORD": "dbpass",
            "CUSTOM_API_KEY": "key",
            "STORAGE_ACCOUNT_KEY": "storagekey",
            "DB_CONNECTION_STRING": "connstr",
            "SAFE_VAR": "ok",
        }
        result = filter_env(env)
        assert "SAFE_VAR" in result
        assert "MY_CUSTOM_SECRET" not in result
        assert "DB_PASSWORD" not in result
        assert "CUSTOM_API_KEY" not in result
        assert "STORAGE_ACCOUNT_KEY" not in result
        assert "DB_CONNECTION_STRING" not in result

    def test_preserves_non_sensitive_skip_flags(self) -> None:
        """Env vars used in Jinja2 templates must not be filtered."""
        env = {
            "AWS_BM_SKIP_TEARDOWN": "true",
            "AWS_IAM_SKIP_TEARDOWN": "false",
            "AWS_REGION": "us-west-2",
        }
        result = filter_env(env)
        assert result == env


# ---------------------------------------------------------------------------
# is_secret_env_var
# ---------------------------------------------------------------------------


class TestIsSecretEnvVar:
    """Tests for environment-variable secret classification."""

    def test_explicit_deny_list(self) -> None:
        assert is_secret_env_var("NICO_CLIENT_SECRET")
        assert is_secret_env_var("NICO_BEARER_TOKEN")
        assert is_secret_env_var("NGC_API_KEY")
        assert is_secret_env_var("ISV_CLIENT_SECRET")
        assert is_secret_env_var("AWS_SECRET_ACCESS_KEY")

    def test_suffix_matches(self) -> None:
        assert is_secret_env_var("MY_CUSTOM_SECRET")
        assert is_secret_env_var("DB_PASSWORD")
        assert is_secret_env_var("CUSTOM_API_KEY")

    def test_non_secret_vars(self) -> None:
        assert not is_secret_env_var("NICO_API_BASE")
        assert not is_secret_env_var("NICO_ORGANIZATION")
        assert not is_secret_env_var("NICO_SITE_ID")
        assert not is_secret_env_var("NICO_CLIENT_ID")
        assert not is_secret_env_var("KUBECTL")
        assert not is_secret_env_var("AWS_REGION")


# ---------------------------------------------------------------------------
# redact_text
# ---------------------------------------------------------------------------


class TestRedactText:
    """Tests for free-text redaction."""

    def test_json_double_quoted(self) -> None:
        text = '{"secret_access_key": "wJalrXUtnFEMI/EXAMPLEKEY", "name": "test"}'
        result = redact_text(text)
        assert "wJalrXUtnFEMI" not in result
        assert f'"secret_access_key": "{REDACTED}"' in result
        assert '"name": "test"' in result

    def test_json_single_quoted_python_repr(self) -> None:
        """Python dict repr uses single quotes."""
        text = "{'NGC_API_KEY': 'nvapi-abc123', 'debug': 'true'}"
        result = redact_text(text)
        assert "nvapi-abc123" not in result
        assert "'debug': 'true'" in result

    def test_key_equals_value(self) -> None:
        text = "export NGC_API_KEY=nvapi-abc123 NCCL_DEBUG=INFO"
        result = redact_text(text)
        assert "nvapi-abc123" not in result
        assert "NCCL_DEBUG=INFO" in result

    def test_prefixed_key_in_json(self) -> None:
        """NGC_API_KEY must be caught, not just api_key."""
        text = '"NGC_API_KEY": "nvapi-secret"'
        result = redact_text(text)
        assert "nvapi-secret" not in result

    def test_aws_secret_in_json(self) -> None:
        text = '"AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG"'
        result = redact_text(text)
        assert "wJalrXUtnFEMI" not in result

    def test_sbatch_script_export(self) -> None:
        """Simulates export lines in a generated sbatch script."""
        text = "export NGC_API_KEY=nvapi-real-key\nexport NCCL_SOCKET_IFNAME=eth0"
        result = redact_text(text)
        assert "nvapi-real-key" not in result
        assert "NCCL_SOCKET_IFNAME=eth0" in result

    def test_no_false_positive_on_safe_keys(self) -> None:
        text = '"cluster_name": "my-cluster", "region": "us-west-2"'
        assert redact_text(text) == text


# ---------------------------------------------------------------------------
# redact_junit_xml_tree
# ---------------------------------------------------------------------------


class TestRedactJunitXmlTree:
    """Tests for JUnit XML sanitisation."""

    def test_redacts_failure_text(self) -> None:
        xml_str = """<testsuites>
          <testsuite name="test">
            <testcase name="test_creds">
              <failure message="failed">
                Output: {"secret_access_key": "wJalrXUtnFEMI/EXAMPLEKEY"}
              </failure>
            </testcase>
          </testsuite>
        </testsuites>"""
        root = ET.fromstring(xml_str)
        redact_junit_xml_tree(root)
        failure = root.find(".//failure")
        assert failure is not None
        assert "wJalrXUtnFEMI" not in (failure.text or "")
        assert REDACTED in (failure.text or "")

    def test_redacts_failure_message_attr(self) -> None:
        xml_str = """<testsuites>
          <testsuite>
            <testcase name="t">
              <failure message='secret_access_key=wJalrXUtnFEMI'>text</failure>
            </testcase>
          </testsuite>
        </testsuites>"""
        root = ET.fromstring(xml_str)
        redact_junit_xml_tree(root)
        failure = root.find(".//failure")
        assert failure is not None
        assert "wJalrXUtnFEMI" not in failure.get("message", "")

    def test_redacts_system_out(self) -> None:
        xml_str = """<testsuites>
          <testsuite>
            <testcase name="t">
              <system-out>export NGC_API_KEY=nvapi-secret</system-out>
            </testcase>
          </testsuite>
        </testsuites>"""
        root = ET.fromstring(xml_str)
        redact_junit_xml_tree(root)
        sysout = root.find(".//system-out")
        assert sysout is not None
        assert "nvapi-secret" not in (sysout.text or "")

    def test_redacts_system_err(self) -> None:
        xml_str = """<testsuites>
          <testsuite>
            <testcase name="t">
              <system-err>"password": "hunter2"</system-err>
            </testcase>
          </testsuite>
        </testsuites>"""
        root = ET.fromstring(xml_str)
        redact_junit_xml_tree(root)
        syserr = root.find(".//system-err")
        assert syserr is not None
        assert "hunter2" not in (syserr.text or "")

    def test_leaves_safe_content_alone(self) -> None:
        xml_str = """<testsuites>
          <testsuite>
            <testcase name="t">
              <failure message="assertion failed">expected 4 nodes</failure>
              <system-out>cluster is healthy</system-out>
            </testcase>
          </testsuite>
        </testsuites>"""
        root = ET.fromstring(xml_str)
        redact_junit_xml_tree(root)
        failure = root.find(".//failure")
        assert failure is not None
        assert failure.text == "expected 4 nodes"
        assert failure.get("message") == "assertion failed"
