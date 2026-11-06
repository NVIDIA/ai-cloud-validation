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
"""Replay: each resource's lifecycle against what the API returned once.

A cassette is the recording of one operation. The render reads the cassettes
of a case and writes them into the test as Go values, so the replay needs no
reader of its own and nothing outside the Go standard library. The transport
matches each request on its method, its URL and its body, serves what was
recorded, and never records: an absent recording is never permission to
record.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import header_switch
import yaml

#: The cassettes a replay test cannot run without. The update is optional: a
#: case with no update recording replays without its update step.
REQUIRED = ("create", "read", "delete")


def cassettes(case: dict[str, Any], directory: Path) -> dict[str, dict[str, Any]]:
    """Each recorded operation of the case, by verb."""
    found = {}
    for request in case.get("expected_requests") or []:
        path = directory / f"{request.get('cassette')}.yaml"
        if isinstance(request, dict) and path.is_file():
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            found[str(request["verb"])] = {**document, "name": request["cassette"], "headers": _asserted(request)}
    return found


def _asserted(request: dict[str, Any]) -> list[str]:
    """The headers replay asserts: those the provider sets, as the request check does."""
    return [
        str(h["name"])
        for h in request.get("headers") or []
        if isinstance(h, dict)
        and h.get("name")
        and h.get("required")
        and not str(h.get("source") or "").startswith("auth")
    ]


def missing(case: dict[str, Any], recorded: dict[str, Any]) -> list[str]:
    """The verbs the case asserts and nothing recorded, each reported as `no_cassette`."""
    return [
        str(r["verb"])
        for r in case.get("expected_requests") or []
        if isinstance(r, dict) and r.get("verb") and str(r["verb"]) not in recorded
    ]


def updates(recorded: dict[str, Any]) -> bool:
    """Whether a change other than the create and the delete is recorded. A
    case can name several update operations, such as a PATCH and a set-labels
    call, and the provider sends only the one that the case's update needs."""
    return any(verb not in REQUIRED for verb in recorded)


def dropped_steps(case: dict[str, Any], recorded: dict[str, Any]) -> list[str]:
    """The update verbs that leave the replay without its update step: none
    while any update operation is recorded."""
    return [] if updates(recorded) else [v for v in missing(case, recorded) if v not in REQUIRED]


def _go(value: str) -> str:
    return json.dumps(value)


def cassette_values(go_name: str, recorded: dict[str, dict[str, Any]]) -> str:
    """The recordings as Go values."""
    lines = [f"var replay{go_name}Cassettes = []replayCassette{{"]
    for verb in sorted(recorded):
        cassette = recorded[verb]
        headers = ", ".join(_go(h) for h in cassette["headers"])
        lines.append(
            f"\t{{name: {_go(cassette['name'])}, verb: {_go(verb)}, "
            f"headers: []string{{{headers}}}, interactions: []replayInteraction{{"
        )
        for interaction in cassette.get("interactions") or []:
            request, response = interaction.get("request") or {}, interaction.get("response") or {}
            link = (
                f"afterRequest: {_go(str(interaction['after_request']))}, " if interaction.get("after_request") else ""
            )
            lines.append(
                f"\t\t{{method: {_go(str(request.get('method', '')))}, "
                f"url: {_go(str(request.get('url', '')))}, body: {_go(str(request.get('body') or ''))}, "
                f"after: {_go(str(interaction.get('after') or ''))}, {link}"
                f"status: {int(response.get('status', 0))}, "
                f"contentType: {_go(str(response.get('content_type') or ''))}, "
                f"response: {_go(str(response.get('body') or ''))}}},"
            )
        lines.append("\t}},")
    lines.append("}")
    return "\n".join(lines) + "\n"


def supplied_forms(recorded: dict[str, dict[str, Any]]) -> str:
    """A case value whose form the recording holds takes that form, with the
    stand-ins of its placeholders, so the configuration sends what the
    recording holds wherever it uses the value."""
    forms: dict[str, str] = {}
    for cassette in recorded.values():
        forms.update(cassette.get("supplied") or {})
    return "".join(f"\tsupplied[{_go(name)}] = replaySupplied({_go(form)})\n" for name, form in sorted(forms.items()))


def _imports(forms: str) -> list[str]:
    return sorted({"testing", *(("maps",) if forms else ()), *(("fmt",) if "fmt." in forms else ())})


def render_test(
    case: dict[str, Any],
    recorded: dict[str, dict[str, Any]],
    *,
    go_name: str,
    config_name: Any,
    values: list[str],
    steps: list[tuple[str, str]],
    forms: str = "",
) -> str:
    """`TestReplay<Type>_lifecycle`: the case's steps against the recordings.

    `forms` builds each supplied dependency's form from its name. The replayer
    takes the names first, because the recording holds the names the run sent.
    """
    stubs = ", ".join(_go(n) for n in values)
    rendered = []
    for kind, which in steps:
        if which == "update" and not updates(recorded):
            continue
        config = f"{config_name(case, which)}(stubRunID, supplied),"
        body = f"Config:   {config}\n\t\t\t\tPlanOnly: true," if kind == "plan" else f"Config: {config}"
        rendered.append(f"\t\t\t{{\n\t\t\t\t{body}\n\t\t\t}},")
    return (
        "// Generated from the case and its recordings. Edit the case or record again,\n"
        "// not this file.\n\npackage provider_test\n\n"
        "import (\n"
        + "".join(f'\t"{name}"\n' for name in _imports(forms))
        + '\n\t"github.com/hashicorp/terraform-plugin-testing/helper/resource"\n)\n\n'
        + cassette_values(go_name, recorded)
        + "\n"
        f"// TestReplay{go_name}_lifecycle drives the lifecycle against the recordings,\n"
        "// so the read, update and delete requests are asserted as the create is.\n"
        f"func TestReplay{go_name}_lifecycle(t *testing.T) {{\n"
        "\trequestEnvironment(t)\n"
        + header_switch.turned_on(case)
        + f"\tsupplied := stubValues({stubs})\n"
        + supplied_forms(recorded)
        + f"\treplay := newReplayer(t, replay{go_name}Cassettes, {'maps.Clone(supplied)' if forms else 'supplied'})\n"
        + forms
        + "\n"
        "\tresource.UnitTest(t, resource.TestCase{\n"
        "\t\tProtoV6ProviderFactories: requestProviderFactories(replay.answer),\n"
        "\t\tSteps: []resource.TestStep{\n" + "\n".join(rendered) + "\n\t\t},\n\t})\n"
        "\treplay.finish()\n}\n"
    )


REPLAYER = r"""// Generated with the cases. Edit the case or record again, not this file.

package provider_test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"reflect"
	"regexp"
	"sort"
	"strings"
	"sync"
)

// replayInteraction is one recorded request and what the API answered.
type replayInteraction struct {
	method, url, body, after string
	// afterRequest is the change call that a read follows, as
	// <cassette>:<index>. A phase cannot tell two calls of one setter apart.
	afterRequest          string
	status                int
	contentType, response string
}

// replayCassette is the recording of one operation.
type replayCassette struct {
	name, verb   string
	headers      []string
	interactions []replayInteraction
}

// replayReporter is what a replayer reports a failure to: the test.
type replayReporter interface {
	Helper()
	Errorf(format string, args ...any)
}

// replayer answers the provider from the recordings. It never records, and it
// never reaches the API: a request that no recording matches fails the test.
type replayer struct {
	t         replayReporter
	mu        sync.Mutex
	cassettes []replayCassette
	values    map[string]string
	served    map[string]int
	used      map[string]bool
	// phase is the last change the provider made: create, update or delete. A
	// read is answered with what the API returned after that change.
	phase string
	// change is the last change call, as <cassette>:<index>. A read that names
	// it under afterRequest is answered only after that call.
	change string
	// missedOnce is set when the first read after the create has been given the
	// recorded not-found. The API answered a read that way while the resource
	// existed, and a provider that believes it loses the resource. Only a
	// not-found is served then: an API that keeps a deleted resource answers the
	// read after the delete with it, and that is not the resource the create made.
	missedOnce bool
}

var placeholder = regexp.MustCompile(`\{([A-Za-z_][A-Za-z0-9_]*)\}`)

func newReplayer(t replayReporter, cassettes []replayCassette, supplied map[string]string) *replayer {
	return &replayer{t: t, cassettes: cassettes, values: supplied,
		served: map[string]int{}, used: map[string]bool{}}
}

// fill puts the offline stand-ins where the recording left placeholders: the
// run id, each supplied value, and each variable by its value in the request
// environment. A variable that the environment does not set, such as an
// account value that only a response holds, takes a stand-in of its own, so
// no placeholder is served empty.
func (r *replayer) fill(text string) string {
	return placeholder.ReplaceAllStringFunc(text, func(found string) string {
		name := found[1 : len(found)-1]
		if value, ok := runStandIn(name); ok {
			return value
		}
		if r.values[name] != "" {
			return r.values[name]
		}
		return found
	})
}

// runStandIn is what replay puts for the run id and for a variable of the
// request environment.
func runStandIn(name string) (string, bool) {
	switch {
	case name == "run_id":
		return stubRunID, true
	case name == strings.ToUpper(name):
		return replayStandIn(name), true
	}
	return "", false
}

// replaySupplied is a value that the run supplied, in the form that its
// recording holds. A provider can check that form, such as a path that the
// read's name pattern accepts, so a flat stand-in does not serve.
func replaySupplied(form string) string {
	return placeholder.ReplaceAllStringFunc(form, func(found string) string {
		if value, ok := runStandIn(found[1 : len(found)-1]); ok {
			return value
		}
		return found
	})
}

// replayStandIn is what replay puts where a recording holds a variable of the
// request environment: its value, or a stand-in of its own, so no placeholder
// is served empty.
func replayStandIn(name string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return "tfkit-stub-" + strings.ToLower(strings.ReplaceAll(name, "_", "-"))
}

// maskNames are the names a field mask goes by, as the provider's operation
// index reads them. A mask names a set of fields, and the API reads its paths
// in any order, so replay compares them as a set.
var maskNames = map[string]bool{"updateMask": true, "update_mask": true, "fieldMask": true,
	"field_mask": true, "mask": true, "paths": true}

// dedupToken is what a recording holds for a de-duplication token. The provider
// makes a new token for each request, so replay requires the parameter and
// accepts any value in it.
const dedupToken = "{dedup_token}"

func sameURL(recorded string, request *http.Request) bool {
	want, err := url.Parse(recorded)
	if err != nil {
		return false
	}
	wanted, sent := want.Query(), request.URL.Query()
	for name, values := range wanted {
		if len(values) == 1 && values[0] == dedupToken {
			if len(sent[name]) != 1 || sent[name][0] == "" {
				return false
			}
			wanted.Del(name)
			sent.Del(name)
		}
	}
	return strings.Trim(want.Path, "/") == strings.Trim(request.URL.Path, "/") &&
		canonicalQuery(wanted) == canonicalQuery(sent)
}

func canonicalQuery(query url.Values) string {
	for name, values := range query {
		if !maskNames[name] {
			continue
		}
		var paths []string
		for _, value := range values {
			paths = append(paths, strings.Split(value, ",")...)
		}
		sort.Strings(paths)
		query[name] = []string{strings.Join(paths, ",")}
	}
	return query.Encode()
}

func sameBody(recorded string, sent []byte) bool {
	if strings.TrimSpace(recorded) == "" || len(bytes.TrimSpace(sent)) == 0 {
		return strings.TrimSpace(recorded) == "" && len(bytes.TrimSpace(sent)) == 0
	}
	var want, got any
	if json.Unmarshal([]byte(recorded), &want) != nil || json.Unmarshal(sent, &got) != nil {
		return recorded == string(sent)
	}
	return reflect.DeepEqual(want, got)
}

type replayMatch struct {
	cassette    *replayCassette
	interaction replayInteraction
	key         string
}

func (r *replayer) matches(request *http.Request, sent []byte) []replayMatch {
	var found []replayMatch
	for i := range r.cassettes {
		cassette := &r.cassettes[i]
		for j, interaction := range cassette.interactions {
			if interaction.method == request.Method && sameURL(r.fill(interaction.url), request) &&
				sameBody(r.fill(interaction.body), sent) {
				found = append(found, replayMatch{cassette, interaction, fmt.Sprintf("%s:%d", cassette.name, j)})
			}
		}
	}
	return found
}

// choose picks the answer. A read is answered from the reads that name the
// last change call, or else from the reads without a call recorded after the
// phase's change; every request takes its recordings in order and repeats
// the last, so a provider that polls more often still reaches the end, and a
// poll for readiness reaches the read that was ready.
func (r *replayer) choose(request *http.Request, found []replayMatch) replayMatch {
	key := request.Method + " " + request.URL.String()
	if found[0].cassette.verb == "read" {
		if r.phase == "create" && !r.missedOnce {
			for _, m := range found {
				if m.interaction.after == "delete" && m.interaction.status == http.StatusNotFound {
					r.missedOnce = true
					return m
				}
			}
		}
		var phase []replayMatch
		cursor := r.change
		for _, m := range found {
			if m.interaction.afterRequest != "" && m.interaction.afterRequest == r.change {
				phase = append(phase, m)
			}
		}
		if len(phase) == 0 {
			cursor = r.phase
			for _, m := range found {
				if m.interaction.afterRequest == "" && m.interaction.after == r.phase {
					phase = append(phase, m)
				}
			}
		}
		if len(phase) == 0 {
			return found[0]
		}
		found, key = phase, cursor+" "+key
	}
	index := r.served[key]
	if index >= len(found) {
		index = len(found) - 1
	}
	r.served[key]++
	// A change sets the phase; a poll of it does not, and neither does a read,
	// which some APIs send as a POST. A recording marks the reads after any
	// change but the create and the delete as after the update.
	if verb := found[index].cassette.verb; request.Method != http.MethodGet && verb != "read" {
		if verb != "create" && verb != "delete" {
			verb = "update"
		}
		r.phase = verb
		r.change = found[index].key
	}
	return found[index]
}

func (r *replayer) answer(request *http.Request) (*http.Response, error) {
	var sent []byte
	if request.Body != nil {
		sent, _ = io.ReadAll(request.Body)
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	found := r.matches(request, sent)
	if len(found) == 0 {
		r.t.Errorf("replay: no recording matches %s %s %s; %s", request.Method,
			request.URL.RequestURI(), sent, r.closest(request))
		return r.response(request, http.StatusNotImplemented,
			`{"error":{"code":501,"message":"tfkit replay: no recording matches this request"}}`, "application/json"), nil
	}
	chosen := r.choose(request, found)
	r.used[chosen.cassette.name] = true
	// The case states the headers of the operation's own request. A poll of the
	// operation it returned is another call, which the case says nothing about.
	asserted := chosen.cassette.headers
	if chosen.cassette.verb != "read" && chosen.interaction.method == http.MethodGet {
		asserted = nil
	}
	for _, header := range asserted {
		if request.Header.Get(header) == "" {
			r.t.Errorf("replay: %s %s carries no %s, which %s asserts", request.Method,
				request.URL.Path, header, chosen.cassette.name)
		}
	}
	return r.response(request, chosen.interaction.status, r.fill(chosen.interaction.response),
		chosen.interaction.contentType), nil
}

func (r *replayer) closest(request *http.Request) string {
	for _, cassette := range r.cassettes {
		for _, interaction := range cassette.interactions {
			if interaction.method == request.Method {
				return fmt.Sprintf("the closest recording is %s %s %s in %s", interaction.method,
					r.fill(interaction.url), r.fill(interaction.body), cassette.name)
			}
		}
	}
	return "no recording has that method"
}

func (r *replayer) response(request *http.Request, status int, body, contentType string) *http.Response {
	return &http.Response{
		StatusCode: status,
		Header:     http.Header{"Content-Type": []string{contentType}},
		Body:       io.NopCloser(strings.NewReader(body)),
		Request:    request,
	}
}

// finish fails the test for each recording the provider never asked for: the
// request that operation makes was not sent, or not as it was recorded.
func (r *replayer) finish() {
	r.t.Helper()
	for _, cassette := range r.cassettes {
		if !r.used[cassette.name] {
			r.t.Errorf("replay: the provider never sent the request that %s recorded", cassette.name)
		}
	}
}
"""
