#!/bin/bash
# Needs root.  Phase 1: pin the GPU to each frequency and run the lost-write check.
# Phase 2: toggle the clock between min and max every 50 ms while the check runs.
# Restores the previous devfreq governor at the end.
d=/sys/class/devfreq/3d00000.gpu; N=${1:-16}
gov=$(cat $d/governor); echo "previous governor: $gov"
cleanup() { kill $toggler 2>/dev/null; echo "$gov" > $d/governor; echo "governor restored to $gov"; }
trap cleanup EXIT
echo userspace > $d/governor || { echo "cannot set userspace governor"; exit 1; }
check() { (cd /home/jstockdale/gputest && FD_MESA_DEBUG=noubwc timeout 200 python3 late-write-check.py $1 2>&1 | awk -F'[:|]' '/^frame/ {n++; split($3,a," "); lost=a[1]; if (lost>0) {f++; t+=lost}} END {printf "%d/%d frames lost pixels, mean %.0f per affected frame", f, n, (f? t/f : 0)}'); }
echo "== phase 1: pinned frequencies"
for f in $(cat $d/available_frequencies); do
	echo "$f" > $d/userspace/set_freq; sleep 0.3
	printf "%4d MHz (reads back %4d): %s\n" $((f/1000000)) $(( $(cat $d/cur_freq) / 1000000 )) "$(check $N)"
done
echo "== phase 2: clock toggling 220 <-> 680 MHz every 50 ms during the run"
( while :; do echo 220000000 > $d/userspace/set_freq; sleep 0.05; echo 680000000 > $d/userspace/set_freq; sleep 0.05; done ) & toggler=$!
sleep 0.2; printf "toggling: %s\n" "$(check 24)"
kill $toggler 2>/dev/null; wait $toggler 2>/dev/null; toggler=
echo "== phase 3: pinned at 475 MHz again (the frequency the default governor picks)"
echo 475000000 > $d/userspace/set_freq; sleep 0.3; printf "475 MHz pinned: %s\n" "$(check 24)"
