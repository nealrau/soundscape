"""Audio loading: a tolerant WAV reader (works on files still being recorded) plus
conversion of any other format through ffmpeg or macOS's built-in afconvert."""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile

import numpy as np

ANALYSIS_SR = 22050

_EXTRA_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")


def _which(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for d in _EXTRA_BIN_DIRS:
        p = os.path.join(d, name)
        if os.access(p, os.X_OK):
            return p
    return None


def _parse_wav_header(f):
    head = f.read(12)
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    fmt = None
    while True:
        hdr = f.read(8)
        if len(hdr) < 8:
            raise ValueError("no data chunk")
        cid, size = hdr[:4], struct.unpack("<I", hdr[4:])[0]
        if cid == b"fmt ":
            body = f.read(size + (size & 1))
            tag, ch, sr, _, align, bits = struct.unpack("<HHIIHH", body[:16])
            if tag == 0xFFFE and len(body) >= 26:  # WAVE_FORMAT_EXTENSIBLE
                tag = struct.unpack("<H", body[24:26])[0]
            fmt = (tag, ch, sr, align, bits)
        elif cid == b"data":
            if fmt is None:
                raise ValueError("data before fmt chunk")
            offset = f.tell()
            f.seek(0, os.SEEK_END)
            avail = f.tell() - offset
            # A file still being written may carry a stale (or zero) size: trust what's on disk.
            length = size if 0 < size <= avail else avail
            return fmt, offset, length
        else:
            f.seek(size + (size & 1), os.SEEK_CUR)


def read_wav(path: str, start: float = 0.0, end: float | None = None):
    """Return (mono float32 samples, sample_rate, total_duration_seconds)."""
    with open(path, "rb") as f:
        (tag, ch, sr, align, bits), offset, length = _parse_wav_header(f)
        total_frames = length // align
        s = max(0, int(start * sr))
        e = total_frames if end is None or end <= 0 else min(total_frames, int(end * sr))
        if e <= s:
            return np.zeros(0, np.float32), sr, total_frames / sr
        f.seek(offset + s * align)
        raw = f.read((e - s) * align)
    n = len(raw) // align
    raw = raw[: n * align]
    if tag == 3:
        dt = "<f4" if bits == 32 else "<f8"
        x = np.frombuffer(raw, dt).astype(np.float32)
    elif tag == 1:
        if bits == 16:
            x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
        elif bits == 24:
            b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
            v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
            v = np.where(v >= 1 << 23, v - (1 << 24), v)
            x = v.astype(np.float32) / float(1 << 23)
        elif bits == 32:
            x = np.frombuffer(raw, "<i4").astype(np.float32) / 2147483648.0
        elif bits == 8:
            x = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128.0) / 128.0
        else:
            raise ValueError(f"unsupported PCM bit depth {bits}")
    else:
        raise ValueError(f"unsupported WAV format tag {tag}")
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return np.ascontiguousarray(x, np.float32), sr, total_frames / sr


def convert_to_wav(path: str) -> str:
    """Decode any audio/video file to a temporary mono WAV. Caller deletes it."""
    fd, out = tempfile.mkstemp(suffix=".wav", prefix="soundscape-")
    os.close(fd)
    ffmpeg = _which("ffmpeg")
    if ffmpeg:
        cmd = [ffmpeg, "-nostdin", "-v", "error", "-y", "-i", path, "-vn", "-ac", "1",
               "-ar", str(ANALYSIS_SR * 2), "-c:a", "pcm_s16le", out]
    else:
        afconvert = _which("afconvert")
        if not afconvert:
            os.unlink(out)
            raise RuntimeError("cannot decode this file: install ffmpeg (brew install ffmpeg)")
        cmd = [afconvert, "-f", "WAVE", "-d", "LEI16", "-c", "1", path, out]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        os.unlink(out)
        raise RuntimeError(f"could not decode {os.path.basename(path)}: {proc.stderr.strip()[:300]}")
    return out


def load_audio(path: str, start: float = 0.0, end: float | None = None):
    """Load any audio file as mono float32. Returns (x, sr, total_duration)."""
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    try:
        return read_wav(path, start, end)
    except ValueError:
        tmp = convert_to_wav(path)
        try:
            return read_wav(tmp, start, end)
        finally:
            os.unlink(tmp)


def _next_fast_len(n: int) -> int:
    best = 1 << max(0, (n - 1).bit_length())
    p5 = 1
    while p5 < best:
        p35 = p5
        while p35 < best:
            q = p35
            while q < n:
                q *= 2
            best = min(best, q)
            p35 *= 3
        p5 *= 5
    return best


def resample(x: np.ndarray, sr: int, target: int = ANALYSIS_SR) -> np.ndarray:
    """Band-limited FFT resampling (ideal low-pass), padded to a fast FFT length."""
    if sr == target or len(x) == 0:
        return x.astype(np.float32, copy=False)
    n_out = int(round(len(x) * target / sr))
    n_pad = _next_fast_len(len(x))
    n_pad_out = int(round(n_pad * target / sr))
    X = np.fft.rfft(x.astype(np.float64), n=n_pad)
    keep = min(len(X), n_pad_out // 2 + 1)
    Y = np.zeros(n_pad_out // 2 + 1, np.complex128)
    Y[:keep] = X[:keep]
    y = np.fft.irfft(Y, n=n_pad_out) * (n_pad_out / n_pad)
    return y[:n_out].astype(np.float32)
