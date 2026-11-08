#!/bin/sh
# CX-vote kit, on top of the UFS HS-G5 kit: the G5 kernel plus the ufshcd and
# dwc3-qcom system-suspend patches, its module tree, and the DTB that gives
# the GCC GDSCs CX as their parent, in boot (appended) and in vendor_boot.
#
#   sudo sh ~/ufs-cx/install.sh      then reboot
#   sudo sh ~/ufs-cx/rollback.sh     back to the G5 kit, then reboot
#
# Before anything is written it checks the kit's checksums, that the G5 kit
# is installed (boot holds its image, vendor_boot holds fix7-ice's), that
# root is writable, the battery and dpkg.  It saves both partitions
# (root-only, hash-checked) and the G5 module tree, swaps the module tree,
# then writes vendor_boot and boot synchronously, each read back from the
# disk.
#
# If the tablet does not come back: TWRP, then write the saved images back,
#   dd if=/mnt/ubuntu/var/backups/ubuntu-gts9u-ufs-cx/vendor_boot-fix7-ice.img \
#      of=/dev/block/by-name/vendor_boot bs=4M
#   dd if=/mnt/ubuntu/var/backups/ubuntu-gts9u-ufs-cx/boot-g5.img \
#      of=/dev/block/by-name/boot bs=4M
# and run rollback.sh on the next boot to put the module tree back.
set -eu
export LC_ALL=C

kit=$(cd "$(dirname "$0")" && pwd)
release=7.2.0-rc3-dirty
modules=/usr/lib/modules/$release
backup=/var/backups/ubuntu-gts9u-ufs-cx
g5_backup=/var/backups/ubuntu-gts9u-ufs-g5
log=/var/log/gts9u-ufs-cx.log
size=100663296
g5_boot=4179f4061db3949c136a313c771edb10d6dce90bafefff52e92ccf21ed4d5648
fix7_vendor_boot=4b8b2f92e5d3f6e4631e684e853c16243404263f629a40b6c1f5313c37e6eb36
saved_set=/var/lib/gts9u-boot-sets/ubuntu

say() {
	line="$(date '+%F %T') ufs-cx install: $*"
	echo "$line" >> "$log"
	echo "$line" 2>/dev/null || :
}
die() { say "STOP: $*"; exit 1; }
part_sha() { # partition label
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

[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> /run/gts9u-ufs-cx.lock
flock -n 9 || { echo 'install.sh or rollback.sh is still running; see /var/log/gts9u-ufs-cx.log' >&2; exit 1; }
tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9wifi' || die 'this is not an SM-X710'
[ -e "$backup/complete" ] && die "the CX kit is already installed ($backup); run rollback.sh first"
[ -e "$g5_backup/complete" ] || die "the G5 kit is not installed ($g5_backup/complete missing); install it first"
[ "$(uname -r)" = "$release" ] || die "running kernel release $(uname -r), expected $release"
(cd "$kit" && sha256sum -c --quiet SHA256SUMS) || die 'the kit does not match its checksums; nothing was changed'
cx_boot=$(awk '$2 == "boot.img" {print $1}' "$kit/SHA256SUMS")
cx_vendor_boot=$(awk '$2 == "vendor_boot.img" {print $1}' "$kit/SHA256SUMS")
for f in boot.img vendor_boot.img; do
	[ "$(stat -c %s "$kit/$f")" = "$size" ] || die "$f is not the partition size"
done
[ -f "$kit/modules-$release.tar" ] || die "no modules-$release.tar in the kit"
[ "$(part_sha boot)" = "$g5_boot" ] || die 'the boot partition does not hold the G5 kit'
[ "$(part_sha vendor_boot)" = "$fix7_vendor_boot" ] || die 'vendor_boot does not hold fix7-ice'
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

# The running images, saved from the disk and checked.
dd if=/dev/disk/by-partlabel/boot of="$backup/boot-g5.img" iflag=direct \
	bs=4M count=$((size / 4194304)) status=none
[ "$(sha256sum "$backup/boot-g5.img" | cut -d' ' -f1)" = "$g5_boot" ] ||
	die 'the saved boot image does not match the G5 kit'
dd if=/dev/disk/by-partlabel/vendor_boot of="$backup/vendor_boot-fix7-ice.img" iflag=direct \
	bs=4M count=$((size / 4194304)) status=none
[ "$(sha256sum "$backup/vendor_boot-fix7-ice.img" | cut -d' ' -f1)" = "$fix7_vendor_boot" ] ||
	die 'the saved vendor_boot image does not match fix7-ice'
say "boot and vendor_boot saved to $backup"

# The module tree first; the flashes last, so that a failure before them
# leaves the G5 kit bootable, and rollback.sh can undo any step.
stage=/usr/lib/modules/.ufs-cx-$release
rm -rf -- "$stage"
mkdir -p "$stage"
tar -C "$stage" --strip-components=4 -xf "$kit/modules-$release.tar" || die 'extracting the module tree failed'
[ -f "$stage/modules.dep" ] || die 'module tree incomplete after extraction'
mv "$modules" "$backup/modules-$release" || die 'moving the G5 module tree aside failed'
mv "$stage" "$modules" || die 'moving the new module tree into place failed'
depmod -a "$release" || die 'depmod failed'
say "modules: $(find "$modules" -name '*.ko*' | wc -l) modules installed (G5 tree in $backup)"

# vendor_boot first, boot last.  Either half alone still boots: ABL takes the
# DTB from vendor_boot, and both kernels run on both DTBs.
say 'writing vendor_boot and boot (a few seconds each; do not interrupt)'
write_part vendor_boot "$kit/vendor_boot.img" "$cx_vendor_boot"
write_part boot "$kit/boot.img" "$cx_boot"

# Keep the saved Ubuntu boot set (dual boot) in step with the partitions.
for name in boot vendor_boot; do
	[ -f "$saved_set/$name.img" ] || continue
	cp "$saved_set/$name.img" "$backup/saved-set-$name.img"
	cp "$kit/$name.img" "$saved_set/$name.img.new"
	mv "$saved_set/$name.img.new" "$saved_set/$name.img"
	want=$(awk -v f="$name.img" '$2 == f {print $1}' "$kit/SHA256SUMS")
	[ "$(sha256sum "$saved_set/$name.img" | cut -d' ' -f1)" = "$want" ] ||
		die "the saved Ubuntu boot set's $name.img did not update: do NOT reboot; run rollback.sh"
	say "saved Ubuntu boot set: $name.img updated (previous one in $backup)"
done

touch "$backup/complete"
finished=1
say 'CX kit installed; reboot now (sudo reboot)'
