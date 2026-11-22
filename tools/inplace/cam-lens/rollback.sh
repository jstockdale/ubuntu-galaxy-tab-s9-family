#!/bin/sh
# Undo ~/cam-lens/install.sh: back to the CX kit's boot and vendor_boot.
# Run it before the CX kit's own rollback.sh.
#   sudo sh ~/cam-lens/rollback.sh      then reboot
set -eu
export LC_ALL=C

backup=/var/backups/ubuntu-gts9u-cam-lens
log=/var/log/gts9u-cam-lens.log
size=100663296
cx_boot=47f7df27477418857bfd087bb44245510c8be5c563d3dcbe2a344778f023df98
cx_vendor_boot=be06a00f699c104cf5974d2bf85cc6e974d0e3b38b72b42329aa7340fd5272cf
saved_set=/var/lib/gts9u-boot-sets/ubuntu

say() {
	line="$(date '+%F %T') cam-lens rollback: $*"
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
	say "$1 holds the CX kit's image (read back from the disk)"
}

[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> /run/gts9u-cam-lens.lock
flock -n 9 || { echo 'install.sh or rollback.sh is still running' >&2; exit 1; }
tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9wifi' || die 'this is not an SM-X710'
[ -d "$backup" ] || die "no $backup: install.sh never ran (or was rolled back already)"
[ "$(sha256sum "$backup/boot-cx.img" | cut -d' ' -f1)" = "$cx_boot" ] ||
	die 'the saved CX boot image is missing or changed; nothing was changed'
[ "$(sha256sum "$backup/vendor_boot-cx.img" | cut -d' ' -f1)" = "$cx_vendor_boot" ] ||
	die 'the saved CX vendor_boot image is missing or changed; nothing was changed'

trap '' HUP PIPE INT TERM
restore_part boot "$backup/boot-cx.img" "$cx_boot"
restore_part vendor_boot "$backup/vendor_boot-cx.img" "$cx_vendor_boot"
for name in boot vendor_boot; do
	[ -f "$backup/saved-set-$name.img" ] || continue
	cp "$backup/saved-set-$name.img" "$saved_set/$name.img.new"
	mv "$saved_set/$name.img.new" "$saved_set/$name.img"
	say "saved Ubuntu boot set: $name.img restored"
done
mv "$backup" "$backup.rolled-back-$(date +%Y%m%d-%H%M%S)" || die "cannot retire $backup"
say 'back on the CX kit; reboot now (sudo reboot)'
