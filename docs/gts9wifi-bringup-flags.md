# Galaxy Tab S9 (SM-X710): the bring-up flags `clk_ignore_unused`, `pd_ignore_unused`, `regulator_ignore_unused`

Status, 2026-11-07: all three are still in `configs/vendor_boot/cmdline.txt`,
which the X710 and the Ultra share (audit item PMR-11).  This page records what
each flag holds on today on the X710 at Stage 2 (fix2-s2 kernel, videocc built
in, `initcall_blacklist=bwmon_driver_init`), the risk of dropping each one, and
the images that test them one at a time.  The morning procedure is in
`tools/inplace/flags/README.md`.  `cmdline.txt` stays unchanged until those
tests pass on the X710 and the same capture has been taken on the Ultra.

## What each flag guards on this kernel (7.2-rc3)

All three only skip a single clean-up pass.  After that pass the kernel
switches clocks, domains and rails off on its own as their users release them,
flag or no flag.

| flag | pass it skips | when on the X710 | what that pass would turn off |
|---|---|---|---|
| `clk_ignore_unused` | `clk_disable_unused()`, `late_initcall_sync` | 13.66 s | a clock registered by then with enable count 0, without `CLK_IGNORE_UNUSED`, whose ops have `.is_enabled` reading "on" and a `.disable`; plus `.unprepare` for clocks with an `.is_prepared` op |
| `pd_ignore_unused` | `genpd_power_off_unused()`, `late_initcall_sync` | 13.67 s | a genpd that is on, is no longer held by `stay_on`, and has no runtime-active device and no powered subdomain |
| `regulator_ignore_unused` | `regulator_late_cleanup()`, 30 s after `late_initcall_sync` | 44.25 s | a regulator that is not always-on, may change status, has use count 0 and reports enabled |

Three properties of 7.2 shrink the candidate lists considerably:

- **clk_summary's "hardware enable" column prints `Y` for any clock without an
  `.enable` op** (RPMh, q6prm, fixed and fixed-factor clocks, dividers, PLL
  post-dividers, cpufreq).  `clk_disable_unused()` cannot touch those, so
  "enabled with count 0" overstates what the pass does.  Branches with
  `clk_branch2_aon_ops` have `.enable` and `.is_enabled` but no `.disable`.
- **genpd keeps a domain that was on at boot on until its provider's
  `sync_state`** (`stay_on`, since 6.13), and `sync_state` then powers unused
  domains off itself, with or without the flag.  On Stage 2 every clock
  controller, rpmhpd and every genpd provider reports `state_synced=1`; only
  gem_noc (`24100000.interconnect`) and mc_virt (`interconnect-1`) do not, and
  they are not genpd providers.
- **An RPMh regulator reports -EINVAL ("unknown") until Linux first votes it**,
  and `regulator_late_cleanup()` treats that as off.  A rail that a consumer
  switched off reads 0.  So the pass can only hit `regulator-boot-on` rails
  without `regulator-always-on` whose consumer has not taken them by 44 s.

## Evidence

- Root snapshots of the Stage 2 boot 972349f7 (fix2-s2, 145 s after boot):
  `/var/log/gts9u-stage0/start-clk_summary.txt`,
  `start-regulator_summary.txt`, `0d-before-genpd.txt` on the tablet.
- Kernel log of the current Stage 2 boot (`/var/log/dmesg`, journal):
  `clk: Not disabling unused clocks` at 13.663 s, `PM: genpd: Not disabling
  unused power domains` at 13.673 s, `regulator: Not disabling unused
  regulators` at 44.251 s.  DRM bound at 10.96 s and did its first modeset at
  12.06 s, PCIe0 link up at 11.18 s, Bluetooth up at 5.4 s, UFS reprobed at
  10.77 s: every one of those holds its clocks before the clock pass runs.
- `/sys/class/regulator/*/state`, `num_users` and every `state_synced` on the
  running Stage 2 boot; the ops of each clock in `drivers/clk/qcom/*-sm8550.c`.

## `clk_ignore_unused`: six clocks

103 clocks read "enabled in hardware, enable count 0" at 145 s.  97 cannot be
touched by the pass:

| class | clocks | why untouched |
|---|---|---|
| q6prm LPASS clocks | 39 `LPASS_CLK_ID_*` | prepare-only ops; registered when the ADSP comes up (~40 s), after the pass anyway |
| RPMh | 17: `clk1`-`clk4`, `clk6`-`clk8` and their `_ao`, `bi_tcxo_ao`, `qdss`, `ipa` | prepare-only ops |
| fixed / fixed-factor | `sleep-clk`, `bi-tcxo-ao-div2-clk`, `dsi0_pll_post_out_div_clk`, `dsi0_pll_by_2_bit_clk` | no gate |
| cpufreq | `qcom_cpufreq0..2` | `.recalc_rate` only |
| QMP DP PHY | `88e8000.phy::vco_div_clk`, `::link_clk` | no gate |
| dividers | 11 `*_div_clk_src` (dispcc 5, videocc 4, gpucc 2) | `clk_regmap_div(_ro)_ops` |
| camcc PLL post-dividers | 13 `cam_cc_pll*_out_even/odd` | postdiv ops |
| RCGs and muxes | `disp_cc_xo_clk_src`, `disp_cc_sleep_clk_src`, `gcc_pcie_1_phy_aux_clk_src`, `gcc_pcie_1_pipe_clk_src` | no `.disable` |
| always-on branches | `gcc_ddrss_gpu_axi_clk`, `gpu_cc_cx_gmu_clk`, `gpu_cc_hub_cx_int_clk`, `gpu_cc_hub_aon_clk` | `clk_branch2_aon_ops` has no `.disable` |

The pass switches off these six, none of which any device-tree node consumes:

| clock | provider | risk area | assessment |
|---|---|---|---|
| `disp_cc_mdss_rscc_ahb_clk` | dispcc | panel/DSI | display RSCC interface clock; the mainline DPU votes through the apps RSC and never uses the display RSCC.  Low |
| `disp_cc_mdss_rscc_vsync_clk` | dispcc | panel/DSI | same block.  Low |
| `disp_cc_mdss_ahb1_clk` | dispcc | panel/DSI | the second MDSS AHB clock; no DPU, DSI or DP node references it.  Low |
| `disp_cc_sleep_clk` | dispcc | panel/DSI | Low |
| `gcc_boot_rom_ahb_clk` | gcc, voted (0x52000) | TrustZone | only the APPS vote is dropped; other masters vote on their own.  Low |
| `gcc_pcie_1_phy_rchng_clk` | gcc, voted | PCIe | PCIe1 is unused on the X710 (`pcie_1_gdsc` off); the WCN6855 sits on PCIe0.  Low |

No clock in the other risk areas is affected: UFS (every UFS clock has users,
the symbol muxes have no gate), USB (held), WCN/PCIe0 (held since the 11.2 s
link-up), audio (LPASS clocks registered after the pass; `LPASS_HW_*` in use),
DP (its link clocks read off), GPU (GMU clocks are always-on branches) and the
fingerprint/TrustZone side: with `clk_ignore_unused` set, every QUP serial
engine clock except the two UARTs reads off, so Linux holds none of them and
TrustZone does not depend on an HLOS vote (the same finding as the X910's in
`docs/fingerprint-reader.md`).

Caveat: the snapshot is from 145 s.  A clock that was on at 13.66 s and taken
by a driver later is not in it.  The `flags-noclk` image therefore also
traces every `clk_disable`/`clk_unprepare`, and `flags-check.sh` lists what
PID 1 switched off in the two seconds after "clk: Disabling unused clocks".

## `pd_ignore_unused`: nothing expected

48 domains.  The 18 that are on all have a holder:

| domain | holder |
|---|---|
| `cx` (rpmhpd) | QUP I2C/UARTs (`9a0000.i2c`, both serials active), subdomain `mmcx` |
| `mmcx` (rpmhpd) | dispcc, DPU, DSI (active) |
| `mdss_gdsc` | `ae00000.display-subsystem` (active) |
| `usb30_prim_gdsc`, `usb3_phy_gdsc` | `a600000.usb`, `88e8000.phy` (active) |
| `ufs_phy_gdsc`, `ufs_mem_phy_gdsc` | `1d84000.ufshc` (active); `1d88000.crypto` and `1d80000.phy` have no runtime PM, which genpd counts as always in use |
| `pcie_0_gdsc`, `pcie_0_phy_gdsc` | `1c00000.pcie` (active); `1c06000.phy` without runtime PM |
| `power-domain-cluster`, `power-domain-cpu0..7` | PSCI, run by cpuidle |

The other 30 (videocc 4, camcc 9, gpucc 2, `mdss_int2_gdsc`, PCIe1 2, the
idle RPMh rails) are off already.  Every provider has reached `sync_state`,
which has done the same sweep for its own domains, and the Stage 1 capture
(rpmhpd and gcc unsynced) shows no unheld domain either.  Dropping the flag
should change nothing; the image tests that.  UFS is the area to watch: its
GDSCs cannot be collapsed by genpd while the PHY and ICE devices are attached.

## `regulator_ignore_unused`: nothing expected

| regulator | DT | users (145 s) | consumers | at 44 s |
|---|---|---|---|---|
| `vph_pwr` | always-on, boot-on | 6 | parent of the PM8550VS rails | skipped (always-on) |
| `vreg_l1b_1p8`, `vreg_l10b_1p8`, `vreg_l16b_3p0` | always-on | 1 | sensor VDD, DMIC bias, sensor VDDIO | skipped |
| `speaker_vdd`, `panel_ldo_en` | always-on, boot-on | 9 / 1 | CS35L45 x4, panel I/O LDO | skipped |
| `vreg_l5b_3p104`, `vreg_l15b_1p8` | - | 1 / 2 | eUSB2 repeater; repeater + WCN vddio | held |
| `vreg_l8b_1p8`, `vreg_l9b_2p9` | - | 1 | microSD | held |
| `vreg_l11b_1p2`, `vreg_l12b_1p8`, `vreg_l13b_3p0`, `vreg_l14b_3p2` | - | 1-2 | panel VDD/VDDIO/VCI, touch, digitizer | held while the panel is on |
| `vreg_l17b_2p5`, `vreg_l1g_1p2`, `vreg_l3g_1p2` | - | 1 | UFS VCC, VCCQ, HBA | held / switched by UFS runtime PM |
| `vreg_l1e_0p88`, `vreg_l3e_1p2`, `vreg_l3f_0p88` | - | 1-5 | DSI, eUSB2, combo, UFS and PCIe PHYs | held |
| `vreg_s4e_0p968`, `vreg_s2g_1p012`, `vreg_s4g_1p352`, `vreg_s5g_0p968`, `vreg_s6g_1p904` | - | 1-3 | WCN6855 PMU (power sequencer) | held |
| `pogo_vddo` | GPIO, not boot-on | 1 | pogo controller `7-002a` | driven low at probe, then held |
| `display_panel_avdd` | GPIO, **boot-on only** | 1 | panel AVDD | the one candidate |

`display_panel_avdd` is switched off by the pass only if the panel driver has
not enabled it by 44 s.  On Stage 2 the panel powers on at 12.06 s and again
after the boot-time cycle (~24 s), so it holds the rail at 44 s.  The WCN
rails are held by the power sequencer and released through it.  Dropping the
flag should print nothing; any "`<rail>: disabling`" line in the variant's
dmesg is a finding.  Rails added later (cameras, charger OTG) need this table
redone: an RPMh LDO without `regulator-boot-on` is safe, a fixed GPIO rail
with `regulator-boot-on` and without `regulator-always-on` is the risky kind.

## Test images and order

`tools/inplace/flags/build-variants.sh` rebuilds, from the fix2-s2 images
themselves, a control that reproduces fix2-s2 byte for byte and then four
`vendor_boot.img` variants whose only difference is the cmdline.  `boot.img` is
fix2-s2's in all of them.  `scripts/build-android-v4-bundle.sh` gained
`GTS9U_DROP_CMDLINE` for this: tokens of `cmdline.txt` to leave out of one
image, each required to be a whole word of it (a typo fails the build;
`root=` is refused), so `cmdline.txt` is never edited for an experiment.

| image | dropped | added | expected |
|---|---|---|---|
| `flags-nopd` | `pd_ignore_unused` | - | "genpd: Disabling unused power domains"; same domain states as fix2-s2 |
| `flags-noreg` | `regulator_ignore_unused` | - | no "regulator: Not disabling" line and no "`<rail>: disabling`" line |
| `flags-noclk` | `clk_ignore_unused` | `trace_event=clk:clk_disable,clk:clk_unprepare trace_buf_size=8M` | exactly the six clocks above; panel unaffected |
| `flags-none` | all three | the same trace | only after the three above pass |

Order: `pd`, then `regulator`, then `clk` (lowest expected change first), then
optionally all three together.

## The Ultra

`cmdline.txt` is shared.  Before it changes, take the same capture on the
Ultra: `flags-check.sh control` runs there unchanged (its expected clock list
is X710-specific, so read the "enabled with count 0" list rather than the
verdict).  If the two tablets end up needing different flags, the bundle
script can carry a per-device default for `GTS9U_DROP_CMDLINE` from
`scripts/lib/device.sh` instead of editing the shared file.
