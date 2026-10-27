"""Render analysis results as a compact, token-efficient text report for Claude."""

from __future__ import annotations

from collections import Counter, defaultdict

from . import music, theory

FOCI = ("overview", "key", "chords", "tempo", "melody", "tuning", "mix", "speech", "theory", "all")
WEAK_KEY = 0.45  # below this profile fit there is no meaningful key
SOURCE_LABEL = {"output": "system audio output", "input": "input device", "file": "file"}


def ts(t: float, precise: bool = False) -> str:
    m, s = divmod(max(0.0, t), 60)
    return f"{int(m)}:{s:04.1f}" if precise else f"{int(m)}:{int(s):02d}"


def _bar(v: float, vmax: float, width: int = 10) -> str:
    n = int(round(width * v / vmax)) if vmax > 0 else 0
    return "█" * n + "·" * (width - n)


def _conf_word(v: float, hi: float, mid: float) -> str:
    return "high" if v >= hi else "medium" if v >= mid else "low"


def mode_short(mode: str | None) -> str | None:
    return mode.split(" (")[0] if mode else None


def centre_label(res: dict) -> str | None:
    """'G# Phrygian', 'D Dorian', 'A minor'… — the key as a musician would name it."""
    ce = res.get("centre")
    if not ce:
        return None
    names = music.spelling(ce["parent"], False) if ce.get("override") else music.spelling(ce["tonic"], ce["minor"])
    if ce.get("mode"):
        return f"{names[ce['tonic']]} {mode_short(ce['mode'])}"
    return music.key_name(ce["tonic"], ce["minor"])


class Ctx:
    def __init__(self, res: dict):
        self.res = res
        ce = res.get("centre")
        self.tonic = ce["tonic"] if ce else None
        self.minor = ce["minor"] if ce else None
        self.modal = bool(ce and ce.get("mode") and not ce["mode"].startswith(("Aeolian", "Harmonic")))
        if ce and ce.get("override"):
            self.names = music.spelling(ce["parent"], False)
        else:
            self.names = music.spelling(self.tonic, self.minor)
        self.label = centre_label(res)
        self.t0 = res.get("offset", 0.0)

    def chord(self, lab) -> str:
        return "N.C." if lab is None else music.chord_label(lab[0], lab[1], self.names)

    def numeral(self, lab) -> str:
        if lab is None or self.tonic is None:
            return "—"
        return music.roman(lab[0], lab[1], self.tonic, self.minor, self.modal)


# --- sections ------------------------------------------------------------------------------

def key_confidence(res: dict) -> str:
    keys, kc = res.get("keys") or [], res.get("key_choice") or {}
    if not keys:
        return "none"
    s1 = kc.get("score", keys[0][2])
    others = [k for k in keys if (k[0], k[1]) != (kc.get("tonic"), kc.get("minor"))]
    margin = s1 - others[0][2] if others else 1.0
    rel_close = "tonic evidence" in (kc.get("reason") or "")
    if s1 >= 0.75 and (margin >= 0.06 or rel_close):
        return "high"
    return "medium" if s1 >= 0.55 else "low"


def sec_key(c: Ctx, full: bool) -> list[str]:
    res = c.res
    if not res.get("keys"):
        return ["KEY: no stable pitched content to estimate a key."]
    kc = res["key_choice"]
    name = music.key_name(kc["tonic"], kc["minor"])
    if kc["score"] < WEAK_KEY:
        return [f"KEY: no clear tonal centre — best guess {name}, but the fit is weak ({kc['score']:.2f}). "
                "Likely noise, speech, percussion, or very dense/atonal audio."]
    ce = res.get("centre") or {}
    if ce.get("override"):
        parent = music.spelling(ce["parent"], False)[ce["parent"]]
        rel_minor = music.MINOR_KEY_NAMES[(ce["parent"] + 9) % 12]
        out = [f"KEY: {c.label}  (tonal centre {c.names[ce['tonic']]}; confidence {ce['confidence']})",
               f"  same notes as {parent} major / {rel_minor} minor — a key-profile reading of the notes alone says "
               f"{name} ({kc['score']:.2f}), but the music rests on {c.names[ce['tonic']]}: bass "
               f"{ce['bass_share'] * 100:.0f}% on it, chords rooted there {ce['chord_share'] * 100:.0f}% of the time"]
        if ce.get("runner_up"):
            t2, m2 = ce["runner_up"]
            out.append(f"  other centre considered: {c.names[t2]} {mode_short(m2)}")
        out.append(f"  {ce['in_scale'] * 100:.0f}% of pitched energy fits the {c.label} scale")
        tune = res.get("tuning") or {}
        if tune and (full or abs(tune.get("cents", 0)) >= 8):
            out.append(f"  tuning: A4 ≈ {tune['a4_hz']:.1f} Hz ({tune['cents']:+.0f} cents vs 440)")
        return out
    conf = key_confidence(res)
    head = c.label if ce.get("mode") else name
    out = [f"KEY: {head}  (confidence {conf}; profile fit {kc['score']:.2f})"]
    alts = []
    for t, m, s in res["keys"]:
        if (t, m) == (kc["tonic"], kc["minor"]):
            continue
        tag = ""
        if m != kc["minor"] and (t - kc["tonic"]) % 12 == (3 if kc["minor"] else 9):
            tag = ", relative"
        elif t == kc["tonic"]:
            tag = ", parallel"
        elif (t - kc["tonic"]) % 12 in (5, 7) and m == kc["minor"]:
            tag = ", neighbour"
        alts.append(f"{music.key_name(t, m)} {s:.2f}{tag}")
        if len(alts) == (3 if full else 2):
            break
    out.append("  next best: " + "; ".join(alts))
    if kc.get("reason"):
        out.append(f"  note: {kc['reason']}")
    mode = res.get("mode") or {}
    if full and mode:
        chrom = ", ".join(f"{c.names[pc]} {sh * 100:.0f}%" for pc, sh in mode["chromatic"][:4])
        out.append(f"  mode: {mode['best']}; {mode['in_scale_share'] * 100:.0f}% of pitched energy is in-scale"
                   + (f"; chromatic notes: {chrom}" if chrom else ""))
    elif mode and mode.get("best") not in ("Ionian (major)", "Aeolian (natural minor)"):
        out.append(f"  mode hint: {mode['best']}")
    tune = res.get("tuning") or {}
    if tune and (full or abs(tune.get("cents", 0)) >= 8):
        out.append(f"  tuning: A4 ≈ {tune['a4_hz']:.1f} Hz ({tune['cents']:+.0f} cents vs 440)")
    if full and res.get("pitch_classes"):
        pcs = res["pitch_classes"]
        order = sorted(range(12), key=lambda i: -pcs[i])
        vmax = pcs[order[0]]
        out.append("  pitch-class share (strongest first):")
        for i in order[:8]:
            out.append(f"    {c.names[i]:<2} {_bar(pcs[i], vmax)} {pcs[i] * 100:4.1f}%")
        bpc = res.get("bass_pitch_classes")
        if bpc:
            top = sorted(range(12), key=lambda i: -bpc[i])[:3]
            out.append("  bass centres on: " + ", ".join(f"{c.names[i]} {bpc[i] * 100:.0f}%" for i in top))
    secs = res.get("key_sections") or []
    if secs:
        merged = []
        for s, e, t, m, sc in secs:
            if sc < 0.6:
                continue
            if merged and merged[-1][2:4] == [t, m]:
                merged[-1][1] = e
            else:
                merged.append([s, e, t, m])
        if len(merged) > 1:
            out.append("  key over time: " + " → ".join(
                f"{ts(s + c.t0)}–{ts(e + c.t0)} {music.key_name(t, m)}" for s, e, t, m in merged))
        elif full:
            out.append("  key over time: stable (no clear modulation)")
    return out


def _chord_stats(c: Ctx):
    segs = [s for s in c.res["chords"]["segments"] if s[2] is not None]
    dur = defaultdict(float)
    for s, e, lab in segs:
        dur[lab] += e - s
    total = sum(dur.values()) or 1
    return segs, sorted(dur.items(), key=lambda kv: -kv[1]), total


def sec_chords(c: Ctx, level: str) -> list[str]:
    segs, by_dur, total = _chord_stats(c)
    if not segs or (c.res.get("key_strength", 0) < WEAK_KEY and level != "full"):
        return ["CHORDS: no clear harmony detected."]
    grid = c.res["chords"]["grid"]
    key = f"; numerals in {c.label}" if c.tonic is not None else ""
    out = [f"CHORDS ({grid} grid{key})"]
    loop = music.find_loop([s[2] for s in segs])
    if loop:
        pat = loop["pattern"]
        out.append(f"  main progression (×{loop['repeats']}): {' – '.join(c.chord(l) for l in pat)}"
                   f"   ({' – '.join(c.numeral(l) for l in pat)})")
    top = by_dur[:6 if level != "short" else 4]
    out.append("  most used: " + ", ".join(f"{c.chord(l)} ({c.numeral(l)}) {d / total * 100:.0f}%" for l, d in top))
    if level == "short":
        return out
    limit = None if level == "full" else 28
    shown = segs if limit is None else segs[:limit]
    line = " · ".join(f"{ts(s + c.t0, True)} {c.chord(l)}" for s, e, l in shown)
    out.append(f"  timeline: {line}" + (f" · … (+{len(segs) - limit} more)" if limit and len(segs) > limit else ""))
    first, last = segs[0][2], segs[-1][2]
    out.append(f"  opens on {c.chord(first)}, ends on {c.chord(last)}")
    return out


def _nn(midi: int, spelled: dict) -> str:
    return f"{spelled.get(midi % 12) or theory.ROOT_NAMES[midi % 12][0]}{midi // 12 - 1}"


def _event_line(i: int, e: dict, c: Ctx) -> list[str]:
    d = e["id"]
    spelled = {}
    if d["kind"] == "chord":
        for name in d["tones"]:
            letter = theory.LETTERS.index(name[0])
            pc = (theory.NATURAL[letter] + name.count("#") - name.count("b")) % 12
            spelled[pc] = name
    notes = " ".join(_nn(m, spelled) for m in e["notes"])
    head = f"  #{i} {ts(e['start'] + c.t0, True)}–{ts(e['end'] + c.t0, True)}  {notes} →"
    if d["kind"] == "note":
        return [f"{head} single note{'' if 'doubled' not in d['desc'] else ' (' + d['desc'].split('single note ')[1] + ')'}"]
    if d["kind"] == "interval":
        line = f"{head} harmonic interval {d['name']}"
        return [line + (f" — {d['note']}" if d.get("note") else "")]
    if d["kind"] == "cluster":
        return [f"{head} cluster/non-tertian sonority (pitch classes "
                + " ".join(theory.ROOT_NAMES[p][0] for p in d["pcs"]) + ")"]
    pos = d["figure"] or (f"{d['bass_name']} in the bass" if d["inversion"] != 0 else "root position")
    line = f"{head} {d['symbol']}: {d['root_name']} {d['name']}, {pos}"
    if d.get("incomplete"):
        line += f" ({d['incomplete']})"
    if e.get("roman"):
        line += f" · {e['roman']} in {c.label}"
    lines = [line, f"       spelled {'–'.join(d['tones'])} · bass {_nn(d['bass'], spelled)[:-1]} · "
                   f"soprano {_nn(d['top'], spelled)} · above bass: {' '.join(dict.fromkeys(d['intervals_above_bass']))}"]
    if d["symmetric"]:
        lines.append("       (symmetrical chord: the root/spelling is only certain from context)")
    if d["alternatives"]:
        lines.append(f"       could also be spelled: {'; '.join(d['alternatives'])}")
    return lines


def sec_theory(c: Ctx, full: bool) -> list[str]:
    th = c.res.get("theory") or {}
    events = th.get("events") or []
    if not events:
        return ["THEORY: no distinct pitched events found."]
    out = ["THEORY (each sound event; notes low→high, as heard)"]
    limit = None if full else 12
    for i, e in enumerate(events[:limit] if limit else events, start=1):
        out += _event_line(i, e, c)
    if limit and len(events) > limit:
        out.append(f"  … +{len(events) - limit} more events (detail=full lists all)")
    if th.get("union"):
        u = th["union"]
        pos = u["figure"] or "root position"
        out.append(f"  all notes together (arpeggio/broken chord): {'–'.join(u['tones'])} → {u['symbol']}: "
                   f"{u['root_name']} {u['name']}, {pos} (lowest note {theory.note_name(u['bass'])})")
    mel = th.get("melody") or []
    if 2 <= len(mel) <= 24:
        steps = []
        for a, b in zip(mel, mel[1:]):
            arrow = "↑" if b > a else "↓"
            steps.append(f"{theory.note_name(a)}→{theory.note_name(b)} {arrow}{theory.interval_name(b - a).split(' ')[0]}")
        out.append("  melodic intervals: " + " · ".join(steps[:16]) + (" …" if len(steps) > 16 else ""))
    elif len(mel) > 24:
        out.append(f"  melody: {len(mel)} notes ({theory.note_name(min(mel))}–{theory.note_name(max(mel))}); "
                   "see NOTES/melody focus for the sequence")
    sc = th.get("scale")
    if sc and sc["fit"] >= 0.7:
        tonic = theory.ROOT_NAMES[sc["tonic"]][0]
        extra = ""
        if not sc["exact"]:
            bits = []
            if sc["extra"]:
                bits.append("extra " + ", ".join(theory.ROOT_NAMES[(sc['tonic'] + i) % 12][0] for i in sc["extra"]))
            if sc["missing"]:
                bits.append("missing " + ", ".join(theory.ROOT_NAMES[(sc['tonic'] + i) % 12][0] for i in sc["missing"]))
            extra = f" (closest match; {'; '.join(bits)})"
        out.append(f"  scale: {tonic} {sc['name']}, {sc['direction']} {theory.note_name(sc['low'])}–"
                   f"{theory.note_name(sc['high'])}{extra}")
    if th.get("cadence"):
        out.append(f"  cadence (in {c.label}): {th['cadence']}")
    return out


def sec_tempo(c: Ctx, full: bool) -> list[str]:
    t = c.res.get("tempo")
    if not t or t["confidence"] < 0.12:
        return ["TEMPO: no clear steady pulse (free time, speech, ambience or too short)."]
    bpm = t.get("bpm_from_beats") or t["bpm"]
    if abs(bpm - t["bpm"]) / t["bpm"] > 0.04:
        bpm = t["bpm"]
    conf = _conf_word(t["confidence"], 0.35, 0.2)
    alt = f"; could also be felt as {' or '.join(str(a) for a in t['alternates'][:2])}" if t.get("alternates") else ""
    line = f"TEMPO: {bpm:.0f} BPM (confidence {conf}{alt})"
    if "stability" in t:
        line += f" · {'steady' if t['stability'] < 0.06 else 'loose/rubato' if t['stability'] > 0.15 else 'mostly steady'}"
        if full:
            line += f" (beat jitter {t['stability'] * 100:.0f}%, {t['beats']} beats)"
    out = [line]
    if full and c.res.get("onset_rate") is not None:
        out.append(f"  rhythmic density: {c.res['onset_rate']:.1f} strong onsets/s")
    return out


def sec_notes(c: Ctx, full: bool) -> list[str]:
    notes = c.res.get("notes") or []
    if not notes:
        return ["NOTES: no clear pitched line detected."]
    poly = c.res.get("content", {}).get("label") == "music" and c.res.get("aperiodicity", 1) > 0.2
    head = "NOTES (dominant pitch line" + ("; approximate — several things sound at once)" if poly else ")")
    mids = [n["midi"] for n in notes]
    pcs = Counter()
    for n in notes:
        pcs[n["midi"] % 12] += n["dur"]
    lo, hi = min(mids), max(mids)
    out = [head,
           f"  range: {music.midi_to_name(lo, c.names)}–{music.midi_to_name(hi, c.names)} · "
           f"{len(notes)} notes · most time on: " + ", ".join(c.names[p] for p, _ in pcs.most_common(5))]
    limit = None if full else 40
    seq = notes if limit is None else notes[:limit]
    out.append("  sequence (note:seconds): " + " ".join(
        f"{music.midi_to_name(n['midi'], c.names)}:{n['dur']:.1f}" for n in seq)
        + (f" … (+{len(notes) - limit})" if limit and len(notes) > limit else ""))
    held = [n for n in notes if n["dur"] >= 0.15]
    if held:
        mean_abs = sum(abs(n["cents"]) * n["dur"] for n in held) / sum(n["dur"] for n in held)
        out.append(f"  intonation vs the recording's own tuning: average {mean_abs:.0f} cents off-centre")
    return out


def sec_tuner(c: Ctx) -> list[str]:
    notes = c.res.get("tuner_notes") or []
    tune = c.res.get("tuning") or {}
    out = ["TUNER (relative to A4 = 440 Hz)"]
    if not notes:
        out.append("  no sustained pitched notes found — play/sing single notes and hold each ~1 s.")
    groups: dict = {}
    for n in notes:
        g = groups.setdefault(n["midi"], {"dur": 0.0, "cents": 0.0, "hz": 0.0, "first": n["start"], "wob": 0.0})
        g["dur"] += n["dur"]
        g["cents"] += n["cents"] * n["dur"]
        g["hz"] += n["hz"] * n["dur"]
        g["wob"] += n["wobble"] * n["dur"]
    for m, g in sorted(groups.items(), key=lambda kv: kv[1]["first"])[:14]:
        cents = g["cents"] / g["dur"]
        verdict = "in tune" if abs(cents) < 5 else ("sharp" if cents > 0 else "flat")
        out.append(f"  {music.midi_to_name(m, music.PITCH_SHARP):<4} {g['hz'] / g['dur']:7.1f} Hz  {cents:+4.0f} cents  "
                   f"{verdict:<7} held {g['dur']:.1f}s  wobble ±{g['wob'] / g['dur']:.0f}c")
    if tune:
        out.append(f"  overall reference: A4 ≈ {tune['a4_hz']:.1f} Hz ({tune['cents']:+.0f} cents)")
    return out


def _balance_words(bands: dict, centroid: float) -> str:
    low = bands.get("sub", 0) + bands.get("bass", 0)
    high = bands.get("presence", 0) + bands.get("air", 0)
    words = []
    if low > 0.55:
        words.append("bass-heavy")
    elif low < 0.15:
        words.append("thin low end")
    if high > 0.12:
        words.append("bright/airy top")
    elif high < 0.02:
        words.append("dull/rolled-off top")
    words.append("dark" if centroid < 900 else "warm" if centroid < 1800 else "balanced" if centroid < 3000 else "bright")
    return ", ".join(words)


def sec_mix(c: Ctx, full: bool) -> list[str]:
    r = c.res
    L = r.get("loudness") or {}
    parts = []
    if L:
        parts.append(f"≈ {L['lufs']:.1f} LUFS integrated")
        if full and L.get("short_term_max") is not None:
            parts.append(f"short-term max {L['short_term_max']:.1f}")
        parts.append(f"loudness range {L['lra']:.0f} LU")
    parts.append(f"peak {r['peak_dbfs']:.1f} dBFS")
    if full:
        parts.append(f"RMS {r['rms_dbfs']:.1f} dBFS")
    out = ["MIX: " + " · ".join(parts)]
    if r["source"] == "input":
        out.append("  (microphone levels depend on mic gain and distance — compare relatively)")
    clip = r.get("clipped_ratio", 0)
    if clip > 0.0005 or full:
        out.append(f"  clipping: {'none' if clip < 1e-5 else f'{clip * 100:.2f}% of samples at full scale'}")
    sp = r["spectrum"]
    bands = sp["bands"]
    out.append("  tonal balance: " + " · ".join(f"{k} {v * 100:.0f}%" for k, v in bands.items())
               + f" → {_balance_words(bands, sp['centroid_hz'])} (centroid {sp['centroid_hz'] / 1000:.1f} kHz)")
    return out


def sec_speech(c: Ctx, full: bool, transcript: dict | None) -> list[str]:
    r = c.res
    f0 = r.get("f0_stats") or {}
    cues = [f"syllabic modulation {r.get('syllabic_modulation', 0):.2f} (speech typically > 0.3)",
            f"~{r.get('syllable_rate_hz', 0):.1f} syllable-pulses/s",
            f"{r.get('pause_rate', 0):.1f} short pauses/s"]
    if f0:
        cues.append(f"voice pitch median {f0['median_hz']:.0f} Hz (range {f0['p10_hz']:.0f}–{f0['p90_hz']:.0f})")
    out = ["SPEECH CUES: " + "; ".join(cues)]
    if transcript:
        if transcript.get("text"):
            text = transcript["text"].strip()
            if not full and len(text) > 1500:
                text = text[:1500] + " …[truncated; use detail=full]"
            lang = f" ({transcript['language']})" if transcript.get("language") else ""
            out.append(f"TRANSCRIPT{lang}: {text}")
        elif transcript.get("error"):
            out.append(f"TRANSCRIPT: unavailable — {transcript['error']}")
    return out


def sec_timeline(c: Ctx) -> list[str]:
    rows = c.res.get("timeline") or []
    if not rows:
        return []
    step = rows[0]["end"] - rows[0]["start"]
    out = [f"TIMELINE ({step:.0f} s windows: level · main chords)"]
    for row in rows:
        if row["active"] < 0.2:
            desc = "quiet/silence"
        else:
            desc = " ".join(c.chord(l) for l in row["chords"]) or "—"
        out.append(f"  {ts(row['start'] + c.t0)}–{ts(row['end'] + c.t0)}  {row['level_db']:6.1f} dB  {desc}")
    return out


# --- top level --------------------------------------------------------------------------------

def glance(c: Ctx) -> str:
    r = c.res
    bits = []
    if r.get("key_choice") and r["key_choice"]["score"] >= WEAK_KEY and r.get("duration", 0) >= 5:
        ce = r.get("centre") or {}
        bits.append(f"{c.label} ({ce['confidence'] if ce.get('override') else key_confidence(r)})")
    t = r.get("tempo")
    if t and t["confidence"] >= 0.12:
        bits.append(f"{t['bpm']:.0f} BPM")
    segs = [s for s in r["chords"]["segments"] if s[2] is not None]
    loop = music.find_loop([s[2] for s in segs]) if segs else None
    if loop:
        bits.append("loop " + "–".join(c.chord(l) for l in loop["pattern"]))
    if r.get("loudness"):
        bits.append(f"≈{r['loudness']['lufs']:.0f} LUFS".replace("-", "−"))
    return " · ".join(bits)


def render(res: dict, focus: str = "overview", detail: str = "standard", label: str | None = None,
           transcript: dict | None = None) -> str:
    focus = focus if focus in FOCI else "overview"
    src = label or SOURCE_LABEL.get(res.get("source"), res.get("source", ""))
    cap = res.get("captured_seconds", 0)
    status = res.get("status")
    if status == "too_short":
        return f"SOUNDSCAPE · {src} · only {cap:.1f} s captured — too short to analyse (need ≥ 1 s, ideally 15 s+)."
    if status == "silent":
        return (f"SOUNDSCAPE · {src} · {ts(cap)} captured · SILENCE — nothing above the noise floor was heard.\n"
                "If audio was playing: check the right source (output = what the Mac plays, input = microphone), "
                "volume, and permissions (run setup).")
    c = Ctx(res)
    tr = res.get("trimmed", [0, cap])
    lines = [f"SOUNDSCAPE · {src} · {ts(cap)} captured · sound {ts(res['active_seconds'])}"
             f" (analysed {ts(tr[0] + res['window'][0])}–{ts(tr[1] + res['window'][0])})"]
    ct = res["content"]
    lines.append(f"Content: {ct['label']} (confidence {ct['confidence']})")
    g = glance(c)
    if g:
        lines.append(f"At a glance: {g}")
    if res.get("duration", 0) < 8 and focus not in ("tuning", "melody", "theory"):
        lines.append("Note: short clip — key/tempo estimates firm up with 20 s or more of audio.")

    full = detail == "full"
    brief = detail == "brief"
    is_speech = ct["label"].startswith("speech")
    secs: list[list[str]] = []
    if focus == "key":
        secs += [sec_key(c, not brief)]
        if not brief:
            secs += [sec_chords(c, "short" if not full else "full"), sec_tempo(c, False)]
    elif focus == "chords":
        secs += [sec_chords(c, "full" if full else "standard" if not brief else "short")]
        if not brief:
            secs += [sec_key(c, False), sec_tempo(c, False)]
    elif focus == "tempo":
        secs += [sec_tempo(c, True)]
        if not brief:
            secs += [sec_key(c, False)]
    elif focus == "melody":
        secs += [sec_notes(c, full)]
        if not brief:
            secs += [sec_key(c, False), sec_tempo(c, False)]
    elif focus == "tuning":
        secs += [sec_tuner(c)]
        if not brief:
            secs += [sec_notes(c, False)]
    elif focus == "mix":
        secs += [sec_mix(c, True)]
        if not brief:
            secs += [sec_timeline(c)]
    elif focus == "speech":
        secs += [sec_speech(c, full, transcript)]
    elif focus == "theory":
        secs += [sec_theory(c, full)]
        n_chords = sum(e["id"]["kind"] == "chord" for e in (res.get("theory") or {}).get("events", []))
        if not brief and (n_chords >= 3 or res.get("duration", 0) >= 12):
            secs += [sec_key(c, False), sec_chords(c, "short")]
    elif focus == "all":
        secs += [sec_key(c, True), sec_chords(c, "full"), sec_theory(c, True), sec_tempo(c, True), sec_notes(c, True),
                 sec_tuner(c), sec_mix(c, True), sec_speech(c, True, transcript), sec_timeline(c)]
    else:  # overview
        if is_speech:
            secs += [sec_speech(c, full, transcript)]
        n_events = len((res.get("theory") or {}).get("events", []))
        if not is_speech and res.get("duration", 0) < 12 and 0 < n_events <= 8:
            secs += [sec_theory(c, False)]  # short clip: a chord, interval or scale question
        if not brief:
            if res.get("keys") and not (is_speech and ct["confidence"] == "high"):
                secs += [sec_key(c, False), sec_chords(c, "short"), sec_tempo(c, False)]
            if res.get("voiced_ratio", 0) > 0.5 and res.get("aperiodicity", 1) < 0.2:
                secs += [sec_notes(c, False)]
            secs += [sec_mix(c, False), sec_timeline(c)]
    for s in secs:
        if s:
            lines.extend(s)
    return "\n".join(lines)
