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

"""Tests for the Firebird image-registry config and scripts.

The upload service and image catalog are faked statefully (``FakeImages``): it
reassembles the uploaded parts, checks size and checksum at finish the way the
API does, and registers the image. BM provisioning reuses the harness with SSH
key generation and the SSH wait stubbed. No test reaches a network.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
import yaml
from isvtest.core.resolution import parse_validations

from isvctl.config.merger import merge_yaml_files
from isvctl.config.schema import RunConfig
from isvctl.orchestrator.loop import _apply_capability_step_gates

from .harness import (
    FIREBIRD,
    PROJECT,
    HttpError,
    Route,
    composite,
    config_steps,
    fake_keygen,
    load,
    operation,
    render,
    run,
    schema_errors,
)

IMAGES = f"/compute/images/{PROJECT}"
UPLOADS = "/compute/images/uploads"
UPLOAD_ID = "upload.1"


class FakeImages:
    """The upload service and one project's image catalog."""

    def __init__(self, part_size: int = 5, finish_status: int = 0) -> None:
        """Serve uploads with ``part_size``; ``finish_status`` makes finish fail with it."""
        self.part_size = part_size
        self.finish_status = finish_status
        self.declared: dict[str, Any] = {}
        self.parts: dict[int, bytes] = {}
        self.images: dict[str, dict[str, Any]] = {}
        self.aborted = False
        self.deleted: list[str] = []

    def routes(self, max_parts: int = 4) -> dict[str, Route]:
        """Return the upload and catalog routes (one session, ``image.1`` and ``image.2``)."""
        routes: dict[str, Route] = {
            f"POST {IMAGES}/uploads": self._start,
            f"POST {UPLOADS}/{UPLOAD_ID}/complete": self._finish,
            f"GET {UPLOADS}/{UPLOAD_ID}": {"uploadId": UPLOAD_ID, "status": "COMPLETE", "finished": True},
            f"DELETE {UPLOADS}/{UPLOAD_ID}": self._abort,
            f"GET {IMAGES}": lambda _b, _q: {"items": list(self.images.values())},
        }
        for n in range(1, max_parts + 1):
            routes[f"PUT {UPLOADS}/{UPLOAD_ID}/parts/{n}"] = self._part(n)
        for image_id in ("image.1", "image.2"):
            routes[f"GET {IMAGES}/{image_id}"] = self._get(image_id)
            routes[f"DELETE {IMAGES}/{image_id}"] = self._delete(image_id)
        return routes

    def _start(self, body: Any, _q: Any) -> dict[str, Any]:
        """POST .../uploads: declare the file."""
        self.declared = dict(body)
        return {"uploadId": UPLOAD_ID, "partSizeBytes": self.part_size, "maxInFlightParts": 4}

    def _part(self, n: int) -> Route:
        """PUT one raw part."""

        def put(body: Any, _q: Any) -> dict[str, Any]:
            assert isinstance(body, bytes)
            self.parts[n] = body
            return {"partNumber": n, "sizeBytes": len(body)}

        return put

    def _finish(self, body: Any, _q: Any) -> dict[str, Any]:
        """POST .../complete: parts contiguous, size and checksum match, then register."""
        if self.finish_status:
            raise HttpError(self.finish_status)
        data = b"".join(self.parts[n] for n in sorted(self.parts))
        assert sorted(self.parts) == list(range(1, len(self.parts) + 1))
        assert len(data) == self.declared["sizeBytes"]
        assert body == {"checksum": hashlib.sha256(data).hexdigest(), "checksumAlgorithm": "sha256"}
        image_id = f"image.{len(self.images) + 1}"
        self.images[image_id] = {"id": image_id, "name": self.declared["name"], "scopeId": PROJECT}
        return {"uploadId": UPLOAD_ID, "objectId": UPLOAD_ID, "status": "COMPLETE", "imageId": image_id}

    def _abort(self, _b: Any, _q: Any) -> dict[str, Any]:
        """DELETE .../uploads/{id}: abort the upload."""
        self.aborted = True
        return {"uploadId": UPLOAD_ID, "status": "ABORTED"}

    def _get(self, image_id: str) -> Route:
        """GET one image; 404 when absent."""

        def get(_b: Any, _q: Any) -> dict[str, Any]:
            if image_id not in self.images:
                raise HttpError(404)
            return {"image": self.images[image_id]}

        return get

    def _delete(self, image_id: str) -> Route:
        """DELETE one image; 404 when absent."""

        def delete(_b: Any, _q: Any) -> dict[str, Any]:
            if image_id not in self.images:
                raise HttpError(404)
            del self.images[image_id]
            self.deleted.append(image_id)
            return operation(image_id)

        return delete


def _image_file(tmp_path: Path, size: int = 12) -> Path:
    """Write a local image source of ``size`` bytes."""
    path = tmp_path / "disk.qcow2"
    path.write_bytes(bytes(range(size)))
    return path


def _upload(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    routes: dict[str, Route],
    source: str,
    image_format: str = "qcow2",
) -> tuple[int, dict[str, Any], Any]:
    """Run upload_image against ``routes``."""
    return run(
        monkeypatch,
        capsys,
        "image-registry/upload_image.py",
        routes,
        ["--image-url", source, "--image-format", image_format],
    )


# ── Config ────────────────────────────────────────────────────────────


def _run_config() -> RunConfig:
    """Return the merged image-registry config."""
    return RunConfig.model_validate(merge_yaml_files([FIREBIRD / "config" / "image-registry.yaml"]))


def test_config_orders_the_image_delete_after_the_bm_release() -> None:
    """The BM is deprovisioned (and the network deleted) before the image it references."""
    steps = config_steps("image-registry", "image_registry")
    names = list(steps)

    assert names[:2] == ["upload_image", "create_network"]
    assert names.index("teardown_bm") < names.index("teardown_network") < names.index("teardown_image")
    assert not {"crud_install_config", "install_config_bm", "launch_instance", "teardown_instance"} & set(steps)
    assert "labels:" not in (FIREBIRD / "config" / "image-registry.yaml").read_text()


def test_bm_steps_run_only_under_the_bare_metal_capability() -> None:
    """A core run skips the network and BM steps; a bare_metal run keeps them."""
    config = _run_config()
    steps = config.commands["image_registry"].steps
    entries = parse_validations(config.tests.validations)
    bm_steps = {"create_network", "install_image_bm", "teardown_bm", "teardown_network"}

    core = {s.name for s in _apply_capability_step_gates(steps, entries, "core") if s.skip}
    bare_metal = {s.name for s in _apply_capability_step_gates(steps, entries, "bare_metal") if s.skip}

    assert core == bm_steps
    assert bare_metal == set()


def test_config_overrides_the_suite_vmdk_with_a_bootable_qcow2(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite default is a .vmdk the API refuses; the provider default is Ubuntu's qcow2."""
    monkeypatch.delenv("FIREBIRD_IMAGE_URL", raising=False)
    monkeypatch.delenv("FIREBIRD_IMAGE_FORMAT", raising=False)
    suite = yaml.safe_load((FIREBIRD.parents[1] / "suites" / "image-registry.yaml").read_text())
    assert suite["tests"]["settings"]["image_format"] == "vmdk"

    args = render("image-registry", "image_registry", "upload_image", {})

    assert args[1].endswith("ubuntu-24.04-server-cloudimg-amd64.img")
    assert args[3] == "qcow2"


def test_bm_teardown_undoes_exactly_what_the_install_did() -> None:
    """teardown_bm deprovisions an installed BM; teardown_image deletes the uploaded image."""
    install = {
        "instance_id": "bm.A",
        "owned": True,
        "attached_subnet": True,
        "key_file": "/tmp/k",
        "generated_key": True,
    }

    assert render("image-registry", "image_registry", "teardown_bm", {"install_image_bm": install}) == [
        "--instance-id=bm.A",
        "--deprovision",
        "--detach-subnet",
        "--delete-key-pair",
        "--key-file=/tmp/k",
    ]
    assert render("image-registry", "image_registry", "teardown_image", {"upload_image": {"image_id": "image.1"}}) == [
        "--image-id=image.1"
    ]
    skipped = {"upload_image": {"image_id": "", "skipped": True}}
    assert render("image-registry", "image_registry", "teardown_image", skipped) == ["--image-id=", "--upload-skipped"]


# ── upload_image ──────────────────────────────────────────────────────


def test_upload_streams_parts_and_registers_the_image(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """12 bytes at a 5-byte part size go up as 5+5+2 with a checksum, then the image is read back."""
    fake = FakeImages()
    source = _image_file(tmp_path)

    code, out, _ = _upload(monkeypatch, capsys, fake.routes(), str(source))

    assert code == 0, out
    assert [len(fake.parts[n]) for n in sorted(fake.parts)] == [5, 5, 2]
    assert b"".join(fake.parts.values()) == source.read_bytes()
    assert fake.declared["sizeBytes"] == 12 and fake.declared["filename"] == "disk.qcow2"
    assert out["image_id"] == "image.1" and out["storage_bucket"] == PROJECT and out["disk_ids"] == [UPLOAD_ID]
    assert not fake.aborted
    assert schema_errors("upload_image", out) == []
    assert composite("image-registry", "CustomOsImageUploadedCheck", out) == []


def test_upload_accepts_a_file_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """``file://`` sources are read locally."""
    code, out, _ = _upload(monkeypatch, capsys, FakeImages().routes(), _image_file(tmp_path).as_uri())

    assert code == 0, out


@pytest.mark.parametrize("status", [404, 501])
def test_upload_skips_when_the_upload_service_is_not_registered(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path, status: int
) -> None:
    """The upload routes answer 404 or 501: a schema-valid structured skip."""
    code, out, api = _upload(monkeypatch, capsys, {f"POST {IMAGES}/uploads": status}, str(_image_file(tmp_path)))

    assert code == 0
    assert out["skipped"] is True and "image upload API" in out["skip_reason"]
    assert api.paths() == [f"POST {IMAGES}/uploads"]
    assert schema_errors("upload_image", out) == []


@pytest.mark.parametrize("status", [404, 501])
def test_upload_fails_rather_than_skips_once_the_session_exists(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path, status: int
) -> None:
    """A 404/501 after the upload has started is a failure (and the session is aborted), not a disabled service."""
    fake = FakeImages()
    routes = {**fake.routes(), f"PUT {UPLOADS}/{UPLOAD_ID}/parts/1": status}

    code, out, _ = _upload(monkeypatch, capsys, routes, str(_image_file(tmp_path)))

    assert code == 1
    assert "skipped" not in out and out["upload_id"] == UPLOAD_ID
    assert fake.aborted


def test_upload_fails_an_expired_session(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """An expired session never completes: the status wait fails at once instead of polling on."""
    statuses = iter([{"status": "UPLOADING", "expired": True}, {"status": "COMPLETE", "finished": True}])
    routes = {**FakeImages().routes(), f"GET {UPLOADS}/{UPLOAD_ID}": lambda _b, _q: next(statuses)}

    code, out, api = _upload(monkeypatch, capsys, routes, str(_image_file(tmp_path)))

    assert code == 1 and "expired" in out["error"]
    assert api.paths("GET").count(f"GET {UPLOADS}/{UPLOAD_ID}") == 1


def test_upload_refuses_a_format_bare_metal_cannot_boot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The suite's vmdk is refused before any call; the API would reject it only after the upload."""
    code, out, api = _upload(monkeypatch, capsys, {}, str(_image_file(tmp_path)), image_format="vmdk")

    assert code == 1
    assert out["error_type"] == "bad_input" and "qcow2 or raw" in out["error"]
    assert api.calls == []


@pytest.mark.parametrize("failure", ["part", "finish"])
def test_upload_aborts_the_session_when_it_fails_before_finishing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path, failure: str
) -> None:
    """A failed part or a refused finish (e.g. raw without a partition table) aborts the session."""
    fake = FakeImages(finish_status=400 if failure == "finish" else 0)
    routes = fake.routes()
    if failure == "part":
        routes[f"PUT {UPLOADS}/{UPLOAD_ID}/parts/2"] = 500

    code, out, _ = _upload(monkeypatch, capsys, routes, str(_image_file(tmp_path)))

    assert code == 1
    assert fake.aborted
    assert out["upload_id"] == UPLOAD_ID and out["image_id"] == ""
    assert composite("image-registry", "CustomOsImageUploadedCheck", out)


def test_upload_records_a_registered_image_when_the_status_wait_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Finish registered the image, so it is recorded for teardown and the session is not aborted."""
    fake = FakeImages()
    routes = {**fake.routes(), f"GET {UPLOADS}/{UPLOAD_ID}": {"uploadId": UPLOAD_ID, "status": "FAILED"}}

    code, out, _ = _upload(monkeypatch, capsys, routes, str(_image_file(tmp_path)))

    assert code == 1 and "ended FAILED" in out["error"]
    assert out["image_id"] == "image.1"
    assert not fake.aborted
    assert render("image-registry", "image_registry", "teardown_image", {"upload_image": out}) == ["--image-id=image.1"]


# ── crud_image ────────────────────────────────────────────────────────


def test_probe_image_has_an_mbr_the_api_accepts() -> None:
    """The CRUD probe image passes the API's raw-image check (signature, valid entries, one used)."""
    image = load("image-registry/crud_image.py").probe_disk_image()
    entries = [image[446 + i * 16 : 462 + i * 16] for i in range(4)]

    assert len(image) >= 4096
    assert image[510:512] == b"\x55\xaa"
    assert all(entry[0] in (0x00, 0x80) for entry in entries)
    assert any(entry[4] for entry in entries)


def test_crud_runs_the_lifecycle_on_its_own_image(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Create (upload), get, list, delete, then 404 - on a probe image, not the install image."""
    fake = FakeImages(part_size=5 * 1024 * 1024)

    code, out, _ = run(monkeypatch, capsys, "image-registry/crud_image.py", fake.routes())

    assert code == 0, out
    assert fake.declared["name"].startswith("isv-ir-crud-")
    assert fake.deleted == ["image.1"] and fake.images == {}
    assert composite("image-registry", "CustomOsImageCrudCheck", out) == []


def test_crud_deletes_its_image_when_a_later_operation_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed read still deletes the probe image on the way out."""
    fake = FakeImages(part_size=5 * 1024 * 1024)
    routes = {**fake.routes(), f"GET {IMAGES}/image.1": 500}

    code, out, _ = run(monkeypatch, capsys, "image-registry/crud_image.py", routes)

    assert code == 1
    assert fake.deleted == ["image.1"]
    assert out["operations"]["create"]["passed"] is True
    assert out["operations"]["delete"]["passed"] is False
    assert composite("image-registry", "CustomOsImageCrudCheck", out)


def test_crud_fails_a_delete_that_leaves_the_image_readable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A delete Operation that completes while the image still reads back is not a delete."""
    fake = FakeImages(part_size=5 * 1024 * 1024)
    routes = {**fake.routes(), f"DELETE {IMAGES}/image.1": operation("image.1")}

    code, out, _ = run(monkeypatch, capsys, "image-registry/crud_image.py", routes)

    assert code == 1
    assert out["operations"]["delete"]["passed"] is False
    assert composite("image-registry", "CustomOsImageCrudCheck", out)


def test_crud_skips_when_the_upload_service_is_not_registered(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without the upload service there is no create, so the step skips."""
    code, out, _ = run(monkeypatch, capsys, "image-registry/crud_image.py", {f"POST {IMAGES}/uploads": 501})

    assert code == 0 and out["skipped"] is True


# ── install_image_bm ──────────────────────────────────────────────────


def _bm_routes(provisions: list[dict[str, Any]], reported_image: str = "image.1") -> dict[str, Route]:
    """Return routes for one AVAILABLE BM already in the project and subnet."""
    bm_path = f"/projects/{PROJECT}/compute/bms/bm.A"

    def provision(body: Any, _q: Any) -> dict[str, Any]:
        provisions.append(body)
        return operation("bm.A")

    return {
        "GET /compute/bms": {
            "items": [{"id": "bm.A", "state": "AVAILABLE", "projectId": PROJECT, "subnetId": "subnet.S"}]
        },
        f"POST {bm_path}/provision": provision,
        f"GET {bm_path}": {
            "bm": {
                "id": "bm.A",
                "state": "RUNNING",
                "powerState": "ON",
                "ipAddress": "172.16.244.10",
                "subnetId": "subnet.S",
                "spec": {"imageId": reported_image},
            }
        },
    }


def _install(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    routes: dict[str, Route],
    argv: list[str],
    temp_root: Path,
) -> tuple[int, dict[str, Any], Any]:
    """Run install_image_bm with key generation kept under ``temp_root`` and the SSH and cloud-init waits stubbed."""

    def prepare(module: Any) -> None:
        fake_keygen(monkeypatch, module, temp_root)
        monkeypatch.setattr(module._common["provision"], "wait_for_ssh", lambda *_a, **_k: True)
        monkeypatch.setattr(module._common["provision"], "wait_for_cloud_init", lambda *_a, **_k: "done")
        # A reusable BM is configured, but install must provision anyway.
        monkeypatch.setenv("BM_INSTANCE_ID", "bm.kept")
        monkeypatch.setenv("BM_KEY_FILE", "/tmp/kept")

    return run(monkeypatch, capsys, "image-registry/install_image_bm.py", routes, argv, prepare=prepare)


INSTALL_ARGS = ["--image-id=image.1", "--vpc-id=vpc.V", "--subnet-id=subnet.S"]


def test_install_provisions_the_uploaded_image_on_a_bm(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The uploaded image is provisioned (even with a reusable BM configured) with a key generated for the run."""
    provisions: list[dict[str, Any]] = []

    code, out, _ = _install(monkeypatch, capsys, _bm_routes(provisions), INSTALL_ARGS, tmp_path)

    assert code == 0, out
    assert [p["imageId"] for p in provisions] == ["image.1"]
    assert out["instance_id"] == "bm.A" and out["owned"] is True and out["attached_subnet"] is False
    assert out["generated_key"] is True and Path(out["key_file"]).parent.parent == tmp_path
    assert composite("image-registry", "BmHostBootedFromCustomImageCheck", out) == []


def test_install_fails_when_the_bm_reports_another_image(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """RUNNING is not enough: the BM must report the uploaded image."""
    routes = _bm_routes([], reported_image="image.stock")
    code, out, _ = _install(monkeypatch, capsys, routes, INSTALL_ARGS, tmp_path)

    assert code == 1
    assert "reports image image.stock" in out["error"]
    assert out["owned"] is True  # teardown still deprovisions it
    assert composite("image-registry", "BmHostBootedFromCustomImageCheck", out)


def test_install_skips_when_the_upload_was_skipped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """No uploaded image, nothing to install: a structured skip with no API calls."""
    code, out, api = _install(monkeypatch, capsys, {}, ["--image-id=", "--upload-skipped"], tmp_path)

    assert code == 0 and out["skipped"] is True
    assert api.calls == []


# ── teardown_image ────────────────────────────────────────────────────


@pytest.mark.parametrize(("present", "message"), [(True, "Image deleted"), (False, "Image already gone")])
def test_teardown_image_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], present: bool, message: str
) -> None:
    """The uploaded image is deleted; an already-deleted one passes."""
    fake = FakeImages()
    if present:
        fake.images["image.1"] = {"id": "image.1"}

    code, out, _ = run(monkeypatch, capsys, "image-registry/teardown_image.py", fake.routes(), ["--image-id=image.1"])

    assert code == 0 and out["message"] == message
    assert composite("image-registry", "CustomOsImageDeletedCheck", out) == []


def test_teardown_image_fails_while_a_bm_still_references_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The API refuses (400) to delete an image a BM references; teardown reports it."""
    routes = {f"DELETE {IMAGES}/image.1": 400}

    code, out, _ = run(monkeypatch, capsys, "image-registry/teardown_image.py", routes, ["--image-id=image.1"])

    assert code == 1
    assert composite("image-registry", "CustomOsImageDeletedCheck", out)


def test_teardown_image_skips_when_the_upload_was_skipped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A skipped upload leaves nothing to delete."""
    code, out, api = run(
        monkeypatch, capsys, "image-registry/teardown_image.py", {}, ["--image-id=", "--upload-skipped"]
    )

    assert code == 0 and out["skipped"] is True and api.calls == []
