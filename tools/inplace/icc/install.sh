#!/bin/sh
# Install the interconnect late release on the running SM-X710 in place:
# ubuntu-gts9u-icc-late-release (helper) and its unit, exactly as the next
# ubuntu-gts9u-device package ships them, enabled so that every boot releases
# gem_noc and mc_virt once the panel recovery's pm_test cycle has returned.
#
#   sudo sh ~/icc/install.sh          install and enable; acts from the next boot
#   sudo sh ~/icc/install.sh --now    the same, and release on this boot too
#   sh ~/icc/verify.sh                (no sudo) what this boot did
#   sudo sh ~/icc/rollback.sh         disable and remove; a reboot brings the floor back
#
# A release cannot be undone on a running kernel: only a reboot puts the
# INT_MAX floor back.  Without --now nothing changes on this boot.
#
# The files go where the package will put them (/usr/libexec and
# /usr/lib/systemd/system), owned by root, so the next device package simply
# takes them over (dpkg replaces files no package owns).  If the installed
# package already ships them, nothing is copied.
#
# Hangups are ignored, so a dropped SSH session does not cut it short; every
# line goes to /var/log/gts9u-icc-kit.log first.
#
# If a boot ever hangs after the release, the unit's loop breaker keeps the
# floor on the boot after it (README.md).  To keep it on every boot without
# Linux, from TWRP:
#   [ -d /data/home ] || mount -t ext4 /dev/block/by-name/userdata /data
#   mkdir -p /data/etc/ubuntu-gts9u && touch /data/etc/ubuntu-gts9u/icc-hold
# then reboot: the unit keeps the floor while that file exists.
set -eu

kit=$(cd "$(dirname "$0")" && pwd)
log=/var/log/gts9u-icc-kit.log
backup=/var/backups/ubuntu-gts9u-icc
name=ubuntu-gts9u-icc-late-release
helper=/usr/libexec/$name
unit=/usr/lib/systemd/system/$name.service
wants=/etc/systemd/system/multi-user.target.wants/$name.service

say() {
	line="$(date '+%F %T') $*"
	echo "$line" >> "$log"
	echo "$line" 2>/dev/null || :
}
die() { say "STOP: $*"; exit 1; }
run_logged() {
	tag=$1
	shift
	out=$(mktemp)
	if "$@" > "$out" 2>&1; then rc=0; else rc=$?; fi
	while IFS= read -r l || [ -n "$l" ]; do say "$tag: $l"; done < "$out"
	rm -f "$out"
	return "$rc"
}
# owner_of FILE: the package that ships FILE, or nothing.
owner_of() {
	dpkg-query -S "$1" 2>/dev/null | sed -n 's/^\([^:,]*\):.*/\1/p' | head -n 1
}
same() { [ -f "$2" ] && cmp -s "$1" "$2"; }

now=0
case "${1:-}" in
'') ;;
--now) now=1 ;;
*) echo 'usage: install.sh [--now]' >&2; exit 2 ;;
esac
[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> /run/gts9u-icc-kit.lock
flock -n 9 || { echo "install.sh or rollback.sh is still running; follow it with: tail -f $log" >&2; exit 1; }
trap '' HUP PIPE
touch "$log" && chgrp adm "$log" 2>/dev/null && chmod 0640 "$log" || :

tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9wifi' ||
	die 'this is not an SM-X710 (the late release is for samsung,gts9wifi only)'
say "icc kit install$([ "$now" = 1 ] && echo ' --now'): boot $(cat /proc/sys/kernel/random/boot_id), kernel $(uname -r)"

# --- 1. checks ---------------------------------------------------------------
(cd "$kit" && sha256sum -c --quiet SHA256SUMS) || die 'kit checksum mismatch (SHA256SUMS)'
src_helper=$kit/files/$name
src_unit=$kit/files/$name.service
dash -n "$src_helper" || die "$src_helper does not parse"
systemctl is-active -q gts9u-stage0.service 2>/dev/null &&
	die 'Stage 0 is running (it writes state_synced and /sys/power/state itself); wait for it'

pkg_helper=$(owner_of "$helper")
pkg_unit=$(owner_of "$unit")
copy=1
if [ -n "$pkg_helper$pkg_unit" ]; then
	if same "$src_helper" "$helper" && same "$src_unit" "$unit"; then
		say "the installed $pkg_helper$([ "$pkg_unit" != "$pkg_helper" ] && echo " / $pkg_unit") already ships these exact files; nothing to copy"
		copy=0
	else
		die "the installed package ($pkg_helper $pkg_unit) ships a different $name; use the package, not this kit"
	fi
fi

# --- 2. backups (only of files no package owns, from an earlier kit) ---------
if [ "$copy" = 1 ] && [ ! -e "$backup/complete" ]; then
	[ -e "$backup" ] && die "$backup exists but has no 'complete' marker: not from this script; inspect it first"
	new=$backup.new
	rm -rf -- "$new"
	mkdir -p "$new" && chmod 0700 "$new" || die "cannot create $new"
	date '+%F %T' > "$new/date"
	for f in "$helper" "$unit"; do
		if [ -e "$f" ]; then
			cp -a "$f" "$new/" || die "saving $f failed"
			say "backed up the existing $f"
		fi
	done
	if [ -L "$wants" ]; then echo enabled > "$new/enablement"; else echo disabled > "$new/enablement"; fi
	: > "$new/complete"
	mv "$new" "$backup" || die "cannot move $new into place"
	say "backups in $backup"
fi

# --- 3. files ----------------------------------------------------------------
if [ "$copy" = 1 ]; then
	# Explicit owner and modes: a copy from the user's kit directory must not
	# end up owned by the user.
	install -o root -g root -m 0755 "$src_helper" "$helper" || die "installing $helper failed"
	install -o root -g root -m 0644 "$src_unit" "$unit" || die "installing $unit failed"
	same "$src_helper" "$helper" && same "$src_unit" "$unit" || die 'the installed files do not match the kit'
	say "installed $helper and $unit"
fi
run_logged daemon-reload systemctl daemon-reload || die 'systemctl daemon-reload failed'
if [ "$(systemctl is-enabled "$name.service" 2>/dev/null)" = masked ]; then
	die "$name.service is masked; unmask it first (sudo systemctl unmask $name.service) if the release is wanted again"
fi
run_logged enable systemctl enable "$name.service" || die "systemctl enable $name.service failed"
[ -L "$wants" ] || die "$wants was not created"
say "enabled: $(systemctl is-enabled "$name.service" 2>&1)"
# The off switch's directory, so that 'sudo touch /etc/ubuntu-gts9u/icc-hold'
# works as written (no package creates it).
mkdir -p /etc/ubuntu-gts9u || die 'cannot create /etc/ubuntu-gts9u'
[ -e /etc/ubuntu-gts9u/icc-hold ] &&
	say 'NOTE: /etc/ubuntu-gts9u/icc-hold exists, so the unit keeps the floor until it is removed'

# --- 4. what the unit would do now --------------------------------------------
run_logged check "$helper" check || die "$helper check failed"

# --- 5. optional release on this boot -----------------------------------------
if [ "$now" = 1 ]; then
	say 'releasing on this boot (irreversible until the next reboot)'
	sync
	# restart, not start: a unit left active by an earlier hold would
	# otherwise not run again.
	if ! run_logged start systemctl restart "$name.service"; then
		run_logged journal journalctl -b -u "$name.service" --no-pager -o cat -n 20 || :
		die "$name.service failed; see $([ -r /var/log/ubuntu-gts9u-icc-release.log ] && echo /var/log/ubuntu-gts9u-icc-release.log)"
	fi
	run_logged record cat /run/ubuntu-gts9u/icc-release || :
	result=$(sed -n 's/^result=//p' /run/ubuntu-gts9u/icc-release 2>/dev/null)
	case "$result" in
	released|nothing) say "this boot: $result" ;;
	*) say "this boot: ${result:-no record}: the floor is still held (see the reason above)" ;;
	esac
fi

say "done.  $([ "$now" = 1 ] && echo 'Check this boot with' || echo 'Reboot when convenient; then check with'): sh $kit/verify.sh"
