#!/bin/sh
# Undo ~/suspendkit/install.sh: disable and remove the ADSP-start sleep lock
# (ubuntu-gts9u-adsp-start-inhibit.service and its helper), and put back
# whatever was there before (normally nothing).  No reboot needed: without the
# unit the next boot simply starts the DSP with no lock, as before.
#
#   sudo sh ~/suspendkit/rollback.sh
#
# A file that a device package owns by now is left alone: the package carries
# the lock, and removing the package's file is the package's business.
#
# The reliability runner needs no rollback: a run removes its sleep.conf.d
# drop-in and RTC alarm and restores mem_sleep when it ends (a reboot clears
# /run in any case).  An interconnect release (run --release-icc) ends with the
# next reboot.
set -eu

kit=$(cd "$(dirname "$0")" && pwd)
log=/var/log/gts9u-suspendkit-install.log
backup=/var/backups/ubuntu-gts9u-suspendkit
unit=ubuntu-gts9u-adsp-start-inhibit.service
helper=/usr/libexec/ubuntu-gts9u-adsp-start-inhibit
unitfile=/usr/lib/systemd/system/$unit
wants=/etc/systemd/system/ubuntu-gts9u-adsp-boot.service.wants/$unit

say() {
	line="$(date '+%F %T') rollback: $*"
	echo "$line" >> "$log"
	echo "$line" 2>/dev/null || :
}
die() { say "STOP: $*"; exit 1; }
owner_of() { dpkg -S "$1" 2>/dev/null | cut -d: -f1; }

[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> /run/gts9u-suspendkit-install.lock
flock -n 9 || { echo 'install.sh or rollback.sh is still running' >&2; exit 1; }
trap '' HUP PIPE INT TERM
touch "$log" && chgrp adm "$log" && chmod 0640 "$log" || { echo "cannot write $log" >&2; exit 1; }
[ -e "$backup/complete" ] || die "no complete backup in $backup: install.sh never ran (or was rolled back already)"

packaged=0
for f in "$helper" "$unitfile"; do
	o=$(owner_of "$f")
	if [ -n "$o" ]; then
		say "$f now ships in $o: left in place"
		packaged=1
	fi
done

if [ "$packaged" = 0 ]; then
	systemctl stop "$unit" >/dev/null 2>&1 || :
	systemctl disable "$unit" >/dev/null 2>&1 || :
	rm -f "$wants"
	for f in "$helper" "$unitfile"; do
		if grep -qx "$f" "$backup/existed"; then
			cp -a "$backup/${f##*/}" "$f" || die "restoring $f failed"
			say "restored the earlier $f"
		elif [ -e "$f" ]; then
			cmp -s "$f" "$kit/files/${f##*/}" ||
				say "note: $f differs from the kit's copy (changed since install); removing it anyway"
			rm -f "$f" || die "removing $f failed"
			say "removed $f"
		fi
	done
	if grep -qx "$wants" "$backup/existed" && [ -e "$unitfile" ]; then
		systemctl enable "$unit" >/dev/null 2>&1 || say "note: could not re-enable the earlier $unit"
	fi
	systemctl daemon-reload || die 'systemctl daemon-reload failed'
fi
mv "$backup" "$backup.rolled-back-$(date +%Y%m%d-%H%M%S)" || die "cannot retire $backup"
say 'done; no reboot needed'
