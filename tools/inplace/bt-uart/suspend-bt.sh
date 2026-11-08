#!/bin/sh
# suspend-bt.sh - suspend cycles with Bluetooth up and scanning, the
# suspending task pinned to the CPU that takes the Bluetooth UART's
# interrupt.  After every resume it checks whether that interrupt went
# unhandled or was disabled, and whether the controller still answers.
#
#   sudo sh ~/bt-uart/suspend-bt.sh platform [N]   N pm_test=platform cycles (default 20)
#   sudo sh ~/bt-uart/suspend-bt.sh s2idle [N]     N real s2idle sleeps, RTC-woken (default 3)
#   sudo sh ~/bt-uart/suspend-bt.sh deep [N]       N real deep sleeps, RTC-woken (default 3)
#   sh ~/bt-uart/suspend-bt.sh status              the latest run (no root)
#   sudo sh ~/bt-uart/suspend-bt.sh recover        rebind a controller whose interrupt was disabled
# options, after N: --no-pin (let the scheduler place the task),
#                   --no-scan (no LE scan traffic)
#
# Why pin: the interrupt goes to one CPU (CPU 0; there is no irqbalance).
# A storm can only disable it when the task that suspends the UART runs on
# that same CPU.  The handler then refires the moment the port lock drops,
# and the task never reaches the shutdown() that masks the interrupt.
# Pinning makes every cycle that case.
#
# Not read-only.  A run detaches into the transient unit
# gts9u-bt-uart-test.service, with CPUAffinity set to the interrupt's CPU, so
# that the sleeping task inherits it.  It stops at the first failed cycle.
# platform writes pm_test and /sys/power/state itself.  s2idle and deep
# run the suspendkit's gts9u-suspend-cycles.sh in the foreground for one
# cycle at a time, with its hooks and checklist.  While a cycle runs, a
# tracefs instance records the interrupt's handler results and the UART's
# suspend callback (two kprobes).  The kprobes and the instance are removed
# afterwards, and pm_test and mem_sleep are put back.  Logs go to
# /var/log/gts9u-bt-uart/ (root:adm), one directory per run.
set -u
export LC_ALL=C
umask 027

TAG=gts9u-bt-uart
UNIT=gts9u-bt-uart-test
BASE=/var/log/gts9u-bt-uart
T=/sys/kernel/tracing
INST=$T/instances/gts9u-bt-uart
P=/sys/power
self=$(readlink -f "$0")
kit=$(dirname "$self")
log=/dev/null
rundir=

say() {
	line="$(date '+%F %T') $*"
	printf '%s\n' "$line" >> "$log"
	printf '%s\n' "$line" 2>/dev/null || :
}
die() { say "STOP: $*"; [ -n "$rundir" ] && : > "$rundir/FAILED"; exit 1; }
need_root() { [ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }; }

# The geni UART with a bluetooth child node, its controller, and its
# interrupt: the GIC SPI in the UART's DT node (<type number flags ...>,
# type 0 = SPI), matched to the Linux IRQ by hwirq.
find_bt() {
	uart= hci= irq= irq_cpu=
	for d in /sys/bus/platform/drivers/qcom_geni_serial/*.serial; do
		[ -d "$d/of_node/bluetooth" ] && uart=${d##*/}
	done
	[ -n "$uart" ] || return 1
	# hciN only: connections show up there as hciN:M.
	for h in /sys/class/bluetooth/hci*; do
		case "${h##*/}" in *:*) continue ;; esac
		readlink -f "$h" | grep -q "/$uart/" && hci=${h##*/}
	done
	# shellcheck disable=SC2046
	set -- $(od -An -tu4 --endian=big "/sys/bus/platform/devices/$uart/of_node/interrupts" 2>/dev/null)
	[ "${1:-}" = 0 ] && [ -n "${2:-}" ] || return 1
	hwirq=$(($2 + 32))
	for d in /sys/kernel/irq/*; do
		[ "$(cat "$d/hwirq" 2>/dev/null)" = "$hwirq" ] &&
			[ "$(cat "$d/chip_name" 2>/dev/null)" = GICv3 ] && irq=${d##*/}
	done
	[ -n "$irq" ] || return 1
	irq_cpu=$(cut -d, -f1 "/proc/irq/$irq/effective_affinity_list" | cut -d- -f1)
}

# HCI Read Local Version Information: a command complete with status 0.
hci_ok() {
	[ -n "$hci" ] || return 1
	timeout 10 hcitool -i "$hci" cmd 0x04 0x0001 2>&1 | tr -s ' \n' '  ' |
		grep -qiE '0x0e plen [0-9]+ 01 01 10 00'
}

trace_off() {
	if [ -d "$INST" ]; then
		echo 0 > "$INST/tracing_on" 2>/dev/null
		echo 0 > "$INST/events/enable" 2>/dev/null
		rmdir "$INST" 2>/dev/null
	fi
	echo '-:btuart/suspend' >> "$T/dynamic_events" 2>/dev/null
	echo '-:btuart/suspended' >> "$T/dynamic_events" 2>/dev/null
}
trace_on() {
	trace_off
	mkdir "$INST" && echo 4096 > "$INST/buffer_size_kb" || return 1
	echo "irq == $irq" > "$INST/events/irq/irq_handler_exit/filter" &&
		echo 1 > "$INST/events/irq/irq_handler_exit/enable" || return 1
	window=1
	# BTF names the argument; the raw form is dev->kobj.name on arm64.
	echo 'p:btuart/suspend qcom_geni_serial_suspend dev=dev->kobj.name:string' \
		>> "$T/dynamic_events" 2>/dev/null ||
		echo 'p:btuart/suspend qcom_geni_serial_suspend dev=+0(+0(%x0)):string' \
		>> "$T/dynamic_events" 2>/dev/null || window=0
	if [ "$window" = 1 ]; then
		echo 'r:btuart/suspended qcom_geni_serial_suspend ret=$retval' >> "$T/dynamic_events" &&
			echo 1 > "$INST/events/btuart/enable" || window=0
	fi
	[ "$window" = 1 ] || say 'note: no kprobe on qcom_geni_serial_suspend; the in-window count is unavailable'
}

# One cycle's trace -> "irqs unhandled in_window in_window_unhandled cpu ret":
# handler results for the interrupt, overall and between the UART's suspend
# callback's entry and its return (same task), and that task's CPU.
analyse() {
	awk -v dev="$uart" '
	function cpu_of(s) { if (match(s, /\[[0-9]+\]/)) return substr(s, RSTART + 1, RLENGTH - 2) + 0; return "" }
	function pid_of(s,   m) {
		if (!match(s, /-[0-9]+ +\[/)) return ""
		m = substr(s, RSTART + 1, RLENGTH - 1); sub(/ +\[$/, "", m); return m
	}
	/ irq_handler_exit: / { n++; u = ($0 ~ / ret=unhandled/); nu += u; if (win) { w++; wu += u }; next }
	/ suspend: / && index($0, "dev=\"" dev "\"") { win = 1; wpid = pid_of($0); cpu = cpu_of($0); next }
	/ suspended: / && win && pid_of($0) == wpid { win = 0; ret = $NF; sub(/^ret=/, "", ret) }
	END { printf "%d %d %d %d %s %s\n", n, nu, w, wu, (cpu == "" ? "-" : cpu), (ret == "" ? "-" : ret) }
	' "$1"
}

scan_pid=
scan_start() {
	[ "$scan" = 1 ] || return 0
	btmgmt --index "${hci#hci}" find -l > /dev/null 2>&1 &
	scan_pid=$!
}
scan_stop() {
	[ -n "$scan_pid" ] || return 0
	kill "$scan_pid" 2>/dev/null
	wait "$scan_pid" 2>/dev/null
	scan_pid=
}

restore() {
	scan_stop
	trace_off
	echo none > "$P/pm_test" 2>/dev/null
	[ -n "${mem_sleep0:-}" ] && echo "$mem_sleep0" > "$P/mem_sleep" 2>/dev/null
	:
}

describe() {
	want=$(cat "$kit/BUILD_ID" 2>/dev/null)
	id=$(od -An -tx1 -v /sys/kernel/notes | tr -d ' \n' | grep -o '474e5500[0-9a-f]\{40\}' | cut -c9-)
	if [ -n "$want" ] && [ "$id" = "$want" ]; then what='the BT UART kit'
	elif [ "$id" = bfb40d206abbdfcf7f4f3298fd84c96c4c2ea1c6 ]; then what='the CX kit (no fix: baseline)'
	else what='an unknown kernel'; fi
	say "boot $(cat /proc/sys/kernel/random/boot_id), kernel $(uname -v), build ID $(echo "$id" | cut -c1-12): $what"
	say "Bluetooth: $hci on $uart, IRQ $irq ($(cat "/sys/kernel/irq/$irq/actions"), hwirq $hwirq) on CPU $irq_cpu; this task on CPUs $(sed -n 's/^Cpus_allowed_list:[[:space:]]*//p' /proc/self/status)"
	say "mem_sleep [$(cat $P/mem_sleep)], pm_test [$(cat $P/pm_test)], IRQ $irq $(tr '\n' ' ' < "/proc/irq/$irq/spurious")"
}

# ------------------------------------------------------------------ run ---
cycle() { # mode name first
	marker="$TAG: cycle $2 start"
	echo "$marker" > /dev/kmsg
	: > "$INST/trace"
	echo 1 > "$INST/tracing_on"
	scan_start
	sleep 3
	rc=0
	if [ "$1" = platform ]; then
		grep -qw deep "$P/mem_sleep" && echo deep > "$P/mem_sleep"
		echo platform > "$P/pm_test"
		echo mem > "$P/state" || rc=$?
		echo none > "$P/pm_test"
	else
		opts="--foreground --sleep 20 --gap 0 --s2idle 0 --deep 0"
		[ "$1" = s2idle ] && opts="$opts --s2idle 1" || opts="$opts --deep 1"
		[ "$3" = 1 ] || opts="$opts --no-rtc-check"
		# shellcheck disable=SC2086
		sh "$kit/gts9u-suspend-cycles.sh" run $opts >> "$rundir/suspendkit.out" 2>&1 || rc=$?
		say "  suspendkit: rc $rc, $(readlink -f /var/log/gts9u-suspendkit/latest 2>/dev/null)"
	fi
	sleep 5
	echo 0 > "$INST/tracing_on"
	scan_stop
	cp "$INST/trace" "$rundir/$2.trace"
	klog=$(dmesg | sed -n "/$marker/,\$p")
	printf '%s\n' "$klog" > "$rundir/$2.dmesg"
	# shellcheck disable=SC2046
	set -- "$@" $(analyse "$rundir/$2.trace")
	irqs=$4 nu=$5 w=$6 wu=$7 scpu=$8 sret=$9
	[ "$window" = 1 ] || w=- wu=-
	why=
	[ "$rc" = 0 ] || why="$why suspend-rc-$rc"
	dead=0
	printf '%s\n' "$klog" | grep -qE "nobody cared|Disabling IRQ #$irq\$" && { why="$why irq-disabled"; dead=1; }
	printf '%s\n' "$klog" | grep -qE "$hci: (command 0x[0-9a-f]+ tx timeout|Opcode 0x[0-9a-f]+ failed: -110)" &&
		why="$why hci-timeouts"
	[ "$nu" = 0 ] || why="$why unhandled-$nu"
	hci_ok || { sleep 5; hci_ok; } || why="$why hci-no-answer"
	[ -z "$why" ] && res=ok || res=FAIL
	printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$2" "$1" "$res" "$rc" "$scpu" \
		"$irq_cpu" "$irqs" "$nu" "$w" "$wu" "${why# }" >> "$tsv"
	say "$2: $res; suspend on CPU $scpu (IRQ on $irq_cpu), callback ret $sret; IRQ $irq handled $((irqs - nu)), unhandled $nu; while the port was suspending: $w (unhandled $wu)${why:+; failed:$why}"
	[ "$res" = ok ] && return 0
	[ "$dead" = 1 ] && say "IRQ $irq is disabled: Bluetooth stays dead until 'sudo sh $self recover'"
	return 1
}

cmd_run() { # mode count
	mode=$1 count=$2
	find_bt || { echo 'no Bluetooth UART, controller or interrupt found' >&2; exit 1; }
	[ -n "$hci" ] || { echo "no controller on $uart" >&2; exit 1; }
	exec 9> /run/gts9u-bt-uart-test.lock
	flock -n 9 || { echo 'another run holds /run/gts9u-bt-uart-test.lock' >&2; exit 1; }
	[ -d "$BASE" ] || mkdir -p "$BASE"
	chgrp adm "$BASE" 2>/dev/null; chmod 2750 "$BASE" 2>/dev/null
	rundir=$BASE/$mode-$(date +%Y%m%d-%H%M%S)
	mkdir -p "$rundir" || exit 1
	ln -sfn "${rundir##*/}" "$BASE/latest"
	log=$rundir/run.log
	tsv=$rundir/cycles.tsv
	printf 'cycle\tmode\tresult\trc\tsuspend_cpu\tirq_cpu\tirqs\tunhandled\tin_window\tin_window_unhandled\treasons\n' > "$tsv"
	mem_sleep0=$(sed -n 's/.*\[\([a-z0-9]*\)\].*/\1/p' "$P/mem_sleep")
	trap 'restore; chgrp -R adm "$rundir" 2>/dev/null' EXIT
	trap 'exit 1' HUP INT TERM
	say "$TAG: $count $mode cycles, pin $pin, LE scan $scan"
	describe
	grep -q '\[none\]' "$P/pm_test" || die "pm_test is not none: $(cat $P/pm_test)"
	hci_ok || die "$hci does not answer an HCI command before the first cycle"
	trace_on || die 'cannot set up the trace instance'
	i=1
	while [ "$i" -le "$count" ]; do
		cycle "$mode" "$mode-$i" "$i" || {
			: > "$rundir/FAILED"
			say "RESULT: FAIL at cycle $mode-$i; logs in $rundir"
			exit 1
		}
		i=$((i + 1))
	done
	: > "$rundir/DONE"
	say "RESULT: PASS, $count $mode cycles; logs in $rundir"
}

cmd_status() {
	d=$BASE/latest
	[ -r "$d/run.log" ] || { echo "no readable run in $BASE (adm group?)" >&2; exit 1; }
	echo "== $(readlink -f "$d")"
	systemctl is-active --quiet "$UNIT.service" && echo '(still running)'
	cat "$d/run.log"
	echo; column -t -s "$(printf '\t')" "$d/cycles.tsv" 2>/dev/null || cat "$d/cycles.tsv"
}

cmd_recover() {
	need_root
	find_bt || { echo 'no Bluetooth UART found' >&2; exit 1; }
	drv=/sys/bus/platform/drivers/qcom_geni_serial
	echo "rebinding $uart (IRQ $irq)"
	marker="$TAG: recover $(date +%s)"
	echo "$marker" > /dev/kmsg
	echo "$uart" > "$drv/unbind" || exit 1
	sleep 1
	echo "$uart" > "$drv/bind" || exit 1
	# The controller loads its firmware again; the address comes after.
	i=0
	until dmesg | sed -n "/$marker/,\$p" | grep -q 'QCA setup on UART is completed'; do
		i=$((i + 1)); [ "$i" -le 30 ] || { echo 'no QCA setup within 30 s' >&2; exit 1; }
		sleep 1
	done
	systemctl restart ubuntu-gts9u-bluetooth-address.service
	find_bt
	hci_ok && echo "$hci answers again" || { echo "$hci still does not answer" >&2; exit 1; }
}

# ---------------------------------------------------------------- main ---
cmd=${1:-}
case "$cmd" in
platform|s2idle|deep)
	need_root
	shift
	[ "$cmd" = platform ] && n=20 || n=3
	case "${1:-}" in [0-9]*) n=$1; shift ;; esac
	pin=1 scan=1
	for o in "$@"; do
		case "$o" in
		--no-pin) pin=0 ;;
		--no-scan) scan=0 ;;
		*) echo "unknown option $o" >&2; exit 2 ;;
		esac
	done
	tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9wifi' ||
		{ echo 'this is not an SM-X710' >&2; exit 1; }
	if [ "${GTS9U_BT_UART_DETACHED:-}" = 1 ]; then
		cmd_run "$cmd" "$n"
		exit
	fi
	find_bt || { echo 'no Bluetooth UART, controller or interrupt found' >&2; exit 1; }
	[ "$cmd" = platform ] || [ -f "$kit/gts9u-suspend-cycles.sh" ] ||
		{ echo "no $kit/gts9u-suspend-cycles.sh" >&2; exit 1; }
	systemctl reset-failed "$UNIT.service" > /dev/null 2>&1
	set -- --unit="$UNIT" --collect --quiet -p KillMode=mixed -p TimeoutStopSec=900 \
		-p SyslogIdentifier="$TAG" --setenv=GTS9U_BT_UART_DETACHED=1
	[ "$pin" = 1 ] && set -- "$@" -p CPUAffinity="$irq_cpu"
	opts=
	[ "$pin" = 1 ] || opts="$opts --no-pin"
	[ "$scan" = 1 ] || opts="$opts --no-scan"
	# shellcheck disable=SC2086
	systemd-run "$@" /bin/sh "$self" "$cmd" "$n" $opts ||
		{ echo 'systemd-run failed' >&2; exit 1; }
	echo "DETACHED as $UNIT.service; follow with: sh $self status   (or: journalctl -f -t $TAG)"
	;;
status) cmd_status ;;
recover) cmd_recover ;;
*) sed -n '2,/--no-scan/{s/^# \{0,1\}//;p}' "$self"; exit 2 ;;
esac
