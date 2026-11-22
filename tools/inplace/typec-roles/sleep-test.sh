#!/bin/sh
# Deep sleeps of the SM-X710 with every disk filesystem frozen, to test
# whether the UFS storage comes back, without risking the data on it.
#   sudo sh ~/typec-roles/sleep-test.sh ro [CYCLES] [SECONDS] [release-floor] [hold=S] [aborts=N] restart-ok
#   sudo sh ~/typec-roles/sleep-test.sh dry [release-floor] [hold=S] restart-ok
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
# screen for one) is tried again (default 5, at most 20); one more ends the
# run with NO-SLEEP, and aborts=0 ends it at the first.  Do not touch the
# screen during a run.
# status (no root) prints the latest run's result from /run.
#
# On 2026-11-21 one deep sleep (rtcwake -m mem -s 30) did not bring the UFS
# PHY back on this tablet: its calibration timed out, every reset of the UFS
# host failed, ext4 aborted its journal and the root went read-only.  A sleep
# test with the root writable can therefore cost data.  This one never sleeps
# with a disk filesystem writable, never writes to the disk after it froze
# them, and needs nothing from the disk after that:
#   - before anything else: root, this board, deep in mem_sleep, the RTC, the
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
#   - each cycle: rtcwake -m mem; right after it a second fsfreeze -f of
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
#     the power domains of the rails, the UFS, USB and PCIe, the runtime
#     state of the UFS host and of the USB controller, the DSPs, the panel's
#     backlight, the warmest thermal zone, and the cycle's kernel lines;
#     once before the first sleep, every power domain, the rails in use, the
#     bandwidth votes (those in the sleep set marked) and the PCIe
#     functions.  The terminal gets all of it ("evidence:" lines), pmsg two
#     short lines per cycle; a read that fails changes nothing;
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
ufs_diag='calibration entry: SW_RESET|after the PCS_READY timeout: SW_RESET|PCS_READY .*SERDES_START|1 s after SERDES_START: SW_RESET'
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
# between.  A resume whose calibration fails retries it 15 times, up to four
# lines each (the entry, the state after the timeout, the late poll's
# outcome, the state 1 s on); the terminal and the run's log in /run get
# every line, and those go with the restart.  pmsg is 128 KiB of ramoops
# (the hang-log kit's DTB), about 73 KiB of it data with ECC 96, and it is
# a ring: what goes there must leave room for the verdicts.  A cycle puts at
# most 17 diagnostic lines there, each cut at 300 characters; a passing one
# has only its entry lines, one or two.  Ten cycles, the last one failed,
# come to well under 20 KiB with their verdicts; an aborted attempt adds its
# entry lines and its verdict, about 1 KiB, and a run has at most 21 of them.
# ext4's own lines, when it recorded or logged an error, add at most 6.
# Each cycle that was not given up before its sleep adds two evidence lines
# (ev_keep), at most 700 bytes: 7 KiB for ten.
diag_head=12
diag_tail=4
# Evidence around each cycle (added after the r7 baselines of 2026-11-22,
# whose 13 cycles all resumed with aosd, cxsd and ddr at +0): how deep each
# sleep went and what may have kept the SoC out of AOSS sleep, CX collapse
# and DDR low power.  Reads only, of debugfs (not on the disk), sysfs, /proc
# and the kernel log, with the RAM root's tools; a read that fails gives "-"
# or nothing and changes nothing in the run.  The terminal and the run's log
# get every line ("cycle N/M: evidence: ..."), pmsg two short ones per cycle
# (ev_pmsg).  One read is not passive: ddr_stats asks the AOSS to bring its
# numbers up to date (a QMP message, qcom_ddr_stats_show in
# drivers/soc/qcom/qcom_stats.c), as every read of that file does.
genpd_sum=/sys/kernel/debug/pm_genpd/pm_genpd_summary
reg_sum=/sys/kernel/debug/regulator/regulator_summary
icc_sum=/sys/kernel/debug/interconnect/interconnect_summary
wake_sum=/sys/kernel/debug/wakeup_sources
ufs_ctl=/sys/bus/platform/devices/1d84000.ufshc
usb_ctl=/sys/bus/platform/devices/a600000.usb
pci_devs=/sys/bus/pci/devices
# The power domains each cycle shows (pm_genpd_summary): the rails, the
# UFS, USB and PCIe (Wi-Fi) domains, the display's and the CPU cluster's;
# any domain that is on as well.
ev_domains='^(cx|mx|mxc|mmcx|lcx|lmx|gfx|ebi|ufs_|usb|pcie|mdss|power-domain-cluster)'
# The kernel lines of a cycle the terminal gets (dmesg, all levels; the run
# sets pm_debug_messages): the PM core's, the CPUs', the storage's and the
# diagnostic's, the USB host's, the PCIe link's and the Wi-Fi's, the DSPs',
# the clocks', rails' and votes', warnings and errors.  At most ev_klines,
# and not the Wi-Fi's per-packet noise, the CPUs' feature lines or the
# hung-task reports of writers waiting on the frozen disk.
ev_kernel='PM: |psci|CPU[0-9]+( is up|: Booted| killed)|non-boot CPUs|Timekeeping|[Ww]ake|ufs|qmp|calibrat|SW_RESET|PCS_READY|SERDES|sd 0:|scsi|I/O error|EXT4|dwc3|xhci|usb [0-9]|typec|tcpm|PCIe|pcie|L2|LTSSM|mhi|ath11k|remoteproc|q6v5|smp2p|ipcc|glink|rpmh|gdsc|genpd|icc|interconnect|qcom_stats|aoss|regulator|clk|WARNING|BUG|Freezing|Restarting|Filesystems sync|fail|error|timed? ?out|-110'
ev_kernel_skip='msdu_done|SANITY CHECK|blocked for more than|hung_task_timeout_secs|Tainted:'
ev_klines=150
# How to reach TWRP from a running or looping tablet (docs/porting-log.md,
# the TWRP flash: Volume Down + Power, then at once Volume Up + Power).
recovery_keys='hold Power and Volume Down until the screen goes dark, then at once Power and Volume Up, for TWRP'

usage() {
	sed -n '2,20s/^# //p' "$0" >&2
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
	read -r sv < "/sys/power/suspend_stats/$1" 2>/dev/null
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
	awk 'NR <= 2 { next }
		NF >= 7 && $2 ~ /^[0-9]+$/ && $3 ~ /^[0-9]+$/ { show = $2 > 0; if (show) print; next }
		show && NF == 5 && $2 ~ /^[0-9]+$/ && $2 > 0 { print }' "$reg_sum" 2>/dev/null |
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
}

# Before a sleep: the snapshots ev_after compares with, and the domains.
ev_before() { # SFX
	ev_stats "$ramdir/ev-stats-$1.0"
	ev_irqs "$ramdir/ev-irqs-$1.0"
	ev_wake "$ramdir/ev-wake-$1.0"
	evstate=$(ev_state)
	say "$cyc: evidence: before: $evstate"
	ev_genpd domains | while IFS= read -r evl; do say "$cyc: evidence: before: genpd: $evl"; done
}

# Right after rtcwake returns: the same snapshots.
ev_resumed() { # SFX
	ev_stats "$ramdir/ev-stats-$1.1"
	ev_irqs "$ramdir/ev-irqs-$1.1"
	ev_wake "$ramdir/ev-wake-$1.1"
}

# Once the cycle's kernel lines are in K: what the sleep reached, what woke
# or kept it, whether the PCIe link went down and came back, the domains
# now, and the cycle's kernel lines.  ev_keep then puts two short lines of
# it in pmsg, for an attempt that was not given up before the sleep.
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
	say "$cyc: evidence: qcom_stats: $evdepth"
	say "$cyc: evidence: wakeup sources: $evwake"
	say "$cyc: evidence: interrupts: $evirq"
	say "$cyc: evidence: pcie: $evlink"
	ev_genpd domains | while IFS= read -r evl; do say "$cyc: evidence: after: genpd: $evl"; done
	awk -v re="$ev_kernel" -v skip="$ev_kernel_skip" -v max="$ev_klines" '
		$0 ~ skip { next }
		$0 ~ re { if (++n <= max) print; next }
		END { if (n > max) printf "(%d more kernel lines of this cycle left out; its kmsg file in the run directory has them all)\n", n - max }' "$2" 2>/dev/null |
		while IFS= read -r evl; do say "$cyc: evidence: kernel: $evl"; done
}

ev_keep() { # SFX
	ev_pmsg "$cyc: evidence: qcom_stats: $evdepth"
	ev_pmsg "$cyc: evidence: $evpcie; irq $(ev_irq_delta "$ramdir/ev-irqs-$1.0" "$ramdir/ev-irqs-$1.1" 3); wakeup $evwake; before: $evstate"
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

# Was this attempt a suspend that the kernel gave up before the tablet
# slept, with the storage as it was before?  Only if every one of these
# holds; anything else, or anything that cannot be read, leaves it a FAIL:
#   1. rtcwake failed (its write to /sys/power/state did);
#   2. suspend_stats: success +0 and fail +1, the kernel's own word that
#      this suspend failed (a sleep that happened counts as a success,
#      however badly it resumed);
#   3. the attempt's kernel lines have "PM: suspend entry (deep)" and
#      "PM: suspend exit";
#   4. and a line that shows the suspend given up on the way down
#      (abort_re: a pending wakeup, a wakeup IRQ, a device that refused);
#   5. and less than 1 s of "Timekeeping suspended for" (a real sleep of
#      SECONDS logs about that);
#   6. no failure line (fail_re: the UFS and its PHY, I/O and ext4 errors,
#      a write the block layer logged), and no line of the diagnostic but
#      its calibration entries (the others come with a timeout);
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
	grep -q 'PM: suspend entry (deep)' "$k" && grep -q 'PM: suspend exit' "$k" || return 1
	if [ -n "$asleep" ]; then
		awk -v s="$asleep" 'BEGIN { exit !(s + 0 < 1) }' || return 1
	fi
	[ -z "$prot_why" ] || return 1
	[ "$storage" = healthy ] && [ "${fails:-0}" = 0 ] || return 1
	! grep -v 'calibration entry: SW_RESET' "$ramdir/diag-$sfx" | grep -q . || return 1
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
# 5 times, IRQ 37 (pwrkey) once)".
abort_note() {
	[ "$aborted" != 0 ] || return 0
	awk -v n="$aborted" '{ if (!($0 in c)) o[++k] = $0; c[$0]++ }
	END {
		printf "%d attempt%s aborted before the sleep (", n, n == 1 ? "" : "s"
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
	if [ "$freeze_hung" = 1 ]; then
		verdict "SLEEP-TEST RESULT: STOPPED before any sleep: $1, and cannot be undone: restarting (sysrq b) with what this run froze still frozen; floor $floor; kernel $kernel"
		verdict "$2"
		finish healthy
	fi
	if [ "$setro" = 1 ] && ! blockdev --setrw "/dev/$disk" >> "$ramdir/log" 2>&1; then
		verdict "SLEEP-TEST RESULT: STOPPED before any sleep: $1; and blockdev --setrw /dev/$disk failed, so nothing is thawed: restarting (sysrq b); floor $floor; kernel $kernel"
		verdict "$2"
		finish healthy
	fi
	if thaw_all; then
		if errors_back; then
			eb="every filesystem this run froze is thawed again${back_done:+, errors= is back as it was on:$back_done}, and the tablet is not restarted"
		else
			eb="every filesystem this run froze is thawed again, and the tablet is not restarted; but $back_why"
		fi
		verdict "SLEEP-TEST RESULT: STOPPED before any sleep: $1; $eb; floor $floor; kernel $kernel"
		verdict "$2"
		[ -z "$back_why" ] ||
			verdict 'NEXT: put errors= back by hand as said above, or restart the tablet (sudo reboot): the boot mounts every filesystem with its usual options'
		say "stopped: swap on the disk stays off until the next boot"
		exit 1
	fi
	verdict "SLEEP-TEST RESULT: STOPPED before any sleep: $1; and $thaw_why: restarting (sysrq b), a filesystem stays frozen; floor $floor; kernel $kernel"
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
	setro=0 freeze_hung=0 prot_why= prot_bad= ext4_said= slept=0
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
	# The state the sleeps start from (reads only), before the first read so
	# that the first sleep follows that read as closely as before.
	[ "$dry" = 1 ] || ev_once
	read_check 0
	if [ -z "$read_hash" ]; then
		verdict "SLEEP-TEST RESULT: FAIL before any sleep: the first read of $part failed ($read_why); floor $floor; kernel $kernel"
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
			verdict "SLEEP-TEST RESULT: FAIL (dry, no sleep tried): $why; floor $floor; kernel $kernel"
			finish failed
		fi
		verdict "SLEEP-TEST RESULT: DRY: no sleep tried; frozen:$frozen_list; /dev/$disk read-only at the block layer; a second freeze found each still frozen; the UFS read back as before after ${settle}s; no failure line in the kernel log, and ext4 recorded, logged and gave up on nothing (looked at again right before the restart); floor $floor; kernel $kernel"
		verdict 'NEXT: the boot after this restart (sysrq b, everything frozen) must come up as after a power loss; sh ~/typec-roles/verify.sh then says so (it knows the boot after a sleep test); only then a run with sleeps'
		finish healthy
	fi

	# Before the first sleep: no failure line in the kernel log since the run
	# began (a write the block layer refused, a journal started on a frozen
	# ext4, a UFS or I/O error).
	run_log > "$ramdir/kmsg-0"
	fails=$(grep -cE "$(fail_re)" "$ramdir/kmsg-0")
	if [ "${fails:-0}" != 0 ]; then
		verdict "SLEEP-TEST RESULT: FAIL before any sleep: $fails failure lines in the kernel log since the run began, the first: $(grep -E "$(fail_re)" "$ramdir/kmsg-0" | awk '{sub(/^\[[^]]*\] /, ""); print; exit}'); floor $floor; kernel $kernel"
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
		[ "$(selected /sys/power/mem_sleep)" = deep ] || printf deep > /sys/power/mem_sleep 2>/dev/null
		ro=$(selected "/sys/class/block/$disk/ro")
		if [ -n "$rw" ] || [ -n "$sw" ] || [ "$ro" != 1 ] || [ "$(selected /sys/power/mem_sleep)" != deep ]; then
			why="writable and not frozen:${rw:- none}; disk swap:${sw:- none}; /dev/$disk ro=${ro:-?}; mem_sleep $(selected /sys/power/mem_sleep)"
			verdict "SLEEP-TEST $cyc: NOT STARTED: $why"
			an=$(abort_note)
			verdict "SLEEP-TEST RESULT: ABORTED at $cyc, before its sleep ($why); $passes passed before it${an:+; $an and tried again}; floor $floor; kernel $kernel"
			finish healthy
		fi
		ev_before "$sfx"
		ok0=$(stat_of success) fail0=$(stat_of fail)
		a0=$(count_of aosd) x0=$(count_of cxsd) d0=$(count_of ddr)
		# Each attempt's marker is its own: the kernel lines after it are
		# that attempt's alone.
		marker="gts9u sleep-test: $stamp cycle $i"
		[ "$try" = 1 ] || marker="$marker attempt $try"
		printf '%s\n' "$marker" >> /dev/kmsg
		say "$cyc: deep sleep for ${seconds}s (rtcwake -m mem)$attempt; $(roles)"
		slept=1
		rtcwake -u -d "$rtc" -m mem -s "$seconds" > "$ramdir/rtcwake-$sfx" 2>&1
		rtc_rc=$?
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
		wake=$(selected /sys/power/pm_wakeup_irq)
		asleep=$(awk '/Timekeeping suspended for/ {for (n = 1; n < NF; n++) if ($n == "for") s += $(n + 1)} END {if (s) printf "%.1f", s}' "$k")

		reasons=
		storage=healthy
		fails=$(grep -cE "$(fail_re)" "$k")
		if [ "${fails:-0}" != 0 ]; then
			first=$(grep -E "$(fail_re)" "$k" | awk '{sub(/^\[[^]]*\] /, ""); print; exit}')
			reasons="$reasons; $fails failure lines in the kernel log, the first: $first"
			storage=failed
		fi
		[ -z "$prot_why" ] || reasons="$reasons; the protection failed: $prot_why"
		[ "$rtc_rc" = 0 ] || reasons="$reasons; rtcwake returned $rtc_rc ($(awk 'NR == 1' "$ramdir/rtcwake-$sfx"))"
		# Tasks that the suspend could not freeze (not a wakeup: "failed",
		# not "aborted"); freezer_failed below.
		tfail=$(grep -E "$freezer_re" "$k" | awk '{sub(/^\[[^]]*\] /, ""); print; exit}')
		[ -z "$tfail" ] || reasons="$reasons; the suspend could not freeze every task: $tfail"
		grep -q 'PM: suspend entry (deep)' "$k" || reasons="$reasons; no 'PM: suspend entry (deep)'"
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
		reasons=${reasons#; }

		# Depth: AOSS sleep, CX collapse and DDR low power, each at least once.
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
		say "$cyc: asleep ${asleep:-?}s, woken by IRQ ${wake:-?}, rtcwake $rtc_rc, suspend_stats success $(delta "$ok0" "$ok1") fail $(delta "$fail0" "$fail1"), $dline, $(roles), USB devices $(usb_count)"
		ev_after "$sfx" "$k"
		# A suspend the kernel gave up before the sleep, the storage as it
		# was: not a FAIL.  The same cycle again, or, past the budget, the
		# end of the run without a storage verdict.
		if [ -n "$reasons" ] && aborted_attempt; then
			aborted=$((aborted + 1))
			abort_cause
			printf '%s\n' "$abort_by" >> "$ramdir/aborts"
			gaveup="ABORTED: the kernel gave up the suspend before the tablet slept (rtcwake $rtc_rc, suspend_stats success +0 fail +1): $abort_why; the UFS read back as before"
			if [ "$aborted" -gt "$aborts" ]; then
				verdict "SLEEP-TEST $cyc: $gaveup; aborted attempt $aborted, more than aborts=$aborts: the run ends; floor $floor"
				verdict "SLEEP-TEST RESULT: NO-SLEEP at $cyc: $(abort_note), more than aborts=$aborts; $passes of $cycles cycles passed before it; this run does not answer whether the storage comes back from a deep sleep; floor $floor; kernel $kernel"
				verdict 'NEXT: no storage verdict: after the restart run the test again, without touching the screen; the ABORTED lines name what gave up each sleep'
				finish healthy
			fi
			verdict "SLEEP-TEST $cyc: $gaveup; aborted attempt $aborted (aborts=$aborts): $cyc again in ${abort_pause} s; floor $floor"
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
			verdict "SLEEP-TEST $cyc: NO-SLEEP (freezer): the kernel gave up the suspend before the tablet slept because a task did not freeze (rtcwake $rtc_rc, suspend_stats success +0 fail +1): $tfail; stuck: ${stuck:-not named}; the UFS read back as before; not tried again; floor $floor"
			verdict "SLEEP-TEST RESULT: NO-SLEEP (freezer) at $cyc: a task did not freeze (${stuck:-not named}); $passes of $cycles cycles passed before it${an:+; $an and tried again}; this run does not answer whether the storage comes back from a deep sleep; floor $floor; kernel $kernel"
			verdict 'NEXT: no storage verdict: a task that waits behind a writer of the frozen disk (on a memory map'"'"'s lock) cannot be frozen; touch nothing during the run, and if it happens again, run the test from multi-user.target, without the desktop (README, "The sleep test")'
			finish healthy
		fi
		if [ -n "$reasons" ]; then
			an=$(abort_note)
			verdict "SLEEP-TEST $cyc: FAIL: $reasons ($dline; floor $floor)"
			verdict "SLEEP-TEST RESULT: FAIL at $cyc: $reasons; $passes passed before it${an:+; $an and tried again}; floor $floor; kernel $kernel"
			finish "$storage"
		fi
		case $depth in
		deep)
			deep=$((deep + 1))
			verdict "SLEEP-TEST $cyc: PASS: deep ($dline), resumed, the UFS read back as before; floor $floor" ;;
		shallow)
			verdict "SLEEP-TEST $cyc: SHALLOW: resumed and the UFS read back as before, but not every sleep state was reached ($dline); floor $floor" ;;
		*)
			unknown=$((unknown + 1))
			verdict "SLEEP-TEST $cyc: DEPTH-UNKNOWN: resumed and the UFS read back as before; qcom_stats not readable ($dline); floor $floor" ;;
		esac
		passes=$((passes + 1))
		i=$((i + 1)) try=1
	done

	if [ "$deep" = "$cycles" ]; then
		word=PASS
	elif [ "$unknown" != 0 ]; then
		word=DEPTH-UNKNOWN
	else
		word=SHALLOW
	fi
	heldnote=
	[ "$floor" = released ] || heldnote=' (not the depth of the 2026-11-21 failure, which came with the floor released)'
	an=$(abort_note)
	verdict "SLEEP-TEST RESULT: $word: $passes of $cycles cycles resumed with the UFS readable, $deep of them deep${an:+; $an and tried again}; floor $floor$heldnote; kernel $kernel"
	finish healthy
}

if [ "${1:-}" = __ram ]; then
	run_from_ram "$2"
	exit 1
fi

# ------------------------------------------------------ before: the disk ---
cycles=3 seconds=30 hold=300 aborts=5 release=0 confirmed=0 numbers=0 dry=0 mode= aborts_given=0
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
	cycles=0
else
	[ "$cycles" -ge 1 ] && [ "$cycles" -le 10 ] || { echo "$me: CYCLES is 1 to 10, not $cycles" >&2; usage; }
fi
case $seconds in 15|30|60) ;; *) echo "$me: SECONDS is 15, 30 or 60, not $seconds" >&2; usage ;; esac
num "$hold" && [ "$hold" -le 3600 ] || { echo "$me: hold=S is 0 to 3600 seconds" >&2; usage; }
num "$aborts" && [ "$aborts" -le 20 ] || { echo "$me: aborts=N is 0 to 20" >&2; usage; }

# printf, not echo: dash's echo would read the backslash of an escaped
# mount point (\040) as an escape.
refuse() { printf '%s\n' "$me: REFUSED: $*; nothing was changed" >&2; exit 1; }

[ "$(id -u)" = 0 ] || refuse 'run it with sudo'
# The SM-X710 only: see the header.
tr '\0' '\n' < /proc/device-tree/compatible 2>/dev/null | grep -qx "$board" ||
	refuse "this is not an SM-X710 ($board); the SM-X910 has scripts/diagnostics/kernel10-ramroot-test"
[ "$confirmed" = 1 ] ||
	refuse 'the run ends with a restart of the tablet in any case: add restart-ok to say that is fine'
grep -qw deep /sys/power/mem_sleep 2>/dev/null ||
	refuse "/sys/power/mem_sleep offers no deep ($(cat /sys/power/mem_sleep 2>/dev/null))"
grep -qw mem /sys/power/state 2>/dev/null || refuse '/sys/power/state offers no mem'
if [ -e /sys/power/pm_test ]; then
	[ "$(selected /sys/power/pm_test)" = none ] || refuse "a pm_test level is selected ($(cat /sys/power/pm_test))"
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
stop() { printf '%s\n' "$me: STOPPED before freezing anything: $*; nothing on the disk changed but $logs" >&2; exit 1; }
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
{
	if [ "$dry" = 1 ]; then
		echo "$me $stamp: dry: every disk filesystem frozen and no sleep, then a ${hold}s hold and a restart (sysrq b)"
	else
		echo "$me $stamp: $cycles deep cycles of ${seconds}s with every disk filesystem frozen (aborted attempts tried again: up to $aborts), then a ${hold}s hold and a restart (sysrq b)"
	fi
	echo "kernel: $(uname -r) $(uname -v), build ID $kernel; the kit's: ${kit_id:-none}, revision $(cat "$kit/REVISION" 2>/dev/null || echo '7 or earlier')$([ "$kernel" = "$kit_id" ] || echo ' (not running: a baseline run)')"
	echo "boot $(cat /proc/sys/kernel/random/boot_id), up $(cut -d' ' -f1 /proc/uptime)s"
	echo "userdata: $dev on /dev/$disk (SCSI host $ufs_host); the read: the first $read_count x $read_bs, O_DIRECT"
	echo "mem_sleep [$(cat /sys/power/mem_sleep)], suspend_stats success $(stat_of success) fail $(stat_of fail); freeze_filesystems $(cat /sys/power/freeze_filesystems 2>/dev/null || echo n/a), sync_on_suspend $(cat /sys/power/sync_on_suspend 2>/dev/null || echo n/a)"
	echo "qcom_stats: aosd $(count_of aosd) cxsd $(count_of cxsd) ddr $(count_of ddr)$([ -r "$qstats/aosd" ] || echo ' (not readable: the depth cannot be judged)')"
	echo "interconnect floor: $floor; record: $(tr '\n' ' ' < "$icc_state" 2>/dev/null)"
	echo "  last release log line: $(tail -n 1 "$icc_log" 2>/dev/null)"
	[ ! -e "$icc_hold" ] || echo "  $icc_hold exists"
	echo "INT_MAX nodes: $(grep -c 2147483647 /sys/kernel/debug/interconnect/interconnect_summary 2>/dev/null || echo n/a)"
	echo "Type-C: $(roles), USB devices $(usb_count); charger: $(charger)"
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
		say "INT_MAX nodes now: $(grep -c 2147483647 /sys/kernel/debug/interconnect/interconnect_summary 2>/dev/null || echo n/a)"
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

# The sleep settings; swap off the disk.
printf deep > /sys/power/mem_sleep 2>/dev/null
[ "$(selected /sys/power/mem_sleep)" = deep ] || stop 'cannot select deep in /sys/power/mem_sleep'
[ ! -e /sys/power/pm_debug_messages ] || printf 1 > /sys/power/pm_debug_messages 2>/dev/null
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
for v in stamp ramdir part disk cycles seconds hold aborts dry rootsrc rootname floor armed kernel mount_cmd ufs_host; do
	eval "printf '%s=%s\n' $v \"\$(q \"\$$v\")\""
done > "$ramdir/run.conf"
say 'the run goes on from RAM in a session of its own: it ends with a restart even if this terminal goes away'
say "its result: sh $kit/sleep-test.sh status, or $ramdir/result"
say 'while the disk is frozen, programs that write to it wait (the desktop, the journal, a new SSH login): keep this SSH session, and read the result with status, which needs no sudo'
say 'do not touch the screen until the restart: a double tap on it wakes the tablet and aborts the sleep it is in'
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
