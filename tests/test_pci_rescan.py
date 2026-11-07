"""Run ubuntu-gts9u-pci-rescan against a fake PCI sysfs.  Writes to the fake
remove/rescan/drivers_probe files only take effect when the script sleeps: the
`sleep` stub plays the kernel, so each scenario decides what a re-probe or a
rescan achieves.  Nothing on the host is touched."""
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "packaging/ubuntu-gts9u-device/usr/libexec/ubuntu-gts9u-pci-rescan"
UNIT = REPO / "packaging/ubuntu-gts9u-device/usr/lib/systemd/system/gts9u-pci-rescan.service"
POSTINST = REPO / "packaging/ubuntu-gts9u-device/DEBIAN/postinst"
OVERLAY = REPO / "scripts/build-rootfs-overlay.sh"

EP = "sys/bus/pci/devices/0000:01:00.0"
RP = "sys/bus/pci/devices/0000:00:00.0"
SUBSTATES = ("l1_1_aspm", "l1_2_aspm", "l1_1_pcipm", "l1_2_pcipm")

# The kernel, as far as this script can tell.  Scenario switches are files in
# $FAKE_ROOT: reprobe-binds, rescan-binds.
STUB_SLEEP = r"""#!/bin/sh
# The kernel has every tool; the script under test gets only TOOLS.
PATH=/usr/bin:/bin
r=$FAKE_ROOT
ep=$r/sys/bus/pci/devices/0000:01:00.0
rp=$r/sys/bus/pci/devices/0000:00:00.0
echo "sleep $1" >> "$r/calls.log"
endpoint() {  # create the endpoint as a fresh enumeration does
	mkdir -p "$ep/link" "$ep/net"
	: > "$ep/remove"
	for a in l0s_aspm l1_aspm; do echo 1 > "$ep/link/$a"; done
	for a in l1_1_aspm l1_2_aspm l1_1_pcipm l1_2_pcipm; do echo 0 > "$ep/link/$a"; done
	if [ -e "$r/rescan-binds" ]; then
		ln -s "$r/sys/bus/pci/drivers/ath11k_pci" "$ep/driver"
		mkdir -p "$ep/net/wlp1s0"
	fi
}
if [ "$(cat "$r/sys/bus/pci/drivers_probe" 2>/dev/null)" = 0000:01:00.0 ]; then
	: > "$r/sys/bus/pci/drivers_probe"
	echo reprobe >> "$r/calls.log"
	if [ -e "$r/reprobe-binds" ]; then
		ln -s "$r/sys/bus/pci/drivers/ath11k_pci" "$ep/driver"
		mkdir -p "$ep/net/wlp1s0"
	fi
fi
if [ "$(cat "$ep/remove" 2>/dev/null)" = 1 ]; then
	rm -rf "$ep"; echo remove-ep >> "$r/calls.log"
fi
if [ "$(cat "$rp/remove" 2>/dev/null)" = 1 ]; then
	rm -rf "$rp"; echo remove-rp >> "$r/calls.log"
fi
if [ "$(cat "$r/sys/bus/pci/rescan" 2>/dev/null)" = 1 ]; then
	: > "$r/sys/bus/pci/rescan"
	echo bus-rescan >> "$r/calls.log"
	mkdir -p "$rp"; : > "$rp/remove"; : > "$rp/rescan"
	endpoint
fi
if [ "$(cat "$rp/rescan" 2>/dev/null)" = 1 ]; then
	: > "$rp/rescan"
	echo rp-rescan >> "$r/calls.log"
	endpoint
fi
exit 0
"""

TOOLS = ("cat", "tr", "ls", "readlink", "basename")


class PciRescanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        for directory in ("bin", "tools", "etc/ubuntu-gts9u", RP,
                          "sys/bus/pci/drivers/ath11k_pci"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        for name in ("sys/bus/pci/rescan", "sys/bus/pci/drivers_probe",
                     RP + "/remove", RP + "/rescan"):
            (self.root / name).write_text("")
        for tool in TOOLS:
            os.symlink(shutil.which(tool), self.root / "tools" / tool)
        path = self.root / "bin/sleep"
        path.write_text(STUB_SLEEP)
        path.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def endpoint(self, bound, substates="1"):
        ep = self.root / EP
        (ep / "link").mkdir(parents=True)
        (ep / "net").mkdir()
        (ep / "remove").write_text("")
        for a in ("l0s_aspm", "l1_aspm"):
            (ep / "link" / a).write_text("1\n")
        for a in SUBSTATES:
            (ep / "link" / a).write_text(substates + "\n")
        if bound:
            os.symlink(self.root / "sys/bus/pci/drivers/ath11k_pci", ep / "driver")
            (ep / "net/wlp1s0").mkdir()

    def scenario(self, *switches):
        for switch in switches:
            (self.root / switch).touch()

    def policy(self, text):
        (self.root / "etc/ubuntu-gts9u/pcie-l1ss").write_text(text)

    def run_script(self):
        text = re.sub(r"/sys/bus/pci|/etc/ubuntu-gts9u",
                      lambda match: str(self.root) + match.group(0), SCRIPT.read_text())
        env = dict(os.environ, FAKE_ROOT=str(self.root),
                   PATH="%s:%s" % (self.root / "bin", self.root / "tools"))
        result = subprocess.run(["/bin/sh", "-s"], input=text, env=env, text=True,
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def calls(self):
        log = self.root / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def substates(self):
        return {a: (self.root / EP / "link" / a).read_text().strip() for a in SUBSTATES}

    # --- bound at boot: the vendor_boot firmware did its job ------------
    def test_bound_at_boot_is_a_no_op(self):
        self.endpoint(bound=True)
        result = self.run_script()
        self.assertIn("bound to ath11k_pci at boot; nothing to rescan", result.stderr)
        self.assertNotIn("sleep", " ".join(self.calls()))
        self.assertEqual(set(self.substates().values()), {"1"})
        self.assertIn("link: l0s_aspm=1 l1_aspm=1 l1_1_aspm=1", result.stderr)

    def test_bound_at_boot_keeps_whatever_the_kernel_chose(self):
        self.endpoint(bound=True, substates="0")
        self.run_script()
        self.assertEqual(set(self.substates().values()), {"0"})

    # --- probe failed for want of firmware: re-probe in place -----------------
    def test_reprobe_keeps_the_device_and_its_substates(self):
        self.endpoint(bound=False)
        self.scenario("reprobe-binds")
        result = self.run_script()
        self.assertIn("reprobe", self.calls())
        self.assertNotIn("remove-ep", self.calls())
        self.assertNotIn("remove-rp", self.calls())
        self.assertIn("re-probe bound ath11k_pci", result.stderr)
        # Not re-created, so host init's L1 substates are still there and
        # nothing writes them.
        self.assertEqual(set(self.substates().values()), {"1"})
        self.assertNotIn("L1 substates set", result.stderr)

    # --- re-probe fails: re-create, then re-enable the substates ------------------
    def test_recreated_endpoint_gets_its_substates_back(self):
        self.endpoint(bound=False)
        self.scenario("rescan-binds")
        result = self.run_script()
        calls = self.calls()
        self.assertIn("reprobe", calls)
        self.assertLess(calls.index("remove-ep"), calls.index("remove-rp"))
        self.assertLess(calls.index("remove-rp"), calls.index("bus-rescan"))
        self.assertIn("endpoint bound to ath11k_pci after a rescan", result.stderr)
        self.assertEqual(set(self.substates().values()), {"1"})
        self.assertIn("L1 substates set to 1 (policy auto)", result.stderr)

    def test_late_link_rescans_the_root_port(self):
        self.scenario("rescan-binds")
        result = self.run_script()
        self.assertNotIn("reprobe", self.calls())
        self.assertIn("rp-rescan", self.calls())
        self.assertIn("no endpoint; rescanning the root port", result.stderr)
        self.assertEqual(set(self.substates().values()), {"1"})

    def test_root_port_gone_falls_back_to_the_bus_rescan(self):
        shutil.rmtree(self.root / RP)
        self.scenario("rescan-binds")
        self.run_script()
        self.assertIn("bus-rescan", self.calls())
        self.assertEqual(set(self.substates().values()), {"1"})

    def test_never_binds(self):
        self.endpoint(bound=False)
        result = self.run_script()
        self.assertEqual(sum(1 for line in result.stderr.splitlines() if ": attempt " in line), 4)
        self.assertIn("no driver-bound PCIe endpoint after rescans", result.stderr)

    # --- /etc/ubuntu-gts9u/pcie-l1ss ---------------------------------------------
    def test_policy_off_disables_even_at_boot(self):
        self.endpoint(bound=True)
        self.policy("off\n")
        result = self.run_script()
        self.assertEqual(set(self.substates().values()), {"0"})
        self.assertIn("L1 substates set to 0 (policy off)", result.stderr)

    def test_policy_off_after_a_rescan(self):
        self.endpoint(bound=False)
        self.scenario("rescan-binds")
        self.policy("off")
        self.run_script()
        self.assertEqual(set(self.substates().values()), {"0"})

    def test_policy_on_enables_at_boot(self):
        self.endpoint(bound=True, substates="0")
        self.policy(" on \n")
        self.run_script()
        self.assertEqual(set(self.substates().values()), {"1"})

    def test_unknown_policy_is_auto(self):
        self.endpoint(bound=True, substates="0")
        self.policy("maybe\n")
        result = self.run_script()
        self.assertIn("ignoring 'maybe'", result.stderr)
        self.assertEqual(set(self.substates().values()), {"0"})


class PackagingTest(unittest.TestCase):
    def test_unit_runs_the_packaged_helper(self):
        unit = UNIT.read_text()
        self.assertIn("ExecStart=/usr/libexec/ubuntu-gts9u-pci-rescan\n", unit)
        self.assertIn("WantedBy=multi-user.target", unit)

    def test_postinst_enables_both_wifi_units(self):
        postinst = POSTINST.read_text()
        for unit in ("gts9u-pci-rescan.service", "ubuntu-gts9u-wlan-address.service"):
            self.assertIn("\n%s:multi-user.target\n" % unit, postinst)
            self.assertTrue((REPO / "packaging/ubuntu-gts9u-device/usr/lib/systemd/system"
                             / unit).is_file())

    def test_overlay_no_longer_generates_a_second_copy(self):
        # Two copies of the same unit name would make the overlay's older one
        # win on fresh images, since the overlay is merged after the packages.
        self.assertNotIn("cat > \"$overlay/usr/lib/systemd/system/gts9u-pci-rescan.service\"",
                         OVERLAY.read_text())


if __name__ == "__main__":
    unittest.main()
