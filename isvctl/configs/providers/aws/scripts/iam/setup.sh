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

# AWS IAM Setup Stub - Queries AWS IAM system and outputs inventory JSON
#
# ISVs should replace this with their own implementation that queries their AWS IAM system.
#
# Requirements:
#   - jq for JSON processing
#   - Access to the IAM API (via environment variables)
#
# Environment Variables:
#   - IAM_API_ENDPOINT: Base URL of the IAM API (required)
#   - IAM_API_KEY: API key for authentication (optional, depends on IAM system)
#   - IAM_PROVIDER: Provider name (e.g., 'aws-iam', 'okta', 'custom')
#
# Output: JSON inventory conforming to isvctl iam schema

set -eo pipefail

# Check jq is available
if ! command -v jq &> /dev/null; then
    echo "Error: jq not found - required for JSON processing" >&2
    exit 1
fi

# Get configuration from environment
IAM_API_ENDPOINT="${IAM_API_ENDPOINT:-}"
IAM_PROVIDER="${IAM_PROVIDER:-aws-iam}"
CLUSTER_NAME="${IAM_CLUSTER_NAME:-iam-system}"

# Validate required configuration
if [ -z "$IAM_API_ENDPOINT" ]; then
    echo "Warning: IAM_API_ENDPOINT not set - using mock data for demonstration" >&2

    # Output mock inventory for demonstration
    cat << EOF
{
  "success": true,
  "platform": "iam",
  "cluster_name": "${CLUSTER_NAME}",
  "iam": {
    "user_count": 0,
    "roles": ["admin", "user", "readonly"],
    "supports_mfa": true,
    "supports_service_accounts": true,
    "auth_methods": ["password", "oauth"]
  },
  "aws": {
    "region": "${AWS_REGION:-us-east-1}",
    "account_id": "",
    "iam_provider": "${IAM_PROVIDER}",
    "api_endpoint": "https://api.example.com/iam"
  }
}
EOF
    exit 0
fi

# Query the IAM API for system information
# ISVs should implement their own API calls here
echo "Querying IAM system at ${IAM_API_ENDPOINT}..." >&2

# Example: Query user count
USER_COUNT=0
if command -v curl &> /dev/null; then
    # Example API call - ISVs should customize this
    RESPONSE=$(curl -s -f "${IAM_API_ENDPOINT}/users" \
        -H "Authorization: Bearer ${IAM_API_KEY:-}" \
        -H "Content-Type: application/json" 2>/dev/null || echo '{"users":[]}')
    USER_COUNT=$(echo "$RESPONSE" | jq -r '.users | length // 0' 2>/dev/null || echo "0")
fi

# Example: Query available roles
ROLES='["admin", "user", "readonly"]'
if command -v curl &> /dev/null; then
    RESPONSE=$(curl -s -f "${IAM_API_ENDPOINT}/roles" \
        -H "Authorization: Bearer ${IAM_API_KEY:-}" \
        -H "Content-Type: application/json" 2>/dev/null || echo '{"roles":[]}')
    ROLES=$(echo "$RESPONSE" | jq -r '.roles // ["admin", "user", "readonly"]' 2>/dev/null || echo '["admin", "user", "readonly"]')
fi

# Get AWS account ID if available
AWS_ACCOUNT_ID=""
if command -v aws &> /dev/null; then
    AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "")
fi

# Output JSON inventory
cat << EOF
{
  "success": true,
  "platform": "iam",
  "cluster_name": "${CLUSTER_NAME}",
  "iam": {
    "user_count": ${USER_COUNT},
    "roles": ${ROLES},
    "supports_mfa": true,
    "supports_service_accounts": true,
    "auth_methods": ["password", "oauth"]
  },
  "aws": {
    "region": "${AWS_REGION:-us-east-1}",
    "account_id": "${AWS_ACCOUNT_ID}",
    "iam_provider": "${IAM_PROVIDER}",
    "api_endpoint": "${IAM_API_ENDPOINT}"
  }
}
EOF
