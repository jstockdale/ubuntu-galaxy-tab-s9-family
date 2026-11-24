#!/bin/sh
# Watch the sensors' sleep hook stop the sensor clients and bring them back,
# where a real sleep cannot show it: the SM-X710's sleep test ends every run
# with a restart, and its suspend is masked.  Needs this kit's files in place.
#
#   sudo sh ~/sensors-hook/cycle.sh hand [SECONDS]
#       the hook's pre step as systemd-sleep runs it (pre suspend), SECONDS
#       (default 10, 1 to 120) with the clients stopped, its post step, then
#       the wait for the sensors.  No sleep, no other hook.
#   sudo sh ~/sensors-hook/cycle.sh freezer
#       one suspend through systemd-sleep itself, in a unit of its own as
#       systemd-suspend.service would run it, so that every hook in
#       /usr/lib/systemd/system-sleep runs before and after it, with
#       pm_test=freezer: the kernel freezes the tasks, waits (pm_test_delay,
#       5 s by default) and thaws them.  No device suspends and the SoC does
#       not sleep: the storage, PCIe, USB and the DSPs are not touched.  The
#       other hooks run as for a real sleep, the flashlight's, the sleep
#       statistics' and the Bluetooth UART check's, and on a tablet that has
#       them the USB gadget's (it rebinds the UDC after the cycle: an SSH
#       session over the USB cable can drop, the run goes on) and the USB
#       role's (a tablet that is USB host leaves host role for the cycle).
#       For the cycle only, a drop-in in /run/systemd/sleep.conf.d allows
#       suspend (the board's policy may not) and pm_test is set; both are put
#       back when the cycle ends, also on an error or Ctrl-C.  sleep.target
#       and logind play no part, so a masked suspend.target stays masked.
#
# Both refuse while the sensor recovery is still running or the proxy is not
# up (nothing to watch come back).  Each run says, between the two steps,
# what the hook left of the clients, and ends with one line:
#   SENSORS BACK after N s (...)       the resume unit succeeded, the proxy
#                                      is up and offers an accelerometer
#   SENSORS NOT BACK after N s (...)   anything else, with what was seen
# N counts from the post step (hand) or the end of systemd-sleep (freezer).
# Everything also goes to /var/log/gts9u-sensors-hook.log; hangups are
# ignored, so a dropped SSH session does not cut a run short.
set -eu
export LC_ALL=C

kit=$(cd "$(dirname "$0")" && pwd)
# The system's root: empty on a tablet (the tests point it at a scratch tree).
root=
log=$root/var/log/gts9u-sensors-hook.log
hook=$root/usr/lib/systemd/system-sleep/ubuntu-gts9u-sensors
sleep_bin=$root/usr/lib/systemd/systemd-sleep
power=$root/sys/power
dropin=$root/run/systemd/sleep.conf.d/zz-gts9u-sensors-hook-cycle.conf
unit=ubuntu-gts9u-sensors-resume.service
clients='iio-sensor-proxy.service hexagonrpcd-adsp-sensorspd.service'
wait_max=300

say() {
	line="$(date '+%F %T') sensors-hook cycle: $*"
	echo "$line" >> "$log"
	echo "$line" 2>/dev/null || :
}
die() { say "STOP: $*"; exit 1; }
usage() { echo 'usage: sudo sh cycle.sh hand [SECONDS] | freezer' >&2; exit 2; }
now() { date +%s.%N; }
states() {
	for u in $clients $unit; do printf '%s %s, ' "${u%.service}" "$(systemctl is-active "$u" 2>/dev/null)"; done |
		sed 's/, $//'
}
invocation() { systemctl show -p InvocationID --value "$unit" 2>/dev/null; }
# step pre|post: the installed hook's step, as systemd-sleep runs it, with
# what it says.
step() {
	say "$1: sh $hook $1 suspend"
	out=$(sh "$hook" "$1" suspend 2>&1) || say "the hook's $1 step returned $?"
	[ -z "$out" ] || printf '%s\n' "$out" | while IFS= read -r l; do say "hook: $l"; done
}
accel() {
	timeout 10 gdbus introspect --system --dest net.hadess.SensorProxy --object-path /net/hadess/SensorProxy \
		--only-properties 2>/dev/null | sed -n 's/^ *readonly b HasAccelerometer = \([a-z]*\);$/\1/p'
}

mode=${1:-}
hold=10
case $mode in
hand)
	[ $# -le 2 ] || usage
	hold=${2:-10}
	case $hold in ''|*[!0-9]*) usage ;; esac
	[ "$hold" -ge 1 ] && [ "$hold" -le 120 ] || usage ;;
freezer) [ $# = 1 ] || usage ;;
*) usage ;;
esac
[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> "$root/run/gts9u-sensors-hook.lock"
flock -n 9 || { echo 'install.sh, rollback.sh or cycle.sh is still running; see /var/log/gts9u-sensors-hook.log' >&2; exit 1; }
trap '' HUP PIPE
touch "$log" && chgrp adm "$log" && chmod 0640 "$log" || { echo "cannot write $log" >&2; exit 1; }

# --- checks --------------------------------------------------------------------
want=$(awk '$2 == "files/usr/lib/systemd/system-sleep/ubuntu-gts9u-sensors" {print $1}' "$kit/SHA256SUMS" 2>/dev/null)
[ -n "$want" ] && [ "$(sha256sum < "$hook" | cut -d' ' -f1)" = "$want" ] ||
	die "the hook in place is not this kit's revision $(cat "$kit/REVISION" 2>/dev/null) (sudo sh $kit/install.sh first)"
case $(systemctl is-active "$unit" 2>/dev/null) in
activating|deactivating|reloading) die "$unit is still running (the last recovery): try again when it is done" ;;
esac
[ "$(systemctl is-active iio-sensor-proxy.service 2>/dev/null)" = active ] ||
	die 'iio-sensor-proxy is not running: the sensors are not up, there is nothing to watch come back'
if [ "$mode" = freezer ]; then
	[ -x "$sleep_bin" ] || die "$sleep_bin is missing"
	command -v systemd-run >/dev/null 2>&1 || die 'systemd-run is missing'
	grep -qw freezer "$power/pm_test" 2>/dev/null || die 'this kernel has no pm_test=freezer'
	[ "$(sed -n 's/.*\[\([a-z]*\)\].*/\1/p' "$power/pm_test")" = none ] ||
		die "pm_test is not none ($(cat "$power/pm_test")): something else is testing"
	[ ! -e "$dropin" ] || die "$dropin exists: another cycle did not end cleanly; remove it after a look"
fi
say "boot $(cat "$root/proc/sys/kernel/random/boot_id" 2>/dev/null), $mode, kit revision $(cat "$kit/REVISION" 2>/dev/null); now: $(states)"
since=$(date '+%F %T')

# --- the cycle -------------------------------------------------------------------
if [ "$mode" = hand ]; then
	step pre
	say "after pre: $(states)"
	before=$(invocation)
	sleep "$hold"
	step post
	t0=$(now)
else
	restore() {
		echo none > "$power/pm_test" 2>/dev/null || :
		rm -f "$dropin"
	}
	trap 'restore' EXIT
	trap 'exit 1' INT TERM
	mkdir -p "${dropin%/*}"
	printf '%s\n' '# ~/sensors-hook/cycle.sh freezer, for one cycle only; removed when it ends.' \
		'[Sleep]' 'AllowSuspend=yes' 'SuspendState=mem' > "$dropin"
	echo freezer > "$power/pm_test"
	before=$(invocation)
	say "systemd-sleep suspend with pm_test=freezer, in the unit gts9u-sensors-hook-cycle-$$"
	if systemd-run --wait --collect --quiet --unit="gts9u-sensors-hook-cycle-$$" "$sleep_bin" suspend >> "$log" 2>&1; then
		rc=0
	else
		rc=$?
	fi
	t0=$(now)
	restore
	trap - EXIT INT TERM
	say "systemd-sleep returned $rc; pm_test $(sed -n 's/.*\[\([a-z]*\)\].*/\1/p' "$power/pm_test"), drop-in removed"
	journalctl -o cat --no-pager -t systemd-sleep --since "$since" 2>/dev/null | while IFS= read -r l; do say "journal: $l"; done
fi

# --- the sensors back ------------------------------------------------------------
# The post step queues a restart of the resume unit (--no-block): first a new
# invocation of it, then its end.
waited=0
while [ "$waited" -lt 30 ] && [ "$(invocation)" = "$before" ]; do
	sleep 1
	waited=$((waited + 1))
done
restarted=1
[ "$(invocation)" != "$before" ] || restarted=0
while [ "$restarted" = 1 ] && [ "$waited" -lt "$wait_max" ]; do
	case $(systemctl is-active "$unit" 2>/dev/null) in
	activating|deactivating|reloading) ;;
	*) break ;;
	esac
	sleep 1
	waited=$((waited + 1))
done
t1=$(now)
took=$(awk -v a="$t0" -v b="$t1" 'BEGIN {printf "%.0f", b - a}')
result=$(systemctl show -p Result --value "$unit" 2>/dev/null)
restarts=$(systemctl show -p NRestarts --value "$unit" 2>/dev/null)
journalctl -o cat --no-pager -t ubuntu-gts9u-sensors-resume --since "$since" 2>/dev/null | while IFS= read -r l; do say "journal: $l"; done
ready=$(journalctl -o short-unix --no-pager -t ubuntu-gts9u-sensors-resume --since "$since" 2>/dev/null |
	awk -v a="$t0" '/SSC ready after attempt/ {printf "SSC ready %.1f s after it", $1 - a; exit}')
isp=$(systemctl is-active iio-sensor-proxy.service 2>/dev/null)
has=$(accel)
seen="${ready:-no SSC ready line}; $unit result ${result:-?}, restarts ${restarts:-?}; iio-sensor-proxy $isp, HasAccelerometer ${has:-?}"
if [ "$restarted" = 0 ]; then
	say "SENSORS NOT BACK after $took s (the post step did not start $unit again; $seen)"
	exit 1
elif [ "$waited" -ge "$wait_max" ]; then
	say "SENSORS NOT BACK after $took s ($unit still running after ${wait_max}s, a retry may still bring them: sh $kit/verify.sh later; $seen)"
	exit 1
elif [ "$result" = success ] && [ "$isp" = active ] && [ "$has" = true ]; then
	say "SENSORS BACK after $took s ($seen)"
else
	say "SENSORS NOT BACK after $took s ($seen)"
	exit 1
fi
