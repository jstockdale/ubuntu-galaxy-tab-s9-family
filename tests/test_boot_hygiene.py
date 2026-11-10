"""Offline tests for the boot-hygiene helpers: RTC offset save, the fingerprint
gate generator, the pstore archive and the bring-up microSD logger.  Paths are
rewritten into a temporary directory; nothing on the host is touched."""
import os
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
PKG = REPO / "packaging/ubuntu-gts9u-device"
RTC = PKG / "usr/libexec/ubuntu-gts9u-rtc-offset"
GATE = PKG / "usr/lib/systemd/system-generators/ubuntu-gts9u-fingerprint-gate"
RUNTIME = "/var/lib/ubuntu-gts9u/android-secnvm-root"
ARCHIVE = PKG / "usr/libexec/ubuntu-gts9u-pstore-archive"
SDLOG = REPO / "scripts/bringup-sdlog/gts9u-bringup-sdlog"
UNITS = PKG / "usr/lib/systemd/system"

X710 = b"qcom,kalama-mtp\0qcom,kalama\0qcom,mtp\0samsung,gts9wifi\0qcom,sm8550\0"
X910 = b"qcom,kalama-mtp\0qcom,kalama\0qcom,mtp\0samsung,gts9uwifi\0qcom,sm8550\0"


def run_sh(script, env=None, timeout=20):
    return subprocess.run(["/bin/sh"], input=script, text=True, capture_output=True,
                          env=env, timeout=timeout)


class RtcOffsetSave(unittest.TestCase):
    def save(self, stored, now, counter=1000, synced="yes"):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            rtc = root / "rtc0"
            rtc.mkdir()
            (rtc / "name").write_text("rtc-pm8xxx c400000.spmi:pmic@0:rtc@6100\n")
            (rtc / "since_epoch").write_text(f"{counter}\n")
            state = root / "rtc-offset"
            if stored is not None:
                state.write_text(f"{stored}\n")
            epoch = root / "clock-epoch"
            script = RTC.read_text()
            for old, new in (("/sys/class/rtc/rtc0", str(rtc)),
                             ("/var/lib/ubuntu-gts9u/rtc-offset", str(state)),
                             ("/usr/lib/clock-epoch", str(epoch))):
                self.assertIn(old, script)
                script = script.replace(old, new)
            prelude = f"""
set -- save
timedatectl() {{ echo {synced}; }}
date() {{ [ "$1" = +%s ] && echo {now} && return 0; command date "$@"; }}
"""
            r = run_sh(prelude + script)
            self.assertEqual(r.returncode, 0, r.stderr)
            value = int(state.read_text()) if state.exists() else None
            return value, epoch.exists()

    def test_first_save_writes(self):
        self.assertEqual(self.save(None, 5000), (4000, True))

    def test_smaller_offset_replaces_larger(self):
        # k + 1 stored, k seen: keep k, so the restored clock is never ahead.
        self.assertEqual(self.save(4001, 5000)[0], 4000)

    def test_larger_by_quantisation_is_kept_out(self):
        self.assertEqual(self.save(4000, 5001)[0], 4000)
        self.assertEqual(self.save(4000, 5002)[0], 4000)

    def test_real_drift_rewrites(self):
        self.assertEqual(self.save(4000, 5003)[0], 4003)
        self.assertEqual(self.save(4000, 4997)[0], 3997)

    def test_unsynchronised_changes_nothing(self):
        self.assertEqual(self.save(4001, 5000, synced="no"), (4001, False))


class FingerprintGate(unittest.TestCase):
    def gate(self, compatible, provisioned=True):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            out = root / "normal"
            out.mkdir()
            compat = root / "compatible"
            if compatible is not None:
                compat.write_bytes(compatible)
            runtime = root / "android-secnvm-root"
            if provisioned:
                runtime.mkdir()
            script = GATE.read_text().replace("/proc/device-tree/compatible", str(compat))
            self.assertIn(RUNTIME, script)
            script = script.replace(RUNTIME, str(runtime))
            r = run_sh(f"set -- {out} {root}/early {root}/late\n" + script)
            self.assertEqual(r.returncode, 0, r.stderr)
            link = out / "fprintd.service"
            return os.readlink(link) if link.is_symlink() else None

    def test_x710_masks_fprintd(self):
        self.assertEqual(self.gate(X710), "/dev/null")
        self.assertEqual(self.gate(X710, provisioned=False), "/dev/null")

    def test_unprovisioned_ultra_masks_fprintd(self):
        # Without Android's sec_nvm and spdaemon fprintd can never become
        # ready; masked, GDM's activation fails at once instead of waiting.
        self.assertEqual(self.gate(X910, provisioned=False), "/dev/null")

    def test_no_device_tree_masks_fprintd(self):
        self.assertEqual(self.gate(None), "/dev/null")

    def test_ultra_leaves_fprintd(self):
        self.assertIsNone(self.gate(X910))

    def test_units_carry_the_board_condition(self):
        for name in ("ubuntu-gts9u-fingerprint-secure.service",
                     "ubuntu-gts9u-fingerprint-ui.service",
                     "ubuntu-gts9u-qcomtee.service"):
            with self.subTest(unit=name):
                self.assertIn("ConditionFirmware=device-tree-compatible(samsung,gts9uwifi)",
                              (UNITS / name).read_text())

    def test_secure_transport_needs_the_runtime(self):
        unit = (UNITS / "ubuntu-gts9u-fingerprint-secure.service").read_text()
        self.assertIn("ConditionPathExists=" + RUNTIME, unit)


class PstoreArchive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.src = root / "pstore"
        self.dst = root / "archive"
        self.src.mkdir()
        stubs = root / "bin"
        stubs.mkdir()
        (stubs / "journalctl").write_text(
            "#!/bin/sh\necho ' -1 0123456789abcdef0123456789abcdef Sat x'\n"
            "echo '  0 fedcba9876543210fedcba9876543210 Sat y'\n")
        (stubs / "dmesg").write_text("#!/bin/sh\necho 'ramoops: error in header, 2'\n")
        for f in stubs.iterdir():
            f.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{stubs}:{os.environ['PATH']}",
                        GTS9U_PSTORE_SOURCE=str(self.src),
                        GTS9U_PSTORE_ARCHIVE=str(self.dst))

    def tearDown(self):
        self.tmp.cleanup()

    def run_archive(self):
        r = subprocess.run([str(ARCHIVE)], env=self.env, text=True, capture_output=True,
                           timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def archives(self):
        return sorted(p.name for p in self.dst.iterdir() if not p.name.startswith("."))

    def test_archives_new_records_once(self):
        (self.src / "console-ramoops-0").write_text("boot A console\n")
        (self.src / "1700000000").mkdir()
        (self.src / "1700000000/dmesg.txt").write_text("panic\n")
        self.run_archive()
        names = self.archives()
        self.assertEqual(len(names), 1)
        self.assertRegex(names[0], r"^000001-\d{8}T\d{6}Z-boot-01234567$")
        kept = self.dst / names[0]
        self.assertEqual((kept / "console-ramoops-0").read_text(), "boot A console\n")
        self.assertEqual((kept / "1700000000/dmesg.txt").read_text(), "panic\n")
        self.assertIn("0123456789abcdef0123456789abcdef", (kept / "INFO").read_text())
        self.assertIn("ramoops: error in header, 2", (kept / "INFO").read_text())
        # Same records still there (next boot within 15 min): nothing new.
        self.assertIn("nothing new", self.run_archive())
        self.assertEqual(len(self.archives()), 1)
        # A new console only: only it is kept, the old dmesg record is not.
        (self.src / "console-ramoops-0").write_text("boot B console\n")
        self.run_archive()
        second = self.dst / self.archives()[1]
        self.assertTrue(second.name.startswith("000002-"))
        self.assertEqual(sorted(p.name for p in second.iterdir()),
                         ["INFO", "SHA256SUMS", "console-ramoops-0"])

    def test_keeps_the_newest_ten_past_octal_numbers(self):
        for i in range(14):
            (self.src / "console-ramoops-0").write_text(f"boot {i}\n")
            self.run_archive()
        names = self.archives()
        self.assertEqual(len(names), 10)
        self.assertEqual([n[:6] for n in names], [f"{i:06d}" for i in range(5, 15)])
        self.assertEqual((self.dst / names[-1] / "console-ramoops-0").read_text(), "boot 13\n")

    def test_empty_source_is_quiet(self):
        self.assertIn("nothing new", self.run_archive())
        self.assertEqual(self.archives(), [])


class BringupLogger(unittest.TestCase):
    def test_timer_is_boot_only(self):
        timer = (REPO / "scripts/bringup-sdlog/gts9u-bringup-sdlog.timer").read_text()
        settings = [line for line in timer.splitlines() if not line.startswith("#")]
        self.assertFalse([s for s in settings if s.startswith(("OnUnitActiveSec", "OnCalendar"))])
        self.assertEqual(sum(s.startswith("OnBootSec=") for s in settings), 3)

    def test_copies_moved_pstore_and_prunes(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            card = root / "card"
            (card / "bringup/df92b567").mkdir(parents=True)  # an old-style dir
            for i in range(1, 22):
                (card / f"bringup/{i:06d}-aaaaaaaa").mkdir()
            moved = root / "varpstore"
            moved.mkdir()
            (moved / "console-ramoops-0").write_text("previous boot\n")
            run = root / "run"
            run.mkdir()
            script = SDLOG.read_text()
            for old, new in (("/var/lib/systemd/pstore", str(moved)),
                             ("/sys/fs/pstore", str(root / "nopstore")),
                             ("/run/gts9u-bringup-sdlog.dir", str(run / "dir")),
                             ("/run/bringup-sd", str(card)),
                             ("/dev/mmcblk1p1", str(root / "dev"))):
                self.assertIn(old, script)
                script = script.replace(old, new)
            (root / "dev").write_text("")
            # A plain file stands in for the card's block device.
            self.assertIn('[ -b "$dev" ]', script)
            script = script.replace('[ -b "$dev" ]', '[ -e "$dev" ]')
            prelude = """
mountpoint() { return 0; }
umount() { :; }
sync() { :; }
dmesg() { echo kernel; }
journalctl() { echo journal; }
systemctl() { :; }
lspci() { :; }
ip() { :; }
"""
            env = dict(os.environ, GTS9U_SDLOG_KEEP="20")
            for _ in range(2):
                r = run_sh(prelude + script, env=env)
                self.assertEqual(r.returncode, 0, r.stderr)
            names = sorted(p.name for p in (card / "bringup").iterdir())
            self.assertIn("df92b567", names)
            numbered = [n for n in names if n[:6].isdigit() and n[6:7] == "-"]
            # 21 old + this boot = 22, pruned once to 20; the second run of
            # the same boot reuses its directory and does not prune again.
            self.assertEqual(len(numbered), 20)
            self.assertEqual(numbered[0][:6], "000003")
            newest = card / "bringup" / numbered[-1]
            self.assertTrue(numbered[-1].startswith("000022-"))
            self.assertEqual((newest / "pstore-console-ramoops-0").read_text(), "previous boot\n")
            self.assertGreaterEqual(len(list(newest.glob("journal-*s.txt"))), 1)


if __name__ == "__main__":
    unittest.main()
