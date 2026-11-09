#!/bin/bash
# Build the mainline kernel, device tree, ath12k modules and the camera V4L2
# bridge module for the SM-X910 from pinned sources.
#
# Derived from scripts/build-mainline-kernel.sh of the postmarketOS gts9uwifi
# port (MIT). Only the source layout and output paths changed: the pinned
# upstream checkout stays pristine and every device source is applied here.
#
# Nothing is flashed and no device is touched.
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
. "$repo/scripts/lib/device.sh"
base=${UBUNTU_WORKDIR:-/root/ubuntu-gts9u}
kernel_src=${KERNEL_SRC:-$base/linux-mainline}
kernel_tree=${KERNEL_WORKTREE:-$base/build/linux-src-$DEVICE}
build_dir=${KERNEL_BUILD_DIR:-$base/build/linux-$DEVICE}
# Out-of-tree module scratch trees sit next to the kernel build directory and
# are named per device: two boards must never clean each other's trees, and
# a build run as another user must not trip over the last one's files.
scratch=$(dirname "$build_dir")
out_dir=${KERNEL_OUT_DIR:-$base/out/kernel-$DEVICE}
v4l2loopback_commit=9ef83fb9bc88e8f841786753c362ac52c580defc
fingerprint_baseline=5046f92f507d80b13d2e25c53a5d743861ba5a97
# The complete EL721/QTEE path is physically validated for v1.2.0. Keep the
# old variable as an explicit development opt-out, while release builds enable
# the working fingerprint stack without relying on an undocumented shell flag.
enable_fingerprint=${ENABLE_FINGERPRINT_EXPERIMENTAL:-1}
fingerprint_panel=${FINGERPRINT_PANEL_FOD:-$enable_fingerprint}
fingerprint_touch=${FINGERPRINT_TOUCH_FOD:-$enable_fingerprint}
fingerprint_sensor=${FINGERPRINT_EL721:-$enable_fingerprint}
qtee_admin_null_credentials=${QCOMTEE_ADMIN_NULL_CREDENTIALS:-0}
enable_gunyah_cma=${ENABLE_GUNYAH_CMA:-0}

validate_bool() {
	case "$2" in
		0|1) ;;
		*) echo "$1 must be 0 or 1" >&2; exit 1 ;;
	esac
}

validate_bool ENABLE_FINGERPRINT_EXPERIMENTAL "$enable_fingerprint"
validate_bool FINGERPRINT_PANEL_FOD "$fingerprint_panel"
validate_bool FINGERPRINT_TOUCH_FOD "$fingerprint_touch"
validate_bool FINGERPRINT_EL721 "$fingerprint_sensor"
validate_bool QCOMTEE_ADMIN_NULL_CREDENTIALS "$qtee_admin_null_credentials"
validate_bool ENABLE_GUNYAH_CMA "$enable_gunyah_cma"

# Linux 7.2 requires Clang >= 17.  Prefer the versioned LLVM toolchain when it
# is installed (the imported baseline was generated with LLVM 22), while still
# allowing CI/builders that already expose a suitable unversioned clang.
if [ -x /usr/lib/llvm-22/bin/clang ]; then
	export PATH=/usr/lib/llvm-22/bin:$PATH
fi

# The build directory is reused between runs, which is what makes an ordinary
# rebuild quick.  It also makes the resulting image depend on what was in the
# tree beforehand: on 2026-08-04, building v0.11 from sources identical to the
# ones behind the running kernel produced a different boot.img, and this was
# why.  A release that cannot be reproduced from its own tree is not a release.
#
# KERNEL_CLEAN=1 discards both the object directory and the disposable source
# worktree.  Removing only the objects is not enough: apply_unless() makes the
# source tree accumulate every patch ever tested, so an A/B build can otherwise
# claim to omit a patch while still compiling the copy left by an earlier run.
# Slow, so it is opt-in, but it is what a release build and any source-level
# comparison require.
if [ "${KERNEL_CLEAN:-0}" = 1 ] && [ -d "$build_dir" ]; then
	# The old key may be the only one a flashed kernel trusts.
	if [ -f "$build_dir/certs/signing_key.pem" ]; then
		bash "$repo/scripts/kernel-key-registry.sh" add "$build_dir" \
			"archived before KERNEL_CLEAN" || {
			echo "not discarding $build_dir: its signing key is not archived" >&2
			exit 1
		}
	fi
	echo "KERNEL_CLEAN=1: discarding $build_dir for a from-scratch build"
	rm -rf -- "$build_dir"
fi
if [ "${KERNEL_CLEAN:-0}" = 1 ] && [ -e "$kernel_tree/.git" ]; then
	echo "KERNEL_CLEAN=1: recreating disposable source worktree from pinned HEAD"
	git -C "$kernel_src" worktree remove --force "$kernel_tree"
	git -C "$kernel_src" worktree prune
fi

dts=$repo/kernel/dts
drv=$repo/kernel/drivers
pat=$repo/kernel/patches
cfg=$repo/kernel/config

test -d "$kernel_src/.git" || {
	echo "missing pinned kernel checkout: $kernel_src" >&2
	echo 'run scripts/fetch-mainline.sh first' >&2
	exit 1
}
test -f "$dts/$DEVICE_DTS.dts"
test -f "$cfg/config-mainline.aarch64"
test -f "$cfg/config-$DEVICE.fragment"
test -f "$pat/gunyah-host-vm-manager.patch"
test -f "$pat/gunyah-qtvm-auth.patch"
test -f "$pat/gunyah-qcom-runtime-overlay.patch"
test -f "$repo/packaging/v4l2loopback/patches/0001-backward-compatible-client-usage-event.patch"
test -f "$repo/packaging/v4l2loopback/patches/0002-fix-buffer-queue-management.patch"
test -f "$repo/packaging/v4l2loopback/patches/0003-preserve-output-queue-for-capture.patch"
test -f "$repo/kernel/include/linux/samsung_wacom.h"
test -f "$drv/egis_el721.c"
test -f "$drv/qcom_spss.c"
test -f "$drv/qcom_glink_spss.c"
test -f "$drv/spcom.c"
test -f "$drv/spss_utils.c"
test -f "$drv/panel-samsung-ana38407-glut.h"
test -f "$repo/kernel/include/linux/remoteproc/qcom_spss.h"
test -f "$repo/kernel/include/uapi/linux/spcom.h"
test -f "$repo/kernel/include/uapi/linux/spss_utils.h"

mkdir -p "$(dirname "$kernel_tree")" "$build_dir" "$out_dir"

if [ ! -e "$kernel_tree/.git" ]; then
	git -C "$kernel_src" worktree add --detach "$kernel_tree" HEAD
fi

# ---------------------------------------------------------------------------
# Board device tree
# ---------------------------------------------------------------------------

board_dts=$kernel_tree/arch/arm64/boot/dts/qcom/$DEVICE_DTS.dts
# Keep the exact last physically booted board description for every build.
# Adding the EL721 GPIO description to vendor_boot makes Samsung ABL reset before
# Linux can persist a log.  The restricted EL721 compatibility device is
# therefore created by its driver after boot and does not mutate the DTB.
# Measured on the tablet: ABL bootloops on any DTB whose structure moves,
# whatever the addition is.  Two inert nodes did it, and so did adding them
# with their phandle pinned so nothing else renumbered.  Repacking
# vendor_boot with the untouched tree reproduces the working image to the
# byte -- one cosmetic byte of avbtool version aside -- so the packing is
# faithful and the tree itself is what ABL rejects.  The baseline stays
# pinned until there is a route that does not go through the DTB.
if [ "$DEVICE" = gts9uwifi ]; then
	git -C "$repo" show \
		"$fingerprint_baseline:kernel/dts/sm8550-samsung-gts9uwifi.dts" \
		> "$board_dts"
else
	# No tree has been physically booted on this board yet, so there is no
	# baseline to pin: the working copy is the bring-up tree.
	install -m 0644 "$repo/kernel/dts/$DEVICE_DTS.dts" "$board_dts"
fi

dts_makefile=$kernel_tree/arch/arm64/boot/dts/qcom/Makefile
if ! grep -q 'sm8550-samsung-gts9uwifi.dtb' "$dts_makefile"; then
	patch -d "$kernel_tree" -p1 < "$pat/add-gts9uwifi-dtb.patch"
fi
# ABL's ufdt fork requires /__symbols__ in any DTB destined for vendor_boot.
if ! grep -q '^DTC_FLAGS_sm8550-samsung-gts9uwifi := -@$' "$dts_makefile"; then
	sed -i '/sm8550-samsung-gts9uwifi\.dtb/a DTC_FLAGS_sm8550-samsung-gts9uwifi := -@' \
		"$dts_makefile"
fi
# The other family boards hang off the Ultra's entry, with the same flag.
if ! grep -q "$DEVICE_DTS.dtb" "$dts_makefile"; then
	sed -i "/sm8550-samsung-gts9uwifi\.dtb/a dtb-\$(CONFIG_ARCH_QCOM) += $DEVICE_DTS.dtb" \
		"$dts_makefile"
fi
if ! grep -q "^DTC_FLAGS_$DEVICE_DTS := -@\$" "$dts_makefile"; then
	sed -i "/$DEVICE_DTS\.dtb/a DTC_FLAGS_$DEVICE_DTS := -@" "$dts_makefile"
fi

# ---------------------------------------------------------------------------
# Hardware patches, each guarded so re-running is idempotent
# ---------------------------------------------------------------------------

# Keep the ABL-facing DTB pinned; Linux adds Galaxy CPU/GPU OPPs before probe.
bash "$repo/scripts/stage-performance-overlay.sh" "$kernel_tree"

apply_unless() {
	# apply_unless <marker> <file> <patch>
	local marker=$1 file=$2 patch=$3
	if ! grep -q "$marker" "$kernel_tree/$file"; then
		patch -d "$kernel_tree" -p1 < "$pat/$patch"
	fi
}

apply_unless 'Use a boost-independent frequency reference' \
	drivers/base/arch_topology.c arm-topology-use-boost-frequency-reference.patch

apply_unless 'Recompute the software boost limit' \
	drivers/cpufreq/cpufreq.c cpufreq-recompute-software-boost-limit.patch

# The deferred-probe timeout fires ten seconds after the last driver
# registration, from a workqueue the freezer leaves running, and on both
# boards that lands inside the boot-time pm_test=platform panel cycle.  Its
# retry moved suspended devices back onto dpm_list, so they never got their
# resume and complete callbacks: on the X710 the LPASS LPI pinctrl kept
# runtime PM disabled and every DMIC pin request failed with -EACCES.  While
# probing is blocked, do not retry deferred devices, and run the timeout
# itself only after devices have resumed.
apply_unless 'device_pm_move_to_tail() would take devices' \
	drivers/base/dd.c driver-core-no-deferred-retry-while-probing-blocked.patch
apply_unless 'deferred_probe_timeout_postponed' \
	drivers/base/dd.c driver-core-postpone-deferred-probe-timeout-while-blocked.patch

# Linux 7.2-rc3 only contains the Gunyah watchdog. Import the host-side
# Resource Manager and VM-manager stack before enabling it below. The source
# snapshot and the small 7.2 API adaptations are recorded in PROVENANCE.md.
if [ ! -f "$kernel_tree/drivers/virt/gunyah/rsc_mgr.c" ]; then
	patch -d "$kernel_tree" -p1 < "$pat/gunyah-host-vm-manager.patch"
fi
if ! grep -q 'GH_VM_ANDROID_SET_AUTH_TYPE' \
	"$kernel_tree/include/uapi/linux/gunyah.h"; then
	git -C "$kernel_tree" apply --recount "$pat/gunyah-qtvm-auth.patch"
fi
if [ ! -f "$kernel_tree/drivers/virt/gunyah/qcom_bootinfo.c" ]; then
	patch -d "$kernel_tree" -p1 < "$pat/gunyah-qcom-runtime-overlay.patch"
fi
apply_unless 'This legacy firmware call is verified only on the SM-X910' \
	drivers/virt/gunyah/qcom_bootinfo.c gunyah-qcom-runtime-overlay-platform-guard.patch
apply_unless 'Never issue a private SMC without a probed SCM provider' \
	drivers/virt/gunyah/gunyah_qcom.c gunyah-qcom-platform-scm-guard.patch
if [ "$enable_gunyah_cma" = 1 ]; then
	if [ ! -f "$kernel_tree/drivers/virt/gunyah/vm_mgr_cma_mem.c" ]; then
		git -C "$kernel_tree" apply --recount "$pat/gunyah-qtvm-cma.patch"
	fi
elif [ -f "$kernel_tree/drivers/virt/gunyah/vm_mgr_cma_mem.c" ]; then
	echo 'CMA experiment remains in this source tree; use a fresh KERNEL_WORKTREE/KERNEL_BUILD_DIR for a default build.' >&2
	exit 1
fi

if [ ! -f "$kernel_tree/drivers/soc/qcom/samsung-gts9uwifi-sec-log.c" ]; then
	patch -d "$kernel_tree" -p1 < "$pat/add-samsung-sec-log-console.patch"
fi
apply_unless 'previous_index, index' \
	drivers/soc/qcom/samsung-gts9uwifi-sec-log.c \
	keep-sec-log-previous-index-current.patch

# ignore-console-null.patch is deliberately NOT applied by default.  The
# postmarketOS v1.71 kernel that was physically validated came from the direct
# build, which never applied it; only the Alpine package did.  It is a
# diagnostic aid that lets tty0/ttyMSM0 survive the `console=null` that ABL can
# append, and it cannot show anything while the DDIC still reads 00 00 00.
# Keep the first Ubuntu kernel byte-comparable to the validated one; set
# APPLY_IGNORE_CONSOLE_NULL=1 only for an explicit diagnostic build.
if [ "${APPLY_IGNORE_CONSOLE_NULL:-0}" = 1 ]; then
	apply_unless 'ignore_console_null' \
		kernel/printk/printk.c ignore-console-null.patch
fi

apply_unless 'Match Samsung SM8550 sequencing' \
	drivers/phy/phy-snps-eusb2.c match-samsung-sm8550-eusb2-phy-init.patch
apply_unless 'static void qmp_ufs_tx_pull_down_ctrl' \
	drivers/phy/qualcomm/phy-qcom-qmp-ufs.c \
	qmp-ufs-reset-serdes-before-power-down-gts9u.patch
bash "$repo/scripts/stage-ufs-resume-experiment.sh" "$kernel_tree"
bash "$repo/scripts/stage-gpu-recovery-experiment.sh" "$kernel_tree"
apply_unless 'Late-created SM-X910 DSP endpoints' \
	drivers/mailbox/qcom-ipcc.c ipcc-reserve-gts9u-runtime-channels.patch
apply_unless 'Mask IPCC DSP traffic during SM-X910 system suspend' \
	drivers/mailbox/qcom-ipcc.c ipcc-mask-summary-during-suspend-gts9u.patch
# Same firmware stack on the X710: extend the suspend mask to gts9wifi.
apply_unless '!of_machine_is_compatible("samsung,gts9wifi")' \
	drivers/mailbox/qcom-ipcc.c ipcc-mask-summary-during-suspend-gts9wifi.patch
# SM8550's top UFS OPP has a 300 MHz UniPro clock, which mainline maps to
# HS-G4; the UFS 4.0 part, the controller and the PHY all do HS-G5, as the
# Kalama driver runs it at 300 MHz.  On the SM-X710 it ran G5 Rate-A at
# 3.48 GB/s and passed a 30-minute stress run (2026-11-07); the SM-X910 is
# being tried with it, together with its UFS PCS reset (2026-11-08).
apply_unless 'it runs HS-G5' \
	drivers/ufs/host/ufs-qcom.c ufs-qcom-hs-g5-at-300mhz-sm8550.patch
apply_unless 'Users may open the misc device as soon as it is registered' \
	drivers/misc/fastrpc.c fastrpc-publish-after-session-init.patch
apply_unless 'qcom,early-tx-intent' \
	drivers/rpmsg/qcom_glink_native.c qcom-glink-early-tx-intent.patch
apply_unless 'qcom,early-glink' \
	drivers/remoteproc/qcom_common.c qcom-early-glink-prepare.patch
apply_unless 'qcom,no-auto-boot' \
	drivers/remoteproc/qcom_q6v5_pas.c qcom-pas-opt-out-auto-boot.patch
apply_unless 'qcom,handover-delay-ms' \
	drivers/remoteproc/qcom_q6v5_pas.c qcom-pas-handover-grace.patch
apply_unless 'PTN3222_MAX_INIT_CELLS' \
	drivers/phy/phy-nxp-ptn3222.c configure-nxp-ptn3222-from-dt.patch
apply_unless 'pwrseq_qcom_wcn_program_wlan_pdc' \
	drivers/power/sequencing/pwrseq-qcom-wcn.c \
	wcn7850-pwrseq-cold-reset-aop.patch
apply_unless 'pwrseq_qcom_wcn6855_clk_release' \
	drivers/power/sequencing/pwrseq-qcom-wcn.c \
	pwrseq-wcn6855-cold-reset-hold-xo-gts9wifi.patch
apply_unless 'default y if ARCH_QCOM' \
	drivers/pci/pwrctrl/Kconfig build-wcn-pcie-providers-in.patch
apply_unless 'clk_set_rate(qmp->pipe_clks\[0\].clk, ULONG_MAX)' \
	drivers/phy/qualcomm/phy-qcom-qmp-pcie.c unpark-pcie0-pipe-mux.patch
apply_unless 'Match the SM-X910 Samsung kernel at standard mode' \
	drivers/i2c/busses/i2c-qcom-geni.c \
	match-samsung-geni-i2c-100khz-timing.patch
apply_unless "Samsung's SM8550 driver cancels first" \
	drivers/i2c/busses/i2c-qcom-geni.c \
	qcom-geni-cancel-before-abort.patch
apply_unless 'sc8280xp_snd_startup' \
	sound/soc/qcom/sc8280xp.c set-mi2s-codec-dai-format.patch
apply_unless 'ret != -ENODEV && ret != -EPROBE_DEFER' \
	drivers/gpu/drm/msm/dp/dp_display.c \
	msm-dp-allow-unresolved-usbc-bridge.patch
apply_unless 'bridge->of_node = msm_dp_display->pdev->dev.of_node' \
	drivers/gpu/drm/msm/dp/dp_drm.c msm-dp-associate-bridge-of-node.patch
apply_unless 'defer_hpd_until_resume' \
	drivers/gpu/drm/msm/dp/dp_drm.h msm-dp-defer-oob-hpd-until-resume.patch
# Both boards get DP HPD out of band from typec_displayport, but msm_dp's
# hpd_enable held the controller resumed for its unused HPD pin, which kept
# MDSS, mdss_gdsc and dispcc powered for the whole uptime.  Power it only
# while a sink is plugged, with one runtime PM reference per plugged sink.
apply_unless 'hpd_out_of_band' \
	drivers/gpu/drm/msm/dp/dp_drm.h msm-dp-oob-hpd-no-runtime-pm.patch
apply_unless 'was_plugged' \
	drivers/gpu/drm/msm/dp/dp_display.c msm-dp-balance-plugged-runtime-pm.patch
# Bring-up switch: msm.dsi_phy_timings=<14 bytes> replays a vendor D-PHY
# timing table (the X710 glass shows speckle after idle gaps with the values
# mainline derives from the lane rate).  Inert without the parameter.
apply_unless 'dsi_phy_timings' \
	drivers/gpu/drm/msm/dsi/phy/dsi_phy_7nm.c msm-dsi-phy-7nm-timing-override.patch
# The same table as board data: qcom,dsi-phy-timings on the PHY node is used
# while the lane rate is within 1% of qcom,dsi-phy-timings-bitclk-hz.  Only
# the X710 DTS carries it; the Ultra's PHY keeps computing its timings.
apply_unless 'qcom,dsi-phy-timings-bitclk-hz' \
	drivers/gpu/drm/msm/dsi/phy/dsi_phy_7nm.c msm-dsi-phy-7nm-dt-timings.patch
# Bring-up switches for the panel that reads 00 00 00 after a cold boot until
# a pm_test=platform cycle; all inert without their parameter.
# msm.dsi_regdump=1 logs DSI controller/PHY registers at probe and every
# enable, msm.mdss_handoff_gdsc_cycle=1 power-cycles the MDSS GDSC at probe,
# msm.boot_display_cycle=1 power-cycles the display subsystem once after the
# first modeset.
apply_unless 'dsi_regdump' \
	drivers/gpu/drm/msm/dsi/dsi_manager.c msm-dsi-handoff-regdump.patch
apply_unless 'mdss_handoff_gdsc_cycle' \
	drivers/gpu/drm/msm/msm_mdss.c msm-mdss-handoff-gdsc-cycle.patch
apply_unless 'boot_display_cycle' \
	drivers/gpu/drm/msm/msm_kms.c msm-kms-boot-display-cycle.patch
# Bring-up switch: msm.cached_coherent=0 withholds cached-coherent GEM
# buffers from Mesa (the X710 shows a 96-px lattice of unwritten/stale
# pixels in GPU-rendered content).  Inert without the parameter.
apply_unless 'static bool cached_coherent' \
	drivers/gpu/drm/msm/adreno/adreno_device.c msm-adreno-cached-coherent-param.patch
# Bring-up switches for the same lattice: msm.gpu_hwcg=0 leaves the GPU's
# hardware clock gating off, msm.gpu_llc=0 keeps the GPU off the system
# cache (LLCC) slices.  Inert without the parameters.
apply_unless 'bool gpu_hwcg' \
	drivers/gpu/drm/msm/adreno/adreno_device.c msm-adreno-hwcg-llc-params.patch
# Bring-up switch: msm.a7xx_ccu_inval selects what a7xx_submit() does to the
# colour/depth caches before the completion fence (0 none, 1 invalidate as
# mainline, 2 clean+idle then invalidate).  Inert without the parameter.
apply_unless 'a7xx_ccu_inval' \
	drivers/gpu/drm/msm/adreno/adreno_device.c msm-adreno-a7xx-ccu-inval-param.patch
# Bring-up switch: msm.gpu_scm_init=<bits> re-issues the secure-world GPU
# register init request on the A740 (mainline dropped it).  Inert at 0.
apply_unless 'gpu_scm_init' \
	drivers/gpu/drm/msm/adreno/adreno_device.c msm-adreno-a740-scm-init-param.patch
# Bring-up switches: msm.gmu_hub_hz=<Hz> overrides the GPU hub clock rate;
# gunyah.qcom_overlay_all=1 lets the Gunyah RM overlay be fetched on machines
# other than the SM-X910.  Both inert by default.
apply_unless 'gmu_hub_hz' \
	drivers/gpu/drm/msm/adreno/adreno_device.c msm-adreno-gmu-hub-clk-param.patch
apply_unless 'qcom_overlay_all' \
	drivers/virt/gunyah/qcom_bootinfo.c gunyah-qcom-runtime-overlay-all-param.patch
apply_unless 'adopt_retained_source_ufp' \
	include/linux/usb/tcpm.h tcpm-adopt-retained-source-ufp-role.patch
apply_unless 'consume_retained_sink_dfp' \
	include/linux/usb/tcpm.h tcpm-use-retained-sink-data-role.patch
# A dock's DisplayPort alternate mode is discovered while the tablet is still
# UFP; typec_displayport then refuses to bind (-EPROTO) and the mode sat
# driverless until a physical re-plug.  Impatient adapters hard-reset around
# that window forever.  Re-probe driverless partner altmodes when the data
# role changes.
apply_unless 'Re-probe driverless partner alternate modes' \
	drivers/usb/typec/class.c \
	typec-reprobe-partner-altmodes-on-role-change.patch
# In USB host role the xHCI refuses system suspend (-EINVAL: root hubs not
# suspended).  ubuntu-gts9u-usb-role (a system-sleep hook) works around it
# for hubs and docks with nothing attached; this logs the root hub states
# whenever it still happens, to find the cause.
apply_unless 'suspend refused: root hub states' \
	drivers/usb/host/xhci.c xhci-say-why-suspend-is-refused.patch
apply_unless 'disable_irq_nosync(irq)' \
	drivers/remoteproc/qcom_q6v5.c \
	qcom-q6v5-mask-completed-handover-irq.patch
apply_unless 'soc_marketing_names' \
	arch/arm64/kernel/cpuinfo.c \
	arm64-report-soc-marketing-name.patch
if ! grep -q 'QCOMTEE_SHM_POOL_MAX_SIZE' \
	"$kernel_tree/drivers/tee/qcomtee/shm.c"; then
	git -C "$kernel_tree" apply --recount \
		"$pat/qcomtee-use-tzmem-pool.patch"
fi
# The BAUTH application registers the two non-secure buffers with QTEE itself,
# and that only succeeds when the range is a whole SHM bridge, so large memory
# objects get their own contiguous DMA32 allocation and their own bridge.
if ! grep -q 'qcomtee_bridged_alloc' \
	"$kernel_tree/drivers/tee/qcomtee/shm.c"; then
	git -C "$kernel_tree" apply --recount \
		"$pat/qcomtee-bridge-large-objects.patch"
fi
# Samsung keymaster bootstraps the SPU through a carveout shared exclusively
# by TrustZone and CP_SPSS_SP_SHARED.  Publish that existing reserved-memory
# range as a protected TEE DMA heap so both QTEE and SPCOM retain the same
# physical allocation.
apply_unless 'TEE_DMA_HEAP_SPSS_SHARED' \
	include/linux/tee_core.h qcomtee-add-spss-shared-heap.patch
if [ "$qtee_admin_null_credentials" = 1 ]; then
	# Diagnostic only.  This reproduces Samsung's in-kernel QSEECom client
	# environment for a CAP_SYS_ADMIN process; it is not part of release builds.
	apply_unless 'capable(CAP_SYS_ADMIN)' \
		drivers/tee/qcomtee/call.c qcomtee-allow-admin-null-credentials.patch
fi
# TA discovery and loading live in the userspace QTEE bridge.  Keep the kernel
# transport generic: the old module parameters were diagnostic-only and
# hard-coded the `fingerpr` engine instead of Samsung's EL721 `dualfp` TA.

# Upstream HI847 currently only binds through ACPI and assumes platform power
# resources.  The X910 exposes it through CCI/DT and needs explicit VDDIO,
# module-enable, reset and MCLK sequencing.
if ! grep -q 'Samsung.s module must remain powered' \
	"$kernel_tree/drivers/media/i2c/hi847.c"; then
	git -C "$kernel_tree" apply --recount "$pat/hi847-add-devicetree-power.patch"
fi
apply_unless 'Export the DT rotation and front/back location to libcamera' \
	drivers/media/i2c/hi847.c hi847-libcamera-compliance.patch

# The Goodix patch has two variants: a full one for a pristine tree and an
# upgrade for a tree that already carries the partial Samsung decoder.
goodix=drivers/input/touchscreen/goodix_berlin_core.c
if ! grep -q 'forcing 16-byte Samsung events for firmware PID 6936' \
	"$kernel_tree/$goodix"; then
	if grep -q 'GOODIX_BERLIN_SAMSUNG_EVENT_ID_MASK' "$kernel_tree/$goodix"; then
		patch -d "$kernel_tree" -p1 \
			< "$pat/upgrade-partial-goodix-samsung-events.patch"
	else
		patch -d "$kernel_tree" -p1 \
			< "$pat/support-samsung-goodix-16-byte-events.patch"
	fi
fi
install -m 0644 "$repo/kernel/include/linux/samsung_wacom.h" \
	"$kernel_tree/include/linux/samsung_wacom.h"
apply_unless 'samsung_wacom_should_suppress_touch' \
	drivers/input/touchscreen/goodix_berlin_core.c \
	suppress-goodix-touch-while-spen-hovering.patch
apply_unless 'samsung,gts9wifi' \
	drivers/pci/controller/dwc/pcie-designware.c \
	pcie-dwc-link-wait-longer-gts9wifi.patch
if [ "$fingerprint_touch" = 1 ]; then
	apply_unless 'GOODIX_BERLIN_SPONGE_FOD_RECT' \
		drivers/input/touchscreen/goodix_berlin_core.c \
		support-goodix-samsung-fod.patch
	# Existing incremental trees already contain the older FOD patch.  Clean
	# trees get this behavior from that patch directly; this small upgrade keeps
	# incremental test builds byte-for-byte equivalent at the source level.
	apply_unless 'starts while the finger is down instead of at the later RELEASE' \
		drivers/input/touchscreen/goodix_berlin_core.c \
		synthesize-goodix-fod-press.patch
	apply_unless 'failed to disable FOD mode at suspend' \
		drivers/input/touchscreen/goodix_berlin_core.c \
		cleanup-goodix-fod-on-suspend.patch
	apply_unless 'Keep suppressed contacts until their coordinate RELEASE' \
		drivers/input/touchscreen/goodix_berlin_core.c \
		retain-goodix-fod-contact-until-release.patch
	# Double tap to wake arms Samsung's gesture through the same sponge: it
	# needs the sponge address, the command helper and the lock that
	# support-goodix-samsung-fod.patch adds, so it lives in this block.  The
	# double_tap_to_wake attribute is off by default and is created only where
	# the firmware publishes the sponge (the SM-X910's GT6936).
	apply_unless 'double_tap_to_wake' \
		drivers/input/touchscreen/goodix_berlin_core.c \
		support-goodix-samsung-double-tap-wake.patch
fi

# Xorg only creates a PRIME GPU screen when MODE_GETRESOURCES succeeds.  Keep
# the split GPU/DPU topology, but expose an empty KMS resource list on Adreno.
# GNOME/Wayland does not need the Xorg side, but the empty resource list is
# also what lets the render-only Adreno node coexist with the DPU.
msm_drv=drivers/gpu/drm/msm/msm_drv.c
if sed -n '/static const struct drm_driver msm_gpu_driver/,/^};/p' \
	"$kernel_tree/$msm_drv" | grep -q 'DRIVER_FEATURES_GPU,$'; then
	patch -d "$kernel_tree" -p1 < "$pat/expose-separate-gpu-kms-resources.patch"
elif ! grep -q 'msm_gpu_mode_config_funcs' "$kernel_tree/$msm_drv"; then
	echo 'separate GPU framebuffer hook missing; refusing to build' >&2
	exit 1
fi

# ---------------------------------------------------------------------------
# Out-of-tree drivers, shipped into the pristine tree with their Kconfig and
# Makefile entries, exactly as the reference port does.
# ---------------------------------------------------------------------------

camera_dir=$kernel_tree/drivers/media/i2c
install -m 0644 "$drv/hi1337_gts9u.c" "$camera_dir/hi1337_gts9u.c"
install -m 0644 "$drv/hi1337_gts9u_tables.h" \
	"$camera_dir/hi1337_gts9u_tables.h"
install -m 0644 "$drv/dw9808_vcm.c" "$camera_dir/dw9808_vcm.c"
if ! grep -q 'VIDEO_HI1337_GTS9U' "$camera_dir/Kconfig"; then
	sed -i '/^config VIDEO_HI847/i \
config VIDEO_HI1337_GTS9U\
\ttristate "Hynix HI1337 sensors on Samsung Galaxy Tab S9 Ultra"\
\tdepends on I2C && VIDEO_DEV\
\tselect V4L2_FWNODE\
\thelp\
\t  Four-lane RAW10 HI1337 camera modules used by the SM-X910.\
' "$camera_dir/Kconfig"
fi
grep -q 'hi1337_gts9u.o' "$camera_dir/Makefile" || \
	printf 'obj-$(CONFIG_VIDEO_HI1337_GTS9U) += hi1337_gts9u.o\n' \
		>> "$camera_dir/Makefile"
if ! grep -q 'VIDEO_DW9808_VCM' "$camera_dir/Kconfig"; then
	sed -i '/^config VIDEO_DW9807_VCM/i \
config VIDEO_DW9808_VCM\
\ttristate "Dongwoon DW9808 voice coil lens"\
\tdepends on I2C && VIDEO_DEV\
\tselect MEDIA_CONTROLLER\
\tselect VIDEO_V4L2_SUBDEV_API\
\thelp\
\t  Focus actuator used by the rear HI1337 camera on the SM-X910.\
' "$camera_dir/Kconfig"
fi
grep -q 'dw9808_vcm.o' "$camera_dir/Makefile" || \
	printf 'obj-$(CONFIG_VIDEO_DW9808_VCM) += dw9808_vcm.o\n' \
		>> "$camera_dir/Makefile"
# The SM-X710's camera nodes are disabled in its DTB; this built-in switch
# turns them on at runtime, on request only (docs/gts9wifi-cameras.md).
# X710 trees only: on the Ultra even an unset Kconfig symbol would add a line
# to its .config, which CONFIG_IKCONFIG embeds in the Image, and the gts9uwifi
# build must stay byte-for-byte what it was (scripts/lib/device.sh).
if [ "$DEVICE" = gts9wifi ]; then
	install -m 0644 "$drv/gts9wifi_camera.c" "$camera_dir/gts9wifi_camera.c"
	if ! grep -q 'VIDEO_GTS9WIFI_CAMERA' "$camera_dir/Kconfig"; then
		sed -i '/^config VIDEO_HI847/i \
config VIDEO_GTS9WIFI_CAMERA\
\tbool "Opt-in camera switch for the Samsung Galaxy Tab S9 (SM-X710)"\
\tdepends on OF_DYNAMIC\
\thelp\
\t  Enables the disabled SM-X710 camera nodes at runtime through\
\t  /sys/module/gts9wifi_camera/parameters/enable.\
' "$camera_dir/Kconfig"
	fi
	grep -q 'gts9wifi_camera.o' "$camera_dir/Makefile" || \
		printf 'obj-$(CONFIG_VIDEO_GTS9WIFI_CAMERA) += gts9wifi_camera.o\n' \
			>> "$camera_dir/Makefile"
fi

panel_dir=$kernel_tree/drivers/gpu/drm/panel
# The pinned baseline driver only knows the Ultra's AMSA46AS02 glass.
if [ "$fingerprint_panel" = 1 ] || [ "$DEVICE" != gts9uwifi ]; then
	install -m 0644 "$drv/panel-samsung-ana38407.c" \
		"$panel_dir/panel-samsung-ana38407.c"
else
	git -C "$repo" show \
		"$fingerprint_baseline:kernel/drivers/panel-samsung-ana38407.c" \
		> "$panel_dir/panel-samsung-ana38407.c"
fi
# Samsung's 60 Hz gamma tables for the 11" glass (generated by
# scripts/gen-ana38407-glut.py); the pinned baseline driver ignores it.
install -m 0644 "$drv/panel-samsung-ana38407-glut.h" \
	"$panel_dir/panel-samsung-ana38407-glut.h"
if ! grep -q 'DRM_PANEL_SAMSUNG_ANA38407' "$panel_dir/Kconfig"; then
	sed -i '/^endmenu$/i \
config DRM_PANEL_SAMSUNG_ANA38407\
\ttristate "Samsung ANA38407 (Galaxy Tab S9 family) DSI command-mode panel"\
\tdepends on OF\
\tdepends on DRM_MIPI_DSI\
\tdepends on BACKLIGHT_CLASS_DEVICE\
\thelp\
\t  DSC command-mode DSI panel on the Galaxy Tab S9 Wi-Fi tablets (SM-X710, SM-X910).\
' "$panel_dir/Kconfig"
fi
grep -q 'panel-samsung-ana38407.o' "$panel_dir/Makefile" || \
	printf 'obj-$(CONFIG_DRM_PANEL_SAMSUNG_ANA38407) += panel-samsung-ana38407.o\n' \
		>> "$panel_dir/Makefile"

supply_dir=$kernel_tree/drivers/power/supply
install -m 0644 "$drv/sm5714_battery.c" "$supply_dir/sm5714_battery.c"
if ! grep -q 'BATTERY_SM5714' "$supply_dir/Kconfig"; then
	sed -i '/^endif # POWER_SUPPLY$/i \
config BATTERY_SM5714\
\ttristate "Silicon Mitus SM5714 charger and fuel gauge"\
\tdepends on I2C\
\tdepends on IIO\
\thelp\
\t  Battery state of charge and charging status on boards that drive the\
\t  SM5714 combo PMIC from the AP, such as the Galaxy Tab S9 Ultra Wi-Fi.\
' "$supply_dir/Kconfig"
fi
grep -q 'sm5714_battery.o' "$supply_dir/Makefile" || \
	printf 'obj-$(CONFIG_BATTERY_SM5714)\t+= sm5714_battery.o\n' \
		>> "$supply_dir/Makefile"

install -m 0644 "$drv/sm5440_direct.c" "$supply_dir/sm5440_direct.c"
if ! grep -q 'CHARGER_SM5440_DIRECT' "$supply_dir/Kconfig"; then
	sed -i '/^endif # POWER_SUPPLY$/i \
config CHARGER_SM5440_DIRECT\
\ttristate "Silicon Mitus SM5440 direct charger for Samsung SM-X910"\
\tdepends on I2C\
\tdepends on BATTERY_SM5714\
\thelp\
\t  Conservative PPS-controlled 2:1 direct charging on the Galaxy Tab S9\
\t  Ultra Wi-Fi, with automatic fallback to the SM5714 switching charger.\
' "$supply_dir/Kconfig"
fi
grep -q 'sm5440_direct.o' "$supply_dir/Makefile" || \
	printf 'obj-$(CONFIG_CHARGER_SM5440_DIRECT)\t+= sm5440_direct.o\n' \
		>> "$supply_dir/Makefile"

tcpm_dir=$kernel_tree/drivers/usb/typec/tcpm
install -m 0644 "$drv/sm5714_usbpd.c" "$tcpm_dir/sm5714_usbpd.c"
if ! grep -q 'TYPEC_SM5714' "$tcpm_dir/Kconfig"; then
	sed -i '/^endif # TYPEC_TCPM$/i \
config TYPEC_SM5714\
\ttristate "Silicon Mitus SM5714 USB Type-C and PD controller"\
\tdepends on I2C\
\tdepends on TYPEC_TCPM\
\tdepends on BATTERY_SM5714\
\thelp\
\t  USB Type-C CC and USB-PD message transport for the SM5714 PDIC.\
' "$tcpm_dir/Kconfig"
fi
grep -q 'sm5714_usbpd.o' "$tcpm_dir/Makefile" || \
	printf 'obj-$(CONFIG_TYPEC_SM5714)\t+= sm5714_usbpd.o\n' \
		>> "$tcpm_dir/Makefile"

mux_dir=$kernel_tree/drivers/usb/typec/mux
install -m 0644 "$drv/ps5169.c" "$mux_dir/ps5169.c"
if ! grep -q 'TYPEC_MUX_PS5169' "$mux_dir/Kconfig"; then
	cat >> "$mux_dir/Kconfig" <<'EOF'

config TYPEC_MUX_PS5169
	tristate "Parade PS5169 Type-C redriver"
	depends on I2C
	depends on TYPEC
	depends on USB_ROLE_SWITCH
	help
	  USB 3.x and DisplayPort lane redriver used by the SM-X910.
EOF
fi
grep -q 'ps5169.o' "$mux_dir/Makefile" || \
	printf 'obj-$(CONFIG_TYPEC_MUX_PS5169)\t+= ps5169.o\n' >> "$mux_dir/Makefile"

keyboard_dir=$kernel_tree/drivers/input/keyboard
install -m 0644 "$drv/samsung_stm32_pogo.c" \
	"$keyboard_dir/samsung_stm32_pogo.c"
if ! grep -q 'KEYBOARD_SAMSUNG_STM32_POGO' "$keyboard_dir/Kconfig"; then
	cat >> "$keyboard_dir/Kconfig" <<'EOF'

config KEYBOARD_SAMSUNG_STM32_POGO
	tristate "Samsung SM-X910 STM32 pogo keyboard"
	depends on I2C
	depends on GPIOLIB
	depends on REGULATOR
	help
	  Mainline-oriented driver for the STM32 controller in Samsung's
	  EF-DX920 Book Cover Keyboard Slim for the Galaxy Tab S9 Ultra.
EOF
fi
grep -q 'samsung_stm32_pogo.o' "$keyboard_dir/Makefile" || \
	printf 'obj-$(CONFIG_KEYBOARD_SAMSUNG_STM32_POGO) += samsung_stm32_pogo.o\n' \
		>> "$keyboard_dir/Makefile"

touch_dir=$kernel_tree/drivers/input/touchscreen
install -m 0644 "$drv/samsung_wacom_w90xx.c" \
	"$touch_dir/samsung_wacom_w90xx.c"
if ! grep -q 'TOUCHSCREEN_SAMSUNG_WACOM_W90XX' "$touch_dir/Kconfig"; then
	cat >> "$touch_dir/Kconfig" <<'EOF'

config TOUCHSCREEN_SAMSUNG_WACOM_W90XX
	tristate "Samsung SM-X910 Wacom W90xx EMR digitiser"
	depends on I2C
	help
	  Driver for the Wacom EMR digitiser behind the S Pen on Samsung's
	  Galaxy Tab S9 Ultra.  Mainline's wacom_i2c cannot drive it: this
	  controller answers big-endian from different offsets, and
	  wacom_w9000 only covers the W9002 and W9007A.
EOF
fi
grep -q 'samsung_wacom_w90xx.o' "$touch_dir/Makefile" || \
	printf 'obj-$(CONFIG_TOUCHSCREEN_SAMSUNG_WACOM_W90XX) += samsung_wacom_w90xx.o\n' \
		>> "$touch_dir/Makefile"

# STMicroelectronics FTS1BA90A touch controller of the 11" Galaxy Tab S9
# (SM-X710).  Mainline stmfts speaks a different FingerTipS command set.
install -m 0644 "$drv/samsung_fts1ba90a.c" \
	"$touch_dir/samsung_fts1ba90a.c"
if ! grep -q 'TOUCHSCREEN_SAMSUNG_FTS1BA90A' "$touch_dir/Kconfig"; then
	cat >> "$touch_dir/Kconfig" <<'EOF'

config TOUCHSCREEN_SAMSUNG_FTS1BA90A
	tristate "Samsung Galaxy Tab S9 STMicroelectronics FTS1BA90A touchscreen"
	depends on I2C
	depends on REGULATOR
	help
	  Driver for the STMicroelectronics FTS1BA90A touch controller in
	  Samsung's Galaxy Tab S9 (SM-X710) and S9+ (SM-X810).  Mainline's
	  stmfts cannot drive it: this firmware uses 16-byte events behind
	  opcodes 0x60/0x61 and an in-band system reset, not the FingerTipS
	  command set.
EOF
fi
grep -q 'samsung_fts1ba90a.o' "$touch_dir/Makefile" || \
	printf 'obj-$(CONFIG_TOUCHSCREEN_SAMSUNG_FTS1BA90A) += samsung_fts1ba90a.o\n' \
		>> "$touch_dir/Makefile"

# The EL721 is permanently assigned to TrustZone on the shipping X910.  This
# driver therefore mirrors only Samsung's power/reset ABI and intentionally
# contains no SPI transfer or image-capture path.  Matching will be provided by
# a QTEE-backed libfprint driver once its userspace protocol is bridged.
fingerprint_dir=$kernel_tree/drivers/misc
install -m 0644 "$drv/egis_el721.c" "$fingerprint_dir/egis_el721.c"
if ! grep -q 'FINGERPRINT_EGIS_EL721' "$fingerprint_dir/Kconfig"; then
	cat >> "$fingerprint_dir/Kconfig" <<'EOF'

config FINGERPRINT_EGIS_EL721
	tristate "EgisTec EL721 secure fingerprint power interface"
	depends on OF && GPIOLIB && REGULATOR
	help
	  Power/reset and restricted compatibility interface for the secure
	  EgisTec EL721 optical fingerprint sensor in the Galaxy Tab S9 Ultra.
	  Image capture and matching remain in Qualcomm TrustZone and are never
	  exposed by this driver.
EOF
fi
grep -q 'egis_el721.o' "$fingerprint_dir/Makefile" || \
	printf 'obj-$(CONFIG_FINGERPRINT_EGIS_EL721) += egis_el721.o\n' \
		>> "$fingerprint_dir/Makefile"

# The encrypted fingerprint templates are tied to a credential kept by the
# Samsung K250A secure element.  The shipping DT describes its I2C controller
# as disabled because Samsung ABL rejects enlarged replacement DTBs.  This
# port therefore instantiates the already-present controller at module load
# time and reverts that live-DT change when the module is removed.
snvm_source=$repo/kernel/drivers/snvm
snvm_dir=$fingerprint_dir/snvm
case "$snvm_dir" in
	"$kernel_tree"/drivers/misc/snvm) rm -rf -- "$snvm_dir" ;;
	*) echo "unsafe SNVM driver path: $snvm_dir" >&2; exit 1 ;;
esac
cp -a "$snvm_source" "$snvm_dir"
if ! grep -q 'drivers/misc/snvm/Kconfig' "$fingerprint_dir/Kconfig"; then
	printf '\nsource "drivers/misc/snvm/Kconfig"\n' >> "$fingerprint_dir/Kconfig"
fi
grep -q 'CONFIG_STAR_K250A_LEGO).*snvm/' "$fingerprint_dir/Makefile" || \
	printf 'obj-$(CONFIG_STAR_K250A_LEGO) += snvm/\n' \
		>> "$fingerprint_dir/Makefile"

# HwVault wraps Ubuntu's fingerprint root through Qualcomm's Secure Processing
# Unit.  Mainline has the common remoteproc and GLINK machinery but not the
# SM8550 SPSS transport.  These two GPL Qualcomm drivers are kept as modules:
# the remoteproc half publishes the stock resources dynamically after boot, so
# Samsung ABL still receives the byte-for-byte validated DTB.
spss_header=$kernel_tree/include/linux/remoteproc/qcom_spss.h
spss_rproc_dir=$kernel_tree/drivers/remoteproc
spss_glink_dir=$kernel_tree/drivers/rpmsg
install -m 0644 "$repo/kernel/include/linux/remoteproc/qcom_spss.h" \
	"$spss_header"
install -m 0644 "$drv/qcom_spss.c" "$spss_rproc_dir/qcom_spss.c"
install -m 0644 "$drv/qcom_glink_spss.c" \
	"$spss_glink_dir/qcom_glink_spss.c"
if ! grep -q '^config QCOM_SPSS$' "$spss_rproc_dir/Kconfig"; then
	cat >> "$spss_rproc_dir/Kconfig" <<'EOF'

config QCOM_SPSS
	tristate "Qualcomm Secure Processing SubSystem"
	depends on ARCH_QCOM && REMOTEPROC
	depends on RPMSG_QCOM_GLINK_SPSS
	select QCOM_MDT_LOADER
	help
	  Loads the signed SPSS firmware and exposes its GLINK channels on
	  Qualcomm SM8450/SM8550 devices.
EOF
fi
grep -q 'qcom_spss.o' "$spss_rproc_dir/Makefile" || \
	printf 'obj-$(CONFIG_QCOM_SPSS) += qcom_spss.o\n' \
		>> "$spss_rproc_dir/Makefile"
if ! grep -q '^config RPMSG_QCOM_GLINK_SPSS$' "$spss_glink_dir/Kconfig"; then
	cat >> "$spss_glink_dir/Kconfig" <<'EOF'

config RPMSG_QCOM_GLINK_SPSS
	tristate "Qualcomm GLINK SPSS transport"
	depends on RPMSG_QCOM_GLINK && QCOM_SMEM && MAILBOX
	help
	  GLINK transport for Qualcomm's Secure Processing SubSystem.
EOF
fi
grep -q 'qcom_glink_spss.o' "$spss_glink_dir/Makefile" || \
	printf 'obj-$(CONFIG_RPMSG_QCOM_GLINK_SPSS) += qcom_glink_spss.o\n' \
		>> "$spss_glink_dir/Makefile"

# SPCOM is the userspace-facing channel multiplexer above GLINK SPSS.  Keep it
# in drivers/soc/qcom so it is built against the same in-tree headers and
# symbol versions as the remoteproc transport.
spcom_dir=$kernel_tree/drivers/soc/qcom
install -m 0644 "$repo/kernel/include/uapi/linux/spcom.h" \
	"$kernel_tree/include/uapi/linux/spcom.h"
install -m 0644 "$repo/kernel/include/uapi/linux/spss_utils.h" \
	"$kernel_tree/include/uapi/linux/spss_utils.h"
install -m 0644 "$drv/spcom.c" "$spcom_dir/spcom.c"
install -m 0644 "$drv/spss_utils.c" "$spcom_dir/spss_utils.c"
install -m 0644 "$drv/qcom_sp_hlos_heap.c" "$spcom_dir/qcom_sp_hlos_heap.c"
if ! grep -q '^config QCOM_SPCOM$' "$spcom_dir/Kconfig"; then
	cat >> "$spcom_dir/Kconfig" <<'EOF'

config QCOM_SPCOM
	tristate "Qualcomm Secure Processor communication"
	depends on ARCH_QCOM && QCOM_SPSS
	help
	  Exposes the SPSS GLINK channels and DMA-BUF retention ioctls used by
	  Qualcomm Keymaster and Samsung's fingerprint secure applications.
EOF
fi
grep -q 'CONFIG_QCOM_SPCOM).*spcom.o' "$spcom_dir/Makefile" || \
	printf 'obj-$(CONFIG_QCOM_SPCOM) += spcom.o\n' >> "$spcom_dir/Makefile"
if ! grep -q '^config QCOM_SP_HLOS_HEAP$' "$spcom_dir/Kconfig"; then
	cat >> "$spcom_dir/Kconfig" <<'EOF'

config QCOM_SP_HLOS_HEAP
	tristate "Qualcomm HLOS/SPSS shared DMA-BUF heap"
	depends on ARCH_QCOM && QCOM_SCM && DMABUF_HEAPS && CMA
	help
	  Exposes the contiguous qcom,sp-hlos heap used by SPSS NVM and assigns
	  each allocation to HLOS and CP_SPSS_HLOS_SHARED for its DMA-BUF lifetime.
EOF
fi
grep -q 'CONFIG_QCOM_SP_HLOS_HEAP).*qcom_sp_hlos_heap.o' "$spcom_dir/Makefile" || \
	printf 'obj-$(CONFIG_QCOM_SP_HLOS_HEAP) += qcom_sp_hlos_heap.o\n' \
		>> "$spcom_dir/Makefile"
if ! grep -q '^config QCOM_SPSS_UTILS$' "$spcom_dir/Kconfig"; then
	cat >> "$spcom_dir/Kconfig" <<'EOF'

config QCOM_SPSS_UTILS
	tristate "Qualcomm Secure Processor userspace utilities"
	depends on ARCH_QCOM && QCOM_SPSS
	help
	  Exposes the SPSS boot-event ABI used to coordinate Qualcomm's sec_nvm
	  service and SP daemon across userspace processes.
EOF
fi
grep -q 'CONFIG_QCOM_SPSS_UTILS).*spss_utils.o' "$spcom_dir/Makefile" || \
	printf 'obj-$(CONFIG_QCOM_SPSS_UTILS) += spss_utils.o\n' \
		>> "$spcom_dir/Makefile"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

cp "$cfg/config-mainline.aarch64" "$build_dir/.config"

# Two fragments, applied in order: the hardware one inherited byte-for-byte
# from the reference port, then the Ubuntu desktop one this port adds.  Keeping
# them separate means the inherited file stays comparable to its origin.
# An array, not a space-separated string: this repository lives under a path
# that contains spaces, and word splitting turned it into nonexistent files.
fragments=("$cfg/config-$DEVICE.fragment")
[ -f "$cfg/config-ubuntu-desktop.fragment" ] && \
	fragments+=("$cfg/config-ubuntu-desktop.fragment")

apply_fragment() {
	while IFS= read -r setting; do
		case "$setting" in
			CONFIG_*=y)
				symbol=${setting%%=*}
				"$kernel_tree/scripts/config" --file "$build_dir/.config" \
					--enable "${symbol#CONFIG_}" ;;
			CONFIG_*=m)
				symbol=${setting%%=*}
				"$kernel_tree/scripts/config" --file "$build_dir/.config" \
					--module "${symbol#CONFIG_}" ;;
			CONFIG_*=\"*)
				symbol=${setting%%=*}
				value=${setting#*=}
				value=${value#\"}
				value=${value%\"}
				"$kernel_tree/scripts/config" --file "$build_dir/.config" \
					--set-str "${symbol#CONFIG_}" "$value" ;;
			'# CONFIG_'*' is not set')
				symbol=${setting#\# CONFIG_}
				symbol=${symbol% is not set}
				"$kernel_tree/scripts/config" --file "$build_dir/.config" \
					--disable "$symbol" ;;
		esac
	done < "$1"
}

for fragment in "${fragments[@]}"; do
	echo "applying config fragment: ${fragment##*/}"
	apply_fragment "$fragment"
done

if [ "$fingerprint_sensor" = 1 ]; then
	"$kernel_tree/scripts/config" --file "$build_dir/.config" \
		--module QCOMTEE --enable FINGERPRINT_EGIS_EL721 \
		--module STAR_K250A_LEGO \
		--module RPMSG_QCOM_GLINK_SPSS --module QCOM_SPSS \
		--module QCOM_SPSS_UTILS --module QCOM_SPCOM \
		--module QCOM_SP_HLOS_HEAP
else
	"$kernel_tree/scripts/config" --file "$build_dir/.config" \
		--module QCOMTEE --disable FINGERPRINT_EGIS_EL721 \
		--disable STAR_K250A_LEGO --disable QCOM_SPSS \
		--disable QCOM_SPSS_UTILS --disable QCOM_SPCOM \
		--disable QCOM_SP_HLOS_HEAP \
		--disable RPMSG_QCOM_GLINK_SPSS
fi

make -C "$kernel_tree" O="$build_dir" ARCH=arm64 LLVM=1 olddefconfig

# A fragment line naming a symbol that does not exist is dropped silently by
# olddefconfig: that is how the GPU once shipped with no clock controller.
# Assert every requested setting survived.  A =y that a select could only
# satisfy as =m is warned about; anything missing is fatal.  The =m requests
# that stayed modules are kept: the module payload is checked against them
# after it is built.  One a later fragment builds in is not a module request.
missing=
downgraded=
requested_m=()
for fragment in "${fragments[@]}"; do
	while IFS= read -r setting; do
		case "$setting" in
			CONFIG_*=y|CONFIG_*=m)
				symbol=${setting%%=*}
				want=${setting##*=}
				if grep -qxF "$symbol=y" "$build_dir/.config"; then
					:
				elif grep -qxF "$symbol=m" "$build_dir/.config"; then
					[ "$want" = y ] && downgraded="$downgraded $symbol"
					[ "$want" = m ] && requested_m+=("$symbol")
				else
					missing="$missing $symbol"
				fi ;;
			CONFIG_*=\"*)
				symbol=${setting%%=*}
				grep -q "^$symbol=" "$build_dir/.config" || \
					missing="$missing $symbol" ;;
		esac
	done < "$fragment"
done
[ -n "$downgraded" ] && \
	echo "warning: fragment asked =y, kconfig could only give =m:$downgraded" >&2
if [ -n "$missing" ]; then
	echo "config fragment symbols unknown or disabled:$missing" >&2
	exit 1
fi

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

# Two reasons to pin the build identity rather than let the kernel pick it up
# from the host:
#
#  - Privacy. Without this the banner embeds the builder's account and machine
#    name (it read "root@PC-ARTURO" during bring-up), and that string ships
#    inside every published boot.img.
#  - Reproducibility. UTS_VERSION carries the build counter and timestamp, and
#    a one-character change there ("#1" vs "#18") shifts the linked image, so
#    two builds of identical sources never match byte for byte.
#
# SOURCE_DATE_EPOCH defaults to the commit date of the pinned kernel, so it is
# a property of the sources rather than of when we happened to build.
export KBUILD_BUILD_USER=${KBUILD_BUILD_USER:-ubuntu}
export KBUILD_BUILD_HOST=${KBUILD_BUILD_HOST:-$DEVICE}
export SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-$(git -C "$kernel_tree" log -1 --format=%ct)}
export KBUILD_BUILD_TIMESTAMP=${KBUILD_BUILD_TIMESTAMP:-$(
	LC_ALL=C date -u -d "@$SOURCE_DATE_EPOCH" 2>/dev/null
)}
# The build counter lives in .version and increments on every link.
printf '0\n' > "$build_dir/.version"

make -C "$kernel_tree" O="$build_dir" ARCH=arm64 LLVM=1 -j"$(nproc)" \
	Image.gz qcom/$DEVICE_DTS.dtb

if [ "${BUILD_WIFI_MODULES:-1}" = 1 ]; then
	modules_root=$out_dir/modules-root
	case "$modules_root" in
		"$base"/out/*/modules-root) rm -rf -- "$modules_root" ;;
		*) echo "unsafe modules output path: $modules_root" >&2; exit 1 ;;
	esac

	# An isolated M= build needs the built-in export table under the
	# external-module name, and a clean O= tree lacks scripts/module.lds
	# until modules_prepare.
	module_tree=$kernel_tree/drivers/net/wireless/ath/ath12k
	make -C "$kernel_tree" O="$build_dir" ARCH=arm64 LLVM=1 modules_prepare
	cp "$build_dir/vmlinux.symvers" "$build_dir/Module.symvers"
	make -C "$kernel_tree" O="$build_dir" ARCH=arm64 LLVM=1 -j"$(nproc)" \
		M="$module_tree" modules
	make -C "$kernel_tree" O="$build_dir" ARCH=arm64 LLVM=1 \
		M="$module_tree" \
		INSTALL_MOD_PATH="$modules_root" INSTALL_MOD_STRIP=1 \
		DEPMOD=true modules_install
	if grep -qx 'CONFIG_QCOMTEE=m' "$build_dir/.config"; then
		qcomtee_tree=$kernel_tree/drivers/tee/qcomtee
		make -C "$kernel_tree" O="$build_dir" ARCH=arm64 LLVM=1 \
			-j"$(nproc)" M="$qcomtee_tree" modules
		make -C "$kernel_tree" O="$build_dir" ARCH=arm64 LLVM=1 \
			M="$qcomtee_tree" INSTALL_MOD_PATH="$modules_root" \
			INSTALL_MOD_STRIP=1 DEPMOD=true modules_install
	fi
	if grep -qx 'CONFIG_STAR_K250A_LEGO=m' "$build_dir/.config"; then
		make -C "$kernel_tree" O="$build_dir" ARCH=arm64 LLVM=1 \
			-j"$(nproc)" M="$snvm_dir" modules
		make -C "$kernel_tree" O="$build_dir" ARCH=arm64 LLVM=1 \
			M="$snvm_dir" INSTALL_MOD_PATH="$modules_root" \
			INSTALL_MOD_STRIP=1 DEPMOD=true modules_install
	fi

	release=$(make -s -C "$kernel_tree" O="$build_dir" ARCH=arm64 LLVM=1 \
		kernelrelease)
	release_dir=$modules_root/lib/modules/$release
	# Only Image.gz and the isolated M= trees are built, so an in-tree =m
	# option is silently left out; the payload check below fails the build
	# for any the fragments ask for.  An in-tree driver a fragment keeps
	# modular on purpose (an experiment switch) is listed here and built as
	# a single target: M=drivers/soc/qcom would also build that directory's
	# other =m objects, and pmic_glink_altmode needs DRM bridge symbols this
	# build never produces.  Keyed on the fragments, not on .config: the
	# inherited base config leaves many options at =m that no board here
	# ships, the bandwidth monitor among them on the Ultra.
	for single in CONFIG_QCOM_ICC_BWMON:drivers/soc/qcom/icc-bwmon.ko; do
		symbol=${single%%:*}
		object=${single#*:}
		case " ${requested_m[*]} " in
			*" $symbol "*) ;;
			*) continue ;;
		esac
		grep -qx "$symbol=m" "$build_dir/.config" || continue
		make -C "$kernel_tree" O="$build_dir" ARCH=arm64 LLVM=1 -j"$(nproc)" \
			"$object"
		installed=$release_dir/kernel/$object
		install -D -m 0644 "$build_dir/$object" "$installed"
		llvm-strip --strip-debug "$installed"
		sig_hash=$(sed -n 's/^CONFIG_MODULE_SIG_HASH="\(.*\)"$/\1/p' "$build_dir/.config")
		if [ -n "$sig_hash" ] && [ -f "$build_dir/certs/signing_key.pem" ]; then
			"$build_dir/scripts/sign-file" "$sig_hash" "$build_dir/certs/signing_key.pem" \
				"$build_dir/certs/signing_key.x509" "$installed"
		fi
		if grep -qx 'CONFIG_MODULE_COMPRESS_ZSTD=y' "$build_dir/.config"; then
			zstd -T0 -19 -q --rm -f "$installed"
		fi
	done
	# Containers need the modular NAT/filter stack too, not just ath12k.
	# Without these modules Waydroid fails before Android starts, with
	# "can't initialize iptables table filter" from waydroid-net.sh.
	bash "$repo/scripts/build-netfilter-modules.sh" \
		"$kernel_tree" "$build_dir" "$release_dir/updates/netfilter"
	if grep -qx 'CONFIG_QCOM_SPSS=m' "$build_dir/.config" &&
	   grep -qx 'CONFIG_RPMSG_QCOM_GLINK_SPSS=m' "$build_dir/.config"; then
		# The fingerprint secure-transport helper also requires the small
		# board-specific IPCC IRQ bridge.  Keep it in the same signed module
		# set as the SPSS remoteproc/glink stack so replacing a kernel release
		# cannot leave fprintd waiting for a module signed by the previous key.
		spss_irq_module_dir=$scratch/spss-irq-module-$DEVICE
		case "$spss_irq_module_dir" in
			"$scratch"/spss-irq-module-"$DEVICE")
				rm -rf -- "$spss_irq_module_dir"
				;;
			*) echo "unsafe SPSS IRQ module path: $spss_irq_module_dir" >&2; exit 1 ;;
		esac
		install -d "$spss_irq_module_dir" "$release_dir/updates"
		install -m 0644 "$repo/kernel/drivers/qcom_spss_irq.c" \
			"$spss_irq_module_dir/"
		install -m 0644 "$repo/kernel/modules/spss-irq/Makefile" \
			"$spss_irq_module_dir/"
		make -C "$kernel_tree" O="$build_dir" ARCH=arm64 LLVM=1 \
			-j"$(nproc)" M="$spss_irq_module_dir" modules
		installed=$release_dir/updates/qcom_spss_irq.ko
		install -m 0644 "$spss_irq_module_dir/qcom_spss_irq.ko" "$installed"
		"$build_dir/scripts/sign-file" sha256 \
			"$build_dir/certs/signing_key.pem" \
			"$build_dir/certs/signing_key.x509" "$installed"
		modinfo -F signer "$installed" | grep -q .

		# Build only the two imported objects.  A directory-wide remoteproc
		# modules build also selects unrelated optional PRU modules, whose
		# dependencies are deliberately absent from this tablet kernel.
		make -C "$kernel_tree" O="$build_dir" ARCH=arm64 LLVM=1 \
			M="$spss_glink_dir" clean
		make -C "$kernel_tree" O="$build_dir" ARCH=arm64 LLVM=1 \
			M="$spss_rproc_dir" clean
		make -C "$kernel_tree" O="$build_dir" ARCH=arm64 LLVM=1 \
			-j"$(nproc)" M="$spss_glink_dir" qcom_glink_spss.ko
		make -C "$kernel_tree" O="$build_dir" ARCH=arm64 LLVM=1 \
			-j"$(nproc)" M="$spss_rproc_dir" \
			KBUILD_EXTRA_SYMBOLS="$spss_glink_dir/Module.symvers" \
			qcom_spss.ko
		install -d "$release_dir/updates"
		for module in \
			"$spss_glink_dir/qcom_glink_spss.ko" \
			"$spss_rproc_dir/qcom_spss.ko"; do
			installed=$release_dir/updates/${module##*/}
			install -m 0644 "$module" "$installed"
			"$build_dir/scripts/sign-file" sha256 \
				"$build_dir/certs/signing_key.pem" \
				"$build_dir/certs/signing_key.x509" "$installed"
			modinfo -F signer "$installed" | grep -q .
		done

		make -C "$kernel_tree" O="$build_dir" ARCH=arm64 LLVM=1 \
			-j"$(nproc)" M="$spcom_dir" spss_utils.ko
		installed=$release_dir/updates/spss_utils.ko
		install -m 0644 "$spcom_dir/spss_utils.ko" "$installed"
		"$build_dir/scripts/sign-file" sha256 \
			"$build_dir/certs/signing_key.pem" \
			"$build_dir/certs/signing_key.x509" "$installed"
		modinfo -F signer "$installed" | grep -q .

		make -C "$kernel_tree" O="$build_dir" ARCH=arm64 LLVM=1 \
			-j"$(nproc)" M="$spcom_dir" \
			KBUILD_EXTRA_SYMBOLS="$spss_rproc_dir/Module.symvers" \
			spcom.ko
		installed=$release_dir/updates/spcom.ko
		install -m 0644 "$spcom_dir/spcom.ko" "$installed"
		"$build_dir/scripts/sign-file" sha256 \
			"$build_dir/certs/signing_key.pem" \
			"$build_dir/certs/signing_key.x509" "$installed"
		modinfo -F signer "$installed" | grep -q .

		make -C "$kernel_tree" O="$build_dir" ARCH=arm64 LLVM=1 \
			-j"$(nproc)" M="$spcom_dir" qcom_sp_hlos_heap.ko
		installed=$release_dir/updates/qcom_sp_hlos_heap.ko
		install -m 0644 "$spcom_dir/qcom_sp_hlos_heap.ko" "$installed"
		"$build_dir/scripts/sign-file" sha256 \
			"$build_dir/certs/signing_key.pem" \
			"$build_dir/certs/signing_key.x509" "$installed"
		modinfo -F signer "$installed" | grep -q .
	fi

	# v4l2loopback is the application-facing half of the camera bridge. Build
	# the exact reviewed revision against this exact kernel, then sign it with
	# the key generated by this kernel build. A DKMS package on the tablet
	# would be unable to reproduce that ABI or signature reliably.
	loopback_tree=$scratch/v4l2loopback-$DEVICE
	case "$loopback_tree" in
		"$scratch"/v4l2loopback-"$DEVICE") rm -rf -- "$loopback_tree" ;;
		*) echo "unsafe v4l2loopback path: $loopback_tree" >&2; exit 1 ;;
	esac
	git clone --quiet https://github.com/v4l2loopback/v4l2loopback.git \
		"$loopback_tree"
	git -C "$loopback_tree" checkout --quiet "$v4l2loopback_commit"
	git -C "$loopback_tree" apply \
		"$repo/packaging/v4l2loopback/patches/0001-backward-compatible-client-usage-event.patch" \
		"$repo/packaging/v4l2loopback/patches/0002-fix-buffer-queue-management.patch" \
		"$repo/packaging/v4l2loopback/patches/0003-preserve-output-queue-for-capture.patch"
	make -C "$kernel_tree" O="$build_dir" ARCH=arm64 LLVM=1 \
		-j"$(nproc)" M="$loopback_tree" modules
	install -d "$release_dir/updates"
	install -m 0644 "$loopback_tree/v4l2loopback.ko" \
		"$release_dir/updates/v4l2loopback.ko"
	test -f "$build_dir/certs/signing_key.pem"
	test -f "$build_dir/certs/signing_key.x509"
	"$build_dir/scripts/sign-file" sha256 \
		"$build_dir/certs/signing_key.pem" \
		"$build_dir/certs/signing_key.x509" \
		"$release_dir/updates/v4l2loopback.ko"
	modinfo -F signer "$release_dir/updates/v4l2loopback.ko" | grep -q .

	# Every in-tree option a fragment asks for as =m must be in this payload.
	# Nothing builds the general module tree, so an =m nobody ships is simply
	# absent on the tablet: that is how the video clock controller, the
	# crypto engine, qcom_stats and the PMIC RTC were lost, each found only on
	# hardware.  Kbuild says which objects a symbol builds; all must ship.
	python3 -I - "$kernel_tree" "$release_dir" "${requested_m[@]}" <<'PY'
import os
import re
import sys

tree, payload, symbols = sys.argv[1], sys.argv[2], sys.argv[3:]
wanted = set(symbols)
objects = {symbol: set() for symbol in wanted}
assign = re.compile(r'^\s*obj-\$\((CONFIG_[A-Za-z0-9_]+)\)\s*[:+]?=(.*)$')
# Other architectures name the same generic symbols for objects an arm64
# build never has; tools/, scripts/ and samples/ are not part of the kernel.
skip = {'Documentation', 'samples', 'scripts', 'tools', 'usr'}
roots = []
for top in sorted(os.listdir(tree)):
    path = os.path.join(tree, top)
    if top.startswith('.') or top in skip or not os.path.isdir(path):
        continue
    roots.append(os.path.join(path, 'arm64') if top == 'arch' else path)
for root in roots:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith('.')]
        for name in ('Makefile', 'Kbuild'):
            if name not in filenames:
                continue
            with open(os.path.join(dirpath, name), errors='replace') as f:
                text = f.read().replace('\\\n', ' ')
            for line in text.splitlines():
                match = assign.match(line)
                if not match or match.group(1) not in wanted:
                    continue
                for word in match.group(2).split('#', 1)[0].split():
                    if word.endswith('.o'):
                        objects[match.group(1)].add(os.path.basename(word)[:-2])
shipped = set()
for dirpath, dirnames, filenames in os.walk(payload):
    for name in filenames:
        base = re.sub(r'\.ko(\.(gz|xz|zst))?$', '', name)
        if base != name:
            shipped.add(base)
# A symbol with no object of its own (IP_NF_IPTABLES only gates the legacy
# tables) has nothing to ship.
absent = ['  %s=m: %s' % (s, ' '.join(o + '.ko' for o in sorted(objects[s] - shipped)))
          for s in sorted(wanted) if objects[s] - shipped]
if absent:
    print('a config fragment asks for modules this payload does not ship:',
          file=sys.stderr)
    print('\n'.join(absent), file=sys.stderr)
    print('build them in (=y) or ship them from build-mainline-kernel.sh',
          file=sys.stderr)
    sys.exit(1)
print('module payload: all %d fragment =m requests ship' % len(wanted))
PY

	install -m 0644 "$build_dir/modules.builtin" \
		"$release_dir/modules.builtin"
	install -m 0644 "$build_dir/modules.builtin.modinfo" \
		"$release_dir/modules.builtin.modinfo"
	find "$release_dir" -type f -name '*.ko*' -printf '%P\n' \
		| sort > "$release_dir/modules.order"
	depmod -b "$modules_root" "$release"
	mkdir -p "$modules_root/usr/lib"
	mv "$modules_root/lib/modules" "$modules_root/usr/lib/modules"
	rmdir "$modules_root/lib"
	printf '%s\n' "$release" > "$out_dir/kernel.release"
elif [ "${#requested_m[@]}" -gt 0 ]; then
	# A kernel-only iteration ships no modules at all; say what is missing.
	echo "warning: BUILD_WIFI_MODULES=0 ships no modules; absent on the tablet:" \
		"${requested_m[*]}" >&2
fi

install -m 0644 "$build_dir/arch/arm64/boot/Image.gz" "$out_dir/Image.gz"
install -m 0644 \
	"$build_dir/arch/arm64/boot/dts/qcom/$DEVICE_DTS.dtb" \
	"$out_dir/$DEVICE_DTS.dtb"
install -m 0644 "$build_dir/.config" "$out_dir/config"

sha256sum "$out_dir/Image.gz" \
	"$out_dir/$DEVICE_DTS.dtb" \
	"$out_dir/config" > "$out_dir/SHA256SUMS"

# The kernel trusts only this build's module key, and the next clean build
# deletes it; without it, no module can ever be added to this kernel.
bash "$repo/scripts/kernel-key-registry.sh" add "$build_dir" \
	"$DEVICE kernel built from $(git -c safe.directory="$repo" -C "$repo" \
		describe --always --dirty --abbrev=12 2>/dev/null || echo unknown)"

cat > "$out_dir/fingerprint.layers" <<EOF
panel_fod=$fingerprint_panel
touch_fod=$fingerprint_touch
el721=$fingerprint_sensor
EOF

cat "$out_dir/SHA256SUMS"
