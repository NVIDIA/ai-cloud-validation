#!/bin/bash
# NCCL AllReduce test for MPI-capable container runtimes (Pyxis/Enroot/Singularity)
#
# This runs one task per GPU with true multi-node coordination,
# validating both intra-node (NVLink/NVSwitch) and inter-node (network fabric)
# GPU communication.
#
# Variables:
#   JOB_NAME - Slurm job name
#   PARTITION - Slurm partition
#   NODES - Number of nodes
#   TOTAL_TASKS - Total tasks (nodes * gpus_per_node)
#   GPUS_PER_NODE - GPUs per node
#   IMAGE - Container image (default: NCCL_HPC_IMAGE)
#   CONTAINER_RUNTIME - Container runtime (pyxis, enroot, singularity)
#   CONTAINER_OPTS - Runtime-specific srun options
#   NCCL_CMD - Full NCCL test command

#SBATCH --job-name={{JOB_NAME}}
#SBATCH --partition={{PARTITION}}
#SBATCH --nodes={{NODES}}
#SBATCH --ntasks={{TOTAL_TASKS}}
#SBATCH --ntasks-per-node={{GPUS_PER_NODE}}
#SBATCH --gpus-per-node={{GPUS_PER_NODE}}
#SBATCH --time=00:30:00
#SBATCH --output=slurm-%j.out
#SBATCH --error=slurm-%j.err
#SBATCH --exclusive

# NCCL environment variables for debugging
export NCCL_DEBUG=INFO
export NCCL_DEBUG_SUBSYS=INIT

# Additional environment variables (UCX, NCCL tuning, etc.)
{{EXTRA_ENV}}

# Print job info
echo "=========================================="
echo "NCCL Multi-Node AllReduce Test"
echo "=========================================="
echo "Job ID: $SLURM_JOB_ID"
echo "Nodes: $SLURM_JOB_NUM_NODES"
echo "Node List: $SLURM_JOB_NODELIST"
echo "Tasks: $SLURM_NTASKS"
echo "GPUs per Node: {{GPUS_PER_NODE}}"
echo "Total GPUs: {{TOTAL_TASKS}}"
echo "Container: {{IMAGE}}"
echo "Mode: {{CONTAINER_RUNTIME}} (true multi-node)"
echo "=========================================="

# Run NCCL AllReduce test
# -b: Start size, -e: End size, -f: Factor, -g: GPUs per process
srun {{CONTAINER_OPTS}} {{NCCL_CMD}}

echo ""
echo "=========================================="
echo "NCCL test completed"
echo "=========================================="
