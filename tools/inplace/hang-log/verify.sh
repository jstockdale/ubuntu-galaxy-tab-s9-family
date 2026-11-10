#!/bin/sh
# Checks after booting the hang-log kit.  Nothing is written, except by
# hung-test, which touches only a loop file system it makes in /run.
#   sh ~/hang-log/verify.sh              kernel, detectors, ramoops, netconsole
#   sudo sh ~/hang-log/verify.sh         also lists /sys/fs/pstore
#   sudo sh ~/hang-log/verify.sh hung-test
#                                        a task blocked on a frozen file
#                                        system must be reported (2 min)
set -u
export LC_ALL=C
kit=$(cd "$(dirname "$0")" && pwd)
fail=0
ok() { echo "ok   $*"; }
bad() { echo "FAIL $*"; fail=1; }
note() { echo "note $*"; }
dt=/proc/device-tree
ramoops=$dt/reserved-memory/sec-pmsg@880900000
u32() { od -An -tu4 --endian=big "$1" 2>/dev/null | tr -d ' \n'; }

hung_test() {
	[ "$(id -u)" = 0 ] || { echo 'hung-test needs sudo' >&2; exit 1; }
	timeout=$(cat /proc/sys/kernel/hung_task_timeout_secs 2>/dev/null) ||
		{ echo 'this kernel has no hung-task detector' >&2; exit 1; }
	[ "$timeout" -gt 0 ] && [ "$timeout" -le 120 ] ||
		{ echo "hung_task_timeout_secs is $timeout" >&2; exit 1; }
	warnings=$(cat /proc/sys/kernel/hung_task_warnings)
	[ "$warnings" != 0 ] || { echo 'no hung-task report is left this boot (hung_task_warnings 0)' >&2; exit 1; }
	dir=$(mktemp -d /run/gts9u-hang-test.XXXXXX)
	mnt=$dir/mnt
	pid=
	cleanup() {
		fsfreeze -u "$mnt" 2>/dev/null
		[ -n "$pid" ] && wait "$pid" 2>/dev/null
		umount "$mnt" 2>/dev/null
		rm -rf -- "$dir"
		# The test used one of this boot's reports; give it back.
		echo "$warnings" > /proc/sys/kernel/hung_task_warnings
	}
	trap cleanup EXIT
	trap 'exit 1' INT TERM HUP
	truncate -s 16M "$dir/fs.img"
	mkfs.ext4 -q -F "$dir/fs.img"
	mkdir "$mnt"
	mount -o loop "$dir/fs.img" "$mnt"
	fsfreeze -f "$mnt"
	sh -c "echo blocked > '$mnt/file'" &
	pid=$!
	echo "<4>gts9u hang-log: hung-test, pid $pid blocks on a frozen file system" > /dev/kmsg
	echo "pid $pid writes to a frozen ext4 (a loop file in /run); expect a report within $((2 * timeout)) s"
	waited=0
	while [ "$waited" -le $((2 * timeout + 15)) ]; do
		sleep 5
		waited=$((waited + 5))
		dmesg | grep -q "task sh:$pid blocked" && break
	done
	if dmesg | grep -q "task sh:$pid blocked"; then
		ok "the hung-task detector reported pid $pid after about $waited s:"
		dmesg | grep -A12 "task sh:$pid blocked" | head -16
	else
		bad "no hung-task report for pid $pid after $waited s (state $(awk '{print $3}' "/proc/$pid/stat" 2>/dev/null))"
	fi
	exit "$fail"
}
[ "${1:-}" = hung-test ] && hung_test

echo "kernel: $(uname -v)"
id=$(od -An -tx1 -v /sys/kernel/notes | tr -d ' \n' | grep -o '474e5500[0-9a-f]\{40\}' | cut -c9-)
want=$(cat "$kit/BUILD_ID" 2>/dev/null)
[ -n "$want" ] && [ "$id" = "$want" ] && ok "running the kit's kernel (build ID $(echo "$id" | cut -c1-12))" ||
	bad "build ID $id, the kit's is ${want:-unknown}"
findmnt -no OPTIONS / | tr ',' '\n' | grep -qx rw && ok 'root is rw' || bad 'root is not rw'

k=$(journalctl -k -b --no-pager -q 2>/dev/null)
key=$(cat "$kit/KEY_ID" 2>/dev/null)
printf '%s\n' "$k" | grep -q "Loaded X.509 cert .*: ${key:-none}" &&
	ok "the kernel trusts the kit's module key (${key%"${key#????????????}"})" ||
	bad "no 'Loaded X.509 cert' line for the kit's key ${key:-unknown}"

cfg=$(zcat /proc/config.gz 2>/dev/null)
for c in CONFIG_DETECT_HUNG_TASK=y CONFIG_DEFAULT_HUNG_TASK_TIMEOUT=60 \
	CONFIG_DETECT_HUNG_TASK_BLOCKER=y CONFIG_SOFTLOCKUP_DETECTOR=y \
	CONFIG_HARDLOCKUP_DETECTOR=y CONFIG_HARDLOCKUP_DETECTOR_BUDDY=y \
	CONFIG_WQ_WATCHDOG=y CONFIG_NETCONSOLE=m CONFIG_NETCONSOLE_DYNAMIC=y \
	CONFIG_PSTORE_RAM=y CONFIG_PSTORE_CONSOLE=y CONFIG_PSTORE_PMSG=y \
	CONFIG_PSTORE_FTRACE=y '# CONFIG_PSTORE_COMPRESS is not set'; do
	printf '%s\n' "$cfg" | grep -qx "$c" || bad "config: no $c"
done
ok 'config: the detectors, netconsole and pstore options are as built'

sysctl_is() { # name value
	v=$(cat "/proc/sys/kernel/$1" 2>/dev/null)
	[ "$v" = "$2" ] && ok "kernel.$1 = $v" || bad "kernel.$1 is ${v:-missing}, expected $2"
}
sysctl_is hung_task_timeout_secs 60
sysctl_is hung_task_panic 0
sysctl_is watchdog 1
sysctl_is soft_watchdog 1
sysctl_is nmi_watchdog 1
sysctl_is softlockup_panic 0
read -r level rest < /proc/sys/kernel/printk
[ "$level" -ge 5 ] && ok "console loglevel $level (warnings reach ramoops)" ||
	bad "console loglevel $level: is /etc/sysctl.d/60-gts9u-hang-log.conf installed?"

# The ramoops region as the kit's DTB describes it, and as ramoops took it.
[ "$(u32 "$ramoops/ecc-size")" = 96 ] && [ "$(u32 "$ramoops/record-size")" = 262144 ] &&
	[ "$(u32 "$ramoops/pmsg-size")" = 131072 ] && [ "$(u32 "$ramoops/ftrace-size")" = 131072 ] &&
	[ "$(u32 "$ramoops/console-size")" = 1048576 ] &&
	ok 'DT: ramoops records 256 KiB, console 1 MiB, pmsg and ftrace 128 KiB, ECC 96' ||
	bad 'DT: the ramoops node is not the kit'"'"'s'
printf '%s\n' "$k" | grep -q 'ramoops: using 0x200000@0x880900000, ecc: 96' &&
	ok 'ramoops registered with ECC 96' || bad 'no "ramoops: using 0x200000@0x880900000, ecc: 96" this boot'
[ -c /dev/pmsg0 ] && ok '/dev/pmsg0 exists' || bad 'no /dev/pmsg0'
printf '%s\n' "$k" | grep -E 'ramoops|pstore|persistent_ram' | sed 's/^/     /'

# The camera-bus kit's DTB changes are still there.
[ "$(u32 "$dt/rear-camera-vio-regulator/startup-delay-us")" = 10000 ] &&
	[ "$(u32 "$dt/soc@0/cci@ac15000/i2c-bus@1/clock-frequency")" = 400000 ] &&
	[ "$(u32 "$dt/soc@0/cci@ac16000/i2c-bus@0/clock-frequency")" = 400000 ] &&
	ok 'DT: rear_cam_vio 10 ms, both rear camera buses at 400 kHz' ||
	bad 'DT: the camera-bus kit'"'"'s lens delay or bus speeds are missing'

path=$(modinfo -n netconsole 2>/dev/null)
signer=$(modinfo -F sig_key netconsole 2>/dev/null)
[ -n "$path" ] && [ "$signer" = "$(cat "$kit/SIGNER" 2>/dev/null)" ] &&
	ok "netconsole module present and signed with the kit's key (${path##*/})" ||
	bad "netconsole module missing or not signed with the kit's key (${path:-none})"

check_log() { # description pattern
	n=$(printf '%s\n' "$k" | grep -ciE "$2")
	[ "$n" = 0 ] && ok "no $1 this boot" ||
		{ bad "$1 this boot:"; printf '%s\n' "$k" | grep -iE "$2" | tail -10; }
}
check_log 'module signature or symbol failures' \
	'failed to validate module|module verification failed|unavailable key|disagrees about version|Unknown symbol'
n=$(printf '%s\n' "$k" | grep -ciE 'blocked (in I/O wait )?for more than|soft lockup|hard LOCKUP|workqueue lockup')
[ "$n" = 0 ] && ok 'no hung-task, lockup or workqueue report this boot' ||
	{ note "$n detector lines this boot (a finding, not a kit failure):"
	  printf '%s\n' "$k" | grep -iE 'blocked (in I/O wait )?for more than|soft lockup|hard LOCKUP|workqueue lockup' | tail -10; }

if [ "$(id -u)" = 0 ]; then
	echo '/sys/fs/pstore (systemd-pstore moves these to /var/lib/systemd/pstore at boot):'
	ls -la /sys/fs/pstore
else
	echo 'note: run with sudo to list /sys/fs/pstore'
fi
[ "$fail" = 0 ] && echo 'all checks passed' || echo 'some checks FAILED'
exit "$fail"
