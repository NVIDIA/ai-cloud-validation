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

"""Normalization helpers for NICo inventory validation scripts."""

from typing import Any


def first_string(data: dict[str, Any], *keys: str) -> str:
    """Return the first non-empty string-ish field from a NICo API object."""
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
            continue
        if isinstance(value, int | float | bool):
            return str(value)
    return ""


def normalize_state(data: dict[str, Any]) -> str:
    """Normalize NICo state/status fields to validation contract values."""
    raw = first_string(data, "state", "status", "instanceState")
    state = raw.lower()
    if state in {"active", "inuse", "in-use", "ready", "running"}:
        return "running"
    if state in {"deleted", "deleting", "terminated"}:
        return "terminated"
    if state in {"stopped", "stopping"}:
        return "stopped"
    return state or "unknown"


def normalize_instance(data: dict[str, Any]) -> dict[str, str]:
    """Normalize a NICo instance object for instance validations."""
    return {
        "instance_id": first_string(data, "instance_id", "instanceId", "id"),
        "state": normalize_state(data),
        "vpc_id": first_string(data, "vpc_id", "vpcId", "network_id", "networkId"),
        "public_ip": first_string(data, "public_ip", "publicIp", "ip_address", "ipAddress"),
        "private_ip": first_string(data, "private_ip", "privateIp", "internal_ip", "internalIp"),
    }


def normalize_vpc(data: dict[str, Any]) -> dict[str, str]:
    """Normalize a NICo VPC object to tenant-compatible fields."""
    vpc_id = first_string(data, "vpc_id", "vpcId", "id")
    name = first_string(data, "name", "vpc_name", "vpcName") or vpc_id
    return {
        "tenant_id": vpc_id,
        "tenant_name": name,
        "description": first_string(data, "description"),
    }


def normalize_subnet(data: dict[str, Any]) -> dict[str, str]:
    """Normalize a NICo subnet object for network inventory checks."""
    return {
        "subnet_id": first_string(data, "subnet_id", "subnetId", "id"),
        "vpc_id": first_string(data, "vpc_id", "vpcId", "network_id", "networkId"),
        "cidr": first_string(data, "cidr", "cidrBlock", "cidr_block"),
    }


def first_non_empty_id(items: list[dict[str, str]], key: str) -> str:
    """Return the first non-empty identifier in a normalized object list."""
    for item in items:
        value = item.get(key, "").strip()
        if value:
            return value
    return ""
