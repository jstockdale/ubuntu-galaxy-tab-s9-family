#!/bin/bash
# Assemble the in-place Wi-Fi kit on the build host, as the user.  Copies
# nothing to the tablet and flashes nothing: the last line it prints is the scp
# that the owner runs.
#
#   tools/inplace/wifi/stage.sh OUT_DIR BASE_VENDOR_BOOT [BASE_VENDOR_BOOT...]
#
# Each BASE is a vendor_boot.img the tablet may be running (for the 2026-11-07
# Stage 2 state: the fix2-s2 image, sha256 0742866c...).  For each, the kit gets
# its Wi-Fi variant, made by make-vendor-boot.sh from the initramfs overlay that
# scripts/build-rootfs-overlay.sh assembles for gts9wifi right here (into a
# temporary UBUNTU_WORKDIR, so nothing under ~/ubuntu-gts9u/out changes).
#
# Inputs (defaults are the X710 bl5 build's): FIRMWARE_DIR
# (~/ubuntu-gts9u/firmware/x710-bl5), KERNEL_OUT_DIR
# (~/ubuntu-gts9u/out/kernel-gts9wifi; the overlay script copies its module
# tree, which the kit does not use).
set -euo pipefail

[ $# -ge 2 ] || { echo "usage: $0 OUT_DIR BASE_VENDOR_BOOT [BASE_VENDOR_BOOT...]" >&2; exit 2; }
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../.." && pwd)
out=$1
shift

export DEVICE=gts9wifi
firmware=${FIRMWARE_DIR:-$HOME/ubuntu-gts9u/firmware/x710-bl5}
kernel_out=${KERNEL_OUT_DIR:-$HOME/ubuntu-gts9u/out/kernel-gts9wifi}
package=$repo/packaging/ubuntu-gts9u-device

[ ! -e "$out" ] || [ -z "$(ls -A "$out")" ] || { echo "$out exists and is not empty" >&2; exit 1; }
mkdir -p "$out/files" "$out/images"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

# --- the initramfs overlay, exactly as the release builds it ----------------------
UBUNTU_WORKDIR=$work FIRMWARE_DIR=$firmware KERNEL_OUT_DIR=$kernel_out \
	bash "$repo/scripts/build-rootfs-overlay.sh" > "$work/overlay.log" 2>&1 || {
	tail -n 20 "$work/overlay.log" >&2
	echo 'build-rootfs-overlay.sh failed' >&2
	exit 1
}
ramdisk=$work/out/initramfs-overlay
hw21=usr/lib/firmware/ath11k/WCN6855/hw2.1
[ -f "$ramdisk/$hw21/amss.bin" ] || { echo "the overlay script put no $hw21 into the ramdisk" >&2; exit 1; }
(cd "$ramdisk" && sha256sum "$hw21"/*) > "$out/FIRMWARE.sha256"

# --- one Wi-Fi variant per base ------------------------------------------------------
{
	echo '# base-sha256 wifi-variant-sha256 file'
	for base in "$@"; do
		base_sha=$(sha256sum "$base" | cut -d' ' -f1)
		file=images/vendor_boot-${base_sha:0:12}-wifi.img
		echo "== $base" >&2
		bash "$here/make-vendor-boot.sh" "$base" "$ramdisk" "$out/$file" >&2
		echo "$base_sha $(sha256sum "$out/$file" | cut -d' ' -f1) $file"
	done
} > "$out/IMAGES"

# --- scripts and the packaged helpers --------------------------------------------------------
install -m 0755 "$here/install.sh" "$here/rollback.sh" "$here/verify.sh" "$out/"
install -m 0644 "$here/README.md" "$out/"
install -m 0755 "$package/usr/libexec/ubuntu-gts9u-wlan-address" \
	"$package/usr/libexec/ubuntu-gts9u-pci-rescan" "$out/files/"
install -m 0644 "$package/usr/lib/systemd/system/ubuntu-gts9u-wlan-address.service" \
	"$package/usr/lib/systemd/system/gts9u-pci-rescan.service" "$out/files/"
git -C "$repo" log -1 --format='kit from %H (%cd)' > "$out/SOURCE"
git -C "$repo" status --porcelain >> "$out/SOURCE"

(cd "$out" && find . -type f ! -name SHA256SUMS -printf '%P\n' | LC_ALL=C sort |
	xargs -d '\n' sha256sum > SHA256SUMS)
echo
cat "$out/IMAGES"
echo "kit: $out ($(du -sh "$out" | cut -f1))"
echo "copy it with:  scp -r $out jstockdale@10.42.0.2:wifi-kit"
