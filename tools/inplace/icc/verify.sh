#!/bin/sh
# What the interconnect late release did on this boot, without root (the owner
# is in group adm, which reads the release log and kern.log).  Each line is
# "ok", "CHECK" (look at it) or "info".
#
#   sh ~/icc/verify.sh
set -u

name=ubuntu-gts9u-icc-late-release
helper=/usr/libexec/$name
record=/run/ubuntu-gts9u/icc-release
dev=/sys/bus/platform/devices
ok() { echo "  ok     $*"; }
ck() { echo "  CHECK  $*"; }
inf() { echo "  info   $*"; }
hdr() { echo "== $*"; }
synced() { cat "$dev/$1/state_synced" 2>/dev/null || echo n/a; }
prop() { systemctl show -p "$2" --value "$1" 2>/dev/null; }
val() { sed -n "s/^$1=//p" "$record" 2>/dev/null; }

hdr boot
inf "boot $(cat /proc/sys/kernel/random/boot_id), up $(cut -d. -f1 /proc/uptime) s, $(uname -r)"
inf "cmdline extras: $(tr ' ' '\n' < /proc/cmdline | grep -E '^(initcall_blacklist|icc_core|fw_devlink|clk_ignore|pd_ignore|regulator_ignore)' | tr '\n' ' ')"
if tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9wifi'; then
	ok 'SM-X710 (samsung,gts9wifi)'
else
	ck 'not an SM-X710: the unit is meant to be skipped here'
fi

hdr unit
[ -x "$helper" ] && ok "$helper present" || ck "$helper missing"
pkg=$(dpkg-query -S "$helper" 2>/dev/null | cut -d: -f1)
inf "installed by: ${pkg:-the kit (no package owns it)}"
en=$(systemctl is-enabled "$name.service" 2>&1)
[ "$en" = enabled ] && ok "$name.service enabled" || ck "$name.service is $en"
[ -e /etc/ubuntu-gts9u/icc-hold ] && ck '/etc/ubuntu-gts9u/icc-hold exists: the floor is kept on purpose'
inf "unit: $(prop "$name.service" ActiveState) / $(prop "$name.service" Result), condition $(prop "$name.service" ConditionResult)"
cyc_exit=$(prop ubuntu-gts9u-panel-coldboot-recover.service ExecMainExitTimestampMonotonic)
rel_start=$(prop "$name.service" ExecMainStartTimestampMonotonic)
inf "panel cycle unit: $(prop ubuntu-gts9u-panel-coldboot-recover.service ActiveState) / $(prop ubuntu-gts9u-panel-coldboot-recover.service Result), returned at $(( ${cyc_exit:-0} / 1000000 )) s"
if [ "${rel_start:-0}" != 0 ] && [ "${cyc_exit:-0}" != 0 ]; then
	if [ "$rel_start" -gt "$cyc_exit" ]; then
		ok "release ran at $(( rel_start / 1000000 )) s, after the panel cycle returned"
	else
		ck "release started at $(( rel_start / 1000000 )) s, BEFORE the panel cycle returned at $(( cyc_exit / 1000000 )) s"
	fi
fi

hdr 'this boot'
if [ -r "$record" ]; then
	if [ "$(val boot_id)" = "$(cat /proc/sys/kernel/random/boot_id)" ]; then
		case "$(val result)" in
		released) ok "released $(val released) at $(val uptime) s; INT_MAX nodes $(val intmax_before) -> $(val intmax_after)" ;;
		nothing) ok 'nothing to release: every provider synced by itself' ;;
		held) ck "held: $(val reason)" ;;
		*) ck "result $(val result): $(val reason)" ;;
		esac
		case "$(val result):$(val intmax_after)" in
		released:0) ok 'no interconnect node left at INT_MAX' ;;
		released:*) ck "INT_MAX nodes after the release: $(val intmax_after) (expected 0)" ;;
		esac
	else
		ck "$record is from another boot"
	fi
else
	ck "no release record ($record): the unit has not run this boot"
fi
line=
for p in 24100000.interconnect interconnect-1; do line="$line $p=$(synced "$p")"; done
case "$line" in
*=0*) inf "state_synced:$line (the floor is still held)" ;;
*) ok "state_synced:$line" ;;
esac
other="rpmhpd=$(synced 17a00000.rsc:power-controller) gcc=$(synced 100000.clock-controller) gpu_cc=$(synced 3d90000.clock-controller) videocc=$(synced aaf0000.clock-controller)"
case "$other" in
*=0*) ck "untouched providers: $other (rpmhpd=0 brings the GPU write loss back)" ;;
*) ok "untouched providers: $other" ;;
esac
for d in 24091000.pmu 240b6400.pmu; do
	drv=$(basename "$(readlink "$dev/$d/driver" 2>/dev/null)" 2>/dev/null)
	inf "$d: driver '${drv:-none}'"
done
inf "suspend_stats success/fail $(cat /sys/power/suspend_stats/success)/$(cat /sys/power/suspend_stats/fail), mem_sleep $(cat /sys/power/mem_sleep)"

hdr logs
if [ -r /var/log/ubuntu-gts9u-icc-release.log ]; then
	tail -n 8 /var/log/ubuntu-gts9u-icc-release.log | sed 's/^/  info   /'
else
	inf '/var/log/ubuntu-gts9u-icc-release.log not readable (or absent)'
fi
if [ -r /var/log/kern.log ]; then
	grep -a "$name" /var/log/kern.log | tail -n 2 | sed 's/^/  info   /'
fi
# Anything alarming in the kernel log since the release marker (journal, adm).
# The journal of this board usually starts at ~40 s, after the boot-time
# release, so without the marker the whole retained boot is scanned.
kern=$(journalctl -k -b --no-pager -o short-monotonic 2>/dev/null)
if echo "$kern" | grep -q "$name: releasing"; then
	kern=$(echo "$kern" | awk -v m="$name: releasing" 'index($0, m) {f = 1} f')
	scope='since the release'
else
	scope="in this boot's retained kernel journal (from $(echo "$kern" | sed -n '1{s/^\[ *\([0-9]*\)\..*/\1/p;q}') s; the release marker is not in it)"
fi
sig='rpmh.*(timeout|fail)|tcs.*(timeout|busy)|noc[^a-z]*err|Unhandled context fault|smmu.*fault|gmu.*(timeout|fail)|hangcheck|ufshcd.*(abort|err|fail|timeout|reset)|AER: (Uncorrected|Multiple)|PCIe Bus Error: severity=(Uncorrected|Fatal)|soft lockup|hung_task|Call trace:'
bad=$(echo "$kern" | grep -E -i "$sig" | tail -n 5)
if [ -z "$bad" ]; then
	ok "no interconnect/RPMh/UFS/PCIe/GPU error signatures $scope"
else
	ck "error signatures $scope:"
	echo "$bad" | sed 's/^/           /'
fi
# A correctable WCN6855 link error shows up on boots without any release too
# (boot df92b567, 312 s): counted, not judged.
n=$(echo "$kern" | grep -c 'PCIe Bus Error: severity=Correctable')
[ "$n" = 0 ] || inf "correctable PCIe link errors $scope: $n (seen without a release as well)"
late=$(echo "$kern" | grep -c 'GX_BW_PERF_VOTE.*timed out')
[ "$late" = 0 ] || inf "late GMU answers to GPU bandwidth votes $scope: $late (known, also with the floor held)"
