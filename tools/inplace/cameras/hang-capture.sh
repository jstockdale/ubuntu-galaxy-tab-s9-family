#!/bin/sh
# Keep evidence if the SM-X710 hangs during a risky test (an untested camera
# size, say).  Run it on the host, before the test, in two terminals:
#
#   sh hang-capture.sh log TARGET [DIR]      the kernel log, streamed into
#                                            DIR/hang-<tablet>-<time>.log
#   sh hang-capture.sh shell TARGET          a root shell that needs no disk,
#                                            for sysrq during the hang
#   sh hang-capture.sh netconsole TARGET [PORT [DIR]] [--allow-wifi]
#                                            the kernel log over UDP, sent by
#                                            printk itself (hang-log kit)
#   sh hang-capture.sh netconsole-off TARGET
#
# TARGET is <user>@<tablet>.  ssh checks the tablet's host key strictly;
# HANG_SSH_OPTS adds options, e.g. -o HostKeyAlias=<USB address> on Wi-Fi.
# Each mode asks for the tablet's sudo password once.
#
# On 2026-11-10 squirtle hung with 30+ tasks in D state: ping and an open SSH
# session still worked, but the disk had stalled, so the journal kept
# nothing, and a command that needs the disk (sudo, ls, a new login) would
# have blocked.  Hence both sessions start before the test, and the shell
# uses only builtins.  "log" raises the console loglevel to 7 while it runs,
# so hung-task stacks and sysrq dumps also reach the ramoops console, which
# the next boot keeps under /sys/fs/pstore, and turns on all-CPU backtraces
# for hung-task reports.  Ctrl-C ends it and restores both.
set -eu

usage() { sed -n 's/^#   //p' "$0" >&2; exit 2; }
[ $# -ge 2 ] || usage
mode=$1
target=$2
shift 2
# shellcheck disable=SC2086 # HANG_SSH_OPTS is a list of options
ssh_t() { ssh -t -o StrictHostKeyChecking=yes -o ServerAliveInterval=5 \
	-o ServerAliveCountMax=6 ${HANG_SSH_OPTS:-} "$target" "$@"; }
# Run a script as root on the tablet, passed as base64 so that no quoting
# survives two shells; extra words reach it as $1, $2...  The remote login
# shell expands them, so they may name its variables ($SSH_CLIENT).
as_root() { # SCRIPT [WORD...]
	b64=$(printf '%s\n' "$1" | base64 -w0)
	shift
	ssh_t "sudo sh -c \"\$(echo $b64 | base64 -d)\" hang-capture $*"
}
stamp=$(date +%Y%m%dT%H%M%S)
name=$(printf '%s' "${target#*@}" | tr -c 'A-Za-z0-9.-' _)

case $mode in
log)
	dir=${1:-.}
	mkdir -p "$dir"
	file=$dir/hang-$name-$stamp.log
	echo "== host: $(date -Is), kernel log of $target" > "$file"
	echo "writing $file; Ctrl-C stops the stream and restores the loglevel"
	remote='set -u
printk=/proc/sys/kernel/printk
bt=/proc/sys/kernel/hung_task_all_cpu_backtrace
read -r old rest < "$printk"
oldbt=
[ -w "$bt" ] && read -r oldbt < "$bt"
tick=
restore() {
	[ -n "$tick" ] && kill "$tick" 2>/dev/null
	echo "$old" > "$printk"
	[ -n "$oldbt" ] && echo "$oldbt" > "$bt"
	echo "== hang-capture: console loglevel back to $old"
}
trap "restore; exit 0" INT TERM HUP
echo 7 > "$printk"
btstate=unavailable
[ -n "$oldbt" ] && echo 1 > "$bt" && btstate=on
read -r up idle < /proc/uptime
echo "== hang-capture on $(uname -n): kernel $(uname -v), boot $(cat /proc/sys/kernel/random/boot_id)"
echo "== uptime $up s at $(date -Is); console loglevel $old -> 7; hung-task all-CPU backtraces $btstate"
echo "hang-capture: log started at uptime $up" > /dev/kmsg
[ -c /dev/pmsg0 ] && echo "hang-capture: log started at uptime $up" > /dev/pmsg0
(while sleep 30; do read -r up idle < /proc/uptime; echo "== tick: uptime $up"; done) &
tick=$!
dmesg -w -x
restore'
	# The terminal gets the stream as the remote tty sends it; the file
	# gets it without the tty's carriage returns.
	as_root "$remote" | tee /dev/tty | tr -d '\r' >> "$file" || :
	echo "== host: $(date -Is), stream ended" | tee -a "$file"
	;;
shell)
	cat <<'EOF'
In a hang, type these in the root shell that opens below.  echo is built
into the shell and /proc is not on the disk, so they work while it stalls:

  echo w > /proc/sysrq-trigger     the blocked (D-state) tasks, with stacks
  echo l > /proc/sysrq-trigger     a backtrace of every CPU (a CPU spinning
                                   with interrupts off cannot answer)
  echo 'what you saw' > /dev/pmsg0 a note the next boot keeps (pstore pmsg)

Then, once the log stream has shown them, or has stopped:

  echo c > /proc/sysrq-trigger     a panic: the kernel writes its log to
                                   pstore and resets the tablet after 10 s

Prefer that to holding the power key: the forced restart on 2026-11-10 let
about 10 % of the ramoops RAM decay.  Nothing else: no Tab completion, no
other command, no exit; any of them may wait on the stalled disk.
EOF
	remote='cd / || exit 1
exec env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin TERM=vt100 HOME=/ PS1="hang# " /bin/sh -i'
	as_root "$remote"
	;;
netconsole)
	port=6666
	dir=.
	wifi=no
	for a in "$@"; do
		case $a in
		--allow-wifi) wifi=allow-wifi ;;
		*[!0-9]*) dir=$a ;;
		*) port=$a ;;
		esac
	done
	mkdir -p "$dir"
	file=$dir/netconsole-$name-$stamp.log
	remote='set -eu
host=$1 port=$2 wifi=$3
route=$(ip -o route get "$host")
dev=$(echo "$route" | sed -n "s/.* dev \([^ ]*\).*/\1/p")
hop=$(echo "$route" | sed -n "s/.* via \([^ ]*\).*/\1/p")
hop=${hop:-$host}
if [ -e "/sys/class/net/$dev/wireless" ] || [ -e "/sys/class/net/$dev/phy80211" ]; then
	if [ "$wifi" != allow-wifi ]; then
		echo "$dev is Wi-Fi: mac80211 transmits under BH locks while netpoll"
		echo "sends with interrupts off; untested here.  Use the USB link, or"
		echo "pass --allow-wifi to try it anyway."
		exit 1
	fi
fi
ping -c 1 -W 1 "$hop" > /dev/null 2>&1 || :
mac=$(ip neigh show "$hop" dev "$dev" | sed -n "s/.* lladdr \([^ ]*\).*/\1/p")
[ -n "$mac" ] || { echo "no link-layer address for $hop on $dev"; exit 1; }
modprobe netconsole
t=/sys/kernel/config/netconsole/hang-capture
if [ -d "$t" ]; then echo 0 > "$t/enabled"; rmdir "$t"; fi
mkdir "$t"
echo "$dev" > "$t/dev_name"
echo "$host" > "$t/remote_ip"
echo "$port" > "$t/remote_port"
echo "$mac" > "$t/remote_mac"
echo 1 > "$t/enabled"
echo "<3>hang-capture: netconsole to $host:$port via $dev ($mac)" > /dev/kmsg
echo "netconsole: $dev -> $host:$port ($mac); console loglevel $(cut -f1 /proc/sys/kernel/printk)"'
	echo "== host: $(date -Is), netconsole of $target on UDP $port" > "$file"
	python3 -I -u -c '
import socket, sys, time
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("", int(sys.argv[1])))
out = open(sys.argv[2], "ab", buffering=0)
while True:
    data, peer = s.recvfrom(65535)
    stamp = time.strftime("%H:%M:%S ").encode()
    for line in data.splitlines():
        out.write(stamp + line + b"\n")
' "$port" "$file" &
listener=$!
	trap 'kill "$listener" 2>/dev/null || :' EXIT INT TERM
	as_root "$remote" '"${SSH_CLIENT%% *}"' "$port" "$wifi" ||
		{ echo 'netconsole setup failed; nothing is listening now' >&2; exit 1; }
	echo "listening on UDP $port into $file; Ctrl-C stops listening (the"
	echo "tablet keeps sending until: sh $0 netconsole-off $target)."
	echo "Nothing arriving?  Check this host's firewall for UDP $port."
	tail -f "$file"
	;;
netconsole-off)
	remote='t=/sys/kernel/config/netconsole/hang-capture
if [ -d "$t" ]; then echo 0 > "$t/enabled"; rmdir "$t"; echo "netconsole target removed"; else echo "no netconsole target"; fi'
	as_root "$remote"
	;;
*) usage ;;
esac
