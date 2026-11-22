#!/bin/sh
# Hang-log kit, on top of the camera-bus kit (itself on top of the BT UART
# kit): the BT UART kernel rebuilt with the hung-task, lockup and workqueue
# detectors, netconsole as a module and pstore ftrace, and the camera-bus DTB
# with a reorganised ramoops region (pmsg and ftrace zones, ECC 96), in boot
# (appended) and vendor_boot; its module tree; and a sysctl drop-in.
#
#   sudo sh ~/hang-log/install.sh      then reboot
#   sudo sh ~/hang-log/rollback.sh     back to the camera-bus kit, then reboot
#
# Before anything is written it checks the kit's checksums, that the
# camera-bus kit is installed (boot and vendor_boot hold its images), the
# running kernel, that root is writable, the battery, dpkg and free space.
# It saves boot, vendor_boot (root-only, hash-checked) and the module tree,
# swaps the module tree, then writes vendor_boot and boot synchronously, each
# read back from the disk.
#
# The modules are signed with this build's own key.  Until the reboot, the
# running kernel cannot load any module from the new tree.
#
# If the tablet does not come back: TWRP, then write the saved images back,
#   dd if=/mnt/ubuntu/var/backups/ubuntu-gts9u-hang-log/vendor_boot-cam-bus.img \
#      of=/dev/block/by-name/vendor_boot bs=4M
#   dd if=/mnt/ubuntu/var/backups/ubuntu-gts9u-hang-log/boot-cam-bus.img \
#      of=/dev/block/by-name/boot bs=4M
# and run rollback.sh on the next boot: until then the old kernel refuses
# this kit's module tree (v4l2loopback, netfilter); Wi-Fi, USB networking and
# Bluetooth are built in.
set -eu
export LC_ALL=C

kit=$(cd "$(dirname "$0")" && pwd)
release=7.2.0-rc3-dirty
modules=/usr/lib/modules/$release
backup=/var/backups/ubuntu-gts9u-hang-log
bt_backup=/var/backups/ubuntu-gts9u-bt-uart
cambus_backup=/var/backups/ubuntu-gts9u-cam-bus
log=/var/log/gts9u-hang-log.log
size=100663296
cambus_boot=5529f6e70dbc80cdc1b173770a4138fbdc8fd8829708fc32a9cc703e6c7c9996
cambus_vendor_boot=51777e20a965ce7d3a63d03adf8d2fdfdafa844a0dbdff9149032bcaf37a4d94
# The BT UART kit's kernel, the one in the camera-bus kit's boot.
bt_build_id=e862f606ab391efa4a42f394fb4aff845c804069
sysctl_conf=/etc/sysctl.d/60-gts9u-hang-log.conf
saved_set=/var/lib/gts9u-boot-sets/ubuntu

say() {
	line="$(date '+%F %T') hang-log install: $*"
	echo "$line" >> "$log"
	echo "$line" 2>/dev/null || :
}
die() { say "STOP: $*"; exit 1; }
part_sha() {
	dd if="/dev/disk/by-partlabel/$1" iflag=direct bs=4M count=$((size / 4194304)) \
		status=none | sha256sum | cut -d' ' -f1
}
write_part() { # label image sha
	dd if="$2" of="/dev/disk/by-partlabel/$1" bs=4M conv=fsync status=none ||
		die "writing $1 failed: do NOT reboot; run rollback.sh"
	blockdev --flushbufs "/dev/disk/by-partlabel/$1" 2>/dev/null || :
	[ "$(part_sha "$1")" = "$3" ] || die "$1 read-back does not match: do NOT reboot; run rollback.sh"
	say "$1 written and read back ($(echo "$3" | cut -c1-12))"
}
build_id() { od -An -tx1 -v /sys/kernel/notes | tr -d ' \n' | grep -o '474e5500[0-9a-f]\{40\}' | cut -c9-; }

[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> /run/gts9u-hang-log.lock
flock -n 9 || { echo 'install.sh or rollback.sh is still running; see /var/log/gts9u-hang-log.log' >&2; exit 1; }
tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9wifi' || die 'this is not an SM-X710'
[ -e "$backup/complete" ] && die "the hang-log kit is already installed ($backup); run rollback.sh first"
[ -e "$bt_backup/complete" ] || die "the BT UART kit is not installed ($bt_backup/complete missing)"
[ -e "$cambus_backup/complete" ] || die "the camera-bus kit is not installed ($cambus_backup/complete missing)"
[ "$(uname -r)" = "$release" ] || die "running kernel release $(uname -r), expected $release"
[ "$(build_id)" = "$bt_build_id" ] || die "running kernel build ID $(build_id), expected the BT UART kit's ($bt_build_id)"
(cd "$kit" && sha256sum -c --quiet SHA256SUMS) || die 'the kit does not match its checksums; nothing was changed'
new_boot=$(awk '$2 == "boot.img" {print $1}' "$kit/SHA256SUMS")
new_vendor_boot=$(awk '$2 == "vendor_boot.img" {print $1}' "$kit/SHA256SUMS")
for f in boot.img vendor_boot.img; do
	[ "$(stat -c %s "$kit/$f")" = "$size" ] || die "$f is not the partition size"
done
[ -f "$kit/modules-$release.tar" ] || die "no modules-$release.tar in the kit"
[ "$(part_sha boot)" = "$cambus_boot" ] || die 'the boot partition does not hold the camera-bus kit'
[ "$(part_sha vendor_boot)" = "$cambus_vendor_boot" ] || die 'vendor_boot does not hold the camera-bus kit'
[ ! -e "$sysctl_conf" ] || die "$sysctl_conf exists already"
findmnt -no OPTIONS / | tr ',' '\n' | grep -qx rw || die 'the root filesystem is not writable'
cap=$(cat /sys/class/power_supply/*battery*/capacity 2>/dev/null | head -1)
[ "${cap:-0}" -ge 30 ] || die "battery at ${cap:-unknown} %; charge to 30 % first"
fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock >/dev/null 2>&1 && die 'dpkg is busy; try again when it is done'
[ -z "$(dpkg --audit 2>/dev/null)" ] || die 'dpkg --audit reports unfinished packages'
[ "$(df -Pk /var/backups | awk 'NR == 2 {print $4}')" -gt 614400 ] || die 'less than 600 MB free'

trap '' HUP PIPE INT TERM
finished=0
trap '[ "$finished" = 1 ] || say "STOP: install ended early; do NOT reboot before running rollback.sh"' EXIT
say "boot $(cat /proc/sys/kernel/random/boot_id), kernel $(uname -v)"
mkdir -p "$backup"
chmod 0700 "$backup"
dd if=/dev/disk/by-partlabel/boot of="$backup/boot-cam-bus.img" iflag=direct \
	bs=4M count=$((size / 4194304)) status=none
[ "$(sha256sum "$backup/boot-cam-bus.img" | cut -d' ' -f1)" = "$cambus_boot" ] ||
	die 'the saved boot image does not match the camera-bus kit'
dd if=/dev/disk/by-partlabel/vendor_boot of="$backup/vendor_boot-cam-bus.img" iflag=direct \
	bs=4M count=$((size / 4194304)) status=none
[ "$(sha256sum "$backup/vendor_boot-cam-bus.img" | cut -d' ' -f1)" = "$cambus_vendor_boot" ] ||
	die 'the saved vendor_boot image does not match the camera-bus kit'
say "boot and vendor_boot saved to $backup"

# The module tree first, the flash last, so that a failure before it leaves
# the camera-bus kit bootable, and rollback.sh can undo any step.
stage=/usr/lib/modules/.hang-log-$release
rm -rf -- "$stage"
mkdir -p "$stage"
tar -C "$stage" --strip-components=4 -xf "$kit/modules-$release.tar" || die 'extracting the module tree failed'
[ -f "$stage/modules.dep" ] || die 'module tree incomplete after extraction'
mv "$modules" "$backup/modules-$release" || die 'moving the BT UART module tree aside failed'
mv "$stage" "$modules" || die 'moving the new module tree into place failed'
depmod -a "$release" || die 'depmod failed'
say "modules: $(find "$modules" -name '*.ko*' | wc -l) modules installed (BT UART tree in $backup)"

install -m 0644 "$kit/60-gts9u-hang-log.conf" "$sysctl_conf"
say "$sysctl_conf installed (applies at the next boot)"

# Either half alone still boots: ABL takes the DTB from vendor_boot, and
# each kernel runs on both DTBs.
say 'writing vendor_boot and boot (a few seconds each; do not interrupt)'
write_part vendor_boot "$kit/vendor_boot.img" "$new_vendor_boot"
write_part boot "$kit/boot.img" "$new_boot"

# Keep the saved Ubuntu boot set (dual boot) in step with the partitions.
for name in boot vendor_boot; do
	[ -f "$saved_set/$name.img" ] || continue
	cp "$saved_set/$name.img" "$backup/saved-set-$name.img"
	cp "$kit/$name.img" "$saved_set/$name.img.new"
	mv "$saved_set/$name.img.new" "$saved_set/$name.img"
	say "saved Ubuntu boot set: $name.img updated (previous one in $backup)"
done

touch "$backup/complete"
finished=1
say 'hang-log kit installed; reboot now (sudo reboot)'
