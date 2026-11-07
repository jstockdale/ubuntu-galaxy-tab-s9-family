#!/bin/sh
# Bring the SM-X710 Wi-Fi fixes onto the installed system in place, with one
# reboot: what the next device package and vendor_boot will carry.
#
#   sudo sh ~/wifi-kit/install.sh                     everything; then reboot
#   sudo sh ~/wifi-kit/install.sh --userspace-only    address + rescan units only
#   sudo sh ~/wifi-kit/install.sh --vendor-boot-only  the ramdisk firmware only
#   sudo sh ~/wifi-kit/rollback.sh [--images-only]    undo; then reboot
#
# After the reboot:  sh ~/wifi-kit/verify.sh   (no sudo; sudo adds the dmesg checks)
#
# What it does, in this order (every line also goes to /var/log/gts9u-wifi-kit.log,
# and hangups are ignored, so a dropped SSH session does not cut it short):
#   1. checks: SM-X710, kit checksums, the rootfs ath11k hw2.1 files equal the
#      ones the new ramdisk carries, what vendor_boot holds now (must be one of
#      the bases in IMAGES, or already its Wi-Fi variant)
#   2. backups, once, in /var/backups/ubuntu-gts9u-wifi: the vendor_boot
#      partition as read, and the gts9u-pci-rescan.service it replaces
#   3. userspace: ubuntu-gts9u-wlan-address (+ .service, enabled) and
#      ubuntu-gts9u-pci-rescan with its new gts9u-pci-rescan.service; then a
#      read-only --check of the EFS address.  Nothing restarts now.
#   4. last: vendor_boot <- the Wi-Fi variant of what it held, read back from
#      the disk; a bad read-back puts the backup back.
#
# If the tablet does not come back after the reboot (no panel, no SSH), from
# TWRP (the Ubuntu root is the userdata partition):
#   [ -d /data/var ] || mount -t ext4 /dev/block/by-name/userdata /data
#   cd /data/var/backups/ubuntu-gts9u-wifi && sha256sum -c vendor_boot.sha256 &&
#     dd if=vendor_boot.img of=/dev/block/by-name/vendor_boot bs=4M conv=fsync
# (or the same dd from /data/home/jstockdale/display-exp/fix2-s2/vendor_boot.img,
# sha256 0742866c...), then reboot and run sudo sh ~/wifi-kit/rollback.sh.
set -eu

kit=$(cd "$(dirname "$0")" && pwd)
backup=/var/backups/ubuntu-gts9u-wifi
log=/var/log/gts9u-wifi-kit.log
part=/dev/disk/by-partlabel/vendor_boot
part_size=100663296
libexec=/usr/libexec
units=/usr/lib/systemd/system

say() {
	line="$(date '+%F %T') install: $*"
	echo "$line" >> "$log"
	echo "$line" 2>/dev/null || :
}
die() { say "STOP: $*"; exit 1; }
run_logged() {
	tag=$1
	shift
	out=$(mktemp)
	if "$@" > "$out" 2>&1; then rc=0; else rc=$?; fi
	while IFS= read -r l || [ -n "$l" ]; do say "$tag: $l"; done < "$out"
	rm -f "$out"
	return "$rc"
}
# The partition's content, read from the disk rather than the page cache.
partition_sha() {
	blockdev --flushbufs "$part" 2>/dev/null || true
	dd if="$part" iflag=direct bs=4M count=$((part_size / 4194304)) status=none |
		sha256sum | cut -d' ' -f1
}
packaged() { dpkg -S "$1" >/dev/null 2>&1; }

do_user=1
do_image=1
case "${1:-}" in
'') ;;
--userspace-only) do_image=0 ;;
--vendor-boot-only) do_user=0 ;;
*) echo 'usage: install.sh [--userspace-only|--vendor-boot-only]' >&2; exit 2 ;;
esac
[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> /run/gts9u-wifi-kit.lock
flock -n 9 || { echo 'install.sh or rollback.sh is already running: tail -f /var/log/gts9u-wifi-kit.log' >&2; exit 1; }
trap '' HUP PIPE
flashed=none
trap 'rc=$?; [ "$rc" = 0 ] || say "ended early (status $rc); vendor_boot: $flashed"' EXIT

tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9wifi' ||
	die 'this is not an SM-X710'
[ -b "$part" ] || die "$part does not exist"
[ "$(blockdev --getsize64 "$part")" -ge "$part_size" ] || die "$part is smaller than $part_size bytes"
say "kit $kit, boot $(cat /proc/sys/kernel/random/boot_id), kernel $(uname -r)"

# --- 1. checks ---------------------------------------------------------------
(cd "$kit" && sha256sum -c --quiet SHA256SUMS) || die 'kit checksum mismatch (copy the kit again)'
# The ramdisk serves the first probe, the rootfs every later load (resume,
# recovery): they must be the same files.
(cd / && sha256sum -c --quiet "$kit/FIRMWARE.sha256") ||
	die 'the rootfs ath11k hw2.1 files differ from the ones in the new ramdisk; refusing to mix them'

current=$(partition_sha)
target=
state=unknown
while read -r base variant file; do
	case "$base" in ''|'#'*) continue ;; esac
	if [ "$current" = "$base" ]; then state=base; target=$file; fi
	if [ "$current" = "$variant" ]; then state=variant; target=$file; fi
done < "$kit/IMAGES"
say "vendor_boot holds $(echo "$current" | cut -c1-12): $state${target:+ ($target)}"
if [ "$do_image" = 1 ] && [ "$state" = unknown ]; then
	die "vendor_boot holds an image this kit has no Wi-Fi variant for; build one on the host with tools/inplace/wifi/stage.sh --base <that vendor_boot.img>, or run --userspace-only"
fi

# --- 2. backups ----------------------------------------------------------------
if [ -e "$backup/complete" ]; then
	say "re-run: backups from $(cat "$backup/date") are kept"
else
	[ -e "$backup" ] && die "$backup exists without a 'complete' marker: inspect it first"
	[ "$state" = variant ] &&
		die 'vendor_boot already holds the Wi-Fi variant but there is no backup: rollback could not restore the original; flash the base back first (sudo sh ~/display-exp/apply.sh fix2-s2)'
	new=$backup.new
	rm -rf -- "$new"
	mkdir -p "$new" && chmod 0700 "$new" || die "cannot create $new"
	date '+%F %T' > "$new/date"
	dd if="$part" of="$new/vendor_boot.img" iflag=direct bs=4M count=$((part_size / 4194304)) status=none ||
		die 'reading vendor_boot for the backup failed'
	[ "$(sha256sum "$new/vendor_boot.img" | cut -d' ' -f1)" = "$current" ] ||
		die 'the vendor_boot backup does not match what was just read; nothing was changed'
	echo "$current  vendor_boot.img" > "$new/vendor_boot.sha256"
	# What the userspace step replaces or adds.
	for f in "$units/gts9u-pci-rescan.service" "$units/ubuntu-gts9u-wlan-address.service" \
		"$libexec/ubuntu-gts9u-pci-rescan" "$libexec/ubuntu-gts9u-wlan-address"; do
		if [ -e "$f" ]; then
			cp -a "$f" "$new/" || die "saving $f failed"
			echo "$f" >> "$new/existed"
		fi
	done
	: > "$new/complete"
	mv "$new" "$backup" || die "cannot move $new into place"
	say "backups in $backup"
fi

# --- 3. userspace ----------------------------------------------------------------
if [ "$do_user" = 1 ]; then
	# A device package that already ships these files owns them from now on.
	if packaged "$libexec/ubuntu-gts9u-wlan-address"; then
		say "userspace: ubuntu-gts9u-device $(dpkg-query -W -f '${Version}' ubuntu-gts9u-device) already ships the helpers; skipped"
	else
		# Explicit root ownership and modes: a user-owned file under /usr is
		# what broke tmpfiles and GDM after build 11.
		install -o root -g root -m 0755 "$kit/files/ubuntu-gts9u-wlan-address" "$libexec/" &&
		install -o root -g root -m 0755 "$kit/files/ubuntu-gts9u-pci-rescan" "$libexec/" &&
		install -o root -g root -m 0644 "$kit/files/ubuntu-gts9u-wlan-address.service" "$units/" &&
		install -o root -g root -m 0644 "$kit/files/gts9u-pci-rescan.service" "$units/" ||
			die 'installing the userspace files failed (backups are complete; rollback.sh restores them)'
		systemctl daemon-reload || die 'systemctl daemon-reload failed'
		run_logged systemctl systemctl enable ubuntu-gts9u-wlan-address.service gts9u-pci-rescan.service ||
			die 'enabling the units failed'
		for u in ubuntu-gts9u-wlan-address.service gts9u-pci-rescan.service; do
			[ -e "/etc/systemd/system/multi-user.target.wants/$u" ] || die "$u is not enabled"
		done
		say 'userspace: wlan-address and pci-rescan installed and enabled (they act from the next boot)'
	fi
	run_logged check "$libexec/ubuntu-gts9u-wlan-address" --check ||
		say 'WARNING: the EFS address check failed; the unit will leave the address alone'
fi

# --- 4. vendor_boot, last --------------------------------------------------------------
if [ "$do_image" = 1 ]; then
	image=$kit/$target
	want=$(sha256sum "$image" | cut -d' ' -f1)
	if [ "$state" = variant ]; then
		say 'vendor_boot already holds the Wi-Fi variant; not written again'
	else
		say "writing vendor_boot <- $target (a few seconds; do not interrupt)"
		trap '' INT TERM
		flashed=partial
		if dd if="$image" of="$part" bs=4M conv=fsync status=none && [ "$(partition_sha)" = "$want" ]; then
			flashed=wifi-variant
			say "vendor_boot now holds $(echo "$want" | cut -c1-12) (read back from the disk)"
		else
			say 'the Wi-Fi vendor_boot did not read back; putting the backup back'
			if dd if="$backup/vendor_boot.img" of="$part" bs=4M conv=fsync status=none &&
				[ "$(partition_sha)" = "$(cut -d' ' -f1 "$backup/vendor_boot.sha256")" ]; then
				flashed=original
				die 'the Wi-Fi vendor_boot failed; vendor_boot holds the original again (read back)'
			fi
			die 'the Wi-Fi vendor_boot failed AND the backup did not read back: do NOT reboot; run rollback.sh --images-only, or dd the backup from TWRP (header of install.sh)'
		fi
	fi
fi
say 'done.  Reboot now (sudo reboot), then: sh ~/wifi-kit/verify.sh'
