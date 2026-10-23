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

# shellcheck shell=bash disable=SC2034
# env_used.sh - sourceable elbencho sweep configuration snapshot.
# Auto-generated. The companion env_used.yaml is the human/external-tool form.
# This file is the canonical artifact for nv-elbencho-sweep.sh --resume.
# Generated: 2026-09-08T23:08:02Z

unset TEST_DIRS
declare -gA TEST_DIRS=(
    [/scratch/scale-test/data]=1
)
export TEST_DIRS

export FS_MAX_AGG_THROUGHPUT=5
export FS_MAX_NODE_THROUGHPUT_GBPS=10
export FS_MAX_NODE_IOPS=10000

unset ELBENCHO_SCALE_THREAD_LIST
declare -ga ELBENCHO_SCALE_THREAD_LIST=(1 4)
export ELBENCHO_SCALE_THREAD_LIST
unset ELBENCHO_SCALE_IO_SIZES
declare -ga ELBENCHO_SCALE_IO_SIZES=(1M)
export ELBENCHO_SCALE_IO_SIZES
unset ELBENCHO_IODEPTH_LIST
declare -ga ELBENCHO_IODEPTH_LIST=(1)
export ELBENCHO_IODEPTH_LIST

export ELBENCHO_FILE_SIZE_MULTIPLIER=1024
export ELBENCHO_FILE_LAYOUT=worker-directories
export ELBENCHO_FILES_PER_NODE=''
export ELBENCHO_FILE_SIZE=''
export ELBENCHO_SCALE_READ_WRITE_DURATION=15
export ELBENCHO_READ_AFTER_WRITE_PAUSE=0
export ELBENCHO_LIVE_CSV_EXTENDED=0
export ELBENCHO_LIVEINT=1000
export ELBENCHO_SINGLE_BIG_FILE=0
export ELBENCHO_SINGLE_BIG_FILE_BASENAME=elbencho-bigfile
export ELBENCHO_SINGLE_BIG_FILE_SIZE=''
export ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=0

# Original CLI flag values (resume reconstructs sweep behavior from these).
# Names match the local variables used in nv-elbencho-sweep.sh.
export dio_or_bio=dio
export rand_option=0
export single_option=0
export sweep_write_only=0
export sweep_write_no_read=0
export sweep_read_from=''
export nodes_spec=2
export ELBENCHO_TREEFILE_CACHE_PATH=''
declare -ga ELBENCHO_TREEFILE_CACHE_ATTEMPTS=(
)
export ELBENCHO_TREEFILE_CACHE_ATTEMPTS
