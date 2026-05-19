#!/bin/bash
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

# Identity/IAM Teardown Stub - Cleans up test artifacts
#
# ISVs should replace this with their own implementation.
# This script should clean up any test users, tokens, or other resources
# created during the test run.
#
# Environment Variables:
#   - IAM_API_ENDPOINT: Base URL of the IAM API
#   - IAM_API_KEY: API key for authentication
#   - IAM_CLEANUP_PREFIX: Prefix for test users to clean up (default: test-user-)
#
# Output: JSON with cleanup result

set -eo pipefail

# Get configuration from environment
IAM_API_ENDPOINT="${IAM_API_ENDPOINT:-}"
IAM_API_KEY="${IAM_API_KEY:-}"
IAM_CLEANUP_PREFIX="${IAM_CLEANUP_PREFIX:-test-user-}"

echo "Identity teardown starting..." >&2

# If no API endpoint, just log and exit
if [ -z "$IAM_API_ENDPOINT" ]; then
    echo "IAM_API_ENDPOINT not set - nothing to clean up" >&2
    echo '{"success": true, "cleaned_up": 0}'
    exit 0
fi

echo "Cleaning up test users with prefix '${IAM_CLEANUP_PREFIX}'..." >&2

CLEANED_UP=0

# List and delete test users
# ISVs should customize this for their IAM API
if command -v curl &> /dev/null && command -v jq &> /dev/null; then
    RESPONSE=$(curl -s -f "${IAM_API_ENDPOINT}/users?prefix=${IAM_CLEANUP_PREFIX}" \
        -H "Authorization: Bearer ${IAM_API_KEY}" \
        -H "Content-Type: application/json" 2>/dev/null || echo '{"users":[]}')

    # Extract user IDs and delete each
    USER_IDS=$(echo "$RESPONSE" | jq -r '.users[]?.id // empty' 2>/dev/null || true)

    for USER_ID in $USER_IDS; do
        echo "  Deleting user: ${USER_ID}" >&2
        curl -s -f -X DELETE "${IAM_API_ENDPOINT}/users/${USER_ID}" \
            -H "Authorization: Bearer ${IAM_API_KEY}" \
            -H "Content-Type: application/json" 2>/dev/null || true
        CLEANED_UP=$((CLEANED_UP + 1))
    done
fi

echo "Cleanup complete. Removed ${CLEANED_UP} test user(s)." >&2

cat << EOF
{
  "success": true,
  "platform": "iam",
  "message": "Cleanup complete",
  "resources_deleted": {"users": {"count": ${CLEANED_UP}}},
  "aws": {
    "region": "${AWS_REGION:-us-east-1}",
    "cleanup_prefix": "${IAM_CLEANUP_PREFIX}"
  }
}
EOF
