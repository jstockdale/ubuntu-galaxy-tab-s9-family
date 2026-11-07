#!/bin/sh
# Host side: rebuild files/ and SHA256SUMS of this kit from the repository
# sources, so the kit installs exactly what the next device package and the
# bring-up overlay carry.  Run after any change to those files:
#   sh tools/inplace/boothygiene/make-kit.sh
set -eu

kit=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$kit/../../.." && pwd)
pkg=$repo/packaging/ubuntu-gts9u-device

# path in the package tree (and on the tablet)
device_files='
usr/lib/systemd/system/ubuntu-gts9u-rtc-restore.service
usr/lib/udev/rules.d/70-gts9u-rtc-restore.rules
usr/libexec/ubuntu-gts9u-rtc-offset
usr/lib/systemd/journald.conf.d/60-ubuntu-gts9u.conf
usr/lib/systemd/system-generators/ubuntu-gts9u-fingerprint-gate
usr/lib/systemd/system/ubuntu-gts9u-fingerprint-secure.service
usr/lib/systemd/system/ubuntu-gts9u-fingerprint-ui.service
usr/lib/systemd/system/ubuntu-gts9u-qcomtee.service
usr/lib/systemd/system/fprintd.service.d/10-gts9u-el721.conf
usr/share/gnome-shell/extensions/gts9u-fingerprint-overlay@agcarbajo/extension.js
usr/share/gnome-shell/extensions/gts9u-fingerprint-overlay@agcarbajo/metadata.json
usr/lib/systemd/system/ubuntu-gts9u-pstore-archive.service
usr/libexec/ubuntu-gts9u-pstore-archive
usr/lib/systemd/system/ubuntu-gts9u-panel-coldboot-recover.service
usr/lib/systemd/system/ubuntu-gts9u-pogo-firmware.service
'

rm -rf -- "$kit/files"
for rel in $device_files; do
	install -D -m "$( [ -x "$pkg/$rel" ] && echo 0755 || echo 0644 )" \
		"$pkg/$rel" "$kit/files/$rel"
done
# The opt-in bring-up logger (GTS9U_BRINGUP_SDLOG=1 images only; install.sh
# replaces it only where it is already installed).
install -D -m 0755 "$repo/scripts/bringup-sdlog/gts9u-bringup-sdlog" \
	"$kit/files/usr/libexec/gts9u-bringup-sdlog"
for unit in gts9u-bringup-sdlog.service gts9u-bringup-sdlog.timer; do
	install -D -m 0644 "$repo/scripts/bringup-sdlog/$unit" \
		"$kit/files/usr/lib/systemd/system/$unit"
done

(cd "$kit" && find . -type f ! -name SHA256SUMS ! -name make-kit.sh | sed 's#^\./##' |
	LC_ALL=C sort | xargs -d '\n' sha256sum > SHA256SUMS)
echo "kit rebuilt: $(find "$kit/files" -type f | wc -l) payload files"
