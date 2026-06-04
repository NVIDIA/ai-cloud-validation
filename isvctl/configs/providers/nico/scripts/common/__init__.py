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

"""Shared Python utilities for NICo provider scripts.

Every NICo script reaches this package via a single ``sys.path`` entry -
``providers/nico/scripts/`` - so ``from common.X import Y`` resolves without
any namespace-package juggling. Modules:

- ``nico_client``: authenticated NICo (Forge) REST API helpers with pagination,
  URL encoding, and machine health/capability parsing utilities.
"""
