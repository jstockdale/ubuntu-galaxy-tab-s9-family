"""Offline checks for ubuntu-gts9u-usb-runtime-pm.service: what it gates on,
what it is ordered after, and what its commands write, run against a fake
power/control file.  Nothing on the host is touched."""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
PKG = REPO / "packaging/ubuntu-gts9u-device"
UNIT_NAME = "ubuntu-gts9u-usb-runtime-pm.service"
UNIT = PKG / "usr/lib/systemd/system" / UNIT_NAME
CONTROL = "/sys/bus/platform/devices/a600000.usb/power/control"


def directives(text):
    """(key, value) pairs in order; systemd allows repeated keys."""
    pairs = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        key, _, value = line.partition("=")
        pairs.append((key, value))
    return pairs


class UnitFile(unittest.TestCase):
    def setUp(self):
        self.pairs = directives(UNIT.read_text())

    def values(self, key):
        return [v for k, v in self.pairs if k == key]

    def test_sm_x710_only(self):
        self.assertEqual(self.values("ConditionFirmware"),
                         ["device-tree-compatible(samsung,gts9wifi)"])

    def test_opt_out_file_and_controller_present(self):
        self.assertIn("!/etc/ubuntu-gts9u/usb-runtime-pm-off",
                      self.values("ConditionPathExists"))
        self.assertIn(CONTROL, self.values("ConditionPathExists"))

    def test_after_the_boot_panel_cycle_and_the_gadget(self):
        after = " ".join(self.values("After")).split()
        self.assertIn("ubuntu-gts9u-panel-coldboot-recover.service", after)
        self.assertIn("gts9u-usb-gadget.service", after)
        # Ordering only: the unit must not pull either of them in.
        self.assertEqual(self.values("Wants") + self.values("Requires"), [])

    def test_oneshot_kept_active_so_a_stop_reverts(self):
        self.assertEqual(self.values("Type"), ["oneshot"])
        self.assertEqual(self.values("RemainAfterExit"), ["yes"])
        self.assertTrue(self.values("ExecStop")[0].startswith("-"))

    def test_enabled_for_multi_user(self):
        self.assertEqual(self.values("WantedBy"), ["multi-user.target"])

    def test_commands_write_auto_then_on(self):
        start = self.values("ExecStart")
        stop = [v.lstrip("-") for v in self.values("ExecStop")]
        self.assertEqual(len(start), 1)
        self.assertEqual(len(stop), 1)
        with tempfile.TemporaryDirectory() as tmp:
            fake = pathlib.Path(tmp) / "control"
            fake.write_text("on\n")
            for command, expected in ((start[0], "auto"), (stop[0], "on")):
                self.assertIn(CONTROL, command)
                argv = command.replace(CONTROL, str(fake)).split(" ", 2)
                self.assertEqual(argv[:2], ["/bin/sh", "-c"])
                script = argv[2].strip("'")
                subprocess.run(["/bin/sh", "-c", script], check=True)
                self.assertEqual(fake.read_text(), expected)

    @unittest.skipUnless(shutil.which("systemd-analyze"), "no systemd-analyze")
    def test_systemd_accepts_it(self):
        r = subprocess.run(["systemd-analyze", "verify", "--man=no", str(UNIT)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(UNIT_NAME, r.stderr)


class PackageScripts(unittest.TestCase):
    def test_postinst_enables_it(self):
        postinst = (PKG / "DEBIAN/postinst").read_text()
        self.assertIn(f"\n{UNIT_NAME}:multi-user.target\n", postinst)

    def test_prerm_stops_it_on_removal(self):
        prerm = (PKG / "DEBIAN/prerm").read_text()
        remove = prerm.split("remove|deconfigure)", 1)[1].split(";;", 1)[0]
        self.assertIn(f"systemctl stop {UNIT_NAME}", remove)


if __name__ == "__main__":
    unittest.main()
