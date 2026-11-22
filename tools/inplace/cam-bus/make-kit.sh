#!/bin/bash
# Pack the camera-bus kit on the build host.  It goes on top of the BT UART
# kit and changes only the DTB: the rear camera's two buses (CCI0 master 1,
# the HI1337; CCI1 master 0, the DW9808 lens and its EEPROM) get stock's
# 400 kHz, 4 mA and weak pull-up instead of sm8550.dtsi's 1 MHz, 2 mA and
# strong 2.2 kOhm pull-up.  The kernel and module tree stay the BT UART kit's.
#
#   bash tools/inplace/cam-bus/make-kit.sh BT_KIT_DIR LENS_KIT_DIR NEW_DTB KIT_DIR
#
# boot.img: the BT kit's kernel (its boot.img's kernel minus the lens DTB it
# ends with) with NEW_DTB appended, by the same recipe; with the lens DTB the
# recipe must reproduce the BT kit's boot.img byte for byte, which is checked
# first.  vendor_boot.img: the lens kit's image with only its DTB swapped; a
# repack with the lens DTB must reproduce it byte for byte.  Nothing is
# flashed.
set -euo pipefail

[ $# -eq 4 ] || { echo "usage: $0 BT_KIT_DIR LENS_KIT_DIR NEW_DTB KIT_DIR" >&2; exit 2; }
btkit=$1
lenskit=$2
dtb=$(readlink -f "$3")
kit=$4
here=$(cd "$(dirname "$0")" && pwd)
workdir=${UBUNTU_WORKDIR:-$HOME/ubuntu-gts9u}
tools=${ANDROID_TOOLS:-$workdir/tools}
mkbootimg=$tools/mkbootimg.py
avbtool=$tools/avbtool.py
size=100663296

lens_dtb=164e1bf8a393c642ffc7bd357ce5dfbc608b07ba77070ede8b2052434de42a84
bt_boot=faa774ad340f66c07f1f0ad4948b6edc7e141a0cd2235b473234070588d317ba
lens_vendor_boot=b18510f38bae75d285347baef80a2beebfd57e246a2a7e7a4c7ce17983bd166a
# The lens DTB with stock's bus settings on CCI0 master 1 and CCI1 master 0.
pads_dtb=4ed9c1d943e24e5115841da4e3b8a6298f4e47e5dcdee74f1f3c5efebc3635ec

sha() { sha256sum "$1" | cut -d' ' -f1; }
for f in "$mkbootimg" "$avbtool" "$dtb" "$btkit/boot.img" "$lenskit/vendor_boot.img"; do
	[ -f "$f" ] || { echo "missing: $f" >&2; exit 1; }
done
[ "$(sha "$dtb")" = "$pads_dtb" ] || { echo "$dtb is not the camera-bus DTB ($pads_dtb)" >&2; exit 1; }
[ "$(sha "$btkit/boot.img")" = "$bt_boot" ] || { echo "$btkit/boot.img is not the BT UART kit's" >&2; exit 1; }
[ "$(sha "$lenskit/vendor_boot.img")" = "$lens_vendor_boot" ] || { echo "$lenskit/vendor_boot.img is not the lens kit's" >&2; exit 1; }
[ ! -e "$kit" ] || { echo "$kit exists; choose a new directory" >&2; exit 1; }

tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT

unpack() { mkdir -p "$2"; unpack_bootimg --boot_img "$1" --out "$2" --format=mkbootimg --null > "$2/args"; }

# The lens DTB, as the lens kit's vendor_boot carries it.
unpack "$lenskit/vendor_boot.img" "$tmp/base"
mapfile -d '' args < "$tmp/base/args"
dtb_index=
for i in "${!args[@]}"; do
	[ "${args[$i]}" = --dtb ] && dtb_index=$((i + 1))
done
[ -n "$dtb_index" ] || { echo 'the lens vendor_boot carries no DTB' >&2; exit 1; }
lens_copy=${args[$dtb_index]}
[ "$(sha "$lens_copy")" = "$lens_dtb" ] || { echo "the lens vendor_boot's DTB is not $lens_dtb" >&2; exit 1; }

# The BT kit's kernel: its boot kernel ends with exactly that DTB.
unpack "$btkit/boot.img" "$tmp/bt"
kernel_blob=$tmp/bt/kernel
[ -f "$kernel_blob" ] || { echo 'the BT boot.img has no kernel' >&2; exit 1; }
lens_size=$(stat -c %s "$lens_copy")
cmp -s <(tail -c "$lens_size" "$kernel_blob") "$lens_copy" ||
	{ echo 'the BT kernel does not end with the lens DTB' >&2; exit 1; }
head -c -"$lens_size" "$kernel_blob" > "$tmp/Image.gz"

pack_boot() { # DTB IMAGE: the BT/CX kits' recipe
	cat "$tmp/Image.gz" "$1" > "$tmp/Image.gz-dtb"
	python3 "$mkbootimg" --kernel "$tmp/Image.gz-dtb" --cmdline '' \
		--header_version 4 --os_version 13 --os_patch_level 2025-07 -o "$2"
	python3 "$avbtool" add_hash_footer --image "$2" --partition_name boot \
		--partition_size "$size" --salt "$(sha "$2")"
}
pack_boot "$lens_copy" "$tmp/self-boot.img"
[ "$(sha "$tmp/self-boot.img")" = "$bt_boot" ] || { echo 'self-test failed: the boot recipe does not reproduce the BT UART kit' >&2; exit 1; }
echo "self-test: the boot recipe reproduces the BT UART kit's boot.img (${bt_boot:0:12})"

pack_vb() { # DTB IMAGE
	local a=("${args[@]}")
	a[$dtb_index]=$1
	python3 "$mkbootimg" "${a[@]}" --vendor_boot "$2"
	python3 "$avbtool" add_hash_footer --image "$2" --partition_name vendor_boot \
		--partition_size "$size" --salt "$(sha "$2")"
}
pack_vb "${args[$dtb_index]}" "$tmp/self-vb.img" >/dev/null
[ "$(sha "$tmp/self-vb.img")" = "$lens_vendor_boot" ] || { echo 'self-test failed: the vendor_boot repack does not reproduce the lens kit' >&2; exit 1; }
echo "self-test: the vendor_boot repack reproduces the lens kit's (${lens_vendor_boot:0:12})"

mkdir -p "$kit"
pack_boot "$dtb" "$kit/boot.img"
pack_vb "$dtb" "$kit/vendor_boot.img" >/dev/null

unpack "$kit/boot.img" "$tmp/newboot"
cmp -s "$tmp/newboot/kernel" <(cat "$tmp/Image.gz" "$dtb") ||
	{ echo 'the new boot kernel is not the BT kernel plus the camera-bus DTB' >&2; exit 1; }
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
echo "boot and vendor_boot: only the DTB differs from the installed kits' (now ${pads_dtb:0:12})"

install -m 0644 "$here/install.sh" "$here/rollback.sh" "$here/README.md" "$kit/"
(cd "$kit" && sha256sum boot.img vendor_boot.img install.sh rollback.sh README.md > SHA256SUMS)
cat "$kit/SHA256SUMS"
