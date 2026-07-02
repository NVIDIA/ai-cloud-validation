#!/usr/bin/env python3
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

"""Download a VM disk image, stage it in Cloud Storage, and register it as a
Compute Engine machine image.

Translates the AWS reference's single ``ec2.import_image`` call onto GCP's two
documented import paths:

  * **RAW source** (``--image-format raw``): a ``disk.raw`` packaged as a
    ``.tar.gz`` in Cloud Storage registers directly via
    ``ImagesClient.insert`` with ``raw_disk.source`` set to the GCS object URL
    (pure SDK; no Cloud Build).
  * **Foreign-format source** (vmdk / vhd / vhdx / qcow2): converted + registered
    via the documented virtual-disk import workflow ``gcloud compute images
    import`` (Cloud Build-backed). Multi-step but fully supported
    (https://cloud.google.com/compute/docs/import/importing-virtual-disks).

Both paths emit the same provider-neutral contract so downstream steps and the
suite validators are import-path agnostic. The bucket and image names are
suffixed with a unique per-run id so parallel runs do not collide on Compute
Engine's name-as-id namespace and teardown owns only its own resources.

Required JSON output (suite ``image_upload`` group):
    {
        "success":        bool,       # import reached a terminal success state
        "platform":       "image_registry",
        "image_id":       str,        # registered Compute Engine image name (Image.name)
        "storage_bucket": str,        # Cloud Storage bucket holding the staged disk object
        "disk_ids":       list[str],  # GCS disk-object identifiers (forwarded to teardown --disk-ids)
        "error":          str,        # (optional) present when success is false
    }

Usage:
    python upload_image.py --image-url <url> --image-format vmdk --region <region>

AWS reference implementation:
    ../../aws/scripts/image-registry/upload_image.py
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

import requests
from common.compute import (
    narrow_region_to_zone,
    resolve_project,
    unique_suffix,
    wait_for_global_op,
)
from common.errors import classify_gcp_error, handle_gcp_errors
from google.api_core import exceptions as gax
from google.cloud import compute_v1, storage

# Formats Compute Engine can register directly from a RAW disk object. Anything
# else is a foreign virtual-disk format that must go through the Cloud
# Build-backed `gcloud compute images import` translation workflow.
_RAW_FORMATS = {"raw", "tar.gz", "targz"}
# Foreign virtual-disk formats accepted by `gcloud compute images import`.
_FOREIGN_FORMATS = {"vmdk", "vhd", "vhdx", "qcow2", "qcow", "vdi"}

# Bound the synchronous image-register global op wait (RAW path). The default
# step timeout sits well above this so the cap is headroom, not a sum.
_IMAGE_REGISTER_WAIT_S = 1800
# `gcloud compute images import` drives Cloud Build (download + convert +
# register); 40 min covers the observed worst case for a server cloud image.
_GCLOUD_IMPORT_TIMEOUT_S = 2400
# requests streaming download budget for a ~1 GB disk image.
_DOWNLOAD_TIMEOUT_S = 1200


def _object_key_from_url(url: str, image_format: str) -> str:
    """Derive a stable Cloud Storage object key from the source URL."""
    tail = url.rstrip("/").rsplit("/", 1)[-1] or f"disk.{image_format}"
    # Strip query strings / fragments so the object key is a clean filename.
    return tail.split("?", 1)[0].split("#", 1)[0]


def download_image(url: str, image_format: str) -> Path | None:
    """Stream-download the source disk image to a temp file.

    Mirrors the AWS oracle's chunked download with progress logging on stderr.
    Returns the local path, or ``None`` on a request error (the caller surfaces
    a structured failure).
    """
    print(f"Downloading image from {url}...", file=sys.stderr)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=f".{image_format}", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            response = requests.get(url, stream=True, timeout=_DOWNLOAD_TIMEOUT_S)
            response.raise_for_status()
            total = int(response.headers.get("content-length", 0))
            done = 0
            last = 0
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                tmp.write(chunk)
                done += len(chunk)
                pct = int(done * 100 / total) if total else 0
                if pct >= last + 10:
                    print(f"  download progress: {pct}%", file=sys.stderr)
                    last = pct
        return tmp_path
    except requests.RequestException as e:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        print(f"Download failed: {e}", file=sys.stderr)
        return None


def ensure_bucket(storage_client: storage.Client, bucket_name: str, region: str) -> storage.Bucket:
    """Create (or adopt) a regional Cloud Storage bucket for the staged disk.

    A run-id-suffixed name makes a same-run rerun adopt its own bucket
    (``Conflict`` -> ``get_bucket``) rather than fail, while a name owned by a
    different project surfaces honestly.
    """
    try:
        bucket = storage_client.create_bucket(bucket_name, location=region)
        print(f"Created bucket gs://{bucket_name} ({region})", file=sys.stderr)
        return bucket
    except gax.Conflict:
        print(f"Bucket gs://{bucket_name} already exists; adopting", file=sys.stderr)
        return storage_client.get_bucket(bucket_name)
    except Exception as e:
        # google-cloud-storage raises google.cloud.exceptions.Conflict (409);
        # fall back to a name-based adopt rather than fail a same-run rerun.
        if "409" in str(e) or "conflict" in str(e).lower():
            print(f"Bucket gs://{bucket_name} already exists; adopting", file=sys.stderr)
            return storage_client.get_bucket(bucket_name)
        raise


def upload_object(bucket: storage.Bucket, local_path: Path, object_key: str) -> str:
    """Upload the local disk file to ``object_key`` and return the GCS URL."""
    print(f"Uploading {local_path} to gs://{bucket.name}/{object_key}...", file=sys.stderr)
    blob = bucket.blob(object_key)
    blob.upload_from_filename(str(local_path))
    print("Upload complete", file=sys.stderr)
    return f"https://storage.googleapis.com/{bucket.name}/{object_key}"


def register_raw_image(
    project: str,
    image_name: str,
    raw_source_url: str,
    on_accepted: Callable[[], None] | None = None,
) -> None:
    """Register a Compute Engine image directly from a RAW disk object (SDK path).

    ``on_accepted`` (if given) fires the instant ``ImagesClient.insert`` returns
    an accepted operation -- i.e. this step now owns the image name -- BEFORE the
    register global-op wait, so a wait-side timeout still leaves the caller with
    confirmed ownership for cleanup. A synchronous AlreadyExists/Conflict raised
    by ``insert`` propagates BEFORE ``on_accepted`` fires, so the caller never
    claims ownership of a pre-existing image it did not create.
    """
    print(f"Registering image {image_name} from RAW source {raw_source_url}...", file=sys.stderr)
    image = compute_v1.Image()
    image.name = image_name
    image.description = "ISV image-registry validation imported image (createdby=isvtest)"
    image.raw_disk = compute_v1.RawDisk(source=raw_source_url)
    op = compute_v1.ImagesClient().insert(project=project, image_resource=image)
    if on_accepted is not None:
        on_accepted()
    op_name = getattr(op, "name", "")
    if op_name:
        wait_for_global_op(project, op_name, timeout=_IMAGE_REGISTER_WAIT_S)


def import_foreign_image(
    project: str,
    zone: str,
    image_name: str,
    gcs_source: str,
    guest_os: str,
    on_accepted: Callable[[], None] | None = None,
) -> None:
    """Convert + register a foreign-format disk via `gcloud compute images import`.

    The documented Cloud Build-backed workflow is the supported path for
    VMDK/VHD/qcow2 sources; there is no direct SDK insert for foreign formats.
    Raises ``RuntimeError`` on a non-zero exit so the caller records a
    structured failure.

    ``on_accepted`` (if given) fires only AFTER the import subprocess exits 0.
    The Cloud Build workflow registers the image at the END of a clean run, so a
    clean exit is the first point this step can prove it owns the image; a
    synchronous AlreadyExists/Conflict (or any mid-build failure) exits non-zero
    BEFORE ``on_accepted`` fires, so the caller never claims a pre-existing image.
    """
    # `gcloud compute images import` is deprecated (Cloud SDK now gates it behind
    # the explicit `--cmd-deprecated` acknowledgment flag; without it the CLI
    # exits rc=2 with "argument --cmd-deprecated: Must be specified" before doing
    # any work). The command still performs the full Cloud Build-backed
    # download/convert/register workflow this step relies on, so we pass the
    # acknowledgment flag rather than fail. The long-term successor is
    # `gcloud compute migration image-imports` (Migrate to Virtual Machines API);
    # switching to it is a follow-up, not required for the import to succeed here.
    cmd = [
        "gcloud",
        "compute",
        "images",
        "import",
        image_name,
        "--cmd-deprecated",
        f"--source-file={gcs_source}",
        f"--os={guest_os}",
        f"--project={project}",
        f"--zone={zone}",
        "--quiet",
    ]
    print(f"Importing foreign-format image: {' '.join(cmd)}", file=sys.stderr)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_GCLOUD_IMPORT_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            "gcloud CLI not found on PATH; the foreign-format (vmdk/vhd/qcow2) import "
            "path requires the Google Cloud CLI + Cloud Build API. Install gcloud or "
            "supply a RAW source (--image-format raw)."
        ) from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"gcloud compute images import timed out after {_GCLOUD_IMPORT_TIMEOUT_S}s") from e
    if proc.stdout:
        print(proc.stdout, file=sys.stderr)
    if proc.returncode != 0:
        raise RuntimeError(f"gcloud compute images import failed (rc={proc.returncode}): {proc.stderr.strip()}")
    # Clean exit: the Cloud Build workflow has registered the image, so this step
    # now owns it. Claim ownership only here, never before the import runs.
    if on_accepted is not None:
        on_accepted()


def _gcs_source_from_url(raw_source_url: str) -> str:
    """Translate the public-style https URL into the gs:// form gcloud expects."""
    prefix = "https://storage.googleapis.com/"
    if raw_source_url.startswith(prefix):
        return "gs://" + raw_source_url[len(prefix) :]
    return raw_source_url


@handle_gcp_errors
def main() -> int:
    """Upload a disk image and register it as a Compute Engine machine image."""
    parser = argparse.ArgumentParser(description="Import a VM disk image as a Compute Engine image")
    parser.add_argument(
        "--image-url",
        # Ubuntu 22.04 LTS (jammy) VMDK: matches the --guest-os ubuntu-2204
        # default below (gcloud `--os` has no ubuntu-2404 choice as of SDK 564).
        default="https://cloud-images.ubuntu.com/releases/jammy/release/ubuntu-22.04-server-cloudimg-amd64.vmdk",
        help="URL to download the source disk image from",
    )
    parser.add_argument("--image-format", default="vmdk", help="Disk format (raw, vmdk, vhd, qcow2)")
    parser.add_argument("--region", required=True, help="GCP region (bucket location + zone derivation)")
    parser.add_argument("--zone", default=None, help="GCP zone for the import worker (overrides region)")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    parser.add_argument("--bucket-name", default=None, help="Override the staged-disk bucket name")
    parser.add_argument("--image-name", default=None, help="Override the registered image name")
    # `gcloud compute images import --os` accepts a fixed enum of translation
    # targets (run `gcloud compute images import --help`). As of Cloud SDK 564
    # the newest Ubuntu choice is `ubuntu-2204` — there is NO `ubuntu-2404`
    # choice, so this value MUST match a value gcloud actually accepts AND the
    # source disk's Ubuntu version. The provider config threads it as a
    # `guest_os` setting coupled to `image_url` (both pin Ubuntu 22.04 jammy),
    # so the disk and the translation hint stay in sync and an operator
    # overriding the image also sets the matching --os value. The default here
    # mirrors that coupling for a standalone invocation (jammy VMDK image-url
    # default above). A value outside the enum fails the import with
    # "argument --os: Invalid choice".
    parser.add_argument("--guest-os", default="ubuntu-2204", help="gcloud --os value for foreign-format translation")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "image_registry",
        "image_id": "",
        "storage_bucket": "",
        "disk_ids": [],
        "region": args.region,
    }

    project = resolve_project(args.project)
    # Bucket location is regional; the gcloud import worker is zonal.
    region = args.region.rsplit("-", 1)[0] if _looks_like_zone(args.region) else args.region
    zone = args.zone or narrow_region_to_zone(args.region)

    image_format = args.image_format.strip().lower()
    image_name = args.image_name or unique_suffix("isv-ir-image")
    # Cloud Storage holds a bucket name during its (soft-)delete retention
    # window after the bucket is removed, so a run-id-only name cannot be
    # recreated on a back-to-back rerun: the new create races the prior run's
    # teardown delete and a freshly created/adopted handle then 404s
    # ("bucket does not exist") on upload. Add a short per-invocation token so
    # every attempt provisions its OWN bucket and never collides with a
    # just-deleted name -- but fold that token INTO the base passed to
    # unique_suffix (before the run id), not after it, so the final name still
    # ends in "-<RUN_ID>". Run-id-scoped orphan sweeps match only artifacts
    # whose name ends with the run id, so a trailing random token would hide
    # this bucket from the scoped cleanup tool. Teardown still owns exactly this
    # bucket via the emitted storage_bucket output (it forwards the value, not a
    # reconstructed name), so cleanup provenance is unchanged.
    bucket_name = args.bucket_name or unique_suffix(f"isv-ir-import-{uuid.uuid4().hex[:6]}")
    object_key = _object_key_from_url(args.image_url, image_format)

    storage_client = storage.Client(project=project)

    # Stamp the bucket onto the result immediately so a failure mid-import
    # still forwards a real bucket name to teardown for cleanup.
    result["storage_bucket"] = bucket_name
    result["disk_ids"] = [object_key]

    image_path: Path | None = None
    bucket_created = False

    # Cleanup ownership is "did this step's create get accepted?", which is
    # SEPARATE from "what name did we submit." `image_id` doubles as the
    # cleanup target (consumed by _cleanup_on_failure here and forwarded to the
    # teardown step), so it is stamped ONLY from this callback, which the
    # register helpers invoke after the create is accepted (insert ack on the
    # RAW path / clean `gcloud images import` exit on the foreign path) and
    # before the register op-wait / READY poll. A wait-side timeout after accept
    # therefore still cleans up the image this step created; a synchronous
    # AlreadyExists/Conflict raises before the callback fires, so a name
    # collision with a pre-existing image is reported as a failure and never
    # deletes a resource this step did not create.
    def _mark_image_owned() -> None:
        result["image_id"] = image_name

    try:
        image_path = download_image(args.image_url, image_format)
        if image_path is None:
            result["error"] = f"Failed to download image from {args.image_url}"
            print(json.dumps(result, indent=2))
            return 1

        bucket = ensure_bucket(storage_client, bucket_name, region)
        bucket_created = True
        raw_source_url = upload_object(bucket, image_path, object_key)

        # The unsupported-format branch raises BEFORE any insert, so no name is
        # reserved and ownership is never claimed.
        if image_format not in (_RAW_FORMATS | _FOREIGN_FORMATS):
            raise RuntimeError(
                f"unsupported image format {image_format!r}; expected one of {sorted(_RAW_FORMATS | _FOREIGN_FORMATS)}"
            )

        if image_format in _RAW_FORMATS:
            register_raw_image(project, image_name, raw_source_url, on_accepted=_mark_image_owned)
        else:
            import_foreign_image(
                project,
                zone,
                image_name,
                _gcs_source_from_url(raw_source_url),
                args.guest_os,
                on_accepted=_mark_image_owned,
            )

        # Observable-completion: confirm the image is actually registered and
        # usable before reporting success (the gcloud path returns before the
        # READY transition is independently observable).
        image = _wait_for_image_ready(project, image_name, timeout=600)

        result["success"] = True
        result["image_name"] = image_name
        result["storage_path"] = object_key
        result["image_format"] = image_format
        result["zone"] = zone
        result["image_state"] = image.status or "READY"
        print(f"Imported image {image_name} (status={image.status})", file=sys.stderr)
        print(json.dumps(result, indent=2))
        return 0
    except Exception as e:
        error_type, error_msg = classify_gcp_error(e)
        result["success"] = False
        result["error_type"] = error_type
        result["error"] = error_msg
        # Best-effort cleanup of this step's own partial artifacts so a failed
        # import does not leak a bucket/image (teardown still re-attempts via
        # the forwarded identifiers, but cleaning here keeps the failure tidy).
        # Delete the image ONLY when ownership was confirmed: result["image_id"]
        # is populated solely by the post-accept callback, so a synchronous
        # create conflict (which never set it) leaves a pre-existing image alone.
        _cleanup_on_failure(
            project,
            storage_client,
            bucket_name if bucket_created else None,
            result["image_id"] or None,
        )
        print(json.dumps(result, indent=2))
        return 1
    finally:
        if image_path is not None and image_path.exists():
            image_path.unlink(missing_ok=True)


def _looks_like_zone(value: str) -> bool:
    """Return True iff ``value`` ends in a single zone letter (e.g. us-central1-a)."""
    parts = value.rsplit("-", 1)
    return len(parts) == 2 and len(parts[1]) == 1 and parts[1].isalpha()


def _wait_for_image_ready(project: str, image_name: str, *, timeout: int = 600) -> compute_v1.Image:
    """Poll images.get until the image reports READY, raising on FAILED/timeout."""
    client = compute_v1.ImagesClient()
    deadline = time.monotonic() + timeout
    while True:
        image = client.get(project=project, image=image_name)
        status = (image.status or "").upper()
        if status == "READY":
            return image
        if status == "FAILED":
            raise RuntimeError(f"image {image_name} entered FAILED state during import")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"image {image_name} did not reach READY (last={status!r}) in {timeout}s")
        time.sleep(10)


def _cleanup_on_failure(
    project: str,
    storage_client: storage.Client,
    bucket_name: str | None,
    image_name: str | None,
) -> None:
    """Best-effort removal of this step's partial bucket / image on failure."""
    if image_name:
        try:
            op = compute_v1.ImagesClient().delete(project=project, image=image_name)
            op_name = getattr(op, "name", "")
            if op_name:
                wait_for_global_op(project, op_name, timeout=300)
        except gax.NotFound:
            pass
        except Exception as exc:
            print(f"  cleanup-on-failure: image delete raised: {exc}", file=sys.stderr)
    if bucket_name:
        try:
            bucket = storage_client.get_bucket(bucket_name)
            bucket.delete(force=True)
        except gax.NotFound:
            pass
        except Exception as exc:
            print(f"  cleanup-on-failure: bucket delete raised: {exc}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
