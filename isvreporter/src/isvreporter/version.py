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

"""Version resolution and build provenance for all workspace packages.

The canonical version lives in each package's pyproject.toml. At runtime,
importlib.metadata reads it from installed package metadata (works in wheels,
editable installs, and airgapped environments after ``uv sync``). That number
is reported verbatim: it is the one thing about the build that is known
exactly, and every consumer is entitled to read it as a plain release number.

What it does not say is whether this checkout has moved past that release. A
tree several commits past ``v0.9.0`` still carries ``0.9.0`` while running
checks that release never had. That is a separate fact and it travels in its
own field, never folded into the version string.

Source and catalog identity are independent observations. This module reports
the optional source reference; ``isvtest.catalog`` reports the catalog digest.
When a git checkout is present, the reference identifies a clean tag, extra
commits, or local changes. Partners may copy the source tree onto air-gapped
clusters, where source provenance is simply unverified.
"""

import logging
import os
import re
import subprocess
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

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
# running copy cannot rediscover - the air-gapped case, where there is no
# checkout to describe and no network to ask.
BUILD_REF_ENV = "ISVTEST_BUILD_REF"

# Matches the service column that stores it; truncated rather than dropped so an
# over-long value still carries its leading, most identifying part.
_BUILD_REF_MAX_LENGTH = 128

# Where this module sits in the workspace tree. A repository that does not hold
# the file at this path is somebody else's checkout, whatever its tags say.
_SOURCE_RELATIVE_PATH = Path("isvreporter/src/isvreporter/version.py")


def _repository_root() -> Path | None:
    """Return the git checkout this module lives in, or None when installed.

    Only this workspace's own tree counts. Creating an environment inside
    another repository is ordinary - ``uv sync`` puts ``.venv`` under whatever
    tree it is run from, and ``--target`` installs anywhere at all - and a
    ``v*`` tag there describes cleanly enough to be taken for ours, which would
    report a partner's version for our checks. An editable install still
    counts, because its files stay in the source tree.
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

    The workspace releases its packages in lockstep off a single repository tag,
    so one description covers all of them. Reported verbatim
    (``v0.9.0-9-g08339c7``, with ``-dirty`` appended for uncommitted changes)
    rather than reshaped into a version: the recipient should receive the
    observation, not this module's interpretation of it.

    None is the ordinary answer, not an error. There is no checkout when the
    package was installed from a wheel, when a partner copied the source tree
    onto a cluster without ``.git``, or when the environment is air-gapped and
    shallow-cloned. Every such case reports source provenance as unverified.

    Cached: the answer cannot change within a process, and every package's
    version lookup would otherwise spawn its own git.
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

    Prefers ``ISVTEST_BUILD_REF``, so a pipeline that builds an artifact and
    ships it into an air-gapped cluster can pass down provenance it knows and
    the running copy cannot rediscover. Falls back to describing the checkout
    when there is one.

    The value is free text from the environment and is not validated beyond a
    length bound: an operator supplying their own reference should not have to
    match ``git describe`` output to be believed, and nothing downstream is
    permitted to depend on it.
    """
    supplied = os.environ.get(BUILD_REF_ENV, "").strip()
    if supplied:
        return supplied[:_BUILD_REF_MAX_LENGTH]
    return describe_checkout()


def get_version(package_name: str) -> str:
    """Return the version of *package_name*, or ``"dev"`` if unavailable.

    The installed package metadata, verbatim. Deliberately no git and no
    suffix: this is the base version, and build provenance belongs in
    :func:`build_ref` and the catalog digest rather than folded in here, where
    it would corrupt the one value every consumer can read as a plain release
    number.

    Args:
        package_name: Distribution name (e.g. ``"isvreporter"``).

    Returns:
        Version string such as ``"1.2.3"``.
    """
    try:
        return version(package_name)
    except PackageNotFoundError:
        return "dev"


def parse_build_ref(ref: str | None) -> tuple[str, int, str, bool] | None:
    """Split a ``git describe`` reference into (tag, distance, commit, dirty).

    Returns None for anything this cannot read, including the operator-supplied
    free text :func:`build_ref` also accepts. Callers must treat that as "no
    detail available" rather than as evidence of anything.
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

    Returns True on a clean commit tagged with the reported version, False when
    the reference demonstrably differs from it, and **None when there is nothing
    to go on** - no checkout, unreadable output, or operator-supplied free text.

    None is the common case in the field and must not be read as either answer.
    It is the honest report from a mechanism that only works where git does.

    A tag that disagrees with the installed metadata counts as not-a-release:
    the install is stale with respect to the tree, so the checks that run are
    not the ones the reported version published.

    *ref* is required rather than defaulting to :func:`build_ref`. A caller
    holding a known-absent reference and a caller that has not looked yet are
    different situations with different answers, and a predicate that quietly
    shells out to git would hide the one dependency this design exists to keep
    optional.
    """
    parsed = parse_build_ref(ref)
    if parsed is None:
        return None
    tag, distance, _commit, dirty = parsed
    return distance == 0 and not dirty and tag == package_version.strip()
