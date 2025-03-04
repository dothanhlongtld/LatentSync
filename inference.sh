#!/bin/bash

python -m scripts.inference \
    --unet_config_path "configs/unet/second_stage.yaml" \
    --inference_ckpt_path "checkpoints/latentsync_unet.pt" \
    --inference_steps 20 \
    --guidance_scale 1.5 \
    --video_path "assets/demo4_video.mp4" \
    --audio_path "assets/demo4_audio.mp3" \
    --video_out_path "video_out.mp4"
