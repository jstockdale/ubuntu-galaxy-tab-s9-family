#!/bin/sh
# Boot hygiene for the SM-X710, in place over ubuntu-gts9u-device 2.55, until
# the next device package carries the same files.  No kernel, boot image or
# package changes; one reboot to see the effect.
#
#   sudo sh ~/boothygiene/install.sh     install; then sudo reboot
#   sh ~/boothygiene/verify.sh           after the reboot (no sudo)
#   sudo sh ~/boothygiene/rollback.sh    undo all of it; then sudo reboot
#
# What it changes (see README.md):
#   1. ubuntu-gts9u-rtc-restore runs before journald and once per boot
#      (RemainAfterExit), so no boot loses its first ~39 s of journal any more;
#      rtc-offset save keeps the smaller offset and refreshes
#      /usr/lib/clock-epoch; journald gets SystemMaxUse=1G
#   2. the fingerprint stack is gated to the SM-X910: three units get
#      ConditionFirmware, a generator masks fprintd on other boards (GDM no
#      longer waits 25 s for net.reactivated.Fprint), the overlay extension's
#      fingerprint part stays inactive (its GNOME 46 guards stay on)
#   3. ubuntu-gts9u-pstore-archive keeps the ramoops records of the last 10
#      boots in /var/lib/ubuntu-gts9u/pstore (readable by group adm)
#   4. ubuntu-gts9u-pogo-firmware runs after the cold-boot panel cycle
#   5. only where the bring-up microSD logger is installed: boot-only (four
#      snapshots in the first 3 minutes), newest 20 boots on the card, and it
#      copies /var/lib/systemd/pstore
#
# Every line goes to /var/log/gts9u-boothygiene.log; hangups are ignored, so a
# dropped SSH session does not cut the run short.  Originals are backed up once
# in /var/backups/ubuntu-gts9u-boothygiene.  Nothing here touches boot,
# vendor_boot, modules or the running journald; rebooting is safe at any point.
set -eu

kit=$(cd "$(dirname "$0")" && pwd)
backup=/var/backups/ubuntu-gts9u-boothygiene
log=/var/log/gts9u-boothygiene.log
expect_device=2.55

say() {
	line="$(date '+%F %T') $*"
	echo "$line" >> "$log"
	echo "$line" 2>/dev/null || :
}
die() { say "STOP: $*"; exit 1; }
finished=0
trap '[ "$finished" = 1 ] || say "NOTE: install.sh ended early; rebooting is safe (each file is either the 2.55 one or the new one). Fix the cause and run it again, or run rollback.sh."' EXIT

[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; trap - EXIT; exit 1; }
exec 9> /run/gts9u-boothygiene.lock
flock -n 9 || { echo 'install.sh or rollback.sh is already running; see /var/log/gts9u-boothygiene.log' >&2; trap - EXIT; exit 1; }
trap '' HUP PIPE

tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9wifi' ||
	die 'this is not an SM-X710 (the BEFORE.md5 list is for its 2.55 install)'
say "boothygiene install: boot $(cat /proc/sys/kernel/random/boot_id)"

# --- 1. checks: nothing is changed before all of them pass ------------------
version=$(dpkg-query -W -f '${Version}' ubuntu-gts9u-device 2>/dev/null || true)
[ "$version" = "$expect_device" ] ||
	die "ubuntu-gts9u-device is '$version', not $expect_device: a newer package already carries these files (or this kit was not made for it)"
fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock >/dev/null 2>&1 &&
	die 'dpkg is busy (apt, unattended-upgrades or PackageKit); nothing was changed, try again later'
(cd "$kit" && sha256sum -c --quiet SHA256SUMS) || die 'kit checksum mismatch; nothing was changed'

logger_installed=no
[ -e /usr/libexec/gts9u-bringup-sdlog ] && logger_installed=yes
say "bring-up microSD logger installed: $logger_installed"

payload=$(cd "$kit/files" && find . -type f | sed 's#^\./##' | LC_ALL=C sort)
selected=
bad=
for rel in $payload; do
	case "$rel" in
	*gts9u-bringup-sdlog*) [ "$logger_installed" = yes ] || continue ;;
	esac
	selected="$selected $rel"
	target="/$rel"
	new=$(md5sum < "$kit/files/$rel" | cut -d' ' -f1)
	before=$(awk -v f="$rel" '$2 == f { print $1 }' "$kit/BEFORE.md5")
	if [ -e "$target" ]; then
		now=$(md5sum < "$target" | cut -d' ' -f1)
		if [ "$now" != "$new" ] && [ "$now" != "$before" ]; then
			bad="$bad $target"
		fi
	elif [ -n "$before" ]; then
		bad="$bad $target(missing)"
	fi
done
[ -z "$bad" ] || die "these differ from both the 2.55 file and the kit's (changed by hand?), nothing was changed:$bad"
say 'checks passed'

# --- 2. backups, once -------------------------------------------------------
if [ ! -e "$backup/complete" ]; then
	rm -rf -- "$backup"
	mkdir -p "$backup/files"
	: > "$backup/created"
	for rel in $selected; do
		if [ -e "/$rel" ]; then
			mkdir -p "$backup/files/$(dirname "$rel")"
			cp -a "/$rel" "$backup/files/$rel"
		else
			echo "$rel" >> "$backup/created"
		fi
	done
	[ -e /usr/lib/clock-epoch ] && echo yes > "$backup/clock-epoch-existed" ||
		echo no > "$backup/clock-epoch-existed"
	echo "$selected" > "$backup/selected"
	touch "$backup/complete"
	say "backups in $backup ($(find "$backup/files" -type f | wc -l) files; $(wc -l < "$backup/created") new)"
else
	say "backups already in $backup (from an earlier run); kept"
fi

# --- 3. files ---------------------------------------------------------------
# Root-owned explicitly (install -o 0 -g 0): files copied as the user must
# never land in /usr with the user as owner.
for rel in $selected; do
	if [ -x "$kit/files/$rel" ]; then mode=0755; else mode=0644; fi
	install -D -o 0 -g 0 -m "$mode" "$kit/files/$rel" "/$rel.boothygiene-new"
	mv -f "/$rel.boothygiene-new" "/$rel"
	say "installed /$rel ($mode)"
done
if [ ! -e /usr/lib/clock-epoch ]; then
	touch /usr/lib/clock-epoch
	say 'created /usr/lib/clock-epoch (systemd clock epoch = now)'
fi

# --- 4. systemd -------------------------------------------------------------
systemctl daemon-reload || die 'systemctl daemon-reload failed; rebooting is still safe'
systemctl enable ubuntu-gts9u-pstore-archive.service >> "$log" 2>&1 ||
	die 'enabling ubuntu-gts9u-pstore-archive.service failed'
say "pstore archive: $(systemctl is-enabled ubuntu-gts9u-pstore-archive.service 2>&1)"
# The generator ran with the reload: fprintd is masked for this boot already.
say "fprintd: $(systemctl show -p LoadState --value fprintd.service)"
# Clear what the stack left failed on this boot.  The broker is left running
# until the reboot: the Shell running now still has the old overlay, which
# pulses it twice a second, and with the broker stopped every pulse would
# become a D-Bus activation that the new condition refuses.
systemctl reset-failed fprintd.service ubuntu-gts9u-fingerprint-secure.service >> "$log" 2>&1 || true
if [ "$logger_installed" = yes ]; then
	# Stop this boot's every-minute dumps now; the new timer starts with the
	# next boot (timers.target) and fires at 45, 90 and 180 s only.
	systemctl stop gts9u-bringup-sdlog.timer >> "$log" 2>&1 || true
	say "bring-up logger timer: $(systemctl is-active gts9u-bringup-sdlog.timer 2>&1) until the reboot"
fi

finished=1
say 'done; reboot now (sudo reboot), then run: sh ~/boothygiene/verify.sh'
