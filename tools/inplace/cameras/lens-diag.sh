#!/bin/sh
# Why the SM-X710's DW9808 lens fails its first write, in the same boot as a
# camera-test.sh rear run (the rear camera must already be enabled).
#
#   sudo sh ~/cameras-kit/lens-diag.sh
#
# Not read-only: it rebinds the dw9808-vcm driver (clearing the runtime-PM
# error a failed resume latches) and opens the lens, which powers the rear
# module and moves the lens.  A reboot undoes everything (the camera switch
# is per boot).
#
#   1. open the lens with the rail cold, as the first open after enabling did
#   2. if that fails again: open it while the sensor streams, so the shared
#      rear rail has been up for seconds
#   3. whichever open works: sweep focus_absolute with the lens held open, so
#      it stays powered between steps
#   4. 20 power cycles: each open powers the lens up and moves it, and the
#      close comes 0.2 s later, once it has settled; a failure is rebound and
#      counted.  This is the power-up on its own.
#   5. up to 6 quick cycles, closed right after the move.  Before the
#      dw9808_vcm suspend fix the first may already fail: a park that finds
#      the lens still settling returns -EBUSY after powering it down, the
#      device stays "active", and the next open talks to an unpowered lens
#      (CCI timeouts, then "unbalanced disables for rear_cam_vio").
#
# Everything lands in /var/log/gts9u-cameras-test/TIME-lens/ (group adm).
set -u
[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
lens=14-000c
drv=/sys/bus/i2c/drivers/dw9808-vcm
dev=/sys/bus/i2c/devices/$lens
[ -e "$dev" ] || { echo "no $lens: run camera-test.sh rear in this boot first" >&2; exit 1; }
out=/var/log/gts9u-cameras-test/$(date +%Y%m%d-%H%M%S)-lens
mkdir -p "$out"; chgrp adm "$out" 2>/dev/null; chmod 0750 "$out"
log() { echo "$*" | tee -a "$out/summary.txt"; }
subdev() { # the lens's v4l-subdev node
	for s in "$dev"/video4linux/v4l-subdev*; do [ -e "$s" ] && echo "/dev/${s##*/}"; done | head -1
}
state() {
	log "  rpm=$(cat "$dev/power/runtime_status") $(grep -E 'rear_cam_vio|vreg_l4b' /sys/kernel/debug/regulator/regulator_summary 2>/dev/null | tr -s ' ' | tr '\n' '|')"
}
rebind() {
	echo "$lens" > "$drv/unbind" 2>/dev/null
	sleep 1
	echo "$lens" > "$drv/bind" || log "  bind failed"
	sleep 1
}
since() { # MARK FILE: the kernel log after MARK, and its lens and bus lines
	dmesg | tail -n +"$(($1 + 1))" > "$2"
	grep -iE 'dw9808|cci|timeout|i2c' "$2" | sed 's/^/  /' | tee -a "$out/summary.txt"
}
try_open() { # label
	mark=$(dmesg | wc -l)
	sd=$(subdev)
	log "$1: open $sd"
	if v4l2-ctl -d "$sd" --get-ctrl focus_absolute > "$out/$1-open.txt" 2>&1; then
		log "  open OK: $(cat "$out/$1-open.txt")"
		ok=1
	else
		log "  open FAILED: $(tail -n 1 "$out/$1-open.txt")"
		ok=0
	fi
	state
	since "$mark" "$out/$1-dmesg.txt"
	return $((1 - ok))
}

log "kernel $(uname -r), boot $(cat /proc/sys/kernel/random/boot_id)"
log "before:"; state

# 1: the rail cold, as on the first open.
rebind
if try_open cold; then good=cold; else
	# 2: the rail warm, held by the streaming sensor.
	log "streaming the sensor to hold the rail up"
	timeout 25 cam --camera=1 --capture=400 \
		--stream=width=640,height=480,pixelformat=BGR888 > "$out/stream.txt" 2>&1 &
	cam_pid=$!
	sleep 6
	rebind
	if try_open warm; then good=warm; else good=; fi
	wait "$cam_pid" 2>/dev/null
fi

# 3: the lens answering: a focus sweep, held open by fd 3 so that the lens
# stays powered from step to step.
if [ -n "$good" ]; then
	sd=$(subdev)
	mark=$(dmesg | wc -l)
	log "sweep on $sd (opened $good), held open:"
	(
		command exec 3<> "$sd" || { log "  hold-open FAILED"; exit 1; }
		for pos in 0 256 512 768 1023 512; do
			if v4l2-ctl -d "$sd" --set-ctrl focus_absolute=$pos 2>> "$out/sweep.txt"; then
				log "  focus_absolute=$pos ok"
			else
				log "  focus_absolute=$pos FAILED"
			fi
			sleep 0.3
		done
	)
	state
	since "$mark" "$out/sweep-dmesg.txt"

	# 4: power cycles.  fd 3 opens the lens (power-up, init, walk to the
	# last position), the set jumps it, and fd 3 closes it 0.2 s later
	# (walk back, power-down).
	mark=$(dmesg | wc -l)
	log "20 power cycles, closed once settled:"
	fails=0
	i=1
	while [ "$i" -le 20 ]; do
		pos=$((i % 2 ? 800 : 200))
		if ! (exec 3<> "$sd" && v4l2-ctl -d "$sd" --set-ctrl focus_absolute=$pos &&
			sleep 0.2) 2>> "$out/cycles.txt"; then
			fails=$((fails + 1))
			log "  cycle $i (focus_absolute=$pos) FAILED: $(tail -n 1 "$out/cycles.txt")"
			rebind
			sd=$(subdev)
		fi
		sleep 0.3
		i=$((i + 1))
	done
	log "  $((20 - fails)) of 20 power cycles ok"
	state
	since "$mark" "$out/cycles-dmesg.txt"

	# 5: quick cycles: v4l2-ctl closes right after the jump.
	mark=$(dmesg | wc -l)
	log "quick cycles, closed right after the move (stops at the first failure):"
	i=1
	while [ "$i" -le 6 ]; do
		pos=$((i % 2 ? 800 : 200))
		if ! v4l2-ctl -d "$sd" --set-ctrl focus_absolute=$pos 2>> "$out/quick.txt"; then
			log "  quick cycle $i (focus_absolute=$pos) FAILED: $(tail -n 1 "$out/quick.txt")"
			break
		fi
		log "  quick cycle $i ok"
		sleep 0.3
		i=$((i + 1))
	done
	sleep 2
	state
	since "$mark" "$out/quick-dmesg.txt"
	grep -c 'unbalanced disables for rear_cam_vio' "$out/quick-dmesg.txt" |
		sed 's/^/  unbalanced rear_cam_vio disables: /' | tee -a "$out/summary.txt"
fi
log "result: ${good:-no open worked}"
log "results: $out"
