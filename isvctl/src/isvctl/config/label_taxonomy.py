# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Canonical label taxonomy for matrix-style suite discovery."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LabelMetadata:
    """Human-facing metadata for a validation label."""

    kind: str
    display_name: str


CAPABILITY_LABELS: dict[str, str] = {
    "native_cloud": "Native AI Cloud",
    "vm": "VMaaS",
    "bare_metal": "BMaaS",
    "slurm": "SLURM",
    "kubernetes": "K8s",
    "control_plane": "Native AI Cloud",
    "image_registry": "Image Registry",
}

REQUIREMENT_LABELS: dict[str, str] = {
    "iam": "IAM",
    "security": "Security",
    "network": "Networking",
    "observability": "Observability",
    "sanitization": "Sanitization",
}

TRAIT_LABELS: dict[str, str] = {
    "min_req": "Minimum Requirement",
    "gpu": "GPU",
    "ssh": "SSH",
    "workload": "Workload",
    "slow": "Slow",
    "attestation": "Attestation",
    "capacity": "Capacity",
    "disk": "Disk",
    "dpu": "DPU",
    "firmware": "Firmware",
    "governance": "Governance",
    "health": "Health",
    "infiniband": "InfiniBand",
    "ingestion": "Ingestion",
    "l2": "Layer 2",
}

_KIND_ORDER = {
    "capability": 0,
    "requirement": 1,
    "trait": 2,
    "uncategorized": 3,
}

_LABEL_ORDER = {
    label: index
    for index, label in enumerate(
        [
            "native_cloud",
            "control_plane",
            "vm",
            "bare_metal",
            "slurm",
            "kubernetes",
            "image_registry",
            "iam",
            "security",
            "network",
            "observability",
            "sanitization",
        ]
    )
}


def label_metadata(label: str) -> LabelMetadata:
    """Return taxonomy metadata for ``label``."""
    if label in CAPABILITY_LABELS:
        return LabelMetadata(kind="capability", display_name=CAPABILITY_LABELS[label])
    if label in REQUIREMENT_LABELS:
        return LabelMetadata(kind="requirement", display_name=REQUIREMENT_LABELS[label])
    if label in TRAIT_LABELS:
        return LabelMetadata(kind="trait", display_name=TRAIT_LABELS[label])
    return LabelMetadata(kind="uncategorized", display_name=label.replace("_", " ").title())


def label_sort_key(label: str) -> tuple[int, str, str]:
    """Sort labels by matrix taxonomy, then display name, then raw label."""
    metadata = label_metadata(label)
    order = _LABEL_ORDER.get(label, len(_LABEL_ORDER))
    return (_KIND_ORDER[metadata.kind], f"{order:04d}:{metadata.display_name.casefold()}", label)
