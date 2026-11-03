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
"""The shell both entry points carry: the provider's and each module's.

Each directory gets its own copy, so a module runs with nothing from another
directory but the provider build. The text is written once here, so the two
copies cannot drift.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

#: Where an entry point lands inside the directory it runs. The kit decides the
#: shape inside that directory and nothing above it, so this is a name, not a
#: path.
RUNNER_SUBDIR = "tools"
RUNNER_NAME = "run-tests.sh"
#: What overrides the mirror path a render wrote into an entry point.
MIRROR_VARIABLE = "TFKIT_PROVIDER_MIRROR"

JSON_FLAG = r"""json=
if [ "${1:-}" = --json ]; then
  [ $# -ge 2 ] || usage
  json=$2
  shift 2
  # The live mode changes directory before it writes, so a relative path would
  # name two different files depending on the mode.
  case $json in
    /*) ;;
    *) json="$PWD/$json" ;;
  esac
fi"""

LOAD_ENV = r"""# Every value a run needs comes from one file, so that starting a run is a copy
# and an edit rather than a paragraph of exports. A value already in the
# environment wins, as with every other tool that reads a .env, so one run can
# override the file without an edit. A missing file is not an error: an
# offline run reads none of them.
load_env() {
  [ -f "$1" ] || return 0
  # The file holds credentials. Inside a git work tree, git must ignore it, or
  # the next `git add` stages them. Outside a work tree, nothing commits it.
  env_dir=$(dirname -- "$1")
  if command -v git >/dev/null 2>&1 &&
    git -C "$env_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1 &&
    ! git -C "$env_dir" check-ignore -q -- "$(basename -- "$1")"; then
    echo "$self: git does not ignore $1, which holds credentials; add .env to .gitignore, then run again" >&2
    exit 2
  fi
  # Drop each line whose name the environment already has, then read the rest
  # in this shell, so a later line that reads that name reads the
  # environment's value. The rest stays in a variable: a temporary file would
  # keep the credentials when a line fails and the shell exits.
  filtered=$(awk '{
    line = $0
    sub(/^[ \t]*export[ \t]+/, "", line)
    if (match(line, /^[A-Za-z_][A-Za-z0-9_]*=/) && (substr(line, 1, RLENGTH - 1) in ENVIRON)) next
    print
  }' "$1")
  set -a
  eval "$filtered"
  set +a
}
load_env "$root/.env"

need() {
  command -v "$1" >/dev/null 2>&1 ||
    { echo "$self: $1 is not on PATH" >&2; exit 2; }
}"""

MIRROR = r"""# A temporary directory for what a run writes, removed when it ends.
# A template, because BSD mktemp reads TMPDIR only when it is given one, and a
# machine whose per-user temporary directory is closed then fails here rather
# than where the operator could redirect it.
make_tmp() {
  tmp=$(mktemp -d "${TMPDIR:-/tmp}/tfkit-tests.XXXXXX")
  trap 'rm -rf "$tmp"' EXIT
  # A signal trap replaces the default action, so it must exit itself: a
  # cancelled live run must not go on with its configuration removed.
  trap 'exit 130' INT
  trap 'exit 143' TERM
}

# A value as an HCL string literal: quoted, with each character HCL reads as
# syntax escaped, so a path or a value reaches Terraform as the text it was.
hcl_quote() {
  # Character by character, because sed works a line at a time and a value may
  # hold a newline. The newline printf adds makes a trailing one in the value a
  # record of its own, so it survives as well.
  printf '%s\n' "$1" | awk '
    BEGIN { ORS = ""; print "\"" }
    {
      if (NR > 1) print "\\n"
      n = length($0)
      for (i = 1; i <= n; i++) {
        c = substr($0, i, 1)
        if (c == "\\" || c == "\"") print "\\" c
        else if ((c == "$" || c == "%") && substr($0, i + 1, 1) == "{") print c c
        else print c
      }
    }
    END { print "\"" }'
}

# The mirror the provider is built into and read from: TFKIT_PROVIDER_MIRROR
# when it is set, else the path the render was given. A relative path is
# relative to this directory, so it holds wherever the repository is cloned.
provider_mirror() {
  mirror=${TFKIT_PROVIDER_MIRROR:-$MIRROR}
  case $mirror in
    /*) ;;
    *) mirror="$root/$mirror" ;;
  esac
}

# Terraform reaches a provider that no registry holds only through a filesystem
# mirror, and only a CLI configuration names one. It is written to the
# temporary directory, so no configuration of the operator's is read or
# changed. The exclude names the source host, or Terraform asks the public
# registry for it and fails.
point_terraform_at() {
  TF_CLI_CONFIG_FILE="$tmp/cli.tfrc"
  export TF_CLI_CONFIG_FILE
  cat >"$TF_CLI_CONFIG_FILE" <<TFRC
provider_installation {
  filesystem_mirror { path = $(hcl_quote "$1") }
  direct { exclude = ["$host/*/*"] }
}
TFRC
}"""

RUN_ID = r"""# A run id names what does not exist yet, so it only has to be unique and
# visible: one is generated when none is set, and printed when the run starts
# and again after its tests. A fixed id in .env would make the second run
# collide with the first. A TFKIT_VAR_ value names what must already exist, so
# a missing one stays a refusal; a generated value there would build against
# the wrong parent.
#
# Lowercase letters and digits, 13 characters: an API that refuses capitals,
# hyphens or a long name still takes it. All eight characters after `tfkit`
# are random, so two runs share an id with a chance of 1 in 36^8: the sweeper
# finds a run's resources by its id, and two runs with one id list, and after
# approval delete, each other's. A set id in another form is refused: each
# name's room was measured for this one.
#
# The letters are listed: a range such as a-z follows the locale's order, and
# some locales sort capitals inside it. `tr` reads bytes only in the C locale.
choose_run_id() {
  run_id_letters=0123456789abcdefghijklmnopqrstuvwxyz
  if [ -z "${TFKIT_RUN_ID:-}" ]; then
    TFKIT_RUN_ID=tfkit$(LC_ALL=C tr -dc "$run_id_letters" </dev/urandom | head -c 8)
    echo "$self: TFKIT_RUN_ID is unset, so this run is $TFKIT_RUN_ID" >&2
  else
    c="[$run_id_letters]"
    case "$TFKIT_RUN_ID" in
      tfkit$c$c$c$c$c$c$c$c) ;;
      *)
        echo "$self: TFKIT_RUN_ID is $TFKIT_RUN_ID; a run id is tfkit and 8 lowercase letters or digits, because each name's room was measured for that. Unset it, and this script makes one" >&2
        exit 2
        ;;
    esac
    echo "$self: this run is $TFKIT_RUN_ID" >&2
  fi
  export TFKIT_RUN_ID
}

# What a run leaves behind is found by its id, so the id is printed after the
# tests, before the sweeper's listing of what they left.
say_run_id() {
  echo "$self: run $TFKIT_RUN_ID. Everything it created carries that id in its name or labels; find what it left by it." >&2
}

# A killed `go test` returns to this script, so the script can still list what
# the run left: whoever runs a test sees what it leaves. The listing deletes
# nothing, and it keeps the run's exit status.
list_leftovers() {
  if [ -z "$SWEEPER" ]; then
    echo "$self: no sweeper is wired, so what run $TFKIT_RUN_ID left is not listed" >&2
    return 0
  fi
  echo "$self: what run $TFKIT_RUN_ID left, as the sweeper lists it:" >&2
  (cd "$root/$SWEEPER" && go run . --run-id "$TFKIT_RUN_ID") ||
    echo "$self: the sweeper's listing did not finish, or did not list each type; read its lines above, and list run $TFKIT_RUN_ID with it by hand" >&2
}"""

#: The run id's lines in `.env.example`, the same in every directory.
RUN_ID_LINES = [
    "# Names every resource one run creates, so that a later sweep finds this",
    "# run's resources and no other. Unset, a live run generates one and prints",
    "# it. A value set in the environment wins over this file.",
    "TFKIT_RUN_ID=",
    "",
]


#: The comment width of the rest of `.env.example`.
WIDTH = 76


def mirror_lines(default: str, role: str) -> list[str]:
    """The mirror's lines in `.env.example`; `role` says what this directory does with it."""
    text = (
        f"The filesystem mirror that holds the provider build. {role} "
        f"Unset, it is {default}, relative to this directory."
    )
    return [f"# {line}" for line in textwrap.wrap(text, WIDTH)] + [f"{MIRROR_VARIABLE}=", ""]


def relative_sweeper(sweeper: str | None, directory: Path) -> str:
    """The sweeper's directory from `directory`, or nothing when no sweeper is wired."""
    return relative_mirror(sweeper, directory) if sweeper else ""


def relative_mirror(mirror: str, directory: Path) -> str:
    """The mirror as a path from `directory`, so a clone anywhere still finds it."""
    return Path(os.path.relpath(Path(mirror).absolute(), directory.absolute())).as_posix()


def single_quoted(value: str) -> str:
    """`value` for the inside of a shell single-quoted string."""
    return value.replace("'", "'\\''")


def filled(template: str, **values: str) -> str:
    """`template` with each `__NAME__` replaced: the shared parts, then `values`.

    Each value goes inside single quotes in the template, so it is escaped for
    them: a path the operator chose may hold an apostrophe.
    """
    parts = {"JSON_FLAG": JSON_FLAG, "LOAD_ENV": LOAD_ENV, "MIRROR_FUNCTIONS": MIRROR, "RUN_ID_FUNCTIONS": RUN_ID}
    for name, text in parts.items():
        template = template.replace(f"__{name}__", text)
    for name, text in values.items():
        template = template.replace(f"__{name}__", single_quoted(text))
    return template
