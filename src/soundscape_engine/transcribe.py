"""Optional on-device speech transcription (mlx-whisper on Apple Silicon). Never required."""

from __future__ import annotations

import importlib.util
import os

DEFAULT_MODEL = "mlx-community/whisper-small-mlx"


def available() -> str | None:
    return "mlx-whisper" if importlib.util.find_spec("mlx_whisper") else None


def transcribe(path: str, start: float = 0.0, end: float | None = None, full: bool = False) -> dict:
    if not available():
        return {"error": "not installed (run the soundscape setup tool with install_transcription=true)"}
    import mlx_whisper

    from .audio_io import load_audio, resample

    x, sr, _ = load_audio(path, start, end)
    model = os.environ.get("SOUNDSCAPE_WHISPER_MODEL", DEFAULT_MODEL)
    try:
        out = mlx_whisper.transcribe(resample(x, sr, 16000), path_or_hf_repo=model, verbose=None)
    except Exception as e:  # model download / runtime failures should not break the report
        return {"error": f"{type(e).__name__}: {e}"[:300]}
    text = out.get("text", "")
    if full and out.get("segments"):
        text = "\n".join(f"  [{int(s['start'] // 60)}:{int(s['start'] % 60):02d}] {s['text'].strip()}"
                         for s in out["segments"])
        text = "\n" + text
    return {"text": text, "language": out.get("language"), "model": model}
