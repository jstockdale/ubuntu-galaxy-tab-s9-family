#!/bin/bash
# Pack the Type-C roles kit on the build host.  It goes on top of the
# hang-log kit: the hang-log kernel rebuilt with this branch's four TCPM
# patches and the sm5714_usbpd policy that turns them on
# (docs/usb-c-dock-roles.md), in boot, plus the build's module tree, signed
# with its own key.  The configuration and the DTB do not change, so
# vendor_boot stays the hang-log kit's.
#
#   bash tools/inplace/typec-roles/make-kit.sh BUILD_SCRATCH KERNEL_OUT_DIR \
#       HANG_KERNEL_OUT_DIR HANG_KIT_DIR KIT_DIR
#
# BUILD_SCRATCH holds the build's linux-src-gts9wifi and linux-gts9wifi
# (KERNEL_WORKTREE and KERNEL_BUILD_DIR), KERNEL_OUT_DIR its output;
# HANG_KERNEL_OUT_DIR is the hang-log kit's kernel build
# (~/ubuntu-gts9u/out/kernel-gts9wifi-hang), HANG_KIT_DIR that kit
# (~/ubuntu-gts9u/backups/hang-log-kit-r2).  HANG_SCRATCH, when set, is the
# hang-log build's scratch; its kernel source tree is then the reference the
# new one is compared with, file by file.
#
# Self-tests, each byte for byte:
#   - boot: the hang-log kernel plus its DTB, by the kits' recipe, is the
#     hang-log kit's boot.img, the image on the tablet;
#   - the hang-log vendor_boot carries that same DTB.
# Then the changes are checked to be only the intended ones: the DTB, the
# configuration and the module list are the hang-log build's, the source
# trees differ in the three Type-C files alone and those are what this
# checkout's patches and driver give, the new image has the new code and the
# old one does not and was built after the last change to those files, and
# the new boot image differs only in its kernel.  Nothing is flashed.
set -euo pipefail
export LC_ALL=C

[ $# -eq 5 ] || { sed -n 's/^#   //p' "$0" >&2; exit 2; }
scratch=$1
out=$2
hang=$3
hangkit=$4
kit=$5
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../.." && pwd)
tree=$scratch/linux-src-gts9wifi
build=$scratch/linux-gts9wifi
workdir=${UBUNTU_WORKDIR:-$HOME/ubuntu-gts9u}
tools=${ANDROID_TOOLS:-$workdir/tools}
registry=${KERNEL_KEY_REGISTRY:-$workdir/kernel-keys}
mkbootimg=$tools/mkbootimg.py
avbtool=$tools/avbtool.py
size=100663296
dts=sm8550-samsung-gts9wifi
tcpm_dir=drivers/usb/typec/tcpm
patches=(tcpm-request-dfp-as-sink-before-discovery.patch tcpm-try-snk-extra-wait.patch
	tcpm-no-dr-swap-during-vdm.patch tcpm-dr-set-checks-data-role.patch)

# The tablet's current set: the hang-log kit.
hang_boot=d583546b5e5c33dbc48fbafc99b7eb039df33f64b0316e498d3f158f834ee968
hang_vendor_boot=9165f42917a8ee4069b878fc0c0660391b39087fc6f3ea2b607f626821a7dbd4
hang_image=3cb91f345b5e217290a737f254d7a278c426a0885305de008f308b27e7321216
hang_dtb=5ff2495f1cc0e43f141956f84f2d24a4a325ede3e246806d7f034ba89d3c4060
hang_config=4cd636e12d586ae4c0510217e4237f90627013afa2406a9d34a694ab5c44c394
hang_build_id=68d243e908c0edfddc10429ede4bb235f205e44c

# What only the new kernel may contain: the patch's log line, the driver's
# two parameters, and the deadline of the Try.SNK wait, a member of tcpm_port
# whose name is in the image's type information; that one tells the wait
# with a deadline from the plain longer timeout before it.  Then what this
# revision adds: the log lines of the two fixes for a data role swap asked
# for from userspace, and policy_open, a member of the driver's structure,
# which only a driver that reads its parameters at each attach has.
new_strings=('Requesting DFP before discovery' sink_host_swap try_snk_extra_wait_ms try_snk_wait_until
	'VDM in progress, DATA_ROLE_SWAP not started' 'Data role swap ended with the role unchanged'
	policy_open)
# The objects of the changed sources.  Each has to be newer than what it was
# compiled from and older than the vmlinux it was linked into.
objects=("$tcpm_dir/tcpm.o:$tcpm_dir/tcpm.c" "$tcpm_dir/tcpm.o:include/linux/usb/tcpm.h"
	"$tcpm_dir/sm5714_usbpd.o:$tcpm_dir/sm5714_usbpd.c"
	"$tcpm_dir/sm5714_usbpd.o:include/linux/usb/tcpm.h")

sha() { sha256sum "$1" | cut -d' ' -f1; }
die() { echo "$*" >&2; exit 1; }
for f in "$mkbootimg" "$avbtool" "$out/Image.gz" "$out/$dts.dtb" "$out/config" \
	"$hang/Image.gz" "$hang/$dts.dtb" "$hang/config" "$hangkit/boot.img" \
	"$hangkit/vendor_boot.img" "$tree/$tcpm_dir/tcpm.c" "$tree/include/linux/usb/tcpm.h" \
	"$build/.config" "$build/certs/signing_key.x509" "$build/vmlinux" \
	"$build/$tcpm_dir/tcpm.o" "$build/$tcpm_dir/sm5714_usbpd.o" \
	"$tree/$tcpm_dir/sm5714_usbpd.c" "$build/arch/arm64/boot/Image.gz" \
	"$repo/kernel/drivers/sm5714_usbpd.c" "${patches[@]/#/$repo/kernel/patches/}"; do
	[ -f "$f" ] || die "missing: $f"
done
for t in unpack_bootimg python3 sha256sum cmp diff patch tar modinfo xxd openssl git; do
	command -v "$t" >/dev/null || die "missing tool: $t"
done
[ "$(sha "$hang/Image.gz")" = "$hang_image" ] || die "$hang is not the hang-log kit's kernel build"
[ "$(sha "$hang/$dts.dtb")" = "$hang_dtb" ] || die "$hang/$dts.dtb is not the hang-log kit's"
[ "$(sha "$hang/config")" = "$hang_config" ] || die "$hang/config is not the hang-log kit's"
[ "$(sha "$hangkit/boot.img")" = "$hang_boot" ] || die "$hangkit/boot.img is not the hang-log kit's"
[ "$(sha "$hangkit/vendor_boot.img")" = "$hang_vendor_boot" ] || die "$hangkit/vendor_boot.img is not the hang-log kit's"
cmp -s "$out/config" "$build/.config" || die "$out/config is not $build/.config"
release=$(cat "$out/kernel.release")
[ "$release" = 7.2.0-rc3-dirty ] || die "kernel release $release, expected 7.2.0-rc3-dirty"
modules=$out/modules-root/usr/lib/modules/$release
hang_modules=$hang/modules-root/usr/lib/modules/$release
[ -f "$modules/modules.dep" ] && [ -f "$hang_modules/modules.dep" ] || die "no module tree in $out or $hang"
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
unpack "$hangkit/boot.img" "$tmp/b"
cmp -s "$tmp/b/kernel" <(cat "$hang/Image.gz" "$hang/$dts.dtb") ||
	die 'the hang-log boot kernel is not the hang-log Image.gz plus its DTB'
pack_boot "$hang/Image.gz" "$hang/$dts.dtb" "$tmp/self-boot.img"
[ "$(sha "$tmp/self-boot.img")" = "$hang_boot" ] ||
	die 'self-test failed: the boot recipe does not reproduce the hang-log kit'
unpack "$hangkit/vendor_boot.img" "$tmp/vb"
mapfile -d '' args < "$tmp/vb/args"
dtb_index=
for i in "${!args[@]}"; do
	[ "${args[$i]}" = --dtb ] && dtb_index=$((i + 1))
done
[ -n "$dtb_index" ] || die 'the hang-log vendor_boot carries no DTB'
[ "$(sha "${args[$dtb_index]}")" = "$hang_dtb" ] || die "the hang-log vendor_boot's DTB is not $hang_dtb"
echo "self-test: boot (${hang_boot:0:12}) reproduced byte for byte; vendor_boot (${hang_vendor_boot:0:12}) carries the same DTB"

# 2. The DTB and the configuration: the hang-log build's, unchanged.
cmp -s "$out/$dts.dtb" "$hang/$dts.dtb" ||
	die "the DTB changed ($(sha "$out/$dts.dtb")); this kit does not write vendor_boot"
conf() { # CONFIG -> "SYMBOL<tab>VALUE" lines, "n" for "is not set"
	sed -n -e 's/^\(CONFIG_[A-Za-z0-9_]*\)=\(.*\)$/\1\t\2/p' \
		-e 's/^# \(CONFIG_[A-Za-z0-9_]*\) is not set$/\1\tn/p' "$1" | sort
}
cmp -s "$out/config" "$hang/config" || {
	echo 'the configuration differs from the hang-log kernel'"'"'s:' >&2
	join -t "$(printf '\t')" -a1 -a2 -e - -o 0,1.2,2.2 <(conf "$hang/config") <(conf "$out/config") |
		awk -F '\t' '$2 != $3' >&2
	exit 1
}
[ "$("$tree/scripts/extract-ikconfig" "$out/Image.gz" | sha256sum | cut -d' ' -f1)" = "$hang_config" ] ||
	die 'the configuration embedded in Image.gz is not out/config'
echo "DTB (${hang_dtb:0:12}) and configuration (${hang_config:0:12}): the hang-log build's, byte for byte"

# 3. The sources.  The driver is this checkout's; the two TCPM files are the
# hang-log tree's with this checkout's four patches on top; nothing else in
# the kernel tree differs.
cmp -s "$tree/$tcpm_dir/sm5714_usbpd.c" "$repo/kernel/drivers/sm5714_usbpd.c" ||
	die "the build tree's sm5714_usbpd.c is not this checkout's"
grep -q 'request_dfp_as_sink' "$tree/include/linux/usb/tcpm.h" &&
	grep -q 'try_snk_extra_wait_ms' "$tree/include/linux/usb/tcpm.h" &&
	grep -q 'tcpm_try_snk_wait_left' "$tree/$tcpm_dir/tcpm.c" &&
	grep -q 'Requesting DFP before discovery' "$tree/$tcpm_dir/tcpm.c" &&
	grep -q 'VDM in progress, DATA_ROLE_SWAP not started' "$tree/$tcpm_dir/tcpm.c" &&
	grep -q 'Data role swap ended with the role unchanged' "$tree/$tcpm_dir/tcpm.c" ||
	die 'the build tree does not carry the four TCPM patches'
source_check='marker lines only (HANG_SCRATCH not set)'
if [ -n "${HANG_SCRATCH:-}" ]; then
	hang_tree=$HANG_SCRATCH/linux-src-gts9wifi
	[ -f "$hang_tree/$tcpm_dir/tcpm.c" ] || die "no kernel source tree in $HANG_SCRATCH"
	mkdir -p "$tmp/src/$tcpm_dir" "$tmp/src/include/linux/usb"
	cp "$hang_tree/$tcpm_dir/tcpm.c" "$tmp/src/$tcpm_dir/"
	cp "$hang_tree/include/linux/usb/tcpm.h" "$tmp/src/include/linux/usb/"
	for p in "${patches[@]}"; do
		patch -s -d "$tmp/src" -p1 --no-backup-if-mismatch < "$repo/kernel/patches/$p" ||
			die "$p does not apply to the hang-log tree"
	done
	for f in "$tcpm_dir/tcpm.c" include/linux/usb/tcpm.h; do
		cmp -s "$tmp/src/$f" "$tree/$f" ||
			die "$f is not the hang-log tree's with this checkout's patches applied"
	done
	# The isolated M= module builds leave their products next to the
	# sources (net/netfilter, ath12k, ...), and those differ with every
	# build: they are signed with the build's own key.
	diff -rq -x .git -x '*.o' -x '*.ko' -x '.*.cmd' -x '*.mod' -x '*.mod.c' \
		-x modules.order -x Module.symvers \
		"$hang_tree" "$tree" > "$tmp/trees.diff" || :
	sed -e "s|^Files $hang_tree/\(.*\) and .* differ\$|\1|" "$tmp/trees.diff" | sort > "$tmp/trees.changed"
	sort > "$tmp/trees.want" <<EOF
$tcpm_dir/sm5714_usbpd.c
$tcpm_dir/tcpm.c
include/linux/usb/tcpm.h
EOF
	cmp -s "$tmp/trees.changed" "$tmp/trees.want" || {
		echo 'the kernel source trees differ in more than the three Type-C files:' >&2
		cat "$tmp/trees.diff" >&2
		exit 1
	}
	source_check="compared with $hang_tree: three files differ, tcpm.c and tcpm.h by this checkout's four patches"
	echo 'sources: only sm5714_usbpd.c, tcpm.c and tcpm.h differ from the hang-log tree, by exactly this checkout'"'"'s changes'
else
	echo 'sources: the four TCPM patches and this checkout'"'"'s driver are in the build tree (HANG_SCRATCH not set: no tree comparison)'
fi

# 4. The images: the new code is in the new one and not in the old one.
# They are unpacked to files first: with pipefail, a grep -q that stops at
# its first match fails the zcat feeding it, and the match with it.
zcat "$out/Image.gz" > "$tmp/new.Image"
zcat "$hang/Image.gz" > "$tmp/hang.Image"
for s in "${new_strings[@]}"; do
	grep -qaF -- "$s" "$tmp/new.Image" || die "the new kernel image does not contain '$s'"
	! grep -qaF -- "$s" "$tmp/hang.Image" || die "self-test failed: the hang-log image contains '$s' too"
done
echo "image: ${#new_strings[@]} strings of the change are in the new kernel and not in the hang-log one"
# The image is the build directory's, made from its vmlinux, and that from
# objects compiled after the last change to their sources.
for pair in "${objects[@]}"; do
	o=$build/${pair%%:*}
	[ "$o" -nt "$tree/${pair#*:}" ] && [ "$build/vmlinux" -nt "$o" ] ||
		die "${pair%%:*} is older than ${pair#*:}, or newer than vmlinux"
done
[ "$build/arch/arm64/boot/Image.gz" -nt "$build/vmlinux" ] || die 'Image.gz is older than vmlinux'
cmp -s "$build/arch/arm64/boot/Image.gz" "$out/Image.gz" || die "$out/Image.gz is not the build directory's"
echo 'build: the changed objects are newer than their sources, vmlinux newer than them, and Image.gz is the build directory'"'"'s'

# 5. The kernel's GNU build ID note: the vDSOs' come first, the kernel's last.
build_id() {
	xxd -p "$1" | tr -d '\n' |
		grep -o '040000001400000003000000474e5500[0-9a-f]\{40\}' | tail -1 | cut -c33-
}
[ "$(build_id "$tmp/hang.Image")" = "$hang_build_id" ] ||
	die 'self-test failed: the build ID read from the hang-log image is not the one the tablet reports'
new_id=$(build_id "$tmp/new.Image")
[ ${#new_id} = 40 ] && [ "$new_id" != "$hang_build_id" ] || die 'no new build ID in the new image'
echo "build IDs: hang-log ${hang_build_id:0:12}, new ${new_id:0:12}"

# 6. The module key and the modules.  The serial is what modinfo reports as
# sig_key; the subject key ID is what the kernel logs and the registry files
# it under.  Only the public certificate is read here.
cert=$build/certs/signing_key.x509
serial=$(openssl x509 -inform DER -in "$cert" -noout -serial | cut -d= -f2 | sed 's/../&:/g; s/:$//')
key_id=$(openssl x509 -inform DER -in "$cert" -noout -ext subjectKeyIdentifier |
	sed -n 's/^ *\([0-9A-F:]*\)$/\1/p' | tr -d ':' | tr 'A-F' 'a-f')
[ ${#key_id} = 40 ] || die "could not read the subject key ID of $cert"
hang_serial=$(modinfo -F sig_key "$hang_modules/updates/v4l2loopback.ko")
[ "$serial" != "$hang_serial" ] || die 'the build reused the hang-log key; it needs its own (tcpc_dev and tcpm_port changed)'
# The registry's index: key ID, serial, ..., the build's Image.gz sha256.
[ -d "$registry/$key_id" ] && awk -F '\t' -v k="$key_id" -v i="$(sha "$out/Image.gz")" \
	'$1 == k && $6 == i {found = 1} END {exit !found}' "$registry/REGISTRY.tsv" ||
	die "key $key_id with this Image.gz is not in the registry ($registry); run scripts/kernel-key-registry.sh add $build"
list() { (cd "$1" && find . -name '*.ko' -o -name '*.ko.zst' | sort); }
diff <(list "$hang_modules") <(list "$modules") > "$tmp/modules.diff" || {
	echo 'the module list differs from the hang-log build:' >&2
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
echo "modules: $n, the hang-log build's list, all signed with key ${key_id:0:12} (archived)"

# 7. The kit's boot image, and what changed in it.
mkdir -p "$kit"
pack_boot "$out/Image.gz" "$out/$dts.dtb" "$kit/boot.img"
[ "$(stat -c %s "$kit/boot.img")" = "$size" ] || die 'wrong boot.img size'
unpack "$kit/boot.img" "$tmp/nb"
mapfile -d '' a < "$tmp/b/args"
mapfile -d '' b < "$tmp/nb/args"
[ "${#a[@]}" = "${#b[@]}" ] || die 'boot header argument count differs from the hang-log kit'
for i in "${!a[@]}"; do
	case "${a[$i]}" in
	"$tmp"/b/kernel) cmp -s "${b[$i]}" <(cat "$out/Image.gz" "$out/$dts.dtb") ||
		die 'the boot kernel is not Image.gz plus the DTB' ;;
	"$tmp"/b/*) cmp -s "${a[$i]}" "${b[$i]}" || die "boot differs from the hang-log kit in ${a[$((i - 1))]}" ;;
	*) [ "${a[$i]}" = "${b[$i]}" ] || die "boot differs from the hang-log kit: ${a[$i]} vs ${b[$i]}" ;;
	esac
done
echo 'boot: only the kernel differs from the hang-log kit; vendor_boot is not part of this kit'

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
} > "$kit/SOURCE"
install -m 0644 "$here/install.sh" "$here/rollback.sh" "$here/verify.sh" "$here/README.md" "$kit/"
files=(boot.img "modules-$release.tar" BUILD_ID KEY_ID SIGNER SOURCE
	install.sh rollback.sh verify.sh README.md)
(cd "$kit" && sha256sum "${files[@]}" > SHA256SUMS)
cat "$kit/SHA256SUMS"
