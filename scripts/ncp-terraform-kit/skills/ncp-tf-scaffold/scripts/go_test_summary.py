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
"""The summary the provider entry point prints after each `go test -v` run."""

from __future__ import annotations

SUMMARISE = r"""# `go test` has one word for every test that did not pass, and a run where
# every case behaved correctly can still print a screen of FAIL. A case that
# refused to start because a value was withheld attempted nothing, and reading
# it as a failure is how a correct run looks broken.
#
# Only one distinction here is safe to draw mechanically: the guards' refusals
# are this script's own wording, so they can be recognised. Everything else is
# counted, not diagnosed -- whether a refusal by the API is a limit or a defect
# is a judgement, and the verification record is where a person writes it down.
#
# `go test` writes no result line for a test that its timeout or a panic stops,
# and a package can fail outside every test. Both are counted from what the
# stream lacks, because nothing in it says "failed" for them.
summarise() {
  awk '
    function note(name, text) {
      if (length(text) > 72) { text = substr(text, 1, 69) "..." }
      broke[++breaks] = name "\t" text
      if (breaks == 1) { first = text } else if (text != first) { differ = 1 }
    }
    # A package ends with its own result line, so a test of that package that
    # has no result by then never gets one, and the panic before that line is
    # what stopped it. One run can test several packages.
    function close_package(   i, k) {
      for (i = 1; i <= started; i++) {
        for (k = 0; k < pending[order[i]]; k++) {
          note(order[i], "started, no result" (stopped == "" ? "" : ": " stopped))
        }
        pending[order[i]] = 0
      }
      started = 0
      stopped = ""
    }
    /^=== RUN / {
      name = $3
      # A subtest reports its result indented, so only a top-level test can
      # lack a result line at the left margin.
      if (name !~ /\//) { order[++started] = name; pending[name]++ }
      next
    }
    /^    [A-Za-z0-9_.-]+\.go:[0-9]+: / && name != "" && !(name in reason) {
      line = $0
      sub(/^ +[A-Za-z0-9_.-]+\.go:[0-9]+: /, "", line)
      reason[name] = line
      next
    }
    # Terraform prefixes its own diagnostics, and that line says what the API
    # answered where the Go frame only says which step ran.
    /^ +Error: / && name != "" && !(name in problem) {
      line = $0
      sub(/^ +Error: /, "", line)
      problem[name] = line
      next
    }
    /^panic: / {
      if (stopped == "") stopped = $0
      if (panicked == "") panicked = $0
      next
    }
    /^FAIL\t/ || /^FAIL$/ {
      if (package_failed == "") package_failed = $0
      close_package()
      next
    }
    /^ok  / { close_package(); next }
    /^--- (PASS|SKIP|FAIL): / { pending[$3]-- }
    /^--- PASS: / { passed++; next }
    /^--- SKIP: / { skipped++; next }
    /^--- FAIL: / {
      failed_name = $3
      # Classify on the wording this script owns; show whichever line says most.
      text = reason[failed_name]
      shown = (failed_name in problem) ? problem[failed_name] : text
      guard = (text ~ /^unset: / || text ~ / is unset;/ || text ~ / is off; set it to true\./)
      if (guard) {
        sub(/\. This case needs.*/, "", shown)
        if (length(shown) > 72) { shown = substr(shown, 1, 69) "..." }
        refused[++refusals] = failed_name "\t" shown
      } else {
        note(failed_name, shown)
      }
      next
    }
    END {
      close_package()
      printf "\n%s\n", "Summary"
      printf "  %-20s %d\n", "passed", passed
      if (refusals > 0) printf "  %-20s %d\n", "refused to start", refusals
      if (skipped > 0)  printf "  %-20s %d\n", "skipped", skipped
      printf "  %-20s %d\n", "failed", breaks
      if (refusals > 0) {
        printf "\n%s\n", "Refused to start, so nothing was attempted:"
        for (i = 1; i <= refusals; i++) { split(refused[i], part, "\t"); printf "  %-44s %s\n", part[1], part[2] }
      }
      if (breaks > 0) {
        printf "\n%s\n", "Failed:"
        for (i = 1; i <= breaks; i++) { split(broke[i], part, "\t"); printf "  %-44s %s\n", part[1], part[2] }
        if (breaks > 1 && !differ) {
          printf "\n%s\n", "  Every failure reports the same message. That is usually one broken"
          printf "%s\n", "  harness rather than " breaks " broken tests."
        }
      } else if (package_failed != "" || panicked != "") {
        printf "\n%s\n", "The package failed, and no test result says why:"
        printf "  %s\n", (panicked != "" ? panicked : package_failed)
      }
      printf "\n"
    }
  ' "$1"
}"""
