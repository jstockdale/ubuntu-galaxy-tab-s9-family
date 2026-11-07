"""Offline checks of the DISP-5 panel experiment kit (tools/inplace/panel).

report.py against synthetic kernel logs (register leftovers, E1/E2/E3
verdicts), the kit checksums, shell syntax, and apply.sh / rollback.sh against
regular files standing in for the boot partitions.  make-images.sh is run
only when PANEL_BASE_VENDOR_BOOT points at fix2-s2's vendor_boot.img.
"""
import hashlib
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

KIT = pathlib.Path(__file__).resolve().parents[1] / "tools/inplace/panel"
SIZE = 100663296


def dump(t, dev, when, block, regs, size):
    lines = []
    for off in range(0, size, 16):
        vals = " ".join("%08x" % regs.get(off + 4 * k, 0) for k in range(4))
        lines.append("[%12.6f] %s: regdump %s %s +%03x: %s" % (t, dev, when, block, off, vals))
    return lines


def boot_log(extra_cmdline="", first="00 00 00", handoff=None, cycle=None,
             suspend=True, leftover=True, cycle_answer="80 00 04", later=None):
    """A boot as dmesg shows it.  With cycle, msm.boot_display_cycle's own
    re-enable (dumps and id cycle_answer) comes before its summary line, as
    in msm_kms_boot_display_cycle(); later adds a power-on after it (the
    display manager's modeset) that answers later."""
    ctrl_probe = {0x004: 0x00000135, 0x0ac: 0x10000000, 0x000: 0x20070000}
    phy_probe = {0x024: 0x7f, 0x0a4: 0x60 if leftover else 0, 0x0a0: 0x1f}
    ctrl_bad = {**ctrl_probe, 0x004: 0x00000105, 0x11c: 0x23f}
    ctrl_good = {**ctrl_bad, 0x0ac: 0}
    phy_bad = dict(phy_probe)
    phy_good = {**phy_probe, 0x0a4: 0}
    out = ["[    0.000000] Kernel command line: root=LABEL=UBTS9_UFS msm.dsi_regdump=1 " + extra_cmdline]
    if handoff:
        out.append("[    3.700000] msm-mdss ae00000.display-subsystem: bootloader hand-off: MDSS GDSC " + handoff)
    out += dump(4.0, "msm_dsi ae94000.dsi", "probe", "ctrl", ctrl_probe, 0x400)
    out += dump(4.1, "msm_dsi_phy ae95000.phy", "probe", "phy", phy_probe, 0x200)
    out += dump(11.7, "msm_dsi ae94000.dsi", "enable", "ctrl", ctrl_bad, 0x404)
    out += dump(11.7, "msm_dsi_phy ae95000.phy", "enable", "phy", phy_bad, 0x200)
    out.append("[   11.917003] panel-samsung-ana38407 ae94000.dsi.0: first power-on: TCON_RDY high before reset, high 115 ms after it")
    if first == "00 00 00":
        out += ["[   12.060538] msm_dsi_host_cmd_rx:Invalid response cmd"] * 3
    out.append("[   12.063756] panel-samsung-ana38407 ae94000.dsi.0: ana38407 panel id: " + first)
    if first == "00 00 00":
        out.append("[   12.257938] panel-samsung-ana38407 ae94000.dsi.0: panel id 00 00 00: the DDIC did not answer; the panel will stay dark")
    if cycle:
        if "power-cycled" in cycle:
            out += dump(12.6, "msm_dsi ae94000.dsi", "enable", "ctrl", ctrl_good, 0x404)
            out += dump(12.6, "msm_dsi_phy ae95000.phy", "enable", "phy", phy_good, 0x200)
            out.append("[   12.800000] panel-samsung-ana38407 ae94000.dsi.0: ana38407 panel id: "
                       + cycle_answer)
        out.append("[   12.900000] msm_dpu ae01000.display-controller: boot display cycle: MDSS GDSC " + cycle)
    if later:
        out += dump(18.0, "msm_dsi ae94000.dsi", "enable", "ctrl", ctrl_good, 0x404)
        out.append("[   18.200000] panel-samsung-ana38407 ae94000.dsi.0: ana38407 panel id: " + later)
    if suspend:
        out.append("[   23.978376] PM: suspend entry (deep)")
    if first == "00 00 00" and suspend:
        out += dump(30.0, "msm_dsi ae94000.dsi", "enable", "ctrl", ctrl_good, 0x404)
        out += dump(30.0, "msm_dsi_phy ae95000.phy", "enable", "phy", phy_good, 0x200)
        out.append("[   31.473566] panel-samsung-ana38407 ae94000.dsi.0: ana38407 panel id: 80 00 04")
    if suspend:
        out.append("[   31.866597] PM: suspend exit")
    return "\n".join(out) + "\n"


class ReportTest(unittest.TestCase):
    def report(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "dmesg.txt"
            path.write_text(text)
            result = subprocess.run([sys.executable, "-I", str(KIT / "report.py"), str(path)],
                                    capture_output=True, text=True, check=True)
            return result.stdout

    def test_reference_boot_finds_the_leftovers(self):
        out = self.report(boot_log())
        self.assertIn("recovered after a system suspend cycle", out)
        self.assertIn("enable 1 at 11.700", out)
        self.assertIn("answer 00 00 00", out)
        self.assertIn("answer 80 00 04", out)
        self.assertIn("ctrl +0ac LANE_CTRL: bootloader 10000000, working 00000000", out)
        self.assertIn("phy +0a4 LANE_CTRL1", out)
        self.assertIn("leftovers (probe == bad != good, status registers excluded): 2", out)
        # CTRL changed from probe to the bad enable: mainline wrote it, so no leftover.
        self.assertNotIn("ctrl +004 CTRL: bootloader", out)

    def test_runtime_recovery_is_e3(self):
        out = self.report(boot_log(
            "systemd.mask=ubuntu-gts9u-panel-coldboot-recover.service", suspend=False,
            later="80 00 04"))
        self.assertIn("MASKED for this boot", out)
        self.assertIn("WITHOUT any system suspend (a blank/unblank: E3 PASS)", out)

    def test_e1_verdicts(self):
        out = self.report(boot_log("msm.mdss_handoff_gdsc_cycle=1", first="80 00 04",
                                   handoff="power-cycled", suspend=False))
        self.assertIn("E1 (GDSC cycle at probe): PASS", out)
        out = self.report(boot_log("msm.mdss_handoff_gdsc_cycle=1", handoff="power-cycled",
                                   suspend=False, later="80 00 04"))
        self.assertIn("E1 (GDSC cycle at probe): FAIL", out)

    def test_e2_verdict_reads_the_cycles_own_power_on(self):
        # The id of the cycle's re-enable precedes its summary line; nothing after.
        out = self.report(boot_log("msm.boot_display_cycle=1",
                                   cycle="power-cycled (noirq-style), 180 ms", suspend=False))
        self.assertIn("E2 (display-only boot cycle): PASS", out)
        self.assertIn("its power-on answered 80 00 04", out)
        self.assertIn("enable 2 at 12.600: blocks ctrl,phy, answer 80 00 04", out)
        self.assertIn("leftovers (probe == bad != good, status registers excluded): 2", out)

    def test_e2_fails_when_only_a_later_power_on_answers(self):
        out = self.report(boot_log("msm.boot_display_cycle=1",
                                   cycle="power-cycled (noirq-style), 180 ms", suspend=False,
                                   cycle_answer="00 00 00", later="80 00 04"))
        self.assertIn("E2 (display-only boot cycle): FAIL", out)
        self.assertIn("its power-on answered 00 00 00", out)

    def test_e2_gdsc_that_stayed_on_fails(self):
        out = self.report(boot_log("msm.boot_display_cycle=1", cycle="stayed on, 90 ms",
                                   suspend=False))
        self.assertIn("E2 (display-only boot cycle): FAIL - boot display cycle: MDSS GDSC stayed on", out)

    def test_e2_is_not_tested_when_the_first_power_on_answered(self):
        out = self.report(boot_log("msm.boot_display_cycle=1", first="80 00 04",
                                   cycle="power-cycled (noirq-style), 180 ms", suspend=False))
        self.assertIn("E2 (display-only boot cycle): NOT TESTED", out)

    def test_e2_warning_fails(self):
        text = boot_log("msm.boot_display_cycle=1", cycle="power-cycled (noirq-style), 180 ms",
                        suspend=False)
        text = text.replace("[   12.900000]", "[   12.890000] msm_dpu ae01000.display-controller:"
                            " boot display cycle: re-enable failed: -110\n[   12.900000]")
        out = self.report(text)
        self.assertIn("E2 (display-only boot cycle): FAIL", out)
        self.assertIn("re-enable failed: -110", out)

    def test_first_power_on_that_answers_is_flagged(self):
        out = self.report(boot_log(first="80 00 04"))
        self.assertIn("the FIRST power-on answered", out)

    def test_log_without_dumps(self):
        text = "\n".join(l for l in boot_log().splitlines() if "regdump" not in l)
        self.assertIn("no regdump lines", self.report(text))


class KitTest(unittest.TestCase):
    def test_checksums_cover_the_kit(self):
        listed = {}
        for line in (KIT / "SHA256SUMS").read_text().splitlines():
            digest, name = line.split(None, 1)
            listed[name.lstrip("*")] = digest
        files = sorted(p.name for p in KIT.iterdir() if p.is_file() and p.name != "SHA256SUMS")
        self.assertEqual(sorted(listed), files)
        for name in files:
            self.assertEqual(hashlib.sha256((KIT / name).read_bytes()).hexdigest(),
                             listed[name], name)

    def test_images_listed(self):
        names = [l.split()[1] for l in (KIT / "IMAGES.sha256").read_text().splitlines()]
        self.assertEqual(names, ["panel-%s/vendor_boot.img" % v for v in ("ref", "e0", "e1", "e2")])

    def test_shell_syntax(self):
        for name in ("apply.sh", "rollback.sh", "collect.sh", "e3.sh"):
            subprocess.run(["dash", "-n", str(KIT / name)], check=True)
        subprocess.run(["bash", "-n", str(KIT / "make-images.sh")], check=True)

    def dispcc_block(self, runtime_status):
        """Run collect.sh's dispcc block on a simulated debugfs and sysfs."""
        text = (KIT / "collect.sh").read_text()
        start = text.index("{\n\tr=$(ls -d")
        end = text.index('} > "$out/dispcc.txt"')
        block = text[start:end] + "}\n"
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            regs = root / "regmap/af00000.clock-controller"
            regs.mkdir(parents=True)
            special = {0x9000: 0x80008001, 0xf000: 0x00000001}
            with open(regs / "registers", "w") as f:
                for reg in range(0, 0x11008 + 4, 4):
                    f.write("%05x: %08x\n" % (reg, special.get(reg, reg)))
            power = root / "devices/af00000.clock-controller/power"
            power.mkdir(parents=True)
            (power / "runtime_status").write_text(runtime_status + "\n")
            script = root / "block.sh"
            script.write_text("d=%s\ndev=%s/devices\n%s" % (root, root, block))
            return subprocess.run(["dash", str(script)], capture_output=True, text=True,
                                  check=True).stdout.splitlines()

    def test_dispcc_registers_are_read_by_seeking(self):
        """collect.sh reads single regmap debugfs lines; check the offsets."""
        self.assertEqual(self.dispcc_block("active"),
                         ["08000: 00008000", "09000: 80008001", "0a000: 0000a000",
                          "0b000: 0000b000", "0f000: 00000001"])

    def test_dispcc_registers_are_not_read_while_it_is_suspended(self):
        self.assertEqual(self.dispcc_block("suspended"),
                         ["dispcc runtime_status 'suspended': registers not read"])

    @unittest.skipUnless(os.environ.get("PANEL_BASE_VENDOR_BOOT"), "no fix2-s2 vendor_boot")
    def test_make_images_reproduces_the_kit_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["bash", str(KIT / "make-images.sh"),
                            os.environ["PANEL_BASE_VENDOR_BOOT"], directory],
                           check=True, capture_output=True)


class FlashTest(unittest.TestCase):
    """apply.sh / rollback.sh with files standing in for the partitions."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        root = self.tmp
        (root / "dev").mkdir()
        self.boot = root / "dev/boot"
        self.vb = root / "dev/vendor_boot"
        for path, fill in ((self.boot, b"B"), (self.vb, b"0")):
            path.write_bytes(fill * 4096)
            os.truncate(path, SIZE)
        (root / "compat").write_bytes(b"samsung,gts9wifi\0qcom,sm8550\0")
        self.images = root / "display-exp"
        self.kit = root / "kit"
        self.kit.mkdir()
        lines = []
        for variant, fill in (("panel-ref", b"R"), ("panel-e0", b"0"), ("panel-e1", b"1"),
                              ("panel-e2", b"2")):
            d = self.images / variant
            d.mkdir(parents=True)
            img = d / "vendor_boot.img"
            img.write_bytes(fill * 8192)
            os.truncate(img, SIZE)
            (d / "CMDLINE.txt").write_text("added=test-%s\n" % variant)
            lines.append("%s  %s/vendor_boot.img" % (self.sha(img), variant))
        (self.kit / "IMAGES.sha256").write_text("\n".join(lines) + "\n")
        for name in ("apply.sh", "rollback.sh"):
            text = (KIT / name).read_text()
            for old, new in (("/proc/device-tree/compatible", str(root / "compat")),
                             ("/dev/disk/by-partlabel/", str(root / "dev") + "/"),
                             ("/var/backups/ubuntu-gts9u-panel-exp", str(root / "backup")),
                             ("/var/log/gts9u-panel-exp.log", str(root / "log")),
                             ("iflag=direct ", ""),
                             ('[ "$(id -u)" = 0 ]', "true")):
                text = text.replace(old, new)
            (self.kit / name).write_text(text)
        subprocess.run("sha256sum IMAGES.sha256 apply.sh rollback.sh > SHA256SUMS",
                       shell=True, cwd=self.kit, check=True)

    @staticmethod
    def sha(path):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def run_kit(self, name, *args, env=None):
        environ = dict(os.environ, PANEL_EXP_IMAGES=str(self.images), **(env or {}))
        return subprocess.run(["dash", str(self.kit / name), *args], env=environ,
                              capture_output=True, text=True)

    def test_refuses_a_kernel_that_is_not_fix2_s2(self):
        before = self.sha(self.vb)
        result = self.run_kit("apply.sh", "panel-e1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not the fix2-s2 kernel", result.stdout)
        self.assertEqual(self.sha(self.vb), before)

    def test_refuses_another_board(self):
        (self.tmp / "compat").write_bytes(b"samsung,gts9uwifi\0")
        result = self.run_kit("apply.sh", "panel-e1", env={"FORCE_BOOT": "1"})
        self.assertIn("not an SM-X710", result.stdout)

    def test_refuses_an_image_that_does_not_match(self):
        with open(self.images / "panel-e2/vendor_boot.img", "r+b") as f:
            f.write(b"X")
        result = self.run_kit("apply.sh", "panel-e2", env={"FORCE_BOOT": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected", result.stdout)

    def test_apply_backs_up_once_and_rollback_restores(self):
        original = self.sha(self.vb)
        result = self.run_kit("apply.sh", "panel-e1", env={"FORCE_BOOT": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.sha(self.vb), self.sha(self.images / "panel-e1/vendor_boot.img"))
        self.assertIn("command-line additions: test-panel-e1", result.stdout)
        self.assertIn("OK vendor_boot <- panel-e1", (self.tmp / "log").read_text())
        result = self.run_kit("apply.sh", "panel-e2", env={"FORCE_BOOT": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.sha(self.tmp / "backup/vendor_boot.first.img"), original)
        result = self.run_kit("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.sha(self.vb), original)
        self.assertIn("not fix2-s2's vendor_boot", result.stdout)

    def test_a_vendor_boot_flashed_after_a_rollback_is_backed_up_again(self):
        """Another kit's vendor_boot installed between two experiment rounds
        must come back at the second rollback, not the first round's backup."""
        result = self.run_kit("apply.sh", "panel-e1", env={"FORCE_BOOT": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first_backup = self.sha(self.tmp / "backup/vendor_boot.first.img")
        result = self.run_kit("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with open(self.vb, "r+b") as f:          # e.g. the wifi kit's vendor_boot
            f.write(b"W" * 4096)
        foreign = self.sha(self.vb)
        result = self.run_kit("apply.sh", "panel-e2", env={"FORCE_BOOT": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("kept the older backup", result.stdout)
        self.assertEqual(self.sha(self.tmp / "backup/vendor_boot.first.img"), foreign)
        self.assertEqual(self.sha(self.tmp / "backup/vendor_boot.prev.img"), first_backup)
        result = self.run_kit("apply.sh", "panel-e0", env={"FORCE_BOOT": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.sha(self.tmp / "backup/vendor_boot.first.img"), foreign)
        result = self.run_kit("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.sha(self.vb), foreign)

    def test_reapplying_over_the_same_vendor_boot_keeps_one_backup(self):
        result = self.run_kit("apply.sh", "panel-ref", env={"FORCE_BOOT": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.run_kit("rollback.sh")
        result = self.run_kit("apply.sh", "panel-ref", env={"FORCE_BOOT": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("already holds this vendor_boot", result.stdout)
        self.assertFalse((self.tmp / "backup/vendor_boot.prev.img").exists())


# A stand-in for the kernel, run as `sleep`: e3.sh polls with sleep, so each
# call lets the simulated display react to what the script wrote to fb0.
KERNEL_SIM = r"""#!/bin/sh
s=%(root)s
blank=$(cat "$s/fb0_blank")
if [ "$blank" = 4 ] && [ "$(cat "$s/scenario_collapse")" = yes ]; then
	echo suspended > "$s/mdss_status"
	echo off-0 > "$s/gd/current_state"
	printf 'State          Time Spent(ms) Usage      Rejected\nS0             10             6          0\n' > "$s/gd/idle_states"
elif [ "$blank" = 0 ] && [ ! -e "$s/unblanked" ]; then
	: > "$s/unblanked"
	echo active > "$s/mdss_status"
	echo on > "$s/gd/current_state"
	cat "$s/scenario_answer" > "$s/panel_id"
	echo "[  99.0] panel-samsung-ana38407 ae94000.dsi.0: ana38407 panel id: $(cat "$s/scenario_answer")" >> "$s/dmesg"
fi
"""

CHVT_SIM = r"""#!/bin/sh
s=%(root)s
echo "tty$1" > "$s/active_vt"
if [ -s "$s/scenario_vt_answer" ]; then
	cat "$s/scenario_vt_answer" > "$s/panel_id"
	echo "[  90.0] panel-samsung-ana38407 ae94000.dsi.0: ana38407 panel id: $(cat "$s/scenario_vt_answer")" >> "$s/dmesg"
fi
"""


class E3Test(unittest.TestCase):
    """e3.sh against a simulated display: files for sysfs/debugfs, stubs for
    chvt, dmesg and sleep (the last one advances the simulated kernel)."""

    def run_e3(self, collapse=True, answer="80 00 04", vt_answer="", cmdline=None):
        root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        (root / "gd").mkdir()
        (root / "bin").mkdir()
        files = {
            "panel_id": "00 00 00\n", "mdss_status": "active\n", "fb0_blank": "",
            "active_vt": "tty1\n", "state_synced": "1\n",
            "cmdline": cmdline if cmdline is not None else
            "root=LABEL=x msm.dsi_regdump=1 systemd.mask=ubuntu-gts9u-panel-coldboot-recover.service\n",
            "dmesg": "[  12.0] panel-samsung-ana38407 ae94000.dsi.0: ana38407 panel id: 00 00 00\n",
            "gd/current_state": "on\n",
            "gd/idle_states": "State          Time Spent(ms) Usage      Rejected\nS0             10             5          0\n",
            "scenario_collapse": "yes\n" if collapse else "no\n",
            "scenario_answer": answer + "\n", "scenario_vt_answer": vt_answer,
        }
        for name, content in files.items():
            (root / name).write_text(content)
        for name, body in (("sleep", KERNEL_SIM), ("chvt", CHVT_SIM),
                           ("dmesg", '#!/bin/sh\ncat %(root)s/dmesg\n')):
            path = root / "bin" / name
            path.write_text(body % {"root": root})
            path.chmod(0o755)
        text = (KIT / "e3.sh").read_text()
        for old, new in (('[ "$(id -u)" = 0 ]', "true"),
                         ("/var/log/gts9u-panel-exp.log", str(root / "log")),
                         ("/var/log/gts9u-panel-exp/", str(root / "exp") + "/"),
                         ("/sys/class/backlight/ae94000.dsi.0/panel_id", str(root / "panel_id")),
                         ("/sys/bus/platform/devices/ae00000.display-subsystem/power/runtime_status",
                          str(root / "mdss_status")),
                         ("/sys/kernel/debug/pm_genpd/mdss_gdsc", str(root / "gd")),
                         ("/sys/class/graphics/fb0/blank", str(root / "fb0_blank")),
                         ("/sys/devices/genpd_provider/mdss_gdsc/state_synced",
                          str(root / "state_synced")),
                         ("/sys/class/tty/tty0/active", str(root / "active_vt")),
                         ("/proc/cmdline", str(root / "cmdline")),
                         ('sh "$kit/collect.sh" e3-before', 'mkdir -p "%s/exp/$(cut -c1-8 '
                          '/proc/sys/kernel/random/boot_id)/01-e3-before"' % root),
                         ('sh "$kit/collect.sh" e3-after', 'mkdir -p "%s/exp/$(cut -c1-8 '
                          '/proc/sys/kernel/random/boot_id)/02-e3-after"' % root)):
            self.assertIn(old, text)
            text = text.replace(old, new)
        script = root / "e3.sh"
        script.write_text(text)
        env = dict(os.environ, PATH="%s:%s" % (root / "bin", os.environ["PATH"]))
        result = subprocess.run(["dash", str(script)], capture_output=True, text=True,
                                env=env, timeout=60)
        return result, root

    def test_pass(self):
        result, root = self.run_e3()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("blanked: MDSS suspended", result.stdout)
        self.assertIn("E3 PASS", result.stdout)
        self.assertIn("sudo chvt 1", result.stdout)
        self.assertIn("02-e3-after", result.stdout)
        self.assertEqual((root / "active_vt").read_text().strip(), "tty3")

    def test_fail(self):
        result, _root = self.run_e3(answer="00 00 00")
        self.assertIn("E3 FAIL", result.stdout)

    def test_inconclusive_without_a_collapse(self):
        result, _root = self.run_e3(collapse=False, answer="00 00 00")
        self.assertIn("E3 INCONCLUSIVE", result.stdout)

    def test_panel_side_recovery(self):
        result, _root = self.run_e3(collapse=False)
        self.assertIn("E3 RECOVERED WITHOUT A GDSC COLLAPSE", result.stdout)

    def test_vt_switch_modeset_is_reported(self):
        result, _root = self.run_e3(vt_answer="80 00 04")
        self.assertIn("the switch to VT 3 re-powered the panel", result.stdout)
        self.assertIn("E3 NOT TESTED", result.stdout)
        self.assertNotIn("E3 PASS", result.stdout)

    def test_refuses_a_boot_that_ran_the_cycle(self):
        result, root = self.run_e3(cmdline="root=LABEL=x msm.dsi_regdump=1\n")
        self.assertEqual(result.returncode, 2)
        self.assertEqual((root / "fb0_blank").read_text(), "")


if __name__ == "__main__":
    unittest.main()
