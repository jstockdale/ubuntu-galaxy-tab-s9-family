# Galaxy Tab S9 (SM-X710, gts9wifi): display bring-up notes

The 11" glass is the AMSA10FA01 behind the same ANA38407 DDIC as the Ultra,
2560x1600, DSI command mode with DSC (two 1280x100 slices, 8 bpc / 8 bpp).
`panel-samsung-ana38407.c` serves both tablets through a per-glass
descriptor; the X710 power-on sequence is Samsung's revision-D
`POWER_ON_PRE_SETTING` transcribed from the X710 kernel sources.

## Link defaults

* Panel VDD (L11B) at 1.2 V, the stock X710 level.  At 1.104 V the glass
  showed white single-pixel noise.
* Every mode runs the stock 1.524 Gbps per lane.  The lane rate follows from
  the mode: msm's `dsi_adjust_pclk_for_compression()` shrinks hdisplay to
  854 columns and keeps the blanking, and the lane rate is 6 x that pixel
  clock.  The 120 Hz mode uses a 266-pixel back porch for this (the stock
  34/64/34 porches would give 1.234 Gbps); the 60 and 30 Hz modes use
  Samsung's own porches, which land on the same rate.
* Samsung's D-PHY timing table `[00 35 0d 0d 1f 27 0d 0d 0c 02 04 00 2a 12]`
  comes from the DSI PHY node (`qcom,dsi-phy-timings`, valid within 1% of
  `qcom,dsi-phy-timings-bitclk-hz = <1524000000>`), and its clk_pre/clk_post
  also go to the controller's `DSI_CLKOUT_TIMING_CTRL`, as the vendor driver
  programs them.  The Ultra's DTS has no table, so its PHY still computes
  its timings.
* Non-continuous DSI clock (`MIPI_DSI_CLOCK_NON_CONTINUOUS`); stock declares
  no forced HS clock lane.
* The expected panel id is pinned to `80 00 04` (revision D, the bootloader's
  `lcd_id=800004`).  `00 00 00` is logged as a DDIC that did not answer;
  another id as glass the power-on sequence was not written for.  The id the
  DDIC answered at the last power-on is in
  `/sys/class/backlight/ae94000.dsi.0/panel_id`.

How these were settled (builds 10 to 13, all as command-line switches on one
kernel): a continuous clock alone blacks the glass out (the build-10 black
panel); 1.524 Gbps with the timings mainline computes gave frame-done
timeouts and more speckle; Samsung's table at 1.524 Gbps ran clean.  The DDR
bandwidth monitor, the third build-10 change, made no difference either way.
It is built in now, because its probe is what lets the interconnect leave
INT_MAX (sync_state); the first boots of that change hold it back with
`initcall_blacklist=bwmon_driver_init`, the one switch that can, since a
modprobe blacklist does not reach a built-in driver.  The switches still exist and
override the defaults:
`panel_samsung_ana38407.hback_porch=` (preferred mode only),
`panel_samsung_ana38407.continuous_clock=`, `msm.dsi_phy_timings=<14 bytes>`.
A release vendor_boot needs none of them, so it is built without
`GTS9U_EXTRA_CMDLINE`.

## Refresh rates

The DDIC sets its own refresh in command mode, so a mode below 120 Hz only
takes effect through Samsung's `VRR_SETTING`, and at 60 Hz the glass also
needs `GLUT_SETTING`'s gamma offsets for the current brightness level.  The
driver offers:

| mode | stock node | VRR_SETTING (0x60, 0xDD, 0xB9) | GLUT |
| --- | --- | --- | --- |
| 120 Hz (preferred) | wqxga120hs | 00, 00, 80 00 00 00 | cleared |
| 60 Hz | wqxga60hs | 10, 00, aa aa aa aa | GLUT_OFFSET_60HS row of the brightness level |
| 30 Hz, only with `panel_samsung_ana38407.mode_30hz=1` | wqxga30hs | 10, 01, aa aa aa aa | as 60 Hz |

A refresh change is a full modeset, so the panel goes through power-off and
the complete power-on sequence with the new branches; below 120 Hz every
brightness change that moves the platform level re-sends the GLUT row ahead
of the 0x51 write, as Samsung's `BRIGHTNESS_SETTING` does.  The tables are
generated from Samsung's `GTS9_ANA38407_AMSA10FA01.dat` by
`scripts/gen-ana38407-glut.py` into
`kernel/drivers/panel-samsung-ana38407-glut.h`.  Not implemented: the
frame-skipping 60/30 Hz "PHS" modes, low-frequency drive and Samsung's
seamless VRR.

## Cold boot: the DDIC does not answer the first enable

The DDIC answers `00 00 00` on its first probe after every cold boot and only
works after `ubuntu-gts9u-panel-coldboot-recover` has run a
`pm_test=platform` cycle; GDM is ordered after it.  Blanking and a
`pm_test=devices` cycle do not recover it (measured on the Ultra).
Display-wise, the platform level adds the noirq phase, in which genpd powers
the MDSS GDSC down; at runtime that never happens because the DP controller
keeps the MDSS active.  The kernel carries switches to test in-kernel
replacements, all inert by default:

| switch | what it does |
| --- | --- |
| `msm.dsi_regdump=1` | logs DSI controller and PHY registers at probe (as the bootloader left them) and at every enable |
| `msm.mdss_handoff_gdsc_cycle=1` | power-cycles the MDSS GDSC at probe, after the core reset |
| `msm.boot_display_cycle=1` | after the first modeset, takes only the display through the cycle's steps, GDSC included |

The panel also waits for the DDIC's TCON_RDY line (TLMM 88) after reset, as
Samsung's Anapass power sequence does, and reports at the first power-on
whether it was high before the reset and how long it took to rise after it.

## Still open

* Intermittent faint speckle on frames written after an idle gap and the
  occasional mirrored or stretched copy of a buffer region (GPU/UBWC path is
  the current suspect; `FD_MESA_DEBUG=noubwc` test pending).
* Square blocking shared with the Ultra (GMU bandwidth-vote timeouts).
* Suspend is not released yet.  On the clean baseline kernel (2026-11-06)
  every pm_test level passed, and one real s2idle (43.6 s) and one real deep
  sleep (44.5 s) both resumed on the power key; the black panel with USB gone
  that experimental builds showed did not reproduce.  Until RTC-driven
  repeat runs (about 20 cycles of each mode) back that up,
  `ubuntu-gts9u-suspend-mode` keeps `samsung,gts9wifi` at mode `off`, which
  makes logind refuse every suspend (power key, cover, idle, menus); the power
  key locks and blanks instead, and the cover locks.  Test s2idle for one boot
  with `echo s2idle > /run/ubuntu-gts9u/suspend-mode` and
  `systemctl restart ubuntu-gts9u-suspend-mode`.

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
