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

"""Parallel filesystem client (``wekafs``) on a provisioned BM: install, token, mount, and unmount over SSH.

The Filesystem API hands out, per filesystem, its
``storageEndpoint`` (``http://<host>:<port>``) and ``authCredentialsBase64``,
a base64 client ``auth-token.json`` for the tenant's filesystem organization. The mount
steps run on an already-provisioned BM (``BM_INSTANCE_ID`` + ``BM_KEY_FILE``, as
the network suite does):

* The client comes from the storage endpoint's own distribution route
  (``<storageEndpoint>/dist/v1/install``), installed only when ``mount.wekafs``
  is missing and left installed afterwards. ``https`` on the endpoint's own
  host:port is tried first, falling back to the API-returned URL only if
  ``https`` fails; ``ISV_WEKA_INSTALL_URL`` overrides both.
* The token travels to the BM over SSH stdin only. A fixed program on the BM
  (``sudo python3 -c ...``, no secret in its argv) checks it decodes to the
  client's auth-token shape and writes it to a suite-owned file,
  ``/root/.weka/isv-<run_id>.json`` (0600, in a 0700 directory) - never the
  tenant's own ``/root/.weka/auth-token.json``, which this provider never
  writes to or deletes. Mounts pass ``auth_token_path=<that file>`` and every
  ``weka`` CLI call sets ``WEKA_TOKEN`` to it, so nothing here ever depends on,
  overwrites, or removes a token the suite did not write itself. Nothing here
  prints, logs, decodes, or stores the credential locally; every error text
  that could echo remote output goes through ``redact``.
* Mounts live under ``/mnt/isv-<run_id>[-<role>]``, and ``unmount`` refuses any
  other path, so the tenant's own mounts are never touched.

Stdlib only.
"""

import argparse
import json
import os
import re
import shlex
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

from common.filesystems import mount_credentials
from common.firebird_client import FirebirdClient
from common.probes import Host, running_host, skipped
from common.ssh_utils import ssh_run

NO_BM = "no BM configured for mount checks"
NOT_MOUNTED = "the run's filesystem is not mounted on the BM"
TOKEN_DIR = "/root/.weka"
# The tenant's own client token: this provider never writes, reads, or deletes it.
TENANT_TOKEN_FILE = f"{TOKEN_DIR}/auth-token.json"
WEKA_TOKEN_ENV = "WEKA_TOKEN"
INSTALL_URL_ENV = "ISV_WEKA_INSTALL_URL"
# UDP mode needs no DPDK-capable NIC or hugepage setup on the client, so it works
# on any tenant BM that routes to the storage endpoint.
DEFAULT_MOUNT_OPTIONS = "net=udp"
FS_TYPE = "wekafs"
INSTALL_TIMEOUT = 900
MOUNT_TIMEOUT = 900  # the first mount builds the client drivers and starts its container
SSH_SLACK = 60
REDACTED = "<redacted>"
_MOUNT_POINT = re.compile(r"^/mnt/isv-[0-9a-f]{6}(-[A-Za-z0-9_-]+)?$")
_RUN_ID_FROM_MOUNT = re.compile(r"^/mnt/isv-([0-9a-f]{6})(?:-[A-Za-z0-9_-]+)?$")
_RUN_ID_FROM_TOKEN = re.compile(r"^/root/\.weka/isv-([0-9a-f]{6})\.json$")
_HOSTNAME = re.compile(r"^[A-Za-z0-9.-]+$|^\[[0-9A-Fa-f:.]+\]$")
_SECRET_FIELDS = re.compile(
    r'("?(?:authCredentialsBase64|access_token|refresh_token|accessToken|refreshToken|password)"?\s*[:=]\s*)'
    r'("[^"]*"|\S+)',
    re.IGNORECASE,
)
_BEARER = re.compile(r"(Bearer\s+)\S+", re.IGNORECASE)
# A long base64/base64url run (a token blob or JWT that reached an error text without its
# field name). Paths match the character class too, so a run is masked only when one
# segment between slashes is long, which path segments never are.
_BLOB = re.compile(r"[A-Za-z0-9+/=_-]{40,}")
_BLOB_SEGMENT = 24
ERROR_CHARS = 400

# Runs as root on the BM with the credential on stdin and the target path in argv[1]
# (never a secret, so it is safe there). It prints key names only.
TOKEN_WRITER = """
import base64, json, os, sys
path = sys.argv[1]
try:
    token = json.loads(base64.b64decode(sys.stdin.read().strip(), validate=True))
except Exception as e:
    sys.exit("the mount credential is not base64 JSON (" + type(e).__name__ + ")")
if not isinstance(token, dict) or not {"access_token", "refresh_token", "token_type"} <= set(token):
    shape = sorted(token) if isinstance(token, dict) else type(token).__name__
    sys.exit("the mount credential is not a Weka auth token (keys: " + str(shape) + ")")
os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
os.chmod(os.path.dirname(path), 0o700)
tmp = path + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(token, f)
os.replace(tmp, path)
print(json.dumps({"keys": sorted(token)}))
"""

# Writes, reads back, and removes a probe file; prints {"ok": ..., "bytes": ...}.
RW_PROBE = """
import json, os, sys
path = os.path.join(sys.argv[1], ".isv-probe-" + os.urandom(6).hex())
data = os.urandom(1 << 20)
try:
    with open(path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    with open(path, "rb") as f:
        ok = f.read() == data
finally:
    if os.path.exists(path):
        os.remove(path)
print(json.dumps({"ok": ok, "bytes": len(data)}))
"""

STATVFS = """
import json, os, sys
s = os.statvfs(sys.argv[1])
print(json.dumps({"total_bytes": s.f_blocks * s.f_frsize, "free_bytes": s.f_bavail * s.f_frsize,
                  "files": s.f_files, "files_free": s.f_ffree}))
"""


def redact(text: Any, *secrets: str) -> str:
    """Return ``text`` with every secret value, token field, and bearer token masked, truncated."""
    out = str(text or "")
    for secret in secrets:
        if secret:
            out = out.replace(secret, REDACTED)
    out = _SECRET_FIELDS.sub(lambda m: m.group(1) + REDACTED, out)
    out = _BEARER.sub(lambda m: m.group(1) + REDACTED, out)
    out = _BLOB.sub(lambda m: REDACTED if max(map(len, m.group(0).split("/"))) >= _BLOB_SEGMENT else m.group(0), out)
    # One line: remote tools end messages with newlines and stray NULs.
    return " ".join(out.replace("\x00", " ").split())[-ERROR_CHARS:]


# ── Arguments and gating ──────────────────────────────────────────────


def bm_configured(args: argparse.Namespace) -> bool:
    """Return whether a BM was configured for the mount checks."""
    return bool(args.instance_id and args.key_file)


def mounted_host(
    args: argparse.Namespace, result: dict[str, Any], keys: tuple[str, ...]
) -> tuple[Host | None, int | None]:
    """Resolve the BM carrying the run's mount, or finish ``result`` as a skip or failure.

    Returns ``(host, None)`` to go on, or ``(None, exit_code)`` when ``result`` is
    final: a skip without a BM, a failure (every subtest in ``keys`` failed with
    the reason) when the filesystem is not mounted or the BM cannot be resolved.
    """
    if not bm_configured(args):
        skipped(result, NO_BM)
        return None, 0
    error = ""
    if not args.mount_point:
        error = f"{NOT_MOUNTED}: {redact(args.mount_error) or 'setup_mount did not mount it'}"
    elif not is_run_mount_point(args.mount_point):
        error = f"refusing to probe {args.mount_point!r}: not a /mnt/isv-<run_id> mount point"
    else:
        try:
            host = running_host(FirebirdClient(), args.instance_id, args.ssh_user, args.key_file)
            if not is_mounted(host, args.mount_point):
                error = f"{NOT_MOUNTED}: nothing is mounted at {args.mount_point}"
            else:
                return host, None
        except Exception as e:
            error = redact(e)
    result["error"] = error
    result.setdefault("tests", {}).update({key: {"passed": False, "error": error} for key in keys})
    return None, 1


def fill_missing(tests: dict[str, dict[str, Any]], keys: tuple[str, ...], error: str) -> None:
    """Record every subtest in ``keys`` that did not run as failed with ``error``."""
    for key in keys:
        tests.setdefault(key, {"passed": False, "error": error or "not run"})


# ── Paths and endpoints ───────────────────────────────────────────────


def mount_point(run_id: str, role: str = "") -> str:
    """Return the run's mount point, ``/mnt/isv-<run_id>`` or ``/mnt/isv-<run_id>-<role>``."""
    path = f"/mnt/isv-{run_id}" + (f"-{role}" if role else "")
    if not _MOUNT_POINT.match(path):
        raise ValueError(f"invalid mount point {path!r} (run ID must be 6 lowercase hex digits)")
    return path


def is_run_mount_point(path: str, run_id: str = "") -> bool:
    """Return whether ``path`` is a mount point this provider creates (for ``run_id`` when given)."""
    if not _MOUNT_POINT.match(path or ""):
        return False
    return not run_id or path == f"/mnt/isv-{run_id}" or path.startswith(f"/mnt/isv-{run_id}-")


def run_id_of(path: str) -> str:
    """Return the run ID embedded in a ``/mnt/isv-<run_id>[-role]`` mount point, or ``""``."""
    match = _RUN_ID_FROM_MOUNT.match(path or "")
    return match.group(1) if match else ""


def token_run_id(token_path: str) -> str:
    """Return the run ID embedded in a ``/root/.weka/isv-<run_id>.json`` token path, or ``""``."""
    match = _RUN_ID_FROM_TOKEN.match(token_path or "")
    return match.group(1) if match else ""


def token_file(run_id: str) -> str:
    """Return the run's suite-owned client token path, ``/root/.weka/isv-<run_id>.json``.

    Never the tenant's own ``TENANT_TOKEN_FILE``: this is the only token path the
    suite ever writes to or deletes.
    """
    if not re.fullmatch(r"[0-9a-f]{6}", run_id or ""):
        raise ValueError(f"invalid run ID {run_id!r} (must be 6 lowercase hex digits)")
    return f"{TOKEN_DIR}/isv-{run_id}.json"


def backend_host(endpoint: str) -> str:
    """Return the backend host of a ``storageEndpoint`` (``http://host:port``, or ``host:port``)."""
    parts = urlsplit(endpoint if "//" in endpoint else f"//{endpoint}")
    host = parts.hostname or ""
    if not host or not _HOSTNAME.match(host):
        raise ValueError(f"storageEndpoint {endpoint!r} names no usable host")
    return host


def install_url(endpoint: str) -> str:
    """Return the backend's client installer URL (``<storageEndpoint>/dist/v1/install``)."""
    parts = urlsplit(endpoint if "//" in endpoint else f"http://{endpoint}")
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"storageEndpoint {endpoint!r} is not an http(s) URL")
    backend_host(endpoint)
    return f"{parts.scheme}://{parts.netloc}/dist/v1/install"


def install_urls(endpoint: str) -> list[str]:
    """Return the installer URLs to try in order: ``https`` on the endpoint's host:port first.

    The API-returned URL (``install_url``) is the fallback, tried only if
    ``https`` is refused.
    """
    fallback = install_url(endpoint)
    https = f"https://{urlsplit(fallback).netloc}/dist/v1/install"
    return [https] if https == fallback else [https, fallback]


# ── Remote execution ──────────────────────────────────────────────────


def remote(host: Host, command: str, timeout: int = 60, input_text: str | None = None) -> tuple[int, str, str]:
    """Run ``command`` on ``host``; return (exit code, stdout, stderr)."""
    return ssh_run(host.ip, host.user, host.key_file, command, timeout=timeout, input_text=input_text)


def run_python(host: Host, program: str, args: list[str], timeout: int = 120) -> dict[str, Any]:
    """Run ``program`` as root on ``host`` (program on stdin) and return the JSON it prints last.

    Raises RuntimeError with the (redacted) error output when it fails.
    """
    command = "sudo python3 - " + " ".join(shlex.quote(a) for a in args)
    code, stdout, stderr = remote(host, command, timeout=timeout, input_text=program)
    lines = [line for line in stdout.splitlines() if line.strip()]
    if code != 0 or not lines:
        raise RuntimeError(f"remote probe exited {code}: {redact(stderr or stdout) or 'no output'}")
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError as e:
        raise RuntimeError(f"remote probe printed no JSON: {redact(lines[-1])}") from e


# ── Client ────────────────────────────────────────────────────────────


def client_version(host: Host) -> str:
    """Return the installed client version, or "" when ``mount.wekafs`` is missing."""
    code, stdout, _ = remote(host, "command -v mount.wekafs >/dev/null 2>&1 && weka --version")
    match = re.search(r"\d+(?:\.\d+)+", stdout) if code == 0 else None
    return match.group(0) if match else ""


def install_client(host: Host, endpoint: str, timeout: int = INSTALL_TIMEOUT) -> str:
    """Install the filesystem client from the backend's distribution endpoint; return its version.

    Trusts the installer served by the storage endpoint the authenticated API
    returned, executed as root. ``https`` on that host:port is tried first, the
    API's own URL as fallback (``install_urls``), unless ``ISV_WEKA_INSTALL_URL``
    overrides both with a single URL to use instead. Downloads the installer
    first (so a failed download is not piped into sh as an empty script), never
    follows a redirect, and runs it as root under ``timeout``.
    """
    override = os.environ.get(INSTALL_URL_ENV, "").strip()
    urls = [override] if override else install_urls(endpoint)
    errors: list[str] = []
    code, stdout, stderr = 1, "", "no URL tried"
    for url in urls:
        command = (
            'f=$(mktemp) && curl -fsS --max-time 300 -o "$f" '
            + shlex.quote(url)
            + f' && sudo timeout {int(timeout)} sh "$f"; rc=$?; rm -f "$f"; exit $rc'
        )
        code, stdout, stderr = remote(host, command, timeout=timeout + 300 + SSH_SLACK)
        if code == 0:
            break
        errors.append(f"{url}: {redact(stderr or stdout) or 'no output'}")
    if code != 0:
        raise RuntimeError(f"Weka client install failed (exit {code}): {'; '.join(errors)}")
    version = client_version(host)
    if not version:
        raise RuntimeError("Weka client install reported success but mount.wekafs is still missing")
    return version


def deliver_token(host: Host, credential: str, token_path: str) -> None:
    """Write the mount credential to ``token_path`` on the BM, over SSH stdin only.

    ``token_path`` is a suite-owned file (``token_file``); this never touches
    ``TENANT_TOKEN_FILE``.
    """
    command = f"sudo python3 -c {shlex.quote(TOKEN_WRITER)} {shlex.quote(token_path)}"
    code, stdout, stderr = remote(host, command, timeout=60, input_text=credential + "\n")
    if code != 0:
        raise RuntimeError(f"could not write the Weka token on the BM: {redact(stderr or stdout, credential)}")


def remove_token(host: Host, token_path: str) -> bool:
    """Remove ``token_path`` from the BM; return whether there was one.

    Refuses to remove anything but a suite-owned ``isv-*.json`` token, so a
    caller can never be handed the tenant's own token to delete.
    """
    if not _RUN_ID_FROM_TOKEN.match(token_path or ""):
        raise ValueError(f"refusing to remove {token_path!r}: not a suite-owned /root/.weka/isv-*.json token")
    quoted = shlex.quote(token_path)
    code, stdout, stderr = remote(
        host, f"if sudo test -e {quoted}; then echo present; fi; sudo rm -f {quoted} {quoted}.tmp"
    )
    if code != 0:
        raise RuntimeError(f"could not remove {token_path}: {redact(stderr or stdout)}")
    return "present" in stdout


def leftover_token_files(host: Host) -> list[str]:
    """Return the suite-owned client token files present on the BM (``/root/.weka/isv-*.json``)."""
    code, stdout, _ = remote(host, f"ls -1 {TOKEN_DIR}/isv-*.json 2>/dev/null")
    if code != 0:
        return []
    return [line.strip() for line in stdout.splitlines() if _RUN_ID_FROM_TOKEN.match(line.strip())]


def _weka_prefix(token_path: str = "") -> str:
    """Return the sudo prefix that points the ``weka`` CLI at ``token_path``, or bare ``sudo`` when empty."""
    return f"sudo env {WEKA_TOKEN_ENV}={shlex.quote(token_path)}" if token_path else "sudo"


def weka_filesystems(host: Host, backend: str, token_path: str = "") -> list[str]:
    """Return the filesystem names the BM's client token can see on ``backend``."""
    command = f"{_weka_prefix(token_path)} weka fs -H {shlex.quote(backend)} -J"
    code, stdout, stderr = remote(host, command, timeout=120)
    if code != 0:
        raise RuntimeError(f"weka fs failed (exit {code}): {redact(stderr or stdout)}")
    try:
        return [str(fs.get("name", "")) for fs in json.loads(stdout)]
    except (json.JSONDecodeError, AttributeError, TypeError) as e:
        raise RuntimeError("weka fs printed no filesystem list") from e


def weka_json(host: Host, command: str, token_path: str = "", timeout: int = 120) -> Any:
    """Run ``weka <command> -J`` on the BM (as root, via ``token_path`` when given) and return its JSON.

    Raises with the (redacted) error.
    """
    code, stdout, stderr = remote(host, f"{_weka_prefix(token_path)} weka {command} -J", timeout=timeout)
    if code != 0:
        raise RuntimeError(f"weka {command.split()[0]} failed: {redact(stderr or stdout) or f'exit {code}'}")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"weka {command.split()[0]} printed no JSON") from e


# ── Mounts ────────────────────────────────────────────────────────────


def wekafs_mounts(host: Host) -> list[dict[str, str]]:
    """Return the BM's wekafs mounts as ``{source, target, options}`` from /proc/mounts."""
    code, stdout, stderr = remote(host, "cat /proc/mounts")
    if code != 0:
        raise RuntimeError(f"could not read /proc/mounts: {redact(stderr)}")
    mounts = []
    for line in stdout.splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[2] == FS_TYPE:
            mounts.append({"source": fields[0], "target": fields[1], "options": fields[3]})
    return mounts


def mount_of(host: Host, path: str) -> dict[str, str] | None:
    """Return the wekafs mount at ``path``, or None."""
    return next((m for m in wekafs_mounts(host) if m["target"] == path), None)


def is_mounted(host: Host, path: str) -> bool:
    """Return whether a wekafs filesystem is mounted at ``path``."""
    return mount_of(host, path) is not None


def mount(
    host: Host, backend: str, name: str, path: str, options: str, timeout: int = MOUNT_TIMEOUT, token_path: str = ""
) -> None:
    """Mount filesystem ``name`` from ``backend`` at ``path`` (idempotent for the same source).

    ``token_path``, when given, is passed as the mount's own ``auth_token_path``
    so the mount reads the suite-owned token rather than any default the client
    would otherwise fall back to.
    """
    if not is_run_mount_point(path):
        raise ValueError(f"refusing to mount at {path!r}: not a /mnt/isv-<run_id> mount point")
    source = f"{backend}/{name}"
    current = mount_of(host, path)
    if current:
        if current["source"] != source:
            raise RuntimeError(f"{path} already has {current['source']} mounted, expected {source}")
        return
    all_options = ",".join(filter(None, [options, f"auth_token_path={token_path}" if token_path else ""]))
    option_arg = f"-o {shlex.quote(all_options)} " if all_options else ""
    command = (
        f"sudo mkdir -p {shlex.quote(path)} && sudo timeout {int(timeout)} "
        f"mount -t {FS_TYPE} {option_arg}{shlex.quote(source)} {shlex.quote(path)}"
    )
    code, stdout, stderr = remote(host, command, timeout=timeout + SSH_SLACK)
    if code != 0:
        raise RuntimeError(f"mount -t {FS_TYPE} {source} failed (exit {code}): {redact(stderr or stdout)}")
    if not is_mounted(host, path):
        raise RuntimeError(f"mount -t {FS_TYPE} {source} returned 0 but nothing is mounted at {path}")


def unmount(host: Host, path: str, timeout: int = 300) -> bool:
    """Unmount ``path`` if mounted and remove the empty mount point; return whether it was mounted.

    Refuses any path that is not a ``/mnt/isv-<run_id>`` mount point.
    """
    if not is_run_mount_point(path):
        raise ValueError(f"refusing to unmount {path!r}: not a /mnt/isv-<run_id> mount point")
    was_mounted = is_mounted(host, path)
    quoted = shlex.quote(path)
    command = (
        f"sudo timeout {int(timeout)} umount {quoted} && " if was_mounted else ""
    ) + f"{{ [ ! -d {quoted} ] || sudo rmdir {quoted}; }}"
    code, stdout, stderr = remote(host, command, timeout=timeout + SSH_SLACK)
    if code != 0:
        raise RuntimeError(f"could not unmount {path}: {redact(stderr or stdout) or f'exit {code}'}")
    return was_mounted


def probe_read_write(host: Host, path: str) -> dict[str, Any]:
    """Write, read back, and remove a 1 MiB probe file under ``path``; return ``{ok, bytes}``."""
    return run_python(host, RW_PROBE, [path])


def statvfs(host: Host, path: str) -> dict[str, int]:
    """Return ``{total_bytes, free_bytes, files, files_free}`` of the filesystem at ``path``."""
    return run_python(host, STATVFS, [path])


def mount_filesystem(
    client: FirebirdClient, host: Host, fs: dict[str, Any], path: str, options: str, timeout: int = MOUNT_TIMEOUT
) -> None:
    """Deliver ``fs``'s mount credential to the BM and mount it at ``path``.

    The client must already be installed (``setup_mount``). The credential is
    fetched fresh here, so a token that expired since an earlier mount is
    replaced, and is held only in this frame. The token goes to the run's own
    suite-owned file (``token_file``, derived from ``path``'s run ID) - never
    the tenant's own token.
    """
    fs_id = str(fs.get("id", ""))
    name = str(fs.get("name", ""))
    _, endpoint, credential = mount_credentials(client, fs_id)
    token_path = token_file(run_id_of(path))
    deliver_token(host, credential, token_path)
    backend = backend_host(endpoint)
    visible = weka_filesystems(host, backend, token_path)
    if name not in visible:
        raise RuntimeError(f"filesystem {name} is not visible to the Weka token on the backend")
    mount(host, backend, name, path, options, timeout, token_path)


def token_role(host: Host, token_path: str = "") -> str:
    """Return the backend role of the BM's token (``weka user whoami``), or "" when unreadable."""
    try:
        whoami = weka_json(host, "user whoami", token_path)
    except RuntimeError:
        return ""
    entry = whoami[0] if isinstance(whoami, list) and whoami else whoami
    return str(entry.get("role", "")) if isinstance(entry, dict) else ""


def run_mount_points(host: Host, run_id: str) -> list[str]:
    """Return the run's mount points on the BM: mounted ones and leftover directories."""
    mounted = [m["target"] for m in wekafs_mounts(host) if is_run_mount_point(m["target"], run_id)]
    _, stdout, _ = remote(host, f"ls -d /mnt/isv-{run_id} /mnt/isv-{run_id}-* 2>/dev/null")
    leftover = [line.strip() for line in stdout.splitlines() if is_run_mount_point(line.strip(), run_id)]
    return sorted(set(mounted) | set(leftover))


def release_bm(host: Host, paths: list[str], *, token_paths: Sequence[str] = ()) -> tuple[list[str], list[str]]:
    """Unmount ``paths`` (removing their directories) and remove ``token_paths``; return (released, failed).

    ``token_paths`` must each be a suite-owned ``isv-*.json`` token
    (``remove_token`` refuses anything else) - never the tenant's own token.
    """
    released: list[str] = []
    failed: list[str] = []
    for path in paths:
        try:
            was_mounted = unmount(host, path)
            released.append(f"mount:{path}" if was_mounted else f"mount-point:{path}")
        except Exception as e:
            failed.append(f"mount:{path}: {redact(e)}")
    for token_path in token_paths:
        try:
            if remove_token(host, token_path):
                released.append(f"token:{token_path}")
        except Exception as e:
            failed.append(f"token:{token_path}: {redact(e)}")
    return released, failed
