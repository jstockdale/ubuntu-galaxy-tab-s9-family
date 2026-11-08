#!/bin/bash
# Pack the lens-bus kit on the build host.  It goes on top of the CX-vote kit
# and changes only the DTB: the rear module's rail (rear_cam_vio, GPIO15)
# waits stock's 10 ms after power-up before the DW9808 lens is written to.
# The kernel and module tree stay the CX kit's.
#
#   bash tools/inplace/cam-lens/make-kit.sh CX_KERNEL_OUT_DIR NEW_DTB CX_KIT_DIR KIT_DIR
#
# boot.img: the CX kernel with NEW_DTB appended, by the CX kit's recipe; with
# the CX DTB that recipe reproduces the CX kit's boot.img byte for byte, which
# is checked first.  vendor_boot.img: the CX kit's image with only its DTB
# swapped; a repack with the CX DTB must reproduce that image byte for byte
# (its footer is fresh), and the result is read back.  Nothing is flashed.
set -euo pipefail

[ $# -eq 4 ] || { echo "usage: $0 CX_KERNEL_OUT_DIR NEW_DTB CX_KIT_DIR KIT_DIR" >&2; exit 2; }
out=$1
dtb=$(readlink -f "$2")
cxkit=$3
kit=$4
here=$(cd "$(dirname "$0")" && pwd)
workdir=${UBUNTU_WORKDIR:-$HOME/ubuntu-gts9u}
tools=${ANDROID_TOOLS:-$workdir/tools}
mkbootimg=$tools/mkbootimg.py
avbtool=$tools/avbtool.py
size=100663296

cx_dtb=290a5395f64748ec237c93859b098251f31c3f5fee51ee1677caf2d111fedd8c
cx_boot=47f7df27477418857bfd087bb44245510c8be5c563d3dcbe2a344778f023df98
cx_vendor_boot=be06a00f699c104cf5974d2bf85cc6e974d0e3b38b72b42329aa7340fd5272cf
# The CX DTB with startup-delay-us = <10000> on rear_cam_vio, nothing else.
lens_dtb=164e1bf8a393c642ffc7bd357ce5dfbc608b07ba77070ede8b2052434de42a84

sha() { sha256sum "$1" | cut -d' ' -f1; }
for f in "$mkbootimg" "$avbtool" "$out/Image.gz" "$out/sm8550-samsung-gts9wifi.dtb" \
	"$dtb" "$cxkit/boot.img" "$cxkit/vendor_boot.img"; do
	[ -f "$f" ] || { echo "missing: $f" >&2; exit 1; }
done
[ "$(sha "$out/sm8550-samsung-gts9wifi.dtb")" = "$cx_dtb" ] || { echo "$out is not the CX kernel build" >&2; exit 1; }
[ "$(sha "$dtb")" = "$lens_dtb" ] || { echo "$dtb is not the lens-bus DTB ($lens_dtb)" >&2; exit 1; }
[ "$(sha "$cxkit/boot.img")" = "$cx_boot" ] || { echo "$cxkit/boot.img is not the CX kit's" >&2; exit 1; }
[ "$(sha "$cxkit/vendor_boot.img")" = "$cx_vendor_boot" ] || { echo "$cxkit/vendor_boot.img is not the CX kit's" >&2; exit 1; }
[ ! -e "$kit" ] || { echo "$kit exists; choose a new directory" >&2; exit 1; }

tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT

pack_boot() { # DTB IMAGE: the CX kit's recipe
	cat "$out/Image.gz" "$1" > "$tmp/Image.gz-dtb"
	python3 "$mkbootimg" --kernel "$tmp/Image.gz-dtb" --cmdline '' \
		--header_version 4 --os_version 13 --os_patch_level 2025-07 -o "$2"
	python3 "$avbtool" add_hash_footer --image "$2" --partition_name boot \
		--partition_size "$size" --salt "$(sha "$2")"
}
pack_boot "$out/sm8550-samsung-gts9wifi.dtb" "$tmp/self-boot.img"
[ "$(sha "$tmp/self-boot.img")" = "$cx_boot" ] || { echo 'self-test failed: the boot recipe does not reproduce the CX kit' >&2; exit 1; }
echo "self-test: the boot recipe reproduces the CX kit's boot.img (${cx_boot:0:12})"

unpack() { mkdir -p "$2"; unpack_bootimg --boot_img "$1" --out "$2" --format=mkbootimg --null > "$2/args"; }
unpack "$cxkit/vendor_boot.img" "$tmp/base"
mapfile -d '' args < "$tmp/base/args"
dtb_index=
for i in "${!args[@]}"; do
	[ "${args[$i]}" = --dtb ] && dtb_index=$((i + 1))
done
[ -n "$dtb_index" ] || { echo 'the CX vendor_boot carries no DTB' >&2; exit 1; }
[ "$(sha "${args[$dtb_index]}")" = "$cx_dtb" ] || { echo "the CX vendor_boot's DTB is not $cx_dtb" >&2; exit 1; }
pack_vb() { # DTB IMAGE
	local a=("${args[@]}")
	a[$dtb_index]=$1
	python3 "$mkbootimg" "${a[@]}" --vendor_boot "$2"
	python3 "$avbtool" add_hash_footer --image "$2" --partition_name vendor_boot \
		--partition_size "$size" --salt "$(sha "$2")"
}
pack_vb "${args[$dtb_index]}" "$tmp/self-vb.img" >/dev/null
[ "$(sha "$tmp/self-vb.img")" = "$cx_vendor_boot" ] || { echo 'self-test failed: the vendor_boot repack does not reproduce the CX kit' >&2; exit 1; }
echo "self-test: the vendor_boot repack reproduces the CX kit's (${cx_vendor_boot:0:12})"

mkdir -p "$kit"
pack_boot "$dtb" "$kit/boot.img"
pack_vb "$dtb" "$kit/vendor_boot.img" >/dev/null
unpack "$kit/vendor_boot.img" "$tmp/check"
mapfile -d '' check_args < "$tmp/check/args"
[ "${#check_args[@]}" = "${#args[@]}" ] || { echo 'argument count differs after the swap' >&2; exit 1; }
for i in "${!args[@]}"; do
	[ "$i" = "$dtb_index" ] && continue
	case "${args[$i]}" in
	"$tmp"/base/*) cmp -s "${args[$i]}" "${check_args[$i]}" ||
		{ echo "the new vendor_boot differs in ${args[$((i - 1))]}" >&2; exit 1; } ;;
	*) [ "${args[$i]}" = "${check_args[$i]}" ] || { echo "the new vendor_boot differs: ${args[$i]}" >&2; exit 1; } ;;
	esac
done
cmp -s "${check_args[$dtb_index]}" "$dtb" || { echo 'the DTB did not read back' >&2; exit 1; }
echo "vendor_boot: only the DTB differs from the CX kit's (now ${lens_dtb:0:12})"

install -m 0644 "$here/install.sh" "$here/rollback.sh" "$here/README.md" "$kit/"
(cd "$kit" && sha256sum boot.img vendor_boot.img install.sh rollback.sh README.md > SHA256SUMS)
cat "$kit/SHA256SUMS"
