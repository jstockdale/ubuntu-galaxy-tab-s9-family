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
KERN_WARNING, which a console prints only before Ubuntu sets the console
loglevel to 4 early in boot, and only after a timeout. That matters because
each console line costs 0.1 to 0.25 s with interrupts off on the SM-X710
(see the [hang-log kit](../tools/inplace/hang-log/README.md)). `dmesg` and
`journalctl -k` show all of them.

The source compiled with the recorded flags of both kit builds (the
SM-X710's and the SM-X910's), without a warning; the test script runs its
functions with mocked MMIO.

### Reading the diagnostic

After a resume, `calibration entry:` shows the state the calibration started
from. These readings are inferences from what the registers mean, not
proven. `SW_RESET 0 START 0x1` is what the last calibration left, so the PCS
kept its registers through the sleep: the case the reset is for. A reset
PCS would show `SW_RESET 0x1 START 0`. If a calibration still ends in `phy
initialization timed-out`, the lines after it point to the next step:
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
read-only, run from a RAM root, so that a resume that loses the storage
costs a restart and nothing else (its README has the details). The order:
1. on revision 7, before the install, a baseline: does it fail every time,
   or one time in N? With the floor released (`release-floor` in a boot
   after a forced restart);
2. revision 8 installed: `verify.sh` (the diagnostic's entry lines, no UFS
   failure), then the sleep test docked and undocked, each with the floor
   released;
3. only after those pass: sleeps with a writable root
   (`scripts/diagnostics/suspend/gts9u-suspend-cycles.sh`), then the suspend
   policy.

After boot and after every sleep, `findmnt -no OPTIONS /` must show `rw`
without `emergency_ro` and
`journalctl -k -b | grep -iE 'phy initialization timed-out|ufshcd.*(error|fail)'`
must print nothing.

X710 suspend has so far passed one real deep and one real s2idle cycle
(2026-11-06, baseline kernel, rpmhpd unsynced and the interconnect floor
held), failed one real deep sleep (2026-11-21, above) and stays off by
default; see the [display notes](gts9wifi-display.md) and
`scripts/diagnostics/suspend/`.
