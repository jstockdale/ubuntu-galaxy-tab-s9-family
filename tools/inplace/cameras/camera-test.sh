#!/bin/sh
# First light for the SM-X710 cameras, once a kernel with the gts9wifi_camera
# switch is booted.  Enables the chosen camera(s) for this boot only, then
# collects evidence; nothing persists across a reboot.
#
#   sudo sh ~/cameras-kit/camera-test.sh rear      start here
#   sudo sh ~/cameras-kit/camera-test.sh front     after a reboot
#   sudo sh ~/cameras-kit/camera-test.sh both      after a reboot
#
# Everything lands in /var/log/gts9u-cameras-test/TIME-CHOICE/ (group adm,
# so it can be read and copied over SSH without sudo); summary.txt first.
#
#   1. records the kernel, cmdline and switch state; stops the relays
#   2. ubuntu-gts9u-camera-enable CHOICE (writes the switch, waits up to 20 s
#      for the HI1337(s) and CAMSS)
#   3. dmesg since step 1, media-ctl -p, v4l2-ctl --list-devices, the sensor
#      and lens controls
#   4. cam -l, then per camera 60 frames at 640x480 BGR888 through libcamera's
#      software ISP; the last frame is kept as a PPM
#   5. rebuilds the V4L2 nodes from the new table, starts the relays and reads
#      60 frames of /dev/video20 (YUYV 640x480); the last one is kept as a PPM
#
# A hang or a reset here is the finding: a reboot returns the tablet to the
# state before the run (the switch is per boot, the DTB is unchanged).
set -u

choice=${1:-}
case "$choice" in
rear | front | both) ;;
*) echo 'usage: camera-test.sh rear|front|both' >&2; exit 2 ;;
esac
[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
tr '\0' '\n' < /proc/device-tree/compatible | grep -qx samsung,gts9wifi ||
	{ echo 'this is not an SM-X710' >&2; exit 1; }

out=/var/log/gts9u-cameras-test/$(date +%Y%m%d-%H%M%S)-$choice
mkdir -p "$out"
chgrp adm "$out" 2>/dev/null; chmod 0750 "$out"
summary=$out/summary.txt
ok() { echo "ok     $*" | tee -a "$summary"; }
ck() { echo "CHECK  $*" | tee -a "$summary"; }
inf() { echo "info   $*" | tee -a "$summary"; }
owner=$(cat /run/ubuntu-gts9u/desktop-user 2>/dev/null || echo jstockdale)
uid=$(id -u "$owner" 2>/dev/null || echo 1000)
as_owner() {
	runuser -u "$owner" -- env XDG_RUNTIME_DIR="/run/user/$uid" \
		DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" "$@"
}

# --- 1 -------------------------------------------------------------------------
inf "$(uname -r), boot $(cat /proc/sys/kernel/random/boot_id), up $(cut -d. -f1 /proc/uptime) s"
cat /proc/cmdline > "$out/cmdline.txt"
param=/sys/module/gts9wifi_camera/parameters/enable
if [ ! -r "$param" ]; then
	ck 'this kernel has no gts9wifi_camera switch: flash the camera kernel first'
	exit 1
fi
inf "switch before: $(cat "$param")"
dmesg_start=$(dmesg | wc -l)
systemctl stop ubuntu-gts9u-camera-relays.service

# --- 2 -------------------------------------------------------------------------
if /usr/libexec/ubuntu-gts9u-camera-enable "$choice" > "$out/enable.log" 2>&1; then
	ok "enable: $(tail -n 1 "$out/enable.log")"
else
	ck "enable: $(tail -n 1 "$out/enable.log")"
fi
inf "switch after: $(cat "$param")"

# --- 3 -------------------------------------------------------------------------
sleep 2
dmesg | tail -n +"$((dmesg_start + 1))" > "$out/dmesg.txt"
grep -iE 'gts9wifi-camera|hi1337|dw9808|cci|camss|csiphy|vfe|csid|regulator|probe|error|fail|timeout' \
	"$out/dmesg.txt" > "$out/dmesg-camera.txt" || :
inf "dmesg: $(wc -l < "$out/dmesg.txt") new lines, $(grep -ciE 'error|fail|timeout' "$out/dmesg.txt") with error/fail/timeout"
grep -E 'model=0x' "$out/dmesg.txt" | while IFS= read -r l; do ok "sensor identified: ${l#*] }"; done
for d in /sys/bus/i2c/drivers/hi1337-gts9u/* /sys/bus/i2c/drivers/dw9808-vcm/*; do
	[ -e "$d/of_node" ] && inf "bound ${d##*/drivers/} at $(readlink -f "$d/of_node" | sed 's#.*/base##')"
done
for m in /dev/media*; do
	[ -e "$m" ] || continue
	media-ctl -d "$m" -p > "$out/media-ctl-${m##*/}.txt" 2>&1
	inf "$m: $(media-ctl -d "$m" -p 2>/dev/null | sed -n 's/^driver *//p' | head -1), $(grep -c '^- entity' "$out/media-ctl-${m##*/}.txt") entities"
done
ls /dev/media* >/dev/null 2>&1 || ck 'no /dev/media*: CAMSS did not register (its async notifier is still waiting for a sensor or lens)'
v4l2-ctl --list-devices > "$out/v4l2-list-devices.txt" 2>&1
for sd in /dev/v4l-subdev*; do
	[ -e "$sd" ] || continue
	name=$(cat "/sys/class/video4linux/${sd##*/}/name" 2>/dev/null)
	case "$name" in
	hi1337* | dw9808*) v4l2-ctl -d "$sd" --list-ctrls > "$out/ctrls-${sd##*/}.txt" 2>&1
		inf "$sd = $name" ;;
	esac
done

# --- 4 -------------------------------------------------------------------------
cam -l > "$out/cam-list.txt" 2>&1
cams=$(sed -n 's/^\([0-9][0-9]*\): .*/\1/p' "$out/cam-list.txt")
if [ -z "$cams" ]; then
	ck "cam -l lists no camera ($(tail -n 1 "$out/cam-list.txt"))"
fi
tmp=/run/gts9u-cameras-test
for c in $cams; do
	rm -rf "$tmp"; mkdir -p "$tmp"
	inf "camera $c: $(sed -n "s/^$c: //p" "$out/cam-list.txt")"
	cam -c "$c" -p > "$out/cam-$c-properties.txt" 2>&1
	if timeout 60 cam -c "$c" -C60 -s width=640,height=480,pixelformat=BGR888 \
		-F "$tmp/frame-#.ppm" > "$out/cam-$c-capture.txt" 2>&1; then
		last=$(ls "$tmp"/*.ppm 2>/dev/null | sort | tail -n 1)
		frames=$(ls "$tmp"/*.ppm 2>/dev/null | wc -l)
		if [ -n "$last" ]; then
			cp "$last" "$out/cam-$c-last.ppm"
			ok "camera $c: $frames frames through libcamera, last kept as cam-$c-last.ppm"
		else
			ck "camera $c: cam ran but wrote no frame (see cam-$c-capture.txt)"
		fi
	else
		ck "camera $c: cam failed or timed out (see cam-$c-capture.txt)"
	fi
	rm -rf "$tmp"
done

# --- 5 -------------------------------------------------------------------------
systemctl restart ubuntu-gts9u-camera-loopback.service
inf "camera table: $(tr '\n' ';' < /run/ubuntu-gts9u/camera-profile)"
# The owner's PipeWire must see cameras that appeared after it started.
nodes() { as_owner pw-cli ls Node 2>/dev/null | grep -c 'libcamera_input' || :; }
if [ "$(nodes)" = 0 ]; then
	as_owner systemctl --user restart wireplumber.service
	sleep 5
fi
inf "PipeWire libcamera nodes: $(nodes)"
systemctl start ubuntu-gts9u-camera-relays.service
sleep 4
inf "relays: $(systemctl show -p ActiveState --value ubuntu-gts9u-camera-relays.service); $(cat /sys/devices/virtual/video4linux/video*/name 2>/dev/null | tr '\n' ' ')"
if [ -e /dev/video20 ]; then
	# Five seconds: the relay opens the camera on the first reader and serves
	# its black splash until frames arrive and auto-exposure settles.
	rm -rf "$tmp"; mkdir -p "$tmp"
	timeout 30 v4l2-ctl -d /dev/video20 --stream-mmap --stream-count=150 \
		--stream-to="$tmp/video20.yuyv" > "$out/video20-capture.txt" 2>&1
	v4l2-ctl -d /dev/video20 --get-fmt-video >> "$out/video20-capture.txt" 2>&1
	size=$(stat -c %s "$tmp/video20.yuyv" 2>/dev/null || echo 0)
	if [ "$size" -ge 614400 ]; then
		python3 - "$tmp/video20.yuyv" "$out/video20-last.ppm" <<'PY' >> "$summary" 2>&1
import sys
data = open(sys.argv[1], 'rb').read()
w, h = 640, 480
frame = data[len(data) - w * h * 2:]
out = bytearray()
for i in range(0, len(frame), 4):
    y0, u, y1, v = frame[i], frame[i + 1], frame[i + 2], frame[i + 3]
    for y in (y0, y1):
        c, d, e = y - 16, u - 128, v - 128
        out += bytes(max(0, min(255, int(x))) for x in (
            1.164 * c + 1.596 * e, 1.164 * c - 0.392 * d - 0.813 * e, 1.164 * c + 2.017 * d))
open(sys.argv[2], 'wb').write(b'P6\n%d %d\n255\n' % (w, h) + bytes(out))
lumas = frame[0::2]
mean = sum(lumas) / len(lumas)
spread = max(lumas) - min(lumas)
verdict = 'ok   ' if spread > 32 else 'CHECK'
print('%s  /dev/video20 relay frame: mean luma %.0f, spread %d (a black splash has spread 0); kept as video20-last.ppm' % (verdict, mean, spread))
PY
	else
		ck "/dev/video20 gave $size bytes (see video20-capture.txt)"
	fi
	rm -rf "$tmp"
else
	ck 'no /dev/video20 after rebuilding the nodes'
fi
chgrp -R adm "$out" 2>/dev/null; chmod -R g+r "$out"
echo "results: $out (summary.txt)"
