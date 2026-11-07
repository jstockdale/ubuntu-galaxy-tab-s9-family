#!/bin/sh
# Undo exactly what install.sh did, from /var/backups/ubuntu-gts9u-powerthermal.
#
#   sudo sh ~/powerthermal/rollback.sh
#
# - the greeter: the helper takes its two snippets out of the gdm user's
#   ~/.config/wireplumber, and an earlier wireplumber directory there is put
#   back.  Skipped once the installed device package ships the helper itself:
#   the feature then belongs to the package (remove the package's copy with
#   sudo /usr/libexec/ubuntu-gts9u-greeter-audio --remove if really wanted).
# - /usr/share/ubuntu-gts9u/greeter-wireplumber and
#   /usr/libexec/ubuntu-gts9u-greeter-audio go, if install.sh created them and
#   no package owns them now.
# - rtkit is removed, if install.sh installed it.
# Nothing is restarted.  Log: /var/log/gts9u-powerthermal.log.
set -eu

backup=/var/backups/ubuntu-gts9u-powerthermal
log=/var/log/gts9u-powerthermal.log
share=/usr/share/ubuntu-gts9u/greeter-wireplumber
helper=/usr/libexec/ubuntu-gts9u-greeter-audio

say() {
	line="$(date '+%F %T') rollback: $*"
	echo "$line" >> "$log"
	echo "$line" 2>/dev/null || :
}
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
flock -n 9 || { echo 'install.sh or rollback.sh is already running' >&2; exit 1; }
trap '' HUP PIPE

# The optional polkit rule (README) is a manual step, so take it out whether or
# not install.sh ever ran.  The file name is the kit's own.
rule=/etc/polkit-1/rules.d/60-ubuntu-gts9u-rtkit-linger.rules
if [ -e "$rule" ]; then
	rm -f -- "$rule"
	say "removed $rule"
fi

[ -e "$backup/complete" ] || { say "nothing to roll back: no $backup/complete"; exit 0; }
if fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock >/dev/null 2>&1; then
	say 'STOP: dpkg or apt is busy; wait for it and run this again'
	exit 1
fi

gdm_home=
if entry=$(getent passwd gdm); then
	gdm_home=$(printf '%s\n' "$entry" | cut -d: -f6)
fi

# --- greeter ---------------------------------------------------------------
if dpkg_owns "$helper"; then
	say "the device package now ships $helper; its greeter snippets are left in place"
else
	if [ -x "$helper" ]; then
		run_logged greeter "$helper" --remove || say 'WARNING: helper --remove failed'
	fi
	if [ -n "$gdm_home" ] && [ -f "$backup/greeter-wireplumber.tar" ]; then
		tar -C "$gdm_home/.config" -xpf "$backup/greeter-wireplumber.tar"
		say "restored $gdm_home/.config/wireplumber from the backup"
	fi
	if [ -e "$backup/usr-files.installed-by-kit" ]; then
		for p in "$share" "$helper"; do
			if grep -qx "$p" "$backup/usr-present-before" 2>/dev/null; then
				say "$p existed before install.sh; left"
			else
				rm -rf -- "$p"
				say "removed $p"
			fi
		done
	fi
fi

# --- rtkit -----------------------------------------------------------------
if [ -e "$backup/rtkit.installed-by-kit" ]; then
	export DEBIAN_FRONTEND=noninteractive
	# dpkg, not apt-get remove -y: apt would also remove, unasked, anything
	# installed since that depends on rtkit; dpkg refuses instead.
	if run_logged dpkg dpkg --remove rtkit; then
		say 'rtkit removed'
	else
		say 'WARNING: dpkg --remove rtkit failed (something depends on it now?); see the lines above'
	fi
else
	say "rtkit was $(cat "$backup/rtkit-before" 2>/dev/null || echo unknown) before install.sh; left as it is"
fi

mv "$backup" "$backup.rolled-back-$(date +%Y%m%d-%H%M%S)"
say 'done; as the desktop user: systemctl --user restart pipewire.service filter-chain.service pipewire-pulse.service wireplumber.service (and log out and in for Mutter) to drop realtime'
