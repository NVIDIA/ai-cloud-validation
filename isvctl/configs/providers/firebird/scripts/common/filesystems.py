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

"""Filesystem (shared storage) helpers for the Firebird storage scripts and shim.

The Filesystem API (``/projects/{p}/storage/fs``) provisions filesystems in the
tenant's organization on the parallel filesystem. Create, resize, and delete
return an Operation; the create Operation completes once the filesystem is READY.

Stdlib only and free of ``common.*`` imports: the StorageProvider shim runs in
the validation process, where ``common`` may be another provider's package, so it
loads this file (and ``firebird_client.py``) by path. Every function takes a
``FirebirdClient``; API errors are recognized by their ``status`` attribute.

Safety rules enforced here:

* Only filesystems named ``isv-fs-...`` are ever created, resized, or deleted:
  ``submit_create`` checks the requested name, and ``submit_resize`` and
  ``delete_filesystem`` check the filesystem's name (``delete_filesystem`` reads
  it first), so the tenant's own filesystems cannot be touched even by a wrong
  ID.
* ``GET .../storage/fs/{id}`` also returns ``authCredentialsBase64`` (a client login
  token for mounting). ``get_filesystem`` and ``mount_endpoint`` drop it. Only
  ``mount_credentials`` returns it, for the scripts that mount a filesystem on a
  BM, which hand it to the BM over SSH stdin and never print, log, or decode it
  locally.
"""

import re
import secrets
import sys
import time
from typing import Any
from urllib.parse import quote

FS_PREFIX = "isv-fs-"
# A filesystem any run of this provider creates: the prefix, the run ID (6 hex from
# the setup step; 12 when StorageProviderApiCheck mints its own), and a role.
RUN_NAME = re.compile(r"^isv-fs-[0-9a-f]{6,12}-[A-Za-z0-9_-]+$")
NAME_MAX = 32  # the API's filesystem-name length limit
_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")
GIB_BYTES = 1 << 30
GIB_PER_TIB = 1024
POLL_SECONDS = 5
CONFLICT_RETRY_SECONDS = 10


class RefusedDeleteError(RuntimeError):
    """Raised instead of deleting a filesystem this provider did not create."""


class RefusedResizeError(RuntimeError):
    """Raised instead of resizing a filesystem this provider did not create."""


def _log(message: str) -> None:
    """Print progress to stderr (stdout is reserved for the JSON result)."""
    print(message, file=sys.stderr, flush=True)


def _status(error: Exception) -> int:
    """Return the HTTP status of an API error (0 when it has none)."""
    return int(getattr(error, "status", 0) or 0)


# ── Names ─────────────────────────────────────────────────────────────


def new_run_id() -> str:
    """Return a fresh 6-hex run ID; every filesystem a run creates carries it."""
    return secrets.token_hex(3)


def run_prefix(run_id: str) -> str:
    """Return the name prefix of the run's filesystems (``isv-fs-<run_id>-``)."""
    if not re.fullmatch(r"[0-9a-f]{6}", run_id or ""):
        raise ValueError(f"run ID must be 6 lowercase hex digits, got {run_id!r}")
    return f"{FS_PREFIX}{run_id}-"


def fs_name(run_id: str, role: str) -> str:
    """Return the name of the run's filesystem for ``role`` (for example ``hss01``)."""
    name = run_prefix(run_id) + _NAME_CHARS.sub("-", role)
    return name[:NAME_MAX]


def is_owned(name: str) -> bool:
    """Return whether ``name`` is a filesystem name this provider creates (and may delete)."""
    return name.startswith(FS_PREFIX)


# ── Reads ─────────────────────────────────────────────────────────────


def fs_path(client: Any, fs_id: str = "") -> str:
    """Return the project filesystem collection path, or one filesystem's path."""
    base = client.project_path("/storage/fs")
    return f"{base}/{quote(fs_id)}" if fs_id else base


def capacity_gib(fs: dict[str, Any]) -> int | None:
    """Return a filesystem's capacity in GiB, or None when the readback carries none.

    ``capacity`` is ``{size, sizeUnit}`` with ``GiB`` or ``TiB`` (the enum name,
    or its number 1/2); the deprecated ``totalCapacityGb`` (an int64, so a JSON
    string) is the fallback.
    """
    capacity = fs.get("capacity") or {}
    size = capacity.get("size")
    if isinstance(size, int | str) and str(size).isdigit() and int(size) > 0:
        unit = capacity.get("sizeUnit")
        if unit in ("TiB", 2):
            return int(size) * GIB_PER_TIB
        if unit in ("GiB", 1):
            return int(size)
    legacy = fs.get("totalCapacityGb")
    if isinstance(legacy, int | str) and str(legacy).isdigit():
        return int(legacy)
    return None


def list_filesystems(client: Any) -> list[dict[str, Any]]:
    """Return every filesystem in the project."""
    return client.paginate(fs_path(client), "items")


def get_filesystem(client: Any, fs_id: str) -> dict[str, Any] | None:
    """Return the filesystem resource, or None when it does not exist (404).

    Only the ``filesystem`` object is returned: the response's
    ``authCredentialsBase64`` is dropped here and never reaches a caller.
    """
    try:
        response = client.request("GET", fs_path(client, fs_id))
    except Exception as e:
        if _status(e) == 404:
            return None
        raise
    return dict(response.get("filesystem") or {})


def _mount_response(client: Any, fs_id: str) -> dict[str, Any]:
    """Return the per-filesystem GET response; raise unless it names a storage endpoint."""
    response = client.request("GET", fs_path(client, fs_id))
    if not str(response.get("storageEndpoint") or "").strip():
        raise RuntimeError(f"GET filesystem {fs_id} returned no storageEndpoint")
    return response


def mount_endpoint(client: Any, fs_id: str) -> tuple[dict[str, Any], str]:
    """Return ``(filesystem, storageEndpoint)``; the credential is dropped here."""
    response = _mount_response(client, fs_id)
    return dict(response.get("filesystem") or {}), str(response["storageEndpoint"]).strip()


def mount_credentials(client: Any, fs_id: str) -> tuple[dict[str, Any], str, str]:
    """Return ``(filesystem, storageEndpoint, authCredentialsBase64)`` for mounting ``fs_id``.

    The caller holds the credential in memory only and passes it to the BM over
    SSH stdin (``common.wekafs.deliver_token``); it must never reach argv, a log,
    a file on this host, or the step's JSON.
    """
    response = _mount_response(client, fs_id)
    blob = str(response.get("authCredentialsBase64") or "").strip()
    if not blob:
        raise RuntimeError(f"GET filesystem {fs_id} returned no mount credential")
    return dict(response.get("filesystem") or {}), str(response["storageEndpoint"]).strip(), blob


def storage_status(client: Any) -> tuple[int, int]:
    """Return ``(total_quota_gib, allocated_gib)`` of the tenant's storage organization.

    ``GET .../storage/fs/status`` reports the organization's quota and its
    allocation across all filesystems, so the allocation shows what it
    actually provisioned. The API labels both GB; they are converted from
    bytes in GiB.
    """
    status = client.request("GET", fs_path(client) + "/status")
    return int(status.get("totalQuotaGb") or 0), int(status.get("allocatedGb") or 0)


# ── Mutations ─────────────────────────────────────────────────────────


def _operation(response: dict[str, Any], what: str) -> dict[str, Any]:
    """Return the Operation of a mutation response; raise if it names no resource."""
    operation = response.get("operation") or {}
    if not operation.get("resourceId"):
        raise RuntimeError(f"{what} returned no operation resource ID")
    return operation


def submit_create(client: Any, name: str, size_gib: int) -> dict[str, Any]:
    """Create a filesystem of ``size_gib`` GiB; return its (unawaited) Operation."""
    if not is_owned(name):
        raise ValueError(f"refusing to create {name!r}: provider filesystems are named {FS_PREFIX}*")
    body = {"name": name, "capacity": {"size": size_gib, "sizeUnit": "GiB"}}
    _log(f"  creating filesystem {name} ({size_gib} GiB)")
    return _operation(client.request("POST", fs_path(client), body), f"create filesystem {name}")


def submit_resize(client: Any, fs: dict[str, Any], size_gib: int) -> dict[str, Any]:
    """Resize ``fs`` to ``size_gib`` GiB; return the (unawaited) Operation.

    Renaming is not supported, so the body carries the current name. Raises
    ``RefusedResizeError`` for a filesystem whose name is not ``isv-fs-...``.
    """
    name = str(fs.get("name", ""))
    if not is_owned(name):
        raise RefusedResizeError(f"refusing to resize filesystem {fs.get('id')} ({name!r}): not named {FS_PREFIX}*")
    body = {"name": name, "capacity": {"size": size_gib, "sizeUnit": "GiB"}}
    _log(f"  resizing filesystem {fs.get('id')} to {size_gib} GiB")
    return _operation(client.request("PUT", fs_path(client, fs.get("id", "")), body), f"resize {fs.get('id')}")


def wait_ready(client: Any, fs_id: str, timeout: int) -> dict[str, Any]:
    """Poll the filesystem until READY and return it; raise on ERROR, disappearance, or timeout."""
    deadline = time.monotonic() + timeout
    while True:
        fs = get_filesystem(client, fs_id)
        if fs is None:
            raise RuntimeError(f"filesystem {fs_id} disappeared while waiting for READY")
        state = fs.get("state")
        if state == "READY":
            return fs
        if state == "ERROR":
            raise RuntimeError(f"filesystem {fs_id} is in ERROR")
        if time.monotonic() >= deadline:
            raise RuntimeError(f"filesystem {fs_id} is {state} after {timeout}s, expected READY")
        time.sleep(POLL_SECONDS)


def create_ready(client: Any, name: str, size_gib: int, timeout: int, created: list[str]) -> dict[str, Any]:
    """Create a filesystem, wait for its Operation and a READY readback, and return it.

    The new ID is appended to ``created`` (and logged) as soon as the API
    accepts the request, so the caller deletes it even when a later wait fails.
    """
    deadline = time.monotonic() + timeout
    operation = submit_create(client, name, size_gib)
    fs_id = operation["resourceId"]
    created.append(fs_id)
    _log(f"  filesystem {name} is {fs_id}")
    client.wait_operation(operation, max(1, int(deadline - time.monotonic())))
    return wait_ready(client, fs_id, max(1, int(deadline - time.monotonic())))


def delete_filesystem(client: Any, fs_id: str, timeout: int) -> bool:
    """Delete one of this provider's filesystems and wait until it is gone.

    Returns False when it was already gone. Raises ``RefusedDeleteError`` for a
    filesystem whose name is not ``isv-fs-...``. A 409 (another Operation still
    acting on it, for example a create that has not finished) is retried until
    ``timeout``.
    """
    deadline = time.monotonic() + timeout
    fs = get_filesystem(client, fs_id)
    if fs is None:
        return False
    name = str(fs.get("name", ""))
    if not is_owned(name):
        raise RefusedDeleteError(f"refusing to delete filesystem {fs_id} ({name!r}): not named {FS_PREFIX}*")
    _log(f"  deleting filesystem {fs_id} ({name})")
    while True:
        try:
            response = client.request("DELETE", fs_path(client, fs_id))
            break
        except Exception as e:
            if _status(e) == 404:
                return False
            if _status(e) != 409 or time.monotonic() >= deadline:
                raise
            _log(f"  filesystem {fs_id} is busy, retrying the delete")
            time.sleep(CONFLICT_RETRY_SECONDS)
    operation = response.get("operation") or {}
    if operation.get("id"):
        client.wait_operation(operation, max(1, int(deadline - time.monotonic())))
    while get_filesystem(client, fs_id) is not None:
        if time.monotonic() >= deadline:
            raise RuntimeError(f"filesystem {fs_id} is still readable {timeout}s after its delete")
        time.sleep(POLL_SECONDS)
    return True


def delete_all(client: Any, fs_ids: list[str], timeout: int) -> list[str]:
    """Delete each filesystem in ``fs_ids`` within ``timeout`` seconds in total.

    All deletes share one deadline, so a caller's cleanup never outlasts the
    budget it passed, however many filesystems there are. A delete that starts
    after the deadline still attempts its DELETE once but waits only a moment.
    Returns one error string per failure.
    """
    deadline = time.monotonic() + timeout
    errors = []
    for fs_id in fs_ids:
        try:
            delete_filesystem(client, fs_id, max(1, int(deadline - time.monotonic())))
        except Exception as e:
            errors.append(f"filesystem:{fs_id}: {e}")
            _log(f"  could not delete filesystem {fs_id}: {e}")
    return errors
