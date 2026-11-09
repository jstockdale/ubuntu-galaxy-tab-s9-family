"""The sleep hook that leaves USB host role so the xHCI can suspend.

Runs the real hook against a fake sysfs (GTS9U_SYSFS) and run directory
(GTS9U_RUN); nothing on the host is touched."""

from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "packaging/ubuntu-gts9u-device/usr/lib/systemd/system-sleep/ubuntu-gts9u-usb-role"


class UsbRoleSleepHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sys = Path(self.tmp.name) / "sys"
        self.run_dir = Path(self.tmp.name) / "run"
        self.role = self.sys / "class/usb_role/a600000.usb-role-switch/role"
        self.role.parent.mkdir(parents=True)
        (self.sys / "bus/usb/devices").mkdir(parents=True)
        (self.sys / "class/typec/port0").mkdir(parents=True)

    def device(self, name, device_class, interfaces=()):
        d = self.sys / "bus/usb/devices" / name
        d.mkdir()
        (d / "bDeviceClass").write_text(device_class + "\n")
        for number, interface_class in enumerate(interfaces):
            i = self.sys / "bus/usb/devices" / f"{name}:1.{number}"
            i.mkdir()
            (i / "bInterfaceClass").write_text(interface_class + "\n")

    def partner(self):
        (self.sys / "class/typec/port0-partner").mkdir()

    def hook(self, phase):
        env = dict(os.environ, GTS9U_SYSFS=str(self.sys), GTS9U_RUN=str(self.run_dir))
        subprocess.run(["sh", str(HOOK), phase, "suspend"], env=env, check=True, timeout=10)
        return self.role.read_text().strip()

    def belkin_hub(self):
        # What blastoise showed on its Belkin USB-C hub: the hub chip and
        # the hub's Billboard device, nothing plugged into the ports.
        self.device("1-1", "09", ["09"])
        self.device("1-1.5", "11", ["11"])

    def test_hub_only_dock_leaves_host_for_the_sleep_and_returns(self):
        self.role.write_text("host\n")
        self.belkin_hub()
        self.partner()
        self.assertEqual(self.hook("pre"), "device")
        self.assertTrue((self.run_dir / "usb-role-before-sleep").exists())
        self.assertEqual(self.hook("post"), "host")
        self.assertFalse((self.run_dir / "usb-role-before-sleep").exists())

    def test_charger_with_nothing_enumerated_leaves_host_too(self):
        self.role.write_text("host\n")
        self.partner()
        self.assertEqual(self.hook("pre"), "device")
        self.assertEqual(self.hook("post"), "host")

    def test_a_keyboard_behind_the_hub_keeps_host(self):
        self.role.write_text("host\n")
        self.belkin_hub()
        self.device("1-1.2", "00", ["03"])
        self.partner()
        self.assertEqual(self.hook("pre"), "host")
        self.assertFalse((self.run_dir / "usb-role-before-sleep").exists())

    def test_a_drive_keeps_host(self):
        self.role.write_text("host\n")
        self.device("1-1", "00", ["08"])
        self.assertEqual(self.hook("pre"), "host")

    def test_device_role_is_left_alone(self):
        self.role.write_text("device\n")
        self.assertEqual(self.hook("pre"), "device")
        self.assertEqual(self.hook("post"), "device")

    def test_unplugged_during_sleep_is_not_forced_back_to_host(self):
        self.role.write_text("host\n")
        self.belkin_hub()
        self.assertEqual(self.hook("pre"), "device")
        # No port0-partner: the hub went away while the tablet slept.
        self.assertEqual(self.hook("post"), "device")
        self.assertFalse((self.run_dir / "usb-role-before-sleep").exists())

    def test_post_without_a_switch_does_nothing(self):
        self.role.write_text("host\n")
        self.partner()
        self.assertEqual(self.hook("post"), "host")

    def resume_with_klog(self, text):
        klog = Path(self.tmp.name) / "klog"
        klog.write_text(text)
        trace = Path(self.tmp.name) / "trace"
        env = dict(os.environ, GTS9U_SYSFS=str(self.sys), GTS9U_RUN=str(self.run_dir),
                   GTS9U_KLOG=str(klog), GTS9U_INLINE="1", GTS9U_TRIES="3",
                   GTS9U_TRACE=str(trace))
        subprocess.run(["sh", str(HOOK), "post", "suspend"], env=env, check=True, timeout=20)
        return trace.read_text().split() if trace.exists() else []

    def test_a_dead_xhc_after_resume_is_reset_with_the_devices_attached(self):
        # blastoise, 2026-11-08: Beats Studio Pro behind the Belkin hub; the
        # tablet slept, and the xHC came back with a Host System Error.
        self.role.write_text("host\n")
        self.belkin_hub()
        self.device("1-1.1", "00", ["01", "01", "01", "03"])
        self.partner()
        self.assertEqual(self.hook("pre"), "host")
        self.assertTrue((self.run_dir / "usb-sleep-start").exists())
        trace = self.resume_with_klog(
            "xhci-hcd xhci-hcd.2.auto: WARNING: Host System Error\n"
            "usb usb1-port1: couldn't allocate usb_device\n")
        self.assertEqual(trace, ["device", "host"])
        self.assertEqual(self.role.read_text().strip(), "host")
        self.assertFalse((self.run_dir / "usb-sleep-start").exists())

    def test_a_clean_resume_with_devices_attached_is_left_alone(self):
        self.role.write_text("host\n")
        self.device("1-1", "00", ["03"])
        self.partner()
        self.hook("pre")
        self.assertEqual(self.resume_with_klog("PM: suspend exit\n"), [])
        self.assertEqual(self.role.read_text().strip(), "host")

    def test_no_role_switch_at_all(self):
        self.role.unlink(missing_ok=True)
        env = dict(os.environ, GTS9U_SYSFS=str(self.sys), GTS9U_RUN=str(self.run_dir))
        result = subprocess.run(["sh", str(HOOK), "pre", "suspend"], env=env, timeout=10)
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
