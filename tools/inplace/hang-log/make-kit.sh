#!/bin/bash
# Pack the hang-log kit on the build host.  It goes on top of the camera-bus
# kit: the BT UART kernel rebuilt with this branch's hang-evidence options
# (config-gts9wifi.fragment), and the camera-bus DTB with the ramoops region
# reorganised (pmsg and ftrace zones, ECC 64), in boot (appended) and
# vendor_boot, plus the build's module tree, signed with its own key.
#
#   bash tools/inplace/hang-log/make-kit.sh BUILD_SCRATCH KERNEL_OUT_DIR \
#       BT_KERNEL_OUT_DIR CAMBUS_KIT_DIR KIT_DIR
#
# BUILD_SCRATCH holds the build's linux-src-gts9wifi and linux-gts9wifi
# (KERNEL_WORKTREE and KERNEL_BUILD_DIR), KERNEL_OUT_DIR its output;
# BT_KERNEL_OUT_DIR is the BT UART kit's kernel build
# (~/ubuntu-gts9u/out/kernel-gts9wifi-btirq2), CAMBUS_KIT_DIR the camera-bus
# kit (~/ubuntu-gts9u/backups/cam-bus-kit).
#
# Self-tests, with the changes dropped, each byte for byte:
#   - boot: the BT UART kernel plus the camera-bus DTB, by the kits' recipe,
#     is the camera-bus kit's boot.img, the image on the tablet;
#   - vendor_boot: the camera-bus vendor_boot repacked with its own DTB is
#     itself;
#   - DTB: the camera-bus commit's DTS, compiled in this build's own tree,
#     is the camera-bus DTB; this checkout's DTS gives the new one.
# Then the changes are checked to be only the intended ones: the
# configuration differs from the BT UART kernel's in the listed symbols
# alone, the DTB only in the ramoops node, the module list only by
# netconsole, and the new images only in their kernel or DTB.  Nothing is
# flashed.
set -euo pipefail
export LC_ALL=C

[ $# -eq 5 ] || { sed -n 's/^#   //p' "$0" >&2; exit 2; }
scratch=$1
out=$2
bt=$3
cambus=$4
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
[ -x /usr/lib/llvm-22/bin/clang ] && export PATH=/usr/lib/llvm-22/bin:$PATH

# The tablet's current set: the camera-bus kit over the BT UART kit.
cambus_boot=5529f6e70dbc80cdc1b173770a4138fbdc8fd8829708fc32a9cc703e6c7c9996
cambus_vendor_boot=51777e20a965ce7d3a63d03adf8d2fdfdafa844a0dbdff9149032bcaf37a4d94
cambus_dtb=4ed9c1d943e24e5115841da4e3b8a6298f4e47e5dcdee74f1f3c5efebc3635ec
# The commit whose DTS is the camera-bus DTB.
cambus_rev=52cb2fc
bt_image=7b8de34ea2a69c7290eadc6bbfb4cdd267772e00eb9e57848555e7e854494f55
bt_build_id=e862f606ab391efa4a42f394fb4aff845c804069
fix6_config=c17980f07ca04461e67dba7e1b3d58f9719d079f65bcc3d45b723fc2c27ccf5a

# Every symbol whose value may differ from the BT UART kernel's, as
# SYMBOL<tab>OLD<tab>NEW ("-": absent, "n": not set): what
# config-gts9wifi.fragment's hang-evidence lines ask for, and what Kconfig
# derives from them (NETPOLL and NET_POLL_CONTROLLER from NETCONSOLE,
# PRINTK_EXECUTION_CTX selected by NETCONSOLE_DYNAMIC, the lockup core and
# the buddy hard-lockup detector, the new panic switches at their defaults).
expected_config_diff() {
	cat <<'EOF'
CONFIG_BOOTPARAM_HARDLOCKUP_PANIC	-	n
CONFIG_BOOTPARAM_HUNG_TASK_PANIC	-	0
CONFIG_BOOTPARAM_SOFTLOCKUP_PANIC	-	0
CONFIG_BOOTPARAM_WQ_STALL_PANIC	-	0
CONFIG_DEFAULT_HUNG_TASK_TIMEOUT	-	60
CONFIG_DETECT_HUNG_TASK	n	y
CONFIG_DETECT_HUNG_TASK_BLOCKER	-	y
CONFIG_HARDLOCKUP_DETECTOR	n	y
CONFIG_HARDLOCKUP_DETECTOR_ARCH	-	n
CONFIG_HARDLOCKUP_DETECTOR_BUDDY	-	y
CONFIG_HARDLOCKUP_DETECTOR_COUNTS_HRTIMER	-	y
CONFIG_HARDLOCKUP_DETECTOR_PERF	-	n
CONFIG_LOCKUP_DETECTOR	-	y
CONFIG_NETCONSOLE	n	m
CONFIG_NETCONSOLE_DYNAMIC	-	y
CONFIG_NETCONSOLE_EXTENDED_LOG	-	n
CONFIG_NETPOLL	-	y
CONFIG_NET_POLL_CONTROLLER	-	y
CONFIG_PRINTK_EXECUTION_CTX	-	y
CONFIG_PSTORE_FTRACE	n	y
CONFIG_SOFTLOCKUP_DETECTOR	n	y
CONFIG_SOFTLOCKUP_DETECTOR_INTR_STORM	-	n
CONFIG_WQ_WATCHDOG	n	y
EOF
}

sha() { sha256sum "$1" | cut -d' ' -f1; }
die() { echo "$*" >&2; exit 1; }
for f in "$mkbootimg" "$avbtool" "$out/Image.gz" "$out/$dts.dtb" "$out/config" \
	"$bt/Image.gz" "$bt/config" "$cambus/boot.img" "$cambus/vendor_boot.img" \
	"$tree/arch/arm64/boot/dts/qcom/$dts.dts" "$build/.config" \
	"$build/certs/signing_key.x509" "$build/scripts/dtc/dtc"; do
	[ -f "$f" ] || die "missing: $f"
done
for t in unpack_bootimg python3 sha256sum cmp tar modinfo xxd openssl make git; do
	command -v "$t" >/dev/null || die "missing tool: $t"
done
[ "$(sha "$bt/Image.gz")" = "$bt_image" ] || die "$bt is not the BT UART kit's kernel build"
[ "$(sha "$bt/config")" = "$fix6_config" ] || die "$bt/config is not fix6's"
[ "$(sha "$cambus/boot.img")" = "$cambus_boot" ] || die "$cambus/boot.img is not the camera-bus kit's"
[ "$(sha "$cambus/vendor_boot.img")" = "$cambus_vendor_boot" ] || die "$cambus/vendor_boot.img is not the camera-bus kit's"
cmp -s "$out/config" "$build/.config" || die "$out/config is not $build/.config"
release=$(cat "$out/kernel.release")
[ "$release" = 7.2.0-rc3-dirty ] || die "kernel release $release, expected 7.2.0-rc3-dirty"
modules=$out/modules-root/usr/lib/modules/$release
bt_modules=$bt/modules-root/usr/lib/modules/$release
[ -f "$modules/modules.dep" ] && [ -f "$bt_modules/modules.dep" ] || die "no module tree in $out or $bt"
[ ! -e "$kit" ] || die "$kit exists; choose a new directory"

tmp=$(mktemp -d)
board=$tree/arch/arm64/boot/dts/qcom/$dts.dts
cp -p "$board" "$tmp/board.dts"
restore_board() {
	cmp -s "$tmp/board.dts" "$board" || cp "$tmp/board.dts" "$board"
	rm -rf -- "$tmp"
}
trap restore_board EXIT

unpack() { mkdir -p "$2"; unpack_bootimg --boot_img "$1" --out "$2" --format=mkbootimg --null > "$2/args"; }
pack_boot() { # IMAGE_GZ DTB OUT: the CX, lens, BT UART and camera-bus kits' recipe
	cat "$1" "$2" > "$tmp/Image.gz-dtb"
	python3 "$mkbootimg" --kernel "$tmp/Image.gz-dtb" --cmdline '' \
		--header_version 4 --os_version 13 --os_patch_level 2025-07 -o "$3"
	python3 "$avbtool" add_hash_footer --image "$3" --partition_name boot \
		--partition_size "$size" --salt "$(sha "$3")"
}

# 1. The images on the tablet, rebuilt from their parts.
unpack "$cambus/vendor_boot.img" "$tmp/vb"
mapfile -d '' args < "$tmp/vb/args"
dtb_index=
for i in "${!args[@]}"; do
	[ "${args[$i]}" = --dtb ] && dtb_index=$((i + 1))
done
[ -n "$dtb_index" ] || die 'the camera-bus vendor_boot carries no DTB'
[ "$(sha "${args[$dtb_index]}")" = "$cambus_dtb" ] || die "the camera-bus vendor_boot's DTB is not $cambus_dtb"
cp "${args[$dtb_index]}" "$tmp/cambus.dtb"
pack_vb() { # DTB OUT
	local a=("${args[@]}")
	a[$dtb_index]=$1
	python3 "$mkbootimg" "${a[@]}" --vendor_boot "$2"
	python3 "$avbtool" add_hash_footer --image "$2" --partition_name vendor_boot \
		--partition_size "$size" --salt "$(sha "$2")"
}
unpack "$cambus/boot.img" "$tmp/b"
cmp -s "$tmp/b/kernel" <(cat "$bt/Image.gz" "$tmp/cambus.dtb") ||
	die 'the camera-bus boot kernel is not the BT UART Image.gz plus the camera-bus DTB'
pack_boot "$bt/Image.gz" "$tmp/cambus.dtb" "$tmp/self-boot.img"
[ "$(sha "$tmp/self-boot.img")" = "$cambus_boot" ] ||
	die 'self-test failed: the boot recipe does not reproduce the camera-bus kit'
pack_vb "$tmp/cambus.dtb" "$tmp/self-vb.img" >/dev/null
[ "$(sha "$tmp/self-vb.img")" = "$cambus_vendor_boot" ] ||
	die 'self-test failed: the vendor_boot repack does not reproduce the camera-bus kit'
echo "self-test: boot (${cambus_boot:0:12}) and vendor_boot (${cambus_vendor_boot:0:12}) reproduced byte for byte"

# 2. The DTB: both DTS versions compiled in the build's own tree.
cmp -s "$board" "$repo/kernel/dts/$dts.dts" || die "the build tree's DTS is not this checkout's"
# Removing the DTB first makes make rebuild it whatever the files' times.
built=$build/arch/arm64/boot/dts/qcom/$dts.dtb
dtb_make() { rm -f "$built"; make -s -C "$tree" O="$build" ARCH=arm64 LLVM=1 "qcom/$dts.dtb"; }
git -C "$repo" show "$cambus_rev:kernel/dts/$dts.dts" > "$board"
dtb_make
[ "$(sha "$built")" = "$cambus_dtb" ] ||
	die "self-test failed: the $cambus_rev DTS does not compile to the camera-bus DTB"
cp "$tmp/board.dts" "$board"
dtb_make
cmp -s "$built" "$out/$dts.dtb" ||
	die "this checkout's DTS does not compile to $out/$dts.dtb"
echo "self-test: the $cambus_rev DTS compiles to the camera-bus DTB (${cambus_dtb:0:12}); this one to $(sha "$out/$dts.dtb" | cut -c1-12)"
decompile() { "$build/scripts/dtc/dtc" -I dtb -O dts -q "$1"; }
diff <(decompile "$tmp/cambus.dtb") <(decompile "$out/$dts.dtb") > "$tmp/dtb.diff" || :
grep '^[<>]' "$tmp/dtb.diff" | sed 's/^\([<>]\) *\t*/\1    /' | sort > "$tmp/dtb.changed" || :
sort > "$tmp/dtb.want" <<'EOF'
<    ecc-size = <0x10>;
>    ecc-size = <0x40>;
>    ftrace-size = <0x20000>;
>    pmsg-size = <0x20000>;
EOF
cmp -s "$tmp/dtb.changed" "$tmp/dtb.want" || {
	echo 'the DTB differs from the camera-bus one in more than the ramoops node:' >&2
	cat "$tmp/dtb.diff" >&2
	exit 1
}
grep -q 'sec-pmsg@880900000' <(decompile "$out/$dts.dtb") || die 'no sec-pmsg@880900000 in the new DTB'
echo 'DTB: only the ramoops node changed (ecc-size 16 -> 64, pmsg-size and ftrace-size added)'

# 3. The configuration: the BT UART kernel's but for the expected symbols.
conf() { # CONFIG -> "SYMBOL<tab>VALUE" lines, "n" for "is not set"
	sed -n -e 's/^\(CONFIG_[A-Za-z0-9_]*\)=\(.*\)$/\1\t\2/p' \
		-e 's/^# \(CONFIG_[A-Za-z0-9_]*\) is not set$/\1\tn/p' "$1" | sort
}
join -t "$(printf '\t')" -a1 -a2 -e - -o 0,1.2,2.2 <(conf "$bt/config") <(conf "$out/config") |
	awk -F '\t' '$2 != $3' > "$tmp/config.diff"
cmp -s "$tmp/config.diff" <(expected_config_diff) || {
	echo 'the configuration differs from the BT UART kernel'"'"'s other than expected:' >&2
	diff <(expected_config_diff) "$tmp/config.diff" >&2 || :
	exit 1
}
echo "config: $(wc -l < "$tmp/config.diff") symbols differ from the BT UART kernel's, all expected"
[ "$("$tree/scripts/extract-ikconfig" "$out/Image.gz" | sha256sum | cut -d' ' -f1)" = "$(sha "$out/config")" ] ||
	die 'the configuration embedded in Image.gz is not out/config'

# 4. The kernel's GNU build ID note: the vDSOs' come first, the kernel's last.
build_id() {
	zcat "$1" | xxd -p | tr -d '\n' |
		grep -o '040000001400000003000000474e5500[0-9a-f]\{40\}' | tail -1 | cut -c33-
}
[ "$(build_id "$bt/Image.gz")" = "$bt_build_id" ] ||
	die 'self-test failed: the build ID read from the BT UART image is not the one the tablet reports'
new_id=$(build_id "$out/Image.gz")
[ ${#new_id} = 40 ] && [ "$new_id" != "$bt_build_id" ] || die 'no new build ID in the new image'
echo "build IDs: BT UART ${bt_build_id:0:12}, new ${new_id:0:12}"

# 5. The module key and the modules.  The serial is what modinfo reports as
# sig_key; the subject key ID is what the kernel logs and the registry files
# it under.  Only the public certificate is read here.
cert=$build/certs/signing_key.x509
serial=$(openssl x509 -inform DER -in "$cert" -noout -serial | cut -d= -f2 | sed 's/../&:/g; s/:$//')
key_id=$(openssl x509 -inform DER -in "$cert" -noout -ext subjectKeyIdentifier |
	sed -n 's/^ *\([0-9A-F:]*\)$/\1/p' | tr -d ':' | tr 'A-F' 'a-f')
[ ${#key_id} = 40 ] || die "could not read the subject key ID of $cert"
bt_serial=$(modinfo -F sig_key "$bt_modules/updates/v4l2loopback.ko")
[ "$serial" != "$bt_serial" ] || die 'the build reused the BT UART key; it needs its own (the ABI changed)'
# The registry's index: key ID, serial, ..., the build's Image.gz sha256.
[ -d "$registry/$key_id" ] && awk -F '\t' -v k="$key_id" -v i="$(sha "$out/Image.gz")" \
	'$1 == k && $6 == i {found = 1} END {exit !found}' "$registry/REGISTRY.tsv" ||
	die "key $key_id with this Image.gz is not in the registry ($registry); run scripts/kernel-key-registry.sh add $build"
list() { (cd "$1" && find . -name '*.ko' -o -name '*.ko.zst' | sort); }
diff <(list "$bt_modules") <(list "$modules") | grep '^[<>]' > "$tmp/modules.diff" || :
[ "$(cat "$tmp/modules.diff")" = '> ./kernel/drivers/net/netconsole.ko.zst' ] || {
	echo 'the module list differs from the BT UART build other than by netconsole:' >&2
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
echo "modules: $n, the BT UART build's plus netconsole, all signed with key ${key_id:0:12} (archived)"

# 6. The kit's images, and what changed in them.
mkdir -p "$kit"
pack_boot "$out/Image.gz" "$out/$dts.dtb" "$kit/boot.img"
pack_vb "$out/$dts.dtb" "$kit/vendor_boot.img" >/dev/null
for f in boot.img vendor_boot.img; do
	[ "$(stat -c %s "$kit/$f")" = "$size" ] || die "wrong $f size"
done
unpack "$kit/boot.img" "$tmp/nb"
mapfile -d '' a < "$tmp/b/args"
mapfile -d '' b < "$tmp/nb/args"
[ "${#a[@]}" = "${#b[@]}" ] || die 'boot header argument count differs from the camera-bus kit'
for i in "${!a[@]}"; do
	case "${a[$i]}" in
	"$tmp"/b/kernel) cmp -s "${b[$i]}" <(cat "$out/Image.gz" "$out/$dts.dtb") ||
		die 'the boot kernel is not Image.gz plus the new DTB' ;;
	"$tmp"/b/*) cmp -s "${a[$i]}" "${b[$i]}" || die "boot differs from the camera-bus kit in ${a[$((i - 1))]}" ;;
	*) [ "${a[$i]}" = "${b[$i]}" ] || die "boot differs from the camera-bus kit: ${a[$i]} vs ${b[$i]}" ;;
	esac
done
unpack "$kit/vendor_boot.img" "$tmp/nvb"
mapfile -d '' c < "$tmp/nvb/args"
[ "${#c[@]}" = "${#args[@]}" ] || die 'vendor_boot argument count differs after the swap'
for i in "${!args[@]}"; do
	[ "$i" = "$dtb_index" ] && continue
	case "${args[$i]}" in
	"$tmp"/vb/*) cmp -s "${args[$i]}" "${c[$i]}" || die "the new vendor_boot differs in ${args[$((i - 1))]}" ;;
	*) [ "${args[$i]}" = "${c[$i]}" ] || die "the new vendor_boot differs: ${args[$i]}" ;;
	esac
done
cmp -s "${c[$dtb_index]}" "$out/$dts.dtb" || die 'the DTB did not read back from vendor_boot'
echo 'boot: only the kernel and its appended DTB differ from the camera-bus kit; vendor_boot: only the DTB'

tar --owner=0 --group=0 -C "$out/modules-root" -cf "$kit/modules-$release.tar" "usr/lib/modules/$release"
printf '%s\n' "$new_id" > "$kit/BUILD_ID"
printf '%s\n' "$key_id" > "$kit/KEY_ID"
printf '%s\n' "$serial" > "$kit/SIGNER"
cp "$tmp/config.diff" "$kit/CONFIG.diff"
cp "$tmp/dtb.diff" "$kit/DTB.diff"
install -m 0644 "$here/install.sh" "$here/rollback.sh" "$here/verify.sh" "$here/README.md" \
	"$here/60-gts9u-hang-log.conf" "$kit/"
files=(boot.img vendor_boot.img "modules-$release.tar" BUILD_ID KEY_ID SIGNER CONFIG.diff
	DTB.diff install.sh rollback.sh verify.sh 60-gts9u-hang-log.conf README.md)
(cd "$kit" && sha256sum "${files[@]}" > SHA256SUMS)
cat "$kit/SHA256SUMS"
