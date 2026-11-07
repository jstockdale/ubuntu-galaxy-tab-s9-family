# GPU write-integrity checks (Adreno 740, freedreno)

Standalone tests that render into a buffer and verify every byte from the CPU.
No display, no root, no numpy: python3 plus the system libEGL, libGLESv2 and
libgbm.  Written for the SM-X710 investigation of 2026-11-06
(see docs/gts9wifi-display.md, "GPU write corruption").

Copy the directory to the tablet and run as the desktop user:

    scp -r scripts/diagnostics/gpu-write-check jstockdale@<tablet>:gputest
    ssh jstockdale@<tablet> 'cd gputest && FD_MESA_DEBUG=noubwc python3 late-write-check.py 24'

A healthy unit reports 0 frames with lost pixels (the SM-X910 Ultra does).

| Script | What it measures |
|---|---|
| `late-write-check.py [frames]` | one full-frame quad into a LINEAR dma-buf; CPU reads memory right after the fence, again later, then via glReadPixels. Separates lost writes from late writes and read-side errors. |
| `loss-pattern.py [--noclear] [--cols=A|B|C] [--alpha=X] [frames]` | same, with per-frame lattice statistics, stale-byte histogram, GPU suspend counter and pack voltage per frame. |
| `overdraw-check.py [--linear] [--delay=ms] [frames] [quads] [W] [H]` | N overlapping quads, GL readback; fastest smoke test, includes read-side noise. |
| `path-check.py <clear|uniform|const|texture|blit|blend>` | which render path produces the damage. |
| `fmt-check.py <rgba8|r8|rgb565|rgb10a2|rgba16f>` | which bits of each format are hit. |
| `compute-direct.py` | compute-shader imageStore straight into the buffer (no render backend). |
| `buffer-check.py [N] [F]` | N separate buffers, to rule out one bad memory region. |
| `freq-sweep.sh [frames]` | root: pins every GPU frequency, then toggles 220/680 MHz, running the check at each. |
| `regdump.py regdump-list.txt out.txt` | reads 289 unprotected GPU registers through CP_REG_TO_MEM using the msm ioctls (poison values 0xdeafbead/0xdeadbeef mean the block was gated at read time; dump under load and compare only stable values). |

Notes: image stores into an imported dma-buf land in RGBA byte order while
draws land in BGRA; the msm wait-fence timeout is an absolute monotonic time;
Mesa 25.x needs the explicit LINEAR modifier on the dma-buf import (handled).
