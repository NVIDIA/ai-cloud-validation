# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Exercise application observation over real local HTTP connections."""

import importlib.util
import json
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

SPEC = importlib.util.spec_from_file_location(
    "upgrade_observer",
    Path(__file__).resolve().parents[2] / "isvctl/configs/providers/aws/scripts/eks/upgrade_observer.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
TOKEN = "unique-canary-token"


@pytest.fixture
def endpoint() -> Iterator[tuple[str, dict[str, Any]]]:
    """Run an isolated HTTP endpoint with configurable failures and request accounting."""
    state: dict[str, Any] = {"status": 200, "body": TOKEN, "calls": 0}

    class Handler(BaseHTTPRequestHandler):
        """Return exactly the requested failure mode without unrelated server logs."""

        def do_GET(self) -> None:
            """Serve a canary response or redirect to a URL that must not be followed."""
            state["calls"] += 1
            self.send_response(state["status"])
            if state["status"] == 302:
                self.send_header("Location", "/would-mask-an-outage")
            self.end_headers()
            self.wfile.write(state["body"].encode())

        def log_message(self, fmt: str, *args: Any) -> None:
            """Suppress routine HTTP logs in unit-test output."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def wait_for(predicate: Callable[[], bool]) -> None:
    """Bound synchronization with the background observer."""
    deadline = time.monotonic() + 3
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Observer did not emit the expected evidence")
        time.sleep(0.01)


def test_continuous_observer_preserves_outage_and_recovery(
    endpoint: tuple[str, dict[str, Any]], tmp_path: Path
) -> None:
    """Persist failed probes even when the application later recovers."""
    url, state = endpoint
    artifact = tmp_path / "application.jsonl"
    observer = MODULE.ApplicationObserver(url, TOKEN, artifact, interval=0.02)
    observer.start()
    try:
        state["status"] = 503
        wait_for(lambda: any(p["status_code"] == 503 for p in observer.probes))
        failed_index = len(observer.probes)
        state["status"] = 200
        wait_for(lambda: any(p["status_code"] == 200 for p in observer.probes[failed_index:]))
    finally:
        output = observer.stop()
    assert output["endpoint"] == url
    assert output["expected_response"] == TOKEN
    assert output["probes"][0]["response"] == TOKEN
    assert any(p["status_code"] == 503 for p in output["probes"])
    assert output["probes"][-1]["status_code"] == 200
    assert [json.loads(line) for line in artifact.read_text().splitlines()] == output["probes"]
    assert state["calls"] == len(output["probes"])


@pytest.mark.parametrize("status", [302, 429, 503])
def test_failed_requests_are_never_retried_or_redirected(
    endpoint: tuple[str, dict[str, Any]], tmp_path: Path, status: int
) -> None:
    """A redirect or transient failure remains exactly one failed request."""
    url, state = endpoint
    state["status"] = status
    observer = MODULE.ApplicationObserver(url, TOKEN, tmp_path / "evidence")
    probe = observer.request()
    assert probe["status_code"] == status
    assert probe["duration_seconds"] >= 0
    assert state["calls"] == 1


def test_wrong_baseline_prevents_upgrade(endpoint: tuple[str, dict[str, Any]], tmp_path: Path) -> None:
    """A reachable default page is not a healthy canary baseline."""
    url, state = endpoint
    state["body"] = "default backend"
    observer = MODULE.ApplicationObserver(url, TOKEN, tmp_path / "evidence")
    with pytest.raises(RuntimeError, match="baseline"):
        observer.start()
    assert not observer._thread.is_alive()
    assert observer.probes[0]["response"] == "default backend"


def test_evidence_file_is_never_overwritten(endpoint: tuple[str, dict[str, Any]], tmp_path: Path) -> None:
    """A collector error fails before upgrade submission instead of losing prior evidence."""
    path = tmp_path / "evidence"
    path.write_text("previous run\n")
    observer = MODULE.ApplicationObserver(endpoint[0], TOKEN, path)
    with pytest.raises(RuntimeError, match="persist"):
        observer.start()
    assert path.read_text() == "previous run\n"


@pytest.mark.parametrize("endpoint", ["file:///tmp/data", "http://user:secret@example.invalid", "https://"])
def test_invalid_endpoint(endpoint: str, tmp_path: Path) -> None:
    """Only a credential-free application URL is accepted as the observer endpoint."""
    with pytest.raises(ValueError):
        MODULE.ApplicationObserver(endpoint, TOKEN, tmp_path / "evidence")


def test_connection_failure_is_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A socket failure is emitted as status zero, not silently omitted."""

    class Offline:
        """Simulate a transport error before any HTTP response arrives."""

        def open(self, request: Any, timeout: float) -> None:
            """Fail the single request attempt."""
            raise TimeoutError("private endpoint detail")

    monkeypatch.setattr(MODULE.urllib.request, "build_opener", lambda *args: Offline())
    probe = MODULE.ApplicationObserver("https://example.invalid", TOKEN, tmp_path / "evidence").request()
    assert probe["status_code"] == 0
    assert probe["error_type"] == "TimeoutError"
    assert "private endpoint" not in json.dumps(probe)
