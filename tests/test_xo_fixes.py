"""The fixes for the clocks that keep XO voted through a system sleep, offline.

scripts/build-mainline-kernel.sh applies each behind a variable of its own:
lpass-va-macro-pm-clock-framework.patch (upstream eb667d0fbdd3, v7.3-rc1)
with APPLY_LPASS_VA_MACRO_PM_CLK=1.  The build script's block runs from its
own text on files taken from the pinned kernel checkout (only read, with
git show): off by default, 0 or 1 only, the patch applied strictly and
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
    CASES = (("APPLY_LPASS_VA_MACRO_PM_CLK", "apply_va_macro_pm_clk"),)

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
        for start in ("# lpass-va-macro-pm-clock-framework.patch, upstream eb667d0fbdd3",):
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


if __name__ == "__main__":
    unittest.main()
