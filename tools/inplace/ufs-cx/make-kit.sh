#!/bin/bash
# Pack the CX-vote kit on the build host.  It goes on top of the UFS HS-G5
# kit: the G5 kernel plus the ufshcd and dwc3-qcom system-suspend patches,
# and the DTB whose &gcc names CX as the GCC GDSCs' parent, in both images
# that carry a DTB.
#
#   bash tools/inplace/ufs-cx/make-kit.sh KERNEL_OUT_DIR FIX7_VENDOR_BOOT KIT_DIR
#
# KERNEL_OUT_DIR is build-mainline-kernel.sh's KERNEL_OUT_DIR for a gts9wifi
# build of this branch.  FIX7_VENDOR_BOOT is fix7-ice's vendor_boot.img (the
# one on the tablet under the G5 kit).
#
# boot.img: the G5 kit's recipe (build-android-v4-bundle.sh's), the new
# kernel with the new DTB appended.
#
# vendor_boot.img: fix7-ice's image with its DTB swapped and nothing else.
# The repack is proven exact first: fix7-ice's own arguments and DTB,
# unpacked and packed again, must reproduce its body (everything before the
# AVB footer) byte for byte.  Its footer cannot be reproduced: its digest is
# stale, computed before " iommu.strict=0" was patched into the cmdline in
# place (zero those 15 bytes and the digest matches).  vbmeta is disabled, so
# ABL never checks it; the new image gets a fresh footer with the
# deterministic salt of build-android-v4-bundle.sh.  The result is read back:
# every argument but the DTB is identical, and the DTB is the new one.
#
# DEVICE_DEB, if set, is an ubuntu-gts9u-device 2.58 built for gts9wifi
# against this kernel; it ships in the kit for a separate, optional install.
# Its qcom_spss_irq.ko must be this build's.  Nothing is flashed.
set -euo pipefail

[ $# -eq 3 ] || { echo "usage: $0 KERNEL_OUT_DIR FIX7_VENDOR_BOOT KIT_DIR" >&2; exit 2; }
out=$1
base_vb=$(readlink -f "$2")
kit=$3
here=$(cd "$(dirname "$0")" && pwd)
workdir=${UBUNTU_WORKDIR:-$HOME/ubuntu-gts9u}
tools=${ANDROID_TOOLS:-$workdir/tools}
mkbootimg=$tools/mkbootimg.py
avbtool=$tools/avbtool.py
size=100663296

# fix7-ice: the DTB both of its images carry (the ICE clock tree), and its
# vendor_boot.  fix6's kernel configuration: the G5 kit kept it, and so does
# this kit (the patches change functions, not the configuration).
fix7_dtb=2da186303f7b0714f0de521dfbb2a0f27dadb413be10d804765b9bad7da8067c
fix7_vendor_boot=4b8b2f92e5d3f6e4631e684e853c16243404263f629a40b6c1f5313c37e6eb36
fix6_config=c17980f07ca04461e67dba7e1b3d58f9719d079f65bcc3d45b723fc2c27ccf5a
# fix7-ice's DTB plus `power-domains = <&rpmhpd RPMHPD_CX>` on gcc, nothing
# else (docs/porting-log.md, "The UFS clocks' CX vote never reached the rail").
cx_dtb=290a5395f64748ec237c93859b098251f31c3f5fee51ee1677caf2d111fedd8c

sha() { sha256sum "$1" | cut -d' ' -f1; }
release=$(cat "$out/kernel.release")
modules=$out/modules-root/usr/lib/modules/$release
dtb=$out/sm8550-samsung-gts9wifi.dtb

for f in "$mkbootimg" "$avbtool" "$base_vb" "$out/Image.gz" "$dtb" "$out/config"; do
	[ -f "$f" ] || { echo "missing: $f" >&2; exit 1; }
done
for t in unpack_bootimg python3 sha256sum cmp tar; do
	command -v "$t" >/dev/null || { echo "missing tool: $t" >&2; exit 1; }
done
[ "$(sha "$dtb")" = "$cx_dtb" ] || {
	echo "$dtb is not the CX-parent DTB ($cx_dtb)" >&2; exit 1; }
[ "$(sha "$out/config")" = "$fix6_config" ] || {
	echo "$out: the kernel configuration differs from fix6's" >&2; exit 1; }
[ "$(sha "$base_vb")" = "$fix7_vendor_boot" ] || {
	echo "$base_vb is not fix7-ice's vendor_boot" >&2; exit 1; }
[ -f "$modules/modules.dep" ] || { echo "no module tree in $out" >&2; exit 1; }
# install.sh swaps the module directory the G5 kit installed under this name.
[ "$release" = 7.2.0-rc3-dirty ] || { echo "kernel release $release, expected 7.2.0-rc3-dirty" >&2; exit 1; }
deb=${DEVICE_DEB:-}
if [ -n "$deb" ]; then
	[ "$(dpkg-deb -f "$deb" Package)" = ubuntu-gts9u-device ] &&
		[ "$(dpkg-deb -f "$deb" Version)" = 2.58 ] ||
		{ echo "$deb is not ubuntu-gts9u-device 2.58" >&2; exit 1; }
	irq=usr/lib/modules/$release/updates/qcom_spss_irq.ko
	cmp -s <(dpkg-deb --fsys-tarfile "$deb" | tar -xO "./$irq") "$out/modules-root/$irq" ||
		{ echo "$deb carries a different $irq than this kernel build" >&2; exit 1; }
fi
[ ! -e "$kit" ] || { echo "$kit exists; choose a new directory" >&2; exit 1; }

tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT

# --- vendor_boot: fix7-ice's with the DTB swapped --------------------------------
# unpack_bootimg prints the mkbootimg arguments that rebuild the image; with
# --null they come NUL-separated, so the cmdline survives intact.
unpack() { # IMAGE DIR
	mkdir -p "$2"
	unpack_bootimg --boot_img "$1" --out "$2" --format=mkbootimg --null > "$2/args"
}
unpack "$base_vb" "$tmp/base"
mapfile -d '' args < "$tmp/base/args"
dtb_index=
for i in "${!args[@]}"; do
	case "${args[$i]}" in
	--dtb) [ -z "$dtb_index" ] || { echo 'two --dtb arguments' >&2; exit 1; }
		dtb_index=$((i + 1)) ;;
	--header_version) [ "${args[$((i + 1))]}" = 4 ] || { echo 'not a v4 vendor_boot' >&2; exit 1; } ;;
	esac
done
[ -n "$dtb_index" ] || { echo 'fix7-ice vendor_boot carries no DTB' >&2; exit 1; }
[ "$(sha "${args[$dtb_index]}")" = "$fix7_dtb" ] || {
	echo "fix7-ice vendor_boot's DTB is not $fix7_dtb" >&2; exit 1; }

body_vb() { # DTB IMAGE: fix7-ice's arguments with the DTB swapped, no footer
	local a=("${args[@]}")
	a[$dtb_index]=$1
	python3 "$mkbootimg" "${a[@]}" --vendor_boot "$2"
}
body_vb "${args[$dtb_index]}" "$tmp/self.img"
base_body=$(python3 "$avbtool" info_image --image "$base_vb" |
	awk '/^Original image size:/ {print $4}')
[ "$(stat -c %s "$tmp/self.img")" = "$base_body" ] &&
	cmp -s "$tmp/self.img" <(head -c "$base_body" "$base_vb") || {
	echo 'self-test failed: repacking fix7-ice vendor_boot does not reproduce its body' >&2; exit 1; }
echo "self-test: the repack reproduces fix7-ice vendor_boot's $base_body-byte body exactly"

mkdir -p "$kit"
body_vb "$dtb" "$kit/vendor_boot.img"
# Deterministic salt, as add_hash_footer in build-android-v4-bundle.sh.
python3 "$avbtool" add_hash_footer --image "$kit/vendor_boot.img" \
	--partition_name vendor_boot --partition_size "$size" \
	--salt "$(sha "$kit/vendor_boot.img")"
unpack "$kit/vendor_boot.img" "$tmp/check"
mapfile -d '' check_args < "$tmp/check/args"
[ "${#check_args[@]}" = "${#args[@]}" ] || { echo 'argument count differs after the swap' >&2; exit 1; }
for i in "${!args[@]}"; do
	[ "$i" = "$dtb_index" ] && continue
	case "${args[$i]}" in
	"$tmp"/base/*) cmp -s "${args[$i]}" "${check_args[$i]}" ||
		{ echo "the new vendor_boot differs in ${args[$((i - 1))]}" >&2; exit 1; } ;;
	*) [ "${args[$i]}" = "${check_args[$i]}" ] ||
		{ echo "the new vendor_boot differs: ${args[$i]} vs ${check_args[$i]}" >&2; exit 1; } ;;
	esac
done
cmp -s "${check_args[$dtb_index]}" "$dtb" || { echo 'the DTB did not read back' >&2; exit 1; }
[ "$(stat -c %s "$kit/vendor_boot.img")" = "$size" ] || { echo 'wrong vendor_boot size' >&2; exit 1; }
echo "vendor_boot: only the DTB differs from fix7-ice's (now ${cx_dtb:0:12})"

# --- boot: the G5 kit's recipe -------------------------------------------------------
cat "$out/Image.gz" "$dtb" > "$tmp/Image.gz-dtb"
python3 "$mkbootimg" \
	--kernel "$tmp/Image.gz-dtb" \
	--cmdline '' \
	--header_version 4 \
	--os_version 13 \
	--os_patch_level 2025-07 \
	-o "$kit/boot.img"
python3 "$avbtool" add_hash_footer \
	--image "$kit/boot.img" \
	--partition_name boot \
	--partition_size "$size" \
	--salt "$(sha "$kit/boot.img")"

tar --owner=0 --group=0 -C "$out/modules-root" \
	-cf "$kit/modules-$release.tar" "usr/lib/modules/$release"
install -m 0644 "$here/install.sh" "$here/rollback.sh" "$here/verify.sh" \
	"$here/suspend-votes.sh" "$here/README.md" "$kit/"
files=(boot.img vendor_boot.img "modules-$release.tar" install.sh rollback.sh
	verify.sh suspend-votes.sh README.md)
if [ -n "$deb" ]; then
	install -m 0644 "$deb" "$kit/"
	files+=("$(basename "$deb")")
fi
(cd "$kit" && sha256sum "${files[@]}" > SHA256SUMS)
cat "$kit/SHA256SUMS"
