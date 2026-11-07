#!/bin/sh
# Watch charging for a few minutes, as the desktop user (no sudo).
#
#   sh charge-watch.sh [minutes] [label]      default 5 minutes
#
# Every 5 s: battery status, current_now/avg, voltage, capacity, temperature;
# the SM5714 charger's online/type/limits; TCPM's online/type/contract; and
# UPower's state and rate.  Afterwards: how many SM5714 "enabled charging"
# lines, SM5440 lines and status changes the window produced.
#
# Before the powerthermal kernel, on the PC's USB port, expect one "enabled
# charging" line every ~31 s.  After it: none once the boot has settled, one
# "no USB-PD PPS source" debug line at most, and no "start failed: -95".
set -eu

minutes=${1:-5}
label=${2:-run}
case "$minutes" in ''|*[!0-9]*) echo "usage: charge-watch.sh [minutes] [label]" >&2; exit 2 ;; esac
[ "$(id -u)" != 0 ] || { echo 'run as the desktop user, not root' >&2; exit 1; }

out=$HOME/powerthermal-logs
mkdir -p "$out"
stamp=$(date +%Y%m%d-%H%M%S)
csv=$out/charge-$label-$stamp.csv
sumf=$out/charge-$label-$stamp.summary.txt
bat=/sys/class/power_supply/sm5714-battery
usb=/sys/class/power_supply/sm5714-usb
tcpm=$(ls -d /sys/class/power_supply/tcpm-source-psy-* 2>/dev/null | head -n 1)

r() { cat "$1" 2>/dev/null | tr -d '\n' || true; }
# The bracketed entry of a usb_type list, e.g. "C [PD] PD_PPS" -> PD.
sel() { r "$1" | sed -n 's/.*\[\([^]]*\)\].*/\1/p'; }
upower_field() {
	upower -i /org/freedesktop/UPower/devices/battery_sm5714_battery 2>/dev/null |
		awk -v k="$1" '$1 == k":" { $1 = ""; sub(/^ /, ""); print; exit }'
}

since=$(date +%s)
echo "time,status,current_now_ua,current_avg_ua,voltage_uv,capacity,temp_dc,usb_online,usb_type,usb_max_ua,usb_max_uv,tcpm_online,tcpm_type,tcpm_uv,tcpm_ua,upower_state,upower_rate" > "$csv"
end=$(( since + minutes * 60 ))
echo "logging to $csv for $minutes min (Ctrl-C ends early)"
trap 'end=0' INT TERM
while [ "$(date +%s)" -lt "$end" ]; do
	line="$(date +%T),$(r $bat/status),$(r $bat/current_now),$(r $bat/current_avg),$(r $bat/voltage_now),$(r $bat/capacity),$(r $bat/temp)"
	line="$line,$(r $usb/online),$(sel $usb/usb_type),$(r $usb/current_max),$(r $usb/voltage_max)"
	if [ -n "$tcpm" ]; then
		line="$line,$(r "$tcpm/online"),$(sel "$tcpm/usb_type"),$(r "$tcpm/voltage_now"),$(r "$tcpm/current_now")"
	else
		line="$line,,,,"
	fi
	line="$line,$(upower_field state),$(upower_field energy-rate | tr -d ' W')"
	echo "$line" >> "$csv"
	echo "$line"
	sleep 5 || true
done
trap - INT TERM

klog=$(journalctl -k --no-pager -o short-monotonic --since "@$since" 2>/dev/null || true)
count() { printf '%s\n' "$klog" | grep -c -e "$1" || true; }
{
	echo "charge watch $label $stamp ($minutes min): $csv"
	echo "kernel lines in the window:"
	echo "  sm5714 'enabled charging':        $(count 'enabled charging')"
	echo "  sm5440 'direct-charge start failed': $(count 'direct-charge start failed')"
	echo "  sm5440 'direct charge started':   $(count 'direct charge started')"
	echo "  sm5440 'stopping direct charge':  $(count 'stopping direct charge')"
	echo "  sm5440 'has no PPS profile':      $(count 'has no PPS profile') (a PD charger without PPS; once per plug-in)"
	echo "  sm5714-usbpd 'USB-PD contract':   $(count 'USB-PD contract')"
	echo "status values seen: $(cut -d, -f2 "$csv" | tail -n +2 | sort | uniq -c | tr '\n' ' ')"
	echo "UPower states seen: $(cut -d, -f16 "$csv" | tail -n +2 | sort | uniq -c | tr '\n' ' ')"
	neg_chg=$(awk -F, 'NR > 1 && $2 == "Charging" && $4 < -100000' "$csv" | wc -l)
	echo "samples 'Charging' with current_avg < -100 mA: $neg_chg (expected 0 with the powerthermal kernel)"
	p=/sys/module/sm5714_battery/parameters/report_net_drain
	if [ -r "$p" ]; then
		echo "report_net_drain parameter: $(r "$p")"
	else
		echo 'report_net_drain parameter: absent (kernel without the powerthermal commits)'
	fi
} | tee "$sumf"
