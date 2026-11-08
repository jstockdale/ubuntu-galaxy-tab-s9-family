#!/bin/bash
# Bring Bluetooth AAC to a tablet that is already installed, without a new
# image.  Run on the tablet, as root:
#
#   sudo bash install-pipewire-aac-live.sh install [DEB_DIR]
#   sudo bash install-pipewire-aac-live.sh check
#   sudo bash install-pipewire-aac-live.sh rollback
#
# DEB_DIR holds the PipeWire set scripts/build-pipewire-aac.sh produced
# (out/packages/*+gts9uN_*.deb; other packages there are ignored); it
# defaults to this script's own directory.
#
# Ubuntu builds libspa-0.2-bluetooth without the AAC codec, so headphones that
# offer only SBC and AAC (Apple and Beats, most true-wireless earbuds) stay on
# SBC.  Images built since build-ubuntu-rootfs.sh gained the AAC stanza
# install the rebuild from out/local-debs; this does the same on a running
# system:
#
# - It replaces exactly the binary packages of the pipewire source that are
#   installed.  They depend on each other at "= version", so a partial set
#   cannot install, and installing packages that are not there (the set also
#   carries libpipewire-0.3-common) would only grow the system.
# - One APT transaction, which may add libfdk-aac2 (multiverse) and never
#   removes anything (--no-remove) or adds recommends.
# - No downgrades: it refuses a rebuild that is older than what is installed,
#   and a rebuild of an Ubuntu version the archive has already superseded.
# - No holds.  An image does not hold these packages either: the +gts9uN
#   suffix already sorts above the archive build it was made from, and a hold
#   would make the port's updater fail ("Held packages were changed") the
#   first time it ships a different PipeWire.  A newer Ubuntu PipeWire
#   replaces the rebuild on the next upgrade, which takes AAC away until
#   build-pipewire-aac.sh is rerun for that version.  Holds left by the
#   earlier ~/bt-aac/install-aac.sh are released.
#
# Afterwards it restarts the desktop user's PipeWire (the codec plugins are
# loaded once, at start) and checks that WirePlumber registered the AAC
# endpoint with BlueZ and, if a headset is connected, that it plays AAC.
#
# Pipelines below never end in "grep -q": under pipefail an early exit there
# can SIGPIPE the producer and turn a match into a failure.
set -Eeuo pipefail

log() { echo "install-pipewire-aac-live: $*"; }
die() { echo "install-pipewire-aac-live: $*" >&2; exit 1; }

[[ $EUID == 0 ]] || die 'run as root: sudo bash install-pipewire-aac-live.sh ...'
[[ $(dpkg --print-architecture) == arm64 ]] || die 'this is for the arm64 tablets'

relays=ubuntu-gts9u-camera-relays.service

# "name version" for each installed binary package of the pipewire source.
# Filter on the status word, not on "ii": a package the old install-aac.sh
# held reads "hi", and one an interrupted install left behind reads "iU" or
# "iF".  Both are on the system and must be in the set (and a held one must
# be released), or APT refuses the "= version" dependencies of the rest.
installed_pipewire() {
	dpkg-query -W -f '${db:Status-Status} ${Package} ${Version} ${source:Package}\n' |
		awk '$1 != "not-installed" && $1 != "config-files" && $4 == "pipewire" { print $2, $3 }'
}

# Newest version of a package that the APT lists offer (empty if none).
newest_archive_version() {
	local v best=
	while read -r v; do
		[[ -z $v ]] && continue
		[[ -z $best ]] || dpkg --compare-versions "$v" gt "$best" || continue
		best=$v
	done < <(apt-cache madison "$1" 2>/dev/null | awk -F'|' '{ gsub(/[ \t]/, "", $2); print $2 }')
	printf '%s\n' "$best"
}

# The desktop user whose PipeWire to restart: whoever ran sudo, unless
# GTS9U_AUDIO_USER names someone else.  Empty when there is none.
audio_user() {
	local user=${GTS9U_AUDIO_USER:-${SUDO_USER:-}}
	[[ -n $user && $user != root ]] && id -u "$user" >/dev/null 2>&1 && printf '%s\n' "$user"
	return 0
}

as_user() {
	local user=$1 uid
	shift
	uid=$(id -u "$user")
	runuser -u "$user" -- env XDG_RUNTIME_DIR="/run/user/$uid" \
		DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" "$@"
}

has_session() {
	[[ -S /run/user/$(id -u "$1")/bus ]]
}

# Epoch second the user's running WirePlumber started (empty if none).
wireplumber_started() {
	local t
	t=$(as_user "$1" systemctl --user show --timestamp=unix -p ExecMainStartTimestamp \
		--value wireplumber.service 2>/dev/null) || true
	[[ $t == @[0-9]* ]] && printf '%s\n' "${t#@}"
	return 0
}

# 0 when bluetoothd logged WirePlumber registering the AAC source endpoint
# (the one headphones use) at or after the given epoch second.
aac_endpoint_since() {
	local lines
	lines=$(journalctl -u bluetooth.service --since "@$1" -o cat --no-pager 2>/dev/null) || true
	grep -E '^Endpoint registered: .* path=/MediaEndpoint/A2DPSource/aac$' <<<"$lines" >/dev/null
}

# Restart the user's PipeWire so libspa-0.2-bluetooth is loaded again.  The
# camera relays are clients of that graph: stop them first, as the camera
# installers do, and always start them again.
restart_user_audio() {
	local user=$1 rc=0 relays_were_active=
	if systemctl is-active --quiet "$relays"; then
		relays_were_active=1
		systemctl stop "$relays" || rc=$?
	fi
	log "restarting $user's PipeWire, PipeWire-Pulse and WirePlumber"
	as_user "$user" systemctl --user restart pipewire.service pipewire-pulse.service \
		wireplumber.service || rc=$?
	if [[ -n $relays_were_active ]]; then
		systemctl start "$relays" || rc=$?
	fi
	(( rc == 0 )) || die "restarting the audio stack failed ($rc); check: journalctl --user -u wireplumber"
}

# Make the desktop user's session use the installed codec, unless it already
# does, and wait for the AAC endpoint.
reload_user_audio() {
	local user started i
	user=$(audio_user)
	if [[ -z $user ]]; then
		log 'no desktop user (run this through sudo, or set GTS9U_AUDIO_USER);'
		log 'as that user run: systemctl --user restart pipewire pipewire-pulse wireplumber'
		return 0
	fi
	if ! has_session "$user"; then
		log "$user has no running session; the new PipeWire starts with their next login"
		return 0
	fi
	started=$(wireplumber_started "$user")
	if [[ -n $started ]] && aac_endpoint_since "$started"; then
		log "$user's running WirePlumber already registered the AAC endpoint"
		return 0
	fi
	started=$(date +%s)
	restart_user_audio "$user"
	for i in $(seq 1 30); do
		if aac_endpoint_since "$started"; then
			log 'WirePlumber registered the A2DP AAC endpoint with BlueZ'
			return 0
		fi
		sleep 0.5
	done
	die 'no AAC endpoint registered with BlueZ within 15 s; check: journalctl -u bluetooth -b | grep Endpoint'
}

# Non-zero unless every installed PipeWire package is a gts9u rebuild and the
# AAC codec plugin is installed and resolves an installed libfdk-aac.
verify_install() {
	local bad=0 name inst codec fdk
	while read -r name inst; do
		if [[ $inst != *+gts9u* ]]; then
			echo "  $name $inst is the archive build (no AAC)"
			bad=1
		fi
	done < <(installed_pipewire)
	codec=$(dpkg -L libspa-0.2-bluetooth 2>/dev/null |
		grep -E '/spa-0\.2/bluez5/libspa-codec-bluez5-aac\.so$') || true
	if [[ -z $codec || ! -f $codec ]]; then
		echo '  the installed libspa-0.2-bluetooth has no AAC codec plugin'
		bad=1
	else
		fdk=$(ldd "$codec" 2>/dev/null | awk '$1 ~ /^libfdk-aac\.so/ { print $3 }') || true
		if [[ -z $fdk || ! -e $fdk ]]; then
			echo "  $codec does not resolve libfdk-aac (is libfdk-aac2 installed?)"
			bad=1
		else
			echo "  $codec -> $fdk"
		fi
	fi
	return "$bad"
}

# If a Bluetooth headset is connected and playing A2DP, move it to its AAC
# profile (when it offers one) and report the codec its sink runs.  This only
# reports: AAC is optional for headphones and the install does not depend on it.
check_headset() {
	local user card profiles aac active codec
	user=$(audio_user)
	[[ -n $user ]] && has_session "$user" || return 0
	card=$(as_user "$user" pactl list cards short 2>/dev/null |
		awk '$2 ~ /^bluez_card\./ { print $2 }' | head -n 1) || true
	if [[ -z $card ]]; then
		log 'no Bluetooth headset connected; connect it, then run: sudo bash install-pipewire-aac-live.sh check'
		return 0
	fi
	# This card's A2DP profile lines ("name: description (sinks: ...)"), then
	# its active profile.
	profiles=$(as_user "$user" pactl list cards 2>/dev/null | awk -v c="$card" '
		$1 == "Name:" { mine = ($2 == c) }
		mine && /^\t\t[^\t].*: .*\(A2DP Sink, codec / { print "profile", $0 }
		mine && /^\tActive Profile: / { print "active", $3 }') || true
	aac=$(awk '$1 == "profile" && /codec AAC\)/ { sub(/^profile[ \t]+/, ""); sub(/:.*/, ""); print; exit }' \
		<<<"$profiles")
	active=$(awk '$1 == "active" { print $2; exit }' <<<"$profiles")
	if [[ -z $aac ]]; then
		log "$card offers no AAC profile; its A2DP profiles:"
		awk '$1 == "profile" { sub(/^profile[ \t]+/, ""); print "    " $0 }' <<<"$profiles"
		return 0
	fi
	if [[ $active != "$aac" ]]; then
		if [[ $active != a2dp-sink* ]]; then
			log "$card offers AAC as profile $aac but is on ${active:-no profile}; select it in Settings, or: pactl set-card-profile $card $aac"
			return 0
		fi
		log "$card: switching from $active to its AAC profile $aac"
		as_user "$user" pactl set-card-profile "$card" "$aac" || true
		sleep 2
	fi
	codec=$(as_user "$user" pactl list sinks 2>/dev/null | awk -v s="bluez_output.${card#bluez_card.}." '
		$1 == "Name:" { mine = (index($2, s) == 1) }
		mine && $1 == "api.bluez5.codec" { gsub(/"/, "", $3); print $3 }' | head -n 1) || true
	log "$card plays ${codec:-an unknown codec} (api.bluez5.codec)"
}

# Release APT holds on the named packages.  The old ~/bt-aac/install-aac.sh
# held the whole set, and apt-get -y refuses to change a held package in
# either direction ("Held packages were changed").
release_holds() {
	local -a held=()
	mapfile -t held < <(apt-mark showhold | grep -Fx -f <(printf '%s\n' "$@") || true)
	if (( ${#held[@]} )); then
		log "releasing APT holds on: ${held[*]}"
		apt-mark unhold "${held[@]}" >/dev/null
	fi
}

install_set() {
	local dir=$1 deb info pkg ver src arch rebuild= base name inst inst_base newest
	local -A deb_for=()
	local -a debs=() selected=() names=()
	local current=0

	[[ -d $dir ]] || die "no such directory: $dir"
	dir=$(realpath "$dir")
	shopt -s nullglob
	debs=("$dir"/*.deb)
	shopt -u nullglob
	(( ${#debs[@]} )) || die "no .deb files in $dir"

	for deb in "${debs[@]}"; do
		info=$(dpkg-deb --show \
			--showformat='${Package}\t${Version}\t${source:Package}\t${Architecture}\n' "$deb") ||
			die "cannot read ${deb##*/}"
		IFS=$'\t' read -r pkg ver src arch <<<"$info"
		if [[ $src != pipewire ]]; then
			log "ignoring ${deb##*/}: not built from the pipewire source"
			continue
		fi
		[[ $arch == arm64 || $arch == all ]] || die "${deb##*/} is for $arch"
		[[ $ver == *+gts9u* ]] || die "${deb##*/} is not a gts9u rebuild ($ver)"
		[[ -z $rebuild || $rebuild == "$ver" ]] || die "mixed versions in $dir: $rebuild and $ver"
		[[ -z ${deb_for[$pkg]:-} ]] || die "two .debs for $pkg in $dir"
		rebuild=$ver
		deb_for[$pkg]=$deb
	done
	base=${rebuild%+gts9u*}

	[[ -n ${deb_for[libspa-0.2-bluetooth]:-} ]] || die "no libspa-0.2-bluetooth .deb in $dir"
	dpkg-deb -c "${deb_for[libspa-0.2-bluetooth]}" |
		grep -E '/spa-0\.2/bluez5/libspa-codec-bluez5-aac\.so$' >/dev/null ||
		die "${deb_for[libspa-0.2-bluetooth]##*/} has no AAC codec; it is not a build-pipewire-aac.sh build"

	while read -r name inst; do
		[[ -n ${deb_for[$name]:-} ]] ||
			die "$name $inst is installed but $dir has no rebuild of it; copy the whole out/packages set"
		inst_base=${inst%+gts9u*}
		if ! dpkg --compare-versions "$inst_base" le "$base"; then
			die "$name is at $inst, newer than the $base this rebuild is made from; rebuild with PIPEWIRE_VERSION=$inst_base scripts/build-pipewire-aac.sh"
		fi
		if ! dpkg --compare-versions "$rebuild" ge "$inst"; then
			die "$name is at $inst, a newer rebuild than this one ($rebuild); install from that build's .debs instead"
		fi
		[[ $inst == "$rebuild" ]] && current=$((current + 1))
		names+=("$name")
		selected+=("${deb_for[$name]}")
	done < <(installed_pipewire)
	(( ${#selected[@]} )) || die 'no PipeWire package is installed; this is for an installed desktop image'
	for pkg in "${!deb_for[@]}"; do
		[[ " ${names[*]} " == *" $pkg "* ]] || log "skipping ${deb_for[$pkg]##*/}: $pkg is not installed"
	done

	# A rebuild of a superseded Ubuntu version would lack its fixes, and the
	# next upgrade would replace it anyway.
	newest=$(newest_archive_version pipewire)
	if [[ -n $newest ]] && dpkg --compare-versions "$newest" gt "$base"; then
		die "the archive now has pipewire $newest, newer than $base; rebuild with PIPEWIRE_VERSION=$newest scripts/build-pipewire-aac.sh"
	fi

	release_holds "${names[@]}"

	if (( current == ${#selected[@]} )); then
		log "$rebuild is already installed"
	else
		log "installing $rebuild over the installed set (${#selected[@]} packages)"
		DEBIAN_FRONTEND=noninteractive apt-get install -y --no-remove \
			--no-install-recommends "${selected[@]}"
	fi
	log 'installed PipeWire packages:'
	installed_pipewire | sed 's/^/  /'
	verify_install || die 'the install did not leave a working AAC codec (see above)'
	reload_user_audio
	check_headset
}

check_state() {
	local user started=
	log 'installed PipeWire packages:'
	installed_pipewire | sed 's/^/  /'
	verify_install || die 'no working AAC codec installed (see above)'
	user=$(audio_user)
	if [[ -n $user ]] && has_session "$user"; then
		started=$(wireplumber_started "$user")
	fi
	if [[ -z $started ]]; then
		log "no running WirePlumber for ${user:-the desktop user}; cannot check the BlueZ endpoints"
	elif aac_endpoint_since "$started"; then
		log "$user's WirePlumber registered the A2DP AAC endpoint with BlueZ"
	else
		die "$user's WirePlumber (started $(date -d "@$started")) registered no AAC endpoint; as $user run: systemctl --user restart pipewire pipewire-pulse wireplumber"
	fi
	check_headset
}

rollback_set() {
	local name inst target user
	local -a pins=() names=()
	while read -r name inst; do
		[[ $inst == *+gts9u* ]] || continue
		target=$(newest_archive_version "$name")
		[[ -n $target ]] || die "the APT lists offer no $name; run apt-get update first"
		names+=("$name")
		pins+=("$name=$target")
	done < <(installed_pipewire)
	if (( ${#pins[@]} == 0 )); then
		log 'no gts9u PipeWire rebuild is installed; nothing to roll back'
		return 0
	fi
	release_holds "${names[@]}"
	log "reinstalling the archive builds: ${pins[*]}"
	DEBIAN_FRONTEND=noninteractive apt-get install -y --no-remove \
		--no-install-recommends --allow-downgrades "${pins[@]}"
	log 'libfdk-aac2 stays installed, unused; apt autoremove removes it'
	user=$(audio_user)
	if [[ -n $user ]] && has_session "$user"; then
		restart_user_audio "$user"
	fi
}

case ${1:-} in
install) install_set "${2:-$(dirname "$(realpath "$0")")}" ;;
check) check_state ;;
rollback) rollback_set ;;
*) die 'usage: install-pipewire-aac-live.sh install [DEB_DIR] | check | rollback' ;;
esac
