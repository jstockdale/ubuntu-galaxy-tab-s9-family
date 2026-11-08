#!/bin/sh
# Undo ~/bt-uart/install.sh: back to the CX kit (its boot image and module
# tree).  Run it before the CX kit's own rollback.sh.
#   sudo sh ~/bt-uart/rollback.sh      then reboot
set -eu
export LC_ALL=C

release=7.2.0-rc3-dirty
modules=/usr/lib/modules/$release
backup=/var/backups/ubuntu-gts9u-bt-uart
log=/var/log/gts9u-bt-uart.log
size=100663296
cx_boot=47f7df27477418857bfd087bb44245510c8be5c563d3dcbe2a344778f023df98
saved_set=/var/lib/gts9u-boot-sets/ubuntu

say() {
	line="$(date '+%F %T') bt-uart rollback: $*"
	echo "$line" >> "$log"
	echo "$line" 2>/dev/null || :
}
die() { say "STOP: $*"; exit 1; }
part_sha() {
	dd if="/dev/disk/by-partlabel/$1" iflag=direct bs=4M count=$((size / 4194304)) \
		status=none | sha256sum | cut -d' ' -f1
}

[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> /run/gts9u-bt-uart.lock
flock -n 9 || { echo 'install.sh or rollback.sh is still running; see /var/log/gts9u-bt-uart.log' >&2; exit 1; }
tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9wifi' || die 'this is not an SM-X710'
[ -d "$backup" ] || die "no $backup: install.sh never ran (or was rolled back already)"
[ "$(sha256sum "$backup/boot-cx.img" | cut -d' ' -f1)" = "$cx_boot" ] ||
	die 'the saved CX boot image is missing or changed; nothing was changed'

trap '' HUP PIPE INT TERM
if [ "$(part_sha boot)" != "$cx_boot" ]; then
	dd if="$backup/boot-cx.img" of=/dev/disk/by-partlabel/boot bs=4M conv=fsync status=none ||
		die 'writing boot failed: do NOT reboot; run rollback.sh again, or flash it from TWRP'
	blockdev --flushbufs /dev/disk/by-partlabel/boot 2>/dev/null || :
	[ "$(part_sha boot)" = "$cx_boot" ] ||
		die 'boot read-back does not match: do NOT reboot; run rollback.sh again'
fi
say 'boot holds the CX kit (read back from the disk)'
if [ -f "$backup/saved-set-boot.img" ]; then
	cp "$backup/saved-set-boot.img" "$saved_set/boot.img.new"
	mv "$saved_set/boot.img.new" "$saved_set/boot.img"
	say 'saved Ubuntu boot set: boot.img restored'
fi
if [ -d "$backup/modules-$release" ]; then
	rm -rf -- "/usr/lib/modules/.bt-uart-$release" "$modules"
	mv "$backup/modules-$release" "$modules" || die 'restoring the CX module tree failed'
	depmod -a "$release" || die 'depmod failed'
	say "modules: CX tree restored ($(find "$modules" -name '*.ko*' | wc -l) modules)"
fi
mv "$backup" "$backup.rolled-back-$(date +%Y%m%d-%H%M%S)" || die "cannot retire $backup"
say 'back on the CX kit; reboot now (sudo reboot)'
