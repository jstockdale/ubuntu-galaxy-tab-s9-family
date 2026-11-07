#!/bin/sh
# gts9u-suspend-cycles.sh - unattended suspend/resume reliability runs on the
# SM-X710 (gts9wifi): N cycles of s2idle, then N of deep, each woken by the
# PMIC RTC alarm and each followed by the same checklist.  The run stops at
# the first cycle that fails.
#
#   sudo sh ~/suspendkit/gts9u-suspend-cycles.sh run                20 s2idle, then 20 deep
#   sudo sh ~/suspendkit/gts9u-suspend-cycles.sh run --s2idle 5 --deep 0
#   sudo sh ~/suspendkit/gts9u-suspend-cycles.sh run --release-icc  release gem_noc + mc_virt first
#   sudo sh ~/suspendkit/gts9u-suspend-cycles.sh key power          one sleep, woken by Power
#   sudo sh ~/suspendkit/gts9u-suspend-cycles.sh key voldown        one sleep, woken by Volume Down
#   sudo sh ~/suspendkit/gts9u-suspend-cycles.sh rtc-check          RTC alarm while awake (25 s)
#   sh ~/suspendkit/gts9u-suspend-cycles.sh check                   preflight, never sleeps
#   sh ~/suspendkit/gts9u-suspend-cycles.sh status                  progress and summary
#   sudo sh ~/suspendkit/gts9u-suspend-cycles.sh stop               end after the current cycle
#
# run and key detach into the transient unit gts9u-suspendkit.service, so a
# USB link that drops while the gadget sleeps, or a closed SSH session, cannot
# cut a run short.  `status` needs no root: the logs are root:adm under
# /var/log/gts9u-suspendkit/, one directory per run (latest -> the newest),
# synced after every line, so a cycle that never comes back still leaves
# everything up to the moment it went to sleep.  After a forced restart,
# `status` names the cycle that did not return.
#
# How a cycle sleeps.  The same way systemd-suspend.service would, minus
# logind: units that conflict with suspend.target are stopped first (the
# sensor protection domain, as the suspend.target transaction does), the RTC
# alarm is armed with `rtcwake -m no -s SECS`, and /usr/lib/systemd/
# systemd-sleep runs, so every /usr/lib/systemd/system-sleep hook (USB gadget,
# sensors, flashlight, sleep statistics) runs around the transition.  This
# works with sleep.target and suspend.target masked and with the suspend-mode
# switch's AllowSuspend=no: for the length of one cycle only, a later-sorting
# drop-in in /run/systemd/sleep.conf.d sets AllowSuspend=yes and
# SuspendState=mem, so /sys/power/mem_sleep alone picks s2idle or deep.  It is
# removed when systemd-sleep returns, also on failure, and with it /run
# (a reboot) clears it anyway.  Masks and the policy drop-in are never
# touched.  What this path lacks is logind's PrepareForSleep signal:
# NetworkManager and GNOME are not told, so the screen is not locked first and
# Wi-Fi comes back through the driver alone.
#
# After every resume, one line per cycle (cycles.tsv and run.log):
#   systemd-sleep's return code, the mode the kernel entered ("PM: suspend
#   entry (...)"), /sys/power/pm_wakeup_irq and its name (pm8xxx_rtc_alarm,
#   IRQ 21 on the X710 now that the PMIC RTC is registered; pmic_pwrkey 22,
#   pmic_resin 23), the time asleep, device resume time, the qcom_stats
#   deltas of aosd (AOSS sleep), cxsd (CX collapse) and ddr (DDR low power),
#   suspend_stats success/fail deltas, Wi-Fi reconnected (only required when
#   it was connected at the start), the panel id the DDIC answered, the root
#   filesystem still rw, and failure signatures in the kernel log since the
#   cycle's marker.
#
# --release-icc first writes 1 to state_synced of gem_noc (24100000) and
# mc_virt (interconnect-1), which the Stage 2 kernel holds at INT_MAX because
# the bandwidth monitor is kept out with initcall_blacklist.  Only then do
# aosd/cxsd/ddr have a chance to count, so this measures sleep depth.  It
# cannot be undone short of a reboot, and an interconnect synced before the
# boot-time cycle hung that cycle (Stage 3), so expect a possible wedge and a
# forced restart (hold Power + Volume Down); the next boot is plain Stage 2.
set -u
umask 027

TAG=gts9u-suspendkit
UNIT=gts9u-suspendkit
BASE=/var/log/gts9u-suspendkit
POWER=/sys/power
STATS=/sys/kernel/debug/qcom_stats
ICC_SUMMARY=/sys/kernel/debug/interconnect/interconnect_summary
RTC=rtc0
RTC_SYS=/sys/class/rtc/rtc0
SLEEP_BIN=/usr/lib/systemd/systemd-sleep
DROPIN_DIR=/run/systemd/sleep.conf.d
DROPIN=$DROPIN_DIR/zz-gts9u-suspendkit.conf
STOPFILE=/run/gts9u-suspendkit.stop
LOCK=/run/gts9u-suspendkit.lock
WIFI_IF=wlp1s0
ICC_GEM_NOC=/sys/devices/platform/soc@0/24100000.interconnect
ICC_MC_VIRT=/sys/devices/platform/interconnect-1
COLDBOOT=ubuntu-gts9u-panel-coldboot-recover.service
ADSP_INHIBIT=ubuntu-gts9u-adsp-start-inhibit.service
SENSORS_RESUME=ubuntu-gts9u-sensors-resume.service
RTC_IRQ_NAME=pm8xxx_rtc_alarm
PWRKEY_IRQ_NAME=pmic_pwrkey
RESIN_IRQ_NAME=pmic_resin

self=$(readlink -f "$0")
log=
tsv=
rundir=
stop_requested=0

# ------------------------------------------------------------------ output ---
say() {
	line="$(date '+%F %T') up=$(cut -d' ' -f1 /proc/uptime 2>/dev/null) $*"
	if [ -n "$log" ]; then
		printf '%s\n' "$line" >> "$log"
		sync "$log" 2>/dev/null || :
	fi
	printf '%s\n' "$line" 2>/dev/null || :
}

usage() {
	sed -n '2,/end after the current cycle$/{s/^# \{0,1\}//;p}' "$self"
	cat <<'EOF'

run options:
  --s2idle N          s2idle cycles (default 20; 0 skips them)
  --deep N            deep cycles, after the s2idle ones (default 20; 0 skips them)
  --sleep SECS        RTC alarm per cycle (default 30)
  --gap SECS          awake time after one cycle's checks, before the next sleep (default 30)
  --settle SECS       wait after resume before the checks (default 5)
  --wifi-wait SECS    how long Wi-Fi may take to reconnect (default 45)
  --sensors-wait SECS before each sleep, wait at most this long for the sensor
                      recovery of the last resume to finish (default 120; 0: never)
  --release-icc       release gem_noc and mc_virt first (irreversible until reboot)
  --icc-watch SECS    awake watch after that release, before the first cycle (default 120)
  --no-rtc-check      skip the awake RTC alarm test before the first cycle
  --no-conflicts      do not stop the units that conflict with suspend.target
  --print-times       pm_print_times=1 for the run (per-device callback times)
  --keep-going        record a failed cycle and go on (still stops on a read-only root)
  --foreground        do not detach into gts9u-suspendkit.service
  --any-board         run on a board other than the SM-X710 too
key options: power|voldown, then --mode s2idle|deep (default s2idle), --timeout SECS
  (RTC backup alarm, default 90), --wifi-wait, --settle, --sensors-wait, --foreground
EOF
}

die() {
	say "ABORT: $*"
	[ -n "$rundir" ] && : > "$rundir/ABORTED"
	exit 1
}

need_root() {
	[ "$(id -u)" = 0 ] || { echo "run this with sudo: sudo sh $self $*" >&2; exit 1; }
}

is_x710() {
	tr '\0' '\n' < /proc/device-tree/compatible 2>/dev/null | grep -qx 'samsung,gts9wifi'
}

# The bracketed choice of a sysfs list such as mem_sleep (a lone word as is).
selected() {
	v=$(cat "$1" 2>/dev/null) || return 0
	case "$v" in
	*'['*) printf '%s\n' "$v" | sed -n 's/.*\[\([a-z0-9]*\)\].*/\1/p' ;;
	*) printf '%s\n' "$v" | awk 'NF == 1 {print $1}' ;;
	esac
}

# pause SECS: sleep, but give up early once a stop has been asked for.
# (Every sh variable is global: helpers use names no caller uses.)
pause() {
	_pn=0
	while [ "$_pn" -lt "$1" ]; do
		stop_check && return 0
		sleep 1
		_pn=$((_pn + 1))
	done
}

stop_check() {
	[ -e "$STOPFILE" ] && stop_requested=1
	[ "$stop_requested" = 1 ]
}

# ------------------------------------------------------------- readings ---
uptime_s() { cut -d. -f1 /proc/uptime 2>/dev/null || echo 0; }

ncpu_cols() { head -n 1 /proc/interrupts | wc -w; }

# irq_num NAME: the Linux IRQ number of the action NAME in /proc/interrupts.
irq_num() {
	awk -v n="$1" '$1 ~ /^[0-9]+:$/ && $NF == n {sub(":", "", $1); print $1; exit}' /proc/interrupts 2>/dev/null
}

# irq_count NUM: that IRQ's count, summed over every CPU.
irq_count() {
	[ -n "$1" ] || { echo 0; return; }
	awk -v q="$1:" -v c="$(ncpu_cols)" '$1 == q {s = 0; for (i = 2; i <= c + 1; i++) s += $i; print s; f = 1; exit} END {if (!f) print 0}' /proc/interrupts 2>/dev/null
}

# irq_name NUM: the action name(s) of that IRQ ("?" when unknown).
irq_name() {
	case "$1" in ''|none) echo none; return ;; esac
	awk -v q="$1:" '$1 == q {line = $0; if (sub(/.*(Edge|Level)[ \t]+/, "", line)) print line; else print $NF; f = 1; exit} END {if (!f) print "?"}' /proc/interrupts 2>/dev/null
}

wake_irq() { cat "$POWER/pm_wakeup_irq" 2>/dev/null || echo none; }

ssv() { cat "$POWER/suspend_stats/$1" 2>/dev/null || echo 0; }

# "aosd COUNT TICKS cxsd COUNT TICKS ddr COUNT TICKS" ("-" when unreadable).
stats_line() {
	out=
	for s in aosd cxsd ddr; do
		if [ -r "$STATS/$s" ]; then
			v=$(awk '/^Count:/ {c = $2} /^Accumulated Duration:/ {d = $3} END {if (c != "") print c, d; else print "- -"}' "$STATS/$s" 2>/dev/null)
		else
			v='- -'
		fi
		out="$out $s ${v:-- -}"
	done
	echo "${out# }"
}

# qd NAME PRE POST: "COUNT SECONDS" between two stats_line readings.  The
# AOP counts in ticks of the 19.2 MHz architected timer.
qd() {
	printf '%s\n%s\n' "$2" "$3" | awk -v n="$1" '
		{ for (i = 1; i + 2 <= NF; i += 3) if ($i == n) { c[NR] = $(i + 1); d[NR] = $(i + 2) } }
		END {
			if (c[1] == "" || c[2] == "" || c[1] == "-" || c[2] == "-") print "- -"
			else printf "%d %.1f\n", c[2] - c[1], (d[2] - d[1]) / 19200000
		}'
}

s2idle_usage() {
	cat /sys/devices/system/cpu/cpu[0-9]*/cpuidle/state*/s2idle/usage 2>/dev/null | awk '{s += $1} END {print s + 0}'
}

intmax() {
	# grep -c prints 0 and fails when nothing matches: no "|| echo" here.
	if [ -r "$ICC_SUMMARY" ]; then
		grep -c 2147483647 "$ICC_SUMMARY"
	else
		echo n/a
	fi
}

icc_line() {
	echo "gem_noc=$(cat "$ICC_GEM_NOC/state_synced" 2>/dev/null || echo n/a) mc_virt=$(cat "$ICC_MC_VIRT/state_synced" 2>/dev/null || echo n/a)"
}

wifi_state() {
	if command -v nmcli >/dev/null 2>&1; then
		nmcli -t -f DEVICE,STATE device 2>/dev/null |
			awk -F: -v d="$WIFI_IF" '$1 == d {print $2; f = 1} END {if (!f) print "absent"}'
	elif [ -r "/sys/class/net/$WIFI_IF/operstate" ]; then
		cat "/sys/class/net/$WIFI_IF/operstate"
	else
		echo absent
	fi
}

wifi_ok() { case "$1" in connected|up) return 0 ;; esac; return 1; }

panel_id() {
	for f in /sys/class/backlight/*/panel_id; do
		[ -e "$f" ] || continue
		v=$(cat "$f" 2>/dev/null) || v=unread
		echo "${v:-unread}"
		return
	done
	echo n/a
}

root_mode() { findmnt -no OPTIONS / 2>/dev/null | cut -d, -f1; }

adsp_state() {
	for d in /sys/class/remoteproc/remoteproc*; do
		[ "$(cat "$d/name" 2>/dev/null)" = adsp ] || continue
		cat "$d/state" 2>/dev/null
		return
	done
	echo absent
}

unit_state() { systemctl show -p ActiveState --value "$1" 2>/dev/null; }

# The kernel log after the line holding MARKER.
kmsg_since() {
	dmesg 2>/dev/null | awk -v m="$1" 'f; index($0, m) {f = 1}'
}

# ---------------------------------------------------- failure signatures ---
# Fatal in a cycle's kernel log.  Case-insensitive ones first, then the
# kernel's own splat markers, which must match case-sensitively ("BUG:" would
# match "debug:").  "dwc3-qcom ... HS-PHY not in L2" is normal on every
# suspend of this board and matches none of these.
STRICT_I='Freezing of tasks failed|Freezing (user space|remaining freezable) .*failed|failed to (suspend|resume)|PM: dpm_run_callback\(\)|PM: Device [^ ]+ failed|of devices (aborted|failed)|PM: Some devices failed|Wakeup pending, aborting|resume .*returns -|returned -[0-9]+ after|I/O error|EXT4-fs (error|warning)|Remounting filesystem read-only|blk_update_request|ufshcd.*(abort|error|fail|timeout|timed out)|ufs_qcom.*(err|fail)|AER: (Uncorrect|Multiple Uncorrect)|severity=(Uncorrect|Fatal)|ath11k.*(fail|error|timeout|timed out|crash)|qcom-pcie.*(fail|error)|link never came up|mhi .*(fail|error|timeout)|dwc3.*(fail|error)|xhci.*(died|fail|error)|gmu.*(timeout|timed out|fail)|hfi.*(timeout|timed out)|hangcheck|gpu fault|Unhandled context fault|smmu.*fault|gdsc.*(stuck|timeout|fail)|rpmh.*(timeout|fail|error)|tcs.*(timeout|busy)|qcom-apm.*timeout|watchdog received|fatal error received|remoteproc.*crash|q6v5 .*(fatal|crash)|rcu.*stall|soft lockup|hard lockup|hung_task|blocked for more than|panel id 00 00 00|did not answer'
STRICT_C='Oops|BUG:|WARNING:|Internal error|Unable to handle|Call trace:|cut here'
# Logged, not fatal.  The GMU answering one GPU bandwidth vote late (and the
# matching "Unexpected message id") is a known intermittent of this port with
# nothing released (docs/gpu-recovery.md); a correctable AER on the Wi-Fi link
# also shows up without any suspend.
NOTED='HFI_H2F_MSG_GX_BW_PERF_VOTE|Unexpected message id|AER: Correctable|severity=Correctable'
# The detail lines under every AER report carry the endpoint's driver name
# but no severity, for example (tablet, 2026-11-07, no suspend involved):
#   ath11k_pci 0000:01:00.0:   device [17cb:1103] error status/mask=00001000/0000e000
#   ath11k_pci 0000:01:00.0:    [12] Timeout
# so they would match "ath11k.*(error|timeout)".  The report's own
# "severity=" line decides (Uncorrectable/Fatal are in STRICT_I), so these are
# neither fatal nor counted.
AER_DETAIL='error status/mask=|: {3,}\[ ?[0-9]+\] '

# scan FILE: the fatal lines of a kernel log excerpt, at most 20.
scan() {
	{
		grep -vE "$NOTED|$AER_DETAIL" "$1" | grep -E -i "$STRICT_I"
		grep -vE "$NOTED|$AER_DETAIL" "$1" | grep -E "$STRICT_C"
	} 2>/dev/null | sort -u | head -n 20
}

# --------------------------------------------------------- sleep plumbing ---
write_dropin() {
	mkdir -p "$DROPIN_DIR" || return 1
	printf '%s\n' "# $TAG ($self): one test cycle only; removed when it returns." \
		'[Sleep]' 'AllowSuspend=yes' 'SuspendState=mem' > "$DROPIN"
}

remove_dropin() { rm -f "$DROPIN"; }

rtc_disarm() { rtcwake -d "$RTC" -m disable >/dev/null 2>&1 || :; }

# Stop what the suspend.target transaction would stop.  Prints the names.
stop_conflicts() {
	for u in $(systemctl show -p ConflictedBy --value sleep.target suspend.target 2>/dev/null | tr ' ' '\n' | sort -u); do
		case "$(unit_state "$u")" in
		active|activating|reloading)
			systemctl stop "$u" >/dev/null 2>&1 || :
			printf ' %s' "$u"
			;;
		esac
	done
}

# Put back what the run changed in sysfs and /run; safe to call twice.
restore() {
	remove_dropin
	rtc_disarm
	if [ -n "${mem_default:-}" ] && [ "$(selected "$POWER/mem_sleep")" != "$mem_default" ]; then
		printf '%s' "$mem_default" > "$POWER/mem_sleep" 2>/dev/null || :
	fi
	[ -z "${debug_was:-}" ] || printf '%s' "$debug_was" > "$POWER/pm_debug_messages" 2>/dev/null || :
	[ -z "${times_was:-}" ] || printf '%s' "$times_was" > "$POWER/pm_print_times" 2>/dev/null || :
}

# A unit stop_conflicts stopped comes back through the sensors hook after
# each resume.  If systemd-sleep refused before running its hooks, nothing
# would start it again, so at the end of a run do what that hook does.
restart_stopped() {
	[ -s "$rundir/stopped-units" ] || return 0
	for u in $(sort -u "$rundir/stopped-units"); do
		[ "$(unit_state "$u")" = inactive ] || continue
		case "$(unit_state "$SENSORS_RESUME")" in
		active|activating) continue ;;
		esac
		if [ "$u" = hexagonrpcd-adsp-sensorspd.service ]; then
			systemctl restart --no-block "$SENSORS_RESUME" >/dev/null 2>&1 || :
			say "restarted $SENSORS_RESUME, which brings $u back (as the sensors sleep hook does)"
		else
			systemctl start --no-block "$u" >/dev/null 2>&1 || :
			say "started $u again"
		fi
	done
}

# Before a sleep, let the sensor recovery that the last resume (or the boot)
# started run to its end, at most $sensors_wait seconds.  After a resume the
# sensors hook restarts ubuntu-gts9u-sensors-resume, which attaches the
# sensor protection domain, waits for SSC and watches the handover IRQ for up
# to a minute; with the default gap the next cycle would stop the domain in
# the middle of that, 40 times a run.  An ADSP crash caused by that churn
# would end a run as a suspend failure.  Never fatal: a recovery that ends
# badly is logged, and one that is still running at the limit is left to the
# sensors hook, as a real suspend would leave it.
wait_sensors() {
	[ "$sensors_wait" -gt 0 ] || return 0
	_sw=0
	while :; do
		case "$(unit_state "$SENSORS_RESUME")" in
		activating|deactivating|reloading) ;;
		*) break ;;
		esac
		stop_check && return 0
		if [ "$_sw" -ge "$sensors_wait" ]; then
			say "note: $SENSORS_RESUME is still running after ${_sw}s; sleeping anyway"
			return 0
		fi
		sleep 1
		_sw=$((_sw + 1))
	done
	[ "$_sw" = 0 ] || say "waited ${_sw}s for $SENSORS_RESUME to finish"
	_sr=$(systemctl show -p Result --value "$SENSORS_RESUME" 2>/dev/null)
	case "$_sr" in
	''|success) ;;
	*) say "note: $SENSORS_RESUME ended with result '$_sr': the sensors may not have come back (journalctl -u $SENSORS_RESUME)" ;;
	esac
}

# ------------------------------------------------------------ RTC check ---
# Arm the alarm while awake: it must add exactly one pm8xxx_rtc_alarm
# interrupt and clear wakealarm.  A run whose RTC cannot wake the tablet
# would leave it asleep until a key press.
rtc_check() {
	secs=${1:-20}
	n=$(irq_num "$RTC_IRQ_NAME")
	[ -n "$n" ] || { say "rtc-check: no $RTC_IRQ_NAME in /proc/interrupts"; return 1; }
	w=$(cat "$RTC_SYS/device/power/wakeup" 2>/dev/null)
	[ "$w" = enabled ] || { say "rtc-check: $RTC wakeup is '${w:-missing}', not enabled"; return 1; }
	rtc_disarm
	c0=$(irq_count "$n")
	if ! out=$(rtcwake -d "$RTC" -m no -s "$secs" 2>&1); then
		say "rtc-check: rtcwake failed: $out"
		return 1
	fi
	a=$(cat "$RTC_SYS/wakealarm" 2>/dev/null)
	[ -n "$a" ] || { say "rtc-check: wakealarm is empty right after rtcwake -m no -s $secs"; return 1; }
	say "rtc-check: alarm armed for ${secs}s (IRQ $n $RTC_IRQ_NAME count $c0); waiting $((secs + 5))s awake"
	pause $((secs + 5))
	stop_check && { rtc_disarm; say 'rtc-check: stopped by request'; return 1; }
	c1=$(irq_count "$n")
	a=$(cat "$RTC_SYS/wakealarm" 2>/dev/null)
	if [ $((c1 - c0)) -ge 1 ] && [ -z "$a" ]; then
		say "rtc-check: ok, IRQ $n $RTC_IRQ_NAME $c0 -> $c1, wakealarm cleared"
		return 0
	fi
	rtc_disarm
	say "rtc-check: FAILED, IRQ $n $RTC_IRQ_NAME $c0 -> $c1, wakealarm '${a}'"
	return 1
}

# ---------------------------------------------------- interconnect release ---
release_icc() {
	before=$(intmax)
	pending=
	for p in "$ICC_GEM_NOC" "$ICC_MC_VIRT"; do
		[ -e "$p/state_synced" ] || die "icc: $p/state_synced does not exist"
		[ "$(cat "$p/state_synced" 2>/dev/null)" = 1 ] || pending="$pending $p"
	done
	if [ -z "$pending" ]; then
		say "icc: gem_noc and mc_virt are already synced; INT_MAX lines $before"
		return 0
	fi
	marker="$TAG $runid icc-release"
	echo "$marker" >> /dev/kmsg
	say "icc: writing 1 to$(for p in $pending; do printf ' %s' "${p##*/}"; done)/state_synced (INT_MAX lines $before); this holds until reboot"
	sync
	for p in $pending; do
		# printf, not echo: state_synced_store() rejects "1\n".
		printf 1 > "$p/state_synced" || die "icc: the write to ${p##*/}/state_synced failed"
		[ "$(cat "$p/state_synced" 2>/dev/null)" = 1 ] || die "icc: ${p##*/} still reads $(cat "$p/state_synced" 2>/dev/null)"
	done
	say "icc: released; $(icc_line); INT_MAX lines now $(intmax) (target 0); watching ${icc_watch}s awake"
	cp "$ICC_SUMMARY" "$rundir/icc-after-release.txt" 2>/dev/null || :
	t=0
	while [ "$t" -lt "$icc_watch" ]; do
		pause 10
		t=$((t + 10))
		stop_check && die 'stopped by request during the interconnect watch'
		kmsg_since "$marker" > "$rundir/icc-kmsg.txt"
		hits=$(scan "$rundir/icc-kmsg.txt")
		if [ -n "$hits" ]; then
			printf '%s\n' "$hits" | while IFS= read -r l; do say "  kmsg: $l"; done
			die 'icc: failure signature in the kernel log after the release (nothing was suspended)'
		fi
	done
	[ "$(root_mode)" = rw ] || die "icc: the root filesystem is $(root_mode) after the release"
	say "icc: ${icc_watch}s after the release clean; Wi-Fi $(wifi_state), panel $(panel_id)"
}

# ----------------------------------------------------------------- cycle ---
# cycle MODE ID SECS EXPECT: one sleep and its checklist.  Status 0 if it
# passed.  EXPECT is the IRQ action that must wake it.
cycle() {
	mode=$1 id=$2 secs=$3 expect=$4
	reasons=
	fail() { reasons="${reasons}${reasons:+; }$*"; }
	# Nothing slept: one FAIL row with the reason, so the table stays complete.
	not_started() {
		printf '%s\t%s\tFAIL\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t%s\n' "$id" "$mode" "not started: $*" >> "$tsv"
		say "cycle $id: FAILED before sleeping: $*"
		return 1
	}

	pre_q=$(stats_line)
	pre_ok=$(ssv success)
	pre_fail=$(ssv fail)
	pre_s2=$(s2idle_usage)
	if [ "$(selected "$POWER/mem_sleep")" != "$mode" ]; then
		printf '%s' "$mode" > "$POWER/mem_sleep" 2>/dev/null || :
	fi
	[ "$(selected "$POWER/mem_sleep")" = "$mode" ] ||
		{ not_started "cannot select $mode in $POWER/mem_sleep ($(cat "$POWER/mem_sleep" 2>/dev/null))"; return 1; }
	marker="$TAG $runid cycle $id"
	echo "$marker" >> /dev/kmsg
	stopped=
	[ "$do_conflicts" = 1 ] && stopped=$(stop_conflicts)
	[ -z "$stopped" ] || printf '%s\n' $stopped >> "$rundir/stopped-units"
	rtc_disarm
	if ! out=$(rtcwake -d "$RTC" -m no -s "$secs" 2>&1); then
		not_started "rtcwake failed: $out"
		return 1
	fi
	[ -n "$(cat "$RTC_SYS/wakealarm" 2>/dev/null)" ] ||
		{ not_started 'the RTC alarm did not arm (wakealarm empty)'; return 1; }
	write_dropin || { rtc_disarm; not_started "cannot write $DROPIN"; return 1; }
	case "$expect" in
	"$PWRKEY_IRQ_NAME") hint="press Power once while the panel is dark (backup RTC alarm in ${secs}s)" ;;
	"$RESIN_IRQ_NAME") hint="press Volume Down once while the panel is dark (backup RTC alarm in ${secs}s)" ;;
	*) hint="RTC alarm in ${secs}s" ;;
	esac
	echo "$id" > "$rundir/IN-CYCLE"
	say "cycle $id: $mode sleep starts, $hint${stopped:+; stopped$stopped}"
	sync
	rc=0
	"$SLEEP_BIN" suspend > "$rundir/$id-systemd-sleep.txt" 2>&1 || rc=$?
	remove_dropin
	alarm_left=$(cat "$RTC_SYS/wakealarm" 2>/dev/null)
	[ -z "$alarm_left" ] || rtc_disarm
	wirq=$(wake_irq)
	rm -f "$rundir/IN-CYCLE"
	say "cycle $id: systemd-sleep returned $rc; checking"
	# Plain sleep, not pause: a stop request ends the run after this
	# cycle's checks, and must not cut them short into a false failure.
	[ "$settle" -gt 0 ] && sleep "$settle"

	kmsg_since "$marker" > "$rundir/$id-kmsg.txt"
	k=$rundir/$id-kmsg.txt
	entered=$(sed -n 's/.*PM: suspend entry (\([a-z0-9]*\)).*/\1/p' "$k" | tail -n 1)
	exits=$(grep -c 'PM: suspend exit' "$k")
	# s2idle prints one line per idle round (a non-wake IRQ that brings the
	# CPUs back is followed by another round): the time asleep is their sum.
	slept=$(sed -n 's/.*Timekeeping suspended for \([0-9.]*\) seconds.*/\1/p' "$k" |
		awk '{s += $1; n++} END {if (n) printf "%.3f\n", s}')
	resume_ms=$(sed -n 's/.*PM: resume of devices complete after \([0-9.]*\) msecs.*/\1/p' "$k" | tail -n 1)
	wname=$(irq_name "$wirq")
	post_q=$(stats_line)
	d_ok=$(( $(ssv success) - pre_ok ))
	d_fail=$(( $(ssv fail) - pre_fail ))
	d_s2=$(( $(s2idle_usage) - pre_s2 ))
	set -- $(qd aosd "$pre_q" "$post_q") $(qd cxsd "$pre_q" "$post_q") $(qd ddr "$pre_q" "$post_q")
	aosd_n=$1 aosd_s=$2 cxsd_n=$3 cxsd_s=$4 ddr_n=$5 ddr_s=$6

	[ "$rc" = 0 ] || fail "systemd-sleep returned $rc ($(tail -n 1 "$rundir/$id-systemd-sleep.txt" 2>/dev/null))"
	[ "$entered" = "$mode" ] || fail "the kernel entered '${entered:-nothing}', not $mode"
	[ "$exits" -ge 1 ] || fail "no 'PM: suspend exit' in the kernel log"
	[ "$d_ok" = 1 ] || fail "suspend_stats success +$d_ok"
	[ "$d_fail" = 0 ] || fail "suspend_stats fail +$d_fail (last_failed_dev $(ssv last_failed_dev), step $(ssv last_failed_step), errno $(ssv last_failed_errno))"
	case " $wname " in
	*" $expect "*) ;;
	*) fail "woken by IRQ $wirq ($wname), not $expect" ;;
	esac
	if [ "$expect" = "$RTC_IRQ_NAME" ] && [ -n "$alarm_left" ]; then
		fail "the RTC alarm had not fired yet"
	fi

	# Wi-Fi only has to come back if it was connected when the run started.
	ws=$(wifi_state)
	wt=0
	if [ "$wifi_expected" = 1 ]; then
		while ! wifi_ok "$ws" && [ "$wt" -lt "$wifi_wait" ]; do
			sleep 1
			wt=$((wt + 1))
			ws=$(wifi_state)
		done
		wifi_ok "$ws" || fail "Wi-Fi is '$ws' ${wt}s after resume"
	fi
	pnl=$(panel_id)
	[ "$pnl" != '00 00 00' ] || fail 'the panel DDIC answered 00 00 00'
	root=$(root_mode)
	[ "$root" = rw ] || fail "the root filesystem is '$root'"
	hits=$(scan "$k")
	if [ -n "$hits" ]; then
		printf '%s\n' "$hits" > "$rundir/$id-FAILURE.txt"
		fail "kernel log: $(printf '%s\n' "$hits" | head -n 1 | sed 's/^\[[^]]*\] //' | cut -c1-160) (all in $id-FAILURE.txt)"
	fi
	noted=$(grep -cE "$NOTED" "$k")
	panel_lines=$(grep -c 'ana38407 panel id' "$k")

	result=PASS
	[ -z "$reasons" ] || result=FAIL
	printf '%s\n' "$id	$mode	$result	$rc	${entered:--}	$wirq	$wname	${slept:--}	${resume_ms:--}	$aosd_n	$aosd_s	$cxsd_n	$cxsd_s	$ddr_n	$ddr_s	$d_s2	$d_ok	$d_fail	$ws	$wt	$pnl	$root	${reasons:--}" >> "$tsv"
	sync "$tsv" 2>/dev/null || :
	say "cycle $id: $result entered=${entered:--} wake=$wirq($wname) slept=${slept:-?}s resume=${resume_ms:-?}ms aosd+$aosd_n(${aosd_s}s) cxsd+$cxsd_n(${cxsd_s}s) ddr+$ddr_n(${ddr_s}s) s2idle_usage+$d_s2 ok+$d_ok fail+$d_fail wifi=$ws(${wt}s) panel='$pnl'(+$panel_lines) root=$root noted=$noted"
	[ -z "$reasons" ] || say "cycle $id: FAILED: $reasons"
	[ -z "$reasons" ]
}

# ---------------------------------------------------------------- summary ---
summary() {
	[ "$(wc -l < "$tsv")" -gt 1 ] || { say 'summary: no cycle ran'; return; }
	awk -F'\t' 'NR > 1 {
		m = $2
		if (!(m in n)) name[++k] = m
		n[m]++
		if ($3 == "PASS") p[m]++
		if ($6 != "-") { wn = $6 "(" $7 ")"; gsub(/ /, "_", wn); if (!((m, wn) in h)) wl[m] = wl[m] " " wn; h[m, wn]++ }
		if ($8 != "-") { sl[m] += $8; sc[m]++ }
		if ($10 != "-") { an[m] += $10; as[m] += $11; cn[m] += $12; cs[m] += $13; dn[m] += $14; ds[m] += $15; q[m] = 1 }
		if ($3 == "FAIL" && first == "") first = $1 ": " $23
	} END {
		for (i = 1; i <= k; i++) {
			m = name[i]
			hl = ""
			c = split(wl[m], ws, " ")
			for (j = 1; j <= c; j++) hl = hl " " ws[j] "x" h[m, ws[j]]
			printf "summary %s: %d/%d passed; woken by:%s; asleep %.0f s over %d cycles", m, p[m], n[m], (hl == "" ? " -" : hl), sl[m], sc[m]
			if (q[m]) {
				printf "; aosd +%d (%.1f s) cxsd +%d (%.1f s) ddr +%d (%.1f s)", an[m], as[m], cn[m], cs[m], dn[m], ds[m]
				if (sl[m] > 0) printf ", CX collapsed %.0f%% of the time asleep", 100 * cs[m] / sl[m]
			} else printf "; qcom_stats unreadable"
			printf "\n"
		}
		if (first != "") printf "summary: first failure %s\n", first
	}' "$tsv" > "$rundir/SUMMARY.txt"
	while IFS= read -r l; do say "$l"; done < "$rundir/SUMMARY.txt"
}

# ----------------------------------------------------------------- checks ---
preflight() {
	is_x710 || [ "$any_board" = 1 ] || die 'this is not an SM-X710 (samsung,gts9wifi); --any-board to run anyway'
	[ -x "$SLEEP_BIN" ] || die "$SLEEP_BIN is missing"
	command -v rtcwake >/dev/null 2>&1 || die 'rtcwake is missing (util-linux)'
	[ -e "$RTC_SYS/wakealarm" ] || die "$RTC_SYS/wakealarm is missing (no PMIC RTC on this kernel)"
	up=$(uptime_s)
	[ "$up" -ge 90 ] || die "uptime ${up}s: wait until the boot has settled (90 s)"
	case "$(unit_state "$COLDBOOT")" in
	activating|deactivating) die "$COLDBOOT is still running" ;;
	esac
	case "$(unit_state "$ADSP_INHIBIT")" in
	active|activating) die "$ADSP_INHIBIT still holds sleep off while the audio DSP starts" ;;
	esac
	a=$(adsp_state)
	case "$a" in
	running|absent) ;;
	*) die "the audio DSP is '$a', not running; a suspend during its start timed out APM before" ;;
	esac
	[ "$a" = absent ] || [ -e /dev/fastrpc-adsp ] || die 'the ADSP runs but /dev/fastrpc-adsp is missing: its start has not finished'
	[ "$(unit_state gts9u-stage0.service)" != active ] || die 'Stage 0 (gts9u-stage0.service) is running'
	[ "$(root_mode)" = rw ] || die "the root filesystem is $(root_mode)"
	for m in $modes; do
		grep -qw "$m" "$POWER/mem_sleep" || die "$POWER/mem_sleep offers no $m ($(cat "$POWER/mem_sleep"))"
	done
	[ "$(cat "$POWER/pm_test" 2>/dev/null | sed -n 's/.*\[\([a-z]*\)\].*/\1/p')" = none ] ||
		die "$POWER/pm_test is not none: $(cat "$POWER/pm_test")"
	if [ -e "$DROPIN" ]; then
		say "note: removing $DROPIN left by an interrupted run"
		remove_dropin
	fi
	[ -r "$STATS/aosd" ] || say "note: $STATS is not readable; the aosd/cxsd/ddr columns stay '-'"
}

describe() {
	say "boot $(cat /proc/sys/kernel/random/boot_id 2>/dev/null), kernel $(uname -r), up $(uptime_s)s"
	say "cmdline extras: $(tr ' ' '\n' < /proc/cmdline 2>/dev/null | grep -E '^(initcall_blacklist|icc_core|mem_sleep_default|clk_ignore_unused|pd_ignore_unused|regulator_ignore_unused)' | tr '\n' ' ')"
	say "mem_sleep [$(cat "$POWER/mem_sleep" 2>/dev/null)], policy: $(cat /run/ubuntu-gts9u/suspend-mode.applied 2>/dev/null || echo '?'), sleep.target $(systemctl show -p LoadState --value sleep.target 2>/dev/null), suspend.target $(systemctl show -p LoadState --value suspend.target 2>/dev/null)"
	say "wake IRQs: $RTC_IRQ_NAME=$(irq_num "$RTC_IRQ_NAME") $PWRKEY_IRQ_NAME=$(irq_num "$PWRKEY_IRQ_NAME") $RESIN_IRQ_NAME=$(irq_num "$RESIN_IRQ_NAME"); $RTC wakeup $(cat "$RTC_SYS/device/power/wakeup" 2>/dev/null || echo '?')"
	say "interconnect: $(icc_line), INT_MAX lines $(intmax); qcom_stats: $(stats_line)"
	say "suspend_stats success=$(ssv success) fail=$(ssv fail); ADSP $(adsp_state); Wi-Fi $(wifi_state); panel '$(panel_id)'; root $(root_mode)"
}

new_rundir() {
	[ -d "$BASE" ] || mkdir -p "$BASE" || { echo "cannot create $BASE" >&2; exit 1; }
	chgrp adm "$BASE" 2>/dev/null || :
	# setgid: everything below is group adm, readable over plain SSH.
	chmod 2750 "$BASE" 2>/dev/null || :
	runid=$(date +%Y%m%d-%H%M%S)
	rundir=$BASE/$1-$runid
	mkdir -p "$rundir" || { echo "cannot create $rundir" >&2; exit 1; }
	chgrp adm "$rundir" 2>/dev/null || :
	chmod 2750 "$rundir" 2>/dev/null || :
	ln -sfn "${rundir##*/}" "$BASE/latest"
	log=$rundir/run.log
	tsv=$rundir/cycles.tsv
	: > "$log"
	printf '%s\n' 'cycle	mode	result	rc	entered	wake_irq	wake_name	slept_s	resume_ms	aosd_n	aosd_s	cxsd_n	cxsd_s	ddr_n	ddr_s	s2idle_usage	ok	fail	wifi	wifi_s	panel	root	reasons' > "$tsv"
	cat /proc/sys/kernel/random/boot_id > "$rundir/BOOT" 2>/dev/null || :
}

# ------------------------------------------------------------- detaching ---
detach() {
	if [ "$(unit_state "$UNIT.service")" = active ] || [ "$(unit_state "$UNIT.service")" = activating ]; then
		echo "a run is already active; follow it with: sh $self status" >&2
		exit 1
	fi
	systemctl reset-failed "$UNIT.service" >/dev/null 2>&1 || :
	# KillMode=mixed: a stop sends SIGTERM to this script only, which then
	# finishes the cycle in flight (systemd-sleep must run its post hooks).
	systemd-run --unit="$UNIT" --collect --quiet \
		-p KillMode=mixed -p TimeoutStopSec=900 -p SyslogIdentifier="$TAG" \
		--setenv=GTS9U_SUSPENDKIT_DETACHED=1 /bin/sh "$self" "$@" ||
		{ echo 'systemd-run failed' >&2; exit 1; }
	echo "DETACHED as $UNIT.service; follow with: sh $self status   (or: journalctl -f -t $TAG)"
}

take_lock() {
	exec 9> "$LOCK"
	flock -n 9 || { echo "another $TAG run holds $LOCK" >&2; exit 1; }
}

on_exit() {
	rc=$?
	restore
	rm -f "$STOPFILE"
	if [ -n "$rundir" ]; then
		chgrp -R adm "$rundir" 2>/dev/null || :
		if [ ! -e "$rundir/DONE" ] && [ ! -e "$rundir/FAILED" ] && [ ! -e "$rundir/ABORTED" ] && [ ! -e "$rundir/STOPPED" ]; then
			say "ended early (status $rc)"
			: > "$rundir/ABORTED"
		fi
		say 'restored: drop-in removed, RTC alarm cleared, mem_sleep and pm_debug_messages as before'
		restart_stopped
	fi
}

setup_run() {
	trap '' HUP PIPE
	trap 'stop_requested=1' TERM INT
	trap on_exit EXIT
	rm -f "$STOPFILE"
	mem_default=$(selected "$POWER/mem_sleep")
	debug_was=$(cat "$POWER/pm_debug_messages" 2>/dev/null || :)
	times_was=$(cat "$POWER/pm_print_times" 2>/dev/null || :)
	# The kernel prints "Timekeeping suspended for" and the per-phase
	# device times only with pm_debug_messages.
	printf 1 > "$POWER/pm_debug_messages" 2>/dev/null || say 'note: cannot set pm_debug_messages; slept and resume times stay empty'
	[ "$print_times" = 1 ] && { printf 1 > "$POWER/pm_print_times" 2>/dev/null || :; }
	wifi_expected=0
	wifi_ok "$(wifi_state)" && wifi_expected=1
}

# --------------------------------------------------------------- commands ---
cmd_run() {
	n_s2idle=20 n_deep=20 sleep_s=30 gap=30 settle=5 wifi_wait=45 sensors_wait=120
	release=0 icc_watch=120 rtc_pre=1 do_conflicts=1 print_times=0 keep_going=0
	foreground=0 any_board=0 rtc_secs=20
	orig="$*"
	while [ $# -gt 0 ]; do
		case $1 in
		--s2idle) n_s2idle=$2; shift ;;
		--deep) n_deep=$2; shift ;;
		--sleep) sleep_s=$2; shift ;;
		--gap) gap=$2; shift ;;
		--settle) settle=$2; shift ;;
		--wifi-wait) wifi_wait=$2; shift ;;
		--sensors-wait) sensors_wait=$2; shift ;;
		--release-icc) release=1 ;;
		--icc-watch) icc_watch=$2; shift ;;
		--no-rtc-check) rtc_pre=0 ;;
		--rtc-check-secs) rtc_secs=$2; shift ;;
		--no-conflicts) do_conflicts=0 ;;
		--print-times) print_times=1 ;;
		--keep-going) keep_going=1 ;;
		--foreground) foreground=1 ;;
		--any-board) any_board=1 ;;
		-h|--help) usage; exit 0 ;;
		*) echo "unknown option $1" >&2; usage >&2; exit 2 ;;
		esac
		shift
	done
	for v in "$n_s2idle" "$n_deep" "$sleep_s" "$gap" "$settle" "$wifi_wait" "$sensors_wait" "$icc_watch" "$rtc_secs"; do
		case "$v" in ''|*[!0-9]*) echo "not a number: '$v'" >&2; exit 2 ;; esac
	done
	[ "$sleep_s" -ge 5 ] || { echo '--sleep must be at least 5 s' >&2; exit 2; }
	need_root run
	if [ "$foreground" = 0 ] && [ "${GTS9U_SUSPENDKIT_DETACHED:-}" != 1 ]; then
		# shellcheck disable=SC2086
		detach run $orig
		exit 0
	fi
	modes=
	[ "$n_s2idle" -gt 0 ] && modes="$modes s2idle"
	[ "$n_deep" -gt 0 ] && modes="$modes deep"
	[ -n "$modes" ] || { echo 'nothing to do: --s2idle 0 and --deep 0' >&2; exit 2; }
	take_lock
	new_rundir run
	setup_run
	say "$TAG run: $n_s2idle s2idle + $n_deep deep cycles, ${sleep_s}s asleep, ${gap}s awake between, options: ${orig:-none}"
	describe
	preflight
	if [ "$release" = 1 ]; then
		release_icc
	fi
	if [ "$rtc_pre" = 1 ]; then
		rtc_check "$rtc_secs" || die 'the RTC alarm did not fire while awake: no sleep is started (fix that first)'
	fi
	stop_check && { say 'stopped by request before the first cycle'; : > "$rundir/STOPPED"; exit 3; }
	failed=0
	for m in $modes; do
		if [ "$m" = s2idle ]; then total=$n_s2idle; else total=$n_deep; fi
		i=1
		while [ "$i" -le "$total" ]; do
			wait_sensors
			if stop_check; then
				say "stopped by request before cycle $m-$i"
				summary
				: > "$rundir/STOPPED"
				exit 3
			fi
			if ! cycle "$m" "$m-$i" "$sleep_s" "$RTC_IRQ_NAME"; then
				failed=$((failed + 1))
				if [ "$keep_going" = 0 ] || [ "$(root_mode)" != rw ]; then
					summary
					: > "$rundir/FAILED"
					say "RESULT: FAIL at cycle $m-$i (stopped at the first failure); logs in $rundir"
					exit 1
				fi
			fi
			i=$((i + 1))
			[ "$m-$((i - 1))" = "$(echo "$modes" | awk '{print $NF}')-$total" ] || pause "$gap"
		done
	done
	summary
	if [ "$failed" = 0 ]; then
		: > "$rundir/DONE"
		say "RESULT: PASS, every cycle resumed and passed its checks; logs in $rundir"
	else
		: > "$rundir/FAILED"
		say "RESULT: $failed cycle(s) FAILED (--keep-going); logs in $rundir"
		exit 1
	fi
}

cmd_key() {
	key=${1:-}
	[ $# -gt 0 ] && shift
	case "$key" in
	power) expect=$PWRKEY_IRQ_NAME ;;
	voldown) expect=$RESIN_IRQ_NAME ;;
	*) echo 'usage: key power|voldown [--mode s2idle|deep] [--timeout SECS]' >&2; exit 2 ;;
	esac
	kmode=s2idle timeout=90 settle=5 wifi_wait=45 sensors_wait=120 foreground=0 any_board=0
	do_conflicts=1 print_times=0
	orig="$*"
	while [ $# -gt 0 ]; do
		case $1 in
		--mode) kmode=$2; shift ;;
		--timeout) timeout=$2; shift ;;
		--settle) settle=$2; shift ;;
		--wifi-wait) wifi_wait=$2; shift ;;
		--sensors-wait) sensors_wait=$2; shift ;;
		--foreground) foreground=1 ;;
		--any-board) any_board=1 ;;
		*) echo "unknown option $1" >&2; exit 2 ;;
		esac
		shift
	done
	case "$kmode" in s2idle|deep) ;; *) echo "--mode is s2idle or deep" >&2; exit 2 ;; esac
	for v in "$timeout" "$settle" "$wifi_wait" "$sensors_wait"; do
		case "$v" in ''|*[!0-9]*) echo "not a number: '$v'" >&2; exit 2 ;; esac
	done
	need_root key "$key"
	if [ "$foreground" = 0 ] && [ "${GTS9U_SUSPENDKIT_DETACHED:-}" != 1 ]; then
		# shellcheck disable=SC2086
		detach key "$key" $orig
		exit 0
	fi
	modes=$kmode
	take_lock
	new_rundir "key-$key"
	setup_run
	say "$TAG key: one $kmode sleep woken by $expect (IRQ $(irq_num "$expect")), RTC backup in ${timeout}s"
	describe
	preflight
	wait_sensors
	stop_check && { say 'stopped by request before the sleep'; : > "$rundir/STOPPED"; exit 3; }
	if cycle "$kmode" "$kmode-$key" "$timeout" "$expect"; then
		: > "$rundir/DONE"
		say "RESULT: PASS, $key woke the tablet from $kmode"
	else
		: > "$rundir/FAILED"
		say "RESULT: FAIL, see above; logs in $rundir"
		exit 1
	fi
}

cmd_rtc_check() {
	secs=20
	[ "${1:-}" = --secs ] && { secs=$2; shift 2; }
	case "$secs" in ''|*[!0-9]*) echo "not a number: '$secs'" >&2; exit 2 ;; esac
	need_root rtc-check
	take_lock
	# The lock proves no run is active, so a stop file is a stale one from a
	# `stop` sent while nothing ran; it would end this check at once.
	rm -f "$STOPFILE"
	trap 'stop_requested=1' TERM INT
	rtc_check "$secs"
}

cmd_stop() {
	need_root stop
	: > "$STOPFILE"
	echo "asked $TAG to stop after the current cycle; follow with: sh $self status"
}

cmd_status() {
	lines=${1:-25}
	latest=$BASE/latest
	if [ ! -e "$latest" ]; then
		echo "no run yet (nothing in $BASE)"
		return 0
	fi
	d=$(readlink -f "$latest")
	echo "run: $d"
	echo "unit $UNIT.service: $(unit_state "$UNIT.service")"
	for f in DONE FAILED STOPPED ABORTED; do
		[ -e "$d/$f" ] && echo "state: $f"
	done
	if [ -e "$d/IN-CYCLE" ]; then
		if [ "$(cat "$d/BOOT" 2>/dev/null)" != "$(cat /proc/sys/kernel/random/boot_id)" ]; then
			echo "state: cycle $(cat "$d/IN-CYCLE") NEVER RETURNED: the tablet was restarted while it slept (boot $(cat "$d/BOOT" 2>/dev/null)); the log below ends where it went to sleep"
		else
			echo "state: asleep or resuming in cycle $(cat "$d/IN-CYCLE")"
		fi
	elif [ ! -e "$d/DONE" ] && [ ! -e "$d/FAILED" ] && [ ! -e "$d/STOPPED" ] && [ ! -e "$d/ABORTED" ] &&
		[ "$(unit_state "$UNIT.service")" != active ]; then
		echo "state: not running and not finished (ended without its exit handler, e.g. a restart between cycles)"
	fi
	if [ -s "$d/cycles.tsv" ]; then
		awk -F'\t' 'NR > 1 {n++; if ($3 == "PASS") p++} END {printf "cycles: %d done, %d passed\n", n, p}' "$d/cycles.tsv"
	fi
	[ -s "$d/SUMMARY.txt" ] && cat "$d/SUMMARY.txt"
	echo "--- last $lines lines of $d/run.log"
	tail -n "$lines" "$d/run.log" 2>/dev/null || echo "(not readable: needs group adm)"
}

cmd_check() {
	any_board=1
	echo "== $TAG check (never sleeps; root shows qcom_stats and the interconnect summary)"
	echo "board: $(tr '\0' ' ' < /proc/device-tree/compatible 2>/dev/null)"
	echo "kernel $(uname -r), up $(uptime_s)s, boot $(cat /proc/sys/kernel/random/boot_id 2>/dev/null)"
	echo "mem_sleep [$(cat "$POWER/mem_sleep" 2>/dev/null)] state [$(cat "$POWER/state" 2>/dev/null)] pm_test [$(cat "$POWER/pm_test" 2>/dev/null)]"
	echo "suspend_stats success=$(ssv success) fail=$(ssv fail) last pm_wakeup_irq=$(wake_irq)"
	echo "policy: $(cat /run/ubuntu-gts9u/suspend-mode.applied 2>/dev/null || echo '?'); sleep.target $(systemctl show -p LoadState --value sleep.target 2>/dev/null); suspend.target $(systemctl show -p LoadState --value suspend.target 2>/dev/null)"
	for f in /etc/systemd/sleep.conf.d/*.conf /run/systemd/sleep.conf.d/*.conf; do
		[ -e "$f" ] || continue
		echo "  $f: $(grep -E '^(AllowSuspend|SuspendState)=' "$f" | tr '\n' ' ')"
	done
	[ -e "$DROPIN" ] && echo "  WARNING: $DROPIN is left from an interrupted run (the next run removes it)"
	echo "units stopped before each sleep (ConflictedBy suspend.target): $(systemctl show -p ConflictedBy --value sleep.target suspend.target 2>/dev/null | tr '\n' ' ')"
	echo "wake IRQs (number, count, wakeup):"
	for name in "$RTC_IRQ_NAME" "$PWRKEY_IRQ_NAME" "$RESIN_IRQ_NAME"; do
		n=$(irq_num "$name")
		echo "  $name: IRQ ${n:-missing}, count $(irq_count "$n")"
	done
	echo "  $RTC: $(cat "$RTC_SYS/name" 2>/dev/null), wakeup $(cat "$RTC_SYS/device/power/wakeup" 2>/dev/null || echo '?'), wakealarm '$(cat "$RTC_SYS/wakealarm" 2>/dev/null)'"
	for d in /sys/bus/platform/devices/*pon@1300:pwrkey /sys/bus/platform/devices/*pon@1300:resin; do
		[ -e "$d" ] && echo "  ${d##*/}: wakeup $(cat "$d/power/wakeup" 2>/dev/null || echo n/a)"
	done
	echo "interconnect: $(icc_line), INT_MAX lines $(intmax)"
	echo "qcom_stats: $(stats_line)"
	echo "ADSP $(adsp_state), fastrpc-adsp $([ -e /dev/fastrpc-adsp ] && echo present || echo missing), $ADSP_INHIBIT $(unit_state "$ADSP_INHIBIT"), $COLDBOOT $(unit_state "$COLDBOOT")"
	echo "Wi-Fi $(wifi_state), panel '$(panel_id)', root $(root_mode)"
	echo "sleep inhibitors (block):"
	systemd-inhibit --list --no-pager --no-legend 2>/dev/null | awk '$NF == "block"' | sed 's/^/  /'
	echo "last run: $(readlink "$BASE/latest" 2>/dev/null || echo none)"
}

cmd=${1:-}
[ $# -gt 0 ] && shift
case "$cmd" in
run) cmd_run "$@" ;;
key) cmd_key "$@" ;;
rtc-check) cmd_rtc_check "$@" ;;
stop) cmd_stop ;;
status) cmd_status "$@" ;;
check) cmd_check ;;
-h|--help|help) usage ;;
*) usage >&2; exit 2 ;;
esac
