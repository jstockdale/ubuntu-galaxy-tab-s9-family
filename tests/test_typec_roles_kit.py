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
KEY_ID = "aa" * 20
SIGNER = ":".join(["5A"] * 20)
MODULES = ("modules.dep", "kernel/net/a.ko", "updates/b.ko")
TCPM_DIR = "drivers/usb/typec/tcpm"
GCC = "drivers/clk/qcom/gcc-sm8550"
DWC3 = "drivers/usb/dwc3/dwc3-qcom"

PREFIXES = (
    "/dev/disk/by-partlabel/", "/usr/lib/modules/", "/var/backups", "/var/log/", "/run/",
    "/var/lib/gts9u-boot-sets/", "/proc/device-tree/", "/proc/sys/kernel/", "/proc/config.gz",
    "/sys/kernel/notes", "/sys/class/power_supply/", "/sys/class/typec/",
    "/sys/kernel/debug/usb/", "/sys/bus/usb/devices/", "/sys/module/sm5714_usbpd/parameters",
    "/dev/kmsg",
)

STUBS = {
    "id": 'echo 0',
    "uname": 'case $1 in -r) echo %s ;; *) echo "#1 SMP PREEMPT test" ;; esac' % RELEASE,
    "flock": 'exit 0',
    "findmnt": 'cat "$FAKE_ROOT/mount-options" 2>/dev/null || echo rw,relatime',
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
        for name in ("install.sh", "rollback.sh", "verify.sh", "README.md"):
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
        self.assertIn("size=100663296", text) if name != "verify.sh" else None
        text = text.replace("size=100663296", f"size={SIZE}")
        # The stand-in partitions are files, and a build host's dd may not do
        # direct I/O on them (uutils dd 0.10 fails with EINVAL).
        text = text.replace(" iflag=direct", "")
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
        env = {"PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_ROOT": str(self.root),
               "HOME": str(self.tmp)}
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
        hang-log kit's configuration and ramoops, and the switches as built."""
        self.running(build_id)
        params = self.root / "sys/module/sm5714_usbpd/parameters"
        (params / "sink_host_swap").write_text("Y\n")
        (params / "try_snk_extra_wait_ms").write_text("600\n")
        (self.root / "journal").write_text(
            f"kernel: Loaded X.509 cert 'Build time autogenerated kernel key: {KEY_ID}'\n"
            "kernel: ramoops: using 0x200000@0x880900000, ecc: 96\n")
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
        self.assertIn(f"ok   running the kit's kernel (build ID {NEWER_BUILD_ID[:12]})", result.stdout)
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

    def journal_add(self, text):
        with (self.root / "journal").open("a") as journal:
            journal.write(text)

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


class ScriptsTest(unittest.TestCase):
    def test_scripts_parse(self):
        for name, shell in (("install.sh", "sh"), ("rollback.sh", "sh"), ("verify.sh", "sh"),
                            ("make-kit.sh", "bash")):
            with self.subTest(name):
                subprocess.run([shell, "-n", str(KIT_SRC / name)], check=True)

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
        the second revision of that patch alone.  The last six came after
        it: the log lines of the three fixes for a swap asked for from
        userspace, of which the third has three (the PR_Swap one is in no
        earlier revision of that patch), and the driver's mark for reading
        its parameters at each attach."""
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
        # Each file a patch changes is one the kit compares with the hang-log build's.
        patched = re.search(r"^patched=\((.*?)\)$", text, re.M | re.S).group(1)
        changed = set()
        for name in names:
            changed.update(re.findall(r"^\+\+\+ b/(\S+)", (REPO / "kernel/patches" / name).read_text(),
                                      re.M))
        self.assertEqual(changed, {f"{TCPM_DIR}/tcpm.c", "include/linux/usb/tcpm.h", f"{GCC}.c",
                                   f"{DWC3}.c"})
        self.assertEqual(patched, '"$tcpm_dir/tcpm.c" include/linux/usb/tcpm.h "$gcc.c" "$dwc3.c"')
        self.assertIn("\ntcpm_dir=drivers/usb/typec/tcpm\n", text)
        self.assertIn("\ngcc=drivers/clk/qcom/gcc-sm8550\n", text)
        self.assertIn("\ndwc3=drivers/usb/dwc3/dwc3-qcom\n", text)

    def test_every_changed_source_is_required_dated_and_compared(self):
        """The kit's changes to the kernel tree are the patched files and the
        driver.  Each source is required, its object is dated against it,
        and with HANG_SCRATCH each is what may differ from the hang-log tree.
        The dwc3-qcom guard adds no string to the image: the marker in the
        build tree's source and the date of its object are its proof."""
        text = (KIT_SRC / "make-kit.sh").read_text()
        names = {"tcpm_dir": TCPM_DIR, "gcc": GCC, "dwc3": DWC3}

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
        self.assertNotIn("llvm-objdump", text)

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
        self.assertIn('llvm-objcopy -O binary --only-section=.data "$HANG_SCRATCH/linux-gts9wifi/$gcc.o"',
                      text)
        self.assertIn("""tr '\\n' ';')" = '5 6;5 6;' ]""", text)
        self.assertIn('\techo "power domain table: $gdsc_check"\n', text)
        tools = re.search(r"^for t in (.*); do$", text, re.M).group(1).split()
        self.assertIn("llvm-objcopy", tools)
        self.assertNotIn("llvm-objdump", tools)


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
                 f'tcpm_dir={TCPM_DIR}\ngcc={GCC}\ndwc3={DWC3}\n' + objects.group(0) + "\n" + gate.group(0),
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
        kit = re.search(r"^patches=\((.*?)\)$", text, re.M | re.S).group(1).split()
        self.assertEqual(sorted(re.findall(r"`kernel/patches/([^`]+)`", readme)), sorted(kit))
        self.assertEqual((len(kit), len([name for name in kit if name.startswith("tcpm-")])), (7, 5))
        self.assertIn("seven patches, five of them to TCPM.", readme)
        # What has run, and what has not.
        self.assertIn("New in this revision are the third fix, the SM-X910's power domain change "
                      "and a NULL check in the dwc3-qcom glue. The third fix and the NULL check "
                      "are in no kernel that has run on a tablet, and the power domain change has "
                      "run on the SM-X910 only. None of the three fixes and neither switch has "
                      "been tried on a tablet. **On the SM-X710 the previous revision of the kit "
                      "is installed, without the third fix and the power domain change.**", readme)
        for stale in ("was built on", "no dock has been plugged into it", "is not in this kit",
                      "this kit changes nothing there"):
            self.assertNotIn(stale, readme)
        # What the SM-X710 showed, and what is still to be done there.
        self.assertIn("That is the SM-X910's fault, seen once here: the reason this revision "
                      "carries the power domain change.", readme)
        self.assertIn("not done yet: `verify.sh dock` and `swap` on a kit kernel, the Satechi on a "
                      "kit kernel, and a boot with the dock attached and devices on the bus.",
                      readme)
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
