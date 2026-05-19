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

# AWS ISO/VMDK Import Tests Setup Stub
# Returns minimal AWS inventory for ISO import validation tests.
#
# All ISO tests are SELF-CONTAINED - they create their own S3 buckets,
# import images, and clean up after. No pre-existing infrastructure is required.
#
# Environment Variables:
#   - AWS_REGION: AWS region (default: us-west-2)

set -eo pipefail

# -----------------------------------------------------------------------------
# Check Dependencies
# -----------------------------------------------------------------------------

if ! command -v aws &> /dev/null; then
    echo "Error: AWS CLI not found" >&2
    exit 1
fi

if ! command -v jq &> /dev/null; then
    echo "Error: jq not found (required for JSON parsing)" >&2
    exit 1
fi

# Verify AWS credentials
if ! aws sts get-caller-identity &> /dev/null; then
    echo "Error: AWS credentials not configured or invalid" >&2
    echo "" >&2
    echo "Configure credentials using one of:" >&2
    echo "  - aws configure" >&2
    echo "  - export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=..." >&2
    echo "  - IAM instance role" >&2
    exit 1
fi

echo "========================================" >&2
echo "  AWS ISO/VMDK Import Validation Setup" >&2
echo "========================================" >&2
echo "" >&2

# Get caller identity (credentials already verified above)
CALLER_IDENTITY=$(aws sts get-caller-identity --output json)
if [[ -z "$CALLER_IDENTITY" ]]; then
    echo "Error: Failed to get AWS caller identity" >&2
    exit 1
fi

ACCOUNT_ID=$(echo "$CALLER_IDENTITY" | jq -r '.Account')
if [[ -z "$ACCOUNT_ID" || "$ACCOUNT_ID" == "null" ]]; then
    echo "Error: Failed to parse AWS account ID from caller identity" >&2
    exit 1
fi
echo "AWS Account: $ACCOUNT_ID" >&2

# -----------------------------------------------------------------------------
# Get Configuration
# -----------------------------------------------------------------------------

AWS_REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || echo "us-west-2")}"
echo "Region: $AWS_REGION" >&2

echo "" >&2
echo "All ISO import tests are self-contained and will:" >&2
echo "  - Download VMDK images from Ubuntu cloud" >&2
echo "  - Create temporary S3 buckets for import" >&2
echo "  - Import VMDK as AMI via VM Import" >&2
echo "  - Launch GPU instances for validation" >&2
echo "  - Clean up all resources after tests complete" >&2
echo "" >&2
echo "No pre-existing S3 bucket or infrastructure required." >&2
echo "" >&2

# -----------------------------------------------------------------------------
# Output JSON Inventory
# -----------------------------------------------------------------------------

cat << EOF
{
  "platform": "image_registry",
  "cluster_name": "aws-image-registry-validation",
  "iso": {
    "provider": "aws_vm_import",
    "region": "${AWS_REGION}",
    "account_id": "${ACCOUNT_ID}",
    "default_image_url": "https://cloud-images.ubuntu.com/releases/noble/release/ubuntu-24.04-server-cloudimg-amd64.vmdk",
    "supported_formats": ["vmdk", "vhd", "ova", "raw"],
    "gpu_instance_types": ["g4dn.xlarge"]
  }
}
EOF
