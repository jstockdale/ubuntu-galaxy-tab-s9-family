#!/bin/bash
# Rebuild a vendor_boot.img with a new vendor ramdisk fragment and nothing else
# changed: same DTB, same cmdline, same bootconfig, same header fields.  Runs on
# the build host, as the user, and never flashes anything.
#
#   make-vendor-boot.sh BASE_VENDOR_BOOT INITRAMFS_OVERLAY_DIR OUT_IMAGE
#
# INITRAMFS_OVERLAY_DIR is what scripts/build-rootfs-overlay.sh writes as
# out/initramfs-overlay (the Bluetooth set plus, on gts9wifi, the ath11k hw2.1
# set).  The fragment is packed from it with build-android-v4-bundle.sh's own
# recipe, so OUT_IMAGE is the vendor_boot the next release would build from the
# same DTB and cmdline as BASE.
#
# Before building anything it proves the repack is exact: BASE's own ramdisk,
# unpacked and packed again the same way, must reproduce BASE byte for byte.
# Afterwards it checks that every file of BASE's ramdisk is in the new one
# unchanged, and that DTB, cmdline and bootconfig read back identical.
#
# Tools: unpack_bootimg (Ubuntu package mkbootimg), lz4, cpio, python3, and the
# build's own mkbootimg.py and avbtool.py from $ANDROID_TOOLS (default
# $UBUNTU_WORKDIR/tools, UBUNTU_WORKDIR defaulting to ~/ubuntu-gts9u).
set -euo pipefail

[ $# -eq 3 ] || { echo "usage: $0 BASE_VENDOR_BOOT INITRAMFS_OVERLAY_DIR OUT_IMAGE" >&2; exit 2; }
base_img=$(readlink -f "$1")
overlay=$(readlink -f "$2")
out_img=$3

workdir=${UBUNTU_WORKDIR:-$HOME/ubuntu-gts9u}
tools=${ANDROID_TOOLS:-$workdir/tools}
mkbootimg=$tools/mkbootimg.py
avbtool=$tools/avbtool.py
vendor_boot_size=100663296

for f in "$base_img" "$mkbootimg" "$avbtool"; do
	[ -f "$f" ] || { echo "missing: $f" >&2; exit 1; }
done
[ -d "$overlay" ] || { echo "missing initramfs overlay: $overlay" >&2; exit 1; }
for t in unpack_bootimg lz4 cpio python3 sha256sum; do
	command -v "$t" >/dev/null || { echo "missing tool: $t" >&2; exit 1; }
done
[ "$(stat -c %s "$base_img")" = "$vendor_boot_size" ] ||
	{ echo "$base_img is not a $vendor_boot_size-byte vendor_boot image" >&2; exit 1; }

tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
sha() { sha256sum "$1" | cut -d' ' -f1; }

# unpack_bootimg prints the mkbootimg arguments that rebuild the image; with
# --null they come NUL-separated, so the cmdline survives intact.
unpack() { # IMAGE DIR
	mkdir -p "$2"
	unpack_bootimg --boot_img "$1" --out "$2" --format=mkbootimg --null > "$2/args"
}
unpack "$base_img" "$tmp/base"
mapfile -d '' args < "$tmp/base/args"

# Exactly the layout build-android-v4-bundle.sh produces: header v4, 4 KiB
# pages, one platform fragment.
fragment_index=
for i in "${!args[@]}"; do
	case "${args[$i]}" in
	--vendor_ramdisk_fragment)
		[ -z "$fragment_index" ] || { echo 'more than one vendor ramdisk fragment' >&2; exit 1; }
		fragment_index=$((i + 1)) ;;
	--header_version) [ "${args[$((i + 1))]}" = 4 ] || { echo 'not a v4 vendor_boot' >&2; exit 1; } ;;
	esac
done
[ -n "$fragment_index" ] || { echo 'no vendor ramdisk fragment in the base image' >&2; exit 1; }
base_fragment=${args[$fragment_index]}

# pack DIR FRAGMENT: build-android-v4-bundle.sh's recipe, verbatim.  The
# release build runs as root (umask 022), so its directories are 0755 and the
# overlay installs files 0644; a user's umask must not leak into the archive.
pack() {
	find "$1" -type d -exec chmod 0755 {} +
	find "$1" -type f -exec chmod 0644 {} +
	find "$1" -exec touch -h -d '@0' {} +
	(
		cd "$1"
		find . -print0 | LC_ALL=C sort -z \
			| cpio --reproducible --owner=0:0 --null -o --format=newc 2>/dev/null
	) | lz4 -l -12 - "$2" >/dev/null 2>&1
}
# build FRAGMENT IMAGE: the base's own arguments with the fragment swapped.
build() {
	local a=("${args[@]}")
	a[$fragment_index]=$1
	python3 "$mkbootimg" "${a[@]}" --vendor_boot "$2"
	# Deterministic salt, as add_hash_footer in build-android-v4-bundle.sh.
	python3 "$avbtool" add_hash_footer --image "$2" --partition_name vendor_boot \
		--partition_size "$vendor_boot_size" --salt "$(sha "$2")"
}

# --- 1. the repack must reproduce the base exactly --------------------------------
mkdir "$tmp/base-ramdisk"
lz4 -dc "$base_fragment" | (cd "$tmp/base-ramdisk" && cpio -idm --no-absolute-filenames --quiet)
pack "$tmp/base-ramdisk" "$tmp/self.lz4"
cmp -s "$tmp/self.lz4" "$base_fragment" || {
	echo 'self-test failed: repacking the base ramdisk does not reproduce its fragment' >&2
	exit 1
}
build "$tmp/self.lz4" "$tmp/self.img" >/dev/null
[ "$(sha "$tmp/self.img")" = "$(sha "$base_img")" ] || {
	echo 'self-test failed: repacking the base image does not reproduce it byte for byte' >&2
	exit 1
}
echo "self-test: repack reproduces $(basename "$base_img") ($(sha "$base_img" | cut -c1-12)) exactly"

# --- 2. the new fragment ------------------------------------------------------------
mkdir "$tmp/new-ramdisk"
cp -a "$overlay/." "$tmp/new-ramdisk/"
pack "$tmp/new-ramdisk" "$tmp/new.lz4"
# Nothing the base carried may be lost or changed.
(cd "$tmp/base-ramdisk" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0r sha256sum) > "$tmp/base.sums"
(cd "$tmp/new-ramdisk" && sha256sum -c --quiet "$tmp/base.sums") || {
	echo 'the new ramdisk drops or changes files the base carried' >&2
	exit 1
}
build "$tmp/new.lz4" "$tmp/out.img" >/dev/null

# --- 3. read back: only the fragment differs -----------------------------------------
unpack "$tmp/out.img" "$tmp/check"
mapfile -d '' check_args < "$tmp/check/args"
[ "${#check_args[@]}" = "${#args[@]}" ] || { echo 'argument count differs after rebuild' >&2; exit 1; }
for i in "${!args[@]}"; do
	[ "$i" = "$fragment_index" ] && continue
	case "${args[$i]}" in
	"$tmp"/base/*) cmp -s "${args[$i]}" "${check_args[$i]}" ||
		{ echo "rebuilt image differs in ${args[$((i - 1))]}" >&2; exit 1; } ;;
	*) [ "${args[$i]}" = "${check_args[$i]}" ] ||
		{ echo "rebuilt image differs: ${args[$i]} vs ${check_args[$i]}" >&2; exit 1; } ;;
	esac
done
cmp -s "${check_args[$fragment_index]}" "$tmp/new.lz4" || { echo 'fragment did not read back' >&2; exit 1; }
[ "$(stat -c %s "$tmp/out.img")" = "$vendor_boot_size" ] || { echo 'wrong image size' >&2; exit 1; }

mkdir -p "$(dirname "$out_img")"
cp "$tmp/out.img" "$out_img"
echo "base:     $(sha "$base_img")  $(stat -c %s "$base_fragment")-byte fragment"
echo "out:      $(sha "$out_img")  $(stat -c %s "$tmp/new.lz4")-byte fragment"
echo "dtb, cmdline, bootconfig and header: identical to the base"
echo 'added to the ramdisk:'
(cd "$tmp/new-ramdisk" && find . -type f | LC_ALL=C sort) | while read -r f; do
	[ -e "$tmp/base-ramdisk/$f" ] || printf '  %s  %s\n' "$(sha "$tmp/new-ramdisk/$f" | cut -c1-16)" "${f#./}"
done
