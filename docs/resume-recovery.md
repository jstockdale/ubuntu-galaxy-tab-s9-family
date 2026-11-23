# Resume failure and recovery, 2026-09-09

> **Status, 2026-11-07: superseded.** This is the SM-X910 record of
> 2026-09-09. The UFS fixes are in the default kernel: the baseline SERDES
> patch, and since the kernel #11 validation the PCS-reset change described
> below (see [NPU status](npu-status.md)). The sleep-disable file described
> below was removed after the kernel #10 normal-root cycles
> ([RAM-root diagnostic](ramroot-diagnostic.md)). If an old test tablet still
> has `/etc/systemd/sleep.conf.d/90-gts9u-ufs-recovery.conf`, delete it to
> re-enable suspend. Since 2026-11-22 the PCS-reset assertion is no longer
> gated on the board: every SM8550 PHY gets it, the SM-X710 included, after
> the SM-X710 failed the same way (see [SM-X710](#sm-x710-gts9wifi) at the
> end).

Automatic brightness was validated by changing the room lighting, and the
owner confirmed that it still worked after a power-button suspend. That
cycle nevertheless exposed an intermittent **storage resume failure**.
Deep suspend must not be described as reliable on that build (build 8,
`7.2.0-rc3-dirty #8`); later kernels carry the board UFS fixes.

## Captured failure

The running kernel was `7.2.0-rc3-dirty`, build 8. Kernel timestamps:

| Seconds since boot, excluding sleep | Event |
| --- | --- |
| 968.994 | Deep suspend starts; filesystem sync succeeds |
| 970.367 | ath12k reports Wi-Fi firmware initialization |
| 970.371 | QMP UFS PHY initialization times out; calibration returns -110 |
| 971.328 | UFS resume fails with -5 |
| 972.446 | System resume completes despite the device failure |
| 973.735 | ext4 aborts the journal and enters emergency read-only mode |
| 1072.956 | Wi-Fi reassociates |

SSH returned, but root was `rw,noatime,emergency_ro`; uncached reads and
writes failed. On this split test SM-X910 the root filesystem is internal UFS
`/dev/sda35`, partition label `linuxroot`, ext4 label `UBTS9U_UFS`, not the
microSD. A whole-tablet install has its root in `userdata`; the SM-X710
always does, with ext4 label `UBTS9_UFS`. Memory pressure
was not the cause. The owner also reports occasional black-screen resumes
after idle or lid closure while charger sounds still work. That symptom
has not yet been proven to share the same cause.

## Offline repair and local safeguard

With the owner's help, the tablet entered TWRP. The Ubuntu partition was
verified unmounted before checking it. A read-only e2fsck found pending
journal recovery. The first 2 MiB were backed up locally before writing.
Journal-only preen recovered the journal and cleared already-unlinked
orphan inodes; full preen corrected free-space counters. A final read-only
five-pass check reported no remaining consistency errors. No Android
userdata or partition-table changes were made.

The same procedure, for any install (it is also what a TWRP update ZIP means
by "Run e2fsck manually from TWRP before flashing again"). From the PC, with
the tablet in TWRP, run `adb shell` and then:

```sh
P=/dev/block/by-name/linuxroot     # split SM-X910
P=/dev/block/by-name/userdata      # whole-tablet install (every SM-X710)
blkid "$P"                         # LABEL="UBTS9U_UFS" (X910) or "UBTS9_UFS" (X710)
grep "$(readlink -f "$P")" /proc/mounts   # must print nothing
e2fsck -n "$P"                     # read-only first look
dd if="$P" of=/tmp/root-head.img bs=1M count=2
# on the PC, in a second terminal: adb pull /tmp/root-head.img
e2fsck -p -E journal_only "$P"     # replay the journal
e2fsck -fp "$P"                    # full preen
e2fsck -fn "$P"                    # read-only re-check: must be clean
```

Set only the `P=` line that matches the install. TWRP mounts `userdata` as
Internal Storage, so on a whole-tablet install the `grep` usually lists
`/data` (and `/sdcard`): unmount each listed mount point (`umount /sdcard`,
`umount /data`) before going on. Pull the 2 MiB backup to the PC before the
first writing step; TWRP's `/tmp` does not survive a reboot. If
`e2fsck -fp` reports an unexpected inconsistency and asks for a manual run,
run `e2fsck -f "$P"` and answer its questions, then repeat the final
read-only check.

The original Ubuntu boot set remains installed, including boot SHA-256
`a48a7d1b81e27641683fc930b5f9c712990e3748121b09b1cc82bd33e5d9ac8f`.
After reboot, root was writable, a 4 MiB fsync/readback check passed,
Wi-Fi connected, HTTPS returned 204 with successful TLS verification,
and libssc again delivered light samples. Sensor packages remain
`libssc 0.4.4-gts9u3` and `iio-sensor-proxy 3.9-gts9u3`.

The test tablet then had a **temporary local** file (since removed; see the
status note at the top)
`/etc/systemd/sleep.conf.d/90-gts9u-ufs-recovery.conf` setting all four
`AllowSuspend`, `AllowHibernation`, `AllowSuspendThenHibernate`, and
`AllowHybridSleep` options to `no`. Logind reports `CanSuspend=no`.
This prevents ordinary idle/lid/power-button suspend from reproducing the
failure unattended, but increases standby power consumption and disables
the short power-button suspend action. It is a safeguard, not a UFS fix,
and is not silently included in future images. Keep it until controlled
resume validation is possible; remove that one file when deliberately
resuming those tests. Direct kernel PM test writes bypass this policy.

## Separate cold-boot panel failure

On the first boot after repair, GDM was running but the panel ID remained
`00 00 00`. The existing platform-test recovery helper had exited with EIO:
the userspace freezer was aborted after 2.1 seconds, before device suspend.
Repeating the helper succeeded and restored panel ID `80 00 04`, without
UFS errors. This is different from the storage failure above.

The helper now retries at most three times **only when the kernel's
`failed_freeze` counter increases**. It restores `pm_test=none` between
failed attempts and on exit, waits two seconds between attempts, and does
not retry a device suspend/resume failure. The service timeout is 90 seconds.
Four shell-harness tests cover success, transient freezer abort, retry
exhaustion, and refusal to retry a device failure. Both changed files are
in the device package source and installed on the tablet.

The final reboot completed the platform cycle on its first attempt, recovered
panel ID `80 00 04`, and reached an active GDM Wayland greeter. Mutter reported
`PowerSaveMode=0`, panel brightness was 628/2047, root was `rw,noatime`,
SensorProxy advertised ambient light, HTTPS returned 204, and systemd had
no failed services. The retry branch is covered by the harness; it did not
need to run on that final boot. This confirms the handoff state, not a fix
for intermittent deep-resume failures.

## UFS investigation still pending

The Samsung Kalama driver explicitly asserts PCS `SW_RESET` before writing
PHY calibration tables and clears it afterwards. The running mainline
driver only clears PCS reset during calibration. On PHYs with PCS reset,
its separate external reset handle is not acquired. This is a concrete
sequence difference worth testing, **not a validated fix**. Do not deploy
an experimental kernel or repeat deep suspend with writable UFS merely
because the platform PM test passes.

The upstream SM8550 policy already selects UFS power level 5 because PHY
retention is unsupported. Simply selecting a lower UFS power level is
not an established workaround; the clock-off path also powers down the
PHY. See the [upstream suspend-fix series](https://patches.linaro.org/project/linux-scsi/cover/20241219-ufs-qcom-suspend-fix-v3-0-63c4b95a70b9%40linaro.org/).

Private diagnostic captures, offline fsck transcripts, the original boot
backup, and copies of both PHY implementations are under the ignored
`work/als-oneui-20260909/` directory. No SSH password belongs in this document
or any published artifact. Release 1.2.0 preparation has not been started
as part of this recovery.

## PCS reset candidate, 2026-09-09 (not deployed)

The pinned upstream source confirms that `qmp_ufs_phy_init()` does not acquire
the external reset on PHYs with PCS reset. Calibration therefore clears PCS
reset without explicitly asserting it first. The published
[Kalama implementation](https://github.com/LineageOS/android_kernel_oneplus_sm8650/blob/638ecc42531912963b2a0d9eddf6b1ae3be9bb37/drivers/phy/qualcomm/phy-qcom-ufs-qmp-v4-kalama.c)
asserts PCS reset before its tables. Upstream commit
[`a079b2d71534`](https://github.com/torvalds/linux/commit/a079b2d715340482e425ff136b55810ab8279800)
deliberately removed reset and SerDes stop from **power-off** according to the
hardware programming guide; this candidate does not restore that sequence.

`kernel/patches/qmp-ufs-assert-pcs-reset-before-calibration-gts9u.patch` instead
asserts PCS reset immediately before calibration tables, for the SM8550 PHY
(until 2026-11-21 only on `samsung,gts9uwifi`; see the SM-X710 section). The
existing readback in `qphy_setbits()` completes that write before table
programming. No PHY tables, polling timeouts, UFS power levels, device tree,
wake sources or other drivers are changed.
The missing assertion is a hypothesis, **not proof of the failure's cause**.

When this was written the build recipe enabled it only with
`UFS_PCS_RESET_EXPERIMENTAL=1`. Since the kernel #11 validation it is on by
default: `build-mainline-kernel.sh` applies the baseline
`qmp-ufs-reset-serdes-before-power-down-gts9u.patch` and then stages this
change on every build. Set `UFS_PCS_RESET_EXPERIMENTAL=0` with a fresh
`KERNEL_WORKTREE` to build without it; a tree that already contains it is
refused when the variable is 0. Running the stage script by hand is needed
only for a tree prepared outside `build-mainline-kernel.sh`, and that tree
must already carry the baseline SERDES patch:

```sh
bash scripts/stage-ufs-resume-experiment.sh /path/to/kernel-tree
```

`scripts/test-ufs-resume-experiment.py PRISTINE_QMP_UFS_C` applies both patches
with zero fuzz, checks that `UFS_PCS_RESET_EXPERIMENTAL=0` is a no-op, that the
default applies the change idempotently, and that 0 or invalid values are
rejected on a tree already carrying it, and compiles the actual calibration function with mocked
MMIO. It checks reset-before-tables ordering, platform isolation, repeated
calibration and error propagation. These tests passed against the pinned
`a13c140cc289c0b7b3770bce5b3ad42ab35074aa` source; they cannot validate physical
power sequencing or resume. The baseline RX-state patch also needed a trailing
context repair for strict application; its build marker now uses the helper
name rather than a comment changed in the previous commit.

The next physical test must have a recovery path and keep internal UFS
filesystems unmounted (for example a diagnostic RAM-root boot). Do not format
the owner's currently inserted WINPE microSD or use it as a disposable root.
Before enabling normal suspend, validate repeated real deep cycles and UFS
reads, then normal-root filesystem writes and existing hardware functions
under supervision. A platform PM test alone is insufficient. The local sleep
safeguard remained in place then (it has since been removed) and no reboot
is implied by staging or building.

### Build-only result on PC-ARTURO

The existing runtime9 source snapshot (`5e34b887d`, with the validated #8
performance and fingerprint edits) was built incrementally with only the
additional PCS reset candidate. Backup and candidate artifacts are under
`/root/ubuntu-gts9u/build/ufs-resume-20260909.9EDJeS/` on PC-ARTURO:

- `validated-kernel8/` preserves the original Image, vmlinux, System.map,
  configuration, Module.symvers, PHY source/object and version metadata.
- `candidate-kernel9/Image` SHA-256:
  `0bbfe08b67d976044f37ebaaf095665e369da913f3b5f0d8ac37fee85c083a23`.
- Build identity: `#9 SMP PREEMPT Wed Sep 9 10:46:40 CEST 2026`, release
  `7.2.0-rc3-dirty`. LLVM 22.1.8, `ARCH=arm64 LLVM=1 LOCALVERSION=-dirty
  KBUILD_BUILD_VERSION=9 KBUILD_BUILD_TIMESTAMP='Wed Sep 9 10:46:40 CEST 2026'`,
  target `Image`, eight jobs, existing source and object directories.
- Configuration SHA-256 remains
  `474a26a731694fb6838f463e21a057ff3e272fe27fc42b3d6b9d3b628070fcb3`;
  signing key, certificate and Module.symvers hashes also remained unchanged.
  No modules were replaced or signing keys regenerated.
- The first compile omitted `LOCALVERSION=-dirty`, producing an incompatible
  `7.2.0-rc3+` release. That output is isolated in
  `rejected-release-mismatch/` and **must not be installed**. The second,
  matched compile above is the candidate. Both logs are retained locally.

The build tree now contains the experimental PHY source and #9 objects;
it is no longer an untouched #8 build directory. No boot image was packed,
flashed or installed, and the tablet still runs #8 with suspend disabled.
Physical validation and a RAM-root diagnostic boot are still pending.

### Subsequent GPU/UFS combined candidate

The later desktop freeze exposed a GPU recovery deadlock with root still
writable. Kernel #10 now combines the #9 UFS candidate with a bounded GMU
fault-capture wait; it has now passed three RAM-root deep/RTC cycles and is installed for
normal desktop validation. See [RAM-root results](ramroot-diagnostic.md);
long-duration and lid/idle acceptance remain pending. See
[GPU recovery](gpu-recovery.md) for the traces, tests, hashes, preserved #9
backup and remaining requirements for a combined physical test session.

## SM-X710 (gts9wifi)

The baseline SERDES patch has no board check: it applies to every SM8550 UFS
PHY, so SM-X710 kernels carry it. Up to 2026-11-21 the PCS-reset assertion
was gated on `samsung,gts9uwifi` (an `of_machine_is_compatible()` check), so
SM-X710 kernels did not get it. The 2026-11-06 note here asked to widen the
gate only once UFS resume errors appeared on the SM-X710.

### The failure of 2026-11-21

They did on 2026-11-21 at 23:34, on Type-C kit revision 7 (kernel build ID
`84c2748b5ee4`, boot `82555513`), in one `sudo rtcwake -m mem -s 30` with the
tablet on a powered dock (power sink, USB host):
`qcom-qmp-ufs-phy 1d80000.phy: phy initialization timed-out`, `Failed to
calibrate PHY: -110` and `Enabling the controller failed`, 15 times each;
five `ufshcd_host_reset_and_restore: Host init failed -5`;
`ufshcd_wl_resume failed: -5`; then 10 I/O errors on `sda` (two of them
writes that ext4 reported as `potential data loss`), an ext4 journal abort
on `sda34` and a read-only root. The same messages as the SM-X910 record
above. The USB host came through the same sleep. The tablet was
restarted with sysrq `b`. (`~/ubuntu-gts9u/backups/squirtle-sleep-20261121-2334/`
holds the kernel log.)

How deep that sleep was matters:
- That boot's interconnect floor was released at 46.81 s (22:41:13) by
  `ubuntu-gts9u-icc-late-release` (`released 24100000.interconnect
  interconnect-1; INT_MAX nodes 43 -> 0`; the same unit reported
  `not touched: rpmhpd=1 gcc=1 gpu_cc=1`). The sleep ran with the floor
  released, and with rpmhpd synced since boot.
- The same boot's suspend cycle for the panel (`pm_test=platform`, at
  22:40:48, with the floor still held) suspended and resumed the UFS
  controller, a PHY calibration included, without an error, as every boot's
  has. That cycle stops short of the SoC's sleep.
- The two real sleeps of 2026-11-06 that resumed (one deep, one s2idle) were
  on the baseline kernel, before rpmhpd synced at boot and before the floor
  was released at run time. This tree's notes record no real deep sleep on
  the SM-X710 between those and this one.
- The boot after the forced restart (`788ce779`) held the floor: the loop
  breaker, since boot `82555513` had released it and ended without a clean
  shutdown. Every boot after a forced restart does, so a sleep test in such
  a boot does not reach the depth of this failure unless the floor is
  released first.

The kit's `vmlinux` confirms the gate held: its `qmp_ufs_phy_calibrate()`
calls `of_machine_compatible_match()` (the inline `of_machine_is_compatible()`
with `samsung,gts9uwifi`) before the SW_RESET write, and squirtle's root
compatible is `samsung,gts9wifi`.

### The fix: the reset on every SM8550 PHY

The assertion now goes to every SM8550 PHY, without a board check
(`kernel/patches/qmp-ufs-assert-pcs-reset-before-calibration-gts9u.patch`).
That the missing reset caused the failure is probable, not proven:
- mainline never asserts the PCS reset of this PHY after its first
  calibration (`qmp_ufs_phy_init()` returns before taking the external reset,
  and calibrate only clears SW_RESET and sets SERDES_START, which nothing
  else in the driver writes), so on a PCS that kept its registers through a
  power collapse both writes change nothing;
- Samsung's kernels assert it before every calibration, on both tablets:
  the SM-X710 and SM-X910 EUR 15 sources carry byte-identical
  `phy-qcom-ufs-qmp-v4-kalama.c`, `phy-qcom-ufs.c` and `ufs-qcom.c`;
- the SM-X910 failed the same way, in a `pm_test=devices` cycle (see
  [NPU status](npu-status.md)) and in the kernel #8 deep sleep above; this
  tree's notes record no PCS_READY timeout on it since it got the assertion
  (kernel #10: three RAM-root deep cycles, then normal-root cycles);
- but this is one failure on the SM-X710, and nothing has shown that the
  reset is what was missing. The diagnostic below is there for the case
  that it is not.

It was not. On 2026-11-23 the same failure came back on Type-C kit revision
9, which carries the reset, and every calibration entry the diagnostic has
logged on the SM-X710, 11 that passed and 15 that failed, starts from
`SW_RESET 0x1 START 0x0`: a PCS fresh from its reset (the host controller's
reset, or the collapse of the PHY's GDSC, puts it there). The state the
reset was written for, a PCS that kept its registers, has not been seen
once, so on every path observed the extra reset writes a bit that is set
already. What did show is in "Reading the diagnostic" and "What ran on the
tablet (2026-11-22 and 23, revision 9)", below.

The hardware is the same for this purpose: both mainline trees use
`sm8550_ufsphy_cfg`, and the SM-X710's stock device tree gives the PHY
`vdda-phy` on L1D, `vdda-pll` on L3E and `vdda-qref` on L1E, as Qualcomm's
Kalama reference boards do (the SM-X910's stock tree was not checked for
this). Both mainline board trees give `vdda-phy` L1E instead and do not
describe L1D: a difference from stock that the reset does not touch, and one
of the next steps if the reset is not enough. The earlier note that a widened
gate was in X710 build 5, which hung before userspace, does not hold against
it: the [porting log](porting-log.md) traces that hang to the pogo driver
dereferencing a keyboard booster the X710 does not have.

On the SM-X910 nothing changes at run time: it ran this write before, under
the same condition with its board check true. Compiled with the recorded
flags of the SM-X910's Type-C kit build, the new `qmp_ufs_phy_calibrate()`
differs from the old one only in what the board check cost: the call of
`of_machine_compatible_match()` and its branch, and the two-pointer array on
the stack for it, which also took the stack protector's canary with it. The
register writes and their order are the same, and no other function of the
object changes; the image moves by those bytes, so its build ID changes.

`scripts/stage-ufs-resume-experiment.sh` replaces the board-gated form in a
kernel tree prepared before the change (an incremental kit build) with the
current one, exactly (its hunk is pinned to the old patch by sha256).
`UFS_PCS_RESET_EXPERIMENTAL=0` refuses a tree with either form. Its checks
that a patch is in a tree, and the removal of the old form, reverse with
`--force`, not `--batch`: given a patch that is not in the tree but would go
in, `patch --reverse --batch` says "Unreversed patch detected!  Ignoring -R."
and goes forward, which a dry run then passes. `scripts/test-ufs-resume-experiment.py`
checks the upgrade, a marker without its code, and fails on any board check
in the calibration function.

### The diagnostic

`kernel/patches/qmp-ufs-pcs-state-diagnostic.patch` is for test builds only:
`build-mainline-kernel.sh` applies it with
`APPLY_QMP_UFS_PCS_STATE_DIAGNOSTIC=1` (on top of the PCS reset, which it
requires), and refuses a default build in a tree that carries it. It reads
and logs, and changes no result:
- at the entry of every calibration of an SM8550 PHY, a debug line with
  SW_RESET, START, POWER_DOWN_CONTROL and READY_STATUS of the PCS and the
  SerDes common block's C_READY_STATUS and CMN_STATUS. The offsets are the
  v6 layout's in the pinned headers, the same as the Kalama driver's for the
  PCS (PHY_BASE 0x400: START 0x0, POWER_DOWN_CONTROL 0x4, SW_RESET 0x8,
  READY_STATUS 0x1a8); mainline's combo and eDP PHY drivers poll the two
  SerDes registers (0x1f8, 0x1d0) for their PLL. The calibration runs after
  `phy_power_on()`, so the PHY's supplies and clocks are on, and the
  calibration itself writes and polls these blocks;
- after a PCS_READY timeout, warning lines with the state the timeout left,
  whether PCS_READY came within the 1 s that the Kalama driver waits (a poll
  of the rest of that second, after the timeout only; the calibration fails
  all the same), and the state at the end of that second if it did not.

A calibration runs at probe (and once more if the gear the device takes
differs from the PHY's), at the boot's suspend cycle for the panel, and at
every resume from a system sleep (`spm_lvl` 5 on SM8550: the link is off);
runtime suspend keeps the link in hibernate (`rpm_lvl` 3) and does not
recalibrate. So a boot logs two or three entry lines, and each sleep one
more. The entry line is at KERN_DEBUG, which no console prints, not even
with the `loglevel=7` of the kits' command lines; the others are at
KERN_WARNING, logged only after a timeout, which a console prints only with
a console loglevel above 4. Ubuntu sets 4 early in boot. squirtle runs 5:
it still has the hang-log kit's first `/etc/sysctl.d/60-gts9u-hang-log.conf`
(`kernel.printk = 5 4 1 7`, read 2026-11-22), as it did in the failing
sleep. That matters because each console line costs 0.1 to 0.25 s with
interrupts off on the SM-X710 (see the
[hang-log kit](../tools/inplace/hang-log/README.md)): a resume there whose
15 calibrations all fail prints about 45 more lines, 5 to 11 s, and the time
the diagnostic reports for a late PCS_READY includes the line before its
poll. `dmesg` and `journalctl -k` show all of them.

The source compiled with the recorded flags of both kit builds (the
SM-X710's and the SM-X910's), without a warning; the test script runs its
functions with mocked MMIO.

A second diagnostic, `kernel/patches/qmp-ufs-pwrdn-readback-diagnostic.patch`
(Type-C kit revision 10; applied only with both
`APPLY_QMP_UFS_PCS_STATE_DIAGNOSTIC=1` and `APPLY_SM8550_SLEEP_FIXES=1`,
refused otherwise in a tree that carries it), reads POWER_DOWN_CONTROL back
in the power-on before each calibration: right after the power-up write and
100 us later (`udelay`), logged at KERN_DEBUG right before the entry line,
`power-up write: PWRDN 0x1 right after it, 0x1 100 us later`. Only the
power-on that `qmp_ufs_set_mode()` comes before is read, which is
`ufs_qcom_power_up_sequence()`'s, one per calibration; the power-on at every
ungating of the UFS clocks is not. It writes no register.

### Reading the diagnostic

After a resume, `calibration entry:` shows the state the calibration started
from. These readings are inferences from what the registers mean, not
proven. `SW_RESET 0 START 0x1` would be what the last calibration left, so
the PCS kept its registers through the sleep: the case the reset is for. A
reset PCS shows `SW_RESET 0x1 START 0x0`, and every entry on record does.
`PWRDN` is POWER_DOWN_CONTROL's SW_PWRDN, active low: `0x1` means the PHY's
analog part is powered up, which `qmp_ufs_power_on()` sets right before the
calibration; `0x0` is analog power-down, its reset value. It read `0x1` in
every calibration that passed and `0x0` in all 15 of the 2026-11-23 failure,
while the calibration's own writes to SW_RESET and START held: the PHY did
not keep the power-up, so its PLL could not lock (`C_READY 0x0`). The
revision 10 line tells where it went: `0x1` and `0x1` with `PWRDN 0x0` at
the entry, undone after the power-on; `0x1` then `0x0`, undone within
100 us; `0x0` right after the write, the write did not take, which a PHY
without an input it needs (its core supply, L1D, the first suspect) would
show. If a calibration still ends in `phy initialization timed-out`, the
lines after it point to the next step:
- `PCS_READY ... us after the timeout, within 1 s of SERDES_START`: the PHY
  was only slow; give the poll the Kalama driver's 1 s;
- `no PCS_READY within 1 s` with `C_READY 0x1`: the SerDes PLL is ready and
  the PCS is stuck despite its reset; next, the external `ufsphy` reset,
  which the Kalama driver asserts around its calibration and mainline does
  not take for a PHY with a PCS reset;
- `no PCS_READY within 1 s` with `C_READY 0`: the PLL never became ready;
  next, the PHY's analog supply (L1D, see above) or its reference clock.

### Testing it

`tools/inplace/typec-roles/` revision 8 carries the reset and the
diagnostic, and `sleep-test.sh`: deep sleeps with every disk filesystem
frozen, ext4 kept from recording an error (`errors=continue` with the disk
read-only), run from a RAM root, so that a resume that loses the storage
costs a restart and nothing else (its README has the details, and the next
section why a freeze). The order:
1. `sleep-test.sh dry`: the protection and the restart with no sleep, and
   a clean boot after it (`verify.sh` knows the boot after a sleep test);
2. on revision 7, before the install, a baseline: does it fail every time,
   or one time in N? With the floor released (`release-floor` in a boot
   after a forced restart);
3. revision 8 installed: `verify.sh` (the diagnostic's entry lines, no UFS
   failure), then the sleep test docked and undocked, each with the floor
   released;
4. only after those pass: sleeps with a writable root
   (`scripts/diagnostics/suspend/gts9u-suspend-cycles.sh`), then the suspend
   policy.

Steps 1 and 2 ran on 2026-11-22: revision 7 resumed 13 of 13 deep sleeps,
none of them deep enough to count in `qcom_stats` ("What ran on the
tablet", below). Revision 8 was never installed; revision 9, which carries
its reset and diagnostic, ran from 2026-11-22 evening, and its fourth run
lost the UFS the 2026-11-21 way ("What ran on the tablet (2026-11-22 and 23,
revision 9)", below). Revision 10 adds the second diagnostic above.

The sleep test also has experiment options, each off unless given (the
kit's README, "The experiment options", has the details and the risks):

```sh
sudo sh ~/typec-roles/sleep-test.sh ro [CYCLES] [SECONDS] [release-floor] [hold=S] [aborts=N]
     [pci=remove] [pm_test=LEVEL] [trace|trace=live] [mode=s2idle] restart-ok
sudo sh ~/typec-roles/sleep-test.sh dry [release-floor] [hold=S] [pci=remove] [mode=s2idle] restart-ok
```

- `pci=remove` removes every PCI root port, and the Wi-Fi below it, right
  before the freeze, so that the PCIe host powers down in the suspend; the
  Wi-Fi is gone until the restart. It is refused if that would not empty
  the bus, and a cycle after which the PCIe host did not come back ends the
  run (`PCIE-DOWN`): the next suspend would read its registers unclocked.
- `pm_test=LEVEL` (`freezer`, `devices`, `platform`, `processors`, `core`)
  stops each suspend at that level, waits 5 s and resumes, with no SoC
  sleep call (PSCI `SYSTEM_SUSPEND`): each cycle is PM-TEST, never a depth.
  It is set only around each `rtcwake`. At `devices` and `platform` the
  CPU cluster's power domain cannot even power off through idle (its device,
  the RPMh RSC, is prepared), so `qcom_stats` can only stay +0 there.
- `mode=s2idle` sleeps each cycle with `rtcwake -m freeze`, suspend-to-idle,
  instead of `rtcwake -m mem` (PSCI `SYSTEM_SUSPEND`): the CPUs stay online,
  the last one takes the cluster down and enters PSCI `CPU_SUSPEND`.
  `/sys/power/mem_sleep` plays no part. Every verdict carries `(s2idle)`; a
  sleep that a wakeup other than the RTC's cut short is WOKEN EARLY and
  tried again within `aborts=N`; `pm_test=` is then `freezer`, `devices` or
  `platform` only.
- `trace` raises the console loglevel to 8 for each sleep, so that the PM
  core's lines reach the ramoops console: live up to the console suspend,
  the rest at the resume. `trace=live` also keeps the consoles printing
  through the suspend (`console_suspend` N), at a cost in time with
  interrupts off and with the debug UART kept powered.

Every `RESULT` line names the options in force, and pmsg gets progress
lines (`progress: ...`) up to the sleep a run enters, so that a run that
never comes back still leaves its start, its options and that sleep in the
next boot's pstore archive.

A failure line of the PCIe host or its PHY in the same words as the UFS's
(`qcom-pcie 1c00000.pcie: Host init failed: -16`) fails a cycle too, but
counts as a storage failure only from revision 10's `sleep-test.sh` on,
which then says that the storage came back and names the PCIe host's line;
before it, its NEXT line called every such run's storage lost.

Nobody touches the screen during a run: the touchscreen's double tap wakes
the tablet, and in a suspend under way it makes the kernel give the suspend
up (the script calls such an attempt ABORTED, tries the cycle again, and
after `aborts=N` of them ends with NO-SLEEP, not FAIL). A suspend whose
task freezer gives up is NO-SLEEP (freezer) and is not tried again (below);
the next run then goes from `multi-user.target`, without the desktop.

After boot and after every sleep, `findmnt -no OPTIONS /` must show `rw`
without `emergency_ro`,
`journalctl -k -b | grep -iE 'phy initialization timed-out|ufshcd.*(error|fail)'`
must print nothing, and `cat /sys/fs/ext4/sda34/errors_count` must say 0.

### Protecting the storage in a sleep test

The first form of `sleep-test.sh` made every disk filesystem read-only with
`sysrq u` before the first sleep. On 2026-11-22 (03:28, the first baseline
run on squirtle) that recorded an ext4 error on the live root, and every
boot after it failed to mount the root. The mechanism, in the pinned kernel
source (7.2-rc3):
- `sysrq u` remounts every filesystem read-only
  (`do_emergency_remount`). For ext4, `__ext4_remount()` then calls
  `ext4_mark_recovery_complete()` (`fs/ext4/super.c`). That function, on a
  read-only filesystem with `needs_recovery` or `orphan_present` set,
  requires the orphan file to be empty and otherwise records
  `ext4_error(sb, "Orphan file not empty on read-only fs.")` and returns
  `-EFSCORRUPTED`. A running desktop always holds deleted files open, so
  the orphan file is never empty: the error is recorded, and with the disk
  still writable at the block layer, `ext4_handle_error()` saves it to the
  superblock (the error flag, the count, the first error's function and
  line: `ext4_mark_recovery_complete`, line 6370).
- At the next mount, `ext4_orphan_cleanup()` (`fs/ext4/orphan.c`) skips the
  cleanup on a filesystem with `EXT4_ERROR_FS` ("Skipping orphan recovery
  on fs with errors"), so the orphan file stays full, and
  `ext4_mark_recovery_complete()`, called from `__ext4_fill_super()` because
  `orphan_present` is set, fails the same way: the mount fails with
  `EFSCORRUPTED`. This port's initramfs has no fsck (see below), its
  `panic` reboots after 10 s (`panic=10` on the command line), and the
  tablet looped. The superblock read from recovery: "clean with errors",
  error count 5, first error 03:28:48 in `ext4_mark_recovery_complete`
  line 6370.
- Repaired on 2026-11-22, about 03:45 PST, from TWRP (3.7.1_12) with
  Ubuntu's own `e2fsck` 1.47.0, copied to TWRP's RAM with its libraries and
  run through its own dynamic loader: TWRP's `e2fsck` 1.45.4 is too old
  for the `orphan_file` feature. `e2fsck -fn` ended with 4 (errors left),
  `e2fsck -fy` with 1 (repaired), a second `e2fsck -fn` with 0. It fixed
  only what the orphans left behind: 24 deleted inodes with no deletion
  time, the block and inode bitmaps and free counts they still held, and
  the orphan file's block (passes 2 to 4 found nothing; it also applied
  its usual extent-tree optimizations). The tablet booted cleanly at 03:47,
  the root read-write, `errors_count` 0. The transcripts are in
  `~/ubuntu-gts9u/backups/squirtle-bootloop-20261122-0334/`
  (`fsck-n.log`, `fsck-y.log`).
- On 2026-11-21 the same remount did not leave this state only because
  the UFS was dead by then and the error could not be written.

The general fact behind it, for this port as it is: with `orphan_file` and
no fsck in the initramfs, **any ext4 error recorded on the running root
while deleted files are held open makes the next mount after a reset
fail**. The error sets `EXT4_ERROR_FS` in the superblock (or in the
journal's superblock, which the next mount turns into it:
`ext4_clear_journal_err()`, "Filesystem error recorded from previous
mount"); after a reset `orphan_present` is still set; the mount skips the
orphan cleanup because of the error and then fails in
`ext4_mark_recovery_complete()` on the orphan file that is not empty. A
clean shutdown avoids it only when the programs' files are closed and the
orphan file is empty by the time the root is unmounted. An fsck in the
initramfs would repair such a root at boot; whether to add one
(`fsck.ext4 -p` may not repair these states without a manual run, which is
not checked) is an open item, not decided.

The script now freezes the filesystems instead (`fsfreeze -f`, FIFREEZE):
- `freeze_super()` (`fs/super.c`) blocks new writers, syncs the filesystem,
  then calls `ext4_freeze()`, which flushes the journal
  (`jbd2_journal_flush`), clears `needs_recovery`, clears `orphan_present`
  only if the orphan file is empty, and writes the superblock. Nothing in
  that path records an error.
- After a reset with the root frozen, the next mount has no journal to
  replay, runs `ext4_orphan_cleanup()` (no error recorded; on a read-only
  mount it lifts `SB_RDONLY` for the cleanup, as after any power loss),
  which empties the orphan file, and then `ext4_mark_recovery_complete()`
  clears `orphan_present`. That is the path of an ordinary power loss, less
  the journal replay. A mount that has to replay a journal ("INFO: recovery
  required on readonly filesystem") or that finds one ext4 did not ask to
  replay ("JBD2: Clearing recovery information on journal") in the boot
  right after a run means the freeze did not hold to the restart;
  `verify.sh` fails then (it knows that boot by the run's `SLEEP-TEST
  RESTART` line in the pmsg the boot archived).
- Writers to a frozen filesystem wait in `__sb_start_write()`
  (`include/linux/fs/super.h`) through `percpu_down_read_freezable(...,
  true)`, which sleeps `TASK_UNINTERRUPTIBLE | TASK_FREEZABLE`
  (`kernel/locking/percpu-rwsem.c`), and the suspend's task freezer takes a
  task in `TASK_FREEZABLE` (`__set_task_frozen()`, `kernel/freezer.c`): the
  writers themselves do not stop a suspend. A task queued behind one of
  them can: a write fault on a shared mapping of a file on the root
  (sqlite's `-shm`, the journal's files) waits in `ext4_page_mkwrite()` ->
  `sb_start_pagefault()` while it holds the VMA's lock, and another thread
  of that process that forks, unmaps or changes the mapping waits for that
  lock killable or uninterruptible, not freezable (`mm/mmap.c`,
  `mm/mmap_lock.c`). `__set_task_frozen()` refuses it, and after 20 s the
  freezer gives up ("Freezing user space processes failed after ...",
  `kernel/power/process.c`), naming the task. The script calls that
  NO-SLEEP (freezer); whether it happens on this desktop is not known. The
  suspend's own filesystem sync finds nothing to write. The hung-task
  detector reports such writers after 60 s awake (`kernel/hung_task.c`);
  `hung_task_panic` is 0 by the kernel's default
  (`CONFIG_BOOTPARAM_HUNG_TASK_PANIC=0`, as the hang-log kit's build keeps
  it; its `verify.sh` checks the sysctl), so that is a warning, and the
  script refuses to start otherwise.
- The kernel's own freeze on suspend (`filesystems_freeze()`) freezes only
  filesystems marked for it unless `/sys/power/freeze_filesystems` is 1;
  at 1 it takes an extra hold on a filesystem that userspace froze and
  drops only that hold at resume (`freeze_super()`/`thaw_super()` holders,
  `fs/super.c`). At 0, squirtle's setting, the resume runs
  `filesystems_thaw()` all the same: it finds no freeze of its own on the
  frozen filesystem, leaves the userspace freeze in place and warns once
  per boot, `WARN_ON_ONCE(sb->s_writers.freeze_kcount == 0)` at
  `fs/super.c:1919` (`may_unfreeze()`, inlined in `thaw_super_locked()`),
  which taints the kernel `W`. The same happens after a suspend that was
  given up, the freezer's too. It is expected, and the script refuses to
  start with `panic_on_warn` set. After every sleep the script freezes each
  filesystem a second time, which must fail with `EBUSY` (`freeze_super()`
  -> `may_freeze()`): the check that nothing thawed it.
- `exfat` has no freeze (`exfat_sops` has no `freeze_fs`): the script
  refuses while an exfat filesystem is mounted writable.
- `blockdev --setro` on the whole disk makes `bdev_read_only()` true for
  its partitions (`get_disk_ro()`, `include/linux/blkdev.h`). From then on
  `ext4_handle_error()` skips `save_error_info()` and its superblock write,
  so the error count cannot rise. It does not skip the journal abort: under
  `errors=remount-ro` (this root's superblock default) or for a forced
  read-only, `jbd2_journal_abort()` writes the error number into the
  journal's superblock (`jbd2_journal_update_sb_errno()`,
  `fs/jbd2/journal.c`), and the block layer lets that write through after
  logging the first write to a read-only disk (`bio_check_ro()`,
  `block/blk-core.c`). The next mount then reads it, marks the filesystem
  in error ("Filesystem error recorded from previous mount") and fails as
  above.
- Hence `errors=continue`: as the last step before the run moves to RAM,
  every ext4 filesystem it freezes is remounted with it (`nsenter -t 1 -m
  -- mount -o remount,errors=continue MOUNTPOINT`, the system's own mount
  in PID 1's mount namespace: a remount of a mount in another namespace is
  refused, `do_remount()`), and `/proc/1/mounts` has to show it. With
  `errors=continue` and the disk read-only, `ext4_handle_error()` neither
  aborts the journal nor saves anything nor goes `emergency_ro`. A forced
  read-only still does (`ext4_abort()`: on a frozen filesystem only after
  jbd2 aborted the journal on its own, an abort that writes the error by
  itself), and the script looks for that (below). `errors=` is a
  mount option, changed by a remount of the read-write filesystem
  (`ext4_parse_param()`, the last `errors=` given wins); the superblock's
  `s_errors`, which the next boot mounts with, is not touched (the kernel
  shows `errors=` only when it differs from it: `_ext4_show_options()`). A
  stop before any sleep puts each one back after the thaw (a frozen
  filesystem refuses a remount, `reconfigure_super()`: `EBUSY`). Written
  from the source; what has since run on the tablet, and what of it is
  still source reading, is in "What ran on the tablet" below.
- Swap to a swap file goes to its blocks past the filesystem
  (`mm/page_io.c`), so a freeze does not stop it: the script turns swap on
  the disk off first.

What the script does with ext4's state: it refuses to start on a root with
a recorded error; before the first freeze, once the disk is read-only, at
the end and right before the restart it compares each frozen filesystem's
`errors_count` with the one it read before the run, looks at its options
(`emergency_ro`, `shutdown`), and at the kernel log since the run began
(`EXT4-fs error (device NAME)`, `Aborting journal on device NAME-`,
`EXT4-fs (NAME): Remounting filesystem read-only`). Before any sleep any
of it stops the run, thawed and not restarted, with `e2fsck -f` from
recovery as the next step. After a sleep it says `SLEEP-TEST ERROR-STATE`
(a journal aborted, an error saved) with NEXT lines that the root will
probably not mount at the next boot ("probably": `JBD2: I/O error when
updating journal superblock` means that write failed), or `SLEEP-TEST
NOTE` (an error logged on the read-only disk under `errors=continue`,
nothing written), and restarts in either case.

#### What ran on the tablet (2026-11-22)

On squirtle, kit revision 7's kernel (build ID `84c2748b5ee4`), in this
order. The logs are in `~/ubuntu-gts9u/backups/`.

**The freeze proof, 09:18** (`squirtle-freeze-proof-20261122/`, `run-1.log`;
the script lives there, not in this tree). Throwaway ext4 images in
`/dev/shm` with the root's features (`orphan_file`, `errors=remount-ro`),
deleted files held open, each copied as a restart would find it and then
mounted as the boot mounts the root:
- A, a power loss: mounts (journal recovery, then the orphan cleanup).
- B, `fsfreeze -f` and `blockdev --setro`: mounts cleanly; the copy has
  `orphan_present` and no `needs_recovery`, so the mount cleans the orphans
  and replays nothing. A `sync` with the image frozen returned at once.
- C, the error flag set by hand (`debugfs` `ssv state 3`, "clean with
  errors"; a plain `mount -o remount,ro` was refused first, as expected with
  deleted files open): the mount fails with the lines of the 03:28 boot
  loop (`ext4_mark_recovery_complete:6370: ... Orphan file not empty on
  read-only fs`, "mount failed"). `e2fsck -p`, the boot's automatic check,
  repaired it, and the copy then mounted: an fsck in the initramfs would
  have ended that loop by itself.
- D, an ext4 error raised while frozen and read-only, with
  `errors=remount-ro`: the copy mounted, where the proof expected it not
  to. The error came from `/sys/fs/ext4/<dev>/trigger_fs_error`, whose
  handler (`trigger_test_error()`, `fs/ext4/sysfs.c`) calls `ext4_error()`.
  That macro passes error number 0 (`fs/ext4/ext4.h`), so
  `ext4_handle_error()` aborted the journal with `jbd2_journal_abort(journal,
  -0)` and `jbd2_journal_update_sb_errno()` wrote `s_errno` 0 into the
  journal's superblock. That write was attempted behind the read-only flag
  ("Trying to write to read-only block-device", logged once by
  `bio_check_ro()`, which lets it through), and the filesystem went
  `emergency_ro`. At the next mount `ext4_clear_journal_err()` read
  `j_errno` 0 and marked nothing. Only an error that carries an errno (an
  `ext4_error_err()`, or the inode and block read errors that pass `-EIO`,
  which is what a lost UFS raises) leaves a non-zero `s_errno`, and the next
  mount then reports "Filesystem error recorded from previous mount" and
  marks the filesystem in error. The sysfs trigger cannot raise such an
  error, so that part, and with it the case `errors=continue` is for, is
  still read from the source only.
- E, the same error with `errors=continue` set before the freeze: no
  journal abort, no write attempt, no `emergency_ro`; the copy mounted.

**`dry`, 09:35** (boot `439e9851`, the floor held;
`squirtle-sleep-dry-20261122.log`): `errors=continue` took on the root
(`/proc/1/mounts` showed it), the root froze through `/proc/1/root`, `sda`
went read-only, the second freeze found it still frozen (`EBUSY`), the read
after 10 s matched, `sysrq b`. The next boot (`780e25d7`) mounted the root
with the orphan cleanup ("6 orphan inodes deleted") and no journal replay,
`errors_count` 0; `verify.sh` passed every check but the build ID (revision
7 running, not the kit's).

**Revision 7 baseline, 09:44** (boot `780e25d7`, up 448 s, on the powered
dock as power sink and USB host, the floor released;
`squirtle-sleep-r7-baseline-20261122-0945.log` and its console):
`ro 3 30 release-floor hold=60 restart-ok`. All three cycles slept 30.5 to
31.3 s, were woken by the RTC (IRQ 21, `pm8xxx_rtc_alarm`) and resumed with
the UFS reading back as before; each was SHALLOW, `aosd`, `cxsd` and `ddr`
+0. The task freezer got through behind the frozen root. The console had:
- at the first resume, the one warning expected, `WARNING: fs/super.c:1919
  at thaw_super_locked+0x200/0x23c`, from `filesystems_thaw()` called by
  `pm_suspend()`. `suspend_finish()` thaws filesystems after every suspend
  (`kernel/power/suspend.c`), as the kernel's own holder
  (`FREEZE_EXCL | FREEZE_HOLDER_KERNEL`, owner `filesystems_freeze`);
  `may_unfreeze()` finds no kernel freeze on a filesystem only userspace
  froze (`freeze_kcount` 0), warns once per boot (`WARN_ON_ONCE`) and
  refuses, so the run's freeze stays. The kernel is tainted `W`;
- hung-task reports for tasks waiting on the frozen root (`systemd-journal`,
  `systemd-timesyn`, `bluetoothd`, rsyslogd's `rs:main Q:Reg`,
  `NetworkManager`, `tailscaled`), as expected;
- `ttyMSM0: Unable to drain transmitter` at the first suspend, and no UFS,
  PHY or I/O line.

The boot after it (`26a78998`) came up with the root clean, `errors_count`
0, and held the floor once (the loop breaker).

**Revision 7 baseline, 10:17** (boot `26a78998`, up 29 minutes, docked as
before, no unplug of the dock's power in that boot, the floor released by
the run at 1759.5 s; `squirtle-sleep-r7-baseline-20261122-1015.log`):
`ro 10 30 release-floor hold=60 restart-ok`. Ten of ten resumed with the
UFS readable, every one SHALLOW (`aosd`, `cxsd`, `ddr` +0), each woken by
IRQ 21 after 30.5 to 31.3 s; the same console lines; the boot after it
(`f2ecad3b`) mounted the root with the orphan cleanup and an empty journal,
`errors_count` 0 (`…-1015-after-restart.log`).

So revision 7, the kernel that failed on 2026-11-21, resumed the UFS 13 times
out of 13 under this protection, and the failure was not reproduced: the
reset of revision 8 is still not shown to be the fix. No cycle reached AOSS
sleep, CX collapse or DDR low power. What has not run: revision 8, a cycle
that reached those states, an ext4 error with an errno under
`errors=continue`, and the 2026-11-21 conditions (a writable root, the dock's
power unplugged and plugged back 13 minutes before, the panel woken 2
minutes before, 53 minutes up). The analysis of why the sleeps stay
shallow and what to run next is in
`~/ubuntu-gts9u/backups/sleep-shallow-report-2026-11-22.md`; from then
on the sleep test also prints read-only evidence around each cycle
(`tools/inplace/typec-roles/README.md`, "The sleep test").

**Undocked on the PC's cable, 12:05 and 12:56** (revision 7, USB in device
role, the floor released by each run). With the Wi-Fi's PCIe root port
removed from the bus first, the 12:05 run did not come back from its first
`rtcwake -m mem`: the tablet restarted by itself about 71 s after the
cycle's marker, with no panic record and nobody touching it, and the next
boot mounted the root clean. The 12:56 control, the same with the PCI
devices kept, resumed 3 of 3, all SHALLOW. So a sleep that can go deeper
than the 16 that resumed (nothing of PCIe or USB left in the sleep set) does
not return on this port yet, and that is a different failure from the
2026-11-21 one, with no UFS line in it. The records, the reading of the
source and the `pci=remove`, `pm_test=` and `trace` options that came out of
it are in the [porting log](porting-log.md) under the same date and in
`~/ubuntu-gts9u/backups/deep-sleep-pcie-analysis-2026-11-22.md`.

**With the root port removed again, 13:42 and 14:00** (revision 7, on the
cable, `release-floor pci=remove trace`, one cycle each, the run removing
the root port itself). `pm_test=core`
(`squirtle-sleep-r7-pcirm-core-20261122.log`) passed as PM-TEST: the PCIe
host went down (`qcom-pcie 1c00000.pcie: Timeout waiting for L2 entry!
LTSSM: 0x11`, a warning) and came back (`qcom-pcie 1c00000.pcie: PCIe Gen.2
x1 link up`, `PM: noirq resume of devices complete after 267.277 msecs`),
CPUs 1 to 7 went offline, the PSCI syscore hook ran (`Calling
psci_idle_syscore_suspend+0x0/0xb0`), the kernel busy-waited 5 s and
resumed; all counters +0, as expected at `core`. `pm_test=platform`
(`squirtle-sleep-r7-pcirm-platform-20261122.log`) passed too: `PM: noirq
suspend of devices complete after 21.772 msecs`, the 5 s wait, the link up
again, `PM: noirq resume of devices complete after 283.843 msecs`; `aosd`,
`cxsd`, `ddr` and `apss` +0. That +0 was the only outcome `platform` allows
here, so it says nothing about the deep state: the expectation that idle
CPUs could reach it during the wait was wrong (the cluster's domain stays on
while its device, the RSC, is prepared). The UFS read back as before in
both, and each boot after came up. So the kernel's suspend path, the PCIe
host's power-down and power-up included, comes back when nothing below it
sleeps; what is left of 12:05 is the `SYSTEM_SUSPEND` call, the state behind
it, and the resume from that state.

**Docked, 14:18** (revision 7, on the powered dock in host role, the same
options without `pm_test`; `squirtle-sleep-r7-pcirm-docked-20261122.log`).
The real sleep did not come back here either: the records end at `progress:
cycle 1/1: entering the sleep` and `printk: Suspending console(s)`, the
tablet restarted by itself, and the next boot mounted the root clean. So the
USB role makes no difference: with the PCIe host powered down, a real `deep`
sleep does not return on this port, docked or not.

**Suspend-to-idle, 15:49** (revision 7, on the PC's cable again, a fresh
boot, `ro 1 30 release-floor pci=remove mode=s2idle trace hold=60
restart-ok`; `squirtle-sleep-r7-pcirm-s2idle-20261122.log`). Suspend-to-idle
reaches the cluster's deepest state by another firmware call (`CPU_SUSPEND`
instead of `SYSTEM_SUSPEND`). It did not come back either: the records end
at `progress: cycle 1/1: entering the sleep, rtcwake -m freeze -s 30` and
`printk: Suspending console(s)`, the tablet restarted by itself about 91 s
after the sleep entry (about 92 s at 14:18), and the next boot mounted the
root clean. So the failure is not in one firmware call: with the PCIe host
powered down, the state the SoC reaches, or the way out of it, does not
return on this port. Not known yet: whether the RTC alarm fails to wake the
tablet or the resume hangs, and what restarts it (Linux runs no watchdog
here). The [porting log](porting-log.md) has the reading and the next run.

**Three more, 16:16 to 16:50** (the same run with a 60 s sleep, then with
`hold=120`, then 30 s with `trace=live`). None came back. The restart came
about 122 s after the sleep entry for both 60 s sleeps, whatever the hold,
and about 92 s for the 30 s one: it follows the RTC alarm by about a minute
and is not the run's own. With the consoles kept on, the record shows every
phase of the device suspend completing and ends at `PM: suspend-to-idle`,
with no line from the way back. So the hang is not in a driver's resume:
Linux does not run again after the CPUs enter the idle state, and something
below the kernel restarts the tablet.

**18:22, the same run with the floor held** (`ro 1 30 pci=remove
mode=s2idle trace=live hold=60`, no `release-floor`, on the boot after the
16:50 hang). It came back: SHALLOW, woken by the RTC after 27.7 s, the UFS
read back as before, errors_count 0. The PCIe host went down and came up
again exactly as in the runs that hung (`Timeout waiting for L2 entry`,
then `PCIe Gen.2 x1 link up`), so the host's power-down is not the cause:
the sleep is lost only when the floor is released as well and the SoC goes
deeper. The PMIC's power-on log, read on the same boot, shows each hang
ending with the SoC side dropping PS_HOLD (a warm reset, then a hard one),
the RTC alarm still pending, and no PMIC watchdog or fault. Until a kernel
fixes the deeper state, a sleep with the floor held is the one that
returns; the [porting log](porting-log.md) has the candidates.

#### What ran on the tablet (2026-11-22 and 23, revision 9)

Type-C kit revision 9 (build ID `7912707458aa`: revision 8's reset and
diagnostic, and run-time switches that collapse the PCIe and USB GDSCs for
a run) went on over revision 7 at 20:59. Each run: one 30 s suspend-to-idle
cycle with the root port removed and the floor released, undocked on the
PC's cable, in a boot of its own (`squirtle-sleep-r9-*-20261122.log` and
the pstore directories of those names; the [porting log](porting-log.md)
has the details):
- 21:01, no switch: hung, restarted by itself about a minute after the RTC
  alarm, as on revision 7;
- 21:49 (`trace=live`), 23:34 and 23:46 (the ADSP stopped), `gdsc=pcie0`:
  each came back, woken by the RTC, the UFS calibrated with `PWRDN 0x1` and
  read back as before, but the PCIe host did not (`pcie_0_gdsc status stuck
  at 'off'`, `Host init failed: -16`), and `aosd`, `cxsd`, `ddr` stayed +0;
- 2026-11-23 00:14, `gdsc=pcie0,usb`, the ADSP stopped: came back with the
  same PCIe failure, and the UFS PHY failed as on 2026-11-21: 15
  calibrations, each entry `SW_RESET 0x1 START 0x0 PWRDN 0x0 READY 0x4
  C_READY 0x0 CMN 0xa0`, each `phy initialization timed-out` and `no
  PCS_READY within 1 s of SERDES_START`, then I/O errors. The protection
  held: ext4 logged 5 errors on the read-only disk and saved none, the run
  restarted the tablet, and the next boot mounted the root with
  `errors_count` 0.

What that and the analysis of 2026-11-23
(`~/ubuntu-gts9u/backups/ufs-phy-resume-rootcause-2026-11-23.md`) show:
- the mechanism of the UFS failure: the PHY did not keep the power-up bit
  its power-on wrote, so its PLL could not lock, and no retry changed that.
  Why it did not is not known. The retries never cycle the PHY's core
  supply, which stock and Qualcomm's reference boards give as L1D while
  this port's device tree gives L1E and does not describe L1D, nor the
  PHY's GDSC, nor the external `ufsphy` reset. Whether the USB GDSCs'
  collapse (run 3's alone) has a part is open: one failure against three
  passes, and 2026-11-21 failed with them kept on;
- revision 8's reset is not the fix (above);
- the PCIe failure is a separate one, from the 7.2-rc phy-mux rework
  (e108373c54fb), which v7.2 final reverted: the PIPE clock mux is no longer
  parked on XO while its GDSC is off, so the GDSC cannot power up again;
- two leftovers keep the SoC out of its deep states, both fixed upstream
  later: the GPU RSC's stop sequence skipped (d9108bfdb746), and ufs-qcom
  enabling every UFS host clock once more at each runtime resume
  (f07317a8d57f; squirtle's counts read 522 to 525 on 2026-11-23). The
  inline crypto engine's driver also keeps the ICE core clock enabled from
  its probe on, which nothing upstream changes.

Type-C kit revision 10 carries the three upstream commits (without
`unpark-pcie0-pipe-mux.patch`) and the second diagnostic; it was built on
2026-11-23 (kernel build ID 90da273a8a49) and has not run yet. The device
tree's UFS PHY supplies (L1D) are left for
a later, separate change.

#### Repairing the root from recovery

What the 2026-11-22 repair did, for a root that does not mount. TWRP's own
`e2fsck` (1.45.4 in TWRP 3.7.1_12) does not know `orphan_file`; Ubuntu's
does. The build host's rootfs carries the same binary that was used
(`rootfs-gts9uwifi/usr/sbin/e2fsck`, identical to the copy in the backup
directory above); it needs `libext2fs.so.2`, `libcom_err.so.2`,
`libblkid.so.1`, `libuuid.so.1`, `libe2p.so.2`, `libc.so.6` and the loader
`ld-linux-aarch64.so.1`, all in `usr/lib/aarch64-linux-gnu/`. Copy the
eight files to one directory in TWRP's RAM (`adb push` to `/tmp/e2fsck/`),
check as in "Offline repair" above that userdata is not mounted, and run
it through its own loader:

```sh
E=/tmp/e2fsck; P=/dev/block/by-name/userdata
$E/ld-linux-aarch64.so.1 --library-path $E $E/e2fsck -fn "$P"   # look: 4 = errors left
$E/ld-linux-aarch64.so.1 --library-path $E $E/e2fsck -fy "$P"   # repair: 1 = repaired
$E/ld-linux-aarch64.so.1 --library-path $E $E/e2fsck -fn "$P"   # must end with 0
```

These command lines show the method; the owner's own were not recorded
here. TWRP's `/tmp` is gone after a reboot.

Why the boot did not repair the root: the initramfs that boots on this port
is built on the build host (`scripts/make-initramfs.sh` runs
`update-initramfs` in the rootfs chroot). initramfs-tools' `fsck` hook puts
`fsck` and `fsck.ext4` in only for the types it finds for `/` in fstab, and
for `/` it looks the type up from the device (`LABEL=...`, through
`blkid`), which does not exist on the build host; `initramfs.conf`
(`scripts/build-ubuntu-rootfs.sh`) sets no `FSTYPE`. So the initramfs has
no fsck (the two built images, `rootfs/boot/initrd.img-7.2.0-rc3-dirty` and
`rootfs-gts9uwifi/boot/initrd.img-7.2.0-rc3-dirty`, list none), and its
`checkfs` says "fsck not present, so skipping root file system" before
the mount. Not changed here; open, as said above.

X710 suspend has so far passed one real deep and one real s2idle cycle
(2026-11-06, baseline kernel, rpmhpd unsynced and the interconnect floor
held), lost the UFS in one real deep sleep (2026-11-21) and in one
suspend-to-idle with the USB and PCIe GDSCs collapsed (2026-11-23), both
above, and stays off by default; see the [display notes](gts9wifi-display.md)
and `scripts/diagnostics/suspend/`.
