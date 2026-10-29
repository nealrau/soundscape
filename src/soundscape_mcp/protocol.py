"""Minimal MCP (JSON-RPC 2.0 over stdio) server: initialize, tools/list, tools/call, ping,
cancellation and progress notifications. Standard library only, so it starts instantly."""

from __future__ import annotations

import json
import logging
import sys
import threading
import traceback

from . import __version__
from .tools import BY_NAME, TOOLS, ToolError

log = logging.getLogger("soundscape")

SUPPORTED_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]

INSTRUCTIONS = (
    "Soundscape captures Mac audio locally, including audio playing through AirPods. "
    "For input ON/init: setup → start(source='input', continuous=true, include_system_audio=true). "
    "Prefer a built-in Mac input device when AirPods are used for playback. For desktop audio alone use output. "
    "Keep listening between questions: peek/stream analyse snapshots without stopping. Silence does not turn it off. "
    "OFF means stop(analyze=false). Client disconnect also stops capture. Reports are pulled on tool calls, not pushed. "
    "For music-theory practice use peek(focus='all', detail='full', source='output'), "
    "practice_card for a multiple-choice PNG, replay to hear the clip. Speech transcription requires setup "
    "with install_transcription=true. Distinguish speech transcripts from estimated musical notes; state uncertainty."
)


class Ctx:
    def __init__(self, server: "Server", req_id, token):
        self.server, self.req_id, self.token = server, req_id, token
        self._n = 0.0

    def progress(self, message: str, progress: float | None = None, total: float | None = None):
        log.info("progress[%s]: %s", self.req_id, message)
        if self.token is None:
            return
        self._n = max(self._n + 0.001, progress if progress is not None else self._n + 1)
        params = {"progressToken": self.token, "progress": self._n, "message": message}
        if total is not None and progress is not None:
            params["total"] = total
        self.server.send({"jsonrpc": "2.0", "method": "notifications/progress", "params": params})

    def cancelled(self) -> bool:
        return self.req_id in self.server.cancelled


class Server:
    def __init__(self):
        self.lock = threading.Lock()
        self.cancelled: set = set()

    def send(self, obj: dict):
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self.lock:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    def reply(self, req_id, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": req_id}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        self.send(msg)

    def call_tool(self, req_id, params: dict):
        name = params.get("name")
        args = params.get("arguments") or {}
        token = (params.get("_meta") or {}).get("progressToken")
        tool = BY_NAME.get(name)
        if not tool:
            self.reply(req_id, error={"code": -32602, "message": f"unknown tool: {name}"})
            return
        ctx = Ctx(self, req_id, token)
        log.info("call %s %s", name, json.dumps(args))
        try:
            text, is_error = tool["handler"](args, ctx), False
        except (ToolError, KeyError) as e:
            text, is_error = str(e).strip("'\""), True
        except Exception as e:
            log.error("tool %s crashed: %s", name, traceback.format_exc())
            text, is_error = f"internal error in {name}: {type(e).__name__}: {e}", True
        if req_id in self.cancelled:
            self.cancelled.discard(req_id)
            return
        self.reply(req_id, text if isinstance(text, dict) else
                   {"content": [{"type": "text", "text": text}], "isError": is_error})

    def handle(self, msg: dict):
        method, req_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method is None:
            return  # a response to something we never send
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
            self.reply(req_id, {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "soundscape", "version": __version__},
                "instructions": INSTRUCTIONS,
            })
        elif method == "ping":
            self.reply(req_id, {})
        elif method == "tools/list":
            self.reply(req_id, {"tools": [{k: t[k] for k in ("name", "description", "inputSchema")} for t in TOOLS]})
        elif method == "tools/call":
            threading.Thread(target=self.call_tool, args=(req_id, params), daemon=True).start()
        elif method == "notifications/cancelled":
            self.cancelled.add(params.get("requestId"))
        elif method.startswith("notifications/"):
            pass
        elif req_id is not None:
            self.reply(req_id, error={"code": -32601, "message": f"method not found: {method}"})

    def serve(self):
        log.info("soundscape MCP server %s starting (python %s)", __version__, sys.version.split()[0])
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
            except ValueError:
                self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            for m in msg if isinstance(msg, list) else [msg]:
                try:
                    self.handle(m)
                except Exception:
                    log.error("handler error: %s", traceback.format_exc())
        log.info("stdin closed; shutting down")
