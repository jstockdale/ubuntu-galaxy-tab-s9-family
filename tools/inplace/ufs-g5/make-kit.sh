#!/bin/bash
# Pack the UFS HS-G5 kit on the build host: fix7-ice's boot image with the
# G5 kernel in place of fix6's, and the module tree built with that kernel.
#
#   bash tools/inplace/ufs-g5/make-kit.sh KERNEL_OUT_DIR KIT_DIR
#
# KERNEL_OUT_DIR is build-mainline-kernel.sh's KERNEL_OUT_DIR for a gts9wifi
# build with ufs-qcom-hs-g5-at-300mhz-sm8550.patch.  Its DTB must be the one
# fix7-ice appends (the ICE clock tree), so vendor_boot stays fix7-ice's.
# The packing is build-android-v4-bundle.sh's; it reproduces fix7-ice's
# boot.img byte for byte from the fix6 kernel output.  Nothing is flashed.
set -euo pipefail

out=${1:?KERNEL_OUT_DIR}
kit=${2:?KIT_DIR}
here=$(cd "$(dirname "$0")" && pwd)
base=${UBUNTU_WORKDIR:-$HOME/ubuntu-gts9u}
tools=${ANDROID_TOOLS:-$base/tools}
# fix7-ice: the DTB both of its images carry.
fix7_dtb=2da186303f7b0714f0de521dfbb2a0f27dadb413be10d804765b9bad7da8067c
release=$(cat "$out/kernel.release")
modules=$out/modules-root/usr/lib/modules/$release

# fix6's kernel configuration: the experiment changes one function, not the
# configuration.
fix6_config=c17980f07ca04461e67dba7e1b3d58f9719d079f65bcc3d45b723fc2c27ccf5a

[ "$(sha256sum "$out/sm8550-samsung-gts9wifi.dtb" | cut -d' ' -f1)" = "$fix7_dtb" ] || {
	echo "$out: the DTB is not fix7-ice's; vendor_boot would need rebuilding" >&2
	exit 1
}
[ "$(sha256sum "$out/config" | cut -d' ' -f1)" = "$fix6_config" ] || {
	echo "$out: the kernel configuration differs from fix6's" >&2
	exit 1
}
[ -f "$modules/modules.dep" ] || { echo "no module tree in $out" >&2; exit 1; }
[ ! -e "$kit" ] || { echo "$kit exists; choose a new directory" >&2; exit 1; }

mkdir -p "$kit"
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
cat "$out/Image.gz" "$out/sm8550-samsung-gts9wifi.dtb" > "$tmp/Image.gz-dtb"
python3 "$tools/mkbootimg.py" \
	--kernel "$tmp/Image.gz-dtb" \
	--cmdline '' \
	--header_version 4 \
	--os_version 13 \
	--os_patch_level 2025-07 \
	-o "$kit/boot.img"
salt=$(sha256sum "$kit/boot.img" | cut -d' ' -f1)
python3 "$tools/avbtool.py" add_hash_footer \
	--image "$kit/boot.img" \
	--partition_name boot \
	--partition_size 100663296 \
	--salt "$salt"

tar --owner=0 --group=0 -C "$out/modules-root" \
	-cf "$kit/modules-$release.tar" "usr/lib/modules/$release"
install -m 0644 "$here/install.sh" "$here/rollback.sh" "$here/verify.sh" "$kit/"
(cd "$kit" && sha256sum boot.img "modules-$release.tar" install.sh \
	rollback.sh verify.sh > SHA256SUMS)
cat "$kit/SHA256SUMS"
