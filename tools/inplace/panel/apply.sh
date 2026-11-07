#!/bin/sh
# Flash one DISP-5 (panel cold boot) vendor_boot variant, or fix2-s2's own
# vendor_boot back.  Root; the change takes effect at the next boot.
#
#   sudo sh ~/panel/apply.sh panel-ref|panel-e0|panel-e1|panel-e2|fix2-s2
#
# Images are read from ~/display-exp/<variant>/vendor_boot.img (the layout
# ~/display-exp/apply.sh uses).  Only vendor_boot is written; boot must hold
# the fix2-s2 kernel, which carries every switch the variants use.  Checks
# before anything is written: SM-X710, kit checksums, image checksum against
# IMAGES.sha256, boot partition = fix2-s2.  The vendor_boot found on the first
# run is kept in /var/backups/ubuntu-gts9u-panel-exp/ (rollback.sh falls back
# to it).  Every write is read back from the disk.  Log:
# /var/log/gts9u-panel-exp.log.
set -eu

kit=$(cd "$(dirname "$0")" && pwd)
user_home=$(getent passwd "${SUDO_USER:-jstockdale}" | cut -d: -f6)
images=${PANEL_EXP_IMAGES:-$user_home/display-exp}
backup_dir=/var/backups/ubuntu-gts9u-panel-exp
log=/var/log/gts9u-panel-exp.log
boot_size=100663296
fix2s2_boot=dcf881554a2790b02ce230790cd868ee06f8c9e1ddcb8e7dc48e7c0cc20f09b0
fix2s2_vendor_boot=0742866cbfb01da797aed4129eacfb13655ca0476ec13546f210bf1c874f4fd5

say() { echo "apply: $*" | tee -a "$log"; }
die() { say "REFUSED: $*"; exit 1; }

[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
variant=${1:-}
case "$variant" in
	panel-ref|panel-e0|panel-e1|panel-e2|fix2-s2) ;;
	*) echo "usage: sudo sh $0 panel-ref|panel-e0|panel-e1|panel-e2|fix2-s2" >&2; exit 1 ;;
esac

echo "=== $(date -Is) apply $variant (boot $(cut -c1-8 /proc/sys/kernel/random/boot_id))" >> "$log"

tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9wifi' ||
	die 'not an SM-X710 (samsung,gts9wifi)'
(cd "$kit" && sha256sum --quiet -c SHA256SUMS) >> "$log" 2>&1 ||
	die "kit files do not match $kit/SHA256SUMS"

image=$images/$variant/vendor_boot.img
[ -f "$image" ] || die "missing $image (copy the images first, see README)"
want=$(sha256sum "$image" | cut -d' ' -f1)
if [ "$variant" = fix2-s2 ]; then
	expect=$fix2s2_vendor_boot
else
	expect=$(awk -v f="$variant/vendor_boot.img" '$2 == f { print $1 }' "$kit/IMAGES.sha256")
fi
[ -n "$expect" ] && [ "$want" = "$expect" ] ||
	die "$image is $want, expected ${expect:-<none listed>}"
[ "$(stat -c %s "$image")" -eq "$boot_size" ] || die "$image has the wrong size"

part_sha() { # partlabel: sha256 of the whole partition image size, from the disk
	dd if="/dev/disk/by-partlabel/$1" iflag=direct bs=4M count=24 status=none |
		head -c "$boot_size" | sha256sum | cut -d' ' -f1
}

boot_now=$(part_sha boot)
if [ "$boot_now" != "$fix2s2_boot" ] && [ "${FORCE_BOOT:-0}" != 1 ]; then
	die "boot partition holds $boot_now, not the fix2-s2 kernel; the switches are only known to exist there (FORCE_BOOT=1 to go on)"
fi
say "boot partition: fix2-s2 kernel ($(echo "$boot_now" | cut -c1-12))"

vb_now=$(part_sha vendor_boot)
say "vendor_boot now: $vb_now"
mkdir -p "$backup_dir"
chmod 0750 "$backup_dir"
if [ ! -f "$backup_dir/vendor_boot.first.img" ]; then
	dd if=/dev/disk/by-partlabel/vendor_boot iflag=direct bs=4M count=24 status=none |
		head -c "$boot_size" > "$backup_dir/vendor_boot.first.img"
	[ "$(sha256sum "$backup_dir/vendor_boot.first.img" | cut -d' ' -f1)" = "$vb_now" ] ||
		die 'backup of the current vendor_boot does not verify'
	echo "$vb_now  vendor_boot.first.img" > "$backup_dir/SHA256SUMS"
	say "backed up the current vendor_boot to $backup_dir/vendor_boot.first.img"
fi

if [ "$vb_now" = "$want" ]; then
	say "vendor_boot already holds $variant; nothing written"
else
	dd if="$image" of=/dev/disk/by-partlabel/vendor_boot bs=4M conv=fsync status=none
	blockdev --flushbufs /dev/disk/by-partlabel/vendor_boot 2>/dev/null || true
	got=$(part_sha vendor_boot)
	[ "$got" = "$want" ] || die "read-back of vendor_boot is $got, wrote $want; run rollback.sh now"
	sync
	say "OK vendor_boot <- $variant ($(echo "$want" | cut -c1-12))"
fi
sed -n 's/^added=/apply: command-line additions: /p' "$images/$variant/CMDLINE.txt" 2>/dev/null | tee -a "$log" || true
say "next: sudo reboot"
