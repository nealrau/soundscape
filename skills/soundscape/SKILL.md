---
name: soundscape
description: Turn Soundscape listening on or off, keep listening to desktop audio through AirPods between questions, transcribe speech or musical notes, identify chord progressions, create AP-style multiple-choice practice images, and replay captured audio. Use for Soundscape init, input on/off, output on/off, and follow-up questions while Soundscape is on.
argument-hint: "input on | input off | identify the chord progression"
---

# Soundscape for Codex

Use the tools from the local `soundscape` MCP server. Tool prefixes vary by host; discover the
installed Soundscape tools rather than assuming a Claude-specific prefix. Never launch a one-shot
shell `start`: shell exit closes capture. The MCP connection owns the listening session.

## Init / input on / output on

1. Call `setup`. Explain any permission prompt; macOS requires the user's Allow action.
2. For input, choose the built-in Mac microphone from setup's actual device list when the user is
   listening through AirPods, unless they explicitly selected another input. This keeps the AirPods
   available for normal playback. The system-output tap hears desktop audio regardless of speakers,
   headphones, or mute. Do not assume headphones are connected; setup names the current output.
3. Call `start(source="input", continuous=true, include_system_audio=true, device=chosen_device)`.
   For desktop-only listening, use `source="output"`. If microphone permission is denied and the
   user only needs desktop audio, use output and explain the unavailable microphone.
4. Say listening is ON and return control immediately so the user can play audio and ask questions.
   Capture stays active between turns. Silence and answering a question must NOT stop it.

## Questions while ON

Use `sessions` to find the session if needed. Use `peek(last_seconds=30, focus=..., detail="full")`
to analyse the recent window without stopping. Increase the window up to 600 seconds when needed.
For desktop clips choose `source="output"`; for singing/tuning choose `source="input"`.
`stream` returns successive bounded reports with a cursor. Reports are pulled by tool calls;
there are no unsolicited chat messages or automatic reasoning while Codex is idle. Do not run an
endless wait loop. If waiting for an upcoming clip, use a bounded `wait`, then `peek`, never `stop`.

- Progression: `focus="chords"`; explain chord symbols, key, and Roman numerals.
- AP-style theory, intervals, inversions, cadences: `focus="theory"` or `"all"`.
- Musical transcription: `focus="melody"` plus `"theory"`; report estimated notes/timing, not a
  guaranteed polyphonic score. Dense recordings can be ambiguous; preserve confidence/alternatives.
- Spoken transcription: `focus="speech"`; first `setup(install_transcription=true)` if missing.
  The speech model downloads on first use. Never present inferred speech as an actual transcript.
- Multiple-choice screenshot: analyse first, then call `practice_card(question, choices, answer,
  explanation)` with 4 plausible options. The answer is a zero-based index. Keep the answer hidden
  unless requested; `reveal_answer=true` includes it. Render the returned PNG inline. These are
  original practice questions, not official College Board material.
- For an AP Classroom screenshot, read the question and all visible choices first. Use the
  question's supplied key/tonic and distinguish uppercase/lowercase numerals, accidentals,
  seventh chords and inversions. For intervals, distinguish melodic/harmonic, direction and
  compound intervals. Analyse the played excerpt, compare every choice, then give the best
  matching letter and exact option text with concise auditory evidence. Do not manufacture a
  replacement practice question when the user wants an existing question answered. If no
  option fits or confidence is low, say so and request a replay rather than forcing a match.
  Soundscape captures audio only, not the desktop screen; use the supplied screenshot or a
  host screen/browser tool when the user has asked you to inspect the question on screen.
- "Play it": `replay(last_seconds=..., source="output")` plays the captured excerpt through the
  current Mac output. For an inline audio player use `play=false`, then embed the returned WAV.
  Snapshot/analyse before replay; playback can enter the system tap and must not be mistaken for
  fresh evidence. Replay reproduces the original recording, not a synthesized transcription.

## Off / stop

Call `stop(analyze=false)` immediately for the relevant session (or each active session if the
user says all listening off). Confirm OFF. A client disconnect also stops its capture processes.
Continuous sessions retain a rolling last 10–15 minutes locally; peeks are capped at 10 minutes.
Saved replay files and images follow the normal 7-day cleanup. Purge only when the user asks.

## Compatibility

Accept natural-language "Soundscape input on/off" and `$soundscape input on/off` in Codex.
Slash-menu spelling depends on the client; do not promise Claude's `/soundscape:input` syntax
exists in Codex. In Claude, the existing input/output commands remain available.
