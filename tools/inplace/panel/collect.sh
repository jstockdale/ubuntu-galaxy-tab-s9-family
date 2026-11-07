#!/bin/sh
# Root-only evidence of one DISP-5 boot (or one moment of it), saved where
# group adm can read it over plain SSH.  Read-only apart from the files it
# writes: the kernel ring buffer (the journal of this board loses the first
# ~40 s), the display's power state, the MDSS GDSC, a few display clock
# controller registers, panel supplies, clocks and GPIOs, and report.py's
# analysis of it all.
#
#   sudo sh ~/panel/collect.sh <label>
#   -> /var/log/gts9u-panel-exp/<boot id>/<NN>-<label>/  (report.txt first)
set -u

kit=$(cd "$(dirname "$0")" && pwd)
[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
label=$(printf '%s' "${1:-snapshot}" | tr -c 'A-Za-z0-9._-' '_')
boot=$(cut -c1-8 /proc/sys/kernel/random/boot_id)
base=/var/log/gts9u-panel-exp
mkdir -p "$base/$boot"
seq=$(find "$base/$boot" -mindepth 1 -maxdepth 1 -type d | wc -l)
out=$base/$boot/$(printf '%02d' $((seq + 1)))-$label
mkdir -p "$out"
d=/sys/kernel/debug
dev=/sys/bus/platform/devices

dmesg > "$out/dmesg.txt"
cp /proc/cmdline "$out/cmdline.txt"

{
	echo "time $(date -Is), uptime $(cut -d' ' -f1 /proc/uptime) s, boot $boot"
	echo "panel_id: $(cat /sys/class/backlight/ae94000.dsi.0/panel_id 2>&1)"
	echo "actual_brightness: $(cat /sys/class/backlight/ae94000.dsi.0/actual_brightness 2>&1)"
	for c in /sys/class/drm/card*-DSI-1; do
		[ -d "$c" ] && echo "${c##*/}: status $(cat "$c/status") enabled $(cat "$c/enabled") dpms $(cat "$c/dpms")"
	done
	echo "active VT: $(cat /sys/class/tty/tty0/active)"
	echo "state_synced: dispcc $(cat "$dev/af00000.clock-controller/state_synced" 2>&1)," \
		"mdss_gdsc $(cat /sys/devices/genpd_provider/mdss_gdsc/state_synced 2>&1)"
	for p in ae00000.display-subsystem ae01000.display-controller ae94000.dsi \
		ae95000.phy ae90000.displayport-controller af00000.clock-controller; do
		echo "$p: $(cat "$dev/$p/power/runtime_status" 2>&1)" \
			"active_ms $(cat "$dev/$p/power/runtime_active_time" 2>&1)" \
			"suspended_ms $(cat "$dev/$p/power/runtime_suspended_time" 2>&1)"
	done
	for u in ubuntu-gts9u-panel-coldboot-recover.service ubuntu-gts9u-icc-late-release.service \
		display-manager.service gts9u-usb-gadget.service; do
		echo "$u: $(systemctl show -p LoadState -p ActiveState -p Result --value "$u" 2>&1 | tr '\n' ' ')"
	done
	echo "suspend_stats: success $(cat /sys/power/suspend_stats/success) fail $(cat /sys/power/suspend_stats/fail)"
	# The two other things the boot-time cycle is known to carry.
	echo "wifi: $(ip -br link show wlp1s0 2>&1 | head -n1)" \
		"(gts9u-pci-rescan: $(systemctl show -p Result --value gts9u-pci-rescan.service 2>&1))"
	echo "dp hpd replay: $(dmesg | grep -c 'replaying deferred Type-C DP HPD') line(s)"
} > "$out/state.txt" 2>&1

cp "$d/pm_genpd/pm_genpd_summary" "$out/genpd.txt" 2>/dev/null
for g in mdss_gdsc mmcx; do
	[ -d "$d/pm_genpd/$g" ] || continue
	for f in current_state idle_states active_time total_idle_time; do
		echo "== $g/$f"
		cat "$d/pm_genpd/$g/$f" 2>&1
	done
done > "$out/genpd-mdss.txt" 2>&1

# Display clock controller registers, through regmap's debugfs.  One line per
# register ("<addr>: <value>"), so a register is read on its own by seeking to
# its line; the line is checked before it is believed.  9000 MDSS GDSCR (bit
# 31 PWR_ON, 1 HW_CTRL, 0 SW_COLLAPSE), b000 INT2 GDSCR, 8000/a000 their
# BCRs, f000 DISP_CC_MISC_CMD (bit 0 masks the reset into DSI PHY0 during a
# GDSC collapse, bits 5/7/9 DSI0 pixel/byte/PHY clock gating), which mainline
# never writes.  regmap reads take no runtime-PM reference: with the clock
# controller runtime-suspended its MMCX vote may be gone and a read could
# fault, so they are only made while it is active (it stayed active through
# every blank on Stage 2).
{
	r=$(ls -d "$d"/regmap/*af00000.clock-controller* 2>/dev/null | head -n1)
	rpm=$(cat "$dev/af00000.clock-controller/power/runtime_status" 2>/dev/null)
	if [ "$rpm" != active ]; then
		echo "dispcc runtime_status '$rpm': registers not read"
	elif [ -z "$r" ] || [ ! -r "$r/registers" ]; then
		echo 'no dispcc regmap in debugfs'
	else
		first=$(head -n1 "$r/registers")
		addr=${first%%:*}
		width=${#addr}
		len=$((width + 2 + 8 + 1))
		if [ "$first" != "$(printf '%0*x: %s' "$width" 0 "${first#*: }")" ] ||
			[ "$(head -c "$len" "$r/registers" | wc -c)" != "$len" ]; then
			echo "unexpected regmap layout: $first"
		else
			for reg in 8000 9000 a000 b000 f000; do
				want=$(printf '%0*x' "$width" "0x$reg")
				line=$(dd if="$r/registers" bs="$len" skip=$((0x$reg / 4)) count=1 status=none 2>/dev/null | head -n1)
				case "$line" in
					"$want: "*) echo "$line" ;;
					*) echo "$want: not at the expected place (got '$line')" ;;
				esac
			done
		fi
	fi
} > "$out/dispcc.txt" 2>&1

cp "$d/regulator/regulator_summary" "$out/regulators.txt" 2>/dev/null
grep -E 'disp_cc|dsi0|dsi1|mdss' "$d/clk/clk_summary" > "$out/clk.txt" 2>/dev/null
cp "$d/gpio" "$out/gpio.txt" 2>/dev/null

python3 "$kit/report.py" "$out" > "$out/report.txt" 2>&1

chgrp -R adm "$base"
chmod 2750 "$base" "$base/$boot" "$out"
find "$out" -type f -exec chmod 0640 {} +
echo "saved $out ($(wc -l < "$out/dmesg.txt") dmesg lines)"
head -n 120 "$out/report.txt"
[ "$(wc -l < "$out/report.txt")" -le 120 ] || echo "... (full report: $out/report.txt)"
