"""Music analysis on top of dsp.py: tuning, chroma, key/mode, chords, tempo/beats, notes."""

from __future__ import annotations

from collections import Counter

import numpy as np

from .dsp import EPS, median_filter, viterbi

PITCH_SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
PITCH_FLAT = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
PITCH_NEUTRAL = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "G#", "A", "Bb", "B"]
MAJOR_KEY_NAMES = ["C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
MINOR_KEY_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "G#", "A", "Bb", "B"]
_FLAT_MAJOR = {5, 10, 3, 8, 1}      # F Bb Eb Ab Db
_FLAT_MINOR = {2, 7, 0, 5, 10, 3}   # Dm Gm Cm Fm Bbm Ebm

# Key profiles (index 0 = tonic). Ensemble: Krumhansl-Kessler (1982), Temperley (2007),
# Sha'ath (KeyFinder, tuned for audio with overtones).
PROFILES = {
    "krumhansl": (
        [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88],
        [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17],
    ),
    "temperley": (
        [0.748, 0.060, 0.488, 0.082, 0.670, 0.460, 0.096, 0.715, 0.104, 0.366, 0.057, 0.400],
        [0.712, 0.084, 0.474, 0.618, 0.049, 0.460, 0.105, 0.747, 0.404, 0.067, 0.133, 0.330],
    ),
    "shaath": (
        [7.24, 3.50, 3.58, 2.85, 5.82, 4.56, 2.45, 6.99, 3.39, 4.56, 4.07, 4.46],
        [7.00, 3.14, 4.36, 5.40, 3.67, 4.09, 3.91, 6.20, 3.63, 2.87, 5.35, 3.83],
    ),
}

MAJOR_SCALE = [0, 2, 4, 5, 7, 9, 11]
MINOR_SCALE = [0, 2, 3, 5, 7, 8, 10]
MODES_MAJOR = {"Ionian (major)": MAJOR_SCALE, "Mixolydian": [0, 2, 4, 5, 7, 9, 10], "Lydian": [0, 2, 4, 6, 7, 9, 11]}
MODES_MINOR = {"Aeolian (natural minor)": MINOR_SCALE, "Dorian": [0, 2, 3, 5, 7, 9, 10],
               "Phrygian": [0, 1, 3, 5, 7, 8, 10], "Harmonic minor": [0, 2, 3, 5, 7, 8, 11]}

CHORD_QUALITIES = [  # (suffix, intervals, penalty)
    ("", [0, 4, 7], 0.0),
    ("m", [0, 3, 7], 0.0),
    ("7", [0, 4, 7, 10], 0.035),
    ("maj7", [0, 4, 7, 11], 0.035),
    ("m7", [0, 3, 7, 10], 0.035),
    ("dim", [0, 3, 6], 0.06),
    ("sus4", [0, 5, 7], 0.06),
]


def key_name(tonic: int, minor: bool) -> str:
    return f"{(MINOR_KEY_NAMES if minor else MAJOR_KEY_NAMES)[tonic]} {'minor' if minor else 'major'}"


def spelling(tonic: int | None, minor: bool | None) -> list[str]:
    if tonic is None:
        return PITCH_NEUTRAL
    if (minor and tonic in _FLAT_MINOR) or (not minor and tonic in _FLAT_MAJOR):
        return PITCH_FLAT
    if (minor and tonic in (9, 4)) or (not minor and tonic in (0, 7)):
        return PITCH_NEUTRAL
    return PITCH_SHARP


def midi_to_name(m: float, names=PITCH_NEUTRAL) -> str:
    n = int(round(m))
    return f"{names[n % 12]}{n // 12 - 1}"


# --- tuning & chroma ------------------------------------------------------------------

def estimate_tuning(freqs: np.ndarray, amps: np.ndarray):
    """Deviation of the recording from A4=440 Hz, in cents (circular mean of peak offsets)."""
    sel = (freqs > 80) & (freqs < 2000)
    if sel.sum() < 20:
        return 0.0, 0.0
    midi = 69 + 12 * np.log2(freqs[sel] / 440.0)
    z = np.sum(np.sqrt(amps[sel]) * np.exp(2j * np.pi * (midi - np.round(midi))))
    z /= np.sum(np.sqrt(amps[sel])) + EPS
    return float(np.angle(z) / (2 * np.pi) * 100), float(np.abs(z))


def chroma_from_peaks(fi, freqs, amps, n_frames: int, ref_hz: float, fmin: float, fmax: float):
    sel = (freqs >= fmin) & (freqs <= fmax)
    f, a, idx = freqs[sel], amps[sel], fi[sel]
    C = np.zeros((n_frames, 12))
    if len(f) == 0:
        return C
    midi = 69 + 12 * np.log2(f / ref_hz)
    n = np.round(midi)
    d = midi - n
    w = np.sqrt(a) * np.exp(-0.5 * (d / 0.2) ** 2) * np.minimum(1.0, 1200.0 / f)
    np.add.at(C, (idx, n.astype(int) % 12), w)
    return C


def _norm_rows(C: np.ndarray, kind: str = "l2") -> np.ndarray:
    if kind == "l1":
        s = C.sum(axis=1, keepdims=True)
    else:
        s = np.sqrt((C ** 2).sum(axis=1, keepdims=True))
    return C / np.maximum(s, EPS)


# --- key & mode -------------------------------------------------------------------------

def key_scores(chroma: np.ndarray) -> np.ndarray:
    """Mean Pearson correlation over the profile ensemble. Index 0-11 major, 12-23 minor."""
    v = chroma - chroma.mean()
    if np.allclose(v, 0):
        return np.zeros(24)
    out = np.zeros(24)
    for maj, mnr in PROFILES.values():
        for mode, prof in ((0, np.array(maj)), (1, np.array(mnr))):
            for t in range(12):
                p = np.roll(prof, t)
                p = p - p.mean()
                out[mode * 12 + t] += np.dot(v, p) / (np.linalg.norm(v) * np.linalg.norm(p) + EPS)
    return out / len(PROFILES)


def rank_keys(chroma: np.ndarray):
    s = key_scores(chroma)
    order = np.argsort(-s)
    return [(int(k % 12), bool(k >= 12), float(s[k])) for k in order]


def mode_hint(chroma: np.ndarray, tonic: int, minor: bool):
    p = chroma / (chroma.sum() + EPS)
    modes = MODES_MINOR if minor else MODES_MAJOR
    shares = {name: float(sum(p[(tonic + i) % 12] for i in iv)) for name, iv in modes.items()}
    default = "Aeolian (natural minor)" if minor else "Ionian (major)"
    best = max(shares, key=shares.get)
    scale = MINOR_SCALE if minor else MAJOR_SCALE
    chromatic = [((tonic + i) % 12, float(p[(tonic + i) % 12])) for i in range(12)
                 if i not in scale and p[(tonic + i) % 12] >= 0.05]
    return {
        "best": best if shares[best] - shares[default] >= 0.02 else default,
        "in_scale_share": shares[default],
        "shares": shares,
        "chromatic": sorted(chromatic, key=lambda t: -t[1]),
    }


# --- chords -------------------------------------------------------------------------------

def _templates():
    labels, T, roots, pens = [], [], [], []
    for root in range(12):
        for suffix, iv, pen in CHORD_QUALITIES:
            v = np.zeros(12)
            v[[(root + i) % 12 for i in iv]] = 1
            T.append(v / np.linalg.norm(v))
            labels.append((root, suffix))
            roots.append(root)
            pens.append(pen)
    return labels, np.array(T), np.array(roots), np.array(pens)


def chord_sequence(seg_chroma: np.ndarray, seg_bass: np.ndarray, seg_active: np.ndarray, p_stay: float):
    """Return a state label per segment: (root, suffix) or None for no-chord."""
    labels, T, roots, pens = _templates()
    S = _norm_rows(seg_chroma)
    B = _norm_rows(seg_bass, "l1")
    score = S @ T.T - pens + 0.12 * B[:, roots]
    n_score = np.where(seg_active & (seg_chroma.sum(axis=1) > EPS), 0.45, 2.0)
    full = np.concatenate([score, n_score[:, None]], axis=1)
    path = viterbi(12.0 * full, p_stay)
    return [labels[i] if i < len(labels) else None for i in path], full.max(axis=1)


def chord_label(root: int, suffix: str, names) -> str:
    return names[root] + suffix


def roman(root: int, suffix: str, tonic: int, minor: bool, modal: bool = False) -> str:
    """Modal centres use accidentals relative to the major scale (Phrygian bII, Mixolydian bVII);
    minor keys use the usual minor-key numerals (III, VI, VII)."""
    deg = (root - tonic) % 12
    major_map = ["I", "bII", "II", "bIII", "III", "IV", "#IV", "V", "bVI", "VI", "bVII", "VII"]
    minor_map = ["I", "bII", "II", "III", "#III", "IV", "#IV", "V", "VI", "#VI", "VII", "#VII"]
    num = (minor_map if minor and not modal else major_map)[deg]
    acc = "".join(ch for ch in num if ch in "b#")
    core = num.lstrip("b#")
    if suffix in ("m", "m7", "dim"):
        core = core.lower()
    tail = {"": "", "m": "", "7": "7", "maj7": "maj7", "m7": "7", "dim": "°", "sus4": "sus4"}[suffix]
    return acc + core + tail


def collapse(times_start, times_end, labels, min_dur: float = 0.3):
    """Merge consecutive equal labels; fold very short segments into their neighbour."""
    segs = []
    for s, e, lab in zip(times_start, times_end, labels):
        if segs and segs[-1][2] == lab:
            segs[-1][1] = e
        else:
            segs.append([s, e, lab])
    changed = True
    while changed and len(segs) > 1:
        changed = False
        for i, (s, e, lab) in enumerate(segs):
            if e - s < min_dur:
                j = i - 1 if i > 0 else i + 1
                if j < i:
                    segs[j][1] = e
                else:
                    segs[j][0] = s
                del segs[i]
                changed = True
                break
        merged = []
        for seg in segs:
            if merged and merged[-1][2] == seg[2]:
                merged[-1][1] = seg[1]
            else:
                merged.append(seg)
        segs = merged
    return [tuple(s) for s in segs]


def find_loop(labels: list):
    """Shortest repeating chord cycle (2-8 chords) covering most of the sequence."""
    n = len(labels)
    for p in range(2, 9):
        if n < 2 * p + 1:
            break
        matches = sum(labels[i] == labels[i + p] for i in range(n - p))
        ratio = matches / (n - p)
        if ratio >= 0.7:
            pattern = [Counter(labels[j::p]).most_common(1)[0][0] for j in range(p)]
            if len(set(pattern)) < 2:
                continue
            return {"pattern": pattern, "repeats": round(n / p, 1), "match": round(ratio, 2)}
    return None


# --- tempo & beats ------------------------------------------------------------------------

def estimate_tempo(env: np.ndarray, fps: float):
    if len(env) < fps * 4 or env.std() < EPS:
        return None
    e = env - env.mean()
    n = len(e)
    nfft = 1 << (2 * n - 1).bit_length()
    ac = np.fft.irfft(np.abs(np.fft.rfft(e, nfft)) ** 2, nfft)[:n]
    ac = ac / (ac[0] + EPS)
    lo, hi = int(fps * 60 / 240), min(n - 2, int(fps * 60 / 40))
    if hi <= lo + 2:
        return None
    lags = np.arange(lo, hi + 1)
    bpm = 60 * fps / lags
    prior = np.exp(-0.5 * (np.log2(bpm / 120.0) / 1.0) ** 2)
    strength = ac[lags] + 0.5 * ac[np.minimum(2 * lags, n - 1)]
    i = int(np.argmax(strength * prior))
    L = lags[i]
    a, b, c = ac[L - 1], ac[L], ac[min(L + 1, n - 1)]
    den = a - 2 * b + c
    Lr = L + (0.5 * (a - c) / den if abs(den) > 1e-12 else 0)
    tempo = 60 * fps / Lr
    alts = []
    for f in (2.0, 0.5, 1.5, 2 / 3):
        t = tempo * f
        if 40 <= t <= 240:
            lag = int(round(60 * fps / t))
            if lag < n and ac[lag] > 0.6 * b:
                alts.append(round(t))
    return {"bpm": float(tempo), "confidence": float(max(0.0, b)), "alternates": alts}


def track_beats(env: np.ndarray, fps: float, bpm: float, tightness: float = 100.0):
    """Dynamic-programming beat tracker (Ellis 2007). Returns beat times in seconds."""
    period = 60 * fps / bpm
    e = env / (env.std() + EPS)
    k = np.arange(-int(period), int(period) + 1)
    win = np.exp(-0.5 * (k * 32.0 / period) ** 2)
    local = np.convolve(e, win, mode="same")
    T = len(local)
    offs = np.arange(-int(round(2 * period)), -int(round(period / 2)) + 1)
    if len(offs) == 0:
        return np.zeros(0)
    tx = -tightness * np.log(-offs / period) ** 2
    cum = np.zeros(T)
    back = -np.ones(T, int)
    for t in range(T):
        idx = t + offs
        ok = idx >= 0
        if not ok.any():
            cum[t] = local[t]
            continue
        cand = cum[idx[ok]] + tx[ok]
        j = int(np.argmax(cand))
        if cand[j] > 0:
            cum[t] = local[t] + cand[j]
            back[t] = idx[ok][j]
        else:
            cum[t] = local[t]
    is_max = np.zeros(T, bool)
    is_max[1:-1] = (cum[1:-1] > cum[:-2]) & (cum[1:-1] >= cum[2:])
    maxima = np.nonzero(is_max)[0]
    if len(maxima) == 0:
        return np.zeros(0)
    med = np.median(cum[maxima])
    last = int(maxima[cum[maxima] > 0.5 * med][-1])
    beats = [last]
    while back[beats[-1]] >= 0:
        beats.append(int(back[beats[-1]]))
    beats = np.array(beats[::-1])
    strength = local[beats]
    thr = 0.5 * np.sqrt(np.mean(strength ** 2) + EPS)
    good = np.nonzero(strength >= thr)[0]
    if len(good):
        beats = beats[good[0]:good[-1] + 1]
    return beats / fps


# --- notes (melody / tuner) -----------------------------------------------------------------

def notes_from_f0(f0: np.ndarray, hop_s: float, ref_hz: float, min_dur: float = 0.08):
    voiced = f0 > 0
    midi = np.full(len(f0), np.nan)
    midi[voiced] = 69 + 12 * np.log2(f0[voiced] / ref_hz)
    sm = midi.copy()
    if voiced.sum() > 5:
        filled = np.where(voiced, midi, 0)
        med = median_filter(filled, 5)
        sm = np.where(voiced, med, np.nan)
        sm = np.where(np.abs(sm - midi) > 1.0, midi, sm)
    notes = []
    cur = None
    for i, m in enumerate(sm):
        if np.isnan(m):
            if cur:
                notes.append(cur)
                cur = None
            continue
        if cur and abs(m - cur["center"]) <= 0.6:
            cur["vals"].append(m)
            cur["end"] = i + 1
            if len(cur["vals"]) % 4 == 0:
                cur["center"] = float(np.median(cur["vals"]))
        else:
            if cur:
                notes.append(cur)
            cur = {"start": i, "end": i + 1, "vals": [m], "center": m}
    if cur:
        notes.append(cur)
    out = []
    for nt in notes:
        dur = (nt["end"] - nt["start"]) * hop_s
        if dur < min_dur:
            continue
        m = float(np.median(nt["vals"]))
        out.append({
            "start": nt["start"] * hop_s,
            "dur": dur,
            "midi": int(round(m)),
            "cents": float((m - round(m)) * 100),
            "hz": float(ref_hz * 2 ** ((m - 69) / 12)),
            "wobble": float(np.std(nt["vals"]) * 100),
        })
    merged = []
    for nt in out:
        if merged and merged[-1]["midi"] == nt["midi"] and nt["start"] - (merged[-1]["start"] + merged[-1]["dur"]) < 0.05:
            prev = merged[-1]
            total = prev["dur"] + nt["dur"]
            prev["cents"] = (prev["cents"] * prev["dur"] + nt["cents"] * nt["dur"]) / total
            prev["dur"] = nt["start"] + nt["dur"] - prev["start"]
        else:
            merged.append(dict(nt))
    return merged
