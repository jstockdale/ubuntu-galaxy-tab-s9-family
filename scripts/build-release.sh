#!/bin/bash
# Produce a complete, installable release: kernel, rootfs, overlay, root
# filesystem image, Android v4 bundle, TWRP ZIP and a SHA-256 manifest.
#
# The release is a single flashable ZIP.  Everything the tablet needs is in it,
# including the root filesystem, which the installer writes into the internal
# UFS; there is no second file to write to a card by hand any more.
#
# Every stage is a separate script that can be run on its own; this only fixes
# the order and the data flow between them.  Nothing is flashed here: the owner
# flashes the ZIP from TWRP.
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
. "$repo/scripts/lib/device.sh"
base=${UBUNTU_WORKDIR:-/root/ubuntu-gts9u}
version=${RELEASE_VERSION:-$(tr -d '[:space:]' < "$repo/VERSION")}
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
	echo "invalid release version: $version" >&2
	exit 2
}
profile=${GTS9U_PROFILE:-desktop}
artifacts=${ARTIFACTS_DIR:-$repo/artifacts}

# A firmware staging produced by scripts/stage-firmware.py identifies the
# stock package it came from; the identity is stamped into the image, the
# ZIP and the artifact name so a fleet with mixed bootloader revisions can
# tell the builds apart, and so the installer can refuse a mismatch.
fw_provenance=${FIRMWARE_DIR:+${FIRMWARE_DIR}/PROVENANCE.json}
fw_build_id= fw_blrev=
if [ -n "$fw_provenance" ] && [ -f "$fw_provenance" ]; then
	fw_build_id=$(python3 -c 'import json,sys
print(json.load(open(sys.argv[1]))["package"]["build_id"])' "$fw_provenance")
	fw_blrev=$(python3 -c 'import json,sys
print(json.load(open(sys.argv[1]))["package"]["bl_rev"])' "$fw_provenance")
	echo "firmware staging: $fw_build_id (bootloader revision $fw_blrev)"
fi

kernel_out=$base/out/kernel-$DEVICE
rootfs=${ROOTFS_DIR:-$base/rootfs}
export ROOTFS_DIR="$rootfs"
overlay=$base/out/rootfs-overlay
initramfs_overlay=$base/out/initramfs-overlay
bundle=$base/out/bundle
image=$base/out/ubuntu-$DEVICE${fw_blrev:+-bl$fw_blrev}-v$version-ufs.img
zip=$artifacts/ubuntu-24.04-$DEVICE_SLUG${fw_blrev:+-bl$fw_blrev}-v$version.zip

# No secret is needed to build a shipping image any more: it has no account,
# and the owner creates theirs in the first-boot wizard.  GTS9U_PW is only for
# development images that must be reachable over SSH before anyone logs in.

step() { printf '\n########## %s\n' "$1"; }

step "1/7 kernel, device tree and isolated $DEVICE_WLAN modules"
if [ "${SKIP_KERNEL:-0}" = 1 ] && [ -f "$kernel_out/Image.gz" ]; then
	echo 'reusing the existing kernel build'
else
	bash "$repo/scripts/build-mainline-kernel.sh"
fi

kernel_release=$(cat "$kernel_out/kernel.release")

step "2/7 Ubuntu $profile rootfs"
if [ "${SKIP_ROOTFS:-0}" = 1 ] && [ -d "$rootfs/etc" ]; then
	echo 'reusing the existing rootfs'
else
	GTS9U_PROFILE="$profile" \
	KERNEL_MODULES_ROOT="$kernel_out/modules-root" \
	KERNEL_RELEASE="$kernel_release" \
		bash "$repo/scripts/build-ubuntu-rootfs.sh"
fi

python3 "$repo/scripts/sanitize-release-rootfs.py" "$rootfs"

step "3/7 firmware and module overlay, and early Bluetooth firmware"
bash "$repo/scripts/build-rootfs-overlay.sh"

step "4/7 UFS root filesystem image and Ubuntu initramfs"
python3 - "$rootfs" "$version" "$kernel_release" "$fw_build_id" "$fw_blrev" "$DEVICE" <<'PY'
import json, pathlib, sys
root, version, kernel, fw_build_id, fw_blrev, device = sys.argv[1:]
identity = {'device': device, 'version': version, 'tag': 'v' + version,
            'kernel_release': kernel}
if fw_build_id:
    identity['firmware'] = {'build_id': fw_build_id, 'bl_rev': int(fw_blrev)}
pathlib.Path(root, 'usr/lib/gts9u-release.json').write_text(
    json.dumps(identity) + '\n')
PY
IMAGE_OUT="$image" KERNEL_RELEASE="$kernel_release" \
	bash "$repo/scripts/build-ufs-image.sh"

initramfs=$rootfs/boot/initrd.img-$kernel_release
test -f "$initramfs" || { echo "missing initramfs: $initramfs" >&2; exit 1; }

step "5/7 Android boot header v4 bundle"
INITRAMFS="$initramfs" \
INITRAMFS_OVERLAY_DIR="$initramfs_overlay" \
	bash "$repo/scripts/build-android-v4-bundle.sh"

step "6/7 TWRP ZIP"
mkdir -p "$artifacts"
python3 "$repo/scripts/build-update-payload.py" --base "$base" --rootfs "$rootfs" --version "$version" --device "$DEVICE" \
	--bootstrap "$base/out/gts9u-update.pyz"
zip_args=(--project "$repo" --update-payload "$base/out/update-payload" \
	--rootfs "$image")
if [ -n "$fw_blrev" ]; then
	zip_args+=(--label "Ubuntu 24.04 LTS v$version for $DEVICE_MODEL (mainline $kernel_release, firmware $fw_build_id)")
	zip_args+=(--firmware-identity "$DEVICE bl$fw_blrev $fw_build_id")
else
	zip_args+=(--label "Ubuntu 24.04 LTS v$version for $DEVICE_MODEL (mainline $kernel_release)")
fi
zip_args+=(--device-model "$DEVICE_MODEL" --device-codenames "$DEVICE_CODENAMES" --rootfs-label "$DEVICE_LABEL")
python3 "$repo/scripts/make-twrp-zip.py" "$bundle" "$zip" "${zip_args[@]}"

step "7/7 static validation and manifest"
bash "$repo/scripts/validate-bundle.sh" "$zip"
python3 "$repo/scripts/validate-update-zip.py" "$zip"

{
	printf 'Ubuntu 24.04 LTS for Samsung Galaxy Tab S9 Ultra Wi-Fi (SM-X910)\n'
	printf 'release: v%s (%s profile)\n' "$version" "$profile"
	printf 'generated: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
	printf 'kernel_release: %s\n' "$kernel_release"
	printf 'kernel_source: %s\n' \
		"$(git -C "$base/linux-mainline" rev-parse HEAD)"
	printf 'port_revision: %s\n' "$(git -C "$repo" rev-parse HEAD)"
	if [ -n "$fw_build_id" ]; then
		printf 'firmware_build: %s\n' "$fw_build_id"
		printf 'firmware_bl_rev: %s\n' "$fw_blrev"
	fi
	printf '\n'
	printf 'install_target: internal UFS, linuxroot if present, else userdata\n'
	printf 'rootfs_image_sha256: %s\n' \
		"$(sha256sum "$image" | cut -d' ' -f1)"
	printf 'rootfs_image_bytes: %s\n' "$(stat -c %s "$image")"
	printf '\n'
	( cd "$artifacts" && sha256sum "${zip##*/}" )
	printf '\n'
	cat "$bundle/SHA256SUMS"
} > "$artifacts/MANIFEST${fw_blrev:+-bl$fw_blrev}-v$version.txt"

printf '\n=== release v%s ===\n' "$version"
cat "$artifacts/MANIFEST${fw_blrev:+-bl$fw_blrev}-v$version.txt"
printf '\nNothing was flashed. Flash the ZIP from TWRP, from external media.\n'
