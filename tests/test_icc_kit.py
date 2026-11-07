"""Run the in-place interconnect kit (tools/inplace/icc) against a fake root.

install.sh and rollback.sh normally run as root on the tablet; here every
absolute path they touch is moved under a temporary directory, and systemctl,
dpkg-query, flock, chgrp, install's ownership flags and id are stubbed.  The
kit itself is assembled by make-kit.sh exactly as for the tablet, so the
checksum gate is exercised too.
"""
import os
import pathlib
import re
import subprocess
import tempfile
import unittest


REPO = pathlib.Path(__file__).resolve().parents[1]
KIT_SRC = REPO / "tools/inplace/icc"
NAME = "ubuntu-gts9u-icc-late-release"

PREFIXES = (
    "/var/log/", "/var/backups/", "/var/lib/ubuntu-gts9u/", "/usr/libexec/", "/usr/lib/systemd/",
    "/etc/systemd/", "/etc/ubuntu-gts9u", "/run/", "/proc/device-tree/",
    "/proc/sys/kernel/random/boot_id",
)


class KitTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = pathlib.Path(directory.name)
        self.root = self.tmp / "root"
        self.kit = self.tmp / "kit"
        subprocess.run(["bash", str(KIT_SRC / "make-kit.sh"), str(self.kit)],
                       check=True, capture_output=True, text=True)
        (self.root / "proc/device-tree").mkdir(parents=True)
        (self.root / "proc/device-tree/compatible").write_bytes(b"samsung,gts9wifi\0qcom,sm8550\0")
        (self.root / "proc/sys/kernel/random").mkdir(parents=True)
        (self.root / "proc/sys/kernel/random/boot_id").write_text("df92b567-0000\n")
        for d in ("var/log", "var/backups", "var/lib/ubuntu-gts9u", "usr/libexec",
                  "usr/lib/systemd/system", "etc/systemd/system", "run"):
            (self.root / d).mkdir(parents=True, exist_ok=True)

    @property
    def helper(self):
        return self.root / "usr/libexec" / NAME

    @property
    def unit(self):
        return self.root / "usr/lib/systemd/system" / f"{NAME}.service"

    @property
    def wants(self):
        return self.root / "etc/systemd/system/multi-user.target.wants" / f"{NAME}.service"

    def run_script(self, name, *args):
        script = (self.kit / name).read_text()
        for prefix in PREFIXES:
            script = script.replace(prefix, f"{self.root}{prefix}")
        # Fed on stdin, $0 is the shell: point the kit directory at the kit.
        kit_line = 'kit=$(cd "$(dirname "$0")" && pwd)'
        if kit_line in script:
            script = script.replace(kit_line, f"kit='{self.kit}'", 1)
        r = self.root
        prelude = f"""
id() {{ echo 0; }}
flock() {{ return 0; }}
chgrp() {{ :; }}
install() {{
    mode= src= dst=
    while [ $# -gt 0 ]; do
        case "$1" in
        -o|-g) shift ;;
        -m) mode=$2; shift ;;
        -D) ;;
        *) if [ -z "$src" ]; then src=$1; else dst=$1; fi ;;
        esac
        shift
    done
    mkdir -p "$(dirname "$dst")" && cp "$src" "$dst" && chmod "$mode" "$dst"
}}
systemctl() {{
    case "$1" in
    is-active) return 1 ;;
    daemon-reload) return 0 ;;
    is-enabled)
        if [ -e '{r}/masked' ]; then echo masked
        elif [ -L '{self.wants}' ]; then echo enabled
        else echo disabled; fi ;;
    enable) mkdir -p '{self.wants.parent}' && ln -sf '{self.unit}' '{self.wants}' ;;
    disable) rm -f '{self.wants}' ;;
    mask) : > '{r}/masked' ;;
    restart)
        mkdir -p '{r}/run/ubuntu-gts9u'
        printf 'result=released\\nreleased=24100000.interconnect interconnect-1\\nintmax_after=0\\n' \\
            > '{r}/run/ubuntu-gts9u/icc-release' ;;
    esac
}}
set -- {' '.join(args)}
"""
        # dash cannot name a function dpkg-query: a stub on PATH instead.
        stubs = self.tmp / "bin"
        stubs.mkdir(exist_ok=True)
        stub = stubs / "dpkg-query"
        stub.write_text(f"#!/bin/sh\n[ -e '{r}/owned' ] || exit 1\n"
                        "echo \"ubuntu-gts9u-device: $2\"\n")
        stub.chmod(0o755)
        env = dict(os.environ, PATH=f"{stubs}:{os.environ['PATH']}")
        return subprocess.run(["/bin/sh", "-s"], input=prelude + script, cwd=self.kit,
                              env=env, text=True, capture_output=True, timeout=60)

    def log(self):
        return (self.root / "var/log/gts9u-icc-kit.log").read_text()

    def test_install_enables_and_matches_the_package_files(self):
        r = self.run_script("install.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        pkg = REPO / "packaging/ubuntu-gts9u-device"
        self.assertEqual(self.helper.read_bytes(),
                         (pkg / "usr/libexec" / NAME).read_bytes())
        self.assertEqual(self.unit.read_bytes(),
                         (pkg / "usr/lib/systemd/system" / f"{NAME}.service").read_bytes())
        self.assertTrue(self.helper.stat().st_mode & 0o111)
        self.assertTrue(self.wants.is_symlink())
        self.assertTrue((self.root / "var/backups/ubuntu-gts9u-icc/complete").exists())
        self.assertIn("enabled: enabled", self.log())
        self.assertNotIn("releasing on this boot", self.log())
        # The off switch's directory exists, so a plain touch works.
        self.assertTrue((self.root / "etc/ubuntu-gts9u").is_dir())

    def test_install_now_releases_and_reports(self):
        r = self.run_script("install.sh", "--now")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("this boot: released", self.log())

    def test_checksum_mismatch_changes_nothing(self):
        with (self.kit / "files" / NAME).open("a") as f:
            f.write("# tampered\n")
        r = self.run_script("install.sh")
        self.assertEqual(r.returncode, 1)
        self.assertIn("kit checksum mismatch", self.log())
        self.assertFalse(self.helper.exists())

    def test_other_board_is_refused(self):
        (self.root / "proc/device-tree/compatible").write_bytes(b"samsung,gts9uwifi\0")
        r = self.run_script("install.sh")
        self.assertEqual(r.returncode, 1)
        self.assertFalse(self.helper.exists())

    def test_different_packaged_version_is_not_overwritten(self):
        self.helper.write_text("#!/bin/sh\n# packaged, older\n")
        self.unit.write_text("[Unit]\n")
        (self.root / "owned").write_text("")
        r = self.run_script("install.sh")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ships a different", self.log())
        self.assertEqual(self.helper.read_text(), "#!/bin/sh\n# packaged, older\n")

    def test_rollback_removes_what_install_added(self):
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        r = self.run_script("rollback.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.helper.exists())
        self.assertFalse(self.unit.exists())
        self.assertFalse(self.wants.exists())
        self.assertFalse((self.root / "var/backups/ubuntu-gts9u-icc").exists())
        self.assertTrue(any(p.name.startswith("ubuntu-gts9u-icc.rolled-back-")
                            for p in (self.root / "var/backups").iterdir()))

    def test_rollback_restores_an_earlier_unowned_copy(self):
        self.helper.write_text("#!/bin/sh\n# earlier kit\n")
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        self.assertEqual(self.run_script("rollback.sh").returncode, 0)
        self.assertEqual(self.helper.read_text(), "#!/bin/sh\n# earlier kit\n")
        self.assertFalse(self.unit.exists())

    def test_rollback_masks_a_packaged_unit(self):
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        (self.root / "owned").write_text("")
        r = self.run_script("rollback.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.root / "masked").exists())
        self.assertTrue(self.helper.exists())


    def test_rollback_clears_the_loop_breaker_marker(self):
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        marker = self.root / "var/lib/ubuntu-gts9u/icc-release.pending"
        marker.write_text("boot_id=df92b567-0000\nuptime=33.9\n")
        r = self.run_script("rollback.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(marker.exists())
        self.assertIn("removed the loop-breaker marker", self.log())

    def test_rollback_of_a_packaged_unit_clears_the_marker_too(self):
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        (self.root / "owned").write_text("")
        marker = self.root / "var/lib/ubuntu-gts9u/icc-release.pending"
        marker.write_text("boot_id=df92b567-0000\nuptime=33.9\n")
        self.assertEqual(self.run_script("rollback.sh").returncode, 0)
        self.assertFalse(marker.exists())


class VerifyTest(unittest.TestCase):
    """verify.sh against a fake root: the lines the morning plan quotes."""

    BOOT = "df92b567-453c-4cfe-a950-578d4d595476"

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = pathlib.Path(directory.name)
        self.root = r = self.tmp / "root"
        kit = self.tmp / "kit"
        subprocess.run(["bash", str(KIT_SRC / "make-kit.sh"), str(kit)],
                       check=True, capture_output=True, text=True)
        self.script = (kit / "verify.sh").read_text()
        (r / "proc/device-tree").mkdir(parents=True)
        (r / "proc/device-tree/compatible").write_bytes(b"samsung,gts9wifi\0qcom,sm8550\0")
        (r / "proc/sys/kernel/random").mkdir(parents=True)
        (r / "proc/sys/kernel/random/boot_id").write_text(self.BOOT + "\n")
        (r / "proc/uptime").write_text("95.20 10.00\n")
        (r / "proc/cmdline").write_text("root=LABEL=UBTS9_UFS initcall_blacklist=bwmon_driver_init\n")
        devs = r / "sys/bus/platform/devices"
        for name, val in (("24100000.interconnect", "1"), ("interconnect-1", "1"),
                          ("17a00000.rsc:power-controller", "1"),
                          ("100000.clock-controller", "1"), ("3d90000.clock-controller", "1"),
                          ("aaf0000.clock-controller", "1")):
            (devs / name).mkdir(parents=True)
            (devs / name / "state_synced").write_text(val + "\n")
        for name in ("24091000.pmu", "240b6400.pmu"):
            (devs / name).mkdir(parents=True)
        (r / "sys/power/suspend_stats").mkdir(parents=True)
        (r / "sys/power/suspend_stats/success").write_text("1\n")
        (r / "sys/power/suspend_stats/fail").write_text("0\n")
        (r / "sys/power/mem_sleep").write_text("s2idle [deep]\n")
        (r / "usr/libexec").mkdir(parents=True)
        helper = r / "usr/libexec" / NAME
        helper.write_text("#!/bin/sh\n")
        helper.chmod(0o755)
        (r / "run/ubuntu-gts9u").mkdir(parents=True)
        (r / "run/ubuntu-gts9u/icc-release").write_text(
            f"boot_id={self.BOOT}\nuptime=33.96\nresult=released\nreason=x\n"
            "released=24100000.interconnect interconnect-1\nintmax_before=43\nintmax_after=0\n")
        (r / "var/lib/ubuntu-gts9u").mkdir(parents=True)
        (r / "var/log").mkdir(parents=True)
        (r / "var/log/ubuntu-gts9u-icc-release.log").write_text("2026-11-08 08:00:00 up=33.96 releasing\n")
        (r / "var/log/kern.log").write_text(
            "2026-11-08T08:00:00.1-08:00 squirtle kernel: msm_dpu ae01000.display-controller: ok\n")
        (r / "var/log/syslog").write_text(
            f"2026-11-08T08:00:00.2-08:00 squirtle kernel: {NAME}: releasing 24100000.interconnect interconnect-1"
            " (blocked only by the unbound DDR bandwidth monitors)\n"
            f"2026-11-08T08:00:00.3-08:00 squirtle kernel: {NAME}: released 24100000.interconnect"
            " interconnect-1; INT_MAX nodes 43 -> 0\n")

    def run_verify(self):
        # One pass, so /proc/sys/... is not rewritten twice.
        script = re.sub(r"(?<![\w.])/(?:proc|sys|run|var/log|var/lib|usr/libexec|etc)/",
                        lambda m: f"{self.root}{m.group(0)}", self.script)
        prelude = f"""
systemctl() {{
    case "$*" in
    is-enabled*) echo enabled ;;
    *ActiveState*coldboot*|*coldboot*ActiveState*) echo active ;;
    *Result*coldboot*|*coldboot*Result*) echo success ;;
    *ExecMainExitTimestampMonotonic*) echo 31910936 ;;
    *ExecMainStartTimestampMonotonic*) echo 33950000 ;;
    *ActiveState*) echo active ;;
    *Result*) echo success ;;
    *ConditionResult*) echo yes ;;
    esac
}}
journalctl() {{ echo '[   39.000000] squirtle kernel: first retained line'; }}
uname() {{ echo 7.2.0-rc3; }}
"""
        stubs = self.tmp / "bin"
        stubs.mkdir(exist_ok=True)
        (stubs / "dpkg-query").write_text("#!/bin/sh\nexit 1\n")
        (stubs / "dpkg-query").chmod(0o755)
        env = dict(os.environ, PATH=f"{stubs}:{os.environ['PATH']}")
        return subprocess.run(["/bin/sh", "-s"], input=prelude + script, env=env,
                              text=True, capture_output=True, timeout=30)

    def test_released_boot(self):
        r = self.run_verify()
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout
        self.assertIn("ok     release ran at 33 s, after the panel cycle returned at 31 s", out)
        self.assertIn("ok     released 24100000.interconnect interconnect-1 at 33.96 s; INT_MAX nodes 43 -> 0", out)
        self.assertIn("ok     no interconnect node left at INT_MAX", out)
        self.assertIn("ok     state_synced: 24100000.interconnect=1 interconnect-1=1", out)
        self.assertIn("ok     untouched providers: rpmhpd=1 gcc=1 gpu_cc=1 videocc=1", out)
        # The /dev/kmsg markers come from syslog, where rsyslog files them.
        self.assertIn(f"kernel: {NAME}: released 24100000.interconnect interconnect-1; INT_MAX nodes 43 -> 0", out)

    def test_loop_breaker_lines(self):
        (self.root / "var/lib/ubuntu-gts9u/icc-release.pending").write_text(
            f"boot_id={self.BOOT}\nuptime=33.96\n")
        out = self.run_verify().stdout
        self.assertIn("ok     loop breaker armed for this boot", out)
        (self.root / "var/lib/ubuntu-gts9u/icc-release.pending").write_text(
            "boot_id=aaaaaaaa-1111\nuptime=33.96\n")
        out = self.run_verify().stdout
        self.assertIn("CHECK  loop breaker: a marker from boot aaaaaaaa is waiting", out)
        (self.root / "var/lib/ubuntu-gts9u/icc-release.pending").unlink()
        (self.root / "var/lib/ubuntu-gts9u/icc-release.unclean").write_text(
            f"boot_id=aaaaaaaa-1111\nuptime=33.10\nheld_boot={self.BOOT}\n")
        out = self.run_verify().stdout
        self.assertIn("CHECK  loop breaker held the floor on this boot: boot aaaaaaaa released it", out)


if __name__ == "__main__":
    unittest.main()
