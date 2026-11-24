"""Run the Type-C roles kit's tablet scripts (tools/inplace/typec-roles)
against a fake root.

install.sh and rollback.sh normally run as root on the SM-X710 and write the
boot partition; verify.sh reads the Type-C port and the TCPM log.  Here every
absolute path they touch is moved under a temporary directory, the partition
size and the pinned image identities (the hang-log kit's, and revision 7 of
the kit's) are replaced by those of small stand-in images, and id, uname,
flock, findmnt, fuser, dpkg, depmod, blockdev, systemctl, journalctl, modinfo,
dmesg, sleep and sync are stubbed, and for verify.sh cat as well: the TCPM log
it reads is emptied by each read and holds 1023 lines, as the kernel's does.
findmnt reports a read-only root, fuser a busy dpkg and depmod a failure on
request, and the sync stub records what a power failure right after each
call would find, and fails on request.  For the tests of a run stopped
anywhere, the commands that change the disk are wrapped so that a run can be
killed, or see the command fail, before any one of them.
make-kit.sh needs a kernel build and is only checked for syntax, for pinning
the same identities as the tablet scripts, for what its comparison of two
kernel source trees leaves out, for the strings it looks for in the image,
and for its check that the image was built from the sources as they are.
"""
import gzip
import hashlib
import io
import itertools
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import unittest


REPO = pathlib.Path(__file__).resolve().parents[1]
KIT_SRC = REPO / "tools/inplace/typec-roles"
RELEASE = "7.2.0-rc3-dirty"
SIZE = 4 * 1024 * 1024
BACKUP = "var/backups/ubuntu-gts9u-typec-roles"
HANG_BUILD_ID = "68d243e908c0edfddc10429ede4bb235f205e44c"
NEW_BUILD_ID = "0123456789abcdef0123456789abcdef01234567"
NEWER_BUILD_ID = "89abcdef0123456789abcdef0123456789abcdef"
NEWEST_BUILD_ID = "456789abcdef0123456789abcdef0123456789ab"
# The revisions of the kit before it recorded what it wrote, as install.sh
# knows them: the sha256 of boot.img and BUILD_ID from each packed kit
# (~/ubuntu-gts9u/backups/typec-roles-kit*, 2026-11-20 and 21).  Revision 7
# has been on the SM-X710 since 2026-11-21 22:39.
EARLIER = (
    ("45b5e2d884d937b01c784f6cc6ba581aa97734d3bd8a0a0cf385320bab9c68f5",
     "65ce39dc68d2e85591e088300f6b0ccf81f5b243", "r1"),
    ("3c483cf09959aec58213c0ad0d38466e78e5312a362183316e80f4ddc46780a9",
     "7a614f2582c59b33e37632ef82a16ade7510aa6f", "r2"),
    ("743c7f7cf859c3e155720e340b8ba39dcbeeb80d8bdf1b6be9c353867c3e1604",
     "4ad9cb230f76092db3dbcc8291ae38367bfe2b4e", "r3"),
    ("5794ceefd658b9f41d1adfbbaa4b45acb4e64c616f6e76acb2bce5805547e31f",
     "6479859ed5e3c2c5d7cc43c085d2141e14b4fd2a", "r4,r5"),
    ("e11e4e09668cd509b6b848ddcc32f0a7bf74cdfcb3da496caf8617f2e7b4e354",
     "326b061707e9bd906d1e5eb4bc8ea158e19f42bb", "r6"),
    ("613c0c12817c3286c4b927539245e41882c7729ec2b712e1eb1e77ae59d3a9d2",
     "84c2748b5ee4b283b56a77579b0b0758f88117e1", "r7"),
)
R7_BOOT, R7_BUILD_ID, _ = EARLIER[-1]
# What the tests' revision 7 runs: a stand-in for its kernel.
R7_STANDIN_ID = "7" * 40
# Revision 7's install.sh (93030d5, packed from 109c079), and the one of
# revisions 5 and 6 (f00f2b3): their sha256 in the packed kits' SHA256SUMS,
# and their git blobs.
OLD_INSTALLS = (
    ("r7", "0805fb25ba81b7f183d1c123f943404505e772e132869fa064a2b4c495b465e1",
     "609bc42dde980ee4c819c51dbd89f6944b3d8295"),
    ("r5 and r6", "f8315affbce49434f25bee951120ccf93d726b04f03707ba086583d24eee84cd",
     "7dee403a7f1b67e1f5e91ec8c3c9af9d35c077c6"),
)
# The rollback.sh of revisions 5 to 7, the same way.
OLD_ROLLBACK = ("116604b91498bc37a8f16043c83ecca9dd47a329c3c4bb67013090f2dcd62d4a",
                "720de2324ffbfcec804b8693f1f9254de2be3a10")
# Revision 8's scripts, the same in every repack of it (on squirtle since
# 2026-11-22, never installed there): sha256 in its SHA256SUMS, git blob.
R8_SCRIPTS = {
    "install.sh": ("1683466c67711366964a2f6c3b242ad1e65601fc87a345a680c2ef4166a6b17d",
                   "f31d705dd95038c549f0abf3435cd88e3e6c59eb"),
    "rollback.sh": ("95c1a6151f170d0a9cd69c3026f7422498b97e2b6d28ad08ed0be312e93d77fc",
                    "b579697abe4295919e365e3f35e9112c4661af0a"),
    "sleep-test.sh": ("aa1ebc86eb2d3ca4e90550399c20d29d33bedfd27144b45802aa2df19f837deb",
                      "ac4d122f8c49b66ccf06d1253f39bc91338cf9e2"),
}
# What the tests' revision 8 runs: a stand-in for its kernel.
R8_STANDIN_ID = "8" * 40
# Revision 9's scripts (7e860fc, packed into typec-roles-kit-r9 and installed
# on squirtle on 2026-11-22 at 20:59 by its own install.sh): sha256 in its
# SHA256SUMS, git blob.
R9_SCRIPTS = {
    "install.sh": ("5206e83f2020e4a7780f2310a5938072ee1105e9d74d6bc48b389b6581da14be",
                   "ee931167c55f22a8429ce2f7a079f1cdc9b90154"),
    "rollback.sh": ("95c1a6151f170d0a9cd69c3026f7422498b97e2b6d28ad08ed0be312e93d77fc",
                    "b579697abe4295919e365e3f35e9112c4661af0a"),
    "verify.sh": ("69d2fa2f6e5a061dc903426c644731abe6e5fa2908c3dd7b1ec395343a1ced48",
                  "e8f1ade1743b5b2b0647ac59c5219e571e5e9754"),
    "sleep-test.sh": ("4f8a50f27d8ac08f93c6b1fda8255b66a86ad6dfdfd5b48fdea9300c8714de5a",
                      "b27a607d36c9f4606e450cfe26042fbdf6436c78"),
}
# What the tests' revision 9 runs: a stand-in for its kernel.
R9_STANDIN_ID = "9" * 40
# Revision 10's scripts (packed from ab2c536 into typec-roles-kit-r10 and
# installed on squirtle on 2026-11-23 at 11:37 by its own install.sh, over
# revision 9): sha256 in its SHA256SUMS, git blob.
R10_SCRIPTS = {
    "install.sh": ("703e4de048fb48edda2b3f539e7cf1dcfde4c581353a5b94cedf3985d1e7b6d8",
                   "0b3ed7906e0f43197098b0181c2bcdda0f3f9f57"),
    "rollback.sh": ("95c1a6151f170d0a9cd69c3026f7422498b97e2b6d28ad08ed0be312e93d77fc",
                    "b579697abe4295919e365e3f35e9112c4661af0a"),
    "verify.sh": ("44e17a78da2a4e048dba1e885ae924d7d8ffd95755b335a424d31063b115fd3d",
                  "f90e1be312cb68709ff77892a437d1a438d4e250"),
    "sleep-test.sh": ("99e02b4d2015e3bca340ce459f241e59bd12a784bb90733d6424fc5af28a204b",
                      "35016ef9c8cee9d51870656a8790915f8a0b7748"),
}
# What the tests' revision 10 runs: a stand-in for its kernel.
R10_STANDIN_ID = "10" * 20
# How sleep-test.sh names the debug switches on a kernel without them, at
# the end of every cycle's verdict and RESULT line (the fake tablet's kernel
# has none unless a test gives them).
NO_SWITCHES = "switches: not offered by this kernel"
KEY_ID = "aa" * 20
SIGNER = ":".join(["5A"] * 20)
MODULES = ("modules.dep", "kernel/net/a.ko", "updates/b.ko")
TCPM_DIR = "drivers/usb/typec/tcpm"
GCC = "drivers/clk/qcom/gcc-sm8550"
DWC3 = "drivers/usb/dwc3/dwc3-qcom"
QMP = "drivers/phy/qualcomm/phy-qcom-qmp-ufs"
RPMHPD = "drivers/pmdomain/qcom/rpmhpd"
# Revision 10's files.
PHYMUX = "drivers/clk/qcom/clk-regmap-phy-mux"
QMP_PCIE = "drivers/phy/qualcomm/phy-qcom-qmp-pcie"
GMU = "drivers/gpu/drm/msm/adreno/a6xx_gmu"
UFS_QCOM = "drivers/ufs/host/ufs-qcom"
# Revision 11's.
PCIE_QCOM = "drivers/pci/controller/dwc/pcie-qcom"
# make-kit.sh's patches: the USB-C dock changes, the deep-sleep debug
# switches (revision 9), which the build applies behind their switch, the
# SM8550 sleep fixes (revision 10) behind theirs, then the UFS PHY's three
# (revisions 8 and 10), which it applies through its stage script and its
# diagnostic switch.
TYPEC_PATCHES = ["tcpm-request-dfp-as-sink-before-discovery.patch", "tcpm-try-snk-extra-wait.patch",
                 "tcpm-no-dr-swap-during-vdm.patch", "tcpm-dr-set-checks-data-role.patch",
                 "tcpm-keep-queued-swap-pending.patch", "gcc-sm8550-usb-gdscs-retention.patch",
                 "dwc3-qcom-check-hcd-before-reading-port-speed.patch"]
UFS_PATCHES = ["qmp-ufs-assert-pcs-reset-before-calibration-gts9u.patch",
               "qmp-ufs-pcs-state-diagnostic.patch", "qmp-ufs-pwrdn-readback-diagnostic.patch"]
DEBUG_PATCHES = ["gcc-sm8550-gdsc-debug-switches.patch", "rpmhpd-debug-retention-floor.patch"]
# Revision 12's debug patch in the same block of the build script: the AOP's
# CXPC violators log (sleep-test.sh's cxpc), qcom_stats.c alone.
CXPC_PATCH = "qcom-stats-cxpc-violators-diagnostic.patch"
QCOM_STATS = "drivers/soc/qcom/qcom_stats"
# Revision 11's pcie_0_gdsc, behind APPLY_SM8550_PCIE0_GDSC_OFF, and the line
# it gives that domain, by which the build script and make-kit.sh know it.
GDSC_PATCHES = ["gcc-sm8550-pcie0-gdsc-off-in-suspend.patch"]
PCIE0_LINE = "\t.pwrsts = PWRSTS_OFF_ON, /* gcc-sm8550-pcie0-gdsc-off-in-suspend.patch */\n"
# The genpd flag the same patch gives pcie_0_gdsc (the guard's clock
# controller half), and the PCIe host's patches: the guard's other half,
# behind APPLY_SM8550_PCIE0_GDSC_GUARD, and the root port's debug switch,
# behind that and APPLY_DEEP_SLEEP_DEBUG_SWITCHES; each with a line the build
# script and make-kit.sh know it by.
GUARD_FLAG_LINE = "\t\t.flags = GENPD_FLAG_ACTIVE_WAKEUP,\n"
PCIE_PATCHES = ["pcie-qcom-keep-power-domain-while-link-kept.patch", "pcie-qcom-debug-root-port-d3.patch"]
GUARD_LINE = "qcom_pcie_mark_sleep_path(dev, pcie->link_kept);"
RPD3_MARKER = "PCIe root port to D3 for a sleep test, not for release builds"
# Revision 11's ufs-qcom: the UFS PHY power reference fix, which every build
# applies, and its diagnostic, behind APPLY_QMP_UFS_PCS_STATE_DIAGNOSTIC
# (tests/test_ufs_qcom_phy_power.py has their own tests).
UFS_QCOM_PATCHES = ["ufs-qcom-track-phy-power-state-gts9u.patch", "ufs-qcom-phy-power-count-diagnostic.patch"]
# Revision 10's three upstream commits, each with the file it changes (and
# ufs-qcom.h with ufs-qcom.c), its upstream hash, and the line the build
# script knows it by; and the patch it leaves out.
FIX_PATCHES = ["clk-qcom-regmap-phy-mux-revert-rework.patch", "msm-adreno-a6xx-fix-stale-rpmh-votes.patch",
               "ufs-qcom-enable-only-lane-clocks.patch"]
FIXES = ((FIX_PATCHES[0], f"{PHYMUX}.c", "0aec16a93bac6b2db928119bd34cc1e896173af4",
          "static int phy_mux_enable(struct clk_hw *hw)"),
         (FIX_PATCHES[1], f"{GMU}.c", "d9108bfdb746edacdb05bd27959a4ae63c6c7f3f",
          "if (!test_and_clear_bit(GMU_STATUS_FW_START, &gmu->status))"),
         (FIX_PATCHES[2], f"{UFS_QCOM}.c", "f07317a8d57f382ec505597816271dd72ffa20c7",
          'host->tx_lane0_sync_clk = devm_clk_get(dev, "tx_lane0_sync_clk");'))
UNPARK = "unpark-pcie0-pipe-mux.patch"
# Revision 9's debug switches: the word sleep-test.sh's options and record
# give each, and its file below /sys/module.
SWITCHES = (("pcie0", "gcc_sm8550/parameters/debug_pcie0_off"),
            ("pcie0phy", "gcc_sm8550/parameters/debug_pcie0_phy_off"),
            ("usb", "gcc_sm8550/parameters/debug_usb_off"),
            ("cxmx", "rpmhpd/parameters/debug_ret_floor"))
# Revision 11's read-only fifth, and what it says on that kernel.
BOOT_STATE = "gcc_sm8550/parameters/debug_boot_state"
R11_BOOT = "pcie0 Y, pcie0phy N, usb N"
# Revision 11's root port switch (sleep-test.sh's root-port-d3, the record's
# rpd3), N at every boot.
RPD3_SWITCH = "pcie_qcom/parameters/debug_root_port_d3"
# The lines revision 11's PCIe host logs at each resume
# (pcie-qcom-keep-power-domain-while-link-kept.patch): the guard holding in
# a sleep that kept the link, failing, and a host that powered down.
GUARD_KEPT = "qcom-pcie 1c00000.pcie: system sleep: the link was kept, and its power domain stayed on"
GUARD_BAD = "qcom-pcie 1c00000.pcie: system sleep: the link was kept, but genpd powered its power domain off"
GUARD_DOWN = "qcom-pcie 1c00000.pcie: system sleep: the host powered down, and genpd powered its power domain off"


def kit_patches(text):
    """make-kit.sh's seven lists of patches, as it names them."""
    return [re.search(rf"^{name}=\((.*?)\)$", text, re.M | re.S).group(1).split()
            for name in ("typec_patches", "gdsc_patches", "debug_patches", "fix_patches", "ufs_patches",
                         "pcie_patches", "ufs_qcom_patches")]

PREFIXES = (
    "/dev/disk/by-partlabel/", "/usr/lib/modules/", "/var/backups", "/var/log/", "/run/",
    "/var/lib/gts9u-boot-sets/", "/proc/device-tree/", "/proc/sys/kernel/", "/proc/config.gz",
    "/sys/kernel/notes", "/sys/class/power_supply/", "/sys/class/typec/",
    "/sys/kernel/debug/usb/", "/sys/bus/usb/devices/", "/sys/module/sm5714_usbpd/parameters",
    "/dev/kmsg",
    # sleep-test.sh's as well.
    "/proc/uptime", "/proc/1/mounts", "/proc/swaps", "/proc/sysrq-trigger", "/sys/power/",
    "/sys/class/rtc/", "/sys/class/block/", "/sys/kernel/debug/qcom_stats",
    "/sys/kernel/debug/interconnect/", "/sys/bus/platform/devices/", "/dev/pmsg0",
    "/etc/ubuntu-gts9u/", "/var/lib/ubuntu-gts9u/", "/usr/libexec/", "/sys/fs/ext4/",
    "/proc/interrupts",
    # sleep-test.sh's evidence.
    "/sys/kernel/debug/pm_genpd/", "/sys/kernel/debug/regulator/", "/sys/kernel/debug/wakeup_sources",
    "/sys/bus/pci/devices", "/sys/class/remoteproc/", "/sys/class/backlight/", "/sys/class/thermal/",
    # sleep-test.sh's experiment options (trace, pm_test=, mode=s2idle).
    "/sys/module/printk/parameters/", "/sys/module/suspend/parameters/", "/sys/devices/system/cpu",
    # Revision 9's debug switches (verify.sh, sleep-test.sh's gdsc= and cxmx=).
    "/sys/module/gcc_sm8550/parameters", "/sys/module/rpmhpd/parameters",
    # Revision 10's clock counts (verify.sh under sudo, sleep-test.sh's
    # evidence), read from each clock's own files since revision 11.
    "/sys/kernel/debug/clk",
    # Revision 11's rpmh option: the tracepoint, the trace, the command DB.
    "/sys/kernel/tracing", "/sys/kernel/debug/cmd-db",
    # Revision 11's evidence of the audio DSP's clients and the command line.
    "/proc/asound/", "/proc/[0-9]", "/proc/cmdline",
    # Revision 11's root port debug switch (verify.sh, sleep-test.sh's
    # root-port-d3).
    "/sys/module/pcie_qcom/parameters",
    # Revision 12: the device package's sensors hook (sleep-test.sh's
    # sensors=hook), the CXPC violators switch (cxpc).
    "/usr/lib/systemd/system-sleep/", "/sys/module/qcom_stats/parameters",
)

STUBS = {
    "id": 'echo 0',
    "uname": 'case $1 in -r) echo %s ;; *) echo "#1 SMP PREEMPT test" ;; esac' % RELEASE,
    "flock": 'exit 0',
    # The root's options (-no OPTIONS /), or its source (-no SOURCE /).
    "findmnt": '''case $* in
*SOURCE*) cat "$FAKE_ROOT/mount-source" 2>/dev/null || echo /dev/sda34 ;;
*) cat "$FAKE_ROOT/mount-options" 2>/dev/null || echo rw,relatime ;;
esac''',
    "fuser": '[ -e "$FAKE_ROOT/dpkg-busy" ]',
    "dpkg": 'exit 0',
    "depmod": '[ ! -e "$FAKE_ROOT/depmod-fails" ]',
    "blockdev": 'exit 0',
    "systemctl": 'exit 3',
    "sleep": 'exec /usr/bin/sleep 0.01',
    "dmesg": 'cat "$FAKE_ROOT/dev/kmsg" 2>/dev/null',
    "journalctl": 'cat "$FAKE_ROOT/journal" 2>/dev/null',
    "modinfo": 'case $1 in -n) echo /fake/netconsole.ko ;; -F) cat "$FAKE_ROOT/signer" ;; esac',
    # What a power failure right after this call would find: is the hang-log
    # image saved, whose module tree is in place, what boot holds, is an
    # install complete, is an upgrade under way, and the boot image of the
    # record's last line.  The call fails if a file named after its number
    # asks.
    "sync": '''b=$FAKE_ROOT/%s
echo "saved=$([ -s "$b/boot-hang-log.img" ] && echo yes || echo no)" \\
	"tree=$(cut -d' ' -f1 "$FAKE_ROOT/usr/lib/modules/%s/modules.dep" 2>/dev/null)" \\
	"boot=$(sha256sum < "$FAKE_ROOT/dev/disk/by-partlabel/boot" | cut -c1-64)" \\
	"complete=$([ -e "$b/complete" ] && echo yes || echo no)" \\
	"upgrading=$([ -e "$b/upgrading" ] && echo yes || echo no)" \\
	"recorded=$(tail -n 1 "$b/revisions" 2>/dev/null | cut -d' ' -f1 | grep . || echo none)" \\
	>> "$FAKE_ROOT/sync.calls"
[ ! -e "$FAKE_ROOT/sync-fails-$(wc -l < "$FAKE_ROOT/sync.calls")" ]''' % (BACKUP, RELEASE),
}
# The commands with which install.sh and rollback.sh change the disk, and dd,
# which also reads the partitions.  Wrapped, each call is a step: a run can be
# stopped before any one of them, killed as a power cut would stop it, or with
# that command failing.
STEP_COMMANDS = ("mkdir", "chmod", "rm", "tar", "mv", "cp", "touch", "dd", "depmod", "sync",
                 "blockdev")
STEP_WRAPPER = '''#!/bin/sh
n=0 stop=
[ ! -e "$FAKE_ROOT/steps" ] || read -r n < "$FAKE_ROOT/steps"
n=$((n + 1))
echo "$n" > "$FAKE_ROOT/steps"
echo "$n %(name)s $*" >> "$FAKE_ROOT/steps.log"
[ ! -e "$FAKE_ROOT/stop" ] || read -r stop < "$FAKE_ROOT/stop"
case $stop in
"kill $n") kill -KILL "$STEPPED_SCRIPT"; exit 137 ;;
"fail $n") exit 1 ;;
esac
exec %(real)s "$@"
'''
# The xHCI driver's other ways of saying that the controller is gone, and the
# locked-down kernel refusing a module of another build.
USB_HOST_ERRORS = (
    "kernel: xhci-hcd xhci-hcd.2.auto: WARNING: Host System Error",
    "kernel: xhci-hcd xhci-hcd.2.auto: Controller not ready at resume -19",
)
MODULE_REFUSED = ("kernel: Lockdown: modprobe: unsigned module loading is restricted; "
                  "see man kernel_lockdown.7")
# The controller lost its state in a suspend and the driver set it up again.
USB_HOST_REINIT = "kernel: xhci-hcd xhci-hcd.2.auto: xHC error in resume, USBSTS 0x401, Reinit"
# What revision 8's diagnostic logs at each calibration of the UFS PHY (made
# from the patch's format string; not seen on a tablet yet), and what a
# PCS_READY timeout logs, the 2026-11-21 lines with the diagnostic's after.
UFS_CAL_ENTRY = ("kernel: qcom-qmp-ufs-phy 1d80000.phy: calibration entry: SW_RESET 0x1 START 0 "
                 "PWRDN 0x1 READY 0 C_READY 0 CMN 0")
# Revision 10's second diagnostic, the line before each calibration entry.
UFS_PWRUP = "kernel: qcom-qmp-ufs-phy 1d80000.phy: power-up write: PWRDN 0x1 right after it, 0x1 100 us later"
# The PCIe host's link at boot, and its failure at a resume (2026-11-22).
PCIE_UP = "kernel: qcom-pcie 1c00000.pcie: PCIe Gen.2 x1 link up"
PCIE_INIT_FAILED = "kernel: qcom-pcie 1c00000.pcie: Host init failed: -16"
UFS_TIMEOUT = (
    "kernel: qcom-qmp-ufs-phy 1d80000.phy: calibration entry: SW_RESET 0 START 0x1 PWRDN 0x1 "
    "READY 0 C_READY 0x1 CMN 0x3",
    "kernel: qcom-qmp-ufs-phy 1d80000.phy: phy initialization timed-out",
    "kernel: qcom-qmp-ufs-phy 1d80000.phy: after the PCS_READY timeout: SW_RESET 0 START 0x1 "
    "PWRDN 0x1 READY 0 C_READY 0x1 CMN 0x3",
    "kernel: qcom-qmp-ufs-phy 1d80000.phy: no PCS_READY within 1 s of SERDES_START",
    "kernel: qcom-qmp-ufs-phy 1d80000.phy: 1 s after SERDES_START: SW_RESET 0 START 0x1 PWRDN 0x1 "
    "READY 0 C_READY 0x1 CMN 0x3",
    "kernel: ufshcd-qcom 1d84000.ufshc: Failed to calibrate PHY: -110",
    "kernel: ufshcd-qcom 1d84000.ufshc: Enabling the controller failed",
    "kernel: ufs_device_wlun 0:0:0:49488: ufshcd_wl_resume failed: -5",
)
# Revision 11's ufs-qcom diagnostic (ufs-qcom-phy-power-count-diagnostic.patch;
# made from its format strings, not seen on a tablet yet): the PHY's counts
# at the entry of each power-up sequence, the same after one that failed,
# and a setup_clocks call that found the PHY's power reference already as it
# asked.  And a calibration entry as revision 10's run 5 logged all 15 of
# its own (2026-11-23, the line of its log), with no power-up write before.
UFS_COUNTS = ("ufshcd-qcom 1d84000.ufshc: power-up sequence entry: PHY power_count {count} init_count 3, "
              "host reference held, setup_clocks repeats: power-on 0, power-off 0")
UFS_COUNTS_FAILED = ("ufshcd-qcom 1d84000.ufshc: power-up sequence failed, before phy_exit(): PHY power_count 1 "
                     "init_count 3, host reference held, setup_clocks repeats: power-on 0, power-off 0")
UFS_REPEAT = ("ufshcd-qcom 1d84000.ufshc: setup_clocks repeated a PHY power-on (1): clock gating state 3, "
              "active_reqs 1, in the ungate work")
UFS_CAL_ENTRY_DOWN = ("qcom-qmp-ufs-phy 1d80000.phy: calibration entry: SW_RESET 0x1 START 0x0 PWRDN 0x0 READY 0x4 "
                      "C_READY 0x0 CMN 0xa0")
# The NEXT line of a cycle whose calibration had no PHY power-on before it.
LEAK_NEXT = ("NEXT: the UFS PHY's power-on did not run before its calibration: a leaked PHY power reference "
             "(docs/resume-recovery.md, \"The PHY power reference\")")

PASS_LOG = """\
[  100.000001] state change SNK_DEBOUNCED -> SNK_ATTACHED [rev3 NONE_AMS]
[  100.200001] state change SNK_TRANSITION_SINK -> SNK_READY [rev3 POWER_NEGOTIATION]
[  100.200002] AMS DATA_ROLE_SWAP start
[  100.200003] state change SNK_READY -> AMS_START [rev3 DATA_ROLE_SWAP]
[  100.200004] Requesting DFP before discovery
[  100.200005] state change AMS_START -> DR_SWAP_SEND [rev3 DATA_ROLE_SWAP]
[  100.203405] state change DR_SWAP_SEND -> DR_SWAP_CHANGE_DR [rev3 DATA_ROLE_SWAP]
[  100.203406] state change DR_SWAP_CHANGE_DR -> SNK_READY [rev3 DATA_ROLE_SWAP]
"""
# The dock answers Wait; the request is repeated after tDRSwapWait.
WAIT_LOG = """\
[  500.000001] state change SNK_DEBOUNCED -> SNK_ATTACHED [rev3 NONE_AMS]
[  500.200001] state change SNK_TRANSITION_SINK -> SNK_READY [rev3 POWER_NEGOTIATION]
[  500.200002] AMS DATA_ROLE_SWAP start
[  500.200003] state change SNK_READY -> AMS_START [rev3 DATA_ROLE_SWAP]
[  500.200004] Requesting DFP before discovery
[  500.200005] state change AMS_START -> DR_SWAP_SEND [rev3 DATA_ROLE_SWAP]
[  500.203405] state change DR_SWAP_SEND -> DR_SWAP_CANCEL [rev3 DATA_ROLE_SWAP]
[  500.203406] state change DR_SWAP_CANCEL -> SNK_READY [rev3 DATA_ROLE_SWAP]
[  500.310002] AMS DATA_ROLE_SWAP start
[  500.310003] state change SNK_READY -> AMS_START [rev3 DATA_ROLE_SWAP]
[  500.310004] Requesting DFP before discovery
[  500.310005] state change AMS_START -> DR_SWAP_SEND [rev3 DATA_ROLE_SWAP]
[  500.313405] state change DR_SWAP_SEND -> DR_SWAP_CHANGE_DR [rev3 DATA_ROLE_SWAP]
[  500.313406] state change DR_SWAP_CHANGE_DR -> SNK_READY [rev3 DATA_ROLE_SWAP]
"""
FLAP_LOG = """\
[  200.000001] state change SNK_DEBOUNCED -> SNK_ATTACHED [rev3 NONE_AMS]
[  200.900001] Data role mismatch, initiating error recovery
[  200.900002] state change SNK_READY -> ERROR_RECOVERY [rev3 DISCOVER_IDENTITY]
[  201.500001] state change SNK_DEBOUNCED -> SNK_ATTACHED [rev3 NONE_AMS]
"""
# The logs below were recorded on the SM-X910 (2026-11-21); nothing has been
# recorded on the SM-X710 yet.  The Anker hub: the source is reported 43 ms
# into the longer wait, and VBUS follows while its debounce is checked.
TRY_SNK_LOG = """\
[  125.735454] state change SRC_ATTACH_WAIT -> SNK_TRY [delayed 200 ms]
[  125.839289] state change SNK_TRY -> SNK_TRY_WAIT [delayed 100 ms]
[  125.839298] pending state change SNK_TRY_WAIT -> SRC_TRYWAIT @ 600 ms [rev3 NONE_AMS]
[  125.882673] state change SNK_TRY_WAIT -> SNK_TRY_WAIT_DEBOUNCE [rev3 NONE_AMS]
[  125.882676] pending state change SNK_TRY_WAIT_DEBOUNCE -> SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS @ 15 ms [rev3 NONE_AMS]
[  125.897682] state change SNK_TRY_WAIT_DEBOUNCE -> SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS [delayed 15 ms]
[  125.900642] state change SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS -> SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS [rev3 NONE_AMS]
[  125.928089] state change SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS -> SNK_ATTACHED [rev3 NONE_AMS]
[  126.300001] state change SNK_READY -> AMS_START [rev3 DATA_ROLE_SWAP]
[  126.300002] Requesting DFP before discovery
[  126.303401] state change DR_SWAP_SEND -> DR_SWAP_CHANGE_DR [rev3 DATA_ROLE_SWAP]
"""
# The Satechi dock on the release kernel: Try.SNK gave up, the tablet stayed
# the power source.
TRY_SNK_LOST_LOG = """\
[  400.100001] state change SNK_TRY -> SNK_TRY_WAIT [delayed 100 ms]
[  400.100002] state change SNK_TRY_WAIT -> SRC_TRYWAIT [rev3 NONE_AMS]
[  400.500001] state change SRC_TRYWAIT_DEBOUNCE -> SRC_ATTACHED [delayed 200 ms]
"""
# The Satechi dock plugged in cold with the first revision of the longer
# wait: its Rp is reported 25 ms into the wait and is gone again 46 ms later,
# and that revision gave the attempt up there.
TRY_SNK_LOST_IN_DEBOUNCE_LOG = """\
[   52.202162] state change SNK_TRY -> SNK_TRY_WAIT [delayed 100 ms]
[   52.202170] pending state change SNK_TRY_WAIT -> SRC_TRYWAIT @ 600 ms [rev1 NONE_AMS]
[   52.227297] CC1: 0 -> 0, CC2: 0 -> 3 [state SNK_TRY_WAIT, polarity 0, connected]
[   52.227301] state change SNK_TRY_WAIT -> SNK_TRY_WAIT_DEBOUNCE [rev1 NONE_AMS]
[   52.227303] pending state change SNK_TRY_WAIT_DEBOUNCE -> SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS @ 15 ms [rev1 NONE_AMS]
[   52.242309] state change SNK_TRY_WAIT_DEBOUNCE -> SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS [delayed 15 ms]
[   52.273736] CC1: 0 -> 0, CC2: 5 -> 0 [state SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS, polarity 0, disconnected]
[   52.273741] pending state change SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS -> SRC_TRYWAIT @ 15 ms [rev1 NONE_AMS]
[   52.288750] state change SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS -> SRC_TRYWAIT [delayed 15 ms]
[   52.395189] state change SRC_TRYWAIT -> SRC_TRYWAIT_UNATTACHED [delayed 100 ms]
[   52.606956] state change SRC_ATTACH_WAIT -> SRC_ATTACHED [delayed 200 ms]
"""
# The same plug-in as this kit's kernel handles it: with the Rp gone the port
# goes back to waiting for what is left of the time, and takes the Rp that
# comes back.  Made from the log above; not recorded on a tablet.
TRY_SNK_RP_LOST_LOG = """\
[   52.202162] state change SNK_TRY -> SNK_TRY_WAIT [delayed 100 ms]
[   52.202170] pending state change SNK_TRY_WAIT -> SRC_TRYWAIT @ 596 ms [rev1 NONE_AMS]
[   52.227301] state change SNK_TRY_WAIT -> SNK_TRY_WAIT_DEBOUNCE [rev1 NONE_AMS]
[   52.227303] pending state change SNK_TRY_WAIT_DEBOUNCE -> SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS @ 15 ms [rev1 NONE_AMS]
[   52.242309] state change SNK_TRY_WAIT_DEBOUNCE -> SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS [delayed 15 ms]
[   52.273736] CC1: 0 -> 0, CC2: 5 -> 0 [state SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS, polarity 0, disconnected]
[   52.273741] state change SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS -> SNK_TRY_WAIT [rev1 NONE_AMS]
[   52.273745] pending state change SNK_TRY_WAIT -> SRC_TRYWAIT @ 524 ms [rev1 NONE_AMS]
[   52.560001] state change SNK_TRY_WAIT -> SNK_TRY_WAIT_DEBOUNCE [rev1 NONE_AMS]
[   52.575001] state change SNK_TRY_WAIT_DEBOUNCE -> SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS [delayed 15 ms]
[   52.575005] state change SNK_TRY_WAIT_DEBOUNCE_CHECK_VBUS -> SNK_ATTACHED [rev1 NONE_AMS]
"""
# The SM-X710's own, 2026-11-21: after a boot whose suspend cycle ran in host
# role, the xHCI of a later attach in host role did not come up.
USB_HOST_PROBE_FAILED = """\
kernel: PM: suspend entry (deep)
kernel: PM: suspend exit
kernel: xhci-hcd xhci-hcd.2.auto: xHCI Host Controller
kernel: xhci-hcd xhci-hcd.2.auto: Host halt failed, -110
kernel: xhci-hcd xhci-hcd.2.auto: can't setup: -110
kernel: xhci-hcd xhci-hcd.2.auto: probe with driver xhci-hcd failed with error -110
"""
# The SM-X910's boot suspend cycle with a dock attached (2026-11-21).
USB_HOST_DIED = """\
kernel: PM: suspend entry (deep)
kernel: xhci-hcd xhci-hcd.2.auto: xHCI host not responding to stop endpoint command
kernel: xhci-hcd xhci-hcd.2.auto: xHCI host controller not responding, assume dead
kernel: xhci-hcd xhci-hcd.2.auto: HC died; cleaning up
kernel: PM: suspend exit
kernel: xhci-hcd xhci-hcd.2.auto: Host halt failed, -110
"""

# A swap asked for through data_role with the kernel's own request off, as
# the two TCPM patches for it leave the log: two writes fall into the
# discovery and are refused, the third goes out after it and is accepted.
# The SWAP logs but SWAP_CUT_LOG are made from the patches' log lines; none
# was recorded on a tablet.
SWAP_LOG = """\
[  300.000001] state change SNK_DEBOUNCED -> SNK_ATTACHED [rev3 NONE_AMS]
[  300.200001] state change SNK_TRANSITION_SINK -> SNK_READY [rev3 POWER_NEGOTIATION]
[  300.200402] AMS DISCOVER_IDENTITY start
[  300.203001] AMS DATA_ROLE_SWAP start
[  300.203002] VDM in progress, DATA_ROLE_SWAP not started
[  300.210001] AMS DATA_ROLE_SWAP start
[  300.210002] VDM in progress, DATA_ROLE_SWAP not started
[  300.214001] AMS DISCOVER_MODES finished
[  300.217001] AMS DATA_ROLE_SWAP start
[  300.217002] state change SNK_READY -> AMS_START [rev3 DATA_ROLE_SWAP]
[  300.217003] state change AMS_START -> DR_SWAP_SEND [rev3 DATA_ROLE_SWAP]
[  300.221001] state change DR_SWAP_SEND -> DR_SWAP_CHANGE_DR [rev3 DATA_ROLE_SWAP]
[  300.221002] state change DR_SWAP_CHANGE_DR -> SNK_READY [rev3 DATA_ROLE_SWAP]
"""
# The same swap, then the dock's Soft_Reset and the error recovery after it.
SWAP_UNDONE_LOG = SWAP_LOG + """\
[  300.225001] Data role mismatch, initiating error recovery
[  300.225002] state change SNK_READY -> ERROR_RECOVERY [rev3 NONE_AMS]
"""
# The write got in before the discovery: one swap, nothing refused.
SWAP_EARLY_LOG = """\
[  400.000001] state change SNK_DEBOUNCED -> SNK_ATTACHED [rev3 NONE_AMS]
[  400.200001] state change SNK_TRANSITION_SINK -> SNK_READY [rev3 POWER_NEGOTIATION]
[  400.200201] AMS DATA_ROLE_SWAP start
[  400.200202] state change SNK_READY -> AMS_START [rev3 DATA_ROLE_SWAP]
[  400.200203] state change AMS_START -> DR_SWAP_SEND [rev3 DATA_ROLE_SWAP]
[  400.204001] state change DR_SWAP_SEND -> DR_SWAP_CHANGE_DR [rev3 DATA_ROLE_SWAP]
[  400.204002] state change DR_SWAP_CHANGE_DR -> SNK_READY [rev3 DATA_ROLE_SWAP]
[  400.204402] AMS DISCOVER_IDENTITY start
"""
# What the stopgap's write did on the release kernel (from the trace this
# work started with): sent 0.5 ms after TCPM's Discover Identity, reported
# discarded by the SM5714, the dock's reply to the discovery arrives where
# the answer to the swap is expected, and TCPM sends a Soft_Reset.
SWAP_CUT_LOG = """\
[ 3089.500001] state change SNK_DEBOUNCED -> SNK_ATTACHED [rev3 NONE_AMS]
[ 3089.648160] state change SNK_TRANSITION_SINK -> SNK_READY [rev3 POWER_NEGOTIATION]
[ 3089.648562] AMS DISCOVER_IDENTITY start
[ 3089.651312] PD TX complete, status: 0
[ 3089.651815] AMS DATA_ROLE_SWAP start
[ 3089.651816] state change SNK_READY -> AMS_START [rev3 DATA_ROLE_SWAP]
[ 3089.651824] state change AMS_START -> DR_SWAP_SEND [rev3 DATA_ROLE_SWAP]
[ 3089.654456] PD TX complete, status: 1
[ 3089.654501] pending state change DR_SWAP_SEND -> DR_SWAP_SEND_TIMEOUT @ 60 ms [rev3 DATA_ROLE_SWAP]
[ 3089.654510] state change DR_SWAP_SEND -> SNK_SOFT_RESET [rev3 DATA_ROLE_SWAP]
[ 3089.654513] AMS DATA_ROLE_SWAP finished
[ 3089.654535] AMS SOFT_RESET_AMS start
[ 3089.654537] state change SNK_SOFT_RESET -> AMS_START [rev3 SOFT_RESET_AMS]
[ 3089.654539] state change AMS_START -> SOFT_RESET_SEND [rev3 SOFT_RESET_AMS]
[ 3089.657998] state change SOFT_RESET_SEND -> SNK_WAIT_CAPABILITIES [rev3 NONE_AMS]
[ 3089.811174] state change SNK_TRANSITION_SINK -> SNK_READY [rev3 POWER_NEGOTIATION]
"""
# A swap that the dock answered with Wait.
SWAP_WAIT_LOG = """\
[  800.000001] state change SNK_DEBOUNCED -> SNK_ATTACHED [rev3 NONE_AMS]
[  800.200001] state change SNK_TRANSITION_SINK -> SNK_READY [rev3 POWER_NEGOTIATION]
[  800.230001] AMS DATA_ROLE_SWAP start
[  800.230002] state change SNK_READY -> AMS_START [rev3 DATA_ROLE_SWAP]
[  800.230003] state change AMS_START -> DR_SWAP_SEND [rev3 DATA_ROLE_SWAP]
[  800.233405] state change DR_SWAP_SEND -> DR_SWAP_CANCEL [rev3 DATA_ROLE_SWAP]
[  800.233406] state change DR_SWAP_CANCEL -> SNK_READY [rev3 DATA_ROLE_SWAP]
"""
# A hard reset ends the first swap; the port is ready again as it was, the
# write is told so, and the next one is accepted.
SWAP_AGAIN_LOG = """\
[  900.000001] state change SNK_DEBOUNCED -> SNK_ATTACHED [rev3 NONE_AMS]
[  900.200001] state change SNK_TRANSITION_SINK -> SNK_READY [rev3 POWER_NEGOTIATION]
[  900.230001] AMS DATA_ROLE_SWAP start
[  900.230002] state change SNK_READY -> AMS_START [rev3 DATA_ROLE_SWAP]
[  900.230003] state change AMS_START -> DR_SWAP_SEND [rev3 DATA_ROLE_SWAP]
[  900.231001] Received hard reset
[  900.231002] state change DR_SWAP_SEND -> HARD_RESET_START [rev3 HARD_RESET]
[  901.400001] state change SNK_TRANSITION_SINK -> SNK_READY [rev3 POWER_NEGOTIATION]
[  901.400002] Data role swap ended with the role unchanged
[  901.430001] AMS DATA_ROLE_SWAP start
[  901.430002] state change SNK_READY -> AMS_START [rev3 DATA_ROLE_SWAP]
[  901.430003] state change AMS_START -> DR_SWAP_SEND [rev3 DATA_ROLE_SWAP]
[  901.433405] state change DR_SWAP_SEND -> DR_SWAP_CHANGE_DR [rev3 DATA_ROLE_SWAP]
[  901.433406] state change DR_SWAP_CHANGE_DR -> SNK_READY [rev3 DATA_ROLE_SWAP]
"""
# One fix without the other: the swap goes out into the discovery and is cut
# short, the write is told that nothing changed, and the next one is done.
SWAP_CUT_AGAIN_LOG = SWAP_CUT_LOG + """\
[ 3089.811175] Data role swap ended with the role unchanged
[ 3089.840001] AMS DATA_ROLE_SWAP start
[ 3089.840002] state change SNK_READY -> AMS_START [rev3 DATA_ROLE_SWAP]
[ 3089.840003] state change AMS_START -> DR_SWAP_SEND [rev3 DATA_ROLE_SWAP]
[ 3089.843405] state change DR_SWAP_SEND -> DR_SWAP_CHANGE_DR [rev3 DATA_ROLE_SWAP]
[ 3089.843406] state change DR_SWAP_CHANGE_DR -> SNK_READY [rev3 DATA_ROLE_SWAP]
"""
# What the third fix, tcpm-keep-queued-swap-pending.patch, leaves in the log;
# made from its code, none of it seen on a tablet.  The dock's own DR_Swap is
# handled while the write's swap waits in AMS_START: it takes the swap's place
# in upcoming_state, SNK_READY leaves the write waiting, and the dock's swap
# gives the tablet the role the write asked for.
SWAP_PARTNER_FIRST_LOG = """\
[  600.000001] state change SNK_DEBOUNCED -> SNK_ATTACHED [rev3 NONE_AMS]
[  600.200001] state change SNK_TRANSITION_SINK -> SNK_READY [rev3 POWER_NEGOTIATION]
[  600.230001] AMS DATA_ROLE_SWAP start
[  600.230002] state change SNK_READY -> AMS_START [rev3 DATA_ROLE_SWAP]
[  600.230101] PD RX, header: 0x7a9 [1]
[  600.230102] state change AMS_START -> SNK_READY [rev3 DATA_ROLE_SWAP]
[  600.230103] Swap is still queued, left pending
[  600.230104] AMS DATA_ROLE_SWAP finished
[  600.230105] state change SNK_READY -> DR_SWAP_ACCEPT [rev3 DATA_ROLE_SWAP]
[  600.231501] state change DR_SWAP_ACCEPT -> DR_SWAP_CHANGE_DR [rev3 DATA_ROLE_SWAP]
[  600.231502] AMS DATA_ROLE_SWAP finished
[  600.231503] state change DR_SWAP_CHANGE_DR -> SNK_READY [rev3 NONE_AMS]
"""
# The tablet as PD 3 source, the partner's USB device since the partner's
# first swap: the write's AMS_START waits tSinkTx, and the partner's second
# DR_Swap comes in that time.  The port accepts it and drops the DR_Swap it
# had queued, which would have put the role back.
SWAP_PARTNER_FIRST_AS_SOURCE_LOG = """\
[  700.000001] state change SRC_ATTACH_WAIT -> SRC_ATTACHED [delayed 200 ms]
[  700.300001] state change SRC_TRANSITION_SUPPLY -> SRC_READY [rev3 POWER_NEGOTIATION]
[  700.400001] PD RX, header: 0x289 [1]
[  700.400002] state change SRC_READY -> DR_SWAP_ACCEPT [rev3 DATA_ROLE_SWAP]
[  700.401501] state change DR_SWAP_ACCEPT -> DR_SWAP_CHANGE_DR [rev3 DATA_ROLE_SWAP]
[  700.401502] AMS DATA_ROLE_SWAP finished
[  700.401503] state change DR_SWAP_CHANGE_DR -> SRC_READY [rev3 NONE_AMS]
[  700.500001] AMS DATA_ROLE_SWAP start
[  700.500002] pending state change SRC_READY -> AMS_START @ 16 ms [rev3 DATA_ROLE_SWAP]
[  700.505001] PD RX, header: 0x4a9 [1]
[  700.505002] state change SRC_READY -> DR_SWAP_ACCEPT [rev3 DATA_ROLE_SWAP]
[  700.506501] state change DR_SWAP_ACCEPT -> DR_SWAP_CHANGE_DR [rev3 DATA_ROLE_SWAP]
[  700.506502] Partner swapped first, queued DR_SWAP_SEND dropped
[  700.506503] AMS DATA_ROLE_SWAP finished
[  700.506504] state change DR_SWAP_CHANGE_DR -> SRC_READY [rev3 NONE_AMS]
"""
# The same tablet, and a new Request of the partner's in that time instead:
# SRC_READY at the end of the negotiation leaves the write waiting and sends
# its swap from there, not from AMS_START.
SWAP_SENT_FROM_READY_LOG = """\
[  750.000001] state change SRC_ATTACH_WAIT -> SRC_ATTACHED [delayed 200 ms]
[  750.300001] state change SRC_TRANSITION_SUPPLY -> SRC_READY [rev3 POWER_NEGOTIATION]
[  750.400001] PD RX, header: 0x289 [1]
[  750.400002] state change SRC_READY -> DR_SWAP_ACCEPT [rev3 DATA_ROLE_SWAP]
[  750.401501] state change DR_SWAP_ACCEPT -> DR_SWAP_CHANGE_DR [rev3 DATA_ROLE_SWAP]
[  750.401502] AMS DATA_ROLE_SWAP finished
[  750.401503] state change DR_SWAP_CHANGE_DR -> SRC_READY [rev3 NONE_AMS]
[  750.500001] AMS DATA_ROLE_SWAP start
[  750.500002] pending state change SRC_READY -> AMS_START @ 16 ms [rev3 DATA_ROLE_SWAP]
[  750.505001] PD RX, header: 0x14a2 [1]
[  750.505002] state change SRC_READY -> SRC_NEGOTIATE_CAPABILITIES [rev3 POWER_NEGOTIATION]
[  750.506001] pending state change SRC_NEGOTIATE_CAPABILITIES -> SRC_TRANSITION_SUPPLY @ 35 ms [rev3 POWER_NEGOTIATION]
[  750.541001] state change SRC_NEGOTIATE_CAPABILITIES -> SRC_TRANSITION_SUPPLY [delayed 35 ms]
[  750.542001] state change SRC_TRANSITION_SUPPLY -> SRC_READY [rev3 POWER_NEGOTIATION]
[  750.542002] Swap is still queued, left pending
[  750.542003] AMS POWER_NEGOTIATION finished
[  750.542004] state change SRC_READY -> DR_SWAP_SEND [rev3 NONE_AMS]
[  750.545401] state change DR_SWAP_SEND -> DR_SWAP_CHANGE_DR [rev3 NONE_AMS]
[  750.545402] AMS NONE_AMS finished
[  750.545403] state change DR_SWAP_CHANGE_DR -> SRC_READY [rev3 NONE_AMS]
"""
# The tablet as PD 3 source and USB host, nothing for the writer to do: its
# own VCONN swap, queued for the cable's discovery, and a power role swap
# that a program writing power_role queued, each met by the partner's swap
# of the same kind in the tSinkTx wait.  Both queued swaps are dropped.
SWAP_PARTNER_FIRST_OTHER_KINDS_LOG = """\
[  760.000001] state change SRC_ATTACH_WAIT -> SRC_ATTACHED [delayed 200 ms]
[  760.300001] state change SRC_TRANSITION_SUPPLY -> SRC_READY [rev3 POWER_NEGOTIATION]
[  760.400001] AMS VCONN_SWAP start
[  760.400002] pending state change SRC_READY -> AMS_START @ 16 ms [rev3 VCONN_SWAP]
[  760.405001] PD RX, header: 0x48b [1]
[  760.405002] state change SRC_READY -> VCONN_SWAP_ACCEPT [rev3 VCONN_SWAP]
[  760.405003] Partner swapped first, queued VCONN_SWAP_SEND dropped
[  760.406501] AMS VCONN_SWAP finished
[  760.406502] state change VCONN_SWAP_ACCEPT -> VCONN_SWAP_START [rev3 NONE_AMS]
[  760.600001] AMS POWER_ROLE_SWAP start
[  760.600002] pending state change SRC_READY -> AMS_START @ 16 ms [rev3 POWER_ROLE_SWAP]
[  760.605001] PD RX, header: 0x68a [1]
[  760.605002] state change SRC_READY -> PR_SWAP_ACCEPT [rev3 POWER_ROLE_SWAP]
[  760.605003] Partner swapped first, queued PR_SWAP_SEND dropped
[  760.606501] state change PR_SWAP_ACCEPT -> PR_SWAP_START [rev3 POWER_ROLE_SWAP]
"""


def dock_messages(start, count):
    """COUNT lines of the dock's messages, a microsecond apart from START
    (in microseconds): lines that no counter counts."""
    return "".join("[%5d.%06d] PD RX, header: 0x1a6f [1]\n" % divmod(start + i, 1_000_000)
                   for i in range(count))


# The TCPM log as the kernel keeps it: what was logged since the last read,
# and reading it empties it.  The ring has 1024 entries and holds 1023
# lines; once it is full, every line that comes replaces the last one with
# "overflow".  The tests log in pieces, each put in place whole, so that a
# read takes a piece whole or leaves it to the next read.  Each read is noted
# in the events file.
TCPM_LOG_STUB = r'''case $# in 1) ;; *) exec /usr/bin/cat "$@" ;; esac
case $1 in "$FAKE_ROOT"/sys/kernel/debug/usb/tcpm-*/log) ;; *) exec /usr/bin/cat "$1" ;; esac
echo read >> "$FAKE_ROOT/events"
for piece in "$FAKE_ROOT"/tcpm-spool/[0-9]*; do
	[ -e "$piece" ] || continue
	/usr/bin/cat "$piece" && rm "$piece"
done | awk 'NR < 1023 { print; next } { last = $0 }
END { if (NR == 1023) print last; else if (NR > 1023) { sub(/\].*/, "]", last); print last " overflow" } }'
'''


def sha(data):
    return hashlib.sha256(data).hexdigest()


def added_lines(patch):
    """The lines a patch of the kernel adds, by the file it adds them to."""
    added, target = {}, None
    for line in (REPO / "kernel/patches" / patch).read_text().splitlines():
        if line.startswith("+++ b/"):
            target = line[6:].split("\t")[0]
        elif line.startswith("+") and target:
            added.setdefault(target, []).append(line[1:])
    return added


def pinned(script, name):
    return re.search(rf"^{name}=([0-9a-f]+)$", (KIT_SRC / script).read_text(), re.M).group(1)


def earlier_revisions():
    """install.sh's table of the revisions that recorded nothing."""
    text = (KIT_SRC / "install.sh").read_text()
    block = re.search(r"^earlier='(.*?)'$", text, re.M | re.S).group(1)
    return tuple(tuple(line.split()) for line in block.splitlines())


def kernel_source():
    """The pinned kernel checkout, as tests/test_typec_dock_roles.py finds it."""
    base = os.environ.get("UBUNTU_WORKDIR", str(pathlib.Path.home() / "ubuntu-gts9u"))
    source = pathlib.Path(os.environ.get("KERNEL_SRC", base + "/linux-mainline"))
    return source if (source / ".git").exists() and shutil.which("patch") else None


def modules_tar(marker, extra=()):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name in (*MODULES, *extra):
            data = f"{marker} {name}\n".encode()
            info = tarfile.TarInfo(f"usr/lib/modules/{RELEASE}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class FakeTablet(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = pathlib.Path(directory.name)
        self.root = self.tmp / "root"
        self.kit = self.tmp / "kit"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, body in STUBS.items():
            stub = self.bin / name
            stub.write_text(f"#!/bin/sh\n{body}\n")
            stub.chmod(0o755)
        for d in ("dev/disk/by-partlabel", "var/backups/ubuntu-gts9u-hang-log", "var/log", "run",
                  "proc/device-tree", "proc/sys/kernel/random", "sys/kernel",
                  "sys/class/power_supply/sm5714-battery", "sys/class/typec/port0",
                  "sys/kernel/debug/usb/tcpm-4-0033", "sys/bus/usb/devices/1-1",
                  "sys/module/sm5714_usbpd/parameters",
                  f"usr/lib/modules/{RELEASE}/kernel/net", f"usr/lib/modules/{RELEASE}/updates"):
            (self.root / d).mkdir(parents=True)
        (self.root / "proc/device-tree/compatible").write_bytes(b"samsung,gts9wifi\0qcom,sm8550\0")
        (self.root / "proc/sys/kernel/random/boot_id").write_text("df92b567-0000\n")
        (self.root / "proc/sys/kernel/hung_task_timeout_secs").write_text("60\n")
        (self.root / "var/backups/ubuntu-gts9u-hang-log/complete").touch()
        (self.root / "sys/class/power_supply/sm5714-battery/capacity").write_text("96\n")
        (self.root / "sys/class/power_supply/sm5714-battery/status").write_text("Full\n")
        self.running(HANG_BUILD_ID)
        # The hang-log kit as installed: stand-in images and its module tree.
        self.hang_boot = os.urandom(SIZE)
        self.hang_vendor_boot = os.urandom(SIZE)
        self.part("boot").write_bytes(self.hang_boot)
        self.part("vendor_boot").write_bytes(self.hang_vendor_boot)
        self.modules = self.root / "usr/lib/modules" / RELEASE
        for name in ("modules.dep", "kernel/net/a.ko", "updates/b.ko"):
            (self.modules / name).write_text(f"hang {name}\n")
        # What revision 7 of the kit wrote to boot; no test tablet holds it
        # unless a test says so.  The scripts see the stand-ins in place of
        # the pinned identities.
        self.r7_boot = os.urandom(SIZE)
        self.standins = {R7_BOOT: sha(self.r7_boot), R7_BUILD_ID: R7_STANDIN_ID}
        self.stepped = False
        # The kit, as make-kit.sh lays it out.
        self.new_boot = os.urandom(SIZE)
        self.kit.mkdir()
        (self.kit / "boot.img").write_bytes(self.new_boot)
        (self.kit / f"modules-{RELEASE}.tar").write_bytes(modules_tar("new"))
        (self.kit / "BUILD_ID").write_text(NEW_BUILD_ID + "\n")
        (self.kit / "KEY_ID").write_text(KEY_ID + "\n")
        (self.kit / "SIGNER").write_text(SIGNER + "\n")
        (self.kit / "REVISION").write_text("11\n")
        for name in ("install.sh", "rollback.sh", "verify.sh", "sleep-test.sh", "ufs-phy-power-check.sh",
                     "README.md"):
            (self.kit / name).write_bytes((KIT_SRC / name).read_bytes())
        self.seal()

    def seal(self):
        names = sorted(p.name for p in self.kit.iterdir() if p.is_file() and p.name != "SHA256SUMS")
        (self.kit / "SHA256SUMS").write_text(
            "".join(f"{sha((self.kit / n).read_bytes())}  {n}\n" for n in names))

    def part(self, label):
        return self.root / "dev/disk/by-partlabel" / label

    def running(self, build_id):
        note = bytes.fromhex("040000001400000003000000") + b"GNU\0" + bytes.fromhex(build_id)
        (self.root / "sys/kernel/notes").write_bytes(note)

    def syncs(self):
        """What the disk held at each sync since this was last asked."""
        calls = self.root / "sync.calls"
        lines = calls.read_text().splitlines() if calls.exists() else []
        calls.unlink(missing_ok=True)
        return [dict(field.split("=") for field in line.split()) for line in lines]

    def run_with_failing_sync(self, name, number):
        """Run the script NAME with the sync of this NUMBER, of those it
        calls, failing."""
        self.syncs()
        marker = self.root / f"sync-fails-{number}"
        marker.touch()
        try:
            return self.run_script(name)
        finally:
            marker.unlink()

    def script(self, name):
        text = (self.kit / name).read_text()
        # One pass, so that a temporary directory below /run or /var/log is
        # not rewritten a second time.
        text = re.sub("|".join(map(re.escape, PREFIXES)),
                      lambda match: f"{self.root}{match.group(0)}", text)
        if name in ("install.sh", "rollback.sh"):
            self.assertIn("size=100663296", text)
            text = text.replace("size=100663296", f"size={SIZE}")
            # The stand-in partitions are files, and a build host's dd may not
            # do direct I/O on them (uutils dd 0.10 fails with EINVAL).
            text = text.replace(" iflag=direct", "")
        if name == "sleep-test.sh":
            # The stand-in userdata partition is a file; its dd is a stub.
            self.assertEqual(text.count('[ -b "$part" ]'), 1)
            text = text.replace('[ -b "$part" ]', '[ -f "$part" ]')
            # No mount namespace and no chroot here: the run's commands run
            # straight from the RAM root's tools (RamRootTest runs the real
            # one), and its SIGINT goes to a stand-in for systemd.
            body = re.compile(r"^in_ram_root\(\) \{ # CMD \[ARGS\]\n.*?^\}\n", re.M | re.S)
            self.assertEqual(len(body.findall(text)), 1)
            text = body.sub(lambda m: 'in_ram_root() { # CMD [ARGS]\n\t$ram_prefix "$@"\n}\n', text)
            # The freeze reaches the system's mounts through /proc/1/root:
            # the stubs take that path as it is.
            self.assertEqual(text.count("\nsysroot=/proc/1/root\n"), 1)
            # A write to a PCI device's remove is the fake kernel's here
            # (PCI_REMOVE_FAKE): the run's own function is replaced.
            body = re.compile(r"^pci_remove\(\) \{ # NAME\n.*?^\}\n", re.M | re.S)
            self.assertEqual(len(body.findall(text)), 1)
            text = body.sub(lambda m: PCI_REMOVE_FAKE.replace("@ROOT@", str(self.root))
                            .replace("@TMP@", str(self.tmp)), text)
        text = text.replace(pinned("install.sh", "hang_boot"), sha(self.hang_boot))
        text = text.replace(pinned("install.sh", "hang_vendor_boot"), sha(self.hang_vendor_boot))
        for real, standin in self.standins.items():
            text = text.replace(real, standin)
        if self.stepped:
            # The wrapped commands stop the script by its process ID.
            text = text.replace("\nset -eu\n", "\nset -eu\nSTEPPED_SCRIPT=$$; export STEPPED_SCRIPT\n", 1)
            self.assertIn("STEPPED_SCRIPT", text)
        # The script finds its kit by its own location.
        self.assertIn('kit=$(cd "$(dirname "$0")" && pwd)', text) if name != "rollback.sh" else None
        return text.replace('kit=$(cd "$(dirname "$0")" && pwd)', f"kit={self.kit}")

    def run_script(self, name, *args, **popen):
        path = self.tmp / f"run-{name}"
        path.write_text(self.script(name))
        env = {"PATH": f"{self.bin}:{getattr(self, 'host_path', os.environ['PATH'])}",
               "FAKE_ROOT": str(self.root), "HOME": str(self.tmp)}
        return subprocess.run(["sh", str(path), *args], env=env, capture_output=True, text=True,
                              timeout=120, **popen)

    def tree(self, path=None):
        path = path or self.modules
        return {str(p.relative_to(path)): p.read_text() for p in path.rglob("*") if p.is_file()}

    def install_log(self):
        return (self.root / "var/log/gts9u-typec-roles.log").read_text()

    def state(self):
        """What the scripts can change: the partitions, the module trees, the
        backups and the saved boot set, by path: a file's content, a
        directory's mode (the scripts set none of a file's).  A retired
        backup's time stamp is left out."""
        found = {}
        for top in ("dev/disk/by-partlabel", "usr/lib/modules", "var/backups", "var/lib"):
            for p in sorted((self.root / top).rglob("*")):
                name = re.sub(r"\.(rolled-back|unfinished)-[0-9]{8}-[0-9]{6}", r".\1-TIME",
                              str(p.relative_to(self.root)))
                found[name] = sha(p.read_bytes()) if p.is_file() else oct(p.stat().st_mode & 0o7777)
        return found

    def saved_set(self, boot):
        """A saved Ubuntu boot set (dual boot) whose boot.img is BOOT."""
        saved = self.root / "var/lib/gts9u-boot-sets/ubuntu"
        saved.mkdir(parents=True)
        (saved / "boot.img").write_bytes(boot)
        return saved

    def step_commands(self):
        """Wrap the commands that change the disk (STEP_COMMANDS): each call
        is counted, and the call that "stop" names kills the script or fails."""
        path = os.environ["PATH"]
        for name in STEP_COMMANDS:
            stub = self.bin / name
            if stub.exists():
                real = self.bin / f".real-{name}"
                stub.rename(real)
            else:
                real = shutil.which(name, path=path)
                self.assertIsNotNone(real, name)
            stub.write_text(STEP_WRAPPER % {"name": name, "real": shlex.quote(str(real))})
            stub.chmod(0o755)
        self.stepped = True

    def steps(self):
        """The steps of the runs since the last call: (command, arguments)."""
        log = self.root / "steps.log"
        lines = log.read_text().splitlines() if log.exists() else []
        log.unlink(missing_ok=True)
        (self.root / "steps").unlink(missing_ok=True)
        found = []
        for line in lines:
            _, command, *arguments = line.split(" ", 2)
            found.append((command, arguments[0] if arguments else ""))
        return found

    def run_stopped(self, name, how, number):
        """Run the script NAME stopped at its step NUMBER: killed before it
        (how = "kill"), as a power cut or a kill would stop it, or with the
        command of that step failing (how = "fail")."""
        self.steps()
        (self.root / "stop").write_text(f"{how} {number}\n")
        try:
            return self.run_script(name)
        finally:
            (self.root / "stop").unlink()
            self.steps()

    def install_r7(self, boot=None, build_id=R7_STANDIN_ID):
        """Revision 7 of the kit as its own install.sh left the SM-X710 on
        2026-11-21, and revisions 1 to 6 theirs: boot and the hang-log module
        tree saved in the backup (root-only), and the saved set's image too
        if there is a set; the kit's module tree in place, its image on boot,
        the set's image replaced by it, and "complete".  No record.  Then its
        kernel booted.  ScriptsTest checks this against revision 7's own
        install.sh."""
        boot = self.r7_boot if boot is None else boot
        self.backup = self.root / BACKUP
        self.backup.mkdir()
        self.backup.chmod(0o700)
        (self.backup / "boot-hang-log.img").write_bytes(self.part("boot").read_bytes())
        self.modules.rename(self.backup / f"modules-{RELEASE}")
        for name in MODULES:
            (self.modules / name).parent.mkdir(parents=True, exist_ok=True)
            (self.modules / name).write_text(f"r7 {name}\n")
        self.part("boot").write_bytes(boot)
        saved = self.root / "var/lib/gts9u-boot-sets/ubuntu/boot.img"
        if saved.exists():
            (self.backup / "saved-set-boot.img").write_bytes(saved.read_bytes())
            saved.write_bytes(boot)
        (self.backup / "complete").touch()
        self.running(build_id)

    def debug_switches(self, boot=None, **values):
        """Revision 9's debug switches as the kernel shows them, N unless
        VALUES says otherwise (by word: pcie0="Y"); a value of None leaves
        that switch out.  BOOT is revision 11's debug_boot_state (R11_BOOT
        on its kernel), and None leaves it out, as the kernels of revisions
        9 and 10 have it.  rpd3, revision 11's root port switch, is left out
        unless VALUES gives it."""
        for word, path in SWITCHES + (("rpd3", RPD3_SWITCH),):
            value = values.get(word, None if word == "rpd3" else "N")
            f = self.root / "sys/module" / path
            if value is None:
                f.unlink(missing_ok=True)
                continue
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(value + "\n")
        f = self.root / "sys/module" / BOOT_STATE
        if boot is None:
            f.unlink(missing_ok=True)
        else:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(boot + "\n")

    def r11_switches(self, **values):
        """The debug switches as revision 11's kernel boots with them:
        pcie0 Y (pcie_0_gdsc OFF_ON), the others N, the root port's N, and
        debug_boot_state."""
        self.debug_switches(boot=R11_BOOT, **{"pcie0": "Y", "rpd3": "N", **values})

    def r7_kit(self):
        """The kit directory as revision 7's packed kit, with stand-ins for
        its kernel and modules, and its own install.sh (OLD_INSTALLS)."""
        (self.kit / "boot.img").write_bytes(self.r7_boot)
        (self.kit / f"modules-{RELEASE}.tar").write_bytes(modules_tar("r7"))
        (self.kit / "BUILD_ID").write_text(R7_STANDIN_ID + "\n")
        self.seal()

    def newer_kit(self, marker="newer", build_id=NEWER_BUILD_ID):
        """The kit's next revision in the same place: another kernel and
        another module tree, with the same list of modules."""
        boot = os.urandom(SIZE)
        (self.kit / "boot.img").write_bytes(boot)
        (self.kit / f"modules-{RELEASE}.tar").write_bytes(modules_tar(marker))
        (self.kit / "BUILD_ID").write_text(build_id + "\n")
        self.seal()
        return boot

    def boot_kit_kernel(self, build_id):
        """A boot of the kernel BUILD_ID with the kit's module key, the
        hang-log kit's configuration and ramoops, and the switches as built:
        the driver's two, and the debug switches as revision 11's kernel
        boots with them (pcie0 on, the others off); the PCIe link up, and
        revision 10's power-up write line before the calibration entry."""
        self.running(build_id)
        params = self.root / "sys/module/sm5714_usbpd/parameters"
        (params / "sink_host_swap").write_text("Y\n")
        (params / "try_snk_extra_wait_ms").write_text("600\n")
        self.r11_switches()
        (self.root / "journal").write_text(
            f"kernel: Loaded X.509 cert 'Build time autogenerated kernel key: {KEY_ID}'\n"
            "kernel: ramoops: using 0x200000@0x880900000, ecc: 96\n"
            f"{PCIE_UP}\n{UFS_PWRUP}\n{UFS_CAL_ENTRY}\n")
        (self.root / "signer").write_text(SIGNER + "\n")
        with gzip.open(self.root / "proc/config.gz", "wt") as config:
            config.write("CONFIG_DETECT_HUNG_TASK=y\nCONFIG_SOFTLOCKUP_DETECTOR=y\n"
                         "CONFIG_WQ_WATCHDOG=y\nCONFIG_NETCONSOLE=m\nCONFIG_PSTORE_RAM=y\n"
                         "CONFIG_PSTORE_FTRACE=y\n")


class InstallTest(FakeTablet):
    def assert_untouched(self):
        self.assertEqual(self.part("boot").read_bytes(), self.hang_boot)
        self.assertEqual(self.part("vendor_boot").read_bytes(), self.hang_vendor_boot)
        self.assertEqual(self.tree()["updates/b.ko"], "hang updates/b.ko\n")
        self.assertFalse((self.root / BACKUP).exists())

    def test_install_writes_boot_and_the_module_tree_only(self):
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Type-C roles kit installed; reboot now", result.stdout)
        self.assertEqual(self.part("boot").read_bytes(), self.new_boot)
        self.assertEqual(self.part("vendor_boot").read_bytes(), self.hang_vendor_boot)
        self.assertEqual(self.tree(), {n: f"new {n}\n" for n in
                                       ("modules.dep", "kernel/net/a.ko", "updates/b.ko")})
        backup = self.root / BACKUP
        self.assertEqual((backup / "boot-hang-log.img").read_bytes(), self.hang_boot)
        self.assertEqual((backup / f"modules-{RELEASE}/updates/b.ko").read_text(), "hang updates/b.ko\n")
        self.assertTrue((backup / "complete").exists())
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertEqual([p.name for p in (self.root / "usr/lib/modules").iterdir()], [RELEASE])
        self.assertNotIn("STOP", self.install_log())

    def test_install_records_what_it_writes(self):
        """For the next revision's install.sh: the boot image and the kernel,
        on the disk before boot is written (the sync test below)."""
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        backup = self.root / BACKUP
        self.assertEqual((backup / "revisions").read_text(), f"{sha(self.new_boot)} {NEW_BUILD_ID}\n")
        self.assertEqual(sorted(p.name for p in backup.iterdir()),
                         ["boot-hang-log.img", "complete", f"modules-{RELEASE}", "revisions"])

    def test_rollback_restores_the_hang_log_kit_and_install_runs_again(self):
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("back on the hang-log kit", result.stdout)
        self.assertEqual(self.part("boot").read_bytes(), self.hang_boot)
        self.assertEqual(self.tree(), {n: f"hang {n}\n" for n in
                                       ("modules.dep", "kernel/net/a.ko", "updates/b.ko")})
        self.assertFalse((self.root / BACKUP).exists())
        retired = list((self.root / "var/backups").glob("ubuntu-gts9u-typec-roles.rolled-back-*"))
        self.assertEqual(len(retired), 1)
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        self.assertEqual(self.part("boot").read_bytes(), self.new_boot)

    def test_the_same_revision_again_changes_nothing_and_says_so(self):
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        before = self.state()
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"STOP: this revision of the Type-C roles kit is already installed "
                      f"({self.root / BACKUP}); nothing was changed", result.stdout)
        self.assertNotIn("ended early", result.stdout)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.part("boot").read_bytes(), self.new_boot)
        self.assertEqual(self.tree()["updates/b.ko"], "new updates/b.ko\n")

    def test_saved_boot_set_follows_install_and_rollback(self):
        # Not the hang-log kit's image: what goes back is what the set held.
        found = os.urandom(SIZE)
        saved = self.saved_set(found)
        (saved / "vendor_boot.img").write_bytes(self.hang_vendor_boot)
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        self.assertEqual((saved / "boot.img").read_bytes(), self.new_boot)
        self.assertEqual((saved / "vendor_boot.img").read_bytes(), self.hang_vendor_boot)
        self.assertEqual((self.root / BACKUP / "saved-set-boot.img").read_bytes(), found)
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("saved Ubuntu boot set: boot.img restored (from saved-set-boot.img)",
                      result.stdout)
        self.assertEqual((saved / "boot.img").read_bytes(), found)
        self.assertEqual(sorted(p.name for p in saved.iterdir()), ["boot.img", "vendor_boot.img"])

    def test_an_empty_copy_of_the_saved_set_s_image_is_replaced_by_the_hang_log_kit_s(self):
        """What a power failure right after install.sh can leave of the copy."""
        saved = self.saved_set(self.hang_boot)
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        (self.root / BACKUP / "saved-set-boot.img").write_bytes(b"")
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("saved Ubuntu boot set: boot.img restored (from boot-hang-log.img)",
                      result.stdout)
        self.assertEqual((saved / "boot.img").read_bytes(), self.hang_boot)

    def test_rollback_finishes_when_the_saved_boot_set_is_gone(self):
        found = os.urandom(SIZE)
        saved = self.saved_set(found)
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        shutil.rmtree(saved)
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"saved Ubuntu boot set: {saved} is gone; its boot.img stays in the "
                      "retired backup", result.stdout)
        self.assertIn("back on the hang-log kit; reboot now", result.stdout)
        self.assertFalse(saved.exists())
        (retired,) = (self.root / "var/backups").glob("ubuntu-gts9u-typec-roles.rolled-back-*")
        self.assertEqual((retired / "saved-set-boot.img").read_bytes(), found)

    def test_a_rollback_that_cannot_restore_the_saved_set_says_what_is_done_and_runs_again(self):
        saved = self.saved_set(self.hang_boot)
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        # cp puts its copy inside the directory, and mv does not put a
        # directory in a file's place.
        (saved / "boot.img.new").mkdir()
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"STOP: restoring {saved}/boot.img failed", result.stdout)
        self.assertIn("STOP: rollback ended early; the hang-log kernel and its modules are back "
                      "in place; run rollback.sh again to finish", result.stdout)
        self.assertNotIn("do NOT reboot", result.stdout)
        self.assertEqual(self.part("boot").read_bytes(), self.hang_boot)
        self.assertEqual(self.tree(), {n: f"hang {n}\n" for n in MODULES})
        self.assertEqual((saved / "boot.img").read_bytes(), self.new_boot)
        self.assertTrue((self.root / BACKUP).exists())
        shutil.rmtree(saved / "boot.img.new")
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("back on the hang-log kit; reboot now", result.stdout)
        self.assertEqual((saved / "boot.img").read_bytes(), self.hang_boot)
        self.assertEqual([p.name for p in saved.iterdir()], ["boot.img"])
        self.assertFalse((self.root / BACKUP).exists())

    def test_install_syncs_the_saved_image_then_the_modules_then_the_finished_kit(self):
        """What a power failure right after each sync would find: the way
        back before anything else changes, the kit's modules before its
        kernel is written, and the install as it ends."""
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        hang, new = sha(self.hang_boot), sha(self.new_boot)
        first = {"saved": "yes", "upgrading": "no"}
        self.assertEqual(self.syncs(), [
            {**first, "tree": "hang", "boot": hang, "complete": "no", "recorded": "none"},
            # The record of what is written comes before the flash.
            {**first, "tree": "new", "boot": hang, "complete": "no", "recorded": new},
            {**first, "tree": "new", "boot": new, "complete": "yes", "recorded": new},
        ])
        # What the locked-down kernel means until the reboot and afterwards.
        self.assertIn("until the reboot the running kernel can load no module it has not loaded "
                      "yet", result.stdout)
        self.assertIn("while the kit is installed, do not upgrade ubuntu-gts9u-device",
                      result.stdout)

    def test_a_sync_that_fails_before_the_flash_stops_the_install_and_rollback_undoes_it(self):
        for number in (1, 2):
            with self.subTest(number):
                FakeTablet.setUp(self)
                result = self.run_with_failing_sync("install.sh", number)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("STOP: sync failed", result.stdout)
                self.assertIn("STOP: install ended early; do NOT reboot before running rollback.sh",
                              result.stdout)
                self.assertNotIn("writing boot", result.stdout)
                self.assertEqual(self.part("boot").read_bytes(), self.hang_boot)
                self.assertFalse((self.root / BACKUP / "complete").exists())
                # The first sync is before the module swap, the second after it.
                self.assertEqual(self.tree()["updates/b.ko"],
                                 ("hang", "new")[number - 1] + " updates/b.ko\n")
                result = self.run_script("rollback.sh")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.part("boot").read_bytes(), self.hang_boot)
                self.assertEqual(self.tree(), {n: f"hang {n}\n" for n in MODULES})
                self.assertFalse((self.root / BACKUP).exists())

    def test_a_last_sync_that_fails_is_reported_with_the_kit_in_place(self):
        result = self.run_with_failing_sync("install.sh", 3)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('STOP: the kit is in place, but sync failed: run "sudo sync" until it '
                      "succeeds, then reboot", result.stdout)
        self.assertNotIn("ended early", result.stdout)
        self.assertNotIn("reboot now", result.stdout)
        self.assertTrue((self.root / BACKUP / "complete").exists())
        self.assertEqual(self.part("boot").read_bytes(), self.new_boot)
        self.assertEqual(self.tree(), {n: f"new {n}\n" for n in MODULES})

    def test_rollback_syncs_the_hang_log_kit_before_the_rest_and_again_at_the_end(self):
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        self.syncs()
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        hang_log = {"tree": "hang", "boot": sha(self.hang_boot), "upgrading": "no"}
        self.assertEqual(self.syncs(), [
            # The backup is still where a second run would look for it.
            {**hang_log, "saved": "yes", "complete": "yes", "recorded": sha(self.new_boot)},
            {**hang_log, "saved": "no", "complete": "no", "recorded": "none"},
        ])

    def test_a_rollback_that_stops_says_so_and_a_second_run_finishes_it(self):
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        result = self.run_with_failing_sync("rollback.sh", 1)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("STOP: sync failed", result.stdout)
        self.assertIn("STOP: rollback ended early; do NOT reboot before running rollback.sh again",
                      result.stdout)
        # The kernel and its modules are back; the rest is not.
        self.assertEqual(self.part("boot").read_bytes(), self.hang_boot)
        self.assertEqual(self.tree(), {n: f"hang {n}\n" for n in MODULES})
        self.assertTrue((self.root / BACKUP).exists())
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("back on the hang-log kit; reboot now", result.stdout)
        self.assertNotIn("STOP", result.stdout)
        self.assertEqual(self.part("boot").read_bytes(), self.hang_boot)
        self.assertEqual(self.tree(), {n: f"hang {n}\n" for n in MODULES})
        self.assertFalse((self.root / BACKUP).exists())

    def test_rollback_s_last_sync_failing_is_reported_with_the_hang_log_kit_in_place(self):
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        result = self.run_with_failing_sync("rollback.sh", 2)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('STOP: the hang-log kit is back in place, but sync failed: run "sudo sync" '
                      "until it succeeds, then reboot", result.stdout)
        self.assertNotIn("ended early", result.stdout)
        self.assertNotIn("reboot now", result.stdout)
        self.assertEqual(self.part("boot").read_bytes(), self.hang_boot)
        self.assertEqual(self.tree(), {n: f"hang {n}\n" for n in MODULES})
        self.assertFalse((self.root / BACKUP).exists())

    def test_refusals_change_nothing(self):
        cases = {
            "the hang-log kit is not installed":
                lambda: (self.root / "var/backups/ubuntu-gts9u-hang-log/complete").unlink(),
            "expected the hang-log kit's": lambda: self.running(NEW_BUILD_ID),
            "the boot partition does not hold the hang-log kit":
                lambda: self.part("boot").write_bytes(self.new_boot),
            "does not match its checksums":
                lambda: (self.kit / "BUILD_ID").write_text("0" * 40 + "\n"),
            "does not have the hang-log kit's list of modules":
                lambda: (self.modules / "updates/by-hand.ko").write_text("hang extra\n"),
            "battery at 12 %":
                lambda: (self.root / "sys/class/power_supply/sm5714-battery/capacity").write_text("12\n"),
            "this is not an SM-X710":
                lambda: (self.root / "proc/device-tree/compatible").write_bytes(b"samsung,gts9u\0"),
            "the root filesystem is not writable":
                lambda: (self.root / "mount-options").write_text("ro,relatime\n"),
            "dpkg is busy": lambda: (self.root / "dpkg-busy").touch(),
            "BUILD_ID in the kit is not a kernel build ID": lambda: (
                (self.kit / "BUILD_ID").write_text(NEW_BUILD_ID.upper() + "\n"), self.seal()),
        }
        for message, damage in cases.items():
            with self.subTest(message):
                FakeTablet.setUp(self)
                damage()
                before = self.state()
                result = self.run_script("install.sh")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(message, result.stdout)
                self.assertNotIn("ended early", result.stdout)
                self.assertEqual(self.state(), before)
                self.assertEqual(self.tree()["updates/b.ko"], "hang updates/b.ko\n")
                self.assertFalse((self.root / BACKUP).exists())

    def test_a_root_that_ext4_gave_up_on_is_refused(self):
        # ext4 keeps "rw" among the options and adds one of these.
        for options in ("rw,noatime,emergency_ro", "rw,relatime,shutdown"):
            with self.subTest(options):
                FakeTablet.setUp(self)
                (self.root / "mount-options").write_text(options + "\n")
                before = self.state()
                result = self.run_script("install.sh")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("the root filesystem is not writable", result.stdout)
                self.assertEqual(self.state(), before)
                self.assertFalse((self.root / BACKUP).exists())
        FakeTablet.setUp(self)
        (self.root / "mount-options").write_text("rw,noatime,errors=remount-ro\n")
        self.assertEqual(self.run_script("install.sh").returncode, 0)

    def test_vendor_boot_from_another_kit_is_refused(self):
        self.part("vendor_boot").write_bytes(os.urandom(SIZE))
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("vendor_boot does not hold the hang-log kit", result.stdout)
        self.assertEqual(self.part("boot").read_bytes(), self.hang_boot)
        self.assertFalse((self.root / BACKUP).exists())

    def test_kit_with_another_module_list_is_refused(self):
        (self.kit / f"modules-{RELEASE}.tar").write_bytes(modules_tar("new", extra=("kernel/net/c.ko",)))
        self.seal()
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("list of modules", result.stdout)
        self.assert_untouched()

    def test_rollback_without_an_install_is_refused(self):
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("install.sh never ran", result.stdout)
        self.assertEqual(self.part("boot").read_bytes(), self.hang_boot)

    def test_rollback_retires_an_install_that_stopped_before_changing_anything(self):
        (self.root / BACKUP).mkdir()
        refused = self.run_script("install.sh")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("did not finish; run rollback.sh first", refused.stdout)
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("stopped before it changed anything", result.stdout)
        self.assertFalse((self.root / BACKUP).exists())
        self.assertEqual(self.part("boot").read_bytes(), self.hang_boot)
        self.assertEqual(self.run_script("install.sh").returncode, 0)

    def test_rollback_refuses_a_changed_saved_image_after_the_flash(self):
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        (self.root / BACKUP / "boot-hang-log.img").write_bytes(os.urandom(SIZE))
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing or changed; nothing was changed", result.stdout)
        self.assertEqual(self.part("boot").read_bytes(), self.new_boot)
        self.assertEqual(self.tree()["updates/b.ko"], "new updates/b.ko\n")


class UpgradeHelpers:
    """A tablet with an earlier revision of the kit installed: revision 7 as
    its own install.sh left it, which recorded nothing, or a revision that
    recorded what it wrote.  The kit directory holds the next revision."""

    def setUp(self):
        super().setUp()
        self.backup = self.root / BACKUP
        # The revision the kit directory holds: its module marker, boot
        # image and kernel.
        self.expected = ("new", self.new_boot, NEW_BUILD_ID)

    def install_recorded(self):
        """A revision that records what it writes (this one, by a first
        install), installed and booted; the kit directory then holds the
        next revision."""
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.running(NEW_BUILD_ID)
        self.newer_boot = self.newer_kit()
        self.expected = ("newer", self.newer_boot, NEWER_BUILD_ID)

    def assert_installed(self):
        marker, boot, build_id = self.expected
        self.assertEqual(self.part("boot").read_bytes(), boot)
        self.assertEqual(self.part("vendor_boot").read_bytes(), self.hang_vendor_boot)
        self.assertEqual(self.tree(), {n: f"{marker} {n}\n" for n in MODULES})
        # The way back is what the first install saved, as it saved it.
        self.assertEqual((self.backup / "boot-hang-log.img").read_bytes(), self.hang_boot)
        self.assertEqual(self.tree(self.backup / f"modules-{RELEASE}"),
                         {n: f"hang {n}\n" for n in MODULES})
        self.assertEqual(self.backup.stat().st_mode & 0o777, 0o700)
        self.assertEqual([p.name for p in (self.root / "usr/lib/modules").iterdir()], [RELEASE])
        self.assertEqual((self.backup / "revisions").read_text().splitlines()[-1],
                         f"{sha(boot)} {build_id}")
        self.assertFalse((self.backup / "upgrading").exists())
        self.assertTrue((self.backup / "complete").exists())

    def assert_upgraded(self, result, found):
        """RESULT is an upgrade that found FOUND on boot and finished."""
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"upgrade: boot holds {sha(found)[:12]}; the hang-log kit saved in "
                      f"{self.backup} stays the way back", result.stdout)
        self.assertIn("until the reboot the module tree in place is built for the new kernel, not "
                      "for the running one", result.stdout)
        self.assertNotIn("can load no module", result.stdout)
        self.assertIn("Type-C roles kit installed; reboot now", result.stdout)
        self.assert_installed()

    def assert_on_the_hang_log_kit(self):
        self.assertEqual(self.part("boot").read_bytes(), self.hang_boot)
        self.assertEqual(self.part("vendor_boot").read_bytes(), self.hang_vendor_boot)
        self.assertEqual(self.tree(), {n: f"hang {n}\n" for n in MODULES})
        self.assertEqual([p.name for p in (self.root / "usr/lib/modules").iterdir()], [RELEASE])
        self.assertFalse(self.backup.exists())

    def assert_rolled_back(self):
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("back on the hang-log kit; reboot now", result.stdout)
        self.assert_on_the_hang_log_kit()
        self.assertTrue(list((self.root / "var/backups").glob("ubuntu-gts9u-typec-roles.rolled-back-*")))


class UpgradeTest(UpgradeHelpers, FakeTablet):
    """install.sh of a later revision of the kit on a tablet that has an
    earlier one, and rollback.sh after it."""

    def old_script(self, blob, digest):
        """An earlier revision's script, from this checkout's git objects."""
        found = subprocess.run(["git", "-C", str(REPO), "cat-file", "blob", blob],
                               capture_output=True)
        if found.returncode != 0:
            self.skipTest(f"no git object {blob} (an earlier revision's script) here")
        self.assertEqual(sha(found.stdout), digest)
        return found.stdout

    def test_revision_7_s_own_install_sh_leaves_what_these_tests_start_from(self):
        """install_r7() builds what revision 7's install.sh left on the
        tablet, and revision 5's and 6's: run here, each leaves the same
        partitions, module trees, backup and saved boot set.  This revision's
        first install leaves the same and its record."""
        for name, digest, blob in OLD_INSTALLS:
            for with_set in (False, True):
                with self.subTest(name, saved_set=with_set):
                    self.setUp()
                    if with_set:
                        self.saved_set(os.urandom(SIZE))
                    before = self.tmp / "before"
                    shutil.copytree(self.root, before)
                    this_install = (self.kit / "install.sh").read_bytes()
                    (self.kit / "install.sh").write_bytes(self.old_script(blob, digest))
                    self.r7_kit()
                    result = self.run_script("install.sh")
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn(f"boot written and read back ({sha(self.r7_boot)[:12]})",
                                  result.stdout)
                    by_its_install = self.state()
                    shutil.rmtree(self.root)
                    shutil.copytree(before, self.root)
                    self.install_r7()
                    self.assertEqual(self.state(), by_its_install)
                    shutil.rmtree(self.root)
                    shutil.copytree(before, self.root)
                    (self.kit / "install.sh").write_bytes(this_install)
                    self.seal()
                    result = self.run_script("install.sh")
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    record = self.backup / "revisions"
                    self.assertEqual(record.read_text(), f"{sha(self.r7_boot)} {R7_STANDIN_ID}\n")
                    record.unlink()
                    self.assertEqual(self.state(), by_its_install)

    def test_revision_7_s_own_rollback_sh_undoes_a_finished_upgrade(self):
        """Run from the earlier kit's directory by mistake: it knows nothing of
        upgrades, and a finished one leaves nothing it does not undo."""
        found = os.urandom(SIZE)
        saved = self.saved_set(found)
        self.install_r7()
        self.assert_upgraded(self.run_script("install.sh"), self.r7_boot)
        digest, blob = OLD_ROLLBACK
        (self.kit / "rollback.sh").write_bytes(self.old_script(blob, digest))
        self.assert_rolled_back()
        self.assertEqual((saved / "boot.img").read_bytes(), found)

    def test_squirtle_as_its_own_scripts_left_it_is_upgraded_in_place(self):
        """The SM-X710 as it is on 2026-11-22, made by the scripts that made
        it: revision 6 installed by its own install.sh and booted, rolled back
        by its own rollback.sh, revision 7 installed by its own install.sh and
        booted, a sleep test of revision 8's sleep-test.sh refused (it
        changes nothing then) and a forced restart (nothing on the disk of
        the kit's changes).  Revision 8's install.sh then upgrades it with
        one command, and rollback.sh still goes back to the hang-log kit."""
        kit_scripts = {name: (self.kit / name).read_bytes() for name in ("install.sh", "rollback.sh")}
        r6_boot, r6_id = os.urandom(SIZE), "6" * 40
        (self.kit / "boot.img").write_bytes(r6_boot)
        (self.kit / f"modules-{RELEASE}.tar").write_bytes(modules_tar("r6"))
        (self.kit / "BUILD_ID").write_text(r6_id + "\n")
        name, digest, blob = OLD_INSTALLS[1]
        self.assertEqual(name, "r5 and r6")
        (self.kit / "install.sh").write_bytes(self.old_script(blob, digest))
        digest, blob = OLD_ROLLBACK
        (self.kit / "rollback.sh").write_bytes(self.old_script(blob, digest))
        self.seal()
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.running(r6_id)
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("back on the hang-log kit", result.stdout)
        self.running(HANG_BUILD_ID)
        (retired,) = (self.root / "var/backups").glob("ubuntu-gts9u-typec-roles.rolled-back-*")
        name, digest, blob = OLD_INSTALLS[0]
        self.assertEqual(name, "r7")
        (self.kit / "install.sh").write_bytes(self.old_script(blob, digest))
        self.r7_kit()
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.running(R7_STANDIN_ID)
        self.assertFalse((self.backup / "revisions").exists())
        # The kit directory now holds revision 8, copied over revision 7's.
        (self.kit / "boot.img").write_bytes(self.new_boot)
        (self.kit / f"modules-{RELEASE}.tar").write_bytes(modules_tar("new"))
        (self.kit / "BUILD_ID").write_text(NEW_BUILD_ID + "\n")
        for name, data in kit_scripts.items():
            (self.kit / name).write_bytes(data)
        self.seal()
        before = self.state()
        refused = self.run_script("sleep-test.sh", "ro")
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertEqual(self.state(), before)
        self.assert_upgraded(self.run_script("install.sh"), self.r7_boot)
        self.assertEqual((self.backup / "revisions").read_text(), f"{sha(self.new_boot)} {NEW_BUILD_ID}\n")
        # Revision 6's retired backup is left as it was.
        self.assertTrue((retired / "boot-hang-log.img").exists())
        self.assert_rolled_back()

    def r8_kit(self):
        """The kit directory as revision 8's packed kit (every repack of it
        has the same install.sh, rollback.sh, boot.img and kernel), with
        stand-ins for its kernel and modules, its own install.sh and
        rollback.sh (R8_SCRIPTS), its REVISION and a log of one of its sleep
        tests.  Returns its boot image."""
        boot = os.urandom(SIZE)
        (self.kit / "boot.img").write_bytes(boot)
        (self.kit / f"modules-{RELEASE}.tar").write_bytes(modules_tar("r8"))
        (self.kit / "BUILD_ID").write_text(R8_STANDIN_ID + "\n")
        (self.kit / "REVISION").write_text("8\n")
        for name, (digest, blob) in R8_SCRIPTS.items():
            (self.kit / name).write_bytes(self.old_script(blob, digest))
        logs = self.kit / "logs/20261122-164431-sleep"
        logs.mkdir(parents=True)
        (logs / "before").write_text("sleep-test.sh 20261122-164431: 1 suspend-to-idle cycles of 60s\n")
        self.seal()
        return boot

    def this_kit(self, scripts):
        """This revision's packed kit copied over the kit directory, as the
        README's scp does: every file of the kit replaced, logs left as they
        are."""
        (self.kit / "boot.img").write_bytes(self.new_boot)
        (self.kit / f"modules-{RELEASE}.tar").write_bytes(modules_tar("new"))
        (self.kit / "BUILD_ID").write_text(NEW_BUILD_ID + "\n")
        (self.kit / "REVISION").write_text("11\n")
        for name, data in scripts.items():
            (self.kit / name).write_bytes(data)
        self.seal()

    def own_r9_kit(self):
        """The kit directory as revision 9's packed kit, with stand-ins for
        its kernel and modules and its own scripts (R9_SCRIPTS), as the
        owner copied it to squirtle on 2026-11-22.  Returns its boot image."""
        boot = os.urandom(SIZE)
        (self.kit / "boot.img").write_bytes(boot)
        (self.kit / f"modules-{RELEASE}.tar").write_bytes(modules_tar("r9"))
        (self.kit / "BUILD_ID").write_text(R9_STANDIN_ID + "\n")
        (self.kit / "REVISION").write_text("9\n")
        for name, (digest, blob) in R9_SCRIPTS.items():
            (self.kit / name).write_bytes(self.old_script(blob, digest))
        self.seal()
        return boot

    def test_squirtle_on_2026_11_23_with_revision_9_is_upgraded_to_this_revision_in_place(self):
        """What squirtle has on 2026-11-23: revision 7 installed by its own
        install.sh, then revision 9 installed over it by its own (which
        records what it writes) and booted, its sleep tests' logs in the kit
        directory.  This revision's files copied over revision 9's: one
        install and no rollback, the record has both, a second install
        changes nothing, and rollback.sh still goes back to the hang-log
        kit."""
        scripts = {name: (self.kit / name).read_bytes() for name in ("install.sh", "rollback.sh", "verify.sh",
                                                                    "sleep-test.sh", "README.md")}
        self.install_r7()
        r9_boot = self.own_r9_kit()
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"upgrade: boot holds {sha(self.r7_boot)[:12]}", result.stdout)
        self.assertEqual(self.part("boot").read_bytes(), r9_boot)
        self.assertEqual((self.backup / "revisions").read_text(), f"{sha(r9_boot)} {R9_STANDIN_ID}\n")
        self.running(R9_STANDIN_ID)
        logs = self.kit / "logs/20261123-001433-sleep"
        logs.mkdir(parents=True)
        (logs / "before").write_text("sleep-test 20261123-001433: 1 suspend-to-idle cycles of 30s\n")
        self.this_kit(scripts)
        self.assert_upgraded(self.run_script("install.sh"), r9_boot)
        self.assertEqual((self.backup / "revisions").read_text(),
                         f"{sha(r9_boot)} {R9_STANDIN_ID}\n{sha(self.new_boot)} {NEW_BUILD_ID}\n")
        self.assertTrue((logs / "before").exists())
        for rebooted in (False, True):
            with self.subTest(rebooted=rebooted):
                self.running(NEW_BUILD_ID if rebooted else R9_STANDIN_ID)
                before = self.state()
                result = self.run_script("install.sh")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("STOP: this revision of the Type-C roles kit is already installed "
                              f"({self.backup}); nothing was changed", result.stdout)
                self.assertEqual(self.state(), before)
        self.assert_rolled_back()

    def own_r10_kit(self):
        """The kit directory as revision 10's packed kit, with stand-ins for
        its kernel and modules and its own scripts (R10_SCRIPTS), as the
        owner copied it to squirtle on 2026-11-23.  Returns its boot image."""
        boot = os.urandom(SIZE)
        (self.kit / "boot.img").write_bytes(boot)
        (self.kit / f"modules-{RELEASE}.tar").write_bytes(modules_tar("r10"))
        (self.kit / "BUILD_ID").write_text(R10_STANDIN_ID + "\n")
        (self.kit / "REVISION").write_text("10\n")
        for name, (digest, blob) in R10_SCRIPTS.items():
            (self.kit / name).write_bytes(self.old_script(blob, digest))
        self.seal()
        return boot

    def test_squirtle_after_2026_11_23_with_revision_10_is_upgraded_to_this_revision_in_place(self):
        """What squirtle has after 2026-11-23 11:37: revision 7 installed by
        its own install.sh, revision 9 over it by its own, revision 10 over
        that by its own (each recording what it writes) and booted, sleep
        tests' logs in the kit directory.  This revision's files copied over
        revision 10's: one install and no rollback, the record has all
        three, a second install changes nothing, and rollback.sh still goes
        back to the hang-log kit."""
        scripts = {name: (self.kit / name).read_bytes() for name in ("install.sh", "rollback.sh", "verify.sh",
                                                                    "sleep-test.sh", "README.md")}
        self.install_r7()
        r9_boot = self.own_r9_kit()
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        self.running(R9_STANDIN_ID)
        r10_boot = self.own_r10_kit()
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"upgrade: boot holds {sha(r9_boot)[:12]}", result.stdout)
        self.assertEqual(self.part("boot").read_bytes(), r10_boot)
        self.running(R10_STANDIN_ID)
        record = f"{sha(r9_boot)} {R9_STANDIN_ID}\n{sha(r10_boot)} {R10_STANDIN_ID}\n"
        self.assertEqual((self.backup / "revisions").read_text(), record)
        logs = self.kit / "logs/20261123-122339-sleep"
        logs.mkdir(parents=True)
        (logs / "before").write_text("sleep-test 20261123-122339: 1 suspend-to-idle cycles of 30s\n")
        self.this_kit(scripts)
        self.assert_upgraded(self.run_script("install.sh"), r10_boot)
        self.assertEqual((self.backup / "revisions").read_text(), record + f"{sha(self.new_boot)} {NEW_BUILD_ID}\n")
        self.assertTrue((logs / "before").exists())
        for rebooted in (False, True):
            with self.subTest(rebooted=rebooted):
                self.running(NEW_BUILD_ID if rebooted else R10_STANDIN_ID)
                before = self.state()
                result = self.run_script("install.sh")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("STOP: this revision of the Type-C roles kit is already installed "
                              f"({self.backup}); nothing was changed", result.stdout)
                self.assertEqual(self.state(), before)
        # The way back to revision 10 from TWRP (the README): its image on
        # boot again, its kernel booted; install.sh of this revision then
        # runs as an upgrade over it, as over any revision the record has.
        self.part("boot").write_bytes(r10_boot)
        self.running(R10_STANDIN_ID)
        self.assert_upgraded(self.run_script("install.sh"), r10_boot)
        self.assert_rolled_back()

    def test_squirtle_on_2026_11_22_evening_is_upgraded_to_this_revision_in_place(self):
        """Revision 7 installed by its own install.sh and running, revision
        8's files copied into ~/typec-roles and never installed (only its
        sleep tests ran, which leave their logs and change nothing of the
        install), then this revision's files copied over them: one install,
        and a second one changes nothing.  rollback.sh still goes back to the
        hang-log kit."""
        scripts = {name: (self.kit / name).read_bytes() for name in ("install.sh", "rollback.sh", "verify.sh",
                                                                    "sleep-test.sh", "README.md")}
        self.install_r7()
        self.r8_kit()
        before = self.state()
        refused = self.run_script("sleep-test.sh", "ro")
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertEqual(self.state(), before)
        self.this_kit(scripts)
        self.assert_upgraded(self.run_script("install.sh"), self.r7_boot)
        self.assertEqual((self.backup / "revisions").read_text(), f"{sha(self.new_boot)} {NEW_BUILD_ID}\n")
        self.assertTrue((self.kit / "logs/20261122-164431-sleep/before").exists())
        for rebooted in (False, True):
            with self.subTest(rebooted=rebooted):
                self.running(NEW_BUILD_ID if rebooted else R7_STANDIN_ID)
                before = self.state()
                result = self.run_script("install.sh")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("STOP: this revision of the Type-C roles kit is already installed "
                              f"({self.backup}); nothing was changed", result.stdout)
                self.assertEqual(self.state(), before)
        self.assert_rolled_back()

    def test_revision_8_installed_by_its_own_install_sh_is_upgraded_to_this_revision(self):
        """Revision 8 installed over revision 7 by its own install.sh (which
        records what it writes) and booted, then this revision's files: one
        install, the record has both, and rollback.sh goes back to the
        hang-log kit."""
        scripts = {name: (self.kit / name).read_bytes() for name in ("install.sh", "rollback.sh", "verify.sh",
                                                                    "sleep-test.sh", "README.md")}
        self.install_r7()
        r8_boot = self.r8_kit()
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.part("boot").read_bytes(), r8_boot)
        self.assertEqual((self.backup / "revisions").read_text(), f"{sha(r8_boot)} {R8_STANDIN_ID}\n")
        self.running(R8_STANDIN_ID)
        self.this_kit(scripts)
        self.assert_upgraded(self.run_script("install.sh"), r8_boot)
        self.assertEqual((self.backup / "revisions").read_text(),
                         f"{sha(r8_boot)} {R8_STANDIN_ID}\n{sha(self.new_boot)} {NEW_BUILD_ID}\n")
        self.running(NEW_BUILD_ID)
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("this revision of the Type-C roles kit is already installed", result.stdout)
        self.assert_rolled_back()

    def test_revision_7_is_upgraded_in_place(self):
        """The SM-X710 from 2026-11-21 22:39 on: one command, no rollback."""
        self.install_r7()
        self.assert_upgraded(self.run_script("install.sh"), self.r7_boot)
        self.assertEqual((self.backup / "revisions").read_text(),
                         f"{sha(self.new_boot)} {NEW_BUILD_ID}\n")
        self.assertEqual(sorted(p.name for p in self.backup.iterdir()),
                         ["boot-hang-log.img", "complete", f"modules-{RELEASE}", "revisions"])
        self.assertNotIn("STOP", self.install_log())
        self.assert_rolled_back()

    def test_every_revision_before_the_record_is_known_by_its_boot_image_and_kernel(self):
        for boot, build_id, name in EARLIER:
            with self.subTest(name):
                self.setUp()
                image, standin = os.urandom(SIZE), "a" * 40
                self.standins.update({boot: sha(image), build_id: standin})
                self.install_r7(boot=image, build_id=standin)
                self.assert_upgraded(self.run_script("install.sh"), image)

    def test_a_revision_that_recorded_what_it_wrote_is_upgraded(self):
        self.install_recorded()
        self.assert_upgraded(self.run_script("install.sh"), self.new_boot)
        self.assertEqual((self.backup / "revisions").read_text(),
                         f"{sha(self.new_boot)} {NEW_BUILD_ID}\n"
                         f"{sha(self.newer_boot)} {NEWER_BUILD_ID}\n")
        self.assert_rolled_back()

    def test_after_any_number_of_upgrades_rollback_restores_the_hang_log_kit(self):
        found = os.urandom(SIZE)
        saved = self.saved_set(found)
        self.install_r7()
        self.assert_upgraded(self.run_script("install.sh"), self.r7_boot)
        self.running(NEW_BUILD_ID)
        previous = self.new_boot
        for marker, build_id in (("newer", NEWER_BUILD_ID), ("newest", NEWEST_BUILD_ID)):
            boot = self.newer_kit(marker, build_id)
            self.expected = (marker, boot, build_id)
            self.assert_upgraded(self.run_script("install.sh"), previous)
            self.assertEqual((saved / "boot.img").read_bytes(), boot)
            self.assertEqual((self.backup / "saved-set-boot.img").read_bytes(), found)
            self.running(build_id)
            previous = boot
        self.assertEqual(len((self.backup / "revisions").read_text().splitlines()), 3)
        self.assert_rolled_back()
        self.assertEqual((saved / "boot.img").read_bytes(), found)

    def test_an_upgrade_runs_on_any_kernel_the_tablet_can_be_running(self):
        """The hang-log kit's after a restore of boot from TWRP, or before the
        reboot of a first install; an earlier revision's, before or after the
        reboot of its install; this one's, after boot was written from TWRP."""
        for name, build_id in (("the hang-log kit's", HANG_BUILD_ID), ("revision 7's", R7_STANDIN_ID),
                               ("revision 1's", EARLIER[0][1]), ("this revision's", NEW_BUILD_ID)):
            with self.subTest(name):
                self.setUp()
                self.install_r7(build_id=build_id)
                self.assert_upgraded(self.run_script("install.sh"), self.r7_boot)

    def test_a_kernel_anywhere_in_the_record_may_run(self):
        """Two upgrades without a reboot between them: the second one runs on
        the kernel of the install before the first, which is not the record's
        last line."""
        self.install_recorded()
        self.assert_upgraded(self.run_script("install.sh"), self.new_boot)
        boot = self.newer_kit("newest", NEWEST_BUILD_ID)
        self.expected = ("newest", boot, NEWEST_BUILD_ID)
        self.assert_upgraded(self.run_script("install.sh"), self.newer_boot)

    def test_an_upgrade_on_a_kernel_of_no_revision_is_refused(self):
        self.install_r7(build_id="f" * 40)
        before = self.state()
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"STOP: running kernel build ID {'f' * 40}, neither the hang-log kit's nor a "
                      "revision of this kit's", result.stdout)
        self.assertNotIn("ended early", result.stdout)
        self.assertEqual(self.state(), before)

    def test_the_same_revision_again_changes_nothing_and_says_so(self):
        """Before the reboot of the upgrade and after it; over revision 7, and
        over a revision with a record, whose last line is then this one."""
        for over in ("revision 7", "a revision with a record"):
            for rebooted in (False, True):
                with self.subTest(over=over, rebooted=rebooted):
                    self.setUp()
                    if over == "revision 7":
                        self.install_r7()
                    else:
                        self.install_recorded()
                    running = (R7_STANDIN_ID, NEW_BUILD_ID)[over != "revision 7"]
                    found = self.part("boot").read_bytes()
                    self.assert_upgraded(self.run_script("install.sh"), found)
                    self.running(self.expected[2] if rebooted else running)
                    before = self.state()
                    result = self.run_script("install.sh")
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn("STOP: this revision of the Type-C roles kit is already installed "
                                  f"({self.backup}); nothing was changed", result.stdout)
                    self.assertNotIn("ended early", result.stdout)
                    self.assertEqual(self.state(), before)

    def test_a_kernel_without_a_build_id_is_no_revision_s(self):
        """Also when the record has a line cut short, with no build ID."""
        self.install_recorded()
        with (self.backup / "revisions").open("a") as record:
            record.write(sha(self.new_boot) + "\n")
        (self.root / "sys/kernel/notes").write_bytes(b"")
        before = self.state()
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("STOP: running kernel build ID unknown, neither the hang-log kit's nor a "
                      "revision of this kit's", result.stdout)
        self.assertEqual(self.state(), before)

    def test_this_revision_on_boot_without_its_record_is_installed_again(self):
        """boot written from TWRP, say: the module tree may be another's."""
        for name, install in (("over revision 7", self.install_r7),
                              ("over a revision with a record", self.install_recorded)):
            with self.subTest(name):
                self.setUp()
                install()
                found = self.expected[1]
                self.part("boot").write_bytes(found)
                result = self.run_script("install.sh")
                self.assertNotIn("already installed", result.stdout)
                self.assert_upgraded(result, found)

    def test_the_hang_log_kit_on_boot_is_upgraded_too(self):
        """After the way back from TWRP: boot holds the hang-log kit again and
        its kernel runs; the module tree and the backup are still the kit's."""
        self.install_r7()
        self.part("boot").write_bytes(self.hang_boot)
        self.running(HANG_BUILD_ID)
        self.assert_upgraded(self.run_script("install.sh"), self.hang_boot)
        self.assert_rolled_back()

    def test_an_unknown_image_on_boot_is_refused_with_nothing_written(self):
        for name, install in (
                ("over revision 7", self.install_r7),
                ("over a revision with a record", self.install_recorded),
                ("a revision of no record and none of 1 to 7",
                 lambda: self.install_r7(boot=os.urandom(SIZE)))):
            with self.subTest(name):
                self.setUp()
                install()
                if name != "a revision of no record and none of 1 to 7":
                    self.part("boot").write_bytes(os.urandom(SIZE))
                other = self.part("boot").read_bytes()
                before = self.state()
                result = self.run_script("install.sh")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("STOP: the boot partition holds neither the hang-log kit nor a "
                              f"revision of this kit ({sha(other)})", result.stdout)
                self.assertNotIn("ended early", result.stdout)
                self.assertEqual(self.state(), before)

    def test_an_earlier_revision_goes_back_on_the_same_way(self):
        self.install_recorded()
        self.assert_upgraded(self.run_script("install.sh"), self.new_boot)
        # The kit directory holds the first revision again.
        (self.kit / "boot.img").write_bytes(self.new_boot)
        (self.kit / f"modules-{RELEASE}.tar").write_bytes(modules_tar("new"))
        (self.kit / "BUILD_ID").write_text(NEW_BUILD_ID + "\n")
        self.seal()
        self.expected = ("new", self.new_boot, NEW_BUILD_ID)
        self.assert_upgraded(self.run_script("install.sh"), self.newer_boot)
        self.assert_rolled_back()

    def refusals(self):
        """What an upgrade refuses, by name: the message, and the damage."""
        saved = self.backup / "boot-hang-log.img"
        saved_tree = self.backup / f"modules-{RELEASE}"
        uname = '#!/bin/sh\ncase $1 in -r) echo 7.2.0-rc4 ;; *) echo "#1 SMP PREEMPT test" ;; esac\n'
        return {
            "saved hang-log image changed": (
                f"the hang-log boot image saved in {self.backup} is missing or changed; nothing was "
                "changed", lambda: saved.write_bytes(os.urandom(SIZE))),
            "saved hang-log image missing": (
                "is missing or changed; nothing was changed", saved.unlink),
            "saved hang-log tree moved back by a rollback.sh that stopped": (
                f"the hang-log module tree saved in {self.backup} is missing; nothing was changed "
                "(if rollback.sh stopped, run it again)", lambda: shutil.rmtree(saved_tree)),
            "saved hang-log tree without one of its modules": (
                f"the hang-log module tree saved in {self.backup} does not have the hang-log kit's "
                "list of modules", (saved_tree / "updates/b.ko").unlink),
            "the hang-log kit rolled back from under the kit": (
                "the hang-log kit is not installed",
                (self.root / "var/backups/ubuntu-gts9u-hang-log/complete").unlink),
            "another kernel release": (
                f"running kernel release 7.2.0-rc4, expected {RELEASE}",
                lambda: (self.bin / "uname").write_text(uname)),
            "kit changed after it was packed": (
                "the kit does not match its checksums; nothing was changed",
                lambda: (self.kit / "BUILD_ID").write_text("0" * 40 + "\n")),
            "kit whose BUILD_ID is no build ID": (
                "BUILD_ID in the kit is not a kernel build ID",
                lambda: ((self.kit / "BUILD_ID").write_text(NEW_BUILD_ID[:39] + "\n"), self.seal())),
            "a module added by hand to the earlier kit's tree": (
                "the installed module tree does not have the hang-log kit's list of modules",
                lambda: (self.modules / "updates/by-hand.ko").write_text("r7 extra\n")),
            "no module tree, and no upgrade under way": (
                f"no module tree {self.modules}; run rollback.sh",
                lambda: shutil.rmtree(self.modules)),
            "vendor_boot of another build": (
                "vendor_boot does not hold the hang-log kit",
                lambda: self.part("vendor_boot").write_bytes(os.urandom(SIZE))),
            "low battery": (
                "battery at 12 %",
                lambda: (self.root / "sys/class/power_supply/sm5714-battery/capacity").write_text("12\n")),
            "read-only root": (
                "the root filesystem is not writable",
                lambda: (self.root / "mount-options").write_text("ro,relatime\n")),
            "dpkg at work": ("dpkg is busy", (self.root / "dpkg-busy").touch),
            "another tablet": (
                "this is not an SM-X710",
                lambda: (self.root / "proc/device-tree/compatible").write_bytes(b"samsung,gts9u\0")),
        }

    def test_upgrade_refusals_change_nothing(self):
        for name in self.refusals():
            with self.subTest(name):
                self.setUp()
                self.install_r7()
                message, damage = self.refusals()[name]
                damage()
                before = self.state()
                result = self.run_script("install.sh")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(message, result.stdout)
                self.assertNotIn("ended early", result.stdout)
                self.assertEqual(self.state(), before)

    def stop_in_the_module_swap(self):
        """An upgrade from revision 7 that stops between the module swap and
        the flash: depmod fails."""
        self.install_r7()
        (self.root / "depmod-fails").touch()
        result = self.run_script("install.sh")
        (self.root / "depmod-fails").unlink()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("STOP: depmod failed", result.stdout)
        self.assertIn("STOP: the upgrade ended early; do NOT reboot before running install.sh "
                      "again, or rollback.sh", result.stdout)
        self.assertTrue((self.backup / "upgrading").exists())
        self.assertEqual(self.part("boot").read_bytes(), self.r7_boot)
        self.assertEqual(self.tree(), {n: f"new {n}\n" for n in MODULES})

    def test_an_upgrade_that_stopped_is_finished_by_running_it_again(self):
        self.stop_in_the_module_swap()
        self.assert_upgraded(self.run_script("install.sh"), self.r7_boot)
        self.assert_rolled_back()

    def test_rollback_undoes_an_upgrade_that_stopped(self):
        self.stop_in_the_module_swap()
        left = self.root / "usr/lib/modules" / f".typec-roles-old-{RELEASE}"
        left.mkdir()
        (left / "a.ko").write_text("r7 a.ko\n")
        self.assert_rolled_back()

    def test_an_upgrade_that_stopped_in_the_flash_is_finished_by_running_it_again(self):
        """boot half-written is no image the script knows; it is written again
        all the same, and so is one that was written whole, also once its
        kernel has been booted, and by the revision after it."""
        for name in ("half", "whole", "whole and booted", "whole and booted, then the next revision"):
            with self.subTest(name):
                self.setUp()
                self.stop_in_the_module_swap()
                found = self.new_boot
                if name == "half":
                    found = self.new_boot[:SIZE // 2] + self.r7_boot[SIZE // 2:]
                self.part("boot").write_bytes(found)
                if "booted" in name:
                    self.running(NEW_BUILD_ID)
                if "next" in name:
                    self.expected = ("newer", self.newer_kit(), NEWER_BUILD_ID)
                result = self.run_script("install.sh")
                self.assertNotIn("already installed", result.stdout)
                self.assert_upgraded(result, found)

    def stop_between_the_two_moves(self):
        """An upgrade from revision 7 killed between its two moves: the earlier
        kit's tree moved aside, the new one not yet in its place."""
        self.install_r7()
        self.step_commands()
        stage = self.root / "usr/lib/modules" / f".typec-roles-{RELEASE}"
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        number = self.steps().index(("mv", f"{stage} {self.modules}")) + 1
        self.setUp()
        self.install_r7()
        self.step_commands()
        self.assertEqual(self.run_stopped("install.sh", "kill", number).returncode, -9)
        self.assertFalse(self.modules.exists())
        self.assertEqual(self.tree(self.root / "usr/lib/modules" / f".typec-roles-old-{RELEASE}"),
                         {n: f"r7 {n}\n" for n in MODULES})
        self.assertTrue((self.backup / "upgrading").exists())

    def test_an_upgrade_that_stopped_between_the_two_moves_is_finished_by_running_it_again(self):
        self.stop_between_the_two_moves()
        result = self.run_script("install.sh")
        self.assertIn("an upgrade had stopped in the module swap: the earlier module tree is back "
                      f"in {self.modules}", result.stdout)
        self.assertNotIn("STOP", result.stdout)
        self.assert_upgraded(result, self.r7_boot)
        self.assert_rolled_back()

    def test_an_upgrade_that_stopped_between_the_two_moves_is_undone_by_rollback(self):
        self.stop_between_the_two_moves()
        self.assert_rolled_back()

    def test_a_missing_module_tree_with_nothing_to_put_back_is_left_to_rollback(self):
        old = lambda: self.modules.parent / f".typec-roles-old-{RELEASE}"
        cases = {
            # Not this script's doing: it moves the tree only under its mark.
            "set aside, and no upgrade under way": lambda: self.modules.rename(old()),
            "set aside without its modules.dep": lambda: (
                (self.backup / "upgrading").touch(), self.modules.rename(old()),
                (old() / "modules.dep").unlink()),
            "gone": lambda: ((self.backup / "upgrading").touch(), shutil.rmtree(self.modules)),
        }
        for name, damage in cases.items():
            with self.subTest(name):
                self.setUp()
                self.install_r7()
                damage()
                before = self.state()
                result = self.run_script("install.sh")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(f"STOP: no module tree {self.modules}; run rollback.sh", result.stdout)
                self.assertNotIn("ended early", result.stdout)
                self.assertEqual(self.state(), before)
                self.assert_rolled_back()

    def test_an_upgrade_records_what_it_writes_and_syncs_it_with_the_modules_before_the_flash(self):
        self.install_r7()
        self.syncs()
        self.assert_upgraded(self.run_script("install.sh"), self.r7_boot)
        kept = {"saved": "yes", "tree": "new", "complete": "yes", "recorded": sha(self.new_boot)}
        self.assertEqual(self.syncs(), [
            {**kept, "boot": sha(self.r7_boot), "upgrading": "yes"},
            {**kept, "boot": sha(self.new_boot), "upgrading": "no"},
        ])

    def test_an_upgrade_whose_sync_fails_before_the_flash_is_finished_by_running_it_again(self):
        self.install_r7()
        result = self.run_with_failing_sync("install.sh", 1)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("STOP: sync failed", result.stdout)
        self.assertIn("STOP: the upgrade ended early; do NOT reboot before running install.sh "
                      "again, or rollback.sh", result.stdout)
        self.assertNotIn("writing boot", result.stdout)
        self.assertEqual(self.part("boot").read_bytes(), self.r7_boot)
        self.assertTrue((self.backup / "upgrading").exists())
        self.assert_upgraded(self.run_script("install.sh"), self.r7_boot)
        self.assert_rolled_back()

    def test_an_upgrade_s_last_sync_failing_is_reported_with_the_kit_in_place(self):
        self.install_r7()
        result = self.run_with_failing_sync("install.sh", 2)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('STOP: the kit is in place, but sync failed: run "sudo sync" until it '
                      "succeeds, then reboot", result.stdout)
        self.assertNotIn("ended early", result.stdout)
        self.assertNotIn("reboot now", result.stdout)
        self.assert_installed()
        self.assert_rolled_back()

    def test_verify_checks_the_new_revision_after_an_upgrade(self):
        """Every revision so far has the same module key: before the reboot
        the earlier revision's kernel passes every check but the build ID."""
        self.install_recorded()
        self.assert_upgraded(self.run_script("install.sh"), self.new_boot)
        self.boot_kit_kernel(NEW_BUILD_ID)
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"FAIL build ID {NEW_BUILD_ID}, the kit's is {NEWER_BUILD_ID}", result.stdout)
        self.assertEqual(result.stdout.count("FAIL "), 1, result.stdout)
        self.boot_kit_kernel(NEWER_BUILD_ID)
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"ok   running the kit's kernel (build ID {NEWER_BUILD_ID[:12]}, revision 11)",
                      result.stdout)
        self.assertIn("all checks passed", result.stdout)


class StoppedRunTest(UpgradeHelpers, FakeTablet):
    """A run stopped at each of its steps (STEP_COMMANDS): killed before it,
    as a power cut or a kill stops it, or with that step's command failing.
    From each state an upgrade leaves, install.sh run again finishes it and
    rollback.sh undoes it; a first install that stopped is undone by
    rollback.sh, as before; a rollback.sh that stopped is finished by running
    it again."""

    def snapshot(self, name):
        copy = self.tmp / name
        if copy.exists():
            shutil.rmtree(copy)
        shutil.copytree(self.root, copy)
        return copy

    def restore(self, copy):
        shutil.rmtree(self.root)
        shutil.copytree(copy, self.root)

    def full_run(self, name):
        """The steps of NAME run on this tablet without a stop, and the
        tablet as it was before, to go back to."""
        prepared = self.snapshot("prepared")
        result = self.run_script(name)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        steps = self.steps()
        self.assertTrue(steps)
        self.finished = self.state()
        self.restore(prepared)
        return prepared, steps

    def new_state(self, seen):
        """Is the tablet in a state not met before in SEEN (which it joins)?
        Two stops that leave the same disk lead to the same: the scripts
        read nothing else that a stop changes."""
        state = tuple(sorted(self.state().items()))
        if state in seen:
            return False
        seen.add(state)
        return True

    def stop(self, name, how, number, steps):
        """Run NAME stopped at step NUMBER; whether it changed anything."""
        before = self.state()
        result = self.run_stopped(name, how, number)
        out = result.stdout + result.stderr
        if how == "kill":
            self.assertEqual(result.returncode, -9, out)
        elif result.returncode == 0:
            # A failure the script lets pass (blockdev's flush, or rollback.sh
            # reading boot before it writes it anyway) changes nothing.
            self.assertEqual(self.state(), self.finished, out)
        changed = self.state() != before
        if result.returncode not in (0, -9) and changed:
            self.assertRegex(result.stdout, r"STOP: (install|the upgrade|rollback) ended early|"
                             r"STOP: the (kit|hang-log kit) is (in|back in) place, but sync failed")
        return changed

    def test_an_upgrade_stopped_at_any_step_is_finished_by_install_and_undone_by_rollback(self):
        """From revision 7, with a saved boot set made since its install: the
        upgrade copies the hang-log kit's image for the set and updates it."""
        self.install_r7()
        boot_set = self.saved_set(self.r7_boot)
        self.step_commands()
        prepared, steps = self.full_run("install.sh")
        self.assertEqual([step for step, _ in steps if step in ("mv", "touch", "depmod", "cp")],
                         ["touch", "mv", "mv", "depmod", "cp", "mv", "cp", "mv", "touch"])
        # The untouched tablet too: rollback.sh on revision 7 as it is.
        seen = set()
        for number, step in enumerate(steps, 1):
            for how in ("kill", "fail"):
                with self.subTest(step=number, command=" ".join(step), how=how):
                    self.restore(prepared)
                    self.stop("install.sh", how, number, steps)
                    if not self.new_state(seen):
                        continue
                    stopped = self.snapshot("stopped")
                    copy_made = (self.backup / "saved-set-boot.img").exists()
                    self.assert_rolled_back()
                    # rollback.sh restores the set from the copy; without one
                    # it leaves the set as revision 7 left it, as before.
                    self.assertEqual((boot_set / "boot.img").read_bytes(),
                                     self.hang_boot if copy_made else self.r7_boot)
                    self.restore(stopped)
                    found = self.part("boot").read_bytes()
                    result = self.run_script("install.sh")
                    if "already installed" in result.stdout:
                        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                        self.assert_installed()
                    else:
                        self.assert_upgraded(result, found)
                    self.assertEqual((boot_set / "boot.img").read_bytes(), self.new_boot)
                    self.assertEqual((self.backup / "saved-set-boot.img").read_bytes(),
                                     self.hang_boot)

    def test_an_upgrade_stopped_twice_is_finished_by_a_third_run(self):
        """Killed after the swap, with the earlier tree still set aside, then
        killed again at each step of the run that was to finish it."""
        self.install_r7()
        self.step_commands()
        old = self.modules.parent / f".typec-roles-old-{RELEASE}"
        prepared, steps = self.full_run("install.sh")
        number = steps.index(("rm", f"-rf -- {old}")) + 1
        self.assertTrue(self.stop("install.sh", "kill", number, steps))
        self.assertEqual(self.tree(old), {n: f"r7 {n}\n" for n in MODULES})
        self.assertEqual(self.tree(), {n: f"new {n}\n" for n in MODULES})
        once, again = self.full_run("install.sh")
        seen = set()
        for number, step in enumerate(again, 1):
            with self.subTest(step=number, command=" ".join(step)):
                self.restore(once)
                self.stop("install.sh", "kill", number, again)
                if not self.new_state(seen):
                    continue
                twice = self.snapshot("twice")
                found = self.part("boot").read_bytes()
                result = self.run_script("install.sh")
                if "already installed" in result.stdout:
                    self.assert_installed()
                else:
                    self.assert_upgraded(result, found)
                self.restore(twice)
                self.assert_rolled_back()

    def test_a_first_install_stopped_at_any_step_is_undone_by_rollback(self):
        """With a saved boot set from before the install."""
        found = os.urandom(SIZE)
        boot_set = self.saved_set(found)
        self.step_commands()
        untouched = self.state()
        prepared, steps = self.full_run("install.sh")
        seen = set()
        for number, step in enumerate(steps, 1):
            for how in ("kill", "fail"):
                with self.subTest(step=number, command=" ".join(step), how=how):
                    self.restore(prepared)
                    self.stop("install.sh", how, number, steps)
                    if not self.backup.exists():
                        self.assertEqual(self.state(), untouched)
                        continue
                    if not self.new_state(seen):
                        continue
                    if not (self.backup / "complete").exists():
                        # A second run does not finish it: rollback.sh first.
                        before = self.state()
                        refused = self.run_script("install.sh")
                        self.assertIn("exists from an install that did not finish; run rollback.sh "
                                      "first", refused.stdout)
                        self.assertEqual(self.state(), before)
                    result = self.run_script("rollback.sh")
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    if "install.sh had stopped before it changed anything" in result.stdout:
                        retired = "unfinished"
                    else:
                        self.assertIn("back on the hang-log kit; reboot now", result.stdout)
                        retired = "rolled-back"
                    self.assert_on_the_hang_log_kit()
                    self.assertTrue(list((self.root / "var/backups").glob(
                        f"ubuntu-gts9u-typec-roles.{retired}-*")))
                    self.assertEqual((boot_set / "boot.img").read_bytes(), found)

    def test_a_rollback_stopped_at_any_step_after_an_upgrade_is_finished_by_running_it_again(self):
        self.install_r7()
        boot_set = self.saved_set(self.r7_boot)
        self.assert_upgraded(self.run_script("install.sh"), self.r7_boot)
        self.step_commands()
        prepared, steps = self.full_run("rollback.sh")
        seen = set()
        for number, step in enumerate(steps, 1):
            for how in ("kill", "fail"):
                with self.subTest(step=number, command=" ".join(step), how=how):
                    self.restore(prepared)
                    self.stop("rollback.sh", how, number, steps)
                    if not self.new_state(seen):
                        continue
                    if self.backup.exists():
                        self.assert_rolled_back()
                    else:
                        # It had retired the backup: done but for its last sync.
                        again = self.run_script("rollback.sh")
                        self.assertIn("install.sh never ran (or was rolled back already)",
                                      again.stdout)
                        self.assert_on_the_hang_log_kit()
                    self.assertEqual((boot_set / "boot.img").read_bytes(), self.hang_boot)
class VerifyTest(FakeTablet):
    def setUp(self):
        super().setUp()
        self.port = self.root / "sys/class/typec/port0"
        self.partner = self.root / "sys/class/typec/port0-partner"
        self.log = self.root / "sys/kernel/debug/usb/tcpm-4-0033/log"
        self.log.write_text("")
        self.spool = self.root / "tcpm-spool"
        self.spool.mkdir()
        self.pieces = itertools.count(1)
        (self.bin / "cat").write_text("#!/bin/sh\n" + TCPM_LOG_STUB)
        (self.bin / "cat").chmod(0o755)
        self.roles("device", "sink")
        usb = self.root / "sys/bus/usb/devices/1-1"
        (usb / "idVendor").write_text("291a\n")
        (usb / "idProduct").write_text("8383\n")

    def log_tcpm(self, text):
        """Log TEXT on the port: the next read of the TCPM log gets it."""
        name = "%06d" % next(self.pieces)
        (self.spool / ("." + name)).write_text(text)
        os.replace(self.spool / ("." + name), self.spool / name)

    def unread(self):
        """What the next read of the TCPM log gets, as it is logged."""
        text = []
        for piece in sorted(self.spool.iterdir()):
            try:
                text.append("" if piece.name.startswith(".") else piece.read_text())
            except FileNotFoundError:  # read in the meantime
                pass
        return "".join(text)

    def cat(self, path):
        """Read PATH with the cat the scripts get."""
        env = {"PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_ROOT": str(self.root)}
        return subprocess.run(["cat", str(path)], env=env, capture_output=True, text=True,
                              check=True).stdout

    def test_the_fake_tcpm_log_is_emptied_by_a_read_and_overflows_as_the_kernel_s(self):
        self.log_tcpm(PASS_LOG)
        self.log_tcpm(SWAP_EARLY_LOG)
        self.assertEqual(self.cat(self.log), PASS_LOG + SWAP_EARLY_LOG)
        self.assertEqual(self.cat(self.log), "")
        full = dock_messages(500_000_000, 1023)
        self.log_tcpm(full)
        self.assertEqual(self.cat(self.log), full)
        self.log_tcpm(full + dock_messages(500_001_023, 2))
        self.assertEqual(self.cat(self.log), "".join(full.splitlines(True)[:1022])
                         + "[  500.001024] overflow\n")
        # Every other file is read as it is.
        self.assertEqual(self.cat(self.kit / "BUILD_ID"), NEW_BUILD_ID + "\n")

    def roles(self, data, power):
        self.port.joinpath("data_role").write_text(
            "[host] device\n" if data == "host" else "host [device]\n")
        self.port.joinpath("power_role").write_text(
            "[source] sink\n" if power == "source" else "source [sink]\n")

    def plug(self, scenarios):
        """Play the user: plug in at each trial's cue, unplug once it is recorded."""
        kmsg = self.root / "dev/kmsg"
        logs = self.kit / "logs"

        def play():
            for n, (log, data, power) in enumerate(scenarios, 1):
                deadline = time.monotonic() + 60
                while not (kmsg.exists() and kmsg.read_text().endswith(f"trial {n}\n")):
                    if time.monotonic() > deadline:
                        return
                    time.sleep(0.005)
                self.log_tcpm(log)
                self.roles(data, power)
                self.partner.mkdir()
                while not list(logs.glob(f"*/trial-{n}.state")):
                    if time.monotonic() > deadline:
                        return
                    time.sleep(0.005)
                self.partner.rmdir()

        thread = threading.Thread(target=play)
        thread.start()
        self.addCleanup(thread.join)

    def results(self):
        (table,) = (self.kit / "logs").glob("*/results.tsv")
        rows = [line.split("\t") for line in table.read_text().splitlines()]
        return [dict(zip(rows[0], row)) for row in rows[1:]]

    def test_dock_verdict_is_the_roles_and_a_single_attach(self):
        # The last one overflowed between two reads: what it lost may be an
        # attach or a mismatch.
        self.plug([(PASS_LOG, "host", "sink"), (FLAP_LOG, "device", "sink"),
                   (TRY_SNK_LOG, "host", "sink"), (PASS_LOG, "host", "source"),
                   (PASS_LOG, "device", "sink"), (WAIT_LOG, "host", "sink"),
                   (PASS_LOG + "[  100.900001] overflow\n", "host", "sink")])
        result = self.run_script("verify.sh", "dock", "7")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        rows = self.results()
        self.assertEqual([r["verdict"] for r in rows],
                         ["PASS", "FAIL", "PASS", "FAIL", "FAIL", "PASS", "FAIL"])
        self.assertEqual(rows[6]["overflow"], "1")
        # A request that was repeated after Wait is still one attach, and the
        # verdict is the roles.
        self.assertEqual((rows[5]["attaches"], rows[5]["dfp_requests"], rows[5]["dr_swaps"]),
                         ("1", "2", "1"))
        self.assertEqual((rows[0]["attaches"], rows[0]["dfp_requests"], rows[0]["dr_swaps"]),
                         ("1", "1", "1"))
        self.assertEqual((rows[1]["attaches"], rows[1]["role_mismatches"],
                          rows[1]["error_recoveries"]), ("2", "1", "1"))
        # The pending timer line is not a Try.SNK that gave up, and VBUS
        # arriving in the debounce is not an Rp that went away.
        self.assertEqual((rows[2]["try_snk_kept"], rows[2]["try_snk_lost"],
                          rows[2]["try_snk_rp_lost"]), ("1", "0", "0"))
        self.assertEqual(list(rows[0])[-1], "try_snk_rp_lost")
        self.assertEqual({len(r) for r in rows}, {20})
        self.assertEqual(rows[0]["usb_devices"], "1")
        self.assertIn("3 of 7 plug-ins ended as USB host and power sink", result.stdout)
        self.assertIn("this is a baseline run", result.stdout)
        (trial,) = (self.kit / "logs").glob("*/trial-1.tcpm.log")
        self.assertEqual(trial.read_text(), PASS_LOG)
        (state,) = (self.kit / "logs").glob("*/trial-1.state")
        self.assertIn("1-1 291a:8383", state.read_text())

    def test_the_recorder_does_not_run_next_to_the_stopgap(self):
        """The daemon writes the data role itself; a trial next to it would
        record the daemon, not the kernel."""
        (self.bin / "systemctl").write_text(
            "#!/bin/sh\ncase $* in 'is-active --quiet typec-sink-host') exit 0 ;; esac\nexit 3\n")
        self.boot_kit()
        self.log_tcpm(PASS_LOG)
        switch = self.root / "sys/module/sm5714_usbpd/parameters/sink_host_swap"
        for mode in ("dock", "record", "now", "swap"):
            with self.subTest(mode):
                result = self.run_script("verify.sh", mode)
                self.assertEqual(result.returncode, 1)
                self.assertIn("sudo systemctl stop typec-sink-host", result.stderr)
                self.assertFalse((self.kit / "logs").exists())
                self.assertEqual(switch.read_text(), "Y\n")
        # Not even read.
        self.assertEqual(self.unread(), PASS_LOG)

    def test_a_trial_s_log_starts_empty(self):
        """A second run in the same second has the first one's directory."""
        (self.bin / "date").write_text("#!/bin/sh\necho 20261121-174500\n")
        (self.bin / "date").chmod(0o755)
        earlier = self.kit / "logs/20261121-174500-dock/trial-1.tcpm.log"
        earlier.parent.mkdir(parents=True)
        earlier.write_text(FLAP_LOG)
        self.plug([(PASS_LOG, "host", "sink")])
        result = self.run_script("verify.sh", "dock", "1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([r["verdict"] for r in self.results()], ["PASS"])
        self.assertEqual(earlier.read_text(), PASS_LOG)
        # now reads the log once, into a log of its own as well.
        earlier = self.kit / "logs/20261121-174500-now/trial-1.tcpm.log"
        earlier.parent.mkdir(parents=True)
        earlier.write_text(FLAP_LOG)
        self.log_tcpm(PASS_LOG)
        result = self.run_script("verify.sh", "now")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(earlier.read_text(), PASS_LOG)

    def test_record_gives_no_verdict_and_passes(self):
        self.plug([(TRY_SNK_LOST_LOG, "host", "source")])
        result = self.run_script("verify.sh", "record")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (row,) = self.results()
        self.assertEqual((row["verdict"], row["data_8s"], row["power_8s"], row["try_snk_lost"]),
                         ("-", "host", "source", "1"))

    def test_now_saves_the_log_without_waiting_for_a_plug(self):
        self.log_tcpm(PASS_LOG)
        self.roles("host", "sink")
        self.partner.mkdir()
        result = self.run_script("verify.sh", "now")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (row,) = self.results()
        self.assertEqual((row["data_8s"], row["power_8s"], row["partner"], row["dr_swaps"]),
                         ("host", "sink", "yes", "1"))

    def test_summary_counts_saved_logs(self):
        flap = self.tmp / "flap.log"
        flap.write_text(FLAP_LOG + "[  202.000000] overflow\n")
        result = self.run_script("verify.sh", "summary", str(flap))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2 attaches, host role requested 0, swapped 0, role mismatches 1", result.stdout)
        self.assertIn("LOG OVERFLOWED", result.stdout)

    def test_try_snk_counters_tell_its_endings_apart(self):
        """Kept, given up without an Rp, given up when the Rp went away again
        (the first revision of the wait), and kept after the Rp went away and
        came back."""
        quiet = "1 attach, host role requested 0, swapped 0, role mismatches 0"
        expected = {
            "kept": (TRY_SNK_LOG, "1 attach, host role requested 1, swapped 1, role mismatches 0, "
                                  "Try.SNK kept 1 lost 0"),
            "lost": (TRY_SNK_LOST_LOG, quiet + ", Try.SNK kept 0 lost 1"),
            "lost-in-debounce": (TRY_SNK_LOST_IN_DEBOUNCE_LOG, quiet + ", Try.SNK kept 0 lost 1"),
            "rp-lost": (TRY_SNK_RP_LOST_LOG, quiet + ", Try.SNK kept 1 lost 0 (Rp came and went 1)"),
            "no-try-snk": (PASS_LOG, "1 attach, host role requested 1, swapped 1, role mismatches 0"),
        }
        paths = []
        for name, (log, _) in expected.items():
            paths.append(self.tmp / f"{name}.log")
            paths[-1].write_text(log)
        result = self.run_script("verify.sh", "summary", *map(str, paths))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(),
                         [f"{path}: {sentence}"
                          for path, (_, sentence) in zip(paths, expected.values())])

    def test_a_bad_count_or_mode_is_refused(self):
        self.assertEqual(self.run_script("verify.sh", "dock", "ten").returncode, 2)
        self.assertEqual(self.run_script("verify.sh", "swap", "0").returncode, 2)
        self.assertEqual(self.run_script("verify.sh", "docks").returncode, 2)
        self.assertFalse((self.kit / "logs").exists())

    def data_role(self, role):
        """Put a new data_role in place in one step: one that takes a write
        and reads as ROLE, or with None one that takes none, as the port
        does without a partner (for root as well: the link leads nowhere)."""
        new = self.port / "data_role.new"
        if role is None:
            new.symlink_to("nowhere/data_role")
        else:
            new.write_text("[host] device\n" if role == "host" else "host [device]\n")
        os.replace(new, self.port / "data_role")

    def plug_for_swap(self, scenarios):
        """Play the user and the port for the swap mode: at each trial's cue
        the port logs LOG and data_role takes a write and reads as ROLE
        (None: it refuses every write); unplugged once the trial is recorded.
        LOG can be a tuple of pieces: each after the first is logged once
        the ones before it have been read.  Returns the list that gets what
        the switch said during each trial."""
        kmsg = self.root / "dev/kmsg"
        logs = self.kit / "logs"
        switch = self.root / "sys/module/sm5714_usbpd/parameters/sink_host_swap"
        seen = []
        if getattr(self, "player", None):
            self.player.join()
        kmsg.unlink(missing_ok=True)
        self.data_role(None)
        # The script's waits before it reads the roles, fourteen steps of a
        # second, are long enough here for all the writer's writes; the
        # writer's own are short.  Each wait is noted in the events file.
        (self.bin / "sleep").write_text("#!/bin/sh\necho \"sleep $1\" >> \"$FAKE_ROOT/events\"\n"
                                        "case $1 in\n1) exec /usr/bin/sleep 0.05 ;;\n"
                                        "0.005|0.3) exec /usr/bin/sleep 0.001 ;;\nesac\n"
                                        "exec /usr/bin/sleep 0.01\n")

        def play():
            for n, (log, role) in enumerate(scenarios, 1):
                deadline = time.monotonic() + 60
                while not (kmsg.exists() and kmsg.read_text().endswith(f"trial {n}\n")):
                    if time.monotonic() > deadline:
                        return
                    time.sleep(0.005)
                seen.append(switch.read_text() if switch.exists() else None)
                pieces = (log,) if isinstance(log, str) else log
                self.log_tcpm(pieces[0])
                self.data_role(role)
                self.partner.mkdir()
                for piece in pieces[1:]:
                    while self.unread():
                        if time.monotonic() > deadline:
                            return
                        time.sleep(0.005)
                    self.log_tcpm(piece)
                while not list(logs.glob(f"*/trial-{n}.state")):
                    if time.monotonic() > deadline:
                        return
                    time.sleep(0.005)
                self.data_role(None)
                self.partner.rmdir()

        self.player = threading.Thread(target=play)
        self.player.start()
        self.addCleanup(self.player.join)
        return seen

    def swap_rows(self):
        (table,) = (self.kit / "logs").glob("*/swap.tsv")
        rows = [line.split("\t") for line in table.read_text().splitlines()]
        self.assertEqual({len(row) for row in rows}, {7})
        return [dict(zip(rows[0], row)) for row in rows[1:]]

    def test_swap_judges_what_the_writes_to_data_role_did(self):
        self.boot_kit()
        switch = self.root / "sys/module/sm5714_usbpd/parameters/sink_host_swap"
        seen = self.plug_for_swap([
            (SWAP_LOG, "host"),           # refused in the discovery, then done
            (SWAP_CUT_LOG, "device"),     # sent into the discovery
            (PASS_LOG, "host"),           # the kernel asked itself
            (SWAP_UNDONE_LOG, "device"),  # done, and a reset after it
            (FLAP_LOG, None),             # no write was taken
            (SWAP_WAIT_LOG, "device"),    # success reported, nothing swapped
            (SWAP_AGAIN_LOG, "host"),     # told that nothing changed, then done
            (SWAP_CUT_AGAIN_LOG, "host"),  # sent into the discovery, told so, then done
            (SWAP_LOG + "[  300.900001] overflow\n", "host"),  # done, but the log lost lines
            (TRY_SNK_LOST_LOG, "host"),   # the host from the attach: nothing to swap
        ])
        result = self.run_script("verify.sh", "swap", "10")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([r["verdict"] for r in self.results()],
                         ["PASS", "FAIL", "FAIL", "PASS", "-", "FAIL", "PASS", "FAIL", "-", "-"])
        self.assertEqual({len(r) for r in self.results()}, {20})
        rows = self.swap_rows()
        self.assertEqual([(r["asked"], r["refused_in_vdm"], r["sent"], r["role_unchanged"],
                           r["cut_short"]) for r in rows],
                         [("3", "2", "1", "0", "0"), ("1", "0", "1", "0", "1"),
                          ("1", "0", "1", "0", "0"), ("3", "2", "1", "0", "0"),
                          ("0", "0", "0", "0", "0"), ("1", "0", "1", "0", "0"),
                          ("2", "0", "2", "1", "0"), ("2", "0", "2", "1", "1"),
                          ("3", "2", "1", "0", "0"), ("0", "0", "0", "0", "0")])
        accepted = "write 1 was accepted, the data role right after it: "
        self.assertEqual([r["write"] for r in rows],
                         [accepted + "host", accepted + "device", accepted + "host",
                          accepted + "device", "none of 25 writes was accepted",
                          accepted + "device", accepted + "host", accepted + "host",
                          accepted + "host", accepted + "host"])
        out = result.stdout
        self.assertIn("trial 1: PASS, data host/host, power sink/sink (at 8 s/14 s)", out)
        self.assertIn("  data_role: 3 swaps asked for, 2 refused during a VDM exchange, 1 sent, "
                      "0 ended with the role unchanged, 0 cut short by a Soft Reset; "
                      + accepted + "host\n", out)
        self.assertIn("  data_role: 1 swaps asked for, 0 refused during a VDM exchange, 1 sent, "
                      "0 ended with the role unchanged, 1 cut short by a Soft Reset; "
                      + accepted + "device\n", out)
        self.assertIn("  data_role: 2 swaps asked for, 0 refused during a VDM exchange, 2 sent, "
                      "1 ended with the role unchanged, 0 cut short by a Soft Reset; ", out)
        # A swap that was cut short fails the plug-in whatever the write was
        # told and whatever the role is in the end.  A log that lost lines
        # between two reads gives no verdict: what it lost may be just that.
        self.assertIn("trial 8: FAIL, data host/host, power sink/sink (at 8 s/14 s)", out)
        self.assertIn("  data_role: 2 swaps asked for, 0 refused during a VDM exchange, 2 sent, "
                      "1 ended with the role unchanged, 1 cut short by a Soft Reset; "
                      + accepted + "host\n", out)
        self.assertIn("trial 9: data host/host, power sink/sink (at 8 s/14 s)", out)
        self.assertEqual(self.results()[8]["overflow"], "1")
        self.assertIn("trial 5: data ?/?, power sink/sink", out)
        self.assertIn("trial 10: data host/host, power sink/sink", out)
        # Trial 3 alone; trials 2 and 6 (in trial 4 the swap was done before
        # the reset undid it, and in trial 10 the tablet is the host with no
        # swap in the log: a write that asks for the role it has is done);
        # trial 9; trial 5; trial 10.
        self.assertEqual(out.count("the kernel asked for the host role itself: "
                                   "the switch was not off for this plug-in"), 1)
        self.assertEqual(out.count("a write reported a swap that did not happen"), 2)
        self.assertEqual(self.trial_lines(out, 9)[-1],
                         "  the TCPM log overflowed between two reads, so lines were lost: this "
                         "plug-in cannot be judged")
        self.assertEqual(out.count("cannot be judged"), 1)
        self.assertEqual(self.trial_lines(out, 5)[-1],
                         "  every write was refused before TCPM started a swap (the log has no AMS "
                         "DATA_ROLE_SWAP): the port was not ready at any of them, or the partner "
                         "has no PD")
        self.assertEqual(out.count("every write was refused"), 1)
        self.assertEqual(self.trial_lines(out, 10)[-1],
                         "  no swap was asked for: not a PD source with the tablet as its USB device")
        self.assertEqual(out.count("no swap was asked for"), 1)
        self.assertIn("3 of 10 plug-ins: a swap asked for through data_role, none cut short by a "
                      "Soft Reset, none reported done with the role unchanged", out)
        self.assertNotIn("the refusal itself was not seen", out)
        self.assertNotIn("no plug-in could be judged", out)
        self.assertNotIn("left pending", out)
        self.assertNotIn("partner swapped first", out)
        # The kernel's own request is off for every trial and back afterwards.
        self.assertEqual(seen, ["N\n"] * 10)
        self.assertEqual(switch.read_text(), "Y\n")
        (kernel,) = (self.kit / "logs").glob("*/kernel")
        self.assertIn("sink_host_swap: N\n", kernel.read_text())
        self.assertIn("sink_host_swap before this run and after it: Y\n", kernel.read_text())

    def trial_lines(self, out, n):
        """The lines the swap mode printed for trial N."""
        lines = out.splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith(f"trial {n}:"))
        end = start + 1
        while end < len(lines) and lines[end].startswith("  "):
            end += 1
        return lines[start:end]

    def test_swap_counts_what_the_third_fix_logs(self):
        """A write that the partner's own DR_Swap answered, and one whose
        swap went out from a ready state: both are done, neither was sent
        from AMS_START, and the third fix's lines are counted, the drops of
        a queued PR_Swap or VCONN_Swap with those of a DR_Swap."""
        self.boot_kit()
        self.plug_for_swap([(SWAP_PARTNER_FIRST_LOG, "host"),
                            (SWAP_PARTNER_FIRST_AS_SOURCE_LOG, "host"),
                            (SWAP_SENT_FROM_READY_LOG, "host"),
                            (SWAP_PARTNER_FIRST_OTHER_KINDS_LOG, "host")])
        result = self.run_script("verify.sh", "swap", "4")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([r["verdict"] for r in self.results()], ["PASS"] * 3 + ["-"])
        # No swap of the tablet's took the role in the first two.
        self.assertEqual([r["dr_swaps"] for r in self.results()], ["0", "0", "1", "0"])
        self.assertEqual([(r["asked"], r["refused_in_vdm"], r["sent"], r["role_unchanged"],
                           r["cut_short"]) for r in self.swap_rows()],
                         [("1", "0", "0", "0", "0"), ("1", "0", "0", "0", "0"),
                          ("1", "0", "1", "0", "0"), ("0", "0", "0", "0", "0")])
        out = result.stdout
        pending = "  still queued when the port was ready again, left pending: 1"
        dropped = "  dropped because the partner swapped first: 1"
        accepted = "write 1 was accepted, the data role right after it: host"
        self.assertEqual(self.trial_lines(out, 1)[1:],
                         ["  data_role: 1 swaps asked for, 0 refused during a VDM exchange, 0 sent, "
                          "0 ended with the role unchanged, 0 cut short by a Soft Reset; " + accepted,
                          pending])
        self.assertEqual(self.trial_lines(out, 2)[1:],
                         ["  data_role: 1 swaps asked for, 0 refused during a VDM exchange, 0 sent, "
                          "0 ended with the role unchanged, 0 cut short by a Soft Reset; " + accepted,
                          dropped])
        self.assertEqual(self.trial_lines(out, 3)[1:],
                         ["  data_role: 1 swaps asked for, 0 refused during a VDM exchange, 1 sent, "
                          "0 ended with the role unchanged, 0 cut short by a Soft Reset; " + accepted,
                          pending])
        # The drops of the other two kinds count the same.
        self.assertEqual(self.trial_lines(out, 4)[2], "  dropped because the partner swapped first: 2")
        self.assertEqual(out.count("dropped because the partner swapped first"), 2)
        self.assertIn("3 of 4 plug-ins: a swap asked for through data_role", out)

    def test_the_log_is_read_every_second_during_a_plug_in(self):
        """A dock that re-attaches in a loop fills the log in seconds.  What
        was logged before a read in the middle of the plug-in is judged with
        what came after it, and together they are more lines than the log
        holds: read once at the end it would have kept 1022 and an
        overflow."""
        self.boot_kit()
        lines = SWAP_LOG.splitlines(True)
        first = "".join(lines[:7]) + dock_messages(300_210_100, 700)
        second = dock_messages(300_212_000, 700) + "".join(lines[7:])
        self.plug_for_swap([((first, second), "host")])
        result = self.run_script("verify.sh", "swap")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # Read once to empty it before the plug-in, after each second of the
        # fourteen, and once at the end.
        events = (self.root / "events").read_text().splitlines()
        self.assertEqual([event for event in events if event in ("read", "sleep 1")],
                         ["read"] + ["sleep 1", "read"] * 14 + ["read"])
        (trial,) = (self.kit / "logs").glob("*/trial-1.tcpm.log")
        self.assertEqual(trial.read_text(), first + second)
        self.assertGreater(len((first + second).splitlines()), 1023)
        self.assertEqual([r["verdict"] for r in self.results()], ["PASS"])
        self.assertEqual(self.results()[0]["overflow"], "0")
        self.assertEqual([(r["asked"], r["refused_in_vdm"], r["sent"]) for r in self.swap_rows()],
                         [("3", "2", "1")])

    def test_a_log_that_overflows_between_two_reads_gives_no_verdict_and_fails_the_run(self):
        """The second plug-in's swap is among the lines the log lost, so its
        write, which reported the device role, cannot be called a lie
        either."""
        self.boot_kit()
        storm = dock_messages(300_000_000, 1100) + SWAP_LOG
        self.plug_for_swap([(SWAP_EARLY_LOG, "host"), (storm, "device")])
        result = self.run_script("verify.sh", "swap", "2")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([r["verdict"] for r in self.results()], ["PASS", "-"])
        self.assertEqual(self.results()[1]["overflow"], "1")
        (trial,) = (self.kit / "logs").glob("*/trial-2.tcpm.log")
        kept = trial.read_text().splitlines()
        self.assertEqual(len(kept), 1023)
        self.assertEqual(kept[-1], "[  300.221002] overflow")
        self.assertNotIn("a write reported a swap that did not happen", result.stdout)
        self.assertEqual(self.trial_lines(result.stdout, 2)[-1],
                         "  the TCPM log overflowed between two reads, so lines were lost: this "
                         "plug-in cannot be judged")
        self.assertIn("1 of 2 plug-ins: a swap asked for through data_role", result.stdout)
        self.assertNotIn("FAIL", result.stdout)

    def test_a_swap_run_with_no_verdict_fails(self):
        """Nothing was shown: every write refused in one plug-in, the role
        already the host in the other."""
        self.boot_kit()
        self.plug_for_swap([(FLAP_LOG, None), (TRY_SNK_LOST_LOG, "host")])
        result = self.run_script("verify.sh", "swap", "2")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([r["verdict"] for r in self.results()], ["-", "-"])
        self.assertIn("every write was refused before TCPM started a swap",
                      self.trial_lines(result.stdout, 1)[-1])
        self.assertIn("no swap was asked for", self.trial_lines(result.stdout, 2)[-1])
        self.assertTrue(result.stdout.endswith(
            "0 of 2 plug-ins: a swap asked for through data_role, none cut short by a Soft Reset, "
            "none reported done with the role unchanged\n"
            "note no write fell into a VDM exchange, so the refusal itself was not seen: plug in "
            "again\n"
            "FAIL no plug-in could be judged (the lines under each plug-in say why)\n"),
            result.stdout)

    def test_swap_says_when_no_write_met_a_vdm_exchange(self):
        """One clean swap is a pass, but the refusal was not put to the test.
        A switch that was off stays off, and the kernel before the kit, which
        has no switch, is recorded as well."""
        switch = self.root / "sys/module/sm5714_usbpd/parameters/sink_host_swap"
        for was in ("N\n", None):
            with self.subTest(was):
                shutil.rmtree(self.kit / "logs", ignore_errors=True)
                switch.unlink(missing_ok=True)
                if was:
                    switch.write_text(was)
                seen = self.plug_for_swap([(SWAP_EARLY_LOG, "host")])
                result = self.run_script("verify.sh", "swap")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual([r["verdict"] for r in self.results()], ["PASS"])
                self.assertEqual([(r["asked"], r["refused_in_vdm"], r["sent"])
                                  for r in self.swap_rows()], [("1", "0", "1")])
                self.assertIn("1 of 1 plug-ins: a swap asked for through data_role", result.stdout)
                self.assertIn("note no write fell into a VDM exchange, so the refusal itself "
                              "was not seen: plug in again", result.stdout)
                self.assertNotIn("FAIL", result.stdout)
                self.assertIn("this is a baseline run", result.stdout)
                self.assertEqual(seen, [was])
                self.assertEqual(switch.read_text() if switch.exists() else None, was)
                (kernel,) = (self.kit / "logs").glob("*/kernel")
                self.assertIn("sink_host_swap: N\n" if was else "sink_host_swap: absent\n",
                              kernel.read_text())
                self.assertEqual("before this run" in kernel.read_text(), bool(was))

    @staticmethod
    def scripts_running(path):
        """The processes that run the script at PATH: the shell and any
        subshell of it, such as the writer."""
        found = set()
        for proc in pathlib.Path("/proc").iterdir():
            try:
                if proc.name.isdigit() and str(path).encode() in (
                        proc / "cmdline").read_bytes().split(b"\0"):
                    found.add(int(proc.name))
            except OSError:  # gone in the meantime
                pass
        return found

    def test_a_swap_run_that_is_stopped_puts_the_switch_back_and_leaves_no_writer(self):
        self.boot_kit()
        self.data_role(None)
        switch = self.root / "sys/module/sm5714_usbpd/parameters/sink_host_swap"
        path = self.tmp / "run-verify.sh"
        path.write_text(self.script("verify.sh"))
        # The writer looks for the partner once a second here: one that the
        # stop leaves behind outlives the script by up to that second.
        (self.bin / "sleep").write_text("#!/bin/sh\ncase $1 in\n0.005) exec /usr/bin/sleep 1 ;;\n"
                                        "esac\nexec /usr/bin/sleep 0.01\n")
        env = {"PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_ROOT": str(self.root),
               "HOME": str(self.tmp)}
        run = subprocess.Popen(["sh", str(path), "swap", "3"], env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
        self.addCleanup(run.wait)
        self.addCleanup(run.kill)
        kmsg = self.root / "dev/kmsg"
        deadline = time.monotonic() + 60
        while not (kmsg.exists() and kmsg.read_text().endswith("trial 1\n")):
            self.assertLess(time.monotonic(), deadline)
            self.assertIsNone(run.poll())
            time.sleep(0.005)
        # It waits for the plug-in now, and its writer with it: a subshell
        # that is there on two looks 0.05 s apart (a command it starts is a
        # copy of it only until its exec).
        writers = set()
        while not writers:
            self.assertLess(time.monotonic(), deadline)
            self.assertIsNone(run.poll())
            before = self.scripts_running(path) - {run.pid}
            time.sleep(0.05)
            writers = before & self.scripts_running(path)
        self.assertEqual(switch.read_text(), "N\n")
        run.terminate()
        # Nothing of the run is left once it has ended.  Looked at before
        # the pipes are read to their end, which a writer left behind
        # holds open.
        run.wait(timeout=20)
        left = self.scripts_running(path)
        stdout, stderr = run.communicate(timeout=60)
        self.assertEqual(run.returncode, 1, stdout + stderr)
        self.assertIn("stopped; what was recorded is in", stdout)
        self.assertEqual(switch.read_text(), "Y\n")
        self.assertEqual(left, set())
        # A writer left behind would take this plug-in.
        self.data_role("device")
        self.partner.mkdir()
        time.sleep(0.3)
        self.assertEqual(self.port.joinpath("data_role").read_text(), "host [device]\n")

    def test_a_writer_that_is_not_done_when_its_plug_in_is_recorded_ends_there(self):
        """A write to data_role can wait seconds for its swap.  A writer that
        is still at it when the plug-in is recorded is stopped then: left
        running, it would write into the next plug-in ahead of that one's
        own writer."""
        self.boot_kit()
        self.plug_for_swap([(FLAP_LOG, None), (SWAP_EARLY_LOG, "host")])
        # The first plug-in takes no write, and the writer's last writes are
        # a second apart here: it is not done when the roles have been read.
        (self.bin / "sleep").write_text("#!/bin/sh\ncase $1 in\n1) exec /usr/bin/sleep 0.05 ;;\n"
                                        "0.005) exec /usr/bin/sleep 0.001 ;;\n"
                                        "0.3) exec /usr/bin/sleep 1 ;;\nesac\n"
                                        "exec /usr/bin/sleep 0.01\n")
        result = self.run_script("verify.sh", "swap", "2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([r["verdict"] for r in self.results()], ["-", "PASS"])
        self.assertEqual([r["write"] for r in self.swap_rows()],
                         ["the writer was stopped before it was done",
                          "write 1 was accepted, the data role right after it: host"])
        self.assertIn("0 cut short by a Soft Reset; the writer was stopped before it was done\n",
                      result.stdout)
        # A writer says its word when it ends by itself; the first one never
        # did, also not after its plug-in was recorded.
        self.assertEqual([p.name for p in (self.kit / "logs").glob("*/trial-*.writes")],
                         ["trial-2.writes"])

    def test_the_usage_names_every_mode_and_nothing_else(self):
        result = self.run_script("verify.sh", "docks")
        self.assertEqual(result.returncode, 2)
        usage = result.stderr.splitlines()
        self.assertEqual(usage[0], "Checks after booting the Type-C roles kit, and a recorder "
                                   "for plug-in tests.")
        self.assertEqual(usage[-1], "  sh ~/typec-roles/verify.sh summary LOG...   "
                                    "the counters of saved TCPM logs")
        for mode in ("dock [N]", "record [N]", "now ", "swap [N]", "summary LOG..."):
            self.assertEqual(sum(f"verify.sh {mode}" in line for line in usage), 1, mode)

    def boot_kit(self):
        self.boot_kit_kernel(NEW_BUILD_ID)

    def test_checks_pass_on_the_kit_kernel(self):
        self.boot_kit()
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("FAIL", result.stdout)
        self.assertIn("ok   sm5714_usbpd.sink_host_swap = Y", result.stdout)
        self.assertIn("ok   sm5714_usbpd.try_snk_extra_wait_ms = 600", result.stdout)
        self.assertIn(f"ok   the kernel trusts the kit's module key ({KEY_ID[:12]})", result.stdout)
        self.assertIn("ok   ramoops registered with ECC 96", result.stdout)
        self.assertIn("ok   netconsole module present and signed with the kit's key", result.stdout)
        self.assertIn("ok   no module was refused, and no symbol failures this boot", result.stdout)
        self.assertIn("all checks passed", result.stdout)
        # A controller that has not been through a suspend has shown nothing.
        self.assertIn("note the USB host controller did not die, but no suspend cycle has run yet "
                      "this boot: run this again later", result.stdout)
        self.assertNotIn("ok   the USB host controller", result.stdout)
        # Nothing is attached, so there is no bus to count devices on.
        self.assertNotIn("on the bus", result.stdout)

    def journal_add(self, text, guard=None):
        """TEXT at the end of this boot's kernel log.  On a kernel with
        revision 11's root port switch (the kit's), each suspend of TEXT gets
        the PCIe host's line that its guard held (GUARD_KEPT, right after the
        suspend's first line) unless TEXT has a "system sleep:" line of its
        own, as that kernel logs one at every resume; GUARD says so for any
        kernel (True) or none (False)."""
        if guard is None:
            guard = (self.root / "sys/module" / RPD3_SWITCH).exists() and "system sleep:" not in text
        if guard:
            text = text.replace("kernel: PM: suspend entry (deep)\n",
                                f"kernel: PM: suspend entry (deep)\nkernel: {GUARD_KEPT}\n")
        with (self.root / "journal").open("a") as journal:
            journal.write(text)

    def wifi_function(self):
        """The Wi-Fi's PCI function as sysfs shows it, ath11k bound."""
        d = self.root / "sys/devices/platform/soc@0/1c00000.pcie/pci0000:00/0000:00:00.0/0000:01:00.0"
        d.mkdir(parents=True)
        (d / "current_link_speed").write_text("5.0 GT/s PCIe\n")
        driver = self.root / "sys/bus/pci/drivers/ath11k_pci"
        driver.mkdir(parents=True)
        (d / "driver").symlink_to(os.path.relpath(driver, d))
        bus = self.root / "sys/bus/pci/devices"
        bus.mkdir(parents=True, exist_ok=True)
        (bus / "0000:01:00.0").symlink_to(os.path.relpath(d, bus))

    def test_the_wifi_is_checked_after_the_boot_s_first_suspend(self):
        """Revision 11 lets pcie_0_gdsc collapse in system suspend, and its
        guard keeps it on under the kept link of the boot's panel cycle:
        after the boot's first suspend a PCI core, ath11k or MHI line that
        says the Wi-Fi did not come back fails, on any kernel; with none and
        the function bound it passes; before any suspend, or without the
        function, it is a note."""
        cycle = (f"kernel: PM: suspend entry (deep)\nkernel: {GUARD_KEPT}\n"
                 "kernel: PM: suspend debug: Waiting for 0 second(s).\n")
        fw = ("kernel: ath11k_pci 0000:01:00.0: fw_version 0x11021302 fw_build_timestamp 2026-02-04 08:10\n"
              "kernel: PM: suspend exit\n")
        self.boot_kit()
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("note no suspend has run yet this boot (the panel cycle runs about 22 s in): the Wi-Fi after it "
                      "was not checked; run this again later\n", result.stdout)
        self.wifi_function()
        self.journal_add(cycle + fw)
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok   Wi-Fi after this boot's first suspend: 0000:01:00.0, link 5.0 GT/s PCIe, no PCI, ath11k or "
                      "MHI failure line\n", result.stdout)
        self.assertNotIn("no ath11k firmware line", result.stdout)
        lost = ("kernel: pci 0000:01:00.0: Unable to change power state from D3hot to D0, device inaccessible",
                "kernel: ath11k_pci 0000:01:00.0: failed to power up hif during resume: -110",
                "kernel: mhi mhi0: Device failed to enter MHI Ready",
                "kernel: pcieport 0000:00:00.0: AER: Uncorrectable (Fatal) error message received from 0000:01:00.0")
        for line in lost:
            with self.subTest(line):
                self.boot_kit()
                self.journal_add(cycle + line + "\nkernel: PM: suspend exit\n")
                result = self.run_script("verify.sh")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("FAIL the Wi-Fi did not come back from this boot's first suspend (the panel cycle, unless "
                              f"a sleep test ran before it) (1 lines):\n{line}\n", result.stdout)
                self.assertIn("note on revision 11's kernel the guard keeps pcie_0_gdsc on in that suspend (the "
                              'guard\'s line above): README, "Revision 11: the runs", says what to do when the Wi-Fi '
                              "did not come back all the same\n", result.stdout)
                self.assertEqual(result.stdout.count("FAIL "), 1, result.stdout)
        # Such a line before the first suspend is not about it; and the
        # function taken off the bus (pci=remove) is a note.
        (self.root / "sys/bus/pci/devices/0000:01:00.0").unlink()
        self.boot_kit()
        (self.root / "journal").write_text(lost[1] + "\n" + (self.root / "journal").read_text() + cycle + fw)
        result = self.run_script("verify.sh")
        self.assertNotIn("the Wi-Fi did not come back", result.stdout)
        # No function on the bus, no failure line: a note.
        self.assertIn("note no Wi-Fi function on the PCI bus with ath11k_pci bound, and no line that says it did not "
                      "come back from this boot's first suspend (a sleep test's pci=remove leaves the bus so until a "
                      "restart)\n", result.stdout)

    def test_the_guard_of_the_pcie_power_domain_is_checked_in_the_boot_s_first_suspend(self):
        """Revision 11's guard: on the kit's kernel the boot's first suspend
        (the panel cycle) has to have the PCIe host's line of a kept link
        with its power domain on, and none this boot may have the domain off
        under a kept link (a FAIL on any kernel); a first suspend without the
        host's line fails on the kit's kernel; another of its lines there is
        a note (a sleep test before the panel cycle); before any suspend,
        and on another kernel, it is a note."""
        panel = ("kernel: PM: suspend entry (deep)\nkernel: PM: suspend debug: Waiting for 0 second(s).\n"
                 "kernel: {line}\nkernel: PM: suspend exit\n")
        self.boot_kit()
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("note no suspend has run yet this boot (the panel cycle runs about 22 s in): the PCIe power "
                      "domain's guard was not checked; run this again later\n", result.stdout)
        self.journal_add(panel.format(line=GUARD_KEPT) + panel.format(line=GUARD_DOWN))
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok   PCIe power domain guard: in this boot's first suspend (the panel cycle) the host kept its "
                      "link and pcie_0_gdsc stayed on (2 sleeps logged this boot, none with the domain off under a "
                      "kept link)\n", result.stdout)
        # The guard failing, in any suspend of the boot.
        self.boot_kit()
        self.journal_add(panel.format(line=GUARD_KEPT) + panel.format(line=GUARD_BAD))
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL the guard did not hold this boot: genpd powered the PCIe host's power domain off under a "
                      "link it kept (1 of 2 sleeps; the Wi-Fi's controller lost its state under it):\n"
                      f"kernel: {GUARD_BAD}\n", result.stdout)
        self.assertEqual(result.stdout.count("FAIL "), 1, result.stdout)
        # No line in the first suspend: the kit's kernel logs one at every
        # resume.
        self.boot_kit()
        self.journal_add("kernel: PM: suspend entry (deep)\nkernel: PM: suspend exit\n", guard=False)
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL no line of the PCIe power domain's guard in this boot's first suspend: the kit's kernel "
                      "logs one at every resume (\"system sleep: ...\"); without it nothing shows that pcie_0_gdsc "
                      "stayed on under the Wi-Fi's link\n", result.stdout)
        self.assertEqual(result.stdout.count("FAIL "), 1, result.stdout)
        # A sleep test with pci=remove before the panel cycle: a note.
        self.boot_kit()
        self.journal_add(panel.format(line=GUARD_DOWN))
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("note PCIe power domain guard, this boot's first suspend: the host powered down, and genpd "
                      "powered its power domain off (a sleep test before the panel cycle, or a Wi-Fi without its "
                      "link); 1 sleeps logged this boot, none with the domain off under a kept link\n", result.stdout)
        # Another kernel: a note, with the lines or without.
        for lines, said in ((GUARD_KEPT, "note the running kernel is not the kit's; its PCIe host logged 1 sleeps, "
                                         "the first: the link was kept, and its power domain stayed on\n"),
                            (None, "note the running kernel is not the kit's and logs no line of the PCIe power "
                                   "domain's guard (revision 11's kernel has it)\n")):
            with self.subTest(said):
                self.boot_kit()
                self.running(HANG_BUILD_ID)
                self.journal_add(panel.format(line=lines) if lines else
                                 "kernel: PM: suspend entry (deep)\nkernel: PM: suspend exit\n", guard=False)
                result = self.run_script("verify.sh")
                self.assertIn(said, result.stdout)
                self.assertNotIn("guard did not hold", result.stdout)

    def test_checks_see_the_ufs_phy_through_the_diagnostic_s_lines(self):
        """Revision 8: the PCS reset logs nothing; on the kit's kernel the
        diagnostic logs each calibration of the UFS PHY.  A calibration that
        failed fails the checks, with the diagnostic's lines; so does the
        kit's kernel with no calibration line at all.  On another kernel the
        reset cannot be seen, and that is all that is said."""
        self.boot_kit()
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok   UFS PHY: 1 calibrations this boot, each logged by the diagnostic of the "
                      "kit's kernel, none failed\n"
                      "note last calibration: SW_RESET 0x1 START 0 PWRDN 0x1 READY 0 C_READY 0 CMN 0\n",
                      result.stdout)
        # A resume whose calibration timed out, as on 2026-11-21 (with
        # revision 10's power-up line before its entry).
        self.journal_add("kernel: PM: suspend entry (deep)\n" + UFS_PWRUP + "\n" + "\n".join(UFS_TIMEOUT) + "\n")
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("FAIL "), 1, result.stdout)
        shown = "\n".join(UFS_TIMEOUT[1:]) + "\n"
        self.assertIn("FAIL the UFS PHY or host failed to come up this boot (4 lines; the diagnostic's "
                      "lines follow theirs):\n" + UFS_PWRUP + "\n" + UFS_CAL_ENTRY + "\n" + UFS_PWRUP + "\n"
                      + UFS_TIMEOUT[0] + "\n" + shown, result.stdout)
        self.assertNotIn("calibrations this boot", result.stdout)
        # The kit's kernel without the diagnostic's lines: not the kernel the
        # kit was made from, or the lines were lost.
        self.boot_kit()
        (self.root / "journal").write_text((self.root / "journal").read_text().replace(
            UFS_CAL_ENTRY + "\n", "").replace(UFS_PWRUP + "\n", ""))
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("FAIL "), 1, result.stdout)
        self.assertIn("FAIL no UFS PHY calibration line this boot: the kit's kernel logs one at every "
                      "calibration (the kernel's own log: sudo dmesg | grep 'calibration entry')",
                      result.stdout)
        # Another kernel: the build ID check fails, and the UFS PHY's change
        # is said to be out of sight, not to be missing.
        self.running(HANG_BUILD_ID)
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"FAIL build ID {HANG_BUILD_ID}, the kit's is {NEW_BUILD_ID}", result.stdout)
        self.assertIn("note the running kernel is not the kit's: whether it resets the UFS PHY's PCS "
                      "before calibration cannot be seen from userspace", result.stdout)
        self.assertNotIn("UFS PHY calibration line", result.stdout)

    def test_the_sleep_test_s_own_kernel_log_lines_are_not_counted(self):
        """sleep-test.sh writes its verdicts to the kernel log, and a failed
        cycle's verdict quotes the first failure line.  verify.sh, run in
        the same boot (during the run's hold), counts the kernel's lines
        only; a copy of a diagnostic line, which the run does not write
        there, would not count either."""
        own = "kernel: gts9u sleep-test: 20261122-031500 "
        self.boot_kit()
        self.journal_add(own + "cycle 1/1: diagnostic: " + UFS_CAL_ENTRY[len("kernel: "):] + "\n" +
                         own + "SLEEP-TEST cycle 1/1: PASS: deep (aosd +1 cxsd +1 ddr +1), resumed\n")
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok   UFS PHY: 1 calibrations this boot", result.stdout)
        self.journal_add("kernel: PM: suspend entry (deep)\n" + "\n".join(UFS_TIMEOUT) + "\n" +
                         own + "SLEEP-TEST cycle 1/1: FAIL: 4 failure lines in the kernel log, the first: "
                         + UFS_TIMEOUT[1][len("kernel: "):] + "\n" +
                         own + "SLEEP-TEST RESULT: FAIL at cycle 1/1: 4 failure lines in the kernel log, "
                         "the first: " + UFS_TIMEOUT[1][len("kernel: "):] + "\n")
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL the UFS PHY or host failed to come up this boot (4 lines; ", result.stdout)
        self.assertNotIn("gts9u sleep-test", result.stdout)

    def test_checks_see_the_debug_switches_as_the_kit_s_kernel_boots_them(self):
        """Revisions 9 to 11: on the kit's kernel each of the four debug
        switches is there and reads as revision 11's kernel boots it, pcie0
        Y (pcie_0_gdsc OFF_ON) and the others N, and debug_boot_state says
        so; a switch that reads otherwise, or one missing, fails.  On
        another kernel they are a note: listed if it has them, said to be
        absent if not."""
        names = ("gcc_sm8550.debug_pcie0_phy_off", "gcc_sm8550.debug_usb_off", "rpmhpd.debug_ret_floor",
                 "pcie_qcom.debug_root_port_d3")
        self.boot_kit()
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok   debug switches at boot (gcc_sm8550.debug_boot_state): pcie0 Y, pcie0phy N, usb N: "
                      "pcie_0_gdsc collapses in system suspend (OFF_ON), revision 11's default\n", result.stdout)
        self.assertIn("ok   debug switch gcc_sm8550.debug_pcie0_off = Y (on: pcie_0_gdsc OFF_ON, the kit's default "
                      "since revision 11)\n", result.stdout)
        for name in names:
            self.assertIn(f"ok   debug switch {name} = N (off)\n", result.stdout)
        for values, line in (
                ({"usb": "Y"}, "FAIL debug switch gcc_sm8550.debug_usb_off is Y, expected N (off): the kit's kernel "
                               "starts every boot with it off, and only a sleep test sets it, until its restart\n"),
                ({"cxmx": None}, "FAIL debug switch rpmhpd.debug_ret_floor is missing, expected N (off)"),
                # Revision 11's root port switch: set by hand, or a kernel
                # without it (revision 11 as first prepared).
                ({"rpd3": "Y"}, "FAIL debug switch pcie_qcom.debug_root_port_d3 is Y, expected N (off): the kit's "
                                "kernel starts every boot with it off, and only a sleep test sets it, until its "
                                "restart\n"),
                ({"rpd3": None}, "FAIL debug switch pcie_qcom.debug_root_port_d3 is missing, expected N (off)"),
                # gdsc-ret=pcie0 by hand, or a kernel without the new default.
                ({"pcie0": "N"}, "FAIL debug switch gcc_sm8550.debug_pcie0_off is N, expected Y: the kit's kernel "
                                 "starts every boot with pcie_0_gdsc OFF_ON, and only a sleep test (gdsc-ret=pcie0) "
                                 "changes it, until its restart\n"),
                ({"boot": None}, "FAIL gcc_sm8550.debug_boot_state is missing, expected 'pcie0 Y, pcie0phy N, usb "
                                 "N': the kit's kernel boots with pcie_0_gdsc OFF_ON and the other GDSCs in "
                                 "retention\n"),
                ({"boot": "pcie0 N, pcie0phy N, usb N"}, "FAIL gcc_sm8550.debug_boot_state is pcie0 N, pcie0phy N, "
                                                         "usb N, expected 'pcie0 Y, pcie0phy N, usb N'")):
            with self.subTest(line):
                self.boot_kit()
                self.r11_switches(**values) if "boot" not in values else self.debug_switches(
                    pcie0="Y", rpd3="N", **values)
                result = self.run_script("verify.sh")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(line, result.stdout)
                self.assertEqual(result.stdout.count("FAIL "), 1, result.stdout)
        # Another kernel: revision 8's, with none of them, or one with them
        # (revision 10's, all N), or revision 11's.
        self.boot_kit()
        self.running(HANG_BUILD_ID)
        self.debug_switches(pcie0=None, pcie0phy=None, usb=None, cxmx=None)
        result = self.run_script("verify.sh")
        self.assertIn("note the running kernel is not the kit's and offers no debug switches (the kernels of "
                      "revisions 9 to 11 have them, the root port's from revision 11)\n", result.stdout)
        self.assertNotIn("debug switch gcc_sm8550", result.stdout)
        self.debug_switches(usb="Y")
        result = self.run_script("verify.sh")
        self.assertIn("note debug switches of the running kernel, which is not the kit's: "
                      "gcc_sm8550.debug_pcie0_off=N gcc_sm8550.debug_pcie0_phy_off=N gcc_sm8550.debug_usb_off=Y "
                      "rpmhpd.debug_ret_floor=N\n", result.stdout)
        self.assertEqual(result.stdout.count("FAIL "), 1, result.stdout)
        self.assertIn(f"FAIL build ID {HANG_BUILD_ID}, the kit's is {NEW_BUILD_ID}", result.stdout)
        self.r11_switches()
        result = self.run_script("verify.sh")
        self.assertIn("note debug switches of the running kernel, which is not the kit's: "
                      "gcc_sm8550.debug_pcie0_off=Y gcc_sm8550.debug_pcie0_phy_off=N gcc_sm8550.debug_usb_off=N "
                      "rpmhpd.debug_ret_floor=N pcie_qcom.debug_root_port_d3=N (at boot: pcie0 Y, pcie0phy N, usb N)\n",
                      result.stdout)

    def test_checks_see_revision_10_s_pcie_gpu_and_power_up_lines(self):
        """Revision 10: the Wi-Fi's PCIe link has to come up at boot without
        unpark-pcie0-pipe-mux.patch, no PCIe host or PHY failure line may
        follow (and one is not a UFS failure, though two of its words are
        the UFS's), no GPU RSC sequence may fail, and every calibration of
        the UFS PHY has the second diagnostic's line before it."""
        pcie_phy = "kernel: qcom-qmp-pcie-phy 1c06000.phy: phy initialization timed-out"
        gpu = "kernel: msm_dpu ae01000.display-controller: [drm:a6xx_rpmh_stop] *ERROR* Unable to power off the GPU RSC"
        self.boot_kit()
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for said in ("ok   PCIe: the link came up this boot (PCIe Gen.2 x1 link up)\n",
                     "ok   GPU: no RSC sleep or wake sequence failed this boot\n",
                     "note last power-up write: PWRDN 0x1 right after it, 0x1 100 us later (0x1 is the PHY powered "
                     "up)\n",
                     "ok   UFS PHY: 1 calibrations this boot"):
            self.assertIn(said, result.stdout)
        for journal, line in (
                (lambda j: j.replace(PCIE_UP + "\n", ""),
                 "FAIL no PCIe link-up line this boot: the Wi-Fi's link did not come up (revision 10 switches its "
                 "pipe clock by the clock's own enable, without unpark-pcie0-pipe-mux.patch)\n"),
                (lambda j: j + PCIE_INIT_FAILED + "\n",
                 "FAIL the PCIe host or its PHY failed this boot (1 lines):\n" + PCIE_INIT_FAILED + "\n"),
                (lambda j: j + pcie_phy + "\n" + PCIE_INIT_FAILED + "\n",
                 "FAIL the PCIe host or its PHY failed this boot (2 lines):\n" + pcie_phy + "\n"),
                (lambda j: j + gpu + "\n",
                 "FAIL the GPU RSC's sleep or wake sequence failed this boot (1 lines):\n" + gpu + "\n"),
                (lambda j: j.replace(UFS_PWRUP + "\n", ""),
                 "FAIL 1 UFS PHY calibrations and 0 power-up write lines this boot: the kit's kernel logs one "
                 "before every calibration\n")):
            with self.subTest(line.splitlines()[0]):
                self.boot_kit()
                (self.root / "journal").write_text(journal((self.root / "journal").read_text()))
                result = self.run_script("verify.sh")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(line, result.stdout)
                self.assertEqual(result.stdout.count("FAIL "), 1, result.stdout)
                # The PCIe host's lines are not the storage's.
                self.assertNotIn("the UFS PHY or host failed", result.stdout)
        # Another kernel without a link-up line: a note, not a failure.
        self.boot_kit()
        self.running(HANG_BUILD_ID)
        (self.root / "journal").write_text((self.root / "journal").read_text().replace(PCIE_UP + "\n", "")
                                           .replace(UFS_PWRUP + "\n", ""))
        result = self.run_script("verify.sh")
        self.assertIn("note no PCIe link-up line this boot\n", result.stdout)
        self.assertNotIn("power-up write", result.stdout)
        self.assertEqual(result.stdout.count("FAIL "), 1, result.stdout)
        self.assertIn(f"FAIL build ID {HANG_BUILD_ID}, the kit's is {NEW_BUILD_ID}", result.stdout)

    def test_the_ufs_host_s_clock_counts_are_checked_under_sudo(self):
        """Revision 10's lane clock fix: under sudo (debugfs is root's) the
        UFS host's five clocks, from each clock's own counters (revision 11;
        revision 10 read clk_summary); on the kit's kernel a count above 3 is
        the leak and fails, on another kernel the counts are a note.
        Without the files, a note says it needs root."""
        self.boot_kit()
        result = self.run_script("verify.sh")
        self.assertIn(f"note the UFS host clocks' enable counts need root: sudo sh {self.kit}/verify.sh\n",
                      result.stdout)
        clk = self.root / "sys/kernel/debug/clk"
        write_clocks(clk, **CLK_LEAK)
        leak = ("tcsr_ufs_pad_clkref_en 522/522, gcc_ufs_phy_ahb_clk 523/523, gcc_ufs_phy_axi_clk 522/522, "
                "gcc_ufs_phy_unipro_core_clk 522/522, gcc_ufs_phy_ice_core_clk 523/523")
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"FAIL UFS host clocks enabled up to 523 times ({leak}; enable/prepare): with the kit's "
                      "kernel no count is above 3\n", result.stdout)
        self.assertEqual(result.stdout.count("FAIL "), 1, result.stdout)
        # As the fix leaves them while the UFS works: ufshcd's one, and the
        # inline crypto engine's own on its core and AHB clocks.
        write_clocks(clk, bi_tcxo=(9, 9), tcsr_ufs_pad_clkref_en=(1, 1), gcc_ufs_phy_ahb_clk=(3, 3),
                     gcc_ufs_phy_axi_clk=(1, 1), gcc_ufs_phy_unipro_core_clk=(1, 1), gcc_ufs_phy_ice_core_clk=(3, 3))
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok   UFS host clocks (enable/prepare): tcsr_ufs_pad_clkref_en 1/1, gcc_ufs_phy_ahb_clk 3/3, "
                      "gcc_ufs_phy_axi_clk 1/1, gcc_ufs_phy_unipro_core_clk 1/1, gcc_ufs_phy_ice_core_clk 3/3\n",
                      result.stdout)
        # Another kernel: the counts, and no verdict on them.
        write_clocks(clk, **CLK_LEAK)
        self.running(HANG_BUILD_ID)
        result = self.run_script("verify.sh")
        self.assertIn(f"note UFS host clocks of the running kernel, which is not the kit's (enable/prepare): {leak}\n",
                      result.stdout)
        self.assertEqual(result.stdout.count("FAIL "), 1, result.stdout)

    def test_checks_know_the_controller_s_other_ways_of_dying(self):
        for line in USB_HOST_ERRORS:
            with self.subTest(line):
                self.boot_kit()
                self.journal_add(f"kernel: PM: suspend entry (deep)\n{line}\n"
                                 "kernel: PM: suspend exit\n")
                result = self.run_script("verify.sh")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(f"FAIL the USB host controller died this boot:\n{line}\n",
                              result.stdout)
                self.assertEqual(result.stdout.count("FAIL "), 1, result.stdout)
                self.assertNotIn("the USB host controller did not die", result.stdout)
                self.assertNotIn("came back from a suspend", result.stdout)

    def test_a_controller_that_was_set_up_again_after_a_suspend_is_noted_and_is_not_a_death(self):
        self.boot_kit()
        self.journal_add("kernel: PM: suspend entry (deep)\n"
                         f"{USB_HOST_REINIT}\nkernel: PM: suspend exit\n")
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("note the USB host controller came back from a suspend without its state and "
                      "was set up again (times this boot: 1)", result.stdout)
        self.assertNotIn("the USB host controller died", result.stdout)
        self.assertIn("ok   the USB host controller did not die this boot (suspend cycles so far: 1)",
                      result.stdout)
        self.assertNotIn("FAIL", result.stdout)

    def test_checks_fail_when_the_kernel_refused_a_module(self):
        """Modules of another build under the kit's kernel, or the kit's under
        the hang-log kit's: the locked-down kernel loads none of them."""
        self.boot_kit()
        self.journal_add(MODULE_REFUSED + "\n")
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"FAIL modules refused or symbol failures this boot:\n{MODULE_REFUSED}\n",
                      result.stdout)
        self.assertEqual(result.stdout.count("FAIL "), 1, result.stdout)
        self.assertNotIn("no module was refused", result.stdout)
        self.assertIn("some checks FAILED", result.stdout)

    def test_checks_that_read_the_kernel_log_do_not_pass_without_it(self):
        """A user outside the adm and systemd-journal groups gets nothing
        from journalctl -k."""
        for name, lose in (("empty", lambda: (self.root / "journal").write_text("")),
                           ("unreadable", lambda: (self.root / "journal").unlink())):
            with self.subTest(name):
                self.boot_kit()
                lose()
                result = self.run_script("verify.sh")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("FAIL cannot read this boot's kernel log (journalctl -k -b): the "
                              "module key, ramoops, the module failures and the USB host "
                              "controller were not checked\n", result.stdout)
                self.assertEqual(result.stdout.count("FAIL "), 1, result.stdout)
                for claim in ("the kernel trusts", "Loaded X.509", "ramoops registered",
                              'no "ramoops', "no module was refused", "symbol failures this boot",
                              "the USB host controller died", "came back from a suspend"):
                    self.assertNotIn(claim, result.stdout)
                # What does not need the log is still checked.
                self.assertIn("ok   netconsole module present and signed with the kit's key",
                              result.stdout)
                self.assertIn("ok   sm5714_usbpd.sink_host_swap = Y", result.stdout)

    def test_checks_fail_when_the_usb_host_controller_died(self):
        """The kit carries the USB power domain change: a controller that did
        not come back fails the check, as the SM-X910's and this tablet's
        did without it."""
        for name, journal, lines in (
                ("the SM-X910's", USB_HOST_DIED,
                 ("xHCI host not responding to stop endpoint command", "HC died; cleaning up",
                  "Host halt failed, -110")),
                ("the SM-X710's", USB_HOST_PROBE_FAILED, ("Host halt failed, -110",))):
            with self.subTest(name):
                self.boot_kit()
                self.journal_add(journal)
                self.roles("host", "sink")
                self.partner.mkdir(exist_ok=True)
                (self.root / "sys/bus/usb/devices/1-1/idVendor").unlink(missing_ok=True)
                result = self.run_script("verify.sh")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("FAIL the USB host controller died this boot:", result.stdout)
                for line in lines:
                    self.assertIn(line, result.stdout)
                self.assertIn("note USB host with no device on the bus", result.stdout)
                self.assertEqual(result.stdout.count("FAIL "), 1, result.stdout)
                self.assertIn("some checks FAILED", result.stdout)

    def test_checks_count_the_suspend_cycles_and_the_devices_behind_a_dock(self):
        """A boot with the dock attached, as it should end: the boot's suspend
        cycle ran, the controller is alive and the hub is on the bus."""
        self.boot_kit()
        self.journal_add("kernel: PM: suspend entry (deep)\nkernel: PM: suspend exit\n")
        self.roles("host", "sink")
        self.partner.mkdir()
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok   the USB host controller did not die this boot (suspend cycles so far: 1)",
                      result.stdout)
        self.assertIn("ok   USB host, devices on the bus: 1", result.stdout)
        self.assertIn("ok   Type-C port registered: data host, power sink, partner present",
                      result.stdout)

    def test_a_usb_host_without_devices_is_noted_and_does_not_fail(self):
        """A charger that took the swap has nothing behind it, and the Anker
        hub without its charger enumerates nothing."""
        self.boot_kit()
        self.roles("host", "sink")
        self.partner.mkdir()
        (self.root / "sys/bus/usb/devices/1-1/idVendor").unlink()
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("note USB host with no device on the bus (nothing behind the dock, "
                      "or its hub did not come up)", result.stdout)
        self.assertNotIn("FAIL", result.stdout)

    def test_checks_fail_on_the_hang_log_kernel(self):
        self.boot_kit()
        self.running(HANG_BUILD_ID)
        for name in ("sink_host_swap", "try_snk_extra_wait_ms"):
            (self.root / "sys/module/sm5714_usbpd/parameters" / name).unlink()
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"FAIL build ID {HANG_BUILD_ID}", result.stdout)
        self.assertIn("FAIL sm5714_usbpd.sink_host_swap is missing", result.stdout)

    def test_a_root_that_ext4_gave_up_on_is_not_writable(self):
        # 2026-11-21: UFS did not come back from a sleep, ext4 aborted its
        # journal, and the root stayed "rw,noatime,emergency_ro".
        self.boot_kit()
        for options, passes in (("rw,noatime", True), ("rw,noatime,emergency_ro", False),
                                ("rw,relatime,shutdown", False), ("ro,relatime", False),
                                ("rw,noatime,errors=remount-ro", True)):
            with self.subTest(options):
                (self.root / "mount-options").write_text(options + "\n")
                result = self.run_script("verify.sh")
                if passes:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("ok   root is rw", result.stdout)
                else:
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn(f"FAIL root is not writable (mounted {options})", result.stdout)
                    self.assertNotIn("ok   root is rw", result.stdout)
                    self.assertNotIn("all checks passed", result.stdout)

    def ext4_record(self, count, func="", line="0"):
        """ext4's record of the root, as /sys/fs/ext4/sda34 shows it."""
        d = self.root / "sys/fs/ext4/sda34"
        d.mkdir(parents=True, exist_ok=True)
        for name, value in (("errors_count", count), ("last_error_func", func), ("last_error_line", line)):
            (d / name).write_text(f"{value}\n")

    def test_an_error_ext4_recorded_on_the_root_fails_in_any_boot(self):
        """With an error recorded and deleted files held open, the root does
        not mount after a reset (this port's initramfs has no fsck): an
        error count above 0, an error the mount found recorded (in the
        superblock or the journal's), or one recorded since, fails; a
        stick's does not, nor sleep-test.sh's own lines that quote one."""
        self.boot_kit()
        result = self.run_script("verify.sh")
        self.assertIn("note cannot read ", result.stdout)
        self.assertIn("ok   the root (sda34) was mounted with no ext4 error found or recorded this boot",
                      result.stdout)
        self.ext4_record(0)
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok   ext4 has recorded no error on the root (sda34: errors_count 0)", result.stdout)
        self.ext4_record(5, "ext4_mark_recovery_complete", 6370)
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL ext4 has recorded 5 errors on the root (sda34, the last in "
                      "ext4_mark_recovery_complete:6370): e2fsck -f it from recovery", result.stdout)
        self.ext4_record(0)
        own = "kernel: gts9u sleep-test: 20261122-031500 SLEEP-TEST ERROR-STATE: ... "
        for line, fails in (
                ("kernel: EXT4-fs warning (device sda34): ext4_clear_journal_err:6412: Filesystem error recorded "
                 "from previous mount: IO failure", True),
                ("kernel: EXT4-fs (sda34): warning: mounting fs with errors, running e2fsck is recommended", True),
                ("kernel: EXT4-fs error (device sda34): ext4_mark_recovery_complete:6370: comm mount: Orphan file "
                 "not empty on read-only fs.", True),
                ("kernel: EXT4-fs error (device sdg2): __ext4_find_entry:1622: inode #2: comm gvfsd: reading "
                 "directory lblock 0", False),
                (own + "EXT4-fs error (device sda34): __ext4_find_entry:1622", False)):
            with self.subTest(line):
                self.boot_kit()
                self.journal_add(line + "\n")
                result = self.run_script("verify.sh")
                if fails:
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertEqual(result.stdout.count("FAIL "), 1, result.stdout)
                    self.assertIn("FAIL ext4 found or recorded an error on the root (sda34) this boot: e2fsck -f it "
                                  f"from recovery before the next reset (docs/resume-recovery.md, \"Repairing the "
                                  f"root from recovery\"):\n{line}\n", result.stdout)
                else:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("ok   the root (sda34) was mounted with no ext4 error", result.stdout)

    def test_a_replayed_journal_fails_only_the_boot_right_after_a_sleep_test(self):
        """"recovery required on readonly filesystem" is what a power loss
        leaves; after sleep-test.sh's restart, whose root was frozen (the
        journal flushed, needs_recovery clear), it means the freeze did not
        hold to the restart.  That boot is known by the run's RESTART line in
        the pmsg this boot archived; without the archive it cannot be told."""
        replayed = ("kernel: EXT4-fs (sda34): INFO: recovery required on readonly filesystem\n"
                    "kernel: EXT4-fs (sda34): write access will be enabled during recovery\n"
                    "kernel: EXT4-fs (sda34): recovery complete\n")
        clean = ("kernel: EXT4-fs (sda34): orphan cleanup on readonly fs\n"
                 "kernel: EXT4-fs (sda34): mounted filesystem 3c5f-uuid ro with ordered data mode. Quota mode: "
                 "none.\n")
        boot = (self.root / "proc/sys/kernel/random/boot_id").read_text().strip()
        archive = self.root / "var/lib/ubuntu-gts9u/pstore"

        def archived(name, by, pmsg):
            d = archive / name
            d.mkdir(parents=True)
            (d / "INFO").write_text(f"archived:      2026-11-22 11:47:20 UTC by boot {by}\n")
            (d / "pmsg-ramoops-0").write_text(pmsg)

        restart = "gts9u sleep-test: 20261122-031500 SLEEP-TEST RESTART: sysrq b; frozen: /media/stick /\n"
        # No archive: a replayed journal is noted, whatever came before.
        self.boot_kit()
        self.journal_add(replayed)
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"whether this boot is one cannot be told (there is no {archive})", result.stdout)
        # This boot archived a sleep test's restart: the same lines fail.
        archived("000009-20261122T100000Z-boot-0aa11bb2", "0aa11bb2-0000", restart)
        archived("000010-20261122T114720Z-boot-788ce779", boot, "gts9u sleep-test: 20261122-031500 SLEEP-TEST "
                 "RESULT: DRY: no sleep tried\n" + restart)
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL this is the boot after a sleep test's restart, and a journal was not empty at the "
                      "mount: the root was not frozen at the restart, or something wrote to it while it was; check "
                      "it with e2fsck -fn from recovery:\nkernel: EXT4-fs (sda34): INFO: recovery required on "
                      "readonly filesystem\n", result.stdout)
        # A journal ext4 did not ask to replay, the same.
        self.boot_kit()
        self.journal_add("kernel: JBD2: Clearing recovery information on journal\n" + clean)
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL this is the boot after a sleep test's restart", result.stdout)
        # The boot after a sleep test whose root was frozen to the restart.
        self.boot_kit()
        self.journal_add(clean)
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok   the boot after a sleep test's restart: the root's journal was empty at the mount "
                      "(frozen to the restart)\nnote the root's mount: orphan cleanup on readonly fs;mounted "
                      "filesystem 3c5f-uuid ro with ordered data mode. Quota mode: none.\n", result.stdout)
        # The archive of an earlier boot: this boot is not the one after a run.
        shutil.rmtree(archive / "000010-20261122T114720Z-boot-788ce779")
        self.boot_kit()
        self.journal_add(replayed)
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("note the root's journal was replayed at this boot's mount (EXT4-fs (sda34): INFO: recovery "
                      "required on readonly filesystem): what a power loss or a forced restart leaves",
                      result.stdout)


# sleep-test.sh copies the tools it needs from the freeze on to RAM and runs
# them through the copied dynamic loader, so they have to be real
# executables.  The fake kernel's tools are this launcher, under each tool's
# name, which runs the shell stub of that name with the tool's arguments,
# and passes on the file it was run from.
LAUNCHER_C = r'''
#include <libgen.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
int main(int argc, char **argv)
{
	char path[4096], exe[4096];
	char **args = calloc(argc + 2, sizeof(*args));
	ssize_t n = readlink("/proc/self/exe", exe, sizeof(exe) - 1);
	exe[n > 0 ? n : 0] = 0;
	snprintf(path, sizeof(path), "%s/%s", STUB_DIR, basename(strdup(argv[0])));
	setenv("STUB_EXE", exe, 1);
	args[0] = "sh";
	args[1] = path;
	for (int i = 1; i < argc; i++)
		args[i + 1] = argv[i];
	execv("/bin/sh", args);
	perror("execv /bin/sh");
	return 127;
}
'''
# The fake kernel's removal of a PCI device (remove_store ->
# pci_stop_and_remove_bus_device_locked): the device's directory goes, and
# every link in /sys/bus/pci/devices to it or below it.  @TMP@/pci-remove.calls
# notes each call and whether the disk was frozen by then;
# @TMP@/pci-remove-keeps names devices whose link stays (a removal that does
# not empty the bus; the directories stay then too).  It runs in the run's
# RAM part, with the RAM root's tools and the host's rm by its path, and
# removes nothing outside the fake root's sys/devices.
PCI_REMOVE_FAKE = r'''pci_remove() { # NAME (the fake kernel's)
	echo "$1 $([ -e '@TMP@/disk-at-freeze' ] && echo after || echo before)" >> '@TMP@/pci-remove.calls'
	fake_s=-
	read -r fake_s 2>/dev/null < '@ROOT@/sys/module/gcc_sm8550/parameters/debug_pcie0_off' || :
	echo "${fake_s:--}" >> '@TMP@/pcie0-at-pci-remove'
	fake_r=$(readlink -f "$pci_devs/$1") || return 0
	fake_t=$(readlink -f '@ROOT@/sys/devices') || return 0
	case $fake_r in "$fake_t"/?*) ;; *) return 0 ;; esac
	for fake_l in "$pci_devs"/*; do
		fake_q=$(readlink -f "$fake_l") || continue
		case $fake_q/ in "$fake_r"/*) ;; *) continue ;; esac
		! grep -qxF "${fake_l##*/}" '@TMP@/pci-remove-keeps' 2>/dev/null || continue
		/usr/bin/rm -- "$fake_l"
	done
	[ -e '@TMP@/pci-remove-keeps' ] || /usr/bin/rm -r -- "$fake_r"
}
'''
# Each RAM tool notes whether it ran before or after the first freeze, from
# where, and with which PATH.
RAM_CALL = 'echo "$([ -e "$T/disk-at-freeze" ] && echo after || echo before) $STUB_EXE $CALLER_PATH" >> "$T/ram-calls"\n'
# The fake kernel, in the tools the run calls.  F is the fake root, T the
# test's directory.  Every call is noted; what breaks the rules of the run
# goes to $T/violations.  STUB_EXE is the file the tool ran from: the copy in
# the RAM root.  $T/frozen lists the mount points that are frozen.
SLEEP_STUBS = {
    "dmesg": RAM_CALL + 'cat "$F/dev/kmsg"',
    # FIFREEZE and FITHAW on a mount point below /proc/1/root: fails for one
    # named in $T/freeze-fails or $T/thaw-fails, and as the kernel does for
    # a second freeze or a thaw of what is not frozen.  The first freeze
    # notes the disk as it is.  $T/freeze-records-error: an ext4 error on the
    # root while it is frozen and its disk still writable (a reader's, as the
    # root is frozen), which ext4 logs and saves (save_error_info: sysfs's
    # count rises); $T/freeze-warns: a journal handle started on the frozen
    # root (ext4_journal_check_start's warning).
    "fsfreeze": RAM_CALL + r'''echo "$*" >> "$T/fsfreeze.calls"
case $1 in
--version) echo 'fsfreeze from util-linux 2.39.3'; exit 0 ;;
-f|-u) ;;
*) echo "fsfreeze $*" >> "$T/violations"; exit 1 ;;
esac
case $2 in
/proc/1/root/*) mp=${2#/proc/1/root} ;;
*) echo "fsfreeze $* (not through /proc/1/root)" >> "$T/violations"; exit 1 ;;
esac
awk -v m="$mp" '$2 == m { f = 1 } END { exit !f }' "$F/proc/1/mounts" || { echo "fsfreeze: $2: not a mount point" >&2; exit 1; }
touch "$T/frozen"
if [ "$1" = -f ]; then
	# One superblock per source: frozen through any of its mount points.
	src=$(awk -v m="$mp" '$2 == m { s = $1 } END { print s }' "$F/proc/1/mounts")
	if awk -v fz=" $(tr '\n' ' ' < "$T/frozen") " 'index(fz, " " $2 " ") { print $1 }' "$F/proc/1/mounts" | grep -qxF -- "$src"; then
		echo "fsfreeze: $2: freeze failed: Device or resource busy" >&2; exit 1
	fi
	if grep -qxF -- "$mp" "$T/freeze-fails" 2>/dev/null; then echo "fsfreeze: $2: freeze failed: Operation not supported" >&2; exit 1; fi
	if grep -qxF -- "$mp" "$T/freeze-hangs" 2>/dev/null; then /usr/bin/sleep 3; exit 1; fi
	[ -e "$T/disk-at-freeze" ] || { manifest > "$T/disk-at-freeze"; switches > "$T/switches-at-freeze"; }
	echo "$mp" >> "$T/frozen"
	[ ! -e "$T/freeze-records-error" ] || [ "$mp" != / ] || {
		kl 'EXT4-fs error (device sda34): ext4_lookup:1817: inode #393217: comm systemd-journal: iget: checksum invalid'
		echo 1 > "$F/sys/fs/ext4/sda34/errors_count"
		echo ext4_lookup > "$F/sys/fs/ext4/sda34/last_error_func"
		echo 1817 > "$F/sys/fs/ext4/sda34/last_error_line"
	}
	[ ! -e "$T/freeze-warns" ] || [ "$mp" != / ] ||
		kl 'WARNING: fs/ext4/ext4_jbd2.c:78 at ext4_journal_check_start+0x6c/0xa0, CPU#3: kworker/u16:2/1234'
	exit 0
fi
grep -qxF -- "$mp" "$T/frozen" || { echo "fsfreeze: $2: unfreeze failed: Invalid argument" >&2; exit 1; }
if grep -qxF -- "$mp" "$T/thaw-fails" 2>/dev/null; then echo "fsfreeze: $2: unfreeze failed: Input/output error" >&2; exit 1; fi
grep -vxF -- "$mp" "$T/frozen" > "$T/frozen.new"
mv "$T/frozen.new" "$T/frozen"''',
    # A sleep: the rules checked at its start, then cycle N's scenario in
    # $T/cycle-N (log, rc, out, wakeup-irq, suspend-failed,
    # no-aosd/no-cxsd/no-ddr, storage-dead, other-data, new-mount,
    # new-mount-unfreezable, thawed: a mount point the sleep thawed; and
    # ext4's errors, logged as fs/ext4 and fs/jbd2 log them: ext4-error, an
    # error on the root, handled as ext4_handle_error does with the
    # behaviour the root runs with in the fake mount table; journal-abort,
    # jbd2 aborting the root's journal on its own (a forced read-only that
    # no errors= changes) and ext4 following it, jsb-fails with jbd2's write
    # of the error failing; stick-error, an error on the stick, whose disk
    # is writable: saved, its count rises).
    "rtcwake": RAM_CALL + r'''echo "$*" >> "$T/rtcwake.calls"
case $* in *--version*) exit 0 ;; esac
n=$(grep -cE -- '-m (mem|freeze)' "$T/rtcwake.calls")
# rtcwake -m freeze (mode=s2idle): suspend-to-idle, which mem_sleep plays no
# part in (the stand-in notes what it held at each such sleep).
case $* in *"-m freeze "*) frz=1 ;; *) frz= ;; esac
fz=$(awk -v fz=" $(tr '\n' ' ' < "$T/frozen" 2>/dev/null) " 'index(fz, " " $2 " ") {print $1}' "$F/proc/1/mounts" | tr '\n' ' ')
w=$(awk -v fz=" $fz " '$1 ~ /^\/dev\// && $3 !~ /^fuse/ && $4 !~ /^ro(,|$)/ && index(fz, " " $1 " ") == 0 {print $2}' "$F/proc/1/mounts")
[ -z "$w" ] || echo "cycle $n slept with $w writable and not frozen" >> "$T/violations"
awk 'NR > 1 && $1 !~ /^\/dev\/zram/' "$F/proc/swaps" | grep -q . && echo "cycle $n slept with swap on the disk" >> "$T/violations"
[ "$(cat "$F/sys/class/block/sda/ro")" = 1 ] || echo "cycle $n slept with sda writable at the block layer" >> "$T/violations"
grep -q u "$F/proc/sysrq-trigger" && echo "cycle $n slept after a sysrq u" >> "$T/violations"
# The kernel shows "s2idle [deep]" once "deep" is written; the stand-in, what was written.
if [ -n "$frz" ]; then cat "$F/sys/power/mem_sleep" >> "$T/mem-sleep-at-freeze"
else case $(cat "$F/sys/power/mem_sleep") in *'[deep]'*|deep) ;; *) echo "cycle $n slept with mem_sleep $(cat "$F/sys/power/mem_sleep")" >> "$T/violations" ;; esac; fi
case $* in "-u -d rtc0 -m mem -s "*|"-u -d rtc0 -m freeze -s "*) ;; *) echo "rtcwake $*" >> "$T/violations" ;; esac
# The experiment's knobs as this sleep finds them (pm_test, the console
# loglevel, console_suspend), and the last line in pmsg, one line each.
echo "pm_test=$(cat "$F/sys/power/pm_test") printk=$(tr '\t' ' ' < "$F/proc/sys/kernel/printk") console_suspend=$(cat "$F/sys/module/printk/parameters/console_suspend")" >> "$T/knobs-at-sleep"
{ tail -n 1 "$F/dev/pmsg0" 2>/dev/null || :; } >> "$T/pmsg-at-sleep"
# Revision 9's debug switches as this sleep finds them.
switches >> "$T/switches-at-sleep"
# A level written to pm_test (the stand-in keeps what was written): the
# suspend stops there and nothing sleeps.
case $(cat "$F/sys/power/pm_test") in freezer|devices|platform|processors|core) pt=1 ;; *) pt= ;; esac
s=$T/cycle-$n
if [ -e "$s/log" ]; then cat "$s/log" >> "$F/dev/kmsg"
elif [ -n "$pt" ] && [ -n "$frz" ]; then printf '[  700.000000] PM: suspend entry (s2idle)\n[  700.200000] PM: suspend debug: Waiting for 5 second(s).\n[  705.300000] PM: suspend exit\n' >> "$F/dev/kmsg"
elif [ -n "$pt" ]; then printf '[  700.000000] PM: suspend entry (deep)\n[  700.200000] suspend debug: Waiting for 5 second(s).\n[  705.300000] PM: suspend exit\n' >> "$F/dev/kmsg"
elif [ -n "$frz" ]; then printf '[  700.000000] PM: suspend entry (s2idle)\n[  700.100000] PM: suspend-to-idle\n[  730.000000] Timekeeping suspended for 30.002 seconds\n[  730.000100] PM: Triggering wakeup from IRQ 21\n[  730.000200] PM: resume from suspend-to-idle\n[  730.100000] PM: suspend exit\n' >> "$F/dev/kmsg"
else printf '[  700.000000] PM: suspend entry (deep)\n[  730.000000] Timekeeping suspended for 30.002 seconds\n[  730.100000] PM: suspend exit\n' >> "$F/dev/kmsg"; fi
# Suspend-to-idle: each CPU's s2idle state entered once (cycle flag
# no-s2idle: none), and the cluster's domain taken down to S1 (flags
# no-cluster: not; cluster-rejected: refused).
if [ -n "$frz" ] && [ -z "$pt" ] && [ ! -e "$s/no-s2idle" ]; then
	for u in "$F"/sys/devices/system/cpu/cpu*/cpuidle/state*/s2idle; do
		[ -d "$u" ] || continue
		echo $(($(cat "$u/usage") + 1)) > "$u/usage"
		echo $(($(cat "$u/time") + 1500)) > "$u/time"
	done
fi
cl=$F/sys/kernel/debug/pm_genpd/power-domain-cluster/idle_states
if [ -n "$frz" ] && [ -z "$pt" ] && [ -e "$cl" ] && [ ! -e "$s/no-cluster" ]; then
	if [ -e "$s/cluster-rejected" ]; then c=4; else c=3; fi
	awk -v c="$c" '$1 == "S1" { $c += 1; if (c == 3) $7 += 1 } { print }' "$cl" > "$T/cl.new" && mv "$T/cl.new" "$cl"
fi
if [ -e "$s/suspend-failed" ]; then k=fail; else k=success; fi
echo $(($(cat "$F/sys/power/suspend_stats/$k") + 1)) > "$F/sys/power/suspend_stats/$k"
for q in aosd cxsd ddr; do
	[ -z "$pt" ] && [ ! -e "$s/no-$q" ] && [ -e "$F/sys/kernel/debug/qcom_stats/$q" ] || continue
	sed -i "s/^Count: \(.*\)/echo Count: \$((\1 + 1))/e" "$F/sys/kernel/debug/qcom_stats/$q"
done
# What the sleep left in the files the evidence reads, if the cycle says.
[ ! -d "$s/qcom_stats" ] || cp "$s/qcom_stats/"* "$F/sys/kernel/debug/qcom_stats/"
[ ! -e "$s/interrupts" ] || cp "$s/interrupts" "$F/proc/interrupts"
[ ! -e "$s/wakeup_sources" ] || cp "$s/wakeup_sources" "$F/sys/kernel/debug/wakeup_sources"
[ ! -d "$s/clk" ] || cp -R "$s/clk/." "$F/sys/kernel/debug/clk/"
[ ! -d "$s/genpd" ] || cp -R "$s/genpd/." "$F/sys/kernel/debug/pm_genpd/"
# Revision 11's rpmh: the tracepoint's switch and the trace's size as this
# sleep finds them, then what the cycle says rpmh-rsc wrote.
ev=$F/sys/kernel/tracing/events/rpmh/rpmh_send_msg/enable
[ ! -e "$ev" ] || echo "enable $(cat "$ev") trace $(wc -c < "$F/sys/kernel/tracing/trace")" >> "$T/rpmh-at-sleep"
[ ! -e "$s/trace" ] || cat "$s/trace" >> "$F/sys/kernel/tracing/trace"
# pm_wakeup_irq: the cycle's, or the RTC's.
# (After a pm_test cycle nothing woke it: no data, which the stand-in
# shows as an empty file.)
if [ -e "$s/wakeup-irq" ]; then cat "$s/wakeup-irq" > "$F/sys/power/pm_wakeup_irq"
elif [ -n "$pt" ]; then : > "$F/sys/power/pm_wakeup_irq"
else echo 21 > "$F/sys/power/pm_wakeup_irq"; fi
[ ! -e "$s/storage-dead" ] || touch "$T/storage-dead"
[ ! -e "$s/other-data" ] || touch "$T/other-data"
[ ! -e "$s/new-mount" ] || echo '/dev/sdh1 /media/stick2 ext4 rw,relatime 0 0' >> "$F/proc/1/mounts"
[ ! -e "$s/new-mount-unfreezable" ] || echo /media/stick2 >> "$T/freeze-fails"
if [ -e "$s/thawed" ]; then
	grep -vxF -- "$(cat "$s/thawed")" "$T/frozen" > "$T/frozen.new"
	mv "$T/frozen.new" "$T/frozen"
fi
if [ -e "$s/ext4-error" ]; then
	kl 'EXT4-fs error (device sda34): __ext4_find_entry:1622: inode #2: comm systemd: reading directory lblock 0'
	if [ "$(mode_of /dev/sda34)" != continue ]; then
		kl 'Aborting journal on device sda34-8.'
		kl 'Trying to write to read-only block-device sda34'
		kl 'EXT4-fs (sda34): Remounting filesystem read-only'
		add_opt /dev/sda34 emergency_ro
	fi
fi
if [ -e "$s/journal-abort" ]; then
	kl 'Aborting journal on device sda34-8.'
	kl 'Trying to write to read-only block-device sda34'
	[ ! -e "$s/jsb-fails" ] || kl 'JBD2: I/O error when updating journal superblock for sda34-8.'
	kl 'EXT4-fs error (device sda34): ext4_journal_check_start:86: comm kworker/u16:3: Detected aborted journal'
	kl 'EXT4-fs (sda34): Remounting filesystem read-only'
	add_opt /dev/sda34 emergency_ro
fi
if [ -e "$s/stick-error" ]; then
	kl 'EXT4-fs error (device sdg2): __ext4_find_entry:1622: inode #2: comm gvfsd-trash: reading directory lblock 0'
	echo $(($(cat "$F/sys/fs/ext4/sdg2/errors_count") + 1)) > "$F/sys/fs/ext4/sdg2/errors_count"
	echo __ext4_find_entry > "$F/sys/fs/ext4/sdg2/last_error_func"
	echo 1622 > "$F/sys/fs/ext4/sdg2/last_error_line"
fi
[ ! -e "$s/out" ] || cat "$s/out"
exit "$(cat "$s/rc" 2>/dev/null || echo 0)"''',
    "dd": RAM_CALL + r'''echo "$*" >> "$T/dd.calls"
inf=
for a; do
	case $a in
	if=*) inf=${a#if=} ;;
	of=/dev/null) ;;
	of=*|oflag=*|conv=*|seek=*) echo "dd $*" >> "$T/violations" ;;
	esac
done
case $inf in
/dev/zero) exit 0 ;;
"$F/dev/disk/by-partlabel/userdata")
	case " $* " in *" iflag=direct "*) ;; *) echo "dd read userdata without O_DIRECT: $*" >> "$T/violations" ;; esac
	if [ -e "$T/storage-dead" ]; then
		echo "dd: error reading '$inf': Input/output error" >&2
		exit 1
	fi
	cat "$F/dev/sda34"
	[ ! -e "$T/other-data" ] || echo changed
	exit 0 ;;
esac
echo "dd $*" >> "$T/violations"
exit 1''',
    "blockdev": RAM_CALL + r'''echo "$*" >> "$T/blockdev.calls"
case $1 in
--setro)
	[ ! -e "$T/setro-fails" ] || exit 1
	echo 1 > "$F/sys/class/block/${2#/dev/}/ro" ;;
--setrw) echo 0 > "$F/sys/class/block/${2#/dev/}/ro" ;;
--getro) cat "$F/sys/class/block/${2#/dev/}/ro" ;;
*) echo "blockdev $*" >> "$T/violations"; exit 1 ;;
esac''',
    "sync": RAM_CALL + 'echo "sync $*" >> "$T/sync.calls"',
    # $T/abort-at-hold: jbd2 aborts the root's journal during the hold, once
    # the run's RESULT line is out (the hold's sleep 1).
    "sleep": RAM_CALL + r'''if [ -e "$T/abort-at-hold" ] && [ "${1:-}" = 1 ] &&
	grep -qs '^SLEEP-TEST RESULT: ' "$F"/run/gts9u-sleep-test/latest/result; then
	rm "$T/abort-at-hold"
	kl 'Aborting journal on device sda34-8.'
	kl 'EXT4-fs (sda34): Remounting filesystem read-only'
	add_opt /dev/sda34 emergency_ro
fi
exit 0''',
    # setns into PID 1's mount namespace, then the command: here it runs in
    # the test's own (mount is a stub).
    "nsenter": RAM_CALL + r'''echo "$*" >> "$T/nsenter.calls"
case $1 in --version) echo 'nsenter from util-linux 2.39.3'; exit 0 ;; esac
[ "$1 $2 $3 $4" = "-t 1 -m --" ] || { echo "nsenter $*" >> "$T/violations"; exit 1; }
shift 4
exec "$@"''',
}
# The tools of the disk part of the run, as plain scripts in PATH.
SLEEP_PLAIN_STUBS = {
    # The tmpfs of the run's RAM directory, and the remounts of errors= as
    # util-linux 2.39.3 makes them with a mount point alone: the options of
    # fstab or of the mount table first, -o's after, so that the last
    # errors= is the one ext4 keeps; then the fake kernel shows errors= only
    # when it differs from the superblock's default (_ext4_show_options).
    # $T/remount-fails names the errors= words (or "WORD MOUNTPOINT") for
    # which the remount fails, as mount(8) says it, status 32;
    # $T/remount-ignored those for which it goes through and changes
    # nothing.  Never while anything is frozen.
    "mount": r'''echo "$*" >> "$T/mount.calls"
case $1 in
-t) exit 0 ;;
-o) ;;
*) echo "mount $*" >> "$T/violations"; exit 1 ;;
esac
[ ! -s "$T/frozen" ] || echo "mount $* with $(tr '\n' ' ' < "$T/frozen")frozen" >> "$T/violations"
case $2 in
remount,errors=continue|remount,errors=remount-ro|remount,errors=panic) want=${2#remount,errors=} ;;
*) echo "mount $*" >> "$T/violations"; exit 1 ;;
esac
[ $# = 3 ] || { echo "mount $* (not a mount point alone)" >> "$T/violations"; exit 1; }
src=$(awk -v m="$3" '$2 == m { s = $1 } END { print s }' "$F/proc/1/mounts")
if [ -z "$src" ] || grep -qxF -e "$want" -e "$want $3" "$T/remount-fails" 2>/dev/null; then
	echo "mount: $3: mount point not mounted or bad option." >&2
	echo "       dmesg(1) may have more information after failed mount system call." >&2
	exit 32
fi
! grep -qxF -- "$want" "$T/remount-ignored" 2>/dev/null || exit 0
word=
[ "$want" = "$(sb_default "${src##*/}")" ] || word=errors=$want
# errors= before emergency_ro and shutdown, as _ext4_show_options prints them.
awk -v s="$src" -v w="$word" '$1 == s {
	n = split($4, o, ","); r = ""; late = ""
	for (i = 1; i <= n; i++)
		if (o[i] ~ /^(emergency_ro|shutdown)$/) late = late "," o[i]
		else if (o[i] !~ /^errors=/) r = r (r == "" ? "" : ",") o[i]
	if (w != "") r = r "," w
	$4 = r late
} { print }' "$F/proc/1/mounts" > "$T/mounts.new" && mv "$T/mounts.new" "$F/proc/1/mounts"''',
    "setsid": '[ ! -e "$T/setsid-fails" ] || exit 125\nexec /usr/bin/setsid "$@"',
    # Never run here: in_ram_root is replaced (RamRootTest runs the real one).
    "chroot": 'echo "chroot $*" >> "$T/violations"; exit 1',
    # $T/error-at-swapoff: an ext4 error on the root in the minutes between
    # the checks and the first freeze (here while swap goes off), saved, as
    # its disk is writable; $T/abort-at-swapoff: the same error with ext4
    # giving up on the root, as it does with remount-ro, its behaviour then.
    "swapoff": '''echo "$*" >> "$T/swapoff.calls"
[ ! -e "$T/swapoff-fails" ] || exit 1
[ ! -e "$T/lock-switch-at-swapoff" ] || chmod 0444 "$F/sys/module/$(cat "$T/lock-switch-at-swapoff")"
awk -v s="$1" '$1 != s' "$F/proc/swaps" > "$T/swaps.new" && mv "$T/swaps.new" "$F/proc/swaps"
if [ -e "$T/error-at-swapoff" ] || [ -e "$T/abort-at-swapoff" ]; then
	kl 'EXT4-fs error (device sda34): ext4_lookup:1817: inode #393217: comm tracker-miner-f: iget: checksum invalid'
	echo 1 > "$F/sys/fs/ext4/sda34/errors_count"
	echo ext4_lookup > "$F/sys/fs/ext4/sda34/last_error_func"
	echo 1817 > "$F/sys/fs/ext4/sda34/last_error_line"
fi
if [ -e "$T/abort-at-swapoff" ]; then
	kl 'Aborting journal on device sda34-8.'
	kl 'EXT4-fs (sda34): Remounting filesystem read-only'
	add_opt /dev/sda34 emergency_ro
fi''',
    "systemctl": r'''echo "$*" >> "$T/systemctl.calls"
case $1 in
is-active)
	[ "${2:-}" != --quiet ] || shift
	if [ -e "$T/active-$2" ]; then cat "$T/active-$2"; exit 0; fi
	echo inactive; exit 3 ;;
# sensors=stop's: each unit inactive after it, but one named in
# $T/stop-ignored (still running, status 1).
stop)
	shift
	rc=0
	for u; do
		if grep -qxF -- "$u" "$T/stop-ignored" 2>/dev/null; then rc=1; else rm -f "$T/active-$u"; fi
	done
	exit "$rc" ;;
restart)
	[ "$2" = ubuntu-gts9u-icc-late-release.service ] || exit 1
	if [ -e "$T/release-fails" ]; then
		printf 'result=held\nreason=the panel cycle did not succeed\n' > "$F/run/ubuntu-gts9u/icc-release"
		exit 0
	fi
	for p in 24100000.interconnect interconnect-1; do
		echo 1 > "$F/sys/bus/platform/devices/$p/state_synced"
	done
	printf 'boot_id=%s\nuptime=600\n' "$(cat "$F/proc/sys/kernel/random/boot_id")" \
		> "$F/var/lib/ubuntu-gts9u/icc-release.pending"
	printf 'result=released\nreason=restarted\n' > "$F/run/ubuntu-gts9u/icc-release"
	echo 'releasing: 24100000.interconnect interconnect-1' >> "$F/var/log/ubuntu-gts9u-icc-release.log" ;;
esac''',
    # A superblock's header: its state from $T/fs-state, clean unless a test
    # says otherwise, and its default error behaviour (sb_default), as
    # e2fsprogs 1.47.0 prints them (lib/e2p/ls.c).
    "dumpe2fs": '''echo "$*" >> "$T/dumpe2fs.calls"
echo 'dumpe2fs 1.47.0 (5-Feb-2023)' >&2
[ "$1" = -h ] || exit 1
echo 'Filesystem volume name:   UBTS9_UFS'
echo "Filesystem state:         $(cat "$T/fs-state" 2>/dev/null || echo clean)"
case $(sb_default "${2##*/}") in
continue) echo 'Errors behavior:          Continue' ;;
remount-ro) echo 'Errors behavior:          Remount read-only' ;;
panic) echo 'Errors behavior:          Panic' ;;
*) echo 'Errors behavior:          Unknown (continue)' ;;
esac''',
}
# The disk as the run must leave it after the first freeze: everything but
# /run, /proc, /sys and /dev of the fake root, and the kit with its logs.
# And the fake kernel's ext4: a line in its log (kl), a superblock's default
# error behaviour ($T/sb-errors-NAME, remount-ro if there is none), the one
# a filesystem runs with (its errors= in the mount table, else that
# default), and an option ext4 adds to its mount (emergency_ro).
MANIFEST = r'''manifest() {
	(cd "$F" && find . -path ./run -prune -o -path ./proc -prune -o -path ./sys -prune \
		-o -path ./dev -prune -o -type f -exec sha256sum {} +) | sort
	(cd "$T/kit" && find . -type f -exec sha256sum {} +) | sed 's|  \./|  kit/|' | sort
}
kl() { printf '[  731.500000] %s\n' "$*" >> "$F/dev/kmsg"; }
# Revision 9's debug switches, pcie0 pcie0phy usb cxmx: each value, "-" for
# one the fake kernel does not offer; then revision 11's root port switch,
# rpd3, on a fake kernel that offers it.
switches() {
	for s in gcc_sm8550/parameters/debug_pcie0_off gcc_sm8550/parameters/debug_pcie0_phy_off \
		gcc_sm8550/parameters/debug_usb_off rpmhpd/parameters/debug_ret_floor; do
		printf '%s ' "$(cat "$F/sys/module/$s" 2>/dev/null || echo -)"
	done
	[ ! -e "$F/sys/module/pcie_qcom/parameters/debug_root_port_d3" ] ||
		printf '%s ' "$(cat "$F/sys/module/pcie_qcom/parameters/debug_root_port_d3")"
	[ ! -e "$F/sys/module/qcom_stats/parameters/debug_cxpc_violators" ] ||
		printf '%s ' "$(cat "$F/sys/module/qcom_stats/parameters/debug_cxpc_violators")"
	echo
}
sb_default() { cat "$T/sb-errors-$1" 2>/dev/null || echo remount-ro; }
mode_of() {
	m=$(awk -v s="$1" '$1 == s { n = split($4, o, ","); for (i = 1; i <= n; i++) if (o[i] ~ /^errors=/) { sub(/^errors=/, "", o[i]); print o[i] }; exit }' "$F/proc/1/mounts")
	[ -n "$m" ] || m=$(sb_default "${1##*/}")
	echo "$m"
}
add_opt() {
	awk -v s="$1" -v o="$2" '$1 == s { $4 = $4 "," o } { print }' "$F/proc/1/mounts" > "$T/mounts.new" &&
		mv "$T/mounts.new" "$F/proc/1/mounts"
}
'''
# The 2026-11-22 03:29 attempt on squirtle (kit revision 7's kernel, docked
# and powered): the touchscreen's double tap came in the noirq phase, and
# the kernel gave the suspend up before the tablet slept.  The kernel lines
# that tell it, from that run's kmsg-1
# (~/ubuntu-gts9u/backups/squirtle-sleep-r7-baseline-20261122-0328/run),
# and rtcwake's output.
TOUCH_ABORT = """\
[13851.674165] PM: suspend entry (deep)
[13858.770514] PM: suspend of devices complete after 186.566 msecs
[13858.980061] PM: late suspend of devices complete after 209.497 msecs
[13858.996988] PM: Triggering wakeup from IRQ 199
[13859.007192] PM: noirq suspend of devices complete after 26.474 msecs
[13859.007458] Disabling non-boot CPUs ...
[13859.007463] Wakeup pending. Abort CPU freeze
[13859.007466] Non-boot CPUs are not disabled
[13859.031732] PM: noirq resume of devices complete after 24.259 msecs
[13859.037881] samsung_fts1ba90a 9-0049: double tap to wake
[13860.845153] PM: resume of devices complete after 631.666 msecs
[13861.416262] PM: suspend exit
"""
TOUCH_ABORT_OUT = 'rtcwake: wakeup from "mem" using rtc0 at Thu Mar  5 05:27:35 1970\nrtcwake: write error\n'
TOUCH_HINT = "IRQ 199 (fts1ba90a, the touchscreen: do not touch the screen during the run)"
# /proc/interrupts: IRQ 199 as squirtle showed it (its counts made up here),
# and made-up lines for the rest: the RTC's, a named one that is not the
# touchscreen, a shared one, one with a flow handler's name, and one with no
# action.
INTERRUPTS = (
    "           CPU0       CPU1       CPU2       CPU3       CPU4       CPU5       CPU6       CPU7       \n"
    " 21:          4          0          0          0          0          0          0          0  pmic_arb 1 Edge      pm8xxx_rtc_alarm\n"
    " 37:          2          0          0          0          0          0          0          0  pmic_arb 2 Edge      pm8550_pwrkey\n"
    " 40:          0          0          0          0          0          0          0          0     GICv3  72 Level     foo, bar\n"
    " 41:          0          0          0          0          0          0          0          0     GICv3  73 Level   -edge      baz\n"
    "199:          9          0          0          0          0          0          0          0   msmgpio  25 Level     fts1ba90a\n"
    "251:          0          0          0          0          0          0          0          0     GICv3 300 Level   \n"
)
# A journal handle started on a frozen ext4: ext4_journal_check_start's
# WARN_ON (fs/ext4/ext4_jbd2.c:78) in 7.2's format (__warn, kernel/panic.c).
JOURNAL_WARN = "WARNING: fs/ext4/ext4_jbd2.c:78 at ext4_journal_check_start+0x6c/0xa0, CPU#3: kworker/u16:2/1234"
# What the first resume with a filesystem frozen from userspace logs, with
# freeze_filesystems 0: the suspend's own thaw (filesystems_thaw) finds no
# freeze of its own, WARN_ON_ONCE in may_unfreeze (fs/super.c:1919),
# inlined in thaw_super_locked.  Made from the source; not seen on a
# tablet.  Its module list names the UFS driver, and modules whose names
# say "fail" (net_failover and failover are real ones): a list, not a
# storage line.
SUPER_WARN = """\
[  730.050000] ------------[ cut here ]------------
[  730.050001] WARNING: fs/super.c:1919 at thaw_super_locked+0x1a8/0x1f0, CPU#4: rtcwake/4321
[  730.050002] Modules linked in: snd_soc_wcd938x qcom_pd_mapper ufs_qcom phy_qcom_qmp_ufs net_failover failover
[  730.050003] CPU: 4 UID: 0 PID: 4321 Comm: rtcwake Not tainted 7.2.0-rc3-dirty #8 PREEMPT
[  730.050004] Hardware name: Samsung Galaxy Tab S9 (SM-X710) (DT)
[  730.050005] pc : thaw_super_locked+0x1a8/0x1f0
[  730.050006] lr : thaw_super_locked+0x40/0x1f0
[  730.050007] Call trace:
[  730.050008]  thaw_super_locked+0x1a8/0x1f0 (P)
[  730.050009]  thaw_super+0x2c/0x40
[  730.050010]  filesystems_thaw_callback+0x5c/0x80
[  730.050011]  __iterate_supers+0xc4/0x120
[  730.050012]  filesystems_thaw+0x20/0x30
[  730.050013]  suspend_finish+0x24/0x40
[  730.050014]  enter_state+0x2a0/0x3c0
[  730.050015]  pm_suspend+0x5c/0xd0
[  730.050016] ---[ end trace 0000000000000000 ]---
"""
# A suspend whose task freezer gave up after its 20 s (try_to_freeze_tasks,
# kernel/power/process.c): the tasks that did not freeze, each named by
# sched_show_task, then the tasks restarted (freeze_processes), the
# suspend's own thaw of the filesystems (its warning) and the end.  A
# hung-task report before it names a task the same way, and is not the
# freezer's.  Made from the source; not seen on a tablet.
FREEZER_FAIL = """\
[  690.000000] INFO: task gsd-housekeepin:2601 blocked for more than 61 seconds.
[  690.000001] task:gsd-housekeepin state:D stack:0     pid:2601  tgid:2601  ppid:2287   task_flags:0x400000 flags:0x00000008
[  700.000000] PM: suspend entry (deep)
[  700.000100] Filesystems sync: 0.002 seconds
[  700.000200] Freezing user space processes
[  720.003000] Freezing user space processes failed after 20.003 seconds (2 tasks refusing to freeze, wq_busy=0):
[  720.003100] task:gnome-shell     state:D stack:0     pid:2412  tgid:2412  ppid:2287   task_flags:0x400100 flags:0x00000009
[  720.003200] Call trace:
[  720.003300]  __switch_to+0x100/0x1c0 (T)
[  720.003400]  __schedule+0x2f0/0x7a0
[  720.003500]  rwsem_down_read_slowpath+0x1d8/0x5c0
[  720.003600]  lock_mm_and_find_vma+0x88/0x1a0
[  720.003700] task:pool-gnome-shel state:D stack:0     pid:2431  tgid:2412  ppid:2287   task_flags:0x400040 flags:0x00000009
[  720.003800] Call trace:
[  720.003900]  __switch_to+0x100/0x1c0 (T)
[  720.004000] OOM killer enabled.
[  720.004100] Restarting tasks: Starting
[  720.004200] Restarting tasks: Done
""" + SUPER_WARN.replace("[  730.", "[  720.1") + """\
[  720.200000] PM: suspend exit
"""
FREEZER_LINE = ("Freezing user space processes failed after 20.003 seconds (2 tasks refusing to freeze, "
                "wq_busy=0):")
# What sleep-test.sh's evidence reads, in the forms 7.2 prints them, with
# made-up values: pm_genpd_summary (genpd_summary_one), regulator_summary
# (regulator_summary_show_subtree), interconnect_summary (icc_summary_show),
# wakeup_sources (print_wakeup_source_stats) and qcom_stats
# (qcom_print_stats, qcom_ddr_stats_print).
GENPD_SUMMARY = (
    "domain                          status          children        performance\n"
    "    /device                         runtime status                  managed by\n"
    "------------------------------------------------------------------------------\n"
    "cx                              on                              0\n"
    "                                                mmcx, ufs_phy_gdsc\n"
    "    9a0000.i2c                      active                      0           SW\n"
    "ufs_phy_gdsc                    off-0                           0\n"
    "    1d84000.ufshc                   suspended                   0           SW\n"
    "gpu_cc_cx_gdsc                  off-0                           0\n"
    "    3d64000.gmu                     suspended                   0           SW\n")
REGULATOR_SUMMARY = (
    " regulator                      use open bypass  opmode voltage current     min     max\n"
    "---------------------------------------------------------------------------------------\n"
    " regulator-dummy                  0    0      0 unknown     0mV     0mA     0mV     0mV\n"
    " vreg_l1e_0p88                    2    3      0    fast   880mV     0mA   880mV   880mV\n"
    "    1d80000.phy-vdda-phy          1                                 0mA     0mV     0mV\n"
    "    88e8000.phy-vdda-phy          1                                 0mA     0mV     0mV\n"
    "    ae94000.dsi-vdda              0                                 0mA     0mV     0mV\n")
ICC_SUMMARY = (
    " node                                  tag          avg         peak\n"
    "--------------------------------------------------------------------\n"
    "qhm_qup1                                          0            0\n"
    "ebi                                           76800       200000\n"
    "  1c00000.pcie                           0        76800       200000\n"
    "  3d00000.gpu                            3            0            0\n"
    "  cpu0                                   3         1000         2000\n")


# The name of each SM-X710 CPU's state with an s2idle entry (its PSCI
# domain's domain-idle-states: cpu-sleep-0-0 on the little cores, 1-0 on
# the big ones, 2-0 on the prime one).
CPU_SLEEP = ["cpu-sleep-0-0"] * 3 + ["cpu-sleep-1-0"] * 4 + ["cpu-sleep-2-0"]
# /sys/kernel/debug/pm_genpd/power-domain-cluster/idle_states as 7.2 prints
# it (idle_states_show), with made-up values.
CLUSTER_STATES = ("State  Time(ms)       Usage      Rejected   Above      Below      S2idle\n"
                  "S0     1200           10         0          0          0          0\n"
                  "S1     54000          5          0          0          0          0\n")


def wakeup_sources(*rows):
    """/sys/kernel/debug/wakeup_sources with ROWS of (name, events, wakeups)."""
    lines = ["name\t\tactive_count\tevent_count\twakeup_count\texpire_count\tactive_since\ttotal_time\t"
             "max_time\tlast_change\tprevent_suspend_time"]
    lines += [f"{name:<12s}\t{events}\t\t{events}\t\t{wakeups}\t\t0\t\t0\t\t0\t\t0\t\t1000\t\t0"
              for name, events, wakeups in rows]
    return "\n".join(lines) + "\n"


def qcom_stat(count, seconds):
    """A qcom_stats state's file: COUNT entries, SECONDS spent in it."""
    return (f"Count: {count}\nLast Entered At: 0\nLast Exited At: 0\n"
            f"Accumulated Duration: {int(seconds * 19200000)}\n")


# The clocks the fake debugfs has, each with its parent's name (None: no
# clk_parent file, a clock without a parent): XO and its AO peer, the UFS
# host's five, a PLL under XO for them, and a clock that is no UFS host's;
# and the UFS PHY's own two (ref_aux and qref), off unless a test says.
CLK_PARENTS = {"xo_board": None, "bi_tcxo": "xo_board", "bi_tcxo_ao": "xo_board",
               "tcsr_ufs_pad_clkref_en": "bi_tcxo", "gcc_gpll0": "bi_tcxo",
               "gcc_ufs_phy_ahb_clk": None, "gcc_ufs_phy_axi_clk": "gcc_gpll0",
               "gcc_ufs_phy_unipro_core_clk": "gcc_gpll0", "gcc_ufs_phy_ice_core_clk": "gcc_gpll0",
               "gcc_usb30_prim_master_clk": "gcc_gpll0",
               "gcc_ufs_phy_phy_aux_clk": "bi_tcxo", "tcsr_ufs_clkref_en": "bi_tcxo"}


def write_clocks(directory, **counts):
    """/sys/kernel/debug/clk as 7.2 makes it (clk_debug_create_one): a
    directory per clock with its enable and prepare counts, and its parent's
    name for a clock that has one.  COUNTS gives a clock's (enable, prepare);
    the others read 0 and 0."""
    for name, parent in CLK_PARENTS.items():
        d = directory / name
        d.mkdir(parents=True, exist_ok=True)
        enable, prepare = counts.get(name, (0, 0))
        (d / "clk_enable_count").write_text(f"{enable}\n")
        (d / "clk_prepare_count").write_text(f"{prepare}\n")
        if parent:
            (d / "clk_parent").write_text(parent + "\n")


# squirtle's counts on 2026-11-23 (backups/squirtle-clk-ufs-20261123.txt,
# its second read), the lane clock leak of the kernels up to revision 9.
CLK_LEAK = {"bi_tcxo": (6, 6), "tcsr_ufs_pad_clkref_en": (522, 522), "gcc_ufs_phy_axi_clk": (522, 522),
            "gcc_ufs_phy_ahb_clk": (523, 523), "gcc_ufs_phy_unipro_core_clk": (522, 522),
            "gcc_ufs_phy_ice_core_clk": (523, 523)}


def ddr_stats(seconds_at_547):
    """qcom_stats/ddr_stats: two low-power modes, two frequencies."""
    return ("DDR LPM Stat Name:0xd4\tcount:0\tDuration (ticks):0\n"
            "DDR LPM Stat Name:0xd0\tcount:2\tDuration (ticks):100\n"
            f"DDR Freq 547Mhz:\tCP IDX:0\tcount:5\tDuration (ticks):{int(seconds_at_547 * 19200000)}\n"
            "DDR Freq 2092Mhz:\tCP IDX:5\tcount:9\tDuration (ticks):384000000\n")


def rpmh_line(ts, state, addr, data):
    """A line of the trace for one command rpmh-rsc wrote (rpmh_send_msg,
    drivers/soc/qcom/trace-rpmh.h)."""
    return (f"          <idle>-0       [000] d..2. {ts:12.6f}: rpmh_send_msg: apps_rsc: tcs(m): 2 [{state}] "
            f"cmd(n): 0 msgid: 0x10108 addr: {addr} data: {data} complete: 0\n")


def clk_line(ts, event, name):
    """A line of the trace for a clock's prepare or unprepare (clk_prepare,
    clk_unprepare, include/trace/events/clk.h)."""
    return f"     kworker/0:1-12      [000] ...1. {ts:12.6f}: {event}: {name}\n"


# The command DB as its debugfs file dumps it (made-up addresses).
CMD_DB = ("Command DB DUMP\nSlave ARC (v1.0)\n-------------------------\n0x30000: cx.lvl [00 01]\n0x30010: mx.lvl\n"
          "0x30030: xo.lvl\nSlave BCM (v1.0)\n-------------------------\n0x50000: MC0\n0x50004: SH0\n"
          "Slave VRM (v1.0)\n-------------------------\n0x40100: ldob1\n")


class SleepTestTest(FakeTablet):
    """sleep-test.sh against a fake SM-X710: the fake kernel's tools are
    stubs (SLEEP_STUBS) that the run copies to its RAM directory with the
    host's dynamic loader and libraries, as it copies the real ones on the
    tablet; grep, awk, sha256sum, tail and the shell are the host's own."""

    def setUp(self):
        super().setUp()
        if not shutil.which("cc"):
            self.skipTest("no C compiler for the stub launcher")
        F, T = self.root, self.tmp
        # No host tool from /usr/sbin (rtcwake, blockdev, swapoff, chroot,
        # fsfreeze, dumpe2fs): a tool the stubs do not provide is missing,
        # not the host's.
        self.host_path = "/usr/bin:/bin"
        stubs = T / "stubs"
        stubs.mkdir()
        launcher = T / "launcher"
        (T / "launcher.c").write_text(LAUNCHER_C)
        subprocess.run(["cc", "-O1", f'-DSTUB_DIR="{stubs}"', "-o", str(launcher), str(T / "launcher.c")],
                       check=True)
        prologue = f"CALLER_PATH=$PATH\nexport PATH=/usr/bin:/bin\nF={F}\nT={T}\n{MANIFEST}"
        for name, body in SLEEP_STUBS.items():
            (stubs / name).write_text(prologue + body + "\n")
            (self.bin / name).unlink(missing_ok=True)
            os.link(launcher, self.bin / name)
        for name, body in SLEEP_PLAIN_STUBS.items():
            (self.bin / name).write_text(f"#!/bin/sh\n{prologue}{body}\n")
            (self.bin / name).chmod(0o755)
        for d in ("sys/power/suspend_stats", "sys/class/rtc/rtc0/device/power",
                  "sys/kernel/debug/qcom_stats", "sys/kernel/debug/interconnect",
                  "sys/devices/soc/ufshc/block/sda/sda34", "sys/class/block", "run/ubuntu-gts9u",
                  "var/lib/ubuntu-gts9u", "etc/ubuntu-gts9u", "usr/libexec"):
            (F / d).mkdir(parents=True, exist_ok=True)
        for p in ("24100000.interconnect", "interconnect-1", "17a00000.rsc:power-controller"):
            (F / "sys/bus/platform/devices" / p / "of_node").mkdir(parents=True)
            (F / "sys/bus/platform/devices" / p / "state_synced").write_text("1\n")
            if p != "17a00000.rsc:power-controller":
                (F / "sys/bus/platform/devices" / p / "of_node/#interconnect-cells").write_bytes(b"\0\0\0\1")
        (F / "proc/uptime").write_text("600.25 4000.00\n")
        (F / "proc/1").mkdir()
        # As squirtle had them on 2026-11-22: the root runs with its
        # superblock's default, remount-ro, which ext4 does not show; the
        # dock's stick with errors=remount-ro over a default of continue.
        (F / "proc/1/mounts").write_text(
            "/dev/sda34 / ext4 rw,noatime 0 0\n"
            "proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0\n"
            "tmpfs /run tmpfs rw,nosuid,nodev,size=1135504k,mode=755 0 0\n"
            "/dev/sdg2 /media/stick ext4 rw,nosuid,nodev,relatime,errors=remount-ro 0 0\n"
            "/dev/fuse /run/user/1000/doc fuse.portal rw,nosuid,nodev 0 0\n"
            "/dev/loop3 /snap/core/1 squashfs ro,nodev,relatime 0 0\n")
        for dev, default in (("sda34", "remount-ro"), ("sdg2", "continue"), ("sdh1", "continue"),
                             ("loop5", "remount-ro")):
            (T / f"sb-errors-{dev}").write_text(default + "\n")
        for sysctl in ("panic_on_warn", "hung_task_panic"):
            (F / "proc/sys/kernel" / sysctl).write_text("0\n")
        (F / "proc/swaps").write_text(
            "Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"
            "/swapfile                               file\t\t4194300\t\t0\t\t-2\n"
            "/dev/zram0                              partition\t4194300\t\t0\t\t100\n")
        (F / "proc/sysrq-trigger").write_text("")
        (F / "dev/kmsg").write_text("[    0.000000] Booting Linux on physical CPU 0x0\n")
        (F / "sys/power/mem_sleep").write_text("s2idle [deep]\n")
        (F / "sys/power/state").write_text("freeze mem\n")
        (F / "sys/power/pm_test").write_text("[none] core processors platform devices freezer\n")
        (F / "sys/power/pm_debug_messages").write_text("0\n")
        # The knobs of the experiment options, as squirtle had them on
        # 2026-11-22 (kernel.printk as the kernel prints it, tabs between).
        (F / "sys/power/pm_print_times").write_text("0\n")
        (F / "proc/sys/kernel/printk").write_text("5\t4\t1\t7\n")
        for d, name, value in (("printk", "console_suspend", "Y"), ("printk", "ignore_loglevel", "N"),
                               ("suspend", "pm_test_delay", "5")):
            (F / "sys/module" / d / "parameters").mkdir(parents=True, exist_ok=True)
            (F / "sys/module" / d / "parameters" / name).write_text(value + "\n")
        (F / "sys/power/pm_wakeup_irq").write_text("21\n")
        (F / "sys/power/suspend_stats/success").write_text("4\n")
        (F / "sys/power/suspend_stats/fail").write_text("0\n")
        (F / "sys/class/rtc/rtc0/wakealarm").write_text("")
        (F / "sys/class/rtc/rtc0/device/power/wakeup").write_text("enabled\n")
        for q, n in (("aosd", 7), ("cxsd", 5), ("ddr", 6)):
            (F / "sys/kernel/debug/qcom_stats" / q).write_text(
                f"Count: {n}\nLast Entered At: 0\nLast Exited At: 0\nAccumulated Duration: 0\n")
        (F / "sys/kernel/debug/interconnect/interconnect_summary").write_text("node tag avg peak\n")
        # ext4's error records of the two ext4 filesystems: none.
        for dev in ("sda34", "sdg2", "sdh1", "loop5"):
            (F / "sys/fs/ext4" / dev).mkdir(parents=True)
            for attr, value in (("errors_count", "0"), ("last_error_func", ""), ("last_error_line", "0")):
                (F / "sys/fs/ext4" / dev / attr).write_text(value + "\n")
        (T / "frozen").write_text("")
        # userdata is sda34, on the UFS LUN sda.
        (F / "dev/sda34").write_bytes(b"userdata " * 1000)
        self.part("userdata").symlink_to("../../sda34")
        (F / "sys/devices/soc/ufshc/block/sda/sda34/partition").write_text("34\n")
        (F / "sys/class/block/sda34").symlink_to("../../devices/soc/ufshc/block/sda/sda34")
        (F / "sys/class/block/sda").symlink_to("../../devices/soc/ufshc/block/sda")
        (F / "sys/devices/soc/ufshc/block/sda/ro").write_text("0\n")
        # The UFS LUN's SCSI device, 0:0:0:0 (host 0); the stick is on host 1.
        (F / "sys/devices/soc/ufshc/host0/target0:0:0/0:0:0:0").mkdir(parents=True)
        (F / "sys/devices/soc/ufshc/block/sda/device").symlink_to("../../host0/target0:0:0/0:0:0:0")
        (F / "run/ubuntu-gts9u/icc-release").write_text(
            "boot_id=df92b567-0000\nuptime=46.81\nresult=released\nreason=finished\n")
        (F / "var/log/ubuntu-gts9u-icc-release.log").write_text(
            "2026-11-22 10:00:46 up=46.81 releasing: 24100000.interconnect interconnect-1\n")
        # This boot released the floor: the loop breaker's marker is its own.
        (F / "var/lib/ubuntu-gts9u/icc-release.pending").write_text("boot_id=df92b567-0000\nuptime=46.81\n")
        (F / "usr/libexec/ubuntu-gts9u-icc-late-release").write_text("#!/bin/sh\n")
        (F / "usr/libexec/ubuntu-gts9u-icc-late-release").chmod(0o755)
        (F / "etc/hostname").write_text("squirtle\n")
        # The CPUs' cpuidle states as squirtle has them: WFI, and the one
        # with an s2idle entry, named by the CPU's cluster.
        for cpu in range(8):
            for state, name in (("state0", "WFI"), ("state1", CPU_SLEEP[cpu])):
                d = F / f"sys/devices/system/cpu/cpu{cpu}/cpuidle" / state
                d.mkdir(parents=True)
                for attr, value in (("name", name), ("usage", "1000"), ("time", "5000000")):
                    (d / attr).write_text(value + "\n")
                if state == "state1":
                    (d / "s2idle").mkdir()
                    for attr in ("usage", "time"):
                        (d / "s2idle" / attr).write_text("0\n")
        self.port = F / "sys/class/typec/port0"
        (self.port / "data_role").write_text("[host] device\n")
        (self.port / "power_role").write_text("source [sink]\n")
        (F / "sys/class/typec/port0-partner").mkdir()

    def floor_held(self, by_breaker=True):
        """The boot after a forced restart: the loop breaker held the floor
        and took this boot's marker away."""
        for p in ("24100000.interconnect", "interconnect-1"):
            (self.root / "sys/bus/platform/devices" / p / "state_synced").write_text("0\n")
        (self.root / "var/lib/ubuntu-gts9u/icc-release.pending").unlink()
        (self.root / "run/ubuntu-gts9u/icc-release").write_text(
            "boot_id=df92b567-0000\nresult=held\nreason=loop breaker: boot 82555513 released the "
            "floor at 46.81 s and then ended without a clean shutdown\n")

    def cycle(self, n, *flags, log=None, rc=None, out=None, irq=None):
        """The scenario of the Nth sleep (rtcwake -m mem call) of a run: an
        attempt that the kernel gave up is a sleep of its own."""
        d = self.tmp / f"cycle-{n}"
        d.mkdir(exist_ok=True)
        for flag in flags:
            (d / flag).touch()
        if log is not None:
            (d / "log").write_text(log)
        if rc is not None:
            (d / "rc").write_text(f"{rc}\n")
        if out is not None:
            (d / "out").write_text(out)
        if irq is not None:
            (d / "wakeup-irq").write_text(f"{irq}\n" if irq else "")

    def touch_abort(self, n, log=TOUCH_ABORT, irq=199):
        """The Nth sleep given up as on 2026-11-22 03:29."""
        self.cycle(n, "suspend-failed", "no-aosd", "no-cxsd", "no-ddr", rc=1, log=log,
                   out=TOUCH_ABORT_OUT, irq=irq)

    def manifest(self):
        return subprocess.run(["sh", "-c", f"F={self.root}; T={self.tmp}\n{MANIFEST}manifest"],
                              capture_output=True, text=True, check=True).stdout

    def read(self, name):
        path = self.tmp / name
        return path.read_text() if path.exists() else ""

    def sysrq(self):
        return (self.root / "proc/sysrq-trigger").read_text()

    def run_test(self, *args):
        before = self.manifest()
        result = self.run_script("sleep-test.sh", *args)
        self.out = result.stdout + result.stderr
        self.before = before
        # Whatever the run did, in RAM it ran nothing but the RAM root's
        # tools: a command that is not one of them says "not found".
        for log in (self.root / "run/gts9u-sleep-test").glob("2*/log"):
            self.assertNotIn(": not found", log.read_text(), log)
        return result

    def ramdir(self):
        (run,) = (self.root / "run/gts9u-sleep-test").glob("2*")
        return run

    def assert_read_only_rules(self, frozen="/media/stick\n/\n"):
        """What the run must never do: sleep with a disk filesystem writable
        and not frozen, with swap or the UFS disk writable, write to the disk
        after the first freeze, read userdata through the page cache or open
        it for writing, run a tool from anywhere but its RAM copy from the
        freeze on, or remount anything (sysrq u).  At the end every writable
        disk filesystem is still frozen, in the order frozen: the deepest
        mount first and the root last."""
        self.assertEqual(self.read("violations"), "", self.out)
        self.assertEqual(self.manifest(), self.read("disk-at-freeze"), self.out)
        self.assertNotIn("u", self.sysrq())
        self.assertEqual(self.read("frozen"), frozen, self.out)
        # Every tool called from the freeze on, and every one called from
        # the RAM root's tools, ran from the RAM root.
        ram = str(self.ramdir())
        calls = [line.split(" ", 2) for line in self.read("ram-calls").splitlines()]
        self.assertIn("after", [when for when, _, _ in calls])
        for when, exe, path in calls:
            if when == "after" or path == f"{ram}/tools":
                self.assertEqual((pathlib.Path(exe).is_relative_to(os.path.realpath(ram)), path),
                                 (True, f"{ram}/tools"), (when, exe, path))
        for call in self.read("dd.calls").splitlines():
            if "userdata" in call:
                self.assertEqual(call, f"if={self.part('userdata')} iflag=direct bs=4M count=16 "
                                       "status=none")
        # The run in RAM has nothing in its PATH but the RAM root's tools: a
        # command that is not one of them says "not found" in its log.
        self.assertNotIn(": not found", (self.ramdir() / "log").read_text())

    def errors_mode(self):
        """The errors= of the root and of the stick in the fake mount table."""
        found = {}
        for line in (self.root / "proc/1/mounts").read_text().splitlines():
            src, mp, _, opts, *_ = line.split()
            if mp in ("/", "/media/stick"):
                found[mp] = [o for o in opts.split(",") if o.startswith("errors=")]
        return found

    def test_a_healthy_run_passes_every_cycle_and_restarts_with_sysrq_b(self):
        result = self.run_test("ro", "restart-ok")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        for i in (1, 2, 3):
            self.assertIn(f"SLEEP-TEST cycle {i}/3: PASS: deep (aosd +1 cxsd +1 ddr +1), resumed, "
                          "the UFS read back as before; floor released", self.out)
        self.assertIn("SLEEP-TEST RESULT: PASS: 3 of 3 cycles resumed with the UFS readable, 3 of them "
                      "deep; floor released; kernel 68d243e908c0", self.out)
        self.assertIn("NEXT: the boot after this restart holds the interconnect floor once", self.out)
        # (The paths below /sys and /proc are the fake root's here.)
        self.assertIn("NEXT: after the restart, sh ~/typec-roles/verify.sh and cat "
                      f"{self.root}/sys/fs/ext4/sda34/errors_count (it must say 0)", self.out)
        # In order: the tools copied, errors=continue on the stick and on the
        # root, the freezes (the stick, then the root), blockdev --setro,
        # the first read, then the sleeps, each followed by a second freeze
        # of each that finds it frozen; at the end sysrq b with everything
        # still frozen, and no thaw.
        self.assertEqual(self.read("rtcwake.calls").count("-m mem -s 30"), 3)
        self.assertEqual(self.read("fsfreeze.calls"),
                         "--version\n" + "-f /proc/1/root/media/stick\n-f /proc/1/root/\n" * 4)
        self.assertEqual(self.sysrq(), "b")
        self.assertIn("restart: sysrq b; every disk filesystem frozen; the storage read back as before",
                      self.out)
        self.assertEqual(self.read("blockdev.calls").splitlines()[-1], "--setro /dev/sda")
        self.assertEqual(self.read("swapoff.calls"), "/swapfile\n")
        self.assertEqual(self.read("mount.calls"),
                         f"-t tmpfs -o size=256m,mode=0755,nosuid,nodev gts9u-sleep-test {self.ramdir()}\n"
                         "-o remount,errors=continue /media/stick\n-o remount,errors=continue /\n")
        # Through the system's own mount(8), in PID 1's mount namespace.
        self.assertEqual(self.read("nsenter.calls"),
                         f"--version\n-t 1 -m -- {self.bin}/mount -o remount,errors=continue /media/stick\n"
                         f"-t 1 -m -- {self.bin}/mount -o remount,errors=continue /\n")
        # The kernel shows errors=continue on the root, whose default is
        # remount-ro, and nothing on the stick, whose default is continue.
        self.assertEqual(self.errors_mode(), {"/": ["errors=continue"], "/media/stick": []})
        self.assertEqual(self.read("dd.calls").count("userdata"), 4)
        log = (self.ramdir() / "log").read_text()
        order = ["swapoff /swapfile", "do not touch the screen until the restart",
                 "errors=continue: /media/stick (/dev/sdg2), until the restart (it ran with errors=remount-ro)",
                 "errors=continue: / (/dev/sda34), until the restart (it ran with errors=remount-ro)",
                 "frozen: /media/stick (/dev/sdg2, ext4)", "frozen: / (/dev/sda34, ext4)",
                 "every writable disk filesystem is frozen: /media/stick /",
                 "/dev/sda is read-only at the block layer too",
                 "ext4: no error recorded, logged or given up on since the run began; error counts: "
                 "/media/stick 0 / 0",
                 "from here on the run cannot be stopped", "cycle 1/3: deep sleep"]
        self.assertEqual([log.index(said) for said in order], sorted(log.index(said) for said in order))
        self.assertEqual(log.count("errors=continue: /media/stick"), 1)
        # Before the restart, the line that tells verify.sh in the next boot.
        self.assertEqual((self.ramdir() / "result").read_text().splitlines()[-1],
                         "SLEEP-TEST RESTART: sysrq b; frozen: /media/stick /")
        self.assertNotIn("ERROR-STATE", self.out)
        self.assertNotIn("SLEEP-TEST NOTE", self.out)
        # The terminal showed the run's log (the part on the disk relays it).
        self.assertIn("SLEEP-TEST RESULT: PASS", result.stdout)
        self.assertEqual(log.count("SLEEP-TEST RESULT: PASS"), 1)
        # The verdict lines are in /run, in the kernel log and in pmsg.
        result_file = (self.ramdir() / "result").read_text()
        self.assertIn("SLEEP-TEST RESULT: PASS", result_file)
        self.assertIn("gts9u sleep-test: ", (self.root / "dev/kmsg").read_text())
        # The record before, on the disk, owned by nobody else (no SUDO_UID).
        (logs,) = (self.kit / "logs").glob("*-sleep")
        before = (logs / "before").read_text()
        self.assertIn("interconnect floor: released", before)
        self.assertIn("(not running: a baseline run)", before)
        self.assertIn("root: /dev/sda34 ext4, ext4 errors_count 0, state clean; to freeze, the root "
                      "last: /media/stick /", before)
        self.assertIn("  /media/stick (/dev/sdg2, sdg2): errors_count 0, errors=remount-ro, default continue\n"
                      "  / (/dev/sda34, sda34): errors_count 0, errors=remount-ro, default remount-ro\n", before)
        # The root's state, then each filesystem's default error behaviour.
        self.assertEqual(self.read("dumpe2fs.calls"), "-h /dev/sda34\n-h /dev/sdg2\n-h /dev/sda34\n")

    def test_nothing_remounts_and_nothing_asks_systemd_to_reboot(self):
        """2026-11-22: sysrq u's remount read-only recorded an ext4 error on
        the live root (the orphan file was not empty), and the tablet could
        not boot.  No remount but of errors= is left, and no clean reboot
        either: systemd would wait on the frozen disk."""
        text = (KIT_SRC / "sleep-test.sh").read_text()
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        for gone in ("printf u", "sysrq u", "kill -INT", "ctrl-alt-del", "init_pid", "tune2fs", "remount,ro",
                     "remount -o ro", ",ro,"):
            self.assertNotIn(gone, code)
        self.assertEqual(code.count("printf b >> /proc/sysrq-trigger"), 1)
        self.assertIn("fsfreeze -f \"$sysroot$fmp\"", code)
        # The one remount there is: errors=, the mount option alone.
        self.assertEqual([line.strip() for line in code.splitlines() if "sys_mount " in line],
                         ['deadline "$mount_deadline" sys_mount -o "remount,errors=$3" "$2" > "$ramdir/mount.out" 2>&1'])
        # Any other "remount" is a word of errors= ("remount-ro").
        for line in code.splitlines():
            for word in re.finditer(r"remount(?!-ro\b)", line):
                self.assertTrue(line.startswith("remount,errors=", word.start()), line)

    def test_the_verdicts_also_go_to_pmsg_when_there_is_one(self):
        (self.root / "dev/pmsg0").write_text("")
        self.run_test("restart-ok", "1", "15", "hold=0")
        pmsg = (self.root / "dev/pmsg0").read_text()
        self.assertIn("SLEEP-TEST cycle 1/1: PASS", pmsg)
        self.assertIn("SLEEP-TEST RESULT: PASS: 1 of 1", pmsg)
        self.assertEqual(self.read("rtcwake.calls").count("-u -d rtc0 -m mem -s 15"), 1)

    def diag_re(self):
        """sleep-test.sh's pattern for the diagnostic's lines."""
        return re.compile(re.search(r"^ufs_diag='([^']*)'$", self.script("sleep-test.sh"), re.M).group(1))

    def fail_re(self):
        """sleep-test.sh's pattern for a failure line, the disk sda."""
        text = self.script("sleep-test.sh")
        fail = re.search(r'^fail_re\(\) \{\n\techo "(.*)"\n\}', text, re.M).group(1)
        for name in ("ufs_fail", "journal_warn"):
            fail = fail.replace(f"${name}", re.search(rf"^{name}='([^']*)'$", text, re.M).group(1))
        return re.compile(fail.replace("$disk", "sda").replace("\\\\", "\\"))

    def pmsg_lines(self, evidence=True, progress=False):
        """What the run wrote to pmsg, without its prefix and stamp; without
        the cycles' evidence lines if EVIDENCE is false, and without the
        progress lines ("progress: ...": the run's start, its protection, each
        sleep's entry) unless PROGRESS is true."""
        prefix = f"gts9u sleep-test: {self.ramdir().name} "
        lines = (self.root / "dev/pmsg0").read_text().splitlines()
        self.assertTrue(all(line.startswith(prefix) for line in lines), lines)
        return [line[len(prefix):] for line in lines
                if (evidence or ": evidence: " not in line)
                and (progress or not line[len(prefix):].startswith("progress: "))]

    def test_each_cycle_s_diagnostic_lines_are_kept_with_its_verdict(self):
        """Revision 8's kernel logs a line at each calibration, and revision
        10's one more right before it (the power-up write read back).  Each
        cycle shows its own lines and no earlier cycle's again, and keeps
        them in the result file and in pmsg, before its verdict.  The kernel
        log gets no copy: nothing the run writes there can be matched again
        as a diagnostic or failure line, by a later cycle or by verify.sh."""
        (self.root / "dev/pmsg0").write_text("")
        pwrup = UFS_PWRUP[len("kernel: "):]
        entry = {n: f"qcom-qmp-ufs-phy 1d80000.phy: calibration entry: SW_RESET 0 START 0x1 PWRDN 0x1 "
                    f"READY 0x{n} C_READY 0x1 CMN 0x3" for n in (1, 2)}
        for n, line in entry.items():
            self.cycle(n, log=f"[  {n}00.000001] PM: suspend entry (deep)\n[  {n}00.000002] {pwrup}\n"
                              f"[  {n}00.000003] {line}\n"
                              f"[  {n}00.000004] Timekeeping suspended for 30.002 seconds\n"
                              f"[  {n}00.000005] PM: suspend exit\n")
        result = self.run_test("ro", "2", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        self.assertIn("SLEEP-TEST RESULT: PASS: 2 of 2 cycles", self.out)
        for n, line in entry.items():
            self.assertEqual(re.findall(rf"cycle {n}/2: diagnostic: (.*)", self.out), [pwrup, line])
        kept = [f"cycle 1/2: diagnostic: {pwrup}", f"cycle 1/2: diagnostic: {entry[1]}",
                "SLEEP-TEST cycle 1/2: PASS: deep (aosd +1 cxsd +1 ddr +1), resumed, the UFS read back as "
                f"before; floor released; {NO_SWITCHES}",
                f"cycle 2/2: diagnostic: {pwrup}", f"cycle 2/2: diagnostic: {entry[2]}",
                "SLEEP-TEST cycle 2/2: PASS: deep (aosd +1 cxsd +1 ddr +1), resumed, the UFS read back as "
                f"before; floor released; {NO_SWITCHES}"]
        self.assertEqual(self.pmsg_lines(evidence=False)[:6], kept)
        # In pmsg each cycle's five evidence lines come between its
        # diagnostic lines and its verdict; the result file has none.
        pmsg = self.pmsg_lines()
        self.assertEqual([line.split(": evidence: ")[0] for line in pmsg[:16]],
                         kept[:2] + ["cycle 1/2"] * 5 + kept[2:5] + ["cycle 2/2"] * 5 + kept[5:])
        self.assertEqual((self.ramdir() / "result").read_text().splitlines()[:6], kept)
        self.assertNotIn("evidence:", (self.ramdir() / "result").read_text())
        kmsg = (self.root / "dev/kmsg").read_text()
        self.assertNotIn("diagnostic:", kmsg)
        self.assertNotIn("evidence:", kmsg)
        for line in entry.values():
            self.assertEqual(kmsg.count(line), 1)
        self.assertEqual(kmsg.count(pwrup), 2)
        # Nothing the run itself wrote to the kernel log is a diagnostic or
        # a failure line by its own patterns.
        fail = self.fail_re()
        for line in ("[  1.0] I/O error, dev sda, sector 8 op 0x0:(READ)",
                     "[  1.0] EXT4-fs error (device sda34): ext4_journal_check_start:86",
                     "[  1.0] " + UFS_TIMEOUT[1][len("kernel: "):], "[  1.0] " + JOURNAL_WARN,
                     "[  1.0] WARNING: CPU: 3 PID: 1234 at fs/ext4/ext4_jbd2.c:78 "
                     "ext4_journal_check_start+0x6c/0xa0"):
            self.assertIsNotNone(fail.search(line), line)
        # The warning every first resume with a frozen filesystem gives is
        # not one.
        for line in SUPER_WARN.splitlines():
            self.assertIsNone(fail.search(line), line)
        own = [line for line in kmsg.splitlines() if "gts9u sleep-test: " in line]
        self.assertTrue(own)
        for line in own:
            self.assertIsNone(self.diag_re().search(line), line)
            self.assertIsNone(fail.search(line), line)

    def failed_resume(self, sets):
        """A resume whose calibration timed out SETS times, as on 2026-11-21,
        with revision 8's diagnostic lines: the UFS_TIMEOUT set (two failure
        lines and four diagnostic ones each), each set's state lines told
        apart by their CMN value; then two failure lines for the host and an
        I/O error."""
        lines = ["PM: suspend entry (deep)"]
        for s in range(1, sets + 1):
            lines += [line[len("kernel: "):].replace("CMN 0x3", f"CMN {s:#x}") for line in UFS_TIMEOUT[:6]]
        lines += [line[len("kernel: "):] for line in UFS_TIMEOUT[6:]]
        lines += ["PM: suspend exit", "I/O error, dev sda, sector 29026560 op 0x0:(READ) flags 0x80700"]
        return "".join(f"[  800.{n:06d}] {line}\n" for n, line in enumerate(lines, 1))

    def test_a_failed_cycle_s_diagnostic_lines_survive_the_restart_in_pmsg(self):
        """One failed calibration: its four diagnostic lines go to pmsg and
        the result file before the cycle's verdict, all of them."""
        (self.root / "dev/pmsg0").write_text("")
        log = self.failed_resume(1)
        self.cycle(1, "storage-dead", log=log)
        result = self.run_test("ro", "1", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        diag = [line.split("] ", 1)[1] for line in log.splitlines() if self.diag_re().search(line)]
        self.assertEqual(len(diag), 4)
        self.assertEqual([line[len("kernel: "):] for line in UFS_TIMEOUT if self.diag_re().search(line)],
                         [line.replace("CMN 0x1", "CMN 0x3") for line in diag])
        want = [f"cycle 1/1: diagnostic: {line}" for line in diag]
        # The cycle's evidence lines come before its verdict, after the
        # diagnostic's.
        self.assertTrue(all(line.startswith("cycle 1/1: evidence: ") for line in self.pmsg_lines()[4:6]),
                        self.pmsg_lines())
        pmsg = self.pmsg_lines(evidence=False)
        self.assertEqual(pmsg[:4], want)
        self.assertTrue(pmsg[4].startswith("SLEEP-TEST cycle 1/1: FAIL: "), pmsg)
        self.assertTrue(pmsg[5].startswith("SLEEP-TEST RESULT: FAIL at cycle 1/1: "), pmsg)
        self.assertIn("NEXT: the storage did not come back", "\n".join(pmsg[6:]))
        self.assertNotIn("left out", "\n".join(pmsg))
        self.assertEqual((self.ramdir() / "result").read_text().splitlines()[:4], want)
        self.assertNotIn("diagnostic:", (self.root / "dev/kmsg").read_text())
        self.assertEqual(self.sysrq(), "b")

    def test_a_failed_resume_s_retries_are_capped_in_pmsg(self):
        """Fifteen failed calibrations: sixty diagnostic lines, all on the
        terminal and in the run's log; pmsg and the result file keep the
        first 12 and the last 4 and say how many were left out, so that the
        ring keeps the verdicts."""
        (self.root / "dev/pmsg0").write_text("")
        log = self.failed_resume(15)
        self.cycle(1, "storage-dead", log=log)
        result = self.run_test("ro", "1", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        diag = [f"cycle 1/1: diagnostic: {line.split('] ', 1)[1]}" for line in log.splitlines()
                if self.diag_re().search(line)]
        self.assertEqual(len(diag), 60)
        # The terminal and the run's log: every line, in order.
        self.assertEqual(re.findall(r"cycle 1/1: diagnostic: .*", self.out), diag)
        self.assertEqual(re.findall(r"cycle 1/1: diagnostic: .*", (self.ramdir() / "log").read_text()), diag)
        note = (f"cycle 1/1: diagnostic: 44 of 60 lines left out here (kept: the first 12 and the last "
                f"4); the terminal and {self.ramdir()}/log had them all")
        want = diag[:12] + [note] + diag[-4:]
        pmsg = self.pmsg_lines(evidence=False)
        self.assertEqual(pmsg[:17], want)
        self.assertTrue(pmsg[17].startswith("SLEEP-TEST cycle 1/1: FAIL: 33 failure lines"), pmsg[17])
        self.assertTrue(pmsg[18].startswith("SLEEP-TEST RESULT: FAIL at cycle 1/1: "), pmsg[18])
        self.assertEqual((self.ramdir() / "result").read_text().splitlines()[:17], want)
        self.assertLess(len((self.root / "dev/pmsg0").read_bytes()), 8 * 1024)
        # Every pmsg line is cut at 300 characters, past the prefix.
        self.assertIn("%.300s", re.search(r"^keep\(\) \{\n.*?^\}\n", self.script("sleep-test.sh"),
                                          re.M | re.S).group(0))
        self.assertIn("%.300s", re.search(r"^ev_pmsg\(\) \{\n.*?^\}\n", self.script("sleep-test.sh"),
                                          re.M | re.S).group(0))

    def leaked_resume(self, sets, count=None, pwrup=False):
        """A resume whose calibration timed out SETS times as in revision
        10's run 5 (2026-11-23): each entry with PWRDN 0x0, and before it
        revision 10's power-up write line only if PWRUP, revision 11's
        counts line only with a COUNT; then the host's failure lines and an
        I/O error."""
        lines = ["PM: suspend entry (deep)"]
        for _ in range(sets):
            if count is not None:
                lines.append(UFS_COUNTS.format(count=count))
            if pwrup:
                lines.append(UFS_PWRUP[len("kernel: "):])
            lines += [UFS_CAL_ENTRY_DOWN, UFS_TIMEOUT[1][len("kernel: "):]]
            if count is not None:
                lines.append(UFS_COUNTS_FAILED)
        lines += [line[len("kernel: "):] for line in UFS_TIMEOUT[5:]]
        lines += ["PM: suspend exit", "I/O error, dev sda, sector 29026560 op 0x0:(READ) flags 0x80700"]
        return "".join(f"[  800.{n:06d}] {line}\n" for n, line in enumerate(lines, 1))

    def test_a_resume_whose_calibrations_had_no_phy_power_on_names_a_leaked_reference(self):
        """Revision 10's run 5: a kernel whose second UFS PHY diagnostic
        logged its power-up write at this boot's calibrations (here, the
        probe's in the kernel log; with the ring buffer past it, the
        journal's), then a resume whose 15 calibration entries have none
        before them: the FAIL, then a NEXT line that names the leaked PHY
        power reference, in pmsg too, before the run's other NEXT lines.  A
        kernel that logged no power-up write this boot (revision 9's) gets
        no such line from the same resume."""
        boot = f"[    5.080000] {UFS_PWRUP[len('kernel: '):]}\n[    5.080100] {UFS_CAL_ENTRY[len('kernel: '):]}\n"
        for name, where in (("the kernel log", "kmsg"), ("the journal", "journal"), ("neither", None)):
            with self.subTest(name):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                (self.root / "dev/pmsg0").write_text("")
                if where == "kmsg":
                    with open(self.root / "dev/kmsg", "a") as kmsg:
                        kmsg.write(boot)
                elif where == "journal":
                    (self.root / "journal").write_text("kernel: " + boot.replace("\n[", "\nkernel: ["))
                self.cycle(1, "storage-dead", log=self.leaked_resume(15))
                result = self.run_test("ro", "1", "restart-ok", "hold=0")
                self.assertEqual(result.returncode, 1, self.out)
                self.assert_read_only_rules()
                self.assertIn("SLEEP-TEST cycle 1/1: FAIL: ", self.out)
                self.assertIn(f"cycle 1/1: diagnostic: {UFS_CAL_ENTRY_DOWN}", self.out)
                self.assertIn("NEXT: the storage did not come back", self.out)
                pmsg = self.pmsg_lines(evidence=False)
                if where is None:
                    self.assertNotIn("leaked PHY power reference", self.out)
                    self.assertFalse([line for line in pmsg if "leaked" in line])
                    continue
                self.assertEqual(self.out.count(LEAK_NEXT), 1)
                self.assertIn(LEAK_NEXT, pmsg)
                result_line = next(i for i, line in enumerate(pmsg) if line.startswith("SLEEP-TEST RESULT: FAIL"))
                self.assertEqual(pmsg[result_line + 1], LEAK_NEXT)
                self.assertLess(pmsg.index(LEAK_NEXT),
                                next(i for i, line in enumerate(pmsg) if "NEXT: the storage did not come back" in line))
                self.assertIn(LEAK_NEXT, (self.ramdir() / "result").read_text())

    def leak_seen(self, diag, pwrdn_seen):
        """sleep-test.sh's phy_leak_seen on the diagnostic lines DIAG."""
        body = re.search(r"^phy_leak_seen\(\) \{ # DIAG\n.*?^\}\n", (KIT_SRC / "sleep-test.sh").read_text(),
                         re.M | re.S).group(0)
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp, "diag")
            path.write_text("".join(f"{line}\n" for line in diag))
            return subprocess.run(["sh", "-c", f"pwrdn_seen={pwrdn_seen}\n{body}phy_leak_seen \"$1\"", "sh",
                                   str(path)], capture_output=True, text=True).returncode == 0

    def test_a_calibration_is_without_a_phy_power_on_by_the_lines_before_it(self):
        """phy_leak_seen: on a kernel with the second diagnostic, an entry
        with no power-up write since the entry before it; on revision 11's,
        a power-up sequence that began with power_count 2 or more, whatever
        else; never a healthy cycle, a failure whose power-on ran, or a
        kernel without the second diagnostic."""
        pwrup = UFS_PWRUP[len("kernel: "):]
        entry = UFS_CAL_ENTRY[len("kernel: "):]
        for name, diag, pwrdn_seen, leak in (
                ("revision 10's run 5", [UFS_CAL_ENTRY_DOWN] * 15, 1, True),
                ("the same, a kernel without the second diagnostic", [UFS_CAL_ENTRY_DOWN] * 15, 0, False),
                ("a pass on revision 10", [pwrup, entry], 1, False),
                ("a failure whose power-on ran", [pwrup, UFS_CAL_ENTRY_DOWN] * 3, 1, False),
                ("one entry of three without its power-on", [pwrup, entry, entry, pwrup, entry], 1, True),
                ("revision 11, balanced", [UFS_COUNTS.format(count=1), pwrup, entry], 1, False),
                ("revision 11, power_count 2", [UFS_COUNTS.format(count=2), entry], 0, True),
                ("revision 11, power_count 12", [UFS_COUNTS.format(count=12), pwrup, entry], 1, True),
                ("revision 11, a setup_clocks repeat", [UFS_REPEAT, UFS_COUNTS.format(count=1), pwrup, entry], 1,
                 False),
                ("no line", [], 1, False)):
            with self.subTest(name):
                self.assertEqual(self.leak_seen(diag, pwrdn_seen), leak)

    def test_revision_11_s_counts_and_setup_clocks_lines_leave_an_aborted_attempt_aborted(self):
        """The counts at a power-up sequence's entry and the setup_clocks
        lines come in healthy cycles: an attempt given up before the sleep
        with them is ABORTED and tried again, and they are the cycle's
        diagnostic lines.  The line of a failed power-up sequence makes it a
        FAIL."""
        exit_line = "[13861.416262] PM: suspend exit\n"
        extra = (f"[13859.031800] {UFS_COUNTS.format(count=1)}\n[13859.031801] {UFS_PWRUP[len('kernel: '):]}\n"
                 f"[13859.031802] {UFS_CAL_ENTRY[len('kernel: '):]}\n[13859.500000] {UFS_REPEAT}\n")
        for name, log, aborted in (("counts and a repeat", TOUCH_ABORT.replace(exit_line, extra + exit_line), True),
                                   ("a failed power-up sequence", TOUCH_ABORT.replace(
                                       exit_line, extra + f"[13860.000000] {UFS_COUNTS_FAILED}\n" + exit_line), False)):
            with self.subTest(name):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                (self.root / "proc/interrupts").write_text(INTERRUPTS)
                self.touch_abort(1, log=log)
                result = self.run_test("ro", "2", "restart-ok", "hold=0")
                self.assertEqual(result.returncode, 1, self.out)
                self.assert_read_only_rules()
                for line in (UFS_COUNTS.format(count=1), UFS_REPEAT):
                    self.assertIn(f"cycle 1/2: diagnostic: {line}", self.out)
                if aborted:
                    self.assertIn("SLEEP-TEST cycle 1/2: ABORTED: the kernel gave up the suspend", self.out)
                    self.assertIn("SLEEP-TEST RESULT: PASS: 2 of 2 cycles", self.out)
                    self.assertNotIn("FAIL", self.out)
                else:
                    self.assertIn(f"cycle 1/2: diagnostic: {UFS_COUNTS_FAILED}", self.out)
                    self.assertNotIn("ABORTED", self.out)
                    self.assertIn("SLEEP-TEST RESULT: FAIL at cycle 1/2:", self.out)
                self.assertNotIn(LEAK_NEXT, self.out)

    def test_the_evidence_before_the_first_sleep_names_a_leaked_ufs_phy_power_reference(self):
        """Revision 10's run 5 before its sleep: the UFS PHY's supply enabled
        in regulator_summary, and in the clock read after it the host's core
        clock off with the PHY's own two clocks on.  One line, on the
        terminal and in pmsg, and the run as without it."""
        (self.root / "dev/pmsg0").write_text("")
        self.evidence_files()
        write_clocks(self.root / "sys/kernel/debug/clk", **{**CLK_LEAK, "gcc_ufs_phy_axi_clk": (0, 0),
                                                            "gcc_ufs_phy_unipro_core_clk": (0, 0),
                                                            "tcsr_ufs_pad_clkref_en": (0, 0),
                                                            "gcc_ufs_phy_phy_aux_clk": (1, 1),
                                                            "tcsr_ufs_clkref_en": (1, 1)})
        self.evidence_cycle(1)
        result = self.run_test("ro", "1", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        line = ("evidence: UFS PHY: LEAKED POWER REFERENCE: its supply 1d80000.phy-vdda-phy is enabled (use 1) with "
                "the UFS host clock-gated (gcc_ufs_phy_axi_clk 0/0) and its own clocks on (gcc_ufs_phy_phy_aux_clk "
                "1/1, tcsr_ufs_clkref_en 1/1): this boot has a leaked UFS PHY power reference, and on a kernel "
                "without ufs-qcom-track-phy-power-state-gts9u.patch the resume from a sleep that powers "
                "ufs_mem_phy_gdsc off will lose the UFS (docs/resume-recovery.md, \"The PHY power reference\")")
        self.assertEqual(self.out.count(line), 1)
        self.assertIn("SLEEP-TEST RESULT: SHALLOW: 1 of 1 cycles resumed with the UFS readable", self.out)
        pmsg = [l for l in self.pmsg_lines() if "UFS PHY" in l]
        self.assertEqual(pmsg, ["evidence: UFS PHY: leaked power reference before the first sleep (its supply on, "
                                "the host clock-gated, its own clocks on): a kernel without "
                                "ufs-qcom-track-phy-power-state-gts9u.patch loses the UFS at the resume"])
        # Once, with the evidence before the first sleep, before the read.
        log = (self.ramdir() / "log").read_text()
        self.assertLess(log.index("1d80000.phy-vdda-phy"), log.index(line))
        self.assertLess(log.index(line), log.index("read of "))

    def leak_line(self, regulators, clocks):
        """sleep-test.sh's ev_phy_leak on a regulator_summary and clock
        lines ("NAME ENABLE PREPARE PARENT"); None for a file not given."""
        body = re.search(r"^ev_phy_leak\(\) \{ # REGULATORS CLOCKS\n.*?^\}\n", (KIT_SRC / "sleep-test.sh").read_text(),
                         re.M | re.S).group(0)
        with tempfile.TemporaryDirectory() as tmp:
            args = []
            for name, text in (("reg", regulators), ("clk", clocks)):
                path = pathlib.Path(tmp, name)
                if text is not None:
                    path.write_text(text)
                args.append(str(path))
            result = subprocess.run(["sh", "-c", "ufs_phy_supply=1d80000.phy-vdda-phy\n" + body +
                                     'ev_phy_leak "$1" "$2"', "sh", *args], capture_output=True, text=True)
        # A file that cannot be read says nothing either (its status is
        # awk's, which the caller does not look at).
        self.assertEqual(result.stderr, "")
        return result.stdout

    def test_the_leak_line_needs_the_supply_on_the_host_gated_and_the_phy_s_clocks_on(self):
        on = REGULATOR_SUMMARY
        off = REGULATOR_SUMMARY.replace("1d80000.phy-vdda-phy          1", "1d80000.phy-vdda-phy          0")
        self.assertNotEqual(on, off)
        leak = "gcc_ufs_phy_axi_clk 0 0 gcc_gpll0\ngcc_ufs_phy_phy_aux_clk 1 1 -\ntcsr_ufs_clkref_en 1 1 bi_tcxo\n"
        self.assertTrue(self.leak_line(on, leak).startswith("LEAKED POWER REFERENCE: its supply 1d80000.phy-vdda-phy "
                                                            "is enabled (use 1) with the UFS host clock-gated"))
        for name, regulators, clocks in (
                ("the supply off", off, leak),
                ("the host's clocks on", on, leak.replace("axi_clk 0 0", "axi_clk 1 1")),
                ("the PHY's clocks off", on, leak.replace("aux_clk 1 1", "aux_clk 0 0")
                 .replace("clkref_en 1 1", "clkref_en 0 0")),
                ("one of the PHY's clocks off", on, leak.replace("clkref_en 1 1", "clkref_en 0 0")),
                ("no host clock read", on, leak.replace("gcc_ufs_phy_axi_clk 0 0 gcc_gpll0\n", "")),
                ("no clocks", on, ""), ("no clock file", on, None), ("no regulator file", None, leak)):
            with self.subTest(name):
                self.assertEqual(self.leak_line(regulators, clocks), "")

    def evidence_files(self):
        """The files the evidence reads, as squirtle has them (made-up
        values): the sleep statistics, the domains, rails, votes and wakeup
        sources, the interrupts, the UFS host and the USB controller, both
        PCIe functions, the audio DSP, the panel's backlight and two thermal
        zones."""
        F = self.root
        stats = F / "sys/kernel/debug/qcom_stats"
        (stats / "apss").write_text(qcom_stat(10, 10))
        (stats / "adsp").write_text(qcom_stat(3, 0))
        (stats / "cdsp").write_text("")
        (stats / "ddr_stats").write_text(ddr_stats(10))
        for d, name, text in (("pm_genpd", "pm_genpd_summary", GENPD_SUMMARY),
                              ("regulator", "regulator_summary", REGULATOR_SUMMARY)):
            (F / "sys/kernel/debug" / d).mkdir(parents=True)
            (F / "sys/kernel/debug" / d / name).write_text(text)
        (F / "sys/kernel/debug/interconnect/interconnect_summary").write_text(ICC_SUMMARY)
        (F / "sys/kernel/debug/wakeup_sources").write_text(
            wakeup_sources(("alarmtimer.0.auto", 1, 0), ("a600000.usb", 0, 0)))
        write_clocks(F / "sys/kernel/debug/clk", **CLK_LEAK)
        (F / "proc/interrupts").write_text(INTERRUPTS)
        files = {
            "sys/bus/platform/devices/1d84000.ufshc/power/runtime_status": "suspended",
            "sys/bus/platform/devices/1d84000.ufshc/devfreq/1d84000.ufshc/cur_freq": "75000000",
            "sys/bus/platform/devices/a600000.usb/power/runtime_status": "active",
            "sys/bus/platform/devices/a600000.usb/power/wakeup": "enabled",
            "sys/class/remoteproc/remoteproc0/name": "adsp",
            "sys/class/remoteproc/remoteproc0/state": "running",
            "sys/class/backlight/ae94000.dsi.0/bl_power": "4",
            "sys/class/backlight/ae94000.dsi.0/brightness": "0",
            "sys/class/thermal/thermal_zone0/type": "cpu-0-0-top",
            "sys/class/thermal/thermal_zone0/temp": "41200",
            "sys/class/thermal/thermal_zone1/type": "pm8550_tz",
            "sys/class/thermal/thermal_zone1/temp": "35000",
        }
        for dev, wakeup in (("0000:00:00.0", "enabled"), ("0000:01:00.0", "disabled")):
            for attr, value in (("power_state", "D0"), ("current_link_speed", "5.0 GT/s PCIe"),
                                ("d3cold_allowed", "1"), ("power/wakeup", wakeup),
                                ("power/runtime_status", "active")):
                files[f"sys/bus/pci/devices/{dev}/{attr}"] = value
        for path, value in files.items():
            (F / path).parent.mkdir(parents=True, exist_ok=True)
            (F / path).write_text(value + "\n")

    def evidence_cycle(self, n, link_up=False):
        """Sleep N of a run as the r7 baselines slept (aosd, cxsd and ddr
        +0) and what it leaves in the evidence's files: the CPUs' subsystem
        30 s asleep, DDR 30 s at its lowest frequency, the RTC's interrupt
        and its wakeup source once more; the PCIe link down and up again if
        LINK_UP.  Its kernel lines with the Wi-Fi's per-packet noise and a
        CPU's feature line, which the evidence leaves out."""
        d = self.tmp / f"cycle-{n}" / "qcom_stats"
        d.mkdir(parents=True)
        (d / "apss").write_text(qcom_stat(10 + n, 10 + 30 * n))
        (d / "ddr_stats").write_text(ddr_stats(10 + 30 * n))
        (self.tmp / f"cycle-{n}" / "interrupts").write_text(
            INTERRUPTS.replace(" 21:          4", f" 21:          {4 + n}"))
        (self.tmp / f"cycle-{n}" / "wakeup_sources").write_text(
            wakeup_sources(("alarmtimer.0.auto", 1 + n, n), ("a600000.usb", 0, 0)))
        # The UFS host resumed with the sleep: one more enable of each of its
        # clocks, as the lane clock leak has it.
        write_clocks(self.tmp / f"cycle-{n}" / "clk", **{name: (e + n, p + n) for name, (e, p) in CLK_LEAK.items()})
        log = (f"[  {n}00.000001] PM: suspend entry (deep)\n"
               f"[  {n}00.000002] CPU features: SANITY CHECK: Unexpected variation in SYS_ID_AA64MMFR1_EL1. "
               "Boot CPU: 0x00000011212122, CPU3: 0x00000010212122\n"
               f"[  {n}00.000003] Timekeeping suspended for 30.002 seconds\n"
               f"[  {n}00.000004] mhi mhi0: Requested to power ON\n"
               + (f"[  {n}00.000005] qcom-pcie 1c00000.pcie: PCIe Gen.2 x1 link up\n" if link_up else "")
               + f"[  {n}00.000006] PM: suspend exit\n"
               f"[  {n}00.000007] ath11k_pci 0000:01:00.0: msdu_done bit in attention is not set\n")
        self.cycle(n, "no-aosd", "no-cxsd", "no-ddr", log=log)

    def test_each_cycle_s_evidence_reaches_the_terminal_and_pmsg(self):
        """Around each cycle, read only: the sleep statistics (the SoC's
        states, the subsystems', DDR's modes and frequencies), the wakeup
        sources and interrupts that fired, whether the PCIe link went down
        and whether the PCIe host came back, the domains of the rails, UFS,
        USB and PCIe, XO's and the UFS host's clock counts before the sleep
        and after the wake (revision 10), and the cycle's kernel lines; once
        before the first sleep, every domain, the rails in use, the
        bandwidth votes (those in the sleep set marked), the wakeup sources
        and the PCIe functions.  The terminal and the log get it all, pmsg
        three short lines per cycle, the result file and the kernel log
        nothing.  The verdicts are as without it."""
        (self.root / "dev/pmsg0").write_text("")
        self.evidence_files()
        self.evidence_cycle(1)
        self.evidence_cycle(2, link_up=True)
        result = self.run_test("ro", "2", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        for i in (1, 2):
            self.assertIn(f"SLEEP-TEST cycle {i}/2: SHALLOW: resumed and the UFS read back as before, but not "
                          "every sleep state was reached (aosd +0 cxsd +0 ddr +0); floor released", self.out)
            self.assertIn(f"cycle {i}/2: evidence: qcom_stats: aosd +0, cxsd +0, ddr +0; subsystems adsp +0, "
                          "apss +1 30.0s; ddr modes 0xd4 +0, 0xd0 +0; ddr time at 547MHz 30.0s", self.out)
            self.assertIn(f"cycle {i}/2: evidence: wakeup sources: alarmtimer.0.auto +1 (wakeup +1)", self.out)
            self.assertIn(f"cycle {i}/2: evidence: interrupts: 21 pm8xxx_rtc_alarm +1\n", self.out)
            self.assertIn(f"cycle {i}/2: evidence: before: ufs suspended 75000000Hz; usb active wakeup enabled; "
                          "pci 0000:00:00.0 D0 0000:01:00.0 D0; remoteproc adsp=running; backlight ae94000.dsi.0 "
                          "power=4 brightness=0; warmest 41.2C cpu-0-0-top", self.out)
            for when in ("before", "after"):
                self.assertRegex(self.out, rf"cycle {i}/2: evidence: {when}: genpd: cx +on +0\n")
                self.assertRegex(self.out, rf"cycle {i}/2: evidence: {when}: genpd: ufs_phy_gdsc +off-0 +0\n")
                self.assertRegex(self.out, rf"cycle {i}/2: evidence: {when}: genpd:     1d84000\.ufshc +suspended")
            self.assertIn(f"cycle {i}/2: evidence: kernel: [  {i}00.000004] mhi mhi0: Requested to power ON",
                          self.out)
        self.assertIn("cycle 1/2: evidence: pcie: no link-up line: the PCIe host kept its link, clocks and votes "
                      "through the sleep; PCIe came back (no host failure line)\n", self.out)
        self.assertIn("cycle 2/2: evidence: pcie: qcom-pcie 1c00000.pcie: PCIe Gen.2 x1 link up; PCIe came back "
                      "(no host failure line)\n", self.out)
        # The clock counts: before each sleep as the file was, after each
        # wake as the sleep left it; XO and the UFS host's five, not the
        # others, the second consumer's line read as no clock.
        def clocks(add):
            return ", ".join(f"{name} {e + add}/{p + add}" for name, (e, p) in CLK_LEAK.items())
        for i in (1, 2):
            self.assertIn(f"cycle {i}/2: evidence: before: clocks (enable/prepare): {clocks(i - 1)}\n", self.out)
            self.assertIn(f"cycle {i}/2: evidence: after: clocks (enable/prepare): {clocks(i)}\n", self.out)
        self.assertNotIn("bi_tcxo_ao", self.out)
        self.assertNotIn("1d88000.crypto", self.out)
        evidence = [line for line in self.out.splitlines() if "evidence:" in line]
        # Each cycle shows the domains of its list and those that are on,
        # not the others; the Wi-Fi's per-packet lines and the CPUs' feature
        # lines are left out.
        self.assertFalse([line for line in evidence if "cycle " in line and "gpu_cc_cx_gdsc" in line])
        self.assertFalse([line for line in evidence if "msdu_done" in line or "SANITY CHECK" in line])
        # Once, before the first sleep: every domain line (with its
        # children), the rails in use and their users, the bandwidth votes
        # with those in the sleep set marked, the wakeup sources with events,
        # each PCIe function.
        once = [line.split("] ", 1)[1] for line in evidence if "cycle " not in line]
        fixture = {line.split()[0]: line for text in (GENPD_SUMMARY, REGULATOR_SUMMARY, ICC_SUMMARY)
                   for line in text.splitlines() if line.strip()}
        for want in [f"evidence:   {fixture[name]}" for name in ("gpu_cc_cx_gdsc", "vreg_l1e_0p88",
                                                                  "1d80000.phy-vdda-phy", "ebi", "cpu0")] + [
                "evidence:   " + " " * 48 + "mmcx, ufs_phy_gdsc",
                f"evidence:   {fixture['1c00000.pcie']}   (sleep set too)",
                "evidence:   alarmtimer.0.auto 1 0",
                "evidence: pci 0000:00:00.0: D0, link 5.0 GT/s PCIe, d3cold_allowed 1, wakeup enabled, "
                "runtime active"]:
            self.assertIn(want, once)
        for unwanted in ("regulator-dummy", "ae94000.dsi-vdda", "qhm_qup1", "3d00000.gpu", "a600000.usb 0 0"):
            self.assertFalse([line for line in once if unwanted in line], unwanted)
        # The order: the state once, the first read, then each cycle's
        # evidence before its sleep and after it, before its verdict.
        log = (self.ramdir() / "log").read_text()
        order = ["evidence: power domains", "read of ", "cycle 1/2: evidence: before:", "cycle 1/2: deep sleep",
                 "cycle 1/2: asleep", "cycle 1/2: evidence: qcom_stats", "SLEEP-TEST cycle 1/2: SHALLOW",
                 "cycle 2/2: evidence: before:", "cycle 2/2: deep sleep", "cycle 2/2: evidence: qcom_stats"]
        self.assertEqual([log.index(said) for said in order], sorted(log.index(said) for said in order))
        # The UFS host's clocks are on here (counts in the hundreds, the
        # lane clock leak): no line about the UFS PHY's power reference.
        self.assertNotIn("UFS PHY: LEAKED POWER REFERENCE", self.out)
        # pmsg: five lines per cycle, each cut at 300 characters.
        pmsg = [line for line in self.pmsg_lines() if ": evidence: " in line]
        self.assertEqual(len(pmsg), 10, pmsg)
        self.assertEqual(pmsg[0], "cycle 1/2: evidence: qcom_stats: aosd +0, cxsd +0, ddr +0; subsystems adsp +0, "
                                  "apss +1 30.0s; ddr modes 0xd4 +0, 0xd0 +0; ddr time at 547MHz 30.0s")
        self.assertTrue(pmsg[1].startswith("cycle 1/2: evidence: pcie link kept; PCIe came back (no host failure "
                                           "line); irq 21 pm8xxx_rtc_alarm +1; wakeup alarmtimer.0.auto +1 "
                                           "(wakeup +1); before: ufs suspended 75000000Hz; usb active wakeup "
                                           "enabled;"), pmsg[1])
        self.assertEqual(pmsg[2], "cycle 1/2: evidence: clocks before: xo 6/6, pad_clkref 522/522, axi 522/522, "
                                  "ahb 523/523, unipro_core 522/522, ice_core 523/523; after: xo 7/7, pad_clkref "
                                  "523/523, axi 523/523, ahb 524/524, unipro_core 523/523, ice_core 524/524")
        self.assertTrue(pmsg[6].startswith("cycle 2/2: evidence: pcie link down and up again; PCIe came back (no "
                                           "host failure line); irq 21 pm8xxx_rtc_alarm +1; wakeup "), pmsg[6])
        self.assertTrue(pmsg[7].startswith("cycle 2/2: evidence: clocks before: xo 7/7, pad_clkref 523/523, "),
                        pmsg[7])
        prefix = f"gts9u sleep-test: {self.ramdir().name} "
        for line in (self.root / "dev/pmsg0").read_text().splitlines():
            self.assertLessEqual(len(line), len(prefix) + 300, line)
        # Neither the result file nor the kernel log.
        self.assertNotIn("evidence:", (self.ramdir() / "result").read_text())
        self.assertNotIn("evidence:", (self.root / "dev/kmsg").read_text())

    def test_evidence_that_cannot_be_read_changes_nothing(self):
        """Files of the evidence that are missing or cannot be read as files
        (a directory in their place) give "-", "none" or nothing, no error
        line in the log, and the run as without them."""
        F = self.root
        (F / "sys/kernel/debug/qcom_stats/ddr_stats").mkdir()
        (F / "sys/kernel/debug/pm_genpd/pm_genpd_summary").mkdir(parents=True)
        (F / "sys/kernel/debug/wakeup_sources").mkdir()
        (F / "sys/kernel/debug").mkdir(parents=True, exist_ok=True)
        (F / "sys/kernel/debug/clk").write_text("")
        (F / "sys/bus/pci/devices/0000:01:00.0").mkdir(parents=True)
        result = self.run_test("ro", "2", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        self.assertIn("SLEEP-TEST RESULT: PASS: 2 of 2 cycles resumed with the UFS readable, 2 of them deep; floor "
                      "released; kernel 68d243e908c0", self.out)
        for i in (1, 2):
            self.assertIn(f"cycle {i}/2: evidence: qcom_stats: aosd +1, cxsd +1, ddr +1\n", self.out)
            self.assertIn(f"cycle {i}/2: evidence: wakeup sources: none\n", self.out)
            self.assertIn(f"cycle {i}/2: evidence: interrupts: none\n", self.out)
            self.assertIn(f"cycle {i}/2: evidence: before: ufs -; usb - wakeup -; pci 0000:01:00.0 -; "
                          "remoteproc none; backlight none; warmest -\n", self.out)
        self.assertIn("evidence: pci 0000:01:00.0: -, link -, d3cold_allowed -, wakeup -, runtime -", self.out)
        # No clock directory that can be read: said so, and the run as
        # without it.
        for i in (1, 2):
            for when in ("before", "after"):
                self.assertIn(f"cycle {i}/2: evidence: {when}: clocks (enable/prepare): clock counts not readable\n",
                              self.out)
                self.assertIn(f"cycle {i}/2: evidence: {when}: XO held by: clock counts not readable\n", self.out)
        log = (self.ramdir() / "log").read_text()
        for word in ("cannot open", "No such file", "Is a directory", "awk:", "read error"):
            self.assertNotIn(word, log)

    def test_ten_cycles_of_evidence_leave_pmsg_room_for_the_verdicts(self):
        """Ten cycles with every evidence file: pmsg (a ring of about 73 KiB
        of data) gets well under a quarter of it."""
        (self.root / "dev/pmsg0").write_text("")
        self.evidence_files()
        for n in range(1, 11):
            self.evidence_cycle(n)
        result = self.run_test("ro", "10", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("SLEEP-TEST RESULT: SHALLOW: 10 of 10 cycles resumed with the UFS readable, 0 of them "
                      "deep", self.out)
        self.assertEqual(len([line for line in self.pmsg_lines() if ": evidence: " in line]), 50)
        self.assertLess(len((self.root / "dev/pmsg0").read_bytes()), 24 * 1024)

    def test_the_evidence_reads_and_writes_only_its_own_files(self):
        """The evidence functions write nothing but their output files in
        the RAM directory (the OUT argument) and pmsg, run no command that
        changes anything, and are given OUT files in the RAM directory."""
        text = (KIT_SRC / "sleep-test.sh").read_text()
        start = text.index("# ------------------------------------------------- evidence, reads only ---")
        end = text.index("# The failure lines of a kernel log excerpt", start)
        code = "\n".join(line for line in text[start:end].splitlines() if not line.lstrip().startswith("#"))
        # awk's programs (single-quoted) compare with > and are not the shell's.
        shell = re.sub(r"'[^']*'", "''", code)
        targets = {m.group(1) for m in re.finditer(r"(?<![0-9&<])>>?\s*(\"[^\"]*\"|[^\s;|)]+)", shell)}
        self.assertEqual(targets, {'"$1"', "/dev/pmsg0"})
        for word in ("fsfreeze", "blockdev", "mount", "sysrq", "/dev/kmsg", "dd ", "tee ", "cp ", "mv ", "rm ",
                     "sync", "echo 1", "dynamic_debug", "/sys/power/"):
            self.assertNotIn(word, shell)
        for call in re.finditer(r"^\s*(ev_stats|ev_irqs|ev_wake) (\S+)", text, re.M):
            self.assertTrue(call.group(2).startswith('"$ramdir/ev-'), call.group(0))

    def test_a_held_floor_is_said_loudly_and_labels_every_verdict(self):
        self.floor_held()
        for n in (1, 2):
            self.cycle(n, "no-cxsd", "no-ddr")
        result = self.run_test("ro", "2", "restart-ok")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        self.assertIn("THE INTERCONNECT FLOOR IS held: this run cannot reach the depth of the 2026-11-21 "
                      "failure", self.out)
        self.assertIn("SLEEP-TEST cycle 1/2: SHALLOW: resumed and the UFS read back as before, but not "
                      "every sleep state was reached (aosd +1 cxsd +0 ddr +0); floor held", self.out)
        self.assertIn("SLEEP-TEST RESULT: SHALLOW: 2 of 2 cycles resumed with the UFS readable, 0 of them "
                      "deep; floor held (not the depth of the 2026-11-21 failure, which came with the "
                      "floor released)", self.out)
        # This boot did not release the floor: the next boot will.
        self.assertNotIn("holds the interconnect floor once", self.out)
        self.assertNotIn("restart ubuntu-gts9u-icc-late-release.service", self.read("systemctl.calls"))

    def test_release_floor_releases_it_the_documented_way_before_freezing(self):
        self.floor_held()
        result = self.run_test("ro", "1", "release-floor", "restart-ok")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        self.assertIn("restart ubuntu-gts9u-icc-late-release.service", self.read("systemctl.calls"))
        self.assertIn("interconnect floor released (", self.out)
        self.assertIn("SLEEP-TEST RESULT: PASS: 1 of 1 cycles resumed with the UFS readable, 1 of them "
                      "deep; floor released;", self.out)
        self.assertIn("NEXT: the boot after this restart holds the interconnect floor once", self.out)
        # The release wrote its marker and log before the first freeze.
        self.assertIn("icc-release.pending", self.read("disk-at-freeze"))

    def test_release_floor_leaves_the_owner_s_hold_file_alone(self):
        self.floor_held()
        (self.root / "etc/ubuntu-gts9u/icc-hold").touch()
        result = self.run_test("ro", "release-floor", "restart-ok")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("REFUSED: " + str(self.root / "etc/ubuntu-gts9u/icc-hold") + " exists: the floor "
                      "is held on purpose", self.out)
        self.assert_nothing_done()
        self.assertTrue((self.root / "etc/ubuntu-gts9u/icc-hold").exists())

    def test_a_release_that_does_not_take_stops_before_anything_is_frozen(self):
        self.floor_held()
        (self.tmp / "release-fails").touch()
        result = self.run_test("ro", "release-floor", "restart-ok")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("STOPPED before freezing anything: the floor could not be released (the panel cycle "
                      "did not succeed)", self.out)
        self.assertEqual(self.sysrq(), "")
        # The RAM root's check ran fsfreeze --version, and nothing else.
        self.assertEqual(self.read("fsfreeze.calls"), "--version\n")
        self.assertEqual(self.read("rtcwake.calls").count("-m mem"), 0)
        self.assertEqual(self.read("swapoff.calls"), "")

    def test_a_storage_that_does_not_come_back_fails_the_cycle_and_restarts_with_sysrq_b(self):
        """The 2026-11-21 failure on a revision 8 kernel: the run stops at
        that cycle, shows the diagnostic's lines, and restarts with the disk
        still frozen."""
        self.cycle(2, "storage-dead", log="".join(
            f"[  800.{n:06d}] {line[len('kernel: '):]}\n" for n, line in enumerate(
                ("kernel: PM: suspend entry (deep)",) + UFS_TIMEOUT + (
                    "kernel: PM: suspend exit",
                    "kernel: I/O error, dev sda, sector 29026560 op 0x0:(READ) flags 0x80700"),
                1)))
        result = self.run_test("ro", "3", "restart-ok")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        self.assertIn("SLEEP-TEST cycle 1/3: PASS", self.out)
        self.assertIn("SLEEP-TEST cycle 2/3: FAIL: 5 failure lines in the kernel log, the first: "
                      "qcom-qmp-ufs-phy 1d80000.phy: phy initialization timed-out; the read failed: "
                      "dd ended with 1: dd: error reading", self.out)
        self.assertIn("SLEEP-TEST RESULT: FAIL at cycle 2/3:", self.out)
        self.assertIn("cycle 2/3: diagnostic: qcom-qmp-ufs-phy 1d80000.phy: after the PCS_READY timeout: "
                      "SW_RESET 0 START 0x1", self.out)
        self.assertIn("cycle 2/3: diagnostic: qcom-qmp-ufs-phy 1d80000.phy: no PCS_READY within 1 s of "
                      "SERDES_START", self.out)
        self.assertIn('NEXT: the storage did not come back', self.out)
        self.assertEqual(self.read("rtcwake.calls").count("-m mem"), 2)
        self.assertEqual(self.sysrq(), "b")
        self.assertIn("restart: sysrq b; every disk filesystem frozen since before the first sleep", self.out)

    def test_a_read_that_differs_after_a_resume_fails_the_cycle(self):
        self.cycle(1, "other-data")
        self.run_test("restart-ok", "2")
        self.assertIn("SLEEP-TEST cycle 1/2: FAIL: the read differs from the first one", self.out)
        self.assertEqual(self.sysrq(), "b")

    def test_a_sleep_that_did_not_happen_fails_with_the_storage_healthy(self):
        """The kernel failed the suspend, and its log shows no wakeup and no
        device that refused (a CPU that would not go down, a firmware that
        said no): a FAIL as before, with the storage healthy."""
        for name, lines in (
                ("a CPU", "[  700.100000] Error taking CPU5 down: -16\n"
                          "[  700.100001] Non-boot CPUs are not disabled\n"),
                ("nothing said", "")):
            with self.subTest(name):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                self.cycle(1, "suspend-failed", rc=1, out=TOUCH_ABORT_OUT,
                           log=f"[  700.000000] PM: suspend entry (deep)\n{lines}"
                               "[  700.200000] PM: suspend exit\n")
                result = self.run_test("restart-ok", "2")
                self.assertEqual(result.returncode, 1, self.out)
                self.assert_read_only_rules()
                self.assertIn('SLEEP-TEST cycle 1/2: FAIL: rtcwake returned 1 (rtcwake: wakeup from "mem" '
                              "using rtc0 at Thu Mar  5 05:27:35 1970); suspend_stats success 4 -> 4; "
                              "suspend_stats fail 0 -> 1 (", self.out)
                self.assertNotIn("ABORTED", self.out)
                self.assertIn("SLEEP-TEST RESULT: FAIL at cycle 1/2:", self.out)
                self.assertIn("restart: sysrq b; every disk filesystem frozen; the storage read back as "
                              "before", self.out)
                self.assertEqual(self.read("rtcwake.calls").count("-m mem"), 1)

    def test_a_suspend_whose_tasks_did_not_freeze_is_no_sleep_and_not_tried_again(self):
        """A task queued behind a writer of the frozen root (on a memory
        map's lock) is not freezable, and after 20 s the freezer gives up:
        no sleep, no storage verdict, not retried; the tasks are named from
        the freezer's own lines, not from a hung-task report before it.
        The suspend's own thaw then warns (fs/super.c:1919), and nothing
        counts that."""
        (self.root / "dev/pmsg0").write_text("")
        self.cycle(2, "suspend-failed", "no-aosd", "no-cxsd", "no-ddr", rc=1, log=FREEZER_FAIL,
                   out=TOUCH_ABORT_OUT, irq="")
        result = self.run_test("ro", "3", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        stuck = "gnome-shell (state D), pool-gnome-shel (state D)"
        self.assertIn("SLEEP-TEST cycle 2/3: NO-SLEEP (freezer): the kernel gave up the suspend before the tablet "
                      "slept because a task did not freeze (rtcwake 1, suspend_stats success +0 fail +1): "
                      f"{FREEZER_LINE}; stuck: {stuck}; the UFS read back as before; not tried again; floor "
                      "released", self.out)
        self.assertIn(f"SLEEP-TEST RESULT: NO-SLEEP (freezer) at cycle 2/3: a task did not freeze ({stuck}); 1 of 3 "
                      "cycles passed before it; this run does not answer whether the storage comes back from a deep "
                      "sleep; floor released; kernel 68d243e908c0", self.out)
        self.assertIn("NEXT: no storage verdict: a task that waits behind a writer of the frozen disk (on a memory "
                      "map's lock) cannot be frozen; touch nothing during the run, and if it happens again, run the "
                      'test from multi-user.target, without the desktop (README, "The sleep test")', self.out)
        self.assertNotIn("gsd-housekeepin", self.out.split("SLEEP-TEST cycle 2/3", 1)[1])
        self.assertNotIn("FAIL", self.out)
        self.assertNotIn("ABORTED", self.out)
        self.assertEqual(self.read("rtcwake.calls").count("-m mem"), 2)
        self.assertIn("SLEEP-TEST RESULT: NO-SLEEP (freezer)", "\n".join(self.pmsg_lines()))
        self.assertEqual(self.sysrq(), "b")

    def test_a_freezer_that_gave_up_with_trouble_on_the_storage_is_still_a_fail(self):
        """Any doubt about the storage leaves the attempt a FAIL: a UFS
        failure line, a read that differs, a filesystem that was thawed."""
        timeout = UFS_TIMEOUT[1][len("kernel: "):]
        exit_line = "[  720.200000] PM: suspend exit\n"
        cases = {
            "a PHY timeout": ((), FREEZER_FAIL.replace(exit_line, f"[  720.150000] {timeout}\n{exit_line}"),
                              f"1 failure lines in the kernel log, the first: {timeout}"),
            "the read differs": (("other-data",), FREEZER_FAIL, "the read differs from the first one"),
            "a thawed filesystem": (("thawed",), FREEZER_FAIL, "the protection failed: /media/stick (/dev/sdg2) had "
                                                               "been thawed"),
        }
        for name, (flags, log, said) in cases.items():
            with self.subTest(name):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                self.cycle(1, "suspend-failed", *flags, rc=1, log=log, out=TOUCH_ABORT_OUT, irq="")
                if "thawed" in flags:
                    (self.tmp / "cycle-1/thawed").write_text("/media/stick\n")
                result = self.run_test("ro", "2", "restart-ok", "hold=0")
                self.assertEqual(result.returncode, 1, self.out)
                self.assertIn("SLEEP-TEST cycle 1/2: FAIL: ", self.out)
                self.assertIn(said, self.out)
                self.assertIn(f"the suspend could not freeze every task: {FREEZER_LINE}", self.out)
                self.assertNotIn("NO-SLEEP", self.out)
                self.assertEqual(self.read("rtcwake.calls").count("-m mem"), 1)
                self.assertEqual(self.read("violations"), "")

    def test_a_filesystem_thawed_during_a_sleep_fails_the_protection(self):
        """The design rests on the filesystems staying frozen through each
        sleep (the kernel's own thaw at resume leaves a freeze from
        userspace alone).  A second freeze after the resume that succeeds
        shows one had been thawed: a FAIL of the protection, in a cycle and
        in an aborted attempt alike, and the filesystem frozen again."""
        (self.root / "proc/interrupts").write_text(INTERRUPTS)
        for name in ("a sleep", "an aborted attempt"):
            with self.subTest(name):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                (self.root / "proc/interrupts").write_text(INTERRUPTS)
                if name == "an aborted attempt":
                    self.touch_abort(1)
                self.cycle(1, "thawed")
                (self.tmp / "cycle-1/thawed").write_text("/media/stick\n")
                result = self.run_test("ro", "2", "restart-ok", "hold=0")
                self.assertEqual(result.returncode, 1, self.out)
                self.assertIn("SLEEP-TEST cycle 1/2: FAIL: ", self.out)
                self.assertIn("the protection failed: /media/stick (/dev/sdg2) had been thawed: a second fsfreeze -f "
                              "succeeded (frozen again now), so it was writable after the resume", self.out)
                self.assertIn("NEXT: the protection failed in this run (/media/stick (/dev/sdg2) had been thawed",
                              self.out)
                self.assertNotIn("ABORTED", self.out)
                self.assertEqual(self.read("frozen"), "/\n/media/stick\n")
                self.assertEqual(self.read("rtcwake.calls").count("-m mem"), 1)
                self.assertEqual(self.sysrq(), "b")

    def test_the_warning_of_the_first_resume_counts_for_nothing(self):
        """fs/super.c:1919 at the first resume, as expected with
        freeze_filesystems 0: a sleep with it passes, an aborted attempt
        with it is still ABORTED."""
        (self.root / "proc/interrupts").write_text(INTERRUPTS)
        self.cycle(1, log="[  700.000000] PM: suspend entry (deep)\n"
                          "[  730.000000] Timekeeping suspended for 30.002 seconds\n" + SUPER_WARN +
                          "[  730.100000] PM: suspend exit\n")
        self.touch_abort(2, log=TOUCH_ABORT.replace("[13861.416262] PM: suspend exit\n",
                                                    SUPER_WARN + "[13861.416262] PM: suspend exit\n"))
        result = self.run_test("ro", "2", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        self.assertIn("SLEEP-TEST cycle 1/2: PASS: deep", self.out)
        self.assertIn("SLEEP-TEST cycle 2/2: ABORTED: ", self.out)
        self.assertIn("SLEEP-TEST RESULT: PASS: 2 of 2", self.out)
        self.assertNotIn("FAIL", self.out)

    def test_storage_lines_of_the_scsi_layer_are_never_an_aborted_attempt(self):
        """The SCSI layer's lines about the UFS: a command timing out, I/O
        rejected to an offline device, a UFS disk that refused to suspend
        (named "sd 0:0:0:0", no "ufs" and no "[sda]" in it).  The same kind
        of line about the stick, on another SCSI host, does not decide."""
        (self.root / "proc/interrupts").write_text(INTERRUPTS)
        exit_line = "[13861.416262] PM: suspend exit\n"
        for line, aborted in (("sd 0:0:0:0: [sda] tag#7 timing out command, waited 30s", False),
                              ("sd 0:0:0:0: rejecting I/O to offline device", False),
                              ("sd 0:0:0:0: failed to suspend async: error -5", False),
                              ("scsi 0:0:0:1: rejecting I/O to offline device", False),
                              ("sd 1:0:0:0: [sdg] Synchronize Cache(10) failed: Result: hostbyte=DID_ERROR "
                               "driverbyte=DRIVER_OK", True)):
            with self.subTest(line):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                (self.root / "proc/interrupts").write_text(INTERRUPTS)
                self.touch_abort(1, log=TOUCH_ABORT.replace(exit_line, f"[13860.000000] {line}\n{exit_line}"))
                result = self.run_test("ro", "1", "aborts=0", "restart-ok", "hold=0")
                self.assertEqual(result.returncode, 1, self.out)
                if aborted:
                    self.assertIn("SLEEP-TEST cycle 1/1: ABORTED: ", self.out)
                    self.assertNotIn("FAIL", self.out)
                else:
                    self.assertIn("SLEEP-TEST cycle 1/1: FAIL: ", self.out)
                    self.assertNotIn("ABORTED", self.out)

    def test_a_journal_started_on_a_frozen_ext4_fails_the_run(self):
        """ext4_journal_check_start's warning: a transaction on a frozen
        filesystem, which the next mount would discard.  In a cycle's lines
        it fails the cycle; before the first sleep it fails the run there."""
        self.cycle(1, log="[  700.000000] PM: suspend entry (deep)\n"
                          "[  730.000000] Timekeeping suspended for 30.002 seconds\n"
                          f"[  730.050000] {JOURNAL_WARN}\n[  730.100000] PM: suspend exit\n")
        result = self.run_test("ro", "2", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn(f"SLEEP-TEST cycle 1/2: FAIL: 1 failure lines in the kernel log, the first: {JOURNAL_WARN}",
                      self.out)
        FakeTablet.setUp(self)
        SleepTestTest.setUp(self)
        (self.tmp / "freeze-warns").touch()
        result = self.run_test("ro", "2", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("SLEEP-TEST RESULT: FAIL before any sleep: 1 failure lines in the kernel log since the run "
                      f"began, the first: {JOURNAL_WARN}; floor released", self.out)
        self.assertEqual(self.read("rtcwake.calls").count("-m mem"), 0)
        self.assertEqual(self.sysrq(), "b")

    def test_a_write_the_block_layer_logged_fails_the_cycle(self):
        self.cycle(1, log="[  700.000000] PM: suspend entry (deep)\n"
                   "[  700.100000] Trying to write to read-only block-device sda34\n"
                   "[  700.200000] PM: suspend exit\n")
        self.run_test("restart-ok", "1")
        self.assertIn("SLEEP-TEST cycle 1/1: FAIL: 1 failure lines in the kernel log, the first: Trying "
                      "to write to read-only block-device sda34", self.out)

    def test_a_cycle_that_missed_a_sleep_state_is_shallow_and_the_run_goes_on(self):
        self.cycle(2, "no-ddr")
        self.run_test("restart-ok")
        self.assertIn("SLEEP-TEST cycle 2/3: SHALLOW: resumed and the UFS read back as before, but not "
                      "every sleep state was reached (aosd +1 cxsd +1 ddr +0); floor released", self.out)
        self.assertIn("SLEEP-TEST cycle 3/3: PASS", self.out)
        self.assertIn("SLEEP-TEST RESULT: SHALLOW: 3 of 3 cycles resumed with the UFS readable, 2 of them "
                      "deep; floor released;", self.out)
        self.assertEqual(self.sysrq(), "b")

    def test_without_qcom_stats_the_depth_is_unknown_and_no_pass(self):
        shutil.rmtree(self.root / "sys/kernel/debug/qcom_stats")
        self.run_test("restart-ok", "1")
        self.assertIn("qcom_stats: aosd - cxsd - ddr - (not readable: the depth cannot be judged)", self.out)
        self.assertIn("SLEEP-TEST cycle 1/1: DEPTH-UNKNOWN: resumed and the UFS read back as before; "
                      "qcom_stats not readable (aosd ? cxsd ? ddr ?)", self.out)
        self.assertIn("SLEEP-TEST RESULT: DEPTH-UNKNOWN:", self.out)

    def assert_stopped_unfrozen(self, why):
        """The protection stopped before any sleep: everything thawed, the
        disk writable again, errors= back as it was (after the thaw: a
        frozen filesystem refuses a remount), no sleep, no restart."""
        self.assertIn(f"SLEEP-TEST RESULT: STOPPED before any sleep: {why}; every filesystem this run "
                      "froze is thawed again, errors= is back as it was on: /media/stick /, and the tablet "
                      "is not restarted; floor released; kernel 68d243e908c0", self.out)
        self.assertEqual(self.read("frozen"), "")
        self.assertEqual(self.sysrq(), "")
        self.assertEqual(self.read("rtcwake.calls").count("-m mem"), 0)
        self.assertEqual((self.root / "sys/devices/soc/ufshc/block/sda/ro").read_text().strip(), "0")
        self.assertEqual(self.read("violations"), "")
        self.assertIn("stopped: swap on the disk stays off until the next boot", self.out)
        self.assertEqual(self.errors_mode(), {"/": [], "/media/stick": ["errors=remount-ro"]})
        self.assertEqual(self.read("mount.calls").splitlines()[-4:],
                         ["-o remount,errors=continue /media/stick", "-o remount,errors=continue /",
                          "-o remount,errors=remount-ro /media/stick", "-o remount,errors=remount-ro /"])
        self.assertNotIn(": not found", (self.ramdir() / "log").read_text())

    def test_a_freeze_that_fails_thaws_what_was_frozen_and_stops_without_a_restart(self):
        """The root is frozen last: the stick was frozen before it, and is
        thawed again."""
        (self.tmp / "freeze-fails").write_text("/\n")
        result = self.run_test("restart-ok")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_stopped_unfrozen("fsfreeze -f / (/dev/sda34) failed: fsfreeze: /proc/1/root/: freeze "
                                     "failed: Operation not supported")
        self.assertEqual(self.read("fsfreeze.calls"), "--version\n-f /proc/1/root/media/stick\n"
                                                      "-f /proc/1/root/\n-u /proc/1/root/media/stick\n")
        # Only ext4 is frozen, and an ext4 that cannot be: no unmounting
        # will help; the kernel log says why.
        self.assertIn("NEXT: nothing slept; the kernel log says why the freeze failed (sudo dmesg | tail -n 40); "
                      "run again once that is understood", self.out)
        self.assertNotIn("unmount", self.out)
        self.assertEqual(self.read("blockdev.calls"), "--getro /dev/sda\n")

    def test_a_thaw_that_fails_restarts_rather_than_leave_a_frozen_system_running(self):
        (self.tmp / "freeze-fails").write_text("/\n")
        (self.tmp / "thaw-fails").write_text("/media/stick\n")
        self.run_test("restart-ok", "hold=0")
        self.assertIn("SLEEP-TEST RESULT: STOPPED before any sleep: fsfreeze -f / (/dev/sda34) failed: "
                      "fsfreeze: /proc/1/root/: freeze failed: Operation not supported; and fsfreeze -u "
                      "/media/stick (/dev/sdg2) failed: fsfreeze: /proc/1/root/media/stick: unfreeze failed: "
                      "Input/output error: restarting (sysrq b), a filesystem stays frozen", self.out)
        self.assertEqual(self.read("rtcwake.calls").count("-m mem"), 0)
        self.assertEqual(self.sysrq(), "b")
        # The restart's line names what is still frozen; errors= is left to
        # the restart.
        self.assertEqual((self.ramdir() / "result").read_text().splitlines()[-1],
                         "SLEEP-TEST RESTART: sysrq b; frozen: /media/stick")
        self.assertNotIn("remount-ro", self.read("mount.calls"))

    def test_a_loop_device_s_filesystem_is_frozen_first_and_the_root_last(self):
        """A filesystem on a loop device writes through to its backing file,
        on the root, say: it is frozen before anything else.  A bind mount of
        the root is the root's superblock: frozen once, by "/", last."""
        mounts = self.root / "proc/1/mounts"
        mounts.write_text(mounts.read_text() + "/dev/loop5 /mnt/image ext4 rw,relatime 0 0\n"
                          "/dev/sda34 /var/bind ext4 rw,noatime 0 0\n")
        result = self.run_test("ro", "1", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertEqual(self.read("fsfreeze.calls"), "--version\n" + "-f /proc/1/root/mnt/image\n"
                                                      "-f /proc/1/root/media/stick\n-f /proc/1/root/\n" * 2)
        self.assert_read_only_rules(frozen="/mnt/image\n/media/stick\n/\n")
        self.assertIn("SLEEP-TEST RESULT: PASS: 1 of 1", self.out)
        (logs,) = (self.kit / "logs").glob("*-sleep")
        self.assertIn("to freeze, the root last: /mnt/image /media/stick /\n", (logs / "before").read_text())

    def test_a_freeze_that_does_not_end_restarts_the_tablet(self):
        """Nothing undoes a freeze still under way (thaw_super waits for
        it): a restart, as after a power loss."""
        (self.tmp / "freeze-hangs").write_text("/\n")
        result = self.run_test("restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("SLEEP-TEST RESULT: STOPPED before any sleep: fsfreeze -f / (/dev/sda34) had not ended "
                      "after 120s, and cannot be undone: restarting (sysrq b) with what this run froze still "
                      "frozen", self.out)
        # What a hung freeze means, and not the advice for another failure.
        self.assertIn("NEXT: a freeze waits for every writer in flight and for the disk; one that does not end "
                      "has a writer that does not finish, or a disk that does not answer", self.out)
        self.assertNotIn("unmount", self.out)
        self.assertEqual(self.read("rtcwake.calls").count("-m mem"), 0)
        self.assertNotIn("-u ", self.read("fsfreeze.calls"))
        self.assertEqual(self.sysrq(), "b")
        # A restart puts errors= back by itself: no remount after the freeze.
        self.assertNotIn("remount-ro", self.read("mount.calls"))

    def test_a_filesystem_mounted_between_cycles_is_frozen_too_or_ends_the_run(self):
        """The dock's stick mounted again after a resume, say."""
        self.cycle(1, "new-mount")
        self.run_test("restart-ok", "2")
        self.assertIn("cycle 2/2: writable and not frozen: /media/stick2(/dev/sdh1); freezing it", self.out)
        self.assertIn("frozen: /media/stick2 (/dev/sdh1, ext4)", self.out)
        self.assertIn("SLEEP-TEST RESULT: PASS: 2 of 2", self.out)
        self.assert_read_only_rules(frozen="/media/stick\n/\n/media/stick2\n")
        self.assertEqual(self.sysrq(), "b")
        # And one that cannot be frozen.
        FakeTablet.setUp(self)
        SleepTestTest.setUp(self)
        self.cycle(1, "new-mount", "new-mount-unfreezable")
        self.run_test("restart-ok", "2")
        self.assertIn("SLEEP-TEST cycle 2/2: NOT STARTED: writable and not frozen: /media/stick2(/dev/sdh1); "
                      "disk swap: none; /dev/sda ro=1; mem_sleep deep", self.out)
        self.assertIn("SLEEP-TEST RESULT: ABORTED at cycle 2/2, before its sleep", self.out)
        self.assertEqual(self.read("rtcwake.calls").count("-m mem"), 1)
        self.assertEqual(self.read("violations"), "")
        self.assertEqual(self.sysrq(), "b")

    def test_the_disk_is_made_read_only_at_the_block_layer_or_nothing_sleeps(self):
        (self.tmp / "setro-fails").touch()
        result = self.run_test("restart-ok")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_stopped_unfrozen("blockdev --setro /dev/sda failed")
        self.assertEqual(self.read("fsfreeze.calls").count("-u "), 2)

    def test_a_first_read_that_fails_ends_the_run_before_any_sleep(self):
        (self.tmp / "storage-dead").touch()
        self.run_test("restart-ok")
        self.assertIn("SLEEP-TEST RESULT: FAIL before any sleep: the first read of", self.out)
        self.assertEqual(self.read("rtcwake.calls").count("-m mem"), 0)
        self.assertEqual(self.sysrq(), "b")

    def test_an_error_saved_while_the_root_is_frozen_stops_the_run_with_the_owner_told_to_e2fsck(self):
        """An ext4 error on the frozen root before its disk is read-only
        (a reader's): ext4 logs it and saves it (its count rises).  The run
        stops before any sleep, thawed, errors= back, not restarted, and
        says the root needs e2fsck from recovery before the next boot."""
        (self.tmp / "freeze-records-error").touch()
        result = self.run_test("restart-ok")
        self.assertEqual(result.returncode, 1, self.out)
        bad = ("/ (/dev/sda34): its error count rose 0 -> 1 (last error in ext4_lookup:1817), 1 ext4 error "
               "lines, the first: EXT4-fs error (device sda34): ext4_lookup:1817: inode #393217: comm "
               "systemd-journal: iget: checksum invalid")
        self.assert_stopped_unfrozen("ext4 recorded an error on, or gave up on, a filesystem while it was being "
                                     f"frozen: {bad}")
        self.assertIn(f"NEXT: ext4 recorded an error on, or gave up on: {bad}; the tablet runs on, thawed: save "
                      "what you need, then go to recovery (hold Power and Volume Down until the screen goes dark, "
                      "then at once Power and Volume Up, for TWRP) and run e2fsck -f on it there before the next "
                      "boot", self.out)
        self.assertIn("--setrw /dev/sda", self.read("blockdev.calls"))

    def test_an_ext4_error_before_the_freeze_stops_the_run_before_anything_is_frozen(self):
        """Between the checks and the first freeze (swap going off, the
        floor's release) ext4 records an error on the root, or gives up on
        it: compared with the count the checks read, and with the kernel's
        lines since the run began, nothing is frozen, errors= goes back, and
        the owner is told to e2fsck, not to unmount anything."""
        for flag, said in (
                ("error-at-swapoff", "/ (/dev/sda34): its error count rose 0 -> 1 (last error in "
                                     "ext4_lookup:1817), 1 ext4 error lines, the first: EXT4-fs error (device "
                                     "sda34): ext4_lookup:1817: inode #393217: comm tracker-miner-f: iget: "
                                     "checksum invalid"),
                ("abort-at-swapoff", "/ (/dev/sda34): mounted rw,noatime,errors=continue,emergency_ro, its error "
                                     "count rose 0 -> 1 (last error in ext4_lookup:1817), the kernel logged: "
                                     "Aborting journal on device sda34-8., 1 ext4 error lines")):
            with self.subTest(flag):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                (self.tmp / flag).touch()
                result = self.run_test("ro", "1", "restart-ok")
                self.assertEqual(result.returncode, 1, self.out)
                self.assertIn("SLEEP-TEST RESULT: STOPPED before any sleep: ext4 recorded an error on, or gave up "
                              f"on, a filesystem before anything was frozen: {said}", self.out)
                self.assertEqual(self.read("fsfreeze.calls"), "--version\n")
                self.assert_stopped_unfrozen("ext4 recorded an error on, or gave up on, a filesystem before "
                                             "anything was frozen: " + self.out.split(
                                                 "a filesystem before anything was frozen: ", 1)[1].split(
                                                 "; every filesystem this run froze", 1)[0])
                self.assertIn("run e2fsck -f on it there before the next boot", self.out)
                self.assertNotIn("unmount", self.out)

    # errors=continue: the remount, its check, and putting it back.

    def test_errors_continue_that_does_not_take_stops_before_anything_is_frozen(self):
        """The remount goes through and changes nothing, or fails: what it
        changed already is put back, and nothing is frozen."""
        for name, damage, said, back in (
                ("ignored", ("remount-ignored", "continue\n"),
                 "errors=continue did not take on /media/stick (/dev/sdg2): mount -o remount,errors=continue "
                 "/media/stick went through, but /media/stick runs with errors=remount-ro (ROOT/proc/1/mounts: "
                 "rw,nosuid,nodev,relatime,errors=remount-ro)", ""),
                ("fails on the root", ("remount-fails", "continue /\n"),
                 "errors=continue did not take on / (/dev/sda34): mount -o remount,errors=continue / failed "
                 "(status 32): mount: /: mount point not mounted or bad option.",
                 "; errors= is back as it was on: /media/stick")):
            with self.subTest(name):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                (self.tmp / damage[0]).write_text(damage[1])
                result = self.run_test("ro", "1", "restart-ok")
                self.assertEqual(result.returncode, 1, self.out)
                (logs,) = (self.kit / "logs").glob("*-sleep")
                said = said.replace("ROOT/", f"{self.root}/")
                self.assertIn(f"STOPPED before freezing anything: {said}{back}; nothing on the disk changed but "
                              f"{logs}", self.out)
                self.assertEqual(self.errors_mode(), {"/": [], "/media/stick": ["errors=remount-ro"]})
                self.assertEqual(self.read("fsfreeze.calls"), "--version\n")
                self.assertEqual(self.read("rtcwake.calls").count("-m mem"), 0)
                self.assertEqual(self.sysrq(), "")
                self.assertFalse((self.ramdir() / "started").exists())
                self.assertEqual(self.read("violations"), "")

    def test_a_stop_that_cannot_put_errors_back_says_so(self):
        """A stop before any sleep whose remount back fails: the filesystem
        keeps errors=continue until the next restart, and the run says
        which, and how to put it back by hand."""
        (self.tmp / "setro-fails").touch()
        (self.tmp / "remount-fails").write_text("remount-ro /media/stick\n")
        result = self.run_test("restart-ok")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("SLEEP-TEST RESULT: STOPPED before any sleep: blockdev --setro /dev/sda failed; every "
                      "filesystem this run froze is thawed again, and the tablet is not restarted; but "
                      "/media/stick (/dev/sdg2) keeps errors=continue until the next restart (mount -o "
                      "remount,errors=remount-ro /media/stick failed (status 32): mount: /media/stick: mount "
                      "point not mounted or bad option.; by hand: sudo mount -o "
                      "remount,errors=remount-ro /media/stick); floor released", self.out)
        self.assertIn("NEXT: put errors= back by hand as said above, or restart the tablet (sudo reboot)", self.out)
        self.assertEqual(self.errors_mode(), {"/": [], "/media/stick": []})
        self.assertEqual(self.read("frozen"), "")
        self.assertEqual(self.sysrq(), "")

    def test_a_filesystem_that_runs_with_errors_continue_is_left_as_it_is(self):
        """The stick without errors= among its options, its superblock's
        default continue: ext4 runs it with continue already."""
        mounts = self.root / "proc/1/mounts"
        mounts.write_text(mounts.read_text().replace("rw,nosuid,nodev,relatime,errors=remount-ro",
                                                     "rw,nosuid,nodev,relatime"))
        result = self.run_test("ro", "1", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        self.assertEqual(self.read("mount.calls").splitlines()[1:], ["-o remount,errors=continue /"])
        self.assertEqual(self.errors_mode(), {"/": ["errors=continue"], "/media/stick": []})
        self.assertIn("SLEEP-TEST RESULT: PASS: 1 of 1", self.out)

    def test_a_run_that_does_not_start_in_ram_puts_errors_back(self):
        """The part on the disk set errors=continue, and the run in RAM never
        started: nothing was frozen, and the part on the disk puts errors=
        back itself."""
        (self.tmp / "setsid-fails").touch()
        result = self.run_test("ro", "1", "restart-ok")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("STOPPED: the run did not start from RAM (status 125): nothing was frozen; errors= is back "
                      "as it was on: /media/stick /", self.out)
        self.assertEqual(self.errors_mode(), {"/": [], "/media/stick": ["errors=remount-ro"]})
        self.assertEqual(self.read("fsfreeze.calls"), "--version\n")
        self.assertEqual(self.sysrq(), "")

    # ext4 after a sleep: what it did, said, and the restart all the same.

    def test_an_ext4_error_on_the_read_only_disk_is_noted_and_nothing_was_written(self):
        """errors=continue at work: the root's reads failed after a resume,
        ext4 logged an error, and with the disk read-only it neither aborted
        its journal nor saved anything.  The cycle fails (an ext4 error on
        the UFS disk), and the next boot is expected to mount the root."""
        (self.root / "dev/pmsg0").write_text("")
        self.cycle(1, "ext4-error")
        result = self.run_test("ro", "1", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        line = ("EXT4-fs error (device sda34): __ext4_find_entry:1622: inode #2: comm systemd: reading directory "
                "lblock 0")
        self.assertIn(f"SLEEP-TEST cycle 1/1: FAIL: 1 failure lines in the kernel log, the first: {line}", self.out)
        self.assertIn("SLEEP-TEST NOTE: ext4 logged errors on a filesystem of the read-only UFS disk, and with "
                      "errors=continue it neither aborted its journal nor saved them: / (/dev/sda34): 1 ext4 error "
                      f"lines, the first: {line}", self.out)
        self.assertIn("NEXT: nothing of those errors was written: the boot after this restart is expected to mount "
                      "the root as usual", self.out)
        self.assertNotIn("ERROR-STATE", self.out)
        self.assertNotIn("emergency_ro", (self.root / "proc/1/mounts").read_text())
        self.assertIn(f"ext4: {line}", self.pmsg_lines())
        self.assertEqual(self.sysrq(), "b")

    def test_a_journal_abort_is_an_error_state_whose_next_steps_say_the_root_will_probably_not_mount(self):
        """jbd2 aborts the root's journal on its own (no errors= stops that),
        and writes the error into the journal's superblock: the next mount
        would mark the root in error and fail.  ERROR-STATE, the evidence in
        pmsg, NEXT lines for recovery, and the restart all the same."""
        for jsb in (False, True):
            with self.subTest(jsb_write_failed=jsb):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                (self.root / "dev/pmsg0").write_text("")
                self.cycle(1, "journal-abort", *(("jsb-fails",) if jsb else ()))
                result = self.run_test("ro", "2", "restart-ok", "hold=0")
                self.assertEqual(result.returncode, 1, self.out)
                self.assert_read_only_rules()
                self.assertIn("SLEEP-TEST cycle 1/2: FAIL: ", self.out)
                self.assertIn("SLEEP-TEST ERROR-STATE: ext4 recorded an error on, or gave up on, a filesystem of "
                              "this run: / (/dev/sda34): mounted rw,noatime,errors=continue,emergency_ro, the "
                              "kernel logged: Aborting journal on device sda34-8., 1 ext4 error lines, the first: "
                              "EXT4-fs error (device sda34): ext4_journal_check_start:86: comm kworker/u16:3: "
                              "Detected aborted journal", self.out)
                pmsg = self.pmsg_lines()
                self.assertIn("ext4: Aborting journal on device sda34-8.", pmsg)
                if jsb:
                    self.assertIn("NEXT: ext4 gave up on the root; jbd2 writes the error into the journal's "
                                  "superblock even with the disk read-only, but the kernel logged that a journal "
                                  "superblock write failed (JBD2: I/O error when updating journal superblock for "
                                  "sda34-8.), so it may not have reached the disk", self.out)
                else:
                    self.assertIn("NEXT: ext4 gave up on the root, and jbd2 writes the error into the journal's "
                                  "superblock even with the disk read-only: the boot after this restart will "
                                  "probably not mount the root", self.out)
                self.assertIn("go to recovery (hold Power and Volume Down until the screen goes dark, then at "
                              "once Power and Volume Up, for TWRP) and repair the root with e2fsck -f", self.out)
                self.assertIn('NEXT: the storage did not come back: read the diagnostic lines above', self.out)
                # Not the next steps of a root that is fine.
                self.assertNotIn("(it must say 0)", self.out)
                self.assertNotIn("errors_count (0)", self.out)
                self.assertEqual(self.sysrq(), "b")
                self.assertEqual(self.read("rtcwake.calls").count("-m mem"), 1)

    def test_an_error_on_another_disk_s_filesystem_is_saved_and_said_but_not_the_root_s(self):
        """The stick's disk is not read-only: ext4 saves an error there (its
        count rises).  The UFS cycle passes; the stick needs e2fsck before
        it is mounted again, and the root's next steps are as usual."""
        self.cycle(1, "stick-error")
        result = self.run_test("ro", "1", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        self.assertIn("SLEEP-TEST RESULT: PASS: 1 of 1", self.out)
        self.assertIn("SLEEP-TEST ERROR-STATE: ext4 recorded an error on, or gave up on, a filesystem of this run: "
                      "/media/stick (/dev/sdg2): its error count rose 0 -> 1 (last error in __ext4_find_entry:1622), "
                      "1 ext4 error lines, the first: EXT4-fs error (device sdg2):", self.out)
        self.assertIn("NEXT: run e2fsck -f on each filesystem named above (not the root) before it is mounted again",
                      self.out)
        self.assertIn("(it must say 0)", self.out)
        self.assertNotIn("will probably not mount the root", self.out)
        self.assertEqual(self.sysrq(), "b")

    def test_ext4_is_looked_at_again_right_before_the_restart(self):
        """What ext4 did during the hold is said before sysrq b, in a run
        with sleeps and in dry."""
        for args in (("ro", "1"), ("dry",)):
            with self.subTest(args[0]):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                (self.tmp / "abort-at-hold").touch()
                result = self.run_test(*args, "restart-ok", "hold=1")
                self.assertEqual(result.returncode, 1, self.out)
                self.assertIn("SLEEP-TEST ERROR-STATE (right before the restart): ext4 recorded an error on, or "
                              "gave up on, a filesystem of this run: / (/dev/sda34): mounted "
                              "rw,noatime,errors=continue,emergency_ro, the kernel logged: Aborting journal on "
                              "device sda34-8.", self.out)
                result_file = (self.ramdir() / "result").read_text()
                self.assertLess(result_file.index("ERROR-STATE (right before the restart)"),
                                result_file.index("SLEEP-TEST RESTART: sysrq b"))
                self.assertIn("will probably not mount the root", result_file)
                self.assertEqual(self.sysrq(), "b")

    def test_dry_freezes_reads_twice_and_restarts_without_a_sleep(self):
        (self.root / "dev/pmsg0").write_text("")
        result = self.run_test("dry", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        self.assertEqual(self.read("rtcwake.calls"), "--version\n")
        self.assertEqual(self.read("dd.calls").count("userdata"), 2)
        # The second freeze of each finds it frozen: what a run with sleeps
        # relies on, seen on the tablet before any sleep.
        self.assertEqual(self.read("fsfreeze.calls"),
                         "--version\n" + "-f /proc/1/root/media/stick\n-f /proc/1/root/\n" * 2)
        self.assertIn("SLEEP-TEST RESULT: DRY: no sleep tried; frozen: /media/stick /; /dev/sda read-only at "
                      "the block layer; a second freeze found each still frozen; the UFS read back as before "
                      "after 10s; no failure line in the kernel log, and ext4 recorded, logged and gave up on "
                      "nothing (looked at again right before the restart); floor released; kernel 68d243e908c0",
                      self.out)
        self.assertIn("NEXT: the boot after this restart (sysrq b, everything frozen) must come up as after a "
                      "power loss; sh ~/typec-roles/verify.sh then says so (it knows the boot after a sleep test); "
                      "only then a run with sleeps", self.out)
        self.assertNotIn("PASS", self.out)
        self.assertEqual(self.sysrq(), "b")
        self.assertIn("SLEEP-TEST RESULT: DRY", "\n".join(self.pmsg_lines()))
        self.assertEqual(self.pmsg_lines()[-1], "SLEEP-TEST RESTART: sysrq b; frozen: /media/stick /")
        (logs,) = (self.kit / "logs").glob("*-sleep")
        self.assertIn("dry: every disk filesystem frozen and no sleep", (logs / "before").read_text())

    def test_dry_fails_on_a_filesystem_that_is_not_frozen_any_more(self):
        """dry's own check of the freeze: a second freeze that succeeds."""
        # The settling sleep after the first read thaws the root.
        sleep = self.tmp / "stubs/sleep"
        self.assertEqual(sleep.read_text().count("\nexit 0\n"), 1)
        sleep.write_text(sleep.read_text().replace("\nexit 0\n", """
if [ "${1:-}" = 10 ]; then grep -vxF / "$T/frozen" > "$T/f.new"; mv "$T/f.new" "$T/frozen"; fi
exit 0
"""))
        result = self.run_test("dry", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("SLEEP-TEST RESULT: FAIL (dry, no sleep tried): the protection failed: / (/dev/sda34) had "
                      "been thawed: a second fsfreeze -f succeeded (frozen again now), so it was writable after "
                      "the resume", self.out)
        self.assertIn("NEXT: the protection failed in this run", self.out)
        self.assertEqual(self.read("frozen"), "/media/stick\n/\n")
        self.assertEqual(self.sysrq(), "b")

    # ABORTED: a suspend that the kernel gave up before the tablet slept.

    def test_an_aborted_attempt_is_tried_again_and_the_run_still_passes(self):
        """2026-11-22 03:29: the touchscreen's double tap in the noirq phase.
        Not a FAIL: the same cycle again, and a pass counts only real
        sleeps."""
        (self.root / "proc/interrupts").write_text(INTERRUPTS)
        (self.root / "dev/pmsg0").write_text("")
        self.touch_abort(1)
        result = self.run_test("ro", "3", "restart-ok")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        line = ("SLEEP-TEST cycle 1/3: ABORTED: the kernel gave up the suspend before the tablet slept "
                f"(rtcwake 1, suspend_stats success +0 fail +1): a wakeup from {TOUCH_HINT}; the UFS read "
                f"back as before; aborted attempt 1 (aborts=5): cycle 1/3 again in 5 s; floor released; {NO_SWITCHES}")
        self.assertIn(line, self.out)
        self.assertNotIn("FAIL", self.out)
        self.assertIn("cycle 1/3: deep sleep for 30s (rtcwake -m mem), attempt 2;", self.out)
        for i in (1, 2, 3):
            self.assertEqual(self.out.count(f"SLEEP-TEST cycle {i}/3: PASS: deep"), 1)
        self.assertIn("SLEEP-TEST RESULT: PASS: 3 of 3 cycles resumed with the UFS readable, 3 of them deep; "
                      "1 attempt aborted before the sleep (IRQ 199 (fts1ba90a, the touchscreen) once) and tried "
                      "again; floor released; kernel 68d243e908c0", self.out)
        self.assertEqual(self.read("rtcwake.calls").count("-m mem"), 4)
        self.assertEqual(self.read("dd.calls").count("userdata"), 5)
        self.assertEqual(self.sysrq(), "b")
        # Kept like the other verdicts, before the retry's.
        pmsg = self.pmsg_lines()
        self.assertIn(line, pmsg)
        self.assertLess(pmsg.index(line), pmsg.index(
            "SLEEP-TEST cycle 1/3: PASS: deep (aosd +1 cxsd +1 ddr +1), resumed, the UFS read back as before; "
            f"floor released; {NO_SWITCHES}"))
        self.assertIn(line, (self.ramdir() / "result").read_text())
        # The attempt and the retry each have files of their own, and the
        # retry's kernel lines are its own.
        run = self.ramdir()
        self.assertEqual((run / "rtcwake-1").read_text(), TOUCH_ABORT_OUT)
        self.assertEqual((run / "rtcwake-1.2").read_text(), "")
        self.assertIn("Wakeup pending. Abort CPU freeze", (run / "kmsg-1").read_text())
        self.assertNotIn("Wakeup pending", (run / "kmsg-1.2").read_text())
        self.assertEqual((run / "kmsg-1.2").read_text().count("PM: suspend entry (deep)"), 1)

    def test_aborted_attempts_past_the_budget_end_the_run_with_no_sleep_not_fail(self):
        """The first cycle passes, then six attempts at the second are given
        up (aborts=5 tries five again): NO-SLEEP, with the storage question
        open, and the restart as after a healthy run."""
        (self.root / "proc/interrupts").write_text(INTERRUPTS)
        for n in range(2, 8):
            self.touch_abort(n)
        result = self.run_test("ro", "3", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        self.assertEqual(self.read("rtcwake.calls").count("-m mem"), 7)
        self.assertEqual(self.out.count("SLEEP-TEST cycle 2/3: ABORTED: "), 6)
        for n in range(1, 6):
            self.assertIn(f"aborted attempt {n} (aborts=5): cycle 2/3 again in 5 s; floor released", self.out)
        self.assertIn("aborted attempt 6, more than aborts=5: the run ends; floor released", self.out)
        self.assertIn("SLEEP-TEST RESULT: NO-SLEEP at cycle 2/3: 6 attempts aborted before the sleep (IRQ 199 "
                      "(fts1ba90a, the touchscreen) 6 times), more than aborts=5; 1 of 3 cycles passed before "
                      "it; this run does not answer whether the storage comes back from a deep sleep; floor "
                      "released; kernel 68d243e908c0", self.out)
        self.assertIn("NEXT: no storage verdict: after the restart run the test again, without touching the "
                      "screen", self.out)
        self.assertNotIn("FAIL", self.out)
        self.assertNotIn("RESULT: PASS", self.out)
        self.assertNotIn("cycle 3/3", self.out)
        self.assertIn("restart: sysrq b; every disk filesystem frozen; the storage read back as before",
                      self.out)
        self.assertEqual(self.sysrq(), "b")
        for n in range(2, 7):
            self.assertTrue((self.ramdir() / f"kmsg-2.{n}").exists())

    def test_aborts_0_ends_the_run_at_the_first_abort(self):
        (self.root / "proc/interrupts").write_text(INTERRUPTS)
        self.touch_abort(1)
        result = self.run_test("ro", "3", "aborts=0", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn(f"SLEEP-TEST cycle 1/3: ABORTED: the kernel gave up the suspend before the tablet slept "
                      f"(rtcwake 1, suspend_stats success +0 fail +1): a wakeup from {TOUCH_HINT}; the UFS read "
                      "back as before; aborted attempt 1, more than aborts=0: the run ends; floor released",
                      self.out)
        self.assertIn("SLEEP-TEST RESULT: NO-SLEEP at cycle 1/3: 1 attempt aborted before the sleep (IRQ 199 "
                      "(fts1ba90a, the touchscreen) once), more than aborts=0; 0 of 3 cycles passed before it;",
                      self.out)
        self.assertEqual(self.read("rtcwake.calls").count("-m mem"), 1)
        self.assertNotIn("FAIL", self.out)
        (logs,) = (self.kit / "logs").glob("*-sleep")
        self.assertIn("(aborted attempts tried again: up to 0)", (logs / "before").read_text())

    def test_an_aborted_attempt_with_storage_trouble_or_a_real_sleep_is_still_a_fail(self):
        """Every doubt is a FAIL: the read differs, a PHY timeout line, a
        UFS device that refused to suspend, a diagnostic timeout line, a
        sleep that the kernel counted as a success, or a long suspended
        timekeeping."""
        timeout = UFS_TIMEOUT[1][len("kernel: "):]
        exit_line = "[13861.416262] PM: suspend exit\n"
        cases = {
            "the read differs": ((), TOUCH_ABORT, "the read differs from the first one"),
            "a PHY timeout": ((), TOUCH_ABORT.replace(exit_line, f"[13860.000000] {timeout}\n{exit_line}"),
                              f"1 failure lines in the kernel log, the first: {timeout}"),
            "a UFS device refused": ((), TOUCH_ABORT.replace(
                exit_line, "[13860.000000] ufs_device_wlun 0:0:0:49488: failed to suspend: error -5\n"
                           f"{exit_line}"), "rtcwake returned 1"),
            "a diagnostic timeout line": ((), TOUCH_ABORT.replace(
                exit_line, "[13860.000000] " + UFS_TIMEOUT[2][len("kernel: "):] + "\n" + exit_line),
                "rtcwake returned 1"),
            "a counted success": (("no-fail",), TOUCH_ABORT, "rtcwake returned 1"),
            "a long sleep": ((), TOUCH_ABORT.replace(
                exit_line, "[13860.000000] Timekeeping suspended for 30.002 seconds\n" + exit_line),
                "rtcwake returned 1"),
        }
        for name, (flags, log, said) in cases.items():
            with self.subTest(name):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                (self.root / "proc/interrupts").write_text(INTERRUPTS)
                if "no-fail" in flags:
                    self.cycle(1, rc=1, log=log, out=TOUCH_ABORT_OUT, irq=199)
                else:
                    self.touch_abort(1, log=log)
                if name == "the read differs":
                    self.cycle(1, "other-data")
                result = self.run_test("ro", "2", "restart-ok", "hold=0")
                self.assertEqual(result.returncode, 1, self.out)
                self.assertIn("SLEEP-TEST cycle 1/2: FAIL: ", self.out)
                self.assertIn(said, self.out)
                self.assertNotIn("ABORTED", self.out)
                self.assertIn("SLEEP-TEST RESULT: FAIL at cycle 1/2:", self.out)
                self.assertEqual(self.read("rtcwake.calls").count("-m mem"), 1)
                self.assertEqual(self.read("violations"), "")

    def test_an_rtcwake_failure_without_a_sign_of_the_suspend_given_up_is_still_a_fail(self):
        """rtcwake failed and suspend_stats counted a failure, but the kernel
        log shows no pending wakeup and no device that refused: a FAIL, as
        on 2026-11-22 before this change."""
        (self.root / "proc/interrupts").write_text(INTERRUPTS)
        log = "".join(line + "\n" for line in TOUCH_ABORT.splitlines()
                      if "Triggering wakeup" not in line and "Wakeup pending" not in line)
        self.touch_abort(1, log=log, irq="")
        self.run_test("ro", "3", "restart-ok", "hold=0")
        self.assertIn('SLEEP-TEST cycle 1/3: FAIL: rtcwake returned 1 (rtcwake: wakeup from "mem" using rtc0 at '
                      "Thu Mar  5 05:27:35 1970); suspend_stats success 4 -> 4; suspend_stats fail 0 -> 1 "
                      f"(aosd +0 cxsd +0 ddr +0; floor released; {NO_SWITCHES})", self.out)
        self.assertIn("SLEEP-TEST RESULT: FAIL at cycle 1/3:", self.out)
        self.assertNotIn("ABORTED", self.out)

    def test_the_cause_of_an_abort_is_named(self):
        """The wakeup IRQ by its name in /proc/interrupts, the touchscreen
        with its hint and no other; an IRQ with no name; a device that
        refused to suspend; a wakeup source."""
        triggering = "[13858.996988] PM: Triggering wakeup from IRQ 199\n"
        no_irq = TOUCH_ABORT.replace(triggering, "")
        cases = {
            "the touchscreen": (TOUCH_ABORT, 199, INTERRUPTS, f"a wakeup from {TOUCH_HINT}",
                                "IRQ 199 (fts1ba90a, the touchscreen) once"),
            "another named IRQ": (TOUCH_ABORT.replace("IRQ 199", "IRQ 37"), 37, INTERRUPTS,
                                  "a wakeup from IRQ 37 (pm8550_pwrkey)", "IRQ 37 (pm8550_pwrkey) once"),
            "a shared IRQ": (TOUCH_ABORT.replace("IRQ 199", "IRQ 40"), 40, INTERRUPTS,
                             "a wakeup from IRQ 40 (foo, bar)", "IRQ 40 (foo, bar) once"),
            "a flow handler's name": (TOUCH_ABORT.replace("IRQ 199", "IRQ 41"), 41, INTERRUPTS,
                                      "a wakeup from IRQ 41 (baz)", "IRQ 41 (baz) once"),
            "an IRQ with no action": (TOUCH_ABORT.replace("IRQ 199", "IRQ 251"), 251, INTERRUPTS,
                                      "a wakeup from IRQ 251 (no name for it in ROOT/proc/interrupts)", "IRQ 251 once"),
            "an IRQ not listed": (TOUCH_ABORT.replace("IRQ 199", "IRQ 250"), 250, INTERRUPTS,
                                  "a wakeup from IRQ 250 (no name for it in ROOT/proc/interrupts)", "IRQ 250 once"),
            "no /proc/interrupts": (TOUCH_ABORT, 199, None,
                                    "a wakeup from IRQ 199 (no name for it in ROOT/proc/interrupts)", "IRQ 199 once"),
            "pm_wakeup_irq only": (no_irq, 199, INTERRUPTS, f"a wakeup from {TOUCH_HINT}",
                                   "IRQ 199 (fts1ba90a, the touchscreen) once"),
            "a device that refused": (
                "[  700.000000] PM: suspend entry (deep)\n"
                "[  700.100000] samsung_fts1ba90a 9-0049: failed to suspend async: error -16\n"
                "[  700.100001] PM: Some devices failed to suspend, or early wake event detected\n"
                "[  700.200000] PM: suspend exit\n", "", INTERRUPTS,
                "samsung_fts1ba90a 9-0049 refused to suspend (async, error -16)",
                "samsung_fts1ba90a 9-0049 refused to suspend (async, error -16) once"),
            "a wakeup source": (
                "[  700.000000] PM: suspend entry (deep)\n"
                "[  700.050000] PM: Wakeup pending, aborting suspend\n"
                "[  700.050001] PM: active wakeup source: event2\n"
                "[  700.050002] Freezing user space processes aborted after 0.004 seconds (1 tasks refusing "
                "to freeze, wq_busy=0):\n"
                "[  700.200000] PM: suspend exit\n", "", INTERRUPTS,
                "a pending wakeup (wakeup source event2)", "wakeup source event2 once"),
            "only the kernel's line": (
                "[  700.000000] PM: suspend entry (deep)\n"
                "[  700.050000] Wakeup pending. Abort CPU freeze\n"
                "[  700.200000] PM: suspend exit\n", "", INTERRUPTS,
                "the kernel logged: Wakeup pending. Abort CPU freeze",
                '"Wakeup pending. Abort CPU freeze" once'),
        }
        for name, (log, irq, interrupts, why, by) in cases.items():
            with self.subTest(name):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                if interrupts is not None:
                    (self.root / "proc/interrupts").write_text(interrupts)
                self.touch_abort(1, log=log, irq=irq)
                # The fake root's /proc/interrupts, by its path here.
                why = why.replace("ROOT/", f"{self.root}/")
                result = self.run_test("ro", "1", "aborts=0", "restart-ok", "hold=0")
                self.assertEqual(result.returncode, 1, self.out)
                self.assertIn(f"(rtcwake 1, suspend_stats success +0 fail +1): {why}; the UFS read back as "
                              "before; aborted attempt 1, more than aborts=0", self.out)
                if TOUCH_HINT not in why:
                    self.assertNotIn("the touchscreen", self.out)
                self.assertIn(f"SLEEP-TEST RESULT: NO-SLEEP at cycle 1/1: 1 attempt aborted before the sleep "
                              f"({by}), more than aborts=0;", self.out)

    def test_it_says_before_freezing_not_to_touch_the_screen(self):
        self.run_test("ro", "1", "restart-ok", "hold=0")
        log = (self.ramdir() / "log").read_text()
        said = "do not touch the screen until the restart: a double tap on it wakes the tablet and aborts the sleep it is in"
        self.assertEqual(log.count(said), 1)
        self.assertLess(log.index(said), log.index("freezing every writable disk filesystem now"))

    def test_hold_0_restarts_at_once_and_the_hold_is_the_owner_s(self):
        self.run_test("restart-ok", "1", "hold=0")
        log = (self.ramdir() / "log").read_text()
        self.assertIn("readable without root) for 0s, then the tablet restarts", log)
        for bad in ("hold=3601", "hold=x", "0", "11", "1 45", "1 30 30", "rw", "--restart-ok",
                    "aborts=21", "aborts=x", "aborts=", "dry 1", "dry aborts=3", "ro dry", "dry dry"):
            with self.subTest(bad):
                result = self.run_test(*bad.split(), "restart-ok")
                self.assertEqual(result.returncode, 2, self.out)
                self.assertIn("sleep-test.sh dry [release-floor] [hold=S] [pci=remove] [mode=s2idle]\n"
                              "       [gdsc=pcie0,pcie0phy,usb] [gdsc-ret=pcie0] [cxmx=ret] restart-ok", result.stderr)

    def assert_nothing_done(self):
        self.assertEqual(self.sysrq(), "")
        for calls in ("mount.calls", "swapoff.calls", "rtcwake.calls", "dd.calls", "blockdev.calls",
                      "fsfreeze.calls"):
            self.assertEqual(self.read(calls), "", calls)
        self.assertNotIn("restart", self.read("systemctl.calls"))
        self.assertFalse((self.root / "run/gts9u-sleep-test").exists())
        self.assertFalse((self.kit / "logs").exists())
        self.assertEqual(self.manifest(), self.before)

    def test_refusals_change_nothing(self):
        def mounts(edit):
            path = self.root / "proc/1/mounts"
            path.write_text(edit(path.read_text()))

        cases = {
            "run it with sudo": lambda: (self.bin / "id").write_text("#!/bin/sh\necho 1000\n"),
            "this is not an SM-X710": lambda: (self.root / "proc/device-tree/compatible").write_bytes(
                b"samsung,gts9uwifi\0qcom,sm8550\0"),
            "offers no deep": lambda: (self.root / "sys/power/mem_sleep").write_text("[s2idle]\n"),
            "offers no mem": lambda: (self.root / "sys/power/state").write_text("freeze\n"),
            "a pm_test level is selected": lambda: (self.root / "sys/power/pm_test").write_text(
                "none core processors platform [devices] freezer\n"),
            "no RTC to wake the tablet": lambda: (self.root / "sys/class/rtc/rtc0/wakealarm").unlink(),
            "rtc0 cannot wake the tablet": lambda: (
                self.root / "sys/class/rtc/rtc0/device/power/wakeup").write_text("disabled\n"),
            "no block device": lambda: self.part("userdata").unlink(),
            "is not a partition": lambda: (
                self.root / "sys/devices/soc/ufshc/block/sda/sda34/partition").unlink(),
            "not writable at the block layer": lambda: (
                self.root / "sys/devices/soc/ufshc/block/sda/ro").write_text("1\n"),
            "the root filesystem is not writable (rw,noatime,emergency_ro)": lambda: (
                self.root / "mount-options").write_text("rw,noatime,emergency_ro\n"),
            # 2026-11-22: the root that sysrq u left with errors.
            "ext4 has recorded 5 errors on the root filesystem /dev/sda34 (the last in "
            "ext4_mark_recovery_complete:6370): it needs e2fsck -f from recovery first": lambda: [
                (self.root / "sys/fs/ext4/sda34/errors_count").write_text("5\n"),
                (self.root / "sys/fs/ext4/sda34/last_error_func").write_text("ext4_mark_recovery_complete\n"),
                (self.root / "sys/fs/ext4/sda34/last_error_line").write_text("6370\n")],
            "cannot read ext4's error count of the root": lambda: (
                self.root / "sys/fs/ext4/sda34/errors_count").unlink(),
            "the root filesystem /dev/sda34 is 'clean with errors', not clean (dumpe2fs -h): it needs "
            "e2fsck -f from recovery first": lambda: (self.tmp / "fs-state").write_text("clean with errors\n"),
            "the root filesystem /dev/sda34 is 'unreadable', not clean": lambda: (
                self.bin / "dumpe2fs").write_text("#!/bin/sh\nexit 1\n"),
            "the root filesystem is 'btrfs', not ext4": lambda: mounts(
                lambda text: text.replace("/dev/sda34 / ext4", "/dev/sda34 / btrfs")),
            # The SD card as the desktop mounts it.
            "/media/jstockdale/squirtle (/dev/mmcblk1p1, exfat) is mounted writable and cannot be frozen: "
            "unmount it first (sudo umount /media/jstockdale/squirtle)": lambda: mounts(
                lambda text: text + "/dev/mmcblk1p1 /media/jstockdale/squirtle exfat rw,nosuid,nodev,"
                "relatime,uid=1000,gid=1000,fmask=0022,dmask=0022,iocharset=utf8,errors=remount-ro 0 0\n"),
            "battery at 12 %": lambda: (
                self.root / "sys/class/power_supply/sm5714-battery/capacity").write_text("12\n"),
            "wait until the boot has settled": lambda: (self.root / "proc/uptime").write_text("70.5 9\n"),
            "the boot-time panel cycle is still running": lambda: (
                self.tmp / "active-ubuntu-gts9u-panel-coldboot-recover.service").write_text("activating\n"),
            "the audio DSP is still starting": lambda: (
                self.tmp / "active-ubuntu-gts9u-adsp-start-inhibit.service").write_text("active\n"),
            "missing tool: rtcwake": lambda: (self.bin / "rtcwake").unlink(),
            "missing tool: fsfreeze": lambda: (self.bin / "fsfreeze").unlink(),
            "missing tool: dumpe2fs": lambda: (self.bin / "dumpe2fs").unlink(),
            # The first resume's warning, and the hung tasks of a frozen disk.
            "kernel.panic_on_warn is 1, not 0: a frozen disk gives a kernel warning at the first resume and "
            "hung tasks after a minute, and either would panic the tablet in the middle of the run": lambda: (
                self.root / "proc/sys/kernel/panic_on_warn").write_text("1\n"),
            "kernel.hung_task_panic is unreadable, not 0": lambda: (
                self.root / "proc/sys/kernel/hung_task_panic").unlink(),
            # errors=panic, among the options or as the superblock's default.
            "/media/stick (/dev/sdg2) runs with errors=panic: an ext4 error during the run would panic the "
            "tablet; give it errors=remount-ro first (sudo mount -o remount,errors=remount-ro "
            "/media/stick)": lambda: mounts(lambda text: text.replace("errors=remount-ro", "errors=panic")),
            "/ (/dev/sda34) runs with errors=panic": lambda: (self.tmp / "sb-errors-sda34").write_text("panic\n"),
            # Without the superblock's default, the check of errors=continue
            # could not be judged.
            "cannot tell ext4's error behaviour of /media/stick (/dev/sdg2): dumpe2fs -h names no default in its "
            "superblock, or its options an unknown one": lambda: (self.tmp / "sb-errors-sdg2").write_text("unknown\n"),
            # A mount point that /proc/1/mounts has to escape.
            "the mount point /media/my\\040stick has a space or the like in its path, which the run cannot "
            "name: unmount it first": lambda: mounts(
                lambda text: text + "/dev/sdh1 /media/my\\040stick ext4 rw,relatime 0 0\n"),
        }
        for message, damage in cases.items():
            with self.subTest(message):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                damage()
                result = self.run_test("ro", "restart-ok")
                self.assertEqual(result.returncode, 1, self.out)
                self.assertIn("REFUSED: ", self.out)
                self.assertIn(message, self.out)
                self.assertIn("nothing was changed", self.out)
                self.assert_nothing_done()
        with self.subTest("no restart-ok"):
            FakeTablet.setUp(self)
            SleepTestTest.setUp(self)
            result = self.run_test("ro")
            self.assertEqual(result.returncode, 1, self.out)
            self.assertIn("add restart-ok to say that is fine", self.out)
            self.assert_nothing_done()

    def test_swap_on_the_disk_goes_off_or_nothing_is_frozen(self):
        (self.tmp / "swapoff-fails").touch()
        result = self.run_test("restart-ok")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("STOPPED before freezing anything: swapoff /swapfile failed", self.out)
        self.assertEqual(self.sysrq(), "")
        self.assertEqual(self.read("fsfreeze.calls"), "--version\n")

    def test_status_reads_the_result_without_root(self):
        (self.bin / "id").write_text("#!/bin/sh\necho 1000\n")
        result = self.run_script("sleep-test.sh", "status")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("no sleep test in", result.stdout)
        # The command it gives reads the newest archive's pmsg, in order.
        (command,) = [line.strip() for line in result.stdout.splitlines() if "grep -rah" in line]
        archive = self.root / "var/lib/ubuntu-gts9u/pstore"
        for seq, stamp in (("000009", "20261122-020000"), ("000010", "20261122-031500")):
            d = archive / f"{seq}-20261122T110000Z-boot-788ce779"
            d.mkdir(parents=True)
            (d / "pmsg-ramoops-0").write_text(
                f"gts9u sleep-test: {stamp} cycle 1/1: diagnostic: x\n"
                f"gts9u sleep-test: {stamp} SLEEP-TEST RESULT: FAIL at cycle 1/1: y\n")
            (d / "console-ramoops-0").write_text("[  1.0] Booting Linux\n")
        shown = subprocess.run(["sh", "-c", command], capture_output=True, text=True, check=True).stdout
        self.assertEqual(shown, "gts9u sleep-test: 20261122-031500 cycle 1/1: diagnostic: x\n"
                                "gts9u sleep-test: 20261122-031500 SLEEP-TEST RESULT: FAIL at cycle 1/1: y\n")
        # A run whose only verdict so far is an aborted attempt is still
        # going: the cycle is tried again.
        going = self.root / "run/gts9u-sleep-test/20261122-032843"
        going.mkdir(parents=True)
        (self.root / "run/gts9u-sleep-test/latest").symlink_to("20261122-032843")
        (going / "log").write_text("[13873.80] cycle 1/3: asleep ?s\n")
        (going / "result").write_text("SLEEP-TEST cycle 1/3: ABORTED: the kernel gave up the suspend\n")
        result = self.run_script("sleep-test.sh", "status")
        self.assertIn("SLEEP-TEST cycle 1/3: ABORTED: the kernel gave up the suspend\nno SLEEP-TEST RESULT "
                      "line yet: the run is still going (an ABORTED cycle is tried again)\n", result.stdout)
        (going / "result").write_text((going / "result").read_text() + "SLEEP-TEST RESULT: NO-SLEEP at cycle "
                                      "1/3: x\n")
        result = self.run_script("sleep-test.sh", "status")
        self.assertIn("SLEEP-TEST RESULT: NO-SLEEP at cycle 1/3: x\n", result.stdout)
        self.assertNotIn("still going", result.stdout)
        shutil.rmtree(going)
        (self.root / "run/gts9u-sleep-test/latest").unlink()
        (self.bin / "id").write_text("#!/bin/sh\necho 0\n")
        self.run_test("restart-ok", "1", "hold=0")
        (self.bin / "id").write_text("#!/bin/sh\necho 1000\n")
        result = self.run_script("sleep-test.sh", "status")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SLEEP-TEST RESULT: PASS: 1 of 1", result.stdout)
        self.assertNotIn("still going", result.stdout)
        for p in [self.ramdir(), *self.ramdir().iterdir()]:
            if p.is_file():
                self.assertTrue(p.stat().st_mode & 0o004, p)

    def test_the_run_ends_without_its_terminal(self):
        """SSH drops in the sleep: the part on the disk dies with it, and the
        run in its own session goes on to its result in /run."""
        path = self.tmp / "run-sleep-test.sh"
        path.write_text(self.script("sleep-test.sh"))
        env = {"PATH": f"{self.bin}:{self.host_path}", "FAKE_ROOT": str(self.root),
               "HOME": str(self.tmp)}
        # The sleep is slow here, so that the terminal is gone in the middle.
        (self.tmp / "stubs/rtcwake").write_text(
            (self.tmp / "stubs/rtcwake").read_text().replace("exit \"$(cat", "/usr/bin/sleep 1\nexit \"$(cat"))
        run = subprocess.Popen(["sh", str(path), "restart-ok", "2", "hold=0"], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               start_new_session=True)
        self.addCleanup(run.wait)
        seen = b""
        while b"freezing every writable disk filesystem now" not in seen:
            chunk = run.stdout.read1(4096)
            self.assertTrue(chunk, seen)
            seen += chunk
        run.stdout.close()
        os.killpg(run.pid, 9)
        run.wait()
        deadline = time.monotonic() + 60
        result = self.root / "run/gts9u-sleep-test/latest/result"
        while "SLEEP-TEST RESULT" not in (result.read_text() if result.exists() else ""):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        while self.sysrq() != "b":
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        # The run's last line, after sysrq b (here the tablet stays up):
        # nothing of it writes to the test's directory after that.
        log = self.root / "run/gts9u-sleep-test/latest/log"
        while "restart: still here after sysrq b" not in log.read_text():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        time.sleep(0.2)
        self.assertIn("SLEEP-TEST RESULT: PASS: 2 of 2", result.read_text())
        self.assertEqual(self.read("violations"), "")

    # The experiment options (2026-11-22): pci=remove, pm_test=, trace.

    def pci_tree(self, devices=(("0000:00:00.0", "0x060400", ""), ("0000:01:00.0", "0x028000", "0000:00:00.0"))):
        """PCI devices as sysfs shows them, by default the SM-X710's: the
        root port 0000:00:00.0 on pcie0's host bridge (pci0000:00), the
        Wi-Fi 0000:01:00.0 below it.  DEVICES: (name, class, the device it
        sits below, or "" for the host bridge).  Each name is a link in
        /sys/bus/pci/devices to its directory, which has its class and a
        remove file (PCI_REMOVE_FAKE removes them)."""
        bridge = self.root / "sys/devices/platform/soc@0/1c00000.pcie/pci0000:00"
        bus = self.root / "sys/bus/pci/devices"
        bus.mkdir(parents=True, exist_ok=True)
        paths = {}
        for name, cls, parent in devices:
            d = (paths[parent] if parent else bridge) / name
            d.mkdir(parents=True)
            (d / "class").write_text(cls + "\n")
            (d / "remove").write_text("")
            (d / "power_state").write_text("D0\n")
            paths[name] = d
            (bus / name).symlink_to(os.path.relpath(d, bus))
        return paths

    def progress_lines(self):
        return [line for line in self.pmsg_lines(progress=True) if line.startswith("progress: ")]

    def test_pci_remove_takes_the_root_port_off_after_the_checks_and_before_the_freeze(self):
        """The root port (and the Wi-Fi below it) goes in RAM, after every
        check and errors=continue, right before the first freeze and the
        first evidence: the bus is empty for the whole run, and the run says
        so before it moves to RAM, in its log, its record and pmsg."""
        (self.root / "dev/pmsg0").write_text("")
        self.pci_tree()
        result = self.run_test("ro", "1", "pci=remove", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        # One removal, of the root port alone, before the first freeze.
        self.assertEqual(self.read("pci-remove.calls"), "0000:00:00.0 before\n")
        self.assertEqual(list((self.root / "sys/bus/pci/devices").iterdir()), [])
        log = (self.ramdir() / "log").read_text()
        order = ["pci=remove: right before it freezes the disks the run removes the PCI root port(s) 0000:00:00.0 "
                 "and everything below them, the Wi-Fi with them: Wi-Fi is gone from then until the restart, and "
                 "an SSH session over Wi-Fi drops there while the run carries on",
                 "errors=continue: / (/dev/sda34)",
                 "pci=remove: removing the PCI root port(s) 0000:00:00.0 and everything below them (2 PCI devices: "
                 "0000:00:00.0 0000:01:00.0); Wi-Fi is gone from now until the restart, and an SSH session over "
                 "Wi-Fi drops now: the run goes on",
                 "pci=remove: removed 0000:00:00.0; PCI devices 2 -> 0; 10s for the system to settle before the "
                 "freeze",
                 "freezing every writable disk filesystem now", "frozen: /media/stick", "evidence: state:",
                 "cycle 1/1: deep sleep"]
        self.assertEqual([log.index(said) for said in order], sorted(log.index(said) for said in order))
        self.assertRegex(self.out, r"cycle 1/1: evidence: before: [^\n]*; pci none; ")
        self.assertIn("SLEEP-TEST RESULT: PASS: 1 of 1 cycles resumed with the UFS readable, 1 of them deep; floor "
                      "released; kernel 68d243e908c0; options: pci=remove", self.out)
        (logs,) = (self.kit / "logs").glob("*-sleep")
        before = (logs / "before").read_text()
        self.assertIn("then a 0s hold and a restart (sysrq b); options: pci=remove\n", before)
        self.assertIn("PCI: 2 devices: 0000:00:00.0 0000:01:00.0; root ports: 0000:00:00.0; pci=remove: all of them "
                      "go right before the freeze\n", before)
        self.assertEqual(self.progress_lines()[:3], [
            "progress: start: ro, 1 cycles of 30s, aborts=5, hold=0s; options: pci=remove; floor released; kernel "
            "68d243e908c0; up 600.25s",
            "progress: pci=remove: removed 0000:00:00.0; PCI devices 2 -> 0",
            "progress: protected: frozen: /media/stick /; /dev/sda read-only; from here the run ends with a restart"])
        self.assertEqual(self.sysrq(), "b")

    def test_dry_with_pci_remove_removes_it_before_the_freeze_and_sleeps_not(self):
        self.pci_tree()
        result = self.run_test("dry", "pci=remove", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        self.assertEqual(self.read("pci-remove.calls"), "0000:00:00.0 before\n")
        self.assertEqual(self.read("rtcwake.calls"), "--version\n")
        self.assertIn("SLEEP-TEST RESULT: DRY: no sleep tried;", self.out)
        self.assertIn("floor released; kernel 68d243e908c0; options: pci=remove", self.out)

    def test_pci_remove_is_refused_with_nothing_changed_when_it_would_not_empty_the_bus(self):
        cases = {
            "pci=remove: no PCI root port to remove (0 PCI devices)": (),
            # An endpoint on the host bridge, no bridge.
            "pci=remove: no PCI root port to remove (1 PCI devices: 0000:00:00.0)": (
                ("0000:00:00.0", "0x028000", ""),),
            # A device on the root bus beside the root port.
            "pci=remove: 0000:00:01.0 not below a PCI root port (0000:00:00.0): removing the root ports would not "
            "empty the bus": (("0000:00:00.0", "0x060400", ""), ("0000:01:00.0", "0x028000", "0000:00:00.0"),
                              ("0000:00:01.0", "0x058000", "")),
        }
        for message, devices in cases.items():
            with self.subTest(message):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                if devices:
                    self.pci_tree(devices)
                result = self.run_test("ro", "pci=remove", "restart-ok")
                self.assertEqual(result.returncode, 1, self.out)
                self.assertIn("REFUSED: " + message, self.out)
                self.assertIn("nothing was changed", self.out)
                self.assert_nothing_done()
                self.assertEqual(self.read("pci-remove.calls"), "")
                self.assertEqual(len(list((self.root / "sys/bus/pci/devices").glob("*"))), len(devices))

    def test_a_removal_that_leaves_a_pci_device_stops_the_run_before_anything_is_frozen(self):
        """The bus is not empty after the root port's removal: the run stops
        before the freeze, errors= back, no restart, and says that what was
        removed stays removed until a restart."""
        (self.root / "dev/pmsg0").write_text("")
        self.pci_tree()
        (self.tmp / "pci-remove-keeps").write_text("0000:01:00.0\n")
        result = self.run_test("ro", "1", "pci=remove", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("SLEEP-TEST RESULT: STOPPED before any sleep: pci=remove: 1 of 2 PCI devices still there after "
                      "removing 0000:00:00.0: 0000:01:00.0; every filesystem this run froze is thawed again, errors= "
                      "is back as it was on: /media/stick /, and the tablet is not restarted; floor released; kernel "
                      "68d243e908c0; options: pci=remove", self.out)
        self.assertIn("NEXT: what was removed stays removed until the restart, the Wi-Fi with it (sudo reboot brings "
                      "it back)", self.out)
        self.assertEqual(self.read("pci-remove.calls"), "0000:00:00.0 before\n")
        self.assertEqual(self.sysrq(), "")
        self.assertEqual(self.read("fsfreeze.calls"), "--version\n")
        self.assertEqual(self.read("rtcwake.calls").count("-m mem"), 0)
        self.assertEqual(self.errors_mode(), {"/": [], "/media/stick": ["errors=remount-ro"]})
        self.assertIn("SLEEP-TEST RESULT: STOPPED before any sleep: pci=remove:", "\n".join(self.pmsg_lines()))

    def test_a_pcie_host_that_did_not_come_back_ends_a_pci_remove_run(self):
        """With pci=remove, a cycle or an aborted attempt whose kernel lines
        say the PCIe host did not come back ends the run: the host is powered
        down but still marked suspended, and the next suspend would read its
        registers unclocked.  Without pci=remove the same lines change
        nothing (the host never powered down)."""
        # The kernel's own lines: the link wait's, then the PM core's, which
        # has "PM: " after the device (dev_fmt, drivers/base/power/main.c).
        link = "qcom-pcie 1c00000.pcie: Link failed to come up. LTSSM: DW_PCIE_LTSSM_POLL_CONFIG"
        core = "qcom-pcie 1c00000.pcie: PM: failed to resume async noirq: error -110"
        down = f"[  731.000000] {link}\n[  731.000001] {core}\n"

        def deep_log(lines):
            return ("[  700.000000] PM: suspend entry (deep)\n"
                    "[  730.000000] Timekeeping suspended for 30.002 seconds\n" + lines +
                    "[  731.100000] PM: suspend exit\n")
        for name, pci, setup, sleeps, first in (
                ("a cycle", True, lambda: self.cycle(1, log=deep_log(down)), 1, link),
                ("the PM core's line alone", True,
                 lambda: self.cycle(1, log=deep_log(f"[  731.000001] {core}\n")), 1, core),
                ("an aborted attempt", True, lambda: self.touch_abort(1, log=TOUCH_ABORT.replace(
                    "[13861.416262] PM: suspend exit\n", down + "[13861.416262] PM: suspend exit\n")), 1, link),
                ("without pci=remove", False, lambda: self.cycle(1, log=deep_log(down)), 3, None)):
            with self.subTest(name):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                (self.root / "proc/interrupts").write_text(INTERRUPTS)
                if pci:
                    self.pci_tree()
                setup()
                result = self.run_test("ro", "3", *(("pci=remove",) if pci else ()), "restart-ok", "hold=0")
                self.assertEqual(result.returncode, 1, self.out)
                self.assert_read_only_rules()
                self.assertEqual(self.read("rtcwake.calls").count("-m mem"), sleeps)
                self.assertEqual(self.sysrq(), "b")
                if pci:
                    self.assertIn("SLEEP-TEST RESULT: PCIE-DOWN after cycle 1/3: the PCIe host did not come back "
                                  f"({first}), and with "
                                  "pci=remove a later suspend would read its registers with its clocks off, so the "
                                  "run ends here;", self.out)
                    self.assertIn("; options: pci=remove", self.out)
                    self.assertNotIn("again in 5 s", self.out)
                else:
                    self.assertNotIn("PCIE-DOWN", self.out)
                    self.assertIn("SLEEP-TEST RESULT: PASS: 3 of 3", self.out)

    def test_a_pcie_host_failure_with_the_storage_back_is_not_called_a_lost_storage(self):
        """2026-11-22, revision 9's gdsc=pcie0 runs: the PCIe host's `Host
        init failed` (and its PHY's `phy initialization timed-out`) are in
        the UFS's words, so the cycle fails, but the storage read back as
        before.  The NEXT line says that and names the PCIe host's line, the
        restart says the storage read back, and the cycle's evidence says
        that PCIe did not come back; until revision 10 the NEXT line said
        "the storage did not come back".  A UFS failure line, or a read that
        failed, is still the storage's."""
        host = "qcom-pcie 1c00000.pcie: Host init failed: -16"
        phy = "qcom-qmp-pcie-phy 1c06000.phy: phy initialization timed-out"
        ufs = "qcom-qmp-ufs-phy 1d80000.phy: phy initialization timed-out"

        def deep_log(lines):
            return ("[  700.000000] PM: suspend entry (deep)\n"
                    "[  700.100000] qcom-pcie 1c00000.pcie: Timeout waiting for L2 entry! LTSSM: 0x11\n"
                    "[  730.000000] Timekeeping suspended for 30.002 seconds\n" + lines +
                    "[  731.100000] PM: suspend exit\n")
        for name, lines, flags, storage in (
                ("the PCIe host", f"[  731.000000] {host}\n", (), False),
                ("the PCIe PHY and host", f"[  730.900000] {phy}\n[  731.000000] {host}\n", (), False),
                ("and a UFS line", f"[  730.900000] {ufs}\n[  731.000000] {host}\n", (), True),
                ("and a read that failed", f"[  731.000000] {host}\n", ("storage-dead",), True)):
            with self.subTest(name):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                (self.root / "dev/pmsg0").write_text("")
                self.pci_tree()
                self.cycle(1, *flags, log=deep_log(lines))
                result = self.run_test("ro", "1", "pci=remove", "restart-ok", "hold=0")
                self.assertEqual(result.returncode, 1, self.out)
                self.assert_read_only_rules()
                self.assertEqual(self.sysrq(), "b")
                self.assertRegex(self.out, r"SLEEP-TEST cycle 1/1: FAIL: [0-9]+ failure lines in the kernel log, "
                                           "the first: ")
                self.assertIn(f"cycle 1/1: evidence: pcie: qcom-pcie 1c00000.pcie: Timeout waiting for L2 entry! "
                              f"LTSSM: 0x11; PCIe did not come back ({host})\n", self.out)
                pmsg = self.pmsg_lines()
                self.assertTrue([line for line in pmsg if line.startswith(
                    "cycle 1/1: evidence: pcie: qcom-pcie 1c00000.pcie: Timeout waiting for L2 entry! LTSSM: 0x11; "
                    f"PCIe did not come back ({host}); irq ")], pmsg)
                if storage:
                    self.assertIn("NEXT: the storage did not come back; after the restart run sh "
                                  "~/typec-roles/verify.sh", self.out)
                    self.assertNotIn("the storage came back", self.out)
                    self.assertIn("restart: sysrq b; every disk filesystem frozen since before the first sleep",
                                  self.out)
                else:
                    self.assertIn("NEXT: the storage came back (it read back as before, and no UFS failure line); "
                                  f"the PCIe host did not ({host}): the restart brings it back. After it, sh "
                                  "~/typec-roles/verify.sh and cat "
                                  f"{self.root}/sys/fs/ext4/sda34/errors_count (0); the cycle's pcie evidence and "
                                  "kernel lines above say how the host failed", self.out)
                    self.assertIn("NEXT: the storage came back", "\n".join(pmsg))
                    self.assertNotIn("the storage did not come back", self.out)
                    self.assertIn("restart: sysrq b; every disk filesystem frozen; the storage read back as before",
                                  self.out)
                # Either way not a PCIE-DOWN: the cycle failed first.
                self.assertNotIn("PCIE-DOWN", self.out)

    def test_pm_test_cycles_say_pm_test_and_never_a_depth(self):
        """pm_test=platform: set right before each rtcwake, none again right
        after; each cycle PM-TEST, the run PM-TEST, never PASS, SHALLOW, DEEP
        or a depth."""
        (self.root / "dev/pmsg0").write_text("")
        # In the second qcom_stats counted the SoC's states during the wait.
        # On the SM-X710 that cannot happen at platform (the prepared RPMh RSC
        # keeps the CPU cluster's domain on, docs/porting-log.md, 2026-11-22),
        # but the counts are shown as read, and they are still no depth.
        d = self.tmp / "cycle-2/qcom_stats"
        d.mkdir(parents=True)
        for q, n in (("aosd", 8), ("cxsd", 6), ("ddr", 7)):
            (d / q).write_text(qcom_stat(n, 1))
        result = self.run_test("ro", "2", "pm_test=platform", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        for i, counts in ((1, "aosd +0 cxsd +0 ddr +0"), (2, "aosd +1 cxsd +1 ddr +1")):
            self.assertIn(f"SLEEP-TEST cycle {i}/2: PM-TEST platform: the suspend went as far as pm_test=platform "
                          "(the devices suspended through their noirq phase), waited there and came back with no "
                          f"SoC sleep call, and the UFS read back as before; qcom_stats {counts} (CPU idle during "
                          "the wait, not a depth test); floor released", self.out)
        self.assertIn("SLEEP-TEST RESULT: PM-TEST platform: 2 of 2 cycles went as far as pm_test=platform and came "
                      "back with the UFS readable; no SoC sleep call, so no depth was tested; floor released; kernel "
                      "68d243e908c0; options: pm_test=platform", self.out)
        verdicts = [line for line in self.out.splitlines() if "SLEEP-TEST " in line]
        self.assertEqual(len(verdicts), 4, verdicts)
        for word in ("PASS", "SHALLOW", "DEEP", "deep", "DEPTH"):
            self.assertFalse([line for line in verdicts if word in line], word)
        # The level for each sleep, and none before, between and after.
        self.assertEqual(self.read("knobs-at-sleep"), "pm_test=platform printk=5 4 1 7 console_suspend=Y\n" * 2)
        self.assertEqual((self.root / "sys/power/pm_test").read_text(), "none")
        self.assertEqual(self.read("rtcwake.calls").count("-u -d rtc0 -m mem -s 30"), 2)
        self.assertEqual(self.sysrq(), "b")
        log = (self.ramdir() / "log").read_text()
        self.assertIn("cycle 1/2: pm_test=platform cycle: rtcwake -m mem -s 30, and the suspend stops at platform (the "
                      "devices suspended through their noirq phase), waits 5s there and comes back, no SoC sleep call;", log)
        self.assertIn("cycle 1/2: for this sleep: pm_test platform", log)
        self.assertIn("pm_test=platform: each cycle's suspend stops at platform", log)
        (logs,) = (self.kit / "logs").glob("*-sleep")
        self.assertIn("2 pm_test=platform cycles (rtcwake -m mem -s 30; the suspend stops at platform, waits 5s and "
                      "comes back: no SoC sleep call) with every disk filesystem frozen", (logs / "before").read_text())
        self.assertIn(f"progress: cycle 1/2: entering the sleep, rtcwake -m mem -s 30; pm_test platform; {NO_SWITCHES}; up ",
                      "\n".join(self.progress_lines()))

    def test_each_pm_test_level_says_how_far_its_suspend_went(self):
        for level, what in (("freezer", "the tasks frozen, no device suspended"),
                            ("devices", "the devices' suspend phase, not their late and noirq ones"),
                            ("platform", "the devices suspended through their noirq phase"),
                            ("processors", "the devices suspended and the other CPUs offline"),
                            ("core", "everything but the SoC sleep call (PSCI SYSTEM_SUSPEND), interrupts off")):
            with self.subTest(level):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                result = self.run_test("ro", "1", f"pm_test={level}", "restart-ok", "hold=0")
                self.assertEqual(result.returncode, 1, self.out)
                self.assertIn(f"SLEEP-TEST cycle 1/1: PM-TEST {level}: the suspend went as far as pm_test={level} "
                              f"({what}), waited there", self.out)
                self.assertEqual(self.read("knobs-at-sleep"), f"pm_test={level} printk=5 4 1 7 console_suspend=Y\n")
                self.assertEqual((self.root / "sys/power/pm_test").read_text(), "none")

    def test_a_pm_test_cycle_that_did_not_stop_at_its_level_fails(self):
        """The kernel's "suspend debug: Waiting for" line is the proof that
        the suspend stopped at the level; without it the cycle FAILs."""
        self.cycle(1, log="[  700.000000] PM: suspend entry (deep)\n"
                          "[  730.000000] Timekeeping suspended for 30.002 seconds\n"
                          "[  730.100000] PM: suspend exit\n")
        result = self.run_test("ro", "1", "pm_test=devices", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("SLEEP-TEST cycle 1/1: FAIL: no 'suspend debug: Waiting for' line: the suspend did not stop at "
                      f"pm_test=devices (aosd +0 cxsd +0 ddr +0; floor released; {NO_SWITCHES})", self.out)
        self.assertIn("SLEEP-TEST RESULT: FAIL at cycle 1/1: no 'suspend debug: Waiting for' line", self.out)
        self.assertEqual((self.root / "sys/power/pm_test").read_text(), "none")

    def test_an_aborted_pm_test_attempt_is_tried_again_with_its_level(self):
        (self.root / "proc/interrupts").write_text(INTERRUPTS)
        (self.root / "dev/pmsg0").write_text("")
        self.touch_abort(1)
        result = self.run_test("ro", "1", "pm_test=platform", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("SLEEP-TEST cycle 1/1: ABORTED: the kernel gave up the suspend before the tablet slept", self.out)
        self.assertIn("SLEEP-TEST cycle 1/1: PM-TEST platform:", self.out)
        self.assertEqual(self.read("knobs-at-sleep"), "pm_test=platform printk=5 4 1 7 console_suspend=Y\n" * 2)
        self.assertEqual((self.root / "sys/power/pm_test").read_text(), "none")
        self.assertEqual([line.split("; up ")[0] for line in self.progress_lines() if "entering" in line], [
            f"progress: cycle 1/1: entering the sleep, rtcwake -m mem -s 30; pm_test platform; {NO_SWITCHES}",
            f"progress: cycle 1/1: entering the sleep, rtcwake -m mem -s 30, attempt 2; pm_test platform; {NO_SWITCHES}"])

    def test_a_pm_test_level_that_does_not_take_sleeps_not(self):
        """The level was offered at the checks and is refused right before
        the sleep (here the fake kernel locks the file once the first read
        has run): that cycle does not start, and the run ends as ABORTED,
        with the restart."""
        dd = self.tmp / "stubs/dd"
        dd.write_text(dd.read_text().replace(RAM_CALL, RAM_CALL + 'chmod 0444 "$F/sys/power/pm_test"\n', 1))
        result = self.run_test("ro", "1", "pm_test=platform", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("SLEEP-TEST cycle 1/1: NOT STARTED: pm_test=platform did not take "
                      f"({self.root}/sys/power/pm_test: [none] core processors platform devices freezer)", self.out)
        self.assertIn("SLEEP-TEST RESULT: ABORTED at cycle 1/1, before its sleep (pm_test=platform did not take", self.out)
        self.assertIn("; options: pm_test=platform", self.out)
        self.assertEqual(self.read("rtcwake.calls").count("-m mem"), 0)
        self.assertEqual(self.sysrq(), "b")

    def test_the_knobs_are_never_left_set_by_a_run_that_stops_before_a_sleep(self):
        """A stop before any sleep (here a freeze that fails) does not
        restart the tablet: pm_test, the console loglevel and console_suspend
        are as before the run, since they are set only around rtcwake."""
        (self.tmp / "freeze-fails").write_text("/media/stick\n")
        result = self.run_test("ro", "1", "pm_test=platform", "trace=live", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("SLEEP-TEST RESULT: STOPPED before any sleep:", self.out)
        self.assertIn("options: pm_test=platform trace=live", self.out)
        self.assertEqual(self.sysrq(), "")
        self.assertEqual(self.read("knobs-at-sleep"), "")
        self.assertEqual((self.root / "sys/power/pm_test").read_text(),
                         "[none] core processors platform devices freezer\n")
        self.assertEqual((self.root / "proc/sys/kernel/printk").read_text(), "5\t4\t1\t7\n")
        self.assertEqual((self.root / "sys/module/printk/parameters/console_suspend").read_text(), "Y\n")

    def test_trace_raises_the_console_loglevel_for_each_sleep_alone(self):
        (self.root / "dev/pmsg0").write_text("")
        result = self.run_test("ro", "2", "trace", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        self.assertEqual(self.read("knobs-at-sleep"),
                         "pm_test=[none] core processors platform devices freezer printk=8 4 1 7 "
                         "console_suspend=Y\n" * 2)
        # Back after each sleep: the other three numbers as they were.
        self.assertEqual((self.root / "proc/sys/kernel/printk").read_text(), "5 4 1 7\n")
        self.assertEqual((self.root / "sys/module/printk/parameters/console_suspend").read_text(), "Y\n")
        self.assertEqual((self.root / "sys/power/pm_debug_messages").read_text(), "1")
        self.assertIn("cycle 1/2: for this sleep: console loglevel 8", (self.ramdir() / "log").read_text())
        self.assertIn("SLEEP-TEST RESULT: PASS: 2 of 2 cycles resumed with the UFS readable, 2 of them deep; floor "
                      "released; kernel 68d243e908c0; options: trace", self.out)
        self.assertIn(f"progress: cycle 2/2: entering the sleep, rtcwake -m mem -s 30; console loglevel 8; {NO_SWITCHES}; up ",
                      "\n".join(self.progress_lines()))

    def test_trace_live_also_keeps_the_consoles_on_for_each_sleep_alone(self):
        result = self.run_test("ro", "2", "trace=live", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertEqual(self.read("knobs-at-sleep"),
                         "pm_test=[none] core processors platform devices freezer printk=8 4 1 7 "
                         "console_suspend=N\n" * 2)
        self.assertEqual((self.root / "sys/module/printk/parameters/console_suspend").read_text(), "Y")
        self.assertEqual((self.root / "proc/sys/kernel/printk").read_text(), "5 4 1 7\n")
        self.assertIn("cycle 1/2: for this sleep: console loglevel 8, console_suspend N",
                      (self.ramdir() / "log").read_text())
        self.assertIn("trace=live: console_suspend N for each sleep (Y otherwise)", self.out)
        self.assertIn("options: trace=live", self.out)

    def test_all_three_options_are_named_in_the_header_pmsg_and_result(self):
        (self.root / "dev/pmsg0").write_text("")
        self.pci_tree()
        result = self.run_test("ro", "1", "pci=remove", "pm_test=core", "trace=live", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        options = "options: pci=remove pm_test=core trace=live"
        self.assertEqual(self.read("knobs-at-sleep"), "pm_test=core printk=8 4 1 7 console_suspend=N\n")
        self.assertIn(f"SLEEP-TEST RESULT: PM-TEST core: 1 of 1 cycles went as far as pm_test=core and came back with "
                      f"the UFS readable; no SoC sleep call, so no depth was tested; floor released; kernel 68d243e908c0; "
                      f"{options}", self.out)
        (logs,) = (self.kit / "logs").glob("*-sleep")
        self.assertEqual((logs / "before").read_text().splitlines()[0].split("; ")[-1], options)
        self.assertIn(f"; {options}; ", self.progress_lines()[0])
        self.assertTrue(self.progress_lines()[-1].startswith(
            "progress: cycle 1/1: entering the sleep, rtcwake -m mem -s 30; pm_test core, console loglevel 8, "
            f"console_suspend N; {NO_SWITCHES}; up "), self.progress_lines())

    def test_progress_lines_reach_pmsg_before_each_sleep(self):
        """A run that never comes back from a sleep leaves in pmsg its start
        with its options, the protection and the sleep it entered: each
        sleep (each attempt) finds its own line the last one there."""
        (self.root / "proc/interrupts").write_text(INTERRUPTS)
        (self.root / "dev/pmsg0").write_text("")
        self.touch_abort(1)
        result = self.run_test("ro", "2", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        progress = self.progress_lines()
        self.assertEqual(progress, [
            "progress: start: ro, 2 cycles of 30s, aborts=5, hold=0s; options: none; floor released; kernel "
            "68d243e908c0; up 600.25s",
            "progress: protected: frozen: /media/stick /; /dev/sda read-only; from here the run ends with a restart",
            f"progress: cycle 1/2: entering the sleep, rtcwake -m mem -s 30; no pm_test, no trace; {NO_SWITCHES}; up 600.25s",
            f"progress: cycle 1/2: entering the sleep, rtcwake -m mem -s 30, attempt 2; no pm_test, no trace; {NO_SWITCHES}; up "
            "600.25s",
            f"progress: cycle 2/2: entering the sleep, rtcwake -m mem -s 30; no pm_test, no trace; {NO_SWITCHES}; up 600.25s"])
        # The first two lines in pmsg; each sleep's own the last when it began.
        self.assertEqual(self.pmsg_lines(progress=True)[:2], progress[:2])
        prefix = f"gts9u sleep-test: {self.ramdir().name} "
        self.assertEqual([line[len(prefix):] for line in self.read("pmsg-at-sleep").splitlines()], progress[2:])
        self.assertIn("SLEEP-TEST RESULT: PASS: 2 of 2 cycles resumed with the UFS readable, 2 of them deep; 1 attempt "
                      "aborted before the sleep (IRQ 199 (fts1ba90a, the touchscreen) once) and tried again; floor "
                      "released; kernel 68d243e908c0; options: none", self.out)
        # Each cut at 280 characters past "progress: "; the result file and
        # the kernel log have none of them.
        for line in (self.root / "dev/pmsg0").read_text().splitlines():
            if line.startswith(prefix + "progress: "):
                self.assertLessEqual(len(line), len(prefix + "progress: ") + 280, line)
        self.assertIn("%.280s", re.search(r"^progress\(\) \{\n.*?^\}\n", self.script("sleep-test.sh"),
                                          re.M | re.S).group(0))
        self.assertNotIn("progress:", (self.ramdir() / "result").read_text())
        self.assertNotIn("progress:", (self.root / "dev/kmsg").read_text())

    def test_the_options_a_kernel_cannot_serve_are_refused_with_nothing_changed(self):
        # (option, what the message says with ROOT for the fake root, the
        # file under it, and what is done to that file).
        cases = (
            ("pm_test=freezer", "pm_test=freezer: this kernel does not offer that level (ROOT/sys/power/pm_test: "
             "[none] core processors platform devices)", "sys/power/pm_test",
             "[none] core processors platform devices\n"),
            ("pm_test=platform", "pm_test=platform: no ROOT/sys/power/pm_test (a kernel without CONFIG_PM_DEBUG)",
             "sys/power/pm_test", None),
            ("trace", "trace: ROOT/sys/power/pm_print_times is 1: its two lines per device callback, thousands per "
             "sleep, would go through the consoles at 0.1 to 0.25 s each with interrupts off",
             "sys/power/pm_print_times", "1\n"),
            ("trace=live", "trace=live: no ROOT/sys/power/pm_debug_messages (a kernel without "
             "CONFIG_PM_SLEEP_DEBUG)", "sys/power/pm_debug_messages", None),
            ("trace", "trace: cannot read the console loglevel in ROOT/proc/sys/kernel/printk",
             "proc/sys/kernel/printk", "x\n"),
            ("trace=live", "trace=live: cannot read ROOT/sys/module/printk/parameters/console_suspend",
             "sys/module/printk/parameters/console_suspend", None),
        )
        for option, message, path, content in cases:
            with self.subTest(message):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                if content is None:
                    (self.root / path).unlink()
                else:
                    (self.root / path).write_text(content)
                result = self.run_test("ro", "1", option, "restart-ok")
                self.assertEqual(result.returncode, 1, self.out)
                self.assertIn("REFUSED: " + message.replace("ROOT/", f"{self.root}/"), self.out)
                self.assertIn("nothing was changed", self.out)
                self.assert_nothing_done()

    def test_the_options_are_parsed_strictly(self):
        for bad in ("pm_test=deep", "pm_test=", "pm_test=core pm_test=core", "pm_test=core pm_test=platform",
                    "trace trace", "trace trace=live", "trace=on", "pci=", "pci=add", "dry pm_test=platform",
                    "dry trace", "dry trace=live",
                    # Revision 11's pci=keep: one pci= at most, and no sleep in dry.
                    "pci=remove pci=keep", "pci=keep pci=keep", "dry pci=keep",
                    # Revision 9's debug switches: one list of known words, once each.
                    "gdsc=", "gdsc=pcie1", "gdsc=pcie0,", "gdsc=,usb", "gdsc=pcie0,,usb", "gdsc=pcie0,pcie0",
                    "gdsc=PCIE0", "gdsc=pcie0 gdsc=usb", "cxmx=", "cxmx=on", "cxmx=ret cxmx=ret",
                    # Revision 11's gdsc-ret=pcie0: that word alone, once, and
                    # not with gdsc=pcie0.
                    "gdsc-ret=", "gdsc-ret=usb", "gdsc-ret=pcie0phy", "gdsc-ret=pcie0 gdsc-ret=pcie0",
                    "gdsc=pcie0 gdsc-ret=pcie0", "gdsc=usb,pcie0 gdsc-ret=pcie0",
                    # Revision 11's rpmh: once, and no sleep in dry.
                    "rpmh rpmh", "dry rpmh", "rpmh=1",
                    # Revision 11's root-port-d3: once, with pci=keep, not with
                    # gdsc-ret=pcie0 or a pm_test= level short of the noirq
                    # phase, and no sleep in dry; sensors=stop: that word
                    # alone, once, and no sleep in dry.
                    "root-port-d3", "pci=remove root-port-d3", "pci=keep root-port-d3 root-port-d3",
                    "pci=keep root-port-d3 gdsc-ret=pcie0", "pci=keep root-port-d3 pm_test=devices",
                    "pci=keep root-port-d3 pm_test=freezer", "dry root-port-d3", "root-port-d3=1",
                    "sensors=", "sensors=start", "sensors=stop sensors=stop", "dry sensors=stop", "sensors",
                    "sensors=stop sensors=hook", "sensors=hook sensors=hook", "sensors=Hook",
                    # Revision 12's cxpc: once, and no sleep in dry.
                    "cxpc cxpc", "dry cxpc", "cxpc=1"):
            with self.subTest(bad):
                result = self.run_test(*bad.split(), "restart-ok")
                self.assertEqual(result.returncode, 2, self.out)
                self.assertIn("sleep-test.sh dry [release-floor] [hold=S] [pci=remove] [mode=s2idle]\n"
                              "       [gdsc=pcie0,pcie0phy,usb] [gdsc-ret=pcie0] [cxmx=ret] restart-ok", result.stderr)
                self.assertIn("[pci=remove|pci=keep] [pm_test=LEVEL] [trace|trace=live] [mode=s2idle]\n"
                              "       [gdsc=pcie0,pcie0phy,usb] [gdsc-ret=pcie0] [cxmx=ret] [root-port-d3] [rpmh]\n"
                              "       [cxpc] [sensors=stop|sensors=hook] restart-ok", result.stderr)
                self.assert_nothing_done()

    # Revision 9's debug switches (gdsc=, cxmx=ret): run-time module
    # parameters of the kit's kernel, off at every boot.

    def switch_files(self):
        """What each debug switch's file holds, by word; None for one that
        is not there."""
        found = {}
        for word, path in SWITCHES:
            f = self.root / "sys/module" / path
            found[word] = f.read_text() if f.exists() else None
        return found

    def test_the_debug_switches_are_set_right_before_the_freeze_and_named_everywhere(self):
        """gdsc= and cxmx=ret: Y to each switch asked for, after the PCI
        removal and before the first freeze, each read back; the header,
        pmsg right after the write and before each sleep, every cycle's
        verdict and the RESULT line say how they read.  Nothing puts them
        back: the restart does."""
        (self.root / "dev/pmsg0").write_text("")
        self.pci_tree()
        self.debug_switches()
        result = self.run_test("ro", "2", "pci=remove", "gdsc=usb,pcie0", "cxmx=ret", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        on = "switches: pcie0 Y, pcie0phy N, usb Y, cxmx Y"
        # Off at the PCI removal, on at the first freeze and at each sleep,
        # and still on at the restart.
        self.assertEqual(self.read("pcie0-at-pci-remove"), "N\n")
        self.assertEqual(self.read("switches-at-freeze"), "Y N Y Y \n")
        self.assertEqual(self.read("switches-at-sleep"), "Y N Y Y \n" * 2)
        self.assertEqual(self.switch_files(), {"pcie0": "Y", "pcie0phy": "N\n", "usb": "Y", "cxmx": "Y"})
        self.assertEqual(self.sysrq(), "b")
        for i in (1, 2):
            self.assertIn(f"SLEEP-TEST cycle {i}/2: PASS: deep (aosd +1 cxsd +1 ddr +1), resumed, the UFS read back "
                          f"as before; floor released; {on}\n", self.out)
        # The options name the switches in one order, whatever the order given.
        self.assertIn("SLEEP-TEST RESULT: PASS: 2 of 2 cycles resumed with the UFS readable, 2 of them deep; floor "
                      f"released; kernel 68d243e908c0; options: pci=remove gdsc=pcie0,usb cxmx=ret; {on}\n", self.out)
        log = (self.ramdir() / "log").read_text()
        order = ["debug switches pcie0 usb cxmx: right before it freezes the disks, after the PCI removal, the run "
                 "writes Y to each and reads it back",
                 "pci=remove: removed 0000:00:00.0",
                 f"debug switches set right before the freeze: pcie0 usb cxmx; {on}; they stay so until the restart "
                 "that ends the run",
                 "frozen: /media/stick", "cycle 1/2: deep sleep"]
        self.assertEqual([log.index(said) for said in order], sorted(log.index(said) for said in order))
        (logs,) = (self.kit / "logs").glob("*-sleep")
        before = (logs / "before").read_text()
        self.assertIn("debug switches: pcie0 N, pcie0phy N, usb N, cxmx N "
                      f"({self.root}/sys/module/gcc_sm8550/parameters/debug_pcie0_off, debug_pcie0_phy_off, "
                      f"debug_usb_off; {self.root}/sys/module/rpmhpd/parameters/debug_ret_floor; {self.root}/sys/module/"
                      "pcie_qcom/parameters/debug_root_port_d3); this run sets pcie0 usb cxmx to Y right before the "
                      "freeze\n", before)
        progress = self.progress_lines()
        self.assertEqual(progress[1:3], ["progress: pci=remove: removed 0000:00:00.0; PCI devices 2 -> 0",
                                         f"progress: debug switches set: pcie0 usb cxmx; {on}"])
        self.assertEqual([line.split("; up ")[0] for line in progress if "entering" in line], [
            f"progress: cycle {i}/2: entering the sleep, rtcwake -m mem -s 30; no pm_test, no trace; {on}"
            for i in (1, 2)])
        # The verdicts in pmsg carry them too.
        pmsg = self.pmsg_lines(evidence=False)
        self.assertEqual(len([line for line in pmsg if line.startswith("SLEEP-TEST cycle ") and line.endswith(on)]), 2)
        self.assertTrue([line for line in pmsg if line.startswith("SLEEP-TEST RESULT: PASS") and line.endswith(on)])

    def test_every_run_names_the_switches_as_they_are(self):
        """A run without gdsc= or cxmx= writes no switch, and still says how
        they read: off on the kit's kernel, not offered on one without them
        (revision 8's and before)."""
        for name, offered, said in (
                ("revision 9's kernel", True, "switches: pcie0 N, pcie0phy N, usb N, cxmx N"),
                ("an earlier kernel", False, "switches: not offered by this kernel")):
            with self.subTest(name):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                (self.root / "dev/pmsg0").write_text("")
                if offered:
                    self.debug_switches()
                result = self.run_test("ro", "1", "restart-ok", "hold=0")
                self.assertEqual(result.returncode, 1, self.out)
                self.assert_read_only_rules()
                self.assertIn("SLEEP-TEST cycle 1/1: PASS: deep (aosd +1 cxsd +1 ddr +1), resumed, the UFS read back "
                              f"as before; floor released; {said}\n", self.out)
                self.assertIn(f"floor released; kernel 68d243e908c0; options: none; {said}\n", self.out)
                (logs,) = (self.kit / "logs").glob("*-sleep")
                self.assertIn(f"\ndebug {said} (", (logs / "before").read_text())
                self.assertNotIn("to Y right before the freeze", (logs / "before").read_text())
                self.assertIn(f"; no pm_test, no trace; {said}; up ", "\n".join(self.progress_lines()))
                self.assertFalse([line for line in self.progress_lines() if "debug switches set" in line])
                self.assertEqual(self.switch_files(), {word: "N\n" if offered else None for word, _ in SWITCHES})
                self.assertEqual(self.read("switches-at-sleep"), ("N N N N \n" if offered else "- - - - \n"))

    def test_the_debug_switches_are_refused_with_nothing_changed_where_they_cannot_serve(self):
        """gdsc= without pci=remove (a collapsed PCIe GDSC under a live
        host), a switch the kernel does not offer, and a switch on already,
        asked for or not: refused before anything is done."""
        gcc = "ROOT/sys/module/gcc_sm8550/parameters"
        cases = (
            (("gdsc=pcie0",), {}, "gdsc=pcie0 needs pci=remove: with the PCI devices kept the PCIe host stays up "
             "through the sleep, and a PCIe GDSC that collapses (OFF_ON) takes the controller away from a live "
             "host; the GDSC switches are for runs with pci=remove"),
            (("gdsc=usb", "cxmx=ret"), {}, "gdsc=usb needs pci=remove"),
            (("pci=remove", "gdsc=pcie0"), {"pcie0": None, "pcie0phy": None, "usb": None, "cxmx": None},
             f"this kernel does not offer the debug switch pcie0 ({gcc}/debug_pcie0_off): the kernels of kit "
             "revisions 9 to 11 have it"),
            (("pci=remove", "gdsc=pcie0", "cxmx=ret"), {"cxmx": None},
             "this kernel does not offer the debug switch cxmx (ROOT/sys/module/rpmhpd/parameters/debug_ret_floor)"),
            (("pci=remove",), {"pcie0": "Y"},
             f"a debug switch is on already: pcie0 ({gcc}/debug_pcie0_off: Y); a run sets the ones it is given "
             "itself, right before the freeze, and records what it set: put it back (echo N | sudo tee FILE) or "
             "restart the tablet"),
            (("pci=remove", "gdsc=pcie0"), {"pcie0": "Y", "usb": "Y"},
             f"a debug switch is on already: pcie0 ({gcc}/debug_pcie0_off: Y) usb ({gcc}/debug_usb_off: Y);"),
            (("dry", "gdsc=pcie0phy"), {}, "gdsc=pcie0phy needs pci=remove"),
        )
        for args, values, message in cases:
            with self.subTest(message):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                self.pci_tree()
                self.debug_switches(**values)
                files = self.switch_files()
                result = self.run_test(*(args if args[0] == "dry" else ("ro", "1") + args), "restart-ok")
                self.assertEqual(result.returncode, 1, self.out)
                self.assertIn("REFUSED: " + message.replace("ROOT/", f"{self.root}/"), self.out)
                self.assertIn("nothing was changed", self.out)
                self.assert_nothing_done()
                self.assertEqual(self.read("pci-remove.calls"), "")
                self.assertEqual(self.switch_files(), files)
        # cxmx=ret alone needs no pci=remove: the run goes on.
        with self.subTest("cxmx=ret without pci=remove"):
            FakeTablet.setUp(self)
            SleepTestTest.setUp(self)
            self.debug_switches()
            result = self.run_test("ro", "1", "cxmx=ret", "restart-ok", "hold=0")
            self.assertEqual(result.returncode, 1, self.out)
            self.assert_read_only_rules()
            self.assertIn("options: cxmx=ret; switches: pcie0 N, pcie0phy N, usb N, cxmx Y\n", self.out)
            self.assertEqual(self.read("switches-at-sleep"), "N N N Y \n")

    def test_a_stop_before_any_sleep_puts_the_switches_back(self):
        """A switch that does not read Y after the write (here the fake
        kernel locks it between the checks and the run in RAM) stops the run
        before the freeze; so does a freeze that fails after the switches
        took.  Either way the tablet runs on, thawed, with every switch the
        run wrote back to N, and the record says so."""
        for name, setup, why in (
                ("a switch that does not take",
                 lambda: (self.tmp / "lock-switch-at-swapoff").write_text("gcc_sm8550/parameters/debug_pcie0_off\n"),
                 "a debug switch did not take: pcie0 (ROOT/sys/module/gcc_sm8550/parameters/debug_pcie0_off) reads "
                 "N after a write of Y; every filesystem"),
                ("a freeze that fails", lambda: (self.tmp / "freeze-fails").write_text("/media/stick\n"),
                 "STOPPED before any sleep: fsfreeze -f /media/stick (/dev/sdg2) failed")):
            with self.subTest(name):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                (self.root / "dev/pmsg0").write_text("")
                self.pci_tree()
                self.debug_switches()
                setup()
                result = self.run_test("ro", "1", "pci=remove", "gdsc=pcie0,usb", "restart-ok", "hold=0")
                self.assertEqual(result.returncode, 1, self.out)
                self.assertEqual(self.sysrq(), "")
                self.assertEqual(self.read("rtcwake.calls").count("-m mem"), 0)
                self.assertEqual(self.read("frozen"), "")
                self.assertEqual(self.errors_mode(), {"/": [], "/media/stick": ["errors=remount-ro"]})
                self.assertEqual({word: (value or "").strip() for word, value in self.switch_files().items()},
                                 {"pcie0": "N", "pcie0phy": "N", "usb": "N", "cxmx": "N"})
                self.assertIn("SLEEP-TEST RESULT: STOPPED before any sleep: ", self.out)
                self.assertIn(why.replace("ROOT/", f"{self.root}/"), self.out)
                self.assertIn("errors= is back as it was on: /media/stick /, and the tablet is not restarted; the debug "
                              "switches it set are back to N; floor released; kernel 68d243e908c0; options: pci=remove "
                              "gdsc=pcie0,usb; switches: pcie0 N, pcie0phy N, usb N, cxmx N\n", self.out)
                if name == "a switch that does not take":
                    self.assertIn("NEXT: the kernel did not take that write (its own line, if it wrote one: sudo dmesg "
                                  "| grep -e gcc-sm8550 -e rpmhpd -e debug_root_port_d3; a root-port-d3 that found no "
                                  "root port of a qcom PCIe host gets -ENODEV); run again once that is understood; what "
                                  "pci=remove removed stays removed until the restart, the Wi-Fi with it (sudo reboot "
                                  "brings it back)", self.out)
                    self.assertEqual(self.read("fsfreeze.calls"), "--version\n")
                    self.assertFalse([line for line in self.progress_lines() if "debug switches set" in line])
                else:
                    self.assertTrue([line for line in self.progress_lines() if "debug switches set" in line])

    def test_dry_with_the_debug_switches_sets_and_records_them_without_a_sleep(self):
        self.pci_tree()
        self.debug_switches()
        result = self.run_test("dry", "pci=remove", "gdsc=pcie0", "cxmx=ret", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        self.assertEqual(self.read("rtcwake.calls"), "--version\n")
        self.assertEqual(self.read("switches-at-freeze"), "Y N N Y \n")
        self.assertIn("SLEEP-TEST RESULT: DRY: no sleep tried;", self.out)
        self.assertIn("floor released; kernel 68d243e908c0; options: pci=remove gdsc=pcie0 cxmx=ret; switches: pcie0 Y, "
                      "pcie0phy N, usb N, cxmx Y\n", self.out)
        self.assertEqual(self.sysrq(), "b")

    def test_gdsc_usb_with_the_tablet_as_usb_host_says_to_run_undocked(self):
        self.pci_tree()
        self.debug_switches()
        result = self.run_test("ro", "1", "pci=remove", "gdsc=usb", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("gdsc=usb with the tablet as USB host: with its GDSCs collapsed in the sleep the xHCI is not "
                      "expected to come back (the reason for the retention patch); run undocked", self.out)
        FakeTablet.setUp(self)
        SleepTestTest.setUp(self)
        self.pci_tree()
        self.debug_switches()
        (self.port / "data_role").write_text("host [device]\n")
        result = self.run_test("ro", "1", "pci=remove", "gdsc=usb", "restart-ok", "hold=0")
        self.assertNotIn("run undocked", self.out)

    # mode=s2idle (2026-11-22 evening): each sleep is suspend-to-idle,
    # rtcwake -m freeze, instead of PSCI SYSTEM_SUSPEND.

    def s2idle_files(self):
        """What an s2idle run's evidence reads beyond the default fake root:
        the cluster domain's idle states (debugfs) and /proc/interrupts."""
        d = self.root / "sys/kernel/debug/pm_genpd/power-domain-cluster"
        d.mkdir(parents=True, exist_ok=True)
        (d / "idle_states").write_text(CLUSTER_STATES)
        (self.root / "proc/interrupts").write_text(INTERRUPTS)

    @staticmethod
    def s2idle_log(asleep="30.002", irq=21):
        """A suspend-to-idle's kernel lines with pm_debug_messages, worded as
        7.2 prints them: "PM: suspend entry (s2idle)" (pm_suspend()), "PM:
        suspend-to-idle" and "PM: resume from suspend-to-idle" (s2idle_loop(),
        kernel/power/suspend.c), "Timekeeping suspended for N seconds"
        (tk_debug_account_sleep_time(), kernel/time/timekeeping_debug.c: no
        prefix) and "PM: Triggering wakeup from IRQ N"
        (pm_system_irq_wakeup(), drivers/base/power/wakeup.c).  ASLEEP None:
        no Timekeeping line; IRQ None: no wakeup line."""
        lines = ["PM: suspend entry (s2idle)", "Filesystems sync: 0.000 seconds",
                 "Freezing user space processes",
                 "Freezing user space processes completed (elapsed 0.002 seconds)",
                 "PM: suspend of devices complete after 84.636 msecs",
                 "PM: late suspend of devices complete after 4.974 msecs",
                 "PM: noirq suspend of devices complete after 21.772 msecs", "PM: suspend-to-idle"]
        if asleep:
            lines.append(f"Timekeeping suspended for {asleep} seconds")
        if irq:
            lines.append(f"PM: Triggering wakeup from IRQ {irq}")
        lines += ["PM: resume from suspend-to-idle", "PM: noirq resume of devices complete after 283.843 msecs",
                  "PM: resume of devices complete after 84.396 msecs", "Restarting tasks: Done", "PM: suspend exit"]
        return "".join(f"[  7{n:02d}.000000] {line}\n" for n, line in enumerate(lines))

    S2_PASS = ("asleep 30.0s (1 Timekeeping lines), s2idle entries +8 on 8 of 8 CPUs in state1 (cpu-sleep-0-0 "
               "cpu-sleep-1-0 cpu-sleep-2-0), cluster S0 +0 (s2idle +0, rejected +0), S1 +1 (s2idle +1, rejected +0)")

    def test_mode_s2idle_sleeps_with_rtcwake_freeze_and_names_its_mode_everywhere(self):
        """mode=s2idle: rtcwake -m freeze for each sleep, /sys/power/mem_sleep
        neither needed nor touched (here it offers no deep at all), the mode
        in the header, the progress lines, every verdict and the RESULT, and a
        third evidence line per cycle in pmsg: the CPUs' s2idle entries and
        the cluster domain's states.  The suspend-to-idle's own kernel lines
        are among the cycle's kernel lines."""
        (self.root / "dev/pmsg0").write_text("")
        (self.root / "sys/power/mem_sleep").write_text("[s2idle]\n")
        self.s2idle_files()
        for n in (1, 2):
            self.cycle(n, log=self.s2idle_log())
        result = self.run_test("ro", "2", "mode=s2idle", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        self.assertEqual(self.read("rtcwake.calls"), "--version\n" + "-u -d rtc0 -m freeze -s 30\n" * 2)
        self.assertEqual((self.root / "sys/power/mem_sleep").read_text(), "[s2idle]\n")
        self.assertEqual(self.read("mem-sleep-at-freeze"), "[s2idle]\n" * 2)
        for i in (1, 2):
            self.assertIn(f"SLEEP-TEST cycle {i}/2: PASS (s2idle): deep (aosd +1 cxsd +1 ddr +1), resumed, the UFS "
                          f"read back as before; {self.S2_PASS}; floor released", self.out)
        self.assertIn("SLEEP-TEST RESULT: PASS (s2idle): 2 of 2 cycles resumed with the UFS readable, 2 of them "
                      "deep; floor released; kernel 68d243e908c0; options: mode=s2idle", self.out)
        # Every verdict of the run says so; the restart's line is the run's.
        pmsg = self.pmsg_lines(progress=True)
        verdicts = [line for line in pmsg if line.startswith("SLEEP-TEST ") and "RESTART" not in line]
        self.assertEqual(len(verdicts), 3, verdicts)
        self.assertTrue(all("(s2idle)" in line for line in verdicts), verdicts)
        self.assertEqual([line.split("; up ")[0] for line in pmsg if line.startswith("progress: ")], [
            "progress: start: ro, 2 cycles of 30s, aborts=5, hold=0s; options: mode=s2idle; floor released; kernel "
            "68d243e908c0",
            "progress: protected: frozen: /media/stick /; /dev/sda read-only; from here the run ends with a restart",
            f"progress: cycle 1/2: entering the sleep, rtcwake -m freeze -s 30; no pm_test, no trace; {NO_SWITCHES}",
            f"progress: cycle 2/2: entering the sleep, rtcwake -m freeze -s 30; no pm_test, no trace; {NO_SWITCHES}"])
        # Six evidence lines per cycle in pmsg, the sixth the s2idle one.
        evidence = [line for line in pmsg if ": evidence: " in line]
        self.assertEqual(len(evidence), 12, evidence)
        self.assertEqual(evidence[5], "cycle 1/2: evidence: s2idle: s2idle entries +8 on 8 of 8 CPUs in state1 "
                                      "(cpu-sleep-0-0 cpu-sleep-1-0 cpu-sleep-2-0); cluster S0 +0 (s2idle +0, rejected "
                                      "+0), S1 +1 (s2idle +1, rejected +0); asleep 30.0s (1 Timekeeping lines); woken "
                                      "by IRQ 21 (pm8xxx_rtc_alarm)")
        log = (self.ramdir() / "log").read_text()
        for said in ("cycle 1/2: suspend-to-idle for 30s (rtcwake -m freeze); data=host power=sink partner=present",
                     "cycle 1/2: asleep 30.0s, woken by IRQ 21, rtcwake 0, suspend_stats success +1 fail +0, aosd +1 "
                     "cxsd +1 ddr +1, data=host",
                     "cycle 1/2: evidence: s2idle: s2idle entries +8 on 8 of 8 CPUs in state1 (cpu-sleep-0-0 "
                     "cpu-sleep-1-0 cpu-sleep-2-0), 0.012s counted in them (not the time every CPU was frozen); "
                     "cluster domain: S0 +0 (s2idle +0, rejected +0), S1 +1 (s2idle +1, rejected +0); asleep 30.0s "
                     "(1 Timekeeping lines); woken by IRQ 21 (pm8xxx_rtc_alarm); rtcwake took 0.0s; suspend_stats "
                     "last_hw_sleep not offered by this kernel",
                     "mode=s2idle: each sleep is rtcwake -m freeze, suspend-to-idle"):
            self.assertIn(said, log)
        self.assertNotIn("deep sleep for", log)
        # This kernel has no suspend_stats/last_hw_sleep, and reading it
        # leaks no error into the log (dash reports a redirection that fails
        # before a 2>/dev/null after it takes effect).
        self.assertFalse((self.root / "sys/power/suspend_stats/last_hw_sleep").exists())
        for word in ("cannot open", "No such file"):
            self.assertNotIn(word, log)
        # The suspend-to-idle's own lines reach the cycle's kernel lines.
        kernel = [line.split("] ", 2)[-1] for line in log.splitlines() if "cycle 1/2: evidence: kernel: " in line]
        for line in ("PM: suspend entry (s2idle)", "PM: suspend-to-idle", "Timekeeping suspended for 30.002 seconds",
                     "PM: Triggering wakeup from IRQ 21", "PM: resume from suspend-to-idle", "PM: suspend exit"):
            self.assertIn(line, kernel)
        (logs,) = (self.kit / "logs").glob("*-sleep")
        before = (logs / "before").read_text()
        self.assertEqual(before.splitlines()[0].split(": ", 1)[1],
                         "2 suspend-to-idle cycles of 30s (rtcwake -m freeze) with every disk filesystem frozen "
                         "(aborted attempts and early wakes tried again: up to 5), then a 0s hold and a restart "
                         "(sysrq b); options: mode=s2idle")
        self.assertIn("\nmem_sleep [s2idle], suspend_stats success 4 fail 0;", before)
        self.assertIn(f"\nmode=s2idle: rtcwake -m freeze ({self.root}/sys/power/state: freeze mem), mem_sleep not used; the "
                      "cpuidle states with an s2idle entry: state1(cpu-sleep-0-0) state1(cpu-sleep-1-0) "
                      "state1(cpu-sleep-2-0) on 8 CPUs; power-domain-cluster: S0 usage 10 s2idle 0 rejected 0, S1 "
                      "usage 5 s2idle 0 rejected 0\n", before)
        self.assertEqual(self.sysrq(), "b")

    def test_mode_deep_is_the_default_and_its_lines_are_as_before(self):
        """mode=deep is accepted and is what a run without mode= does: the same
        result, pmsg and log, rtcwake -m mem with deep selected, and none of
        mode=s2idle's words or evidence."""
        seen = {}
        for args in (("ro", "1"), ("ro", "1", "mode=deep")):
            with self.subTest(args):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                (self.root / "dev/pmsg0").write_text("")
                self.s2idle_files()
                result = self.run_test(*args, "restart-ok", "hold=0")
                self.assertEqual(result.returncode, 1, self.out)
                self.assert_read_only_rules()
                self.assertEqual(self.read("rtcwake.calls"), "--version\n-u -d rtc0 -m mem -s 30\n")
                self.assertEqual((self.root / "sys/power/mem_sleep").read_text(), "deep")
                stamp = self.ramdir().name
                log = (self.ramdir() / "log").read_text()
                for said in ("cycle 1/1: deep sleep for 30s (rtcwake -m mem); data=host",
                             "cycle 1/1: asleep 30.0s, woken by IRQ 21, rtcwake 0, suspend_stats success +1 fail +0, "
                             "aosd +1 cxsd +1 ddr +1, data=host power=sink partner=present, USB devices 0",
                             "SLEEP-TEST cycle 1/1: PASS: deep (aosd +1 cxsd +1 ddr +1), resumed, the UFS read back "
                             "as before; floor released",
                             "SLEEP-TEST RESULT: PASS: 1 of 1 cycles resumed with the UFS readable, 1 of them deep; "
                             "floor released; kernel 68d243e908c0; options: none"):
                    self.assertIn(said, log)
                pmsg = (self.root / "dev/pmsg0").read_text().replace(stamp, "STAMP")
                self.assertIn("progress: cycle 1/1: entering the sleep, rtcwake -m mem -s 30; no pm_test, no trace;",
                              pmsg)
                self.assertNotIn("s2idle", pmsg)
                self.assertNotIn("s2idle", (self.ramdir() / "result").read_text())
                self.assertEqual(pmsg.count(": evidence: "), 5)
                self.assertNotIn("evidence: s2idle", log)
                (logs,) = (self.kit / "logs").glob("*-sleep")
                self.assertEqual((logs / "before").read_text().splitlines()[0].split(": ", 1)[1],
                                 "1 deep cycles of 30s with every disk filesystem frozen (aborted attempts tried "
                                 "again: up to 5), then a 0s hold and a restart (sysrq b); options: none")
                seen[args] = ((self.ramdir() / "result").read_text().replace(str(self.root), "ROOT"),
                              re.sub(r"up [0-9.]+s", "up Ns", pmsg).replace(str(self.root), "ROOT"))
        self.assertEqual(seen[("ro", "1")], seen[("ro", "1", "mode=deep")])

    def test_mode_s2idle_is_refused_with_nothing_changed_where_it_cannot_run(self):
        """Refused before anything is done: no freeze in /sys/power/state, or no
        cpuidle state with an s2idle entry; dry checks the same.  And parsed
        strictly: one mode=, deep or s2idle, and no pm_test level that
        suspend-to-idle never reaches (the kernel refuses processors and core
        with it)."""
        cases = (
            ("mode=s2idle: ROOT/sys/power/state offers no freeze (mem)",
             lambda: (self.root / "sys/power/state").write_text("mem\n")),
            ("mode=s2idle: no cpuidle state of cpu0 has an s2idle entry (ROOT/sys/devices/system/cpu/cpu0/cpuidle/"
             "state*/s2idle): the CPUs would not enter suspend-to-idle's own state",
             lambda: shutil.rmtree(self.root / "sys/devices/system/cpu/cpu0/cpuidle/state1/s2idle")),
        )
        for message, damage in cases:
            for verb in ("ro", "dry"):
                with self.subTest(message=message, verb=verb):
                    FakeTablet.setUp(self)
                    SleepTestTest.setUp(self)
                    damage()
                    result = self.run_test(verb, "mode=s2idle", "restart-ok")
                    self.assertEqual(result.returncode, 1, self.out)
                    self.assertIn("REFUSED: " + message.replace("ROOT/", f"{self.root}/"), self.out)
                    self.assertIn("nothing was changed", self.out)
                    self.assert_nothing_done()
        for bad in ("mode=s3", "mode=", "mode=deep mode=s2idle", "mode=s2idle mode=s2idle", "mode=s2idle pm_test=core",
                    "pm_test=processors mode=s2idle"):
            with self.subTest(bad):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                result = self.run_test("ro", *bad.split(), "restart-ok")
                self.assertEqual(result.returncode, 2, self.out)
                self.assertIn("[trace|trace=live] [mode=s2idle]\n       [gdsc=pcie0,pcie0phy,usb] [gdsc-ret=pcie0] "
                              "[cxmx=ret] [root-port-d3] [rpmh]\n       [cxpc] [sensors=stop|sensors=hook] restart-ok", result.stderr)
                self.assert_nothing_done()
                if "pm_test=" in bad:
                    level = bad.split("pm_test=")[1].split()[0]
                    self.assertIn(f"pm_test={level} with mode=s2idle: suspend-to-idle never gets there", result.stderr)

    def test_mode_s2idle_combines_with_the_other_options(self):
        """mode=s2idle with pci=remove, pm_test=, trace=live, release-floor,
        aborts= and hold=: each does what it does in a deep run, and the
        options are named in one order."""
        (self.root / "dev/pmsg0").write_text("")
        self.pci_tree()
        self.s2idle_files()
        self.floor_held()
        result = self.run_test("ro", "1", "trace=live", "pm_test=devices", "mode=s2idle", "pci=remove", "aborts=2",
                               "release-floor", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        options = "options: mode=s2idle pci=remove pm_test=devices trace=live"
        self.assertEqual(self.read("rtcwake.calls"), "--version\n-u -d rtc0 -m freeze -s 30\n")
        self.assertEqual(self.read("pci-remove.calls"), "0000:00:00.0 before\n")
        self.assertEqual(self.read("knobs-at-sleep"), "pm_test=devices printk=8 4 1 7 console_suspend=N\n")
        self.assertIn("SLEEP-TEST RESULT: PM-TEST devices (s2idle): 1 of 1 cycles went as far as pm_test=devices and "
                      "came back with the UFS readable; no suspend-to-idle, so no depth was tested; floor released; "
                      f"kernel 68d243e908c0; {options}", self.out)
        progress = self.progress_lines()
        self.assertTrue(progress[0].startswith("progress: start: ro, 1 cycles of 30s, aborts=2, hold=0s; "
                                               f"{options}; floor released;"), progress)
        self.assertTrue(progress[-1].startswith("progress: cycle 1/1: entering the sleep, rtcwake -m freeze -s 30; "
                                                f"pm_test devices, console loglevel 8, console_suspend N; {NO_SWITCHES}; "
                                                "up "), progress)
        self.assertEqual((self.root / "sys/power/pm_test").read_text(), "none")
        self.assertEqual(self.sysrq(), "b")

    def test_dry_with_mode_s2idle_leaves_mem_sleep_and_pm_debug_messages_alone(self):
        result = self.run_test("dry", "mode=s2idle", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        self.assertEqual(self.read("rtcwake.calls"), "--version\n")
        self.assertIn("SLEEP-TEST RESULT: DRY: no sleep tried;", self.out)
        self.assertIn("floor released; kernel 68d243e908c0; options: mode=s2idle", self.out)
        self.assertEqual((self.root / "sys/power/mem_sleep").read_text(), "s2idle [deep]\n")
        self.assertEqual((self.root / "sys/power/pm_debug_messages").read_text(), "0\n")
        self.assertEqual(self.sysrq(), "b")

    def test_an_s2idle_sleep_woken_early_is_tried_again_within_aborts(self):
        """A sleep the kernel counted (rtcwake 0, success +1) but that the
        touchscreen ended after 3 s, or before any CPU entered its s2idle
        state: WOKEN EARLY, not a pass and not a FAIL; the cycle again, counted
        against aborts=N; past it NO-SLEEP (s2idle).  Woken early by the RTC's
        alarm is no early wake, and a cycle whose deep states counted is a PASS
        however short."""
        touch = "a wakeup from IRQ 199 (fts1ba90a, the touchscreen: do not touch the screen during the run)"
        cl0 = "cluster S0 +0 (s2idle +0, rejected +0), S1 +0 (s2idle +0, rejected +0)"
        cl1 = "cluster S0 +0 (s2idle +0, rejected +0), S1 +1 (s2idle +1, rejected +0)"
        states = "s2idle entries +8 on 8 of 8 CPUs in state1 (cpu-sleep-0-0 cpu-sleep-1-0 cpu-sleep-2-0)"
        shallow = ("no-aosd", "no-cxsd", "no-ddr")
        cases = {
            "after 3 s": (lambda: self.cycle(1, *shallow, log=self.s2idle_log("3.104", 199), irq=199),
                          f"asleep 3.1s (1 Timekeeping lines), {states}, {cl1}"),
            "before any CPU entered its state": (
                lambda: self.cycle(1, *shallow, "no-s2idle", "no-cluster", log=self.s2idle_log(None, 199), irq=199),
                f"never asleep (no Timekeeping line), s2idle entries +0 on 0 of 8 CPUs, {cl0}"),
        }
        for name, (setup, how) in cases.items():
            with self.subTest(name):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                (self.root / "dev/pmsg0").write_text("")
                self.s2idle_files()
                setup()
                result = self.run_test("ro", "2", "mode=s2idle", "restart-ok", "hold=0")
                self.assertEqual(result.returncode, 1, self.out)
                self.assert_read_only_rules()
                early = (f"SLEEP-TEST cycle 1/2: WOKEN EARLY (s2idle): the suspend-to-idle was cut short ({how}; 30s "
                         f"asked for) by {touch}; the UFS read back as before; counted as aborted attempt 1 "
                         f"(aborts=5): cycle 1/2 again in 5 s; floor released; {NO_SWITCHES}")
                self.assertIn(early, self.out)
                self.assertIn(early, self.pmsg_lines())
                self.assertEqual(self.out.count("SLEEP-TEST cycle 1/2: PASS (s2idle): deep"), 1)
                self.assertIn("SLEEP-TEST cycle 2/2: PASS (s2idle): deep", self.out)
                self.assertIn("SLEEP-TEST RESULT: PASS (s2idle): 2 of 2 cycles resumed with the UFS readable, 2 of "
                              "them deep; 1 attempt aborted before the sleep or woken early (woken early by IRQ 199 "
                              "(fts1ba90a, the touchscreen) once) and tried again; floor released; kernel "
                              "68d243e908c0; options: mode=s2idle", self.out)
                self.assertNotIn("FAIL", self.out)
                self.assertEqual(self.read("rtcwake.calls").count("-m freeze"), 3)
                self.assertIn("progress: cycle 1/2: entering the sleep, rtcwake -m freeze -s 30, attempt 2;",
                              "\n".join(self.progress_lines()))
        with self.subTest("aborts=0"):
            FakeTablet.setUp(self)
            SleepTestTest.setUp(self)
            self.s2idle_files()
            self.cycle(1, *shallow, log=self.s2idle_log("3.104", 199), irq=199)
            result = self.run_test("ro", "2", "mode=s2idle", "aborts=0", "restart-ok", "hold=0")
            self.assertEqual(result.returncode, 1, self.out)
            self.assertIn(f"by {touch}; the UFS read back as before; counted as aborted attempt 1, more than aborts=0: "
                          "the run ends; floor released", self.out)
            self.assertIn("SLEEP-TEST RESULT: NO-SLEEP (s2idle) at cycle 1/2: 1 attempt aborted before the sleep or "
                          "woken early (woken early by IRQ 199 (fts1ba90a, the touchscreen) once), more than aborts=0; "
                          "0 of 2 cycles passed before it; this run does not answer whether the storage comes back "
                          "from a deep sleep; floor released; kernel 68d243e908c0; options: mode=s2idle", self.out)
            self.assertIn("NEXT: no storage verdict: after the restart run the test again, without touching the "
                          "screen; the ABORTED and WOKEN EARLY lines name what ended each attempt", self.out)
            self.assertEqual(self.read("rtcwake.calls").count("-m freeze"), 1)
            self.assertEqual(self.sysrq(), "b")
        with self.subTest("the RTC's alarm after 2 s"):
            FakeTablet.setUp(self)
            SleepTestTest.setUp(self)
            self.s2idle_files()
            self.cycle(1, *shallow, log=self.s2idle_log("2.004", 21))
            result = self.run_test("ro", "1", "mode=s2idle", "restart-ok", "hold=0")
            self.assertEqual(result.returncode, 1, self.out)
            self.assertIn("SLEEP-TEST cycle 1/1: SHALLOW (s2idle): resumed and the UFS read back as before, but not "
                          "every sleep state was reached (aosd +0 cxsd +0 ddr +0; asleep 2.0s (1 Timekeeping lines), "
                          f"{states}, {cl1}); floor released", self.out)
            self.assertNotIn("WOKEN EARLY (s2idle):", self.out)
        with self.subTest("deep states counted"):
            FakeTablet.setUp(self)
            SleepTestTest.setUp(self)
            self.s2idle_files()
            self.cycle(1, log=self.s2idle_log("3.104", 199), irq=199)
            result = self.run_test("ro", "1", "mode=s2idle", "restart-ok", "hold=0")
            self.assertEqual(result.returncode, 1, self.out)
            self.assertIn("SLEEP-TEST cycle 1/1: PASS (s2idle): deep (aosd +1 cxsd +1 ddr +1), resumed, the UFS read "
                          f"back as before; asleep 3.1s (1 Timekeeping lines), {states}, {cl1}; floor released",
                          self.out)
            self.assertNotIn("WOKEN EARLY (s2idle):", self.out)

    def test_an_s2idle_sleep_in_which_no_cpu_entered_its_state_fails(self):
        """The kernel counted a suspend, but no CPU entered its s2idle state and
        no wakeup on record explains it: a FAIL, with the storage as it was."""
        self.s2idle_files()
        self.cycle(1, "no-aosd", "no-cxsd", "no-ddr", "no-s2idle", "no-cluster", log=self.s2idle_log(None, None),
                   irq="")
        result = self.run_test("ro", "2", "mode=s2idle", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        self.assertIn("cycle 1/2: no \"Timekeeping suspended for\" line (timekeeping was not suspended), no wakeup IRQ "
                      "recorded (pm_wakeup_irq has no data), rtcwake 0, suspend_stats success +1 fail +0", self.out)
        self.assertIn("SLEEP-TEST cycle 1/2: FAIL (s2idle): no CPU entered its s2idle state (s2idle entries +0 on "
                      f"every CPU) (aosd +0 cxsd +0 ddr +0; floor released; {NO_SWITCHES})", self.out)
        self.assertIn("SLEEP-TEST RESULT: FAIL (s2idle) at cycle 1/2: no CPU entered its s2idle state (s2idle "
                      "entries +0 on every CPU); 0 passed before it; floor released; kernel 68d243e908c0; options: "
                      "mode=s2idle", self.out)
        self.assertIn("restart: sysrq b; every disk filesystem frozen; the storage read back as before", self.out)
        self.assertEqual(self.read("rtcwake.calls").count("-m freeze"), 1)
        self.assertNotIn("WOKEN EARLY (s2idle):", self.out)

    def test_s2idle_shallow_with_the_cluster_refused_and_its_pm_test(self):
        """A full-length suspend-to-idle whose cluster domain the RSC refused
        (S1 rejected): SHALLOW (s2idle), with that in its line.  pm_test=platform
        with mode=s2idle: rtcwake -m freeze, the suspend stops before the
        idle loop, PM-TEST (s2idle), and the cycle line says truthfully that
        nothing was suspended or woke it."""
        self.s2idle_files()
        self.cycle(1, "no-aosd", "no-cxsd", "no-ddr", "cluster-rejected", log=self.s2idle_log())
        result = self.run_test("ro", "1", "mode=s2idle", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("SLEEP-TEST cycle 1/1: SHALLOW (s2idle): resumed and the UFS read back as before, but not every "
                      "sleep state was reached (aosd +0 cxsd +0 ddr +0; asleep 30.0s (1 Timekeeping lines), s2idle "
                      "entries +8 on 8 of 8 CPUs in state1 (cpu-sleep-0-0 cpu-sleep-1-0 cpu-sleep-2-0), cluster S0 +0 "
                      "(s2idle +0, rejected +0), S1 +0 (s2idle +0, rejected +1)); floor released", self.out)
        self.assertIn("SLEEP-TEST RESULT: SHALLOW (s2idle): 1 of 1 cycles resumed with the UFS readable, 0 of them "
                      "deep; floor released; kernel 68d243e908c0; options: mode=s2idle", self.out)
        FakeTablet.setUp(self)
        SleepTestTest.setUp(self)
        self.s2idle_files()
        result = self.run_test("ro", "1", "mode=s2idle", "pm_test=platform", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        self.assertEqual(self.read("rtcwake.calls"), "--version\n-u -d rtc0 -m freeze -s 30\n")
        self.assertEqual(self.read("knobs-at-sleep"), "pm_test=platform printk=5 4 1 7 console_suspend=Y\n")
        self.assertEqual((self.root / "sys/power/pm_test").read_text(), "none")
        self.assertIn("SLEEP-TEST cycle 1/1: PM-TEST platform (s2idle): the suspend went as far as pm_test=platform "
                      "(the devices suspended through their noirq phase), waited there and came back with no "
                      "suspend-to-idle, and the UFS read back as before; qcom_stats aosd +0 cxsd +0 ddr +0 (CPU idle "
                      "during the wait, not a depth test); floor released", self.out)
        self.assertIn("SLEEP-TEST RESULT: PM-TEST platform (s2idle): 1 of 1 cycles went as far as pm_test=platform "
                      "and came back with the UFS readable; no suspend-to-idle, so no depth was tested; floor "
                      "released; kernel 68d243e908c0; options: mode=s2idle pm_test=platform", self.out)
        self.assertIn("cycle 1/1: no \"Timekeeping suspended for\" line (timekeeping was not suspended), no wakeup IRQ "
                      "recorded (pm_wakeup_irq has no data), rtcwake 0", self.out)
        log = (self.ramdir() / "log").read_text()
        self.assertIn("cycle 1/1: pm_test=platform cycle: rtcwake -m freeze -s 30, and the suspend stops at platform "
                      "(the devices suspended through their noirq phase), waits 5s there and comes back, no "
                      "suspend-to-idle; data=host", log)
        self.assertIn("pm_test=platform: each cycle's suspend-to-idle stops at platform", log)
        (logs,) = (self.kit / "logs").glob("*-sleep")
        self.assertIn("1 pm_test=platform cycles (rtcwake -m freeze -s 30; the suspend stops at platform, waits 5s and "
                      "comes back: no suspend-to-idle) with every disk filesystem frozen", (logs / "before").read_text())
        verdicts = [line for line in self.out.splitlines() if "SLEEP-TEST " in line]
        for word in ("PASS", "SHALLOW", "FAIL", "WOKEN EARLY"):
            self.assertFalse([line for line in verdicts if word in line], word)

    def test_an_aborted_s2idle_attempt_is_tried_again(self):
        """A suspend-to-idle the kernel gave up in its noirq phase (the
        touchscreen's wakeup, rtcwake 1, a failure counted): ABORTED (s2idle),
        tried again, as in a deep run.  An attempt whose kernel lines are a
        deep suspend's ("PM: suspend entry (deep)") is no aborted
        suspend-to-idle: a FAIL."""
        log = ("[13851.674165] PM: suspend entry (s2idle)\n"
               "[13858.770514] PM: suspend of devices complete after 186.566 msecs\n"
               "[13858.980061] PM: late suspend of devices complete after 209.497 msecs\n"
               "[13858.996988] PM: Triggering wakeup from IRQ 199\n"
               "[13859.007100] PM: noirq suspend of devices aborted after 10.100 msecs\n"
               "[13859.007192] PM: noirq suspend of devices failed\n"
               "[13859.031732] PM: late resume of devices complete after 24.259 msecs\n"
               "[13861.416262] PM: suspend exit\n")
        out = 'rtcwake: wakeup from "freeze" using rtc0 at Thu Mar  5 05:27:35 1970\nrtcwake: write error\n'
        for name, attempt, aborted in (("s2idle", log, True),
                                       ("deep lines", log.replace("(s2idle)", "(deep)"), False)):
            with self.subTest(name):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                self.s2idle_files()
                self.cycle(1, "suspend-failed", "no-aosd", "no-cxsd", "no-ddr", "no-s2idle", "no-cluster", rc=1,
                           log=attempt, out=out, irq=199)
                result = self.run_test("ro", "1", "mode=s2idle", "restart-ok", "hold=0")
                self.assertEqual(result.returncode, 1, self.out)
                self.assert_read_only_rules()
                if aborted:
                    self.assertIn("SLEEP-TEST cycle 1/1: ABORTED (s2idle): the kernel gave up the suspend before the "
                                  "tablet slept (rtcwake 1, suspend_stats success +0 fail +1): a wakeup from IRQ 199 "
                                  "(fts1ba90a, the touchscreen: do not touch the screen during the run); the UFS read "
                                  "back as before; aborted attempt 1 (aborts=5): cycle 1/1 again in 5 s; floor "
                                  "released", self.out)
                    self.assertIn("SLEEP-TEST RESULT: PASS (s2idle): 1 of 1 cycles resumed with the UFS readable, 1 "
                                  "of them deep; 1 attempt aborted before the sleep (IRQ 199 (fts1ba90a, the "
                                  "touchscreen) once) and tried again;", self.out)
                    self.assertNotIn("FAIL", self.out)
                else:
                    self.assertIn("SLEEP-TEST cycle 1/1: FAIL (s2idle): rtcwake returned 1", self.out)
                    self.assertIn("no 'PM: suspend entry (s2idle)'", self.out)
                    self.assertNotIn("ABORTED", self.out)

    # Three small defects the 2026-11-22 13:42 and 14:00 logs showed.

    def test_the_int_max_count_is_one_line(self):
        """grep -c prints 0 and fails when nothing matches: the count, then
        "n/a" on a line of its own, came out under "INT_MAX nodes now: 0"."""
        self.floor_held()
        result = self.run_test("ro", "1", "release-floor", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        log = (self.ramdir() / "log").read_text()
        self.assertRegex(log, r"INT_MAX nodes now: 0\n\[[0-9.]+\] ")
        self.assertNotIn("\nn/a\n", log)
        (logs,) = (self.kit / "logs").glob("*-sleep")
        self.assertIn("\nINT_MAX nodes: 0\nType-C: ", (logs / "before").read_text())
        FakeTablet.setUp(self)
        SleepTestTest.setUp(self)
        (self.root / "sys/kernel/debug/interconnect/interconnect_summary").write_text(
            "node tag avg peak\nebi 2147483647 2147483647\nqhm 2147483647 0\n")
        self.run_test("ro", "1", "restart-ok", "hold=0")
        (logs,) = (self.kit / "logs").glob("*-sleep")
        self.assertIn("\nINT_MAX nodes: 2\nType-C: ", (logs / "before").read_text())
        FakeTablet.setUp(self)
        SleepTestTest.setUp(self)
        (self.root / "sys/kernel/debug/interconnect/interconnect_summary").unlink()
        self.run_test("ro", "1", "restart-ok", "hold=0")
        (logs,) = (self.kit / "logs").glob("*-sleep")
        self.assertIn("\nINT_MAX nodes: n/a\nType-C: ", (logs / "before").read_text())

    def test_mem_sleep_is_shown_as_the_kernel_shows_it(self):
        """The file's content once ("s2idle [deep]"), not inside brackets of
        its own ("[s2idle [deep]]")."""
        self.run_test("ro", "1", "restart-ok", "hold=0")
        (logs,) = (self.kit / "logs").glob("*-sleep")
        before = (logs / "before").read_text()
        self.assertIn("\nmem_sleep s2idle [deep], suspend_stats success 4 fail 0; freeze_filesystems n/a", before)
        self.assertNotIn("[s2idle [deep]]", before)

    def test_pm_debug_messages_is_set_for_sleeps_alone_and_the_restart_undoes_it(self):
        """A run with sleeps sets pm_debug_messages to 1 and says that the
        restart at its end puts it back, and a stop before any sleep says it
        stays 1 until the next boot; dry does not sleep and leaves it."""
        result = self.run_test("ro", "1", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertEqual((self.root / "sys/power/pm_debug_messages").read_text(), "1")
        (logs,) = (self.kit / "logs").glob("*-sleep")
        self.assertIn("; pm_debug_messages 0 (this run sets 1; the restart at its end puts it back), pm_print_times 0;",
                      (logs / "before").read_text())
        self.assertIn("pm_debug_messages 1 for this run (it was 0), so that each cycle's kernel lines have the PM "
                      "core's: the restart that ends the run puts it back; a stop before any sleep leaves it at 1 "
                      "until the next boot", self.out)
        self.assertEqual(self.sysrq(), "b")
        with self.subTest("dry"):
            FakeTablet.setUp(self)
            SleepTestTest.setUp(self)
            result = self.run_test("dry", "restart-ok", "hold=0")
            self.assertEqual(result.returncode, 1, self.out)
            self.assertEqual((self.root / "sys/power/pm_debug_messages").read_text(), "0\n")
            (logs,) = (self.kit / "logs").glob("*-sleep")
            self.assertIn("; pm_debug_messages 0 (dry leaves it as it is), pm_print_times 0;",
                          (logs / "before").read_text())
            self.assertNotIn("pm_debug_messages 1 for this run", self.out)
        with self.subTest("a stop in RAM before any sleep"):
            FakeTablet.setUp(self)
            SleepTestTest.setUp(self)
            (self.tmp / "freeze-fails").write_text("/\n")
            result = self.run_test("ro", "1", "restart-ok", "hold=0")
            self.assertEqual(result.returncode, 1, self.out)
            self.assertIn("stopped: swap on the disk stays off until the next boot; pm_debug_messages stays 1 until "
                          "the next boot (it was 0)", self.out)
            self.assertEqual(self.sysrq(), "")
        with self.subTest("a stop on the disk"):
            FakeTablet.setUp(self)
            SleepTestTest.setUp(self)
            (self.tmp / "swapoff-fails").touch()
            result = self.run_test("ro", "1", "restart-ok", "hold=0")
            self.assertEqual(result.returncode, 1, self.out)
            self.assertIn("STOPPED before freezing anything: swapoff /swapfile failed; nothing on the disk changed but "
                          f"{self.kit}/logs/", self.out)
            self.assertIn("; pm_debug_messages stays 1 until the next boot (it was 0)", self.out)

    def test_a_cycle_with_no_wakeup_irq_on_record_says_so(self):
        """pm_wakeup_irq has no data when no wakeup IRQ was recorded (a pm_test
        cycle ends by itself; 2026-11-22 13:42 and 14:00 printed "woken by IRQ
        ?"), and at platform timekeeping is never suspended ("asleep ?s"): the
        cycle line says so.  The kernel's own "Triggering wakeup from IRQ N"
        names the IRQ when the file says nothing."""
        result = self.run_test("ro", "1", "pm_test=core", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("cycle 1/1: no \"Timekeeping suspended for\" line (timekeeping was not suspended), no wakeup IRQ "
                      "recorded (pm_wakeup_irq has no data), rtcwake 0, suspend_stats success +1 fail +0", self.out)
        self.assertNotIn("?s,", self.out)
        self.assertNotIn("IRQ ?", self.out)
        FakeTablet.setUp(self)
        SleepTestTest.setUp(self)
        self.cycle(1, irq="", log="[  700.000000] PM: suspend entry (deep)\n"
                                  "[  730.000000] Timekeeping suspended for 30.002 seconds\n"
                                  "[  730.000100] PM: Triggering wakeup from IRQ 21\n"
                                  "[  730.100000] PM: suspend exit\n")
        result = self.run_test("ro", "1", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("cycle 1/1: asleep 30.0s, woken by IRQ 21, rtcwake 0", self.out)
    """The RAM root of sleep-test.sh, made by its own functions from this
    host's real tools (dash, grep, mawk, and Rust coreutils, which refuse to
    run under a loader started by hand), entered by its own in_ram_root in a
    user namespace whose /run is a tmpfs, as /run is on the tablet: every
    tool runs there, at its own path, and nothing of the host's disk is in
    reach."""

    def test_the_tools_run_in_the_ram_root_with_nothing_of_the_disk_in_reach(self):
        if not shutil.which("unshare"):
            self.skipTest("no unshare")
        probe = subprocess.run(["unshare", "-rm", "true"], capture_output=True)
        if probe.returncode != 0:
            self.skipTest("no unprivileged mount namespace here")
        text = (KIT_SRC / "sleep-test.sh").read_text()
        blocks = [re.search(pattern, text, re.M | re.S).group(0) for pattern in (
            r"^for d in bin sbin lib lib32 lib64 libx32; do\n.*?^done\n",
            r"^root_copy\(\) \{.*?^\}\n", r"^ram_tool\(\) \{.*?^\}\n",
            r"^in_ram_root\(\) \{.*?^\}\n")]
        tools = re.search(r"^ram_tools='([^']*)'$", text, re.M).group(1).split()
        self.assertEqual(tools, ["rtcwake", "dmesg", "dd", "sha256sum", "sleep", "grep", "awk", "sync",
                                 "blockdev", "fsfreeze", "nsenter", "readlink"])
        inside = r"""
            PATH=$1/tools
            set -e
            # The host's files are out of reach: only what was copied is here.
            [ ! -e /usr/bin/ls ] && [ ! -e /etc/passwd ] && [ ! -e /home ]
            sleep 0
            sync
            [ "$(awk 'BEGIN { print 6 * 7 }')" = 42 ]
            [ "$(printf 'a\nb\n' | grep -c b)" = 1 ]
            [ "$(dd if=/dev/zero bs=4096 count=4 status=none | sha256sum)" = \
                "4fe7b59af6de3b665b67788cc2f99892ab827efae3a467342b3bb4e3bc8e5bfe  -" ]
            for t in dmesg rtcwake blockdev fsfreeze nsenter; do $t --version > /dev/null; done
            [ "$(readlink -f /tools/..)" = / ]
            # /proc, /sys and /dev are the system's.
            read -r up _ < /proc/uptime && [ -e /sys/kernel ] && [ -c /dev/null ]
            echo inside-ok
        """
        script = ("set -u\nstop() { echo \"$*\"; exit 1; }\n"
                  "mount -t tmpfs gts9u-run /run && mkdir -p /run/gts9u-sleep-test/t &&\n"
                  "mount -t tmpfs t /run/gts9u-sleep-test/t || exit 1\n"
                  "ramdir=/run/gts9u-sleep-test/t\n"
                  'mkdir -p "$ramdir/tools"; : > "$ramdir/copied"\n' + "".join(blocks) +
                  "ram_tool sh /bin/sh || exit 1\n"
                  f"for t in {' '.join(tools)}; do\n"
                  '  ram_tool "$t" "$(command -v "$t")" || exit 1\n'
                  "done\n"
                  "ram_prefix=\n"
                  'in_ram_root "$ramdir/tools/sh" -c "$1" sh "$ramdir"\n')
        result = subprocess.run(["unshare", "-rm", "sh", "-c", script, "ram-root", inside],
                                capture_output=True, text=True)
        self.assertEqual(result.stdout.strip().splitlines()[-1:], ["inside-ok"],
                         result.stdout + result.stderr)


    # Revision 11: pcie_0_gdsc OFF_ON from the boot, its debug switch Y and
    # debug_boot_state saying so (r11_switches).

    def test_revision_11_s_default_is_named_with_its_boot_state_and_needs_no_switch(self):
        """On revision 11's kernel a run with the root port removed needs no
        gdsc=: it writes no switch, and every verdict, RESULT and progress
        line says pcie0 is Y and that it booted so; the header says what
        pcie_0_gdsc does in the run's sleeps."""
        (self.root / "dev/pmsg0").write_text("")
        self.pci_tree()
        self.r11_switches()
        result = self.run_test("ro", "1", "pci=remove", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        on = "switches: pcie0 Y, pcie0phy N, usb N, cxmx N, rpd3 N; at boot: pcie0 Y"
        self.assertEqual(self.read("switches-at-freeze"), "Y N N N N \n")
        self.assertEqual(self.read("switches-at-sleep"), "Y N N N N \n")
        self.assertIn("SLEEP-TEST cycle 1/1: PASS: deep (aosd +1 cxsd +1 ddr +1), resumed, the UFS read back as "
                      f"before; floor released; {on}\n", self.out)
        self.assertIn(f"kernel 68d243e908c0; options: pci=remove; {on}\n", self.out)
        self.assertFalse([line for line in self.progress_lines() if "debug switches set" in line])
        (logs,) = (self.kit / "logs").glob("*-sleep")
        before = (logs / "before").read_text()
        self.assertIn(f"debug {on} (", before)
        self.assertIn(f"debug switches at boot: {R11_BOOT} ({self.root}/sys/module/gcc_sm8550/parameters/"
                      "debug_boot_state); pcie_0_gdsc in this run's sleeps: OFF_ON, collapsed when the PCIe host has "
                      "powered down\n", before)
        self.assertIn("ADSP clients: iio-sensor-proxy inactive, hexagonrpcd-adsp-sensorspd inactive, "
                      "hexagonrpcd-adsp-rootpd inactive; PipeWire and WirePlumber processes 0; sound streams not "
                      "closed: none\n", before)

    def boot_log(self, *lines, journal=False):
        """This boot's kernel log before the run, with LINES (the PCIe host's
        "system sleep:" lines of the boot's panel cycle and earlier sleeps),
        in the ring buffer (dmesg) or, with JOURNAL, only in the journal (a
        ring buffer that dropped them)."""
        text = "".join(f"[   2{i}.500000] {line}\n" for i, line in enumerate(lines))
        if journal:
            with (self.root / "journal").open("a") as f:
                f.write(text)
        else:
            with (self.root / "dev/kmsg").open("a") as f:
                f.write(text)

    def test_revision_11_s_default_with_the_pci_devices_kept_needs_a_guard_that_held(self):
        """A sleep with the PCI devices kept on a kernel that lets pcie_0_gdsc
        collapse goes ahead without pci=keep only when this boot's kernel log
        says its guard held (a kept link with the domain on, and never the
        domain off under a kept link), read from dmesg or, if the ring
        buffer has none of the host's lines, the journal; otherwise it is
        refused with nothing changed, unless the run removes the PCI
        devices, keeps the domain in retention (gdsc-ret=pcie0) or says
        pci=keep; dry and the pm_test= levels before the noirq phase are
        not refused; a kernel whose pcie0 booted N (revision 10's) is not
        either."""
        refused = ("pcie_0_gdsc collapses in this kernel's suspend (its debug switch pcie0 booted Y: revision 11), "
                   "and {why}, so with the PCI devices kept genpd may collapse that GDSC under the live PCIe host: "
                   "give pci=remove, or gdsc-ret=pcie0 to keep it in retention for the run, or pci=keep to run it "
                   'so (the Wi-Fi may not come back, or the tablet may crash at the resume: README, "Revision 11")')
        none = ('it has no guard (no "system sleep:" line of the PCIe host in this boot\'s kernel log: revision 11 '
                "as first prepared, or no suspend yet)")
        unseen = ("its guard's line of a kept link with the domain on is not in this boot's kernel log (the first "
                  "sleep logged: the host powered down, and genpd powered its power domain off)")
        failed = ('its guard did not hold this boot (a sleep logged "the link was kept, but genpd powered its power '
                  'domain off")')
        for lines, journal, args, why in (
                ((), False, ("ro", "1"), none),
                ((), False, ("ro", "1", "pm_test=platform"), none),
                ((), False, ("ro", "1", "mode=s2idle"), none),
                ((GUARD_DOWN,), False, ("ro", "1"), unseen),
                ((GUARD_KEPT, GUARD_BAD), False, ("ro", "1"), failed),
                ((GUARD_KEPT,), False, ("ro", "1"), None),
                ((GUARD_KEPT, GUARD_DOWN), False, ("ro", "1", "mode=s2idle"), None),
                ((GUARD_KEPT,), True, ("ro", "1"), None),
                ((), False, ("ro", "1", "pm_test=devices"), None),
                ((), False, ("ro", "1", "pm_test=freezer"), None),
                ((), False, ("dry",), None),
                ((GUARD_BAD,), False, ("ro", "1", "pci=keep"), None),
                ((), False, ("ro", "1", "gdsc-ret=pcie0"), None),
                ((GUARD_BAD,), False, ("ro", "1", "pci=remove"), None)):
            with self.subTest(lines=lines, journal=journal, args=args):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                self.pci_tree()
                self.r11_switches()
                self.s2idle_files()
                self.boot_log(*lines, journal=journal)
                result = self.run_test(*args, "restart-ok", "hold=0")
                if why is None:
                    self.assertNotIn("REFUSED", self.out)
                    self.assertEqual(self.sysrq(), "b", self.out)
                else:
                    self.assertEqual(result.returncode, 1, self.out)
                    self.assertIn("REFUSED: " + refused.format(why=why), self.out)
                    self.assert_nothing_done()
        # What the record says of the guard.
        FakeTablet.setUp(self)
        SleepTestTest.setUp(self)
        self.pci_tree()
        self.r11_switches()
        self.boot_log(GUARD_KEPT, GUARD_DOWN)
        self.run_test("ro", "1", "restart-ok", "hold=0")
        (logs,) = (self.kit / "logs").glob("*-sleep")
        self.assertIn("PCIe power domain guard: held this boot (2 sleeps logged, none with the domain off under a "
                      "kept link; the first: the link was kept, and its power domain stayed on)\n",
                      (logs / "before").read_text())
        # Revision 10's kernel: every switch boots N, nothing to refuse.
        FakeTablet.setUp(self)
        SleepTestTest.setUp(self)
        self.pci_tree()
        self.debug_switches()
        self.run_test("ro", "1", "restart-ok", "hold=0")
        self.assertNotIn("REFUSED", self.out)
        self.assertEqual(self.sysrq(), "b", self.out)

    def test_a_guard_that_does_not_hold_in_a_sleep_ends_the_run(self):
        """With the PCI devices kept (a guard that held at boot, pci=keep or
        not), a cycle whose kernel lines have the guard's warning ends the
        run with PCIE-DOWN and its restart; its pcie evidence names what the
        host logged."""
        for extra in ((), ("pci=keep",)):
            with self.subTest(extra):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                self.pci_tree()
                self.r11_switches()
                self.boot_log(GUARD_KEPT)
                self.cycle(1, log=f"[  100.000001] PM: suspend entry (deep)\n[  100.000002] {GUARD_BAD}\n"
                                  "[  100.000004] Timekeeping suspended for 30.002 seconds\n"
                                  "[  100.000005] PM: suspend exit\n")
                result = self.run_test("ro", "2", *extra, "restart-ok", "hold=0")
                self.assertEqual(result.returncode, 1, self.out)
                self.assert_read_only_rules()
                self.assertIn("cycle 1/2: evidence: pcie: no link-up line: the PCIe host kept its link, clocks and "
                              "votes through the sleep; the host logged: the link was kept, but genpd powered its "
                              "power domain off; PCIe came back (no host failure line)\n", self.out)
                self.assertIn(f"SLEEP-TEST RESULT: PCIE-DOWN after cycle 1/2: with the PCI devices kept, the PCIe "
                              f"host or the Wi-Fi did not come back ({GUARD_BAD}), and a later suspend would meet a "
                              "controller that lost its registers under its link, so the run ends here", self.out)
                self.assertEqual(self.read("rtcwake.calls").count("-m mem -s 30"), 1)
                self.assertEqual(self.sysrq(), "b")

    def test_root_port_d3_lets_the_root_port_go_to_d3hot_for_a_pci_keep_run(self):
        """root-port-d3 on revision 11's kernel, with pci=keep and a guard
        that held this boot: Y to its switch right before the freeze, read
        back, named in the header, every verdict and the RESULT line; the
        cycle's evidence names the host's power-down and the Wi-Fi's
        firmware load.  Nothing puts it back: the restart does."""
        (self.root / "dev/pmsg0").write_text("")
        self.pci_tree()
        self.r11_switches()
        self.boot_log(GUARD_KEPT)
        self.cycle(1, log="[  100.000001] PM: suspend entry (deep)\n"
                          "[  100.000004] Timekeeping suspended for 30.002 seconds\n"
                          "[  100.000005] qcom-pcie 1c00000.pcie: PCIe Gen.2 x1 link up\n"
                          f"[  100.000006] {GUARD_DOWN}\n"
                          "[  100.000007] ath11k_pci 0000:01:00.0: fw_version 0x11021302 fw_build_timestamp "
                          "2026-02-04 08:10\n[  100.000008] PM: suspend exit\n")
        result = self.run_test("ro", "1", "pci=keep", "root-port-d3", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        on = "switches: pcie0 Y, pcie0phy N, usb N, cxmx N, rpd3 Y; at boot: pcie0 Y"
        self.assertEqual(self.read("switches-at-freeze"), "Y N N N Y \n")
        self.assertEqual(self.read("switches-at-sleep"), "Y N N N Y \n")
        self.assertEqual((self.root / "sys/module" / RPD3_SWITCH).read_text(), "Y")
        self.assertEqual(self.sysrq(), "b")
        self.assertIn("pci=keep: the PCI devices stay, and with root-port-d3 the root port goes to D3hot after the "
                      "Wi-Fi in each sleep, so that the PCIe host powers itself down and is set up again at the "
                      "resume; pcie_0_gdsc collapses once the host is down (the guard marks it out-of-band)", self.out)
        self.assertIn(f"root-port-d3: right before it freezes the disks the run writes Y to {self.root}/sys/module/"
                      "pcie_qcom/parameters/debug_root_port_d3 and reads it back; it stays so until the restart "
                      "that ends the run", self.out)
        self.assertIn("the host logged: the host powered down, and genpd powered its power domain off; PCIe came "
                      "back (no host failure line)\n", self.out)
        self.assertIn("cycle 1/1: evidence: wifi: pci 0000:00:00.0 - D0 -, 0000:01:00.0 - D0 -; no Wi-Fi failure "
                      "line; ath11k firmware loads 1\n", self.out)
        self.assertIn(f"kernel 68d243e908c0; options: pci=keep root-port-d3; {on}\n", self.out)
        self.assertIn(f"progress: debug switches set: rpd3; {on}", self.progress_lines())

    def test_root_port_d3_is_refused_without_its_switch_or_a_guard_that_held(self):
        """root-port-d3 needs the switch (revision 11's kernel), reading N as
        it boots, and a guard that held this boot; a stop before any sleep
        puts it back to N."""
        for name, switches, lines, refused in (
                ("no switch", {"rpd3": None}, (GUARD_KEPT,),
                 "this kernel does not offer the debug switch rpd3 ({switch}): the kernels of kit revisions 9 to 11 "
                 "have it, revision 11's alone the root port's (sh ~/typec-roles/verify.sh says which kernel runs)"),
                ("no guard", {}, (), 'root-port-d3 needs a guard that held this boot (a sleep that logged "the link '
                                    'was kept, and its power domain stayed on", none that logged it off): guard none'),
                ("a guard that failed", {}, (GUARD_KEPT, GUARD_BAD),
                 'root-port-d3 needs a guard that held this boot (a sleep that logged "the link was kept, and its '
                 'power domain stayed on", none that logged it off): guard failed, the first sleep logged: the link '
                 'was kept, and its power domain stayed on'),
                ("set since the boot", {"rpd3": "Y"}, (GUARD_KEPT,),
                 "a debug switch does not read as this kernel booted it: rpd3 ({switch}: Y) (at boot: pcie0 Y, the "
                 "others N;")):
            with self.subTest(name):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                self.pci_tree()
                self.r11_switches(**switches)
                self.boot_log(*lines)
                result = self.run_test("ro", "1", "pci=keep", "root-port-d3", "restart-ok", "hold=0")
                self.assertEqual(result.returncode, 1, self.out)
                self.assertIn("REFUSED: " + refused.replace("{switch}", f"{self.root}/sys/module/{RPD3_SWITCH}"),
                              self.out)
                self.assert_nothing_done()
        # A stop before any sleep: the switch back to N.
        FakeTablet.setUp(self)
        SleepTestTest.setUp(self)
        self.pci_tree()
        self.r11_switches()
        self.boot_log(GUARD_KEPT)
        (self.tmp / "freeze-fails").write_text("/media/stick\n")
        result = self.run_test("ro", "1", "pci=keep", "root-port-d3", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("the debug switches it set are back to N", self.out)
        self.assertEqual((self.root / "sys/module" / RPD3_SWITCH).read_text().strip(), "N")
        self.assertEqual(self.sysrq(), "")

    def test_sensors_stop_stops_the_audio_dsp_s_sensor_clients_before_the_run(self):
        """sensors=stop: systemctl stop of the two units after the record of
        the state before the run and before the freeze, their state after
        it in the log, the option in every RESULT line; a unit still running
        stops the run before anything is frozen, and says that the two stay
        stopped until the next boot."""
        for unit in ("iio-sensor-proxy", "hexagonrpcd-adsp-sensorspd", "hexagonrpcd-adsp-rootpd"):
            (self.tmp / f"active-{unit}.service").write_text("active\n")
        self.pci_tree()
        result = self.run_test("ro", "1", "pci=remove", "sensors=stop", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        self.assertIn("stop iio-sensor-proxy.service hexagonrpcd-adsp-sensorspd.service\n",
                      self.read("systemctl.calls"))
        (logs,) = (self.kit / "logs").glob("*-sleep")
        self.assertIn("ADSP clients: iio-sensor-proxy active, hexagonrpcd-adsp-sensorspd active, "
                      "hexagonrpcd-adsp-rootpd active; PipeWire and WirePlumber processes 0; sound streams not "
                      "closed: none; sensors=stop: this run stops iio-sensor-proxy.service "
                      "hexagonrpcd-adsp-sensorspd.service next, until the restart\n", (logs / "before").read_text())
        log = (self.ramdir() / "log").read_text()
        order = ["sensors=stop: systemctl stop iio-sensor-proxy.service hexagonrpcd-adsp-sensorspd.service "
                 "(auto-rotation and auto-brightness are off until the restart)",
                 "sensors=stop: ADSP clients now: iio-sensor-proxy inactive, hexagonrpcd-adsp-sensorspd inactive, "
                 "hexagonrpcd-adsp-rootpd active (systemctl stop status 0); 10s to settle",
                 "frozen: /media/stick", "cycle 1/1: deep sleep"]
        self.assertEqual([log.index(said) for said in order], sorted(log.index(said) for said in order))
        self.assertIn("options: pci=remove sensors=stop; switches: not offered by this kernel\n", self.out)
        self.assertEqual(self.sysrq(), "b")
        # One that does not stop: nothing frozen, nothing slept.
        FakeTablet.setUp(self)
        SleepTestTest.setUp(self)
        for unit in ("iio-sensor-proxy", "hexagonrpcd-adsp-sensorspd"):
            (self.tmp / f"active-{unit}.service").write_text("active\n")
        (self.tmp / "stop-ignored").write_text("hexagonrpcd-adsp-sensorspd.service\n")
        result = self.run_test("ro", "1", "sensors=stop", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("STOPPED before freezing anything: sensors=stop: still running after systemctl stop (status "
                      "1): hexagonrpcd-adsp-sensorspd.service; ADSP clients now: iio-sensor-proxy inactive, "
                      "hexagonrpcd-adsp-sensorspd active, hexagonrpcd-adsp-rootpd inactive", self.out)
        self.assertIn("iio-sensor-proxy and hexagonrpcd-adsp-sensorspd stay stopped until the next boot (sudo "
                      "reboot brings them back)", self.out)
        self.assertEqual(self.read("frozen"), "")
        self.assertEqual(self.read("rtcwake.calls").count("-m mem"), 0)
        self.assertEqual(self.sysrq(), "")

    def sensors_hook(self, quiesces=True):
        """The device package's sensors hook as the stand-in root has it: one
        that stops the Sensor Core's clients before a sleep (this branch's),
        or the earlier one, which stops neither."""
        hook = self.root / "usr/lib/systemd/system-sleep/ubuntu-gts9u-sensors"
        hook.parent.mkdir(parents=True, exist_ok=True)
        body = ('\t\tsystemctl stop iio-sensor-proxy.service 2>/dev/null || true\n'
                '\t\tsystemctl stop hexagonrpcd-adsp-sensorspd.service 2>/dev/null || true\n') if quiesces else ""
        hook.write_text('#!/bin/sh\necho "hook $*" >> "$HOME/hook.calls"\ncase "${1-}" in\n\tpre)\n'
                        '\t\tsystemctl stop ubuntu-gts9u-sensors-resume.service 2>/dev/null || true\n'
                        f"{body}\t\t;;\nesac\nexit 0\n")

    def test_sensors_hook_runs_the_device_package_s_sleep_hook_as_a_systemd_suspend_does(self):
        """sensors=hook: the device package's system-sleep hook's pre step
        (pre suspend, as systemd-sleep runs it) in place of sensors=stop's
        systemctl stop, checked the same way; no post step (the run ends with
        a restart).  Refused, nothing done, with a hook that does not stop
        iio-sensor-proxy, and on a dry run."""
        for unit in ("iio-sensor-proxy", "hexagonrpcd-adsp-sensorspd"):
            (self.tmp / f"active-{unit}.service").write_text("active\n")
        self.sensors_hook()
        result = self.run_test("ro", "1", "sensors=hook", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        self.assertEqual(self.read("hook.calls"), "hook pre suspend\n")
        calls = self.read("systemctl.calls")
        self.assertLess(calls.index("stop iio-sensor-proxy.service\n"),
                        calls.index("stop hexagonrpcd-adsp-sensorspd.service\n"))
        log = (self.ramdir() / "log").read_text()
        hook = f"{self.root}/usr/lib/systemd/system-sleep/ubuntu-gts9u-sensors"
        order = [f"sensors=hook: {hook} pre suspend, as systemd-sleep runs it before a systemctl suspend (it stops "
                 "iio-sensor-proxy.service hexagonrpcd-adsp-sensorspd.service; auto-rotation and auto-brightness "
                 "are off until the restart, which runs no post)",
                 "sensors=hook: ADSP clients now: iio-sensor-proxy inactive, hexagonrpcd-adsp-sensorspd inactive, "
                 "hexagonrpcd-adsp-rootpd inactive (the hook status 0); 10s to settle",
                 "frozen: /media/stick", "cycle 1/1: deep sleep"]
        self.assertEqual([log.index(said) for said in order], sorted(log.index(said) for said in order))
        self.assertIn("options: sensors=hook; switches: not offered by this kernel\n", self.out)
        (logs,) = (self.kit / "logs").glob("*-sleep")
        self.assertIn("; sensors=hook: this run stops iio-sensor-proxy.service hexagonrpcd-adsp-sensorspd.service "
                      f"next through {hook} pre suspend, until the restart\n", (logs / "before").read_text())
        # The earlier hook, and a dry run: refused, nothing done.
        for quiesces, args in ((False, ("ro", "1", "sensors=hook", "restart-ok")),
                               (True, ("dry", "sensors=hook", "restart-ok"))):
            with self.subTest(args=args):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                self.sensors_hook(quiesces)
                hook = f"{self.root}/usr/lib/systemd/system-sleep/ubuntu-gts9u-sensors"
                result = self.run_test(*args)
                self.assertNotEqual(result.returncode, 0, self.out)
                if quiesces:
                    self.assertIn("dry sleeps not at all: sensors=hook is for the audio DSP's wakes in a sleep",
                                  self.out)
                else:
                    self.assertIn("REFUSED: sensors=hook needs the device package's system-sleep hook that stops "
                                  f"the sensor clients before a sleep ({hook}, naming iio-sensor-proxy.service); "
                                  "this tablet's does not: sensors=stop stops them by hand", self.out)
                    self.assert_nothing_done()
                self.assertEqual(self.read("hook.calls"), "")
                self.assertEqual(self.read("frozen"), "")

    def test_cxpc_has_the_aop_log_what_held_cx_in_each_sleep(self):
        """cxpc (revision 12's kernel): Y to the CXPC violators switch right
        before the freeze, with the other switches; the AOP's log as it is
        and every requester's DDR vote now in the record, read before
        anything is frozen; after each sleep the summary of the log the
        kernel logged, among the evidence and in pmsg, and its lines among
        the cycle's kernel lines.  Refused on a kernel without the switch,
        and with dry."""
        (self.root / "dev/pmsg0").write_text("")
        self.debug_switches()
        switch = self.root / "sys/module/qcom_stats/parameters/debug_cxpc_violators"
        switch.parent.mkdir(parents=True, exist_ok=True)
        switch.write_text("N\n")
        stats = self.root / "sys/kernel/debug/qcom_stats"
        stats.mkdir(parents=True, exist_ok=True)
        (stats / "cxpc_violators").write_text("no log\n")
        (stats / "ddr_votes").write_text("TZ          absent\nHLOS        ab 0 ib 0\nAUDIO       ab 12 ib 40\n")
        log = ("[  700.000000] PM: suspend entry (deep)\n"
               "[  700.100000] cxpc violators: the AOP's CXPC monitor is on for this suspend\n"
               "[  730.000000] Timekeeping suspended for 30.002 seconds\n"
               "[  730.200000] cxpc violators: mode 0xcc (CXPC), 3 of 36 lines, every 1000 ms, time shift 0, flush 10\n"
               "[  730.200001] cxpc violators: 0x1000: AUDIO 3, HLOS 1\n"
               "[  730.200002] cxpc violators: 0x2000: AUDIO 3\n"
               "[  730.200003] cxpc violators: 0x3000: CXPC enter\n"
               "[  730.200004] cxpc violators: summary: 3 lines, 1 of them CXPC enter or exit; DRVs: HLOS 1/3, "
               "AUDIO 2/3 (every line)\n"
               "[  730.300000] PM: suspend exit\n")
        self.cycle(1, log=log)
        result = self.run_test("ro", "1", "cxpc", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        self.assertEqual(switch.read_text(), "Y")
        self.assertEqual(self.read("switches-at-freeze"), "N N N N Y \n")
        self.assertEqual(self.read("switches-at-sleep"), "N N N N Y \n")
        self.assertIn("SLEEP-TEST RESULT: PASS: 1 of 1", self.out)
        on = "switches: pcie0 N, pcie0phy N, usb N, cxmx N, cxpc Y"
        self.assertIn(f"kernel 68d243e908c0; options: cxpc; {on}\n", self.out)
        self.assertIn(f"progress: debug switches set: cxpc; {on}", self.progress_lines())
        (logs,) = (self.kit / "logs").glob("*-sleep")
        before = (logs / "before").read_text()
        self.assertIn(f"; {self.root}/sys/module/qcom_stats/parameters/debug_cxpc_violators); this run sets cxpc to Y "
                      "right before the freeze\n", before)
        self.assertIn(f"cxpc: the AOP's CXPC log as it is now ({self.root}/sys/kernel/debug/qcom_stats/cxpc_violators): "
                      "no log\n", before)
        self.assertIn(f"cxpc: every requester's DDR vote now ({self.root}/sys/kernel/debug/qcom_stats/ddr_votes): "
                      "HLOS ab 0 ib 0, AUDIO ab 12 ib 40\n", before)
        summary = "3 lines, 1 of them CXPC enter or exit; DRVs: HLOS 1/3, AUDIO 2/3 (every line)"
        self.assertIn("cycle 1/1: evidence: cxpc violators (the AOP log; each of its lines is among the kernel lines of "
                      f"this cycle): {summary}\n", self.out)
        self.assertIn("cycle 1/1: evidence: kernel: [  730.200001] cxpc violators: 0x1000: AUDIO 3, HLOS 1\n", self.out)
        self.assertIn(f"cycle 1/1: evidence: cxpc: {summary}", self.pmsg_lines())
        # A kernel without the switch, and dry: refused, nothing done.
        FakeTablet.setUp(self)
        SleepTestTest.setUp(self)
        self.debug_switches()
        result = self.run_test("ro", "1", "cxpc", "restart-ok")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("REFUSED: cxpc needs the CXPC violators switch "
                      f"({self.root}/sys/module/qcom_stats/parameters/debug_cxpc_violators): the kernels of kit "
                      "revision 12 have it", self.out)
        self.assert_nothing_done()
        result = self.run_test("dry", "cxpc", "restart-ok")
        self.assertEqual(result.returncode, 2, self.out)
        self.assertIn("dry sleeps not at all: cxpc is for the AOP's log of a sleep", self.out)

    def test_gdsc_ret_pcie0_keeps_the_domain_in_retention_for_the_run(self):
        """gdsc-ret=pcie0 writes N to pcie0 right before the freeze, read
        back, and the record says it: pcie0 N with "at boot: pcie0 Y"."""
        (self.root / "dev/pmsg0").write_text("")
        self.pci_tree()
        self.r11_switches()
        result = self.run_test("ro", "1", "pci=remove", "gdsc-ret=pcie0", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        off = "switches: pcie0 N, pcie0phy N, usb N, cxmx N, rpd3 N; at boot: pcie0 Y"
        self.assertEqual(self.read("pcie0-at-pci-remove"), "Y\n")
        self.assertEqual(self.read("switches-at-freeze"), "N N N N N \n")
        self.assertIn(f"kernel 68d243e908c0; options: pci=remove gdsc-ret=pcie0; {off}\n", self.out)
        self.assertIn(f"progress: debug switches set: (N: pcie0); {off}", self.progress_lines())
        self.assertIn("gdsc-ret=pcie0: right before it freezes the disks, after the PCI removal, the run writes N to "
                      "its switch: pcie_0_gdsc stays on in retention in the sleeps, as on revision 10, where every "
                      "such sleep with the PCI devices removed and the floor released hung (8 of 8): a control "
                      "run", self.out)
        (logs,) = (self.kit / "logs").glob("*-sleep")
        self.assertIn("pcie_0_gdsc in this run's sleeps: RET_ON, left on\n", (logs / "before").read_text())

    def test_a_switch_that_does_not_read_as_it_booted_is_refused_and_a_stop_puts_them_back_so(self):
        """On revision 11's kernel the boot state is the reference: pcie0 N
        there is a write since the boot, refused; a stop before any sleep
        puts what the run wrote back as it booted (pcie0 Y)."""
        self.pci_tree()
        self.r11_switches(pcie0="N")
        result = self.run_test("ro", "1", "pci=remove", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn(f"REFUSED: a debug switch does not read as this kernel booted it: pcie0 ({self.root}/sys/module/"
                      f"gcc_sm8550/parameters/debug_pcie0_off: N) (at boot: pcie0 Y, the others N; {self.root}/sys/"
                      "module/gcc_sm8550/parameters/debug_boot_state)", self.out)
        self.assert_nothing_done()
        FakeTablet.setUp(self)
        SleepTestTest.setUp(self)
        self.pci_tree()
        self.r11_switches()
        (self.tmp / "freeze-fails").write_text("/media/stick\n")
        result = self.run_test("ro", "1", "pci=remove", "gdsc-ret=pcie0", "gdsc=usb", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("the debug switches it set are back as they booted", self.out)
        self.assertEqual({word: (value or "").strip() for word, value in self.switch_files().items()},
                         {"pcie0": "Y", "pcie0phy": "N", "usb": "N", "cxmx": "N"})
        self.assertEqual(self.sysrq(), "")

    def test_pci_keep_ends_the_run_when_the_wifi_did_not_come_back(self):
        """pci=keep on revision 11's kernel: the Wi-Fi's state after the
        resume in the evidence, and a cycle whose kernel lines say the Wi-Fi
        did not come back ends the run with PCIE-DOWN and its restart; one
        whose lines do not goes on."""
        lost = "pci 0000:01:00.0: Unable to change power state from D3hot to D0, device inaccessible"
        self.pci_tree()
        self.r11_switches()
        self.cycle(1, log=f"[  100.000001] PM: suspend entry (deep)\n[  100.000002] {lost}\n"
                          "[  100.000003] ath11k_pci 0000:01:00.0: failed to resume core: -110\n"
                          "[  100.000004] Timekeeping suspended for 30.002 seconds\n[  100.000005] PM: suspend exit\n")
        result = self.run_test("ro", "2", "pci=keep", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        self.assertIn("pci=keep: the PCI devices stay and the PCIe host keeps its link through each sleep; "
                      "pcie_0_gdsc may collapse under the kept link (OFF_ON, and no guard that held this boot): the "
                      "Wi-Fi may not come back, or the tablet may crash at the resume and restart by itself; a cycle "
                      "whose kernel lines say the PCIe host or the Wi-Fi did not come back, or that the guard did not "
                      "hold, ends the run (PCIE-DOWN); give it one cycle", self.out)
        self.assertIn(f"cycle 1/2: evidence: wifi: pci 0000:00:00.0 - D0 -, 0000:01:00.0 - D0 -; {lost}; ath11k "
                      "firmware loads 0\n", self.out)
        self.assertIn(f"SLEEP-TEST RESULT: PCIE-DOWN after cycle 1/2: with the PCI devices kept, the PCIe host or "
                      f"the Wi-Fi did not come back ({lost}), and a later suspend would meet a controller that lost "
                      "its registers under its link, so the run ends here; 1 of 2 cycles came back with the UFS "
                      "readable", self.out)
        self.assertEqual(self.read("rtcwake.calls").count("-m mem -s 30"), 1)
        self.assertEqual(self.sysrq(), "b")
        # A cycle without such a line: the run goes on.
        FakeTablet.setUp(self)
        SleepTestTest.setUp(self)
        self.pci_tree()
        self.r11_switches()
        self.run_test("ro", "2", "pci=keep", "restart-ok", "hold=0")
        self.assertIn("cycle 1/2: evidence: wifi: pci 0000:00:00.0 - D0 -, 0000:01:00.0 - D0 -; no Wi-Fi failure "
                      "line; ath11k firmware loads 0\n", self.out)
        self.assertIn("SLEEP-TEST RESULT: PASS: 2 of 2 cycles", self.out)

    def rpmh_files(self, tracing_on="1", clock="[local] global counter uptime perf mono mono_raw boot tai"):
        """tracefs with the RPMh tracepoint off, tracing on, the trace with
        a line of an earlier command, and the command DB, as squirtle's
        kernel has them (made-up addresses)."""
        tr = self.root / "sys/kernel/tracing"
        (tr / "events/rpmh/rpmh_send_msg").mkdir(parents=True, exist_ok=True)
        (tr / "events/rpmh/rpmh_send_msg/enable").write_text("0\n")
        (tr / "tracing_on").write_text(tracing_on + "\n")
        (tr / "trace_clock").write_text(clock + "\n")
        (tr / "trace").write_text("# tracer: nop\n" + rpmh_line(5.0, "active", "0x30000", "0x2"))
        (self.root / "sys/kernel/debug").mkdir(parents=True, exist_ok=True)
        (self.root / "sys/kernel/debug/cmd-db").write_text(CMD_DB)

    def test_rpmh_names_the_last_flush_before_each_sleep_and_the_levels_it_left_out(self):
        """rpmh: the tracepoint on right before the freeze and the trace
        emptied right before each sleep; after it, the last flush before the
        kernel's "Timekeeping suspended" line, by the command DB's names,
        with xo.lvl, cx.lvl or mx.lvl that it left out (held at their vote);
        a sleep with no flush in its trace says so.  The tracepoint stays on
        to the restart."""
        (self.root / "dev/pmsg0").write_text("")
        self.rpmh_files()
        # Cycle 1: two flushes before the sleep (at 100.000030), the second
        # (2 ms after the first) with XO off and MC0 without a vote in the
        # sleep set, SH0 with one, and an LDO's enable register; cx.lvl
        # voted 0 active, mx.lvl left out with no vote in the trace; a flush
        # after the sleep.
        self.cycle(1)
        (self.tmp / "cycle-1/trace").write_text(
            rpmh_line(99.990000, "active", "0x30000", "0x0")
            + rpmh_line(99.998000, "sleep", "0x30010", "0x1") + rpmh_line(99.998001, "wake", "0x30010", "0x2")
            + rpmh_line(100.000010, "sleep", "0x50000", "0x40000000")
            + rpmh_line(100.000011, "wake", "0x50000", "0x60004001")
            + rpmh_line(100.000012, "sleep", "0x30030", "0x0") + rpmh_line(100.000013, "wake", "0x30030", "0x3")
            + rpmh_line(100.000014, "sleep", "0x50004", "0x60000010")
            + rpmh_line(100.000015, "wake", "0x50004", "0x60000020")
            + rpmh_line(100.000016, "sleep", "0x40104", "0x0") + rpmh_line(100.000017, "wake", "0x40104", "0x1")
            + rpmh_line(100.500000, "sleep", "0x30030", "0x3"))
        (self.tmp / "cycle-1/log").write_text("[  100.000000] PM: suspend entry (deep)\n"
                                              "[  100.000030] Timekeeping suspended for 30.002 seconds\n"
                                              "[  100.000040] PM: suspend exit\n")
        # Cycle 2: only active votes.
        self.cycle(2)
        (self.tmp / "cycle-2/trace").write_text(rpmh_line(700.000000, "active", "0x30010", "0x1"))
        result = self.run_test("ro", "2", "rpmh", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        self.assertIn("SLEEP-TEST RESULT: PASS: 2 of 2 cycles", self.out)
        self.assertIn(f"rpmh: the tracepoint {self.root}/sys/kernel/tracing/events/rpmh/rpmh_send_msg/enable (0 now) "
                      f"goes on right before the freeze; command DB: {self.root}/sys/kernel/debug/cmd-db, 6 addresses\n",
                      self.out)
        self.assertIn("rpmh: the tracepoint rpmh_send_msg is on until the restart; the command DB names 6 RPMh "
                      "addresses", self.out)
        # On at each sleep, and the trace emptied right before it.
        self.assertEqual(self.read("rpmh-at-sleep"), "enable 1 trace 0\nenable 1 trace 0\n")
        self.assertIn("cycle 1/2: for this sleep: rpmh trace emptied\n", self.out)
        self.assertIn("cycle 1/2: evidence: rpmh sleep set: the last of 2 flushes before the sleep at 100.000030 (at "
                      "100.000010, 8 commands), sleep/wake: MC0 0x40000000/0x60004001, xo.lvl 0x0/0x3, SH0 "
                      "0x60000010/0x60000020, ldob1+4 0x0/0x1; left out, so held at their vote through the sleep: "
                      "cx.lvl (active 0x0), mx.lvl; active votes before the sleep 1, commands after it 1\n", self.out)
        self.assertIn("cycle 2/2: evidence: rpmh sleep set: no flush before the sleep at 730.000000 (1 active votes in "
                      "it): the RSC slept with the sets of an earlier flush\n", self.out)
        pmsg = [line for line in self.pmsg_lines() if ": evidence: rpmh: " in line]
        self.assertEqual(pmsg, ["cycle 1/2: evidence: rpmh: flush 2 of 2 at 100.000010, sleep/wake: xo.lvl 0x0/0x3, "
                                "SH0 0x60000010/0x60000020; held: cx.lvl (active 0x0), mx.lvl",
                                "cycle 2/2: evidence: rpmh: no flush before the sleep at 730.000000 (1 active votes in "
                                "it): the RSC slept with the sets of an earlier flush"])
        self.assertIn("options: rpmh;", self.out)
        self.assertEqual((self.root / "sys/kernel/tracing/events/rpmh/rpmh_send_msg/enable").read_text(), "1")

    def clk_events(self):
        """The clock framework's prepare and unprepare tracepoints, off."""
        for ev in ("clk_prepare", "clk_unprepare"):
            d = self.root / "sys/kernel/tracing/events/clk" / ev
            d.mkdir(parents=True, exist_ok=True)
            (d / "enable").write_text("0\n")

    def test_rpmh_names_a_suspend_to_idle_flush_at_the_sleep_s_entry_and_what_holds_xo_there(self):
        """A suspend-to-idle flushes the sets after the last CPU stopped
        sched_clock: at the time of the "Timekeeping suspended" line, which
        the trace prints up to 1 us later than the kernel log.  That flush
        is the sleep's ("the sleep's entry"), where revision 11's script
        said "no flush" (2026-11-23).  With the clock tracepoints the XO line
        names what the suspend left prepared below XO (a prepare before the
        sleep, none after it, counts), the whole flush goes to pmsg in
        pieces, and the clock tracepoints are on until the restart."""
        (self.root / "dev/pmsg0").write_text("")
        (self.root / "sys/power/mem_sleep").write_text("[s2idle]\n")
        self.s2idle_files()
        self.rpmh_files()
        self.clk_events()
        write_clocks(self.root / "sys/kernel/debug/clk", bi_tcxo=(3, 3), gcc_gpll0=(2, 2),
                     gcc_ufs_phy_ice_core_clk=(1, 1), gcc_usb30_prim_master_clk=(1, 1))
        # Cycle 1: USB's clock unprepared before the sleep, the ICE clock
        # not; the flush at the sleep's instant (708.000000 in the kernel
        # log: s2idle_log's Timekeeping line), one command 1 us later as the
        # trace rounds it, and one 2 us later, after the sleep.
        self.cycle(1, log=self.s2idle_log())
        (self.tmp / "cycle-1/trace").write_text(
            clk_line(703.000000, "clk_unprepare", "gcc_usb30_prim_master_clk")
            + rpmh_line(704.000000, "active", "0x30030", "0x3")
            + rpmh_line(708.000000, "sleep", "0x50000", "0x0") + rpmh_line(708.000000, "wake", "0x50000", "0x60004001")
            + rpmh_line(708.000001, "sleep", "0x30000", "0x0") + rpmh_line(708.000001, "wake", "0x30000", "0x2")
            + rpmh_line(708.000002, "sleep", "0x30010", "0x0")
            + clk_line(709.000000, "clk_prepare", "gcc_usb30_prim_master_clk"))
        result = self.run_test("ro", "1", "mode=s2idle", "rpmh", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        self.assertIn("rpmh: the clk_prepare and clk_unprepare tracepoints are on until the restart too (XO at each "
                      "sleep's entry)\n", self.out)
        flush = ("the last of 1 flushes before the sleep at 708.000000 (at 708.000000, the sleep's entry, 4 commands), "
                 "sleep/wake: MC0 0x0/0x60004001, cx.lvl 0x0/0x2; left out, so held at their vote through the sleep: "
                 "xo.lvl (active 0x3), mx.lvl; active votes before the sleep 1, commands after it 1")
        self.assertIn(f"cycle 1/1: evidence: rpmh sleep set: {flush}\n", self.out)
        self.assertIn("cycle 1/1: evidence: XO at the entry to the sleep (clock trace): XO still voted at the entry "
                      "to the sleep, held by: gcc_ufs_phy_ice_core_clk; unprepared in the suspend before the sleep: "
                      "gcc_usb30_prim_master_clk (1 prepares and unprepares traced before the sleep)\n", self.out)
        pmsg = [line for line in self.pmsg_lines() if ": evidence: " in line]
        self.assertIn("cycle 1/1: evidence: rpmh: flush 1 of 1 at 708.000000, the sleep's entry, sleep/wake: cx.lvl "
                      "0x0/0x2; held: xo.lvl (active 0x3), mx.lvl", pmsg)
        entry = [line for line in pmsg if ": evidence: XO at entry: " in line]
        self.assertEqual(len(entry), 1, pmsg)
        self.assertTrue(entry[0].startswith("cycle 1/1: evidence: XO at entry: XO held by gcc_ufs_phy_ice_core_clk; "
                                            "data="), entry)
        pieces = [line for line in pmsg if ": evidence: rpmh sleep set (" in line]
        self.assertEqual(len(pieces), 2, pieces)
        self.assertEqual("".join(line.split("): ", 1)[1] for line in pieces), flush)
        self.assertTrue(pieces[0].startswith("cycle 1/1: evidence: rpmh sleep set (1/2): "), pieces)
        self.assertTrue(all(len(line) <= 300 for line in pieces), pieces)
        for ev in ("clk_prepare", "clk_unprepare"):
            self.assertEqual((self.root / "sys/kernel/tracing/events/clk" / ev / "enable").read_text(), "1")

    def test_xo_at_the_sleep_s_entry_is_free_when_the_suspend_unprepared_xo_itself(self):
        """XO's own unprepare before the sleep: no clock of Linux votes it in
        the sleep set.  A deep sleep flushes before sched_clock stops, so its
        flush is before the Timekeeping line's time, as before.  Without the
        clock tracepoints the XO line says so and the run goes on."""
        (self.root / "dev/pmsg0").write_text("")
        self.rpmh_files()
        self.clk_events()
        write_clocks(self.root / "sys/kernel/debug/clk", bi_tcxo=(1, 1), gcc_gpll0=(1, 1),
                     gcc_usb30_prim_master_clk=(1, 1))
        self.cycle(1)
        (self.tmp / "cycle-1/trace").write_text(
            clk_line(701.000000, "clk_unprepare", "gcc_usb30_prim_master_clk")
            + clk_line(701.000001, "clk_unprepare", "gcc_gpll0") + clk_line(701.000002, "clk_unprepare", "bi_tcxo")
            + rpmh_line(729.999000, "sleep", "0x30030", "0x0") + rpmh_line(729.999001, "wake", "0x30030", "0x3"))
        result = self.run_test("ro", "1", "rpmh", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("cycle 1/1: evidence: rpmh sleep set: the last of 1 flushes before the sleep at 730.000000 (at "
                      "729.999000, 2 commands), sleep/wake: xo.lvl 0x0/0x3; left out, so held at their vote through the "
                      "sleep: cx.lvl, mx.lvl; active votes before the sleep 0, commands after it 0\n", self.out)
        self.assertIn("cycle 1/1: evidence: XO at the entry to the sleep (clock trace): XO free at the entry to the "
                      "sleep: bi_tcxo unprepared at 701.000002, so no clock of Linux votes XO in the sleep set (3 prepares "
                      "and unprepares traced before the sleep)\n", self.out)
        entry = [line for line in self.pmsg_lines() if ": evidence: XO at entry: " in line]
        self.assertEqual(len(entry), 1)
        self.assertTrue(entry[0].startswith("cycle 1/1: evidence: XO at entry: XO free; data="), entry)
        # A kernel without the clock tracepoints: said, and the run goes on.
        FakeTablet.setUp(self)
        SleepTestTest.setUp(self)
        self.rpmh_files()
        self.cycle(1)
        result = self.run_test("ro", "1", "rpmh", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("SLEEP-TEST RESULT: PASS: 1 of 1 cycles", self.out)
        self.assertIn("rpmh: no clk_prepare and clk_unprepare tracepoints to turn on", self.out)
        self.assertIn("cycle 1/1: evidence: XO at the entry to the sleep (clock trace): no clock trace (the "
                      "clk_prepare and clk_unprepare tracepoints were not on)\n", self.out)

    def test_rpmh_is_refused_without_the_tracepoint_tracing_or_the_kernel_log_s_clock_and_a_stop_turns_it_off(self):
        tr = "sys/kernel/tracing"
        cases = (
            ("rpmh needs the RPMh tracepoint (ROOT/sys/kernel/tracing/events/rpmh/rpmh_send_msg/enable): tracefs, "
             "and a kernel with CONFIG_FTRACE (the kit's kernels have both)", lambda: shutil.rmtree(
                 self.root / tr / "events")),
            ("rpmh: tracing is off (ROOT/sys/kernel/tracing/tracing_on reads 0), so the trace would stay empty: echo "
             "1 | sudo tee ROOT/sys/kernel/tracing/tracing_on, or run without rpmh", lambda: self.rpmh_files("0")),
            ("rpmh: the trace clock is global (ROOT/sys/kernel/tracing/trace_clock), not local, the kernel log's: "
             "echo local | sudo tee ROOT/sys/kernel/tracing/trace_clock, or run without rpmh",
             lambda: self.rpmh_files(clock="local [global] counter")))
        for message, edit in cases:
            with self.subTest(message):
                FakeTablet.setUp(self)
                SleepTestTest.setUp(self)
                self.rpmh_files()
                edit()
                result = self.run_test("ro", "1", "rpmh", "restart-ok")
                self.assertEqual(result.returncode, 1, self.out)
                self.assertIn("REFUSED: " + message.replace("ROOT/", f"{self.root}/"), self.out)
                self.assert_nothing_done()
        # A stop before any sleep: the tracepoint as the run found it.
        FakeTablet.setUp(self)
        SleepTestTest.setUp(self)
        self.rpmh_files()
        self.clk_events()
        (self.tmp / "freeze-fails").write_text("/media/stick\n")
        result = self.run_test("ro", "1", "rpmh", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("SLEEP-TEST RESULT: STOPPED before any sleep", self.out)
        self.assertEqual((self.root / tr / "events/rpmh/rpmh_send_msg/enable").read_text(), "0")
        for ev in ("clk_prepare", "clk_unprepare"):
            self.assertEqual((self.root / tr / "events/clk" / ev / "enable").read_text(), "0")
        self.assertEqual(self.sysrq(), "")

    def test_revision_11_s_evidence_names_xo_s_holders_genpd_s_power_offs_and_the_dsps_interrupts(self):
        """The prepared clocks below XO with no prepared clock below them,
        and XO's own consumers; what genpd powered off in the sleep (each
        domain's idle states' usage); the DSPs' interrupt lines; the
        subsystems over 5 s awake; every prepared clock once."""
        (self.root / "dev/pmsg0").write_text("")
        self.evidence_files()
        for d, usage in (("cx", 3), ("mx", 0), ("pcie_0_gdsc", 7), ("gpu_cc_cx_gdsc", 2)):
            (self.root / "sys/kernel/debug/pm_genpd" / d).mkdir(parents=True, exist_ok=True)
            (self.root / "sys/kernel/debug/pm_genpd" / d / "idle_states").write_text(
                "State  Time(ms)       Usage      Rejected   Above      Below      S2idle\n"
                f"S0     1200           {usage:<10} 0          0          0          0\n")
        (self.root / "proc/interrupts").write_text(INTERRUPTS + " 13:         40          0          0          0     "
                                                   "     0          0          0          0     GICv3 261 Level     "
                                                   "ipcc_0\n")
        self.evidence_cycle(1)
        d = self.tmp / "cycle-1"
        (d / "interrupts").write_text((d / "interrupts").read_text() + " 13:         90          0          0          0"
                                      "          0          0          0          0     GICv3 261 Level     ipcc_0\n")
        # The sleep powered cx and pcie_0_gdsc off once, mx not.
        for dom, usage in (("cx", 4), ("pcie_0_gdsc", 8)):
            (d / "genpd" / dom).mkdir(parents=True)
            (d / "genpd" / dom / "idle_states").write_text(
                "State  Time(ms)       Usage      Rejected   Above      Below      S2idle\n"
                f"S0     1300           {usage:<10} 0          0          0          0\n")
        result = self.run_test("ro", "1", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        held = "gcc_ufs_phy_axi_clk 522/522, gcc_ufs_phy_ice_core_clk 523/523, gcc_ufs_phy_unipro_core_clk " \
               "522/522, tcsr_ufs_pad_clkref_en 522/522, bi_tcxo itself 5"
        self.assertIn(f"evidence: XO (bi_tcxo) voted in the sleep set by these prepared clocks below it "
                      f"(enable/prepare): {held}\n", self.out)
        self.assertIn(f"cycle 1/1: evidence: before: XO held by: {held}\n", self.out)
        self.assertIn("cycle 1/1: evidence: after: XO held by: gcc_ufs_phy_axi_clk 523/523, gcc_ufs_phy_ice_core_clk "
                      "524/524, gcc_ufs_phy_unipro_core_clk 523/523, tcsr_ufs_pad_clkref_en 523/523, bi_tcxo itself "
                      "6\n", self.out)
        self.assertIn("evidence:   bi_tcxo 6 6 xo_board", self.out)
        self.assertIn("cycle 1/1: evidence: genpd powered off in the sleep (idle states usage of each domain): cx +1, "
                      "mx +0, pcie_0_gdsc +1\n", self.out)
        self.assertIn("cycle 1/1: evidence: dsp interrupts: 13 ipcc_0 +50\n", self.out)
        self.assertIn("evidence: awake, 5 s before the first sleep: aosd +0, cxsd +0, ddr +0; subsystems adsp +0, "
                      "apss +0", self.out)
        pmsg = [line for line in self.pmsg_lines() if ": evidence: " in line]
        self.assertEqual(pmsg[3], "cycle 1/1: evidence: wifi: pci 0000:00:00.0 - D0 5.0 GT/s PCIe, 0000:01:00.0 - D0 "
                                  "5.0 GT/s PCIe; no Wi-Fi failure line; ath11k firmware loads 0; genpd off: cx +1, "
                                  "mx +0, pcie_0_gdsc +1; dsp irq 13 ipcc_0 +50")
        self.assertTrue(pmsg[4].startswith("cycle 1/1: evidence: XO held by: gcc_ufs_phy_axi_clk 523/523"), pmsg[4])


# llvm-objdump --no-show-raw-insn of qmp_ufs_phy_calibrate in a vmlinux, as
# revision 7's shows it (abridged): the board gate is the call of
# of_machine_compatible_match.
CALIBRATE_R7 = """
vmlinux:\tfile format elf64-littleaarch64

Disassembly of section .text:

ffff800080964020 <qmp_ufs_phy_calibrate>:
ffff800080964020:      \tbti\tc
ffff800080964064:      \tbl\t0xffff800080cbcf30 <reset_control_assert>
ffff800080964068:      \tcbnz\tw0, 0xffff800080964280 <qmp_ufs_phy_calibrate+0x260>
ffff80008096408c:      \tbl\t0xffff800081578e48 <of_machine_compatible_match>
ffff8000809640d4:      \tbl\t0xffff8000809642cc <qmp_ufs_init_all>
ffff800080964170:      \tbl\t0xffff800080cbd1e4 <reset_control_deassert>
ffff8000809642bc:      \tbl\t0xffff80008002bdbc <_dev_err>
ffff8000809642c8:      \tbl\t0xffff800081b71520 <__stack_chk_fail>
"""
# Revision 8's, without the gate; a call into another function's middle
# names no function.
CALIBRATE_R8 = (CALIBRATE_R7.replace(
    "ffff80008096408c:      \tbl\t0xffff800081578e48 <of_machine_compatible_match>\n",
    "ffff80008096408c:      \tbl\t0xffff800080964400 <qmp_ufs_dbg_state>\n"
    "ffff800080964090:      \tbl\t0xffff800081578e40 <of_machine_compatible_match+0x8>\n"))


class UfsGateTest(unittest.TestCase):
    """make-kit.sh's checks for the UFS PHY changes of revisions 8 and 10:
    the three patches in the build tree's file, the tree comparison
    replaying the build on the hang-log tree's file, and the disassembly of
    the calibration in vmlinux, which tells the new code (no board check)
    from the old."""

    text = (KIT_SRC / "make-kit.sh").read_text()

    def gate(self, vmlinux, new_id, hang=None):
        """Section 5b on stand-in vmlinux files: an "id=" line (what
        llvm-objcopy gives as .notes) and the disassembly (what llvm-objdump
        gives)."""
        build_id = re.search(r"^build_id\(\) \{.*?^\}\n", self.text, re.M | re.S).group(0)
        section = re.search(r"^notes_build_id\(\) \{.*?^echo \"UFS PHY: \$gate_check\"\n", self.text,
                            re.M | re.S).group(0)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            bin_ = tmp / "bin"
            bin_.mkdir()
            (bin_ / "llvm-objcopy").write_text(
                '#!/bin/sh\nfor last; do :; done\nid=$(sed -n "s/^id=//p" "$4")\n'
                'printf "040000001400000003000000474e5500%s" "$id" | xxd -r -p > "$last"\n')
            (bin_ / "llvm-objdump").write_text('#!/bin/sh\nfor last; do :; done\nsed "/^id=/d" "$last"\n')
            for stub in bin_.iterdir():
                stub.chmod(0o755)
            (tmp / "build").mkdir()
            (tmp / "build/vmlinux").write_text(f"id={vmlinux[0]}\n{vmlinux[1]}")
            env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}")
            prefix = (f'set -euo pipefail; export LC_ALL=C\ndie() {{ echo "$*" >&2; exit 1; }}\n'
                      f'build={tmp}/build tmp={tmp} new_id={new_id} hang_build_id={HANG_BUILD_ID}\n')
            if hang:
                (tmp / "hang/linux-gts9wifi").mkdir(parents=True)
                (tmp / "hang/linux-gts9wifi/vmlinux").write_text(f"id={hang[0]}\n{hang[1]}")
                prefix += f"HANG_SCRATCH={tmp}/hang\n"
            result = subprocess.run(["bash", "-c", prefix + build_id + section], capture_output=True,
                                    text=True, env=env)
            return result.returncode, result.stdout + result.stderr

    def test_the_new_calibration_passes_and_the_old_one_does_not(self):
        new = "1" * 40
        self.assertEqual(self.gate((new, CALIBRATE_R8), new),
                         (0, f"UFS PHY: qmp_ufs_phy_calibrate() in vmlinux (build ID {new[:12]}) calls no "
                             "of_machine_compatible_match()\n"))
        self.assertEqual(self.gate((new, CALIBRATE_R8), new, hang=(HANG_BUILD_ID, CALIBRATE_R7)),
                         (0, f"UFS PHY: qmp_ufs_phy_calibrate() in vmlinux (build ID {new[:12]}) calls no "
                             "of_machine_compatible_match(); the hang-log build's does\n"))
        for name, args, message in (
                ("the old calibration", ((new, CALIBRATE_R7), new),
                 "still asks of_machine_compatible_match(): the board gate"),
                ("no such function", ((new, "Disassembly of section .text:\n"), new),
                 "no qmp_ufs_phy_calibrate() calling reset_control_assert()"),
                ("another build's vmlinux", (("2" * 40, CALIBRATE_R8), new),
                 "is not the ELF of the new image (another build ID)"),
                ("a hang-log vmlinux without the gate", ((new, CALIBRATE_R8), new, (HANG_BUILD_ID, CALIBRATE_R8)),
                 "self-test failed: the hang-log calibration does not show the board gate either"),
                ("another hang-log vmlinux", ((new, CALIBRATE_R8), new, ("3" * 40, CALIBRATE_R7)),
                 "is not the hang-log kernel's")):
            with self.subTest(name):
                code, out = self.gate(*args)
                self.assertEqual(code, 1, out)
                self.assertIn(message, out)

    def flows(self, tmp):
        """The UFS PHY driver as the build leaves it: before revision 8
        (SERDES, then the SM-X910-only reset), revisions 8 and 9's (SERDES,
        the stage script, the diagnostic), revision 10's (and the second
        diagnostic), and three that lack one of revision 10's three."""
        source = subprocess.run(["git", "-C", str(kernel_source()), "show",
                                 f"HEAD:{QMP}.c"], check=True, capture_output=True, text=True).stdout
        legacy = re.search(r"<<'LEGACY_PCS_RESET'\n(.*?\n)LEGACY_PCS_RESET\n",
                           (REPO / "scripts/stage-ufs-resume-experiment.sh").read_text(), re.S).group(1)
        trees = {}
        for name in ("old", "r9", "new", "pcs-only", "diagnostic-only", "no-pcs", "no-diagnostic"):
            root = tmp / name
            (root / QMP).parent.mkdir(parents=True)
            (root / f"{QMP}.c").write_text(source)
            subprocess.run(["patch", "-s", "-d", str(root), "-p1"],
                           input=(REPO / "kernel/patches/qmp-ufs-reset-serdes-before-power-down-gts9u.patch")
                           .read_text(), check=True, text=True)
            if name == "old":
                subprocess.run(["patch", "-s", "--fuzz=0", "-d", str(root), "-p1"], input=legacy,
                               check=True, text=True)
            if name in ("r9", "new", "pcs-only", "no-diagnostic"):
                subprocess.run(["bash", str(REPO / "scripts/stage-ufs-resume-experiment.sh"), str(root)],
                               check=True, capture_output=True)
            if name in ("r9", "new", "diagnostic-only", "no-pcs"):
                subprocess.run(["patch", "-s", "--fuzz=0", "-d", str(root), "-p1"],
                               input=(REPO / "kernel/patches" / UFS_PATCHES[1]).read_text(), check=True,
                               text=True)
            if name in ("new", "no-pcs", "no-diagnostic"):
                subprocess.run(["patch", "-s", "--fuzz=0", "--no-backup-if-mismatch", "-d", str(root), "-p1"],
                               input=(REPO / "kernel/patches" / UFS_PATCHES[2]).read_text(), check=True,
                               text=True)
            trees[name] = root
        return trees

    @unittest.skipUnless(kernel_source(), "no pinned kernel checkout")
    def test_the_build_tree_has_to_carry_both_patches_whole(self):
        start = self.text.index('mkdir -p "$tmp/ufs/${qmp%/*}"')
        end = self.text.index("still carries the SM-X910-only PCS reset\"\n") + len(
            "still carries the SM-X910-only PCS reset\"\n")
        check = self.text[start:end]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            trees = self.flows(tmp)
            for name, message in (("new", None),
                                  ("r9", f"does not carry {UFS_PATCHES[2]} whole"),
                                  ("no-diagnostic", f"does not carry {UFS_PATCHES[1]} whole"),
                                  ("no-pcs", f"does not carry {UFS_PATCHES[0]} whole"),
                                  ("pcs-only", f"does not carry {UFS_PATCHES[2]} whole"),
                                  ("diagnostic-only", f"does not carry {UFS_PATCHES[2]} whole"),
                                  ("old", f"does not carry {UFS_PATCHES[2]} whole")):
                with self.subTest(name):
                    work = tmp / f"work-{name}"
                    work.mkdir()
                    result = subprocess.run(
                        ["bash", "-c", 'set -euo pipefail\ndie() { echo "$*" >&2; exit 1; }\n'
                         f'tree={trees[name]} tmp={work} repo={REPO} qmp={QMP}\n'
                         f'ufs_patches=({" ".join(UFS_PATCHES)})\n' + check],
                        capture_output=True, text=True)
                    if message is None:
                        self.assertEqual((result.returncode, result.stderr), (0, ""))
                    else:
                        self.assertEqual(result.returncode, 1, result.stderr)
                        self.assertIn(message, result.stderr)

    @unittest.skipUnless(kernel_source(), "no pinned kernel checkout")
    def test_the_tree_comparison_replays_the_build_on_the_hang_log_tree_s_file(self):
        start = self.text.index("\t# Like every build before revision 8, the hang-log build gave the PCS")
        typec = self.text.index('\tfor p in "${typec_patches[@]}"; do', start)
        ufs = self.text.index("\t# The UFS PHY as the build changes it:", typec)
        end = self.text.index("does not apply to the hang-log tree's $qmp.c\"\n\tdone\n", ufs) + len(
            "does not apply to the hang-log tree's $qmp.c\"\n\tdone\n")
        replay = self.text[start:typec] + self.text[ufs:end]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            trees = self.flows(tmp)
            for name, hang, message in (("the hang-log kit's tree", "old", None),
                                        ("a tree with the new reset", "new", "is not the one with the "
                                         "SM-X910-only PCS reset")):
                with self.subTest(name):
                    work = tmp / f"replay-{hang}"
                    (work / "src" / QMP).parent.mkdir(parents=True)
                    shutil.copy(trees[hang] / f"{QMP}.c", work / "src" / f"{QMP}.c")
                    result = subprocess.run(
                        ["bash", "-c", 'set -euo pipefail\ndie() { echo "$*" >&2; exit 1; }\n'
                         f'hang_tree={trees[hang]} tmp={work} repo={REPO} qmp={QMP}\n' + replay],
                        capture_output=True, text=True)
                    if message is None:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual((work / "src" / f"{QMP}.c").read_text(),
                                         (trees["new"] / f"{QMP}.c").read_text())
                    else:
                        self.assertEqual(result.returncode, 1, result.stderr)
                        self.assertIn(message, result.stderr)


class DeepSleepDebugSwitchTest(unittest.TestCase):
    """Revision 9's debug switches: the build script's switch,
    APPLY_DEEP_SLEEP_DEBUG_SWITCHES, run from its own text (off by default,
    0 or 1 only, each patch applied strictly and once, a tree that carries
    either refused for a default build), and the two patches against the
    pinned kernel checkout (only read): the GDSC table as it was, the
    handler's four writes, and rpmhpd's votes with the floor off and on, the
    aggregation compiled from the patched and the pinned rpmhpd.c."""

    BUILD = REPO / "scripts/build-mainline-kernel.sh"
    MARKER = "SM8550 deep-sleep debug, not for release builds"
    GDSC = DEBUG_PATCHES[0]
    FLOOR = DEBUG_PATCHES[1]

    def block(self):
        """The build script's lines from deep_sleep_debug_marker= to the
        loop's done."""
        lines = self.BUILD.read_text().splitlines(True)
        first = next(i for i, line in enumerate(lines) if line.startswith("deep_sleep_debug_marker="))
        last = next(i for i in range(first, len(lines)) if lines[i] == "done\n")
        return "".join(lines[first:last + 1])

    def run_block(self, tree, value):
        return subprocess.run(["bash", "-c", "set -euo pipefail\nkernel_tree=$1 pat=$2 apply_deep_sleep_debug=$3\n"
                               + self.block(), "build", str(tree), str(REPO / "kernel/patches"), value],
                              capture_output=True, text=True)

    def test_the_switch_is_off_by_default_and_0_or_1(self):
        text = self.BUILD.read_text()
        self.assertIn("\napply_deep_sleep_debug=${APPLY_DEEP_SLEEP_DEBUG_SWITCHES:-0}\n", text)
        validate = re.search(r"^validate_bool\(\) \{.*?^\}\n", text, re.M | re.S).group(0)
        check = ("apply_deep_sleep_debug=${APPLY_DEEP_SLEEP_DEBUG_SWITCHES:-0}\n" + validate +
                 'validate_bool APPLY_DEEP_SLEEP_DEBUG_SWITCHES "$apply_deep_sleep_debug"\n')
        self.assertIn('\nvalidate_bool APPLY_DEEP_SLEEP_DEBUG_SWITCHES "$apply_deep_sleep_debug"\n', text)
        for value, ok in (("", True), ("0", True), ("1", True), ("yes", False), ("2", False), ("01", False)):
            with self.subTest(value=value):
                env = {k: v for k, v in os.environ.items() if k != "APPLY_DEEP_SLEEP_DEBUG_SWITCHES"}
                if value:
                    env["APPLY_DEEP_SLEEP_DEBUG_SWITCHES"] = value
                result = subprocess.run(["bash", "-c", "set -euo pipefail\n" + check], capture_output=True,
                                        text=True, env=env)
                self.assertEqual(result.returncode == 0, ok, result.stderr)
                if not ok:
                    self.assertIn("APPLY_DEEP_SLEEP_DEBUG_SWITCHES must be 0 or 1", result.stderr)

    def test_the_switch_covers_both_patches_after_the_usb_gdsc_patch_on_both_boards(self):
        """The GDSC patch goes on top of the USB GDSC patch; nothing of the
        block depends on the board; the two files are changed by no other
        patch, so this order is the whole of what the build does to them."""
        text = self.BUILD.read_text()
        usb = text.index("apply_unless 'Retention across system suspend, as on sm8650' \\\n"
                         "\tdrivers/clk/qcom/gcc-sm8550.c gcc-sm8550-usb-gdscs-retention.patch\n")
        block = self.block()
        self.assertLess(usb, text.index(block))
        self.assertNotIn("DEVICE", block)
        self.assertIn(f"drivers/clk/qcom/gcc-sm8550.c:{self.GDSC}", block)
        self.assertIn(f"drivers/pmdomain/qcom/rpmhpd.c:{self.FLOOR}", block)
        self.assertIn(f"{QCOM_STATS}.c:{CXPC_PATCH}", block)
        self.assertEqual(text.count(f"deep_sleep_debug_marker='{self.MARKER}'"), 1)
        for patch in (self.GDSC, self.FLOOR):
            with self.subTest(patch):
                added = "\n".join(added_lines(patch).popitem()[1])
                self.assertIn(self.MARKER, added)
                self.assertNotIn("of_machine_is_compatible", added)
        # The CXPC log's: an SM8550 at run time, both boards built alike.
        added = "\n".join(added_lines(CXPC_PATCH).popitem()[1])
        self.assertIn(self.MARKER, added)
        self.assertIn('of_machine_is_compatible("qcom,sm8550")', added)
        touching = {}
        for patch in (REPO / "kernel/patches").glob("*.patch"):
            for target in re.findall(r"^\+\+\+ b/(\S+)", patch.read_text(), re.M):
                touching.setdefault(target, []).append(patch.name)
        self.assertEqual(sorted(touching[f"{GCC}.c"]), sorted(["gcc-sm8550-usb-gdscs-retention.patch", self.GDSC,
                                                              GDSC_PATCHES[0]]))
        self.assertEqual(touching[f"{RPMHPD}.c"], [self.FLOOR])
        self.assertEqual(touching[f"{QCOM_STATS}.c"], [CXPC_PATCH])

    def pinned_files(self, tree, usb=True):
        """The three files as the build has them when the block runs: the
        pinned gcc-sm8550.c with the USB GDSC patch, the pinned rpmhpd.c and
        qcom_stats.c."""
        for path in (f"{GCC}.c", f"{RPMHPD}.c", f"{QCOM_STATS}.c"):
            (tree / path).parent.mkdir(parents=True, exist_ok=True)
            (tree / path).write_text(subprocess.run(["git", "-C", str(kernel_source()), "show", "HEAD:" + path],
                                                    check=True, capture_output=True, text=True).stdout)
        if usb:
            subprocess.run(["patch", "-s", "-d", str(tree), "-p1", "--no-backup-if-mismatch"],
                           input=(REPO / "kernel/patches/gcc-sm8550-usb-gdscs-retention.patch").read_text(),
                           check=True, text=True)

    @unittest.skipUnless(kernel_source(), "no pinned kernel checkout")
    def test_the_switch_applies_both_once_and_a_default_build_refuses_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = pathlib.Path(tmp)
            self.pinned_files(tree)
            files = {p: (tree / p).read_text() for p in (f"{GCC}.c", f"{RPMHPD}.c", f"{QCOM_STATS}.c")}
            listing = sorted(p.name for p in (tree / GCC).parent.iterdir())
            result = self.run_block(tree, "0")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual({p: (tree / p).read_text() for p in files}, files)
            result = self.run_block(tree, "1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotRegex(result.stdout, "offset|fuzz")
            patched = {p: (tree / p).read_text() for p in files}
            for p in files:
                self.assertEqual(patched[p].count(self.MARKER), 1, p)
            # No backup next to them.
            self.assertEqual(sorted(p.name for p in (tree / GCC).parent.iterdir()), listing)
            self.assertEqual(sorted(p.name for p in (tree / RPMHPD).parent.iterdir()), ["rpmhpd.c"])
            self.assertEqual(sorted(p.name for p in (tree / QCOM_STATS).parent.iterdir()), ["qcom_stats.c"])
            # Again: a reused tree, nothing changes.
            result = self.run_block(tree, "1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual({p: (tree / p).read_text() for p in files}, patched)
            # A default build in that tree: refused, the tree as it was.
            result = self.run_block(tree, "0")
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("The deep-sleep debug switches remain in this source tree", result.stderr)
            self.assertEqual({p: (tree / p).read_text() for p in files}, patched)
            # A copy with the marker but not the whole patch is refused, and
            # so is the marker alone in a file without the patch.
            for p, damaged in ((f"{GCC}.c", patched[f"{GCC}.c"].replace("PWRSTS_OFF_ON : PWRSTS_RET_ON",
                                                                          "PWRSTS_OFF_ON : PWRSTS_ON")),
                               (f"{RPMHPD}.c", files[f"{RPMHPD}.c"] + f"/* {self.MARKER} */\n")):
                with self.subTest(p):
                    self.assertNotEqual(damaged, patched[p])
                    (tree / p).write_text(damaged)
                    result = self.run_block(tree, "1")
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual((tree / p).read_text(), damaged)
                    (tree / p).write_text(patched[p])
            # Without the USB GDSC patch under it the GDSC patch is refused:
            # its handler gives back PWRSTS_RET_ON, that patch's state.
            pathlib.Path(tmp, "bare").mkdir()
            self.pinned_files(pathlib.Path(tmp, "bare"), usb=False)
            bare = (pathlib.Path(tmp, "bare") / f"{GCC}.c").read_text()
            result = self.run_block(pathlib.Path(tmp, "bare"), "1")
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(f"{self.GDSC} goes on top of gcc-sm8550-usb-gdscs-retention.patch, which this tree does "
                          "not carry", result.stderr)
            self.assertEqual((pathlib.Path(tmp, "bare") / f"{GCC}.c").read_text(), bare)

    @unittest.skipUnless(kernel_source(), "no pinned kernel checkout")
    def test_the_gdsc_table_is_as_it_was_and_each_switch_writes_its_own_domains(self):
        """The patch adds code after module_exit() and changes no line of the
        table: the four GDSCs keep the states the patches under it gave
        them.  Each switch has a const descriptor of its GDSCs; the handler
        refuses a value before SYSTEM_RUNNING (the command line) and writes
        .pwrsts of its own switch's GDSCs and nothing else; a switch reads
        its first GDSC's state; debug_boot_state is read-only."""
        with tempfile.TemporaryDirectory() as tmp:
            tree = pathlib.Path(tmp)
            self.pinned_files(tree)
            before = (tree / f"{GCC}.c").read_text()
            subprocess.run(["patch", "-s", "-d", str(tree), "-p1", "--fuzz=0", "--no-backup-if-mismatch"],
                           input=(REPO / "kernel/patches" / self.GDSC).read_text(), check=True, text=True)
            after = (tree / f"{GCC}.c").read_text()
        exit_at = after.index("module_exit(gcc_sm8550_exit);\n") + len("module_exit(gcc_sm8550_exit);\n")
        # Everything before module_exit() is as it was; the rest is new.
        self.assertTrue(before.startswith(after[:exit_at]))
        self.assertEqual(before[exit_at:], '\nMODULE_DESCRIPTION("QTI GCC SM8550 Driver");\nMODULE_LICENSE("GPL");\n')
        for name in ("pcie_0_gdsc", "pcie_0_phy_gdsc", "usb30_prim_gdsc", "usb3_phy_gdsc"):
            body = re.search(rf"^static struct gdsc {name} = {{\n(.*?)^}};", after, re.M | re.S).group(1)
            self.assertIn("\t.pwrsts = PWRSTS_RET_ON,\n", body, name)
        new = after[exit_at:]
        self.assertEqual(re.findall(r"^static const struct gcc_sm8550_debug_switch (\w+) = \{\n\t\.name = \"(\w+)\",\n"
                                    r"\t\.gdscs = \{ (.*?) \},\n\};", new, re.M),
                         [("gcc_sm8550_debug_pcie0", "pcie0", "&pcie_0_gdsc"),
                          ("gcc_sm8550_debug_pcie0_phy", "pcie0phy", "&pcie_0_phy_gdsc"),
                          ("gcc_sm8550_debug_usb", "usb", "&usb30_prim_gdsc, &usb3_phy_gdsc")])
        handler = re.search(r"^static int gcc_sm8550_debug_set\(.*?^\}\n", new, re.M | re.S).group(0)
        self.assertIn("\tif (system_state != SYSTEM_RUNNING)\n\t\treturn -EPERM;\n", handler)
        self.assertLess(handler.index("return -EPERM;"), handler.index("kstrtobool(val, &off)"))
        self.assertIn("\tfor (i = 0; i < ARRAY_SIZE(sw->gdscs) && sw->gdscs[i]; i++)\n"
                      "\t\tWRITE_ONCE(*(u8 *)&sw->gdscs[i]->pwrsts, off ? PWRSTS_OFF_ON : PWRSTS_RET_ON);\n", handler)
        # That is the one write: no assignment to a .pwrsts anywhere else.
        self.assertEqual(new.count("->pwrsts"), 2)
        self.assertEqual(new.count("WRITE_ONCE("), 1)
        self.assertIsNone(re.search(r"pwrsts\s*=(?!=)", new))
        self.assertIn("\treturn READ_ONCE(sc->pwrsts) == PWRSTS_OFF_ON;\n", new)
        params = [" ".join(m.split()) for m in re.findall(r"^module_param_cb\((.*?)\);", new, re.M | re.S)]
        self.assertEqual(params, [
            "debug_pcie0_off, &gcc_sm8550_debug_ops, (void *)&gcc_sm8550_debug_pcie0, 0644",
            "debug_pcie0_phy_off, &gcc_sm8550_debug_ops, (void *)&gcc_sm8550_debug_pcie0_phy, 0644",
            "debug_usb_off, &gcc_sm8550_debug_ops, (void *)&gcc_sm8550_debug_usb, 0644",
            "debug_boot_state, &gcc_sm8550_debug_boot_ops, NULL, 0444"])
        boot_set = re.search(r"^static int gcc_sm8550_debug_boot_set\(.*?^\}\n", new, re.M | re.S).group(0)
        self.assertEqual(boot_set.count("\n"), 4)
        self.assertIn("\treturn -EPERM;\n", boot_set)
        # A plain parameter: lockdown refuses only module_param_hw() ones,
        # which the comment names and the code does not use.
        self.assertFalse([line for line in new.splitlines()
                          if "module_param_hw" in line and not line.lstrip().startswith("*")])

    def switch_program(self, source):
        """A program that runs the debug switches' code of SOURCE (the
        patched gcc-sm8550.c, from its comment after module_exit() to the
        end) against four stand-in GDSCs with their compiled power states,
        the kernel's macros as small C ones, and prints what each step
        reads and writes."""
        code = source[source.index("/*\n * SM8550 deep-sleep debug, not for release builds"):]
        code = code[:code.index("MODULE_DESCRIPTION(\"QTI GCC SM8550 Driver\");")]
        table = {name: re.search(rf"^static struct gdsc {name} = {{\n.*?\t\.pwrsts = (\w+),", source,
                                 re.M | re.S).group(1)
                 for name in ("pcie_0_gdsc", "pcie_0_phy_gdsc", "usb30_prim_gdsc", "usb3_phy_gdsc")}
        gdscs = "".join(f"static struct gdsc {name} = {{ .pwrsts = {state} }};\n" for name, state in table.items())
        return r'''
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
typedef unsigned char u8;
#define BIT(n) (1u << (n))
#define PWRSTS_OFF BIT(0)
#define PWRSTS_RET BIT(1)
#define PWRSTS_ON BIT(2)
#define PWRSTS_OFF_ON (PWRSTS_OFF | PWRSTS_ON)
#define PWRSTS_RET_ON (PWRSTS_RET | PWRSTS_ON)
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define READ_ONCE(x) (*(const volatile __typeof__(x) *)&(x))
#define WRITE_ONCE(x, v) (*(volatile __typeof__(x) *)&(x) = (v))
#define pr_info(...) printf("pr_info: " __VA_ARGS__)
enum { SYSTEM_BOOTING, SYSTEM_RUNNING };
static int system_state;
struct gdsc { const u8 pwrsts; };
struct kernel_param;
struct kernel_param_ops {
	int (*set)(const char *val, const struct kernel_param *kp);
	int (*get)(char *buffer, const struct kernel_param *kp);
};
struct kernel_param { const char *name; const struct kernel_param_ops *ops; void *arg; };
#define module_param_cb(n, o, a, perm) static const struct kernel_param __param_##n = { #n, o, a }
#define MODULE_PARM_DESC(n, d)
static int kstrtobool(const char *s, bool *res)
{
	switch (s[0]) {
	case 'y': case 'Y': case '1': *res = true; return 0;
	case 'n': case 'N': case '0': *res = false; return 0;
	}
	return -EINVAL;
}
''' + gdscs + code + r'''
static void show(const char *when)
{
	char b[4096];
	const struct kernel_param *all[] = { &__param_debug_pcie0_off, &__param_debug_pcie0_phy_off,
					     &__param_debug_usb_off, &__param_debug_boot_state };
	printf("%s:", when);
	for (unsigned i = 0; i < 4; i++) {
		all[i]->ops->get(b, all[i]);
		b[strcspn(b, "\n")] = 0;
		printf(" %s=%s", all[i]->name, b);
	}
	printf(" table=%u,%u,%u,%u\n", pcie_0_gdsc.pwrsts, pcie_0_phy_gdsc.pwrsts, usb30_prim_gdsc.pwrsts,
	       usb3_phy_gdsc.pwrsts);
}
static void set(const struct kernel_param *kp, const char *v)
{
	printf("set %s=%s: %d\n", kp->name, v, kp->ops->set(v, kp));
}
int main(void)
{
	show("boot");
	set(&__param_debug_pcie0_off, "Y");
	set(&__param_debug_boot_state, "Y");
	system_state = SYSTEM_RUNNING;
	set(&__param_debug_usb_off, "x");
	show("running");
	set(&__param_debug_usb_off, "Y");
	show("usb");
	set(&__param_debug_pcie0_off, "N");
	show("pcie0 N");
	set(&__param_debug_pcie0_off, "Y");
	set(&__param_debug_usb_off, "N");
	show("back");
	return 0;
}
'''

    def run_switches(self, pcie0_off):
        """The debug switches' code compiled and run on the pinned file with
        the USB GDSC patch, and with revision 11's pcie_0_gdsc if PCIE0_OFF."""
        with tempfile.TemporaryDirectory() as tmp:
            tree = pathlib.Path(tmp)
            self.pinned_files(tree)
            for patch in ([GDSC_PATCHES[0]] if pcie0_off else []) + [self.GDSC]:
                subprocess.run(["patch", "-s", "-d", str(tree), "-p1", "--fuzz=0", "--no-backup-if-mismatch"],
                               input=(REPO / "kernel/patches" / patch).read_text(), check=True, text=True)
            c = tree / "switches.c"
            c.write_text(self.switch_program((tree / f"{GCC}.c").read_text()))
            subprocess.run(["cc", "-std=gnu11", "-Wall", "-Werror", "-Wno-unused-function", "-Wno-unused-variable",
                            "-o", str(tree / "switches"), str(c)], check=True, capture_output=True, text=True)
            return subprocess.run([str(tree / "switches")], check=True, capture_output=True, text=True).stdout

    @unittest.skipUnless(kernel_source() and shutil.which("cc"), "no pinned kernel checkout or no C compiler")
    def test_the_switches_read_the_table_write_their_own_domains_and_keep_the_boot_state(self):
        """The patch's code compiled with stand-ins for the kernel's macros:
        without revision 11's patch every switch boots N, with it pcie0 Y; a
        write before SYSTEM_RUNNING is refused and changes nothing, a bad
        value too, debug_boot_state takes none; a write changes its own
        GDSCs' state alone (5 is PWRSTS_OFF_ON, 6 PWRSTS_RET_ON), and
        debug_boot_state goes on saying how the kernel booted."""
        r10 = ("boot: debug_pcie0_off=N debug_pcie0_phy_off=N debug_usb_off=N "
               "debug_boot_state=pcie0 N, pcie0phy N, usb N table=6,6,6,6\n"
               "set debug_pcie0_off=Y: -1\nset debug_boot_state=Y: -1\nset debug_usb_off=x: -22\n"
               "running: debug_pcie0_off=N debug_pcie0_phy_off=N debug_usb_off=N "
               "debug_boot_state=pcie0 N, pcie0phy N, usb N table=6,6,6,6\n"
               "pr_info: gcc-sm8550: debug switches: pcie_0_gdsc RET_ON, pcie_0_phy_gdsc RET_ON, usb30_prim_gdsc and "
               "usb3_phy_gdsc OFF_ON, from the next power-off\nset debug_usb_off=Y: 0\n"
               "usb: debug_pcie0_off=N debug_pcie0_phy_off=N debug_usb_off=Y "
               "debug_boot_state=pcie0 N, pcie0phy N, usb N table=6,6,5,5\n")
        out = self.run_switches(False)
        self.assertTrue(out.startswith(r10), out)
        out = self.run_switches(True)
        self.assertEqual(out.splitlines()[0], "boot: debug_pcie0_off=Y debug_pcie0_phy_off=N debug_usb_off=N "
                                              "debug_boot_state=pcie0 Y, pcie0phy N, usb N table=5,6,6,6")
        for line in ("set debug_pcie0_off=Y: -1", "set debug_boot_state=Y: -1", "set debug_usb_off=x: -22",
                     "running: debug_pcie0_off=Y debug_pcie0_phy_off=N debug_usb_off=N "
                     "debug_boot_state=pcie0 Y, pcie0phy N, usb N table=5,6,6,6",
                     "usb: debug_pcie0_off=Y debug_pcie0_phy_off=N debug_usb_off=Y "
                     "debug_boot_state=pcie0 Y, pcie0phy N, usb N table=5,6,5,5",
                     "pcie0 N: debug_pcie0_off=N debug_pcie0_phy_off=N debug_usb_off=Y "
                     "debug_boot_state=pcie0 Y, pcie0phy N, usb N table=6,6,5,5",
                     "back: debug_pcie0_off=Y debug_pcie0_phy_off=N debug_usb_off=N "
                     "debug_boot_state=pcie0 Y, pcie0phy N, usb N table=5,6,6,6"):
            self.assertIn(line + "\n", out)

    def aggregate_program(self, source):
        """A program that runs rpmhpd_aggregate_corner() of SOURCE, with its
        struct rpmhpd and helpers, against a recording rpmhpd_send_corner():
        cx/cx_ao and mx/mx_ao as SM8550 has them (cmd-db's 0, 16, 64, ...:
        RETENTION at hlvl 1), and gfx for a resource without the floor."""
        struct = re.search(r"^struct rpmhpd \{\n.*?^\};\n", source, re.M | re.S).group(0)
        functions = [re.search(rf"^static [^\n(]*\b{name}\(.*?^\}}\n", source, re.M | re.S)
                     for name in ("rpmhpd_debug_floored", "rpmhpd_debug_floor", "to_active_sleep",
                                  "rpmhpd_aggregate_corner")]
        body = "".join(match.group(0) + "\n" for match in functions if match)
        floor = "static bool debug_ret_floor __attribute__((unused));\n"
        return r'''
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
typedef unsigned int u32;
struct device;
struct generic_pm_domain { int unused; };
#define RPMH_ARC_MAX_LEVELS 32
enum { RPMH_SLEEP_STATE, RPMH_WAKE_ONLY_STATE, RPMH_ACTIVE_ONLY_STATE };
#define max(a, b) ((a) > (b) ? (a) : (b))
''' + struct + floor + r'''
static int rpmhpd_send_corner(struct rpmhpd *pd, int state, unsigned int corner, bool sync)
{
	printf(" %s:%c%u", pd->res_name, "SWA"[state], corner);
	return 0;
}
''' + body + r'''
static struct rpmhpd cx_ao, mx_ao, gfx;
static struct rpmhpd cx = { .peer = &cx_ao, .res_name = "cx.lvl", .enable_corner = 1, .level_count = 10 };
static struct rpmhpd cx_ao = { .active_only = true, .peer = &cx, .res_name = "cx.lvl", .enable_corner = 1,
			       .level_count = 10 };
static struct rpmhpd mx = { .peer = &mx_ao, .res_name = "mx.lvl", .enable_corner = 1, .level_count = 10 };
static struct rpmhpd mx_ao = { .active_only = true, .peer = &mx, .res_name = "mx.lvl", .enable_corner = 1,
			       .level_count = 10 };
static struct rpmhpd gfx = { .res_name = "gfx.lvl", .enable_corner = 1, .level_count = 14 };

int main(int argc, char **argv)
{
	struct rpmhpd *all[] = { &cx, &cx_ao, &mx, &mx_ao, &gfx };
	unsigned int corners[] = { 0, 1, 3 };
	debug_ret_floor = argc > 1 && argv[1][0] == '1';
	for (int synced = 0; synced < 2; synced++)
		for (int p = 0; p < 5; p++)
			for (int peer_on = 0; peer_on < 2; peer_on++)
				for (int c = 0; c < 3; c++) {
					struct rpmhpd *pd = all[p];
					for (int i = 0; i < 5; i++) {
						all[i]->state_synced = synced;
						all[i]->enabled = false;
						all[i]->corner = 0;
						all[i]->active_corner = 0;
					}
					if (pd->peer) {
						pd->peer->enabled = peer_on;
						pd->peer->corner = 2;
					}
					printf("synced %d %s%s peer %d corner %u:", synced, pd->res_name,
					       pd->active_only ? "(ao)" : "", peer_on, corners[c]);
					rpmhpd_aggregate_corner(pd, corners[c]);
					printf("\n");
				}
	return 0;
}
'''

    def run_aggregate(self, source, floor):
        with tempfile.TemporaryDirectory() as tmp:
            c = pathlib.Path(tmp, "aggregate.c")
            c.write_text(self.aggregate_program(source))
            subprocess.run(["cc", "-std=gnu11", "-Wall", "-Wno-unused-function", "-o", str(c.with_suffix("")),
                            str(c)], check=True, capture_output=True, text=True)
            return subprocess.run([str(c.with_suffix("")), "1" if floor else "0"], check=True,
                                  capture_output=True, text=True).stdout

    @unittest.skipUnless(kernel_source() and shutil.which("cc"), "no pinned kernel checkout or no C compiler")
    def test_the_floor_changes_no_vote_when_off_and_floors_cx_and_mx_in_every_set_when_on(self):
        """rpmhpd_aggregate_corner() of the patched rpmhpd.c, compiled with a
        recording rpmhpd_send_corner(): with the switch off every vote, in
        every case tried (synced or not, each domain, its peer on or off,
        corners 0, 1 and 3), is the pinned file's; with it on, cx.lvl and
        mx.lvl never get less than RETENTION (1) in the active (A), wake (W)
        and sleep (S) sets, gfx.lvl is as before, and an unsynced resource
        still gets the clamp to its highest level."""
        with tempfile.TemporaryDirectory() as tmp:
            tree = pathlib.Path(tmp)
            self.pinned_files(tree, usb=False)
            pinned = (tree / f"{RPMHPD}.c").read_text()
            subprocess.run(["patch", "-s", "-d", str(tree), "-p1", "--fuzz=0", "--no-backup-if-mismatch"],
                           input=(REPO / "kernel/patches" / self.FLOOR).read_text(), check=True, text=True)
            patched = (tree / f"{RPMHPD}.c").read_text()
        before = self.run_aggregate(pinned, False)
        off = self.run_aggregate(patched, False)
        on = self.run_aggregate(patched, True)
        self.assertEqual(off, before)
        self.assertEqual(len(on.splitlines()), 2 * 5 * 2 * 3)
        # The cases the switch is for: CX powered off at the end of a
        # suspend, and MX, which no consumer powers on.
        self.assertIn("synced 1 cx.lvl peer 0 corner 0: cx.lvl:A0 cx.lvl:W0 cx.lvl:S0\n", before)
        self.assertIn("synced 1 cx.lvl peer 0 corner 0: cx.lvl:A1 cx.lvl:W1 cx.lvl:S1\n", on)
        self.assertIn("synced 1 mx.lvl peer 0 corner 0: mx.lvl:A1 mx.lvl:W1 mx.lvl:S1\n", on)
        # An active-only peer's own votes, its partner off: floored as well.
        self.assertIn("synced 1 mx.lvl(ao) peer 0 corner 0: mx.lvl:A1 mx.lvl:W1 mx.lvl:S1\n", on)
        for b, o in zip(before.splitlines(), on.splitlines()):
            with self.subTest(b):
                head, votes = b.split(":", 1)
                if "gfx.lvl" in head or "synced 0" in head:
                    self.assertEqual(o, b)
                    continue
                # The same sends, each vote raised to 1 at most.
                old = [v.rsplit(":", 1) for v in votes.split()]
                new = [v.rsplit(":", 1) for v in o.split(":", 1)[1].split()]
                self.assertEqual([(r, s[0]) for r, s in old], [(r, s[0]) for r, s in new])
                self.assertEqual([max(int(s[1:]), 1) for _, s in old], [int(s[1:]) for _, s in new])

    @unittest.skipUnless(kernel_source(), "no pinned kernel checkout")
    def test_the_floor_s_handler_votes_again_under_the_lock_and_only_for_cx_and_mx(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = pathlib.Path(tmp)
            self.pinned_files(tree, usb=False)
            subprocess.run(["patch", "-s", "-d", str(tree), "-p1", "--fuzz=0", "--no-backup-if-mismatch"],
                           input=(REPO / "kernel/patches" / self.FLOOR).read_text(), check=True, text=True)
            patched = (tree / f"{RPMHPD}.c").read_text()
        handler = re.search(r"^static int rpmhpd_debug_ret_floor_set\(.*?^\}\n", patched, re.M | re.S).group(0)
        lock, unlock = handler.index("mutex_lock(&rpmhpd_lock);"), handler.index("mutex_unlock(&rpmhpd_lock);")
        for said in ("debug_ret_floor = on;", "rpmhpd_aggregate_corner(pd, pd->enabled ?",
                     "max(pd->corner, pd->enable_corner) : 0);",
                     "if (!pd || pd->active_only || !rpmhpd_debug_floored(pd))"):
            with self.subTest(said):
                self.assertTrue(lock < handler.index(said) < unlock, said)
        self.assertIn("module_param_cb(debug_ret_floor, &rpmhpd_debug_ret_floor_ops, &debug_ret_floor, 0644);",
                      patched)
        # The descriptor the handler walks is set once, by probe, under the lock.
        self.assertIn("\tmutex_lock(&rpmhpd_lock);\n\tdebug_ret_floor_desc = desc;\n\tmutex_unlock(&rpmhpd_lock);\n",
                      patched)
        # rpmhpd_sync_state() votes the same way, as the handler says.
        sync = re.search(r"^static void rpmhpd_sync_state\(.*?^\}\n", patched, re.M | re.S).group(0)
        self.assertIn("corner = max(pd->corner, pd->enable_corner);", sync)
        self.assertIn("corner = 0;", sync)


def upstream_linux():
    """A read-only clone of mainline Linux that has revision 10's three
    upstream commits (v7.4-rc4 on the build host): UPSTREAM_LINUX_GIT, or the
    one the 2026-11-22 research left under UBUNTU_WORKDIR.  Only read."""
    base = os.environ.get("UBUNTU_WORKDIR", str(pathlib.Path.home() / "ubuntu-gts9u"))
    path = pathlib.Path(os.environ.get("UPSTREAM_LINUX_GIT",
                                       base + "/tmp/deep-sleep-plan-scratch/pcie-agent/torvalds.git"))
    if not (path / "HEAD").exists() and not (path / ".git").exists():
        return None
    found = subprocess.run(["git", "-C", str(path), "cat-file", "-e", f"{FIXES[2][2]}^{{commit}}"],
                           capture_output=True)
    return path if found.returncode == 0 else None


def diff_lines(text):
    """The changed lines of a diff (+ and -, not the file headers), by file."""
    found, target = {}, None
    for line in text.splitlines():
        if line.startswith("+++ b/"):
            target = line[6:].split("\t")[0]
        elif line.startswith("--- ") or line.startswith("+++ "):
            continue
        elif target and line[:1] in "+-" and line[:1]:
            found.setdefault(target, []).append(line)
    return found


class SleepFixesTest(unittest.TestCase):
    """Revision 10's SM8550 sleep fixes: the build script's variable,
    APPLY_SM8550_SLEEP_FIXES, run from its own text (off by default, 0 or 1
    only, each upstream commit applied strictly and once to the files as the
    build has them by then, on both boards, a tree that carries any of them
    refused without the variable, unpark-pcie0-pipe-mux.patch left out with
    it and refused in a tree with it), the patches against the upstream
    commits (author, sign-offs, the change), and the second UFS PHY
    diagnostic behind both variables.  The pinned kernel checkout and the
    upstream clone are only read."""

    BUILD = REPO / "scripts/build-mainline-kernel.sh"
    PWRDN = UFS_PATCHES[2]
    PWRDN_MARKER = "the power-up write of POWER_DOWN_CONTROL holds"

    def text(self):
        return self.BUILD.read_text()

    def fix_block(self):
        """sleep_fix() and its three calls."""
        text = self.text()
        start = text.index("sleep_fix() { # FILE LINE PATCH\n")
        end = text.index(f"\t{FIX_PATCHES[2]}\n", start) + len(f"\t{FIX_PATCHES[2]}\n")
        return text[start:end]

    def unpark_block(self):
        """The unpark patch's if/elif/else, and apply_unless() it calls."""
        text = self.text()
        apply_unless = re.search(r"^apply_unless\(\) \{.*?^\}\n", text, re.M | re.S).group(0)
        start = text.index('if [ "$apply_sm8550_sleep_fixes" = 1 ]; then\n\tif grep -q \'clk_set_rate')
        end = text.index("\nfi\n", start) + len("\nfi\n")
        return apply_unless + text[start:end]

    def pwrdn_block(self):
        """The two UFS PHY diagnostics' blocks, from qmp_ufs_source= on."""
        text = self.text()
        start = text.index("qmp_ufs_source=")
        marker = text.index("qmp_ufs_pwrdn_marker=", start)
        end = text.index("\nfi\n", marker) + len("\nfi\n")
        return text[start:end]

    def run_block(self, block, tree, fixes, diag="0"):
        return subprocess.run(["bash", "-c", "set -euo pipefail\nkernel_tree=$1 pat=$2 apply_sm8550_sleep_fixes=$3 "
                               "apply_qmp_ufs_diagnostic=$4\n" + block, "build", str(tree),
                               str(REPO / "kernel/patches"), fixes, diag], capture_output=True, text=True)

    def test_the_variable_is_off_by_default_and_0_or_1(self):
        text = self.text()
        self.assertIn("\napply_sm8550_sleep_fixes=${APPLY_SM8550_SLEEP_FIXES:-0}\n", text)
        self.assertIn('\nvalidate_bool APPLY_SM8550_SLEEP_FIXES "$apply_sm8550_sleep_fixes"\n', text)
        validate = re.search(r"^validate_bool\(\) \{.*?^\}\n", text, re.M | re.S).group(0)
        check = ("apply_sm8550_sleep_fixes=${APPLY_SM8550_SLEEP_FIXES:-0}\n" + validate +
                 'validate_bool APPLY_SM8550_SLEEP_FIXES "$apply_sm8550_sleep_fixes"\n')
        for value, ok in (("", True), ("0", True), ("1", True), ("yes", False), ("2", False), ("01", False)):
            with self.subTest(value=value):
                env = {k: v for k, v in os.environ.items() if k != "APPLY_SM8550_SLEEP_FIXES"}
                if value:
                    env["APPLY_SM8550_SLEEP_FIXES"] = value
                result = subprocess.run(["bash", "-c", "set -euo pipefail\n" + check], capture_output=True,
                                        text=True, env=env)
                self.assertEqual(result.returncode == 0, ok, result.stderr)
                if not ok:
                    self.assertIn("APPLY_SM8550_SLEEP_FIXES must be 0 or 1", result.stderr)

    def test_the_fixes_come_after_every_other_patch_of_their_files_on_both_boards(self):
        """No board in the blocks; the fixes go on after this script's other
        patches of their files (the HS-G5 one of ufs-qcom.c on the SM-X710
        alone, and since revision 11 the UFS PHY power reference fix of
        every build and its diagnostic), and no other patch of the checkout
        touches those files; the unpark patch is the PCIe PHY's only one."""
        text = self.text()
        block, unpark, pwrdn = self.fix_block(), self.unpark_block(), self.pwrdn_block()
        for name, part in (("fixes", block), ("unpark", unpark), ("diagnostics", pwrdn)):
            with self.subTest(name):
                self.assertNotIn("DEVICE", part)
                self.assertNotIn("gts9", part.replace("gts9u.patch", ""))
        at = text.index(block)
        for earlier in ("msm-adreno-gmu-hub-clk-param.patch", "msm-adreno-a740-scm-init-param.patch",
                        'bash "$repo/scripts/stage-gpu-recovery-experiment.sh"', "ufs-qcom-hs-g5-at-300mhz-sm8550.patch",
                        f"drivers/ufs/host/ufs-qcom.c {UFS_QCOM_PATCHES[0]}\n",
                        f'< "$pat/{UFS_QCOM_PATCHES[1]}"'):
            self.assertLess(text.index(earlier), at, earlier)
        self.assertLess(text.index('if [ "$apply_sm8550_sleep_fixes" = 1 ]; then\n\tif grep -q \'clk_set_rate'), at)
        touching = {}
        for patch in (REPO / "kernel/patches").glob("*.patch"):
            for target in re.findall(r"^\+\+\+ b/(\S+)", patch.read_text(), re.M):
                touching.setdefault(target, set()).add(patch.name)
        self.assertEqual(touching[f"{PHYMUX}.c"], {FIX_PATCHES[0]})
        self.assertEqual(touching[f"{GMU}.c"], {FIX_PATCHES[1], "msm-adreno-bound-fault-coredump-wait-gts9u.patch",
                                                "msm-adreno-a740-scm-init-param.patch",
                                                "msm-adreno-gmu-hub-clk-param.patch"})
        self.assertEqual(touching[f"{UFS_QCOM}.c"], {FIX_PATCHES[2], "ufs-qcom-hs-g5-at-300mhz-sm8550.patch",
                                                     *UFS_QCOM_PATCHES})
        self.assertEqual(touching[f"{UFS_QCOM}.h"], {FIX_PATCHES[2], *UFS_QCOM_PATCHES})
        self.assertEqual(touching[f"{QMP_PCIE}.c"], {UNPARK})
        self.assertEqual(touching[f"{QMP}.c"], {"qmp-ufs-reset-serdes-before-power-down-gts9u.patch",
                                                UFS_PATCHES[0], UFS_PATCHES[1], UFS_PATCHES[2]})

    def test_each_patch_is_its_upstream_commit_with_its_author_and_sign_offs(self):
        """The header of git format-patch with the upstream hash, author and
        date, "commit ... upstream (TAG)", the commit's message and every
        tag of it, then this port's note of what was adapted.  With the
        upstream clone at hand: the message is the commit's, word for word,
        and the changed lines are the commit's."""
        upstream = upstream_linux()
        for (name, target, commit, line), tag in zip(FIXES, ("v7.2", "v7.3-rc1", "v7.4-rc1")):
            with self.subTest(name):
                text = (REPO / "kernel/patches" / name).read_text()
                self.assertTrue(text.startswith(f"From {commit} Mon Sep 17 00:00:00 2001\nFrom: "), text[:100])
                self.assertIn(f"\ncommit {commit} upstream ({tag}).\n", text)
                self.assertIn("\n[ Backport to the v7.2-rc3 base of the Galaxy Tab S9 port.", text)
                self.assertIn(f"APPLY_SM8550_SLEEP_FIXES=1", text.split("\n---\n", 1)[0])
                self.assertGreaterEqual(text.count("\nSigned-off-by: "), 2)
                added = added_lines(name)
                self.assertIn(target, added)
                self.assertTrue([a for a in added[target] if line in a], line)
                if upstream is None:
                    continue
                show = subprocess.run(["git", "-C", str(upstream), "show", "--format=%an <%ae>%n%B", commit],
                                      check=True, capture_output=True, text=True).stdout
                author, rest = show.split("\n", 1)
                message = rest[:rest.index("\ndiff --git ")].strip()
                self.assertIn(f"\nFrom: {author}\n", text)
                # The subject line (folded) and the body, tags included.
                body = message.split("\n", 1)[1].strip()
                self.assertIn("\n" + body + "\n", text)
                for tagline in [l for l in message.splitlines() if re.match(r"^[A-Z][A-Za-z-]+: ", l)]:
                    self.assertIn(tagline, text)
                self.assertEqual(diff_lines(text), diff_lines(show[show.index("\ndiff --git "):]))

    def gpu_tree(self, tree):
        """a6xx_gmu.c and what its patches also touch, as the build has them
        when the fixes' block runs: the pinned files, the GPU recovery stage
        script, then the adreno parameter patches in the build's order."""
        adreno = "drivers/gpu/drm/msm/adreno"
        for name in ("a6xx_gmu.c", "a6xx_hfi.c", "adreno_device.c", "a6xx_gpu.c"):
            path = f"{adreno}/{name}"
            (tree / path).parent.mkdir(parents=True, exist_ok=True)
            (tree / path).write_text(subprocess.run(["git", "-C", str(kernel_source()), "show", "HEAD:" + path],
                                                    check=True, capture_output=True, text=True).stdout)
        subprocess.run(["bash", str(REPO / "scripts/stage-gpu-recovery-experiment.sh"), str(tree)], check=True,
                       capture_output=True, env={k: v for k, v in os.environ.items()
                                                 if k != "GPU_FAULT_WAIT_EXPERIMENTAL"})
        for name in ("msm-adreno-cached-coherent-param.patch", "msm-adreno-hwcg-llc-params.patch",
                     "msm-adreno-a7xx-ccu-inval-param.patch", "msm-adreno-a740-scm-init-param.patch",
                     "msm-adreno-gmu-hub-clk-param.patch"):
            subprocess.run(["patch", "-s", "-d", str(tree), "-p1"], input=(REPO / "kernel/patches" / name).read_text(),
                           check=True, capture_output=True, text=True)

    def pinned_tree(self, tree, g5):
        """The fixes' files as the build has them when their block runs, on
        the SM-X710 (G5: the HS-G5 patch of ufs-qcom.c) or the SM-X910."""
        for path in (f"{PHYMUX}.c", f"{UFS_QCOM}.c", f"{UFS_QCOM}.h", f"{QMP_PCIE}.c"):
            (tree / path).parent.mkdir(parents=True, exist_ok=True)
            (tree / path).write_text(subprocess.run(["git", "-C", str(kernel_source()), "show", "HEAD:" + path],
                                                    check=True, capture_output=True, text=True).stdout)
        self.gpu_tree(tree)
        if g5:
            subprocess.run(["patch", "-s", "-d", str(tree), "-p1"],
                           input=(REPO / "kernel/patches/ufs-qcom-hs-g5-at-300mhz-sm8550.patch").read_text(),
                           check=True, capture_output=True, text=True)

    def files(self, tree):
        return {str(p.relative_to(tree)): p.read_text() for p in tree.rglob("*") if p.is_file()}

    @unittest.skipUnless(kernel_source(), "no pinned kernel checkout")
    def test_the_variable_applies_the_three_once_on_both_boards_and_a_build_without_it_refuses_them(self):
        upstream = upstream_linux()
        for board, g5 in (("SM-X710", True), ("SM-X910", False)):
            with self.subTest(board), tempfile.TemporaryDirectory() as tmp:
                tree = pathlib.Path(tmp)
                self.pinned_tree(tree, g5)
                before = self.files(tree)
                result = self.run_block(self.fix_block(), tree, "0")
                self.assertEqual((result.returncode, self.files(tree)), (0, before), result.stderr)
                result = self.run_block(self.fix_block(), tree, "1")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotRegex(result.stdout, "offset|fuzz")
                fixed = self.files(tree)
                # The three files and ufs-qcom.h changed, nothing else, no
                # backup left next to them.
                self.assertEqual(sorted(p for p in fixed if fixed[p] != before.get(p)),
                                 sorted([f"{PHYMUX}.c", f"{GMU}.c", f"{UFS_QCOM}.c", f"{UFS_QCOM}.h"]))
                self.assertEqual(sorted(fixed), sorted(before))
                for name, target, commit, line in FIXES:
                    self.assertEqual(fixed[target].count(line), 1, name)
                if upstream:
                    # The revert gives v7.2's file, and ufs-qcom.h is the
                    # commit's.
                    for path, commit in ((f"{PHYMUX}.c", FIXES[0][2]), (f"{UFS_QCOM}.h", FIXES[2][2])):
                        self.assertEqual(fixed[path], subprocess.run(
                            ["git", "-C", str(upstream), "show", f"{commit}:{path}"], check=True,
                            capture_output=True, text=True).stdout, path)
                # A reused tree: nothing changes.
                result = self.run_block(self.fix_block(), tree, "1")
                self.assertEqual((result.returncode, self.files(tree)), (0, fixed), result.stderr)
                # A build without the variable in that tree: refused, the
                # tree as it was.
                result = self.run_block(self.fix_block(), tree, "0")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(f"{FIX_PATCHES[0]} remains in this source tree; use a fresh "
                              "KERNEL_WORKTREE/KERNEL_BUILD_DIR for a build without APPLY_SM8550_SLEEP_FIXES=1.",
                              result.stderr)
                self.assertEqual(self.files(tree), fixed)
                # A copy with a fix's line and not the whole of it is refused.
                damaged = fixed[f"{UFS_QCOM}.h"].replace("\tstruct clk *rx_lane1_sync_clk;\n", "")
                self.assertNotEqual(damaged, fixed[f"{UFS_QCOM}.h"])
                (tree / f"{UFS_QCOM}.h").write_text(damaged)
                result = self.run_block(self.fix_block(), tree, "1")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((tree / f"{UFS_QCOM}.h").read_text(), damaged)

    @unittest.skipUnless(kernel_source(), "no pinned kernel checkout")
    def test_the_unpark_patch_is_left_out_with_the_fixes_and_refused_with_the_revert(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = pathlib.Path(tmp)
            self.pinned_tree(tree, True)
            pristine = self.files(tree)
            # With the fixes: left out, nothing written.
            result = self.run_block(self.unpark_block(), tree, "1")
            self.assertEqual((result.returncode, self.files(tree)), (0, pristine), result.stderr)
            # Without: the patch goes in as before (apply_unless).
            result = self.run_block(self.unpark_block(), tree, "0")
            self.assertEqual(result.returncode, 0, result.stderr)
            parked = self.files(tree)
            self.assertIn("clk_set_rate(qmp->pipe_clks[0].clk, ULONG_MAX);", parked[f"{QMP_PCIE}.c"])
            # The fixes in a tree that carries it: refused, nothing written.
            result = self.run_block(self.unpark_block(), tree, "1")
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(f"{UNPARK} is in this source tree, and APPLY_SM8550_SLEEP_FIXES=1 leaves it out",
                          result.stderr)
            self.assertEqual(self.files(tree), parked)
            # A build without the fixes in a tree with the revert: refused
            # before the patch would go in on top of it.
            (tree / f"{QMP_PCIE}.c").write_text(pristine[f"{QMP_PCIE}.c"])
            subprocess.run(["patch", "-s", "--fuzz=0", "-d", str(tree), "-p1"],
                           input=(REPO / "kernel/patches" / FIX_PATCHES[0]).read_text(), check=True, text=True)
            reverted = self.files(tree)
            result = self.run_block(self.unpark_block(), tree, "0")
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(f"{FIX_PATCHES[0]} remains in this source tree", result.stderr)
            self.assertEqual(self.files(tree), reverted)

    def test_the_power_up_diagnostic_reads_and_logs_and_writes_nothing(self):
        """The second UFS PHY diagnostic: two reads of POWER_DOWN_CONTROL,
        one udelay between them, one KERN_DEBUG line, a flag that only
        qmp_ufs_set_mode() of the SM8550 PHY raises; no register write, no
        board check, no line of the first diagnostic changed."""
        added = added_lines(self.PWRDN)[f"{QMP}.c"]
        code = [line for line in added if not line.lstrip().startswith(("*", "/*"))]
        joined = "\n".join(code)
        for word in ("writel", "qphy_setbits", "qphy_clrbits", "regmap", "of_machine_is_compatible", "return -"):
            self.assertNotIn(word, joined)
        self.assertEqual(joined.count("readl(reg)"), 2)
        self.assertEqual(joined.count("udelay(QMP_UFS_DBG_PWRDN_DELAY_US)"), 1)
        self.assertIn("#define QMP_UFS_DBG_PWRDN_DELAY_US\t100U", joined)
        self.assertIn('\t\t   "power-up write: PWRDN %#x right after it, %#x %u us later\\n",', joined)
        self.assertIn("\tdev_printk(KERN_DEBUG, qmp->dev,", joined)
        self.assertIn("\tif (cfg == &sm8550_ufsphy_cfg)\n\t\tqmp->dbg_pwrdn_pending = true;", joined)
        self.assertIn("\tif (qmp->dbg_pwrdn_pending)\n\t\tqmp_ufs_dbg_pwrdn(qmp);", joined)
        # Its marker is its own, and not the first diagnostic's.
        self.assertEqual("\n".join(added).count(self.PWRDN_MARKER), 1)
        self.assertNotIn("Diagnostic for SM8550 resume tests, not for release builds", "\n".join(added))
        self.assertEqual([p.name for p in (REPO / "kernel/patches").glob("*.patch")
                          if self.PWRDN_MARKER in p.read_text()], [self.PWRDN])
        # It removes nothing.
        self.assertEqual([line for line in diff_lines((REPO / "kernel/patches" / self.PWRDN).read_text())[f"{QMP}.c"]
                          if line.startswith("-")], [])

    @unittest.skipUnless(kernel_source(), "no pinned kernel checkout")
    def test_the_power_up_diagnostic_goes_on_top_of_the_diagnostic_with_both_variables_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = pathlib.Path(tmp)
            (tree / QMP).parent.mkdir(parents=True)
            (tree / f"{QMP}.c").write_text(subprocess.run(["git", "-C", str(kernel_source()), "show",
                                                           f"HEAD:{QMP}.c"], check=True, capture_output=True,
                                                          text=True).stdout)
            subprocess.run(["patch", "-s", "-d", str(tree), "-p1"],
                           input=(REPO / "kernel/patches/qmp-ufs-reset-serdes-before-power-down-gts9u.patch")
                           .read_text(), check=True, text=True)
            subprocess.run(["bash", str(REPO / "scripts/stage-ufs-resume-experiment.sh"), str(tree)], check=True,
                           capture_output=True, env={k: v for k, v in os.environ.items()
                                                     if k != "UFS_PCS_RESET_EXPERIMENTAL"})
            staged = (tree / f"{QMP}.c").read_text()
            listing = sorted(p.name for p in (tree / QMP).parent.iterdir())
            # The diagnostic alone (revisions 8 and 9): the second one stays out.
            result = self.run_block(self.pwrdn_block(), tree, "0", "1")
            self.assertEqual(result.returncode, 0, result.stderr)
            first = (tree / f"{QMP}.c").read_text()
            self.assertNotIn(self.PWRDN_MARKER, first)
            # Both variables: on top of it, strictly, once.
            result = self.run_block(self.pwrdn_block(), tree, "1", "1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotRegex(result.stdout, "offset|fuzz")
            both = (tree / f"{QMP}.c").read_text()
            self.assertEqual(both.count(self.PWRDN_MARKER), 1)
            # No backup next to it.
            self.assertEqual(sorted(p.name for p in (tree / QMP).parent.iterdir()), listing)
            # A reused tree: the first diagnostic's strict check still
            # passes with the second on top, and nothing changes.
            result = self.run_block(self.pwrdn_block(), tree, "1", "1")
            self.assertEqual((result.returncode, (tree / f"{QMP}.c").read_text()), (0, both), result.stderr)
            # Either variable off in that tree: refused, as it was.
            for fixes, diag, said in (("0", "1", "The UFS PHY power-up diagnostic remains in this source tree"),
                                      ("1", "0", "The UFS PHY diagnostic remains in this source tree")):
                with self.subTest(fixes=fixes, diag=diag):
                    result = self.run_block(self.pwrdn_block(), tree, fixes, diag)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(said, result.stderr)
                    self.assertEqual((tree / f"{QMP}.c").read_text(), both)
            # The marker without the code is refused too.
            (tree / f"{QMP}.c").write_text(first + f"/* {self.PWRDN_MARKER} */\n")
            result = self.run_block(self.pwrdn_block(), tree, "1", "1")
            self.assertNotEqual(result.returncode, 0)
            self.assertNotEqual(staged, first)


class PcieGdscOffTest(unittest.TestCase):
    """Revision 11's pcie_0_gdsc: the build script's variable,
    APPLY_SM8550_PCIE0_GDSC_OFF, run from its own text (off by default, 0 or
    1 only, refused without APPLY_SM8550_SLEEP_FIXES=1 before the tree is
    touched, the patch applied strictly and once on top of the USB GDSC
    patch, a tree that carries it refused without the variable, and so is
    one with its first form), and the patch against the pinned kernel
    checkout (only read): two lines of the table, pcie_0_gdsc's .pwrsts
    changed and its genpd flag added, so that the debug switches go in one
    line further down with it than without it."""

    BUILD = REPO / "scripts/build-mainline-kernel.sh"
    PATCH = GDSC_PATCHES[0]

    def text(self):
        return self.BUILD.read_text()

    def block(self):
        """The build script's lines from pcie0_gdsc_patch= to its fi."""
        text = self.text()
        start = text.index("pcie0_gdsc_patch=")
        return text[start:text.index("\nfi\n", start) + len("\nfi\n")]

    def run_block(self, block, tree, value, debug="0"):
        return subprocess.run(["bash", "-c", "set -euo pipefail\nkernel_tree=$1 pat=$2 apply_pcie0_gdsc_off=$3 "
                               "apply_deep_sleep_debug=$4\n" + block, "build", str(tree),
                               str(REPO / "kernel/patches"), value, debug], capture_output=True, text=True)

    def pinned_file(self, tree):
        """gcc-sm8550.c as the build has it when the block runs: the pinned
        file with the USB GDSC patch."""
        (tree / GCC).parent.mkdir(parents=True, exist_ok=True)
        (tree / f"{GCC}.c").write_text(subprocess.run(["git", "-C", str(kernel_source()), "show", f"HEAD:{GCC}.c"],
                                                      check=True, capture_output=True, text=True).stdout)
        subprocess.run(["patch", "-s", "-d", str(tree), "-p1", "--fuzz=0", "--no-backup-if-mismatch"],
                       input=(REPO / "kernel/patches/gcc-sm8550-usb-gdscs-retention.patch").read_text(),
                       check=True, text=True)

    def test_the_variable_is_off_by_default_0_or_1_and_needs_the_sleep_fixes(self):
        text = self.text()
        self.assertIn("\napply_pcie0_gdsc_off=${APPLY_SM8550_PCIE0_GDSC_OFF:-0}\n", text)
        self.assertIn('\nvalidate_bool APPLY_SM8550_PCIE0_GDSC_OFF "$apply_pcie0_gdsc_off"\n', text)
        validate = re.search(r"^validate_bool\(\) \{.*?^\}\n", text, re.M | re.S).group(0)
        start = text.index('if [ "$apply_pcie0_gdsc_off" = 1 ] && [ "$apply_sm8550_sleep_fixes" != 1 ]; then\n')
        needs = text[start:text.index("\nfi\n", start) + len("\nfi\n")]
        # Checked with the other variables, before the source tree is made.
        self.assertLess(start, text.index('git -C "$kernel_src" worktree add --detach "$kernel_tree" HEAD'))
        check = ("apply_sm8550_sleep_fixes=${APPLY_SM8550_SLEEP_FIXES:-0}\n"
                 "apply_pcie0_gdsc_off=${APPLY_SM8550_PCIE0_GDSC_OFF:-0}\n" + validate +
                 'validate_bool APPLY_SM8550_PCIE0_GDSC_OFF "$apply_pcie0_gdsc_off"\n' + needs)
        for value, fixes, ok in (("", "", True), ("0", "", True), ("0", "1", True), ("1", "1", True),
                                 ("1", "", False), ("1", "0", False), ("yes", "1", False), ("2", "1", False),
                                 ("01", "1", False)):
            with self.subTest(value=value, fixes=fixes):
                env = {k: v for k, v in os.environ.items()
                       if k not in ("APPLY_SM8550_PCIE0_GDSC_OFF", "APPLY_SM8550_SLEEP_FIXES")}
                if value:
                    env["APPLY_SM8550_PCIE0_GDSC_OFF"] = value
                if fixes:
                    env["APPLY_SM8550_SLEEP_FIXES"] = fixes
                result = subprocess.run(["bash", "-c", "set -euo pipefail\n" + check], capture_output=True,
                                        text=True, env=env)
                self.assertEqual(result.returncode == 0, ok, result.stderr)
                if not ok and value == "1":
                    self.assertIn("APPLY_SM8550_PCIE0_GDSC_OFF=1 needs APPLY_SM8550_SLEEP_FIXES=1: without the "
                                  "PIPE clock revert pcie_0_gdsc does not power up again at the resume",
                                  result.stderr)
                elif not ok:
                    self.assertIn("APPLY_SM8550_PCIE0_GDSC_OFF must be 0 or 1", result.stderr)

    def test_the_patch_goes_on_after_the_usb_patch_and_before_the_debug_switches_on_both_boards(self):
        """No board in the block or the patch; the patch changes two lines of
        gcc-sm8550.c in one hunk, pcie_0_gdsc's power states and its genpd
        flag, and nothing else."""
        text = self.text()
        block = self.block()
        at = text.index(block)
        self.assertLess(text.index("\tdrivers/clk/qcom/gcc-sm8550.c gcc-sm8550-usb-gdscs-retention.patch\n"), at)
        self.assertLess(at, text.index("deep_sleep_debug_marker='"))
        self.assertNotIn("DEVICE", block)
        self.assertNotIn("gts9", block)
        self.assertEqual(text.count(f"pcie0_gdsc_patch={self.PATCH}\n"), 1)
        patch = (REPO / "kernel/patches" / self.PATCH).read_text()
        self.assertEqual(diff_lines(patch), {f"{GCC}.c": ["+" + GUARD_FLAG_LINE[:-1], "-\t.pwrsts = PWRSTS_RET_ON,",
                                                          "+" + PCIE0_LINE[:-1]]})
        self.assertIn(' \t\t.name = "pcie_0_gdsc",\n+' + GUARD_FLAG_LINE + ' \t},\n-\t.pwrsts = PWRSTS_RET_ON,\n',
                      patch)
        # One hunk, so that a reused tree's reverse check takes both lines
        # or neither.
        self.assertEqual(patch.count("\n@@ "), 1)
        header = patch.split("\n--- a/", 1)[0]
        for words in ("APPLY_SM8550_PCIE0_GDSC_OFF=1", "APPLY_SM8550_SLEEP_FIXES=1", "967e011013ed",
                      "No board check", "APPLY_SM8550_PCIE0_GDSC_GUARD=1", "GENPD_FLAG_ACTIVE_WAKEUP",
                      PCIE_PATCHES[0], "genpd_finish_suspend()"):
            self.assertIn(words, " ".join(header.split()))
        self.assertNotIn("of_machine_is_compatible", patch)

    @unittest.skipUnless(kernel_source(), "no pinned kernel checkout")
    def test_the_variable_applies_it_once_and_a_build_without_it_refuses_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = pathlib.Path(tmp)
            self.pinned_file(tree)
            before = (tree / f"{GCC}.c").read_text()
            listing = sorted(p.name for p in (tree / GCC).parent.iterdir())
            result = self.run_block(self.block(), tree, "0")
            self.assertEqual((result.returncode, (tree / f"{GCC}.c").read_text()), (0, before), result.stderr)
            result = self.run_block(self.block(), tree, "1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotRegex(result.stdout, "offset|fuzz")
            after = (tree / f"{GCC}.c").read_text()
            self.assertEqual(sorted(p.name for p in (tree / GCC).parent.iterdir()), listing)
            # pcie_0_gdsc's entry alone: its genpd flag added below its
            # name, its .pwrsts changed; one line more.
            old, new = before.splitlines(True), after.splitlines(True)
            name = old.index('\t\t.name = "pcie_0_gdsc",\n')
            self.assertEqual(old[name + 1:name + 3], ["\t},\n", "\t.pwrsts = PWRSTS_RET_ON,\n"])
            self.assertEqual(new, old[:name + 1] + [GUARD_FLAG_LINE, "\t},\n", PCIE0_LINE] + old[name + 3:])
            body = re.search(r"^static struct gdsc pcie_0_gdsc = \{\n(.*?)^\};", after, re.M | re.S).group(1)
            self.assertIn(PCIE0_LINE, body)
            self.assertIn("\t.pd = {\n" + '\t\t.name = "pcie_0_gdsc",\n' + GUARD_FLAG_LINE + "\t},\n", body)
            self.assertEqual(after.count("PWRSTS_OFF_ON, /* "), 1)
            self.assertEqual(after.count("GENPD_FLAG_ACTIVE_WAKEUP"), 1)
            # A reused tree: nothing changes.
            result = self.run_block(self.block(), tree, "1")
            self.assertEqual((result.returncode, (tree / f"{GCC}.c").read_text()), (0, after), result.stderr)
            # A build without the variable in that tree: refused, as it was.
            result = self.run_block(self.block(), tree, "0")
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(f"{self.PATCH} remains in this source tree; use a fresh KERNEL_WORKTREE/KERNEL_BUILD_DIR "
                          "for a build without APPLY_SM8550_PCIE0_GDSC_OFF=1.", result.stderr)
            self.assertEqual((tree / f"{GCC}.c").read_text(), after)
            # A copy with the line but not the patch's context is refused,
            # and so is the patch's first form (revision 11 as first
            # prepared): the .pwrsts line without the genpd flag, which
            # would build a kernel without the guard's clock half.
            first_form = after.replace(GUARD_FLAG_LINE, "")
            damaged = after.replace(PCIE0_LINE + "\t.flags = VOTABLE | POLL_CFG_GDSCR | RETAIN_FF_ENABLE,\n",
                                    PCIE0_LINE + "\t.flags = VOTABLE | POLL_CFG_GDSCR,\n")
            for copy in (damaged, first_form):
                self.assertNotEqual(copy, after)
                (tree / f"{GCC}.c").write_text(copy)
                result = self.run_block(self.block(), tree, "1")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(f"{self.PATCH} is in this source tree only in part (or in its first form, without "
                              "the genpd flag); use a fresh KERNEL_WORKTREE/KERNEL_BUILD_DIR.", result.stderr)
                self.assertEqual((tree / f"{GCC}.c").read_text(), copy)

    @unittest.skipUnless(kernel_source(), "no pinned kernel checkout")
    def test_the_debug_switches_go_in_one_line_further_down_with_it(self):
        """The debug switches' block of the build after this one, in a tree
        with the patch and in one without: no offset without it, one line
        with it (the genpd flag's), no fuzz either way, and the two files
        differ in pcie_0_gdsc's two lines alone; the debug patch's reverse
        check passes on both (a reused tree)."""
        debug = DeepSleepDebugSwitchTest("run").block()
        files = []
        with tempfile.TemporaryDirectory() as tmp:
            for value in ("0", "1"):
                tree = pathlib.Path(tmp, value)
                self.pinned_file(tree)
                for path in (RPMHPD, QCOM_STATS):
                    (tree / path).parent.mkdir(parents=True)
                    (tree / f"{path}.c").write_text(subprocess.run(
                        ["git", "-C", str(kernel_source()), "show", f"HEAD:{path}.c"], check=True,
                        capture_output=True, text=True).stdout)
                for again in (False, True):
                    result = self.run_block(self.block() + debug, tree, value, "1")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn("fuzz", result.stdout)
                    # The debug patch's hunk, applied or checked in reverse.
                    self.assertEqual(re.findall(r"offset .*", result.stdout),
                                     ["offset 1 line)."] if value == "1" else [], result.stdout)
                files.append((tree / f"{GCC}.c").read_text().splitlines(True))
        old, new = files
        name = old.index('\t\t.name = "pcie_0_gdsc",\n')
        self.assertEqual(new, old[:name + 1] + [GUARD_FLAG_LINE, "\t},\n", PCIE0_LINE] + old[name + 3:])


class PcieGuardTest(unittest.TestCase):
    """Revision 11's guard and root port switch in the build script, run from
    its own text: APPLY_SM8550_PCIE0_GDSC_GUARD (off by default, 0 or 1 only,
    refused without APPLY_SM8550_PCIE0_GDSC_OFF=1 and the other way round,
    before the tree is touched), the PCIe host's half of the guard applied
    strictly and once, the root port switch only with the guard and the
    debug switches, a tree that carries either refused for any other build;
    and the two patches against the pinned kernel checkout (only read)."""

    BUILD = REPO / "scripts/build-mainline-kernel.sh"
    GUARD = PCIE_PATCHES[0]
    RPD3 = PCIE_PATCHES[1]

    def text(self):
        return self.BUILD.read_text()

    def block(self, start):
        text = self.text()
        at = text.index(start)
        return text[at:text.index("\nfi\n", at) + len("\nfi\n")]

    def run_block(self, block, tree, guard, debug="0"):
        return subprocess.run(["bash", "-c", "set -euo pipefail\nkernel_tree=$1 pat=$2 apply_pcie0_gdsc_guard=$3 "
                               "apply_deep_sleep_debug=$4\n" + block, "build", str(tree),
                               str(REPO / "kernel/patches"), guard, debug], capture_output=True, text=True)

    def pinned_file(self, tree):
        (tree / PCIE_QCOM).parent.mkdir(parents=True, exist_ok=True)
        (tree / f"{PCIE_QCOM}.c").write_text(subprocess.run(
            ["git", "-C", str(kernel_source()), "show", f"HEAD:{PCIE_QCOM}.c"], check=True, capture_output=True,
            text=True).stdout)

    def test_the_variable_is_off_by_default_0_or_1_and_goes_with_the_gdsc_one(self):
        text = self.text()
        self.assertIn("\napply_pcie0_gdsc_guard=${APPLY_SM8550_PCIE0_GDSC_GUARD:-0}\n", text)
        validate = re.search(r"^validate_bool\(\) \{.*?^\}\n", text, re.M | re.S).group(0)
        start = text.index('if [ "$apply_pcie0_gdsc_off" != "$apply_pcie0_gdsc_guard" ]; then\n')
        together = text[start:text.index("\nfi\n", start) + len("\nfi\n")]
        self.assertLess(text.index('\nvalidate_bool APPLY_SM8550_PCIE0_GDSC_GUARD "$apply_pcie0_gdsc_guard"\n'),
                        start)
        self.assertLess(start, text.index('git -C "$kernel_src" worktree add --detach "$kernel_tree" HEAD'))
        check = ("apply_pcie0_gdsc_off=${APPLY_SM8550_PCIE0_GDSC_OFF:-0}\n"
                 "apply_pcie0_gdsc_guard=${APPLY_SM8550_PCIE0_GDSC_GUARD:-0}\n" + validate +
                 'validate_bool APPLY_SM8550_PCIE0_GDSC_OFF "$apply_pcie0_gdsc_off"\n'
                 'validate_bool APPLY_SM8550_PCIE0_GDSC_GUARD "$apply_pcie0_gdsc_guard"\n' + together)
        for off, guard, ok in (("", "", True), ("0", "0", True), ("1", "1", True), ("1", "", False),
                               ("1", "0", False), ("", "1", False), ("0", "1", False), ("1", "yes", False),
                               ("1", "01", False)):
            with self.subTest(off=off, guard=guard):
                env = {k: v for k, v in os.environ.items()
                       if k not in ("APPLY_SM8550_PCIE0_GDSC_OFF", "APPLY_SM8550_PCIE0_GDSC_GUARD")}
                if off:
                    env["APPLY_SM8550_PCIE0_GDSC_OFF"] = off
                if guard:
                    env["APPLY_SM8550_PCIE0_GDSC_GUARD"] = guard
                result = subprocess.run(["bash", "-c", "set -euo pipefail\n" + check], capture_output=True,
                                        text=True, env=env)
                self.assertEqual(result.returncode == 0, ok, result.stderr)
                if not ok and guard in ("", "0", "1"):
                    self.assertIn("APPLY_SM8550_PCIE0_GDSC_OFF=1 and APPLY_SM8550_PCIE0_GDSC_GUARD=1 go together: "
                                  "without the guard genpd collapses pcie_0_gdsc under a PCIe host that keeps its "
                                  "link (the boot's panel cycle)", result.stderr)
                elif not ok:
                    self.assertIn("APPLY_SM8550_PCIE0_GDSC_GUARD must be 0 or 1", result.stderr)

    def test_the_blocks_go_in_order_on_both_boards(self):
        """The guard after pcie_0_gdsc's patch, the root port switch after
        the debug switches; no board in either block or patch; pcie-qcom.c
        is changed by these two patches alone."""
        text = self.text()
        guard = self.block("pcie0_guard_patch=")
        rpd3 = self.block("pcie_rp_d3_patch=")
        self.assertLess(text.index("pcie0_gdsc_patch="), text.index(guard))
        self.assertLess(text.index(guard), text.index("deep_sleep_debug_marker='"))
        self.assertLess(text.index("\tdrivers/soc/qcom/qcom_stats.c:qcom-stats-cxpc-violators-diagnostic.patch; do\n"),
                        text.index(rpd3))
        self.assertIn(f"\npcie0_guard_patch={self.GUARD}\n", "\n" + guard)
        self.assertIn(f"\npcie0_guard_line='{GUARD_LINE}'\n", "\n" + guard)
        self.assertIn(f"\npcie_rp_d3_patch={self.RPD3}\n", "\n" + rpd3)
        self.assertIn(f"\npcie_rp_d3_marker='{RPD3_MARKER}'\n", rpd3)
        self.assertIn('if [ "$apply_deep_sleep_debug" = 1 ] && [ "$apply_pcie0_gdsc_guard" = 1 ]; then\n', rpd3)
        for block in (guard, rpd3):
            self.assertNotIn("DEVICE", block)
            self.assertNotIn("gts9", block)
        touching = sorted(patch.name for patch in (REPO / "kernel/patches").glob("*.patch")
                          if re.search(rf"^\+\+\+ b/{re.escape(PCIE_QCOM)}\.c", patch.read_text(), re.M))
        self.assertEqual(touching, sorted(PCIE_PATCHES))
        for name in PCIE_PATCHES:
            with self.subTest(name):
                patch = (REPO / "kernel/patches" / name).read_text()
                self.assertEqual(list(diff_lines(patch)), [f"{PCIE_QCOM}.c"])
                self.assertNotIn("of_machine_is_compatible", patch)
                self.assertIn("No board check", patch.split("\n--- a/", 1)[0])
        guard_patch = (REPO / "kernel/patches" / self.GUARD).read_text()
        self.assertIn("+" + "\t\t" + GUARD_LINE + "\n", guard_patch)
        self.assertNotIn("-", "".join(l[0] for l in diff_lines(guard_patch)[f"{PCIE_QCOM}.c"]))
        rpd3_patch = (REPO / "kernel/patches" / self.RPD3).read_text()
        self.assertIn(RPD3_MARKER, "\n".join(added_lines(self.RPD3)[f"{PCIE_QCOM}.c"]))
        self.assertNotIn("-", "".join(l[0] for l in diff_lines(rpd3_patch)[f"{PCIE_QCOM}.c"]))

    def test_the_guard_marks_the_awake_path_only_after_a_suspend_that_worked(self):
        """What the PCIe host's half does, read from its added lines: the
        awake path for a kept link, out-of-band for a host that powered
        down, both at the end of a suspend_noirq that succeeded; the
        notifier registered at the end of a probe that succeeded; one line
        per sleep at the resume, the four that verify.sh and sleep-test.sh
        read."""
        patch = (REPO / "kernel/patches" / self.GUARD).read_text()
        added = "\n".join(line[1:] for line in diff_lines(patch)[f"{PCIE_QCOM}.c"])
        self.assertIn("#include <linux/pm_domain.h>", added)
        self.assertIn("\tif (link_kept)\n\t\tdevice_set_awake_path(dev);\n\telse\n"
                      "\t\tdevice_set_out_band_wakeup(dev);\n", added)
        self.assertIn("\n\tif (!ret) {\n\t\tpcie->link_kept = !pcie->pci->suspended;\n\t\t" + GUARD_LINE + "\n\t}\n",
                      added)
        self.assertIn(" \t}\n+\n+\tif (!ret) {\n+\t\tpcie->link_kept = !pcie->pci->suspended;\n+\t\t" + GUARD_LINE
                      + "\n+\t}\n+\n \treturn ret;\n }\n", patch)
        self.assertIn("+\tWRITE_ONCE(pcie->pd_off, false);\n+\n \tret = dw_pcie_suspend_noirq(pcie->pci);\n", patch)
        self.assertIn("+\tpcie->pd_nb_registered = !dev_pm_genpd_add_notifier(dev, &pcie->pd_nb);\n+\n \treturn 0;\n"
                      " \n err_phy_exit:\n", patch)
        self.assertIn("+\tqcom_pcie_report_sleep(pcie);\n+\n \tif (pcie->pci->suspended) {\n", patch)
        self.assertIn("\tif (action == GENPD_NOTIFY_OFF)\n\t\tWRITE_ONCE(pcie->pd_off, true);\n", added)
        lines = re.findall(r'dev_(?:warn|info)\(dev, "(system sleep: [^"]*)\\n"\)', added)
        self.assertEqual(lines, ["system sleep: the link was kept, but genpd powered its power domain off",
                                 "system sleep: the link was kept, and its power domain stayed on",
                                 "system sleep: the host powered down, and genpd powered its power domain off",
                                 "system sleep: the host powered down, and its power domain stayed on"])
        self.assertIn('dev_warn(dev, "system sleep: the link was kept, but genpd', added)
        # The kit's readers know them by the same words.
        for script in ("verify.sh", "sleep-test.sh"):
            text = (KIT_SRC / script).read_text()
            with self.subTest(script):
                self.assertIn("guard_re='[0-9a-f]+\\.pcie: system sleep: '", text)
                # The guard failing, and holding.
                for line in lines[:2]:
                    self.assertIn(line[len("system sleep: "):], text)

    @unittest.skipUnless(kernel_source(), "no pinned kernel checkout")
    def test_the_guard_applies_once_and_a_build_without_it_refuses_it(self):
        block = self.block("pcie0_guard_patch=")
        with tempfile.TemporaryDirectory() as tmp:
            tree = pathlib.Path(tmp)
            self.pinned_file(tree)
            before = (tree / f"{PCIE_QCOM}.c").read_text()
            listing = sorted(p.name for p in (tree / PCIE_QCOM).parent.iterdir())
            result = self.run_block(block, tree, "0")
            self.assertEqual((result.returncode, (tree / f"{PCIE_QCOM}.c").read_text()), (0, before), result.stderr)
            result = self.run_block(block, tree, "1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotRegex(result.stdout, "offset|fuzz")
            after = (tree / f"{PCIE_QCOM}.c").read_text()
            self.assertEqual(sorted(p.name for p in (tree / PCIE_QCOM).parent.iterdir()), listing)
            self.assertEqual(after.count(GUARD_LINE), 1)
            # Lines added, none taken away.
            self.assertEqual([l for l in before.splitlines(True) if l not in after.splitlines(True)], [])
            # A reused tree: nothing changes.
            result = self.run_block(block, tree, "1")
            self.assertEqual((result.returncode, (tree / f"{PCIE_QCOM}.c").read_text()), (0, after), result.stderr)
            # A build without the variable in that tree: refused.
            result = self.run_block(block, tree, "0")
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(f"{self.GUARD} remains in this source tree; use a fresh KERNEL_WORKTREE/KERNEL_BUILD_DIR "
                          "for a build without APPLY_SM8550_PCIE0_GDSC_GUARD=1.", result.stderr)
            self.assertEqual((tree / f"{PCIE_QCOM}.c").read_text(), after)
            # A copy with the line but not the rest is refused.
            damaged = after.replace("\tWRITE_ONCE(pcie->pd_off, false);\n", "")
            self.assertNotEqual(damaged, after)
            (tree / f"{PCIE_QCOM}.c").write_text(damaged)
            result = self.run_block(block, tree, "1")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((tree / f"{PCIE_QCOM}.c").read_text(), damaged)

    @unittest.skipUnless(kernel_source(), "no pinned kernel checkout")
    def test_the_root_port_switch_needs_the_guard_and_the_debug_switches(self):
        guard = self.block("pcie0_guard_patch=")
        rpd3 = self.block("pcie_rp_d3_patch=")
        with tempfile.TemporaryDirectory() as tmp:
            tree = pathlib.Path(tmp)
            self.pinned_file(tree)
            self.assertEqual(self.run_block(guard, tree, "1").returncode, 0)
            guarded = (tree / f"{PCIE_QCOM}.c").read_text()
            # Without both: not applied.
            for g, d in (("0", "0"), ("0", "1"), ("1", "0")):
                with self.subTest(guard=g, debug=d):
                    result = self.run_block(rpd3, tree, g, d)
                    self.assertEqual((result.returncode, (tree / f"{PCIE_QCOM}.c").read_text()), (0, guarded),
                                     result.stderr)
            # With both: on top of the guard, at the end of the file.
            result = self.run_block(guard + rpd3, tree, "1", "1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotRegex(result.stdout, "offset|fuzz")
            switched = (tree / f"{PCIE_QCOM}.c").read_text()
            self.assertTrue(switched.startswith(guarded.rstrip("\n")))
            self.assertEqual(switched.count(RPD3_MARKER), 1)
            # Again: a reused tree, nothing changes.
            result = self.run_block(guard + rpd3, tree, "1", "1")
            self.assertEqual((result.returncode, (tree / f"{PCIE_QCOM}.c").read_text()), (0, switched), result.stderr)
            # Any other build in that tree: refused, the tree as it was.
            for g, d in (("0", "0"), ("0", "1"), ("1", "0")):
                with self.subTest(refused=(g, d)):
                    result = self.run_block(rpd3, tree, g, d)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(f"{self.RPD3} remains in this source tree; use a fresh KERNEL_WORKTREE/"
                                  "KERNEL_BUILD_DIR for a build without both APPLY_DEEP_SLEEP_DEBUG_SWITCHES=1 and "
                                  "APPLY_SM8550_PCIE0_GDSC_GUARD=1.", result.stderr)
                    self.assertEqual((tree / f"{PCIE_QCOM}.c").read_text(), switched)

    def test_the_root_port_switch_is_run_time_only_and_for_qcom_root_ports_alone(self):
        added = "\n".join(added_lines(self.RPD3)[f"{PCIE_QCOM}.c"])
        patch = (REPO / "kernel/patches" / self.RPD3).read_text()
        self.assertIn(" builtin_platform_driver(qcom_pcie_driver);\n+\n+/*\n", patch)
        self.assertIn("static bool qcom_pcie_debug_rp_d3;", added)
        self.assertIn("\tif (system_state != SYSTEM_RUNNING)\n\t\treturn -EPERM;", added)
        self.assertIn("\t\tif (!pci_is_pcie(pdev) || pci_pcie_type(pdev) != PCI_EXP_TYPE_ROOT_PORT)\n\t\t\tcontinue;",
                      added)
        self.assertIn("\t\t    bridge->dev.parent->driver != &qcom_pcie_driver.driver)\n\t\t\tcontinue;", added)
        self.assertIn("\t\tif (pci_bridge_d3_possible(pdev))\n\t\t\tcontinue;\n\t\tpdev->bridge_d3 = on;", added)
        self.assertIn("\tpci_lock_rescan_remove();", added)
        self.assertIn("\tpci_unlock_rescan_remove();", added)
        self.assertIn("\tif (on && !n)\n\t\treturn -ENODEV;", added)
        self.assertIn("module_param_cb(debug_root_port_d3, &qcom_pcie_debug_rp_d3_ops, &qcom_pcie_debug_rp_d3, 0644);",
                      added)
        # It writes bridge_d3 and nothing else of the PCI core's.
        self.assertEqual(re.findall(r"pdev->(\w+) =", added), ["bridge_d3"])


class PipeClockGateTest(unittest.TestCase):
    """make-kit.sh's section 5c for revision 10's PCIe PIPE clock, on
    stand-in vmlinux files: their symbol lists (what llvm-nm gives) and the
    disassembly of qmp_pcie_enable() (what llvm-objdump gives)."""

    text = (KIT_SRC / "make-kit.sh").read_text()
    GATE = ("t phy_mux_enable", "t phy_mux_disable", "t phy_mux_is_enabled")
    RATE = ("t phy_mux_recalc_rate", "t phy_mux_determine_rate", "t phy_mux_set_rate")
    CALLS = ("clk_bulk_prepare", "clk_bulk_enable", "reset_control_deassert")

    def gate(self, symbols, calls, hang=None):
        calls_of = re.search(r"^calls_of\(\) \{.*?^\}\n", self.text, re.M | re.S).group(0)
        section = self.text[self.text.index('llvm-nm --defined-only "$build/vmlinux" > "$tmp/vmlinux.nm"\n'):]
        section = section[:section.index('echo "PCIe PIPE clock: $pcie_check"\n') +
                          len('echo "PCIe PIPE clock: $pcie_check"\n')]

        def write(vmlinux, syms, called):
            vmlinux.parent.mkdir(parents=True, exist_ok=True)
            vmlinux.write_text("ELF\n")
            pathlib.Path(f"{vmlinux}.nm").write_text("".join(f"ffff80008096{n:04x} {s}\n" for n, s in enumerate(syms)))
            pathlib.Path(f"{vmlinux}.dis").write_text(
                "ffff800080961c20 <qmp_pcie_enable>:\n" +
                "".join(f"ffff8000809{n:05x}:      \tbl\t0xffff800080abcdef <{c}>\n" for n, c in enumerate(called)))
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            bin_ = tmp / "bin"
            bin_.mkdir()
            (bin_ / "llvm-nm").write_text('#!/bin/sh\nfor last; do :; done\ncat "$last.nm"\n')
            (bin_ / "llvm-objdump").write_text('#!/bin/sh\nfor last; do :; done\ncat "$last.dis"\n')
            for stub in bin_.iterdir():
                stub.chmod(0o755)
            write(tmp / "build/vmlinux", symbols, calls)
            prefix = (f'set -euo pipefail; export LC_ALL=C\ndie() {{ echo "$*" >&2; exit 1; }}\n'
                      f'build={tmp}/build tmp={tmp} new_id={"1" * 40} unpark={UNPARK}\n')
            if hang:
                write(tmp / "hang/linux-gts9wifi/vmlinux", *hang)
                prefix += f"HANG_SCRATCH={tmp}/hang\n"
            env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}")
            result = subprocess.run(["bash", "-c", prefix + calls_of + section], capture_output=True, text=True,
                                    env=env)
            return result.returncode, (result.stdout + result.stderr).replace(str(tmp), "TMP")

    def test_the_revert_passes_and_the_rework_with_the_unpark_patch_does_not(self):
        new = (self.GATE, self.CALLS)
        old = (self.RATE, self.CALLS + ("clk_set_rate",))
        ok = ("PCIe PIPE clock: vmlinux (build ID 111111111111) has the PIPE clock mux's gate operations and not "
              "its rate ones, and its qmp_pcie_enable() calls no clk_set_rate()")
        self.assertEqual(self.gate(*new), (0, ok + "\n"))
        self.assertEqual(self.gate(*new, hang=old),
                         (0, ok + "; the hang-log build's has the rate operations and calls it\n"))
        for name, args, message in (
                ("the rework", old, "no phy_mux_enable in "),
                ("a gate operation missing", (self.GATE[:2], self.CALLS), "no phy_mux_is_enabled in "),
                ("both kinds", (self.GATE + self.RATE[2:], self.CALLS),
                 "phy_mux_set_rate in TMP/build/vmlinux: the PIPE clock mux has the rework's rate operations"),
                ("the unpark patch", (self.GATE, self.CALLS + ("clk_set_rate",)),
                 f"qmp_pcie_enable() in TMP/build/vmlinux calls clk_set_rate(): {UNPARK}"),
                ("no such function", (self.GATE, ()), "no qmp_pcie_enable() calling clk_bulk_enable() in "),
                ("a hang-log build with the revert", (*new, new),
                 "self-test failed: the hang-log vmlinux does not have the rework's PIPE clock mux"),
                ("a hang-log build without the unpark patch", (*new, (self.RATE, self.CALLS)),
                 "self-test failed: the hang-log build's qmp_pcie_enable() does not call clk_set_rate() either")):
            with self.subTest(name):
                code, out = self.gate(*args[:2], hang=args[2] if len(args) > 2 else None)
                self.assertEqual(code, 1, out)
                self.assertIn(message, out)


class UfsPhyPowerCheckTest(unittest.TestCase):
    """ufs-phy-power-check.sh (revision 11), on a fake sysfs: read-only and
    without root, it samples the UFS host's runtime state and the load
    requested on the rail of the UFS PHY's vdda-phy supply, which it finds by
    that consumer's link (on the SM-X910 the rail may be another regulator),
    and says IMBALANCE when the PHY stays powered while its host is
    runtime-suspended."""

    # A stand-in for sleep: each call moves the fake sysfs on to the next
    # sample of samples.txt ("STATUS MICROAMPS" lines; the first is set
    # before the run).
    SLEEP = '''#!/bin/sh
n=$(cat "$D/n")
n=$((n + 1))
echo "$n" > "$D/n"
line=$(sed -n "$((n + 1))p" "$D/samples.txt")
[ -n "$line" ] || exit 0
set -- $line
echo "$1" > "$D/sys/devices/platform/soc@0/1d84000.ufshc/power/runtime_status"
echo "$2" > "$D/sys/class/regulator/$RAIL/requested_microamps"
'''

    def check(self, samples, rail="regulator.11", consumer=True, host=True):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            reg = d / "sys/class/regulator"
            for name, label, consumers in (("regulator.0", "regulator-dummy", ()),
                                           ("regulator.12", "vreg_l3e_1p2", ("1d80000.phy-vdda-pll",)),
                                           (rail, "vreg_l1e_0p88", ("1c06000.phy-vdda-phy", "ae94000.dsi-vdds")
                                            + (("1d80000.phy-vdda-phy",) if consumer else ()))):
                (reg / name).mkdir(parents=True)
                (reg / name / "name").write_text(label + "\n")
                (reg / name / "requested_microamps").write_text("0\n")
                for c in consumers:
                    (reg / name / c).symlink_to("../../../devices/platform/soc@0")
            status = d / "sys/devices/platform/soc@0/1d84000.ufshc/power/runtime_status"
            if host:
                status.parent.mkdir(parents=True)
                status.write_text(samples[0].split()[0] + "\n")
                (reg / rail / "requested_microamps").write_text(samples[0].split()[1] + "\n")
            (d / "samples.txt").write_text("".join(s + "\n" for s in samples))
            (d / "n").write_text("0\n")
            (d / "bin").mkdir()
            (d / "bin/sleep").write_text(self.SLEEP)
            (d / "bin/sleep").chmod(0o755)
            script = d / "check.sh"
            text = (KIT_SRC / "ufs-phy-power-check.sh").read_text()
            self.assertEqual(text.count("/sys/"), 2)
            script.write_text(text.replace("/sys/", f"{d}/sys/"))
            before = {str(p): p.read_bytes() for p in d.rglob("*") if p.is_file() and "regulator" in str(p)}
            result = subprocess.run(["sh", str(script), str(len(samples)), "0"], capture_output=True, text=True,
                                    env={"PATH": f"{d}/bin:/usr/bin:/bin", "D": str(d), "RAIL": rail}, timeout=60)
            # It wrote nothing (only the stand-in moved the loads on).
            after = {str(p): p.read_bytes() for p in d.rglob("*") if p.is_file() and "regulator" in str(p)}
            self.assertEqual(sorted(after), sorted(before))
            return result.returncode, result.stdout.replace(str(d), "D"), result.stderr.replace(str(d), "D")

    def test_a_phy_powered_while_its_host_is_suspended_is_an_imbalance(self):
        code, out, err = self.check(["suspended 286000"] * 8 + ["active 286000"] * 2)
        self.assertEqual((code, err), (1, ""))
        self.assertIn("UFS PHY vdda-phy: vreg_l1e_0p88 (D/sys/class/regulator/regulator.11), consumers: "
                      "1c06000.phy-vdda-phy 1d80000.phy-vdda-phy ae94000.dsi-vdds", out)
        self.assertIn("samples 10: host suspended 8 (PHY powered in 8), active 2 (PHY powered in 2), in transition 0",
                      out)
        self.assertIn("IMBALANCE: the UFS PHY stays powered while its host is runtime-suspended", out)

    def test_a_phy_that_powers_down_with_its_host_is_ok_and_one_stray_sample_is_tolerated(self):
        for name, samples in (("balanced", ["suspended 98000"] * 6 + ["active 286000"] * 4),
                              ("one stray sample", ["suspended 98000"] * 6 + ["suspended 188000"] + ["active 188000"]),
                              ("a resume between the two reads", ["suspended 0", "resuming 188000", "active 188000",
                                                                 "suspended 0"])):
            with self.subTest(name):
                code, out, err = self.check(samples)
                self.assertEqual((code, err), (0, ""), out)
                self.assertTrue(out.endswith("OK: the UFS PHY powers down with its host\n"), out)

    def test_the_rail_is_found_by_its_consumer_on_either_tablet(self):
        """The SM-X910's rail may be another regulator: found by the link."""
        code, out, _ = self.check(["suspended 286000"] * 4, rail="regulator.27")
        self.assertEqual(code, 1, out)
        self.assertIn("(D/sys/class/regulator/regulator.27)", out)

    def test_what_it_cannot_judge_it_says(self):
        code, out, _ = self.check(["active 286000"] * 4)
        self.assertEqual(code, 3)
        self.assertIn("INCONCLUSIVE: the UFS host was never runtime-suspended in the window", out)
        code, _, err = self.check(["suspended 0"], host=False)
        self.assertEqual((code, err), (2, "no UFS host at D/sys/devices/platform/soc@0/1d84000.ufshc\n"))
        code, _, err = self.check(["suspended 0"], consumer=False)
        self.assertEqual((code, err), (2, "no regulator with the consumer 1d80000.phy-vdda-phy\n"))

    def test_it_reads_and_writes_nothing_and_needs_no_root(self):
        text = (KIT_SRC / "ufs-phy-power-check.sh").read_text()
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        self.assertEqual(re.findall(r"(?<![0-9&<])>>?\s*\S+", code), [">&2"] * len(re.findall(">&2", code)))
        for word in ("sudo", "id -u", "tee ", "/sys/kernel/debug", "echo 1", "rm ", "mv ", "cp "):
            self.assertNotIn(word, code)
        self.assertIn("load=188000", code)
        self.assertIn('if [ -e "$r/$phy-vdda-phy" ]; then', code)


class ScriptsTest(unittest.TestCase):
    def test_scripts_parse(self):
        for name, shell in (("install.sh", "sh"), ("rollback.sh", "sh"), ("verify.sh", "sh"),
                            ("sleep-test.sh", "sh"), ("ufs-phy-power-check.sh", "sh"), ("make-kit.sh", "bash")):
            with self.subTest(name):
                subprocess.run([shell, "-n", str(KIT_SRC / name)], check=True)

    def test_a_filesystem_is_known_by_the_kernel_s_name_of_its_device(self):
        """sleep-test.sh reads /sys/fs/ext4/NAME and ext4's lines by the
        device's kernel name: a /dev/mapper or /dev/disk/by-* source in
        /proc/1/mounts is resolved to it."""
        body = re.search(r"^dev_name\(\) \{.*?^\}\n", (KIT_SRC / "sleep-test.sh").read_text(), re.M | re.S).group(0)
        with tempfile.TemporaryDirectory() as d:
            dev = pathlib.Path(d) / "dev"
            for sub in ("mapper", "disk/by-uuid"):
                (dev / sub).mkdir(parents=True)
            for name in ("dm-0", "sda34"):
                (dev / name).touch()
            (dev / "mapper/root").symlink_to("../dm-0")
            (dev / "disk/by-uuid/3c5f").symlink_to("../../sda34")
            out = subprocess.run(["sh", "-c", body + 'for s; do dev_name "$s"; done', "sh", str(dev / "mapper/root"),
                                  str(dev / "disk/by-uuid/3c5f"), str(dev / "sda34")],
                                 capture_output=True, text=True, check=True).stdout
        self.assertEqual(out, "dm-0\nsda34\nsda34\n")

    def test_scripts_pin_the_same_hang_log_kit(self):
        self.assertEqual(pinned("install.sh", "hang_boot"), pinned("make-kit.sh", "hang_boot"))
        self.assertEqual(pinned("install.sh", "hang_boot"), pinned("rollback.sh", "hang_boot"))
        self.assertEqual(pinned("install.sh", "hang_vendor_boot"),
                         pinned("make-kit.sh", "hang_vendor_boot"))
        self.assertEqual(pinned("install.sh", "hang_build_id"), HANG_BUILD_ID)
        self.assertEqual(pinned("make-kit.sh", "hang_build_id"), HANG_BUILD_ID)

    def test_install_knows_the_revisions_before_the_record(self):
        self.assertEqual(earlier_revisions(), EARLIER)
        install = (KIT_SRC / "install.sh").read_text()
        # One table, and the record that takes over from it.
        self.assertEqual(install.count(R7_BOOT), 1)
        self.assertIn('echo "$new_boot $new_id" >> "$backup/revisions"', install)

    def test_the_revisions_without_a_record_are_the_packed_kits(self):
        """Where the packed kits are (~/ubuntu-gts9u/backups on the build
        host): each whose install.sh keeps no record is in install.sh's
        table under its revision, with the boot.img and the BUILD_ID that
        its SHA256SUMS vouches for."""
        workdir = pathlib.Path(os.environ.get("UBUNTU_WORKDIR", str(pathlib.Path.home() / "ubuntu-gts9u")))
        kits = sorted(p.parent for p in (workdir / "backups").glob("typec-roles-kit*/SHA256SUMS"))
        if not kits:
            self.skipTest(f"no packed kits in {workdir / 'backups'}")
        table = {(boot, build_id): names.split(",") for boot, build_id, names in EARLIER}
        for kit in kits:
            with self.subTest(kit.name):
                sums = {name: digest for digest, name in
                        (line.split() for line in (kit / "SHA256SUMS").read_text().splitlines())}
                install = (kit / "install.sh").read_bytes()
                self.assertEqual(sha(install), sums["install.sh"])
                if b'"$backup/revisions"' in install:
                    continue
                build_id = (kit / "BUILD_ID").read_bytes()
                self.assertEqual(sha(build_id), sums["BUILD_ID"])
                revision = re.fullmatch(r"typec-roles-kit(?:-(r\d+))?", kit.name).group(1) or "r1"
                self.assertIn(revision, table[(sums["boot.img"], build_id.decode().strip())])

    def test_kit_ships_what_install_checks(self):
        text = (KIT_SRC / "make-kit.sh").read_text()
        shipped = re.search(r"^files=\((.*?)\)$", text, re.M | re.S).group(1).split()
        for name in ("boot.img", '"modules-$release.tar"', "BUILD_ID", "KEY_ID", "SIGNER",
                     "install.sh", "rollback.sh", "verify.sh"):
            self.assertIn(name, shipped)
        self.assertNotIn("vendor_boot.img", text.replace("$hangkit/vendor_boot.img", ""))
        # Every script and the README go into the kit, and each is in its
        # SHA256SUMS: revision 11's UFS PHY power check among them.
        copied = re.search(r'^install -m 0644 (.*?) "\$kit/"$', text, re.M | re.S).group(1).split()
        scripts = ["install.sh", "rollback.sh", "verify.sh", "sleep-test.sh", "ufs-phy-power-check.sh", "README.md"]
        self.assertEqual([w for w in copied if w != "\\"], [f'"$here/{name}"' for name in scripts])
        for name in scripts:
            self.assertIn(name, shipped)
            self.assertTrue((KIT_SRC / name).is_file(), name)

    def test_the_sleep_test_reads_revision_11_s_ufs_qcom_lines(self):
        """ufs_diag takes the ufs-qcom diagnostic's three kinds of line, as
        their format strings give them; an attempt given up before the sleep
        may have the counts at a power-up sequence's entry and the
        setup_clocks lines (healthy cycles have them) but not the line of a
        failed power-up sequence."""
        text = (KIT_SRC / "sleep-test.sh").read_text()
        diag = re.search(r"^ufs_diag='([^']*)'$", text, re.M).group(1)
        self.assertEqual(diag.split("|")[-3:], ["power-up sequence entry: PHY power_count",
                                                "power-up sequence failed, before phy_exit",
                                                "setup_clocks repeated a PHY power-"])
        for line in (UFS_COUNTS.format(count=1), UFS_COUNTS_FAILED, UFS_REPEAT,
                     UFS_REPEAT.replace("power-on (1)", "power-off (16)")):
            self.assertRegex(line, diag)
        # The lines are those the patch's format strings make.
        added = "\n".join(added_lines(UFS_QCOM_PATCHES[1])[f"{UFS_QCOM}.c"])
        self.assertIn('"%s: PHY power_count %d init_count %d, host reference %s, setup_clocks repeats: power-on %u, '
                      'power-off %u\\n",', added)
        self.assertIn('"power-up sequence entry"', added)
        self.assertIn('"power-up sequence failed, before phy_exit()"', added)
        self.assertIn('"setup_clocks repeated a PHY power-%s (%u): clock gating state %d, active_reqs %d, %s\\n",',
                      added)
        body = re.search(r"^gave_up_early\(\) \{\n.*?^\}\n", text, re.M | re.S).group(0)
        self.assertEqual(re.findall(r"-e '([^']*)'", body), ["calibration entry: SW_RESET", "power-up write: PWRDN",
                                                             "power-up sequence entry: PHY power_count",
                                                             "setup_clocks repeated a PHY power-"])
        # The NEXT line and the evidence name a section that is there.
        self.assertEqual(text.count('(docs/resume-recovery.md, \\"The PHY power reference\\")'), 2)
        self.assertIn("\n### The PHY power reference (2026-11-23)\n", (REPO / "docs/resume-recovery.md").read_text())
        self.assertIn("(resume-recovery.md#the-phy-power-reference-2026-11-23)", (REPO / "docs/porting-log.md").read_text())

    def test_tree_comparison_skips_build_products_and_nothing_else(self):
        """The M= module builds leave objects in the kernel source tree, and
        those differ with every build; a source file that differs, or one
        that only one tree has, still has to show."""
        text = (KIT_SRC / "make-kit.sh").read_text()
        options = re.search(r'^\tdiff -rq (.*?)\n\t\t"\$hang_tree" "\$tree"', text, re.M | re.S)
        self.assertIsNotNone(options)
        with tempfile.TemporaryDirectory() as tmp:
            old, new = pathlib.Path(tmp, "old"), pathlib.Path(tmp, "new")
            for tree, tag in ((old, "old"), (new, "new")):
                (tree / "net/netfilter").mkdir(parents=True)
                (tree / "drivers").mkdir()
                (tree / ".git").write_text("gitdir: %s\n" % tag)
                for product in ("nft_log.o", "nft_log.ko", ".nft_log.o.cmd", "nft_log.mod",
                                "nft_log.mod.c", "nft_log.mod.o", "modules.order",
                                "Module.symvers"):
                    (tree / "net/netfilter" / product).write_text(tag)
                (tree / "net/netfilter/nft_log.c").write_text("same\n")
                (tree / "drivers/tcpm.c").write_text(tag)
            (new / "drivers/extra.c").write_text("new\n")
            (new / "net/netfilter/extra.ko").write_text("new\n")
            flags = options.group(1).replace("\\\n", " ").rstrip("\\ ")
            result = subprocess.run(["bash", "-c", 'diff -rq %s "$1" "$2"' % flags,
                                     "diff", str(old), str(new)], capture_output=True, text=True)
        self.assertEqual(sorted(result.stdout.splitlines()), [
            "Files %s and %s differ" % (old / "drivers/tcpm.c", new / "drivers/tcpm.c"),
            "Only in %s: extra.c" % (new / "drivers"),
        ], result.stderr)

    def test_tree_comparison_passes_over_a_backup_patch_kept_and_no_other(self):
        """The build script's patch(1) keeps FILE.orig when a hunk goes in at
        an offset, as the dwc3-qcom guard does on this branch.  That backup
        is in the new tree alone and is the hang-log tree's file.  Anything
        else called .orig still has to show: one with other content, one of
        a file the kit's patches do not change, one both trees have and one
        that only the hang-log tree has."""
        text = (KIT_SRC / "make-kit.sh").read_text()
        block = re.search(r'^\tbackups=\n\tfor f in "\$\{patched\[@\]\}"; do\n.*?^\tdone\n',
                          text, re.M | re.S)
        self.assertIsNotNone(block)
        with tempfile.TemporaryDirectory() as tmp:
            old, new = pathlib.Path(tmp, "old"), pathlib.Path(tmp, "new")
            for tree, tag in ((old, "old"), (new, "new")):
                for name in ("usb/glue.c", "clk/gcc.c", "usb/tcpm.c", "include/tcpm.h",
                             "usb/other.c"):
                    (tree / name).parent.mkdir(parents=True, exist_ok=True)
                    (tree / name).write_text("%s of %s\n" % (name, tag))
            (old / "usb/tcpm.c.orig").write_text("an older backup\n")
            (new / "usb/tcpm.c.orig").write_text("usb/tcpm.c of old\n")
            (new / "usb/glue.c.orig").write_text("usb/glue.c of old\n")
            (new / "clk/gcc.c.orig").write_text("not the file of old\n")
            (new / "usb/other.c.orig").write_text("usb/other.c of old\n")
            (old / "include/tcpm.h.orig").write_text("include/tcpm.h of old\n")
            script = ('set -euo pipefail; hang_tree=$1 tree=$2 tmp=$3\n'
                      'patched=(usb/glue.c clk/gcc.c usb/tcpm.c include/tcpm.h)\n'
                      'diff -rq "$hang_tree" "$tree" > "$tmp/trees.diff" || :\n'
                      + block.group(0) +
                      'cat "$tmp/trees.diff"; echo "backups:$backups"\n')
            result = subprocess.run(["bash", "-c", script, "backups", str(old), str(new), tmp],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(result.stdout.splitlines()), sorted([
            "Files %s and %s differ" % (old / name, new / name)
            for name in ("usb/glue.c", "clk/gcc.c", "usb/tcpm.c", "include/tcpm.h",
                         "usb/other.c", "usb/tcpm.c.orig")
        ] + [
            "Only in %s: gcc.c.orig" % (new / "clk"),
            "Only in %s: other.c.orig" % (new / "usb"),
            "Only in %s: tcpm.h.orig" % (old / "include"),
            "backups: glue.c.orig",
        ]), result.stderr)

    def test_the_image_check_looks_for_what_only_this_revision_has(self):
        """The first three strings are also in an image built with the plain
        longer timeout before it, and the deadline of the Try.SNK wait is in
        the second revision of that patch alone.  The six after them came
        after it: the log lines of the three fixes for a swap asked for from
        userspace, of which the third has three (the PR_Swap one is in no
        earlier revision of that patch), and the driver's mark for reading
        its parameters at each attach.  Then revisions 8 to 11's."""
        text = (KIT_SRC / "make-kit.sh").read_text()
        strings = shlex.split(re.search(r"^new_strings=\((.*?)\)$", text, re.M | re.S).group(1))
        self.assertEqual(strings, ["Requesting DFP before discovery", "sink_host_swap",
                                   "try_snk_extra_wait_ms", "try_snk_wait_until",
                                   "VDM in progress, DATA_ROLE_SWAP not started",
                                   "Data role swap ended with the role unchanged",
                                   "Swap is still queued, left pending",
                                   "Partner swapped first, queued DR_SWAP_SEND dropped",
                                   "Partner swapped first, queued PR_SWAP_SEND dropped",
                                   "policy_open",
                                   "calibration entry", "after the PCS_READY timeout",
                                   "no PCS_READY within 1 s of SERDES_START",
                                   "gcc_sm8550.debug_pcie0_off", "gcc_sm8550.debug_pcie0_phy_off",
                                   "gcc_sm8550.debug_usb_off", "rpmhpd.debug_ret_floor",
                                   "failed to get tx_lane0_sync_clk", "failed to get rx_lane0_sync_clk",
                                   "failed to get rx_lane1_sync_clk", "power-up write: PWRDN",
                                   "gcc_sm8550.debug_boot_state",
                                   "system sleep: the link was kept, and its power domain stayed on",
                                   "system sleep: the link was kept, but genpd powered its power domain off",
                                   "pcie_qcom.debug_root_port_d3",
                                   "Failed to power on PHY: %pe", "power-up sequence entry",
                                   "setup_clocks repeats: power-on"])
        # Revision 11's ufs-qcom: the fix's message for a PHY power-on that
        # failed (a format string of its code), and two of the diagnostic's,
        # its entry line's label and its counts' format; each in no other
        # patch, and in no driver of this checkout.
        for line, patch, form in ((strings[25], UFS_QCOM_PATCHES[0], f'"{strings[25]}\\n"'),
                                  (strings[26], UFS_QCOM_PATCHES[1], f'"{strings[26]}"'),
                                  (strings[27], UFS_QCOM_PATCHES[1], f'setup_clocks repeats: power-on %u, power-off %u\\n"')):
            with self.subTest(line):
                self.assertEqual(len([added for added in added_lines(patch)[f"{UFS_QCOM}.c"] if form in added]), 1, form)
                self.assertEqual([p.name for p in (REPO / "kernel/patches").glob("*.patch") if line in p.read_text()],
                                 [patch])
                self.assertFalse([p.name for p in (REPO / "kernel/drivers").rglob("*") if p.is_file()
                                  and line in p.read_text(errors="replace")])
        # Revision 11's guard: two of the lines its PCIe half logs at a
        # resume, format strings of that patch's code and of no other patch.
        guard = added_lines(PCIE_PATCHES[0])[f"{PCIE_QCOM}.c"]
        for line in strings[22:24]:
            with self.subTest(line):
                self.assertEqual(len([added for added in guard if f'"{line}\\n"' in added]), 1, line)
                self.assertEqual([patch.name for patch in (REPO / "kernel/patches").glob("*.patch")
                                  if line in patch.read_text()], [PCIE_PATCHES[0]])
        # Revision 10's four: the messages of ufs-qcom's lane clock lookup,
        # in upstream f07317a8d57f's code and no other patch, and the second
        # UFS PHY diagnostic's format string.  The PIPE clock revert, the
        # unpark patch left out and the GMU fix leave no string; 5c and the
        # GMU's object date are for them.
        lanes = added_lines(FIX_PATCHES[2])[f"{UFS_QCOM}.c"]
        for line in strings[17:20]:
            with self.subTest(line):
                self.assertEqual(len([added for added in lanes if f'"{line}\\n"' in added]), 1, line)
                self.assertEqual([patch.name for patch in (REPO / "kernel/patches").glob("*.patch")
                                  if line in patch.read_text()], [FIX_PATCHES[2]])
        pwrdn = added_lines(UFS_PATCHES[2])[f"{QMP}.c"]
        self.assertTrue([added for added in pwrdn if f'"{strings[20]} %#x right after it' in added])
        self.assertEqual([patch.name for patch in (REPO / "kernel/patches").glob("*.patch")
                          if strings[20] in patch.read_text()], [UFS_PATCHES[2]])
        # Revision 8's three: string literals of the diagnostic's C code, in
        # no other patch's code, nor in the PCS reset (which leaves no
        # string).  Revision 10's second diagnostic names the calibration
        # entry in its prose alone.
        diag = added_lines(UFS_PATCHES[1])[f"{QMP}.c"]
        for line in strings[10:13]:
            with self.subTest(line):
                self.assertTrue([added for added in diag if f'"{line}' in added], line)
                self.assertEqual([patch.name for patch in (REPO / "kernel/patches").glob("*.patch")
                                  if any(f'"{line}' in added for lines in added_lines(patch.name).values()
                                         for added in lines)], [UFS_PATCHES[1]])
                self.assertEqual([patch.name for patch in (REPO / "kernel/patches").glob("*.patch")
                                  if line in patch.read_text()
                                  and patch.name not in (UFS_PATCHES[1], UFS_PATCHES[2])], [])
        # Revision 9's four: the names the kernel gives the debug switches'
        # parameters, built in (KBUILD_MODNAME, then the parameter's own),
        # each made by a module_param_cb() of its patch alone; and revision
        # 11's fifth, the same way.  Revision 11's pcie_0_gdsc leaves no
        # string (PcieGdscOffTest, and the power domain checks).
        for line, patch, target in ((strings[13], DEBUG_PATCHES[0], f"{GCC}.c"),
                                    (strings[14], DEBUG_PATCHES[0], f"{GCC}.c"),
                                    (strings[15], DEBUG_PATCHES[0], f"{GCC}.c"),
                                    (strings[16], DEBUG_PATCHES[1], f"{RPMHPD}.c"),
                                    (strings[21], DEBUG_PATCHES[0], f"{GCC}.c"),
                                    (strings[24], PCIE_PATCHES[1], f"{PCIE_QCOM}.c")):
            with self.subTest(line):
                module, name = line.split(".")
                self.assertEqual(module, pathlib.Path(target).stem.replace("-", "_"))
                calls = [added for added in added_lines(patch)[target] if added.startswith(f"module_param_cb({name}, ")]
                self.assertEqual(len(calls), 1, calls)
                self.assertFalse([other.name for other in (REPO / "kernel/patches").glob("*.patch")
                                  if other.name != patch and name in other.read_text()])
        tcpm_c = f"{TCPM_DIR}/tcpm.c"
        wait = added_lines("tcpm-try-snk-extra-wait.patch")
        # A member of a structure: its name is in the image's type information.
        self.assertIn("\tunsigned long try_snk_wait_until;", wait[tcpm_c])
        self.assertIn("static unsigned int tcpm_try_snk_wait_left(struct tcpm_port *port)",
                      wait[tcpm_c])
        driver = (REPO / "kernel/drivers/sm5714_usbpd.c").read_text()
        others = [(REPO / "kernel/patches" / name).read_text()
                  for name in ("tcpm-request-dfp-as-sink-before-discovery.patch",
                               "tcpm-no-dr-swap-during-vdm.patch",
                               "tcpm-dr-set-checks-data-role.patch",
                               "tcpm-keep-queued-swap-pending.patch",
                               "gcc-sm8550-usb-gdscs-retention.patch",
                               "dwc3-qcom-check-hcd-before-reading-port-speed.patch")]
        others.append(driver)
        for name in ("try_snk_wait_until", "tcpm_try_snk_wait_left"):
            with self.subTest(name):
                self.assertFalse([other[:40] for other in others if name in other])
        # The build tree is checked for the same revision of the patch.
        self.assertIn("grep -q 'tcpm_try_snk_wait_left' \"$tree/$tcpm_dir/tcpm.c\"", text)
        # Each of the five log lines is its own patch's, there once, and the
        # build tree is checked for it: a tree with an earlier revision of the
        # third fix has its first two lines.  The VCONN_Swap one is not
        # checked; it came with the PR_Swap one.
        for name, line in (("tcpm-no-dr-swap-during-vdm.patch", strings[4]),
                           ("tcpm-dr-set-checks-data-role.patch", strings[5]),
                           ("tcpm-keep-queued-swap-pending.patch", strings[6]),
                           ("tcpm-keep-queued-swap-pending.patch", strings[7]),
                           ("tcpm-keep-queued-swap-pending.patch", strings[8])):
            with self.subTest(line):
                calls = [added.strip() for added in added_lines(name)[tcpm_c] if line in added]
                self.assertEqual(calls, [f'tcpm_log(port, "{line}");'])
                self.assertEqual([patch.name for patch in (REPO / "kernel/patches").glob("*.patch")
                                  if line in patch.read_text()], [name])
                self.assertNotIn(line, driver)
                self.assertIn(f"grep -q '{line}' \"$tree/$tcpm_dir/tcpm.c\"", text)
        # A member of the driver's structure, and of no patch.
        self.assertIn("\tbool policy_open;\n", driver)
        self.assertFalse([patch.name for patch in (REPO / "kernel/patches").glob("*.patch")
                          if "policy_open" in patch.read_text()])

    def test_the_kit_s_patches_are_patches_the_build_applies(self):
        text = (KIT_SRC / "make-kit.sh").read_text()
        typec, gdsc, debug, fix, ufs, pcie, ufs_qcom = kit_patches(text)
        self.assertEqual((typec, gdsc, debug, fix, ufs, pcie, ufs_qcom),
                         (TYPEC_PATCHES, GDSC_PATCHES, DEBUG_PATCHES, FIX_PATCHES, UFS_PATCHES, PCIE_PATCHES,
                          UFS_QCOM_PATCHES))
        self.assertIn('\npatches=("${typec_patches[@]}" "${gdsc_patches[@]}" "${debug_patches[@]}" '
                      '"${fix_patches[@]}" "${ufs_patches[@]}"\n\t"${pcie_patches[@]}" "${ufs_qcom_patches[@]}")\n',
                      text)
        build = (REPO / "scripts/build-mainline-kernel.sh").read_text().replace("\\\n", " ")
        for name in typec:
            with self.subTest(name):
                self.assertTrue((REPO / "kernel/patches" / name).is_file())
                self.assertEqual(len(re.findall(
                    rf"^apply_unless '[^']+'\s+\S+\s+{re.escape(name)}\s*$", build, re.M)), 1)
        # The UFS PHY's: the PCS reset through the stage script, which the
        # build runs, and the diagnostic behind its switch.
        stage = (REPO / "scripts/stage-ufs-resume-experiment.sh").read_text()
        self.assertIn(f"patch_file=$repo/kernel/patches/{UFS_PATCHES[0]}\n", stage)
        self.assertEqual(build.count('bash "$repo/scripts/stage-ufs-resume-experiment.sh" "$kernel_tree"'), 1)
        self.assertEqual(build.count(f'< "$pat/{UFS_PATCHES[1]}"'), 2)
        # Revision 10's second diagnostic behind both variables, the same way.
        self.assertEqual(build.count(f'< "$pat/{UFS_PATCHES[2]}"'), 2)
        self.assertIn('if [ "$apply_qmp_ufs_diagnostic" = 1 ] && [ "$apply_sm8550_sleep_fixes" = 1 ]; then', build)
        # Revision 10's three upstream commits behind theirs, each by the file
        # it changes and the line it adds; the unpark patch applied only
        # without it.
        flat = re.sub(r"[ \t]+", " ", build)
        for name, target, _, line in FIXES:
            with self.subTest(name):
                self.assertTrue((REPO / "kernel/patches" / name).is_file())
                self.assertEqual(flat.count(f"sleep_fix {target} '{line}' {name}"), 1)
        self.assertEqual(build.count(f"drivers/phy/qualcomm/phy-qcom-qmp-pcie.c {UNPARK}"), 1)
        # Revision 11's behind its variable, by the line it gives
        # pcie_0_gdsc, which the kit knows it by too.
        self.assertEqual(gdsc, ["gcc-sm8550-pcie0-gdsc-off-in-suspend.patch"])
        self.assertIn(f"\npcie0_gdsc_patch={gdsc[0]}\n", build)
        self.assertIn('\npcie0_gdsc_line="PWRSTS_OFF_ON, /* $pcie0_gdsc_patch */"\n', build)
        self.assertEqual(build.count('< "$pat/$pcie0_gdsc_patch"'), 2)
        self.assertIn(f"\npcie0_line='{PCIE0_LINE.strip()[len('.pwrsts = '):]}'\n", text)
        self.assertIn(PCIE0_LINE, (REPO / "kernel/patches" / gdsc[0]).read_text().replace("\n+\t", "\n\t"))
        # The guard's genpd flag, by its line, in the same patch.
        self.assertIn(f"\nguard_line='{GUARD_FLAG_LINE[:-1]}'\n", text)
        self.assertIn("\n+" + GUARD_FLAG_LINE, (REPO / "kernel/patches" / gdsc[0]).read_text())
        # Revision 11's PCIe host patches behind theirs, each by the line
        # the build script and the kit know it by.
        self.assertIn(f"\npcie0_guard_patch={pcie[0]}\n", build)
        self.assertIn(f"\npcie0_guard_line='{GUARD_LINE}'\n", build)
        self.assertEqual(build.count('< "$pat/$pcie0_guard_patch"'), 2)
        self.assertIn(f"\npcie_rp_d3_patch={pcie[1]}\n", build)
        self.assertIn(f"\npcie_rp_d3_marker='{RPD3_MARKER}'\n", build)
        self.assertEqual(build.count('< "$pat/$pcie_rp_d3_patch"'), 2)
        self.assertIn(f"\npcie_lines=('{GUARD_LINE}'\n\t'{RPD3_MARKER}')\n", text)
        for name in pcie:
            with self.subTest(name):
                self.assertTrue((REPO / "kernel/patches" / name).is_file())
        # Revision 11's ufs-qcom two: the fix by apply_unless, in every
        # build, and its diagnostic behind the UFS PHY's diagnostic switch,
        # strictly, by the function its marker names.
        for name in ufs_qcom:
            with self.subTest(name):
                self.assertTrue((REPO / "kernel/patches" / name).is_file())
        self.assertEqual(len(re.findall(rf"^apply_unless '[^']+'\s+drivers/ufs/host/ufs-qcom\.c\s+"
                                        rf"{re.escape(ufs_qcom[0])}\s*$", build, re.M)), 1)
        self.assertEqual(build.count(f'< "$pat/{ufs_qcom[1]}"'), 2)
        self.assertIn("\nufs_qcom_count_marker='static void ufs_qcom_dbg_phy_counts('\n", build)
        # Revision 9's two behind their switch, each by the file it changes.
        for name, target in zip(debug, (f"{GCC}.c", f"{RPMHPD}.c")):
            with self.subTest(name):
                self.assertTrue((REPO / "kernel/patches" / name).is_file())
                self.assertEqual(build.count(f"{target}:{name}"), 1)
        # Continuation lines joined: revision 12's third is the loop's last.
        self.assertIn("\tdrivers/pmdomain/qcom/rpmhpd.c:rpmhpd-debug-retention-floor.patch  "
                      "\tdrivers/soc/qcom/qcom_stats.c:qcom-stats-cxpc-violators-diagnostic.patch; do\n", build)
        # Each file a patch changes is one the kit compares with the hang-log
        # build's, and so is the PCIe PHY's, which loses the unpark patch.
        patched = re.search(r"^patched=\((.*?)\)$", text, re.M | re.S).group(1)
        changed = set()
        for name in typec + gdsc + debug + fix + ufs + ufs_qcom:
            changed.update(re.findall(r"^\+\+\+ b/(\S+)", (REPO / "kernel/patches" / name).read_text(),
                                      re.M))
        self.assertEqual(changed, {f"{TCPM_DIR}/tcpm.c", "include/linux/usb/tcpm.h", f"{GCC}.c",
                                   f"{DWC3}.c", f"{QMP}.c", f"{RPMHPD}.c", f"{PHYMUX}.c", f"{GMU}.c",
                                   f"{UFS_QCOM}.c", f"{UFS_QCOM}.h"})
        self.assertEqual(re.findall(r"^\+\+\+ b/(\S+)", (REPO / "kernel/patches" / UNPARK).read_text(), re.M),
                         [f"{QMP_PCIE}.c"])
        self.assertEqual(" ".join(patched.split()),
                         '"$tcpm_dir/tcpm.c" include/linux/usb/tcpm.h "$gcc.c" "$dwc3.c" "$qmp.c" '
                         '"$rpmhpd.c" "$phymux.c" "$qmp_pcie.c" "$gmu.c" "$ufs_qcom.c" "$ufs_qcom.h" "$pcie_qcom.c"')
        self.assertIn("\ntcpm_dir=drivers/usb/typec/tcpm\n", text)
        self.assertIn("\ngcc=drivers/clk/qcom/gcc-sm8550\n", text)
        self.assertIn("\ndwc3=drivers/usb/dwc3/dwc3-qcom\n", text)
        self.assertIn(f"\nqmp={QMP}\n", text)
        self.assertIn(f"\nrpmhpd={RPMHPD}\n", text)
        for name, value in (("phymux", PHYMUX), ("qmp_pcie", QMP_PCIE), ("gmu", GMU), ("ufs_qcom", UFS_QCOM),
                            ("pcie_qcom", PCIE_QCOM)):
            self.assertIn(f"\n{name}={value}\n", text)
        self.assertIn(f"\nunpark={UNPARK}\n", text)
        self.assertIn('fix_files=("$phymux.c" "$gmu.c" "$ufs_qcom.c")\n', text)
        self.assertIn("\nrevision=11\n", text)
        # The build tree has to carry each fix whole and not the unpark
        # patch; the hang-log tree none of the fixes and the unpark patch.
        self.assertIn('die "the build tree\'s $f does not carry ${fix_patches[$i]} whole"', text)
        self.assertIn('die "the build tree\'s $qmp_pcie.c carries $unpark, which revision 10 leaves out"', text)
        self.assertIn("die 'the hang-log tree carries one of the SM8550 sleep fixes already'", text)
        self.assertIn('die "the hang-log tree\'s $qmp_pcie.c does not carry $unpark"', text)
        # The build tree has to carry both whole, and the hang-log tree
        # neither; with the switches off the RPMh tables are the hang-log
        # build's; the module tree and vmlinux show the switches.
        self.assertIn('debug_files=("$gcc.c" "$rpmhpd.c")\n', text)
        self.assertIn('die "the build tree\'s $f does not carry ${debug_patches[$i]} whole"', text)
        self.assertIn("die 'the hang-log tree carries the deep-sleep debug switches already'", text)
        self.assertIn('cmp -s "$tmp/rpmhpd.hang.data" "$tmp/rpmhpd.new.data" ||', text)
        for parm in ("gcc_sm8550.parm=debug_pcie0_off:", "gcc_sm8550.parm=debug_pcie0_phy_off:",
                     "gcc_sm8550.parm=debug_usb_off:", "rpmhpd.parm=debug_ret_floor:",
                     "gcc_sm8550.parm=debug_boot_state:", "pcie_qcom.parm=debug_root_port_d3:"):
            self.assertIn(parm, text)
        self.assertIn("for f in gcc_sm8550_debug_set gcc_sm8550_debug_get gcc_sm8550_debug_boot_get "
                      "rpmhpd_debug_ret_floor_set \\\n\tqcom_pcie_debug_rp_d3_set qcom_pcie_pd_notify; do", text)
        # Each of those functions is the debug patch's, and revision 11's
        # two the PCIe host patches': the root port switch's handler and the
        # guard record's notifier.
        for f in ("gcc_sm8550_debug_set", "gcc_sm8550_debug_get", "gcc_sm8550_debug_boot_get"):
            self.assertTrue([a for a in added_lines(DEBUG_PATCHES[0])[f"{GCC}.c"] if a.startswith(f"static int {f}(")],
                            f)
        for f, patch in (("qcom_pcie_debug_rp_d3_set", PCIE_PATCHES[1]), ("qcom_pcie_pd_notify", PCIE_PATCHES[0])):
            self.assertTrue([a for a in added_lines(patch)[f"{PCIE_QCOM}.c"] if a.startswith(f"static int {f}(")], f)
        # The build tree has to carry revision 11's line whole, its PCIe
        # neighbours as upstream has them; the hang-log tree has not.
        self.assertIn('die "the build tree does not carry ${gdsc_patches[0]} (pcie_0_gdsc)"', text)
        self.assertIn('die "the build tree\'s $gcc.c does not carry ${gdsc_patches[$i]} whole"', text)
        self.assertIn('die "the hang-log tree\'s pcie_0_gdsc is not upstream\'s PWRSTS_RET_ON"', text)
        self.assertIn('for p in "${gdsc_patches[@]}" "${debug_patches[@]}" "${pcie_patches[@]}"; do', text)

    def test_every_changed_source_is_required_dated_and_compared(self):
        """The kit's changes to the kernel tree are the patched files and the
        driver.  Each source is required, its object is dated against it,
        and with HANG_SCRATCH each is what may differ from the hang-log tree.
        The dwc3-qcom guard adds no string to the image: the marker in the
        build tree's source and the date of its object are its proof, and so
        for revision 10's GMU fix."""
        text = (KIT_SRC / "make-kit.sh").read_text()
        names = {"tcpm_dir": TCPM_DIR, "gcc": GCC, "dwc3": DWC3, "qmp": QMP, "rpmhpd": RPMHPD,
                 "phymux": PHYMUX, "qmp_pcie": QMP_PCIE, "gmu": GMU, "ufs_qcom": UFS_QCOM, "pcie_qcom": PCIE_QCOM}

        def expand(word):
            return re.sub(r"\$\{?(\w+)\}?", lambda match: names[match.group(1)], word)

        patched = [expand(word) for word in shlex.split(
            re.search(r"^patched=\((.*?)\)$", text, re.M | re.S).group(1))]
        driver = f"{TCPM_DIR}/sm5714_usbpd.c"
        want = re.search(r'^\tsort > "\$tmp/trees\.want" <<EOF\n(.*?)^EOF$', text, re.M | re.S)
        self.assertEqual(sorted(expand(word) for word in want.group(1).split()),
                         sorted(patched + [driver]))
        objects = [expand(pair) for pair in shlex.split(
            re.search(r"^objects=\((.*?)\)$", text, re.M | re.S).group(1))]
        required = text[text.index('for f in "$mkbootimg"'):]
        required = required[:required.index("; do\n")]
        self.assertIn('"${patched[@]/#/$tree/}"', required)
        for source in patched + [driver]:
            if source.endswith(".c"):
                with self.subTest(source):
                    self.assertIn(f"{source[:-2]}.o:{source}", objects)
                    variable = next(f"${name}" for name, value in names.items()
                                    if source.startswith(value))
                    obj = source[:-2].replace(names[variable[1:]], variable) + ".o"
                    self.assertIn(f'"$build/{obj}"', required)
        self.assertIn("grep -q 'no hcd and no root hub' \"$tree/$dwc3.c\" ||\n"
                      "\tdie 'the build tree does not carry the dwc3-qcom guard'", text)
        # ufs-qcom.h is dated against ufs-qcom.o, which includes it.
        self.assertIn(f"{UFS_QCOM}.o:{UFS_QCOM}.h", objects)
        # The guard's proof stays its marker and the date of its object; the
        # one disassembler is calls_of(), for the UFS PHY's calibration
        # (revision 8) and the PCIe PHY's power-on (revision 10).
        self.assertEqual(text.count("llvm-objdump -d"), 1)
        self.assertIn('llvm-objdump -d --no-show-raw-insn --disassemble-symbols="$2" "$1"', text)
        self.assertEqual(re.findall(r'calls_of "\$build/vmlinux" (\w+)', text),
                         ["qmp_ufs_phy_calibrate", "qmp_pcie_enable"])

    def test_the_hang_log_tree_is_a_reference_for_the_guard_and_the_power_domains(self):
        """The hang-log build has this branch's opps patch in dwc3-qcom.c and
        neither of the two patches this kit adds there and in gcc-sm8550.c,
        and its compiled clock controller is the reference for the power
        domain table, as the release build's is for the SM-X910 kit."""
        text = (KIT_SRC / "make-kit.sh").read_text()
        self.assertIn("grep -q 'dwc3_qcom_set_pstate' \"$hang_tree/$dwc3.c\" ||\n\t\tdie ", text)
        self.assertIn("! grep -q 'no hcd and no root hub' \"$hang_tree/$dwc3.c\" &&\n"
                      "\t\t! grep -q 'Retention across system suspend, as on sm8650' "
                      "\"$hang_tree/$gcc.c\" ||\n\t\tdie ", text)
        build = (REPO / "scripts/build-mainline-kernel.sh").read_text()
        self.assertIn("apply_unless 'dwc3_qcom_set_pstate' \\\n\tdrivers/usb/dwc3/dwc3-qcom.c \\\n"
                      "\tdwc3-qcom-drop-required-opps-vote-in-system-suspend.patch\n", build)
        self.assertIn('\tllvm-objcopy -O binary --only-section=.data "$1" "$tmp/gcc.hang.data"\n', text)
        self.assertIn('\tgdsc_table_check "$HANG_SCRATCH/linux-gts9wifi/$gcc.o" "$build/$gcc.o" || exit 1\n', text)
        # The four bytes, each named by its domain and member (pahole).
        self.assertIn('\tprintf \'%s\\n\' "pcie_0_gdsc $flags_at 0 10" "pcie_0_gdsc $pwrsts_at 6 5" '
                      '"usb30_prim_gdsc $pwrsts_at 5 6" \\\n\t\t"usb3_phy_gdsc $pwrsts_at 5 6" | sort > '
                      '"$tmp/gcc.data.want"\n', text)
        self.assertIn('\tflags_at=$((pd_at + flags_in))\n', text)
        # The hang-log tree has neither half of revision 11's guard.
        self.assertIn('\t! grep -qF -- GENPD_FLAG_ACTIVE_WAKEUP "$hang_tree/$gcc.c" &&\n'
                      '\t\t! grep -qF -e "${pcie_lines[0]}" -e "${pcie_lines[1]}" "$hang_tree/$pcie_qcom.c" ||\n', text)
        self.assertIn('\techo "power domain table: $gdsc_check"\n', text)
        tools = re.search(r"^for t in (.*); do$", text, re.M).group(1).split()
        self.assertIn("llvm-objcopy", tools)
        self.assertIn("pahole", tools)
        # Only for the UFS PHY's board gate (UfsGateTest).
        self.assertIn("llvm-objdump", tools)


    def power_domain_gate(self, source):
        """Run make-kit.sh's check for the USB power domain patch on SOURCE."""
        text = (KIT_SRC / "make-kit.sh").read_text()
        gate = re.search(r"^gdsc_pwrsts\(\) \{.*?^grep -q 'Retention across system suspend[^\n]*\n"
                         r"\tdie 'the build tree does not carry the USB power domain patch'\n",
                         text, re.M | re.S)
        self.assertIsNotNone(gate)
        with tempfile.TemporaryDirectory() as tmp:
            pathlib.Path(tmp, "gcc-sm8550.c").write_text(source)
            return subprocess.run(
                ["bash", "-c", 'set -euo pipefail; export LC_ALL=C\n'
                 'die() { echo "$*" >&2; exit 1; }\ntree=$1 gcc=gcc-sm8550\n' + gate.group(0),
                 "gate", tmp], capture_output=True, text=True)

    def test_the_power_domain_check_reads_the_two_usb_domains(self):
        """Upstream already has PWRSTS_RET_ON on the four PCIe domains, so the
        patched file has six of them: counting the lines would refuse the
        patched tree.  The same check as the SM-X910 kit's."""
        def block(name, pwrsts):
            return ('static struct gdsc %s = {\n\t.gdscr = 0x39004,\n\t.pd = {\n'
                    '\t\t.name = "%s",\n\t},\n\t.pwrsts = %s,\n'
                    '\t.flags = POLL_CFG_GDSCR | RETAIN_FF_ENABLE,\n};\n\n' % (name, name, pwrsts))

        def source(usb30, usb3_phy, marker):
            return ("".join(block(name, "PWRSTS_RET_ON") for name in
                            ("pcie_0_gdsc", "pcie_0_phy_gdsc", "pcie_1_gdsc", "pcie_1_phy_gdsc"))
                    + block("ufs_phy_gdsc", "PWRSTS_OFF_ON")
                    + ("/* Retention across system suspend, as on sm8650: see "
                       "gcc-sm8550-usb-gdscs-retention.patch */\n" if marker else "")
                    + block("usb30_prim_gdsc", usb30) + block("usb3_phy_gdsc", usb3_phy)
                    # What follows a domain's block is not read as its state.
                    + block("usb3_phy_gdsc_2", "PWRSTS_RET_ON"))

        off, ret = "PWRSTS_OFF_ON", "PWRSTS_RET_ON"
        patched = self.power_domain_gate(source(ret, ret, True))
        self.assertEqual((patched.returncode, patched.stdout, patched.stderr), (0, "", ""))
        refused = "the build tree does not carry the USB power domain patch"
        for name, text, message in (
                ("upstream", source(off, off, False), refused + " (usb30_prim_gdsc)"),
                ("the controller's only", source(ret, off, True), refused + " (usb3_phy_gdsc)"),
                ("the PHY's only", source(off, ret, True), refused + " (usb30_prim_gdsc)"),
                ("no marker", source(ret, ret, False), refused),
                ("no such domain", "", refused + " (usb30_prim_gdsc)")):
            with self.subTest(name):
                result = self.power_domain_gate(text)
                self.assertEqual((result.returncode, result.stderr), (1, message + "\n"))

    @unittest.skipUnless(kernel_source(), "no pinned kernel checkout")
    def test_the_power_domain_check_passes_on_the_patched_kernel_file_only(self):
        path = "drivers/clk/qcom/gcc-sm8550.c"
        upstream = subprocess.run(["git", "-C", str(kernel_source()), "show", "HEAD:" + path],
                                  check=True, capture_output=True, text=True).stdout
        # The reason the check cannot count lines.
        self.assertGreater(upstream.count("PWRSTS_RET_ON"), 0)
        self.assertEqual(self.power_domain_gate(upstream).returncode, 1)
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp, path)
            target.parent.mkdir(parents=True)
            target.write_text(upstream)
            with open(REPO / "kernel/patches/gcc-sm8550-usb-gdscs-retention.patch", "rb") as stream:
                applied = subprocess.run(["patch", "-d", tmp, "-p1", "--no-backup-if-mismatch"],
                                         stdin=stream, check=True, capture_output=True, text=True)
            self.assertNotRegex(applied.stdout, r"fuzz|offset")
            result = self.power_domain_gate(target.read_text())
        self.assertEqual((result.returncode, result.stderr), (0, ""))

    def pcie0_gate(self, source):
        """Run make-kit.sh's check of the build tree for revision 11's
        pcie_0_gdsc on SOURCE, a gcc-sm8550.c."""
        text = (KIT_SRC / "make-kit.sh").read_text()
        pwrsts = re.search(r"^gdsc_pwrsts\(\) \{.*?^\}\n", text, re.M | re.S).group(0)
        start = text.index("# Revision 11: pcie_0_gdsc PWRSTS_OFF_ON by the line of its patch")
        last = '\t\tdie "the build tree\'s $gcc.c does not carry ${gdsc_patches[$i]} whole"\ndone\n'
        gate = text[start:text.index(last, start)] + last
        lists = re.search(r"^gdsc_patches=\(.*?\)\n", text, re.M).group(0) + \
            re.search(r"^pcie0_line=.*\n", text, re.M).group(0) + re.search(r"^guard_line=.*\n", text, re.M).group(0)
        with tempfile.TemporaryDirectory() as tmp:
            tree = pathlib.Path(tmp, "tree")
            (tree / GCC).parent.mkdir(parents=True)
            (tree / f"{GCC}.c").write_text(source)
            pathlib.Path(tmp, "tmp").mkdir()
            result = subprocess.run(
                ["bash", "-c", 'set -euo pipefail; export LC_ALL=C\n'
                 'die() { echo "$*" >&2; exit 1; }\ntree=$1 tmp=$2 repo=$3 gcc=' + GCC + "\n" + lists + pwrsts + gate,
                 "gate", str(tree), str(pathlib.Path(tmp, "tmp")), str(REPO)], capture_output=True, text=True)
            # The tree is only read.
            self.assertEqual((tree / f"{GCC}.c").read_text(), source)
            return result.returncode, result.stderr

    @unittest.skipUnless(kernel_source(), "no pinned kernel checkout")
    def test_the_pcie0_check_passes_on_the_file_with_revision_11_s_patch_whole_only(self):
        upstream = subprocess.run(["git", "-C", str(kernel_source()), "show", f"HEAD:{GCC}.c"],
                                  check=True, capture_output=True, text=True).stdout
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp, f"{GCC}.c")
            target.parent.mkdir(parents=True)
            target.write_text(upstream)
            for name in ("gcc-sm8550-usb-gdscs-retention.patch", GDSC_PATCHES[0]):
                subprocess.run(["patch", "-s", "-d", tmp, "-p1", "--fuzz=0", "--no-backup-if-mismatch"],
                               input=(REPO / "kernel/patches" / name).read_text(), check=True, text=True)
                if name != GDSC_PATCHES[0]:
                    usb = target.read_text()
            patched = target.read_text()
        self.assertEqual(self.pcie0_gate(patched), (0, ""))
        missing = "the build tree does not carry gcc-sm8550-pcie0-gdsc-off-in-suspend.patch (pcie_0_gdsc)\n"
        for name, source, message in (
                ("upstream", upstream, missing),
                ("revision 10", usb, missing),
                # The state without the patch's line, and the line elsewhere.
                ("the state alone", usb.replace('"pcie_0_gdsc",\n\t},\n\t.pwrsts = PWRSTS_RET_ON,',
                                               '"pcie_0_gdsc",\n\t},\n\t.pwrsts = PWRSTS_OFF_ON,'), missing),
                ("the line on another domain", usb.replace(
                    '"pcie_0_phy_gdsc",\n\t},\n\t.pwrsts = PWRSTS_RET_ON,',
                    '"pcie_0_phy_gdsc",\n\t},\n' + PCIE0_LINE[:-1]), missing),
                ("its PHY's domain off as well", patched.replace(
                    '"pcie_0_phy_gdsc",\n\t},\n\t.pwrsts = PWRSTS_RET_ON,',
                    '"pcie_0_phy_gdsc",\n\t},\n\t.pwrsts = PWRSTS_OFF_ON,'),
                 "the build tree's pcie_0_phy_gdsc is not PWRSTS_RET_ON\n"),
                ("PCIe1's off", patched.replace('"pcie_1_gdsc",\n\t},\n\t.pwrsts = PWRSTS_RET_ON,',
                                                '"pcie_1_gdsc",\n\t},\n\t.pwrsts = PWRSTS_OFF_ON,'),
                 "the build tree's pcie_1_gdsc is not PWRSTS_RET_ON\n"),
                ("the line without the patch's context", patched.replace(
                    PCIE0_LINE + "\t.flags = VOTABLE | POLL_CFG_GDSCR | RETAIN_FF_ENABLE,\n",
                    PCIE0_LINE + "\t.flags = VOTABLE | POLL_CFG_GDSCR,\n"),
                 "the build tree's drivers/clk/qcom/gcc-sm8550.c does not carry "
                 "gcc-sm8550-pcie0-gdsc-off-in-suspend.patch whole\n"),
                # The patch's first form (revision 11 as first prepared), and
                # the guard's flag on another domain as well.
                ("without the genpd flag", patched.replace(GUARD_FLAG_LINE, ""),
                 "the build tree does not carry gcc-sm8550-pcie0-gdsc-off-in-suspend.patch whole "
                 "(GENPD_FLAG_ACTIVE_WAKEUP for pcie_0_gdsc, and nowhere else)\n"),
                ("the flag on PCIe1 too", patched.replace('"pcie_1_gdsc",\n\t},', '"pcie_1_gdsc",\n'
                                                          + GUARD_FLAG_LINE + '\t},'),
                 "the build tree does not carry gcc-sm8550-pcie0-gdsc-off-in-suspend.patch whole "
                 "(GENPD_FLAG_ACTIVE_WAKEUP for pcie_0_gdsc, and nowhere else)\n")):
            with self.subTest(name):
                self.assertNotEqual(source, patched)
                code, err = self.pcie0_gate(source)
                self.assertEqual(code, 1, err)
                self.assertTrue(err.endswith(message), err)

    def pcie_gate(self, source):
        """Run make-kit.sh's check of the build tree for revision 11's two
        PCIe host patches on SOURCE, a pcie-qcom.c."""
        text = (KIT_SRC / "make-kit.sh").read_text()
        start = text.index("# The PCIe host driver: the guard's half and the root port's debug switch,")
        end = text.index('\t\tdie "the build tree\'s $pcie_qcom.c does not carry ${pcie_patches[$i]} whole"\ndone\n', start)
        gate = text[start:end] + '\t\tdie "the build tree\'s $pcie_qcom.c does not carry ${pcie_patches[$i]} whole"\ndone\n'
        lists = re.search(r"^pcie_patches=\(.*?\)\n", text, re.M).group(0) + \
            re.search(r"^pcie_lines=\(.*?\)\n", text, re.M | re.S).group(0)
        with tempfile.TemporaryDirectory() as tmp:
            tree = pathlib.Path(tmp, "tree")
            (tree / PCIE_QCOM).parent.mkdir(parents=True)
            (tree / f"{PCIE_QCOM}.c").write_text(source)
            pathlib.Path(tmp, "tmp").mkdir()
            result = subprocess.run(
                ["bash", "-c", 'set -euo pipefail; export LC_ALL=C\n'
                 'die() { echo "$*" >&2; exit 1; }\ntree=$1 tmp=$2 repo=$3 pcie_qcom=' + PCIE_QCOM + "\n" + lists + gate,
                 "gate", str(tree), str(pathlib.Path(tmp, "tmp")), str(REPO)], capture_output=True, text=True)
            self.assertEqual((tree / f"{PCIE_QCOM}.c").read_text(), source)
            return result.returncode, result.stderr

    @unittest.skipUnless(kernel_source(), "no pinned kernel checkout")
    def test_the_pcie_host_check_passes_on_the_file_with_both_patches_whole_only(self):
        upstream = subprocess.run(["git", "-C", str(kernel_source()), "show", f"HEAD:{PCIE_QCOM}.c"],
                                  check=True, capture_output=True, text=True).stdout
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp, f"{PCIE_QCOM}.c")
            target.parent.mkdir(parents=True)
            target.write_text(upstream)
            for name in PCIE_PATCHES:
                subprocess.run(["patch", "-s", "-d", tmp, "-p1", "--fuzz=0", "--no-backup-if-mismatch"],
                               input=(REPO / "kernel/patches" / name).read_text(), check=True, text=True)
                if name == PCIE_PATCHES[0]:
                    guard = target.read_text()
            both = target.read_text()
        self.assertEqual(self.pcie_gate(both), (0, ""))
        for name, source, message in (
                ("upstream", upstream, f"the build tree does not carry {PCIE_PATCHES[0]}\n"),
                ("the guard alone", guard, f"the build tree does not carry {PCIE_PATCHES[1]}\n"),
                ("the guard's line without the rest", both.replace("\tWRITE_ONCE(pcie->pd_off, false);\n", ""),
                 f"the build tree's {PCIE_QCOM}.c does not carry {PCIE_PATCHES[0]} whole\n"),
                ("the switch's marker without the switch", both.replace("\t\tif (pci_bridge_d3_possible(pdev))\n"
                                                                       "\t\t\tcontinue;\n", ""),
                 f"the build tree's {PCIE_QCOM}.c does not carry {PCIE_PATCHES[1]} whole\n")):
            with self.subTest(name):
                self.assertNotEqual(source, both)
                code, err = self.pcie_gate(source)
                self.assertEqual(code, 1, err)
                self.assertTrue(err.endswith(message), err)

    # pahole -C STRUCT OBJECT, as it prints a member: struct gdsc's pd at 0
    # and pwrsts at 0x28 (40), struct generic_pm_domain's flags at 8.
    PAHOLE = ('#!/bin/sh\ncase $2 in\n'
              'gdsc) printf \'struct gdsc {\\n\\tstruct generic_pm_domain   pd;                   /*     0    32 */\\n'
              '\\tu8                         pwrsts;               /*    40     1 */\\n};\\n\' ;;\n'
              'generic_pm_domain) printf \'struct generic_pm_domain {\\n'
              '\\tunsigned int               flags;                /*     8     4 */\\n};\\n\' ;;\n'
              'esac\n')

    def table_check(self, hang, new, new_syms, pahole=PAHOLE):
        """Run make-kit.sh's gdsc_table_check() on stand-ins: HANG and NEW are
        the .data sections (bytes) llvm-objcopy gives, NEW_SYMS the symbol
        table lines llvm-objdump -t gives for the new object, PAHOLE the
        stand-in for pahole, which gives the members' offsets."""
        text = (KIT_SRC / "make-kit.sh").read_text()
        check = (re.search(r"^member_offset\(\) \{.*?^\}\n", text, re.M | re.S).group(0) +
                 re.search(r"^gdsc_table_check\(\) \{.*?^\}\n", text, re.M | re.S).group(0))
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            bin_ = tmp / "bin"
            bin_.mkdir()
            # llvm-objcopy -O binary --only-section=.data IN OUT; llvm-objdump -t IN
            (bin_ / "llvm-objcopy").write_text('#!/bin/sh\nfor last; do :; done\nin=$(eval echo \\${$(($# - 1))})\n'
                                               'cp "$in.data" "$last"\n')
            (bin_ / "llvm-objdump").write_text('#!/bin/sh\nfor last; do :; done\ncat "$last.syms"\n')
            (bin_ / "pahole").write_text(pahole)
            for stub in bin_.iterdir():
                stub.chmod(0o755)
            (tmp / "hang.o.data").write_bytes(hang)
            (tmp / "new.o.data").write_bytes(new)
            (tmp / "new.o.syms").write_text(new_syms)
            (tmp / "hang.o").write_text("ELF\n")
            (tmp / "new.o").write_text("ELF\n")
            env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}")
            result = subprocess.run(["bash", "-c", f"set -euo pipefail; export LC_ALL=C\ntmp={tmp}/w gcc={GCC}\n"
                                     f"mkdir -p {tmp}/w\n" + check + 'gdsc_table_check "$1" "$2"\n',
                                     "check", str(tmp / "hang.o"), str(tmp / "new.o")],
                                    capture_output=True, text=True, env=env)
            return result.returncode, result.stderr.replace(str(tmp), "TMP")

    def test_the_power_domain_table_check_names_each_changed_byte_by_its_domain(self):
        """Three domains of 0x40 bytes in .data at 0x100, 0x180 and 0x200,
        .pwrsts at 0x28 of each and the genpd flags at 8, as llvm-objdump -t
        lists an object's symbols (with a function and a .rodata entry to
        pass over) and pahole the members."""
        syms = ("\nnew.o:\tfile format elf64-littleaarch64\n\nSYMBOL TABLE:\n"
                "0000000000000000 l    df *ABS*\t0000000000000000 gcc-sm8550.c\n"
                "0000000000000010 l     F .text\t0000000000000020 gcc_sm8550_probe\n"
                "0000000000000100 l     O .data\t0000000000000040 pcie_0_gdsc\n"
                "0000000000000180 l     O .data\t0000000000000040 usb30_prim_gdsc\n"
                "0000000000000200 l     O .data\t0000000000000040 usb3_phy_gdsc\n"
                "0000000000000300 l     O .rodata\t0000000000000010 gcc_sm8550_desc\n")
        hang = bytearray(0x300)
        for at, state in ((0x128, 6), (0x1a8, 5), (0x228, 5)):
            hang[at] = state
        def new(changes):
            data = bytearray(hang)
            for at, value in changes.items():
                data[at] = value
            return bytes(data)
        # The two USB domains' and pcie_0_gdsc's .pwrsts, and pcie_0_gdsc's
        # genpd flags (GENPD_FLAG_ACTIVE_WAKEUP, 8: 010 in cmp's octal).
        r11 = {0x128: 5, 0x1a8: 6, 0x228: 6, 0x108: 8}
        self.assertEqual(self.table_check(bytes(hang), new(r11), syms), (0, ""))
        refused = ("the compiled drivers/clk/qcom/gcc-sm8550 table does not differ from the hang-log build's in the "
                   "four bytes alone (pcie_0_gdsc's .pd.flags at 8 0 to 8, and the .pwrsts at 40 of pcie_0_gdsc 6 to "
                   "5 and of the two USB domains 5 to 6; symbol, byte in it, old, new, octal):\n")
        for name, changes, named in (
                ("revision 10's", {0x1a8: 6, 0x228: 6}, "usb30_prim_gdsc 40 5 6\nusb3_phy_gdsc 40 5 6\n"),
                ("revision 11 as first prepared, without the flag", {0x128: 5, 0x1a8: 6, 0x228: 6},
                 "pcie_0_gdsc 40 6 5\nusb30_prim_gdsc 40 5 6\n"),
                ("the flag on another domain", {0x128: 5, 0x1a8: 6, 0x228: 6, 0x188: 8}, "usb30_prim_gdsc 8 0 10\n"),
                ("a fifth byte", {**r11, 0x130: 1}, "pcie_0_gdsc 48 0 1\n"),
                ("a byte outside every domain", {**r11, 0x2a0: 1}, "? 0 1\n"),
                ("another byte of a domain", {0x128: 5, 0x1a8: 6, 0x229: 6, 0x108: 8}, "usb3_phy_gdsc 41 0 6\n"),
                ("the other way round", {0x128: 7, 0x1a8: 6, 0x228: 6, 0x108: 8}, "pcie_0_gdsc 40 6 7\n")):
            with self.subTest(name):
                code, err = self.table_check(bytes(hang), new(changes), syms)
                self.assertEqual(code, 1, err)
                self.assertTrue(err.startswith(refused), err)
                self.assertIn(named, err)
        self.assertEqual(self.table_check(bytes(hang), b"", syms), (1, "no .data in TMP/new.o\n"))
        # Without the members' offsets nothing is named: refused.
        code, err = self.table_check(bytes(hang), new(r11), syms, pahole="#!/bin/sh\nexit 1\n")
        self.assertEqual((code, err), (1, "pahole gives no offsets of struct gdsc's pwrsts and pd and of struct "
                                          "generic_pm_domain's flags in TMP/new.o\n"))

    def staleness_gate(self, ages=(), out_image=b"image"):
        """Run make-kit.sh's check that the image was made from the sources as
        they are, on stand-in files.  AGES moves a file in time; None takes it
        away."""
        text = (KIT_SRC / "make-kit.sh").read_text()
        objects = re.search(r"^objects=\(.*?\)$", text, re.M | re.S)
        gate = re.search(r'^for pair in "\$\{objects\[@\]\}"; do\n.*?'
                         r'^cmp -s "\$build/arch/arm64/boot/Image\.gz" "\$out/Image\.gz" \|\|[^\n]*\n',
                         text, re.M | re.S)
        self.assertIsNotNone(objects)
        self.assertIsNotNone(gate)
        # The sources, the objects compiled from them, vmlinux, the image.
        files = {f"tree/{TCPM_DIR}/tcpm.c": 100, f"tree/{TCPM_DIR}/sm5714_usbpd.c": 100,
                 "tree/include/linux/usb/tcpm.h": 100, f"build/{TCPM_DIR}/tcpm.o": 200,
                 f"tree/{GCC}.c": 100, f"build/{GCC}.o": 200,
                 f"tree/{DWC3}.c": 100, f"build/{DWC3}.o": 200,
                 f"tree/{QMP}.c": 100, f"build/{QMP}.o": 200,
                 f"tree/{RPMHPD}.c": 100, f"build/{RPMHPD}.o": 200,
                 f"tree/{PHYMUX}.c": 100, f"build/{PHYMUX}.o": 200,
                 f"tree/{QMP_PCIE}.c": 100, f"build/{QMP_PCIE}.o": 200,
                 f"tree/{GMU}.c": 100, f"build/{GMU}.o": 200,
                 f"tree/{UFS_QCOM}.c": 100, f"tree/{UFS_QCOM}.h": 100, f"build/{UFS_QCOM}.o": 200,
                 f"tree/{PCIE_QCOM}.c": 100, f"build/{PCIE_QCOM}.o": 200,
                 f"build/{TCPM_DIR}/sm5714_usbpd.o": 200, "build/vmlinux": 300,
                 "build/arch/arm64/boot/Image.gz": 400, "out/Image.gz": 400}
        files.update(ages)
        with tempfile.TemporaryDirectory() as tmp:
            for name, age in files.items():
                path = pathlib.Path(tmp, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                if age is not None:
                    path.write_bytes(out_image if name == "out/Image.gz" else b"image")
                    os.utime(path, (1_700_000_000 + age,) * 2)
            result = subprocess.run(
                ["bash", "-c", 'set -euo pipefail; export LC_ALL=C\n'
                 'die() { echo "$*" >&2; exit 1; }\ntree=$1/tree build=$1/build out=$1/out\n'
                 f'tcpm_dir={TCPM_DIR}\ngcc={GCC}\ndwc3={DWC3}\nqmp={QMP}\nrpmhpd={RPMHPD}\n'
                 f'phymux={PHYMUX}\nqmp_pcie={QMP_PCIE}\ngmu={GMU}\nufs_qcom={UFS_QCOM}\npcie_qcom={PCIE_QCOM}\n'
                 + objects.group(0) + "\n"
                 + gate.group(0),
                 "gate", tmp], capture_output=True, text=True)
            return result.returncode, result.stdout, result.stderr.replace(tmp, "TMP")

    def test_the_image_has_to_be_built_from_the_sources_as_they_are(self):
        """The build skips a patch whose marker is in the tree already, and an
        incremental build can leave an object, or the whole image, from
        before the last change to a source."""
        self.assertEqual(self.staleness_gate(), (0, "", ""))
        stale = "%s.o is older than %s, or newer than vmlinux\n"
        tcpm, driver = f"{TCPM_DIR}/tcpm", f"{TCPM_DIR}/sm5714_usbpd"
        header = "include/linux/usb/tcpm.h"
        cases = {
            "tcpm.c changed since": ({f"tree/{tcpm}.c": 250}, stale % (tcpm, tcpm + ".c")),
            "tcpm.h changed since": ({f"tree/{header}": 250}, stale % (tcpm, header)),
            "the driver changed since": ({f"tree/{driver}.c": 250}, stale % (driver, driver + ".c")),
            "gcc-sm8550.c changed since": ({f"tree/{GCC}.c": 250}, stale % (GCC, GCC + ".c")),
            "dwc3-qcom.c changed since": ({f"tree/{DWC3}.c": 250}, stale % (DWC3, DWC3 + ".c")),
            "no dwc3-qcom object": ({f"build/{DWC3}.o": None}, stale % (DWC3, DWC3 + ".c")),
            # Revision 8: the UFS PHY driver, which the stage script and the
            # diagnostic change in a reused tree.
            "phy-qcom-qmp-ufs.c changed since": ({f"tree/{QMP}.c": 250}, stale % (QMP, QMP + ".c")),
            "no phy-qcom-qmp-ufs object": ({f"build/{QMP}.o": None}, stale % (QMP, QMP + ".c")),
            # Revision 9: the RPMh power domain driver, its debug switch.
            "rpmhpd.c changed since": ({f"tree/{RPMHPD}.c": 250}, stale % (RPMHPD, RPMHPD + ".c")),
            "no rpmhpd object": ({f"build/{RPMHPD}.o": None}, stale % (RPMHPD, RPMHPD + ".c")),
            # Revision 10: the PIPE clock mux, the PCIe PHY (the unpark patch
            # out of a reused tree), the GMU and ufs-qcom with its header.
            "clk-regmap-phy-mux.c changed since": ({f"tree/{PHYMUX}.c": 250}, stale % (PHYMUX, PHYMUX + ".c")),
            "phy-qcom-qmp-pcie.c changed since": ({f"tree/{QMP_PCIE}.c": 250},
                                                  stale % (QMP_PCIE, QMP_PCIE + ".c")),
            "no a6xx_gmu object": ({f"build/{GMU}.o": None}, stale % (GMU, GMU + ".c")),
            "ufs-qcom.h changed since": ({f"tree/{UFS_QCOM}.h": 250}, stale % (UFS_QCOM, UFS_QCOM + ".h")),
            # Revision 11: the PCIe host driver, its guard and root port switch.
            "pcie-qcom.c changed since": ({f"tree/{PCIE_QCOM}.c": 250}, stale % (PCIE_QCOM, PCIE_QCOM + ".c")),
            "no pcie-qcom object": ({f"build/{PCIE_QCOM}.o": None}, stale % (PCIE_QCOM, PCIE_QCOM + ".c")),
            # The driver includes the header too.
            "the driver's object alone is old": (
                {f"build/{driver}.o": 50, f"tree/{driver}.c": 40}, stale % (driver, header)),
            "an object as old as its source": ({f"build/{tcpm}.o": 100},
                                               stale % (tcpm, tcpm + ".c")),
            "an object that is not there": ({f"build/{driver}.o": None},
                                            stale % (driver, driver + ".c")),
            "an object compiled after the link": ({f"build/{tcpm}.o": 350},
                                                  stale % (tcpm, tcpm + ".c")),
            "vmlinux linked after the image": ({"build/vmlinux": 450},
                                               "Image.gz is older than vmlinux\n"),
        }
        for name, (ages, message) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.staleness_gate(ages), (1, "", message))
        with self.subTest("an image of another build in the output directory"):
            self.assertEqual(self.staleness_gate(out_image=b"other"),
                             (1, "", "TMP/out/Image.gz is not the build directory's\n"))

    def test_readmes_name_the_kit_and_what_the_locked_down_kernel_means(self):
        self.assertIn("(typec-roles/README.md)", (REPO / "tools/inplace/README.md").read_text())
        self.assertIn("tools/inplace/typec-roles/README.md",
                      (REPO / "docs/usb-c-dock-roles.md").read_text())
        readme = " ".join((KIT_SRC / "README.md").read_text().split())
        install = (KIT_SRC / "install.sh").read_text()
        for name in ("/var/backups/ubuntu-gts9u-typec-roles", "/var/log/gts9u-typec-roles.log",
                     "boot-hang-log.img", "sink_host_swap", "try_snk_extra_wait_ms"):
            with self.subTest(name):
                self.assertIn(name, readme)
                self.assertIn(name, install + (KIT_SRC / "verify.sh").read_text())
        # The hang-log kernel refuses the kit's modules, and the kit's kernel
        # the hang-log kit's: what a boot with the two mixed is without, and
        # what it still has on this tablet.
        self.assertIn("**The kernel is locked down and refuses a module signed with another "
                      "build's key**", readme)
        self.assertIn("**no NAT, no v4l2loopback and no netconsole until `rollback.sh` has run**",
                      readme)
        self.assertIn("Wi-Fi, USB networking and Bluetooth are built in", readme)
        self.assertIn("Wi-Fi, USB networking and Bluetooth\n# are built in", install)
        # The swap test is one of the steps after the install, and what a
        # program that writes data_role gets from this kernel is said.
        self.assertIn("sudo sh ~/typec-roles/verify.sh dock 10\n"
                      "sudo sh ~/typec-roles/verify.sh swap 3\n",
                      (KIT_SRC / "README.md").read_text())
        self.assertIn("a program that writes the port's `data_role` itself now gets `EAGAIN` "
                      "(`Resource temporarily unavailable`) while a VDM exchange is under way, "
                      "from the moment TCPM queues a VDM (discovery's, for one) until that "
                      "exchange is over, and when the swap ended with the role unchanged", readme)
        self.assertNotIn("discovery is running", readme)
        # The patches it names are the kit's, and so is their count.
        text = (KIT_SRC / "make-kit.sh").read_text()
        kit = sum(kit_patches(text), [])
        self.assertEqual(sorted(set(re.findall(r"`kernel/patches/([^`]+)`", readme))), sorted(kit))
        self.assertEqual((len(kit), len([name for name in kit if name.startswith("tcpm-")])), (20, 5))
        self.assertIn("and the UFS PHY's two (revision 8): nine patches, five of them to TCPM. Revision 9 adds the "
                      "two debug patches above: eleven patches. Revision 10 adds the three upstream fixes and the "
                      "second UFS PHY diagnostic, fifteen patches, and leaves out `unpark-pcie0-pipe-mux.patch`, "
                      "which every default build applies.", readme)
        self.assertIn("In the kernel source tree twelve files differ from the hang-log build:", readme)
        self.assertIn("Against revision 9's tree six differ: those five and `phy-qcom-qmp-ufs.c`.", readme)
        self.assertIn("Revision 11 adds `kernel/patches/gcc-sm8550-pcie0-gdsc-off-in-suspend.patch`, the PCIe "
                      "host's half of the guard and the root port's debug switch, rewrites the GDSC debug switch "
                      "patch, and adds the UFS PHY power reference fix and its diagnostic: twenty patches, thirteen "
                      "files (`pcie-qcom.c` the new one); against revision 10's tree four differ, `gcc-sm8550.c`, "
                      "`pcie-qcom.c`, `ufs-qcom.c` and `ufs-qcom.h`.", readme)
        # Revision 11's ufs-qcom: in every build, what its lines are, the
        # PCS reset kept, and the check without root.
        for said in ("**Revision 11 also fixes the UFS PHY's power reference**, in every build of both tablets: no "
                     "build variable and no board check, so a default build changes by this patch as well.",
                     "Revision 8's PCS reset was not the fix for this failure",
                     "`ufshcd-qcom 1d84000.ufshc: power-up sequence entry: PHY power_count 1 init_count N, host "
                     "reference held, setup_clocks repeats: power-on 0, power-off 0`",
                     "`setup_clocks repeated a PHY power-on (1): clock gating state 3, active_reqs 1, in the ungate "
                     "work`. Each such line is a leak the fix absorbed",
                     "`ufs-phy-power-check.sh` (in the kit; read-only, no root, both tablets)",
                     "the sleep came back 9 times of 9 with it (twice without the storage, revision 9's run 3 and "
                     "revision 10's run 5",
                     "**`PWRDN 0x0` at the entry with no `power-up write` line before it means the power-on did not "
                     "run**"):
            with self.subTest(said):
                self.assertIn(said, readme)
        self.assertNotIn("its core supply L1D the first suspect", readme)
        self.assertNotIn("a UFS PHY fault of its own, which this revision does not change", readme)
        # Revision 9: what it is, how it is built, and the runs it was for.
        for said in ("**Revision 9** is revision 8 plus two debug patches, which a release build does not get",
                     "**never set a PCIe one for a suspend in which the PCIe host stays up**",
                     "**Four debug switches** (revision 9), each read as its power domains are, at every boot "
                     "as the kernel's table has them: off, but `debug_pcie0_off` on from revision 11; since "
                     "revision 11 a fifth that says how they booted, and a sixth, the root port's, off at every "
                     "boot.",
                     "| `/sys/module/gcc_sm8550/parameters/debug_pcie0_off` | `N`; `Y` on revision 11 | "
                     "`gdsc=pcie0` (`Y`), `gdsc-ret=pcie0` (`N`) |",
                     "| `/sys/module/gcc_sm8550/parameters/debug_boot_state` (revision 11, read-only) | "
                     "`pcie0 Y, pcie0phy N, usb N` |",
                     "| `/sys/module/rpmhpd/parameters/debug_ret_floor` | `N` | `cxmx=ret` |",
                     "APPLY_QMP_UFS_PCS_STATE_DIAGNOSTIC=1 APPLY_DEEP_SLEEP_DEBUG_SWITCHES=1 "
                     "APPLY_SM8550_SLEEP_FIXES=1 \\",
                     "**`gdsc=LIST`**, **`gdsc-ret=pcie0`** and **`cxmx=ret`** (`ro` and `dry`; the kernels of "
                     "revisions 9 to 11)",
                     "**Revision 9: the runs** (2026-11-22 and 23, squirtle;",
                     "each with `ro 1 30 release-floor pci=remove mode=s2idle trace=live hold=60 restart-ok`:",
                     "2. `gdsc=pcie0`;\n3. `gdsc=pcie0,usb`;\n4. `gdsc=pcie0,usb,pcie0phy`;\n"
                     "5. `gdsc=pcie0,usb,pcie0phy cxmx=ret`.",
                     "Revision 9 installs the same way over revision 7, which is what squirtle has on 2026-11-22",
                     # Revision 10: what it is, what it leaves out, and its run.
                     "**Revision 10** (built 2026-11-23, on squirtle since 11:37 that day) is revision 9 plus three upstream commits",
                     "`unpark-pcie0-pipe-mux.patch`, which selected the PHY's clock at each PHY power-on, is left out",
                     "**Revision 10: the runs** (2026-11-23; six have run, listed after the command).",
                     "`pcie_0_gdsc` still has to be let off (`gdsc=pcie0`) for the sleep to return.",
                     "A stopped ADSP keeps the DDR at 547 MHz (revision 9's run 2c did the same): do not stop it for a run.",
                     "Revision 10 installs the same way over revision 9, which squirtle has had since 2026-11-22 20:59",
                     "the NEXT line then says that the storage came back and names the PCIe host's line",
                     "`PCIe came back (no host failure line)`",
                     # Revision 11: what it is, its guard, why its first boot is
                     # safe, and its runs.
                     "**Revision 11** (written 2026-11-23, not built yet) is revision 10 plus `pcie_0_gdsc`, the "
                     "PCIe host's power domain, collapsed in a system suspend in which the host has powered itself "
                     "down and kept on while the host keeps its link (a guard), and a debug switch that lets the "
                     "PCIe root port go to D3hot in a system suspend.",
                     "Two build variables, which go together or not at all (each is refused without the other), "
                     "`APPLY_SM8550_PCIE0_GDSC_OFF=1` and `APPLY_SM8550_PCIE0_GDSC_GUARD=1`",
                     "With the guard the panel cycle keeps the domain on by construction: the root port is in D0 "
                     "then, the host keeps its link, and the root port's switch cannot be set before the system "
                     "is up. That has not run either",
                     "| `/sys/module/pcie_qcom/parameters/debug_root_port_d3` (revision 11) | `N` | `root-port-d3` |",
                     "**Revision 11: the runs** (none has run; the kernel is not built yet).",
                     "1. **Install and check.**",
                     "No drop-in is needed: the guard keeps `pcie_0_gdsc` on in the boot's panel cycle by "
                     "construction",
                     "masking the unit instead does not do (the floor is then never released)",
                     "   ExecStartPre=-/bin/sh -c 'p=/sys/module/gcc_sm8550/parameters; grep -qx \"pcie0 Y, pcie0phy "
                     "N, usb N\" $$p/debug_boot_state && echo N > $$p/debug_pcie0_off'\n"
                     "   ExecStartPost=-/bin/sh -c 'p=/sys/module/gcc_sm8550/parameters; grep -qx \"pcie0 Y, pcie0phy "
                     "N, usb N\" $$p/debug_boot_state && echo Y > $$p/debug_pcie0_off'\n",
                     "   sudo sh ~/typec-roles/sleep-test.sh ro 1 30 release-floor pci=remove mode=s2idle hold=60 "
                     "rpmh restart-ok\n",
                     "   sudo sh ~/typec-roles/sleep-test.sh ro 1 30 release-floor pci=keep hold=60 restart-ok\n",
                     "- **`rpmh`** (`ro`; revision 11's script, any kit kernel): what of Linux's own votes stands in "
                     "the way of XO shutdown, CX collapse and AOSS sleep (`cxsd`, `aosd`).",
                     "   sudo sh ~/typec-roles/sleep-test.sh ro 1 30 pm_test=platform pci=keep root-port-d3 hold=60 "
                     "restart-ok\n",
                     "   sudo sh ~/typec-roles/sleep-test.sh ro 1 30 release-floor mode=s2idle pci=keep root-port-d3 "
                     "hold=60 rpmh restart-ok\n",
                     "Revision 11 installs the same way over revision 10, which squirtle has had since 2026-11-23 "
                     "11:37",
                     "- **`pci=keep`** (`ro`): the PCI devices stay, and the run watches the Wi-Fi.",
                     "- **`root-port-d3`** (`ro`, with `pci=keep`; revision 11's kernel)",
                     "None of these makes a normal sleep with the Wi-Fi deep"):
            with self.subTest(said):
                self.assertIn(said, readme if "\\" not in said and "\n" not in said
                              else (KIT_SRC / "README.md").read_text())
        # What has run, and what has not: revision 7 on the SM-X710 on
        # 2026-11-21, the sleep that lost the storage, and what revision 8 is.
        self.assertIn("Revision 7 added the third fix, the SM-X910's power domain change and a NULL "
                      "check in the dwc3-qcom glue.", readme)
        self.assertIn("on revision 7's kernel (installed at 22:39) everything that was run passed: two "
                      "boots with the dock attached, the charger plugged into the running tablet, "
                      "`verify.sh dock 4` (4 of 4), `verify.sh swap 3` (3 of 3;", readme)
        self.assertIn("one real deep sleep on revision 7 (`sudo rtcwake -m mem -s 30`, 23:34, on a "
                      "powered dock, the interconnect floor released) kept the USB host and lost the "
                      "storage", readme)
        self.assertIn("**Revision 8** is revision 7 plus two changes to the UFS PHY driver and nothing "
                      "else in the kernel", readme)
        self.assertIn("That the missing reset caused the 2026-11-21 failure is probable, not proven",
                      readme)
        for stale in ("was built on", "no dock has been plugged into it", "is not in this kit",
                      "this kit changes nothing there", "New in this revision are",
                      "**On the SM-X710 the previous revision of the kit is installed",
                      "not done yet: `verify.sh dock` and `swap` on a kit kernel",
                      "The data role fault has not been seen on it", "Not on the SM-X710 for now"):
            self.assertNotIn(stale, readme)
        # What the SM-X710 showed.
        self.assertIn("That is the SM-X910's fault, seen once here: the reason revision 7 carries the "
                      "power domain change.", readme)
        # The sleep test: how to run it, what a pass means, the floor, and
        # where the result is after the restart.
        for said in ("sudo sh ~/typec-roles/sleep-test.sh ro 3 30 restart-ok",
                     "sudo sh ~/typec-roles/sleep-test.sh ro 3 30 release-floor restart-ok",
                     "**ends with a restart of the tablet**",
                     "**What a pass means.** `SLEEP-TEST RESULT: PASS` with `floor released`",
                     "A run with the floor held says `floor held` in every verdict",
                     "So the boot after a run in a boot that released the floor holds it once, and the "
                     "next run needs `release-floor`",
                     "The verdict and diagnostic lines are in pmsg",
                     """`grep -rah 'gts9u sleep-test' "$(ls -d /var/lib/ubuntu-gts9u/pstore/[0-9]* | """
                     """tail -n 1)"`""",
                     # 2026-11-22: the freeze, the dry mode, and the aborted
                     # sleeps.
                     "sudo sh ~/typec-roles/sleep-test.sh dry restart-ok",
                     "Each run freezes every disk filesystem and **ends with a restart of the tablet** "
                     "(`sysrq b`)",
                     "**Why a freeze, not read-only.**",
                     "**Do not touch the screen during a run.**",
                     "**Keep an SSH session open before starting.**",
                     "**`aborts=N`** (default 5, at most 20)",
                     "`SLEEP-TEST RESULT: NO-SLEEP at cycle N/M: ...`",
                     "`SLEEP-TEST RESULT: DRY: no sleep tried; ...`"):
            with self.subTest(said):
                self.assertIn(said, readme if "\\" not in said else (KIT_SRC / "README.md").read_text())
        # A docked boot tests the host controller only with devices on the
        # bus, and an earlier revision is upgraded in place.
        self.assertIn("A boot with nothing on the bus does not test the host controller.", readme)
        self.assertNotIn("there is no upgrade from an earlier revision", readme)
        for said in ("**Over an earlier revision of this kit** `install.sh` runs as an upgrade: one "
                     "command, then one reboot, and no `rollback.sh` first.",
                     "`rollback.sh` returns to the hang-log kit, not to the earlier revision, after "
                     "any number of upgrades.",
                     "Anything else is refused with nothing written, and the message names the "
                     "image or the build ID it found.",
                     "If an upgrade stops half-way, run `install.sh` again, or `rollback.sh`, before "
                     "any reboot; a second run of `install.sh` picks up wherever the first one "
                     "stopped.",
                     "`install.sh` of the revision that is installed already says so and changes "
                     "nothing.",
                     "A first install that stopped is not finished by a second run: `rollback.sh` "
                     "undoes it, then `install.sh` runs again.",
                     # Copied over the earlier kit, not into a directory inside it.
                     "scp ~/ubuntu-gts9u/backups/typec-roles-kit/* <user>@<tablet>:typec-roles/",
                     # The way back from TWRP stays.
                     "dd if=/mnt/ubuntu/var/backups/ubuntu-gts9u-typec-roles/boot-hang-log.img \\ "
                     "of=/dev/block/by-name/boot bs=4M"):
            with self.subTest(said):
                self.assertIn(said, readme)
        # Why the recorder reads the log every second, and what a plug-in or
        # a swap run without a verdict means.
        self.assertIn("so during a plug-in the script **reads the log every second** and adds "
                      "what it read to the trial's `trial-N.tcpm.log`. A log that overflows all "
                      "the same has lost lines between two reads: in `dock` that plug-in fails, "
                      "since its one attach cannot be shown, and in `swap` it gets no verdict "
                      "and the run fails.", readme)
        self.assertIn("**no `DR_SWAP_SEND` was cut short by a Soft Reset**", readme)
        self.assertNotIn("none was sent into a discovery", readme.replace("*", ""))
        self.assertIn("A run in which no plug-in got a verdict fails.", readme)
        for said in ("until the reboot the running kernel can load no module it has not loaded yet",
                     "while the kit is installed, do not upgrade ubuntu-gts9u-device: its "
                     "updates/qcom_spss_irq.ko is signed for another kernel"):
            self.assertIn(f"say '{said}'", install)


if __name__ == "__main__":
    unittest.main()
