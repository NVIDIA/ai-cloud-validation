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
import os
import socket
import time

import torch

h = socket.gethostname()
r = int(os.getenv("GPU_STRESS_RUNTIME", "30"))
m = int(os.getenv("GPU_MEMORY_GB", "16"))
n = torch.cuda.device_count()
if n == 0:
    print(f"FAILURE: No GPUs on {h}")
    exit(1)
print(f"{h}: {n} GPUs, runtime={r}s, memory={m}GB")
sz = int((m * 1e9 / 4 / 4) ** 0.5)
a = [torch.randn(sz, sz, device=f"cuda:{i}", dtype=torch.float32) for i in range(n)]
t0 = time.time()
loops = 0
while time.time() - t0 < r:
    for x in a:
        torch.mm(x, x)
    loops += 1
print(f"SUCCESS: {h} completed {loops} loops with {n} GPU(s)")
