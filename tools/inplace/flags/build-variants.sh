#!/bin/bash
# Host side.  Rebuild the bring-up flag experiment images for the SM-X710 on
# top of fix2-s2: same kernel, same DTB, same vendor ramdisk, same cmdline
# extras, and exactly one of clk_ignore_unused, pd_ignore_unused and
# regulator_ignore_unused left out (plus an optional image without all three).
#
# The inputs are taken out of the fix2-s2 images themselves, not from out/,
# so a kernel or overlay rebuilt since then cannot leak in.  A control build
# with the unchanged cmdline has to reproduce fix2-s2's boot.img and
# vendor_boot.img byte for byte before any variant is written; each variant is
# then unpacked again and must differ from fix2-s2 in the cmdline only.
#
#   tools/inplace/flags/build-variants.sh <output dir>
#
# Writes <output dir>/flags-*/{boot.img,vendor_boot.img,CMDLINE.txt,SHA256SUMS}
# and <output dir>/IMAGES-flags.sha256 (paths relative to ~/display-exp on
# the tablet).  Never flashes anything; needs no root.
set -euo pipefail

repo=$(cd "$(dirname "$0")/../../.." && pwd)
fix2=${FIX2_DIR:-/home/jstockdale/ubuntu-gts9u/backups/x710-fix-integration-2026-11-06/fix2-images/fix2-s2}
initramfs=${INITRAMFS:-/home/jstockdale/ubuntu-gts9u/rootfs/boot/initrd.img-7.2.0-rc3-dirty}
tools=${ANDROID_TOOLS:-/home/jstockdale/ubuntu-gts9u/tools}
mainline=${LINUX_MAINLINE:-/home/jstockdale/ubuntu-gts9u/linux-mainline}
kernel_meta=${KERNEL_META_DIR:-/home/jstockdale/ubuntu-gts9u/out/kernel-gts9wifi}
out=${1:?usage: build-variants.sh <output dir>}

# fix2-s2 as flashed on 2026-11-07 (boot dbe15039 and later).
want_boot=dcf881554a2790b02ce230790cd868ee06f8c9e1ddcb8e7dc48e7c0cc20f09b0
want_vendor_boot=0742866cbfb01da797aed4129eacfb13655ca0476ec13546f210bf1c874f4fd5
want_initramfs=e09d0d740eb38486da11a91662b2d2ea5dae8b544b8a1d8c91d24db220f372db
want_config=f501978daf5a945d7987167124b896f9e1d43d154cfaff866ca2a6537e56fb05
extra='initcall_blacklist=bwmon_driver_init icc_core.dyndbg=+p'
# clk variant only: record every clk_disable/clk_unprepare in the trace ring
# buffer, so the morning check can list exactly what clk_disable_unused()
# switched off at late_initcall_sync (13.7 s), including clocks a driver
# turned back on later.  8 MiB per CPU holds hours of runtime gating.
clk_trace='trace_event=clk:clk_disable,clk:clk_unprepare trace_buf_size=8M'

sha() { sha256sum "$1" | cut -d' ' -f1; }
die() { echo "build-variants: $*" >&2; exit 1; }

for f in "$fix2/boot.img" "$fix2/vendor_boot.img" "$initramfs" \
	"$tools/mkbootimg.py" "$tools/avbtool.py" "$kernel_meta/config" "$kernel_meta/kernel.release"; do
	[ -f "$f" ] || die "missing input: $f"
done
command -v unpack_bootimg >/dev/null || die 'unpack_bootimg not installed (package mkbootimg)'
[ "$(sha "$fix2/boot.img")" = "$want_boot" ] || die "$fix2/boot.img is not fix2-s2"
[ "$(sha "$fix2/vendor_boot.img")" = "$want_vendor_boot" ] || die "$fix2/vendor_boot.img is not fix2-s2"
[ "$(sha "$initramfs")" = "$want_initramfs" ] || die "$initramfs is not the fix2-s2 initramfs"
[ "$(sha "$kernel_meta/config")" = "$want_config" ] || die "$kernel_meta/config is not the fix2-s2 kernel config"

mkdir -p "$out"
work=$(mktemp -d "$out/.work.XXXXXX")
trap 'rm -rf -- "$work"' EXIT

# 1. Inputs out of the fix2-s2 images.
unpack_bootimg --boot_img "$fix2/vendor_boot.img" --out "$work/vb" >/dev/null
unpack_bootimg --boot_img "$fix2/boot.img" --out "$work/boot" >/dev/null
dtb=$work/vb/dtb
ksize=$(stat -c %s "$work/boot/kernel")
dsize=$(stat -c %s "$dtb")
mkdir -p "$work/kernel-out"
head -c $((ksize - dsize)) "$work/boot/kernel" > "$work/kernel-out/Image.gz"
cmp -s <(tail -c "$dsize" "$work/boot/kernel") "$dtb" ||
	die 'boot.img kernel does not end with the vendor_boot DTB'
cp "$dtb" "$work/kernel-out/sm8550-samsung-gts9wifi.dtb"
cp "$kernel_meta/config" "$kernel_meta/kernel.release" "$work/kernel-out/"
mkdir "$work/ramdisk"
lz4 -dc "$work/vb/vendor_ramdisk00" | (cd "$work/ramdisk" && cpio -idm --quiet --no-absolute-filenames)
chmod 0755 "$work/ramdisk"
fix2_cmdline=$(python3 -I - "$fix2/vendor_boot.img" <<'EOF'
import sys
with open(sys.argv[1], 'rb') as f:
    h = f.read(4096)
assert h[:8] == b'VNDRBOOT', 'not a vendor_boot image'
# vendor boot header v3/v4: magic, version, page_size, kernel_addr,
# ramdisk_addr, vendor_ramdisk_size, then the 2048-byte cmdline.
print(h[28:28 + 2048].split(b'\0', 1)[0].decode())
EOF
)
mkdir -p "$work/base"
ln -s "$mainline" "$work/base/linux-mainline"

# 2. One bundle build per cmdline.
build() { # name drop-tokens extra-tokens
	local name=$1 drop=$2 more=$3 dir=$work/out-$1
	DEVICE=gts9wifi UBUNTU_WORKDIR="$work/base" ANDROID_TOOLS="$tools" \
	KERNEL_OUT_DIR="$work/kernel-out" KERNEL_DTB="$dtb" BUNDLE_OUT_DIR="$dir" \
	INITRAMFS="$initramfs" INITRAMFS_OVERLAY_DIR="$work/ramdisk" \
	GTS9U_DROP_CMDLINE="$drop" GTS9U_EXTRA_CMDLINE="$extra${more:+ $more}" \
		bash "$repo/scripts/build-android-v4-bundle.sh" < /dev/null > "$work/build-$name.log" 2>&1 ||
		{ cat "$work/build-$name.log" >&2; die "bundle build $name failed"; }
}
cmdline_of() {
	python3 -I - "$1" <<'EOF'
import sys
with open(sys.argv[1], 'rb') as f:
    h = f.read(4096)
print(h[28:28 + 2048].split(b'\0', 1)[0].decode())
EOF
}

build control '' ''
[ "$(sha "$work/out-control/boot.img")" = "$want_boot" ] ||
	die 'control boot.img does not reproduce fix2-s2'
[ "$(sha "$work/out-control/vendor_boot.img")" = "$want_vendor_boot" ] ||
	die 'control vendor_boot.img does not reproduce fix2-s2'
echo "control reproduces fix2-s2 (boot ${want_boot:0:12}, vendor_boot ${want_vendor_boot:0:12})"

# name | tokens dropped | tokens added on top of fix2-s2's extras
variants='flags-nopd|pd_ignore_unused|
flags-noreg|regulator_ignore_unused|
flags-noclk|clk_ignore_unused|'"$clk_trace"'
flags-none|clk_ignore_unused pd_ignore_unused regulator_ignore_unused|'"$clk_trace"

: > "$work/IMAGES-flags.sha256"
first_boot=
for f in boot.img vendor_boot.img; do
	echo "$(sha "$fix2/$f")  fix2-s2/$f" >> "$work/IMAGES-flags.sha256"
done
while IFS='|' read -r name drop more; do
	build "$name" "$drop" "$more"
	dir=$work/out-$name
	# Everything but the cmdline must be fix2-s2's.
	[ "$(sha "$dir/boot.img")" = "$want_boot" ] || die "$name: boot.img is not fix2-s2's"
	unpack_bootimg --boot_img "$dir/vendor_boot.img" --out "$work/check-$name" >/dev/null
	for part in dtb bootconfig vendor_ramdisk00; do
		cmp -s "$work/check-$name/$part" "$work/vb/$part" || die "$name: $part differs from fix2-s2"
	done
	got=$(cmdline_of "$dir/vendor_boot.img")
	want=" $fix2_cmdline "
	for t in $drop; do want=${want/" $t "/ }; done
	want=${want# }; want=${want% }
	[ -n "$more" ] && want="$want $more"
	[ "$got" = "$want" ] || die "$name: cmdline '$got' is not '$want'"

	rm -rf "${out:?}/$name"
	mkdir -p "$out/$name"
	# boot.img is fix2-s2's in every variant: one copy, hard links for the rest.
	if [ -n "$first_boot" ]; then
		ln "$first_boot" "$out/$name/boot.img"
	else
		cp --sparse=always "$dir/boot.img" "$out/$name/boot.img"
		first_boot=$out/$name/boot.img
	fi
	cp --sparse=always "$dir/vendor_boot.img" "$out/$name/"
	{
		echo "variant=$name"
		echo "base=fix2-s2 (boot ${want_boot}, vendor_boot ${want_vendor_boot})"
		echo "dropped=$drop"
		echo "added=${more:-none}"
		echo "vendor_cmdline=$got"
	} > "$out/$name/CMDLINE.txt"
	cp "$dir/BUILD-METADATA.txt" "$out/$name/BUILD-METADATA.txt"
	(cd "$out/$name" && sha256sum boot.img vendor_boot.img > SHA256SUMS)
	for f in boot.img vendor_boot.img; do
		echo "$(sha "$out/$name/$f")  $name/$f" >> "$work/IMAGES-flags.sha256"
	done
	echo "$name: vendor_boot $(sha "$out/$name/vendor_boot.img" | cut -c1-12), dropped [$drop]${more:+, added [$more]}"
done <<< "$variants"
cp "$work/IMAGES-flags.sha256" "$out/IMAGES-flags.sha256"
echo "wrote $out/IMAGES-flags.sha256"
