#!/bin/bash
# Test bench for the installer's firmware-revision gate, without a device.
#
# The real configs/twrp/ubuntu-update-binary runs inside an unprivileged user
# namespace where /dev is a tmpfs holding plain files for the five partitions,
# with getprop, unzip and blockdev faked on PATH the same way
# test-repartition-zip.sh fakes TWRP. Only the identity/firmware gate region
# is asserted; the run is allowed to die later wherever the fakes end.
#
# Needs no root and touches no device.
set -u

repo=$(cd "$(dirname "$0")/.." && pwd)
installer=$repo/configs/twrp/ubuntu-update-binary
[ -f "$installer" ] || { echo "missing installer: $installer"; exit 1; }

shell=mksh
command -v mksh >/dev/null || shell=bash

work=$(mktemp -d /tmp/gts9u-fwgate.XXXXXX)
trap 'rm -rf "$work"' EXIT
failures=0
ok()  { echo "  PASS  $1"; }
bad() { echo "  FAIL  $1"; failures=$((failures + 1)); }

mkdir -p "$work/bin"
cat > "$work/bin/getprop" <<'G'
#!/bin/sh
case "$1" in
  ro.product.device) echo gts9uwifi ;;
  ro.boot.em.model)  echo SM-X910 ;;
  ro.bootloader)     echo "${FAKE_BOOTLOADER:-}" ;;
  *) echo "" ;;
esac
G
cat > "$work/bin/unzip" <<'U'
#!/bin/sh
# AOSP-ziptool-shaped fake: -l lists FAKE_MEMBERS, -p prints known members.
if [ "$1" = -l ]; then
    for member in $FAKE_MEMBERS; do
        printf '     1024  1980-01-01 00:00   %s\n' "$member"
    done
    exit 0
fi
[ "$1" = -p ] || exit 1
case " $FAKE_MEMBERS " in *" $3 "*) ;; *) exit 1 ;; esac
case "$3" in
  BUNDLE-LABEL)      echo "firmware gate test bundle" ;;
  FIRMWARE-IDENTITY) echo "${FAKE_FW_IDENTITY:-}" ;;
  SHA256SUMS)
    for img in boot.img init_boot.img vendor_boot.img dtbo.img vbmeta.img; do
      printf '%s  %s\n' \
        1111111111111111111111111111111111111111111111111111111111111111 "$img"
    done ;;
  *) exit 1 ;;
esac
U
cat > "$work/bin/blockdev" <<'B'
#!/bin/sh
case "${2##*/}" in
  boot|vendor_boot) echo 100663296 ;;
  init_boot)        echo 8388608 ;;
  dtbo)             echo 16777216 ;;
  vbmeta)           echo 131072 ;;
  *) echo 0 ;;
esac
B
chmod +x "$work/bin"/*
cp "$installer" "$work/update-binary"

cat > "$work/inner.sh" <<INNER
set -u
# Unprivileged tmpfs mounts are forced nodev, so a bound character device
# would refuse to open; a plain empty file serves every >/dev/null redirect.
mount -t tmpfs tmpfs /dev
touch /dev/null
mkdir -p /dev/block/by-name
for part in boot init_boot vendor_boot dtbo vbmeta; do
    : > /dev/block/by-name/\$part
done
export PATH="$work/bin:\$PATH"
exec $shell "$work/update-binary" dummy 3 /tmp/test.zip 3>>"\$GTS9U_UI_LOG"
INNER

run_case() { # name bootloader members identity
    : > "$work/$1.ui"
    FAKE_BOOTLOADER=$2 FAKE_MEMBERS=$3 FAKE_FW_IDENTITY=$4 \
        GTS9U_UI_LOG="$work/$1.ui" \
        timeout 20 unshare -r -m bash "$work/inner.sh" \
        > "$work/$1.log" 2>&1
    echo $? > "$work/$1.rc"
}

base_members="boot.img init_boot.img vendor_boot.img dtbo.img vbmeta.img BUNDLE-LABEL SHA256SUMS"

echo "== 1. matching revision passes the gate =="
run_case match X910XXS5CYG1 "$base_members FIRMWARE-IDENTITY" \
    "gts9uwifi bl5 X910XXS5CYG1"
grep -q "ZIP staged from X910XXS5CYG1 (bl5); device bootloader is rev 5" \
    "$work/match.ui" && ok "prints both identities" || bad "identity line missing"
grep -q "carries bl5 firmware" "$work/match.ui" \
    && bad "aborted a matching pair" || ok "no mismatch abort"
grep -q "read back with SHA-256" "$work/match.ui" \
    && ok "an empty image write is refused by the read-back hash" \
    || bad "an empty image write went unnoticed"

echo "== 2. mismatched revision aborts =="
run_case mismatch X910XXU1AWHA "$base_members FIRMWARE-IDENTITY" \
    "gts9uwifi bl5 X910XXS5CYG1"
grep -q "carries bl5 firmware but the tablet's bootloader is rev 1" \
    "$work/mismatch.ui" && ok "refuses with the exact reason" || bad "no refusal"
[ "$(cat "$work/mismatch.rc")" = 1 ] && ok "exits 1" \
    || bad "rc=$(cat "$work/mismatch.rc")"
grep -q "Installation aborted" "$work/mismatch.ui" \
    && ok "uses the abort path" || bad "did not abort cleanly"

echo "== 3. the override member downgrades the abort to a warning =="
run_case override X910XXU1AWHA \
    "$base_members FIRMWARE-IDENTITY ALLOW-FIRMWARE-MISMATCH" \
    "gts9uwifi bl5 X910XXS5CYG1"
grep -q "mismatch overridden by ALLOW-FIRMWARE-MISMATCH" "$work/override.ui" \
    && ok "warns about the override" || bad "override not honoured"
grep -q "carries bl5 firmware but" "$work/override.ui" \
    && bad "aborted despite the override" || ok "continues past the gate"

echo "== 4. a ZIP without the identity is accepted unchanged =="
run_case legacy X910XXS5CYG1 "$base_members" ""
grep -q "Firmware:" "$work/legacy.ui" \
    && bad "invented an identity" || ok "no firmware messages at all"

echo "== 5. an unreadable bootloader string warns and continues =="
run_case unknown twrp-weird-string "$base_members FIRMWARE-IDENTITY" \
    "gts9uwifi bl5 X910XXS5CYG1"
grep -q "cannot read a bootloader revision" "$work/unknown.ui" \
    && ok "warns" || bad "no warning"
grep -q "carries bl5 firmware but" "$work/unknown.ui" \
    && bad "aborted on an unknown revision" || ok "does not abort blind"

echo
[ "$failures" -eq 0 ] && echo "ALL CORRECT ($shell)" || echo "$failures FAILURES"
exit "$failures"
