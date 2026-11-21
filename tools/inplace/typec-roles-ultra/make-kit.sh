#!/bin/bash
# Pack the Type-C roles kit for the SM-X910 on the build host.  It goes on
# top of release v1.2.5: the release kernel rebuilt with this branch's two
# TCPM patches, the sm5714_usbpd policy that turns them on and the retention
# of the USB power domains across suspend (docs/usb-c-dock-roles.md), in
# boot, plus the build's module tree, signed with its own key.  The
# configuration and the DTB do not change, so vendor_boot stays the
# release's.
#
#   bash tools/inplace/typec-roles-ultra/make-kit.sh BUILD_SCRATCH \
#       KERNEL_OUT_DIR BASE_KERNEL_OUT_DIR BASE_BUNDLE_DIR KIT_DIR
#
# BUILD_SCRATCH holds the build's linux-src-gts9uwifi and linux-gts9uwifi
# (KERNEL_WORKTREE and KERNEL_BUILD_DIR), KERNEL_OUT_DIR its output;
# BASE_KERNEL_OUT_DIR is the release's kernel build
# (~/ubuntu-gts9u/out/kernel-gts9uwifi), BASE_BUNDLE_DIR its images
# (~/ubuntu-gts9u/out/bundle).  BASE_SCRATCH, when set, is the release
# build's scratch; its kernel source tree is then the reference the new one
# is compared with, file by file, and its compiled clock controller the
# reference for the power domain table.
#
# Self-tests, each byte for byte:
#   - boot: the release kernel plus its DTB, by the kits' recipe, is the
#     release's boot.img, the image on the tablet;
#   - the release vendor_boot carries that same DTB.
# Then the changes are checked to be only the intended ones: the DTB, the
# configuration, the fingerprint layers and the module list are the release
# build's, the source trees differ in the three Type-C files and the clock
# controller alone and those are what this checkout's patches and driver
# give, the new image has the new code and the old one does not, the
# compiled power domain table differs in the two power-state masks alone, and
# the new boot image differs only in its kernel.  Nothing is flashed.
set -euo pipefail
export LC_ALL=C

[ $# -eq 5 ] || { sed -n 's/^#   //p' "$0" >&2; exit 2; }
scratch=$1
out=$2
base=$3
bundle=$4
kit=$5
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../.." && pwd)
tree=$scratch/linux-src-gts9uwifi
build=$scratch/linux-gts9uwifi
workdir=${UBUNTU_WORKDIR:-$HOME/ubuntu-gts9u}
tools=${ANDROID_TOOLS:-$workdir/tools}
registry=${KERNEL_KEY_REGISTRY:-$workdir/kernel-keys}
mkbootimg=$tools/mkbootimg.py
avbtool=$tools/avbtool.py
size=100663296
dts=sm8550-samsung-gts9uwifi
tcpm_dir=drivers/usb/typec/tcpm
gcc=drivers/clk/qcom/gcc-sm8550
patches=(tcpm-request-dfp-as-sink-before-discovery.patch tcpm-try-snk-extra-wait.patch
	gcc-sm8550-usb-gdscs-retention.patch)
# The files those patches change.
patched=("$tcpm_dir/tcpm.c" include/linux/usb/tcpm.h "$gcc.c")

# The tablet's current set: release v1.2.5 (out/bundle of 2026-11-10).
base_boot=d09cb64ff411ca39feab17c1c4330081619df9304b6a5b974bf23ccab915bdac
base_vendor_boot=f3e8c6371d0a943a53d07787fa3e87c234967bcbee41210471f6cf46c0ee573d
base_image=5bb0cd52e5b868e25501765857f32924025883a87b7268ae972d3374a5b0177f
base_dtb=613b3bb7729d55d1c60aaeda348a098163b79aed1efbf24cdcc582ff0d58ccc4
base_config=a5648666cc6973ca2e78b3c47c8c660e03b0441defae69419b0618b194e1f1c7
base_build_id=3447147dc203542e441eebfa4d62e60c57100f50

# What only the new kernel may contain: the patch's log line, the driver's
# two parameters, and the deadline of the Try.SNK wait, a member of tcpm_port
# whose name is in the image's type information.  The last one is what tells
# this revision's wait from the plain longer timeout before it.
new_strings=('Requesting DFP before discovery' sink_host_swap try_snk_extra_wait_ms try_snk_wait_until)
# The objects of the changed sources.  Each has to be newer than what it was
# compiled from and older than the vmlinux it was linked into.
objects=("$gcc.o:$gcc.c" "$tcpm_dir/tcpm.o:$tcpm_dir/tcpm.c" "$tcpm_dir/tcpm.o:include/linux/usb/tcpm.h"
	"$tcpm_dir/sm5714_usbpd.o:$tcpm_dir/sm5714_usbpd.c"
	"$tcpm_dir/sm5714_usbpd.o:include/linux/usb/tcpm.h")

sha() { sha256sum "$1" | cut -d' ' -f1; }
die() { echo "$*" >&2; exit 1; }
for f in "$mkbootimg" "$avbtool" "$out/Image.gz" "$out/$dts.dtb" "$out/config" \
	"$out/fingerprint.layers" "$base/fingerprint.layers" \
	"$base/Image.gz" "$base/$dts.dtb" "$base/config" "$bundle/boot.img" \
	"$bundle/vendor_boot.img" "${patched[@]/#/$tree/}" \
	"$build/.config" "$build/certs/signing_key.x509" "$build/vmlinux" \
	"$build/$gcc.o" "$build/$tcpm_dir/tcpm.o" "$build/$tcpm_dir/sm5714_usbpd.o" \
	"$tree/$tcpm_dir/sm5714_usbpd.c" "$build/arch/arm64/boot/Image.gz" \
	"$repo/kernel/drivers/sm5714_usbpd.c" "${patches[@]/#/$repo/kernel/patches/}"; do
	[ -f "$f" ] || die "missing: $f"
done
for t in unpack_bootimg python3 sha256sum cmp diff patch tar modinfo xxd openssl git llvm-objcopy; do
	command -v "$t" >/dev/null || die "missing tool: $t"
done
[ "$(sha "$base/Image.gz")" = "$base_image" ] || die "$base is not release v1.2.5's kernel build"
[ "$(sha "$base/$dts.dtb")" = "$base_dtb" ] || die "$base/$dts.dtb is not release v1.2.5's"
[ "$(sha "$base/config")" = "$base_config" ] || die "$base/config is not release v1.2.5's"
[ "$(sha "$bundle/boot.img")" = "$base_boot" ] || die "$bundle/boot.img is not release v1.2.5's"
[ "$(sha "$bundle/vendor_boot.img")" = "$base_vendor_boot" ] || die "$bundle/vendor_boot.img is not release v1.2.5's"
cmp -s "$out/config" "$build/.config" || die "$out/config is not $build/.config"
release=$(cat "$out/kernel.release")
[ "$release" = 7.2.0-rc3-dirty ] || die "kernel release $release, expected 7.2.0-rc3-dirty"
modules=$out/modules-root/usr/lib/modules/$release
base_modules=$base/modules-root/usr/lib/modules/$release
[ -f "$modules/modules.dep" ] && [ -f "$base_modules/modules.dep" ] || die "no module tree in $out or $base"
[ ! -e "$kit" ] || die "$kit exists; choose a new directory"

tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT

unpack() { mkdir -p "$2"; unpack_bootimg --boot_img "$1" --out "$2" --format=mkbootimg --null > "$2/args"; }
pack_boot() { # IMAGE_GZ DTB OUT: the recipe of every kernel kit since the CX kit
	cat "$1" "$2" > "$tmp/Image.gz-dtb"
	python3 "$mkbootimg" --kernel "$tmp/Image.gz-dtb" --cmdline '' \
		--header_version 4 --os_version 13 --os_patch_level 2025-07 -o "$3"
	python3 "$avbtool" add_hash_footer --image "$3" --partition_name boot \
		--partition_size "$size" --salt "$(sha "$3")"
}

# 1. The images on the tablet, rebuilt from their parts.
unpack "$bundle/boot.img" "$tmp/b"
cmp -s "$tmp/b/kernel" <(cat "$base/Image.gz" "$base/$dts.dtb") ||
	die 'the release boot kernel is not the release Image.gz plus its DTB'
pack_boot "$base/Image.gz" "$base/$dts.dtb" "$tmp/self-boot.img"
[ "$(sha "$tmp/self-boot.img")" = "$base_boot" ] ||
	die 'self-test failed: the boot recipe does not reproduce the release image'
unpack "$bundle/vendor_boot.img" "$tmp/vb"
mapfile -d '' args < "$tmp/vb/args"
dtb_index=
for i in "${!args[@]}"; do
	[ "${args[$i]}" = --dtb ] && dtb_index=$((i + 1))
done
[ -n "$dtb_index" ] || die 'the release vendor_boot carries no DTB'
[ "$(sha "${args[$dtb_index]}")" = "$base_dtb" ] || die "the release vendor_boot's DTB is not $base_dtb"
echo "self-test: boot (${base_boot:0:12}) reproduced byte for byte; vendor_boot (${base_vendor_boot:0:12}) carries the same DTB"

# 2. The DTB, the configuration and the fingerprint layers: the release
# build's, unchanged.
cmp -s "$out/$dts.dtb" "$base/$dts.dtb" ||
	die "the DTB changed ($(sha "$out/$dts.dtb")); this kit does not write vendor_boot"
conf() { # CONFIG -> "SYMBOL<tab>VALUE" lines, "n" for "is not set"
	sed -n -e 's/^\(CONFIG_[A-Za-z0-9_]*\)=\(.*\)$/\1\t\2/p' \
		-e 's/^# \(CONFIG_[A-Za-z0-9_]*\) is not set$/\1\tn/p' "$1" | sort
}
cmp -s "$out/config" "$base/config" || {
	echo 'the configuration differs from the release kernel'"'"'s:' >&2
	join -t "$(printf '\t')" -a1 -a2 -e - -o 0,1.2,2.2 <(conf "$base/config") <(conf "$out/config") |
		awk -F '\t' '$2 != $3' >&2
	exit 1
}
[ "$("$tree/scripts/extract-ikconfig" "$out/Image.gz" | sha256sum | cut -d' ' -f1)" = "$base_config" ] ||
	die 'the configuration embedded in Image.gz is not out/config'
cmp -s "$out/fingerprint.layers" "$base/fingerprint.layers" ||
	die "fingerprint.layers differs from the release build's: $(tr '\n' ' ' < "$out/fingerprint.layers")"
echo "DTB (${base_dtb:0:12}), configuration (${base_config:0:12}) and fingerprint layers: the release build's, byte for byte"

# 3. The sources.  The driver is this checkout's; the two TCPM files and the
# clock controller are the release tree's with this checkout's three patches
# on top; nothing else in the kernel tree differs.
cmp -s "$tree/$tcpm_dir/sm5714_usbpd.c" "$repo/kernel/drivers/sm5714_usbpd.c" ||
	die "the build tree's sm5714_usbpd.c is not this checkout's"
grep -q 'request_dfp_as_sink' "$tree/include/linux/usb/tcpm.h" &&
	grep -q 'try_snk_extra_wait_ms' "$tree/include/linux/usb/tcpm.h" &&
	grep -q 'tcpm_try_snk_wait_left' "$tree/$tcpm_dir/tcpm.c" &&
	grep -q 'Requesting DFP before discovery' "$tree/$tcpm_dir/tcpm.c" ||
	die 'the build tree does not carry both TCPM patches'
# The power state the source gives one power domain.  Counting the
# PWRSTS_RET_ON lines of the file does not do: the four PCIe domains carry
# it upstream.
gdsc_pwrsts() { # FILE NAME
	awk -v head="static struct gdsc $2 = {" '
		$0 == head { inside = 1; next }
		inside && /^};/ { exit }
		inside && $1 == ".pwrsts" { print $3 }
	' "$1"
}
for d in usb30_prim_gdsc usb3_phy_gdsc; do
	[ "$(gdsc_pwrsts "$tree/$gcc.c" "$d")" = 'PWRSTS_RET_ON,' ] ||
		die "the build tree does not carry the USB power domain patch ($d)"
done
grep -q 'Retention across system suspend, as on sm8650' "$tree/$gcc.c" ||
	die 'the build tree does not carry the USB power domain patch'
source_check='marker lines only (BASE_SCRATCH not set)'
if [ -n "${BASE_SCRATCH:-}" ]; then
	base_tree=$BASE_SCRATCH/linux-src-gts9uwifi
	for f in "${patched[@]}"; do
		[ -f "$base_tree/$f" ] || die "no kernel source tree in $BASE_SCRATCH"
		mkdir -p "$tmp/src/${f%/*}"
		cp "$base_tree/$f" "$tmp/src/$f"
	done
	for p in "${patches[@]}"; do
		patch -s -d "$tmp/src" -p1 --no-backup-if-mismatch < "$repo/kernel/patches/$p" ||
			die "$p does not apply to the release tree"
	done
	for f in "${patched[@]}"; do
		cmp -s "$tmp/src/$f" "$tree/$f" ||
			die "$f is not the release tree's with this checkout's patches applied"
	done
	# The isolated M= module builds leave their products next to the
	# sources (net/netfilter, ath12k, ...), and those differ with every
	# build: they are signed with the build's own key.
	diff -rq -x .git -x '*.o' -x '*.ko' -x '.*.cmd' -x '*.mod' -x '*.mod.c' \
		-x modules.order -x Module.symvers \
		"$base_tree" "$tree" > "$tmp/trees.diff" || :
	sed -e "s|^Files $base_tree/\(.*\) and .* differ\$|\1|" "$tmp/trees.diff" | sort > "$tmp/trees.changed"
	sort > "$tmp/trees.want" <<EOF
$gcc.c
$tcpm_dir/sm5714_usbpd.c
$tcpm_dir/tcpm.c
include/linux/usb/tcpm.h
EOF
	cmp -s "$tmp/trees.changed" "$tmp/trees.want" || {
		echo 'the kernel source trees differ in more than the three Type-C files and gcc-sm8550.c:' >&2
		cat "$tmp/trees.diff" >&2
		exit 1
	}
	source_check="compared with $base_tree: four files differ, tcpm.c, tcpm.h and gcc-sm8550.c by this checkout's three patches"
	echo 'sources: only sm5714_usbpd.c, tcpm.c, tcpm.h and gcc-sm8550.c differ from the release tree, by exactly this checkout'"'"'s changes'
else
	echo 'sources: the three patches and this checkout'"'"'s driver are in the build tree (BASE_SCRATCH not set: no tree comparison)'
fi

# 4. The images: the new code is in the new one and not in the old one.
# They are unpacked to files first: with pipefail, a grep -q that stops at
# its first match fails the zcat feeding it, and the match with it.
zcat "$out/Image.gz" > "$tmp/new.Image"
zcat "$base/Image.gz" > "$tmp/base.Image"
for s in "${new_strings[@]}"; do
	grep -qaF -- "$s" "$tmp/new.Image" || die "the new kernel image does not contain '$s'"
	! grep -qaF -- "$s" "$tmp/base.Image" || die "self-test failed: the release image contains '$s' too"
done
echo "image: ${#new_strings[@]} strings of the change are in the new kernel and not in the release one"
# The image is the build directory's, made from its vmlinux, and that from
# objects compiled after the last change to their sources.
for pair in "${objects[@]}"; do
	o=$build/${pair%%:*}
	[ "$o" -nt "$tree/${pair#*:}" ] && [ "$build/vmlinux" -nt "$o" ] ||
		die "${pair%%:*} is older than ${pair#*:}, or newer than vmlinux"
done
[ "$build/arch/arm64/boot/Image.gz" -nt "$build/vmlinux" ] || die 'Image.gz is older than vmlinux'
cmp -s "$build/arch/arm64/boot/Image.gz" "$out/Image.gz" || die "$out/Image.gz is not the build directory's"
# The power domain change is two numbers in a table and leaves no string.
# The table was compiled from the patched source and linked into this image
# (the check above), and against the release build's it differs in exactly
# those two: 5 (PWRSTS_OFF_ON) became 6 (PWRSTS_RET_ON).
gdsc_check='the object is newer than the patched source and older than vmlinux (BASE_SCRATCH not set)'
if [ -n "${BASE_SCRATCH:-}" ]; then
	[ -f "$BASE_SCRATCH/linux-gts9uwifi/$gcc.o" ] || die "no $gcc.o in $BASE_SCRATCH"
	llvm-objcopy -O binary --only-section=.data "$BASE_SCRATCH/linux-gts9uwifi/$gcc.o" "$tmp/gcc.base.data"
	llvm-objcopy -O binary --only-section=.data "$build/$gcc.o" "$tmp/gcc.new.data"
	[ -s "$tmp/gcc.new.data" ] || die "no .data in $gcc.o"
	cmp -l "$tmp/gcc.base.data" "$tmp/gcc.new.data" > "$tmp/gcc.data.diff" || :
	[ "$(awk '{print $2, $3}' "$tmp/gcc.data.diff" | tr '\n' ';')" = '5 6;5 6;' ] || {
		echo "the compiled $gcc table does not differ from the release build's in two power-state masks alone:" >&2
		cat "$tmp/gcc.data.diff" >&2
		exit 1
	}
	gdsc_check="the compiled table differs from the release build's in two bytes, 5 to 6"
fi
echo "power domains: $gdsc_check"

# 5. The kernel's GNU build ID note: the vDSOs' come first, the kernel's last.
build_id() {
	xxd -p "$1" | tr -d '\n' |
		grep -o '040000001400000003000000474e5500[0-9a-f]\{40\}' | tail -1 | cut -c33-
}
[ "$(build_id "$tmp/base.Image")" = "$base_build_id" ] ||
	die 'self-test failed: the build ID read from the release image is not the one the tablet reports'
new_id=$(build_id "$tmp/new.Image")
[ ${#new_id} = 40 ] && [ "$new_id" != "$base_build_id" ] || die 'no new build ID in the new image'
echo "build IDs: release ${base_build_id:0:12}, new ${new_id:0:12}"

# 6. The module key and the modules.  The serial is what modinfo reports as
# sig_key; the subject key ID is what the kernel logs and the registry files
# it under.  Only the public certificate is read here.
cert=$build/certs/signing_key.x509
serial=$(openssl x509 -inform DER -in "$cert" -noout -serial | cut -d= -f2 | sed 's/../&:/g; s/:$//')
key_id=$(openssl x509 -inform DER -in "$cert" -noout -ext subjectKeyIdentifier |
	sed -n 's/^ *\([0-9A-F:]*\)$/\1/p' | tr -d ':' | tr 'A-F' 'a-f')
[ ${#key_id} = 40 ] || die "could not read the subject key ID of $cert"
list() { (cd "$1" && find . -name '*.ko' -o -name '*.ko.zst' | sort); }
base_serial=$(modinfo -F sig_key "$base_modules/$(list "$base_modules" | sed -n 1p)")
[ -n "$base_serial" ] || die 'the release module tree is not signed'
[ "$serial" != "$base_serial" ] || die 'the build reused the release key; it needs its own (tcpc_dev and tcpm_port changed)'
# The registry's index: key ID, serial, ..., the build's Image.gz sha256.
[ -d "$registry/$key_id" ] && awk -F '\t' -v k="$key_id" -v i="$(sha "$out/Image.gz")" \
	'$1 == k && $6 == i {found = 1} END {exit !found}' "$registry/REGISTRY.tsv" ||
	die "key $key_id with this Image.gz is not in the registry ($registry); run scripts/kernel-key-registry.sh add $build"
diff <(list "$base_modules") <(list "$modules") > "$tmp/modules.diff" || {
	echo 'the module list differs from the release build:' >&2
	cat "$tmp/modules.diff" >&2
	exit 1
}
n=0
while IFS= read -r ko; do
	[ "$(modinfo -F sig_key "$modules/$ko")" = "$serial" ] || die "$ko is not signed with this build's key"
	case $(modinfo -F vermagic "$modules/$ko") in
	"$release "*) ;;
	*) die "$ko has another vermagic" ;;
	esac
	n=$((n + 1))
done < <(list "$modules")
echo "modules: $n, the release build's list, all signed with key ${key_id:0:12} (archived)"

# 7. The kit's boot image, and what changed in it.
mkdir -p "$kit"
pack_boot "$out/Image.gz" "$out/$dts.dtb" "$kit/boot.img"
[ "$(stat -c %s "$kit/boot.img")" = "$size" ] || die 'wrong boot.img size'
unpack "$kit/boot.img" "$tmp/nb"
mapfile -d '' a < "$tmp/b/args"
mapfile -d '' b < "$tmp/nb/args"
[ "${#a[@]}" = "${#b[@]}" ] || die 'boot header argument count differs from the release image'
for i in "${!a[@]}"; do
	case "${a[$i]}" in
	"$tmp"/b/kernel) cmp -s "${b[$i]}" <(cat "$out/Image.gz" "$out/$dts.dtb") ||
		die 'the boot kernel is not Image.gz plus the DTB' ;;
	"$tmp"/b/*) cmp -s "${a[$i]}" "${b[$i]}" || die "boot differs from the release image in ${a[$((i - 1))]}" ;;
	*) [ "${a[$i]}" = "${b[$i]}" ] || die "boot differs from the release image: ${a[$i]} vs ${b[$i]}" ;;
	esac
done
echo 'boot: only the kernel differs from the release image; vendor_boot is not part of this kit'

tar --owner=0 --group=0 -C "$out/modules-root" -cf "$kit/modules-$release.tar" "usr/lib/modules/$release"
printf '%s\n' "$new_id" > "$kit/BUILD_ID"
printf '%s\n' "$key_id" > "$kit/KEY_ID"
printf '%s\n' "$serial" > "$kit/SIGNER"
# What was built: this checkout, and whether it had uncommitted changes.
{
	echo "checkout: $(git -C "$repo" rev-parse HEAD) ($(git -C "$repo" describe --always --dirty --abbrev=12))"
	echo "uncommitted kernel inputs (git status --porcelain kernel scripts):"
	git -C "$repo" status --porcelain -- kernel scripts | sed 's/^/  /'
	echo "kernel/drivers/sm5714_usbpd.c $(sha "$repo/kernel/drivers/sm5714_usbpd.c")"
	for p in "${patches[@]}"; do
		echo "kernel/patches/$p $(sha "$repo/kernel/patches/$p")"
	done
	echo "source check: $source_check"
	echo "power domain table: $gdsc_check"
} > "$kit/SOURCE"
install -m 0644 "$here/install.sh" "$here/rollback.sh" "$here/verify.sh" "$here/README.md" "$kit/"
files=(boot.img "modules-$release.tar" BUILD_ID KEY_ID SIGNER SOURCE
	install.sh rollback.sh verify.sh README.md)
(cd "$kit" && sha256sum "${files[@]}" > SHA256SUMS)
cat "$kit/SHA256SUMS"
