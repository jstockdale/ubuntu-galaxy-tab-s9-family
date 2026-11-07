#!/bin/sh
# Check the powerthermal work on the running system, as the desktop user.
#
#   sh ~/powerthermal/verify.sh
#
# "ok" lines are what the change should give; "CHECK" lines need a look (each
# says why); "info" lines are context.  Read-only: it writes nothing.
#
# Userspace part (after install.sh, then a logout/login or a reboot):
#   rtkit installed and answering, PipeWire's data loops and Mutter's KMS
#   thread realtime, the greeter's WirePlumber snippets in place and, from the
#   next boot, only one WirePlumber registering Bluetooth endpoints.
# Kernel part (after the next kernel build with the powerthermal commits):
#   no SM5440/SM5714 charging loop, the net-drain status parameter, gpuss-3..6
#   registered, the 95 C CPU cooling maps bound, the xo/ap thermistors readable.
set -u

ok() { echo "ok     $*"; }
check() { echo "CHECK  $*"; }
info() { echo "info   $*"; }

me=$(id -un)
up=$(cut -d. -f1 /proc/uptime)
info "kernel $(uname -r), up ${up}s, boot $(cat /proc/sys/kernel/random/boot_id)"

echo '--- rtkit and realtime'
if dpkg-query -W -f='${Status}\n' rtkit 2>/dev/null | grep -q 'ok installed'; then
	ok "rtkit $(dpkg-query -W -f='${Version}' rtkit) installed"
else
	check 'rtkit is not installed (sudo sh ~/powerthermal/install.sh)'
fi
state=$(systemctl is-active rtkit-daemon.service 2>/dev/null || true)
[ "$state" = active ] && ok 'rtkit-daemon active' ||
	info "rtkit-daemon $state (it is D-Bus activated; PipeWire starts it at login)"

pw=$(pgrep -u "$me" -x pipewire | head -n 1 || true)
if [ -n "$pw" ]; then
	cls=$(ps -L -o cls=,rtprio=,comm= -p "$pw" | awk '$3 == "pw-data-loop" { print $1 "/" $2; exit }')
	case "$cls" in
	FF/*|RR/*) ok "pipewire $pw data loop runs $cls" ;;
	*) check "pipewire $pw data loop runs ${cls:-?} (expected FF/n): log out and back in, or reboot, after installing rtkit" ;;
	esac
	rterr=$(journalctl --user -b --no-pager _PID="$pw" 2>/dev/null | grep -c 'RTKit error' || true)
	[ "$rterr" = 0 ] && ok "no 'RTKit error' from this pipewire" ||
		check "this pipewire logged 'RTKit error' $rterr times (started before rtkit?)"
else
	info 'no pipewire running for this user'
fi
gs=$(pgrep -u "$me" -x gnome-shell | head -n 1 || true)
if [ -n "$gs" ]; then
	kms=$(ps -L -o cls=,rtprio=,comm= -p "$gs" | awk '/KMS thread/ { print $1 "/" $2; exit }')
	case "$kms" in
	FF/*|RR/*) ok "gnome-shell KMS thread runs $kms" ;;
	'') info 'gnome-shell has no KMS thread' ;;
	*) check "gnome-shell KMS thread runs $kms (expected FF/n after a new login with rtkit)" ;;
	esac
fi

echo '--- greeter WirePlumber'
gh=$(getent passwd gdm | cut -d: -f6)
lua=$gh/.config/wireplumber/bluetooth.lua.d/51-gts9u-greeter-no-bluez.lua
if [ -n "$gh" ] && [ -r "$lua" ]; then
	ok "greeter snippet present ($lua)"
	installed=$(stat -c %Y "$lua")
	booted=$(( $(date +%s) - up ))
	if [ "$installed" -lt "$booted" ]; then
		fails=$(journalctl --user -b --no-pager -u wireplumber 2>/dev/null | grep -c 'RegisterProfile() failed' || true)
		[ "$fails" = 0 ] && ok "this boot: no 'RegisterProfile() failed' in your WirePlumber" ||
			check "this boot: your WirePlumber logged 'RegisterProfile() failed' $fails times"
		senders=$(journalctl -b --no-pager -u bluetooth 2>/dev/null |
			sed -n 's/.*Endpoint registered: sender=\([^ ]*\).*/\1/p' | sort -u | wc -l)
		[ "$senders" -le 1 ] && ok "this boot: Bluetooth endpoints registered by $senders WirePlumber" ||
			check "this boot: $senders D-Bus senders registered Bluetooth endpoints (expected 1)"
	else
		info 'installed during this boot: the greeter picks it up at the next boot'
	fi
else
	check "greeter snippet missing ($lua)"
fi

echo '--- charging (kernel)'
param=/sys/module/sm5714_battery/parameters/report_net_drain
if [ -r "$param" ]; then
	ok "powerthermal kernel: report_net_drain=$(cat "$param")"
	if [ "$up" -gt 300 ]; then
		n=$(journalctl -k --no-pager --since "-5min" 2>/dev/null | grep -c 'enabled charging' || true)
		[ "$n" -le 1 ] && ok "'enabled charging' lines in the last 5 min: $n" ||
			check "'enabled charging' lines in the last 5 min: $n (the 30 s loop is back?)"
	else
		info 'up less than 5 min: the charging-loop count needs a settled boot'
	fi
	n=$(journalctl -k -b --no-pager 2>/dev/null | grep -c 'direct-charge start failed: -95' || true)
	[ "$n" = 0 ] && ok 'no "direct-charge start failed: -95" this boot' ||
		check "\"direct-charge start failed: -95\" $n times this boot"
else
	info 'kernel without the powerthermal charging commits; skipping the kernel checks'
fi
b=/sys/class/power_supply/sm5714-battery
info "battery: $(cat $b/status) $(cat $b/capacity)% current_now=$(cat $b/current_now) current_avg=$(cat $b/current_avg) uA"

echo '--- thermal (kernel)'
zone_of() { for z in /sys/class/thermal/thermal_zone*; do [ "$(cat "$z/type")" = "$1" ] && echo "$z" && return; done; }
missing=
for g in 3 4 5 6; do
	[ -n "$(zone_of gpuss-$g-thermal)" ] || missing="$missing gpuss-$g"
done
[ -z "$missing" ] && ok 'gpuss-3..6 thermal zones registered' ||
	info "missing thermal zones:$missing (expected before the thermal DT fix)"
# After the fix cdsp0-3 read tsens1 4-7: they must give plausible SoC
# temperatures (within a few degrees of the others at idle), not an error.
temps=
for t in cdsp0 cdsp1 cdsp2 cdsp3 gpuss-0 gpuss-3 gpuss-4 gpuss-5 gpuss-6 gpuss-7 cpu7-top; do
	z=$(zone_of "$t-thermal")
	[ -n "$z" ] || continue
	v=$(cat "$z/temp" 2>&1) || v="error:$v"
	temps="$temps $t=$v"
done
info "zone temps (mC):$temps"
z=$(zone_of cpu7-top-thermal)
if [ -n "$z" ] && [ -e "$z/cdev0" ]; then
	ok "cpu7-top-thermal bound to $(cat "$z/cdev0/type") at trip $(cat "$z/cdev0_trip_point")"
else
	info 'cpu7-top-thermal has no cooling device (expected before the thermal DT fix)'
fi
iio=/sys/bus/iio/devices/iio:device0
for ch in 68 324 328; do
	if [ -r "$iio/in_temp${ch}_input" ]; then
		info "$(cat "$iio/in_temp${ch}_label") $(cat "$iio/in_temp${ch}_input") mC"
	fi
done
for c in /sys/class/thermal/cooling_device*; do
	case "$(cat "$c/type")" in
	cpufreq-*|devfreq-*) info "$(cat "$c/type") state $(cat "$c/cur_state")/$(cat "$c/max_state")" ;;
	esac
done
info "LMh: $(grep dcvsh /proc/interrupts | awk '{ n = 0; for (i = 2; i <= 9; i++) n += $i; printf "%s=%d ", $NF, n }')"
