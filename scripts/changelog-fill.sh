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

# Feed scripts/changelog-prompt.md to an LLM CLI which then edits
# CHANGELOG.md in place. Invoked by `make changelog-fill`.
#
# Usage:
#   scripts/changelog-fill.sh                # auto-detect
#   scripts/changelog-fill.sh cursor         # explicit
#   make changelog-fill                      # auto
#   make changelog-fill CLI=codex            # explicit
#
# Auto-detect priority (first installed wins): cursor-agent, codex, claude.

set -euo pipefail

CLI="${1:-auto}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROMPT_FILE="$SCRIPT_DIR/changelog-prompt.md"
PRIORITY="cursor-agent codex claude"

invocation() {
  case "$1" in
    cursor | cursor-agent) echo "cursor-agent -p --force" ;;
    codex)                 echo "codex exec" ;;
    claude)                echo "claude -p" ;;
    *) return 1 ;;
  esac
}

install_hint() {
  case "$1" in
    cursor | cursor-agent) echo "https://cursor.com/docs/cli/overview" ;;
    codex)                 echo "https://github.com/openai/codex" ;;
    claude)                echo "https://docs.anthropic.com/en/docs/claude-code" ;;
    *) echo "<none>" ;;
  esac
}

test -f "$PROMPT_FILE" || {
  echo "Error: prompt file $PROMPT_FILE not found" >&2
  exit 1
}

if [ "$CLI" = "auto" ]; then
  for candidate in $PRIORITY; do
    if command -v "$candidate" >/dev/null 2>&1; then
      CLI="$candidate"
      break
    fi
  done
  if [ "$CLI" = "auto" ]; then
    {
      echo "Error: no LLM CLI found in PATH. Install one of:"
      for c in $PRIORITY; do
        echo "  $c -> $(install_hint "$c")"
      done
    } >&2
    exit 1
  fi
fi

cmd="$(invocation "$CLI")" || {
  echo "Error: unsupported CLI '$CLI' (use one of: cursor, codex, claude, or auto)" >&2
  exit 1
}

bin="${cmd%% *}"
command -v "$bin" >/dev/null 2>&1 || {
  echo "Error: $bin not installed. See $(install_hint "$CLI")" >&2
  exit 1
}

echo "Using '$cmd' to fill CHANGELOG.md..." >&2
$cmd "$(cat "$PROMPT_FILE")"
