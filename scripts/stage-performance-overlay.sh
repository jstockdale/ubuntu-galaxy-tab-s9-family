#!/bin/bash
# Stage the early, built-in board OPP overlay in a disposable kernel tree.
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
tree=${1:?usage: stage-performance-overlay.sh KERNEL_WORKTREE}
target=$tree/drivers/soc/qcom
test -f "$target/Makefile"
command -v dtc >/dev/null
command -v python3 >/dev/null
install -m0644 "$repo/kernel/drivers/gts9u-performance.c" "$target/gts9u-performance.c"
for overlay in gts9u-performance gts9-family; do
	dtc -I dts -O dtb -o "$target/$overlay.dtbo" "$repo/kernel/dts/$overlay.dtso"
done
python3 - "$target" <<'PY'
from pathlib import Path
import sys
target = Path(sys.argv[1])
lines = ['/* Generated from kernel/dts/gts9u-performance.dtso and gts9-family.dtso. */']
for name, symbol in (('gts9u-performance', 'gts9u_performance_overlay'),
                     ('gts9-family', 'gts9_family_overlay')):
    data = (target / f'{name}.dtbo').read_bytes()
    lines.append(f'static const unsigned char {symbol}[] __initconst = {{')
    for offset in range(0, len(data), 12):
        lines.append('\t' + ', '.join(f'0x{b:02x}' for b in data[offset:offset+12]) + ',')
    lines.append('};')
lines.append('')
(target / 'gts9u-performance-overlay.h').write_text('\n'.join(lines))
PY
# The port already enables OF_OVERLAY for Gunyah. Do not silently build this
# as a module: that would run after the CPU and GPU have consumed their OPPs.
grep -q '^obj-$(CONFIG_OF_OVERLAY) += gts9u-performance.o$' "$target/Makefile" ||
    printf '\nobj-$(CONFIG_OF_OVERLAY) += gts9u-performance.o\n' >> "$target/Makefile"
