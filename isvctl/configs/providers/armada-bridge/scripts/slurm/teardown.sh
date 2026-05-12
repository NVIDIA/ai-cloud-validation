#!/usr/bin/env bash
# teardown.sh — Armada Bridge Slurm suite, teardown phase.
#
# Bridge endpoint: DELETE /orchestrator/tenants/:tenant/slurm/:slurmID

set -euo pipefail

echo '{"success": false, "error": "Not implemented: Bridge Slurm teardown requires DELETE /orchestrator/tenants/:tenant/slurm/:slurmID."}'
exit 1
