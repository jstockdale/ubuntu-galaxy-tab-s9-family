#!/bin/sh
# Undo ~/ufs-cx/install.sh: back to the G5 kit (its boot image and module
# tree, fix7-ice's vendor_boot).  Run it before the G5 kit's own rollback.sh.
#   sudo sh ~/ufs-cx/rollback.sh      then reboot
set -eu
export LC_ALL=C

release=7.2.0-rc3-dirty
modules=/usr/lib/modules/$release
backup=/var/backups/ubuntu-gts9u-ufs-cx
log=/var/log/gts9u-ufs-cx.log
size=100663296
g5_boot=4179f4061db3949c136a313c771edb10d6dce90bafefff52e92ccf21ed4d5648
fix7_vendor_boot=4b8b2f92e5d3f6e4631e684e853c16243404263f629a40b6c1f5313c37e6eb36
saved_set=/var/lib/gts9u-boot-sets/ubuntu

say() {
	line="$(date '+%F %T') ufs-cx rollback: $*"
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
		[ "$(part_sha "$1")" = "$3" ] ||
			die "$1 read-back does not match: do NOT reboot; run rollback.sh again"
	fi
	say "$1 holds $4 (read back from the disk)"
}

[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> /run/gts9u-ufs-cx.lock
flock -n 9 || { echo 'install.sh or rollback.sh is still running; see /var/log/gts9u-ufs-cx.log' >&2; exit 1; }
tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9wifi' || die 'this is not an SM-X710'
[ -d "$backup" ] || die "no $backup: install.sh never ran (or was rolled back already)"
[ "$(sha256sum "$backup/boot-g5.img" | cut -d' ' -f1)" = "$g5_boot" ] ||
	die 'the saved G5 boot image is missing or changed; nothing was changed'
[ "$(sha256sum "$backup/vendor_boot-fix7-ice.img" | cut -d' ' -f1)" = "$fix7_vendor_boot" ] ||
	die 'the saved fix7-ice vendor_boot image is missing or changed; nothing was changed'

trap '' HUP PIPE INT TERM
# boot first, vendor_boot last: the reverse of install.sh.
restore_part boot "$backup/boot-g5.img" "$g5_boot" 'the G5 kit'
restore_part vendor_boot "$backup/vendor_boot-fix7-ice.img" "$fix7_vendor_boot" 'fix7-ice'
for name in boot vendor_boot; do
	[ -f "$backup/saved-set-$name.img" ] || continue
	cp "$backup/saved-set-$name.img" "$saved_set/$name.img.new"
	mv "$saved_set/$name.img.new" "$saved_set/$name.img"
	say "saved Ubuntu boot set: $name.img restored"
done
if [ -d "$backup/modules-$release" ]; then
	rm -rf -- "/usr/lib/modules/.ufs-cx-$release" "$modules"
	mv "$backup/modules-$release" "$modules" || die 'restoring the G5 module tree failed'
	depmod -a "$release" || die 'depmod failed'
	say "modules: G5 tree restored ($(find "$modules" -name '*.ko*' | wc -l) modules)"
fi
mv "$backup" "$backup.rolled-back-$(date +%Y%m%d-%H%M%S)" || die "cannot retire $backup"
say 'back on the G5 kit; reboot now (sudo reboot)'
