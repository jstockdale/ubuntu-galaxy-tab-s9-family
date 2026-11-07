"""Offline test of tools/inplace/boothygiene install.sh / rollback.sh.

The scripts run against a temporary root: their absolute paths are rewritten
and root-only commands (id, flock, systemctl, dpkg-query, fuser, install -o)
are stubbed on PATH.  The real 2.55 files are not needed: the copied kit gets
a BEFORE.md5 describing the stand-ins this test writes."""
import hashlib
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
KIT = REPO / "tools/inplace/boothygiene"
X710 = b"qcom,kalama-mtp\0qcom,kalama\0qcom,mtp\0samsung,gts9wifi\0qcom,sm8550\0"

STUBS = {
    "id": "#!/bin/sh\necho 0\n",
    "flock": "#!/bin/sh\nexit 0\n",
    "fuser": "#!/bin/sh\nexit 1\n",
    "dpkg-query": "#!/bin/sh\necho 2.55\n",
    "systemctl": "#!/bin/sh\necho \"$*\" >> \"$TEST_ROOT/systemctl.calls\"\n"
                 "case \"$1\" in show) echo masked ;; is-enabled) echo enabled ;;"
                 " is-active) echo inactive ;; esac\nexit 0\n",
    # install -o/-g need root; drop them, keep the rest.
    "install": "#!/bin/sh\nfor a; do shift; case \"$skip$a\" in -o|-g) skip=1; continue ;;"
               " 1*) skip=; continue ;; esac; set -- \"$@\" \"$a\"; done\n"
               "exec /usr/bin/install \"$@\"\n",
}


def md5(data):
    return hashlib.md5(data).hexdigest()


class KitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.root = base / "root"
        self.kit = base / "kit"
        shutil.copytree(KIT, self.kit)
        (self.root / "run").mkdir(parents=True)
        (self.root / "var/log").mkdir(parents=True)
        (self.root / "var/backups").mkdir(parents=True)
        (self.root / "compatible").write_bytes(X710)
        bin_dir = base / "bin"
        bin_dir.mkdir()
        for name, text in STUBS.items():
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        self.env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}",
                        TEST_ROOT=str(self.root))
        # Stand-ins for the installed 2.55 files: every payload file listed in
        # BEFORE.md5 exists with old content; BEFORE.md5 describes them.
        self.payload = sorted(str(p.relative_to(self.kit / "files"))
                              for p in (self.kit / "files").rglob("*") if p.is_file())
        listed = [line.split()[1] for line in (KIT / "BEFORE.md5").read_text().splitlines()]
        before = []
        for rel in listed:
            data = f"old {rel}\n".encode()
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            before.append(f"{md5(data)}  {rel}\n")
        (self.kit / "BEFORE.md5").write_text("".join(before))
        self.reseal()
        self.replaced = listed
        self.created = [rel for rel in self.payload if rel not in listed]

    def tearDown(self):
        self.tmp.cleanup()

    def reseal(self):
        files = sorted(p for p in self.kit.rglob("*")
                       if p.is_file() and p.name not in ("SHA256SUMS", "make-kit.sh"))
        (self.kit / "SHA256SUMS").write_text("".join(
            f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(self.kit)}\n"
            for p in files))

    def script(self, name):
        text = (self.kit / name).read_text()
        root = str(self.root)
        for old, new in (('kit=$(cd "$(dirname "$0")" && pwd)', f"kit={self.kit}"),
                         ('"/$rel', f'"{root}/$rel'),
                         ("/usr/lib/clock-epoch", f"{root}/usr/lib/clock-epoch"),
                         ("/usr/libexec/gts9u-bringup-sdlog", f"{root}/usr/libexec/gts9u-bringup-sdlog"),
                         ("/proc/device-tree/compatible", f"{root}/compatible"),
                         ("/var/backups/", f"{root}/var/backups/"),
                         ("/var/log/gts9u", f"{root}/var/log/gts9u"),
                         ("/run/gts9u-boothygiene.lock", f"{root}/run/lock")):
            text = text.replace(old, new)
        return text

    def run_kit(self, name):
        return subprocess.run(["/bin/sh"], input=self.script(name), text=True,
                              capture_output=True, env=self.env, timeout=60)

    def test_install_then_rollback(self):
        (self.root / "usr/lib").mkdir(parents=True, exist_ok=True)
        r = self.run_kit("install.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for rel in self.payload:
            with self.subTest(rel=rel):
                self.assertEqual((self.root / rel).read_bytes(),
                                 (self.kit / "files" / rel).read_bytes())
                self.assertEqual(os.access(self.root / rel, os.X_OK),
                                 os.access(self.kit / "files" / rel, os.X_OK))
        self.assertTrue((self.root / "usr/lib/clock-epoch").exists())
        calls = (self.root / "systemctl.calls").read_text()
        self.assertIn("daemon-reload", calls)
        self.assertIn("enable ubuntu-gts9u-pstore-archive.service", calls)
        self.assertIn("stop gts9u-bringup-sdlog.timer", calls)
        backup = self.root / "var/backups/ubuntu-gts9u-boothygiene"
        self.assertEqual(sorted((backup / "created").read_text().split()), sorted(self.created))
        # A second run is a no-op that succeeds.
        r = self.run_kit("install.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        r = self.run_kit("rollback.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for rel in self.replaced:
            self.assertEqual((self.root / rel).read_text(), f"old {rel}\n")
        for rel in self.created:
            self.assertFalse((self.root / rel).exists(), rel)
        self.assertFalse((self.root / "usr/lib/clock-epoch").exists())
        self.assertFalse(backup.exists())
        self.assertTrue(list((self.root / "var/backups").glob("ubuntu-gts9u-boothygiene.rolled-back-*")))

    def test_refuses_a_hand_edited_file(self):
        target = self.root / "usr/lib/systemd/system/ubuntu-gts9u-rtc-restore.service"
        target.write_text("edited by hand\n")
        r = self.run_kit("install.sh")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("differ from both", r.stdout)
        self.assertEqual(target.read_text(), "edited by hand\n")
        self.assertFalse((self.root / "var/backups/ubuntu-gts9u-boothygiene").exists())
        for rel in self.created:
            self.assertFalse((self.root / rel).exists(), rel)

    def test_logger_left_alone_where_not_installed(self):
        for rel in [r for r in self.replaced if "bringup-sdlog" in r]:
            (self.root / rel).unlink()
        r = self.run_kit("install.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.root / "usr/libexec/gts9u-bringup-sdlog").exists())
        self.assertFalse((self.root / "usr/lib/systemd/system/gts9u-bringup-sdlog.timer").exists())
        self.assertNotIn("bringup", (self.root / "systemctl.calls").read_text())

    def test_refuses_other_boards(self):
        (self.root / "compatible").write_bytes(X710.replace(b"gts9wifi", b"gts9uwifi"))
        r = self.run_kit("install.sh")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not an SM-X710", r.stdout)

    def test_kit_payload_matches_the_repository(self):
        """files/ must be what make-kit.sh builds from the package sources."""
        pkg = REPO / "packaging/ubuntu-gts9u-device"
        sdlog = REPO / "scripts/bringup-sdlog"
        for rel in self.payload:
            name = pathlib.Path(rel).name
            source = sdlog / name if "bringup-sdlog" in name else pkg / rel
            with self.subTest(rel=rel):
                self.assertEqual((KIT / "files" / rel).read_bytes(), source.read_bytes())
        sums = subprocess.run(["sha256sum", "-c", "--quiet", "SHA256SUMS"], cwd=KIT,
                              capture_output=True, text=True)
        self.assertEqual(sums.returncode, 0, sums.stdout + sums.stderr)


if __name__ == "__main__":
    unittest.main()
