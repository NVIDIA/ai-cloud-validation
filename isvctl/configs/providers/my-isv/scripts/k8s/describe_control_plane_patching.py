#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Report how this platform patches the control plane (K8S02-02) - my-isv template.

TODO: replace the stub below with a call to whatever your platform uses to
describe the cluster's patching configuration.

``automated_patching_enabled`` is the claim under test, so report it from the
cluster's actual configuration rather than from your product policy - a run
that hard-codes ``true`` certifies nothing. ``current_version`` is the patch
version the control plane is on as your platform sees it;
K8sAutomatedControlPlanePatchingCheck matches it against the version the API
server reports and against the newest patch upstream publishes for that minor,
so the claim has to be corroborated by a control plane that is actually current.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow importing provider-local helpers from scripts/common/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.stub import emit_stub


def main() -> int:
    """Emit the control-plane patching template result (K8S02-02)."""
    return emit_stub(
        "describe_control_plane_patching",
        hint="control-plane patching configuration API",
        automated_patching_enabled=True,
        current_version="1.34.1",
    )


if __name__ == "__main__":
    sys.exit(main())
