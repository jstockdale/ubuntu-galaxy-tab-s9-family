# Galaxy Tab S9 (SM-X710, gts9wifi): the interconnect boot floor

State on 2026-11-07, test unit squirtle, kernel 7.2.0-rc3 (fix2-s2 images,
`initcall_blacklist=bwmon_driver_init`).  This note covers why the
interconnect stays at its boot vote, why letting it go early in boot hangs the
boot-time pm_test cycle, what ships in the meantime
(`ubuntu-gts9u-icc-late-release`), and the experiment that should pinpoint the
hang.

## Summary

* Until every interconnect provider has reached `sync_state()`, every NoC,
  LLCC and DDR node keeps an INT_MAX vote, in the sleep set as well as the
  active one.  DDR does not scale (Stage 1 `ddr_stats`: residency only at
  2092 and 2736 MHz, no DDR low-power entry; `aosd` and `cxsd` count 0).
* On the X710 two providers never sync by themselves: gem_noc
  (`24100000.interconnect`) and mc_virt (`interconnect-1`).  Their last
  consumers are the DDR bandwidth monitors, `pmu@240b6400`
  (qcom,sm8550-cpu-bwmon) and `pmu@24091000` (qcom,sm8550-llcc-bwmon), whose
  driver is held back on purpose.
* Letting the floor go early in boot hangs the panel recovery's
  pm_test=platform cycle (about 24 s) every time: Stage 3 (bwmon built in,
  four boots) and Stage 3b (both PMUs disabled in the DT, no bwmon at all).
  Watchdog reset, boot loop.  So bwmon is not the cause; the timing is.
* Releasing the floor at runtime, after that cycle, held: Stage 0 on the
  baseline kernel (10 minutes, then a pm_test=platform rung with everything
  released, clean) and Stage 2 on the fix2 kernel (10 minutes; stopped before
  its rung).
* Interim: `ubuntu-gts9u-icc-late-release.service` releases the floor once
  the boot cycle has returned.  The root cause is open; the weekend plan below
  separates the three candidate mechanisms in two or three runs, only the
  last of which needs a reflash (a vendor_boot swap).

## How the floor works (drivers/interconnect, drivers/base)

* `icc_node_add()`: the qcom RPMh providers have no `get_bw`, so every node
  starts with `init_avg = init_peak = INT_MAX` and is written to the hardware
  that way.  That includes nodes no consumer ever names (the GIC's NoC ports
  in BCM SN1, the crypto engine CE0, LPASS LP0, the NSP CO0, ...): they sit at
  their maximum until sync, then at zero.
* `aggregate_requests()`: while `synced_state` is false, every node's
  aggregate is `max(requests, init)`.  Consumers' `icc_set_bw(0)` and
  `icc_disable()` in their suspend paths therefore change nothing on the bus
  until the floor is gone.
* `icc_sync_state()` is global.  It counts calls in a static counter and only
  acts when the count reaches `providers_count` (the DT nodes with
  `#interconnect-cells`: 14 here, matching the 14 "interconnect provider is in
  synced state" lines).  Then it clears the floor of every node of every
  provider at once.  A provider reading `state_synced=1` says nothing about
  its own floor: today twelve of them read 1 and all 43 consumer-visible nodes
  still read INT_MAX.  The release is the second of the two writes.
* `state_synced` (drivers/base/dd.c) accepts exactly `"1"`: `echo 1` sends
  `"1\n"` and gets `EINVAL`.  Use `printf 1`.  The write runs `sync_state()`
  under the device lock and cannot be undone.
* A provider syncs by itself when every *managed* consumer link is active
  (`__device_links_queue_sync_state()`).  The bwmon links stay "available"
  (supplier bound, consumer not).
* Tags: consumers vote `QCOM_ICC_TAG_ALWAYS` (active, wake and sleep sets) or
  `QCOM_ICC_TAG_ACTIVE_ONLY` (no sleep set).  The CPUs' DDR vote (cpufreq OPP,
  2.188 GB/s peak) and almost every config-NoC path are ACTIVE_ONLY; USB, UFS,
  PCIe and the display vote DDR with ALWAYS.  Keepalive BCMs (CN0, MC0, SH0,
  SN0, QUP0-2) keep a minimum vote in the active and wake sets only, never in
  the sleep set (bcm-voter.c).  So once the floor is gone, the sleep set holds
  exactly the ALWAYS votes of the devices that are still active.
* The sleep set takes effect whenever the APSS cluster power domain goes down:
  `rpmh_rsc_pd_callback()` flushes it on `GENPD_NOTIFY_PRE_OFF`, from cpuidle
  as well as from system suspend.  With the floor, the sleep set is INT_MAX
  everywhere and the cluster's idle states never let DDR or CX go.
* But not during a pm_test cycle: from `dpm_prepare()` to `dpm_complete()`
  genpd refuses to power a domain off while `prepared_count > 0`
  (`genpd_power_off()`), and the cluster domain holds the RSC itself and the
  CPUs' genpd devices.  The platform level's 5 s wait is `msleep(1000)` five
  times (`suspend_test()`, level > TEST_CORE), so CPU0 does idle, but only in
  CPU-level states.  During the cycle AOP never gets the sleep set; what
  changes is the active set, one `icc_disable()`/`icc_set_bw(0)` at a time as
  devices suspend.
* Config-NoC register access: every SM8550 config-NoC BCM is keepalive (CN0)
  except CN1, which holds only `qhs_display_cfg`, the display subsystem's
  register port.  Only the MDSS votes it (`cpu-cfg`), and it drops that vote
  in its own suspend.  Without the floor, any access to display-subsystem
  registers between the MDSS's suspend and its resume goes to an unvoted NoC
  port.  With the floor it is always at maximum.

Measured at the runtime release on Stage 2 (boot 972349f7,
/var/log/gts9u-stage0/0d-*): INT_MAX nodes 43 -> 0; RPMh messages per 5 s
602 -> 1094; the two busiest BCM addresses (0x50000, 0x50004) went from
0x6fffffff (both votes at maximum) to changing bandwidth votes, and two
others (0x50020, 0x50044) now toggle to zero (0x40000000).

## Timeline

| run | kernel | floor released | boot cycle | outcome |
| --- | --- | --- | --- | --- |
| Stage 0 (11-06/07) | baseline | at runtime, after rpmhpd, gcc, gpu_cc | passed (floor held) | 10 min clean; then a pm_test=platform rung with everything released: clean |
| Stage 1 | fix1-s1 | never (videocc and bwmon held) | passed | ok |
| Stage 2 | fix2-s2 | never at boot | passed | ok; GPU write loss gone (rpmhpd syncs at boot) |
| Stage 3, 4 boots | fix2-s3, bwmon built in | at 13.45 s (late_initcall) | hung | watchdog reset, loop |
| Stage 3b | fix3-s3, both PMUs disabled | at 13.30 s, no bwmon | hung | watchdog reset, loop |
| Stage 2 + release | fix2-s2 | at 163 s by stage0.sh | passed (floor held) | 10 min clean (UFS, GPU, links); stopped before the rung |

## Where it hangs: what the evidence says, and what it does not

* /var/log/kern.log (rsyslog keeps every kernel line, unlike the journal)
  ends at `PM: suspend entry (deep)` on all four Stage 3 boots and on 3b.
  The first line of the next boot is NUL-padded: an unclean reset.  rsyslog
  is frozen a few milliseconds after that line, so this only says the hang is
  inside the cycle (suspend, the late and noirq phases, the 5 s test wait or
  resume), not where.
* The Stage 3 note "last console line: dwc3-qcom a600000.usb: port-1 HS-PHY
  not in L2" cannot come from a console of that boot: `console_suspend_all()`
  marks *every* console suspended, the ramoops and sec_log consoles included,
  right after "Suspending console(s)".  That line is the Stage 1 sequence
  (24.21 s, inside a cycle that passed).  Treat the hang point as unknown.
* Between the early sync (13.3 s) and the cycle (24 s) the boot ran normally
  with the floor gone: rootfs, systemd, the PCI rescan and the WCN6855 probe
  (about 21.5 s), Bluetooth firmware, the USB gadget, the microSD logger at
  18 s.  The floor being gone is not the problem by itself; the cycle is.
* ramoops reports a header error on every boot ("ramoops: error in header"),
  so pstore has not yet carried a console across a reset on this unit.  TWRP's
  `/proc/last_kmsg` (the sec_log ring) is the other channel; its retention
  across a watchdog reset on the X710 is untested.

## What differs between the boot cycle and the runtime rung that passed

| | boot cycle, ~24 s | Stage 0 rung, runtime |
| --- | --- | --- |
| ADSP | not started: `ubuntu-gts9u-adsp-boot` is ordered after the cycle (32 s) | running (sensors, audio), voting DDR and NoCs through its own RSC; the X710 DT deletes its kernel-side DDR path |
| GPU | bound, never opened (first open ~45 s) | initialised, GMU cycling |
| panel | DDIC dead (`00 00 00`), DSI reads failing, fbcon on it | alive, GNOME |
| Wi-Fi | WCN6855 probed 1 to 2 s before, after the PCI rescan; MSIs through the GIC ITS | long running |
| deferred | LPI pinctrl, VA codec, sound card, aux_bridge still deferred; deferred-probe timeout 0.5 s before | all bound |
| floor dropped | 13.3 s, in the initramfs, before Wi-Fi and the USB gadget came up | minutes after boot |
| kernel | fix2/fix3 (videocc =y, aligned 3.36 GHz OPP overlay, bwmon =y on fix2-s3, RTC/qcom_stats/temp-alarm =y) | baseline |
| mem_sleep | deep (pinned by the helper) | deep (run-level.sh) |

The kernel row is a confound in both directions: a cycle with the floor gone
has never run on the fix kernels at runtime.  The first morning test closes
that, and it is also the safety gate for the late-release unit.

## Candidate mechanisms

The cycle cannot reach the sleep set (above), so whatever breaks does so in
the active set, while devices drop their votes one by one, or in a device
callback that relies on a vote it never takes.  Ranked by what fits:

**H1, a boot-only device state behind an unvoted path.**  Something that
exists only at the first cycle touches registers through a NoC port its own
driver never votes, which only the floor kept clocked.  The best-placed
suspect is the display: CN1 (`qhs_display_cfg`) is the one config-NoC BCM
without keepalive, and at the boot cycle the display stack is in a state it is
never in at runtime (DDIC dead, DSI reads failing, the first suspend after the
bootloader hand-off, fbcon drawing to a dead panel, Type-C DP HPD events
deferred "until the first resume").  The display clock controller
(`af00000`), whose MDSS GDSC the noirq phase switches off and on, sits next to
the MDSS (`ae00000`) and is probably behind the same port; that is to be
checked against the trace, not assumed.  Next in line: the WCN6855, probed one
to two seconds before the cycle after the PCI rescan and power-cycled by it,
whose DMA path to DDR sits on a non-keepalive BCM (SN7, `qns_pcie_mem_noc`)
and whose MSIs go through the GIC ITS (SN1, a BCM no DT node votes).
Predicts: W1 passes, W2 hangs, and its trace names the callback.

**H2, the APSS as the only voter.**  At runtime the ADSP votes DDR, LLCC and
CX through its own RSC; at the boot cycle it is not running, so once the floor
is gone the APSS's own suspend-time votes are all there is, and with rpmhpd
synced too (Stage 2 and 3) CX and MX can fall to whatever those leave, under
something still running.  Predicts: W1 hangs.

**H3, initialisation under real votes.**  Drivers that came up after 13.3 s
(the WCN6855 after the rescan, the USB gadget, Bluetooth) set up differently
when the floor is already gone.  Predicts: W2 passes, W3 hangs.

Out: bwmon (3b hangs without it), the sleep mode (deep in both), the release
itself (the same writes passed at runtime), and the sleep set (not reachable
during the cycle).

## Interim: the late release (device package)

`ubuntu-gts9u-icc-late-release.service`, `WantedBy=multi-user.target`:

* `ConditionFirmware=device-tree-compatible(samsung,gts9wifi)`: skipped on
  the Ultra before the helper starts (checked on the tablet with
  `systemd-analyze condition`); the helper checks the compatible again.
* `After=ubuntu-gts9u-panel-coldboot-recover.service
  ubuntu-gts9u-adsp-boot.service`, ordering only, and `Before=sleep.target`.
  It runs at about 33 s, in the state every runtime release so far was made
  in (cycle done, ADSP up).  The GPU's first open (~45 s) then happens with
  the floor gone, which is the upstream norm.
* It releases only interconnect providers (`#interconnect-cells`) that are
  unsynced and whose only unsatisfied consumers are the two bwmon PMUs with no
  driver bound; all or nothing; rpmhpd, gcc and gpu_cc are never written.
* It keeps the floor (and says why) when the cycle unit failed, is masked or
  never ran this boot, while a pm_test level is selected, or while
  `/etc/ubuntu-gts9u/icc-hold` exists.  A kernel without pm_test (the cycle
  unit's condition fails) releases.
* Records: `/run/ubuntu-gts9u/icc-release`, `/var/log/ubuntu-gts9u-icc-release.log`
  (group adm), and two `/dev/kmsg` markers, because this board's journal
  usually starts at ~40 s.  `ubuntu-gts9u-icc-late-release status|check` read
  only.

Suspend: the release lasts until reboot, so every later suspend runs without
the floor, which is the point (DDR low power, CX collapse).  What is proven:
a pm_test=platform rung on the baseline kernel.  Not yet: the same rung on the
fix kernels (morning test), and real s2idle and deep with the floor gone (the
suspend ladder).  If real suspend fails that way, `/etc/ubuntu-gts9u/icc-hold`
keeps the floor without touching anything else.

By hand: `printf 1 | sudo tee /sys/bus/platform/devices/24100000.interconnect/state_synced`
and the same for `interconnect-1` (not `echo`).  `sudo grep -c 2147483647
/sys/kernel/debug/interconnect/interconnect_summary` must then read 0.

## Weekend experiment

`tools/inplace/icc/weekend/icc-cycle-experiment.sh` (in the kit as
`~/icc/weekend/`), root.  Every rung logs to
`/var/log/gts9u-icc-exp/experiment.log` (group adm) line by line with a sync,
and marks the kernel log.  A runtime rung that hangs costs one watchdog reset
and comes back on a normal Stage 2 boot.  A boot rung disarms itself before
the cycle, so it cannot loop.  `--trace` sets `console_suspend=N`,
`pm_print_times` and `pm_debug_messages` for the cycle, so every device
callback reaches the ramoops and sec_log consoles while it runs.

0. **Calibrate the capture** (once, cheap): `arm-boot trace-only`, reboot;
   once the desktop is up, force a restart (hold Power + Volume Down) and read
   both channels: Linux `sudo sh ~/icc/weekend/icc-cycle-experiment.sh
   collect` (pstore), and TWRP `cat /proc/last_kmsg` (sec_log).  The boot
   cycle's `PM: calling ...` lines must survive in one of them; if neither
   keeps them, a hang can only be located by bisection (the `--level` and
   `--unbind-wifi` switches), not read.
1. **W0** (the morning test): the late release, then
   `sudo sh ~/pmtest/run-level.sh platform`.  Expected to pass, as Stage 0
   did on the baseline kernel.  If it hangs on the fix kernel, the release
   is unsafe for any later cycle or suspend: `sudo touch
   /etc/ubuntu-gts9u/icc-hold` and the rest of this list runs at runtime.
2. **W1**: `systemd-run --unit=gts9u-icc-exp --collect sh
   ~/icc/weekend/icc-cycle-experiment.sh runtime --stop-adsp --trace`.  The
   boot cycle's lone voter, recreated at runtime.  Hang: H2; go to W1b.
   Pass: H1 or H3; go to W2.
3. **W1b** (only if W1 hung): the same with `--level devices`.  Pass: the
   culprit is in the late or noirq phase (or the 5 s wait); hang: in an
   ordinary suspend callback.  With the capture working, the trace names it.
4. **W2**: `arm-boot release-before-cycle --trace`, reboot (Stage 2 images,
   no flashing).  The floor goes from userspace right before the boot cycle.
   Hang: H1, reproduced without the kernel's early sync; read the trace, then
   one variant per boot: `--unbind-wifi` (passes: the WCN6855; hangs: look at
   the display first).  Pass: H3; go to W3.
5. **W3** (only if W2 passed): `arm-boot kernel-early --trace`, then
   `sudo sh ~/display-exp/apply.sh fix2-s3`, reboot.  If it hangs, the boot
   after it (still fix2-s3) skips the panel cycle: panel dark, SSH works.
   `collect`, `sudo sh ~/display-exp/apply.sh fix2-s2`, `disarm`, reboot.

Reading a trace: the last `PM: calling <device> ...` line without its
`returned` names the callback that hung; `PM: suspend debug: Waiting for 5
second(s)` with nothing after it points at the wait (a timer, a work item or
an interrupt touching something already off); resume lines point at a resume
callback (`dispcc`/MDSS, `qcom-pcie`, `ath11k_pci` first).

What each outcome would mean for the fix:

* H1: the named driver takes the vote its hardware needs for as long as it
  touches it (a DT `interconnects` path, or keeping `cpu-cfg` until the
  last register access), and the floor can go at boot again; then bwmon can
  come back and DDR scales by load.
* H2: a CX/DDR vote held across the boot cycle, or the ADSP started before
  it (its own reason for coming after, the VA microphone timeout, would need
  re-testing).
* H3: per driver, from the trace.
* Independently, DISP-5 (a panel bring-up without the cycle) removes the
  boot cycle and with it the reason to hold the floor at boot.
