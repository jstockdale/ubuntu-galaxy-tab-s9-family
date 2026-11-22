"""install.sh / rollback.sh / verify.sh against a fake root, house style.

The scripts' root='' line is pointed at a temporary tree, and the root-only
commands (id, flock, systemctl, install -o/-g) are stubbed on PATH.  The
sampler's read-only 'check' runs for real against the fake /proc and /sys."""
import hashlib
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
KIT = HERE.parent / "tools" / "inplace" / "stall-watch"
sys.path.insert(0, str(HERE))
from stall_watch_fakeroot import FakeRoot  # noqa: E402

SCRIPTS = ("install.sh", "rollback.sh", "verify.sh")

STUBS = {
    "id": "#!/bin/sh\necho 0\n",
    "flock": "#!/bin/sh\nexit 0\n",
    "systemctl": r"""#!/bin/sh
echo "$*" >> "$TEST_ROOT/systemctl.calls"
case "$1" in
restart)
	mkdir -p "$TEST_ROOT/run/gts9u-stall-watch"
	now=$(date +%s)
	printf '{"version": "1", "pid": %s, "started": %s, "uptime_s": 80, "updated": %s, "samples": 3, "missed": 0, "trigger_wakeups": 0, "trigger_unconfirmed": 0, "cpu_s": 0.1, "cpu_pct": 0.3, "late_ms": {"p50": 0.2, "p99": 1.1, "max": 2.0}, "work_ms": {"p50": 1.0, "p99": 2.0, "max": 3.0}, "incidents": 0, "minor_episodes": 0, "last_incident": null, "incident_open": false, "kmsg_lines": 0, "ring_bytes_written": 0, "queue_dropped": 0, "errors": 0, "last_error": null, "triggers": {"memory": "armed: some 150000 1000000"}, "kmsg": "reading", "mlock": "ok", "vmlck_kb": 26000, "rss_kb": 28000, "compositor": 2924, "interval_ms": 1000, "marks": 0, "slow_reads": {}}\n' "$TEST_PID" "$now" "$now" > "$TEST_ROOT/run/gts9u-stall-watch/status.json" ;;
is-enabled) echo enabled ;;
is-active) echo active ;;
show) echo "$TEST_PID" ;;
esac
exit 0
""",
    # install -o/-g need root; drop them, keep the rest.
    "install": "#!/bin/sh\nfor a; do shift; case \"$skip$a\" in -o|-g) skip=1; continue ;;"
               " 1*) skip=; continue ;; esac; set -- \"$@\" \"$a\"; done\n"
               "exec /usr/bin/install \"$@\"\n",
}


def sha(p):
    return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()


class KitScripts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.root = base / "root"
        self.fake = FakeRoot(self.root)
        for d in ("run/systemd/system", "var/log", "etc/systemd/system", "usr/local/bin", "var/lib", "var/backups"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self.kit = base / "kit"
        shutil.copytree(KIT, self.kit)
        for name in SCRIPTS:
            p = self.kit / name
            text = p.read_text()
            self.assertEqual(text.count("\nroot=''\n"), 1, name)
            p.write_text(text.replace("\nroot=''\n", "\nroot='%s'\n" % self.root))
        self.reseal()
        bin_dir = base / "bin"
        bin_dir.mkdir()
        for name, text in STUBS.items():
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        self.env = dict(os.environ, PATH="%s:%s" % (bin_dir, os.environ["PATH"]), TEST_ROOT=str(self.root),
                        TEST_PID=str(os.getpid()))

    def tearDown(self):
        self.tmp.cleanup()

    def reseal(self):
        files = sorted(p for p in self.kit.iterdir() if p.is_file() and p.name != "SHA256SUMS")
        (self.kit / "SHA256SUMS").write_text("".join("%s  %s\n" % (sha(p), p.name) for p in files))

    def sh(self, name, *args):
        return subprocess.run(["/bin/sh", str(self.kit / name)] + list(args), env=self.env, capture_output=True,
                              text=True, timeout=120)

    def r(self, path):
        return self.root / path.lstrip("/")

    def installed_files(self):
        return {
            "/usr/local/lib/gts9u-stall-watch/stall_watch.py": "stall_watch.py",
            "/usr/local/lib/gts9u-stall-watch/README.md": "README.md",
            "/usr/local/bin/stall-watch": "stall-watch",
            "/etc/systemd/system/gts9u-stall-watch.service": "gts9u-stall-watch.service",
            "/etc/gts9u-stall-watch.conf": "gts9u-stall-watch.conf",
        }

    # ------------------------------------------------------------------
    def test_shell_syntax(self):
        for name in SCRIPTS + ("stall-watch",):
            for shell in ("dash", "sh"):
                with self.subTest(name=name, shell=shell):
                    r = subprocess.run([shell, "-n", str(KIT / name)], capture_output=True, text=True)
                    self.assertEqual(r.returncode, 0, r.stderr)

    def test_shipped_checksums(self):
        r = subprocess.run(["sha256sum", "-c", "--quiet", "SHA256SUMS"], cwd=KIT, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        listed = sorted(l.split()[1] for l in (KIT / "SHA256SUMS").read_text().splitlines())
        present = sorted(p.name for p in KIT.iterdir() if p.is_file() and p.name != "SHA256SUMS")
        self.assertEqual(listed, present)

    def test_install_upgrade_rollback(self):
        r = self.sh("install.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("checks passed", r.stdout)
        self.assertIn("is running and has taken 3 samples", r.stdout)
        for dst, src in self.installed_files().items():
            with self.subTest(dst=dst):
                self.assertEqual(self.r(dst).read_bytes(), (self.kit / src).read_bytes())
        self.assertEqual(self.r("/usr/local/bin/stall-watch").stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.r("/etc/systemd/system/gts9u-stall-watch.service").stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.r("/usr/local/lib/gts9u-stall-watch/stall_watch.py").stat().st_mode & 0o777, 0o644)
        self.assertFalse(list(self.root.rglob("*.stall-watch-new")))
        data = self.r("/var/log/gts9u-stall-watch")
        self.assertEqual(data.stat().st_mode & 0o7777, 0o2750)
        self.assertIn("rev=1", self.r("/var/lib/gts9u-stall-watch/installed").read_text())
        calls = self.r("/systemctl.calls").read_text()
        self.assertIn("daemon-reload", calls)
        self.assertIn("enable gts9u-stall-watch.service", calls)
        self.assertIn("restart gts9u-stall-watch.service", calls)
        self.assertNotIn("stop", calls.replace("--now", ""))   # nothing else is touched
        self.assertEqual(sorted(set(l.split()[0] for l in calls.splitlines())), ["daemon-reload", "enable", "restart"])

        # something recorded, then an upgrade from an earlier revision: the recording stays
        (data / "incidents/20261122-135717-memory").mkdir(parents=True)
        (data / "incidents/20261122-135717-memory/summary.json").write_text("{}")
        self.r("/var/lib/gts9u-stall-watch/installed").write_text("rev=0\n")
        prog = self.kit / "stall_watch.py"
        prog.write_text(prog.read_text() + "\n# revision 2\n")
        self.reseal()
        r = self.sh("install.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("upgrade on SM-X910: revision 0 -> 1", r.stdout)
        self.assertTrue(self.r("/usr/local/lib/gts9u-stall-watch/stall_watch.py").read_text().endswith("# revision 2\n"))
        self.assertTrue((data / "incidents/20261122-135717-memory/summary.json").exists())

        # an edited configuration survives an upgrade; the new defaults go next to it
        conf = self.r("/etc/gts9u-stall-watch.conf")
        conf.write_text("late_ms = 300\n")
        c = self.kit / "gts9u-stall-watch.conf"
        c.write_text(c.read_text() + "# new default\n")
        self.reseal()
        r = self.sh("install.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(conf.read_text(), "late_ms = 300\n")
        self.assertEqual(self.r("/etc/gts9u-stall-watch.conf.dist").read_text(), c.read_text())

        # rollback --keep-data: everything but the recordings goes; the edited conf is saved
        r = self.sh("rollback.sh", "--keep-data")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for dst in self.installed_files():
            self.assertFalse(self.r(dst).exists(), dst)
        self.assertFalse(self.r("/usr/local/lib/gts9u-stall-watch").exists())
        self.assertFalse(self.r("/etc/gts9u-stall-watch.conf.dist").exists())
        self.assertFalse(self.r("/var/lib/gts9u-stall-watch").exists())
        self.assertFalse(self.r("/run/gts9u-stall-watch").exists())
        self.assertTrue((data / "incidents/20261122-135717-memory/summary.json").exists())
        saved = list(self.r("/var/backups").glob("gts9u-stall-watch.conf.removed-*"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_text(), "late_ms = 300\n")
        self.assertIn("disable --now gts9u-stall-watch.service", self.r("/systemctl.calls").read_text())

        # install again over the kept data, then a full rollback removes it
        r = self.sh("install.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("install revision 1", r.stdout)
        r = self.sh("rollback.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(data.exists())
        # and a second rollback is harmless
        r = self.sh("rollback.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_same_revision_twice_is_fine(self):
        for _ in range(2):
            r = self.sh("install.sh")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("is current", r.stdout)

    def test_x710_is_accepted(self):
        self.fake.put("/proc/device-tree/compatible", b"qcom,kalama-mtp\0qcom,kalama\0qcom,mtp\0samsung,gts9wifi\0qcom,sm8550\0")
        r = self.sh("install.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("on SM-X710", r.stdout)

    def test_refuses_other_boards(self):
        self.fake.put("/proc/device-tree/compatible", b"qcom,sm8650-qrd\0qcom,sm8650\0")
        r = self.sh("install.sh")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("neither an SM-X910 nor an SM-X710", r.stdout)
        self.assertFalse(self.r("/usr/local/lib/gts9u-stall-watch").exists())
        self.assertFalse(self.r("/systemctl.calls").exists())
        env = dict(self.env, STALL_WATCH_ANY_BOARD="1")
        r = subprocess.run(["/bin/sh", str(self.kit / "install.sh")], env=env, capture_output=True, text=True,
                           timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_refuses_a_changed_kit(self):
        p = self.kit / "stall_watch.py"
        p.write_text(p.read_text() + "\n# tampered\n")
        r = self.sh("install.sh")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("does not match its checksums", r.stdout)
        self.assertFalse(self.r("/usr/local/lib/gts9u-stall-watch").exists())
        self.assertFalse(self.r("/systemctl.calls").exists())

    def test_refuses_when_the_sampler_cannot_run(self):
        p = self.kit / "stall_watch.py"
        p.write_text(p.read_text().replace('if args.cmd == "check":', 'if args.cmd == "check":\n        raise SystemExit(3)'))
        self.reseal()
        r = self.sh("install.sh")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("the sampler could not read this system", r.stdout)
        self.assertFalse(self.r("/usr/local/lib/gts9u-stall-watch").exists())

    def test_rollback_without_install(self):
        r = self.sh("rollback.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.sh("rollback.sh", "--bogus")
        self.assertEqual(r.returncode, 2)

    def test_verify_reports(self):
        r = self.sh("install.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.sh("verify.sh")
        out = r.stdout
        self.assertIn("ok     gts9u-stall-watch.service enabled", out)
        self.assertIn("ok     gts9u-stall-watch.service active", out)
        self.assertIn("ok     cost 0.3 % of one CPU", out)
        self.assertIn("ok     wake-up lateness p99 1.1 ms", out)
        self.assertIn("ok     PSI memory trigger armed", out)
        self.assertIn("ok     reading the kernel log live", out)
        self.assertIn("ok     stall-watch status", out)
        self.assertIn("ok     stall-watch list", out)
        # the test runner is not a real-time, memory-locked process: verify must say so
        self.assertIn("CHECK  main thread policy", out)
        self.assertNotEqual(r.returncode, 0)


class KitInTheRepository(unittest.TestCase):
    def test_the_kit_matches_its_checksums(self):
        sums = (KIT / "SHA256SUMS").read_text().split("\n")
        names = set()
        for line in filter(None, sums):
            digest, name = line.split(None, 1)
            names.add(name)
            self.assertEqual(hashlib.sha256((KIT / name).read_bytes()).hexdigest(), digest, name)
        self.assertEqual(names, {p.name for p in KIT.iterdir() if p.is_file()} - {"SHA256SUMS"})

    def test_the_unit_keeps_dev_kmsg_open(self):
        """ProtectClock= and PrivateDevices= imply a DeviceAllow= list, and with
        one systemd allows only the standard pseudo devices: opening /dev/kmsg
        would fail and the kernel lines of a stall would be missing."""
        unit = (KIT / "gts9u-stall-watch.service").read_text()
        keys = {line.split("=", 1)[0] for line in unit.split("\n") if "=" in line and not line.startswith("#")}
        self.assertFalse(keys & {"ProtectClock", "PrivateDevices", "DeviceAllow", "DevicePolicy", "ProtectKernelLogs"})
        self.assertIn("CPUSchedulingPolicy", keys)


if __name__ == "__main__":
    unittest.main()
