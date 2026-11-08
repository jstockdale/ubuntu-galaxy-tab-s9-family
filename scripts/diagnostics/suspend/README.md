# Suspend diagnostics for the SM-X710 (gts9wifi)

Two root tools for deciding whether the X710 can be given a suspend mode.
Suspend is off on this board (`ubuntu-gts9u-suspend-mode` writes
`AllowSuspend=no`). On the test unit, `sleep.target` and `suspend.target` were
also masked by hand; undo that (`sudo systemctl unmask sleep.target
suspend.target`) before any mode can suspend. Both tools work in either state
and leave it as they found it.

| File | What it is for |
|---|---|
| `gts9u-suspend-cycles.sh` | Unattended reliability runs: N RTC-woken cycles of s2idle, then N of deep, a checklist after every resume, stop at the first failure. Also one-off key wakes and an awake RTC alarm test. |
| `x710-suspend-ladder.sh` | The reviewed step-by-step ladder: `pm_test` rungs and single real sleeps woken by hand, with `nowifi`, `nobt` and `trace` to isolate a failure. Use it to bisect a cycle that failed. |

`tools/inplace/suspendkit/` packs both (and the ADSP-start sleep lock below)
for a tablet that already runs device package 2.55.

## Wake IRQs on the X710

The numbers move with probe order, so both tools look them up by name in
`/proc/interrupts` and print `pm_wakeup_irq=N (name)`. On the Stage 2 kernel
(fix2-s2, 2026-11-07), with the PMIC RTC built in:

| IRQ | Action | Source |
|---|---|---|
| 21 | `pm8xxx_rtc_alarm` | PMIC RTC alarm (`rtcwake`) |
| 22 | `pmic_pwrkey` | Power |
| 23 | `pmic_resin` | Volume Down (wake-armed by the X710 DTS) |

Before the RTC was registered, Power was 21 and Volume Down 22; older notes
(the suspend unit's README of 2026-11-06, the Ultra's development notes) use
those numbers. The X710 builds `rtc-pm8xxx` in (`config-gts9wifi.fragment`),
so it probes before the PMIC keys; on the Ultra it is only `=m` in the shared
config, so the keys register first and Power stays on 21 there.

## How a cycle sleeps

The way `systemd-suspend.service` would, minus logind:

0. If `ubuntu-gts9u-sensors-resume` is still running (the sensor recovery
   that the sensors hook starts after every resume, or the boot's), the run
   waits for it to finish, at most `--sensors-wait` seconds (default 120, `0`
   to skip). Stopping the sensor protection domain in the middle of that
   recovery, 40 times a run, is ADSP churn of the tool's own making; an ADSP
   crash from it would end a run as a suspend failure. A recovery that ends
   badly is logged as a `note:`, never failed on.
1. The units that conflict with `suspend.target` are stopped, as the
   suspend transaction would stop them (today only
   `hexagonrpcd-adsp-sensorspd.service`; the sensors hook starts it again
   after resume).
2. The RTC alarm is armed with `rtcwake -d rtc0 -m no -s SECS`.
3. `/sys/power/mem_sleep` is set to the cycle's mode, and a drop-in
   `/run/systemd/sleep.conf.d/zz-gts9u-suspendkit.conf` (`AllowSuspend=yes`,
   `SuspendState=mem`) is written for this one cycle. It sorts after the
   policy's `50-ubuntu-gts9u-suspend-mode.conf`, so it wins; it is removed as
   soon as `systemd-sleep` returns, and `/run` does not survive a restart.
4. `/usr/lib/systemd/systemd-sleep suspend` runs, so every
   `/usr/lib/systemd/system-sleep` hook runs around the transition: USB gadget
   rebind, sensors, flashlight, and the sleep statistics line.

Not covered: logind's `PrepareForSleep`. NetworkManager and GNOME are not told,
so the screen is not locked first and Wi-Fi comes back through the driver
alone. The real path (`systemctl suspend` through logind) is the last rung
before a mode is chosen; see "Choosing a mode" below.

## The checklist after each resume

One line per cycle in `run.log` and `cycles.tsv`; any of these fails it:

- `systemd-sleep` return code is not 0;
- the kernel's `PM: suspend entry (...)` is not the cycle's mode, or there
  is no `PM: suspend exit`;
- `suspend_stats` success did not grow by exactly one, or fail grew;
- `pm_wakeup_irq` is not `pm8xxx_rtc_alarm` (a key, or a spurious wake), or
  the alarm had not fired yet;
- Wi-Fi is not connected again within `--wifi-wait` (only when it was
  connected at the start);
- the panel's DDIC answered `00 00 00`;
- the root filesystem is not `rw`;
- a failure signature in the kernel log since the cycle's marker (freezer or
  device suspend/resume failures, UFS, PCIe/ath11k/MHI, USB, GMU/GPU faults,
  RPMh/GDSC, APM timeouts, remoteproc crashes, lockups, splats).
  Correctable AER and the known late GMU bandwidth-vote answer are only
  counted (`noted=`). The detail lines printed under every AER report
  (`device [17cb:1103] error status/mask=...`, `[12] Timeout`) are ignored:
  they carry the Wi-Fi driver's name but no severity, and the report's own
  `severity=` line decides (Uncorrectable and Fatal fail the cycle).

Recorded, not judged: time asleep (the sum of the `Timekeeping suspended for`
lines, printed because the run sets `pm_debug_messages`; s2idle prints one per
idle round), device resume time, the `qcom_stats` deltas and the s2idle usage
of the CPU idle states.

### Sleep depth: aosd, cxsd, ddr

`/sys/kernel/debug/qcom_stats/{aosd,cxsd,ddr}` count AOSS sleep, CX collapse
and DDR low power. On Stage 2, `gem_noc` and `mc_virt` keep their boot-time
`INT_MAX` votes (the bandwidth monitor is held off with
`initcall_blacklist=bwmon_driver_init`), so DDR and CX cannot drop and all
three are expected to stay at +0: a run there measures resume reliability, not
depth. `run --release-icc` first writes 1 to `state_synced` of
`24100000.interconnect` (gem_noc) and `interconnect-1` (mc_virt), checks the
`INT_MAX` count (target 0), watches the kernel log for two minutes, and only
then starts the cycles. The summary then gives the share of the time asleep
spent in CX collapse.

The release cannot be undone short of a reboot. A release on a running
Stage 2 held a 10-minute watch, but an interconnect synced before the boot-time
`pm_test` cycle hung that cycle (Stage 3), and no real suspend has run with it
released yet. Expect a possible wedge: hold Power and Volume Down until the
tablet restarts; it comes back on plain Stage 2.

## Likely first findings, and what they point at

| Result | Points at | Next step |
|---|---|---|
| s2idle woken by `q6v5 ready`, `smp2p-adsp`, `ipcc_0` or `glink-smem` | ADSP traffic as wake noise: `q6v5 ready` fires about 5 times a second on Stage 2, where once per DSP boot is expected | `run --s2idle 10 --deep 0 --keep-going` to count it; deep is not affected by non-PDC IRQs |
| woken by a `dwc3`/USB IRQ | the USB gadget link | repeat with the cable out, over Wi-Fi SSH |
| deep returns within milliseconds, `wake=none` | the IPCC bounce the X710 IPCC patch addresses, or another pending IRQ | compare `grep -E "ipcc_0\|smp2p" /proc/interrupts` around a cycle |
| `Wi-Fi is '...'` after resume | ath11k resume | ladder `s2idle nowifi nobt` to confirm; then try one cycle with `echo enabled > /sys/bus/pci/devices/0000:01:00.0/power/wakeup` (keeps the WCN6855 link and power up through suspend) |
| root `ro`, UFS lines | UFS resume | stop; TWRP and fsck as in `docs/resume-recovery.md` |

## A cycle that does not come back

First press Power once. The RTC alarm has only been proven while awake
(`rtc-check`); if it cannot wake the SoC from a mode, the tablet sleeps on
and a key press brings it back, and the cycle then fails with
`woken by IRQ 22 (pmic_pwrkey), not pm8xxx_rtc_alarm`: the RTC wake is the
finding, not a hang. Only if Power does nothing within 30 s, hold Power and
Volume Down until it restarts.

The log is synced line by line and the cycle's name is written to `IN-CYCLE`
before it sleeps. After a forced restart, `status` reports
`cycle ... NEVER RETURNED`. Then bisect with the ladder, for example
`x710-suspend-ladder.sh s2idle nowifi nobt`, and `trace` for a hang (no
ramoops record survives a reset on this unit yet, so the trace is of limited
use until that is fixed).

## The ADSP-start sleep lock

`ubuntu-gts9u-adsp-start-inhibit.service` (device package) holds
`systemd-inhibit --what=sleep --mode=block` from before
`ubuntu-gts9u-adsp-boot` starts the DSP until it is up (remoteproc running,
`/dev/fastrpc-adsp` and a sound card), plus 10 s. A suspend overlapping the DSP
start timed out APM on this board. It only matters once a suspend mode is on,
and it holds off what goes through logind: the cover, GNOME idle, the Suspend
menu, a sleep key. It does not hold off the power key, because
`ubuntu-gts9u-powerkey` asks for the suspend as root, and logind lets root pass
a block lock. The runner refuses to start while the lock is held.

The helper is the unit's main process (`Type=notify`) and runs
`systemd-inhibit` as a background holder; it sends `READY=1` once the holder
has started, which systemd-inhibit only does after logind granted the lock.
It cannot be `systemd-inhibit`'s command instead: systemd 254 and later remove
`NOTIFY_SOCKET` from that child's environment, so its `READY=1` never arrives
and the unit fails (`protocol`, or the start timeout, which also drops the
lock before the DSP starts).

## Choosing a mode

1. 20/20 s2idle and 20/20 deep cycles pass (`run`), without and then with
   `--release-icc`.
2. `key power` and `key voldown` pass in the mode to be chosen.
3. The real path: `sudo systemctl unmask sleep.target suspend.target`, write
   the mode to `/run/ubuntu-gts9u/suspend-mode` (this boot only), `sudo
   systemctl restart ubuntu-gts9u-suspend-mode`, then `systemctl suspend`, wake
   with Power, and check the `ubuntu-gts9u-sleep-stats` journal line,
   NetworkManager, GNOME and the power-key helper.
4. Set the mode in `board_default()` of `ubuntu-gts9u-suspend-mode` for
   `samsung,gts9wifi`, or in `/etc/ubuntu-gts9u/suspend-mode` on one tablet.
