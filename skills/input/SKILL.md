---
name: input
description: Listen to audio the user plays or performs — through the microphone/an input device AND the Mac's own audio output at the same time, so a track played on the Mac (a practice or ear-training page, a video) is heard even with the volume muted — and tell them what it hears: key, chords, tempo, notes being sung or played, tuning (tuner), or speech. Also answers music-theory / ear-training questions (chord quality and inversion, intervals, scales, cadences). Use when the user asks "what kind of chord is this?", "what interval is that?", "what note am I singing?", "is my guitar in tune?", "what key is this?", "listen to me play", "listen to this".
argument-hint: "[question or device, e.g. tune my guitar]"
---

# Soundscape · listen to what the user plays or performs

The user wants you to listen. Their request: **$ARGUMENTS**
(If empty or init/on, enable continuous listening and return.)

You hear through the local `soundscape` MCP server. It captures audio on-device and hands you a
compact **text report**, with optional replay files and practice-question images. An input session records the **microphone** (or chosen
input device) **and the Mac's own audio output** side by side. Playback on the Mac is therefore heard
directly even when the speakers are muted or turned down. The report's `NOTE:` line says which one it
analysed (it picks the source that actually carried the sound).

## Steps

For off/stop, call `stop(analyze=false)` immediately and return; do not run setup or start.

1. **Set up.** Call `setup`.
   - Any `PROBLEM` line → relay it and stop.
   - Microphone and/or system audio permission `undetermined` → tell the user: *"First run: macOS will
     ask to let **Soundscape Capture** use the microphone and record system audio — click Allow."* Then continue.
   - Microphone `denied` → give the System Settings path from the setup output and stop.
     System audio `denied` → continue (mic only), but mention that audio played on a muted Mac won't be heard.
   - If the user named a device ("my Scarlett", "the USB mic", "BlackHole"), match it against the
     `input devices` list and pass its name as `device` below. Otherwise use the default input.
2. **Pick a focus**: what kind of chord / interval / scale / cadence, inversion, figured bass, Roman numerals,
   "spell this chord" → `theory` · tuning / "am I in tune" / "tune my guitar" → `tuning` · "what note(s)" / melody /
   singing → `melody` · key/scale → `key` · chords → `chords` · tempo → `tempo` · levels/tone → `mix` ·
   speech / "what did they say" → `speech` · otherwise `overview`.
3. **Start or reuse continuous capture.** `start` with `source: "input"`, `continuous: true`.
   Input also captures desktop audio by default. With AirPods, prefer the built-in Mac microphone
   from the device list unless the user chose another input.
4. **Cue the user:** "Listening is ON — play the clip, then ask your question." If this was just
   init/on, return immediately. Capture continues between turns.
5. **Read recent audio.** For a question use `peek` with `last_seconds: 30`, the selected focus and
   `detail: "standard"`. For a future clip, a bounded `wait` may precede `peek`.
6. **Keep capture ON.** Never stop after silence or an answer. For off/stop, call
   `stop(analyze=false)` immediately, skipping setup and waiting. For multiple-choice images,
   replay, incremental reports, or transcription follow [Soundscape](../soundscape/SKILL.md).
7. **Answer.**
   - Tuning: list each note heard with its offset, e.g. *"E2 −8 c (flat — tighten slightly), A2 +2 c ✓…"*.
     Within ±5 cents is in tune. Note the `wobble` value: high wobble means an unsteady note, not a
     tuning problem. Ask them to replay any string that wasn't detected.
   - Notes/melody: give the note names in order (and the range); suggest the likely key if there are
     enough notes.
   - Key/chords/tempo: answer directly first, then 2–4 bullets of evidence; be honest about confidence.
   - If the analysis came from the microphone, room noise and reverb may lower accuracy — say so when
     confidence is not high. If it came from the Mac's own output, no caveat is needed.
   - Don't paste the raw report unless asked.

## Music-theory questions (`theory` focus)

Typical for ear training or AP Music Theory practice: *"what kind of chord is this?"*, *"what interval?"*,
*"which cadence?"*, *"what inversion?"*. The sound is often only one or two seconds long, so use
`wait` with `silence_seconds: 2.5` (the default `min_sound_seconds: 1` catches even a short chord).
The `THEORY` section lists every sound event: its notes from low to high (with octaves), and for each one:

- **Chords:** root and quality in standard terms. Triads: major / minor / diminished / augmented.
  Sevenths: MM7, Mm7 (dominant), mm7, ø7 (dm7, half-diminished), °7 (dd7, fully diminished), mM7, +M7.
  Also sus, add6 and 9ths. Each comes with its inversion, figured bass (5/3, 6, 6/4 · 7, 6/5, 4/3, 4/2),
  spelled tones, bass and soprano. With 2+ chords and a clear key it adds a Roman numeral
  (e.g. `V65 in C major`) and names the **cadence** (PAC / IAC / HC / plagal / deceptive / Phrygian HC).
- **Intervals:** harmonic (sounding together) or melodic (↑/↓ between notes), e.g. `↑M6`, `P5`, `A4/d5`.
- **Scales:** tonic, type (major, the minors, the modes, pentatonic, blues, whole-tone, octatonic) and direction.

Answer the way a theory teacher would: *"That's a **half-diminished seventh chord (ø7)** on G#, in
**first inversion (6/5)** — spelled G#–B–D–F# with B in the bass."* Mention the report's alternatives
when they matter. Some are enharmonic (Bm6 = G#ø7/B). Some are symmetrical: a °7 or + chord's root depends
on spelling and context. If the user gives the key, use the spelled chord to state the Roman numeral and
function (e.g. vii°7 or ii°65).

## Notes

- Live loop: for continuous feedback (e.g., tuning string by string), you may `peek` with
  `focus: "tuning"`, `last_seconds: 4` between `wait(until: "sound")` calls, then `stop` at the end.
- Re-analyse the same capture with another focus via `analyze_file` on the WAV path from `stop`.
- Recordings stay on this Mac (`~/.soundscape/sessions`, auto-deleted after 7 days).
