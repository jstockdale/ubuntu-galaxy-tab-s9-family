#!/usr/bin/env python3
"""Exercise libcamera-gts9u's software autofocus (SoftAf) off the tablet.

The controller ships as new files in
packaging/libcamera/patches/0009-simple-smooth-continuous-autofocus.patch.
This test takes soft_af.h and soft_af.cpp from that patch, builds them with
tests/soft_af_sim.cpp (a simulated lens and scene) and checks, frame by
frame, what an owner would see and hear:

* the lens never moves more than 24 codes in a frame (no jump, no click);
* it finds the peak, quickly, wherever it is, with noise;
* a still scene never moves it again (no periodic hunting);
* a new scene is refocused, a nearby one with a local search;
* a shake or a pan does not move it, and a flat scene ends Failed, still,
  until detail appears;
* AfModeAuto scans once per trigger;
* a lens left elsewhere, or a restarted stream, starts without a jump.

Needs a C++17 compiler (c++, g++ or clang++).  Nothing on the host is
touched beyond a temporary directory.
"""
import csv
import io
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
PATCH = REPO / "packaging/libcamera/patches/0009-simple-smooth-continuous-autofocus.patch"
SIM = REPO / "tests/soft_af_sim.cpp"
MAX_STEP = 24
FOCUSED, FAILED = 2, 3


def sharpness(position, peak, amp=1.0, sigma=70):
    """Contrast at position, as a fraction of the scene's best."""
    import math
    d = (position - peak) / sigma
    return (1 + amp * math.exp(-0.5 * d * d)) / (1 + amp)


def new_files(patch):
    """The files a git patch creates, by path: their added lines."""
    files = {}
    current = None
    for line in patch.read_text().splitlines():
        if line.startswith("diff --git "):
            current = None
        elif line.startswith("--- /dev/null"):
            current = ""
        elif line.startswith("+++ b/") and current == "":
            current = line[len("+++ b/"):]
            files[current] = []
        elif current and line.startswith("+") and not line.startswith("+++"):
            files[current].append(line[1:])
        elif current and line.startswith("\\ No newline"):
            pass
    return {path: "\n".join(lines) + "\n" for path, lines in files.items()}


class Run:
    def __init__(self, text):
        rows = list(csv.DictReader(io.StringIO(text)))
        self.lens = [int(r["lens"]) for r in rows]
        self.state = [int(r["state"]) for r in rows]
        self.commands = [(int(r["frame"]), int(r["command"]))
                         for r in rows if int(r["command"]) >= 0]

    def first(self, states, after=0):
        for frame, state in enumerate(self.state):
            if frame >= after and state in states:
                return frame
        return None

    def commands_between(self, start, end=None):
        return [c for f, c in self.commands if f >= start and (end is None or f < end)]

    def max_step(self, start_position):
        previous, worst = start_position, 0
        for _, command in self.commands:
            worst = max(worst, abs(command - previous))
            previous = command
        return worst

    def travel(self, start, end=None):
        moves = self.commands_between(start, end)
        previous = self.lens[start] if start < len(self.lens) else None
        total = 0
        for command in moves:
            if previous is not None:
                total += abs(command - previous)
            previous = command
        return total

    def reversals(self, start, end=None):
        moves = self.commands_between(start, end)
        direction, count = 0, 0
        for a, b in zip(moves, moves[1:]):
            if b == a:
                continue
            d = 1 if b > a else -1
            if direction and d != direction:
                count += 1
            direction = d
        return count


class SoftAfTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
        if not compiler:
            raise unittest.SkipTest("no C++ compiler")
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = pathlib.Path(cls.tmp.name)
        files = new_files(PATCH)
        for name in ("soft_af.h", "soft_af.cpp"):
            path = f"src/ipa/simple/{name}"
            if path not in files:
                raise AssertionError(f"{PATCH.name} does not create {path}")
            (tmp / name).write_text(files[path])
        cls.sim = tmp / "soft_af_sim"
        subprocess.run([compiler, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                        f"-I{tmp}", str(SIM), str(tmp / "soft_af.cpp"),
                        "-o", str(cls.sim)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_sim(self, **kw):
        args = [str(self.sim)] + [f"{k}={v}" for k, v in kw.items()]
        out = subprocess.run(args, check=True, capture_output=True, text=True).stdout
        return Run(out)

    def assertSharp(self, run, peak, amp=1.0, sigma=70, at_least=0.98):
        got = sharpness(run.lens[-1], peak, amp, sigma)
        self.assertGreaterEqual(
            got, at_least,
            f"lens {run.lens[-1]} for a peak at {peak}: {got:.3f} of the best sharpness")

    def assertSmooth(self, run, start=512):
        self.assertLessEqual(run.max_step(start), MAX_STEP,
                             "the lens jumped more than 24 codes in a frame")

    def test_static_peak_found_everywhere(self):
        for latency in (2, 3):
            for peak in (110, 200, 320, 450, 512, 600, 720, 850, 940):
                for seed in (1, 2, 3):
                    with self.subTest(peak=peak, seed=seed, latency=latency):
                        r = self.run_sim(scene=f"0:{peak}:1000:1.0:70", frames=600,
                                         seed=seed, latency=latency)
                        self.assertSmooth(r)
                        focused = r.first({FOCUSED})
                        self.assertIsNotNone(focused, "never focused")
                        self.assertLessEqual(focused, 150, "slower than 5 s")
                        self.assertSharp(r, peak)
                        self.assertEqual(r.commands_between(focused + 1), [],
                                         "moved again in a still scene")

    def test_still_scene_never_hunts(self):
        for noise in (0.03, 0.06):
            with self.subTest(noise=noise):
                r = self.run_sim(scene="0:420:1000:0.8:80", frames=1800, noise=noise)
                focused = r.first({FOCUSED})
                self.assertIsNotNone(focused)
                self.assertEqual(r.commands_between(focused + 1), [],
                                 "60 s of a still scene moved the lens")
                self.assertEqual(r.state[-1], FOCUSED)

    def test_new_scene_refocused(self):
        r = self.run_sim(scene="0:300:1000:1.0:70,600:650:700:1.2:70", frames=1500)
        self.assertSmooth(r)
        refocused = r.first({FOCUSED}, after=630)
        self.assertIsNotNone(refocused)
        settled = r.commands_between(600)
        self.assertTrue(settled, "did not react to the new scene")
        self.assertSharp(r, 650, amp=1.2)
        last_move = max(f for f, _ in r.commands)
        self.assertLessEqual(last_move - 600, 240, "refocus took more than 8 s")
        self.assertEqual(r.commands_between(last_move + 1), [])

    def test_nearby_scene_local_search(self):
        r = self.run_sim(scene="0:400:1000:1.0:80,600:470:1000:1.0:80", frames=1200)
        self.assertSmooth(r)
        self.assertSharp(r, 470, sigma=80)
        self.assertLess(r.travel(600), 700, "a nearby refocus swept the whole range")
        self.assertLessEqual(r.reversals(600), 4)

    def test_brief_shake_ignored(self):
        r = self.run_sim(scene="0:400:1000:1.0:70", frames=900, shake="600:610:0.5")
        focused = r.first({FOCUSED})
        self.assertLess(focused, 600)
        self.assertEqual(r.commands_between(focused + 1), [],
                         "a 0.3 s shake moved the lens")

    def test_pan_waits_for_a_still_scene(self):
        r = self.run_sim(scene="0:400:1000:1.0:70,600:600:900:1.0:70", frames=1500,
                         pan="600:690:0.4")
        self.assertSmooth(r)
        self.assertEqual(r.commands_between(600, 690), [],
                         "the lens moved while the scene was moving")
        self.assertSharp(r, 600)

    def test_flat_scene_fails_quietly(self):
        # Three bounded retries (after 2, 6 and 18 s), then stillness.
        r = self.run_sim(scene="0:400:1000:0.0:70", frames=2400, noise=0.03)
        self.assertSmooth(r)
        failed = r.first({FAILED})
        self.assertIsNotNone(failed, "a flat scene did not end Failed")
        self.assertEqual(r.commands_between(1500), [], "still searching after 50 s")
        self.assertEqual(r.state[-1], FAILED)

    def test_failed_then_textured_scene(self):
        # Pointing at a subject from a blank wall adds contrast, even
        # out of focus: that is a scene change.
        r = self.run_sim(scene="0:400:1000:0.0:70,600:550:1300:1.5:70", frames=1500)
        self.assertSmooth(r)
        self.assertEqual(r.state[599], FAILED, "a featureless scene did not fail")
        refocused = r.first({FOCUSED}, after=600)
        self.assertIsNotNone(refocused, "a scene with detail after a failure never focused")
        self.assertLessEqual(refocused - 600, 300, "slower than 10 s")
        self.assertSharp(r, 550, amp=1.5)

    def test_failed_retries_find_a_silent_subject(self):
        # A subject whose blur looks exactly as flat as the wall did: only
        # the bounded retries (2, 6 and 18 s after each failure) find it.
        r = self.run_sim(scene="0:400:1000:0.0:70,600:550:1000:1.5:70", frames=1800)
        refocused = r.first({FOCUSED}, after=600)
        self.assertIsNotNone(refocused)
        self.assertLessEqual(refocused, 1500, "not found within 50 s")
        self.assertSharp(r, 550, amp=1.5)

    def test_auto_mode_scans_once_per_trigger(self):
        r = self.run_sim(scene="0:300:1000:1.0:70,600:700:1000:1.0:70", frames=1200,
                         mode="auto", trigger=30)
        self.assertEqual(r.commands_between(0, 30), [], "moved before the trigger")
        self.assertGreaterEqual(sharpness(r.lens[599], 300), 0.98)
        self.assertEqual(r.commands_between(600), [], "auto mode refocused by itself")

    def test_lens_left_elsewhere_starts_without_a_jump(self):
        for start in (0, 900, 1023):
            with self.subTest(start=start):
                r = self.run_sim(scene="0:400:1000:1.0:70", frames=600, start=start)
                self.assertTrue(r.commands)
                self.assertLessEqual(abs(r.commands[0][1] - start), MAX_STEP)
                self.assertSmooth(r, start=start)
                self.assertSharp(r, 400)

    def test_reconfigured_stream_keeps_focus(self):
        # A photo, or a recording starting, reconfigures the stream; the scene
        # is the same, so the lens must not move.
        for restart in (400, 700):
            with self.subTest(restart=restart):
                r = self.run_sim(scene="0:420:1000:1.0:70", frames=1200, restart=restart)
                focused = r.first({FOCUSED})
                self.assertLess(focused, restart)
                self.assertEqual(r.commands_between(focused + 1), [],
                                 "a reconfiguration restarted the search")
                self.assertSharp(r, 420)

    def test_restarted_stream(self):
        r = self.run_sim(scene="0:400:1000:1.0:70,500:650:1000:1.0:70", frames=1200,
                         restart=500)
        self.assertSmooth(r)
        self.assertSharp(r, 650)

    def test_noisy_scene(self):
        for seed in (1, 2, 3, 4):
            with self.subTest(seed=seed):
                r = self.run_sim(scene="0:560:1000:0.8:80", frames=900, noise=0.08,
                                 seed=seed)
                self.assertSmooth(r)
                self.assertSharp(r, 560, amp=0.8, sigma=80, at_least=0.95)
                focused = r.first({FOCUSED, FAILED})
                self.assertEqual(len(r.commands_between(focused + 1)), 0)

    def test_patch_wires_it_into_the_ipa(self):
        text = PATCH.read_text()
        self.assertIn("+    'soft_af.cpp',", text)
        self.assertIn("af_.process(frame", text)
        for gone in ("afLockedStats_ >= 45", "AfPhase::Probe"):
            self.assertNotIn("+" + gone, text)


if __name__ == "__main__":
    unittest.main()
