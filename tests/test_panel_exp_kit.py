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
             suspend=True, leftover=True):
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
        out.append("[   12.900000] msm_dpu ae01000.display-controller: boot display cycle: MDSS GDSC " + cycle)
    if suspend:
        out.append("[   23.978376] PM: suspend entry (deep)")
    if first == "00 00 00":
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
            "systemd.mask=ubuntu-gts9u-panel-coldboot-recover.service", suspend=False))
        self.assertIn("MASKED for this boot", out)
        self.assertIn("WITHOUT any system suspend (a blank/unblank: E3 PASS)", out)

    def test_e1_verdicts(self):
        out = self.report(boot_log("msm.mdss_handoff_gdsc_cycle=1", first="80 00 04",
                                   handoff="power-cycled", suspend=False))
        self.assertIn("E1 (GDSC cycle at probe): PASS", out)
        out = self.report(boot_log("msm.mdss_handoff_gdsc_cycle=1", handoff="power-cycled",
                                   suspend=False))
        self.assertIn("E1 (GDSC cycle at probe): FAIL", out)

    def test_e2_verdict(self):
        out = self.report(boot_log("msm.boot_display_cycle=1",
                                   cycle="power-cycled (noirq-style), 180 ms", suspend=False))
        self.assertIn("E2 (display-only boot cycle): PASS", out)

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


if __name__ == "__main__":
    unittest.main()
