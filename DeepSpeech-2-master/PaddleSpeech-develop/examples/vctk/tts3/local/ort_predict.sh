train_output_path=$1

stage=0
stop_stage=0

# e2e, synthesize from text
if [ ${stage} -le 0 ] && [ ${stop_stage} -ge 0 ]; then
    python3 ${BIN_DIR}/../ort_predict_e2e.py \
        --inference_dir=${train_output_path}/inference_onnx \
        --am=fastspeech2_vctk \
        --voc=pwgan_vctk \
        --output_dir=${train_output_path}/onnx_infer_out_e2e \
        --text=${BIN_DIR}/../sentences_en.txt \
        --phones_dict=dump/phone_id_map.txt \
        --device=cpu \
        --cpu_threads=2 \
        --spk_id=0 \
        --lang=en

fi

if [ ${stage} -le 1 ] && [ ${stop_stage} -ge 1 ]; then
    python3 ${BIN_DIR}/../ort_predict_e2e.py \
        --inference_dir=${train_output_path}/inference_onnx \
        --am=fastspeech2_vctk \
        --voc=hifigan_vctk \
        --output_dir=${train_output_path}/onnx_infer_out_e2e \
        --text=${BIN_DIR}/../sentences_en.txt \
        --phones_dict=dump/phone_id_map.txt \
        --device=cpu \
        --cpu_threads=2 \
        --spk_id=0 \
        --lang=en
fi
