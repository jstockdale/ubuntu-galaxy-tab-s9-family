"""Run the SM-X910 Type-C roles kit's tablet scripts
(tools/inplace/typec-roles-ultra) against a fake root.

install.sh and rollback.sh normally run as root on the SM-X910 and write the
boot partition; verify.sh reads the Type-C port and the TCPM log.  Here every
absolute path they touch is moved under a temporary directory, the partition
size and the pinned image identities (release v1.2.5's, and revision 3 of the
kit's) are replaced by those of small stand-in images, and id, uname, flock,
findmnt, fuser, dpkg, depmod, blockdev, systemctl, journalctl, modinfo, dmesg,
sync and sleep are stubbed, and for verify.sh cat as well: the TCPM log it
reads is emptied by each read and holds 1023 lines, as the kernel's does.  The
systemctl stub keeps the typec-sink-host
stopgap's state in two files, depmod fails on request, which stops an
install between the module swap and the flash, and sync records what is on
the disk each time it is called, and fails on request.  make-kit.sh needs a
kernel build and is only checked for syntax, for pinning the same identities
as the tablet scripts, for naming patches the build applies, for what its
comparison of two kernel source trees leaves out, and for its checks of the
power domain patch and of the age of the objects the image was linked from.
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
import importlib.util


REPO = pathlib.Path(__file__).resolve().parents[1]
KIT_SRC = REPO / "tools/inplace/typec-roles-ultra"
RELEASE = "7.2.0-rc3-dirty"
SIZE = 4 * 1024 * 1024
BACKUP = "var/backups/ubuntu-gts9u-typec-roles"
# Release v1.2.5's kernel, as the tablet reports it.
BASE_BUILD_ID = "3447147dc203542e441eebfa4d62e60c57100f50"
NEW_BUILD_ID = "0123456789abcdef0123456789abcdef01234567"
NEWER_BUILD_ID = "89abcdef0123456789abcdef0123456789abcdef"
KEY_ID = "aa" * 20
SIGNER = ":".join(["5A"] * 20)
MODULES = ("modules.dep", "kernel/net/a.ko", "updates/b.ko")
TCPM_DIR = "drivers/usb/typec/tcpm"
GCC = "drivers/clk/qcom/gcc-sm8550"
DWC3 = "drivers/usb/dwc3/dwc3-qcom"
QMP = "drivers/phy/qualcomm/phy-qcom-qmp-ufs"
PCS_PATCH = "qmp-ufs-assert-pcs-reset-before-calibration-gts9u.patch"

PREFIXES = (
    "/dev/disk/by-partlabel/", "/usr/lib/modules/", "/var/backups", "/var/log/", "/run/",
    "/var/lib/gts9u-boot-sets/", "/proc/device-tree/", "/proc/sys/kernel/",
    "/sys/kernel/notes", "/sys/class/power_supply/", "/sys/class/typec/",
    "/sys/kernel/debug/usb/", "/sys/bus/usb/devices/", "/sys/module/sm5714_usbpd/parameters",
    "/dev/kmsg", "/sys/class/regulator/",
    # Revision 8's checks (verify.sh).
    "/sys/bus/platform/", "/sys/module/gcc_sm8550/", "/sys/module/rpmhpd/", "/sys/module/pcie_qcom/",
    "/sys/module/qcom_stats/", "/sys/class/bluetooth/", "/sys/class/tty/", "/usr/lib/systemd/system-sleep/",
    "/dev/pmsg0",
    # Revision 9's (verify.sh: the GPU lines' boot time).
    "/proc/uptime",
)

STUBS = {
    "id": 'echo 0',
    "uname": 'case $1 in -r) echo %s ;; *) echo "#1 SMP PREEMPT test" ;; esac' % RELEASE,
    "flock": 'exit 0',
    "findmnt": 'cat "$FAKE_ROOT/mount-options" 2>/dev/null || echo rw,relatime',
    "fuser": '[ -e "$FAKE_ROOT/dpkg-busy" ]',
    "dpkg": 'exit 0',
    "depmod": '[ ! -e "$FAKE_ROOT/depmod-fails" ]',
    # The stand-in partitions are files; their size is the partition's.
    "blockdev": 'case $1 in --getsize64) exec stat -c %s "$2" ;; esac',
    # The stopgap: enabled and running while its two files exist.
    "systemctl": '''echo "$*" >> "$FAKE_ROOT/systemctl.calls"
case $1 in
is-enabled) [ -e "$FAKE_ROOT/stopgap-enabled" ] ;;
is-active) [ -e "$FAKE_ROOT/stopgap-active" ] ;;
disable) [ ! -e "$FAKE_ROOT/systemctl-fails" ] || { echo 'Failed to disable unit' >&2; exit 1; }
	rm -f "$FAKE_ROOT/stopgap-enabled" ;;
enable) [ ! -e "$FAKE_ROOT/systemctl-fails" ] || { echo 'Failed to enable unit' >&2; exit 1; }
	touch "$FAKE_ROOT/stopgap-enabled" ;;
*) exit 3 ;;
esac''',
    "sleep": 'exec /usr/bin/sleep 0.01',
    "dmesg": 'cat "$FAKE_ROOT/dev/kmsg" 2>/dev/null',
    "journalctl": 'cat "$FAKE_ROOT/journal" 2>/dev/null',
    "modinfo": 'case $1 in -F) cat "$FAKE_ROOT/signer" ;; esac',
    # What a power failure right after this call would find: is the release
    # image saved, whose module tree is in place, what boot holds, is an
    # install complete, is an upgrade under way.  The call fails if a file
    # named after its number asks.
    "sync": '''b=$FAKE_ROOT/%s
echo "saved=$([ -s "$b/boot-release.img" ] && echo yes || echo no)" \\
	"tree=$(cut -d' ' -f1 "$FAKE_ROOT/usr/lib/modules/%s/modules.dep" 2>/dev/null)" \\
	"boot=$(sha256sum < "$FAKE_ROOT/dev/disk/by-partlabel/boot" | cut -c1-64)" \\
	"complete=$([ -e "$b/complete" ] && echo yes || echo no)" \\
	"upgrading=$([ -e "$b/upgrading" ] && echo yes || echo no)" >> "$FAKE_ROOT/sync.calls"
[ ! -e "$FAKE_ROOT/sync-fails-$(wc -l < "$FAKE_ROOT/sync.calls")" ]''' % (BACKUP, RELEASE),
}
# The xHCI driver's other ways of saying that the controller is gone, and the
# locked-down kernel refusing a module of another build.
USB_HOST_ERRORS = (
    "blastoise kernel: xhci-hcd xhci-hcd.2.auto: WARNING: Host System Error",
    "blastoise kernel: xhci-hcd xhci-hcd.2.auto: Controller not ready at resume -19",
)
MODULE_REFUSED = ("blastoise kernel: Lockdown: modprobe: unsigned module loading is restricted; "
                  "see man kernel_lockdown.7")
# The controller lost its state in a suspend and the driver set it up again.
USB_HOST_REINIT = "blastoise kernel: xhci-hcd xhci-hcd.2.auto: xHC error in resume, USBSTS 0x401, Reinit"
# The L15B step of kernel/drivers/gts9u-performance.c (2026-11-25): its line
# with the pinned DTB, and one of its refusals (a node that is not PM8550's
# vreg_l15b_1p8).
L15B_NODE = "/soc@0/rsc@17a00000/regulators-0/ldo15"
L15B_STEP = "gts9u-performance: L15B held in HPM: 2 of its DTB properties dropped"
L15B_REFUSED = f"gts9u-performance: L15B not held in HPM: {L15B_NODE} is not PM8550's vreg_l15b_1p8, left alone"
# Revisions 9 and 10.  The driver's twelve switches as its kernel boots them
# (sysfs gives a bool as Y or N, and every value ends in a newline), in the
# order the driver declares them; revision 9 has the first ten, revisions 1
# to 8 the first four, the release the first two.  R9_SWITCHES are the six
# that revision 9 added, R10_SWITCHES the two of revision 10.
SWITCHES = {"otg_rp": "0", "connection_wakeup": "N", "sink_host_swap": "Y", "try_snk_extra_wait_ms": "600",
            "swap_back_to_sink": "Y", "own_pr_swap_in_place": "Y", "swap_protocol_reset": "Y",
            "pr_swap_rd_delay_ms": "0", "sink_tx_ok_rp": "Y", "recovery_detach": "Y",
            "cc_reopen_via_snk": "Y", "cc_self_heal": "Y"}
R9_SWITCHES = tuple(SWITCHES)[4:10]
R10_SWITCHES = tuple(SWITCHES)[10:]
# Revision 10's driver lines as its dev_info() and dev_warn() format them
# (the kernel log of a tablet, as blastoise's journal writes it), and the
# resync the probe asks for at every boot, which is none of them.
CC_OPENED = ("blastoise kernel: sm5714-usbpd 8-0033: CC opened for TCPM's error recovery after a swap or "
             "Hard Reset")
CC_REOPENED = "blastoise kernel: sm5714-usbpd 8-0033: CC on again through Unattached.SNK after the driver opened it"
CC_HEALED = ("blastoise kernel: sm5714-usbpd 8-0033: CC finds no partner since the driver opened it (CC status "
             "0x00, status1 0x10, CC state 0x01): CC logic re-initialised, try 1 of 3")
CC_HEALED_VBUS = ("blastoise kernel: sm5714-usbpd 8-0033: CC finds no partner though VBUS comes from one (CC "
                  "status 0x00, status1 0x10, CC state 0x05): re-initialisation failed, try 2 of 3")
CC_RESYNC = ("blastoise kernel: sm5714-usbpd 8-0033: CC status 0x09 shows a partner TCPM was not told of; "
             "resyncing TCPM")
CC_PROBE_RESYNC = "blastoise kernel: sm5714-usbpd 8-0033: resynchronizing TCPM, CC status 0x00"
# The GMU's lines as blastoise logged them under revision 8 (2026-11-25 and
# 26, journalctl -k), and the note of the hfi interrupt fix as its
# dev_warn_once() formats it.
GMU_TIMEOUT = ("blastoise kernel: platform 3d6a000.gmu: [drm:a6xx_hfi_wait_for_msg_interrupt] *ERROR* "
               "Message HFI_H2F_MSG_GX_BW_PERF_VOTE id 3921 timed out waiting for response")
GMU_UNEXPECTED = ("blastoise kernel: platform 3d6a000.gmu: [drm:a6xx_hfi_send_msg] *ERROR* "
                  "Unexpected message id 3921 on the response queue")
GMU_OOB = ("blastoise kernel: platform 3d6a000.gmu: [drm:a6xx_gmu_set_oob] *ERROR* "
           "Timeout waiting for GMU OOB set GPU_SET: 0x0")
GMU_MASK = ("blastoise kernel: platform 3d6a000.gmu: GMU interrupt mask 0xff7ffffe left 0x1 unmasked "
            "(status 0x1), written again: 0xff7fffff")

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
# What the Anker hub did on the release kernel: the dock's own swap, its
# Soft_Reset as DFP, error recovery, and the next attach.
FLAP_LOG = """\
[  200.000001] state change SNK_DEBOUNCED -> SNK_ATTACHED [rev3 NONE_AMS]
[  200.900001] Data role mismatch, initiating error recovery
[  200.900002] state change SNK_READY -> ERROR_RECOVERY [rev3 DISCOVER_IDENTITY]
[  201.500001] state change SNK_DEBOUNCED -> SNK_ATTACHED [rev3 NONE_AMS]
"""
# The Anker hub with the kit (2026-11-21): the source is reported 43 ms into
# the longer wait, and VBUS follows while its debounce is checked.
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
# What the Satechi dock did on the release kernel: Try.SNK gave up, the
# tablet stayed the power source.
TRY_SNK_LOST_LOG = """\
[  400.100001] state change SNK_TRY -> SNK_TRY_WAIT [delayed 100 ms]
[  400.100002] state change SNK_TRY_WAIT -> SRC_TRYWAIT [rev3 NONE_AMS]
[  400.500001] state change SRC_TRYWAIT_DEBOUNCE -> SRC_ATTACHED [delayed 200 ms]
"""
# The Satechi dock plugged in cold on revision 3 of the kit (2026-11-21): its
# Rp is reported 25 ms into the longer wait and is gone again 46 ms later,
# and revision 3 gave the attempt up there.
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
# The same plug-in as the kit handles it from revision 4 on: with the Rp gone
# the port goes back to waiting for what is left of the time, and takes the
# Rp that comes back.  Made from the log above; not recorded on a tablet.
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
# The boot's suspend cycle with a dock attached, on release v1.2.5 and on
# revision 3 of the kit (2026-11-21).
USB_HOST_DIED = """\
blastoise kernel: PM: suspend entry (deep)
blastoise kernel: xhci-hcd xhci-hcd.2.auto: xHCI host not responding to stop endpoint command
blastoise kernel: xhci-hcd xhci-hcd.2.auto: xHCI host controller not responding, assume dead
blastoise kernel: xhci-hcd xhci-hcd.2.auto: HC died; cleaning up
blastoise kernel: PM: suspend exit
blastoise kernel: xhci-hcd xhci-hcd.2.auto: Host halt failed, -110
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


def constant(script, name):
    return re.search(rf"^{name}=(\S+)$", (KIT_SRC / script).read_text(), re.M).group(1)


def kit_paths(text):
    """make-kit.sh's path variables (tcpm_dir=..., gcc=...), by name."""
    names = {}
    for name, value in re.findall(r"^([a-z_0-9]+)=((?:drivers|sound|include)/[A-Za-z0-9_./-]+)$", text, re.M):
        names[name] = value
    return names


def kit_expand(word, names):
    """WORD with make-kit.sh's $name and ${name} put in."""
    return re.sub(r"\$\{?(\w+)\}?", lambda match: names[match.group(1)], word)


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
        for d in ("dev/disk/by-partlabel", "var/backups", "var/log", "run",
                  "proc/device-tree", "proc/sys/kernel/random", "sys/kernel",
                  "sys/class/power_supply/battery", "sys/class/typec/port0",
                  "sys/kernel/debug/usb/tcpm-8-0033", "sys/bus/usb/devices/1-1",
                  "sys/module/sm5714_usbpd/parameters",
                  f"usr/lib/modules/{RELEASE}/kernel/net", f"usr/lib/modules/{RELEASE}/updates"):
            (self.root / d).mkdir(parents=True)
        (self.root / "proc/device-tree/compatible").write_bytes(
            b"samsung,gts9uwifi\0samsung,gts9u\0qcom,sm8550\0")
        (self.root / "proc/sys/kernel/random/boot_id").write_text("5c0d9f0e-0000\n")
        (self.root / "sys/class/power_supply/battery/capacity").write_text("97\n")
        (self.root / "sys/class/power_supply/battery/status").write_text("Full\n")
        self.running(BASE_BUILD_ID)
        # Release v1.2.5 as installed: stand-in images, its module tree, and
        # the stopgap service enabled and running.
        self.base_boot = os.urandom(SIZE)
        self.base_vendor_boot = os.urandom(SIZE)
        # What revision 3 of the kit wrote to boot; no test tablet holds it
        # unless a test says so.
        self.r3_boot = os.urandom(SIZE)
        self.part("boot").write_bytes(self.base_boot)
        self.part("vendor_boot").write_bytes(self.base_vendor_boot)
        self.modules = self.root / "usr/lib/modules" / RELEASE
        for name in MODULES:
            (self.modules / name).write_text(f"release {name}\n")
        self.stopgap(enabled=True, active=True)
        # The kit, as make-kit.sh lays it out.
        self.new_boot = os.urandom(SIZE)
        self.kit.mkdir()
        (self.kit / "boot.img").write_bytes(self.new_boot)
        (self.kit / f"modules-{RELEASE}.tar").write_bytes(modules_tar("new"))
        (self.kit / "BUILD_ID").write_text(NEW_BUILD_ID + "\n")
        (self.kit / "KEY_ID").write_text(KEY_ID + "\n")
        (self.kit / "SIGNER").write_text(SIGNER + "\n")
        (self.kit / "SOURCE").write_text("checkout: test\n")
        for name in ("install.sh", "rollback.sh", "verify.sh", "README.md"):
            (self.kit / name).write_bytes((KIT_SRC / name).read_bytes())
        self.seal()

    def seal(self):
        names = sorted(p.name for p in self.kit.iterdir() if p.is_file() and p.name != "SHA256SUMS")
        (self.kit / "SHA256SUMS").write_text(
            "".join(f"{sha((self.kit / n).read_bytes())}  {n}\n" for n in names))

    def newer_kit(self):
        """The kit's next revision in the same place: another kernel and
        another module tree, with the same list of modules."""
        self.newer_boot = os.urandom(SIZE)
        (self.kit / "boot.img").write_bytes(self.newer_boot)
        (self.kit / f"modules-{RELEASE}.tar").write_bytes(modules_tar("newer"))
        (self.kit / "BUILD_ID").write_text(NEWER_BUILD_ID + "\n")
        self.seal()

    def part(self, label):
        return self.root / "dev/disk/by-partlabel" / label

    def running(self, build_id):
        note = bytes.fromhex("040000001400000003000000") + b"GNU\0" + bytes.fromhex(build_id)
        (self.root / "sys/kernel/notes").write_bytes(note)

    def stopgap(self, enabled=None, active=None):
        for name, wanted in (("stopgap-enabled", enabled), ("stopgap-active", active)):
            if wanted:
                (self.root / name).touch()
            elif wanted is not None:
                (self.root / name).unlink(missing_ok=True)

    def stopgap_enabled(self):
        return (self.root / "stopgap-enabled").exists()

    def systemctl_calls(self):
        calls = self.root / "systemctl.calls"
        return calls.read_text().splitlines() if calls.exists() else []

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
        self.assertIn("size=100663296", text) if name != "verify.sh" else None
        text = text.replace("size=100663296", f"size={SIZE}")
        # The stand-in partitions are files, and a build host's dd may not do
        # direct I/O on them (uutils dd 0.10 fails with EINVAL).
        text = text.replace(" iflag=direct", "")
        text = text.replace(pinned("install.sh", "base_boot"), sha(self.base_boot))
        text = text.replace(pinned("install.sh", "base_vendor_boot"), sha(self.base_vendor_boot))
        text = text.replace(pinned("install.sh", "kit_r3_boot"), sha(self.r3_boot))
        # The script finds its kit by its own location.
        self.assertIn('kit=$(cd "$(dirname "$0")" && pwd)', text) if name != "rollback.sh" else None
        return text.replace('kit=$(cd "$(dirname "$0")" && pwd)', f"kit={self.kit}")

    def run_script(self, name, *args, **popen):
        path = self.tmp / f"run-{name}"
        path.write_text(self.script(name))
        env = {"PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_ROOT": str(self.root),
               "HOME": str(self.tmp)}
        return subprocess.run(["sh", str(path), *args], env=env, capture_output=True, text=True,
                              timeout=120, **popen)

    def tree(self):
        return {str(p.relative_to(self.modules)): p.read_text()
                for p in self.modules.rglob("*") if p.is_file()}

    def log(self):
        return (self.root / "var/log/gts9u-typec-roles.log").read_text()


class InstallTest(FakeTablet):
    def assert_untouched(self):
        self.assertEqual(self.part("boot").read_bytes(), self.base_boot)
        self.assertEqual(self.part("vendor_boot").read_bytes(), self.base_vendor_boot)
        self.assertEqual(self.tree(), {n: f"release {n}\n" for n in MODULES})
        self.assertFalse((self.root / BACKUP).exists())
        self.assertTrue(self.stopgap_enabled())

    def test_install_writes_boot_and_the_module_tree_only(self):
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Type-C roles kit installed; reboot now", result.stdout)
        self.assertEqual(self.part("boot").read_bytes(), self.new_boot)
        self.assertEqual(self.part("vendor_boot").read_bytes(), self.base_vendor_boot)
        self.assertEqual(self.tree(), {n: f"new {n}\n" for n in MODULES})
        backup = self.root / BACKUP
        self.assertEqual((backup / "boot-release.img").read_bytes(), self.base_boot)
        self.assertEqual((backup / f"modules-{RELEASE}/updates/b.ko").read_text(),
                         "release updates/b.ko\n")
        self.assertTrue((backup / "complete").exists())
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertFalse((self.root / "usr/lib/modules" / f".typec-roles-{RELEASE}").exists())
        # What the terminal shows is also in the log.
        self.assertIn("Type-C roles kit installed; reboot now", self.log())
        self.assertNotIn("STOP", self.log())

    def test_install_disables_the_stopgap_and_rollback_enables_it_again(self):
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("typec-sink-host.service disabled from the next boot", result.stdout)
        self.assertFalse(self.stopgap_enabled())
        self.assertTrue((self.root / BACKUP / "stopgap-was-enabled").exists())
        # Disabled for the next boot, not stopped: the release kernel still
        # runs until then.
        self.assertIn("disable typec-sink-host.service", self.systemctl_calls())
        self.assertFalse([c for c in self.systemctl_calls() if c.startswith(("stop", "disable --now"))])
        self.assertTrue((self.root / "stopgap-active").exists())
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("typec-sink-host.service enabled again", result.stdout)
        self.assertTrue(self.stopgap_enabled())

    def test_a_stopgap_that_was_not_enabled_stays_that_way(self):
        self.stopgap(enabled=False, active=False)
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("typec-sink-host", result.stdout)
        self.assertFalse((self.root / BACKUP / "stopgap-was-enabled").exists())
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.stopgap_enabled())
        self.assertFalse([c for c in self.systemctl_calls() if c.startswith(("enable", "disable"))])

    def test_a_stopgap_that_cannot_be_disabled_is_reported_and_the_install_finishes(self):
        (self.root / "systemctl-fails").touch()
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("disabling typec-sink-host.service FAILED; do it by hand before the reboot: "
                      "sudo systemctl disable typec-sink-host.service", result.stdout)
        self.assertIn("Type-C roles kit installed; reboot now", result.stdout)
        self.assertIn("Failed to disable unit", self.log())
        self.assertTrue((self.root / BACKUP / "complete").exists())
        # It was not disabled, so rollback has nothing to enable.
        self.assertFalse((self.root / BACKUP / "stopgap-was-enabled").exists())
        self.assertEqual(self.part("boot").read_bytes(), self.new_boot)

    def test_rollback_reports_a_stopgap_it_could_not_enable_and_still_finishes(self):
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        (self.root / "systemctl-fails").touch()
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("enabling typec-sink-host.service failed; do it by hand: "
                      "sudo systemctl enable typec-sink-host.service", result.stdout)
        self.assertIn("back on release v1.2.5", result.stdout)
        self.assertEqual(self.part("boot").read_bytes(), self.base_boot)
        self.assertEqual(self.tree(), {n: f"release {n}\n" for n in MODULES})

    def test_rollback_restores_the_release_and_install_runs_again(self):
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("back on release v1.2.5; reboot now", result.stdout)
        self.assertEqual(self.part("boot").read_bytes(), self.base_boot)
        self.assertEqual(self.part("vendor_boot").read_bytes(), self.base_vendor_boot)
        self.assertEqual(self.tree(), {n: f"release {n}\n" for n in MODULES})
        self.assertFalse((self.root / BACKUP).exists())
        retired = list((self.root / "var/backups").glob("ubuntu-gts9u-typec-roles.rolled-back-*"))
        self.assertEqual(len(retired), 1)
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        self.assertEqual(self.part("boot").read_bytes(), self.new_boot)
        self.assertFalse(self.stopgap_enabled())

    def test_install_records_what_it_wrote(self):
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        backup = self.root / BACKUP
        self.assertEqual((backup / "installed-boot").read_text(), sha(self.new_boot) + "\n")
        self.assertEqual(sorted(p.name for p in backup.iterdir()),
                         ["boot-release.img", "complete", "installed-boot",
                          f"modules-{RELEASE}", "stopgap-was-enabled"])

    def saved_set(self, boot):
        """A saved Ubuntu boot set (dual boot) whose boot.img is BOOT."""
        saved = self.root / "var/lib/gts9u-boot-sets/ubuntu"
        saved.mkdir(parents=True)
        (saved / "boot.img").write_bytes(boot)
        return saved

    def test_saved_boot_set_follows_install_and_rollback(self):
        # Not the release's image: what goes back is what the set held.
        found = os.urandom(SIZE)
        saved = self.saved_set(found)
        (saved / "vendor_boot.img").write_bytes(self.base_vendor_boot)
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        self.assertEqual((saved / "boot.img").read_bytes(), self.new_boot)
        self.assertEqual((saved / "vendor_boot.img").read_bytes(), self.base_vendor_boot)
        self.assertEqual((self.root / BACKUP / "saved-set-boot.img").read_bytes(), found)
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("saved Ubuntu boot set: boot.img restored (from saved-set-boot.img)",
                      result.stdout)
        self.assertEqual((saved / "boot.img").read_bytes(), found)
        self.assertEqual(sorted(p.name for p in saved.iterdir()), ["boot.img", "vendor_boot.img"])

    def test_an_empty_copy_of_the_saved_set_s_image_is_replaced_by_the_release_s(self):
        """What a power failure right after install.sh can leave of the copy."""
        saved = self.saved_set(self.base_boot)
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        (self.root / BACKUP / "saved-set-boot.img").write_bytes(b"")
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("saved Ubuntu boot set: boot.img restored (from boot-release.img)",
                      result.stdout)
        self.assertEqual((saved / "boot.img").read_bytes(), self.base_boot)

    def test_rollback_finishes_when_the_saved_boot_set_is_gone(self):
        found = os.urandom(SIZE)
        saved = self.saved_set(found)
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        shutil.rmtree(saved)
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"saved Ubuntu boot set: {saved} is gone; its boot.img stays in the "
                      "retired backup", result.stdout)
        self.assertIn("back on release v1.2.5; reboot now", result.stdout)
        self.assertFalse(saved.exists())
        (retired,) = (self.root / "var/backups").glob("ubuntu-gts9u-typec-roles.rolled-back-*")
        self.assertEqual((retired / "saved-set-boot.img").read_bytes(), found)

    def test_a_rollback_that_cannot_restore_the_saved_set_says_what_is_done_and_runs_again(self):
        saved = self.saved_set(self.base_boot)
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        # cp puts its copy inside the directory, and mv does not put a
        # directory in a file's place.
        (saved / "boot.img.new").mkdir()
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"STOP: restoring {saved}/boot.img failed", result.stdout)
        self.assertIn("STOP: rollback ended early; the release kernel and its modules are back in "
                      "place; run rollback.sh again to finish", result.stdout)
        self.assertNotIn("do NOT reboot", result.stdout)
        self.assertEqual(self.part("boot").read_bytes(), self.base_boot)
        self.assertEqual(self.tree(), {n: f"release {n}\n" for n in MODULES})
        self.assertEqual((saved / "boot.img").read_bytes(), self.new_boot)
        self.assertFalse(self.stopgap_enabled())
        self.assertTrue((self.root / BACKUP).exists())
        shutil.rmtree(saved / "boot.img.new")
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("back on release v1.2.5; reboot now", result.stdout)
        self.assertEqual((saved / "boot.img").read_bytes(), self.base_boot)
        self.assertEqual([p.name for p in saved.iterdir()], ["boot.img"])
        self.assertTrue(self.stopgap_enabled())
        self.assertFalse((self.root / BACKUP).exists())

    def test_install_syncs_the_saved_image_then_the_modules_then_the_finished_kit(self):
        """What a power failure right after each sync would find: the way
        back before anything else changes, the kit's modules before its
        kernel is written, and the install as it ends."""
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        base, new = sha(self.base_boot), sha(self.new_boot)
        self.assertEqual(self.syncs(), [
            {"saved": "yes", "tree": "release", "boot": base, "complete": "no", "upgrading": "no"},
            {"saved": "yes", "tree": "new", "boot": base, "complete": "no", "upgrading": "no"},
            {"saved": "yes", "tree": "new", "boot": new, "complete": "yes", "upgrading": "no"},
        ])

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
                self.assertEqual(self.part("boot").read_bytes(), self.base_boot)
                self.assertFalse((self.root / BACKUP / "complete").exists())
                self.assertTrue(self.stopgap_enabled())
                result = self.run_script("rollback.sh")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.part("boot").read_bytes(), self.base_boot)
                self.assertEqual(self.tree(), {n: f"release {n}\n" for n in MODULES})
                self.assertFalse((self.root / BACKUP).exists())
                self.assertTrue(self.stopgap_enabled())

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
        self.assertFalse(self.stopgap_enabled())

    def test_rollback_syncs_the_release_before_the_rest_and_again_at_the_end(self):
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        self.syncs()
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        release = {"tree": "release", "boot": sha(self.base_boot), "upgrading": "no"}
        self.assertEqual(self.syncs(), [
            # The backup is still where a second run would look for it.
            {**release, "saved": "yes", "complete": "yes"},
            {**release, "saved": "no", "complete": "no"},
        ])

    def test_a_rollback_that_stops_says_so_and_a_second_run_finishes_it(self):
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        result = self.run_with_failing_sync("rollback.sh", 1)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("STOP: sync failed", result.stdout)
        self.assertIn("STOP: rollback ended early; do NOT reboot before running rollback.sh again",
                      result.stdout)
        # The kernel and its modules are back; the rest is not.
        self.assertEqual(self.part("boot").read_bytes(), self.base_boot)
        self.assertEqual(self.tree(), {n: f"release {n}\n" for n in MODULES})
        self.assertTrue((self.root / BACKUP).exists())
        self.assertFalse(self.stopgap_enabled())
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("back on release v1.2.5; reboot now", result.stdout)
        self.assertNotIn("STOP", result.stdout)
        self.assertEqual(self.part("boot").read_bytes(), self.base_boot)
        self.assertEqual(self.tree(), {n: f"release {n}\n" for n in MODULES})
        self.assertFalse((self.root / BACKUP).exists())
        self.assertTrue(self.stopgap_enabled())

    def test_rollback_s_last_sync_failing_is_reported_with_the_release_in_place(self):
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        result = self.run_with_failing_sync("rollback.sh", 2)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('STOP: the release is back in place, but sync failed: run "sudo sync" until '
                      "it succeeds, then reboot", result.stdout)
        self.assertNotIn("ended early", result.stdout)
        self.assertNotIn("reboot now", result.stdout)
        self.assertEqual(self.part("boot").read_bytes(), self.base_boot)
        self.assertEqual(self.tree(), {n: f"release {n}\n" for n in MODULES})
        self.assertFalse((self.root / BACKUP).exists())
        self.assertTrue(self.stopgap_enabled())

    def test_refusals_change_nothing(self):
        cases = {
            "expected release v1.2.5's": lambda: self.running(NEW_BUILD_ID),
            "the boot partition does not hold release v1.2.5":
                lambda: self.part("boot").write_bytes(self.new_boot),
            f"the boot partition is not {SIZE} bytes":
                lambda: self.part("boot").write_bytes(self.base_boot + bytes(4096)),
            "does not match its checksums":
                lambda: (self.kit / "BUILD_ID").write_text("0" * 40 + "\n"),
            "does not have the release's list of modules":
                lambda: (self.modules / "updates/by-hand.ko").write_text("release extra\n"),
            "battery at 12 %":
                lambda: (self.root / "sys/class/power_supply/battery/capacity").write_text("12\n"),
            "this is not an SM-X910":
                lambda: (self.root / "proc/device-tree/compatible").write_bytes(
                    b"samsung,gts9wifi\0qcom,sm8550\0"),
            "the root filesystem is not writable":
                lambda: (self.root / "mount-options").write_text("ro,relatime\n"),
            "dpkg is busy": lambda: (self.root / "dpkg-busy").touch(),
        }
        for message, damage in cases.items():
            with self.subTest(message):
                FakeTablet.setUp(self)
                damage()
                boot_before = self.part("boot").read_bytes()
                result = self.run_script("install.sh")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(message, result.stdout)
                self.assertEqual(self.part("boot").read_bytes(), boot_before)
                self.assertEqual(self.part("vendor_boot").read_bytes(), self.base_vendor_boot)
                self.assertEqual(self.tree()["updates/b.ko"], "release updates/b.ko\n")
                self.assertFalse((self.root / BACKUP).exists())
                self.assertTrue(self.stopgap_enabled())
                self.assertFalse([c for c in self.systemctl_calls() if c.startswith("disable")])

    def test_a_refusal_names_the_image_it_found(self):
        self.part("boot").write_bytes(self.new_boot)
        result = self.run_script("install.sh")
        self.assertIn(f"the boot partition does not hold release v1.2.5 ({sha(self.new_boot)})",
                      result.stdout)

    def test_vendor_boot_from_another_build_is_refused(self):
        other = os.urandom(SIZE)
        self.part("vendor_boot").write_bytes(other)
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"vendor_boot does not hold release v1.2.5 ({sha(other)})", result.stdout)
        self.assertEqual(self.part("boot").read_bytes(), self.base_boot)
        self.assertEqual(self.part("vendor_boot").read_bytes(), other)
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
        self.assert_untouched()

    def test_rollback_retires_an_install_that_stopped_before_changing_anything(self):
        (self.root / BACKUP).mkdir()
        refused = self.run_script("install.sh")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("did not finish; run rollback.sh first", refused.stdout)
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("stopped before it changed anything", result.stdout)
        self.assertFalse((self.root / BACKUP).exists())
        self.assertEqual(self.part("boot").read_bytes(), self.base_boot)
        self.assertEqual(self.run_script("install.sh").returncode, 0)

    def test_rollback_undoes_an_install_that_stopped_after_the_module_swap(self):
        """The state a failed flash leaves: boot saved, the new module tree in
        place, no "complete"."""
        backup = self.root / BACKUP
        backup.mkdir()
        (backup / "boot-release.img").write_bytes(self.base_boot)
        self.modules.rename(backup / f"modules-{RELEASE}")
        (self.modules / "updates").mkdir(parents=True)
        (self.modules / "updates/b.ko").write_text("new updates/b.ko\n")
        self.part("boot").write_bytes(self.new_boot[:SIZE // 2] + self.base_boot[SIZE // 2:])
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.part("boot").read_bytes(), self.base_boot)
        self.assertEqual(self.tree(), {n: f"release {n}\n" for n in MODULES})
        self.assertFalse(backup.exists())
        # install.sh had not reached the stopgap.
        self.assertTrue(self.stopgap_enabled())
        self.assertFalse([c for c in self.systemctl_calls() if c.startswith("enable")])

    def test_rollback_refuses_a_changed_saved_image_after_the_flash(self):
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        (self.root / BACKUP / "boot-release.img").write_bytes(os.urandom(SIZE))
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing or changed; nothing was changed", result.stdout)
        self.assertEqual(self.part("boot").read_bytes(), self.new_boot)
        self.assertEqual(self.tree()["updates/b.ko"], "new updates/b.ko\n")
        self.assertFalse(self.stopgap_enabled())


class UpgradeTest(FakeTablet):
    """install.sh of one revision of the kit on a tablet that has another."""

    def install_first(self):
        """A revision of the kit installed and booted."""
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.running(NEW_BUILD_ID)
        self.backup = self.root / BACKUP

    def assert_upgraded(self, result, found):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"upgrade: boot holds {sha(found)[:12]}; the release saved in", result.stdout)
        self.assertIn("Type-C roles kit installed; reboot now", result.stdout)
        self.assertEqual(self.part("boot").read_bytes(), self.newer_boot)
        self.assertEqual(self.part("vendor_boot").read_bytes(), self.base_vendor_boot)
        self.assertEqual(self.tree(), {n: f"newer {n}\n" for n in MODULES})
        # The release is still what the backup holds.
        self.assertEqual((self.backup / "boot-release.img").read_bytes(), self.base_boot)
        self.assertEqual({str(p.relative_to(self.backup / f"modules-{RELEASE}")): p.read_text()
                          for p in (self.backup / f"modules-{RELEASE}").rglob("*") if p.is_file()},
                         {n: f"release {n}\n" for n in MODULES})
        self.assertEqual([p.name for p in (self.root / "usr/lib/modules").iterdir()], [RELEASE])
        self.assertEqual((self.backup / "installed-boot").read_text(), sha(self.newer_boot) + "\n")
        self.assertFalse((self.backup / "upgrading").exists())
        self.assertTrue((self.backup / "complete").exists())

    def assert_rolled_back(self):
        result = self.run_script("rollback.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("back on release v1.2.5; reboot now", result.stdout)
        self.assertEqual(self.part("boot").read_bytes(), self.base_boot)
        self.assertEqual(self.part("vendor_boot").read_bytes(), self.base_vendor_boot)
        self.assertEqual(self.tree(), {n: f"release {n}\n" for n in MODULES})
        self.assertEqual([p.name for p in (self.root / "usr/lib/modules").iterdir()], [RELEASE])
        self.assertFalse(self.backup.exists())
        self.assertTrue(self.stopgap_enabled())

    def test_a_newer_revision_installs_over_an_installed_one(self):
        self.install_first()
        self.newer_kit()
        result = self.run_script("install.sh")
        self.assert_upgraded(result, self.new_boot)
        self.assertNotIn("STOP", self.log())
        # The first install disabled the stopgap; it stays disabled, and what
        # rollback.sh needs to know about it is kept.
        self.assertFalse(self.stopgap_enabled())
        self.assertTrue((self.backup / "stopgap-was-enabled").exists())
        self.assertEqual(self.systemctl_calls().count("disable typec-sink-host.service"), 1)
        self.assert_rolled_back()

    def test_an_upgrade_does_not_ask_which_kernel_is_running(self):
        """The kit's kernel is running, or the release's after a restore from
        TWRP, or one of a revision before the last."""
        for build_id in (BASE_BUILD_ID, NEW_BUILD_ID, "f" * 40):
            with self.subTest(build_id):
                FakeTablet.setUp(self)
                self.install_first()
                self.running(build_id)
                self.newer_kit()
                self.assert_upgraded(self.run_script("install.sh"), self.new_boot)

    def test_revision_3_left_no_record_and_is_known_by_its_image(self):
        self.r3_boot = self.new_boot
        self.install_first()
        (self.backup / "installed-boot").unlink()
        self.newer_kit()
        self.assert_upgraded(self.run_script("install.sh"), self.new_boot)
        self.assert_rolled_back()

    def test_an_image_without_a_record_that_is_not_revision_3_is_refused(self):
        self.install_first()
        (self.backup / "installed-boot").unlink()
        self.newer_kit()
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("the boot partition holds neither release v1.2.5 nor a revision of this kit "
                      f"({sha(self.new_boot)})", result.stdout)
        self.assertEqual(self.part("boot").read_bytes(), self.new_boot)
        self.assertEqual(self.tree(), {n: f"new {n}\n" for n in MODULES})
        self.assertFalse((self.backup / "upgrading").exists())

    def test_the_same_revision_twice_is_refused(self):
        self.install_first()
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("this revision of the Type-C roles kit is already installed", result.stdout)
        self.assertIn("nothing was changed", result.stdout)
        self.assertNotIn("ended early", result.stdout)
        self.assertEqual(self.part("boot").read_bytes(), self.new_boot)
        self.assertEqual(self.tree(), {n: f"new {n}\n" for n in MODULES})
        self.assertFalse((self.backup / "upgrading").exists())
        self.assertEqual((self.backup / "installed-boot").read_text(), sha(self.new_boot) + "\n")

    def test_this_revision_on_boot_under_another_record_is_installed_again(self):
        """boot written from TWRP, say: the module tree may be another's."""
        self.install_first()
        self.newer_kit()
        self.part("boot").write_bytes(self.newer_boot)
        self.assert_upgraded(self.run_script("install.sh"), self.newer_boot)

    def test_an_earlier_revision_goes_back_on_the_same_way(self):
        self.install_first()
        self.newer_kit()
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        # The kit directory holds the first revision again.
        (self.kit / "boot.img").write_bytes(self.new_boot)
        (self.kit / f"modules-{RELEASE}.tar").write_bytes(modules_tar("new"))
        (self.kit / "BUILD_ID").write_text(NEW_BUILD_ID + "\n")
        self.seal()
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"upgrade: boot holds {sha(self.newer_boot)[:12]}", result.stdout)
        self.assertEqual(self.part("boot").read_bytes(), self.new_boot)
        self.assertEqual(self.tree(), {n: f"new {n}\n" for n in MODULES})
        self.assertEqual((self.backup / "installed-boot").read_text(), sha(self.new_boot) + "\n")
        self.assert_rolled_back()

    def test_an_unknown_image_on_boot_is_refused(self):
        self.install_first()
        other = os.urandom(SIZE)
        self.part("boot").write_bytes(other)
        self.newer_kit()
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("the boot partition holds neither release v1.2.5 nor a revision of this kit "
                      f"({sha(other)})", result.stdout)
        self.assertEqual(self.part("boot").read_bytes(), other)
        self.assertEqual(self.tree(), {n: f"new {n}\n" for n in MODULES})
        self.assertFalse((self.backup / "upgrading").exists())
        self.assertEqual((self.backup / "installed-boot").read_text(), sha(self.new_boot) + "\n")

    def test_the_release_on_boot_is_upgraded_too(self):
        """After the way back from TWRP: boot holds the release again, the
        module tree and the backup are still the kit's."""
        self.install_first()
        self.part("boot").write_bytes(self.base_boot)
        self.running(BASE_BUILD_ID)
        self.newer_kit()
        self.assert_upgraded(self.run_script("install.sh"), self.base_boot)
        self.assert_rolled_back()

    def test_upgrade_refusals_change_nothing(self):
        saved = BACKUP + "/boot-release.img"
        cases = {
            "saved release image changed": (
                "the release boot image saved in",
                lambda: (self.root / saved).write_bytes(os.urandom(SIZE))),
            "saved release image missing": (
                "is missing or changed; nothing was changed",
                lambda: (self.root / saved).unlink()),
            "saved release module tree missing": (
                "the release module tree saved in",
                lambda: shutil.rmtree(self.root / BACKUP / f"modules-{RELEASE}")),
            "kit changed after it was packed": (
                "the kit does not match its checksums; nothing was changed",
                lambda: (self.kit / "BUILD_ID").write_text("0" * 40 + "\n")),
            "a module added by hand": (
                "does not have the release's list of modules",
                lambda: (self.modules / "updates/by-hand.ko").write_text("new extra\n")),
            "vendor_boot of another build": (
                "vendor_boot does not hold release v1.2.5",
                lambda: self.part("vendor_boot").write_bytes(os.urandom(SIZE))),
            "low battery": (
                "battery at 12 %",
                lambda: (self.root / "sys/class/power_supply/battery/capacity").write_text("12\n")),
            "read-only root": (
                "the root filesystem is not writable",
                lambda: (self.root / "mount-options").write_text("ro,relatime\n")),
            "dpkg at work": ("dpkg is busy", lambda: (self.root / "dpkg-busy").touch()),
            "another tablet": (
                "this is not an SM-X910",
                lambda: (self.root / "proc/device-tree/compatible").write_bytes(
                    b"samsung,gts9wifi\0qcom,sm8550\0")),
        }
        for name, (message, damage) in cases.items():
            with self.subTest(name):
                FakeTablet.setUp(self)
                self.install_first()
                self.newer_kit()
                damage()
                result = self.run_script("install.sh")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(message, result.stdout)
                self.assertNotIn("ended early", result.stdout)
                self.assertEqual(self.part("boot").read_bytes(), self.new_boot)
                self.assertEqual(self.tree()["updates/b.ko"], "new updates/b.ko\n")
                self.assertFalse((self.backup / "upgrading").exists())
                self.assertTrue((self.backup / "complete").exists())
                self.assertEqual((self.backup / "installed-boot").read_text(),
                                 sha(self.new_boot) + "\n")
                self.assertFalse(
                    (self.root / "usr/lib/modules" / f".typec-roles-{RELEASE}").exists())

    def stop_in_the_module_swap(self):
        """An upgrade that stops between the module swap and the flash."""
        self.install_first()
        self.newer_kit()
        (self.root / "depmod-fails").touch()
        result = self.run_script("install.sh")
        (self.root / "depmod-fails").unlink()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("STOP: depmod failed", result.stdout)
        self.assertIn("STOP: the upgrade ended early; do NOT reboot before running install.sh "
                      "again, or rollback.sh", result.stdout)
        self.assertTrue((self.backup / "upgrading").exists())
        self.assertEqual(self.part("boot").read_bytes(), self.new_boot)
        self.assertEqual(self.tree(), {n: f"newer {n}\n" for n in MODULES})
        self.assertEqual((self.backup / "installed-boot").read_text(), sha(self.new_boot) + "\n")

    def test_an_upgrade_that_stopped_is_finished_by_running_it_again(self):
        self.stop_in_the_module_swap()
        self.assert_upgraded(self.run_script("install.sh"), self.new_boot)
        self.assert_rolled_back()

    def test_an_upgrade_that_stopped_in_the_flash_is_finished_by_running_it_again(self):
        """boot half-written is no image the script knows; it is written again
        all the same, and so is one that was written whole."""
        for name, written in (("half", lambda: self.newer_boot[:SIZE // 2] + self.new_boot[SIZE // 2:]),
                              ("whole", lambda: self.newer_boot)):
            with self.subTest(name):
                FakeTablet.setUp(self)
                self.stop_in_the_module_swap()
                found = written()
                self.part("boot").write_bytes(found)
                result = self.run_script("install.sh")
                self.assertNotIn("already installed", result.stdout)
                self.assert_upgraded(result, found)

    def test_rollback_undoes_an_upgrade_that_stopped(self):
        self.stop_in_the_module_swap()
        left = self.root / "usr/lib/modules" / f".typec-roles-old-{RELEASE}"
        left.mkdir()
        (left / "a.ko").write_text("new a.ko\n")
        self.assert_rolled_back()
        self.assertEqual(len(list((self.root / "var/backups").glob("*.rolled-back-*"))), 1)

    def stop_between_the_two_moves(self):
        """An upgrade that stops with the earlier kit's module tree moved
        aside and the new one not yet in its place."""
        self.install_first()
        self.newer_kit()
        (self.backup / "upgrading").touch()
        self.modules.rename(self.root / "usr/lib/modules" / f".typec-roles-old-{RELEASE}")
        stage = self.root / "usr/lib/modules" / f".typec-roles-{RELEASE}"
        stage.mkdir()
        (stage / "modules.dep").write_text("newer modules.dep\n")

    def test_an_upgrade_that_stopped_between_the_two_moves_is_finished_by_running_it_again(self):
        self.stop_between_the_two_moves()
        result = self.run_script("install.sh")
        self.assertIn("an upgrade had stopped in the module swap: the earlier module tree is back "
                      f"in {self.modules}", result.stdout)
        self.assertNotIn("STOP", result.stdout)
        self.assert_upgraded(result, self.new_boot)
        self.assert_rolled_back()

    def test_an_upgrade_that_stopped_between_the_two_moves_is_undone_by_rollback(self):
        self.stop_between_the_two_moves()
        self.assert_rolled_back()

    def test_a_missing_module_tree_with_nothing_to_put_back_is_left_to_rollback(self):
        old = f".typec-roles-old-{RELEASE}"
        cases = {
            # Not this script's doing: it moves the tree only under its mark.
            "set aside, and no upgrade under way": lambda: self.modules.rename(
                self.modules.parent / old),
            "set aside without its modules.dep": lambda: (
                (self.backup / "upgrading").touch(),
                self.modules.rename(self.modules.parent / old),
                (self.modules.parent / old / "modules.dep").unlink()),
            "gone": lambda: ((self.backup / "upgrading").touch(), shutil.rmtree(self.modules)),
        }
        for name, damage in cases.items():
            with self.subTest(name):
                FakeTablet.setUp(self)
                self.install_first()
                self.newer_kit()
                damage()
                result = self.run_script("install.sh")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(f"STOP: no module tree {self.modules}; run rollback.sh", result.stdout)
                self.assertNotIn("ended early", result.stdout)
                self.assertFalse(self.modules.exists())
                self.assertEqual(self.part("boot").read_bytes(), self.new_boot)
                self.assert_rolled_back()

    def test_an_upgrade_syncs_the_new_modules_before_the_flash_and_again_at_the_end(self):
        self.install_first()
        self.newer_kit()
        self.syncs()
        self.assert_upgraded(self.run_script("install.sh"), self.new_boot)
        kept = {"saved": "yes", "tree": "newer", "complete": "yes"}
        self.assertEqual(self.syncs(), [
            {**kept, "boot": sha(self.new_boot), "upgrading": "yes"},
            {**kept, "boot": sha(self.newer_boot), "upgrading": "no"},
        ])

    def test_an_upgrade_whose_sync_fails_before_the_flash_is_finished_by_running_it_again(self):
        self.install_first()
        self.newer_kit()
        result = self.run_with_failing_sync("install.sh", 1)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("STOP: sync failed", result.stdout)
        self.assertIn("STOP: the upgrade ended early; do NOT reboot before running install.sh "
                      "again, or rollback.sh", result.stdout)
        self.assertNotIn("writing boot", result.stdout)
        self.assertEqual(self.part("boot").read_bytes(), self.new_boot)
        self.assertTrue((self.backup / "upgrading").exists())
        self.assertEqual((self.backup / "installed-boot").read_text(), sha(self.new_boot) + "\n")
        self.assert_upgraded(self.run_script("install.sh"), self.new_boot)
        self.assert_rolled_back()

    def test_an_upgrade_s_last_sync_failing_is_reported_with_the_kit_in_place(self):
        self.install_first()
        self.newer_kit()
        result = self.run_with_failing_sync("install.sh", 2)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('STOP: the kit is in place, but sync failed: run "sudo sync" until it '
                      "succeeds, then reboot", result.stdout)
        self.assertNotIn("ended early", result.stdout)
        self.assertNotIn("reboot now", result.stdout)
        self.assertEqual(self.part("boot").read_bytes(), self.newer_boot)
        self.assertEqual(self.tree(), {n: f"newer {n}\n" for n in MODULES})
        self.assertFalse((self.backup / "upgrading").exists())
        self.assertEqual((self.backup / "installed-boot").read_text(), sha(self.newer_boot) + "\n")
        self.assert_rolled_back()

    def test_the_saved_boot_set_follows_an_upgrade(self):
        saved = self.root / "var/lib/gts9u-boot-sets/ubuntu"
        saved.mkdir(parents=True)
        (saved / "boot.img").write_bytes(self.base_boot)
        self.install_first()
        self.newer_kit()
        self.assert_upgraded(self.run_script("install.sh"), self.new_boot)
        self.assertEqual((saved / "boot.img").read_bytes(), self.newer_boot)
        # What rollback.sh restores is what the first install found.
        self.assertEqual((self.backup / "saved-set-boot.img").read_bytes(), self.base_boot)
        self.assert_rolled_back()
        self.assertEqual((saved / "boot.img").read_bytes(), self.base_boot)
        self.assertEqual([p.name for p in saved.iterdir()], ["boot.img"])

    def test_a_boot_set_saved_since_the_first_install_goes_back_to_the_release(self):
        self.install_first()
        saved = self.root / "var/lib/gts9u-boot-sets/ubuntu"
        saved.mkdir(parents=True)
        (saved / "boot.img").write_bytes(self.new_boot)
        self.newer_kit()
        self.assert_upgraded(self.run_script("install.sh"), self.new_boot)
        self.assertEqual((saved / "boot.img").read_bytes(), self.newer_boot)
        self.assertEqual((self.backup / "saved-set-boot.img").read_bytes(), self.base_boot)
        self.assert_rolled_back()
        self.assertEqual((saved / "boot.img").read_bytes(), self.base_boot)


class VerifyTest(FakeTablet):
    def setUp(self):
        super().setUp()
        self.stopgap(enabled=False, active=False)
        self.port = self.root / "sys/class/typec/port0"
        self.partner = self.root / "sys/class/typec/port0-partner"
        self.tcpm = self.root / "sys/kernel/debug/usb/tcpm-8-0033/log"
        self.tcpm.write_text("")
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
        self.assertEqual(self.cat(self.tcpm), PASS_LOG + SWAP_EARLY_LOG)
        self.assertEqual(self.cat(self.tcpm), "")
        full = dock_messages(500_000_000, 1023)
        self.log_tcpm(full)
        self.assertEqual(self.cat(self.tcpm), full)
        self.log_tcpm(full + dock_messages(500_001_023, 2))
        self.assertEqual(self.cat(self.tcpm), "".join(full.splitlines(True)[:1022])
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
        (kernel,) = (self.kit / "logs").glob("*/kernel")
        self.assertIn(f"build ID: {BASE_BUILD_ID} (the kit's: {NEW_BUILD_ID})", kernel.read_text())
        self.assertIn("sink_host_swap: absent", kernel.read_text())
        self.assertIn("tcpm-8-0033/log", kernel.read_text())

    def test_the_recorder_does_not_run_next_to_the_stopgap(self):
        """The daemon writes the data role itself; a trial next to it would
        record the daemon, not the kernel."""
        self.stopgap(active=True)
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
        (revision 3), and kept after the Rp went away and came back."""
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
        """The kit's kernel as it boots: its build ID, the driver's twelve
        switches at their defaults (revision 10's driver), msm and TCPM
        built in, the kernel log, 2 h 21 min up."""
        self.running(NEW_BUILD_ID)
        params = self.root / "sys/module/sm5714_usbpd/parameters"
        for name, value in SWITCHES.items():
            (params / name).write_text(value + "\n")
        (self.modules / "modules.builtin").write_text(
            "kernel/drivers/pinctrl/qcom/pinctrl-msm.ko\nkernel/drivers/gpu/drm/msm/msm.ko\n"
            "kernel/drivers/usb/typec/tcpm/tcpm.ko\nkernel/drivers/usb/typec/tcpm/sm5714_usbpd.ko\n")
        (self.root / "proc/uptime").write_text("8460.12 30217.45\n")
        (self.root / "journal").write_text(
            f"kernel: Loaded X.509 cert 'Build time autogenerated kernel key: {KEY_ID}'\n")
        (self.root / "signer").write_text(SIGNER + "\n")

    def test_checks_pass_on_the_kit_kernel(self):
        self.boot_kit()
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("FAIL", result.stdout)
        self.assertIn("ok   this is an SM-X910", result.stdout)
        for name, value in SWITCHES.items():
            self.assertIn(f"ok   sm5714_usbpd.{name} = {value}\n", result.stdout)
        self.assertIn("ok   2 modules, all signed with the kit's key", result.stdout)
        self.assertIn("ok   the typec-sink-host stopgap is neither running nor enabled", result.stdout)
        self.assertIn(f"ok   the kernel trusts the kit's module key ({KEY_ID[:12]})", result.stdout)
        self.assertIn("ok   no module was refused, and no symbol failures this boot", result.stdout)
        # A controller that has not been through a suspend has shown nothing.
        self.assertIn("note the USB host controller did not die, but no suspend cycle has run yet "
                      "this boot: run this again later", result.stdout)
        self.assertNotIn("ok   the USB host controller", result.stdout)
        # Nothing is attached, so there is no bus to count devices on.
        self.assertNotIn("on the bus", result.stdout)
        self.assertIn("all checks passed", result.stdout)

    def journal_add(self, text):
        with (self.root / "journal").open("a") as journal:
            journal.write(text)

    def test_checks_know_the_controller_s_other_ways_of_dying(self):
        for line in USB_HOST_ERRORS:
            with self.subTest(line):
                self.boot_kit()
                self.journal_add("blastoise kernel: PM: suspend entry (deep)\n"
                                 f"{line}\nblastoise kernel: PM: suspend exit\n")
                result = self.run_script("verify.sh")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(f"FAIL the USB host controller died this boot:\n{line}\n",
                              result.stdout)
                self.assertEqual(result.stdout.count("FAIL "), 1, result.stdout)
                self.assertNotIn("the USB host controller did not die", result.stdout)

    def test_a_controller_that_was_set_up_again_after_a_suspend_is_noted_and_does_not_fail(self):
        self.boot_kit()
        self.journal_add("blastoise kernel: PM: suspend entry (deep)\n"
                         f"{USB_HOST_REINIT}\nblastoise kernel: PM: suspend exit\n")
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("note the USB host controller came back from a suspend without its state and "
                      "was set up again (times this boot: 1)", result.stdout)
        self.assertIn("ok   the USB host controller did not die this boot (suspend cycles so far: 1)",
                      result.stdout)
        self.assertNotIn("FAIL", result.stdout)

    def test_checks_fail_when_the_kernel_refused_a_module(self):
        """Modules of another build under the kit's kernel, or the kit's under
        the release's: the locked-down kernel loads none of them."""
        self.boot_kit()
        self.journal_add(MODULE_REFUSED + "\n")
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"FAIL modules refused or symbol failures this boot:\n{MODULE_REFUSED}\n",
                      result.stdout)
        self.assertEqual(result.stdout.count("FAIL "), 1, result.stdout)
        self.assertNotIn("no module was refused", result.stdout)

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
                              "module key, the module failures, the USB host controller, L15B and "
                              "revision 8's kernel-log checks were not checked\n", result.stdout)
                self.assertEqual(result.stdout.count("FAIL "), 1, result.stdout)
                for claim in ("the kernel trusts", "Loaded X.509", "no module was refused",
                              "symbol failures this boot", "the USB host controller did not die",
                              "the USB host controller died", "DT: L15B", "kernel log: gts9u-performance",
                              "ramoops:", "PCIe power domain guard", "Bluetooth:"):
                    self.assertNotIn(claim, result.stdout)
                # What does not need the log is still checked.
                self.assertIn("ok   2 modules, all signed with the kit's key", result.stdout)
                self.assertIn("ok   sm5714_usbpd.sink_host_swap = Y", result.stdout)

    def test_checks_fail_when_the_usb_host_controller_died(self):
        self.boot_kit()
        with (self.root / "journal").open("a") as journal:
            journal.write(USB_HOST_DIED)
        self.roles("host", "sink")
        self.partner.mkdir()
        (self.root / "sys/bus/usb/devices/1-1/idVendor").unlink()
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL the USB host controller died this boot:", result.stdout)
        for line in ("xHCI host not responding to stop endpoint command", "HC died; cleaning up",
                     "Host halt failed, -110"):
            self.assertIn(line, result.stdout)
        self.assertIn("note USB host with no device on the bus", result.stdout)
        self.assertEqual(result.stdout.count("FAIL "), 1, result.stdout)
        self.assertIn("some checks FAILED", result.stdout)

    def test_checks_count_the_suspend_cycles_and_the_devices_behind_a_dock(self):
        """A boot with the dock attached, as it should end: the boot's suspend
        cycle ran, the controller is alive and the hub is on the bus."""
        self.boot_kit()
        with (self.root / "journal").open("a") as journal:
            journal.write("blastoise kernel: PM: suspend entry (deep)\n"
                          "blastoise kernel: PM: suspend exit\n")
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
        """A charger that took the swap has nothing behind it."""
        self.boot_kit()
        self.roles("host", "sink")
        self.partner.mkdir()
        (self.root / "sys/bus/usb/devices/1-1/idVendor").unlink()
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("note USB host with no device on the bus (nothing behind the dock, "
                      "or its hub did not come up)", result.stdout)
        self.assertNotIn("FAIL", result.stdout)

    def test_checks_fail_on_the_release_kernel(self):
        self.boot_kit()
        self.running(BASE_BUILD_ID)
        for name in ("sink_host_swap", "try_snk_extra_wait_ms"):
            (self.root / "sys/module/sm5714_usbpd/parameters" / name).unlink()
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"FAIL build ID {BASE_BUILD_ID}", result.stdout)
        self.assertIn("FAIL sm5714_usbpd.sink_host_swap is missing", result.stdout)

    def test_checks_fail_while_the_stopgap_is_running_or_enabled(self):
        for state in ({"active": True}, {"enabled": True}):
            with self.subTest(state):
                self.stopgap(enabled=False, active=False)
                self.boot_kit()
                self.stopgap(**state)
                result = self.run_script("verify.sh")
                self.assertEqual(result.returncode, 1)
                self.assertIn("FAIL the typec-sink-host stopgap is running or enabled", result.stdout)
                self.assertEqual(result.stdout.count("FAIL "), 1, result.stdout)

    def test_checks_fail_with_modules_of_another_key_and_note_a_changed_switch(self):
        """Since revision 9 a switch off its default is a note (it was
        changed since the boot, by hand or by a swap run killed outright),
        not a FAIL of the kit; up to revision 8 it was a FAIL."""
        self.boot_kit()
        (self.root / "signer").write_text(":".join(["6B"] * 20) + "\n")
        (self.root / "sys/module/sm5714_usbpd/parameters/try_snk_extra_wait_ms").write_text("0\n")
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL 2 of 2 modules are not signed with the kit's key", result.stdout)
        self.assertIn("note sm5714_usbpd.try_snk_extra_wait_ms = 0, changed from its default 600 since the boot\n",
                      result.stdout)
        self.assertEqual(result.stdout.count("FAIL "), 1, result.stdout)

    def l15b(self, set_load=False, mode="normal"):
        """PM8550 LDO15 as the running DT and the regulator's sysfs show it:
        the node as the kernel's L15B step leaves it (no
        regulator-allow-set-load or regulator-allowed-modes), or with
        SET_LOAD the pinned DTB's node; MODE is its opmode."""
        node = self.root / "proc/device-tree" / L15B_NODE.lstrip("/")
        if node.exists():
            shutil.rmtree(node)
        node.mkdir(parents=True)
        (node / "regulator-name").write_bytes(b"vreg_l15b_1p8\0")
        (node / "regulator-initial-mode").write_bytes(bytes.fromhex("00000003"))
        if set_load:
            (node / "regulator-allow-set-load").write_bytes(b"")
            (node / "regulator-allowed-modes").write_bytes(bytes.fromhex("0000000100000003"))
        reg = self.root / "sys/class/regulator/regulator.22"
        reg.mkdir(parents=True, exist_ok=True)
        (reg / "name").write_text("vreg_l15b_1p8\n")
        (reg / "opmode").write_text(mode + "\n")

    # Revision 8's kernel as blastoise is to boot it: the early helper's
    # lines, ramoops, the DT the overlay leaves, rpmhpd, gcc and gpu_cc
    # synced, the interconnect held, qcom_stats bound, the debug switches as
    # they boot, the guard's line of the panel cycle, Bluetooth and the
    # console's tty, the sensors' hook of device 2.75.
    R8_LINES = ("gts9u-performance: SoC hand-off: gcc under CX, GMU post-init providers, crypto engine disabled",
                "gts9u-performance: ramoops in the pmsg carve-out at 0x880900000",
                "ramoops: using 0x200000@0x880900000, ecc: 96",
                "qcom-pcie 1c00000.pcie: system sleep: the link was kept, and its power domain stayed on",
                "PM: suspend exit")

    def boot_kit_r8(self):
        self.boot_kit()
        (self.kit / "REVISION").write_text("8\n")
        self.l15b()
        self.journal_add("".join(f"blastoise kernel: {line}\n" for line in (L15B_STEP,) + self.R8_LINES))
        F = self.root
        (F / "dev/pmsg0").write_text("")
        dt = F / "proc/device-tree/soc@0"
        for node, prop, value in (("clock-controller@100000", "power-domains", b"\0\0\0\x3a\0\0\0\0"),
                                  ("gmu@3d6a000", "post-init-providers", b"\0\0\0\x3b\0\0\0\x41"),
                                  ("crypto@1dfa000", "status", b"disabled\0")):
            (dt / node).mkdir(parents=True, exist_ok=True)
            (dt / node / prop).write_bytes(value)
        devices = F / "sys/bus/platform/devices"
        for d, synced in (("17a00000.rsc:power-controller", "1"), ("100000.clock-controller", "1"),
                          ("3d90000.clock-controller", "1"), ("24100000.interconnect", "0"),
                          ("interconnect-1", "0"), ("1700000.interconnect", "1")):
            (devices / d / "of_node").mkdir(parents=True, exist_ok=True)
            (devices / d / "state_synced").write_text(synced + "\n")
            if d.endswith("interconnect") or d.startswith("interconnect"):
                (devices / d / "of_node/#interconnect-cells").write_bytes(b"\0\0\0\x01")
        (F / "sys/bus/platform/drivers/qcom_stats/c3f0000.sram").mkdir(parents=True)
        for path, value in (("gcc_sm8550/parameters/debug_pcie0_off", "Y"),
                            ("gcc_sm8550/parameters/debug_pcie0_phy_off", "N"),
                            ("gcc_sm8550/parameters/debug_usb_off", "N"), ("rpmhpd/parameters/debug_ret_floor", "N"),
                            ("pcie_qcom/parameters/debug_root_port_d3", "N"),
                            ("qcom_stats/parameters/debug_cxpc_violators", "N")):
            (F / "sys/module" / path).parent.mkdir(parents=True, exist_ok=True)
            (F / "sys/module" / path).write_text(value + "\n")
        (F / "sys/class/bluetooth/hci0").mkdir(parents=True)
        (F / "sys/class/tty/ttyMSM0/power").mkdir(parents=True)
        (F / "sys/class/tty/ttyMSM0/power/wakeup").write_text("disabled\n")
        hook = F / "usr/lib/systemd/system-sleep/ubuntu-gts9u-sensors"
        hook.parent.mkdir(parents=True)
        hook.write_text("systemctl stop ubuntu-gts9u-sensors-resume.service iio-sensor-proxy.service\n")

    def test_revision_8_s_checks_pass_on_its_kernel(self):
        self.boot_kit_r8()
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("FAIL", result.stdout)
        for said in ("ok   kernel log: gts9u-performance: SoC hand-off: gcc under CX, GMU post-init providers, "
                     "crypto engine disabled",
                     "ok   kernel log: gts9u-performance: ramoops in the pmsg carve-out at 0x880900000",
                     "ok   ramoops: 0x200000@0x880900000, ECC 96, and its pmsg device",
                     "ok   DT: gcc under CX (power-domains), the GMU's post-init-providers, crypto@1dfa000 disabled",
                     "ok   sync_state: rpmhpd, gcc and gpu_cc synced at boot",
                     "note interconnect floor: held (unsynced: 24100000.interconnect interconnect-1), as every boot "
                     "of this board holds it until sleep-test.sh release-floor",
                     "ok   qcom_stats: bound to c3f0000.sram",
                     "ok   debug switch gcc_sm8550.debug_pcie0_off = Y, as it boots",
                     "ok   debug switch qcom_stats.debug_cxpc_violators = N, as it boots",
                     "ok   PCIe power domain guard: in this boot's first suspend (the panel cycle) the host kept its "
                     "link and pcie_0_gdsc stayed on",
                     "ok   Bluetooth: hci0, no disabled interrupt and no HCI command timeout this boot",
                     "ok   console ttyMSM0: wakeup disabled",
                     "ok   the sensors' sleep hook stops iio-sensor-proxy",
                     f"ok   DT: L15B (vreg_l15b_1p8) held in HPM"):
            with self.subTest(said):
                self.assertIn(said, result.stdout)
        self.assertIn("all checks passed", result.stdout)

    def test_each_of_revision_8_s_checks_fails_on_its_kernel_and_is_a_note_on_another(self):
        """A FAIL on revision 8's own kernel, a note on any other (revision 7
        and the release have none of it); a guard that did not hold or a
        Bluetooth UART's interrupt disabled is a FAIL on any kernel."""
        # self.root is looked up at each call: every subtest has a fresh fake.
        def drop_line(text):
            return lambda: (self.root / "journal").write_text(
                "".join(l for l in (self.root / "journal").read_text().splitlines(True) if text not in l))

        cases = {
            "no hand-off line": (drop_line("SoC hand-off"), 'no "gts9u-performance: SoC hand-off: gcc under CX'),
            "no ramoops line": (drop_line("ramoops in the pmsg"), 'no "gts9u-performance: ramoops in the pmsg'),
            "no ramoops": (drop_line("ramoops: using"), "no ramoops on 0x880900000 with ECC 96"),
            "no pmsg": (lambda: (self.root / "dev/pmsg0").unlink(), "no ramoops on 0x880900000 with ECC 96"),
            "gcc not under CX": (lambda: (self.root / "proc/device-tree/soc@0/clock-controller@100000/power-domains").unlink(),
                                 "DT: gcc has no power-domains"),
            "crypto enabled": (lambda: (self.root / "proc/device-tree/soc@0/crypto@1dfa000/status").write_bytes(b"okay\0"),
                               "DT: gcc has no power-domains"),
            "rpmhpd unsynced": (lambda: [(self.root / "sys/bus/platform/devices/17a00000.rsc:power-controller/state_synced")
                                         .write_text("0\n"),
                                         self.journal_add("blastoise kernel: qcom-rpmhpd 17a00000.rsc:power-controller: "
                                                          "sync_state() pending due to aaf0000.clock-controller\n")],
                                "sync_state: not synced: 17a00000.rsc:power-controller; "
                                "17a00000.rsc:power-controller waits for aaf0000.clock-controller"),
            "qcom_stats unbound": (lambda: (self.root / "sys/bus/platform/drivers/qcom_stats/c3f0000.sram").rmdir(),
                                   "qcom_stats: no device bound"),
            "a debug switch set": (lambda: (self.root / "sys/module/pcie_qcom/parameters/debug_root_port_d3").write_text("Y\n"),
                                   "debug switch pcie_qcom.debug_root_port_d3 is Y, expected N at boot"),
            "no guard line": (drop_line("system sleep:"),
                              "PCIe power domain guard: this boot's first suspend logged no \"system sleep:\" line"),
            "no Bluetooth": (lambda: (self.root / "sys/class/bluetooth/hci0").rmdir(), "no Bluetooth controller"),
            "tty wakeup on": (lambda: (self.root / "sys/class/tty/ttyMSM0/power/wakeup").write_text("enabled\n"),
                              "console ttyMSM0: wakeup not disabled"),
            "no L15B step": (drop_line("L15B held"), "DT: L15B (vreg_l15b_1p8) not checked: this kernel has no L15B step"),
        }
        for kernel in ("its", "another"):
            for name, (make, message) in cases.items():
                with self.subTest(kernel=kernel, case=name):
                    FakeTablet.setUp(self)
                    VerifyTest.setUp(self)
                    self.boot_kit_r8()
                    make()
                    if kernel == "another":
                        self.running(NEWER_BUILD_ID)
                    result = self.run_script("verify.sh")
                    word = "FAIL" if kernel == "its" else "note"
                    self.assertIn(f"{word} {message}", result.stdout)
                    if kernel == "its":
                        self.assertEqual(result.returncode, 1)
                    else:
                        # The build ID's own FAIL, and nothing of revision 8's.
                        self.assertNotIn(f"FAIL {message}", result.stdout)
        for name, line, message in (
                ("the guard did not hold", "qcom-pcie 1c00000.pcie: system sleep: the link was kept, but genpd "
                                           "powered its power domain off",
                 "FAIL the PCIe power domain guard did not hold this boot"),
                ("an interrupt disabled", "irq 196: nobody cared (try booting with the \"irqpoll\" option)",
                 "FAIL an interrupt was disabled or a Bluetooth command timed out this boot:")):
            for kernel in ("its", "another"):
                with self.subTest(kernel=kernel, case=name):
                    FakeTablet.setUp(self)
                    VerifyTest.setUp(self)
                    self.boot_kit_r8()
                    self.journal_add(f"blastoise kernel: {line}\n")
                    if kernel == "another":
                        self.running(NEWER_BUILD_ID)
                    result = self.run_script("verify.sh")
                    self.assertIn(message, result.stdout)

    def test_revision_7_and_the_release_get_notes_alone_from_revision_8_s_checks(self):
        """The kit without REVISION (revisions 1 to 7): every check of
        revision 8 that fails is a note, also on the kit's own kernel."""
        self.boot_kit()
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout)
        for message in ("no \"gts9u-performance: SoC hand-off", "DT: gcc has no power-domains",
                        "sync_state: not synced", "qcom_stats: no device bound",
                        "the sensors' sleep hook does not stop iio-sensor-proxy"):
            with self.subTest(message):
                self.assertIn(f"note {message}", result.stdout)
                self.assertNotIn(f"FAIL {message}", result.stdout)

    def test_verify_checks_that_the_kernel_s_l15b_step_held_it_in_hpm(self):
        """A kernel with the L15B step: the step's line, and the running DT
        and the regulator as the step leaves them."""
        self.boot_kit()
        self.l15b()
        self.journal_add(f"blastoise kernel: {L15B_STEP}\n")
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok   DT: L15B (vreg_l15b_1p8) held in HPM: no regulator-allow-set-load or "
                      f"regulator-allowed-modes, and it reads normal ({L15B_STEP})\n", result.stdout)
        self.assertIn("all checks passed", result.stdout)
        for name, kwargs, line, said in (
                ("the properties still in the node", {"set_load": True, "mode": "idle"}, L15B_STEP,
                 "DT: L15B (vreg_l15b_1p8) may drop to LPM: its node still has regulator-allow-set-load "
                 f"or regulator-allowed-modes, though the kernel logged \"{L15B_STEP}\" (it reads idle now)"),
                ("the node as the step leaves it, the regulator in LPM", {"mode": "idle"}, L15B_STEP,
                 "DT: L15B's node has neither property, but the regulator reads idle (its opmode), "
                 "not normal (HPM)"),
                ("the step refused the node", {"set_load": True, "mode": "idle"}, L15B_REFUSED,
                 f"DT: the kernel's L15B step failed: {L15B_REFUSED}")):
            with self.subTest(name):
                self.boot_kit()
                self.l15b(**kwargs)
                self.journal_add(f"blastoise kernel: {line}\n")
                result = self.run_script("verify.sh")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(f"FAIL {said}\n", result.stdout)
                self.assertEqual(result.stdout.count("FAIL "), 1, result.stdout)
                self.assertNotIn("ok   DT: L15B", result.stdout)
                # On another kernel it is a note.
                self.running(NEWER_BUILD_ID)
                result = self.run_script("verify.sh")
                self.assertIn(f"note {said}\n", result.stdout)
        self.boot_kit()
        self.journal_add(f"blastoise kernel: {L15B_STEP}\n")
        shutil.rmtree(self.root / "proc/device-tree" / L15B_NODE.lstrip("/"))
        result = self.run_script("verify.sh")
        self.assertIn(f"FAIL DT: no vreg_l15b_1p8 node at {self.root}/proc/device-tree{L15B_NODE} "
                      f"({L15B_STEP})\n", result.stdout)

    def test_a_kernel_without_the_l15b_step_gets_a_note(self):
        """Revisions 1 to 7 and the release have no L15B step, and their node
        is the pinned DTB's, L15B in LPM with the USB PHY down.  Without the
        step's line the block judges nothing, on the kit's kernel too: a
        note, never "held"."""
        for name, kwargs in (("the pinned DTB's node", {"set_load": True, "mode": "idle"}),
                             ("a node without the two properties", {})):
            with self.subTest(name):
                self.boot_kit()
                self.l15b(**kwargs)
                result = self.run_script("verify.sh")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                mode = kwargs.get("mode", "normal")
                self.assertIn("note DT: L15B (vreg_l15b_1p8) not checked: this kernel has no L15B step "
                              "(no \"gts9u-performance: L15B\" line this boot), and with the pinned DTB "
                              f"L15B may drop to LPM in a sleep (it reads {mode} now)\n", result.stdout)
                self.assertNotIn("ok   DT: L15B", result.stdout)
                self.assertIn("all checks passed", result.stdout)

    def test_the_l15b_lines_are_the_helper_s(self):
        """verify.sh knows the L15B step by the prefix of every line that
        gts9_hold_l15b_hpm() logs, and its success by the prefix of the two
        it logs with pr_info(); the lines these tests feed it are those
        format strings filled in."""
        source = (REPO / "kernel/drivers/gts9u-performance.c").read_text()
        body = re.search(r"^static int __init gts9_hold_l15b_hpm\(void\)\n\{\n.*?^\}\n", source,
                         re.M | re.S).group(0)
        lines = re.findall(r'\bpr_(err|info)\("([^"]*)\\n"', body)
        self.assertEqual([level for level, _ in lines], ["err", "err", "err", "info", "info"])
        for level, fmt in lines:
            with self.subTest(fmt):
                self.assertTrue(fmt.startswith("gts9u-performance: L15B"), fmt)
                self.assertEqual(fmt.startswith("gts9u-performance: L15B held in HPM"), level == "info")
        # The rest of the file logs nothing with that prefix.
        rest = source.replace(body, "")
        self.assertNotIn("gts9u-performance: L15B", rest)
        verify = (KIT_SRC / "verify.sh").read_text()
        self.assertIn("sed -n 's/^.*\\(gts9u-performance: L15B.*\\)$/\\1/p'", verify)
        self.assertIn("grep -q '^gts9u-performance: L15B held in HPM'", verify)

        def pattern(fmt):
            spec = {"%s": r"\S+", "%pOF": r"/\S+", "%d": r"-?\d+", "%u": r"\d+"}
            return "".join(spec.get(piece, re.escape(piece))
                           for piece in re.split(r"(%pOF|%[sdu])", fmt))
        self.assertTrue(re.fullmatch(pattern(lines[3][1]), L15B_STEP))
        self.assertTrue(re.fullmatch(pattern(lines[1][1]), L15B_REFUSED))
        self.assertIn(f'#define GTS9_L15B_PATH\t"{L15B_NODE}"\n', source)

    # Revision 9's kernel as blastoise is to boot it: revision 8's, the kit's
    # REVISION 9.
    def boot_kit_r9(self):
        self.boot_kit_r8()
        (self.kit / "REVISION").write_text("9\n")

    GMU_COUNTS = ('note GPU, this boot\'s kernel log (up 2 h 21 min): {} "timed out waiting for response", '
                  '{} "Timeout waiting for GMU OOB", {} "Unexpected message id" (with the fix all three stay at 0); '
                  '{} "GMU interrupt mask" (the fix\'s note that it found the mask changed, at most once a boot)\n')

    def test_revision_9_s_checks_pass_on_its_kernel(self):
        self.boot_kit_r9()
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("FAIL", result.stdout)
        for name, value in SWITCHES.items():
            with self.subTest(name):
                self.assertIn(f"ok   sm5714_usbpd.{name} = {value}\n", result.stdout)
        self.assertIn("ok   GPU: the GMU HFI fixes are in this kernel (msm is built in, and the build ID is the "
                      "kit's)\n", result.stdout)
        self.assertIn(self.GMU_COUNTS.format(0, 0, 0, 0), result.stdout)
        self.assertNotIn("GMU interrupt mask 0x", result.stdout)
        # Revision 8's checks are still there.
        self.assertIn("ok   sync_state: rpmhpd, gcc and gpu_cc synced at boot", result.stdout)
        self.assertIn("all checks passed", result.stdout)

    def test_a_missing_switch_fails_and_one_off_its_default_is_a_note(self):
        """A switch that is not there means that the running driver is not
        the kit's (since revision 10 the kit's has twelve): a FAIL, each one.
        One that is not at its default was changed since the boot: a note,
        and the kit's checks still pass."""
        params = lambda: self.root / "sys/module/sm5714_usbpd/parameters"
        for name, default in SWITCHES.items():
            with self.subTest(missing=name):
                FakeTablet.setUp(self)
                VerifyTest.setUp(self)
                self.boot_kit_r9()
                (params() / name).unlink()
                result = self.run_script("verify.sh")
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(f"FAIL sm5714_usbpd.{name} is missing, expected {default}: the running kernel's "
                              "driver is not this kit's\n", result.stdout)
                self.assertEqual(result.stdout.count("FAIL "), 1, result.stdout)
        changed = {"otg_rp": "2", "connection_wakeup": "Y", "sink_host_swap": "N", "try_snk_extra_wait_ms": "1500",
                   "swap_back_to_sink": "N", "own_pr_swap_in_place": "N", "swap_protocol_reset": "N",
                   "pr_swap_rd_delay_ms": "310", "sink_tx_ok_rp": "N", "recovery_detach": "N",
                   "cc_reopen_via_snk": "N", "cc_self_heal": "N"}
        self.assertEqual(set(changed), set(SWITCHES))
        for name, value in changed.items():
            with self.subTest(changed=name):
                FakeTablet.setUp(self)
                VerifyTest.setUp(self)
                self.boot_kit_r9()
                (params() / name).write_text(value + "\n")
                result = self.run_script("verify.sh")
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertIn(f"note sm5714_usbpd.{name} = {value}, changed from its default {SWITCHES[name]} "
                              "since the boot\n", result.stdout)
                self.assertNotIn(f"sm5714_usbpd.{name} = {SWITCHES[name]}\n", result.stdout)
                self.assertNotIn("FAIL", result.stdout)
        # Revision 8's kernel with this kit (before the reboot, or after a
        # way back from TWRP): its driver has the first four alone.
        FakeTablet.setUp(self)
        VerifyTest.setUp(self)
        self.boot_kit_r9()
        later = tuple(SWITCHES)[4:]
        for name in later:
            (params() / name).unlink()
        self.running(NEWER_BUILD_ID)
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(f"FAIL build ID {NEWER_BUILD_ID}, the kit's is {NEW_BUILD_ID}\n", result.stdout)
        for name in later:
            self.assertIn(f"FAIL sm5714_usbpd.{name} is missing, expected {SWITCHES[name]}", result.stdout)
        for name in tuple(SWITCHES)[:4]:
            self.assertIn(f"ok   sm5714_usbpd.{name} = {SWITCHES[name]}\n", result.stdout)
        self.assertEqual(result.stdout.count("FAIL "), 1 + len(later), result.stdout)

    def test_the_gpu_fixes_are_known_by_the_kit_s_build_id_with_msm_built_in(self):
        """msm is built in, so the build ID is all that tells that the
        fixes run: ok on revision 9's kernel; a note on the kernel of a kit
        before revision 9 and on any other kernel; and a modules.builtin
        without msm is a FAIL on revision 9's kernel, a note elsewhere."""
        fixes_in = "ok   GPU: the GMU HFI fixes are in this kernel"
        not_known = ("note GPU: the GMU HFI fixes are not known to be in this kernel: msm is built in, and this "
                     "is not revision 9's build\n")
        not_built_in = (f"GPU: modules.builtin of {RELEASE} does not list msm as built in, so the build ID does "
                        "not tell whether the GMU HFI fixes run\n")
        for name, boot, kernel, said in (
                ("revision 9's kernel", self.boot_kit_r9, NEW_BUILD_ID, fixes_in),
                ("revision 8's kit on its kernel", self.boot_kit_r8, NEW_BUILD_ID, not_known),
                ("a kit without REVISION", self.boot_kit, NEW_BUILD_ID, not_known),
                ("revision 9's kit on another kernel", self.boot_kit_r9, NEWER_BUILD_ID, not_known)):
            with self.subTest(name):
                FakeTablet.setUp(self)
                VerifyTest.setUp(self)
                boot()
                self.running(kernel)
                result = self.run_script("verify.sh")
                self.assertIn(said, result.stdout)
                self.assertEqual(result.stdout.count("GPU: the GMU HFI fixes"), 1, result.stdout)
        for kernel, word in ((NEW_BUILD_ID, "FAIL"), (NEWER_BUILD_ID, "note")):
            with self.subTest(msm="a module", kernel=kernel):
                FakeTablet.setUp(self)
                VerifyTest.setUp(self)
                self.boot_kit_r9()
                (self.modules / "modules.builtin").write_text("kernel/drivers/usb/typec/tcpm/tcpm.ko\n")
                self.running(kernel)
                result = self.run_script("verify.sh")
                self.assertIn(f"{word} {not_built_in}", result.stdout)
                self.assertNotIn(fixes_in, result.stdout)

    def test_this_boot_s_gmu_lines_are_counted_as_information(self):
        """The vote timeouts, the OOB timeouts, the late answers and the
        fix's note, from this boot's kernel log as blastoise writes them:
        counted, never a verdict; the note is shown, with its values."""
        self.boot_kit_r9()
        self.journal_add("".join(f"{line}\n" for line in (
            GMU_TIMEOUT, GMU_UNEXPECTED, GMU_TIMEOUT.replace("id 3921", "id 1032"),
            GMU_UNEXPECTED.replace("id 3921", "id 1032"), GMU_OOB, GMU_MASK)))
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("FAIL", result.stdout)
        self.assertIn(self.GMU_COUNTS.format(2, 1, 2, 1) + f"  {GMU_MASK}\n", result.stdout)
        self.assertEqual(result.stdout.count("GMU interrupt mask 0x"), 1, result.stdout)
        # On a kernel without the fix the same lines are counted the same way.
        self.running(NEWER_BUILD_ID)
        result = self.run_script("verify.sh")
        self.assertIn(self.GMU_COUNTS.format(2, 1, 2, 1), result.stdout)
        # Without the boot time the counts still come.
        (self.root / "proc/uptime").unlink()
        result = self.run_script("verify.sh")
        self.assertIn(self.GMU_COUNTS.format(2, 1, 2, 1).replace("up 2 h 21 min", "uptime unknown"),
                      result.stdout)
        # Without the kernel log there is nothing to count: its own FAIL.
        self.running(NEW_BUILD_ID)
        (self.root / "journal").write_text("")
        result = self.run_script("verify.sh")
        self.assertIn("FAIL cannot read this boot's kernel log", result.stdout)
        self.assertNotIn("GPU, this boot's kernel log", result.stdout)

    def test_the_gmu_lines_are_the_drivers_own(self):
        """The four texts verify.sh counts are the msm driver's: the vote
        timeout and the late answer in a6xx_hfi.c, the OOB timeout in
        a6xx_gmu.c (the pinned kernel's), the note in the hfi interrupt
        fix's added lines; and the fake lines above are those formats
        filled in."""
        verify = (KIT_SRC / "verify.sh").read_text()
        for text in ("timed out waiting for response", "Timeout waiting for GMU OOB", "Unexpected message id",
                     "GMU interrupt mask"):
            self.assertEqual(verify.count(f"gmu_count '{text}'"), 1, text)
        note = [line for line in added_lines("msm-adreno-a6xx-hfi-irq-leave-polled-bits.patch")[
            "drivers/gpu/drm/msm/adreno/a6xx_gmu.c"] if "GMU interrupt mask" in line]
        self.assertEqual(len(note), 1, note)
        fmt = re.search(r'"(GMU interrupt mask [^"]*)\\n"', note[0]).group(1)
        self.assertEqual(fmt.count("0x%x"), 4, fmt)
        self.assertTrue(re.fullmatch(re.escape(fmt).replace("0x%x", "0x[0-9a-f]+"),
                                     GMU_MASK[GMU_MASK.index("GMU interrupt mask"):]), (fmt, GMU_MASK))
        kernel = kernel_source()
        if kernel is None:
            return
        hfi = subprocess.run(["git", "-C", str(kernel), "show", "HEAD:drivers/gpu/drm/msm/adreno/a6xx_hfi.c"],
                             check=True, capture_output=True, text=True).stdout
        gmu = subprocess.run(["git", "-C", str(kernel), "show", "HEAD:drivers/gpu/drm/msm/adreno/a6xx_gmu.c"],
                             check=True, capture_output=True, text=True).stdout
        self.assertIn('"Message %s id %d timed out waiting for response\\n"', hfi)
        self.assertIn('"Unexpected message id %d on the response queue\\n"', hfi)
        self.assertIn('"Timeout waiting for GMU OOB set %s: 0x%x\\n"', gmu)

    def test_a_plug_in_run_records_revision_9_s_switches(self):
        """The trial's kernel file has the switches that change what a
        dock's power role swap does, as they were for the run; absent on a
        kernel that has none."""
        self.boot_kit_r9()
        (self.root / "sys/module/sm5714_usbpd/parameters/pr_swap_rd_delay_ms").write_text("310\n")
        self.plug([(PASS_LOG, "host", "sink")])
        result = self.run_script("verify.sh", "record")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (kernel,) = (self.kit / "logs").glob("*/kernel")
        text = kernel.read_text()
        for name in (*R9_SWITCHES, "otg_rp"):
            with self.subTest(name):
                value = "310" if name == "pr_swap_rd_delay_ms" else SWITCHES[name]
                self.assertIn(f"\n{name}: {value}\n", text)
        self.assertLess(text.index("try_snk_extra_wait_ms: 600\n"), text.index("swap_back_to_sink: Y\n"))
        self.assertLess(text.index("otg_rp: 0\n"), text.index("TCPM log: "))

    # Revision 10's kernel as blastoise is to boot it: revision 9's, the
    # kit's REVISION 10.
    def boot_kit_r10(self):
        self.boot_kit_r9()
        (self.kit / "REVISION").write_text("10\n")

    CC_COUNTS = ('note CC, this boot\'s kernel log (up 2 h 21 min): {} "CC opened for TCPM\'s error recovery", '
                 '{} "CC on again through Unattached.SNK" (the reopen after one), {} "CC finds no partner" '
                 '(the self-heal: no partner within 5 s of a reopen, or none with VBUS), {} "shows a partner '
                 'TCPM was not told of" (the attach watchdog\'s resync, 0 on a healthy port)\n')
    TCPM_FIX_IN = ("ok   TCPM: a sink is UFP after every Hard Reset in this kernel (TCPM is built in, and the build "
                   "ID is the kit's)\n")

    def test_revision_10_s_checks_pass_on_its_kernel(self):
        """The twelve switches at their defaults, the Hard Reset fix known
        by the build ID, the CC lines counted (none on a plain boot, the
        probe's resync among them), revision 9's and 8's checks still
        there."""
        self.boot_kit_r10()
        self.journal_add(CC_PROBE_RESYNC + "\n")
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("FAIL", result.stdout)
        for name, value in SWITCHES.items():
            with self.subTest(name):
                self.assertIn(f"ok   sm5714_usbpd.{name} = {value}\n", result.stdout)
        self.assertNotIn("the running driver has", result.stdout)
        self.assertIn(self.TCPM_FIX_IN, result.stdout)
        self.assertIn(self.CC_COUNTS.format(0, 0, 0, 0), result.stdout)
        # The counts' note is the last of them: no line shown under it.
        after = result.stdout.split(self.CC_COUNTS.format(0, 0, 0, 0), 1)[1]
        self.assertFalse(after.startswith("  "), after)
        self.assertNotIn("resynchronizing", result.stdout)
        self.assertIn("ok   GPU: the GMU HFI fixes are in this kernel", result.stdout)
        self.assertIn("ok   sync_state: rpmhpd, gcc and gpu_cc synced at boot", result.stdout)
        self.assertIn("all checks passed", result.stdout)

    def test_verify_names_revision_9_s_and_earlier_drivers_by_their_switches(self):
        """Revision 10's kit on another kernel: on revision 9's, its two
        switches missing (a FAIL each, and the build ID's), and a note that
        says whose driver runs; on revisions 1 to 8's and on the release's
        the same with their four and two."""
        params = lambda: self.root / "sys/module/sm5714_usbpd/parameters"
        for name, have, said in (
                ("revision 9's kernel", 10, "note sm5714_usbpd: the running driver has revision 9's ten switches "
                                            "and not revision 10's two: this is revision 9's kernel (or another "
                                            "without revision 10's driver)\n"),
                ("revision 8's kernel", 4, "note sm5714_usbpd: the running driver has the four switches of "
                                           "revisions 1 to 8: this is one of their kernels\n"),
                ("the release kernel", 2, "note sm5714_usbpd: the running driver has the release's two switches: "
                                          "this is the release kernel (or another without the kit's driver)\n")):
            with self.subTest(name):
                FakeTablet.setUp(self)
                VerifyTest.setUp(self)
                self.boot_kit_r10()
                for switch in tuple(SWITCHES)[have:]:
                    (params() / switch).unlink()
                self.running(NEWER_BUILD_ID)
                result = self.run_script("verify.sh")
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(f"FAIL build ID {NEWER_BUILD_ID}, the kit's is {NEW_BUILD_ID}\n", result.stdout)
                for switch in tuple(SWITCHES)[have:]:
                    self.assertIn(f"FAIL sm5714_usbpd.{switch} is missing, expected {SWITCHES[switch]}",
                                  result.stdout)
                self.assertEqual(result.stdout.count("FAIL "), 1 + len(SWITCHES) - have, result.stdout)
                self.assertIn(said, result.stdout)
                self.assertEqual(result.stdout.count("the running driver has"), 1, result.stdout)
                self.assertIn("note TCPM: the Hard Reset fix is not known to be in this kernel", result.stdout)
        # A set of switches that is no revision's: the FAILs alone.
        FakeTablet.setUp(self)
        VerifyTest.setUp(self)
        self.boot_kit_r10()
        (params() / "swap_back_to_sink").unlink()
        result = self.run_script("verify.sh")
        self.assertEqual(result.stdout.count("FAIL "), 1, result.stdout)
        self.assertNotIn("the running driver has", result.stdout)

    def test_the_hard_reset_fix_is_known_by_the_kit_s_build_id_with_tcpm_built_in(self):
        """TCPM is built in and the fix logs nothing, so the build ID is all
        that tells that it runs: ok on revision 10's kernel; a note on the
        kernel of a kit before revision 10 and on any other kernel; and a
        modules.builtin without tcpm is a FAIL on revision 10's kernel, a
        note elsewhere."""
        not_known = ("note TCPM: the Hard Reset fix is not known to be in this kernel: TCPM is built in, and this "
                     "is not revision 10's build\n")
        not_built_in = (f"TCPM: modules.builtin of {RELEASE} does not list tcpm as built in, so the build ID does "
                        "not tell whether the Hard Reset fix runs\n")
        for name, boot, kernel, said in (
                ("revision 10's kernel", self.boot_kit_r10, NEW_BUILD_ID, self.TCPM_FIX_IN),
                ("revision 9's kit on its kernel", self.boot_kit_r9, NEW_BUILD_ID, not_known),
                ("a kit without REVISION", self.boot_kit, NEW_BUILD_ID, not_known),
                ("revision 10's kit on another kernel", self.boot_kit_r10, NEWER_BUILD_ID, not_known)):
            with self.subTest(name):
                FakeTablet.setUp(self)
                VerifyTest.setUp(self)
                boot()
                self.running(kernel)
                result = self.run_script("verify.sh")
                self.assertIn(said, result.stdout)
                self.assertEqual(result.stdout.count("TCPM: "), 1, result.stdout)
        for kernel, word in ((NEW_BUILD_ID, "FAIL"), (NEWER_BUILD_ID, "note")):
            with self.subTest(tcpm="a module", kernel=kernel):
                FakeTablet.setUp(self)
                VerifyTest.setUp(self)
                self.boot_kit_r10()
                (self.modules / "modules.builtin").write_text("kernel/drivers/gpu/drm/msm/msm.ko\n")
                self.running(kernel)
                result = self.run_script("verify.sh")
                self.assertIn(f"{word} {not_built_in}", result.stdout)
                self.assertNotIn(self.TCPM_FIX_IN, result.stdout)
                if word == "FAIL":
                    self.assertEqual(result.stdout.count("FAIL "), 1, result.stdout)

    def test_this_boot_s_cc_lines_are_counted_as_information(self):
        """The recovery's CC opens, the reopens, the self-heal's warnings
        and the watchdog's resyncs, from this boot's kernel log as blastoise
        writes it: counted, never a verdict, the self-heal's and the
        watchdog's lines shown with their values; the probe's resync of
        every boot is none of them."""
        self.boot_kit_r10()
        self.journal_add("".join(f"[  {n}.000001] {line}\n" for n, line in enumerate((
            CC_PROBE_RESYNC, CC_OPENED, CC_REOPENED, CC_HEALED, CC_OPENED, CC_REOPENED, CC_HEALED_VBUS,
            CC_RESYNC), start=10)))
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("FAIL", result.stdout)
        self.assertIn(self.CC_COUNTS.format(2, 2, 2, 1) + f"  [  13.000001] {CC_HEALED}\n"
                      f"  [  16.000001] {CC_HEALED_VBUS}\n  [  17.000001] {CC_RESYNC}\n", result.stdout)
        self.assertNotIn("resynchronizing", result.stdout)
        self.assertIn("all checks passed", result.stdout)
        # At most six lines are shown.
        self.journal_add("".join(f"{CC_HEALED}\n" for _ in range(8)))
        result = self.run_script("verify.sh")
        self.assertIn(self.CC_COUNTS.format(2, 2, 10, 1), result.stdout)
        shown = result.stdout.split(self.CC_COUNTS.format(2, 2, 10, 1), 1)[1].splitlines()
        self.assertEqual([line.startswith("  ") for line in shown[:7]], [True] * 6 + [False], shown)
        # On another kernel the same lines are counted the same way.
        self.running(NEWER_BUILD_ID)
        result = self.run_script("verify.sh")
        self.assertIn(self.CC_COUNTS.format(2, 2, 10, 1), result.stdout)
        # Without the kernel log there is nothing to count: its own FAIL.
        self.running(NEW_BUILD_ID)
        (self.root / "journal").write_text("")
        result = self.run_script("verify.sh")
        self.assertIn("FAIL cannot read this boot's kernel log", result.stdout)
        self.assertNotIn("CC, this boot's kernel log", result.stdout)

    def test_the_cc_lines_are_the_drivers_own(self):
        """The four texts verify.sh counts are revision 10's driver's (the
        recovery's CC open is revision 9's), each in one format string; the
        probe's resync is another; and the fake lines above are those
        formats filled in."""
        verify = (KIT_SRC / "verify.sh").read_text()
        driver = (REPO / "kernel/drivers/sm5714_usbpd.c").read_text()
        # Every string literal of the driver that ends in a newline.
        formats = re.findall(r'"((?:[^"\\\n]|\\.)*)\\n"', driver)
        counted = {"CC opened for TCPM's error recovery": CC_OPENED,
                   "CC on again through Unattached.SNK": CC_REOPENED,
                   "CC finds no partner": CC_HEALED, "shows a partner TCPM was not told of": CC_RESYNC}
        for text, line in counted.items():
            with self.subTest(text):
                self.assertEqual(verify.count(f"cc_count '{text}'") + verify.count(f'cc_count "{text}"'), 1, text)
                (fmt,) = [f for f in formats if text in f]
                pattern = re.escape(fmt).replace("0x%02x", "0x[0-9a-f]{2}").replace("%s", ".+").replace(
                    "%u", "[0-9]+").replace("%zu", "[0-9]+")
                self.assertTrue(re.fullmatch(pattern, line.split("8-0033: ", 1)[1]), (fmt, line))
        (fmt,) = [f for f in formats if "CC finds no partner" in f]
        pattern = re.escape(fmt).replace("0x%02x", "0x[0-9a-f]{2}").replace("%s", ".+").replace(
            "%u", "[0-9]+").replace("%zu", "[0-9]+")
        self.assertTrue(re.fullmatch(pattern, CC_HEALED_VBUS.split("8-0033: ", 1)[1]))
        for why in ("since the driver opened it", "though VBUS comes from one"):
            self.assertIn(f'why = "{why}";', driver)
            self.assertIn(why, CC_HEALED + CC_HEALED_VBUS)
        (probe,) = [f for f in formats if "resynchronizing TCPM" in f]
        self.assertFalse([text for text in counted if text in probe])
        self.assertEqual(CC_PROBE_RESYNC.split("8-0033: ", 1)[1],
                         probe.replace("0x%02x", "0x00"))
        # The probe's resync is the work queued at every probe.
        self.assertIn("mod_delayed_work(system_dfl_wq, &sm->cc_resync_work,", driver)

    def test_a_plug_in_run_records_revision_10_s_switches(self):
        """The trial's kernel file has revision 10's two switches too, as
        they were for the run, after revision 9's and before otg_rp; absent
        on a kernel that has neither."""
        self.boot_kit_r10()
        (self.root / "sys/module/sm5714_usbpd/parameters/cc_self_heal").write_text("N\n")
        self.plug([(PASS_LOG, "host", "sink")])
        result = self.run_script("verify.sh", "record")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (kernel,) = (self.kit / "logs").glob("*/kernel")
        text = kernel.read_text()
        self.assertIn("\ncc_reopen_via_snk: Y\ncc_self_heal: N\notg_rp: 0\n", text)
        self.assertLess(text.index("recovery_detach: Y\n"), text.index("cc_reopen_via_snk: Y\n"))
        # Revision 9's kernel: the two are absent.
        FakeTablet.setUp(self)
        VerifyTest.setUp(self)
        self.boot_kit_r10()
        for name in R10_SWITCHES:
            (self.root / "sys/module/sm5714_usbpd/parameters" / name).unlink()
        self.plug([(PASS_LOG, "host", "sink")])
        self.assertEqual(self.run_script("verify.sh", "record").returncode, 0)
        (kernel,) = (self.kit / "logs").glob("*/kernel")
        self.assertIn("\ncc_reopen_via_snk: absent\ncc_self_heal: absent\n", kernel.read_text())


# llvm-objdump --no-show-raw-insn of qmp_ufs_phy_calibrate in a vmlinux, as
# the release's shows it (abridged): the board gate is the call of
# of_machine_compatible_match.
CALIBRATE_RELEASE = """
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
# The new one, without the gate.
CALIBRATE_NEW = CALIBRATE_RELEASE.replace(
    "ffff80008096408c:      \tbl\t0xffff800081578e48 <of_machine_compatible_match>\n", "")


class UfsGateTest(unittest.TestCase):
    """make-kit.sh's checks of the UFS PHY driver, which the build changes
    since 2026-11-22: the PCS reset whole in the build tree's file, neither
    the release's SM-X910-only form nor the SM-X710 kit's diagnostic there,
    the tree comparison replaying the stage script on the release tree's
    file, and the disassembly of the calibration in vmlinux, which tells the
    new code (no board check) from the release's."""

    text = (KIT_SRC / "make-kit.sh").read_text()

    def gate(self, vmlinux, new_id, base=None):
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
                      f'build={tmp}/build tmp={tmp} new_id={new_id} base_build_id={BASE_BUILD_ID}\n')
            if base:
                (tmp / "base/linux-gts9uwifi").mkdir(parents=True)
                (tmp / "base/linux-gts9uwifi/vmlinux").write_text(f"id={base[0]}\n{base[1]}")
                prefix += f"BASE_SCRATCH={tmp}/base\n"
            result = subprocess.run(["bash", "-c", prefix + build_id + section], capture_output=True,
                                    text=True, env=env)
            return result.returncode, result.stdout + result.stderr

    def test_the_new_calibration_passes_and_the_release_s_does_not(self):
        new = "1" * 40
        self.assertEqual(self.gate((new, CALIBRATE_NEW), new),
                         (0, f"UFS PHY: qmp_ufs_phy_calibrate() in vmlinux (build ID {new[:12]}) calls no "
                             "of_machine_compatible_match()\n"))
        self.assertEqual(self.gate((new, CALIBRATE_NEW), new, base=(BASE_BUILD_ID, CALIBRATE_RELEASE)),
                         (0, f"UFS PHY: qmp_ufs_phy_calibrate() in vmlinux (build ID {new[:12]}) calls no "
                             "of_machine_compatible_match(); the release build's does\n"))
        for name, args, message in (
                ("the release's calibration", ((new, CALIBRATE_RELEASE), new),
                 "still asks of_machine_compatible_match(): the board gate"),
                ("no such function", ((new, "Disassembly of section .text:\n"), new),
                 "no qmp_ufs_phy_calibrate() calling reset_control_assert()"),
                ("another build's vmlinux", (("2" * 40, CALIBRATE_NEW), new),
                 "is not the ELF of the new image (another build ID)"),
                ("a release vmlinux without the gate", ((new, CALIBRATE_NEW), new, (BASE_BUILD_ID, CALIBRATE_NEW)),
                 "self-test failed: the release calibration does not show the board gate either"),
                ("another release vmlinux", ((new, CALIBRATE_NEW), new, ("3" * 40, CALIBRATE_RELEASE)),
                 "is not the release kernel's")):
            with self.subTest(name):
                code, out = self.gate(*args)
                self.assertEqual(code, 1, out)
                self.assertIn(message, out)

    def flows(self, tmp):
        """The UFS PHY driver as builds leave it: the release's (SERDES, then
        the SM-X910-only reset), a default build's (SERDES, the stage
        script), this kit's since revision 8 and the SM-X710 kit's (the new
        one and both diagnostics, APPLY_QMP_UFS_PCS_STATE_DIAGNOSTIC=1 with
        APPLY_SM8550_SLEEP_FIXES=1), the new one with the first diagnostic
        alone, and the kit's with the old form's marker left in a comment."""
        source = subprocess.run(["git", "-C", str(kernel_source()), "show",
                                 f"HEAD:{QMP}.c"], check=True, capture_output=True, text=True).stdout
        legacy = re.search(r"<<'LEGACY_PCS_RESET'\n(.*?\n)LEGACY_PCS_RESET\n",
                           (REPO / "scripts/stage-ufs-resume-experiment.sh").read_text(), re.S).group(1)
        trees = {}
        for name in ("release", "new", "diagnostic", "kit", "stale-marker"):
            root = tmp / name
            (root / QMP).parent.mkdir(parents=True)
            (root / f"{QMP}.c").write_text(source)
            subprocess.run(["patch", "-s", "-d", str(root), "-p1"],
                           input=(REPO / "kernel/patches/qmp-ufs-reset-serdes-before-power-down-gts9u.patch")
                           .read_text(), check=True, text=True)
            if name == "release":
                subprocess.run(["patch", "-s", "--fuzz=0", "-d", str(root), "-p1"], input=legacy,
                               check=True, text=True)
            else:
                subprocess.run(["bash", str(REPO / "scripts/stage-ufs-resume-experiment.sh"), str(root)],
                               check=True, capture_output=True)
            if name in ("diagnostic", "kit", "stale-marker"):
                subprocess.run(["patch", "-s", "--fuzz=0", "-d", str(root), "-p1"],
                               input=(REPO / "kernel/patches/qmp-ufs-pcs-state-diagnostic.patch").read_text(),
                               check=True, text=True)
            if name in ("kit", "stale-marker"):
                subprocess.run(["patch", "-s", "--fuzz=0", "-d", str(root), "-p1"],
                               input=(REPO / "kernel/patches/qmp-ufs-pwrdn-readback-diagnostic.patch").read_text(),
                               check=True, text=True)
            if name == "stale-marker":
                with (root / f"{QMP}.c").open("a") as source_file:
                    source_file.write("/* SM-X910 experiment: assert PCS reset before calibration tables. */\n")
            trees[name] = root
        return trees

    def defined(self):
        """make-kit.sh's own definitions of the names the checks use."""
        return "".join(re.search(rf"^{name}=.*\n", self.text, re.M).group(0)
                       for name in ("qmp", "pcs_patch", "pcs_legacy_marker", "qmp_diag_marker", "ufs_patches"))

    @unittest.skipUnless(kernel_source(), "no pinned kernel checkout")
    def test_the_build_tree_has_to_carry_the_pcs_reset_whole_and_nothing_else(self):
        start = self.text.index('mkdir -p "$tmp/ufs/${qmp%/*}"')
        end = self.text.index("or a diagnostic beside the two\"\n") + len("or a diagnostic beside the two\"\n")
        check = self.text[start:end]
        pwrdn = "does not carry qmp-ufs-pwrdn-readback-diagnostic.patch whole"
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            trees = self.flows(tmp)
            for name, message in (("kit", None), ("new", pwrdn), ("release", pwrdn), ("diagnostic", pwrdn),
                                  ("stale-marker", "carries the SM-X910-only PCS reset, or a diagnostic beside "
                                                   "the two")):
                with self.subTest(name):
                    work = tmp / f"work-{name}"
                    work.mkdir()
                    result = subprocess.run(
                        ["bash", "-c", 'set -euo pipefail\ndie() { echo "$*" >&2; exit 1; }\n'
                         f'tree={trees[name]} tmp={work} repo={REPO}\n' + self.defined() + check],
                        capture_output=True, text=True)
                    if message is None:
                        self.assertEqual((result.returncode, result.stderr), (0, ""))
                    else:
                        self.assertEqual(result.returncode, 1, result.stderr)
                        self.assertIn(message, result.stderr)

    @unittest.skipUnless(kernel_source(), "no pinned kernel checkout")
    def test_the_tree_comparison_replays_the_stage_script_on_the_release_tree_s_file(self):
        start = self.text.index("\t# The UFS PHY driver as the build changes it: the release build gave the")
        end = self.text.index("with this checkout's stage script and diagnostics on it\"\n", start) + len(
            "with this checkout's stage script and diagnostics on it\"\n")
        replay = self.text[start:end]
        other = "is not the release tree's with this checkout's stage script and diagnostics on it"
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            trees = self.flows(tmp)
            for name, base, tree, message in (
                    ("the release tree and this kit's build", "release", "kit", None),
                    ("a base tree with the new reset", "new", "kit",
                     "is not the one with the SM-X910-only PCS reset"),
                    ("a base tree with the diagnostic", "diagnostic", "kit",
                     "is not the one with the SM-X910-only PCS reset"),
                    ("a default build's tree", "release", "new", other),
                    ("a build tree with one diagnostic", "release", "diagnostic", other)):
                with self.subTest(name):
                    work = tmp / f"replay-{base}-{tree}"
                    (work / "src").mkdir(parents=True)
                    result = subprocess.run(
                        ["bash", "-c", 'set -euo pipefail\ndie() { echo "$*" >&2; exit 1; }\n'
                         f'base_tree={trees[base]} tree={trees[tree]} tmp={work} repo={REPO}\n'
                         + self.defined() + replay],
                        capture_output=True, text=True)
                    if message is None:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual((work / "src" / f"{QMP}.c").read_text(),
                                         (trees["kit"] / f"{QMP}.c").read_text())
                    else:
                        self.assertEqual(result.returncode, 1, result.stderr)
                        self.assertIn(message, result.stderr)


class ScriptsTest(unittest.TestCase):
    def test_scripts_parse(self):
        for name, shell in (("install.sh", "sh"), ("rollback.sh", "sh"), ("verify.sh", "sh"),
                            ("make-kit.sh", "bash")):
            with self.subTest(name):
                subprocess.run([shell, "-n", str(KIT_SRC / name)], check=True)

    def test_scripts_pin_the_same_release(self):
        self.assertEqual(pinned("install.sh", "base_boot"), pinned("make-kit.sh", "base_boot"))
        self.assertEqual(pinned("install.sh", "base_boot"), pinned("rollback.sh", "base_boot"))
        self.assertEqual(pinned("install.sh", "base_vendor_boot"),
                         pinned("make-kit.sh", "base_vendor_boot"))
        self.assertEqual(pinned("install.sh", "base_build_id"), BASE_BUILD_ID)
        self.assertEqual(pinned("make-kit.sh", "base_build_id"), BASE_BUILD_ID)
        for name in ("base_boot", "base_vendor_boot"):
            self.assertEqual(len(pinned("install.sh", name)), 64)

    def test_install_and_rollback_agree_on_where_things_are(self):
        for name in ("release", "backup", "log", "size", "saved_set", "stopgap"):
            with self.subTest(name):
                self.assertEqual(constant("install.sh", name), constant("rollback.sh", name))
        self.assertEqual(constant("install.sh", "size"), constant("make-kit.sh", "size"))
        # The image install.sh saves is the one rollback.sh writes back, and
        # the marker it leaves is the one rollback.sh looks for.
        install = (KIT_SRC / "install.sh").read_text()
        rollback = (KIT_SRC / "rollback.sh").read_text()
        for name in ('"$backup/boot-release.img"', '"$backup/saved-set-boot.img"',
                     '"$backup/modules-$release"', '"$backup/stopgap-was-enabled"',
                     "/run/gts9u-typec-roles.lock",
                     # Where a module swap that stopped leaves trees behind.
                     "/usr/lib/modules/.typec-roles-$release",
                     "/usr/lib/modules/.typec-roles-old-$release"):
            with self.subTest(name):
                self.assertIn(name, install)
                self.assertIn(name, rollback)

    def test_revision_3_is_pinned_apart_from_the_release(self):
        r3 = pinned("install.sh", "kit_r3_boot")
        self.assertEqual(len(r3), 64)
        self.assertNotEqual(r3, pinned("install.sh", "base_boot"))
        self.assertNotEqual(r3, pinned("install.sh", "base_vendor_boot"))

    def test_the_kit_s_patches_are_patches_the_build_applies(self):
        text = (KIT_SRC / "make-kit.sh").read_text()
        names = re.search(r"^patches=\((.*?)\)$", text, re.M | re.S).group(1).split()
        self.assertEqual(names, ["tcpm-request-dfp-as-sink-before-discovery.patch",
                                 "tcpm-try-snk-extra-wait.patch",
                                 "tcpm-no-dr-swap-during-vdm.patch",
                                 "tcpm-dr-set-checks-data-role.patch",
                                 "tcpm-keep-queued-swap-pending.patch",
                                 "gcc-sm8550-usb-gdscs-retention.patch",
                                 "dwc3-qcom-check-hcd-before-reading-port-speed.patch"])
        build = (REPO / "scripts/build-mainline-kernel.sh").read_text().replace("\\\n", " ")
        for name in names:
            with self.subTest(name):
                self.assertTrue((REPO / "kernel/patches" / name).is_file())
                self.assertEqual(len(re.findall(
                    rf"^apply_unless '[^']+'\s+\S+\s+{re.escape(name)}\s*$", build, re.M)), 1)
        # Each file a patch changes is one the kit compares with the release's.
        paths = kit_paths(text)
        patched = {kit_expand(word, paths) for word in shlex.split(
            re.search(r"^patched=\((.*?)\)$", text, re.M | re.S).group(1))}
        self.assertIn("\ntcpm_dir=drivers/usb/typec/tcpm\n", text)
        self.assertIn("\ngcc=drivers/clk/qcom/gcc-sm8550\n", text)
        self.assertIn("\ndwc3=drivers/usb/dwc3/dwc3-qcom\n", text)
        # Revision 8: every patch the replay applies, in the build's order.
        order = [kit_expand(word, {}) if "$" not in word else word for word in shlex.split(
            re.search(r"^build_order=\((.*?)\)$", text, re.M | re.S).group(1))]
        arrays = {name: shlex.split(re.search(rf"^{name}=\((.*?)\)$", text, re.M | re.S).group(1))
                  for name in ("cx_patches", "ufs_qcom_patches", "gdsc_patches", "pcie_patches", "debug_patches",
                               "fix_patches", "ufs_patches", "xo_patches", "gpu_patches", "swap_back_patches")}
        expanded = []
        for word in order:
            whole = re.fullmatch(r'"\$\{(\w+)\[@\]\}"', f'"{word}"') or re.fullmatch(r"\$\{(\w+)\[@\]\}", word)
            one = re.fullmatch(r"\$\{(\w+)\[(\d)\]\}", word)
            if whole:
                expanded += arrays[whole.group(1)]
            elif one:
                expanded.append(arrays[one.group(1)][int(one.group(2))])
            else:
                expanded.append(word)
        self.assertEqual(sorted(expanded), sorted(set(expanded)))
        # The kit's own seven, every revision-8 patch but the UFS PHY's two
        # diagnostics (replayed with the stage script), and nothing else.
        self.assertEqual(set(expanded), set(names) | {n for name, ns in arrays.items() if name != "ufs_patches"
                                                     for n in ns})
        # The build's order: where it applies each patch, by the forms the
        # build script applies one in (apply_unless, a NAME_patch= of a
        # strict block, the debug switches' FILE:PATCH list, sleep_fix, or
        # a patch read from $pat), lines joined, comments left out.
        code = [line.strip() for line in (REPO / "scripts/build-mainline-kernel.sh").read_text()
                .replace("\\\n", " ").splitlines() if not line.lstrip().startswith("#")]
        at = {}
        for patch_name in expanded:
            form = re.compile(r"(^apply_unless .*\s|^\w+_patch=|^for \w+ in .*:|^sleep_fix .*\s|\$pat/)"
                              + re.escape(patch_name))
            hits = [(number, match.end()) for number, line in enumerate(code)
                    for match in [form.search(line)] if match]
            self.assertTrue(hits, patch_name)
            at[patch_name] = hits[0]
        self.assertEqual(sorted(expanded, key=at.__getitem__), expanded)
        # And the unpark patch, which the replay takes out of the release
        # tree's PCIe PHY driver.
        changed = set()
        for name in expanded + arrays["ufs_patches"] + [constant("make-kit.sh", "unpark")]:
            changed.update(re.findall(r"^\+\+\+ b/(\S+)", (REPO / "kernel/patches" / name).read_text(), re.M))
        self.assertEqual(changed, patched | {paths["qmp"] + ".c"})

    def test_every_changed_source_is_required_dated_and_compared(self):
        """The kit's changes to the kernel tree are the patched files, the
        driver and the UFS PHY driver, which the stage script changes.  Each
        source is required, its object is dated against it, and with
        BASE_SCRATCH each is what may differ from the release tree.  The
        dwc3-qcom guard adds no string to the image: the marker in the build
        tree's source and the date of its object are its proof."""
        text = (KIT_SRC / "make-kit.sh").read_text()
        names = kit_paths(text)

        def expand(word):
            return kit_expand(word, names)

        patched = [expand(word) for word in shlex.split(
            re.search(r"^patched=\((.*?)\)$", text, re.M | re.S).group(1))]
        driver = f"{TCPM_DIR}/sm5714_usbpd.c"
        staged = [f"{QMP}.c", f"{names['helper']}.c", f"{names['helper']}-overlay.h"]
        want = re.search(r'^\tsort > "\$tmp/trees\.want" <<EOF\n(.*?)^EOF$', text, re.M | re.S)
        self.assertEqual(sorted(expand(word) for word in want.group(1).split()),
                         sorted(patched + [driver] + staged))
        objects = [expand(pair) for pair in shlex.split(
            re.search(r"^objects=\((.*?)\)$", text, re.M | re.S).group(1))]
        required = text[text.index('for f in "$mkbootimg"'):]
        required = required[:required.index("; do\n")]
        self.assertIn('"${patched[@]/#/$tree/}"', required)
        self.assertIn('"$tree/$qmp.c"', required)
        self.assertIn('"$tree/$helper.c" "$tree/$helper-overlay.h"', required)
        for source in patched + [driver] + staged:
            with self.subTest(source):
                stem = source.rsplit(".", 1)[0]
                if source.endswith(".c"):
                    self.assertIn(f"{stem}.o:{source}", objects)
                    variable = max((name for name, value in names.items() if stem == value
                                    or stem.startswith(value + "/")), key=lambda name: len(names[name]))
                    obj = stem.replace(names[variable], f"${variable}", 1) + ".o"
                    self.assertIn(f'"$build/{obj}"', required)
                elif not source.endswith("Kconfig"):
                    # A header: dated against the object that includes it.
                    self.assertTrue([pair for pair in objects if pair.endswith(f":{source}")], source)
        self.assertIn("grep -q 'no hcd and no root hub' \"$tree/$dwc3.c\" ||\n"
                      "\tdie 'the build tree does not carry the dwc3-qcom guard'", text)
        # The disassemblies: the UFS PHY's calibration (UfsGateTest), the
        # clock controller's symbols (the power domain table), what the
        # PCIe PHY's power-on and the UART's sleep callbacks call, and since
        # revision 9 what the GMU's HFI wait loads (Revision9Test).
        self.assertEqual(re.findall(r"^.*llvm-objdump.*$", text, re.M), [
            "for t in unpack_bootimg python3 sha256sum cmp diff patch tar modinfo xxd openssl git "
            "llvm-objcopy llvm-objdump llvm-nm pahole dtc; do",
            '\tllvm-objdump -t "$2" > "$tmp/gcc.new.syms"',
            "\tllvm-objdump -d --no-show-raw-insn --disassemble-symbols=qmp_ufs_phy_calibrate \"$1\" |",
            '\tllvm-objdump -d --no-show-raw-insn --disassemble-symbols="$2" "$1" |',
            '\tllvm-objdump -d --no-show-raw-insn --disassemble-symbols="$2" "$1" |'])
        self.assertIn('branches_of() { # VMLINUX FUNCTION: what FUNCTION calls or branches to by name', text)
        self.assertIn("loads_of() { # VMLINUX FUNCTION: the offsets of FUNCTION's 64-bit loads", text)

    def config_gate(self, base, new):
        """Run make-kit.sh's configuration check on two stand-in configs."""
        text = (KIT_SRC / "make-kit.sh").read_text()
        block = re.search(r"^conf\(\) \{ # CONFIG.*?^\texit 1\n\}\n", text, re.M | re.S)
        self.assertIsNotNone(block)
        changes = re.search(r"^config_changes=\(.*?\)$", text, re.M | re.S).group(0)
        with tempfile.TemporaryDirectory() as tmp:
            for name, body in (("base/config", base), ("out/config", new)):
                pathlib.Path(tmp, name).parent.mkdir(parents=True, exist_ok=True)
                pathlib.Path(tmp, name).write_text(body)
            return subprocess.run(["bash", "-c", 'set -euo pipefail; export LC_ALL=C\n'
                                   'die() { echo "$*" >&2; exit 1; }\n'
                                   f'base={tmp}/base out={tmp}/out tmp={tmp}\n' + changes + "\n" + block.group(0)],
                                  capture_output=True, text=True)

    def test_the_configuration_differs_by_the_fragment_s_four_lines_alone(self):
        """Revision 8's kernel is the release's configuration with the four
        lines of config-gts9uwifi.fragment of 2026-11-25 (an olddefconfig of
        this checkout gives exactly these, nothing derived); any other change,
        or one of them missing, stops the kit.  Each change's old value is
        the base configuration's, its new one the fragment's."""
        base = ("CONFIG_A=y\nCONFIG_PSTORE_COMPRESS=y\n# CONFIG_PSTORE_FTRACE is not set\nCONFIG_QCOM_STATS=m\n"
                "CONFIG_SM_VIDEOCC_8550=m\nCONFIG_Z=m\n")
        new = ("CONFIG_A=y\n# CONFIG_PSTORE_COMPRESS is not set\nCONFIG_PSTORE_FTRACE=y\nCONFIG_QCOM_STATS=y\n"
               "CONFIG_SM_VIDEOCC_8550=y\nCONFIG_Z=m\n")
        self.assertEqual(self.config_gate(base, new).returncode, 0)
        for name, other in (("one more", new + "CONFIG_NEW=y\n"), ("one gone", new.replace("CONFIG_Z=m\n", "")),
                            ("one of the four missing", new.replace("CONFIG_QCOM_STATS=y", "CONFIG_QCOM_STATS=m")),
                            ("a derived change", new.replace("CONFIG_A=y", "CONFIG_A=m"))):
            with self.subTest(name):
                result = self.config_gate(base, other)
                self.assertEqual(result.returncode, 1)
                self.assertIn("does not differ from the release kernel's in config_changes alone", result.stderr)
        self.assertEqual(self.config_gate(base, base).returncode, 1)
        text = (KIT_SRC / "make-kit.sh").read_text()
        changes = [line.split("\t") for line in shlex.split(
            re.search(r"^config_changes=\((.*?)\)$", text, re.M | re.S).group(1))]
        self.assertEqual([c[0] for c in changes], sorted(c[0] for c in changes))
        fragment = (REPO / "kernel/config/config-gts9uwifi.fragment").read_text().splitlines()
        mainline = (REPO / "kernel/config/config-mainline.aarch64").read_text().splitlines()

        def value(lines, symbol):
            for line in lines:
                if line.startswith(symbol + "="):
                    return line.split("=", 1)[1]
                if line == f"# {symbol} is not set":
                    return "n"
            return "n"

        for symbol, old, new_value in changes:
            with self.subTest(symbol):
                self.assertEqual(value(mainline, symbol), old)
                self.assertEqual(value(fragment, symbol), new_value)

    def test_the_kit_ships_the_x710_kit_s_sleep_test_and_ufs_check(self):
        """One sleep test for both boards: the SM-X710 kit's file, packed
        as it is, with the UFS PHY check; and the kit says its revision."""
        text = (KIT_SRC / "make-kit.sh").read_text()
        shipped = re.search(r"^files=\((.*?)\)$", text, re.M | re.S).group(1).split()
        for name in ("sleep-test.sh", "ufs-phy-power-check.sh", "REVISION"):
            self.assertIn(name, shipped)
        self.assertIn('install -m 0644 "$repo/tools/inplace/typec-roles/sleep-test.sh" \\\n'
                      '\t"$repo/tools/inplace/typec-roles/ufs-phy-power-check.sh" "$kit/"\n', text)
        self.assertIn('printf \'%s\\n\' "$revision" > "$kit/REVISION"\n', text)
        self.assertEqual(constant("make-kit.sh", "revision"), "10")
        self.assertFalse((KIT_SRC / "sleep-test.sh").exists())

    def test_revision_8_s_strings_are_their_sources_own(self):
        """deep_strings: each is in this checkout's helper, a patch's added
        lines, or (the video clock controller's driver name) the pinned
        kernel; none is in the release's sources of the Type-C patches."""
        text = (KIT_SRC / "make-kit.sh").read_text()
        strings = shlex.split(re.search(r"^deep_strings=\((.*?)\)$", text, re.M | re.S).group(1))
        self.assertEqual(len(strings), len(set(strings)))
        helper = (REPO / "kernel/drivers/gts9u-performance.c").read_text()
        added = "\n".join(line for patch in (REPO / "kernel/patches").glob("*.patch")
                          for lines in added_lines(patch.name).values() for line in lines)
        kernel = kernel_source()
        for s in strings:
            with self.subTest(s):
                if s == "video_cc-sm8550":
                    if kernel is None:
                        continue
                    source = subprocess.run(["git", "-C", str(kernel), "show",
                                             "HEAD:drivers/clk/qcom/videocc-sm8550.c"],
                                            check=True, capture_output=True, text=True).stdout
                    self.assertIn(f'.name = "{s}"', source)
                elif s.startswith("gts9u-performance:"):
                    self.assertIn(s, helper)
                else:
                    plain = s.split(".", 1)[1] if re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", s) else s
                    self.assertIn(plain, added + helper)

    def test_the_stopgap_is_the_same_service_everywhere(self):
        self.assertEqual(constant("install.sh", "stopgap"), "typec-sink-host.service")
        verify = (KIT_SRC / "verify.sh").read_text()
        self.assertEqual(verify.count("systemctl is-active --quiet typec-sink-host"), 2)
        self.assertEqual(verify.count("systemctl is-enabled --quiet typec-sink-host"), 1)

    def test_kit_ships_what_install_checks(self):
        text = (KIT_SRC / "make-kit.sh").read_text()
        shipped = re.search(r"^files=\((.*?)\)$", text, re.M | re.S).group(1).split()
        for name in ("boot.img", '"modules-$release.tar"', "BUILD_ID", "KEY_ID", "SIGNER",
                     "install.sh", "rollback.sh", "verify.sh", "README.md"):
            self.assertIn(name, shipped)
        # vendor_boot is read from the release bundle and never packed.
        self.assertNotIn("vendor_boot.img", text.replace("$bundle/vendor_boot.img", ""))
        self.assertNotIn("write_part vendor_boot", (KIT_SRC / "install.sh").read_text())

    def test_tree_comparison_skips_build_products_and_nothing_else(self):
        """The M= module builds leave objects in the kernel source tree, and
        those differ with every build; a source file that differs, or one
        that only one tree has, still has to show."""
        text = (KIT_SRC / "make-kit.sh").read_text()
        options = re.search(r'^\tdiff -rq (.*?)\n\t\t"\$base_tree" "\$tree"', text, re.M | re.S)
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
        an offset, as the dwc3-qcom guard does over the CX-vote patch and
        ufs-qcom's power reference fix under the lane clock fix.  That
        backup is in the new tree alone and is the file as it was right
        before that patch: the one the replay recorded then, or else the
        release tree's; what was passed over is said on the console and in
        SOURCE.  Anything else called .orig still has to show: one with
        other content (also one the replay recorded otherwise), one of a
        file the kit's patches do not change, one both trees have and one
        that only the release tree has."""
        text = (KIT_SRC / "make-kit.sh").read_text()
        block = re.search(r'^\tbackups=\n\tfor f in "\$\{patched\[@\]\}"; do\n.*?^\tdone\n',
                          text, re.M | re.S)
        self.assertIsNotNone(block)
        said = re.search(r'^\tif \[ -n "\$backups" \]; then\n.*?^\tfi\n', text, re.M | re.S)
        self.assertIsNotNone(said)
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
            # A file the replay recorded right before the patch that kept
            # its backup (the dwc3-qcom guard over the CX-vote patch): the
            # backup has to be that, not the release tree's file; and one
            # recorded that the backup is not.
            for tree, tag in ((old, "old"), (new, "new")):
                (tree / "usb/dwc3.c").write_text("usb/dwc3.c of %s\n" % tag)
            (pathlib.Path(tmp, "orig/usb")).mkdir(parents=True)
            (pathlib.Path(tmp, "orig/usb/dwc3.c")).write_text("usb/dwc3.c of old, with the CX patch\n")
            (new / "usb/dwc3.c.orig").write_text("usb/dwc3.c of old, with the CX patch\n")
            (pathlib.Path(tmp, "orig/clk")).mkdir(parents=True)
            (pathlib.Path(tmp, "orig/clk/gcc.c")).write_text("recorded, and not the backup\n")
            script = ('set -euo pipefail; base_tree=$1 tree=$2 tmp=$3\n'
                      'patched=(usb/glue.c clk/gcc.c usb/tcpm.c include/tcpm.h usb/dwc3.c)\n'
                      'diff -rq "$base_tree" "$tree" > "$tmp/trees.diff" || :\n'
                      + block.group(0) +
                      'cat "$tmp/trees.diff"; echo "backups:$backups"\n'
                      'source_check=compared\n' + said.group(0) +
                      'echo "source check: $source_check"\n')
            result = subprocess.run(["bash", "-c", script, "backups", str(old), str(new), tmp],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(result.stdout.splitlines()), sorted([
            "Files %s and %s differ" % (old / name, new / name)
            for name in ("usb/glue.c", "clk/gcc.c", "usb/tcpm.c", "include/tcpm.h",
                         "usb/other.c", "usb/tcpm.c.orig", "usb/dwc3.c")
        ] + [
            "Only in %s: gcc.c.orig" % (new / "clk"),
            "Only in %s: other.c.orig" % (new / "usb"),
            "Only in %s: tcpm.h.orig" % (old / "include"),
            "backups: glue.c.orig dwc3.c.orig",
            "sources: passed over glue.c.orig dwc3.c.orig, the file as patch(1) kept it",
            "source check: compared; passed over, a copy of the file that patch(1) kept: glue.c.orig dwc3.c.orig",
        ]), result.stderr)
        # Nothing passed over: nothing said, and the source check as it was.
        quiet = subprocess.run(["bash", "-c", 'set -euo pipefail; backups= source_check=compared\n'
                                + said.group(0) + 'echo "source check: $source_check"\n'],
                               capture_output=True, text=True)
        self.assertEqual((quiet.returncode, quiet.stdout), (0, "source check: compared\n"),
                         quiet.stderr)

    def test_tree_comparison_passes_over_the_new_overlays_blobs_and_no_other(self):
        """Revision 8's two overlays are new: the release tree has no blob of
        either, the build tree has both (each compared with what this
        checkout's stage script makes, before the tree comparison).  Their
        "Only in" lines are passed over; another blob in the new tree alone,
        and an existing overlay's blob that differs, still show."""
        text = (KIT_SRC / "make-kit.sh").read_text()
        overlays = re.search(r"^overlays=\(([^)]*)\)$", text, re.M).group(1).split()
        new_overlays = re.search(r"^new_overlays=\(([^)]*)\)$", text, re.M).group(1).split()
        # The release v1.2.5 tree's stage script embedded the other three.
        self.assertEqual(new_overlays, ["gts9u-sync-state", "gts9u-ramoops"])
        self.assertTrue(set(new_overlays) < set(overlays), overlays)
        for o in new_overlays:
            self.assertTrue((REPO / "kernel/dts" / f"{o}.dtso").is_file(), o)
        block = re.search(r'^\tfor o in "\$\{new_overlays\[@\]\}"; do\n.*?^\tdone\n', text, re.M | re.S)
        self.assertIsNotNone(block)
        with tempfile.TemporaryDirectory() as tmp:
            old, new = pathlib.Path(tmp, "old"), pathlib.Path(tmp, "new")
            for tree, tag in ((old, "old"), (new, "new")):
                (tree / "drivers/soc/qcom").mkdir(parents=True)
                for o in ("gts9u-performance", "gts9-performance", "gts9-family"):
                    (tree / f"drivers/soc/qcom/{o}.dtbo").write_text(
                        tag if o == "gts9u-performance" else "same\n")
            for o in new_overlays + ["gts9u-other"]:
                (new / f"drivers/soc/qcom/{o}.dtbo").write_text("new\n")
            script = ('set -euo pipefail; base_tree=$1 tree=$2 tmp=$3\n'
                      'new_overlays=(%s)\n' % " ".join(new_overlays) +
                      'diff -rq "$base_tree" "$tree" > "$tmp/trees.diff" || :\n'
                      + block.group(0) + 'cat "$tmp/trees.diff"\n')
            result = subprocess.run(["bash", "-c", script, "overlays", str(old), str(new), tmp],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        qcom = "drivers/soc/qcom"
        self.assertEqual(sorted(result.stdout.splitlines()), sorted([
            "Files %s and %s differ" % (old / qcom / "gts9u-performance.dtbo", new / qcom / "gts9u-performance.dtbo"),
            "Only in %s: gts9u-other.dtbo" % (new / qcom),
        ]), result.stderr)

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
        patched file has six of them: the first form of this check counted
        the lines, expected two and refused the patched tree."""
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

    def test_the_image_check_looks_for_what_only_this_revision_has(self):
        """Revision 3's image has the first three strings as well, and the
        deadline of the Try.SNK wait is in the second revision of that patch
        alone.  The last six came after it: the log lines of the three fixes
        for a swap asked for from userspace, of which the third has three
        (the PR_Swap one is in no earlier revision of that patch), and the
        driver's mark for reading its parameters at each attach."""
        text = (KIT_SRC / "make-kit.sh").read_text()
        strings = shlex.split(re.search(r"^new_strings=\((.*?)\)$", text, re.M | re.S).group(1))
        self.assertEqual(strings, ["Requesting DFP before discovery", "sink_host_swap",
                                   "try_snk_extra_wait_ms", "try_snk_wait_until",
                                   "VDM in progress, DATA_ROLE_SWAP not started",
                                   "Data role swap ended with the role unchanged",
                                   "Swap is still queued, left pending",
                                   "Partner swapped first, queued DR_SWAP_SEND dropped",
                                   "Partner swapped first, queued PR_SWAP_SEND dropped",
                                   "policy_open"])
        tcpm_c = "drivers/usb/typec/tcpm/tcpm.c"
        wait = added_lines("tcpm-try-snk-extra-wait.patch")
        # A member of a structure: its name is in the image's type information.
        self.assertIn("\tunsigned long try_snk_wait_until;", wait[tcpm_c])
        self.assertIn("static unsigned int tcpm_try_snk_wait_left(struct tcpm_port *port)",
                      wait[tcpm_c])
        others = [(REPO / "kernel/patches" / name).read_text()
                  for name in ("tcpm-request-dfp-as-sink-before-discovery.patch",
                               "tcpm-no-dr-swap-during-vdm.patch",
                               "tcpm-dr-set-checks-data-role.patch",
                               "tcpm-keep-queued-swap-pending.patch",
                               "gcc-sm8550-usb-gdscs-retention.patch",
                               "dwc3-qcom-check-hcd-before-reading-port-speed.patch")]
        driver = (REPO / "kernel/drivers/sm5714_usbpd.c").read_text()
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
        names = kit_paths(text)
        # The sources, the objects compiled from them, vmlinux, the image.
        files = {"build/vmlinux": 300, "build/arch/arm64/boot/Image.gz": 400, "out/Image.gz": 400}
        for pair in shlex.split(objects.group(0)[len("objects=("):-1]):
            obj, source = kit_expand(pair, names).split(":")
            files[f"tree/{source}"] = 100
            files[f"build/{obj}"] = 200
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
                 + "".join(f"{name}={value}\n" for name, value in names.items()) + objects.group(0) + "\n"
                 + gate.group(0),
                 "gate", tmp], capture_output=True, text=True)
            return result.returncode, result.stdout, result.stderr.replace(tmp, "TMP")

    def test_the_image_has_to_be_built_from_the_sources_as_they_are(self):
        """The build skips a patch whose marker is in the tree already, and an
        incremental build after a change of clocks can leave an object, or
        the whole image, from before the last change to a source."""
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
            # The UFS PHY driver, which the stage script changes in a reused
            # tree.
            "phy-qcom-qmp-ufs.c changed since": ({f"tree/{QMP}.c": 250}, stale % (QMP, QMP + ".c")),
            "no phy-qcom-qmp-ufs object": ({f"build/{QMP}.o": None}, stale % (QMP, QMP + ".c")),
            # The driver includes the header too.
            "the driver's object alone is old": (
                {f"build/{driver}.o": 50, f"tree/{driver}.c": 40}, stale % (driver, header)),
            "an object as old as its source": ({f"build/{tcpm}.o": 100},
                                               stale % (tcpm, tcpm + ".c")),
            "an object that is not there": ({f"build/{GCC}.o": None}, stale % (GCC, GCC + ".c")),
            "an object compiled after the link": ({f"build/{GCC}.o": 350},
                                                  stale % (GCC, GCC + ".c")),
            "vmlinux linked after the image": ({"build/vmlinux": 450},
                                               "Image.gz is older than vmlinux\n"),
            # Revision 8's: the early helper and its generated header, ufshcd,
            # a header of the XO fixes.
            "the helper changed since": ({"tree/drivers/soc/qcom/gts9u-performance.c": 250},
                                         stale % ("drivers/soc/qcom/gts9u-performance",
                                                  "drivers/soc/qcom/gts9u-performance.c")),
            "its overlays changed since": ({"tree/drivers/soc/qcom/gts9u-performance-overlay.h": 250},
                                           stale % ("drivers/soc/qcom/gts9u-performance",
                                                    "drivers/soc/qcom/gts9u-performance-overlay.h")),
            "ufshcd.c changed since": ({"tree/drivers/ufs/core/ufshcd.c": 250},
                                       stale % ("drivers/ufs/core/ufshcd", "drivers/ufs/core/ufshcd.c")),
            "dsi_phy.h changed since": ({"tree/drivers/gpu/drm/msm/dsi/phy/dsi_phy.h": 250},
                                        stale % ("drivers/gpu/drm/msm/dsi/phy/dsi_phy",
                                                 "drivers/gpu/drm/msm/dsi/phy/dsi_phy.h")),
            # Revision 9's: the GMU's two files.
            "a6xx_hfi.c changed since": ({"tree/drivers/gpu/drm/msm/adreno/a6xx_hfi.c": 250},
                                         stale % ("drivers/gpu/drm/msm/adreno/a6xx_hfi",
                                                  "drivers/gpu/drm/msm/adreno/a6xx_hfi.c")),
            "no a6xx_hfi object": ({"build/drivers/gpu/drm/msm/adreno/a6xx_hfi.o": None},
                                   stale % ("drivers/gpu/drm/msm/adreno/a6xx_hfi",
                                            "drivers/gpu/drm/msm/adreno/a6xx_hfi.c")),
            "a6xx_gmu.c changed since": ({"tree/drivers/gpu/drm/msm/adreno/a6xx_gmu.c": 250},
                                         stale % ("drivers/gpu/drm/msm/adreno/a6xx_gmu",
                                                  "drivers/gpu/drm/msm/adreno/a6xx_gmu.c")),
        }
        for name, (ages, message) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.staleness_gate(ages), (1, "", message))
        with self.subTest("an image of another build in the output directory"):
            self.assertEqual(self.staleness_gate(out_image=b"other"),
                             (1, "", "TMP/out/Image.gz is not the build directory's\n"))

    def test_readmes_name_the_kit_and_what_the_scripts_use(self):
        self.assertIn("(typec-roles-ultra/README.md)", (REPO / "tools/inplace/README.md").read_text())
        self.assertIn("tools/inplace/typec-roles-ultra/README.md",
                      (REPO / "docs/usb-c-dock-roles.md").read_text())
        readme = " ".join((KIT_SRC / "README.md").read_text().split())
        install = (KIT_SRC / "install.sh").read_text()
        for name in (constant("install.sh", "backup"), constant("install.sh", "log"),
                     "boot-release.img", "sink_host_swap", "try_snk_extra_wait_ms"):
            with self.subTest(name):
                self.assertIn(name, readme)
        self.assertIn("boot-release.img", install)
        self.assertIn("**vendor_boot is not written**", readme)
        self.assertIn("`rollback.sh` enables it again", readme)
        # The release kernel refuses the kit's modules: what the way back
        # from TWRP costs until rollback.sh has put the release's back.
        self.assertIn("**Wi-Fi is down until `rollback.sh` has run**", readme)
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
        kit = re.search(r"^patches=\((.*?)\)$", text, re.M | re.S).group(1).split()
        self.assertEqual(sorted(re.findall(r"`kernel/patches/([^`]+)`", readme)), sorted(kit))
        self.assertEqual((len(kit), len([name for name in kit if name.startswith("tcpm-")])), (7, 5))
        self.assertIn("seven patches, five of them to TCPM.", readme)
        # What has run, and what has not.
        self.assertIn("This revision adds a third fix of that kind and a NULL check in the "
                      "dwc3-qcom glue, for what the SM-X710 showed in a boot with a dock "
                      "attached (the write-up has it); neither is in a kernel that has run on a "
                      "tablet. **None of the three fixes and neither switch has been tried on a "
                      "tablet yet**", readme)
        self.assertNotIn("was built on", readme)
        # A docked boot tests the host controller only with devices on the bus.
        self.assertIn("A boot with nothing on the bus does not test the host controller.", readme)
        self.assertIn("And it needs devices on the bus", readme)
        self.assertIn("Check 4 has not been run on any revision.", readme)
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
        # The L15B check judges only a kernel that logs the step, revisions
        # 1 to 7 have none, and revision 8's has to.
        self.assertIn("With that line `verify.sh` checks that the running DT's ldo15 has neither "
                      "property and that the regulator reads `normal` (HPM), and fails on the kit's "
                      "kernel if not. Without it, as on revisions 1 to 7, it prints a note and checks "
                      "nothing; revision 8's `make-kit.sh` requires the step in its kernel, and on that "
                      "kernel a missing line is a FAIL.", readme)
        # Revision 8: what it is, and that nothing of it has run.
        self.assertIn("**Revision 8 (2026-11-25): the deep-sleep groundwork. Written and checked on the host; "
                      "no kernel has been built for it, no kit packed, nothing of it has run on an SM-X910.**",
                      readme)
        for name in ("APPLY_QMP_UFS_PCS_STATE_DIAGNOSTIC=1", "APPLY_DEEP_SLEEP_DEBUG_SWITCHES=1",
                     "APPLY_SM8550_SLEEP_FIXES=1", "APPLY_SM8550_PCIE0_GDSC_OFF=1", "APPLY_SM8550_PCIE0_GDSC_GUARD=1",
                     "APPLY_LPASS_VA_MACRO_PM_CLK=1", "APPLY_DSI_PHY_AHB_RELEASE=1",
                     "APPLY_GENI_SERIAL_FORCE_SUSPEND=1"):
            with self.subTest(name):
                self.assertIn(name, (KIT_SRC / "README.md").read_text())
                self.assertIn(name, text)
        self.assertIn("`f81161f20d148a8e649bc240cddcb82b8898a1af0558f2346643ece0b15190fc`", readme)
        for said in ("until the reboot the running kernel can load no module it has not loaded yet",
                     "while the kit is installed: rollback.sh first, then upgrade "
                     "ubuntu-gts9u-device or ubuntu-gts9u-hardware"):
            self.assertIn(f"say '{said}'", install)


def make_kit_array(name):
    """make-kit.sh's array NAME, its words as bash gives them."""
    text = (KIT_SRC / "make-kit.sh").read_text()
    return shlex.split(re.search(rf"^{name}=\((.*?)\)$", text, re.M | re.S).group(1))


def make_kit_block(start, end):
    """The lines of make-kit.sh from the one that starts with START to the
    first one after it that is END."""
    text = (KIT_SRC / "make-kit.sh").read_text()
    match = re.search(rf"^{re.escape(start)}.*?^{re.escape(end)}\n", text, re.M | re.S)
    assert match, start
    return match.group(0)


def make_kit_definitions(*names):
    """make-kit.sh's own definitions (NAME=... or NAME=(...)) of NAMES."""
    text = (KIT_SRC / "make-kit.sh").read_text()
    return "".join(re.search(rf"^{name}=(\(.*?\)|\S+)$\n", text, re.M | re.S).group(0) for name in names)


class Revision9Test(unittest.TestCase):
    """What make-kit.sh adds for revision 9: the GMU's two HFI fixes and the
    swap back to sink in the build's order, their strings, the driver's
    switches as it declares them, and the GMU's HFI wait in vmlinux.  The
    checks of the driver's switches in modules.builtin.modinfo (6c) and of
    the kernel before this one (PREV_KIT, 5a) are revision 10's since, in
    Revision10Test."""

    text = (KIT_SRC / "make-kit.sh").read_text()

    def test_the_three_patches_are_the_build_s_in_its_order(self):
        """The two GMU patches go in every build right after the sleep
        fixes, whose stale RPMh vote fix is a6xx_gmu.c's too, and the swap
        back right after the third swap fix; no APPLY_* variable gates them.
        The replay has them there, and each file they change is compared."""
        self.assertEqual(make_kit_array("gpu_patches"), ["msm-adreno-a6xx-hfi-irq-leave-polled-bits.patch",
                                                         "msm-adreno-a6xx-hfi-take-queued-response.patch"])
        self.assertEqual(make_kit_array("swap_back_patches"), ["tcpm-swap-back-to-sink.patch"])
        order = make_kit_array("build_order")
        at = order.index("${fix_patches[@]}")
        self.assertEqual(order[at + 1], "${gpu_patches[@]}")
        at = order.index("tcpm-keep-queued-swap-pending.patch")
        self.assertEqual(order[at + 1:], ["${swap_back_patches[@]}"])
        build = (REPO / "scripts/build-mainline-kernel.sh").read_text().replace("\\\n", " ")
        lines = build.splitlines()
        for name, previous in (("msm-adreno-a6xx-hfi-irq-leave-polled-bits.patch",
                                "ufs-qcom-enable-only-lane-clocks.patch"),
                               ("msm-adreno-a6xx-hfi-take-queued-response.patch",
                                "msm-adreno-a6xx-hfi-irq-leave-polled-bits.patch"),
                               ("tcpm-swap-back-to-sink.patch", "tcpm-keep-queued-swap-pending.patch")):
            with self.subTest(name):
                (number,) = [n for n, line in enumerate(lines) if re.fullmatch(
                    rf"apply_unless '[^']+'\s+\S+\s+{re.escape(name)}\s*", line)]
                # At the top level of the script (not indented: not under an
                # APPLY_* test), right after the patch before it.
                code_before = [line for line in lines[:number] if line.strip() and not line.lstrip().startswith("#")]
                self.assertTrue(code_before[-1].endswith(previous), code_before[-1])
        paths = kit_paths(self.text)
        patched = {kit_expand(word, paths) for word in make_kit_array("patched")}
        for name in (*make_kit_array("gpu_patches"), *make_kit_array("swap_back_patches")):
            with self.subTest(name):
                files = set(re.findall(r"^\+\+\+ b/(\S+)", (REPO / "kernel/patches" / name).read_text(), re.M))
                self.assertTrue(files <= patched, files - patched)
        self.assertEqual(paths["hfi"], "drivers/gpu/drm/msm/adreno/a6xx_hfi")
        self.assertEqual({kit_expand(w, paths) for w in make_kit_array("gpu_files")},
                         {paths["gmu"] + ".c", paths["hfi"] + ".c"})

    def test_the_build_tree_is_checked_for_the_build_s_markers(self):
        """The build applies each of the three by a marker (apply_unless);
        make-kit.sh looks for that marker in the build tree, and for its
        absence in the release tree."""
        build = (REPO / "scripts/build-mainline-kernel.sh").read_text().replace("\\\n", " ")
        for name, marker, path in (("msm-adreno-a6xx-hfi-irq-leave-polled-bits.patch", "Leave those bits to them",
                                    '"$tree/$gmu.c"'),
                                   ("msm-adreno-a6xx-hfi-take-queued-response.patch",
                                    "response that is already in the queue ends the wait too", '"$tree/$hfi.c"'),
                                   ("tcpm-swap-back-to-sink.patch", "request_sink_as_source",
                                    '"$tree/include/linux/usb/tcpm.h"')):
            with self.subTest(name):
                self.assertRegex(build, rf"(?m)^apply_unless '{re.escape(marker)}'\s+\S+\s+{re.escape(name)}\s*$")
                self.assertIn(f"grep -q '{marker}' {path}", self.text)
                self.assertIn(f"! grep -q '{marker}' \"$base_tree/", self.text)
                self.assertIn(marker, "\n".join(line for lines in added_lines(name).values() for line in lines))
        driver = (REPO / "kernel/drivers/sm5714_usbpd.c").read_text()
        self.assertIn("\tsm->tcpc.request_sink_as_source = swap_back_to_sink;\n", driver)
        self.assertIn("grep -q 'sm->tcpc.request_sink_as_source = swap_back_to_sink;' "
                      '"$tree/$tcpm_dir/sm5714_usbpd.c"', self.text)
        self.assertIn("static void tcpm_swap_back_work(struct work_struct *work)",
                      added_lines("tcpm-swap-back-to-sink.patch")["drivers/usb/typec/tcpm/tcpm.c"])

    def test_revision_9_s_strings_are_their_sources_own(self):
        """gpu_strings are the hfi interrupt fix's; swap_back_strings the swap
        back's (tcpm.c or tcpm.h) or the driver's, the six new switches
        among them; none is in the added lines of any other patch the kit
        replays, nor in the pinned kernel's TCPM or GMU sources."""
        gpu, swap_back = make_kit_array("gpu_strings"), make_kit_array("swap_back_strings")
        self.assertEqual(len(gpu + swap_back), len(set(gpu + swap_back)))
        mine = {"msm-adreno-a6xx-hfi-irq-leave-polled-bits.patch", "msm-adreno-a6xx-hfi-take-queued-response.patch",
                "tcpm-swap-back-to-sink.patch"}
        gmu_added = "\n".join(added_lines("msm-adreno-a6xx-hfi-irq-leave-polled-bits.patch")[
            "drivers/gpu/drm/msm/adreno/a6xx_gmu.c"])
        tcpm_added = "\n".join(line for lines in added_lines("tcpm-swap-back-to-sink.patch").values()
                               for line in lines)
        driver = (REPO / "kernel/drivers/sm5714_usbpd.c").read_text()
        for s in gpu:
            with self.subTest(s):
                self.assertIn(f'"{s}\\n"', gmu_added)
        for s in swap_back:
            with self.subTest(s):
                self.assertTrue(s in tcpm_added or s in driver, s)
        self.assertTrue(set(R9_SWITCHES) <= set(swap_back))
        for name in R9_SWITCHES:
            self.assertIn(f"module_param({name}, ", driver)
        others = [p for p in make_kit_array("patches") + make_kit_array("cx_patches") + make_kit_array("fix_patches")
                  + make_kit_array("xo_patches") + make_kit_array("debug_patches") + make_kit_array("pcie_patches")
                  + make_kit_array("gdsc_patches") + make_kit_array("ufs_qcom_patches") + make_kit_array("ufs_patches")
                  if p not in mine]
        other_added = "\n".join(line for p in others for lines in added_lines(p).values() for line in lines)
        for s in gpu + swap_back:
            with self.subTest(other=s):
                self.assertNotIn(s, other_added)
        kernel = kernel_source()
        if kernel is None:
            return
        upstream = "".join(subprocess.run(["git", "-C", str(kernel), "show", f"HEAD:{path}"], check=True,
                                          capture_output=True, text=True).stdout
                           for path in ("drivers/usb/typec/tcpm/tcpm.c", "include/linux/usb/tcpm.h",
                                        "drivers/gpu/drm/msm/adreno/a6xx_gmu.c",
                                        "drivers/gpu/drm/msm/adreno/a6xx_hfi.c"))
        for s in gpu + swap_back:
            with self.subTest(upstream=s):
                self.assertNotIn(s, upstream)
        # The new state is not a prefix of an upstream name (GET_SOURCE_CAP
        # alone is: GET_SOURCE_CAPABILITIES).
        self.assertIn("GET_SOURCE_CAPABILITIES", upstream)
        self.assertIn("GET_SOURCE_CAP_TIMEOUT", swap_back)
        self.assertNotIn("GET_SOURCE_CAP", swap_back)

    def test_the_switches_are_the_driver_s_with_their_types_and_defaults(self):
        """make-kit.sh's switches are the driver's module_param()s, in its
        order and with its types; verify.sh's defaults are the driver's
        initial values (a bool without one is N, a number 0), and so are the
        tests' and the README's."""
        driver = (REPO / "kernel/drivers/sm5714_usbpd.c").read_text()
        declared = re.findall(r"^module_param\((\w+), (\w+), 0644\);$", driver, re.M)
        self.assertEqual([f"{name}:{kind}" for name, kind in declared], make_kit_array("switches"))
        self.assertEqual([name for name, _ in declared], list(SWITCHES))
        defaults = {}
        for name, kind in declared:
            init = re.search(rf"^static (bool|unsigned int) {name}( = ([^;]+))?;$", driver, re.M)
            self.assertIsNotNone(init, name)
            self.assertEqual(init.group(1), {"bool": "bool", "uint": "unsigned int"}[kind])
            value = init.group(3)
            if kind == "bool":
                defaults[name] = {"true": "Y", None: "N", "false": "N"}[value]
            else:
                defaults[name] = value or "0"
        self.assertEqual(defaults, SWITCHES)
        verify = (KIT_SRC / "verify.sh").read_text()
        listed = re.search(r"^for p in (otg_rp:.*?); do$", verify, re.M | re.S).group(1).replace("\\\n\t", "")
        self.assertEqual(dict(p.split(":") for p in listed.split()), SWITCHES)
        # Revision 9's table has its six, revision 10's its two.
        readme = (KIT_SRC / "README.md").read_text()
        for name in tuple(SWITCHES)[4:]:
            with self.subTest(name):
                self.assertIn(f"| `{name}` | `{SWITCHES[name]}` |", readme)

    # a6xx_hfi_wait_for_msg_interrupt() as llvm-objdump shows it in revision
    # 8's vmlinux (the release's code: its a6xx_hfi.o has the same text) and
    # in revision 9's object (abridged): the fix's load of the response
    # queue's header is "ldr x9, [x20, #0x6f8]", gmu + 1720 + 64 + 0.
    WAIT_RELEASE = """
vmlinux:\tfile format elf64-littleaarch64

Disassembly of section .text:

ffff800080e5fa20 <a6xx_hfi_wait_for_msg_interrupt>:
ffff800080e5fa50:      \tmov\tw21, w1
ffff800080e5fa54:      \tmov\tx20, x0
ffff800080e5fa60:      \tbl\t0xffff8000801f9db8 <ktime_get>
ffff800080e5fa6c:      \tldr\tw8, [x20, #0x30]
ffff800080e5fa70:      \tldr\tx9, [x20, #0x28]
ffff800080e5fa7c:      \tldr\tw9, [x8]
ffff800080e5fa80:      \tdmb\toshld
ffff800080e5fa94:      \ttbnz\tw9, #0x0, 0xffff800080e5fae8 <a6xx_hfi_wait_for_msg_interrupt+0xc8>
"""
    WAIT_NEW = WAIT_RELEASE.replace(
        "ffff800080e5fa94:      \ttbnz\tw9, #0x0, 0xffff800080e5fae8 <a6xx_hfi_wait_for_msg_interrupt+0xc8>\n",
        "ffff800080e5fa90:      \ttbnz\tw8, #0x0, 0xffff800080e5fb08 <a6xx_hfi_wait_for_msg_interrupt+0xe4>\n"
        "ffff800080e5faa4:      \tldr\tx9, [x20, #0x6f8]\n"
        "ffff800080e5faa8:      \tldr\tw8, [x9, #0x28]\n"
        "ffff800080e5faac:      \tldr\tw9, [x9, #0x2c]\n"
        "ffff800080e5fab0:      \tcmp\tw8, w9\n")
    GMU_PAHOLE = ("struct a6xx_gmu {\n\tstruct device *             dev;                  /*     0     8 */\n"
                  "\tvoid *                     mmio;                 /*    40     8 */\n"
                  "\tstruct a6xx_hfi_queue      queues[3];            /*  1720   192 */\n"
                  "\t/* size: 2000, cachelines: 32, members: 43 */\n};\n")
    QUEUE_PAHOLE = ("struct a6xx_hfi_queue {\n\tstruct a6xx_hfi_queue_header * header;           /*     0     8 */\n"
                    "\tspinlock_t                 lock;                 /*     8     4 */\n"
                    "\t/* size: 64, cachelines: 1, members: 6 */\n};\n")
    NM = "ffff800080e5d014 t a6xx_hfi_irq\nffff800080e5fa20 t a6xx_hfi_wait_for_msg_interrupt\n"

    def hfi_gate(self, new=None, nm=None, gmu=None, base=None):
        """Section 5d on stand-ins: vmlinux's disassembly and symbols, the
        object's pahole layouts and .text; BASE, when given, is the release
        build's (disassembly, symbols, .text)."""
        start = self.text.index("struct_size() { # OBJECT STRUCT")
        end = self.text.index('echo "GMU HFI and swap back to sink: $fix_check"\n')
        block = self.text[start:end + len('echo "GMU HFI and swap back to sink: $fix_check"\n')]
        member = re.search(r"^member_offset\(\) \{.*?^\}\n", self.text, re.M | re.S).group(0)
        nm_has = re.search(r"^nm_has\(\) \{.*\}.*\n", self.text, re.M).group(0)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            bin_ = tmp / "bin"
            bin_.mkdir()
            (bin_ / "llvm-objdump").write_text('#!/bin/sh\nfor last; do :; done\ncat "$last.dis"\n')
            (bin_ / "pahole").write_text('#!/bin/sh\ncat "$3.$2" 2>/dev/null\n')
            (bin_ / "llvm-objcopy").write_text('#!/bin/sh\ncp "$4.text" "$5"\n')
            for stub in bin_.iterdir():
                stub.chmod(0o755)
            obj = tmp / "build/drivers/gpu/drm/msm/adreno/a6xx_hfi.o"
            obj.parent.mkdir(parents=True)
            obj.write_text("object")
            (tmp / "build/vmlinux.dis").write_text(self.WAIT_NEW if new is None else new)
            (obj.parent / "a6xx_hfi.o.a6xx_gmu").write_text(self.GMU_PAHOLE if gmu is None else gmu)
            (obj.parent / "a6xx_hfi.o.a6xx_hfi_queue").write_text(self.QUEUE_PAHOLE)
            (obj.parent / "a6xx_hfi.o.text").write_bytes(b"new code")
            (tmp / "vmlinux.nm").write_text(self.NM + "ffff8000809a3d0c t tcpm_swap_back_work\n" if nm is None else nm)
            setup = (f'set -euo pipefail; export LC_ALL=C\ndie() {{ echo "$*" >&2; exit 1; }}\n'
                     f'build={tmp}/build tmp={tmp} new_id={"1" * 40}\n'
                     + make_kit_definitions("hfi", "gpu_patches") + member + nm_has)
            if base is not None:
                dis, base_nm, text = base
                b = tmp / "base/linux-gts9uwifi"
                (b / "drivers/gpu/drm/msm/adreno").mkdir(parents=True)
                (b / "vmlinux.dis").write_text(dis)
                (b / "drivers/gpu/drm/msm/adreno/a6xx_hfi.o").write_text("object")
                (b / "drivers/gpu/drm/msm/adreno/a6xx_hfi.o.text").write_bytes(text)
                (tmp / "vmlinux.base.nm").write_text(base_nm)
                setup += f"BASE_SCRATCH={tmp}/base\n"
            env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}")
            result = subprocess.run(["bash", "-c", setup + block], capture_output=True, text=True, env=env)
            return result.returncode, result.stdout + result.stderr

    def test_the_hfi_wait_reads_the_response_queue_and_the_swap_back_is_linked(self):
        said = ("GMU HFI and swap back to sink: vmlinux (build ID 111111111111) has tcpm_swap_back_work(), "
                "a6xx_hfi_irq() and a6xx_hfi_wait_for_msg_interrupt(), and the wait reads the response queue's "
                "header (gmu + 1784, pahole)")
        self.assertEqual(self.hfi_gate(), (0, said + "\n"))
        release = (self.WAIT_RELEASE, self.NM, b"release code")
        self.assertEqual(self.hfi_gate(base=release),
                         (0, said + "; the release build's has no swap back, its wait reads no such field, and its "
                                    "drivers/gpu/drm/msm/adreno/a6xx_hfi.o has other code\n"))
        for name, kwargs, message in (
                ("the release's wait", {"new": self.WAIT_RELEASE},
                 "does not read the response queue's header (gmu + 1784): "
                 "msm-adreno-a6xx-hfi-take-queued-response.patch is not in it"),
                ("another layout", {"gmu": self.GMU_PAHOLE.replace("/*  1720", "/*  1728")},
                 "does not read the response queue's header (gmu + 1792)"),
                ("no layout", {"gmu": ""}, "pahole gives no offsets of struct a6xx_gmu's queues"),
                ("no swap back", {"nm": self.NM}, "no tcpm_swap_back_work in"),
                ("the wait inlined", {"nm": "ffff800080e5d014 t a6xx_hfi_irq\nffff8000809a3d0c t tcpm_swap_back_work\n"},
                 "no a6xx_hfi_wait_for_msg_interrupt in"),
                ("a release with the swap back", {"base": (self.WAIT_RELEASE, self.NM + "1 t tcpm_swap_back_work\n",
                                                           b"release code")},
                 "self-test failed: the release vmlinux has tcpm_swap_back_work() too"),
                ("a release whose wait reads the header", {"base": (self.WAIT_NEW, self.NM, b"release code")},
                 "self-test failed: the release build's a6xx_hfi_wait_for_msg_interrupt() reads no field"),
                ("a release object with the new code", {"base": (self.WAIT_RELEASE, self.NM, b"new code")},
                 "the compiled drivers/gpu/drm/msm/adreno/a6xx_hfi.o is the release build's")):
            with self.subTest(name):
                code, out = self.hfi_gate(**kwargs)
                self.assertEqual(code, 1, out)
                self.assertIn(message, out)

    def test_the_readme_says_what_revision_9_is_and_that_none_of_it_has_run(self):
        readme = " ".join((KIT_SRC / "README.md").read_text().split())
        for said in ("**Revision 9 (2026-11-26): the GPU's one-second freezes and the swap back to sink. "
                     "Built and checked on the host; nothing that it adds has run on a tablet.**",
                     "**Both were written from nine timeouts captured on this tablet with the gpu-watch kit on "
                     "2026-11-26, under revision 8's kernel, and neither has run on a tablet.**",
                     "**None of revision 9's dock code has run on a tablet.**",
                     "(`mv ~/typec-roles ~/typec-roles-r8`)",
                     "twenty-three files and two overlay blobs may differ"):
            with self.subTest(said):
                self.assertIn(said, readme)
        # The Build section's REVISION and PREV_KIT are revision 10's since
        # (Revision10Test).
        for name in (*make_kit_array("gpu_patches"), *make_kit_array("swap_back_patches")):
            self.assertIn(f"`{name}`", readme)
        for line in ("timed out waiting for response", "Timeout waiting for GMU OOB", "Unexpected message id",
                     "GMU interrupt mask"):
            self.assertIn(f"`{line}`", readme)
        self.assertNotIn("twenty-two files", readme)


# The commits that hold what revision 10 changes: revision 9's kit (its
# scripts, driver and patches, 319b074), revision 8's kit's scripts
# (2bb395a), and the Hard Reset fix (7232cff; its parent has the release's
# tcpm-use-retained-sink-data-role.patch).
R9_COMMIT = "319b07433a72a1c9e2a15789a9778c447041f94f"
R8_COMMIT = "2bb395a1306b03541a1dbec19ab6b85b03c2f112"
HARD_RESET_COMMIT = "7232cffd1c55701c68d581a26dd7344f79e3069b"


def repo_file_at(commit, path):
    """PATH as COMMIT of this checkout has it, or None without git or the
    commit (an export of the tree)."""
    try:
        return subprocess.run(["git", "-C", str(REPO), "show", f"{commit}:{path}"], check=True,
                              capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None


def code_lines(script):
    """A shell script's lines that are not comments or blank."""
    return [line for line in script.splitlines() if line.strip() and not line.lstrip().startswith("#")]


class Revision10Test(unittest.TestCase):
    """What make-kit.sh adds for revision 10: TCPM's Hard Reset role
    (data_role_swapped gone from tcpm_port, the release's Hard Reset hunk
    taken out in the replay), the driver's CC reopen, self-heal and watchdog
    with its two new switches, their strings, and revision 9's kit
    (PREV_KIT) as the kernel that has what revision 10 takes out and none of
    what it adds."""

    text = (KIT_SRC / "make-kit.sh").read_text()

    def test_no_patch_was_added_or_removed(self):
        """The replay's patches are revision 9's; the patch that lost its
        Hard Reset hunk is one the build applies before the five TCPM
        patches, and not in the replay (the release tree has it): named,
        required and recorded in SOURCE; the two TCPM patches have nothing
        left of what they had."""
        r9 = repo_file_at(R9_COMMIT, "tools/inplace/typec-roles-ultra/make-kit.sh")
        if r9 is not None:
            for name in ("build_order", "patches", "gpu_patches", "swap_back_patches", "patched"):
                with self.subTest(name):
                    self.assertEqual(make_kit_array(name), shlex.split(
                        re.search(rf"^{name}=\((.*?)\)$", r9, re.M | re.S).group(1)))
        self.assertEqual(constant("make-kit.sh", "retained_patch"), "tcpm-use-retained-sink-data-role.patch")
        build = (REPO / "scripts/build-mainline-kernel.sh").read_text().replace("\\\n", " ")
        lines = [line for line in build.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        (at,) = [n for n, line in enumerate(lines)
                 if re.fullmatch(r"apply_unless '[^']+'\s+\S+\s+tcpm-use-retained-sink-data-role\.patch\s*", line)]
        (dfp,) = [n for n, line in enumerate(lines)
                  if line.endswith(" tcpm-request-dfp-as-sink-before-discovery.patch")]
        self.assertLess(at, dfp)
        self.assertNotIn("tcpm-use-retained-sink-data-role.patch", make_kit_array("build_order"))
        required = self.text[self.text.index('for f in "$mkbootimg"'):]
        self.assertIn('"$repo/kernel/patches/$retained_patch"', required[:required.index("; do\n")])
        self.assertIn('for p in "${build_order[@]}" "$pcs_patch" "${ufs_patches[@]}" "$retained_patch"; do\n',
                      self.text)
        retained = (REPO / "kernel/patches/tcpm-use-retained-sink-data-role.patch").read_text()
        self.assertNotIn("TYPEC_HOST : tcpm_data_role_for_sink(port)", retained)
        self.assertNotIn("SNK_HARD_RESET", retained)
        dfp_patch = (REPO / "kernel/patches/tcpm-request-dfp-as-sink-before-discovery.patch").read_text()
        self.assertNotIn("data_role_swapped", dfp_patch)
        self.assertNotIn("TYPEC_HOST : tcpm_data_role_for_sink(port)", dfp_patch)
        for name in make_kit_array("patches") + make_kit_array("swap_back_patches"):
            with self.subTest(name):
                self.assertNotIn("data_role_swapped", (REPO / "kernel/patches" / name).read_text())

    def test_the_hunk_taken_out_is_the_release_patch_s(self):
        """The hunk make-kit.sh writes is the last hunk of the release's
        tcpm-use-retained-sink-data-role.patch byte for byte, the one
        7232cff took out; the current patch is that one without it."""
        hunk = subprocess.run(["bash", "-c", re.search(r"^release_hard_reset_hunk\(\) \{.*?^\}\n", self.text,
                                                       re.M | re.S).group(0) + "release_hard_reset_hunk"],
                              check=True, capture_output=True, text=True).stdout
        lines = hunk.splitlines()
        self.assertEqual(lines[:2], ["--- a/drivers/usb/typec/tcpm/tcpm.c", "+++ b/drivers/usb/typec/tcpm/tcpm.c"])
        self.assertEqual([line[0] for line in lines[3:]], [" "] * 3 + ["-"] + ["+"] * 3 + [" "] * 3)
        self.assertEqual(lines[9], "+\t\t\t       TYPEC_HOST : tcpm_data_role_for_sink(port));")
        old = repo_file_at(HARD_RESET_COMMIT + "^", "kernel/patches/tcpm-use-retained-sink-data-role.patch")
        if old is None:
            self.skipTest("no git history")
        self.assertTrue(old.endswith("\n".join(lines[2:]) + "\n"), old[-600:])
        new = (REPO / "kernel/patches/tcpm-use-retained-sink-data-role.patch").read_text()
        self.assertEqual(new.split("\n---\n", 1)[1].rstrip("\n"),
                         old[:old.index("\n".join(lines[2:]))].rstrip("\n"))

    def test_the_replay_takes_the_hunk_out_of_a_model_of_the_release_tree(self):
        """The release tree's tcpm.c, modelled from the pinned kernel with
        the release build's two TCPM patches (the adopt patch and the
        release's retained-role patch, as patch(1) applies them, keeping
        tcpm.c.orig), gives this build's tcpm.c by make-kit.sh's replay: the
        hunk out, then the kit's six TCPM patches in the build's order.  And
        the tree comparison passes over the model's tcpm.c.orig (in the
        release tree alone) as what patch(1) kept, and shows one that is
        not that file."""
        kernel = kernel_source()
        old = repo_file_at(HARD_RESET_COMMIT + "^", "kernel/patches/tcpm-use-retained-sink-data-role.patch")
        if kernel is None or old is None:
            self.skipTest("no pinned kernel checkout, or no git history")
        tcpm_c, tcpm_h = "drivers/usb/typec/tcpm/tcpm.c", "include/linux/usb/tcpm.h"
        hunk = subprocess.run(["bash", "-c", re.search(r"^release_hard_reset_hunk\(\) \{.*?^\}\n", self.text,
                                                       re.M | re.S).group(0) + "release_hard_reset_hunk"],
                              check=True, capture_output=True, text=True).stdout
        tcpm_patches = [p for p in make_kit_array("patches") if p.startswith("tcpm-")] + \
            make_kit_array("swap_back_patches")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            (tmp / "old-retained.patch").write_text(old)
            (tmp / "hunk").write_text(hunk)

            def tree(name, *patches):
                root = tmp / name
                for path in (tcpm_c, tcpm_h):
                    (root / path).parent.mkdir(parents=True, exist_ok=True)
                    (root / path).write_text(subprocess.run(
                        ["git", "-C", str(kernel), "show", f"HEAD:{path}"], check=True, capture_output=True,
                        text=True).stdout)
                for patch in patches:
                    # As scripts/build-mainline-kernel.sh applies one: it keeps
                    # FILE.orig where a hunk goes in at an offset.
                    with open(patch) as stream:
                        subprocess.run(["patch", "-s", "--forward", "--batch", "-d", str(root), "-p1"],
                                       stdin=stream, check=True, capture_output=True)
                return root

            adopt = REPO / "kernel/patches/tcpm-adopt-retained-source-ufp-role.patch"
            new_retained = REPO / "kernel/patches/tcpm-use-retained-sink-data-role.patch"
            release = tree("release", adopt, tmp / "old-retained.patch")
            build = tree("build", adopt, new_retained, *(REPO / "kernel/patches" / p for p in tcpm_patches))
            # The release tree as the build left it has the backup; this
            # build's tree has none.
            self.assertTrue((release / f"{tcpm_c}.orig").exists())
            self.assertFalse((build / f"{tcpm_c}.orig").exists())
            # The replay.
            src = tmp / "src"
            for path in (tcpm_c, tcpm_h):
                (src / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(release / path, src / path)
            with open(tmp / "hunk") as stream:
                subprocess.run(["patch", "-s", "--reverse", "--force", "--fuzz=0", "--no-backup-if-mismatch",
                                "-d", str(src), "-p1"], stdin=stream, check=True)
            for p in tcpm_patches:
                with open(REPO / "kernel/patches" / p) as stream:
                    subprocess.run(["patch", "-s", "--forward", "--batch", "--no-backup-if-mismatch",
                                    "-d", str(src), "-p1"], stdin=stream, check=True)
            for path in (tcpm_c, tcpm_h):
                with self.subTest(path):
                    self.assertEqual((src / path).read_text(), (build / path).read_text())
            # The backup that only the release tree has.
            block = make_kit_block("\trelease_backup=", "\tfi")
            said = re.search(r'^\tif \[ -n "\$release_backup" \]; then\n.*?^\tfi\n', self.text, re.M | re.S)
            for name, damage, passed in (("the file patch(1) kept", None, True),
                                         ("another file", "not the file\n", False)):
                with self.subTest(name):
                    if damage:
                        (release / f"{tcpm_c}.orig").write_text(damage)
                    work = tmp / f"work-{passed}"
                    work.mkdir()
                    shutil.copy(tmp / "hunk", work / "hard-reset.hunk")
                    script = ('set -euo pipefail; base_tree=$1 tree=$2 tmp=$3 repo=$4\n'
                              'tcpm_dir=drivers/usb/typec/tcpm retained_patch=tcpm-use-retained-sink-data-role.patch\n'
                              'diff -rq "$base_tree" "$tree" > "$tmp/trees.diff" || :\n'
                              + block + 'cat "$tmp/trees.diff"; echo "release_backup:$release_backup"\n'
                              'source_check=compared\n' + said.group(0) + 'echo "source check: $source_check"\n')
                    result = subprocess.run(["bash", "-c", script, "orig", str(release), str(build), str(work),
                                             str(REPO)], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    lines = sorted(result.stdout.splitlines())
                    only = f"Only in {release / 'drivers/usb/typec/tcpm'}: tcpm.c.orig"
                    if passed:
                        self.assertNotIn(only, lines)
                        self.assertIn("release_backup: tcpm.c.orig", lines)
                        self.assertIn("sources: passed over the release tree's tcpm.c.orig, the file before its "
                                      "tcpm-use-retained-sink-data-role.patch as patch(1) kept it", lines)
                        self.assertIn("source check: compared; passed over, in the release tree alone, the copy "
                                      "that its patch(1) kept of the file before its "
                                      "tcpm-use-retained-sink-data-role.patch: tcpm.c.orig", lines)
                    else:
                        self.assertIn(only, lines)
                        self.assertIn("release_backup:", lines)
                        self.assertIn("source check: compared", lines)
                    # tcpm.c itself differs, by the kit's patches.
                    self.assertIn(f"Files {release / tcpm_c} and {build / tcpm_c} differ", lines)

    def marker_gate(self, tcpm, driver):
        """The build tree's revision 10 markers (section 3) on a stand-in
        tcpm.c and sm5714_usbpd.c."""
        block = make_kit_block("! grep -q 'data_role_swapped' \"$tree/$tcpm_dir/tcpm.c\" ||",
                               "\tdie \"the build tree's sm5714_usbpd.c has no cc_reopen_via_snk or no cc_self_heal\"")
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp, "drivers/usb/typec/tcpm")
            d.mkdir(parents=True)
            (d / "tcpm.c").write_text(tcpm)
            (d / "sm5714_usbpd.c").write_text(driver)
            setup = (f'set -euo pipefail; export LC_ALL=C\ndie() {{ echo "$*" >&2; exit 1; }}\ntree={tmp}\n'
                     + make_kit_definitions("tcpm_dir", "retained_patch") + "patches=(" +
                     " ".join(make_kit_array("patches")) + ")\n")
            result = subprocess.run(["bash", "-c", setup + block + "echo ok\n"], capture_output=True, text=True)
            return result.returncode, result.stdout + result.stderr

    def test_the_build_tree_is_checked_for_revision_10_s_markers(self):
        driver = (REPO / "kernel/drivers/sm5714_usbpd.c").read_text()
        tcpm = "\t\ttcpm_set_roles(port, port->self_powered, TYPEC_STATE_USB, TYPEC_SINK,\n" \
               "\t\t\t       tcpm_data_role_for_sink(port));\n\tktime_t sink_dfp_not_before;\n"
        self.assertEqual(self.marker_gate(tcpm, driver), (0, "ok\n"))
        r9_driver = repo_file_at(R9_COMMIT, "kernel/drivers/sm5714_usbpd.c") or driver.replace(
            "module_param(cc_self_heal, bool, 0644);", "")
        for name, args, message in (
                ("revision 9's request-dfp patch", (tcpm + "\tbool data_role_swapped;\n", driver),
                 "has data_role_swapped: its tcpm-request-dfp-as-sink-before-discovery.patch is revision 9's"),
                ("the host role kept at a Hard Reset", (tcpm.replace(
                    "tcpm_data_role_for_sink(port));", "port->tcpc->adopt_retained_source_ufp &&\n"
                    "\t\t\t       port->data_role == TYPEC_HOST ?\n"
                    "\t\t\t       TYPEC_HOST : tcpm_data_role_for_sink(port));"), driver),
                 "keeps a sink's host role across a Hard Reset: its tcpm-use-retained-sink-data-role.patch or "
                 "tcpm-request-dfp-as-sink-before-discovery.patch is revision 9's"),
                ("revision 9's driver", (tcpm, r9_driver), "has no cc_reopen_via_snk or no cc_self_heal")):
            with self.subTest(name):
                code, out = self.marker_gate(*args)
                self.assertEqual(code, 1, out)
                self.assertIn(message, out)
        # The release tree is checked for the hunk, and for none of the
        # driver's two switches.
        self.assertIn("grep -qF 'TYPEC_HOST : tcpm_data_role_for_sink(port)' \"$base_tree/$tcpm_dir/tcpm.c\" ||",
                      self.text)
        self.assertIn("! grep -q -e cc_reopen_via_snk -e cc_self_heal \"$base_tree/$tcpm_dir/sm5714_usbpd.c\" ||",
                      self.text)

    def test_revision_10_s_strings_are_the_driver_s_own(self):
        """cc_strings are the driver's (its two switches' names, its new
        format strings or parts of one, two members of its structure), in
        no patch the kit replays, in the pinned kernel's TCPM, or in
        revision 9's driver.  gone_strings are revision 9's: data_role_swapped
        its request-dfp patch's, the rest its driver's; none is in this
        checkout's driver or patches."""
        cc, gone = make_kit_array("cc_strings"), make_kit_array("gone_strings")
        self.assertEqual(len(cc + gone), len(set(cc + gone)))
        driver = (REPO / "kernel/drivers/sm5714_usbpd.c").read_text()
        struct = driver[driver.index("struct sm5714_usbpd {\n"):]
        struct = struct[:struct.index("\n};\n")]
        # A switch's name, a whole format string, a string the warning's %s
        # takes, or a member of the driver's structure.
        for s in cc:
            with self.subTest(s):
                if s in R10_SWITCHES:
                    self.assertIn(f"module_param({s}, bool, 0644);", driver)
                elif re.fullmatch(r"[a-z_]+", s):
                    self.assertRegex(struct, rf"\n\t(bool|unsigned int) {s};\n")
                elif "%" in s or s.startswith("CC on again"):
                    self.assertIn(f'"{s}\\n"', driver)
                else:
                    self.assertIn(f'"{s}"', driver)
        self.assertEqual(cc[:2], list(R10_SWITCHES))
        self.assertEqual(cc[-2:], ["heal_armed", "watch_suspect"])
        added = "\n".join(line for patch in (REPO / "kernel/patches").glob("*.patch")
                          for lines in added_lines(patch.name).values() for line in lines)
        for s in cc + gone:
            with self.subTest(patches=s):
                self.assertNotIn(s, added)
        for s in gone:
            with self.subTest(gone=s):
                self.assertNotIn(s, driver)
        r9_driver = repo_file_at(R9_COMMIT, "kernel/drivers/sm5714_usbpd.c")
        r9_dfp = repo_file_at(R9_COMMIT, "kernel/patches/tcpm-request-dfp-as-sink-before-discovery.patch")
        if r9_driver is not None:
            for s in cc:
                with self.subTest(r9=s):
                    self.assertNotIn(s, r9_driver)
            for s in gone[1:]:
                with self.subTest(r9_gone=s):
                    self.assertIn(s, r9_driver)
            self.assertIn("\tbool irq_saw_attach;\n", r9_driver)
            self.assertIn("\tbool watch_saw_attach;\n", r9_driver)
            self.assertIn("+\tbool data_role_swapped;\n", r9_dfp)
        kernel = kernel_source()
        if kernel is None:
            return
        upstream = "".join(subprocess.run(["git", "-C", str(kernel), "show", f"HEAD:{path}"], check=True,
                                          capture_output=True, text=True).stdout
                           for path in ("drivers/usb/typec/tcpm/tcpm.c", "include/linux/usb/tcpm.h",
                                        "include/linux/usb/pd.h"))
        for s in cc + gone:
            with self.subTest(upstream=s):
                self.assertNotIn(s, upstream)

    def image_gate(self, new, base):
        """Section 4's string checks on stand-in images."""
        block = re.search(r'^for s in "\$\{new_strings\[@\]\}" "\$\{deep_strings\[@\]\}".*?^echo "image: [^\n]*\n',
                          self.text, re.M | re.S).group(0)
        with tempfile.TemporaryDirectory() as tmp:
            pathlib.Path(tmp, "new.Image").write_bytes(new)
            pathlib.Path(tmp, "base.Image").write_bytes(base)
            setup = (f'set -euo pipefail; export LC_ALL=C\ndie() {{ echo "$*" >&2; exit 1; }}\ntmp={tmp}\n'
                     + make_kit_definitions("new_strings", "deep_strings", "gpu_strings", "swap_back_strings",
                                            "cc_strings", "gone_strings"))
            result = subprocess.run(["bash", "-c", setup + block], capture_output=True, text=True)
            return result.returncode, result.stdout + result.stderr

    def test_the_image_has_revision_10_s_strings_and_none_it_takes_out(self):
        earlier = (make_kit_array("new_strings") + make_kit_array("deep_strings") + make_kit_array("gpu_strings")
                   + make_kit_array("swap_back_strings"))
        cc, gone = make_kit_array("cc_strings"), make_kit_array("gone_strings")
        new = b"\0".join(s.encode() for s in earlier + cc)
        self.assertEqual(self.image_gate(new, b"release"),
                         (0, f"image: {len(earlier + cc)} strings of the change are in the new kernel and not in the "
                             f"release one, and none of the {len(gone)} that revision 10 takes out\n"))
        for name, args, message in (
                ("a string of revision 10 missing", (new.replace(cc[2].encode(), b""), b"release"),
                 f"the new kernel image does not contain '{cc[2]}'"),
                ("one in the release image", (new, cc[-1].encode()),
                 f"self-test failed: the release image contains '{cc[-1]}' too"),
                ("data_role_swapped still there", (new + b"\0data_role_swapped", b"release"),
                 "the new kernel image still contains 'data_role_swapped', which revision 10 takes out"),
                ("the old resync line still there", (new + b"\0" + gone[-1].encode(), b"release"),
                 f"the new kernel image still contains '{gone[-1]}'")):
            with self.subTest(name):
                code, out = self.image_gate(*args)
                self.assertEqual(code, 1, out)
                self.assertIn(message, out)
        # What revision 10 takes out may be in the release image: the
        # watchdog's old line and members are.
        self.assertEqual(self.image_gate(new, b"\0".join(s.encode() for s in gone[1:]))[0], 0)

    def switch_gate(self, new, base, prev=None, undescribed=(), described=None, base_described=(),
                    prev_described=()):
        """Section 6c on stand-in modules.builtin.modinfo files, each a list
        of NAME:TYPE (one parmtype and one parm line each, the driver's
        description for revision 10's two and "what NAME does" for the
        others; no parm line in NEW for the names in UNDESCRIBED; DESCRIBED
        replaces NEW's descriptions; BASE_DESCRIBED and PREV_DESCRIBED add the
        driver's description of those names to the release's and to
        revision 9's)."""
        block = make_kit_block("builtin_switches() {", 'echo "driver: $driver_check"')
        real = dict(d.split(":", 1) for d in make_kit_array("cc_switch_descriptions"))

        def modinfo(switches, undescribed=(), described=None, extra=()):
            lines = []
            for s in switches:
                name, kind = s.split(":")
                lines.append(f"sm5714_usbpd.parmtype={name}:{kind}")
                if name not in undescribed:
                    text = (described or {}).get(name, real.get(name, f"what {name} does"))
                    lines.append(f"sm5714_usbpd.parm={name}:{text}")
            lines += [f"sm5714_usbpd.parm={name}:{real[name]}" for name in extra]
            return "\n".join(lines + ["sm5714_usbpd.license=GPL"]) + "\n"

        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            (tmp / "builtin.modinfo").write_text(modinfo(new, undescribed, described))
            (tmp / "builtin.base.modinfo").write_text(modinfo(base, extra=base_described))
            setup = (f'set -euo pipefail; export LC_ALL=C\ndie() {{ echo "$*" >&2; exit 1; }}\n'
                     f'tmp={tmp} release={RELEASE}\n' + make_kit_definitions("switches", "cc_switch_descriptions"))
            if prev is not None:
                kit = tmp / "kit9"
                kit.mkdir()
                data = modinfo(prev, extra=prev_described).replace("\n", "\0").encode()
                with tarfile.open(kit / f"modules-{RELEASE}.tar", "w") as tar:
                    info = tarfile.TarInfo(f"usr/lib/modules/{RELEASE}/modules.builtin.modinfo")
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
                setup += f"PREV_KIT={kit} prev_revision=9\n"
            result = subprocess.run(["bash", "-c", setup + block], capture_output=True, text=True)
            return result.returncode, result.stdout + result.stderr

    def test_the_driver_s_twelve_switches_are_built_in_with_their_types(self):
        twelve = make_kit_array("switches")
        self.assertEqual(len(twelve), 12)
        self.assertEqual([s.split(":")[0] for s in twelve], list(SWITCHES))
        # The two descriptions are the driver's.
        driver = " ".join((REPO / "kernel/drivers/sm5714_usbpd.c").read_text().split())
        for d in make_kit_array("cc_switch_descriptions"):
            name, text = d.split(":", 1)
            with self.subTest(name):
                self.assertIn(f'MODULE_PARM_DESC({name}, "{text}");', driver)
        self.assertEqual([d.split(":")[0] for d in make_kit_array("cc_switch_descriptions")], list(R10_SWITCHES))
        said = ("driver: its twelve switches in modules.builtin.modinfo with their types, and revision 10's two "
                "with their descriptions, the release build's with the first two alone")
        self.assertEqual(self.switch_gate(twelve, twelve[:2]), (0, said + "\n"))
        self.assertEqual(self.switch_gate(twelve, twelve[:2], prev=twelve[:10]),
                         (0, said + ", revision 9's with the first ten\n"))
        for name, args, kwargs, message in (
                ("one missing", (twelve[:11], twelve[:2]), {}, "has no sm5714_usbpd switch cc_self_heal of type bool"),
                ("another type", ([s.replace(":bool", ":int") if s.startswith("cc_reopen") else s for s in twelve],
                                  twelve[:2]), {}, "has no sm5714_usbpd switch cc_reopen_via_snk of type bool"),
                ("a thirteenth", (twelve + ["extra_switch:bool"], twelve[:2]), {},
                 "the driver has other switches than these twelve"),
                ("a release driver with four", (twelve, twelve[:4]), {},
                 "self-test failed: the release driver's switches are not"),
                ("revision 9's kit with the new driver", (twelve, twelve[:2], twelve), {},
                 "revision 9's driver does not have exactly the switches"),
                ("revision 9's kit with revision 8's driver", (twelve, twelve[:2], twelve[:4]), {},
                 "revision 9's driver does not have exactly the switches"),
                ("another description", (twelve, twelve[:2]),
                 {"described": {"cc_reopen_via_snk": "turn CC on again (default on)"}},
                 "does not describe cc_reopen_via_snk as the driver does: 'leave a CC open of the driver's through "
                 "Unattached.SNK before toggling (default on)'"),
                ("the release describes it", (twelve, twelve[:2]), {"base_described": ("cc_self_heal",)},
                 "self-test failed: the release build's modules.builtin.modinfo describes cc_self_heal too"),
                ("revision 9's describes it", (twelve, twelve[:2], twelve[:10]),
                 {"prev_described": ("cc_reopen_via_snk",)},
                 "revision 9's modules.builtin.modinfo describes cc_reopen_via_snk already"),
                ("a switch without its description", (twelve, twelve[:2]), {"undescribed": ("sink_tx_ok_rp",)},
                 "has no sm5714_usbpd switch sink_tx_ok_rp of type bool, or no description of it")):
            with self.subTest(name):
                code, out = self.switch_gate(*args, **kwargs)
                self.assertEqual(code, 1, out)
                self.assertIn(message, out)

    def prev_gate(self, image=None, kernel=None, boot=None, revision="9", sums_ok=True, image_id=None,
                  new_id="1" * 40, prev=True):
        """Section 5a on a stand-in revision 9 kit: its boot.img's kernel
        (the stand-in unpack gives KERNEL, by default the gzipped IMAGE and
        the release DTB), REVISION and SHA256SUMS."""
        block = make_kit_block('prev_check="not compared (PREV_KIT not set)"', "fi")
        build_id = re.search(r"^build_id\(\) \{.*?^\}\n", self.text, re.M | re.S).group(0)
        prev_id = "9" * 40
        strings = (make_kit_array("new_strings") + make_kit_array("deep_strings") + make_kit_array("gpu_strings")
                   + make_kit_array("swap_back_strings") + make_kit_array("gone_strings"))
        if image is None:
            image = b"\0".join(s.encode() for s in strings)
        note = bytes.fromhex("040000001400000003000000") + b"GNU\0" + bytes.fromhex(image_id or prev_id)
        image = b"head\0" + image + b"\0" + note + b"tail"
        dtb = b"\xd0\x0d\xfe\xed the release DTB"
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            (tmp / "base").mkdir()
            (tmp / "base/sm8550-samsung-gts9uwifi.dtb").write_bytes(dtb)
            (tmp / "work").mkdir()
            kit = tmp / "kit9"
            kit.mkdir()
            boot_img = boot or os.urandom(4096)
            (kit / "boot.img").write_bytes(boot_img)
            (kit / "REVISION").write_text(revision + "\n")
            (kit / f"modules-{RELEASE}.tar").write_bytes(b"")
            sums = "".join(f"{sha((kit / n).read_bytes())}  {n}\n" for n in ("boot.img", "REVISION"))
            (kit / "SHA256SUMS").write_text(sums if sums_ok else sums.replace(sums[:8], "00000000"))
            (tmp / "kernel").write_bytes(kernel if kernel is not None else gzip.compress(image) + dtb)
            setup = (f'set -euo pipefail; export LC_ALL=C\ndie() {{ echo "$*" >&2; exit 1; }}\n'
                     'sha() { sha256sum "$1" | cut -d\' \' -f1; }\n'
                     f'unpack() {{ mkdir -p "$2"; cp {tmp}/kernel "$2/kernel"; }}\n'
                     f'tmp={tmp}/work base={tmp}/base dts=sm8550-samsung-gts9uwifi release={RELEASE} '
                     f'new_id={new_id}\nprev_revision=9 prev_boot={sha(boot_img) if boot is None else sha(b"x")} '
                     f'prev_build_id={prev_id}\n'
                     + (f"PREV_KIT={kit}\n" if prev else "")
                     + make_kit_definitions("new_strings", "deep_strings", "gpu_strings", "swap_back_strings",
                                            "cc_strings", "gone_strings")
                     + build_id)
            result = subprocess.run(["bash", "-c", setup + block + 'echo "check: $prev_check"\n'],
                                    capture_output=True, text=True)
            return result.returncode, result.stdout + result.stderr

    def test_revision_9_s_kernel_has_what_revision_10_takes_out_and_none_of_what_it_adds(self):
        earlier = (make_kit_array("new_strings") + make_kit_array("deep_strings") + make_kit_array("gpu_strings")
                   + make_kit_array("swap_back_strings"))
        gone, cc = make_kit_array("gone_strings"), make_kit_array("cc_strings")
        said = (f"revision 9's boot.img ({{}}) holds an image with build ID 999999999999 and the release DTB, "
                f"with all {len(earlier)} strings of revisions 1 to 9 and the {len(gone)} that revision 10 takes "
                f"out, and none of the {len(cc)} of revision 10")
        code, out = self.prev_gate()
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"^revision 9: " + re.escape(said).replace(r"\{\}", "[0-9a-f]{12}") + "\ncheck: ")
        self.assertEqual(self.prev_gate(prev=False), (0, "check: not compared (PREV_KIT not set)\n"))
        full = b"\0".join(s.encode() for s in earlier + gone)
        for name, kwargs, message in (
                ("an image with a string of revision 10", {"image": full + b"\0" + cc[2].encode()},
                 f"revision 9's image contains '{cc[2]}' already: it is not a string of revision 10 alone"),
                ("an image without a string of revision 9",
                 {"image": b"\0".join(s.encode() for s in earlier[:-1] + gone)},
                 f"self-test failed: revision 9's image does not contain '{earlier[-1]}'"),
                ("an image without data_role_swapped",
                 {"image": b"\0".join(s.encode() for s in earlier + gone[1:])},
                 "self-test failed: revision 9's image does not contain 'data_role_swapped'"),
                ("another boot.img", {"boot": os.urandom(4096)}, "boot.img is not revision 9's"),
                ("another DTB after the kernel", {"kernel": gzip.compress(full) + b"\xd0\x0d\xfe\xed another DTB"},
                 "self-test failed: revision 9's boot kernel does not end with the release DTB"),
                ("another build", {"image_id": "8" * 40},
                 "self-test failed: the build ID of revision 9's image is not 999999999999"),
                ("revision 8's kit", {"revision": "8"}, "is not revision 9's kit"),
                ("a kit that does not match its checksums", {"sums_ok": False}, "does not match its SHA256SUMS"),
                ("the new image is revision 9's", {"new_id": "9" * 40}, "the new image is revision 9's")):
            with self.subTest(name):
                code, out = self.prev_gate(**kwargs)
                self.assertEqual(code, 1, out)
                self.assertIn(message, out)

    def test_prev_kit_is_revision_9_s(self):
        """PREV_KIT's facts are revision 9's kit's: its boot.img hash and
        build ID as its report and its kit give them (and verify.sh's
        build ID check would know it by the same)."""
        self.assertEqual(constant("make-kit.sh", "prev_revision"), "9")
        self.assertEqual(pinned("make-kit.sh", "prev_boot"),
                         "a62145c2cbb49a5d5ccbd72a60a587cdcbb3deb35fd1914f2b8ce6be46ad552c")
        self.assertEqual(pinned("make-kit.sh", "prev_build_id"), "ba183276ef1888ac2813ba5429a107144ccef10d")
        self.assertIn("# Revision 9 of this kit (2026-11-26), what the tablet runs before this one", self.text)

    # struct tcpm_port as pahole shows it in revision 10's tcpm.o (abridged),
    # and in revision 9's, with data_role_swapped between the two members
    # around it (both read from the real objects, 2026-11-27).
    TCPM_PAHOLE = ("struct tcpm_port {\n\tstruct device *             dev;                  /*     0     8 */\n"
                   "\tunsigned int               sink_dfp_tries;       /*  1048     4 */\n"
                   "\tbool                       sink_dfp_done;        /*  1052     1 */\n\n"
                   "\t/* XXX 3 bytes hole, try to pack */\n\n"
                   "\tktime_t                    sink_dfp_not_before;  /*  1056     8 */\n"
                   "\tbool                       send_discover_before_swap; /*  1064     1 */\n\n"
                   "\t/* XXX 3 bytes hole, try to pack */\n\n"
                   "\tunsigned int               rx_msgid;             /*  1068     4 */\n"
                   "\t/* size: 15592, cachelines: 244, members: 151 */\n};\n")
    TCPM_PAHOLE_R9 = TCPM_PAHOLE.replace(
        "\tbool                       send_discover_before_swap; /*  1064     1 */\n\n"
        "\t/* XXX 3 bytes hole, try to pack */\n\n",
        "\tbool                       data_role_swapped;    /*  1064     1 */\n"
        "\tbool                       send_discover_before_swap; /*  1065     1 */\n\n"
        "\t/* XXX 2 bytes hole, try to pack */\n\n").replace("members: 151", "members: 152")
    CC_NM = "".join(f"ffff8000809b{n:04x} t {f}\n" for n, f in enumerate(
        ("sm5714_usbpd_cc_watch_work", "sm5714_usbpd_start_toggling", "sm5714_usbpd_set_cc", "sm5714_usbpd_init",
         "sm5714_usbpd_open_cc", "sm5714_usbpd_restart_cc", "sm5714_usbpd_init_regs")))
    # What each of the driver's functions calls in revision 10's vmlinux, as
    # llvm-objdump shows it (abridged to the calls), and in revision 9's.
    CALLS = {"sm5714_usbpd_cc_watch_work": ("mutex_lock", "regmap_read", "sm5714_usbpd_restart_cc", "_dev_info",
                                            "_dev_warn", "tcpm_cc_change", "queue_delayed_work_on"),
             "sm5714_usbpd_start_toggling": ("mutex_lock", "regmap_write", "usleep_range_state", "_dev_info",
                                             "sm5714_usbpd_set_cc"),
             "sm5714_usbpd_set_cc": ("msleep", "regmap_read", "sm5714_usbpd_open_cc", "_dev_info"),
             "sm5714_usbpd_init": ("mutex_lock", "sm5714_usbpd_init_regs", "mutex_unlock")}
    CALLS_R9 = {"sm5714_usbpd_cc_watch_work": ("regmap_read", "_dev_info", "tcpm_cc_change", "queue_delayed_work_on"),
                "sm5714_usbpd_start_toggling": ("mutex_lock", "regmap_write", "sm5714_usbpd_set_cc"),
                "sm5714_usbpd_set_cc": ("msleep", "regmap_read", "_dev_info"),
                "sm5714_usbpd_init": ("mutex_lock", "regmap_read", "regmap_write", "regmap_bulk_read")}

    @staticmethod
    def disassembly(function, calls):
        lines = [f"ffff8000809b0000 <{function}>:"]
        for n, callee in enumerate(calls):
            lines.append(f"ffff8000809b{n * 4 + 4:04x}:      \tbl\t0xffff8000801f{n:04x} <{callee}>")
        # A tail branch to itself and an unnamed one, which are no calls of
        # the function.
        lines.append(f"ffff8000809b0100:      \tb\t0xffff8000809b0010 <{function}+0x10>")
        lines.append("ffff8000809b0104:      \tret")
        return "\nvmlinux:\tfile format elf64-littleaarch64\n\n" + "\n".join(lines) + "\n"

    def tcpm_gate(self, layout=None, nm=None, base_nm=None, calls=None):
        """Section 5e on stand-ins: tcpm.o's pahole layout of tcpm_port,
        vmlinux's symbols and disassembly and, when given, the release
        build's symbols."""
        block = make_kit_block("member_end() {", 'echo "driver: $cc_check"')
        member = re.search(r"^member_offset\(\) \{.*?^\}\n", self.text, re.M | re.S).group(0)
        nm_has = re.search(r"^nm_has\(\) \{.*\}.*\n", self.text, re.M).group(0)
        branches = re.search(r"^branches_of\(\) \{.*?^\}\n", self.text, re.M | re.S).group(0)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            bin_ = tmp / "bin"
            bin_.mkdir()
            (bin_ / "pahole").write_text('#!/bin/sh\ncat "$3.$2" 2>/dev/null\n')
            (bin_ / "llvm-objdump").write_text('#!/bin/sh\nfor a; do case $a in --disassemble-symbols=*) '
                                               'f=${a#*=} ;; esac; last=$a; done\n'
                                               # As llvm-objdump does, a symbol it does not find
                                               # is a warning and no failure.
                                               'cat "$last.$f.dis" 2>/dev/null || :\n')
            for stub in bin_.iterdir():
                stub.chmod(0o755)
            obj = tmp / "build/drivers/usb/typec/tcpm/tcpm.o"
            obj.parent.mkdir(parents=True)
            obj.write_text("object")
            (obj.parent / "tcpm.o.tcpm_port").write_text(self.TCPM_PAHOLE if layout is None else layout)
            (tmp / "vmlinux.nm").write_text(self.CC_NM if nm is None else nm)
            (tmp / "build/vmlinux").write_text("vmlinux")
            for function, callees in (self.CALLS if calls is None else calls).items():
                (tmp / f"build/vmlinux.{function}.dis").write_text(self.disassembly(function, callees))
            setup = (f'set -euo pipefail; export LC_ALL=C\ndie() {{ echo "$*" >&2; exit 1; }}\n'
                     f'build={tmp}/build tmp={tmp} new_id={"1" * 40}\n'
                     + make_kit_definitions("tcpm_dir") + "patches=(" + " ".join(make_kit_array("patches")) + ")\n"
                     + member + nm_has + branches)
            if base_nm is not None:
                (tmp / "vmlinux.base.nm").write_text(base_nm)
                setup += f"BASE_SCRATCH={tmp}/base\n"
            env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}")
            result = subprocess.run(["bash", "-c", setup + block], capture_output=True, text=True, env=env)
            return result.returncode, (result.stdout + result.stderr).replace(str(tmp), "TMP")

    def test_tcpm_port_has_no_data_role_swapped_and_the_driver_s_new_code_is_linked(self):
        functions = make_kit_array("cc_functions")
        self.assertEqual(functions, ["sm5714_usbpd_open_cc", "sm5714_usbpd_restart_cc", "sm5714_usbpd_init_regs"])
        pairs = [c.split(":") for c in make_kit_array("cc_calls")]
        # Each caller and callee is the driver's or the kernel's, and the
        # summary names every pair.
        driver = (REPO / "kernel/drivers/sm5714_usbpd.c").read_text()
        for caller, callee in pairs:
            with self.subTest(caller=caller, callee=callee):
                self.assertIn(f"{caller}(", driver)
                self.assertIn(callee, self.CALLS[caller])
                self.assertNotIn(callee, self.CALLS_R9[caller])
        said_calls = ("its attach watchdog calls sm5714_usbpd_restart_cc() and _dev_warn(), start_toggling() "
                      "usleep_range_state() and _dev_info(), set_cc() sm5714_usbpd_open_cc() and init() "
                      "sm5714_usbpd_init_regs()")
        for caller, callee in pairs:
            self.assertIn(f"{callee}()", said_calls)
        said = ("TCPM: struct tcpm_port in tcpm.o (pahole) has no data_role_swapped: send_discover_before_swap is "
                "at 1064, the end of sink_dfp_not_before\ndriver: vmlinux (build ID 111111111111) has "
                f"{', '.join(f + '()' for f in functions)}; {said_calls}\n")
        self.assertEqual(self.tcpm_gate(), (0, said))
        release_nm = "ffff8000809b0000 t sm5714_usbpd_cc_watch_work\n"
        self.assertEqual(self.tcpm_gate(base_nm=release_nm),
                         (0, said[:-1] + "; the release build's has none of them\n"))
        for name, kwargs, message in (
                ("revision 9's layout", {"layout": self.TCPM_PAHOLE_R9},
                 "struct tcpm_port in TMP/build/drivers/usb/typec/tcpm/tcpm.o has data_role_swapped, or a member "
                 "after sink_dfp_not_before: tcpm-request-dfp-as-sink-before-discovery.patch is revision 9's"),
                ("a member renamed", {"layout": self.TCPM_PAHOLE_R9.replace("data_role_swapped", "role_swapped")},
                 "has data_role_swapped, or a member after sink_dfp_not_before"),
                ("no layout", {"layout": ""},
                 "pahole gives no offsets of struct tcpm_port's sink_dfp_not_before and send_discover_before_swap"),
                ("a function inlined away", {"nm": self.CC_NM.replace(f" t {functions[-1]}\n", " t other\n")},
                 f"no {functions[-1]} in TMP/build/vmlinux"),
                ("revision 9's watchdog", {"calls": {**self.CALLS, "sm5714_usbpd_cc_watch_work":
                                                     self.CALLS_R9["sm5714_usbpd_cc_watch_work"]}},
                 "sm5714_usbpd_cc_watch_work() in TMP/build/vmlinux does not call sm5714_usbpd_restart_cc(): the "
                 "build's sm5714_usbpd.c is not revision 10's"),
                ("revision 9's start_toggling()", {"calls": {**self.CALLS, "sm5714_usbpd_start_toggling":
                                                             self.CALLS_R9["sm5714_usbpd_start_toggling"]}},
                 "sm5714_usbpd_start_toggling() in TMP/build/vmlinux does not call usleep_range_state()"),
                ("a watchdog that does not warn", {"calls": {**self.CALLS, "sm5714_usbpd_cc_watch_work": tuple(
                    c for c in self.CALLS["sm5714_usbpd_cc_watch_work"] if c != "_dev_warn")}},
                 "sm5714_usbpd_cc_watch_work() in TMP/build/vmlinux does not call _dev_warn()"),
                ("set_cc() not found", {"calls": {k: v for k, v in self.CALLS.items() if k != "sm5714_usbpd_set_cc"}},
                 "sm5714_usbpd_set_cc() in TMP/build/vmlinux does not call sm5714_usbpd_open_cc()"),
                ("a release with one of them", {"base_nm": release_nm + f"1 t {functions[0]}\n"},
                 f"self-test failed: the release vmlinux has {functions[0]}() too")):
            with self.subTest(name):
                code, out = self.tcpm_gate(**kwargs)
                self.assertEqual(code, 1, out)
                self.assertIn(message, out)

    def test_the_readme_says_what_revision_10_is_and_that_none_of_it_has_run(self):
        text = (KIT_SRC / "README.md").read_text()
        readme = " ".join(text.split())
        for said in ("**Revision 10 (2026-11-27): a sink is UFP after every Hard Reset, and a CC open no longer "
                     "leaves the port dead. Built and checked on the host; nothing that it adds has run on a "
                     "tablet.**",
                     "**That the chip's CC logic stays off after the toggling's write alone is read from Samsung's "
                     "code, not measured, and none of revision 10's driver code has run on a tablet.**",
                     "(`mv ~/typec-roles ~/typec-roles-r9`)",
                     "**Not on revision 9: a run with `swap_protocol_reset` at `N`.**",
                     "On revision 9 leave out the first run's repeat with `swap_protocol_reset` at `N`",
                     "`REVISION` the revision (10; revisions 1 to 7 had none)",
                     "PREV_KIT=$HOME/ubuntu-gts9u/backups/typec-roles-ultra-kit-r9",
                     "`a62145c2cbb49a5d5ccbd72a60a587cdcbb3deb35fd1914f2b8ce6be46ad552c`",
                     "twenty-three files and two overlay blobs may differ"):
            with self.subTest(said):
                self.assertIn(said, readme)
        for name in ("tcpm-use-retained-sink-data-role.patch", "tcpm-request-dfp-as-sink-before-discovery.patch"):
            self.assertIn(f"`{name}`", readme)
        for line in ("CC opened for TCPM's error recovery", "CC on again through Unattached.SNK",
                     "CC finds no partner", "shows a partner TCPM was not told of"):
            self.assertIn(f"`{line}`", readme)
        for name in R10_SWITCHES:
            self.assertIn(f"| `{name}` | `{SWITCHES[name]}` |", readme)
        self.assertNotIn("PREV_KIT=$HOME/ubuntu-gts9u/backups/typec-roles-ultra-kit-r8", readme)
        # A command run with sudo over ssh: -t, and its output through tee,
        # never redirected on the build host (the password prompt would go
        # into the file).  No command reads the TCPM log twice.
        commands = [line.strip() for line in text.splitlines() if line.strip().startswith("ssh ")]
        commands += re.findall(r"`(ssh [^`]*)`", text)
        self.assertTrue([c for c in commands if "sudo" in c])
        for command in commands:
            with self.subTest(command):
                # The README's placeholders (<user>, <tablet>) are no
                # redirections.
                quoted = re.sub(r"'[^']*'", "''", re.sub(r"<[a-z]+>", "X", command))
                self.assertNotIn(">", quoted)
                if "sudo" in command:
                    self.assertTrue(command.startswith("ssh -t "), command)
                    if "reboot" not in command:
                        self.assertRegex(quoted, r"^ssh -t \S+ '' \| tee \S+$")
        self.assertFalse([line for line in text.splitlines() if line.count("tcpm-8-0033/log") > 1])


class Revision10UpgradeTest(FakeTablet):
    """Revision 10 over a tablet that has revision 9, revision 8 or the
    release, each with one install.sh and no rollback first, and each way
    back: revision 9's and revision 8's own install.sh (from their kits'
    commits), rollback.sh to the release, and from TWRP the release's or
    revision 9's boot image with rollback.sh or revision 9's install.sh."""

    def setUp(self):
        super().setUp()
        scripts = {}
        for name, commit in (("r8", R8_COMMIT), ("r9", R9_COMMIT)):
            scripts[name] = {s: repo_file_at(commit, f"tools/inplace/typec-roles-ultra/{s}")
                             for s in ("install.sh", "rollback.sh", "verify.sh", "README.md")}
            if None in scripts[name].values():
                self.skipTest("no git history: revision 8's and 9's scripts come from their commits")
        scripts["r10"] = {s: (KIT_SRC / s).read_text() for s in ("install.sh", "rollback.sh", "verify.sh", "README.md")}
        self.kits, self.boots = {}, {}
        for revision, build_id in (("r8", "8" * 40), ("r9", "9" * 40), ("r10", NEW_BUILD_ID)):
            kit = self.tmp / f"kit-{revision}"
            kit.mkdir()
            self.boots[revision] = os.urandom(SIZE)
            (kit / "boot.img").write_bytes(self.boots[revision])
            (kit / f"modules-{RELEASE}.tar").write_bytes(modules_tar(revision))
            (kit / "BUILD_ID").write_text(build_id + "\n")
            (kit / "REVISION").write_text(revision[1:] + "\n")
            for name in ("KEY_ID", "SIGNER", "SOURCE"):
                (kit / name).write_bytes((self.kit / name).read_bytes())
            for name, body in scripts[revision].items():
                (kit / name).write_text(body)
            self.kits[revision] = kit
            self.kit = kit
            self.seal()
        self.backup = self.root / BACKUP

    def install(self, revision, script="install.sh"):
        self.kit = self.kits[revision]
        result = self.run_script(script)
        self.assertEqual(result.returncode, 0, f"{revision} {script}: " + result.stdout + result.stderr)
        return result

    def assert_on(self, revision):
        """boot holds REVISION's image (or the release's), the module tree
        is its, the release is still what the backup holds, vendor_boot is
        the release's."""
        self.assertEqual(self.part("vendor_boot").read_bytes(), self.base_vendor_boot)
        if revision == "release":
            self.assertEqual(self.part("boot").read_bytes(), self.base_boot)
            self.assertEqual(self.tree(), {n: f"release {n}\n" for n in MODULES})
            self.assertFalse(self.backup.exists())
            return
        self.assertEqual(self.part("boot").read_bytes(), self.boots[revision])
        self.assertEqual(self.tree(), {n: f"{revision} {n}\n" for n in MODULES})
        self.assertEqual((self.backup / "boot-release.img").read_bytes(), self.base_boot)
        self.assertEqual({str(p.relative_to(self.backup / f"modules-{RELEASE}")): p.read_text()
                          for p in (self.backup / f"modules-{RELEASE}").rglob("*") if p.is_file()},
                         {n: f"release {n}\n" for n in MODULES})
        self.assertEqual((self.backup / "installed-boot").read_text(), sha(self.boots[revision]) + "\n")
        self.assertFalse((self.backup / "upgrading").exists())
        self.assertEqual([p.name for p in (self.root / "usr/lib/modules").iterdir()], [RELEASE])

    def boot_into(self, revision):
        self.running({"r8": "8" * 40, "r9": "9" * 40, "r10": NEW_BUILD_ID, "release": BASE_BUILD_ID}[revision])

    def test_the_earlier_revisions_pin_the_same_images(self):
        for revision in ("r8", "r9"):
            for name in ("base_boot", "base_vendor_boot", "base_build_id", "kit_r3_boot"):
                with self.subTest(revision=revision, pin=name):
                    self.assertEqual(re.search(rf"^{name}=([0-9a-f]+)$", (self.kits[revision] / "install.sh").read_text(),
                                               re.M).group(1), pinned("install.sh", name))

    def test_revision_10_over_the_release_and_back(self):
        result = self.install("r10")
        self.assertIn("boot saved to", result.stdout)
        self.assert_on("r10")
        self.boot_into("r10")
        self.install("r10", "rollback.sh")
        self.assert_on("release")

    def test_revision_10_over_revision_9_and_back_to_9(self):
        self.install("r9")
        self.boot_into("r9")
        result = self.install("r10")
        self.assertIn(f"upgrade: boot holds {sha(self.boots['r9'])[:12]}; the release saved in", result.stdout)
        self.assertNotIn("STOP", result.stdout)
        self.assert_on("r10")
        self.boot_into("r10")
        # The way back to revision 9: its own install.sh, kept as
        # ~/typec-roles-r9.
        result = self.install("r9")
        self.assertIn(f"upgrade: boot holds {sha(self.boots['r10'])[:12]}", result.stdout)
        self.assert_on("r9")
        self.boot_into("r9")
        self.install("r10")
        self.assert_on("r10")
        # The same revision twice is refused, nothing changed.
        self.kit = self.kits["r10"]
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("this revision of the Type-C roles kit is already installed", result.stdout)
        self.assert_on("r10")
        # Revision 9's rollback.sh is revision 10's: either takes it back to
        # the release.
        self.install("r9", "rollback.sh")
        self.assert_on("release")

    def test_revision_10_over_revision_8_and_back_to_8(self):
        self.install("r8")
        self.boot_into("r8")
        result = self.install("r10")
        self.assertIn(f"upgrade: boot holds {sha(self.boots['r8'])[:12]}; the release saved in", result.stdout)
        self.assert_on("r10")
        self.boot_into("r10")
        result = self.install("r8")
        self.assertIn(f"upgrade: boot holds {sha(self.boots['r10'])[:12]}", result.stdout)
        self.assert_on("r8")
        self.boot_into("r8")
        self.install("r10")
        self.assert_on("r10")
        self.install("r10", "rollback.sh")
        self.assert_on("release")

    def test_revision_10_over_revision_9_over_revision_8(self):
        """The tablet's own history: revision 8, then 9, then 10, each over
        the one before."""
        for revision in ("r8", "r9", "r10"):
            self.install(revision)
            self.assert_on(revision)
            self.boot_into(revision)
        self.install("r10", "rollback.sh")
        self.assert_on("release")
        self.assertTrue(self.stopgap_enabled())

    def test_the_ways_back_from_twrp(self):
        """Revision 10 does not come up.  From TWRP the saved release image
        goes back on boot, and rollback.sh on the next boot (release kernel,
        revision 10's modules) finishes it; or revision 9's boot image goes
        back, and revision 9's install.sh takes it as an upgrade."""
        for way in ("release", "r9"):
            with self.subTest(way):
                FakeTablet.setUp(self)
                Revision10UpgradeTest.setUp(self)
                self.install("r9")
                self.boot_into("r9")
                self.install("r10")
                # TWRP's dd.
                if way == "release":
                    self.part("boot").write_bytes((self.backup / "boot-release.img").read_bytes())
                    self.boot_into("release")
                    self.install("r10", "rollback.sh")
                    self.assert_on("release")
                else:
                    self.part("boot").write_bytes((self.kits["r9"] / "boot.img").read_bytes())
                    self.boot_into("r9")
                    result = self.install("r9")
                    self.assertIn(f"upgrade: boot holds {sha(self.boots['r9'])[:12]}", result.stdout)
                    self.assert_on("r9")

    def test_install_and_rollback_are_revision_9_s_but_for_comments(self):
        """install.sh differs from revision 9's (and 8's) in its header
        comment alone, rollback.sh not at all: what revision 9 and 8 were
        rehearsed with is what revision 10 runs."""
        for revision in ("r8", "r9"):
            with self.subTest(revision):
                old = (self.kits[revision] / "install.sh").read_text()
                self.assertEqual(code_lines(old), code_lines((KIT_SRC / "install.sh").read_text()))
                self.assertEqual((self.kits[revision] / "rollback.sh").read_text(),
                                 (KIT_SRC / "rollback.sh").read_text())
        self.assertIn('since revision 10 a sink that is UFP\n# after every Hard Reset',
                      (KIT_SRC / "install.sh").read_text())

def dts_node(text, head):
    """The DTS node that starts with the line HEAD (its indent included), up
    to its closing line."""
    start = text.index(head)
    indent = head[:len(head) - len(head.lstrip("\t"))]
    return text[start:text.index(f"\n{indent}}};\n", start)]


def dts_props(block):
    """The properties set directly in a DTS block (not in its child nodes):
    name -> value as written, a value of None for a boolean one."""
    found, depth = {}, 0
    for line in block.splitlines()[1:]:
        s = line.strip()
        if s.endswith("{"):
            depth += 1
            continue
        if s.startswith("}"):
            depth -= 1
            continue
        if depth or not s.endswith(";") or s.startswith(("/*", "*")):
            continue
        name, _, value = s[:-1].partition(" = ")
        found[name] = value or None
    return found


class DeepSleepGroundworkTest(unittest.TestCase):
    """The SM-X910's share of what lets the SM-X710 sleep deeply (2026-11-25):
    the X710's configuration lines in the Ultra's fragment; its three board
    DTS changes for the SoC's sync_state hand-off and its ramoops node in two
    Ultra-only early overlays, since the Ultra's DTB is pinned; the early
    helper applying them on the Ultra alone, each on its own; and the UFS and
    USB CX-vote patches in every build."""

    FRAGMENTS = {b: REPO / f"kernel/config/config-{b}.fragment" for b in ("gts9wifi", "gts9uwifi")}
    DTS = {b: REPO / f"kernel/dts/sm8550-samsung-{b}.dts" for b in ("gts9wifi", "gts9uwifi")}
    SYNC = REPO / "kernel/dts/gts9u-sync-state.dtso"
    RAMOOPS = REPO / "kernel/dts/gts9u-ramoops.dtso"
    HELPER = REPO / "kernel/drivers/gts9u-performance.c"
    STAGE = REPO / "scripts/stage-performance-overlay.sh"

    @staticmethod
    def fragments(dtso):
        """{target-path: {property: value}} of an overlay source."""
        text = dtso.read_text()
        found = {}
        for match in re.finditer(r"^\tfragment@\d+ \{\n", text, re.M):
            block = dts_node(text, match.group(0))
            target = re.search(r'^\t\ttarget-path = "([^"]+)";$', block, re.M).group(1)
            # dts_node() leaves out the closing line, which the inner one needs.
            overlay = dts_node(block + "\n", "\t\t__overlay__ {\n")
            found[target] = dts_props(overlay)
        return found

    def test_the_ultra_fragment_has_the_x710_s_sleep_lines(self):
        """The video clock controller built in (rpmhpd and gcc sync), the
        sleep statistics built in, and the X710's two pstore lines for its
        ramoops region: each once in both fragments.  The bandwidth monitors
        are the one deviation: the X710 ships the module (and its rootfs
        blacklists it), the Ultra ships none; both leave the two PMUs without
        a driver, the interconnect at its boot vote."""
        for line in ("CONFIG_SM_VIDEOCC_8550=y", "CONFIG_QCOM_STATS=y", "CONFIG_PSTORE_FTRACE=y",
                     "# CONFIG_PSTORE_COMPRESS is not set"):
            for board, path in self.FRAGMENTS.items():
                with self.subTest(line=line, board=board):
                    self.assertEqual(path.read_text().splitlines().count(line), 1)
        ultra = self.FRAGMENTS["gts9uwifi"].read_text()
        self.assertNotIn("CONFIG_QCOM_ICC_BWMON", "\n".join(
            l for l in ultra.splitlines() if not l.startswith("#")))
        self.assertIn("CONFIG_QCOM_ICC_BWMON=m", self.FRAGMENTS["gts9wifi"].read_text().splitlines())
        # The base configuration has bwmon =m: what the payload check keys on
        # is the fragments, so no module comes of it on the Ultra.
        self.assertIn("CONFIG_QCOM_ICC_BWMON=m",
                      (REPO / "kernel/config/config-mainline.aarch64").read_text().splitlines())
        build = (REPO / "scripts/build-mainline-kernel.sh").read_text()
        self.assertRegex(build, r'\n\t+case " \$\{requested_m\[\*\]\} " in\n\t+\*" \$symbol "\*\) ;;\n'
                                r'\t+\*\) continue ;;\n')

    def test_the_sync_state_overlay_is_the_x710_dts_s_three_changes(self):
        """gcc under CX, the GMU's post-init providers and the crypto engine
        disabled: the X710 DTS's &gcc, &gmu and &crypto, value for value
        (RPMHPD_CX is 0), at the paths those labels name in sm8550.dtsi."""
        found = self.fragments(self.SYNC)
        self.assertEqual(found, {
            "/soc@0/clock-controller@100000": {"power-domains": "<&rpmhpd 0>"},
            "/soc@0/gmu@3d6a000": {"post-init-providers": "<&gcc>, <&gpucc>"},
            "/soc@0/crypto@1dfa000": {"status": '"disabled"'},
        })
        x710 = self.DTS["gts9wifi"].read_text()
        self.assertEqual(dts_props(dts_node(x710, "&gcc {\n")), {"power-domains": "<&rpmhpd RPMHPD_CX>"})
        self.assertEqual(dts_props(dts_node(x710, "&gmu {\n")), {"post-init-providers": "<&gcc>, <&gpucc>"})
        self.assertEqual(dts_props(dts_node(x710, "&crypto {\n")), {"status": '"disabled"'})
        # Not in the Ultra's working-copy DTS: the overlay is its one source.
        ultra = self.DTS["gts9uwifi"].read_text()
        for label in ("&gcc {", "&gmu {", "&crypto {"):
            self.assertNotIn(f"\n{label}\n", ultra)
        kernel = kernel_source()
        if kernel is None:
            self.skipTest("no pinned kernel checkout")
        show = lambda path: subprocess.run(["git", "-C", str(kernel), "show", f"HEAD:{path}"],
                                           check=True, capture_output=True, text=True).stdout
        self.assertRegex(show("include/dt-bindings/power/qcom,rpmhpd.h"), r"\n#define RPMHPD_CX\t+0\n")
        dtsi = show("arch/arm64/boot/dts/qcom/sm8550.dtsi")
        soc = dts_node(dtsi, "\tsoc: soc@0 {\n")
        for label, node in (("gcc", "clock-controller@100000"), ("gpucc", "clock-controller@3d90000"),
                            ("gmu", "gmu@3d6a000"), ("crypto", "crypto@1dfa000")):
            with self.subTest(label):
                self.assertEqual(dtsi.count(f"\t{label}: {node} {{\n"), 1)
                self.assertIn(f"\n\t\t{label}: {node} {{\n", soc)
        self.assertEqual(dtsi.count("\trpmhpd: power-controller {\n"), 1)
        self.assertIn("\t\t\trpmhpd: power-controller {\n", dts_node(dtsi, "\t\tapps_rsc: rsc@17a00000 {\n"))
        # Upstream gives crypto no status (enabled), gcc no power-domains,
        # and the GMU no post-init-providers: the overlay adds each.
        self.assertNotIn("status", dts_props(dts_node(dtsi, "\t\tcrypto: crypto@1dfa000 {\n")))
        self.assertNotIn("power-domains", dts_props(dts_node(dtsi, "\t\tgcc: clock-controller@100000 {\n")))
        self.assertNotIn("post-init-providers", dts_props(dts_node(dtsi, "\t\tgmu: gmu@3d6a000 {\n")))

    def test_the_ramoops_overlay_is_the_x710_dts_s_node_on_the_same_carve_out(self):
        """The X710 DTS's ramoops node, property for property, on the
        carve-out the Ultra's pinned DTS reserves at the same address and
        size, no-map, without a compatible."""
        found = self.fragments(self.RAMOOPS)
        self.assertEqual(list(found), ["/reserved-memory/sec-pmsg@880900000"])
        x710 = dts_props(dts_node(self.DTS["gts9wifi"].read_text(), "\t\tsec_pmsg_mem: sec-pmsg@880900000 {\n"))
        reg = x710.pop("reg")
        self.assertIn("no-map", x710)
        del x710["no-map"]
        self.assertEqual(found["/reserved-memory/sec-pmsg@880900000"], x710)
        self.assertEqual(x710["compatible"], '"ramoops"')
        build = (REPO / "scripts/build-mainline-kernel.sh").read_text()
        pin = re.search(r"^fingerprint_baseline=([0-9a-f]{40})$", build, re.M).group(1)
        if not (REPO / ".git").exists() or not shutil.which("git"):
            self.skipTest("not a git checkout")
        pinned_dts = subprocess.run(["git", "-C", str(REPO), "show", f"{pin}:kernel/dts/sm8550-samsung-gts9uwifi.dts"],
                                    check=True, capture_output=True, text=True).stdout
        ultra = dts_props(dts_node(pinned_dts, "\t\tsec_pmsg_mem: sec-pmsg@880900000 {\n"))
        self.assertEqual(ultra, {"reg": reg, "no-map": None})
        # Inside /reserved-memory, where of_platform_default_populate_init()
        # makes a ramoops device of it.
        self.assertIn("\t\tsec_pmsg_mem: sec-pmsg@880900000 {\n", dts_node(pinned_dts, "\treserved-memory {\n"))

    def test_the_overlays_compile_and_resolve_only_these_labels(self):
        """Each compiles with dtc (as the stage script runs it) and refers to
        the base tree through these labels alone, which /__symbols__ of the
        pinned DTB resolves (scripts/test-performance-overlay.sh checks that
        on the DTB itself)."""
        if not shutil.which("dtc"):
            self.skipTest("no dtc")
        with tempfile.TemporaryDirectory() as tmp:
            for dtso, labels in ((self.SYNC, ["gcc", "gpucc", "rpmhpd"]), (self.RAMOOPS, [])):
                with self.subTest(dtso.name):
                    out = pathlib.Path(tmp) / (dtso.stem + ".dtbo")
                    run = subprocess.run(["dtc", "-I", "dts", "-O", "dtb", "-o", str(out), str(dtso)],
                                         capture_output=True, text=True)
                    self.assertEqual((run.returncode, run.stderr), (0, ""))
                    back = subprocess.run(["dtc", "-I", "dtb", "-O", "dts", str(out)],
                                          check=True, capture_output=True, text=True).stdout
                    fixups = re.search(r"\t__fixups__ \{\n(.*?)\n\t\};", back, re.S)
                    names = sorted(re.findall(r"^\t\t([a-z_]+) = ", fixups.group(1), re.M)) if fixups else []
                    self.assertEqual(names, labels)

    def test_the_helper_applies_them_on_the_ultra_alone_each_on_its_own(self):
        """After the board gate's "if (!ultra) return ret;", the Ultra's
        three overlays in turn, each its own transaction, each logging one
        line when it applied and its error kept; the stage script stages
        the two new ones under the names the helper uses."""
        source = self.HELPER.read_text()
        init = source[source.index("static int __init gts9u_performance_init(void)\n{\n"):]
        init = init[:init.index("\n}\n") + 3]
        gate = init.index("\tif (!ultra)\n\t\treturn ret;\n")
        calls = [(m.start(), m.group(1)) for m in re.finditer(r"\terr = gts9u_apply\((\w+),\s+sizeof\(\1\),", init)]
        self.assertEqual([name for at, name in calls if at > gate],
                         ["gts9u_performance_overlay", "gts9u_sync_state_overlay", "gts9u_ramoops_overlay"])
        self.assertEqual(len(calls), 5)
        for name, line in (("gts9u_sync_state_overlay",
                            "gts9u-performance: SoC hand-off: gcc under CX, GMU post-init providers, "
                            "crypto engine disabled"),
                           ("gts9u_ramoops_overlay", "gts9u-performance: ramoops in the pmsg carve-out at 0x880900000")):
            with self.subTest(name):
                block = init[init.index(f"\terr = gts9u_apply({name},"):]
                block = block[:min(block.index(end) for end in ("\tret = ret ?: err;\n", "\treturn ret ?: err;\n")
                                   if end in block)]
                self.assertIn(f'\tif (!err)\n\t\tpr_info("{line}\\n");\n', block)
                self.assertEqual(source.count(line), 1)
        # Neither is applied before the gate, and the last one's error is
        # what the initcall returns with the others'.
        self.assertTrue(init.rstrip().endswith("return ret ?: err;\n}"))
        stage = self.STAGE.read_text()
        self.assertIn("for overlay in gts9u-performance gts9u-sync-state gts9u-ramoops gts9-performance "
                      "gts9-family; do\n", stage)
        for dtso, symbol in (("gts9u-sync-state", "gts9u_sync_state_overlay"),
                             ("gts9u-ramoops", "gts9u_ramoops_overlay")):
            self.assertIn(f"('{dtso}', '{symbol}'),", stage)
            self.assertTrue((REPO / f"kernel/dts/{dtso}.dtso").is_file())

    def test_the_cx_vote_patches_are_in_every_build(self):
        """With gcc under CX on both boards the UFS and USB drivers drop
        their CX votes in system sleep on both: the two hooks are in no board
        gate, in the order the X710 has always applied them."""
        build = (REPO / "scripts/build-mainline-kernel.sh").read_text()
        hooks = ("\napply_unless 'ufshcd_opp_drop_votes' \\\n"
                 "\tdrivers/ufs/core/ufshcd.c ufshcd-drop-opp-votes-in-system-suspend.patch\n"
                 "apply_unless 'dwc3_qcom_set_pstate' \\\n\tdrivers/usb/dwc3/dwc3-qcom.c \\\n"
                 "\tdwc3-qcom-drop-required-opps-vote-in-system-suspend.patch\n")
        self.assertEqual(build.count(hooks), 1)
        before = build[:build.index(hooks)]
        self.assertIn("\nfi\n", before[before.rindex("\nif "):])



def x710_kit_tests():
    """The SM-X710 kit's test module, loaded under a name of its own: its
    sleep-test.sh fakes are what the SM-X910's runs of the same script use."""
    spec = importlib.util.spec_from_file_location("typec_roles_kit_tests_for_the_ultra",
                                                  REPO / "tests/test_typec_roles_kit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


X710 = x710_kit_tests()


def own_tests_only(cls):
    """A subclass of a test case that runs its own tests, not the ones it
    inherits (unittest takes only callable test attributes)."""
    for name in dir(cls):
        if name.startswith("test") and name not in vars(cls):
            setattr(cls, name, None)
    return cls


# The SM-X910's touch controller as blastoise's /proc/interrupts shows it
# (2026-11-25; counts made up), and the kernel lines of a double tap that
# aborts a suspend on the way down, the SM-X710's of 2026-11-22 with this
# board's IRQ and driver.
ULTRA_INTERRUPTS = X710.INTERRUPTS.replace(
    "199:          9          0          0          0          0          0          0          0   msmgpio  25 Level     fts1ba90a\n",
    "201:          9          0          0          0          0          0          0          0   msmgpio  25 Level     goodix-berlin\n")
ULTRA_TOUCH_ABORT = X710.TOUCH_ABORT.replace("IRQ 199", "IRQ 201").replace(
    "samsung_fts1ba90a 9-0049: double tap to wake", "goodix-berlin-i2c 3-005d: double tap")
ULTRA_TOUCH_HINT = "IRQ 201 (goodix-berlin, the touchscreen: do not touch the screen during the run)"


@own_tests_only
class UltraSleepTestTest(X710.SleepTestTest):
    """tools/inplace/typec-roles/sleep-test.sh, which the SM-X910 kit ships
    since revision 8, on a fake SM-X910: the compatible, the Wi-Fi function
    with ath12k_wifi7_pci, the Goodix touch controller, both radios on, and
    the interconnect floor held, as every boot there holds it (no late
    release on this board): gem_noc and mc_virt wait for the two bandwidth
    monitors alone.  What is the same on both boards is the SM-X710 kit's
    tests'; these are what the SM-X910 does otherwise, and the pmic
    option."""

    PROVIDERS = (("24100000.interconnect", "240b6400.pmu", b"qcom,sm8550-cpu-bwmon\0qcom,sdm845-bwmon\0"),
                 ("interconnect-1", "24091000.pmu", b"qcom,sm8550-llcc-bwmon\0qcom,sc7280-llcc-bwmon\0"))

    def setUp(self):
        super().setUp()
        F = self.root
        (F / "proc/device-tree/compatible").write_bytes(b"samsung,gts9uwifi\0qcom,sm8550\0")
        # The SM-X710's late release is not on this board, nor its records.
        for path in ("usr/libexec/ubuntu-gts9u-icc-late-release", "var/lib/ubuntu-gts9u/icc-release.pending",
                     "run/ubuntu-gts9u/icc-release", "var/log/ubuntu-gts9u-icc-release.log"):
            (F / path).unlink()
        # No hung task detector in this board's kernel (the release's
        # configuration: CONFIG_DETECT_HUNG_TASK is not set; blastoise has
        # no /proc/sys/kernel/hung_task_panic, 2026-11-25).
        (F / "proc/sys/kernel/hung_task_panic").unlink()
        for provider, pmu, compatible in self.PROVIDERS:
            d = F / "sys/bus/platform/devices" / provider
            (d / "state_synced").write_text("0\n")
            self.consumer(provider, pmu, compatible)
        self.radios("1", "1")

    def consumer(self, provider, name, compatible, status="available", driver=False):
        """A device link from PROVIDER to the consumer NAME, as sysfs shows
        it: consumer:platform:NAME with its status and a link to the device,
        whose node has COMPATIBLE."""
        dev = self.root / "sys/devices/platform/soc@0" / name
        (dev / "of_node").mkdir(parents=True, exist_ok=True)
        (dev / "of_node/compatible").write_bytes(compatible)
        if driver:
            (dev / "driver").mkdir(exist_ok=True)
        link = self.root / "sys/bus/platform/devices" / provider / f"consumer:platform:{name}"
        link.mkdir()
        (link / "status").write_text(status + "\n")
        (link / "consumer").symlink_to(dev)

    def radios(self, bluetooth, wlan):
        """The two rfkill switches, blastoise's names: STATE 1 unblocked,
        0 blocked."""
        for i, (name, kind, state) in enumerate((("hci0", "bluetooth", bluetooth), ("phy0", "wlan", wlan))):
            r = self.root / f"sys/class/rfkill/rfkill{i}"
            r.mkdir(parents=True, exist_ok=True)
            for attr, value in (("name", name), ("type", kind), ("state", state)):
                (r / attr).write_text(value + "\n")

    def synced(self):
        return [(self.root / "sys/bus/platform/devices" / p / "state_synced").read_text().strip()
                for p, _, _ in self.PROVIDERS]

    def wifi_bound(self):
        """The fake bus's Wi-Fi function with ath12k_wifi7_pci bound, in D0."""
        paths = self.pci_tree()
        driver = self.root / "sys/bus/pci/drivers/ath12k_wifi7_pci"
        driver.mkdir(parents=True)
        (paths["0000:01:00.0"] / "driver").symlink_to(os.path.relpath(driver, paths["0000:01:00.0"]))
        return paths

    def test_release_floor_releases_the_floor_in_the_run_and_the_run_sleeps(self):
        """No late release on the SM-X910: release-floor writes "1" to
        state_synced of the two providers that wait for the unbound
        bandwidth monitors alone, before anything is frozen, and nothing
        else; the run sleeps with the floor released, and says that the
        next boot holds it again."""
        result = self.run_test("ro", "1", "release-floor", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        self.assertEqual(self.synced(), ["1", "1"])
        self.assertIn("interconnect floor held; the SM-X910 has no late release: releasing it here, state_synced "
                      "of 24100000.interconnect interconnect-1 (each waits for the unbound bandwidth monitors alone)",
                      self.out)
        self.assertIn("SLEEP-TEST cycle 1/1: PASS: deep (aosd +1 cxsd +1 ddr +1), resumed, the UFS read back as "
                      "before; floor released", self.out)
        self.assertIn("NEXT: the boot after this restart holds the interconnect floor again (the SM-X910 releases "
                      "it only in a run); give the next run release-floor", self.out)
        self.assertNotIn("loop breaker", self.out)
        self.assertNotIn("restart", self.read("systemctl.calls"))
        self.assertIn("gts9u sleep-test: ", (self.root / "dev/kmsg").read_text())
        self.assertIn("releasing the interconnect floor: 24100000.interconnect interconnect-1",
                      (self.root / "dev/kmsg").read_text())
        (logs,) = (self.kit / "logs").glob("*-sleep")
        before = (logs / "before").read_text()
        self.assertIn("interconnect floor: held; the SM-X910 has no late release (release-floor releases it in "
                      "the run: this one does)", before)
        self.assertIn("radios: Bluetooth on, Wi-Fi on (rfkill)", before)
        self.assertIn("hung tasks: this kernel has no hung task detector (no kernel.hung_task_panic): a writer "
                      "waiting on the frozen disk is not reported", before)
        # rpmhpd, gcc and gpu_cc are never written, whatever they read.
        self.assertEqual((self.root / "sys/bus/platform/devices/17a00000.rsc:power-controller/state_synced")
                         .read_text(), "1\n")

    def test_a_kernel_with_the_hung_task_detector_needs_its_panic_off(self):
        """The same check as the SM-X710's where the file is there: 0 runs
        with no hung-task line in the record, anything else is refused."""
        (self.root / "proc/sys/kernel/hung_task_panic").write_text("0\n")
        result = self.run_test("ro", "1", "release-floor", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("SLEEP-TEST cycle 1/1: PASS: deep", self.out)
        (logs,) = (self.kit / "logs").glob("*-sleep")
        self.assertNotIn("hung tasks:", (logs / "before").read_text())
        fresh_fake(self)
        (self.root / "proc/sys/kernel/hung_task_panic").write_text("1\n")
        result = self.run_test("ro", "1", "release-floor", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("REFUSED: kernel.hung_task_panic is 1, not 0", self.out)
        self.assertEqual(self.read("rtcwake.calls"), "")

    def test_without_release_floor_the_floor_stays_held_and_every_verdict_says_so(self):
        result = self.run_test("ro", "1", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertEqual(self.synced(), ["0", "0"])
        self.assertIn("THE INTERCONNECT FLOOR IS held: this run cannot reach the depth of the 2026-11-21 failure",
                      self.out)
        self.assertIn("floor held", self.out)
        self.assertNotIn("holds the interconnect floor again", self.out)

    def test_release_floor_is_refused_while_anything_else_holds_a_provider_back(self):
        """The late release's rule: all or nothing, the bandwidth monitors
        alone, without a driver.  The crypto engine of a kernel without
        gts9u-sync-state.dtso, a bandwidth monitor with a driver bound, or
        a consumer of another compatible at a monitor's name, each stops
        the run before anything is done; icc-hold too."""
        cases = {
            "the crypto engine": (lambda: self.consumer("interconnect-1", "1dfa000.crypto",
                                                        b"qcom,sm8550-qce\0qcom,qce\0"),
                                  "interconnect-1 waits for 1dfa000.crypto(available);"),
            "a monitor with its driver": (
                lambda: (self.root / "sys/devices/platform/soc@0/240b6400.pmu/driver").mkdir(),
                "24100000.interconnect waits for 240b6400.pmu(available);"),
            "another device at a monitor's name": (
                lambda: (self.root / "sys/devices/platform/soc@0/24091000.pmu/of_node/compatible").write_bytes(
                    b"qcom,something-else\0"),
                "interconnect-1 waits for 24091000.pmu(available);"),
            "icc-hold": (lambda: (self.root / "etc/ubuntu-gts9u/icc-hold").touch(),
                         "exists: the floor is held on purpose"),
        }
        for name, (make, message) in cases.items():
            with self.subTest(name):
                fresh_fake(self)
                make()
                before = self.manifest()
                result = self.run_test("ro", "1", "release-floor", "restart-ok", "hold=0")
                self.assertEqual(result.returncode, 1, self.out)
                self.assertIn("REFUSED", self.out)
                self.assertIn(message, self.out)
                self.assertEqual(self.synced(), ["0", "0"])
                self.assertEqual(self.read("rtcwake.calls"), "")
                self.assertEqual(self.manifest(), before)

    def test_both_radios_off_is_refused_on_the_sm_x910_alone(self):
        """What a run is for there first: L15B through a whole-SoC sleep
        with a radio on.  Either radio on is enough; a dry run sleeps not at
        all; the SM-X710 runs as before."""
        self.radios("0", "0")
        result = self.run_test("ro", "1", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("REFUSED: Bluetooth and Wi-Fi are both off (no rfkill of either unblocked): on the SM-X910 "
                      "a run is first for PM8550 L15B", self.out)
        self.assertEqual(self.read("rtcwake.calls"), "")
        for name, radios, args in (("Bluetooth on", ("1", "0"), ("ro", "1")), ("Wi-Fi on", ("0", "1"), ("ro", "1")),
                                   ("dry", ("0", "0"), ("dry",))):
            with self.subTest(name):
                fresh_fake(self)
                self.radios(*radios)
                self.run_test(*args, "restart-ok", "hold=0")
                self.assertNotIn("REFUSED", self.out)
                self.assertNotIn("radios", self.out.replace("radios: Bluetooth", ""))
        with self.subTest("the SM-X710"):
            fresh_fake(self, ultra=False)
            self.radios("0", "0")
            result = self.run_test("ro", "1", "restart-ok", "hold=0")
            self.assertNotIn("REFUSED", self.out)
            self.assertNotIn("radios:", self.out)
            self.assertIn("SLEEP-TEST cycle 1/1: PASS", self.out)

    def test_the_wifi_judged_is_the_board_s_own(self):
        """ath12k_wifi7_pci, the SM-X910's: its firmware's load, its country
        line a note, its failure line a PCIE-DOWN."""
        country = "ath12k_wifi7_pci 0000:01:00.0: Failed to set the requested Country regulatory setting"
        fw = ("ath12k_wifi7_pci 0000:01:00.0: fw_version 0x1103006c fw_build_timestamp 2026-03-06 09:10 "
              "fw_build_id QC_IMAGE_VERSION_STRING=WLAN.HMT.1.1.c7-00108")
        lost = "ath12k_wifi7_pci 0000:01:00.0: failed to resume core: -110"

        def cycle(*lines):
            return ("[  100.000000] PM: suspend entry (deep)\n[  100.000005] Timekeeping suspended for 30.002 "
                    "seconds\n" + "".join(f"[  100.00001{i}] {line}\n" for i, line in enumerate(lines))
                    + "[  101.000009] PM: suspend exit\n")

        self.wifi_bound()
        self.r11_switches()
        self.cycle(1, log=cycle(country, fw))
        result = self.run_test("ro", "2", "pci=keep", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn("0000:01:00.0 ath12k_wifi7_pci D0 -; no Wi-Fi failure line; note: " + country
                      + " (1 line of the firmware turning a country setting down: not a failure line); ath12k "
                      "firmware loads 1\n", self.out)
        self.assertIn("SLEEP-TEST RESULT: PASS: 2 of 2 cycles", self.out)
        fresh_fake(self)
        self.wifi_bound()
        self.r11_switches()
        self.cycle(1, log=cycle(country))
        self.run_test("ro", "2", "pci=keep", "restart-ok", "hold=0")
        self.assertIn("Wi-Fi did not come back (no Wi-Fi function in D0 with ath12k_wifi7_pci bound, or no firmware "
                      f"load in this cycle's kernel lines; ath12k said: {country})", self.out)
        fresh_fake(self)
        self.wifi_bound()
        self.r11_switches()
        self.cycle(1, log=cycle(lost, fw))
        self.run_test("ro", "2", "pci=keep", "restart-ok", "hold=0")
        self.assertIn(f"SLEEP-TEST RESULT: PCIE-DOWN after cycle 1/2: with the PCI devices kept, the PCIe host or the "
                      f"Wi-Fi did not come back ({lost})", self.out)

    def test_a_double_tap_on_the_way_down_is_named_by_the_board_s_touch_irq(self):
        (self.root / "proc/interrupts").write_text(ULTRA_INTERRUPTS)
        self.touch_abort(1, log=ULTRA_TOUCH_ABORT, irq=201)
        result = self.run_test("ro", "2", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assertIn(f"ABORTED: the kernel gave up the suspend before the tablet slept (rtcwake 1, suspend_stats "
                      f"success +0 fail +1): a wakeup from {ULTRA_TOUCH_HINT}", self.out)
        self.assertIn("SLEEP-TEST RESULT: PASS: 2 of 2 cycles resumed with the UFS readable, 2 of them deep; 1 "
                      "attempt aborted before the sleep (IRQ 201 (goodix-berlin, the touchscreen) once) and tried "
                      "again; floor held", self.out)

    def registers(self, ocp=(0, 0, 0, 0, 0, 0), pon=0):
        """The regmap's debugfs lines of SPMI device 0-00, every register
        0x0000 to 0xffff: the over-current log's six bytes at 0x7076, and
        PON as the byte at 0x7010 (a stand-in for the power-on log)."""
        values = {0x7076 + i: b for i, b in enumerate(ocp)}
        values[0x7010] = pon
        return "".join(f"{a:04x}: {values.get(a, 0):02x}\n" for a in range(0x10000))

    def test_pmic_reads_the_records_before_and_after_each_sleep_and_writes_none(self):
        """Read only: the record before the run, and each cycle's evidence;
        a new over-current entry gets its own line.  The fake file is what
        the kernel's regmap debugfs file gives, read by offset."""
        regs = self.root / "sys/kernel/debug/regmap/0-00/registers"
        regs.parent.mkdir(parents=True)
        regs.write_text(self.registers())
        (self.root / "dev/pmsg0").write_text("")
        self.cycle(1)
        (self.tmp / "cycle-1/pmic").write_text(self.registers(pon=1))
        self.cycle(2)
        (self.tmp / "cycle-2/pmic").write_text(self.registers(ocp=(0x12, 0xcf, 0, 0, 0, 0), pon=1))
        result = self.run_test("ro", "2", "pmic", "restart-ok", "hold=0")
        self.assertEqual(result.returncode, 1, self.out)
        self.assert_read_only_rules()
        self.assertIn("cycle 1/2: evidence: PMIC: over-current log 0000 0000 0000 (unchanged); SDAM 0x7000-0x75ff "
                      "changed since the read before", self.out)
        self.assertIn("cycle 2/2: evidence: PMIC: over-current log 12cf 0000 0000 (was 0000 0000 0000: a NEW ENTRY); "
                      "SDAM 0x7000-0x75ff changed since the read before", self.out)
        self.assertIn("SLEEP-TEST PMIC: cycle 2/2: the PMIC logged a new over-current entry (0000 0000 0000 -> 12cf "
                      "0000 0000; 12cf is PM8550's LDO15, L15B): after the run, read its power-on and over-current "
                      "logs as root", self.out)
        self.assertEqual(self.out.count("SLEEP-TEST PMIC:"), 1)
        self.assertIn("SLEEP-TEST PMIC: cycle 2/2", (self.root / "dev/pmsg0").read_text())
        (logs,) = (self.kit / "logs").glob("*-sleep")
        self.assertIn("PMIC: over-current log (0x7076) 0000 0000 0000; SDAM 0x7000-0x75ff in", (logs / "before").read_text())
        self.assertEqual(len((logs / "pmic-before").read_text().splitlines()), 0x600)
        self.assertTrue((logs / "pmic-before").read_text().startswith("7000: 00\n"))
        # Every read of the file: from 0x7000, 0x600 lines of 9 bytes, never
        # a write (the dd stub logs any of= as a violation).
        reads = [c for c in self.read("dd.calls").splitlines() if "regmap" in c]
        self.assertEqual(len(reads), 4)
        for call in reads:
            self.assertEqual(call, f"if={regs} bs=9 skip=28672 count=1536 status=none")
        self.assertIn("options: pmic", self.out)

    def test_pmic_is_refused_where_the_records_cannot_be_read(self):
        """No file; lines of another width; and the same layout from another
        first register, where the read by offset gives 0x600 whole lines,
        0x7100's on: only the first line's address tells it apart."""
        shifted = "".join(f"{a:04x}: 00\n" for a in range(0x100, 0x10000))
        for name, content in (("no regmap", None), ("another layout", "00000: 00\n" * 10),
                              ("another first register", shifted)):
            with self.subTest(name):
                fresh_fake(self)
                if content is not None:
                    regs = self.root / "sys/kernel/debug/regmap/0-00/registers"
                    regs.parent.mkdir(parents=True)
                    regs.write_text(content)
                result = self.run_test("ro", "1", "pmic", "restart-ok", "hold=0")
                self.assertEqual(result.returncode, 1, self.out)
                self.assertIn("REFUSED: pmic: ", self.out)
                self.assertIn("does not read as PMK8550's SDAM, 0x600 lines from \"7000: \"", self.out)
                self.assertEqual(self.read("rtcwake.calls"), "")


def fresh_fake(case, ultra=True):
    """A fresh fake for another subtest: the SM-X910's, or with ULTRA false
    the SM-X710 kit's own."""
    if ultra:
        case.setUp()
    else:
        X710.SleepTestTest.setUp(case)


if __name__ == "__main__":
    unittest.main()
