"""The one-shot boot rungs of tools/inplace/icc/weekend/icc-cycle-experiment.sh.

The state machine matters more than anything else in that script: a boot rung
must fire once, must never loop a hang, and its ExecCondition must only skip
the panel cycle while a kernel-early attempt is unresolved on a Stage 3
cmdline.  Every absolute path is moved under a temporary root.
"""
import pathlib
import re
import subprocess
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / (
    "tools/inplace/icc/weekend/icc-cycle-experiment.sh")
PROVIDERS = ("24100000.interconnect", "interconnect-1")
STAGE2 = "root=LABEL=UBTS9_UFS initcall_blacklist=bwmon_driver_init icc_core.dyndbg=+p\n"
STAGE3 = "root=LABEL=UBTS9_UFS icc_core.dyndbg=+p\n"


class ExperimentTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name)
        r = self.root
        (r / "proc/device-tree").mkdir(parents=True)
        (r / "proc/device-tree/compatible").write_bytes(b"samsung,gts9wifi\0")
        (r / "proc/sys/kernel/random").mkdir(parents=True)
        self.boot("aaaaaaaa", STAGE2)
        (r / "proc/uptime").write_text("21.00 1.00\n")
        for p in PROVIDERS + ("17a00000.rsc:power-controller",):
            d = r / "sys/bus/platform/devices" / p
            d.mkdir(parents=True)
            (d / "state_synced").write_text("0\n" if p in PROVIDERS else "1\n")
        (r / "sys/module/printk/parameters").mkdir(parents=True)
        (r / "sys/module/printk/parameters/console_suspend").write_text("Y\n")
        (r / "sys/power/suspend_stats").mkdir(parents=True)
        for f, v in (("pm_print_times", "0"), ("pm_debug_messages", "0"), ("pm_test", "[none] platform"),
                     ("suspend_stats/success", "1"), ("suspend_stats/fail", "0")):
            (r / "sys/power" / f).write_text(v + "\n")
        (r / "sys/class/remoteproc/remoteproc0").mkdir(parents=True)
        (r / "sys/class/remoteproc/remoteproc0/name").write_text("adsp\n")
        (r / "sys/class/remoteproc/remoteproc0/state").write_text("offline\n")
        (r / "etc/systemd/system").mkdir(parents=True)
        (r / "kmsg").write_text("")

    def boot(self, boot_id, cmdline):
        (self.root / "proc/sys/kernel/random/boot_id").write_text(boot_id + "-0000\n")
        (self.root / "proc/cmdline").write_text(cmdline)

    def run_script(self, *args):
        # One pass, so /proc/sys/... is not rewritten twice.
        script = re.sub(r"/(?:sys|proc|var|etc|usr/local)/",
                        lambda m: f"{self.root}{m.group(0)}", SCRIPT.read_text())
        script = script.replace("/dev/kmsg", f"{self.root}/kmsg")
        prelude = f"""
id() {{ echo 0; }}
sync() {{ :; }}
sleep() {{ :; }}
chgrp() {{ :; }}
dmesg() {{ :; }}
systemctl() {{ :; }}
install() {{
    src= dst=
    while [ $# -gt 0 ]; do
        case "$1" in -o|-g|-m) shift ;; -D) ;; *) if [ -z "$src" ]; then src=$1; else dst=$1; fi ;; esac
        shift
    done
    mkdir -p "$(dirname "$dst")" && cp "$src" "$dst"
}}
set -- {' '.join(args)}
"""
        return subprocess.run(["/bin/sh", "-s"], input=prelude + script, text=True,
                              capture_output=True, timeout=30)

    @property
    def state(self):
        return self.root / "var/lib/gts9u-icc-exp"

    def synced(self, p):
        return (self.root / "sys/bus/platform/devices" / p / "state_synced").read_text()

    def test_release_before_cycle_fires_once(self):
        r = self.run_script("arm-boot", "release-before-cycle", "--trace")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        dropin = (self.root / "etc/systemd/system/ubuntu-gts9u-panel-coldboot-recover.service.d/"
                  "90-gts9u-icc-experiment.conf").read_text()
        self.assertIn("ExecCondition=", dropin)
        self.assertIn("mode=release-before-cycle", (self.state / "armed").read_text())

        self.boot("bbbbbbbb", STAGE2)
        self.assertEqual(self.run_script("boot-gate").returncode, 0)
        r = self.run_script("pre-cycle")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.state / "armed").exists())
        self.assertIn("fired_boot=bbbbbbbb", (self.state / "fired").read_text())
        for p in PROVIDERS:
            self.assertEqual(self.synced(p), "1")
        self.assertEqual(self.synced("17a00000.rsc:power-controller"), "1\n")
        self.assertEqual((self.root / "sys/module/printk/parameters/console_suspend").read_text().strip(), "N")

        r = self.run_script("post-cycle")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("returned=bbbbbbbb", (self.state / "fired").read_text())
        self.assertEqual((self.root / "sys/module/printk/parameters/console_suspend").read_text().strip(), "Y")

        # A second boot: nothing armed, nothing happens.
        self.boot("cccccccc", STAGE2)
        for p in PROVIDERS:
            (self.root / "sys/bus/platform/devices" / p / "state_synced").write_text("0\n")
        self.assertEqual(self.run_script("boot-gate").returncode, 0)
        self.assertEqual(self.run_script("pre-cycle").returncode, 0)
        self.assertEqual(self.synced(PROVIDERS[0]), "0\n")

    def test_hang_does_not_loop(self):
        self.run_script("arm-boot", "release-before-cycle")
        self.boot("bbbbbbbb", STAGE2)
        self.run_script("pre-cycle")
        # The cycle hung: no post-cycle.  The next boot runs a normal cycle.
        self.boot("cccccccc", STAGE2)
        for p in PROVIDERS:
            (self.root / "sys/bus/platform/devices" / p / "state_synced").write_text("0\n")
        self.assertEqual(self.run_script("boot-gate").returncode, 0)
        self.assertEqual(self.run_script("pre-cycle").returncode, 0)
        self.assertEqual(self.synced(PROVIDERS[0]), "0\n")

    def test_kernel_early_gates_only_on_stage3_until_resolved(self):
        self.run_script("arm-boot", "kernel-early", "--trace")
        self.boot("bbbbbbbb", STAGE3)
        self.assertEqual(self.run_script("boot-gate").returncode, 0)
        self.run_script("pre-cycle")
        self.assertEqual(self.synced(PROVIDERS[0]), "0\n")  # the kernel's job here
        # Hung.  Next boot, still Stage 3: the cycle is skipped, every time.
        self.boot("cccccccc", STAGE3)
        self.assertEqual(self.run_script("boot-gate").returncode, 1)
        self.boot("dddddddd", STAGE3)
        self.assertEqual(self.run_script("boot-gate").returncode, 1)
        # Back on Stage 2: the cycle runs again even before disarm.
        self.boot("eeeeeeee", STAGE2)
        self.assertEqual(self.run_script("boot-gate").returncode, 0)
        r = self.run_script("disarm")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.root / "etc/systemd/system/ubuntu-gts9u-panel-coldboot-recover.service.d/"
                          "90-gts9u-icc-experiment.conf").exists())
        self.assertFalse((self.state / "fired").exists())
        self.boot("ffffffff", STAGE3)
        self.assertEqual(self.run_script("boot-gate").returncode, 0)

    def test_kernel_early_that_returns_is_not_gated(self):
        self.run_script("arm-boot", "kernel-early")
        self.boot("bbbbbbbb", STAGE3)
        self.run_script("pre-cycle")
        self.run_script("post-cycle")
        self.boot("cccccccc", STAGE3)
        self.assertEqual(self.run_script("boot-gate").returncode, 0)

    def test_arming_twice_is_refused(self):
        self.assertEqual(self.run_script("arm-boot", "trace-only").returncode, 0)
        r = self.run_script("arm-boot", "trace-only")
        self.assertEqual(r.returncode, 1)
        self.assertIn("already armed", r.stdout)

    def test_unknown_mode_is_refused(self):
        r = self.run_script("arm-boot", "bogus")
        self.assertEqual(r.returncode, 1)
        self.assertFalse((self.state / "armed").exists())


    def test_runtime_rung_releases_cycles_and_restores_the_sleep_mode(self):
        r = self.root
        (r / "sys/power/mem_sleep").write_text("[s2idle] deep\n")
        (r / "sys/power/state").write_text("")
        (r / "sys/power/suspend_stats/last_failed_dev").write_text("\n")
        (r / "sys/power/suspend_stats/last_failed_errno").write_text("0\n")
        script = re.sub(r"/(?:sys|proc|var|etc|usr/local)/",
                        lambda m: f"{r}{m.group(0)}", SCRIPT.read_text())
        script = script.replace("/dev/kmsg", f"{r}/kmsg")
        prelude = f"""
id() {{ echo 0; }}
sync() {{ :; }}
sleep() {{ :; }}
chgrp() {{ :; }}
dmesg() {{ :; }}
systemctl() {{
    case "$*" in
    *Result*) echo success ;;
    *ActiveState*) echo active ;;
    esac
}}
set -- runtime --trace
"""
        res = subprocess.run(["/bin/sh", "-s"], input=prelude + script, text=True,
                             capture_output=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        for p in PROVIDERS:
            self.assertEqual(self.synced(p), "1")
        self.assertEqual((r / "sys/power/state").read_text().strip(), "mem")
        self.assertEqual((r / "sys/power/pm_test").read_text().strip(), "none")
        # Deep for the cycle only: the default sleep mode is put back.
        self.assertEqual((r / "sys/power/mem_sleep").read_text().strip(), "s2idle")
        self.assertEqual((r / "sys/module/printk/parameters/console_suspend").read_text().strip(), "Y")
        self.assertIn("runtime: returned rc=0", res.stdout)


if __name__ == "__main__":
    unittest.main()
