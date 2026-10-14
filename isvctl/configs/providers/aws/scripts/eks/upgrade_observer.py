# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Collect application responses independently of Kubernetes and provider polling.

The controller must be able to reach the test Service's stable HTTP(S) endpoint.
Requests are neither retried nor redirected: either could conceal an outage.
Each result is flushed to a JSONL artifact before the next request starts.
"""

import http.client
import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Preserve redirects as observations instead of following them to a healthy page."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        """Decline redirects so urllib exposes the original HTTP error response."""
        return None


class ApplicationObserver:
    """Continuously probe a canary without depending on control-plane availability."""

    def __init__(
        self, endpoint: str, expected_response: str, artifact: Path, *, interval: float = 1, timeout: float = 2
    ) -> None:
        """Configure bounded requests and an exclusive, run-specific evidence artifact."""
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Application endpoint must be HTTP(S), without embedded credentials")
        if not expected_response or len(expected_response.encode()) > 4096:
            raise ValueError("Expected canary response must contain 1..4096 bytes")
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in (interval, timeout)):
            raise ValueError("Observer interval and timeout must be finite positive numbers")
        self.endpoint = endpoint
        self.expected_response = expected_response
        self.artifact = artifact
        self.interval = interval
        self.timeout = timeout
        self.probes: list[dict[str, Any]] = []
        self.error: Exception | None = None
        self._stop = threading.Event()
        self._first = threading.Event()
        self._thread: threading.Thread | None = None

    def request(self) -> dict[str, Any]:
        """Record every attempt, including transport errors, HTTP failures and timeouts."""
        probe: dict[str, Any] = {"at": time.time(), "status_code": 0, "response": ""}
        began = time.monotonic()
        request = urllib.request.Request(self.endpoint, headers={"Connection": "close", "Cache-Control": "no-cache"})
        opener = urllib.request.build_opener(NoRedirect())
        try:
            with opener.open(request, timeout=self.timeout) as response:
                probe["status_code"] = response.status
                probe["response"] = response.read(4097).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            probe["status_code"] = error.code
            error.close()
        except (OSError, ValueError, http.client.HTTPException) as error:
            # Do not serialize exception text, which can include a credential-bearing URL.
            probe["error_type"] = type(error).__name__
        finally:
            probe["duration_seconds"] = time.monotonic() - began
        return probe

    def _observe(self) -> None:
        """Persist evidence continuously; a collector failure is never a missing sample."""
        try:
            with self.artifact.open("x", encoding="utf-8") as stream:
                while not self._stop.is_set():
                    began = time.monotonic()
                    probe = self.request()
                    stream.write(json.dumps(probe) + "\n")
                    stream.flush()
                    self.probes.append(probe)
                    self._first.set()
                    self._stop.wait(max(0, self.interval - (time.monotonic() - began)))
        except Exception as error:
            self.error = error
            self._first.set()

    def start(self) -> None:
        """Establish a successful baseline before the caller submits an upgrade."""
        if self._thread is not None:
            raise RuntimeError("Observer cannot be started twice")
        self._thread = threading.Thread(target=self._observe, name="upgrade-application-observer", daemon=True)
        self._thread.start()
        if not self._first.wait(self.timeout + 5):
            self._stop.set()
            raise TimeoutError("Application baseline observation did not complete")
        if self.error is not None:
            raise RuntimeError("Could not persist application evidence") from self.error
        baseline = self.probes[0]
        if (
            baseline["status_code"] != 200
            or baseline["response"] != self.expected_response
            or baseline["duration_seconds"] > self.timeout
        ):
            self.stop()
            raise RuntimeError("Canary baseline request failed; upgrade was not started")

    def stop(self) -> dict[str, Any]:
        """Join the observer and return evidence, preserving every unsuccessful request."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(self.timeout + 5)
            if self._thread.is_alive():
                raise TimeoutError("Application observer did not stop within its request deadline")
        if self.error is not None:
            raise RuntimeError("Application evidence collection failed") from self.error
        return {"endpoint": self.endpoint, "expected_response": self.expected_response, "probes": list(self.probes)}
