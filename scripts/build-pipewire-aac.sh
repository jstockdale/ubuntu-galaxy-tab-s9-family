#!/bin/bash
# Rebuild Ubuntu's exact PipeWire source with the Bluetooth AAC codec enabled.
#
# Ubuntu builds libspa-0.2-bluetooth without AAC because fdk-aac lives in
# multiverse, so A2DP sinks that offer AAC (Beats, AirPods, most TWS) fall back
# to SBC/SBC-XQ.  This rebuilds the same source version with
# -Dbluez5-codec-aac=enabled against libfdk-aac-dev and a version that sorts
# above the archive's, in the arm64 build chroot the other package rebuilds use.
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
base=${UBUNTU_WORKDIR:-/root/ubuntu-gts9u}
buildroot=${BUILDROOT_DIR:-$base/buildroot}
out=${DEB_OUT_DIR:-$base/out/packages}
pw_version=${PIPEWIRE_VERSION:-1.0.5-1ubuntu3.3}
suffix=${PIPEWIRE_AAC_SUFFIX:-+gts9u1}
test -x "$buildroot/usr/bin/apt-get"
mkdir -p "$out"
# apt-get source needs deb-src lines; the chroot normally has them already.
if ! grep -rqsE '^deb-src .*noble-updates' "$buildroot/etc/apt/sources.list" "$buildroot/etc/apt/sources.list.d/"; then
    cat > "$buildroot/etc/apt/sources.list.d/gts9u-pipewire-source.list" <<'SRC'
deb-src http://ports.ubuntu.com/ubuntu-ports noble main restricted universe multiverse
deb-src http://ports.ubuntu.com/ubuntu-ports noble-updates main restricted universe multiverse
SRC
fi
mount --bind /dev "$buildroot/dev"
mount -t devpts devpts "$buildroot/dev/pts"
mount -t proc proc "$buildroot/proc"
trap 'umount -l "$buildroot/proc"; umount -l "$buildroot/dev/pts"; umount -l "$buildroot/dev"' EXIT
PW_VERSION=$pw_version PW_SUFFIX=$suffix chroot "$buildroot" /usr/bin/env PW_VERSION="$pw_version" PW_SUFFIX="$suffix" /bin/bash -euo pipefail <<'EOF2'
export DEBIAN_FRONTEND=noninteractive
export DEBEMAIL=noreply@example.invalid DEBFULLNAME="Ubuntu gts9uwifi port contributors"
apt-get update -qq
apt-get build-dep -y --no-install-recommends pipewire
apt-get install -y --no-install-recommends libfdk-aac-dev devscripts
rm -rf /build/pipewire-aac
mkdir -p /build/pipewire-aac
cd /build/pipewire-aac
apt-get source --download-only "pipewire=$PW_VERSION"
work=$(mktemp -d /build/pipewire-aac/source.XXXXXX)
dpkg-source -x pipewire_${PW_VERSION}.dsc "$work/tree"
cd "$work/tree"
# Enable the AAC codec in whatever form the packaging expresses it.
if grep -qE 'bluez5-codec-aac=disabled' debian/rules; then
    sed -i -E 's/bluez5-codec-aac=disabled/bluez5-codec-aac=enabled/g' debian/rules
elif ! grep -qE 'bluez5-codec-aac=enabled' debian/rules; then
    # Append to the meson configure arguments of the override.
    sed -i -E '0,/dh_auto_configure --/s|(dh_auto_configure -- *\\?)|\1\n\t\t-Dbluez5-codec-aac=enabled \\|' debian/rules
fi
grep -nE 'bluez5-codec-aac' debian/rules
# Ubuntu declares Build-Conflicts: libfdk-aac-dev precisely so the archive
# build cannot pick AAC up; drop that entry (and the field if it is then
# empty), then build-depend on fdk-aac.  The binary dependency comes from shlibs.
perl -0pi -e 's/^(Build-Conflicts:(?:[^\n]|\n[ \t])*)/ my $f = $1; $f =~ s|,?[ \t]*libfdk-aac-dev(?:[ \t]*\([^)]*\))?||g; $f =~ s|^Build-Conflicts:[ \t]*,[ \t]*|Build-Conflicts: |; $f /me; s/^Build-Conflicts:[ \t]*\n//m' debian/control
grep -nE '^Build-Conflicts' debian/control || echo "no Build-Conflicts field left"
if ! grep -qE '^Build-Depends:.*libfdk-aac-dev' debian/control; then
    sed -i -E '0,/^Build-Depends:/s/^Build-Depends:/Build-Depends: libfdk-aac-dev,/' debian/control
fi
{
    printf '%s\n' "pipewire (${PW_VERSION}${PW_SUFFIX}) noble; urgency=medium" ''
    printf '%s\n' '  * Enable the Bluetooth AAC codec (libfdk-aac) for the Galaxy Tab S9 image.' ''
    printf ' -- Ubuntu gts9uwifi port contributors <noreply@example.invalid>  %s\n\n' "$(date -R)"
    cat debian/changelog
} > debian/changelog.gts9u
mv debian/changelog.gts9u debian/changelog
DEB_BUILD_OPTIONS='nocheck parallel=8' dpkg-buildpackage -b -uc -us
cp ../*.deb /build/pipewire-aac/
ls /build/pipewire-aac/*.deb
EOF2
rm -f "$out"/libspa-0.2-bluetooth_*"$suffix"_arm64.deb "$out"/pipewire*_*"$suffix"_*.deb "$out"/libpipewire*_*"$suffix"_*.deb "$out"/libspa-0.2-*_*"$suffix"_*.deb "$out"/gstreamer1.0-pipewire_*"$suffix"_*.deb
cp "$buildroot"/build/pipewire-aac/*"$suffix"_*.deb "$out/"
echo "pipewire ${pw_version}${suffix} debs:"; ls "$out" | grep -E "$suffix" | sed 's/^/  /'
