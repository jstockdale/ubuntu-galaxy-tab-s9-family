#!/bin/sh
# Stop the SM-X910's camera stack from publishing four phantom GTS9U-* webcams
# on the SM-X710, in place and without a reboot: the camera part of the next
# ubuntu-gts9u-device, on top of 2.55.
#
#   sudo sh ~/cameras-kit/install.sh      about ten seconds; no reboot
#   sh ~/cameras-kit/verify.sh            checks, no sudo
#   sudo sh ~/cameras-kit/rollback.sh     back to 2.55's camera files
#
# What it does, in this order (every line goes to /var/log/gts9u-cameras.log):
#   1. checks: SM-X710, kit checksums, dpkg not busy, the files it replaces
#      are still 2.55's
#   2. backups, once, in /var/backups/ubuntu-gts9u-cameras
#   3. stops the four relays (they hold the phantom nodes open)
#   4. puts the kit's files in place, removes the fixed modules-load.d and
#      modprobe.d entries, reloads systemd and udev, enables the new
#      ubuntu-gts9u-camera-loopback and ubuntu-gts9u-camera-enable units
#   5. restarts the loopback loader: on this kernel the X710 has no camera,
#      so the table is empty and the GTS9U-* nodes are removed
#   6. starts the relays, which now skip themselves on the empty table
#
# These are package files changed in place; dpkg --verify lists them until a
# device package that carries them is installed (it then simply replaces
# them).  Nothing outside the camera stack is touched, and the kernel and its
# boot images are not involved.
set -eu

kit=$(cd "$(dirname "$0")" && pwd)
backup=/var/backups/ubuntu-gts9u-cameras
log=/var/log/gts9u-cameras.log
relays=ubuntu-gts9u-camera-relays.service
loader=ubuntu-gts9u-camera-loopback.service
enabler=ubuntu-gts9u-camera-enable.service

# The log first: a terminal that is gone must not end the run (set -e).
say() {
	line="$(date '+%F %T') install: $*"
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

[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> /run/gts9u-cameras.lock
flock -n 9 || {
	echo 'install.sh or rollback.sh is still running; follow it with: tail -f /var/log/gts9u-cameras.log' >&2
	exit 1
}
trap '' HUP PIPE
finished=0
trap 'rc=$?; [ "$finished" = 1 ] || say "ended early (status $rc); re-run install.sh, or undo with rollback.sh"' EXIT

# --- 1. checks ---------------------------------------------------------------
tr '\0' '\n' < /proc/device-tree/compatible | grep -qx 'samsung,gts9wifi' ||
	die 'this is not an SM-X710'
say "boot $(cat /proc/sys/kernel/random/boot_id), $(uname -r), ubuntu-gts9u-device $(dpkg-query -W -f '${Version}' ubuntu-gts9u-device 2>/dev/null || echo '?')"
(cd "$kit" && sha256sum -c --quiet SHA256SUMS) || die 'kit checksum mismatch'
fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock >/dev/null 2>&1 &&
	die 'dpkg is busy (apt, unattended-upgrades or PackageKit); nothing was changed, try again when it is done'

# --- 2. backups --------------------------------------------------------------
if [ -e "$backup/complete" ]; then
	say "re-run: backups from $(cat "$backup/date") are kept"
else
	[ -e "$backup" ] && die "$backup exists but has no 'complete' marker: not from this script; inspect it first"
	# Only what 2.55 installed is backed up and replaced: anything else means
	# the camera files changed since this kit was made.
	(cd / && sha256sum -c --quiet "$kit/BEFORE.sha256") ||
		die 'the camera files are not the ones ubuntu-gts9u-device 2.55 installed; nothing was changed'
	new=$backup.new
	rm -rf -- "$new"
	mkdir -p "$new/root" && chmod 0700 "$new" || die "cannot create $new"
	date '+%F %T' > "$new/date"
	dpkg-query -W -f '${Version}\n' ubuntu-gts9u-device > "$new/device-version" 2>/dev/null || :
	: > "$new/absent"
	for path in $(awk '{ print $1 }' "$kit/FILES") $(cat "$kit/REMOVED"); do
		if [ -e "/$path" ]; then
			mkdir -p "$new/root/$(dirname "$path")"
			cp -a "/$path" "$new/root/$path" || die "saving /$path failed"
		else
			echo "$path" >> "$new/absent"
		fi
	done
	for unit in "$relays" "$loader" "$enabler"; do
		echo "$unit $(systemctl is-enabled "$unit" 2>/dev/null || echo absent)"
	done > "$new/enablement"
	{
		lsmod | grep -E '^v4l2loopback ' || echo 'v4l2loopback not loaded'
		for node in /sys/devices/virtual/video4linux/video*; do
			[ -r "$node/name" ] && echo "${node##*/}: $(cat "$node/name")"
		done
	} > "$new/state-before" 2>/dev/null || :
	: > "$new/complete"
	mv "$new" "$backup" || die "cannot move $new into place"
	say "backups in $backup ($(wc -l < "$backup/absent") of the kit's files are new)"
fi

# --- 3. relays ---------------------------------------------------------------
run_logged systemctl systemctl stop "$relays" || die "stopping $relays failed"
say 'relays stopped'

# --- 4. files ----------------------------------------------------------------
while read -r path mode; do
	install -D -o root -g root -m "$mode" "$kit/files/$path" "/$path.gts9u-new" &&
		mv -f "/$path.gts9u-new" "/$path" || die "installing /$path failed"
done < "$kit/FILES"
for path in $(cat "$kit/REMOVED"); do
	rm -f "/$path"
done
say "files: $(wc -l < "$kit/FILES") in place; removed $(tr '\n' ' ' < "$kit/REMOVED")"
run_logged daemon-reload systemctl daemon-reload || die 'systemctl daemon-reload failed'
run_logged enable systemctl enable "$loader" "$enabler" || die "enabling $loader and $enabler failed"
run_logged udev udevadm control --reload || die 'udevadm control --reload failed'

# --- 5. nodes ----------------------------------------------------------------
if ! run_logged loader systemctl restart "$loader"; then
	run_logged journal journalctl -b -u "$loader" -n 5 --no-pager -o cat || :
	die "the loopback loader failed (above); if an application still holds a /dev/video2x node, close it and re-run install.sh"
fi
# systemctl prints nothing on success; the loader's own lines (which nodes it
# removed or created) went to the journal.  Copy this run's lines into the log.
journalctl --sync 2>/dev/null || :
invocation=$(systemctl show -p InvocationID --value "$loader" 2>/dev/null || :)
if [ -n "$invocation" ]; then
	run_logged loader journalctl -q -b --no-pager -o cat "_SYSTEMD_INVOCATION_ID=$invocation" || :
fi
say "camera table: $(wc -l < /run/ubuntu-gts9u/camera-profile) camera(s); $(lsmod | grep -qE '^v4l2loopback ' && echo 'v4l2loopback loaded' || echo 'v4l2loopback not loaded')"

# --- 6. relays again -----------------------------------------------------------
run_logged systemctl systemctl start "$relays" || say "starting $relays failed (see journalctl -u $relays)"
say "relays: $(systemctl show -p ActiveState --value "$relays"), condition result $(systemctl show -p ConditionResult --value "$relays")"

finished=1
say 'done; check with: sh ~/cameras-kit/verify.sh'
