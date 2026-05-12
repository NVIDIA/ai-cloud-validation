#!/usr/bin/env bash
# setup.sh — Armada Bridge k8s suite, setup phase.
#
# Bridge endpoint: POST /orchestrator/tenants/:tenant/clusters
# Then: GET /orchestrator/tenants/:tenant/clusters/:clusterID/kubeconfig
# Writes kubeconfig to $KUBECONFIG_PATH and outputs:
#   {"success": true, "cluster_id": "...", "kubeconfig_path": "..."}
#
# NOT IMPLEMENTED: echo error JSON and exit 1 until live implementation.

set -euo pipefail

echo '{"success": false, "error": "Not implemented: Bridge k8s setup requires POST /orchestrator/tenants/:tenant/clusters + kubeconfig fetch. See bridge-isv-ncp-status.md Kubernetes suite."}'
exit 1
