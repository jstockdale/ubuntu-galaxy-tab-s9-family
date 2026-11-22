# Type-C roles kit (SM-X710, November 2026)

This kit goes on top of the [hang-log kit](../hang-log/README.md). It carries
the kernel changes for powered USB-C docks described in
[`docs/usb-c-dock-roles.md`](../../../docs/usb-c-dock-roles.md), so that they
can be tested on a tablet. Everything the hang-log kit set stays: the same
configuration, the same DTB, the same ramoops region and sysctl drop-in.

**Why.** A powered dock should leave the tablet as power sink and USB host,
also when the tablet boots with the dock attached. On the SM-X910 two docks
did not get there:
- the dock's own data role swap ended in `Data role mismatch` and an endless
  re-attach (every attach of one hub; the other dock has two on record, one
  mismatch and one hard reset);
- a dock seen as a sink first kept the tablet as power source, because
  Try.SNK gives up after 100 ms and the SM5714 reports a source later than
  that: 123 to 147 ms after Rd in the eight attaches measured;
- a boot with the dock attached lost its USB host controller in the suspend
  cycle the boot runs for the panel (`HC died`). On the SM-X710 the next
  xHCI failed to start after such a boot, once (below).

**On the SM-X910 (2026-11-21)** a test kernel with both changes was tried
with two docks. The data role change did what it should on all three
attaches where the tablet was the power sink: the dock accepted the tablet's
request 4 to 9 ms after it, and the tablet stayed USB host and charged. The
longer wait kept the sink role on three attaches of four; the fourth, one
dock plugged into the running tablet, showed its Rp for 46 ms only, and that
first revision of the wait gave up there. The second revision, one deadline
for the whole wait, which goes back to waiting when an Rp goes away again,
ran there the same day: two boots with a dock attached and four plug-ins
into the running tablet, all six with the tablet as USB host and power sink
at the end, one of them through the case the first revision lost. This kit's
kernel carries that second revision, three fixes for a data role swap asked
for from userspace (a write to the port's `data_role`) and a driver that
reads its two switches at each attach. The SM-X910 has booted once with a
dock attached on a kernel with the first two fixes and the driver, with the
same outcome. New in this revision are the third fix, the SM-X910's power
domain change and a NULL check in the dwc3-qcom glue. The third fix and the
NULL check are in no kernel that has run on a tablet, and the power domain
change has run on the SM-X910 only. None of the three fixes and neither
switch has been tried on a tablet. **On the SM-X710 the previous revision
of the kit is installed, without the third fix and the power domain
change.**

The SM-X710 has the same controller, driver and connector description. What
it showed on 2026-11-21:
- on the kernel before the kit, `verify.sh dock` recorded the Satechi dock
  once: it left the tablet as power source and USB host, with its devices on
  the bus, as on the SM-X910. The Anker hub without its charger left it the
  same way with nothing on the bus: that hub enumerates no device while the
  tablet powers it, on either tablet;
- on the previous revision's kernel, booted with the Anker hub attached
  without its charger, the tablet was power source and USB host when the
  boot's suspend cycle ran. When the hub's charger went in, at 718 s, the
  port attached again, the kernel's own request for the host role was
  accepted, and the tablet ended as host and sink on 9 V; but the new USB
  host controller failed to start (`Host halt failed, -110`), so no device
  came up. In the boot before, whose suspend cycle ran in device role, the
  same hub attached without that error. That is the SM-X910's fault, seen
  once here: the reason this revision carries the power domain change.
  6.6 s after the failed start the dwc3-qcom glue oopsed, and the next
  reboot stalled for 7.5 minutes: the NULL check is for that;
- not done yet: `verify.sh dock` and `swap` on a kit kernel, the Satechi on
  a kit kernel, and a boot with the dock attached and devices on the bus.

The data role fault has not been seen on it.

**What changes:**
- **Kernel:** the hang-log kernel rebuilt with
  `kernel/patches/tcpm-request-dfp-as-sink-before-discovery.patch`,
  `kernel/patches/tcpm-try-snk-extra-wait.patch`, the `sm5714_usbpd.c` that
  turns both on, the three fixes for a swap asked for from userspace,
  `kernel/patches/tcpm-no-dr-swap-during-vdm.patch`,
  `kernel/patches/tcpm-dr-set-checks-data-role.patch` and
  `kernel/patches/tcpm-keep-queued-swap-pending.patch`,
  `kernel/patches/gcc-sm8550-usb-gdscs-retention.patch`, which keeps the
  two USB power domains in retention across suspend instead of switching
  them off, and
  `kernel/patches/dwc3-qcom-check-hcd-before-reading-port-speed.patch`,
  which lets the USB controller's glue suspend when an xHCI failed to
  start: seven patches, five of them to TCPM. In the kernel source tree
  five files differ from the hang-log build: `tcpm.c`, `tcpm.h`, the
  driver, `gcc-sm8550.c` and `dwc3-qcom.c` (the hang-log build's has the
  dwc3-qcom CX vote patch already). `make-kit.sh` checks that, and that the
  configuration and the DTB are byte for byte the hang-log build's.
- **boot** holds the new kernel. **vendor_boot is not written**: it carries
  the DTB, which did not change. `install.sh` checks that it still holds the
  hang-log kit's image.
- **The module tree** comes from this build and is signed with its own key,
  which is archived in the key registry. The patches add fields to
  `tcpc_dev` and `tcpm_port`, and the kernel has no `MODVERSIONS`; a new key
  is what marks a module of another build. **The kernel is locked down and
  refuses a module signed with another build's key** (`Lockdown: ...
  unsigned module loading is restricted` in the kernel log). A kernel with
  the other build's module tree still has what is built in (storage, the
  display, USB with the gadget network, Wi-Fi, Bluetooth) but not what is
  built as modules: the netfilter modules (NAT, connection tracking), the
  camera's v4l2loopback, netconsole and the secure processor's modules. That
  combination has not been booted. Three things follow:
  - between `install.sh` and the reboot the running kernel can load no
    module it has not loaded yet: reboot soon;
  - `ubuntu-gts9u-device` owns `updates/qcom_spss_irq.ko` in the tree.
    Upgrading it while the kit is installed puts a module signed for another
    kernel there, which this kernel refuses. The hang-log kit has a key of
    its own too, so rolling back to it does not change that;
  - after a restore of boot from TWRP (below) the hang-log kernel runs with
    this kit's modules until `rollback.sh` has run.
- **Two switches**, both on by default, applied from the next attach:

  | Parameter | Default | Meaning |
  | --- | --- | --- |
  | `/sys/module/sm5714_usbpd/parameters/sink_host_swap` | `Y` | As PD sink, ask for the USB host role before discovery |
  | `/sys/module/sm5714_usbpd/parameters/try_snk_extra_wait_ms` | `600` | Extra wait for a source in Try.SNK; `0` is the standard 100 ms |

  ```sh
  echo N | sudo tee /sys/module/sm5714_usbpd/parameters/sink_host_swap
  echo 0 | sudo tee /sys/module/sm5714_usbpd/parameters/try_snk_extra_wait_ms
  ```

  The power domain change and the NULL check have no switch; `rollback.sh`
  is the way back.

**What it can change for other things you plug in** (the write-up has the
detail):
- a PD source that offers data role swap and accepts it, a laptop over a
  C-to-C cable for one, becomes the tablet's USB device, where the tablet used
  to be its device;
- a USB stick, keyboard or unpowered hub gets power about 0.6 s later;
- the A-to-C cable to the build host is not PD and should behave as before;
- a program that writes the port's `data_role` itself now gets `EAGAIN`
  (`Resource temporarily unavailable`) while a VDM exchange is under way,
  from the moment TCPM queues a VDM (discovery's, for one) until that
  exchange is over, and when the swap ended with the role unchanged, where
  it used to be told that the swap was done. Writing again is the answer to
  both. A write to `data_role`, `power_role` or `vconn_source` that waits
  behind a message of the partner's is answered once its swap has run, no
  longer before it is sent;
- the USB power domains now stay in retention while the tablet sleeps. What
  that costs in sleep and idle, and whether the USB host survives a real
  sleep with it, has not been measured on this tablet.

## Build (host)

The kernel inputs are the hang-log build's plus the change, so build from the
commit the hang-log kernel came from with the change on top.

```sh
X=/path/to/scratch/build-typec     # on the external drive, no spaces
git worktree add --detach $X/source <commit>
DEVICE=gts9wifi UBUNTU_WORKDIR=$HOME/ubuntu-gts9u TMPDIR=$HOME/ubuntu-gts9u/tmp \
  KERNEL_WORKTREE=$X/linux-src-gts9wifi KERNEL_BUILD_DIR=$X/linux-gts9wifi \
  KERNEL_OUT_DIR=$HOME/ubuntu-gts9u/out/kernel-gts9wifi-typec \
  bash $X/source/scripts/build-mainline-kernel.sh
HANG_SCRATCH=/path/to/scratch/build-hang \
  bash tools/inplace/typec-roles/make-kit.sh $X \
  $HOME/ubuntu-gts9u/out/kernel-gts9wifi-typec \
  $HOME/ubuntu-gts9u/out/kernel-gts9wifi-hang \
  $HOME/ubuntu-gts9u/backups/hang-log-kit-r2 \
  $HOME/ubuntu-gts9u/backups/typec-roles-kit
```

The build archives its module key in the registry
(`scripts/kernel-key-registry.sh`); `make-kit.sh` refuses a build whose key is
not there. Run `make-kit.sh` from the checkout that was built: it compares
the build tree with that checkout's driver and patches. With `HANG_SCRATCH`
it also compares the whole kernel source tree with the hang-log build's
(the objects that the module builds leave in the tree aside), and the
compiled power domain table with the hang-log build's: the two power-state
values and nothing else. Either way it checks that the image is the build
directory's and was built after the last change to `tcpm.c`, `tcpm.h`, the
driver, `gcc-sm8550.c` and `dwc3-qcom.c`. The NULL check leaves no string in
the image: the marker in the build tree's `dwc3-qcom.c` and the date of its
object are what show it.
`SOURCE` in the kit records the checkout and which of the two checks ran.

## Install, check, roll back (tablet)

```sh
ssh <user>@<tablet> mkdir -p typec-roles
scp ~/ubuntu-gts9u/backups/typec-roles-kit/* <user>@<tablet>:typec-roles/
sudo sh ~/typec-roles/verify.sh dock 3     # optional: the baseline, before
sudo sh ~/typec-roles/install.sh
sudo reboot
sh ~/typec-roles/verify.sh
sudo sh ~/typec-roles/verify.sh dock 10
sudo sh ~/typec-roles/verify.sh swap 3
sudo sh ~/typec-roles/rollback.sh          # back to the hang-log kit; reboot
```

`install.sh` saves boot and the module tree in
`/var/backups/ubuntu-gts9u-typec-roles` and logs to
`/var/log/gts9u-typec-roles.log`. It syncs the disk after saving boot,
before writing the new kernel and at the end, so that a power failure finds
either the hang-log or the kit's kernel with its own modules. Reboot soon
after it: until then the running kernel loads no further module. A first
install that stopped is not finished by a second run: `rollback.sh` undoes
it, then `install.sh` runs again. The hang-log kit's own `verify.sh` reports
another build ID and module key while this kit is installed; its other
checks still apply.

**Over an earlier revision of this kit** `install.sh` runs as an upgrade: one
command, then one reboot, and no `rollback.sh` first. Copy the new kit over
the old one in `~/typec-roles` as above (the `logs` of earlier runs stay),
run `install.sh` and reboot. The hang-log kit that the first install saved
stays the way back: the upgrade checks that it is whole, replaces the earlier
kit's module tree and writes boot again, and vendor_boot keeps the hang-log
kit's image. `rollback.sh` returns to the hang-log kit, not to the earlier
revision, after any number of upgrades. Boot has to hold the hang-log kit
(after a restore from TWRP) or a revision of this kit, and the running kernel
has to be one of theirs: revisions 1 to 7 are known by their boot images and
kernels, and every later install or upgrade adds a line to `revisions` in the
backup before it writes boot. Anything else is refused with nothing written,
and the message names the image or the build ID it found. Reboot soon after
an upgrade too: until then the module tree in place is built for the new
kernel, not for the running one. If an upgrade stops half-way, run
`install.sh` again, or `rollback.sh`, before any reboot; a second run of
`install.sh` picks up wherever the first one stopped. `install.sh` of the
revision that is installed already says so and changes nothing.

`rollback.sh` writes boot and the module tree back first and syncs. If it
stops after that, it says that the hang-log kernel and its modules are back
in place; a second run finishes the rest (the saved boot set, retiring the
backup).

If the tablet does not come back: TWRP, then

```sh
dd if=/mnt/ubuntu/var/backups/ubuntu-gts9u-typec-roles/boot-hang-log.img \
   of=/dev/block/by-name/boot bs=4M
```

and `rollback.sh` on the next boot. That boot has the hang-log kernel with
this kit's modules, which it refuses: **no NAT, no v4l2loopback and no
netconsole until `rollback.sh` has run**. Wi-Fi, USB networking and
Bluetooth are built in, so it can be run over SSH; then reboot.

## The plug-in recorder

`verify.sh dock [N]` asks for N plug-ins (10 by default). For each it waits
until nothing is attached, empties the TCPM log, asks for the plug, reads the
roles 8 s and 14 s after the attach, and saves the TCPM log, the kernel log of
the trial and the port, battery and USB state under
`~/typec-roles/logs/<time>-dock/`. A plug-in passes when the tablet is **USB
host and power sink at both readings, attached once, with no data role
mismatch**. The roles decide, not the fact that a swap was sent.

The TCPM log holds 1023 lines; once it is full, every line that comes
replaces its last one with `overflow`, until the log is read, which empties
it. A dock that re-attaches in a loop fills it in seconds, so during a
plug-in the script **reads the log every second** and adds what it read to
the trial's `trial-N.tcpm.log`. A log that overflows all the same has lost
lines between two reads: in `dock` that plug-in fails, since its one attach
cannot be shown, and in `swap` it gets no verdict and the run fails.

`verify.sh record [N]` records the same without a verdict, for the things
that must keep working. `verify.sh now` saves the log as it is, for a boot
with the dock attached. `verify.sh summary LOG...` prints the counters of
saved logs. All of them also run on the kernel before the kit.

`verify.sh swap [N]` (1 by default) tests the fixes for a data role swap
asked for from userspace. For the run it switches the kernel's own request
off (`sink_host_swap`), and at each plug-in a writer asks for the host role
through the port's `data_role`, as a helper in userspace would: from the
moment the partner shows, which is when the kernel starts its discovery, up
to twenty writes 5 ms apart and five more 300 ms apart, until one is
accepted. A plug-in passes when a swap was asked for, **no `DR_SWAP_SEND` was
cut short by a Soft Reset** (what a swap sent into a discovery ends in), **no
write was told "done" with the role unchanged**, and the kernel made no
request of its own. The roles at the end do not decide here: with the
kernel's request off, the dock's own swap can still end them as it used to.
`N refused during a VDM exchange` in a plug-in's line counts the writes the
first fix turned away (`VDM in progress, DATA_ROLE_SWAP not started` in the
TCPM log); a run in which no write met a VDM exchange says so and asks for
another plug-in. The third fix's lines are counted under the plug-in when
they show (`left pending`, and `dropped because the partner swapped first`
for a queued swap of any kind); they do not decide, and the run cannot bring
about the race they come from. A
plug-in without a verdict says why: no swap asked for, every write refused
before TCPM started one, or a log that lost lines. A run in which no plug-in
got a verdict fails. Start the run with nothing plugged in: the first plug-in
then also shows that the driver read the switch at the attach. On a kernel
without the fixes the same run should show the fault, the write sent into the
discovery and cut short; that has not been run either. The switch is put back
when the run ends, also on Ctrl-C. If the run is killed outright the switch
stays off until the next boot, or until
`echo Y | sudo tee /sys/module/sm5714_usbpd/parameters/sink_host_swap`.

Without an argument `verify.sh` checks the boot: the kit's kernel and module
key, the two switches, and what the hang-log kit set. A module the kernel
refused fails it, and so does a kernel log that cannot be read. It also reads
the boot's kernel log for a USB host controller that died (`HC died`, `not
responding to stop endpoint command`, `Host halt failed`, `Host System
Error`, `Controller not ready at resume`), and fails if it did. After a boot
with the dock attached that is the check for the power domain change. It
needs a suspend cycle to have run: before the boot's own cycle, about half a
minute in, it prints a note and no verdict. And it needs devices on the bus:
with nothing behind the dock a controller that lost its state can go unseen
until the next attach, as on 2026-11-21. A controller that came back from a
suspend without its state and was set up again (`xHC error in resume`) is a
note: USB works, the devices were enumerated again.

What a passing dock looks like in `trial-N.tcpm.log` is in the write-up:
`SNK_READY -> AMS_START`, `Requesting DFP before discovery`, `AMS_START ->
DR_SWAP_SEND -> DR_SWAP_CHANGE_DR -> SNK_READY`, then one discovery. A dock
that answers Wait shows `DR_SWAP_SEND -> DR_SWAP_CANCEL` and the request
again, up to three per attach; the verdict is still the roles.

## Physical checks still to run (owner)

1. After installing and rebooting: `verify.sh`.
2. `sudo sh ~/typec-roles/verify.sh dock 10` with each powered dock. Note
   whether the battery charges; near 100 % it reports `Full` and no current.
   Keep a dock's own charger plugged in: without it the dock has no power
   to give and the tablet ends as power source, as it should.
   Watch a dock that can also be powered from its host (the Satechi): on the
   SM-X910 it attached with the tablet as power source when plugged into the
   running tablet, stable but not charging. A line `Try.SNK kept 1 lost 0
   (Rp came and went 1)` means the second revision of the wait did its work.
   If the dock re-attaches without end,
   `echo 0 | sudo tee /sys/module/sm5714_usbpd/parameters/try_snk_extra_wait_ms`
   gives its old behaviour back; keep the logs. If its log shows
   `SNK_TRY_WAIT -> SRC_TRYWAIT` (the wait ran out), set the parameter to
   `1500` for one plug-in to read how late the source really is.
3. `sudo sh ~/typec-roles/verify.sh swap 3` with a powered dock, started
   with nothing plugged in. Each plug-in should pass, and at least one line
   should count a write `refused during a VDM exchange`. Afterwards
   `cat /sys/module/sm5714_usbpd/parameters/sink_host_swap` says `Y` again,
   and one `sudo sh ~/typec-roles/verify.sh dock 1` passes with `host role
   requested 1`: the kernel asks by itself again from the next attach.
4. **A boot with the dock attached and devices behind it**: the Anker hub
   with its charger in its PD port and something plugged into the hub, or
   the Satechi with its power supply. Then `sh ~/typec-roles/verify.sh` and
   `sudo sh ~/typec-roles/verify.sh now`. The devices behind the dock work,
   the battery charges, and `verify.sh` says `the USB host controller did
   not die this boot` with at least one suspend cycle counted. A boot with
   nothing on the bus does not test the host controller.
5. `sudo sh ~/typec-roles/verify.sh record` once each with: a plain PD
   charger (charges; a swap only if the charger offers one), an unpowered hub
   (not the Anker hub: without its charger it enumerates nothing) and a USB
   stick (enumerate), the A-to-C cable to the build host (`usb0` comes up,
   the gadget network works).
6. Suspend and resume with the dock attached, then with the charger: does
   the dock come back afterwards? Battery drain over a night of sleep,
   against the hang-log kernel, is the check for what retention costs.
7. Bluetooth, Wi-Fi, audio and the rear camera at a known size: the kernel was
   rebuilt, and nothing else in it changed.
