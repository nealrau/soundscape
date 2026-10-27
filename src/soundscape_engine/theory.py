"""Ear-training level reading: which notes sound in each event, what chord / interval / scale /
cadence they form, spelled and named the way a music-theory class (e.g. AP Music Theory) does."""

from __future__ import annotations

import numpy as np

from .dsp import EPS

LETTERS = "CDEFGAB"
NATURAL = [0, 2, 4, 5, 7, 9, 11]
ACC = {-2: "bb", -1: "b", 0: "", 1: "#", 2: "##"}
ROOT_NAMES = {0: ["C"], 1: ["C#", "Db"], 2: ["D"], 3: ["Eb", "D#"], 4: ["E"], 5: ["F"], 6: ["F#", "Gb"],
              7: ["G"], 8: ["Ab", "G#"], 9: ["A"], 10: ["Bb", "A#"], 11: ["B"]}

TRIAD_FIGS = ["5/3 (root position)", "6 (1st inversion)", "6/4 (2nd inversion)"]
SEVENTH_FIGS = ["7 (root position)", "6/5 (1st inversion)", "4/3 (2nd inversion)", "4/2 (3rd inversion)"]

# (tones as (semitones, degree), name, symbol, roman quality mark, priority: lower = preferred reading)
CHORD_TYPES = [
    (((0, 1), (4, 3), (7, 5)), "major triad", "", "", 0),
    (((0, 1), (3, 3), (7, 5)), "minor triad", "m", "", 0),
    (((0, 1), (3, 3), (6, 5)), "diminished triad", "°", "°", 0),
    (((0, 1), (4, 3), (8, 5)), "augmented triad", "+", "+", 0.2),
    (((0, 1), (4, 3), (7, 5), (10, 7)), "dominant seventh (Mm7, major-minor 7th)", "7", "", 1),
    (((0, 1), (4, 3), (7, 5), (11, 7)), "major seventh (MM7)", "maj7", "", 1),
    (((0, 1), (3, 3), (7, 5), (10, 7)), "minor seventh (mm7)", "m7", "", 1),
    (((0, 1), (3, 3), (6, 5), (10, 7)), "half-diminished seventh (ø7, dm7)", "ø7", "ø", 1),
    (((0, 1), (3, 3), (6, 5), (9, 7)), "fully diminished seventh (°7, dd7)", "°7", "°", 1),
    (((0, 1), (3, 3), (7, 5), (11, 7)), "minor-major seventh (mM7)", "m(maj7)", "", 1.5),
    (((0, 1), (4, 3), (8, 5), (11, 7)), "augmented major seventh (+M7)", "+maj7", "+", 1.5),
    (((0, 1), (4, 3), (8, 5), (10, 7)), "augmented (dominant) seventh (+7)", "+7", "+", 1.6),
    (((0, 1), (4, 3), (6, 5), (10, 7)), "dominant seventh flat five (7b5; same sound as a French +6)", "7b5", "", 2),
    (((0, 1), (5, 4), (7, 5)), "suspended fourth (sus4)", "sus4", "", 2),
    (((0, 1), (2, 2), (7, 5)), "suspended second (sus2)", "sus2", "", 2.2),
    (((0, 1), (5, 4), (7, 5), (10, 7)), "dominant seventh sus4 (7sus4)", "7sus4", "", 2.2),
    (((0, 1), (4, 3), (7, 5), (9, 6)), "major sixth (added 6th)", "6", "", 2.5),
    (((0, 1), (3, 3), (7, 5), (9, 6)), "minor sixth (added 6th)", "m6", "", 2.5),
    (((0, 1), (4, 3), (7, 5), (2, 9)), "major add9", "add9", "", 2.5),
    (((0, 1), (4, 3), (7, 5), (10, 7), (2, 9)), "dominant ninth", "9", "", 3),
    (((0, 1), (4, 3), (7, 5), (11, 7), (2, 9)), "major ninth", "maj9", "", 3),
    (((0, 1), (3, 3), (7, 5), (10, 7), (2, 9)), "minor ninth", "m9", "", 3),
]

INTERVALS = ["P1 (unison)", "m2 (minor 2nd)", "M2 (major 2nd)", "m3 (minor 3rd)", "M3 (major 3rd)",
             "P4 (perfect 4th)", "A4/d5 (tritone)", "P5 (perfect 5th)", "m6 (minor 6th)", "M6 (major 6th)",
             "m7 (minor 7th)", "M7 (major 7th)", "P8 (octave)"]
COMPOUND = {13: "m9", 14: "M9", 15: "m10", 16: "M10", 17: "P11", 18: "A11", 19: "P12", 20: "m13",
            21: "M13", 22: "m14", 23: "M14", 24: "P15 (two octaves)"}

SCALES = [
    ("major (Ionian)", [0, 2, 4, 5, 7, 9, 11]),
    ("natural minor (Aeolian)", [0, 2, 3, 5, 7, 8, 10]),
    ("harmonic minor", [0, 2, 3, 5, 7, 8, 11]),
    ("melodic minor (ascending form)", [0, 2, 3, 5, 7, 9, 11]),
    ("melodic minor (raised 6-7 up, natural down)", [0, 2, 3, 5, 7, 8, 9, 10, 11]),
    ("Dorian", [0, 2, 3, 5, 7, 9, 10]),
    ("Phrygian", [0, 1, 3, 5, 7, 8, 10]),
    ("Lydian", [0, 2, 4, 6, 7, 9, 11]),
    ("Mixolydian", [0, 2, 4, 5, 7, 9, 10]),
    ("Locrian", [0, 1, 3, 5, 6, 8, 10]),
    ("major pentatonic", [0, 2, 4, 7, 9]),
    ("minor pentatonic", [0, 3, 5, 7, 10]),
    ("blues", [0, 3, 5, 6, 7, 10]),
    ("whole-tone", [0, 2, 4, 6, 8, 10]),
    ("octatonic (half-whole)", [0, 1, 3, 4, 6, 7, 9, 10]),
    ("octatonic (whole-half)", [0, 2, 3, 5, 6, 8, 9, 11]),
    ("chromatic", list(range(12))),
]

# Expected strength of harmonic h of a note, relative to its fundamental, for "explaining away"
# overtones (conservative: real chord tones are louder than these).
HARMONIC_OFFSETS = [(12, 0.9), (19, 0.6), (24, 0.45), (28, 0.35), (31, 0.3), (34, 0.3), (36, 0.2), (38, 0.15), (40, 0.15)]
MIDI_LO, MIDI_HI = 24, 108


# --- naming helpers -----------------------------------------------------------------------------

def _spell(root_name: str, semis: int, degree: int) -> str | None:
    letter = LETTERS.index(root_name[0])
    root_pc = (NATURAL[letter] + {"#": 1, "b": -1, "": 0}.get(root_name[1:2], 0)) % 12
    target_letter = (letter + degree - 1) % 7
    diff = ((root_pc + semis) - NATURAL[target_letter]) % 12
    diff = diff - 12 if diff > 6 else diff
    return LETTERS[target_letter] + ACC[diff] if diff in ACC else None


def spell_chord(root_pc: int, tones) -> tuple[str, list[str]]:
    """Spell chord tones by stacking letters from the root (G#°7 = G#-B-D-F, not Ab)."""
    best = None
    for name in ROOT_NAMES[root_pc]:
        spelled = [_spell(name, s, d) for s, d in tones]
        if None in spelled:
            continue
        cost = sum(len(x) - 1 for x in spelled) + sum(x.endswith(("##", "bb")) * 3 for x in spelled)
        if best is None or cost < best[0]:
            best = (cost, name, spelled)
    if best is None:
        return ROOT_NAMES[root_pc][0], [ROOT_NAMES[(root_pc + s) % 12][0] for s, _ in tones]
    return best[1], best[2]


def note_name(midi: int, names=None) -> str:
    names = names or [ROOT_NAMES[i][0] for i in range(12)]
    return f"{names[midi % 12]}{midi // 12 - 1}"


def interval_name(semis: int) -> str:
    semis = abs(semis)
    if semis <= 12:
        return INTERVALS[semis]
    return COMPOUND.get(semis, f"{semis} semitones") + f" (compound {INTERVALS[semis % 12].split(' ')[0]})"


# --- multi-pitch per event ----------------------------------------------------------------------

def event_notes(P_fi, P_f, P_a, frame_times, start: float, end: float, ref_hz: float):
    """Which notes (MIDI numbers) sound between start and end, with their strengths."""
    sel_frames = np.nonzero((frame_times >= start + 0.06) & (frame_times <= end))[0]
    if len(sel_frames) == 0:
        sel_frames = np.array([int(np.argmin(np.abs(frame_times - (start + end) / 2)))])
    lo, hi = sel_frames.min(), sel_frames.max()
    m = (P_fi >= lo) & (P_fi <= hi)
    f, a = P_f[m], P_a[m]
    if len(f) == 0:
        return []
    midi = 69 + 12 * np.log2(f / ref_hz)
    n = np.round(midi).astype(int)
    w = a * np.exp(-0.5 * ((midi - n) / 0.25) ** 2)
    ok = (n >= MIDI_LO) & (n < MIDI_HI)
    sal = np.zeros(MIDI_HI)
    np.add.at(sal, n[ok], w[ok])
    sal /= len(sel_frames)
    peak = sal.max()
    if peak <= EPS:
        return []
    explained = np.zeros_like(sal)
    notes = []
    for k in range(MIDI_LO, MIDI_HI):
        v = sal[k]
        if v < 0.07 * peak or v <= 1.25 * explained[k] + 0.03 * peak:
            continue
        notes.append((k, float(v / peak)))
        for off, r in HARMONIC_OFFSETS:
            if k + off < MIDI_HI:
                explained[k + off] += v * r
    return notes


# --- chord identification -----------------------------------------------------------------------

def identify(notes: list[int]) -> dict:
    """Name the sonority formed by these MIDI notes (lowest = bass)."""
    if not notes:
        return {"kind": "none"}
    notes = sorted(set(notes))
    bass, top = notes[0], notes[-1]
    pcs = sorted({n % 12 for n in notes})
    if len(pcs) == 1:
        span = top - bass
        return {"kind": "note", "pc": pcs[0], "bass": bass, "top": top,
                "desc": "single note" + (f" doubled across {span // 12 + 1} octaves" if span >= 12 else "")}
    if len(pcs) == 2:
        upper = min(n for n in notes if n % 12 != bass % 12)
        semis = upper - bass
        out = {"kind": "interval", "semis": semis, "bass": bass, "top": top, "name": interval_name(semis)}
        if semis % 12 == 7:
            out["note"] = "open fifth / power chord: no third, so neither major nor minor"
        return out
    readings = []
    for root in pcs:
        rel = {(p - root) % 12 for p in pcs}
        for tones, name, sym, mark, prio in CHORD_TYPES:
            ivs = {s for s, _ in tones}
            if rel == ivs:
                readings.append((prio, root != bass % 12, root, tones, name, sym, mark, None))
            elif rel < ivs and ivs - rel == {7} and len(rel) >= 3:
                readings.append((prio + 0.8, root != bass % 12, root, [t for t in tones if t[0] != 7],
                                 name, sym, mark, "5th omitted"))
    if not readings:
        return {"kind": "cluster", "pcs": pcs, "bass": bass, "top": top}
    readings.sort(key=lambda r: (r[0], r[1]))
    prio, _, root, tones, name, sym, mark, note = readings[0]
    root_name, spelled = spell_chord(root, tones)
    bass_semis = (bass - root) % 12
    position = [i for i, (s, _) in enumerate(sorted(tones, key=lambda t: t[1])) if s == bass_semis]
    seventh = any(d == 7 for _, d in tones)
    degree_order = sorted(tones, key=lambda t: t[1])
    if bass_semis == 0:
        inv = 0
    else:
        deg = next(d for s, d in tones if s == bass_semis)
        inv = {3: 1, 5: 2, 7: 3}.get(deg)
    if seventh and len(degree_order) >= 3:
        figure = SEVENTH_FIGS[inv] if inv is not None else None
    elif len(tones) == 3 and name.endswith("triad"):
        figure = TRIAD_FIGS[inv] if inv is not None and inv < 3 else None
    else:
        figure = None
    bass_name = next(sp for (s, _), sp in zip(tones, spelled) if s == bass_semis) if position else None
    alts = []
    for r in readings[1:]:
        if (r[2], r[4]) != (root, name):
            rn, _ = spell_chord(r[2], r[3])
            label = f"{rn}{r[5]} ({r[4]})"
            if label not in alts:
                alts.append(label)
    return {"kind": "chord", "root": root, "root_name": root_name, "name": name, "symbol": root_name + sym,
            "mark": mark, "seventh": seventh, "tones": spelled, "inversion": inv, "figure": figure,
            "bass": bass, "top": top, "bass_name": bass_name, "incomplete": note,
            "symmetric": name.startswith(("fully diminished", "augmented triad")),
            "intervals_above_bass": [INTERVALS[(n - bass) % 12].split(" ")[0] for n in notes[1:]
                                     if (n - bass) % 12],
            "alternatives": alts[:2]}


def roman_figured(ch: dict, tonic: int, minor: bool, modal: bool) -> str:
    deg = (ch["root"] - tonic) % 12
    major_map = ["I", "bII", "II", "bIII", "III", "IV", "#IV", "V", "bVI", "VI", "bVII", "VII"]
    minor_map = ["I", "bII", "II", "III", "#III", "IV", "#IV", "V", "VI", "#VI", "VII", "#VII"]
    num = (minor_map if minor and not modal else major_map)[deg]
    acc, core = "".join(c for c in num if c in "b#"), num.lstrip("b#")
    if ch["name"].startswith(("minor", "diminished", "half-diminished", "fully diminished")):
        core = core.lower()
    fig = {None: "", 0: "", 1: "6", 2: "64"}
    if ch["seventh"]:
        fig = {None: "7", 0: "7", 1: "65", 2: "43", 3: "42"}
    figure = fig.get(ch["inversion"], "")
    if ch["seventh"] and ch["inversion"] is None:
        figure = "7"
    if ch["name"].startswith("major seventh"):
        figure = figure.replace("7", "M7") if figure == "7" else figure
    return f"{acc}{core}{ch['mark']}{figure}"


# --- melodies, scales, cadences -----------------------------------------------------------------

def scale_match(midis: list[int]) -> dict | None:
    if len(midis) < 5:
        return None
    pcs = {m % 12 for m in midis}
    best = None
    for tonic in dict.fromkeys([midis[0] % 12, min(midis) % 12]):
        rel = {(p - tonic) % 12 for p in pcs}
        for name, ivs in SCALES:
            s = set(ivs)
            score = len(rel & s) / len(rel | s)
            if best is None or score > best[0] + 1e-9:
                best = (score, tonic, name, sorted(rel - s), sorted(s - rel))
    score, tonic, name, extra, missing = best
    direction = "ascending" if midis[-1] > midis[0] else "descending" if midis[-1] < midis[0] else "mixed"
    return {"tonic": tonic, "name": name, "exact": score == 1.0, "fit": score, "direction": direction,
            "extra": extra, "missing": missing, "low": min(midis), "high": max(midis)}


def cadence(chords: list[dict], tonic: int, minor: bool) -> str | None:
    if len(chords) < 2:
        return None
    a, b = chords[-2], chords[-1]
    ra, rb = (a["root"] - tonic) % 12, (b["root"] - tonic) % 12
    rooted = lambda c: c["inversion"] == 0  # noqa: E731
    if rb == 0 and ra in (7, 11):
        if ra == 7 and rooted(a) and rooted(b) and b["top"] % 12 == tonic:
            return "perfect authentic cadence (PAC): V(7)→I, both in root position, tonic in the soprano"
        return "imperfect authentic cadence (IAC): dominant→tonic, but an inversion, vii°, or soprano not on the tonic"
    if rb == 0 and ra == 5:
        return "plagal cadence (IV→I, 'Amen')"
    if ra == 7 and rb in (8, 9):
        return "deceptive cadence (V→vi / V→VI)"
    if rb == 7:
        if minor and ra == 5 and a.get("inversion") == 1:
            return "Phrygian half cadence (iv6→V)"
        return "half cadence (ends on V)"
    return None
