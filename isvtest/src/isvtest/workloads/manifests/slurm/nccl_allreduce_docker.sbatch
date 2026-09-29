#!/bin/bash
# NCCL AllReduce test for Docker container runtime
#
# This runs one container per node with all GPUs, validating intra-node
# GPU interconnect (NVLink/NVSwitch) on each node.
#
# Variables:
#   JOB_NAME - Slurm job name
#   PARTITION - Slurm partition
#   NODES - Number of nodes
#   GPUS_PER_NODE - GPUs per node
#   TOTAL_GPUS - Total GPU count across all nodes
#   IMAGE - Container image (default: NCCL_HPC_IMAGE)
#   NCCL_SIZE_PARAMS - NCCL test size parameters (e.g., "-b 1M -e 256M -f 2")

#SBATCH --job-name={{JOB_NAME}}
#SBATCH --partition={{PARTITION}}
#SBATCH --nodes={{NODES}}
#SBATCH --ntasks-per-node=1
#SBATCH --gpus-per-node={{GPUS_PER_NODE}}
#SBATCH --time=00:30:00
#SBATCH --output=slurm-%j.out
#SBATCH --error=slurm-%j.err
#SBATCH --exclusive

# Print job info
echo "=========================================="
echo "NCCL Multi-Node AllReduce Test"
echo "=========================================="
echo "Job ID: $SLURM_JOB_ID"
echo "Nodes: $SLURM_JOB_NUM_NODES"
echo "Node List: $SLURM_JOB_NODELIST"
echo "GPUs per Node: {{GPUS_PER_NODE}}"
echo "Total GPUs: {{TOTAL_GPUS}}"
echo "Container: {{IMAGE}}"
echo "Mode: Docker (intra-node multi-GPU per node)"
echo "=========================================="

# Run NCCL AllReduce test on each node
# -g {{GPUS_PER_NODE}}: Use all GPUs on this node in a single NCCL communicator
# This validates NVLink/NVSwitch within each node
srun --ntasks-per-node=1 docker run --rm --gpus all --network=host --ipc=host \
    -e NCCL_DEBUG=INFO -e NCCL_DEBUG_SUBSYS=INIT \
    {{IMAGE}} all_reduce_perf {{NCCL_SIZE_PARAMS}} -g {{GPUS_PER_NODE}}

echo ""
echo "=========================================="
echo "NCCL test completed"
echo "=========================================="
