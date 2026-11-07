#!/bin/bash
# Build only the SPSS IRQ bridge against an already built experimental kernel.
# Does not rebuild, flash, or replace a boot image.
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
. "$repo/scripts/lib/device.sh"
base=${UBUNTU_WORKDIR:-/root/ubuntu-gts9u}
kernel_tree=${KERNEL_WORKTREE:-$base/build/linux-src-$DEVICE}
build_dir=${KERNEL_BUILD_DIR:-$base/build/linux-$DEVICE}
# Not the kernel stage's tree: this helper runs from the rootfs stage, usually
# as root, and must not leave files the next user-run kernel stage cannot clean.
module_dir=${SPSS_IRQ_BUILD_DIR:-$(dirname "$build_dir")/spss-irq-module-$DEVICE-helper}
out_dir=${SPSS_IRQ_OUT_DIR:-$base/out/spss-irq-module}
test -f "$build_dir/Module.symvers"
test -f "$build_dir/certs/signing_key.pem"
test -f "$build_dir/certs/signing_key.x509"
if [ -x /usr/lib/llvm-22/bin/clang ]; then
  export PATH=/usr/lib/llvm-22/bin:$PATH
fi
mkdir -p "$module_dir" "$out_dir"
install -m 0644 "$repo/kernel/drivers/qcom_spss_irq.c" "$module_dir/"
install -m 0644 "$repo/kernel/modules/spss-irq/Makefile" "$module_dir/"
make -C "$kernel_tree" O="$build_dir" ARCH=arm64 LLVM=1 \
  M="$module_dir" modules
install -m 0644 "$module_dir/qcom_spss_irq.ko" "$out_dir/"
"$build_dir/scripts/sign-file" sha256 "$build_dir/certs/signing_key.pem" \
  "$build_dir/certs/signing_key.x509" "$out_dir/qcom_spss_irq.ko"
modinfo -F vermagic "$out_dir/qcom_spss_irq.ko"
modinfo -F signer "$out_dir/qcom_spss_irq.ko"
sha256sum "$out_dir/qcom_spss_irq.ko"
