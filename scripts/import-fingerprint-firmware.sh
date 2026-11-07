#!/bin/bash
# Package the signed EL721 secure apps and the owner's device-specific
# calibration files as a local, non-redistributable .deb.
#
# The signed app segments are revision-matched firmware: they come from the
# staging directory scripts/stage-firmware.py produced (FIRMWARE_DIR), and
# each file is verified against that staging's PROVENANCE.json — the same
# recorded hashes the rest of the build trusts.  This replaces the original
# single-build hash blocks that were pinned to one owner's firmware and could
# never validate another bootloader revision's set.
#
# calib.dat, egoptbds.dat and the optional cell_id are per-unit: they are
# harvested from the specific tablet's own stock install before it is wiped
# and passed in a directory of their own.  They have no universal pin; their
# hashes are recorded into the package manifest, and a builder who wants a
# hard gate can export GTS9U_CALIB_SHA256 / GTS9U_EGOPTBDS_SHA256.
#
# Proprietary bytes never enter the repository and the package is built for
# exactly one unit.
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
base=${UBUNTU_WORKDIR:-/root/ubuntu-gts9u}
out=${DEB_OUT_DIR:-$base/out/packages}
unit_dir=${1:-${GTS9U_FINGERPRINT_FIRMWARE_DIR:-}}
ta_dir=${GTS9U_FINGERPRINT_TA_DIR:-${FIRMWARE_DIR:-}}
version=${GTS9U_FINGERPRINT_FIRMWARE_VERSION:-1.1}

[ -n "$unit_dir" ] || {
	echo "usage: $0 DIRECTORY-WITH-THIS-UNIT'S-calib.dat-AND-egoptbds.dat" >&2
	echo "the signed app segments come from FIRMWARE_DIR (stage-firmware.py output)" >&2
	exit 2
}
if [ -d "$unit_dir/ta" ]; then
	unit_dir=$unit_dir/ta
fi
test -d "$unit_dir"
[ -n "$ta_dir" ] || {
	echo 'set FIRMWARE_DIR (or GTS9U_FINGERPRINT_TA_DIR) to a staging produced' >&2
	echo 'by scripts/stage-firmware.py; it supplies the signed app segments' >&2
	exit 2
}
provenance=$ta_dir/PROVENANCE.json
test -f "$provenance" || {
	echo "missing $provenance: the staging was not made by stage-firmware.py" >&2
	exit 2
}

ta_files=$(for app in dualfp skeymast hwvault; do
	for n in 0 1 2 3 4 5 6 7 8; do printf '%s.b0%s\n' "$app" "$n"; done
done)

# One line per segment, "sha256 name", from the staging's own record.
expected_tas=$(python3 - "$provenance" <<'PY' $ta_files
import json, sys
record = json.load(open(sys.argv[1]))
hashes = record.get("files_sha256", {})
package = record.get("package", {})
missing = [n for n in sys.argv[2:] if n not in hashes]
if missing:
    sys.exit("staging has no recorded hash for: " + " ".join(missing))
for name in sys.argv[2:]:
    print(hashes[name], name)
print(f'# staged from {package.get("build_id", "?")} bl{package.get("bl_rev", "?")}',
      file=sys.stderr)
PY
)

fw_build_id=$(python3 -c 'import json,sys
print(json.load(open(sys.argv[1]))["package"]["build_id"])' "$provenance")
fw_blrev=$(python3 -c 'import json,sys
print(json.load(open(sys.argv[1]))["package"]["bl_rev"])' "$provenance")

while read -r hash name; do
	file=$ta_dir/$name
	test -f "$file" || { echo "missing $file" >&2; exit 1; }
	actual=$(sha256sum "$file" | awk '{print $1}')
	[ "$actual" = "$hash" ] || {
		echo "$name does not match the $fw_build_id staging's recorded hash" >&2
		exit 1
	}
done <<EOF
$expected_tas
EOF

for name in calib.dat egoptbds.dat; do
	file=$unit_dir/$name
	test -s "$file" || { echo "missing or empty per-unit file: $file" >&2; exit 1; }
done
calib_sha=$(sha256sum "$unit_dir/calib.dat" | awk '{print $1}')
egoptbds_sha=$(sha256sum "$unit_dir/egoptbds.dat" | awk '{print $1}')
if [ -n "${GTS9U_CALIB_SHA256:-}" ] && [ "$calib_sha" != "$GTS9U_CALIB_SHA256" ]; then
	echo "calib.dat is $calib_sha, expected $GTS9U_CALIB_SHA256" >&2
	exit 1
fi
if [ -n "${GTS9U_EGOPTBDS_SHA256:-}" ] && [ "$egoptbds_sha" != "$GTS9U_EGOPTBDS_SHA256" ]; then
	echo "egoptbds.dat is $egoptbds_sha, expected $GTS9U_EGOPTBDS_SHA256" >&2
	exit 1
fi

staging=$base/build/deb/ubuntu-gts9u-fingerprint-firmware
rm -rf -- "$staging"
mkdir -p "$staging/DEBIAN" \
	"$staging/usr/lib/firmware/gts9u/fingerprint" \
	"$staging/usr/share/doc/ubuntu-gts9u-fingerprint-firmware"
while read -r _ name; do
	install -m0644 "$ta_dir/$name" \
		"$staging/usr/lib/firmware/gts9u/fingerprint/$name"
done <<EOF
$expected_tas
EOF
for name in calib.dat egoptbds.dat; do
	install -m0600 "$unit_dir/$name" \
		"$staging/usr/lib/firmware/gts9u/fingerprint/$name"
done
if [ -f "$unit_dir/cell_id" ]; then
	cell_id=$(tr -d '\r\n' < "$unit_dir/cell_id")
	case "$cell_id" in
		??????????????????????) ;;
		*) echo 'cell_id must contain exactly 22 hexadecimal characters' >&2; exit 1 ;;
	esac
	case "$cell_id" in
		*[!0123456789abcdefABCDEF]*)
			echo 'cell_id must contain exactly 22 hexadecimal characters' >&2
			exit 1
			;;
	esac
	printf '%s\n' "$(printf '%s' "$cell_id" | tr '[:upper:]' '[:lower:]')" > \
		"$staging/usr/lib/firmware/gts9u/fingerprint/cell_id"
fi
cat > "$staging/DEBIAN/control" <<EOF
Package: ubuntu-gts9u-fingerprint-firmware
Version: $version
Section: non-free-firmware
Priority: optional
Architecture: all
Maintainer: Local builder <noreply@example.invalid>
Description: locally imported Samsung secure apps for the SM-X910 EL721
 User-supplied signed dualfp, skeymast and HwVault images from the
 $fw_build_id (bl$fw_blrev) firmware staging, with this one unit's EL721
 calibration data. Built locally; not redistributable.
EOF
{
	printf 'ta_source_build: %s\nta_source_bl_rev: %s\n' "$fw_build_id" "$fw_blrev"
	printf 'calib.dat_sha256: %s\negoptbds.dat_sha256: %s\n' \
		"$calib_sha" "$egoptbds_sha"
	printf 'imported: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$staging/usr/share/doc/ubuntu-gts9u-fingerprint-firmware/IMPORT-MANIFEST.txt"
cat > "$staging/usr/share/doc/ubuntu-gts9u-fingerprint-firmware/copyright" <<'EOF'
The files dualfp.b00 through dualfp.b08, skeymast.b00 through skeymast.b08,
hwvault.b00 through hwvault.b08 and the device-specific EL721 calibration data
are proprietary Samsung/Qualcomm firmware and configuration.
This package was built locally from files supplied by the device owner.
Redistribution is not granted by this project.
EOF
# dpkg-deb --root-owner-group records every archive entry as root:root, so
# no filesystem chown is needed and the import also runs unprivileged.
find "$staging" -type d -exec chmod 0755 {} +
find "$staging" -type f -exec chmod 0644 {} +
find "$staging" -exec touch -h -d '@0' {} +
mkdir -p "$out"
deb=$out/ubuntu-gts9u-fingerprint-firmware_${version}_all.deb
dpkg-deb --root-owner-group --build "$staging" "$deb" >/dev/null
echo "built $deb (TA set: $fw_build_id bl$fw_blrev)"
sha256sum "$deb"
