"""MCP tool definitions and handlers."""

from __future__ import annotations

import os
import tempfile
import subprocess
import threading
import shutil
import wave
import time
from pathlib import Path

from . import environment as env
from .sessions import REGISTRY, Session

FOCI = ["overview", "key", "chords", "tempo", "melody", "tuning", "mix", "speech", "theory", "all"]
FOCUS = {"type": "string", "enum": FOCI, "default": "overview",
         "description": "What to analyse in depth. overview=everything briefly; key=key/mode/tonal centre; "
                        "theory=ear-training read of each sound (chord quality + inversion/figured bass, intervals, "
                        "scales, cadences); "
                        "chords=progression with Roman numerals; tempo=BPM/pulse; melody=note sequence; "
                        "tuning=tuner (Hz/cents per held note); mix=loudness/tonal balance; "
                        "speech=speech cues + transcript; all=every section."}
DETAIL = {"type": "string", "enum": ["brief", "standard", "full"], "default": "standard",
          "description": "Report length. brief ~10 lines, standard ~30, full = everything (long)."}
INCLUDE_SYS = {"type": "boolean", "default": True,
               "description": "source=input only: also capture the Mac's own audio output, so audio played on the "
                              "Mac is heard even with the volume muted; the analysis uses whichever carried the sound."}
SESSION_ID = {"type": "string", "description": "Session id from start. Omit to use the most recent session."}

PERMISSION_HELP = {
    "system_audio": "System Settings › Privacy & Security › Screen & System Audio Recording › "
                    "'System Audio Recording Only' › enable Soundscape Capture (or remove it with − and run setup again).",
    "microphone": "System Settings › Privacy & Security › Microphone › enable Soundscape Capture.",
}


class ToolError(Exception):
    pass


# --- helpers ---------------------------------------------------------------------------------

def _ready_or_raise(ctx):
    problems = env.ensure_ready(progress=ctx.progress)
    if problems:
        raise ToolError("Soundscape is not set up:\n- " + "\n- ".join(problems))


def _label(s: Session) -> str:
    if s.source == "output":
        return "system audio output"
    return f"input: {s.info.get('device') or s.requested_device or 'default input'}"


def _analyze_session(s: Session, focus: str, detail: str, start: float = 0.0, save: bool = True) -> str:
    if s.max_seconds == 0 or s.running:
        with tempfile.TemporaryDirectory(prefix="soundscape-") as tmp:
            snapshot = Path(tmp) / "snapshot.wav"
            try:
                begin, end = s.snapshot(snapshot, last_seconds=60 if start <= 0 else 600,
                                        start=start if start > 0 else None)
            except (ValueError, OSError) as exc:
                raise ToolError(str(exc)) from exc
            args = ["analyze", str(snapshot), "--source", s.source, "--focus", focus,
                    "--detail", detail, "--label", s.label or _label(s)]
            if save:
                args += ["--save", str(s.report_path)]
            ok, out = env.run_engine(args)
            if not ok:
                raise ToolError("analysis failed: " + out)
            # Keep the exact analysed excerpt for replay even if rendering/answering takes time.
            cached = env.SESSIONS / f"{s.id}-last-analysis.wav"
            with s._files_lock:
                shutil.copyfile(snapshot, cached)
                s.last_analysis_path = cached
                s.last_analysis_window = (begin, end)
            return f"Window: {begin:.1f}–{end:.1f}s from session start.\n" + out
    args = ["analyze", str(s.path), "--source", s.source, "--focus", focus, "--detail", detail,
            "--label", s.label or _label(s)]
    if start > 0:
        args += ["--start", f"{start:.2f}"]
    if save:
        args += ["--save", str(s.report_path)]
    ok, out = env.run_engine(args)
    if not ok:
        raise ToolError("analysis failed: " + out)
    if s.source == "output" and s.max_level <= -119:
        out += ("\n\nHINT: the capture was pure digital silence. If something was playing, macOS may be blocking "
                "system-audio capture — " + PERMISSION_HELP["system_audio"])
    return out


def _start_session(ctx, source: str, device: str | None, max_seconds: int, label: str | None,
                   include_system_audio: bool = False) -> Session:
    if source not in ("output", "input"):
        raise ToolError("source must be 'output' (what the Mac plays) or 'input' (microphone / input device)")
    _ready_or_raise(ctx)
    s = Session(source, device, max_seconds, label)
    REGISTRY.add(s)
    ctx.progress("Starting capture… (if macOS shows a permission prompt for Soundscape Capture, click Allow)")
    if not s.wait_started(20, ctx.cancelled):
        s.stop(2)
        err = s.error or {}
        msg = err.get("message") or "capture helper did not start (see ~/.soundscape/logs)"
        if err.get("code") == "permission_denied":
            msg += "\nThe user must enable it: " + PERMISSION_HELP.get(err.get("kind", ""), "")
        raise ToolError(msg)
    if source == "input" and include_system_audio:
        comp = Session("output", None, max_seconds)
        if comp.wait_started(20, ctx.cancelled):
            s.companion = comp
        else:
            comp.stop(2)
            s.companion_error = (comp.error or {}).get("message", "system audio capture unavailable")[:160]
    return s


def _pick(s: Session, focus: str, start: float = 0.0):
    """For an input session with a companion system-audio capture, choose what to analyse.
    Returns (session_to_analyse, note_for_report, other_session_or_None)."""
    if not s.companion:
        return s, None, None
    mic, out = s, s.companion
    def heard(member):
        if start <= 0:
            return member.sound_seconds
        with member._lock:
            levels = list(member.levels)
        threshold = member.threshold()
        member_start = max(0, member.audio_t - (s.audio_t - start))
        return sum(0.1 for t, rms, _ in levels if t >= member_start and rms > threshold)
    out_sound, mic_sound = heard(out), heard(mic)
    out_has = out_sound >= 0.3
    # A few seconds of typing or room noise next to a whole track doesn't make the mic a real source.
    mic_has = mic_sound >= 0.3 and (not out_has or mic_sound >= max(1.0, 0.3 * out_sound))
    if out_has and not mic_has:
        return out, ("NOTE: the microphone heard only background noise (speakers muted or quiet), so this analyses "
                     "the audio the Mac was playing, captured directly."), None
    if out_has and mic_has:
        if focus in ("tuning", "melody"):
            return mic, ("NOTE: the Mac was also playing audio; this analyses the microphone (your voice/instrument). "
                         "Mac audio at a glance below."), out
        return out, ("NOTE: the Mac was playing audio and the microphone also heard sound; this analyses the Mac's "
                     "audio directly (cleaner than the mic). Microphone at a glance below."), mic
    return mic, None, None


def _glance(s: Session) -> str:
    out = _analyze_session(s, "overview", "brief", start=max(0, s.audio_t - 20), save=False)
    lines = out.splitlines()[1:3]
    if not lines or "noise/ambience" in lines[0]:
        return f"{_label(s)}: only background noise." if lines else ""
    return f"{_label(s)} at a glance: " + " | ".join(lines)


def _analyze_group(s: Session, focus: str, detail: str, start: float = 0.0, save: bool = True) -> str:
    target, note, other = _pick(s, focus, start)
    out = _analyze_session(target, focus, detail, start=start, save=save)
    if note:
        out = note + "\n" + out
    if other is not None:
        g = _glance(other)
        if g:
            out += "\n" + g
    if save:
        s.report_path.write_text(out + "\n")
    return out


def _wait(ctx, s: Session, until: str, max_seconds: float, silence_seconds: float, min_sound: float) -> str:
    t_start = time.time()
    sound_at_start = s.group_sound_seconds()
    last_progress = 0.0
    reason = "timeout"
    while True:
        elapsed = time.time() - t_start
        sound = s.group_sound_seconds()
        if not s.running:
            reason = "capture ended" + (f" ({s.stop_info.get('reason')})" if s.stop_info else "")
            break
        if ctx.cancelled():
            reason = "cancelled"
            break
        if until == "sound" and sound > sound_at_start + 0.3:
            reason = "sound detected"
            break
        if (until == "sound_then_silence" and sound - sound_at_start >= min_sound
                and s.group_silence_for() >= silence_seconds):
            reason = f"audio stopped ({silence_seconds:g}s of silence after {sound:.0f}s of sound)"
            break
        if elapsed >= max_seconds:
            reason = f"waited {max_seconds:g}s"
            break
        if elapsed - last_progress >= 2:
            last_progress = elapsed
            heard = "hearing audio" if s.group_silence_for() < 1 else "silence"
            level = max(m.current_level() for m in s.members())
            ctx.progress(f"Listening… {s.audio_t:.0f}s · {heard} · level {level:.0f} dBFS · "
                         f"sound so far {sound:.0f}s", elapsed, max_seconds)
        time.sleep(0.25)
    playing = s.running and s.group_silence_for() < 1.0
    lines = [f"Result: {reason}.", s.describe(),
             f"Audio is {'still playing' if playing else 'not playing right now'}."]
    nothing = all(m.first_sound_t is None for m in s.members())
    if nothing and s.source == "output" and s.max_level <= -119 and s.audio_t > 5:
        lines.append("Nothing at all has come through (digital silence). Either nothing is playing, or system-audio "
                     "permission is missing: " + PERMISSION_HELP["system_audio"])
    elif nothing and s.source == "input" and s.audio_t > 5:
        lines.append("Only background noise so far — nothing playing on the Mac and nothing loud at the microphone "
                     "(muted mic, wrong device, or too far away?).")
    return "\n".join(lines)


# --- handlers ----------------------------------------------------------------------------------

def t_setup(args, ctx):
    rebuild = bool(args.get("rebuild"))
    problems = env.ensure_ready(progress=ctx.progress, rebuild=rebuild)
    lines = []
    if args.get("install_transcription"):
        ctx.progress("Installing on-device transcription (mlx-whisper)…")
        ok, msg = env.install_transcription()
        lines.append(("transcription: " if ok else "transcription install failed: ") + msg)
    hs, es = env.helper_state(), env.engine_state()
    caps = env.engine_capabilities() if es == "ok" else {}
    request = args.get("request_permission", "none")
    perms = {}
    if hs == "ok":
        if request != "none":
            ctx.progress("Waiting for the macOS permission prompt — the user should click Allow…")
        perms = env.permissions(request)
    lines.insert(0, f"Soundscape {'ready' if not problems else 'NOT ready'}.")
    lines.append(f"  capture helper: {hs} ({env.APP})")
    lines.append(f"  analysis engine: {es}" + (f" (numpy {caps.get('numpy')}; transcription: "
                                               f"{caps.get('transcription') or 'not installed'})" if caps else ""))
    if perms:
        lines.append(f"  permissions: system audio = {perms.get('system_audio')} · microphone = {perms.get('microphone')}")
    if hs == "ok":
        d = env.devices()
        devs = d.get("devices", [])
        di = next((x["name"] for x in devs if x.get("uid") == d.get("default_input")), "?")
        do = next((x["name"] for x in devs if x.get("uid") == d.get("default_output")), "?")
        lines.append(f"  default output: {do} · default input: {di}")
        ins = [f"{x['name']} ({x['input_channels']}ch, {x['transport']})" for x in devs if x.get("input_channels")]
        if ins:
            lines.append("  input devices: " + "; ".join(ins))
        loop = [x["name"] for x in devs if x.get("input_channels") and
                any(k in x["name"].lower() for k in ("blackhole", "loopback", "soundflower", "vb-cable"))]
        if loop:
            lines.append("  loopback devices (usable with source=input): " + ", ".join(loop))
    for p in problems:
        lines.append("PROBLEM: " + p)
    nxt = []
    for kind in ("system_audio", "microphone"):
        state = perms.get(kind)
        if state == "undetermined":
            nxt.append(f"{kind.replace('_', ' ')} permission not granted yet. Tell the user macOS will ask about "
                       f"'Soundscape Capture' and to click Allow, then call setup with request_permission=\"{kind}\" "
                       f"(or just call start — it prompts too).")
        elif state in ("denied", "restricted"):
            nxt.append(f"{kind.replace('_', ' ')} permission is DENIED. The user must enable it: {PERMISSION_HELP[kind]}")
    if nxt:
        lines.append("NEXT: " + "\n      ".join(nxt))
    return "\n".join(lines)


START_LOCK = threading.Lock()


def t_start(args, ctx):
    with START_LOCK:
        return _t_start(args, ctx)


def _t_start(args, ctx):
    continuous = bool(args.get("continuous", False))
    if continuous:
        for sid in reversed(REGISTRY.active_ids()):
            active = REGISTRY.get(sid)
            if (active.max_seconds == 0 and active.source == args.get("source", "output")
                    and active.requested_device == args.get("device")
                    and (active.companion is not None) == (active.source == "input" and args.get("include_system_audio", True))):
                return "Already listening (continuous ON). " + active.describe()
    s = _start_session(ctx, args.get("source", "output"), args.get("device"),
                       0 if continuous else int(min(3600, max(5, args.get("max_seconds", 600)))), args.get("label"),
                       include_system_audio=args.get("include_system_audio", True))
    what = "everything the Mac plays" if s.source == "output" else f"the input device '{s.info.get('device')}'"
    if s.companion:
        what += " plus the Mac's own audio output (works even with the volume muted)"
    elif s.companion_error:
        what += f" (Mac audio output not captured: {s.companion_error})"
    if continuous:
        return (f"Listening ON: {what}. session_id={s.id}. Keeps capturing between questions until stop or "
                "the MCP client disconnects. Rolling last 10–15 minutes retained locally. "
                "Use peek for questions, stream for incremental reports, replay to hear a clip; stop only when asked to turn OFF.")
    return (f"Listening to {what}. session_id={s.id} · {int(s.info.get('sample_rate', 0))} Hz · "
            f"auto-stops after {s.max_seconds}s.\n"
            "Tell the user to start the audio now, then call wait (until='sound_then_silence') to keep listening, "
            "peek for a quick read, or stop for the full analysis.")


def t_wait(args, ctx):
    s = REGISTRY.get(args.get("session_id"))
    return _wait(ctx, s, args.get("until", "sound_then_silence"),
                 float(min(900, max(1, args.get("max_seconds", 120)))),
                 float(max(0.5, args.get("silence_seconds", 4))), float(max(0.0, args.get("min_sound_seconds", 1))))


def t_peek(args, ctx):
    s = REGISTRY.get(args.get("session_id"))
    last = float(min(600, max(1, args.get("last_seconds", 30))))
    start = max(0.0, s.audio_t - last)
    head = s.describe()
    target = _select_source(s, args.get("source", "auto"), args.get("focus", "overview"), start)
    return head + "\n" + _analyze_session(target, args.get("focus", "overview"), args.get("detail", "brief"),
                                         start=max(0.0001, target.audio_t - last), save=False)


def t_stop(args, ctx):
    s = REGISTRY.get(args.get("session_id"))
    was_running = s.running
    s.stop()
    if not args.get("analyze", True):
        if not args.get("keep_audio", True):
            for member in s.members():
                for path in member.recording_paths():
                    path.unlink(missing_ok=True)
                if member.last_analysis_path:
                    member.last_analysis_path.unlink(missing_ok=True)
        return "Listening OFF. " + s.describe()
    ctx.progress("Analysing the capture…")
    out = _analyze_group(s, args.get("focus", "overview"), args.get("detail", "standard"))
    if not args.get("keep_audio", True):
        for m in s.members():
            try:
                for path in m.recording_paths():
                    path.unlink(missing_ok=True)
                if m.last_analysis_path:
                    m.last_analysis_path.unlink(missing_ok=True)
            except OSError:
                pass
        saved = f"report: {s.report_path} (audio deleted)"
    else:
        saved = "audio: " + ", ".join(str(p) for m in s.members() for p in m.recording_paths()) + f" · report: {s.report_path}"
    note = "" if was_running else " (session had already ended)"
    return f"{out}\n\n[stopped{note}; {saved}]"


def t_listen(args, ctx):
    seconds = float(min(900, max(3, args.get("seconds", 30))))
    until = args.get("until", "sound_then_silence")
    s = _start_session(ctx, args.get("source", "output"), args.get("device"), int(seconds) + 5, args.get("label"),
                       include_system_audio=args.get("include_system_audio", True))
    status = _wait(ctx, s, until, seconds, float(args.get("silence_seconds", 4)), 1.0)
    s.stop()
    ctx.progress("Analysing…")
    out = _analyze_group(s, args.get("focus", "overview"), args.get("detail", "standard"))
    return f"{out}\n\n[{status.splitlines()[0]} audio: {', '.join(str(m.path) for m in s.members())}]"


def t_analyze_file(args, ctx):
    path = os.path.expanduser(args.get("path", ""))
    if not path or not Path(path).exists():
        raise ToolError(f"file not found: {path}")
    _ready_or_raise(ctx)
    a = ["analyze", path, "--focus", args.get("focus", "overview"), "--detail", args.get("detail", "standard"),
         "--label", Path(path).name]
    if args.get("start"):
        a += ["--start", str(float(args["start"]))]
    if args.get("end"):
        a += ["--end", str(float(args["end"]))]
    ctx.progress("Analysing file…")
    ok, out = env.run_engine(a)
    if not ok:
        raise ToolError("analysis failed: " + out)
    return out


def t_sessions(args, ctx):
    if args.get("purge_recordings"):
        REGISTRY.stop_all()
        n = 0
        if env.SESSIONS.exists():
            for p in env.SESSIONS.iterdir():
                p.unlink()
                n += 1
        return f"Stopped all sessions and deleted {n} saved recordings/reports from {env.SESSIONS}."
    lines = [REGISTRY.sessions[sid].describe() for sid in REGISTRY.order[-10:]]
    saved = sorted(env.SESSIONS.glob("*.wav"), key=lambda p: p.stat().st_mtime)[-10:] if env.SESSIONS.exists() else []
    out = ["Sessions this run:"] + ([f"  {l}" for l in lines] or ["  none"])
    out.append(f"Saved recordings ({env.SESSIONS}, auto-deleted after "
               f"{os.environ.get('SOUNDSCAPE_RETENTION_DAYS', '7')} days):")
    out += [f"  {p.name}  {p.stat().st_size / 1e6:.1f} MB" for p in saved] or ["  none"]
    return "\n".join(out)



def _select_source(s, source, focus, start=0):
    if source == "auto":
        return _pick(s, focus, start)[0]
    for member in s.members():
        if member.source == source:
            return member
    raise ToolError(f"This session does not capture {source} audio.")


def t_stream(args, ctx):
    s = REGISTRY.get(args.get("session_id"))
    last = min(60, max(1, float(args.get("last_seconds", 20))))
    target = _select_source(s, args.get("source", "auto"), args.get("focus", "overview"),
                            max(0, s.audio_t - last))
    # Each source has its own audio clock: system taps may deliver no frames while idle.
    with target.stream_lock:
        cursor = target.stream_cursor
        if target.audio_t - cursor < 1:
            return "No new audio yet. " + s.describe()
        start = max(cursor, target.audio_t - last)
        end = target.audio_t
        out = _analyze_session(target, args.get("focus", "overview"), args.get("detail", "brief"),
                               start=max(0.0001, start), save=False)
        target.stream_cursor = end
        gap = f"Skipped {start - cursor:.1f}s of older audio; " if start > cursor else ""
        return f"{gap}stream through {end:.1f}s of {target.source} audio. Capture {'ON' if s.running else 'OFF'}.\n{out}"


def t_replay(args, ctx):
    s = REGISTRY.get(args.get("session_id"))
    last = min(120, max(1, float(args.get("last_seconds", 30))))
    target = _select_source(s, args.get("source", "auto"), "chords", max(0, s.audio_t - last))
    path = env.SESSIONS / f"{s.id}-replay-{time.time_ns()}.wav"
    try:
        with target._files_lock:
            cached = target.last_analysis_path if args.get("from_analysis", True) else None
            if cached and cached.exists():
                with wave.open(str(cached), "rb") as src, wave.open(str(path), "wb") as out:
                    out.setparams(src.getparams())
                    count = min(src.getnframes(), int(120 * src.getframerate()))
                    src.setpos(src.getnframes() - count)
                    out.writeframes(src.readframes(count))
                    begin, end = target.last_analysis_window
                    begin = max(begin, end - 120)
        if not cached or not cached.exists():
            begin, end = target.snapshot(path, last_seconds=last)
    except (ValueError, OSError) as exc:
        raise ToolError(str(exc)) from exc
    if args.get("play", True):
        # Snapshot before playback; never recursively analyse/replay our own output.
        proc = subprocess.Popen(["/usr/bin/afplay", str(path)], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        threading.Thread(target=proc.wait, daemon=True).start()
    return (f"{'Playing' if args.get('play', True) else 'Saved'} {begin:.1f}–{end:.1f}s through the current Mac "
            f"output (select AirPods in macOS). Audio: {path}. "
            f"Capture is {'ON' if s.running else 'OFF'}; playback may be heard by the capture. Do not analyse it as new evidence.")


def t_practice_card(args, ctx):
    from .practice import create_card
    try:
        path = create_card(env.SESSIONS, args)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    import base64
    return {"content": [{"type": "text", "text": f"Practice question image: {path}. "
                        "Choices supplied from the audio analysis; this is original practice material, not an official AP question."},
                        {"type": "image", "mimeType": "image/png", "data": base64.b64encode(path.read_bytes()).decode()}],
            "isError": False}


TOOLS = [
    {
        "name": "setup",
        "description": "Prepare Soundscape (build the local capture helper, install the analysis engine) and report "
                       "macOS permissions and audio devices. Fast when already set up. Call first in a session.",
        "inputSchema": {"type": "object", "properties": {
            "request_permission": {"type": "string", "enum": ["none", "system_audio", "microphone", "all"],
                                   "default": "none",
                                   "description": "Trigger the macOS permission prompt (warn the user first)."},
            "rebuild": {"type": "boolean", "default": False},
            "install_transcription": {"type": "boolean", "default": False,
                                      "description": "Install on-device speech-to-text (mlx-whisper, ~0.5 GB model)."},
        }},
        "handler": t_setup,
    },
    {
        "name": "start",
        "description": "Start listening in the background. source='output' captures everything the Mac plays "
                       "(music, video, calls; any output device incl. headphones; even when muted); source='input' "
                       "captures the microphone or another input device, plus (by default) the Mac's own output. "
                       "Returns a session_id; follow with wait, peek or stop.",
        "inputSchema": {"type": "object", "required": ["source"], "properties": {
            "source": {"type": "string", "enum": ["output", "input"]},
            "device": {"type": "string", "description": "Input device name/UID (source=input only). Default: system default."},
            "include_system_audio": INCLUDE_SYS,
            "continuous": {"type": "boolean", "default": False, "description": "Keep listening until stop/client disconnect, retaining a rolling 10–15 minute window. Reuses a matching active continuous session."},
            "max_seconds": {"type": "integer", "default": 600, "description": "Auto-stop after this long (≤3600)."},
            "label": {"type": "string"},
        }},
        "handler": t_start,
    },
    {
        "name": "wait",
        "description": "Keep listening (blocks) until a condition is met, then return a short status (no analysis). "
                       "until='sound_then_silence' returns after audio has played and then stopped; 'sound' returns as "
                       "soon as audio is heard; 'duration' listens for max_seconds.",
        "inputSchema": {"type": "object", "properties": {
            "session_id": SESSION_ID,
            "until": {"type": "string", "enum": ["sound_then_silence", "sound", "duration"],
                      "default": "sound_then_silence"},
            "max_seconds": {"type": "number", "default": 120, "description": "Give up after this long (≤900)."},
            "silence_seconds": {"type": "number", "default": 4},
            "min_sound_seconds": {"type": "number", "default": 1,
                                  "description": "Sound needed before silence can end the wait (a quiz chord can be ~1 s)."},
        }},
        "handler": t_wait,
    },
    {
        "name": "peek",
        "description": "Quick analysis of the most recent audio while the session keeps listening.",
        "inputSchema": {"type": "object", "properties": {
            "session_id": SESSION_ID,
            "last_seconds": {"type": "number", "default": 30, "minimum": 1, "maximum": 600},
            "source": {"type": "string", "enum": ["auto", "output", "input"], "default": "auto"},
            "focus": FOCUS,
            "detail": dict(DETAIL, default="brief"),
        }},
        "handler": t_peek,
    },
    {
        "name": "stop",
        "description": "Stop listening and return analysis (last 60 seconds for continuous sessions; full capture for timed sessions) "
                       "(silence at the edges is trimmed). Also saves the report as Markdown.",
        "inputSchema": {"type": "object", "properties": {
            "session_id": SESSION_ID,
            "focus": FOCUS,
            "detail": DETAIL,
            "analyze": {"type": "boolean", "default": True, "description": "Set false for immediate OFF without analysis."},
            "keep_audio": {"type": "boolean", "default": True,
                           "description": "Keep the WAV (auto-deleted after 7 days) for re-analysis."},
        }},
        "handler": t_stop,
    },
    {
        "name": "listen",
        "description": "One-shot: start, listen until the audio stops (or for `seconds` with until='duration'), "
                       "then return the analysis. Good for short clips and tuning checks.",
        "inputSchema": {"type": "object", "required": ["source"], "properties": {
            "source": {"type": "string", "enum": ["output", "input"]},
            "device": {"type": "string"},
            "include_system_audio": INCLUDE_SYS,
            "seconds": {"type": "number", "default": 30, "description": "Maximum listening time (≤900)."},
            "until": {"type": "string", "enum": ["sound_then_silence", "duration"], "default": "sound_then_silence"},
            "silence_seconds": {"type": "number", "default": 4},
            "focus": FOCUS,
            "detail": DETAIL,
        }},
        "handler": t_listen,
    },
    {
        "name": "analyze_file",
        "description": "Analyse an audio or video file on disk (wav, mp3, m4a, aiff, flac, mp4, …) — same report as a capture.",
        "inputSchema": {"type": "object", "required": ["path"], "properties": {
            "path": {"type": "string"},
            "start": {"type": "number", "description": "Start time in seconds."},
            "end": {"type": "number", "description": "End time in seconds."},
            "focus": FOCUS,
            "detail": DETAIL,
        }},
        "handler": t_analyze_file,
    },
    {
        "name": "sessions",
        "description": "List listening sessions and saved recordings, or delete all recordings (purge_recordings).",
        "inputSchema": {"type": "object", "properties": {
            "purge_recordings": {"type": "boolean", "default": False},
        }},
        "handler": t_sessions,
    },
]

TOOLS += [
    {"name": "stream", "description": "Read the next bounded audio report without stopping capture. Call again for new audio; reports are pulled, not pushed into chat. No-new-audio returns immediately.",
     "inputSchema": {"type": "object", "properties": {"session_id": SESSION_ID, "focus": FOCUS,
         "detail": dict(DETAIL, default="brief"), "last_seconds": {"type": "number", "default": 20},
         "source": {"type": "string", "enum": ["auto", "output", "input"], "default": "auto"}}}, "handler": t_stream},
    {"name": "replay", "description": "Save and optionally play recent captured audio through the current Mac output, including AirPods. Capture continues; replay may enter the capture.",
     "inputSchema": {"type": "object", "properties": {"session_id": SESSION_ID,
         "last_seconds": {"type": "number", "default": 30, "minimum": 1, "maximum": 120},
         "source": {"type": "string", "enum": ["auto", "output", "input"], "default": "auto"},
         "from_analysis": {"type": "boolean", "default": True, "description": "Replay the exact last analysed excerpt (up to 120s), if available. Set false to use last_seconds of current capture."},
         "play": {"type": "boolean", "default": True}}}, "handler": t_replay},
    {"name": "practice_card", "description": "Render an original multiple-choice music-theory question as a PNG image. First analyse audio, then supply the question, plausible choices, and evidence-based answer. Never fabricate heard notes.",
     "inputSchema": {"type": "object", "required": ["question", "choices"], "properties": {
         "question": {"type": "string", "maxLength": 600},
         "choices": {"type": "array", "minItems": 2, "maxItems": 5, "items": {"type": "string", "maxLength": 240}},
         "answer": {"type": "integer", "minimum": 0, "maximum": 4, "description": "Zero-based correct choice, optional; include only if supported."},
         "explanation": {"type": "string", "maxLength": 1000},
         "reveal_answer": {"type": "boolean", "default": False}}}, "handler": t_practice_card},
]

BY_NAME = {t["name"]: t for t in TOOLS}
