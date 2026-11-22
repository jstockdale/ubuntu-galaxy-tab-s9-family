# UFS HS-G5 kit (SM-X710, November 2026)

The kit tries the UFS link at HS-G5 Rate-A. Mainline 7.2-rc3 maps SM8550's
300 MHz UniPro OPP to HS-G4, although the UFS 4.0 part, the controller and the
PHY all support G5 ([porting log](../../docs/porting-log.md#why-the-link-stops-at-hs-g4-2026-11-07)).
`kernel/patches/ufs-qcom-hs-g5-at-300mhz-sm8550.patch` maps 300 MHz to G5 on
UFS controller HW ver 5, and `build-mainline-kernel.sh` applies it for
`DEVICE=gts9wifi` only.

It installs on top of fix7-ice (fix6's kernel, the ICE clock DTB,
`iommu.strict=0` in `vendor_boot`):

- `boot`: fix7-ice's image with the G5 kernel in place of fix6's. It has the
  same DTB appended, so `vendor_boot` is unchanged.
- The module tree built with that kernel. `CONFIG_MODULE_ALLOW_BTF_MISMATCH`
  is off and this build has its own signing key. The tree's
  `updates/qcom_spss_irq.ko` therefore replaces device package 2.57's copy
  while the kit is installed (`dpkg --verify` reports it), and
  `rollback.sh` puts the package's copy back.

## Build (host)

```sh
X=/path/to/scratch/build-g5
DEVICE=gts9wifi UBUNTU_WORKDIR=$HOME/ubuntu-gts9u \
  KERNEL_WORKTREE=$X/linux-src-gts9wifi KERNEL_BUILD_DIR=$X/linux-gts9wifi \
  KERNEL_OUT_DIR=$HOME/ubuntu-gts9u/out/kernel-gts9wifi-g5 \
  bash scripts/build-mainline-kernel.sh
bash tools/inplace/ufs-g5/make-kit.sh \
  $HOME/ubuntu-gts9u/out/kernel-gts9wifi-g5 $HOME/ubuntu-gts9u/backups/ufs-g5-kit
```

`make-kit.sh` refuses unless the kernel configuration is fix6's and the DTB is
fix7-ice's. Its packing reproduces fix7-ice's `boot.img` (`cc878408…`) byte
for byte from the fix6 kernel output.

## Install, check, roll back (tablet)

Copy the kit to `~/ufs-g5`, then:

```sh
sudo sh ~/ufs-g5/install.sh     # then: sudo reboot
sh ~/ufs-g5/verify.sh           # non-root
sudo sh ~/ufs-g5/rollback.sh    # then: sudo reboot
```

`install.sh` stops before writing anything unless all of the following hold:

- the kit matches its checksums;
- `boot` and `vendor_boot` hold fix7-ice;
- root is writable;
- the battery is at 30 % or more;
- dpkg is idle and clean;
- there is 400 MB free.

It then saves the `boot` partition to `/var/backups/ubuntu-gts9u-ufs-g5/`
(root-only, hash-checked) and swaps the module tree. It writes `boot` last,
synchronously, and reads it back from the disk.

`verify.sh` checks the following:

- root is writable;
- no UFS PHY, UIC or link errors, and no module BTF, signature or symbol
  failures, this boot;
- the gear and rate during a 1 GiB `O_DIRECT` read.

On fix7-ice the load sample reads `HS_GEAR4/HS_RATE_A` (2026-11-07). The G5
kernel must read `HS_GEAR5/HS_RATE_A`.

If the tablet does not come back, boot TWRP and write
`/var/backups/ubuntu-gts9u-ufs-g5/boot-fix7-ice.img` from the Ubuntu root to
`boot`. Then run `rollback.sh` on the next boot to restore the module tree.

## Tests still to run

1. `verify.sh` after the first boot.
2. fio on a freshly rewritten 2 GiB file, compared with 1844 MB/s read and
   1595 MB/s write.
3. 30 minutes of mixed fio, then `verify.sh` again.
4. 20 suspend and resume cycles (`scripts/diagnostics/suspend/`), with
   `verify.sh` after each.
5. Idle drain compared with fix7-ice.
6. A reboot.
