#!/bin/sh
# After install.sh and one reboot: check the boot-hygiene changes on this boot.
# No sudo (the journal and /var/lib/ubuntu-gts9u/pstore are readable by adm).
#   sh ~/boothygiene/verify.sh
# Prints ok / CHECK lines and a summary; CHECK lines say what was expected.
set -u

ok=0
check=0
pass() { echo "ok     $*"; ok=$((ok + 1)); }
fail() { echo "CHECK  $*"; check=$((check + 1)); }
show() { systemctl show -p "$2" --value "$1" 2>/dev/null; }
boot=$(cat /proc/sys/kernel/random/boot_id)
echo "boot $boot, up $(cut -d' ' -f1 /proc/uptime) s, device $(dpkg-query -W -f '${Version}' ubuntu-gts9u-device)"

# --- 1. journal from the first kernel line, stamped with the right clock ----
first=$(journalctl -b -k -o short-monotonic --no-pager 2>/dev/null | head -n 1)
mono=$(printf '%s' "$first" | sed -n 's/^\[ *\([0-9.]*\)\].*/\1/p')
if [ -n "$mono" ] && awk -v m="$mono" 'BEGIN { exit !(m < 1.0) }'; then
	pass "kernel journal starts at ${mono} s"
else
	fail "kernel journal starts at '${mono:-none}' s (expected < 1 s; was ~39 s): $first"
fi
# realtime - monotonic of the boot's first entry must be the real boot time
# (now - uptime), not systemd's build date (2026-10-12).
firstjson=$(journalctl -b -o json --no-pager 2>/dev/null | head -n 1)
if python3 -c '
import json, sys, time
first = json.loads(sys.argv[1])
boot_wall = (int(first["__REALTIME_TIMESTAMP"]) - int(first["__MONOTONIC_TIMESTAMP"])) / 1e6
real_boot = time.time() - float(open("/proc/uptime").read().split()[0])
fmt = lambda t: time.strftime("%F %T", time.localtime(t))
print(f"       first entry says the boot began {fmt(boot_wall)}; it began {fmt(real_boot)}"
      f" ({boot_wall - real_boot:+.1f} s)")
sys.exit(0 if abs(boot_wall - real_boot) < 10 else 1)
' "$firstjson"; then
	pass "first entry stamped with the real clock"
else
	fail "first entry not stamped with the real clock (see above)"
fi
max=$(journalctl -b -t systemd-journald --no-pager 2>/dev/null | sed -n 's/.*System Journal .* max \([^,]*\),.*/\1/p' | tail -n 1)
[ "$max" = 1.0G ] && pass "journald SystemMaxUse 1.0G" || fail "journald max '$max' (expected 1.0G)"
first_boot=$(journalctl --list-boots --no-pager -q 2>/dev/null | awk '$1 == "0" { print $4, $5 }')
echo "       journal's first entry of this boot: $first_boot"
n=$(journalctl -b --no-pager 2>/dev/null | grep -c 'Time jumped backwards')
# Information only: the offset saved by the old helper may still be the larger
# of the two whole-second readings until the next NTP save replaces it.
echo "       'Time jumped backwards' this boot: $n (0 expected once rtc-offset holds the smaller offset)"

# --- 2. RTC restore: once, before journald ----------------------------------
# Not with -u: PID 1 logs this start before journald runs, so the line reaches
# the journal through the kernel log, without the UNIT= field -u matches (it
# would count 0).  PID 1's own lines carry SYSLOG_IDENTIFIER=systemd either way.
starts=$(journalctl -b -o cat --no-pager SYSLOG_IDENTIFIER=systemd 2>/dev/null | grep -c '^Starting ubuntu-gts9u-rtc-restore\.service')
[ "$starts" = 1 ] && pass "rtc-restore ran once" || fail "rtc-restore started $starts times (expected 1)"
state="$(show ubuntu-gts9u-rtc-restore.service ActiveState)/$(show ubuntu-gts9u-rtc-restore.service SubState)"
[ "$state" = active/exited ] && pass "rtc-restore active/exited (RemainAfterExit)" || fail "rtc-restore $state (expected active/exited)"
r=$(show ubuntu-gts9u-rtc-restore.service InactiveExitTimestampMonotonic)
j=$(show systemd-journald.service ExecMainStartTimestampMonotonic)
if [ "${r:-0}" -gt 0 ] && [ "${j:-0}" -gt "$r" ]; then
	pass "rtc-restore started before journald ($((r / 1000)) ms < $((j / 1000)) ms)"
else
	fail "rtc-restore at ${r:-?} us, journald at ${j:-?} us (expected restore first)"
fi
journalctl -b -t ubuntu-gts9u-rtc-offset -o cat --no-pager 2>/dev/null | grep -E 'clock set|left alone' | sed 's/^/       /'
[ -e /usr/lib/clock-epoch ] && pass "/usr/lib/clock-epoch $(stat -c %y /usr/lib/clock-epoch | cut -c1-19)" || fail "/usr/lib/clock-epoch missing"

# --- 3. fingerprint stack gated off ------------------------------------------
[ "$(show fprintd.service LoadState)" = masked ] && pass "fprintd masked (generator)" || fail "fprintd LoadState $(show fprintd.service LoadState) (expected masked)"
for u in ubuntu-gts9u-fingerprint-secure ubuntu-gts9u-fingerprint-ui ubuntu-gts9u-qcomtee; do
	c=$(show $u.service ConditionResult); a=$(show $u.service ActiveState)
	[ "$c" = no ] && [ "$a" = inactive ] && pass "$u skipped (condition)" || fail "$u condition=$c state=$a (expected no/inactive)"
done
failed=$(systemctl --failed --plain --no-legend 2>/dev/null | grep -E 'fprint|fingerprint' | cut -d' ' -f1 | tr '\n' ' ')
[ -z "$failed" ] && pass "no failed fingerprint units" || fail "failed: $failed"
n=$(journalctl -b --no-pager 2>/dev/null | grep -c "net.reactivated.Fprint': timed out")
[ "$n" = 0 ] && pass "no 25 s Fprint activation timeouts" || fail "$n Fprint activation timeouts this boot"
journalctl -b --no-pager 2>/dev/null | grep -q 'GTS9U fingerprint overlay: not a Galaxy Tab S9 Ultra' &&
	pass "overlay extension inactive" || fail "no 'overlay ... inactive' line from gnome-shell (log in once, or GDM has not started)"
lsmod | grep -qE '^(qcomtee|snvm) ' && fail "qcomtee/snvm loaded" || pass "qcomtee/snvm not loaded"

# --- 4. pstore archive ---------------------------------------------------------
state="$(show ubuntu-gts9u-pstore-archive.service LoadState)/$(show ubuntu-gts9u-pstore-archive.service UnitFileState)/$(show ubuntu-gts9u-pstore-archive.service ActiveState)/$(show ubuntu-gts9u-pstore-archive.service ConditionResult)"
case "$state" in
loaded/enabled/active/yes) pass "pstore archive ran" ;;
loaded/enabled/inactive/no) pass "pstore archive skipped: systemd-pstore found no records this boot" ;;
*) fail "pstore archive load/enabled/active/condition = $state" ;;
esac
journalctl -b -u ubuntu-gts9u-pstore-archive.service -o cat --no-pager 2>/dev/null | sed 's/^/       /'
arch=/var/lib/ubuntu-gts9u/pstore
if [ -r "$arch" ]; then
	last=$(ls -1 "$arch" | grep -E '^[0-9]+-' | sort -n | tail -n 1)
	echo "       $(ls -1 "$arch" | grep -cE '^[0-9]+-') archive(s); newest: ${last:-none}"
	if [ -n "$last" ]; then
		sed -n '1,2p' "$arch/$last/INFO" | sed 's/^/       /'
		[ -r "$arch/$last/console-ramoops-0" ] &&
			echo "       console tail: $(tail -n 1 "$arch/$last/console-ramoops-0" | cut -c1-100)"
	fi
fi

# --- 5. pogo restore after the panel cycle -------------------------------------
c=$(show ubuntu-gts9u-panel-coldboot-recover.service ActiveEnterTimestampMonotonic)
p=$(show ubuntu-gts9u-pogo-firmware.service ExecMainStartTimestampMonotonic)
if [ "${c:-0}" -gt 0 ] && [ "${p:-0}" -ge "$c" ]; then
	pass "pogo restore started after the panel cycle ($((c / 1000000)) s -> $((p / 1000000)) s)"
else
	fail "panel cycle done at ${c:-?} us, pogo restore at ${p:-?} us (expected pogo after)"
fi
journalctl -b -u ubuntu-gts9u-pogo-firmware.service -o cat --no-pager 2>/dev/null | grep 'ubuntu-gts9u-pogo' | sed 's/^/       /'

# --- 6. bring-up microSD logger (only where installed) ------------------------
if [ -e /usr/libexec/gts9u-bringup-sdlog ]; then
	grep -q '^OnUnitActiveSec' /usr/lib/systemd/system/gts9u-bringup-sdlog.timer &&
		fail "logger timer still repeats" || pass "logger timer boot-only"
	runs=$(journalctl -b -u gts9u-bringup-sdlog.service --no-pager 2>/dev/null | grep -c 'Starting gts9u-bringup-sdlog')
	echo "       logger runs this boot: $runs (expected 4 once uptime > 3 min)"
	card=$(ls -d /media/*/*/bringup 2>/dev/null | head -n 1)
	d=
	[ -n "$card" ] && d=$(ls -1 "$card" | grep -E '^[0-9]{6}-' | sort -n | tail -n 1)
	if [ -n "$d" ]; then
		echo "       card: $card, $(ls -1 "$card" | grep -cE '^[0-9]{6}-') numbered boot dirs, newest $d:"
		echo "         $(ls "$card/$d" | tr '\n' ' ')"
	else
		echo "       card: ${card:-not mounted for this user}; no numbered boot directory yet"
	fi
fi

echo "summary: $ok ok, $check CHECK"
