#!/bin/sh
# Non-root checks after booting the G5 kit (or fix7-ice, for comparison).
#   sh ~/ufs-g5/verify.sh [FILE]
# FILE (default ~/usertests/ufs-test.bin, at least 1 GiB, owned by the user)
# is read with O_DIRECT while the UFS gear is sampled.  Nothing is written.
set -u
export LC_ALL=C
host=/sys/devices/platform/soc@0/1d84000.ufshc
file=${1:-$HOME/usertests/ufs-test.bin}
fail=0
ok() { echo "ok   $*"; }
bad() { echo "FAIL $*"; fail=1; }

echo "kernel: $(uname -v)"
echo "build id: $(od -An -tx1 -v /sys/kernel/notes | tr -d ' \n' | grep -o '474e5500[0-9a-f]\{40\}' | cut -c9-)"
findmnt -no OPTIONS / | tr ',' '\n' | grep -qx rw && ok 'root is rw' || bad 'root is not rw'

k=$(journalctl -k -b --no-pager -q 2>/dev/null)
n=$(printf '%s\n' "$k" | grep -ciE 'phy initialization timed-out|ufshcd.*(error|fail)|ufshcd.*UIC|ufshcd.*link')
[ "$n" = 0 ] && ok 'no UFS link, UIC or PHY errors this boot' ||
	{ bad "UFS kernel messages this boot:"; printf '%s\n' "$k" | grep -iE 'phy initialization timed-out|ufshcd.*(error|fail)|ufshcd.*UIC|ufshcd.*link' | tail -10; }
n=$(printf '%s\n' "$k" | grep -cE 'failed to validate module|module verification failed|disagrees about version|Unknown symbol')
[ "$n" = 0 ] && ok 'no module BTF, signature or symbol failures' ||
	{ bad 'module load failures:'; printf '%s\n' "$k" | grep -E 'failed to validate module|module verification failed|disagrees about version|Unknown symbol' | tail -10; }

if [ -r "$file" ]; then
	(dd if="$file" of=/dev/null bs=4M count=256 iflag=direct 2>&1 | tail -1) &
	seen=
	for i in 1 2 3 4; do
		sleep 0.3
		seen="$seen $(cat $host/power_info/gear)/$(cat $host/power_info/rate)"
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
for f in gear rate lane mode; do printf '%s=%s ' "$f" "$(cat $host/power_info/$f)"; done; echo
cat $host/devfreq/*/trans_stat 2>/dev/null | tail -4
[ "$fail" = 0 ] && echo 'all checks passed' || echo 'some checks FAILED'
exit "$fail"
