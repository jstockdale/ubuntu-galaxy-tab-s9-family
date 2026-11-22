#!/bin/sh
# Is the stall watch running and sampling?  No sudo needed: group adm reads
# its recordings, and the rest is in /proc.
#
#   sh ~/stall-watch/verify.sh         the checks (about 5 s)
#   sh ~/stall-watch/verify.sh mark    also saves a manual mark and checks the
#                                      incident record it makes (about 20 s more)
#
# Prints ok / CHECK lines and a summary; CHECK lines say what was expected.
set -u
export LC_ALL=C

root=''
data=$root/var/log/gts9u-stall-watch
rundir=$root/run/gts9u-stall-watch
service=gts9u-stall-watch.service
sw="python3 -I -S $root/usr/local/lib/gts9u-stall-watch/stall_watch.py --data $data --run-dir $rundir"

ok=0
check=0
pass() { echo "ok     $*"; ok=$((ok + 1)); }
fail() { echo "CHECK  $*"; check=$((check + 1)); }
field() { # key from status.json (dotted for nested)
	python3 -I -S -c 'import json, sys
try:
    d = json.load(open(sys.argv[1]))
    for k in sys.argv[2].split("."):
        d = d[k]
    print(d)
except Exception:
    print("")' "$rundir/status.json" "$1"
}

[ "$(systemctl is-enabled "$service" 2>/dev/null)" = enabled ] && pass "$service enabled" ||
	fail "$service is not enabled (run install.sh)"
[ "$(systemctl is-active "$service" 2>/dev/null)" = active ] && pass "$service active" ||
	fail "$service is not active: systemctl status $service"
pid=$(systemctl show -p MainPID --value "$service" 2>/dev/null)
if [ -n "$pid" ] && [ "$pid" != 0 ] && [ -r "/proc/$pid/stat" ]; then
	# fields after the command name: 38 = rt_priority, 39 = policy
	set -- $(sed 's/.*) //' "/proc/$pid/stat")
	shift 37
	if [ "${2:-}" = 1 ] && [ "${1:-}" = 1 ]; then
		pass "main thread is SCHED_FIFO priority 1"
	else
		fail "main thread policy ${2:-?} priority ${1:-?} (expected FIFO 1: CPUSchedulingPolicy in the unit)"
	fi
	lck=$(awk '/^VmLck:/ {print $2}' "/proc/$pid/status")
	if [ "${lck:-0}" -gt 1024 ]; then
		pass "memory locked: $((lck / 1024)) MB"
	else
		fail "memory not locked (VmLck ${lck:-0} kB; status says: $(field mlock))"
	fi
	adj=$(cat "/proc/$pid/oom_score_adj" 2>/dev/null)
	[ "$adj" = -900 ] && pass "oom_score_adj -900" || fail "oom_score_adj '$adj' (expected -900)"
else
	fail "no main process for $service"
fi

if [ -r "$rundir/status.json" ]; then
	n1=$(field samples)
	sleep 3
	n2=$(field samples)
	age=$(python3 -I -S -c 'import json, sys, time; print(int(time.time() - json.load(open(sys.argv[1]))["updated"]))' "$rundir/status.json" 2>/dev/null)
	if [ "${n2:-0}" -gt "${n1:-0}" ] 2>/dev/null || [ "${age:-99}" -le 12 ]; then
		pass "sampling: $n2 samples, status $age s old"
	else
		fail "status not moving ($n1 -> $n2 samples, $age s old)"
	fi
	cpu=$(field cpu_pct)
	python3 -c "import sys; sys.exit(0 if float('${cpu:-9}' or 9) < 2.0 else 1)" 2>/dev/null &&
		pass "cost $cpu % of one CPU" || fail "cost ${cpu:-?} % of one CPU (expected well under 2 %)"
	p99=$(field late_ms.p99)
	python3 -c "import sys; sys.exit(0 if float('${p99:-999}' or 999) < 50 else 1)" 2>/dev/null &&
		pass "wake-up lateness p99 $p99 ms" || fail "wake-up lateness p99 ${p99:-?} ms (expected < 50 ms)"
	trig=$(field triggers.memory)
	case "$trig" in armed*) pass "PSI memory trigger $trig" ;; *) fail "PSI memory trigger: ${trig:-none}" ;; esac
	k=$(field kmsg)
	[ "$k" = reading ] && pass "reading the kernel log live" || fail "kernel log: ${k:-?}"
	e=$(field errors)
	[ "${e:-0}" = 0 ] && pass "no sampling errors" || fail "$e sampling errors, last: $(field last_error)"
	c=$(field compositor)
	[ -n "$c" ] && [ "$c" != None ] && pass "compositor found (pid $c)" ||
		echo "       no compositor found (fine at the login screen)"
else
	fail "no $rundir/status.json"
fi

grp=$(stat -c '%G %a' "$data" 2>/dev/null)
[ "$grp" = "adm 2750" ] && pass "$data is root:adm 2750" || fail "$data is '$grp' (expected adm 2750)"
if ls "$data/ring"/seg-*.jsonl.gz > /dev/null 2>&1; then
	pass "ring on disk, readable: $(ls "$data/ring" | wc -l) files, $(du -sh "$data/ring" 2>/dev/null | cut -f1)"
else
	up=$(field uptime_s)
	if [ "${up:-0}" -lt 70 ]; then
		echo "       no ring file yet: the first write comes after 60 s (running ${up:-?} s)"
	else
		fail "no ring files in $data/ring (or not readable: are you in group adm? id -nG)"
	fi
fi
$sw status > /dev/null 2>&1 && pass "stall-watch status" || fail "stall-watch status failed"
$sw list > /dev/null 2>&1 && pass "stall-watch list" || fail "stall-watch list failed"

if [ "${1:-}" = mark ]; then
	before=$(ls "$data/incidents" 2>/dev/null | wc -l)
	$sw mark > /dev/null || fail "stall-watch mark failed"
	i=0
	while [ $i -lt 40 ]; do
		now=$(ls "$data/incidents" 2>/dev/null | wc -l)
		[ "$now" -gt "$before" ] && break
		sleep 1
		i=$((i + 1))
	done
	last=$(ls "$data/incidents" 2>/dev/null | tail -n 1)
	if [ "${now:-0}" -gt "$before" ] && [ -r "$data/incidents/$last/samples.jsonl" ] &&
		[ -r "$data/incidents/$last/dump.txt" ]; then
		pass "mark saved as $last (summary, samples, dump)"
		$sw report "$last" | head -n 12
	else
		fail "no new incident record within 40 s of the mark"
	fi
fi

echo "$ok ok, $check to check"
[ "$check" = 0 ]
