---
name: output
description: Listen to what the Mac is playing (system audio output from any app — music, video, calls, a DAW) and tell the user what's in it — key, chords, tempo, melody, tuning, loudness/mix, or what's being said. Use when the user asks "what key is this song in?", "what are the chords?", "what BPM is this?", "listen to what I'm playing", "how loud is my master?".
argument-hint: "[question, e.g. what key is this in?]"
allowed-tools: [mcp__plugin_soundscape_soundscape__setup, mcp__plugin_soundscape_soundscape__start, mcp__plugin_soundscape_soundscape__wait, mcp__plugin_soundscape_soundscape__peek, mcp__plugin_soundscape_soundscape__stop, mcp__plugin_soundscape_soundscape__listen, mcp__plugin_soundscape_soundscape__analyze_file, mcp__plugin_soundscape_soundscape__sessions]
---

# Soundscape · listen to the Mac's audio output

The user wants you to listen to what their Mac is playing. Their question: **$ARGUMENTS**
(If it's empty, give a general read of what's playing.)

You hear through the local `soundscape` MCP server. It captures audio on-device and hands you a
compact **text report** — never raw audio. Don't ask for screen recordings, uploads or files.

## Steps

1. **Set up.** Call `setup`.
   - Any `PROBLEM` line → relay it in plain words and stop.
   - System audio permission `undetermined` → tell the user: *"First run: macOS will ask to let
     **Soundscape Capture** record system audio — click Allow."* Then continue (step 3 triggers the prompt).
   - `denied` → give the user the System Settings path from the setup output and stop.
2. **Pick a focus** from the question: key/scale → `key` · chords/progression → `chords` ·
   BPM/tempo → `tempo` · melody/riff/notes → `melody` · in tune? → `tuning` · loudness/EQ/mix → `mix` ·
   what's being said / transcribe → `speech` · anything else → `overview`.
3. **Start.** `start` with `source: "output"`.
4. **Cue the user** in one short line, e.g. *"🎧 Listening to your Mac — play it now. I'll analyse
   when it stops (or just tell me to stop)."*
5. **Stay listening.** `wait` with `until: "sound_then_silence"`, `max_seconds: 120`.
   - *audio stopped* → step 6.
   - *timed out while still playing* → for key/chords/tempo, 45 s+ of sound is plenty: go to step 6.
     Otherwise `wait` again (give up after ~10 minutes total).
   - *no sound at all* → tell the user nothing is coming through (is it playing? unmuted?) and offer to
     keep listening. If the result mentions digital silence + permission, relay that fix.
6. **Analyse.** `stop` with the chosen `focus` and `detail: "standard"` (use `"full"` only if they want
   the whole chord/note timeline).
7. **Answer.**
   - Lead with the direct answer in one line: *"It's in **A minor**."*
   - Back it with 2–4 bullets from the report in musician-friendly terms: the progression with Roman
     numerals, where it resolves, tempo, notable chromatic notes.
   - Be honest about confidence. If the report says medium/low, or notes a relative-key ambiguity, say so
     and name the alternative (*"could also be heard as C major, the relative major — but it keeps landing on Am"*).
   - Mention tuning only if it's notably off 440 Hz (that matters for playing along).
   - Don't paste the raw report unless asked; mention the saved report path once.

## Notes for interpreting reports

- Relative major/minor keys share the same notes; the tonic is where the music resolves (bass,
  opening/closing chord). The report's `note:` line explains which way it leaned and why.
- A `mode hint` other than plain major/minor (Dorian, Mixolydian…) is a hint, not a verdict.
- `key over time` with several keys = modulation, or several songs played in one capture.
- Tempo alternates (×2, ×½) are the same pulse felt differently; pick what fits the genre.
- Re-analysing the same capture with another focus: call `analyze_file` with the WAV path printed by `stop`.
- Recordings stay on this Mac in `~/.soundscape/sessions` and auto-delete after 7 days;
  `sessions` with `purge_recordings: true` deletes them now.
