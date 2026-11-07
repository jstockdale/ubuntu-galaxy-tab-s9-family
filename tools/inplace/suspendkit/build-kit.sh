#!/bin/sh
# Assemble ~/suspendkit for the tablet from this repository (run on the host):
#
#   sh tools/inplace/suspendkit/build-kit.sh [OUTDIR]     default: ./suspendkit
#   tar -C OUTDIR -cf - . | ssh jstockdale@10.42.0.2 'mkdir -p ~/suspendkit && tar -C ~/suspendkit -xf -'
#
# The kit holds the reliability runner and the ladder (they run from the kit
# directory, nothing to install), the ADSP-start sleep lock (unit and helper,
# installed by install.sh), install.sh, rollback.sh, verify.sh, this README
# and SHA256SUMS over all of it.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../.." && pwd)
out=${1:-$PWD/suspendkit}

# Only ever replace a directory this script made.
if [ -e "$out" ]; then
	[ -e "$out/.suspendkit" ] || [ -z "$(ls -A "$out" 2>/dev/null)" ] || {
		echo "$out exists and is not a suspendkit build; choose another OUTDIR" >&2
		exit 1
	}
	rm -rf -- "$out"
fi
mkdir -p "$out/files"
: > "$out/.suspendkit"

install -m 0755 "$here/install.sh" "$here/rollback.sh" "$here/verify.sh" "$out/"
install -m 0644 "$here/README.md" "$out/"
install -m 0755 "$repo/scripts/diagnostics/suspend/gts9u-suspend-cycles.sh" \
	"$repo/scripts/diagnostics/suspend/x710-suspend-ladder.sh" "$out/"
install -m 0755 "$repo/packaging/ubuntu-gts9u-device/usr/libexec/ubuntu-gts9u-adsp-start-inhibit" "$out/files/"
install -m 0644 "$repo/packaging/ubuntu-gts9u-device/usr/lib/systemd/system/ubuntu-gts9u-adsp-start-inhibit.service" "$out/files/"
git -C "$repo" rev-parse --short HEAD > "$out/REVISION" 2>/dev/null || echo unknown > "$out/REVISION"

(cd "$out" && find . -type f ! -name SHA256SUMS ! -name .suspendkit | sed 's#^\./##' |
	LC_ALL=C sort | while IFS= read -r f; do sha256sum "$f"; done > SHA256SUMS)
echo "kit in $out ($(wc -l < "$out/SHA256SUMS") files, revision $(cat "$out/REVISION"))"
echo "copy it with: tar -C $out -cf - . | ssh jstockdale@10.42.0.2 'mkdir -p ~/suspendkit && tar -C ~/suspendkit -xf -'"
