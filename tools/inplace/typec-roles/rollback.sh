#!/bin/sh
# Undo ~/typec-roles/install.sh: back to the hang-log kit's boot, vendor_boot
# and module tree.  Revisions up to 13 never wrote vendor_boot; from
# revision 14 on an install saves the hang-log kit's vendor_boot before it
# writes that partition, and this puts it back.  Also after any number of
# upgrades from one revision of the kit to the next, the last one finished
# or not: what the first install saved is the way back, and an upgrade
# leaves it as it was.  Run it before the hang-log kit's own rollback.sh.
#   sudo sh ~/typec-roles/rollback.sh      then reboot
set -eu
export LC_ALL=C

release=7.2.0-rc3-dirty
modules=/usr/lib/modules/$release
backup=/var/backups/ubuntu-gts9u-typec-roles
saved_vb=$backup/vendor_boot-hang-log.img
log=/var/log/gts9u-typec-roles.log
size=100663296
hang_boot=d583546b5e5c33dbc48fbafc99b7eb039df33f64b0316e498d3f158f834ee968
hang_vendor_boot=9165f42917a8ee4069b878fc0c0660391b39087fc6f3ea2b607f626821a7dbd4
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
# Put back the saved Ubuntu boot set's NAME.img (dual boot) from the copy an
# install made of it, or, for an empty copy (what a power failure right
# after install.sh leaves), from FALLBACK, the hang-log kit's image.
restore_set() { # NAME FALLBACK
	from=$backup/saved-set-$1.img
	[ -f "$from" ] || return 0
	[ -s "$from" ] || from=$2
	if [ -d "$saved_set" ]; then
		cp "$from" "$saved_set/$1.img.new" && mv "$saved_set/$1.img.new" "$saved_set/$1.img" ||
			die "restoring $saved_set/$1.img failed"
		say "saved Ubuntu boot set: $1.img restored (from ${from##*/})"
	else
		say "saved Ubuntu boot set: $saved_set is gone; its $1.img stays in the retired backup"
	fi
}

[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> /run/gts9u-typec-roles.lock
flock -n 9 || { echo 'install.sh or rollback.sh is still running; see /var/log/gts9u-typec-roles.log' >&2; exit 1; }
tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9wifi' || die 'this is not an SM-X710'
[ -d "$backup" ] || die "no $backup: install.sh never ran (or was rolled back already)"
on_vb=$(part_sha vendor_boot)
if [ "$(sha256sum "$backup/boot-hang-log.img" 2>/dev/null | cut -d' ' -f1)" != "$hang_boot" ]; then
	# install.sh saves boot (and vendor_boot) before it changes anything else.
	[ ! -e "$backup/modules-$release" ] && [ "$(part_sha boot)" = "$hang_boot" ] &&
		[ "$on_vb" = "$hang_vendor_boot" ] ||
		die 'the saved hang-log boot image is missing or changed; nothing was changed'
	mv "$backup" "$backup.unfinished-$(date +%Y%m%d-%H%M%S)" || die "cannot retire $backup"
	say 'install.sh had stopped before it changed anything; the hang-log kit is still in place'
	exit 0
fi
# vendor_boot: an install writes it only once its copy of the hang-log kit's
# image is whole and on the disk.  Holding anything else without that copy,
# it was not this kit that wrote it.
if [ "$on_vb" != "$hang_vendor_boot" ]; then
	[ -e "$saved_vb" ] ||
		die "vendor_boot holds an image this kit did not write ($on_vb), and no copy of the hang-log kit's is in $backup; nothing was changed: write the hang-log kit's vendor_boot.img to vendor_boot first"
	[ "$(sha256sum "$saved_vb" | cut -d' ' -f1)" = "$hang_vendor_boot" ] ||
		die 'the saved hang-log vendor_boot image is changed; nothing was changed'
fi

trap '' HUP PIPE INT TERM
finished=0
early='do NOT reboot before running rollback.sh again'
trap '[ "$finished" = 1 ] || say "STOP: rollback ended early; $early"' EXIT
# The kernel, its DTB and its module tree first: the hang-log kernel refuses
# the kit's modules.  The rest can be finished by a second run.
restore_part boot "$backup/boot-hang-log.img" "$hang_boot"
restore_part vendor_boot "$saved_vb" "$hang_vendor_boot"
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
restore_set boot "$backup/boot-hang-log.img"
restore_set vendor_boot "$saved_vb"
mv "$backup" "$backup.rolled-back-$(date +%Y%m%d-%H%M%S)" || die "cannot retire $backup"
finished=1
sync || die 'the hang-log kit is back in place, but sync failed: run "sudo sync" until it succeeds, then reboot'
say 'back on the hang-log kit; reboot now (sudo reboot)'
