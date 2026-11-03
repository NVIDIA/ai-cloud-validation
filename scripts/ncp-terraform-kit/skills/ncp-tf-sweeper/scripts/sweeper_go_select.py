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
"""The Go of the sweeper's selection: what selects a resource, and where each
type is listed. Its tests use the fake client of the core's tests."""

from __future__ import annotations

SELECT = r"""package main

import (
	"fmt"
	"regexp"
	"strings"
)

// matches says whether a resource belongs to the run, and by what. The kit
// prefix alone never selects: a test account is shared, and another run's
// resources carry the same prefix.
func matches(t Type, r Resource, runID string) (string, bool) {
	if carries(t, r.Name[strings.LastIndex(r.Name, "/")+1:], runID) {
		return "name", true
	}
	for _, field := range t.RunFields {
		if carries(t, r.Fields[field], runID) {
			return "field", true
		}
	}
	if t.RunLabel != "" && r.Labels[t.RunLabel] == runID {
		return "label", true
	}
	// A type whose resources carry no run id of their own is the run's when the
	// resource it is listed under is, and its name holds that resource's name.
	if t.ByParent != "" {
		segments := strings.Split(r.Name, "/")
		for _, segment := range segments[:len(segments)-1] {
			if carries(t, segment, runID) {
				return "parent", true
			}
		}
	}
	return "", false
}

// deleted says whether the NCP keeps the resource only as deleted: its field
// holds the value that the type's destroy expectation names. The approval
// record and a delete never hold it, because the NCP refuses the delete.
func deleted(t Type, r Resource) bool {
	return t.DeletedField != "" && r.Fields[t.DeletedField] == t.DeletedValue
}

// carries says whether a value is the kit prefix and the run id, alone or
// followed by a separator, after nothing or after a prefix that the type's
// cases write before the kit prefix.
func carries(t Type, value, runID string) bool {
	for _, prefix := range append([]string{""}, t.NamePrefixes...) {
		rest, ok := strings.CutPrefix(value, prefix)
		if !ok {
			continue
		}
		for _, separator := range separators(t) {
			named := kitPrefix + separator + runID
			if rest == named {
				return true
			}
			for _, next := range []string{separator, "@", "."} {
				if strings.HasPrefix(rest, named+next) {
					return true
				}
			}
		}
	}
	return false
}

// separators are the type's own separator and `-`. An empty separator is an
// API that permits none: it gives none after the run id either, so any text
// may follow the run id. The prefix match still selects one run, because
// runIDForm fixes the length of the id.
func separators(t Type) []string {
	if t.Separator == "-" {
		return []string{"-"}
	}
	return []string{t.Separator, "-"}
}

// expand fills a template from the run's values.
func expand(template, runID string, getenv func(string) string) (string, error) {
	var missing []string
	out := template
	for {
		start := strings.Index(out, "{")
		end := strings.Index(out, "}")
		if start < 0 || end < start {
			break
		}
		name := out[start+1 : end]
		value := runID
		if name != "run_id" {
			variable := "TFKIT_VAR_" + strings.ToUpper(name)
			value = getenv(variable)
			if value == "" {
				missing = append(missing, variable)
				value = "?"
			}
		}
		out = out[:start] + value + out[end+1:]
	}
	if len(missing) > 0 {
		return "", fmt.Errorf("unset: %s", strings.Join(missing, ", "))
	}
	return out, nil
}

// fill gives a parameter its value. With Env, the variable's value fills the
// template's first placeholder, which is the account scope's.
func fill(p Param, runID string, getenv func(string) string) (string, error) {
	template := p.Template
	if p.Env != "" {
		value := getenv(p.Env)
		if value == "" {
			return "", fmt.Errorf("unset: %s", p.Env)
		}
		start, end := strings.Index(template, "{"), strings.Index(template, "}")
		if start < 0 || end < start {
			return value, nil
		}
		template = template[:start] + value + template[end+1:]
	}
	return expand(template, runID, getenv)
}

// scopes are the parameter values a type is listed under. A parameter that
// several cases give takes each distinct value, and each combination of the
// parameters' values is one scope. A supplied value in another form than the
// parameter takes is no scope: the error names it, and the scopes that the
// run's own resources give are still returned.
func scopes(t Type, runID string, found []Found, getenv func(string) string) ([]map[string]string, error) {
	var names []string
	var refused error
	values := map[string][]string{}
	for _, p := range t.Params {
		if _, seen := values[p.Name]; !seen {
			names = append(names, p.Name)
			values[p.Name] = []string{}
		}
		var given []string
		if p.ScopeType != "" {
			for _, f := range found {
				if f.Type == p.ScopeType {
					given = append(given, f.Name)
				}
			}
			supplied, err := fill(p, runID, getenv)
			if err == nil && p.Pattern != "" {
				err = takesForm(p, supplied)
				if err != nil {
					refused = err
				}
			}
			if err == nil {
				given = append(given, supplied)
			} else if len(given) == 0 {
				// No resource of the scope type is the run's, and the supplied
				// one is unset: the type is listed nowhere, which is not clean.
				return nil, err
			}
		} else {
			value, err := fill(p, runID, getenv)
			if err != nil {
				return nil, err
			}
			given = []string{value}
		}
		for _, value := range given {
			if !contains(values[p.Name], value) {
				values[p.Name] = append(values[p.Name], value)
			}
		}
	}
	all := []map[string]string{{}}
	for _, name := range names {
		var next []map[string]string
		for _, scope := range all {
			for _, value := range values[name] {
				copied := map[string]string{name: value}
				for k, v := range scope {
					copied[k] = v
				}
				next = append(next, copied)
			}
		}
		all = next
	}
	return all, refused
}

// takesForm refuses a supplied value that does not match the parameter's
// pattern. The pattern is the document's, and a document can write one in a
// syntax that Go's regexp does not take, such as a lookahead: that refuses the
// value too, so the type is under cannot_list and the listing goes on.
func takesForm(p Param, supplied string) error {
	form, err := regexp.Compile(p.Pattern)
	if err != nil {
		return fmt.Errorf("the form %s that %s takes is not a Go regular expression, so %s is not checked: %v",
			p.Pattern, p.Name, suppliedVariables(p), err)
	}
	if !form.MatchString(supplied) {
		return fmt.Errorf("%s does not take the form %s that %s takes", suppliedVariables(p), p.Pattern, p.Name)
	}
	return nil
}

// suppliedVariables names the TFKIT_VAR_ variables that a parameter's supplied
// value is read from, and never their values.
func suppliedVariables(p Param) string {
	var variables []string
	template := p.Template
	if p.Env != "" {
		if end := strings.Index(template, "}"); end >= 0 {
			template = template[end+1:]
		}
	}
	for _, found := range regexp.MustCompile(`\{([a-z_][a-z0-9_]*)\}`).FindAllStringSubmatch(template, -1) {
		if found[1] != "run_id" {
			variables = append(variables, "TFKIT_VAR_"+strings.ToUpper(found[1]))
		}
	}
	return strings.Join(variables, ", ")
}

func contains(values []string, value string) bool {
	for _, held := range values {
		if held == value {
			return true
		}
	}
	return false
}
"""

SELECT_TESTS = r"""package main

import (
	"context"
	"strings"
	"testing"
)

func TestNearMissNamesAreNotSelected(t *testing.T) {
	for _, name := range []string{
		"tfkit-zzz999zzz9999-group",             // another run
		"other-" + testRun + "-group",           // the run id after another prefix
		"tfkit-group",                           // the prefix alone
		"tfkit-" + testRun + "9-group",          // a longer id that holds this one
		"xtfkit-" + testRun + "-group",          // the prefix inside a word
		"groups/tfkit-zzz999zzz9999-" + testRun, // the run id as a discriminator
	} {
		if by, ok := matches(group, Resource{Name: name}, testRun); ok {
			t.Errorf("%s selected by %s", name, by)
		}
	}
}

func TestTheRunsOwnNamesAreSelected(t *testing.T) {
	for _, name := range []string{
		"tfkit-" + testRun + "-group",
		"projects/p/groups/tfkit-" + testRun + "-group",
		"tfkit-" + testRun + "-sa@p.iam.example",
		"tfkit-" + testRun,
	} {
		if by, ok := matches(group, Resource{Name: name}, testRun); !ok || by != "name" {
			t.Errorf("%s not selected by its name", name)
		}
	}
	if _, ok := matches(role, Resource{Name: "projects/p/roles/tfkit_" + testRun + "_role"}, testRun); !ok {
		t.Error("a name with the type's own separator is not selected")
	}
}

func TestANameAfterAnAPIRequiredPrefixIsSelected(t *testing.T) {
	policy := Type{Name: "kit_policy", NamePrefixes: []string{"custom."}}
	mine := "organizations/o/policies/custom.tfkit" + testRun + "policy"
	if by, ok := matches(policy, Resource{Name: mine}, testRun); !ok || by != "name" {
		t.Errorf("%s not selected by its name", mine)
	}
	for _, name := range []string{
		"organizations/o/policies/custom.tfkitzzz999zzz9999policy",  // another run
		"organizations/o/policies/other.tfkit" + testRun + "policy", // a prefix no case writes
		"tfkit" + testRun + "/policies/custom.policy",               // the run id in the scope only
	} {
		if by, ok := matches(policy, Resource{Name: name}, testRun); ok {
			t.Errorf("%s selected by %s", name, by)
		}
	}
	if _, ok := matches(group, Resource{Name: "groups/custom.tfkit-" + testRun + "-group"}, testRun); ok {
		t.Error("a prefix selected for a type whose cases write none")
	}
}

func TestALabelMatchesATruncatedName(t *testing.T) {
	r := Resource{Name: "tfkit-abc", Labels: map[string]string{"tfkit-run": testRun}}
	if by, ok := matches(group, r, testRun); !ok || by != "label" {
		t.Fatalf("matched %q %v", by, ok)
	}
	r.Labels["tfkit-run"] = "zzz999zzz9999"
	if _, ok := matches(group, r, testRun); ok {
		t.Fatal("another run's label selected")
	}
}

func TestARunFieldIsMatchedByTheNameRules(t *testing.T) {
	keyed := Type{Name: "kit_keyed", Separator: "-", RunFields: []string{"key.id"}}
	held := func(value string) Resource {
		return Resource{Name: "groups/01abc", Fields: map[string]string{"key.id": value}}
	}
	for _, value := range []string{"tfkit-" + testRun + "@example.com", "tfkit-" + testRun + "-group@example.com"} {
		if by, ok := matches(keyed, held(value), testRun); !ok || by != "field" {
			t.Errorf("%s not selected by its field", value)
		}
	}
	for _, value := range []string{
		"tfkit-zzz999zzz9999@example.com",         // another run
		"tfkit@example.com",                       // the prefix alone
		"tfkit-" + testRun + "9@example.com",      // a longer id that holds this one
		"other-tfkit-" + testRun + "@example.com", // the prefix inside a word
		"",
	} {
		if by, ok := matches(keyed, held(value), testRun); ok {
			t.Errorf("%q selected by %s", value, by)
		}
	}
	if _, ok := matches(group, held("tfkit-"+testRun+"@example.com"), testRun); ok {
		t.Error("a field that the plan does not name selected a resource")
	}
}

func TestAParentSelectsOnlyATypeWithNoRunIDOfItsOwn(t *testing.T) {
	key := Type{Name: "kit_key", Separator: "-", ByParent: "kit_account"}
	mine := "projects/p/accounts/tfkit-" + testRun + "-sa@p.example/keys/0a1b"
	if by, ok := matches(key, Resource{Name: mine}, testRun); !ok || by != "parent" {
		t.Fatalf("a key under the run's account is not selected by its parent: %q %v", by, ok)
	}
	for _, name := range []string{
		"projects/p/accounts/tfkit-zzz999zzz9999-sa@p.example/keys/0a1b", // another run's account
		"projects/p/accounts/supplied@p.example/keys/0a1b",               // a supplied account
		"projects/p/accounts/other-tfkit-" + testRun + "/keys/0a1b",      // the prefix inside a word
		"projects/p/accounts/supplied/keys/tfkit-zzz999zzz9999-" + testRun,
	} {
		if by, ok := matches(key, Resource{Name: name}, testRun); ok {
			t.Errorf("%s selected by %s", name, by)
		}
	}
	owned := Type{Name: "kit_owned", Separator: "-"}
	if _, ok := matches(owned, Resource{Name: mine}, testRun); ok {
		t.Error("a type that carries its own run id was selected through its parent")
	}
}

func TestAnAccountScopeIsFilledFromItsVariable(t *testing.T) {
	account := Type{Name: "kit_account", ListMethod: "GET", ListPath: "{parent}/accounts", DeleteMethod: "DELETE", DeletePath: "{id}", Separator: "-", Params: []Param{{Name: "parent", Template: "projects/{project}", Env: "KIT_PROJECT"}}}
	client := &fakeClient{held: map[string][]Resource{"kit_account|": {{Name: "projects/p/accounts/tfkit-" + testRun + "-sa"}}}}
	record := list(context.Background(), client, []Type{account}, testRun, env(map[string]string{"KIT_PROJECT": "p"}))
	if scope := record.Scope["kit_account"]; len(scope) != 1 || scope[0]["parent"] != "projects/p" {
		t.Fatalf("listed in %v; the account scope is projects/p", scope)
	}
	if len(record.Found) != 1 {
		t.Fatalf("found %v", names(record.Found))
	}
	record = list(context.Background(), client, []Type{account}, testRun, env(nil))
	if len(record.CannotList) != 1 || record.CannotList[0].Reason != "unset: KIT_PROJECT" {
		t.Fatalf("cannot_list is %v", record.CannotList)
	}
}

func TestTheAccountScopeCompletesARelativeParent(t *testing.T) {
	pool := Type{Name: "kit_pool_provider", Separator: "-", Params: []Param{{Name: "parent", Template: "projects/{project}/locations/global/pools/{pool_id}", Env: "KIT_PROJECT"}}}
	listed, err := scopes(pool, testRun, nil, env(map[string]string{"KIT_PROJECT": "p", "TFKIT_VAR_POOL_ID": "pool"}))
	if err != nil || len(listed) != 1 || listed[0]["parent"] != "projects/p/locations/global/pools/pool" {
		t.Fatalf("listed in %v (%v)", listed, err)
	}
	if _, err := scopes(pool, testRun, nil, env(map[string]string{"KIT_PROJECT": "p"})); err == nil || !strings.Contains(err.Error(), "TFKIT_VAR_POOL_ID") {
		t.Fatalf("a placeholder after the account scope that is unset is not reported: %v", err)
	}
}

func TestATypeWithNoScopeIsRecordedNotSkipped(t *testing.T) {
	client := heldTwo()
	client.held["kit_group|"] = []Resource{{Name: "tfkit-zzz999zzz9999-group"}}
	record := list(context.Background(), client, []Type{member, group}, testRun, env(nil))
	for _, reason := range record.CannotList {
		if reason.Type == "kit_member" && reason.Reason == "unset: TFKIT_VAR_GROUP" {
			return
		}
	}
	t.Fatalf("no group of the run and no supplied group, and cannot_list is %v", record.CannotList)
}

func TestASuppliedValueInAnotherFormIsNotListed(t *testing.T) {
	formed := member
	formed.Params = []Param{{Name: "group", Template: "{group}", ScopeType: "kit_group", Pattern: "^groups/[^/]+$"}}
	plan := []Type{formed, group}
	client := heldTwo()
	record := list(context.Background(), client, plan, testRun, env(map[string]string{"TFKIT_VAR_GROUP": "g1"}))
	if len(record.CannotList) != 1 || record.CannotList[0].Type != "kit_member" {
		t.Fatalf("cannot_list is %v; a supplied group in another form is not listed, and is not clean", record.CannotList)
	}
	reason := record.CannotList[0].Reason
	if !strings.Contains(reason, "TFKIT_VAR_GROUP") || !strings.Contains(reason, "^groups/[^/]+$") || strings.Contains(reason, "g1") {
		t.Fatalf("the reason %q names the variable and the form, and not the value", reason)
	}
	for _, c := range client.calls {
		if c.kind == "kit_member" && c.name == "g1" {
			t.Fatal("the sweeper listed under a value in another form than the parameter takes")
		}
	}
	if !strings.Contains(strings.Join(names(record.Found), ","), "members/tfkit-"+testRun+"-member") {
		t.Fatalf("found %v; the run's own group is still listed", names(record.Found))
	}
	record = list(context.Background(), heldTwo(), plan, testRun, env(map[string]string{"TFKIT_VAR_GROUP": "groups/g1"}))
	if len(record.CannotList) != 0 {
		t.Fatalf("cannot_list is %v; a supplied group in the form is listed", record.CannotList)
	}
}

func TestAFormThatGoCannotReadIsRecordedNotAPanic(t *testing.T) {
	formed := member
	formed.Params = []Param{{Name: "group", Template: "{group}", ScopeType: "kit_group", Pattern: "^(?=groups/)groups/[^/]+$"}}
	client := heldTwo()
	record := list(context.Background(), client, []Type{formed, group}, testRun, env(map[string]string{"TFKIT_VAR_GROUP": "groups/g1"}))
	if len(record.CannotList) != 1 || record.CannotList[0].Type != "kit_member" {
		t.Fatalf("cannot_list is %v; a supplied value under a form that Go cannot read is not listed", record.CannotList)
	}
	reason := record.CannotList[0].Reason
	if !strings.Contains(reason, "TFKIT_VAR_GROUP") || !strings.Contains(reason, "(?=groups/)") || strings.Contains(reason, "g1") {
		t.Fatalf("the reason %q names the variable and the form, and not the value", reason)
	}
	if !strings.Contains(strings.Join(names(record.Found), ","), "members/tfkit-"+testRun+"-member") {
		t.Fatalf("found %v; the run's own group is still listed", names(record.Found))
	}
}

func TestEachValueOfAParameterIsAScope(t *testing.T) {
	cluster := Type{Name: "kit_cluster", ListMethod: "GET", ListPath: "{parent}/clusters", Separator: "-", Params: []Param{
		{Name: "parent", Template: "projects/{project_id}/locations/r1"},
		{Name: "parent", Template: "projects/{project_id}/locations/r1-a"},
		{Name: "parent", Template: "projects/{project_id}/locations/r1"},
	}}
	record := list(context.Background(), &fakeClient{}, []Type{cluster}, testRun, env(map[string]string{"TFKIT_VAR_PROJECT_ID": "p"}))
	var listed []string
	for _, scope := range record.Scope["kit_cluster"] {
		listed = append(listed, scope["parent"])
	}
	if strings.Join(listed, ",") != "projects/p/locations/r1,projects/p/locations/r1-a" {
		t.Fatalf("listed in %v; each case's location once", listed)
	}
}
"""
