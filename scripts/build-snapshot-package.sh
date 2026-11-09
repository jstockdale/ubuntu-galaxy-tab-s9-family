#!/bin/bash
# Rebuild Ubuntu's exact GNOME Camera (Snapshot) source so that it takes
# pictures at the camera's full resolution.
#
# Snapshot takes pictures from its viewfinder stream, which it keeps at 1080p:
# 1280x1080 through libcamera's software ISP.  The patches in
# packaging/gnome-snapshot stream the camera's largest size in picture mode and
# stop camerabin's redundant renegotiations, each of which pipewiresrc 1.0
# turns into a camera restart.  This rebuilds the same source version with a
# version that sorts above the archive's, in the arm64 build chroot the other
# package rebuilds use.
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
base=${UBUNTU_WORKDIR:-/root/ubuntu-gts9u}
buildroot=${BUILDROOT_DIR:-$base/buildroot}
out=${DEB_OUT_DIR:-$base/out/packages}
snapshot_version=46.2-1ubuntu2
snapshot_dsc_sha256=7355f6f715cc8b4a2b9a93422100853863cb932464fb8262d8dd626ea44b9fc2
suffix=+gts9u1
test -x "$buildroot/usr/bin/apt-get"
mkdir -p "$out"
# apt-get source needs deb-src lines; the chroot normally has them already.
if ! grep -rqsE '^deb-src .*noble' "$buildroot/etc/apt/sources.list" "$buildroot/etc/apt/sources.list.d/"; then
    cat > "$buildroot/etc/apt/sources.list.d/gts9u-snapshot-source.list" <<'SRC'
deb-src http://ports.ubuntu.com/ubuntu-ports noble main restricted universe multiverse
deb-src http://ports.ubuntu.com/ubuntu-ports noble-updates main restricted universe multiverse
SRC
fi
rm -rf "$buildroot/build/snapshot-inputs"
mkdir -p "$buildroot/build/snapshot-inputs"
cp "$repo"/packaging/gnome-snapshot/patches/*.patch "$buildroot/build/snapshot-inputs/"
rm -f "$buildroot/etc/resolv.conf"
cp /etc/resolv.conf "$buildroot/etc/resolv.conf"
# A recursive bind, made a slave so that unmounting it cannot unmount the
# host's /dev/pts; a plain bind of /dev is also refused in a user namespace.
mount --rbind /dev "$buildroot/dev"
mount --make-rslave "$buildroot/dev"
mount -t proc proc "$buildroot/proc"
trap 'umount -l "$buildroot/proc"; umount -l "$buildroot/dev"' EXIT
chroot "$buildroot" /usr/bin/env SNAPSHOT_VERSION="$snapshot_version" \
    SNAPSHOT_DSC_SHA256="$snapshot_dsc_sha256" SNAPSHOT_SUFFIX="$suffix" \
    /bin/bash -euo pipefail <<'EOF2'
export DEBIAN_FRONTEND=noninteractive
export DEBEMAIL=noreply@example.invalid DEBFULLNAME="Ubuntu gts9uwifi port contributors"
apt-get update -qq
# Left alone, build-dep satisfies cargo:native and rustc:native with
# cargo-1.74 and rustc-1.74, which install no cargo or rustc command and do
# not match Noble's libstd-rust-dev 1.75.  Install Noble's default toolchain,
# replacing those two if an earlier build-dep installed them.
apt-get install -y --no-install-recommends cargo rustc libstd-rust-dev \
    cargo-1.74- rustc-1.74-
apt-get build-dep -y --no-install-recommends "gnome-snapshot=$SNAPSHOT_VERSION"
cargo --version
rustc --version
rm -rf /build/gnome-snapshot
mkdir -p /build/gnome-snapshot
cd /build/gnome-snapshot
apt-get source --download-only "gnome-snapshot=$SNAPSHOT_VERSION"
echo "$SNAPSHOT_DSC_SHA256  gnome-snapshot_${SNAPSHOT_VERSION}.dsc" | sha256sum -c -
work=$(mktemp -d /build/gnome-snapshot/source.XXXXXX)
dpkg-source -x "gnome-snapshot_${SNAPSHOT_VERSION}.dsc" "$work/tree"
cd "$work/tree"
# Appended to Ubuntu's quilt series; dpkg-buildpackage applies them.
for patch in /build/snapshot-inputs/*.patch; do
    cp "$patch" debian/patches/
    echo "${patch##*/}" >> debian/patches/series
done
{
    printf '%s\n' "gnome-snapshot (${SNAPSHOT_VERSION}${SNAPSHOT_SUFFIX}) noble; urgency=medium" ''
    printf '%s\n' '  * Take pictures at the camera'"'"'s full resolution, without restarting the' \
        '    camera, for the Galaxy Tab S9 image.' ''
    printf ' -- Ubuntu gts9uwifi port contributors <noreply@example.invalid>  %s\n\n' "$(date -R)"
    cat debian/changelog
} > debian/changelog.gts9u
mv debian/changelog.gts9u debian/changelog
# The package's own test target covers only the application crate and times
# out under emulation, so aperture's caps tests run here, on the patched tree:
# dpkg-buildpackage unapplies the series, vendored-crates config included,
# when it finishes.  debian/rules creates the placeholder only for its build.
dpkg-source --before-build .
placeholder=debian/missing-sources/system-deps/src/tests/lib
mkdir -p "$placeholder"
touch "$placeholder/libteststatic.a"
cargo test --offline --manifest-path aperture/Cargo.toml \
    --target-dir "$work/aperture-tests"
rm -rf "$placeholder" aperture/Cargo.lock
DEB_BUILD_OPTIONS='nocheck parallel=8' dpkg-buildpackage -b -uc -us
cp ../*.deb /build/gnome-snapshot/
ls /build/gnome-snapshot/*.deb
EOF2
deb=gnome-snapshot_${snapshot_version}${suffix}_arm64.deb
rm -f "$out"/gnome-snapshot_*"$suffix"_arm64.deb
cp "$buildroot/build/gnome-snapshot/$deb" "$out/"
test "$(dpkg-deb -f "$out/$deb" Version)" = "${snapshot_version}${suffix}"
dpkg-deb -c "$out/$deb" | grep -q ' ./usr/bin/snapshot$'
ls -l "$out/$deb"
sha256sum "$out/$deb"
