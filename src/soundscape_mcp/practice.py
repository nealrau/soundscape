"""Local, deterministic multiple-choice PNGs; no model or network calls."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def create_card(directory, args):
    from . import environment as env
    question, choices = args.get("question", ""), args.get("choices", [])
    if not isinstance(question, str) or not question.strip() or len(question) > 600:
        raise ValueError("question must contain 1–600 characters")
    if not isinstance(choices, list) or not 2 <= len(choices) <= 5 or any(
            not isinstance(c, str) or not c.strip() or len(c) > 240 for c in choices):
        raise ValueError("provide 2–5 choices of 1–240 characters each")
    answer = args.get("answer")
    if answer is not None and (type(answer) is not int or not 0 <= answer < len(choices)):
        raise ValueError("answer must be a zero-based index into choices")
    if not isinstance(args.get("explanation", ""), str) or len(args.get("explanation", "")) > 1000:
        raise ValueError("explanation must be at most 1000 characters")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"practice-{time.time_ns()}.png"
    proc = env.run([env.VENV_PY, "-m", "soundscape_mcp.practice", str(path), json.dumps(args)])
    if proc.returncode:
        raise ValueError("Could not render image. Run setup first. " + proc.stderr[-300:])
    return path


def render(path, args):
    from PIL import Image, ImageDraw, ImageFont
    font_path = "/System/Library/Fonts/Supplemental/Arial.ttf"
    def font(size):
        try:
            return ImageFont.truetype(font_path, size)
        except OSError:
            return ImageFont.truetype("DejaVuSans.ttf", size)
    regular, title, small = font(28), font(38), font(20)
    canvas = Image.new("RGB", (1200, 5000), "#f6f7fb")
    draw = ImageDraw.Draw(canvas)
    y = 55
    def paragraph(text, face=regular, color="#19243b", indent=60):
        nonlocal y
        # Character wrapping also handles long uninterrupted tokens.
        for source_line in text.splitlines() or [""]:
            line = ""
            for char in source_line:
                if draw.textlength(line + char, font=face) > 1080:
                    draw.text((indent, y), line, font=face, fill=color)
                    y += face.size + 12
                    line = ""
                line += char
            draw.text((indent, y), line, font=face, fill=color)
            y += face.size + 12
    paragraph("SOUNDSCAPE  /  EAR TRAINING", small, "#5265bc")
    y += 20
    paragraph(args["question"], title)
    y += 32
    for index, choice in enumerate(args["choices"]):
        paragraph(f"{chr(65 + index)}.  {choice}")
        y += 20
    if args.get("reveal_answer") and args.get("answer") is not None:
        y += 15
        paragraph("Answer: " + chr(65 + args["answer"]), color="#286346")
        paragraph(args.get("explanation", ""), small)
    y += 25
    paragraph("Original AP-style practice • Audio estimates may be uncertain", small, "#667085")
    canvas.crop((0, 0, 1200, min(5000, y + 40))).save(path)


if __name__ == "__main__":
    render(Path(sys.argv[1]), json.loads(sys.argv[2]))
