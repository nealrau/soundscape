"""DSP building blocks (numpy only), written to stream over long recordings in batches."""

from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

EPS = 1e-10


def frames_view(x: np.ndarray, n: int, hop: int) -> np.ndarray:
    """Non-centred frames: frame i covers x[i*hop : i*hop+n] (zero-padded at the end)."""
    if len(x) < n:
        x = np.pad(x, (0, n - len(x)))
    else:
        rem = (len(x) - n) % hop
        if rem:
            x = np.pad(x, (0, hop - rem))
    return sliding_window_view(x, n)[::hop]


def periodic_hann(n: int) -> np.ndarray:
    return (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(n) / n)).astype(np.float32)


def stft_batches(x: np.ndarray, n_fft: int, hop: int, batch: int = 256):
    """Yield (first_frame_index, magnitude[frames, bins]) scaled so a full-scale sine ~ 1.0."""
    fr = frames_view(x, n_fft, hop)
    win = periodic_hann(n_fft)
    scale = np.float32(2.0 / win.sum())
    for s in range(0, len(fr), batch):
        spec = np.fft.rfft(fr[s:s + batch] * win, axis=1)
        yield s, (np.abs(spec) * scale).astype(np.float32)


def frame_rms(x: np.ndarray, n: int, hop: int) -> np.ndarray:
    n_frames = 1 if len(x) <= n else 1 + -(-(len(x) - n) // hop)
    c = np.concatenate([[0.0], np.cumsum(x.astype(np.float64) ** 2)])
    starts = np.arange(n_frames) * hop
    ends = np.minimum(starts + n, len(x))
    starts = np.minimum(starts, len(x))
    return np.sqrt((c[ends] - c[starts]) / n).astype(np.float32)


def to_db(v, floor: float = -120.0):
    return np.maximum(floor, 20 * np.log10(np.maximum(v, EPS)))


def spectral_peaks(mag: np.ndarray, sr: int, n_fft: int, fmin: float, fmax: float,
                   max_peaks: int = 40, rel_db: float = -55.0, abs_floor: float = 1e-5):
    """Local maxima per frame with parabolic interpolation.
    Returns (frame_idx, freq_hz, amplitude) arrays."""
    lo = max(1, int(np.floor(fmin * n_fft / sr)))
    hi = min(mag.shape[1] - 1, int(np.ceil(fmax * n_fft / sr)))
    if hi - lo < 3:
        return (np.zeros(0, int), np.zeros(0), np.zeros(0))
    l, c, r = mag[:, lo - 1:hi - 1], mag[:, lo:hi], mag[:, lo + 1:hi + 1]
    thr = np.maximum(c.max(axis=1, keepdims=True) * 10 ** (rel_db / 20), abs_floor)
    fi, ki = np.nonzero((c > l) & (c >= r) & (c > thr))
    if len(fi) == 0:
        return (np.zeros(0, int), np.zeros(0), np.zeros(0))
    a = np.log(l[fi, ki] + EPS)
    b = np.log(c[fi, ki] + EPS)
    g = np.log(r[fi, ki] + EPS)
    den = a - 2 * b + g
    p = np.where(np.abs(den) > 1e-12, 0.5 * (a - g) / np.where(den == 0, 1, den), 0.0)
    p = np.clip(p, -0.5, 0.5)
    freq = (lo + ki + p) * sr / n_fft
    amp = np.exp(b - 0.25 * (a - g) * p)
    # keep the strongest max_peaks per frame
    order = np.lexsort((-amp, fi))
    fi, freq, amp = fi[order], freq[order], amp[order]
    _, first, counts = np.unique(fi, return_index=True, return_counts=True)
    rank = np.arange(len(fi)) - np.repeat(first, counts)
    keep = rank < max_peaks
    return fi[keep], freq[keep], amp[keep]


def onset_envelope(x: np.ndarray, sr: int, n_fft: int = 2048, hop: int = 512, n_bands: int = 40):
    """Log-band spectral flux onset strength. Returns (envelope, frames_per_second)."""
    freqs = np.fft.rfftfreq(n_fft, 1 / sr)
    edges = np.geomspace(30, min(sr / 2 - 1, 11000), n_bands + 1)
    idx = np.searchsorted(edges, freqs) - 1
    M = np.zeros((len(freqs), n_bands), np.float32)
    ok = (idx >= 0) & (idx < n_bands)
    M[np.nonzero(ok)[0], idx[ok]] = 1.0
    prev = None
    flux = []
    for _, mag in stft_batches(x, n_fft, hop, batch=1024):
        lb = np.log1p(1000.0 * (mag @ M))
        stacked = lb if prev is None else np.vstack([prev, lb])
        d = np.maximum(0, np.diff(stacked, axis=0)).mean(axis=1)
        flux.append(d if prev is not None else np.concatenate([[0.0], d]))
        prev = lb[-1:]
    env = np.concatenate(flux) if flux else np.zeros(1)
    fps = sr / hop
    w = max(1, int(fps * 1.0))
    local = np.convolve(env, np.ones(w) / w, mode="same")
    env = np.maximum(0, env - local)
    return env.astype(np.float32), fps


def yin(x: np.ndarray, sr: int, fmin: float = 50.0, fmax: float = 1500.0, frame: int = 2048,
        hop: int = 256, threshold: float = 0.15, batch: int = 512):
    """Vectorised YIN. Returns (f0_hz with 0 for unvoiced, aperiodicity in [0,1], hop_seconds)."""
    W = frame // 2
    tau_min = max(2, int(sr / fmax))
    tau_max = min(frame - W - 1, int(sr / fmin))
    fr = frames_view(x, frame, hop)
    f0_all, ap_all = [], []
    taus = np.arange(tau_max + 1)
    for s in range(0, len(fr), batch):
        y = fr[s:s + batch].astype(np.float64)
        n2 = 2 * frame
        Y = np.fft.rfft(y, n=n2, axis=1)
        A = np.fft.rfft(y[:, :W], n=n2, axis=1)
        acf = np.fft.irfft(np.conj(A) * Y, n=n2, axis=1)[:, :tau_max + 1]
        cs = np.concatenate([np.zeros((len(y), 1)), np.cumsum(y ** 2, axis=1)], axis=1)
        e0 = cs[:, W][:, None]
        et = cs[:, taus + W] - cs[:, taus]
        d = np.maximum(0, e0 + et - 2 * acf)
        cum = np.cumsum(d[:, 1:], axis=1)
        cmndf = np.ones_like(d)
        cmndf[:, 1:] = d[:, 1:] * taus[1:] / np.maximum(cum, EPS)
        seg = cmndf[:, tau_min:tau_max]
        is_min = np.zeros_like(seg, bool)
        is_min[:, 1:-1] = (seg[:, 1:-1] <= seg[:, :-2]) & (seg[:, 1:-1] <= seg[:, 2:])
        below = is_min & (seg < threshold)
        has = below.any(axis=1)
        first = np.where(has, below.argmax(axis=1), seg.argmin(axis=1))
        # follow the dip down to its local minimum
        t = first + tau_min
        rows = np.arange(len(y))
        for _ in range(4):
            nxt = np.minimum(t + 1, tau_max)
            step = cmndf[rows, nxt] < cmndf[rows, t]
            t = np.where(step, nxt, t)
        tl, tr = np.maximum(t - 1, 1), np.minimum(t + 1, tau_max)
        a, b, c = cmndf[rows, tl], cmndf[rows, t], cmndf[rows, tr]
        den = a - 2 * b + c
        shift = np.where(np.abs(den) > 1e-12, 0.5 * (a - c) / np.where(den == 0, 1, den), 0)
        tau = t + np.clip(shift, -1, 1)
        ap = np.clip(b, 0, 1)
        energy = cs[:, frame] / frame
        voiced = (ap < max(threshold * 2, 0.3)) & (energy > 1e-7)
        f0_all.append(np.where(voiced, sr / tau, 0.0))
        ap_all.append(ap)
    return np.concatenate(f0_all), np.concatenate(ap_all), hop / sr


def median_filter(v: np.ndarray, k: int) -> np.ndarray:
    if k <= 1 or len(v) < k:
        return v.copy()
    pad = k // 2
    vp = np.pad(v, (pad, pad), mode="edge")
    return np.median(sliding_window_view(vp, k), axis=1)


def viterbi(log_emission: np.ndarray, p_stay: float) -> np.ndarray:
    """Most likely state path with a uniform switch probability. log_emission: [T, S]."""
    T, S = log_emission.shape
    ls, lw = np.log(p_stay), np.log((1 - p_stay) / max(1, S - 1))
    delta = log_emission[0].copy()
    back = np.zeros((T, S), np.int32)
    for t in range(1, T):
        best = int(delta.argmax())
        stay = delta + ls
        switch = delta[best] + lw
        use_switch = switch > stay
        back[t] = np.where(use_switch, best, np.arange(S))
        delta = np.where(use_switch, switch, stay) + log_emission[t]
    path = np.zeros(T, np.int32)
    path[-1] = int(delta.argmax())
    for t in range(T - 1, 0, -1):
        path[t - 1] = back[t, path[t]]
    return path
