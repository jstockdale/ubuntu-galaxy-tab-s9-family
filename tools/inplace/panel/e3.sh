#!/bin/sh
# E3: one display-only blank/unblank, on a boot whose cold-boot cycle did not
# run (panel-e0) and whose panel therefore still reads 00 00 00.
#
#   sudo sh ~/panel/e3.sh
#
# Since the DP controller stopped holding the MDSS (fix1/fix2 kernels) and
# the display clock controller reaches sync_state, a blank runtime-suspends
# the whole display and powers the MDSS GDSC down; the unblank brings it up
# and runs the panel's full power-on.  That is the platform cycle's display
# path without freezing anything.  The blank is driven through the console's
# framebuffer, so no one has to log in to a dark screen: switch to VT 3 (the
# login screen on VT 1 hands the display over), blank fb0 (CRTC off, panel
# unprepared, MDSS suspended), check the GDSC is off, unblank, read the id.
# collect.sh snapshots before and after.  Ends on VT 3: if the panel answered,
# the text console is on the glass; go back with the chvt line it prints.
set -u

kit=$(cd "$(dirname "$0")" && pwd)
[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
log=/var/log/gts9u-panel-exp.log
pid=/sys/class/backlight/ae94000.dsi.0/panel_id
mdss=/sys/bus/platform/devices/ae00000.display-subsystem/power/runtime_status
gd=/sys/kernel/debug/pm_genpd/mdss_gdsc
fb=/sys/class/graphics/fb0/blank

say() { echo "e3: $*" | tee -a "$log"; }
usage() { awk 'NR == 2 { print $3 }' "$gd/idle_states" 2>/dev/null; }
id_lines() { dmesg | grep -c 'ana38407 panel id:'; }

echo "=== $(date -Is) e3 (boot $(cut -c1-8 /proc/sys/kernel/random/boot_id))" >> "$log"
id_before=$(cat "$pid" 2>/dev/null)
if ! grep -q 'systemd.mask=ubuntu-gts9u-panel-coldboot-recover.service' /proc/cmdline; then
	say 'this boot did not mask the cold-boot cycle; E3 needs a panel-e0 boot'
	[ "${FORCE:-0}" = 1 ] || exit 2
fi
case "$id_before" in
	'00 00 00') ;;
	*) say "the panel already answers ($id_before): nothing for E3 to recover"
	   [ "${FORCE:-0}" = 1 ] || exit 2 ;;
esac
synced=$(cat /sys/devices/genpd_provider/mdss_gdsc/state_synced 2>/dev/null)
[ "$synced" = 1 ] || say "mdss_gdsc state_synced=$synced: runtime PM keeps the GDSC on, expect INCONCLUSIVE"
[ -w "$fb" ] || { say "no $fb"; exit 1; }

sh "$kit/collect.sh" e3-before > /dev/null
vt_before=$(cat /sys/class/tty/tty0/active)
usage_before=$(usage)
lines_before=$(id_lines)
say "before: panel id '$id_before', VT $vt_before, MDSS $(cat "$mdss"), mdss_gdsc $(cat "$gd/current_state" 2>/dev/null) usage ${usage_before:-?}"

timeout 15 chvt 3 || say 'chvt 3 did not complete'
sleep 3
# The console takes the display back with its own mode; if that differs from
# the login screen's, the switch was a modeset that re-powered the panel (no
# GDSC collapse inside one commit).  Record it, and wait for the unblank's
# power-on from here.
lines_vt=$(id_lines)
id_vt=$(cat "$pid" 2>/dev/null)
if [ "$lines_vt" -gt "$lines_before" ]; then
	say "the switch to VT 3 re-powered the panel (console modeset): panel id '$id_vt'"
fi
lines_before=$lines_vt
echo 4 > "$fb" || say 'fb0 blank write failed'
n=0
while [ "$(cat "$mdss")" != suspended ] && [ "$n" -lt 40 ]; do
	sleep 0.5
	n=$((n + 1))
done
sleep 2
mdss_blank=$(cat "$mdss")
gdsc_blank=$(cat "$gd/current_state" 2>/dev/null)
usage_blank=$(usage)
say "blanked: MDSS $mdss_blank after $((n / 2)) s, mdss_gdsc $gdsc_blank usage ${usage_blank:-?}"

echo 0 > "$fb" || say 'fb0 unblank write failed'
n=0
while [ "$(id_lines)" -le "$lines_before" ] && [ "$n" -lt 20 ]; do
	sleep 0.5
	n=$((n + 1))
done
sleep 1
id_after=$(cat "$pid" 2>/dev/null)
say "unblanked: panel id '$id_after', connector dpms $(cat /sys/class/drm/card*-DSI-1/dpms 2>/dev/null | head -n1)"
sh "$kit/collect.sh" e3-after > /dev/null

collapsed=no
if [ "$mdss_blank" = suspended ] && { [ "${gdsc_blank%%-*}" = off ] ||
	{ [ -n "$usage_before" ] && [ -n "$usage_blank" ] && [ "$usage_blank" -gt "$usage_before" ]; }; }; then
	collapsed=yes
fi
if [ "$id_vt" = '80 00 04' ]; then
	say 'E3 NOT TESTED: the console modeset of the VT switch (panel power cycle, no GDSC collapse) already recovered the DDIC; note it (panel side)'
elif [ "$collapsed" = yes ] && [ "$id_after" = '80 00 04' ]; then
	say 'E3 PASS: a runtime blank/unblank (MDSS GDSC off, panel supplies cycled) recovered the DDIC; no system suspend'
elif [ "$collapsed" = yes ]; then
	say "E3 FAIL: the GDSC went off and the panel was powered again, but it still answers '$id_after'"
elif [ "$id_after" = '80 00 04' ]; then
	say 'E3 RECOVERED WITHOUT A GDSC COLLAPSE: the panel power cycle alone was enough (panel side)'
else
	say "E3 INCONCLUSIVE: the MDSS did not power down (MDSS $mdss_blank, GDSC $gdsc_blank); see README fallback"
fi
say "the console is on VT 3 now; back to the login screen: sudo chvt ${vt_before#tty}"
say "evidence: $(ls -d /var/log/gts9u-panel-exp/"$(cut -c1-8 /proc/sys/kernel/random/boot_id)"/*-e3-after)"
