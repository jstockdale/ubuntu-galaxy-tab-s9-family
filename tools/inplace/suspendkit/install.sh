#!/bin/sh
# Put the ADSP-start sleep lock on an installed SM-X710 in place, ahead of the
# device package that will carry it (ubuntu-gts9u-adsp-start-inhibit.service
# and its helper, exactly as the repository ships them).  The reliability
# runner and the ladder need no install: they run from this directory.
#
#   sudo sh ~/suspendkit/install.sh     install, enable, smoke-test; no reboot needed
#   sh ~/suspendkit/verify.sh           no sudo; after the next boot it shows the lock at work
#   sudo sh ~/suspendkit/rollback.sh    remove it again
#
# The files go where the device package will put them (/usr/libexec and
# /usr/lib/systemd/system), as files no package owns: the next device package
# that ships them simply takes them over, and its postinst creates the same
# enablement link.  install.sh refuses to touch a file a package already owns.
#
# In this order, every line logged to /var/log/gts9u-suspendkit-install.log:
#   1. checks: SM-X710, kit checksums, dpkg not busy, nothing owned by a package
#   2. backups, once, in /var/backups/ubuntu-gts9u-suspendkit (normally empty:
#      the files do not exist before)
#   3. the helper and the unit, daemon-reload, enable (a link in
#      ubuntu-gts9u-adsp-boot.service.wants)
#   4. smoke test: start the unit now.  The DSP is already up, so the helper
#      must take the lock, report ready and exit at once.  This proves the
#      systemd-inhibit and notify wiring without a reboot; the real hold
#      happens at the next boot, before the DSP starts.
set -eu

kit=$(cd "$(dirname "$0")" && pwd)
log=/var/log/gts9u-suspendkit-install.log
backup=/var/backups/ubuntu-gts9u-suspendkit
unit=ubuntu-gts9u-adsp-start-inhibit.service
helper=/usr/libexec/ubuntu-gts9u-adsp-start-inhibit
unitfile=/usr/lib/systemd/system/$unit
wants=/etc/systemd/system/ubuntu-gts9u-adsp-boot.service.wants/$unit

say() {
	line="$(date '+%F %T') install: $*"
	echo "$line" >> "$log"
	echo "$line" 2>/dev/null || :
}
die() { say "STOP: $*"; exit 1; }
# run_logged TAG CMD...: run CMD, copy its output into the log, keep its status.
run_logged() {
	tag=$1
	shift
	out=$(mktemp)
	if "$@" > "$out" 2>&1; then rc=0; else rc=$?; fi
	while IFS= read -r l || [ -n "$l" ]; do say "$tag: $l"; done < "$out"
	rm -f "$out"
	return "$rc"
}
owner_of() { dpkg -S "$1" 2>/dev/null | cut -d: -f1; }

case "${1:-}" in
'') ;;
*) echo 'usage: install.sh' >&2; exit 2 ;;
esac
[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> /run/gts9u-suspendkit-install.lock
flock -n 9 || { echo 'install.sh or rollback.sh is still running' >&2; exit 1; }
trap '' HUP PIPE
touch "$log" && chgrp adm "$log" && chmod 0640 "$log" || { echo "cannot write $log" >&2; exit 1; }
say "boot $(cat /proc/sys/kernel/random/boot_id), kit revision $(cat "$kit/REVISION" 2>/dev/null || echo '?')"

# --- 1. checks ----------------------------------------------------------------
tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9wifi' ||
	die 'this is not an SM-X710 (the lock is gated to samsung,gts9wifi)'
(cd "$kit" && sha256sum -c --quiet SHA256SUMS) || die 'kit checksum mismatch'
for c in systemd-inhibit systemd-notify dpkg flock; do
	command -v "$c" >/dev/null 2>&1 || die "$c is missing"
done
fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock >/dev/null 2>&1 &&
	die 'dpkg is busy (apt, unattended-upgrades or PackageKit); nothing was changed, try again when it is done'
[ -f /usr/lib/systemd/system/ubuntu-gts9u-adsp-boot.service ] ||
	die 'ubuntu-gts9u-adsp-boot.service is missing: this is not a device package 2.5x system'
same=0
for f in "$helper" "$unitfile"; do
	o=$(owner_of "$f")
	[ -n "$o" ] || continue
	if cmp -s "$f" "$kit/files/${f##*/}"; then
		say "$f already ships in $o, identical to the kit"
		same=$((same + 1))
	else
		die "$f ships in $o and differs from the kit (a newer device package?); not overwriting it"
	fi
done
if [ "$same" = 2 ]; then
	say 'the device package carries the lock already: nothing to install'
	exit 0
fi

# --- 2. backups ---------------------------------------------------------------
if [ -e "$backup/complete" ]; then
	say "re-run: backups from $(cat "$backup/date") are kept"
else
	[ -e "$backup" ] && die "$backup exists without a 'complete' marker: not from this script; inspect it first"
	new=$backup.new
	rm -rf -- "$new"
	mkdir -p "$new" && chmod 0700 "$new" || die "cannot create $new"
	date '+%F %T' > "$new/date"
	: > "$new/existed"
	for f in "$helper" "$unitfile"; do
		if [ -e "$f" ]; then
			cp -a "$f" "$new/" || die "saving $f failed"
			echo "$f" >> "$new/existed"
		fi
	done
	[ -e "$wants" ] && echo "$wants" >> "$new/existed"
	: > "$new/complete"
	mv "$new" "$backup" || die "cannot move $new into place"
	say "backups in $backup (files that existed before: $(wc -l < "$backup/existed"))"
fi

# --- 3. install ----------------------------------------------------------------
run_logged install install -m 0755 "$kit/files/ubuntu-gts9u-adsp-start-inhibit" "$helper" ||
	die "installing $helper failed"
run_logged install install -m 0644 "$kit/files/$unit" "$unitfile" ||
	die "installing $unitfile failed"
run_logged daemon-reload systemctl daemon-reload || die 'systemctl daemon-reload failed'
run_logged enable systemctl enable "$unit" || die "systemctl enable $unit failed"
[ -L "$wants" ] || die "enabling did not create $wants"
run_logged analyze systemd-analyze verify "$unitfile" || say 'note: systemd-analyze verify complained (see above)'
say "installed $helper and $unitfile; enabled ($wants)"

# --- 4. smoke test ---------------------------------------------------------------
since=$(date '+%F %T')
if run_logged start systemctl start "$unit"; then
	sleep 1
	res=$(systemctl show -p Result --value "$unit")
	state=$(systemctl show -p ActiveState --value "$unit")
	journalctl -u "$unit" --since "$since" -o cat --no-pager 2>/dev/null | while IFS= read -r l; do say "journal: $l"; done
	if [ "$res" = success ]; then
		say "smoke test: ok (the lock was taken and released; unit now $state, result $res)"
	else
		say "smoke test: CHECK, unit $state, result $res (see the journal lines above)"
	fi
else
	journalctl -u "$unit" --since "$since" -o cat --no-pager 2>/dev/null | while IFS= read -r l; do say "journal: $l"; done
	say 'smoke test: FAILED to start; the DSP start does not depend on it (Wants= only). Roll back with sudo sh ~/suspendkit/rollback.sh'
	exit 1
fi
say 'done.  It acts from the next boot; after that boot run: sh ~/suspendkit/verify.sh'
