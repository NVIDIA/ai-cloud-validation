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
"""The Go unit tests of the sweeper's core: its fake client, the listing, the
delete flow and the record, and the order of the derived plan."""

from __future__ import annotations

TESTS = r"""package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// These tests use a plan of their own, so they hold for every NCP's plan.
// TestThePlanDeletesATypeBeforeItsScope alone reads the derived plan.
const testRun = "tfkitabc123de"

var (
	group   = Type{Name: "kit_group", ListMethod: "GET", ListPath: "groups", DeleteMethod: "DELETE", DeletePath: "groups/{id}", RunLabel: "tfkit-run", Separator: "-"}
	member  = Type{Name: "kit_member", ListMethod: "GET", ListPath: "{group}/members", DeleteMethod: "DELETE", DeletePath: "{id}", Separator: "-", Params: []Param{{Name: "group", Template: "{group}", ScopeType: "kit_group"}}}
	role    = Type{Name: "kit_role", ListMethod: "GET", ListPath: "projects/{project}/roles", DeleteMethod: "DELETE", DeletePath: "{id}", Separator: "_", Params: []Param{{Name: "project", Env: "KIT_PROJECT"}}}
	pinned  = Type{Name: "kit_pinned", ListMethod: "GET", ListPath: "pinned", Separator: "-", CannotDelete: "no_delete_operation"}
	unseen  = Type{Name: "kit_unseen", CannotList: "no_list_operation", DeleteMethod: "POST", DeletePath: "unseen/terminate"}
	allPlan = []Type{member, group, role, pinned, unseen}
)

type call struct{ verb, kind, name string }

type fakeClient struct {
	held    map[string][]Resource
	refuse  map[string]bool
	fail    map[string]error
	calls   []call
	onFirst func(*fakeClient)
}

func (f *fakeClient) key(t Type, scope map[string]string) string {
	return t.Name + "|" + scope["group"]
}

func (f *fakeClient) List(_ context.Context, t Type, scope map[string]string) ([]Resource, error) {
	f.calls = append(f.calls, call{"list", t.Name, scope["group"]})
	if err := f.fail[f.key(t, scope)]; err != nil {
		return nil, err
	}
	return append([]Resource(nil), f.held[f.key(t, scope)]...), nil
}

func (f *fakeClient) Delete(_ context.Context, t Type, scope map[string]string, r Resource) error {
	f.calls = append(f.calls, call{"delete", t.Name, r.Name})
	if f.onFirst != nil {
		f.onFirst(f)
		f.onFirst = nil
	}
	if f.refuse[r.Name] {
		return errors.New("409 the resource is in use")
	}
	key := f.key(t, scope)
	kept := f.held[key][:0]
	for _, held := range f.held[key] {
		if held.Name != r.Name {
			kept = append(kept, held)
		}
	}
	f.held[key] = kept
	return nil
}

func env(values map[string]string) func(string) string {
	return func(name string) string { return values[name] }
}

func names(found []Found) []string {
	var out []string
	for _, f := range found {
		out = append(out, f.Type+" "+f.Name)
	}
	return out
}

func TestNoRunIDRefusesBeforeAnyAccountIsContacted(t *testing.T) {
	contacted := false
	var stdout, stderr bytes.Buffer
	code := run(context.Background(), nil, &stdout, &stderr, env(nil),
		func(context.Context) (Client, error) { contacted = true; return &fakeClient{}, nil })
	if code != 2 || contacted || stdout.Len() != 0 {
		t.Fatalf("exit %d, contacted %v, stdout %q", code, contacted, stdout.String())
	}
	if !strings.Contains(stderr.String(), "--run-id") {
		t.Fatalf("the refusal does not name --run-id: %s", stderr.String())
	}
}

func TestARunIDThatIsNotTheRunnerFormIsRefused(t *testing.T) {
	for _, id := range []string{"tfkit", "tfk", "tfkitabc123def", "tfkitABC123DE", "abc123def4567"} {
		contacted := false
		var stdout, stderr bytes.Buffer
		code := run(context.Background(), []string{"--run-id", id}, &stdout, &stderr, env(nil),
			func(context.Context) (Client, error) { contacted = true; return &fakeClient{}, nil })
		if code != 2 || contacted || stdout.Len() != 0 {
			t.Errorf("run id %q: exit %d, contacted %v, stdout %q", id, code, contacted, stdout.String())
		}
	}
	contacted := false
	var stdout, stderr bytes.Buffer
	run(context.Background(), []string{"--run-id", testRun}, &stdout, &stderr, env(nil),
		func(context.Context) (Client, error) { contacted = true; return &fakeClient{}, nil })
	if !contacted {
		t.Fatalf("the runner's run id %q was refused: %s", testRun, stderr.String())
	}
}

func heldTwo() *fakeClient {
	return &fakeClient{held: map[string][]Resource{
		"kit_group|":                             {{Name: "tfkit-" + testRun + "-group"}, {Name: "tfkit-zzz999zzz9999-group"}},
		"kit_member|tfkit-" + testRun + "-group": {{Name: "members/tfkit-" + testRun + "-member"}},
		"kit_role|":                              {{Name: "roles/tfkit_" + testRun + "_role"}},
		"kit_pinned|":                            {{Name: "tfkit-" + testRun + "-pinned"}},
	}}
}

func TestAListingDeletesNothing(t *testing.T) {
	client := heldTwo()
	record := list(context.Background(), client, allPlan, testRun, env(map[string]string{"KIT_PROJECT": "p"}))
	for _, c := range client.calls {
		if c.verb == "delete" {
			t.Fatalf("a listing deleted %s", c.name)
		}
	}
	listed := map[string]bool{}
	for _, c := range client.calls {
		listed[c.kind] = true
	}
	for _, kind := range []string{"kit_group", "kit_member", "kit_role", "kit_pinned"} {
		if !listed[kind] {
			t.Errorf("%s was not listed", kind)
		}
	}
	got := strings.Join(names(record.Found), ", ")
	want := "kit_pinned tfkit-" + testRun + "-pinned, kit_role roles/tfkit_" + testRun + "_role, kit_group tfkit-" + testRun + "-group, kit_member members/tfkit-" + testRun + "-member"
	if got != want {
		t.Fatalf("found %s\nwant  %s", got, want)
	}
	if len(record.CannotList) != 1 || record.CannotList[0].Type != "kit_unseen" || record.CannotList[0].Reason != "no_list_operation" {
		t.Fatalf("cannot_list is %v", record.CannotList)
	}
	if len(record.CannotDelete) != 1 || record.CannotDelete[0].Type != "kit_pinned" {
		t.Fatalf("cannot_delete is %v", record.CannotDelete)
	}
}

func TestAnUnsetScopeIsRecordedNotGuessed(t *testing.T) {
	record := list(context.Background(), heldTwo(), allPlan, testRun, env(nil))
	for _, reason := range record.CannotList {
		if reason.Type == "kit_role" && reason.Reason == "unset: KIT_PROJECT" {
			return
		}
	}
	t.Fatalf("cannot_list is %v", record.CannotList)
}

func TestADeleteFollowsThePlanOrderAndOnlyTheApproval(t *testing.T) {
	client := heldTwo()
	getenv := env(map[string]string{"KIT_PROJECT": "p"})
	approved := list(context.Background(), client, allPlan, testRun, getenv)
	client.held["kit_role|"] = append(client.held["kit_role|"], Resource{Name: "roles/tfkit_" + testRun + "_late"})
	record := sweep(context.Background(), client, allPlan, testRun, getenv, approved)
	var deleted []string
	for _, c := range client.calls {
		if c.verb == "delete" {
			deleted = append(deleted, c.kind)
		}
	}
	if strings.Join(deleted, ",") != "kit_member,kit_group,kit_role" {
		t.Fatalf("deleted in the order %v; a member goes before its group", deleted)
	}
	late := "roles/tfkit_" + testRun + "_late"
	for _, f := range record.Removed {
		if f.Name == late {
			t.Fatal("a resource that appeared after the approval was deleted")
		}
	}
	if !strings.Contains(strings.Join(names(record.Found), ","), late) {
		t.Fatal("the late resource is not recorded under found")
	}
	if got := strings.Join(names(record.Left), ", "); got != "kit_pinned tfkit-"+testRun+"-pinned, kit_role "+late {
		t.Fatalf("left is %s", got)
	}
}

func TestAFailedDeleteIsRecordedAndTheSweepGoesOn(t *testing.T) {
	client := heldTwo()
	client.refuse = map[string]bool{"members/tfkit-" + testRun + "-member": true}
	getenv := env(map[string]string{"KIT_PROJECT": "p"})
	approved := list(context.Background(), client, allPlan, testRun, getenv)
	record := sweep(context.Background(), client, allPlan, testRun, getenv, approved)
	if len(record.Failed) != 1 || !strings.Contains(record.Failed[0].Error, "409") {
		t.Fatalf("failed is %v", record.Failed)
	}
	if len(record.Removed) != 2 {
		t.Fatalf("removed is %v; the sweep stopped at the refusal", names(record.Removed))
	}
}

func TestAnApprovalOfAnotherRunDeletesNothing(t *testing.T) {
	client := heldTwo()
	getenv := env(map[string]string{"KIT_PROJECT": "p"})
	approved := list(context.Background(), client, allPlan, testRun, getenv)
	approved.RunID = "zzz999zzz9999"
	if record := sweep(context.Background(), client, allPlan, testRun, getenv, approved); len(record.Removed) != 0 {
		t.Fatalf("removed %v", names(record.Removed))
	}
}

func TestTheRecordIsOneDocumentOnStdout(t *testing.T) {
	var stdout, stderr bytes.Buffer
	code := run(context.Background(), []string{"--run-id", testRun}, &stdout, &stderr,
		env(map[string]string{"KIT_PROJECT": "p"}),
		func(context.Context) (Client, error) { return heldTwo(), nil })
	if code != 0 && code != exitUnlisted {
		t.Fatalf("exit %d: %s", code, stderr.String())
	}
	var record map[string]any
	if err := json.Unmarshal(stdout.Bytes(), &record); err != nil {
		t.Fatalf("stdout is not one document: %v\n%s", err, stdout.String())
	}
	for _, key := range []string{"kind", "run_id", "scope", "found", "cannot_list", "cannot_delete"} {
		if _, ok := record[key]; !ok {
			t.Errorf("the record has no %s", key)
		}
	}
}

func TestDeleteTakesOnlyARecordOfThisRun(t *testing.T) {
	path := filepath.Join(t.TempDir(), "approved.json")
	if err := os.WriteFile(path, []byte(`{"kind":"sweep","run_id":"zzz999zzz9999","found":[]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	var stdout, stderr bytes.Buffer
	code := run(context.Background(), []string{"--run-id", testRun, "--delete", path}, &stdout, &stderr,
		env(nil), func(context.Context) (Client, error) { return heldTwo(), nil })
	if code != 2 {
		t.Fatalf("exit %d", code)
	}
}

func TestAnApprovalHoldsTheScopeItWasListedIn(t *testing.T) {
	client := heldTwo()
	second := "tfkit-" + testRun + "-group2"
	client.held["kit_group|"] = append(client.held["kit_group|"], Resource{Name: second})
	getenv := env(map[string]string{"KIT_PROJECT": "p"})
	approved := list(context.Background(), client, allPlan, testRun, getenv)
	member := "members/tfkit-" + testRun + "-member"
	client.held["kit_member|"+second] = []Resource{{Name: member}}
	record := sweep(context.Background(), client, allPlan, testRun, getenv, approved)
	for _, f := range record.Removed {
		if f.Type == "kit_member" && f.Scope["group"] == second {
			t.Fatalf("deleted %s in %s; the operator approved the one in the first group", f.Name, second)
		}
	}
	if len(client.held["kit_member|"+second]) != 1 {
		t.Fatal("the member in the second group is gone")
	}
}

func TestASweepThatCannotListAPlannedTypeExitsUnlisted(t *testing.T) {
	if code := status(allPlan, list(context.Background(), heldTwo(), allPlan, testRun, env(nil))); code != exitUnlisted {
		t.Fatalf("exit %d; a listing that could not list kit_role exits %d", code, exitUnlisted)
	}
	listed := list(context.Background(), heldTwo(), allPlan, testRun, env(map[string]string{"KIT_PROJECT": "p"}))
	if code := status(allPlan, listed); code != 0 {
		t.Fatalf("exit %d; a type that the plan cannot list is not a failed listing", code)
	}
	if exitUnlisted == 1 || exitUnlisted == 2 {
		t.Fatal("exitUnlisted must differ from a failed delete and from a refusal")
	}
	var stdout, stderr bytes.Buffer
	run(context.Background(), []string{"--run-id", testRun}, &stdout, &stderr, env(nil),
		func(context.Context) (Client, error) { return heldTwo(), nil })
	if !strings.Contains(stderr.String(), "types not listed") {
		t.Fatalf("the summary does not count cannot_list: %s", stderr.String())
	}
}

func TestAResourceTheNCPKeepsAsDeletedIsNotFound(t *testing.T) {
	kept := group
	kept.RunFields, kept.DeletedField, kept.DeletedValue = []string{"key.id"}, "state", "DELETED"
	if got := strings.Join(kept.ListedFields(), ","); got != "key.id,state" {
		t.Fatalf("ListedFields is %s; the adapter returns each run field and the deleted field", got)
	}
	gone, live := "tfkit-"+testRun+"-gone", "tfkit-"+testRun+"-group"
	client := heldTwo()
	client.held["kit_group|"] = []Resource{
		{Name: gone, Fields: map[string]string{"state": "DELETED"}},
		{Name: live, Fields: map[string]string{"state": "ACTIVE"}},
		{Name: "tfkit-zzz999zzz9999-group", Fields: map[string]string{"state": "DELETED"}},
	}
	client.held["kit_member|"+gone] = []Resource{{Name: "members/tfkit-" + testRun + "-kept"}}
	plan := []Type{member, kept}
	record := list(context.Background(), client, plan, testRun, env(nil))
	if got := strings.Join(names(record.Deleted), ", "); got != "kit_group "+gone {
		t.Fatalf("deleted is %s; only the run's resource that holds the marker", got)
	}
	if record.Deleted[0].Fields["state"] != "DELETED" {
		t.Fatalf("the deleted record does not show its field: %v", record.Deleted[0])
	}
	want := "kit_group " + live + ", kit_member members/tfkit-" + testRun + "-member, kit_member members/tfkit-" + testRun + "-kept"
	if got := strings.Join(names(record.Found), ", "); got != want {
		t.Fatalf("found %s\nwant  %s; a deleted resource is still a scope", got, want)
	}
	if record := list(context.Background(), client, []Type{group}, testRun, env(nil)); len(record.Found) != 2 {
		t.Fatalf("found %v; a type with no marker finds each resource of the run", names(record.Found))
	}
	approved := record
	approved.Found = append(approved.Found, Found{Type: "kit_group", Name: gone, Matched: "name"})
	swept := sweep(context.Background(), client, plan, testRun, env(nil), approved)
	for _, c := range client.calls {
		if c.verb == "delete" && c.name == gone {
			t.Fatal("the sweep deleted a resource that the NCP keeps as deleted")
		}
	}
	if len(swept.Left) != 0 {
		t.Fatalf("left is %v; a resource that the NCP keeps as deleted is not left", names(swept.Left))
	}
}

func TestANotFoundUnderADeletedScopeHoldsNothing(t *testing.T) {
	kept := group
	kept.DeletedField, kept.DeletedValue = "state", "DELETED"
	gone, live := "tfkit-"+testRun+"-gone", "tfkit-"+testRun+"-group"
	missing := fmt.Errorf("HTTP 404: %w", ErrNotFound)
	client := &fakeClient{held: map[string][]Resource{"kit_group|": {
		{Name: gone, Fields: map[string]string{"state": "DELETED"}},
		{Name: live, Fields: map[string]string{"state": "ACTIVE"}},
	}}, fail: map[string]error{"kit_member|" + gone: missing}}
	plan := []Type{member, kept}
	record := list(context.Background(), client, plan, testRun, env(nil))
	if len(record.CannotList) != 0 || status(plan, record) != 0 {
		t.Fatalf("cannot_list is %v; the NCP does not find a scope that it deleted, so nothing is there", record.CannotList)
	}
	for name, err := range map[string]error{gone: errors.New("HTTP 403: denied"), live: missing} {
		client.fail = map[string]error{"kit_member|" + name: err}
		record = list(context.Background(), client, plan, testRun, env(nil))
		if len(record.CannotList) != 1 || status(plan, record) != exitUnlisted {
			t.Errorf("the failed list in %s (%v) is not under cannot_list: %v", name, err, record.CannotList)
		}
	}
}

// The derived plan deletes a type before each type that it is listed under: a
// resource cannot outlive the one that it sits in.
func TestThePlanDeletesATypeBeforeItsScope(t *testing.T) {
	at := map[string]int{}
	for i, typ := range plan {
		at[typ.Name] = i
	}
	for i, typ := range plan {
		scopes := []string{typ.ByParent}
		for _, p := range typ.Params {
			scopes = append(scopes, p.ScopeType)
		}
		for _, scope := range scopes {
			if j, ok := at[scope]; ok && j < i {
				t.Errorf("%s is deleted after %s, which it is listed under", typ.Name, scope)
			}
		}
	}
}
"""
