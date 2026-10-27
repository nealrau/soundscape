"""Run every analysis over one clip and return a plain dict (rendered by report.py)."""

from __future__ import annotations

import numpy as np

from . import music
from .audio_io import ANALYSIS_SR, load_audio, resample
from .dsp import EPS, frame_rms, frames_view, onset_envelope, spectral_peaks, stft_batches, to_db, yin

N_FFT, HOP = 8192, 2048          # harmonic analysis grid (~370 ms window, ~93 ms hop @ 22.05 kHz)
BANDS = [("sub", 20, 60), ("bass", 60, 250), ("low-mid", 250, 500), ("mid", 500, 2000),
         ("upper-mid", 2000, 4000), ("presence", 4000, 6000), ("air", 6000, 11025)]


# --- loudness (ITU-R BS.1770, computed per block in the frequency domain) -------------------

def _k_weight_power(freqs: np.ndarray, fs: float) -> np.ndarray:
    def biquad_resp(b, a):
        z = np.exp(-2j * np.pi * freqs / fs)
        return np.abs((b[0] + b[1] * z + b[2] * z ** 2) / (a[0] + a[1] * z + a[2] * z ** 2)) ** 2

    f0, G, Q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    K = np.tan(np.pi * f0 / fs)
    Vh = 10 ** (G / 20)
    Vb = Vh ** 0.4996667741545416
    a0 = 1 + K / Q + K * K
    shelf = biquad_resp([(Vh + Vb * K / Q + K * K) / a0, 2 * (K * K - Vh) / a0, (Vh - Vb * K / Q + K * K) / a0],
                        [1, 2 * (K * K - 1) / a0, (1 - K / Q + K * K) / a0])
    f0, Q = 38.13547087602444, 0.5003270373238773
    K = np.tan(np.pi * f0 / fs)
    a0 = 1 + K / Q + K * K
    hp = biquad_resp([1, -2, 1], [1, 2 * (K * K - 1) / a0, (1 - K / Q + K * K) / a0])
    return shelf * hp


def loudness(x: np.ndarray, sr: int, stereo_correction: bool):
    n, hop = int(0.4 * sr), int(0.1 * sr)
    if len(x) < n:
        return None
    fr = frames_view(x, n, hop)
    w = _k_weight_power(np.fft.rfftfreq(n, 1 / sr), sr)
    w[1:-1] *= 2
    ms = []
    for s in range(0, len(fr), 128):
        X = np.fft.rfft(fr[s:s + 128].astype(np.float64), axis=1)
        ms.append((np.abs(X) ** 2 * w).sum(axis=1) / n / n)
    ms = np.concatenate(ms)
    offset = -0.691 + (3.01 if stereo_correction else 0.0)
    block = offset + 10 * np.log10(ms + 1e-20)
    gated = ms[block > -70]
    if len(gated) == 0:
        return None
    rel = offset + 10 * np.log10(gated.mean()) - 10
    final = gated[(offset + 10 * np.log10(gated + 1e-20)) > rel]
    integrated = offset + 10 * np.log10(final.mean())
    # short-term (3 s) loudness range: 10th..95th percentile
    k = 30
    st = np.convolve(ms, np.ones(k) / k, mode="valid") if len(ms) >= k else ms
    st_db = offset + 10 * np.log10(st + 1e-20)
    st_db = st_db[st_db > -70]
    lra = float(np.percentile(st_db, 95) - np.percentile(st_db, 10)) if len(st_db) > 5 else 0.0
    return {"lufs": float(integrated), "short_term_max": float(st_db.max()) if len(st_db) else None, "lra": lra}


# --- helpers ----------------------------------------------------------------------------

def _activity(rms_db: np.ndarray):
    if len(rms_db) == 0:
        return np.zeros(0, bool), -60.0
    p95 = float(np.percentile(rms_db, 95))
    floor = float(np.percentile(rms_db, 10))
    thr = max(-60.0, p95 - 45)
    if floor < -50 and p95 - floor > 15:
        thr = max(thr, floor + 6)
    thr = min(thr, p95 - 10)
    return rms_db > thr, thr


def _segment_means(frame_times, values, bounds):
    idx = np.searchsorted(bounds, frame_times, side="right") - 1
    ok = (idx >= 0) & (idx < len(bounds) - 1)
    out = np.zeros((len(bounds) - 1, values.shape[1]))
    cnt = np.zeros(len(bounds) - 1)
    np.add.at(out, idx[ok], values[ok])
    np.add.at(cnt, idx[ok], 1)
    return out / np.maximum(cnt, 1)[:, None], cnt


def _clip01(v):
    return float(min(1.0, max(0.0, v)))


# --- main -------------------------------------------------------------------------------

def analyze(path: str, start: float = 0.0, end: float | None = None, source: str = "file",
            trim: bool = True) -> dict:
    x_nat, sr_nat, total = load_audio(path, start, end)
    res: dict = {"source": source, "path": path, "file_duration": total, "window": [start, end],
                 "captured_seconds": len(x_nat) / sr_nat if sr_nat else 0.0}
    if len(x_nat) < sr_nat * 0.25:
        res["status"] = "too_short"
        return res

    peak = float(np.max(np.abs(x_nat)))
    res["peak_dbfs"] = float(to_db(peak))
    res["clipped_ratio"] = float(np.mean(np.abs(x_nat) >= 0.999))

    # Activity on a ~23 ms grid, then trim leading/trailing silence.
    rms_n, rms_hop = int(0.0464 * sr_nat), int(0.0232 * sr_nat)
    rms_db = to_db(frame_rms(x_nat, rms_n, rms_hop))
    active, thr = _activity(rms_db)
    res["silence_threshold_db"] = thr
    res["level_spread_db"] = float(np.percentile(rms_db, 95) - np.percentile(rms_db, 10))
    if peak < 10 ** (-80 / 20) or active.sum() == 0:
        res["status"] = "silent"
        res["active_seconds"] = 0.0
        return res
    first, last = np.nonzero(active)[0][[0, -1]]
    t0 = max(0, first * rms_hop - int(0.25 * sr_nat)) if trim else 0
    t1 = min(len(x_nat), last * rms_hop + rms_n + int(0.25 * sr_nat)) if trim else len(x_nat)
    res["trimmed"] = [t0 / sr_nat, t1 / sr_nat]
    res["active_seconds"] = float(active.sum() * rms_hop / sr_nat)
    res["active_ratio"] = float(active.mean())
    x_nat = x_nat[t0:t1]
    offset_s = start + t0 / sr_nat
    res["offset"] = offset_s
    res["duration"] = len(x_nat) / sr_nat
    res["rms_dbfs"] = float(to_db(np.sqrt(np.mean(x_nat.astype(np.float64) ** 2))))
    res["loudness"] = loudness(x_nat, sr_nat, stereo_correction=(source == "output"))

    x = resample(x_nat, sr_nat, ANALYSIS_SR)
    sr = ANALYSIS_SR
    del x_nat

    # --- harmonic grid pass: peaks + spectral shape ---
    fr_rms = frame_rms(x, N_FFT, HOP)
    n_frames = len(fr_rms)
    frame_times = (np.arange(n_frames) * HOP + N_FFT / 2) / sr
    frame_active, _ = _activity(to_db(fr_rms))
    freqs_bins = np.fft.rfftfreq(N_FFT, 1 / sr)
    band_idx = [(name, (freqs_bins >= lo) & (freqs_bins < hi)) for name, lo, hi in BANDS]
    band_pow = np.zeros((n_frames, len(BANDS)))
    centroid = np.zeros(n_frames)
    flatness = np.zeros(n_frames)
    flat_sel = (freqs_bins > 100) & (freqs_bins < 8000)
    P_fi, P_f, P_a = [], [], []
    for s, mag in stft_batches(x, N_FFT, HOP, batch=128):
        e = slice(s, s + len(mag))
        pw = mag.astype(np.float64) ** 2
        for j, (_, sel) in enumerate(band_idx):
            band_pow[e, j] = pw[:, sel].sum(axis=1)
        tot = pw.sum(axis=1) + EPS
        centroid[e] = (pw * freqs_bins).sum(axis=1) / tot
        fp = pw[:, flat_sel] + 1e-12
        flatness[e] = np.exp(np.log(fp).mean(axis=1)) / fp.mean(axis=1)
        fi, f, a = spectral_peaks(mag, sr, N_FFT, 30, 5000)
        P_fi.append(fi + s)
        P_f.append(f)
        P_a.append(a)
    P_fi, P_f, P_a = np.concatenate(P_fi), np.concatenate(P_f), np.concatenate(P_a)
    act = frame_active if frame_active.any() else np.ones(n_frames, bool)

    bp = band_pow[act].sum(axis=0)
    res["spectrum"] = {
        "bands": {name: float(v) for (name, _, _), v in zip(BANDS, bp / (bp.sum() + EPS))},
        "centroid_hz": float(np.median(centroid[act])),
        "flatness": float(np.median(flatness[act])),
    }

    # --- tuning, chroma, key ---
    tune_cents, tune_strength = music.estimate_tuning(P_f, P_a)
    ref = 440.0 * 2 ** (tune_cents / 1200)
    res["tuning"] = {"cents": tune_cents, "a4_hz": ref, "strength": tune_strength}
    chroma = music.chroma_from_peaks(P_fi, P_f, P_a, n_frames, ref, 65, 2100)
    bass = music.chroma_from_peaks(P_fi, P_f, P_a, n_frames, ref, 35, 260)
    tonal = act & (chroma.sum(axis=1) > EPS)
    if tonal.sum() >= 3:
        cn = chroma[tonal] / chroma[tonal].sum(axis=1, keepdims=True)
        gchroma = cn.mean(axis=0)
        bn = bass[tonal] / np.maximum(bass[tonal].sum(axis=1, keepdims=True), EPS)
        gbass = bn.mean(axis=0)
        keys = music.rank_keys(gchroma)
        res["pitch_classes"] = gchroma.tolist()
        res["bass_pitch_classes"] = (gbass / (gbass.sum() + EPS)).tolist()
        res["keys"] = keys[:6]
        res["key_strength"] = keys[0][2]
    else:
        gchroma = gbass = None
        res["keys"] = []
        res["key_strength"] = 0.0

    # --- rhythm ---
    env, fps = onset_envelope(x, sr)
    tempo = music.estimate_tempo(env, fps)
    beats = np.zeros(0)
    if tempo and tempo["confidence"] >= 0.12:
        beats = music.track_beats(env, fps, tempo["bpm"])
        if len(beats) > 3:
            ibi = np.diff(beats)
            tempo["beats"] = int(len(beats))
            tempo["stability"] = float(np.std(ibi) / (np.mean(ibi) + EPS))
            tempo["bpm_from_beats"] = float(60 / np.median(ibi))
    res["tempo"] = tempo
    if len(env) > 10:
        pk = (env[1:-1] > env[:-2]) & (env[1:-1] >= env[2:]) & (env[1:-1] > env.mean() + 1.5 * env.std())
        res["onset_rate"] = float(pk.sum() / (len(env) / fps))

    # --- chords (beat-synchronous when the pulse is clear) ---
    dur = len(x) / sr
    if len(beats) > 8 and tempo and tempo["confidence"] >= 0.2:
        bounds = np.concatenate([[0.0], beats, [dur]])
        p_stay, grid = 0.7, "beat"
    else:
        bounds = np.arange(0, dur + 0.5, 0.5)
        p_stay, grid = 0.85, "0.5 s"
    seg_c, cnt = _segment_means(frame_times, chroma, bounds)
    seg_b, _ = _segment_means(frame_times, bass, bounds)
    seg_a, _ = _segment_means(frame_times, act[:, None].astype(float), bounds)
    seg_active = (seg_a[:, 0] > 0.5) & (cnt > 0)
    if len(seg_c) >= 2 and gchroma is not None:
        labels, conf = music.chord_sequence(seg_c, seg_b, seg_active, p_stay)
        segs = music.collapse(bounds[:-1], bounds[1:], labels)
        res["chords"] = {"grid": grid, "segments": [(s, e, lab) for s, e, lab in segs]}
    else:
        res["chords"] = {"grid": grid, "segments": []}

    # Settle relative major/minor ambiguity with tonic evidence (bass, first/last chord).
    if res["keys"]:
        res["key_choice"] = _choose_key(res, gbass)
        tonic, minor = res["key_choice"]["tonic"], res["key_choice"]["minor"]
        res["mode"] = music.mode_hint(gchroma, tonic, minor)
        res["key_sections"] = _key_sections(chroma, tonal, frame_times) if dur >= 45 else []

    # --- pitch track (melody / tuner / voice) ---
    hop = 256 if dur <= 180 else 512
    f0, ap, hop_s = yin(x, sr, hop=hop)
    rms_y = to_db(frame_rms(x, 2048, hop))[: len(f0)]
    y_act, _ = _activity(rms_y)
    f0 = np.where(y_act[: len(f0)], f0, 0)
    voiced = f0 > 0
    res["voiced_ratio"] = float(voiced.sum() / max(1, y_act.sum()))
    res["aperiodicity"] = float(np.median(ap[voiced])) if voiced.any() else 1.0
    if voiced.any():
        res["f0_stats"] = {"median_hz": float(np.median(f0[voiced])),
                           "p10_hz": float(np.percentile(f0[voiced], 10)),
                           "p90_hz": float(np.percentile(f0[voiced], 90))}
    # Notes feed the content classifier too, so they are always extracted.
    notes = music.notes_from_f0(f0, hop_s, ref)
    res["notes"] = notes
    res["tuner_notes"] = music.notes_from_f0(f0, hop_s, 440.0, min_dur=0.25)
    steady_time = sum(n["dur"] for n in notes if n["dur"] >= 0.12)
    res["note_steadiness"] = float(steady_time / max(hop_s, voiced.sum() * hop_s))

    # --- syllable-rate modulation (speech cue) & pauses ---
    env_hop = int(sr * 0.01)
    e100 = frame_rms(x, env_hop * 2, env_hop)
    res.update(_speech_cues(e100, 100.0))
    res["content"] = _classify(res)
    res["timeline"] = _timeline(res, x, sr, frame_times, act)
    return res


def _choose_key(res: dict, gbass) -> dict:
    keys = res["keys"]
    (t1, m1, s1), (t2, m2, s2) = keys[0], keys[1]
    choice = {"tonic": t1, "minor": m1, "score": s1, "reason": None}
    relative = (m1 != m2) and ((t1 + (3 if m1 else -3)) % 12 == t2)
    if not relative or s1 - s2 > 0.15 or gbass is None:
        return choice

    def evidence(tonic):
        ev = 1.5 * gbass[tonic]
        segs = [s for s in res["chords"]["segments"] if s[2] is not None]
        if segs:
            if segs[0][2][0] == tonic:
                ev += 0.15
            if segs[-1][2][0] == tonic:
                ev += 0.25
            total = sum(e - s for s, e, _ in segs)
            ev += 0.5 * sum(e - s for s, e, lab in segs if lab[0] == tonic) / max(total, EPS)
        return ev

    e1, e2 = evidence(t1), evidence(t2)
    # Profiles barely separate relative keys; where the music "lands" usually decides it.
    if e2 > e1 * 1.2 and (e2 - e1) * 0.3 > s1 - s2:
        choice = {"tonic": t2, "minor": m2, "score": s2,
                  "reason": f"{music.key_name(t1, m1)} uses the same notes and fits the pitch profile slightly better, "
                            f"but the music centres on {music.MINOR_KEY_NAMES[t2] if m2 else music.MAJOR_KEY_NAMES[t2]} "
                            f"(opening/closing chord, bass, time spent) — tonic evidence {e2:.2f} vs {e1:.2f}"}
    else:
        choice["reason"] = (f"relative key {music.key_name(t2, m2)} shares these notes; tonic evidence still favours "
                            f"{music.key_name(t1, m1)} ({e1:.2f} vs {e2:.2f})")
    choice["evidence"] = [e1, e2]
    return choice


def _key_sections(chroma, tonal, frame_times, win_s=20.0, hop_s=10.0):
    out = []
    end = frame_times[-1] if len(frame_times) else 0
    t = 0.0
    while t + win_s * 0.6 <= end:
        sel = tonal & (frame_times >= t) & (frame_times < t + win_s)
        if sel.sum() >= 20:
            c = (chroma[sel] / chroma[sel].sum(axis=1, keepdims=True)).mean(axis=0)
            tonic, minor, score = music.rank_keys(c)[0]
            out.append((t, min(t + win_s, end), tonic, minor, score))
        t += hop_s
    return out


def _speech_cues(e100: np.ndarray, rate: float) -> dict:
    if len(e100) < rate * 2:
        return {"syllabic_modulation": 0.0, "pause_rate": 0.0}
    env = np.log(e100 + 1e-5)
    env = env - np.convolve(env, np.ones(50) / 50, mode="same")
    spec = np.abs(np.fft.rfft(env * np.hanning(len(env)))) ** 2
    f = np.fft.rfftfreq(len(env), 1 / rate)
    band = spec[(f >= 3) & (f <= 8)].sum()
    total = spec[(f >= 0.5) & (f <= 16)].sum() + EPS
    peak_f = float(f[(f >= 2) & (f <= 10)][np.argmax(spec[(f >= 2) & (f <= 10)])])
    db = 20 * np.log10(e100 + 1e-7)
    loud = np.percentile(db, 90)
    quiet = db < loud - 25
    # count short pauses (60-600 ms) bounded by sound
    pauses, run = 0, 0
    for q in quiet:
        if q:
            run += 1
        else:
            if 6 <= run <= 60:
                pauses += 1
            run = 0
    return {"syllabic_modulation": float(band / total), "syllable_rate_hz": peak_f,
            "pause_rate": float(pauses / (len(e100) / rate))}


def _classify(res: dict) -> dict:
    tempo_conf = (res.get("tempo") or {}).get("confidence", 0.0)
    key_r = res.get("key_strength", 0.0)
    steady = res.get("note_steadiness", 0.0)
    voiced = res.get("voiced_ratio", 0.0)
    flat = res["spectrum"]["flatness"]
    f0m = (res.get("f0_stats") or {}).get("median_hz", 0)
    music_s = (0.35 * _clip01((key_r - 0.45) / 0.35) + 0.25 * _clip01(tempo_conf / 0.35)
               + 0.25 * _clip01(steady) + 0.15 * (1 - _clip01(res.get("pause_rate", 0) / 2)))
    speech_s = (0.35 * _clip01((res.get("syllabic_modulation", 0) - 0.2) / 0.35)
                + 0.25 * _clip01(1 - steady) * _clip01(voiced / 0.3)
                + 0.25 * _clip01(res.get("pause_rate", 0) / 1.2)
                + 0.15 * (1.0 if 75 <= f0m <= 300 else 0.0))
    # Conversation is punctuated by short pauses; hum, fans and drones are not.
    speech_s *= 0.4 + 0.6 * _clip01(res.get("pause_rate", 0) / 0.5)
    noise_s = _clip01((flat - 0.15) / 0.3) * (1 - _clip01(voiced / 0.3))
    if res.get("level_spread_db", 99) < 6 and key_r < 0.5 and tempo_conf < 0.2:
        noise_s = max(noise_s, 0.75)  # flat, featureless level: room tone, fan, hiss
    scores = {"music": music_s, "speech": speech_s, "noise/ambience": noise_s}
    label = max(scores, key=scores.get)
    if scores[label] < 0.35:
        label = "noise/ambience"  # nothing fits: room tone, hum, texture
    if music_s > 0.5 and speech_s > 0.5 and abs(music_s - speech_s) < 0.15:
        label = "speech + music (or vocals)"
    top = max(scores.values())
    return {"label": label, "scores": scores,
            "confidence": "high" if top > 0.7 else "medium" if top > 0.45 else "low"}


def _timeline(res, x, sr, frame_times, act):
    dur = len(x) / sr
    if dur < 30:
        return []
    step = next((s for s in (5, 10, 15, 20, 30, 60, 120) if dur / s <= 12), 120)
    segs = [s for s in res["chords"]["segments"] if s[2] is not None]
    rows = []
    for t in np.arange(0, dur, step):
        t_end = min(dur, t + step)
        seg = x[int(t * sr):int(t_end * sr)]
        level = float(to_db(np.sqrt(np.mean(seg.astype(np.float64) ** 2)))) if len(seg) else -120
        a = act[(frame_times >= t) & (frame_times < t_end)]
        chords = {}
        for s, e, lab in segs:
            ov = min(e, t_end) - max(s, t)
            if ov > 0:
                chords[lab] = chords.get(lab, 0) + ov
        top = [lab for lab, _ in sorted(chords.items(), key=lambda kv: -kv[1])[:3]]
        rows.append({"start": float(t), "end": float(t_end), "level_db": level,
                     "active": float(a.mean()) if len(a) else 0.0, "chords": top})
    return rows
