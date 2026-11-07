"""Run the suspend reliability runner (scripts/diagnostics/suspend/
gts9u-suspend-cycles.sh) and the ADSP-start sleep lock helper against a fake
root.  sysfs, procfs, /run, /var/log, systemd-sleep, rtcwake, systemctl,
dmesg and nmcli are all simulated; nothing on the host sleeps or is touched."""
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
RUNNER = REPO / "scripts/diagnostics/suspend/gts9u-suspend-cycles.sh"
LADDER = REPO / "scripts/diagnostics/suspend/x710-suspend-ladder.sh"
PACKAGE = REPO / "packaging/ubuntu-gts9u-device"
INHIBIT = PACKAGE / "usr/libexec/ubuntu-gts9u-adsp-start-inhibit"
INHIBIT_UNIT = PACKAGE / "usr/lib/systemd/system/ubuntu-gts9u-adsp-start-inhibit.service"
POSTINST = PACKAGE / "DEBIAN/postinst"

GTS9WIFI = b"qcom,kalama-mtp\0qcom,kalama\0qcom,mtp\0samsung,gts9wifi\0qcom,sm8550\0"
GTS9UWIFI = b"qcom,kalama-mtp\0qcom,kalama\0qcom,mtp\0samsung,gts9uwifi\0qcom,sm8550\0"

PREFIXES = ("/sys/", "/proc/", "/run/", "/var/log/", "/dev/kmsg", "/dev/fastrpc-adsp",
            "/usr/lib/systemd/systemd-sleep", "/etc/systemd/sleep.conf.d")

INTERRUPTS = """\
           CPU0       CPU1       CPU2       CPU3       CPU4       CPU5       CPU6       CPU7
 13:         43          0          0          0          0          0          0          0     GICv3 261 Level     ipcc_0
 21:          0          0          0          0          0          0          0          0  pmic_arb 6431283 Edge      pm8xxx_rtc_alarm
 22:         18          0          0          0          0          0          0          0  pmic_arb 1303088 Edge      pmic_pwrkey
 23:          0          0          0          0          0          0          0          0  pmic_arb 1294896 Edge      pmic_resin
 34:       1133          0          0          0          0          0          0          0  pmic_arb 9445945 Edge      sdam0
171:       4547          0          0          0          0          0          0          0  smp2p-adsp   1 Edge      q6v5 ready
"""

# Bump IRQ $1 by one in proc/interrupts.
BUMP = """bump() {
	awk -v q="$1:" '$1 == q {$2 = $2 + 1} {print}' "$R/proc/interrupts" > "$R/proc/interrupts.new"
	mv "$R/proc/interrupts.new" "$R/proc/interrupts"
}
"""

# systemd-sleep: one scripted suspend per call.  $R/scenario/N (N = call
# number) may hold shell assignments: rc, wake (IRQ), extra (kernel lines),
# entered (mode the kernel reports), panel, okinc, fail.
STUB_SLEEP = """#!/bin/sh
R=$FAKE_ROOT
""" + BUMP + """
n=$(( $(cat "$R/sleep-calls" 2>/dev/null || echo 0) + 1 ))
echo $n > "$R/sleep-calls"
{ echo "--- call $n"; cat "$R/run/systemd/sleep.conf.d/"*.conf; } >> "$R/dropins-seen"
cp "$R/systemctl.log" "$R/systemctl-at-sleep-$n" 2>/dev/null
mode=$(sed -n 's/.*\\[\\([a-z0-9]*\\)\\].*/\\1/p' "$R/sys/power/mem_sleep")
[ -n "$mode" ] || mode=$(cat "$R/sys/power/mem_sleep")
rc=0 wake=21 extra= entered=$mode panel='80 00 04' okinc=1 fail=0
[ -f "$R/scenario/$n" ] && . "$R/scenario/$n"
{
	echo "[  100.0] PM: suspend entry ($entered)"
	[ -n "$extra" ] && echo "[  100.1] $extra"
	echo "[  130.0] PM: Timekeeping suspended for 30.012 seconds"
	echo "[  130.5] PM: resume of devices complete after 450.125 msecs"
	echo "[  130.6] panel-samsung-ana38407 ae94000.dsi.0: ana38407 panel id: $panel"
	echo "[  130.7] PM: suspend exit"
} >> "$R/dev/kmsg"
printf '%s' "$panel" > "$R/sys/class/backlight/ae94000.dsi.0/panel_id"
if [ "$wake" = none ]; then rm -f "$R/sys/power/pm_wakeup_irq"; else echo "$wake" > "$R/sys/power/pm_wakeup_irq"; bump "$wake"; fi
[ "$wake" = 21 ] && : > "$R/sys/class/rtc/rtc0/wakealarm"
echo $(( $(cat "$R/sys/power/suspend_stats/success") + okinc )) > "$R/sys/power/suspend_stats/success"
echo $(( $(cat "$R/sys/power/suspend_stats/fail") + fail )) > "$R/sys/power/suspend_stats/fail"
for s in aosd cxsd ddr; do
	f="$R/sys/kernel/debug/qcom_stats/$s"
	c=$(sed -n 's/^Count: //p' "$f"); d=$(sed -n 's/^Accumulated Duration: //p' "$f")
	printf 'Count: %s\\nLast Entered At: 0\\nLast Exited At: 0\\nAccumulated Duration: %s\\n' $((c + 1)) $((d + 480000000)) > "$f"
done
exit $rc
"""

STUB_RTCWAKE = """#!/bin/sh
R=$FAKE_ROOT
echo "rtcwake $*" >> "$R/rtcwake.log"
case "$*" in
*"-m no"*) [ -e "$R/rtc-broken" ] || echo 1234567 > "$R/sys/class/rtc/rtc0/wakealarm" ;;
*"-m disable"*) : > "$R/sys/class/rtc/rtc0/wakealarm" ;;
esac
exit 0
"""

# sleep: time passes; a pending RTC alarm fires (unless the RTC is "deaf").
STUB_SLEEPCMD = """#!/bin/sh
R=$FAKE_ROOT
""" + BUMP + """
echo "sleep $*" >> "$R/sleep.log"
if [ -s "$R/sys/class/rtc/rtc0/wakealarm" ] && [ ! -e "$R/rtc-deaf" ]; then
	: > "$R/sys/class/rtc/rtc0/wakealarm"
	bump 21
fi
exit 0
"""

STUB_SYSTEMCTL = """#!/bin/sh
R=$FAKE_ROOT
echo "systemctl $*" >> "$R/systemctl.log"
case "$1" in
show)
	case "$*" in
	*ConflictedBy*) echo; echo hexagonrpcd-adsp-sensorspd.service ;;
	*LoadState*) echo masked ;;
	*ActiveState*) for u; do :; done; cat "$R/units/$u" 2>/dev/null || echo inactive ;;
	esac ;;
stop) echo inactive > "$R/units/$2" ;;
esac
exit 0
"""

STUB_DMESG = """#!/bin/sh
cat "$FAKE_ROOT/dev/kmsg"
"""

STUB_NMCLI = """#!/bin/sh
echo "lo:connected (externally)"
echo "wlp1s0:$(cat "$FAKE_ROOT/wifi" 2>/dev/null || echo connected)"
"""

STUB_FINDMNT = """#!/bin/sh
cat "$FAKE_ROOT/rootopts" 2>/dev/null || echo rw,noatime
"""

TOOLS = ("awk", "sed", "grep", "sort", "head", "tail", "cut", "tr", "wc", "cat", "mkdir",
         "rm", "ln", "chmod", "readlink", "date", "sync", "uname", "cp", "mv", "flock",
         "touch")


class FakeRoot:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        r = self.root
        for d in ("bin", "tools", "units", "scenario", "dev", "var/log", "run/ubuntu-gts9u",
                  "run/systemd/sleep.conf.d", "proc/device-tree", "proc/sys/kernel/random",
                  "sys/power/suspend_stats", "sys/class/rtc/rtc0/device/power",
                  "sys/class/remoteproc/remoteproc0", "sys/class/backlight/ae94000.dsi.0",
                  "sys/kernel/debug/qcom_stats", "sys/kernel/debug/interconnect",
                  "sys/devices/platform/soc@0/24100000.interconnect",
                  "sys/devices/platform/interconnect-1", "usr/lib/systemd",
                  "sys/devices/system/cpu/cpu0/cpuidle/state1/s2idle"):
            (r / d).mkdir(parents=True, exist_ok=True)
        for tool in TOOLS:
            os.symlink(shutil.which(tool), r / "tools" / tool)
        self.write("proc/interrupts", INTERRUPTS)
        self.write("proc/uptime", "600.25 4000.00\n")
        self.write("proc/cmdline", "root=LABEL=UBTS9_UFS initcall_blacklist=bwmon_driver_init\n")
        self.write("proc/sys/kernel/random/boot_id", "11111111-2222\n")
        (r / "proc/device-tree/compatible").write_bytes(GTS9WIFI)
        self.write("sys/power/mem_sleep", "s2idle [deep]\n")
        self.write("sys/power/state", "freeze mem\n")
        self.write("sys/power/pm_test", "[none] core processors platform devices freezer\n")
        self.write("sys/power/pm_debug_messages", "0\n")
        self.write("sys/power/pm_print_times", "0\n")
        for k, v in (("success", "1"), ("fail", "0"), ("last_failed_dev", ""),
                     ("last_failed_step", ""), ("last_failed_errno", "0")):
            self.write("sys/power/suspend_stats/" + k, v + "\n")
        self.write("sys/class/rtc/rtc0/wakealarm", "")
        self.write("sys/class/rtc/rtc0/name", "rtc-pm8xxx c400000.spmi:pmic@0:rtc@6100\n")
        self.write("sys/class/rtc/rtc0/device/power/wakeup", "enabled\n")
        self.write("sys/class/remoteproc/remoteproc0/name", "adsp\n")
        self.write("sys/class/remoteproc/remoteproc0/state", "running\n")
        self.write("dev/fastrpc-adsp", "")
        self.write("dev/kmsg", "[    1.0] Booting Linux\n")
        self.write("sys/class/backlight/ae94000.dsi.0/panel_id", "80 00 04\n")
        for s in ("aosd", "cxsd", "ddr"):
            self.write("sys/kernel/debug/qcom_stats/" + s,
                       "Count: 0\nLast Entered At: 0\nLast Exited At: 0\nAccumulated Duration: 0\n")
        self.write("sys/kernel/debug/interconnect/interconnect_summary",
                   " node  tag  avg  peak\n a 0 2147483647 2147483647\n b 0 2147483647 0\n")
        self.write("sys/devices/platform/soc@0/24100000.interconnect/state_synced", "0\n")
        self.write("sys/devices/platform/interconnect-1/state_synced", "0\n")
        self.write("sys/devices/system/cpu/cpu0/cpuidle/state1/s2idle/usage", "5\n")
        self.write("run/ubuntu-gts9u/suspend-mode.applied", "off\n")
        self.policy = ("# Written by ubuntu-gts9u-suspend-mode for mode 'off'.\n"
                       "[Sleep]\nAllowSuspend=no\n")
        self.write("run/systemd/sleep.conf.d/50-ubuntu-gts9u-suspend-mode.conf", self.policy)
        self.write("units/hexagonrpcd-adsp-sensorspd.service", "active\n")
        self.write("units/ubuntu-gts9u-panel-coldboot-recover.service", "active\n")
        self.stub("bin/id", "#!/bin/sh\necho 0\n")
        self.stub("bin/chgrp", "#!/bin/sh\nexit 0\n")
        self.stub("bin/systemctl", STUB_SYSTEMCTL)
        self.stub("bin/rtcwake", STUB_RTCWAKE)
        self.stub("bin/sleep", STUB_SLEEPCMD)
        self.stub("bin/dmesg", STUB_DMESG)
        self.stub("bin/nmcli", STUB_NMCLI)
        self.stub("bin/findmnt", STUB_FINDMNT)
        self.stub("bin/systemd-inhibit", "#!/bin/sh\nexit 0\n")
        self.stub("bin/systemd-notify", "#!/bin/sh\necho \"notify $*\" >> \"$FAKE_ROOT/notify.log\"\n")
        self.stub("usr/lib/systemd/systemd-sleep", STUB_SLEEP)

    def cleanup(self):
        self.tmp.cleanup()

    def write(self, rel, text):
        (self.root / rel).write_text(text)

    def read(self, rel):
        return (self.root / rel).read_text()

    def exists(self, rel):
        return (self.root / rel).exists()

    def stub(self, rel, body):
        path = self.root / rel
        path.write_text(body)
        path.chmod(0o755)

    def install(self, source, name):
        text = source.read_text()
        pattern = re.compile("|".join(re.escape(p) for p in PREFIXES))
        text = pattern.sub(lambda m: str(self.root) + m.group(0), text)
        path = self.root / name
        path.write_text(text)
        path.chmod(0o755)
        return path

    def run(self, script, *args, env_extra=None):
        env = dict(FAKE_ROOT=str(self.root), HOME=str(self.root),
                   PATH="%s:%s" % (self.root / "bin", self.root / "tools"))
        if env_extra:
            env.update(env_extra)
        return subprocess.run(["/bin/sh", str(script), *args], env=env, text=True,
                              capture_output=True, timeout=60)


class SuspendCyclesTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRoot()
        self.script = self.fake.install(RUNNER, "gts9u-suspend-cycles.sh")

    def tearDown(self):
        self.fake.cleanup()

    def run_cycles(self, *args):
        return self.fake.run(self.script, "run", "--foreground", "--gap", "0", "--settle", "0",
                             "--wifi-wait", "2", *args)

    def latest(self):
        return pathlib.Path(os.path.realpath(self.fake.root / "var/log/gts9u-suspendkit/latest"))

    def rows(self):
        lines = (self.latest() / "cycles.tsv").read_text().splitlines()
        header = lines[0].split("\t")
        return [dict(zip(header, line.split("\t"))) for line in lines[1:]]

    def scenario(self, call, text):
        self.fake.write("scenario/%d" % call, text)

    def assert_restored(self):
        f = self.fake
        self.assertFalse(f.exists("run/systemd/sleep.conf.d/zz-gts9u-suspendkit.conf"))
        self.assertEqual(f.read("run/systemd/sleep.conf.d/50-ubuntu-gts9u-suspend-mode.conf"),
                         f.policy)
        self.assertIn(f.read("sys/power/mem_sleep").strip(), ("deep", "s2idle [deep]"))
        self.assertEqual(f.read("sys/power/pm_debug_messages").strip(), "0")
        self.assertEqual(f.read("sys/class/rtc/rtc0/wakealarm"), "")

    # ------------------------------------------------------------ passing run
    def test_full_run_passes_and_restores(self):
        result = self.run_cycles("--s2idle", "2", "--deep", "3")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = self.rows()
        self.assertEqual([r["cycle"] for r in rows],
                         ["s2idle-1", "s2idle-2", "deep-1", "deep-2", "deep-3"])
        for r in rows:
            self.assertEqual(r["result"], "PASS", r)
            self.assertEqual(r["entered"], r["mode"])
            self.assertEqual((r["wake_irq"], r["wake_name"]), ("21", "pm8xxx_rtc_alarm"))
            self.assertEqual((r["aosd_n"], r["cxsd_n"], r["ddr_n"]), ("1", "1", "1"))
            self.assertEqual(r["cxsd_s"], "25.0")
            self.assertEqual(r["slept_s"], "30.012")
            self.assertEqual(r["resume_ms"], "450.125")
            self.assertEqual((r["ok"], r["fail"], r["wifi"], r["panel"], r["root"]),
                             ("1", "0", "connected", "80 00 04", "rw"))
        d = self.latest()
        self.assertTrue((d / "DONE").exists())
        self.assertFalse((d / "IN-CYCLE").exists())
        summary = (d / "SUMMARY.txt").read_text()
        self.assertIn("summary s2idle: 2/2 passed; woken by: 21(pm8xxx_rtc_alarm)x2", summary)
        self.assertIn("summary deep: 3/3 passed", summary)
        self.assertIn("CX collapsed 83% of the time asleep", summary)
        self.assertIn("RESULT: PASS", (d / "run.log").read_text())
        self.assert_restored()
        # Every sleep saw the cycle's drop-in, which allows suspend and lets
        # mem_sleep pick the mode; the policy drop-in stayed beside it.
        seen = self.fake.read("dropins-seen")
        self.assertEqual(seen.count("AllowSuspend=yes\nSuspendState=mem"), 5)
        self.assertEqual(seen.count("AllowSuspend=no"), 5)
        # The RTC self-test came first, then one arm per cycle.
        rtc = self.fake.read("rtcwake.log").splitlines()
        self.assertEqual(sum("-m no -s 20" in l for l in rtc), 1)
        self.assertEqual(sum("-m no -s 30" in l for l in rtc), 5)
        # The sensor PD was stopped before the first sleep, as suspend.target would.
        self.assertIn("systemctl stop hexagonrpcd-adsp-sensorspd.service",
                      self.fake.read("systemctl-at-sleep-1"))

    def test_status_reads_the_run_without_root(self):
        self.run_cycles("--s2idle", "1", "--deep", "0")
        self.fake.stub("bin/id", "#!/bin/sh\necho 1000\n")
        result = self.fake.run(self.script, "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("state: DONE", result.stdout)
        self.assertIn("cycles: 1 done, 1 passed", result.stdout)

    def test_run_needs_root(self):
        self.fake.stub("bin/id", "#!/bin/sh\necho 1000\n")
        result = self.run_cycles("--s2idle", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sudo", result.stderr)
        self.assertFalse(self.fake.exists("sleep-calls"))

    # ---------------------------------------------------------- stop on fail
    def test_stops_at_the_first_failed_return_code(self):
        self.scenario(2, "rc=1 okinc=0 fail=1\n")
        self.fake.write("sys/power/suspend_stats/last_failed_dev", "a600000.usb\n")
        result = self.run_cycles("--s2idle", "3", "--deep", "3")
        self.assertEqual(result.returncode, 1)
        rows = self.rows()
        self.assertEqual([r["result"] for r in rows], ["PASS", "FAIL"])
        self.assertIn("systemd-sleep returned 1", rows[1]["reasons"])
        self.assertIn("last_failed_dev a600000.usb", rows[1]["reasons"])
        self.assertEqual(self.fake.read("sleep-calls").strip(), "2")
        self.assertTrue((self.latest() / "FAILED").exists())
        self.assertIn("RESULT: FAIL at cycle s2idle-2", (self.latest() / "run.log").read_text())
        self.assert_restored()
        # No sleep hook brought the sensor PD back in this fake: the run does.
        self.assertIn("systemctl restart --no-block ubuntu-gts9u-sensors-resume.service",
                      self.fake.read("systemctl.log"))

    def test_sensors_left_to_the_hook_when_it_ran(self):
        self.fake.write("units/ubuntu-gts9u-sensors-resume.service", "activating\n")
        result = self.run_cycles("--s2idle", "1", "--deep", "0", "--no-rtc-check")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("restart --no-block", self.fake.read("systemctl.log"))

    def test_wake_by_another_irq_fails(self):
        self.scenario(1, "wake=171\n")
        result = self.run_cycles("--s2idle", "2", "--deep", "0")
        self.assertEqual(result.returncode, 1)
        row = self.rows()[0]
        self.assertEqual(row["wake_name"], "q6v5 ready")
        self.assertIn("woken by IRQ 171 (q6v5 ready), not pm8xxx_rtc_alarm", row["reasons"])
        self.assertIn("the RTC alarm had not fired yet", row["reasons"])

    def test_wrong_mode_entered_fails(self):
        self.scenario(1, "entered=s2idle\n")
        result = self.run_cycles("--s2idle", "0", "--deep", "1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("the kernel entered 's2idle', not deep", self.rows()[0]["reasons"])

    def test_kernel_failure_signature_fails(self):
        self.scenario(1, "extra='ufshcd-qcom 1d84000.ufshc: ufshcd_abort: Device abort task at tag 7'\n")
        result = self.run_cycles("--s2idle", "2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("kernel log: ", self.rows()[0]["reasons"])
        self.assertTrue((self.latest() / "s2idle-1-FAILURE.txt").exists())

    def test_noted_lines_do_not_fail(self):
        self.scenario(1, "extra='pcieport 0000:00:00.0: AER: Correctable error message received from 0000:01:00.0'\n")
        self.scenario(2, "extra='dwc3-qcom a600000.usb: port-1 HS-PHY not in L2'\n")
        result = self.run_cycles("--s2idle", "2", "--deep", "0")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("noted=1", (self.latest() / "run.log").read_text())

    def test_dead_panel_fails(self):
        self.scenario(1, "panel='00 00 00'\n")
        result = self.run_cycles("--s2idle", "1", "--deep", "0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("the panel DDIC answered 00 00 00", self.rows()[0]["reasons"])

    def test_wifi_must_return_when_it_was_connected(self):
        # Connected at the start; the scenario drops it during the first sleep.
        self.scenario(1, ": > \"$R/wifi\"; echo disconnected > \"$R/wifi\"\n")
        result = self.run_cycles("--s2idle", "1", "--deep", "0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Wi-Fi is 'disconnected'", self.rows()[0]["reasons"])

    def test_wifi_not_required_when_it_was_down(self):
        self.fake.write("wifi", "disconnected\n")
        result = self.run_cycles("--s2idle", "1", "--deep", "0")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_read_only_root_fails(self):
        self.scenario(1, "echo ro,noatime > \"$R/rootopts\"\n")
        result = self.run_cycles("--s2idle", "2", "--keep-going")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.rows()), 1)
        self.assertIn("the root filesystem is 'ro'", self.rows()[0]["reasons"])

    def test_keep_going_records_and_continues(self):
        self.scenario(1, "wake=22\n")
        result = self.run_cycles("--s2idle", "3", "--deep", "0", "--keep-going")
        self.assertEqual(result.returncode, 1)
        self.assertEqual([r["result"] for r in self.rows()], ["FAIL", "PASS", "PASS"])
        self.assertIn("summary s2idle: 2/3 passed", (self.latest() / "SUMMARY.txt").read_text())

    # ------------------------------------------------------------ prechecks
    def test_rtc_that_never_fires_stops_before_any_sleep(self):
        self.fake.write("rtc-deaf", "")
        result = self.run_cycles("--s2idle", "2")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.fake.exists("sleep-calls"))
        self.assertIn("rtc-check: FAILED", (self.latest() / "run.log").read_text())
        self.assertTrue((self.latest() / "ABORTED").exists())
        self.assert_restored()

    def test_refuses_while_the_adsp_starts(self):
        self.fake.write("units/ubuntu-gts9u-adsp-start-inhibit.service", "active\n")
        result = self.run_cycles("--s2idle", "1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("audio DSP starts", result.stdout)
        self.assertFalse(self.fake.exists("sleep-calls"))

    def test_refuses_on_another_board(self):
        (self.fake.root / "proc/device-tree/compatible").write_bytes(GTS9UWIFI)
        result = self.run_cycles("--s2idle", "1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not an SM-X710", result.stdout)

    def test_refuses_early_in_boot(self):
        self.fake.write("proc/uptime", "40.00 100.00\n")
        result = self.run_cycles("--s2idle", "1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("uptime 40s", result.stdout)

    def test_second_run_leaves_the_first_alone(self):
        import fcntl
        self.run_cycles("--s2idle", "1", "--deep", "0", "--no-rtc-check")
        first = self.latest()
        with open(self.fake.root / "run/gts9u-suspendkit.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_cycles("--s2idle", "1", "--deep", "0", "--no-rtc-check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("holds", result.stderr)
        self.assertEqual(self.latest(), first)
        self.assertEqual(self.fake.read("sleep-calls").strip(), "1")

    def test_leftover_dropin_is_removed(self):
        self.fake.write("run/systemd/sleep.conf.d/zz-gts9u-suspendkit.conf", "[Sleep]\nAllowSuspend=yes\n")
        result = self.run_cycles("--s2idle", "1", "--deep", "0", "--no-rtc-check")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("left by an interrupted run", (self.latest() / "run.log").read_text())
        self.assert_restored()

    # ------------------------------------------------------- interconnect
    def test_release_icc_writes_state_synced_once(self):
        result = self.run_cycles("--s2idle", "1", "--deep", "0", "--release-icc", "--icc-watch", "10")
        self.assertEqual(result.returncode, 0, result.stdout)
        for p in ("sys/devices/platform/soc@0/24100000.interconnect/state_synced",
                  "sys/devices/platform/interconnect-1/state_synced"):
            self.assertEqual(self.fake.read(p), "1")
        log = (self.latest() / "run.log").read_text()
        self.assertIn("INT_MAX lines 2", log)
        self.assertIn("icc: 10s after the release clean", log)
        # A plain file keeps the last write; the kernel's list would not.
        self.fake.write("sys/power/mem_sleep", "s2idle [deep]\n")
        second =self.run_cycles("--s2idle", "1", "--deep", "0", "--release-icc", "--no-rtc-check")
        self.assertEqual(second.returncode, 0, second.stdout)
        self.assertIn("already synced", (self.latest() / "run.log").read_text())

    def test_intmax_zero_is_one_number(self):
        self.fake.write("sys/kernel/debug/interconnect/interconnect_summary", " node  tag  avg  peak\n a 0 0 0\n")
        result = self.run_cycles("--s2idle", "1", "--deep", "0", "--release-icc", "--icc-watch", "0",
                                 "--no-rtc-check")
        self.assertEqual(result.returncode, 0, result.stdout)
        log = (self.latest() / "run.log").read_text()
        self.assertIn("INT_MAX lines now 0 (target 0); watching", log)
        self.assertNotIn("n/a", log.split("icc: released")[1].splitlines()[0])

    # ----------------------------------------------------------------- keys
    def test_key_power_passes_on_pwrkey(self):
        self.scenario(1, "wake=22\n")
        result = self.fake.run(self.script, "key", "power", "--foreground", "--settle", "0",
                               "--wifi-wait", "1")
        self.assertEqual(result.returncode, 0, result.stdout)
        row = self.rows()[0]
        self.assertEqual((row["cycle"], row["wake_name"], row["result"]),
                         ("s2idle-power", "pmic_pwrkey", "PASS"))
        self.assertIn("-m no -s 90", self.fake.read("rtcwake.log"))
        # The RTC backup alarm is cleared once the key woke it.
        self.assertEqual(self.fake.read("sys/class/rtc/rtc0/wakealarm"), "")

    def test_key_voldown_fails_on_rtc_fallback(self):
        result = self.fake.run(self.script, "key", "voldown", "--mode", "deep", "--foreground",
                               "--settle", "0", "--wifi-wait", "1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not pmic_resin", self.rows()[0]["reasons"])

    # ------------------------------------------------------- rtc-check, stop
    def test_rtc_check_alone(self):
        result = self.fake.run(self.script, "rtc-check")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("rtc-check: ok, IRQ 21 pm8xxx_rtc_alarm 0 -> 1, wakealarm cleared", result.stdout)
        self.fake.write("rtc-deaf", "")
        result = self.fake.run(self.script, "rtc-check")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.fake.read("sys/class/rtc/rtc0/wakealarm"), "")

    def test_stop_file_ends_the_run_between_cycles(self):
        self.scenario(1, "touch \"$R/run/gts9u-suspendkit.stop\"\n")
        result = self.run_cycles("--s2idle", "3", "--deep", "0", "--no-rtc-check")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(self.fake.read("sleep-calls").strip(), "1")
        self.assertTrue((self.latest() / "STOPPED").exists())
        self.assert_restored()

    def test_check_never_sleeps(self):
        result = self.fake.run(self.script, "check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("pm8xxx_rtc_alarm: IRQ 21", result.stdout)
        self.assertIn("pmic_pwrkey: IRQ 22", result.stdout)
        self.assertIn("pmic_resin: IRQ 23", result.stdout)
        self.assertFalse(self.fake.exists("sleep-calls"))


class LadderTest(unittest.TestCase):
    def test_ladder_names_wake_irqs(self):
        fake = FakeRoot()
        try:
            script = fake.install(LADDER, "x710-suspend-ladder.sh")
            result = fake.run(script, "check")
            self.assertIn("pm8xxx_rtc_alarm: IRQ 21", result.stdout)
            self.assertIn("pmic_pwrkey: IRQ 22", result.stdout)
            self.assertIn("pmic_resin: IRQ 23", result.stdout)
        finally:
            fake.cleanup()


class AdspStartInhibitTest(unittest.TestCase):
    # sleep: each call is one second; the DSP comes up on call UP_AT.
    STUB = """#!/bin/sh
R=$FAKE_ROOT
n=$(( $(cat "$R/sleep-n" 2>/dev/null || echo 0) + 1 ))
echo $n > "$R/sleep-n"
echo "sleep $*" >> "$R/sleep.log"
if [ -n "${UP_AT:-}" ] && [ "$n" -ge "$UP_AT" ]; then
	echo running > "$R/sys/class/remoteproc/remoteproc0/state"
	: > "$R/dev/fastrpc-adsp"
	echo ' 0 [SamsungGalaxyTa]: sm8550 - Samsung-Galaxy-Tab-S9' > "$R/proc/asound/cards"
fi
"""

    def setUp(self):
        self.fake = FakeRoot()
        f = self.fake
        (f.root / "proc/asound").mkdir(parents=True, exist_ok=True)
        f.write("proc/asound/cards", "--- no soundcards ---\n")
        f.write("sys/class/remoteproc/remoteproc0/state", "offline\n")
        (f.root / "dev/fastrpc-adsp").unlink()
        f.write("units/ubuntu-gts9u-adsp-boot.service", "activating\n")
        f.stub("bin/sleep", self.STUB)
        self.script = f.install(INHIBIT, "adsp-start-inhibit")

    def tearDown(self):
        self.fake.cleanup()

    def hold(self, **env):
        env.setdefault("NOTIFY_SOCKET", "/run/fake-notify")
        return self.fake.run(self.script, "hold", env_extra=env)

    def test_releases_after_the_dsp_is_up_and_settled(self):
        result = self.hold(UP_AT="4", ADSP_INHIBIT_SETTLE="7")
        self.assertEqual(result.returncode, 0)
        self.assertIn("notify --ready", self.fake.read("notify.log"))
        self.assertIn("audio DSP up after 4s", result.stderr)
        self.assertIn("sleep 7", self.fake.read("sleep.log"))

    def test_nothing_to_hold_when_already_up(self):
        f = self.fake
        f.write("sys/class/remoteproc/remoteproc0/state", "running\n")
        f.write("dev/fastrpc-adsp", "")
        f.write("proc/asound/cards", " 0 [SamsungGalaxyTa]: sm8550\n")
        result = self.hold()
        self.assertEqual(result.returncode, 0)
        self.assertIn("already up", result.stderr)
        self.assertFalse(f.exists("sleep.log"))

    def test_gives_up_when_the_start_failed(self):
        self.fake.write("units/ubuntu-gts9u-adsp-boot.service", "failed\n")
        result = self.hold()
        self.assertEqual(result.returncode, 0)
        self.assertIn("failed after 0s; releasing", result.stderr)

    def test_gives_up_at_the_cap(self):
        result = self.hold(ADSP_INHIBIT_MAX="5")
        self.assertEqual(result.returncode, 0)
        self.assertIn("after 5s; releasing anyway", result.stderr)
        self.assertIn("remoteproc fastrpc-adsp sound-card", result.stderr)

    def test_no_notify_socket_by_hand(self):
        result = self.fake.run(self.script, "hold", env_extra={"UP_AT": "1", "ADSP_INHIBIT_SETTLE": "0"})
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.fake.exists("notify.log"))

    def test_unit_wiring(self):
        unit = INHIBIT_UNIT.read_text()
        self.assertIn("Before=ubuntu-gts9u-adsp-boot.service", unit)
        self.assertIn("After=ubuntu-gts9u-panel-coldboot-recover.service", unit)
        self.assertIn("WantedBy=ubuntu-gts9u-adsp-boot.service", unit)
        self.assertIn("ConditionFirmware=device-tree-compatible(samsung,gts9wifi)", unit)
        self.assertRegex(unit, r"(?m)^Type=notify$")
        self.assertRegex(unit, r"(?m)^NotifyAccess=all$")
        self.assertRegex(unit, r"(?m)^ExecStart=/usr/bin/systemd-inhibit --what=sleep --mode=block .* /usr/libexec/ubuntu-gts9u-adsp-start-inhibit hold$")
        self.assertIn("ubuntu-gts9u-adsp-start-inhibit.service:ubuntu-gts9u-adsp-boot.service",
                      POSTINST.read_text())


KIT = REPO / "tools/inplace/suspendkit"
KIT_PREFIXES = ("/usr/libexec/", "/usr/lib/systemd/system/", "/etc/systemd/system/",
                "/var/log/", "/var/backups/", "/run/", "/proc/", "/sys/", "/dev/")

KIT_SYSTEMCTL = """#!/bin/sh
R=$FAKE_ROOT
echo "systemctl $*" >> "$R/systemctl.log"
wants="$R/etc/systemd/system/ubuntu-gts9u-adsp-boot.service.wants"
case "$1" in
enable) mkdir -p "$wants"; ln -sf "$R/usr/lib/systemd/system/$2" "$wants/$2" ;;
disable) rm -f "$wants/$2" ;;
is-enabled) [ -L "$wants/$2" ] && echo enabled || { echo disabled; exit 1; } ;;
start) [ -e "$R/start-fails" ] && exit 1
	echo "ubuntu-gts9u-adsp-start-inhibit: the audio DSP is already up: nothing to hold" >> "$R/journal" ;;
show) case "$*" in
	*Result*) echo success ;;
	*ActiveState*) echo inactive ;;
	*Monotonic*) echo 0 ;;
	esac ;;
esac
exit 0
"""

KIT_DPKG = """#!/bin/sh
[ "$1" = -S ] || exit 2
f="$FAKE_ROOT/owned/$(basename "$2")"
[ -e "$f" ] || { echo "dpkg-query: no path found matching pattern $2" >&2; exit 1; }
echo "ubuntu-gts9u-device: $2"
"""


class SuspendKitTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRoot()
        r = self.fake.root
        for d in ("usr/libexec", "usr/lib/systemd/system", "etc/systemd/system",
                  "var/backups", "owned", "kitbuild"):
            (r / d).mkdir(parents=True, exist_ok=True)
        self.fake.write("usr/lib/systemd/system/ubuntu-gts9u-adsp-boot.service", "[Unit]\n")
        self.fake.write("journal", "")
        self.fake.stub("bin/systemctl", KIT_SYSTEMCTL)
        self.fake.stub("bin/dpkg", KIT_DPKG)
        self.fake.stub("bin/fuser", "#!/bin/sh\nexit 1\n")
        self.fake.stub("bin/systemd-analyze", "#!/bin/sh\nexit 0\n")
        self.fake.stub("bin/journalctl", "#!/bin/sh\ncat \"$FAKE_ROOT/journal\"\n")
        for tool in ("install", "sha256sum", "cmp", "stat", "mktemp", "find", "basename",
                     "dirname", "git"):
            target = r / "tools" / tool
            if not target.exists():
                os.symlink(shutil.which(tool), target)
        self.kit = r / "kitbuild/suspendkit"
        build = subprocess.run(["/bin/sh", str(KIT / "build-kit.sh"), str(self.kit)],
                               text=True, capture_output=True, timeout=60)
        self.assertEqual(build.returncode, 0, build.stderr)
        # Point every kit script at the fake root, then re-sum the kit.
        pattern = re.compile("|".join(re.escape(p) for p in KIT_PREFIXES))
        for script in self.kit.glob("*.sh"):
            text = pattern.sub(lambda m: str(r) + m.group(0), script.read_text())
            script.write_text(text)
        sums = subprocess.run("find . -type f ! -name SHA256SUMS ! -name .suspendkit | "
                              "sed 's#^./##' | LC_ALL=C sort | xargs sha256sum",
                              shell=True, cwd=self.kit, text=True, capture_output=True)
        (self.kit / "SHA256SUMS").write_text(sums.stdout)

    def tearDown(self):
        self.fake.cleanup()

    def kit_run(self, name, *args):
        return self.fake.run(self.kit / name, *args)

    def test_build_lists_every_file(self):
        names = sorted(l.split()[1] for l in (self.kit / "SHA256SUMS").read_text().splitlines())
        self.assertEqual(names, ["README.md", "REVISION", "files/ubuntu-gts9u-adsp-start-inhibit",
                                 "files/ubuntu-gts9u-adsp-start-inhibit.service",
                                 "gts9u-suspend-cycles.sh", "install.sh", "rollback.sh",
                                 "verify.sh", "x710-suspend-ladder.sh"])

    def test_install_verify_rollback(self):
        f = self.fake
        result = self.kit_run("install.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        helper = f.root / "usr/libexec/ubuntu-gts9u-adsp-start-inhibit"
        unit = f.root / "usr/lib/systemd/system/ubuntu-gts9u-adsp-start-inhibit.service"
        self.assertEqual(helper.read_bytes(), INHIBIT.read_bytes())
        self.assertEqual(unit.read_text(), INHIBIT_UNIT.read_text())
        self.assertEqual(oct(helper.stat().st_mode & 0o777), "0o755")
        self.assertTrue(f.exists("etc/systemd/system/ubuntu-gts9u-adsp-boot.service.wants/"
                                 "ubuntu-gts9u-adsp-start-inhibit.service"))
        log = f.read("var/log/gts9u-suspendkit-install.log")
        self.assertIn("smoke test: ok", log)
        self.assertIn("files that existed before: 0", log)
        again = self.kit_run("install.sh")
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertIn("backups from", again.stdout)

        verify = self.kit_run("verify.sh")
        self.assertIn("ok     kit checksums", verify.stdout)
        self.assertIn("ubuntu-gts9u-adsp-start-inhibit.service enabled", verify.stdout)
        self.assertIn("installed during this boot", verify.stdout)

        back = self.kit_run("rollback.sh")
        self.assertEqual(back.returncode, 0, back.stdout + back.stderr)
        self.assertFalse(helper.exists())
        self.assertFalse(unit.exists())
        self.assertFalse(f.exists("etc/systemd/system/ubuntu-gts9u-adsp-boot.service.wants/"
                                  "ubuntu-gts9u-adsp-start-inhibit.service"))
        self.assertFalse(f.exists("var/backups/ubuntu-gts9u-suspendkit"))
        self.assertNotEqual(self.kit_run("rollback.sh").returncode, 0)

    def test_failed_smoke_test_is_reported(self):
        self.fake.write("start-fails", "")
        result = self.kit_run("install.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("smoke test: FAILED", result.stdout)

    def test_packaged_identical_files_need_nothing(self):
        f = self.fake
        shutil.copy(INHIBIT, f.root / "usr/libexec/ubuntu-gts9u-adsp-start-inhibit")
        shutil.copy(INHIBIT_UNIT, f.root / "usr/lib/systemd/system/ubuntu-gts9u-adsp-start-inhibit.service")
        f.write("owned/ubuntu-gts9u-adsp-start-inhibit", "")
        f.write("owned/ubuntu-gts9u-adsp-start-inhibit.service", "")
        result = self.kit_run("install.sh")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("nothing to install", result.stdout)
        self.assertFalse(f.exists("var/backups/ubuntu-gts9u-suspendkit"))

    def test_packaged_different_file_is_not_overwritten(self):
        f = self.fake
        f.write("usr/libexec/ubuntu-gts9u-adsp-start-inhibit", "#!/bin/sh\n# newer\n")
        f.write("owned/ubuntu-gts9u-adsp-start-inhibit", "")
        result = self.kit_run("install.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not overwriting", result.stdout)
        self.assertEqual(f.read("usr/libexec/ubuntu-gts9u-adsp-start-inhibit"), "#!/bin/sh\n# newer\n")

    def test_checksum_mismatch_stops(self):
        with open(self.kit / "files/ubuntu-gts9u-adsp-start-inhibit", "a") as fh:
            fh.write("# tampered\n")
        result = self.kit_run("install.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("kit checksum mismatch", result.stdout)
        self.assertFalse(self.fake.exists("usr/libexec/ubuntu-gts9u-adsp-start-inhibit"))

    def test_other_board_stops(self):
        (self.fake.root / "proc/device-tree/compatible").write_bytes(GTS9UWIFI)
        result = self.kit_run("install.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not an SM-X710", result.stdout)


if __name__ == "__main__":
    unittest.main()
