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
"""The run instructions of the provider's README, written with its entry point.

The render knows each fact that the section states: the entry point it wrote,
the workspaces, the mirror and the sweeper. The section holds those facts and
nothing about one run, so the same inputs give the same README. Text outside
the markers is the operator's and the agent's, and stays.
"""

from __future__ import annotations

from pathlib import Path

from runner_parts import RUNNER_NAME, RUNNER_SUBDIR

#: The first line of the section. A render replaces the text from here to END.
BEGIN = "<!-- BEGIN ncp-tf-scaffold: the render writes this section. Edit the cases, not this text. -->"
END = "<!-- END ncp-tf-scaffold -->"

#: Go's own default stops a package after 10 minutes, and the replay tier of
#: the entry point gives its package 60, because replay waits between polls.
REPLAY_TIMEOUT = "60m"


def _tiers(workspaces: str, sweeper: str) -> list[str]:
    runner = f"./{RUNNER_SUBDIR}/{RUNNER_NAME}"
    rows = [
        "| `gofmt -l .` | no Go file needs formatting | `gofmt -l <file>` |",
        "| `go vet ./...` | the provider and the rendered tests compile, and vet finds nothing |"
        " `go vet ./<package>` |",
        "| `golangci-lint run ./...`, only when `.golangci.yml` exists | the code passes the"
        " repository's linter | `golangci-lint run ./<package>` |",
        f"| the unit tests: each package except `{workspaces}` | the provider's own functions |"
        " `go test -count=1 -run '^<Test>$' ./<package>` |",
    ]
    if sweeper:
        rows.append(
            f"| the sweeper's tests, in `{sweeper}` | the sweeper selects only what one run"
            f" created | `cd {sweeper} && go test -count=1 -run '^<Test>$' ./...` |"
        )
    rows += [
        f"| the request checks | each resource's create request has the method, the path and"
        f" the body fields that the document requires. Each request gets a refusal, so they"
        f" need no account and no recording | `go test -count=1 -run"
        f" '^TestRequest<Type>_create$' ./{workspaces}` |",
        f"| the replay tests, only when a `replay_*_test.go` exists | the whole lifecycle"
        f" against the answers that the API gave once | `go test -count=1 -timeout"
        f" {REPLAY_TIMEOUT} -run '^TestReplay<Type>_lifecycle$' ./{workspaces}` |",
        f"| the workspaces | Terraform plans each case against this build, with no call to"
        f" the API | `{runner} offline <workspace>` |",
    ]
    return rows


def render_section(*, workspaces: str, mirror: str, sweeper: str = "") -> str:
    """The README section, from the paths that the entry point holds.

    `workspaces`, `mirror` and `sweeper` are relative to the provider's
    directory, as the entry point holds them.
    """
    runner = f"./{RUNNER_SUBDIR}/{RUNNER_NAME}"
    variables = f"{workspaces}/RUN_VARIABLES.md"
    lines = [
        BEGIN,
        "",
        "## Run the tests",
        "",
        "```sh",
        "cp .env.example .env",
        f"{runner} offline",
        f"{runner} live",
        f"{runner} build",
        "```",
        "",
        "An offline run needs no account, no credentials and no project. Copy"
        " `.env.example` without a change, and the offline run can pass. A live run creates"
        " and deletes real resources. `.env.example` says what each value names, and"
        f" [`RUN_VARIABLES.md`]({variables}) lists each value that a live run needs, the case"
        " that needs it, and where it goes. Add `--json FILE` to write the result stream that"
        " `ncp-tf-verify-report record` reads.",
        "",
        "### What the offline run does, in this order",
        "",
        "The offline run stops at the first tier that fails. `<Type>` is the case's name in"
        " Go, as the name of its test file gives it.",
        "",
        "| Tier | What it proves | Run one test of it alone |",
        "|---|---|---|",
        *_tiers(workspaces, sweeper),
        "",
        "### Select the resources that run",
        "",
        f"- Offline: `{runner} offline <workspace>...` plans only the named workspaces, which"
        f" are the directories under `{workspaces}`. The other tiers run in full.",
        f"- Live: `{runner} live -run '^TestAcc<Type>_lifecycle$'` runs one case.",
        "",
        "### How a resource gets a replay test",
        "",
        "A resource gets a replay test when its create, its read and its delete are recorded."
        " Ask for its recordings: `ncp-tf-cassette` records them, with the operator's word for"
        " each change to the account. Then the scaffold renders the cases with `--mode"
        " replay`. Each replay test holds its recordings, so the"
        f" `{workspaces}/replay_*_test.go` files name the resources that have one.",
        "",
        "### What a refusal means",
        "",
        "| The run says | What it means |",
        "|---|---|",
        "| `<tool> is not on PATH` | The tier needs that tool. Install it; the run skips no tier. |",
        "| `gofmt would rewrite` | A file is not formatted. A rendered file was edited by hand,"
        " or the provider's code needs `gofmt -w`. |",
        "| `git does not ignore .../.env` | `.env` holds credentials. Add it to `.gitignore`. |",
        "| `no main package` or `more than one main package` | `build` builds the one main"
        " package of the provider, and found none or several. |",
        "| `unset: TFKIT_VAR_<NAME>` | A live case needs a value that `.env` does not give."
        " The case stops before it creates anything, and names each value that it needs. |",
        "| `is off; set it to true.` | A live case sends a header that a provider setting"
        " switches on. Set that setting in `.env`. |",
        "| `no replay test under` | No resource has recordings yet, so the replay tier did not"
        " run. This is not a pass of that tier. |",
        "",
        "### Where the source address is",
        "",
        f"The `versions.tf` file of each workspace under `{workspaces}` states the provider's"
        f" source address, and `{RUNNER_SUBDIR}/{RUNNER_NAME}` states it too. `go.mod` holds the"
        " Go module path, which is not the source address. The address is a placeholder until a"
        " namespace is chosen: a local mirror satisfies it, and no registry resolves it. Change"
        " it in all of these places together: render the cases again with the new address.",
        "",
        "### Build the provider by hand (the manual path)",
        "",
        "Do this only to write Terraform against the provider directly. The tests do not need"
        f" it. `{runner} build` builds the provider into `{mirror}`, relative to this"
        " directory, or into"
        " `TFKIT_PROVIDER_MIRROR` when `.env` sets it. Then write a CLI configuration file and"
        " point `TF_CLI_CONFIG_FILE` at it:",
        "",
        "```hcl",
        "provider_installation {",
        "  filesystem_mirror {",
        '    path    = "<the absolute path of the mirror>"',
        '    include = ["<the source address in versions.tf>"]',
        "  }",
        "  direct {",
        '    exclude = ["<the source address in versions.tf>"]',
        "  }",
        "}",
        "```",
        "",
        "Without this file, `terraform init` looks in a registry, finds no provider, and its"
        " error does not name the mirror.",
        "",
        END,
    ]
    return "\n".join(lines) + "\n"


def updated(existing: str | None, section: str, title: str) -> str:
    """The README with `section` in place of the one it holds, or added to it."""
    if existing is None:
        return f"# {title}\n\n{section}"
    start = existing.find(BEGIN)
    stop = existing.find(END, start) if start >= 0 else -1
    if start >= 0 and stop >= 0:
        after = existing[stop + len(END) :].lstrip("\n")
        return existing[:start] + section + ("\n" + after if after else "")
    return existing.rstrip("\n") + "\n\n" + section


def write(directory: Path, *, workspaces: str, mirror: str, sweeper: str = "") -> Path:
    """Write the section into `directory`'s README.md, and return its path."""
    readme = directory / "README.md"
    existing = readme.read_text(encoding="utf-8") if readme.exists() else None
    section = render_section(workspaces=workspaces, mirror=mirror, sweeper=sweeper)
    readme.write_text(updated(existing, section, directory.resolve().name), encoding="utf-8")
    return readme
