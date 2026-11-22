# Type-C roles kit (SM-X710, November 2026)

This kit goes on top of the [hang-log kit](../hang-log/README.md). It carries
the kernel changes for powered USB-C docks described in
[`docs/usb-c-dock-roles.md`](../../../docs/usb-c-dock-roles.md), so that they
can be tested on a tablet, and since revision 8 the UFS PHY change described
in [`docs/resume-recovery.md`](../../../docs/resume-recovery.md#sm-x710-gts9wifi)
with a sleep test that cannot damage the storage. Everything the hang-log kit
set stays: the same configuration, the same DTB, the same ramoops region and
sysctl drop-in.

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
same outcome. Revision 7 added the third fix, the SM-X910's power domain
change and a NULL check in the dwc3-qcom glue.

The SM-X710 has the same controller, driver and connector description. What
it showed on 2026-11-21:
- on the kernel before the kit, `verify.sh dock` recorded the Satechi dock
  once: it left the tablet as power source and USB host, with its devices on
  the bus, as on the SM-X910. The Anker hub without its charger left it the
  same way with nothing on the bus: that hub enumerates no device while the
  tablet powers it, on either tablet;
- on revision 6's kernel, booted with the Anker hub attached without its
  charger, the tablet was power source and USB host when the boot's suspend
  cycle ran. When the hub's charger went in, at 718 s, the port attached
  again, the kernel's own request for the host role was accepted, and the
  tablet ended as host and sink on 9 V; but the new USB host controller
  failed to start (`Host halt failed, -110`), so no device came up. In the
  boot before, whose suspend cycle ran in device role, the same hub attached
  without that error. That is the SM-X910's fault, seen once here: the
  reason revision 7 carries the power domain change. 6.6 s after the failed
  start the dwc3-qcom glue oopsed, and the next reboot stalled for 7.5
  minutes: the NULL check is for that;
- on revision 7's kernel (installed at 22:39) everything that was run
  passed: two boots with the dock attached, the charger plugged into the
  running tablet, `verify.sh dock 4` (4 of 4), `verify.sh swap 3` (3 of 3;
  with the kernel's own request switched off, as that run has it, the
  original data role fault showed again) and `verify.sh dock 1` twice;
- one real deep sleep on revision 7 (`sudo rtcwake -m mem -s 30`, 23:34, on
  a powered dock, the interconnect floor released) kept the USB host and
  lost the storage: the UFS PHY did not come back, ext4 aborted its journal
  and the root went read-only. Revision 8 is for that.

**Revision 8** is revision 7 plus two changes to the UFS PHY driver and
nothing else in the kernel:
- `kernel/patches/qmp-ufs-assert-pcs-reset-before-calibration-gts9u.patch`
  now asserts the PCS reset before every calibration of the SM8550 UFS PHY
  on both tablets. Up to revision 7 it did so on the SM-X910 alone (a board
  check in the code), and the SM-X710 never got it. That the missing reset
  caused the 2026-11-21 failure is probable, not proven
  ([`docs/resume-recovery.md`](../../../docs/resume-recovery.md#sm-x710-gts9wifi));
- `kernel/patches/qmp-ufs-pcs-state-diagnostic.patch`, a diagnostic that
  release builds do not get (the build applies it only with
  `APPLY_QMP_UFS_PCS_STATE_DIAGNOSTIC=1`). It reads registers and logs:
  a debug line at every calibration with the state the PHY starts from, and
  after a PCS_READY timeout the state it left and whether PCS_READY came
  within 1 s. It changes no result. If the reset does not fix the sleep, the
  one failed sleep says what to try next (below, "The sleep test").

`sleep-test.sh` is new in revision 8: deep sleeps with every disk
filesystem read-only, so that a resume that loses the storage costs a
restart and nothing else. It also runs on revision 7's kernel, for a
baseline before the install.

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
  them off,
  `kernel/patches/dwc3-qcom-check-hcd-before-reading-port-speed.patch`,
  which lets the USB controller's glue suspend when an xHCI failed to
  start, and the UFS PHY's two (revision 8): nine patches, five of them to
  TCPM. In the kernel source tree six files differ from the hang-log build:
  `tcpm.c`, `tcpm.h`, the driver, `gcc-sm8550.c`, `dwc3-qcom.c` (the
  hang-log build's has the dwc3-qcom CX vote patch already) and
  `phy-qcom-qmp-ufs.c` (the hang-log build's has the SM-X910-only form of
  the PCS reset, which `scripts/stage-ufs-resume-experiment.sh` replaces).
  `make-kit.sh` checks that, and that the configuration and the DTB are byte
  for byte the hang-log build's.
- **boot** holds the new kernel. **vendor_boot is not written**: it carries
  the DTB, which did not change. `install.sh` checks that it still holds the
  hang-log kit's image.
- **The module tree** comes from this build and is signed with the key of
  its build directory, which is archived in the key registry. The hang-log
  kit has another key; revisions 2 to 7 of this kit were built in one
  directory and share theirs, and revision 8 is built in that directory as well. The patches
  add fields to `tcpc_dev` and `tcpm_port`, and the kernel has no
  `MODVERSIONS`; a new key is what marks a module of another build, and
  between two revisions with the same key nothing does. **The kernel is
  locked down and refuses a module signed with another build's key**
  (`Lockdown: ... unsigned module loading is restricted` in the kernel log).
  A kernel with the other build's module tree still has what is built in
  (storage, the display, USB with the gadget network, Wi-Fi, Bluetooth) but
  not what is built as modules: the netfilter modules (NAT, connection
  tracking), the camera's v4l2loopback, netconsole and the secure
  processor's modules. That combination has not been booted. Three things
  follow:
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

  The power domain change, the NULL check and the UFS PHY's PCS reset have
  no switch; `rollback.sh` is the way back.

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
  that costs in sleep and idle has not been measured on this tablet. The
  USB host came through one real sleep with it (2026-11-21, 30 s, on a
  powered dock, devices not enumerated again); **the storage did not**, see
  the sleep test below.

## Build (host)

The kernel inputs are the hang-log build's plus the change, so build from the
commit the hang-log kernel came from with the change on top.

```sh
X=/path/to/scratch/build-typec     # on the external drive, no spaces
git worktree add --detach $X/source <commit>
DEVICE=gts9wifi UBUNTU_WORKDIR=$HOME/ubuntu-gts9u TMPDIR=$HOME/ubuntu-gts9u/tmp \
  APPLY_QMP_UFS_PCS_STATE_DIAGNOSTIC=1 \
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

Without `APPLY_QMP_UFS_PCS_STATE_DIAGNOSTIC=1` the kernel has no
diagnostic and `make-kit.sh` refuses it (its strings are not in the image).
A build tree that carries the diagnostic is refused for a build without the
switch, so a release cannot be built from it by mistake.

The build archives its module key in the registry
(`scripts/kernel-key-registry.sh`); `make-kit.sh` refuses a build whose key is
not there. Run `make-kit.sh` from the checkout that was built: it compares
the build tree with that checkout's driver, patches and stage script. With
`HANG_SCRATCH` it also compares the whole kernel source tree with the
hang-log build's (the objects that the module builds leave in the tree
aside), the compiled power domain table with the hang-log build's (the two
power-state values and nothing else), and checks its test for the UFS PHY's
board gate on the hang-log `vmlinux`, which has the gate. Either way it
checks that the image is the build directory's and was built after the last
change to `tcpm.c`, `tcpm.h`, the driver, `gcc-sm8550.c`, `dwc3-qcom.c` and
`phy-qcom-qmp-ufs.c`, and that `qmp_ufs_phy_calibrate()` in the build's
`vmlinux` (the image's, by its build ID) no longer calls
`of_machine_compatible_match()`: the board check of revision 7 and before.
The NULL check leaves no string in the image: the marker in the build tree's
`dwc3-qcom.c` and the date of its object are what show it.
`SOURCE` in the kit records the checkout, the revision and which of the
checks ran; `REVISION` holds the revision number.

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
revision that is installed already says so and changes nothing. Revision 8
over revision 7 is such an upgrade; the tablet's revision 7, installed by
revision 7's own `install.sh` after a rollback of revision 6, is the state
the tests start the upgrade from.

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

## The sleep test

```sh
sudo sh ~/typec-roles/sleep-test.sh ro 3 30 restart-ok                 # floor as the boot left it
sudo sh ~/typec-roles/sleep-test.sh ro 3 30 release-floor restart-ok   # release it first
sh ~/typec-roles/sleep-test.sh status                                  # the result, no root
```

Each run makes every disk filesystem read-only and **ends with a restart of
the tablet**, whatever happens: `restart-ok` says that is fine, and without
it nothing is done. In order:

1. It refuses, with nothing changed, unless it runs as root on an SM-X710,
   `/sys/power/mem_sleep` offers `deep`, the RTC can wake the tablet,
   userdata is a partition of a writable UFS LUN, the root filesystem is
   writable, the battery is at 30 % or more, the boot has settled (120 s,
   the panel cycle done, the audio DSP started) and every tool it needs is
   there.
2. It records the state before in `~/typec-roles/logs/<time>-sleep/` and in
   `/run/gts9u-sleep-test/<time>/`: the kernel and whether it is the kit's,
   the interconnect floor, `suspend_stats`, `qcom_stats`, the Type-C roles,
   the charger, the mounts and the swap.
3. It copies every tool it needs from then on, the shell and itself, with
   their libraries and the dynamic loader, into a tmpfs, each at its own
   path, and switches off every swap that is not zram (a swap file or
   partition on the disk: the kernel writes a swap file below its
   filesystem, read-only or not). From then on it runs in a mount
   namespace whose root is that tmpfs, in a session of its own: nothing it
   runs comes from the disk, and it goes on to the end when SSH drops.
4. `sync`, `sysrq u`, and a check that every disk filesystem of the system
   is read-only; the UFS disk is then made read-only at the block layer as
   well, so that a write that still came would be logged. Only then does it
   read the first 64 MiB of userdata with `O_DIRECT` and hash them.
5. Each cycle: `rtcwake -m mem -s SECONDS`, then the kernel log since the
   sleep, the same read, `suspend_stats` and `qcom_stats`. A cycle **fails**
   on a UFS failure line (`phy initialization timed-out`, `Failed to
   calibrate PHY`, `Enabling the controller failed`, `Host init failed`,
   `ufshcd_wl_resume failed`), an I/O or ext4 error on the UFS disk, a write
   to the disk that the block layer logged (`Trying to write to read-only
   block-device`: it logs the first one and does not stop it), a read that
   failed or differs, a sleep that did not happen (`rtcwake`,
   `suspend_stats`, no `PM: suspend entry (deep)`).
   The run stops at the first failed cycle. A cycle that passed is **PASS**
   when AOSS sleep, CX collapse and DDR low power (`aosd`, `cxsd`, `ddr`)
   all counted up, **SHALLOW** when one did not (it did not test the depth
   of the 2026-11-21 failure), **DEPTH-UNKNOWN** without `qcom_stats`.
6. The verdicts: one `SLEEP-TEST cycle N/M:` line per cycle, one
   `SLEEP-TEST RESULT:` line, and `NEXT:` lines with the next step. They go
   to the terminal, to `/run/gts9u-sleep-test/<time>/result` (readable
   without root, `latest` points to the newest), to the kernel log and to
   `/dev/pmsg0`, which pstore keeps across the restart.
7. After `hold=S` (300 s unless given), the restart. While the storage reads
   back as before, a clean reboot: SIGINT to systemd, which starts
   `ctrl-alt-del.target` (it checks first that this is `reboot.target`). It
   needs no program from the disk, and the kernel shuts the UFS device down.
   After a storage failure, or if no reboot came after 300 s, `sysrq b`: it
   needs nothing from the disk, and loses nothing, since everything was
   synced and read-only before the first sleep.

**What a pass means.** `SLEEP-TEST RESULT: PASS` with `floor released`: in
each cycle the SoC reached AOSS sleep, CX collapse and DDR low power, the
UFS came back without a single failure line, and 64 MiB read past the page
cache matched the read before the first sleep. It says nothing about writes
after a resume (the disk was read-only); that is the next step, a normal
root (`scripts/diagnostics/suspend/gts9u-suspend-cycles.sh`), and not for
the first runs. A run with the floor held says `floor held` in every verdict:
it does not sleep as deep as the 2026-11-21 failure, which came with the
floor released.

**The interconnect floor.** `ubuntu-gts9u-icc-late-release` releases it
about 45 s into a boot. After a boot that released it and then ended
without a clean shutdown it holds it once (its loop breaker), and it holds
it while `/etc/ubuntu-gts9u/icc-hold` exists. A sleep test always ends that
way for the breaker: its marker can only be cleared by a clean stop with a
writable disk. So the boot after a run in a boot that released the floor
holds it once, and the next run needs `release-floor`, which restarts the
unit, the documented way to release it now (before anything goes
read-only). `release-floor` is refused while `icc-hold` exists; remove that
file yourself if you mean to.

**The result after the restart.** `/run` is gone then. The verdict lines
are in pstore:
`sudo grep -ah 'gts9u sleep-test' -r /var/lib/ubuntu-gts9u/pstore /sys/fs/pstore | tail -n 60`
(`sleep-test.sh status` prints that command when there is no run in
`/run`). If SSH drops during the sleep and comes back before the restart,
`sh ~/typec-roles/sleep-test.sh status` or, if the storage is gone and no
program starts,
`while IFS= read -r l; do echo "$l"; done < /run/gts9u-sleep-test/latest/result`
(built into the shell).

**Reading the diagnostic** (revision 8's kernel only). After each resume the
run prints the diagnostic's lines of the cycle. `calibration entry:` shows
the PCS and SerDes state the calibration started from. What follows is
inferred from the registers, not proven. `SW_RESET 0 START 0x1` after a
resume is what the last calibration left: the PCS kept its registers
through the sleep, the case the reset is for. If a cycle fails with `phy
initialization timed-out`, the lines after it say what to try next:
- `PCS_READY ... us after the timeout, within 1 s of SERDES_START`: the PHY
  was only slow; give the poll the Kalama driver's 1 s;
- `no PCS_READY within 1 s` with `C_READY 0x1` (the SerDes PLL ready): the
  PCS is stuck despite its reset; next, the external `ufsphy` reset that the
  Kalama driver asserts around calibration;
- `no PCS_READY within 1 s` with `C_READY 0`: the PLL never became ready;
  next, the PHY's analog supply (L1D, which the stock device tree gives the
  PHY and this one does not describe) or its reference clock.

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
when the run ends, also on Ctrl-C. If the run is killed outright the
switch stays off until the next boot, or until
`echo Y | sudo tee /sys/module/sm5714_usbpd/parameters/sink_host_swap`.

Without an argument `verify.sh` checks the boot: the kit's kernel (and its
revision) and module key, the two switches, and what the hang-log kit set. A
module the kernel refused fails it, and so does a kernel log that cannot be
read. It also reads the boot's kernel log for a USB host controller that
died (`HC died`, `not responding to stop endpoint command`, `Host halt
failed`, `Host System Error`, `Controller not ready at resume`), and fails if
it did. After a boot with the dock attached that is the check for the power
domain change. It needs a suspend cycle to have run: before the boot's own
cycle, about half a minute in, it prints a note and no verdict. And it needs
devices on the bus: with nothing behind the dock a controller that lost its
state can go unseen until the next attach, as on 2026-11-21. A controller
that came back from a suspend without its state and was set up again (`xHC
error in resume`) is a note: USB works, the devices were enumerated again.
For the UFS PHY it fails on a calibration or UFS host failure this boot, and
otherwise counts the diagnostic's `calibration entry` lines: on the kit's
kernel that line, logged from inside the calibration that now resets the
PCS on both tablets, is what can be seen of revision 8 from userspace (the
reset itself logs nothing; `make-kit.sh` checked the code on the build). On
another kernel it notes that nothing tells.

What a passing dock looks like in `trial-N.tcpm.log` is in the write-up:
`SNK_READY -> AMS_START`, `Requesting DFP before discovery`, `AMS_START ->
DR_SWAP_SEND -> DR_SWAP_CHANGE_DR -> SNK_READY`, then one discovery. A dock
that answers Wait shows `DR_SWAP_SEND -> DR_SWAP_CANCEL` and the request
again, up to three per attach; the verdict is still the roles.

## Physical checks still to run (owner)

1. After installing and rebooting: `verify.sh`. On revision 8 it should
   count at least one `calibration entry` and no UFS failure.
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
6. **The sleep test**, in this order, each run in its own boot (the run
   restarts the tablet): on revision 7, before the install, a baseline
   (`release-floor` if the floor is held): does revision 7 fail every time
   or one time in N? Then install revision 8, reboot, `verify.sh`, and the
   same run docked, then undocked. Only after `PASS` with `floor released`,
   docked and undocked, does a sleep with a writable root follow. Battery
   drain over a night of sleep, against the hang-log kernel, is the check
   for what the USB power domains' retention costs, and comes after that.
7. Bluetooth, Wi-Fi, audio and the rear camera at a known size: the kernel was
   rebuilt, and nothing else in it changed.
