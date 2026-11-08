#!/bin/bash
# Keep every kernel's module signing key.
#
# Each kernel build generates its own module signing key and the kernel trusts
# only that one.  The images enforce lockdown (integrity), so a module for a
# kernel that is already flashed loads only if it is signed with that exact
# key.  The key lives in the build directory, which is disposable
# (KERNEL_CLEAN=1 discards it); on 2026-11-07 the key behind the kernel on a
# test unit turned out to be gone with its build directory.  Every kernel
# build therefore archives its key here, named by the key ID the kernel logs
# at boot ("Loaded X.509 cert '...: <key id>'").
#
# Builds that do not start clean keep the previous key, so one key can sign
# several kernels.  Each of those builds gets its own builds/<build id>/ under
# the key, with the .config and Module.symvers that modules for that kernel
# are built against; the build ID is the start of the Image.gz's sha256, or
# of the .config's for a saved key directory without an image.
#
#   kernel-key-registry.sh add BUILD_DIR [NOTE]   archive one build's key
#   kernel-key-registry.sh scan DIR...            archive every key found
#   kernel-key-registry.sh mirror                 copy to every mirror now
#   kernel-key-registry.sh list
#
# BUILD_DIR is a kernel object directory (certs/signing_key.pem) or a saved
# key directory (signing_key.pem at its top).  The registry is
# KERNEL_KEY_REGISTRY, by default $UBUNTU_WORKDIR/kernel-keys.  Every
# directory or "rclone:REMOTE:PATH" named in its MIRRORS file, one per line,
# receives a copy after each change; a backup only ever gains keys.  A mirror
# directory must already exist: one on an unmounted drive is reported, not
# recreated underneath the mount point.  Run with sudo, the registry stays
# owned by the user who ran it.
#
# The private keys are secrets: the registry is 0700, the keys 0600, and
# neither belongs in the repository, an image or a release.
set -euo pipefail

base=${UBUNTU_WORKDIR:-/root/ubuntu-gts9u}
registry=${KERNEL_KEY_REGISTRY:-$base/kernel-keys}
index=$registry/REGISTRY.tsv
stage=$(mktemp -d)
partial=
trap 'rm -rf -- "$stage" ${partial:+"$partial"}' EXIT

usage() {
	sed -n 's/^#   //p' "$0" >&2
	exit 2
}

# Under sudo, give the registry back to the user who asked for it.
owner() {
	if [ "$(id -u)" = 0 ] && [ -n "${SUDO_UID:-}" ]; then
		chown -R "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" "$@"
	fi
}

init_registry() {
	if [ ! -d "$registry" ]; then
		mkdir -p "$registry"
		chmod 0700 "$registry"
		owner "$registry"
	fi
	if [ ! -f "$index" ]; then
		printf 'key_id\tserial\tnot_before\tbuild\tkernel_release\timage_gz_sha256\tarchived\tsource\tnote\n' > "$index"
		owner "$index"
	fi
}

mirror() {
	local list=$registry/MIRRORS dest failed=0
	if [ ! -f "$list" ]; then
		echo "WARNING: $list names no backup; the keys exist only in $registry" >&2
		return 1
	fi
	while IFS= read -r dest; do
		case $dest in '' | '#'*) continue ;; esac
		if [[ $dest == rclone:* ]]; then
			# An rclone remote, e.g. "rclone:gdrive-keys:" (ideally a crypt
			# remote).  copy never deletes at the destination.  Under sudo it
			# runs as the user whose rclone.conf names the remote.  Its stdin
			# is not the MIRRORS file, which a password prompt would eat.
			local as=()
			[ "$(id -u)" = 0 ] && [ -n "${SUDO_UID:-}" ] &&
				as=(sudo -u "#$SUDO_UID" -H)
			if "${as[@]}" rclone copy --exclude MIRRORS "$registry" "${dest#rclone:}" < /dev/null; then
				echo "kernel keys copied to ${dest#rclone:}"
			else
				echo "WARNING: kernel keys NOT copied to ${dest#rclone:}" >&2
				failed=1
			fi
			continue
		fi
		if [ -d "$dest" ] && chmod 0700 "$dest" &&
			cp -a --no-preserve=ownership "$registry/." "$dest/"; then
			owner "$dest"
			echo "kernel keys mirrored to $dest"
		else
			echo "WARNING: kernel keys NOT mirrored to $dest (missing or not writable)" >&2
			failed=1
		fi
	done < "$list"
	return "$failed"
}

add() {
	local dir=${1:?build or key directory} note=${2:-}
	local pem x509 config symvers release_file image
	dir=$(cd "$dir" && pwd)
	if [ -f "$dir/certs/signing_key.pem" ]; then
		pem=$dir/certs/signing_key.pem
		x509=$dir/certs/signing_key.x509
		release_file=$dir/include/config/kernel.release
		image=$dir/arch/arm64/boot/Image.gz
	elif [ -f "$dir/signing_key.pem" ]; then
		pem=$dir/signing_key.pem
		x509=$dir/signing_key.x509
		release_file=$dir/kernel.release
		image=
	else
		echo "no signing_key.pem in $dir or $dir/certs" >&2
		return 1
	fi
	config=$dir/.config
	symvers=$dir/Module.symvers
	if [ ! -r "$pem" ]; then
		echo "cannot read $pem (run with sudo)" >&2
		return 1
	fi

	# The certificate the kernel embeds, taken from the key file itself so a
	# stray signing_key.x509 cannot pair a key with the wrong certificate.
	openssl x509 -in "$pem" -outform DER -out "$stage/signing_key.x509"
	if [ -f "$x509" ] && ! cmp -s "$x509" "$stage/signing_key.x509"; then
		echo "$x509 does not match the certificate in $pem" >&2
		return 1
	fi
	if ! cmp -s <(openssl pkey -in "$pem" -pubout) \
		<(openssl x509 -in "$pem" -noout -pubkey); then
		echo "the private key in $pem does not match its certificate" >&2
		return 1
	fi

	local key_id serial not_before release=- image_sha=- build now
	key_id=$(openssl x509 -in "$pem" -noout -ext subjectKeyIdentifier |
		sed -n '2s/[[:space:]:]//gp' | tr 'A-F' 'a-f')
	serial=$(openssl x509 -in "$pem" -noout -serial | sed 's/^serial=//')
	not_before=$(date -u -d "$(openssl x509 -in "$pem" -noout -startdate |
		sed 's/^notBefore=//')" +%Y-%m-%dT%H:%M:%SZ)
	[[ $key_id =~ ^[0-9a-f]{40}$ ]] || {
		echo "unexpected key ID '$key_id' in $pem" >&2
		return 1
	}
	[ -f "$release_file" ] && release=$(tr -d '[:space:]' < "$release_file")
	if [ -n "$image" ] && [ -f "$image" ]; then
		image_sha=$(sha256sum "$image" | cut -d' ' -f1)
		build=${image_sha:0:16}
	elif [ -f "$config" ]; then
		build=config-$(sha256sum "$config" | cut -c1-16)
	else
		build=key-only
	fi
	now=$(date -u +%Y-%m-%dT%H:%M:%SZ)

	init_registry
	local entry=$registry/$key_id changed=0
	if [ -d "$entry" ]; then
		if ! cmp -s "$pem" "$entry/signing_key.pem"; then
			echo "$entry holds a different key with the same ID" >&2
			return 1
		fi
	else
		# Assembled beside the registry and renamed into place, so an
		# interrupted run leaves no half entry; the trap removes the rest.
		partial=$(mktemp -d "$registry/.partial.XXXXXX")
		install -m 0600 "$pem" "$partial/signing_key.pem"
		install -m 0644 "$stage/signing_key.x509" "$partial/signing_key.x509"
		cat > "$partial/INFO" <<EOF
key_id=$key_id
serial=$serial
not_before=$not_before
first_archived=$now on $(hostname) from $dir
EOF
		mkdir -m 0700 "$partial/builds"
		mv -T "$partial" "$entry"
		partial=
		echo "kernel key $key_id ARCHIVED in $entry"
		changed=1
	fi

	local record=$entry/builds/$build
	if [ -d "$record" ]; then
		echo "kernel key $key_id, build $build already archived"
	else
		mkdir -p -m 0700 "$entry/builds"
		partial=$(mktemp -d "$entry/builds/.partial.XXXXXX")
		[ -f "$config" ] && install -m 0644 "$config" "$partial/.config"
		[ -f "$symvers" ] && install -m 0644 "$symvers" "$partial/Module.symvers"
		[ "$release" != - ] && printf '%s\n' "$release" > "$partial/kernel.release"
		cat > "$partial/INFO" <<EOF
key_id=$key_id
build=$build
kernel_release=$release
image_gz_sha256=$image_sha
source=$dir
archived=$now on $(hostname)
note=$note
EOF
		mv -T "$partial" "$record"
		partial=
		printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$key_id" "$serial" \
			"$not_before" "$build" "$release" "$image_sha" "$now" "$dir" \
			"$note" >> "$index"
		echo "kernel key $key_id: build $build recorded"
		changed=1
	fi
	owner "$entry" "$index"
	if [ "$changed" = 1 ]; then
		mirror || echo "WARNING: the key is in $registry but not in every backup" >&2
	fi
}

scan() {
	local root found
	[ $# -gt 0 ] || usage
	for root in "$@"; do
		while IFS= read -r -d '' found; do
			found=$(dirname "$found")
			[ "$(basename "$found")" = certs ] && found=$(dirname "$found")
			# Its own process, so errexit still holds inside add.
			bash "$0" add "$found" "found by scan" || echo "skipped $found" >&2
		done < <(find "$root" -name signing_key.pem -type f -print0 2>/dev/null)
	done
}

case ${1:-} in
add) shift; add "$@" ;;
scan) shift; scan "$@" ;;
mirror) init_registry; mirror ;;
list) column -t -s $'\t' "$index" ;;
*) usage ;;
esac
