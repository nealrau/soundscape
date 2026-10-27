"""Synthetic test material: chord progressions with bass + drums, tones, silence."""

from __future__ import annotations

import wave

import numpy as np

SR = 44100
NOTE = {n: i for i, n in enumerate(["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"])}
NOTE.update({"Db": 1, "Eb": 3, "Gb": 6, "Ab": 8, "Bb": 10})
QUAL = {"": [0, 4, 7], "m": [0, 3, 7], "7": [0, 4, 7, 10], "dim": [0, 3, 6]}


def hz(midi: float, a4: float = 440.0) -> float:
    return a4 * 2 ** ((midi - 69) / 12)


def tone(f: float, dur: float, amp: float = 0.2, decay: float = 1.5, harmonics: int = 8) -> np.ndarray:
    t = np.arange(int(dur * SR)) / SR
    env = np.minimum(1, t / 0.01) * np.exp(-t * decay)
    y = sum((1 / k ** 1.3) * np.sin(2 * np.pi * f * k * t) for k in range(1, harmonics + 1) if f * k < SR / 2)
    return amp * env * y


def parse_chord(name: str):
    root = name[:2] if len(name) > 1 and name[1] in "#b" else name[:1]
    return NOTE[root], QUAL[name[len(root):]]


def progression(chords: list[str], bpm: float = 100, beats_per_chord: int = 4, a4: float = 440.0,
                drums: bool = True, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    beat = 60 / bpm
    cl = beat * beats_per_chord
    out = np.zeros(int((len(chords) * cl + 1.5) * SR))
    for i, name in enumerate(chords):
        root, iv = parse_chord(name)
        start = int(i * cl * SR)
        bass = tone(hz(36 + root, a4), cl, 0.35, 1.0, 6)
        out[start:start + len(bass)] += bass
        for b in range(beats_per_chord):  # re-strike the chord each beat, like a strummed/comped part
            s = start + int(b * beat * SR)
            for v in iv:
                n = tone(hz(60 + (root + v) % 12, a4), beat * 1.2, 0.12, 2.0)
                out[s:s + len(n)] += n[: len(out) - s]
    if drums:
        n_beats = int(len(chords) * beats_per_chord)
        t = np.arange(int(0.15 * SR)) / SR
        kick = 0.6 * np.sin(2 * np.pi * (50 + 80 * np.exp(-t * 40)) * t) * np.exp(-t * 25)
        hat = 0.08 * rng.standard_normal(int(0.04 * SR)) * np.exp(-np.arange(int(0.04 * SR)) / SR * 90)
        for b in range(n_beats):
            s = int(b * beat * SR)
            out[s:s + len(kick)] += kick[: len(out) - s]
            h = s + int(beat / 2 * SR)
            out[h:h + len(hat)] += hat[: max(0, len(out) - h)]
    return (0.8 * out / np.max(np.abs(out))).astype(np.float32)


def write_wav(path: str, x: np.ndarray, sr: int = SR):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
