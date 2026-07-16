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

# A single test node pool on the primary GKE cluster (the GCP analog of the AWS
# EKS oracle's terraform-node-pool module). Attaches a
# google_container_node_pool to the cluster already provisioned by
# ../terraform, exercising the node-pool create / scale / destroy leg.
#
# The module keeps its own local state file (selected by create_node_pool.py
# via -state=<file>) so multiple coexisting pools (CPU vs GPU) never clobber one
# another. Cluster wiring (name, location) is read from the primary cluster's
# state via terraform_remote_state at CREATE (the eks idiom), and PERSISTED into
# this pool's own state (echoed as outputs + threaded back as fallback vars) so a
# DESTROY still resolves after the primary state's outputs are gone — best-effort
# teardown may destroy the primary before a transient dependent-destroy retry.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0"
    }
  }

  backend "local" {
    path = "terraform.tfstate"
  }
}

provider "google" {
  project = var.project
}

data "terraform_remote_state" "cluster" {
  backend = "local"
  config = {
    path = var.cluster_state_path
  }
}

locals {
  # Prefer the LIVE primary outputs (create-time wiring, eks idiom); fall back to
  # the values this module persisted as its own inputs/outputs so a var-less
  # DESTROY still resolves after the primary teardown emptied the primary state's
  # outputs. `try` catches the missing-attribute error on an emptied remote state
  # and uses the threaded fallback var instead of aborting the destroy. The pool
  # is targeted by its own state resource id at destroy, so these values only need
  # to be PRESENT, never re-fetched from the (possibly-gone) primary.
  cluster_name     = try(data.terraform_remote_state.cluster.outputs.cluster_name, var.cluster_name)
  cluster_location = try(data.terraform_remote_state.cluster.outputs.location, var.cluster_location)

  # Stable markers so kubectl probes / the node-count exclusion can identify the
  # test pool even when the caller supplies no custom labels. Merged UNDER the
  # caller labels so an explicit label wins on a key collision.
  effective_labels = merge(
    {
      "isv.ncp.validation/pool"      = "test"
      "isv.ncp.validation/pool-name" = var.pool_name
    },
    var.labels,
  )

  # Kubernetes uses "NoSchedule"; the GKE API expects "NO_SCHEDULE". The taints
  # variable accepts the Kubernetes spelling so the same JSON feeds the
  # validation; translate on the way to the resource.
  effect_to_gke = {
    NoSchedule       = "NO_SCHEDULE"
    PreferNoSchedule = "PREFER_NO_SCHEDULE"
    NoExecute        = "NO_EXECUTE"
  }

  is_gpu = var.node_type == "gpu" && var.accelerator_type != "" && var.accelerator_count > 0
}

resource "google_container_node_pool" "this" {
  name           = var.pool_name
  cluster        = local.cluster_name
  location       = local.cluster_location
  node_locations = length(var.node_locations) > 0 ? var.node_locations : null

  node_count = var.node_count

  node_config {
    machine_type = var.machine_type
    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]

    labels = local.effective_labels

    dynamic "guest_accelerator" {
      for_each = local.is_gpu ? [1] : []
      content {
        type  = var.accelerator_type
        count = var.accelerator_count

        gpu_driver_installation_config {
          gpu_driver_version = "LATEST"
        }
      }
    }

    dynamic "taint" {
      for_each = var.taints
      content {
        key    = taint.value.key
        value  = taint.value.value
        effect = local.effect_to_gke[taint.value.effect]
      }
    }
  }
}
