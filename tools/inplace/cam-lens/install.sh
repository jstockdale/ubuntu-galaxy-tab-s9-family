#!/bin/sh
# Lens-bus kit, on top of the CX-vote kit: the same kernel and modules, and
# the DTB with CCI1 master 0 (rear DW9808 lens, EEPROM) at stock's 400 kHz,
# in boot (appended) and in vendor_boot.
#
#   sudo sh ~/cam-lens/install.sh      then reboot
#   sudo sh ~/cam-lens/rollback.sh     back to the CX kit, then reboot
#
# Before anything is written it checks the kit's checksums, that the CX kit
# is installed (boot and vendor_boot hold its images), that root is writable
# and the battery.  It saves both partitions (root-only, hash-checked), then
# writes vendor_boot and boot synchronously, each read back from the disk.
#
# If the tablet does not come back: TWRP, then write the saved images back,
#   dd if=/mnt/ubuntu/var/backups/ubuntu-gts9u-cam-lens/vendor_boot-cx.img \
#      of=/dev/block/by-name/vendor_boot bs=4M
#   dd if=/mnt/ubuntu/var/backups/ubuntu-gts9u-cam-lens/boot-cx.img \
#      of=/dev/block/by-name/boot bs=4M
set -eu
export LC_ALL=C

kit=$(cd "$(dirname "$0")" && pwd)
backup=/var/backups/ubuntu-gts9u-cam-lens
cx_backup=/var/backups/ubuntu-gts9u-ufs-cx
log=/var/log/gts9u-cam-lens.log
size=100663296
cx_boot=47f7df27477418857bfd087bb44245510c8be5c563d3dcbe2a344778f023df98
cx_vendor_boot=be06a00f699c104cf5974d2bf85cc6e974d0e3b38b72b42329aa7340fd5272cf
saved_set=/var/lib/gts9u-boot-sets/ubuntu

say() {
	line="$(date '+%F %T') cam-lens install: $*"
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

[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> /run/gts9u-cam-lens.lock
flock -n 9 || { echo 'install.sh or rollback.sh is still running; see /var/log/gts9u-cam-lens.log' >&2; exit 1; }
tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9wifi' || die 'this is not an SM-X710'
[ -e "$backup/complete" ] && die "the lens kit is already installed ($backup); run rollback.sh first"
[ -e "$cx_backup/complete" ] || die "the CX kit is not installed ($cx_backup/complete missing)"
(cd "$kit" && sha256sum -c --quiet SHA256SUMS) || die 'the kit does not match its checksums; nothing was changed'
new_boot=$(awk '$2 == "boot.img" {print $1}' "$kit/SHA256SUMS")
new_vendor_boot=$(awk '$2 == "vendor_boot.img" {print $1}' "$kit/SHA256SUMS")
for f in boot.img vendor_boot.img; do
	[ "$(stat -c %s "$kit/$f")" = "$size" ] || die "$f is not the partition size"
done
[ "$(part_sha boot)" = "$cx_boot" ] || die 'the boot partition does not hold the CX kit'
[ "$(part_sha vendor_boot)" = "$cx_vendor_boot" ] || die 'vendor_boot does not hold the CX kit'
findmnt -no OPTIONS / | tr ',' '\n' | grep -qx rw || die 'the root filesystem is not writable'
cap=$(cat /sys/class/power_supply/*battery*/capacity 2>/dev/null | head -1)
[ "${cap:-0}" -ge 30 ] || die "battery at ${cap:-unknown} %; charge to 30 % first"
[ "$(df -Pk /var/backups | awk 'NR == 2 {print $4}')" -gt 307200 ] || die 'less than 300 MB free'

trap '' HUP PIPE INT TERM
finished=0
trap '[ "$finished" = 1 ] || say "STOP: install ended early; do NOT reboot before running rollback.sh"' EXIT
say "boot $(cat /proc/sys/kernel/random/boot_id), kernel $(uname -v)"
mkdir -p "$backup"
chmod 0700 "$backup"
dd if=/dev/disk/by-partlabel/boot of="$backup/boot-cx.img" iflag=direct \
	bs=4M count=$((size / 4194304)) status=none
[ "$(sha256sum "$backup/boot-cx.img" | cut -d' ' -f1)" = "$cx_boot" ] ||
	die 'the saved boot image does not match the CX kit'
dd if=/dev/disk/by-partlabel/vendor_boot of="$backup/vendor_boot-cx.img" iflag=direct \
	bs=4M count=$((size / 4194304)) status=none
[ "$(sha256sum "$backup/vendor_boot-cx.img" | cut -d' ' -f1)" = "$cx_vendor_boot" ] ||
	die 'the saved vendor_boot image does not match the CX kit'
say "boot and vendor_boot saved to $backup"

# Either half alone still boots: ABL takes the DTB from vendor_boot, and the
# kernel runs on both DTBs.
say 'writing vendor_boot and boot (a few seconds each; do not interrupt)'
write_part vendor_boot "$kit/vendor_boot.img" "$new_vendor_boot"
write_part boot "$kit/boot.img" "$new_boot"

for name in boot vendor_boot; do
	[ -f "$saved_set/$name.img" ] || continue
	cp "$saved_set/$name.img" "$backup/saved-set-$name.img"
	cp "$kit/$name.img" "$saved_set/$name.img.new"
	mv "$saved_set/$name.img.new" "$saved_set/$name.img"
	say "saved Ubuntu boot set: $name.img updated (previous one in $backup)"
done

touch "$backup/complete"
finished=1
say 'lens kit installed; reboot now (sudo reboot)'
