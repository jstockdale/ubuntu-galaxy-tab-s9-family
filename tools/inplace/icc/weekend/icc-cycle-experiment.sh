#!/bin/sh
# Weekend experiments: why does an interconnect that leaves its INT_MAX boot
# floor BEFORE the boot-time pm_test=platform cycle hang that cycle, when the
# same release AFTER it holds?  docs/gts9wifi-interconnect.md has the
# reasoning and the order to run these in.  Root for everything but status.
#
# Runtime rungs (on the normal Stage 2 boot, after the boot cycle; a hang
# costs one watchdog reset and the next boot is a normal one):
#   sudo systemd-run --unit=gts9u-icc-exp --collect sh $0 runtime [options]
#       --level devices|platform   pm_test level (default platform)
#       --stop-adsp                stop the ADSP first, as at the boot cycle
#                                  (audio and sensors may need a reboot after)
#       --unbind-wifi              unbind ath11k from the WCN6855 for the cycle
#                                  (Wi-Fi SSH drops; use USB)
#       --trace                    console_suspend=N, pm_print_times,
#                                  pm_debug_messages for the cycle
#   It releases the floor first if it is still held.
#
# Boot rungs (one shot each: the attempt disarms itself before the cycle, so a
# hang cannot loop):
#   sh $0 arm-boot release-before-cycle [--trace] [--unbind-wifi]
#       next boot, Stage 2 images: release the floor from userspace right
#       before the panel cycle (Stage 3 without touching the images)
#   sh $0 arm-boot kernel-early [--trace] [--unbind-wifi]
#       for a boot on the fix2-s3 vendor_boot (bwmon built in, the kernel
#       syncs at ~13 s): only the tracing; if that boot does not come back,
#       the boot after it skips the panel cycle (panel dark, SSH works) so
#       the evidence can be collected without TWRP
#   sh $0 arm-boot trace-only
#       calibration: the normal boot cycle with tracing, floor held
#   sh $0 disarm                  remove the drop-in and the arm state
#
# After any of them:
#   sh $0 collect                 previous boot's pstore, kernel tail, markers
#   sh $0 status                  (no root) what is armed, last log lines
#
# Everything is logged to /var/log/gts9u-icc-exp/experiment.log (group adm),
# synced line by line, and marked in the kernel log ("gts9u-icc-exp:").
set -u

self=$(readlink -f "$0")
installed=/usr/local/libexec/gts9u-icc-experiment
state=/var/lib/gts9u-icc-exp
logdir=/var/log/gts9u-icc-exp
log=$logdir/experiment.log
cycle_unit=ubuntu-gts9u-panel-coldboot-recover.service
dropin_dir=/etc/systemd/system/$cycle_unit.d
dropin=$dropin_dir/90-gts9u-icc-experiment.conf
dev=/sys/bus/platform/devices
providers='24100000.interconnect interconnect-1'
helper=/usr/libexec/ubuntu-gts9u-icc-late-release
summary=/sys/kernel/debug/interconnect/interconnect_summary
printk_suspend=/sys/module/printk/parameters/console_suspend

say() {
	line="$(date '+%F %T') up=$(cut -d' ' -f1 /proc/uptime) $*"
	if [ "$(id -u)" = 0 ]; then
		mkdir -p "$logdir" 2>/dev/null && chgrp adm "$logdir" 2>/dev/null && chmod 2750 "$logdir" 2>/dev/null
		echo "$line" >> "$log" 2>/dev/null
		sync "$log" 2>/dev/null || sync
	fi
	echo "$line"
}
kmsg() { printf 'gts9u-icc-exp: %s\n' "$*" >> /dev/kmsg 2>/dev/null || :; }
die() { say "STOP: $*"; exit 1; }
need_root() { [ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }; }
need_board() {
	tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9wifi' ||
		die 'not an SM-X710'
}
intmax() {
	n=$(grep -c 2147483647 "$summary" 2>/dev/null) || :
	echo "${n:-n/a}"
}
synced_line() {
	for p in $providers; do
		printf '%s=%s ' "$p" "$(cat "$dev/$p/state_synced" 2>/dev/null || echo n/a)"
	done
	printf 'rpmhpd=%s' "$(cat "$dev/17a00000.rsc:power-controller/state_synced" 2>/dev/null || echo n/a)"
}
all_released() {
	for p in $providers; do
		[ "$(cat "$dev/$p/state_synced" 2>/dev/null)" = 1 ] || return 1
	done
}
field() { sed -n "s/^$1=//p" "$2" 2>/dev/null | head -n 1; }
boot8() { cut -c1-8 /proc/sys/kernel/random/boot_id; }
cmdline_extras() {
	tr ' ' '\n' < /proc/cmdline | grep -E '^(initcall_blacklist|icc_core|no_console_suspend|initcall_debug)' | tr '\n' ' '
}

# Direct write, for use before the cycle (the packaged helper refuses then, by
# design).  printf, not echo: the kernel rejects "1\n".
release_now() {
	for p in $providers; do
		[ "$(cat "$dev/$p/state_synced" 2>/dev/null)" = 1 ] && continue
		printf 1 > "$dev/$p/state_synced" 2>/dev/null || say "write to $p/state_synced failed"
	done
	say "released: $(synced_line); INT_MAX nodes now $(intmax)"
}

trace_on() {
	mkdir -p "$state"
	[ -e "$state/console_suspend.saved" ] || cat "$printk_suspend" > "$state/console_suspend.saved" 2>/dev/null
	echo N > "$printk_suspend" 2>/dev/null || say 'cannot set console_suspend=N'
	echo 1 > /sys/power/pm_print_times 2>/dev/null
	echo 1 > /sys/power/pm_debug_messages 2>/dev/null
	say 'trace on: console_suspend=N pm_print_times=1 pm_debug_messages=1 (each device callback reaches the ramoops and sec_log consoles)'
}
trace_off() {
	v=$(cat "$state/console_suspend.saved" 2>/dev/null)
	echo "${v:-Y}" > "$printk_suspend" 2>/dev/null
	rm -f "$state/console_suspend.saved"
	echo 0 > /sys/power/pm_print_times 2>/dev/null
	echo 0 > /sys/power/pm_debug_messages 2>/dev/null
}

adsp_dir() {
	for d in /sys/class/remoteproc/remoteproc*; do
		[ "$(cat "$d/name" 2>/dev/null)" = adsp ] && { echo "$d"; return; }
	done
}

wifi_pci=0000:01:00.0
wifi_drv=/sys/bus/pci/drivers/ath11k_pci
wifi_unbind() {
	if [ -e "$wifi_drv/$wifi_pci" ]; then
		echo "$wifi_pci" > "$wifi_drv/unbind" 2>/dev/null || say "unbinding $wifi_pci failed"
	fi
	say "Wi-Fi: ath11k $([ -e "$wifi_drv/$wifi_pci" ] && echo still bound || echo unbound) from $wifi_pci"
}
wifi_bind() {
	[ -e "$wifi_drv/$wifi_pci" ] && return 0
	echo "$wifi_pci" > "$wifi_drv/bind" 2>/dev/null || say "rebinding $wifi_pci failed (a reboot brings Wi-Fi back)"
	say "Wi-Fi: ath11k $([ -e "$wifi_drv/$wifi_pci" ] && echo bound again || echo not bound)"
}

failure_scan() { # file
	grep -E -i 'rpmh.*(timeout|fail)|tcs.*(timeout|busy)|noc[^a-z]*err|Unhandled context fault|smmu.*fault|gmu.*(timeout|fail)|ufshcd.*(abort|err|fail|timeout|reset)|AER: (Uncorrected|Multiple)|link (is )?down|ath11k.*(fail|timeout|crash)|dwc3.*(fail|err)|soft lockup|hung_task|Call trace:|Oops|BUG:' "$1" | head -n 20
}

runtime() {
	level=platform stop_adsp=0 unbind_wifi=0 trace=0
	while [ $# -gt 0 ]; do
		case "$1" in
		--level) level=${2:-}; shift ;;
		--stop-adsp) stop_adsp=1 ;;
		--unbind-wifi) unbind_wifi=1 ;;
		--trace) trace=1 ;;
		*) die "unknown option $1" ;;
		esac
		shift
	done
	case "$level" in devices|platform) ;; *) die "--level must be devices or platform" ;; esac
	[ "$(systemctl show -p Result --value "$cycle_unit")" = success ] &&
		[ "$(systemctl show -p ActiveState --value "$cycle_unit")" = active ] ||
		die "$cycle_unit has not finished successfully this boot; the runtime rungs are for after the boot cycle"
	grep -q '\[none\]' /sys/power/pm_test || die "a pm_test level is already selected: $(cat /sys/power/pm_test)"
	mkdir -p "$state"
	say "runtime: level=$level stop_adsp=$stop_adsp unbind_wifi=$unbind_wifi trace=$trace; boot $(boot8), kernel $(uname -r), cmdline: $(cmdline_extras)"
	say "runtime: before: $(synced_line); INT_MAX nodes $(intmax); suspend_stats $(cat /sys/power/suspend_stats/success)/$(cat /sys/power/suspend_stats/fail)"
	if ! all_released; then
		if [ -x "$helper" ]; then
			"$helper" release >> "$log" 2>&1 || say "the helper failed (see above)"
			say "after the helper: $(synced_line); INT_MAX nodes $(intmax)"
		else
			release_now
		fi
		all_released || die 'the floor is still held (see the helper lines above); nothing was cycled'
	fi
	adsp=$(adsp_dir)
	if [ "$stop_adsp" = 1 ]; then
		[ -n "$adsp" ] || die 'no adsp remoteproc'
		if [ "$(cat "$adsp/state")" = running ]; then
			echo stop > "$adsp/state" || die 'stopping the ADSP failed'
			n=0
			while [ "$(cat "$adsp/state")" != offline ] && [ $n -lt 20 ]; do sleep 0.5; n=$((n + 1)); done
		fi
		say "runtime: ADSP $(cat "$adsp/state")"
	fi
	[ "$unbind_wifi" = 1 ] && wifi_unbind
	[ "$trace" = 1 ] && trace_on
	marker="gts9u-icc-exp: runtime $level $(date +%s)"
	echo "$marker" > /dev/kmsg 2>/dev/null
	say "runtime: entering pm_test=$level (mem, deep); USB networking drops for ~10 s"
	sync
	# Deep for the cycle, as the boot cycle pins it; the default goes back after.
	mem_before=$(sed -n 's/.*\[\([a-z0-9]*\)\].*/\1/p' /sys/power/mem_sleep 2>/dev/null)
	echo deep > /sys/power/mem_sleep
	echo "$level" > /sys/power/pm_test
	if echo mem > /sys/power/state; then rc=0; else rc=$?; fi
	echo none > /sys/power/pm_test
	[ -n "$mem_before" ] && echo "$mem_before" > /sys/power/mem_sleep 2>/dev/null
	[ "$trace" = 1 ] && trace_off
	[ "$unbind_wifi" = 1 ] && wifi_bind
	say "runtime: returned rc=$rc; suspend_stats $(cat /sys/power/suspend_stats/success)/$(cat /sys/power/suspend_stats/fail) last_failed_dev='$(cat /sys/power/suspend_stats/last_failed_dev)' errno=$(cat /sys/power/suspend_stats/last_failed_errno)"
	if [ "$stop_adsp" = 1 ] && [ -n "$adsp" ]; then
		echo start > "$adsp/state" 2>/dev/null || say 'restarting the ADSP failed (reboot restores audio and sensors)'
		sleep 2
		say "runtime: ADSP $(cat "$adsp/state") again (audio and sensors may still need a reboot)"
	fi
	sleep 10
	out=$logdir/runtime-$(boot8)-$(date +%H%M%S)-kmsg.txt
	dmesg | awk -v m="$marker" 'index($0, m) {f = 1} f' > "$out"
	hits=$(failure_scan "$out")
	if [ -n "$hits" ]; then
		echo "$hits" | while IFS= read -r l; do say "  kmsg: $l"; done
		say "runtime: failure signatures after the cycle (all of it in $out)"
	else
		say "runtime: no failure signatures after the cycle ($out); panel: $(grep -c 'ana38407 panel id' "$out") id reads"
	fi
	say "runtime: after: $(synced_line); INT_MAX nodes $(intmax)"
}

arm_boot() {
	mode=${1:-}
	trace=0 unbind_wifi=0
	[ $# -gt 0 ] && shift
	while [ $# -gt 0 ]; do
		case "$1" in
		--trace) trace=1 ;;
		--unbind-wifi) unbind_wifi=1 ;;
		*) die "unknown option $1" ;;
		esac
		shift
	done
	case "$mode" in
	release-before-cycle|kernel-early) ;;
	trace-only) trace=1 ;;
	*) die 'arm-boot needs release-before-cycle, kernel-early or trace-only' ;;
	esac
	[ -e "$state/armed" ] && die "already armed ($(tr '\n' ' ' < "$state/armed")); disarm first"
	install -D -o root -g root -m 0755 "$self" "$installed" || die "cannot install $installed"
	mkdir -p "$state" "$dropin_dir"
	[ -e "$state/fired" ] && mv "$state/fired" "$state/fired.$(date +%Y%m%d-%H%M%S)"
	printf 'mode=%s\ntrace=%s\nunbind_wifi=%s\narmed_at=%s\narmed_boot=%s\n' \
		"$mode" "$trace" "$unbind_wifi" "$(date '+%F %T')" "$(boot8)" > "$state/armed"
	cat > "$dropin" <<EOF
# One-shot interconnect experiment (tools/inplace/icc/weekend).  Inert once
# fired; remove with: sudo sh $installed disarm
[Service]
ExecCondition=/bin/sh $installed boot-gate
ExecStartPre=/bin/sh $installed pre-cycle
ExecStartPost=/bin/sh $installed post-cycle
EOF
	systemctl daemon-reload || die 'daemon-reload failed'
	sync
	say "armed: $mode, trace=$trace, unbind_wifi=$unbind_wifi, for the next boot.  $(
		case "$mode" in
		kernel-early) echo 'Now flash the Stage 3 vendor_boot (sudo sh ~/display-exp/apply.sh fix2-s3) and reboot.' ;;
		*) echo 'Reboot (sudo reboot).' ;;
		esac)"
}

# ExecCondition of the panel cycle unit: 0 runs the cycle, 1 skips it.  Kept
# minimal: anything unexpected runs the cycle as usual.
boot_gate() {
	[ -e "$state/armed" ] && exit 0
	[ -e "$state/fired" ] || exit 0
	[ "$(field mode "$state/fired")" = kernel-early ] || exit 0
	grep -q '^returned=' "$state/fired" && exit 0
	# For as long as this is still a Stage 3 cmdline (no bwmon hold), every
	# boot skips the cycle: back on Stage 2 the cycle is safe again.
	case " $(cat /proc/cmdline) " in
	*' initcall_blacklist='*bwmon_driver_init*) exit 0 ;;
	esac
	echo "gated=$(boot8)" >> "$state/fired"
	say "boot-gate: the kernel-early attempt (boot $(field fired_boot "$state/fired")) did not return; skipping the panel cycle on this boot to break the loop.  Panel stays dark; SSH works.  Next: sudo sh $installed collect; then put Stage 2 back (sudo sh ~/display-exp/apply.sh fix2-s2), sudo sh $installed disarm, reboot."
	exit 1
}

pre_cycle() {
	[ -e "$state/armed" ] || exit 0
	mode=$(field mode "$state/armed")
	trace=$(field trace "$state/armed")
	# One shot: consumed before anything else, so a hang cannot loop.
	mv "$state/armed" "$state/fired"
	echo "fired_boot=$(boot8)" >> "$state/fired"
	sync
	say "pre-cycle: mode=$mode trace=$trace; boot $(boot8), kernel $(uname -r), cmdline: $(cmdline_extras)"
	say "pre-cycle: $(synced_line); INT_MAX nodes $(intmax); ADSP $(cat "$(adsp_dir)/state" 2>/dev/null)"
	if [ "$(field unbind_wifi "$state/fired")" = 1 ]; then
		# At boot ath11k binds only after gts9u-pci-rescan (about 22 s);
		# give it up to 20 s so the unbind does not race it.
		n=0
		while [ ! -e "$wifi_drv/$wifi_pci" ] && [ $n -lt 40 ]; do sleep 0.5; n=$((n + 1)); done
		wifi_unbind
	fi
	[ "$trace" = 1 ] && trace_on
	[ "$mode" = release-before-cycle ] && release_now
	kmsg "pre-cycle done (mode $mode); the panel cycle follows"
	sync
	exit 0
}

post_cycle() {
	[ -e "$state/fired" ] || exit 0
	grep -q '^returned=' "$state/fired" && exit 0
	echo "returned=$(boot8)" >> "$state/fired"
	[ "$(field trace "$state/fired")" = 1 ] && trace_off
	[ "$(field unbind_wifi "$state/fired")" = 1 ] && wifi_bind
	say "post-cycle: the boot cycle RETURNED (mode $(field mode "$state/fired")); suspend_stats $(cat /sys/power/suspend_stats/success)/$(cat /sys/power/suspend_stats/fail); $(synced_line); INT_MAX nodes $(intmax)"
	dmesg | grep 'ana38407 panel id' | tail -n 2 | while IFS= read -r l; do say "  $l"; done
	kmsg 'post-cycle: the boot cycle returned'
	exit 0
}

collect() {
	out=$logdir/collect-$(boot8)
	mkdir -p "$out/pstore" "$out/state"
	cp -a /sys/fs/pstore/. "$out/pstore/" 2>/dev/null
	cp -a /var/lib/systemd/pstore/. "$out/pstore/" 2>/dev/null
	ls -laR /sys/fs/pstore /var/lib/systemd/pstore > "$out/pstore/listing.txt" 2>&1
	cp -a "$state/." "$out/state/" 2>/dev/null
	journalctl --list-boots --no-pager 2>/dev/null | tail -n 6 > "$out/boots.txt"
	# A boot that hung in its first ~40 s never reached the journal on this
	# board (none of the Stage 3 loops is in --list-boots), so "-b -1" is then
	# the boot before it: kern.log.tail below is the one that has it.
	{
		echo "# journal boot -1 = $(journalctl --list-boots --no-pager 2>/dev/null | tail -n 2 | head -n 1)"
		echo '# a hung boot is usually missing from the journal; see kern.log.tail'
		journalctl -b -1 -k --no-pager -o short-monotonic 2>/dev/null | tail -n 400
	} > "$out/prev-boot-kernel-tail.txt"
	# Kernel lines are in kern.log; the /dev/kmsg markers written from
	# userspace carry the user facility, so rsyslog files them in syslog.
	cat /var/log/kern.log /var/log/syslog 2>/dev/null |
		grep -a 'gts9u-icc-exp\|icc-late-release\|PM: suspend entry\|PM: suspend exit' |
		sort -u | tail -n 60 > "$out/kernlog-markers.txt"
	tail -n 600 /var/log/kern.log > "$out/kern.log.tail" 2>/dev/null
	dmesg > "$out/dmesg-this-boot.txt"
	chgrp -R adm "$out" 2>/dev/null
	find "$out" -type d -exec chmod 2750 {} + 2>/dev/null
	find "$out" -type f -exec chmod 0640 {} + 2>/dev/null
	say "collected in $out ($(ls "$out/pstore" | wc -l) pstore entries)"
	# The last device callbacks the previous boot's console kept, if any.
	for f in "$out"/pstore/console-ramoops*; do
		[ -f "$f" ] || continue
		say "last lines of ${f##*/}:"
		grep -a -E 'PM:|calling|returned|gts9u-icc-exp|Waiting for|suspend' "$f" | tail -n 15 |
			while IFS= read -r l; do say "  $l"; done
	done
}

status() {
	for f in armed fired; do
		if [ -r "$state/$f" ]; then
			echo "$f: $(tr '\n' ' ' < "$state/$f")"
		else
			echo "$f: no"
		fi
	done
	echo "drop-in: $([ -e "$dropin" ] && echo "$dropin" || echo none)"
	echo "console_suspend=$(cat "$printk_suspend" 2>/dev/null) pm_test: $(cat /sys/power/pm_test)"
	echo "providers: $(synced_line)"
	tail -n 15 "$log" 2>/dev/null
}

disarm() {
	rm -f "$dropin"
	rmdir "$dropin_dir" 2>/dev/null
	rm -f "$state/armed"
	[ -e "$state/fired" ] && mv "$state/fired" "$state/fired.$(date +%Y%m%d-%H%M%S)"
	[ -e "$state/console_suspend.saved" ] && trace_off
	systemctl daemon-reload
	say "disarmed: no drop-in, nothing armed ($installed stays until removed by hand; it is inert)"
}

case "${1:-}" in
status) status; exit 0 ;;
boot-gate) boot_gate ;;
esac
need_root
need_board
cmd=${1:-}
[ $# -gt 0 ] && shift
case "$cmd" in
runtime) runtime "$@" ;;
arm-boot) arm_boot "$@" ;;
pre-cycle) pre_cycle ;;
post-cycle) post_cycle ;;
collect) collect ;;
disarm) disarm ;;
*)
	sed -n '2,45p' "$self"
	exit 2
	;;
esac
