#export CUDA_VISIBLE_DEVICE=0,1,2,3

# nohup python3 local/punc_server.py --config_file conf/punc_application.yaml > punc.log 2>&1 &
paddlespeech_server start --config_file conf/punc_application.yaml &> punc.log &

# nohup python3 local/streaming_asr_server.py --config_file conf/ws_conformer_wenetspeech_application.yaml > streaming_asr.log 2>&1 &
paddlespeech_server start --config_file conf/ws_conformer_wenetspeech_application.yaml &> streaming_asr.log  &

