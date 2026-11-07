#!/bin/sh
# Flash one image set of the bring-up flag experiment through the existing
# ~/display-exp/apply.sh, after checking it, and roll back the same way.
#
#   sudo sh ~/flags-kit/flash.sh flags-nopd|flags-noreg|flags-noclk|flags-none
#   sudo sh ~/flags-kit/flash.sh fix2-s2          (rollback)
#
# Before anything is written:
#   - every image of the set must match IMAGES-flags.sha256 (shipped with the kit);
#   - boot.img must be fix2-s2's kernel (it is in every set);
#   - the flags inside vendor_boot's cmdline are printed;
#   - the boot and vendor_boot partitions are read back: if they hold
#     anything other than fix2-s2 or one of these sets, they are saved to
#     /var/backups/gts9u-flags/ first, so nothing unknown is overwritten.
# Then apply.sh flashes boot + vendor_boot with read-back verification.
# Logs to /var/log/gts9u-flags/flash.log.  Does not reboot.
set -eu
export LC_ALL=C
[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
kit=$(cd "$(dirname "$0")" && pwd)
exp=/home/jstockdale/display-exp
sums=$kit/IMAGES-flags.sha256
fix2_boot=dcf881554a2790b02ce230790cd868ee06f8c9e1ddcb8e7dc48e7c0cc20f09b0
size=100663296
v=${1:-}
case "$v" in
flags-nopd|flags-noreg|flags-noclk|flags-none|fix2-s2) ;;
*) echo "usage: sudo sh $0 flags-nopd|flags-noreg|flags-noclk|flags-none|fix2-s2" >&2; exit 1 ;;
esac
mkdir -p /var/log/gts9u-flags
chgrp adm /var/log/gts9u-flags
chmod 2750 /var/log/gts9u-flags
log=/var/log/gts9u-flags/flash.log
say() { echo "$*"; echo "$(date '+%F %T') $*" >> "$log"; }
die() { say "REFUSED: $*"; exit 1; }

[ -f "$exp/apply.sh" ] || die "no $exp/apply.sh"
[ -f "$sums" ] || die "no $sums"
for f in boot.img vendor_boot.img; do
	want=$(awk -v p="$v/$f" '$2 == p {print $1}' "$sums")
	[ -n "$want" ] || die "$v/$f is not listed in $sums"
	got=$(sha256sum "$exp/$v/$f" 2>/dev/null | cut -d' ' -f1)
	[ "$got" = "$want" ] || die "$exp/$v/$f: sha256 ${got:-missing}, expected $want"
done
[ "$(awk -v p="$v/boot.img" '$2 == p {print $1}' "$sums")" = "$fix2_boot" ] ||
	die "$v/boot.img is not the fix2-s2 kernel"

# vendor boot header v4: the vendor cmdline is 2048 bytes at offset 28.
cl=$(dd if="$exp/$v/vendor_boot.img" bs=1 skip=28 count=2048 status=none | tr -d '\000')
flags=
for t in clk_ignore_unused pd_ignore_unused regulator_ignore_unused; do
	case " $cl " in *" $t "*) flags="$flags $t" ;; esac
done
say "$v: flags in the image:${flags:- none}; other tokens: $(echo "$cl" | tr ' ' '\n' | grep -E '^(initcall_blacklist|icc_core\.dyndbg|trace_event|trace_buf_size)=' | tr '\n' ' ')"

known=$(awk '{print $1}' "$sums" | sort -u)
for p in boot vendor_boot; do
	dev=/dev/disk/by-partlabel/$p
	[ -b "$dev" ] || die "no $dev"
	cur=$(dd if="$dev" iflag=direct bs=4M count=$((size / 4194304)) status=none | sha256sum | cut -d' ' -f1)
	short=${cur%"${cur#????????????}"}
	if printf '%s\n' "$known" | grep -qxF "$cur"; then
		say "$p partition now: $(awk -v h="$cur" '$1 == h {print $2}' "$sums" | head -1) ($short)"
	else
		mkdir -p /var/backups/gts9u-flags
		chmod 0700 /var/backups/gts9u-flags
		b=/var/backups/gts9u-flags/$p-$short.img
		[ -f "$b" ] || dd if="$dev" of="$b" iflag=direct bs=4M count=$((size / 4194304)) status=none
		[ "$(sha256sum "$b" | cut -d' ' -f1)" = "$cur" ] || die "backup $b does not match the $p partition"
		say "$p partition held an image outside this experiment ($short): saved to $b (restore: sudo dd if=$b of=$dev bs=4M conv=fsync)"
	fi
done

say "flashing $v with $exp/apply.sh"
tmp=$(mktemp)
rc=0
sh "$exp/apply.sh" "$v" > "$tmp" 2>&1 || rc=$?
tee -a "$log" < "$tmp"
rm -f "$tmp"
[ "$rc" = 0 ] || die "apply.sh failed (exit $rc): do not reboot; rerun this script or flash fix2-s2"
say "done: $v; now: sudo reboot"
