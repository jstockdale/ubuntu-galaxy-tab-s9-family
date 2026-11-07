#!/bin/sh
# Post-boot checks for the Wi-Fi kit.  No root needed; under sudo it adds the
# kernel-log checks (dmesg is restricted) and the vendor_boot hash.
#
#   sh ~/wifi-kit/verify.sh            this boot
#   sh ~/wifi-kit/verify.sh --soak N   then N seconds of pings to the gateway,
#                                      with AER counters and link states
#                                      compared before and after
#
# Every run appends one line (boot id, address, IPv4, first-probe outcome, L1
# states) to verify-history.tsv next to this script, so "the address is the
# same across reboots" is read straight off that file.
set -u

kit=$(cd "$(dirname "$0")" && pwd)
history=${WIFI_KIT_HISTORY:-$kit/verify-history.tsv}
ep=/sys/bus/pci/devices/0000:01:00.0
fails=0
ok() { echo "ok    $*"; }
bad() { echo "FAIL  $*"; fails=$((fails + 1)); }
info() { echo "info  $*"; }
kv() { sed -n "s/^$1=//p" "$2" 2>/dev/null | head -n 1; }

soak=0
case "${1:-}" in
'') ;;
--soak) soak=${2:-600} ;;
*) echo 'usage: verify.sh [--soak SECONDS]' >&2; exit 2 ;;
esac

boot=$(cat /proc/sys/kernel/random/boot_id)
info "boot $boot, up $(cut -d' ' -f1 /proc/uptime) s, kernel $(uname -r)"

# --- first probe ------------------------------------------------------------------
iface=$(ls "$ep/net" 2>/dev/null | head -n 1)
if [ -n "$iface" ]; then
	ok "Wi-Fi interface $iface on $(basename "$(readlink -f "$ep/driver")")"
else
	bad "no network interface under $ep"
fi
outcome=$(kv outcome /run/ubuntu-gts9u/pci-rescan)
case "$outcome" in
bound-at-boot) ok 'gts9u-pci-rescan: endpoint bound at boot (first probe succeeded, no rescan)' ;;
'') bad 'gts9u-pci-rescan left no /run/ubuntu-gts9u/pci-rescan (old unit, or it did not run)' ;;
*) bad "gts9u-pci-rescan outcome: $outcome (the first probe failed)" ;;
esac
if [ -n "$iface" ]; then
	# When udev finished with the interface, in seconds since boot: ~13-20 s
	# from the initramfs, ~22-30 s after a rescan.
	usec=$(udevadm info -q property "/sys/class/net/$iface" 2>/dev/null | sed -n 's/^USEC_INITIALIZED=//p')
	[ -n "$usec" ] && info "udev initialised $iface at $((usec / 1000000)) s"
fi
t0=$(systemctl show -P ExecMainStartTimestampMonotonic gts9u-pci-rescan.service 2>/dev/null)
t1=$(systemctl show -P ExecMainExitTimestampMonotonic gts9u-pci-rescan.service 2>/dev/null)
info "gts9u-pci-rescan ran $(echo "${t0:-0} ${t1:-0}" | awk '{ if ($2 > $1) printf "%.1f s from %.1f s", ($2 - $1) / 1e6, $1 / 1e6; else print "(no timestamps)" }')"

# --- address ------------------------------------------------------------------
addr=
want=
if [ -n "$iface" ]; then
	addr=$(cat "/sys/class/net/$iface/address")
	want=$(sed -n 's/^MACAddress=//p' /run/systemd/network/05-ubuntu-gts9u-wlan.link 2>/dev/null)
	if [ -z "$want" ]; then
		bad "no EFS address this boot (state: $({ tr '\n' ' ' < /run/ubuntu-gts9u/wlan-address; } 2>/dev/null || echo none))"
	elif [ "$addr" = "$want" ]; then
		ok "$iface address $addr = EFS ($(kv "$iface" /run/ubuntu-gts9u/wlan-address || :))"
	else
		# NetworkManager randomises the address for scans while disconnected.
		state=$(nmcli -g GENERAL.STATE device show "$iface" 2>/dev/null)
		bad "$iface address $addr, EFS says $want (NetworkManager: $state)"
	fi
	case "$addr" in 00:03:7f:*) bad "$addr is the Qualcomm default prefix" ;; esac
	link=$(udevadm info -q property "/sys/class/net/$iface" 2>/dev/null | sed -n 's/^ID_NET_LINK_FILE=//p')
	info "udev link file: $link"
fi
ip4=$( [ -n "$iface" ] && ip -4 -o addr show dev "$iface" 2>/dev/null | awk '{ print $4 }' | head -n 1)
info "IPv4 ${ip4:-none}"

# --- L1 substates ---------------------------------------------------------------------
states=
for a in l0s_aspm l1_aspm l1_1_aspm l1_2_aspm l1_1_pcipm l1_2_pcipm; do
	v=$(cat "$ep/link/$a" 2>/dev/null || echo -)
	states="$states$a=$v "
done
case "$states" in
*l1_1_aspm=1*l1_2_aspm=1*l1_1_pcipm=1*l1_2_pcipm=1*) ok "link: $states" ;;
*) bad "link: $states (L1.1/L1.2 not all on)" ;;
esac
[ -r /etc/ubuntu-gts9u/pcie-l1ss ] && info "L1SS policy override: $(cat /etc/ubuntu-gts9u/pcie-l1ss)"
aer() { for k in correctable nonfatal fatal; do printf '%s:%s ' "$k" "$(awk '/^TOTAL/ { print $2 }' "$ep/aer_dev_$k" 2>/dev/null)"; done; }
info "AER totals: $(aer)"
info "'msdu_done' warnings this boot (journal): $(journalctl -k -b --no-pager 2>/dev/null | grep -c 'msdu_done bit in attention')"

# --- root only ---------------------------------------------------------------------
if [ "$(id -u)" = 0 ]; then
	dmesg | grep -E 'ath11k|Direct firmware load for ath11k|regulatory.db' | head -n 25 | sed 's/^/dmesg /'
	if dmesg | grep -qE 'ath11k_pci .*(probe with driver ath11k_pci failed|failed to power up)'; then
		bad 'dmesg: the ath11k probe failed at least once this boot'
	else
		ok 'dmesg: no ath11k probe failure'
	fi
	vb=$(dd if=/dev/disk/by-partlabel/vendor_boot iflag=direct bs=4M count=24 status=none | sha256sum | cut -d' ' -f1)
	name=$(awk -v s="$vb" '$1 == s { print "base " $3 } $2 == s { print "Wi-Fi variant " $3 }' "$kit/IMAGES" 2>/dev/null)
	info "vendor_boot $(echo "$vb" | cut -c1-12): ${name:-not in IMAGES}"
fi

# date, boot id, address, EFS address (- before the kit), IPv4, rescan outcome, link
printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$(date '+%F %T')" "$boot" "${addr:--}" "${want:--}" \
	"${ip4:--}" "${outcome:--}" "$states" >> "$history" 2>/dev/null
echo
echo "history ($history):"
tail -n 8 "$history" 2>/dev/null
# Boots on which the kit was active (an EFS address was read): one address and
# one IPv4 lease across all of them is the point of the exercise.
kitboots=$(awk -F '\t' '$4 != "-"' "$history" 2>/dev/null | cut -f2 | sort -u | wc -l)
addrs=$(awk -F '\t' '$4 != "-" { print $3 }' "$history" 2>/dev/null | sort -u | tr '\n' ' ')
if [ "$kitboots" -ge 2 ]; then
	case "$addrs" in
	*' '?*) bad "different addresses across $kitboots boots: $addrs" ;;
	*) ok "one address across $kitboots boots: $addrs" ;;
	esac
else
	info "boots with the EFS address so far: $kitboots (reboot and run again; two are the test)"
fi

# --- soak ---------------------------------------------------------------------------
if [ "$soak" -gt 0 ] && [ -n "$iface" ]; then
	gw=$(ip -4 route show default dev "$iface" 2>/dev/null | awk '{ print $3; exit }')
	if [ -z "$gw" ]; then
		bad "no IPv4 default route on $iface for the soak"
	else
		before=$(aer)
		echo
		info "soak: ${soak} s of pings to $gw (5 per second)"
		ping -q -i 0.2 -w "$soak" "$gw" | tail -n 2
		after=$(aer)
		info "AER before: $before"
		info "AER after:  $after"
		[ "$before" = "$after" ] && ok 'no new AER errors during the soak' || bad 'AER counters moved during the soak'
		now=
		for a in l1_1_aspm l1_2_aspm; do now="$now$a=$(cat "$ep/link/$a" 2>/dev/null) "; done
		info "after the soak: $now"
		[ -n "$(ls "$ep/net" 2>/dev/null)" ] && ok 'interface still present' || bad 'interface gone after the soak'
	fi
fi

echo
[ "$fails" = 0 ] && echo 'verify: all checks passed' || echo "verify: $fails check(s) failed"
