"""Listening sessions: one capture-helper process per session, plus live level tracking."""

from __future__ import annotations

import json
import logging
import os
import secrets
import signal
import subprocess
import threading
import time
import wave
from pathlib import Path
from collections import deque
from datetime import datetime

from . import environment as env

log = logging.getLogger("soundscape")

ABS_SOUND_DB = -60.0      # never call anything quieter than this "sound"
# Upper cap on the sound/silence boundary. System output usually starts from digital silence, so a
# low cap keeps quiet musical passages counted as sound; a microphone needs headroom above room noise.
MAX_SOUND_THRESHOLD = {"output": -45.0, "input": -30.0}


class Session:
    def __init__(self, source: str, device: str | None = None, max_seconds: int = 600, label: str | None = None):
        env.SESSIONS.mkdir(parents=True, exist_ok=True)
        self.id = datetime.now().strftime("%m%d-%H%M%S-") + secrets.token_hex(2)
        self.source = source
        self.requested_device = device
        self.max_seconds = max_seconds
        self.label = label
        self.path = env.SESSIONS / f"{self.id}.wav"
        self.report_path = env.SESSIONS / f"{self.id}.md"
        self.segments = [(self.path, 0.0)]
        self._files_lock = threading.Lock()
        self.last_analysis_path = None
        self.last_analysis_window = None
        self.stream_cursor = 0.0
        self.stream_lock = threading.Lock()
        self.state = "starting"
        self.error: dict | None = None
        self.info: dict = {}
        self.stop_info: dict = {}
        self.started_wall = time.time()
        self.permission_prompt = False
        self.levels: deque = deque(maxlen=36000)   # (t, rms_db, peak_db) at 10 Hz: up to 1 hour
        self.audio_t = 0.0
        self.first_sound_t: float | None = None
        self.last_sound_t: float | None = None
        self.sound_seconds = 0.0
        self.last_sound_wall: float | None = None
        self.max_level = -120.0
        # Input sessions can carry a companion system-output capture, so the Mac's own audio is
        # still heard when its speakers are muted (the microphone alone would hear nothing).
        self.companion: Session | None = None
        self.companion_error: str | None = None
        self._thr, self._thr_at, self._n_levels = ABS_SOUND_DB, 0, 0
        self._lock = threading.Lock()
        self._started = threading.Event()
        self._ended = threading.Event()
        cmd = [str(env.HELPER), "record", "--source", source, "--out", str(self.path),
               "--seconds", str(max_seconds), "--watch-stdin"]
        if device and source == "input":
            cmd += ["--device", device]
        self._stderr = open(env.LOGS / f"helper-{self.id}.log", "w")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
                                     env=env.tool_env(), start_new_session=True)
        threading.Thread(target=self._read, daemon=True).start()

    # --- event stream ---
    def _read(self):
        for raw in self.proc.stdout:
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            kind = ev.get("event")
            if kind == "level":
                self._on_level(ev)
            elif kind == "segment":
                with self._files_lock:
                    self.segments.append((Path(ev["path"]), float(ev["start"])))
                    # Keep two completed five-minute segments plus the active segment.
                    while len(self.segments) > 3:
                        old, _ = self.segments.pop(0)
                        old.unlink(missing_ok=True)
            elif kind == "started":
                self.info = ev
                self.state = "recording"
                self._started.set()
            elif kind == "permission_prompt":
                self.permission_prompt = True
            elif kind == "error":
                self.error = ev
                self.state = "error"
                self._started.set()
            elif kind == "stopped":
                self.stop_info = ev
        self.proc.wait()
        if self.state != "error":
            self.state = "stopped"
        self._started.set()
        self._ended.set()
        self._stderr.close()
        log.info("session %s ended: %s %s", self.id, self.state, self.stop_info or self.error)

    def threshold(self) -> float:
        """Sound/silence boundary: 10 dB over the noise floor, clamped to [-60 dBFS, source cap]."""
        n = self._n_levels
        if n < 20:
            return ABS_SOUND_DB
        if n - self._thr_at >= 20:
            with self._lock:
                vals = sorted(l[1] for l in list(self.levels)[-3000:])
            floor = vals[len(vals) // 10]
            self._thr = min(MAX_SOUND_THRESHOLD[self.source], max(ABS_SOUND_DB, floor + 10))
            self._thr_at = n
        return self._thr

    def _on_level(self, ev: dict):
        t, rms, peak = ev.get("t", 0.0), ev.get("rms_db", -120.0), ev.get("peak_db", -120.0)
        with self._lock:
            dt = max(0.0, t - self.audio_t)
            self.audio_t = t
            self.levels.append((t, rms, peak))
            self._n_levels += 1
            self.max_level = max(self.max_level, rms)
        if rms > self.threshold():
            if self.first_sound_t is None:
                self.first_sound_t = t
            self.last_sound_t = t
            self.last_sound_wall = time.time()
            self.sound_seconds += dt

    # --- control ---
    def wait_started(self, timeout: float = 20.0, cancelled=lambda: False) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline and not cancelled():
            if self._started.wait(0.25):
                return self.state == "recording"
            if self.permission_prompt:
                deadline = max(deadline, time.time() + 150)  # give the user time to answer macOS
        return self.state == "recording"

    @property
    def running(self) -> bool:
        return self.proc.poll() is None

    def stop(self, timeout: float = 8.0):
        if self.companion:
            self.companion.stop(timeout)
        if self.running:
            try:
                os.kill(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if not self._ended.wait(timeout):
                self.proc.kill()
                self._ended.wait(2)

    def snapshot(self, destination: Path, last_seconds: float = 30, start: float | None = None):
        """Copy a bounded, immutable PCM window, including across segment boundaries."""
        end = self.audio_t
        begin = max(0.0, end - last_seconds) if start is None else max(0.0, start, end - 600)
        count = 0
        with self._files_lock, wave.open(str(destination), "wb") as out:
            params = None
            begin = max(begin, self.segments[0][1])
            for path, offset in self.segments:
                if offset >= end:
                    break
                with wave.open(str(path), "rb") as src:
                    rate = src.getframerate()
                    left = max(0, int((begin - offset) * rate))
                    right = min(src.getnframes(), int((end - offset) * rate))
                    if right <= left:
                        continue
                    current = (src.getnchannels(), src.getsampwidth(), rate)
                    if params is None:
                        params = current
                        out.setnchannels(current[0]); out.setsampwidth(current[1]); out.setframerate(rate)
                    elif current != params:
                        raise ValueError("Audio format changed; start a new listening session.")
                    src.setpos(left)
                    out.writeframes(src.readframes(right - left))
                    count += right - left
            if params is None:
                out.setnchannels(1); out.setsampwidth(2); out.setframerate(48000)
        if not count:
            destination.unlink(missing_ok=True)
            raise ValueError("No audio available in this window yet. Play a clip and try again.")
        return begin, end

    def recording_paths(self):
        with self._files_lock:
            return [p for p, _ in self.segments]

    def current_level(self) -> float:
        with self._lock:
            recent = [l[1] for l in list(self.levels)[-5:]]
        return max(recent) if recent else -120.0

    def silence_for(self) -> float:
        return max(0.0, time.time() - (self.last_sound_wall or self.started_wall))

    def members(self) -> list:
        return [self] + ([self.companion] if self.companion else [])

    def group_sound_seconds(self) -> float:
        return max(m.sound_seconds for m in self.members())

    def group_silence_for(self) -> float:
        return min(m.silence_for() for m in self.members())

    def _summary(self) -> str:
        if self.first_sound_t is None:
            return "no sound heard yet" if self.max_level > -119 else "digital silence so far"
        return f"sound for {self.sound_seconds:.0f}s, last heard {self.silence_for():.1f}s ago"

    def describe(self) -> str:
        dev = self.info.get("device") or self.requested_device or ("system audio" if self.source == "output" else "default input")
        parts = [f"session {self.id}", f"{self.source} ({dev})", self.state, f"{self.audio_t:.0f}s captured",
                 self._summary()]
        if self.companion:
            parts.append(f"Mac audio output: {self.companion._summary()}")
        elif self.companion_error:
            parts.append(f"Mac audio output not captured ({self.companion_error})")
        if self.max_seconds == 0:
            parts.append("continuous " + ("ON" if self.running else "OFF") + "; last 10–15 min retained")
        return " · ".join(parts)


class Registry:
    def __init__(self):
        self.sessions: dict[str, Session] = {}
        self.order: list[str] = []

    def add(self, s: Session):
        self.sessions[s.id] = s
        self.order.append(s.id)

    def get(self, session_id: str | None) -> Session:
        if session_id:
            if session_id not in self.sessions:
                raise KeyError(f"unknown session '{session_id}'. Active: {', '.join(self.active_ids()) or 'none'}")
            return self.sessions[session_id]
        for sid in reversed(self.order):
            if self.sessions[sid].running:
                return self.sessions[sid]
        if self.order:
            return self.sessions[self.order[-1]]
        raise KeyError("no listening session. Call start first.")

    def active_ids(self) -> list[str]:
        return [sid for sid in self.order if self.sessions[sid].running]

    def stop_all(self):
        for s in self.sessions.values():
            try:
                s.stop(timeout=3)
            except Exception:
                pass


REGISTRY = Registry()
