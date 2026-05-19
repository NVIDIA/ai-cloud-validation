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

"""Shared Python utilities for AWS stub scripts.

Every AWS script reaches this package via a single ``sys.path`` entry -
``providers/aws/scripts/`` - so ``from common.X import Y`` resolves
without any namespace-package juggling. Modules:

- ``ec2``: key pair / security group / public IP helpers
- ``errors``: AWS error classification, ``delete_with_retry``, and the
  ``handle_aws_errors`` decorator used by every script's ``main()``
- ``ssh_utils``: ``wait_for_ssh`` reachability probe (shared with no
  other provider today; moved here from ``providers/shared/`` so all
  AWS imports live under a single ``common`` package)
- ``serial_console``: boto3 serial-console connectivity helper
- ``vpc``: VPC / subnet / SG creation + retry-backed teardown helpers
"""
