"""Offline tests for the modes, links and conffiles scripts/build-device-package.sh
gives the device package.  The script's section between its "modes, ownership
and links" and "build" markers runs on a copy of packaging/ubuntu-gts9u-device;
nothing is built and nothing on the host is touched."""
import os
import pathlib
import shlex
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/build-device-package.sh"
PKG = REPO / "packaging/ubuntu-gts9u-device"
GENERATORS = "usr/lib/systemd/system-generators"
START = "# --- modes, ownership and links "
END = "# --- build "

# Created by the build script, never versioned (see test_sources_hold_no_symlinks).
LINKS = {
    "etc/systemd/system/alsa-restore.service": "/dev/null",
    "etc/systemd/system/alsa-state.service": "/dev/null",
    "usr/share/dconf/profile/user": "/run/ubuntu-gts9u/dconf/profile-user",
}


def section():
    text = SCRIPT.read_text()
    start = text.index("\n" + START) + 1
    end = text.index("\n" + END, start) + 1
    return text[start:end]


def tracked_executables():
    """Package files git records as 100755, or None without a git checkout."""
    try:
        r = subprocess.run(["git", "ls-files", "-s", "--", str(PKG)], cwd=REPO,
                           capture_output=True, text=True, timeout=30)
    except OSError:
        return None
    if r.returncode != 0:
        return None
    paths = []
    for line in r.stdout.splitlines():
        meta, path = line.split("\t", 1)
        if meta.split()[0] == "100755":
            paths.append(pathlib.Path(path).relative_to(PKG.relative_to(REPO)))
    return paths


class StagedTree(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.staging = pathlib.Path(cls.tmp.name) / "ubuntu-gts9u-device"
        shutil.copytree(PKG, cls.staging, symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        # Start from a checkout with no executable bits at all, as one made
        # with core.fileMode=false would be: every 0755 must come from a rule.
        for directory, _, files in os.walk(cls.staging):
            for name in files:
                path = pathlib.Path(directory, name)
                if not path.is_symlink():
                    path.chmod(0o644)
        # chown needs root; the real build runs under fakeroot.  The umask is
        # a desktop login's, so a directory made without an explicit mode shows.
        prelude = ("set -euo pipefail\numask 0002\nstaging=%s\nchown() { :; }\n"
                   % shlex.quote(str(cls.staging)))
        cls.run_result = subprocess.run(["bash"], input=prelude + section(), text=True,
                                        capture_output=True, timeout=120)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.assertEqual(self.run_result.returncode, 0, self.run_result.stderr)

    def mode(self, rel):
        return (self.staging / rel).lstat().st_mode & 0o7777

    def test_generators_are_executable(self):
        # systemd does not run a generator without the executable bit; device
        # 2.56 shipped the fingerprint gate 0644 and fprintd stayed unmasked.
        generators = sorted((self.staging / GENERATORS).iterdir())
        self.assertTrue(generators, "no generators found under " + GENERATORS)
        self.assertIn("ubuntu-gts9u-fingerprint-gate", [g.name for g in generators])
        for path in generators:
            with self.subTest(generator=path.name):
                self.assertEqual(self.mode(path.relative_to(self.staging)), 0o755)

    def test_tracked_executables_stay_executable(self):
        paths = tracked_executables()
        if paths is None:
            self.skipTest("not a git checkout")
        self.assertTrue(paths)
        for rel in paths:
            if not (PKG / rel).exists():
                continue  # deleted in the working tree
            with self.subTest(path=str(rel)):
                self.assertEqual(self.mode(rel), 0o755)

    def test_links_are_created(self):
        for rel, target in LINKS.items():
            with self.subTest(link=rel):
                path = self.staging / rel
                self.assertTrue(path.is_symlink())
                self.assertEqual(os.readlink(path), target)

    def test_directories_are_0755(self):
        # Including the parents the link step creates after the normalisation.
        for directory, _, _ in os.walk(self.staging):
            rel = pathlib.Path(directory).relative_to(self.staging)
            with self.subTest(directory=str(rel)):
                self.assertEqual(self.mode(rel), 0o755)

    def test_conffiles_list_regular_files_only(self):
        listed = (self.staging / "DEBIAN/conffiles").read_text().split()
        etc = self.staging / "etc"
        expected = sorted("/" + str(p.relative_to(self.staging))
                          for p in etc.rglob("*") if p.is_file() and not p.is_symlink())
        self.assertEqual(listed, expected)
        for rel in LINKS:
            self.assertNotIn("/" + rel, listed)

    def test_timestamps_are_fixed(self):
        for rel in (GENERATORS + "/ubuntu-gts9u-fingerprint-gate", *LINKS):
            with self.subTest(path=rel):
                self.assertEqual((self.staging / rel).lstat().st_mtime, 0)


class Sources(unittest.TestCase):
    def test_sources_hold_no_symlinks(self):
        # A checkout with core.symlinks=false (Git for Windows' default) turns
        # a git symlink into a text file holding its target, and the package
        # would ship that.  build-device-package.sh creates the links instead.
        found = []
        for directory, dirs, files in os.walk(PKG):
            for name in dirs + files:
                path = pathlib.Path(directory, name)
                if path.is_symlink():
                    found.append(str(path.relative_to(PKG)))
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
