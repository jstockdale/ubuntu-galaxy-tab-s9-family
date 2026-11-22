#!/bin/sh
# Stall watch for the SM-X910 and the SM-X710: a 1 s system sampler that
# records what the tablet was doing when the desktop froze, so that the next
# freeze explains itself.  It adds one service and its files and changes
# nothing else: no kernel, boot image, package, sysctl, kernel parameter or
# other unit.  No reboot.
#
#   sudo sh ~/stall-watch/install.sh     install, or upgrade any earlier revision in place
#   sh ~/stall-watch/verify.sh           is it running and sampling (no sudo)
#   stall-watch status | list | report   read it (group adm is enough, no sudo)
#   sudo sh ~/stall-watch/rollback.sh    remove it and its recordings (--keep-data keeps them)
#
# Over an earlier revision it replaces the program, unit and wrapper and
# restarts the service; what was recorded stays.  A configuration file you
# edited is kept, and the new defaults go next to it as .dist.
#
# Every line goes to /var/log/gts9u-stall-watch-kit.log; hangups are ignored,
# so a dropped SSH session does not cut the run short.
set -eu
export LC_ALL=C

kit=$(cd "$(dirname "$0")" && pwd)
root=''
rev=1
lib=$root/usr/local/lib/gts9u-stall-watch
bin=$root/usr/local/bin/stall-watch
unit=$root/etc/systemd/system/gts9u-stall-watch.service
conf=$root/etc/gts9u-stall-watch.conf
data=$root/var/log/gts9u-stall-watch
state=$root/var/lib/gts9u-stall-watch
rundir=$root/run/gts9u-stall-watch
log=$root/var/log/gts9u-stall-watch-kit.log
service=gts9u-stall-watch.service

say() {
	line="$(date '+%F %T') stall-watch install: $*"
	echo "$line" >> "$log"
	echo "$line" 2>/dev/null || :
}
die() { say "STOP: $*"; exit 1; }

[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> "$root/run/gts9u-stall-watch-kit.lock"
flock -n 9 || { echo 'install.sh or rollback.sh is already running; see /var/log/gts9u-stall-watch-kit.log' >&2; exit 1; }
trap '' HUP PIPE
finished=0
trap '[ "$finished" = 1 ] || say "NOTE: install.sh ended early; nothing else on the tablet was touched. Fix the cause and run it again, or run rollback.sh."' EXIT

# --- 1. checks: nothing is changed before all of them pass ------------------
compat=$(tr '\0' ' ' < "$root/proc/device-tree/compatible" 2>/dev/null || true)
case " $compat " in
*" samsung,gts9uwifi "*) board=SM-X910 ;;
*" samsung,gts9wifi "*) board=SM-X710 ;;
*)
	[ "${STALL_WATCH_ANY_BOARD:-0}" = 1 ] ||
		die "this is neither an SM-X910 nor an SM-X710 ($compat); set STALL_WATCH_ANY_BOARD=1 to install anyway"
	board=other ;;
esac
(cd "$kit" && sha256sum -c --quiet SHA256SUMS) || die 'the kit does not match its checksums; nothing was changed'
command -v python3 >/dev/null 2>&1 || die 'python3 is not installed'
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' ||
	die "python3 is older than 3.8 ($(python3 -V 2>&1))"
[ -d "$root/run/systemd/system" ] || die 'systemd is not running'
avail=$(df -Pk "$root/var/log" | awk 'NR == 2 {print $4}')
[ "${avail:-0}" -gt 307200 ] || die 'less than 300 MB free in /var/log'
# Read every source once, as the service will, without writing anything.
python3 -I -S "$kit/stall_watch.py" --config /dev/null --root "${root:-/}" check > "$root/run/gts9u-stall-watch-kit.check" 2>&1 ||
	die "the sampler could not read this system: $(tail -n 3 "$root/run/gts9u-stall-watch-kit.check" | tr '\n' ' ')"
rm -f "$root/run/gts9u-stall-watch-kit.check"
if [ -f "$state/installed" ]; then
	was=$(sed -n 's/^rev=//p' "$state/installed")
	say "upgrade on $board: revision ${was:-?} -> $rev; recordings in $data stay"
else
	say "install revision $rev on $board, boot $(cat /proc/sys/kernel/random/boot_id 2>/dev/null)"
fi
say 'checks passed'

# --- 2. files, root-owned -----------------------------------------------------
put() { # source destination mode
	install -D -o 0 -g 0 -m "$3" "$1" "$2.stall-watch-new"
	mv -f "$2.stall-watch-new" "$2"
	say "installed $2 ($3)"
}
mkdir -p "$state"
chmod 0755 "$state"
put "$kit/stall_watch.py" "$lib/stall_watch.py" 0644
put "$kit/README.md" "$lib/README.md" 0644
put "$kit/stall-watch" "$bin" 0755
put "$kit/gts9u-stall-watch.service" "$unit" 0644
shipped=$(sha256sum < "$kit/gts9u-stall-watch.conf" | cut -d' ' -f1)
if [ -f "$conf" ]; then
	now=$(sha256sum < "$conf" | cut -d' ' -f1)
	before=$(cat "$state/conf.sha256" 2>/dev/null || true)
	if [ "$now" = "$shipped" ]; then
		say "$conf is current"
	elif [ "$now" = "$before" ]; then
		put "$kit/gts9u-stall-watch.conf" "$conf" 0644
	else
		put "$kit/gts9u-stall-watch.conf" "$conf.dist" 0644
		say "kept your edited $conf; the new defaults are in $conf.dist"
	fi
else
	put "$kit/gts9u-stall-watch.conf" "$conf" 0644
fi
echo "$shipped" > "$state/conf.sha256"
if getent group adm > /dev/null 2>&1; then
	install -d -o 0 -g adm -m 2750 "$data"
else
	install -d -o 0 -g 0 -m 0750 "$data"
fi
say "recordings go to $data ($(stat -c '%U:%G %a' "$data"))"
{
	echo "rev=$rev"
	echo "installed=$(date '+%F %T')"
	echo "board=$board"
	echo "files=$lib/stall_watch.py $lib/README.md $bin $unit $conf"
} > "$state/installed.new"
mv -f "$state/installed.new" "$state/installed"

# --- 3. the service -------------------------------------------------------------
systemctl daemon-reload || die 'systemctl daemon-reload failed'
systemctl enable "$service" >> "$log" 2>&1 || die "enabling $service failed"
systemctl restart "$service" || die "starting $service failed: journalctl -u $service -n 30"
i=0
samples=0
while [ $i -lt 20 ]; do
	samples=$(python3 -I -S -c 'import json, sys
try:
    print(json.load(open(sys.argv[1]))["samples"])
except Exception:
    print(0)' "$rundir/status.json" 2>/dev/null || echo 0)
	[ "${samples:-0}" -ge 2 ] && break
	sleep 1
	i=$((i + 1))
done
[ "${samples:-0}" -ge 2 ] || die "the service is not sampling after 20 s: journalctl -u $service -n 30"
say "$service is running and has taken $samples samples"
finished=1
say "done; check it with: sh $kit/verify.sh   (after a freeze: stall-watch list, stall-watch report)"
