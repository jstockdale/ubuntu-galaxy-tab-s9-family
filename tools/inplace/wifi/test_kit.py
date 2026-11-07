#!/usr/bin/env python3
"""Run install.sh and rollback.sh against a fake root, as the user.

vendor_boot is an 8 MiB regular file (the scripts' 96 MiB size is rewritten),
dd/blockdev/install/systemctl/dpkg/id are stubs, and every absolute path the
scripts touch is moved under a temporary directory.  No block device is
opened and nothing outside the temporary directory is written.

    python3 tools/inplace/wifi/test_kit.py
"""
import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[2]
PACKAGE = REPO / "packaging/ubuntu-gts9u-device"
SIZE = 8 * 1024 * 1024
HW21 = "usr/lib/firmware/ath11k/WCN6855/hw2.1"
X710 = b"qcom,kalama-mtp\0qcom,kalama\0samsung,gts9wifi\0qcom,sm8550\0"
ULTRA = b"qcom,kalama-mtp\0qcom,kalama\0qcom,mtp\0samsung,gts9uwifi\0qcom,sm8550\0"

STUBS = {
    "id": '#!/bin/sh\n[ "$1" = -u ] && echo 0\n',
    "blockdev": '#!/bin/sh\ncase "$1" in --getsize64) stat -c %s "$2" ;; esac\nexit 0\n',
    # dd without the flags a regular file on tmpfs refuses; a marker file makes
    # the next write to the partition land corrupted, as a failing flash would.
    "dd": r'''#!/bin/sh
args=
for a; do
	case "$a" in iflag=direct|conv=fsync) ;; *) args="$args $a" ;; esac
	case "$a" in of=*vendor_boot) target=${a#of=} ;; esac
done
/bin/dd $args
rc=$?
if [ -n "${target:-}" ] && [ -e "$FAKE_ROOT/corrupt-next-write" ]; then
	rm -f "$FAKE_ROOT/corrupt-next-write"
	printf 'garbage' | /bin/dd of="$target" conv=notrunc status=none
fi
exit $rc
''',
    "install": r'''#!/bin/sh
args=
skip=0
for a; do
	if [ "$skip" = 1 ]; then skip=0; continue; fi
	case "$a" in -o|-g) skip=1 ;; *) args="$args $a" ;; esac
done
exec /usr/bin/install $args
''',
    "systemctl": r'''#!/bin/sh
echo "systemctl $*" >> "$FAKE_ROOT/calls.log"
wants=$FAKE_ROOT/etc/systemd/system/multi-user.target.wants
case "$1" in
enable) shift; mkdir -p "$wants"; for u; do ln -sf "$FAKE_ROOT/usr/lib/systemd/system/$u" "$wants/$u"; done ;;
disable) shift; for u; do rm -f "$wants/$u"; done ;;
esac
exit 0
''',
    "dpkg": '#!/bin/sh\n[ -e "$FAKE_ROOT/packaged" ] && [ "$1" = -S ] && exit 0\nexit 1\n',
    "dpkg-query": '#!/bin/sh\necho 2.56\n',
    "udevadm": '#!/bin/sh\nexit 0\n',
    "sleep": '#!/bin/sh\nexit 0\n',
}


def sha(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


class KitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name) / "root"
        self.kit = pathlib.Path(self.tmp.name) / "kit"
        r = self.root
        for d in ("bin", "proc/device-tree", "run", "var/log", "var/backups",
                  "usr/libexec", "usr/lib/systemd/system", "etc/systemd/system/multi-user.target.wants",
                  "dev/disk/by-partlabel", HW21):
            (r / d).mkdir(parents=True, exist_ok=True)
        for name, body in STUBS.items():
            (r / "bin" / name).write_text(body)
            (r / "bin" / name).chmod(0o755)
        (r / "proc/device-tree/compatible").write_bytes(X710)
        # The overlay's old rescan unit, enabled the way the overlay did it.
        self.old_unit = "[Service]\nExecStart=/bin/sh -c 'old one-liner'\n"
        (r / "usr/lib/systemd/system/gts9u-pci-rescan.service").write_text(self.old_unit)
        os.symlink("/usr/lib/systemd/system/gts9u-pci-rescan.service",
                   r / "etc/systemd/system/multi-user.target.wants/gts9u-pci-rescan.service")
        # rootfs firmware, identical to the ramdisk's
        for name in ("amss.bin", "m3.bin", "board-2.bin", "board.bin", "regdb.bin"):
            (r / HW21 / name).write_bytes(os.urandom(64) + name.encode())
        # base and variant images, and the partition holding the base
        self.base = os.urandom(SIZE)
        self.variant = os.urandom(SIZE)
        self.part = r / "dev/disk/by-partlabel/vendor_boot"
        self.part.write_bytes(self.base)
        self.make_kit()

    def tearDown(self):
        self.tmp.cleanup()

    def make_kit(self):
        k = self.kit
        (k / "files").mkdir(parents=True)
        (k / "images").mkdir()
        for name in ("install.sh", "rollback.sh", "verify.sh", "README.md"):
            shutil.copy(HERE / name, k / name)
        for name in ("ubuntu-gts9u-wlan-address", "ubuntu-gts9u-pci-rescan"):
            shutil.copy(PACKAGE / "usr/libexec" / name, k / "files" / name)
        for name in ("ubuntu-gts9u-wlan-address.service", "gts9u-pci-rescan.service"):
            shutil.copy(PACKAGE / "usr/lib/systemd/system" / name, k / "files" / name)
        (k / "images/variant.img").write_bytes(self.variant)
        base_sha = hashlib.sha256(self.base).hexdigest()
        (k / "IMAGES").write_text("# base variant file\n%s %s images/variant.img\n"
                                  % (base_sha, sha(k / "images/variant.img")))
        (k / "FIRMWARE.sha256").write_text("".join(
            "%s  %s/%s\n" % (sha(p), HW21, p.name) for p in sorted((self.root / HW21).iterdir())))
        self.sums()

    def sums(self):
        lines = []
        for p in sorted(self.kit.rglob("*")):
            if p.is_file() and p.name != "SHA256SUMS":
                lines.append("%s  %s\n" % (sha(p), p.relative_to(self.kit)))
        (self.kit / "SHA256SUMS").write_text("".join(lines))

    def text(self, name):
        text = (self.kit / (name + ".sh")).read_text()
        text = text.replace("part_size=100663296", "part_size=%d" % SIZE)
        text = text.replace('[ -b "$part" ]', '[ -e "$part" ]')
        text = text.replace("(cd / &&", "(cd %s/ &&" % self.root)
        text = re.sub(r"/dev/disk/by-partlabel/vendor_boot|/var/backups|/var/log/|/usr/libexec|"
                      r"/usr/lib/systemd/system|/etc/systemd/system|/proc/device-tree|/run/",
                      lambda m: str(self.root) + m.group(0), text)
        # ... except inside the unit files' symlink targets written by the stub
        return text

    def run_kit(self, name, *args, expect=0):
        script = self.kit / (name + ".sh.test")
        script.write_text(self.text(name))
        env = dict(os.environ, FAKE_ROOT=str(self.root),
                   PATH="%s:/usr/bin:/bin" % (self.root / "bin"))
        result = subprocess.run(["/bin/sh", str(script), *args], env=env, text=True,
                                capture_output=True, timeout=60, cwd=self.tmp.name)
        script.unlink()
        self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def partition(self):
        return self.part.read_bytes()

    def exists(self, rel):
        return (self.root / rel).exists()

    # --- install -----------------------------------------------------------
    def test_install_flashes_the_variant_and_installs_the_units(self):
        out = self.run_kit("install")
        self.assertEqual(self.partition(), self.variant)
        self.assertIn(": base (images/variant.img)", out)
        self.assertIn("(read back from the disk)", out)
        backup = self.root / "var/backups/ubuntu-gts9u-wifi"
        self.assertEqual((backup / "vendor_boot.img").read_bytes(), self.base)
        self.assertEqual((backup / "gts9u-pci-rescan.service").read_text(), self.old_unit)
        for name, mode in (("usr/libexec/ubuntu-gts9u-wlan-address", 0o755),
                           ("usr/libexec/ubuntu-gts9u-pci-rescan", 0o755),
                           ("usr/lib/systemd/system/ubuntu-gts9u-wlan-address.service", 0o644),
                           ("usr/lib/systemd/system/gts9u-pci-rescan.service", 0o644)):
            self.assertEqual((self.root / name).stat().st_mode & 0o777, mode, name)
        self.assertEqual((self.root / "usr/lib/systemd/system/gts9u-pci-rescan.service").read_text(),
                         (PACKAGE / "usr/lib/systemd/system/gts9u-pci-rescan.service").read_text())
        self.assertTrue(self.exists("etc/systemd/system/multi-user.target.wants/ubuntu-gts9u-wlan-address.service"))
        self.assertIn("install: done.", (self.root / "var/log/gts9u-wifi-kit.log").read_text())

    def test_rerun_keeps_backups_and_does_not_rewrite(self):
        self.run_kit("install")
        out = self.run_kit("install")
        self.assertIn("already holds the Wi-Fi variant; not written again", out)
        self.assertIn("re-run: backups from", out)
        self.assertEqual((self.root / "var/backups/ubuntu-gts9u-wifi/vendor_boot.img").read_bytes(),
                         self.base)

    def test_unknown_vendor_boot_is_refused_untouched(self):
        other = os.urandom(SIZE)
        self.part.write_bytes(other)
        out = self.run_kit("install", expect=1)
        self.assertIn("no Wi-Fi variant for", out)
        self.assertEqual(self.partition(), other)
        self.assertFalse(self.exists("var/backups/ubuntu-gts9u-wifi"))
        self.assertFalse(self.exists("usr/libexec/ubuntu-gts9u-wlan-address"))

    def test_userspace_only_works_on_any_vendor_boot(self):
        other = os.urandom(SIZE)
        self.part.write_bytes(other)
        self.run_kit("install", "--userspace-only")
        self.assertEqual(self.partition(), other)
        self.assertTrue(self.exists("usr/libexec/ubuntu-gts9u-wlan-address"))

    def test_vendor_boot_only_leaves_userspace(self):
        self.run_kit("install", "--vendor-boot-only")
        self.assertEqual(self.partition(), self.variant)
        self.assertFalse(self.exists("usr/libexec/ubuntu-gts9u-wlan-address"))
        self.assertEqual((self.root / "usr/lib/systemd/system/gts9u-pci-rescan.service").read_text(),
                         self.old_unit)

    def test_rootfs_firmware_mismatch_is_refused(self):
        (self.root / HW21 / "amss.bin").write_bytes(b"different")
        out = self.run_kit("install", expect=1)
        self.assertIn("refusing to mix them", out)
        self.assertEqual(self.partition(), self.base)

    def test_kit_checksum_mismatch_is_refused(self):
        with open(self.kit / "images/variant.img", "r+b") as f:
            f.write(b"x")
        out = self.run_kit("install", expect=1)
        self.assertIn("kit checksum mismatch", out)
        self.assertEqual(self.partition(), self.base)

    def test_other_board_is_refused(self):
        (self.root / "proc/device-tree/compatible").write_bytes(ULTRA)
        out = self.run_kit("install", expect=1)
        self.assertIn("this is not an SM-X710", out)
        self.assertEqual(self.partition(), self.base)

    def test_bad_flash_puts_the_original_back(self):
        (self.root / "corrupt-next-write").touch()
        out = self.run_kit("install", expect=1)
        self.assertIn("vendor_boot holds the original again (read back)", out)
        self.assertEqual(self.partition(), self.base)

    def test_packaged_helpers_are_not_overwritten(self):
        (self.root / "packaged").touch()
        out = self.run_kit("install")
        self.assertIn("already ships the helpers; skipped", out)
        self.assertFalse(self.exists("usr/libexec/ubuntu-gts9u-wlan-address"))
        self.assertEqual(self.partition(), self.variant)

    # --- rollback -------------------------------------------------------------
    def test_rollback_restores_everything(self):
        self.run_kit("install")
        out = self.run_kit("rollback")
        self.assertIn("(read back from the disk)", out)
        self.assertEqual(self.partition(), self.base)
        self.assertEqual((self.root / "usr/lib/systemd/system/gts9u-pci-rescan.service").read_text(),
                         self.old_unit)
        for gone in ("usr/libexec/ubuntu-gts9u-wlan-address", "usr/libexec/ubuntu-gts9u-pci-rescan",
                     "usr/lib/systemd/system/ubuntu-gts9u-wlan-address.service",
                     "etc/systemd/system/multi-user.target.wants/ubuntu-gts9u-wlan-address.service"):
            self.assertFalse(self.exists(gone), gone)
        # The overlay's enablement of the old unit is untouched.
        self.assertTrue(os.path.islink(self.root / "etc/systemd/system/multi-user.target.wants/gts9u-pci-rescan.service"))
        self.assertFalse(self.exists("var/backups/ubuntu-gts9u-wifi"))
        retired = [p.name for p in (self.root / "var/backups").iterdir()]
        self.assertTrue(any(n.startswith("ubuntu-gts9u-wifi.rolled-back-") for n in retired))
        # A second rollback has nothing to do and says so.
        self.assertIn("no complete backup", self.run_kit("rollback", expect=1))

    def test_rollback_images_only(self):
        self.run_kit("install")
        out = self.run_kit("rollback", "--images-only")
        self.assertIn("images back", out)
        self.assertEqual(self.partition(), self.base)
        self.assertTrue(self.exists("usr/libexec/ubuntu-gts9u-wlan-address"))
        self.assertTrue(self.exists("var/backups/ubuntu-gts9u-wifi/complete"))

    def test_rollback_refuses_a_damaged_backup(self):
        self.run_kit("install")
        with open(self.root / "var/backups/ubuntu-gts9u-wifi/vendor_boot.img", "r+b") as f:
            f.write(b"x")
        out = self.run_kit("rollback", expect=1)
        self.assertIn("does not match its checksum; nothing was changed", out)
        self.assertEqual(self.partition(), self.variant)


if __name__ == "__main__":
    unittest.main()
