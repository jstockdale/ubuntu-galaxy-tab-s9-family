"""Run the Type-C roles kit's tablet scripts (tools/inplace/typec-roles)
against a fake root.

install.sh and rollback.sh normally run as root on the SM-X710 and write the
boot partition; verify.sh reads the Type-C port and the TCPM log.  Here every
absolute path they touch is moved under a temporary directory, the partition
size and the pinned hang-log image identities are replaced by those of small
stand-in images, and id, uname, flock, findmnt, fuser, dpkg, depmod, blockdev,
systemctl, journalctl, modinfo, dmesg, sleep and sync are stubbed; the sync
stub records what a power failure right after each call would find.
make-kit.sh needs a kernel build and is only checked for syntax, for pinning
the same identities as the tablet scripts, for what its comparison of two
kernel source trees leaves out, for the strings it looks for in the image,
and for its check that the image was built from the sources as they are.
"""
import gzip
import hashlib
import io
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
KEY_ID = "aa" * 20
SIGNER = ":".join(["5A"] * 20)
MODULES = ("modules.dep", "kernel/net/a.ko", "updates/b.ko")
TCPM_DIR = "drivers/usb/typec/tcpm"

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
    "findmnt": 'echo rw,relatime',
    "fuser": 'exit 1',
    "dpkg": 'exit 0',
    "depmod": 'exit 0',
    "blockdev": 'exit 0',
    "systemctl": 'exit 3',
    "sleep": 'exec /usr/bin/sleep 0.01',
    "dmesg": 'cat "$FAKE_ROOT/dev/kmsg" 2>/dev/null',
    "journalctl": 'cat "$FAKE_ROOT/journal" 2>/dev/null',
    "modinfo": 'case $1 in -n) echo /fake/netconsole.ko ;; -F) cat "$FAKE_ROOT/signer" ;; esac',
    # What a power failure right after this call would find: is the hang-log
    # image saved, whose module tree is in place, what boot holds, is an
    # install complete.  The call fails if a file named after its number asks.
    "sync": '''b=$FAKE_ROOT/%s
echo "saved=$([ -s "$b/boot-hang-log.img" ] && echo yes || echo no)" \\
	"tree=$(cut -d' ' -f1 "$FAKE_ROOT/usr/lib/modules/%s/modules.dep" 2>/dev/null)" \\
	"boot=$(sha256sum < "$FAKE_ROOT/dev/disk/by-partlabel/boot" | cut -c1-64)" \\
	"complete=$([ -e "$b/complete" ] && echo yes || echo no)" >> "$FAKE_ROOT/sync.calls"
[ ! -e "$FAKE_ROOT/sync-fails-$(wc -l < "$FAKE_ROOT/sync.calls")" ]''' % (BACKUP, RELEASE),
}
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
        self.assertFalse((self.root / "usr/lib/modules" / f".typec-roles-{RELEASE}").exists())

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

    def test_second_install_is_refused(self):
        self.assertEqual(self.run_script("install.sh").returncode, 0)
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("already installed", result.stdout)
        self.assertEqual(self.part("boot").read_bytes(), self.new_boot)
        self.assertEqual(self.tree()["updates/b.ko"], "new updates/b.ko\n")

    def saved_set(self, boot):
        """A saved Ubuntu boot set (dual boot) whose boot.img is BOOT."""
        saved = self.root / "var/lib/gts9u-boot-sets/ubuntu"
        saved.mkdir(parents=True)
        (saved / "boot.img").write_bytes(boot)
        return saved

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
        self.assertEqual(self.syncs(), [
            {"saved": "yes", "tree": "hang", "boot": hang, "complete": "no"},
            {"saved": "yes", "tree": "new", "boot": hang, "complete": "no"},
            {"saved": "yes", "tree": "new", "boot": new, "complete": "yes"},
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
        hang_log = {"tree": "hang", "boot": sha(self.hang_boot)}
        self.assertEqual(self.syncs(), [
            # The backup is still where a second run would look for it.
            {**hang_log, "saved": "yes", "complete": "yes"},
            {**hang_log, "saved": "no", "complete": "no"},
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
                self.assertEqual(self.tree()["updates/b.ko"], "hang updates/b.ko\n")
                self.assertFalse((self.root / BACKUP).exists())

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


class VerifyTest(FakeTablet):
    def setUp(self):
        super().setUp()
        self.port = self.root / "sys/class/typec/port0"
        self.partner = self.root / "sys/class/typec/port0-partner"
        self.log = self.root / "sys/kernel/debug/usb/tcpm-4-0033/log"
        self.log.write_text("")
        self.roles("device", "sink")
        usb = self.root / "sys/bus/usb/devices/1-1"
        (usb / "idVendor").write_text("291a\n")
        (usb / "idProduct").write_text("8383\n")

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
                self.log.write_text(log)
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
        self.plug([(PASS_LOG, "host", "sink"), (FLAP_LOG, "device", "sink"),
                   (TRY_SNK_LOG, "host", "sink"), (PASS_LOG, "host", "source"),
                   (PASS_LOG, "device", "sink"), (WAIT_LOG, "host", "sink")])
        result = self.run_script("verify.sh", "dock", "6")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        rows = self.results()
        self.assertEqual([r["verdict"] for r in rows],
                         ["PASS", "FAIL", "PASS", "FAIL", "FAIL", "PASS"])
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
        self.assertIn("3 of 6 plug-ins ended as USB host and power sink", result.stdout)
        self.assertIn("this is a baseline run", result.stdout)
        (trial,) = (self.kit / "logs").glob("*/trial-1.tcpm.log")
        self.assertEqual(trial.read_text(), PASS_LOG)
        (state,) = (self.kit / "logs").glob("*/trial-1.state")
        self.assertIn("1-1 291a:8383", state.read_text())

    def test_record_gives_no_verdict_and_passes(self):
        self.plug([(TRY_SNK_LOST_LOG, "host", "source")])
        result = self.run_script("verify.sh", "record")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (row,) = self.results()
        self.assertEqual((row["verdict"], row["data_8s"], row["power_8s"], row["try_snk_lost"]),
                         ("-", "host", "source", "1"))

    def test_now_saves_the_log_without_waiting_for_a_plug(self):
        self.log.write_text(PASS_LOG)
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
        the TCPM log is LOG and data_role takes a write and reads as ROLE
        (None: it refuses every write); unplugged once the trial is recorded.
        Returns the list that gets what the switch said during each trial."""
        kmsg = self.root / "dev/kmsg"
        logs = self.kit / "logs"
        switch = self.root / "sys/module/sm5714_usbpd/parameters/sink_host_swap"
        seen = []
        if getattr(self, "player", None):
            self.player.join()
        kmsg.unlink(missing_ok=True)
        self.data_role(None)
        # The script's two waits before it reads the roles are long enough
        # here for all the writer's writes; the writer's own are short.
        (self.bin / "sleep").write_text("#!/bin/sh\ncase $1 in\n8|6) exec /usr/bin/sleep 0.3 ;;\n"
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
                self.log.write_text(log)
                self.data_role(role)
                self.partner.mkdir()
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
        ])
        result = self.run_script("verify.sh", "swap", "7")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual([r["verdict"] for r in self.results()],
                         ["PASS", "FAIL", "FAIL", "PASS", "-", "FAIL", "PASS"])
        self.assertEqual({len(r) for r in self.results()}, {20})
        rows = self.swap_rows()
        self.assertEqual([(r["asked"], r["refused_in_vdm"], r["sent"], r["role_unchanged"],
                           r["cut_short"]) for r in rows],
                         [("3", "2", "1", "0", "0"), ("1", "0", "1", "0", "1"),
                          ("1", "0", "1", "0", "0"), ("3", "2", "1", "0", "0"),
                          ("0", "0", "0", "0", "0"), ("1", "0", "1", "0", "0"),
                          ("2", "0", "2", "1", "0")])
        accepted = "write 1 was accepted, the data role right after it: "
        self.assertEqual([r["write"] for r in rows],
                         [accepted + "host", accepted + "device", accepted + "host",
                          accepted + "device", "none of 25 writes was accepted",
                          accepted + "device", accepted + "host"])
        out = result.stdout
        self.assertIn("trial 1: PASS, data host/host, power sink/sink (at 8 s/14 s)", out)
        self.assertIn("  data_role: 3 swaps asked for, 2 refused during a discovery, 1 sent, "
                      "0 ended with the role unchanged, 0 cut short by a Soft Reset; "
                      + accepted + "host\n", out)
        self.assertIn("  data_role: 1 swaps asked for, 0 refused during a discovery, 1 sent, "
                      "0 ended with the role unchanged, 1 cut short by a Soft Reset; "
                      + accepted + "device\n", out)
        self.assertIn("  data_role: 2 swaps asked for, 0 refused during a discovery, 2 sent, "
                      "1 ended with the role unchanged, 0 cut short by a Soft Reset; ", out)
        self.assertIn("trial 5: data ?/?, power sink/sink", out)
        # Trial 3 alone, trials 2 and 6 (in trial 4 the swap was done before
        # the reset undid it), trial 5 alone.
        self.assertEqual(out.count("the kernel asked for the host role itself: "
                                   "the switch was not off for this plug-in"), 1)
        self.assertEqual(out.count("a write reported a swap that did not happen"), 2)
        self.assertEqual(out.count("no swap was asked for: not a PD source with the tablet "
                                   "as its USB device"), 1)
        self.assertIn("3 of 7 plug-ins: a swap asked for through data_role, none sent into a "
                      "discovery, none reported done with the role unchanged", out)
        self.assertNotIn("the refusal itself was not seen", out)
        # The kernel's own request is off for every trial and back afterwards.
        self.assertEqual(seen, ["N\n"] * 7)
        self.assertEqual(switch.read_text(), "Y\n")
        (kernel,) = (self.kit / "logs").glob("*/kernel")
        self.assertIn("sink_host_swap: N\n", kernel.read_text())
        self.assertIn("sink_host_swap before this run and after it: Y\n", kernel.read_text())

    def test_swap_says_when_no_write_met_a_discovery(self):
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
                self.assertIn("note no write fell into a discovery, so the refusal itself "
                              "was not seen: plug in again", result.stdout)
                self.assertIn("this is a baseline run", result.stdout)
                self.assertEqual(seen, [was])
                self.assertEqual(switch.read_text() if switch.exists() else None, was)
                (kernel,) = (self.kit / "logs").glob("*/kernel")
                self.assertIn("sink_host_swap: N\n" if was else "sink_host_swap: absent\n",
                              kernel.read_text())
                self.assertEqual("before this run" in kernel.read_text(), bool(was))

    def test_a_swap_run_that_is_stopped_puts_the_switch_back_and_leaves_no_writer(self):
        self.boot_kit()
        self.data_role(None)
        switch = self.root / "sys/module/sm5714_usbpd/parameters/sink_host_swap"
        path = self.tmp / "run-verify.sh"
        path.write_text(self.script("verify.sh"))
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
        # It waits for the plug-in now, and its writer with it.
        time.sleep(0.2)
        self.assertEqual(switch.read_text(), "N\n")
        run.terminate()
        stdout, stderr = run.communicate(timeout=60)
        self.assertEqual(run.returncode, 1, stdout + stderr)
        self.assertIn("stopped; what was recorded is in", stdout)
        self.assertEqual(switch.read_text(), "Y\n")
        # A writer left behind would take this plug-in.
        self.data_role("device")
        self.partner.mkdir()
        time.sleep(0.3)
        self.assertEqual(self.port.joinpath("data_role").read_text(), "host [device]\n")

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
        self.running(NEW_BUILD_ID)
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
        self.assertNotIn("USB host controller", result.stdout)

    def journal_add(self, text):
        with (self.root / "journal").open("a") as journal:
            journal.write(text)

    def test_checks_know_the_controller_s_other_ways_of_dying(self):
        """Reported like the death in the boot's suspend cycle, and like it
        not a failure of this kit."""
        for line in USB_HOST_ERRORS:
            with self.subTest(line):
                self.boot_kit()
                self.journal_add(f"kernel: PM: suspend entry (deep)\n{line}\n"
                                 "kernel: PM: suspend exit\n")
                result = self.run_script("verify.sh")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("note the USB host controller died this boot (not something this "
                              f"kit changes):\n{line}\n", result.stdout)
                self.assertNotIn("FAIL", result.stdout)
                self.assertNotIn("came back from a suspend", result.stdout)
                self.assertIn("all checks passed", result.stdout)

    def test_a_controller_that_was_set_up_again_after_a_suspend_is_noted_and_is_not_a_death(self):
        self.boot_kit()
        self.journal_add("kernel: PM: suspend entry (deep)\n"
                         f"{USB_HOST_REINIT}\nkernel: PM: suspend exit\n")
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("note the USB host controller came back from a suspend without its state and "
                      "was set up again (times this boot: 1)", result.stdout)
        self.assertNotIn("the USB host controller died", result.stdout)
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

    def test_a_dead_usb_host_controller_is_reported_and_not_a_failure(self):
        """The kit does not carry the SM-X910's USB power domain change."""
        self.boot_kit()
        self.journal_add(USB_HOST_DIED)
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("FAIL", result.stdout)
        self.assertIn("note the USB host controller died this boot (not something this kit changes):",
                      result.stdout)
        for line in ("xHCI host not responding to stop endpoint command", "HC died; cleaning up",
                     "Host halt failed, -110"):
            self.assertIn(line, result.stdout)
        self.assertIn("all checks passed", result.stdout)

    def test_checks_fail_on_the_hang_log_kernel(self):
        self.boot_kit()
        self.running(HANG_BUILD_ID)
        for name in ("sink_host_swap", "try_snk_extra_wait_ms"):
            (self.root / "sys/module/sm5714_usbpd/parameters" / name).unlink()
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"FAIL build ID {HANG_BUILD_ID}", result.stdout)
        self.assertIn("FAIL sm5714_usbpd.sink_host_swap is missing", result.stdout)


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

    def test_the_image_check_looks_for_what_only_this_revision_has(self):
        """The first three strings are also in an image built with the plain
        longer timeout before it, and the deadline of the Try.SNK wait is in
        the second revision of that patch alone.  The last three came after
        it: the log lines of the two fixes for a swap asked for from
        userspace, and the driver's mark for reading its parameters at each
        attach."""
        text = (KIT_SRC / "make-kit.sh").read_text()
        strings = shlex.split(re.search(r"^new_strings=\((.*?)\)$", text, re.M | re.S).group(1))
        self.assertEqual(strings, ["Requesting DFP before discovery", "sink_host_swap",
                                   "try_snk_extra_wait_ms", "try_snk_wait_until",
                                   "VDM in progress, DATA_ROLE_SWAP not started",
                                   "Data role swap ended with the role unchanged", "policy_open"])
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
                               "tcpm-dr-set-checks-data-role.patch")]
        others.append(driver)
        for name in ("try_snk_wait_until", "tcpm_try_snk_wait_left"):
            with self.subTest(name):
                self.assertFalse([other[:40] for other in others if name in other])
        # The build tree is checked for the same revision of the patch.
        self.assertIn("grep -q 'tcpm_try_snk_wait_left' \"$tree/$tcpm_dir/tcpm.c\"", text)
        # Each of the two log lines is its own patch's, there once, and the
        # build tree is checked for it.
        for name, line in (("tcpm-no-dr-swap-during-vdm.patch", strings[4]),
                           ("tcpm-dr-set-checks-data-role.patch", strings[5])):
            with self.subTest(name):
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
                                 "tcpm-dr-set-checks-data-role.patch"])
        build = (REPO / "scripts/build-mainline-kernel.sh").read_text().replace("\\\n", " ")
        for name in names:
            with self.subTest(name):
                self.assertTrue((REPO / "kernel/patches" / name).is_file())
                self.assertEqual(len(re.findall(
                    rf"^apply_unless '[^']+'\s+\S+\s+{re.escape(name)}\s*$", build, re.M)), 1)
        # Each file a patch changes is one the kit compares with the hang-log build's.
        changed = set()
        for name in names:
            changed.update(re.findall(r"^\+\+\+ b/(\S+)", (REPO / "kernel/patches" / name).read_text(),
                                      re.M))
        self.assertEqual(changed, {f"{TCPM_DIR}/tcpm.c", "include/linux/usb/tcpm.h"})

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
                 f'tcpm_dir={TCPM_DIR}\n' + objects.group(0) + "\n" + gate.group(0),
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
        for said in ("until the reboot the running kernel can load no module it has not loaded yet",
                     "while the kit is installed, do not upgrade ubuntu-gts9u-device: its "
                     "updates/qcom_spss_irq.ko is signed for another kernel"):
            self.assertIn(f"say '{said}'", install)


if __name__ == "__main__":
    unittest.main()
