#!/bin/sh
# Undo ~/hang-log/install.sh: back to the camera-bus kit's boot and
# vendor_boot, the BT UART kit's module tree, and no sysctl drop-in.  Run it
# before the camera-bus kit's own rollback.sh.
#   sudo sh ~/hang-log/rollback.sh      then reboot
set -eu
export LC_ALL=C

release=7.2.0-rc3-dirty
modules=/usr/lib/modules/$release
backup=/var/backups/ubuntu-gts9u-hang-log
log=/var/log/gts9u-hang-log.log
size=100663296
cambus_boot=5529f6e70dbc80cdc1b173770a4138fbdc8fd8829708fc32a9cc703e6c7c9996
cambus_vendor_boot=51777e20a965ce7d3a63d03adf8d2fdfdafa844a0dbdff9149032bcaf37a4d94
sysctl_conf=/etc/sysctl.d/60-gts9u-hang-log.conf
saved_set=/var/lib/gts9u-boot-sets/ubuntu

say() {
	line="$(date '+%F %T') hang-log rollback: $*"
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
	say "$1 holds the camera-bus kit's image (read back from the disk)"
}

[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> /run/gts9u-hang-log.lock
flock -n 9 || { echo 'install.sh or rollback.sh is still running; see /var/log/gts9u-hang-log.log' >&2; exit 1; }
tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9wifi' || die 'this is not an SM-X710'
[ -d "$backup" ] || die "no $backup: install.sh never ran (or was rolled back already)"
[ "$(sha256sum "$backup/boot-cam-bus.img" | cut -d' ' -f1)" = "$cambus_boot" ] ||
	die 'the saved camera-bus boot image is missing or changed; nothing was changed'
[ "$(sha256sum "$backup/vendor_boot-cam-bus.img" | cut -d' ' -f1)" = "$cambus_vendor_boot" ] ||
	die 'the saved camera-bus vendor_boot image is missing or changed; nothing was changed'

trap '' HUP PIPE INT TERM
restore_part boot "$backup/boot-cam-bus.img" "$cambus_boot"
restore_part vendor_boot "$backup/vendor_boot-cam-bus.img" "$cambus_vendor_boot"
for name in boot vendor_boot; do
	[ -f "$backup/saved-set-$name.img" ] || continue
	cp "$backup/saved-set-$name.img" "$saved_set/$name.img.new"
	mv "$saved_set/$name.img.new" "$saved_set/$name.img"
	say "saved Ubuntu boot set: $name.img restored"
done
if [ -d "$backup/modules-$release" ]; then
	rm -rf -- "/usr/lib/modules/.hang-log-$release" "$modules"
	mv "$backup/modules-$release" "$modules" || die 'restoring the BT UART module tree failed'
	depmod -a "$release" || die 'depmod failed'
	say "modules: BT UART tree restored ($(find "$modules" -name '*.ko*' | wc -l) modules)"
fi
rm -f -- "$sysctl_conf"
say "$sysctl_conf removed"
mv "$backup" "$backup.rolled-back-$(date +%Y%m%d-%H%M%S)" || die "cannot retire $backup"
say 'back on the camera-bus kit; reboot now (sudo reboot)'
