# GCP Image Registry Validation Guide

This guide is the operator walkthrough for validating GCP image-registry
capabilities with the ISV validation framework. It is the GCP port of the
[AWS ISO/VMDK import guide](../../../../aws/scripts/image-registry/docs/aws-image-registry.md);
the [AWS reference](../../../../../../../docs/references/aws.md) is the canonical
contract and the GCP scripts under `providers/gcp/scripts/image-registry/`
translate it onto Compute Engine + Cloud Storage. For the NCP-level operator
contract (auth, project resolution, the required `NETWORK_FIREWALL_TRUST_IP`
firewall source, and the full operator env-var table) see
[`docs/references/gcp.md`](../../../../../../../docs/references/gcp.md).

## Overview

The GCP image-registry validation exercises the disk-import → image →
instance → install-config lifecycle:

1. **upload_image** (setup) — Download a VM disk image, stage it in a Cloud
   Storage bucket, and register it as a Compute Engine machine image.
2. **crud_image** (test) — Get / list / create / delete the machine image.
3. **launch_instance** (test) — Launch a GPU instance from the imported image,
   open an SSH firewall rule, and expose a public IP for SSH validation.
4. **crud_install_config** (test) — Create / read / update / delete a Compute
   Engine **instance template** (the install-config analog).
5. **teardown** (teardown) — Delete the instance, image, bucket + objects,
   firewall rule, and local SSH key.

### How each step maps onto GCP

| Step | AWS oracle | GCP translation |
|------|-----------|-----------------|
| `upload_image` | `s3` upload + `ec2.import_image` (single call) | Cloud Storage upload + register a Compute Engine image. **RAW** source (`disk.raw` in a `.tar.gz`) registers directly via `ImagesClient.insert(raw_disk.source=…)`; **foreign formats** (vmdk / vhd / qcow2) convert + register through the documented `gcloud compute images import` (Cloud Build-backed) workflow. Both emit `image_id`, `storage_bucket`, `disk_ids`. |
| `crud_image` | `describe` / `describe(Owners=self)` / `copy_image` / `deregister_image` | `ImagesClient` get / list / insert(from `source_image`) / delete. The created copy is deleted; the source `image_id` is never touched. |
| `launch_instance` | key pair + security group + `run_instances` from the AMI | local SSH key (no managed key store) + **VPC firewall rule** (no security-group resource) + `InstancesClient.insert` from the imported image. `security_group_id` carries the firewall name; `instance_profile` is empty (no attached service account is needed for the SSH-based checks — GCP has no instance-profile resource). |
| `crud_install_config` | EC2 Launch Template create / read / update-version / delete | `InstanceTemplatesClient` insert / get / **create-replacement** / delete. Instance templates are immutable, so the oracle's in-place UPDATE is realized as a replacement template. |
| `teardown` | terminate / deregister / delete bucket / delete key / delete SG | independent idempotent deletes of instance (zonal), image (global), bucket + objects, firewall rule, and the local SSH key. |

### Subtests exercised

| Validation group | Checks | Step |
|------------------|--------|------|
| `image_upload` | `StepSuccessCheck`, `FieldExistsCheck` (image_id, storage_bucket, disk_ids) | upload_image |
| `image_crud` | `StepSuccessCheck`, `FieldExistsCheck`, `CrudOperationsCheck` (get, list, create, delete) | crud_image |
| `vm_from_image` | `StepSuccessCheck`, `FieldExistsCheck` (instance_id, public_ip, key_path), `InstanceStateCheck` (running) | launch_instance |
| `vm_ssh` | `ConnectivityCheck`, `OsCheck` (ubuntu) | launch_instance |
| `install_config_crud` | `StepSuccessCheck`, `FieldExistsCheck` (config_id, config_name, operations) | crud_install_config |
| `teardown_checks` | `StepSuccessCheck` | teardown |

### Bare-metal install is a platform gap

The canonical suite also defines `install_image_bm` and `install_config_bm`
(install an OS image / install-config onto bare metal). Google Cloud Bare Metal
Solution has **no self-service API** to provision a bare-metal server from a
customer-supplied or arbitrary OS image — standard certified OSes are installed
by Google during onboarding, and a custom image requires a Google Cloud support
case. This is a verified platform gap, so — exactly as the AWS oracle's
image-registry config omits these two steps — the GCP config leaves them
**unwired**. Their `bm_from_image` / `bm_from_config` validation groups then
resolve to a `step_not_configured` skip (scoped to those two groups only; the
shared `StepSuccessCheck` / `FieldExistsCheck` / `InstanceStateCheck` classes the
live groups use are untouched).

## Prerequisites

### Tools and APIs

```bash
# Google Cloud CLI (required for the foreign-format vmdk/vhd/qcow2 import path,
# which drives Cloud Build). Not needed if you only import RAW sources.
gcloud --version

# Python SDKs (installed via uv sync): google-cloud-compute, google-cloud-storage, requests
uv run python -c "from google.cloud import compute_v1, storage; import requests; print('OK')"
```

Enable these APIs on the project: **Compute Engine** (`compute.googleapis.com`),
**Cloud Storage** (`storage.googleapis.com`), and — for the foreign-format
import path — **Cloud Build** (`cloudbuild.googleapis.com`).

### IAM roles

The principal running the suite (user or service account) needs, on the project:

- `roles/compute.admin` — register / list / delete images, launch / delete the
  instance, create / delete the firewall rule and instance templates.
- `roles/storage.admin` — create the staging bucket, upload the disk object,
  delete the bucket on teardown.
- `roles/cloudbuild.builds.editor` + `roles/compute.storageAdmin` — only for the
  foreign-format `gcloud compute images import` path (Cloud Build runs the
  conversion). The RAW path needs neither.

### Authentication, project, and firewall source

Auth (ADC) and project resolution are the NCP-wide contract — see
[`docs/references/gcp.md`](../../../../../../../docs/references/gcp.md).
`launch_instance` opens an SSH (tcp/22) firewall rule and there is **no
open-internet default**: it reads the **required** `NETWORK_FIREWALL_TRUST_IP`
operator env var (a bare IPv4 normalizes to `/32`; comma-separated IPv4 CIDRs
are allowed). When it is unset, empty, non-IPv4, or `0.0.0.0/0`, `launch_instance`
fails fast with an operator error before creating any resource.

```bash
export GOOGLE_CLOUD_PROJECT=your-project-id          # or rely on ADC's bundled project
export NETWORK_FIREWALL_TRUST_IP=203.0.113.10        # your workstation / CI egress IP
```

## Running

```bash
uv run isvctl test run -f isvctl/configs/providers/gcp/config/image-registry.yaml
```

### Skip teardown (debugging)

```bash
GCP_IMAGE_REGISTRY_SKIP_TEARDOWN=true \
  uv run isvctl test run -f isvctl/configs/providers/gcp/config/image-registry.yaml
```

`GCP_IMAGE_REGISTRY_SKIP_TEARDOWN=true` forwards `--skip-destroy` to the teardown
step, preserving the in-test image, bucket, disk objects, instance, firewall
rule, and SSH key. Unset, teardown runs normally. (This is the GCP-namespaced
override of the suite-default vendor-neutral `IR_SKIP_TEARDOWN`.)

### Use a RAW source (pure-SDK, no Cloud Build)

```bash
uv run isvctl test run -f isvctl/configs/providers/gcp/config/image-registry.yaml \
  --set image_format=raw \
  --set image_url=https://example.com/path/to/ubuntu-disk.tar.gz
```

A RAW source (a `disk.raw` packaged as `.tar.gz`) registers directly via
`ImagesClient.insert` and skips the Cloud Build conversion entirely — faster and
fewer APIs/roles required.

### Run notes

- **Default image type.** The default `image_url` is an Ubuntu 22.04 (jammy)
  VMDK (foreign format), so the default run uses the `gcloud compute images
  import` Cloud Build path. The import (download + convert + register) routinely
  takes 20–40 minutes, which is why the `upload_image` step cap is 3600s. Use a
  RAW source (above) to avoid Cloud Build.
- **Guest-OS translation target.** The foreign-format import requires a
  `gcloud ... --os` translation target, threaded as the `guest_os` setting
  (default `ubuntu-2204`) coupled to `image_url`. That enum has no
  `ubuntu-2404` choice, so the GCP default pins Ubuntu 22.04 to keep the disk
  and the translation hint in sync — diverging from the vendor-neutral suite
  default (Ubuntu 24.04). An operator changing `image_url` to a different OS
  must set the matching gcloud `--os` value in `guest_os`.
- **GPU instance / zone walk.** `launch_instance` defaults to `g2-standard-8`
  with an integrated NVIDIA L4 (matching the AWS oracle launching a GPU instance
  from the imported image). The imported Ubuntu image has no GPU drivers; the
  suite validates SSH + OS, not GPU, so the L4 simply idles. L4 capacity drifts,
  so the launch walks the reviewed preferred-zone list on
  `ZONE_RESOURCE_POOL_EXHAUSTED` and records any partially-created records in
  `leaked_zones` for teardown to reclaim.
- **Resource naming.** Every created resource (image, bucket, instance,
  firewall, instance templates, SSH key) is suffixed with a unique per-run id
  (the `RUN_ID` environment variable) so parallel runs do not collide on Compute
  Engine's name-as-id namespace and teardown owns only its own resources.

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `gcloud CLI not found on PATH` (upload_image) | The foreign-format path needs the Google Cloud CLI + Cloud Build API. Install gcloud, or switch to a RAW source (`--set image_format=raw`). |
| `NETWORK_FIREWALL_TRUST_IP is unset` (launch_instance) | Export your operator IP/CIDR; there is no open-internet fallback for SSH ingress. |
| Image import fails in Cloud Build | Confirm Cloud Build API is enabled and the principal holds `roles/cloudbuild.builds.editor`; check the build log link printed on stderr. |
| `ZONE_RESOURCE_POOL_EXHAUSTED` across all zones | L4 stockout in every candidate zone — retry later or pin a zone with observed capacity via `--set zone=<zone>`. |
| SSH validation (`vm_ssh`) fails | Verify `NETWORK_FIREWALL_TRUST_IP` matches the host the suite runs from, and that the imported image is an Ubuntu cloud image with cloud-init SSH-key injection. |

## Related documentation

- [GCP reference (operator contract)](../../../../../../../docs/references/gcp.md)
- [AWS ISO/VMDK import guide](../../../../aws/scripts/image-registry/docs/aws-image-registry.md)
- [Image registry suite contract](../../../../../suites/image-registry.yaml)
