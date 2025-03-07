# Copyright (c) 2024 Bytedance Ltd. and/or its affiliates
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

import argparse
from omegaconf import OmegaConf
import torch
from diffusers import AutoencoderKL, DDIMScheduler
from latentsync.models.unet import UNet3DConditionModel
from latentsync.pipelines.lipsync_pipeline import LipsyncPipeline
from diffusers.utils.import_utils import is_xformers_available
from accelerate.utils import set_seed
from latentsync.whisper.audio2feature import Audio2Feature
import ffmpeg
import os
from pathlib import Path
import subprocess


def cut_video(input_path: str, output_path: str, start_time: int, end_time: int):
    ffmpeg.input(input_path, ss=start_time).output(
        output_path,
        t=end_time - start_time,
        y="-y",
        **{
            "crf": "28",
            "c:v": "libx264",
            "preset": "ultrafast",
            "vf": "fps=30,format=yuv420p",
            "c:a": "aac",
            "strict": "experimental",
            "hide_banner": None,
            "loglevel": "error",
        },
    ).run()

    return output_path

def cut_audio(input_path: str, output_path: str, start_time: int, end_time: int):
    ffmpeg.input(input_path, ss=start_time).output(
        output_path,
        t=end_time - start_time,
        format="mp3",  # Ensure output format is MP3
        acodec="libmp3lame",  # Use MP3 codec
        audio_bitrate="128k",  # Set bitrate
        y="-y",
        **{
            "hide_banner": None,
            "loglevel": "error",
        },
    ).run()

    return output_path

def concatenate_videos(video_paths, output_path):
    filter_args = "".join([f"[{i}:v:0][{i}:a:0]" for i in range(len(video_paths))])
    filter_args += f"concat=n={len(video_paths)}:v=1:a=1[v][a]"

    ffmpeg_cmd = ["ffmpeg", "-y"]

    for path in video_paths:
        ffmpeg_cmd.extend(["-i", path])

    ffmpeg_cmd.extend(
        [
            "-filter_complex",
            filter_args,
            "-hide_banner",
            "-loglevel",
            "error",
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "28",
            "-r",
            "30",
            "-y",
            output_path,
        ]
    )

    subprocess.run(ffmpeg_cmd, check=True)

    return output_path

def main(config, args):
    # Check if the GPU supports float16
    is_fp16_supported = torch.cuda.is_available() and torch.cuda.get_device_capability()[0] > 7
    dtype = torch.float16 if is_fp16_supported else torch.float32

    print(f"Input video path: {args.video_path}")
    print(f"Input audio path: {args.audio_path}")
    print(f"Loaded checkpoint path: {args.inference_ckpt_path}")

    video_probe = ffmpeg.probe(args.video_path, cmd="ffprobe")
    video_info = next(s for s in video_probe["streams"] if s["codec_type"] == "video")
    video_duration = float(video_info["duration"])

    segment_duration = 5

    if video_duration <= segment_duration:
        total_segments = 1
    else:
        total_segments = int(video_duration // segment_duration)
        if video_duration % segment_duration != 0:
            total_segments += 1

    cut_videos = []

    output_folder_path = 'assets'

    for i in range(total_segments):
        part = f"{i+1}"

        start_time = i * segment_duration
        end_time = (i + 1) * segment_duration

        end_time = min(end_time, video_duration)

        segment_video_output_path = cut_video(
            input_path=args.video_path,
            output_path=os.path.join(
                output_folder_path,
                f"{part}.mp4",
            ),
            start_time=start_time,
            end_time=end_time,
        )

        cut_videos.append(segment_video_output_path)

    cut_audios = []

    for i in range(total_segments):
        part = f"{i+1}"

        start_time = i * segment_duration
        end_time = (i + 1) * segment_duration

        end_time = min(end_time, video_duration)

        segment_video_output_path = cut_audio(
            input_path=args.audio_path,
            output_path=os.path.join(
                output_folder_path,
                f"{part}.mp3",
            ),
            start_time=start_time,
            end_time=end_time,
        )

        cut_audios.append(segment_video_output_path)

    scheduler = DDIMScheduler.from_pretrained("configs")

    if config.model.cross_attention_dim == 768:
        whisper_model_path = "checkpoints/whisper/small.pt"
    elif config.model.cross_attention_dim == 384:
        whisper_model_path = "checkpoints/whisper/tiny.pt"
    else:
        raise NotImplementedError("cross_attention_dim must be 768 or 384")

    audio_encoder = Audio2Feature(model_path=whisper_model_path, device="cuda", num_frames=config.data.num_frames)

    vae = AutoencoderKL.from_pretrained("stabilityai/sd-vae-ft-mse", torch_dtype=dtype)
    vae.config.scaling_factor = 0.18215
    vae.config.shift_factor = 0

    unet, _ = UNet3DConditionModel.from_pretrained(
        OmegaConf.to_container(config.model),
        args.inference_ckpt_path,  # load checkpoint
        device="cpu",
    )

    unet = unet.to(dtype=dtype)

    # set xformers
    if is_xformers_available():
        unet.enable_xformers_memory_efficient_attention()

    pipeline = LipsyncPipeline(
        vae=vae,
        audio_encoder=audio_encoder,
        unet=unet,
        scheduler=scheduler,
    ).to("cuda")

    if args.seed != -1:
        set_seed(args.seed)
    else:
        torch.seed()

    print(f"Initial seed: {torch.initial_seed()}")

    outputs = []

    for i, (video_path, audio_path) in enumerate(zip(cut_videos, cut_audios)):
        print(f"Processing segment {i+1}/{total_segments}")

        video_out_path = f"output_{i+1}.mp4"

        pipeline(
            video_path=video_path,
            audio_path=audio_path,
            video_out_path=video_out_path,
            video_mask_path=video_out_path.replace(".mp4", "_mask.mp4"),
            num_frames=config.data.num_frames,
            num_inference_steps=args.inference_steps,
            guidance_scale=args.guidance_scale,
            weight_dtype=dtype,
            width=config.data.resolution,
            height=config.data.resolution,
        )

        outputs.append(video_out_path)

    print("Combining segments...")

    concatenate_videos(outputs, args.video_out_path)

    print(args.video_out_path)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--unet_config_path", type=str, default="configs/unet.yaml")
    parser.add_argument("--inference_ckpt_path", type=str, required=True)
    parser.add_argument("--video_path", type=str, required=True)
    parser.add_argument("--audio_path", type=str, required=True)
    parser.add_argument("--video_out_path", type=str, required=True)
    parser.add_argument("--inference_steps", type=int, default=20)
    parser.add_argument("--guidance_scale", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=1247)
    args = parser.parse_args()

    config = OmegaConf.load(args.unet_config_path)

    main(config, args)
