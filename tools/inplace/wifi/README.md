# In-place Wi-Fi kit for the SM-X710 (gts9wifi)

Brings three Wi-Fi fixes onto an installed X710 without a reflash, with one
reboot. They are the same changes the next device package and the next
vendor_boot carry (`docs/gts9wifi-wifi.md`):

| fault (Stage 2, 2026-11-07) | fix | where |
| --- | --- | --- |
| `wlp1s0` has a Qualcomm default address (`00:03:7f:12:…`) that changes every boot, so the DHCP lease moves | `ubuntu-gts9u-wlan-address.service` reads `/efs/wifi/.mac.info` (EFS mounted `ro,noload`) and applies it before NetworkManager | userspace |
| ath11k's first probe fails at ~11.7 s (`amss.bin` -2, then -110) because the firmware is only on the rootfs | the WCN6855 hw2.1 set goes into the vendor_boot ramdisk, next to the Bluetooth set | vendor_boot |
| `gts9u-pci-rescan` brings Wi-Fi up ~22 s by re-creating the endpoint, which loses the L1.1/L1.2 substates | the rescan helper re-probes in place first, and re-enables the substates after a re-creation (through the `powersupersave` ASPM policy: a re-created device's `link/l1_*` writes alone are capped at L0s+L1); `/etc/ubuntu-gts9u/pcie-l1ss` (`auto`/`on`/`off`) overrides | userspace |

Measured on the Stage 2 boot before the kit (`verify.sh`, boot `df92b567`):
address `00:03:7f:12:09:4b`, udev initialised `wlp1s0` at 22 s,
`gts9u-pci-rescan` ran 13.3 s from 18.6 s, `l1_1_aspm=l1_2_aspm=l1_1_pcipm=l1_2_pcipm=0`,
1 correctable AER error, 121 `msdu_done` warnings in 43 minutes.

## What the kit contains

Built on the host by `stage.sh` (nothing in the kit is hand-edited):

* `images/vendor_boot-<base>-wifi.img`: the vendor_boot the tablet runs, with
  only its ramdisk fragment rebuilt. `make-vendor-boot.sh` first proves the
  repack exact (the base's own ramdisk, repacked, reproduces the base byte for
  byte), then packs the ramdisk `scripts/build-rootfs-overlay.sh` builds for
  gts9wifi and checks DTB, cmdline, bootconfig and header read back identical.
  For the fix2-s2 base (`0742866c…`, cmdline with
  `initcall_blacklist=bwmon_driver_init icc_core.dyndbg=+p`) the variant is
  `03cc1ccb8eb43c2bad94aad23f7b6c606b00721b075e08b4a9c83a908e8daa94`; the
  fragment grows from 148,595 to 2,944,962 bytes (vendor_boot is a 96 MiB
  partition; init_boot is not touched).
* `IMAGES`: base sha256, variant sha256, file. `install.sh` writes a variant
  only over its own base.
* `FIRMWARE.sha256`: the five ramdisk files. `install.sh` refuses unless the
  rootfs copies under `/usr/lib/firmware/ath11k/WCN6855/hw2.1` are identical
  (the ramdisk serves the first probe, the rootfs every later load).
* `files/`: the helpers and units from `packaging/ubuntu-gts9u-device`.
* `install.sh`, `rollback.sh`, `verify.sh`, `SHA256SUMS`, `SOURCE`.

## Morning test plan

`(host)` runs on the build PC as jstockdale, `(tablet)` in an SSH session on
squirtle. **sudo** marks the steps that need it. The tablet's Wi-Fi address
changes once at step 4 (new MAC, new DHCP lease), so use the USB link
(`ssh jstockdale@10.42.0.2`) or `-o HostKeyAlias=10.42.0.2` with the new
address.

1. **(host) Stage the kit.** A staged copy built overnight is in
   `/tmp/claude-1000/-home-jstockdale-projects-ubuntu-galaxy-tab-s9-ultra/0b18c5c0-eba0-4cb6-977b-893306d69db3/scratchpad/overnight/wifi/kit`.
   To rebuild it (needs the fix2-s2 `vendor_boot.img`, sha256 `0742866c…`: the
   host keeps it in
   `~/ubuntu-gts9u/backups/x710-fix-integration-2026-11-06/fix2-images/fix2-s2/`,
   the tablet in `~/display-exp/fix2-s2/`):

   ```
   tools/inplace/wifi/stage.sh ~/ubuntu-gts9u/out/inplace-wifi \
       ~/ubuntu-gts9u/backups/x710-fix-integration-2026-11-06/fix2-images/fix2-s2/vendor_boot.img
   ```

   Expected: `self-test: repack reproduces … (0742866cbfb0) exactly`, then the
   five added `usr/lib/firmware/ath11k/WCN6855/hw2.1/*` files, and an `IMAGES`
   line `0742866c… 03cc1ccb… images/vendor_boot-0742866cbfb0-wifi.img`.

2. **(host) Copy it:** `scp -r <kit dir> jstockdale@10.42.0.2:wifi-kit`

3. **(tablet) Baseline:** `sh ~/wifi-kit/verify.sh`
   Expected now: `FAIL` for the rescan outcome, the EFS address, the Qualcomm
   prefix and the link states. This records the "before" line in
   `~/wifi-kit/verify-history.tsv`.

4. **(tablet, sudo) Install:** `sudo sh ~/wifi-kit/install.sh`
   Expected in the output (and `/var/log/gts9u-wifi-kit.log`):
   ```
   vendor_boot holds 0742866cbfb0: base (images/vendor_boot-0742866cbfb0-wifi.img)
   backups in /var/backups/ubuntu-gts9u-wifi
   userspace: wlan-address and pci-rescan installed and enabled ...
   check: EFS (/dev/disk/by-partlabel/efs) wifi/.mac.info: 68:df:e4:...
   vendor_boot now holds 03cc1ccb8eb4 (read back from the disk)
   done.  Reboot now ...
   ```
   It refuses (and changes nothing) if vendor_boot holds anything other than
   fix2-s2 or its Wi-Fi variant: another kit flashed first. Re-stage with that
   image as the base (step 1) instead of forcing it.

5. **(tablet, sudo)** `sudo reboot`

6. **(tablet) First boot:** `sh ~/wifi-kit/verify.sh`, then
   **(sudo)** `sudo sh ~/wifi-kit/verify.sh` for the kernel-log lines.
   Expected:
   ```
   ok    Wi-Fi interface wlp1s0 on ath11k_pci
   ok    gts9u-pci-rescan: endpoint bound at boot (first probe succeeded, no rescan)
   info  udev initialised wlp1s0 at ~13-20 s          (was 22 s)
   info  gts9u-pci-rescan ran 0.0 s ...               (was 13.3 s)
   ok    wlp1s0 address 68:df:e4:... = EFS (set-from 00:03:7f:...)   or (already)
   info  udev link file: /run/systemd/network/05-ubuntu-gts9u-wlan.link   (if udev applied it)
   ok    link: l0s_aspm=1 l1_aspm=1 l1_1_aspm=1 l1_2_aspm=1 l1_1_pcipm=1 l1_2_pcipm=1
   ok    dmesg: no ath11k probe failure                (sudo run)
   info  vendor_boot 03cc1ccb8eb4: Wi-Fi variant ...   (sudo run)
   ```
   Also check that Bluetooth still comes up (its firmware shares the
   fragment): `bluetoothctl show | head -n 3` shows `Powered: yes` and the
   `68:DF:E4:…:C1` controller.

7. **(tablet, sudo)** `sudo reboot`, then `sh ~/wifi-kit/verify.sh` again.
   Expected: the same `ok` lines plus `ok    one address across 2 boots: 68:df:e4:...`
   and the same IPv4 lease in the last two history lines. A third reboot is
   cheap insurance.

8. **(tablet) Soak with the L1 substates on:** `sh ~/wifi-kit/verify.sh --soak 600`
   while using the tablet normally (a browser download or a video helps).
   Expected: ping loss 0% (a few lost pings during a scan are fine), `ok    no
   new AER errors during the soak`, `l1_1_aspm=1 l1_2_aspm=1` after it, the
   interface still present. Note the `msdu_done` count against the baseline
   (121 in 43 min); it predates this work and is not expected to change.

## If something is wrong

* **Wi-Fi misbehaves with L1.2 (AER errors, stalls, drops):** switch the
  substates off now, without a reboot (**sudo**):
  `sudo mkdir -p /etc/ubuntu-gts9u && echo off | sudo tee /etc/ubuntu-gts9u/pcie-l1ss && sudo systemctl restart gts9u-pci-rescan`
  (`/etc/ubuntu-gts9u` does not exist on the tablet yet, so the `mkdir` is
  needed; `verify.sh` then shows the substates 0). It stays off on every boot
  until the file is removed (`sudo rm /etc/ubuntu-gts9u/pcie-l1ss`). Writing
  0 works under any ASPM policy, powersupersave included.
* **Address or rescan helpers misbehave:** `sudo sh ~/wifi-kit/rollback.sh`
  undoes everything (vendor_boot from the backup, units removed or restored),
  then `sudo reboot`. If another kit wrote vendor_boot after this one,
  rollback stops before changing anything (`neither the backup nor this kit's
  Wi-Fi variant`): roll that kit back first, or add `--force` to put the
  fix2-s2 backup back anyway (that kit's vendor_boot change is then lost).
* **Only the vendor_boot change is suspect:** `sudo sh ~/wifi-kit/rollback.sh --images-only`,
  `sudo reboot`. The userspace part works without it (the rescan helper then
  re-probes in place and the address helper uses its `.link`).
* **The tablet does not boot after step 5:** from TWRP
  ```
  [ -d /data/var ] || mount -t ext4 /dev/block/by-name/userdata /data
  cd /data/var/backups/ubuntu-gts9u-wifi && sha256sum -c vendor_boot.sha256 &&
    dd if=vendor_boot.img of=/dev/block/by-name/vendor_boot bs=4M conv=fsync
  ```
  (or the same `dd` from `/data/home/jstockdale/display-exp/fix2-s2/vendor_boot.img`,
  sha256 `0742866c…`), reboot, then `sudo sh ~/wifi-kit/rollback.sh`.
  `sudo sh ~/display-exp/apply.sh fix2-s2` is the same image route from a
  running system.

## Notes

* Composition with other vendor_boot changes: the variant is "base +
  firmware", nothing else. A vendor_boot from another workstream (cmdline
  flags, panel) gets the firmware by passing it as the base to `stage.sh`;
  `make-vendor-boot.sh` keeps its cmdline and DTB byte for byte.
* The address helper is gated on `samsung,gts9wifi`. It never writes EFS and
  leaves nothing persistent: the `.link` is in `/run`, derived on every boot.
* Logs: `/var/log/gts9u-wifi-kit.log`; this boot's helper outcomes are in
  `/run/ubuntu-gts9u/wlan-address` and `/run/ubuntu-gts9u/pci-rescan` (readable
  without the journal, which currently loses the first ~40 s of each boot).
