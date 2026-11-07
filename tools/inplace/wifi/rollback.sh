#!/bin/sh
# Undo ~/wifi-kit/install.sh: vendor_boot back to what it held before, the
# overlay's gts9u-pci-rescan.service back, ubuntu-gts9u-wlan-address and
# ubuntu-gts9u-pci-rescan removed.  Then reboot.
#
#   sudo sh ~/wifi-kit/rollback.sh
#   sudo sh ~/wifi-kit/rollback.sh --images-only    only vendor_boot
#
# From TWRP, when the tablet no longer boots (the Ubuntu root is userdata):
#   [ -d /data/var ] || mount -t ext4 /dev/block/by-name/userdata /data
#   cd /data/var/backups/ubuntu-gts9u-wifi && sha256sum -c vendor_boot.sha256 &&
#     dd if=vendor_boot.img of=/dev/block/by-name/vendor_boot bs=4M conv=fsync
# then reboot and run this script for the rest.
set -eu

backup=/var/backups/ubuntu-gts9u-wifi
log=/var/log/gts9u-wifi-kit.log
part=/dev/disk/by-partlabel/vendor_boot
part_size=100663296
libexec=/usr/libexec
units=/usr/lib/systemd/system

say() {
	line="$(date '+%F %T') rollback: $*"
	echo "$line" >> "$log"
	echo "$line" 2>/dev/null || :
}
die() { say "STOP: $*"; exit 1; }
partition_sha() {
	blockdev --flushbufs "$part" 2>/dev/null || true
	dd if="$part" iflag=direct bs=4M count=$((part_size / 4194304)) status=none |
		sha256sum | cut -d' ' -f1
}
packaged() { dpkg -S "$1" >/dev/null 2>&1; }

images_only=0
case "${1:-}" in
'') ;;
--images-only) images_only=1 ;;
*) echo 'usage: rollback.sh [--images-only]' >&2; exit 2 ;;
esac
[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> /run/gts9u-wifi-kit.lock
flock -n 9 || { echo 'install.sh or rollback.sh is already running: tail -f /var/log/gts9u-wifi-kit.log' >&2; exit 1; }
trap '' HUP PIPE
finished=0
trap 'rc=$?; [ "$finished" = 1 ] || say "ended early (status $rc); run rollback.sh again"' EXIT

tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9wifi' ||
	die 'this is not an SM-X710'
[ -e "$backup/complete" ] || die "no complete backup in $backup: install.sh never ran, or this was rolled back already"
(cd "$backup" && sha256sum -c --quiet vendor_boot.sha256) ||
	die 'the vendor_boot backup does not match its checksum; nothing was changed'
want=$(cut -d' ' -f1 "$backup/vendor_boot.sha256")

# --- vendor_boot ---------------------------------------------------------------
trap '' INT TERM
if [ "$(partition_sha)" = "$want" ]; then
	say 'vendor_boot already holds the backed-up image'
else
	say 'writing vendor_boot <- the backup (a few seconds; do not interrupt)'
	dd if="$backup/vendor_boot.img" of="$part" bs=4M conv=fsync status=none ||
		die 'writing vendor_boot failed: do NOT reboot; run rollback.sh --images-only again, or dd from TWRP (header)'
	[ "$(partition_sha)" = "$want" ] ||
		die 'vendor_boot did not read back: do NOT reboot; run rollback.sh --images-only again, or dd from TWRP (header)'
	say "vendor_boot holds $(echo "$want" | cut -c1-12) again (read back from the disk)"
fi
if [ "$images_only" = 1 ]; then
	finished=1
	say 'images back; reboot now (sudo reboot).  Run rollback.sh without --images-only for the rest.'
	exit 0
fi

# --- userspace ---------------------------------------------------------------------
existed() { grep -qx "$1" "$backup/existed" 2>/dev/null; }
restore_or_remove() { # PATH
	if packaged "$1"; then
		say "$1 belongs to a package now; left alone"
	elif existed "$1"; then
		install -o root -g root -m "$2" "$backup/${1##*/}" "$1" || die "restoring $1 failed"
		say "restored $1"
	elif [ -e "$1" ]; then
		rm -f "$1" || die "removing $1 failed"
		say "removed $1"
	fi
}
if ! packaged "$units/ubuntu-gts9u-wlan-address.service"; then
	systemctl disable ubuntu-gts9u-wlan-address.service >/dev/null 2>&1 || true
fi
restore_or_remove "$units/ubuntu-gts9u-wlan-address.service" 0644
restore_or_remove "$libexec/ubuntu-gts9u-wlan-address" 0755
restore_or_remove "$units/gts9u-pci-rescan.service" 0644
restore_or_remove "$libexec/ubuntu-gts9u-pci-rescan" 0755
systemctl daemon-reload || die 'systemctl daemon-reload failed'
# This boot's runtime state; it would be gone after the reboot anyway.
rm -f /run/systemd/network/05-ubuntu-gts9u-wlan.link
udevadm control --reload >/dev/null 2>&1 || true

mv "$backup" "$backup.rolled-back-$(date +%Y%m%d-%H%M%S)" || die "cannot retire $backup"
finished=1
say 'done; reboot now (sudo reboot)'
