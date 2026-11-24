"""The sensors' system-sleep hook of the device package, offline.

/usr/lib/systemd/system-sleep/ubuntu-gts9u-sensors runs with a stand-in
systemctl that records its calls (and can fail, or hang in a stop).  Before
a sleep it has to stop the Sensor Core's clients, iio-sensor-proxy first and
then the sensor protection domain's file server, synchronously, so that no
sensor stream wakes the audio DSP through the sleep (2026-11-23: about 1,500
of its wakes in a 30 s suspend with them running, none with both stopped);
after it, the resume unit that brings both back, its failed state reset
first.  Every call waits a bounded time, nothing fails the sleep, and the
clients are stopped only where the resume unit can run.  Both boards, no
board check.  The resume side is checked as text: ubuntu-gts9u-sensors-resume
starts the file server and restarts the proxy.  Nothing on the host is
touched.
"""
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
PKG = REPO / "packaging/ubuntu-gts9u-device"
HOOK = PKG / "usr/lib/systemd/system-sleep/ubuntu-gts9u-sensors"
RESUME = PKG / "usr/libexec/ubuntu-gts9u-sensors-resume"
RESUME_UNIT = PKG / "usr/lib/systemd/system/ubuntu-gts9u-sensors-resume.service"
KIT_SLEEP_TEST = REPO / "tools/inplace/typec-roles/sleep-test.sh"

GATE = "show -p LoadState --value ubuntu-gts9u-sensors-resume.service"
STOPS = ["stop ubuntu-gts9u-sensors-resume.service", "stop iio-sensor-proxy.service",
         "stop hexagonrpcd-adsp-sensorspd.service"]
POST = ["reset-failed ubuntu-gts9u-sensors-resume.service",
        "restart --no-block ubuntu-gts9u-sensors-resume.service"]

# The stand-in: every call recorded; "show" prints $STATE/loadstate (loaded
# when absent); $STATE/fail fails every other call; $STATE/hang-UNIT makes
# "stop UNIT" hang the way a stop job that does not end would.
SYSTEMCTL = """#!/bin/sh
echo "$*" >> "$STATE/calls"
case "$1" in
show) cat "$STATE/loadstate" 2>/dev/null || echo loaded; exit 0 ;;
esac
[ ! -e "$STATE/fail" ] || exit 1
case "$1" in
stop) [ ! -e "$STATE/hang-$2" ] || exec sleep 30 ;;
esac
exit 0
"""


class SensorsHookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "systemctl").write_text(SYSTEMCTL)
        (self.bin / "systemctl").chmod(0o755)
        self.ssccli = self.tmp / "ssccli"
        # The hook as shipped, with ssccli where the test puts it.
        text = HOOK.read_text()
        self.assertEqual(text.count("/usr/bin/ssccli"), 1)
        self.hook = self.tmp / "hook"
        self.hook.write_text(text.replace("/usr/bin/ssccli", str(self.ssccli)))

    def with_ssccli(self):
        self.ssccli.write_text("")
        self.ssccli.chmod(0o755)

    def run_hook(self, *args):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "STATE": str(self.tmp)}
        return subprocess.run(["sh", str(self.hook), *args], env=env, capture_output=True, text=True, timeout=60)

    def recorded(self):
        calls = self.tmp / "calls"
        return calls.read_text().splitlines() if calls.exists() else []

    def reset(self):
        (self.tmp / "calls").unlink(missing_ok=True)

    def test_pre_stops_the_resume_unit_then_the_proxy_then_the_file_server(self):
        self.with_ssccli()
        for kind in ("suspend", "hibernate", "hybrid-sleep", "suspend-then-hibernate"):
            with self.subTest(kind=kind):
                self.reset()
                result = self.run_hook("pre", kind)
                self.assertEqual(result.returncode, 0, result.stderr)
                # Each one synchronously: no --no-block before a sleep.
                self.assertEqual(self.recorded(), [GATE] + STOPS)
                self.assertEqual(result.stderr, "")

    def test_post_resets_and_restarts_the_resume_unit_without_waiting_for_it(self):
        # The reset clears the unit's start limit (and its failed state), so
        # that quick sleeps in a row cannot leave the restart refused.
        self.with_ssccli()
        for kind in ("suspend", "hibernate", "hybrid-sleep", "suspend-then-hibernate"):
            with self.subTest(kind=kind):
                self.reset()
                result = self.run_hook("post", kind)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.recorded(), [GATE] + POST)

    def test_without_ssccli_or_a_resume_unit_the_clients_are_left_as_they_are(self):
        # ubuntu-gts9u-sensors-resume.service has ConditionPathExists=ssccli:
        # without it, or without the unit, nothing would start the proxy again
        # after the sleep.
        self.assertIn("ConditionPathExists=/usr/bin/ssccli", RESUME_UNIT.read_text())
        for ssccli, loadstate in ((False, "loaded"), (True, "masked"), (True, "not-found")):
            with self.subTest(ssccli=ssccli, loadstate=loadstate):
                self.reset()
                self.ssccli.unlink(missing_ok=True)
                if ssccli:
                    self.with_ssccli()
                (self.tmp / "loadstate").write_text(loadstate + "\n")
                pre = self.run_hook("pre", "suspend")
                self.assertEqual(pre.returncode, 0, pre.stderr)
                self.assertIn("ubuntu-gts9u-sensors: ubuntu-gts9u-sensors-resume.service cannot run here", pre.stderr)
                post = self.run_hook("post", "suspend")
                self.assertEqual(post.returncode, 0, post.stderr)
                self.assertEqual(post.stderr, "")
                # Only the look at the unit (where ssccli is there): no stop,
                # no reset, no restart.
                self.assertEqual(self.recorded(), [GATE, GATE] if ssccli else [])

    def test_a_failing_systemctl_or_another_argument_never_fails_the_sleep(self):
        self.with_ssccli()
        (self.tmp / "fail").write_text("")
        for args, calls in ((("pre", "suspend"), STOPS), (("post", "suspend"), POST)):
            with self.subTest(args=args):
                self.reset()
                result = self.run_hook(*args)
                self.assertEqual(result.returncode, 0)
                # Every step is tried all the same, and each failure said.
                self.assertEqual(self.recorded(), [GATE] + calls)
                for call in calls:
                    self.assertIn(f"ubuntu-gts9u-sensors: systemctl {call}: status 1\n", result.stderr)
        self.reset()
        for args in ((), ("pre-suspend",), ("",)):
            with self.subTest(args=args):
                self.assertEqual(self.run_hook(*args).returncode, 0)
        self.assertEqual(self.recorded(), [])

    def test_a_stop_that_hangs_holds_the_sleep_for_the_bound_only(self):
        # A stop job that does not end (a client stuck in the kernel) must not
        # keep the tablet awake: the hook waits STOP_WAIT seconds for each
        # call, says so, and goes on with the next stop.
        text = self.hook.read_text()
        self.assertEqual(text.count("\nSTOP_WAIT=10\n"), 1)
        self.hook.write_text(text.replace("\nSTOP_WAIT=10\n", "\nSTOP_WAIT=1\n"))
        self.with_ssccli()
        (self.tmp / "hang-iio-sensor-proxy.service").write_text("")
        start = time.monotonic()
        result = self.run_hook("pre", "suspend")
        elapsed = time.monotonic() - start
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(elapsed, 8)
        self.assertEqual(self.recorded(), [GATE] + STOPS)
        self.assertIn("ubuntu-gts9u-sensors: systemctl stop iio-sensor-proxy.service: still running after 1s; "
                      "going on, systemd finishes it\n", result.stderr)

    def test_every_systemctl_call_is_bounded(self):
        text = HOOK.read_text()
        calls = [line.strip() for line in text.splitlines()
                 if "systemctl " in line and not line.lstrip().startswith("#")]
        self.assertTrue(calls)
        for line in calls:
            with self.subTest(line=line):
                self.assertTrue(line.startswith("bounded systemctl ") or "timeout -k 2 \"$STOP_WAIT\" systemctl" in line,
                                line)

    def test_the_hook_is_for_both_boards_and_parses(self):
        text = HOOK.read_text()
        self.assertNotIn("device-tree", text)
        self.assertNotIn("gts9wifi", text.replace("SM-X710", ""))
        self.assertEqual(subprocess.run(["sh", "-n", str(HOOK)]).returncode, 0)

    def test_the_resume_side_brings_the_file_server_and_a_fresh_proxy_back(self):
        resume = RESUME.read_text()
        self.assertIn("systemctl start hexagonrpcd-adsp-sensorspd.service", resume)
        self.assertIn("systemctl start iio-sensor-proxy.service", resume)
        self.assertIn("systemctl reset-failed iio-sensor-proxy.service", resume)

    def test_the_sleep_test_runs_this_hook_s_pre_step_and_knows_it_by_the_proxy(self):
        # sensors=hook refuses a hook that does not stop iio-sensor-proxy (the
        # earlier one), by this line; the Type-C roles kit's revisions 12 and
        # 13 carry that check, so the text stays.
        kit = KIT_SLEEP_TEST.read_text()
        self.assertIn("sensors_hook=/usr/lib/systemd/system-sleep/ubuntu-gts9u-sensors", kit)
        self.assertIn("grep -q 'systemctl stop iio-sensor-proxy.service' \"$sensors_hook\"", kit)
        self.assertIn('sh "$sensors_hook" pre suspend', kit)
        self.assertIn("systemctl stop iio-sensor-proxy.service", HOOK.read_text())


if __name__ == "__main__":
    unittest.main()
