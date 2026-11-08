#!/bin/sh
# Checks after booting the BT UART kit.  Nothing is written.
#   sh ~/bt-uart/verify.sh          the kernel, the logs and hci0's state
#   sudo sh ~/bt-uart/verify.sh     also an HCI command round trip
# The CX kit's ~/ufs-cx/verify.sh still applies to this kernel.
set -u
export LC_ALL=C
kit=$(cd "$(dirname "$0")" && pwd)
fail=0
ok() { echo "ok   $*"; }
bad() { echo "FAIL $*"; fail=1; }

echo "kernel: $(uname -v)"
id=$(od -An -tx1 -v /sys/kernel/notes | tr -d ' \n' | grep -o '474e5500[0-9a-f]\{40\}' | cut -c9-)
want=$(cat "$kit/BUILD_ID" 2>/dev/null)
[ -n "$want" ] && [ "$id" = "$want" ] && ok "running the kit's kernel (build ID $(echo "$id" | cut -c1-12))" ||
	bad "build ID $id, the kit's is ${want:-unknown}"
findmnt -no OPTIONS / | tr ',' '\n' | grep -qx rw && ok 'root is rw' || bad 'root is not rw'

k=$(journalctl -k -b --no-pager -q 2>/dev/null)
check_log() { # description pattern
	n=$(printf '%s\n' "$k" | grep -ciE "$2")
	[ "$n" = 0 ] && ok "no $1 this boot" ||
		{ bad "$1 this boot:"; printf '%s\n' "$k" | grep -iE "$2" | tail -10; }
}
check_log 'disabled or unhandled interrupts' 'nobody cared|Disabling IRQ #'
check_log 'HCI command timeouts' 'hci[0-9]+: (command 0x[0-9a-f]+ tx timeout|Opcode 0x[0-9a-f]+ failed: -110)'
check_log 'module signature or symbol failures' 'failed to validate module|module verification failed|disagrees about version|Unknown symbol'

hci=$(ls /sys/class/bluetooth 2>/dev/null | grep -m1 -x 'hci[0-9]*')
if [ -n "$hci" ]; then
	uart=$(readlink -f "/sys/class/bluetooth/$hci" | grep -o '[0-9a-f]*\.serial' | head -1)
	info=$(hciconfig "$hci" 2>/dev/null)
	echo "$hci on ${uart:-an unknown UART}: $(printf '%s\n' "$info" | grep -o 'BD Address: [0-9A-F:]*')"
	printf '%s\n' "$info" | grep -q 'UP RUNNING' && ok "$hci is up" || bad "$hci is not up"
	printf '%s\n' "$info" | grep -q 'BD Address: 00:00:00:00:00:00' &&
		bad "$hci has the NVM's null address (ubuntu-gts9u-bluetooth-address did not apply)" ||
		ok "$hci has its EFS address"
else
	bad 'no Bluetooth controller'
fi

if [ "$(id -u)" = 0 ] && [ -n "$hci" ]; then
	# HCI Read Local Version Information: a command complete with status 0.
	out=$(timeout 10 hcitool -i "$hci" cmd 0x04 0x0001 2>&1)
	printf '%s\n' "$out" | tr -s ' \n' '  ' | grep -qiE '0x0e plen [0-9]+ 01 01 10 00' &&
		ok "$hci answered an HCI command" || { bad "$hci did not answer an HCI command:"; printf '%s\n' "$out"; }
else
	echo 'note: run with sudo for the HCI command round trip'
fi
[ "$fail" = 0 ] && echo 'all checks passed' || echo 'some checks FAILED'
exit "$fail"
