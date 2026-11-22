#!/bin/sh
# Remove the stall watch completely: stop and disable the service, remove its
# program, wrapper, unit and settings, and everything it recorded in
# /var/log/gts9u-stall-watch.  With --keep-data the recordings stay.  Nothing
# else on the tablet was changed by install.sh, so nothing else is restored.
# Safe to run more than once, and after an install that stopped half-way.
#
#   sudo sh ~/stall-watch/rollback.sh [--keep-data]
set -eu
export LC_ALL=C

root=''
lib=$root/usr/local/lib/gts9u-stall-watch
bin=$root/usr/local/bin/stall-watch
unit=$root/etc/systemd/system/gts9u-stall-watch.service
conf=$root/etc/gts9u-stall-watch.conf
data=$root/var/log/gts9u-stall-watch
state=$root/var/lib/gts9u-stall-watch
rundir=$root/run/gts9u-stall-watch
backups=$root/var/backups
log=$root/var/log/gts9u-stall-watch-kit.log
service=gts9u-stall-watch.service

say() {
	line="$(date '+%F %T') stall-watch rollback: $*"
	echo "$line" >> "$log"
	echo "$line" 2>/dev/null || :
}
die() { say "STOP: $*"; exit 1; }

keep=0
case "${1:-}" in
--keep-data) keep=1 ;;
'') ;;
*) echo "usage: sudo sh $0 [--keep-data]" >&2; exit 2 ;;
esac
[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> "$root/run/gts9u-stall-watch-kit.lock"
flock -n 9 || { echo 'install.sh or rollback.sh is already running; see /var/log/gts9u-stall-watch-kit.log' >&2; exit 1; }
trap '' HUP PIPE
finished=0
trap '[ "$finished" = 1 ] || say "NOTE: rollback ended early; run it again"' EXIT

# The service first, while its unit still exists.
systemctl disable --now "$service" >> "$log" 2>&1 || :
for f in "$unit" "$bin"; do
	if [ -e "$f" ] || [ -L "$f" ]; then
		rm -f "$f"
		say "removed $f"
	fi
done
if [ -d "$lib" ]; then
	rm -rf "$lib"
	say "removed $lib"
fi
if [ -f "$conf" ]; then
	shipped=$(cat "$state/conf.sha256" 2>/dev/null || true)
	if [ -n "$shipped" ] && [ "$(sha256sum < "$conf" | cut -d' ' -f1)" != "$shipped" ]; then
		mkdir -p "$backups"
		copy=$backups/gts9u-stall-watch.conf.removed-$(date +%Y%m%d-%H%M%S)
		cp -p "$conf" "$copy"
		say "your edited $conf is saved as $copy"
	fi
	rm -f "$conf"
	say "removed $conf"
fi
rm -f "$conf.dist"
systemctl daemon-reload || say 'systemctl daemon-reload failed (harmless: the unit file is gone)'
systemctl reset-failed "$service" >/dev/null 2>&1 || :
rm -rf "$rundir" "$state"
if [ "$keep" = 1 ]; then
	say "kept the recordings in $data (remove them with: sudo rm -rf $data)"
elif [ -d "$data" ]; then
	rm -rf "$data"
	say "removed the recordings in $data"
fi
finished=1
say 'the stall watch is removed; nothing to reboot'
