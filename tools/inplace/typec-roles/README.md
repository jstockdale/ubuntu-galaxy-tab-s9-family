# Type-C roles kit (SM-X710, November 2026)

This kit goes on top of the [hang-log kit](../hang-log/README.md). It carries
the kernel changes for powered USB-C docks described in
[`docs/usb-c-dock-roles.md`](../../../docs/usb-c-dock-roles.md), so that they
can be tested on a tablet, since revision 8 the UFS PHY change described
in [`docs/resume-recovery.md`](../../../docs/resume-recovery.md#sm-x710-gts9wifi)
with a sleep test that cannot damage the storage, since revision 9 the
deep-sleep debug switches (below, "Revision 9"), and since revision 10 three
upstream fixes for the SoC's sleep and a second UFS PHY diagnostic (below,
"Revision 10"). Everything the hang-log kit set stays: the same
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
  ([`docs/resume-recovery.md`](../../../docs/resume-recovery.md#sm-x710-gts9wifi)).
  Since 2026-11-23 it is known not to: every calibration on record starts
  from a freshly reset PCS, and the failure came back on revision 9, which
  carries the reset (below, "Revision 9: the runs");
- `kernel/patches/qmp-ufs-pcs-state-diagnostic.patch`, a diagnostic that
  release builds do not get (the build applies it only with
  `APPLY_QMP_UFS_PCS_STATE_DIAGNOSTIC=1`). It reads registers and logs:
  a debug line at every calibration with the state the PHY starts from, and
  after a PCS_READY timeout the state it left and whether PCS_READY came
  within 1 s. It changes no result. If the reset does not fix the sleep, the
  one failed sleep says what to try next (below, "The sleep test").

**Revision 9** is revision 8 plus two debug patches, which a release build
does not get (the build applies them only with
`APPLY_DEEP_SLEEP_DEBUG_SWITCHES=1`, and refuses a default build in a tree
that carries them). They add four run-time switches, off at every boot, for
telling apart why a sleep with the PCIe root port removed and the
interconnect floor released never comes back (2026-11-22: the SoC drops
PS_HOLD 43 to 44 s after the RTC alarm; below, "Revision 9: the runs"):
- `kernel/patches/gcc-sm8550-gdsc-debug-switches.patch`:
  `/sys/module/gcc_sm8550/parameters/debug_pcie0_off`, `debug_pcie0_phy_off`
  and `debug_usb_off`. Each moves its power domains (GDSCs) from
  `PWRSTS_RET_ON`, which the kernel's `gdsc_disable()` leaves on in hardware
  so that they only enter retention when CX collapses, to `PWRSTS_OFF_ON`,
  which it collapses, from the next power-off genpd asks for: the PCIe host's
  `pcie_0_gdsc`, its PHY's `pcie_0_phy_gdsc`, and the two USB GDSCs, which go
  back to mainline's `OFF_ON` (the docked xHCI dies with that: run
  undocked). An SM8550 board with the same symptom ("reset about 46 s after
  the RTC alarm") was fixed by making `pcie_0_gdsc` `OFF_ON` (ROCKNIX PR 3636);
- `kernel/patches/rpmhpd-debug-retention-floor.patch`:
  `/sys/module/rpmhpd/parameters/debug_ret_floor` keeps this kernel's votes
  for the CX and MX rails (`cx.lvl`, `mx.lvl`) at RETENTION or above in every
  RPMh set, where without it the suspend votes CX off (level 0) and MX gets
  0 throughout; Samsung's kernel stops at RETENTION.

With every switch off the kernel does what revision 8's does: nothing writes
the power domain table until a switch is written, and its compiled tables
are byte for byte revision 8's (`make-kit.sh` checks them against the
hang-log build's). The kernel command line cannot set the GDSC switches,
because the boot's suspend cycle for the panel runs with the PCIe host up.
`sleep-test.sh` sets them for a run (`gdsc=`, `cxmx=ret`, below), and the
restart that ends every run puts them back. By hand, as root:
`echo Y | sudo tee /sys/module/gcc_sm8550/parameters/debug_pcie0_off`; but
**never set a PCIe one for a suspend in which the PCIe host stays up** (any
suspend without `pci=remove`): genpd collapses the GDSC at every suspend,
and the live controller loses its registers.

**Revision 10** (built 2026-11-23, not on the tablet yet) is revision 9 plus
three upstream commits that 7.2-rc3 lacks, and one more diagnostic, all behind a
build variable of their own, `APPLY_SM8550_SLEEP_FIXES=1` (a default build
and the SM-X910's builds are unchanged without it, and a build without it
refuses a tree that carries any of them). Revision 9's runs and the
analysis of 2026-11-23 point at them (`docs/porting-log.md`):
- `kernel/patches/clk-qcom-regmap-phy-mux-revert-rework.patch`, upstream
  0aec16a93bac (v7.2): the PCIe PIPE clock muxes are gates again, parked on
  XO while their GDSC is off and switched to the PHY's clock when the PHY
  enables it. In revision 9's runs with `gdsc=pcie0` the GDSC could not
  power up at the resume (`pcie_0_gdsc status stuck at 'off'`) with its mux
  left on the clock of a PHY that was off. `unpark-pcie0-pipe-mux.patch`,
  which selected the PHY's clock at each PHY power-on, is left out: the
  clock's own enable does that now, at the same point, and the Wi-Fi's link
  at boot depends on it (`verify.sh` checks the link);
- `kernel/patches/msm-adreno-a6xx-fix-stale-rpmh-votes.patch`, upstream
  d9108bfdb746 (v7.3-rc1): the GPU RSC's sleep sequence runs when the GPU's
  GMU powers down (an inverted test skipped it), so the GPU's last
  bandwidth votes do not stay; its wake sequence now runs at the next power
  up as well;
- `kernel/patches/ufs-qcom-enable-only-lane-clocks.patch`, upstream
  f07317a8d57f (v7.4-rc1): ufs-qcom turns its three lane clocks on and off
  once. Before it every runtime resume of the UFS host enabled all of its
  clocks once more: enable counts of 522 to 525 on squirtle on 2026-11-23,
  the clocks and XO under them on through every sleep;
- `kernel/patches/qmp-ufs-pwrdn-readback-diagnostic.patch`, with
  `APPLY_QMP_UFS_PCS_STATE_DIAGNOSTIC=1` as well (a diagnostic, never in a
  release): before each calibration it reads POWER_DOWN_CONTROL back right
  after the power-up write and 100 us later, `power-up write: PWRDN 0x1
  right after it, 0x1 100 us later` (below, "Reading the diagnostic"). In
  the 2026-11-23 failure the calibration entry read that bit as 0. Reads
  only.

The inline crypto engine's driver keeps its core clock and the UFS AHB
clock enabled from its probe on, which no upstream commit changes:
`gcc_ufs_phy_ice_core_clk`, under XO, stays enabled with revision 10 too.

`sleep-test.sh` is new in revision 8: deep sleeps with every disk
filesystem frozen, so that a resume that loses the storage costs a restart
and nothing else. It also runs on revision 7's kernel, for a baseline
before the install. Its first form made the filesystems read-only with
`sysrq u`, and on 2026-11-22 that left the root unable to mount at the next
boot (below, "The sleep test"); it freezes them now, keeps ext4 from
recording an error during the run (`errors=continue` with the disk
read-only), and its `dry` mode proves the protection and the restart
without a sleep.

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
  TCPM. Revision 9 adds the two debug patches above: eleven patches.
  Revision 10 adds the three upstream fixes and the second UFS PHY
  diagnostic, fifteen patches, and leaves out `unpark-pcie0-pipe-mux.patch`,
  which every default build applies. In the kernel source tree twelve files
  differ from the hang-log build: `tcpm.c`, `tcpm.h`, the driver,
  `gcc-sm8550.c`, `dwc3-qcom.c` (the hang-log build's has the dwc3-qcom CX
  vote patch already), `phy-qcom-qmp-ufs.c` (the hang-log build's has the
  SM-X910-only form of the PCS reset, which
  `scripts/stage-ufs-resume-experiment.sh` replaces), `rpmhpd.c` (revision
  9), and `clk-regmap-phy-mux.c`, `phy-qcom-qmp-pcie.c` (without the unpark
  patch), `a6xx_gmu.c`, `ufs-qcom.c` and `ufs-qcom.h` (revision 10). Against
  revision 9's tree six differ: those five and `phy-qcom-qmp-ufs.c`.
  `make-kit.sh` checks that, and that the configuration and the DTB are
  byte for byte the hang-log build's.
- **boot** holds the new kernel. **vendor_boot is not written**: it carries
  the DTB, which did not change. `install.sh` checks that it still holds the
  hang-log kit's image.
- **The module tree** comes from this build and is signed with the key of
  its build directory, which is archived in the key registry. The hang-log
  kit has another key; revisions 2 to 7 of this kit were built in one
  directory and share theirs, revision 8 is built in that directory as
  well, and revisions 9 and 10 in directories of their own given that key
  (below, "Build"). The patches
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

  The power domain change, the NULL check, the UFS PHY's PCS reset and
  revision 10's three upstream fixes have no switch; `rollback.sh` is the
  way back.
- **Four debug switches** (revision 9), all off at every boot; `sleep-test.sh`
  sets them for a run (`gdsc=`, `cxmx=ret`), `verify.sh` checks that they
  are there and off:

  | Parameter | Default | `sleep-test.sh` | Meaning when `Y` |
  | --- | --- | --- | --- |
  | `/sys/module/gcc_sm8550/parameters/debug_pcie0_off` | `N` | `gdsc=pcie0` | `pcie_0_gdsc` collapses at the next power-off (`OFF_ON`) |
  | `/sys/module/gcc_sm8550/parameters/debug_pcie0_phy_off` | `N` | `gdsc=pcie0phy` | `pcie_0_phy_gdsc` the same |
  | `/sys/module/gcc_sm8550/parameters/debug_usb_off` | `N` | `gdsc=usb` | both USB GDSCs the same (mainline's state) |
  | `/sys/module/rpmhpd/parameters/debug_ret_floor` | `N` | `cxmx=ret` | votes for CX and MX at RETENTION or above |

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
  APPLY_QMP_UFS_PCS_STATE_DIAGNOSTIC=1 APPLY_DEEP_SLEEP_DEBUG_SWITCHES=1 APPLY_SM8550_SLEEP_FIXES=1 \
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
diagnostic, without `APPLY_DEEP_SLEEP_DEBUG_SWITCHES=1` no debug switches,
and without `APPLY_SM8550_SLEEP_FIXES=1` none of revision 10's fixes and
`unpark-pcie0-pipe-mux.patch` instead; `make-kit.sh` refuses each (their
strings are not in the image, or the PIPE clock checks fail). A build tree
that carries the diagnostics, the switches or the fixes is refused for a
build without its variable, so a release cannot be built from it by
mistake; and a tree that carries the unpark patch is refused for a build
with the fixes.

Revisions 2 to 8 were built in one directory, `build-typec`, which holds
their module key. Revision 9 was built from scratch in a directory of its
own (`build-r9`), whose `linux-gts9wifi/certs` was given that key before the
first `make`: `signing_key.pem`, `signing_key.x509`, `x509.genkey` and
`.signing_key.pem.cmd`, copied with their times kept (`cp -p`), so that
kbuild found the key up to date and did not make a new one (no `GENKEY` in
the build's log). The build archived its row under the same key ID in the
registry. Revision 10 was built the same way, in `build-r10`, on
2026-11-23 from commit ab2c536: kernel build ID
`90da273a8a49a60284ad7b06c15288b05913d931`, the same module key, no
`GENKEY`, and the three warnings of revision 9's build.

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
change to `tcpm.c`, `tcpm.h`, the driver, `gcc-sm8550.c`, `dwc3-qcom.c`,
`phy-qcom-qmp-ufs.c`, `rpmhpd.c` and revision 10's five files, and that
`qmp_ufs_phy_calibrate()` in the build's `vmlinux` (the image's, by its build
ID) no longer calls `of_machine_compatible_match()`: the board check of
revision 7 and before. For revision 10 it checks that each upstream commit
is whole in the build tree and the unpark patch is not, that the build's
`vmlinux` has the PIPE clock mux's gate operations (`phy_mux_enable`,
`phy_mux_disable`, `phy_mux_is_enabled`) and not the rework's rate ones, and
that its `qmp_pcie_enable()`, into which the PHY's power-on is inlined,
calls no `clk_set_rate()`; with `HANG_SCRATCH` the hang-log build's shows
the opposite of each, and its tree is compared with the unpark patch taken
out of its `phy-qcom-qmp-pcie.c`. The NULL check and the GMU fix leave no
string in the image: the build tree's source and the date of its object
are what show them.
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
the tests start the upgrade from. Revision 9 installs the same way over
revision 7, which is what squirtle has on 2026-11-22 (revision 7 installed
and running, revision 8's files copied into `~/typec-roles` and never
installed: they changed nothing), and over an installed revision 8 alike.
Revision 10 installs the same way over revision 9, which squirtle has had
since 2026-11-22 20:59 (installed by its own `install.sh`, which recorded
it), and over every earlier revision.

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
sudo sh ~/typec-roles/sleep-test.sh dry restart-ok                     # no sleep: the protection and the restart
sudo sh ~/typec-roles/sleep-test.sh ro 3 30 restart-ok                 # floor as the boot left it
sudo sh ~/typec-roles/sleep-test.sh ro 3 30 release-floor restart-ok   # release it first
sudo sh ~/typec-roles/sleep-test.sh ro 1 30 release-floor mode=s2idle restart-ok   # suspend-to-idle
sh ~/typec-roles/sleep-test.sh status                                  # the result, no root
```

The whole form, with the experiment options (each off unless given; below,
"The experiment options"):

```sh
sudo sh ~/typec-roles/sleep-test.sh ro [CYCLES] [SECONDS] [release-floor] [hold=S] [aborts=N]
     [pci=remove] [pm_test=LEVEL] [trace|trace=live] [mode=s2idle]
     [gdsc=pcie0,pcie0phy,usb] [cxmx=ret] restart-ok
sudo sh ~/typec-roles/sleep-test.sh dry [release-floor] [hold=S] [pci=remove] [mode=s2idle]
     [gdsc=pcie0,pcie0phy,usb] [cxmx=ret] restart-ok
```

Each run freezes every disk filesystem and **ends with a restart of the
tablet** (`sysrq b`), whatever happens: `restart-ok` says that is fine, and
without it nothing is done. One exception, always before any sleep: a
protection that cannot be completed (a freeze that fails, the disk that
does not go read-only, `errors=continue` that does not take, an ext4 error
found before the first sleep) is undone, `errors=` put back, and the run
stops without a restart, saying so in its result. A freeze that does not
end, or a thaw that fails, restarts instead. Everything after the first
read ends in the restart, an `ERROR-STATE` too (below). Before the first
run with sleeps after this change, run `dry` once and check that the tablet
boots cleanly after it (`verify.sh` says so: it knows the boot after a
sleep test).

**Do not touch the screen during a run.** A double tap on it wakes the
tablet, and in a suspend that is under way it makes the kernel give the
suspend up (ABORTED, below). The run says so before it freezes anything.

**Keep an SSH session open before starting.** While the disk is frozen,
every program that writes to it waits: the desktop, the journal, `sudo`'s
log, a new SSH login. Reading works: `sh ~/typec-roles/sleep-test.sh status`
(no `sudo`) in a session that is already open shows the result.

In order:

1. It refuses, with nothing changed, unless it runs as root on an SM-X710,
   `/sys/power/mem_sleep` offers `deep` (with `mode=s2idle` instead:
   `/sys/power/state` offers `freeze` and a cpuidle state of cpu0 has an
   s2idle entry; `mem_sleep` is neither needed nor touched), the RTC can
   wake the tablet, userdata is a partition of a writable UFS LUN,
   `kernel.panic_on_warn` and `kernel.hung_task_panic` are 0 (the first
   resume with a filesystem
   frozen logs one kernel warning, and writers waiting on the frozen disk
   are reported as hung tasks after 60 s: either would otherwise panic the
   tablet in the middle of the run), the root filesystem is writable, ext4
   has recorded no error on it (`/sys/fs/ext4/<dev>/errors_count` is 0 and
   `dumpe2fs -h` says `clean`; otherwise it needs `e2fsck -f` from recovery
   first), every writable disk filesystem is ext4 (exfat, the SD card's,
   cannot be frozen: unmount it first, `sudo umount /media/<user>/<card>`),
   none is mounted at a path with a space or the like in it, none runs
   with `errors=panic`, the battery is at 30 % or more, the boot has
   settled (120 s, the panel cycle done, the audio DSP started) and every
   tool it needs is there.
2. It records the state before in `~/typec-roles/logs/<time>-sleep/` and in
   `/run/gts9u-sleep-test/<time>/`: the kernel and whether it is the kit's,
   the interconnect floor, `suspend_stats`, `qcom_stats`, the Type-C roles,
   the charger, the root's ext4 state, each ext4 filesystem's error count
   and error behaviour (its `errors=`, and its superblock's default), the
   mounts, the order it will freeze them in, and the swap. The error counts
   read here are the ones the run compares with to the end.
3. It copies every tool it needs from then on, the shell and itself, with
   their libraries and the dynamic loader, into a tmpfs, each at its own
   path, and switches off every swap that is not zram (the kernel writes a
   swap file's blocks past its filesystem, frozen or not). From then on it
   runs in a mount namespace whose root is that tmpfs, in a session of its
   own: nothing it runs comes from the disk, and it goes on to the end when
   SSH drops. It writes its lines to its log in `/run` only; the part
   started from the terminal shows that log there (`tail -f`), so a
   terminal or an SSH server that stops reading cannot hold the run up.
4. **`errors=continue`**, the last step on the disk: every ext4 filesystem
   it will freeze that does not run with `errors=continue` already is
   remounted with it, by the system's own `mount` in PID 1's mount
   namespace (`nsenter -t 1 -m -- mount -o remount,errors=continue
   MOUNTPOINT`), and `/proc/1/mounts` has to show it (on this root, whose
   superblock says remount-ro, `errors=continue` appears among the options;
   on a stick whose superblock says continue and that was mounted with
   `errors=remount-ro`, the word goes away). If it does not take, what was
   changed is put back and the run stops before freezing anything. Only
   the mount option changes: the superblock's default, which the next boot
   mounts with, is left alone (no `tune2fs`). Why: with the disk read-only
   at the block layer (next step), an ext4 error under `errors=continue`
   neither aborts the journal nor saves anything to the disk. Under
   `errors=remount-ro`, this root's default, ext4 aborts its journal, and
   jbd2 writes the error into the journal's superblock even on a read-only
   disk (the block layer only warns about that write); the next mount then
   marks the root in error, skips the orphan cleanup, and fails as on
   2026-11-22 (below). A stop before any sleep puts each filesystem's
   previous behaviour back after the thaw, through the same `mount`, and
   says plainly if that failed (that filesystem then runs with
   `errors=continue` until the next restart, and the line gives the command
   to put it back). A restart needs nothing put back. A filesystem mounted
   after this step (a stick after a resume) is frozen without it.
5. With `pci=remove`, the PCI root ports go off the bus first, in RAM, the
   disk still writable (below, "The experiment options"). It freezes every
   writable disk filesystem (`fsfreeze -f`, through
   `/proc/1/root`): one on a loop device first, then the deepest mount
   first, the root last, each once. A freeze flushes the journal and
   records nothing (below). Then the UFS disk is made read-only at the block
   layer as well, so that a write that still came would be logged
   (`Trying to write to read-only block-device`), and so that ext4 saves no
   error to the superblock any more: a read-only disk skips that, but not
   the journal abort, which is what step 4 is for. Before the first freeze
   and again once the disk is read-only it looks at ext4: each filesystem's
   error count against the one read in step 2, its options in
   `/proc/1/mounts` (`emergency_ro` or `shutdown`: ext4 gave up on it), and
   the kernel log since the run began (`EXT4-fs error (device NAME)`,
   `Aborting journal on device NAME-`, `EXT4-fs (NAME): Remounting
   filesystem read-only`). If a freeze fails, the disk cannot be made
   read-only, or ext4 recorded, logged or gave up on anything, the run
   thaws what it froze, puts `errors=` back and stops with `SLEEP-TEST
   RESULT: STOPPED before any sleep: ...` and a NEXT line for that cause
   (an ext4 error: `e2fsck -f` from recovery before the next boot); a
   freeze that does not end within 120 s, or a thaw that fails, restarts.
   Only then does it read the first 64 MiB of userdata with `O_DIRECT` and
   hash them, and the kernel log since the run began must have no failure
   line (`FAIL before any sleep` otherwise). **`dry`** then reads them again
   after 10 s, freezes each filesystem a second time (it must fail with
   `EBUSY`: still frozen), checks the kernel log since the run began for a
   failure line and ext4 as above, and ends with `SLEEP-TEST RESULT: DRY: no
   sleep tried; ...` and the restart.
6. Each cycle: `rtcwake -m mem -s SECONDS` (`mode=s2idle`: `rtcwake -m
   freeze -s SECONDS`); right after it each frozen filesystem is frozen a
   second time, which must fail with `EBUSY` (still
   frozen); a second freeze that succeeds means something thawed it during
   the cycle, which fails the cycle as a failure of the protection (the
   filesystem stays frozen from then on). Then the kernel log since the
   sleep, the same read, `suspend_stats` and `qcom_stats`. A cycle
   **fails** on a UFS failure line (`phy initialization timed-out`, `Failed
   to calibrate PHY`, `Enabling the controller failed`, `Host init failed`,
   `ufshcd_wl_resume failed`), an I/O or ext4 error on the UFS disk, a write
   to the disk that the block layer logged (`Trying to write to read-only
   block-device`: it logs the first one and does not stop it), a journal
   started on a frozen ext4 (the kernel's `WARNING` in
   `ext4_journal_check_start`, `fs/ext4/ext4_jbd2.c`), a read that failed
   or differs, a sleep that did not happen (`rtcwake`, `suspend_stats`, no
   `PM: suspend entry (deep)`, or `(s2idle)` with `mode=s2idle`). The PCIe
   host and its PHY log two of those UFS words too (`qcom-pcie
   1c00000.pcie: Host init failed: -16`, the PCIe PHY's `phy initialization
   timed-out`): such a line fails the cycle as well, but the storage counts
   as failed only on a failure line that is not theirs or on the read, and
   the NEXT line then says that the storage came back and names the PCIe
   host's line (until 2026-11-23 it said "the storage did not come back"
   for those too). The run stops at the first failed cycle. A cycle that
   passed is **PASS** when AOSS sleep, CX collapse and DDR low
   power (`aosd`, `cxsd`, `ddr`) all counted up, **SHALLOW** when one did
   not (it did not test the depth of the 2026-11-21 failure),
   **DEPTH-UNKNOWN** without `qcom_stats`. A filesystem mounted between two
   cycles is frozen before the next one, or that cycle does not start.
   **Expected:** the first resume (or the first suspend given up, below)
   logs one kernel warning with a backtrace, `WARNING: fs/super.c:1919 at
   thaw_super_locked+...` (the suspend's own thaw of filesystems, with
   `/sys/power/freeze_filesystems` 0, finds no freeze of its own to undo and
   leaves the run's alone), and the kernel is tainted `W`. No rule of the run
   counts it. Hung-task reports of writers waiting on the frozen disk, after
   60 s, are expected too.
7. **ABORTED**: a suspend that the kernel gave up before the tablet slept,
   as on 2026-11-22 when the touchscreen's double tap came in the noirq
   phase. It is not a FAIL only when all of these hold, and a FAIL
   otherwise: `rtcwake` failed; `suspend_stats` counted no success and one
   failure; the attempt's kernel lines have `PM: suspend entry (deep)` (or
   `(s2idle)`) and `PM: suspend exit` and a line that shows the suspend
   given up on the way down (`Wakeup pending. Abort CPU freeze`, `Wakeup
   pending, aborting suspend`, `Triggering wakeup from IRQ N`, `Freezing ...
   aborted after`,
   `Some devices failed to suspend, or early wake event detected`, `late` or
   `noirq suspend of devices failed`, `... suspend of devices aborted after`,
   or a device's `failed to suspend ...: error N`); less than 1 s of
   `Timekeeping suspended`; no UFS or I/O failure line, no diagnostic line
   but calibration entries, and no line about the UFS that says something
   failed (a line with `ufs`, `[sda]`, or a SCSI device of the UFS host
   such as `sd 0:0:0:0:`, with `fail`, `error`, `timed out`, `timing out`,
   `abort`, `offline` or `rejecting` in it: a UFS device that refused to
   suspend is a FAIL); every filesystem still frozen; and the read after it
   matches the first. The line names the cause:
   `SLEEP-TEST cycle 1/3: ABORTED: the kernel gave up the suspend before the
   tablet slept (rtcwake 1, suspend_stats success +0 fail +1): a wakeup from
   IRQ 199 (fts1ba90a, the touchscreen: do not touch the screen during the
   run); the UFS read back as before; aborted attempt 1 (aborts=5): cycle 1/3
   again in 5 s; floor released`. Another IRQ is named by its name in
   `/proc/interrupts` (or "no name for it" there), a device that refused by
   its name and error, a wakeup source by its name. The same cycle is then
   tried again after 5 s; an aborted attempt never counts as a pass.
   **`aborts=N`** (default 5, at most 20) is how many aborted attempts a run
   tries again; one more ends it with `SLEEP-TEST RESULT: NO-SLEEP at cycle
   N/M: ...`, which says how many attempts were aborted and by what, how
   many cycles passed before, and that the run does not answer the storage
   question (not a PASS, not a FAIL). `aborts=0` ends the run at the first
   abort. A run that gets its cycles through with aborts on the way is
   still a PASS, and its RESULT line counts them (`1 attempt aborted before
   the sleep (IRQ 199 (fts1ba90a, the touchscreen) once) and tried again`).
8. **NO-SLEEP (freezer)**: a suspend whose task freezer gave up after its
   20 s (`Freezing user space processes failed after ...` or `Freezing
   remaining freezable tasks failed after ...`), with everything else as for
   ABORTED (no storage line, every filesystem still frozen, the read as
   before). A task that waits on the frozen disk can be frozen, but one
   queued behind it on a memory map's lock (a write fault on a shared
   mapping of a file on the root, as sqlite's and the journal's are, holds
   that lock while it waits) cannot. Not a FAIL and not tried again (it
   would meet the same task): `SLEEP-TEST cycle N/M: NO-SLEEP (freezer):
   ...; stuck: gnome-shell (state D), ...` with the tasks the kernel named
   after its line, then `SLEEP-TEST RESULT: NO-SLEEP (freezer) at cycle
   N/M: ...` and the restart. If it happens, run the test from
   `multi-user.target`, without the desktop: over SSH, `sudo systemctl
   isolate multi-user.target` (it stops the desktop and its programs; SSH
   stays), then the test; the restart at its end boots the desktop again.
   `dry` cannot show this: it does not suspend.
9. The verdicts: one `SLEEP-TEST cycle N/M:` line per cycle (and per
   aborted attempt), one `SLEEP-TEST RESULT:` line, and `NEXT:` lines with
   the next step. They go to the terminal, to
   `/run/gts9u-sleep-test/<time>/result` (readable without root, `latest`
   points to the newest), to the kernel log and to `/dev/pmsg0`, which
   pstore keeps across the restart. Each cycle's diagnostic lines (`cycle
   N/M: diagnostic:`, revision 8's kernel) go to the result file and to
   `/dev/pmsg0` too, just before the cycle's verdict: all of them up to 16,
   otherwise the first 12, a line saying how many were left out, and the
   last 4 (a failed resume retries its calibration 15 times, up to four
   lines each, and pmsg is a small ring). The terminal and the run's log in
   `/run` have every line until the restart. The diagnostic lines do not go
   to the kernel log, which has the driver's own. Every `RESULT` line ends
   with the experiment options in force (`; options: none` without them).
   pmsg alone also gets progress lines (`progress: ...`): the run's start
   in RAM with its options, the PCI removal, the protection complete, and
   right before each sleep (each attempt) the cycle it enters, with the
   knobs in force. A run that never comes back from a sleep therefore
   still leaves its start and the sleep it entered in the next boot's
   pstore archive. Before 2026-11-22 nothing went to pmsg before the first
   verdict, and the boot's `systemd-pstore` empties the zone when it
   archives it, so such a run left no pmsg file at all.
10. Once the verdict is out it looks at ext4 again, as in step 5. A journal
    that aborted, a filesystem ext4 gave up on, or an error saved (on a disk
    that is not read-only, a stick's) gives `SLEEP-TEST ERROR-STATE: ...`,
    up to six of ext4's kernel lines in pmsg (`ext4: ...`), and NEXT lines:
    for the root, that the boot after the restart will probably not mount
    it (jbd2 wrote the error into the journal's superblock; "probably",
    because the kernel may have logged that this write failed, `JBD2: I/O
    error when updating journal superblock`, and the NEXT line then says
    the boot may come up) and how to reach TWRP and repair it with
    `e2fsck -f`; for another filesystem, `e2fsck -f` before it is mounted
    again. An ext4 error logged on the read-only UFS disk with nothing
    aborted or saved (`errors=continue` at work) gives `SLEEP-TEST NOTE:
    ...` and a NEXT line that nothing was written and the next boot should
    mount the root as usual, to be checked. Then, after `hold=S` (300 s
    unless given), it looks at ext4 once more and says what is new
    (`SLEEP-TEST ERROR-STATE (right before the restart): ...`), writes
    `SLEEP-TEST RESTART: sysrq b; frozen: ...` (what `verify.sh` finds in the next
    boot's pstore) and restarts with `sysrq b`, the filesystems still
    frozen: it needs nothing from the disk, and the boot after it mounts
    them as after a power loss, with less to do (the freeze flushed the
    journal). An ERROR-STATE restarts too: with the disk frozen the tablet
    cannot be used, a restart keeps every line in pstore, and the repair
    happens in recovery anyway. A frozen filesystem is never thawed after a
    sleep: the thaw would be the first write after a resume, which this test
    does not make, and a clean reboot would need it (systemd stops services
    that wait on the frozen disk).

**Why a freeze, not read-only.** Until 2026-11-22 the run made the
filesystems read-only with `sysrq u`. A remount read-only of a mounted ext4
checks that its orphan file is empty, and while programs hold deleted files
open, as a running desktop always does, it is not: ext4 records an error
("Orphan file not empty on read-only fs"). With that error recorded, the
next boot skips the orphan cleanup and the same check fails the mount, and
this port's initramfs has no fsck: squirtle looped at boot until `e2fsck`
ran from recovery. The same follows from any ext4 error recorded on the
running root, which is why the run also keeps ext4 from recording one
(`errors=continue` with the disk read-only, step 4) and says so when it
did. A freeze flushes the journal and keeps the orphan file as it is,
recording nothing; the next mount cleans the orphans up as after any power
loss. The details, with the kernel functions, are in
[`docs/resume-recovery.md`](../../../docs/resume-recovery.md#protecting-the-storage-in-a-sleep-test).
After a run: `sh ~/typec-roles/verify.sh` (in the boot right after a run
it also fails on a root journal that was not empty at the mount, and in
any boot on an error ext4 recorded on the root), and
`cat /sys/fs/ext4/<root device>/errors_count` must say 0.

**What a pass means.** `SLEEP-TEST RESULT: PASS` with `floor released`: in
each cycle the SoC reached AOSS sleep, CX collapse and DDR low power, the
UFS came back without a single failure line, every filesystem was still
frozen after the resume, and 64 MiB read past the page cache matched the
read before the first sleep. It says nothing about writes after a resume
(the disk was frozen); that is the next step, a normal root
(`scripts/diagnostics/suspend/gts9u-suspend-cycles.sh`), and not for the
first runs. A run with the floor held says `floor held` in every verdict:
it does not sleep as deep as the 2026-11-21 failure, which came with the
floor released.

**What has run on the tablet (2026-11-22).** On squirtle, revision 7's
kernel (the logs are in `~/ubuntu-gts9u/backups/`; details in
[`docs/resume-recovery.md`](../../../docs/resume-recovery.md#what-ran-on-the-tablet-2026-11-22)):
- a freeze proof on throwaway ext4 images (09:18): after a freeze and
  `blockdev --setro` the image mounts cleanly; the error flag the 03:28 run
  left makes the mount fail as in that boot loop, and `e2fsck -p` repairs
  it; an ext4 error raised while frozen under `errors=continue` aborts
  nothing and writes nothing. Under `errors=remount-ro` the same error
  aborted the journal and wrote its superblock past the read-only flag, but
  with error number 0 (the sysfs trigger's `ext4_error()`), which the next
  mount ignores; an error that carries an errno, as a lost disk's do, is
  not something the proof could raise, so that part is read from the
  source only;
- `dry` (09:35): `fsfreeze` of util-linux 2.39.3 froze the root through
  `/proc/1/root`, the second freeze failed with `EBUSY`, `mount -o
  remount,errors=continue` with fstab's `errors=remount-ro` merged in ended
  with `errors=continue`; the next boot mounted the root with the orphan
  cleanup, no journal replay, `errors_count` 0;
- two revision 7 baselines with sleeps, docked, the floor released: 09:44
  `ro 3 30` and 10:17 `ro 10 30`. 13 of 13 cycles resumed with the UFS
  reading back as before, all SHALLOW (`aosd`, `cxsd`, `ddr` +0); the
  desktop's tasks let the freezer through each time; the first resume of
  each run logged the expected `fs/super.c:1919` warning, writers waiting
  on the root were reported as hung tasks; each next boot was clean. The
  2026-11-21 failure was not reproduced;
- undocked on the PC's USB cable: 12:05, the PCIe root port removed by
  hand, `ro 3 30`: the tablet did not come back from its first sleep and
  restarted by itself; 12:56, the same with the PCI devices kept: 3 of 3
  SHALLOW; 13:42, `ro 1 30 release-floor pci=remove pm_test=core trace`:
  PM-TEST core, the PCIe host went down and its link came back; 14:00, the
  same with `pm_test=platform`: PM-TEST platform, all counters +0, which
  is the only outcome that level allows (below, `pm_test=LEVEL`);
- on the powered dock again, 14:18, `ro 1 30 release-floor pci=remove
  trace`, a real sleep with the root port removed: the tablet did not come
  back and restarted by itself, as at 12:05, so the USB role makes no
  difference to that failure;
- on the PC's cable, a fresh boot, 15:49, `ro 1 30 release-floor pci=remove
  mode=s2idle trace`: suspend-to-idle did not come back either (the console
  record ends at `printk: Suspending console(s)`), and the tablet restarted
  by itself about 91 s after the sleep entry, as at 14:18. So the failure
  is not in the `SYSTEM_SUSPEND` call: with the PCIe host powered down the
  sleep returns by neither firmware call;
- the same three more times: 16:16 with a 60 s sleep and 16:44 with a 60 s
  sleep and `hold=120` (the restart about 122 s after the entry both
  times), 16:50 with 30 s and `trace=live` (about 92 s). The restart
  follows the RTC alarm by about a minute, whatever the hold, so it is not
  the run's own; and with the consoles kept on the record ends at `PM:
  suspend-to-idle`, after every phase of the device suspend, with no line
  from the resume;
- 18:22, the 16:50 run without `release-floor`, on the boot after that
  hang (the floor held by the loop breaker): it came back, SHALLOW, woken
  by the RTC after 27.7 s, with the PCIe host powered down and up again as
  in the runs that hung. So the host's power-down alone does not lose the
  sleep; releasing the floor on top of it does.

Revision 8 was never installed: revision 9, which carries it, went on over
revision 7 (2026-11-22 20:59; "Revision 9: the runs", below, has what ran
on it). Not run yet: revision 10, a cycle that reaches AOSS sleep, CX
collapse or DDR low power and comes back, `mode=s2idle` with the PCI
devices kept, and an ext4 error with an errno under `errors=continue`.

**Revision 9: the runs** (2026-11-22 and 23, squirtle; the logs are
`~/ubuntu-gts9u/backups/squirtle-sleep-r9-*-20261122.log` and the pstore
directories of those names; details in `docs/porting-log.md`). Revision 9
installed in place over revision 7 at 20:59, `verify.sh` passed on its
kernel. Then, each run in a boot of its own, undocked on the PC's USB
cable, one 30 s cycle with `release-floor pci=remove mode=s2idle hold=60`:
- 21:01, no switch, `trace=live`: hung and restarted by itself, as on
  revision 7. Revision 8's UFS PHY change does not touch the hang;
- 21:49, `gdsc=pcie0`, `trace=live`: **came back**, woken by the RTC after
  28.2 s, the UFS fine, the first sleep with the root port removed and the
  floor released that did. FAIL for PCIe alone: `pcie_0_gdsc status stuck
  at 'off'`, `qcom-pcie 1c00000.pcie: Host init failed: -16`. Still
  SHALLOW in depth (`aosd`, `cxsd`, `ddr` +0);
- 23:34, the same without `trace=live`: the same. 23:46, the same with the
  ADSP stopped first: the same, the ADSP's naps gone and DDR at 547 MHz
  instead of 1555 MHz, the depth still +0;
- 2026-11-23 00:14, `gdsc=pcie0,usb`, the ADSP stopped: came back with the
  same PCIe failure, and **the UFS did not**: 15 calibrations, each starting
  with `PWRDN 0x0` (the PHY's analog part powered down; `0x1` in every
  other run), each timed out. The root was protected: ext4 saved nothing,
  and the next boot mounted it with `errors_count` 0.

The PCIe failure and the depth are what revision 10 is for (above); the
UFS PHY's `PWRDN 0x0` is what its second diagnostic is for. The NEXT line of
the four runs that came back said "the storage did not come back" for the
PCIe host's `Host init failed` as well; revision 10's `sleep-test.sh` says
that only when it is so.

The plan these runs followed, kept for the record: each with `ro 1 30
release-floor pci=remove mode=s2idle trace=live hold=60 restart-ok`:

1. no switch: the control. It is expected to hang and restart by itself as
   revision 7's runs did at 15:49, 16:16, 16:44 and 16:50. If it comes back,
   revision 8's UFS PHY change mattered (the only other change in the
   kernel), and the order below has to be thought over before going on;
2. `gdsc=pcie0`;
3. `gdsc=pcie0,usb`;
4. `gdsc=pcie0,usb,pcie0phy`;
5. `gdsc=pcie0,usb,pcie0phy cxmx=ret`.

```sh
cd ~/typec-roles && grep ' sleep-test.sh$' SHA256SUMS | sha256sum -c --quiet &&
  sudo sh ~/typec-roles/sleep-test.sh ro 1 30 release-floor pci=remove mode=s2idle trace=live hold=60 \
    gdsc=pcie0 restart-ok
```

A run that comes back with the deep counters moving (`aosd`, `cxsd` and
`ddr` all +1 or more: `PASS (s2idle)`) identifies the cause: the switch it
added. Stop there. Warnings:
- each run that does not come back costs a forced restart, about 45 s after
  the RTC alarm, as on 2026-11-22. Before the next run read its record in
  pstore (above, "The result after the restart"; the RESULT is missing, the
  last progress line names the switches of the sleep it entered) and the
  PMIC's reset log (`~/ubuntu-gts9u/backups/deep-sleep-plan-2026-11-22/`);
- the boot after a forced restart holds the interconnect floor once; every
  run above releases it (`release-floor`);
- `gdsc=usb` gives the USB GDSCs mainline's state, under which the docked
  xHCI dies: undocked only;
- with a PCIe GDSC collapsed the PCIe host and its PHY are set up from
  scratch at the resume. v7.2-rc3 does not park the PCIe pipe clock mux on
  XO while its GDSC is off (the phy-mux rework that v7.2 final reverted, and
  `unpark-pcie0-pipe-mux.patch` only selects the PHY's clock at power-on),
  so the resume may log a kernel warning `pcie_0_gdsc status stuck at
  'off'` (or `pcie_0_phy_gdsc`). If the link then does not come up, the run
  ends with `PCIE-DOWN` and its restart, which is what it is for; the
  warning itself counts for nothing in the verdict (`panic_on_warn` is 0).
  A GDSC that stays off under the host's first register access can also
  stall the bus or crash the kernel: the tablet then restarts by itself or
  has to be forced, and with `trace=live` the console record ends at that
  warning;
- a run that comes back `SHALLOW` never reached the state in which the
  others hang, so it says nothing for or against its switch (with
  `trace=live` the debug UART stays powered, which may keep the SoC up);
- never set a PCIe switch by hand for a suspend without `pci=remove`; the
  kernel command line cannot set them, and every boot starts with them off.

**Revision 10: the runs** (planned; none has run). Install revision 10 over
revision 9, reboot, and run `sh ~/typec-roles/verify.sh`, then `sudo sh
~/typec-roles/verify.sh` for the UFS host's clock counts: the kit's kernel,
every debug switch `N`, the PCIe link up at boot (`ok   PCIe: the link came
up this boot (Gen.2 x1 link up)`: without the unpark patch the Wi-Fi's link
depends on the PIPE clock revert), no GPU RSC failure, one `power-up write`
line for each calibration, and no UFS host clock enabled more than 3 times.
Check that Wi-Fi works before any run. Then run 2b's run again, undocked on
the PC's cable:

```sh
cd ~/typec-roles && grep ' sleep-test.sh$' SHA256SUMS | sha256sum -c --quiet &&
  sudo sh ~/typec-roles/sleep-test.sh ro 1 30 release-floor pci=remove mode=s2idle hold=60 \
    gdsc=pcie0 restart-ok
```

Expected if the PIPE clock was the cause: no `stuck at 'off'`, and the
cycle's pcie evidence says `PCIe came back` with `PCIe Gen.2 x1 link up`.
The clock lines say whether the UFS host's counts stay low through the
sleep, and `qcom_stats` and the DDR times whether the GPU and UFS fixes
moved the depth. A `gdsc=usb` run comes only after the owner has looked at
these: on revision 9 it cost the storage once.

`fstrim.timer` (weekly) would, if it fired during a run, send discards to
the read-only disk, which the block layer logs as a write and the run
fails; it is not stopped for the run.

**Evidence around each cycle.** Since the baselines, every run also prints
read-only evidence (`evidence:` lines on the terminal and in the run's
log; nothing of it decides a verdict, and a read that fails gives `-` or
nothing):
- once, after the protection and before the first read: every line of
  `pm_genpd_summary`, the regulators in use and their users
  (`regulator_summary`), the interconnect nodes with a vote and the requests
  that make it (`interconnect_summary`; a request whose tag is 0 or has the
  sleep bit is marked `(sleep set too)`: it holds bandwidth while the CPUs
  sleep), the wakeup sources with events, and each PCIe function's power
  state, link, `d3cold_allowed` and wakeup;
- before each sleep: the runtime state and clock of the UFS host, the USB
  controller's runtime state and wakeup, each PCIe function's power state,
  the DSPs (`remoteproc`), the panel's backlight, the warmest thermal zone,
  the enable and prepare counts of XO and of the UFS host's clocks
  (`cycle N/M: evidence: before: clocks (enable/prepare): bi_tcxo 6/6,
  tcsr_ufs_pad_clkref_en 522/522, gcc_ufs_phy_axi_clk 522/522,
  gcc_ufs_phy_ahb_clk 525/525, gcc_ufs_phy_unipro_core_clk 524/524,
  gcc_ufs_phy_ice_core_clk 525/525`, from `clk_summary`; `clk_summary not
  readable` without it), and the power domains of the rails, the UFS, USB
  and PCIe and any that is on;
- after it: what every `qcom_stats` file counted during the sleep (the
  SoC's `aosd`, `cxsd`, `ddr`; the subsystems, `apss` being the CPUs', with
  the time each spent asleep; `ddr_stats`' low-power modes and the time at
  each DDR frequency), the wakeup sources and the interrupts that fired,
  whether the PCIe link went down and came back (`PCIe Gen.N xN link up`
  in the cycle's kernel lines; none means the PCIe host kept its link,
  clocks and votes through the sleep) and whether the PCIe host came back
  at all (`PCIe came back (no host failure line)`, or `PCIe did not come
  back (qcom-pcie 1c00000.pcie: Host init failed: -16)`), the same clock
  counts as read right after the wake (`after: clocks`), the same power
  domains, and the cycle's kernel lines (the PM core's, the CPUs', the
  storage's, USB's, PCIe's, the DSPs', warnings and errors; at most 150,
  without the Wi-Fi's per-packet lines and the hung-task reports).

The clock counts are for revision 10's UFS fix: with it, ufshcd enables
the UFS host's clocks once while UFS works and not while it idles, so
`gcc_ufs_phy_axi_clk`, `gcc_ufs_phy_unipro_core_clk` and
`tcsr_ufs_pad_clkref_en` read 0 or 1, and the inline crypto engine's
driver holds `gcc_ufs_phy_ice_core_clk` and `gcc_ufs_phy_ahb_clk` from its
probe on, so those read 1 to 3. Counts in the hundreds are the leak.
Reading `clk_summary` resumes the clock controllers for a moment; the run
reads it once before each sleep, before its other reads, and once right
after the wake.

pmsg gets three short lines of it per cycle (`cycle N/M: evidence: ...`,
the `qcom_stats` line, one with the PCIe link, whether the PCIe host came
back, the top interrupts, the wakeup sources and the state before, and
one with the clock counts before and after in short names: `clocks before:
xo 6/6, pad_clkref 0/0, axi 0/0, ahb 1/1, unipro_core 0/0, ice_core 1/1;
after: ...`), between the cycle's diagnostic lines and its verdict. Reading
`qcom_stats/ddr_stats` sends the AOSS a request to update its numbers, as
every read of that file does; everything else is a plain read.

**The experiment options** (2026-11-22). Each is off unless given, so a run
without them does what it did before. They are for telling apart why a deep
sleep with the PCIe root port removed, on a USB cable to a PC, never came
back. The run's header line, its first pmsg line and every `RESULT` line
name the ones in force (`options: none` without them).
- **`pci=remove`** (`ro` and `dry`): the run removes every PCI root port
  itself, and with it everything below it (the Wi-Fi). While the root port
  is on the bus the PCIe host keeps its link, PHY, clocks and bandwidth
  votes through every suspend; with the bus empty it powers down. A root
  port is a PCI device directly on a host bridge (its sysfs parent is
  `pciDDDD:BB`) with the class of a PCI bridge (`0x0604xx`); on the SM-X710
  that is `0000:00:00.0`, with the Wi-Fi `0000:01:00.0` below it (pcie1 is
  disabled). The run is refused, with nothing changed, if there is no root
  port, or if a PCI device is neither a root port nor below one (removing
  the ports would not empty the bus). The removal is the last step before
  the first freeze, in RAM, with the disk still writable: `echo 1 >
  /sys/bus/pci/devices/<port>/remove` for each port, the device count
  before and after in the log and in pmsg, then 10 s to settle. A bus that
  is not empty after it stops the run before anything is frozen, with no
  restart. **Wi-Fi is gone until the restart**, and an SSH session over
  Wi-Fi drops at that point while the run goes on: run it over the USB
  cable (`usb0`), or read the result in pstore after the restart. The run
  says so before it moves to RAM. With the bus empty the host powers down
  in every suspend and comes back at the resume; if its link does not
  come back, the host is left powered down but marked suspended, and the
  next suspend would read its registers with its clocks off. So a cycle
  (or an aborted attempt) whose kernel lines say the PCIe host did not come
  back (`Link failed to come up`, `Host init failed`, `failed to resume
  ...: error`) ends the run with `SLEEP-TEST RESULT: PCIE-DOWN after cycle
  N/M: ...` and the restart. Until a run has shown the link back (`PCIe
  Gen.2 x1 link up` in a cycle's `pcie` evidence), give `pci=remove` one
  cycle.
- **`pm_test=LEVEL`** (`ro`; `freezer`, `devices`, `platform`,
  `processors` or `core`): each cycle's suspend stops at LEVEL, waits there
  5 s (`pm_test_delay`) and resumes, with no SoC sleep call. `freezer` stops
  after the task freeze; `devices` after the devices' suspend phase, before
  their late and noirq phases; `platform` after their noirq phase;
  `processors` with the other CPUs offline as well; `core` after the
  syscore suspend with interrupts off, short of the SoC sleep call (PSCI
  `SYSTEM_SUSPEND`) alone. `/sys/power/pm_test` is set right before each
  `rtcwake` and back to `none` right after it returns, so no end of the run
  leaves it set. `rtcwake` returns 0 and disables the RTC alarm it set. A
  cycle is **PM-TEST** (`SLEEP-TEST cycle 1/3: PM-TEST platform: the
  suspend went as far as pm_test=platform (...), waited there and came
  back with no SoC sleep call, and the UFS read back as before; qcom_stats
  aosd +0 cxsd +0 ddr +0 (CPU idle during the wait, not a depth test);
  ...`), never PASS, SHALLOW or DEEP: no sleep is asked for, so no depth is
  tested. cpuidle keeps running during the wait (it is paused only when
  the other CPUs go offline), but from `devices` on it cannot take the CPU
  cluster's power domain down: that domain's one device is the RPMh RSC
  (`17a00000.rsc`), the suspend's prepare phase counted it, and genpd powers
  no domain off while one of its devices is prepared. So at `devices` and
  `platform` the CPUs reach their own power collapse at most, and `aosd`,
  `cxsd`, `ddr` and `apss` can only stay +0 (as on 2026-11-22 at 14:00);
  only at `freezer`, before anything is prepared, can idle take the cluster
  down. Until that evening this README said the idle CPUs could take the
  SoC down at `devices` and `platform`: that was wrong. A cycle whose
  kernel lines lack `suspend debug: Waiting for N second(s).` (the kernel's
  line at the level) fails. With `mode=s2idle` the levels are `freezer`,
  `devices` and `platform` only: suspend-to-idle goes into its idle loop
  right after the `platform` level, and the kernel refuses `processors`
  and `core` with it; the run refuses them first. Refused with `dry`, when
  the kernel does not offer the level, or when a level is selected
  already.
- **`trace`** (`ro`): for each sleep, the console loglevel (the first number
  of `kernel.printk`) goes to 8, and back right after `rtcwake` returns;
  `pm_debug_messages` is 1, as every run with sleeps sets it (`dry` leaves
  it as it is): the restart that ends the run is what puts it back, and a
  stop before any sleep says that it stays 1 until the next boot. The PM
  core's lines and its phase lines (`PM: noirq suspend of devices complete
  after ...`) then
  reach the consoles, ramoops' among them. With `console_suspend` at its
  default the consoles are suspended from `printk: Suspending console(s)`
  until the resume: what reaches ramoops while it happens are the lines up
  to that one (the freezer's); the rest is printed at the resume. If the
  tablet never comes back, the last line of the console record says
  whether it got past the task freeze (`Suspending console(s)` last: the
  hang is in the devices' suspend, the sleep or the devices' resume).
  Each console line costs 0.1 to 0.25 s here with interrupts off (the
  hang-log kit measured it): about 10 lines before the consoles are
  suspended and, estimated, 60 to 100 at the resume, so a sleep takes up to
  half a minute longer to return. `pm_print_times` is not used: it prints
  two lines per device callback, thousands per sleep, which at that cost
  would take minutes; `trace` is refused while it is 1.
- **`trace=live`** (`ro`): `trace`, and `console_suspend` N for each sleep
  (back right after), so that the lines reach ramoops while the devices
  suspend and resume, and a hang leaves its last phase line. **Read the
  risks first**, all read from the source, none tried on this tablet: each
  line's 0.1 to 0.25 s with interrupts off now falls inside the suspend and
  resume, the noirq phase included, which can stretch timeouts there; the
  debug UART stays powered through the sleep (serial core keeps a console
  port awake when `console_suspend` is N and never turns its clocks off),
  so the SoC may not reach the state a run without it reaches, and the
  run says nothing about depth; the boot console `qcom_geni0` writes the
  same UART with no power management of its own, which is safe only while
  that port stays clocked. Writing to ramoops and to `sec_log` is a copy
  into reserved memory, safe at any point. Use it after a `trace` run left
  `Suspending console(s)` as the last line.
- **`mode=s2idle`** (`ro` and `dry`; `mode=deep`, the default, is accepted
  too): each cycle is `rtcwake -m freeze -s SECONDS`, suspend-to-idle,
  instead of `rtcwake -m mem`, which this kernel carries out as PSCI
  `SYSTEM_SUSPEND`. `rtcwake` writes `freeze` to `/sys/power/state`;
  `/sys/power/mem_sleep` plays no part and the run leaves it as it is. In
  suspend-to-idle the CPUs stay online and idle in their state with an
  s2idle entry (`state1`, `cpu-sleep-X-0`); the last one down takes the CPU
  cluster's power domain down, which flushes the RPMh RSC's sleep set, and
  enters PSCI `CPU_SUSPEND` with the cluster's deepest state; the RTC's
  alarm ends it. It is the path the 2026-11-22 12:05 failure did not take
  (`docs/porting-log.md`). Refused, with nothing changed, when
  `/sys/power/state` offers no `freeze` or no cpuidle state of cpu0 has an
  s2idle entry, and with `pm_test=processors` or `core`. `dry` checks the
  same and sleeps not. Every cycle's verdict word, and every `RESULT`'s
  about a sleep, carries ` (s2idle)` (`SLEEP-TEST cycle 1/3: PASS (s2idle):
  ...`, `SLEEP-TEST RESULT: SHALLOW (s2idle): ...`; the freezer's is
  `NO-SLEEP (freezer, s2idle)`; `STOPPED`, `FAIL before any sleep` and
  `DRY` name it in their options alone), the options name `mode=s2idle`,
  the progress line before each sleep says `rtcwake -m freeze`, and the
  header says how many CPUs have which s2idle state and the cluster
  domain's idle states. A
  line from a run without `mode=s2idle` (every run before the evening of
  2026-11-22) is from `deep`. Around each cycle the run also reads, and
  prints as `cycle N/M: evidence: s2idle: ...` (a third evidence line per
  cycle in pmsg): the s2idle entries of every CPU
  (`cpuidle/state*/s2idle/usage`; more than one per CPU means CPUs woke and
  went back to sleep inside it), the cluster domain's idle states
  (`/sys/kernel/debug/pm_genpd/power-domain-cluster/idle_states`: `S1`
  usage and s2idle +1 when the cluster went down, rejected +1 when the RSC
  refused), the time asleep (the cycle's `Timekeeping suspended for N
  seconds` lines added up, one for each time every CPU was frozen at once;
  `cpuidle/state*/s2idle/time` leaves that time out and is not the sleep),
  the wakeup IRQ by name, how long `rtcwake` took, and `suspend_stats`'
  `last_hw_sleep` when the kernel has it (this one does not). The
  verdicts:
  - **PASS (s2idle)**, **SHALLOW (s2idle)**, **DEPTH-UNKNOWN (s2idle)**:
    decided by `aosd`, `cxsd` and `ddr` as in a `deep` run, with the
    s2idle evidence in the line. A cycle whose three counted is a PASS
    whatever else: the SoC reached the deep states through
    suspend-to-idle and came back.
  - **WOKEN EARLY (s2idle)**: the kernel counted the suspend (`rtcwake`
    0, a success counted), but a wakeup other than the RTC's alarm cut it
    short: no CPU entered its s2idle state (a wakeup between the devices'
    noirq phase and the idle loop ends the loop at once, and the suspend
    still counts as a success), or less than half of SECONDS was spent
    asleep; and the wakeup is on record (`/sys/power/pm_wakeup_irq`, or the
    kernel's `PM: Triggering wakeup from IRQ N`, names an IRQ whose name has
    no `rtc` in it, or a wakeup source was pending). Not a pass: the line
    names the wakeup (the touchscreen with its hint), and the cycle is
    tried again after 5 s like an ABORTED attempt, counted in the same
    `aborts=N`; one more ends the run with `NO-SLEEP (s2idle)`, whose
    RESULT counts `attempts aborted before the sleep or woken early`. A
    short sleep that the RTC's alarm ended is no early wake: it is judged
    as a sleep (SHALLOW, unless the deep states counted).
  - **FAIL (s2idle)**: as in `deep`, and also a cycle in which no CPU
    entered its s2idle state when no such wakeup explains it.
  - **ABORTED (s2idle)**, **NO-SLEEP (freezer, s2idle)**, **PM-TEST LEVEL
    (s2idle)**, **PCIE-DOWN (s2idle)**: as in `deep`; a PM-TEST cycle
    never enters the idle loop (`no suspend-to-idle`).
- **`gdsc=LIST`** and **`cxmx=ret`** (`ro` and `dry`; the kernels of
  revisions 9 and 10):
  the debug switches (above, "Revision 9"). `gdsc=` takes one or more of
  `pcie0`, `pcie0phy` and `usb`, comma-separated, and is refused without
  `pci=remove`: with the PCI devices kept the PCIe host stays up through
  the sleep, and a PCIe GDSC that collapses would take the controller away
  from a live host. Refused too, with nothing changed, when the kernel does
  not offer a switch asked for (revisions 8 and before), or when any switch
  it offers is on already (set by hand: put it back, `echo N | sudo tee
  FILE`, or restart). The run writes `Y` to each switch asked for right
  before the freeze, after `pci=remove`'s removal, and reads each back; one
  that does not read `Y` stops the run before anything is frozen, with the
  ones that took put back to `N`. After that nothing puts them back but the
  restart that ends the run. Every run, with switches or not, prints them as
  they read: the header (`debug switches: pcie0 N, pcie0phy N, usb N, cxmx
  N (...)`, or `debug switches: not offered by this kernel` on revision 8's
  and older kernels), a progress line in pmsg right after the write, the
  progress line before each sleep, every cycle's verdict and every `RESULT`
  line (`...; floor released; switches: pcie0 Y, pcie0phy N, usb N, cxmx
  N`, and `; options: pci=remove gdsc=pcie0; switches: ...` at the end of a
  `RESULT`). So the pstore record of a run that never comes back says which
  were set for the sleep it entered. With `gdsc=usb` while the tablet is USB
  host the run says the xHCI is not expected to survive: run undocked.

**The interconnect floor.** `ubuntu-gts9u-icc-late-release` releases it
about 45 s into a boot. After a boot that released it and then ended
without a clean shutdown it holds it once (its loop breaker), and it holds
it while `/etc/ubuntu-gts9u/icc-hold` exists. A sleep test always ends that
way for the breaker: its marker can only be cleared by a clean stop with a
writable disk. So the boot after a run in a boot that released the floor
holds it once, and the next run needs `release-floor`, which restarts the
unit, the documented way to release it now (before anything is
frozen). `release-floor` is refused while `icc-hold` exists; remove that
file yourself if you mean to.

**The result after the restart.** `/run` is gone then. The verdict and
diagnostic lines are in pmsg, which the boot after the restart archives
under `/var/lib/ubuntu-gts9u/pstore`, one directory per boot, the newest
last (readable by group adm; otherwise prefix `sudo`):
`grep -rah 'gts9u sleep-test' "$(ls -d /var/lib/ubuntu-gts9u/pstore/[0-9]* | tail -n 1)"`
(`sleep-test.sh status` prints that command when there is no run in
`/run`). From the build host:
`ssh -o HostKeyAlias=10.42.0.2 jstockdale@192.168.1.234 'grep -rah "gts9u sleep-test" "$(ls -d /var/lib/ubuntu-gts9u/pstore/[0-9]* | tail -n 1)"'`.
After a later restart the run's lines are in an older directory: the
stamp in each line tells the runs apart. During the run, in an SSH session
opened before it, `sh ~/typec-roles/sleep-test.sh status` or, if the storage
is gone and no program starts,
`while IFS= read -r l; do echo "$l"; done < /run/gts9u-sleep-test/latest/result`
(built into the shell). A new SSH login may not get through while the disk
is frozen (the login writes to it): the lines are in pmsg after the
restart. If the tablet is still up 30 s after `sysrq b`, everything stays
frozen (`sudo`, `reboot` and a new login may hang): force the restart with
Power and Volume Down held; for TWRP, hold Power and Volume Down until the
screen goes dark, then at once Power and Volume Up.

**Reading the diagnostic** (the kernels of revisions 8 to 10). After each
resume the run prints the diagnostic's lines of the cycle. `calibration
entry:` shows the PCS and SerDes state the calibration started from. What
follows is inferred from the registers, not proven. `SW_RESET 0 START 0x1`
after a resume would be what the last calibration left: the PCS kept its
registers through the sleep, the case revision 8's reset was written for.
It has not been seen: every entry on record, 11 that passed and 15 that
failed, reads `SW_RESET 0x1 START 0x0`, a PCS fresh from its reset.
`PWRDN` is the analog power-up bit that the PHY's power-on sets right
before the calibration: `0x1` in every calibration that passed, `0x0` in
every one of the 2026-11-23 failure. Revision 10 logs a line before each
entry, `power-up write: PWRDN A right after it, B 100 us later`: `0x1` and
`0x1` with `PWRDN 0x0` at the entry means the bit was undone after the
power-on; `0x1` then `0x0`, undone within 100 us; `0x0` right after the
write, the write did not take (the PHY lacked an input it needs, its core
supply L1D the first suspect). If a cycle fails with `phy initialization
timed-out`, the lines after it say what to try next:
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
For the UFS PHY it fails on a calibration or UFS host failure this boot (a
PCIe host's or PCIe PHY's line in the same words is not one), and
otherwise counts the diagnostic's `calibration entry` lines: on the kit's
kernel that line, logged from inside the calibration that now resets the
PCS on both tablets, is what can be seen of revision 8 from userspace (the
reset itself logs nothing; `make-kit.sh` checked the code on the build). On
another kernel it notes that nothing tells. For revision 10 it also wants a
`power-up write` line for each calibration on the kit's kernel and shows
the last one, fails on a PCIe host or PCIe PHY failure this boot and, on
the kit's kernel, on a boot without a PCIe link-up line, fails on a GPU
RSC sequence that timed out (`Unable to power off the GPU RSC` and the
like), and under `sudo` reads the UFS host's clock counts from
`clk_summary`: more than 3 on the kit's kernel fails (the leak), and on
another kernel they are a note.

What a passing dock looks like in `trial-N.tcpm.log` is in the write-up:
`SNK_READY -> AMS_START`, `Requesting DFP before discovery`, `AMS_START ->
DR_SWAP_SEND -> DR_SWAP_CHANGE_DR -> SNK_READY`, then one discovery. A dock
that answers Wait shows `DR_SWAP_SEND -> DR_SWAP_CANCEL` and the request
again, up to three per attach; the verdict is still the roles.

## Physical checks still to run (owner)

1. After installing and rebooting: `verify.sh`. On revision 8 it should
   count at least one `calibration entry` and no UFS failure; on revision 9
   also `ok   debug switch ... = N (off)` for each of the four; on revision
   10 also the PCIe link up at boot, no GPU RSC failure, a `power-up write`
   line per calibration, and under `sudo` the UFS host's clock counts at 3
   or less. Then check that Wi-Fi works: revision 10 switches PCIe0's pipe
   clock without `unpark-pcie0-pipe-mux.patch`.
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
   restarts the tablet), without touching the screen: first `dry`, and a
   check that the boot after it is clean (`verify.sh`, which knows the boot
   after a sleep test and checks that the root's journal was empty at the
   mount, and the root's ext4 `errors_count` 0); then on revision 7, before
   the install, a baseline
   (`release-floor` if the floor is held): does revision 7 fail every time
   or one time in N? Then install revision 8, reboot, `verify.sh`, and the
   same run docked, then undocked. Only after `PASS` with `floor released`,
   docked and undocked, does a sleep with a writable root follow. Battery
   drain over a night of sleep, against the hang-log kernel, is the check
   for what the USB power domains' retention costs, and comes after that.
   Done on 2026-11-22: `dry` and two revision 7 baselines, 13 of 13 cycles
   resumed, all SHALLOW ("What has run on the tablet", above). No run has
   reached a PASS yet: no recorded sleep of the SM-X710 has counted AOSS
   sleep, CX collapse or DDR low power. Revision 9's runs ("Revision 9:
   the runs", above) came first; revision 10's run comes before any of the
   rest.
7. Bluetooth, Wi-Fi, audio and the rear camera at a known size: the kernel was
   rebuilt. Revision 10 changes the PCIe PHY's pipe clock (Wi-Fi), the GPU's
   power-down and power-up, and the UFS host's clocks; watch for GPU hangs
   and UFS errors in normal use.
