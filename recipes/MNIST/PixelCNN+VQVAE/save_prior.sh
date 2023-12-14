#!/bin/bash

data_root="../data"
exp_dir="./exp"

tag=""
checkpoint=""

dump_format="torch"

system="defaults"
preprocess="defaults"
data="vqvae"
train="prior"
model="vqvae"

. ../../_common/parse_options.sh || exit 1;

if [ -z "${tag}" ]; then
    tag=$(date +"%Y%m%d-%H%M%S")
fi

list_dir="${exp_dir}/${tag}/list"
feature_dir="${exp_dir}/${tag}/prior"

cmd=$(
    python ../../_common/parse_run_command.py \
    --config-dir "./conf" \
    hydra.run.dir="./log/$(date +"%Y%m%d-%H%M%S")" \
    system="${system}"
)

for subset in "train" "validation"; do
    list_path="${exp_dir}/${tag}/list/${subset}.txt"
    filename="${subset}\{number\}"

    if [ "${subset}" = "train" ]; then
        is_train="true"
    else
        is_train="false"
    fi

    ${cmd} ./local/save_prior.py \
    --config-dir "./conf" \
    hydra.run.dir="${exp_dir}/${tag}/log/$(date +"%Y%m%d-%H%M%S")" \
    system="${system}" \
    preprocess="${preprocess}" \
    data="${data}" \
    train="${train}" \
    model="${model}" \
    preprocess.dump_format="${dump_format}" \
    preprocess.list_path="${list_path}" \
    preprocess.feature_dir="${feature_dir}" \
    train.dataset.root="${data_root}" \
    train.dataset.train=${is_train} \
    train.checkpoint="${checkpoint}" \
    train.output.filename="${filename}"
done
