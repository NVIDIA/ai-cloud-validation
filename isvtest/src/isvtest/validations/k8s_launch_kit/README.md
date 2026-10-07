<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# k8s_launch_kit

Network Operator checks backed by
[Kubernetes Launch Kit](https://github.com/NVIDIA/k8s-launch-kit). For what
they need and how to run them, see the
[`network_operator` group](../../../../../isvctl/configs/suites/README.md#network-operator-network_operator-group).

## How it works

- [`runner.py`](runner.py): `run_launch_kit()` runs
  `l8k validate --junit-path ... --user-config ... --deployment-files ... --output json`,
  then `l8k sosreport --output-dir ...`, once per input set and pytest session.
  A missing input or `l8k` returns a `skip_reason` and runs nothing.
- [`checks.py`](checks.py): each catalog check reads one native suite from that
  JUnit (`network/validation` or `K8s<Family>-<fabric>`) and reports its cases as
  subtests. `LaunchKitSosreport` reports sosreport and is catalog-excluded.
- `l8k validate` has no deadline, because Launch Kit bounds its own matrix.
  `l8k sosreport` gets 30 minutes, because it does not.

Evidence, relative to the isvctl working directory:

```text
_output/k8s-launch-kit/
  launch-kit-junit.xml
  k8s-launch-kit-validation-report.html   # copy of the report Launch Kit advertises
  commands/{validate,sosreport}/          # command.json, stdout.txt, stderr.log
  sosreport.tar.gz                        # or sosreport/ if the helper failed first
  work/                                   # l8k working directory
```

Stale JUnit, HTML report, and sosreport archive are removed before each run.

Tests run against a mock `l8k`: [`isvtest/tests/k8s_launch_kit/`](../../../../tests/k8s_launch_kit/)
for the runner and checks, and
[`isvctl/tests/test_k8s_launch_kit.py`](../../../../../isvctl/tests/test_k8s_launch_kit.py)
for the network suite end to end.

## Rules for changes

- Do not add discover, generate, deploy, clean, or other lifecycle operations;
  they are prerequisites owned by Launch Kit and the ISV. Tests that change
  Network Operator state need a separate restore design first.
- `l8k clean` is the only supported deletion path; never reproduce Launch Kit
  cleanup with kubectl.
- Do not duplicate Launch Kit flags, schema, or defaults, and never parse the
  user config.
- A missing prerequisite skips. Once `l8k validate` has started, a failed
  command or missing or malformed JUnit fails; a suite with no executed case
  skips with Launch Kit's reason. Never pass without results.
- Do not reinterpret Launch Kit's verdict.
- The PRD source is
  [`network-operator-readiness-requirements.yaml`](../../../../../docs/requirements/network-operator-readiness-requirements.yaml);
  regenerate the committed views with `make plan`.
