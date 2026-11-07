# Galaxy Tab S9 (SM-X710, gts9wifi): display bring-up notes

The 11" glass is the AMSA10FA01 behind the same ANA38407 DDIC as the Ultra,
2560x1600, DSI command mode with DSC (two 1280x100 slices, 8 bpc / 8 bpp).
`panel-samsung-ana38407.c` serves both tablets through a per-glass
descriptor; the X710 power-on sequence is Samsung's revision-D
`POWER_ON_PRE_SETTING` transcribed from the X710 kernel sources.

## What is known to work (builds 8, 9, 11)

* Panel VDD (L11B) at 1.2 V, the stock X710 level.  At 1.104 V the glass
  showed white single-pixel noise.
* 120 Hz mode with the stock porches 34/64/34: with msm's
  `dsi_adjust_pclk_for_compression()` (hdisplay -> 854 columns, blanking
  kept, lane rate = 6 x pixel clock) that is 1.234 Gbps per lane.
* Non-continuous DSI clock (`MIPI_DSI_CLOCK_NON_CONTINUOUS`).
* The panel still needs the cold-boot recovery cycle
  (`ubuntu-gts9u-panel-coldboot-recover`, a `pm_test=platform` suspend) on
  this board too: the DDIC answers `00 00 00` on the first probe and
  `80 00 04` after the cycle, about 25 to 90 s into boot depending on what
  udev is still settling.  GDM is ordered after it.

## What stayed black (build 10)

Build 10 shipped three changes together: the stock 1.524 Gbps lane rate
(back porch 266), a continuous DSI clock, and the DDR bandwidth monitor
(`CONFIG_QCOM_ICC_BWMON`).  The tablet booted completely, the DDIC reported
alive after the recovery cycle, the DPU ran, GDM and gnome-shell started,
backlight was on, and the OLED showed nothing.  Which of the three is
responsible is not yet known, so build 11 returns to the configuration
above and turns each into a switch:

| experiment | how |
| --- | --- |
| bandwidth monitor | `modprobe icc_bwmon` on the running system (module, blacklisted by `/etc/modprobe.d/gts9u-bwmon.conf`) |
| stock lane rate | `panel_samsung_ana38407.hback_porch=266` on the kernel command line |
| continuous clock | `panel_samsung_ana38407.continuous_clock=1` on the kernel command line |

The command line lives in vendor_boot.img only, so a kernel-parameter
experiment is `GTS9U_EXTRA_CMDLINE='...' scripts/build-android-v4-bundle.sh`
and a `dd` of that one image, not a kernel rebuild.  The driver logs the
effective settings at probe (`DSI clock ... between frames`, `mode 0: back
porch ...`).

## Still open

* Intermittent faint speckle on frames written after an idle gap and the
  occasional mirrored or stretched copy of a buffer region (GPU/UBWC path is
  the current suspect; `FD_MESA_DEBUG=noubwc` test pending).
* Square blocking shared with the Ultra (GMU bandwidth-vote timeouts).
* 60 Hz / VRR mode not declared.
* A short power-button press on this board must not suspend: resume from
  deep suspend does not work yet, and the result is a black panel with USB
  gone until a hard reset.  The power-key helper locks the session instead
  on `samsung,gts9wifi`.

## GPU write corruption (2026-11-06): what is known

Single bright or dark pixels on a fixed lattice after large redraws (dark/light
toggle, WebGL) turned out not to be a display problem at all. A standalone test
(`scripts/diagnostics/gpu-write-check/`) shows the GPU losing data on its way
to memory, with no display involved:

- One full-frame quad rendered into a linear buffer: about 1 pixel in 1500 is
  wrong when the CPU reads the memory straight after the GPU's completion
  fence, and it stays wrong afterwards. The writes are lost, not late.
- Positions: screen columns 20 and 28 (mod 48), rows 16 apart. The 48-px
  period matches the render backend's six 16-px column stripes (stripes 1 and
  4). Compute-shader stores, which bypass the render backend, lose single bytes
  at every 16th column instead; plain clears through the 2D engine are clean.
- Values: in 3D draws the green and alpha channels come back multiplied by
  exactly 0.25 (one exponent bit cleared) in UNORM8, RGB565, RGB10A2 and
  half-float alike; R8 targets are clean. Compute stores leave the byte at 0.
- Bursty: some frames clean, some lose thousands of pixels; a second GPU
  process multiplies it; it does not need another process to happen.

Everything below was changed on the X710 without touching rate, position or
signature (reproducer verdict within a minute of each boot):

| Area | Varied |
|---|---|
| Mesa | 25.2.8 and 26.2.6; every freedreno flag incl. flush, sysmem, gmem, nobin, nolrz, noblit, notile, inorder, noubwc, nofp16, hiprio; FD_MESA_GMEM 512K..3M |
| Kernel switches added (all inert by default) | `msm.cached_coherent=0`, `msm.disable_acd=1`, `msm.enable_preemption=0`, `msm.gpu_hwcg=0`, `msm.gpu_llc=0`, `msm.a7xx_ccu_inval=0|2`, `msm.gpu_scm_init=9` (TZ returns -5), `msm.gmu_hub_hz=300000000|400000000`, `gunyah.qcom_overlay_all=1` |
| Device tree | every GPU corner +1, every GPU/GMU corner at NOM, GMU CX corner SVS_L1/NOM, Linux MX vote on the GPU node (confirmed `mx on` in pm_genpd) |
| Clocks and power | all eight GPU frequencies pinned, clock toggling, hub clock 200/300/400 MHz, runtime suspend forced and avoided, 5 V PC port vs 9 V charger, pack 52..54 %, GPU 31..40 °C |
| Memory | eight separate buffers, four formats, tiled and linear targets |

`fw_devlink.sync_state=timeout` (to release the pinned memory-bus votes)
boot-loops; do not use it.

Comparison with the SM-X910 Ultra on the same kernel build, firmware, GPU
parameters, clock tree and Mesa: zero errors in every form of the test, also
under concurrency. Both SoCs are SM8550 revision 2.0. Samsung's stock trees
set up the GPU identically on both. The X710 was clean under Android. Other
X710 mainline ports (nacht20-de, ygecesefa, Azkali) use the same GPU setup and
report no artifacts. Register dumps of both GPUs under load differ only in a
performance-counter select.

Open question: this unit versus the model. A second rev 5 SM-X710 decides it;
copy the kit and run `late-write-check.py 24`. Specks stick on screen because
the panel runs in command mode and keeps its frame memory until the next
commit; the compositor only re-renders damaged regions, so a lost pixel stays
until something redraws it.
