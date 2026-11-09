#!/bin/sh
# BT UART kit, on top of the lens power-up kit (itself on top of the CX-vote
# kit): the CX kernel plus the geni UART interrupt patch, with the lens kit's
# DTB appended, in boot, and its module tree.  vendor_boot keeps the lens
# kit's image, which carries the same DTB.
#
#   sudo sh ~/bt-uart/install.sh      then reboot
#   sudo sh ~/bt-uart/rollback.sh     back to the lens kit, then reboot
#
# Before anything is written it checks the kit's checksums, that the lens kit
# is installed over the CX kit (boot and vendor_boot hold the lens kit's
# images), that root is writable, the battery and dpkg.  It saves boot
# (root-only, hash-checked) and the CX module tree, swaps the module tree,
# then writes boot synchronously and reads it back from the disk.
#
# If the tablet does not come back: TWRP, then write the saved image back,
#   dd if=/mnt/ubuntu/var/backups/ubuntu-gts9u-bt-uart/boot-lens.img \
#      of=/dev/block/by-name/boot bs=4M
# and run rollback.sh on the next boot to put the module tree back.
set -eu
export LC_ALL=C

kit=$(cd "$(dirname "$0")" && pwd)
release=7.2.0-rc3-dirty
modules=/usr/lib/modules/$release
backup=/var/backups/ubuntu-gts9u-bt-uart
cx_backup=/var/backups/ubuntu-gts9u-ufs-cx
lens_backup=/var/backups/ubuntu-gts9u-cam-lens
log=/var/log/gts9u-bt-uart.log
size=100663296
lens_boot=57406c8001a9f32c33b0e25171a8996daed4dd4f84d41c6a9338d71b69c76882
lens_vendor_boot=b18510f38bae75d285347baef80a2beebfd57e246a2a7e7a4c7ce17983bd166a
saved_set=/var/lib/gts9u-boot-sets/ubuntu

say() {
	line="$(date '+%F %T') bt-uart install: $*"
	echo "$line" >> "$log"
	echo "$line" 2>/dev/null || :
}
die() { say "STOP: $*"; exit 1; }
part_sha() { # partition label
	dd if="/dev/disk/by-partlabel/$1" iflag=direct bs=4M count=$((size / 4194304)) \
		status=none | sha256sum | cut -d' ' -f1
}

[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> /run/gts9u-bt-uart.lock
flock -n 9 || { echo 'install.sh or rollback.sh is still running; see /var/log/gts9u-bt-uart.log' >&2; exit 1; }
tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9wifi' || die 'this is not an SM-X710'
[ -e "$backup/complete" ] && die "the BT UART kit is already installed ($backup); run rollback.sh first"
[ -e "$cx_backup/complete" ] || die "the CX kit is not installed ($cx_backup/complete missing); install it first"
[ -e "$lens_backup/complete" ] || die "the lens kit is not installed ($lens_backup/complete missing); install it first"
[ "$(uname -r)" = "$release" ] || die "running kernel release $(uname -r), expected $release"
(cd "$kit" && sha256sum -c --quiet SHA256SUMS) || die 'the kit does not match its checksums; nothing was changed'
new_boot=$(awk '$2 == "boot.img" {print $1}' "$kit/SHA256SUMS")
[ "$(stat -c %s "$kit/boot.img")" = "$size" ] || die 'boot.img is not the partition size'
[ -f "$kit/modules-$release.tar" ] || die "no modules-$release.tar in the kit"
[ "$(part_sha boot)" = "$lens_boot" ] || die 'the boot partition does not hold the lens kit'
[ "$(part_sha vendor_boot)" = "$lens_vendor_boot" ] || die 'vendor_boot does not hold the lens kit'
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

# The running image, saved from the disk and checked.
dd if=/dev/disk/by-partlabel/boot of="$backup/boot-lens.img" iflag=direct \
	bs=4M count=$((size / 4194304)) status=none
[ "$(sha256sum "$backup/boot-lens.img" | cut -d' ' -f1)" = "$lens_boot" ] ||
	die 'the saved boot image does not match the lens kit'
say "boot saved to $backup"

# The module tree first, the flash last, so that a failure before it leaves
# the lens kit bootable, and rollback.sh can undo any step.  The lens kit
# kept the CX module tree; both trees are signed with the same key, so
# either kernel loads either tree.
stage=/usr/lib/modules/.bt-uart-$release
rm -rf -- "$stage"
mkdir -p "$stage"
tar -C "$stage" --strip-components=4 -xf "$kit/modules-$release.tar" || die 'extracting the module tree failed'
[ -f "$stage/modules.dep" ] || die 'module tree incomplete after extraction'
mv "$modules" "$backup/modules-$release" || die 'moving the CX module tree aside failed'
mv "$stage" "$modules" || die 'moving the new module tree into place failed'
depmod -a "$release" || die 'depmod failed'
say "modules: $(find "$modules" -name '*.ko*' | wc -l) modules installed (CX tree in $backup)"

say 'writing boot (a few seconds; do not interrupt)'
dd if="$kit/boot.img" of=/dev/disk/by-partlabel/boot bs=4M conv=fsync status=none ||
	die 'writing boot failed: do NOT reboot; run rollback.sh'
blockdev --flushbufs /dev/disk/by-partlabel/boot 2>/dev/null || :
[ "$(part_sha boot)" = "$new_boot" ] || die 'boot read-back does not match: do NOT reboot; run rollback.sh'
say "boot written and read back ($(echo "$new_boot" | cut -c1-12))"

# Keep the saved Ubuntu boot set (dual boot) in step with the partition.
if [ -f "$saved_set/boot.img" ]; then
	cp "$saved_set/boot.img" "$backup/saved-set-boot.img"
	cp "$kit/boot.img" "$saved_set/boot.img.new"
	mv "$saved_set/boot.img.new" "$saved_set/boot.img"
	[ "$(sha256sum "$saved_set/boot.img" | cut -d' ' -f1)" = "$new_boot" ] ||
		die "the saved Ubuntu boot set's boot.img did not update: do NOT reboot; run rollback.sh"
	say "saved Ubuntu boot set: boot.img updated (previous one in $backup)"
fi

touch "$backup/complete"
finished=1
say 'BT UART kit installed; reboot now (sudo reboot)'
