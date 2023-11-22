#!/bin/bash

set -eu
set -o pipefail

stage=-1
stop_stage=-1

tag=""
continue_from=""
pixelcnn_checkpoint=""
vqvae_checkpoint=""

exp_dir="./exp"

urbansound8k_url="https://zenodo.org/records/1203745/files/UrbanSound8K.tar.gz"
official_data_root="../data"
urbansound8k_data_root="../../UrbanSound8K/data"
dump_root="./dump"

system="defaults"
preprocess="baseline"
data=""
train=""
test="pixelsnail+vqvae"
model=""
optimizer=""
lr_scheduler=""
criterion=""

n_validation=10

. ../../_common/parse_options.sh || exit 1;

set +u

# path to local scripts
export PYTHONPATH="./:${PYTHONPATH}"

set -u

if [ ${stage} -le -1 ] && [ ${stop_stage} -ge -1 ]; then
    echo "Stage -1"

    # official development dataset to train PixelSNAIL+VQVAE
    echo "Please place official development dataset under ${official_data_root}."

    (
        # UrbanSound8K dataset to train HiFiGAN
        . ../../UrbanSound8K/_common/download.sh \
        --urbansound8k-url "${urbansound8k_url}" \
        --data-root "${urbansound8k_data_root}"
    )
fi

if [ ${stage} -le 0 ] && [ ${stop_stage} -ge 0 ]; then
    echo "Stage 0: Preprocessing of official development dataset"

    (
        . ./preprocess_official_dataset.sh \
        --stage 1 \
        --stop-stage 2 \
        --data-root "${official_data_root}" \
        --dump-root "${dump_root}" \
        --preprocess "${preprocess}" \
        --data "${data}" \
        --n-validation ${n_validation}
    )
fi

if [ ${stage} -le 1 ] && [ ${stop_stage} -ge 1 ]; then
    echo "Stage 1: Preprocessing of UrbanSound8K"

    (
        . ./preprocess_urbansound8k.sh \
        --stage 1 \
        --stop-stage 2 \
        --data-root "${urbansound8k_data_root}" \
        --dump-root "${dump_root}" \
        --preprocess "${preprocess}" \
        --data "${data}" \
        --n-validation ${n_validation}
    )
fi

if [ ${stage} -le 2 ] && [ ${stop_stage} -ge 2 ]; then
    echo "Stage 2: Training of VQ-VAE"

    (
        . ./train_vqvae.sh \
        --tag "${tag}" \
        --continue-from "${continue_from}" \
        --dump-root "${dump_root}" \
        --exp-dir "${exp_dir}" \
        --system "${system}" \
        --data "${data}" \
        --train "${train}" \
        --model "${model}" \
        --optimizer "${optimizer}" \
        --lr_scheduler "${lr_scheduler}" \
        --criterion "${criterion}"
    )
fi

if [ ${stage} -le 3 ] && [ ${stop_stage} -ge 3 ]; then
    echo "Stage 3: Save prior from VQ-VAE"

    (
        . ./save_prior.sh \
        --tag "${tag}" \
        --dump-root "${dump_root}" \
        --exp-dir "${exp_dir}" \
        --checkpoint "${vqvae_checkpoint}" \
        --system "${system}" \
        --preprocess "${preprocess}" \
        --data "${data}" \
        --train "${train}" \
        --model "${model}"
    )
fi

if [ ${stage} -le 4 ] && [ ${stop_stage} -ge 4 ]; then
    echo "Stage 4: Training of PixelSNAIL"

    (
        . ./train_pixelsnail.sh \
        --tag "${tag}" \
        --continue-from "${continue_from}" \
        --exp-dir "${exp_dir}" \
        --dump-root "${dump_root}" \
        --system "${system}" \
        --preprocess "${preprocess}" \
        --data "${data}" \
        --train "${train}" \
        --model "${model}" \
        --optimizer "${optimizer}" \
        --lr_scheduler "${lr_scheduler}" \
        --criterion "${criterion}"
    )
fi

if [ ${stage} -le 5 ] && [ ${stop_stage} -ge 5 ]; then
    echo "Stage 5: Training of HiFi-GAN"

    (
        . ./train_hifigan.sh \
        --tag "${tag}" \
        --continue-from "${continue_from}" \
        --exp-dir "${exp_dir}" \
        --dump-root "${dump_root}" \
        --system "${system}" \
        --preprocess "${preprocess}" \
        --data "${data}" \
        --train "${train}" \
        --model "${model}" \
        --optimizer "${optimizer}" \
        --lr_scheduler "${lr_scheduler}" \
        --criterion "${criterion}"
    )
fi
