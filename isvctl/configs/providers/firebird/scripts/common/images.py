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

"""Custom OS image upload and catalog helpers.

Images are uploaded through the API's multipart upload routes:
  1. ``POST /compute/images/{scope}/uploads`` declares the file and returns an
     ``uploadId`` and the part size (5 MiB minimum, all parts but the last)
  2. ``PUT /compute/images/uploads/{id}/parts/{n}`` carries each part as raw bytes
  3. ``POST /compute/images/uploads/{id}/complete`` checks the parts, inspects the
     assembled file (qcow2, or raw with an MBR/GPT partition table - anything
     else is refused), and registers the image; it returns ``imageId`` and
     ``objectId`` (the stored file's reference)
  4. ``GET /compute/images/uploads/{id}`` reports the session state
An unfinished session is aborted (``DELETE /compute/images/uploads/{id}``) on failure.

When those routes are unavailable the step emits a structured skip;
``is_not_registered`` covers a 404 or 501 response.
"""

import hashlib
import time
from typing import Any, BinaryIO
from urllib.parse import quote

from common.firebird_client import FirebirdApiError, FirebirdClient, log

UPLOADS_PATH = "/compute/images/uploads"
NOT_ENABLED_REASON = "The image upload API is not enabled on this Firebird API"
# Formats the API can hand to bare-metal provisioning (it inspects the file at finish).
BM_FORMATS = ("qcow2", "raw")
TERMINAL_FAILURES = ("ABORTED", "FAILED")


def images_path(scope_id: str, image_id: str = "") -> str:
    """Return the image collection path of a scope, or one image's path."""
    base = f"/compute/images/{quote(scope_id)}"
    return f"{base}/{quote(image_id)}" if image_id else base


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    """Read ``size`` bytes (fewer only at end of stream)."""
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def upload(
    client: FirebirdClient,
    scope_id: str,
    stream: BinaryIO,
    size: int,
    *,
    name: str,
    filename: str,
    timeout: int,
    record: dict[str, Any],
) -> dict[str, Any]:
    """Upload ``size`` bytes from ``stream`` as image ``name``; return the finish response.

    ``record["upload_id"]`` is set as soon as the session exists, and
    ``record["image_id"]`` as soon as finish registers the image. Any failure
    before finish aborts the session, then re-raises. Raises
    FirebirdApiError from the start call unchanged, so callers can detect an
    unregistered service.
    """
    body = {"filename": filename, "contentType": "application/octet-stream", "sizeBytes": size, "name": name}
    session = client.request("POST", f"{images_path(scope_id)}/uploads", body)
    upload_id = session.get("uploadId") or ""
    if not upload_id:
        raise RuntimeError("upload start returned no uploadId")
    record["upload_id"] = upload_id
    part_size = int(session.get("partSizeBytes") or 0)
    finished = False
    try:
        if part_size <= 0:
            raise RuntimeError(f"upload {upload_id} has no part size")
        digest = hashlib.sha256()
        sent, part = 0, 0
        while sent < size:
            data = _read_exact(stream, min(part_size, size - sent))
            if not data:
                raise RuntimeError(f"source ended after {sent} of {size} bytes")
            part += 1
            client.request("PUT", f"{UPLOADS_PATH}/{quote(upload_id)}/parts/{part}", data)
            digest.update(data)
            sent += len(data)
        log(f"  uploaded {part} part(s), {sent} bytes")
        finish = client.request(
            "POST",
            f"{UPLOADS_PATH}/{quote(upload_id)}/complete",
            {"checksum": digest.hexdigest(), "checksumAlgorithm": "sha256"},
        )
        finished = True
        # Recorded before waiting, so a failed wait still leaves the image to clean up.
        record["image_id"] = finish.get("imageId") or ""
        wait_complete(client, upload_id, timeout)
        return finish
    finally:
        if not finished:
            abort(client, upload_id)


def wait_complete(client: FirebirdClient, upload_id: str, timeout: int) -> dict[str, Any]:
    """Poll the upload session until it is COMPLETE; raise if it ends otherwise or times out."""
    deadline = time.monotonic() + timeout
    while True:
        status = client.request("GET", f"{UPLOADS_PATH}/{quote(upload_id)}")
        state = status.get("status", "")
        if status.get("finished") or state == "COMPLETE":
            return status
        if state in TERMINAL_FAILURES or status.get("aborted") or status.get("expired"):
            raise RuntimeError(f"upload {upload_id} ended {state}{' (expired)' if status.get('expired') else ''}")
        if time.monotonic() > deadline:
            raise RuntimeError(f"upload {upload_id} still {state} after {timeout}s")
        time.sleep(5)


def abort(client: FirebirdClient, upload_id: str) -> None:
    """Abort an unfinished upload session, best effort (failures are logged, not raised)."""
    try:
        client.request("DELETE", f"{UPLOADS_PATH}/{quote(upload_id)}")
        log(f"  aborted upload {upload_id}")
    except Exception as e:
        log(f"  could not abort upload {upload_id}: {e}")


def resolve_image_id(client: FirebirdClient, scope_id: str, finish: dict[str, Any], name: str) -> str:
    """Return the uploaded image's ID: from the finish response, else by name in the scope.

    A retried finish of an already-registered upload returns no ``imageId``.
    """
    image_id = finish.get("imageId") or ""
    if image_id:
        return image_id
    for image in client.paginate(images_path(scope_id), "items"):
        if image.get("name") == name:
            return image.get("id", "")
    raise RuntimeError(f"upload finished but image {name!r} is not in {scope_id}")


def delete_image(client: FirebirdClient, scope_id: str, image_id: str, timeout: int) -> bool:
    """Delete an image and wait for its Operation; return False if it was already gone.

    The API refuses (400) to delete an image a BM still references.
    """
    try:
        operation = client.request("DELETE", images_path(scope_id, image_id)).get("operation") or {}
    except FirebirdApiError as e:
        if e.status == 404:
            return False
        raise
    if operation:
        client.wait_operation(operation, timeout)
    return True
