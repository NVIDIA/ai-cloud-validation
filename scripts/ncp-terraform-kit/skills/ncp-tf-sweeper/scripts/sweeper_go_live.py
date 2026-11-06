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
"""The Go of the sweeper's live check, and the tests of a list that lags
behind a delete. Those tests use the fake client of the core's tests."""

from __future__ import annotations

LIVE_CHECK = r"""package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"testing"
	"time"
)

// Creator is what the live check needs beyond Client: adapter.go implements it
// so that the check can make one resource of the run and one of another run.
type Creator interface {
	Create(ctx context.Context, t Type, scope map[string]string, name string) (Resource, error)
}

// A list can return a resource for a while after the NCP's read reports it
// gone, so one listing cannot prove that a delete worked. The check lists
// again, relistEvery apart, up to relistTries times. Five minutes is far
// beyond a list that is only behind, and short enough to wait for.
const (
	relistEvery = 10 * time.Second
	relistTries = 30
)

// sweptOnly lists the scope until the run's resource is gone or the tries run
// out, and says what the last listing shows that is wrong: the run's resource
// still there, or the other run's resource gone.
func sweptOnly(ctx context.Context, client Client, t Type, scope map[string]string, mine, theirs string,
	tries int, pause func()) error {
	var present map[string]bool
	for try := 0; try < tries; try++ {
		if try > 0 {
			pause()
		}
		remaining, err := client.List(ctx, t, scope)
		if err != nil {
			return err
		}
		present = map[string]bool{}
		for _, r := range remaining {
			present[r.Name] = true
		}
		if !present[mine] {
			break
		}
	}
	if present[mine] {
		return fmt.Errorf("%s carries the run id and is still listed after %d listings", mine, tries)
	}
	if !present[theirs] {
		return fmt.Errorf("%s belongs to another run and was deleted", theirs)
	}
	return nil
}

// TestSweeperLiveCheck deletes a resource of the run and leaves a resource of
// another run. It creates and deletes real resources, so it runs only when the
// operator sets TFKIT_SWEEPER_LIVE=true, with TFKIT_RUN_ID and the planned type
// to check in TFKIT_SWEEPER_LIVE_TYPE.
func TestSweeperLiveCheck(t *testing.T) {
	if os.Getenv("TFKIT_SWEEPER_LIVE") != "true" {
		t.Skip("the live check creates and deletes resources; set TFKIT_SWEEPER_LIVE=true to run it")
	}
	runID, kind := os.Getenv("TFKIT_RUN_ID"), os.Getenv("TFKIT_SWEEPER_LIVE_TYPE")
	if !runIDForm.MatchString(runID) {
		t.Fatal("TFKIT_RUN_ID is unset or malformed")
	}
	var checked Type
	for _, candidate := range plan {
		if candidate.Name == kind {
			checked = candidate
		}
	}
	if checked.Name == "" || checked.CannotList != "" || checked.CannotDelete != "" {
		t.Fatalf("TFKIT_SWEEPER_LIVE_TYPE=%q is not a planned type that the sweeper lists and deletes", kind)
	}
	ctx := context.Background()
	client, err := NewClient(ctx)
	if err != nil {
		t.Fatal(err)
	}
	creator, ok := client.(Creator)
	if !ok {
		t.Fatal("the adapter's client has no Create method, so the live check cannot make its two resources")
	}
	if err := liveCheck(ctx, client, creator, checked, runID, func() { time.Sleep(relistEvery) }); err != nil {
		t.Error(err)
	}
}

// liveCheck makes one resource of the run and one of another run, sweeps with
// an approval of the run's resource only, and reports what the sweep got
// wrong. The listing of the run id can hold a resource that the check did not
// make, and no operator approved that one.
func liveCheck(ctx context.Context, client Client, creator Creator, checked Type, runID string, pause func()) (failed error) {
	listed, err := scopes(checked, runID, nil, os.Getenv)
	if err != nil || len(listed) == 0 {
		return fmt.Errorf("no scope to create in: %v", err)
	}
	other := "zz" + runID[2:]
	if other == runID {
		other = "yy" + runID[2:]
	}
	mine, err := creator.Create(ctx, checked, listed[0], kitPrefix+checked.Separator+runID+checked.Separator+"sweepcheck")
	if err != nil {
		return err
	}
	theirs, err := creator.Create(ctx, checked, listed[0], kitPrefix+checked.Separator+other+checked.Separator+"sweepcheck")
	if err != nil {
		return err
	}
	defer func() {
		if err := client.Delete(ctx, checked, listed[0], theirs); err != nil {
			failed = errors.Join(failed, fmt.Errorf("deleting the other run's resource after the check: %v", err))
		}
	}()
	approved := Record{RunID: runID, Found: []Found{{Type: checked.Name, Name: mine.Name, Scope: listed[0]}}}
	record := sweep(ctx, client, []Type{checked}, runID, os.Getenv, approved)
	if err := sweptOnly(ctx, client, checked, listed[0], mine.Name, theirs.Name, relistTries, pause); err != nil {
		return fmt.Errorf("%v: %+v", err, record)
	}
	return nil
}
"""

LAGGING_TESTS = r"""package main

import (
	"context"
	"strings"
	"testing"
)

// ghost is a deleted resource that a lagging list still returns.
type ghost struct {
	key      string
	resource Resource
	lists    int
}

// laggingClient deletes as the fake does, and its List returns each deleted
// resource for lag more calls, as a list that is behind the NCP's read does.
type laggingClient struct {
	*fakeClient
	lag    int
	ghosts []*ghost
}

func (l *laggingClient) Delete(ctx context.Context, t Type, scope map[string]string, r Resource) error {
	if err := l.fakeClient.Delete(ctx, t, scope, r); err != nil {
		return err
	}
	l.ghosts = append(l.ghosts, &ghost{key: l.key(t, scope), resource: r, lists: l.lag})
	return nil
}

func (l *laggingClient) List(ctx context.Context, t Type, scope map[string]string) ([]Resource, error) {
	listed, err := l.fakeClient.List(ctx, t, scope)
	for _, g := range l.ghosts {
		if g.key == l.key(t, scope) && g.lists > 0 {
			listed = append(listed, g.resource)
			g.lists--
		}
	}
	return listed, err
}

func TestADeleteThatReturnedIsNotLeftByALaggingList(t *testing.T) {
	client := &laggingClient{fakeClient: heldTwo(), lag: 3}
	getenv := env(map[string]string{"KIT_PROJECT": "p"})
	approved := list(context.Background(), client, allPlan, testRun, getenv)
	record := sweep(context.Background(), client, allPlan, testRun, getenv, approved)
	if len(record.Removed) != 3 {
		t.Fatalf("removed %v", names(record.Removed))
	}
	if got := strings.Join(names(record.Left), ", "); got != "kit_pinned tfkit-"+testRun+"-pinned" {
		t.Fatalf("left is %s; a resource whose delete returned is gone, though a lagging list still shows it", got)
	}
}

func TestTheLiveCheckListsAgainWhileTheListLags(t *testing.T) {
	mine, theirs := "tfkit-"+testRun+"-group", "tfkit-zzz999zzz9999-group"
	client := &laggingClient{fakeClient: heldTwo(), lag: 3}
	if err := client.Delete(context.Background(), group, nil, Resource{Name: mine}); err != nil {
		t.Fatal(err)
	}
	pauses := 0
	err := sweptOnly(context.Background(), client, group, nil, mine, theirs, relistTries, func() { pauses++ })
	if err != nil || pauses != 3 {
		t.Fatalf("%v after %d pauses; the check lists again while the list still shows the deleted resource", err, pauses)
	}
	never := func() {}
	if err := sweptOnly(context.Background(), heldTwo(), group, nil, mine, theirs, relistTries, never); err == nil {
		t.Fatal("a resource of the run that was never deleted passed the check")
	}
	both := heldTwo()
	both.held["kit_group|"] = nil
	if err := sweptOnly(context.Background(), both, group, nil, mine, theirs, relistTries, never); err == nil ||
		!strings.Contains(err.Error(), "belongs to another run") {
		t.Fatalf("the other run's resource is gone, and the check says %v", err)
	}
}

// creatingClient adds the Create method that the live check needs.
type creatingClient struct{ *fakeClient }

func (c creatingClient) Create(_ context.Context, t Type, scope map[string]string, name string) (Resource, error) {
	r := Resource{Name: name}
	key := c.key(t, scope)
	c.held[key] = append(c.held[key], r)
	return r, nil
}

func TestTheLiveCheckDeletesOnlyWhatItCreated(t *testing.T) {
	earlier := "tfkit-" + testRun + "-earlier"
	client := creatingClient{&fakeClient{held: map[string][]Resource{"kit_group|": {{Name: earlier}}}}}
	if err := liveCheck(context.Background(), client, client, group, testRun, func() {}); err != nil {
		t.Fatal(err)
	}
	for _, c := range client.calls {
		if c.verb == "delete" && c.name == earlier {
			t.Fatal("the live check deleted a resource of the run id that it did not create")
		}
	}
}
"""
