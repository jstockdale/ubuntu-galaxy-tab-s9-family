"""The UFS PHY power reference that ufs-qcom keeps track of, and its
diagnostic behind APPLY_QMP_UFS_PCS_STATE_DIAGNOSTIC (2026-11-23, the
SM-X710's Type-C kit revision 10, run 5: a resume's 15 calibrations started
without the PHY's power-on, and the UFS was lost).

The fix goes into every build, on both boards, with no variable: its line in
the build script is checked where it stands.  The diagnostic's block is run
from its own text: applied strictly and once with the variable, nothing
without it, and a tree that carries it is refused for a build without it.
With the pinned kernel checkout at hand (KERNEL_SRC, or the build script's
default place) the patches go in at the build's order on both boards with no
fuzz, make-kit.sh's checks that each is whole in the build tree pass and
refuse a tree without them, and its replay on the hang-log tree's files
gives the same files; the checkout is only read.
scripts/test-ufs-phy-power-balance.py, a step model of the code paths that
change the PHY's power count, shows the race the fix closes.  None of this
touches a PHY.
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
MAKE_KIT = ROOT / "tools/inplace/typec-roles/make-kit.sh"
MODEL = ROOT / "scripts/test-ufs-phy-power-balance.py"
PATCHES = ROOT / "kernel/patches"
FIX = PATCHES / "ufs-qcom-track-phy-power-state-gts9u.patch"
DIAGNOSTIC = PATCHES / "ufs-qcom-phy-power-count-diagnostic.patch"
G5 = PATCHES / "ufs-qcom-hs-g5-at-300mhz-sm8550.patch"
LANES = PATCHES / "ufs-qcom-enable-only-lane-clocks.patch"
UFS_QCOM = "drivers/ufs/host/ufs-qcom"
# The line the build script knows the fix by, and the diagnostic's marker.
FIX_MARKER = "Power the PHY off if this host holds its power, so that the power-on"
DIAG_MARKER = "static void ufs_qcom_dbg_phy_counts("


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
    lines = BUILD.read_text().splitlines(True)
    first = next(i for i, line in enumerate(lines) if line.startswith(start))
    last = next(i for i in range(first, len(lines)) if lines[i] == end)
    return "".join(lines[first:last + 1])


def apply_unless():
    return re.search(r"^apply_unless\(\) \{.*?^\}\n", BUILD.read_text(), re.M | re.S).group(0)


def fix_line():
    """The build script's two lines that apply the fix."""
    return re.search(r"^apply_unless '" + re.escape(FIX_MARKER) + r"' \\\n\t[^\n]*\n", BUILD.read_text(),
                     re.M).group(0)


def diagnostic_block():
    return block("ufs_qcom_source=", "fi\n")


def lanes_step():
    """sleep_fix() and its call for the lane clock fix."""
    text = BUILD.read_text()
    function = re.search(r"^sleep_fix\(\) \{ # FILE LINE PATCH\n.*?^\}\n", text, re.M | re.S).group(0)
    call = re.search(r"^sleep_fix drivers/ufs/host/ufs-qcom\.c \\\n\t[^\n]*\\\n\t" + re.escape(LANES.name) + "\n",
                     text, re.M).group(0)
    return function + call


def run_bash(script, *args):
    return subprocess.run(["bash", "-c", "set -euo pipefail\n" + script, "build", *args],
                          capture_output=True, text=True)


def build_step(script, tree, diag="0", fixes="0"):
    """SCRIPT, a part of the build script, on TREE, with the two variables."""
    return run_bash("kernel_tree=$1 pat=$2 apply_qmp_ufs_diagnostic=$3 apply_sm8550_sleep_fixes=$4\n" + script,
                    str(tree), str(PATCHES), diag, fixes)


def files(tree):
    return {str(p.relative_to(tree)): p.read_text() for p in tree.rglob("*") if p.is_file()}


def code_lines(patch, target):
    """The lines PATCH adds to TARGET, comments left out."""
    added, current = [], None
    for line in patch.read_text().splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith("+") and current == target:
            added.append(line[1:])
    code, comment = [], False
    for line in added:
        stripped = line.strip()
        if stripped.startswith("/*"):
            comment = not stripped.endswith("*/")
            continue
        if comment:
            comment = not stripped.endswith("*/")
            continue
        code.append(line)
    return code


class BuildScriptTest(unittest.TestCase):
    def test_the_fix_goes_into_every_build_after_the_hs_g5_patch_and_before_the_sleep_fixes(self):
        """No variable and no board: the line stands at the top level of the
        script (not inside an if), right after the SM-X710's HS-G5 block,
        with nothing but comments between, and before the sleep fixes, whose
        lane clock fix goes on top of it."""
        text = BUILD.read_text()
        line = fix_line()
        self.assertEqual(text.count(line), 1)
        self.assertEqual(line, f"apply_unless '{FIX_MARKER}' \\\n\tdrivers/ufs/host/ufs-qcom.c {FIX.name}\n")
        g5 = ("if [ \"$DEVICE\" = gts9wifi ]; then\n\tapply_unless 'it runs HS-G5' \\\n"
              "\t\tdrivers/ufs/host/ufs-qcom.c ufs-qcom-hs-g5-at-300mhz-sm8550.patch\nfi\n")
        self.assertEqual(text.count(g5), 1)
        between = text[text.index(g5) + len(g5):text.index(line)]
        self.assertTrue(all(l.startswith("#") for l in between.splitlines()), between)
        self.assertLess(text.index(line), text.index("sleep_fix() { # FILE LINE PATCH\n"))
        for part in (between, line, diagnostic_block()):
            self.assertNotIn("DEVICE", part)
            self.assertNotIn("gts9", part.replace("gts9u.patch", ""))
        # The diagnostic's block follows it; both before the sleep fixes.
        self.assertLess(text.index(line), text.index(diagnostic_block()))
        self.assertLess(text.index(diagnostic_block()), text.index("sleep_fix() { # FILE LINE PATCH\n"))
        # The marker is a line the fix adds, the same for grep -q's basic
        # regular expression as for a fixed string, and no other patch's.
        self.assertIsNone(re.search(r"[.*\[\]^$\\]", FIX_MARKER))
        self.assertEqual(len([l for l in FIX.read_text().splitlines() if l.startswith("+") and FIX_MARKER in l]), 1)
        self.assertEqual([p.name for p in PATCHES.glob("*.patch") if FIX_MARKER in p.read_text()], [FIX.name])
        self.assertEqual([p.name for p in PATCHES.glob("*.patch") if DIAG_MARKER in p.read_text()],
                         [DIAGNOSTIC.name])

    def test_the_diagnostic_comes_with_the_ufs_phy_s_diagnostic_switch_alone(self):
        block_text = diagnostic_block()
        self.assertIn(f"\nufs_qcom_count_marker='{DIAG_MARKER}'\n", block_text)
        self.assertTrue(block_text.startswith("ufs_qcom_source=$kernel_tree/drivers/ufs/host/ufs-qcom.c\n"))
        self.assertIn('if [ "$apply_qmp_ufs_diagnostic" = 1 ]; then\n', block_text)
        self.assertNotIn("apply_sm8550_sleep_fixes", block_text)
        self.assertEqual(block_text.count(f'< "$pat/{DIAGNOSTIC.name}"'), 2)
        self.assertIn("patch --dry-run --reverse --force --fuzz=0 -d", block_text)
        self.assertIn("patch --forward --batch --fuzz=0 --no-backup-if-mismatch -d", block_text)
        self.assertIn("The ufs-qcom PHY count diagnostic remains in this source tree; use a fresh "
                      "KERNEL_WORKTREE/KERNEL_BUILD_DIR for a default build.", block_text)

    def test_the_diagnostic_reads_and_logs_and_writes_nothing(self):
        """Its C: two counters of its own in struct ufs_qcom_host, reads of
        the PHY's counts, three kinds of log line; no register access, no
        call that changes the PHY or the host, no assignment but its
        counters' and two local pointers, and no line of the fix's or of the
        file's removed."""
        code = code_lines(DIAGNOSTIC, f"{UFS_QCOM}.c")
        joined = "\n".join(code)
        for word in ("writel", "readl", "qphy_", "regmap", "phy_power_on(", "phy_power_off(", "phy_init(",
                     "phy_exit(phy)", "ufs_qcom_phy_power_", "ufshcd_", "mutex", "return -", "return ret"):
            self.assertNotIn(word, joined, word)
        # Assignments outside string literals: the two local pointers and the
        # counter's increment.
        bare = [re.sub(r'"(?:[^"\\]|\\.)*"', '""', line) for line in code]
        assigned = [line.strip() for line in bare if re.search(r"(?<![=!<>])=(?!=)", line)]
        self.assertEqual(assigned, ["struct phy *phy = host->generic_phy;", "struct ufs_hba *hba = host->hba;",
                                    "unsigned int n = on ? ++host->dbg_phy_repeat_on :"])
        self.assertEqual(re.findall(r"\+\+[\w>-]+", joined), ["++host->dbg_phy_repeat_on",
                                                              "++host->dbg_phy_repeat_off"])
        self.assertIn("#define UFS_QCOM_DBG_PHY_REPEAT_LOGGED\t16U", joined)
        self.assertIn('ufs_qcom_dbg_phy_counts(host, KERN_DEBUG, "power-up sequence entry");', joined)
        self.assertIn("ufs_qcom_dbg_phy_counts(host, KERN_WARNING,", joined)
        self.assertIn("current_work() == &hba->clk_gating.ungate_work ?", joined)
        self.assertIn("\tif (!host->phy_powered_on)\n\t\t\t\tufs_qcom_dbg_phy_repeat(host, false);", joined)
        self.assertIn("\tif (host->phy_powered_on)\n\t\t\t\tufs_qcom_dbg_phy_repeat(host, true);", joined)
        self.assertEqual(code_lines(DIAGNOSTIC, f"{UFS_QCOM}.h"),
                         ["\tunsigned int dbg_phy_repeat_on;", "\tunsigned int dbg_phy_repeat_off;"])
        self.assertFalse([line for line in DIAGNOSTIC.read_text().splitlines()
                          if line.startswith("-") and not line.startswith("---")])

    def test_the_fix_takes_and_drops_the_reference_through_its_flag_alone(self):
        """The fix's two helpers return at once when the host's flag already
        says so; ufs_qcom_power_up_sequence() drops the reference the flag
        says the host holds, not by phy->power_count; setup_clocks() and
        exit() call the helpers; nothing else calls phy_power_on() or
        phy_power_off() (the pinned file, the fix on it, below)."""
        text = FIX.read_text()
        removed = [line[1:].strip() for line in text.splitlines() if line.startswith("-") and
                   not line.startswith("---")]
        self.assertIn("if (phy->power_count)", removed)
        self.assertIn("phy_power_off(host->generic_phy);", removed)
        code = "\n".join(code_lines(FIX, f"{UFS_QCOM}.c"))
        self.assertNotIn("power_count", code)
        self.assertIn("\tif (host->phy_powered_on)\n\t\treturn 0;\n\n\tret = phy_power_on(host->generic_phy);", code)
        self.assertIn("\tif (!host->phy_powered_on)\n\t\treturn;\n\n\tret = phy_power_off(host->generic_phy);", code)
        self.assertEqual(code.count("host->phy_powered_on = true;"), 1)
        self.assertEqual(code.count("host->phy_powered_on = false;"), 1)
        self.assertEqual(code_lines(FIX, f"{UFS_QCOM}.h"), ["\tbool phy_powered_on;"])
        # Its context in the header: right after is_dev_ref_clk_enabled, the
        # same with and without the lane clock fix.
        self.assertIn("\tbool is_dev_ref_clk_enabled;\n+\tbool phy_powered_on;\n", text)


@unittest.skipUnless(kernel_source(), "no pinned kernel checkout")
class PinnedSourceTest(unittest.TestCase):
    """The patches on the pinned ufs-qcom.c and ufs-qcom.h, in the order and
    the way the build applies them."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)

    def tree(self, name, g5):
        """The two files as the build has them when the fix's turn comes:
        pinned, and on the SM-X710 (G5) with the HS-G5 patch."""
        root = self.tmp / name
        for suffix in (".c", ".h"):
            path = root / f"{UFS_QCOM}{suffix}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(pinned(f"{UFS_QCOM}{suffix}"))
        if g5:
            subprocess.run(["patch", "-s", "-d", str(root), "-p1"], input=G5.read_text(), check=True, text=True)
        return root

    def strict(self, root, patch, reverse=False):
        subprocess.run(["patch", "-s", "--batch", "--fuzz=0", "--no-backup-if-mismatch", "-d", str(root), "-p1"]
                       + (["--reverse", "--force"] if reverse else ["--forward"]),
                       input=patch.read_text(), check=True, capture_output=True, text=True)

    def build(self, root, diag, fixes):
        """The fix's line, the diagnostic's block, the lane clock fix: each
        step's output."""
        out = []
        for script in (apply_unless() + fix_line(), diagnostic_block(), lanes_step()):
            result = build_step(script, root, diag, fixes)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            out.append(result.stdout)
        return out

    def make_kit_part(self, start, end):
        """make-kit.sh's lines from the one with START to the end of END."""
        text = MAKE_KIT.read_text()
        first = text.index(start)
        return text[first:text.index(end, first) + len(end)]

    def make_kit_run(self, part, tree=None, lanes_only=False):
        """PART of make-kit.sh with the build tree TREE ($tree) and a work
        directory of its own ($tmp), ufs_qcom_patches as make-kit.sh has
        them; with LANES_ONLY its revision 10 lists cut to the lane clock
        fix (the other two fixes' files are not here)."""
        text = MAKE_KIT.read_text()
        lists = re.search(r"^ufs_qcom_patches=\(.*?\)\n", text, re.M).group(0)
        if lanes_only:
            self.assertIn(f"\tufs-qcom-enable-only-lane-clocks.patch)\n", text)
            lists += f"fix_patches=({LANES.name})\nfix_files=(\"$ufs_qcom.c\")\n"
        work = Path(tempfile.mkdtemp(dir=self.tmp))
        result = run_bash('die() { echo "$*" >&2; exit 1; }\ntree=$1 tmp=$2 repo=$3 ufs_qcom=' + UFS_QCOM + "\n"
                          + lists + part, str(tree or self.tmp), str(work), str(ROOT))
        return result, work

    def test_the_build_s_order_goes_in_with_no_fuzz_on_both_boards(self):
        """On the SM-X710 the HS-G5 patch first; then the fix (its line
        numbers are a lane-clocked file's: 31 lines up, one in the header,
        and patch(1) keeps the files as they were as .orig), the diagnostic
        and the lane clock fix, the last with no offset.  The two boards'
        files differ by the HS-G5 patch alone, and a default build's by the
        fix alone."""
        finals = {}
        for board, g5 in (("SM-X710", True), ("SM-X910", False)):
            with self.subTest(board):
                root = self.tree(f"build-{board}", g5)
                before = files(root)
                fixed, diagnosed, laned = self.build(root, "1", "1")
                for out in (fixed, diagnosed, laned):
                    self.assertNotIn("fuzz", out)
                self.assertNotIn("offset", laned)
                self.assertEqual(re.findall(r"offset (-?\d+) lines", fixed), ["-31"] * 7 + ["-1"])
                self.assertEqual(re.findall(r"offset (-?\d+) lines", diagnosed), ["-31"] * 4 + ["-1"])
                after = files(root)
                self.assertEqual(sorted(after), sorted(list(before) + [f"{UFS_QCOM}.c.orig", f"{UFS_QCOM}.h.orig"]))
                self.assertEqual(after[f"{UFS_QCOM}.c.orig"], before[f"{UFS_QCOM}.c"])
                self.assertEqual(after[f"{UFS_QCOM}.h.orig"], before[f"{UFS_QCOM}.h"])
                # The same files as the three applied strictly, one by one.
                strict = self.tree(f"strict-{board}", g5)
                for patch in (FIX, DIAGNOSTIC, LANES):
                    self.strict(strict, patch)
                self.assertEqual({p: after[p] for p in (f"{UFS_QCOM}.c", f"{UFS_QCOM}.h")}, files(strict))
                finals[board] = files(strict)
                # The C: phy_power_on() and phy_power_off() only in the
                # helpers, and the PHY's counts read in the diagnostic alone.
                c = re.sub(r"/\*.*?\*/", "", after[f"{UFS_QCOM}.c"], flags=re.S)
                self.assertEqual(len(re.findall(r"(?<!\w)phy_power_on\(", c)), 1)
                self.assertEqual(len(re.findall(r"(?<!\w)phy_power_off\(", c)), 1)
                # The helpers: defined once, called by setup_clocks() (one
                # each), the power-up sequence (one each) and exit() (off).
                self.assertEqual(len(re.findall(r"(?<!\w)ufs_qcom_phy_power_on\(", c)), 3)
                self.assertEqual(len(re.findall(r"(?<!\w)ufs_qcom_phy_power_off\(", c)), 4)
                counts = re.search(r"^static void ufs_qcom_dbg_phy_counts\(.*?^\}\n", c, re.M | re.S).group(0)
                self.assertEqual(c.count("power_count"), counts.count("power_count"))
                self.assertEqual(counts.count("power_count"), 2)
                # A default build: the fix alone, the same way.
                default = self.tree(f"default-{board}", g5)
                for out in self.build(default, "0", "0"):
                    self.assertNotIn("fuzz", out)
                only = self.tree(f"only-{board}", g5)
                self.strict(only, FIX)
                self.assertEqual({p: files(default)[p] for p in (f"{UFS_QCOM}.c", f"{UFS_QCOM}.h")}, files(only))
                self.assertNotIn("power_count", files(default)[f"{UFS_QCOM}.c"])
        # The HS-G5 patch is the only difference between the boards.
        x910 = self.tmp / "x910-from-x710"
        for path, text in finals["SM-X710"].items():
            (x910 / path).parent.mkdir(parents=True, exist_ok=True)
            (x910 / path).write_text(text)
        self.strict(x910, G5, reverse=True)
        self.assertEqual(files(x910), finals["SM-X910"])

    def test_make_kit_finds_each_patch_whole_and_its_replay_gives_the_same_files(self):
        """make-kit.sh's checks of the build tree: the lane clock fix comes
        out whole from the top (revision 10's check), and the diagnostic,
        then the fix, come out whole from under it, which leaves the files
        revision 10 built.  A tree without the diagnostic, or without both,
        is refused, the build tree only read.  Its replay on the hang-log
        tree's files (the HS-G5 patch alone on the SM-X710) puts the lane
        clock fix in first and the two after it, and gives the build's
        files."""
        lanes_check = self.make_kit_part("# Revision 10's sleep fixes: each upstream commit whole in the build tree",
                                         "die \"the build tree's $f does not carry ${fix_patches[$i]} whole\"\ndone\n")
        ufsq_check = self.make_kit_part("# Revision 11's ufs-qcom patches: both whole in the build tree's two files,",
                                        "do not carry ${ufs_qcom_patches[$i]} whole\"\ndone\n")
        self.assertIn('[ "$f" != "$ufs_qcom.c" ] || cp "$tree/$ufs_qcom.h" "$tmp/fix/$ufs_qcom.h"', lanes_check)
        # The replay: the lane clock fix as revision 10's loop applies it,
        # then revision 11's two (the PCIe PHY's unpark reverse between them
        # is not for these files).
        loop = self.make_kit_part('\tfor p in "${fix_patches[@]}"; do\n\t\tpatch -s --forward --batch --fuzz=0',
                                  '\t\t\tdie "$p does not apply to the hang-log tree"\n\tdone\n')
        ufsq_replay = self.make_kit_part("\t# Revision 11's ufs-qcom patches, strictly: here on top of the lane clock",
                                         '\t\t\tdie "$p does not apply to the hang-log tree"\n\tdone\n')
        self.assertLess(MAKE_KIT.read_text().index(loop), MAKE_KIT.read_text().index(ufsq_replay))
        for board, g5 in (("SM-X710", True), ("SM-X910", False)):
            with self.subTest(board):
                root = self.tree(f"kit-{board}", g5)
                self.build(root, "1", "1")
                built = files(root)
                r10 = self.tree(f"r10-{board}", g5)
                self.strict(r10, LANES)
                result, _ = self.make_kit_run(lanes_check, root, lanes_only=True)
                self.assertEqual((result.returncode, result.stderr), (0, ""))
                result, work = self.make_kit_run(ufsq_check, root)
                self.assertEqual((result.returncode, result.stderr), (0, ""))
                self.assertEqual(files(work / "ufsq"), files(r10))
                self.assertEqual(files(root), built)
                # Refused: revision 10's files, the fix without the diagnostic
                # (on the lane clock fix, as a reused tree of revision 10's
                # would have it), each naming the diagnostic.
                fix_only = self.tree(f"fix-only-{board}", g5)
                for patch in (LANES, FIX):
                    self.strict(fix_only, patch)
                for name, tree in (("revision 10's", r10), ("the fix alone", fix_only)):
                    with self.subTest(board=board, tree=name):
                        before = files(tree)
                        result, _ = self.make_kit_run(ufsq_check, tree)
                        self.assertEqual(result.returncode, 1, result.stderr)
                        self.assertIn(f"do not carry {DIAGNOSTIC.name} whole", result.stderr)
                        self.assertEqual(files(tree), before)
                # The replay, on the hang-log build's two files.
                hang = self.tree(f"hang-{board}", g5)
                result, work = self.make_kit_run("mkdir -p \"$tmp/src/${ufs_qcom%/*}\"\n"
                                                 "cp \"$tree/$ufs_qcom.c\" \"$tree/$ufs_qcom.h\" \"$tmp/src/${ufs_qcom%/*}/\"\n"
                                                 + loop + ufsq_replay, hang, lanes_only=True)
                self.assertEqual((result.returncode, result.stderr), (0, ""))
                self.assertEqual(files(work / "src"), {p: t for p, t in built.items() if not p.endswith(".orig")})

    def run_diagnostic(self, root, value):
        return build_step(diagnostic_block(), root, value)

    def test_the_diagnostic_block_applies_it_once_and_a_build_without_the_variable_refuses_it(self):
        root = self.tree("diag", True)
        self.strict(root, FIX)
        fixed = files(root)
        # Without the variable: nothing.
        result = self.run_diagnostic(root, "0")
        self.assertEqual((result.returncode, files(root)), (0, fixed), result.stderr)
        # With it: once, strictly, no backup.
        result = self.run_diagnostic(root, "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("fuzz", result.stdout)
        diagnosed = files(root)
        self.assertEqual(sorted(diagnosed), sorted(fixed))
        self.assertEqual(diagnosed[f"{UFS_QCOM}.c"].count(DIAG_MARKER), 1)
        # A reused tree: nothing changes.
        result = self.run_diagnostic(root, "1")
        self.assertEqual((result.returncode, files(root)), (0, diagnosed), result.stderr)
        # A build without the variable in that tree: refused, as it was.
        result = self.run_diagnostic(root, "0")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("The ufs-qcom PHY count diagnostic remains in this source tree", result.stderr)
        self.assertEqual(files(root), diagnosed)
        # A copy with the marker and not the whole patch: refused with the
        # variable, and without it.
        damaged = diagnosed[f"{UFS_QCOM}.c"].replace("\t\t   \"in the ungate work\" : \"not in the ungate work\");\n",
                                                     "\t\t   \"\");\n")
        self.assertNotEqual(damaged, diagnosed[f"{UFS_QCOM}.c"])
        for text in (damaged, fixed[f"{UFS_QCOM}.c"] + f"/* {DIAG_MARKER} */\n"):
            (root / f"{UFS_QCOM}.c").write_text(text)
            result = self.run_diagnostic(root, "1")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((root / f"{UFS_QCOM}.c").read_text(), text)
            result = self.run_diagnostic(root, "0")
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual((root / f"{UFS_QCOM}.c").read_text(), text)
        # On a file without the fix the diagnostic does not go in.
        bare = self.tree("bare", False)
        result = self.run_diagnostic(bare, "1")
        self.assertNotEqual(result.returncode, 0, result.stdout)


class ModelTest(unittest.TestCase):
    def test_the_step_model_shows_the_race_and_that_the_fix_closes_it(self):
        """scripts/test-ufs-phy-power-balance.py, run as its docstring says:
        the old code leaves a reference after the gate work gave up on a
        command being dispatched, the next sleep's 15 calibrations all start
        without the PHY's power-on, and the fixed code is balanced after
        every interleaving."""
        result = subprocess.run([sys.executable, "-I", str(MODEL)], capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(result.stdout.endswith("ALL CHECKS MET\n"), result.stdout)
        self.assertNotIn("FAIL", result.stdout)
        for said in ("== old: 48 interleavings, 24 leave the PHY reference unbalanced",
                     "ok   old: the next sleep's resume never runs the PHY power-on before its calibrations, "
                     "and all 15 fail",
                     "== fixed: 48 interleavings, 0 leave the PHY reference unbalanced",
                     "ok   fixed: after every interleaving the sleep's first calibration follows a PHY power-on "
                     "and passes",
                     "ok   old: removal leaves power_count -1", "ok   fixed: removal leaves power_count 0"):
            self.assertIn(said, result.stdout)


if __name__ == "__main__":
    unittest.main()
