#!/usr/bin/env bash
# setup.sh — Armada Bridge Slurm suite, setup phase.
#
# Bridge endpoint: POST /orchestrator/tenants/:tenant/slurm
# Then SSH into cluster head node with provisioned credentials.
# Output: {"success": true, "cluster_id": "...", "slurm_host": "..."}

set -euo pipefail

echo '{"success": false, "error": "Not implemented: Bridge Slurm setup requires POST /orchestrator/tenants/:tenant/slurm. See bridge-isv-ncp-status.md Slurm suite."}'
exit 1
