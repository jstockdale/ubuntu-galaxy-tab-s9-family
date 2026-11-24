#!/bin/bash
# Pack the Type-C roles kit on the build host.  It goes on top of the
# hang-log kit: the hang-log kernel rebuilt with this branch's five TCPM
# patches, the sm5714_usbpd policy that turns them on, the retention of the
# USB power domains across suspend and the NULL check of the hcd in the
# dwc3-qcom glue (docs/usb-c-dock-roles.md), since revision 8 the PCS
# reset of the SM8550 UFS PHY for both tablets with its diagnostic
# (docs/resume-recovery.md), since revision 9 the deep-sleep debug switches
# (run-time module parameters of gcc-sm8550 and rpmhpd, off at every boot),
# and since revision 10 the SM8550 sleep fixes (three upstream commits: the
# revert of the PCIe PIPE clock mux rework, with unpark-pcie0-pipe-mux.patch
# left out, the GPU RSC's stop sequence and ufs-qcom's lane clocks) and a
# second UFS PHY diagnostic, and since revision 11 pcie_0_gdsc collapsed in
# system suspend when the PCIe host has powered itself down, and kept on
# while it keeps its link (gcc-sm8550-pcie0-gdsc-off-in-suspend.patch, with
# the guard's genpd flag, and the guard's other half,
# pcie-qcom-keep-power-domain-while-link-kept.patch), with a run-time debug
# switch that lets the PCIe root port go to D3hot in system suspend
# (pcie-qcom-debug-root-port-d3.patch), and the UFS PHY power reference
# that ufs-qcom keeps track of (ufs-qcom-track-phy-power-state-gts9u.patch,
# which every build applies) with a diagnostic of it
# (ufs-qcom-phy-power-count-diagnostic.patch), and since revision 12 the
# AOP's log of the RPMh requesters that keep CX from collapsing, a third
# debug switch patch (qcom-stats-cxpc-violators-diagnostic.patch), and two
# fixes for clocks that keep XO voted through a system sleep, the VA
# macro's LPASS votes (lpass-va-macro-pm-clock-framework.patch, upstream
# eb667d0fbdd3) and the DSI PHY's AHB clock
# (msm-dsi-phy-release-ahb-clock-in-system-sleep.patch), in boot, plus the
# build's module tree, signed with its own key.  The configuration and the
# DTB do not change, so vendor_boot stays the hang-log kit's.  The build for
# this kit sets APPLY_QMP_UFS_PCS_STATE_DIAGNOSTIC=1,
# APPLY_DEEP_SLEEP_DEBUG_SWITCHES=1, APPLY_SM8550_SLEEP_FIXES=1,
# APPLY_SM8550_PCIE0_GDSC_OFF=1, APPLY_SM8550_PCIE0_GDSC_GUARD=1,
# APPLY_LPASS_VA_MACRO_PM_CLK=1 and APPLY_DSI_PHY_AHB_RELEASE=1 (and not
# APPLY_SM8550_ROOT_PORT_D3).
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
# new one is compared with, file by file, its compiled clock controller the
# reference for the power domain table, and its vmlinux the self-test of the
# check for the UFS PHY's board gate.
#
# Self-tests, each byte for byte:
#   - boot: the hang-log kernel plus its DTB, by the kits' recipe, is the
#     hang-log kit's boot.img, the image a first install finds on the tablet
#     and saves (an upgrade finds an earlier revision there, and the saved
#     image stays the way back);
#   - the hang-log vendor_boot carries that same DTB.
# Then the changes are checked to be only the intended ones: the DTB, the
# configuration and the module list are the hang-log build's, the source
# trees differ in the three Type-C files, the clock controller, the
# dwc3-qcom glue, the UFS PHY driver, the RPMh power domain driver, the
# PIPE clock mux, the PCIe PHY, the GMU and ufs-qcom files of revision 10
# (ufs-qcom's changed again in revision 11), the PCIe host driver of
# revision 11, and the qcom_stats driver, the VA macro and its Kconfig, and
# the DSI PHY's two files of revision 12 alone, and those are what this
# checkout's patches, stage script and driver give (the PCIe PHY's without
# unpark-pcie0-pipe-mux.patch), the new image has the new code (the debug
# switches' parameters among it) and the old one does not and was built
# after the last change to those files, the compiled power domain table
# differs in three power-state masks (the two USB domains' and
# pcie_0_gdsc's) and pcie_0_gdsc's genpd flags alone and the RPMh power
# domain driver's tables not at all, the UFS PHY's calibration in the new
# vmlinux no longer asks which
# board it runs on, the new vmlinux has the PIPE clock mux's gate operations
# and not its rate operations and its PCIe PHY power-on sets no clock rate,
# and the new boot image differs only in its kernel.  Nothing is flashed.
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
# This kit's revision: revision 7 plus the UFS PHY's PCS reset for both
# tablets and its diagnostic (8), plus the deep-sleep debug switches (9),
# plus the SM8550 sleep fixes and the second UFS PHY diagnostic (10), plus
# pcie_0_gdsc collapsed in system suspend with its guard and the root port's
# debug switch, and ufs-qcom's UFS PHY power reference fix with its
# diagnostic (11), plus the AOP's CXPC violators log behind a third debug
# switch patch and the VA macro's and the DSI PHY's clocks released in
# system sleep (12).  Written to the kit for people; install.sh knows a
# revision by its boot image and kernel build ID.
revision=12
dts=sm8550-samsung-gts9wifi
tcpm_dir=drivers/usb/typec/tcpm
gcc=drivers/clk/qcom/gcc-sm8550
dwc3=drivers/usb/dwc3/dwc3-qcom
qmp=drivers/phy/qualcomm/phy-qcom-qmp-ufs
rpmhpd=drivers/pmdomain/qcom/rpmhpd
# Revision 10's files: the PIPE clock mux, the PCIe PHY (which loses
# unpark-pcie0-pipe-mux.patch), the GMU and ufs-qcom.
phymux=drivers/clk/qcom/clk-regmap-phy-mux
qmp_pcie=drivers/phy/qualcomm/phy-qcom-qmp-pcie
gmu=drivers/gpu/drm/msm/adreno/a6xx_gmu
ufs_qcom=drivers/ufs/host/ufs-qcom
# Revision 11's PCIe host driver: the guard's half, and the root port's
# debug switch.
pcie_qcom=drivers/pci/controller/dwc/pcie-qcom
# Revision 12's: the qcom_stats driver (the CXPC log's debug switch), the VA
# macro and its Kconfig entry, and the DSI PHY's driver and header.
qcom_stats=drivers/soc/qcom/qcom_stats
va_macro=sound/soc/codecs/lpass-va-macro
dsi_phy=drivers/gpu/drm/msm/dsi/phy/dsi_phy
# The USB-C dock changes, each applied to the hang-log tree's file as it is.
typec_patches=(tcpm-request-dfp-as-sink-before-discovery.patch tcpm-try-snk-extra-wait.patch
	tcpm-no-dr-swap-during-vdm.patch tcpm-dr-set-checks-data-role.patch
	tcpm-keep-queued-swap-pending.patch gcc-sm8550-usb-gdscs-retention.patch
	dwc3-qcom-check-hcd-before-reading-port-speed.patch)
# The UFS PHY's three.  The hang-log tree carries the earlier, SM-X910-only
# form of the first, which scripts/stage-ufs-resume-experiment.sh replaces;
# the diagnostic goes on top, with APPLY_QMP_UFS_PCS_STATE_DIAGNOSTIC=1, and
# revision 10's second diagnostic on top of it, with that and
# APPLY_SM8550_SLEEP_FIXES=1.
ufs_patches=(qmp-ufs-assert-pcs-reset-before-calibration-gts9u.patch
	qmp-ufs-pcs-state-diagnostic.patch qmp-ufs-pwrdn-readback-diagnostic.patch)
# Revision 11's pcie_0_gdsc, with APPLY_SM8550_PCIE0_GDSC_OFF=1: two lines of
# the power domain table, its power states and the guard's genpd flag, on
# top of the USB power domain patch.
gdsc_patches=(gcc-sm8550-pcie0-gdsc-off-in-suspend.patch)
# The line it gives pcie_0_gdsc's power states, by which the build script
# knows it, and the one it gives its genpd.
pcie0_line='PWRSTS_OFF_ON, /* gcc-sm8550-pcie0-gdsc-off-in-suspend.patch */'
guard_line='		.flags = GENPD_FLAG_ACTIVE_WAKEUP,'
# The PCIe host's: the guard's other half (APPLY_SM8550_PCIE0_GDSC_GUARD=1),
# then the root port's debug switch on top of it (with
# APPLY_DEEP_SLEEP_DEBUG_SWITCHES=1 as well), each known by a line it adds.
pcie_patches=(pcie-qcom-keep-power-domain-while-link-kept.patch pcie-qcom-debug-root-port-d3.patch)
pcie_lines=('qcom_pcie_mark_sleep_path(dev, pcie->link_kept);'
	'PCIe root port to D3 for a sleep test, not for release builds')
# Revision 9's debug switches, with APPLY_DEEP_SLEEP_DEBUG_SWITCHES=1: the
# GDSC one on top of the USB power domain patch (and of revision 11's), the
# RPMh one on its own; and revision 12's third, the AOP's CXPC log, in
# qcom_stats.
debug_patches=(gcc-sm8550-gdsc-debug-switches.patch rpmhpd-debug-retention-floor.patch
	qcom-stats-cxpc-violators-diagnostic.patch)
# Revision 10's SM8550 sleep fixes, with APPLY_SM8550_SLEEP_FIXES=1: three
# upstream commits, one file each (ufs-qcom's two), and the PCIe PHY without
# unpark-pcie0-pipe-mux.patch, which the hang-log tree carries.
fix_patches=(clk-qcom-regmap-phy-mux-revert-rework.patch msm-adreno-a6xx-fix-stale-rpmh-votes.patch
	ufs-qcom-enable-only-lane-clocks.patch)
fix_files=("$phymux.c" "$gmu.c" "$ufs_qcom.c")
unpark=unpark-pcie0-pipe-mux.patch
# Revision 11's ufs-qcom, in both of its files: the UFS PHY power reference
# the host keeps track of, which every build applies, then its diagnostic,
# with APPLY_QMP_UFS_PCS_STATE_DIAGNOSTIC=1; both go in under the lane clock
# fix above.
ufs_qcom_patches=(ufs-qcom-track-phy-power-state-gts9u.patch ufs-qcom-phy-power-count-diagnostic.patch)
# Revision 12's two clocks that kept XO voted through a system sleep, each
# behind a variable of its own (APPLY_LPASS_VA_MACRO_PM_CLK=1,
# APPLY_DSI_PHY_AHB_RELEASE=1), strictly: the VA macro's LPASS votes
# (upstream eb667d0fbdd3, its driver and Kconfig entry) and the DSI PHY's
# AHB clock (its driver and header, on top of the port's DSI PHY patches).
xo_patches=(lpass-va-macro-pm-clock-framework.patch msm-dsi-phy-release-ahb-clock-in-system-sleep.patch)
xo_files=("$va_macro.c sound/soc/codecs/Kconfig" "$dsi_phy.c $dsi_phy.h")
patches=("${typec_patches[@]}" "${gdsc_patches[@]}" "${debug_patches[@]}" "${fix_patches[@]}" "${ufs_patches[@]}"
	"${pcie_patches[@]}" "${ufs_qcom_patches[@]}" "${xo_patches[@]}")
# The files those patches change, and the PCIe PHY's.
patched=("$tcpm_dir/tcpm.c" include/linux/usb/tcpm.h "$gcc.c" "$dwc3.c" "$qmp.c" "$rpmhpd.c"
	"$phymux.c" "$qmp_pcie.c" "$gmu.c" "$ufs_qcom.c" "$ufs_qcom.h" "$pcie_qcom.c"
	"$qcom_stats.c" "$va_macro.c" sound/soc/codecs/Kconfig "$dsi_phy.c" "$dsi_phy.h")

# The base on the tablet: the hang-log kit, which a first install saves and
# rollback.sh puts back.
hang_boot=d583546b5e5c33dbc48fbafc99b7eb039df33f64b0316e498d3f158f834ee968
hang_vendor_boot=9165f42917a8ee4069b878fc0c0660391b39087fc6f3ea2b607f626821a7dbd4
hang_image=3cb91f345b5e217290a737f254d7a278c426a0885305de008f308b27e7321216
hang_dtb=5ff2495f1cc0e43f141956f84f2d24a4a325ede3e246806d7f034ba89d3c4060
hang_config=4cd636e12d586ae4c0510217e4237f90627013afa2406a9d34a694ab5c44c394
hang_build_id=68d243e908c0edfddc10429ede4bb235f205e44c

# What only the new kernel may contain: the patch's log line, the driver's
# two parameters, and the deadline of the Try.SNK wait, a member of tcpm_port
# whose name is in the image's type information; that one tells the wait
# with a deadline from the plain longer timeout before it.  Then what the
# revisions after it add: the log lines of the three fixes for a swap asked
# for from userspace (the third has three; its PR_Swap line is in no earlier
# revision of that patch), and policy_open, a member of the driver's
# structure, which only a driver that reads its parameters at each attach
# has.  Then revision 8's UFS PHY diagnostic: the two labels of its state
# lines and its line for a PCS_READY that never came.  The PCS reset itself
# leaves no string; the disassembly check below is for it.  Then revision
# 9's debug switches: the names of the four module parameters as the kernel
# keeps them (built in, so prefixed with the object's name).  Then revision
# 10: the three messages of ufs-qcom's lane clock lookup, and the second UFS
# PHY diagnostic's line.  The PIPE clock mux revert, the missing
# unpark-pcie0-pipe-mux.patch and the GMU fix leave no string; the symbol
# and disassembly checks below are for the first two, the GMU's source and
# object dates for the last.  Last, revision 11: the debug switches' fifth
# parameter, debug_boot_state; two of the four lines the guard's PCIe half
# logs at a resume (the one a kept link should give, and its warning); the
# root port's debug switch, as the kernel keeps it; the message the UFS PHY
# power reference fix gives a failed PHY power-on in ufs-qcom (in no other
# driver), and two of its diagnostic's: the label of its line at each
# power-up sequence, and the end of that line, the setup_clocks repeats.
# Its pcie_0_gdsc's power states and genpd flags are two bytes of a table:
# the power domain check below is for them.  Then revision 12: the CXPC
# log's switch, as the kernel keeps it, and the line each suspend with it
# logs; the VA macro's message for a runtime PM put that failed in its
# fsgen unwind (upstream's, in no other driver); and the DSI PHY's message
# for an AHB clock it could not prepare.
new_strings=('Requesting DFP before discovery' sink_host_swap try_snk_extra_wait_ms try_snk_wait_until
	'VDM in progress, DATA_ROLE_SWAP not started' 'Data role swap ended with the role unchanged'
	'Swap is still queued, left pending' 'Partner swapped first, queued DR_SWAP_SEND dropped'
	'Partner swapped first, queued PR_SWAP_SEND dropped'
	policy_open
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
	'runtime PM put failed in fsgen enable unwind' 'Unable to prepare iface clk')
# The objects of the changed sources.  Each has to be newer than what it was
# compiled from and older than the vmlinux it was linked into.
objects=("$gcc.o:$gcc.c" "$tcpm_dir/tcpm.o:$tcpm_dir/tcpm.c" "$tcpm_dir/tcpm.o:include/linux/usb/tcpm.h"
	"$tcpm_dir/sm5714_usbpd.o:$tcpm_dir/sm5714_usbpd.c"
	"$tcpm_dir/sm5714_usbpd.o:include/linux/usb/tcpm.h" "$dwc3.o:$dwc3.c" "$qmp.o:$qmp.c"
	"$rpmhpd.o:$rpmhpd.c" "$phymux.o:$phymux.c" "$qmp_pcie.o:$qmp_pcie.c" "$gmu.o:$gmu.c"
	"$ufs_qcom.o:$ufs_qcom.c" "$ufs_qcom.o:$ufs_qcom.h" "$pcie_qcom.o:$pcie_qcom.c"
	"$qcom_stats.o:$qcom_stats.c" "$va_macro.o:$va_macro.c" "$dsi_phy.o:$dsi_phy.c" "$dsi_phy.o:$dsi_phy.h")

sha() { sha256sum "$1" | cut -d' ' -f1; }
die() { echo "$*" >&2; exit 1; }
for f in "$mkbootimg" "$avbtool" "$out/Image.gz" "$out/$dts.dtb" "$out/config" \
	"$hang/Image.gz" "$hang/$dts.dtb" "$hang/config" "$hangkit/boot.img" \
	"$hangkit/vendor_boot.img" "${patched[@]/#/$tree/}" \
	"$build/.config" "$build/certs/signing_key.x509" "$build/vmlinux" \
	"$build/$gcc.o" "$build/$tcpm_dir/tcpm.o" "$build/$tcpm_dir/sm5714_usbpd.o" "$build/$dwc3.o" \
	"$build/$qmp.o" "$build/$rpmhpd.o" "$build/$phymux.o" "$build/$qmp_pcie.o" "$build/$gmu.o" \
	"$build/$ufs_qcom.o" "$build/$pcie_qcom.o" "$build/$qcom_stats.o" "$build/$va_macro.o" "$build/$dsi_phy.o" \
	"$tree/$tcpm_dir/sm5714_usbpd.c" "$build/arch/arm64/boot/Image.gz" \
	"$repo/kernel/drivers/sm5714_usbpd.c" "${patches[@]/#/$repo/kernel/patches/}" \
	"$repo/kernel/patches/$unpark" "$repo/scripts/stage-ufs-resume-experiment.sh"; do
	[ -f "$f" ] || die "missing: $f"
done
for t in unpack_bootimg python3 sha256sum cmp diff patch tar modinfo xxd openssl git llvm-objcopy llvm-objdump llvm-nm pahole; do
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

# 3. The sources.  The driver is this checkout's; the two TCPM files, the
# clock controller and the dwc3-qcom glue are the hang-log tree's with this
# checkout's seven patches on top, and the UFS PHY driver is the hang-log
# tree's with its SM-X910-only PCS reset replaced by the one for every SM8550
# PHY and the diagnostic on top; nothing else in the kernel tree differs.
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
# Revision 11: pcie_0_gdsc PWRSTS_OFF_ON by the line of its patch, and the
# guard's GENPD_FLAG_ACTIVE_WAKEUP in its entry and nowhere else in the
# file, the patch whole in the build tree's file (it comes out again with no
# fuzz), and its PHY's domain and the PCIe1 ones as upstream has them.
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
mkdir -p "$tmp/gdsc/${gcc%/*}"
cp "$tree/$gcc.c" "$tmp/gdsc/$gcc.c"
for ((i = ${#gdsc_patches[@]} - 1; i >= 0; i--)); do
	patch -s --reverse --force --fuzz=0 --no-backup-if-mismatch -d "$tmp/gdsc" -p1 \
		< "$repo/kernel/patches/${gdsc_patches[$i]}" ||
		die "the build tree's $gcc.c does not carry ${gdsc_patches[$i]} whole"
done
# The PCIe host driver: the guard's half and the root port's debug switch,
# each known by its line, both whole in the build tree's file (they come out
# again with no fuzz, the switch first).
for i in "${!pcie_patches[@]}"; do
	grep -qF -- "${pcie_lines[$i]}" "$tree/$pcie_qcom.c" ||
		die "the build tree does not carry ${pcie_patches[$i]}"
done
mkdir -p "$tmp/pcie/${pcie_qcom%/*}"
cp "$tree/$pcie_qcom.c" "$tmp/pcie/$pcie_qcom.c"
for ((i = ${#pcie_patches[@]} - 1; i >= 0; i--)); do
	patch -s --reverse --force --fuzz=0 --no-backup-if-mismatch -d "$tmp/pcie" -p1 \
		< "$repo/kernel/patches/${pcie_patches[$i]}" ||
		die "the build tree's $pcie_qcom.c does not carry ${pcie_patches[$i]} whole"
done
# The guard adds no string to the image; its comment is the marker.
grep -q 'no hcd and no root hub' "$tree/$dwc3.c" ||
	die 'the build tree does not carry the dwc3-qcom guard'
# The UFS PHY: both patches are in the build tree's file, each whole (they
# come out again with no fuzz, the diagnostic first), and the SM-X910-only
# form of the PCS reset is not.
mkdir -p "$tmp/ufs/${qmp%/*}"
cp "$tree/$qmp.c" "$tmp/ufs/$qmp.c"
for ((i = ${#ufs_patches[@]} - 1; i >= 0; i--)); do
	patch -s --reverse --force --fuzz=0 --no-backup-if-mismatch -d "$tmp/ufs" -p1 \
		< "$repo/kernel/patches/${ufs_patches[$i]}" ||
		die "the build tree's $qmp.c does not carry ${ufs_patches[$i]} whole"
done
! grep -q 'SM-X910 experiment: assert PCS reset before calibration tables' "$tree/$qmp.c" ||
	die "the build tree's $qmp.c still carries the SM-X910-only PCS reset"
# Revision 9's debug switches and revision 12's third: each patch whole in
# the build tree's file (it comes out again with no fuzz), the GDSC one over
# the USB power domain patch, which the check above found under it.
debug_files=("$gcc.c" "$rpmhpd.c" "$qcom_stats.c")
for i in "${!debug_patches[@]}"; do
	f=${debug_files[$i]}
	mkdir -p "$tmp/debug/${f%/*}"
	cp "$tree/$f" "$tmp/debug/$f"
	patch -s --reverse --force --fuzz=0 --no-backup-if-mismatch -d "$tmp/debug" -p1 \
		< "$repo/kernel/patches/${debug_patches[$i]}" ||
		die "the build tree's $f does not carry ${debug_patches[$i]} whole"
done
# Revision 10's sleep fixes: each upstream commit whole in the build tree
# (ufs-qcom's in both of its files), and the PCIe PHY without
# unpark-pcie0-pipe-mux.patch, which the PIPE clock mux revert replaces.
for i in "${!fix_patches[@]}"; do
	f=${fix_files[$i]}
	mkdir -p "$tmp/fix/${f%/*}"
	cp "$tree/$f" "$tmp/fix/$f"
	[ "$f" != "$ufs_qcom.c" ] || cp "$tree/$ufs_qcom.h" "$tmp/fix/$ufs_qcom.h"
	patch -s --reverse --force --fuzz=0 --no-backup-if-mismatch -d "$tmp/fix" -p1 \
		< "$repo/kernel/patches/${fix_patches[$i]}" ||
		die "the build tree's $f does not carry ${fix_patches[$i]} whole"
done
# Revision 11's ufs-qcom patches: both whole in the build tree's two files,
# under the lane clock fix (they come out again with no fuzz, the diagnostic
# first).
mkdir -p "$tmp/ufsq/${ufs_qcom%/*}"
cp "$tree/$ufs_qcom.c" "$tree/$ufs_qcom.h" "$tmp/ufsq/${ufs_qcom%/*}/"
for ((i = ${#ufs_qcom_patches[@]} - 1; i >= 0; i--)); do
	patch -s --reverse --force --fuzz=0 --no-backup-if-mismatch -d "$tmp/ufsq" -p1 \
		< "$repo/kernel/patches/${ufs_qcom_patches[$i]}" ||
		die "the build tree's $ufs_qcom.c and $ufs_qcom.h do not carry ${ufs_qcom_patches[$i]} whole"
done
! grep -q 'clk_set_rate(qmp->pipe_clks\[0\].clk, ULONG_MAX)' "$tree/$qmp_pcie.c" ||
	die "the build tree's $qmp_pcie.c carries $unpark, which revision 10 leaves out"
# Revision 12's two XO fixes: each whole in the build tree's files (each
# comes out again with no fuzz from its own copies).
for i in "${!xo_patches[@]}"; do
	for f in ${xo_files[$i]}; do
		mkdir -p "$tmp/xo$i/${f%/*}"
		cp "$tree/$f" "$tmp/xo$i/$f"
	done
	patch -s --reverse --force --fuzz=0 --no-backup-if-mismatch -d "$tmp/xo$i" -p1 \
		< "$repo/kernel/patches/${xo_patches[$i]}" ||
		die "the build tree's ${xo_files[$i]} do not carry ${xo_patches[$i]} whole"
done
source_check='marker lines only (HANG_SCRATCH not set)'
if [ -n "${HANG_SCRATCH:-}" ]; then
	hang_tree=$HANG_SCRATCH/linux-src-gts9wifi
	for f in "${patched[@]}"; do
		[ -f "$hang_tree/$f" ] || die "no kernel source tree in $HANG_SCRATCH"
		mkdir -p "$tmp/src/${f%/*}"
		cp "$hang_tree/$f" "$tmp/src/$f"
	done
	# The hang-log build applied this branch's opps patch to dwc3-qcom.c
	# already, and nothing of what this kit adds: the guard goes on top of
	# that, so the comparison below sees the guard alone.
	grep -q 'dwc3_qcom_set_pstate' "$hang_tree/$dwc3.c" ||
		die "the hang-log tree's dwc3-qcom.c does not carry dwc3-qcom-drop-required-opps-vote-in-system-suspend.patch"
	! grep -q 'no hcd and no root hub' "$hang_tree/$dwc3.c" &&
		! grep -q 'Retention across system suspend, as on sm8650' "$hang_tree/$gcc.c" ||
		die 'the hang-log tree carries the dwc3-qcom guard or the USB power domain patch already'
	! grep -q 'SM8550 deep-sleep debug, not for release builds' "$hang_tree/$gcc.c" "$hang_tree/$rpmhpd.c" \
		"$hang_tree/$qcom_stats.c" ||
		die 'the hang-log tree carries the deep-sleep debug switches already'
	# Revision 12's two XO fixes: the hang-log build had neither.
	! grep -qF 'ret = pm_clk_suspend(dev);' "$hang_tree/$va_macro.c" &&
		! grep -qF 'static int dsi_phy_suspend_late(struct device *dev)' "$hang_tree/$dsi_phy.c" ||
		die "the hang-log tree carries revision 12's VA macro or DSI PHY change already"
	! grep -qF -- "$pcie0_line" "$hang_tree/$gcc.c" &&
		[ "$(gdsc_pwrsts "$hang_tree/$gcc.c" pcie_0_gdsc)" = 'PWRSTS_RET_ON,' ] ||
		die "the hang-log tree's pcie_0_gdsc is not upstream's PWRSTS_RET_ON"
	# Revision 11's guard and the root port's switch: in neither of its
	# files.
	! grep -qF -- GENPD_FLAG_ACTIVE_WAKEUP "$hang_tree/$gcc.c" &&
		! grep -qF -e "${pcie_lines[0]}" -e "${pcie_lines[1]}" "$hang_tree/$pcie_qcom.c" ||
		die "the hang-log tree carries revision 11's guard or the root port's debug switch already"
	# Revision 10: the hang-log build had none of the three upstream commits
	# and did apply unpark-pcie0-pipe-mux.patch, as every default build does.
	! grep -q 'static int phy_mux_enable(struct clk_hw \*hw)' "$hang_tree/$phymux.c" &&
		! grep -qF 'if (!test_and_clear_bit(GMU_STATUS_FW_START, &gmu->status))' "$hang_tree/$gmu.c" &&
		! grep -q 'tx_lane0_sync_clk' "$hang_tree/$ufs_qcom.c" ||
		die 'the hang-log tree carries one of the SM8550 sleep fixes already'
	grep -q 'clk_set_rate(qmp->pipe_clks\[0\].clk, ULONG_MAX)' "$hang_tree/$qmp_pcie.c" ||
		die "the hang-log tree's $qmp_pcie.c does not carry $unpark"
	# Revision 11's ufs-qcom patches: the hang-log build had neither.
	! grep -qF -e 'Power the PHY off if this host holds its power' -e 'static void ufs_qcom_dbg_phy_counts(' \
		"$hang_tree/$ufs_qcom.c" ||
		die "the hang-log tree carries revision 11's ufs-qcom patches already"
	# Like every build before revision 8, the hang-log build gave the PCS
	# reset to the SM-X910 alone, and has no diagnostic.
	grep -q 'SM-X910 experiment: assert PCS reset before calibration tables' "$hang_tree/$qmp.c" &&
		! grep -q 'SM8550: assert the PCS reset before the calibration tables' "$hang_tree/$qmp.c" &&
		! grep -q 'Diagnostic for SM8550 resume tests' "$hang_tree/$qmp.c" ||
		die "the hang-log tree's $qmp.c is not the one with the SM-X910-only PCS reset"
	for p in "${typec_patches[@]}"; do
		patch -s -d "$tmp/src" -p1 --no-backup-if-mismatch < "$repo/kernel/patches/$p" ||
			die "$p does not apply to the hang-log tree"
	done
	# Revision 11's pcie_0_gdsc, then the debug switches (one line further
	# down than without it), then the PCIe host's two, as the build applies
	# them, strictly.
	for p in "${gdsc_patches[@]}" "${debug_patches[@]}" "${pcie_patches[@]}"; do
		patch -s --forward --batch --fuzz=0 --no-backup-if-mismatch -d "$tmp/src" -p1 \
			< "$repo/kernel/patches/$p" ||
			die "$p does not apply to the hang-log tree"
	done
	# Revision 10's sleep fixes as the build applies them, strictly, and the
	# PCIe PHY as a build that leaves out unpark-pcie0-pipe-mux.patch has it.
	for p in "${fix_patches[@]}"; do
		patch -s --forward --batch --fuzz=0 --no-backup-if-mismatch -d "$tmp/src" -p1 \
			< "$repo/kernel/patches/$p" ||
			die "$p does not apply to the hang-log tree"
	done
	patch -s --reverse --force --fuzz=0 --no-backup-if-mismatch -d "$tmp/src" -p1 \
		< "$repo/kernel/patches/$unpark" ||
		die "$unpark does not come out of the hang-log tree's $qmp_pcie.c whole"
	# Revision 12's two XO fixes, strictly, as the build applies them (the
	# DSI PHY's on top of the port's DSI PHY patches, which the hang-log
	# tree carries as the build tree does).
	for p in "${xo_patches[@]}"; do
		patch -s --forward --batch --fuzz=0 --no-backup-if-mismatch -d "$tmp/src" -p1 \
			< "$repo/kernel/patches/$p" ||
			die "$p does not apply to the hang-log tree"
	done
	# Revision 11's ufs-qcom patches, strictly: here on top of the lane clock
	# fix, where the build has them under it; the two files come out the same.
	for p in "${ufs_qcom_patches[@]}"; do
		patch -s --forward --batch --fuzz=0 --no-backup-if-mismatch -d "$tmp/src" -p1 \
			< "$repo/kernel/patches/$p" ||
			die "$p does not apply to the hang-log tree"
	done
	# The UFS PHY as the build changes it: this checkout's stage script
	# replaces the SM-X910-only PCS reset, then the diagnostic goes on top,
	# then revision 10's second diagnostic.
	UFS_PCS_RESET_EXPERIMENTAL=1 bash "$repo/scripts/stage-ufs-resume-experiment.sh" "$tmp/src" \
		> "$tmp/stage-ufs.log" ||
		die "scripts/stage-ufs-resume-experiment.sh does not take the hang-log tree's $qmp.c"
	for p in qmp-ufs-pcs-state-diagnostic.patch qmp-ufs-pwrdn-readback-diagnostic.patch; do
		patch -s --forward --batch --fuzz=0 --no-backup-if-mismatch -d "$tmp/src" -p1 \
			< "$repo/kernel/patches/$p" ||
			die "$p does not apply to the hang-log tree's $qmp.c"
	done
	for f in "${patched[@]}"; do
		cmp -s "$tmp/src/$f" "$tree/$f" ||
			die "$f is not the hang-log tree's with this checkout's patches applied"
	done
	# The isolated M= module builds leave their products next to the
	# sources (net/netfilter, ath12k, ...), and those differ with every
	# build: they are signed with the build's own key.
	diff -rq -x .git -x '*.o' -x '*.ko' -x '.*.cmd' -x '*.mod' -x '*.mod.c' \
		-x modules.order -x Module.symvers \
		"$hang_tree" "$tree" > "$tmp/trees.diff" || :
	# patch(1) keeps the file it started from as FILE.orig when a hunk goes
	# in at an offset, and scripts/build-mainline-kernel.sh does not tell it
	# not to: the dwc3-qcom guard goes in four lines down, after the opps
	# patch, and revision 11's ufs-qcom fix 31 lines up in ufs-qcom.c and one
	# in ufs-qcom.h (its line numbers are those of the lane clock fix's files,
	# which go on top of it).  Such a backup is in the new tree alone.  It is
	# passed over when it is what patch(1) kept there, the hang-log tree's
	# file, and shown like any other file when it is anything else.
	backups=
	for f in "${patched[@]}"; do
		[ -f "$tree/$f.orig" ] && [ ! -e "$hang_tree/$f.orig" ] &&
			cmp -s "$tree/$f.orig" "$hang_tree/$f" || continue
		grep -vxF "Only in $tree/${f%/*}: ${f##*/}.orig" "$tmp/trees.diff" > "$tmp/trees.rest" || :
		mv "$tmp/trees.rest" "$tmp/trees.diff"
		backups="$backups ${f##*/}.orig"
	done
	sed -e "s|^Files $hang_tree/\(.*\) and .* differ\$|\1|" "$tmp/trees.diff" | sort > "$tmp/trees.changed"
	sort > "$tmp/trees.want" <<EOF
$gcc.c
$dwc3.c
$qmp.c
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
$tcpm_dir/sm5714_usbpd.c
$tcpm_dir/tcpm.c
include/linux/usb/tcpm.h
EOF
	cmp -s "$tmp/trees.changed" "$tmp/trees.want" || {
		echo 'the kernel source trees differ in more than the three Type-C files, gcc-sm8550.c, dwc3-qcom.c, phy-qcom-qmp-ufs.c, rpmhpd.c, clk-regmap-phy-mux.c, phy-qcom-qmp-pcie.c, a6xx_gmu.c, ufs-qcom.c, ufs-qcom.h, pcie-qcom.c, qcom_stats.c, lpass-va-macro.c, sound/soc/codecs/Kconfig, dsi_phy.c and dsi_phy.h:' >&2
		cat "$tmp/trees.diff" >&2
		exit 1
	}
	source_check="compared with $hang_tree: eighteen files differ, tcpm.c, tcpm.h, gcc-sm8550.c and dwc3-qcom.c by this checkout's seven patches, gcc-sm8550.c by revision 11's pcie_0_gdsc, gcc-sm8550.c, rpmhpd.c and qcom_stats.c by its three debug switch patches, clk-regmap-phy-mux.c, a6xx_gmu.c, ufs-qcom.c and ufs-qcom.h by its three sleep fixes, ufs-qcom.c and ufs-qcom.h by revision 11's PHY power reference fix and its diagnostic, phy-qcom-qmp-pcie.c by $unpark left out, phy-qcom-qmp-ufs.c by its stage script and the two UFS PHY diagnostics, pcie-qcom.c by revision 11's guard and the root port's debug switch, lpass-va-macro.c and sound/soc/codecs/Kconfig by upstream eb667d0fbdd3, dsi_phy.c and dsi_phy.h by the DSI PHY's AHB clock release"
	echo 'sources: only sm5714_usbpd.c, tcpm.c, tcpm.h, gcc-sm8550.c, dwc3-qcom.c, phy-qcom-qmp-ufs.c, rpmhpd.c, clk-regmap-phy-mux.c, phy-qcom-qmp-pcie.c, a6xx_gmu.c, ufs-qcom.c, ufs-qcom.h, pcie-qcom.c, qcom_stats.c, lpass-va-macro.c, sound/soc/codecs/Kconfig, dsi_phy.c and dsi_phy.h differ from the hang-log tree, by exactly this checkout'"'"'s changes'
	if [ -n "$backups" ]; then
		source_check="$source_check; passed over, a copy of the hang-log tree's file that patch(1) kept:$backups"
		echo "sources: passed over$backups, the hang-log tree's file as patch(1) kept it"
	fi
else
	echo 'sources: the twenty-three patches and this checkout'"'"'s driver are in the build tree, and unpark-pcie0-pipe-mux.patch is not (HANG_SCRATCH not set: no tree comparison)'
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
# The power domain changes are four numbers in a table and leave no string.
# The table was compiled from the patched source and linked into this image
# (the check above), and against the hang-log build's it differs in exactly
# those four bytes: the two USB domains' 5 (PWRSTS_OFF_ON) became 6
# (PWRSTS_RET_ON), pcie_0_gdsc's 6 became 5 (revision 11), and pcie_0_gdsc's
# genpd flags 0 became 8 (GENPD_FLAG_ACTIVE_WAKEUP, the guard's; little
# endian, so the low byte).  Each changed byte is named by the object of the
# new build that holds it (its symbol table's .data entries: in an object
# file a symbol's value is its offset in its section), and has to be the
# member it is: .pwrsts of struct gdsc for the three, .pd.flags for the
# fourth, at the offsets the object's own debug information gives them
# (pahole).
member_offset() { # OBJECT STRUCT MEMBER: the member's offset in the struct, or nothing
	# (pahole failing gives nothing, which the caller refuses, rather than
	# a silent exit under pipefail.)
	{ pahole -C "$2" "$1" 2>/dev/null || :; } | awk -v m="$3" '
		{ for (i = 1; i + 2 <= NF; i++) if ($i == m ";" && $(i + 1) == "/*") { print $(i + 2); exit } }'
}
gdsc_table_check() { # HANG_OBJECT NEW_OBJECT: 0, or 1 with what differs on stderr
	llvm-objcopy -O binary --only-section=.data "$1" "$tmp/gcc.hang.data"
	llvm-objcopy -O binary --only-section=.data "$2" "$tmp/gcc.new.data"
	[ -s "$tmp/gcc.new.data" ] || { echo "no .data in $2" >&2; return 1; }
	cmp -l "$tmp/gcc.hang.data" "$tmp/gcc.new.data" > "$tmp/gcc.data.diff" || :
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
	echo "the compiled $gcc table does not differ from the hang-log build's in the four bytes alone (pcie_0_gdsc's .pd.flags at $flags_at 0 to 8, and the .pwrsts at $pwrsts_at of pcie_0_gdsc 6 to 5 and of the two USB domains 5 to 6; symbol, byte in it, old, new, octal):" >&2
	cat "$tmp/gcc.data.named" >&2
	return 1
}
gdsc_check='the object is newer than the patched source and older than vmlinux (HANG_SCRATCH not set)'
if [ -n "${HANG_SCRATCH:-}" ]; then
	[ -f "$HANG_SCRATCH/linux-gts9wifi/$gcc.o" ] || die "no $gcc.o in $HANG_SCRATCH"
	gdsc_table_check "$HANG_SCRATCH/linux-gts9wifi/$gcc.o" "$build/$gcc.o" || exit 1
	gdsc_check="the compiled table differs from the hang-log build's in four bytes, pcie_0_gdsc's .pwrsts 6 to 5 and the two USB domains' 5 to 6 (.pwrsts at $pwrsts_at in each), and pcie_0_gdsc's .pd.flags 0 to 8 (GENPD_FLAG_ACTIVE_WAKEUP, at $flags_at)"
	# The debug switches write nothing until one is set: the RPMh power
	# domain driver's compiled tables are the hang-log build's, byte for byte
	# (the GDSC table's four bytes are the USB power domain patch's and
	# revision 11's).
	[ -f "$HANG_SCRATCH/linux-gts9wifi/$rpmhpd.o" ] || die "no $rpmhpd.o in $HANG_SCRATCH"
	llvm-objcopy -O binary --only-section=.data "$HANG_SCRATCH/linux-gts9wifi/$rpmhpd.o" "$tmp/rpmhpd.hang.data"
	llvm-objcopy -O binary --only-section=.data "$build/$rpmhpd.o" "$tmp/rpmhpd.new.data"
	[ -s "$tmp/rpmhpd.new.data" ] && cmp -s "$tmp/rpmhpd.hang.data" "$tmp/rpmhpd.new.data" ||
		die "the compiled $rpmhpd tables are not the hang-log build's"
	gdsc_check="$gdsc_check; the RPMh power domain driver's are the hang-log build's"
fi
echo "power domains: $gdsc_check"

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

# 5b. The UFS PHY's board gate, which the calibration may no longer have.
# Up to revision 7 it asked of_machine_is_compatible("samsung,gts9uwifi"),
# an inline that calls of_machine_compatible_match(), before the PCS reset;
# now the reset goes to every SM8550 PHY.  No string in the image tells the
# two apart ("samsung,gts9uwifi" is there for other code), so vmlinux is
# disassembled: the ELF the image was made from, which its build ID ties to
# the image.  Its qmp_ufs_phy_calibrate() must call reset_control_assert(),
# which shows the function was found, and must not call
# of_machine_compatible_match().  With HANG_SCRATCH the hang-log build's
# vmlinux, whose calibration has the gate, must show that call.
notes_build_id() { # VMLINUX: the build ID of its .notes
	llvm-objcopy -O binary --only-section=.notes "$1" "$tmp/notes.bin" &&
		build_id "$tmp/notes.bin"
}
calls_of() { # VMLINUX FUNCTION: what FUNCTION calls, one per line
	llvm-objdump -d --no-show-raw-insn --disassemble-symbols="$2" "$1" |
		sed -n 's/.*[[:space:]]bl[[:space:]].*<\([A-Za-z0-9_.]*\)>$/\1/p' | sort -u
}
[ "$(notes_build_id "$build/vmlinux")" = "$new_id" ] ||
	die "$build/vmlinux is not the ELF of the new image (another build ID)"
calls_of "$build/vmlinux" qmp_ufs_phy_calibrate > "$tmp/calibrate.calls"
grep -qx reset_control_assert "$tmp/calibrate.calls" ||
	die "no qmp_ufs_phy_calibrate() calling reset_control_assert() in $build/vmlinux"
! grep -qx of_machine_compatible_match "$tmp/calibrate.calls" ||
	die "qmp_ufs_phy_calibrate() in $build/vmlinux still asks of_machine_compatible_match(): the board gate"
gate_check="qmp_ufs_phy_calibrate() in vmlinux (build ID ${new_id:0:12}) calls no of_machine_compatible_match()"
if [ -n "${HANG_SCRATCH:-}" ]; then
	[ -f "$HANG_SCRATCH/linux-gts9wifi/vmlinux" ] || die "no vmlinux in $HANG_SCRATCH"
	[ "$(notes_build_id "$HANG_SCRATCH/linux-gts9wifi/vmlinux")" = "$hang_build_id" ] ||
		die "self-test failed: $HANG_SCRATCH/linux-gts9wifi/vmlinux is not the hang-log kernel's"
	calls_of "$HANG_SCRATCH/linux-gts9wifi/vmlinux" qmp_ufs_phy_calibrate > "$tmp/calibrate.hang.calls"
	grep -qx of_machine_compatible_match "$tmp/calibrate.hang.calls" ||
		die 'self-test failed: the hang-log calibration does not show the board gate either'
	gate_check="$gate_check; the hang-log build's does"
fi
echo "UFS PHY: $gate_check"

# 5c. Revision 10's PCIe PIPE clock, which leaves no string.  With the
# revert the mux has its gate operations again and not the rework's rate
# ones, and without unpark-pcie0-pipe-mux.patch the PCIe PHY's power-on
# sets no clock rate (it is inlined into qmp_pcie_enable(), which has to
# call clk_bulk_enable(): that shows the function was found).  From the
# vmlinux the image was made from (5b).  With HANG_SCRATCH the hang-log
# build's vmlinux has to show the opposite of each.
llvm-nm --defined-only "$build/vmlinux" > "$tmp/vmlinux.nm"
nm_has() { grep -q " [tT] $2\$" "$1"; } # NM-LIST FUNCTION
for f in phy_mux_enable phy_mux_disable phy_mux_is_enabled; do
	nm_has "$tmp/vmlinux.nm" "$f" || die "no $f in $build/vmlinux: the PIPE clock mux revert is not in it"
done
for f in phy_mux_recalc_rate phy_mux_determine_rate phy_mux_set_rate; do
	! nm_has "$tmp/vmlinux.nm" "$f" || die "$f in $build/vmlinux: the PIPE clock mux has the rework's rate operations"
done
calls_of "$build/vmlinux" qmp_pcie_enable > "$tmp/pcie.calls"
grep -qx clk_bulk_enable "$tmp/pcie.calls" ||
	die "no qmp_pcie_enable() calling clk_bulk_enable() in $build/vmlinux"
! grep -qx clk_set_rate "$tmp/pcie.calls" ||
	die "qmp_pcie_enable() in $build/vmlinux calls clk_set_rate(): $unpark"
pcie_check="vmlinux (build ID ${new_id:0:12}) has the PIPE clock mux's gate operations and not its rate ones, and its qmp_pcie_enable() calls no clk_set_rate()"
if [ -n "${HANG_SCRATCH:-}" ]; then
	llvm-nm --defined-only "$HANG_SCRATCH/linux-gts9wifi/vmlinux" > "$tmp/vmlinux.hang.nm"
	nm_has "$tmp/vmlinux.hang.nm" phy_mux_set_rate && ! nm_has "$tmp/vmlinux.hang.nm" phy_mux_enable ||
		die 'self-test failed: the hang-log vmlinux does not have the rework'"'"'s PIPE clock mux'
	calls_of "$HANG_SCRATCH/linux-gts9wifi/vmlinux" qmp_pcie_enable > "$tmp/pcie.hang.calls"
	grep -qx clk_set_rate "$tmp/pcie.hang.calls" ||
		die "self-test failed: the hang-log build's qmp_pcie_enable() does not call clk_set_rate() either"
	pcie_check="$pcie_check; the hang-log build's has the rate operations and calls it"
fi
echo "PCIe PIPE clock: $pcie_check"

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

# 6b. Revision 9's debug switches as the kernel knows them, with revision
# 11's debug_boot_state and the root port's switch and revision 12's CXPC
# log switch: built in, so each parameter's description is in the module
# tree's modules.builtin.modinfo under its object's name, where the hang-log
# build's has none; and the build's vmlinux, the image's by its build ID
# (5b), has their handlers (its symbols as 5c listed them), the guard's
# genpd notifier, whose address its probe hands genpd (the guard's other two
# functions are inlined), the CXPC log's PM notifier, and the DSI PHY's
# system sleep callbacks of revision 12 (in its dev_pm_ops).  Their names
# are in the image (4).
tr '\0' '\n' < "$modules/modules.builtin.modinfo" > "$tmp/builtin.modinfo"
tr '\0' '\n' < "$hang_modules/modules.builtin.modinfo" > "$tmp/builtin.hang.modinfo"
for parm in gcc_sm8550.parm=debug_pcie0_off: gcc_sm8550.parm=debug_pcie0_phy_off: \
	gcc_sm8550.parm=debug_usb_off: rpmhpd.parm=debug_ret_floor: gcc_sm8550.parm=debug_boot_state: \
	pcie_qcom.parm=debug_root_port_d3: qcom_stats.parm=debug_cxpc_violators:; do
	grep -q "^$parm" "$tmp/builtin.modinfo" ||
		die "the module tree's modules.builtin.modinfo has no $parm"
	! grep -q "^$parm" "$tmp/builtin.hang.modinfo" ||
		die "self-test failed: the hang-log build's modules.builtin.modinfo has $parm too"
done
for f in gcc_sm8550_debug_set gcc_sm8550_debug_get gcc_sm8550_debug_boot_get rpmhpd_debug_ret_floor_set \
	qcom_pcie_debug_rp_d3_set qcom_pcie_pd_notify qcom_stats_vx_set qcom_stats_vx_pm_notify \
	dsi_phy_suspend_late dsi_phy_resume_early; do
	grep -q " [tT] $f\$" "$tmp/vmlinux.nm" || die "no $f in $build/vmlinux"
done
switch_check="the seven parameters' names in the image and their descriptions in modules.builtin.modinfo, none in the hang-log build's; the set and get handlers, the guard's genpd notifier, the CXPC log's PM notifier and the DSI PHY's system sleep callbacks in vmlinux (build ID ${new_id:0:12})"
echo "debug switches: $switch_check"

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
printf '%s\n' "$revision" > "$kit/REVISION"
# What was built: this checkout, and whether it had uncommitted changes.
{
	echo "revision: $revision"
	echo "checkout: $(git -C "$repo" rev-parse HEAD) ($(git -C "$repo" describe --always --dirty --abbrev=12))"
	echo "uncommitted kernel inputs (git status --porcelain kernel scripts):"
	git -C "$repo" status --porcelain -- kernel scripts | sed 's/^/  /'
	echo "kernel/drivers/sm5714_usbpd.c $(sha "$repo/kernel/drivers/sm5714_usbpd.c")"
	for p in "${patches[@]}"; do
		echo "kernel/patches/$p $(sha "$repo/kernel/patches/$p")"
	done
	echo "scripts/stage-ufs-resume-experiment.sh $(sha "$repo/scripts/stage-ufs-resume-experiment.sh")"
	echo "source check: $source_check"
	echo "power domain table: $gdsc_check"
	echo "UFS PHY board gate: $gate_check"
	echo "PCIe PIPE clock: $pcie_check"
	echo "debug switches: $switch_check"
	echo "left out: kernel/patches/$unpark (APPLY_SM8550_SLEEP_FIXES=1)"
	echo "pcie_0_gdsc: PWRSTS_OFF_ON (kernel/patches/${gdsc_patches[0]}, APPLY_SM8550_PCIE0_GDSC_OFF=1)"
	echo "pcie_0_gdsc guard: GENPD_FLAG_ACTIVE_WAKEUP (kernel/patches/${gdsc_patches[0]}) and the PCIe host on the awake path while it keeps its link (kernel/patches/${pcie_patches[0]}, APPLY_SM8550_PCIE0_GDSC_GUARD=1)"
	echo "root port debug switch: pcie_qcom.debug_root_port_d3, N at boot (kernel/patches/${pcie_patches[1]}, APPLY_DEEP_SLEEP_DEBUG_SWITCHES=1 with APPLY_SM8550_PCIE0_GDSC_GUARD=1)"
	echo "UFS PHY power reference: kept track of by ufs-qcom (kernel/patches/${ufs_qcom_patches[0]}, every build), its counts logged (kernel/patches/${ufs_qcom_patches[1]}, APPLY_QMP_UFS_PCS_STATE_DIAGNOSTIC=1)"
	echo "CXPC violators log: qcom_stats.debug_cxpc_violators, N at boot (kernel/patches/${debug_patches[2]}, APPLY_DEEP_SLEEP_DEBUG_SWITCHES=1)"
	echo "VA macro: its LPASS votes released when idle (kernel/patches/${xo_patches[0]}, APPLY_LPASS_VA_MACRO_PM_CLK=1)"
	echo "DSI PHY: its AHB clock unprepared in system sleep (kernel/patches/${xo_patches[1]}, APPLY_DSI_PHY_AHB_RELEASE=1)"
	echo "left out: kernel/patches/pcie-qcom-root-port-d3-in-system-suspend.patch (APPLY_SM8550_ROOT_PORT_D3 not set)"
} > "$kit/SOURCE"
install -m 0644 "$here/install.sh" "$here/rollback.sh" "$here/verify.sh" "$here/sleep-test.sh" \
	"$here/ufs-phy-power-check.sh" "$here/README.md" "$kit/"
files=(boot.img "modules-$release.tar" BUILD_ID KEY_ID SIGNER REVISION SOURCE
	install.sh rollback.sh verify.sh sleep-test.sh ufs-phy-power-check.sh README.md)
(cd "$kit" && sha256sum "${files[@]}" > SHA256SUMS)
cat "$kit/SHA256SUMS"
