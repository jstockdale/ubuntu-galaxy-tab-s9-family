# Type-C roles kit (SM-X910, November 2026)

This kit goes on top of release v1.2.5. It carries the kernel changes for
powered USB-C docks described in
[`docs/usb-c-dock-roles.md`](../../../docs/usb-c-dock-roles.md), so that they
can be tested on a tablet before a release carries them, since
revision 8 (below, "Revision 8") what the SM-X710 sleeps deeply with, and
since revision 9 (below, "Revision 9") the fix for the GPU's one-second
freezes and the swap back to sink for a dock that the tablet has to power.
It is a test vehicle, not an install or update path: a release kernel is
built from committed code.

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
  new `vmlinux` no longer asks which board it runs on, and that the DTB and
  the fingerprint layers are byte for byte the release build's, and up to
  revision 7 the configuration too (revision 8 changes four lines of it,
  below).
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

**Revision 8 (2026-11-25): the deep-sleep groundwork. Written and checked on
the host; no kernel has been built for it, no kit packed, nothing of it has
run on an SM-X910.** It brings this board what the SM-X710's kit revision 14
reached its first whole-SoC deep sleeps with (2026-11-24 and 25,
[`docs/resume-recovery.md`](../../../docs/resume-recovery.md#deep-sleep-on-the-sm-x710-2026-11-24-and-25)).
The kernel is this checkout's `gts9uwifi` build, which since 2026-11-25 has
the first five items below in every build, built with the SM-X710 kit's
switches (below, "Build"); the DTB, vendor_boot, the fingerprint layers and
the module list stay the release's:
- **The configuration**, the release's with four lines changed (`make-kit.sh`
  allows these and nothing else, `config_changes`):
  `CONFIG_SM_VIDEOCC_8550=y` (the video clock controller binds, so rpmhpd and
  gcc can reach `sync_state()` at boot), `CONFIG_QCOM_STATS=y` (the sleep
  counts in `/sys/kernel/debug/qcom_stats`), `CONFIG_PSTORE_FTRACE=y` and no
  `CONFIG_PSTORE_COMPRESS` (the SM-X710's pstore lines, with ramoops below).
  The DDR bandwidth monitors stay without a driver, as on the SM-X710 (there
  the module is shipped and blacklisted): the interconnect keeps its boot vote
  until a sleep test releases it (`release-floor`). Released at boot it hung
  the SM-X710's boot cycle.
- **The early helper** (`kernel/drivers/gts9u-performance.c`): L15B held in
  HPM (its line `gts9u-performance: L15B held in HPM: 2 of its DTB properties
  dropped`), and two more overlays, the SM-X710's board DTS changes this
  board's pinned DTB cannot carry: `kernel/dts/gts9u-sync-state.dtso` (gcc
  under CX, the GMU's post-init providers, the crypto engine disabled; its
  line `gts9u-performance: SoC hand-off: gcc under CX, GMU post-init
  providers, crypto engine disabled`) and `kernel/dts/gts9u-ramoops.dtso`
  (ramoops on Samsung's pmsg carve-out, the SM-X710's layout, ECC 96; its line
  `gts9u-performance: ramoops in the pmsg carve-out at 0x880900000`).
- The UFS and USB CX votes dropped in system sleep
  (`ufshcd-drop-opp-votes-in-system-suspend.patch` and
  `dwc3-qcom-drop-required-opps-vote-in-system-suspend.patch`, in
  `kernel/patches/`), on both boards since gcc is under CX here too.
- ufs-qcom's UFS PHY power reference fix (`ufs-qcom-track-phy-power-state-gts9u.patch`,
  every build since 2026-11-23): without it the SM-X710 lost the storage at a
  resume.
- The SM-X710 kit's sleep patches, the same files and switches: `pcie_0_gdsc`
  collapsed in a sleep in which the PCIe host powered down, with its guard,
  and the root port's debug switch (`root-port-d3` of the sleep test); the
  three SM8550 sleep fixes (the PIPE clock mux revert, so without
  `unpark-pcie0-pipe-mux.patch`, the GPU RSC's stale votes, ufs-qcom's lane
  clocks); the three XO fixes (the geni UARTs force-suspended, the DSI PHY's
  AHB clock, the VA macro's LPASS votes); the debug switches (the GDSCs, the
  RPMh retention floor, the AOP's CXPC log); the UFS PHY's two diagnostics
  and ufs-qcom's count diagnostic. Each was checked on this board's tree: it
  applies strictly to a `gts9uwifi` tree, and the objects compile with the
  release build's flags, without a diagnostic.
- **The kit ships the SM-X710 kit's `sleep-test.sh` and
  `ufs-phy-power-check.sh`**, which know both boards since 2026-11-25. On this
  board `sleep-test.sh release-floor` releases the interconnect floor in the
  run itself (no late release here), a run refuses to start with Bluetooth
  and Wi-Fi both off (it is first for L15B with a radio on), and the `pmic`
  option reads the PMIC's power-on and over-current logs before and after
  each sleep, read only. The X710 kit's README, "The sleep test", has the
  rest, which is the same on both boards.
- **`install.sh` and `rollback.sh` work as before** (only `install.sh`'s
  header comment changed): revision 8 installs over revision 7, or over the
  release, with one command and one reboot, and `rollback.sh` returns to the
  release. vendor_boot is not written. Before copying revision 8 to a tablet
  that runs revision 7, move revision 7's kit aside
  (`mv ~/typec-roles ~/typec-roles-r7`) instead of copying over it: its
  `install.sh`, run over revision 8, is the one-command way back to
  revision 7.
- **`verify.sh`** adds revision 8's checks, each a FAIL on revision 8's
  kernel (the kit's `REVISION`) and a note on any other: the helper's two
  lines, ramoops (its line and `/dev/pmsg0`), the running DT the overlay
  leaves, rpmhpd, gcc and gpu_cc `state_synced` 1 (with what holds one back,
  from the kernel's `sync_state() pending` lines), `qcom_stats` bound, the
  debug switches as they boot, the PCIe guard's line in the boot's panel
  cycle, Bluetooth and the console's tty, and the L15B step's line, which
  revision 8's kernel must log. The interconnect floor and the sensors' sleep
  hook (device 2.75, or the `sensors-hook` kit) are notes.

What revision 8 risks on its first boot, and the way back if it does not
come up, are in the deep-sleep groundwork report
(`~/ubuntu-gts9u/backups/ultra-deep-sleep-groundwork-2026-11-25.md`,
sections 3 and 8): rpmhpd syncing at boot is new on this board, and a boot
that does not come up needs TWRP and revision 7's `boot.img`
(`~/ubuntu-gts9u/backups/typec-roles-ultra-kit-r7/boot.img`, sha256
`f81161f20d148a8e649bc240cddcb82b8898a1af0558f2346643ece0b15190fc`), then
revision 7's `install.sh` as an upgrade.

**Revision 9 (2026-11-26): the GPU's one-second freezes and the swap back to
sink. Built and checked on the host; nothing that it adds has run on a
tablet.** It is revision 8 with the two fixes this checkout got after it, in
every build. The configuration, the DTB, the overlays, vendor_boot, the
fingerprint layers, the module list and the build switches are revision
8's:
- **The GPU controller's lost answers**
  (`msm-adreno-a6xx-hfi-irq-leave-polled-bits.patch` and
  `msm-adreno-a6xx-hfi-take-queued-response.patch`, in `kernel/patches/`,
  both boards;
  [`docs/gpu-recovery.md`](../../../docs/gpu-recovery.md#gmu-vote-timeouts-the-answer-bit-that-goes-missing-2026-11-26)).
  About once an hour of use the desktop froze for a second, with `Message
  HFI_H2F_MSG_GX_BW_PERF_VOTE id N timed out waiting for response` and then
  `Unexpected message id N on the response queue` in the kernel log: the GPU
  controller (GMU) had answered in time, and the driver's own interrupt
  handler had cleared the bit that announces the answer. The handler now
  leaves that bit, and the OOB acknowledge bits, to the code that waits for
  them, and the wait also ends when the answer is in the queue. **Both were
  written from nine timeouts captured on this tablet with the gpu-watch kit
  on 2026-11-26, under revision 8's kernel, and neither has run on a
  tablet.** If the handler finds the interrupt mask changed, it writes it
  again and says so once a boot: `GMU interrupt mask 0x... left 0x...
  unmasked (status 0x...), written again: 0x...`.
- **The swap back to sink** (`tcpm-swap-back-to-sink.patch`, in
  `kernel/patches/`, on top of the five TCPM patches, and the driver
  `sm5714_usbpd.c` with six more switches;
  [`docs/usb-c-dock-roles.md`](../../../docs/usb-c-dock-roles.md#the-power-role-later-asking-a-dock-with-its-own-supply-again)).
  When a charge-through dock's own supply goes away for a moment, the tablet
  becomes the dock's power source, as it should. On 2026-11-26 the Anker hub
  then said nothing, also after its charger was back, and the tablet powered
  it from its battery until it was re-plugged. Now, as power source to a PD
  partner, the tablet asks it from time to time for its source capabilities
  and asks for the sink role when they say that it has its own supply; under
  a PD 3 contract it presents Rp 3.0 A where TCPM signals SinkTxOK, so that
  the hub may ask for the swap itself; and the driver applies the tablet's
  own power role swap in place, resets the SM5714's PD Protocol Layer at the
  end of an in-place swap and releases it after a Hard Reset, and makes
  TCPM's error recovery after either a real detach. **None of revision 9's
  dock code has run on a tablet.** What it was written from ran on revision
  8: the hub's own power role swap (at 14:52, with `otg_rp` at 2), which
  completed and left PD deaf until a detach, and a swap of the tablet's
  own, which the hub accepted and the tablet could not finish.
  The switches, under `/sys/module/sm5714_usbpd/parameters/`, each read from
  the next attach unless it says "each swap" or "each recovery":

  | Parameter | Default | Meaning |
  | --- | --- | --- |
  | `swap_back_to_sink` | `Y` | As power source, ask a PD partner with its own supply for the sink role |
  | `own_pr_swap_in_place` | `Y` | Apply the tablet's own accepted swap to sink in place; `N` is the error recovery of before |
  | `sink_tx_ok_rp` | `Y` | As PD 3 source, Rp 3.0 A where TCPM asks for SinkTxOK; `N` is `otg_rp` for every Rp request |
  | `swap_protocol_reset` | `Y` | Reset the PD Protocol Layer at the end of an in-place swap (each swap) |
  | `recovery_detach` | `Y` | Open CC for TCPM's error recovery after an in-place swap or a Hard Reset (each recovery) |
  | `pr_swap_rd_delay_ms` | `0` | Wait before Rd in an in-place swap to sink (each swap), at most 500 |

  With `otg_rp` (`0`) and `connection_wakeup` (`N`), which the release
  already had, and `sink_host_swap` and `try_snk_extra_wait_ms` above, the
  driver has ten. The write-up's "Trying it on a tablet" has six runs with
  the lines to expect.
- **`make-kit.sh`** replays the three patches in the build's order (with the
  release tree, twenty-three files may differ now), requires their strings
  and the driver's in the new image and finds none of them in the release's
  or, with `PREV_KIT`, in revision 8's (whose `boot.img` it checks against
  the one on the tablet), requires the driver's ten switches with their
  types in `modules.builtin.modinfo`, reads the GMU's HFI wait and the swap
  back's work in `vmlinux`, and writes `REVISION` 9.
- **`install.sh` and `rollback.sh` work as before** (only `install.sh`'s
  header comment changed): revision 9 installs over revision 8, any earlier
  revision or the release with one command and one reboot, and
  `rollback.sh` returns to the release. Before copying revision 9 to a
  tablet that runs revision 8, move revision 8's kit aside
  (`mv ~/typec-roles ~/typec-roles-r8`) instead of copying over it: its
  `install.sh`, run over revision 9, is the one-command way back to
  revision 8.
- **`verify.sh`** checks the driver's ten switches: a missing one is a FAIL
  (the running driver is not this kit's), one that is not at its default
  is a note, changed since the boot (up to revision 8 a changed
  `sink_host_swap` or `try_snk_extra_wait_ms` was a FAIL). The GPU fixes are
  in msm, which is built in: the build ID is what says that they run, and
  `verify.sh` says so on the kit's kernel (and checks that
  `modules.builtin` lists msm). It counts this boot's kernel log lines
  `timed out waiting for response`, `Timeout waiting for GMU OOB` and
  `Unexpected message id`, which should stay at 0, and `GMU interrupt mask`,
  the fix's own note, as information, not as a verdict. The plug-in
  recorder's `kernel` file also records the six new switches and `otg_rp`.

## Build (host)

The kernel inputs are the release's plus the change, so build from the
release's kernel inputs with the change on top, in a new object tree.

```sh
U=/path/to/scratch/build-typec-ultra     # on the external drive, no spaces
git worktree add --detach $U/source <commit>
DEVICE=gts9uwifi UBUNTU_WORKDIR=$HOME/ubuntu-gts9u TMPDIR=$HOME/ubuntu-gts9u/tmp \
  APPLY_QMP_UFS_PCS_STATE_DIAGNOSTIC=1 APPLY_DEEP_SLEEP_DEBUG_SWITCHES=1 APPLY_SM8550_SLEEP_FIXES=1 \
  APPLY_SM8550_PCIE0_GDSC_OFF=1 APPLY_SM8550_PCIE0_GDSC_GUARD=1 \
  APPLY_LPASS_VA_MACRO_PM_CLK=1 APPLY_DSI_PHY_AHB_RELEASE=1 APPLY_GENI_SERIAL_FORCE_SUSPEND=1 \
  KERNEL_WORKTREE=$U/linux-src-gts9uwifi KERNEL_BUILD_DIR=$U/linux-gts9uwifi \
  KERNEL_OUT_DIR=$HOME/ubuntu-gts9u/out/kernel-gts9uwifi-typec \
  bash $U/source/scripts/build-mainline-kernel.sh
BASE_SCRATCH=/path/to/scratch/build PREV_KIT=$HOME/ubuntu-gts9u/backups/typec-roles-ultra-kit-r8 \
  bash tools/inplace/typec-roles-ultra/make-kit.sh $U \
  $HOME/ubuntu-gts9u/out/kernel-gts9uwifi-typec \
  $HOME/ubuntu-gts9u/out/kernel-gts9uwifi \
  $HOME/ubuntu-gts9u/out/bundle \
  $HOME/ubuntu-gts9u/backups/typec-roles-ultra-kit
```

The `APPLY_*` variables are revision 8's, the SM-X710 kit's set, and
revision 9's too (`APPLY_SM8550_ROOT_PORT_D3` stays unset: the root port's
D3hot is the sleep test's `root-port-d3`, off at every boot); revisions 1 to
7 were built without them. Revision 9's fixes need no variable: the build
applies them always. The build archives its module key in the registry
(`scripts/kernel-key-registry.sh`); `make-kit.sh` refuses a build whose key is
not there. Run `make-kit.sh` from the checkout that was built: it compares
the build tree with that checkout's driver, early helper, patches and stage
scripts, and refuses a build without one of revision 8's or 9's changes (the
image has to hold every string of `new_strings`, `deep_strings`,
`gpu_strings` and `swap_back_strings`, none of which the release image has).
With `BASE_SCRATCH` it also compares the whole kernel
source tree with the release build's (the objects that the module builds
leave in the tree aside): it replays every patch on the release tree's files
in the build's order (`build_order`), with `unpark-pcie0-pipe-mux.patch`
taken out, the two stage scripts on top, and twenty-three files and two
overlay blobs may differ, by exactly that; and the compiled power domain
table with the release build's: four bytes and nothing else (the two USB
domains, `pcie_0_gdsc`'s power states and its genpd flag). It also checks,
in `vmlinux`, the UFS PHY's calibration, the PIPE clock mux, the PCIe PHY's
power-on, the geni UART's sleep callbacks, the debug switches, the GMU's HFI
wait and the swap back's work. With `PREV_KIT` (revision 8's kit) it checks
that kit's `boot.img` against the one revision 8 wrote and finds none of
revision 9's strings and switches in its kernel and module tree.
`SOURCE` in the kit records the checkout, the build's switches and which of
the checks ran; `REVISION` the revision (9; revisions 1 to 7 had none).

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

It also reports L15B, PM8550's LDO15 (`vreg_l15b_1p8`, the eUSB2 repeater's
1.8 V and the WCN7850's I/O rail). On the SM-X710 a sleep with Bluetooth on
voted that LDO to LPM, and once the whole SoC slept the PMIC reset the
tablet; held in HPM, the same sleep went deep
([`docs/resume-recovery.md`](../../../docs/resume-recovery.md#deep-sleep-on-the-sm-x710-2026-11-24-and-25)).
This board's pinned DTB still lets L15B leave HPM. A kernel with the L15B
step of `kernel/drivers/gts9u-performance.c` (2026-11-25) takes
`regulator-allow-set-load` and `regulator-allowed-modes` out of the live
tree at boot and logs a `gts9u-performance: L15B` line; the bootloader's
copy, `/sys/firmware/fdt`, keeps both. With that line `verify.sh` checks
that the running DT's ldo15 has neither property and that the regulator
reads `normal` (HPM), and fails on the kit's kernel if not. Without it, as
on revisions 1 to 7, it prints a note and checks nothing; revision 8's
`make-kit.sh` requires the step in its kernel, and on that kernel a missing
line is a FAIL. Whether the SM-X910 needs the step has not been measured,
and it cannot be yet: this board reaches no whole-SoC sleep while its RPMh
power domains have not synced, which revision 8 is to change.

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
8. Revision 9's GPU fix, after a day of ordinary use: `sh
   ~/typec-roles/verify.sh` and its `GPU, this boot's kernel log` line. No
   `timed out waiting for response` is the fix doing its work (revision 8
   logged about one an hour of use); a `GMU interrupt mask` line gives the
   register values the fix found.
9. Revision 9's swap back to sink: the six runs of "Trying it on a tablet"
   in the write-up, with the Anker hub, each with `sudo sh
   ~/typec-roles/verify.sh record`; its `kernel` file has the switches the
   run had.
