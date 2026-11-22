#!/bin/sh
# Deep sleeps of the SM-X710 with every disk filesystem read-only, to test
# whether the UFS storage comes back, without risking the data on it.
#   sudo sh ~/typec-roles/sleep-test.sh ro [CYCLES] [SECONDS] [release-floor] [hold=S] restart-ok
#   sh ~/typec-roles/sleep-test.sh status
# ro (the only mode, and the default): CYCLES deep sleeps (default 3, at most
# 10) of SECONDS each (15, 30 or 60; default 30), each woken by the RTC.
# restart-ok is required: it says that the tablet will be restarted at the
# end, whatever happens.  release-floor first releases the interconnect floor
# if it is held, the way its own HOLD message says.  hold=S is how long the
# result stays on screen and in /run before the restart (default 300 s).
# status (no root) prints the latest run's result from /run.
#
# On 2026-11-21 one deep sleep (rtcwake -m mem -s 30) did not bring the UFS
# PHY back on this tablet: its calibration timed out, every reset of the UFS
# host failed, ext4 aborted its journal and the root went read-only.  A sleep
# test with the root writable can therefore cost data.  This one never sleeps
# with a disk filesystem writable, never writes to the disk after it made
# them read-only, and needs nothing from the disk after that:
#   - before anything else: root, this board, deep in mem_sleep, the RTC, the
#     userdata partition, every tool, and the explicit restart-ok, or nothing
#     is done;
#   - it records the state before (kernel, uptime, the interconnect floor,
#     suspend_stats, qcom_stats, the Type-C roles, the charger) in /run and in
#     ~/typec-roles/logs;
#   - every tool the run needs from then on, the shell, this script, and
#     every library and the dynamic loader they load, are copied to a RAM
#     root: a tmpfs of its own under /run, each file at the path it has on
#     the disk.  The rest of the run happens in a mount namespace of its own
#     with that tmpfs as its root (chroot), /proc, /sys, /dev and /run bound
#     in: every tool runs at its own path, from RAM, and nothing of the disk
#     is in reach.  Swap on the disk is switched off (the kernel writes a
#     swap file below its filesystem);
#   - the run goes on in a session of its own, so it ends even if SSH drops:
#     its log is in /run/gts9u-sleep-test/<time>/ (readable without root,
#     latest -> the newest) and on the terminal while that lives;
#   - then sync, sysrq u (every filesystem on a block device read-only), a
#     check of /proc/1/mounts (the system's) that every disk filesystem is
#     read-only, and the UFS disk made read-only at the block layer as well,
#     so that a write that still came would be logged ("Trying to write to
#     read-only block-device") and fail the run;
#   - a read of the first 64 MiB of userdata with O_DIRECT (opened read-only,
#     past the page cache), hashed, before the first sleep and after each
#     resume;
#   - each cycle: rtcwake -m mem, then the kernel log since the sleep (the
#     UFS failure lines, I/O errors, the diagnostic's lines of a revision 8
#     kernel), the read, suspend_stats, and qcom_stats aosd/cxsd/ddr (did the
#     SoC reach AOSS sleep, CX collapse and DDR low power at all? A cycle
#     that did not is SHALLOW, not a pass).  The run stops at the first
#     failed cycle;
#   - one verdict line per cycle (SLEEP-TEST cycle ...), one for the run
#     (SLEEP-TEST RESULT: ...) and the next steps (NEXT: ...), also in the
#     kernel log and in /dev/pmsg0, which pstore keeps for the next boot;
#     each cycle's diagnostic lines (cycle N/M: diagnostic: ...) go to the
#     result file and to /dev/pmsg0 as well, at most the first 12 and the
#     last 4 of a cycle there, so that a failed one is still readable after
#     the restart;
#   - then, after hold=S, the restart.  While the storage reads back as
#     before, a clean reboot: SIGINT to systemd, which starts
#     ctrl-alt-del.target, reboot.target on this system (checked before).
#     It needs no program from the disk, works with read-only filesystems,
#     and the kernel shuts the UFS device down.  After a storage failure, or
#     with no reboot after 300 s, sysrq b: it needs nothing from the disk (it
#     worked on 2026-11-21) and loses nothing, because everything was synced
#     and read-only before the first sleep.
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
# now (it writes its log and marker before this run goes read-only), and is
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
# systemd, which starts ctrl-alt-del.target on SIGINT.
init_pid=1
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
# The tools the run calls once it has gone read-only: from the RAM root.
ram_tools='rtcwake dmesg dd sha256sum sleep grep awk sync blockdev'
ufs_fail='phy initialization timed-out|Failed to calibrate PHY|Enabling the controller failed|Host init failed|ufshcd_wl_resume failed|Trying to write to read-only block-device'
ufs_diag='calibration entry: SW_RESET|after the PCS_READY timeout: SW_RESET|PCS_READY .*SERDES_START|1 s after SERDES_START: SW_RESET'
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
# come to well under 20 KiB with their verdicts.
diag_head=12
diag_tail=4

usage() {
	sed -n '2,12s/^# //p' "$0" >&2
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
# start with ro.  The shell reads /proc itself.
writable_mounts() {
	while read -r msrc mdir mtype mopts _; do
		case $msrc in /dev/*) ;; *) continue ;; esac
		case $mtype in fuse|fuse.*) continue ;; esac
		case $mopts in ro|ro,*) ;; *) printf ' %s(%s)' "$mdir" "$msrc" ;; esac
	done < /proc/1/mounts
}

# Swap on a disk: the kernel writes a swap file below its filesystem, which
# read-only does not stop.  zram is RAM.
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

# The lines of a kernel log excerpt after the one holding MARKER.
since() { # MARKER FILE
	awk -v m="$1" 'f; index($0, m) {f = 1}' "$2"
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

# The failure lines of a kernel log excerpt: the UFS ones, the block layer's
# warning of a write to the read-only disk (once per device, and the write
# still goes out), and I/O and ext4 errors on the UFS disk.
fail_re() {
	echo "$ufs_fail|I/O error, dev $disk,|Buffer I/O error on dev(ice)? $disk[0-9]|EXT4-fs error \\(device $disk[0-9]|Aborting journal on device $disk[0-9]"
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

# Sync, then sysrq u, and wait for the kernel's "Emergency Remount complete"
# after a line of ours: the kernel remounts in a work queue.
go_read_only() { # WHEN
	gm="gts9u sleep-test: $stamp remount $1"
	printf '%s\n' "$gm" >> /dev/kmsg
	sync
	printf u >> /proc/sysrq-trigger
	gn=0
	while :; do
		dmesg > "$ramdir/kmsg.remount" 2>/dev/null
		since "$gm" "$ramdir/kmsg.remount" | grep -q 'Emergency Remount complete' && return 0
		if [ "$gn" -ge 60 ]; then
			say 'sysrq u: no "Emergency Remount complete" within 60 s'
			return 1
		fi
		sleep 1
		gn=$((gn + 1))
	done
}

restart() { # healthy|failed
	if [ "$1" = healthy ] && [ "$clean_reboot" = 1 ]; then
		say 'restart: a clean reboot (SIGINT to systemd: ctrl-alt-del.target); the storage read back as before'
		# systemd's TERM on the way down ends this run.
		trap - TERM
		kill -INT "$init_pid"
		rn=0
		while [ "$rn" -lt 30 ]; do
			sleep 10
			rn=$((rn + 1))
		done
		say 'restart: no reboot within 300 s; sysrq b'
	elif [ "$1" = healthy ]; then
		say 'restart: sysrq b (ctrl-alt-del.target is not reboot.target here; everything was synced and read-only before the first sleep)'
	else
		say 'restart: sysrq b (immediate; every disk filesystem was synced and read-only before the first sleep)'
	fi
	printf b >> /proc/sysrq-trigger
	sleep 30
	say 'restart: still here after sysrq b; hold Power and Volume Down until the tablet restarts'
	exit 1
}

# The end of a run, once its verdict is out: the next steps, the hold, the
# restart.
finish() { # healthy|failed
	if [ "$armed" = 1 ]; then
		verdict "NEXT: the boot after this restart holds the interconnect floor once (loop breaker: this boot released it and cannot record a clean stop on read-only filesystems); give the next run release-floor"
	fi
	if [ "$1" = failed ]; then
		verdict 'NEXT: the storage did not come back; after the restart run sh ~/typec-roles/verify.sh, and read the diagnostic lines above against docs/resume-recovery.md ("Reading the diagnostic")'
	else
		verdict 'NEXT: after the restart, sh ~/typec-roles/verify.sh'
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
	say "$me: going read-only now: from here on the run cannot be stopped, and it ends with a restart"
	go_read_only first
	rw=$(writable_mounts)
	if [ -n "$rw" ]; then
		verdict "SLEEP-TEST RESULT: ABORTED before any sleep: still writable after sysrq u:$rw; floor $floor; kernel $kernel"
		finish healthy
	fi
	say 'every disk filesystem is read-only'
	if ! blockdev --setro "/dev/$disk" >> "$ramdir/log" 2>&1; then
		verdict "SLEEP-TEST RESULT: ABORTED before any sleep: blockdev --setro /dev/$disk failed; floor $floor; kernel $kernel"
		finish healthy
	fi
	ro=$(selected "/sys/class/block/$disk/ro")
	if [ "$ro" != 1 ]; then
		verdict "SLEEP-TEST RESULT: ABORTED before any sleep: /dev/$disk is not read-only at the block layer (ro=${ro:-?}); floor $floor; kernel $kernel"
		finish healthy
	fi
	say "/dev/$disk is read-only at the block layer too"
	read_check 0
	if [ -z "$read_hash" ]; then
		verdict "SLEEP-TEST RESULT: FAIL before any sleep: the first read of $part failed ($read_why); floor $floor; kernel $kernel"
		finish failed
	fi
	base_hash=$read_hash
	say "read of $part ($read_count x $read_bs, O_DIRECT): sha256 ${base_hash%"${base_hash#????????????????}"}"

	i=1 passes=0 deep=0 unknown=0
	while [ "$i" -le "$cycles" ]; do
		cyc="cycle $i/$cycles"
		rw=$(writable_mounts)
		if [ -n "$rw" ]; then
			say "$cyc: writable again:$rw; sysrq u once more"
			go_read_only "before cycle $i"
			rw=$(writable_mounts)
		fi
		sw=$(disk_swaps)
		[ "$(selected /sys/power/mem_sleep)" = deep ] || printf deep > /sys/power/mem_sleep 2>/dev/null
		ro=$(selected "/sys/class/block/$disk/ro")
		if [ -n "$rw" ] || [ -n "$sw" ] || [ "$ro" != 1 ] || [ "$(selected /sys/power/mem_sleep)" != deep ]; then
			why="writable:${rw:- none}; disk swap:${sw:- none}; /dev/$disk ro=${ro:-?}; mem_sleep $(selected /sys/power/mem_sleep)"
			verdict "SLEEP-TEST $cyc: NOT STARTED: $why"
			verdict "SLEEP-TEST RESULT: ABORTED at $cyc, before its sleep ($why); $passes passed before it; floor $floor; kernel $kernel"
			finish healthy
		fi
		ok0=$(stat_of success) fail0=$(stat_of fail)
		a0=$(count_of aosd) x0=$(count_of cxsd) d0=$(count_of ddr)
		marker="gts9u sleep-test: $stamp cycle $i"
		printf '%s\n' "$marker" >> /dev/kmsg
		say "$cyc: deep sleep for ${seconds}s (rtcwake -m mem); $(roles)"
		rtcwake -u -d "$rtc" -m mem -s "$seconds" > "$ramdir/rtcwake-$i" 2>&1
		rtc_rc=$?
		sleep "$settle"
		read_check "$i"
		sleep 2
		dmesg > "$ramdir/kmsg.all" 2>/dev/null
		k=$ramdir/kmsg-$i
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
		[ "$rtc_rc" = 0 ] || reasons="$reasons; rtcwake returned $rtc_rc ($(awk 'NR == 1' "$ramdir/rtcwake-$i"))"
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
		grep -E "$ufs_diag" "$k" > "$ramdir/diag-$i"
		dn=$(grep -c '' "$ramdir/diag-$i")
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
		done < "$ramdir/diag-$i"
		say "$cyc: asleep ${asleep:-?}s, woken by IRQ ${wake:-?}, rtcwake $rtc_rc, suspend_stats success $(delta "$ok0" "$ok1") fail $(delta "$fail0" "$fail1"), $dline, $(roles), USB devices $(usb_count)"
		if [ -n "$reasons" ]; then
			verdict "SLEEP-TEST $cyc: FAIL: $reasons ($dline; floor $floor)"
			verdict "SLEEP-TEST RESULT: FAIL at $cyc: $reasons; $passes passed before it; floor $floor; kernel $kernel"
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
		i=$((i + 1))
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
	verdict "SLEEP-TEST RESULT: $word: $passes of $cycles cycles resumed with the UFS readable, $deep of them deep; floor $floor$heldnote; kernel $kernel"
	finish healthy
}

if [ "${1:-}" = __ram ]; then
	run_from_ram "$2"
	exit 1
fi

# ------------------------------------------------------ before: the disk ---
cycles=3 seconds=30 hold=300 release=0 confirmed=0 numbers=0
for a in "$@"; do
	case $a in
	ro) ;;
	restart-ok) confirmed=1 ;;
	release-floor) release=1 ;;
	hold=*) hold=${a#hold=} ;;
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
[ "$cycles" -ge 1 ] && [ "$cycles" -le 10 ] || { echo "$me: CYCLES is 1 to 10, not $cycles" >&2; usage; }
case $seconds in 15|30|60) ;; *) echo "$me: SECONDS is 15, 30 or 60, not $seconds" >&2; usage ;; esac
num "$hold" && [ "$hold" -le 3600 ] || { echo "$me: hold=S is 0 to 3600 seconds" >&2; usage; }

refuse() { echo "$me: REFUSED: $*; nothing was changed" >&2; exit 1; }

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
# A root that ext4 gave up on keeps "rw" among its options and adds one of
# these: a failed run's tablet, not restarted yet.
root_opts=$(findmnt -no OPTIONS /)
echo "$root_opts" | tr ',' '\n' | grep -qx rw &&
	! echo "$root_opts" | tr ',' '\n' | grep -qxE 'emergency_ro|shutdown' ||
	refuse "the root filesystem is not writable ($root_opts): restart the tablet first"
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
for t in $ram_tools ldd readlink od cp ln mount unshare chroot setsid flock findmnt systemctl swapoff; do
	command -v "$t" >/dev/null 2>&1 || refuse "missing tool: $t"
done
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
# rest (a tmpfs, swap off, the floor) goes with the next restart.
stop() { echo "$me: STOPPED before going read-only: $*; nothing on the disk changed but $logs" >&2; exit 1; }
stamp=$(date +%Y%m%d-%H%M%S)
ramdir=$base/$stamp
logs=$kit/logs/$stamp-sleep
mkdir -p "$ramdir" || refuse "cannot create $ramdir"
mount -t tmpfs -o size=256m,mode=0755,nosuid,nodev gts9u-sleep-test "$ramdir" ||
	refuse "cannot mount a tmpfs on $ramdir"
ln -sfn "$stamp" "$base/latest"
mkdir -p "$ramdir/tools" "$ramdir/tz" "$logs" || stop "cannot create $ramdir/tools or $logs"
: > "$ramdir/log"
: > "$ramdir/copied"
own() { [ -z "${SUDO_UID:-}" ] || chown -R "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" "$kit/logs"; }

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
# nothing goes read-only.
in_ram_root "$ramdir/tools/sh" -c '
	PATH=$1/tools
	sleep 0 && sync && [ "$(awk "BEGIN { print 7 }")" = 7 ] && [ "$(grep -c x /dev/null)" = 0 ] &&
	[ "$(sha256sum < /dev/null)" = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  -" ] &&
	dd if=/dev/zero of=/dev/null bs=1 count=1 status=none && dmesg > /dev/null &&
	rtcwake --version > /dev/null && blockdev --getro "/dev/$2" > /dev/null' \
	sh "$ramdir" "$disk" > "$ramdir/check.out" 2>&1 ||
	stop "a tool does not run from the RAM root: $(head -n 3 "$ramdir/check.out" | tr '\n' ' ')"

kernel=$(od -An -tx1 -v /sys/kernel/notes | tr -d ' \n' | grep -o '474e5500[0-9a-f]\{40\}' | cut -c9- | cut -c1-12)
kit_id=$(cut -c1-12 "$kit/BUILD_ID" 2>/dev/null)
clean_reboot=0
[ "$(systemctl show -p Id --value ctrl-alt-del.target 2>/dev/null)" = reboot.target ] && clean_reboot=1
{
	echo "$me $stamp: $cycles deep cycles of ${seconds}s with every disk filesystem read-only, then a ${hold}s hold and a restart"
	echo "kernel: $(uname -r) $(uname -v), build ID $kernel; the kit's: ${kit_id:-none}, revision $(cat "$kit/REVISION" 2>/dev/null || echo '7 or earlier')$([ "$kernel" = "$kit_id" ] || echo ' (not running: a baseline run)')"
	echo "boot $(cat /proc/sys/kernel/random/boot_id), up $(cut -d' ' -f1 /proc/uptime)s"
	echo "userdata: $dev on /dev/$disk; the read: the first $read_count x $read_bs, O_DIRECT"
	echo "mem_sleep [$(cat /sys/power/mem_sleep)], suspend_stats success $(stat_of success) fail $(stat_of fail)"
	echo "qcom_stats: aosd $(count_of aosd) cxsd $(count_of cxsd) ddr $(count_of ddr)$([ -r "$qstats/aosd" ] || echo ' (not readable: the depth cannot be judged)')"
	echo "interconnect floor: $floor; record: $(tr '\n' ' ' < "$icc_state" 2>/dev/null)"
	echo "  last release log line: $(tail -n 1 "$icc_log" 2>/dev/null)"
	[ ! -e "$icc_hold" ] || echo "  $icc_hold exists"
	echo "INT_MAX nodes: $(grep -c 2147483647 /sys/kernel/debug/interconnect/interconnect_summary 2>/dev/null || echo n/a)"
	echo "Type-C: $(roles), USB devices $(usb_count); charger: $(charger)"
	echo "restart: $([ "$clean_reboot" = 1 ] && echo 'clean (ctrl-alt-del.target is reboot.target) while the storage reads back' || echo 'sysrq b (ctrl-alt-del.target is not reboot.target)')"
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
for v in stamp ramdir part disk cycles seconds hold floor armed kernel clean_reboot; do
	eval "printf '%s=%s\n' $v \"\$(q \"\$$v\")\""
done > "$ramdir/run.conf"
say 'the run goes on from RAM in a session of its own: it ends with a restart even if this terminal goes away'
say "its result: sh $kit/sleep-test.sh status, or $ramdir/result"
ram_prefix=setsid
in_ram_root "$ramdir/tools/sh" "$ramdir/sleep-test.sh" __ram "$ramdir/run.conf" &
wait $!
