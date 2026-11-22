"""The UFS PHY's PCS reset for both tablets, and the diagnostic behind
APPLY_QMP_UFS_PCS_STATE_DIAGNOSTIC (2026-11-22, the SM-X710's UFS did not
come back from a deep sleep).

The build script's switch is run from its own text: off by default, 0 or 1
only, never without the PCS reset, applied strictly and once, and a tree
that carries the diagnostic is refused for a default build.  With the pinned
kernel checkout at hand (KERNEL_SRC, or the build script's default place)
the patches are applied in build order to its phy-qcom-qmp-ufs.c, the same
order on both boards, and scripts/test-ufs-resume-experiment.py runs the
real calibration with mocked MMIO; the checkout is only read.  None of this
touches a PHY: what the reset does to a sleep is for the tablet to show.
"""
from pathlib import Path
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "scripts/build-mainline-kernel.sh"
STAGE = ROOT / "scripts/stage-ufs-resume-experiment.sh"
PATCHES = ROOT / "kernel/patches"
PCS_RESET = PATCHES / "qmp-ufs-assert-pcs-reset-before-calibration-gts9u.patch"
DIAGNOSTIC = PATCHES / "qmp-ufs-pcs-state-diagnostic.patch"
SERDES = PATCHES / "qmp-ufs-reset-serdes-before-power-down-gts9u.patch"
QMP = "drivers/phy/qualcomm/phy-qcom-qmp-ufs.c"
MARKER = "Diagnostic for SM8550 resume tests, not for release builds"


def kernel_source():
    """The pinned kernel checkout, as tests/test_typec_dock_roles.py finds it."""
    base = os.environ.get("UBUNTU_WORKDIR", str(Path.home() / "ubuntu-gts9u"))
    source = Path(os.environ.get("KERNEL_SRC", base + "/linux-mainline"))
    return source if (source / ".git").exists() and shutil.which("patch") else None


def pinned(path):
    return subprocess.run(["git", "-C", str(kernel_source()), "show", "HEAD:" + path],
                          check=True, capture_output=True, text=True).stdout


def block(start, end):
    """The build script's lines from the one starting with START to the
    first line after it that is END."""
    text = BUILD.read_text()
    lines = text.splitlines(True)
    first = next(i for i, line in enumerate(lines) if line.startswith(start))
    last = next(i for i in range(first, len(lines)) if lines[i] == end)
    return "".join(lines[first:last + 1])


def switch_check():
    """The top of the build script: the switch's default, its validation and
    its refusal without the PCS reset."""
    text = BUILD.read_text()
    validate = re.search(r"^validate_bool\(\) \{.*?^\}\n", text, re.M | re.S).group(0)
    default = re.search(r"^apply_qmp_ufs_diagnostic=.*\n", text, re.M).group(0)
    return default + validate + block("validate_bool APPLY_QMP_UFS_PCS_STATE_DIAGNOSTIC", "fi\n")


def apply_block():
    """The build script's diagnostic block, after the stage script."""
    return block("qmp_ufs_source=", "fi\n")


def run_bash(script, *args, env=None):
    return subprocess.run(["bash", "-c", "set -euo pipefail\n" + script, "build", *args],
                          capture_output=True, text=True, env=env)


class SwitchTest(unittest.TestCase):
    def test_the_switch_is_off_by_default_and_0_or_1(self):
        text = BUILD.read_text()
        self.assertIn("\napply_qmp_ufs_diagnostic=${APPLY_QMP_UFS_PCS_STATE_DIAGNOSTIC:-0}\n", text)
        check = switch_check()
        for value, pcs, ok in (("", None, True), ("0", None, True), ("1", None, True), ("1", "1", True),
                               ("0", "0", True), ("1", "0", False), ("yes", None, False),
                               ("2", None, False), ("01", None, False)):
            with self.subTest(diagnostic=value, pcs=pcs):
                env = {k: v for k, v in os.environ.items()
                       if k not in ("APPLY_QMP_UFS_PCS_STATE_DIAGNOSTIC", "UFS_PCS_RESET_EXPERIMENTAL")}
                if value:
                    env["APPLY_QMP_UFS_PCS_STATE_DIAGNOSTIC"] = value
                if pcs is not None:
                    env["UFS_PCS_RESET_EXPERIMENTAL"] = pcs
                result = run_bash(check, env=env)
                self.assertEqual(result.returncode == 0, ok, result.stderr)
                if value == "1" and pcs == "0":
                    self.assertIn("needs the PCS reset (UFS_PCS_RESET_EXPERIMENTAL=1)", result.stderr)
                elif not ok:
                    self.assertIn("APPLY_QMP_UFS_PCS_STATE_DIAGNOSTIC must be 0 or 1", result.stderr)

    def test_the_diagnostic_goes_on_after_the_pcs_reset_on_both_boards(self):
        """The UFS PHY's three steps are next to each other, in this order,
        and none depends on the board: the build gives both tablets the same
        driver."""
        text = BUILD.read_text()
        serdes = text.index("apply_unless 'static void qmp_ufs_tx_pull_down_ctrl'")
        stage = text.index('bash "$repo/scripts/stage-ufs-resume-experiment.sh" "$kernel_tree"')
        diag = text.index(apply_block())
        self.assertLess(serdes, stage)
        self.assertLess(stage, diag)
        region = text[serdes:diag + len(apply_block())]
        self.assertNotIn("DEVICE", region)
        self.assertNotIn("gts9", region.replace("qmp-ufs-reset-serdes-before-power-down-gts9u.patch", ""))
        for patch in (PCS_RESET, DIAGNOSTIC):
            added = [line for line in patch.read_text().splitlines()
                     if line.startswith("+") and not line.startswith("+++")]
            self.assertTrue(added)
            self.assertNotIn("of_machine_is_compatible", "\n".join(added), patch.name)
        self.assertIn(MARKER, DIAGNOSTIC.read_text())
        self.assertEqual(apply_block().count(f"'{MARKER}'"), 1)

    def test_no_other_patch_touches_the_ufs_phy_driver(self):
        """So the order checked above is the whole of what the build does to
        the file, on either board."""
        touching = sorted(p.name for p in PATCHES.glob("*.patch")
                          if re.search(rf"^\+\+\+ b/{re.escape(QMP)}", p.read_text(), re.M))
        self.assertEqual(touching, sorted([SERDES.name, PCS_RESET.name, DIAGNOSTIC.name]))


@unittest.skipUnless(kernel_source(), "no pinned kernel checkout")
class PinnedSourceTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tree = Path(directory.name)
        self.file = self.tree / QMP
        self.file.parent.mkdir(parents=True)
        self.file.write_text(pinned(QMP))
        # What the build has done to the file when the diagnostic's turn comes.
        subprocess.run(["patch", "-s", "-d", str(self.tree), "-p1"], input=SERDES.read_text(),
                       check=True, text=True)
        subprocess.run(["bash", str(STAGE), str(self.tree)], check=True, capture_output=True,
                       env={k: v for k, v in os.environ.items() if k != "UFS_PCS_RESET_EXPERIMENTAL"})
        self.staged = self.file.read_text()

    def apply(self, value):
        env = dict(os.environ, apply_qmp_ufs_diagnostic=value)
        return run_bash("kernel_tree=$1 pat=$2 apply_qmp_ufs_diagnostic=$3\n" + apply_block(),
                        str(self.tree), str(PATCHES), value, env=env)

    def test_the_switch_applies_the_diagnostic_once_and_a_default_build_refuses_it(self):
        result = self.apply("0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.file.read_text(), self.staged)
        # The SERDES patch left a backup (it goes in at an offset); the
        # diagnostic leaves none.
        files = sorted(p.name for p in self.file.parent.iterdir())
        result = self.apply("1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotRegex(result.stdout, "offset|fuzz")
        with_diagnostic = self.file.read_text()
        self.assertIn(MARKER, with_diagnostic)
        self.assertEqual(sorted(p.name for p in self.file.parent.iterdir()), files)
        # Again: a reused tree, nothing changes.
        result = self.apply("1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.file.read_text(), with_diagnostic)
        # A default build in that tree: refused, the tree as it was.
        result = self.apply("0")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("The UFS PHY diagnostic remains in this source tree", result.stderr)
        self.assertEqual(self.file.read_text(), with_diagnostic)
        # A copy with the marker but not the whole patch is refused as well.
        damaged = with_diagnostic.replace('"no PCS_READY within 1 s of SERDES_START\\n"',
                                          '"no PCS_READY\\n"')
        self.assertNotEqual(damaged, with_diagnostic)
        self.file.write_text(damaged)
        result = self.apply("1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.file.read_text(), damaged)
        # And so is the marker alone, somewhere else in a tree without the
        # diagnostic (patch --reverse --batch would pass it).
        elsewhere = self.staged + f"/* {MARKER} */\n"
        self.file.write_text(elsewhere)
        result = self.apply("1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.file.read_text(), elsewhere)

    def test_the_pcs_reset_has_no_board_check_and_the_old_form_had_one(self):
        calibrate = re.search(r"^static int qmp_ufs_phy_calibrate\(.*?^\}\n", self.staged, re.M | re.S)
        self.assertNotIn("of_machine_is_compatible", calibrate.group(0))
        self.assertIn("if (cfg == &sm8550_ufsphy_cfg && !cfg->no_pcs_sw_reset)\n"
                      "\t\tqphy_setbits(pcs, cfg->regs[QPHY_SW_RESET], SW_RESET);\n", calibrate.group(0))
        # The SM-X910's condition up to revision 7 was this one and its board:
        # the same write on the same PHY.
        legacy = re.search(r"<<'LEGACY_PCS_RESET'\n(.*?\n)LEGACY_PCS_RESET\n", STAGE.read_text(), re.S)
        old = "".join(line[1:] + "\n" for line in legacy.group(1).splitlines() if line.startswith("+"))
        self.assertIn('of_machine_is_compatible("samsung,gts9uwifi")', old)
        self.assertEqual(re.sub(r"\s+", " ", old.replace('of_machine_is_compatible("samsung,gts9uwifi") &&', "")
                                .split("*/", 1)[1]).strip(),
                         "if (cfg == &sm8550_ufsphy_cfg && !cfg->no_pcs_sw_reset) "
                         "qphy_setbits(pcs, cfg->regs[QPHY_SW_RESET], SW_RESET);")

    def test_the_diagnostic_reads_the_v6_registers_the_kalama_driver_names(self):
        """Its offsets against the pinned headers: the v6 PCS layout of
        sm8550_ufsphy_cfg (the Kalama driver's PHY_BASE 0x400 plus START 0x0,
        POWER_DOWN_CONTROL 0x4, SW_RESET 0x8, PCS_READY_STATUS 0x1a8) and the
        v6 SerDes common block's two status registers."""
        driver = pinned(QMP)
        pcs = pinned("drivers/phy/qualcomm/phy-qcom-qmp-pcs-ufs-v6.h")
        com = pinned("drivers/phy/qualcomm/phy-qcom-qmp-qserdes-com-v6.h")
        cfg = re.search(r"^static const struct qmp_phy_cfg sm8550_ufsphy_cfg = \{.*?^\};", driver,
                        re.M | re.S).group(0)
        self.assertIn(".offsets\t\t= &qmp_ufs_offsets_v6,", cfg)
        self.assertIn(".regs\t\t\t= ufsphy_v6_regs_layout,", cfg)
        offsets = re.search(r"qmp_ufs_offsets_v6 = \{(.*?)\};", driver, re.S).group(1)
        self.assertRegex(offsets, r"\.serdes\s*= 0,")
        self.assertRegex(offsets, r"\.pcs\s*= 0x0400,")
        layout = re.search(r"ufsphy_v6_regs_layout\[QPHY_LAYOUT_SIZE\] = \{(.*?)\};", driver, re.S).group(1)
        for reg, name, value in (("QPHY_START_CTRL", "QPHY_V6_PCS_UFS_PHY_START", "0x000"),
                                 ("QPHY_PCS_READY_STATUS", "QPHY_V6_PCS_UFS_READY_STATUS", "0x1a8"),
                                 ("QPHY_SW_RESET", "QPHY_V6_PCS_UFS_SW_RESET", "0x008"),
                                 ("QPHY_PCS_POWER_DOWN_CONTROL", "QPHY_V6_PCS_UFS_POWER_DOWN_CONTROL",
                                  "0x004")):
            with self.subTest(reg):
                self.assertRegex(layout, rf"\[{reg}\]\s*= {name},")
                self.assertRegex(pcs, rf"#define {name}\s+{value}\n")
        self.assertRegex(com, r"#define QSERDES_V6_COM_CMN_STATUS\s+0x1d0\n")
        self.assertRegex(com, r"#define QSERDES_V6_COM_C_READY_STATUS\s+0x1f8\n")
        diagnostic = DIAGNOSTIC.read_text()
        for read in ("readl(pcs + cfg->regs[QPHY_SW_RESET])", "readl(pcs + cfg->regs[QPHY_START_CTRL])",
                     "readl(pcs + cfg->regs[QPHY_PCS_POWER_DOWN_CONTROL])",
                     "readl(pcs + cfg->regs[QPHY_PCS_READY_STATUS])",
                     "readl(qmp->serdes + QSERDES_V6_COM_C_READY_STATUS)",
                     "readl(qmp->serdes + QSERDES_V6_COM_CMN_STATUS)"):
            self.assertIn(read, diagnostic)
        self.assertEqual(diagnostic.count("readl("), 6)

    def test_the_calibration_and_the_diagnostic_run_with_mocked_mmio(self):
        with tempfile.NamedTemporaryFile("w", suffix=".c") as source:
            source.write(pinned(QMP))
            source.flush()
            result = subprocess.run([sys.executable, str(ROOT / "scripts/test-ufs-resume-experiment.py"),
                                     source.name], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS: the diagnostic reads the six registers before the PCS reset", result.stdout)


if __name__ == "__main__":
    unittest.main()
