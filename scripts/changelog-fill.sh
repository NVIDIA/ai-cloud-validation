#!/usr/bin/env bash
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

# Dispatch scripts/changelog-prompt.md to an LLM CLI which edits
# CHANGELOG.md in place. See CONTRIBUTING.md for usage.

set -euo pipefail

CLI="${1:-auto}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROMPT_FILE="$SCRIPT_DIR/changelog-prompt.md"

test -f "$PROMPT_FILE" || {
  echo "Error: prompt file $PROMPT_FILE not found" >&2
  exit 1
}

if [ "$CLI" = "auto" ]; then
  for candidate in codex claude cursor-agent; do
    if command -v "$candidate" >/dev/null 2>&1; then
      CLI="$candidate"
      break
    fi
  done
  if [ "$CLI" = "auto" ]; then
    {
      echo "Error: no LLM CLI found in PATH. Install one of:"
      echo "  codex        -> https://github.com/openai/codex"
      echo "  claude       -> https://docs.anthropic.com/en/docs/claude-code"
      echo "  cursor-agent -> https://cursor.com/docs/cli/overview"
    } >&2
    exit 1
  fi
fi

# Each invocation enables non-interactive file edits in the current
# workspace and streams tool-call progress to the terminal so the user
# can see what the agent is doing on a multi-minute run:
#   cursor-agent --force          (write enable; streams by default)
#   codex -s workspace-write      (write enable; streams by default)
#   claude --permission-mode acceptEdits --verbose
#                                 (write enable; --verbose forces
#                                  streaming, otherwise -p prints only
#                                  the final summary)
case "$CLI" in
  codex)                 cmd="codex exec -s workspace-write";                          hint="https://github.com/openai/codex" ;;
  claude)                cmd="claude -p --permission-mode acceptEdits --verbose";      hint="https://docs.anthropic.com/en/docs/claude-code" ;;
  cursor | cursor-agent) cmd="cursor-agent -p --force";                                hint="https://cursor.com/docs/cli/overview" ;;
  *)
    echo "Error: unsupported CLI '$CLI' (use one of: codex, claude, cursor, cursor-agent or auto)" >&2
    exit 1
    ;;
esac

bin="${cmd%% *}"
command -v "$bin" >/dev/null 2>&1 || {
  echo "Error: $bin not installed. See $hint" >&2
  exit 1
}

echo "Using '$cmd' to fill CHANGELOG.md..." >&2
$cmd "$(cat "$PROMPT_FILE")"
