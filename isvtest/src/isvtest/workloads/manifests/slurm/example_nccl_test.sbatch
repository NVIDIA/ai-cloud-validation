#!/bin/bash
# Example multi-node NCCL test script for SlurmSbatchWorkload
#
# This script runs a simple NCCL all-reduce test across multiple nodes
# to verify GPU interconnect functionality.
#
# Variables (substitute via config):
#   PARTITION - Slurm partition to use (default: gpu)
#   NODES - Number of nodes to request (default: 2)
#   GPUS_PER_NODE - GPUs per node (default: 8)
#   TIME_LIMIT - Job time limit in HH:MM:SS format (default: 00:30:00)
#   JOB_NAME - Name for the job (default: isvtest-nccl)
#   CONTAINER_IMAGE - Container image to use (default: GPU_STRESS_IMAGE)

#SBATCH --job-name={{JOB_NAME}}
#SBATCH --partition={{PARTITION}}
#SBATCH --nodes={{NODES}}
#SBATCH --ntasks-per-node={{GPUS_PER_NODE}}
#SBATCH --gres=gpu:{{GPUS_PER_NODE}}
#SBATCH --time={{TIME_LIMIT}}
#SBATCH --output=slurm-%j.out
#SBATCH --error=slurm-%j.err
#SBATCH --exclusive

# NCCL environment variables
export NCCL_DEBUG=INFO
export NCCL_DEBUG_SUBSYS=INIT,GRAPH

# Print job info
echo "=========================================="
echo "NCCL Multi-Node Test"
echo "=========================================="
echo "Job ID: $SLURM_JOB_ID"
echo "Nodes: $SLURM_JOB_NUM_NODES"
echo "Node List: $SLURM_JOB_NODELIST"
echo "GPUs per Node: {{GPUS_PER_NODE}}"
echo "Total GPUs: $((SLURM_JOB_NUM_NODES * SLURM_GPUS_PER_NODE))"
echo "=========================================="

# Get master node info for distributed setup
MASTER_ADDR=$(scontrol show hostnames $SLURM_JOB_NODELIST | head -n 1)
MASTER_PORT=29500

echo "Master Node: $MASTER_ADDR:$MASTER_PORT"
echo ""

# Run NCCL test using PyTorch distributed
srun --container-image={{CONTAINER_IMAGE}} \
     --container-mounts=/tmp:/tmp \
     python3 -c "
import os
import torch
import torch.distributed as dist

def main():
    # Initialize distributed
    rank = int(os.environ.get('SLURM_PROCID', 0))
    world_size = int(os.environ.get('SLURM_NTASKS', 1))
    local_rank = int(os.environ.get('SLURM_LOCALID', 0))

    os.environ['MASTER_ADDR'] = '$MASTER_ADDR'
    os.environ['MASTER_PORT'] = '$MASTER_PORT'
    os.environ['RANK'] = str(rank)
    os.environ['WORLD_SIZE'] = str(world_size)
    os.environ['LOCAL_RANK'] = str(local_rank)

    print(f'[Rank {rank}/{world_size}] Initializing on GPU {local_rank}')

    torch.cuda.set_device(local_rank)
    dist.init_process_group(backend='nccl')

    # Simple all-reduce test
    tensor = torch.ones(1024 * 1024, device='cuda') * rank

    dist.all_reduce(tensor)

    expected = sum(range(world_size)) * 1024 * 1024
    actual = tensor.sum().item()

    if rank == 0:
        if abs(actual - expected) < 0.01:
            print(f'SUCCESS: NCCL all-reduce verified across {world_size} GPUs')
        else:
            print(f'FAILURE: Expected {expected}, got {actual}')

    dist.destroy_process_group()

if __name__ == '__main__':
    main()
"

echo ""
echo "Job completed"
