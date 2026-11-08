#!/bin/sh
# x710-suspend-ladder.sh - suspend/resume test ladder for the SM-X710 (gts9wifi)
#
# Copy to the tablet (tools/inplace/suspendkit puts it in ~/suspendkit) and
# run the sleeping rungs with sudo:
#
#   ssh -t <user>@10.42.0.2 "sudo sh ~/suspendkit/x710-suspend-ladder.sh check"
#
# Every rung that suspends runs detached in a transient systemd unit, so a
# USB link that drops while the gadget is suspended (or a closed SSH session)
# cannot cut it short.  Each step is logged with the syslog tag "pmtest";
# read the results afterwards, also after a forced restart, with
#
#   ssh -t <user>@10.42.0.2 "sudo sh ~/suspendkit/x710-suspend-ladder.sh report"     # this boot
#   ssh -t <user>@10.42.0.2 "sudo sh ~/suspendkit/x710-suspend-ladder.sh report -1"  # last boot
#
# Subcommands (all but report need root):
#   check                 read-only snapshot of the sleep configuration
#   pmtest LEVEL...       pm_test rungs: freezer devices platform processors core
#   s2idle [nowifi] [nobt] [trace]
#   deep [nowifi] [nobt] [trace]
#                         one real sleep; press the power key to wake.
#                         Wake IRQs are named, not numbered, because the
#                         numbers move with probe order: on the X710 with the
#                         PMIC RTC registered they are pm8xxx_rtc_alarm 21,
#                         pmic_pwrkey 22 (Power) and pmic_resin 23 (Volume
#                         Down); before the RTC, pwrkey was 21 and resin 22.
#                         Every result line prints pm_wakeup_irq=N (name).
#                         nowifi removes the Wi-Fi PCIe endpoint first (a PCI
#                         rescan brings it back afterwards), nobt soft-blocks
#                         Bluetooth, trace keeps the consoles running during
#                         suspend with pm_print_times and every log level, so
#                         a hang leaves its last device in the ramoops console
#   mark                  write a marker into the kernel log (ramoops test)
#   report [BOOT]         journal lines of BOOT (default 0) and pstore records
#
# How a rung sleeps.  It runs systemd-sleep itself, as systemd-suspend.service
# would, so the /usr/lib/systemd/system-sleep hooks (USB gadget, sensors,
# flashlight) still run around the transition.  It does not go through
# suspend.target or logind, because on this board those are deliberately
# closed: sleep.target and suspend.target may be masked, and the suspend-mode
# switch writes AllowSuspend=no (mode off) or SuspendState=freeze (mode s2idle)
# into /run/systemd/sleep.conf.d.  Either would make every rung fail at once,
# or run the "deep" and processors/core rungs as suspend-to-idle.  For the
# length of one rung only, a later-sorting drop-in in /run sets
# AllowSuspend=yes and SuspendState=mem, so /sys/power/mem_sleep alone picks
# the mode; it is removed afterwards, also on failure.  Masks and the policy
# drop-in are left exactly as they were.  logind sends no PrepareForSleep for
# such a sleep (NetworkManager and GNOME are not told), so finish with one
# suspend through the real path before changing the power-key policy.
#
# DRY_RUN=1 prints every sysfs write and command instead of running it.
set -eu

TAG=pmtest
POWER=/sys/power
DEBUGFS=/sys/kernel/debug
WIFI_EP=/sys/bus/pci/devices/0000:01:00.0
ROOT_PORT=/sys/bus/pci/devices/0000:00:00.0
SLEEP_BIN=/usr/lib/systemd/systemd-sleep
LADDER_DROPIN_DIR=/run/systemd/sleep.conf.d
LADDER_DROPIN=$LADDER_DROPIN_DIR/zz-x710-suspend-ladder.conf
dry=${DRY_RUN:-0}

say() { printf '%s\n' "$*"; }
note() { say "$*"; [ "$dry" = 1 ] || logger -t "$TAG" "$*" 2>/dev/null || true; }
die() { say "x710-suspend-ladder: $*" >&2; exit 1; }

# wr VALUE FILE: one sysfs write, or what it would be.
wr() {
	if [ "$dry" = 1 ]; then
		say "would write '$1' > $2"
	else
		printf '%s\n' "$1" > "$2"
	fi
}

run() {
	if [ "$dry" = 1 ]; then
		say "would run: $*"
	else
		"$@"
	fi
}

need_root() {
	[ "$dry" = 1 ] || [ "$(id -u)" = 0 ] || die "run this with sudo"
}

selected() { sed -n 's/.*\[\([a-z0-9]*\)\].*/\1/p' "$1" 2>/dev/null; }

# irq_name N: the action name of IRQ N in /proc/interrupts ("none" for none).
irq_name() {
	case "$1" in ''|none) echo none; return ;; esac
	awk -v q="$1:" '$1 == q {line = $0; if (sub(/.*(Edge|Level)[ \t]+/, "", line)) print line; else print $NF; f = 1; exit} END {if (!f) print "?"}' /proc/interrupts 2>/dev/null
}

# irq_of NAME: the IRQ number of the action NAME ("missing" when absent).
irq_of() {
	n=$(awk -v n="$1" '$1 ~ /^[0-9]+:$/ && $NF == n {sub(":", "", $1); print $1; exit}' /proc/interrupts 2>/dev/null)
	echo "${n:-missing}"
}

# The mode the kernel actually entered last, from its own "suspend entry" line.
last_entry() {
	dmesg 2>/dev/null | sed -n 's/.*PM: suspend entry (\([a-z0-9]*\)).*/\1/p' | tail -1
}

drop_override() {
	if [ "$dry" = 1 ]; then
		say "would remove $LADDER_DROPIN"
	else
		rm -f "$LADDER_DROPIN"
	fi
}

# sleep_now: one suspend through systemd-sleep, in the mode mem_sleep selects.
sleep_now() {
	if [ "$dry" = 1 ]; then
		say "would write $LADDER_DROPIN: [Sleep] AllowSuspend=yes SuspendState=mem"
	else
		# Without the override systemd-sleep would refuse, or pick the
		# policy's state, so a failure here ends the rung.
		mkdir -p "$LADDER_DROPIN_DIR" || return 1
		printf '%s\n' '# x710-suspend-ladder.sh, for one test rung only; removed afterwards.' \
			'[Sleep]' 'AllowSuspend=yes' 'SuspendState=mem' > "$LADDER_DROPIN" || return 1
	fi
	src=0
	run "$SLEEP_BIN" suspend || src=$?
	drop_override
	return "$src"
}

detach() {
	# Re-run this script with an internal subcommand in a transient unit.
	unit=pmtest-$1-$(date +%H%M%S)
	if [ "$dry" = 1 ]; then
		say "would run: systemd-run --unit=$unit --collect --quiet /bin/sh $self __$*"
		sh "$self" "__$@"
		return
	fi
	systemd-run --unit="$unit" --collect --quiet /bin/sh "$self" "__$@"
	say "started $unit; follow with: journalctl -f -t $TAG"
}

self=$(readlink -f "$0")

# ---------------------------------------------------------------- check ---
do_check() {
	say "== kernel";           uname -r
	say "== cmdline";          cat /proc/cmdline
	say "== mem_sleep: $(cat $POWER/mem_sleep)   state: $(cat $POWER/state)"
	say "== pm_test: $(cat $POWER/pm_test)"
	say "== suspend_stats"
	grep -H . $POWER/suspend_stats/success $POWER/suspend_stats/fail \
		$POWER/suspend_stats/last_failed_dev \
		$POWER/suspend_stats/last_failed_step 2>/dev/null || true
	say "== sleep policy (the ladder bypasses it for one rung at a time, and leaves it as it is)"
	for u in sleep.target suspend.target systemd-suspend.service; do
		say "  $u: $(systemctl show -p LoadState --value "$u" 2>/dev/null)"
	done
	for f in /etc/systemd/sleep.conf.d/*.conf /run/systemd/sleep.conf.d/*.conf; do
		[ -e "$f" ] || continue
		say "  $f: $(grep -E '^(AllowSuspend|SuspendState)=' "$f" | tr '\n' ' ')"
	done
	[ -e "$LADDER_DROPIN" ] && say "  WARNING: $LADDER_DROPIN is left over from an interrupted rung; delete it"
	say "== wakeup sources (registered)"
	for w in /sys/class/wakeup/wakeup*; do
		[ -e "$w/name" ] && say "  $(cat "$w/name")"
	done
	say "== power supplies"
	for p in /sys/class/power_supply/*; do
		say "  $(basename "$p") wakeup=$(cat "$p/power/wakeup" 2>/dev/null || echo n/a)"
	done
	say "== Volume Down (resin) wakeup: $(cat /sys/bus/platform/devices/*pon@1300:resin/power/wakeup 2>/dev/null || echo n/a)"
	say "== wake IRQs (expected on the X710 with the PMIC RTC: rtc alarm 21, Power 22, Volume Down 23)"
	for name in pm8xxx_rtc_alarm pmic_pwrkey pmic_resin; do
		say "  $name: IRQ $(irq_of "$name")"
	done
	say "== LPASS LPI pinctrl / VA macro runtime PM (expect active or suspended, not unsupported)"
	for d in 6e80000.pinctrl 6d44000.codec; do
		say "  $d: $(cat /sys/bus/platform/devices/$d/power/runtime_status 2>/dev/null || echo absent)"
	done
	say "== kernel log: driver-core race, deferred-probe timeout, ramoops"
	dmesg 2>/dev/null | grep -E 'should not be sleeping|error -EACCES: Error applying|deferred probe pending|Attempt to enable runtime PM when it is blocked|ramoops: using' | head -20 || true
	say "== coldboot cycle"
	journalctl -b -o short-monotonic --no-pager 2>/dev/null |
		grep -E 'panel-coldboot-recover|PM: suspend (entry|exit)|suspend debug' | head -12 || true
	say "== pstore"
	ls -la /sys/fs/pstore 2>/dev/null || say "  (not readable)"
	ls -d /var/lib/systemd/pstore/* 2>/dev/null || true
	say "== radios"
	ls "$WIFI_EP/driver" >/dev/null 2>&1 && say "  Wi-Fi endpoint bound" || say "  Wi-Fi endpoint absent or unbound"
	rfkill -n 2>/dev/null || true
	say "== ADSP: $(cat /sys/class/remoteproc/remoteproc*/state 2>/dev/null | tr '\n' ' ')"
}

# --------------------------------------------------------------- pmtest ---
pmtest_restore() {
	wr none $POWER/pm_test || true
	[ -z "$default" ] || [ "$default" = "$(selected $POWER/mem_sleep)" ] ||
		wr "$default" $POWER/mem_sleep || true
	wr 0 $POWER/pm_print_times || true
	wr 0 $POWER/pm_debug_messages || true
	drop_override || true
}

run_pmtest() {
	default=$(selected $POWER/mem_sleep)
	trap pmtest_restore EXIT
	trap 'exit 1' INT TERM
	# Per-device callback timings, and the PM core's phase timings.
	wr 1 $POWER/pm_print_times
	wr 1 $POWER/pm_debug_messages
	for level in "$@"; do
		case $level in
		freezer|devices|platform) mode=$default ;;
		# The kernel refuses these two for suspend-to-idle; they never
		# reach PSCI SYSTEM_SUSPEND, so "deep" is safe for them.
		processors|core) mode=deep ;;
		*) note "unknown pm_test level $level"; continue ;;
		esac
		[ "$mode" = "$(selected $POWER/mem_sleep)" ] || wr "$mode" $POWER/mem_sleep
		wr "$level" $POWER/pm_test
		note "rung $level ($mode) start"
		rc=0
		sleep_now || rc=$?
		note "rung $level ($mode) returned rc=$rc, kernel entered: $(last_entry)"
		wr none $POWER/pm_test || true
		[ "$default" = "$(selected $POWER/mem_sleep)" ] || wr "$default" $POWER/mem_sleep || true
		[ "$dry" = 1 ] || sleep 15
	done
	note "pm_test ladder done: success=$(cat $POWER/suspend_stats/success) fail=$(cat $POWER/suspend_stats/fail)"
}

# ----------------------------------------------------------- real sleep ---
snapshot() {
	for c in /sys/devices/system/cpu/cpu[0-9]*/cpuidle/state*/s2idle/usage; do
		[ -e "$c" ] && printf '%s=%s ' "$(echo "$c" | cut -d/ -f6,8)" "$(cat "$c")"
	done
	echo
}

sleep_restore() {
	# Best effort: report what happened even if one restore fails.
	wr none $POWER/pm_test || true
	wr 0 $POWER/pm_debug_messages || true
	[ -z "$default" ] || [ "$default" = "$(selected $POWER/mem_sleep)" ] ||
		wr "$default" $POWER/mem_sleep || true
	drop_override || true
	if [ "$trace" = 1 ]; then
		wr 0 $POWER/pm_print_times || true
		[ -z "$printk_was" ] || wr "$printk_was" /proc/sys/kernel/printk || true
		[ -z "$csusp_was" ] || wr "$csusp_was" /sys/module/printk/parameters/console_suspend || true
	fi
	[ "$bt" = 1 ] || run rfkill unblock bluetooth || true
	if [ "$wifi" = 0 ] && [ ! -e "$WIFI_EP" ]; then
		note "rescanning the Wi-Fi root port"
		wr 1 "$ROOT_PORT/rescan" || true
	fi
}

run_sleep() {
	mode=$1; shift
	wifi=1 bt=1 trace=0
	for opt in "$@"; do
		case $opt in
		nowifi) wifi=0 ;;
		nobt) bt=0 ;;
		trace) trace=1 ;;
		*) note "ignoring unknown option $opt" ;;
		esac
	done
	default=$(selected $POWER/mem_sleep)
	printk_was=$(cut -f1 /proc/sys/kernel/printk)
	csusp_was=$(cat /sys/module/printk/parameters/console_suspend)
	trap sleep_restore EXIT
	trap 'exit 1' INT TERM

	if [ "$wifi" = 0 ] && [ -e "$WIFI_EP" ]; then
		note "removing the Wi-Fi PCIe endpoint for this rung"
		wr 1 "$WIFI_EP/remove"
	fi
	[ "$bt" = 1 ] || run rfkill block bluetooth || true
	if [ "$trace" = 1 ]; then
		dmesg | grep -q 'ramoops: using' ||
			note "no ramoops backend: a trace of a hang will not survive the restart"
		wr N /sys/module/printk/parameters/console_suspend
		wr 8 /proc/sys/kernel/printk
		wr 1 $POWER/pm_print_times
	fi
	wr none $POWER/pm_test
	wr 1 $POWER/pm_debug_messages
	wr "$mode" $POWER/mem_sleep
	wake_before=$(cat $POWER/wakeup_count)
	note "s2idle usage before: $(snapshot)"
	run sync
	note "$mode start (wake it with one short power-key press: pmic_pwrkey, IRQ $(irq_of pmic_pwrkey); Volume Down is pmic_resin, IRQ $(irq_of pmic_resin))"
	rc=0
	sleep_now || rc=$?
	wirq=$(cat $POWER/pm_wakeup_irq 2>/dev/null || echo none)
	note "$mode returned rc=$rc kernel entered: $(last_entry) pm_wakeup_irq=$wirq ($(irq_name "$wirq")) wakeup_count $wake_before->$(cat $POWER/wakeup_count) root=$(findmnt -no OPTIONS / | cut -d, -f1)"
	note "s2idle usage after: $(snapshot)"
	if [ -r $DEBUGFS/wakeup_sources ]; then
		# The sources that fired last are the ones to look at for an
		# early wake or an aborted entry.
		# Column 9 is last_change.
		sort -k9 -n -r $DEBUGFS/wakeup_sources 2>/dev/null | head -6 |
			while read -r line; do note "wakeup_sources: $line"; done
	fi
	sleep_restore
	trap - EXIT
	note "$mode rung done: success=$(cat $POWER/suspend_stats/success) fail=$(cat $POWER/suspend_stats/fail) last_failed_dev=$(cat $POWER/suspend_stats/last_failed_dev 2>/dev/null)"
}

# --------------------------------------------------------------- report ---
do_report() {
	boot=${1:-0}
	say "== journal, boot $boot"
	journalctl -b "$boot" -o short-monotonic --no-pager -t kernel -t "$TAG" \
		-t ubuntu-gts9u-panel-coldboot-recover -t ubuntu-gts9u-powerkey \
		-t systemd-sleep 2>/dev/null |
		grep -E "$TAG|PM: (suspend|resume|Triggering|Timekeeping|Preparing|Suspending|Finishing|Some devices|parent|dpm_run_callback|.* (complete|aborted) after)|suspend debug|Freezing|Filesystems sync|Restarting tasks|Disabling non-boot|Enabling non-boot|returns -|error -|panel-coldboot-recover|ana38407 panel id|powerkey|Entering sleep state|System returned from sleep" |
		grep -v 'PM: hibernation' | tail -80 || true
	if [ -r /sys/fs/pstore ]; then
		say "== /sys/fs/pstore"
		ls -la /sys/fs/pstore
		for f in /sys/fs/pstore/*; do
			[ -f "$f" ] || continue
			say "--- tail of $f"
			tail -40 "$f"
		done
	else
		say "== pstore needs root: sudo sh $self report $boot"
	fi
	ls -la /var/lib/systemd/pstore 2>/dev/null || true
}

do_mark() {
	m="ramoops-mark $(date -u +%Y%m%dT%H%M%SZ) boot $(cat /proc/sys/kernel/random/boot_id)"
	wr "$m" /dev/kmsg
	run sync
	say "wrote: $m"
	say "now reboot (or force-restart), then: sudo grep -r 'ramoops-mark' /sys/fs/pstore /var/lib/systemd/pstore"
}

cmd=${1:-check}
[ $# -gt 0 ] && shift
case $cmd in
check) do_check ;;
report) do_report "$@" ;;
mark) need_root; do_mark ;;
pmtest) need_root; [ $# -gt 0 ] || set -- freezer devices platform; detach pmtest "$@" ;;
s2idle|deep) need_root; detach "$cmd" "$@" ;;
__pmtest) [ "$dry" = 1 ] || [ -x "$SLEEP_BIN" ] || die "$SLEEP_BIN is missing"; run_pmtest "$@" ;;
__s2idle) [ "$dry" = 1 ] || [ -x "$SLEEP_BIN" ] || die "$SLEEP_BIN is missing"; run_sleep s2idle "$@" ;;
__deep) [ "$dry" = 1 ] || [ -x "$SLEEP_BIN" ] || die "$SLEEP_BIN is missing"; run_sleep deep "$@" ;;
*) die "unknown subcommand $cmd (check, pmtest, s2idle, deep, mark, report)" ;;
esac
