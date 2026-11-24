#!/bin/sh
# Deep sleeps of the SM-X710 with every disk filesystem frozen, to test
# whether the UFS storage comes back, without risking the data on it.
#   sudo sh ~/typec-roles/sleep-test.sh ro [CYCLES] [SECONDS] [release-floor] [hold=S] [aborts=N]
#        [pci=remove|pci=keep] [pm_test=LEVEL] [trace|trace=live] [mode=s2idle]
#        [gdsc=pcie0,pcie0phy,usb] [gdsc-ret=pcie0] [cxmx=ret] [root-port-d3] [rpmh]
#        [sensors=stop|sensors=hook] restart-ok
#   sudo sh ~/typec-roles/sleep-test.sh dry [release-floor] [hold=S] [pci=remove] [mode=s2idle]
#        [gdsc=pcie0,pcie0phy,usb] [gdsc-ret=pcie0] [cxmx=ret] restart-ok
#   sh ~/typec-roles/sleep-test.sh status
# ro (the default): CYCLES deep sleeps (default 3, at most 10) of SECONDS
# each (15, 30 or 60; default 30), each woken by the RTC.  dry: the same
# protection, reads and restart, and no sleep: run it first, to see that the
# tablet boots cleanly after a protected run, before any sleep is tried.
# restart-ok is required: it says that the tablet will be restarted at the
# end, whatever happens.  release-floor first releases the interconnect floor
# if it is held, the way its own HOLD message says.  hold=S is how long the
# result stays on screen and in /run before the restart (default 300 s).
# aborts=N is how many times in a run a sleep that the kernel gave up before
# the tablet slept (ABORTED: a wakeup on the way down, a double tap on the
# screen for one; with mode=s2idle also a sleep that a wakeup other than
# the RTC's cut short, WOKEN EARLY) is tried again (default 5, at most 20);
# one more ends the run with NO-SLEEP, and aborts=0 ends it at the first.
# Do not touch the screen during a run.
# Experiments, each off unless given (below, "The experiment options"):
# pci=remove removes every PCI root port, and the Wi-Fi with it, right
# before the freeze, until the restart; pci=keep keeps them and watches the
# Wi-Fi, and a kernel that collapses pcie_0_gdsc in the sleep (revision 11)
# without a guard that held this boot needs one or the other.
# root-port-d3 (with pci=keep) lets the PCIe root port go to D3hot in the
# sleeps, so that the host powers itself down with the Wi-Fi kept (revision
# 11's debug switch).  sensors=stop stops iio-sensor-proxy and
# hexagonrpcd-adsp-sensorspd before the run, until the restart, for an A/B of
# the audio DSP's wakes.  pm_test=LEVEL (freezer, devices,
# platform, processors or core) makes each cycle's suspend stop at that
# level, wait there and resume, with no SoC sleep call: a PM-TEST verdict,
# no depth.  mode=s2idle sleeps each cycle with rtcwake -m freeze
# (suspend-to-idle) instead of rtcwake -m mem (mode=deep, the default: PSCI
# SYSTEM_SUSPEND); with it pm_test= is freezer, devices or platform.  trace
# raises the console loglevel to 8 for each sleep, so that the PM core's
# lines reach ramoops (live up to the console suspend, the rest at the
# resume); trace=live also keeps the consoles printing while devices
# suspend and resume (console_suspend N), at a cost: read the risks below
# first.  gdsc=LIST (pcie0, pcie0phy and usb, comma-separated; with
# pci=remove only), gdsc-ret=pcie0 and cxmx=ret set the kernel's debug
# switches (the kernels of revisions 9 to 11) right before the freeze, until
# the restart: the PCIe and USB GDSCs collapse in the sleep instead of
# staying on, pcie_0_gdsc stays on (in retention) where revision 11 lets it
# collapse, and the CX and MX votes stay at RETENTION or above.  Every run
# prints the switches as they are, and how they booted.  rpmh turns the
# RPMh tracepoint on for the run and names, after each sleep, the sleep set
# that the application processor's RSC slept with (XO, CX, MX, bandwidth).
# status (no root) prints the latest run's result from /run.
#
# On 2026-11-21 one deep sleep (rtcwake -m mem -s 30) did not bring the UFS
# PHY back on this tablet: its calibration timed out, every reset of the UFS
# host failed, ext4 aborted its journal and the root went read-only.  A sleep
# test with the root writable can therefore cost data.  This one never sleeps
# with a disk filesystem writable, never writes to the disk after it froze
# them, and needs nothing from the disk after that:
#   - before anything else: root, this board, deep in mem_sleep (mode=s2idle:
#     freeze in /sys/power/state, and a cpuidle state of cpu0 with an s2idle
#     entry), the RTC, the
#     userdata partition, kernel.panic_on_warn and kernel.hung_task_panic 0
#     (the first resume with a filesystem frozen logs one kernel warning,
#     below, and writers that wait on a frozen filesystem are reported as
#     hung tasks after 60 s), a root filesystem with no recorded error
#     (ext4's error count 0 and state clean), no writable disk filesystem
#     but ext4 ones (those are frozen; exfat and the like cannot be: unmount
#     them first), none of them mounted where /proc/1/mounts has to escape
#     the path (a space in it, say), none of them running with errors=panic,
#     every tool, and the explicit restart-ok, or nothing is done;
#   - it records the state before (kernel, uptime, the interconnect floor,
#     suspend_stats, qcom_stats, the Type-C roles, the charger, ext4's error
#     counts and error behaviour) in /run and in ~/typec-roles/logs;
#   - every tool the run needs from then on, the shell, this script, and
#     every library and the dynamic loader they load, are copied to a RAM
#     root: a tmpfs of its own under /run, each file at the path it has on
#     the disk.  The rest of the run happens in a mount namespace of its own
#     with that tmpfs as its root (chroot), /proc, /sys, /dev and /run bound
#     in: every tool runs at its own path, from RAM, and nothing of the disk
#     is in reach but through /proc/1/root, for the freeze alone.  Swap on
#     the disk is switched off (swap goes to its blocks past the filesystem,
#     frozen or not);
#   - errors=continue: as the last step before the run moves to RAM, every
#     ext4 filesystem it is about to freeze is remounted with
#     errors=continue, unless it runs so already: the system's own mount(8)
#     in PID 1's mount namespace (nsenter -t 1 -m -- mount -o
#     remount,errors=continue MOUNTPOINT), and /proc/1/mounts has to show
#     the change, or the run puts back what it changed and stops before
#     freezing anything.  Only the mount option changes: the superblock's
#     default, which the next boot mounts with, is left alone.  Why: with
#     the UFS disk read-only at the block layer (below), an ext4 error under
#     errors=continue neither aborts the journal nor saves anything to the
#     disk (ext4_handle_error, fs/ext4/super.c).  Under remount-ro, this
#     root's default, ext4 aborts its journal, and jbd2 writes the error
#     into the journal's superblock all the same (jbd2_journal_abort ->
#     jbd2_journal_update_sb_errno; the block layer only warns about a
#     write to a read-only disk, once); the next mount then marks the
#     filesystem in error, skips the orphan cleanup and fails as on
#     2026-11-22 (below).  A stop that leaves the tablet running puts each
#     one's previous behaviour back; a restart needs nothing put back.  A
#     filesystem mounted after that step (a stick after a resume) is frozen
#     without it;
#   - the run goes on in a session of its own, so it ends even if SSH drops.
#     Its log is in /run/gts9u-sleep-test/<time>/ (readable without root,
#     latest -> the newest), and the run writes to nothing else but the
#     kernel log, pmsg and sysfs: the part on the disk shows the log on the
#     terminal (tail -f), so a terminal that stops reading, or an SSH server
#     stuck on the frozen disk, cannot stop the run;
#   - then every writable disk filesystem of the system (/proc/1/mounts) is
#     frozen, the deepest mount first and the root last, each one once
#     (FIFREEZE: fsfreeze -f through /proc/1/root, the system's root), and
#     the UFS disk is made read-only at the block layer as well, so that a
#     write that still came would be logged ("Trying to write to read-only
#     block-device") and fail the run, and so that ext4 saves no error to
#     its superblock any more (save_error_info is skipped on a read-only
#     disk; the journal abort above is not, hence errors=continue).  A
#     freeze that fails thaws what this run froze, puts errors= back and
#     stops it, without a restart;
#   - ext4's state of every filesystem the run freezes is checked before
#     the first freeze, once the disk is read-only, at the end, and right
#     before the restart: its error count against the one read before the
#     run (the count rises only for an error saved while its disk was
#     writable), its options in /proc/1/mounts (emergency_ro or shutdown:
#     ext4 gave up on it), and the kernel log since the run began ("EXT4-fs
#     error (device NAME)", "Aborting journal on device NAME-", "EXT4-fs
#     (NAME): Remounting filesystem read-only").  Before any sleep any of it
#     stops the run, thawed and not restarted, with the owner told to
#     e2fsck it from recovery.  After a sleep the run says SLEEP-TEST
#     ERROR-STATE (a journal aborted, an error saved) or SLEEP-TEST NOTE (an
#     error logged on the read-only UFS disk and neither saved nor followed
#     by an abort: errors=continue at work) with NEXT lines that say what
#     the next boot will probably do, and restarts all the same: a tablet
#     left running with its disk frozen cannot be used, and a restart keeps
#     the lines in pstore;
#   - a read of the first 64 MiB of userdata with O_DIRECT (opened read-only,
#     past the page cache), hashed, before the first sleep and after each
#     resume (dry: once more after 10 s), and before the first sleep the
#     kernel log since the run began must have no failure line;
#   - each cycle: rtcwake -m mem (mode=s2idle: rtcwake -m freeze); right
#     after it a second fsfreeze -f of
#     every filesystem the run froze, which must fail with EBUSY (it is
#     still frozen: freeze_super, fs/super.c), or the cycle fails, the
#     protection having failed (a freeze that succeeds there froze a
#     filesystem that something had thawed, and keeps it frozen from then
#     on); then the kernel log since the sleep (the UFS failure lines, I/O
#     and ext4 errors, a journal started on a frozen ext4, the diagnostic's
#     lines of a revision 8 kernel), the read, suspend_stats, and qcom_stats
#     aosd/cxsd/ddr (did the SoC reach AOSS sleep, CX collapse and DDR low
#     power at all? A cycle that did not is SHALLOW, not a pass).  The run
#     stops at the first failed cycle.  The first resume logs one kernel
#     warning, at fs/super.c:1919 (the kernel's own thaw of the frozen
#     filesystems, with freeze_filesystems 0, finds nothing of its own to
#     thaw: may_unfreeze); it is expected, and no rule here counts it;
#   - around each cycle, reads only (debugfs, sysfs, /proc, the kernel
#     log), evidence of how deep the sleep went and what may have kept the
#     SoC up: every qcom_stats file before and after (the SoC's states, each
#     subsystem's, DDR's modes and frequencies), the wakeup sources and the
#     interrupts that fired, whether the PCIe link went down and came back,
#     and whether the PCIe host came back at all (a host failure line, or
#     none), the power domains of the rails, the UFS, USB and PCIe, the
#     runtime state of the UFS host and of the USB controller, the DSPs, the
#     panel's backlight, the warmest thermal zone, the enable and prepare
#     counts of the UFS host's clocks and of XO (each clock's own debugfs
#     files, right before the sleep and right after the wake), the prepared
#     clocks below XO that keep it voted in the sleep set, the PCI functions'
#     state, what genpd powered off in the sleep (each domain's idle states),
#     the DSPs' interrupts, and the cycle's kernel lines; once before the
#     first sleep, every power domain, the rails in use, the bandwidth votes
#     (those in the sleep set marked), the PCIe functions, every prepared
#     clock, a line when those show the UFS PHY powered with its host
#     clock-gated (a leaked PHY power reference: ev_phy_leak), and the SoC's
#     and subsystems' sleep counts over 5 s awake.  The
#     terminal gets all of it ("evidence:" lines), pmsg five short lines per
#     cycle; a read that fails changes nothing;
#   - a suspend that the kernel gave up before the tablet slept (a wakeup
#     on the way down, such as the touchscreen's double tap, or a device
#     that refused to suspend), with the storage reading back as before and
#     no UFS failure line, is ABORTED, not FAIL: the same cycle is tried
#     again 5 s later.  aborts=N such attempts are tried again in a run; one
#     more ends it with NO-SLEEP (no sleep, and no storage verdict).  Any
#     doubt leaves the cycle a FAIL (aborted_attempt below);
#   - a suspend whose task freezer gave up after its 20 s ("Freezing ...
#     failed after", kernel/power/process.c), the storage as before, is
#     NO-SLEEP (freezer), not FAIL and not tried again: a task that waits
#     on the frozen disk is freezable, but one queued behind it on a memory
#     map's lock is not (freezer_failed below);
#   - one verdict line per cycle (SLEEP-TEST cycle ...), or per attempt of
#     it, one for the run (SLEEP-TEST RESULT: ...) and the next steps
#     (NEXT: ...), also in the kernel log and in /dev/pmsg0, which pstore
#     keeps for the next boot;
#     each cycle's diagnostic lines (cycle N/M: diagnostic: ...) go to the
#     result file and to /dev/pmsg0 as well, at most the first 12 and the
#     last 4 of a cycle there, so that a failed one is still readable after
#     the restart;
#   - progress lines in /dev/pmsg0 alone ("progress: ..."), so that a run
#     that never comes back from a sleep still leaves a record of how far it
#     got: the run's start in RAM with its options, the protection complete,
#     the PCI removal (pci=remove), and before each sleep (each attempt) the
#     cycle, written by the shell's own printf right before rtcwake.  Until
#     2026-11-22 nothing went to pmsg before the first verdict, so a run that
#     hung in its first sleep left no pmsg record at all (systemd-pstore's
#     archiving at boot erases the zone: ramoops_pstore_erase ->
#     persistent_ram_zap, fs/pstore/ram.c, and an empty zone gives no file);
#   - then, after hold=S, the restart: sysrq b, with every disk filesystem
#     still frozen, and a SLEEP-TEST RESTART line in pmsg just before it
#     (verify.sh looks for it in the next boot).  It needs nothing from the
#     disk (it worked on 2026-11-21), and the boot after it mounts the root
#     as after a power loss, with less to do: the freeze flushed the
#     journal, and the orphan cleanup runs at the mount as after any power
#     loss.  A frozen filesystem is never thawed after a sleep: the thaw
#     would be the first write after a resume, which this test does not
#     make; and a clean reboot needs it thawed (systemd stops services that
#     wait on it).
#
# Why a freeze and not a remount read-only (sysrq u, which this script used
# until 2026-11-22).  A remount read-only of a mounted ext4 (__ext4_remount ->
# ext4_mark_recovery_complete, fs/ext4/super.c) finds the orphan file not
# empty while programs hold deleted files open, as a running desktop always
# does, and records an ext4 error, "Orphan file not empty on read-only fs."
# A boot then cannot mount that root: with the error recorded,
# ext4_orphan_cleanup skips the cleanup, the same check fails at the mount
# (EFSCORRUPTED), and this port's initramfs has no fsck: on 2026-11-22 the
# tablet looped until the root was repaired with e2fsck from recovery.  The
# same holds for any ext4 error recorded on the running root, which is why
# the run keeps ext4 from recording one (errors=continue, the disk
# read-only) and says so when it did.  A freeze (freeze_super ->
# ext4_freeze) flushes the journal, clears needs_recovery, keeps
# orphan_present while the orphan file holds entries, and records no error;
# thawing undoes it (ext4_unfreeze).
# Not for the SM-X910: it has its own RAM-root harness
# (scripts/diagnostics/kernel10-ramroot-test), and this one knows the
# SM-X710's partitions, units and interconnect floor only.
#
# The interconnect floor.  ubuntu-gts9u-icc-late-release releases it about
# 45 s into a boot.  It holds it while /etc/ubuntu-gts9u/icc-hold exists, and
# once after a boot that released it and then ended without a clean shutdown
# (its loop breaker).  The 2026-11-21 failure came with the floor released;
# a run with the floor held sleeps less deep, and says so in every verdict.
# release-floor restarts that unit, its documented way to release the floor
# now (it writes its log and marker before this run freezes anything), and is
# refused while icc-hold exists: that file is the owner's.  A run always ends
# in a way the loop breaker counts as unclean, since only a clean stop with a
# writable disk clears its marker: the boot after a run in a boot that
# released the floor holds it once.  The run says so; the next run then needs
# release-floor.  The breaker itself is left as it is.
#
# The experiment options (2026-11-22), each off unless given; the run's
# header line, its pmsg start line and every RESULT line name the ones in
# force ("options: none" without them).  They are for telling apart why a
# deep sleep with the PCIe root port removed, on a USB cable to a PC, never
# came back (docs/resume-recovery.md).
#   - pci=remove (ro and dry): the PCIe host keeps its link, PHY, clocks and
#     bandwidth votes through every suspend while its root port is on the
#     bus (the port is hot-plug capable, pciehp is not built, so
#     pci_bridge_d3_possible() is false and the port stays in D0, and
#     dw_pcie_suspend_noirq() finds a function not in D3hot).  With every
#     root port removed the bus is empty and the host powers down in
#     suspend.  The root ports are the PCI devices directly on a host bridge
#     (their sysfs parent is pciDDDD:BB, the name pci_register_host_bridge()
#     gives it, drivers/pci/probe.c) with the class of a PCI bridge
#     (0x0604xx); on the SM-X710 that is 0000:00:00.0 (pcie0's root port,
#     pcie1 is disabled), the Wi-Fi 0000:01:00.0 below it.  Checked before
#     anything is done: at least one root port, and every PCI device one of
#     them or below one, or the run is refused (removing the ports would not
#     empty the bus).  The removal itself is the run's last step before the
#     first freeze, in RAM, with the disk still writable (what reacts to the
#     Wi-Fi going away writes first; then 10 s to settle): echo 1 > remove
#     on each root port (remove_store -> pci_stop_and_remove_bus_device_locked,
#     drivers/pci/pci-sysfs.c), the device count before and after logged; a
#     bus that is not empty after it stops the run before anything is frozen,
#     without a restart.  Wi-Fi is gone until the restart (nothing puts it
#     back), and an SSH session over Wi-Fi drops there while the run goes on:
#     run it over the USB cable, or read the result in pstore afterwards.
#     With the bus empty the host powers down in every suspend and up again
#     at the resume; if its link does not come back up there
#     (dw_pcie_wait_for_link() times out after about 3.7 s here, with
#     pcie-dwc-link-wait-longer-gts9wifi.patch), dw_pcie_resume_noirq()
#     leaves it powered down but marked suspended, and the next suspend would
#     read its registers with its clocks off (qcom_pcie_link_up()).  So a
#     cycle or an attempt whose kernel lines say the host did not come back
#     (pcie_down_re) ends the run: SLEEP-TEST RESULT: PCIE-DOWN, and the
#     restart.  Until a run has shown the link back ("PCIe Gen.2 x1 link
#     up" in a cycle's pcie evidence), give pci=remove one cycle.
#   - pm_test=LEVEL (ro): /sys/power/pm_test is set to LEVEL right before
#     each rtcwake and back to none right after it returns, so it is never
#     left set, whatever the run does.  The suspend then stops at LEVEL
#     (suspend_test(), kernel/power/suspend.c): freezer, after the task
#     freeze; devices, after the devices' suspend phase (not late, not
#     noirq); platform, after their noirq phase; processors, with the other
#     CPUs offline; core, after syscore_suspend() with interrupts off, short
#     of PSCI SYSTEM_SUSPEND alone.  It waits pm_test_delay (5 s; msleep,
#     mdelay at core; cut short by a pending wakeup), resumes, and the write
#     to /sys/power/state succeeds: suspend_stats counts a success and
#     rtcwake (util-linux 2.39.3) then reads the RTC alarm and disables it
#     before it exits 0, so no alarm is left armed.  cpuidle keeps running
#     during the wait (it is paused only with the CPUs' offlining,
#     pm_sleep_disable_secondary_cpus(), kernel/power/power.h), but from
#     devices on the CPU cluster's power domain cannot power off through it:
#     that domain's one device is the RPMh RSC (17a00000.rsc), dpm_prepare()
#     counted it (genpd_prepare()), and genpd_power_off() does nothing while
#     prepared_count > 0 (drivers/pmdomain/core.c; the CPUs' own devices are
#     syscore ones and are not prepared).  So at devices and platform the
#     CPUs reach their own power collapse at most, the cluster, the RSC and
#     the APSS stay up, and aosd, cxsd, ddr and apss can only stay +0 (the
#     2026-11-22 14:00 run); only at freezer, before the devices are
#     prepared, can idle take the cluster down.  (Until 2026-11-22 evening
#     this said idle could take the SoC down at devices and platform: wrong.)
#     The PM-TEST verdict shows qcom_stats' counts as they come.  The kernel
#     logs "suspend debug: Waiting for N second(s)." at the level; a cycle
#     without that line FAILs.  A pm_test cycle is PM-TEST, never PASS,
#     SHALLOW or DEEP: no sleep is asked for, so no depth is tested; it says
#     whether the suspend and resume path came back to that level with the
#     UFS readable.  With mode=s2idle the levels are freezer, devices and
#     platform: suspend-to-idle leaves suspend_enter() for its idle loop
#     right after the platform level, before processors and core, and the
#     kernel refuses those two with it (enter_state(): "Unsupported test mode
#     for suspend to idle"); the run refuses them first.  Refused with dry,
#     when the kernel does not offer LEVEL, or when a level is selected
#     already.  The boot-time panel cycle runs pm_test=platform at every boot
#     (ubuntu-gts9u-panel-coldboot-recover).
#   - trace (ro): kernel.printk's console loglevel goes to 8 right before
#     each rtcwake and back right after it (the other three numbers as they
#     were), and pm_debug_messages is 1 (as every ro run sets it, until the
#     restart that ends it): the PM
#     core's lines (info) and its phase lines (pm_pr_dbg, KERN_DEBUG:
#     "PM: noirq suspend of devices complete after ...") reach the consoles,
#     ramoops' among them.  With console_suspend Y (the default) the consoles
#     are suspended from "printk: Suspending console(s)" to the resume
#     (console_suspend_all() and console_resume_all(), kernel/printk/printk.c:
#     CON_SUSPENDED, and every printing context has seen it when the first
#     returns), so live in ramoops are the lines up to that one (the
#     freezer's, "Suspending console(s)"); the rest are printed when the
#     resume reaches console_resume_all(), before rtcwake returns.  A hang
#     then leaves "Suspending console(s)" as the last line if it is in the
#     devices' suspend, the SoC's sleep or the devices' resume, an earlier
#     line if it is in the freezer.  The cost: all of this tablet's consoles
#     are legacy ones, each record written to each console in the caller's
#     context with interrupts off (console_emit_next_record(),
#     kernel/printk/printk.c), and the hang-log kit measured 0.1 to 0.25 s
#     per console line (probably the two consoles on the debug UART, the
#     boot console qcom_geni0 and ttyMSM0, each polling the UART's FIFO with
#     timeouts, qcom_geni_serial_poll_bitfield(); not measured apart).
#     About 10 lines go out before the console suspend and, estimated from
#     the source, not counted on the tablet, 60 to 100 at the resume: a
#     sleep takes up to half a minute longer to return, with user space
#     still frozen.  pm_print_times is not used: it prints two lines per
#     device callback and phase (dpm_run_callback(), drivers/base/power/
#     main.c), thousands per sleep, which at that cost would take minutes;
#     the run refuses trace while pm_print_times is 1.  The 1 MiB console
#     zone (585 KiB of data with ECC 96) would hold them, the time is the
#     problem.  Refused with dry.
#   - trace=live (ro): trace, and console_suspend N for each sleep (back to
#     what it was right after), so that the lines reach ramoops while the
#     devices suspend and resume: a hang leaves its last phase line ("PM:
#     late suspend of devices complete ...", "Disabling non-boot CPUs ...",
#     "PM: noirq resume of devices complete ...").  Risks, all from the
#     source and none tried on this tablet: each line now costs its 0.1 to
#     0.25 s with interrupts off inside the suspend and resume, noirq phase
#     included, which can stretch timeouts there; the debug UART (uart7,
#     a9c000) stays powered through the sleep, since uart_suspend_port()
#     keeps a console port awake when console_suspend is N and never turns
#     its clocks off (drivers/tty/serial/serial_core.c), so the SoC may not
#     reach the state a run without it reaches, and a trace=live run says
#     nothing about depth; the boot console qcom_geni0 writes the same UART's
#     registers with no power management of its own, safe only as long as
#     that port stays clocked, which console_suspend N is what ensures.
#     Writing to ramoops and to sec_log is a copy into reserved DDR (no
#     device), safe at any point.  Use it after a trace run left
#     "Suspending console(s)" as the last line.
#   - mode=s2idle (ro and dry; mode=deep, the default, is accepted too):
#     each cycle is rtcwake -m freeze -s SECONDS, suspend-to-idle, instead
#     of rtcwake -m mem, which this kernel carries out as PSCI
#     SYSTEM_SUSPEND (mem_sleep deep).  rtcwake writes freeze to
#     /sys/power/state; /sys/power/mem_sleep plays no part and the run
#     leaves it as it is.  In suspend-to-idle every CPU stays online and
#     idles in its state with an s2idle entry (state1, cpu-sleep-X-0); the
#     last one down powers the cluster's domain off (dev_pm_genpd_suspend(),
#     drivers/cpuidle/cpuidle-psci.c), which flushes the RSC's sleep set,
#     and enters PSCI CPU_SUSPEND with the cluster's deepest state; an
#     armed wakeup IRQ (the RTC's alarm) ends the loop.  Refused, before
#     anything is done, when /sys/power/state offers no freeze or no
#     cpuidle state of cpu0 has an s2idle entry, and with pm_test=processors
#     or core (above).  dry checks the same and sleeps not.  Every cycle's
#     verdict word, and every RESULT's about a sleep, carries " (s2idle)"
#     ("SLEEP-TEST cycle 1/3: PASS (s2idle): ...", "SLEEP-TEST RESULT:
#     SHALLOW (s2idle): ..."; STOPPED, FAIL before any sleep and DRY name it
#     in their options alone), every line with the options names
#     mode=s2idle, and the progress line before each sleep says rtcwake -m
#     freeze.  Around each cycle the run also
#     reads (evidence, reads only) each CPU's cpuidle/state*/s2idle/{usage,
#     time}, the cluster domain's idle states (pm_genpd/power-domain-cluster/
#     idle_states: usage, s2idle, rejected per state) and suspend_stats'
#     last_hw_sleep if the kernel has it (this one has not); one evidence
#     line more per cycle goes to pmsg.  Asleep is the sum of the cycle's
#     "Timekeeping suspended for N seconds" lines, one for each time every
#     CPU was frozen at once; s2idle/time is not the sleep (it leaves out
#     the time every CPU was frozen: sched_clock is suspended then).
#     Verdicts: qcom_stats' aosd, cxsd and ddr decide PASS, SHALLOW and
#     DEPTH-UNKNOWN as in a deep run, and a cycle whose three counted is a
#     PASS whatever else.  Otherwise a sleep the kernel counted but that a
#     wakeup other than the RTC's alarm cut short (no CPU entered its s2idle
#     state, or less than half of SECONDS asleep; and pm_wakeup_irq or the
#     kernel's "Triggering wakeup from IRQ N" names an IRQ whose name has no
#     "rtc", or a wakeup source was pending) is WOKEN EARLY, not a pass: the
#     cycle is tried again like an ABORTED attempt, within the same aborts=N,
#     past which the run ends with NO-SLEEP (s2idle).  A cycle in which no
#     CPU entered its s2idle state and no such wakeup explains it FAILs.
#   - gdsc=LIST, gdsc-ret=pcie0 and cxmx=ret (ro and dry; the kernels of
#     revisions 9 to 11): the kernel's debug switches, run-time module
#     parameters that nothing but root's write changes
#     (gcc-sm8550-gdsc-debug-switches.patch, rpmhpd-debug-retention-floor.patch):
#       pcie0     gcc_sm8550 debug_pcie0_off: pcie_0_gdsc OFF_ON (Y), not RET_ON (N)
#       pcie0phy  gcc_sm8550 debug_pcie0_phy_off: pcie_0_phy_gdsc the same
#       usb       gcc_sm8550 debug_usb_off: both USB GDSCs the same
#                 (mainline's; the docked xHCI dies with it: run undocked)
#       cxmx=ret  rpmhpd debug_ret_floor: the APPS votes for cx.lvl and
#                 mx.lvl at RETENTION or above in every RPMh set
#     Each boots as the kernel's table has it: N on the kernels of revisions
#     9 and 10, and N but pcie0 Y on revision 11's, whose pcie_0_gdsc is
#     OFF_ON from the boot (gcc-sm8550-pcie0-gdsc-off-in-suspend.patch).  A
#     revision 11 kernel says how they booted (debug_boot_state, sw_boot);
#     on an earlier one every switch booted N.  gdsc=LIST writes Y to each
#     switch it names, gdsc-ret=pcie0 writes N to pcie0 (pcie_0_gdsc kept in
#     retention: revision 10's sleep, for an A/B run on revision 11), and
#     cxmx=ret writes Y to cxmx.  With a GDSC switch Y, genpd's power-off of
#     that GDSC at the end of the suspend's noirq phase collapses it (drops
#     the APPS vote of a PCIe one) instead of leaving it on in hardware.
#     gdsc= is refused without pci=remove: with the PCI devices kept the PCIe
#     host stays up through the sleep, and a PCIe GDSC that collapses would
#     take the controller away from a live host.  Refused, with nothing
#     changed, when the kernel does not offer a switch asked for (r8 and
#     before), or when any switch it offers does not read as it booted (set
#     by hand: the run's record would not say who set it).  The run writes
#     each switch it is given right before the freeze (after pci=remove's
#     removal, in RAM, the disk still writable), reads each back (one that
#     does not read what was written stops the run before anything is
#     frozen, with the ones that took put back as they booted), and never
#     puts them back after that: the restart that ends every run does.  The
#     header, the progress line right after the write and the one before
#     each sleep, every cycle's verdict and every RESULT line name the
#     switches as read then ("switches: pcie0 Y, pcie0phy N, usb Y, cxmx N",
#     with "; at boot: pcie0 Y" after it where a switch booted Y, and
#     "switches: not offered by this kernel" on a kernel without them),
#     whether a run asked for any or not, so the pmsg record of a run that
#     never comes back says which were set.
#   - pci=keep (ro): the PCI devices stay, and the run watches the Wi-Fi:
#     it reads the Wi-Fi's state after each resume (evidence: wifi), and a
#     cycle whose kernel lines say the PCIe host or the Wi-Fi did not come
#     back ends the run (PCIE-DOWN) and restarts it.  Give it one cycle.  In
#     a sleep with the PCI devices kept the PCIe host keeps its link (the
#     root port stays in D0, see pci=remove), unless root-port-d3 (below)
#     lets the root port go to D3hot.  Revision 11's kernel lets pcie_0_gdsc
#     collapse in system suspend (its pcie0 switch boots Y), and its guard
#     (the genpd flag of gcc-sm8550-pcie0-gdsc-off-in-suspend.patch and
#     pcie-qcom-keep-power-domain-while-link-kept.patch) keeps it on in a
#     suspend in which the host keeps its link: genpd leaves a domain with
#     GENPD_FLAG_ACTIVE_WAKEUP on for a device on the awake path, which the
#     host sets then.  The host logs at each resume what genpd did with its
#     domain ("system sleep: the link was kept, and its power domain stayed
#     on", guard_re below), the boot's panel cycle (pm_test=platform, at
#     every boot) among those, so the run knows from this boot's kernel log
#     whether the guard held: "held" is at least one such line and none
#     "the link was kept, but genpd powered its power domain off" (a
#     controller that lost its state under its live link).  On a kernel whose
#     pcie0 switch reads Y at the sleep (without gdsc-ret=pcie0) and whose
#     guard did not hold, or which has none (revision 11 as first prepared),
#     a sleep with the PCI devices kept is refused without pci=keep, with
#     nothing changed (dry, and a pm_test= level short of the noirq phase, do
#     not need it): genpd would collapse the GDSC under the live host, and
#     the Wi-Fi would be lost until a reboot, or the tablet panic at the
#     resume (read from the source and two reports, not run; README,
#     "Revision 11").  A line that says the guard did not hold ends a run
#     with the PCI devices kept, pci=keep or not (PCIE-DOWN).
#   - root-port-d3 (ro, with pci=keep; revision 11's kernel): Y to
#     pcie_qcom.debug_root_port_d3 right before the freeze, read back, and
#     nothing puts it back but the restart (pcie-qcom-debug-root-port-d3.patch;
#     the switch word rpd3 in the record).  The root port is hot-plug
#     capable and pciehp is not built, so the PCI core keeps it in D0 in a
#     suspend (pci_bridge_d3_possible()); the switch gives it bridge_d3, so
#     that it goes to D3hot after the Wi-Fi does, the host finds every
#     function in D3hot, sends PME_Turn_Off and powers down
#     (dw_pcie_suspend_noirq()), genpd collapses pcie_0_gdsc (the guard marks
#     the host out-of-band then), and the resume sets the host up again,
#     trains the link and restores the functions (pci_pm_resume_noirq());
#     ath11k powers its chip down and up at every suspend anyway.  It is a
#     sleep with the root port removed, but with the Wi-Fi kept: whether the
#     Wi-Fi comes back from it is what the run is for.  The cycle's pcie
#     evidence names the host's line ("the host powered down, and genpd
#     powered its power domain off"), the link coming back ("PCIe Gen.2 x1
#     link up"), and the wifi evidence the functions' state and whether
#     ath11k loaded its firmware again.  Refused, with nothing changed,
#     without pci=keep, with pci=remove (no root port left), with
#     gdsc-ret=pcie0 (a host powered down under pcie_0_gdsc kept on in
#     retention is the sleep that hung 8 times of 8), with dry or a pm_test=
#     level short of the noirq phase (freezer, devices), on a kernel without
#     the switch, and on one whose guard did not hold this boot.
#   - sensors=stop (ro): right after the record of the state before the run
#     (which names the audio DSP's clients), and before the floor's release,
#     systemctl stop iio-sensor-proxy.service hexagonrpcd-adsp-sensorspd.service,
#     then 10 s to settle.  The record says what each is afterwards; either
#     still active or activating stops the run before anything is frozen.
#     iio-sensor-proxy holds the SSC sensor streams the desktop asks for
#     (orientation, ambient light) open through libssc; hexagonrpcd -s is
#     the sensor protection domain's file server.  With both stopped, the
#     audio DSP's wakes in a sleep (qcom_stats adsp) and awake (evidence:
#     awake, 5 s) say whether those streams are what wakes it 30 to 50 times
#     a second.  A systemd suspend stops hexagonrpcd-adsp-sensorspd itself
#     (its unit's Conflicts=suspend.target) and a system-sleep hook starts it
#     again after the resume, but iio-sensor-proxy keeps running; rtcwake,
#     which this script sleeps with, goes around both, so without
#     sensors=stop both run through the test's sleeps.  Nothing starts them
#     again before the restart that ends the run (the boot starts both), and
#     a stop before any sleep leaves them stopped until the next boot: the
#     sensor protection domain's unit makes one attempt per boot (Restart=no,
#     as a failed attach can take the audio DSP, and the sound, down with
#     it), so start it by hand only knowing that.  Auto-rotation and
#     auto-brightness are off until then.  The options name it in every
#     RESULT line.
#   - rpmh (ro): what of Linux's own votes stands in the way of XO shutdown,
#     CX collapse and AOSS sleep (cxsd, aosd), from the commands that
#     rpmh-rsc writes to the application processor's RSC (the tracepoint
#     rpmh_send_msg, drivers/soc/qcom/trace-rpmh.h).  When the CPUs'
#     cluster powers down, rpmh_flush() (drivers/soc/qcom/rpmh.c) writes
#     the sleep and wake sets the hardware applies while the CPUs sleep,
#     whole, if a vote changed since the last flush; a resource whose sleep
#     vote is its wake vote is left out of both and stays at that vote
#     through the sleep.  The run turns the tracepoint on right before the
#     freeze (events/rpmh/rpmh_send_msg/enable in tracefs) and reads the
#     command DB once (debugfs cmd-db, the names of the RPMh addresses);
#     right before each sleep (before its clock read) it empties the trace
#     buffer, and after it
#     names the last flush before the sleep (by the kernel's
#     "Timekeeping suspended" line, the trace's clock being the kernel
#     log's): each address in it with its sleep and wake values, and which
#     of xo.lvl, cx.lvl and mx.lvl it left out, so that they stayed at their
#     vote through the sleep (with the last active vote the trace has).
#     xo.lvl, cx.lvl and mx.lvl are levels, 0 is off; a BCM's value with
#     bit 29 (0x20000000) set is a bandwidth vote.  "no flush" means no vote
#     changed between the emptying and the sleep: the RSC slept with an
#     earlier flush's sets.  A suspend-to-idle flushes at the sleep's own
#     instant, the time of the "Timekeeping suspended" line, which the
#     trace prints up to 1 us later (sched_clock stands still from the last
#     CPU's tick_freeze() on, ev_rpmh): such a flush is named "at ..., the
#     sleep's entry".  Up to revision 11 the script counted those commands
#     as after the sleep, and every suspend-to-idle said "no flush" (the
#     sets were flushed: the cluster's S1 power-off, which flushes, counted
#     +1 with no rejection).  With the kernel's clk_prepare and
#     clk_unprepare tracepoints (events/clk/, on with rpmh until the
#     restart) the trace also tells which of the clocks that keep XO voted
#     in the sleep set (the "XO held by" read right before the sleep) the
#     suspend unprepared before the sleep began, and which it left: "XO at
#     the entry to the sleep (clock trace): ..." (ev_xo_entry), also in pmsg
#     with the Type-C roles; and the whole flush goes to pmsg too, in eight
#     pieces at most, for a run whose terminal went away (over Wi-Fi).
#     Refused without the RPMh tracepoint (tracefs, a
#     kernel with CONFIG_FTRACE: the kit's kernels), with tracing off
#     (tracing_on 0) or with a trace clock other than local; without the
#     clock tracepoints the XO line says there is no clock trace.  It
#     changes no vote; a stop before any sleep turns them off again, and so
#     does the restart that ends every run.
set -u
export LC_ALL=C
umask 022

me=sleep-test
case ${1:-} in
__ram) ;;
*) kit=$(cd "$(dirname "$0")" && pwd) ;;
esac
base=/run/gts9u-sleep-test
board=samsung,gts9wifi
part=/dev/disk/by-partlabel/userdata
rtc=rtc0
# The system's root, from the run's own mount namespace and RAM root: the
# mount points of /proc/1/mounts are below it.  Only the freeze goes there.
sysroot=/proc/1/root
icc_state=/run/ubuntu-gts9u/icc-release
icc_log=/var/log/ubuntu-gts9u-icc-release.log
icc_hold=/etc/ubuntu-gts9u/icc-hold
icc_unit=ubuntu-gts9u-icc-late-release.service
icc_helper=/usr/libexec/ubuntu-gts9u-icc-late-release
# The loop breaker's marker: this boot's id in it means the next boot holds
# the floor, unless this boot ends with a clean stop on a writable disk.
icc_pending=/var/lib/ubuntu-gts9u/icc-release.pending
qstats=/sys/kernel/debug/qcom_stats
# The experiment options' knobs (the header, "The experiment options").
pm_test_ctl=/sys/power/pm_test
pm_test_delay_ctl=/sys/module/suspend/parameters/pm_test_delay
printk_ctl=/proc/sys/kernel/printk
console_suspend_ctl=/sys/module/printk/parameters/console_suspend
ignore_loglevel_ctl=/sys/module/printk/parameters/ignore_loglevel
print_times_ctl=/sys/power/pm_print_times
debug_messages_ctl=/sys/power/pm_debug_messages
# The kernel's debug switches (gdsc=, cxmx=; the header), by the word the
# options and the record give each: sw_file.
sw_gcc=/sys/module/gcc_sm8550/parameters
sw_rpmhpd=/sys/module/rpmhpd/parameters
sw_names='pcie0 pcie0phy usb cxmx'
# How the three GDSC switches booted, on revision 11's kernel ("pcie0 Y,
# pcie0phy N, usb N"): sw_boot.
sw_boot_file=$sw_gcc/debug_boot_state
# Revision 11's root port switch (root-port-d3; the record's word rpd3), N at
# every boot.
sw_pcie=/sys/module/pcie_qcom/parameters
sw_names="$sw_names rpd3"
# The line revision 11's PCIe host logs at each resume, and the one of a
# guard that did not hold (pcie-qcom-keep-power-domain-while-link-kept.patch).
guard_re='[0-9a-f]+\.pcie: system sleep: '
guard_bad_re='[0-9a-f]+\.pcie: system sleep: the link was kept, but genpd powered its power domain off'
guard_kept_re='[0-9a-f]+\.pcie: system sleep: the link was kept, and its power domain stayed on'
# sensors=stop: the audio DSP's sensor clients in userspace.
sensor_units='iio-sensor-proxy.service hexagonrpcd-adsp-sensorspd.service'
# sensors=hook: the device package's system-sleep hook, which stops them
# before a systemctl suspend in a device package with this branch's hook (it
# names iio-sensor-proxy.service; the earlier hook stops neither, and the
# option is refused with it).
sensors_hook=/usr/lib/systemd/system-sleep/ubuntu-gts9u-sensors
# mode=s2idle's checks and evidence (reads only): each CPU's cpuidle states,
# and the idle states of the CPU cluster's power domain (debugfs).
cpu_devs=/sys/devices/system/cpu
cluster_states=/sys/kernel/debug/pm_genpd/power-domain-cluster/idle_states
# trace's console loglevel: KERN_DEBUG lines (the PM core's pm_pr_dbg) and
# everything above them reach the consoles.
trace_level=8
# The first 64 MiB of userdata, read past the page cache.
read_bs=4M
read_count=16
# How long a read may take before the storage is called gone.
read_deadline=60
settle=10
# The tools the run calls from the RAM root, from the freeze on.  nsenter
# and readlink: a stop before any sleep puts errors= back through the
# system's mount(8), after the thaw; a filesystem mounted during the run is
# named by its device.
ram_tools='rtcwake dmesg dd sha256sum sleep grep awk sync blockdev fsfreeze nsenter readlink'
# The filesystem types a run freezes.  A writable disk mount of any other
# type is refused before anything is done (exfat, the SD card's, has no
# freeze at all: exfat_sops has no freeze_fs, so FIFREEZE fails with
# EOPNOTSUPP).
freeze_types=' ext4 '
ufs_fail='phy initialization timed-out|Failed to calibrate PHY|Enabling the controller failed|Host init failed|ufshcd_wl_resume failed|Trying to write to read-only block-device'
ufs_diag='calibration entry: SW_RESET|after the PCS_READY timeout: SW_RESET|PCS_READY .*SERDES_START|1 s after SERDES_START: SW_RESET|power-up write: PWRDN|power-up sequence entry: PHY power_count|power-up sequence failed, before phy_exit|setup_clocks repeated a PHY power-'
# The UFS PHY's supply as regulator_summary names its consumer (the PHY's
# device, then the supply), for ev_once's line about the PHY's power
# reference (ev_phy_leak).
ufs_phy_supply=1d80000.phy-vdda-phy
# The lines of the PCIe host and of its PHY.  Two of ufs_fail's are theirs as
# well, in the same words ("qcom-pcie 1c00000.pcie: Host init failed: -16",
# "qcom-qmp-pcie-phy 1c06000.phy: phy initialization timed-out"): a failure
# line of theirs does not say that the storage failed.
pcie_dev='[0-9a-f]+\.pcie: |qmp-pcie-phy [0-9a-f]+\.phy: '
# A journal handle started on a frozen ext4 (ext4_journal_check_start's
# WARN_ON, fs/ext4/ext4_jbd2.c:78): a transaction there would be written
# while the run thinks nothing is, and the next mount would discard it
# ("JBD2: Clearing recovery information on journal").  The kernel's warning
# line: "WARNING: fs/ext4/ext4_jbd2.c:78 at ext4_journal_check_start+...,
# CPU#N: COMM/PID" (7.2), "WARNING: CPU: N PID: N at fs/ext4/ext4_jbd2.c:78
# ..." before it.
journal_warn='WARNING: .*(fs/ext4/ext4_jbd2\.c:|ext4_journal_check_start)'
# A suspend that the kernel gave up on the way down, before the tablet
# slept: a wakeup that came in the meantime (the CPU freeze's or the
# freezer's check of it, an IRQ, a wakeup source with pm_debug_messages), or
# a device whose suspend callback refused (the PM core's "DRIVER DEVICE:
# failed to suspend[ async][ late| noirq]: error N", and the suspend's own
# lines after it).  "Triggering wakeup from IRQ" is in a real sleep's resume
# too: these lines count only with the kernel's own word that the suspend
# failed (aborted_attempt).  On 2026-11-22 the touchscreen's double tap did
# it in the noirq phase: "PM: Triggering wakeup from IRQ 199", then
# "Wakeup pending. Abort CPU freeze".
abort_re='Wakeup pending\. Abort CPU freeze|Wakeup pending, aborting suspend|Triggering wakeup from IRQ [1-9]|Freezing .* aborted after|Some devices failed to suspend, or early wake event detected|(late|noirq) suspend of devices failed|suspend of devices aborted after|: failed to suspend[a-z ]*: error -[0-9]'
# The task freezer gave up (not a wakeup: "failed", not "aborted";
# try_to_freeze_tasks, kernel/power/process.c), then names the tasks that
# did not freeze, one "task:NAME state:X ..." line each (sched_show_task),
# before it restarts the tasks ("Restarting tasks: Starting").
freezer_re='Freezing (user space processes|remaining freezable tasks) failed after'
# A PCIe host that did not come back from a suspend: its link wait timed out
# (dw_pcie_wait_for_link(), "Link failed to come up"), its init failed, or
# the PM core reports its noirq resume failed (pm_dev_err(),
# drivers/base/power/main.c, whose dev_fmt puts "PM: " after the device:
# "qcom-pcie 1c00000.pcie: PM: failed to resume async noirq: error -110").
# With pci=remove the run ends after such a cycle (the RESULT line
# PCIE-DOWN).
pcie_down_re='[0-9a-f]+\.pcie: (PM: )?(Link failed to come up|Host init failed|failed to resume[a-z ]*: error)'
# The Wi-Fi did not come back from a sleep with the PCI devices kept
# (pci=keep): the PCI core could not reach its configuration space through
# the link (pci_power_up(), drivers/pci/pci.c), ath11k or MHI said a resume
# step failed (ath11k_warn()/ath11k_err() lines of drivers/net/wireless/ath/
# ath11k/core.c and pci.c, MHI's), or the root port reported an error.
wifi_fail_re='Unable to change power state from [A-Za-z0-9]+ to D0, device inaccessible|ath11k_pci [0-9a-f:.]+: .*(failed|timeout|link down)|mhi mhi[0-9]+: .*(fail|[Ee]rror|timeout|not in valid state|Did not enter)|pcieport [0-9a-f:.]+: AER: '
# A line about the UFS or its disk (storage_lines) that says something
# failed (this, without case) is never an aborted attempt, whatever it is:
# a UFS device that refused to suspend, too.  "timing out command" and
# "rejecting I/O to offline device" are the SCSI layer's (scsi_lib.c).
storage_bad_re='fail|error|err[ =:]|timed?[ -]?out|timing out|abort|offline|rejecting'
# The SM-X710's touch controller (samsung_fts1ba90a), by its IRQ's name in
# /proc/interrupts: its double tap wakes the tablet.
touch_irq=fts1ba90a
# The pause before an aborted attempt's cycle is tried again.
abort_pause=5
# How long a freeze or a thaw may take (fsfreeze; the disk part synced just
# before).
freeze_deadline=120
# How long mount(8) may take to change a filesystem's errors= (a remount
# waits for the writeback in flight).
mount_deadline=60
# How many of a cycle's diagnostic lines go to the result file and to pmsg:
# the first diag_head and the last diag_tail, and one line for the ones
# between.  A resume whose calibration fails retries it 15 times, up to seven
# lines each (revision 11's PHY counts at the power-up sequence's entry,
# revision 10's power-up write, the entry, the state after the timeout, the
# late poll's outcome, the state 1 s on, and revision 11's counts of the
# failed power-up sequence); revision 11 also logs, at any time, the first 16
# setup_clocks calls of each kind that found the PHY power reference as
# they asked.  The terminal and the run's log in /run get
# every line, and those go with the restart.  pmsg is 128 KiB of ramoops
# (the hang-log kit's DTB), about 73 KiB of it data with ECC 96, and it is
# a ring: what goes there must leave room for the verdicts.  A cycle puts at
# most 17 diagnostic lines there, each cut at 300 characters; a passing one
# has only its entry lines, one to three, and revision 11's setup_clocks
# lines if any came (32 at most in a boot).  Ten cycles, the last one failed,
# come to well under 20 KiB with their verdicts; an aborted attempt adds its
# entry lines and its verdict, about 1 KiB, and a run has at most 21 of them.
# ext4's own lines, when it recorded or logged an error, add at most 6.
# Each cycle that was not given up before its sleep adds five evidence
# lines (ev_keep), at most 1600 bytes: 16 KiB for ten; mode=s2idle and
# rpmh add one each, at most 300 bytes more each, and a WOKEN EARLY attempt
# adds its evidence lines and its verdict, about 2.5 KiB, within the same
# 21.  The progress lines
# (progress) add the run's start, the PCI removal, the debug switches and
# the protection, under 1 KiB, and one line of about 200 bytes before each
# attempt at a sleep, at most 31 in a run: under 7 KiB in all.  Every
# verdict and RESULT line carries the debug switches, under 80 bytes each.
# The evidence before the first sleep adds one line, under 300 bytes, when
# it shows a leaked UFS PHY power reference (ev_phy_leak).
diag_head=12
diag_tail=4
# Evidence around each cycle (added after the r7 baselines of 2026-11-22,
# whose 13 cycles all resumed with aosd, cxsd and ddr at +0): how deep each
# sleep went and what may have kept the SoC out of AOSS sleep, CX collapse
# and DDR low power.  Reads only, of debugfs (not on the disk), sysfs, /proc
# and the kernel log, with the RAM root's tools; a read that fails gives "-"
# or nothing and changes nothing in the run.  The terminal and the run's log
# get every line ("cycle N/M: evidence: ..."), pmsg five short ones per
# cycle (six with mode=s2idle; ev_pmsg).  One read is not passive: ddr_stats
# asks the AOSS to bring its numbers up to date (a QMP message,
# qcom_ddr_stats_show in drivers/soc/qcom/qcom_stats.c), as every read of
# that file does.
genpd_sum=/sys/kernel/debug/pm_genpd/pm_genpd_summary
reg_sum=/sys/kernel/debug/regulator/regulator_summary
icc_sum=/sys/kernel/debug/interconnect/interconnect_summary
wake_sum=/sys/kernel/debug/wakeup_sources
# The clocks whose enable and prepare counts each cycle shows: XO, and the
# UFS host's five that the kernels up to revision 9 kept enabled, one more
# time at each runtime resume of the UFS host (522 to 525 on 2026-11-23),
# which revision 10's ufs-qcom-enable-only-lane-clocks.patch ends.  Read from
# each clock's own debugfs directory (clk_debug_create_one(),
# drivers/clk/clk.c: clk_enable_count and clk_prepare_count are the core's
# counters as plain numbers, clk_parent its parent's name), which resumes
# nothing and fails for no other clock.  Revision 10's script read
# clk_summary, whose read runtime-resumes every clock controller first and
# fails whole when one cannot resume (clk_pm_runtime_get_all(): with the ADSP
# stopped, "clk: Failed to runtime PM get '6d44000.codec' for clk 'fsgen'").
clk_dir=/sys/kernel/debug/clk
ev_clk_names='bi_tcxo tcsr_ufs_pad_clkref_en gcc_ufs_phy_axi_clk gcc_ufs_phy_ahb_clk gcc_ufs_phy_unipro_core_clk gcc_ufs_phy_ice_core_clk'
# The clock whose prepare count is XO's vote in the RPMh sleep set: clk-rpmh's
# bi_tcxo (RPMH_CXO_CLK) votes in the sleep, wake and active sets while it is
# prepared (__DEFINE_CLK_RPMH, drivers/clk/qcom/clk-rpmh.c), bi_tcxo_ao in
# the wake and active sets only.  XO voted in the sleep set rules out XO
# shutdown, and with it CX collapse and AOSS sleep (cxsd, aosd: README,
# "Evidence around each cycle").  A clock prepared below it keeps it
# prepared.
xo_clk=bi_tcxo
# The interrupt lines of the DSPs' messages and signals (IPCC, glink, SMP2P,
# the remoteprocs' q6v5 lines, the AOSS QMP mailbox, FastRPC): their counts
# across each sleep (evidence: dsp irq).
ev_irq_dsp='ipcc|glink|smp2p|q6v5|wdog|fatal|ready|handover|stop-ack|qmp|aoss|fastrpc|apr|qrtr'
# rpmh (an option): the tracepoint of rpmh-rsc's commands and its switch,
# in tracefs, and the command DB that names their addresses.
trace_dir=/sys/kernel/tracing
rpmh_event=$trace_dir/events/rpmh/rpmh_send_msg/enable
# With rpmh also the clock framework's prepare and unprepare tracepoints
# (trace_clk_prepare() and trace_clk_unprepare() in drivers/clk/clk.c: a
# clock's 0 -> 1 and 1 -> 0, its parents' with it), when the kernel has
# them: which clocks below XO the suspend left prepared (ev_xo_entry).
clk_events="$trace_dir/events/clk/clk_prepare/enable $trace_dir/events/clk/clk_unprepare/enable"
cmd_db=/sys/kernel/debug/cmd-db
ufs_ctl=/sys/bus/platform/devices/1d84000.ufshc
usb_ctl=/sys/bus/platform/devices/a600000.usb
pci_devs=/sys/bus/pci/devices
# The power domains each cycle shows (pm_genpd_summary): the rails, the
# UFS, USB and PCIe (Wi-Fi) domains, the display's and the CPU cluster's;
# any domain that is on as well.
ev_domains='^(cx|mx|mxc|mmcx|lcx|lmx|gfx|ebi|ufs_|usb|pcie|mdss|power-domain-cluster)'
# The kernel lines of a cycle the terminal gets (dmesg, all levels; the run
# sets pm_debug_messages): the PM core's (suspend-to-idle's too: "PM:
# suspend-to-idle", "PM: Triggering wakeup from IRQ N", "PM: resume from
# suspend-to-idle", "Timekeeping suspended for ..."), the CPUs', the
# storage's and the diagnostic's, the USB host's, the PCIe link's and the
# Wi-Fi's, the DSPs', the clocks', rails' and votes', warnings and errors.
# At most ev_klines,
# and not the Wi-Fi's per-packet noise, the CPUs' feature lines or the
# hung-task reports of writers waiting on the frozen disk.
ev_kernel='PM: |psci|CPU[0-9]+( is up|: Booted| killed)|non-boot CPUs|Timekeeping|[Ww]ake|ufs|qmp|calibrat|SW_RESET|PCS_READY|SERDES|sd 0:|scsi|I/O error|EXT4|dwc3|xhci|usb [0-9]|typec|tcpm|PCIe|pcie|L2|LTSSM|mhi|ath11k|remoteproc|q6v5|smp2p|ipcc|glink|rpmh|gdsc|genpd|icc|interconnect|qcom_stats|aoss|regulator|clk|WARNING|BUG|Freezing|Restarting|Filesystems sync|fail|error|timed? ?out|-110'
ev_kernel_skip='msdu_done|SANITY CHECK|blocked for more than|hung_task_timeout_secs|Tainted:'
ev_klines=150
# How to reach TWRP from a running or looping tablet (docs/porting-log.md,
# the TWRP flash: Volume Down + Power, then at once Volume Up + Power).
recovery_keys='hold Power and Volume Down until the screen goes dark, then at once Power and Volume Up, for TWRP'

usage() {
	sed -n '2,/^# status (no root)/s/^# //p' "$0" >&2
	exit 2
}

# ---------------------------------------------------------------- status ---
if [ "${1:-}" = status ]; then
	if [ ! -e "$base/latest" ]; then
		echo "no sleep test in $base this boot."
		echo 'A run that ended in a restart left its verdict and diagnostic lines in pmsg, which'
		echo 'pstore keeps for the next boot; the newest archive of it (group adm, or use sudo):'
		echo "  grep -rah 'gts9u sleep-test' \"\$(ls -d /var/lib/ubuntu-gts9u/pstore/[0-9]* | tail -n 1)\""
		exit 0
	fi
	echo "run: $base/latest -> $(readlink "$base/latest")"
	if [ -s "$base/latest/result" ]; then
		cat "$base/latest/result"
		# A cycle's verdict, and an ABORTED attempt's, come before the
		# run's: only the RESULT line ends a run.
		grep -q '^SLEEP-TEST RESULT: ' "$base/latest/result" ||
			echo 'no SLEEP-TEST RESULT line yet: the run is still going (an ABORTED cycle is tried again)'
	else
		echo 'no verdict yet: the run is still going'
	fi
	echo "--- the last 30 lines of $base/latest/log"
	tail -n 30 "$base/latest/log"
	exit 0
fi

# ------------------------------------------- for both parts of the run ---
num() { case ${1:-} in ''|*[!0-9]*) return 1 ;; esac; }

uptime_s() {
	upt=?
	read -r upt _ < /proc/uptime 2>/dev/null
	echo "$upt"
}

say() {
	line="[$(uptime_s)] $*"
	printf '%s\n' "$line" >> "$ramdir/log" 2>/dev/null
	printf '%s\n' "$line" 2>/dev/null || :
}

# The disk filesystems of the system that are writable now: every mount in
# /proc/1/mounts (the system's, not the run's own namespace) whose source is
# a device node (a fuse mount's /dev/fuse aside) and whose options do not
# start with ro, one line each: SOURCE MOUNTPOINT TYPE, in mount order.  A
# frozen filesystem stays writable here.  The shell reads /proc itself.
rw_disk_mounts() {
	while read -r msrc mdir mtype mopts _; do
		case $msrc in /dev/*) ;; *) continue ;; esac
		case $mtype in fuse|fuse.*) continue ;; esac
		case $mopts in ro|ro,*) ;; *) printf '%s %s %s\n' "$msrc" "$mdir" "$mtype" ;; esac
	done < /proc/1/mounts
}

# The order in which a run freezes the writable disk filesystems: one on a
# loop device first (it writes through to the file below it, which a frozen
# filesystem would hold up), then the deepest mount first (the reverse of
# the mount order), and the root's last, "/" before a bind mount of it (the
# bind mount's freeze then finds it frozen and is skipped).
freeze_order() { # ROOT-SOURCE
	rw_disk_mounts | awk -v root="$1" '
		{ l[NR] = $0; s[NR] = $1; m[NR] = $2 }
		END {
			for (n = NR; n > 0; n--) if (s[n] ~ /^\/dev\/loop/) print l[n]
			for (n = NR; n > 0; n--) if (s[n] !~ /^\/dev\/loop/ && s[n] != root) print l[n]
			for (n = NR; n > 0; n--) if (s[n] == root && m[n] == "/") print l[n]
			for (n = NR; n > 0; n--) if (s[n] == root && m[n] != "/") print l[n]
		}'
}

# The kernel's name of the block device SOURCE is (sda34 for /dev/sda34,
# dm-0 for a /dev/mapper or /dev/disk/by-* link to it): the name under
# /sys/fs/ext4 and in ext4's and jbd2's kernel lines.
dev_name() { # SOURCE
	dn=$(readlink -f "$1" 2>/dev/null) || dn=
	[ -n "$dn" ] || dn=$1
	echo "${dn##*/}"
}

# ext4's count of the errors recorded on the filesystem on the device NAME
# (sda34: /sys/fs/ext4/sda34/errors_count, the superblock's s_error_count as
# the kernel holds it), or - when it cannot be read.
ext4_errors() { # NAME
	ec=-
	read -r ec < "/sys/fs/ext4/$1/errors_count" 2>/dev/null
	num "$ec" || ec=-
	echo "$ec"
}

# The last error recorded there, as "FUNCTION:LINE", or nothing.
ext4_last_error() { # NAME
	ef= el=
	read -r ef < "/sys/fs/ext4/$1/last_error_func" 2>/dev/null
	read -r el < "/sys/fs/ext4/$1/last_error_line" 2>/dev/null
	[ -z "$ef" ] || echo "$ef:$el"
}

# The options of SOURCE's first mount in /proc/1/mounts.  ext4's own (its
# superblock's) are the same at every mount of it.
mount_opts() { # SOURCE
	mo=
	while read -r osrc _ _ oopts _; do
		[ "$osrc" = "$1" ] || continue
		mo=$oopts
		break
	done < /proc/1/mounts
	echo "$mo"
}

# The errors= word among SOURCE's options, or nothing: ext4 shows errors=
# only when it differs from the superblock's default (_ext4_show_options).
errors_word() { # SOURCE
	ew=,$(mount_opts "$1"),
	case $ew in
	*,errors=*) ew=${ew#*,errors=}; echo "${ew%%,*}" ;;
	esac
}

# The error behaviour SOURCE runs with: its errors= word, else the
# superblock's default SBDEFAULT.
errors_now() { # SOURCE SBDEFAULT
	en=$(errors_word "$1")
	echo "${en:-$2}"
}

# Run CMD in the background and wait for it SECONDS at most: its status, or
# 124 if it has not ended by then (it goes on).  A freeze waits for every
# writer in flight and syncs the filesystem; one that never ends must not
# hold the run up forever.
deadline() { # SECONDS CMD [ARGS]
	dlmax=$1
	shift
	"$@" &
	dlpid=$!
	dlwait=0
	while kill -0 "$dlpid" 2>/dev/null; do
		[ "$dlwait" -lt "$dlmax" ] || return 124
		sleep 1
		dlwait=$((dlwait + 1))
	done
	wait "$dlpid"
}

# mount(8) as the system runs it: the system's own binary ($mount_cmd), in
# PID 1's mount namespace, where the mount points of /proc/1/mounts are and
# where a remount of them is allowed (do_remount refuses a mount of another
# namespace); setns resets the root to that namespace's, so from the RAM
# root too the binary, its libraries, fstab and utab are the system's.  Only
# while nothing is frozen: before the first freeze, and after a thaw.
sys_mount() { # ARGS
	nsenter -t 1 -m -- "$mount_cmd" "$@"
}

# Remount the ext4 filesystem SOURCE on MOUNTPOINT with errors=WORD, and
# check that /proc/1/mounts shows WORD as its behaviour now; 1 and $em_why
# if it does not.  mount(8) is given the mount point alone: it takes the
# rest of the options from fstab (or the mount table) and puts -o's after
# them, so errors=WORD is the last errors= and the one ext4 keeps
# (ext4_parse_param); the check is what proves it.
errors_set() { # SOURCE MOUNTPOINT WORD SBDEFAULT
	em_why=
	deadline "$mount_deadline" sys_mount -o "remount,errors=$3" "$2" > "$ramdir/mount.out" 2>&1
	emrc=$?
	emnow=$(errors_now "$1" "$4")
	[ "$emrc" = 0 ] && [ "$emnow" = "$3" ] && return 0
	emo=
	read -r emo < "$ramdir/mount.out"
	if [ "$emrc" = 124 ]; then
		em_why="mount -o remount,errors=$3 $2 had not ended after ${mount_deadline}s"
	elif [ "$emrc" != 0 ]; then
		em_why="mount -o remount,errors=$3 $2 failed (status $emrc)${emo:+: $emo}"
	else
		em_why="mount -o remount,errors=$3 $2 went through, but $2 runs with errors=$emnow (/proc/1/mounts: $(mount_opts "$1"))"
	fi
	return 1
}

# errors=continue on every filesystem of "targets" (SOURCE MOUNTPOINT MODE
# SBDEFAULT) that does not run with it already.  Each one whose behaviour
# changed goes to "errors-mode" (SOURCE MOUNTPOINT PREVIOUS SBDEFAULT) for
# errors_back, before its check.  1 and $em_why at the first that did not
# take.
errors_continue() {
	: > "$ramdir/errors-mode"
	while read -r csrc cmp cmode cdef; do
		[ "$cmode" != continue ] || continue
		errors_set "$csrc" "$cmp" continue "$cdef"
		cok=$?
		[ "$(errors_now "$csrc" "$cdef")" = "$cmode" ] ||
			printf '%s %s %s %s\n' "$csrc" "$cmp" "$cmode" "$cdef" >> "$ramdir/errors-mode"
		if [ "$cok" != 0 ]; then
			em_why="errors=continue did not take on $cmp ($csrc): $em_why"
			return 1
		fi
		say "errors=continue: $cmp ($csrc), until the restart (it ran with errors=$cmode)"
	done < "$ramdir/targets"
}

# Put back the error behaviour of every filesystem in errors-mode, with
# nothing frozen: $back_done names those put back, $back_why those that
# keep errors=continue, and how to put each back by hand; 1 if any does.
errors_back() {
	back_done= back_why=
	[ -s "$ramdir/errors-mode" ] || return 0
	while read -r bsrc bmp bprev bdef; do
		# Not mounted any more: its options went with the mount.
		if [ -z "$(mount_opts "$bsrc")" ] || [ "$(errors_now "$bsrc" "$bdef")" = "$bprev" ] ||
			errors_set "$bsrc" "$bmp" "$bprev" "$bdef"; then
			back_done="$back_done $bmp"
		else
			back_why="$back_why; $bmp ($bsrc) keeps errors=continue until the next restart ($em_why; by hand: sudo mount -o remount,errors=$bprev $bmp)"
		fi
	done < "$ramdir/errors-mode"
	back_why=${back_why#; }
	[ -z "$back_why" ] || return 1
	: > "$ramdir/errors-mode"
}

# Swap on a disk: the kernel writes swap to the swap file's blocks below its
# filesystem, which neither read-only nor a freeze stops.  zram is RAM.
disk_swaps() {
	{
		read -r _
		while read -r sname _; do
			case $sname in /dev/zram*) ;; *) printf ' %s' "$sname" ;; esac
		done
	} < /proc/swaps
}

count_of() { # the Count of qcom_stats NAME, or -
	cnt=-
	if [ -r "$qstats/$1" ]; then
		while read -r ck cv _; do
			[ "$ck" = Count: ] && cnt=$cv
		done < "$qstats/$1"
	fi
	num "$cnt" || cnt=-
	echo "$cnt"
}

stat_of() { # suspend_stats NAME, or -
	sv=-
	# 2>/dev/null first: dash reports a redirection that fails before a
	# 2>/dev/null after it takes effect (last_hw_sleep is not in every kernel).
	read -r sv 2>/dev/null < "/sys/power/suspend_stats/$1"
	num "$sv" || sv=-
	echo "$sv"
}

delta() { # OLD NEW: +N, or ? when either is not a number
	if num "$1" && num "$2"; then echo "+$(($2 - $1))"; else echo '?'; fi
}

selected() { # the bracketed choice of a sysfs list
	sel=
	read -r sel < "$1" 2>/dev/null
	case $sel in *'['*) sel=${sel#*\[}; sel=${sel%%\]*} ;; esac
	echo "$sel"
}

# The file of the debug switch WORD (gdsc=, cxmx=; the header).
sw_file() { # WORD
	case $1 in
	pcie0) echo "$sw_gcc/debug_pcie0_off" ;;
	pcie0phy) echo "$sw_gcc/debug_pcie0_phy_off" ;;
	usb) echo "$sw_gcc/debug_usb_off" ;;
	cxmx) echo "$sw_rpmhpd/debug_ret_floor" ;;
	rpd3) echo "$sw_pcie/debug_root_port_d3" ;;
	esac
}

# The debug switch WORD as this kernel booted it: what debug_boot_state says
# on a kernel that has it (revision 11's, "pcie0 Y, pcie0phy N, usb N"), N
# on an earlier one (every switch of revisions 9 and 10 boots N), and N for
# cxmx and rpd3 always.  The shell reads sysfs itself.
sw_boot() { # WORD
	sbv=
	if [ "$1" != cxmx ] && [ "$1" != rpd3 ] && [ -e "$sw_boot_file" ]; then
		sbl=
		read -r sbl 2>/dev/null < "$sw_boot_file" || :
		case ", $sbl," in
		*", $1 Y,"*) sbv=Y ;;
		*", $1 N,"*) sbv=N ;;
		esac
	fi
	echo "${sbv:-N}"
}

# The debug switches as they read now, for the record: "switches: pcie0 Y,
# pcie0phy N, usb N, cxmx N", a switch this kernel does not offer left out,
# or "switches: not offered by this kernel"; then "; at boot: pcie0 Y" for
# the switches that booted Y (revision 11's pcie0), so that a Y says whether
# it is the kernel's or the run's.  The shell reads sysfs itself.
sw_now() {
	swl= swy=
	for swn in $sw_names; do
		swf=$(sw_file "$swn")
		[ -e "$swf" ] || continue
		swv=
		read -r swv 2>/dev/null < "$swf" || :
		swl="$swl, $swn ${swv:--}"
		[ "$(sw_boot "$swn")" != Y ] || swy="$swy, $swn Y"
	done
	if [ -n "$swl" ]; then
		echo "switches: ${swl#, }${swy:+; at boot: ${swy#, }}"
	else
		echo 'switches: not offered by this kernel'
	fi
}

roles() {
	rl=
	for rf in data_role power_role; do
		rv=$(selected "/sys/class/typec/port0/$rf")
		rl="$rl ${rf%_role}=${rv:-?}"
	done
	[ -e /sys/class/typec/port0-partner ] && rl="$rl partner=present" || rl="$rl partner=none"
	echo "${rl# }"
}

usb_count() {
	uc=0
	for ud in /sys/bus/usb/devices/[0-9]*; do
		case $ud in *:*) continue ;; esac
		[ -r "$ud/idVendor" ] && uc=$((uc + 1))
	done
	echo "$uc"
}

charger() {
	co=
	for cf in /sys/class/power_supply/*battery*/status /sys/class/power_supply/*battery*/capacity \
		/sys/class/power_supply/tcpm-source-psy-*/online; do
		[ -r "$cf" ] || continue
		cv=
		read -r cv < "$cf" 2>/dev/null
		co="$co ${cf#/sys/class/power_supply/}=$cv"
	done
	echo "${co# }"
}

# The interconnect floor: released when every interconnect provider reads
# state_synced 1 (the kernel drops the floor of all of them at once when the
# last one syncs), held otherwise.  The providers ubuntu-gts9u-icc-late-release
# looks at: platform devices with #interconnect-cells and state_synced.
floor_state() {
	fh=
	fn=0
	for fd in /sys/bus/platform/devices/*; do
		[ -e "$fd/state_synced" ] && [ -e "$fd/of_node/#interconnect-cells" ] || continue
		fn=$((fn + 1))
		fs=
		read -r fs < "$fd/state_synced" 2>/dev/null
		[ "$fs" = 1 ] || fh="$fh ${fd##*/}"
	done
	if [ "$fn" = 0 ]; then
		echo unknown
	elif [ -n "$fh" ]; then
		echo held
	else
		echo released
	fi
}

# The interconnect nodes still at the floor's vote (INT_MAX), or n/a when
# the summary cannot be read.  grep -c prints 0 and fails when there is
# none: a "|| echo n/a" after it printed a second line, "n/a", under the 0.
intmax_nodes() {
	imn=$(grep -c 2147483647 /sys/kernel/debug/interconnect/interconnect_summary 2>/dev/null)
	num "$imn" || imn=n/a
	echo "$imn"
}

# The PCI devices, by their sysfs names: " NAME" each, or nothing.  The
# shell reads sysfs itself; readlink is one of the RAM root's tools.
pci_names() {
	for pd in "$pci_devs"/*; do
		[ -e "$pd" ] || continue
		printf ' %s' "${pd##*/}"
	done
}

pci_count() {
	pcn=0
	for pd in "$pci_devs"/*; do
		[ -e "$pd" ] && pcn=$((pcn + 1))
	done
	echo "$pcn"
}

# The PCI root ports, one name a line: the devices directly on a host
# bridge (their directory's parent is the bridge's, pciDDDD:BB) with the
# class of a PCI-to-PCI bridge (0x0604xx).  Removing one removes everything
# below it.
pci_root_ports() {
	for pd in "$pci_devs"/*; do
		[ -e "$pd" ] || continue
		pr=$(readlink -f "$pd") || continue
		pp=${pr%/*}
		case ${pp##*/} in
		pci[0-9a-f][0-9a-f][0-9a-f][0-9a-f]:[0-9a-f][0-9a-f]) ;;
		*) continue ;;
		esac
		pc=
		read -r pc 2>/dev/null < "$pd/class"
		case $pc in 0x0604??) echo "${pd##*/}" ;; esac
	done
}

# The PCI devices that are neither one of the root ports ROOTS nor below
# one of them: " NAME" each, or nothing.  Removing the root ports leaves
# these on the bus.
pci_outside() { # ROOTS (names)
	for pd in "$pci_devs"/*; do
		[ -e "$pd" ] || continue
		pr=$(readlink -f "$pd") || pr=$pd
		pin=
		for po in $1; do
			pq=$(readlink -f "$pci_devs/$po") || continue
			case $pr/ in "$pq"/*) pin=1 ;; esac
		done
		[ -n "$pin" ] || printf ' %s' "${pd##*/}"
	done
}

# What pm_test=LEVEL leaves out of a suspend (suspend_test(),
# kernel/power/suspend.c): how far the suspend goes before it waits.
pm_test_what() { # LEVEL
	case $1 in
	freezer) echo 'the tasks frozen, no device suspended' ;;
	devices) echo "the devices' suspend phase, not their late and noirq ones" ;;
	platform) echo 'the devices suspended through their noirq phase' ;;
	processors) echo 'the devices suspended and the other CPUs offline' ;;
	core) echo 'everything but the SoC sleep call (PSCI SYSTEM_SUSPEND), interrupts off' ;;
	esac
}

# The words of the sleep mode (mode=, the header): rtcwake's -m, the label
# of the kernel's "PM: suspend entry (LABEL)", the mark every verdict word
# of an s2idle run carries (NO-SLEEP (freezer, s2idle) the one exception
# to its form), and what a pm_test cycle does not do.
mode_words() {
	if [ "$sleep_mode" = s2idle ]; then
		rtc_mode=freeze mem_label=s2idle mword=' (s2idle)' s2fz=', s2idle' no_sleep_call='no suspend-to-idle'
	else
		rtc_mode=mem mem_label=deep mword= s2fz= no_sleep_call='no SoC sleep call'
	fi
}

# mode=s2idle, for the run's header: the cpuidle states with an s2idle
# entry, each once, on how many CPUs; the cluster domain's idle states as
# they stand (usage, s2idle and rejected per state).
s2idle_summary() {
	s2n=0 s2all=
	for s2c in "$cpu_devs"/cpu[0-9]*; do
		[ -d "$s2c" ] || continue
		s2n=$((s2n + 1))
		s2all="$s2all$(s2idle_states "${s2c##*/cpu}")"
	done
	printf 'the cpuidle states with an s2idle entry:%s on %s CPUs; power-domain-cluster: ' \
		"$(printf '%s\n' $s2all | awk 'NF && !seen[$0]++ { printf " %s", $0 }')" "$s2n"
	if [ -r "$cluster_states" ]; then
		awk 'NR > 1 && NF >= 7 { printf "%s%s usage %s s2idle %s rejected %s", n++ ? ", " : "", $1, $3, $7, $4 }
			END { if (!n) printf "no state" }' "$cluster_states" 2>/dev/null
	else
		printf 'not readable'
	fi
	echo
}

# The cpuidle states of CPU N that have an s2idle entry (enter_s2idle, the
# ones suspend-to-idle uses): " stateM(NAME)" each, or nothing.
s2idle_states() { # N
	for sst in "$cpu_devs/cpu$1"/cpuidle/state[0-9]*; do
		[ -d "$sst/s2idle" ] || continue
		ssn=
		read -r ssn 2>/dev/null < "$sst/name" || :
		printf ' %s(%s)' "${sst##*/}" "${ssn:--}"
	done
}

# ------------------------------------------- the run, from the RAM root ---
# Everything below runs with the tools of the RAM root only: PATH is its
# tools directory, and in the run's own namespace its root is that tmpfs.

# The lines of a kernel log excerpt after the one holding MARKER, the run's
# own lines (its markers and verdicts) left out.
since() { # MARKER FILE
	awk -v m="$1" 'f && index($0, "gts9u sleep-test: ") == 0; index($0, m) {f = 1}' "$2"
}

# The kernel's lines since the run began ($rmarker), the run's own left out;
# all of them if that marker has left the ring buffer.
run_log() {
	dmesg > "$ramdir/kmsg.all" 2>/dev/null
	if grep -qF "$rmarker" "$ramdir/kmsg.all"; then
		since "$rmarker" "$ramdir/kmsg.all"
	else
		grep -vF 'gts9u sleep-test: ' "$ramdir/kmsg.all"
	fi
}

# A line for the record and for the next boot: the result file, the kernel
# log and pmsg.  The kernel log's copy is at the default level, 4, which
# pstore's console keeps only with a console loglevel above 4; pmsg is what
# survives the restart in any case.
verdict() {
	say "$*"
	printf '%s\n' "$*" >> "$ramdir/result" 2>/dev/null
	printf 'gts9u sleep-test: %s %.900s\n' "$stamp" "$*" >> /dev/kmsg 2>/dev/null
	[ ! -e /dev/pmsg0 ] || printf 'gts9u sleep-test: %s %s\n' "$stamp" "$*" >> /dev/pmsg0 2>/dev/null
}

# A diagnostic line for the record: the result file and pmsg.  Not the
# kernel log, which has the driver's line already: a copy there would be one
# more line for a later cycle's excerpt or for verify.sh to count.
keep() {
	printf '%s\n' "$*" >> "$ramdir/result" 2>/dev/null
	[ ! -e /dev/pmsg0 ] || printf 'gts9u sleep-test: %s %.300s\n' "$stamp" "$*" >> /dev/pmsg0 2>/dev/null
}

# A progress line for the next boot, in pmsg alone ("progress: ..."): how
# far a run got, for one that never comes back.  One printf of the shell's
# own, one write(2): /dev/pmsg0 hands it to ramoops, which copies it into
# its zone and updates the zone's ECC before the write returns
# (persistent_ram_write_user(), fs/pstore/ram_core.c; the zone is mapped
# write-combined, so the bytes are on their way to DDR, not in a cache).
progress() {
	[ ! -e /dev/pmsg0 ] || printf 'gts9u sleep-test: %s progress: %.280s\n' "$stamp" "$*" 2>/dev/null >> /dev/pmsg0
}

# The debug switches as they read now ($swnote), which every cycle's verdict
# and, through $optnote, every RESULT line ends with.
sw_note() {
	swnote=$(sw_now)
	optnote="; options: $options; $swnote"
}

# What to do after a debug switch that did not take stopped the run.
sw_next() {
	sn='NEXT: the kernel did not take that write (its own line, if it wrote one: sudo dmesg | grep -e gcc-sm8550 -e rpmhpd -e debug_root_port_d3; a root-port-d3 that found no root port of a qcom PCIe host gets -ENODEV); run again once that is understood'
	[ "$pci_remove" = 0 ] ||
		sn="$sn; what pci=remove removed stays removed until the restart, the Wi-Fi with it (sudo reboot brings it back)"
	echo "$sn"
}

# The experiment's knobs for one sleep (pm_test=, trace): set right before
# rtcwake (knobs_on) and put back right after it returns (knobs_off), so
# that nothing the run ends with, a stop or the restart, finds one set.
# $knobs_why says what did not take (pm_test= then does not sleep; a trace
# knob that did not take is said and the sleep goes on), $knobs_now what is
# in force.  With rpmh the trace buffer was emptied right before the
# attempt's clock read (the cycle's loop, before ev_before), so that what is
# in it after the sleep is this attempt's, and its clock prepares and
# unprepares follow that read.
knobs_on() {
	knobs_why= knobs_now=
	knobs_set=1
	[ "$rpmh" = 0 ] || knobs_now='rpmh trace emptied'
	if [ -n "$pm_test" ]; then
		printf '%s' "$pm_test" 2>/dev/null > "$pm_test_ctl"
		if [ "$(selected "$pm_test_ctl")" = "$pm_test" ]; then
			knobs_now="${knobs_now:+$knobs_now, }pm_test $pm_test"
		else
			knobs_why="pm_test=$pm_test did not take ($pm_test_ctl: $(ev_val "$pm_test_ctl"))"
		fi
	fi
	[ -n "$trace" ] || return 0
	set -- $printk_was
	printf '%s %s %s %s\n' "$trace_level" "$2" "$3" "$4" 2>/dev/null > "$printk_ctl"
	set -- $(ev_val "$printk_ctl")
	if [ "${1:-}" = "$trace_level" ]; then
		knobs_now="${knobs_now:+$knobs_now, }console loglevel $trace_level"
	else
		knobs_now="${knobs_now:+$knobs_now, }console loglevel NOT raised (kernel.printk: $*)"
	fi
	[ "$trace" = live ] || return 0
	printf N 2>/dev/null > "$console_suspend_ctl"
	if [ "$(ev_val "$console_suspend_ctl")" = N ]; then
		knobs_now="$knobs_now, console_suspend N"
	else
		knobs_now="$knobs_now, console_suspend NOT off ($(ev_val "$console_suspend_ctl"))"
	fi
}

knobs_off() {
	[ "${knobs_set:-0}" = 1 ] || return 0
	[ -z "$pm_test" ] || printf none 2>/dev/null > "$pm_test_ctl"
	if [ -n "$trace" ]; then
		printf '%s\n' "$printk_was" 2>/dev/null > "$printk_ctl"
		[ "$trace" != live ] || printf '%s' "$suspend_was" 2>/dev/null > "$console_suspend_ctl"
	fi
	knobs_set=0
}

# gdsc=, gdsc-ret= and cxmx=: Y to each debug switch of $sw_set, N to each
# of $sw_ret (gdsc-ret=pcie0), right before the freeze, each read back.  1
# and $sw_why when one does not read what was written.  They stay until the
# restart; a stop before any sleep puts them back as they booted (sw_off).
sw_on() {
	sw_why=
	sw_tried=1
	for swn in $sw_set $sw_ret; do
		swf=$(sw_file "$swn")
		case " $sw_ret " in *" $swn "*) sww=N ;; *) sww=Y ;; esac
		printf '%s' "$sww" 2>/dev/null > "$swf"
		swv=$(ev_val "$swf")
		[ "$swv" = "$sww" ] || sw_why="$sw_why; $swn ($swf) reads $swv after a write of $sww"
	done
	sw_why=${sw_why#; }
	[ -z "$sw_why" ]
}

# rpmh: the tracepoint as the run found it, at a stop before any sleep (the
# restart that ends a run turns it off anyway).
rpmh_off() {
	[ "${rpmh_set:-0}" = 1 ] || return 0
	printf '%s' "${rpmh_was:-0}" 2>/dev/null > "$rpmh_event"
	# The clock tracepoints too, each as it was ($clk_was, in $clk_events'
	# order).
	if [ "${clk_trace:-0}" = 1 ]; then
		set -- $clk_was
		for evf in $clk_events; do
			printf '%s' "${1:-0}" 2>/dev/null > "$evf"
			[ $# = 0 ] || shift
		done
	fi
	rpmh_set=0
}

# A stop before any sleep: each switch this run wrote back as it booted (N,
# but pcie0 Y on revision 11's kernel).  $sw_back says how that went, for
# the RESULT line; nothing when the run wrote none.
sw_off() {
	sw_back=
	[ "${sw_tried:-0}" = 1 ] || return 0
	swl= swas=N
	for swn in $sw_set $sw_ret; do
		swf=$(sw_file "$swn")
		swb=$(sw_boot "$swn")
		[ "$swb" = N ] || swas=boot
		printf '%s' "$swb" 2>/dev/null > "$swf"
		[ "$(ev_val "$swf")" = "$swb" ] || swl="$swl $swn ($swf)"
	done
	if [ "$swas" = N ]; then
		swas='back to N' swhand='off at the next restart, or by hand (echo N | sudo tee FILE)'
	else
		swas='back as they booted' swhand='as booted at the next restart, or by hand (echo Y or N | sudo tee FILE, as sw_boot says: debug_boot_state)'
	fi
	if [ -n "$swl" ]; then
		sw_back="; but the debug switch(es)$swl did not go $swas: $swhand"
	else
		sw_back="; the debug switches it set are $swas"
	fi
}

# Remove the PCI device NAME and everything below it (remove_store ->
# pci_stop_and_remove_bus_device_locked(), drivers/pci/pci-sysfs.c: it
# returns once the drivers below have let go).
pci_remove() { # NAME
	printf 1 2>/dev/null > "$pci_devs/$1/remove"
}

# pci=remove: every PCI root port off the bus, the last step before the
# first freeze, with the disk still writable.  The checks before the run
# refused a bus that this would not empty; they are made again here.  1 and
# $pci_why if the bus is not empty after it.
pci_remove_all() {
	pci_why=
	pn0=$(pci_count) pl0=$(pci_names)
	pci_roots=$(pci_root_ports | awk '{ printf " %s", $0 }')
	if [ -z "$pci_roots" ]; then
		if [ "$pn0" = 0 ]; then
			say "pci=remove: the PCI bus is empty already: nothing to remove"
			progress "pci=remove: the bus was empty already"
			return 0
		fi
		pci_why="pci=remove: no PCI root port among the $pn0 PCI devices (${pl0# }), nothing removed"
		return 1
	fi
	pci_left=$(pci_outside "$pci_roots")
	if [ -n "$pci_left" ]; then
		pci_why="pci=remove:$pci_left not below a PCI root port (${pci_roots# }): removing those would not empty the bus; nothing removed"
		return 1
	fi
	say "pci=remove: removing the PCI root port(s)$pci_roots and everything below them ($pn0 PCI devices:$pl0); Wi-Fi is gone from now until the restart, and an SSH session over Wi-Fi drops now: the run goes on, and its result is in pstore after the restart"
	for pr in $pci_roots; do
		pci_remove "$pr"
	done
	pn1=$(pci_count)
	if [ "$pn1" != 0 ]; then
		pci_why="pci=remove: $pn1 of $pn0 PCI devices still there after removing$pci_roots:$(pci_names)"
		return 1
	fi
	say "pci=remove: removed$pci_roots; PCI devices $pn0 -> 0; ${settle}s for the system to settle before the freeze"
	progress "pci=remove: removed$pci_roots; PCI devices $pn0 -> 0"
	sleep "$settle"
	pn2=$(pci_count)
	if [ "$pn2" != 0 ]; then
		pci_why="pci=remove: $pn2 PCI devices back on the bus ${settle}s after the removal:$(pci_names)"
		return 1
	fi
}

# pci=remove, after a cycle or an attempt: did the PCIe host fail to come
# back (pcie_down_re in its kernel lines, K)?  Its link wait timed out
# leaves it powered down but still marked suspended (dw_pcie_resume_noirq()
# returns -ETIMEDOUT after its deinit), and the next suspend would read its
# registers with its clocks off (qcom_pcie_link_up(), from
# dw_pcie_suspend_noirq(), drivers/pci/controller/dwc/): no further sleep in
# this boot.  pcie_down_end ends the run.
pcie_down() {
	if [ "$pci_remove" = 1 ]; then
		grep -qE "$pcie_down_re" "$k"
	elif [ "$pci_keep" = 1 ]; then
		# pci=keep: a host that lost its controller under the kept link,
		# or a Wi-Fi that did not come back, is not tried again.
		grep -qE "$pcie_down_re|$wifi_fail_re|$guard_bad_re" "$k"
	else
		# The PCI devices kept without pci=keep (a guard that held this
		# boot): a guard that did not hold in this sleep ends it too.
		grep -qE "$guard_bad_re" "$k"
	fi
}

pcie_down_end() {
	if [ "$pci_remove" = 0 ]; then
		pdown=$(grep -E "$pcie_down_re|$wifi_fail_re|$guard_bad_re" "$k" | awk '{sub(/^\[[^]]*\] /, ""); print; exit}')
		verdict "SLEEP-TEST RESULT: PCIE-DOWN$mword after $cyc: with the PCI devices kept, the PCIe host or the Wi-Fi did not come back ($pdown), and a later suspend would meet a controller that lost its registers under its link, so the run ends here; $passes of $cycles cycles came back with the UFS readable; floor $floor; kernel $kernel$optnote"
		verdict 'NEXT: no further sleep in this boot: the restart brings the PCIe host and the Wi-Fi back; the wifi and pcie evidence and the kernel lines of that cycle say what failed (README, "Revision 11: the runs")'
	else
		pdown=$(grep -E "$pcie_down_re" "$k" | awk '{sub(/^\[[^]]*\] /, ""); print; exit}')
		verdict "SLEEP-TEST RESULT: PCIE-DOWN$mword after $cyc: the PCIe host did not come back ($pdown), and with pci=remove a later suspend would read its registers with its clocks off, so the run ends here; $passes of $cycles cycles came back with the UFS readable; floor $floor; kernel $kernel$optnote"
		verdict 'NEXT: no further sleep in this boot: the restart brings the PCIe host back; the pcie evidence and kernel lines of that cycle say how its link failed'
	fi
	finish healthy
}

# Past the aborts=N budget (ABORTED attempts, and with mode=s2idle WOKEN
# EARLY ones): the end of the run, with no storage verdict.
no_sleep_end() {
	verdict "SLEEP-TEST RESULT: NO-SLEEP$mword at $cyc: $(abort_note), more than aborts=$aborts; $passes of $cycles cycles passed before it; this run does not answer whether the storage comes back from a deep sleep; floor $floor; kernel $kernel$optnote"
	if [ "$sleep_mode" = s2idle ]; then
		verdict 'NEXT: no storage verdict: after the restart run the test again, without touching the screen; the ABORTED and WOKEN EARLY lines name what ended each attempt'
	else
		verdict 'NEXT: no storage verdict: after the restart run the test again, without touching the screen; the ABORTED lines name what gave up each sleep'
	fi
	finish healthy
}

# What to do after pci=remove stopped the run.
pci_next() {
	case $pci_why in
	*'nothing removed'*)
		echo 'NEXT: nothing was removed: look at /sys/bus/pci/devices (ls -l shows where each device sits) and run again without pci=remove, or with the bus as the checks before the run expect it' ;;
	*)
		echo 'NEXT: what was removed stays removed until the restart, the Wi-Fi with it (sudo reboot brings it back); the PCI devices left are named above: run again without pci=remove until it is understood' ;;
	esac
}

# ------------------------------------------------- evidence, reads only ---
# Each of these reads and prints; none of them changes what the run decides.
# A redirection that fails is said before a 2>/dev/null after it takes
# effect (dash), so the reads put 2>/dev/null first.

# An evidence line for the next boot: pmsg only, cut at 300 characters like
# keep's (the terminal and the log have the long form; not the result file,
# not the kernel log).
ev_pmsg() {
	[ ! -e /dev/pmsg0 ] || printf 'gts9u sleep-test: %s %.300s\n' "$stamp" "$*" 2>/dev/null >> /dev/pmsg0
}

# The first line of FILE, or -.
ev_val() { # FILE
	evv=
	read -r evv 2>/dev/null < "$1" || :
	echo "${evv:--}"
}

# Every file of qcom_stats as "NAME COUNT TICKS" lines: the SoC's sleep
# states (aosd, cxsd, ddr), each subsystem's (apss is the CPUs'; adsp, cdsp,
# gpu, display, ...; the file of one that is not up is empty) and ddr_stats'
# low-power modes and frequencies ("ddr_stats:lpm:0xd4",
# "ddr_stats:freq:547").  TICKS count the 19.2 MHz always-on timer.
ev_stats() { # OUT
	for evf in "$qstats"/*; do
		[ -f "$evf" ] || continue
		awk -v n="${evf##*/}" '
			function val(key,   p, s) {
				p = index($0, key)
				if (!p) return 0
				s = substr($0, p + length(key))
				sub(/^[ \t]*/, "", s)
				sub(/[^0-9].*$/, "", s)
				return s == "" ? 0 : s
			}
			/^Count:/ { c = $2 }
			/^Accumulated Duration:/ { a = $3 }
			/^DDR LPM Stat Name:/ {
				k = $0
				sub(/^DDR LPM Stat Name:/, "", k)
				sub(/[ \t].*$/, "", k)
				print n ":lpm:" k, val("count:"), val("Duration (ticks):")
			}
			/^DDR Freq / {
				k = $0
				sub(/^DDR Freq /, "", k)
				sub(/[^0-9].*$/, "", k)
				print n ":freq:" k, val("count:"), val("Duration (ticks):")
			}
			END { if (c != "") print n, c, (a == "" ? 0 : a) }' "$evf" 2>/dev/null
	done 2>/dev/null > "$1"
}

# What changed between two ev_stats snapshots, one line: the SoC's states,
# the subsystems (count, and the time spent in their sleep), ddr_stats'
# modes and the time at each DDR frequency.  "-" when nothing was read.
ev_stats_delta() { # BEFORE AFTER
	awk -v hz=19200000 '
		function one(k, dc, dt) {
			return sprintf("%s %s%d%s", k, dc < 0 ? "" : "+", dc, dt > 0 ? sprintf(" %.1fs", dt) : "")
		}
		FILENAME == ARGV[1] { c0[$1] = $2; t0[$1] = $3; next }
		{
			k = $1
			dc = $2 - ((k in c0) ? c0[k] : 0)
			dt = ($3 - ((k in t0) ? t0[k] : 0)) / hz
			if (k == "aosd" || k == "cxsd" || k == "ddr")
				soc = soc (soc == "" ? "" : ", ") one(k, dc, dt)
			else if (k ~ /:lpm:/) {
				sub(/.*:lpm:/, "", k)
				lpm = lpm (lpm == "" ? "" : ", ") one(k, dc, dt)
			} else if (k ~ /:freq:/) {
				sub(/.*:freq:/, "", k)
				if (dt > 0) frq = frq (frq == "" ? "" : ", ") sprintf("%sMHz %.1fs", k, dt)
			} else
				ss = ss (ss == "" ? "" : ", ") one(k, dc, dt)
		}
		END {
			s = soc
			if (ss != "") s = s (s == "" ? "" : "; ") "subsystems " ss
			if (lpm != "") s = s (s == "" ? "" : "; ") "ddr modes " lpm
			if (frq != "") s = s (s == "" ? "" : "; ") "ddr time at " frq
			print s == "" ? "-" : s
		}' "$1" "$2" 2>/dev/null
}

# /proc/interrupts as "IRQ TOTAL NAME" lines: TOTAL over every CPU, NAME
# the action's (what follows Level or Edge and a flow handler's -name), or
# the rest of the line, spaces made _.
ev_irqs() { # OUT
	awk 'NR > 1 && NF > 1 {
		irq = $1
		sub(/:$/, "", irq)
		t = 0
		for (i = 2; i <= NF && $i ~ /^[0-9]+$/; i++) t += $i
		for (j = i; j <= NF; j++) if ($j ~ /^(Level|Edge)$/) break
		if (j < NF) {
			i = j + 1
			if (i < NF && $i ~ /^-/) i++
		}
		nm = ""
		for (; i <= NF; i++) nm = nm (nm == "" ? "" : "_") $i
		printf "%s %.0f %s\n", irq, t, nm == "" ? "-" : nm
	}' /proc/interrupts 2>/dev/null > "$1"
}

# The interrupts that fired between two ev_irqs snapshots, the most first,
# at most TOP of them: "21 pm8xxx_rtc_alarm +1, ...".
ev_irq_delta() { # BEFORE AFTER TOP
	awk -v top="$3" '
		FILENAME == ARGV[1] { b[$1] = $2; next }
		{
			d = $2 - (($1 in b) ? b[$1] : 0)
			if (d > 0) { n++; id[n] = $1; dv[n] = d; nm[n] = $3 }
		}
		END {
			for (j = 1; j <= top && j <= n; j++) {
				m = 0
				for (i = 1; i <= n; i++) if (!(i in used) && (m == 0 || dv[i] > dv[m])) m = i
				used[m] = 1
				printf "%s%s %s +%.0f", j == 1 ? "" : ", ", id[m], nm[m], dv[m]
			}
			if (n > top) printf ", and %d more", n - top
			if (!n) printf "none"
			printf "\n"
		}' "$1" "$2" 2>/dev/null
}

# The kernel's wakeup sources as "NAME EVENTS WAKEUPS" lines (the name is
# what comes before the nine numbers of each line, spaces made _).
ev_wake() { # OUT
	awk 'NR > 1 && NF >= 10 {
		nm = $1
		for (i = 2; i <= NF - 9; i++) nm = nm "_" $i
		print nm, $(NF - 7), $(NF - 6)
	}' "$wake_sum" 2>/dev/null > "$1"
}

# The wakeup sources that had events between two ev_wake snapshots:
# "NAME +EVENTS (wakeup +N)", or none.
ev_wake_delta() { # BEFORE AFTER
	awk '
		FILENAME == ARGV[1] { e[$1] = $2; w[$1] = $3; next }
		{
			de = $2 - (($1 in e) ? e[$1] : 0)
			dw = $3 - (($1 in w) ? w[$1] : 0)
			if (de > 0 || dw > 0)
				s = s (s == "" ? "" : ", ") sprintf("%s +%.0f%s", $1, de, dw > 0 ? sprintf(" (wakeup +%.0f)", dw) : "")
		}
		END { print s == "" ? "none" : s }' "$1" "$2" 2>/dev/null
}

# pm_genpd_summary: the domains of ev_domains and every domain that is on,
# each with its devices (domains), or the whole of it (all).
ev_genpd() { # domains|all
	awk -v re="$ev_domains" -v all="$1" '
		NR <= 3 { next }
		/^[^ ]/ {
			show = all == "all" || $1 ~ re || $2 == "on"
			if (show) print
			next
		}
		show && (all == "all" || /^    [^ ]/) { print }' "$genpd_sum" 2>/dev/null
}

# The state of what can keep the SoC up, read right before a sleep, one
# line: the UFS host's runtime state and clock, the USB controller's, each
# PCIe function's power state, the DSPs, the panel, the warmest thermal zone.
ev_state() {
	evs="ufs $(ev_val "$ufs_ctl/power/runtime_status")"
	for evd in "$ufs_ctl"/devfreq/*/cur_freq; do
		[ -f "$evd" ] || continue
		evs="$evs $(ev_val "$evd")Hz"
	done
	evs="$evs; usb $(ev_val "$usb_ctl/power/runtime_status") wakeup $(ev_val "$usb_ctl/power/wakeup")"
	evp=
	for evd in "$pci_devs"/*; do
		[ -e "$evd" ] || continue
		evp="$evp ${evd##*/} $(ev_val "$evd/power_state")"
	done
	evs="$evs; pci${evp:- none}"
	evr=
	for evd in /sys/class/remoteproc/remoteproc*; do
		[ -e "$evd" ] || continue
		evr="$evr $(ev_val "$evd/name")=$(ev_val "$evd/state")"
	done
	evs="$evs; remoteproc${evr:- none}"
	evb=
	for evd in /sys/class/backlight/*; do
		[ -e "$evd" ] || continue
		evb="$evb ${evd##*/} power=$(ev_val "$evd/bl_power") brightness=$(ev_val "$evd/brightness")"
	done
	evs="$evs; backlight${evb:- none}"
	evt=$(for evd in /sys/class/thermal/thermal_zone*; do
		[ -e "$evd" ] || continue
		printf '%s %s\n' "$(ev_val "$evd/temp")" "$(ev_val "$evd/type")"
	done | awk '$1 ~ /^-?[0-9]+$/ && (n == 0 || $1 + 0 > m + 0) { m = $1; t = $2; n = 1 }
		END { if (n) printf "%.1fC %s", m / 1000, t; else printf "-" }')
	echo "$evs; warmest $evt"
}

# Every clock as "NAME ENABLE PREPARE PARENT" lines (- for what was not
# read, or no parent), from each clock's own debugfs directory ($clk_dir;
# its counters and its parent's name, no clock controller resumed).
ev_clk_snap() { # OUT
	for evc in "$clk_dir"/*/; do
		[ -e "${evc}clk_enable_count" ] || continue
		evn=${evc%/}
		evn=${evn##*/}
		eve= evp= evq=
		read -r eve 2>/dev/null < "${evc}clk_enable_count" || :
		read -r evp 2>/dev/null < "${evc}clk_prepare_count" || :
		[ ! -e "${evc}clk_parent" ] || read -r evq 2>/dev/null < "${evc}clk_parent" || :
		printf '%s %s %s %s\n' "$evn" "${eve:--}" "${evp:--}" "${evq:--}"
	done 2>/dev/null > "$1"
}

# The counts of ev_clk_names in an ev_clk_snap copy, one line:
# "bi_tcxo 6/6, tcsr_ufs_pad_clkref_en 522/522, ..." (enable/prepare), "-"
# for a clock it does not list; with short, "xo 6/6, pad_clkref 522/522, axi
# 522/522, ..." for pmsg; "clock counts not readable" for an empty copy.
ev_clk_line() { # FILE full|short
	[ -s "$1" ] || { echo 'clock counts not readable'; return 0; }
	awk -v names="$ev_clk_names" -v short="$2" '
		BEGIN { n = split(names, want, " ") }
		$2 ~ /^[0-9]+$/ && $3 ~ /^[0-9]+$/ && !($1 in got) { got[$1] = $2 "/" $3 }
		END {
			for (i = 1; i <= n; i++) {
				nm = want[i]
				if (short == "short") {
					if (nm == "bi_tcxo") nm = "xo"
					else if (nm == "tcsr_ufs_pad_clkref_en") nm = "pad_clkref"
					else { sub(/^gcc_ufs_phy_/, "", nm); sub(/_clk$/, "", nm) }
				}
				printf "%s%s %s", (i > 1 ? ", " : ""), nm, ((want[i] in got) ? got[want[i]] : "-")
			}
			printf "\n"
		}' "$1" 2>/dev/null
}

# The clocks of an ev_clk_snap copy that keep XO ($xo_clk) voted in the
# sleep set while they are prepared: the prepared clocks below it (by their
# parents) with no prepared clock below them, "NAME ENABLE/PREPARE" each in
# name order, then "bi_tcxo itself N" for the prepares of XO's own
# consumers (a PHY's or a DSP's reference clock: XO's prepare count less one
# for each prepared clock directly below it, clk_core_prepare() counting a
# child once), "none", or "clock counts not readable".  A clock that its
# driver leaves prepared through its suspend holds XO through the sleep; the
# copies are made right before the sleep and right after the wake, so a
# clock in both is a candidate for that, not a proof (a driver's suspend may
# still drop it).
ev_xo_holders() { # FILE
	[ -s "$1" ] || { echo 'clock counts not readable'; return 0; }
	awk -v xo="$xo_clk" '
		{ en[$1] = $2; pr[$1] = $3; pa[$1] = $4 }
		END {
			for (c in pr) {
				if (c == xo || pr[c] !~ /^[0-9]+$/ || pr[c] + 0 == 0) continue
				x = pa[c]
				for (d = 0; d < 64 && x != "-" && x != "" && x != xo; d++) x = (x in pa) ? pa[x] : ""
				if (x == xo) held[c] = 1
			}
			for (c in held) if (pa[c] in held) inner[pa[c]] = 1
			own = (pr[xo] ~ /^[0-9]+$/) ? pr[xo] + 0 : 0
			for (c in held) if (pa[c] == xo) own--
			n = 0
			for (c in held) if (!(c in inner)) leaf[++n] = c
			# In name order (mawk has no sort).
			for (i = 2; i <= n; i++) {
				v = leaf[i]
				for (j = i - 1; j >= 1 && leaf[j] > v; j--) leaf[j + 1] = leaf[j]
				leaf[j + 1] = v
			}
			if (!n && own <= 0) { print "none"; exit }
			for (i = 1; i <= n; i++) printf "%s%s %s/%s", (i > 1 ? ", " : ""), leaf[i], en[leaf[i]], pr[leaf[i]]
			if (own > 0) printf "%s%s itself %d", n ? ", " : "", xo, own
			printf "\n"
		}' "$1" 2>/dev/null
}

# rpmh with the clock tracepoints (clk_trace): what of XO's prepared clocks
# the suspend left prepared when the sleep began.  CLOCKS is the ev_clk_snap
# copy right before the sleep (the trace was emptied right before it), CLKT
# the trace's clk_prepare and clk_unprepare lines of the attempt
# (ev_rpmh_snap), K the cycle's kernel lines: each clock prepared in CLOCKS
# is taken as prepared unless a clk_unprepare up to the sleep's entry
# (ev_sleep_bound) took it to 0, one that was not unless a clk_prepare made
# it 1 (the framework traces a clock's prepare and unprepare only on its 0
# -> 1 and 1 -> 0, its parents' with it, clk_core_prepare() and
# clk_core_unprepare()), and the parents are CLOCKS's.  One line: XO free
# (bi_tcxo unprepared: no clock of Linux votes XO in the sleep set, xo.lvl
# 0 in every set, clk_rpmh_unprepare()), or the clocks that still hold it,
# as ev_xo_holders names them (those prepared in the suspend marked), then
# what the suspend unprepared of the holders before it.  With short, for
# pmsg, the holders alone.  A prepare count above 1 is not seen moving, nor
# XO's own consumers (a PHY's reference clock) until XO's count reaches 0.
ev_xo_entry() { # CLOCKS CLKT K full|short
	[ "${clk_trace:-0}" = 1 ] || { echo 'no clock trace (the clk_prepare and clk_unprepare tracepoints were not on)'; return 0; }
	[ -s "$1" ] || { echo 'clock counts not readable'; return 0; }
	evb=$(ev_sleep_bound "$3")
	awk -v xo="$xo_clk" -v bound="${evb% *}" -v frozen="${evb#* }" -v short="$4" '
		'"$ev_bound_awk"'
		function below(c,   x, d) {
			x = pa[c]
			for (d = 0; d < 64 && x != "-" && x != "" && x != xo; d++) x = (x in pa) ? pa[x] : ""
			return x == xo
		}
		function sorted(a, n,   i, j, v) {
			for (i = 2; i <= n; i++) {
				v = a[i]
				for (j = i - 1; j >= 1 && a[j] > v; j--) a[j + 1] = a[j]
				a[j + 1] = v
			}
		}
		FILENAME == ARGV[1] { on[$1] = ($3 ~ /^[0-9]+$/ && $3 + 0 > 0); was[$1] = on[$1]; pa[$1] = $4; next }
		{
			for (i = 2; i <= NF; i++) if ($i == "clk_prepare:" || $i == "clk_unprepare:") break
			if (i > NF) next
			ts = $(i - 1)
			sub(/:$/, "", ts)
			if (ev_after_sleep(ts)) next
			c = $(i + 1)
			if ($i == "clk_prepare:") on[c] = 1
			else {
				on[c] = 0
				if (c == xo) xo_off = ts
			}
			seen++
		}
		END {
			if (!(xo in on)) { print "XO not in the clock read"; exit }
			if (!on[xo]) {
				if (short == "short") print "XO free"
				else printf "XO free at the entry to the sleep: %s unprepared%s, so no clock of Linux votes XO in the sleep set (%d prepares and unprepares traced before the sleep)\n", xo, (xo_off != "" ? " at " xo_off : ""), seen
				exit
			}
			for (c in on) if (c != xo && on[c] && below(c)) held[c] = 1
			for (c in held) if (pa[c] in held) inner[pa[c]] = 1
			n = 0
			for (c in held) if (!(c in inner)) leaf[++n] = c
			sorted(leaf, n)
			s = ""
			for (i = 1; i <= n; i++) s = s (i > 1 ? ", " : "") leaf[i] (was[leaf[i]] ? "" : " (prepared in the suspend)")
			if (s == "") s = xo " itself (one of its own consumers: a PHY\047s or a DSP\047s reference clock)"
			if (short == "short") { print "XO held by " s; exit }
			# The holders before the sleep (ev_xo_holders leaves) that it
			# unprepared.
			for (c in was) if (was[c] && c != xo && below(c)) wheld[c] = 1
			for (c in wheld) if (pa[c] in wheld) winner[pa[c]] = 1
			m = 0
			for (c in wheld) if (!(c in winner) && !on[c]) gone[++m] = c
			sorted(gone, m)
			g = ""
			for (i = 1; i <= m; i++) g = g (i > 1 ? ", " : "") gone[i]
			printf "XO still voted at the entry to the sleep, held by: %s; unprepared in the suspend before the sleep: %s (%d prepares and unprepares traced before the sleep)\n", s, (g == "" ? "none" : g), seen
		}' "$1" "$2" 2>/dev/null
}

# Each power domain with its idle states' usage and rejected counts added
# up ("NAME USAGE REJECTED" lines; pm_genpd/NAME/idle_states,
# idle_states_show(), drivers/pmdomain/core.c).  A usage is a power-off that
# went through, genpd_sync_power_off()'s at the end of a suspend's noirq
# phase among them: for a rail (cx, mx, ...) the moment rpmhpd votes it off
# (level 0) in every set, for a GDSC its gdsc_disable().  One awk for every
# domain.
ev_genpd_offs() { # OUT
	awk 'FNR == 1 { if (nm != "") print nm, u + 0, r + 0; nm = FILENAME; sub(/\/idle_states$/, "", nm); sub(/.*\//, "", nm); u = r = 0; next }
		$3 ~ /^[0-9]+$/ && $4 ~ /^[0-9]+$/ { u += $3; r += $4 }
		END { if (nm != "") print nm, u + 0, r + 0 }' /sys/kernel/debug/pm_genpd/*/idle_states 2>/dev/null > "$1"
}

# What genpd powered off between two ev_genpd_offs snapshots: every domain
# of ev_domains, "cx +1, mx +0, ..." ("(rejected +N)" after one that was
# refused); with short, the rails and the domains of the UFS, PCIe and USB
# alone, for pmsg; "-" when nothing was read.
ev_genpd_offs_delta() { # BEFORE AFTER full|short
	awk -v re="$ev_domains" -v short="$3" '
		BEGIN { split("cx mx mmcx ebi pcie_0_gdsc pcie_0_phy_gdsc usb30_prim_gdsc ufs_phy_gdsc ufs_mem_phy_gdsc", k, " ")
			for (i in k) key[k[i]] = 1 }
		FILENAME == ARGV[1] { u[$1] = $2; r[$1] = $3; next }
		$1 ~ re && ($1 in u) && (short != "short" || ($1 in key)) {
			du = $2 - u[$1]; dr = $3 - r[$1]
			s = s (s == "" ? "" : ", ") $1 " +" du (dr ? " (rejected +" dr ")" : "")
		}
		END { print s == "" ? "-" : s }' "$1" "$2" 2>/dev/null
}

# The DSPs' interrupt lines (ev_irq_dsp) that fired between two ev_irqs
# snapshots: "13 ipcc_0 +40, 16 smp2p-adsp +2", or none.
ev_irq_dsp_delta() { # BEFORE AFTER
	awk -v re="$ev_irq_dsp" '
		FILENAME == ARGV[1] { b[$1] = $2; next }
		$3 ~ re { d = $2 - (($1 in b) ? b[$1] : 0); if (d > 0) s = s (s == "" ? "" : ", ") sprintf("%s %s +%.0f", $1, $3, d) }
		END { print s == "" ? "none" : s }' "$1" "$2" 2>/dev/null
}

# The PCI functions as they are now: each one's driver, power state and
# link speed ("0000:01:00.0 ath11k_pci D0 5.0 GT/s PCIe"), or "pci none".
# The power state is the PCI core's record; the link speed a read of the
# function's link status, which the host refuses when its link is down
# ("Unknown" then, dw_pcie_other_conf_map_bus()).
ev_pci_state() {
	evp=
	for evd in "$pci_devs"/*; do
		[ -e "$evd" ] || continue
		evdr=-
		if [ -e "$evd/driver" ]; then
			evdr=$(readlink "$evd/driver" 2>/dev/null)
			evdr=${evdr##*/}
		fi
		evp="$evp, ${evd##*/} ${evdr:--} $(ev_val "$evd/power_state") $(ev_val "$evd/current_link_speed")"
	done
	if [ -n "$evp" ]; then
		echo "pci ${evp#, }"
	else
		echo 'pci none'
	fi
}

# rpmh: the trace's lines of rpmh-rsc's commands, and those of the clock
# framework's prepares and unprepares (when the run turned those
# tracepoints on: clk_trace), copied to OUT in one read (reading the trace
# pauses tracing while the file is open).  ev_rpmh and ev_xo_entry each take
# their own lines of it.
ev_rpmh_snap() { # OUT
	awk '/ rpmh_send_msg: | clk_(un)?prepare: /' "$trace_dir/trace" 2>/dev/null > "$1"
}

# The sleep's time in the cycle's kernel lines K, as ev_rpmh and ev_xo_entry
# take it: "TIME tk" from the first "Timekeeping suspended for" line (the
# sleep's own instant, see ev_rpmh), else "TIME exit" from "PM: suspend
# exit", else nothing.
ev_sleep_bound() { # K
	awk '/Timekeeping suspended for / && s == "" { s = $0; k = "tk" } /PM: suspend exit/ && x == "" { x = $0 }
		END { if (s == "") { s = x; k = "exit" } if (match(s, /^\[ *[0-9]+\.[0-9]+\]/)) { s = substr(s, RSTART + 1, RLENGTH - 2); gsub(/ /, "", s); print s, k } }' "$1" 2>/dev/null
}

# awk functions for a trace line's time TS against the sleep's: after it,
# or at its entry (the frozen instant, up to 1 us after a "Timekeeping
# suspended" line's time; ev_rpmh).  For awk programs given bound= and
# frozen= (ev_sleep_bound's two words).
ev_bound_awk='
		function ev_after_sleep(ts) {
			if (bound == "") return 0
			if (frozen == "tk") return ts + 0 > bound + 0.0000015
			return ts + 0 >= bound + 0
		}
		function ev_at_entry(ts) {
			return bound != "" && frozen == "tk" && ts + 0 >= bound - 0.0000005 && ts + 0 <= bound + 0.0000015
		}'

# rpmh: the last flush of the sleep and wake sets before the sleep, from
# TRACE (the rpmh_send_msg lines of this attempt) and the cycle's kernel
# lines K: the sleep is where "Timekeeping suspended" is in K (the trace's
# clock is the kernel log's, and it stands still in the sleep), else "PM:
# suspend exit"; a sleep or wake command more than 1 ms after the one before
# it starts another flush.  The time of the "Timekeeping suspended" line is
# the sleep's own instant: the last CPU stops sched_clock, the clock of the
# kernel log and of the trace, before anything sleeps (tick_freeze(),
# kernel/time/tick-common.c), and the line is printed at the resume before
# it runs again (timekeeping_resume(), then sched_clock_resume(), in
# tick_unfreeze() and in the syscore resume of a deep sleep).  A
# suspend-to-idle flushes the sets after that, when the last CPU takes the
# cluster's power domain down from its idle state
# (psci_enter_s2idle_domain_idle_state() -> dev_pm_genpd_suspend() ->
# the RSC's GENPD_NOTIFY_PRE_OFF, rpmh_rsc_pd_callback() -> rpmh_flush()):
# its commands carry that same instant, which the trace prints rounded to
# the microsecond and the kernel log cut to it, so up to 1 us after the
# line's time.  Such a flush is the sleep's, "at the sleep's entry"; a deep
# sleep flushes before (the PSCI syscore hook, whose suspend runs before
# sched_clock's).  Revision 11's script counted a command at the line's own
# time as after the sleep, and every suspend-to-idle with rpmh (revision 10's
# runs 5 and 6, revision 11's 1, 4 and 5) said "no flush" for it.  One line:
# the flush's commands, "NAME SLEEP/WAKE" each (the address when the command
# DB does not name it; a VRM's register as NAME+4 and so on), then which of
# xo.lvl, cx.lvl and mx.lvl it left out (sleep = wake: held at that vote
# through the sleep), each with the last active vote before the sleep, if
# the trace has one.  With short, the levels and the BCMs with a vote in the
# sleep set alone, for pmsg.
ev_rpmh() { # TRACE K full|short
	grep -q ' rpmh_send_msg: ' "$1" 2>/dev/null || { echo 'no RPMh command in the trace'; return 0; }
	evb=$(ev_sleep_bound "$2")
	awk -v bound="${evb% *}" -v frozen="${evb#* }" -v short="$3" -v keys='xo.lvl cx.lvl mx.lvl' '
		function hex(s,   i, c, v) {
			s = tolower(s)
			sub(/^0x/, "", s)
			v = 0
			for (i = 1; i <= length(s); i++) {
				c = index("0123456789abcdef", substr(s, i, 1))
				if (!c) return -1
				v = v * 16 + c - 1
			}
			return v
		}
		function name(a,   v, o, k) {
			v = hex(a)
			for (o = 0; o <= 12; o += 4) {
				k = sprintf("%x", v - o)
				if (k in nm) return o ? nm[k] "+" o : nm[k]
			}
			return a
		}
		'"$ev_bound_awk"'
		FILENAME == ARGV[1] {
			if ($1 ~ /^0x[0-9a-fA-F]+:$/ && NF >= 2) { a = $1; sub(/:$/, "", a); nm[sprintf("%x", hex(a))] = $2; named[$2] = 1; nnamed++ }
			next
		}
		{
			for (i = 1; i <= NF; i++) if ($i == "rpmh_send_msg:") break
			if (i > NF || i < 2) next
			ts = $(i - 1)
			sub(/:$/, "", ts)
			if (ev_after_sleep(ts)) { after++; next }
			st = ""
			if (match($0, /\[(sleep|wake|active)\]/)) st = substr($0, RSTART + 1, RLENGTH - 2)
			ad = da = ""
			for (j = i; j < NF; j++) { if ($j == "addr:") ad = $(j + 1); if ($j == "data:") da = $(j + 1) }
			if (ad == "" || da == "" || st == "") next
			n = name(ad)
			if (st == "active") { act[n] = da; nact++; next }
			if (!nf || ts - lt > 0.001) { nf++; ft[nf] = ts }
			lt = ts
			if (!((nf, n) in inf)) { inf[nf, n] = 1; no[nf]++; ord[nf, no[nf]] = n }
			if (st == "sleep") sl[nf, n] = da
			else wk[nf, n] = da
			cnt[nf]++
		}
		END {
			b = (bound == "") ? "in the whole trace (no sleep line in the kernel log)" : "before the sleep at " bound
			if (!nf) {
				printf "no flush %s (%d active votes in it): the RSC slept with the sets of an earlier flush\n", b, nact
				exit
			}
			s = ""
			for (k = 1; k <= no[nf]; k++) {
				n = ord[nf, k]
				v = ((nf, n) in sl) ? sl[nf, n] : "-"
				w = ((nf, n) in wk) ? wk[nf, n] : "-"
				if (short == "short" && n !~ /\.lvl$/ && !(hex(v) > 0 && int(hex(v) / 536870912) % 2)) continue
				s = s (s == "" ? "" : ", ") n " " v "/" w
			}
			h = ""
			nk = split(keys, kk, " ")
			for (k = 1; k <= nk; k++) {
				if (!(kk[k] in named) || ((nf, kk[k]) in inf)) continue
				h = h (h == "" ? "" : ", ") kk[k] ((kk[k] in act) ? " (active " act[kk[k]] ")" : "")
			}
			if (!nnamed) h = "not known (no command DB)"
			e = ev_at_entry(ft[nf]) ? ", the sleep\047s entry" : ""
			if (short == "short")
				printf "flush %d of %d at %s%s, sleep/wake: %s; held: %s\n", nf, nf, ft[nf], e, (s == "" ? "none" : s), (h == "" ? "none" : h)
			else
				printf "the last of %d flushes %s (at %s%s, %d commands), sleep/wake: %s; left out, so held at their vote through the sleep: %s; active votes before the sleep %d, commands after it %d\n", nf, b, ft[nf], e, cnt[nf], (s == "" ? "none" : s), (h == "" ? "none" : h), nact, after
		}' "$ramdir/cmd-db" "$1" 2>/dev/null
}

# The UFS PHY's power reference as ev_once's reads show it
# (docs/resume-recovery.md, "The PHY power reference"): with the disk frozen
# the UFS host is idle, its clock gating turns its clocks off 150 ms after
# its last command, and turns the PHY off with them, the PHY's supplies and
# its own clocks, unless the PHY holds one more power reference than the
# host's (a setup_clocks call that powered it on again while it was on,
# which ufs-qcom-track-phy-power-state-gts9u.patch ends).  One line when
# REGULATORS (a copy of regulator_summary) has the PHY's supply enabled and
# CLOCKS (an ev_clk_snap copy, read after it) has the host's core clock off
# with the PHY's own two clocks on (gcc_ufs_phy_phy_aux_clk and
# tcsr_ufs_clkref_en, its ref_aux and qref, which nothing but the PHY's
# power-on enables): this boot has a leaked reference, and on a kernel
# without that patch the resume from a sleep that powers ufs_mem_phy_gdsc
# off loses the UFS (2026-11-23, revision 10's run 5).  Nothing otherwise,
# and nothing when a read failed.
ev_phy_leak() { # REGULATORS CLOCKS
	awk -v phy="$ufs_phy_supply" '
		FILENAME == ARGV[1] { if ($1 == phy && $2 ~ /^[0-9]+$/) use = $2 + 0; next }
		$2 ~ /^[0-9]+$/ { c[$1] = $2 "/" $3; en[$1] = $2 + 0 }
		END {
			if (use < 1 || !("gcc_ufs_phy_axi_clk" in en) || en["gcc_ufs_phy_axi_clk"] != 0) exit
			if (en["gcc_ufs_phy_phy_aux_clk"] < 1 || en["tcsr_ufs_clkref_en"] < 1) exit
			printf "LEAKED POWER REFERENCE: its supply %s is enabled (use %d) with the UFS host clock-gated (gcc_ufs_phy_axi_clk %s) and its own clocks on (gcc_ufs_phy_phy_aux_clk %s, tcsr_ufs_clkref_en %s): this boot has a leaked UFS PHY power reference, and on a kernel without ufs-qcom-track-phy-power-state-gts9u.patch the resume from a sleep that powers ufs_mem_phy_gdsc off will lose the UFS (docs/resume-recovery.md, \"The PHY power reference\")\n", phy, use, c["gcc_ufs_phy_axi_clk"], c["gcc_ufs_phy_phy_aux_clk"], c["tcsr_ufs_clkref_en"]
		}' "$1" "$2" 2>/dev/null
}

# regulator_summary copied to OUT, in one read: for ev_once's lines of the
# rails in use, and for ev_phy_leak.
ev_reg() { # OUT
	awk '{ print }' "$reg_sum" 2>/dev/null > "$1"
}

# Once, before the first sleep: the domains, the rails, the bandwidth votes,
# the wakeup sources and the PCIe functions as they are with the disk frozen.
# A request on an interconnect node whose tag is 0 or has bit 2
# (QCOM_ICC_TAG_SLEEP, include/dt-bindings/interconnect/qcom,icc.h) votes in
# the sleep set as well (qcom_icc_aggregate), the one the CPUs' RSC sends
# when they sleep.
ev_once() {
	say "evidence: power domains (pm_genpd_summary; every line):"
	ev_genpd all | while IFS= read -r evl; do say "evidence:   $evl"; done
	say "evidence: rails in use (regulator_summary: the regulators with users, and those users):"
	ev_reg "$ramdir/ev-reg-once"
	awk 'NR <= 2 { next }
		NF >= 7 && $2 ~ /^[0-9]+$/ && $3 ~ /^[0-9]+$/ { show = $2 > 0; if (show) print; next }
		show && NF == 5 && $2 ~ /^[0-9]+$/ && $2 > 0 { print }' "$ramdir/ev-reg-once" 2>/dev/null |
		while IFS= read -r evl; do say "evidence:   $evl"; done
	say "evidence: bandwidth (interconnect_summary: the nodes with a vote and the requests that make it):"
	awk 'NR <= 2 { next }
		/^[^ ]/ { show = $2 + 0 > 0 || $3 + 0 > 0; if (show) print; next }
		show && /^  [^ ]/ && ($3 + 0 > 0 || $4 + 0 > 0) {
			t = $2 + 0
			print $0 ((t == 0 || int(t / 4) % 2 == 1) ? "   (sleep set too)" : "")
		}' "$icc_sum" 2>/dev/null |
		while IFS= read -r evl; do say "evidence:   $evl"; done
	say "evidence: wakeup sources with events so far (name, events, wakeups):"
	ev_wake "$ramdir/ev-wake-once"
	awk '$2 > 0 || $3 > 0' "$ramdir/ev-wake-once" 2>/dev/null |
		while IFS= read -r evl; do say "evidence:   $evl"; done
	for evd in "$pci_devs"/*; do
		[ -e "$evd" ] || continue
		say "evidence: pci ${evd##*/}: $(ev_val "$evd/power_state"), link $(ev_val "$evd/current_link_speed"), d3cold_allowed $(ev_val "$evd/d3cold_allowed"), wakeup $(ev_val "$evd/power/wakeup"), runtime $(ev_val "$evd/power/runtime_status")"
	done
	say "evidence: state: $(ev_state)"
	# What keeps XO voted in the sleep set (ev_xo_holders), and every clock
	# that is prepared now, with its parent.
	ev_clk_snap "$ramdir/ev-clk-once"
	say "evidence: XO ($xo_clk) voted in the sleep set by these prepared clocks below it (enable/prepare): $(ev_xo_holders "$ramdir/ev-clk-once")"
	say "evidence: prepared clocks (name, enable, prepare, parent):"
	awk '$3 ~ /^[0-9]+$/ && $3 > 0' "$ramdir/ev-clk-once" 2>/dev/null |
		while IFS= read -r evl; do say "evidence:   $evl"; done
	# A leaked UFS PHY power reference, from the two reads above: in pmsg
	# as well, for a run that loses the UFS.
	evleak=$(ev_phy_leak "$ramdir/ev-reg-once" "$ramdir/ev-clk-once")
	if [ -n "$evleak" ]; then
		say "evidence: UFS PHY: $evleak"
		ev_pmsg "evidence: UFS PHY: leaked power reference before the first sleep (its supply on, the host clock-gated, its own clocks on): a kernel without ufs-qcom-track-phy-power-state-gts9u.patch loses the UFS at the resume"
	fi
	# The SoC's sleep states and each subsystem's while the CPUs are awake,
	# for 5 s: an audio DSP that goes in and out of its sleep as often awake
	# as in the run's sleeps does so on its own, whatever the CPUs do.
	ev_stats "$ramdir/ev-awake.0"
	sleep 5
	ev_stats "$ramdir/ev-awake.1"
	say "evidence: awake, 5 s before the first sleep: $(ev_stats_delta "$ramdir/ev-awake.0" "$ramdir/ev-awake.1")"
}

# mode=s2idle: each CPU's cpuidle states that have an s2idle entry, as "CPU
# STATE USAGE TIME NAME" lines (s2idle/usage, s2idle/time in us; the
# counts enter_s2idle_proper() keeps, drivers/cpuidle/cpuidle.c).
ev_cpuidle() { # OUT
	for evc in "$cpu_devs"/cpu[0-9]*; do
		for evd in "$evc"/cpuidle/state[0-9]*; do
			[ -d "$evd/s2idle" ] || continue
			printf '%s %s %s %s %s\n' "${evc##*/}" "${evd##*/}" "$(ev_val "$evd/s2idle/usage")" \
				"$(ev_val "$evd/s2idle/time")" "$(ev_val "$evd/name")"
		done
	done 2>/dev/null > "$1"
}

# What changed between two ev_cpuidle snapshots, one line: "ENTRIES CPUS
# OF TIME STATE NAMES...": the s2idle entries of every CPU, how many CPUs
# entered (of how many), the seconds counted in them, the deepest state
# entered and its names; "-" when the snapshots cannot be compared.
ev_cpuidle_delta() { # BEFORE AFTER
	awk '
		FILENAME == ARGV[1] { u[$1 " " $2] = $3; t[$1 " " $2] = $4; next }
		{
			k = $1 " " $2
			if (!(k in u) || $3 !~ /^[0-9]+$/ || $4 !~ /^[0-9]+$/ || u[k] !~ /^[0-9]+$/ || t[k] !~ /^[0-9]+$/) { bad = 1; next }
			if (!($1 in cpu)) { cpu[$1] = 1; nc++ }
			du = $3 - u[k]
			if (du <= 0) next
			n += du
			tt += $4 - t[k]
			if (!($1 in went)) { went[$1] = 1; ni++ }
			s = substr($2, 6) + 0
			if (st == "" || s > sn) { sn = s; st = $2; names = $5 }
			else if (s == sn && index(" " names " ", " " $5 " ") == 0) names = names " " $5
		}
		END {
			if (bad || !nc) { print "-"; exit }
			printf "%d %d %d %.3f %s %s\n", n, ni, nc, tt / 1000000, st == "" ? "-" : st, st == "" ? "-" : names
		}' "$1" "$2" 2>/dev/null
}

# mode=s2idle: the idle states of the CPU cluster's power domain
# (idle_states_show(), drivers/pmdomain/core.c: "State Time(ms) Usage
# Rejected Above Below S2idle"), as "STATE USAGE REJECTED S2IDLE" lines.
ev_cluster() { # OUT
	awk 'NR > 1 && NF >= 7 && $3 ~ /^[0-9]+$/ { print $1, $3, $4, $7 }' "$cluster_states" 2>/dev/null > "$1"
}

# What changed between two ev_cluster snapshots: "S0 +0 (s2idle +0,
# rejected +0), S1 +1 (s2idle +1, rejected +0)", or "-".
ev_cluster_delta() { # BEFORE AFTER
	awk '
		FILENAME == ARGV[1] { u[$1] = $2; r[$1] = $3; s[$1] = $4; next }
		($1 in u) { o = o (o == "" ? "" : ", ") sprintf("%s +%d (s2idle +%d, rejected +%d)", $1, $2 - u[$1], $4 - s[$1], $3 - r[$1]) }
		END { print o == "" ? "-" : o }' "$1" "$2" 2>/dev/null
}

# mode=s2idle, once the cycle's kernel lines are read: how far the CPUs got
# into suspend-to-idle.  Sets s2u (the s2idle entries of every CPU, or ?),
# s2v (a short account for the cycle's verdict) and s2pmsg (pmsg's line).
# The time asleep is $asleep, the sum of the "Timekeeping suspended for"
# lines (one each time every CPU was frozen at once); s2idle/time leaves
# that time out (tick_freeze() suspends sched_clock, which the cpuidle core
# measures with) and is not the sleep.
ev_s2idle() { # SFX
	s2cl=$(ev_cluster_delta "$ramdir/ev-cluster-$1.0" "$ramdir/ev-cluster-$1.1")
	[ -n "$s2cl" ] && [ "$s2cl" != - ] || s2cl='not readable'
	s2d=$(ev_cpuidle_delta "$ramdir/ev-cpuidle-$1.0" "$ramdir/ev-cpuidle-$1.1")
	set -- ${s2d:--}
	s2t=
	if [ "$1" = - ]; then
		s2u='?'
		s2e='s2idle entries not readable'
	else
		s2u=$1 s2t=$4
		s2e="s2idle entries +$1 on $2 of $3 CPUs"
		if [ "$1" != 0 ]; then
			s2st=$5
			shift 5
			s2e="$s2e in $s2st ($*)"
		fi
	fi
	if [ -n "$asleep" ]; then
		s2a="asleep ${asleep}s ($tk_n Timekeeping lines)"
	else
		s2a='never asleep (no Timekeeping line)'
	fi
	s2w='no wakeup IRQ recorded'
	if [ -n "$wake" ]; then
		s2irq=$(irq_name "$wake")
		s2w="woken by IRQ $wake${s2irq:+ ($s2irq)}"
	fi
	s2took=$(awk -v a="$up0" -v b="$up1" 'BEGIN { if (a ~ /^[0-9.]+$/ && b ~ /^[0-9.]+$/) printf "%.1f", b - a; else printf "?" }')
	s2hw=$(stat_of last_hw_sleep)
	say "$cyc: evidence: s2idle: $s2e${s2t:+, ${s2t}s counted in them (not the time every CPU was frozen)}; cluster domain: $s2cl; $s2a; $s2w; rtcwake took ${s2took}s; suspend_stats last_hw_sleep $([ "$s2hw" = - ] && echo 'not offered by this kernel' || echo "${s2hw}us")"
	s2v="$s2a, $s2e, cluster $s2cl"
	s2pmsg="$s2e; cluster $s2cl; $s2a; $s2w"
}

# Before a sleep: the snapshots ev_after compares with, and the domains.
ev_before() { # SFX
	ev_clk_snap "$ramdir/ev-clk-$1.0"
	ev_stats "$ramdir/ev-stats-$1.0"
	ev_irqs "$ramdir/ev-irqs-$1.0"
	ev_wake "$ramdir/ev-wake-$1.0"
	ev_genpd_offs "$ramdir/ev-gpd-$1.0"
	if [ "$sleep_mode" = s2idle ]; then
		ev_cpuidle "$ramdir/ev-cpuidle-$1.0"
		ev_cluster "$ramdir/ev-cluster-$1.0"
	fi
	evstate=$(ev_state)
	say "$cyc: evidence: before: $evstate"
	say "$cyc: evidence: before: clocks (enable/prepare): $(ev_clk_line "$ramdir/ev-clk-$1.0" full)"
	say "$cyc: evidence: before: XO held by: $(ev_xo_holders "$ramdir/ev-clk-$1.0")"
	say "$cyc: evidence: before: $(ev_pci_state)"
	ev_genpd domains | while IFS= read -r evl; do say "$cyc: evidence: before: genpd: $evl"; done
}

# Right after rtcwake returns: the same snapshots, the clocks' last.
ev_resumed() { # SFX
	ev_stats "$ramdir/ev-stats-$1.1"
	ev_irqs "$ramdir/ev-irqs-$1.1"
	ev_wake "$ramdir/ev-wake-$1.1"
	ev_genpd_offs "$ramdir/ev-gpd-$1.1"
	if [ "$sleep_mode" = s2idle ]; then
		ev_cpuidle "$ramdir/ev-cpuidle-$1.1"
		ev_cluster "$ramdir/ev-cluster-$1.1"
	fi
	ev_clk_snap "$ramdir/ev-clk-$1.1"
}

# Once the cycle's kernel lines are in K: what the sleep reached, what woke
# or kept it, whether the PCIe link went down and came back, the Wi-Fi's
# state, what genpd powered off, the DSPs' interrupts, what held XO, the
# domains now, and the cycle's kernel lines (and with mode=s2idle,
# ev_s2idle; with rpmh, the sleep set, ev_rpmh).  ev_keep then puts five
# short lines of it in pmsg (one more with mode=s2idle, one more with
# rpmh), for an attempt that was not given up before the sleep.
ev_after() { # SFX K
	evdepth=$(ev_stats_delta "$ramdir/ev-stats-$1.0" "$ramdir/ev-stats-$1.1")
	evwake=$(ev_wake_delta "$ramdir/ev-wake-$1.0" "$ramdir/ev-wake-$1.1")
	evirq=$(ev_irq_delta "$ramdir/ev-irqs-$1.0" "$ramdir/ev-irqs-$1.1" 12)
	# A PCIe host that went to L2 at suspend logs its link coming up again
	# at resume (dw_pcie_wait_for_link); one that kept its link logs nothing
	# and kept its clocks, PHY and votes through the sleep
	# (dw_pcie_suspend_noirq, qcom_pcie_suspend_noirq).
	evlink=$(awk '/PCIe Gen\.[0-9]+ x[0-9]+ link up|Timeout waiting for L2 entry|Link failed to come up|Device not found|Device found, but not active/ {
		sub(/^\[[^]]*\] /, ""); s = s (s == "" ? "" : "; ") $0 } END { print s }' "$2" 2>/dev/null)
	if [ -z "$evlink" ]; then
		evlink='no link-up line: the PCIe host kept its link, clocks and votes through the sleep'
		evpcie='pcie link kept'
	else
		case $evlink in
		*' link up'*) evpcie='pcie link down and up again' ;;
		*) evpcie="pcie: $evlink" ;;
		esac
	fi
	# Revision 11's guard: what genpd did with the PCIe host's power domain
	# in this sleep, as the host logged it at the resume.
	# (Only grep and awk: this runs from the RAM root.)
	evguard=$(grep -E "$guard_re" "$2" 2>/dev/null | awk '{sub(/.*\.pcie: system sleep: /, ""); print; exit}')
	if [ -n "$evguard" ]; then
		evlink="$evlink; the host logged: $evguard"
		evpcie="$evpcie ($evguard)"
	fi
	# Did the PCIe host come back at all: a line of pcie_down_re (its link
	# wait, its init or its noirq resume failed), or none.
	evback=$(grep -E "$pcie_down_re" "$2" 2>/dev/null | awk '{sub(/^\[[^]]*\] /, ""); print; exit}')
	if [ -n "$evback" ]; then
		evback="PCIe did not come back ($evback)"
	else
		evback='PCIe came back (no host failure line)'
	fi
	evclk0=$(ev_clk_line "$ramdir/ev-clk-$1.0" short)
	evclk1=$(ev_clk_line "$ramdir/ev-clk-$1.1" short)
	# The Wi-Fi with the PCI devices kept: its function's state now, and the
	# first line of the cycle's that says it did not come back
	# (wifi_fail_re), or none.
	evwifi=$(grep -E "$wifi_fail_re" "$2" 2>/dev/null | awk '{sub(/^\[[^]]*\] /, ""); print; exit}')
	evfw=$(grep -c 'ath11k_pci .*fw_version' "$2" 2>/dev/null)
	evwifi="$(ev_pci_state); ${evwifi:-no Wi-Fi failure line}; ath11k firmware loads ${evfw:-0}"
	evgpd=$(ev_genpd_offs_delta "$ramdir/ev-gpd-$1.0" "$ramdir/ev-gpd-$1.1" full)
	evdsp=$(ev_irq_dsp_delta "$ramdir/ev-irqs-$1.0" "$ramdir/ev-irqs-$1.1")
	evxo1=$(ev_xo_holders "$ramdir/ev-clk-$1.1")
	say "$cyc: evidence: qcom_stats: $evdepth"
	say "$cyc: evidence: wakeup sources: $evwake"
	say "$cyc: evidence: interrupts: $evirq"
	say "$cyc: evidence: dsp interrupts: $evdsp"
	say "$cyc: evidence: pcie: $evlink; $evback"
	say "$cyc: evidence: wifi: $evwifi"
	say "$cyc: evidence: genpd powered off in the sleep (idle states usage of each domain): $evgpd"
	say "$cyc: evidence: after: XO held by: $evxo1"
	if [ "$rpmh" = 1 ]; then
		ev_rpmh_snap "$ramdir/ev-rpmh-$1"
		evrpmhf=$(ev_rpmh "$ramdir/ev-rpmh-$1" "$2" full)
		say "$cyc: evidence: rpmh sleep set: $evrpmhf"
		evrpmh=$(ev_rpmh "$ramdir/ev-rpmh-$1" "$2" short)
		say "$cyc: evidence: XO at the entry to the sleep (clock trace): $(ev_xo_entry "$ramdir/ev-clk-$1.0" "$ramdir/ev-rpmh-$1" "$2" full)"
		evxoe=$(ev_xo_entry "$ramdir/ev-clk-$1.0" "$ramdir/ev-rpmh-$1" "$2" short)
	fi
	say "$cyc: evidence: after: clocks (enable/prepare): $(ev_clk_line "$ramdir/ev-clk-$1.1" full)"
	ev_genpd domains | while IFS= read -r evl; do say "$cyc: evidence: after: genpd: $evl"; done
	awk -v re="$ev_kernel" -v skip="$ev_kernel_skip" -v max="$ev_klines" '
		$0 ~ skip { next }
		$0 ~ re { if (++n <= max) print; next }
		END { if (n > max) printf "(%d more kernel lines of this cycle left out; its kmsg file in the run directory has them all)\n", n - max }' "$2" 2>/dev/null |
		while IFS= read -r evl; do say "$cyc: evidence: kernel: $evl"; done
	[ "$sleep_mode" != s2idle ] || ev_s2idle "$1"
}

ev_keep() { # SFX
	ev_pmsg "$cyc: evidence: qcom_stats: $evdepth"
	ev_pmsg "$cyc: evidence: $evpcie; $evback; irq $(ev_irq_delta "$ramdir/ev-irqs-$1.0" "$ramdir/ev-irqs-$1.1" 3); wakeup $evwake; before: $evstate"
	ev_pmsg "$cyc: evidence: clocks before: $evclk0; after: $evclk1"
	ev_pmsg "$cyc: evidence: wifi: $evwifi; genpd off: $(ev_genpd_offs_delta "$ramdir/ev-gpd-$1.0" "$ramdir/ev-gpd-$1.1" short); dsp irq $evdsp"
	ev_pmsg "$cyc: evidence: XO held by: $evxo1"
	if [ "$rpmh" = 1 ]; then
		ev_pmsg "$cyc: evidence: rpmh: $evrpmh"
		ev_pmsg "$cyc: evidence: XO at entry: $evxoe; $(roles)"
		# The whole flush as well, for a run whose terminal went away (one
		# over Wi-Fi, which the sleep takes down): in pieces, at most eight.
		ev_pmsg_pieces "$cyc: evidence: rpmh sleep set" "$evrpmhf" 8
	fi
	[ "$sleep_mode" != s2idle ] || ev_pmsg "$cyc: evidence: s2idle: $s2pmsg"
}

# TEXT to pmsg in pieces of 240 characters, "PREFIX (I/N): ...", at most MAX
# of them, the last saying how much was left out.
ev_pmsg_pieces() { # PREFIX TEXT MAX
	[ -e /dev/pmsg0 ] || return 0
	printf '%s\n' "$2" | awk -v p="$1" -v max="$3" '
		{ t = t $0 }
		END {
			n = int((length(t) + 239) / 240)
			for (i = 1; i <= n && i <= max; i++) {
				s = substr(t, (i - 1) * 240 + 1, 240)
				if (i == max && n > max) s = substr(s, 1, 200) " (" length(t) - (max - 1) * 240 - 200 " more on the terminal)"
				printf "%s (%d/%d): %s\n", p, i, n, s
			}
		}' 2>/dev/null | while IFS= read -r evl; do ev_pmsg "$evl"; done
}

# The failure lines of a kernel log excerpt: the UFS ones, the block layer's
# warning of a write to the read-only disk (once per device, and the write
# still goes out), I/O and ext4 errors on the UFS disk, and a journal
# started on a frozen ext4.
fail_re() {
	echo "$ufs_fail|I/O error, dev $disk,|Buffer I/O error on dev(ice)? $disk[0-9]|EXT4-fs error \\(device $disk[0-9]|Aborting journal on device $disk[0-9]|$journal_warn"
}

# The lines of an excerpt about the UFS or its disk: "ufs" not inside a word
# (its host, its PHY, its well-known LUNs: not "bufs"), the userdata disk
# ("[sda]"), or a SCSI device of the UFS host ("sd 0:0:0:0: ...", "scsi
# 0:0:0:1: ...": the PM core's "failed to suspend" names a disk that way).
# The module list of a kernel warning is not about anything.
storage_lines() { # FILE
	grep -v 'Modules linked in:' "$1" |
		grep -iE "(^|[^a-zA-Z0-9])ufs|\\[$disk\\]|(^|\\] )(sd|scsi) $ufs_host:[0-9]+:[0-9]+:[0-9]+: "
}

# Did a calibration in DIAG (a cycle's diagnostic lines) start without the
# UFS PHY's power-on?  Revision 10's second diagnostic logs its power-up
# write line in that power-on, right before each calibration entry, and a
# kernel that has it logged one this boot already, at the UFS probe
# (pwrdn_seen): an entry with no such line since the entry before it had no
# power-on.  Revision 11's line at the entry of a power-up sequence with
# power_count 2 or more says the same: the code before
# ufs-qcom-track-phy-power-state-gts9u.patch powers the PHY off and on again
# there only from 1.  Either is a leaked PHY power reference
# (docs/resume-recovery.md, "The PHY power reference").
phy_leak_seen() { # DIAG
	awk -v pw="$pwrdn_seen" '
		/power-up sequence entry: PHY power_count / {
			for (i = 1; i < NF; i++) if ($i == "power_count" && $(i + 1) ~ /^[0-9]+$/ && $(i + 1) + 0 >= 2) leak = 1
		}
		/power-up write: PWRDN/ { w = 1 }
		/calibration entry: SW_RESET/ { if (pw == 1 && !w) leak = 1; w = 0 }
		END { exit !leak }' "$1" 2>/dev/null
}

# Was this attempt a suspend that the kernel gave up before the tablet
# slept, with the storage as it was before?  Only if every one of these
# holds; anything else, or anything that cannot be read, leaves it a FAIL:
#   1. rtcwake failed (its write to /sys/power/state did);
#   2. suspend_stats: success +0 and fail +1, the kernel's own word that
#      this suspend failed (a sleep that happened counts as a success,
#      however badly it resumed);
#   3. the attempt's kernel lines have "PM: suspend entry (deep)" (mode=
#      s2idle: "(s2idle)") and "PM: suspend exit";
#   4. and a line that shows the suspend given up on the way down
#      (abort_re: a pending wakeup, a wakeup IRQ, a device that refused);
#   5. and less than 1 s of "Timekeeping suspended for" (a real sleep of
#      SECONDS logs about that);
#   6. no failure line (fail_re: the UFS and its PHY, I/O and ext4 errors,
#      a write the block layer logged), and no line of the diagnostic but
#      its calibration entries, revision 10's power-up write lines and
#      revision 11's PHY counts at a power-up sequence's entry and
#      setup_clocks lines (the others come with a timeout or a failed
#      power-up sequence);
#   7. no line about the UFS or its disk that says something failed
#      (storage_lines, storage_bad_re);
#   8. every filesystem still frozen (still_frozen);
#   9. the read after it matches the first one.
aborted_attempt() {
	gave_up_early || return 1
	grep -qE "$abort_re" "$k"
}

# What an ABORTED attempt and a NO-SLEEP (freezer) one have in common: 1-3
# and 5-9 above.
gave_up_early() {
	[ "$rtc_rc" != 0 ] || return 1
	[ "$(delta "$ok0" "$ok1")" = +0 ] && [ "$(delta "$fail0" "$fail1")" = +1 ] || return 1
	grep -q "PM: suspend entry ($mem_label)" "$k" && grep -q 'PM: suspend exit' "$k" || return 1
	if [ -n "$asleep" ]; then
		awk -v s="$asleep" 'BEGIN { exit !(s + 0 < 1) }' || return 1
	fi
	[ -z "$prot_why" ] || return 1
	[ "$storage" = healthy ] && [ "${fails:-0}" = 0 ] || return 1
	! grep -v -e 'calibration entry: SW_RESET' -e 'power-up write: PWRDN' \
		-e 'power-up sequence entry: PHY power_count' -e 'setup_clocks repeated a PHY power-' \
		"$ramdir/diag-$sfx" | grep -q . ||
		return 1
	! storage_lines "$k" | grep -qiE "$storage_bad_re" || return 1
	[ -n "$read_hash" ] && [ "$read_hash" = "$base_hash" ]
}

# Was this attempt a suspend whose task freezer gave up (freezer_re), with
# the storage as it was before?  The tasks waiting on a frozen filesystem
# sleep freezable (__sb_start_write: TASK_UNINTERRUPTIBLE|TASK_FREEZABLE,
# kernel/locking/percpu-rwsem.c), but a task queued behind one of them on a
# memory map's lock (a write fault on a shared mapping of the root holds the
# VMA's lock while it waits: ext4_page_mkwrite -> sb_start_pagefault) waits
# killable or uninterruptible and not freezable, and the freezer cannot take
# it (__set_task_frozen, kernel/freezer.c).  Not a FAIL, and not tried
# again: it would meet the same task.
freezer_failed() {
	gave_up_early || return 1
	grep -qE "$freezer_re" "$k"
}

# mode=s2idle: was this sleep, which the kernel counted (rtcwake 0, nothing
# against the cycle), cut short by a wakeup that was not the RTC's alarm?
# Cut short: no CPU entered its s2idle state (s2idle_loop() also ends at
# once on a wakeup that came after the noirq phase, and the suspend still
# counts as a success: kernel/power/suspend.c), or less than half of
# SECONDS asleep (the "Timekeeping suspended for" lines).  And a wakeup on
# record that is not the RTC's: an IRQ ($wake: pm_wakeup_irq, else the
# kernel's "Triggering wakeup from IRQ N") whose name in /proc/interrupts
# has no "rtc" (the alarm's is pm8xxx_rtc_alarm), or a pending wakeup
# source.  Without such a record the cycle is not WOKEN EARLY: it is judged
# as a sleep (and FAILs if no CPU entered its s2idle state).
woken_early() {
	wcut=
	[ "$s2u" != 0 ] || wcut=1
	awk -v s="${asleep:-0}" -v n="$seconds" 'BEGIN { exit !(s + 0 < n / 2) }' && wcut=1
	[ -n "$wcut" ] || return 1
	if [ -n "$wake" ]; then
		case $(irq_name "$wake") in
		*rtc*) return 1 ;;
		esac
		return 0
	fi
	grep -qE 'Wakeup pending|active wakeup source: ' "$k"
}

# The tasks the kernel named after a failed freeze, in the lines between its
# "Freezing ... failed after" and "Restarting tasks" ("task:%-15.15s
# state:%c ...", sched_show_task): "NAME (state D), ...", each once, the
# first 8.  A hung-task report elsewhere in the excerpt names tasks the same
# way, and is left out.
stuck_tasks() { # FILE
	awk -v re="$freezer_re" '
		$0 ~ re { f = 1; next }
		/Restarting tasks|PM: suspend exit/ { f = 0 }
		f && /task:.* state:[A-Za-z]/ {
			s = $0
			sub(/.*task:/, "", s)
			p = index(s, " state:")
			n = substr(s, 1, p - 1)
			st = substr(s, p + 7, 1)
			sub(/ +$/, "", n)
			if (n == "" || (n in seen)) next
			seen[n] = 1
			if (++c <= 8) printf "%s%s (state %s)", c == 1 ? "" : ", ", n, st
		}
		END { if (c > 8) printf ", and %d more", c - 8 }' "$1"
}

# The action name(s) of IRQ N in /proc/interrupts: what follows its trigger
# type (" 199: <counts> msmgpio 25 Level fts1ba90a"), and a flow handler's
# "-name" after that type, if any, left out.  Nothing when the IRQ is not
# there or has no action.
irq_name() { # N
	awk -v n="$1:" '$1 == n {
		for (f = 2; f < NF; f++) if ($f ~ /^(Level|Edge)$/) break
		if (f < NF && $(f + 1) ~ /^-/) f++
		s = ""
		for (f++; f <= NF; f++) s = s (s == "" ? "" : " ") $f
		print s
		exit
	}' /proc/interrupts 2>/dev/null
}

# What gave up an aborted attempt: $abort_why for its verdict, $abort_by for
# the run's (shorter).  The wakeup IRQ, by its line in this attempt's kernel
# lines, else /sys/power/pm_wakeup_irq (cleared at each suspend's start); a
# wakeup source; a device that refused to suspend; else the kernel's first
# line of abort_re.
abort_cause() {
	abort_why= abort_by=
	airq=$(awk '/Triggering wakeup from IRQ [1-9]/ {print $NF; exit}' "$k")
	if [ -z "$airq" ] && num "$wake" && [ "$wake" != 0 ]; then
		airq=$wake
	fi
	if [ -n "$airq" ]; then
		aname=$(irq_name "$airq")
		case $aname in
		'')
			abort_why="a wakeup from IRQ $airq (no name for it in /proc/interrupts)"
			abort_by="IRQ $airq" ;;
		*"$touch_irq"*)
			abort_why="a wakeup from IRQ $airq ($aname, the touchscreen: do not touch the screen during the run)"
			abort_by="IRQ $airq ($aname, the touchscreen)" ;;
		*)
			abort_why="a wakeup from IRQ $airq ($aname)"
			abort_by="IRQ $airq ($aname)" ;;
		esac
	fi
	asrc=$(awk '/active wakeup source: / {sub(/.*active wakeup source: /, ""); print; exit}' "$k")
	if [ -n "$asrc" ]; then
		abort_why="${abort_why:+$abort_why; }a pending wakeup (wakeup source $asrc)"
		abort_by="${abort_by:+$abort_by and }wakeup source $asrc"
	fi
	adev=$(awk '/: failed to suspend[a-z ]*: error -[0-9]/ {
		sub(/^\[[^]]*\] /, "")
		p = index($0, ": failed to suspend")
		rest = substr($0, p + 19)
		e = index(rest, ": error ")
		how = substr(rest, 1, e - 1)
		sub(/^ /, "", how)
		printf "%s refused to suspend (%serror %s)\n", substr($0, 1, p - 1), how == "" ? "" : how ", ", substr(rest, e + 8)
		exit
	}' "$k")
	if [ -n "$adev" ]; then
		abort_why="${abort_why:+$abort_why; }$adev"
		abort_by="${abort_by:+$abort_by and }$adev"
	fi
	if [ -z "$abort_why" ]; then
		aline=$(grep -E "$abort_re" "$k" | awk '{sub(/^\[[^]]*\] /, ""); print; exit}')
		abort_why="the kernel logged: $aline"
		abort_by="\"$aline\""
	fi
}

# The aborted attempts so far, by what gave them up, or nothing:
# "6 attempts aborted before the sleep (IRQ 199 (fts1ba90a, the touchscreen)
# 5 times, IRQ 37 (pwrkey) once)".  A WOKEN EARLY attempt (mode=s2idle)
# counts the same and is noted "woken early by ...".
abort_note() {
	[ "$aborted" != 0 ] || return 0
	awk -v n="$aborted" '{ if (!($0 in c)) o[++k] = $0; c[$0]++; if (index($0, "woken early by ") == 1) e = 1 }
	END {
		printf "%d attempt%s %s (", n, n == 1 ? "" : "s", e ? "aborted before the sleep or woken early" : "aborted before the sleep"
		for (j = 1; j <= k; j++)
			printf "%s%s %s", j == 1 ? "" : ", ", o[j], c[o[j]] == 1 ? "once" : c[o[j]] " times"
		printf ")\n"
	}' "$ramdir/aborts"
}

# One O_DIRECT read of the stretch, hashed: $read_hash, or $read_why.  It
# runs in the background: a storage that is gone may never answer, and the
# run must end all the same.
read_check() { # NAME
	rp=$ramdir/read-$1
	read_hash= read_why= rrc= re= rh=
	: > "$rp.rc"
	( dd if="$part" iflag=direct bs=$read_bs count=$read_count status=none 2> "$rp.err"
	  echo $? > "$rp.rc" ) | sha256sum > "$rp.sha" &
	rpid=$!
	rwait=0
	while kill -0 "$rpid" 2>/dev/null; do
		if [ "$rwait" -ge "$read_deadline" ]; then
			read_why="the read did not finish within ${read_deadline}s"
			return 0
		fi
		sleep 1
		rwait=$((rwait + 1))
	done
	wait "$rpid" 2>/dev/null
	read -r rrc < "$rp.rc" 2>/dev/null
	if [ "$rrc" != 0 ]; then
		read -r re < "$rp.err" 2>/dev/null
		read_why="dd ended with ${rrc:-no status}${re:+: $re}"
		return 0
	fi
	read -r rh _ < "$rp.sha" 2>/dev/null
	case $rh in
	????????????????????????????????????????????????????????????????) read_hash=$rh ;;
	*) read_why='no hash of the read' ;;
	esac
}

# Lines in reverse order (the last first).
reversed() { # FILE
	awk '{ l[NR] = $0 } END { for (n = NR; n > 0; n--) print l[n] }' "$1"
}

# Did this run freeze the filesystem on SOURCE?  "frozen" has one line per
# filesystem this run froze, SOURCE MOUNTPOINT TYPE, in the order frozen.
is_frozen() { # SOURCE
	awk -v s="$1" '$1 == s { f = 1 } END { exit !f }' "$ramdir/frozen"
}

# Does the run watch ext4's state of the filesystem on SOURCE?
# "errors-before" has one line per filesystem it froze or is about to:
# SOURCE MOUNTPOINT COUNT NAME, COUNT being ext4's error count when the run
# began (or when it froze one mounted since).
is_watched() { # SOURCE
	awk -v s="$1" '$1 == s { f = 1 } END { exit !f }' "$ramdir/errors-before"
}

# The writable disk filesystems this run has not frozen: " MOUNTPOINT(SOURCE)"
# each.
unfrozen() {
	rw_disk_mounts | while read -r usrc ump _; do
		is_frozen "$usrc" || printf ' %s(%s)' "$ump" "$usrc"
	done
}

# Freeze every writable disk filesystem of the system that this run has not
# frozen yet: FIFREEZE (fsfreeze -f) on its mount point below the system's
# root, in freeze_order, each source once (a bind mount shares its
# superblock: a second freeze would fail with EBUSY).  One the run does not
# watch yet (mounted since it began) goes to errors-before first, with its
# error count now; it has no errors=continue.  On a failure: 1, and
# $froze_why; what was frozen stays so (thaw_all).  A freeze that has not
# ended after freeze_deadline seconds sets freeze_hung: it cannot be undone
# (thaw_super waits for it), only restarted from.
freeze_all() {
	froze_why=
	freeze_order "$rootsrc" > "$ramdir/to-freeze"
	while read -r fsrc fmp ftype; do
		is_frozen "$fsrc" && continue
		case $freeze_types in
		*" $ftype "*) ;;
		*) froze_why="$fmp ($fsrc) is $ftype, which this run does not freeze"; return 1 ;;
		esac
		# /proc/1/mounts escapes a space and the like with a backslash.
		case $fmp in
		*\\*) froze_why="cannot name the mount point $fmp"; return 1 ;;
		esac
		if ! is_watched "$fsrc"; then
			fname=$(dev_name "$fsrc")
			printf '%s %s %s %s\n' "$fsrc" "$fmp" "$(ext4_errors "$fname")" "$fname" >> "$ramdir/errors-before"
			say "$fmp ($fsrc) was mounted after the run began: it is frozen without errors=continue"
		fi
		deadline "$freeze_deadline" fsfreeze -f "$sysroot$fmp" > "$ramdir/fsfreeze.out" 2>&1
		frc=$?
		if [ "$frc" = 0 ]; then
			printf '%s %s %s\n' "$fsrc" "$fmp" "$ftype" >> "$ramdir/frozen"
			say "frozen: $fmp ($fsrc, $ftype)"
		elif [ "$frc" = 124 ]; then
			freeze_hung=1
			froze_why="fsfreeze -f $fmp ($fsrc) had not ended after ${freeze_deadline}s"
			return 1
		else
			fo=
			read -r fo < "$ramdir/fsfreeze.out"
			froze_why="fsfreeze -f $fmp ($fsrc) failed${fo:+: $fo}"
			return 1
		fi
	done < "$ramdir/to-freeze"
}

# Thaw what this run froze, the last frozen first: 1 if a thaw failed
# ($thaw_why).  "frozen" keeps those still frozen, in the order frozen.
thaw_all() {
	thaw_why= still=
	reversed "$ramdir/frozen" > "$ramdir/to-thaw"
	while read -r tsrc tmp ttype; do
		if deadline "$freeze_deadline" fsfreeze -u "$sysroot$tmp" > "$ramdir/fsfreeze.out" 2>&1; then
			say "thawed: $tmp ($tsrc)"
		else
			to=
			read -r to < "$ramdir/fsfreeze.out"
			thaw_why="$thaw_why; fsfreeze -u $tmp ($tsrc) failed${to:+: $to}"
			still="$tsrc $tmp $ttype
$still"
		fi
	done < "$ramdir/to-thaw"
	printf '%s' "$still" > "$ramdir/frozen"
	thaw_why=${thaw_why#; }
	[ -z "$thaw_why" ]
}

# After a sleep, or an attempt at one: is every filesystem this run froze
# still frozen?  A second FIFREEZE of a filesystem frozen from userspace
# fails with EBUSY (freeze_super -> may_freeze, fs/super.c), and that is
# all it does.  One that succeeds froze a filesystem that something had
# thawed (it stays frozen from then on); one that does not end waits on a
# filesystem that had been thawed, and on its disk.  $prot_why says which,
# if any: 1.
still_frozen() {
	prot_why=
	while read -r ssrc smp _; do
		deadline "$freeze_deadline" fsfreeze -f "$sysroot$smp" > "$ramdir/refreeze.out" 2>&1
		sfrc=$?
		so=
		read -r so < "$ramdir/refreeze.out"
		case $sfrc in
		0) prot_why="$prot_why; $smp ($ssrc) had been thawed: a second fsfreeze -f succeeded (frozen again now), so it was writable after the resume" ;;
		124)
			freeze_hung=1
			prot_why="$prot_why; $smp ($ssrc) had been thawed: a second fsfreeze -f had not ended after ${freeze_deadline}s" ;;
		*)
			case $so in
			*'Device or resource busy'*) ;;
			*) prot_why="$prot_why; cannot tell whether $smp ($ssrc) is still frozen: fsfreeze -f ended with $sfrc${so:+: $so}" ;;
			esac ;;
		esac
	done < "$ramdir/frozen"
	prot_why=${prot_why#; }
	[ -z "$prot_why" ]
}

# ext4's state of every filesystem the run watches (errors-before): has ext4
# recorded an error on it, logged one, or given up on it since the run
# began?  The signs:
#   - its error count rose, or cannot be read any more: an error saved while
#     its disk was writable (save_error_info -> ext4_update_super; on a
#     read-only disk nothing raises it);
#   - its options in /proc/1/mounts show emergency_ro or shutdown
#     (_ext4_show_options): ext4 gave up on it;
#   - the kernel log since the run began has "EXT4-fs error (device NAME)"
#     (__ext4_error and the like), "Aborting journal on device NAME-" (jbd2:
#     it writes the error into the journal's superblock), "EXT4-fs (NAME):
#     Remounting filesystem read-only" (ext4_handle_error), or "JBD2: I/O
#     error when updating journal superblock for NAME-" (that write failed).
# A filesystem that is no longer mounted (a stick pulled) is left out.
# Sets ext4_bad ("MOUNTPOINT (SOURCE): what; ..."), ext4_gaveup (1 when a
# journal aborted, ext4 gave up, or an error was saved: everything but
# error lines alone on a filesystem of the read-only UFS disk, which with
# errors=continue save and abort nothing), ext4_root (1 when the root is
# among those), ext4_jsb (the root's JBD2 I/O error line), and keeps the
# kernel's lines in ext4-lines.  0 when there is nothing to say.
ext4_check() {
	ext4_bad= ext4_gaveup=0 ext4_root=0 ext4_jsb=
	run_log > "$ramdir/kmsg-run"
	: > "$ramdir/ext4-lines"
	while read -r csrc cmp c0 cname; do
		cwhat= cgave=0
		copts=$(mount_opts "$csrc")
		# Not mounted any more (a stick pulled and unmounted): nothing of
		# the system is on it.
		[ -n "$copts" ] || continue
		case ,$copts, in
		*,emergency_ro,*|*,shutdown,*) cwhat="$cwhat, mounted $copts"; cgave=1 ;;
		esac
		if num "$c0"; then
			c1=$(ext4_errors "$cname")
			if ! num "$c1"; then
				cwhat="$cwhat, its error count can no longer be read"
				cgave=1
			elif [ "$c1" -gt "$c0" ]; then
				le=$(ext4_last_error "$cname")
				cwhat="$cwhat, its error count rose $c0 -> $c1${le:+ (last error in $le)}"
				cgave=1
			fi
		fi
		case $cname in
		''|*[!A-Za-z0-9_-]*) ;;
		*)
			grep -E "EXT4-fs error \\(device $cname\\)|Aborting journal on device $cname-|EXT4-fs \\($cname\\): Remounting filesystem read-only|JBD2: I/O error when updating journal superblock for $cname-" \
				"$ramdir/kmsg-run" > "$ramdir/ext4-$cname"
			cab=$(grep -E 'Aborting journal on device|Remounting filesystem read-only' "$ramdir/ext4-$cname" |
				awk '{sub(/^\[[^]]*\] /, ""); print; exit}')
			if [ -n "$cab" ]; then
				cwhat="$cwhat, the kernel logged: $cab"
				cgave=1
			fi
			cn=$(grep -c 'EXT4-fs error' "$ramdir/ext4-$cname")
			if [ "${cn:-0}" != 0 ]; then
				cerr=$(grep 'EXT4-fs error' "$ramdir/ext4-$cname" | awk '{sub(/^\[[^]]*\] /, ""); print; exit}')
				cwhat="$cwhat, $cn ext4 error lines, the first: $cerr"
				# On the read-only UFS disk, alone: errors=continue.
				case $cname in
				"$disk"[0-9]*) [ "$setro" = 1 ] || cgave=1 ;;
				*) cgave=1 ;;
				esac
			fi
			[ "$csrc" != "$rootsrc" ] || ext4_jsb=$(grep 'JBD2: I/O error when updating journal superblock' "$ramdir/ext4-$cname" |
				awk '{sub(/^\[[^]]*\] /, ""); print; exit}')
			grep '' "$ramdir/ext4-$cname" >> "$ramdir/ext4-lines" ;;
		esac
		[ -z "$cwhat" ] || ext4_bad="$ext4_bad; $cmp ($csrc):${cwhat#,}"
		if [ "$cgave" = 1 ]; then
			ext4_gaveup=1
			[ "$csrc" != "$rootsrc" ] || ext4_root=1
		fi
	done < "$ramdir/errors-before"
	ext4_bad=${ext4_bad#; }
	[ -z "$ext4_bad" ]
}

# What to do about an ext4 error found before any sleep, with the tablet
# running on, thawed (unprotect_and_stop).
e2fsck_next() {
	echo "NEXT: ext4 recorded an error on, or gave up on: $ext4_bad; the tablet runs on, thawed: save what you need, then go to recovery ($recovery_keys) and run e2fsck -f on it there before the next boot: this port's initramfs has no fsck, and a root with a recorded error and deleted files held open does not mount (the 2026-11-22 boot loop; docs/resume-recovery.md, \"Repairing the root from recovery\")"
}

# After a sleep: the verdict lines of ext4_check, when it found something
# not said yet, and what the next boot will do about it.  WHEN goes into
# the verdict word.  1 when ext4 has something to say.
ext4_report() { # WHEN
	ext4_check && return 0
	[ "$ext4_bad" != "$ext4_said" ] || return 1
	ext4_said=$ext4_bad
	if [ "$ext4_gaveup" = 1 ]; then
		verdict "SLEEP-TEST ERROR-STATE$1: ext4 recorded an error on, or gave up on, a filesystem of this run: $ext4_bad"
	else
		verdict "SLEEP-TEST NOTE$1: ext4 logged errors on a filesystem of the read-only UFS disk, and with errors=continue it neither aborted its journal nor saved them: $ext4_bad"
	fi
	awk 'NR <= 6' "$ramdir/ext4-lines" | while IFS= read -r el; do keep "ext4: ${el#\[*\] }"; done
	if [ "$ext4_root" = 1 ]; then
		if [ -n "$ext4_jsb" ]; then
			verdict "NEXT: ext4 gave up on the root; jbd2 writes the error into the journal's superblock even with the disk read-only, but the kernel logged that a journal superblock write failed ($ext4_jsb), so it may not have reached the disk: if the boot after this restart comes up, run sh ~/typec-roles/verify.sh at once; if it restarts again and again (a root that does not mount: panic, restart after 10 s), go to recovery ($recovery_keys) and repair the root with e2fsck -f (docs/resume-recovery.md, \"Repairing the root from recovery\")"
		else
			verdict "NEXT: ext4 gave up on the root, and jbd2 writes the error into the journal's superblock even with the disk read-only: the boot after this restart will probably not mount the root, and restart every few seconds (panic=10; this port's initramfs has no fsck): go to recovery ($recovery_keys) and repair the root with e2fsck -f (docs/resume-recovery.md, \"Repairing the root from recovery\")"
		fi
	elif [ "$ext4_gaveup" = 1 ]; then
		verdict "NEXT: run e2fsck -f on each filesystem named above (not the root) before it is mounted again; the boot does not need it unless /etc/fstab mounts it"
	else
		verdict "NEXT: nothing of those errors was written: the boot after this restart is expected to mount the root as usual; check that it did: sh ~/typec-roles/verify.sh (it fails on any error ext4 recorded) and errors_count 0; if it restarts again and again, go to recovery ($recovery_keys) and e2fsck -f the root (docs/resume-recovery.md)"
	fi
	return 1
}

# What to do after a freeze that failed or did not end.
freeze_next() {
	if [ "$freeze_hung" = 1 ]; then
		echo "NEXT: a freeze waits for every writer in flight and for the disk; one that does not end has a writer that does not finish, or a disk that does not answer: the boot after this restart mounts the root as after a power loss (with whatever was not frozen yet replayed from its journal); the kernel log kept in pstore (console-ramoops) may name the task (\"blocked for more than\"); then run again, from multi-user.target if it is a desktop program (README, \"The sleep test\")"
		return 0
	fi
	if ! ext4_check; then
		e2fsck_next
		return 0
	fi
	case $froze_why in
	*'Device or resource busy'*)
		echo 'NEXT: something else holds that filesystem frozen (another fsfreeze?): find what froze it, thaw it (sudo fsfreeze -u MOUNTPOINT) and run again' ;;
	*'which this run does not freeze'*|*'cannot name the mount point'*)
		echo 'NEXT: unmount that filesystem (sudo umount MOUNTPOINT) and run again' ;;
	*)
		echo 'NEXT: nothing slept; the kernel log says why the freeze failed (sudo dmesg | tail -n 40); run again once that is understood' ;;
	esac
}

# Stop before any sleep, when the protection could not be completed: the UFS
# disk writable at the block layer again, if this run made it read-only,
# then every filesystem it froze thawed, then errors= put back.  No restart;
# but if anything stays frozen or read-only, a half-frozen system is not
# left running: restart.
unprotect_and_stop() { # WHY NEXT
	# Never set before the first sleep (knobs_on is right before rtcwake),
	# and back after each: nothing to put back, unless that changes.
	knobs_off
	if [ "$freeze_hung" = 1 ]; then
		verdict "SLEEP-TEST RESULT: STOPPED before any sleep: $1, and cannot be undone: restarting (sysrq b) with what this run froze still frozen; floor $floor; kernel $kernel$optnote"
		verdict "$2"
		finish healthy
	fi
	if [ "$setro" = 1 ] && ! blockdev --setrw "/dev/$disk" >> "$ramdir/log" 2>&1; then
		verdict "SLEEP-TEST RESULT: STOPPED before any sleep: $1; and blockdev --setrw /dev/$disk failed, so nothing is thawed: restarting (sysrq b); floor $floor; kernel $kernel$optnote"
		verdict "$2"
		finish healthy
	fi
	if thaw_all; then
		# The debug switches this run set go back to N, and the RPMh
		# tracepoint off: the tablet runs on.
		sw_off
		rpmh_off
		sw_note
		if errors_back; then
			eb="every filesystem this run froze is thawed again${back_done:+, errors= is back as it was on:$back_done}, and the tablet is not restarted$sw_back"
		else
			eb="every filesystem this run froze is thawed again, and the tablet is not restarted$sw_back; but $back_why"
		fi
		verdict "SLEEP-TEST RESULT: STOPPED before any sleep: $1; $eb; floor $floor; kernel $kernel$optnote"
		verdict "$2"
		[ -z "$back_why" ] ||
			verdict 'NEXT: put errors= back by hand as said above, or restart the tablet (sudo reboot): the boot mounts every filesystem with its usual options'
		say "stopped: swap on the disk stays off until the next boot${debug_note:+; $debug_note}"
		exit 1
	fi
	verdict "SLEEP-TEST RESULT: STOPPED before any sleep: $1; and $thaw_why: restarting (sysrq b), a filesystem stays frozen; floor $floor; kernel $kernel$optnote"
	verdict "$2"
	finish healthy
}

# The restart: sysrq b, with every disk filesystem frozen (or read-only), so
# that the boot after it mounts them as after a power loss.  No clean
# reboot: systemd would stop services that wait on the frozen disk.  ext4
# is looked at once more first: what it did during the hold is said too.
restart() { # healthy|failed
	ext4_report ' (right before the restart)'
	if [ "$1" = healthy ]; then
		say 'restart: sysrq b; every disk filesystem frozen; the storage read back as before'
	else
		say 'restart: sysrq b; every disk filesystem frozen since before the first sleep'
	fi
	verdict "SLEEP-TEST RESTART: sysrq b; frozen:$(awk '{ printf " %s", $2 }' "$ramdir/frozen")"
	printf b >> /proc/sysrq-trigger
	sleep 30
	say "restart: still here after sysrq b; every disk filesystem stays frozen, so sudo, reboot and a new login may hang: force a restart (hold Power and Volume Down until the tablet restarts; for recovery: $recovery_keys)"
	exit 1
}

# The end of a run, once its verdict is out: what ext4 did, the next steps,
# the hold, the restart.  A run always ends in a restart from here: with
# the disk frozen the tablet cannot be used, and an ERROR-STATE does not get
# better by waiting; the lines stay in pstore.
finish() { # healthy|failed
	knobs_off
	ext4_report ''
	if [ "$armed" = 1 ]; then
		verdict "NEXT: the boot after this restart holds the interconnect floor once (loop breaker: this boot released it and cannot record a clean stop with its disk frozen); give the next run release-floor"
	fi
	if [ -n "$prot_bad" ]; then
		verdict "NEXT: the protection failed in this run ($prot_bad): after the restart run sh ~/typec-roles/verify.sh, and before any other sleep test check the root with e2fsck -fn from recovery; no sleeps until it is understood"
	fi
	if [ "$1" = failed ] && [ "$slept" = 0 ]; then
		[ "$ext4_root" = 1 ] ||
			verdict "NEXT: the run failed before any sleep (above), so it says nothing about a resume: after the restart run sh ~/typec-roles/verify.sh and cat /sys/fs/ext4/$rootname/errors_count (0); no run with sleeps until the failure is understood"
	elif [ "$ext4_root" = 1 ]; then
		[ "$1" != failed ] ||
			verdict 'NEXT: the storage did not come back: read the diagnostic lines above against docs/resume-recovery.md ("Reading the diagnostic")'
	elif [ "$1" = failed ]; then
		verdict "NEXT: the storage did not come back; after the restart run sh ~/typec-roles/verify.sh and cat /sys/fs/ext4/$rootname/errors_count (0), and read the diagnostic lines above against docs/resume-recovery.md (\"Reading the diagnostic\")"
	elif [ -n "$pcie_fail" ]; then
		verdict "NEXT: the storage came back (it read back as before, and no UFS failure line); the PCIe host did not ($pcie_fail): the restart brings it back. After it, sh ~/typec-roles/verify.sh and cat /sys/fs/ext4/$rootname/errors_count (0); the cycle's pcie evidence and kernel lines above say how the host failed"
	else
		verdict "NEXT: after the restart, sh ~/typec-roles/verify.sh and cat /sys/fs/ext4/$rootname/errors_count (it must say 0)"
	fi
	say "this run's log goes with /run at the restart; its verdict lines stay in pstore (sh ~/typec-roles/sleep-test.sh status says where)"
	say "the result stays in $ramdir/result (readable without root) for ${hold}s, then the tablet restarts"
	hn=0
	while [ "$hn" -lt "$hold" ]; do
		sleep 1
		hn=$((hn + 1))
	done
	restart "$1"
}

run_from_ram() { # CONFIG
	. "$1"
	mode_words
	PATH=$ramdir/tools
	TZ=UTC0
	TZDIR=$ramdir/tz
	export PATH TZ TZDIR
	trap '' HUP PIPE INT TERM
	# No terminal: the run's lines go to its log, which the part on the disk
	# shows on the terminal, and its errors too.  A terminal that stops
	# reading (an SSH server waiting on the frozen disk, a connection gone
	# quiet) can then not hold the run up.
	exec > /dev/null 2>> "$ramdir/log"
	# From here this run puts errors= back itself, or restarts.
	: > "$ramdir/started"
	: > "$ramdir/frozen"
	rmarker="gts9u sleep-test: $stamp start"
	setro=0 freeze_hung=0 prot_why= prot_bad= ext4_said= slept=0 knobs_set=0 sw_tried=0 sw_back= pcie_fail= rpmh_set=0
	sw_note
	# The first line in pmsg: a run that never comes back from its first
	# sleep leaves this, the protection's and its cycle's.
	if [ "$dry" = 1 ]; then
		progress "start: dry, hold=${hold}s; options: $options; floor $floor; kernel $kernel; up $(uptime_s)s"
	else
		progress "start: ro, $cycles cycles of ${seconds}s, aborts=$aborts, hold=${hold}s; options: $options; floor $floor; kernel $kernel; up $(uptime_s)s"
	fi
	# pci=remove: the root ports off the bus, the last change before the
	# freeze; a bus that does not empty stops the run here, nothing frozen.
	if [ "$pci_remove" = 1 ]; then
		pci_remove_all || unprotect_and_stop "$pci_why" "$(pci_next)"
	fi
	# gdsc= and cxmx=: the debug switches, after the PCI removal (a PCIe
	# GDSC switch is for a host that powers itself down) and before anything
	# is frozen; one that does not read back Y stops the run here, with the
	# ones that took put back.
	if [ -n "$sw_set$sw_ret" ]; then
		sw_on || unprotect_and_stop "a debug switch did not take: $sw_why" "$(sw_next)"
		sw_note
		sw_words="$sw_set${sw_ret:+ (N: $sw_ret)}"
		say "debug switches set right before the freeze:$sw_words; $swnote; they stay so until the restart that ends the run"
		progress "debug switches set:$sw_words; $swnote"
	fi
	# rpmh: the command DB copied once, then the tracepoint on and read
	# back; one that does not take stops the run here, nothing frozen.
	if [ "$rpmh" = 1 ]; then
		awk '{ print }' "$cmd_db" 2>/dev/null > "$ramdir/cmd-db"
		rpmh_set=1
		printf 1 2>/dev/null > "$rpmh_event"
		[ "$(ev_val "$rpmh_event")" = 1 ] ||
			unprotect_and_stop "the RPMh tracepoint did not go on ($rpmh_event reads $(ev_val "$rpmh_event") after a write of 1)" 'NEXT: run again without rpmh, or once that is understood'
		evn=$(grep -c '^0x' "$ramdir/cmd-db")
		say "rpmh: the tracepoint rpmh_send_msg is on until the restart; the command DB names ${evn:-0} RPMh addresses"
		progress "rpmh: tracepoint on, ${evn:-0} addresses named"
		# The clock tracepoints with it; one that does not take is said, and
		# the run goes on without the clock trace.
		if [ "${clk_trace:-0}" = 1 ]; then
			for evf in $clk_events; do
				printf 1 2>/dev/null > "$evf"
				[ "$(ev_val "$evf")" = 1 ] || clk_trace=0
			done
			if [ "$clk_trace" = 1 ]; then
				say "rpmh: the clk_prepare and clk_unprepare tracepoints are on until the restart too (XO at each sleep's entry)"
			else
				say "rpmh: a clock tracepoint did not go on ($clk_events): no clock trace, the XO line says so"
			fi
		else
			say "rpmh: no clk_prepare and clk_unprepare tracepoints to turn on ($clk_events): no clock trace"
		fi
	fi
	# Nothing recorded or given up since the checks before the run (minutes
	# ago with release-floor's settling): an error saved by now would be in
	# the root the freeze keeps.
	ext4_check ||
		unprotect_and_stop "ext4 recorded an error on, or gave up on, a filesystem before anything was frozen: $ext4_bad" "$(e2fsck_next)"
	say "$me: freezing every writable disk filesystem now, the root last; if a freeze fails, the run thaws them and stops"
	freeze_all || unprotect_and_stop "$froze_why" "$(freeze_next)"
	frozen_list=$(awk '{ printf " %s", $2 }' "$ramdir/frozen")
	say "every writable disk filesystem is frozen:$frozen_list"
	blockdev --setro "/dev/$disk" >> "$ramdir/log" 2>&1 ||
		unprotect_and_stop "blockdev --setro /dev/$disk failed" 'NEXT: the tablet runs on as before the run (disk swap off until the next boot); run again'
	setro=1
	ro=$(selected "/sys/class/block/$disk/ro")
	[ "$ro" = 1 ] ||
		unprotect_and_stop "/dev/$disk is not read-only at the block layer (ro=${ro:-?})" 'NEXT: the tablet runs on as before the run (disk swap off until the next boot); run again'
	say "/dev/$disk is read-only at the block layer too"
	ext4_check ||
		unprotect_and_stop "ext4 recorded an error on, or gave up on, a filesystem while it was being frozen: $ext4_bad" "$(e2fsck_next)"
	say "ext4: no error recorded, logged or given up on since the run began; error counts:$(awk '{ printf " %s %s", $2, $3 }' "$ramdir/errors-before")"
	say "$me: from here on the run cannot be stopped, and it ends with a restart (sysrq b)"
	dmarker="gts9u sleep-test: $stamp protected"
	printf '%s\n' "$dmarker" >> /dev/kmsg
	progress "protected: frozen:$frozen_list; /dev/$disk read-only; from here the run ends with a restart"
	# The state the sleeps start from (reads only), before the first read so
	# that the first sleep follows that read as closely as before.
	[ "$dry" = 1 ] || ev_once
	read_check 0
	if [ -z "$read_hash" ]; then
		verdict "SLEEP-TEST RESULT: FAIL before any sleep: the first read of $part failed ($read_why); floor $floor; kernel $kernel$optnote"
		finish failed
	fi
	base_hash=$read_hash
	say "read of $part ($read_count x $read_bs, O_DIRECT): sha256 ${base_hash%"${base_hash#????????????????}"}"

	# dry: no sleep; the read again after a while, no failure line in the
	# kernel log since the run began, every filesystem still frozen, and
	# ext4 quiet.
	if [ "$dry" = 1 ]; then
		sleep "$settle"
		read_check dry
		run_log > "$ramdir/kmsg-dry"
		fails=$(grep -cE "$(fail_re)" "$ramdir/kmsg-dry")
		still_frozen
		prot_bad=$prot_why
		if [ -z "$read_hash" ]; then
			why="the read after ${settle}s failed: $read_why"
		elif [ "$read_hash" != "$base_hash" ]; then
			why="the read after ${settle}s differs from the first one"
		elif [ "${fails:-0}" != 0 ]; then
			why="$fails failure lines in the kernel log, the first: $(grep -E "$(fail_re)" "$ramdir/kmsg-dry" | awk '{sub(/^\[[^]]*\] /, ""); print; exit}')"
		elif [ -n "$prot_why" ]; then
			why="the protection failed: $prot_why"
		elif ! ext4_check; then
			why="ext4: $ext4_bad"
		else
			why=
		fi
		if [ -n "$why" ]; then
			verdict "SLEEP-TEST RESULT: FAIL (dry, no sleep tried): $why; floor $floor; kernel $kernel$optnote"
			finish failed
		fi
		verdict "SLEEP-TEST RESULT: DRY: no sleep tried; frozen:$frozen_list; /dev/$disk read-only at the block layer; a second freeze found each still frozen; the UFS read back as before after ${settle}s; no failure line in the kernel log, and ext4 recorded, logged and gave up on nothing (looked at again right before the restart); floor $floor; kernel $kernel$optnote"
		verdict 'NEXT: the boot after this restart (sysrq b, everything frozen) must come up as after a power loss; sh ~/typec-roles/verify.sh then says so (it knows the boot after a sleep test); only then a run with sleeps'
		finish healthy
	fi

	# Before the first sleep: no failure line in the kernel log since the run
	# began (a write the block layer refused, a journal started on a frozen
	# ext4, a UFS or I/O error).
	run_log > "$ramdir/kmsg-0"
	fails=$(grep -cE "$(fail_re)" "$ramdir/kmsg-0")
	if [ "${fails:-0}" != 0 ]; then
		verdict "SLEEP-TEST RESULT: FAIL before any sleep: $fails failure lines in the kernel log since the run began, the first: $(grep -E "$(fail_re)" "$ramdir/kmsg-0" | awk '{sub(/^\[[^]]*\] /, ""); print; exit}'); floor $floor; kernel $kernel$optnote"
		finish failed
	fi

	# try: the attempt at cycle i (more than one after an aborted attempt);
	# aborted: the run's aborted attempts, each one's cause in "aborts".
	i=1 passes=0 deep=0 unknown=0 try=1 aborted=0
	: > "$ramdir/aborts"
	while [ "$i" -le "$cycles" ]; do
		cyc="cycle $i/$cycles"
		# An attempt after the first has files and a marker of its own:
		# rtcwake-1.2, kmsg-1.2, ...
		sfx=$i attempt=
		if [ "$try" != 1 ]; then
			sfx=$i.$try attempt=", attempt $try"
		fi
		# A disk filesystem mounted since (a dock's stick after a resume):
		# frozen as well, or no sleep.
		rw=$(unfrozen)
		if [ -n "$rw" ]; then
			say "$cyc: writable and not frozen:$rw; freezing it"
			freeze_all || say "$cyc: $froze_why"
			rw=$(unfrozen)
		fi
		sw=$(disk_swaps)
		# mode=s2idle does not use mem_sleep (rtcwake -m freeze), and leaves it.
		ms=
		if [ "$sleep_mode" = deep ]; then
			[ "$(selected /sys/power/mem_sleep)" = deep ] || printf deep > /sys/power/mem_sleep 2>/dev/null
			ms=$(selected /sys/power/mem_sleep)
		fi
		ro=$(selected "/sys/class/block/$disk/ro")
		if [ -n "$rw" ] || [ -n "$sw" ] || [ "$ro" != 1 ] || { [ "$sleep_mode" = deep ] && [ "$ms" != deep ]; }; then
			why="writable and not frozen:${rw:- none}; disk swap:${sw:- none}; /dev/$disk ro=${ro:-?}"
			[ "$sleep_mode" != deep ] || why="$why; mem_sleep $ms"
			verdict "SLEEP-TEST $cyc: NOT STARTED$mword: $why; $swnote"
			an=$(abort_note)
			verdict "SLEEP-TEST RESULT: ABORTED$mword at $cyc, before its sleep ($why); $passes passed before it${an:+; $an and tried again}; floor $floor; kernel $kernel$optnote"
			finish healthy
		fi
		# rpmh: the trace emptied right before the attempt's clock read, so
		# that it holds what follows that read (ev_xo_entry) and the
		# attempt's RPMh commands (ev_rpmh).
		[ "$rpmh" = 0 ] || : 2>/dev/null > "$trace_dir/trace"
		ev_before "$sfx"
		ok0=$(stat_of success) fail0=$(stat_of fail)
		a0=$(count_of aosd) x0=$(count_of cxsd) d0=$(count_of ddr)
		# Each attempt's marker is its own: the kernel lines after it are
		# that attempt's alone.
		marker="gts9u sleep-test: $stamp cycle $i"
		[ "$try" = 1 ] || marker="$marker attempt $try"
		printf '%s\n' "$marker" >> /dev/kmsg
		if [ -n "$pm_test" ]; then
			say "$cyc: pm_test=$pm_test cycle: rtcwake -m $rtc_mode -s $seconds, and the suspend stops at $pm_test ($(pm_test_what "$pm_test")), waits ${pm_test_delay}s there and comes back, $no_sleep_call$attempt; $(roles)"
		elif [ "$sleep_mode" = s2idle ]; then
			say "$cyc: suspend-to-idle for ${seconds}s (rtcwake -m freeze)$attempt; $(roles)"
		else
			say "$cyc: deep sleep for ${seconds}s (rtcwake -m mem)$attempt; $(roles)"
		fi
		# The experiment's knobs, for this sleep alone: set now, back right
		# after rtcwake.  A pm_test= that did not take sleeps not at all.
		knobs_on
		if [ -n "$knobs_why" ]; then
			knobs_off
			verdict "SLEEP-TEST $cyc: NOT STARTED$mword: $knobs_why; $swnote"
			an=$(abort_note)
			verdict "SLEEP-TEST RESULT: ABORTED$mword at $cyc, before its sleep ($knobs_why); $passes passed before it${an:+; $an and tried again}; floor $floor; kernel $kernel$optnote"
			finish healthy
		fi
		[ -z "$knobs_now" ] || say "$cyc: for this sleep: $knobs_now"
		# The debug switches as this sleep has them, for its verdict.
		sw_note
		slept=1
		# The last line before the sleep, for a run that never comes back.
		progress "$cyc: entering the sleep, rtcwake -m $rtc_mode -s $seconds$attempt; ${knobs_now:-no pm_test, no trace}; $swnote; up $(uptime_s)s"
		up0=$(uptime_s)
		rtcwake -u -d "$rtc" -m "$rtc_mode" -s "$seconds" > "$ramdir/rtcwake-$sfx" 2>&1
		rtc_rc=$?
		up1=$(uptime_s)
		knobs_off
		# The sleep statistics as the sleep left them (reads only).
		ev_resumed "$sfx"
		# Right after the resume: is everything still frozen?
		still_frozen
		[ -z "$prot_why" ] || prot_bad=$prot_why
		sleep "$settle"
		read_check "$sfx"
		sleep 2
		dmesg > "$ramdir/kmsg.all" 2>/dev/null
		k=$ramdir/kmsg-$sfx
		since "$marker" "$ramdir/kmsg.all" > "$k"
		ok1=$(stat_of success) fail1=$(stat_of fail)
		a1=$(count_of aosd) x1=$(count_of cxsd) d1=$(count_of ddr)
		# The first wakeup IRQ of this suspend; pm_wakeup_irq has no data
		# (ENODATA) when none was recorded, as after a pm_test cycle, which
		# ends by itself.  The kernel's own line, if the file says nothing.
		wake=$(selected /sys/power/pm_wakeup_irq)
		[ -n "$wake" ] || wake=$(awk '/Triggering wakeup from IRQ [1-9]/ {print $NF; exit}' "$k")
		# Asleep: the time timekeeping was suspended, one line per suspension
		# (more than one in a suspend-to-idle whose CPUs woke and froze again).
		asleep=$(awk '/Timekeeping suspended for/ {for (n = 1; n < NF; n++) if ($n == "for") s += $(n + 1)} END {if (s) printf "%.1f", s}' "$k")
		tk_n=$(grep -c 'Timekeeping suspended for' "$k")
		s2u= s2v=

		reasons=
		storage=healthy
		fails=$(grep -cE "$(fail_re)" "$k")
		if [ "${fails:-0}" != 0 ]; then
			first=$(grep -E "$(fail_re)" "$k" | awk '{sub(/^\[[^]]*\] /, ""); print; exit}')
			reasons="$reasons; $fails failure lines in the kernel log, the first: $first"
			# Every one of them fails the cycle; the storage failed only
			# with one that is not the PCIe host's or its PHY's (pcie_dev),
			# or with the read (below).
			! grep -E "$(fail_re)" "$k" | grep -qvE "$pcie_dev" || storage=failed
		fi
		[ -z "$prot_why" ] || reasons="$reasons; the protection failed: $prot_why"
		[ "$rtc_rc" = 0 ] || reasons="$reasons; rtcwake returned $rtc_rc ($(awk 'NR == 1' "$ramdir/rtcwake-$sfx"))"
		# Tasks that the suspend could not freeze (not a wakeup: "failed",
		# not "aborted"); freezer_failed below.
		tfail=$(grep -E "$freezer_re" "$k" | awk '{sub(/^\[[^]]*\] /, ""); print; exit}')
		[ -z "$tfail" ] || reasons="$reasons; the suspend could not freeze every task: $tfail"
		grep -q "PM: suspend entry ($mem_label)" "$k" || reasons="$reasons; no 'PM: suspend entry ($mem_label)'"
		grep -q 'PM: suspend exit' "$k" || reasons="$reasons; no 'PM: suspend exit'"
		[ "$(delta "$ok0" "$ok1")" = +1 ] || reasons="$reasons; suspend_stats success $ok0 -> $ok1"
		[ "$(delta "$fail0" "$fail1")" = +0 ] || reasons="$reasons; suspend_stats fail $fail0 -> $fail1"
		if [ -z "$read_hash" ]; then
			reasons="$reasons; the read failed: $read_why"
			storage=failed
		elif [ "$read_hash" != "$base_hash" ]; then
			reasons="$reasons; the read differs from the first one"
			storage=failed
		fi
		# A pm_test cycle logs the kernel's wait at its level
		# (suspend_test()); without it the suspend did not stop there.
		if [ -n "$pm_test" ] && ! grep -q 'suspend debug: Waiting for' "$k"; then
			reasons="$reasons; no 'suspend debug: Waiting for' line: the suspend did not stop at pm_test=$pm_test"
		fi
		reasons=${reasons#; }

		# Depth: AOSS sleep, CX collapse and DDR low power, each at least once.
		# A pm_test cycle has none: nothing sleeps.
		depth=deep
		dline=
		for st in "aosd $a0 $a1" "cxsd $x0 $x1" "ddr $d0 $d1"; do
			set -- $st
			dv=$(delta "$2" "$3")
			dline="$dline $1 $dv"
			case $dv in
			'?') depth=unknown ;;
			+0) [ "$depth" = unknown ] || depth=shallow ;;
			esac
		done
		dline=${dline# }
		[ -z "$pm_test" ] || depth=pm_test
		grep -E "$ufs_diag" "$k" > "$ramdir/diag-$sfx"
		dn=$(grep -c '' "$ramdir/diag-$sfx")
		dj=0
		while IFS= read -r dl; do
			dj=$((dj + 1))
			dl="$cyc: diagnostic: ${dl#\[*\] }"
			say "$dl"
			if [ "$dj" -le "$diag_head" ] || [ "$dj" -gt $((dn - diag_tail)) ]; then
				keep "$dl"
			elif [ "$dj" = $((diag_head + 1)) ]; then
				keep "$cyc: diagnostic: $((dn - diag_head - diag_tail)) of $dn lines left out here (kept: the first $diag_head and the last $diag_tail); the terminal and $ramdir/log had them all"
			fi
		done < "$ramdir/diag-$sfx"
		# The time asleep and the wakeup IRQ: after a pm_test cycle there may
		# be neither (timekeeping is suspended only from core on, and nothing
		# woke the tablet): the line says so in words, not "?".
		if [ -n "$asleep" ]; then
			sl="asleep ${asleep}s"
		else
			sl='no "Timekeeping suspended for" line (timekeeping was not suspended)'
		fi
		if [ -n "$wake" ]; then
			wl="woken by IRQ $wake"
		else
			wl='no wakeup IRQ recorded (pm_wakeup_irq has no data)'
		fi
		say "$cyc: $sl, $wl, rtcwake $rtc_rc, suspend_stats success $(delta "$ok0" "$ok1") fail $(delta "$fail0" "$fail1"), $dline, $(roles), USB devices $(usb_count)"
		ev_after "$sfx" "$k"
		# A suspend the kernel gave up before the sleep, the storage as it
		# was: not a FAIL.  The same cycle again, or, past the budget, the
		# end of the run without a storage verdict.
		if [ -n "$reasons" ] && aborted_attempt; then
			aborted=$((aborted + 1))
			abort_cause
			printf '%s\n' "$abort_by" >> "$ramdir/aborts"
			gaveup="ABORTED$mword: the kernel gave up the suspend before the tablet slept (rtcwake $rtc_rc, suspend_stats success +0 fail +1): $abort_why; the UFS read back as before"
			if [ "$aborted" -gt "$aborts" ]; then
				verdict "SLEEP-TEST $cyc: $gaveup; aborted attempt $aborted, more than aborts=$aborts: the run ends; floor $floor; $swnote"
				no_sleep_end
			fi
			if pcie_down; then
				verdict "SLEEP-TEST $cyc: $gaveup; aborted attempt $aborted (aborts=$aborts); floor $floor; $swnote"
				pcie_down_end
			fi
			verdict "SLEEP-TEST $cyc: $gaveup; aborted attempt $aborted (aborts=$aborts): $cyc again in ${abort_pause} s; floor $floor; $swnote"
			try=$((try + 1))
			sleep "$abort_pause"
			continue
		fi
		ev_keep "$sfx"
		# A suspend whose task freezer gave up, the storage as it was: no
		# storage verdict, and not tried again.
		if [ -n "$reasons" ] && freezer_failed; then
			stuck=$(stuck_tasks "$k")
			an=$(abort_note)
			verdict "SLEEP-TEST $cyc: NO-SLEEP (freezer$s2fz): the kernel gave up the suspend before the tablet slept because a task did not freeze (rtcwake $rtc_rc, suspend_stats success +0 fail +1): $tfail; stuck: ${stuck:-not named}; the UFS read back as before; not tried again; floor $floor; $swnote"
			verdict "SLEEP-TEST RESULT: NO-SLEEP (freezer$s2fz) at $cyc: a task did not freeze (${stuck:-not named}); $passes of $cycles cycles passed before it${an:+; $an and tried again}; this run does not answer whether the storage comes back from a deep sleep; floor $floor; kernel $kernel$optnote"
			verdict 'NEXT: no storage verdict: a task that waits behind a writer of the frozen disk (on a memory map'"'"'s lock) cannot be frozen; touch nothing during the run, and if it happens again, run the test from multi-user.target, without the desktop (README, "The sleep test")'
			finish healthy
		fi
		# mode=s2idle: a sleep the kernel counted, cut short by a wakeup that
		# was not the RTC's (woken_early), is no pass at any depth (unless
		# qcom_stats counted the deep states, which is a pass as in deep):
		# the same cycle again, within the aborted attempts' budget.
		if [ "$sleep_mode" = s2idle ] && [ -z "$reasons" ] && [ "$depth" != pm_test ] &&
			[ "$depth" != deep ] && woken_early; then
			aborted=$((aborted + 1))
			abort_cause
			printf 'woken early by %s\n' "$abort_by" >> "$ramdir/aborts"
			early="WOKEN EARLY$mword: the suspend-to-idle was cut short ($s2v; ${seconds}s asked for) by $abort_why; the UFS read back as before"
			if [ "$aborted" -gt "$aborts" ]; then
				verdict "SLEEP-TEST $cyc: $early; counted as aborted attempt $aborted, more than aborts=$aborts: the run ends; floor $floor; $swnote"
				no_sleep_end
			fi
			if pcie_down; then
				verdict "SLEEP-TEST $cyc: $early; counted as aborted attempt $aborted (aborts=$aborts); floor $floor; $swnote"
				pcie_down_end
			fi
			verdict "SLEEP-TEST $cyc: $early; counted as aborted attempt $aborted (aborts=$aborts): $cyc again in ${abort_pause} s; floor $floor; $swnote"
			try=$((try + 1))
			sleep "$abort_pause"
			continue
		fi
		# mode=s2idle: no CPU entered its s2idle state, and no wakeup other
		# than the RTC's explains it: a FAIL (the storage as it was).
		if [ "$sleep_mode" = s2idle ] && [ "$depth" != pm_test ] && [ "$depth" != deep ] && [ "$s2u" = 0 ]; then
			reasons="${reasons:+$reasons; }no CPU entered its s2idle state (s2idle entries +0 on every CPU)"
		fi
		if [ -n "$reasons" ]; then
			an=$(abort_note)
			verdict "SLEEP-TEST $cyc: FAIL$mword: $reasons ($dline; floor $floor; $swnote)"
			verdict "SLEEP-TEST RESULT: FAIL$mword at $cyc: $reasons; $passes passed before it${an:+; $an and tried again}; floor $floor; kernel $kernel$optnote"
			# A calibration of this cycle without the UFS PHY's power-on
			# before it (phy_leak_seen): what that means, first.
			! phy_leak_seen "$ramdir/diag-$sfx" ||
				verdict "NEXT: the UFS PHY's power-on did not run before its calibration: a leaked PHY power reference (docs/resume-recovery.md, \"The PHY power reference\")"
			# The storage came back: finish says what did not, if it was
			# the PCIe host.
			[ "$storage" != healthy ] ||
				pcie_fail=$(grep -E "$pcie_down_re" "$k" | awk '{sub(/^\[[^]]*\] /, ""); print; exit}')
			finish "$storage"
		fi
		case $depth in
		pm_test)
			verdict "SLEEP-TEST $cyc: PM-TEST $pm_test$mword: the suspend went as far as pm_test=$pm_test ($(pm_test_what "$pm_test")), waited there and came back with $no_sleep_call, and the UFS read back as before; qcom_stats $dline (CPU idle during the wait, not a depth test); floor $floor; $swnote" ;;
		deep)
			deep=$((deep + 1))
			verdict "SLEEP-TEST $cyc: PASS$mword: deep ($dline), resumed, the UFS read back as before${s2v:+; $s2v}; floor $floor; $swnote" ;;
		shallow)
			verdict "SLEEP-TEST $cyc: SHALLOW$mword: resumed and the UFS read back as before, but not every sleep state was reached ($dline${s2v:+; $s2v}); floor $floor; $swnote" ;;
		*)
			unknown=$((unknown + 1))
			verdict "SLEEP-TEST $cyc: DEPTH-UNKNOWN$mword: resumed and the UFS read back as before; qcom_stats not readable ($dline${s2v:+; $s2v}); floor $floor; $swnote" ;;
		esac
		passes=$((passes + 1))
		! pcie_down || pcie_down_end
		i=$((i + 1)) try=1
	done

	an=$(abort_note)
	if [ -n "$pm_test" ]; then
		verdict "SLEEP-TEST RESULT: PM-TEST $pm_test$mword: $passes of $cycles cycles went as far as pm_test=$pm_test and came back with the UFS readable; $no_sleep_call, so no depth was tested${an:+; $an and tried again}; floor $floor; kernel $kernel$optnote"
		finish healthy
	fi
	if [ "$deep" = "$cycles" ]; then
		word=PASS
	elif [ "$unknown" != 0 ]; then
		word=DEPTH-UNKNOWN
	else
		word=SHALLOW
	fi
	heldnote=
	[ "$floor" = released ] || heldnote=' (not the depth of the 2026-11-21 failure, which came with the floor released)'
	verdict "SLEEP-TEST RESULT: $word$mword: $passes of $cycles cycles resumed with the UFS readable, $deep of them deep${an:+; $an and tried again}; floor $floor$heldnote; kernel $kernel$optnote"
	finish healthy
}

if [ "${1:-}" = __ram ]; then
	run_from_ram "$2"
	exit 1
fi

# ------------------------------------------------------ before: the disk ---
cycles=3 seconds=30 hold=300 aborts=5 release=0 confirmed=0 numbers=0 dry=0 mode= aborts_given=0
pci_remove=0 pci_keep=0 pm_test= trace= sleep_mode= sw_set= sw_ret= gdsc_given=0 cxmx_given=0 ret_given=0 rpmh=0
rpd3_given=0 sensors_stop=0 sensors_opt=
for a in "$@"; do
	case $a in
	ro|dry)
		[ -z "$mode" ] || { echo "$me: one mode at most (ro or dry)" >&2; usage; }
		mode=$a
		[ "$a" = ro ] || dry=1 ;;
	restart-ok) confirmed=1 ;;
	release-floor) release=1 ;;
	hold=*) hold=${a#hold=} ;;
	aborts=*) aborts=${a#aborts=} aborts_given=1 ;;
	pci=remove|pci=keep)
		[ "$pci_remove$pci_keep" = 00 ] || { echo "$me: one pci= at most (pci=remove or pci=keep)" >&2; usage; }
		[ "$a" = pci=remove ] && pci_remove=1 || pci_keep=1 ;;
	pci=*) echo "$me: pci= is remove or keep, not '${a#pci=}'" >&2; usage ;;
	pm_test=*)
		[ -z "$pm_test" ] || { echo "$me: one pm_test= at most" >&2; usage; }
		pm_test=${a#pm_test=}
		case $pm_test in
		freezer|devices|platform|processors|core) ;;
		*) echo "$me: pm_test= is freezer, devices, platform, processors or core, not '$pm_test'" >&2; usage ;;
		esac ;;
	trace|trace=live)
		[ -z "$trace" ] || { echo "$me: one trace or trace=live at most" >&2; usage; }
		trace=on
		[ "$a" = trace ] || trace=live ;;
	trace=*) echo "$me: trace or trace=live, not '$a'" >&2; usage ;;
	mode=deep|mode=s2idle)
		[ -z "$sleep_mode" ] || { echo "$me: one mode= at most" >&2; usage; }
		sleep_mode=${a#mode=} ;;
	mode=*) echo "$me: mode= is deep or s2idle, not '${a#mode=}'" >&2; usage ;;
	gdsc=*)
		[ "$gdsc_given" = 0 ] || { echo "$me: one gdsc= at most" >&2; usage; }
		gdsc_given=1
		gl=${a#gdsc=}
		case $gl in
		''|,*|*,|*,,*) echo "$me: gdsc= is one or more of pcie0, pcie0phy and usb, comma-separated, not '$gl'" >&2; usage ;;
		esac
		while [ -n "$gl" ]; do
			gw=${gl%%,*}
			case $gl in *,*) gl=${gl#*,} ;; *) gl= ;; esac
			case $gw in
			pcie0|pcie0phy|usb) ;;
			*) echo "$me: gdsc= is one or more of pcie0, pcie0phy and usb, comma-separated: not '$gw'" >&2; usage ;;
			esac
			case " $sw_set " in *" $gw "*) echo "$me: gdsc= names $gw twice" >&2; usage ;; esac
			sw_set="$sw_set $gw"
		done ;;
	gdsc-ret=pcie0)
		[ "$ret_given" = 0 ] || { echo "$me: one gdsc-ret= at most" >&2; usage; }
		ret_given=1
		sw_ret=pcie0 ;;
	gdsc-ret=*) echo "$me: gdsc-ret=pcie0 is the only gdsc-ret= option, not '$a'" >&2; usage ;;
	rpmh)
		[ "$rpmh" = 0 ] || { echo "$me: one rpmh at most" >&2; usage; }
		rpmh=1 ;;
	cxmx=ret)
		[ "$cxmx_given" = 0 ] || { echo "$me: one cxmx= at most" >&2; usage; }
		cxmx_given=1
		sw_set="$sw_set cxmx" ;;
	cxmx=*) echo "$me: cxmx=ret is the only cxmx= option, not '$a'" >&2; usage ;;
	root-port-d3)
		[ "$rpd3_given" = 0 ] || { echo "$me: one root-port-d3 at most" >&2; usage; }
		rpd3_given=1
		sw_set="$sw_set rpd3" ;;
	sensors=stop|sensors=hook)
		[ "$sensors_stop" = 0 ] || { echo "$me: one sensors= at most" >&2; usage; }
		sensors_opt=$a
		sensors_stop=1
		[ "$a" != sensors=hook ] || sensors_stop=hook ;;
	sensors=*) echo "$me: sensors=stop and sensors=hook are the sensors= options, not '$a'" >&2; usage ;;
	*)
		num "$a" || { echo "$me: unknown argument '$a'" >&2; usage; }
		numbers=$((numbers + 1))
		case $numbers in
		1) cycles=$a ;;
		2) seconds=$a ;;
		*) echo "$me: one CYCLES and one SECONDS at most" >&2; usage ;;
		esac ;;
	esac
done
if [ "$dry" = 1 ]; then
	[ "$numbers" = 0 ] && [ "$aborts_given" = 0 ] || { echo "$me: dry sleeps not at all: no CYCLES, SECONDS or aborts=" >&2; usage; }
	[ -z "$pm_test" ] && [ -z "$trace" ] || { echo "$me: dry sleeps not at all: no pm_test= or trace" >&2; usage; }
	[ "$pci_keep" = 0 ] || { echo "$me: dry sleeps not at all: pci=keep is for a sleep with the PCI devices kept" >&2; usage; }
	[ "$rpmh" = 0 ] || { echo "$me: dry sleeps not at all: rpmh is for the RPMh commands of a sleep" >&2; usage; }
	[ "$rpd3_given" = 0 ] || { echo "$me: dry sleeps not at all: root-port-d3 is for the sleeps of a pci=keep run" >&2; usage; }
	[ "$sensors_stop" = 0 ] || { echo "$me: dry sleeps not at all: $sensors_opt is for the audio DSP's wakes in a sleep" >&2; usage; }
	cycles=0
else
	[ "$cycles" -ge 1 ] && [ "$cycles" -le 10 ] || { echo "$me: CYCLES is 1 to 10, not $cycles" >&2; usage; }
fi
case $seconds in 15|30|60) ;; *) echo "$me: SECONDS is 15, 30 or 60, not $seconds" >&2; usage ;; esac
num "$hold" && [ "$hold" -le 3600 ] || { echo "$me: hold=S is 0 to 3600 seconds" >&2; usage; }
num "$aborts" && [ "$aborts" -le 20 ] || { echo "$me: aborts=N is 0 to 20" >&2; usage; }
sleep_mode=${sleep_mode:-deep}
# Suspend-to-idle leaves suspend_enter() for its idle loop right after the
# platform level, before processors and core (kernel/power/suspend.c), and
# enter_state() refuses those two levels with it.
if [ "$sleep_mode" = s2idle ]; then
	case $pm_test in
	processors|core) echo "$me: pm_test=$pm_test with mode=s2idle: suspend-to-idle never gets there (it enters its idle loop after the devices' noirq phase, before the other CPUs would go offline), and the kernel refuses that level with it; use freezer, devices or platform" >&2; usage ;;
	esac
fi
case " $sw_set " in
*" pcie0 "*) [ -z "$sw_ret" ] || { echo "$me: gdsc=pcie0 and gdsc-ret=pcie0 contradict each other" >&2; usage; } ;;
esac
# root-port-d3 is for a sleep with the PCI devices kept, in which the host
# then powers itself down: not with pci=remove (no root port left), and not
# with pcie_0_gdsc kept in retention (the host powered down under it is the
# sleep that hung 8 times of 8), nor where the noirq phase is not reached.
if [ "$rpd3_given" = 1 ] && [ "$dry" = 0 ]; then
	[ "$pci_keep" = 1 ] || { echo "$me: root-port-d3 needs pci=keep: it is for a sleep with the PCI devices kept, which pci=keep watches" >&2; usage; }
	[ -z "$sw_ret" ] || { echo "$me: root-port-d3 with gdsc-ret=pcie0: the host would power down under pcie_0_gdsc kept on in retention, the sleep that never came back (8 of 8)" >&2; usage; }
	case $pm_test in
	freezer|devices) echo "$me: root-port-d3 with pm_test=$pm_test: the root port goes to D3hot in the noirq phase, which pm_test=$pm_test does not reach" >&2; usage ;;
	esac
fi
mode_words
# The experiment options in force, as every RESULT line names them.
options= trace_word=
[ "$sleep_mode" = deep ] || options="$options mode=s2idle"
[ "$pci_remove" = 0 ] || options="$options pci=remove"
[ "$pci_keep" = 0 ] || options="$options pci=keep"
[ -z "$pm_test" ] || options="$options pm_test=$pm_test"
case $trace in
on) trace_word=trace ;;
live) trace_word=trace=live ;;
esac
[ -z "$trace_word" ] || options="$options $trace_word"
# The debug switches asked for, in sw_names' order: " pcie0 usb cxmx".
sw_asked=$sw_set sw_set= gdsc_word=
for swn in $sw_names; do
	case " $sw_asked " in *" $swn "*) ;; *) continue ;; esac
	sw_set="$sw_set $swn"
	case $swn in cxmx|rpd3) ;; *) gdsc_word="$gdsc_word,$swn" ;; esac
done
[ -z "$gdsc_word" ] || options="$options gdsc=${gdsc_word#,}"
[ -z "$sw_ret" ] || options="$options gdsc-ret=$sw_ret"
[ "$cxmx_given" = 0 ] || options="$options cxmx=ret"
[ "$rpd3_given" = 0 ] || options="$options root-port-d3"
[ "$rpmh" = 0 ] || options="$options rpmh"
[ "$sensors_stop" = 0 ] || options="$options $sensors_opt"
options=${options# }
options=${options:-none}

# The audio DSP's clients in userspace, by their units' state: "iio-sensor-proxy
# active, hexagonrpcd-adsp-sensorspd active, hexagonrpcd-adsp-rootpd inactive".
adsp_units() {
	au=
	for aun in iio-sensor-proxy hexagonrpcd-adsp-sensorspd hexagonrpcd-adsp-rootpd; do
		auv=$(systemctl is-active "$aun.service" 2>/dev/null)
		au="$au, $aun ${auv:-unknown}"
	done
	echo "${au#, }"
}

# printf, not echo: dash's echo would read the backslash of an escaped
# mount point (\040) as an escape.
refuse() { printf '%s\n' "$me: REFUSED: $*; nothing was changed" >&2; exit 1; }

[ "$(id -u)" = 0 ] || refuse 'run it with sudo'
# The SM-X710 only: see the header.
tr '\0' '\n' < /proc/device-tree/compatible 2>/dev/null | grep -qx "$board" ||
	refuse "this is not an SM-X710 ($board); the SM-X910 has scripts/diagnostics/kernel10-ramroot-test"
[ "$confirmed" = 1 ] ||
	refuse 'the run ends with a restart of the tablet in any case: add restart-ok to say that is fine'
if [ "$sleep_mode" = s2idle ]; then
	# rtcwake -m freeze writes freeze to /sys/power/state; mem_sleep plays no
	# part.  The CPUs need a cpuidle state with an s2idle entry
	# (enter_s2idle): without one they would idle as usual, and the run's
	# s2idle counts would say nothing.
	grep -qw freeze /sys/power/state 2>/dev/null ||
		refuse "mode=s2idle: /sys/power/state offers no freeze ($(cat /sys/power/state 2>/dev/null))"
	[ -n "$(s2idle_states 0)" ] ||
		refuse "mode=s2idle: no cpuidle state of cpu0 has an s2idle entry ($cpu_devs/cpu0/cpuidle/state*/s2idle): the CPUs would not enter suspend-to-idle's own state"
else
	grep -qw deep /sys/power/mem_sleep 2>/dev/null ||
		refuse "/sys/power/mem_sleep offers no deep ($(cat /sys/power/mem_sleep 2>/dev/null))"
	grep -qw mem /sys/power/state 2>/dev/null || refuse '/sys/power/state offers no mem'
fi
if [ -e /sys/power/pm_test ]; then
	[ "$(selected /sys/power/pm_test)" = none ] || refuse "a pm_test level is selected ($(cat /sys/power/pm_test))"
fi
# The experiment options (the header): what each needs, checked before
# anything is done.
if [ -n "$pm_test" ]; then
	[ -w "$pm_test_ctl" ] || refuse "pm_test=$pm_test: no $pm_test_ctl (a kernel without CONFIG_PM_DEBUG)"
	tr -d '[]' < "$pm_test_ctl" | tr ' ' '\n' | grep -qx "$pm_test" ||
		refuse "pm_test=$pm_test: this kernel does not offer that level ($pm_test_ctl: $(cat "$pm_test_ctl"))"
fi
if [ -n "$trace" ]; then
	set -- $(cat "$printk_ctl" 2>/dev/null)
	[ $# = 4 ] && num "$1" && num "$2" && num "$3" && num "$4" ||
		refuse "$trace_word: cannot read the console loglevel in $printk_ctl"
	[ -e "$debug_messages_ctl" ] ||
		refuse "$trace_word: no $debug_messages_ctl (a kernel without CONFIG_PM_SLEEP_DEBUG): the PM core would print none of its phase lines"
	[ "$(cat "$print_times_ctl" 2>/dev/null)" != 1 ] ||
		refuse "$trace_word: $print_times_ctl is 1: its two lines per device callback, thousands per sleep, would go through the consoles at 0.1 to 0.25 s each with interrupts off (echo 0 | sudo tee $print_times_ctl)"
	if [ "$trace" = live ]; then
		case $(cat "$console_suspend_ctl" 2>/dev/null) in
		Y|N) ;;
		*) refuse "$trace_word: cannot read $console_suspend_ctl" ;;
		esac
	fi
fi
if [ "$pci_remove" = 1 ]; then
	pci_roots=$(pci_root_ports | awk '{ printf " %s", $0 }')
	pci_list=$(pci_names)
	[ -n "$pci_roots" ] ||
		refuse "pci=remove: no PCI root port to remove ($(pci_count) PCI devices${pci_list:+:$pci_list})"
	pci_left=$(pci_outside "$pci_roots")
	[ -z "$pci_left" ] ||
		refuse "pci=remove:$pci_left not below a PCI root port (${pci_roots# }): removing the root ports would not empty the bus"
	for pr in $pci_roots; do
		[ -e "$pci_devs/$pr/remove" ] || refuse "pci=remove: no $pci_devs/$pr/remove"
	done
fi
# gdsc= and cxmx= (the header): a GDSC switch only with pci=remove, each
# switch asked for offered by this kernel, and none of those it offers on
# already, whether asked for or not: the run sets them itself and records
# what it set.
if [ -n "$gdsc_word" ] && [ "$pci_remove" = 0 ]; then
	refuse "gdsc=${gdsc_word#,} needs pci=remove: with the PCI devices kept the PCIe host stays up through the sleep, and a PCIe GDSC that collapses (OFF_ON) takes the controller away from a live host; the GDSC switches are for runs with pci=remove"
fi
for swn in $sw_set $sw_ret; do
	swf=$(sw_file "$swn")
	[ -e "$swf" ] && [ -w "$swf" ] ||
		refuse "this kernel does not offer the debug switch $swn ($swf): the kernels of kit revisions 9 to 11 have it, revision 11's alone the root port's (sh ~/typec-roles/verify.sh says which kernel runs)"
done
# Each switch as it booted (sw_boot): N, but pcie0 Y on revision 11's
# kernel.  One that reads otherwise was written since the boot.
swon= swset=
for swn in $sw_names; do
	swf=$(sw_file "$swn")
	[ -e "$swf" ] || continue
	swv=$(cat "$swf" 2>/dev/null)
	swb=$(sw_boot "$swn")
	[ "$swv" = "$swb" ] || swon="$swon $swn ($swf: ${swv:-unreadable})"
	[ "$swb" = N ] || swset="$swset $swn $swb"
done
if [ -n "$swon" ]; then
	if [ -z "$swset" ]; then
		refuse "a debug switch is on already:$swon; a run sets the ones it is given itself, right before the freeze, and records what it set: put it back (echo N | sudo tee FILE) or restart the tablet"
	fi
	refuse "a debug switch does not read as this kernel booted it:$swon (at boot:$swset, the others N; $sw_boot_file); a run sets the ones it is given itself, right before the freeze, and records what it set: put it back (echo Y or N | sudo tee FILE) or restart the tablet"
fi
# Revision 11's kernel lets pcie_0_gdsc collapse in system suspend (its
# pcie0 switch booted Y), and its guard keeps it on while the PCIe host keeps
# its link; the host's line at each resume says what genpd did (the header,
# pci=keep).  The guard held this boot: at least one sleep logged its kept
# link with the domain on, and none the domain off under a kept link.
guard_state=none guard_n=0 guard_first=
guard_lines=$(dmesg 2>/dev/null | grep -E "$guard_re")
# The ring buffer may have dropped the boot's first lines: the journal has
# them (on the disk, which is writable still).
[ -n "$guard_lines" ] || guard_lines=$(journalctl -k -b --no-pager -q 2>/dev/null | grep -E "$guard_re")
if [ -n "$guard_lines" ]; then
	guard_n=$(printf '%s\n' "$guard_lines" | grep -cE "$guard_re")
	guard_first=$(printf '%s\n' "$guard_lines" | head -n 1 | sed 's/.*\.pcie: system sleep: //')
	if printf '%s\n' "$guard_lines" | grep -qE "$guard_bad_re"; then
		guard_state=failed
	elif printf '%s\n' "$guard_lines" | grep -qE "$guard_kept_re"; then
		guard_state=held
	else
		guard_state=unseen
	fi
fi
# A sleep with the PCI devices kept on a kernel that collapses
# pcie_0_gdsc, whose guard did not hold this boot (or which has none:
# revision 11 as first prepared), collapses it under the live host at the end
# of the noirq phase: refused unless the run removes them, keeps the GDSC in
# retention for the run, or says pci=keep.  dry and pm_test= levels before
# the noirq phase (freezer, devices) do not get there.
pcie0_off=N
[ ! -e "$(sw_file pcie0)" ] || pcie0_off=$(sw_boot pcie0)
case " $sw_set " in *" pcie0 "*) pcie0_off=Y ;; esac
[ -z "$sw_ret" ] || pcie0_off=N
case $pm_test in freezer|devices) pcie0_reached=0 ;; *) pcie0_reached=1 ;; esac
if [ "$dry" = 0 ] && [ "$pcie0_reached" = 1 ] && [ "$pcie0_off" = Y ] && [ "$pci_remove" = 0 ] && [ "$pci_keep" = 0 ] &&
	[ "$guard_state" != held ]; then
	case $guard_state in
	failed) guard_why="its guard did not hold this boot (a sleep logged \"the link was kept, but genpd powered its power domain off\")" ;;
	unseen) guard_why="its guard's line of a kept link with the domain on is not in this boot's kernel log (the first sleep logged: $guard_first)" ;;
	*) guard_why='it has no guard (no "system sleep:" line of the PCIe host in this boot'"'"'s kernel log: revision 11 as first prepared, or no suspend yet)' ;;
	esac
	refuse "pcie_0_gdsc collapses in this kernel's suspend (its debug switch pcie0 booted Y: revision 11), and $guard_why, so with the PCI devices kept genpd may collapse that GDSC under the live PCIe host: give pci=remove, or gdsc-ret=pcie0 to keep it in retention for the run, or pci=keep to run it so (the Wi-Fi may not come back, or the tablet may crash at the resume: README, \"Revision 11\")"
fi
if [ "$rpd3_given" = 1 ] && [ "$guard_state" != held ]; then
	refuse "root-port-d3 needs a guard that held this boot (a sleep that logged \"the link was kept, and its power domain stayed on\", none that logged it off): guard $guard_state${guard_first:+, the first sleep logged: $guard_first}"
fi
# Revision 10's second UFS PHY diagnostic logs a power-up write line before
# each calibration, the UFS probe's among them: on a kernel that logged one
# this boot (pwrdn_seen 1) a calibration entry without one before it had no
# PHY power-on (phy_leak_seen).  The kernel's own lines, from dmesg, or the
# journal when the ring buffer has dropped the boot's first lines.
pwrdn_seen=0
if dmesg 2>/dev/null | grep -v 'gts9u sleep-test: ' | grep -q 'power-up write: PWRDN' ||
	journalctl -k -b --no-pager -q 2>/dev/null | grep -v 'gts9u sleep-test: ' | grep -q 'power-up write: PWRDN'; then
	pwrdn_seen=1
fi
# sensors=hook: the device package's hook that stops the sensor clients
# before a sleep, or nothing to run.
if [ "$sensors_stop" = hook ]; then
	[ -r "$sensors_hook" ] && grep -q 'systemctl stop iio-sensor-proxy.service' "$sensors_hook" ||
		refuse "sensors=hook needs the device package's system-sleep hook that stops the sensor clients before a sleep ($sensors_hook, naming iio-sensor-proxy.service); this tablet's does not: sensors=stop stops them by hand"
fi
# rpmh: the tracepoint there and writable, and tracing on, or the trace
# would stay empty.  The command DB names the addresses; without it they
# stay numbers (the header says so).
rpmh_was= clk_trace=0 clk_was=
if [ "$rpmh" = 1 ]; then
	[ -e "$rpmh_event" ] && [ -w "$rpmh_event" ] ||
		refuse "rpmh needs the RPMh tracepoint ($rpmh_event): tracefs, and a kernel with CONFIG_FTRACE (the kit's kernels have both)"
	ton=$(cat "$trace_dir/tracing_on" 2>/dev/null)
	[ "$ton" = 1 ] ||
		refuse "rpmh: tracing is off ($trace_dir/tracing_on reads ${ton:-nothing}), so the trace would stay empty: echo 1 | sudo tee $trace_dir/tracing_on, or run without rpmh"
	# Its clock has to be the kernel log's (local, the default) for the
	# sleep to be found in it by the kernel's lines.
	tclk=
	[ ! -e "$trace_dir/trace_clock" ] || tclk=$(selected "$trace_dir/trace_clock")
	[ "$tclk" = local ] ||
		refuse "rpmh: the trace clock is ${tclk:-unknown} ($trace_dir/trace_clock), not local, the kernel log's: echo local | sudo tee $trace_dir/trace_clock, or run without rpmh"
	rpmh_was=$(cat "$rpmh_event" 2>/dev/null)
	# The clock tracepoints, when the kernel has both writable (no refusal
	# without them: the XO line then says there is no clock trace).
	clk_trace=1 clk_was=
	for evf in $clk_events; do
		if [ -e "$evf" ] && [ -w "$evf" ]; then
			clk_was="$clk_was $(cat "$evf" 2>/dev/null)"
		else
			clk_trace=0
		fi
	done
	clk_was=${clk_was# }
fi
# The first resume with a filesystem frozen from userspace logs one kernel
# warning (fs/super.c:1919, may_unfreeze: the suspend's own thaw finds no
# freeze of its own), and a writer that waits on the frozen disk for
# hung_task_timeout_secs is reported as a hung task: neither may panic the
# tablet in the middle of a run.
for p in panic_on_warn hung_task_panic; do
	pv=$(cat "/proc/sys/kernel/$p" 2>/dev/null)
	[ "$pv" = 0 ] ||
		refuse "kernel.$p is ${pv:-unreadable}, not 0: a frozen disk gives a kernel warning at the first resume and hung tasks after a minute, and either would panic the tablet in the middle of the run (sudo sysctl kernel.$p=0)"
done
[ -e "/sys/class/rtc/$rtc/wakealarm" ] || refuse "no /sys/class/rtc/$rtc/wakealarm: no RTC to wake the tablet"
[ "$(cat "/sys/class/rtc/$rtc/device/power/wakeup" 2>/dev/null)" = enabled ] ||
	refuse "$rtc cannot wake the tablet (its power/wakeup is not enabled)"
[ -w /proc/sysrq-trigger ] || refuse 'no /proc/sysrq-trigger'
[ -b "$part" ] || refuse "no block device $part"
dev=$(readlink -f "$part")
name=${dev##*/}
[ -f "/sys/class/block/$name/partition" ] || refuse "$dev is not a partition"
disk=$(readlink -f "/sys/class/block/$name")
disk=${disk%/*}
disk=${disk##*/}
case $disk in sd[a-z]|sd[a-z][a-z]) ;; *) refuse "the disk of $dev is '$disk', not a UFS LUN" ;; esac
[ "$(cat "/sys/class/block/$disk/ro" 2>/dev/null)" = 0 ] ||
	refuse "/dev/$disk is not writable at the block layer (a run that was not restarted?): restart the tablet first"
# The SCSI host of the UFS (0 in "sd 0:0:0:0: ..."): its lines are about the
# storage.  Any host's, when it cannot be read.
ufs_host=$(readlink -f "/sys/class/block/$disk/device" 2>/dev/null)
ufs_host=${ufs_host##*/}
ufs_host=${ufs_host%%:*}
num "$ufs_host" || ufs_host='[0-9]+'
# A root that ext4 gave up on keeps "rw" among its options and adds one of
# these: a failed run's tablet, not restarted yet.
root_opts=$(findmnt -no OPTIONS /)
echo "$root_opts" | tr ',' '\n' | grep -qx rw &&
	! echo "$root_opts" | tr ',' '\n' | grep -qxE 'emergency_ro|shutdown' ||
	refuse "the root filesystem is not writable ($root_opts): restart the tablet first"
# The root as ext4 records it: no error, and clean.  With an error recorded
# and deleted files held open, a boot does not mount it (the header).
rootsrc=$(awk '$2 == "/" { s = $1; t = $3 } END { print s, t }' /proc/1/mounts)
roottype=${rootsrc#* } rootsrc=${rootsrc%% *}
[ "$roottype" = ext4 ] || refuse "the root filesystem is '$roottype', not ext4"
rootname=$(dev_name "$rootsrc")
rooterr=$(ext4_errors "$rootname")
num "$rooterr" || refuse "cannot read ext4's error count of the root (/sys/fs/ext4/$rootname/errors_count)"
[ "$rooterr" = 0 ] ||
	refuse "ext4 has recorded $rooterr errors on the root filesystem $rootsrc (the last in $(ext4_last_error "$rootname")): it needs e2fsck -f from recovery first"
command -v dumpe2fs > /dev/null 2>&1 || refuse 'missing tool: dumpe2fs'
rootstate=$(dumpe2fs -h "$rootsrc" 2>/dev/null | sed -n 's/^Filesystem state: *//p')
[ "$rootstate" = clean ] ||
	refuse "the root filesystem $rootsrc is '${rootstate:-unreadable}', not clean (dumpe2fs -h): it needs e2fsck -f from recovery first"
# Every writable disk filesystem gets frozen, and only ext4 is: anything
# else has to be unmounted first (exfat, the SD card's, has no freeze).
nofreeze=$(rw_disk_mounts | awk -v ok="$freeze_types" 'index(ok, " " $3 " ") == 0 { print; exit }')
if [ -n "$nofreeze" ]; then
	set -- $nofreeze
	refuse "$2 ($1, $3) is mounted writable and cannot be frozen: unmount it first (sudo umount $2)"
fi
# /proc/1/mounts escapes a space, a tab, a newline and a backslash in a
# mount point (\040 and the like): the run could not name it.
escaped=$(rw_disk_mounts | awk 'index($2, "\\") { print $2; exit }')
[ -z "$escaped" ] ||
	refuse "the mount point $escaped has a space or the like in its path, which the run cannot name: unmount it first"
# ext4's error behaviour on each: the errors= of its options, else its
# superblock's default (dumpe2fs -h, "Errors behavior").
sb_errors() { # SOURCE
	case $(dumpe2fs -h "$1" 2>/dev/null | sed -n 's/^Errors behavior: *//p') in
	Continue) echo continue ;;
	'Remount read-only') echo remount-ro ;;
	Panic) echo panic ;;
	*) echo '?' ;;
	esac
}
# The ext4 filesystems the run freezes, each once, in freeze_order: SOURCE
# MOUNTPOINT COUNT NAME MODE SBDEFAULT (ext4's error count now, the
# device's kernel name, the error behaviour it runs with, its superblock's
# default).
fs_targets() {
	tseen=' '
	freeze_order "$rootsrc" | while read -r tsrc tmp ttype; do
		[ "$ttype" = ext4 ] || continue
		case $tseen in *" $tsrc "*) continue ;; esac
		tseen="$tseen$tsrc "
		tname=$(dev_name "$tsrc")
		tdef=$(sb_errors "$tsrc")
		tword=$(errors_word "$tsrc")
		printf '%s %s %s %s %s %s\n' "$tsrc" "$tmp" "$(ext4_errors "$tname")" "$tname" "${tword:-$tdef}" "$tdef"
	done
}
targets=$(fs_targets)
panicky=$(printf '%s\n' "$targets" | awk '$5 == "panic" { print $2, $1; exit }')
[ -z "$panicky" ] ||
	refuse "${panicky% *} (${panicky#* }) runs with errors=panic: an ext4 error during the run would panic the tablet; give it errors=remount-ro first (sudo mount -o remount,errors=remount-ro ${panicky% *})"
# The default is what the run's check of errors=continue needs to know:
# ext4 shows errors= only when it differs from it.
unknown_mode=$(printf '%s\n' "$targets" | awk 'NF && ($6 == "?" || ($5 != "continue" && $5 != "remount-ro")) { print $2, $1; exit }')
[ -z "$unknown_mode" ] ||
	refuse "cannot tell ext4's error behaviour of ${unknown_mode% *} (${unknown_mode#* }): dumpe2fs -h names no default in its superblock, or its options an unknown one"
cap=$(cat /sys/class/power_supply/*battery*/capacity 2>/dev/null | head -n 1)
num "$cap" && [ "$cap" -ge 30 ] || refuse "battery at ${cap:-unknown} %; charge to 30 % first"
up=$(cut -d. -f1 /proc/uptime)
num "$up" && [ "$up" -ge 120 ] || refuse "up ${up}s: wait until the boot has settled (120 s)"
case $(systemctl is-active ubuntu-gts9u-panel-coldboot-recover.service 2>/dev/null) in
activating|deactivating|reloading) refuse 'the boot-time panel cycle is still running' ;;
esac
case $(systemctl is-active ubuntu-gts9u-adsp-start-inhibit.service 2>/dev/null) in
active|activating) refuse 'the audio DSP is still starting (ubuntu-gts9u-adsp-start-inhibit holds sleep off)' ;;
esac
for t in $ram_tools ldd od cp ln mount unshare chroot setsid flock findmnt systemctl swapoff dumpe2fs tail; do
	command -v "$t" >/dev/null 2>&1 || refuse "missing tool: $t"
done
mount_cmd=$(command -v mount)
exec 9> /run/gts9u-typec-roles.lock
flock -n 9 || refuse 'install.sh, rollback.sh or another sleep test is running'
floor=$(floor_state)
if [ "$release" = 1 ] && [ "$floor" != released ]; then
	[ ! -e "$icc_hold" ] ||
		refuse "$icc_hold exists: the floor is held on purpose; remove that file yourself (sudo rm $icc_hold) and run again, or run without release-floor"
	[ -x "$icc_helper" ] || refuse "no $icc_helper on this tablet: the floor cannot be released the documented way"
fi

# ---------------------------------------------- before: prepare the run ---
# From here a stop changes nothing on the disk but ~/typec-roles/logs; the
# rest (a tmpfs, swap off, the floor) goes with the next restart, and
# errors=continue, the last step here, is put back by any stop before a
# sleep.
stop() { printf '%s\n' "$me: STOPPED before freezing anything: $*; nothing on the disk changed but $logs${debug_note:+; $debug_note}" >&2; exit 1; }
stamp=$(date +%Y%m%d-%H%M%S)
ramdir=$base/$stamp
logs=$kit/logs/$stamp-sleep
# The run's start in the kernel log: what ext4 logs from here on is the
# run's to judge (ext4_check), the window from these checks to the first
# freeze included.
rmarker="gts9u sleep-test: $stamp start"
printf '%s\n' "$rmarker" >> /dev/kmsg 2>/dev/null
mkdir -p "$ramdir" || refuse "cannot create $ramdir"
mount -t tmpfs -o size=256m,mode=0755,nosuid,nodev gts9u-sleep-test "$ramdir" ||
	refuse "cannot mount a tmpfs on $ramdir"
ln -sfn "$stamp" "$base/latest"
mkdir -p "$ramdir/tools" "$ramdir/tz" "$logs" || stop "cannot create $ramdir/tools or $logs"
: > "$ramdir/log"
: > "$ramdir/copied"
own() { [ -z "${SUDO_UID:-}" ] || chown -R "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" "$kit/logs"; }
# ext4's state as the checks above read it: the error counts the run
# compares with (errors-before), and the error behaviour of each
# (targets: SOURCE MOUNTPOINT MODE SBDEFAULT).
printf '%s\n' "$targets" | awk 'NF { print $1, $2, $3, $4 }' > "$ramdir/errors-before"
printf '%s\n' "$targets" | awk 'NF { print $1, $2, $5, $6 }' > "$ramdir/targets"
: > "$ramdir/errors-mode"

# The RAM root: the system's top-level symlinks (bin, lib, ... -> usr/...)
# first, then each file at the path it has on the disk.
for d in bin sbin lib lib32 lib64 libx32; do
	[ -L "/$d" ] || continue
	ln -s "$(readlink "/$d")" "$ramdir/$d" && mkdir -p "$ramdir/$(readlink -f "/$d")" ||
		stop "cannot make /$d in $ramdir"
done
# Put the file at PATH into the RAM root at the path it resolves to, and
# PATH itself there as a symlink to it if it is another path.  Hard links of
# one file (Ubuntu 26.04's coreutils are one program) share one copy.
root_copy() { # PATH
	src=$(readlink -f "$1") && [ -f "$src" ] || { echo "$1 not found"; return 1; }
	if [ ! -e "$ramdir$src" ]; then
		mkdir -p "$ramdir${src%/*}" || return 1
		linked=
		while read -r prev; do
			[ "$src" -ef "$prev" ] || continue
			ln "$ramdir$prev" "$ramdir$src" || return 1
			linked=1
			break
		done < "$ramdir/copied"
		[ -n "$linked" ] || cp "$src" "$ramdir$src" || return 1
		echo "$src" >> "$ramdir/copied"
	fi
	if [ "$1" != "$src" ] && [ ! -e "$ramdir$1" ]; then
		mkdir -p "$ramdir${1%/*}" && ln -s "$src" "$ramdir$1" || return 1
	fi
}
# A tool NAME (the executable at PATH) in the RAM root with every library ldd
# lists and the dynamic loader, at their own paths, and in its tools
# directory under NAME: a relative symlink, so that the tool runs at its own
# path and by its own name.
ram_tool() { # NAME PATH
	root_copy "$2" || return 1
	tsrc=$(readlink -f "$2")
	[ "$(od -An -tx1 -N4 "$tsrc" | tr -d ' \n')" = 7f454c46 ] || { echo "$2 is not an ELF executable"; return 1; }
	libs=$(ldd "$tsrc" 2>&1)
	case $libs in
	*'not a dynamic executable'*|*'statically linked'*) ;;
	*'not found'*) echo "$2: $(printf '%s\n' "$libs" | grep 'not found' | head -n 1)"; return 1 ;;
	*)
		for l in $(printf '%s\n' "$libs" | awk '$2 == "=>" && $3 ~ /^\// {print $3} $1 ~ /^\// && $2 != "=>" {print $1}'); do
			root_copy "$l" || return 1
		done ;;
	esac
	ln -sfn "..$tsrc" "$ramdir/tools/$1"
}
ram_tool sh /bin/sh > "$ramdir/copy.err" || stop "cannot copy /bin/sh to RAM: $(cat "$ramdir/copy.err")"
for t in $ram_tools; do
	ram_tool "$t" "$(command -v "$t")" > "$ramdir/copy.err" || stop "cannot copy $t to RAM: $(cat "$ramdir/copy.err")"
done
cp "$0" "$ramdir/sleep-test.sh" || stop 'cannot copy this script to RAM'
# Run CMD in a mount namespace of its own whose root is the RAM root, with
# /run, /proc, /sys and /dev bound in (/run first: the RAM root is in it).
# Nothing of the disk is in reach there.  ram_prefix=setsid: in a session
# of its own as well.
ram_prefix=
in_ram_root() { # CMD [ARGS]
	$ram_prefix unshare -m --propagation private /bin/sh -c '
		r=$1
		shift
		for d in run proc sys dev; do
			mkdir -p "$r/$d" && mount --rbind "/$d" "$r/$d" || exit 125
		done
		exec chroot "$r" "$@"' sh "$ramdir" "$@"
}
# Each tool runs from the RAM root while the disk is still writable, or
# nothing is frozen.
in_ram_root "$ramdir/tools/sh" -c '
	PATH=$1/tools
	sleep 0 && sync && [ "$(awk "BEGIN { print 7 }")" = 7 ] && [ "$(grep -c x /dev/null)" = 0 ] &&
	[ "$(sha256sum < /dev/null)" = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  -" ] &&
	dd if=/dev/zero of=/dev/null bs=1 count=1 status=none && dmesg > /dev/null &&
	rtcwake --version > /dev/null && blockdev --getro "/dev/$2" > /dev/null &&
	fsfreeze --version > /dev/null && nsenter --version > /dev/null &&
	[ "$(readlink -f "$1/tools/..")" = "$(readlink -f "$1")" ]' \
	sh "$ramdir" "$disk" > "$ramdir/check.out" 2>&1 ||
	stop "a tool does not run from the RAM root: $(head -n 3 "$ramdir/check.out" | tr '\n' ' ')"

kernel=$(od -An -tx1 -v /sys/kernel/notes | tr -d ' \n' | grep -o '474e5500[0-9a-f]\{40\}' | cut -c9- | cut -c1-12)
kit_id=$(cut -c1-12 "$kit/BUILD_ID" 2>/dev/null)
# The knobs as they are now: the RAM part puts trace's back to these after
# each sleep.  kernel.printk's four numbers, tabs made spaces.
printk_was=$(cat "$printk_ctl" 2>/dev/null | tr -s '\t ' '  ')
suspend_was=$(cat "$console_suspend_ctl" 2>/dev/null)
pm_test_delay=$(cat "$pm_test_delay_ctl" 2>/dev/null)
num "$pm_test_delay" || pm_test_delay=5
# pm_debug_messages: a run with sleeps sets it to 1 below, for the PM core's
# lines in each cycle's kernel lines, and the restart at the end of the run
# is what puts it back; dry does not sleep and leaves it as it is.
debug_was=$(cat "$debug_messages_ctl" 2>/dev/null)
debug_plan= debug_note=
if [ "$dry" = 1 ]; then
	debug_plan=' (dry leaves it as it is)'
elif [ -n "$debug_was" ] && [ "$debug_was" != 1 ]; then
	debug_plan=' (this run sets 1; the restart at its end puts it back)'
fi
{
	if [ "$dry" = 1 ]; then
		echo "$me $stamp: dry: every disk filesystem frozen and no sleep, then a ${hold}s hold and a restart (sysrq b); options: $options"
	elif [ -n "$pm_test" ]; then
		echo "$me $stamp: $cycles pm_test=$pm_test cycles (rtcwake -m $rtc_mode -s $seconds; the suspend stops at $pm_test, waits ${pm_test_delay}s and comes back: $no_sleep_call) with every disk filesystem frozen (aborted attempts tried again: up to $aborts), then a ${hold}s hold and a restart (sysrq b); options: $options"
	elif [ "$sleep_mode" = s2idle ]; then
		echo "$me $stamp: $cycles suspend-to-idle cycles of ${seconds}s (rtcwake -m freeze) with every disk filesystem frozen (aborted attempts and early wakes tried again: up to $aborts), then a ${hold}s hold and a restart (sysrq b); options: $options"
	else
		echo "$me $stamp: $cycles deep cycles of ${seconds}s with every disk filesystem frozen (aborted attempts tried again: up to $aborts), then a ${hold}s hold and a restart (sysrq b); options: $options"
	fi
	echo "kernel: $(uname -r) $(uname -v), build ID $kernel; the kit's: ${kit_id:-none}, revision $(cat "$kit/REVISION" 2>/dev/null || echo '7 or earlier')$([ "$kernel" = "$kit_id" ] || echo ' (not running: a baseline run)')"
	echo "boot $(cat /proc/sys/kernel/random/boot_id), up $(cut -d' ' -f1 /proc/uptime)s"
	echo "userdata: $dev on /dev/$disk (SCSI host $ufs_host); the read: the first $read_count x $read_bs, O_DIRECT"
	echo "mem_sleep $(cat /sys/power/mem_sleep), suspend_stats success $(stat_of success) fail $(stat_of fail); freeze_filesystems $(cat /sys/power/freeze_filesystems 2>/dev/null || echo n/a), sync_on_suspend $(cat /sys/power/sync_on_suspend 2>/dev/null || echo n/a)"
	echo "qcom_stats: aosd $(count_of aosd) cxsd $(count_of cxsd) ddr $(count_of ddr)$([ -r "$qstats/aosd" ] || echo ' (not readable: the depth cannot be judged)')"
	[ "$sleep_mode" != s2idle ] ||
		echo "mode=s2idle: rtcwake -m freeze (/sys/power/state: $(cat /sys/power/state 2>/dev/null)), mem_sleep not used; $(s2idle_summary)"
	echo "interconnect floor: $floor; record: $(tr '\n' ' ' < "$icc_state" 2>/dev/null)"
	echo "  last release log line: $(tail -n 1 "$icc_log" 2>/dev/null)"
	[ ! -e "$icc_hold" ] || echo "  $icc_hold exists"
	echo "INT_MAX nodes: $(intmax_nodes)"
	echo "Type-C: $(roles), USB devices $(usb_count); charger: $(charger)"
	pci_list=$(pci_names)
	echo "PCI: $(pci_count) devices${pci_list:+:$pci_list}; root ports:$(pci_root_ports | awk '{ printf " %s", $0 } END { if (!NR) printf " none" }')$([ "$pci_remove" = 0 ] || echo '; pci=remove: all of them go right before the freeze')"
	echo "printk: kernel.printk ${printk_was:-n/a}, console_suspend ${suspend_was:-n/a}, ignore_loglevel $(cat "$ignore_loglevel_ctl" 2>/dev/null || echo n/a); pm_debug_messages ${debug_was:-n/a}$debug_plan, pm_print_times $(cat "$print_times_ctl" 2>/dev/null || echo n/a); pm_test [$(selected "$pm_test_ctl")], pm_test_delay ${pm_test_delay}s"
	echo "debug $(sw_now) ($sw_gcc/debug_pcie0_off, debug_pcie0_phy_off, debug_usb_off; $sw_rpmhpd/debug_ret_floor; $sw_pcie/debug_root_port_d3)$([ -z "$sw_set" ] || echo "; this run sets$sw_set to Y right before the freeze")$([ -z "$sw_ret" ] || echo "; this run sets $sw_ret to N right before the freeze")"
	[ ! -e "$sw_boot_file" ] || echo "debug switches at boot: $(cat "$sw_boot_file" 2>/dev/null) ($sw_boot_file); pcie_0_gdsc in this run's sleeps: $([ "$pcie0_off" = Y ] && echo 'OFF_ON, collapsed when the PCIe host has powered down' || echo 'RET_ON, left on')"
	case $guard_state in
	held) echo "PCIe power domain guard: held this boot ($guard_n sleeps logged, none with the domain off under a kept link; the first: $guard_first)" ;;
	failed) echo "PCIe power domain guard: did NOT hold this boot ($guard_n sleeps logged; at least one: the link was kept, but genpd powered its power domain off)" ;;
	unseen) echo "PCIe power domain guard: no sleep this boot logged a kept link with the domain on ($guard_n sleeps logged; the first: $guard_first)" ;;
	*) echo 'PCIe power domain guard: no "system sleep:" line of the PCIe host this boot (a kernel without the guard, or no suspend yet)' ;;
	esac
	# What may keep the audio DSP awake or the clocks and rails on: its
	# clients in userspace, the sound streams open, the boot's ignore_unused
	# options (reads only).
	pw=0
	for pc in /proc/[0-9]*/comm; do
		pn=
		read -r pn 2>/dev/null < "$pc" || continue
		case $pn in pipewire*|wireplumber) pw=$((pw + 1)) ;; esac
	done
	pcm=
	for ps in /proc/asound/card*/pcm*/sub*/status; do
		[ -r "$ps" ] || continue
		pst=
		read -r pst 2>/dev/null < "$ps" || :
		[ "$pst" = closed ] || pcm="$pcm ${ps#/proc/asound/}(${pst:-?})"
	done
	echo "ADSP clients: $(adsp_units); PipeWire and WirePlumber processes $pw; sound streams not closed:${pcm:- none}$([ "$sensors_stop" = 0 ] || echo "; $sensors_opt: this run stops $sensor_units next$([ "$sensors_stop" != hook ] || echo " through $sensors_hook pre suspend"), until the restart")"
	[ "$rpmh" = 0 ] || echo "rpmh: the tracepoint $rpmh_event (${rpmh_was:-?} now) goes on right before the freeze; command DB: $([ -r "$cmd_db" ] && echo "$cmd_db, $(grep -c '^0x' "$cmd_db" 2>/dev/null) addresses" || echo "$cmd_db not readable: the addresses stay numbers")"
	echo "kernel command line, the ignore_unused options:$({ tr ' ' '\n' < /proc/cmdline; } 2>/dev/null | grep -E '^(clk|pd|regulator)_ignore_unused$' | sed 's/^/ /' | tr -d '\n')"
	echo "root: $rootsrc ext4, ext4 errors_count $rooterr, state $rootstate; to freeze, the root last:$(freeze_order "$rootsrc" | awk '!seen[$1]++ { printf " %s", $2 }')"
	echo 'ext4, as the run found it (errors_count, error behaviour, the superblock'"'"'s default):'
	printf '%s\n' "$targets" |
		awk 'NF { printf "  %s (%s, %s): errors_count %s, errors=%s, default %s\n", $2, $1, $4, $3, $5, $6 }'
	echo 'disk mounts:'
	grep '^/dev/' /proc/1/mounts | sed 's/^/  /'
	echo 'swap:'
	sed 's/^/  /' /proc/swaps
	echo 'the RAM root:'
	sed 's/^/  /' "$ramdir/copied"
} > "$ramdir/before" 2>&1
cp "$ramdir/before" "$logs/before"
while IFS= read -r l; do say "$l"; done < "$ramdir/before"

# sensors=stop (the header): the audio DSP's sensor clients stopped for the
# run, the last change before the floor's release; one still running stops
# the run here, nothing frozen.
if [ "$sensors_stop" != 0 ]; then
	if [ "$sensors_stop" = hook ]; then
		say "sensors=hook: $sensors_hook pre suspend, as systemd-sleep runs it before a systemctl suspend (it stops $sensor_units; auto-rotation and auto-brightness are off until the restart, which runs no post)"
		sh "$sensors_hook" pre suspend >> "$ramdir/log" 2>&1
	else
		say "sensors=stop: systemctl stop $sensor_units (auto-rotation and auto-brightness are off until the restart)"
		systemctl stop $sensor_units >> "$ramdir/log" 2>&1
	fi
	sensors_rc=$?
	sensors_now=$(adsp_units)
	sensors_up=
	for sun in $sensor_units; do
		case $(systemctl is-active "$sun" 2>/dev/null) in
		active|activating|reloading|deactivating) sensors_up="$sensors_up $sun" ;;
		esac
	done
	debug_note="${debug_note:+$debug_note; }iio-sensor-proxy and hexagonrpcd-adsp-sensorspd stay stopped until the next boot (sudo reboot brings them back)"
	sensors_by='systemctl stop'
	[ "$sensors_stop" != hook ] || sensors_by='the hook'
	if [ -n "$sensors_up" ]; then
		cp "$ramdir/log" "$logs/log"
		own
		stop "$sensors_opt: still running after $sensors_by (status $sensors_rc):$sensors_up; ADSP clients now: $sensors_now"
	fi
	say "$sensors_opt: ADSP clients now: $sensors_now ($sensors_by status $sensors_rc); ${settle}s to settle"
	sleep "$settle"
fi

if [ "$floor" != released ]; then
	if [ "$release" = 1 ]; then
		say "interconnect floor $floor; releasing it: systemctl restart $icc_unit"
		systemctl restart "$icc_unit" >> "$ramdir/log" 2>&1
		floor=$(floor_state)
		if [ "$floor" != released ]; then
			say "interconnect floor still $floor: $(tr '\n' ' ' < "$icc_state" 2>/dev/null)"
			cp "$ramdir/log" "$logs/log"
			own
			stop "the floor could not be released ($(sed -n 's/^reason=//p' "$icc_state" 2>/dev/null))"
		fi
		say "interconnect floor released ($(tail -n 1 "$icc_log" 2>/dev/null)); 60 s to settle"
		sleep 60
		say "INT_MAX nodes now: $(intmax_nodes)"
	else
		say '=================================================================='
		say "THE INTERCONNECT FLOOR IS ${floor}: this run cannot reach the depth of the 2026-11-21 failure"
		[ ! -e "$icc_hold" ] || say "($icc_hold exists)"
		say 'every verdict says so; add release-floor to release it first'
		say '=================================================================='
	fi
fi
armed=0
[ "$(sed -n 's/^boot_id=//p' "$icc_pending" 2>/dev/null | head -n 1)" = "$(cat /proc/sys/kernel/random/boot_id)" ] &&
	armed=1
[ "$armed" = 0 ] ||
	say "the loop breaker is armed for this boot ($icc_pending): the boot after this run's restart holds the floor once"

# The sleep settings; swap off the disk.  mode=s2idle does not use
# mem_sleep (rtcwake -m freeze) and leaves it as it is.
if [ "$sleep_mode" = deep ]; then
	printf deep > /sys/power/mem_sleep 2>/dev/null
	[ "$(selected /sys/power/mem_sleep)" = deep ] || stop 'cannot select deep in /sys/power/mem_sleep'
fi
if [ "$dry" = 0 ] && [ -n "$debug_was" ] && [ "$debug_was" != 1 ]; then
	printf 1 > "$debug_messages_ctl" 2>/dev/null
	if [ "$(cat "$debug_messages_ctl" 2>/dev/null)" = 1 ]; then
		debug_note="${debug_note:+$debug_note; }pm_debug_messages stays 1 until the next boot (it was $debug_was)"
		say "pm_debug_messages 1 for this run (it was $debug_was), so that each cycle's kernel lines have the PM core's: the restart that ends the run puts it back; a stop before any sleep leaves it at 1 until the next boot"
	fi
fi
for s in $(disk_swaps); do
	say "swapoff $s"
	swapoff "$s" >> "$ramdir/log" 2>&1 || { cp "$ramdir/log" "$logs/log"; own; stop "swapoff $s failed"; }
done
[ -z "$(disk_swaps)" ] || stop "swap still on the disk:$(disk_swaps)"
echo "the rest of this run's log is in $ramdir/log, and its verdict lines in pstore after the restart" >> "$ramdir/log"
cp "$ramdir/log" "$logs/log"
own
sync

q() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
for v in stamp ramdir part disk cycles seconds hold aborts dry rootsrc rootname floor armed kernel mount_cmd ufs_host \
	pci_remove pci_keep pm_test trace options printk_was suspend_was pm_test_delay sleep_mode debug_note sw_set sw_ret \
	rpmh rpmh_was clk_trace clk_was guard_state sensors_stop pwrdn_seen; do
	eval "printf '%s=%s\n' $v \"\$(q \"\$$v\")\""
done > "$ramdir/run.conf"
say 'the run goes on from RAM in a session of its own: it ends with a restart even if this terminal goes away'
say "its result: sh $kit/sleep-test.sh status, or $ramdir/result"
say 'while the disk is frozen, programs that write to it wait (the desktop, the journal, a new SSH login): keep this SSH session, and read the result with status, which needs no sudo'
say 'do not touch the screen until the restart: a double tap on it wakes the tablet and aborts the sleep it is in'
if [ "$pci_remove" = 1 ]; then
	say "pci=remove: right before it freezes the disks the run removes the PCI root port(s)$pci_roots and everything below them, the Wi-Fi with them: Wi-Fi is gone from then until the restart, and an SSH session over Wi-Fi drops there while the run carries on; follow it over the USB cable (usb0), or read its result in pstore after the restart"
fi
if [ "$pci_keep" = 1 ]; then
	if [ "$rpd3_given" = 1 ]; then
		pk_host='the PCI devices stay, and with root-port-d3 the root port goes to D3hot after the Wi-Fi in each sleep, so that the PCIe host powers itself down and is set up again at the resume'
	else
		pk_host='the PCI devices stay and the PCIe host keeps its link through each sleep'
	fi
	if [ "$pcie0_off" != Y ]; then
		pk_gdsc='pcie_0_gdsc stays on in retention (RET_ON), as before revision 11'
	elif [ "$rpd3_given" = 1 ]; then
		pk_gdsc='pcie_0_gdsc collapses once the host is down (the guard marks it out-of-band)'
	elif [ "$guard_state" = held ]; then
		pk_gdsc='pcie_0_gdsc stays on under the kept link (the guard, which held this boot)'
	else
		pk_gdsc='pcie_0_gdsc may collapse under the kept link (OFF_ON, and no guard that held this boot): the Wi-Fi may not come back, or the tablet may crash at the resume and restart by itself'
	fi
	say "pci=keep: $pk_host; $pk_gdsc; a cycle whose kernel lines say the PCIe host or the Wi-Fi did not come back, or that the guard did not hold, ends the run (PCIE-DOWN); give it one cycle"
fi
if [ "$rpd3_given" = 1 ]; then
	say "root-port-d3: right before it freezes the disks the run writes Y to $(sw_file rpd3) and reads it back; it stays so until the restart that ends the run. First use of the switch: the root port, the Wi-Fi and the PCIe host come back through a host power-down, or the Wi-Fi is gone until the restart (PCIE-DOWN), or the tablet restarts by itself"
fi
if [ -n "$sw_ret" ]; then
	say "gdsc-ret=$sw_ret: right before it freezes the disks$([ "$pci_remove" = 0 ] || echo ', after the PCI removal,') the run writes N to its switch: pcie_0_gdsc stays on in retention in the sleeps, as on revision 10$([ "$pci_remove" = 0 ] || echo ', where every such sleep with the PCI devices removed and the floor released hung (8 of 8): a control run')"
fi
if [ -n "$sw_set" ]; then
	say "debug switches$sw_set: right before it freezes the disks$([ "$pci_remove" = 0 ] || echo ', after the PCI removal,') the run writes Y to each and reads it back; they stay so until the restart that ends the run, and every verdict says how they read"
	case " $sw_set " in
	*" usb "*)
		[ "$(selected /sys/class/typec/port0/data_role)" != host ] || [ ! -e /sys/class/typec/port0-partner ] ||
			say "gdsc=usb with the tablet as USB host: with its GDSCs collapsed in the sleep the xHCI is not expected to come back (the reason for the retention patch); run undocked" ;;
	esac
fi
if [ -n "$pm_test" ]; then
	if [ "$sleep_mode" = s2idle ]; then
		say "pm_test=$pm_test: each cycle's suspend-to-idle stops at $pm_test ($(pm_test_what "$pm_test")), waits ${pm_test_delay}s and comes back; no suspend-to-idle (no CPU enters its s2idle state), so the cycles test the way down and back, not a depth; /sys/power/pm_test is set right before each rtcwake and back to none right after"
	else
		say "pm_test=$pm_test: each cycle's suspend stops at $pm_test ($(pm_test_what "$pm_test")), waits ${pm_test_delay}s and comes back; no SoC sleep call (PSCI SYSTEM_SUSPEND), so the cycles test the way down and back, not a depth; /sys/power/pm_test is set right before each rtcwake and back to none right after"
	fi
fi
if [ "$sleep_mode" = s2idle ] && [ "$dry" = 0 ]; then
	say "mode=s2idle: each sleep is rtcwake -m freeze, suspend-to-idle: the CPUs stay online and idle in their s2idle state, the last one takes the CPU cluster's power domain down and enters PSCI CPU_SUSPEND; /sys/power/mem_sleep is not used and stays $(cat /sys/power/mem_sleep 2>/dev/null); a sleep a wakeup other than the RTC's alarm cut short is WOKEN EARLY and tried again, like an aborted attempt (aborts=$aborts)"
fi
if [ -n "$trace" ]; then
	say "$trace_word: for each sleep the console loglevel is $trace_level (kernel.printk $printk_was otherwise), so the PM core's lines reach the consoles and ramoops; each console line costs 0.1 to 0.25 s here with interrupts off, so a sleep takes up to half a minute longer to return"
	[ "$trace" != live ] ||
		say "trace=live: console_suspend N for each sleep (${suspend_was} otherwise): the lines reach ramoops while the devices suspend and resume, at that cost inside the suspend, and the debug UART stays powered through the sleep, which may keep the SoC from the state it reaches otherwise"
fi
# The last step on the disk: errors=continue on every filesystem the run
# freezes (the header), with nothing to stop this part between it and the
# run in RAM, which puts it back itself if it stops before a sleep.
trap '' HUP INT TERM
if ! errors_continue; then
	ec_why=$em_why
	if errors_back; then
		stop "$ec_why${back_done:+; errors= is back as it was on:$back_done}"
	fi
	stop "$ec_why; and $back_why"
fi
shown=$(grep -c '' "$ramdir/log")
ram_prefix=setsid
in_ram_root "$ramdir/tools/sh" "$ramdir/sleep-test.sh" __ram "$ramdir/run.conf" &
ram_pid=$!
trap - HUP INT TERM
# The run's lines, on this terminal while it lives: the run itself writes
# them to its log only.
tail -n "+$((shown + 1))" -f --pid="$ram_pid" "$ramdir/log" 2>/dev/null
wait "$ram_pid"
ram_rc=$?
# A run that never started in RAM froze nothing: errors= back from here.
if [ ! -e "$ramdir/started" ]; then
	if errors_back; then
		printf '%s\n' "$me: STOPPED: the run did not start from RAM (status $ram_rc): nothing was frozen${back_done:+; errors= is back as it was on:$back_done}" >&2
	else
		printf '%s\n' "$me: STOPPED: the run did not start from RAM (status $ram_rc): nothing was frozen; but $back_why" >&2
	fi
	exit 1
fi
exit "$ram_rc"
