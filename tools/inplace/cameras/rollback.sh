#!/bin/sh
# Undo ~/cameras-kit/install.sh: 2.55's camera files, its fixed four-node
# v4l2loopback configuration and its four relays, without a reboot.
#
#   sudo sh ~/cameras-kit/rollback.sh
set -eu

kit=$(cd "$(dirname "$0")" && pwd)
backup=/var/backups/ubuntu-gts9u-cameras
log=/var/log/gts9u-cameras.log
relays=ubuntu-gts9u-camera-relays.service
loader=ubuntu-gts9u-camera-loopback.service
enabler=ubuntu-gts9u-camera-enable.service

say() {
	line="$(date '+%F %T') rollback: $*"
	echo "$line" >> "$log"
	echo "$line" 2>/dev/null || :
}
die() { say "STOP: $*"; exit 1; }
run_logged() {
	tag=$1
	shift
	out=$(mktemp)
	if "$@" > "$out" 2>&1; then rc=0; else rc=$?; fi
	while IFS= read -r l || [ -n "$l" ]; do say "$tag: $l"; done < "$out"
	rm -f "$out"
	return "$rc"
}

[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> /run/gts9u-cameras.lock
flock -n 9 || {
	echo 'install.sh or rollback.sh is still running; follow it with: tail -f /var/log/gts9u-cameras.log' >&2
	exit 1
}
trap '' HUP PIPE
finished=0
trap 'rc=$?; [ "$finished" = 1 ] || say "ended early (status $rc); run rollback.sh again"' EXIT

tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9wifi' ||
	die 'this is not an SM-X710'
[ -e "$backup/complete" ] || die "no complete backup in $backup: install.sh never ran (or was rolled back already)"
[ -f "$kit/FILES" ] && [ -f "$kit/REMOVED" ] || die "the kit's FILES/REMOVED lists are missing"
# After a newer device package the backup is stale: that package owns these
# files now, and putting 2.55's back would half-downgrade it.
was=$(cat "$backup/device-version" 2>/dev/null || echo '?')
now=$(dpkg-query -W -f '${Version}' ubuntu-gts9u-device 2>/dev/null || echo '?')
[ "$was" = "$now" ] ||
	die "ubuntu-gts9u-device is $now now, not $was as at install: reinstall the package version you want instead (nothing was changed)"

# The relays first (they hold the nodes), then the loader's state.
run_logged systemctl systemctl stop "$relays" || die "stopping $relays failed"
run_logged disable systemctl disable "$loader" "$enabler" || :
systemctl stop "$loader" "$enabler" 2>/dev/null || :

# Saved files back; files the kit added (absent before) go.
for path in $(awk '{ print $1 }' "$kit/FILES") $(cat "$kit/REMOVED"); do
	if grep -qx "$path" "$backup/absent"; then
		rm -f "/$path"
	elif [ -e "$backup/root/$path" ]; then
		mkdir -p "/$(dirname "$path")"
		cp -a "$backup/root/$path" "/$path.gts9u-old" &&
			mv -f "/$path.gts9u-old" "/$path" || die "restoring /$path failed"
	else
		die "the backup has no copy of /$path"
	fi
done
rm -f /run/ubuntu-gts9u/camera-profile
say 'files restored'
run_logged daemon-reload systemctl daemon-reload || die 'systemctl daemon-reload failed'
run_logged udev udevadm control --reload || :

# 2.55 loads the module at boot; do it now, with its restored options.
if lsmod | grep -qE '^v4l2loopback '; then
	run_logged modprobe modprobe -r v4l2loopback ||
		die 'v4l2loopback is in use; close what holds /dev/video2x and run rollback.sh again'
fi
run_logged modprobe modprobe v4l2loopback || die 'modprobe v4l2loopback failed'
run_logged systemctl systemctl start "$relays" || say "starting $relays failed (see journalctl -u $relays)"
say "relays: $(systemctl show -p ActiveState --value "$relays"); nodes: $(cat /sys/devices/virtual/video4linux/video*/name 2>/dev/null | tr '\n' ' ')"

# A finished rollback must not count as an install: the next install.sh
# checks the files against 2.55 again and takes fresh backups.
mv "$backup" "$backup.rolled-back-$(date +%Y%m%d-%H%M%S)" || die "cannot retire $backup"
finished=1
say 'done'
