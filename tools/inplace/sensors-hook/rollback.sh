#!/bin/sh
# Undo ~/sensors-hook/install.sh: the device package's own three files back,
# from what the first install saved, after any number of revisions installed
# in place, the last one finished or not.  No reboot: the next sleep runs the
# package's hook again, and the unit is reloaded here.
#   sudo sh ~/sensors-hook/rollback.sh
#
# Each file holding a revision of this kit (the record in the backup) gets
# its saved original back, the unit first and a daemon-reload, then the
# resume script, then the hook: the reverse of install.sh, so that every
# state on the way works.  A file that already holds its original is left.
# A file holding anything else (a change by hand) stops the rollback before
# it changes anything.  When the device package has been installed again
# since the kit saved its files (another version), every file is that
# package's, and only the backup is retired.  The backup is never deleted:
# it is renamed .rolled-back-DATE.
set -eu
export LC_ALL=C

# The system's root: empty on a tablet (the tests point it at a scratch tree).
root=
backup=$root/var/backups/ubuntu-gts9u-sensors-hook
log=$root/var/log/gts9u-sensors-hook.log
pkg=ubuntu-gts9u-device
# The files, in the order rollback.sh restores them.
files='usr/lib/systemd/system/ubuntu-gts9u-sensors-resume.service
usr/libexec/ubuntu-gts9u-sensors-resume
usr/lib/systemd/system-sleep/ubuntu-gts9u-sensors'

say() {
	line="$(date '+%F %T') sensors-hook rollback: $*"
	echo "$line" >> "$log"
	echo "$line" 2>/dev/null || :
}
die() { say "STOP: $*"; exit 1; }
sha() { sha256sum < "$1" | cut -d' ' -f1; }
kit_rev() { awk -v f="$1" -v s="$2" '$2 == f && $3 == s {found = 1} END {exit !found}' "$backup/revisions" 2>/dev/null; }
retire() {
	mv "$backup" "$backup.rolled-back-$(date +%Y%m%d-%H%M%S)" || die "cannot retire $backup"
}

case "${1:-}" in
'') ;;
*) echo 'usage: sudo sh rollback.sh' >&2; exit 2 ;;
esac
[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> "$root/run/gts9u-sensors-hook.lock"
flock -n 9 || { echo 'install.sh, rollback.sh or cycle.sh is still running; see /var/log/gts9u-sensors-hook.log' >&2; exit 1; }
trap '' HUP PIPE INT TERM
touch "$log" && chgrp adm "$log" && chmod 0640 "$log" || { echo "cannot write $log" >&2; exit 1; }
[ -e "$backup/complete" ] || die "no complete backup in $backup: install.sh never ran (or was rolled back already); nothing was changed"
saved=$(cat "$backup/package" 2>/dev/null) || saved=
status=$(dpkg-query -W -f '${Status} ${Version}' "$pkg" 2>/dev/null) || status=
version=${status##* }
if [ "$version" != "$saved" ]; then
	retire
	say "$pkg is '$version' now, not $saved, whose files the kit saved: the files in place are that package's, left as they are; the backup is retired"
	exit 0
fi

restore= unknown=
while read -r rel; do
	target=$root/$rel
	[ -f "$backup/files/$rel" ] || die "the original /$rel is not in $backup; nothing was changed"
	cur=$(sha "$target" 2>/dev/null) || cur=missing
	if [ "$cur" = "$(sha "$backup/files/$rel")" ]; then
		say "/$rel holds its original already"
	elif kit_rev "$rel" "$cur"; then
		restore="$restore $rel"
	else
		unknown="$unknown /$rel"
	fi
done <<FILES
$files
FILES
[ -z "$unknown" ] ||
	die "neither the original nor a revision of this kit (changed by hand?):$unknown; nothing was changed"

finished=0
trap '[ "$finished" = 1 ] || say "STOP: rollback.sh ended early; each file is whole, old or new: run rollback.sh again"' EXIT
for rel in $restore; do
	target=$root/$rel
	tmp=${target%/*}/.${target##*/}.sensors-hook-old
	# cp -a: the original's owner and mode, as the package installed it.
	cp -a "$backup/files/$rel" "$tmp" || die "copying the original of /$rel failed"
	mv -f "$tmp" "$target" || die "renaming $tmp to /$rel failed"
	say "/$rel: the original back ($(stat -c '%a %U:%G' "$target" 2>/dev/null))"
	case $rel in
	*.service) systemctl daemon-reload || die 'systemctl daemon-reload failed: the files are in place; run it by hand' ;;
	esac
done
sync
retire
finished=1
say "done: $pkg $saved's own files are in place; no reboot needed"
