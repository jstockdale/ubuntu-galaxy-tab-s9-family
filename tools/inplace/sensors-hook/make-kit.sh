#!/bin/sh
# Host side: pack the sensors hook kit from this checkout, for a tablet that
# cannot take the device package that carries the hook.
#   sh tools/inplace/sensors-hook/make-kit.sh [OUT]
# OUT (default ~/ubuntu-gts9u/backups/sensors-hook-kit) is made afresh: the
# kit's scripts, the three device package files under files/ at their paths
# on the tablet and with their modes, REVISION, SOURCE and SHA256SUMS, which
# install.sh checks before it changes anything.  Copy it to the tablet as it
# is (README.md, "Install").  Raise revision below for every kit that differs
# from one already on a tablet.
set -eu
export LC_ALL=C

revision=1
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../.." && pwd)
pkg=$repo/packaging/ubuntu-gts9u-device
out=${1:-$HOME/ubuntu-gts9u/backups/sensors-hook-kit}
files='usr/lib/systemd/system-sleep/ubuntu-gts9u-sensors 0755
usr/libexec/ubuntu-gts9u-sensors-resume 0755
usr/lib/systemd/system/ubuntu-gts9u-sensors-resume.service 0644'

case $out in
''|/) echo "make-kit.sh: refusing OUT '$out'" >&2; exit 1 ;;
esac
rm -rf -- "$out"
mkdir -p "$out"
for s in install.sh rollback.sh verify.sh cycle.sh README.md; do
	install -m 0644 "$here/$s" "$out/$s"
done
printf '%s\n' "$files" | while read -r rel mode; do
	case $rel in
	*.service) ;;
	*) sh -n "$pkg/$rel" || { echo "make-kit.sh: $rel does not parse" >&2; exit 1; } ;;
	esac
	install -D -m "$mode" "$pkg/$rel" "$out/files/$rel"
done
echo "$revision" > "$out/REVISION"
# Where the files come from: the commit, and whether the tree had changes.
src=$(git -C "$repo" rev-parse --short=12 HEAD 2>/dev/null) || src=unknown
[ -z "$(git -C "$repo" status --porcelain -- packaging/ubuntu-gts9u-device tools/inplace/sensors-hook 2>/dev/null)" ] ||
	src="$src with uncommitted changes"
echo "$src" > "$out/SOURCE"
(cd "$out" && find . -type f ! -name SHA256SUMS | sed 's#^\./##' | sort | xargs -d '\n' sha256sum > SHA256SUMS)
echo "sensors hook kit revision $revision from $src in $out:"
(cd "$out" && sed 's/^/  /' SHA256SUMS)
