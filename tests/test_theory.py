"""Ear-training and modal-centre tests.  Run:  ~/.soundscape/venv/bin/python -m unittest discover tests"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from soundscape_engine import report  # noqa: E402
from soundscape_engine.analysis import analyze  # noqa: E402
from synth import chord, progression, sequence, write_wav  # noqa: E402

C3, C4 = 48, 60


class TheoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="soundscape-theory-")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_clip(self, name, x):
        path = os.path.join(self.tmp, name + ".wav")
        write_wav(path, x)
        return analyze(path)

    def first_chord(self, res):
        chords = [e["id"] for e in res["theory"]["events"] if e["id"]["kind"] == "chord"]
        self.assertTrue(chords, report.render(res, "theory"))
        return chords[0]

    def test_chord_qualities_root_position(self):
        cases = [
            ("major triad", [0, 4, 7], 2), ("minor triad", [0, 3, 7], 9), ("diminished triad", [0, 3, 6], 11),
            ("augmented triad", [0, 4, 8], 5), ("dominant seventh", [0, 4, 7, 10], 7),
            ("major seventh", [0, 4, 7, 11], 5), ("minor seventh", [0, 3, 7, 10], 2),
            ("half-diminished seventh", [0, 3, 6, 10], 11), ("fully diminished seventh", [0, 3, 6, 9], 1),
            ("minor-major seventh", [0, 3, 7, 11], 0),
        ]
        for i, (name, ivs, root) in enumerate(cases):
            with self.subTest(name):
                res = self.run_clip(f"q{i}", chord([C3 + root + v for v in ivs], seed=i))
                ch = self.first_chord(res)
                self.assertTrue(ch["name"].startswith(name), f"{name}: got {ch['symbol']} {ch['name']}")
                self.assertEqual(ch["root"], root % 12)
                self.assertEqual(ch["inversion"], 0)

    def test_inversions_and_figured_bass(self):
        cases = [
            ("C first inversion", [52, 55, 60], "major triad", 1, "6"),            # E3 G3 C4
            ("F second inversion", [48, 53, 57], "major triad", 2, "6/4"),          # C3 F3 A3
            ("G7 second inversion", [50, 53, 55, 59], "dominant seventh", 2, "4/3"),  # D3 F3 G3 B3
            ("Bø7 third inversion", [45, 47, 50, 53], "half-diminished", 3, "4/2"),   # A2 B2 D3 F3
            ("D7 first inversion", [54, 57, 60, 62], "dominant seventh", 1, "6/5"),   # F#3 A3 C4 D4
        ]
        for i, (label, notes, name, inv, fig) in enumerate(cases):
            with self.subTest(label):
                ch = self.first_chord(self.run_clip(f"inv{i}", chord(notes, seed=10 + i)))
                self.assertTrue(ch["name"].startswith(name), f"{label}: {ch['symbol']} {ch['name']}")
                self.assertEqual(ch["inversion"], inv, label)
                self.assertTrue(ch["figure"].startswith(fig), f"{label}: {ch['figure']}")

    def test_spelling(self):
        ch = self.first_chord(self.run_clip("spell", chord([56, 59, 62, 65])))  # G#3 B3 D4 F4
        self.assertEqual(ch["tones"], ["G#", "B", "D", "F"])
        self.assertIn("°7", ch["symbol"])

    def test_harmonic_intervals(self):
        for i, (notes, expect) in enumerate([([C4, C4 + 7], "P5"), ([62, 65], "m3"), ([65, 71], "A4/d5"),
                                             ([C4, C4 + 9], "M6")]):
            with self.subTest(expect):
                res = self.run_clip(f"int{i}", chord(notes, seed=20 + i))
                ev = res["theory"]["events"][0]["id"]
                self.assertEqual(ev["kind"], "interval", report.render(res, "theory"))
                self.assertTrue(ev["name"].startswith(expect), ev["name"])

    def test_melodic_interval_and_scale(self):
        res = self.run_clip("melint", sequence([[C4], [C4 + 9]], dur=0.9))
        self.assertIn("↑M6", report.render(res, "theory"))
        dorian = [62, 64, 65, 67, 69, 71, 72, 74]
        res = self.run_clip("dorian", sequence([[m] for m in dorian], dur=0.35))
        sc = res["theory"]["scale"]
        self.assertIsNotNone(sc, report.render(res, "theory"))
        self.assertEqual((sc["tonic"], sc["name"], sc["direction"]), (2, "Dorian", "ascending"))
        harm = [57, 59, 60, 62, 64, 65, 68, 69]
        sc = self.run_clip("harm", sequence([[m] for m in harm], dur=0.35))["theory"]["scale"]
        self.assertEqual((sc["tonic"], sc["name"]), (9, "harmonic minor"))

    def test_cadences(self):
        F, G7, C = [41, 57, 60, 65], [43, 59, 62, 65], [36, 55, 64, 72]     # bass + upper voices, C5 on top
        res = self.run_clip("pac", sequence([[36, 52, 55, 60], F, G7, C], dur=1.0))
        self.assertIn("perfect authentic", res["theory"]["cadence"] or "", report.render(res, "theory"))
        res = self.run_clip("hc", sequence([[36, 52, 55, 60], F, [43, 59, 62, 67]], dur=1.0))
        self.assertIn("half cadence", res["theory"]["cadence"] or "", report.render(res, "theory"))

    def test_modal_vamps(self):
        cases = [("G# Phrygian", ["G#m7", "Amaj7"] * 8 + ["G#m7"]),
                 ("D Dorian", ["Dm7", "G7"] * 8 + ["Dm7"]),
                 ("G Mixolydian", ["G", "F", "C", "G"] * 4)]
        for name, chords in cases:
            with self.subTest(name):
                res = self.run_clip(name.replace(" ", ""), progression(chords, bpm=110))
                self.assertEqual(report.centre_label(res), name, report.render(res, "key"))

    def test_major_and_minor_stay_plain(self):
        for name, chords in [("C major", ["C", "G", "Am", "F"] * 4), ("A minor", ["Am", "F", "C", "G"] * 3 + ["Am"])]:
            with self.subTest(name):
                res = self.run_clip(name.replace(" ", ""), progression(chords))
                self.assertEqual(report.centre_label(res), name, report.render(res, "key"))


if __name__ == "__main__":
    unittest.main()
