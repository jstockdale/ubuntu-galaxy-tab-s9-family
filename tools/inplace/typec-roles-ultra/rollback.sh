#!/bin/sh
# Undo ~/typec-roles/install.sh on the SM-X910: back to release v1.2.5's
# boot and module tree (vendor_boot was never changed), and the
# typec-sink-host stopgap enabled again if install.sh disabled it.  Also
# after an upgrade from one revision of the kit to the next, finished or not.
#   sudo sh ~/typec-roles/rollback.sh      then reboot
set -eu
export LC_ALL=C

release=7.2.0-rc3-dirty
modules=/usr/lib/modules/$release
backup=/var/backups/ubuntu-gts9u-typec-roles
log=/var/log/gts9u-typec-roles.log
size=100663296
base_boot=d09cb64ff411ca39feab17c1c4330081619df9304b6a5b974bf23ccab915bdac
saved_set=/var/lib/gts9u-boot-sets/ubuntu
stopgap=typec-sink-host.service

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
	say "$1 holds release v1.2.5's image (read back from the disk)"
}

[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> /run/gts9u-typec-roles.lock
flock -n 9 || { echo 'install.sh or rollback.sh is still running; see /var/log/gts9u-typec-roles.log' >&2; exit 1; }
tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9uwifi' || die 'this is not an SM-X910'
[ -d "$backup" ] || die "no $backup: install.sh never ran (or was rolled back already)"
if [ "$(sha256sum "$backup/boot-release.img" 2>/dev/null | cut -d' ' -f1)" != "$base_boot" ]; then
	# install.sh saves boot before it changes anything else.
	[ ! -e "$backup/modules-$release" ] && [ "$(part_sha boot)" = "$base_boot" ] ||
		die 'the saved release boot image is missing or changed; nothing was changed'
	mv "$backup" "$backup.unfinished-$(date +%Y%m%d-%H%M%S)" || die "cannot retire $backup"
	say 'install.sh had stopped before it changed anything; the release is still in place'
	exit 0
fi

trap '' HUP PIPE INT TERM
finished=0
early='do NOT reboot before running rollback.sh again'
trap '[ "$finished" = 1 ] || say "STOP: rollback ended early; $early"' EXIT
# The kernel and its module tree first: the release kernel refuses the kit's
# modules, and with them Wi-Fi.  The rest can be finished by a second run.
restore_part boot "$backup/boot-release.img" "$base_boot"
if [ -d "$backup/modules-$release" ]; then
	rm -rf -- "/usr/lib/modules/.typec-roles-$release" "/usr/lib/modules/.typec-roles-old-$release" "$modules"
	mv "$backup/modules-$release" "$modules" || die 'restoring the release module tree failed'
	# The tree comes back with its own modules.dep; this refreshes it.
	depmod -a "$release" || die 'depmod failed'
	say "modules: release tree restored ($(find "$modules" -name '*.ko*' | wc -l) modules)"
fi
sync || die 'sync failed'
early='the release kernel and its modules are back in place; run rollback.sh again to finish'
if [ -f "$backup/saved-set-boot.img" ]; then
	from=$backup/saved-set-boot.img
	# An empty file is what a power failure right after install.sh leaves;
	# the set then gets the image of the release that is back in place.
	[ -s "$from" ] || from=$backup/boot-release.img
	if [ -d "$saved_set" ]; then
		cp "$from" "$saved_set/boot.img.new" && mv "$saved_set/boot.img.new" "$saved_set/boot.img" ||
			die "restoring $saved_set/boot.img failed"
		say "saved Ubuntu boot set: boot.img restored (from ${from##*/})"
	else
		say "saved Ubuntu boot set: $saved_set is gone; its boot.img stays in the retired backup"
	fi
fi
if [ -e "$backup/stopgap-was-enabled" ]; then
	systemctl enable "$stopgap" >> "$log" 2>&1 && say "$stopgap enabled again (it starts at the next boot)" ||
		say "enabling $stopgap failed; do it by hand: sudo systemctl enable $stopgap"
fi
mv "$backup" "$backup.rolled-back-$(date +%Y%m%d-%H%M%S)" || die "cannot retire $backup"
finished=1
sync || die 'the release is back in place, but sync failed: run "sudo sync" until it succeeds, then reboot'
say 'back on release v1.2.5; reboot now (sudo reboot)'
