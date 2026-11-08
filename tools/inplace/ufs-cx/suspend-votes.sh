#!/bin/sh
# Show that the UFS and USB controllers drop their CX votes for system sleep
# and take them back on resume (the ufshcd and dwc3-qcom patches).
#
#   sudo sh ~/ufs-cx/suspend-votes.sh
#
# Not read-only: it runs one pm_test=devices cycle (the device suspend and
# resume callbacks, no real sleep, about 5 s) with two kprobe events on the
# functions the patches call, then removes them and sets pm_test back to none.
set -u
export LC_ALL=C
t=/sys/kernel/tracing
p=/sys/power
fail=0
ok() { echo "ok   $*"; }
bad() { echo "FAIL $*"; fail=1; }

[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9wifi' ||
	{ echo 'this is not an SM-X710' >&2; exit 1; }
[ -w "$t/dynamic_events" ] || { echo "no $t/dynamic_events" >&2; exit 1; }
grep -qw devices "$p/pm_test" || { echo 'this kernel has no pm_test' >&2; exit 1; }

cleanup() {
	echo none 2>/dev/null > "$p/pm_test"
	echo 0 2>/dev/null > "$t/events/cxvote/enable"
	echo '-:cxvote/opp' 2>/dev/null >> "$t/dynamic_events"
	echo '-:cxvote/ps' 2>/dev/null >> "$t/dynamic_events"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
cleanup
echo 'p:cxvote/opp dev_pm_opp_set_opp dev=dev->kobj.name:string opp=opp:x64' >> "$t/dynamic_events" ||
	{ echo 'cannot add the dev_pm_opp_set_opp probe' >&2; exit 1; }
echo 'p:cxvote/ps dev_pm_genpd_set_performance_state dev=dev->kobj.name:string state=state:u32' >> "$t/dynamic_events" ||
	{ echo 'cannot add the dev_pm_genpd_set_performance_state probe' >&2; exit 1; }
echo > "$t/trace"
echo 1 > "$t/events/cxvote/enable"
# dwc3_pm_resume() marks the controller active either way; what it held
# before the cycle decides what to expect.
usb_rpm=$(cat /sys/bus/platform/devices/a600000.usb/power/runtime_status)

echo 'pm_test=devices cycle (about 5 s)...'
echo devices > "$p/pm_test"
echo mem > "$p/state" || bad 'the pm_test cycle returned an error'
echo none > "$p/pm_test"
echo 0 > "$t/events/cxvote/enable"

# Trace lines name the event without its group: " opp: (…) dev=… opp=…".
# Keep the cycle's raw trace beside this script, readable without root.
raw=$(dirname "$0")/suspend-votes-trace.txt
grep -E ' (opp|ps): ' "$t/trace" > "$raw"
chmod 0644 "$raw"
ufs=$(grep -E ' opp: .*dev="1d84000\.ufshc"' "$raw")
usb=$(grep -E ' ps: .*dev="a600000\.usb"' "$raw")
echo "trace: $(wc -l < "$raw") probe hits, kept in $raw"
printf '%s\n%s\n' "$ufs" "$usb" | sed '/^$/d'
# Counts, not grep -q at the end of a pipe: an early exit there can make the
# pipe fail under pipefail.
count() { printf '%s\n' "$1" | grep -cE "$2"; }
[ "$(count "$ufs" 'opp=0x0( |$)')" -gt 0 ] && ok 'UFS dropped its OPP votes' ||
	bad 'no UFS OPP drop (opp=0x0) during the cycle'
[ "$(count "$ufs" 'opp=0x[1-9a-f][0-9a-f]*( |$)')" -gt 0 ] && ok 'UFS restored an OPP' ||
	bad 'no UFS OPP restore during the cycle'
if [ "$usb_rpm" = suspended ]; then
	[ "$(count "$usb" 'state=0( |$)')" = 0 ] &&
		ok 'USB was runtime-suspended: it had no vote to drop' ||
		bad 'USB dropped a vote it should not have held (it was runtime-suspended)'
else
	[ "$(count "$usb" 'state=0( |$)')" -gt 0 ] && ok 'USB dropped its vote' ||
		bad 'no USB vote drop (state=0) during the cycle'
fi
[ "$(count "$usb" 'state=256( |$)')" -gt 0 ] && ok 'USB voted again on resume' ||
	bad 'no USB vote (state=256) on resume'

n=$(journalctl -k -b --since '-2min' --no-pager -q 2>/dev/null |
	grep -ciE 'failed to (drop|restore) the OPP votes|failed to set performance state')
[ "$n" = 0 ] && ok 'no vote errors in the kernel log' || bad "$n vote errors in the kernel log"
[ "$fail" = 0 ] && echo 'all checks passed' || echo 'some checks FAILED'
exit "$fail"
