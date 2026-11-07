#!/bin/sh
# Put back the vendor_boot the tablet had before the first apply.sh run (the
# copy in /var/backups/ubuntu-gts9u-panel-exp/), or, without that copy, fix2-s2's
# own vendor_boot from ~/display-exp/fix2-s2/.  Root; then reboot.  Nothing else
# needs undoing: the variants change the kernel command line only, and the
# unit masks they carry (systemd.mask=) live only as long as that boot.
#
#   sudo sh ~/panel/rollback.sh && sudo reboot
set -eu

user_home=$(getent passwd "${SUDO_USER:-jstockdale}" | cut -d: -f6)
images=${PANEL_EXP_IMAGES:-$user_home/display-exp}
backup_dir=/var/backups/ubuntu-gts9u-panel-exp
log=/var/log/gts9u-panel-exp.log
boot_size=100663296
fix2s2_vendor_boot=0742866cbfb01da797aed4129eacfb13655ca0476ec13546f210bf1c874f4fd5

say() { echo "rollback: $*" | tee -a "$log"; }
die() { say "FAILED: $*"; exit 1; }

[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
echo "=== $(date -Is) rollback (boot $(cut -c1-8 /proc/sys/kernel/random/boot_id))" >> "$log"

if [ -f "$backup_dir/vendor_boot.first.img" ]; then
	source=$backup_dir/vendor_boot.first.img
	expect=$(awk '$2 == "vendor_boot.first.img" { print $1 }' "$backup_dir/SHA256SUMS")
else
	source=$images/fix2-s2/vendor_boot.img
	expect=$fix2s2_vendor_boot
fi
[ -f "$source" ] || die "missing $source"
want=$(sha256sum "$source" | cut -d' ' -f1)
[ -n "$expect" ] && [ "$want" = "$expect" ] || die "$source is $want, expected $expect"
[ "$want" = "$fix2s2_vendor_boot" ] ||
	say "note: restoring $(echo "$want" | cut -c1-12), which is not fix2-s2's vendor_boot (it is what was there before the kit)"

dd if="$source" of=/dev/disk/by-partlabel/vendor_boot bs=4M conv=fsync status=none
blockdev --flushbufs /dev/disk/by-partlabel/vendor_boot 2>/dev/null || true
got=$(dd if=/dev/disk/by-partlabel/vendor_boot iflag=direct bs=4M count=24 status=none |
	head -c "$boot_size" | sha256sum | cut -d' ' -f1)
[ "$got" = "$want" ] || die "read-back is $got, wrote $want; try again, or restore from TWRP (README)"
sync
say "OK vendor_boot <- $source ($(echo "$want" | cut -c1-12)); next: sudo reboot"
