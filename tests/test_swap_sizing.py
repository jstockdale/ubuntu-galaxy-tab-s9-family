"""Run ubuntu-gts9u-zram and ubuntu-gts9u-swapfile against a fake root:
/proc/meminfo, the zram sysfs, df, fallocate, dd, mkswap, swapon and swapoff
are all simulated, and nothing on the host is touched.  The swapfiles are
sparse, so a 16 GiB one takes no space on the test machine.

mkswap writes the real signature (SWAPSPACE2 at 4086) and swapon refuses a
file without it; dd marks what it writes as DENSE at offset 0, which matters
only when the `swapon-refuses-sparse` switch is set.  The active swap areas
are the lines of $FAKE_ROOT/swaps."""
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = REPO / "packaging/ubuntu-gts9u-device"
SWAPFILE = PACKAGE / "usr/libexec/ubuntu-gts9u-swapfile"
ZRAM = PACKAGE / "usr/libexec/ubuntu-gts9u-zram"
POSTINST = PACKAGE / "DEBIAN/postinst"
PRERM = PACKAGE / "DEBIAN/prerm"

GIB = 1 << 30
MIB = 1 << 20
# MemTotal in kB, read on the tablets on 2026-11-22.
SM_X910_16GB = 14903260
SM_X710_12GB = 10816184
# No 8 GB tablet has been measured; an 8 GB model would report about 7 GB.
ESTIMATE_8GB = 7200000

COMMON = r"""#!/bin/sh
# The stubs get the host's tools; the scripts under test only get TOOLS.
PATH=/usr/bin:/bin
r=$FAKE_ROOT
echo "$(basename "$0") $*" >> "$r/calls.log"
"""

STUB_SWAPON = COMMON + r"""
case "$1" in
--show=NAME)
	[ -e "$r/swapon-show-fails" ] && exit 255
	cat "$r/swaps"
	exit 0 ;;
--priority) f=$3 ;;
*) exit 2 ;;
esac
[ -f "$f" ] || exit 255
sig=$(dd if="$f" bs=1 skip=4086 count=10 status=none 2>/dev/null | tr -d '\000')
[ "$sig" = SWAPSPACE2 ] || exit 255
if [ -e "$r/swapon-refuses-sparse" ]; then
	tag=$(dd if="$f" bs=1 count=5 status=none 2>/dev/null | tr -d '\000')
	[ "$tag" = DENSE ] || exit 255
fi
grep -qxF "$f" "$r/swaps" && exit 255
echo "$f" >> "$r/swaps"
"""

STUB_SWAPOFF = COMMON + r"""
grep -vxF "$1" "$r/swaps" > "$r/swaps.new"
mv "$r/swaps.new" "$r/swaps"
"""

STUB_MKSWAP = COMMON + r"""
for f; do :; done
[ -f "$f" ] || exit 1
printf SWAPSPACE2 | dd of="$f" bs=1 seek=4086 conv=notrunc status=none
"""

# `fallocate -l SIZE FILE`.  disk-full: the allocation stops part way.
STUB_FALLOCATE = COMMON + r"""
[ "$1" = -l ] || exit 2
[ -e "$r/fallocate-unsupported" ] && exit 1
if [ -e "$r/disk-full" ]; then truncate -s 1G "$3"; exit 1; fi
truncate -s "$2" "$3"
"""

# `dd if=/dev/zero of=FILE bs=1M count=N ...`, written sparse.
STUB_DD = COMMON + r"""
of=; count=
for a; do
	case $a in of=*) of=${a#of=} ;; count=*) count=${a#count=} ;; esac
done
[ -n "$of" ] && [ -n "$count" ] || exit 2
if [ -e "$r/disk-full" ]; then truncate -s 1G "$of"; exit 1; fi
truncate -s "${count}M" "$of"
printf DENSE | dd of="$of" conv=notrunc status=none
"""

# `df -BG --output=avail /`
STUB_DF = COMMON + r"""
echo ' Avail'
echo "  $(cat "$r/avail-gib")G"
"""

TOOLS = ("sed", "grep", "cat", "rm", "mv", "chmod", "stat", "tail", "tr")


def meminfo(kb):
    return ("MemTotal:       %8d kB\nMemFree:          812344 kB\n"
            "MemAvailable:    4087212 kB\nSwapTotal:             0 kB\n" % kb)


class FakeRoot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        for directory in ("bin", "tools", "proc"):
            (self.root / directory).mkdir()
        for tool in TOOLS:
            os.symlink(shutil.which(tool), self.root / "tools" / tool)
        for name, body in (("swapon", STUB_SWAPON), ("swapoff", STUB_SWAPOFF),
                           ("mkswap", STUB_MKSWAP), ("fallocate", STUB_FALLOCATE),
                           ("dd", STUB_DD), ("df", STUB_DF)):
            path = self.root / "bin" / name
            path.write_text(body)
            path.chmod(0o755)
        (self.root / "swaps").write_text("")
        (self.root / "avail-gib").write_text("140\n")
        self.ram(SM_X710_12GB)

    def tearDown(self):
        self.tmp.cleanup()

    def ram(self, kb):
        (self.root / "proc/meminfo").write_text(meminfo(kb))

    def switch(self, name):
        (self.root / name).touch()

    def run_text(self, text, *args):
        env = dict(os.environ, FAKE_ROOT=str(self.root),
                   PATH="%s:%s" % (self.root / "bin", self.root / "tools"))
        result = subprocess.run(["/bin/sh", "-s", "--", *args], input=text, env=env,
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def calls(self, tool=None):
        log = self.root / "calls.log"
        lines = log.read_text().splitlines() if log.exists() else []
        return [line for line in lines if tool is None or line.split()[0] == tool]

    def active(self):
        return (self.root / "swaps").read_text().splitlines()


class SwapfileTest(FakeRoot):
    def setUp(self):
        super().setUp()
        self.path = self.root / "swapfile"

    def script(self):
        text = SWAPFILE.read_text()
        self.assertIn("\nswapfile=/swapfile\n", text)
        text = text.replace("\nswapfile=/swapfile\n", "\nswapfile=%s\n" % self.path)
        return text.replace("/proc/meminfo", str(self.root / "proc/meminfo"))

    def start(self):
        return self.run_text(self.script(), "start")

    def boot(self):
        """Nothing is active after a reboot."""
        (self.root / "swaps").write_text("")
        (self.root / "calls.log").unlink(missing_ok=True)

    def existing(self, gib, signed=True, active=False):
        with open(self.path, "wb") as f:
            f.truncate(gib * GIB)
        if signed:
            with open(self.path, "r+b") as f:
                f.seek(4086)
                f.write(b"SWAPSPACE2")
        if active:
            with open(self.root / "swaps", "a") as f:
                f.write("%s\n" % self.path)
        return self.path.stat().st_ino

    def size(self):
        return self.path.stat().st_size

    def assert_enabled(self, gib):
        self.assertEqual(self.size(), gib * GIB)
        self.assertEqual(self.active(), [str(self.path)])
        with open(self.path, "rb") as f:
            f.seek(4086)
            self.assertEqual(f.read(10), b"SWAPSPACE2")
        self.assertFalse((self.root / "swapfile.new").exists())

    def assert_untouched(self):
        for tool in ("fallocate", "dd", "mkswap", "swapoff"):
            self.assertEqual(self.calls(tool), [], tool)

    # --- the size follows the RAM ------------------------------------------
    def test_size_is_the_ram_as_sold(self):
        cases = (
            (SM_X910_16GB, 16), (SM_X710_12GB, 12), (ESTIMATE_8GB, 8), (7500000, 8),
            # Exact multiples of 4 GiB stay; one kB more is the next model.
            (8 * 1048576, 8), (8 * 1048576 + 1, 12), (12 * 1048576, 12),
            (12 * 1048576 + 1, 16), (16 * 1048576, 16),
            # Outside the family's 8 to 16 GiB: clamped.
            (2 * 1048576, 8), (4 * 1048576, 8), (16 * 1048576 + 1, 16), (64 * 1048576, 16),
        )
        for kb, gib in cases:
            with self.subTest(kb=kb):
                self.path.unlink(missing_ok=True)
                self.boot()
                self.ram(kb)
                result = self.start()
                self.assert_enabled(gib)
                self.assertIn("fallocate -l %dG %s.new" % (gib, self.path), self.calls())
                self.assertIn("enabled %s, %d GiB, priority 10" % (self.path, gib),
                              result.stderr)
                self.assertIn("swapon --priority 10 %s" % self.path, self.calls())

    def test_unreadable_meminfo_keeps_what_is_there(self):
        (self.root / "proc/meminfo").unlink()
        inode = self.existing(16)
        result = self.start()
        self.assertIn("cannot read MemTotal", result.stderr)
        self.assertEqual(self.path.stat().st_ino, inode)
        self.assert_enabled(16)
        self.assert_untouched()

    def test_unreadable_meminfo_creates_nothing(self):
        for text in ("MemFree: 812344 kB\n", "MemTotal: 0 kB\n",
                     "MemTotal: 123456789012345 kB\n", "MemTotal: 14903260\n"):
            with self.subTest(meminfo=text):
                (self.root / "proc/meminfo").write_text(text)
                result = self.start()
                self.assertIn("cannot read MemTotal", result.stderr)
                self.assertFalse(self.path.exists())
                self.assert_untouched()

    def test_unreadable_meminfo_does_not_rebuild(self):
        (self.root / "proc/meminfo").unlink()
        inode = self.existing(16, signed=False)
        result = self.start()
        self.assertIn("unusable; not rebuilding it", result.stderr)
        self.assertEqual(self.path.stat().st_ino, inode)
        self.assertEqual(self.active(), [])
        self.assert_untouched()

    # --- an existing file -----------------------------------------------------
    def test_right_sized_file_is_reused(self):
        self.ram(SM_X910_16GB)
        inode = self.existing(16)
        mtime = self.path.stat().st_mtime_ns
        result = self.start()
        self.assertIn("enabled the existing swapfile", result.stderr)
        self.assert_enabled(16)
        self.assertEqual(self.path.stat().st_ino, inode)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)
        self.assert_untouched()

    def test_wrong_sized_inactive_file_is_replaced(self):
        # The SM-X710 test unit: 12 GB of RAM and the 16 GiB file of before.
        inode = self.existing(16)
        result = self.start()
        self.assertIn("replacing the 16 GiB swapfile with a 12 GiB one", result.stderr)
        self.assert_enabled(12)
        self.assertNotEqual(self.path.stat().st_ino, inode)
        self.assertEqual(self.calls("swapoff"), [])
        self.assertEqual(self.calls("fallocate"), ["fallocate -l 12G %s.new" % self.path])

    def test_active_wrong_sized_file_is_left_alone(self):
        inode = self.existing(16, active=True)
        result = self.start()
        self.assertIn("already active at 16 GiB; it becomes 12 GiB", result.stderr)
        self.assertEqual(self.size(), 16 * GIB)
        self.assertEqual(self.path.stat().st_ino, inode)
        self.assertEqual(self.active(), [str(self.path)])
        self.assert_untouched()
        self.assertEqual(self.calls("swapon"), ["swapon --show=NAME --noheadings"])

    def test_unknown_active_set_is_treated_as_active(self):
        self.switch("swapon-show-fails")
        inode = self.existing(16)
        result = self.start()
        self.assertIn("treating the swapfile as active", result.stderr)
        self.assertEqual(self.size(), 16 * GIB)
        self.assertEqual(self.path.stat().st_ino, inode)
        self.assert_untouched()

    def test_not_enough_space_keeps_the_old_file(self):
        inode = self.existing(16)
        # 12 GiB plus the 20 GiB headroom, with the old file still on disk.
        (self.root / "avail-gib").write_text("31\n")
        result = self.start()
        self.assertIn("only 31 GiB free; not creating a 12 GiB swapfile", result.stderr)
        self.assertIn("kept the 16 GiB swapfile for this boot", result.stderr)
        self.assert_enabled(16)
        self.assertEqual(self.path.stat().st_ino, inode)
        self.assert_untouched()
        # One GiB more is enough.
        self.boot()
        (self.root / "avail-gib").write_text("32\n")
        self.start()
        self.assert_enabled(12)

    def test_failure_part_way_keeps_the_old_file(self):
        # fallocate stops part way and dd runs out too.
        inode = self.existing(16)
        self.switch("disk-full")
        result = self.start()
        self.assertIn("kept the 16 GiB swapfile for this boot", result.stderr)
        self.assert_enabled(16)
        self.assertEqual(self.path.stat().st_ino, inode)
        self.assertEqual(self.calls("mkswap"), [])
        self.assertEqual(self.calls("swapoff"), [])

    def test_no_file_and_no_space_creates_nothing(self):
        (self.root / "avail-gib").write_text("20\n")
        result = self.start()
        self.assertIn("not creating a 12 GiB swapfile", result.stderr)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.active(), [])
        self.assert_untouched()

    def test_no_file_and_a_failure_part_way_leaves_nothing(self):
        self.switch("disk-full")
        self.start()
        self.assertFalse(self.path.exists())
        self.assertFalse((self.root / "swapfile.new").exists())
        self.assertEqual(self.active(), [])

    def test_runs_twice_and_reboots_without_rewriting(self):
        self.existing(16)
        self.start()
        self.assert_enabled(12)
        inode = self.path.stat().st_ino
        mtime = self.path.stat().st_mtime_ns
        # The same boot again: nothing to do.
        (self.root / "calls.log").unlink()
        self.assertIn("already active", self.start().stderr)
        self.assert_untouched()
        self.assertEqual(self.calls("swapon"), ["swapon --show=NAME --noheadings"])
        # And the next boot only turns it back on.
        self.boot()
        self.assertIn("enabled the existing swapfile", self.start().stderr)
        self.assert_enabled(12)
        self.assertEqual(self.path.stat().st_ino, inode)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)
        self.assert_untouched()

    def test_leftover_of_an_interrupted_replacement_is_removed(self):
        self.existing(16)
        with open(self.root / "swapfile.new", "wb") as f:
            f.truncate(3 * GIB)
        self.start()
        self.assert_enabled(12)

    def test_unusable_right_sized_file_is_rebuilt(self):
        inode = self.existing(12, signed=False)
        result = self.start()
        self.assertIn("existing swapfile is unusable; rebuilding it", result.stderr)
        self.assert_enabled(12)
        self.assertNotEqual(self.path.stat().st_ino, inode)

    def test_without_fallocate_dd_writes_it(self):
        self.switch("fallocate-unsupported")
        self.start()
        self.assert_enabled(12)
        self.assertEqual(len(self.calls("dd")), 1)
        self.assertIn("of=%s.new" % self.path, self.calls("dd")[0])
        self.assertIn("count=12288", self.calls("dd")[0])

    def test_sparse_file_refused_is_rewritten_densely(self):
        self.existing(16)
        self.switch("swapon-refuses-sparse")
        result = self.start()
        self.assertIn("rewriting it with dd", result.stderr)
        self.assert_enabled(12)
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(5), b"DENSE")

    def test_something_else_at_the_path_is_left_alone(self):
        self.path.mkdir()
        result = self.start()
        self.assertIn("not a regular file", result.stderr)
        self.assertTrue(self.path.is_dir())
        self.assert_untouched()

    def test_stop_turns_off_only_an_active_file(self):
        self.existing(12, active=True)
        self.run_text(self.script(), "stop")
        self.assertEqual(self.calls("swapoff"), ["swapoff %s" % self.path])
        self.assertEqual(self.active(), [])
        (self.root / "calls.log").unlink()
        self.run_text(self.script(), "stop")
        self.assertEqual(self.calls("swapoff"), [])


class ZramTest(FakeRoot):
    def setUp(self):
        super().setUp()
        for directory in ("sys/class/zram-control", "dev"):
            (self.root / directory).mkdir(parents=True)
        (self.root / "sys/class/zram-control/hot_add").write_text("1\n")
        (self.root / "sys/class/zram-control/hot_remove").write_text("")
        self.device(0)

    def device(self, index, disksize=0):
        sysfs = self.root / ("sys/block/zram%d" % index)
        sysfs.mkdir(parents=True)
        (sysfs / "disksize").write_text("%d\n" % disksize)
        (sysfs / "comp_algorithm").write_text("[lzo-rle] lzo lz4 zstd\n")
        (self.root / ("dev/zram%d" % index)).write_text("")

    def disksize(self, index=0):
        return int((self.root / ("sys/block/zram%d/disksize" % index)).read_text())

    def script(self):
        text = ZRAM.read_text()
        pattern = re.compile(r"/proc/meminfo|/sys/|/dev/zram")
        return pattern.sub(lambda match: str(self.root) + match.group(0), text)

    def start(self):
        return self.run_text(self.script(), "start")

    def test_four_gib_at_most_half_the_ram_below(self):
        cases = ((SM_X910_16GB, 4096), (SM_X710_12GB, 4096),
                 (8 * 1048576, 4096), (ESTIMATE_8GB, 3515), (2 * 1048576, 1024))
        for kb, mib in cases:
            with self.subTest(kb=kb):
                shutil.rmtree(self.root / "sys/block")
                (self.root / "dev/zram0").unlink()
                self.device(0)
                (self.root / "swaps").write_text("")
                self.ram(kb)
                result = self.start()
                self.assertEqual(self.disksize(), mib * MIB)
                self.assertIn("enabled %s/dev/zram0, %d MiB, zstd" % (self.root, mib),
                              result.stderr)

    def test_unreadable_meminfo_uses_the_cap(self):
        (self.root / "proc/meminfo").unlink()
        result = self.start()
        self.assertIn("cannot read MemTotal; using 4096 MiB", result.stderr)
        self.assertEqual(self.disksize(), 4096 * MIB)

    def test_algorithm_and_priority_are_unchanged(self):
        self.start()
        self.assertEqual((self.root / "sys/block/zram0/comp_algorithm").read_text(), "zstd\n")
        self.assertIn("mkswap -U clear %s/dev/zram0" % self.root, self.calls())
        self.assertIn("swapon --priority 100 %s/dev/zram0" % self.root, self.calls())

    def test_a_zram_device_in_use_is_never_reset(self):
        # zram0 is already sized (the old 8 GiB, holding pages): start takes a
        # new device and leaves zram0 alone.
        (self.root / "sys/block/zram0/disksize").write_text("%d\n" % (8 * GIB))
        self.device(1)
        self.start()
        self.assertEqual(self.disksize(0), 8 * GIB)
        self.assertEqual(self.disksize(1), 4096 * MIB)
        self.assertFalse((self.root / "sys/block/zram0/reset").exists())


class PackageTest(unittest.TestCase):
    def test_upgrades_never_touch_live_swap(self):
        # Stopping or restarting either unit swaps off a tier that may hold
        # pages the RAM cannot take back; the new sizes wait for the next boot.
        for script in (POSTINST, PRERM):
            text = script.read_text().replace("\\\n", " ")
            with self.subTest(script=script.name):
                self.assertNotIn("swapoff", text)
                self.assertNotIn("ubuntu-gts9u-zram stop", text)
                self.assertNotIn("ubuntu-gts9u-swapfile stop", text)
                for line in text.splitlines():
                    if "systemctl" in line and re.search(r"ubuntu-gts9u-(zram|swapfile)", line):
                        self.assertNotRegex(line, r"\b(stop|restart|try-restart|reload|"
                                                  r"reload-or-restart|kill|isolate)\b", line)

    def test_units_are_enabled_where_they_were(self):
        postinst = POSTINST.read_text()
        self.assertIn("\nubuntu-gts9u-zram.service:basic.target\n", postinst)
        self.assertIn("\nubuntu-gts9u-swapfile.service:multi-user.target\n", postinst)


if __name__ == "__main__":
    unittest.main()
