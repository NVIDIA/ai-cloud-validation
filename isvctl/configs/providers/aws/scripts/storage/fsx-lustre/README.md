<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# AWS FSx Lustre storage shim

Storage Provider Shim for AWS FSx Lustre. Drives
[`StorageProviderApiCheck`](../../../../../../../isvtest/src/isvtest/validations/storage_provider.py)
against a live EKS + FSx Lustre cluster.

## How it works

The manifest at [`../../../config/storage-provider-manifest.yaml`](../../../config/storage-provider-manifest.yaml)
declares one `aws-fsx-lustre` provider; `shim.module` points at
[`api.py`](api.py); `StorageProviderApiCheck` loads the shim in-process
and runs three subtests against the AWS APIs below.

| Subtest | AWS call |
| ------- | -------- |
| `api-authentication[aws-fsx-lustre]` | `servicequotas:GetServiceQuota` |
| `volume-provisioning[aws-fsx-lustre]` | `fsx:DescribeFileSystems` (fallback) |
| `tenant-quota[aws-fsx-lustre]` | `GetServiceQuota.Value` + sum of `DescribeFileSystems[].StorageCapacity` |

The FSx CSI driver (`fsx.csi.aws.com`) owns volume lifecycle on EKS, so
`create_volume` / `delete_volume` raise `NotSupportedError` and the
acceptance suite falls back to inventorying existing volumes
(the managed-K8s fallback path).

## Required IAM actions

The identity isvctl runs under (developer machine creds, EC2 instance
profile, or pod-mounted IRSA) needs the following:

| Action | Why |
| ------ | --- |
| `sts:GetCallerIdentity` | Resolves the AWS account id as the shim's tenant id |
| `servicequotas:GetServiceQuota` (on `fsx` service code) | Reads the hard quota for the authentication and tenant-quota subtests |
| `fsx:DescribeFileSystems` | Lists filesystems for volume-provisioning and tenant-quota subtests |

Not needed (intentionally - keeps the IAM surface tight):

- `cloudwatch:*` - the shim uses FSx `StorageCapacity` (allocation), not
  CloudWatch consumption metrics. This matches what the AWS Service
  Quota actually counts against.
- `fsx:CreateFileSystem` / `fsx:DeleteFileSystem` - the CSI driver owns
  provisioning.

## Environment variables

| Variable | Required? | Effect |
| -------- | --------- | ------ |
| `AWS_REGION` | Yes | Region for all AWS clients (Service Quotas, FSx, STS). Must match the cluster's FSx region. |
| `AWS_PROFILE` _or_ `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` [+ `AWS_SESSION_TOKEN`] | Yes | Standard boto3 credential chain - no custom logic. |
| `FSX_DEPLOYMENT_TYPE` | No (default `PERSISTENT_2`) | One of `PERSISTENT_2`, `PERSISTENT_1`, `SCRATCH_1`, `SCRATCH_2`. Chooses the Service Quota code. |
| `FSX_QUOTA_CODE` | No | Overrides the `FSX_DEPLOYMENT_TYPE` -> quota-code mapping (e.g. for a code we don't ship). |
| `AWS_ACCOUNT_ID` | No | Skips the `sts:GetCallerIdentity` lookup. Useful in CI / restricted envs. |

## Deployment type to quota code

| `FSX_DEPLOYMENT_TYPE` | Service Quota code | Display name |
| --------------------- | ------------------ | ------------ |
| `PERSISTENT_2` (default) | `L-8F1B9C74` | AWS FSx Lustre (PERSISTENT_2) |
| `PERSISTENT_1` | `L-C8640C82` | AWS FSx Lustre (PERSISTENT_1) |
| `SCRATCH_1` | `L-AD2FC696` | AWS FSx Lustre (SCRATCH) |
| `SCRATCH_2` | `L-AD2FC696` | AWS FSx Lustre (SCRATCH) |

Pick the value that matches your FSx Lustre StorageClass's
`parameters.deploymentType`. If the cluster mixes tiers, run the suite
once per tier (or set `FSX_QUOTA_CODE` directly).

## Running on a cluster

From the repo root:

```bash
export AWS_REGION=us-west-2          # match cluster region
export AWS_PROFILE=...               # or AWS_ACCESS_KEY_ID/SECRET

# Sanity-check creds + that the quota exists:
aws sts get-caller-identity
aws fsx describe-file-systems --max-results 1 --region "$AWS_REGION"
aws service-quotas get-service-quota \
  --service-code fsx --quota-code L-8F1B9C74 --region "$AWS_REGION"

# Run the storage check:
uv run isvctl test run \
    -f isvctl/configs/providers/aws/config/eks.yaml
```

`volume-provisioning[aws-fsx-lustre]` reports **skipped (passed)** with
`created_volume not implemented; observed N CSI-provisioned volume(s)
via list_volumes()` - that is the managed-K8s
fallback, not a failure.

## Troubleshooting

| Symptom | Likely cause | Fix |
| ------- | ------------ | --- |
| `api-authentication[...] FAILED ... AuthenticationError` | Missing creds, expired SSO, IAM role missing `servicequotas:GetServiceQuota` | Refresh creds; attach the IAM actions above |
| `tenant-quota[...] FAILED ... hard_limit_bytes=0` | Wrong `FSX_DEPLOYMENT_TYPE` (account has zero of that tier) | Set `FSX_DEPLOYMENT_TYPE` to match your FSx SC's `parameters.deploymentType` |
| `Failed to load provider manifest: ... not found` | Working dir is not the repo root | `cd` to repo root before `isvctl test run` |
| `AWS_REGION must be set` | `AWS_REGION` env var unset | `export AWS_REGION=...` matching the cluster region |
| `volume-provisioning[...] SKIPPED ... observed 0 ...` | Account has no FSx Lustre filesystems yet | Create a PVC against the FSx StorageClass first, then re-run |

## See also

- [`isvtest/src/isvtest/core/storage_provider/api.py`](../../../../../../../isvtest/src/isvtest/core/storage_provider/api.py) - the `StorageApi` ABC + value types (per-method contract docstrings)
- [`isvctl/configs/providers/my-isv/scripts/storage/`](../../../../my-isv/scripts/storage/) - generic provider template
