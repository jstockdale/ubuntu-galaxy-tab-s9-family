#!/bin/bash
# Pack the Type-C roles kit for the SM-X910 on the build host.  It goes on
# top of release v1.2.5: the release kernel rebuilt with this branch's five
# TCPM patches, the sm5714_usbpd policy that turns them on, the retention of
# the USB power domains across suspend and the NULL check of the hcd in the
# dwc3-qcom glue (docs/usb-c-dock-roles.md), in boot, plus the build's module
# tree, signed with its own key.  The DTB does not change, so vendor_boot
# stays the release's.  Since 2026-11-22 the build also replaces the
# release's SM-X910-only PCS reset of the UFS PHY with the same write for
# every SM8550 PHY (docs/resume-recovery.md): no change at run time on this
# tablet.
#
# Revision 8 (2026-11-25, written, not built) is the deep-sleep groundwork:
# what the SM-X710's kit revision 14 sleeps deeply with, on this board.  Its
# kernel is this checkout's default gts9uwifi build, whose configuration is
# the release's with four lines changed (config_changes: the video clock
# controller and the SoC sleep statistics built in, the SM-X710's two pstore
# lines), whose early helper also holds L15B in HPM and applies two more
# Ultra overlays (the SM-X710's sync_state hand-off, gcc under CX, and its
# ramoops region: kernel/dts/gts9u-sync-state.dtso, gts9u-ramoops.dtso), and
# which drops the UFS and USB CX votes in system sleep on this board too;
# built with the SM-X710 kit's switches:
# APPLY_QMP_UFS_PCS_STATE_DIAGNOSTIC=1, APPLY_DEEP_SLEEP_DEBUG_SWITCHES=1,
# APPLY_SM8550_SLEEP_FIXES=1, APPLY_SM8550_PCIE0_GDSC_OFF=1,
# APPLY_SM8550_PCIE0_GDSC_GUARD=1, APPLY_LPASS_VA_MACRO_PM_CLK=1,
# APPLY_DSI_PHY_AHB_RELEASE=1 and APPLY_GENI_SERIAL_FORCE_SUSPEND=1 (and not
# APPLY_SM8550_ROOT_PORT_D3).  The kit also carries the SM-X710 kit's
# sleep-test.sh and ufs-phy-power-check.sh, which know both boards.
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
# is compared with, file by file, its compiled clock controller the
# reference for the power domain table, and its vmlinux the self-test of the
# disassembly checks.
#
# Self-tests, each byte for byte:
#   - boot: the release kernel plus its DTB, by the kits' recipe, is the
#     release's boot.img, the image on the tablet;
#   - the release vendor_boot carries that same DTB.
# Then the changes are checked to be only the intended ones: the DTB, the
# fingerprint layers and the module list are the release build's, the
# configuration is the release's with config_changes and nothing else, the
# source trees differ in the files this checkout's patches, stage scripts,
# driver and early helper change and those are what they give (replayed on
# the release tree's files in the build's order), the new image has the new
# code and the old one does not and was built after the last change to
# those files, the compiled power domain table differs in four bytes, the
# UFS PHY's calibration in the new vmlinux no longer asks which board it
# runs on, the PIPE clock mux has its gate operations, the geni UART's sleep
# callbacks force-suspend the port, the debug switches are there, and the
# new boot image differs only in its kernel.  Nothing is flashed.
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
# This kit's revision: the Type-C changes (1 to 7) and the deep-sleep
# groundwork (8).  Written to the kit for people; install.sh knows a
# revision by its boot image.
revision=8
dts=sm8550-samsung-gts9uwifi
tcpm_dir=drivers/usb/typec/tcpm
gcc=drivers/clk/qcom/gcc-sm8550
dwc3=drivers/usb/dwc3/dwc3-qcom
qmp=drivers/phy/qualcomm/phy-qcom-qmp-ufs
# Revision 8's files, as the SM-X710 kit names them.
ufshcd=drivers/ufs/core/ufshcd
rpmhpd=drivers/pmdomain/qcom/rpmhpd
phymux=drivers/clk/qcom/clk-regmap-phy-mux
qmp_pcie=drivers/phy/qualcomm/phy-qcom-qmp-pcie
gmu=drivers/gpu/drm/msm/adreno/a6xx_gmu
ufs_qcom=drivers/ufs/host/ufs-qcom
pcie_qcom=drivers/pci/controller/dwc/pcie-qcom
qcom_stats=drivers/soc/qcom/qcom_stats
va_macro=sound/soc/codecs/lpass-va-macro
dsi_phy=drivers/gpu/drm/msm/dsi/phy/dsi_phy
geni=drivers/tty/serial/qcom_geni_serial
# The early helper and the header scripts/stage-performance-overlay.sh
# generates next to it from this checkout's overlays, which it compiles into
# drivers/soc/qcom as well.
helper=drivers/soc/qcom/gts9u-performance
overlays=(gts9u-performance gts9u-sync-state gts9u-ramoops gts9-performance gts9-family)
# Those of the overlays the release build did not have: their blobs are in
# the new tree alone.
new_overlays=(gts9u-sync-state gts9u-ramoops)
# The USB-C dock changes (revisions 1 to 7), each applied to the release
# tree's file as it is.
patches=(tcpm-request-dfp-as-sink-before-discovery.patch tcpm-try-snk-extra-wait.patch
	tcpm-no-dr-swap-during-vdm.patch tcpm-dr-set-checks-data-role.patch
	tcpm-keep-queued-swap-pending.patch gcc-sm8550-usb-gdscs-retention.patch
	dwc3-qcom-check-hcd-before-reading-port-speed.patch)
# Revision 8: the UFS and USB CX votes dropped in system sleep, which every
# build applies on both boards since gcc is under CX here too.
cx_patches=(ufshcd-drop-opp-votes-in-system-suspend.patch
	dwc3-qcom-drop-required-opps-vote-in-system-suspend.patch)
# The SM-X710 kit's: pcie_0_gdsc collapsed with its guard's genpd flag, the
# PCIe host's half of the guard and the root port's debug switch, the three
# debug switch patches, the three SM8550 sleep fixes (with
# unpark-pcie0-pipe-mux.patch left out: the release tree has it), the UFS
# PHY's two diagnostics on top of the PCS reset, ufs-qcom's power reference
# fix (every build) and its diagnostic, and the three XO fixes.
gdsc_patches=(gcc-sm8550-pcie0-gdsc-off-in-suspend.patch)
pcie0_line='PWRSTS_OFF_ON, /* gcc-sm8550-pcie0-gdsc-off-in-suspend.patch */'
guard_line='		.flags = GENPD_FLAG_ACTIVE_WAKEUP,'
pcie_patches=(pcie-qcom-keep-power-domain-while-link-kept.patch pcie-qcom-debug-root-port-d3.patch)
pcie_lines=('qcom_pcie_mark_sleep_path(dev, pcie->link_kept);'
	'PCIe root port to D3 for a sleep test, not for release builds')
debug_patches=(gcc-sm8550-gdsc-debug-switches.patch rpmhpd-debug-retention-floor.patch
	qcom-stats-cxpc-violators-diagnostic.patch)
debug_files=("$gcc.c" "$rpmhpd.c" "$qcom_stats.c")
fix_patches=(clk-qcom-regmap-phy-mux-revert-rework.patch msm-adreno-a6xx-fix-stale-rpmh-votes.patch
	ufs-qcom-enable-only-lane-clocks.patch)
fix_files=("$phymux.c" "$gmu.c" "$ufs_qcom.c")
unpark=unpark-pcie0-pipe-mux.patch
ufs_patches=(qmp-ufs-pcs-state-diagnostic.patch qmp-ufs-pwrdn-readback-diagnostic.patch)
ufs_qcom_patches=(ufs-qcom-track-phy-power-state-gts9u.patch ufs-qcom-phy-power-count-diagnostic.patch)
xo_patches=(lpass-va-macro-pm-clock-framework.patch msm-dsi-phy-release-ahb-clock-in-system-sleep.patch
	qcom-geni-serial-force-suspend-in-system-sleep.patch)
xo_files=("$va_macro.c sound/soc/codecs/Kconfig" "$dsi_phy.c $dsi_phy.h" "$geni.c")
# Every patch above, in the order scripts/build-mainline-kernel.sh applies
# it (the tree comparison replays them so), and the files they change.
build_order=("${cx_patches[@]}" "${ufs_qcom_patches[@]}" gcc-sm8550-usb-gdscs-retention.patch
	"${gdsc_patches[@]}" "${pcie_patches[0]}" "${debug_patches[@]}" "${pcie_patches[1]}"
	dwc3-qcom-check-hcd-before-reading-port-speed.patch qcom-geni-serial-force-suspend-in-system-sleep.patch
	lpass-va-macro-pm-clock-framework.patch msm-dsi-phy-release-ahb-clock-in-system-sleep.patch
	"${fix_patches[@]}" tcpm-request-dfp-as-sink-before-discovery.patch tcpm-try-snk-extra-wait.patch
	tcpm-no-dr-swap-during-vdm.patch tcpm-dr-set-checks-data-role.patch tcpm-keep-queued-swap-pending.patch)
patched=("$tcpm_dir/tcpm.c" include/linux/usb/tcpm.h "$gcc.c" "$dwc3.c" "$ufshcd.c" "$rpmhpd.c"
	"$phymux.c" "$qmp_pcie.c" "$gmu.c" "$ufs_qcom.c" "$ufs_qcom.h" "$pcie_qcom.c" "$qcom_stats.c"
	"$va_macro.c" sound/soc/codecs/Kconfig "$dsi_phy.c" "$dsi_phy.h" "$geni.c")
# The UFS PHY driver: the release tree has the PCS reset in its SM-X910-only
# form, which scripts/stage-ufs-resume-experiment.sh replaces with this
# checkout's patch; the two diagnostics go on top.
pcs_patch=qmp-ufs-assert-pcs-reset-before-calibration-gts9u.patch
pcs_legacy_marker='SM-X910 experiment: assert PCS reset before calibration tables'
qmp_diag_marker='Diagnostic for SM8550 resume tests'
# The configuration: the release's with these four lines changed
# (kernel/config/config-gts9uwifi.fragment, 2026-11-25), each "SYMBOL
# RELEASE NEW", tab-separated, "n" for not set, and nothing kconfig derives
# from them (an olddefconfig of this checkout gives exactly these).
config_changes=('CONFIG_PSTORE_COMPRESS	y	n' 'CONFIG_PSTORE_FTRACE	n	y' 'CONFIG_QCOM_STATS	m	y'
	'CONFIG_SM_VIDEOCC_8550	m	y')

# The tablet's current set: release v1.2.5 (out/bundle of 2026-11-10).
base_boot=d09cb64ff411ca39feab17c1c4330081619df9304b6a5b974bf23ccab915bdac
base_vendor_boot=f3e8c6371d0a943a53d07787fa3e87c234967bcbee41210471f6cf46c0ee573d
base_image=5bb0cd52e5b868e25501765857f32924025883a87b7268ae972d3374a5b0177f
base_dtb=613b3bb7729d55d1c60aaeda348a098163b79aed1efbf24cdcc582ff0d58ccc4
base_config=a5648666cc6973ca2e78b3c47c8c660e03b0441defae69419b0618b194e1f1c7
base_build_id=3447147dc203542e441eebfa4d62e60c57100f50

# What only the new kernel may contain: the patch's log line, the driver's
# two parameters, and the deadline of the Try.SNK wait, a member of tcpm_port
# whose name is in the image's type information; that one tells the wait
# with a deadline from the plain longer timeout before it.  Then what the
# revisions after it add: the log lines of the three fixes for a swap asked
# for from userspace (the third has three; its PR_Swap line is in no earlier
# revision of that patch), and policy_open, a member of the driver's
# structure, which only a driver that reads its parameters at each attach
# has.  Then revision 8 (deep_strings): the early helper's three lines (L15B
# held in HPM, the SoC hand-off, ramoops), the CX-vote patches' messages,
# the video clock controller's and the sleep statistics' debug switch names
# (both built in now), and the strings the SM-X710 kit's make-kit.sh
# requires for the same switches (its revisions 8 to 13), every one of them
# absent from the release image.
new_strings=('Requesting DFP before discovery' sink_host_swap try_snk_extra_wait_ms try_snk_wait_until
	'VDM in progress, DATA_ROLE_SWAP not started' 'Data role swap ended with the role unchanged'
	'Swap is still queued, left pending' 'Partner swapped first, queued DR_SWAP_SEND dropped'
	'Partner swapped first, queued PR_SWAP_SEND dropped'
	policy_open)
deep_strings=('gts9u-performance: L15B held in HPM'
	'gts9u-performance: SoC hand-off: gcc under CX, GMU post-init providers, crypto engine disabled'
	'gts9u-performance: ramoops in the pmsg carve-out at 0x880900000'
	'failed to drop the OPP votes' 'failed to restore the OPP votes' 'failed to set performance state %u: %d'
	video_cc-sm8550
	'calibration entry' 'after the PCS_READY timeout' 'no PCS_READY within 1 s of SERDES_START'
	gcc_sm8550.debug_pcie0_off gcc_sm8550.debug_pcie0_phy_off gcc_sm8550.debug_usb_off
	rpmhpd.debug_ret_floor
	'failed to get tx_lane0_sync_clk' 'failed to get rx_lane0_sync_clk' 'failed to get rx_lane1_sync_clk'
	'power-up write: PWRDN'
	gcc_sm8550.debug_boot_state
	'system sleep: the link was kept, and its power domain stayed on'
	'system sleep: the link was kept, but genpd powered its power domain off'
	pcie_qcom.debug_root_port_d3
	'Failed to power on PHY: %pe' 'power-up sequence entry' 'setup_clocks repeats: power-on'
	qcom_stats.debug_cxpc_violators "the AOP's CXPC monitor is on for this suspend"
	'runtime PM put failed in fsgen disable' 'Unable to prepare iface clk'
	'cxpc violators: raw %s +0x%03x: ' cxpc_violators_raw)
# The objects of the changed sources.  Each has to be newer than what it was
# compiled from and older than the vmlinux it was linked into.
objects=("$gcc.o:$gcc.c" "$tcpm_dir/tcpm.o:$tcpm_dir/tcpm.c" "$tcpm_dir/tcpm.o:include/linux/usb/tcpm.h"
	"$tcpm_dir/sm5714_usbpd.o:$tcpm_dir/sm5714_usbpd.c"
	"$tcpm_dir/sm5714_usbpd.o:include/linux/usb/tcpm.h" "$dwc3.o:$dwc3.c" "$qmp.o:$qmp.c"
	"$ufshcd.o:$ufshcd.c" "$rpmhpd.o:$rpmhpd.c" "$phymux.o:$phymux.c" "$qmp_pcie.o:$qmp_pcie.c"
	"$gmu.o:$gmu.c" "$ufs_qcom.o:$ufs_qcom.c" "$ufs_qcom.o:$ufs_qcom.h" "$pcie_qcom.o:$pcie_qcom.c"
	"$qcom_stats.o:$qcom_stats.c" "$va_macro.o:$va_macro.c" "$dsi_phy.o:$dsi_phy.c" "$dsi_phy.o:$dsi_phy.h"
	"$geni.o:$geni.c" "$helper.o:$helper.c" "$helper.o:$helper-overlay.h")

sha() { sha256sum "$1" | cut -d' ' -f1; }
die() { echo "$*" >&2; exit 1; }
for f in "$mkbootimg" "$avbtool" "$out/Image.gz" "$out/$dts.dtb" "$out/config" \
	"$out/fingerprint.layers" "$base/fingerprint.layers" \
	"$base/Image.gz" "$base/$dts.dtb" "$base/config" "$bundle/boot.img" \
	"$bundle/vendor_boot.img" "${patched[@]/#/$tree/}" \
	"$build/.config" "$build/certs/signing_key.x509" "$build/vmlinux" \
	"$build/$gcc.o" "$build/$tcpm_dir/tcpm.o" "$build/$tcpm_dir/sm5714_usbpd.o" "$build/$dwc3.o" \
	"$build/$ufshcd.o" "$build/$rpmhpd.o" "$build/$phymux.o" "$build/$qmp_pcie.o" "$build/$gmu.o" \
	"$build/$ufs_qcom.o" "$build/$pcie_qcom.o" "$build/$qcom_stats.o" "$build/$va_macro.o" \
	"$build/$dsi_phy.o" "$build/$geni.o" "$build/$helper.o" "$tree/$helper.c" "$tree/$helper-overlay.h" \
	"$tree/$qmp.c" "$build/$qmp.o" "$tree/$tcpm_dir/sm5714_usbpd.c" "$build/arch/arm64/boot/Image.gz" \
	"$repo/kernel/drivers/sm5714_usbpd.c" "$repo/kernel/drivers/gts9u-performance.c" \
	"${patches[@]/#/$repo/kernel/patches/}" "${cx_patches[@]/#/$repo/kernel/patches/}" \
	"${build_order[@]/#/$repo/kernel/patches/}" "${ufs_patches[@]/#/$repo/kernel/patches/}" \
	"$repo/kernel/patches/$unpark" "$repo/kernel/patches/$pcs_patch" \
	"$repo/scripts/stage-ufs-resume-experiment.sh" "$repo/scripts/stage-performance-overlay.sh" \
	"$repo/tools/inplace/typec-roles/sleep-test.sh" "$repo/tools/inplace/typec-roles/ufs-phy-power-check.sh"; do
	[ -f "$f" ] || die "missing: $f"
done
for t in unpack_bootimg python3 sha256sum cmp diff patch tar modinfo xxd openssl git llvm-objcopy llvm-objdump llvm-nm pahole dtc; do
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

# 2. The DTB and the fingerprint layers: the release build's, unchanged.
# The configuration: the release's with config_changes, and nothing else.
cmp -s "$out/$dts.dtb" "$base/$dts.dtb" ||
	die "the DTB changed ($(sha "$out/$dts.dtb")); this kit does not write vendor_boot"
conf() { # CONFIG -> "SYMBOL<tab>VALUE" lines, "n" for "is not set"
	sed -n -e 's/^\(CONFIG_[A-Za-z0-9_]*\)=\(.*\)$/\1\t\2/p' \
		-e 's/^# \(CONFIG_[A-Za-z0-9_]*\) is not set$/\1\tn/p' "$1" | sort
}
join -t "$(printf '\t')" -a1 -a2 -e n -o 0,1.2,2.2 <(conf "$base/config") <(conf "$out/config") |
	awk -F '\t' '$2 != $3' > "$tmp/config.diff"
printf '%s\n' "${config_changes[@]}" | sort > "$tmp/config.want"
cmp -s "$tmp/config.diff" "$tmp/config.want" || {
	echo 'the configuration does not differ from the release kernel'"'"'s in config_changes alone (SYMBOL, release, new):' >&2
	cat "$tmp/config.diff" >&2
	exit 1
}
[ "$("$tree/scripts/extract-ikconfig" "$out/Image.gz" | sha256sum | cut -d' ' -f1)" = "$(sha "$out/config")" ] ||
	die 'the configuration embedded in Image.gz is not out/config'
cmp -s "$out/fingerprint.layers" "$base/fingerprint.layers" ||
	die "fingerprint.layers differs from the release build's: $(tr '\n' ' ' < "$out/fingerprint.layers")"
config_check="the release's (${base_config:0:12}) with ${#config_changes[@]} lines changed: $(printf '%s; ' "${config_changes[@]}" | tr '\t' ' ' | sed 's/; $//')"
echo "DTB (${base_dtb:0:12}) and fingerprint layers: the release build's, byte for byte; configuration: $config_check"

# 3. The sources.  The driver and the early helper are this checkout's; the
# patched files are the release tree's with this checkout's patches on top,
# the UFS PHY driver the release tree's with its SM-X910-only PCS reset
# replaced by the one for every SM8550 PHY and the two diagnostics on top,
# and the helper's generated header and overlay blobs what this checkout's
# stage script makes; nothing else in the kernel tree differs.
cmp -s "$tree/$tcpm_dir/sm5714_usbpd.c" "$repo/kernel/drivers/sm5714_usbpd.c" ||
	die "the build tree's sm5714_usbpd.c is not this checkout's"
grep -q 'request_dfp_as_sink' "$tree/include/linux/usb/tcpm.h" &&
	grep -q 'try_snk_extra_wait_ms' "$tree/include/linux/usb/tcpm.h" &&
	grep -q 'tcpm_try_snk_wait_left' "$tree/$tcpm_dir/tcpm.c" &&
	grep -q 'Requesting DFP before discovery' "$tree/$tcpm_dir/tcpm.c" &&
	grep -q 'VDM in progress, DATA_ROLE_SWAP not started' "$tree/$tcpm_dir/tcpm.c" &&
	grep -q 'Data role swap ended with the role unchanged' "$tree/$tcpm_dir/tcpm.c" &&
	grep -q 'Swap is still queued, left pending' "$tree/$tcpm_dir/tcpm.c" &&
	grep -q 'Partner swapped first, queued DR_SWAP_SEND dropped' "$tree/$tcpm_dir/tcpm.c" &&
	grep -q 'Partner swapped first, queued PR_SWAP_SEND dropped' "$tree/$tcpm_dir/tcpm.c" ||
	die 'the build tree does not carry the five TCPM patches'
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
# pcie_0_gdsc PWRSTS_OFF_ON by the line of its patch, and the guard's
# GENPD_FLAG_ACTIVE_WAKEUP in its entry and nowhere else in the file, the
# patch whole in the build tree's file (it comes out again with no fuzz),
# and its PHY's domain and the PCIe1 ones as upstream has them.
[ "$(gdsc_pwrsts "$tree/$gcc.c" pcie_0_gdsc)" = 'PWRSTS_OFF_ON,' ] && grep -qF -- "$pcie0_line" "$tree/$gcc.c" ||
	die "the build tree does not carry ${gdsc_patches[0]} (pcie_0_gdsc)"
awk -v head='static struct gdsc pcie_0_gdsc = {' -v line="$guard_line" '
	$0 == head { inside = 1; next }
	inside && /^};/ { exit }
	inside && $0 == line { found = 1 }
	END { exit !found }' "$tree/$gcc.c" && [ "$(grep -cF -- GENPD_FLAG_ACTIVE_WAKEUP "$tree/$gcc.c")" = 1 ] ||
	die "the build tree does not carry ${gdsc_patches[0]} whole (GENPD_FLAG_ACTIVE_WAKEUP for pcie_0_gdsc, and nowhere else)"
for d in pcie_0_phy_gdsc pcie_1_gdsc pcie_1_phy_gdsc; do
	[ "$(gdsc_pwrsts "$tree/$gcc.c" "$d")" = 'PWRSTS_RET_ON,' ] ||
		die "the build tree's $d is not PWRSTS_RET_ON"
done
# The guard adds no string to the image; its comment is the marker.  The
# CX-vote patch under it adds the messages deep_strings checks.
grep -q 'no hcd and no root hub' "$tree/$dwc3.c" ||
	die 'the build tree does not carry the dwc3-qcom guard'
grep -q 'dwc3_qcom_set_pstate' "$tree/$dwc3.c" && grep -q 'ufshcd_opp_drop_votes' "$tree/$ufshcd.c" ||
	die 'the build tree does not carry the UFS and USB CX-vote patches'
# The PCIe host driver: the guard's half and the root port's debug switch,
# each known by its line.
for i in "${!pcie_patches[@]}"; do
	grep -qF -- "${pcie_lines[$i]}" "$tree/$pcie_qcom.c" ||
		die "the build tree does not carry ${pcie_patches[$i]}"
done
# The UFS PHY: the PCS reset and the two diagnostics are in the build tree's
# file, each whole (they come out again with no fuzz, the last first), and
# the SM-X910-only form of the PCS reset is not.  --force, not --batch: given
# a patch that is not in the tree but would go in, patch --reverse --batch
# applies it forward.
mkdir -p "$tmp/ufs/${qmp%/*}"
cp "$tree/$qmp.c" "$tmp/ufs/$qmp.c"
for p in qmp-ufs-pwrdn-readback-diagnostic.patch qmp-ufs-pcs-state-diagnostic.patch "$pcs_patch"; do
	patch -s --reverse --force --fuzz=0 --no-backup-if-mismatch -d "$tmp/ufs" -p1 \
		< "$repo/kernel/patches/$p" ||
		die "the build tree's $qmp.c does not carry $p whole"
done
! grep -q -e "$pcs_legacy_marker" -e "$qmp_diag_marker" "$tmp/ufs/$qmp.c" ||
	die "the build tree's $qmp.c carries the SM-X910-only PCS reset, or a diagnostic beside the two"
# The early helper: this checkout's file, and the header and blobs this
# checkout's stage script makes of its overlays (run on an empty tree here).
cmp -s "$tree/$helper.c" "$repo/kernel/drivers/gts9u-performance.c" ||
	die "the build tree's $helper.c is not this checkout's"
mkdir -p "$tmp/stage/drivers/soc/qcom"
: > "$tmp/stage/drivers/soc/qcom/Makefile"
bash "$repo/scripts/stage-performance-overlay.sh" "$tmp/stage" > "$tmp/stage.log" ||
	die 'scripts/stage-performance-overlay.sh does not run'
for f in "$helper-overlay.h" "${overlays[@]/#/drivers/soc/qcom/}"; do
	case $f in *-overlay.h) ;; *) f=$f.dtbo ;; esac
	cmp -s "$tmp/stage/$f" "$tree/$f" ||
		die "the build tree's $f is not what this checkout's stage script makes"
done
grep -q 'gts9u-performance: L15B held in HPM' "$tree/$helper.c" &&
	grep -q 'gts9u_sync_state_overlay' "$tree/$helper.c" && grep -q 'gts9u_ramoops_overlay' "$tree/$helper.c" ||
	die "the build tree's $helper.c has no L15B step, or does not apply the SoC hand-off and ramoops overlays"
source_check='marker lines only (BASE_SCRATCH not set)'
if [ -n "${BASE_SCRATCH:-}" ]; then
	base_tree=$BASE_SCRATCH/linux-src-gts9uwifi
	for f in "${patched[@]}"; do
		[ -f "$base_tree/$f" ] || die "no kernel source tree in $BASE_SCRATCH"
		mkdir -p "$tmp/src/${f%/*}"
		cp "$base_tree/$f" "$tmp/src/$f"
	done
	[ -f "$base_tree/$helper.c" ] || die "no $helper.c in $base_tree"
	# The release tree had none of what revision 8 adds, and the unpark
	# patch, as every default build before 2026-11-23 did.
	! grep -q -e 'ufshcd_opp_drop_votes' "$base_tree/$ufshcd.c" &&
		! grep -q 'dwc3_qcom_set_pstate' "$base_tree/$dwc3.c" &&
		! grep -q 'SM8550 deep-sleep debug, not for release builds' "$base_tree/$gcc.c" "$base_tree/$rpmhpd.c" \
			"$base_tree/$qcom_stats.c" &&
		! grep -qF -- "$pcie0_line" "$base_tree/$gcc.c" &&
		! grep -qF -e "${pcie_lines[0]}" -e "${pcie_lines[1]}" "$base_tree/$pcie_qcom.c" &&
		! grep -qF -e 'Power the PHY off if this host holds its power' "$base_tree/$ufs_qcom.c" &&
		! grep -q 'tx_lane0_sync_clk' "$base_tree/$ufs_qcom.c" &&
		! grep -qF 'return pm_runtime_force_suspend(dev);' "$base_tree/$geni.c" &&
		! grep -q 'L15B' "$base_tree/$helper.c" ||
		die "the release tree carries part of revision 8's changes already"
	grep -q 'clk_set_rate(qmp->pipe_clks\[0\].clk, ULONG_MAX)' "$base_tree/$qmp_pcie.c" ||
		die "the release tree's $qmp_pcie.c does not carry $unpark"
	# The UFS PHY driver as the build changes it: the release build gave the
	# PCS reset to the SM-X910 alone, and this checkout's stage script
	# replaces that form.
	[ -f "$base_tree/$qmp.c" ] || die "no $qmp.c in $base_tree"
	grep -q "$pcs_legacy_marker" "$base_tree/$qmp.c" &&
		! grep -q "$qmp_diag_marker" "$base_tree/$qmp.c" ||
		die "the release tree's $qmp.c is not the one with the SM-X910-only PCS reset"
	mkdir -p "$tmp/src/${qmp%/*}"
	cp "$base_tree/$qmp.c" "$tmp/src/$qmp.c"
	UFS_PCS_RESET_EXPERIMENTAL=1 bash "$repo/scripts/stage-ufs-resume-experiment.sh" "$tmp/src" \
		> "$tmp/stage-ufs.log" ||
		die "scripts/stage-ufs-resume-experiment.sh does not take the release tree's $qmp.c"
	for p in "${ufs_patches[@]}"; do
		patch -s --forward --batch --fuzz=0 --no-backup-if-mismatch -d "$tmp/src" -p1 \
			< "$repo/kernel/patches/$p" ||
			die "$p does not apply to the release tree's $qmp.c"
	done
	cmp -s "$tmp/src/$qmp.c" "$tree/$qmp.c" ||
		die "$qmp.c is not the release tree's with this checkout's stage script and diagnostics on it"
	# Every patch, in the build's order, as patch(1) takes it there: the
	# unpark patch out first.  Where the build's patch(1) puts a hunk in at
	# an offset it keeps the file it started from as FILE.orig; the files it
	# keeps so are recorded here, as they were right before that patch.
	patch -s --reverse --force --fuzz=0 --no-backup-if-mismatch -d "$tmp/src" -p1 \
		< "$repo/kernel/patches/$unpark" ||
		die "$unpark does not come out of the release tree's $qmp_pcie.c whole"
	for p in "${build_order[@]}"; do
		case $p in
		ufs-qcom-track-phy-power-state-gts9u.patch)
			mkdir -p "$tmp/orig/${ufs_qcom%/*}"
			cp "$tmp/src/$ufs_qcom.c" "$tmp/src/$ufs_qcom.h" "$tmp/orig/${ufs_qcom%/*}/" ;;
		dwc3-qcom-check-hcd-before-reading-port-speed.patch)
			mkdir -p "$tmp/orig/${dwc3%/*}"
			cp "$tmp/src/$dwc3.c" "$tmp/orig/$dwc3.c" ;;
		esac
		patch -s --forward --batch --no-backup-if-mismatch -d "$tmp/src" -p1 < "$repo/kernel/patches/$p" ||
			die "$p does not apply to the release tree's files in the build's order"
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
	# patch(1) keeps the file it started from as FILE.orig when a hunk goes
	# in at an offset, and scripts/build-mainline-kernel.sh does not tell it
	# not to: ufs-qcom's power reference fix goes in 31 lines up in ufs-qcom.c
	# and one in ufs-qcom.h (its line numbers are those of the lane clock
	# fix's files), and the dwc3-qcom guard four lines down, after the CX-vote
	# patch.  Such a backup is in the new tree alone.  It is passed over when
	# it is what patch(1) kept there (the file recorded right before that
	# patch in the replay above, or else the release tree's), and shown like
	# any other file when it is anything else.
	backups=
	for f in "${patched[@]}"; do
		kept=$base_tree/$f
		[ ! -f "$tmp/orig/$f" ] || kept=$tmp/orig/$f
		[ -f "$tree/$f.orig" ] && [ ! -e "$base_tree/$f.orig" ] &&
			cmp -s "$tree/$f.orig" "$kept" || continue
		grep -vxF "Only in $tree/${f%/*}: ${f##*/}.orig" "$tmp/trees.diff" > "$tmp/trees.rest" || :
		mv "$tmp/trees.rest" "$tmp/trees.diff"
		backups="$backups ${f##*/}.orig"
	done
	# The new overlays' blobs, which the stage script compiled into the
	# build tree (compared above), are in the new tree alone.
	for o in "${new_overlays[@]}"; do
		grep -vxF "Only in $tree/drivers/soc/qcom: $o.dtbo" "$tmp/trees.diff" > "$tmp/trees.rest" || :
		mv "$tmp/trees.rest" "$tmp/trees.diff"
	done
	sed -e "s|^Files $base_tree/\(.*\) and .* differ\$|\1|" "$tmp/trees.diff" | sort > "$tmp/trees.changed"
	sort > "$tmp/trees.want" <<EOF
$gcc.c
$dwc3.c
$qmp.c
$ufshcd.c
$rpmhpd.c
$phymux.c
$qmp_pcie.c
$gmu.c
$ufs_qcom.c
$ufs_qcom.h
$pcie_qcom.c
$qcom_stats.c
$va_macro.c
sound/soc/codecs/Kconfig
$dsi_phy.c
$dsi_phy.h
$geni.c
$helper.c
$helper-overlay.h
$tcpm_dir/sm5714_usbpd.c
$tcpm_dir/tcpm.c
include/linux/usb/tcpm.h
EOF
	cmp -s "$tmp/trees.changed" "$tmp/trees.want" || {
		echo 'the kernel source trees differ in more, or other, than the files of this checkout'"'"'s patches, stage scripts, driver and early helper:' >&2
		cat "$tmp/trees.diff" >&2
		exit 1
	}
	source_check="compared with $base_tree: twenty-two files differ and two overlay blobs are new, each by exactly this checkout's patches (replayed on the release tree's files in the build's order, $unpark taken out), stage scripts (the UFS PHY's PCS reset, with its two diagnostics; the early helper's header and blobs), driver and early helper"
	echo 'sources: only the files of this checkout'"'"'s patches, stage scripts, driver and early helper differ from the release tree, by exactly those'
	if [ -n "$backups" ]; then
		source_check="$source_check; passed over, a copy of the file that patch(1) kept:$backups"
		echo "sources: passed over$backups, the file as patch(1) kept it"
	fi
else
	echo 'sources: the patches, the PCS reset and its diagnostics, this checkout'"'"'s driver and early helper are in the build tree (BASE_SCRATCH not set: no tree comparison)'
fi

# 4. The images: the new code is in the new one and not in the old one.
# They are unpacked to files first: with pipefail, a grep -q that stops at
# its first match fails the zcat feeding it, and the match with it.
zcat "$out/Image.gz" > "$tmp/new.Image"
zcat "$base/Image.gz" > "$tmp/base.Image"
for s in "${new_strings[@]}" "${deep_strings[@]}"; do
	grep -qaF -- "$s" "$tmp/new.Image" || die "the new kernel image does not contain '$s'"
	! grep -qaF -- "$s" "$tmp/base.Image" || die "self-test failed: the release image contains '$s' too"
done
echo "image: $((${#new_strings[@]} + ${#deep_strings[@]})) strings of the change are in the new kernel and not in the release one"
# The image is the build directory's, made from its vmlinux, and that from
# objects compiled after the last change to their sources.
for pair in "${objects[@]}"; do
	o=$build/${pair%%:*}
	[ "$o" -nt "$tree/${pair#*:}" ] && [ "$build/vmlinux" -nt "$o" ] ||
		die "${pair%%:*} is older than ${pair#*:}, or newer than vmlinux"
done
[ "$build/arch/arm64/boot/Image.gz" -nt "$build/vmlinux" ] || die 'Image.gz is older than vmlinux'
cmp -s "$build/arch/arm64/boot/Image.gz" "$out/Image.gz" || die "$out/Image.gz is not the build directory's"
# The power domain changes are four numbers in a table and leave no string.
# The table was compiled from the patched source and linked into this image
# (the check above), and against the release build's it differs in exactly
# those four bytes: the two USB domains' 5 (PWRSTS_OFF_ON) became 6
# (PWRSTS_RET_ON), pcie_0_gdsc's 6 became 5, and pcie_0_gdsc's genpd flags 0
# became 8 (GENPD_FLAG_ACTIVE_WAKEUP, the guard's; little endian, so the low
# byte).  Each changed byte is named by the object of the new build that
# holds it (its symbol table's .data entries: in an object file a symbol's
# value is its offset in its section), and has to be the member it is:
# .pwrsts of struct gdsc for the three, .pd.flags for the fourth, at the
# offsets the object's own debug information gives them (pahole).  The
# debug switches write nothing until one is set: they change no byte of it.
member_offset() { # OBJECT STRUCT MEMBER: the member's offset in the struct, or nothing
	{ pahole -C "$2" "$1" 2>/dev/null || :; } | awk -v m="$3" '
		{ for (i = 1; i + 2 <= NF; i++) if ($i == m ";" && $(i + 1) == "/*") { print $(i + 2); exit } }'
}
gdsc_table_check() { # BASE_OBJECT NEW_OBJECT: 0, or 1 with what differs on stderr
	llvm-objcopy -O binary --only-section=.data "$1" "$tmp/gcc.base.data"
	llvm-objcopy -O binary --only-section=.data "$2" "$tmp/gcc.new.data"
	[ -s "$tmp/gcc.new.data" ] || { echo "no .data in $2" >&2; return 1; }
	cmp -l "$tmp/gcc.base.data" "$tmp/gcc.new.data" > "$tmp/gcc.data.diff" || :
	llvm-objdump -t "$2" > "$tmp/gcc.new.syms"
	# OFFSET (1-based, cmp's) OLD NEW (octal, cmp's) -> SYMBOL OFFSET-IN-IT OLD NEW
	awk '
		function hex(s,   i, c, v) {
			v = 0
			for (i = 1; i <= length(s); i++) {
				c = index("0123456789abcdef", tolower(substr(s, i, 1)))
				if (!c) return -1
				v = v * 16 + c - 1
			}
			return v
		}
		FILENAME == ARGV[1] {
			for (i = 2; i < NF - 1; i++)
				if ($i == ".data") { n++; at[n] = hex($1); size[n] = hex($(i + 1)); name[n] = $NF }
			next
		}
		{
			o = $1 - 1
			s = "?"
			for (j = 1; j <= n; j++)
				if (o >= at[j] && o < at[j] + size[j]) { s = name[j] " " o - at[j]; break }
			print s, $2, $3
		}' "$tmp/gcc.new.syms" "$tmp/gcc.data.diff" | sort > "$tmp/gcc.data.named"
	pwrsts_at=$(member_offset "$2" gdsc pwrsts)
	pd_at=$(member_offset "$2" gdsc pd)
	flags_in=$(member_offset "$2" generic_pm_domain flags)
	case $pwrsts_at:$pd_at:$flags_in in
	*[!0-9:]*|:*|*::*|*:) echo "pahole gives no offsets of struct gdsc's pwrsts and pd and of struct generic_pm_domain's flags in $2" >&2; return 1 ;;
	esac
	flags_at=$((pd_at + flags_in))
	printf '%s\n' "pcie_0_gdsc $flags_at 0 10" "pcie_0_gdsc $pwrsts_at 6 5" "usb30_prim_gdsc $pwrsts_at 5 6" \
		"usb3_phy_gdsc $pwrsts_at 5 6" | sort > "$tmp/gcc.data.want"
	if cmp -s "$tmp/gcc.data.named" "$tmp/gcc.data.want"; then
		return 0
	fi
	echo "the compiled $gcc table does not differ from the release build's in the four bytes alone (pcie_0_gdsc's .pd.flags at $flags_at 0 to 8, and the .pwrsts at $pwrsts_at of pcie_0_gdsc 6 to 5 and of the two USB domains 5 to 6; symbol, byte in it, old, new, octal):" >&2
	cat "$tmp/gcc.data.named" >&2
	return 1
}
gdsc_check='the object is newer than the patched source and older than vmlinux (BASE_SCRATCH not set)'
if [ -n "${BASE_SCRATCH:-}" ]; then
	[ -f "$BASE_SCRATCH/linux-gts9uwifi/$gcc.o" ] || die "no $gcc.o in $BASE_SCRATCH"
	gdsc_table_check "$BASE_SCRATCH/linux-gts9uwifi/$gcc.o" "$build/$gcc.o" || exit 1
	gdsc_check="the compiled table differs from the release build's in four bytes, pcie_0_gdsc's .pwrsts 6 to 5 and the two USB domains' 5 to 6 (.pwrsts at $pwrsts_at in each), and pcie_0_gdsc's .pd.flags 0 to 8 (GENPD_FLAG_ACTIVE_WAKEUP, at $flags_at)"
	# The RPMh power domain driver's compiled tables: the release build's,
	# byte for byte (its debug switch writes nothing until it is set).
	[ -f "$BASE_SCRATCH/linux-gts9uwifi/$rpmhpd.o" ] || die "no $rpmhpd.o in $BASE_SCRATCH"
	llvm-objcopy -O binary --only-section=.data "$BASE_SCRATCH/linux-gts9uwifi/$rpmhpd.o" "$tmp/rpmhpd.base.data"
	llvm-objcopy -O binary --only-section=.data "$build/$rpmhpd.o" "$tmp/rpmhpd.new.data"
	[ -s "$tmp/rpmhpd.new.data" ] && cmp -s "$tmp/rpmhpd.base.data" "$tmp/rpmhpd.new.data" ||
		die "the compiled $rpmhpd tables are not the release build's"
	gdsc_check="$gdsc_check; the RPMh power domain driver's are the release build's"
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

# 5b. The UFS PHY's board gate, which the calibration may no longer have.
# The release's asked of_machine_is_compatible("samsung,gts9uwifi"), an
# inline that calls of_machine_compatible_match(), before the PCS reset; it
# was true here, so the reset ran, and now it goes to every SM8550 PHY.  No
# string in the image tells the two apart ("samsung,gts9uwifi" is there for
# other code), so vmlinux is disassembled: the ELF the image was made from,
# which its build ID ties to the image.  Its qmp_ufs_phy_calibrate() must
# call reset_control_assert(), which shows the function was found, and must
# not call of_machine_compatible_match().  With BASE_SCRATCH the release
# build's vmlinux, whose calibration has the gate, must show that call.
notes_build_id() { # VMLINUX: the build ID of its .notes
	llvm-objcopy -O binary --only-section=.notes "$1" "$tmp/notes.bin" &&
		build_id "$tmp/notes.bin"
}
calibrate_calls() { # VMLINUX: what qmp_ufs_phy_calibrate() calls, one per line
	llvm-objdump -d --no-show-raw-insn --disassemble-symbols=qmp_ufs_phy_calibrate "$1" |
		sed -n 's/.*[[:space:]]bl[[:space:]].*<\([A-Za-z0-9_.]*\)>$/\1/p' | sort -u
}
[ "$(notes_build_id "$build/vmlinux")" = "$new_id" ] ||
	die "$build/vmlinux is not the ELF of the new image (another build ID)"
calibrate_calls "$build/vmlinux" > "$tmp/calibrate.calls"
grep -qx reset_control_assert "$tmp/calibrate.calls" ||
	die "no qmp_ufs_phy_calibrate() calling reset_control_assert() in $build/vmlinux"
! grep -qx of_machine_compatible_match "$tmp/calibrate.calls" ||
	die "qmp_ufs_phy_calibrate() in $build/vmlinux still asks of_machine_compatible_match(): the board gate"
gate_check="qmp_ufs_phy_calibrate() in vmlinux (build ID ${new_id:0:12}) calls no of_machine_compatible_match()"
if [ -n "${BASE_SCRATCH:-}" ]; then
	[ -f "$BASE_SCRATCH/linux-gts9uwifi/vmlinux" ] || die "no vmlinux in $BASE_SCRATCH"
	[ "$(notes_build_id "$BASE_SCRATCH/linux-gts9uwifi/vmlinux")" = "$base_build_id" ] ||
		die "self-test failed: $BASE_SCRATCH/linux-gts9uwifi/vmlinux is not the release kernel's"
	calibrate_calls "$BASE_SCRATCH/linux-gts9uwifi/vmlinux" > "$tmp/calibrate.base.calls"
	grep -qx of_machine_compatible_match "$tmp/calibrate.base.calls" ||
		die 'self-test failed: the release calibration does not show the board gate either'
	gate_check="$gate_check; the release build's does"
fi
echo "UFS PHY: $gate_check"

# 5c. The PCIe PIPE clock (the sleep fixes), which leaves no string: with
# the revert the mux has its gate operations again and not the rework's
# rate ones, and without unpark-pcie0-pipe-mux.patch the PCIe PHY's power-on
# sets no clock rate (it is inlined into qmp_pcie_enable(), which has to
# call clk_bulk_enable(): that shows the function was found).  Then the geni
# UARTs (upstream d0cd9c8d0fd5 and 3098c989bd38), which add no string: the
# UART's system sleep callbacks call pm_runtime_force_suspend() and
# pm_runtime_force_resume() (a call or a tail branch).  From the vmlinux
# the image was made from (5b).  With BASE_SCRATCH the release build's has
# to show the opposite of each.
llvm-nm --defined-only "$build/vmlinux" > "$tmp/vmlinux.nm"
nm_has() { grep -q " [tT] $2\$" "$1"; } # NM-LIST FUNCTION
branches_of() { # VMLINUX FUNCTION: what FUNCTION calls or branches to by name, one per line
	llvm-objdump -d --no-show-raw-insn --disassemble-symbols="$2" "$1" |
		sed -n 's/.*[[:space:]]bl\{0,1\}[[:space:]].*<\([A-Za-z0-9_.]*\)>$/\1/p' | sort -u
}
for f in phy_mux_enable phy_mux_disable phy_mux_is_enabled; do
	nm_has "$tmp/vmlinux.nm" "$f" || die "no $f in $build/vmlinux: the PIPE clock mux revert is not in it"
done
for f in phy_mux_recalc_rate phy_mux_determine_rate phy_mux_set_rate; do
	! nm_has "$tmp/vmlinux.nm" "$f" || die "$f in $build/vmlinux: the PIPE clock mux has the rework's rate operations"
done
branches_of "$build/vmlinux" qmp_pcie_enable > "$tmp/pcie.calls"
grep -qx clk_bulk_enable "$tmp/pcie.calls" ||
	die "no qmp_pcie_enable() calling clk_bulk_enable() in $build/vmlinux"
! grep -qx clk_set_rate "$tmp/pcie.calls" ||
	die "qmp_pcie_enable() in $build/vmlinux calls clk_set_rate(): $unpark"
branches_of "$build/vmlinux" qcom_geni_serial_suspend > "$tmp/geni.suspend.calls"
branches_of "$build/vmlinux" qcom_geni_serial_resume > "$tmp/geni.resume.calls"
grep -qx uart_suspend_port "$tmp/geni.suspend.calls" && grep -qx pm_runtime_force_suspend "$tmp/geni.suspend.calls" ||
	die "qcom_geni_serial_suspend() in $build/vmlinux does not call both uart_suspend_port() and pm_runtime_force_suspend(): ${xo_patches[2]} is not in it"
grep -qx uart_resume_port "$tmp/geni.resume.calls" && grep -qx pm_runtime_force_resume "$tmp/geni.resume.calls" ||
	die "qcom_geni_serial_resume() in $build/vmlinux does not call both uart_resume_port() and pm_runtime_force_resume(): ${xo_patches[2]} is not in it"
vmlinux_check="vmlinux (build ID ${new_id:0:12}) has the PIPE clock mux's gate operations and not its rate ones, its qmp_pcie_enable() calls no clk_set_rate(), and its geni UART sleep callbacks call pm_runtime_force_suspend() and pm_runtime_force_resume()"
if [ -n "${BASE_SCRATCH:-}" ]; then
	llvm-nm --defined-only "$BASE_SCRATCH/linux-gts9uwifi/vmlinux" > "$tmp/vmlinux.base.nm"
	nm_has "$tmp/vmlinux.base.nm" phy_mux_set_rate && ! nm_has "$tmp/vmlinux.base.nm" phy_mux_enable ||
		die 'self-test failed: the release vmlinux does not have the rework'"'"'s PIPE clock mux'
	branches_of "$BASE_SCRATCH/linux-gts9uwifi/vmlinux" qmp_pcie_enable > "$tmp/pcie.base.calls"
	grep -qx clk_set_rate "$tmp/pcie.base.calls" ||
		die "self-test failed: the release build's qmp_pcie_enable() does not call clk_set_rate() either"
	branches_of "$BASE_SCRATCH/linux-gts9uwifi/vmlinux" qcom_geni_serial_suspend > "$tmp/geni.suspend.base.calls"
	branches_of "$BASE_SCRATCH/linux-gts9uwifi/vmlinux" qcom_geni_serial_resume > "$tmp/geni.resume.base.calls"
	grep -qx uart_suspend_port "$tmp/geni.suspend.base.calls" && grep -qx uart_resume_port "$tmp/geni.resume.base.calls" &&
		! grep -qx pm_runtime_force_suspend "$tmp/geni.suspend.base.calls" &&
		! grep -qx pm_runtime_force_resume "$tmp/geni.resume.base.calls" ||
		die "self-test failed: the release build's geni UART callbacks are not found, or force-suspend and force-resume the port already"
	vmlinux_check="$vmlinux_check; the release build's has the rate operations, calls it, and its UART callbacks call neither"
fi
echo "PCIe PIPE clock and geni UARTs: $vmlinux_check"

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

# 6b. The debug switches as the kernel knows them: built in, so each
# parameter's description is in the module tree's modules.builtin.modinfo
# under its object's name, where the release build's has none; and vmlinux
# has their handlers, the guard's genpd notifier, the CXPC log's PM notifier
# and raw file's show function, and the DSI PHY's system sleep callbacks.
tr '\0' '\n' < "$modules/modules.builtin.modinfo" > "$tmp/builtin.modinfo"
tr '\0' '\n' < "$base_modules/modules.builtin.modinfo" > "$tmp/builtin.base.modinfo"
for parm in gcc_sm8550.parm=debug_pcie0_off: gcc_sm8550.parm=debug_pcie0_phy_off: \
	gcc_sm8550.parm=debug_usb_off: rpmhpd.parm=debug_ret_floor: gcc_sm8550.parm=debug_boot_state: \
	pcie_qcom.parm=debug_root_port_d3: qcom_stats.parm=debug_cxpc_violators:; do
	grep -q "^$parm" "$tmp/builtin.modinfo" ||
		die "the module tree's modules.builtin.modinfo has no $parm"
	! grep -q "^$parm" "$tmp/builtin.base.modinfo" ||
		die "self-test failed: the release build's modules.builtin.modinfo has $parm too"
done
for f in gcc_sm8550_debug_set gcc_sm8550_debug_get gcc_sm8550_debug_boot_get rpmhpd_debug_ret_floor_set \
	qcom_pcie_debug_rp_d3_set qcom_pcie_pd_notify qcom_stats_vx_set qcom_stats_vx_pm_notify \
	dsi_phy_suspend_late dsi_phy_resume_early qcom_stats_vx_raw_show; do
	grep -q " [tT] $f\$" "$tmp/vmlinux.nm" || die "no $f in $build/vmlinux"
done
switch_check="the seven parameters' names in the image and their descriptions in modules.builtin.modinfo, none in the release build's; the set and get handlers, the guard's genpd notifier, the CXPC log's PM notifier, the DSI PHY's system sleep callbacks and the raw CXPC file's show function in vmlinux (build ID ${new_id:0:12})"
echo "debug switches: $switch_check"

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
printf '%s\n' "$revision" > "$kit/REVISION"
# What was built: this checkout, and whether it had uncommitted changes.
{
	echo "revision: $revision"
	echo "checkout: $(git -C "$repo" rev-parse HEAD) ($(git -C "$repo" describe --always --dirty --abbrev=12))"
	echo "uncommitted kernel inputs (git status --porcelain kernel scripts):"
	git -C "$repo" status --porcelain -- kernel scripts | sed 's/^/  /'
	echo "build: APPLY_QMP_UFS_PCS_STATE_DIAGNOSTIC=1 APPLY_DEEP_SLEEP_DEBUG_SWITCHES=1 APPLY_SM8550_SLEEP_FIXES=1 APPLY_SM8550_PCIE0_GDSC_OFF=1 APPLY_SM8550_PCIE0_GDSC_GUARD=1 APPLY_LPASS_VA_MACRO_PM_CLK=1 APPLY_DSI_PHY_AHB_RELEASE=1 APPLY_GENI_SERIAL_FORCE_SUSPEND=1 (their strings and code checked above); APPLY_SM8550_ROOT_PORT_D3 not set"
	echo "kernel/drivers/sm5714_usbpd.c $(sha "$repo/kernel/drivers/sm5714_usbpd.c")"
	echo "kernel/drivers/gts9u-performance.c $(sha "$repo/kernel/drivers/gts9u-performance.c")"
	for o in "${overlays[@]}"; do
		echo "kernel/dts/$o.dtso $(sha "$repo/kernel/dts/$o.dtso")"
	done
	for p in "${build_order[@]}" "$pcs_patch" "${ufs_patches[@]}"; do
		echo "kernel/patches/$p $(sha "$repo/kernel/patches/$p")"
	done
	echo "left out: kernel/patches/$unpark (APPLY_SM8550_SLEEP_FIXES=1)"
	echo "scripts/stage-ufs-resume-experiment.sh $(sha "$repo/scripts/stage-ufs-resume-experiment.sh")"
	echo "scripts/stage-performance-overlay.sh $(sha "$repo/scripts/stage-performance-overlay.sh")"
	echo "configuration: $config_check"
	echo "source check: $source_check"
	echo "power domain table: $gdsc_check"
	echo "UFS PHY board gate: $gate_check"
	echo "PCIe PIPE clock and geni UARTs: $vmlinux_check"
	echo "debug switches: $switch_check"
} > "$kit/SOURCE"
install -m 0644 "$here/install.sh" "$here/rollback.sh" "$here/verify.sh" "$here/README.md" "$kit/"
# The sleep test and the UFS PHY check are the SM-X710 kit's, which know
# both boards.
install -m 0644 "$repo/tools/inplace/typec-roles/sleep-test.sh" \
	"$repo/tools/inplace/typec-roles/ufs-phy-power-check.sh" "$kit/"
files=(boot.img "modules-$release.tar" BUILD_ID KEY_ID SIGNER REVISION SOURCE
	install.sh rollback.sh verify.sh sleep-test.sh ufs-phy-power-check.sh README.md)
(cd "$kit" && sha256sum "${files[@]}" > SHA256SUMS)
cat "$kit/SHA256SUMS"
