---
name: input
description: Listen to audio the user plays or performs — through the microphone/an input device AND the Mac's own audio output at the same time, so a track played on the Mac (a practice or ear-training page, a video) is heard even with the volume muted — and tell them what it hears: key, chords, tempo, notes being sung or played, tuning (tuner), or speech. Use when the user asks "what note am I singing?", "is my guitar in tune?", "what key is this?", "listen to me play", "listen to this".
argument-hint: "[question or device, e.g. tune my guitar]"
allowed-tools: [mcp__plugin_soundscape_soundscape__setup, mcp__plugin_soundscape_soundscape__start, mcp__plugin_soundscape_soundscape__wait, mcp__plugin_soundscape_soundscape__peek, mcp__plugin_soundscape_soundscape__stop, mcp__plugin_soundscape_soundscape__listen, mcp__plugin_soundscape_soundscape__analyze_file, mcp__plugin_soundscape_soundscape__sessions]
---

# Soundscape · listen to what the user plays or performs

The user wants you to listen. Their request: **$ARGUMENTS**
(If it's empty, listen and give a general read.)

You hear through the local `soundscape` MCP server. It captures audio on-device and hands you a
compact **text report** — never raw audio. An input session records the **microphone** (or chosen
input device) **and the Mac's own audio output** side by side. Playback on the Mac is therefore heard
directly even when the speakers are muted or turned down. The report's `NOTE:` line says which one it
analysed (it picks the source that actually carried the sound).

## Steps

1. **Set up.** Call `setup`.
   - Any `PROBLEM` line → relay it and stop.
   - Microphone and/or system audio permission `undetermined` → tell the user: *"First run: macOS will
     ask to let **Soundscape Capture** use the microphone and record system audio — click Allow."* Then continue.
   - Microphone `denied` → give the System Settings path from the setup output and stop.
     System audio `denied` → continue (mic only), but mention that audio played on a muted Mac won't be heard.
   - If the user named a device ("my Scarlett", "the USB mic", "BlackHole"), match it against the
     `input devices` list and pass its name as `device` below. Otherwise use the default input.
2. **Pick a focus**: tuning / "am I in tune" / "tune my guitar" → `tuning` · "what note(s)" / melody /
   singing → `melody` · key/scale → `key` · chords → `chords` · tempo → `tempo` · levels/tone → `mix` ·
   speech / "what did they say" → `speech` · otherwise `overview`.
3. **Start.** `start` with `source: "input"` (plus `device` if chosen). Keep the default
   `include_system_audio: true`; set it to `false` only if the user wants the microphone alone.
4. **Cue the user** in one line, tailored to the task:
   - tuning → *"🎤 Listening — play each string (or note) one at a time and let it ring ~2 s."*
   - singing / notes → *"🎤 Listening — sing or play now; pause for a few seconds when you're done."*
   - otherwise → *"🎧 Listening — play it on your Mac (muted is fine) or into the mic."*
5. **Stay listening.** `wait` with `until: "sound_then_silence"`, `max_seconds: 120`
   (for tuning use `silence_seconds: 5` so gaps between strings don't end it early).
   - *audio stopped* → step 6. *still going at timeout* → `wait` again (cap ~10 minutes) or step 6 if
     there is plenty of material.
   - *only background noise* → nothing played on the Mac and nothing loud reached the mic: say so
     (mic muted? wrong device? too far away?) and offer to retry.
6. **Analyse.** `stop` with the focus and `detail: "standard"`.
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

## Notes

- Live loop: for continuous feedback (e.g., tuning string by string), you may `peek` with
  `focus: "tuning"`, `last_seconds: 4` between `wait(until: "sound")` calls, then `stop` at the end.
- Re-analyse the same capture with another focus via `analyze_file` on the WAV path from `stop`.
- Recordings stay on this Mac (`~/.soundscape/sessions`, auto-deleted after 7 days).
