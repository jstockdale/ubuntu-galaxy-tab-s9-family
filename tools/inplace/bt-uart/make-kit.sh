#!/bin/bash
# Pack the BT UART kit on the build host.  It goes on top of the CX-vote kit:
# the CX kernel plus qcom-geni-serial-serve-irq-while-suspending.patch, with
# the CX DTB appended, in boot only.  vendor_boot keeps the CX kit's image.
#
#   bash tools/inplace/bt-uart/make-kit.sh KERNEL_OUT_DIR CX_KERNEL_OUT_DIR KIT_DIR
#
# KERNEL_OUT_DIR is build-mainline-kernel.sh's KERNEL_OUT_DIR for a gts9wifi
# build of this branch, CX_KERNEL_OUT_DIR the CX kit's
# (~/ubuntu-gts9u/out/kernel-gts9wifi-cx).
#
# boot.img: the CX kit's recipe.  It is proven first: given the CX kernel and
# DTB it must reproduce the CX kit's boot.img byte for byte.  The new image is
# then read back: every header field is the CX kit's, and the kernel is the
# new Image.gz with the CX DTB appended.
#
# The build reuses the CX build's module signing key, so the device package
# 2.58's qcom_spss_irq.ko (signed for the CX kernel) still loads.  Every module
# must carry that signer, and the module list must be the CX build's.
# Nothing is flashed.
set -euo pipefail

[ $# -eq 3 ] || { echo "usage: $0 KERNEL_OUT_DIR CX_KERNEL_OUT_DIR KIT_DIR" >&2; exit 2; }
out=$1
cx=$2
kit=$3
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../.." && pwd)
workdir=${UBUNTU_WORKDIR:-$HOME/ubuntu-gts9u}
tools=${ANDROID_TOOLS:-$workdir/tools}
mkbootimg=$tools/mkbootimg.py
avbtool=$tools/avbtool.py
size=100663296

fix6_config=c17980f07ca04461e67dba7e1b3d58f9719d079f65bcc3d45b723fc2c27ccf5a
cx_dtb=290a5395f64748ec237c93859b098251f31c3f5fee51ee1677caf2d111fedd8c
cx_image=8b6d58c969b91e7518423d5845f60f39b286aeb4e2539aaf662ef2f50b94ee37
cx_boot=47f7df27477418857bfd087bb44245510c8be5c563d3dcbe2a344778f023df98
# The CX kernel's GNU build ID, as the tablet reports it from /sys/kernel/notes.
cx_build_id=bfb40d206abbdfcf7f4f3298fd84c96c4c2ea1c6
# The CX build's module signing certificate (key 1ded767e...).
cx_signer=1E:12:4E:A5:EE:B7:4F:2F:0B:75:5D:05:6A:57:9B:2E:DC:9D:93:97

sha() { sha256sum "$1" | cut -d' ' -f1; }
release=$(cat "$out/kernel.release")
modules=$out/modules-root/usr/lib/modules/$release
cx_modules=$cx/modules-root/usr/lib/modules/$release
dtb=$out/sm8550-samsung-gts9wifi.dtb
cycles=$repo/scripts/diagnostics/suspend/gts9u-suspend-cycles.sh

for f in "$mkbootimg" "$avbtool" "$out/Image.gz" "$dtb" "$out/config" \
	"$cx/Image.gz" "$cx/sm8550-samsung-gts9wifi.dtb" "$cycles"; do
	[ -f "$f" ] || { echo "missing: $f" >&2; exit 1; }
done
for t in unpack_bootimg python3 sha256sum cmp tar modinfo xxd; do
	command -v "$t" >/dev/null || { echo "missing tool: $t" >&2; exit 1; }
done
[ "$(sha "$dtb")" = "$cx_dtb" ] || { echo "$dtb is not the CX DTB ($cx_dtb)" >&2; exit 1; }
[ "$(sha "$cx/sm8550-samsung-gts9wifi.dtb")" = "$cx_dtb" ] &&
	[ "$(sha "$cx/Image.gz")" = "$cx_image" ] ||
	{ echo "$cx is not the CX kernel build" >&2; exit 1; }
[ "$(sha "$out/config")" = "$fix6_config" ] || {
	echo "$out: the kernel configuration differs from fix6's" >&2; exit 1; }
[ "$(sha "$out/Image.gz")" != "$cx_image" ] || {
	echo "$out holds the CX kernel itself, not a build with the patch" >&2; exit 1; }
# install.sh swaps the module directory the CX kit installed under this name.
[ "$release" = 7.2.0-rc3-dirty ] || { echo "kernel release $release, expected 7.2.0-rc3-dirty" >&2; exit 1; }
[ -f "$modules/modules.dep" ] && [ -f "$cx_modules/modules.dep" ] ||
	{ echo "no module tree in $out or $cx" >&2; exit 1; }
[ ! -e "$kit" ] || { echo "$kit exists; choose a new directory" >&2; exit 1; }

# The kernel's GNU build ID note (namesz 4, descsz 20, type 3, "GNU\0").  The
# image carries three: the two vDSOs' come first, the kernel's own last.  The
# self-test below checks that against the ID the tablet reports.
build_id() {
	zcat "$1" | xxd -p | tr -d '\n' |
		grep -o '040000001400000003000000474e5500[0-9a-f]\{40\}' | tail -1 | cut -c33-
}
[ "$(build_id "$cx/Image.gz")" = "$cx_build_id" ] || {
	echo 'self-test failed: the build ID read from the CX image is not the one the tablet reported' >&2; exit 1; }
new_id=$(build_id "$out/Image.gz")
[ ${#new_id} = 40 ] || { echo 'no build ID in the new image' >&2; exit 1; }
echo "build IDs: CX ${cx_build_id:0:12}, new ${new_id:0:12}"

# Modules: the CX build's list, every one signed with the CX key.
list() { (cd "$1" && find . -name '*.ko' -o -name '*.ko.zst' | sort); }
cmp -s <(list "$modules") <(list "$cx_modules") || {
	echo 'the module list differs from the CX build:' >&2
	diff <(list "$cx_modules") <(list "$modules") >&2 || :
	exit 1
}
n=0 same=0
while IFS= read -r ko; do
	[ "$(modinfo -F sig_key "$modules/$ko")" = "$cx_signer" ] ||
		{ echo "$ko is not signed with the CX key" >&2; exit 1; }
	n=$((n + 1))
	cmp -s "$modules/$ko" "$cx_modules/$ko" && same=$((same + 1))
done < <(list "$modules")
echo "modules: $n, all signed with the CX key; $same byte-identical to the CX build's"

tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT

pack_boot() { # IMAGE_GZ DTB OUT
	cat "$1" "$2" > "$tmp/Image.gz-dtb"
	python3 "$mkbootimg" \
		--kernel "$tmp/Image.gz-dtb" \
		--cmdline '' \
		--header_version 4 \
		--os_version 13 \
		--os_patch_level 2025-07 \
		-o "$3"
	python3 "$avbtool" add_hash_footer \
		--image "$3" \
		--partition_name boot \
		--partition_size "$size" \
		--salt "$(sha "$3")"
}
pack_boot "$cx/Image.gz" "$cx/sm8550-samsung-gts9wifi.dtb" "$tmp/cx-boot.img"
[ "$(sha "$tmp/cx-boot.img")" = "$cx_boot" ] || {
	echo 'self-test failed: the recipe does not reproduce the CX kit boot.img' >&2; exit 1; }
echo "self-test: the recipe reproduces the CX kit's boot.img (${cx_boot:0:12}) byte for byte"

mkdir -p "$kit"
pack_boot "$out/Image.gz" "$dtb" "$kit/boot.img"
[ "$(stat -c %s "$kit/boot.img")" = "$size" ] || { echo 'wrong boot size' >&2; exit 1; }
unpack() { # IMAGE DIR
	mkdir -p "$2"
	unpack_bootimg --boot_img "$1" --out "$2" --format=mkbootimg --null > "$2/args"
}
unpack "$tmp/cx-boot.img" "$tmp/a"
unpack "$kit/boot.img" "$tmp/b"
mapfile -d '' a < "$tmp/a/args"
mapfile -d '' b < "$tmp/b/args"
[ "${#a[@]}" = "${#b[@]}" ] || { echo 'boot header argument count differs from the CX kit' >&2; exit 1; }
for i in "${!a[@]}"; do
	case "${a[$i]}" in
	"$tmp"/a/kernel) cmp -s "${b[$i]}" <(cat "$out/Image.gz" "$dtb") ||
		{ echo 'the boot kernel is not Image.gz plus the CX DTB' >&2; exit 1; } ;;
	"$tmp"/a/*) cmp -s "${a[$i]}" "${b[$i]}" ||
		{ echo "boot differs from the CX kit in ${a[$((i - 1))]}" >&2; exit 1; } ;;
	*) [ "${a[$i]}" = "${b[$i]}" ] ||
		{ echo "boot differs from the CX kit: ${a[$i]} vs ${b[$i]}" >&2; exit 1; } ;;
	esac
done
echo 'boot: every header field is the CX kit'"'"'s; the kernel is the new Image.gz plus the CX DTB'

tar --owner=0 --group=0 -C "$out/modules-root" \
	-cf "$kit/modules-$release.tar" "usr/lib/modules/$release"
printf '%s\n' "$new_id" > "$kit/BUILD_ID"
install -m 0644 "$here/install.sh" "$here/rollback.sh" "$here/verify.sh" \
	"$here/suspend-bt.sh" "$here/README.md" "$cycles" "$kit/"
files=(boot.img "modules-$release.tar" BUILD_ID install.sh rollback.sh verify.sh
	suspend-bt.sh gts9u-suspend-cycles.sh README.md)
(cd "$kit" && sha256sum "${files[@]}" > SHA256SUMS)
cat "$kit/SHA256SUMS"
