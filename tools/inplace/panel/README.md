# Panel cold-boot experiments (DISP-5), SM-X710 on fix2-s2

Four vendor_boot variants of the running fix2-s2 images, plus the scripts to
flash, run and read them. Only the kernel command line differs from fix2-s2
(same kernel, DTB, ramdisk; `make-images.sh` proves the repack is
byte-identical before it adds anything). Every switch used here is already
in the fix2-s2 kernel; nothing on the tablet changes except the vendor_boot
partition, and `rollback.sh` puts that back.

## The fault

Every boot, on every kernel since bring-up: the first power-on of the
ANA38407 reads `panel id: 00 00 00` after four `msm_dsi_host_cmd_rx:Invalid
response cmd` (the read requests go out and complete, but mainline clears
the read-back registers before every read and nothing lands in them; no
DSI error or DMA timeout), the glass stays dark, and only the boot-time
`pm_test=platform` cycle (`ubuntu-gts9u-panel-coldboot-recover`, ~24 s into
the boot, picture at ~31.5 s) brings `80 00 04`. Since the TCON_RDY line is
wired up, the first power-on also logs `TCON_RDY high before reset, high
114-115 ms after it` (Stage 1, Stage 2 and all five boot-loop boots): the
DDIC is up when Linux takes over, does reset, and comes out of reset. A dead
DDIC would keep TCON_RDY low, so the host side is the main suspect.

## First power-on: mainline vs Samsung vs what the bootloader leaves

| | bootloader (ABL) hand-off | Samsung kernel | mainline, first power-on | mainline, every later one |
| --- | --- | --- | --- | --- |
| DDIC | running the splash, TCON_RDY high, all rails up | inherits it (continuous splash); no reset, no init | reset pulse (high 5, low 10, high 10 ms) with every rail still up | rails `vdd` (L11B) and `avdd` were off since the blank, then the same pulse |
| panel rails | vddio L12B, vdd L11B, vci L13B, avdd, `panel_ldo_en` (GPIO 187) on | power cycle toggles **only `panel_ldo_en`**, as far as the DT shows (the AMSA10FA01 node's only supply entry; 11 ms on, 15 ms off) | `panel_ldo_en` is always-on in our DTS and never toggles | L11B and avdd cycle; L12B/L13B stay (touch, digitizer hold them) |
| order | | VDD on, LP-11, reset, TCON_RDY, **then** HS clocks (`tcon_prepare()` runs between the LP and HS link clocks) | PHY, PLL, byte/pixel clocks, controller first, then the panel's reset and TCON wait (`prepare_prev_first`) | same |
| DSI PHY | programmed for the splash, PLL running (down-spread SSC) | PHY SW reset `DSI_PHY_SW_RESET = BIT(24)\|BIT(0)` at every prepare, full re-programming | `DSI_PHY_RESET` bit 0 only; never writes `LANE_CTRL1/2/3` (ULPS / force-HS / force-stop) unless continuous clock, never clears `SSC_CONTROL` | same writes, but over registers the GDSC collapse reset |
| DSI controller | command mode, splash | full init, `rx-eot-ignore`, esc clock 12.8 MHz | soft reset, re-programs the main registers; never writes `LANE_CTRL` in non-continuous mode, `LP/HS_TIMER_CTRL` (BTA timeouts), `DYNAMIC_REFRESH_CTRL`, `LOGICAL_LANE_SWAP`, `SPLIT_LINK`; esc 19.2 MHz | same |
| MDSS GDSC | on | its first power-on follows a full power-off (PHY off, PLL off, MDSS collapse) | on since the bootloader; the MDSS core BCR reset at probe is all | collapsed at every blank since dp-pm (DP no longer holds the MDSS; dispcc syncs) |
| DISP_CC_MISC_CMD (af0f000) | unknown | bit 0 masks the reset into DSI PHY0 around idle collapse; bits 5/7/9 DSI0 clock gating | never written | never written |
| display boost MAX77816 (i2c hub, 0x18) | configured | `03=70, 02=8E` before every reset | not described, never written | never written |

What a working power-on has that the first one lacks is, on the host side, a
GDSC collapse of the MDSS (which should reset the DSI controller and, unless
DISP_CC_MISC_CMD bit 0 masks it, the PHY) and, on the panel side, a DDIC that came up from its
supplies instead of from a reset pulse. A continuous HS clock is not the
answer: `continuous_clock=1` blacked the glass but still read `80 00 04`.
The experiments below separate the two sides; the register dumps name what
survives.

## Experiments

| variant | added to the command line | tests | PASS looks like |
| --- | --- | --- | --- |
| `panel-ref` | `msm.dsi_regdump=1` | the normal boot (cycle runs) with DSI controller + PHY register dumps at probe and before every power-on | report: enable 1 `00 00 00`, enable 2 (in the cycle's resume) `80 00 04`, and the list of registers that differ, with bootloader leftovers marked |
| `panel-e0` | `msm.dsi_regdump=1`, cycle and icc late release masked | **E0** what the bootloader leaves and what the bad first enable programs; **E3** (`e3.sh`) one runtime blank/unblank: MDSS suspended, GDSC off, panel rails cycled, nothing frozen | `e3.sh`: `E3 PASS`, the text console appears on the glass |
| `panel-e1` | + `msm.mdss_handoff_gdsc_cycle=1`, masked | **E1** MDSS GDSC power-cycled once at probe (after the core reset, before any child probes): host reset alone, no panel power cycle | login screen with no blank/wake at all; report `E1 (GDSC cycle at probe): PASS` |
| `panel-e2` | + `msm.boot_display_cycle=1`, masked | **E2** right after the first modeset, the display alone goes through the platform cycle's steps (CRTC off, MDSS children and MDSS force-suspended, GDSC off, back on, re-commit) | report `E2 (display-only boot cycle): PASS`; login screen without the cycle |

"Masked" = `systemd.mask=ubuntu-gts9u-panel-coldboot-recover.service
systemd.mask=ubuntu-gts9u-icc-late-release.service`: the boot-time cycle
does not run, for that boot only (systemd 255 drops the mask with the boot;
the next vendor_boot without it is a normal boot again). The icc mask keeps
the interconnect floor at INT_MAX whether or not the overnight icc kit is
installed (its helper would keep it too, but the experiment should not
depend on that).

Cost and risk, lowest first: `panel-ref` (logging only), `panel-e0` (panel
dark until E3; the scripts work over USB SSH), `panel-e2` (forces the DP
controller through suspend as system suspend does), `panel-e1` (collapses
the GDSC under the bootloader's still-running clocks; a GDSC that does not
come back hangs the boot: recovery below).

## Before you start (host, no sudo)

    S=/tmp/claude-1000/-home-jstockdale-projects-ubuntu-galaxy-tab-s9-ultra/0b18c5c0-eba0-4cb6-977b-893306d69db3/scratchpad/overnight/panel
    # the kit
    git -C ~/projects/ubuntu-galaxy-tab-s9-ultra archive overnight/panel tools/inplace/panel |
      ssh jstockdale@10.42.0.2 'rm -rf ~/panel && mkdir ~/panel && tar -x --strip-components=3 -C ~/panel'
    # the four images (96 MiB each, mostly zeros)
    scp -C -r "$S"/images/panel-ref "$S"/images/panel-e0 "$S"/images/panel-e1 "$S"/images/panel-e2 \
      jstockdale@10.42.0.2:display-exp/
    ssh jstockdale@10.42.0.2 'cd ~/panel && sha256sum -c SHA256SUMS && cd ~/display-exp && sha256sum -c ~/panel/IMAGES.sha256'
    # a host copy of the rollback image, for TWRP (the scratchpad lives in /tmp)
    scp jstockdale@10.42.0.2:display-exp/fix2-s2/vendor_boot.img ~/ubuntu-gts9u/fix2-s2-vendor_boot.img
    sha256sum ~/ubuntu-gts9u/fix2-s2-vendor_boot.img   # 0742866cbfb01da7...

If the scratchpad is gone, rebuild the images (reproducible; the script
checks the result against `IMAGES.sha256`):

    tools/inplace/panel/make-images.sh ~/ubuntu-gts9u/fix2-s2-vendor_boot.img /tmp/panel-images

## Procedure (on the tablet; `sudo` lines need your password)

Keep the USB cable in: the scripts and SSH run over 10.42.0.2. In the masked
boots the gadget no longer waits for the cycle, so it comes up earlier.
Do not plug in a DisplayPort monitor during the masked boots (see Couplings).

1. **panel-ref** (normal-looking boot):

        sudo sh ~/panel/apply.sh panel-ref && sudo reboot
        # after the login screen appears (~35 s):
        sudo sh ~/panel/collect.sh ref

   PASS: the printed report shows `first answer: 00 00 00; answers in order:
   00 00 00, 80 00 04`, `recovered after a system suspend cycle`, `enable 1
   ... answer 00 00 00`, `enable 2 ... answer 80 00 04`, and a register
   comparison (`-- leftovers ...: N`). Any N is a result; the leftover lines
   are the answer to DISP-5 if N > 0.

2. **panel-e0 + E3** (panel dark after the Samsung logo until E3):

        sudo sh ~/panel/apply.sh panel-e0 && sudo reboot
        # wait ~40 s, the glass stays dark; then over SSH:
        cat /sys/class/backlight/ae94000.dsi.0/panel_id          # expect 00 00 00 (no sudo)
        sudo sh ~/panel/collect.sh e0
        sudo sh ~/panel/e3.sh

   `e3.sh` prints one verdict line:
   * `E3 PASS` - the runtime blank/unblank recovered the DDIC; the text
     console of VT 3 is on the glass. Back to the login screen with the
     `sudo chvt 1` it prints.
   * `E3 FAIL` - GDSC off and the panel re-powered, still `00 00 00`. Then
     run the cycle by hand in the same boot, for the comparison dumps:
     `sudo /usr/libexec/ubuntu-gts9u-panel-coldboot-recover; sudo sh ~/panel/collect.sh e0-platform`
     (expect `80 00 04`).
   * `E3 RECOVERED WITHOUT A GDSC COLLAPSE` - panel side; note it.
   * `E3 INCONCLUSIVE` - the console could not blank the display (the login
     screen kept the display). Fallback: `sudo chvt 1`, do not touch the
     tablet for 6 minutes (the login screen blanks on idle), check
     `cat /sys/bus/platform/devices/ae00000.display-subsystem/power/runtime_status`
     reads `suspended`, touch the screen, then
     `cat /sys/class/backlight/ae94000.dsi.0/panel_id; sudo sh ~/panel/collect.sh e3-idle`.

3. **panel-e1**:

        sudo sh ~/panel/apply.sh panel-e1 && sudo reboot
        # PASS: the login screen appears ~20 s after power-on, with no blank/wake
        sudo sh ~/panel/collect.sh e1

   PASS: `E1 (GDSC cycle at probe): PASS - bootloader hand-off: MDSS GDSC
   power-cycled` and no `display errors`. Then log in and do three
   power-key blank/wake rounds; each must light within ~1 s, and
   `journalctl -k -b | grep 'panel id'` (no sudo) shows `80 00 04` for each.
   If the glass stays dark instead (FAIL), the boot is still a masked one:
   `sudo sh ~/panel/e3.sh` gives E3 on it as well. No SSH after 3 minutes,
   or a reboot loop: recovery below.

4. **panel-e2** (skip if E1 passed and time is short):

        sudo sh ~/panel/apply.sh panel-e2 && sudo reboot
        sudo sh ~/panel/collect.sh e2

   PASS: `E2 (display-only boot cycle): PASS - boot display cycle: MDSS
   GDSC power-cycled[ (noirq-style)], N ms`, login screen visible without
   the cycle. `stayed on` in that line means the GDSC refused to collapse.
   Dark glass: `sudo sh ~/panel/e3.sh` as in step 3.

5. **Back to fix2-s2**:

        sudo sh ~/panel/rollback.sh && sudo reboot
        cat /sys/class/backlight/ae94000.dsi.0/panel_id     # 80 00 04 after the usual cycle

Everything lands in `/var/log/gts9u-panel-exp/<boot>/<NN>-<label>/`
(`report.txt` first, then the raw `dmesg.txt`, `state.txt`, `genpd*.txt`,
`dispcc.txt`, `regulators.txt`, `clk.txt`, `gpio.txt`), readable by group
adm, so the reports can be read back without sudo:
`cat /var/log/gts9u-panel-exp/*/*/report.txt`. The apply/rollback/e3 log is
`/var/log/gts9u-panel-exp.log`.

## Reading the results

* **E1 PASS** - the host reset is the whole fix. Make
  `mdss_handoff_gdsc_cycle` the default for `samsung,gts9wifi` (a
  compatible check in `msm_mdss.c`; the Ultra has the same defect, try
  `panel-e1`'s command line there before widening it). The coldboot unit
  then only carries Wi-Fi and the DP HPD replay (Couplings).
* **E1 FAIL, E2 PASS** - default E2 the same way (one extra modeset at boot,
  before the display manager; the line reports its duration).
* **E3 PASS, E1 FAIL** - the GDSC at probe is not enough on its own: either
  the panel's power cycle is the missing half, or the collapse has to come
  after the first enable. Next kernel: `panel_samsung_ana38407.cold_start_ms=200`
  alone and together with E1's switch (both are in this branch's panel
  driver; see below). E2 covers both meanwhile.
* **Leftovers listed by the reference boot** - the clean fix is in msm, not
  in the panel: write those registers at enable (e.g. clear `LANE_CTRL`'s
  ULPS/force bits in `dsi_ctrl_enable()`, `LANE_CTRL1-3` in
  `dsi_7nm_phy_enable()`, or a vendor-style `PHY_SW_RESET = BIT(24)|BIT(0)`)
  behind a switch, then the cycle and the GDSC tricks can all go.
* **E3 FAIL** - the platform cycle does more than the display's runtime
  suspend: it suspends every other device too (the touchscreen and the
  digitizer release L12B/L13B, the DDIC's last rails), and its noirq phase
  can now let MMCX drop, since rpmhpd syncs at boot. Compare `e0` /
  `e0-platform` (`regulators.txt`, dumps), then try `pm_test=devices`
  (does not recover, measured on the Ultra under pmOS) against `platform`
  by hand on a panel-e0 boot.

## Couplings during the masked boots

* **Wi-Fi**: ath11k's first probe fails before the root is mounted (no
  firmware in the initramfs); `gts9u-pci-rescan` brought it up at ~21.5 s
  before the cycle on Stage 1, so it may well come up anyway. `state.txt`
  records `wifi:`; that is a data point for the wifi workstream.
* **DisplayPort**: the X710 DT sets `qcom,defer-hpd-until-first-resume`, so
  without the cycle DP HPD is deferred until a real resume, which suspend
  policy "off" never allows. No DP output in these boots.
* **Interconnect**: masked late release, floor stays at INT_MAX (Stage 2).
* **USB gadget, ADSP, sensors, pogo firmware** are ordered after the cycle
  unit; with it masked they simply start earlier.

## Recovery if a variant does not boot

Force a restart and boot TWRP as for the Stage 3 recovery, then (USB to
the host):

    adb push ~/ubuntu-gts9u/fix2-s2-vendor_boot.img /tmp/vb.img
    adb shell 'dd if=/tmp/vb.img of=/dev/block/by-name/vendor_boot bs=4M && sync && sha256sum /dev/block/by-name/vendor_boot'
    adb reboot

(the sum must be `0742866cbfb01da7...`, the image fills the 96 MiB
partition). boot.img is never touched by this kit.

## Panel driver switches for the next kernel build (not in fix2-s2)

`kernel/drivers/panel-samsung-ana38407.c` on this branch adds two module
parameters, both off by default, set as `panel_samsung_ana38407.<name>=`:

* `cold_start_ms=N` - before the first power-on, take the DDIC the
  bootloader left running down the way unprepare() does (reset asserted,
  its supplies released) for N ms (at most 2000), so the first power-on
  starts like every later one. Logs `first power-on: DDIC held off N ms
  before it (TCON_RDY high, then low|high)` - `then high` means a rail this
  driver does not switch kept the DDIC up (`panel_ldo_en` on GPIO 187, or
  vddio/vci, which the touchscreen and the digitizer hold).
* `trace_power_on=1` - one line per power-on with the supplies, the TCON_RDY
  timing and the id, so later power-ons can be compared with the first
  one's 114-115 ms.

## Files

| file | where it runs | what |
| --- | --- | --- |
| `make-images.sh` | host | builds the variants from fix2-s2's vendor_boot, round-trip checked, reproducible |
| `IMAGES.sha256` | both | expected hashes of the four variants |
| `apply.sh` | tablet, sudo | checks board, kit, image and that boot holds fix2-s2, backs up vendor_boot once, flashes, reads back |
| `rollback.sh` | tablet, sudo | restores the backed-up vendor_boot (or fix2-s2's), reads back |
| `collect.sh` | tablet, sudo | evidence of one moment of a boot, plus `report.txt` |
| `e3.sh` | tablet, sudo | E3 on a panel-e0 boot |
| `report.py` | tablet or host | timeline, verdicts, register comparison of a collect directory or a dmesg file |
| `SHA256SUMS` | | checksums of the kit files |
