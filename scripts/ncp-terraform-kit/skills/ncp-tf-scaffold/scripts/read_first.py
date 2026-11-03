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
"""A create that reads its object before it writes.

The request check refuses the provider's first request. A create that changes
one entry of an object, such as one grant in a policy, reads the object first,
so the refused request is the read and the create request is never sent. The
plan cannot say in advance whether a provider reads first: that is the
provider's design. So the check looks at what the provider sent. When the first
request is the case's own read and not its create, the check skips and names
both requests, because it can assert nothing about the create.
"""

from __future__ import annotations

import json
from typing import Any

#: The Go half. It uses `requestObserver`, `capturedRequest` and `pathMatcher`
#: of the request check file, and adds no import.
SKIP_WHEN_THE_CREATE_READS_FIRST = r"""
// skipWhenTheCreateReadsFirst ends the check when the provider's first request
// is the resource's own read and not its create. This check refuses that read,
// so the create request is never sent here, and the check can assert nothing
// about it. A replay or a live run asserts the create request.
func skipWhenTheCreateReadsFirst(t *testing.T, observed *requestObserver, createMethod, createPath, readMethod, readPath string) {
	t.Helper()
	got, sent := observed.first()
	if !sent || sentAs(got, createMethod, createPath) || !sentAs(got, readMethod, readPath) {
		return
	}
	t.Skipf("the provider's first request is the read %s %s, and this check refuses it, "+
		"so the create %s %s is not sent offline. A replay or a live run asserts the create request",
		readMethod, readPath, createMethod, createPath)
}

func sentAs(got capturedRequest, method, path string) bool {
	return got.method == method && pathMatcher(path).MatchString(strings.Trim(got.path, "/"))
}
"""


def _request(case: dict[str, Any], verb: str) -> tuple[str, str] | None:
    for entry in case.get("expected_requests") or []:
        if isinstance(entry, dict) and str(entry.get("verb")) == verb:
            method, path = str(entry.get("method") or ""), str(entry.get("path") or "")
            return (method, path) if method and path else None
    return None


def skip_call(case: dict[str, Any]) -> str:
    """The call a request test makes before it checks the create request, or
    nothing when the case names no read that differs from its create."""
    create, read = _request(case, "create"), _request(case, "read")
    if not create or not read or create == read:
        return ""
    arguments = ", ".join(json.dumps(value) for value in (*create, *read))
    return f"\tskipWhenTheCreateReadsFirst(t, observed, {arguments})\n\n"
