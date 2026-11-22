#!/bin/sh
# Undo ~/typec-roles/install.sh: back to the hang-log kit's boot and module
# tree (vendor_boot was never changed).  Also after any number of upgrades
# from one revision of the kit to the next, the last one finished or not:
# what the first install saved is the way back, and an upgrade leaves it as
# it was.  Run it before the hang-log kit's own rollback.sh.
#   sudo sh ~/typec-roles/rollback.sh      then reboot
set -eu
export LC_ALL=C

release=7.2.0-rc3-dirty
modules=/usr/lib/modules/$release
backup=/var/backups/ubuntu-gts9u-typec-roles
log=/var/log/gts9u-typec-roles.log
size=100663296
hang_boot=d583546b5e5c33dbc48fbafc99b7eb039df33f64b0316e498d3f158f834ee968
saved_set=/var/lib/gts9u-boot-sets/ubuntu

say() {
	line="$(date '+%F %T') typec-roles rollback: $*"
	echo "$line" >> "$log"
	echo "$line" 2>/dev/null || :
}
die() { say "STOP: $*"; exit 1; }
part_sha() {
	dd if="/dev/disk/by-partlabel/$1" iflag=direct bs=4M count=$((size / 4194304)) \
		status=none | sha256sum | cut -d' ' -f1
}
restore_part() { # label image sha
	if [ "$(part_sha "$1")" != "$3" ]; then
		dd if="$2" of="/dev/disk/by-partlabel/$1" bs=4M conv=fsync status=none ||
			die "writing $1 failed: do NOT reboot; run rollback.sh again, or flash it from TWRP"
		blockdev --flushbufs "/dev/disk/by-partlabel/$1" 2>/dev/null || :
		[ "$(part_sha "$1")" = "$3" ] || die "$1 read-back does not match: do NOT reboot; run rollback.sh again"
	fi
	say "$1 holds the hang-log kit's image (read back from the disk)"
}

[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> /run/gts9u-typec-roles.lock
flock -n 9 || { echo 'install.sh or rollback.sh is still running; see /var/log/gts9u-typec-roles.log' >&2; exit 1; }
tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9wifi' || die 'this is not an SM-X710'
[ -d "$backup" ] || die "no $backup: install.sh never ran (or was rolled back already)"
if [ "$(sha256sum "$backup/boot-hang-log.img" 2>/dev/null | cut -d' ' -f1)" != "$hang_boot" ]; then
	# install.sh saves boot before it changes anything else.
	[ ! -e "$backup/modules-$release" ] && [ "$(part_sha boot)" = "$hang_boot" ] ||
		die 'the saved hang-log boot image is missing or changed; nothing was changed'
	mv "$backup" "$backup.unfinished-$(date +%Y%m%d-%H%M%S)" || die "cannot retire $backup"
	say 'install.sh had stopped before it changed anything; the hang-log kit is still in place'
	exit 0
fi

trap '' HUP PIPE INT TERM
finished=0
early='do NOT reboot before running rollback.sh again'
trap '[ "$finished" = 1 ] || say "STOP: rollback ended early; $early"' EXIT
# The kernel and its module tree first: the hang-log kernel refuses the kit's
# modules.  The rest can be finished by a second run.
restore_part boot "$backup/boot-hang-log.img" "$hang_boot"
# What an install or upgrade that stopped can leave next to the tree: the new
# tree unpacked, also by an install that stopped before it moved the hang-log
# tree aside, and the earlier tree an upgrade set aside.  Neither is the way
# back.
rm -rf -- "/usr/lib/modules/.typec-roles-$release" "/usr/lib/modules/.typec-roles-old-$release"
if [ -d "$backup/modules-$release" ]; then
	rm -rf -- "$modules"
	mv "$backup/modules-$release" "$modules" || die 'restoring the hang-log module tree failed'
	# The tree comes back with its own modules.dep; this refreshes it.
	depmod -a "$release" || die 'depmod failed'
	say "modules: hang-log tree restored ($(find "$modules" -name '*.ko*' | wc -l) modules)"
fi
sync || die 'sync failed'
early='the hang-log kernel and its modules are back in place; run rollback.sh again to finish'
if [ -f "$backup/saved-set-boot.img" ]; then
	from=$backup/saved-set-boot.img
	# An empty file is what a power failure right after install.sh leaves;
	# the set then gets the image of the hang-log kit that is back in place.
	[ -s "$from" ] || from=$backup/boot-hang-log.img
	if [ -d "$saved_set" ]; then
		cp "$from" "$saved_set/boot.img.new" && mv "$saved_set/boot.img.new" "$saved_set/boot.img" ||
			die "restoring $saved_set/boot.img failed"
		say "saved Ubuntu boot set: boot.img restored (from ${from##*/})"
	else
		say "saved Ubuntu boot set: $saved_set is gone; its boot.img stays in the retired backup"
	fi
fi
mv "$backup" "$backup.rolled-back-$(date +%Y%m%d-%H%M%S)" || die "cannot retire $backup"
finished=1
sync || die 'the hang-log kit is back in place, but sync failed: run "sudo sync" until it succeeds, then reboot'
say 'back on the hang-log kit; reboot now (sudo reboot)'
