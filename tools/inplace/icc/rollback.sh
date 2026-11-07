#!/bin/sh
# Undo ~/icc/install.sh: the late release no longer runs from the next boot.
#
#   sudo sh ~/icc/rollback.sh    then reboot to get the INT_MAX floor back
#
# A release that already happened on this boot stays in effect until the
# reboot: the kernel has no way back to the boot floor.
#
# Files the kit installed (no package owns them) are removed, or the versions
# saved before the first install are put back.  If the device package ships the
# unit by now, it is masked instead (the package's postinst would re-enable a
# merely disabled unit); undo that later with
#   sudo systemctl unmask ubuntu-gts9u-icc-late-release.service
#
# From TWRP, when Linux is not reachable, the same effect without this script:
#   [ -d /data/home ] || mount -t ext4 /dev/block/by-name/userdata /data
#   mkdir -p /data/etc/ubuntu-gts9u && touch /data/etc/ubuntu-gts9u/icc-hold
set -eu

log=/var/log/gts9u-icc-kit.log
backup=/var/backups/ubuntu-gts9u-icc
name=ubuntu-gts9u-icc-late-release
helper=/usr/libexec/$name
unit=/usr/lib/systemd/system/$name.service
wants=/etc/systemd/system/multi-user.target.wants/$name.service
pending=/var/lib/ubuntu-gts9u/icc-release.pending

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
owner_of() {
	dpkg-query -S "$1" 2>/dev/null | sed -n 's/^\([^:,]*\):.*/\1/p' | head -n 1
}

[ $# = 0 ] || { echo 'usage: rollback.sh' >&2; exit 2; }
[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> /run/gts9u-icc-kit.lock
flock -n 9 || { echo "install.sh or rollback.sh is still running; follow it with: tail -f $log" >&2; exit 1; }
trap '' HUP PIPE INT TERM
tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9wifi' ||
	die 'this is not an SM-X710'
rec=none
[ -r /run/ubuntu-gts9u/icc-release ] && rec=$(tr '\n' ' ' < /run/ubuntu-gts9u/icc-release)
say "boot $(cat /proc/sys/kernel/random/boot_id); this boot's release record: $rec"

pkg=$(owner_of "$unit")
if [ -n "$pkg" ]; then
	run_logged mask systemctl mask "$name.service" || die "systemctl mask $name.service failed"
	say "$pkg ships the unit, so it is masked (undo: sudo systemctl unmask $name.service)"
else
	run_logged disable systemctl disable "$name.service" || :
	[ -L "$wants" ] && { rm -f "$wants" || die "cannot remove $wants"; }
	if [ -e "$backup/complete" ]; then
		for f in "$helper" "$unit"; do
			if [ -e "$backup/${f##*/}" ]; then
				cp -a "$backup/${f##*/}" "$f" || die "restoring $f failed"
				say "restored the earlier $f"
			else
				rm -f "$f" || die "cannot remove $f"
				say "removed $f"
			fi
		done
		if [ "$(cat "$backup/enablement" 2>/dev/null)" = enabled ] && [ -e "$unit" ]; then
			say 'note: the earlier unit was enabled before the kit; it stays disabled now'
		fi
		mv "$backup" "$backup.rolled-back-$(date +%Y%m%d-%H%M%S)" || die "cannot retire $backup"
	else
		for f in "$helper" "$unit"; do
			if [ -e "$f" ]; then
				rm -f "$f" || die "cannot remove $f"
				say "removed $f"
			fi
		done
	fi
	run_logged daemon-reload systemctl daemon-reload || die 'systemctl daemon-reload failed'
fi
# The loop breaker's marker is cleared by the unit's ExecStop at shutdown; a
# removed or masked unit has none, and a stale marker would make a later
# reinstall keep the floor for one boot for no reason.
if [ -e "$pending" ]; then
	rm -f "$pending" || die "cannot remove $pending"
	say "removed the loop-breaker marker $pending"
fi
say "unit now: $(systemctl is-enabled "$name.service" 2>&1)"
say 'done; the floor comes back with the next reboot (sudo reboot)'
