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
"""The entry point and `.env.example` of a layer module or the blueprint.

Each module directory runs its own tests, with its own `.env`, so it can move
to a repository of its own. The one thing it reads from outside is the
provider build, from a filesystem mirror the provider's `build` mode writes.
"""

from __future__ import annotations

import os
from pathlib import Path

import provider_settings
import runner_parts
import suite_values

SUITE_RUNNER_TEMPLATE = r"""#!/bin/sh
# Run this directory's own tests: a layer module's, or the blueprint's.
#
#   cp .env.example .env
#   ./__SELF__ offline
#   ./__SELF__ live
#
# Both read the provider from a filesystem mirror, which the provider's own
# `run-tests.sh build` writes. Nothing else is read from outside this
# directory.
#
# Generated with the cases. An edit here is lost at the next render.
set -eu

SOURCE='__SOURCE__'
VERSION='__VERSION__'
MIRROR='__MIRROR__'
TESTS='__TESTS__'
SWEEPER='__SWEEPER__'

self=$0
root=$(CDPATH= cd -- "$(dirname -- "$0")/__UP__" && pwd)
host=${SOURCE%%/*}

usage() {
  cat >&2 <<USAGE
usage: $self offline [--json FILE]
       $self live    [--json FILE]

  offline  plans this directory's configuration with no call to the NCP
           API, and asserts the plan. It needs no account and no credentials.
  live     applies this directory's configuration in a real account, asserts
           each output, and destroys what it created.

  --json FILE  also write the machine-readable result stream to FILE. That
               file is what \`ncp-tf-verify-report record\` reads.

Values come from $root/.env. Copy .env.example to start one; that file says
what each value names. The provider comes from TFKIT_PROVIDER_MIRROR, or
$MIRROR when that is unset, relative to $root.
USAGE
  exit 2
}

mode=${1:-}
case $mode in
  offline | live) shift ;;
  *) usage ;;
esac

__JSON_FLAG__

[ $# -eq 0 ] || usage

__LOAD_ENV__

__MIRROR_FUNCTIONS__

__RUN_ID_FUNCTIONS__

# The run id and every TFKIT_VAR_ value, as the variables a test file reads.
export_run_values() {
  TF_VAR_tfkit_run_id=$(hcl_quote "$TFKIT_RUN_ID")
  export TF_VAR_tfkit_run_id
  for name in $(env | sed -n 's/^TFKIT_VAR_\([A-Za-z0-9_]*\)=.*/\1/p'); do
    eval "value=\${TFKIT_VAR_$name}"
    lower=$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]')
    export "TF_VAR_tfkit_var_$lower=$(hcl_quote "$value")"
  done
}

# The TFKIT_VAR_ values these test files read and the run does not supply, all
# of them, so an operator finds the set in one run rather than one per run.
unset_run_values() {
  for reference in $(grep -oh 'var\.tfkit_var_[a-z0-9_]*' "$@" | sort -u); do
    name=$(printf '%s' "${reference#var.tfkit_var_}" | tr '[:lower:]' '[:upper:]')
    eval "value=\${TFKIT_VAR_$name:-}"
    [ -n "$value" ] || printf ' TFKIT_VAR_%s' "$name"
  done
}

need terraform

# One mode's files at a time. An offline file and a live one sit in the same
# directory, and `terraform test` runs every file it finds: an offline run
# without the filter would apply the live file.
tests=$root
[ "$TESTS" = . ] || tests="$root/$TESTS"
ls "$tests"/*_"$mode".tftest.hcl >/dev/null 2>&1 ||
  { echo "$self: $TESTS holds no $mode test. Render its case with --mode $mode." >&2; exit 2; }
# The terraform test arguments, as positional parameters, so a path with a
# space stays one argument.
set --
[ "$TESTS" = . ] || set -- "-test-directory=$TESTS"
for file in "$tests"/*_"$mode".tftest.hcl; do
  set -- "$@" "-filter=${file#"$root"/}"
done

provider_mirror
[ -d "$mirror/$SOURCE/$VERSION" ] || {
  echo "$self: $mirror holds no build of $SOURCE $VERSION. Build it with the provider's run-tests.sh build, or set TFKIT_PROVIDER_MIRROR in $root/.env." >&2
  exit 2
}

# Named all at once, and before init: a value found missing at apply costs the
# time of everything created before it.
if [ "$mode" = live ]; then
  missing=$(unset_run_values "$tests"/*_live.tftest.hcl)
  [ -z "$missing" ] ||
    { echo "$self: unset:$missing. $TESTS/RUN_VARIABLES.md says what each one names" >&2; exit 1; }
  choose_run_id
  export_run_values
fi

make_tmp
point_terraform_at "$mirror"

# The lock records the checksum of the provider build it first saw, and every
# build changes it. Left alone, the first run after a provider change refuses
# with a checksum error that names neither the cause nor the cure. Terraform
# writes this file and nothing commits it.
rm -f "$root/.terraform.lock.hcl"
(cd "$root" && terraform init -input=false </dev/null >/dev/null) ||
  { echo "$self: terraform init failed in $root" >&2; exit 1; }

echo "==> ${root##*/} $*" >&2
[ -z "$json" ] || : >"$json"
status=0
if [ -n "$json" ]; then
  (cd "$root" && terraform test "$@" -json </dev/null) >>"$json" || status=1
else
  (cd "$root" && terraform test "$@" </dev/null) || status=1
fi
if [ "$mode" != offline ]; then
  say_run_id
  list_leftovers
fi
exit "$status"
"""


def render(*, tests: str, source: str, version: str, mirror: str, sweeper: str = "") -> str:
    """The entry point of one module directory; `tests` and `mirror` are relative to it."""
    return runner_parts.filled(
        SUITE_RUNNER_TEMPLATE,
        SELF=f"{runner_parts.RUNNER_SUBDIR}/{runner_parts.RUNNER_NAME}",
        UP="/".join([".."] * len(Path(runner_parts.RUNNER_SUBDIR).parts)),
        TESTS=tests,
        SOURCE=source,
        VERSION=version,
        MIRROR=mirror,
        SWEEPER=sweeper,
    )


def render_env_example(tests: Path, tests_shown: str, mirror: str, provider_lines: list[str]) -> str:
    """The one file an operator copies before a run of this directory's tests.

    The run values come from the live test files on disk, so an offline render
    and a live render of the same module write the same file, whichever ran
    last.
    """
    lines = [
        "# What a run of this directory's tests supplies, as a file to copy.",
        "#",
        "#   cp .env.example .env",
        f"#   ./{runner_parts.RUNNER_SUBDIR}/{runner_parts.RUNNER_NAME} offline",
        "#",
        "# An offline run reads only the provider mirror. It needs no account and",
        "# no credentials.",
        "#",
        "# A live run applies this directory's configuration in a real account.",
        "# Each TFKIT_VAR_ value names something only the account knows, or a",
        "# resource that must exist before the run. A run without one stops before",
        "# it creates anything.",
        "#",
        "# Copy this file to .env, which git ignores. Keep real ids out of the",
        "# repository, and put no value in this file.",
        "#",
        "# Generated with the cases. An edit here changes nothing.",
        "",
        *runner_parts.RUN_ID_LINES,
        *runner_parts.mirror_lines(mirror, "The provider's own `run-tests.sh build` writes it."),
        *provider_lines,
        "# ---- what this directory's live test needs ----",
        "#",
    ]
    needed = suite_values.needed(tests)
    if needed:
        lines += [f"# {tests_shown}/RUN_VARIABLES.md says what each one names and where it goes.", ""]
        lines += [line for variable in needed for line in (f"{variable}=", "")]
    else:
        lines += ["# No live test here reads a supplied value.", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def write(
    directory: Path,
    tests: Path,
    *,
    source: str,
    version: str,
    mirror: str,
    export: Path,
    sweeper: str | None = None,
) -> list[str]:
    """Write the entry point and `.env.example` into `directory`, the module's own."""
    tests_shown = Path(os.path.relpath(tests, directory)).as_posix()
    shown_mirror = runner_parts.relative_mirror(mirror, directory)
    example = directory / ".env.example"
    example.write_text(
        render_env_example(tests, tests_shown, shown_mirror, provider_settings.section(export)),
        encoding="utf-8",
    )
    script = directory / runner_parts.RUNNER_SUBDIR / runner_parts.RUNNER_NAME
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        render(
            tests=tests_shown,
            source=source,
            version=version,
            mirror=shown_mirror,
            sweeper=runner_parts.relative_sweeper(sweeper, directory),
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return [str(example), str(script)]
