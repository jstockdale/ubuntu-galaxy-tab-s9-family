#!/bin/sh
# Read-only checks of the camera stack on the SM-X710 (no sudo; the owner is
# in group adm, so the journal is readable).  Each line is "ok", "CHECK" (look
# at it) or "info".
#
#   sh ~/cameras-kit/verify.sh
set -u

kit=$(cd "$(dirname "$0")" && pwd)
ok() { echo "  ok     $*"; }
ck() { echo "  CHECK  $*"; }
inf() { echo "  info   $*"; }
hdr() { echo "== $*"; }
param=/sys/module/gts9wifi_camera/parameters/enable
table=/run/ubuntu-gts9u/camera-profile

hdr board
inf "$(tr '\0' ' ' < /proc/device-tree/compatible), $(uname -r), ubuntu-gts9u-device $(dpkg-query -W -f '${Version}' ubuntu-gts9u-device 2>/dev/null)"

hdr 'kit files'
bad=0
while read -r sum path; do
	case "$path" in files/*) ;; *) continue ;; esac
	if [ "$(sha256sum "/${path#files/}" 2>/dev/null | cut -d' ' -f1)" != "$sum" ]; then
		ck "/${path#files/} differs from the kit"
		bad=1
	fi
done < "$kit/SHA256SUMS"
[ "$bad" = 0 ] && ok "all $(grep -c ' files/' "$kit/SHA256SUMS") kit files in place"
for path in $(cat "$kit/REMOVED"); do
	[ -e "/$path" ] && ck "/$path is back (a package reinstall?)" || ok "/$path gone"
done

hdr units
for unit in ubuntu-gts9u-camera-loopback.service ubuntu-gts9u-camera-enable.service ubuntu-gts9u-camera-relays.service; do
	inf "$unit: $(systemctl is-enabled "$unit" 2>/dev/null), $(systemctl show -p ActiveState --value "$unit"), condition $(systemctl show -p ConditionResult --value "$unit"), result $(systemctl show -p Result --value "$unit")"
done
[ "$(systemctl is-enabled ubuntu-gts9u-camera-loopback.service 2>/dev/null)" = enabled ] &&
	ok 'loopback loader enabled' || ck 'loopback loader not enabled'
journalctl -b -u ubuntu-gts9u-camera-loopback.service -u ubuntu-gts9u-camera-enable.service \
	--no-pager -o cat 2>/dev/null | grep -E 'ubuntu-gts9u-camera-(loopback|enable):' | tail -n 4 |
	while IFS= read -r l; do inf "journal: $l"; done

hdr 'kernel switch'
if [ -r "$param" ]; then
	inf "gts9wifi_camera.enable = $(cat "$param")"
	for d in /sys/bus/i2c/drivers/hi1337-gts9u/* /sys/bus/i2c/drivers/dw9808-vcm/*; do
		[ -e "$d/of_node" ] && inf "bound: ${d##*/drivers/} -> $(readlink -f "$d/of_node" | sed 's#.*/base##')"
	done
	[ -e /sys/bus/platform/drivers/qcom-camss/acb7000.isp ] && inf 'CAMSS bound' || inf 'CAMSS not bound'
	ls /dev/media* >/dev/null 2>&1 && inf "media devices: $(ls /dev/media* | tr '\n' ' ')"
else
	inf 'no gts9wifi_camera switch in this kernel: the X710 cameras cannot be enabled (expected before the camera kernel)'
fi
[ -e /etc/ubuntu-gts9u/cameras ] && inf "/etc/ubuntu-gts9u/cameras: $(grep -v '^#' /etc/ubuntu-gts9u/cameras | tr '\n' ' ')"

hdr 'camera table and V4L2 nodes'
if [ -e "$table" ]; then
	n=$(wc -l < "$table")
	inf "table: $n camera(s)"
	while IFS= read -r l; do inf "  $l"; done < "$table"
else
	ck "$table missing (loader never ran this boot)"
	n=0
fi
nodes=$(for f in /sys/devices/virtual/video4linux/video*/name; do [ -r "$f" ] && printf '%s:%s ' "$(basename "$(dirname "$f")")" "$(cat "$f")"; done)
if printf '%s' "$nodes" | grep -q 'GTS9U-'; then
	ck "SM-X910 (GTS9U-*) nodes present on this SM-X710: $nodes"
elif [ "$n" = 0 ]; then
	[ -z "$nodes" ] && ok 'no loopback camera nodes (no camera enabled)' || ck "loopback nodes with an empty table: $nodes"
else
	inf "loopback nodes: $nodes"
	[ "$(systemctl show -p ActiveState --value ubuntu-gts9u-camera-relays.service)" = active ] &&
		ok 'relays running for the enabled cameras' || ck 'cameras in the table but the relays are not running'
fi
if [ "$n" = 0 ]; then
	[ "$(systemctl show -p ActiveState --value ubuntu-gts9u-camera-relays.service)" = active ] &&
		ck 'relays running with an empty table' || ok 'relays not running (empty table)'
fi
command -v v4l2-ctl >/dev/null 2>&1 && v4l2-ctl --list-devices 2>/dev/null |
	sed -n 's/^\([^ \t].*\):$/\1/p' | while IFS= read -r l; do inf "v4l2: $l"; done
lsmod | grep -qE '^v4l2loopback ' && inf "v4l2loopback loaded ($(lsmod | awk '$1 == "v4l2loopback" { print $3 }') users)" || inf 'v4l2loopback not loaded'
