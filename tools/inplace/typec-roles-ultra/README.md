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
  that: 123 to 147 ms after Rd in the eight attaches measured;
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

The second revision made the wait one deadline, so that an Rp which goes
away again sends the port back to waiting, and added the third change, for
the boot with the dock attached. It ran the same day: two boots with the
Satechi attached and four plug-ins into the running tablet, two with each
dock. All six ended with the tablet as USB host and power sink, and both
boots kept the USB host controller through the boot's suspend cycle. One of
the two Satechi plug-ins took the path the first revision lost: the Rp went
away after 47 ms and was back 407 ms after Rd, with 528 ms of the wait left.

The revision before this one added two fixes for a data role swap asked for
from userspace (a write to the port's `data_role`) and a driver that reads
its two switches at each attach. One boot with the Satechi attached has run
on it and ended as the two before it did, the Rp going away once on the
way. This revision adds a third fix of that kind and a NULL check in the
dwc3-qcom glue, for what the SM-X710 showed in a boot with a dock attached
(the write-up has it); neither is in a kernel that has run on a tablet.
**None of the three fixes and neither switch has been tried on a tablet
yet**, and neither has a real sleep with the dock attached.

**What changes:**
- **Kernel:** the release kernel rebuilt with
  `kernel/patches/tcpm-request-dfp-as-sink-before-discovery.patch`,
  `kernel/patches/tcpm-try-snk-extra-wait.patch`, the `sm5714_usbpd.c` that
  turns both on, `kernel/patches/gcc-sm8550-usb-gdscs-retention.patch`,
  which keeps the two USB power domains in retention across suspend instead
  of switching them off, and the three fixes for a swap asked for from
  userspace, `kernel/patches/tcpm-no-dr-swap-during-vdm.patch`,
  `kernel/patches/tcpm-dr-set-checks-data-role.patch` and
  `kernel/patches/tcpm-keep-queued-swap-pending.patch`, and
  `kernel/patches/dwc3-qcom-check-hcd-before-reading-port-speed.patch`,
  which lets the USB controller's glue suspend when an xHCI failed to
  start: seven patches, five of them to TCPM. In the kernel source tree five
  files differ from the release build: `tcpm.c`, `tcpm.h`, the driver,
  `gcc-sm8550.c` and `dwc3-qcom.c`. A kit built from this tree since
  2026-11-22 has a sixth, `phy-qcom-qmp-ufs.c`: the build replaces the
  release's SM-X910-only PCS reset of the UFS PHY with the same write for
  every SM8550 PHY, which changes nothing at run time on this tablet
  ([`docs/resume-recovery.md`](../../../docs/resume-recovery.md#sm-x710-gts9wifi)).
  `make-kit.sh` checks all of that, that the UFS PHY's calibration in the
  new `vmlinux` no longer asks which board it runs on, and that the
  configuration, the DTB and the fingerprint layers are byte for byte the
  release build's.
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
sudo sh ~/typec-roles/verify.sh swap 3
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
through the port's `data_role`, as the stopgap service did: from the moment
the partner shows, which is when the kernel starts its discovery, up to
twenty writes 5 ms apart and five more 300 ms apart, until one is accepted. A
plug-in passes when a swap was asked for, **no `DR_SWAP_SEND` was cut short
by a Soft Reset** (what a swap sent into a discovery ends in), **no write was
told "done" with the role unchanged**, and the kernel made no request of its
own. The roles at the end do not decide here: with the kernel's request off,
the dock's own swap can still end them as it used to.
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
And it needs devices on the bus: with nothing behind the dock a controller
that lost its state can go unseen until the next attach, as on the SM-X710
on 2026-11-21.
A controller that came back without its state and was set up again (`xHC
error in resume`) is a note too: USB works, the devices were enumerated
again. A module the kernel refused fails the check, and so does a kernel log
that cannot be read.

## Physical checks still to run (owner)

Checks 1 to 3 passed with the second revision on 2026-11-21 (two boots with
the dock attached, four plug-ins); this revision's kernel is a new build, so
they are worth one more run. Check 4 has not been run on any revision.

1. After installing and rebooting: `verify.sh`.
2. **A boot with the dock attached and devices behind it**, then
   `sh ~/typec-roles/verify.sh` and `sudo sh ~/typec-roles/verify.sh now`.
   The keyboard and the hub behind the dock work, the battery charges, and
   `verify.sh` says `the USB host controller did not die this boot` with at
   least one suspend cycle counted. A boot with nothing on the bus does not
   test the host controller.
3. `sudo sh ~/typec-roles/verify.sh dock 10` with each powered dock, plugged
   into the running tablet. Keep the dock's own charger plugged in: without
   it the dock has no power to give and the tablet ends as power source, as
   it should. Note whether the battery charges; near 100 % it reports `Full`
   and no current.
   **The Satechi is the case to watch**: this is where the first revision
   failed, and `(Rp came and went 1)` in its line means the deadline did its
   work. If it re-attaches without end,
   `echo 0 | sudo tee /sys/module/sm5714_usbpd/parameters/try_snk_extra_wait_ms`
   gives its old behaviour back; keep the logs. If its log shows
   `SNK_TRY_WAIT -> SRC_TRYWAIT` (the wait ran out), set the parameter to
   `1500` for one plug-in to read how late the source really is. In every
   log, look for `PD TX complete, status: 1` right after the tablet's
   DR_Swap: a request the controller discarded, which still counts as one of
   the three.
4. `sudo sh ~/typec-roles/verify.sh swap 3` with a powered dock, started
   with nothing plugged in. Each plug-in should pass, and at least one line
   should count a write `refused during a VDM exchange`. Afterwards
   `cat /sys/module/sm5714_usbpd/parameters/sink_host_swap` says `Y` again,
   and one `sudo sh ~/typec-roles/verify.sh dock 1` passes with `host role
   requested 1`: the kernel asks by itself again from the next attach.
5. `sudo sh ~/typec-roles/verify.sh record` once each with: a plain PD
   charger (charges; a swap only if the charger offers one), an unpowered hub
   (not the Anker hub: without its charger it enumerates nothing) and a USB
   stick (enumerate), a DisplayPort monitor or dock (the picture
   comes up), the A-to-C cable to the build host (`usb0` comes up, the gadget
   network works).
6. Suspend and resume with the dock attached, then with the charger. The
   sleep hook still takes the port out of host role around a real sleep, so
   this shows whether the dock comes back afterwards, not whether the host
   controller would survive the sleep on its own. Battery drain over a night
   of sleep, against the release kernel, is the check for what retention
   costs.
7. Bluetooth, Wi-Fi, audio, the fingerprint reader and the cameras: the
   kernel was rebuilt, and nothing else in it changed.
