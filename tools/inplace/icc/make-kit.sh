#!/bin/bash
# Assemble the in-place interconnect kit from this tree, on the build host.
# The helper and unit are copied from packaging/ubuntu-gts9u-device, so the
# kit installs exactly what the next device package ships.
#
#   tools/inplace/icc/make-kit.sh [OUTDIR]        default: work/inplace-icc
#   scp -r OUTDIR jstockdale@10.42.0.2:icc        the tablet then has ~/icc
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../.." && pwd)
out=${1:-$repo/work/inplace-icc}
pkg=$repo/packaging/ubuntu-gts9u-device

dash -n "$pkg/usr/libexec/ubuntu-gts9u-icc-late-release"
for f in install.sh rollback.sh verify.sh weekend/icc-cycle-experiment.sh; do
	dash -n "$here/$f"
done

rm -rf -- "$out"
mkdir -p "$out/files" "$out/weekend"
install -m0755 "$here/install.sh" "$here/rollback.sh" "$here/verify.sh" "$out/"
install -m0644 "$here/README.md" "$out/"
install -m0755 "$here/weekend/icc-cycle-experiment.sh" "$out/weekend/"
install -m0755 "$pkg/usr/libexec/ubuntu-gts9u-icc-late-release" "$out/files/"
install -m0644 "$pkg/usr/lib/systemd/system/ubuntu-gts9u-icc-late-release.service" "$out/files/"
(cd "$out" && find . -type f ! -name SHA256SUMS -printf '%P\n' | LC_ALL=C sort |
	xargs -d '\n' sha256sum > SHA256SUMS)
echo "kit in $out:"
cat "$out/SHA256SUMS"
echo "copy it with: scp -r $out jstockdale@10.42.0.2:icc"
