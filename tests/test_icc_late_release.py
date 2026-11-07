"""Exercise ubuntu-gts9u-icc-late-release against a simulated sysfs tree.

The tree mirrors what the SM-X710 shows on Stage 2 (2026-11-07): gem_noc and
mc_virt unsynced, each held back only by an unbound bwmon PMU, every other
interconnect provider synced, and rpmhpd/gcc as non-interconnect providers
that must never be written whatever they read.
"""
import pathlib
import re
import subprocess
import tempfile
import unittest


HELPER = pathlib.Path(__file__).resolve().parents[1] / (
    "packaging/ubuntu-gts9u-device/usr/libexec/ubuntu-gts9u-icc-late-release"
)

COLDBOOT_OK = """LoadState=loaded
ActiveState=active
Result=success
ConditionResult=yes
ConditionTimestampMonotonic=18859269
ExecMainExitTimestampMonotonic=31910936
"""


class Tree:
    def __init__(self, root: pathlib.Path):
        self.root = root
        self.platform = root / "platform"
        self.links = root / "devlinks"
        self.platform.mkdir()
        self.links.mkdir()
        self.nlinks = 0
        (root / "compatible").write_bytes(b"samsung,gts9wifi\0qcom,sm8550\0")
        (root / "pm_test").write_text("[none] core processors platform devices freezer\n")
        (root / "stats").mkdir()
        (root / "stats" / "success").write_text("1\n")
        (root / "stats" / "fail").write_text("0\n")
        (root / "kmsg").write_text("")
        (root / "boot_id").write_text("df92b567-453c-4cfe-a950-578d4d595476\n")
        (root / "uptime").write_text("33.95 100.00\n")
        (root / "icc_summary").write_text(
            "ebi@interconnect-1 2147483647 2147483647\n"
            "qns_llcc@24100000.interconnect 2147483647 2147483647\n")
        (root / "systemctl.out").write_text(COLDBOOT_OK)
        (root / "drivers" / "bound").mkdir(parents=True)

    def device(self, name, synced=None, icc=False, compatible=None, bound=False):
        d = self.platform / name
        (d / "of_node").mkdir(parents=True)
        if synced is not None:
            (d / "state_synced").write_text(f"{synced}\n")
        if icc:
            (d / "of_node" / "#interconnect-cells").write_bytes(b"\0\0\0\2")
        if compatible:
            (d / "of_node" / "compatible").write_bytes(
                b"".join(c.encode() + b"\0" for c in compatible))
        if bound:
            (d / "driver").symlink_to(self.root / "drivers" / "bound")
        return d

    def link(self, supplier, consumer, status):
        self.nlinks += 1
        lnk = self.links / f"link{self.nlinks}"
        lnk.mkdir()
        (lnk / "status").write_text(f"{status}\n")
        (lnk / "consumer").symlink_to(self.platform / consumer)
        (self.platform / supplier / f"consumer:platform:{consumer}").symlink_to(lnk)

    def synced(self, name):
        return (self.platform / name / "state_synced").read_text()


def x710_stage2(tree: Tree):
    tree.device("24100000.interconnect", 0, icc=True)
    tree.device("interconnect-1", 0, icc=True)
    tree.device("1500000.interconnect", 1, icc=True)
    tree.device("17a00000.rsc:power-controller", 0)
    tree.device("100000.clock-controller", 0)
    tree.device("240b6400.pmu", compatible=["qcom,sm8550-cpu-bwmon", "qcom,sdm845-bwmon"])
    tree.device("24091000.pmu", compatible=["qcom,sm8550-llcc-bwmon", "qcom,sc7280-llcc-bwmon"])
    tree.device("1d84000.ufshc", bound=True)
    tree.device("a600000.usb", bound=True)
    for provider in ("24100000.interconnect", "interconnect-1"):
        tree.link(provider, "1d84000.ufshc", "active")
        tree.link(provider, "a600000.usb", "active")
    tree.link("24100000.interconnect", "240b6400.pmu", "available")
    tree.link("interconnect-1", "24091000.pmu", "available")


class IccLateReleaseTest(unittest.TestCase):
    def run_helper(self, tree: Tree, mode="release"):
        script = HELPER.read_text()
        root = tree.root
        paths = {
            "PLATFORM_DEVICES": tree.platform,
            "DT_COMPATIBLE": root / "compatible",
            "ICC_SUMMARY": root / "icc_summary",
            "PM_TEST": root / "pm_test",
            "SUSPEND_STATS": root / "stats",
            "KMSG": root / "kmsg",
            "LOG": root / "log",
            "STATE": root / "run" / "icc-release",
            "BOOT_ID": root / "boot_id",
            "UPTIME": root / "uptime",
            "HOLD_FILE": root / "icc-hold",
        }
        for var, path in paths.items():
            script, n = re.subn(rf"^{var}=.*$", f"{var}='{path}'", script, flags=re.M)
            self.assertEqual(n, 1, var)
        prelude = f"""
systemctl() {{ cat '{root}/systemctl.out'; }}
id() {{ echo 0; }}
sync() {{ :; }}
sleep() {{ :; }}
chgrp() {{ :; }}
set -- {mode}
"""
        result = subprocess.run(["/bin/sh", "-s"], input=prelude + script,
                                text=True, capture_output=True, timeout=20)
        return result

    def state(self, tree: Tree):
        f = tree.root / "run" / "icc-release"
        if not f.exists():
            return {}
        return dict(line.split("=", 1) for line in f.read_text().splitlines())

    def make(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Tree(pathlib.Path(directory.name))

    def test_releases_both_providers_with_printf(self):
        tree = self.make()
        x710_stage2(tree)
        r = self.run_helper(tree)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # Exactly "1": the kernel rejects "1\n" with EINVAL.
        self.assertEqual(tree.synced("24100000.interconnect"), "1")
        self.assertEqual(tree.synced("interconnect-1"), "1")
        self.assertEqual(tree.synced("1500000.interconnect"), "1\n")
        self.assertEqual(tree.synced("17a00000.rsc:power-controller"), "0\n")
        self.assertEqual(tree.synced("100000.clock-controller"), "0\n")
        st = self.state(tree)
        self.assertEqual(st["result"], "released")
        self.assertEqual(st["released"], "24100000.interconnect interconnect-1")
        self.assertEqual(st["intmax_before"], "2")
        kmsg = (tree.root / "kmsg").read_text()
        self.assertIn("releasing 24100000.interconnect interconnect-1", kmsg)
        self.assertIn("released 24100000.interconnect interconnect-1", kmsg)
        self.assertIn("read back: 24100000.interconnect=1 interconnect-1=1",
                      (tree.root / "log").read_text())

    def test_check_writes_nothing(self):
        tree = self.make()
        x710_stage2(tree)
        r = self.run_helper(tree, "check")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("would release: 24100000.interconnect interconnect-1", r.stdout)
        self.assertEqual(tree.synced("24100000.interconnect"), "0\n")
        self.assertEqual(tree.synced("interconnect-1"), "0\n")
        self.assertFalse((tree.root / "log").exists())
        self.assertEqual(self.state(tree), {})
        self.assertEqual((tree.root / "kmsg").read_text(), "")

    def test_status_reads_only(self):
        tree = self.make()
        x710_stage2(tree)
        r = self.run_helper(tree, "status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("24100000.interconnect: state_synced=0; unbound bwmon: 240b6400.pmu", r.stdout)
        self.assertEqual(tree.synced("interconnect-1"), "0\n")

    def assert_held(self, tree, reason_fragment):
        r = self.run_helper(tree)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(tree.synced("24100000.interconnect"), "0\n")
        self.assertEqual(tree.synced("interconnect-1"), "0\n")
        st = self.state(tree)
        self.assertEqual(st.get("result"), "held", r.stdout)
        self.assertIn(reason_fragment, st["reason"])
        self.assertIn("HOLD", r.stdout)

    def test_foreign_blocker_holds_everything(self):
        tree = self.make()
        x710_stage2(tree)
        tree.device("ae00000.display-subsystem")
        tree.link("interconnect-1", "ae00000.display-subsystem", "available")
        self.assert_held(tree, "ae00000.display-subsystem(available)")

    def test_bound_bwmon_is_not_an_allowed_blocker(self):
        tree = self.make()
        x710_stage2(tree)
        (tree.platform / "240b6400.pmu" / "driver").symlink_to(tree.root / "drivers" / "bound")
        self.assert_held(tree, "240b6400.pmu(available)")

    def test_failed_cycle_holds(self):
        tree = self.make()
        x710_stage2(tree)
        (tree.root / "systemctl.out").write_text(COLDBOOT_OK.replace(
            "ActiveState=active", "ActiveState=failed").replace(
            "Result=success", "Result=exit-code"))
        self.assert_held(tree, "did not succeed")

    def test_masked_cycle_holds(self):
        tree = self.make()
        x710_stage2(tree)
        (tree.root / "systemctl.out").write_text(
            "LoadState=masked\nActiveState=inactive\nResult=success\n"
            "ConditionResult=no\nConditionTimestampMonotonic=0\n"
            "ExecMainExitTimestampMonotonic=0\n")
        self.assert_held(tree, "masked")

    def test_cycle_that_never_ran_holds(self):
        tree = self.make()
        x710_stage2(tree)
        (tree.root / "systemctl.out").write_text(
            "LoadState=loaded\nActiveState=inactive\nResult=success\n"
            "ConditionResult=no\nConditionTimestampMonotonic=0\n"
            "ExecMainExitTimestampMonotonic=0\n")
        self.assert_held(tree, "has not run this boot")

    def test_cycle_impossible_on_this_kernel_releases(self):
        tree = self.make()
        x710_stage2(tree)
        (tree.root / "systemctl.out").write_text(
            "LoadState=loaded\nActiveState=inactive\nResult=success\n"
            "ConditionResult=no\nConditionTimestampMonotonic=123\n"
            "ExecMainExitTimestampMonotonic=0\n")
        r = self.run_helper(tree)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(tree.synced("interconnect-1"), "1")

    def test_pm_test_selected_holds(self):
        tree = self.make()
        x710_stage2(tree)
        (tree.root / "pm_test").write_text("none core processors [platform] devices freezer\n")
        self.assert_held(tree, "pm_test")

    def test_hold_file_holds(self):
        tree = self.make()
        x710_stage2(tree)
        (tree.root / "icc-hold").write_text("")
        self.assert_held(tree, "icc-hold")

    def test_other_board_does_nothing(self):
        tree = self.make()
        x710_stage2(tree)
        (tree.root / "compatible").write_bytes(b"samsung,gts9uwifi\0qcom,sm8550\0")
        r = self.run_helper(tree)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(tree.synced("24100000.interconnect"), "0\n")
        self.assertEqual(self.state(tree)["result"], "not-board")

    def test_all_synced_is_a_no_op(self):
        tree = self.make()
        x710_stage2(tree)
        for name in ("24100000.interconnect", "interconnect-1"):
            (tree.platform / name / "state_synced").write_text("1\n")
        r = self.run_helper(tree)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.state(tree)["result"], "nothing")
        self.assertEqual(tree.synced("24100000.interconnect"), "1\n")

    def test_unsynced_without_unbound_consumer_holds(self):
        tree = self.make()
        x710_stage2(tree)
        tree.device("1700000.interconnect", 0, icc=True)
        tree.link("1700000.interconnect", "1d84000.ufshc", "active")
        self.assert_held(tree, "1700000.interconnect reads 0 with no unbound consumer")

    def test_release_needs_root(self):
        tree = self.make()
        x710_stage2(tree)
        script = HELPER.read_text()
        result = subprocess.run(["/bin/sh", "-s"], input="id() { echo 1000; }\nset -- release\n" + script,
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 1)
        self.assertIn("needs root", result.stderr)


if __name__ == "__main__":
    unittest.main()
