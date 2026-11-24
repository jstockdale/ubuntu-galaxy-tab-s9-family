"""The fixes for the clocks that keep XO voted through a system sleep, offline.

scripts/build-mainline-kernel.sh applies each behind a variable of its own:
lpass-va-macro-pm-clock-framework.patch (upstream eb667d0fbdd3, v7.3-rc1)
with APPLY_LPASS_VA_MACRO_PM_CLK=1, and
msm-dsi-phy-release-ahb-clock-in-system-sleep.patch (posted and reviewed
upstream, not merged) with APPLY_DSI_PHY_AHB_RELEASE=1, and
qcom-geni-serial-force-suspend-in-system-sleep.patch (upstream d0cd9c8d0fd5
with 3098c989bd38, v7.3) with APPLY_GENI_SERIAL_FORCE_SUSPEND=1.  The build
script's block runs from its own text on files taken from the pinned kernel
checkout (only read, with git show): off by default, 0 or 1 only, the patch applied strictly and
once, a tree that carries it refused for a build without the variable, a
damaged copy refused.  The patch's own content is checked against what its
header says.  Nothing is built and no kernel tree is written.
"""
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
BUILD = REPO / "scripts/build-mainline-kernel.sh"
PATCHES = REPO / "kernel/patches"
VA = "sound/soc/codecs/lpass-va-macro.c"
KCONFIG = "sound/soc/codecs/Kconfig"
VA_PATCH = "lpass-va-macro-pm-clock-framework.patch"
DSI_PHY = "drivers/gpu/drm/msm/dsi/phy/dsi_phy"
DSI_PATCH = "msm-dsi-phy-release-ahb-clock-in-system-sleep.patch"
GENI = "drivers/tty/serial/qcom_geni_serial.c"
GENI_PATCH = "qcom-geni-serial-force-suspend-in-system-sleep.patch"
GENI_IRQ_PATCH = "qcom-geni-serial-serve-irq-while-suspending.patch"


def kernel_source():
    """The pinned kernel checkout, as the other kit tests find it."""
    base = os.environ.get("UBUNTU_WORKDIR", str(pathlib.Path.home() / "ubuntu-gts9u"))
    source = pathlib.Path(os.environ.get("KERNEL_SRC", base + "/linux-mainline"))
    return source if (source / ".git").exists() and shutil.which("patch") else None


def pinned(path):
    return subprocess.run(["git", "-C", str(kernel_source()), "show", "HEAD:" + path], check=True,
                          capture_output=True, text=True).stdout


def targets(patch):
    return re.findall(r"^\+\+\+ b/(\S+)", (PATCHES / patch).read_text(), re.M)


def block(start, end_marker="\nfi\n"):
    """The build script's lines from START to the block's last fi."""
    text = BUILD.read_text()
    i = text.index(start)
    return text[i:text.index(end_marker, i) + len(end_marker)]


def validate(text):
    return re.search(r"^validate_bool\(\) \{.*?^\}\n", text, re.M | re.S).group(0)


class VariableTest(unittest.TestCase):
    CASES = (("APPLY_LPASS_VA_MACRO_PM_CLK", "apply_va_macro_pm_clk"),
             ("APPLY_DSI_PHY_AHB_RELEASE", "apply_dsi_phy_ahb"),
             ("APPLY_GENI_SERIAL_FORCE_SUSPEND", "apply_geni_force_suspend"))

    def test_each_variable_is_off_by_default_and_0_or_1(self):
        text = BUILD.read_text()
        for env, var in self.CASES:
            with self.subTest(env):
                assign = f"{var}=${{{env}:-0}}\n"
                check = f'validate_bool {env} "${var}"\n'
                self.assertIn("\n" + assign, text)
                self.assertIn("\n" + check, text)
                for value, ok in (("", True), ("0", True), ("1", True), ("yes", False), ("2", False)):
                    environ = {k: v for k, v in os.environ.items() if k != env}
                    if value:
                        environ[env] = value
                    result = subprocess.run(["bash", "-c", "set -euo pipefail\n" + assign + validate(text) + check],
                                            capture_output=True, text=True, env=environ)
                    self.assertEqual(result.returncode == 0, ok, (value, result.stderr))
                    if not ok:
                        self.assertIn(f"{env} must be 0 or 1", result.stderr)

    def test_no_board_gate(self):
        for start in ("# lpass-va-macro-pm-clock-framework.patch, upstream eb667d0fbdd3",
                      "# msm-dsi-phy-release-ahb-clock-in-system-sleep.patch, posted upstream",
                      "# qcom-geni-serial-force-suspend-in-system-sleep.patch, upstream d0cd9c8d0fd5"):
            with self.subTest(start):
                self.assertNotIn("DEVICE", block(start))


class VaMacroTest(unittest.TestCase):
    START = "# lpass-va-macro-pm-clock-framework.patch, upstream eb667d0fbdd3"

    def run_block(self, tree, value):
        return subprocess.run(["bash", "-c", "set -euo pipefail\nkernel_tree=$1 pat=$2 apply_va_macro_pm_clk=$3\n"
                               + block(self.START), "build", str(tree), str(PATCHES), value],
                              capture_output=True, text=True)

    def test_the_patch_is_upstream_s_commit_on_the_two_files_and_says_so(self):
        text = (PATCHES / VA_PATCH).read_text()
        self.assertTrue(text.startswith("From eb667d0fbdd38d5a800b9e7aafc9a6c14530b9bf "), text[:80])
        self.assertIn("commit eb667d0fbdd38d5a800b9e7aafc9a6c14530b9bf upstream (v7.3-rc1).", text)
        self.assertIn("Link: https://patch.msgid.link/20260702-xo-sd-codec-v7-b4-v8-2-d39d0fdb7859@oss.qualcomm.com",
                      text)
        self.assertEqual(sorted(targets(VA_PATCH)), sorted([KCONFIG, VA]))
        added = [line[1:] for line in text.splitlines() if line.startswith("+") and not line.startswith("+++")]
        self.assertIn("\tdepends on PM_CLK", added)
        self.assertIn("\tret = devm_pm_clk_create(dev);", added)
        self.assertIn("\tret = pm_clk_suspend(dev);", added)
        self.assertIn("\tpm_runtime_set_autosuspend_delay(dev, 100);", added)
        # The probe's clk_prepare_enable() of the two vote clocks goes.
        removed = [line[1:] for line in text.splitlines() if line.startswith("-") and not line.startswith("---")]
        self.assertIn("\tret = clk_prepare_enable(va->macro);", removed)
        self.assertIn("\tret = clk_prepare_enable(va->dcodec);", removed)
        # Its header says it changes behaviour outside system suspend, and
        # which variable applies it.
        self.assertIn("This changes the VA macro's runtime behaviour outside system suspend", text)
        self.assertIn("APPLY_LPASS_VA_MACRO_PM_CLK=1", text)

    def test_no_other_patch_touches_the_two_files(self):
        for patch in PATCHES.glob("*.patch"):
            if patch.name == VA_PATCH:
                continue
            with self.subTest(patch.name):
                self.assertFalse({VA, KCONFIG} & set(targets(patch.name)))

    def tree(self, tmp):
        tree = pathlib.Path(tmp)
        for path in (VA, KCONFIG):
            (tree / path).parent.mkdir(parents=True, exist_ok=True)
            (tree / path).write_text(pinned(path))
        return tree

    @unittest.skipUnless(kernel_source(), "no pinned kernel checkout")
    def test_the_variable_applies_it_once_and_a_build_without_it_refuses_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = self.tree(tmp)
            files = {p: (tree / p).read_text() for p in (VA, KCONFIG)}
            result = self.run_block(tree, "0")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual({p: (tree / p).read_text() for p in files}, files)
            result = self.run_block(tree, "1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("fuzz", result.stdout)
            patched = {p: (tree / p).read_text() for p in files}
            self.assertNotEqual(patched, files)
            self.assertEqual(patched[VA].count("\n\tret = pm_clk_suspend(dev);\n"), 1)
            self.assertNotIn("clk_prepare_enable(va->macro)", patched[VA])
            self.assertEqual(sorted(p.name for p in (tree / VA).parent.iterdir()), ["Kconfig", "lpass-va-macro.c"])
            # Again: a reused tree, nothing changes.
            result = self.run_block(tree, "1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual({p: (tree / p).read_text() for p in files}, patched)
            # A build without the variable in that tree: refused, unchanged.
            result = self.run_block(tree, "0")
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(f"{VA_PATCH} remains in this source tree; use a fresh KERNEL_WORKTREE/KERNEL_BUILD_DIR for a "
                          "build without APPLY_LPASS_VA_MACRO_PM_CLK=1.", result.stderr)
            self.assertEqual({p: (tree / p).read_text() for p in files}, patched)
            # A copy that has the marker but not the whole patch: refused.
            damaged = patched[VA].replace("\tpm_runtime_set_autosuspend_delay(dev, 100);\n", "")
            self.assertNotEqual(damaged, patched[VA])
            (tree / VA).write_text(damaged)
            result = self.run_block(tree, "1")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((tree / VA).read_text(), damaged)


class DsiPhyAhbTest(unittest.TestCase):
    START = "# msm-dsi-phy-release-ahb-clock-in-system-sleep.patch, posted upstream"
    PATCH = DSI_PATCH

    def run_block(self, tree, value):
        return subprocess.run(["bash", "-c", "set -euo pipefail\nkernel_tree=$1 pat=$2 apply_dsi_phy_ahb=$3\n"
                               + block(self.START), "build", str(tree), str(PATCHES), value],
                              capture_output=True, text=True)

    def test_the_patch_is_the_posted_one_on_the_phy_s_two_files_and_says_so(self):
        text = (PATCHES / self.PATCH).read_text()
        self.assertIn("Subject: [PATCH] drm/msm/dsi: fix AHB clock staying enabled across suspend", text)
        self.assertIn("Link: https://lore.kernel.org/all/20261006-dsi-ahb-clk-suspend-fix-v1-1-67da848e1b0a@oss.qualcomm.com/",
                      text)
        self.assertIn("Reviewed-by: Dmitry Baryshkov <dmitry.baryshkov@oss.qualcomm.com>", text)
        self.assertEqual(sorted(targets(self.PATCH)), sorted([DSI_PHY + ".c", DSI_PHY + ".h"]))
        diff = text[text.index("\n--- a/"):]
        added = [l for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]
        removed = [l for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")]
        # The posting: 127 lines in, 9 out.
        self.assertEqual((len(added), len(removed)), (127, 9))
        self.assertIn("+\tLATE_SYSTEM_SLEEP_PM_OPS(dsi_phy_suspend_late, dsi_phy_resume_early)", added)
        self.assertIn("+\tdev_pm_set_driver_flags(dev, DPM_FLAG_NO_DIRECT_COMPLETE);", added)
        self.assertIn("-\tret = pm_clk_add(dev, \"iface\");", removed)
        self.assertIn("APPLY_DSI_PHY_AHB_RELEASE=1", text)

    def test_it_goes_on_top_of_the_other_patch_of_its_file(self):
        """msm-dsi-handoff-regdump.patch is the only other patch of
        dsi_phy.c (none touches dsi_phy.h), and the build applies it first."""
        others = sorted(p.name for p in PATCHES.glob("*.patch")
                        if p.name != self.PATCH and {DSI_PHY + ".c", DSI_PHY + ".h"} & set(targets(p.name)))
        self.assertEqual(others, ["msm-dsi-handoff-regdump.patch"])
        text = BUILD.read_text()
        self.assertLess(text.index("drivers/gpu/drm/msm/dsi/dsi_manager.c msm-dsi-handoff-regdump.patch"),
                        text.index(block(self.START)))

    def tree(self, tmp):
        """The PHY's files as the build has them when the block runs: the
        pinned ones with msm-dsi-handoff-regdump.patch."""
        tree = pathlib.Path(tmp)
        for path in (DSI_PHY + ".c", DSI_PHY + ".h", "drivers/gpu/drm/msm/dsi/dsi.h",
                     "drivers/gpu/drm/msm/dsi/dsi_host.c", "drivers/gpu/drm/msm/dsi/dsi_manager.c"):
            (tree / path).parent.mkdir(parents=True, exist_ok=True)
            (tree / path).write_text(pinned(path))
        subprocess.run(["patch", "-s", "-d", str(tree), "-p1", "--no-backup-if-mismatch"],
                       input=(PATCHES / "msm-dsi-handoff-regdump.patch").read_text(), check=True, text=True)
        return tree

    @unittest.skipUnless(kernel_source(), "no pinned kernel checkout")
    def test_the_variable_applies_it_once_and_a_build_without_it_refuses_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = self.tree(tmp)
            names = (DSI_PHY + ".c", DSI_PHY + ".h")
            files = {p: (tree / p).read_text() for p in names}
            listing = sorted(p.name for p in (tree / DSI_PHY).parent.iterdir())
            result = self.run_block(tree, "0")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual({p: (tree / p).read_text() for p in names}, files)
            result = self.run_block(tree, "1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("fuzz", result.stdout)
            patched = {p: (tree / p).read_text() for p in names}
            self.assertEqual(patched[DSI_PHY + ".c"].count("static int dsi_phy_suspend_late(struct device *dev)"), 1)
            self.assertNotIn("pm_clk_add", patched[DSI_PHY + ".c"])
            self.assertIn("\tbool ahb_clk_prepared;\n", patched[DSI_PHY + ".h"])
            # The port's regdump stays as it was.
            self.assertIn("msm_dsi_phy_regdump(phy, \"probe\");", patched[DSI_PHY + ".c"])
            self.assertEqual(sorted(p.name for p in (tree / DSI_PHY).parent.iterdir()), listing)
            result = self.run_block(tree, "1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual({p: (tree / p).read_text() for p in names}, patched)
            result = self.run_block(tree, "0")
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(f"{self.PATCH} remains in this source tree; use a fresh KERNEL_WORKTREE/KERNEL_BUILD_DIR "
                          "for a build without APPLY_DSI_PHY_AHB_RELEASE=1.", result.stderr)
            self.assertEqual({p: (tree / p).read_text() for p in names}, patched)
            damaged = patched[DSI_PHY + ".h"].replace("\tbool ahb_clk_enabled;\n", "")
            (tree / (DSI_PHY + ".h")).write_text(damaged)
            result = self.run_block(tree, "1")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((tree / (DSI_PHY + ".h")).read_text(), damaged)


class GeniSerialForceSuspendTest(unittest.TestCase):
    """The console's and the Bluetooth UART's resources off in system
    sleep: upstream's two commits, folded, on the geni UART driver, with or
    without the SM-X710's interrupt patch (another function)."""
    START = "# qcom-geni-serial-force-suspend-in-system-sleep.patch, upstream d0cd9c8d0fd5"
    # The two callbacks as v7.3 and v7.4-rc4 have them, less e95442c31ae6's
    # pm_runtime_resume() of the console, which that tree needs because
    # aee1f94dab13 removed the uart_ops .pm callback this tree keeps.
    SUSPEND = """static int qcom_geni_serial_suspend(struct device *dev)
{
\tstruct qcom_geni_serial_port *port = dev_get_drvdata(dev);
\tstruct uart_port *uport = &port->uport;
\tstruct qcom_geni_private_data *private_data = uport->private_data;
\tint ret;

\t/*
\t * This is done so we can hit the lowest possible state in suspend
\t * even with no_console_suspend
\t */
\tif (uart_console(uport)) {
\t\tgeni_icc_set_tag(&port->se, QCOM_ICC_TAG_ACTIVE_ONLY);
\t\tgeni_icc_set_bw(&port->se);
\t}

\tret = uart_suspend_port(private_data->drv, uport);
\tif (ret)
\t\treturn ret;

\t/*
\t * When no_console_suspend is set the console must remain active
\t * across system sleep, so skip the force suspend path.
\t */
\tif (!console_suspend_enabled && uart_console(uport))
\t\treturn 0;

\treturn pm_runtime_force_suspend(dev);
}
"""
    RESUME = """static int qcom_geni_serial_resume(struct device *dev)
{
\tint ret;
\tstruct qcom_geni_serial_port *port = dev_get_drvdata(dev);
\tstruct uart_port *uport = &port->uport;
\tstruct qcom_geni_private_data *private_data = uport->private_data;

\tif (console_suspend_enabled || !uart_console(uport)) {
\t\tret = pm_runtime_force_resume(dev);
\t\tif (ret)
\t\t\treturn ret;
\t}

\tret = uart_resume_port(private_data->drv, uport);
\tif (uart_console(uport)) {
\t\tgeni_icc_set_tag(&port->se, QCOM_ICC_TAG_ALWAYS);
\t\tgeni_icc_set_bw(&port->se);
\t}
\treturn ret;
}
"""

    def run_block(self, tree, value):
        return subprocess.run(["bash", "-c", "set -euo pipefail\nkernel_tree=$1 pat=$2 apply_geni_force_suspend=$3\n"
                               + block(self.START), "build", str(tree), str(PATCHES), value],
                              capture_output=True, text=True)

    def test_the_patch_is_upstream_s_two_commits_on_the_driver_and_says_so(self):
        text = (PATCHES / GENI_PATCH).read_text()
        self.assertTrue(text.startswith("From d0cd9c8d0fd59bc7d140f3d60cf02e1d80376dab "), text[:80])
        self.assertIn("commit d0cd9c8d0fd59bc7d140f3d60cf02e1d80376dab upstream (v7.3-rc1), with\n"
                      "its fix 3098c989bd38edff871d798acd7f1d1b269f7edd upstream (v7.3-rc6,", text)
        self.assertIn("Link: https://patch.msgid.link/20260702-add_force_suspend_resume_to_system_sleep_callbacks-v2-1-"
                      "b79e254a7015@oss.qualcomm.com", text)
        self.assertIn("Fixes: d0cd9c8d0fd5 (\"serial: qcom-geni: add force suspend/resume to system sleep callbacks\")",
                      text)
        self.assertEqual(targets(GENI_PATCH), [GENI])
        diff = text[text.index("\n--- a/"):]
        added = [l for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]
        removed = [l for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")]
        self.assertEqual((len(added), len(removed)), (20, 1))
        self.assertEqual(removed, ["-\treturn uart_suspend_port(private_data->drv, uport);"])
        self.assertIn("+\treturn pm_runtime_force_suspend(dev);", added)
        self.assertIn("+\t\tret = pm_runtime_force_resume(dev);", added)
        self.assertIn(" 1 file changed, 20 insertions(+), 1 deletion(-)", text)
        # What it changes, the variable, both boards, and what it leaves.
        self.assertIn("APPLY_GENI_SERIAL_FORCE_SUSPEND=1", text)
        self.assertIn("No board gate: both tablets have the same two\nUARTs", text)
        self.assertIn("aee1f94dab13, v7.3-rc1, and fixed what that broke in 2de1066dc859 and\n"
                      "e95442c31ae6; none of the three is taken", text)

    def test_no_patch_but_the_interrupt_one_touches_the_driver(self):
        others = sorted(p.name for p in PATCHES.glob("*.patch") if p.name != GENI_PATCH and GENI in targets(p.name))
        self.assertEqual(others, [GENI_IRQ_PATCH])

    def test_the_build_applies_it_for_both_boards_after_the_interrupt_patch(self):
        """The interrupt patch is the SM-X710's alone; this block is not
        inside its board test, and comes after it."""
        text = BUILD.read_text()
        irq = text.index("\t\tqcom-geni-serial-serve-irq-while-suspending.patch\nfi\n")
        self.assertLess(irq, text.index(block(self.START)))
        self.assertNotIn("DEVICE", block(self.START))

    @unittest.skipUnless(kernel_source(), "no pinned kernel checkout")
    def test_its_callbacks_are_upstream_s_with_or_without_the_interrupt_patch(self):
        for irq_first in (False, True):
            with self.subTest(irq_first=irq_first), tempfile.TemporaryDirectory() as tmp:
                tree = pathlib.Path(tmp)
                (tree / GENI).parent.mkdir(parents=True)
                (tree / GENI).write_text(pinned(GENI))
                order = [GENI_IRQ_PATCH, GENI_PATCH] if irq_first else [GENI_PATCH, GENI_IRQ_PATCH]
                for name in order:
                    result = subprocess.run(["patch", "--forward", "--batch", "--fuzz=0", "--no-backup-if-mismatch",
                                             "-d", str(tree), "-p1"], input=(PATCHES / name).read_text(),
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                text = (tree / GENI).read_text()
                self.assertEqual(text.count(self.SUSPEND), 1)
                self.assertEqual(text.count(self.RESUME), 1)
                self.assertNotIn("\tif (uport->suspended)\n\t\treturn IRQ_NONE;\n", text)
                self.assertEqual(sorted(p.name for p in (tree / GENI).parent.iterdir()), ["qcom_geni_serial.c"])

    @unittest.skipUnless(kernel_source(), "no pinned kernel checkout")
    def test_the_variable_applies_it_once_and_a_build_without_it_refuses_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = pathlib.Path(tmp)
            (tree / GENI).parent.mkdir(parents=True)
            # As an SM-X710 build has the file when the block runs.
            (tree / GENI).write_text(pinned(GENI))
            subprocess.run(["patch", "-s", "-d", str(tree), "-p1", "--no-backup-if-mismatch"],
                           input=(PATCHES / GENI_IRQ_PATCH).read_text(), check=True, text=True)
            before = (tree / GENI).read_text()
            result = self.run_block(tree, "0")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((tree / GENI).read_text(), before)
            result = self.run_block(tree, "1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("fuzz", result.stdout)
            patched = (tree / GENI).read_text()
            self.assertEqual(patched.count("\treturn pm_runtime_force_suspend(dev);\n"), 1)
            self.assertIn(self.RESUME, patched)
            self.assertEqual(sorted(p.name for p in (tree / GENI).parent.iterdir()), ["qcom_geni_serial.c"])
            # Again: a reused tree, nothing changes.
            result = self.run_block(tree, "1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((tree / GENI).read_text(), patched)
            # A build without the variable in that tree: refused, unchanged.
            result = self.run_block(tree, "0")
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(f"{GENI_PATCH} remains in this source tree; use a fresh KERNEL_WORKTREE/KERNEL_BUILD_DIR for "
                          "a build without APPLY_GENI_SERIAL_FORCE_SUSPEND=1.", result.stderr)
            self.assertEqual((tree / GENI).read_text(), patched)
            # A copy with the marker but not the whole patch: refused.
            damaged = patched.replace("\t\tret = pm_runtime_force_resume(dev);\n", "\t\tret = 0;\n")
            self.assertNotEqual(damaged, patched)
            (tree / GENI).write_text(damaged)
            result = self.run_block(tree, "1")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((tree / GENI).read_text(), damaged)


if __name__ == "__main__":
    unittest.main()
