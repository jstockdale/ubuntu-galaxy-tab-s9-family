"""The sensors' system-sleep hook of the device package, offline.

/usr/lib/systemd/system-sleep/ubuntu-gts9u-sensors runs with a stand-in
systemctl that records its calls (and can fail).  Before a sleep it has to
stop the Sensor Core's clients, iio-sensor-proxy first and then the sensor
protection domain's file server, synchronously, so that no sensor stream
wakes the audio DSP through the sleep (2026-11-23: about 1,500 of its wakes
in a 30 s suspend with them running, none with both stopped); after it, the
resume unit that brings both back.  Both boards, no board check.  The
resume side is checked as text: ubuntu-gts9u-sensors-resume starts the file
server and restarts the proxy, and the hook stops the proxy only where that
unit can run.  Nothing on the host is touched.
"""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
PKG = REPO / "packaging/ubuntu-gts9u-device"
HOOK = PKG / "usr/lib/systemd/system-sleep/ubuntu-gts9u-sensors"
RESUME = PKG / "usr/libexec/ubuntu-gts9u-sensors-resume"
RESUME_UNIT = PKG / "usr/lib/systemd/system/ubuntu-gts9u-sensors-resume.service"
KIT_SLEEP_TEST = REPO / "tools/inplace/typec-roles/sleep-test.sh"

STOPS = ["stop ubuntu-gts9u-sensors-resume.service", "stop iio-sensor-proxy.service",
         "stop hexagonrpcd-adsp-sensorspd.service"]


class SensorsHookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.calls = self.tmp / "calls"
        (self.bin / "systemctl").write_text(
            "#!/bin/sh\necho \"$*\" >> \"$CALLS\"\n[ ! -e \"$FAIL\" ] || exit 1\nexit 0\n")
        (self.bin / "systemctl").chmod(0o755)
        self.ssccli = self.tmp / "ssccli"
        # The hook as shipped, with ssccli where the test puts it.
        text = HOOK.read_text()
        self.assertEqual(text.count("/usr/bin/ssccli"), 1)
        self.hook = self.tmp / "hook"
        self.hook.write_text(text.replace("/usr/bin/ssccli", str(self.ssccli)))

    def run_hook(self, *args):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "CALLS": str(self.calls), "FAIL": str(self.tmp / "fail")}
        return subprocess.run(["sh", str(self.hook), *args], env=env, capture_output=True, text=True, timeout=30)

    def recorded(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_pre_stops_the_resume_unit_then_the_proxy_then_the_file_server(self):
        self.ssccli.write_text("")
        self.ssccli.chmod(0o755)
        for kind in ("suspend", "hibernate", "hybrid-sleep", "suspend-then-hibernate"):
            with self.subTest(kind=kind):
                self.calls.unlink(missing_ok=True)
                result = self.run_hook("pre", kind)
                self.assertEqual(result.returncode, 0, result.stderr)
                # Each one synchronously: no --no-block before a sleep.
                self.assertEqual(self.recorded(), STOPS)

    def test_post_restarts_the_resume_unit_without_waiting_for_it(self):
        self.ssccli.write_text("")
        self.ssccli.chmod(0o755)
        result = self.run_hook("post", "suspend")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.recorded(), ["restart --no-block ubuntu-gts9u-sensors-resume.service"])

    def test_without_ssccli_the_clients_are_left_as_the_resume_could_not_bring_them_back(self):
        # ubuntu-gts9u-sensors-resume.service has ConditionPathExists=ssccli:
        # without it nothing would start the proxy again after the sleep.
        result = self.run_hook("pre", "suspend")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.recorded(), STOPS[:1])
        self.assertIn("ConditionPathExists=/usr/bin/ssccli", RESUME_UNIT.read_text())

    def test_a_failing_systemctl_or_another_argument_never_fails_the_sleep(self):
        self.ssccli.write_text("")
        self.ssccli.chmod(0o755)
        (self.tmp / "fail").write_text("")
        for args in (("pre", "suspend"), ("post", "suspend")):
            with self.subTest(args=args):
                self.calls.unlink(missing_ok=True)
                self.assertEqual(self.run_hook(*args).returncode, 0)
        self.calls.unlink(missing_ok=True)
        for args in ((), ("pre-suspend",), ("",)):
            with self.subTest(args=args):
                self.assertEqual(self.run_hook(*args).returncode, 0)
        self.assertEqual(self.recorded(), [])

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
        # earlier one), by this line.
        kit = KIT_SLEEP_TEST.read_text()
        self.assertIn("sensors_hook=/usr/lib/systemd/system-sleep/ubuntu-gts9u-sensors", kit)
        self.assertIn("grep -q 'systemctl stop iio-sensor-proxy.service' \"$sensors_hook\"", kit)
        self.assertIn('sh "$sensors_hook" pre suspend', kit)
        self.assertIn("systemctl stop iio-sensor-proxy.service", HOOK.read_text())


if __name__ == "__main__":
    unittest.main()
