#!/bin/sh
# Undo ~/cam-bus/install.sh: back to the BT UART kit's boot and the lens
# kit's vendor_boot.  Run it before the BT UART kit's own rollback.sh.
#   sudo sh ~/cam-bus/rollback.sh      then reboot
set -eu
export LC_ALL=C

backup=/var/backups/ubuntu-gts9u-cam-bus
log=/var/log/gts9u-cam-bus.log
size=100663296
bt_boot=faa774ad340f66c07f1f0ad4948b6edc7e141a0cd2235b473234070588d317ba
bt_vendor_boot=b18510f38bae75d285347baef80a2beebfd57e246a2a7e7a4c7ce17983bd166a
saved_set=/var/lib/gts9u-boot-sets/ubuntu

say() {
	line="$(date '+%F %T') cam-bus rollback: $*"
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
	say "$1 holds the previous kit's image (read back from the disk)"
}

[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> /run/gts9u-cam-bus.lock
flock -n 9 || { echo 'install.sh or rollback.sh is still running' >&2; exit 1; }
tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9wifi' || die 'this is not an SM-X710'
[ -d "$backup" ] || die "no $backup: install.sh never ran (or was rolled back already)"
[ "$(sha256sum "$backup/boot-bt.img" | cut -d' ' -f1)" = "$bt_boot" ] ||
	die 'the saved BT UART boot image is missing or changed; nothing was changed'
[ "$(sha256sum "$backup/vendor_boot-bt.img" | cut -d' ' -f1)" = "$bt_vendor_boot" ] ||
	die 'the saved lens vendor_boot image is missing or changed; nothing was changed'

trap '' HUP PIPE INT TERM
restore_part boot "$backup/boot-bt.img" "$bt_boot"
restore_part vendor_boot "$backup/vendor_boot-bt.img" "$bt_vendor_boot"
for name in boot vendor_boot; do
	[ -f "$backup/saved-set-$name.img" ] || continue
	cp "$backup/saved-set-$name.img" "$saved_set/$name.img.new"
	mv "$saved_set/$name.img.new" "$saved_set/$name.img"
	say "saved Ubuntu boot set: $name.img restored"
done
mv "$backup" "$backup.rolled-back-$(date +%Y%m%d-%H%M%S)" || die "cannot retire $backup"
say 'back on the BT UART kit; reboot now (sudo reboot)'
