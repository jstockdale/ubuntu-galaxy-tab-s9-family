#!/bin/sh
# UFS HS-G5 experiment: fix7-ice's images with a kernel that runs the UFS
# link at HS-G5 Rate-A at the 300 MHz OPP, and the module tree built with that
# kernel (its BTF and signing key must match).  vendor_boot stays fix7-ice's.
#
#   sudo sh ~/ufs-g5/install.sh      then reboot
#   sudo sh ~/ufs-g5/rollback.sh     back to fix7-ice, then reboot
#
# Before anything is written it checks the kit's checksums, that boot and
# vendor_boot hold fix7-ice, that root is writable, the battery and dpkg.
# It saves the boot partition (root-only, hash-checked), swaps the module
# tree, writes boot synchronously and reads it back from the disk.
#
# If the tablet does not come back: TWRP, then write the saved image back,
#   dd if=/mnt/ubuntu/var/backups/ubuntu-gts9u-ufs-g5/boot-fix7-ice.img \
#      of=/dev/block/by-name/boot bs=4M
# and run rollback.sh on the next boot to put the module tree back.
set -eu
export LC_ALL=C

kit=$(cd "$(dirname "$0")" && pwd)
release=7.2.0-rc3-dirty
modules=/usr/lib/modules/$release
backup=/var/backups/ubuntu-gts9u-ufs-g5
log=/var/log/gts9u-ufs-g5.log
size=100663296
fix7_boot=cc8784081c83270872ea6948fadf07e8c89de3de02a7235ddc2d8de8e8454aff
fix7_vendor_boot=4b8b2f92e5d3f6e4631e684e853c16243404263f629a40b6c1f5313c37e6eb36
saved_set=/var/lib/gts9u-boot-sets/ubuntu

say() {
	line="$(date '+%F %T') ufs-g5 install: $*"
	echo "$line" >> "$log"
	echo "$line" 2>/dev/null || :
}
die() { say "STOP: $*"; exit 1; }
part_sha() { # partition label
	dd if="/dev/disk/by-partlabel/$1" iflag=direct bs=4M count=$((size / 4194304)) \
		status=none | sha256sum | cut -d' ' -f1
}

[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> /run/gts9u-ufs-g5.lock
flock -n 9 || { echo 'install.sh or rollback.sh is still running; see /var/log/gts9u-ufs-g5.log' >&2; exit 1; }
tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9wifi' || die 'this is not an SM-X710'
[ -e "$backup/complete" ] && die "the G5 kit is already installed ($backup); run rollback.sh first"
(cd "$kit" && sha256sum -c --quiet SHA256SUMS) || die 'the kit does not match its checksums; nothing was changed'
g5_boot=$(awk '$2 == "boot.img" {print $1}' "$kit/SHA256SUMS")
[ "$(stat -c %s "$kit/boot.img")" = "$size" ] || die 'boot.img is not the boot partition size'
[ "$(part_sha boot)" = "$fix7_boot" ] || die 'the boot partition does not hold fix7-ice'
[ "$(part_sha vendor_boot)" = "$fix7_vendor_boot" ] || die 'vendor_boot does not hold fix7-ice'
findmnt -no OPTIONS / | tr ',' '\n' | grep -qx rw || die 'the root filesystem is not writable'
cap=$(cat /sys/class/power_supply/*battery*/capacity 2>/dev/null | head -1)
[ "${cap:-0}" -ge 30 ] || die "battery at ${cap:-unknown} %; charge to 30 % first"
fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock >/dev/null 2>&1 && die 'dpkg is busy; try again when it is done'
[ -z "$(dpkg --audit 2>/dev/null)" ] || die 'dpkg --audit reports unfinished packages'
[ "$(df -Pk /var/backups | awk 'NR == 2 {print $4}')" -gt 409600 ] || die 'less than 400 MB free'

trap '' HUP PIPE INT TERM
finished=0
trap '[ "$finished" = 1 ] || say "STOP: install ended early; do NOT reboot before running rollback.sh"' EXIT
say "boot $(cat /proc/sys/kernel/random/boot_id), kernel $(uname -v)"
mkdir -p "$backup"
chmod 0700 "$backup"

# The running boot image, saved from the disk and checked.
dd if=/dev/disk/by-partlabel/boot of="$backup/boot-fix7-ice.img" iflag=direct \
	bs=4M count=$((size / 4194304)) status=none
[ "$(sha256sum "$backup/boot-fix7-ice.img" | cut -d' ' -f1)" = "$fix7_boot" ] ||
	die 'the saved boot image does not match fix7-ice'
say "boot partition saved to $backup/boot-fix7-ice.img"

# The module tree first; the flash last, so that a failure before it leaves
# fix7-ice bootable, and rollback.sh can undo either step.
stage=/usr/lib/modules/.ufs-g5-$release
rm -rf -- "$stage"
mkdir -p "$stage"
tar -C "$stage" --strip-components=4 -xf "$kit/modules-$release.tar" || die 'extracting the module tree failed'
[ -f "$stage/modules.dep" ] || die 'module tree incomplete after extraction'
mv "$modules" "$backup/modules-$release" || die 'moving the fix7-ice module tree aside failed'
mv "$stage" "$modules" || die 'moving the new module tree into place failed'
depmod -a "$release" || die 'depmod failed'
say "modules: $(find "$modules" -name '*.ko*' | wc -l) modules installed (fix7-ice tree in $backup)"

say 'writing boot (a few seconds; do not interrupt)'
dd if="$kit/boot.img" of=/dev/disk/by-partlabel/boot bs=4M conv=fsync status=none ||
	die 'writing boot failed: do NOT reboot; run rollback.sh'
blockdev --flushbufs /dev/disk/by-partlabel/boot 2>/dev/null || :
[ "$(part_sha boot)" = "$g5_boot" ] || die 'boot read-back does not match: do NOT reboot; run rollback.sh'
say "boot written and read back ($(echo "$g5_boot" | cut -c1-12))"

# Keep the saved Ubuntu boot set (dual boot) in step with the partition.
if [ -f "$saved_set/boot.img" ]; then
	cp "$saved_set/boot.img" "$backup/saved-set-boot.img"
	cp "$kit/boot.img" "$saved_set/boot.img.new"
	mv "$saved_set/boot.img.new" "$saved_set/boot.img"
	[ "$(sha256sum "$saved_set/boot.img" | cut -d' ' -f1)" = "$g5_boot" ] ||
		die 'the saved Ubuntu boot set did not update: do NOT reboot; run rollback.sh'
	say "saved Ubuntu boot set updated (previous one in $backup)"
fi

touch "$backup/complete"
finished=1
say 'G5 kit installed; reboot now (sudo reboot)'
