"""Offline test of tools/inplace/sensors-hook: make-kit.sh, install.sh,
rollback.sh, verify.sh and cycle.sh.

make-kit.sh packs a kit from this checkout into a scratch directory.  The
tablet scripts run against a scratch root (their root= line points there),
with the root-only and system commands stubbed on PATH: id, flock, fuser,
chgrp, sync, dpkg-query, systemctl (a small state machine), install (its -o
and -g recorded and dropped), stat (root:root), gdbus, journalctl (canned
journals) and systemd-run (runs a stand-in systemd-sleep that runs the
hooks).  The originals in the scratch root are the files the device package
has shipped, read from git, so the kit's list of known versions is checked
against history as well.  Nothing on the host is touched."""
import hashlib
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
KIT_SRC = REPO / "tools/inplace/sensors-hook"
PKG = REPO / "packaging/ubuntu-gts9u-device"
X710 = b"qcom,kalama-mtp\0qcom,kalama\0qcom,mtp\0samsung,gts9wifi\0qcom,sm8550\0"
X910 = b"qcom,kalama-mtp\0qcom,kalama\0samsung,gts9uwifi\0qcom,sm8550\0"
HOOK = "usr/lib/systemd/system-sleep/ubuntu-gts9u-sensors"
SCRIPT = "usr/libexec/ubuntu-gts9u-sensors-resume"
UNIT = "usr/lib/systemd/system/ubuntu-gts9u-sensors-resume.service"
FILES = {HOOK: 0o755, SCRIPT: 0o755, UNIT: 0o644}
# The versions the two test units had on 2026-11-24 (device 2.69 and 2.72):
# the hook from before ac38911, the script and unit of a4d6aa0.
ORIGINALS = {HOOK: "ac38911^", SCRIPT: "a4d6aa0", UNIT: "a4d6aa0"}

STUBS = {
    "id": "#!/bin/sh\necho 0\n",
    "flock": "#!/bin/sh\nexit 0\n",
    "chgrp": "#!/bin/sh\nexit 0\n",
    "sync": "#!/bin/sh\nexit 0\n",
    "fuser": "#!/bin/sh\n[ -e \"$TEST_ROOT/dpkg-busy\" ]\n",
    "dpkg-query": """#!/bin/sh
case "$1" in
-W)
	[ -e "$TEST_ROOT/pkg-version" ] || exit 1
	case "$3" in
	*Status*) printf 'install ok installed %s' "$(cat "$TEST_ROOT/pkg-version")" ;;
	*) printf '%s' "$(cat "$TEST_ROOT/pkg-version")" ;;
	esac ;;
-S)
	if [ -e "$TEST_ROOT/owner-other" ]; then echo "another-package: $2"; else echo "ubuntu-gts9u-device: $2"; fi ;;
esac
""",
    "systemctl": """#!/bin/sh
echo "$*" >> "$TEST_ROOT/systemctl.calls"
s=$TEST_ROOT/state
mkdir -p "$s"
case "$*" in
'show -p LoadState --value '*) echo loaded ;;
'show -p InvocationID --value '*) cat "$s/invocation" 2>/dev/null || echo 0 ;;
'show -p Result --value '*) cat "$s/result" 2>/dev/null || echo success ;;
'show -p NRestarts --value '*) echo 0 ;;
show*) cat "$TEST_ROOT/props" 2>/dev/null ||
	printf '%s\\n' Restart=on-failure RestartUSec=30s StartLimitBurst=3 StartLimitIntervalUSec=15min \\
		TimeoutStopUSec=5s NeedDaemonReload=no ActiveState=inactive SubState=dead Result=success NRestarts=0 ;;
'is-active '*) cat "$s/$2" 2>/dev/null || echo inactive ;;
'stop '*) echo inactive > "$s/$2" ;;
'restart --no-block '*)
	# The resume unit runs at once and brings the proxy back, unless the
	# test says it fails.
	n=$(cat "$s/invocation" 2>/dev/null || echo 0)
	echo $((n + 1)) > "$s/invocation"
	if [ -e "$TEST_ROOT/resume-fails" ]; then echo failed > "$s/$3"; echo exit-code > "$s/result"
	else echo inactive > "$s/$3"; echo active > "$s/iio-sensor-proxy.service"; echo success > "$s/result"; fi ;;
esac
exit 0
""",
    # install -o/-g need root: record them, drop them, keep the rest.
    "install": """#!/bin/sh
echo "$*" >> "$TEST_ROOT/install.calls"
for a; do shift; case "$skip$a" in -o|-g) skip=1; continue ;; 1*) skip=; continue ;; esac; set -- "$@" "$a"; done
exec /usr/bin/install "$@"
""",
    "stat": "#!/bin/sh\n[ \"$1\" = -c ] || exec /usr/bin/stat \"$@\"\necho \"$(/usr/bin/stat -c %a \"$3\") root:root\"\n",
    "gdbus": "#!/bin/sh\nprintf '      readonly b HasAccelerometer = true;\\n      readonly b HasAmbientLight = true;\\n"
             "      readonly d LightLevel = 12.0;\\n'\n",
    # journalctl -o FMT -t ID...: the canned journal journal.FMT.ID (the first
    # ID), with @NOW@ replaced by the time now.
    "journalctl": """#!/bin/sh
fmt= id=
while [ $# -gt 0 ]; do
	case "$1" in -o) fmt=$2; shift ;; -t) [ -n "$id" ] || id=$2; shift ;; esac
	shift
done
f=$TEST_ROOT/journal.$fmt.$id
[ -e "$f" ] || exit 0
sed "s/@NOW@/$(date +%s.%N)/" "$f"
""",
    # systemd-run --wait ... BINARY ARGS: run BINARY ARGS here.
    "systemd-run": """#!/bin/sh
echo "$*" >> "$TEST_ROOT/systemd-run.calls"
while [ $# -gt 0 ]; do case "$1" in --*) shift ;; *) break ;; esac; done
exec "$@"
""",
}

# A stand-in for systemd-sleep: every hook's pre, what the sleep would see,
# every hook's post.
SYSTEMD_SLEEP = """#!/bin/sh
for h in "$TEST_ROOT"/usr/lib/systemd/system-sleep/*; do sh "$h" pre "$1"; done
cat "$TEST_ROOT/sys/power/pm_test" > "$TEST_ROOT/pm_test.during"
cat "$TEST_ROOT"/run/systemd/sleep.conf.d/* > "$TEST_ROOT/dropin.during" 2>/dev/null
[ ! -e "$TEST_ROOT/sleep-fails" ] || exit 1
for h in "$TEST_ROOT"/usr/lib/systemd/system-sleep/*; do sh "$h" post "$1"; done
"""


def git_show(rev, path):
    r = subprocess.run(["git", "-C", str(REPO), "show", f"{rev}:packaging/ubuntu-gts9u-device/{path}"],
                       capture_output=True)
    return r.stdout if r.returncode == 0 else None


def sha(data):
    return hashlib.sha256(data).hexdigest()


class KitTest(unittest.TestCase):
    def setUp(self):
        self.originals = {rel: git_show(rev, rel) for rel, rev in ORIGINALS.items()}
        if None in self.originals.values():
            self.skipTest("the shipped versions are not in this checkout's history")
        self.tmpdir = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmpdir.name)
        self.root = base / "root"
        self.kit = base / "kit"
        made = subprocess.run(["sh", str(KIT_SRC / "make-kit.sh"), str(self.kit)], capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(made.returncode, 0, made.stderr)
        for d in ("run", "var/log", "var/backups", "var/lib/dpkg/info", "proc/sys/kernel/random",
                  "proc/device-tree", "sys/power"):
            (self.root / d).mkdir(parents=True)
        (self.root / "proc/device-tree/compatible").write_bytes(X710)
        (self.root / "proc/sys/kernel/random/boot_id").write_text("b00t\n")
        (self.root / "proc/uptime").write_text("1234.5 100.0\n")
        (self.root / "pkg-version").write_text("2.69\n")
        self.package(self.originals)
        bin_dir = base / "bin"
        bin_dir.mkdir()
        for name, text in STUBS.items():
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        sleep_bin = self.root / "usr/lib/systemd/systemd-sleep"
        sleep_bin.write_text(SYSTEMD_SLEEP)
        sleep_bin.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", TEST_ROOT=str(self.root))

    def tearDown(self):
        self.tmpdir.cleanup()

    def package(self, contents):
        """The device package's files as dpkg installed them, and its md5sums."""
        lines = []
        for rel, data in contents.items():
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(FILES[rel])
            lines.append(f"{hashlib.md5(data).hexdigest()}  {rel}\n")
        (self.root / "var/lib/dpkg/info/ubuntu-gts9u-device.md5sums").write_text("".join(lines))

    def script(self, name):
        text = (self.kit / name).read_text()
        self.assertEqual(text.count("\nroot=\n"), 1, name)
        text = text.replace("\nroot=\n", f"\nroot={self.root}\n")
        # rollback.sh needs nothing from the kit.
        kit_line = 'kit=$(cd "$(dirname "$0")" && pwd)'
        self.assertEqual(text.count(kit_line), 0 if name == "rollback.sh" else 1, name)
        return text.replace(kit_line, f"kit={self.kit}")

    def run_kit(self, name, *args):
        return subprocess.run(["sh", "-s", *args], input=self.script(name), text=True, capture_output=True,
                              env=self.env, timeout=120)

    def files(self):
        return {rel: (self.root / rel).read_bytes() for rel in FILES}

    def kit_files(self):
        return {rel: (self.kit / "files" / rel).read_bytes() for rel in FILES}

    def calls(self, name):
        p = self.root / name
        return p.read_text().splitlines() if p.exists() else []

    def reseal(self, revision):
        (self.kit / "REVISION").write_text(f"{revision}\n")
        files = sorted(p for p in self.kit.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
        (self.kit / "SHA256SUMS").write_text("".join(
            f"{sha(p.read_bytes())}  {p.relative_to(self.kit)}\n" for p in files))

    def backup(self):
        return self.root / "var/backups/ubuntu-gts9u-sensors-hook"

    def assert_unchanged(self):
        self.assertEqual(self.files(), self.originals)
        self.assertFalse(self.backup().exists())
        self.assertNotIn("daemon-reload", self.calls("systemctl.calls"))

    # --- make-kit.sh ------------------------------------------------------------

    def test_make_kit_packs_the_package_files_with_their_modes(self):
        for rel, mode in FILES.items():
            with self.subTest(rel=rel):
                self.assertEqual((self.kit / "files" / rel).read_bytes(), (PKG / rel).read_bytes())
                self.assertEqual((self.kit / "files" / rel).stat().st_mode & 0o777, mode)
        self.assertEqual((self.kit / "REVISION").read_text(), "1\n")
        self.assertTrue((self.kit / "SOURCE").read_text().strip())
        check = subprocess.run(["sha256sum", "-c", "--quiet", "SHA256SUMS"], cwd=self.kit, capture_output=True)
        self.assertEqual(check.returncode, 0, check.stdout)
        listed = {line.split()[1] for line in (self.kit / "SHA256SUMS").read_text().splitlines()}
        self.assertEqual(listed, {"README.md", "REVISION", "SOURCE", "cycle.sh", "install.sh", "rollback.sh",
                                  "verify.sh"} | {f"files/{rel}" for rel in FILES})

    def test_the_known_versions_are_the_ones_packages_shipped(self):
        # install.sh's list, against history: every version of the three
        # files the device package has had since the unit and the script took
        # their present form (504ff29, d6a04f9), ac38911's hook included.
        text = (KIT_SRC / "install.sh").read_text()
        block = text[text.index("known='") + 7:]
        known = {tuple(line.split()) for line in block[:block.index("'")].splitlines()}
        shipped = set()
        for rev in ("504ff29", "d6a04f9", "ac38911^", "ac38911", "a4d6aa0"):
            for rel in FILES:
                data = git_show(rev, rel)
                if data is not None and data != (PKG / rel).read_bytes():
                    shipped.add((rel, sha(data)))
        self.assertEqual(known, shipped - {(SCRIPT, sha(git_show("504ff29", SCRIPT)))})
        self.assertIn((HOOK, sha(self.originals[HOOK])), known)

    # --- install, upgrade, rollback ---------------------------------------------

    def test_install_then_an_upgrade_in_place_then_rollback(self):
        r = self.run_kit("install.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.files(), self.kit_files())
        for rel, mode in FILES.items():
            self.assertEqual((self.root / rel).stat().st_mode & 0o777, mode)
            # Nothing left under the hidden names.
            self.assertEqual(sorted(p.name for p in (self.root / rel).parent.iterdir() if p.name.startswith(".")), [])
        # Root-owned: install -o 0 -g 0 for each file.
        installs = self.calls("install.calls")
        self.assertEqual(len(installs), 3)
        for line in installs:
            self.assertTrue(line.startswith("-o 0 -g 0 -m 0"), line)
        # The order: the hook, the script, then the unit and one reload.
        out = r.stdout
        self.assertLess(out.index(f"/{HOOK}: revision 1"), out.index(f"/{SCRIPT}: revision 1"))
        self.assertLess(out.index(f"/{SCRIPT}: revision 1"), out.index(f"/{UNIT}: revision 1"))
        self.assertEqual(self.calls("systemctl.calls").count("daemon-reload"), 1)
        self.assertIn("done: revision 1 in place", out)
        backup = self.backup()
        for rel in FILES:
            self.assertEqual((backup / "files" / rel).read_bytes(), self.originals[rel])
        self.assertEqual((backup / "package").read_text(), "2.69\n")
        self.assertEqual(len((backup / "revisions").read_text().splitlines()), 3)
        self.assertTrue((self.root / "var/log/gts9u-sensors-hook.log").exists())

        # The same revision again: nothing.
        r = self.run_kit("install.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("revision 1 is installed already", r.stdout)

        # Revision 2 over revision 1, in place.
        hook2 = self.kit / "files" / HOOK
        hook2.write_bytes(hook2.read_bytes() + b"# revision 2\n")
        self.reseal(2)
        r = self.run_kit("install.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("upgrade in place: the originals saved", r.stdout)
        self.assertEqual(self.files(), self.kit_files())
        for rel in FILES:
            self.assertEqual((backup / "files" / rel).read_bytes(), self.originals[rel])
        revisions = (backup / "revisions").read_text().splitlines()
        self.assertEqual(sorted({line.split()[0] for line in revisions}), ["1", "2"])

        # Back to the package's files: the unit first, then the script, then
        # the hook.
        r = self.run_kit("rollback.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.files(), self.originals)
        for rel, mode in FILES.items():
            self.assertEqual((self.root / rel).stat().st_mode & 0o777, mode)
        out = r.stdout
        self.assertLess(out.index(f"/{UNIT}: the original back"), out.index(f"/{SCRIPT}: the original back"))
        self.assertLess(out.index(f"/{SCRIPT}: the original back"), out.index(f"/{HOOK}: the original back"))
        self.assertEqual(self.calls("systemctl.calls").count("daemon-reload"), 3)
        self.assertFalse(backup.exists())
        self.assertEqual(len(list(backup.parent.glob("ubuntu-gts9u-sensors-hook.rolled-back-*"))), 1)

    def test_an_install_that_stopped_halfway_is_finished_by_the_next_one(self):
        r = self.run_kit("install.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # As if it had stopped after the hook: the script and unit original.
        for rel in (SCRIPT, UNIT):
            (self.root / rel).write_bytes(self.originals[rel])
        r = self.run_kit("install.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.files(), self.kit_files())
        # And a rollback from such a state restores only what the kit wrote.
        (self.root / UNIT).write_bytes(self.originals[UNIT])
        r = self.run_kit("rollback.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"/{UNIT} holds its original already", r.stdout)
        self.assertEqual(self.files(), self.originals)

    def test_on_the_sm_x910_too(self):
        (self.root / "proc/device-tree/compatible").write_bytes(X910)
        r = self.run_kit("install.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("samsung,gts9uwifi", r.stdout)
        self.assertEqual(self.files(), self.kit_files())

    def test_ac38911_s_hook_is_recognised(self):
        self.originals[HOOK] = git_show("ac38911", HOOK)
        self.package(self.originals)
        r = self.run_kit("install.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.backup() / "files" / HOOK).read_bytes(), self.originals[HOOK])

    def test_refusals_change_nothing(self):
        cases = {
            "board": lambda: (self.root / "proc/device-tree/compatible").write_bytes(b"qcom,sm8550\0"),
            "unknown": lambda: (self.root / HOOK).write_bytes(self.originals[HOOK] + b"# by hand\n"),
            "owner": lambda: (self.root / "owner-other").write_text(""),
            "package": lambda: (self.root / "pkg-version").unlink(),
            "dpkg busy": lambda: (self.root / "dpkg-busy").write_text(""),
            "checksums": lambda: (self.kit / "files" / HOOK).write_bytes(b"#!/bin/sh\nexit 0\n"),
            "missing": lambda: (self.root / UNIT).unlink(),
        }
        said = {
            "board": "this is neither an SM-X710",
            "unknown": "is neither a version a device package has shipped nor a revision of this kit",
            "owner": "is not ubuntu-gts9u-device's (dpkg-query -S says 'another-package')",
            "package": "ubuntu-gts9u-device is not installed",
            "dpkg busy": "dpkg is busy",
            "checksums": "the kit does not match its checksums",
            "missing": "is missing",
        }
        for name, spoil in cases.items():
            with self.subTest(case=name):
                self.tearDown()
                self.setUp()
                spoil()
                before = {rel: (self.root / rel).read_bytes() for rel in FILES if (self.root / rel).exists()}
                r = self.run_kit("install.sh")
                self.assertNotEqual(r.returncode, 0, r.stdout)
                self.assertIn("STOP: ", r.stdout)
                self.assertIn(said[name], r.stdout)
                after = {rel: (self.root / rel).read_bytes() for rel in FILES if (self.root / rel).exists()}
                self.assertEqual(after, before)
                self.assertFalse(self.backup().exists())
                self.assertEqual(self.calls("install.calls"), [])

    def test_a_package_that_carries_this_revision_needs_nothing(self):
        self.package(self.kit_files())
        r = self.run_kit("install.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("carries this revision's files already: nothing to install", r.stdout)
        self.assertFalse(self.backup().exists())
        self.assertEqual(self.calls("install.calls"), [])

    def test_a_package_installed_after_the_kit_takes_over(self):
        r = self.run_kit("install.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # A later package with its own (newer) files: the kit refuses to go
        # over them, and rollback.sh leaves them and retires the backup.
        newer = {rel: data + b"# a later package\n" for rel, data in self.originals.items()}
        (self.root / "pkg-version").write_text("2.75\n")
        self.package(newer)
        r = self.run_kit("install.sh")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.files(), newer)
        r = self.run_kit("rollback.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("left as they are; the backup is retired", r.stdout)
        self.assertEqual(self.files(), newer)
        self.assertFalse(self.backup().exists())

    def test_a_package_reinstalled_with_the_old_files_gets_a_fresh_backup(self):
        r = self.run_kit("install.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # Another version that ships the known files again (an older line's
        # package, say): those are the originals now.
        (self.root / "pkg-version").write_text("2.72\n")
        self.package(self.originals)
        r = self.run_kit("install.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ubuntu-gts9u-device is 2.72 now, not 2.69", r.stdout)
        self.assertEqual((self.backup() / "package").read_text(), "2.72\n")
        self.assertEqual(self.files(), self.kit_files())
        self.assertEqual(len(list(self.backup().parent.glob("ubuntu-gts9u-sensors-hook.package-2.69-*"))), 1)

    def test_rollback_stops_at_a_file_changed_by_hand(self):
        r = self.run_kit("install.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (self.root / SCRIPT).write_bytes(b"#!/bin/sh\n# by hand\n")
        installed = self.files()
        r = self.run_kit("rollback.sh")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn(f"changed by hand?): /{SCRIPT}; nothing was changed", r.stdout)
        self.assertEqual(self.files(), installed)
        self.assertTrue(self.backup().exists())

    def test_rollback_without_an_install_does_nothing(self):
        r = self.run_kit("rollback.sh")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("install.sh never ran", r.stdout)
        self.assert_unchanged()

    # --- verify.sh --------------------------------------------------------------

    def test_verify_reads_the_files_the_unit_and_the_sleeps(self):
        r = self.run_kit("verify.sh")
        self.assertIn("ubuntu-gts9u-device 2.69's own, not this kit's", r.stdout)
        self.assertIn("the kit is not in place", r.stdout)
        self.assertIn("no system sleep in this boot's journal", r.stdout)
        self.assertEqual(self.run_kit("install.sh").returncode, 0)
        (self.root / "journal.short-monotonic.systemd-sleep").write_text(
            "[  100.000000] tab systemd-sleep[10]: Performing sleep operation 'suspend'...\n"
            "[  130.900000] tab systemd-sleep[10]: System returned from sleep operation 'suspend'.\n"
            "[  132.950000] tab ubuntu-gts9u-sensors-resume[20]: ubuntu-gts9u-sensors-resume: waiting 10s for the fresh "
            "sensorspd attachment to settle\n"
            "[  145.700000] tab ubuntu-gts9u-sensors-resume[20]: ubuntu-gts9u-sensors-resume: SSC ready after attempt 1; "
            "started the sensor proxy with a fresh QMI client\n"
            "[  200.000000] tab systemd-sleep[30]: ubuntu-gts9u-sensors: systemctl stop iio-sensor-proxy.service: still "
            "running after 10s; going on, systemd finishes it\n"
            "[  210.000000] tab systemd-sleep[30]: Performing sleep operation 'suspend'...\n"
            "[  210.300000] tab systemd-sleep[30]: Failed to put system to sleep. System resumed again: Invalid argument\n")
        r = self.run_kit("verify.sh")
        out = r.stdout
        self.assertIn("ok     kit checksums", out)
        self.assertIn("ok     all three files are this revision", out)
        self.assertIn("ok     retries: on failure, three starts at most", out)
        self.assertIn("ok     iio-sensor-proxy: HasAccelerometer = true", out)
        self.assertIn("ok     sleep at 100.0 s: back after 30.9 s; sensors ready 14.8 s after it (attempt 1)", out)
        self.assertIn("info   sleep at 210.0 s: refused by the kernel (0.3 s); sensors not ready yet", out)
        self.assertIn("CHECK  hook at 200.0 s: systemctl stop iio-sensor-proxy.service: still running", out)
        self.assertNotIn("CHECK  /", out)

    # --- cycle.sh ---------------------------------------------------------------

    def sensors_up(self):
        """The sensors running, and the hook's ssccli where this test can put
        it: the kit's hook names /usr/bin/ssccli, which the host has not, so
        the kit is resealed with one in the scratch root, and installed."""
        ssccli = self.root / "usr/bin/ssccli"
        ssccli.parent.mkdir(parents=True, exist_ok=True)
        ssccli.write_text("#!/bin/sh\n")
        ssccli.chmod(0o755)
        hook = self.kit / "files" / HOOK
        text = hook.read_text()
        if str(ssccli) not in text:
            self.assertEqual(text.count("/usr/bin/ssccli"), 1)
            hook.write_text(text.replace("/usr/bin/ssccli", str(ssccli)))
            self.reseal(1)
        r = self.run_kit("install.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        state = self.root / "state"
        state.mkdir(exist_ok=True)
        (state / "iio-sensor-proxy.service").write_text("active\n")
        (state / "hexagonrpcd-adsp-sensorspd.service").write_text("active\n")
        (self.root / "journal.short-unix.ubuntu-gts9u-sensors-resume").write_text(
            "@NOW@ tab ubuntu-gts9u-sensors-resume[20]: ubuntu-gts9u-sensors-resume: SSC ready after attempt 1; "
            "started the sensor proxy with a fresh QMI client\n")

    def test_cycle_by_hand_stops_the_clients_and_sees_them_back(self):
        r = self.run_kit("cycle.sh", "hand", "1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("the hook in place is not this kit's revision 1", r.stdout)
        self.sensors_up()
        r = self.run_kit("cycle.sh", "hand", "1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("after pre: iio-sensor-proxy inactive, hexagonrpcd-adsp-sensorspd inactive, "
                      "ubuntu-gts9u-sensors-resume inactive", r.stdout)
        self.assertIn("SENSORS BACK after", r.stdout)
        self.assertIn("HasAccelerometer true", r.stdout)
        calls = self.calls("systemctl.calls")
        order = ["stop ubuntu-gts9u-sensors-resume.service", "stop iio-sensor-proxy.service",
                 "stop hexagonrpcd-adsp-sensorspd.service", "reset-failed ubuntu-gts9u-sensors-resume.service",
                 "restart --no-block ubuntu-gts9u-sensors-resume.service"]
        self.assertEqual([c for c in calls if c in order], order)

    def test_cycle_says_when_the_sensors_do_not_come_back(self):
        self.sensors_up()
        (self.root / "resume-fails").write_text("")
        r = self.run_kit("cycle.sh", "hand", "1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("SENSORS NOT BACK after", r.stdout)
        self.assertIn("result exit-code", r.stdout)

    def test_cycle_refuses_while_a_recovery_runs_or_the_sensors_are_down(self):
        self.sensors_up()
        (self.root / "state/ubuntu-gts9u-sensors-resume.service").write_text("activating\n")
        r = self.run_kit("cycle.sh", "hand", "1")
        self.assertIn("is still running (the last recovery)", r.stdout)
        (self.root / "state/ubuntu-gts9u-sensors-resume.service").write_text("inactive\n")
        (self.root / "state/iio-sensor-proxy.service").write_text("failed\n")
        r = self.run_kit("cycle.sh", "hand", "1")
        self.assertIn("iio-sensor-proxy is not running", r.stdout)
        for args in ((), ("hand", "0"), ("hand", "x"), ("freezer", "1"), ("sleep",)):
            with self.subTest(args=args):
                self.assertEqual(self.run_kit("cycle.sh", *args).returncode, 2)
        self.assertNotIn("stop iio-sensor-proxy.service", self.calls("systemctl.calls"))

    def test_cycle_freezer_runs_every_hook_through_systemd_sleep_and_puts_everything_back(self):
        pm_test = self.root / "sys/power/pm_test"
        for fails in (False, True):
            with self.subTest(sleep_fails=fails):
                self.sensors_up()
                pm_test.write_text("[none] core processors platform devices freezer\n")
                (self.root / "sleep-fails").unlink(missing_ok=True)
                if fails:
                    (self.root / "sleep-fails").write_text("")
                start = time.monotonic()
                r = self.run_kit("cycle.sh", "freezer")
                self.assertLess(time.monotonic() - start, 60)
                self.assertEqual((self.root / "pm_test.during").read_text(), "freezer\n")
                self.assertIn("AllowSuspend=yes", (self.root / "dropin.during").read_text())
                self.assertEqual(pm_test.read_text(), "none\n")
                self.assertEqual(list((self.root / "run/systemd/sleep.conf.d").iterdir()), [])
                self.assertIn("systemd-sleep returned " + ("1" if fails else "0"), r.stdout)
                if fails:
                    # No post: nothing restarted the resume unit.
                    self.assertIn("SENSORS NOT BACK", r.stdout)
                    self.assertIn("the post step did not start", r.stdout)
                else:
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assertIn("SENSORS BACK after", r.stdout)
        runs = self.calls("systemd-run.calls")
        self.assertEqual(len(runs), 2)
        self.assertTrue(runs[0].startswith("--wait --collect --quiet --unit=gts9u-sensors-hook-cycle-"), runs[0])
        self.assertTrue(runs[0].endswith("usr/lib/systemd/systemd-sleep suspend"), runs[0])

    def test_cycle_freezer_refuses_a_pm_test_in_use_or_missing(self):
        self.sensors_up()
        pm_test = self.root / "sys/power/pm_test"
        for content, said in (("none core processors platform devices [freezer]\n", "pm_test is not none"),
                              ("[none] core\n", "no pm_test=freezer")):
            with self.subTest(content=content):
                pm_test.write_text(content)
                r = self.run_kit("cycle.sh", "freezer")
                self.assertNotEqual(r.returncode, 0)
                self.assertIn(said, r.stdout)
                self.assertEqual(pm_test.read_text(), content)
        self.assertEqual(self.calls("systemd-run.calls"), [])


if __name__ == "__main__":
    unittest.main()
