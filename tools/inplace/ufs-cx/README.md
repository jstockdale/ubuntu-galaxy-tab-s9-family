# CX-vote kit (SM-X710, November 2026)

The kit goes on top of the [UFS HS-G5 kit](../ufs-g5/README.md). It makes the
UFS, USB and PCIe CX votes reach the rail, and drops them across system
sleep ([porting log](../../docs/porting-log.md#the-ufs-clocks-cx-vote-never-reached-the-rail-2026-11-07)):

- **The kernel** is the G5 kernel plus
  `ufshcd-drop-opp-votes-in-system-suspend.patch` and
  `dwc3-qcom-drop-required-opps-vote-in-system-suspend.patch`. It keeps fix6's
  configuration.
- **The DTB** is fix7-ice's plus `power-domains = <&rpmhpd RPMHPD_CX>` on gcc,
  and nothing else (sha256 `290a5395…`). It goes in both images that carry a
  DTB:
  - `boot`: the kernel with it appended, packed with the G5 kit's recipe. With
    the G5 kernel and fix7-ice's DTB that recipe reproduces the G5 kit's
    `boot.img` byte for byte.
  - `vendor_boot`: fix7-ice's image with only the DTB swapped. `make-kit.sh`
    proves that the repack reproduces fix7-ice's body byte for byte, then reads
    every other field back unchanged. fix7-ice's AVB footer is stale: its
    digest was computed before ` iommu.strict=0` was patched into the cmdline.
    vbmeta is disabled, so ABL never checks it. The new image gets a fresh,
    correct footer.
- **The module tree** comes from that kernel build. As with the G5 kit, its
  `updates/qcom_spss_irq.ko` replaces the device package's copy while the kit
  is installed.
- **Optional:** `ubuntu-gts9u-device` 2.58, built against this kernel. It lets
  the USB controller runtime-suspend once the boot-time panel cycle has run
  (porting log, "dwc3 runtime PM on the SM-X710"). It is a separate step,
  after the kit has proven itself.

## Build (host)

The release build owns `~/ubuntu-gts9u/out/packages` and `out/local-debs`.
Build the device package into its own directory.

```sh
X=/path/to/scratch/build-cxg5
DEVICE=gts9wifi UBUNTU_WORKDIR=$HOME/ubuntu-gts9u \
  KERNEL_WORKTREE=$X/linux-src-gts9wifi KERNEL_BUILD_DIR=$X/linux-gts9wifi \
  KERNEL_OUT_DIR=$HOME/ubuntu-gts9u/out/kernel-gts9wifi-cx \
  bash scripts/build-mainline-kernel.sh
DEVICE=gts9wifi UBUNTU_WORKDIR=/path/to/scratch/devpkg \
  DEB_OUT_DIR=/path/to/scratch/devpkg/out \
  SPSS_IRQ_OUT_DIR=$X/spss-irq-module-gts9wifi \
  KERNEL_BUILD_DIR=$X/linux-gts9wifi \
  KERNEL_OUT_DIR=$HOME/ubuntu-gts9u/out/kernel-gts9wifi-cx \
  fakeroot bash scripts/build-device-package.sh
DEVICE_DEB=/path/to/scratch/devpkg/out/ubuntu-gts9u-device_2.58_arm64.deb \
  bash tools/inplace/ufs-cx/make-kit.sh \
  $HOME/ubuntu-gts9u/out/kernel-gts9wifi-cx \
  $HOME/ubuntu-gts9u/backups/fix7-ice/vendor_boot.img \
  $HOME/ubuntu-gts9u/backups/ufs-cx-kit
```

`build-device-package.sh` takes the fingerprint secure owner from
`$UBUNTU_WORKDIR/out/fingerprint-secure`. Link that directory into the scratch
work directory.

## Install, check, roll back (tablet)

Copy the kit to `~/ufs-cx`, then:

```sh
sudo sh ~/ufs-cx/install.sh        # then: sudo reboot
sh ~/ufs-cx/verify.sh              # the G5 kit's checks and this kit's
sudo sh ~/ufs-cx/verify.sh         # also the CX checks (debugfs)
sudo sh ~/ufs-cx/suspend-votes.sh  # one pm_test=devices cycle, traced
sudo sh ~/ufs-cx/rollback.sh       # back to the G5 kit; then: sudo reboot
```

`install.sh` stops before writing anything unless all of the following hold:

- the kit matches its checksums;
- the G5 kit is installed: its marker exists, `boot` holds its image
  (`4179f406…`) and `vendor_boot` holds fix7-ice's (`4b8b2f92…`);
- the running kernel is `7.2.0-rc3-dirty`;
- root is writable;
- the battery is at 30 % or more;
- dpkg is idle and clean;
- there is 600 MB free.

It saves both partitions and the G5 module tree to
`/var/backups/ubuntu-gts9u-ufs-cx/` (root-only, hash-checked), then swaps the
module tree. It writes `vendor_boot` and then `boot`, synchronously, and reads
each back from the disk. If the saved Ubuntu boot set holds `boot.img` and
`vendor_boot.img`, it keeps them in step.

Either half on its own still boots. ABL takes the DTB from `vendor_boot`, and
both kernels run on both DTBs.

Roll this kit back before the G5 kit's own `rollback.sh`. That script knows
nothing of `vendor_boot` and would leave this kit's DTB in it.

If the tablet does not come back, boot TWRP and write
`/var/backups/ubuntu-gts9u-ufs-cx/vendor_boot-fix7-ice.img` to `vendor_boot`
and `boot-g5.img` to `boot`, both from the Ubuntu root. Then run `rollback.sh`
on the next boot to restore the module tree.

## What the checks expect

- `verify.sh` keeps the G5 kit's checks:
  - root is writable;
  - no UFS PHY, UIC or link errors and no module failures this boot;
  - HS-G5 Rate-A during a 1 GiB `O_DIRECT` read.

  It adds a check that the kernel logged no vote errors this boot.
- Run with sudo, `verify.sh` also checks the CX subdomains and the loaded state:
  - `ufs_phy_gdsc`, `ufs_mem_phy_gdsc`, `usb30_prim_gdsc` and `pcie_0_gdsc`
    are CX subdomains;
  - under load, the ICE clock is at 403 MHz, `ufs_phy_gdsc` votes 256 or more,
    and CX is at least that.
- `suspend-votes.sh`: during one `pm_test=devices` cycle, the UFS calls
  `dev_pm_opp_set_opp(…, NULL)` and then sets an OPP again.
  - If the USB controller was active, it votes 0 and then 256.
  - If it was runtime-suspended (device 2.58), it only votes 256, when
    `dwc3_pm_resume()` makes it active.

## Optional: device package 2.58

Only after `verify.sh` and `suspend-votes.sh` pass on the kit.

```sh
sha256sum -c --ignore-missing ~/ufs-cx/SHA256SUMS
sudo apt-get install --simulate ~/ufs-cx/ubuntu-gts9u-device_2.58_arm64.deb   # must remove nothing
sudo apt-get install ~/ufs-cx/ubuntu-gts9u-device_2.58_arm64.deb
sudo reboot
```

It takes effect at the next boot, after the panel cycle. The test matrix is
in the porting log ("dwc3 runtime PM on the SM-X710"). To keep the controller
on without removing anything, create `/etc/ubuntu-gts9u/usb-runtime-pm-off`.
To go back, install the X710's 2.57 from
`~/ubuntu-gts9u/backups/device-2.57/` with `--allow-downgrades`. Do not use the
release build's `out/packages`: that 2.57 is the Ultra's.

## Tests still to run

1. `verify.sh`, with and without sudo, after the first boot.
2. `suspend-votes.sh`.
3. fio as for the G5 kit. The CX change must not cost throughput.
4. The suspend ladder (`scripts/diagnostics/suspend/`): `pm_test` levels, then
   real s2idle and deep. `journalctl -t ubuntu-gts9u-sleep-stats` should show
   `cxsd` counting once the interconnect floor is released.
5. Device 2.58: the porting log's test matrix.
