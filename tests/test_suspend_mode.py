"""Run ubuntu-gts9u-suspend-mode against a fake root: board, mode files,
systemctl and dconf are all simulated, nothing on the host is touched."""
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = REPO / "packaging/ubuntu-gts9u-device"
SCRIPT = PACKAGE / "usr/libexec/ubuntu-gts9u-suspend-mode"
KEYFILES = PACKAGE / "usr/share/ubuntu-gts9u/dconf/suspend-off.d"

GTS9WIFI = b"qcom,kalama-mtp\0qcom,kalama\0samsung,gts9wifi\0qcom,sm8550\0"
GTS9UWIFI = b"qcom,kalama-mtp\0qcom,kalama\0qcom,mtp\0samsung,gts9uwifi\0qcom,sm8550\0"

SLEEP = "run/systemd/sleep.conf.d/50-ubuntu-gts9u-suspend-mode.conf"
LOGIND = "run/systemd/logind.conf.d/50-ubuntu-gts9u-suspend-mode.conf"
PROFILE = "run/ubuntu-gts9u/dconf/profile-user"
DB = "run/ubuntu-gts9u/dconf/suspend-off"

STUB_SYSTEMCTL = """#!/bin/sh
echo "$*" >> "$FAKE_ROOT/systemctl.log"
case "$1 $2" in
"is-active --quiet") [ -e "$FAKE_ROOT/logind-active" ] ;;
*) exit 0 ;;
esac
"""

# Stands in for `dconf compile OUTPUT DIR`: records what it was given.
STUB_DCONF = """#!/bin/sh
[ "$1" = compile ] || exit 2
[ -e "$FAKE_ROOT/dconf-fails" ] && exit 1
cat "$3"/* > "$2"
"""

# Everything the script runs besides the shell's builtins and the stubs above.
TOOLS = ("sed", "head", "tr", "grep", "mkdir", "mktemp", "chmod", "cmp", "mv",
         "rm", "readlink", "cat")


class SuspendModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        for directory in ("etc/ubuntu-gts9u", "etc/systemd/system", "run/systemd/system",
                          "run/ubuntu-gts9u", "proc/device-tree", "sys/power",
                          "usr/share/ubuntu-gts9u/dconf", "bin", "tools"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        # A PATH of its own, so that neither the host's systemctl nor its dconf
        # can be reached by accident.
        for tool in TOOLS:
            os.symlink(shutil.which(tool), self.root / "tools" / tool)
        shutil.copytree(KEYFILES, self.root / "usr/share/ubuntu-gts9u/dconf/suspend-off.d")
        (self.root / "sys/power/state").write_text("freeze mem\n")
        (self.root / "sys/power/mem_sleep").write_text("s2idle [deep]\n")
        self.stub("systemctl", STUB_SYSTEMCTL)
        self.stub("dconf", STUB_DCONF)
        self.board(GTS9WIFI)

    def tearDown(self):
        self.tmp.cleanup()

    def stub(self, name, body):
        path = self.root / "bin" / name
        path.write_text(body)
        path.chmod(0o755)

    def board(self, compatible):
        (self.root / "proc/device-tree/compatible").write_bytes(compatible)

    def choose(self, where, text):
        (self.root / where / "ubuntu-gts9u/suspend-mode").write_text(text)

    def script(self):
        text = SCRIPT.read_text()
        prefixes = ("/run/ubuntu-gts9u", "/etc/ubuntu-gts9u", "/run/systemd/",
                    "/etc/systemd/system", "/proc/device-tree/compatible",
                    "/sys/power/", "/usr/share/ubuntu-gts9u/dconf")
        pattern = re.compile("|".join(re.escape(p) for p in prefixes))
        return pattern.sub(lambda match: str(self.root) + match.group(0), text)

    def run_script(self, *args):
        env = dict(os.environ, FAKE_ROOT=str(self.root),
                   PATH="%s:%s" % (self.root / "bin", self.root / "tools"))
        return subprocess.run(["/bin/sh", "-s", *args], input=self.script(), env=env,
                              text=True, capture_output=True, timeout=20)

    def apply(self):
        result = self.run_script("apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def exists(self, relative):
        return (self.root / relative).exists()

    def read(self, relative):
        return (self.root / relative).read_text()

    def systemctl_log(self):
        log = self.root / "systemctl.log"
        return log.read_text().splitlines() if log.exists() else []

    # --- the board decides when nothing is chosen ------------------------
    def test_gts9wifi_defaults_to_off(self):
        result = self.apply()
        self.assertIn("mode off, from the board default", result.stdout)
        self.assertIn("[Sleep]\nAllowSuspend=no\n", self.read(SLEEP))
        self.assertIn("[Login]\nHandleLidSwitch=lock\nHandleLidSwitchExternalPower=lock\n",
                      self.read(LOGIND))
        profile = self.read(PROFILE)
        self.assertRegex(profile, r"(?m)^user-db:user\nfile-db:.*/run/ubuntu-gts9u/dconf/suspend-off$")
        self.assertIn("sleep-inactive-battery-type='nothing'", self.read(DB))
        self.assertEqual(self.read("run/ubuntu-gts9u/suspend-mode.applied"), "off\n")

    def test_other_boards_keep_the_stock_policy(self):
        self.board(GTS9UWIFI)
        result = self.apply()
        self.assertIn("mode deep, from the board default", result.stdout)
        for relative in (SLEEP, LOGIND, PROFILE, DB):
            self.assertFalse(self.exists(relative), relative)

    def test_no_device_tree_keeps_the_stock_policy(self):
        (self.root / "proc/device-tree/compatible").unlink()
        self.assertIn("mode deep", self.apply().stdout)
        self.assertFalse(self.exists(SLEEP))

    # --- an explicit choice ---------------------------------------------
    def test_s2idle_from_etc(self):
        self.choose("etc", "# validated on 2026-11-07\ns2idle\n")
        self.assertIn("mode s2idle, from", self.apply().stdout)
        self.assertIn("[Sleep]\nSuspendState=freeze\n", self.read(SLEEP))
        self.assertFalse(self.exists(LOGIND))
        self.assertFalse(self.exists(PROFILE))

    def test_runtime_choice_wins_and_is_per_boot(self):
        self.choose("etc", "off\n")
        self.choose("run", "deep  # this boot only\n")
        self.assertIn("mode deep", self.apply().stdout)
        self.assertFalse(self.exists(SLEEP))

    def test_auto_stops_at_the_board_default(self):
        self.choose("etc", "s2idle\n")
        self.choose("run", "AUTO\n")
        self.assertIn("mode off", self.apply().stdout)

    def test_unknown_word_is_ignored(self):
        self.choose("etc", "s2dile\n")
        result = self.apply()
        self.assertIn("ignoring 's2dile'", result.stderr)
        self.assertIn("mode off, from the board default", result.stdout)

    # --- switching modes cleans up after itself ------------------------
    def test_leaving_off_removes_everything_it_wrote(self):
        self.apply()
        self.choose("etc", "deep\n")
        self.apply()
        for relative in (SLEEP, LOGIND, PROFILE, DB):
            self.assertFalse(self.exists(relative), relative)

    # --- logind is reloaded, never restarted, and only when needed -----
    def test_logind_reload_only_when_running_and_changed(self):
        self.apply()                    # boot: logind not running yet
        self.assertNotIn("--no-block reload systemd-logind.service", self.systemctl_log())
        (self.root / "logind-active").touch()
        self.apply()                    # same mode again: nothing to reload
        self.assertNotIn("--no-block reload systemd-logind.service", self.systemctl_log())
        self.choose("run", "s2idle\n")
        self.apply()                    # the lid setting goes away
        self.assertIn("--no-block reload systemd-logind.service", self.systemctl_log())
        self.assertFalse(any("restart" in line for line in self.systemctl_log()))

    def test_same_mode_from_another_place_changes_nothing(self):
        self.apply()
        (self.root / "logind-active").touch()
        self.choose("etc", "off\n")
        self.apply()
        self.assertNotIn("--no-block reload systemd-logind.service", self.systemctl_log())

    # --- what is outside the switch is reported --------------------------
    def test_masked_targets_are_reported(self):
        for unit in ("sleep.target", "suspend.target"):
            os.symlink("/dev/null", self.root / "etc/systemd/system" / unit)
        self.choose("run", "s2idle\n")
        result = self.apply()
        self.assertIn("sleep.target suspend.target masked", result.stderr)
        self.assertIn("systemctl unmask sleep.target suspend.target", result.stderr)

    def test_deep_on_a_kernel_that_defaults_to_s2idle_is_reported(self):
        (self.root / "sys/power/mem_sleep").write_text("[s2idle] deep\n")
        self.choose("etc", "deep\n")
        self.assertIn("suspends to idle", self.apply().stderr)

    def test_without_dconf_gnome_is_left_alone(self):
        (self.root / "bin/dconf").unlink()
        result = self.apply()
        self.assertIn("dconf is missing", result.stderr)
        self.assertFalse(self.exists(PROFILE))
        self.assertIn("AllowSuspend=no", self.read(SLEEP))
        self.assertIn("HandleLidSwitch=lock", self.read(LOGIND))

    def test_failed_compile_leaves_no_profile(self):
        (self.root / "dconf-fails").touch()
        result = self.apply()
        self.assertIn("could not compile", result.stderr)
        self.assertFalse(self.exists(PROFILE))
        self.assertIn("AllowSuspend=no", self.read(SLEEP))

    def test_status_reports_mode_and_files(self):
        self.apply()
        self.choose("run", "s2idle\n")
        result = self.run_script("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("configured mode: s2idle", result.stdout)
        self.assertIn("applied mode:    off", result.stdout)
        self.assertIn("to apply", result.stdout)
        self.assertIn("AllowSuspend=no", result.stdout)

    @unittest.skipUnless(shutil.which("dconf"), "dconf is not installed")
    def test_shipped_keyfile_compiles_and_user_choice_wins(self):
        real = shutil.which("dconf")
        work = self.root / "real"
        (work / "user").mkdir(parents=True)
        (work / "home/dconf").mkdir(parents=True)
        subprocess.run([real, "compile", str(work / "db"), str(KEYFILES)], check=True)
        (work / "user/u").write_text(
            "[org/gnome/settings-daemon/plugins/power]\n"
            "sleep-inactive-battery-type='suspend'\n")
        subprocess.run([real, "compile", str(work / "home/dconf/user"), str(work / "user")],
                       check=True)
        (work / "profile").write_text("user-db:user\nfile-db:%s\n" % (work / "db"))
        env = dict(os.environ, DCONF_PROFILE=str(work / "profile"),
                   XDG_CONFIG_HOME=str(work / "home"))
        key = "/org/gnome/settings-daemon/plugins/power/"

        def read(name):
            return subprocess.run([real, "read", key + name], env=env, text=True,
                                  capture_output=True, check=True).stdout.strip()

        self.assertEqual(read("sleep-inactive-ac-type"), "'nothing'")
        self.assertEqual(read("sleep-inactive-battery-type"), "'suspend'")


if __name__ == "__main__":
    unittest.main()
