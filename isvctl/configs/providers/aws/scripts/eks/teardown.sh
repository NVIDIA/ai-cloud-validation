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

# AWS EKS Teardown Stub - Destroys AWS infrastructure using Terraform
#
# Environment Variables:
#   - AWS_SKIP_TEARDOWN: Set to "true" to skip teardown and preserve resources (default: false)
#   - TF_AUTO_APPROVE: Set to "true" to skip confirmation (default: false)
#
# Warning: This will permanently delete all AWS resources!

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TERRAFORM_DIR="${SCRIPT_DIR}/terraform"

AWS_SKIP_TEARDOWN="${AWS_SKIP_TEARDOWN:-false}"
AWS_REGION="${AWS_REGION:-us-west-2}"
if [ "$AWS_SKIP_TEARDOWN" = "true" ]; then
    echo "" >&2
    echo "========================================" >&2
    echo "  TEARDOWN SKIPPED - Resources Preserved" >&2
    echo "========================================" >&2
    echo "" >&2
    echo "AWS infrastructure was NOT destroyed." >&2
    echo "Your EKS cluster and resources are still running." >&2
    echo "" >&2
    echo "To destroy resources, run without AWS_SKIP_TEARDOWN:" >&2
    echo "  uv run isvctl test run -f isvctl/configs/providers/aws/config/eks.yaml --phase teardown" >&2
    echo "" >&2
    echo "Or manually:" >&2
    echo "  cd isvctl/configs/providers/aws/scripts/eks/terraform" >&2
    echo "  terraform destroy" >&2
    echo "" >&2
    cat << EOF
{
  "success": true,
  "platform": "kubernetes",
  "skipped": true,
  "message": "Teardown skipped (AWS_SKIP_TEARDOWN=true)",
  "aws": {
    "region": "${AWS_REGION}"
  }
}
EOF
    exit 0
fi

# Check terraform
if ! command -v terraform &> /dev/null; then
    echo "Error: terraform not found" >&2
    exit 1
fi

cd "$TERRAFORM_DIR"

echo "" >&2
echo "========================================" >&2
echo "  DESTROYING AWS INFRASTRUCTURE" >&2
echo "========================================" >&2
echo "" >&2

# Initialize if needed
if [ ! -d ".terraform" ]; then
    echo "Initializing Terraform..." >&2
    terraform init >&2
fi

# -----------------------------------------------------------------------------
# Delete standalone EBS volume(s) created for static CSI validation
# -----------------------------------------------------------------------------
# These volumes are provisioned by setup.sh out-of-band (not via Terraform),
# so terraform destroy would leave them orphaned. We look them up by the
# tags setup.sh writes, wait for them to detach (EBS CSI may still have
# mounted them during a previous test run), and delete them before the
# Terraform destroy pulls the cluster out from under them.

if command -v aws &> /dev/null && command -v jq &> /dev/null; then
    CLUSTER_NAME=$(terraform output -raw cluster_name 2>/dev/null || echo "")
    if [ -z "$CLUSTER_NAME" ]; then
        echo "Warning: could not resolve terraform output 'cluster_name'; skipping static CSI EBS cleanup." >&2
        STATIC_VOLS=""
    else
        STATIC_VOLS=$(aws ec2 describe-volumes \
            --filters \
                "Name=tag:isv-ncp-validation-suite,Values=static-csi" \
                "Name=tag:cluster,Values=${CLUSTER_NAME}" \
            --region "$AWS_REGION" --output json 2>/dev/null \
            | jq -r '.Volumes[].VolumeId' 2>/dev/null || echo "")
    fi

    if [ -n "$STATIC_VOLS" ]; then
        for vol in $STATIC_VOLS; do
            echo "Releasing standalone static-CSI EBS volume $vol..." >&2
            aws ec2 wait volume-available --volume-ids "$vol" --region "$AWS_REGION" >&2 2>&1 || true
            if aws ec2 delete-volume --volume-id "$vol" --region "$AWS_REGION" >&2 2>&1; then
                echo "  Deleted $vol" >&2
            else
                echo "  Warning: failed to delete $vol; manual cleanup may be required" >&2
            fi
        done
    fi
fi

# Destroy
TF_AUTO_APPROVE="${TF_AUTO_APPROVE:-false}"
if [ "$TF_AUTO_APPROVE" = "true" ]; then
    echo "Running terraform destroy (auto-approved)..." >&2
    terraform destroy -auto-approve >&2
else
    echo "Running terraform destroy..." >&2
    terraform destroy >&2
fi

echo "" >&2
echo "========================================" >&2
echo "  TEARDOWN COMPLETE" >&2
echo "========================================" >&2
echo "All AWS resources have been destroyed." >&2

# Output JSON result
AWS_REGION="${AWS_REGION:-us-west-2}"
cat << EOF
{
  "success": true,
  "platform": "kubernetes",
  "message": "EKS cluster and AWS resources destroyed",
  "resources_deleted": ["eks_cluster", "node_groups", "vpc_resources"],
  "aws": {
    "region": "${AWS_REGION}"
  }
}
EOF
