"""Run the in-place interconnect kit (tools/inplace/icc) against a fake root.

install.sh and rollback.sh normally run as root on the tablet; here every
absolute path they touch is moved under a temporary directory, and systemctl,
dpkg-query, flock, chgrp, install's ownership flags and id are stubbed.  The
kit itself is assembled by make-kit.sh exactly as for the tablet, so the
checksum gate is exercised too.
"""
import os
import pathlib
import subprocess
import tempfile
import unittest


REPO = pathlib.Path(__file__).resolve().parents[1]
KIT_SRC = REPO / "tools/inplace/icc"
NAME = "ubuntu-gts9u-icc-late-release"

PREFIXES = (
    "/var/log/", "/var/backups/", "/usr/libexec/", "/usr/lib/systemd/",
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
        for d in ("var/log", "var/backups", "usr/libexec", "usr/lib/systemd/system",
                  "etc/systemd/system", "run"):
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


if __name__ == "__main__":
    unittest.main()
