#!/bin/sh
# The userspace half of the powerthermal work, onto the running system in
# place, with no reboot and nothing restarted.
#
#   sudo sh ~/powerthermal/install.sh     install
#   sudo sh ~/powerthermal/rollback.sh    undo exactly what install.sh did
#   sh ~/powerthermal/verify.sh           check (no sudo)
#
# Hangups are ignored, so a dropped SSH session does not cut a run short; every
# line goes to /var/log/gts9u-powerthermal.log first.  In this order:
#   1. checks: root, one run at a time, a Galaxy Tab S9 (X710 or X910), dpkg
#      not busy, the kit's checksums
#   2. backups, once, in /var/backups/ubuntu-gts9u-powerthermal: whether rtkit
#      was installed, the greeter's ~/.config/wireplumber if it had one, and
#      whether the files of step 4 already existed
#   3. rtkit from the Ubuntu archive (apt-get, so it needs the network).  A
#      failure is reported, not fatal: step 4 still runs.
#   4. the greeter's WirePlumber stops claiming Bluetooth audio: the files
#      ubuntu-gts9u-device will ship (/usr/share/ubuntu-gts9u/greeter-wireplumber
#      and /usr/libexec/ubuntu-gts9u-greeter-audio), then that helper, which
#      writes two snippets into the gdm user's ~/.config/wireplumber.  When the
#      installed device package already ships the helper, only the helper runs.
#
# rtkit reaches PipeWire and Mutter's KMS thread at the next login (log out and
# back in, or reboot); the greeter snippet at the next boot.  The kernel half
# (SM5440/SM5714 charging fixes, thermal DT) needs the next kernel build and is
# not in this kit.
set -eu

kit=$(cd "$(dirname "$0")" && pwd)
backup=/var/backups/ubuntu-gts9u-powerthermal
log=/var/log/gts9u-powerthermal.log
share=/usr/share/ubuntu-gts9u/greeter-wireplumber
helper=/usr/libexec/ubuntu-gts9u-greeter-audio

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
dpkg_owns() { dpkg-query -S "$1" >/dev/null 2>&1; }

[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
exec 9> /run/gts9u-powerthermal.lock
flock -n 9 || {
	echo 'install.sh or rollback.sh is already running; follow it with: tail -f /var/log/gts9u-powerthermal.log' >&2
	exit 1
}
trap '' HUP PIPE

# --- 1. checks -------------------------------------------------------------
tr '\0' '\n' < /proc/device-tree/compatible |
	grep -qx -e 'samsung,gts9wifi' -e 'samsung,gts9uwifi' ||
	die 'this is not a Galaxy Tab S9 (SM-X710) or S9 Ultra (SM-X910)'
say "boot $(cat /proc/sys/kernel/random/boot_id), kernel $(uname -r)"
if fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock >/dev/null 2>&1; then
	die 'dpkg or apt is busy (an update?); wait for it and run this again'
fi
(cd "$kit" && sha256sum -c --quiet SHA256SUMS) > /dev/null 2>&1 ||
	die "kit checksums do not match: (cd $kit && sha256sum -c SHA256SUMS)"
say 'kit checksums ok'

gdm_home=
if entry=$(getent passwd gdm); then
	gdm_home=$(printf '%s\n' "$entry" | cut -d: -f6)
else
	say 'no gdm account: the greeter step will be skipped'
fi

# --- 2. backups, once --------------------------------------------------------
if [ ! -e "$backup/complete" ]; then
	rm -rf "$backup"
	mkdir -p "$backup"
	chmod 0700 "$backup"
	if dpkg-query -W -f='${Status}\n' rtkit 2>/dev/null | grep -q 'ok installed'; then
		echo installed > "$backup/rtkit-before"
	else
		echo absent > "$backup/rtkit-before"
	fi
	if [ -n "$gdm_home" ] && [ -d "$gdm_home/.config/wireplumber" ]; then
		tar -C "$gdm_home/.config" -cpf "$backup/greeter-wireplumber.tar" wireplumber
	else
		: > "$backup/greeter-wireplumber.absent"
	fi
	: > "$backup/usr-present-before"
	for p in "$share" "$helper"; do
		if [ -e "$p" ]; then echo "$p" >> "$backup/usr-present-before"; fi
	done
	: > "$backup/complete"
	say "backups in $backup"
else
	say "backups already in $backup (from an earlier run); kept"
fi

# --- 3. rtkit ----------------------------------------------------------------
rtkit_ok=1
if dpkg-query -W -f='${Status}\n' rtkit 2>/dev/null | grep -q 'ok installed'; then
	say 'rtkit is already installed'
else
	export DEBIAN_FRONTEND=noninteractive
	# --no-remove: abort rather than let -y accept removing anything (the
	# locally rebuilt PipeWire packages must stay as they are).
	if run_logged apt apt-get install -y --no-remove --no-install-recommends rtkit ||
	   { run_logged apt apt-get update &&
	     run_logged apt apt-get install -y --no-remove --no-install-recommends rtkit; }; then
		: > "$backup/rtkit.installed-by-kit"
		say "rtkit $(dpkg-query -W -f='${Version}' rtkit) installed"
	else
		rtkit_ok=0
		say 'WARNING: rtkit could not be installed (network?); fix that and re-run install.sh'
	fi
fi

# --- 4. greeter WirePlumber ----------------------------------------------------
greeter_ok=1
if [ -z "$gdm_home" ]; then
	greeter_ok=0
elif dpkg_owns "$helper"; then
	say "the device package ships $helper: running it"
	run_logged greeter "$helper" || greeter_ok=0
else
	install -d -m 0755 "$share/bluetooth.lua.d" "$share/wireplumber.conf.d"
	install -m 0644 "$kit/files/greeter-wireplumber/bluetooth.lua.d/51-gts9u-greeter-no-bluez.lua" \
		"$share/bluetooth.lua.d/"
	install -m 0644 "$kit/files/greeter-wireplumber/wireplumber.conf.d/51-gts9u-greeter-no-bluez.conf" \
		"$share/wireplumber.conf.d/"
	install -m 0755 "$kit/files/ubuntu-gts9u-greeter-audio" "$helper"
	: > "$backup/usr-files.installed-by-kit"
	run_logged greeter "$helper" || greeter_ok=0
fi
if [ "$greeter_ok" = 1 ] && [ -r "$gdm_home/.config/wireplumber/bluetooth.lua.d/51-gts9u-greeter-no-bluez.lua" ]; then
	say "greeter snippets in $gdm_home/.config/wireplumber"
else
	greeter_ok=0
	say 'WARNING: the greeter snippets are not in place (see the lines above)'
fi

say "done: rtkit=$rtkit_ok greeter=$greeter_ok"
say 'next: log out and back in (or reboot) so PipeWire and Mutter ask rtkit; the greeter change applies from the next boot; then: sh ~/powerthermal/verify.sh'
[ "$rtkit_ok" = 1 ] && [ "$greeter_ok" = 1 ]
