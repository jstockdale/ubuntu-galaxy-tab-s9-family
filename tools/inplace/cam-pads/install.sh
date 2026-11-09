#!/bin/sh
# Lens-pads kit, on top of the BT UART kit: the same kernel and modules, and
# the DTB with stock's pad settings (4 mA, weak pull-up) on CCI1 master 0 (the
# rear DW9808 lens and its EEPROM), in boot (appended) and in vendor_boot.
#
#   sudo sh ~/cam-pads/install.sh      then reboot
#   sudo sh ~/cam-pads/rollback.sh     back to the BT UART kit, then reboot
#
# Before anything is written it checks the kit's checksums, that the BT UART
# kit is installed (boot and vendor_boot hold its and the lens kit's images),
# that root is writable and the battery.  It saves both partitions
# (root-only, hash-checked), then writes vendor_boot and boot synchronously,
# each read back from the disk.
#
# If the tablet does not come back: TWRP, then write the saved images back,
#   dd if=/mnt/ubuntu/var/backups/ubuntu-gts9u-cam-pads/vendor_boot-bt.img \
#      of=/dev/block/by-name/vendor_boot bs=4M
#   dd if=/mnt/ubuntu/var/backups/ubuntu-gts9u-cam-pads/boot-bt.img \
#      of=/dev/block/by-name/boot bs=4M
set -eu
export LC_ALL=C

kit=$(cd "$(dirname "$0")" && pwd)
backup=/var/backups/ubuntu-gts9u-cam-pads
bt_backup=/var/backups/ubuntu-gts9u-bt-uart
log=/var/log/gts9u-cam-pads.log
size=100663296
bt_boot=faa774ad340f66c07f1f0ad4948b6edc7e141a0cd2235b473234070588d317ba
bt_vendor_boot=b18510f38bae75d285347baef80a2beebfd57e246a2a7e7a4c7ce17983bd166a
saved_set=/var/lib/gts9u-boot-sets/ubuntu

say() {
	line="$(date '+%F %T') cam-pads install: $*"
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
exec 9> /run/gts9u-cam-pads.lock
flock -n 9 || { echo 'install.sh or rollback.sh is still running; see /var/log/gts9u-cam-pads.log' >&2; exit 1; }
tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9wifi' || die 'this is not an SM-X710'
[ -e "$backup/complete" ] && die "the pads kit is already installed ($backup); run rollback.sh first"
[ -e "$bt_backup/complete" ] || die "the BT UART kit is not installed ($bt_backup/complete missing)"
(cd "$kit" && sha256sum -c --quiet SHA256SUMS) || die 'the kit does not match its checksums; nothing was changed'
new_boot=$(awk '$2 == "boot.img" {print $1}' "$kit/SHA256SUMS")
new_vendor_boot=$(awk '$2 == "vendor_boot.img" {print $1}' "$kit/SHA256SUMS")
for f in boot.img vendor_boot.img; do
	[ "$(stat -c %s "$kit/$f")" = "$size" ] || die "$f is not the partition size"
done
[ "$(part_sha boot)" = "$bt_boot" ] || die 'the boot partition does not hold the BT UART kit'
[ "$(part_sha vendor_boot)" = "$bt_vendor_boot" ] || die 'vendor_boot does not hold the lens kit'
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
dd if=/dev/disk/by-partlabel/boot of="$backup/boot-bt.img" iflag=direct \
	bs=4M count=$((size / 4194304)) status=none
[ "$(sha256sum "$backup/boot-bt.img" | cut -d' ' -f1)" = "$bt_boot" ] ||
	die 'the saved boot image does not match the BT UART kit'
dd if=/dev/disk/by-partlabel/vendor_boot of="$backup/vendor_boot-bt.img" iflag=direct \
	bs=4M count=$((size / 4194304)) status=none
[ "$(sha256sum "$backup/vendor_boot-bt.img" | cut -d' ' -f1)" = "$bt_vendor_boot" ] ||
	die 'the saved vendor_boot image does not match the lens kit'
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
say 'pads kit installed; reboot now (sudo reboot)'
