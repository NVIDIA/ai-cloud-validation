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

"""The layer of each requirement, from the catalog heading that it sits under.

Where a requirement sits in the catalog is a fact of the catalog. Which layer a
reader thinks it asks for is a judgement, and that judgement is not stable.
So the heading decides: each requirement's layers include the layer
of its heading, and a part can add another layer.

`HEADINGS` is the one copy of the table. The coverage skill shows the same
table to the agent, and a test holds the two equal.
"""

from __future__ import annotations

from typing import Any

#: Each layer and the catalog headings that it covers. A heading is a section
#: or a subsection of the catalog, spelled as the catalog spells it.
HEADINGS: dict[str, tuple[str, ...]] = {
    "iam": ("Identity and Access Management (IAM)", "Cryptography and Key Management"),
    "network": (
        "SDN and Virtual Networking",
        "Network Isolation and Encryption",
        "Edge Network Security",
        "Network Transport and Fabric Visibility",
        "Transport and Networking Requirements",
    ),
    "cluster": ("Kubernetes as a Service (KaaS) Requirements",),
    "compute": (
        "General, Compute, and Lifecycle Management",
        "Boot Process and Disks",
        "Hardware Security and Compliance",
        "Break-Fix Requirements",
        "Capacity and Fleet Management",
    ),
    "other": ("Storage Requirements", "Testing Compliance"),
}

_LAYER_OF = {heading: layer for layer, headings in HEADINGS.items() for heading in headings}

#: The catalog's levels, deepest first: the deepest heading that the table names
#: is the one that decides.
_LEVELS = ("subsection", "section", "chapter")


class UnknownHeading(Exception):
    """A served requirement sits under no heading that the table names."""


def heading_and_layer(entry: dict[str, Any]) -> tuple[str, str] | None:
    """The deepest heading of `entry` that the table names, and its layer."""
    for level in _LEVELS:
        heading = str(entry.get(level) or "")
        if heading in _LAYER_OF:
            return heading, _LAYER_OF[heading]
    return None


def layers(catalog: dict[str, dict[str, Any]]) -> dict[str, tuple[str, str]]:
    """Each served requirement's heading and layer.

    A served requirement under a heading that the table does not name stops
    here. Putting it in `other` would hide a catalog revision that added or
    renamed a section, and the table would never learn of it.
    """
    found: dict[str, tuple[str, str]] = {}
    unknown: list[str] = []
    for req_id, entry in catalog.items():
        if not entry.get("served", True):
            continue
        placed = heading_and_layer(entry)
        if placed is None:
            where = " > ".join(str(entry.get(level)) for level in reversed(_LEVELS) if entry.get(level))
            unknown.append(f"{req_id} ({where or 'no heading'})")
        else:
            found[req_id] = placed
    if unknown:
        raise UnknownHeading(
            "these requirements sit under a catalog heading that no layer names: "
            + ", ".join(unknown)
            + ". Add the heading to a layer, `other` included, in catalog_layers.HEADINGS and in the "
            "skill's layer table"
        )
    return found
