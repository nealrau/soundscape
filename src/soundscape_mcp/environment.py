"""Paths, one-time setup (capture helper + analysis engine), permissions and devices."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SRC = PLUGIN_ROOT / "src"
HELPER_SRC = PLUGIN_ROOT / "helper"
HOME = Path(os.environ.get("SOUNDSCAPE_HOME", "~/.soundscape")).expanduser()
APP = HOME / "SoundscapeCapture.app"
HELPER = APP / "Contents" / "MacOS" / "soundscape-capture"
VENV = HOME / "venv"
VENV_PY = VENV / "bin" / "python"
SESSIONS = HOME / "sessions"
LOGS = HOME / "logs"

EXTRA_PATH = ["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local/bin"), "/usr/bin", "/bin",
              "/usr/sbin", "/sbin"]

log = logging.getLogger("soundscape")


def tool_env() -> dict:
    env = dict(os.environ)
    parts = env.get("PATH", "").split(":") if env.get("PATH") else []
    env["PATH"] = ":".join(parts + [p for p in EXTRA_PATH if p not in parts])
    env["PYTHONPATH"] = str(SRC)
    env.setdefault("PYTHONUTF8", "1")
    return env


def which(name: str) -> str | None:
    return shutil.which(name, path=tool_env()["PATH"])


def find_uv() -> str | None:
    for c in (which("uv"), str(Path.home() / ".local/bin/uv"), str(Path.home() / ".cargo/bin/uv"),
              "/opt/homebrew/bin/uv", "/usr/local/bin/uv"):
        if c and os.access(c, os.X_OK):
            return c
    return None


def run(cmd: list, timeout: float = 600, **kw) -> subprocess.CompletedProcess:
    log.info("run: %s", " ".join(map(str, cmd)))
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=timeout,
                          env=tool_env(), **kw)


# --- capture helper -------------------------------------------------------------------------

def helper_source_hash() -> str:
    h = hashlib.sha256()
    for name in ("SoundscapeCapture.swift", "Info.plist"):
        h.update((HELPER_SRC / name).read_bytes())
    return h.hexdigest()


def helper_state() -> str:
    if not HELPER.exists():
        return "missing"
    stamp = APP / "Contents" / "Resources" / "source.sha256"
    try:
        built = stamp.read_text().strip()
    except OSError:
        return "stale"
    return "ok" if built == helper_source_hash() else "stale"


def build_helper() -> tuple[bool, str]:
    proc = run(["/bin/bash", HELPER_SRC / "build.sh", APP], timeout=600)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip()[-1500:]
        if "swiftc not found" in msg or "xcrun: error" in msg:
            msg += "\nFix: install Apple's command line tools with `xcode-select --install`, then run setup again."
        return False, msg
    return True, f"built {APP}"


def helper_json(args: list, timeout: float = 30) -> list[dict]:
    proc = run([HELPER] + args, timeout=timeout)
    events = []
    for line in proc.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    return events


def permissions(request: str | None = None) -> dict:
    args = ["permission"] + (["--request", request] if request and request != "none" else [])
    for ev in helper_json(args, timeout=180 if request and request != "none" else 30):
        if ev.get("event") == "permission":
            return ev
    return {"system_audio": "unknown", "microphone": "unknown"}


def devices() -> dict:
    for ev in helper_json(["devices"]):
        if ev.get("event") == "devices":
            return ev
    return {"devices": []}


# --- analysis engine ------------------------------------------------------------------------

def engine_state() -> str:
    if not VENV_PY.exists():
        return "missing"
    proc = run([VENV_PY, "-c", "import numpy, PIL"], timeout=60)
    return "ok" if proc.returncode == 0 else "broken"


def pip_install(packages: list[str]) -> tuple[bool, str]:
    uv = find_uv()
    if uv:
        proc = run([uv, "pip", "install", "--quiet", "--python", VENV_PY] + packages, timeout=1200)
    else:
        proc = run([VENV_PY, "-m", "pip", "install", "--quiet", "--disable-pip-version-check"] + packages,
                   timeout=1200)
    return proc.returncode == 0, (proc.stderr or proc.stdout).strip()[-1500:]


def build_engine() -> tuple[bool, str]:
    HOME.mkdir(parents=True, exist_ok=True)
    if VENV_PY.exists():
        return pip_install(["numpy>=1.24", "Pillow>=10"])
    if VENV.exists():
        shutil.rmtree(VENV)
    uv = find_uv()
    if uv:
        proc = run([uv, "venv", "--quiet", "--python", ">=3.9", VENV], timeout=600)
    else:
        proc = run([sys.executable, "-m", "venv", VENV], timeout=600)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()[-1500:]
    ok, msg = pip_install(["numpy>=1.24", "Pillow>=10"])
    return ok, "installed numpy into " + str(VENV) if ok else msg


def engine_capabilities() -> dict:
    if engine_state() != "ok":
        return {}
    proc = run([VENV_PY, "-m", "soundscape_engine", "capabilities"], timeout=60)
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}


def install_transcription() -> tuple[bool, str]:
    if os.uname().machine != "arm64":
        return False, "on-device transcription uses mlx-whisper, which needs Apple Silicon"
    ok, msg = pip_install(["mlx-whisper"])
    return ok, ("installed mlx-whisper (the speech model downloads on first use, ~0.5 GB)" if ok else msg)


def run_engine(args: list, timeout: float = 900) -> tuple[bool, str]:
    proc = run([VENV_PY, "-m", "soundscape_engine"] + args, timeout=timeout)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()[-2000:]
    return True, proc.stdout.strip()


# --- orchestration ----------------------------------------------------------------------------

def ensure_ready(progress=None, rebuild: bool = False) -> list[str]:
    """Build whatever is missing. Returns problems (empty when ready)."""
    problems = []
    if sys.platform != "darwin":
        return ["Soundscape capture only runs on macOS."]
    hs = helper_state()
    if rebuild or hs != "ok":
        if progress:
            progress("Building the audio capture helper (one-time, ~20 s)…")
        ok, msg = build_helper()
        if not ok:
            problems.append("capture helper build failed: " + msg)
    if rebuild or engine_state() != "ok":
        if progress:
            progress("Installing the analysis engine (numpy, one-time)…")
        ok, msg = build_engine()
        if not ok:
            problems.append("analysis engine install failed: " + msg)
    return problems


def cleanup_old_sessions(days: float | None = None):
    days = float(os.environ.get("SOUNDSCAPE_RETENTION_DAYS", "7")) if days is None else days
    if not SESSIONS.exists() or days <= 0:
        return
    cutoff = time.time() - days * 86400
    for p in SESSIONS.iterdir():
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def setup_logging():
    LOGS.mkdir(parents=True, exist_ok=True)
    path = LOGS / "server.log"
    try:
        if path.exists() and path.stat().st_size > 2_000_000:
            path.replace(LOGS / "server.log.1")
    except OSError:
        pass
    logging.basicConfig(filename=str(path), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
