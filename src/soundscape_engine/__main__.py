"""CLI: python -m soundscape_engine analyze FILE [options]"""

from __future__ import annotations

import argparse
import json
import sys

import numpy as np


def _jsonable(o):
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    return o


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="soundscape_engine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze", help="analyse an audio file and print a report")
    a.add_argument("path")
    a.add_argument("--start", type=float, default=0.0)
    a.add_argument("--end", type=float, default=0.0)
    a.add_argument("--source", default="file", choices=["file", "output", "input"])
    a.add_argument("--focus", default="overview")
    a.add_argument("--detail", default="standard", choices=["brief", "standard", "full"])
    a.add_argument("--label", default=None)
    a.add_argument("--format", default="text", choices=["text", "json"])
    a.add_argument("--transcribe", default="auto", choices=["auto", "yes", "no"])
    a.add_argument("--save", default=None, help="also write the text report to this path")
    sub.add_parser("capabilities", help="print optional capabilities as JSON")
    args = ap.parse_args(argv)

    if args.cmd == "capabilities":
        from . import transcribe
        print(json.dumps({"numpy": np.__version__, "transcription": transcribe.available()}))
        return 0

    from . import report, transcribe
    from .analysis import analyze

    end = args.end if args.end > 0 else None
    res = analyze(args.path, args.start, end, source=args.source)

    transcript = None
    is_speech = res.get("content", {}).get("label", "").startswith("speech")
    if args.transcribe == "yes" or (args.transcribe == "auto" and res.get("status") is None and (
            args.focus in ("speech", "all") or (args.focus == "overview" and is_speech))):
        if transcribe.available() or args.focus == "speech" or args.transcribe == "yes":
            transcript = transcribe.transcribe(args.path, args.start, end, full=args.detail == "full")

    if args.format == "json":
        res["transcript"] = transcript
        print(json.dumps(_jsonable(res)))
        return 0
    text = report.render(res, args.focus, args.detail, args.label, transcript)
    if args.save:
        with open(args.save, "w") as f:
            f.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
