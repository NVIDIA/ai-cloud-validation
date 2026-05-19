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

"""Unit tests for clean-up operations."""

from isvctl.cleaner.operations import (
    OPERATIONS,
    bcm_validation,
    firmware_flashing,
    firmware_validation,
    network_configuration_reset,
    os_configuration,
    os_reimage,
)


def test_firmware_validation() -> None:
    """Test firmware validation operation."""
    result = firmware_validation()
    assert isinstance(result, dict)
    assert "success" in result
    assert result["success"] is True
    assert "message" in result


def test_firmware_flashing() -> None:
    """Test firmware flashing operation."""
    result = firmware_flashing()
    assert isinstance(result, dict)
    assert "success" in result
    assert result["success"] is True
    assert "message" in result


def test_network_configuration_reset() -> None:
    """Test network configuration reset operation."""
    result = network_configuration_reset()
    assert isinstance(result, dict)
    assert "success" in result
    assert result["success"] is True
    assert "message" in result


def test_bcm_validation() -> None:
    """Test BCM validation operation."""
    result = bcm_validation()
    assert isinstance(result, dict)
    assert "success" in result
    assert result["success"] is True
    assert "message" in result


def test_os_reimage() -> None:
    """Test OS reimage operation."""
    result = os_reimage()
    assert isinstance(result, dict)
    assert "success" in result
    assert result["success"] is True
    assert "message" in result


def test_os_configuration() -> None:
    """Test OS configuration operation."""
    result = os_configuration()
    assert isinstance(result, dict)
    assert "success" in result
    assert result["success"] is True
    assert "message" in result


def test_operations_registry() -> None:
    """Test that all operations are registered correctly."""
    assert isinstance(OPERATIONS, dict)
    assert len(OPERATIONS) == 6

    # Check that all expected operations are registered
    expected_operations = [
        "firmware-validation",
        "firmware-flashing",
        "network-reset",
        "bcm-validation",
        "os-reimage",
        "os-config",
    ]

    for op_name in expected_operations:
        assert op_name in OPERATIONS
        operation_func, description = OPERATIONS[op_name]
        assert callable(operation_func)
        assert isinstance(description, str)
        assert len(description) > 0


def test_operations_registry_callable() -> None:
    """Test that all registered operations can be called."""
    for op_name, (operation_func, description) in OPERATIONS.items():
        result = operation_func()
        assert isinstance(result, dict), f"Operation {op_name} should return a dict"
        assert "success" in result, f"Operation {op_name} should have 'success' key"
        assert isinstance(result["success"], bool), f"Operation {op_name} 'success' should be bool"
