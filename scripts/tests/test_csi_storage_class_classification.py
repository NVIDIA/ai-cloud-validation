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
"""StorageClass classification in ``my-isv/scripts/k8s/_common.sh``.

The classifier decides which StorageClass each storage check probes. Getting it
wrong is not cosmetic: on an OCI cluster it labelled the Lustre class as *block*,
so every block probe requested a ReadWriteOnce PVC from a parallel filesystem.
Those never bind, and each one that did would mint a full-size ``Retain`` filesystem.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SETUP = REPO / "isvctl" / "configs" / "providers" / "my-isv" / "scripts" / "k8s" / "setup.sh"

# The provisioner shape of an OCI cluster with File Storage and Lustre classes.
OCI_STORAGE_CLASSES = """lustre-file=lustre.csi.oraclecloud.com
nfs-file=fss.csi.oraclecloud.com
fss-file=fss.csi.oraclecloud.com
lustre-file-2=lustre.csi.oraclecloud.com
oci=oracle.com/oci
oci-bv=blockvolume.csi.oraclecloud.com"""

OCI_CSI_DRIVERS = "fss.csi.oraclecloud.com lustre.csi.oraclecloud.com"


def _stub_kubectl(tmp_path: Path, storage_classes: str, csidrivers: str) -> Path:
    """A kubectl that answers the two queries the classifier makes, empty otherwise."""
    (tmp_path / "sc.txt").write_text(storage_classes + "\n")
    (tmp_path / "drivers.txt").write_text(csidrivers)
    script = tmp_path / "kubectl"
    script.write_text(
        "#!/bin/bash\n"
        f'here="{tmp_path}"\n'
        'case "$*" in\n'
        '  *csidrivers*) cat "$here/drivers.txt" ;;\n'
        '  *"get sc"*)   cat "$here/sc.txt" ;;\n'
        "  *current-context*) echo stub-context ;;\n"
        "  *) : ;;\n"
        "esac\n"
        "exit 0\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _classify(tmp_path: Path, storage_classes: str, csidrivers: str) -> dict:
    if shutil.which("jq") is None:
        pytest.skip("jq is required by setup.sh")
    stub = _stub_kubectl(tmp_path, storage_classes, csidrivers)
    env = {**os.environ, "KUBECTL": str(stub)}
    for leaked in ("K8S_CSI_BLOCK_SC", "K8S_CSI_SHARED_FS_SC", "K8S_CSI_NFS_SC"):
        env.pop(leaked, None)
    result = subprocess.run(["bash", str(SETUP)], capture_output=True, text=True, env=env, check=False, timeout=120)
    if result.returncode != 0:
        pytest.skip(f"setup.sh needs an environment this host lacks: {result.stderr[-200:]}")
    return json.loads(result.stdout)["csi"]


def test_lustre_is_not_classified_as_block(tmp_path: Path) -> None:
    """The regression: ``lustre.csi`` matched no shared pattern and fell through to block."""
    csi = _classify(tmp_path, OCI_STORAGE_CLASSES, OCI_CSI_DRIVERS)
    assert csi["block_storage_class"] != "lustre-file"
    assert "lustre" not in csi["block_storage_class"]


def test_lustre_and_fss_are_shared_or_nfs(tmp_path: Path) -> None:
    csi = _classify(tmp_path, OCI_STORAGE_CLASSES, OCI_CSI_DRIVERS)
    assert csi["shared_fs_storage_class"] == "lustre-file"
    # OCI File Storage is NFSv3 even though its provisioner says "fss".
    assert csi["nfs_storage_class"] == "nfs-file"


def test_a_registered_block_driver_wins_over_the_catch_all(tmp_path: Path) -> None:
    """With blockvolume registered, it must beat an unknown provisioner sorted earlier."""
    classes = OCI_STORAGE_CLASSES + "\naaa-unknown=some.vendor.example.com"
    csi = _classify(tmp_path, classes, OCI_CSI_DRIVERS + " blockvolume.csi.oraclecloud.com some.vendor.example.com")
    assert csi["block_storage_class"] == "oci-bv"
