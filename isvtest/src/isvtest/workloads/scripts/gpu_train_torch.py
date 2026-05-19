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

# /// script
# requires-python = ">=3.12"
# dependencies = [
#   'torch>=2.8.0',
# ]
#
# [tool.uv]
# extra-index-url = ["https://download.pytorch.org/whl/cu129"]
# ///
"""DDP training validation script.

Launched via ``torchrun --nproc_per_node=<gpus>``.  Each process trains
on one GPU using DistributedDataParallel, which synchronises gradients
across all GPUs via NCCL AllReduce every step - the same communication
path real training workloads use.

Validates:
  - Forward / backward / optimizer work on every GPU
  - NCCL gradient sync completes without error
  - Weights stay identical across ranks (DDP invariant)
"""

import os
import socket

import torch
import torch.distributed as dist
import torch.nn as nn
from torch.nn.parallel import DistributedDataParallel as DDP

h = socket.gethostname()
steps = int(os.getenv("TRAIN_STEPS", "50"))
batch = int(os.getenv("TRAIN_BATCH_SIZE", "64"))
hidden = int(os.getenv("TRAIN_HIDDEN_SIZE", "2048"))
lr = float(os.getenv("TRAIN_LR", "0.01"))

rank = int(os.environ["LOCAL_RANK"])
world = int(os.environ["WORLD_SIZE"])

torch.cuda.set_device(rank)
dev = f"cuda:{rank}"
dist.init_process_group(backend="nccl")

if rank == 0:
    print(f"{h}: {world} GPUs (DDP), steps={steps}, batch={batch}, hidden={hidden}")

model = nn.Sequential(
    nn.Linear(hidden, hidden),
    nn.ReLU(),
    nn.Linear(hidden, hidden),
    nn.ReLU(),
    nn.Linear(hidden, 10),
).to(dev)
ddp_model = DDP(model, device_ids=[rank])
optimizer = torch.optim.SGD(ddp_model.parameters(), lr=lr)
loss_fn = nn.CrossEntropyLoss()

first_loss = 0.0
last_loss = 0.0
for step in range(steps):
    x = torch.randn(batch, hidden, device=dev)
    target = torch.randint(0, 10, (batch,), device=dev)
    optimizer.zero_grad()
    loss = loss_fn(ddp_model(x), target)
    loss.backward()
    optimizer.step()
    lv = loss.item()
    if step == 0:
        first_loss = lv
    last_loss = lv

has_grads = all(p.grad is not None and p.grad.abs().sum().item() > 0 for p in ddp_model.parameters() if p.requires_grad)
decreased = last_loss < first_loss

# Verify DDP kept weights in sync: compare rank 0 params with every other rank
params_flat = torch.cat([p.detach().flatten() for p in model.parameters()])
if rank == 0:
    ref = params_flat.clone()
else:
    ref = torch.zeros_like(params_flat)
dist.broadcast(ref, src=0)
weights_match = torch.allclose(params_flat, ref, atol=1e-6)

# Gather results to rank 0
result = torch.tensor(
    [float(has_grads), float(decreased), float(weights_match)],
    device=dev,
)
gathered = [torch.zeros(3, device=dev) for _ in range(world)] if rank == 0 else None
dist.gather(result, gather_list=gathered, dst=0)

if rank == 0 and gathered is not None:
    all_ok = True
    for i, g in enumerate(gathered):
        grads_ok = bool(g[0].item())
        dec = bool(g[1].item())
        sync = bool(g[2].item())
        status = "ok" if (grads_ok and sync) else "WARN"
        print(
            f"  GPU {i}: loss {first_loss:.4f} -> {last_loss:.4f} "
            f"(decreased={dec}, grads={grads_ok}, synced={sync}) [{status}]"
        )
        if not grads_ok:
            all_ok = False
        if not sync:
            all_ok = False

    if not all_ok:
        print(f"FAILURE: Training validation failed on {h}")
        dist.destroy_process_group()
        exit(1)

    print(f"SUCCESS: {h} trained {steps} steps on {world} GPU(s) with DDP")

dist.destroy_process_group()
