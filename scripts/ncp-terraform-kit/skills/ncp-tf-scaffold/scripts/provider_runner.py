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
"""The provider's entry point: one command for each mode of its generated tests."""

from __future__ import annotations

from pathlib import Path

import go_test_summary
import runner_parts
from runner_parts import RUNNER_NAME, RUNNER_SUBDIR

RUNNER_TEMPLATE = r"""#!/bin/sh
# Run the generated tests. One mode per command, and the only setup is a file
# you copy.
#
#   cp .env.example .env
#   ./__SELF__ offline
#   ./__SELF__ live -run 'TestAcc<Type>_lifecycle'
#   ./__SELF__ build
#
# An offline run needs no account and no credentials. This script builds the
# provider into a temporary filesystem mirror, writes the CLI configuration
# that points at it, runs every workspace, and removes both afterwards. Nothing
# is left in the tree, and nothing has to be written by hand.
#
# Generated with the cases. An edit here is lost at the next render.
set -eu

SOURCE='__SOURCE__'
VERSION='__VERSION__'
MIRROR='__MIRROR__'
WORKSPACES='__WORKSPACES__'
SWEEPER='__SWEEPER__'

self=$0
root=$(CDPATH= cd -- "$(dirname -- "$0")/__UP__" && pwd)
# Not resolved here: `build` needs only the provider's source.
workspaces="$root/$WORKSPACES"
provider_type=${SOURCE##*/}
host=${SOURCE%%/*}

usage() {
  cat >&2 <<USAGE
usage: $self offline [--json FILE] [WORKSPACE...]
       $self live    [--json FILE] [go test argument...]
       $self build

  offline  plans every workspace with no call to the NCP API, and asserts
           the plan. It needs no account, no credentials and no project.
           Naming workspaces runs those alone.
  live     runs the acceptance tests against a real account, and creates and
           destroys real resources.
  build    builds the provider into the filesystem mirror that the layer
           modules and the blueprint read: TFKIT_PROVIDER_MIRROR, or
           $MIRROR when that is unset, relative to $root.

  --json FILE  also write the machine-readable result stream to FILE. That
               file is what \`ncp-tf-verify-report record\` reads.

Values come from $root/.env. Copy .env.example to start one; that file says
what each value names.
USAGE
  exit 2
}

mode=${1:-}
case $mode in
  offline | live | build) shift ;;
  *) usage ;;
esac

__JSON_FLAG__

__LOAD_ENV__

__SUMMARISE__

# `go test -json` writes no readable output at all, so a live run that takes
# minutes shows nothing while it runs. Capture the readable stream instead and
# convert it, which streams and records from one run.
run_tests() {
  capture=$(mktemp "${TMPDIR:-/tmp}/tfkit-output.XXXXXX")
  code=$(mktemp "${TMPDIR:-/tmp}/tfkit-status.XXXXXX")
  trap 'rm -f "$capture" "$code"' EXIT INT TERM
  # `if` rather than a bare call: errexit would end the group at a failing test
  # run, before the line that records what it exited with.
  { if "$@" 2>&1; then echo 0 >"$code"; else echo $? >"$code"; fi; } | tee "$capture"
  test_status=$(cat "$code")
  if [ -n "$json" ]; then
    go tool test2json -t <"$capture" >>"$json"
  fi
  summarise "$capture"
  rm -f "$capture" "$code"
  trap - EXIT INT TERM
  return "$test_status"
}

__MIRROR_FUNCTIONS__

# Build the provider into the filesystem mirror at $1, laid out as Terraform
# looks for it: source address, version, platform.
build_into() {
  # Which package builds the provider is the provider's own choice, so it is
  # asked for rather than assumed.
  # A command substitution swallows a failure, and go failing to list at all
  # would then read as a tree with no main package in it.
  listed=$(cd "$root" && go list -f '{{if eq .Name "main"}}{{.ImportPath}}{{end}}' ./...) ||
    { echo "$self: go could not list the packages under $root" >&2; exit 2; }
  main=$(printf '%s\n' "$listed" | sed '/^$/d')
  case $main in
    '')
      echo "$self: no main package under $root, so there is nothing to build" >&2
      exit 2
      ;;
    *'
'*)
      echo "$self: more than one main package under $root:" >&2
      echo "$main" >&2
      exit 2
      ;;
  esac

  platform="$(go env GOOS)_$(go env GOARCH)"
  binary="$1/$SOURCE/$VERSION/$platform/terraform-provider-${provider_type}_v$VERSION"
  mkdir -p "$(dirname "$binary")"
  (cd "$root" && go build -o "$binary" "$main")
}

if [ "$mode" = build ]; then
  [ $# -eq 0 ] && [ -z "$json" ] || usage
  need go
  provider_mirror
  build_into "$mirror"
  echo "$self: built $SOURCE $VERSION into $mirror" >&2
  exit 0
fi

if [ "$mode" = offline ]; then
  need go
  need gofmt
  need terraform

  # Formatting is the cheapest check there is, and the only one that finds a
  # hand edit to a generated file before the diff does.
  unformatted=$(cd "$root" && gofmt -l .)
  if [ -n "$unformatted" ]; then
    echo "$self: gofmt would rewrite:" >&2
    printf '%s\n' "$unformatted" >&2
    exit 1
  fi

  # The rendered acceptance tests are Go, and nothing else here compiles them:
  # they are built only by a live run, which needs an account. A render that
  # does not compile is then found by whoever first has credentials, which is
  # the most expensive reader to tell. `vet` typechecks test files, `build`
  # does not.
  (cd "$root" && go vet ./...)

  # Which linter and which rules is the repository's decision, and the config
  # file is how it is made: present, the linter runs and the tool is required;
  # absent, it does not, and the run says so rather than leaving a reader to
  # assume one or the other. A check that skips itself in silence is the one
  # that gets trusted while doing nothing.
  if [ -f "$root/.golangci.yml" ] || [ -f "$root/.golangci.yaml" ]; then
    need golangci-lint
    (cd "$root" && golangci-lint run ./...)
  else
    echo "$self: no .golangci.yml under $root, so nothing is linted" >&2
  fi

  [ -z "$json" ] || : >"$json"

  # The provider's own unit tests. They are not the generated ones and no filter
  # should stand for both: selecting the request checks by name across every
  # package runs the generated tests and skips every test beside the code,
  # which is where a provider defect is caught cheapest.
  #
  # Every package except the one holding the generated tests, because those
  # refuse without a run id and a filter is not how that is said.
  generated=$(cd "$workspaces" && go list .) ||
    { echo "$self: go could not list the generated test package" >&2; exit 2; }
  units=$(cd "$root" && go list ./...) ||
    { echo "$self: go could not list the packages under $root" >&2; exit 2; }
  units=$(printf '%s\n' "$units" | grep -vx "$generated" || true)
  unit_status=0
  if [ -n "$units" ]; then
    # shellcheck disable=SC2086
    (cd "$root" && run_tests go test -v -count=1 $units) || unit_status=$?
    [ "$unit_status" -eq 0 ] || exit "$unit_status"
  fi

  # The sweeper is a Go module of its own, so `./...` above reaches none of it.
  # Its tests hold the selection, which an adapter change can break.
  if [ -n "$SWEEPER" ]; then
    (cd "$root/$SWEEPER" && go vet ./...)
    for config in "$root/.golangci.yml" "$root/.golangci.yaml"; do
      [ -f "$config" ] || continue
      (cd "$root/$SWEEPER" && golangci-lint run --config "$config" ./...)
      break
    done
    sweeper_status=0
    # The live check creates and deletes resources when TFKIT_SWEEPER_LIVE is
    # true, and .env can set it. This mode sends nothing to the NCP.
    (cd "$root/$SWEEPER" && unset TFKIT_SWEEPER_LIVE &&
      run_tests go test -v -count=1 ./...) || sweeper_status=$?
    [ "$sweeper_status" -eq 0 ] || exit "$sweeper_status"
  fi

  # What the provider would send, before what Terraform planned. These answer
  # every request with a refusal, so they need no account and no recording, and
  # they see the one thing a plan cannot: a value that reached the wrong field.
  request_status=0
  (cd "$root" && run_tests go test -v -count=1 -run '^TestRequest' "$generated") || request_status=$?
  [ "$request_status" -eq 0 ] || exit "$request_status"

  # The whole lifecycle against what the API returned once, so the read, update
  # and delete requests are asserted as the create is. Replay needs recordings,
  # and a run without them says so: an absent tier read as a passed one is how
  # a defect in the update reaches the first live run.
  # Replay waits between polls as the provider does live, so one package's
  # replays outlast the 10 minutes that `go test` gives all of them together.
  if grep -l '^func TestReplay' "$workspaces"/replay_*_test.go >/dev/null 2>&1; then
    replay_status=0
    (cd "$root" && run_tests go test -v -count=1 -timeout 60m -run '^TestReplay' "$generated") || replay_status=$?
    [ "$replay_status" -eq 0 ] || exit "$replay_status"
  else
    echo "$self: no replay test under $workspaces, so replay did not run" >&2
  fi

  # The workspaces name the provider by its source address, so they reach this
  # build only through a mirror. A temporary one, so an offline run leaves
  # the mirror that `build` writes as it was.
  make_tmp
  build_into "$tmp/mirror"
  point_terraform_at "$tmp/mirror"

  status=0
  ran=0

  run_workspace() {
    dir=$1
    ls "$dir"/*.tftest.hcl >/dev/null 2>&1 || return 0
    ran=$((ran + 1))
    echo "==> ${dir##*/}" >&2
    # The lock records the checksum of a provider this command rebuilds from the
    # source beside it, so it can only ever pin the previous run's binary. Left
    # alone, the first run after any provider change refuses with a checksum
    # error naming neither the cause nor the cure. Terraform writes this file
    # and nothing commits it.
    rm -f "$dir/.terraform.lock.hcl"
    if ! (cd "$dir" && terraform init -input=false >/dev/null); then
      status=1
      return 0
    fi
    if [ -n "$json" ]; then
      (cd "$dir" && terraform test -json) >>"$json" || status=1
    else
      (cd "$dir" && terraform test) || status=1
    fi
  }

  if [ $# -gt 0 ]; then
    for name in "$@"; do
      [ -d "$workspaces/$name" ] ||
        { echo "$self: $workspaces holds no workspace called $name" >&2; exit 2; }
      run_workspace "$workspaces/$name"
    done
  else
    for dir in "$workspaces"/*/; do
      [ -d "$dir" ] || continue
      run_workspace "${dir%/}"
    done
  fi

  [ "$ran" -gt 0 ] ||
    { echo "$self: no workspace under $workspaces holds a test" >&2; exit 2; }
  exit $status
fi

__RUN_ID_FUNCTIONS__
choose_run_id

# The second guard stays where it can name the values one case needs. A case
# selected by -run refuses with its own list; a case not selected needs
# nothing.
need go
TF_ACC=1
export TF_ACC
cd "$workspaces"
[ -z "$json" ] || : >"$json"
# `go test` stops the whole test binary after 10 minutes when it has no
# -timeout. The limit is for all the tests of the package together, not for one
# test, and one lifecycle can take longer than 10 minutes. A kill before a
# destroy step leaves what the test created in the account, so the live limit
# leaves room for every lifecycle in the package. A -timeout argument from the
# operator replaces it.
LIVE_TIMEOUT=180m
for argument in "$@"; do
  case $argument in
    -timeout | -timeout=*) LIVE_TIMEOUT= ;;
  esac
done
[ -z "$LIVE_TIMEOUT" ] || set -- -timeout "$LIVE_TIMEOUT" "$@"
live_status=0
run_tests go test -v "$@" || live_status=$?
say_run_id
list_leftovers
exit "$live_status"
"""


def render_runner(*, workspaces: str, source: str, version: str, mirror: str, sweeper: str = "") -> str:
    """The one command each mode takes, as a script the artifact carries.

    Five acts stood between a clone and a green offline run: build the mirror,
    author a CLI configuration that is in no tree, point Terraform at it, and
    initialise and run each workspace in turn. Every one of them is mechanical,
    and the last two are two invocations per workspace. A suite that takes a
    paragraph to start is run less often than one that takes a word.

    Nothing here is asked of the operator that the artifact cannot answer: the
    package that builds the provider comes from `go list`, the mirror layout
    from the provider's own source address, and the workspaces from where they
    were written. `mirror` is where `build` writes, relative to the provider's
    directory.
    """
    return runner_parts.filled(
        RUNNER_TEMPLATE.replace("__SUMMARISE__", go_test_summary.SUMMARISE),
        SELF=f"{RUNNER_SUBDIR}/{RUNNER_NAME}",
        UP="/".join([".."] * len(Path(RUNNER_SUBDIR).parts)),
        WORKSPACES=workspaces,
        SOURCE=source,
        VERSION=version,
        MIRROR=mirror,
        SWEEPER=sweeper,
    )
