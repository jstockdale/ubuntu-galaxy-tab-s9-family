#!/bin/bash
# Build the DISP-5 (panel cold boot) vendor_boot variants on the build host.
#
# Every variant is the fix2-s2 vendor_boot.img with extra kernel command-line
# tokens and nothing else: same DTB, same vendor ramdisk fragment, same
# bootconfig, same mkbootimg layout and AVB hash footer as
# scripts/build-android-v4-bundle.sh produces.  boot.img (the fix2-s2 kernel,
# which already carries every switch used here) is not touched.
#
#   tools/inplace/panel/make-images.sh BASE_VENDOR_BOOT OUTDIR
#
# BASE_VENDOR_BOOT is fix2-s2's vendor_boot.img (sha256 0742866c...; the
# tablet keeps a copy in ~/display-exp/fix2-s2/).  OUTDIR receives one
# directory per variant, each with vendor_boot.img, CMDLINE.txt and
# SHA256SUMS, plus IMAGES.sha256 over all of them.  Before building anything
# the script repacks the base with its own command line and requires the
# result to be byte-identical to the base, so the variants differ from fix2-s2
# in the command line only.  The output is reproducible: IMAGES.sha256 in the
# kit lists the hashes a correct build gives.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
tools=${ANDROID_TOOLS:-$HOME/ubuntu-gts9u/tools}
mkbootimg=$tools/mkbootimg.py
avbtool=$tools/avbtool.py
vendor_boot_size=100663296
base_sha=0742866cbfb01da797aed4129eacfb13655ca0476ec13546f210bf1c874f4fd5
base_cmdline='root=LABEL=UBTS9_UFS rootfstype=ext4 rootwait console=tty0 console=ttyMSM0,115200n8 earlycon loglevel=7 log_buf_len=4M panic=10 clk_ignore_unused pd_ignore_unused regulator_ignore_unused msm.separate_gpu_kms=1 initcall_blacklist=bwmon_driver_init icc_core.dyndbg=+p'

# The boot-time platform cycle must not run in the experiment boots, and the
# interconnect late release (overnight/icc) must keep the floor when the
# cycle did not run; masking it here makes that independent of whether its
# kit is installed.  systemd 255's debug generator honours systemd.mask=.
mask='systemd.mask=ubuntu-gts9u-panel-coldboot-recover.service systemd.mask=ubuntu-gts9u-icc-late-release.service'

# name|extra command-line tokens
variants=(
	"panel-ref|msm.dsi_regdump=1"
	"panel-e0|msm.dsi_regdump=1 $mask"
	"panel-e1|msm.dsi_regdump=1 msm.mdss_handoff_gdsc_cycle=1 $mask"
	"panel-e2|msm.dsi_regdump=1 msm.boot_display_cycle=1 $mask"
)

base=${1:?usage: $0 BASE_VENDOR_BOOT OUTDIR}
out=${2:?usage: $0 BASE_VENDOR_BOOT OUTDIR}

for f in "$mkbootimg" "$avbtool" "$base"; do
	test -f "$f" || { echo "missing: $f" >&2; exit 1; }
done
command -v unpack_bootimg >/dev/null || {
	echo 'unpack_bootimg not found (Debian/Ubuntu package mkbootimg)' >&2
	exit 1
}
have=$(sha256sum "$base" | cut -d' ' -f1)
if [ "$have" != "$base_sha" ] && [ "${FORCE_BASE:-0}" != 1 ]; then
	echo "base is $have, not fix2-s2's vendor_boot ($base_sha);" \
		'FORCE_BASE=1 to build on it anyway' >&2
	exit 1
fi

tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT

unpack_bootimg --boot_img "$base" --out "$tmp/unpack" > "$tmp/info.txt"
cmdline=$(sed -n 's/^vendor command line args: //p' "$tmp/info.txt")
if [ "$cmdline" != "$base_cmdline" ] && [ "${FORCE_BASE:-0}" != 1 ]; then
	echo "unexpected base command line: $cmdline" >&2
	exit 1
fi
grep -q '^vendor ramdisk table size: 108$' "$tmp/info.txt" || {
	echo 'base vendor_boot does not hold exactly one ramdisk fragment' >&2
	exit 1
}

# pack OUT CMDLINE: the same mkbootimg call and footer as the bundle script.
pack() {
	local target=$1 line=$2 salt
	python3 "$mkbootimg" \
		--ramdisk_type platform \
		--ramdisk_name '' \
		--vendor_ramdisk_fragment "$tmp/unpack/vendor_ramdisk00" \
		--dtb "$tmp/unpack/dtb" \
		--vendor_cmdline "$line" \
		--header_version 4 \
		--vendor_boot "$target" \
		--base 0x80000000 \
		--kernel_offset 0x8000 \
		--ramdisk_offset 0x02000000 \
		--tags_offset 0x01e00000 \
		--pagesize 4096 \
		--dtb_offset 0x1f00000 \
		--vendor_bootconfig "$tmp/unpack/bootconfig"
	salt=$(sha256sum "$target" | cut -d' ' -f1)
	python3 "$avbtool" add_hash_footer \
		--image "$target" \
		--partition_name vendor_boot \
		--partition_size "$vendor_boot_size" \
		--salt "$salt"
	[ "$(stat -c %s "$target")" -eq "$vendor_boot_size" ] || {
		echo "$target: not $vendor_boot_size bytes" >&2
		exit 1
	}
}

pack "$tmp/roundtrip.img" "$cmdline"
if ! cmp -s "$tmp/roundtrip.img" "$base"; then
	echo 'repacking the base with its own command line does not reproduce it;' \
		'refusing to build variants' >&2
	exit 1
fi
echo "round trip OK: repacked base is byte-identical ($have)"

mkdir -p "$out"
: > "$tmp/IMAGES.sha256"
for spec in "${variants[@]}"; do
	name=${spec%%|*}
	extra=${spec#*|}
	dir=$out/$name
	rm -rf -- "$dir"
	mkdir -p "$dir"
	line="$cmdline $extra"
	# The kernel keeps COMMAND_LINE_SIZE (2048) bytes and ABL appends about
	# 900 of its own (/proc/cmdline on fix2-s2: 1127); stay well clear.
	[ "${#line}" -lt 900 ] || { echo "$name: command line too long" >&2; exit 1; }
	pack "$dir/vendor_boot.img" "$line"
	unpack_bootimg --boot_img "$dir/vendor_boot.img" --out "$tmp/check-$name" \
		> "$tmp/check-$name.txt"
	[ "$(sed -n 's/^vendor command line args: //p' "$tmp/check-$name.txt")" = "$line" ] || {
		echo "$name: command line did not round-trip" >&2
		exit 1
	}
	for part in dtb vendor_ramdisk00 bootconfig; do
		cmp -s "$tmp/check-$name/$part" "$tmp/unpack/$part" || {
			echo "$name: $part differs from the base" >&2
			exit 1
		}
	done
	{
		echo "variant=$name"
		echo "base=fix2-s2 vendor_boot $base_sha (boot.img unchanged: fix2-s2)"
		echo "added=$extra"
		echo "vendor_cmdline=$line"
	} > "$dir/CMDLINE.txt"
	(cd "$dir" && sha256sum vendor_boot.img > SHA256SUMS)
	echo "$(cut -d' ' -f1 "$dir/SHA256SUMS")  $name/vendor_boot.img" >> "$tmp/IMAGES.sha256"
	echo "built $name: +$extra"
done
cp "$tmp/IMAGES.sha256" "$out/IMAGES.sha256"
if [ -f "$here/IMAGES.sha256" ]; then
	if diff -u "$here/IMAGES.sha256" "$out/IMAGES.sha256"; then
		echo 'hashes match the kit IMAGES.sha256'
	else
		echo 'WARNING: hashes differ from the kit IMAGES.sha256' >&2
		exit 1
	fi
fi
cat "$out/IMAGES.sha256"
