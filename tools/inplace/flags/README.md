# Bring-up flag experiment (SM-X710, Stage 2)

Four `vendor_boot.img` images, each fix2-s2 with a different set of
`clk_ignore_unused`, `pd_ignore_unused` and `regulator_ignore_unused`
removed.  `boot.img` in every set is fix2-s2's kernel (sha256 `dcf88155…`), and
apart from the cmdline every set is byte-identical to fix2-s2.  The analysis
behind the expected results is in `docs/gts9wifi-bringup-flags.md`.  In short:
dropping `pd` or `regulator` should change nothing; dropping `clk` should switch
off six clocks that no device-tree node uses.

| set | dropped | added | vendor_boot sha256 |
|---|---|---|---|
| `flags-nopd` | `pd_ignore_unused` | - | `60f733b44c9a…` |
| `flags-noreg` | `regulator_ignore_unused` | - | `49b6e34b92f3…` |
| `flags-noclk` | `clk_ignore_unused` | `trace_event=clk:clk_disable,clk:clk_unprepare trace_buf_size=8M` | `37141280deaa…` |
| `flags-none` | all three | the same trace | `c90a49b630d7…` |

The trace tokens only record, in the ftrace ring buffer (64 MiB RAM in
total), every clock the boot-time pass finds on at 13.7 s (and, after that,
ordinary runtime clock gating), so `flags-check.sh` can tell exactly what the
kernel switched off.  The pass traces a clock before it checks for a `.disable`
op, so six clocks without one (two RCGs, four always-on branches) appear in the
trace and stay on; the check knows them.  Run the check soon after boot: the
buffer wraps after a few hours of runtime gating, and the check says so.

Files:

- `build-variants.sh`: host, no root.  Rebuilds the four sets from the fix2-s2
  images (`build-variants.sh <out dir>`).  It first rebuilds fix2-s2 itself and
  stops unless that matches byte for byte; then it checks that each set differs
  from fix2-s2 in the cmdline only.
- `IMAGES-flags.sha256`: checksums of the four sets and of fix2-s2 (the
  rollback), with paths relative to `~/display-exp`.
- `flash.sh`: tablet, root.  Checks the checksums, prints the flags inside the
  image, saves the partitions if they hold anything other than fix2-s2 or one
  of these sets, then flashes with the existing `~/display-exp/apply.sh`.  It
  does not reboot.  Log: `/var/log/gts9u-flags/flash.log`.
- `flags-check.sh`: tablet, root.  Read-only capture (dmesg, clk_summary,
  genpd, regulators, interconnect, trace) to `/var/log/gts9u-flags/<set>-<boot>/`
  (group adm, so readable over plain SSH), plus a verdict: `ok`, `CHECK` or
  `info` per line, compared with the newest `control` capture.

## Morning plan

Starting point: the tablet as it was left, on fix2-s2 (Stage 2), reachable as
`jstockdale@10.42.0.2`.  Take every capture with the screen on and unlocked,
at least 60 s and preferably less than an hour after boot (the clk trace
wraps after a few hours).  Stop at the first failing step and go to step 7.
Each step takes about 10 minutes.

Run these steps before installing anything from the other overnight kits
(interconnect release, suspend inhibitor, Wi-Fi, boot-hygiene units): the
control capture describes plain fix2-s2, and `flags-check.sh` expects the
Stage 2 interconnect pattern (gem_noc and mc_virt unsynced).  If something
was installed first, take a new `control` capture after it; a released
interconnect then shows as `CHECK  interconnect state is not the Stage 2
pattern` on every capture, control included.

### 0. Copy the sets and the kit (host, no sudo)

```sh
S=/tmp/claude-1000/-home-jstockdale-projects-ubuntu-galaxy-tab-s9-ultra/0b18c5c0-eba0-4cb6-977b-893306d69db3/scratchpad
rsync -a --sparse "$S"/overnight/flags/images/flags-nopd "$S"/overnight/flags/images/flags-noreg \
	"$S"/overnight/flags/images/flags-noclk "$S"/overnight/flags/images/flags-none \
	jstockdale@10.42.0.2:display-exp/
rsync -a "$S"/overnight/flags/kit/ jstockdale@10.42.0.2:flags-kit/
```

Expected: no output.  `$S` is on tmpfs.  If the host has rebooted since, rebuild
the sets first (about 10 s) and copy from there.  Build outside `/tmp`: it is a
quota-limited tmpfs shared with the session scratch files, and a full quota
fails the build with "Disk quota exceeded".

```sh
cd ~/projects/ubuntu-galaxy-tab-s9-ultra
git worktree add --detach ~/ubuntu-gts9u/build/flags-wt overnight/flags
~/ubuntu-gts9u/build/flags-wt/tools/inplace/flags/build-variants.sh ~/ubuntu-gts9u/out/flags-images
diff ~/ubuntu-gts9u/out/flags-images/IMAGES-flags.sha256 ~/ubuntu-gts9u/build/flags-wt/tools/inplace/flags/IMAGES-flags.sha256   # no output
# then: images from ~/ubuntu-gts9u/out/flags-images/, kit from ~/ubuntu-gts9u/build/flags-wt/tools/inplace/flags/
```

### 1. Check the copies (tablet, no sudo)

```sh
cd ~/display-exp && sha256sum -c ~/flags-kit/IMAGES-flags.sha256
```

Expected: 10 lines ending in `: OK`, including `fix2-s2/boot.img` and
`fix2-s2/vendor_boot.img`.  Nothing has been written yet.

### 2. Control capture on fix2-s2 (tablet, sudo)

```sh
sudo sh ~/flags-kit/flags-check.sh control
sh ~/fix1/verify.sh
```

Expected from `flags-check.sh`:
- `ok     clk_ignore_unused present as control expects` (likewise `pd_`, `regulator_`);
- info lines `clk: Not disabling unused clocks`, `PM: genpd: Not disabling unused power domains`, `regulator: Not disabling unused regulators`;
- `info   this is the control; enabled-in-hardware with count 0: 103 clocks` (about 103), with no `... is not enabled-with-count-0 on this boot` line under it;
- `ok     no power domain is on without an active device`;
- health all `ok` (panel id `80 00 04`, coldboot-recover succeeded, Wi-Fi, Bluetooth, sound card, touch, digitizer, cover switch, GPU devfreq, no failure signature);
- last line `saved N files in /var/log/gts9u-flags/control-<boot>`.

Note the `CHECK` lines `verify.sh` prints on fix2-s2: later runs should print the same ones.

### 3. Without `pd_ignore_unused` (tablet, sudo)

```sh
sudo sh ~/flags-kit/flash.sh flags-nopd
sudo reboot
```

Expected from `flash.sh` before the reboot:

```
flags-nopd: flags in the image: clk_ignore_unused regulator_ignore_unused; other tokens: initcall_blacklist=bwmon_driver_init icc_core.dyndbg=+p
boot partition now: fix2-s2/boot.img (dcf881554a27)
vendor_boot partition now: fix2-s2/vendor_boot.img (0742866cbfb0)
flashing flags-nopd with /home/jstockdale/display-exp/apply.sh
cmdline extras in image: initcall_blacklist=bwmon_driver_init icc_core.dyndbg=+p
OK boot <- /home/jstockdale/display-exp/flags-nopd/boot.img (dcf881554a27)
OK vendor_boot <- /home/jstockdale/display-exp/flags-nopd/vendor_boot.img (60f733b44c9a)
done: flags-nopd; reboot to apply
done: flags-nopd; now: sudo reboot
```

After the reboot (the panel lights after the boot-time cycle at about 24 s;
wait until the desktop is up and unlock):

```sh
sudo sh ~/flags-kit/flags-check.sh flags-nopd
sh ~/fix1/verify.sh --gpu
```

Expected:
- `ok     pd_ignore_unused absent as flags-nopd expects`, the other two present;
- info `PM: genpd: Disabling unused power domains`;
- `ok     power domains: same on/off status as the control (GPU domains ignored)`;
- `ok     clk_summary: same enabled-with-count-0 set as the control (GPU clocks aside)`;
- `ok     no power domain is on without an active device`; health all `ok`;
- `verify.sh`: the same lines as the control, Stage 2 `state_synced` pattern, GPU write check 0/24.

By hand, two minutes: screen and touch work; power key off and on twice; close
and open the cover; Wi-Fi up; play a sound; hover the S Pen.

Pass: go on.  Fail: capture first (`sudo sh ~/flags-kit/flags-check.sh flags-nopd`
if SSH works), then step 7.

### 4. Without `regulator_ignore_unused` (tablet, sudo)

```sh
sudo sh ~/flags-kit/flash.sh flags-noreg      # vendor_boot ... (49b6e34b92f3)
sudo reboot
# after boot, screen on, uptime > 60 s:
sudo sh ~/flags-kit/flags-check.sh flags-noreg
sh ~/fix1/verify.sh --gpu
```

Expected: `regulator_ignore_unused absent`; no `regulator: Not disabling unused
regulators` info line; `ok     regulator late cleanup switched nothing off (as
the analysis predicts)`; the rest as in step 3.  A line
`CHECK  regulator cleanup: [ 4x.x] <rail>: disabling` is a finding: note the
rail.  If the panel went dark at about 44 s, it is `display_panel_avdd`.
Manual checks as in step 3, plus leave the screen on past one minute of uptime.

### 5. Without `clk_ignore_unused` (tablet, sudo)

```sh
sudo sh ~/flags-kit/flash.sh flags-noclk      # vendor_boot ... (37141280deaa)
sudo reboot
# after boot, screen on, uptime > 60 s:
sudo sh ~/flags-kit/flags-check.sh flags-noclk
sh ~/fix1/verify.sh --gpu
```

Expected:
- `clk_ignore_unused absent`; info `clk: Disabling unused clocks`;
- under `clocks switched off by clk_disable_unused()`: about twelve `info`
  lines `<clock> clk_disable <time>`, the six predicted gates
  (`disp_cc_mdss_ahb1_clk`, `disp_cc_mdss_rscc_ahb_clk`,
  `disp_cc_mdss_rscc_vsync_clk`, `disp_cc_sleep_clk`, `gcc_boot_rom_ahb_clk`,
  `gcc_pcie_1_phy_rchng_clk`) and the clocks the pass found on but cannot gate;
- `info   found on but not gated (no .disable op, as predicted): disp_cc_sleep_clk_src disp_cc_xo_clk_src gcc_ddrss_gpu_axi_clk gpu_cc_cx_gmu_clk gpu_cc_hub_aon_clk gpu_cc_hub_cx_int_clk`
  (some may be missing: harmless);
- `ok     gated exactly the six clocks the analysis predicted`;
- `info   enabled-with-count-0 in the control, now off:` the same six;
- no new `CHECK` in health; no `more known-pattern splats ... than the control`.

`CHECK  switched off beyond the prediction (no longer enabled-with-count-0): ...`
is a finding (a clock that was on at 13.7 s and that the pass gated, or that a
driver took later): note the names.  It does not fail the step by itself; the
health lines, `verify.sh` and the manual checks decide that.  `info   found
on, not predicted, still enabled-with-count-0 now, so not gated`, `info   GPU
clocks gated at boot ...` and `info   predicted but not switched off` are
harmless.  A `CHECK  the ring buffer overwrote ...` line means the run
came too late after boot; the clk_summary diff still holds.  Manual checks as
in step 3, plus WebGL in Chrome and 60 Hz mode.

### 6. Optional: all three dropped (tablet, sudo)

Only if steps 3-5 passed.

```sh
sudo sh ~/flags-kit/flash.sh flags-none       # vendor_boot ... (c90a49b630d7)
sudo reboot
sudo sh ~/flags-kit/flags-check.sh flags-none
sh ~/fix1/verify.sh --gpu
```

Expected: the union of steps 3-5.

### 7. Back to fix2-s2 (tablet, sudo): the rollback for every step

```sh
sudo sh ~/flags-kit/flash.sh fix2-s2
sudo reboot
# after boot:
tr ' ' '\n' < /proc/cmdline | grep ignore_unused      # all three listed
sh ~/fix1/verify.sh
```

Expected: `OK vendor_boot <- /home/jstockdale/display-exp/fix2-s2/vendor_boot.img
(0742866cbfb0)`; `verify.sh` as in step 2.

### If a set does not come back

- Panel dark but SSH works: run `flags-check.sh` for that set (it is the
  evidence), then step 7.
- No SSH after three minutes, or a boot loop: boot TWRP as for the Stage 3
  recovery and write back fix2-s2's `vendor_boot.img` only:
  `dd if=<fix2-s2/vendor_boot.img> of=/dev/block/by-name/vendor_boot bs=4M`.
  `boot.img` is the fix2-s2 kernel in every set, so it needs no restore.
- ramoops does not survive a reboot on this unit; only the microSD boot logger,
  if installed, records a boot that hangs.

## What is left on the tablet

Nothing is installed.  The kit writes `/var/log/gts9u-flags/` and, only if a
partition held an unknown image, `/var/backups/gts9u-flags/` (`flash.sh` prints
the restore command).  To clean up after reporting:
`rm -r ~/flags-kit ~/display-exp/flags-*` and
`sudo rm -r /var/log/gts9u-flags /var/backups/gts9u-flags`.

The captures to bring back are the directories
`/var/log/gts9u-flags/<set>-<boot>/` (readable as jstockdale over SSH).
`cmdline.txt` in the repo does not change until these pass on the X710 and the
`control` capture has been taken on the Ultra too: the file is shared.
