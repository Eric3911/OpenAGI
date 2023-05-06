#!/bin/bash
# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

set -e
source path.sh

ngpu=$(echo $CUDA_VISIBLE_DEVICES | awk -F "," '{print NF}')

if [ $# != 1 ];then
    echo "usage: CUDA_VISIBLE_DEVICES=0 ${0} config_path"
    exit -1
fi

stage=1
stop_stage=3

cfg_path=$1

if [ ${stage} -le 1 ] && [ ${stop_stage} -ge 1 ]; then
    ./local/train.sh ${ngpu} ${cfg_path} || exit -1
fi

ckpt=./checkpoint/epoch_100/model.pdparams
score_file=./scores.txt
stats_file=./stats.0.txt
img_file=./det.png

if [ ${stage} -le 2 ] && [ ${stop_stage} -ge 2 ]; then
    ./local/score.sh ${cfg_path} ${ckpt} ${score_file} ${stats_file} || exit -1
fi

keyword=HeySnips
if [ ${stage} -le 3 ] && [ ${stop_stage} -ge 3 ]; then
    ./local/plot.sh ${keyword} ${stats_file} ${img_file} || exit -1
fi