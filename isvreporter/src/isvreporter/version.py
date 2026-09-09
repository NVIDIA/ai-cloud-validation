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

"""Version and build provenance for all workspace packages.

The version comes from installed package metadata, which importlib.metadata
reads from a wheel, an editable install, or an air-gapped tree after ``uv
sync``. It is reported exactly as it stands, with nothing appended, so every
consumer can read it as a plain release number.

The version cannot say whether the checkout has moved past that release: a tree
several commits past ``v0.9.0`` still reports ``0.9.0`` while running checks
that release never had. That is a separate fact, and it travels in its own
field rather than being folded into the version string. Two such facts exist
and are observed independently - the source reference reported here, and the
catalog digest reported by ``isvtest.catalog``.

Source provenance is optional by design. Partners install from wheels and copy
the source tree onto air-gapped clusters, so there is often no checkout to
describe and no network to ask. Every function here answers None in that case,
and None always means "nothing to go on" - never "no", and never a default.
Nothing downstream may require a source reference to exist.
"""

import logging
import os
import re
import subprocess
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)

# --long so a tagged commit still reports its distance (0) rather than a bare
# tag, which keeps one output shape to parse. --match so a tag that is not a
# release marker cannot become the version.
_DESCRIBE_COMMAND = (
    "git",
    "describe",
    "--tags",
    "--long",
    "--dirty",
    "--match",
    "v*",
)

_DESCRIBE_PATTERN = re.compile(r"^v(?P<tag>.+)-(?P<distance>\d+)-g(?P<commit>[0-9a-f]+)(?P<dirty>-dirty)?$")

# A hung git call must not hold up a test run that is otherwise ready to report.
_DESCRIBE_TIMEOUT_SECONDS = 5

# Lets a pipeline that builds an artifact elsewhere pass down provenance the
# running copy cannot rediscover.
BUILD_REF_ENV = "ISVTEST_BUILD_REF"

# Matches the service column that stores it; truncated rather than dropped so an
# over-long value still carries its leading, most identifying part.
_BUILD_REF_MAX_LENGTH = 128

# Where this module sits in the workspace tree. A repository that does not hold
# the file at this path is somebody else's checkout, whatever its tags say.
_SOURCE_RELATIVE_PATH = Path("isvreporter/src/isvreporter/version.py")


def _repository_root() -> Path | None:
    """Return the git checkout this module lives in, or None when installed.

    Only this workspace's own tree counts. An environment created inside another
    repository is ordinary - ``uv sync`` puts ``.venv`` under whatever tree it
    runs from - and a ``v*`` tag there would describe cleanly enough to pass for
    ours, reporting a partner's version for our checks. Matching this file's own
    path within the tree is what rules that out. Editable installs still count,
    since their files stay in the source tree.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        # A worktree records .git as a file, so existence is the test, not is_dir.
        if (parent / ".git").exists():
            return parent if here == parent / _SOURCE_RELATIVE_PATH else None
    return None


@lru_cache(maxsize=1)
def describe_checkout() -> str | None:
    """Return this checkout's ``git describe`` output, or None when unavailable.

    The workspace releases every package off a single repository tag, so one
    description covers them all. It is passed on exactly as git prints it
    (``v0.9.0-9-g08339c7``, with ``-dirty`` for uncommitted changes), so callers
    receive the observation rather than this module's reading of it.

    Cached because the answer cannot change within a process, and each package's
    version lookup would otherwise start its own git.
    """
    root = _repository_root()
    if root is None:
        return None

    try:
        completed = subprocess.run(
            _DESCRIBE_COMMAND,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=_DESCRIBE_TIMEOUT_SECONDS,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # No git binary, no tags, a shallow clone: none is worth failing a run
        # for, and none leaves us worse off than a partner running air-gapped.
        logger.debug("Could not describe the checkout at %s: %s", root, exc)
        return None

    described = completed.stdout.strip()
    if _DESCRIBE_PATTERN.match(described) is None:
        logger.debug("Unexpected git describe output: %r", described)
        return None
    return described


def build_ref() -> str | None:
    """Return where this build came from, or None when nothing can say.

    Prefers ``ISVTEST_BUILD_REF``, which lets a pipeline that builds elsewhere
    pass down provenance the running copy cannot rediscover. Falls back to
    describing the checkout when there is one.

    The environment value is free text, checked only for length: an operator
    supplying their own reference should not have to imitate ``git describe``
    output to be recorded.
    """
    supplied = os.environ.get(BUILD_REF_ENV, "").strip()
    if supplied:
        return supplied[:_BUILD_REF_MAX_LENGTH]
    return describe_checkout()


def get_version(package_name: str) -> str:
    """Return the version of *package_name*, or ``"dev"`` if unavailable.

    The installed metadata, verbatim - no git, no suffix. Build provenance
    belongs in :func:`build_ref` and the catalog digest, not folded in here.

    Args:
        package_name: Distribution name (e.g. ``"isvreporter"``).

    Returns:
        Version string such as ``"1.2.3"``.
    """
    try:
        return version(package_name)
    except PackageNotFoundError:
        return "dev"


class RunIdentity(NamedTuple):
    """What a run reports about the build that produced its results.

    Every field may be None. Nothing here is invented to fill a gap; the service
    records an absence as unknown.

    Attributes:
        isv_test_version: Release number the results are reported under.
        catalog_digest: Digest of the catalog the build executed.
        build_ref: Source reference the build recorded.
    """

    isv_test_version: str | None
    catalog_digest: str | None
    build_ref: str | None


def run_identity(
    catalog_document: dict[str, Any] | None,
    local_version: str | None,
) -> RunIdentity:
    """Read a run's identity from the catalog artifact its build produced.

    Reporting can run on a different machine than the one that executed the
    tests, so the artifact wins and *local_version* only covers a run that
    produced no artifact at all. The digest and the reference are taken exactly
    as the artifact gives them, absent ones included: substituting local values
    would credit a split or remote run to the reporting machine's checkout.

    Values are read from the artifact rather than recomputed, so the digest the
    service receives is the one written to _output/test_catalog.json and printed
    during the run, not a second number that ought to agree with it.

    Args:
        catalog_document: Parsed test_catalog.json, or None if there was none.
        local_version: Version to fall back on when the artifact names none.

    Returns:
        The version, catalog digest and source reference to report.
    """
    document = catalog_document or {}
    return RunIdentity(
        isv_test_version=document.get("isvTestVersion") or local_version,
        catalog_digest=document.get("catalogDigest"),
        build_ref=document.get("isvTestBuildRef"),
    )


def parse_build_ref(ref: str | None) -> tuple[str, int, str, bool] | None:
    """Split a ``git describe`` reference into (tag, distance, commit, dirty).

    Returns None for anything it cannot read, including the operator-supplied
    free text :func:`build_ref` also accepts.

    Args:
        ref: A reference from :func:`build_ref`, or None.

    Returns:
        The parsed parts, or None when the reference carries no readable detail.
    """
    if ref is None:
        return None
    match = _DESCRIBE_PATTERN.match(ref.strip())
    if match is None:
        return None
    try:
        distance = int(match["distance"])
    except ValueError:
        # Python limits extremely long decimal conversions. Treat hostile or
        # corrupt input like every other unreadable reference: unverified.
        return None
    return match["tag"], distance, match["commit"], bool(match["dirty"])


def build_is_release(package_version: str, ref: str | None) -> bool | None:
    """Whether this build is the release its version names, per *ref*.

    True on a clean commit tagged with the reported version, False when the
    reference says otherwise, None when there is nothing to go on. None is the
    common case in the field and must not be read as either answer.

    A tag disagreeing with the installed metadata counts as False: the install
    is stale against the tree, so the checks that run are not the ones the
    reported version published.

    *ref* is a parameter rather than a call to :func:`build_ref`, so a caller
    holding a known-absent reference stays distinguishable from one that has not
    looked yet, and so this predicate never shells out to git on its own.

    Args:
        package_version: Version the build reports itself under.
        ref: Source reference to judge it against, or None.

    Returns:
        True, False, or None when the reference cannot answer.
    """
    parsed = parse_build_ref(ref)
    if parsed is None:
        return None
    tag, distance, _commit, dirty = parsed
    return distance == 0 and not dirty and tag == package_version.strip()
