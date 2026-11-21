"""Run the SM-X910 Type-C roles kit's tablet scripts
(tools/inplace/typec-roles-ultra) against a fake root.

install.sh and rollback.sh normally run as root on the SM-X910 and write the
boot partition; verify.sh reads the Type-C port and the TCPM log.  Here every
absolute path they touch is moved under a temporary directory, the partition
size and the pinned image identities (release v1.2.5's, and revision 3 of the
kit's) are replaced by those of small stand-in images, and id, uname, flock,
findmnt, fuser, dpkg, depmod, blockdev, systemctl, journalctl, modinfo, dmesg,
sync and sleep are stubbed.  The systemctl stub keeps the typec-sink-host
stopgap's state in two files, depmod fails on request, which stops an
install between the module swap and the flash, and sync records what is on
the disk each time it is called, and fails on request.  make-kit.sh needs a
kernel build and is only checked for syntax, for pinning the same identities
as the tablet scripts, for naming patches the build applies, for what its
comparison of two kernel source trees leaves out, and for its checks of the
power domain patch and of the age of the objects the image was linked from.
"""
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

PREFIXES = (
    "/dev/disk/by-partlabel/", "/usr/lib/modules/", "/var/backups", "/var/log/", "/run/",
    "/var/lib/gts9u-boot-sets/", "/proc/device-tree/", "/proc/sys/kernel/",
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
                self.tcpm.write_text(log)
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
        (kernel,) = (self.kit / "logs").glob("*/kernel")
        self.assertIn(f"build ID: {BASE_BUILD_ID} (the kit's: {NEW_BUILD_ID})", kernel.read_text())
        self.assertIn("sink_host_swap: absent", kernel.read_text())
        self.assertIn("tcpm-8-0033/log", kernel.read_text())

    def test_the_recorder_does_not_run_next_to_the_stopgap(self):
        """The daemon writes the data role itself; a trial next to it would
        record the daemon, not the kernel."""
        self.stopgap(active=True)
        for mode in ("dock", "record", "now"):
            with self.subTest(mode):
                result = self.run_script("verify.sh", mode)
                self.assertEqual(result.returncode, 1)
                self.assertIn("sudo systemctl stop typec-sink-host", result.stderr)
                self.assertFalse((self.kit / "logs").exists())
        self.assertEqual(self.tcpm.read_text(), "")

    def test_record_gives_no_verdict_and_passes(self):
        self.plug([(TRY_SNK_LOST_LOG, "host", "source")])
        result = self.run_script("verify.sh", "record")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (row,) = self.results()
        self.assertEqual((row["verdict"], row["data_8s"], row["power_8s"], row["try_snk_lost"]),
                         ("-", "host", "source", "1"))

    def test_now_saves_the_log_without_waiting_for_a_plug(self):
        self.tcpm.write_text(PASS_LOG)
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
        self.assertEqual(self.run_script("verify.sh", "docks").returncode, 2)
        self.assertFalse((self.kit / "logs").exists())

    def boot_kit(self):
        self.running(NEW_BUILD_ID)
        params = self.root / "sys/module/sm5714_usbpd/parameters"
        (params / "sink_host_swap").write_text("Y\n")
        (params / "try_snk_extra_wait_ms").write_text("600\n")
        (self.root / "journal").write_text(
            f"kernel: Loaded X.509 cert 'Build time autogenerated kernel key: {KEY_ID}'\n")
        (self.root / "signer").write_text(SIGNER + "\n")

    def test_checks_pass_on_the_kit_kernel(self):
        self.boot_kit()
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("FAIL", result.stdout)
        self.assertIn("ok   this is an SM-X910", result.stdout)
        self.assertIn("ok   sm5714_usbpd.sink_host_swap = Y", result.stdout)
        self.assertIn("ok   sm5714_usbpd.try_snk_extra_wait_ms = 600", result.stdout)
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
                              "module key, the module failures and the USB host controller were "
                              "not checked\n", result.stdout)
                self.assertEqual(result.stdout.count("FAIL "), 1, result.stdout)
                for claim in ("the kernel trusts", "Loaded X.509", "no module was refused",
                              "symbol failures this boot", "the USB host controller did not die",
                              "the USB host controller died"):
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

    def test_checks_fail_with_modules_of_another_key_or_a_changed_switch(self):
        self.boot_kit()
        (self.root / "signer").write_text(":".join(["6B"] * 20) + "\n")
        (self.root / "sys/module/sm5714_usbpd/parameters/try_snk_extra_wait_ms").write_text("0\n")
        result = self.run_script("verify.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL 2 of 2 modules are not signed with the kit's key", result.stdout)
        self.assertIn("FAIL sm5714_usbpd.try_snk_extra_wait_ms is 0, expected 600", result.stdout)


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
                                 "gcc-sm8550-usb-gdscs-retention.patch"])
        build = (REPO / "scripts/build-mainline-kernel.sh").read_text().replace("\\\n", " ")
        for name in names:
            with self.subTest(name):
                self.assertTrue((REPO / "kernel/patches" / name).is_file())
                self.assertEqual(len(re.findall(
                    rf"^apply_unless '[^']+'\s+\S+\s+{re.escape(name)}\s*$", build, re.M)), 1)
        # Each file a patch changes is one the kit compares with the release's.
        patched = re.search(r"^patched=\((.*?)\)$", text, re.M | re.S).group(1)
        changed = set()
        for name in names:
            changed.update(re.findall(r"^\+\+\+ b/(\S+)", (REPO / "kernel/patches" / name).read_text(),
                                      re.M))
        self.assertEqual(changed, {"drivers/usb/typec/tcpm/tcpm.c", "include/linux/usb/tcpm.h",
                                   "drivers/clk/qcom/gcc-sm8550.c"})
        self.assertEqual(patched, '"$tcpm_dir/tcpm.c" include/linux/usb/tcpm.h "$gcc.c"')
        self.assertIn("\ntcpm_dir=drivers/usb/typec/tcpm\n", text)
        self.assertIn("\ngcc=drivers/clk/qcom/gcc-sm8550\n", text)

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
        """Revision 3's image has the first three strings as well; the
        deadline of the Try.SNK wait is in this revision's patch alone."""
        text = (KIT_SRC / "make-kit.sh").read_text()
        strings = shlex.split(re.search(r"^new_strings=\((.*)\)$", text, re.M).group(1))
        self.assertEqual(strings, ["Requesting DFP before discovery", "sink_host_swap",
                                   "try_snk_extra_wait_ms", "try_snk_wait_until"])
        tcpm_c = "drivers/usb/typec/tcpm/tcpm.c"
        wait = added_lines("tcpm-try-snk-extra-wait.patch")
        # A member of a structure: its name is in the image's type information.
        self.assertIn("\tunsigned long try_snk_wait_until;", wait[tcpm_c])
        self.assertIn("static unsigned int tcpm_try_snk_wait_left(struct tcpm_port *port)",
                      wait[tcpm_c])
        others = [(REPO / "kernel/patches" / name).read_text()
                  for name in ("tcpm-request-dfp-as-sink-before-discovery.patch",
                               "gcc-sm8550-usb-gdscs-retention.patch")]
        others.append((REPO / "kernel/drivers/sm5714_usbpd.c").read_text())
        for name in ("try_snk_wait_until", "tcpm_try_snk_wait_left"):
            with self.subTest(name):
                self.assertFalse([other[:40] for other in others if name in other])
        # The build tree is checked for the same revision of the patch.
        self.assertIn("grep -q 'tcpm_try_snk_wait_left' \"$tree/$tcpm_dir/tcpm.c\"", text)

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
        files = {f"tree/{GCC}.c": 100, f"tree/{TCPM_DIR}/tcpm.c": 100,
                 f"tree/{TCPM_DIR}/sm5714_usbpd.c": 100, "tree/include/linux/usb/tcpm.h": 100,
                 f"build/{GCC}.o": 200, f"build/{TCPM_DIR}/tcpm.o": 200,
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
                 f'tcpm_dir={TCPM_DIR}\ngcc={GCC}\n' + objects.group(0) + "\n" + gate.group(0),
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
        for said in ("until the reboot the running kernel can load no module it has not loaded yet",
                     "while the kit is installed: rollback.sh first, then upgrade "
                     "ubuntu-gts9u-device or ubuntu-gts9u-hardware"):
            self.assertIn(f"say '{said}'", install)


if __name__ == "__main__":
    unittest.main()
