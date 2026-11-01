"""Opt-in real ROCm acoustic verification; never part of default checks."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from muli import acoustic_bridge


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MuLi ROCm 声学真实验收（仅手动运行，不下载模型）", allow_abbrev=False
    )
    parser.add_argument("--config", type=Path, required=True, help="provider=rocm 的桥接配置")
    parser.add_argument("--operation", choices=("wake", "verify"), default="wake")
    parser.add_argument("--audio", type=Path, required=True, help="待验证的短 WAV")
    parser.add_argument("--reference", type=Path, help="verify 使用的声纹参考 WAV")
    parser.add_argument("--wake-word", default="沐璃")
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--threshold", type=float)
    return parser.parse_args()


def verify(args: argparse.Namespace) -> dict[str, Any]:
    config = acoustic_bridge.load_config(args.config)
    if config.provider != "rocm":
        raise ValueError("配置 provider 必须为 rocm")
    if not acoustic_bridge.rocm_visible():
        raise ValueError("未检测到 ROCm 设备（/dev/kfd、/dev/dri、/opt/rocm）")
    if args.operation == "verify" and args.reference is None:
        raise ValueError("verify 需要 --reference")
    sample_rate = (
        config.wake.sample_rate
        if args.operation == "wake" and config.wake is not None
        else args.sample_rate
    )
    threshold = (
        args.threshold
        if args.threshold is not None
        else config.wake_threshold
        if args.operation == "wake"
        else config.speaker_threshold
    )
    request = {
        "operation": args.operation,
        "audio_path": str(args.audio),
        "sample_rate": sample_rate,
        "requested_device": "rocm",
        "wake_word": args.wake_word,
        "reference_path": str(args.reference) if args.reference is not None else None,
        "threshold": threshold,
    }
    result = acoustic_bridge.detect(config, request)
    flag = "detected" if args.operation == "wake" else "verified"
    if result.get("provider") != "rocm" or type(result.get(flag)) is not bool:
        raise ValueError("声学桥接未在 ROCm provider 上返回明确布尔结果")
    return {
        "operation": args.operation,
        "provider": result["provider"],
        "configured_provider": result["configured_provider"],
        flag: result[flag],
        "threshold": threshold,
        "score": result.get("score"),
        "runtime_verified": True,
    }


def main() -> None:
    if os.environ.get("MULI_ROCM_CHECK") != "1":
        print("仅手动验收：设置 MULI_ROCM_CHECK=1 后运行", file=sys.stderr)
        raise SystemExit(2)
    args = parse_args()
    try:
        payload = verify(args)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "runtime_verified": False,
                    "provider": "rocm",
                    "error": type(exc).__name__,
                    "reason": str(exc)[:300],
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(1) from None
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    if payload["detected" if args.operation == "wake" else "verified"] is not True:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
