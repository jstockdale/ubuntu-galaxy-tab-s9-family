#!/bin/sh
# Type-C roles kit, on top of the hang-log kit: the hang-log kernel rebuilt
# with the two TCPM changes for powered USB-C docks (the tablet asks for the
# USB host role itself before discovery, and Try.SNK waits long enough for
# the SM5714 to report a source), in boot, and its module tree.  The DTB and
# the configuration are the hang-log kit's, so vendor_boot is checked but not
# written.
#
#   sudo sh ~/typec-roles/install.sh      then reboot
#   sudo sh ~/typec-roles/rollback.sh     back to the hang-log kit, then reboot
#
# Before anything is written it checks the kit's checksums, that the hang-log
# kit is installed (boot and vendor_boot hold its images, its kernel is
# running, the module tree has its modules), that root is writable, the
# battery, dpkg and free space.  It saves boot (root-only, hash-checked) and
# the module tree, swaps the module tree, syncs, then writes boot
# synchronously and reads it back from the disk.
#
# The modules are signed with this build's own key, and the kernel is locked
# down: it refuses every module signed with another build's key.  So until
# the reboot the running kernel can load no module that is not loaded
# already (reboot soon), and a boot whose kernel and module tree are not of
# the same build loads none of the tree's modules (v4l2loopback, netfilter,
# netconsole, the secure processor's); Wi-Fi, USB networking and Bluetooth
# are built in.
#
# If the tablet does not come back: TWRP, then write the saved image back,
#   dd if=/mnt/ubuntu/var/backups/ubuntu-gts9u-typec-roles/boot-hang-log.img \
#      of=/dev/block/by-name/boot bs=4M
# and run rollback.sh on the next boot: that boot has the hang-log kernel
# with this kit's modules, which it refuses, until rollback.sh has run.
set -eu
export LC_ALL=C

kit=$(cd "$(dirname "$0")" && pwd)
release=7.2.0-rc3-dirty
modules=/usr/lib/modules/$release
backup=/var/backups/ubuntu-gts9u-typec-roles
hang_backup=/var/backups/ubuntu-gts9u-hang-log
log=/var/log/gts9u-typec-roles.log
size=100663296
hang_boot=d583546b5e5c33dbc48fbafc99b7eb039df33f64b0316e498d3f158f834ee968
hang_vendor_boot=9165f42917a8ee4069b878fc0c0660391b39087fc6f3ea2b607f626821a7dbd4
hang_build_id=68d243e908c0edfddc10429ede4bb235f205e44c
saved_set=/var/lib/gts9u-boot-sets/ubuntu

say() {
	line="$(date '+%F %T') typec-roles install: $*"
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
ko_list() { grep -E '\.ko(\.zst)?$' | sort; }

[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> /run/gts9u-typec-roles.lock
flock -n 9 || { echo 'install.sh or rollback.sh is still running; see /var/log/gts9u-typec-roles.log' >&2; exit 1; }
tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9wifi' || die 'this is not an SM-X710'
[ -e "$backup/complete" ] && die "the Type-C roles kit is already installed ($backup); run rollback.sh first"
[ -e "$backup" ] && die "$backup exists from an install that did not finish; run rollback.sh first"
[ -e "$hang_backup/complete" ] || die "the hang-log kit is not installed ($hang_backup/complete missing)"
[ "$(uname -r)" = "$release" ] || die "running kernel release $(uname -r), expected $release"
[ "$(build_id)" = "$hang_build_id" ] || die "running kernel build ID $(build_id), expected the hang-log kit's ($hang_build_id)"
(cd "$kit" && sha256sum -c --quiet SHA256SUMS) || die 'the kit does not match its checksums; nothing was changed'
new_boot=$(awk '$2 == "boot.img" {print $1}' "$kit/SHA256SUMS")
[ -n "$new_boot" ] || die 'no boot.img in SHA256SUMS'
[ "$(stat -c %s "$kit/boot.img")" = "$size" ] || die 'boot.img is not the partition size'
[ -f "$kit/modules-$release.tar" ] || die "no modules-$release.tar in the kit"
[ "$(part_sha boot)" = "$hang_boot" ] || die 'the boot partition does not hold the hang-log kit'
[ "$(part_sha vendor_boot)" = "$hang_vendor_boot" ] || die 'vendor_boot does not hold the hang-log kit'
# The kit replaces the whole module tree with the same list of modules built
# for the new kernel.  A module added by hand since would be left behind.
tar -tf "$kit/modules-$release.tar" | sed -n "s|^usr/lib/modules/$release/||p" | ko_list > /run/gts9u-typec-roles.kit-modules
(cd "$modules" && find . -type f | sed 's|^\./||' | ko_list) > /run/gts9u-typec-roles.installed-modules
cmp -s /run/gts9u-typec-roles.kit-modules /run/gts9u-typec-roles.installed-modules || {
	diff /run/gts9u-typec-roles.installed-modules /run/gts9u-typec-roles.kit-modules >> "$log" 2>&1 || :
	die "the installed module tree does not have the hang-log kit's list of modules (differences in $log)"
}
rm -f /run/gts9u-typec-roles.kit-modules /run/gts9u-typec-roles.installed-modules
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
dd if=/dev/disk/by-partlabel/boot of="$backup/boot-hang-log.img" iflag=direct \
	bs=4M count=$((size / 4194304)) status=none
[ "$(sha256sum "$backup/boot-hang-log.img" | cut -d' ' -f1)" = "$hang_boot" ] ||
	die 'the saved boot image does not match the hang-log kit'
# The way back has to be on the disk before anything else changes: a power
# failure may keep a later rename and lose this file's data.
sync || die 'sync failed'
say "boot saved to $backup"

# The module tree first, the flash last, so that a failure before it leaves
# the hang-log kit bootable, and rollback.sh can undo any step.
stage=/usr/lib/modules/.typec-roles-$release
rm -rf -- "$stage"
mkdir -p "$stage"
tar -C "$stage" --strip-components=4 -xf "$kit/modules-$release.tar" || die 'extracting the module tree failed'
[ -f "$stage/modules.dep" ] || die 'module tree incomplete after extraction'
mv "$modules" "$backup/modules-$release" || die 'moving the hang-log module tree aside failed'
mv "$stage" "$modules" || die 'moving the new module tree into place failed'
depmod -a "$release" || die 'depmod failed'
# The new kernel refuses every module but these: they have to be on the disk
# before it is.
sync || die 'sync failed'
say "modules: $(find "$modules" -name '*.ko*' | wc -l) modules installed (hang-log tree in $backup)"

say 'writing boot (a few seconds; do not interrupt)'
write_part boot "$kit/boot.img" "$new_boot"

# Keep the saved Ubuntu boot set (dual boot) in step with the partition.
if [ -f "$saved_set/boot.img" ]; then
	cp "$saved_set/boot.img" "$backup/saved-set-boot.img.new"
	mv "$backup/saved-set-boot.img.new" "$backup/saved-set-boot.img"
	cp "$kit/boot.img" "$saved_set/boot.img.new"
	mv "$saved_set/boot.img.new" "$saved_set/boot.img"
	say "saved Ubuntu boot set: boot.img updated (what rollback.sh restores is in $backup)"
fi

touch "$backup/complete"
finished=1
sync || die 'the kit is in place, but sync failed: run "sudo sync" until it succeeds, then reboot'
say 'until the reboot the running kernel can load no module it has not loaded yet'
say 'while the kit is installed, do not upgrade ubuntu-gts9u-device: its updates/qcom_spss_irq.ko is signed for another kernel'
say 'Type-C roles kit installed; reboot now (sudo reboot)'
