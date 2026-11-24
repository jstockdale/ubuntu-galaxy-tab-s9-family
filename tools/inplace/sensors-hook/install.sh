#!/bin/sh
# The sensors' system-sleep hook in place, on an SM-X710 or SM-X910 that
# cannot take a new device package yet: the three files of ubuntu-gts9u-device
# that make it, exactly as the package source of the checkout that packed the
# kit has them (make-kit.sh):
#   usr/lib/systemd/system-sleep/ubuntu-gts9u-sensors   the hook: the sensor
#       clients stopped before every system sleep, each stop within a bound,
#       and the resume unit reset and restarted after it
#   usr/libexec/ubuntu-gts9u-sensors-resume              the resume script
#   usr/lib/systemd/system/ubuntu-gts9u-sensors-resume.service
#                                                         its unit, which tries
#       a failed recovery again, three starts at most
#
#   sudo sh ~/sensors-hook/install.sh    install, or upgrade an earlier revision
#                                        in place; no reboot
#   sh ~/sensors-hook/verify.sh          no sudo: the files, the unit as systemd
#                                        has it, the sensors, this boot's sleeps
#   sudo sh ~/sensors-hook/cycle.sh hand|freezer
#                                        the hook's two steps with no sleep, or
#                                        around one pm_test=freezer cycle
#   sudo sh ~/sensors-hook/rollback.sh   the device package's own files back;
#                                        no reboot
#
# Nothing is changed before every check has passed: root, the board (either),
# the kit's checksums, dpkg not busy, the package installed, and each of the
# three files there, owned by the package and holding either a version some
# device package has shipped ($known below) or a revision of this kit (the
# record in $backup/revisions).  Anything else is refused: a device package
# with this fix or a later one needs no kit, and a file changed by hand is for
# its owner to look at.  Where the package's files are this revision's
# already, nothing is done.
#
# The first install saves the three files as they are (cp -a) in
# /var/backups/ubuntu-gts9u-sensors-hook, with the package's version, before
# it writes anything else.  A later revision installs over an earlier one in
# place, with no rollback in between: the saved originals stay the way back,
# the record gets the new revision's lines, and the files are replaced.
# Each file is written under a hidden name in its directory (systemd-sleep
# and the unit loader skip those), root-owned with its mode set, then renamed
# over the old one: the hook first, then the resume script, then the unit and
# a daemon-reload, so that every state on the way works (the new hook with the
# old unit; the unit's retries only once the hook that resets them is there).
# rollback.sh goes the other way.  Stopped by an error or a power cut, each
# file is whole, old or new, and install.sh run again finishes the job.
#
# No reboot and no restart: the next sleep runs the new hook, and the unit is
# reloaded here (the next start uses it).
#
# A device package installed later replaces the three files with its own (they
# are not conffiles), and the kit is then out of the way: verify.sh says whose
# they are, install.sh saves that package's files afresh if it is needed again,
# and rollback.sh only retires the backup.  dpkg --verify lists the three as
# changed while the kit's files are in place.
set -eu
export LC_ALL=C

kit=$(cd "$(dirname "$0")" && pwd)
# The system's root: empty on a tablet (the tests point it at a scratch tree).
root=
backup=$root/var/backups/ubuntu-gts9u-sensors-hook
log=$root/var/log/gts9u-sensors-hook.log
pkg=ubuntu-gts9u-device
# The files, in the order install.sh writes them, with their modes.
files='usr/lib/systemd/system-sleep/ubuntu-gts9u-sensors 0755
usr/libexec/ubuntu-gts9u-sensors-resume 0755
usr/lib/systemd/system/ubuntu-gts9u-sensors-resume.service 0644'
# Every version of them a device package has shipped since the resume unit
# (504ff29, 2026-08-03) and the resume script (d6a04f9, 2026-09-09) took their
# present form, by sha256: the hook from before ac38911 (device 2.69 on the
# SM-X710 test unit and 2.72 on the SM-X910 one, 2026-11-24) and ac38911's.
known='usr/lib/systemd/system-sleep/ubuntu-gts9u-sensors e5546e9f31d7e05f2675392b21c2c26689a52f6d432666c068c3c8ef802dc657
usr/lib/systemd/system-sleep/ubuntu-gts9u-sensors 725fc185ee43bc1021621303905f3f1ced07451ad3ccae733cba358d1ec1d64e
usr/libexec/ubuntu-gts9u-sensors-resume 4586d748ef9e62c5d6d4a36f930fddedc971ed0497a1146c614e3785b814ab02
usr/lib/systemd/system/ubuntu-gts9u-sensors-resume.service 1898678433dd5d58a3e1b8ec6687517128a224148c6ee77219e67ea487e8f7da'

say() {
	line="$(date '+%F %T') sensors-hook install: $*"
	echo "$line" >> "$log"
	echo "$line" 2>/dev/null || :
}
die() { say "STOP: $*"; exit 1; }
sha() { sha256sum < "$1" | cut -d' ' -f1; }
# kit_sha REL: this revision's sha256 of REL, from the kit's SHA256SUMS.
kit_sha() { awk -v f="files/$1" '$2 == f {print $1}' "$kit/SHA256SUMS"; }
# shipped REL SHA: a version of REL that a device package has shipped.
shipped() { printf '%s\n' "$known" | awk -v f="$1" -v s="$2" '$1 == f && $2 == s {found = 1} END {exit !found}'; }
# kit_rev REL SHA: a revision of this kit, as the record has them.
kit_rev() { awk -v f="$1" -v s="$2" '$2 == f && $3 == s {found = 1} END {exit !found}' "$backup/revisions" 2>/dev/null; }

case "${1:-}" in
'') ;;
*) echo 'usage: sudo sh install.sh' >&2; exit 2 ;;
esac
[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> "$root/run/gts9u-sensors-hook.lock"
flock -n 9 || { echo 'install.sh, rollback.sh or cycle.sh is still running; see /var/log/gts9u-sensors-hook.log' >&2; exit 1; }
trap '' HUP PIPE
touch "$log" && chgrp adm "$log" && chmod 0640 "$log" || { echo "cannot write $log" >&2; exit 1; }

# --- checks: nothing changes before all of them pass ---------------------------
board=$(tr '\0' '\n' < "$root/proc/device-tree/compatible" 2>/dev/null |
	grep -x -e 'samsung,gts9wifi' -e 'samsung,gts9uwifi' | head -n 1) || board=
[ -n "$board" ] || die 'this is neither an SM-X710 (samsung,gts9wifi) nor an SM-X910 (samsung,gts9uwifi)'
revision=$(cat "$kit/REVISION" 2>/dev/null) || revision=
case $revision in ''|*[!0-9]*) die 'the kit has no REVISION' ;; esac
(cd "$kit" && sha256sum -c --quiet SHA256SUMS) || die 'the kit does not match its checksums; nothing was changed'
for c in systemctl dpkg-query timeout flock install sha256sum; do
	command -v "$c" >/dev/null 2>&1 || die "$c is missing; nothing was changed"
done
fuser "$root/var/lib/dpkg/lock-frontend" "$root/var/lib/dpkg/lock" >/dev/null 2>&1 &&
	die 'dpkg is busy (apt, unattended-upgrades or PackageKit); nothing was changed, try again when it is done'
status=$(dpkg-query -W -f '${Status} ${Version}' "$pkg" 2>/dev/null) || status=
case $status in
'install ok installed '*) version=${status##* } ;;
*) die "$pkg is not installed ('$status'); nothing was changed" ;;
esac
say "boot $(cat "$root/proc/sys/kernel/random/boot_id" 2>/dev/null), $board, $pkg $version, kit revision $revision ($(cat "$kit/SOURCE" 2>/dev/null))"

same=0 fresh=1
while read -r rel mode; do
	target=$root/$rel
	new=$(kit_sha "$rel")
	[ -n "$new" ] && [ -f "$kit/files/$rel" ] || die "the kit has no files/$rel"
	[ -f "$target" ] || die "/$rel is missing: not a device package with the sensors' resume unit; nothing was changed"
	owner=$(dpkg-query -S "/$rel" 2>/dev/null | cut -d: -f1) || owner=
	[ "$owner" = "$pkg" ] || die "/$rel is not $pkg's (dpkg-query -S says '${owner:-nobody}'); nothing was changed"
	cur=$(sha "$target")
	if [ "$cur" = "$new" ]; then
		same=$((same + 1))
	elif shipped "$rel" "$cur"; then
		:
	elif kit_rev "$rel" "$cur"; then
		fresh=0
	else
		die "/$rel is neither a version a device package has shipped nor a revision of this kit ($cur): a package with this fix or a later one, or a change by hand; nothing was changed"
	fi
done <<FILES
$files
FILES
if [ "$same" = 3 ]; then
	if [ -e "$backup/complete" ]; then
		say "revision $revision is installed already ($backup); nothing was changed"
	else
		say "$pkg $version carries this revision's files already: nothing to install"
	fi
	exit 0
fi

# --- the originals, once -------------------------------------------------------
if [ -e "$backup/complete" ]; then
	saved=$(cat "$backup/package" 2>/dev/null) || saved=
	if [ "$saved" != "$version" ] && [ "$fresh" = 1 ]; then
		# A package installed since the last revision put its own files back
		# (none of the kit's is left): those are the originals now.
		mv "$backup" "$backup.package-$saved-$(date +%Y%m%d-%H%M%S)" || die "cannot retire $backup"
		say "$pkg is $version now, not $saved: its files are the originals now, the backup of $saved's is retired"
	else
		while read -r rel mode; do
			[ -f "$backup/files/$rel" ] &&
				awk -v f="$rel" -v s="$(sha "$backup/files/$rel")" '$1 == f && $2 == s {found = 1} END {exit !found}' \
					"$backup/originals" ||
				die "the original /$rel saved in $backup is missing or changed; nothing was changed"
		done <<FILES
$files
FILES
		say "upgrade in place: the originals saved in $backup on $(cat "$backup/date" 2>/dev/null) stay the way back"
	fi
elif [ "$fresh" = 0 ]; then
	die "a file holds a revision of this kit, but $backup has no complete backup; nothing was changed"
fi
if [ ! -e "$backup/complete" ]; then
	[ ! -e "$backup" ] || die "$backup exists without its 'complete' mark (not from this script); look at it first"
	new=$backup.new
	rm -rf -- "$new"
	mkdir -p "$new/files" && chmod 0700 "$new" || die "cannot create $new"
	while read -r rel mode; do
		mkdir -p "$new/files/${rel%/*}" && cp -a "$root/$rel" "$new/files/$rel" || die "saving /$rel failed"
		echo "$rel $(sha "$new/files/$rel")" >> "$new/originals"
	done <<FILES
$files
FILES
	echo "$version" > "$new/package"
	date '+%F %T' > "$new/date"
	: > "$new/revisions"
	: > "$new/complete"
	# The way back is on the disk before anything else changes.
	sync
	mv "$new" "$backup" || die "cannot move $new into place"
	sync
	say "the three files of $pkg $version saved in $backup"
fi

# --- the files -----------------------------------------------------------------
trap '' HUP PIPE INT TERM
finished=0
trap '[ "$finished" = 1 ] || say "STOP: install.sh ended early; each file is whole, old or new: run install.sh again to finish, or rollback.sh"' EXIT
# What this revision writes, for the next revision's install.sh and for
# rollback.sh, on the disk before any of it is written.
while read -r rel mode; do
	echo "$revision $rel $(kit_sha "$rel")" >> "$backup/revisions"
done <<FILES
$files
FILES
sync
while read -r rel mode; do
	target=$root/$rel
	tmp=${target%/*}/.${target##*/}.sensors-hook-new
	install -o 0 -g 0 -m "$mode" "$kit/files/$rel" "$tmp" || die "writing $tmp failed"
	mv -f "$tmp" "$target" || die "renaming $tmp to /$rel failed"
	[ "$(sha "$target")" = "$(kit_sha "$rel")" ] || die "/$rel does not read back as the kit's"
	say "/$rel: revision $revision ($mode, root)"
	case $rel in
	*.service) systemctl daemon-reload || die 'systemctl daemon-reload failed: the files are in place; run it by hand' ;;
	esac
done <<FILES
$files
FILES
sync

# --- what is in place now ------------------------------------------------------
sh -n "$root/usr/lib/systemd/system-sleep/ubuntu-gts9u-sensors" || die 'the hook in place does not parse'
props=$(systemctl show -p Restart -p StartLimitBurst -p TimeoutStopUSec -p NeedDaemonReload \
	ubuntu-gts9u-sensors-resume.service 2>/dev/null | tr '\n' ' ')
say "the resume unit as systemd has it: $props"
case " $props" in
*' Restart=on-failure '*) ;;
*) say 'CHECK: systemd does not have the unit with Restart=on-failure (see the line above)' ;;
esac
finished=1
say "done: revision $revision in place; the next system sleep runs the new hook, no reboot needed (sh $kit/verify.sh)"
