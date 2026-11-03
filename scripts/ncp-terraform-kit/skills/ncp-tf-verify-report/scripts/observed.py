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
"""What a lifecycle test printed, in the words a record may keep.

The output of a failed test also holds the API's answer. That answer is the
NCP's own format, and it holds values of one deployment, so it is not read.
Two texts have a form that the kit or Terraform owns:

- a case guard's refusal, in the scaffold's own wording;
- Terraform's diagnostic headline, `Error: <summary>`.

A guard refuses before any request, so its refusal says more than a later
diagnostic. A headline is the provider's text, and a provider can put a value
of the deployment into it: an account, a project, a run, a person. No pattern
finds every such value, so a headline is copied only when it holds words and
nothing else. The record holds no deployment value, and nothing is better than
a leaked one.
"""

from __future__ import annotations

import json
import re

#: `go test` prefixes a test's own message with its file and line.
TEST_MESSAGE = re.compile(r"^\s*\S+_test\.go:\d+: (?P<text>.*)$")

#: The scaffold's guards, in their rendered wording; `go_test_summary.py`
#: recognises the same three forms.
GUARDS = (
    re.compile(r"^(?P<refusal>unset: [A-Z0-9_, ]+)"),
    re.compile(r"^(?P<refusal>[A-Z0-9_]+ is unset);"),
    re.compile(r"^(?P<refusal>[A-Z0-9_]+ is off; set it to true)\."),
)

REFUSED = "refused to start"

DIAGNOSTIC = re.compile(r"^\s*Error: (?P<summary>.+?)\s*$")

#: Words only. A value names itself with a digit, a quote, a slash, a dot, a
#: colon, a hyphen or an `@`, and a headline that holds one is not copied.
WORDS_ONLY = re.compile(r"^[A-Za-z_ ]+$")


def printed(lines: str) -> dict[str, str]:
    """What each top-level test printed that a record may keep, by test name."""
    outputs: dict[str, list[str]] = {}
    for line in lines.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        test, output = event.get("Test"), event.get("Output")
        if event.get("Action") == "output" and isinstance(test, str) and isinstance(output, str):
            outputs.setdefault(test.split("/")[0], []).extend(output.splitlines())
    found = {test: _kept(output) for test, output in outputs.items()}
    return {test: text for test, text in found.items() if text}


def joined(evidence: str, text: str | None) -> str:
    """The evidence of a test's result, with what the test printed after it."""
    if not text:
        return evidence
    return f"{evidence}; {text}" if text.startswith(REFUSED) else f"{evidence}: {text}"


def _kept(output: list[str]) -> str | None:
    return _refusal(output) or _headline(output)


def _refusal(output: list[str]) -> str | None:
    for line in output:
        message = TEST_MESSAGE.match(line)
        for guard in GUARDS:
            matched = guard.match(message.group("text")) if message else None
            if matched:
                return f"{REFUSED}: {matched.group('refusal').rstrip(', ')}"
    return None


def _headline(output: list[str]) -> str | None:
    for line in output:
        matched = DIAGNOSTIC.match(line)
        if matched:
            summary = matched.group("summary")
            return summary if WORDS_ONLY.match(summary) else None
    return None
