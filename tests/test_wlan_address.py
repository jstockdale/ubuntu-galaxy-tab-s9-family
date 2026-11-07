"""Run ubuntu-gts9u-wlan-address against a fake root: the EFS partition, its
mount, sysfs network interfaces, ip and udevadm are all simulated, nothing on
the host is touched."""
import os
import pathlib
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "packaging/ubuntu-gts9u-device/usr/libexec/ubuntu-gts9u-wlan-address"
LINK = "run/systemd/network/05-ubuntu-gts9u-wlan.link"
EFS_MAC = "68:DF:E4:12:34:56"

# `mount -t ext4 -o ro,noload DEV DIR`: put the fake EFS tree in DIR and record
# the mount in the fake /proc/mounts, refusing anything but a read-only mount.
STUB_MOUNT = """#!/bin/sh
echo "mount $*" >> "$FAKE_ROOT/calls.log"
[ -e "$FAKE_ROOT/mount-fails" ] && exit 32
case "$*" in *ro,noload*) ;; *) echo 'not read-only' >&2; exit 99 ;; esac
for target; do :; done
cp -a "$FAKE_ROOT/efs-content/." "$target/"
echo "efs $target ext4 ro 0 0" >> "$FAKE_ROOT/proc/mounts"
"""

STUB_UMOUNT = """#!/bin/sh
echo "umount $*" >> "$FAKE_ROOT/calls.log"
grep -v " $1 " "$FAKE_ROOT/proc/mounts" > "$FAKE_ROOT/proc/mounts.new"
mv "$FAKE_ROOT/proc/mounts.new" "$FAKE_ROOT/proc/mounts"
find "$1" -mindepth 1 -delete
"""

# `ip link set dev IF address ADDR` changes the fake sysfs address.
STUB_IP = """#!/bin/sh
echo "ip $*" >> "$FAKE_ROOT/calls.log"
[ "$1 $2 $3" = "link set dev" ] && [ "$5" = address ] || exit 2
echo "$6" > "$FAKE_ROOT/sys/class/net/$4/address"
"""

STUB_LOG = """#!/bin/sh
echo "$(basename "$0") $*" >> "$FAKE_ROOT/calls.log"
"""

TOOLS = ("sed", "head", "tr", "grep", "mkdir", "mv", "rm", "rmdir", "readlink",
         "basename", "cat", "cp", "find")


def any_block_device():
    """The script tests the EFS node with -b; point the fake one at a real
    block device (never opened: mount is a stub)."""
    for name in sorted(os.listdir("/dev")):
        path = os.path.join("/dev", name)
        try:
            if stat.S_ISBLK(os.stat(path).st_mode):
                return path
        except OSError:
            continue
    return None


BLOCK = any_block_device()


@unittest.skipIf(BLOCK is None, "needs any block device node to stand in for EFS")
class WlanAddressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        for directory in ("bin", "tools", "proc", "run", "dev/disk/by-partlabel",
                          "sys/class/net", "sys/class/block",
                          "sys/bus/pci/drivers/ath11k_pci", "sys/bus/usb/drivers/rndis_host",
                          "efs-content/wifi"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        (self.root / "proc/mounts").write_text("")
        for tool in TOOLS:
            os.symlink(shutil.which(tool), self.root / "tools" / tool)
        self.stub("mount", STUB_MOUNT)
        self.stub("umount", STUB_UMOUNT)
        self.stub("ip", STUB_IP)
        self.stub("udevadm", STUB_LOG)
        self.stub("sleep", "#!/bin/sh\nexit 0\n")
        os.symlink(BLOCK, self.root / "dev/disk/by-partlabel/efs")
        self.efs_mac(EFS_MAC)

    def tearDown(self):
        self.tmp.cleanup()

    def stub(self, name, body):
        path = self.root / "bin" / name
        path.write_text(body)
        path.chmod(0o755)

    def efs_mac(self, text):
        (self.root / "efs-content/wifi/.mac.info").write_text(text)

    def interface(self, name="wlp1s0", address="00:03:7f:12:09:4b", flags="0x1002",
                  driver="sys/bus/pci/drivers/ath11k_pci", wireless=True):
        net = self.root / "sys/class/net" / name
        device = self.root / "sys/devices" / name
        device.mkdir(parents=True)
        os.symlink(self.root / driver, device / "driver")
        net.mkdir(parents=True)
        os.symlink(device, net / "device")
        (net / "address").write_text(address + "\n")
        (net / "flags").write_text(flags + "\n")
        if wireless:
            (net / "phy80211").mkdir()

    def script(self):
        text = SCRIPT.read_text()
        pattern = re.compile(r"/run/systemd/network|/run/ubuntu-gts9u-wlan-efs|"
                             r"/sys/class/net|/sys/class/block|/proc/mounts|/dev/(?!null)")
        return pattern.sub(lambda match: str(self.root) + match.group(0), text)

    def run_script(self, *args):
        env = dict(os.environ, FAKE_ROOT=str(self.root),
                   PATH="%s:%s" % (self.root / "bin", self.root / "tools"))
        result = subprocess.run(["/bin/sh", "-s", "--", *args], input=self.script(), env=env,
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def calls(self):
        log = self.root / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def address(self, name="wlp1s0"):
        return (self.root / "sys/class/net" / name / "address").read_text().strip()

    def link(self):
        return (self.root / LINK).read_text()

    def assert_unmounted(self):
        self.assertEqual((self.root / "proc/mounts").read_text(), "")
        self.assertFalse((self.root / "run/ubuntu-gts9u-wlan-efs").exists())

    # --- the address reaches an interface that already exists -----------
    def test_down_interface_takes_the_efs_address(self):
        self.interface()
        result = self.run_script()
        self.assertEqual(self.address(), "68:df:e4:12:34:56")
        self.assertIn("ip link set dev wlp1s0 address 68:df:e4:12:34:56", self.calls())
        self.assertIn("00:03:7f:12:09:4b -> 68:df:e4:12:34:56", result.stderr)
        self.assert_unmounted()

    def test_efs_is_mounted_read_only_and_unmounted(self):
        self.interface()
        self.run_script()
        mounts = [c for c in self.calls() if c.startswith("mount ")]
        self.assertEqual(len(mounts), 1)
        self.assertIn("-o ro,noload", mounts[0])
        self.assertTrue(any(c.startswith("umount ") for c in self.calls()))
        self.assert_unmounted()

    def test_link_file_for_later_interfaces(self):
        self.run_script()
        link = self.link()
        self.assertIn("[Match]\nDriver=ath11k_pci\nType=wlan\n", link)
        self.assertIn("MACAddress=68:df:e4:12:34:56\n", link)
        # It replaces 99-default.link for the interface, so it must keep the
        # predictable name.
        self.assertIn("NamePolicy=keep kernel database onboard slot path\n", link)
        self.assertIn("udevadm control --reload", self.calls())

    def test_no_interface_yet_is_not_an_error(self):
        result = self.run_script()
        self.assertIn("no Wi-Fi interface yet", result.stderr)
        self.assertFalse(any(c.startswith("ip ") for c in self.calls()))

    def test_interface_that_is_up_is_left_alone(self):
        self.interface(flags="0x1003")
        result = self.run_script()
        self.assertEqual(self.address(), "00:03:7f:12:09:4b")
        self.assertFalse(any(c.startswith("ip ") for c in self.calls()))
        self.assertIn("already up", result.stderr)
        # ... but the next creation gets it.
        self.assertIn("MACAddress=68:df:e4:12:34:56", self.link())

    def test_interface_with_the_address_already(self):
        self.interface(address="68:df:e4:12:34:56")
        result = self.run_script()
        self.assertFalse(any(c.startswith("ip ") for c in self.calls()))
        self.assertIn("already has the native address", result.stderr)

    def test_other_drivers_are_not_touched(self):
        self.interface(name="usb0", driver="sys/bus/usb/drivers/rndis_host", wireless=False)
        self.interface(name="wlx001122334455", driver="sys/bus/usb/drivers/rndis_host")
        self.run_script()
        self.assertEqual(self.address("usb0"), "00:03:7f:12:09:4b")
        self.assertEqual(self.address("wlx001122334455"), "00:03:7f:12:09:4b")
        self.assertFalse(any(c.startswith("ip ") for c in self.calls()))

    # --- what EFS holds ---------------------------------------------------
    def test_lowercase_and_newline_are_accepted(self):
        self.interface()
        self.efs_mac("68:df:e4:12:34:56\n")
        self.run_script()
        self.assertEqual(self.address(), "68:df:e4:12:34:56")

    def test_malformed_address_changes_nothing(self):
        for text in ("", "68:DF:E4:12:34", "68-DF-E4-12-34-56", "zz:DF:E4:12:34:56",
                     "68:DF:E4:12:34:56:78"):
            with self.subTest(text=text):
                self.efs_mac(text)
                result = self.run_script()
                self.assertIn("no valid wifi/.mac.info", result.stderr)
                self.assertFalse((self.root / LINK).exists())
                self.assert_unmounted()

    def test_multicast_and_zero_addresses_are_refused(self):
        for text in ("01:00:5E:00:00:01", "00:00:00:00:00:00"):
            with self.subTest(text=text):
                self.efs_mac(text)
                result = self.run_script()
                self.assertIn("not a unicast address", result.stderr)
                self.assertFalse((self.root / LINK).exists())

    def test_missing_mac_file(self):
        (self.root / "efs-content/wifi/.mac.info").unlink()
        self.interface()
        self.run_script()
        self.assertFalse((self.root / LINK).exists())
        self.assertEqual(self.address(), "00:03:7f:12:09:4b")

    def test_mount_failure_changes_nothing(self):
        (self.root / "mount-fails").touch()
        self.interface()
        result = self.run_script()
        self.assertIn("could not mount EFS", result.stderr)
        self.assertFalse((self.root / LINK).exists())

    # --- finding the partition --------------------------------------------
    def test_sysfs_fallback_without_the_udev_link(self):
        (self.root / "dev/disk/by-partlabel/efs").unlink()
        block = self.root / "sys/class/block/sda6"
        block.mkdir(parents=True)
        (block / "uevent").write_text("MAJOR=8\nMINOR=6\nDEVNAME=sda6\nDEVTYPE=partition\n"
                                      "PARTN=6\nPARTNAME=efs\n")
        other = self.root / "sys/class/block/sda5"
        other.mkdir(parents=True)
        (other / "uevent").write_text("DEVNAME=sda5\nPARTNAME=efs_backup\n")
        os.symlink(BLOCK, self.root / "dev/sda6")
        self.interface()
        self.run_script()
        mount = [c for c in self.calls() if c.startswith("mount ")][0]
        self.assertIn(str(self.root / "dev/sda6"), mount)
        self.assertEqual(self.address(), "68:df:e4:12:34:56")

    def test_no_efs_partition(self):
        (self.root / "dev/disk/by-partlabel/efs").unlink()
        self.interface()
        result = self.run_script()
        self.assertIn("EFS partition not found", result.stderr)
        self.assertFalse(any(c.startswith("mount ") for c in self.calls()))

    # --- --check ------------------------------------------------------------
    def test_check_reads_but_changes_nothing(self):
        self.interface()
        result = self.run_script("--check")
        self.assertIn("wifi/.mac.info: 68:df:e4:12:34:56", result.stdout)
        self.assertIn("wlp1s0: 00:03:7f:12:09:4b now", result.stdout)
        self.assertFalse((self.root / LINK).exists())
        self.assertFalse(any(c.startswith(("ip ", "udevadm")) for c in self.calls()))
        self.assert_unmounted()


if __name__ == "__main__":
    unittest.main()
