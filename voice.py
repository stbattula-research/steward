"""Transcribe Telegram voice notes on-device with Whisper (Apple Silicon, via MLX)."""
import asyncio
import logging
from pathlib import Path

import config

log = logging.getLogger("voice")


def _transcribe_sync(path: Path) -> str:
    import mlx_whisper   # optional dependency, imported lazily
    try:
        out = mlx_whisper.transcribe(str(path), path_or_hf_repo=config.WHISPER_MODEL)
    except Exception as e:
        log.warning("Whisper model %s failed (%s); falling back to whisper-tiny", config.WHISPER_MODEL, e)
        out = mlx_whisper.transcribe(str(path), path_or_hf_repo="mlx-community/whisper-tiny")
    return out.get("text", "").strip()


async def transcribe(path: Path) -> str:
    """Returns the text, or raises RuntimeError with a setup hint."""
    try:
        return await asyncio.to_thread(_transcribe_sync, path)
    except ImportError:
        raise RuntimeError("Voice notes aren't set up. Run: .venv/bin/pip install mlx-whisper && brew install ffmpeg")
