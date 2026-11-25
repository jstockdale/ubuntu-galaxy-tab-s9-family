#!/bin/sh
# Type-C roles kit for the SM-X910, on top of release v1.2.5: the release
# kernel rebuilt with the TCPM changes for powered USB-C docks (the tablet
# asks for the USB host role itself before discovery, Try.SNK waits long
# enough for the SM5714 to report a source, and three fixes for a swap asked
# for from userspace), with the USB power domains kept in retention across
# suspend and with a NULL check in the dwc3-qcom glue, in boot, and its
# module tree; since revision 8 also the deep-sleep groundwork (README,
# "Revision 8").  The DTB is the release's (and up to revision 7 the
# configuration too), so vendor_boot is checked but not written, and the
# module list is the release's.
#
#   sudo sh ~/typec-roles/install.sh      then reboot
#   sudo sh ~/typec-roles/rollback.sh     back to the release kernel, then reboot
#
# Before anything is written it checks the kit's checksums, that the release
# is installed (boot and vendor_boot hold its images, its kernel is running,
# the module tree has its modules), that root is writable, the battery, dpkg
# and free space.  It saves boot (root-only, hash-checked) and the module
# tree, swaps the module tree, syncs, then writes boot synchronously and
# reads it back from the disk.
#
# Over an earlier revision of this kit it runs as an upgrade, with no
# rollback in between: the release saved by the first install stays the way
# back, the earlier kit's module tree is replaced, and boot is written again.
# Then boot may hold the release (after a restore from TWRP), a revision of
# this kit, or this one from an upgrade that stopped.
#
# The kernel now does what the typec-sink-host stopgap service did from
# userspace, and the two would get in each other's way: the service is
# disabled here (it keeps running until the reboot) and rollback.sh enables
# it again.
#
# The modules are signed with this build's own key, and the kernel is locked
# down: it refuses every module signed with another build's key.  So until
# the reboot the running kernel can load no module that is not loaded
# already (reboot soon), and a boot whose kernel and module tree are not of
# the same build has no Wi-Fi, no fingerprint reader and no NAT.
#
# If the tablet does not come back: TWRP, then write the saved image back,
#   dd if=/mnt/ubuntu/var/backups/ubuntu-gts9u-typec-roles/boot-release.img \
#      of=/dev/block/by-name/boot bs=4M
# and run rollback.sh on the next boot, from the tablet's own terminal or
# over the USB cable: that boot has the release kernel with the kit's
# modules, and Wi-Fi is down until rollback.sh has run.
set -eu
export LC_ALL=C

kit=$(cd "$(dirname "$0")" && pwd)
release=7.2.0-rc3-dirty
modules=/usr/lib/modules/$release
# Where the new tree is unpacked, and where an upgrade puts the earlier
# kit's tree while the new one moves in.
stage=/usr/lib/modules/.typec-roles-$release
old=/usr/lib/modules/.typec-roles-old-$release
backup=/var/backups/ubuntu-gts9u-typec-roles
log=/var/log/gts9u-typec-roles.log
size=100663296
# Release v1.2.5 (out/bundle of 2026-11-10).
base_boot=d09cb64ff411ca39feab17c1c4330081619df9304b6a5b974bf23ccab915bdac
base_vendor_boot=f3e8c6371d0a943a53d07787fa3e87c234967bcbee41210471f6cf46c0ee573d
base_build_id=3447147dc203542e441eebfa4d62e60c57100f50
# Revision 3 of this kit (2026-11-21), which did not record what it wrote.
kit_r3_boot=e627127ee00ca65f9873e2e62f748c59ffb28856e6d0c7d9f0ca8da171df9618
saved_set=/var/lib/gts9u-boot-sets/ubuntu
stopgap=typec-sink-host.service

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
tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9uwifi' || die 'this is not an SM-X910'
upgrade=0
if [ -e "$backup/complete" ]; then
	# A revision of this kit is installed.  What the first install saved has
	# to be whole: it is the only way back to the release.
	upgrade=1
	[ "$(sha256sum "$backup/boot-release.img" 2>/dev/null | cut -d' ' -f1)" = "$base_boot" ] ||
		die "the release boot image saved in $backup is missing or changed; nothing was changed"
	[ -f "$backup/modules-$release/modules.dep" ] ||
		die "the release module tree saved in $backup is missing; nothing was changed"
elif [ -e "$backup" ]; then
	die "$backup exists from an install that did not finish; run rollback.sh first"
fi
[ "$(uname -r)" = "$release" ] || die "running kernel release $(uname -r), expected $release"
[ "$upgrade" = 1 ] || [ "$(build_id)" = "$base_build_id" ] ||
	die "running kernel build ID $(build_id), expected release v1.2.5's ($base_build_id)"
(cd "$kit" && sha256sum -c --quiet SHA256SUMS) || die 'the kit does not match its checksums; nothing was changed'
new_boot=$(awk '$2 == "boot.img" {print $1}' "$kit/SHA256SUMS")
[ -n "$new_boot" ] || die 'no boot.img in SHA256SUMS'
[ "$(stat -c %s "$kit/boot.img")" = "$size" ] || die 'boot.img is not the partition size'
[ -f "$kit/modules-$release.tar" ] || die "no modules-$release.tar in the kit"
for p in boot vendor_boot; do
	[ "$(blockdev --getsize64 "/dev/disk/by-partlabel/$p")" = "$size" ] || die "the $p partition is not $size bytes"
done
on_boot=$(part_sha boot)
if [ "$upgrade" = 1 ]; then
	was=$(cat "$backup/installed-boot" 2>/dev/null) || was=
	# After an upgrade that stopped, boot may be half-written: anything goes.
	[ -e "$backup/upgrading" ] || case $on_boot in
	"$new_boot")
		[ "$was" != "$new_boot" ] ||
			die "this revision of the Type-C roles kit is already installed ($backup); nothing was changed" ;;
	"$base_boot"|"$kit_r3_boot"|"${was:-none}") ;;
	*) die "the boot partition holds neither release v1.2.5 nor a revision of this kit ($on_boot)" ;;
	esac
else
	[ "$on_boot" = "$base_boot" ] || die "the boot partition does not hold release v1.2.5 ($on_boot)"
fi
[ "$(part_sha vendor_boot)" = "$base_vendor_boot" ] || die "vendor_boot does not hold release v1.2.5 ($(part_sha vendor_boot))"
# The kit replaces the whole module tree with the same list of modules built
# for the new kernel.  A module added by hand since would be left behind.
if [ "$upgrade" = 1 ] && [ -e "$backup/upgrading" ] && [ ! -e "$modules" ] && [ -f "$old/modules.dep" ]; then
	# An upgrade stopped between its two moves: the earlier kit's tree is
	# whole, next to where it belongs.
	mv "$old" "$modules" || die "moving $old back to $modules failed; run rollback.sh"
	say "an upgrade had stopped in the module swap: the earlier module tree is back in $modules"
fi
[ -d "$modules" ] || die "no module tree $modules; run rollback.sh"
tar -tf "$kit/modules-$release.tar" | sed -n "s|^usr/lib/modules/$release/||p" | ko_list > /run/gts9u-typec-roles.kit-modules
(cd "$modules" && find . -type f | sed 's|^\./||' | ko_list) > /run/gts9u-typec-roles.installed-modules
cmp -s /run/gts9u-typec-roles.kit-modules /run/gts9u-typec-roles.installed-modules || {
	diff /run/gts9u-typec-roles.installed-modules /run/gts9u-typec-roles.kit-modules >> "$log" 2>&1 || :
	die "the installed module tree does not have the release's list of modules (differences in $log)"
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
if [ "$upgrade" = 1 ]; then
	trap '[ "$finished" = 1 ] || say "STOP: the upgrade ended early; do NOT reboot before running install.sh again, or rollback.sh"' EXIT
else
	trap '[ "$finished" = 1 ] || say "STOP: install ended early; do NOT reboot before running rollback.sh"' EXIT
fi
say "boot $(cat /proc/sys/kernel/random/boot_id), kernel $(uname -v)"
if [ "$upgrade" = 1 ]; then
	# From here to the end the module tree and boot may not belong together.
	touch "$backup/upgrading"
	say "upgrade: boot holds $(echo "$on_boot" | cut -c1-12); the release saved in $backup stays the way back"
else
	mkdir -p "$backup"
	chmod 0700 "$backup"
	dd if=/dev/disk/by-partlabel/boot of="$backup/boot-release.img" iflag=direct \
		bs=4M count=$((size / 4194304)) status=none
	[ "$(sha256sum "$backup/boot-release.img" | cut -d' ' -f1)" = "$base_boot" ] ||
		die 'the saved boot image does not match release v1.2.5'
	# The way back has to be on the disk before anything else changes: a
	# power failure may keep a later rename and lose this file's data.
	sync || die 'sync failed'
	say "boot saved to $backup"
fi

# The module tree first, the flash last, so that a failure before it leaves
# the release bootable, and rollback.sh can undo any step.
rm -rf -- "$stage" "$old"
mkdir -p "$stage"
tar -C "$stage" --strip-components=4 -xf "$kit/modules-$release.tar" || die 'extracting the module tree failed'
[ -f "$stage/modules.dep" ] || die 'module tree incomplete after extraction'
if [ "$upgrade" = 1 ]; then
	# The release tree is in the backup already; the earlier kit's goes.
	mv "$modules" "$old" || die 'moving the earlier module tree aside failed'
else
	mv "$modules" "$backup/modules-$release" || die 'moving the release module tree aside failed'
fi
mv "$stage" "$modules" || die 'moving the new module tree into place failed'
rm -rf -- "$old"
depmod -a "$release" || die 'depmod failed'
# The new kernel refuses every module but these: they have to be on the disk
# before it is.
sync || die 'sync failed'
say "modules: $(find "$modules" -name '*.ko*' | wc -l) modules installed (release tree in $backup)"

say 'writing boot (a few seconds; do not interrupt)'
write_part boot "$kit/boot.img" "$new_boot"

# Keep the saved Ubuntu boot set (dual boot) in step with the partition.
# rollback.sh puts back what the first install found there; for a set that
# was made later, that is the release image.
if [ -f "$saved_set/boot.img" ]; then
	if [ ! -f "$backup/saved-set-boot.img" ]; then
		[ "$upgrade" = 1 ] && from=$backup/boot-release.img || from=$saved_set/boot.img
		cp "$from" "$backup/saved-set-boot.img.new"
		mv "$backup/saved-set-boot.img.new" "$backup/saved-set-boot.img"
	fi
	cp "$kit/boot.img" "$saved_set/boot.img.new"
	mv "$saved_set/boot.img.new" "$saved_set/boot.img"
	say "saved Ubuntu boot set: boot.img updated (what rollback.sh restores is in $backup)"
fi

# The stopgap writes the data role from userspace; with this kernel it would
# collide with the kernel's own request.  The kernel and its modules are in
# place by now, so a failure here is reported and not a reason to stop.
if systemctl is-enabled --quiet "$stopgap" 2>/dev/null; then
	if systemctl disable "$stopgap" >> "$log" 2>&1; then
		touch "$backup/stopgap-was-enabled"
		say "$stopgap disabled from the next boot (rollback.sh enables it again)"
	else
		say "disabling $stopgap FAILED; do it by hand before the reboot: sudo systemctl disable $stopgap"
	fi
fi

# What this kit wrote, for the next revision's install.sh.
echo "$new_boot" > "$backup/installed-boot.new"
mv "$backup/installed-boot.new" "$backup/installed-boot"
rm -f "$backup/upgrading"
touch "$backup/complete"
finished=1
sync || die 'the kit is in place, but sync failed: run "sudo sync" until it succeeds, then reboot'
say 'until the reboot the running kernel can load no module it has not loaded yet'
say 'while the kit is installed: rollback.sh first, then upgrade ubuntu-gts9u-device or ubuntu-gts9u-hardware'
say 'Type-C roles kit installed; reboot now (sudo reboot)'
