# Type-C roles kit (SM-X910, November 2026)

This kit goes on top of release v1.2.5. It carries the kernel changes for
powered USB-C docks described in
[`docs/usb-c-dock-roles.md`](../../../docs/usb-c-dock-roles.md), so that they
can be tested on a tablet before a release carries them. It is a test
vehicle, not an install or update path: a release kernel is built from
committed code.

**Why.** A powered dock should leave the tablet as power sink and USB host,
also when the tablet boots with the dock attached. On the SM-X910 two docks
did not get there:
- the dock's own data role swap ended in `Data role mismatch` and an endless
  re-attach (every attach of one hub; the other dock has two on record, one
  mismatch and one hard reset);
- a dock seen as a sink first kept the tablet as power source, because
  Try.SNK gives up after 100 ms and the SM5714 reports a source later than
  that: 124 to 147 ms after Rd in the four attaches measured;
- a boot with the dock attached lost its USB host controller in the suspend
  cycle the boot runs for the panel (`HC died`), and plugging the dock in
  again did not bring it back.

**What has run (2026-11-21).** The first revision of this kit, with the two
TCPM changes, was tried with both docks:
- the data role change did what it should on all three attaches where the
  tablet was the power sink: the dock accepted the tablet's request 4 to 9 ms
  after it, and the tablet stayed USB host and charged;
- the longer wait kept the sink role on three attaches of four. The fourth,
  the Satechi plugged into the running tablet, showed its Rp for 46 ms only,
  and the wait gave up there;
- the boot with the dock attached lost its USB host as before.

This revision makes the wait one deadline, so that an Rp which goes away
again sends the port back to waiting, and adds the third change, for the boot
with the dock attached. **Neither of the two has run on a tablet yet.**

**What changes:**
- **Kernel:** the release kernel rebuilt with
  `kernel/patches/tcpm-request-dfp-as-sink-before-discovery.patch`,
  `kernel/patches/tcpm-try-snk-extra-wait.patch`, the `sm5714_usbpd.c` that
  turns both on, and `kernel/patches/gcc-sm8550-usb-gdscs-retention.patch`,
  which keeps the two USB power domains in retention across suspend instead
  of switching them off. In the kernel source tree four files differ from
  the release build: `tcpm.c`, `tcpm.h`, the driver and `gcc-sm8550.c`.
  `make-kit.sh` checks that, and that the configuration, the DTB and the
  fingerprint layers are byte for byte the release build's.
- **boot** holds the new kernel. **vendor_boot is not written**: it carries
  the DTB, which did not change. `install.sh` checks that it still holds the
  release's image.
- **The module tree** comes from this build and is signed with its own key,
  which is archived in the key registry. The patches add fields to
  `tcpc_dev` and `tcpm_port`, and the kernel has no `MODVERSIONS`; a new key
  is what marks a module of another build. **The kernel is locked down and
  refuses a module signed with another build's key** (`Lockdown: ...
  unsigned module loading is restricted` in the kernel log). A kernel with
  the other build's module tree still has what is built in (storage, the
  display, USB with the gadget network, Bluetooth) but not what is built as
  modules: Wi-Fi, NAT, the camera's v4l2loopback and the fingerprint reader's
  modules. That combination has not been booted. Three things follow:
  - between `install.sh` and the reboot the running kernel can load no
    module it has not loaded yet: reboot soon;
  - the tree belongs to the `ubuntu-gts9u-hardware` package, and
    `ubuntu-gts9u-device` owns `updates/qcom_spss_irq.ko` in it. Upgrading
    either while the kit is installed puts modules signed with the release
    key back, which this kernel refuses. Roll back before upgrading;
  - after a restore of boot from TWRP (below) the release kernel runs with
    the kit's modules until `rollback.sh` has run.
- **The `typec-sink-host` stopgap service is disabled.** It asks for the host
  role from userspace; with this kernel the two would collide.
  `rollback.sh` enables it again.
- **Two switches**, both on by default, applied from the next attach:

  | Parameter | Default | Meaning |
  | --- | --- | --- |
  | `/sys/module/sm5714_usbpd/parameters/sink_host_swap` | `Y` | As PD sink, ask for the USB host role before discovery |
  | `/sys/module/sm5714_usbpd/parameters/try_snk_extra_wait_ms` | `600` | Extra wait for a source in Try.SNK; `0` is the standard 100 ms |

  ```sh
  echo N | sudo tee /sys/module/sm5714_usbpd/parameters/sink_host_swap
  echo 0 | sudo tee /sys/module/sm5714_usbpd/parameters/try_snk_extra_wait_ms
  ```

  The power domain change has no switch; `rollback.sh` is the way back.

**What it can change for other things you plug in** (the write-up has the
detail):
- a PD source that offers data role swap and accepts it, a laptop over a
  C-to-C cable for one, becomes the tablet's USB device, where the tablet used
  to be its device;
- a USB stick, keyboard or unpowered hub gets power about 0.6 s later;
- the A-to-C cable to the build host is not PD and should behave as before;
- the USB power domains now stay in retention while the tablet sleeps. What
  that costs in sleep, and whether the USB host survives a real sleep with
  it, has not been measured.

## Build (host)

The kernel inputs are the release's plus the change, so build from the
release's kernel inputs with the change on top, in a new object tree.

```sh
U=/path/to/scratch/build-typec-ultra     # on the external drive, no spaces
git worktree add --detach $U/source <commit>
DEVICE=gts9uwifi UBUNTU_WORKDIR=$HOME/ubuntu-gts9u TMPDIR=$HOME/ubuntu-gts9u/tmp \
  KERNEL_WORKTREE=$U/linux-src-gts9uwifi KERNEL_BUILD_DIR=$U/linux-gts9uwifi \
  KERNEL_OUT_DIR=$HOME/ubuntu-gts9u/out/kernel-gts9uwifi-typec \
  bash $U/source/scripts/build-mainline-kernel.sh
BASE_SCRATCH=/path/to/scratch/build \
  bash tools/inplace/typec-roles-ultra/make-kit.sh $U \
  $HOME/ubuntu-gts9u/out/kernel-gts9uwifi-typec \
  $HOME/ubuntu-gts9u/out/kernel-gts9uwifi \
  $HOME/ubuntu-gts9u/out/bundle \
  $HOME/ubuntu-gts9u/backups/typec-roles-ultra-kit
```

The build archives its module key in the registry
(`scripts/kernel-key-registry.sh`); `make-kit.sh` refuses a build whose key is
not there. Run `make-kit.sh` from the checkout that was built: it compares
the build tree with that checkout's driver and patches. With `BASE_SCRATCH`
it also compares the whole kernel source tree with the release build's
(the objects that the module builds leave in the tree aside), and the
compiled power domain table with the release build's: the two power-state
values and nothing else.
`SOURCE` in the kit records the checkout and which of the two checks ran.

## Install, check, roll back (tablet)

```sh
ssh <user>@<tablet> mkdir -p typec-roles
scp -r ~/ubuntu-gts9u/backups/typec-roles-ultra-kit/* <user>@<tablet>:typec-roles/
sudo sh ~/typec-roles/verify.sh dock 3     # optional: the baseline, before
sudo sh ~/typec-roles/install.sh
sudo reboot
sh ~/typec-roles/verify.sh
sudo sh ~/typec-roles/verify.sh dock 10
sudo sh ~/typec-roles/rollback.sh          # back to the release kernel; reboot
```

The baseline needs the stopgap stopped (`sudo systemctl stop
typec-sink-host`); the recorder refuses to run next to it. If `install.sh`
cannot disable the service it says so and finishes; disable it by hand
before the reboot, and `verify.sh` fails for as long as it is enabled or
running.

`install.sh` saves boot and the module tree in
`/var/backups/ubuntu-gts9u-typec-roles` and logs to
`/var/log/gts9u-typec-roles.log`. It syncs the disk after saving boot,
before writing the new kernel and at the end, so that a power failure finds
either the release or the kit's kernel with its own modules. Reboot soon
after it: until then the running kernel loads no further module.

**Over an earlier revision of the kit** `install.sh` runs as an upgrade; do
not roll back first. Copy the new kit to a directory of its own, or move the
old one aside and keep its `logs`. The release that the first install saved
stays the way back: the upgrade replaces the earlier kit's module tree and
writes boot again, and `rollback.sh` afterwards returns to the release, not
to the earlier revision. If an upgrade stops half-way, run `install.sh`
again, or `rollback.sh`, before any reboot; a second run of `install.sh`
picks up wherever the first one stopped.

`rollback.sh` writes boot and the module tree back first and syncs. If it
stops after that, it says that the release kernel and its modules are back
in place; a second run finishes the rest (the saved boot set, the stopgap
service, retiring the backup).

If the tablet does not come back: TWRP, then

```sh
dd if=/mnt/ubuntu/var/backups/ubuntu-gts9u-typec-roles/boot-release.img \
   of=/dev/block/by-name/boot bs=4M
```

and `rollback.sh` on the next boot. That boot has the release kernel with
the kit's modules, so **Wi-Fi is down until `rollback.sh` has run**: run it
from the tablet's own terminal or over the USB cable to the build host, then
reboot.

## The plug-in recorder

`verify.sh dock [N]` asks for N plug-ins (10 by default). For each it waits
until nothing is attached, empties the TCPM log, asks for the plug, reads the
roles 8 s and 14 s after the attach, and saves the TCPM log, the kernel log of
the trial and the port, battery and USB state under
`~/typec-roles/logs/<time>-dock/`. A plug-in passes when the tablet is **USB
host and power sink at both readings, attached once, with no data role
mismatch**. The roles decide, not the fact that a swap was sent.

`verify.sh record [N]` records the same without a verdict, for the things
that must keep working. `verify.sh now` saves the log as it is, for a boot
with the dock attached. `verify.sh summary LOG...` prints the counters of
saved logs. All of them also run on the kernel before the kit.

What a passing dock looks like in `trial-N.tcpm.log` is in the write-up:
`SNK_READY -> AMS_START`, `Requesting DFP before discovery`, `AMS_START ->
DR_SWAP_SEND -> DR_SWAP_CHANGE_DR -> SNK_READY`, then one discovery. A dock
that answers Wait shows `DR_SWAP_SEND -> DR_SWAP_CANCEL` and the request
again, up to three per attach; the verdict is still the roles.

The summary line of a plug-in says how Try.SNK ended: `Try.SNK kept 1 lost 0`
is the sink role kept, `(Rp came and went 1)` after it an Rp that went away
and the wait that went on, `kept 0 lost 1` a wait that ran out.

`verify.sh` without arguments also reads the boot's kernel log for a USB host
controller that died (`HC died`, a stop endpoint command without an answer,
`Host halt failed`, `Host System Error`, `Controller not ready at resume`),
and fails if it did. After a boot with the dock attached that is the check
for the third change. It needs a suspend cycle to have run: before the
boot's own cycle, about half a minute in, it prints a note and no verdict.
A controller that came back without its state and was set up again (`xHC
error in resume`) is a note too: USB works, the devices were enumerated
again. A module the kernel refused fails the check, and so does a kernel log
that cannot be read.

## Physical checks still to run (owner)

1. After installing and rebooting: `verify.sh`.
2. **A boot with the dock attached**, then `sh ~/typec-roles/verify.sh` and
   `sudo sh ~/typec-roles/verify.sh now`. The keyboard and the hub behind the
   dock work, the battery charges, and `verify.sh` says `the USB host
   controller did not die this boot` with at least one suspend cycle counted.
3. `sudo sh ~/typec-roles/verify.sh dock 10` with each powered dock, plugged
   into the running tablet. Keep the dock's own charger plugged in: without
   it the dock has no power to give and the tablet ends as power source, as
   it should. Note whether the battery charges; near 100 % it reports `Full`
   and no current.
   **The Satechi is the case to watch**: this is where the first revision
   failed, and `(Rp came and went 1)` in its line means this revision did its
   work. If it re-attaches without end,
   `echo 0 | sudo tee /sys/module/sm5714_usbpd/parameters/try_snk_extra_wait_ms`
   gives its old behaviour back; keep the logs. If its log shows
   `SNK_TRY_WAIT -> SRC_TRYWAIT` (the wait ran out), set the parameter to
   `1500` for one plug-in to read how late the source really is. In every
   log, look for `PD TX complete, status: 1` right after the tablet's
   DR_Swap: a request the controller discarded, which still counts as one of
   the three.
4. `sudo sh ~/typec-roles/verify.sh record` once each with: a plain PD
   charger (charges; a swap only if the charger offers one), an unpowered hub
   and a USB stick (enumerate), a DisplayPort monitor or dock (the picture
   comes up), the A-to-C cable to the build host (`usb0` comes up, the gadget
   network works).
5. Suspend and resume with the dock attached, then with the charger. The
   sleep hook still takes the port out of host role around a real sleep, so
   this shows whether the dock comes back afterwards, not whether the host
   controller would survive the sleep on its own. Battery drain over a night
   of sleep, against the release kernel, is the check for what retention
   costs.
6. Bluetooth, Wi-Fi, audio, the fingerprint reader and the cameras: the
   kernel was rebuilt, and nothing else in it changed.
