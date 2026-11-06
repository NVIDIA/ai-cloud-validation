<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# NCP Terraform kit

Agent skills that build a Terraform integration for an NCP from the NCP's own API
specifications. You get an API coverage report and a module plan, then a Terraform
provider, layer modules, and a verification report. The provider and the modules are
tested offline, live in your account, and by replay of recorded exchanges.

The kit is a set of skills for a coding agent (Codex or Claude Code). It is not a
program that you run. You ask the agent for one step, and you review what the step
writes.

## Terraform modules

The kit plans four layers. It generates a Terraform module for each layer that the
NCP's API supports, fully or in part (a `buildable` or `partial` layer):

| Module | What it configures, for example |
|---|---|
| `iam` | identities, roles and access |
| `network` | networks, subnets, firewalls and connectivity |
| `cluster` | Kubernetes clusters and their node pools |
| `compute` | instances, disks and images |

The coverage report shows which layers the NCP's API can support, and which
resources each layer holds. A layer that the API cannot support is reported with
the operations that it does not have. A blueprint then composes the layers in
order, so one root module can create a whole environment.

## Contents

| Path | What it is |
|---|---|
| `skills/ncp-tf/` | The entry skill. It sends each request to the skill for that step. |
| `skills/ncp-tf-*/` | One skill for each step: coverage, scaffold, provider, module, cassette, sweeper and verification report. |
| `skills/ncp-tf/references/USER_GUIDE.md` | The user guide: the steps, their order, and what you decide. |

## Install

Copy the skills into the repository where the Terraform integration will live:

```bash
# Codex
mkdir -p <your-repo>/.agents/skills
cp -R skills/* <your-repo>/.agents/skills/

# Claude Code
mkdir -p <your-repo>/.claude/skills
cp -R skills/* <your-repo>/.claude/skills/
```

To update the kit, first delete the `ncp-tf*` skill folders of the earlier copy, then
copy the skills again. `cp -R` does not delete a file that the newer kit removed.

The skills run their helper scripts with [uv](https://docs.astral.sh/uv/). Each
script declares its own dependencies, so the install adds nothing else to your
repository. The steps also use Terraform and Go.

## Use

1. Put the NCP's API specifications in your repository, and pin them.
2. Start the agent in your repository.
3. Ask for the first step, in plain language. For example:
   *"Produce the coverage report for `<NCP>` from `<spec directory>`."*
4. Read the file that the step writes. Approve it, or say what to change.
5. Ask for the next step.

Account settings for the live tests go in a `.env` file that git ignores. Never
commit credentials.

For each step, its order, and the decisions that are yours, read the
[user guide](skills/ncp-tf/references/USER_GUIDE.md).
