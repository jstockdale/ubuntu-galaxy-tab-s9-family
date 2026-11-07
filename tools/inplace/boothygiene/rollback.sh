#!/bin/sh
# Undo ~/boothygiene/install.sh: put back the 2.55 (and bring-up overlay)
# files from /var/backups/ubuntu-gts9u-boothygiene, remove the files the kit
# added, then reboot.  The archived ramoops records in
# /var/lib/ubuntu-gts9u/pstore stay (delete them by hand if unwanted).
#
#   sudo sh ~/boothygiene/rollback.sh && sudo reboot
set -eu

backup=/var/backups/ubuntu-gts9u-boothygiene
log=/var/log/gts9u-boothygiene.log

say() {
	line="$(date '+%F %T') rollback: $*"
	echo "$line" >> "$log"
	echo "$line" 2>/dev/null || :
}
die() { say "STOP: $*"; exit 1; }
finished=0
trap '[ "$finished" = 1 ] || say "NOTE: rollback ended early; rebooting is safe. Run rollback.sh again."' EXIT

[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; trap - EXIT; exit 1; }
exec 9> /run/gts9u-boothygiene.lock
flock -n 9 || { echo 'install.sh or rollback.sh is already running; see /var/log/gts9u-boothygiene.log' >&2; trap - EXIT; exit 1; }
trap '' HUP PIPE
[ -e "$backup/complete" ] || die "no complete backup in $backup: install.sh never ran (or was rolled back already)"

# Units first, while their files still exist.
systemctl disable ubuntu-gts9u-pstore-archive.service >> "$log" 2>&1 || true

# Files the kit created.
while IFS= read -r rel; do
	[ -n "$rel" ] || continue
	rm -f -- "/$rel"
	say "removed /$rel"
done < "$backup/created"
# Files it replaced: the originals, with their owner and mode.
(cd "$backup/files" && find . -type f | sed 's#^\./##') | while IFS= read -r rel; do
	cp -a "$backup/files/$rel" "/$rel.boothygiene-old"
	mv -f "/$rel.boothygiene-old" "/$rel"
	say "restored /$rel"
done
if [ "$(cat "$backup/clock-epoch-existed" 2>/dev/null)" = no ]; then
	rm -f /usr/lib/clock-epoch
	say 'removed /usr/lib/clock-epoch'
fi

# The generator is gone: the reload unmasks fprintd for the rest of this boot.
systemctl daemon-reload || die 'systemctl daemon-reload failed; rebooting is still safe'
say "fprintd: $(systemctl show -p LoadState --value fprintd.service)"

mv "$backup" "$backup.rolled-back-$(date +%Y%m%d-%H%M%S)" || die "cannot retire $backup"
finished=1
say 'done; reboot now (sudo reboot)'
