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

# Secondary GKE cluster that shares the primary cluster's VPC network (GCP
# analog of the AWS EKS oracle's terraform-shared-vpc-cluster). GKE supports
# multiple clusters in one VPC natively — the secondary simply attaches to the
# SAME network/subnetwork read from the primary cluster's state. No subnet
# tagging is required (unlike EKS). Its own local state (selected by the stub
# via -state=) so it can be destroyed independently before the primary teardown.

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

data "terraform_remote_state" "primary" {
  backend = "local"
  config = {
    path = var.cluster_state_path
  }
}

locals {
  # Attach to the SAME network/subnetwork as the primary to prove same-VPC
  # coexistence — read LIVE from the primary state at create. Fall back to the
  # values persisted in THIS module's own state/inputs so a var-less DESTROY still
  # resolves after the primary teardown emptied the primary state's outputs (`try`
  # catches the missing-attribute error and uses the threaded fallback var). The
  # secondary is targeted by its own state id at destroy, so these only need to be
  # present.
  primary_network    = try(data.terraform_remote_state.primary.outputs.network, var.network)
  primary_subnetwork = try(data.terraform_remote_state.primary.outputs.subnetwork, var.subnetwork)

  # GKE node-pool names are RFC-1035 capped at 40 chars. cluster_name is already
  # capped at 40, so appending "-np" could overflow for a long cluster-name
  # override. Cap the indirect pool name independently, trimming only the cluster
  # BASE so both the run-id tail and the "-np" role discriminator survive.
  _np_max        = 40
  _cluster_sid   = element(split("-", var.cluster_name), length(split("-", var.cluster_name)) - 1)
  _cluster_base  = trimsuffix(var.cluster_name, "-${local._cluster_sid}")
  _np_base_keep  = max(1, local._np_max - length(local._cluster_sid) - 4) # "-np-<sid>"
  _np_base       = substr(local._cluster_base, 0, min(length(local._cluster_base), local._np_base_keep))
  node_pool_name = length("${var.cluster_name}-np") <= local._np_max ? "${var.cluster_name}-np" : "${local._np_base}-np-${local._cluster_sid}"
}

resource "google_container_cluster" "secondary" {
  name     = var.cluster_name
  location = var.location

  network    = local.primary_network
  subnetwork = local.primary_subnetwork == "" ? null : local.primary_subnetwork

  # Full-run-identity ownership marker stamped atomically at creation (matches the
  # primary cluster); the adopt/destroy paths require it before importing or
  # destroying a state-tracked secondary, so a foreign same-name cluster is never
  # adopted or destroyed as run-owned.
  resource_labels = var.ownership_labels

  remove_default_node_pool = true
  initial_node_count       = 1

  release_channel {
    channel = "REGULAR"
  }

  deletion_protection = false
}

resource "google_container_node_pool" "secondary" {
  name           = local.node_pool_name
  cluster        = google_container_cluster.secondary.name
  location       = google_container_cluster.secondary.location
  node_locations = length(var.node_locations) > 0 ? var.node_locations : null

  node_count = var.node_count

  node_config {
    machine_type = var.machine_type
    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }
}
