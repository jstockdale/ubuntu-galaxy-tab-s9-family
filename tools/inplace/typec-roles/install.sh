#!/bin/sh
# Type-C roles kit, on top of the hang-log kit: the hang-log kernel rebuilt
# with the TCPM changes for powered USB-C docks (the tablet asks for the USB
# host role itself before discovery, Try.SNK waits long enough for the SM5714
# to report a source, and three fixes for a swap asked for from userspace),
# with the USB power domains kept in retention across suspend and with a NULL
# check in the dwc3-qcom glue, and since revision 8 with the UFS PHY's PCS
# reset before every calibration on this tablet too, and its diagnostic,
# since revision 9 with the deep-sleep debug switches (all off at every
# boot), since revision 10 with three upstream fixes for the SoC's sleep
# (the PCIe PIPE clock mux, the GPU RSC's votes, the UFS lane clocks) and a
# second UFS PHY diagnostic, and since revision 11 with pcie_0_gdsc
# collapsed in system suspend when the PCIe host has powered itself down and
# kept on while it keeps its link (the guard; its debug switch on at every
# boot), and a debug switch that lets the PCIe root port go to D3hot (off at
# every boot), and since revision 12 with the AOP's log of what keeps CX from
# collapsing (a debug switch, off at every boot) and the VA macro's and the
# DSI PHY's clocks released in system sleep, in boot, and its module tree.
# The DTB and the configuration are the hang-log kit's, so vendor_boot is
# checked but not written.
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
# Over an earlier revision of this kit it runs as an upgrade, with no
# rollback in between: the hang-log kit saved by the first install stays the
# way back, the earlier kit's module tree is replaced, and boot is written
# again.  In place of the first install's checks of the hang-log kit it
# checks that what the first install saved is whole (the boot image by its
# hash, the module tree by its list of modules), that boot holds the hang-log
# kit (after a restore from TWRP) or a revision of this kit, that the running
# kernel is one of theirs, and that the module tree in place has the same
# list of modules.  vendor_boot holds the hang-log kit's image throughout:
# no revision writes it.
#
# Revisions 1 to 7 recorded nothing; they are known by their boot images and
# kernels below.  Every later install or upgrade adds a line for what it
# writes to $backup/revisions before it writes it, and that line is on the
# disk before boot is written: a later run knows each boot image, and each
# kernel, that this kit can have left.
#
# An upgrade marks itself ($backup/upgrading) before it changes anything and
# removes the mark at the end; once it has begun, Ctrl-C, hangups and TERM
# are ignored.  Stopped by an error, a power cut or a kill, it leaves the mark
# and one of these, from each of which install.sh run again finishes it and
# rollback.sh undoes it:
#   - nothing else changed, or its line added to the record (added again), or
#     the new tree partly unpacked in $stage (unpacked again);
#   - the earlier kit's tree moved aside to $old and none in its place (moved
#     back, then swapped again);
#   - the new tree in place, the earlier one still in $old or gone, depmod not
#     run (swapped again);
#   - boot partly or wholly written (boot is not checked while the mark is
#     there: it is written again);
#   - the saved boot set's copy made, or the set updated, or not (made once,
#     updated again).
# Without the mark: boot holding this revision, with the record's last line
# naming it, is this revision installed already, and nothing is done.
#
# The modules are signed with the build's key, and the kernel is locked down:
# it refuses every module signed with another key.  So until the reboot of a
# first install the running kernel can load no module that is not loaded
# already (reboot soon), and the hang-log kernel with this kit's module tree
# loads none of the tree's modules (v4l2loopback, netfilter, netconsole and
# the secure processor's), while Wi-Fi, USB networking and Bluetooth
# are built in.  Revisions 1 to 11 all have one key: the kernel of one takes
# the signature of another's modules, but a module whose BTF does not match
# the running kernel's is refused all the same (CONFIG_DEBUG_INFO_BTF_MODULES
# without CONFIG_MODULE_ALLOW_BTF_MISMATCH), and revisions whose kernels
# differ in a type do not match.  Reboot soon after an upgrade as well.
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
# Where the new tree is unpacked, and where an upgrade puts the earlier
# kit's tree while the new one moves in.
stage=/usr/lib/modules/.typec-roles-$release
old=/usr/lib/modules/.typec-roles-old-$release
backup=/var/backups/ubuntu-gts9u-typec-roles
hang_backup=/var/backups/ubuntu-gts9u-hang-log
log=/var/log/gts9u-typec-roles.log
size=100663296
hang_boot=d583546b5e5c33dbc48fbafc99b7eb039df33f64b0316e498d3f158f834ee968
hang_vendor_boot=9165f42917a8ee4069b878fc0c0660391b39087fc6f3ea2b607f626821a7dbd4
hang_build_id=68d243e908c0edfddc10429ede4bb235f205e44c
# Revisions 1 to 7 of this kit (2026-11-20 and 21), which recorded nothing:
# one line each, the sha256 of boot.img and the kernel's build ID (from the
# packed kit's SHA256SUMS and BUILD_ID) as $backup/revisions has them, then
# the revision.  4 and 5 have the same kernel.
earlier='45b5e2d884d937b01c784f6cc6ba581aa97734d3bd8a0a0cf385320bab9c68f5 65ce39dc68d2e85591e088300f6b0ccf81f5b243 r1
3c483cf09959aec58213c0ad0d38466e78e5312a362183316e80f4ddc46780a9 7a614f2582c59b33e37632ef82a16ade7510aa6f r2
743c7f7cf859c3e155720e340b8ba39dcbeeb80d8bdf1b6be9c353867c3e1604 4ad9cb230f76092db3dbcc8291ae38367bfe2b4e r3
5794ceefd658b9f41d1adfbbaa4b45acb4e64c616f6e76acb2bce5805547e31f 6479859ed5e3c2c5d7cc43c085d2141e14b4fd2a r4,r5
e11e4e09668cd509b6b848ddcc32f0a7bf74cdfcb3da496caf8617f2e7b4e354 326b061707e9bd906d1e5eb4bc8ea158e19f42bb r6
613c0c12817c3286c4b927539245e41882c7729ec2b712e1eb1e77ae59d3a9d2 84c2748b5ee4b283b56a77579b0b0758f88117e1 r7'
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
tree_list() { (cd "$1" && find . -type f | sed 's|^\./||' | ko_list); }
# Is $2 the boot image ($1 = 1) or the kernel build ID ($1 = 2) of a revision
# of this kit: one of revisions 1 to 7, or one in the record?
kit_knows() {
	[ -n "$2" ] || return 1
	{ printf '%s\n' "$earlier"; cat "$backup/revisions" 2>/dev/null || :; } |
		awk -v f="$1" -v x="$2" '$f == x { found = 1 } END { exit !found }'
}

[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> /run/gts9u-typec-roles.lock
flock -n 9 || { echo 'install.sh or rollback.sh is still running; see /var/log/gts9u-typec-roles.log' >&2; exit 1; }
tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9wifi' || die 'this is not an SM-X710'
upgrade=0
if [ -e "$backup/complete" ]; then
	# A revision of this kit is installed, or an upgrade from one stopped.
	# What the first install saved has to be whole: it is the only way back
	# to the hang-log kit.
	upgrade=1
	[ "$(sha256sum "$backup/boot-hang-log.img" 2>/dev/null | cut -d' ' -f1)" = "$hang_boot" ] ||
		die "the hang-log boot image saved in $backup is missing or changed; nothing was changed"
	# A rollback.sh that stopped after it had moved the tree back leaves this.
	[ -f "$backup/modules-$release/modules.dep" ] ||
		die "the hang-log module tree saved in $backup is missing; nothing was changed (if rollback.sh stopped, run it again)"
elif [ -e "$backup" ]; then
	die "$backup exists from an install that did not finish; run rollback.sh first"
fi
[ -e "$hang_backup/complete" ] || die "the hang-log kit is not installed ($hang_backup/complete missing)"
[ "$(uname -r)" = "$release" ] || die "running kernel release $(uname -r), expected $release"
[ "$upgrade" = 1 ] || [ "$(build_id)" = "$hang_build_id" ] ||
	die "running kernel build ID $(build_id), expected the hang-log kit's ($hang_build_id)"
(cd "$kit" && sha256sum -c --quiet SHA256SUMS) || die 'the kit does not match its checksums; nothing was changed'
new_boot=$(awk '$2 == "boot.img" {print $1}' "$kit/SHA256SUMS")
[ -n "$new_boot" ] || die 'no boot.img in SHA256SUMS'
[ "$(stat -c %s "$kit/boot.img")" = "$size" ] || die 'boot.img is not the partition size'
[ -f "$kit/modules-$release.tar" ] || die "no modules-$release.tar in the kit"
# The kernel in boot.img, for the record.
new_id=$(cat "$kit/BUILD_ID" 2>/dev/null) || new_id=
case $new_id in *[!0-9a-f]*) new_id= ;; esac
[ "${#new_id}" = 40 ] || die 'BUILD_ID in the kit is not a kernel build ID'
on_boot=$(part_sha boot)
if [ "$upgrade" = 1 ]; then
	# The hang-log kit's kernel runs after a restore of boot from TWRP, or
	# before the first install's reboot; a revision of this kit's, this one's
	# included, after an install or upgrade, rebooted or not.
	running=$(build_id)
	[ "$running" = "$hang_build_id" ] || [ "$running" = "$new_id" ] || kit_knows 2 "$running" ||
		die "running kernel build ID ${running:-unknown}, neither the hang-log kit's nor a revision of this kit's"
	installed=$(tail -n 1 "$backup/revisions" 2>/dev/null | cut -d' ' -f1)
	# After an upgrade that stopped, boot may be half-written: anything goes.
	[ -e "$backup/upgrading" ] || case $on_boot in
	"$new_boot")
		[ "$installed" != "$new_boot" ] ||
			die "this revision of the Type-C roles kit is already installed ($backup); nothing was changed" ;;
	"$hang_boot") ;;
	*) kit_knows 1 "$on_boot" ||
		die "the boot partition holds neither the hang-log kit nor a revision of this kit ($on_boot)" ;;
	esac
else
	[ "$on_boot" = "$hang_boot" ] || die 'the boot partition does not hold the hang-log kit'
fi
[ "$(part_sha vendor_boot)" = "$hang_vendor_boot" ] || die 'vendor_boot does not hold the hang-log kit'
# The kit replaces the whole module tree with the same list of modules built
# for the new kernel.  A module added by hand since would be left behind.
tree=$modules
if [ "$upgrade" = 1 ]; then
	if [ ! -e "$modules" ] && [ -e "$backup/upgrading" ] && [ -f "$old/modules.dep" ]; then
		# An upgrade stopped between its two moves: the earlier kit's tree
		# is whole, next to where it belongs, and goes back before the swap.
		tree=$old
	fi
	[ -d "$tree" ] || die "no module tree $modules; run rollback.sh"
fi
tar -tf "$kit/modules-$release.tar" | sed -n "s|^usr/lib/modules/$release/||p" | ko_list > /run/gts9u-typec-roles.kit-modules
tree_list "$tree" > /run/gts9u-typec-roles.installed-modules
cmp -s /run/gts9u-typec-roles.kit-modules /run/gts9u-typec-roles.installed-modules || {
	diff /run/gts9u-typec-roles.installed-modules /run/gts9u-typec-roles.kit-modules >> "$log" 2>&1 || :
	die "the installed module tree does not have the hang-log kit's list of modules (differences in $log)"
}
if [ "$upgrade" = 1 ]; then
	# The tree rollback.sh puts back has to be whole as well.
	tree_list "$backup/modules-$release" > /run/gts9u-typec-roles.saved-modules
	cmp -s /run/gts9u-typec-roles.kit-modules /run/gts9u-typec-roles.saved-modules || {
		diff /run/gts9u-typec-roles.saved-modules /run/gts9u-typec-roles.kit-modules >> "$log" 2>&1 || :
		die "the hang-log module tree saved in $backup does not have the hang-log kit's list of modules (differences in $log); nothing was changed"
	}
fi
rm -f /run/gts9u-typec-roles.kit-modules /run/gts9u-typec-roles.installed-modules \
	/run/gts9u-typec-roles.saved-modules
# ext4 that gave up on a failing disk keeps "rw" among its options and adds
# emergency_ro or shutdown.
root_opts=$(findmnt -no OPTIONS /)
echo "$root_opts" | tr ',' '\n' | grep -qx rw &&
	! echo "$root_opts" | tr ',' '\n' | grep -qxE 'emergency_ro|shutdown' ||
	die 'the root filesystem is not writable'
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
	say "upgrade: boot holds $(echo "$on_boot" | cut -c1-12); the hang-log kit saved in $backup stays the way back"
else
	mkdir -p "$backup"
	chmod 0700 "$backup"
	dd if=/dev/disk/by-partlabel/boot of="$backup/boot-hang-log.img" iflag=direct \
		bs=4M count=$((size / 4194304)) status=none
	[ "$(sha256sum "$backup/boot-hang-log.img" | cut -d' ' -f1)" = "$hang_boot" ] ||
		die 'the saved boot image does not match the hang-log kit'
	# The way back has to be on the disk before anything else changes: a
	# power failure may keep a later rename and lose this file's data.
	sync || die 'sync failed'
	say "boot saved to $backup"
fi
# What this revision writes, for the next revision's install.sh; the sync
# before the flash puts it on the disk.
echo "$new_boot $new_id" >> "$backup/revisions"

# The module tree first, the flash last, so that a failure before it leaves
# the kernel on boot bootable, and rollback.sh can undo any step.
if [ "$tree" = "$old" ]; then
	mv "$old" "$modules" || die "moving $old back to $modules failed; run rollback.sh"
	say "an upgrade had stopped in the module swap: the earlier module tree is back in $modules"
fi
rm -rf -- "$stage" "$old"
mkdir -p "$stage"
tar -C "$stage" --strip-components=4 -xf "$kit/modules-$release.tar" || die 'extracting the module tree failed'
[ -f "$stage/modules.dep" ] || die 'module tree incomplete after extraction'
if [ "$upgrade" = 1 ]; then
	# The hang-log tree is in the backup already; the earlier kit's goes.
	mv "$modules" "$old" || die 'moving the earlier module tree aside failed'
else
	mv "$modules" "$backup/modules-$release" || die 'moving the hang-log module tree aside failed'
fi
mv "$stage" "$modules" || die 'moving the new module tree into place failed'
rm -rf -- "$old"
depmod -a "$release" || die 'depmod failed'
# The new kernel refuses every module but these: they have to be on the disk
# before it is.
sync || die 'sync failed'
say "modules: $(find "$modules" -name '*.ko*' | wc -l) modules installed (hang-log tree in $backup)"

say 'writing boot (a few seconds; do not interrupt)'
write_part boot "$kit/boot.img" "$new_boot"

# Keep the saved Ubuntu boot set (dual boot) in step with the partition.
# rollback.sh puts back what the first install found there; for a set that
# was made later, that is the hang-log kit's image.
if [ -f "$saved_set/boot.img" ]; then
	if [ ! -f "$backup/saved-set-boot.img" ]; then
		[ "$upgrade" = 1 ] && from=$backup/boot-hang-log.img || from=$saved_set/boot.img
		cp "$from" "$backup/saved-set-boot.img.new"
		mv "$backup/saved-set-boot.img.new" "$backup/saved-set-boot.img"
	fi
	cp "$kit/boot.img" "$saved_set/boot.img.new"
	mv "$saved_set/boot.img.new" "$saved_set/boot.img"
	say "saved Ubuntu boot set: boot.img updated (what rollback.sh restores is in $backup)"
fi

rm -f "$backup/upgrading"
touch "$backup/complete"
finished=1
sync || die 'the kit is in place, but sync failed: run "sudo sync" until it succeeds, then reboot'
if [ "$upgrade" = 1 ]; then
	say 'until the reboot the module tree in place is built for the new kernel, not for the running one'
else
	say 'until the reboot the running kernel can load no module it has not loaded yet'
fi
say 'while the kit is installed, do not upgrade ubuntu-gts9u-device: its updates/qcom_spss_irq.ko is signed for another kernel'
say 'Type-C roles kit installed; reboot now (sudo reboot)'
