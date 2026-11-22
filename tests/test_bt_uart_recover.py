"""Exercise the SM-X710 Bluetooth bring-up safety net offline.

ubuntu-gts9u-bt-uart-recover runs against a simulated sysfs, kernel log and
hciconfig.  The kernel's side of a rebind happens in the stubbed sleep: an
unbind written to the driver removes the binding, the UART's IRQ action and
hci0; a bind restores them with a new probe report in the log and a new hci0
carrying the NVM's null address, which "udev" (the address unit) replaces one
tick later.  The default log is boot 0824d1f3 of 2026-11-08, the incident.

The units, the udev rule, the system-sleep hook and postinst are checked as
text, and with systemd-analyze and udevadm where the host has them.  Nothing
on the host is touched.
"""
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
PKG = REPO / "packaging/ubuntu-gts9u-device"
HELPER = PKG / "usr/libexec/ubuntu-gts9u-bt-uart-recover"
UNITS = PKG / "usr/lib/systemd/system"
RECOVER_UNIT = "ubuntu-gts9u-bt-uart-recover.service"
ADDRESS_UNIT = "ubuntu-gts9u-bluetooth-address.service"
RULE = PKG / "usr/lib/udev/rules.d/70-gts9u-bluetooth-address.rules"
HOOK = PKG / "usr/lib/systemd/system-sleep/ubuntu-gts9u-bt-uart-recover"

BOOT = "0824d1f3-6a1e-4c8e-9a51-3f0d2b7c9e10"
NATIVE = "AA:BB:CC:00:11:22"
NULL = "00:00:00:00:5A:AD"
PROBE = ("898000.serial: ttyHS1 at MMIO 0x898000 (irq = 191, base_baud = 0)"
         " is a MSM")
INCIDENT = f"""[    3.801080] {PROBE}
[    3.811944] serial serial0: tty port ttyHS1 registered
[   21.004512] Bluetooth: hci0: setting up wcn6855
[   24.812001] irq 191: nobody cared (try booting with the "irqpoll" option)
[   24.812090] [<000000007b1001b1>] qcom_geni_serial_isr
[   24.812104] Disabling IRQ #191
[   35.001233] Bluetooth: hci0: command 0xfc00 tx timeout
"""
HEALTHY = f"""[    3.801080] {PROBE}
[    3.811944] serial serial0: tty port ttyHS1 registered
[   12.000000] Disabling IRQ #1912
[   13.000000] Disabling IRQ #19
[   21.004512] Bluetooth: hci0: setting up wcn6855
"""

PRELUDE = r"""
T='@ROOT@'
NATIVE='@NATIVE@'
NULL='@NULL@'
note() { echo "$*" >> "$T/calls"; }
dmesg() { [ -e "$T/dmesg" ] && cat "$T/dmesg"; }
hciconfig() {
	if [ ! -d "$T/class/hci0" ]; then
		echo "Can't get device info: No such device" >&2
		return 1
	fi
	if [ "${2:-}" = version ]; then
		note version
		v=$(cat "$T/hci/version")
		case "$v" in
		ok) printf 'hci0:\tType: Primary  Bus: UART\n\tHCI Version: 5.2 (0xb)\n'; return 0 ;;
		timeout) echo "Can't read version info hci0: Connection timed out (110)" >&2; return 1 ;;
		*) echo "$v" >&2; return 1 ;;
		esac
	fi
	printf 'hci0:\tType: Primary  Bus: UART\n\tBD Address: %s  ACL MTU: 1024:8  SCO MTU: 240:4\n\t%s \n\tRX bytes:1 acl:0 sco:0 events:1 errors:0\n\n' \
		"$(cat "$T/hci/addr")" "$(cat "$T/hci/flags")"
}
timeout() { shift; "$@"; }
systemctl() {
	note "systemctl $*"
	if [ "$*" = "start --no-block ubuntu-gts9u-bluetooth-address.service" ] &&
		[ -d "$T/class/hci0" ]; then
		echo "$NATIVE" > "$T/hci/addr"
		echo 'UP RUNNING' > "$T/hci/flags"
	fi
}
sleep() { note "sleep $*"; kernel_tick; }
sync() { :; }
id() { echo 0; }
kernel_tick() {
	drv="$T/drivers/qcom_geni_serial"
	if [ -s "$drv/unbind" ]; then
		[ -e "$T/varlib/bt-uart-recover.pending" ] && note "pending at unbind"
		: > "$drv/unbind"
		rm -f "$T/platform/898000.serial/driver"
		: > "$T/irq/191/actions"
		rm -rf "$T/class/hci0"
		note unbound
	fi
	if [ -s "$drv/bind" ]; then
		: > "$drv/bind"
		note "bind written"
		if [ "$(cat "$T/sim/bind")" = ok ]; then
			ln -s "$drv" "$T/platform/898000.serial/driver"
			echo qcom_geni_serial_uart1 > "$T/irq/191/actions"
			echo "[   40.100000] @PROBE@" >> "$T/dmesg"
			mkdir -p "$T/class/hci0"
			echo "$NULL" > "$T/hci/addr"
			echo DOWN > "$T/hci/flags"
			cat "$T/sim/version_after" > "$T/hci/version"
			note bound
			return
		fi
	fi
	# udev starts the address unit for the new hci0.
	if [ -e "$T/sim/udev" ] && [ -d "$T/class/hci0" ] &&
		[ "$(cat "$T/hci/addr")" = "$NULL" ]; then
		echo "$NATIVE" > "$T/hci/addr"
		echo 'UP RUNNING' > "$T/hci/flags"
		note "udev applied the address"
	fi
}
"""


class Tree:
    def __init__(self, root: pathlib.Path):
        self.root = root
        drv = root / "drivers" / "qcom_geni_serial"
        drv.mkdir(parents=True)
        (drv / "bind").write_text("")
        (drv / "unbind").write_text("")
        uart = root / "platform" / "898000.serial"
        uart.mkdir(parents=True)
        (uart / "driver").symlink_to(drv)
        for irq, action in (("11", "arch_timer"), ("191", "qcom_geni_serial_uart1"),
                            ("212", "qcom_geni_serial_console0")):
            (root / "irq" / irq).mkdir(parents=True)
            (root / "irq" / irq / "actions").write_text(action + "\n")
        (root / "proc_irq" / "191").mkdir(parents=True)
        (root / "proc_irq" / "191" / "spurious").write_text(
            "count 74\nunhandled 0\nlast_unhandled 24812 ms\n")
        (root / "class" / "hci0").mkdir(parents=True)
        (root / "hci").mkdir()
        self.hci(NATIVE, "UP RUNNING PSCAN", "ok")
        (root / "sim").mkdir()
        (root / "sim" / "bind").write_text("ok\n")
        (root / "sim" / "version_after").write_text("ok\n")
        (root / "sim" / "udev").write_text("")
        (root / "compatible").write_bytes(b"samsung,gts9wifi\0qcom,sm8550\0")
        (root / "stats").mkdir()
        (root / "stats" / "success").write_text("1\n")
        (root / "stats" / "fail").write_text("0\n")
        (root / "kmsg").write_text("")
        (root / "calls").write_text("")
        (root / "dmesg").write_text(INCIDENT)
        (root / "boot_id").write_text(BOOT + "\n")
        (root / "uptime").write_text("33.40 100.00\n")

    def hci(self, addr, flags, version):
        (self.root / "hci" / "addr").write_text(addr + "\n")
        (self.root / "hci" / "flags").write_text(flags + "\n")
        (self.root / "hci" / "version").write_text(version + "\n")

    def calls(self):
        return (self.root / "calls").read_text().splitlines()

    def kmsg(self):
        return (self.root / "kmsg").read_text()

    def written(self, attr):
        return (self.root / "drivers" / "qcom_geni_serial" / attr).read_text()

    @property
    def pending(self):
        return self.root / "varlib" / "bt-uart-recover.pending"

    def state(self):
        f = self.root / "run" / "bt-uart-recover"
        if not f.exists():
            return {}
        return dict(line.split("=", 1) for line in f.read_text().splitlines())


class Helper(unittest.TestCase):
    def make(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Tree(pathlib.Path(directory.name))

    def run_helper(self, tree, mode="run", prelude=None):
        root = tree.root
        script = HELPER.read_text()
        paths = {
            "PLATFORM_DEVICES": root / "platform",
            "DRIVER": root / "drivers" / "qcom_geni_serial",
            "IRQ_SYSFS": root / "irq",
            "PROC_IRQ": root / "proc_irq",
            "BLUETOOTH_CLASS": root / "class",
            "DT_COMPATIBLE": root / "compatible",
            "SUSPEND_STATS": root / "stats",
            "KMSG": root / "kmsg",
            "BOOT_ID": root / "boot_id",
            "UPTIME": root / "uptime",
            "STATE": root / "run" / "bt-uart-recover",
            "PENDING": tree.pending,
            "OFF_FILE": root / "bt-uart-recover-off",
        }
        for var, path in paths.items():
            script, n = re.subn(rf"^{var}=.*$", f"{var}='{path}'", script, flags=re.M)
            self.assertEqual(n, 1, var)
        if prelude is None:
            prelude = (PRELUDE.replace("@ROOT@", str(root)).replace("@NATIVE@", NATIVE)
                       .replace("@NULL@", NULL).replace("@PROBE@", PROBE))
        return subprocess.run(["/bin/sh", "-s"], input=prelude + f"set -- {mode}\n" + script,
                              text=True, capture_output=True, timeout=30)

    def run_ok(self, tree, mode="run", rc=0):
        r = self.run_helper(tree, mode)
        self.assertEqual(r.returncode, rc, r.stdout + r.stderr)
        return r

    def assert_untouched(self, tree):
        self.assertEqual(tree.written("unbind"), "")
        self.assertEqual(tree.written("bind"), "")
        self.assertNotIn("unbound", tree.calls())

    # --- detection ----------------------------------------------------------

    def test_healthy_boot_changes_nothing(self):
        tree = self.make()
        (tree.root / "dmesg").write_text(HEALTHY)
        r = self.run_ok(tree)
        self.assertIn("healthy: IRQ 191 log clear; hci0 answers (suspend_stats 1/0)", r.stdout)
        self.assert_untouched(tree)
        self.assertEqual(tree.calls().count("version"), 1)
        st = tree.state()
        self.assertEqual((st["result"], st["rebinds"], st["irq"], st["address"]),
                         ("healthy", "0", "191", NATIVE))
        self.assertEqual(tree.kmsg(), "")

    def test_disable_before_the_last_probe_is_history(self):
        tree = self.make()
        (tree.root / "dmesg").write_text(INCIDENT + f"[   40.100000] {PROBE}\n")
        self.run_ok(tree)
        self.assert_untouched(tree)
        self.assertEqual(tree.state()["result"], "healthy")

    def test_down_or_absent_controller_is_not_asked(self):
        for case in ("down", "absent"):
            with self.subTest(case):
                tree = self.make()
                (tree.root / "dmesg").write_text(HEALTHY)
                if case == "down":
                    tree.hci(NATIVE, "DOWN", "timeout")
                else:
                    (tree.root / "class" / "hci0").rmdir()
                r = self.run_ok(tree)
                self.assertIn("healthy", r.stdout)
                self.assertNotIn("version", tree.calls())
                self.assert_untouched(tree)

    def test_other_hci_errors_are_not_a_fault(self):
        tree = self.make()
        (tree.root / "dmesg").write_text(HEALTHY)
        tree.hci(NATIVE, "UP RUNNING", "Can't read version info hci0: Operation not permitted (1)")
        r = self.run_ok(tree)
        self.assertIn("Operation not permitted", r.stdout)
        self.assertEqual(tree.calls().count("version"), 1)
        self.assert_untouched(tree)

    def test_unreadable_log_leaves_the_probe(self):
        tree = self.make()
        (tree.root / "dmesg").unlink()
        r = self.run_ok(tree)
        self.assertIn("log unreadable; hci0 answers", r.stdout)
        self.assert_untouched(tree)

    # --- recovery -----------------------------------------------------------

    def test_incident_rebinds_once_and_the_address_comes_back(self):
        tree = self.make()
        tree.hci(NATIVE, "UP RUNNING", "timeout")
        r = self.run_ok(tree)
        self.assertIn("FAULT: the kernel disabled IRQ 191 (qcom_geni_serial_uart1)"
                      " after the UART's last probe (suspend_stats 1/0)", r.stdout)
        calls = tree.calls()
        unbound = calls.index("unbound")
        # A disabled IRQ is proof enough: no HCI command to a dead UART.
        self.assertNotIn("version", calls[:unbound])
        # The loop breaker is armed for exactly the unbind and bind.
        self.assertEqual(calls[unbound - 1], "pending at unbind")
        self.assertFalse(tree.pending.exists())
        self.assertEqual(calls.count("unbound"), 1)
        self.assertEqual(calls.count("bind written"), 1)
        self.assertLess(unbound, calls.index("bound"))
        self.assertLess(calls.index("bound"), calls.index("udev applied the address"))
        # The address came from udev's start of the address unit, not from
        # this helper, which then checked that the controller answers.
        self.assertFalse([c for c in calls if c.startswith("systemctl")])
        self.assertEqual(calls[-1], "version")
        st = tree.state()
        self.assertEqual((st["result"], st["rebinds"], st["address"]),
                         ("recovered", "1", NATIVE))
        self.assertTrue(st["reason"].startswith(
            "the kernel disabled IRQ 191 (qcom_geni_serial_uart1) after the UART's last probe;"
            " IRQ 191, hci0 answers"), st["reason"])
        kmsg = tree.kmsg()
        self.assertIn("rebinding 898000.serial: the kernel disabled IRQ 191", kmsg)
        self.assertIn(f"recovered: IRQ 191, hci0 answers, hci0 at {NATIVE}", kmsg)
        # Its own log lines never read as a new disable.
        self.assertNotRegex(kmsg, r"Disabling IRQ #191$")

    def test_a_silent_controller_is_rebound_after_three_timeouts(self):
        tree = self.make()
        (tree.root / "dmesg").write_text(HEALTHY)
        tree.hci(NATIVE, "UP RUNNING", "timeout")
        r = self.run_ok(tree)
        self.assertIn("FAULT: hci0 is up but 3 Read Local Version commands timed out", r.stdout)
        calls = tree.calls()
        before = calls[:calls.index("unbound")]
        self.assertEqual(before.count("version"), 3)
        self.assertEqual(before.count("sleep 5"), 2)
        self.assertEqual(tree.state()["result"], "recovered")

    def test_an_unknown_address_before_accepts_the_native_one(self):
        tree = self.make()
        tree.hci(NULL, "DOWN", "timeout")
        self.run_ok(tree)
        st = tree.state()
        self.assertEqual((st["result"], st["address"]), ("recovered", NATIVE))

    def test_a_missed_udev_start_falls_back_to_the_address_unit(self):
        tree = self.make()
        (tree.root / "sim" / "udev").unlink()
        r = self.run_ok(tree)
        self.assertIn(f"hci0 still reads {NULL} after 45 s; starting {ADDRESS_UNIT}", r.stdout)
        self.assertIn(f"systemctl start --no-block {ADDRESS_UNIT}", tree.calls())
        self.assertEqual(tree.state()["result"], "recovered")

    def test_a_rebind_that_does_not_help_is_reported_not_repeated(self):
        tree = self.make()
        (tree.root / "sim" / "version_after").write_text("timeout\n")
        r = self.run_ok(tree, rc=1)
        self.assertIn("failed: still faulty after the rebind: hci0 is up but 3 Read Local"
                      " Version commands timed out", r.stdout)
        self.assertEqual(tree.calls().count("unbound"), 1)
        st = tree.state()
        self.assertEqual((st["result"], st["rebinds"]), ("failed", "1"))
        self.assertIn("failed: still faulty", tree.kmsg())

    def test_a_failed_bind_is_asked_once_more_and_reported(self):
        tree = self.make()
        (tree.root / "sim" / "bind").write_text("fail\n")
        r = self.run_ok(tree, rc=1)
        self.assertEqual(tree.calls().count("bind written"), 2)
        self.assertIn("898000.serial did not bind again (bind it with: echo 898000.serial >", r.stdout)
        self.assertFalse(tree.pending.exists())
        self.assertEqual(tree.state()["result"], "failed")

    # --- bounds -------------------------------------------------------------

    def test_opt_out_logs_the_fault_and_leaves_it(self):
        tree = self.make()
        (tree.root / "bt-uart-recover-off").write_text("")
        r = self.run_ok(tree)
        self.assertIn("FAULT:", r.stdout)
        self.assertIn("HOLD: ", r.stdout)
        self.assert_untouched(tree)
        self.assertEqual(tree.state()["result"], "held")
        self.assertIn("fault left alone:", tree.kmsg())

    def test_rebinds_are_capped_per_boot(self):
        tree = self.make()
        (tree.root / "run").mkdir()
        (tree.root / "run" / "bt-uart-recover").write_text(
            f"boot_id={BOOT}\nresult=recovered\nrebinds=3\nbreaker=\n")
        r = self.run_ok(tree)
        self.assertIn("HOLD: 3 rebinds this boot already (limit 3)", r.stdout)
        self.assert_untouched(tree)
        self.assertEqual(tree.state()["rebinds"], "3")

    def test_the_cap_starts_again_each_boot(self):
        tree = self.make()
        (tree.root / "run").mkdir()
        (tree.root / "run" / "bt-uart-recover").write_text(
            "boot_id=aaaaaaaa-1111\nresult=recovered\nrebinds=3\nbreaker=\n")
        self.run_ok(tree)
        self.assertEqual(tree.state()["rebinds"], "1")

    def test_a_marker_from_another_boot_holds_this_boot_only(self):
        tree = self.make()
        tree.pending.parent.mkdir()
        tree.pending.write_text("boot_id=aaaaaaaa-1111\nuptime=33.10\n")
        r = self.run_ok(tree)
        self.assertIn("HOLD: loop breaker: boot aaaaaaaa began a rebind at 33.10 s"
                      " and never finished it", r.stdout)
        self.assert_untouched(tree)
        self.assertFalse(tree.pending.exists())
        self.assertIn("loop breaker: boot aaaaaaaa", tree.kmsg())
        # Still held on this boot, after a resume for example...
        r = self.run_ok(tree)
        self.assertIn("HOLD: loop breaker", r.stdout)
        self.assert_untouched(tree)
        # ...and the next boot recovers again.
        (tree.root / "boot_id").write_text("bbbbbbbb-2222\n")
        self.run_ok(tree)
        self.assertEqual(tree.state()["result"], "recovered")

    def test_a_marker_from_this_boot_does_not_hold(self):
        tree = self.make()
        tree.pending.parent.mkdir()
        tree.pending.write_text(f"boot_id={BOOT}\nuptime=33.10\n")
        self.run_ok(tree)
        self.assertEqual(tree.state()["result"], "recovered")
        self.assertFalse(tree.pending.exists())

    def test_an_unbound_uart_is_left_alone(self):
        tree = self.make()
        (tree.root / "platform" / "898000.serial" / "driver").unlink()
        r = self.run_ok(tree)
        self.assertIn("HOLD: 898000.serial has no driver", r.stdout)
        self.assert_untouched(tree)
        self.assertEqual(tree.state()["result"], "held")

    def test_other_board_does_nothing(self):
        tree = self.make()
        (tree.root / "compatible").write_bytes(b"samsung,gts9uwifi\0qcom,sm8550\0")
        r = self.run_ok(tree)
        self.assertIn("not samsung,gts9wifi", r.stdout)
        self.assert_untouched(tree)
        self.assertEqual(tree.state(), {})

    # --- read-only modes ----------------------------------------------------

    def test_check_says_what_run_would_do_and_writes_nothing(self):
        tree = self.make()
        tree.pending.parent.mkdir()
        r = self.run_ok(tree, "check")
        self.assertIn("would rebind 898000.serial on qcom_geni_serial", r.stdout)
        self.assert_untouched(tree)
        self.assertEqual(tree.state(), {})
        self.assertEqual(tree.kmsg(), "")
        self.assertFalse(tree.pending.exists())

    def test_status_reads_only(self):
        tree = self.make()
        r = self.run_ok(tree, "status")
        self.assertIn("898000.serial: bound to qcom_geni_serial", r.stdout)
        self.assertIn("IRQ 191 (qcom_geni_serial_uart1): count 74 unhandled 0", r.stdout)
        self.assertIn("kernel log since the UART's last probe: disabled", r.stdout)
        self.assertIn(f"hci0: UP RUNNING PSCAN, address {NATIVE}", r.stdout)
        self.assert_untouched(tree)
        self.assertEqual(tree.state(), {})

    def test_run_needs_root(self):
        tree = self.make()
        r = self.run_helper(tree, prelude="id() { echo 1000; }\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("run needs root", r.stderr)
        self.assert_untouched(tree)


def directives(text):
    """(key, value) pairs in order; systemd allows repeated keys."""
    pairs = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        key, _, value = line.partition("=")
        pairs.append((key, value))
    return pairs


class Units(unittest.TestCase):
    def values(self, unit, key):
        return [v for k, v in directives((UNITS / unit).read_text()) if k == key]

    def test_recover_unit(self):
        v = lambda key: self.values(RECOVER_UNIT, key)
        self.assertEqual(v("ConditionFirmware"), ["device-tree-compatible(samsung,gts9wifi)"])
        self.assertEqual(v("ConditionPathExists"), ["/sys/bus/platform/devices/898000.serial"])
        after = " ".join(v("After")).split()
        for unit in ("ubuntu-gts9u-panel-coldboot-recover.service", "bluetooth.service",
                     ADDRESS_UNIT):
            self.assertIn(unit, after)
        # Ordering only, like ubuntu-gts9u-icc-late-release.
        self.assertEqual(v("Wants") + v("Requires") + v("BindsTo"), [])
        self.assertEqual(v("Before"), ["sleep.target"])
        self.assertEqual(v("Type"), ["oneshot"])
        # Every start must run the check again.
        self.assertEqual(v("RemainAfterExit"), [])
        self.assertEqual(v("ExecStart"), ["/usr/libexec/ubuntu-gts9u-bt-uart-recover run"])
        self.assertEqual(v("WantedBy"), ["multi-user.target"])

    def test_address_unit_runs_again_for_every_hci0(self):
        v = lambda key: self.values(ADDRESS_UNIT, key)
        self.assertEqual(v("Type"), ["oneshot"])
        self.assertEqual(v("RemainAfterExit"), [])
        # udev is its only trigger: multi-user.target would be a second run.
        self.assertEqual(v("WantedBy") + v("RequiredBy"), [])
        self.assertNotRegex((UNITS / ADDRESS_UNIT).read_text(), r"(?m)^\[Install\]")
        # The measured btmgmt rule: only once bluetoothd is up.
        self.assertEqual(v("After"), ["bluetooth.service"])
        self.assertEqual(v("Wants"), ["bluetooth.service"])
        self.assertEqual(v("ExecStart"), ["/usr/libexec/ubuntu-gts9u-bluetooth-address"])

    def test_the_address_helper_keeps_its_btmgmt_rules(self):
        helper = (PKG / "usr/libexec/ubuntu-gts9u-bluetooth-address").read_text()
        self.assertIn("printf '' | timeout 10 btmgmt", helper)
        calls = [line for line in helper.splitlines()
                 if re.search(r"(^|[|;&]\s*)btmgmt\s", line.split("#")[0])]
        self.assertEqual(calls, [], "btmgmt called without btmgmt_")

    @unittest.skipUnless(shutil.which("systemd-analyze"), "no systemd-analyze")
    def test_systemd_accepts_them(self):
        # The helpers are not on the host, so point ExecStart at /bin/true;
        # everything else is verified as shipped.
        with tempfile.TemporaryDirectory() as tmp:
            for unit in (RECOVER_UNIT, ADDRESS_UNIT):
                text = re.sub(r"^ExecStart=/usr/libexec/\S+", "ExecStart=/bin/true",
                              (UNITS / unit).read_text(), flags=re.M)
                copy = pathlib.Path(tmp) / unit
                copy.write_text(text)
                r = subprocess.run(["systemd-analyze", "verify", "--man=no", str(copy)],
                                   capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertNotIn(unit, r.stderr)


class UdevRule(unittest.TestCase):
    def test_every_hci0_add_starts_the_address_unit(self):
        rules = [line for line in RULE.read_text().splitlines()
                 if line.strip() and not line.startswith("#")]
        self.assertEqual(len(rules), 1)
        keys = [k.strip() for k in rules[0].split(",")]
        for key in ('ACTION=="add"', 'SUBSYSTEM=="bluetooth"', 'KERNEL=="hci0"',
                    'TAG+="systemd"', f'ENV{{SYSTEMD_WANTS}}+="{ADDRESS_UNIT}"'):
            self.assertIn(key, keys)

    @unittest.skipUnless(shutil.which("udevadm"), "no udevadm")
    def test_udev_accepts_it(self):
        r = subprocess.run(["udevadm", "verify", "--no-style", str(RULE)],
                           capture_output=True, text=True)
        if "Unknown command" in r.stderr or "invalid option" in r.stderr:
            self.skipTest("udevadm without verify")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class SleepHook(unittest.TestCase):
    def run_hook(self, compatible, *args):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "compatible").write_bytes(compatible)
            script, n = re.subn(r"^DT_COMPATIBLE=.*$", f"DT_COMPATIBLE='{root}/compatible'",
                                HOOK.read_text(), flags=re.M)
            self.assertEqual(n, 1)
            prelude = f"systemctl() {{ echo \"$*\" >> '{root}/calls'; }}\nset -- {' '.join(args)}\n"
            r = subprocess.run(["/bin/sh", "-s"], input=prelude + script,
                               capture_output=True, text=True, timeout=10)
            self.assertEqual(r.returncode, 0, r.stderr)
            calls = root / "calls"
            return calls.read_text().splitlines() if calls.exists() else []

    def test_starts_the_check_after_every_resume_without_waiting(self):
        self.assertEqual(self.run_hook(b"samsung,gts9wifi\0qcom,sm8550\0", "post", "suspend"),
                         [f"start --no-block {RECOVER_UNIT}"])

    def test_nothing_before_sleep(self):
        self.assertEqual(self.run_hook(b"samsung,gts9wifi\0qcom,sm8550\0", "pre", "suspend"), [])

    def test_nothing_on_other_boards(self):
        self.assertEqual(self.run_hook(b"samsung,gts9uwifi\0qcom,sm8550\0", "post", "suspend"), [])


class PackageScripts(unittest.TestCase):
    def setUp(self):
        self.postinst = (PKG / "DEBIAN/postinst").read_text()
        self.units = self.postinst.split("UNITS='", 1)[1].split("'", 1)[0].split()

    def test_postinst_enables_the_recover_unit(self):
        self.assertIn(f"{RECOVER_UNIT}:multi-user.target", self.units)

    def test_postinst_retires_the_address_units_boot_link(self):
        self.assertFalse([u for u in self.units if u.startswith(ADDRESS_UNIT)])
        self.assertIn(f"deb-systemd-helper purge {ADDRESS_UNIT}", self.postinst)
        self.assertIn(f"rm -f /etc/systemd/system/multi-user.target.wants/{ADDRESS_UNIT}\n",
                      self.postinst)

    def test_postinst_resets_only_a_finished_address_unit(self):
        self.assertIn(f'if [ "$(systemctl is-active {ADDRESS_UNIT} 2>/dev/null)" = active ]; then\n'
                      f"\t\t\t\tsystemctl stop {ADDRESS_UNIT} || true\n", self.postinst)

    def test_postinst_reloads_udev(self):
        self.assertIn("udevadm control --reload", self.postinst)


if __name__ == "__main__":
    unittest.main()
