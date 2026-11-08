#!/bin/bash
# Build the ubuntu-gts9u-device Debian package from packaging/.
#
# The tree under packaging/ubuntu-gts9u-device is the package layout verbatim,
# so what ships is exactly what is versioned here, with these exceptions made
# below: the sensor registry path follows DEVICE, translations are compiled,
# and the package's symlinks are created here rather than versioned.
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
. "$repo/scripts/lib/device.sh"
base=${UBUNTU_WORKDIR:-/root/ubuntu-gts9u}
src=$repo/packaging/ubuntu-gts9u-device
out=${DEB_OUT_DIR:-$base/out/packages}

test -f "$src/DEBIAN/control"

version=$(awk '/^Version:/ {print $2}' "$src/DEBIAN/control")
arch=$(awk '/^Architecture:/ {print $2}' "$src/DEBIAN/control")
staging=$base/build/deb/ubuntu-gts9u-device
deb=$out/ubuntu-gts9u-device_${version}_${arch}.deb

rm -rf -- "$staging"
mkdir -p "$staging" "$out"
cp -a "$src/." "$staging/"
# Test imports may leave ignored bytecode beside extensionless Python helpers.
# Generated caches are not package source or reproducible runtime artifacts.
find "$staging" -type f -name '*.pyc' -delete
find "$staging" -type d -name '__pycache__' -empty -delete

# --- per-device sensor registry tree --------------------------------------
# hexagonrpcd serves the ADSP sensor protection domain from the HexagonFS tree
# the firmware overlay installs under qcom/sm8550/Samsung/$DEVICE_SENSOR_DIR.
# The files naming that tree are versioned with the Ultra's directory, so point
# them at the board this package is built for.  For gts9uwifi nothing changes.
# An X710 package carrying the Ultra's path pointed the daemon at a directory
# that does not exist on that board: SSC never published a sensor, and
# rotation and the light sensor failed on every boot.
# hexagonrpcd cannot find the tree by itself either: its compatible-based guess
# takes the SoC from the first "qcom," entry, and both board DTs list
# qcom,kalama-mtp first for the bootloader's DTB match.
sensor_root=/usr/share/qcom/sm8550/Samsung
sensor_ultra="$sensor_root/gts9uwifi([^[:alnum:]_]|\$)"
for f in \
	usr/lib/systemd/system/hexagonrpcd-adsp-sensorspd.service.d/10-gts9u-hexagonfs.conf \
	usr/lib/systemd/system/ubuntu-gts9u-sensor-registry.service \
	usr/lib/tmpfiles.d/ubuntu-gts9u-sensors.conf \
	usr/libexec/ubuntu-gts9u-sensor-registry; do
	grep -Eq "$sensor_ultra" "$staging/$f" || {
		echo "$f no longer names $sensor_root/gts9uwifi" >&2
		exit 1
	}
	sed -Ei "s#($sensor_root/)gts9uwifi([^[:alnum:]_]|\$)#\\1$DEVICE_SENSOR_DIR\\2#g" \
		"$staging/$f"
done
# Anything else naming the Ultra's tree would silently miss this board's.
if [ "$DEVICE_SENSOR_DIR" != gts9uwifi ] &&
	grep -rIlE "$sensor_ultra" "$staging" >&2; then
	echo "these files still name $sensor_root/gts9uwifi on $DEVICE" >&2
	exit 1
fi

# --- per-device camera labels ------------------------------------------------
# The SM-X710's two cameras have the same libcamera node names as two of the
# SM-X910's four, and WirePlumber's config sandbox cannot read the device tree,
# so the PipeWire labels are chosen here by board.  Everything else in the
# camera stack (loopback nodes, relays, udev aliases) decides at runtime.
camera_lua=usr/share/wireplumber/main.lua.d/51-gts9u-camera-backends.lua
grep -qx 'local gts9u_board = "gts9uwifi"' "$staging/$camera_lua" || {
	echo "$camera_lua no longer carries its board token" >&2
	exit 1
}
sed -i "s/^local gts9u_board = \"gts9uwifi\"\$/local gts9u_board = \"$DEVICE\"/" \
	"$staging/$camera_lua"
grep -q "^  $DEVICE = {\$" "$staging/$camera_lua" || {
	echo "$camera_lua has no camera labels for $DEVICE" >&2
	exit 1
}

# Boot-lifetime native secure owner and the matching signed IRQ module are
# built separately. No proprietary runtime/firmware bytes enter this package.
owner=$base/out/fingerprint-secure/ubuntu-gts9u-fingerprint-secure-owner
irq=${SPSS_IRQ_OUT_DIR:-$base/out/spss-irq-module}/qcom_spss_irq.ko
test -x "$owner" || { echo 'Run scripts/build-fingerprint-secure-owner.sh first' >&2; exit 1; }
test -f "$irq" || { echo 'Run scripts/build-spss-irq-module.sh first' >&2; exit 1; }
release=$(modinfo -F vermagic "$irq" | cut -d' ' -f1)
irq_sig_key=$(modinfo -F sig_key "$irq")
build_dir=${KERNEL_BUILD_DIR:-$base/build/linux-$DEVICE}
kernel_out=${KERNEL_OUT_DIR:-$base/out/kernel-$DEVICE}
for f in "$build_dir/certs/signing_key.x509" "$kernel_out/config" "$kernel_out/kernel.release"; do
	test -f "$f" || { echo "missing $f (kernel build directory not mounted or kernel not built?)" >&2; exit 1; }
done
cert_serial=$(openssl x509 -inform DER -in "$build_dir/certs/signing_key.x509" \
	-noout -serial | sed 's/^serial=//' | tr '[:lower:]' '[:upper:]')
module_serial=$(printf '%s' "$irq_sig_key" | tr -d ':' | tr '[:lower:]' '[:upper:]')
test "$module_serial" = "$cert_serial" || {
	echo "IRQ module signing key does not match this kernel build" >&2
	exit 1
}
cmp -s "$kernel_out/config" "$build_dir/.config" || {
	echo "IRQ module object tree does not match the released kernel configuration" >&2
	exit 1
}
test "$release" = "$(cat "$kernel_out/kernel.release")" || {
	echo "IRQ module release does not match the released kernel" >&2
	exit 1
}
[[ "$release" =~ ^[a-zA-Z0-9.+_-]+$ ]]
install -m0755 "$owner" "$staging/usr/libexec/"
install -d "$staging/usr/lib/modules/$release/updates"
install -m0644 "$irq" "$staging/usr/lib/modules/$release/updates/"

# --- flashlight tile translations -----------------------------------------
# The other place where what ships is not byte-for-byte what is versioned: the
# catalogues are kept as .po next to the extension, because a .po is reviewable
# and a .mo is not, and they are compiled here.  The .po sources are then
# dropped from the staging tree so the package carries only the compiled form.
#
# The tile's source string is English, so a language with no catalogue falls
# back to "Flashlight" rather than showing everyone Spanish.
extension=$staging/usr/share/gnome-shell/extensions/flashlight@ubuntu-gts9u
if [ -d "$extension/po" ]; then
	command -v msgfmt >/dev/null || {
		echo 'msgfmt is missing; run scripts/install-build-deps.sh' >&2
		exit 1
	}
	compiled=0
	for po in "$extension"/po/*.po; do
		lang=$(basename "$po" .po)
		install -d "$extension/locale/$lang/LC_MESSAGES"
		msgfmt -o "$extension/locale/$lang/LC_MESSAGES/gts9u-flashlight.mo" "$po"
		compiled=$((compiled + 1))
	done
	rm -rf -- "$extension/po"
	echo "flashlight translations compiled: $compiled"
	[ "$compiled" -gt 0 ] || { echo 'no catalogues compiled' >&2; exit 1; }
fi

# --- modes, ownership and links -------------------------------------------
# Normalise ownership and modes: a package must not inherit whatever the build
# host happened to have.  tests/test_device_package.py runs this section and
# the next on a copy of the tree.
chown -R root:root "$staging"
find "$staging" -type d -exec chmod 0755 {} +
find "$staging" -type f -exec chmod 0644 {} +
find "$staging/usr/libexec" -type f -exec chmod 0755 {} + 2>/dev/null || true
find "$staging/usr/bin" -type f -name 'gts9u-*' -exec chmod 0755 {} + 2>/dev/null || true
find "$staging/usr/lib/systemd/system-sleep" -type f -exec chmod 0755 {} + \
	2>/dev/null || true
# systemd does not run a generator that is not executable, and the boot goes
# on without it.  Device 2.56 shipped the fingerprint gate 0644, so fprintd was
# never masked on the SM-X710 and the greeter waited 25 s for it.
find "$staging/usr/lib/systemd/system-generators" -type f -exec chmod 0755 {} + \
	2>/dev/null || true
# initramfs-tools refuses to run a hook that is not executable, and silently
# skips it rather than failing the build.
find "$staging/usr/share/initramfs-tools" -type f -exec chmod 0755 {} + \
	2>/dev/null || true
find "$staging/DEBIAN" -type f -name 'p*inst' -exec chmod 0755 {} + 2>/dev/null || true
find "$staging/DEBIAN" -type f -name 'p*rm' -exec chmod 0755 {} + 2>/dev/null || true

# The package's symlinks are made here rather than versioned: a checkout with
# core.symlinks=false (Git for Windows' default) turns a git symlink into a
# small text file holding the target, which would ship as a broken unit or a
# dconf profile dconf cannot parse.
# Masks for alsa-utils' state units: a late alsactl restore of a saved mixer
# state can undo the UCM speaker sequence (upstream 4b07d7b, "Restore stereo
# speaker routing after ALSA state recovery", met it on the SM-X910).  The UCM
# profile and ubuntu-gts9u-speaker-cal set the mixer on every boot instead.
# Every level is listed: install -d -m does not give its mode to the parents
# it creates on the way, and those would keep the build host's umask.
install -d -m0755 "$staging/etc" "$staging/etc/systemd" "$staging/etc/systemd/system" \
	"$staging/usr/share/dconf" "$staging/usr/share/dconf/profile"
ln -sfn /dev/null "$staging/etc/systemd/system/alsa-restore.service"
ln -sfn /dev/null "$staging/etc/systemd/system/alsa-state.service"
# The dconf user profile follows a file ubuntu-gts9u-suspend-mode writes under
# /run, so the suspend-off defaults apply only while that mode is chosen.
ln -sfn /run/ubuntu-gts9u/dconf/profile-user "$staging/usr/share/dconf/profile/user"

# --- conffiles and timestamps ---------------------------------------------
# Only regular files are conffiles; the links above are not.
if [ -d "$staging/etc" ]; then
	(cd "$staging" && find etc -type f | LC_ALL=C sort | sed 's|^|/|') \
		> "$staging/DEBIAN/conffiles"
fi
# Deterministic output: without a fixed mtime the .deb changes hash on every
# build even when its contents do not.
find "$staging" -exec touch -h -d '@0' {} +

# --- build ----------------------------------------------------------------
dpkg-deb --root-owner-group --build "$staging" "$deb"
dpkg-deb --contents "$deb"
sha256sum "$deb"
