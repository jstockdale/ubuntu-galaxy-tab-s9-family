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
#   3. whichever open works: sweep focus_absolute and read the status register
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
	dmesg | tail -n +"$((mark + 1))" > "$out/$1-dmesg.txt"
	grep -iE 'dw9808|cci|timeout|i2c' "$out/$1-dmesg.txt" | sed 's/^/  /' | tee -a "$out/summary.txt"
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

# 3: the lens answering: a focus sweep, with the open held by a reader.
if [ -n "$good" ]; then
	sd=$(subdev)
	log "sweep on $sd (opened $good):"
	for pos in 0 256 512 768 1023 512; do
		if v4l2-ctl -d "$sd" --set-ctrl focus_absolute=$pos 2>> "$out/sweep.txt"; then
			log "  focus_absolute=$pos ok"
		else
			log "  focus_absolute=$pos FAILED"
		fi
		sleep 0.3
	done
fi
log "result: ${good:-no open worked}"
log "results: $out"
