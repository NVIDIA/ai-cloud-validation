#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Report the Kubernetes versions this platform offers (K8S02-01) - my-isv template.

TODO: replace the stub below with a call to whatever your platform uses to
enumerate the Kubernetes versions a tenant can create or upgrade a cluster to.

Report the catalogue, not the cluster under test: K8sSupportedMinorVersionsCheck
compares this list against the three most recent upstream minors, so a list
covering only the version that happens to be running reads as a provider that
offers one minor. Versions may be reported as ``X.Y`` or ``X.Y.Z``, with or
without a leading ``v``.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow importing provider-local helpers from scripts/common/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.stub import emit_stub


def main() -> int:
    """Emit the offered Kubernetes versions template result (K8S02-01)."""
    return emit_stub(
        "list_k8s_versions",
        hint="Kubernetes version catalogue API",
        offered_versions=["1.34.1", "1.33.5", "1.32.9"],
    )


if __name__ == "__main__":
    sys.exit(main())
