#!/bin/sh
# Assemble the in-place camera kit for the SM-X710 from this repository.
#
#   sh tools/inplace/cameras/make-kit.sh OUTDIR
#   scp -r OUTDIR jstockdale@10.42.0.2:cameras-kit
#
# OUTDIR gets the kit scripts, files/ (the device-package files exactly as a
# gts9wifi build of ubuntu-gts9u-device ships them: the WirePlumber labels are
# rewritten for the board, as scripts/build-device-package.sh does), and
# SHA256SUMS over all of it.  Nothing here touches a device.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../.." && pwd)
pkg=$repo/packaging/ubuntu-gts9u-device
out=${1:?usage: make-kit.sh OUTDIR}

[ ! -e "$out" ] || [ -z "$(ls -A "$out")" ] || {
	echo "$out exists and is not empty" >&2
	exit 1
}
mkdir -p "$out/files"

for script in README.md install.sh rollback.sh verify.sh camera-test.sh; do
	install -m 0644 "$here/$script" "$out/$script"
done

# FILES: "path mode" for each file install.sh puts in place; REMOVED: the two
# files the package no longer ships (install.sh moves them into its backup).
cat > "$out/FILES" <<'EOF'
usr/libexec/ubuntu-gts9u-camera-profile 0755
usr/libexec/ubuntu-gts9u-camera-loopback 0755
usr/libexec/ubuntu-gts9u-camera-enable 0755
usr/libexec/ubuntu-gts9u-camera-relays 0755
usr/libexec/ubuntu-gts9u-camera-udev-refresh 0755
usr/lib/systemd/system/ubuntu-gts9u-camera-loopback.service 0644
usr/lib/systemd/system/ubuntu-gts9u-camera-enable.service 0644
usr/lib/systemd/system/ubuntu-gts9u-camera-relays.service 0644
usr/lib/udev/rules.d/70-gts9u-camera.rules 0644
usr/share/wireplumber/main.lua.d/51-gts9u-camera-backends.lua 0644
EOF
cat > "$out/REMOVED" <<'EOF'
usr/lib/modules-load.d/ubuntu-gts9u-cameras.conf
usr/lib/modprobe.d/ubuntu-gts9u-cameras.conf
EOF
# What ubuntu-gts9u-device 2.55 installed at the paths this kit changes
# (read from the test tablet on 2026-11-07; identical to the repository at
# d7da110).  install.sh refuses to back up anything else.
cat > "$out/BEFORE.sha256" <<'EOF'
43ca861961e60e9d7822635907622d883f31f2ed260a8e2d36dcc7abea612b34  usr/libexec/ubuntu-gts9u-camera-relays
a3b787e64050c7c00544a3159caf1a5b4f26ea236528499089475b494b16fe3a  usr/libexec/ubuntu-gts9u-camera-udev-refresh
79113161c9be2dd07848ed245c95f69dcca6a9e8a265f10c9e5d0f631a46ee30  usr/lib/systemd/system/ubuntu-gts9u-camera-relays.service
4a4ff999b96924860b48296da211453c5e646070639c81ba89a0e98b2db375ac  usr/lib/udev/rules.d/70-gts9u-camera.rules
86c7054d9ee6976a039db7c28cb7b257a1967ecb9e97cd52addd8d25ae3249e2  usr/share/wireplumber/main.lua.d/51-gts9u-camera-backends.lua
bb87430ef8c13d597c0ffb202d001b59bb865a2637fbf2c612e977a379b06270  usr/lib/modules-load.d/ubuntu-gts9u-cameras.conf
334d290f1ff87794cac7db46dd54a4ebffed280c27a5e5154d76fc977d8d4cb9  usr/lib/modprobe.d/ubuntu-gts9u-cameras.conf
EOF

while read -r path mode; do
	install -D -m "$mode" "$pkg/$path" "$out/files/$path"
done < "$out/FILES"

lua=$out/files/usr/share/wireplumber/main.lua.d/51-gts9u-camera-backends.lua
grep -qx 'local gts9u_board = "gts9uwifi"' "$lua"
sed -i 's/^local gts9u_board = "gts9uwifi"$/local gts9u_board = "gts9wifi"/' "$lua"
grep -qx 'local gts9u_board = "gts9wifi"' "$lua"

(cd "$out" && find . -type f ! -name SHA256SUMS | LC_ALL=C sort | sed 's#^\./##' |
	xargs sha256sum > SHA256SUMS)
echo "kit in $out ($(wc -l < "$out/SHA256SUMS") files)"
