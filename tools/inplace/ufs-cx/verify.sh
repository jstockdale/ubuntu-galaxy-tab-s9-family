#!/bin/sh
# Checks after booting the CX-vote kit.  Nothing is written.
#   sh ~/ufs-cx/verify.sh [FILE]         the G5 kit's checks, plus this kit's
#   sudo sh ~/ufs-cx/verify.sh [FILE]    also the CX checks (debugfs is root's)
# FILE (default ~/usertests/ufs-test.bin, at least 1 GiB) is read with
# O_DIRECT, so that the UFS sits at its 300 MHz OPP while it is sampled.
set -u
export LC_ALL=C
host=/sys/devices/platform/soc@0/1d84000.ufshc
user_home=$(getent passwd "${SUDO_USER:-$(id -un)}" | cut -d: -f6)
file=${1:-$user_home/usertests/ufs-test.bin}
d=/sys/kernel/debug
fail=0
ok() { echo "ok   $*"; }
bad() { echo "FAIL $*"; fail=1; }

echo "kernel: $(uname -v)"
echo "build id: $(od -An -tx1 -v /sys/kernel/notes | tr -d ' \n' | grep -o '474e5500[0-9a-f]\{40\}' | cut -c9-)"
findmnt -no OPTIONS / | tr ',' '\n' | grep -qx rw && ok 'root is rw' || bad 'root is not rw'

k=$(journalctl -k -b --no-pager -q 2>/dev/null)
check_log() { # description pattern
	n=$(printf '%s\n' "$k" | grep -ciE "$2")
	[ "$n" = 0 ] && ok "no $1 this boot" ||
		{ bad "$1 this boot:"; printf '%s\n' "$k" | grep -iE "$2" | tail -10; }
}
check_log 'UFS link, UIC or PHY errors' 'phy initialization timed-out|ufshcd.*(error|fail)|ufshcd.*UIC|ufshcd.*link'
check_log 'module BTF, signature or symbol failures' 'failed to validate module|module verification failed|disagrees about version|Unknown symbol'
check_log 'performance-state or OPP-vote errors' 'failed to (drop|restore) the OPP votes|failed to set performance state|Failed to set required opps|failed to add.*subdomain|subdomain.*fail'

[ "$(id -u)" = 0 ] && [ -d "$d/pm_genpd" ] && root=1 || root=0
sample() { # the CX picture at this moment
	for c in axi unipro_core ice_core; do
		printf 'gcc_ufs_phy_%s_clk=%s ' "$c" "$(cat "$d/clk/gcc_ufs_phy_${c}_clk/clk_rate")"
	done
	echo
	echo "cx perf_state=$(cat "$d/pm_genpd/cx/perf_state")"
	grep -A3 -E '^(ufs_phy_gdsc|usb30_prim_gdsc) ' "$d/pm_genpd/pm_genpd_summary"
}
loaded=
if [ -r "$file" ]; then
	# At G5 speed 1 GiB takes well under a second, so read it three times.
	(for pass in 1 2 3; do
		dd if="$file" of=/dev/null bs=4M count=256 iflag=direct 2>&1 | tail -1
	done) &
	seen=
	for i in 1 2 3 4; do
		sleep 0.3
		seen="$seen $(cat $host/power_info/gear)/$(cat $host/power_info/rate)"
		[ "$i" = 2 ] && [ "$root" = 1 ] && loaded=$(sample)
	done
	wait
	echo "under load:$seen"
	case "$seen" in
	*HS_GEAR5/HS_RATE_A*) ok 'the link ran HS-G5 Rate-A under load' ;;
	*) bad 'the link did not reach HS-G5 under load' ;;
	esac
else
	bad "no readable $file for the load sample"
fi

if [ "$root" = 1 ]; then
	subs=$(cat "$d/pm_genpd/cx/sub_domains")
	for g in ufs_phy_gdsc ufs_mem_phy_gdsc usb30_prim_gdsc pcie_0_gdsc; do
		printf '%s\n' "$subs" | grep -qx "$g" && ok "$g is a CX subdomain" ||
			bad "$g is not a CX subdomain (the DTB lacks the &gcc CX parent)"
	done
	if [ -n "$loaded" ]; then
		echo "$loaded"
		printf '%s\n' "$loaded" | grep -q 'gcc_ufs_phy_ice_core_clk=403000000' &&
			ok 'ICE clock at 403 MHz under load' || bad 'ICE clock not at 403 MHz under load'
		ufs=$(printf '%s\n' "$loaded" | awk '$1 == "ufs_phy_gdsc" {print $3}')
		cx=$(printf '%s\n' "$loaded" | awk -F= '/^cx perf_state=/ {print $2}')
		[ "${ufs:-0}" -ge 256 ] && ok "ufs_phy_gdsc votes ${ufs} under load" ||
			bad "ufs_phy_gdsc votes ${ufs:-nothing} under load (want 256)"
		[ "${cx:-0}" -ge "${ufs:-256}" ] && ok "CX is at ${cx}, at least the UFS vote" ||
			bad "CX is at ${cx:-unknown}, below the UFS vote"
	fi
else
	echo 'note: run with sudo for the CX checks (debugfs)'
fi
for f in gear rate lane mode; do printf '%s=%s ' "$f" "$(cat $host/power_info/$f)"; done; echo
[ "$fail" = 0 ] && echo 'all checks passed' || echo 'some checks FAILED'
exit "$fail"
