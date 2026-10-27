"""Engine accuracy tests on synthetic audio.  Run:  ~/.soundscape/venv/bin/python -m unittest discover tests"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from soundscape_engine import music, report  # noqa: E402
from soundscape_engine.analysis import analyze  # noqa: E402
from synth import SR, progression, tone, write_wav  # noqa: E402


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="soundscape-test-")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_clip(self, name, x, **kw):
        path = os.path.join(self.tmp, name + ".wav")
        write_wav(path, x)
        return analyze(path, **kw)

    def key_of(self, res):
        kc = res["key_choice"]
        return music.key_name(kc["tonic"], kc["minor"])

    def test_c_major(self):
        res = self.run_clip("cmaj", progression(["C", "F", "G", "C", "Am", "F", "G7", "C"] * 2))
        self.assertEqual(self.key_of(res), "C major")

    def test_e_minor_loop(self):
        res = self.run_clip("emin", progression(["Em", "C", "G", "D"] * 4 + ["Em"], bpm=96))
        self.assertEqual(self.key_of(res), "E minor")
        segs = [s[2] for s in res["chords"]["segments"] if s[2]]
        loop = music.find_loop(segs)
        self.assertIsNotNone(loop)
        names = {music.chord_label(r, q, music.PITCH_SHARP) for r, q in loop["pattern"]}
        self.assertEqual(names, {"Em", "C", "G", "D"})

    def test_a_minor_harmonic(self):
        res = self.run_clip("amin", progression(["Am", "Dm", "E", "Am"] * 3, bpm=110))
        self.assertEqual(self.key_of(res), "A minor")

    def test_f_sharp_major(self):
        res = self.run_clip("fsharp", progression(["F#", "B", "C#", "F#", "D#m", "B", "C#", "F#"] * 2))
        self.assertEqual(self.key_of(res), "F# major")

    def test_detuned_recording(self):
        res = self.run_clip("detuned", progression(["Bb", "Eb", "F", "Bb"] * 3, a4=448.0))
        self.assertEqual(self.key_of(res), "Bb major")
        self.assertAlmostEqual(res["tuning"]["cents"], 1200 * np.log2(448 / 440), delta=6)

    def test_tempo(self):
        for bpm in (84, 100, 128):
            res = self.run_clip(f"tempo{bpm}", progression(["C", "G", "Am", "F"] * 4, bpm=bpm))
            got = res["tempo"]["bpm"]
            ok = any(abs(got * f - bpm) / bpm < 0.04 for f in (1, 0.5, 2))
            self.assertTrue(ok, f"expected ~{bpm} BPM, got {got:.1f}")

    def test_tuner(self):
        x = np.concatenate([tone(445.0, 2.0, 0.5, 0.3), np.zeros(SR // 2), tone(82.41, 2.0, 0.5, 0.3)])
        res = self.run_clip("tuner", x.astype(np.float32))
        text = report.render(res, "tuning")
        self.assertIn("A4", text)
        a4 = [n for n in res["tuner_notes"] if n["midi"] == 69]
        self.assertTrue(a4, text)
        self.assertAlmostEqual(a4[0]["cents"], 1200 * np.log2(445 / 440), delta=4)
        self.assertTrue(any(n["midi"] == 40 for n in res["tuner_notes"]), text)

    def test_silence(self):
        res = self.run_clip("silence", np.zeros(SR * 3, np.float32))
        self.assertEqual(res["status"], "silent")
        self.assertIn("SILENCE", report.render(res))

    @unittest.skipUnless(shutil.which("say") and shutil.which("afconvert"), "macOS say/afconvert needed")
    def test_speech_detection(self):
        aiff = os.path.join(self.tmp, "speech.aiff")
        text = ("Hello, this is a short test of the soundscape listener. It should recognise that this is "
                "spoken language rather than music, because the rhythm follows syllables and pauses.")
        subprocess.run(["say", "-o", aiff, text], check=True)
        res = analyze(aiff)
        self.assertTrue(res["content"]["label"].startswith("speech"), res["content"])

    def test_music_classified_as_music(self):
        res = self.run_clip("musicclass", progression(["G", "D", "Em", "C"] * 4))
        self.assertEqual(res["content"]["label"], "music", res["content"])

    def test_reports_render_for_every_focus(self):
        res = self.run_clip("render", progression(["D", "A", "Bm", "G"] * 4))
        for focus in report.FOCI:
            for detail in ("brief", "standard", "full"):
                text = report.render(res, focus, detail)
                self.assertTrue(text.startswith("SOUNDSCAPE"))
                self.assertLess(len(text), 9000, f"{focus}/{detail} report too long")


if __name__ == "__main__":
    unittest.main()
