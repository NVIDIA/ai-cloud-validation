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

"""Record which skill produced a coverage artifact, the way a code generator does.

`protoc-gen-go` stamps its version into every file it writes, and that works
because a released generator version is immutable. This skill's `version:` is
not: it does not have to change when behaviour changes. A version alone says
nothing about which behaviour wrote an artifact.

So the stamp carries a **content digest** as well. It changes whenever any file
the skill ships changes, which is the property that makes it useful: an artifact
states the skill that wrote it, and `check` compares that against the skill now
doing the checking.

The failure this exists to catch is real and already happened. A GCP report was
generated against an installed copy that predated two gates, its output looked
clean, and nothing in the artifact or the gate said otherwise -- it took a
directory diff to notice. A digest mismatch is one line of output instead.

A mismatch **reports rather than fails**. Re-checking an archived artifact
against a newer skill is a normal thing to do, and failing would make every
backup uncheckable.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent

#: Everything the skill ships that can change its output. `__pycache__` and
#: editor leavings are excluded: they are not the skill, and including them
#: would make the digest depend on whether anyone had run it yet.
_IGNORED_SUFFIXES = frozenset({".pyc", ".pyo"})
_IGNORED_NAMES = frozenset({".DS_Store"})
_IGNORED_DIRS = frozenset({"__pycache__", "evals"})

_VERSION = re.compile(r"^version:\s*(?P<value>\S+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Stamp:
    """Which skill produced an artifact."""

    skill: str
    version: str
    digest: str
    generated: str

    def as_block(self) -> str:
        return (
            "generator:\n"
            f"  skill: {self.skill}\n"
            f"  version: {self.version}\n"
            f"  digest: {self.digest}\n"
            f"  generated: {self.generated}\n"
        )


def shipped_files(root: Path = SKILL_ROOT) -> list[Path]:
    """Every file the skill ships, in a stable order."""
    found = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix not in _IGNORED_SUFFIXES
        and path.name not in _IGNORED_NAMES
        and not _IGNORED_DIRS.intersection(path.relative_to(root).parts)
    ]
    return sorted(found, key=lambda path: path.relative_to(root).as_posix())


def digest(root: Path = SKILL_ROOT) -> str:
    """A content digest over the whole skill.

    Paths are hashed relative to the skill root, so an installed copy and the
    source tree it came from produce the same digest. That is the point: the
    question is whether the bytes match, not where they sit.
    """
    running = hashlib.sha256()
    for path in shipped_files(root):
        running.update(path.relative_to(root).as_posix().encode("utf-8"))
        running.update(b"\0")
        running.update(path.read_bytes())
        running.update(b"\0")
    return f"sha256:{running.hexdigest()}"


def version(root: Path = SKILL_ROOT) -> str:
    """The skill's declared version, or `unknown` when it declares none."""
    skill_md = root / "SKILL.md"
    if not skill_md.is_file():
        return "unknown"
    match = _VERSION.search(skill_md.read_text(encoding="utf-8"))
    return match.group("value") if match else "unknown"


def current(root: Path = SKILL_ROOT, today: date | None = None) -> Stamp:
    return Stamp(
        skill=root.name,
        version=version(root),
        digest=digest(root),
        generated=(today or date.today()).isoformat(),
    )


def recorded(coverage: dict) -> Stamp | None:
    """The stamp an artifact carries, or None when it carries none."""
    block = coverage.get("generator")
    if not isinstance(block, dict):
        return None
    return Stamp(
        skill=str(block.get("skill", "")),
        version=str(block.get("version", "")),
        digest=str(block.get("digest", "")),
        generated=str(block.get("generated", "")),
    )


def mismatch(coverage: dict, root: Path = SKILL_ROOT) -> str | None:
    """Why the recording skill differs from this one, or None when it does not."""
    stamp = recorded(coverage)
    if stamp is None:
        return (
            "this artifact carries no `generator` stamp, so which skill wrote it "
            "is unknown; run `stamp` after generating it"
        )
    here = digest(root)
    if stamp.digest != here:
        return (
            f"generated by a different skill than the one checking it "
            f"(recorded {_short(stamp.digest)} v{stamp.version} on "
            f"{stamp.generated}; running {_short(here)}). Re-generate before "
            f"trusting this result, or accept it as a check of an archived "
            f"artifact"
        )
    return None


def _short(value: str) -> str:
    body = value.split(":", 1)[-1]
    return f"{value.split(':', 1)[0]}:{body[:12]}" if body else value


_BLOCK = re.compile(r"^generator:\n(?:(?:[ \t]+\S.*)?\n)*", re.MULTILINE)
_FIRST_LIST_KEY = re.compile(r"^(mappings|module_plan|caveats):", re.MULTILINE)


def apply_stamp(text: str, stamp: Stamp) -> str:
    """Insert or replace the `generator` block, leaving everything else alone.

    Written textually rather than by a YAML round-trip: the artifact carries a
    licence header, comments, and folded scalars that a dump would discard.
    """
    block = stamp.as_block()
    if _BLOCK.search(text):
        return _BLOCK.sub(block, text, count=1)
    anchor = _FIRST_LIST_KEY.search(text)
    if anchor is None:
        return text.rstrip("\n") + "\n" + block
    return text[: anchor.start()] + block + text[anchor.start() :]
